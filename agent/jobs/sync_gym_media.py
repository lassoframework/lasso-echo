"""
sync_gym_media.py — the nightly gym-media Drive sync (gym_media_drive spec §4).

Runs in the SAME daily slot as the podcast indexer, behind GYM_DRIVE_CONNECT (or
the per-gym pilot allowlist). For each ACTIVE, gym-drive media_source, staggered
30 s apart:

  1. One recursive walk (depth <= 4) of the source's folder. A 403 on a
     previously-connected source marks it revoked_externally + notifies the coach
     channel — no crash (§1.5f).
  2. MIME filter image/* + video/* (docs/pdf/zip logged + skipped). Dedupe on
     content_hash across re-uploads AND folders (earliest kept).
  3. Insert new assets; PATCH changed indexer-owned fields only (never probe /
     vision / used_count / last_used_at).
  4. Assets whose Drive id VANISHED -> eligible=false, reject_reason=
     'removed_from_drive', and any PENDING calendar row referencing them is flipped
     back via the media-not-ready pattern.
  5. Budgeted probe pass: up to GYM_DRIVE_PROBE_MAX_PER_RUN unprobed VIDEO
     candidates are downloaded, ffprobed (duration/aspect), the §4 gate written
     back. Unprobed stays unselectable (fail closed). Temp files always deleted.
  6. Deny sweep: gym_media_selector.observe_denials() returns denied assets to the
     pool.
  7. Per-GYM new-asset digest to the coach channel (best effort).

Degrades cleanly: no SA key / no Supabase creds -> the identity check fails closed (tenant_identity_unestablished) or the lane is unarmed, so nothing is walked or inserted — a log line, no-op. Nothing
here stages, publishes, or writes calendar rows (beyond flipping a pending row
whose media vanished). NOTHING here logs a secret.

INVENTORY MUTATION RECEIPT FENCE (default OFF, DRAFT): when
AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED=true, every per-source index/asset
mutation (steps 2-6b) runs under ONE begin/complete receipt from
agent/local_inventory_mutation.py, and every effect is re-read from the store
BEFORE the receipt may COMPLETE (_verify_index_readback). An uncertain
configuration, duplicate/collision, write, flip, or readback holds the exact
mutation pending and the source returns {"ok": False, "held": True} — no
digest, no certified success. Drive itself stays read-only (walk/download
only); originals, source aliases, approvals, and consent/hold gates are never
rewritten. Fence OFF: byte-identical legacy behavior.

GAP 3 (audit of PR #68, client_dm_support): "the coach channel" in steps 1 and 7
above is a CLIENT-FACING Slack channel, and this module used to post straight to
it with no reference to conditions.compose(), the outbox, or the client_dm_support
three-flag interlock at all -- reachable via the nightly cron with NO
client_dm_support flag involved, and independently via the gym_drive_sync action
(gated on AGENT_CLIENT_DM_AUTOFIX alone). `_client_channel_if_armed(gym_id)` now
gates every one of these on `config.slack_convo_client_reply_armed('echo')` -- the
SAME flag every other client-facing send in this repo already requires -- falling
back to the existing internal `#ops` channel when it is not armed.
"""
from __future__ import annotations

import os
import tempfile
import time
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .. import config, gym_media_index as _idx
from .. import local_inventory_mutation as _mutation

# The indexer-owned columns compared for the changed-row PATCH. Probe columns
# (duration/width/height/aspect/crop_hint), vision_json, rendition_*, and selector
# columns (used_count/last_used_at) are deliberately absent: a re-sync may never
# clobber them.
_OWNED_FIELDS = ("kind", "title", "mime_type", "size_bytes", "content_hash",
                 "drive_modified", "source_id")

_STAGGER_SEC = 30.0


# ---- inventory mutation receipt fence (default OFF) -------------------------
# Mirrors the guard in agent/intake_ingest.py / agent/intake_web.py: when
# AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED=true, the whole per-source index
# mutation (inserts, owned-field patches, the removed-sweep + pending flip,
# probe writes, pre-render persistence, classifier quarantine) runs under ONE
# begin/complete receipt, and every effect is re-read from the store BEFORE the
# receipt may COMPLETE. Any uncertain configuration, collision, write, or
# readback holds the exact mutation pending for operator reconciliation and the
# source returns a held summary — no digest, no COMPLETE. Fence OFF (default):
# every write runs exactly as it always has.
_active_mutation = ContextVar("gym_media_sync_inventory_mutation", default=None)


def _fence_config(gym_id):
    """MutationConfig for this gym's fence, or None when the fence is OFF."""
    if not _mutation.enabled():
        return None
    root = (Path(config.LIBRARY_PATH) / gym_id).absolute()
    root.mkdir(parents=True, exist_ok=True)
    return _mutation.configured(gym_id, root)


def _run_mutation(gym_id, kind, request_payload, apply):
    """Run apply(conn) under the mutation receipt protocol when armed, else
    directly. MutationHold propagates (hold, never fake success)."""
    active = _active_mutation.get()
    if active is not None:
        if active["cfg"].gym_id != gym_id:
            raise _mutation.MutationHold("gym_media_mutation_binding_invalid")
        # The whole source disposition already owns the canonical lock. Nested
        # effects belong to that receipt and must never reacquire its flock.
        return apply(active["conn"])
    cfg = _fence_config(gym_id)
    if cfg is None:
        return apply(None)
    authority = _mutation.MutationAuthority.from_environment()

    def _apply(conn):
        token = _active_mutation.set({"cfg": cfg, "conn": conn})
        try:
            return apply(conn)
        finally:
            _active_mutation.reset(token)

    try:
        return _mutation.run(cfg, authority, kind, request_payload, _apply)
    finally:
        authority.close()


class _EffectReadbackStore:
    """Track inserted rows and attempted patches, including swallowed errors.

    Final readback compares every written field after subsequent effects have
    overridden earlier ones. A failed write remains sticky until verification.
    """
    def __init__(self, store, gym_id, source_id):
        self.store, self.gym_id, self.source_id = store, gym_id, source_id
        self.expected = {}
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.store, name)

    def _write(self, kind, identifier, fields, apply):
        self.expected.setdefault((kind, identifier), {}).update(fields)
        try:
            return apply()
        except Exception:
            self.failed = True
            raise

    def insert_assets_ignore_conflicts(self, rows):
        # A conflict is not an insert effect: preserve the existing owner's
        # probe/consent/selector state. Snapshot input before the store call so
        # a store mutating its arguments cannot rewrite the expected readback.
        expected = {row["id"]: dict(row) for row in rows}
        try:
            inserted = self.store.insert_assets_ignore_conflicts(rows)
            for identifier in inserted:
                self.expected.setdefault(("asset", identifier), {}).update(
                    expected[identifier])
            return inserted
        except Exception:
            self.failed = True
            raise

    def update_asset(self, identifier, fields):
        return self._write("asset", identifier, fields,
                           lambda: self.store.update_asset(identifier, fields))

    def update_indexed_asset_if_hash(self, gym_id, identifier, old_hash, fields):
        return self._write("asset", identifier, fields,
                           lambda: self.store.update_indexed_asset_if_hash(
                               gym_id, identifier, old_hash, fields))

    def update_source(self, identifier, fields):
        return self._write("source", identifier, fields,
                           lambda: self.store.update_source(identifier, fields))

    def verify(self):
        if self.failed:
            raise _mutation.MutationHold("gym_media_write_effect_uncertain")
        try:
            for (kind, identifier), fields in self.expected.items():
                current = (self.store.get_asset(identifier) if kind == "asset"
                           else self.store.get_source(identifier))
                if (not current or current.get("gym_id") != self.gym_id
                        or (kind == "asset" and current.get("source_id") != self.source_id)
                        or (kind == "source" and current.get("id") != self.source_id)
                        or any(current.get(key) != value for key, value in fields.items())):
                    raise _mutation.MutationHold("gym_media_readback_mismatch")
        except _mutation.MutationHold:
            raise
        except Exception:
            raise _mutation.MutationHold("gym_media_readback_failed") from None


def _update_source_verified(store, gym_id, source_id, fields):
    if _active_mutation.get() is None:
        return store.update_source(source_id, fields)
    effects = _EffectReadbackStore(store, gym_id, source_id)
    result = effects.update_source(source_id, fields)
    effects.verify()
    return result


def _verify_index_readback(store, gym_id, rows, vanished, probed_ids):
    """Fenced mode only: re-read every index effect BEFORE the receipt may
    COMPLETE. A missing row, a tenant/source drift, an owned-field mismatch, a
    vanished asset still eligible, or a probed video still without a duration
    holds the whole receipt for reconciliation. Never certifies from the
    in-memory view alone."""
    try:
        for r in rows:
            current = store.get_asset(r["id"])
            if not current:
                raise _mutation.MutationHold("gym_media_readback_missing")
            if (current.get("gym_id") != gym_id
                    or current.get("source_id") != r.get("source_id")):
                raise _mutation.MutationHold("gym_media_readback_mismatch")
            for f in _OWNED_FIELDS:
                if current.get(f) != r.get(f):
                    raise _mutation.MutationHold("gym_media_readback_mismatch")
        for aid in vanished:
            current = store.get_asset(aid)
            if (not current or current.get("eligible") is not False
                    or current.get("reject_reason") != _idx.REJECT_REMOVED):
                raise _mutation.MutationHold("gym_media_readback_mismatch")
        for aid in probed_ids:
            current = store.get_asset(aid)
            if not current or current.get("duration_sec") is None:
                raise _mutation.MutationHold("gym_media_readback_mismatch")
    except _mutation.MutationHold:
        raise
    except Exception:
        raise _mutation.MutationHold("gym_media_readback_failed") from None


def _tenant_identity(gym_id):
    """(established, resolved_key) for the tenant-source binding guard.

    established False means identity CANNOT be established: the account-key
    identity plane was unreadable/truncated, or the stored key is not a uniquely
    registered tenant. The sync fails closed in that case (see sync_source).

    This deliberately does NOT use gym_media_routes._resolve_stale_fingerprint:
    that wrapper is written for the request path, where returning the key
    UNCHANGED on any uncertainty is the safe repair. For the sync guard an
    unchanged return is ambiguous — it means both "this IS the live key" and "we
    could not tell". account_key_resolve.resolve_known_source_keys removes the
    ambiguity: one COMPLETE fresh plane read, and a key is returned ONLY when it
    is proven live or uniquely mapped to a live key; every uncertainty returns
    nothing. It never raises (an uncertain identity authorizes no write).
    """
    try:
        from .. import account_key_resolve as _akr  # noqa: PLC0415
        verified = _akr.resolve_known_source_keys([gym_id])
    except Exception:  # noqa: BLE001 - the guard itself must never crash the sync
        return False, gym_id
    if gym_id not in verified:
        return False, gym_id
    return True, verified[gym_id]


def _resolve_verified_keys(gym_ids, log=None):
    """ONE complete fresh identity-plane read for a whole nightly run.

    resolve_known_source_keys is a full-fleet snapshot; calling it once per
    SOURCE would multiply an expensive fleet-wide read by the fleet size (and a
    partially-failing per-source read could suppress an entire fleet's pass one
    source at a time with no single alarm). run() calls this exactly once and
    passes the trusted per-run result into sync_source(verified_keys=...).

    Fail closed: ANY exception or incomplete read returns {}, which makes every
    source this run refuse with tenant_identity_unestablished (skips one pass;
    never writes on an unproven identity). Never raises.
    """
    try:
        from .. import account_key_resolve as _akr  # noqa: PLC0415
        return _akr.resolve_known_source_keys(list(dict.fromkeys(gym_ids)))
    except Exception as e:  # noqa: BLE001 - uncertain identity authorizes no write
        if log:
            log(f"batched tenant-identity resolution failed "
                f"({type(e).__name__}: {e}); every source this run will be "
                f"refused (fail closed, retry on the next scheduled pass)")
        return {}


def _identity_from_verified(verified, gym_id):
    """(established, resolved) from a TRUSTED per-run batch result. The batch
    was produced by a complete fresh resolve_known_source_keys read this same
    run, so absence from the map means unproven identity (fail closed)."""
    if gym_id not in verified:
        return False, gym_id
    return True, verified[gym_id]


def _drop_reingested(rows, gym_id, log):
    """Split (kept, skipped_titles): drop any row whose content_hash is one of Echo's
    own past Story renders (the re-ingest guard). Never raises: a ledger failure keeps
    the row (fail open on the guard is safe — the worst case is the file is treated as
    normal media, never a silent repost, because staging still runs every A+ gate)."""
    try:
        from .. import story_ledger
    except Exception:  # noqa: BLE001
        return rows, []
    kept, skipped_titles = [], []
    for r in rows:
        ch = r.get("content_hash")
        try:
            is_echo = bool(ch) and story_ledger.is_echo_render(ch)
        except Exception as e:  # noqa: BLE001
            log(f"re-ingest guard lookup failed for {r.get('title')!r}: "
                f"{type(e).__name__}: {e}")
            is_echo = False
        if is_echo:
            skipped_titles.append(r.get("title") or r.get("id") or "")
            log(f"re-ingest guard: skipping {r.get('title')!r} for {gym_id} "
                f"(content_hash matches an Echo Story render; never re-ingested)")
        else:
            kept.append(r)
    return kept, skipped_titles


def _quarantine_finished(store, asset, verdict, gym_id, log, *, source=""):
    """Quarantine a confidently-FINISHED asset out of the raw pool THE SAME WAY any
    other ineligible asset is quarantined: eligible=False + reject_reason (never a
    delete, never a new gate — story_candidates._eligible_raw and
    gym_media_selector.pick_media both already fail closed on eligible is not True).

    2026-09-01 fix: this write is the actual gap the proof run found. A FINISHED
    verdict (direct, or an echo-auto-sort resolution of an ambiguous file) was
    computed correctly and then thrown away — nothing downstream ever consulted it.
    Returns True when the write happened (skips an already-ineligible asset)."""
    if store is None or asset.get("eligible") is False:
        return False
    try:
        store.update_asset(asset.get("id") or "", {
            "eligible": False, "reject_reason": _idx.REJECT_FINISHED_CONTENT})
    except Exception as e:  # noqa: BLE001 - one bad write never sinks the sort
        log(f"quarantine write failed for {asset.get('title')!r}: "
            f"{type(e).__name__}: {e}")
        return False
    log(f"classifier[{source}]: {asset.get('title')!r} is FINISHED content "
        f"(reasons: {'; '.join(verdict.reasons) or 'none'}) -> quarantined out of "
        f"the raw pool for {gym_id}")
    return True


def _sort_ambiguous(assets, gym_id, log, *, store=None, ocr_signals=None):
    """STORY_CLASSIFIER pass (default ON, spec §0): classify every freshly-indexed
    asset raw / finished / ambiguous. AMBIGUOUS enqueues to the "Sort these" queue
    for a human (never auto-decided) unless echo-auto-sort is armed. A CONFIDENT
    FINISHED verdict — direct, or an echo-auto-sort resolution — is QUARANTINED
    out of the raw pool (see _quarantine_finished): this is the 2026-09-01 fix for
    the gap the proof run found, where a FINISHED verdict was computed and then
    discarded with no effect on eligibility. Returns the count enqueued for a human.

    It never posts, stages, or composes. A declared upload lane / Drive folder
    mapping would override the classifier (intent beats inference), but the
    nightly Drive walk has no per-file declaration, so unmapped files run the
    inference path here. `ocr_signals`, when given, is {asset_id: (has_burned_text,
    cut_density)} from the LIVE probes (agent/story_classifier.default_ocr_reader /
    default_cut_probe) already run this pass on the SAME downloaded bytes the
    ffprobe step used (agent/jobs/sync_gym_media.py sync_source step 5) — no
    duplicate download. Without it (e.g. an existing already-probed asset never
    re-downloaded this run), the classifier still decides from real metadata alone,
    offline-safe. Best effort: a classifier / queue / quarantine failure never
    sinks the sync."""
    if not config.story_classifier_enabled():
        return 0
    try:
        from .. import story_classifier as _sc, story_sort_queue as _q
    except Exception:  # noqa: BLE001
        return 0
    enqueued = 0
    auto_sorted = 0
    quarantined = 0
    default_on = config.sort_ambiguous_default_enabled()
    for a in assets:
        try:
            sig = _sc.gather_signals(a)                 # metadata baseline
            extra = (ocr_signals or {}).get(a.get("id") or "")
            if extra is not None:
                sig.has_burned_text, sig.cut_density = extra  # real, live signals
            verdict = _sc.classify(sig)                 # ledger guard runs inside classify
            if verdict.verdict == _sc.FINISHED:
                # A confident, non-ambiguous FINISHED verdict (metadata alone, or
                # OCR/cut-density backed): quarantine it directly, no human queue.
                if _quarantine_finished(store, a, verdict, gym_id, log,
                                        source="direct"):
                    quarantined += 1
                continue
            if verdict.verdict == _sc.AMBIGUOUS:
                # SELF-RUNNING SORT (Blake 2026-08-31: "the only human thing should be
                # the gym approving the post" — a 774-file 'Sort these' queue is a staff
                # task). When armed, Echo DECIDES ambiguous files instead of queueing:
                # lean finished only on a real finished signal (edit-suite filename, or
                # a finished score actually beating raw); everything else is treated as
                # RAW — the safe side. The decision is written through the SAME resolve
                # path a human tap uses (audited as echo-auto-sort), and the portal
                # media tab remains a per-file OVERRIDE, not a chore. A "finished"
                # auto-sort resolution is ALSO quarantined the same way a direct
                # FINISHED verdict is (2026-09-01: this used to only touch the sort
                # queue and never the pool itself).
                if default_on:
                    lane = "finished" if (
                        _sc.edited_filename(a.get("title") or "")
                        or verdict.finished_score > verdict.raw_score) else "raw"
                    _q.enqueue(gym_id, a.get("id") or "", reasons=verdict.reasons,
                               verdict=verdict.verdict)
                    _lane, err = _q.resolve(gym_id, a.get("id") or "", lane,
                                            resolved_by="echo-auto-sort")
                    if err:
                        # could not persist the decision: fall back to the human queue
                        # rather than lose the file entirely.
                        enqueued += 1
                    else:
                        auto_sorted += 1
                        if lane == "finished" and _quarantine_finished(
                                store, a, verdict, gym_id, log, source="auto-sort"):
                            quarantined += 1
                    continue
                if _q.enqueue(gym_id, a.get("id") or "", reasons=verdict.reasons):
                    enqueued += 1
        except Exception as e:  # noqa: BLE001 - one bad file never sinks the sort
            log(f"classifier sort failed for {a.get('title')!r}: "
                f"{type(e).__name__}: {e}")
    if auto_sorted:
        log(f"auto-sorted {auto_sorted} ambiguous file(s) (echo-auto-sort; portal "
            "media tab can re-tag any of them)")
    if quarantined:
        log(f"classifier quarantined {quarantined} finished-content file(s) out of "
            f"the raw pool for {gym_id} (eligible=false, reject_reason="
            f"{_idx.REJECT_FINISHED_CONTENT!r}; portal media tab can restore any "
            "of them)")
    return enqueued


def _post_digest(text, channel=None, poster=None):
    """Best-effort one-liner to the gym's coach channel (or #ops when none)."""
    try:
        if poster is None:
            from ..slack_surface import SlackPoster
            poster = SlackPoster()
        if channel:
            poster._chat_post(text=text, blocks=None, channel=channel)  # noqa: SLF001
        else:
            poster.post_notice(text)
    except Exception as e:  # noqa: BLE001 - Slack must never sink the sync
        print(f"[gym-media] digest post skipped: {type(e).__name__}")


def _coach_channel(gym_id):
    """The gym's approval/coach Slack channel, or '' (falls back to #ops)."""
    try:
        from .. import accounts
        acct = (accounts.get_account(gym_id) or accounts.get_account(f"{gym_id}_ig")
                or accounts.get_account(f"{gym_id}_fb"))
        return getattr(acct, "slack_channel", "") or ""
    except Exception:
        return ""


def _client_channel_if_armed(gym_id):
    """_coach_channel(gym_id), but ONLY when this identity's client-reply flag is
    armed -- the SAME gate every other client-facing reply in this repo requires
    (config.slack_convo_client_reply_armed). Not armed -> '' -> the caller's
    _post_digest falls back to the internal #ops channel, exactly like "no channel
    configured" always has.

    GAP 3 (audit of PR #68): a Drive-revoked notice and the new-media/sort-queue
    digests used to post STRAIGHT to _coach_channel(gym_id) -- a client-facing Slack
    channel -- with no reference to conditions.compose(), the outbox, or the
    three-flag client_dm_support interlock at all. Reachable on
    AGENT_CLIENT_DM_AUTOFIX alone (the gym_drive_sync action calls sync_source,
    which calls this), and ALSO reachable with no client_dm_support flag set at all,
    since the nightly gym_drive_connect cron calls the exact same code path. No gym
    has slack_channel configured yet (audit, 2026-09-07), so every call has actually
    landed on #ops -- this closes the bypass before one does, rather than after.
    This does not make the notice pass through compose()'s grounded-reply gate (it
    is a fixed, factual, non-conditional string, not a claim assembled from a
    Reading), but it can no longer reach a client on any weaker condition than every
    other client-facing send in this system already requires.
    """
    try:
        from .. import config as _config
        if not _config.slack_convo_client_reply_armed("echo"):
            return ""
    except Exception:  # noqa: BLE001 - a config read failure fails closed (internal only)
        return ""
    return _coach_channel(gym_id)


def _flip_pending_for_missing(gym_id, asset_ids, log):
    """When an asset a PENDING calendar row is using disappears from Drive, pull that
    row off it (spec §4). Best effort: no creds -> no-op.

    Writes status='denied' with reject_reason, NOT 'needs_media': 'needs_media' is not
    in the content_calendar status CHECK constraint, so every one of these PATCHes was
    rejected 400 and `flipped` stayed 0 — a photo the client DELETED from their Drive
    stayed scheduled to publish, and only an exception (never a 4xx) was logged.
    'denied' is a real status and is the one the armed deny-backfill lane watches, so
    the day gets a fresh caption on a photo that still exists."""
    if not asset_ids:
        return 0
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        if _active_mutation.get() is not None:
            raise _mutation.MutationHold("gym_media_flip_effect_uncertain")
        return 0
    import requests  # lazy
    flipped = 0
    for aid in asset_ids:
        try:
            # A pending row referencing this drive asset id in its source_fragments.
            r = requests.patch(
                f"{url.rstrip('/')}/rest/v1/content_calendar",
                params={"gym_id": f"eq.{gym_id}", "status": "eq.pending",
                        "source_media_asset_id": f"eq.{aid}"},
                json={"status": "denied",
                      "reject_reason": _idx.REJECT_REMOVED,
                      "media_not_ready_reason": _idx.REJECT_REMOVED},
                headers={"apikey": key, "Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "Prefer": "return=minimal"},
                timeout=30)
            if r.status_code < 400:
                if _active_mutation.get() is not None:
                    readback = requests.get(
                        f"{url.rstrip('/')}/rest/v1/content_calendar",
                        params={"gym_id": f"eq.{gym_id}", "status": "eq.pending",
                                "source_media_asset_id": f"eq.{aid}", "select": "id"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"},
                        timeout=30)
                    remaining = readback.json() if readback.status_code < 400 else None
                    if not isinstance(remaining, list) or remaining:
                        raise _mutation.MutationHold("gym_media_flip_effect_uncertain")
                flipped += 1
            else:
                # A 4xx here used to be invisible: only exceptions were logged, so a
                # rejected flip looked exactly like "no row was using that asset".
                log(f"flip-pending REJECTED {r.status_code} for {aid}: "
                    f"{(r.text or '')[:200]}")
                if _active_mutation.get() is not None:
                    # Fenced: a rejected calendar effect is an uncertain effect.
                    # Hold the whole receipt; never certify the index mutation
                    # while a pending post still points at vanished media.
                    raise _mutation.MutationHold("gym_media_flip_effect_uncertain")
        except _mutation.MutationHold:
            raise
        except Exception as e:  # noqa: BLE001
            if _active_mutation.get() is not None:
                raise _mutation.MutationHold("gym_media_flip_effect_uncertain") from None
            log(f"flip-pending failed for {aid}: {type(e).__name__}: {e}")
    return flipped


def _index_effects(source, *, files, drive, store, probe_fn, log, now_iso,
                   probe_budget, render_budget, host_fn, sweep_missing):
    """All index/asset mutations for ONE verified source walk. Runs inside the
    inventory mutation receipt fence when armed (sync_source wraps this in
    _run_mutation), directly when the fence is OFF. Any exception propagates:
    under the fence it holds the whole receipt pending; unfenced it behaves
    exactly as it always has."""
    gym_id = source.get("gym_id")
    source_id = source.get("id")

    if _active_mutation.get() is not None:
        store = _EffectReadbackStore(store, gym_id, source_id)

    # A source that had been marked revoked but now reads fine is restored.
    if source.get("revoked_externally"):
        try:
            store.update_source(source_id, {"revoked_externally": False})
        except Exception:
            pass

    # 2. classify + dedupe
    rows, skipped = _idx.build_rows(files, source_id, gym_id, now_iso=now_iso, log=log)

    # 2b. RE-INGEST GUARD (Story Studio §0 / the EP124 lesson): a file whose
    # content_hash matches one of Echo's OWN past Story renders was saved back into
    # the client's Drive by the coach. It must NEVER be re-indexed as raw media (or
    # Echo would eat its own output and repost it). Drop those rows here, before
    # insert, and log the skip. Uses the shared render_ledger (Supabase, kv
    # fallback); a not-configured ledger returns False (no skip), so this is inert
    # until the first Story render is recorded.
    rows, reingest_skipped = _drop_reingested(rows, gym_id, log)
    skipped += [(t, "echo_render_reingest_skipped") for t in reingest_skipped]

    existing = {a["id"]: a for a in store.list_assets(gym_id, source_id=source_id)}
    # Drive may expose the same file through two bound folders. The asset PK is
    # global by Drive ID; the first source owns it. Never reassign it or let the
    # second source's disappearance sweep change its eligibility. A cross-tenant
    # collision is an error, not permission to read or mutate that tenant's row.
    owned_rows = []
    for row in rows:
        if row["id"] not in existing:
            owner = store.get_asset(row["id"])
            if owner:
                if owner.get("gym_id") != gym_id:
                    raise ValueError("Drive file is already indexed for another gym")
                log(f"shared Drive file {row['id']} already belongs to source "
                    f"{owner.get('source_id')}; skipping duplicate")
                continue
        owned_rows.append(row)
    rows = owned_rows
    seen_ids = {r["id"] for r in rows}

    # 3. insert new / patch changed indexer-owned fields
    candidates = [r for r in rows if r["id"] not in existing]
    inserted_ids = store.insert_assets_ignore_conflicts(candidates)
    # Another source can win between the ownership precheck and this insert.
    # Re-read every candidate before any probe/update/classification. Unique
    # files still insert even when one shared ID loses the race.
    skipped_ids = set()
    for row in candidates:
        owner = store.get_asset(row["id"])
        if not owner:
            raise RuntimeError("Drive asset insert could not be verified")
        if owner.get("gym_id") != gym_id:
            raise ValueError("Drive file is already indexed for another gym")
        if owner.get("source_id") != source_id:
            skipped_ids.add(row["id"])
    if skipped_ids:
        rows = [r for r in rows if r["id"] not in skipped_ids]
        seen_ids.difference_update(skipped_ids)
    new_rows = [r for r in candidates if r["id"] in inserted_ids]
    inserted = len(new_rows)
    updated = 0
    for r in rows:
        old = existing.get(r["id"])
        if old is None:
            continue
        changes = {f: r[f] for f in _OWNED_FIELDS if old.get(f) != r.get(f)}
        if old.get("reject_reason") == _idx.REJECT_REMOVED and r["id"] in seen_ids:
            # An asset that came back: recompute eligibility from what is known.
            changes["eligible"] = r.get("eligible")
            changes["reject_reason"] = r.get("reject_reason")
        if changes:
            changes["indexed_at"] = now_iso
            if old.get("content_hash") != r.get("content_hash"):
                # Drive IDs survive byte replacement. All inspection and
                # consent decisions for the old bytes must be invalidated in
                # the same conditional write as the new hash. A concurrent
                # operator review locks/checks the old hash and cannot revive
                # this asset after this patch.
                changes.update(
                    review_status="pending_review", reviewed_by=None,
                    reviewed_at=None, review_note=None,
                    review_content_hash=None,
                    moderation_status="pending", moderation_json=None,
                    people_detected=None, consent_status="pending",
                    consent_member_ref=None, release_ref=None,
                    consent_expires_at=None,
                    eligible=r.get("eligible"),
                    duration_sec=None, width=r.get("width"),
                    height=r.get("height"), aspect=r.get("aspect"),
                    vision_json=None, rendition_key=None, rendition_url=None)
                store.update_indexed_asset_if_hash(
                    gym_id, r["id"], old.get("content_hash"), changes)
            else:
                store.update_asset(r["id"], changes)
            updated += 1

    # 4. Nightly reconciliation only. The queued post-bind import must never
    # mutate existing assets or pending calendar rows because a temporarily
    # incomplete Drive walk could otherwise rewrite a pending post.
    removed = 0
    vanished = []
    if sweep_missing:
        for asset_id, old in existing.items():
            if asset_id in seen_ids:
                continue
            if old.get("reject_reason") == _idx.REJECT_REMOVED:
                continue  # already marked; idempotent
            store.update_asset(asset_id, {"eligible": False,
                                          "reject_reason": _idx.REJECT_REMOVED,
                                          "indexed_at": now_iso})
            vanished.append(asset_id)
            removed += 1
        _flip_pending_for_missing(gym_id, vanished, log)

    # 5. budgeted probe pass over unprobed VIDEO candidates
    probe_fn = probe_fn or _idx.probe_video
    budget = config.gym_drive_probe_max_per_run() if probe_budget is None \
        else int(probe_budget)
    merged = {a["id"]: dict(a) for a in existing.values()}
    for r in rows:
        merged.setdefault(r["id"], dict(r))
        merged[r["id"]]["id"] = r["id"]
    candidates = [a for a in merged.values()
                  if a["id"] in seen_ids
                  and a.get("kind") == _idx.KIND_VIDEO
                  and a.get("duration_sec") is None
                  and a.get("eligible") is not False]
    probed = newly_eligible = 0
    probed_ids = []
    reject_counts = Counter()
    # LIVE classifier signals (2026-09-01 hardening): the OCR / cut-density probes
    # ride the SAME downloaded bytes this loop already fetches for ffprobe — no
    # second download, no new budget. {asset_id: (has_burned_text, cut_density)}.
    # Gated on story_classifier_enabled (the classifier's own flag; this closes a
    # gap in an already-armed lane, not a new capability) AND on the classifier's
    # OCR reader actually being armed (agent/ocr_check reuses the existing Gemini
    # vision path, itself gated on AGENT_NANO_ENABLED) — a no-op, not a crash, when
    # that is off.
    ocr_signals = {}
    run_ocr = config.story_classifier_enabled()
    for asset in candidates[:budget]:
        tmp_dir = tempfile.mkdtemp(prefix="gymprobe_")
        tmp_path = Path(tmp_dir) / "probe.bin"
        try:
            drive.download(asset["id"], tmp_path)
            info = probe_fn(tmp_path)
            if run_ocr and info:
                try:
                    from .. import story_classifier as _sc
                    has_text = _sc.default_ocr_reader(str(tmp_path))
                    cuts = _sc.default_cut_probe(str(tmp_path))
                    if has_text is not None or cuts is not None:
                        ocr_signals[asset["id"]] = (has_text, cuts)
                except Exception as e:  # noqa: BLE001 - a probe failure never blocks
                    log(f"classifier probe failed for {asset.get('title')!r}: "
                        f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - one bad file never sinks the pass
            log(f"probe failed for {asset.get('title')!r}: {type(e).__name__}: {e}")
            info = None
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
                os.rmdir(tmp_dir)
            except OSError:
                pass
        if not info:
            continue  # stays unprobed -> stays unselectable (fail closed)
        el, reason, label = _idx.video_eligibility(
            asset.get("size_bytes"), info["duration_sec"], info["width"],
            info["height"])
        probe_fields = {
            "duration_sec": info["duration_sec"], "width": info["width"],
            "height": info["height"], "aspect": label,
            "eligible": el, "reject_reason": reason, "indexed_at": now_iso}
        store.update_asset(asset["id"], probe_fields)
        # reflect the probe back onto the local merged view so the classifier sort
        # (6b) sees real aspect + duration, not the pre-probe NULLs.
        asset.update(probe_fields)
        probed += 1
        probed_ids.append(asset["id"])
        if el:
            newly_eligible += 1
        elif reason:
            reject_counts[reason] += 1

    for r in new_rows:
        if r.get("eligible") is False and r.get("reject_reason"):
            reject_counts[r["reject_reason"]] += 1

    # 5b. budgeted PRE-RENDER pass (audit R-D1 #5): eligible videos with no
    # rendition_url are downloaded, probed for codec, and either transcoded to an
    # H.264 .mp4 (HEVC / odd container, counts against RENDITION_MAX_PER_SYNC) or,
    # when already web-playable, hosted as-is and persisted as their own rendition so
    # the month build never re-hosts them and this pass never re-downloads them.
    # Bounded: at most 2x the transcode budget in candidates per source per run;
    # converges across nights. Runs on the same synced-asset view as the probe pass.
    rendered, prehosted, render_skipped = _prerender_pass(
        gym_id, drive, store, merged, seen_ids, probe_fn, log,
        budget_n=(config.rendition_max_per_sync() if render_budget is None
                  else int(render_budget)),
        host_fn=host_fn)

    # 6b. STORY_CLASSIFIER sort (default ON, spec §0): tag freshly-seen assets raw /
    # finished / ambiguous. AMBIGUOUS queues for a human (or auto-sorts); a
    # CONFIDENT FINISHED verdict is quarantined out of the raw pool (see
    # _sort_ambiguous / _quarantine_finished — the 2026-09-01 fix). Uses the
    # post-probe view (merged) so a probed video classifies on real aspect/duration
    # AND the live OCR/cut signals gathered above. Only NEW rows are sorted (a
    # re-sync never re-queues a file a coach already resolved). Sorts + quarantines
    # only; posts/stages/composes nothing.
    to_sort = [merged.get(r["id"], r) for r in new_rows]
    queued_ambiguous = _sort_ambiguous(to_sort, gym_id, log, store=store,
                                       ocr_signals=ocr_signals)

    # READBACK VERIFICATION (fenced mode only): the receipt may COMPLETE only
    # after every effect re-reads clean from the store. Unfenced this is a
    # no-op and the legacy counters stand as they always have.
    if _active_mutation.get() is not None:
        _verify_index_readback(store, gym_id, rows, vanished, probed_ids)
        store.verify()

    photos = sum(1 for r in rows if r["kind"] == _idx.KIND_PHOTO)
    videos = sum(1 for r in rows if r["kind"] == _idx.KIND_VIDEO)
    return {
        "photos": photos, "videos": videos, "inserted": inserted,
        "updated": updated, "probed": probed, "newly_eligible": newly_eligible,
        "removed": removed, "skipped": len(skipped),
        "rejected": dict(reject_counts), "new_rows": len(new_rows),
        "queued_ambiguous": queued_ambiguous,
        "rendered": rendered, "prehosted": prehosted,
        "render_skipped": render_skipped}


def _prerender_pass(gym_id, drive, store, merged, seen_ids, probe_fn, log, *,
                    budget_n, host_fn=None):
    """Render (or pre-host) up to the budget's worth of eligible videos that carry no
    rendition_url yet. Returns (rendered, prehosted, skipped). Never raises: one bad
    file never sinks the sync; a spent budget or a timed-out clip ends the pass."""
    from .. import media_host as _mh
    host_fn = host_fn or _mh.host_media
    probe_fn = probe_fn or _idx.probe_video
    if budget_n <= 0:
        return 0, 0, 0
    cands = [a for a in merged.values()
             if a["id"] in seen_ids
             and a.get("kind") == _idx.KIND_VIDEO
             and a.get("eligible") is True
             and not a.get("rendition_url")]
    cands.sort(key=lambda a: (int(a.get("used_count") or 0), str(a.get("id"))))
    budget = _idx.RenditionBudget(budget_n)
    rendered = prehosted = skipped = 0
    for asset in cands[: budget_n * 2]:
        # STOP AT BUDGET SPENT (audit round 4 #5): no further download or probe once
        # the transcode budget is gone; the rest of the list waits for tomorrow.
        if budget.spent:
            break
        tmp_dir = tempfile.mkdtemp(prefix="gymrender_")
        tmp_path = Path(tmp_dir) / os.path.basename(asset.get("title") or "clip.bin")
        try:
            drive.download(asset["id"], tmp_path)
            info = probe_fn(tmp_path)
            if not info:
                skipped += 1
                continue
            if _idx.needs_rendition(asset, info):
                url, _conv = _idx.ensure_rendition(asset, tmp_path, store=store,
                                                   probe_info=info, budget=budget,
                                                   host_fn=host_fn)
                if url:
                    rendered += 1
                    log(f"pre-rendered {asset.get('title')!r} for {gym_id}")
                else:
                    skipped += 1        # converter unavailable: builder marks it
            else:
                url = host_fn(str(tmp_path), gym_id)
                if url:
                    key = _mh._key_from_public_url(url) or _idx.rendition_key(
                        gym_id, asset.get("content_hash"), _idx._ext(asset.get("title")))
                    _idx._persist_rendition(store, asset, key, url)
                    prehosted += 1
                else:
                    skipped += 1
        except _idx.RenditionBudgetExhausted:
            skipped += 1                    # waits for tomorrow's budget
        except _idx.RenditionTimeout as e:
            skipped += 1
            log(f"pre-render timed out for {asset.get('title')!r}: {e}")
        except Exception as e:  # noqa: BLE001 - one bad file never sinks the pass
            skipped += 1
            log(f"pre-render failed for {asset.get('title')!r}: {type(e).__name__}: {e}")
        finally:
            try:
                for name in os.listdir(tmp_dir):
                    os.unlink(os.path.join(tmp_dir, name))
                os.rmdir(tmp_dir)
            except OSError:
                pass
    if rendered or prehosted or skipped:
        log(f"{gym_id}: pre-render pass rendered {rendered}, pre-hosted {prehosted}, "
            f"skipped {skipped} (budget {budget_n})")
    return rendered, prehosted, skipped


def sync_source(source, *, drive=None, store=None, probe_fn=None, log=None,
                now_iso=None, probe_budget=None, render_budget=None, host_fn=None,
                sweep_missing=True, emit_digest=True, verified_keys=None):
    """Sync ONE media_source. Returns a per-source summary dict. Never raises out of
    a normal degrade path; a 403 on the walk marks the source revoked_externally and
    returns a revoked summary.

    Arming lanes (2026-10-05): the nightly run() applies the per-gym pilot
    allowlist before calling this. The authorized DIRECT lanes — the Fixer
    re-stage recipe (agent/fixer_ops.run_restage_month) and the client_dm_support
    gym_drive_sync action — carry their OWN arming (Fixer job authorization /
    AGENT_CLIENT_DM_AUTOFIX) and pass the persisted store row unchanged. This
    function's persisted-row re-read plus fail-closed identity guard is their
    safety boundary, so it deliberately does NOT re-check the pilot allowlist
    (re-checking it here would silently break the authorized manual recipe)."""
    log = log or (lambda m: print(f"[gym-media] {m}"))
    from ..integrations import drive_client as _dc
    drive = drive or _dc.DriveClient()
    store = store or _idx.default_store()
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()

    gym_id = source.get("gym_id")
    source_id = source.get("id")
    folder_id = source.get("folder_id")
    # DEFENSE IN DEPTH (independent-review P0, 2026-10-05): never trust the
    # caller-supplied source dict for tenant/folder/active ownership. Re-read
    # the CURRENT persisted row by source ID from the store BEFORE any Drive
    # walk or write: a caller that forged or rewrote gym_id / folder_id /
    # active in memory is refused here even when it would also pass the
    # identity guard below. Fail CLOSED: an unreadable/missing row, a field
    # that disagrees with the persisted row, or a persisted-inactive source all
    # refuse the pass. Test fakes implement the same narrow get_source-by-ID
    # contract (tests/gym_media_fakes.FakeMediaStore.get_source); a store with
    # no way to re-read the row refuses rather than failing open.
    current = None
    get_source = getattr(store, "get_source", None)
    if callable(get_source):
        try:
            current = get_source(source_id)
        except Exception as e:  # noqa: BLE001 - fail closed, never fail open
            log(f"source {source_id}: could not re-read the persisted source "
                f"row ({type(e).__name__}: {e}); refusing this pass")
            current = None
    if current is None:
        log(f"REFUSED source {source_id}: the persisted media_source row could "
            f"not be re-read by ID from the store (missing, unreadable, or the "
            f"store cannot re-read by ID); no walk, no insert, no rewrite — "
            f"the caller-supplied row is never trusted for ownership")
        return {"ok": False, "refused": "source_row_unreadable",
                "gym_id": gym_id}
    drift = [f for f in ("gym_id", "folder_id")
             if source.get(f) != current.get(f)]
    if drift:
        log(f"REFUSED source {source_id}: caller-supplied {', '.join(drift)} "
            f"disagrees with the persisted media_source row (stored gym_id "
            f"{current.get('gym_id')!r}, folder {current.get('folder_id')!r}); "
            f"a caller may not forge or rewrite ownership — no walk, no "
            f"insert, no rewrite")
        return {"ok": False, "refused": "source_row_mismatch",
                "gym_id": current.get("gym_id"), "fields": drift}
    if current.get("active") is False:
        log(f"REFUSED source {source_id}: the persisted media_source row is "
            f"inactive (disconnected); a caller may not resurrect it with an "
            f"in-memory active=True copy — no walk, no insert")
        return {"ok": False, "refused": "source_inactive",
                "gym_id": current.get("gym_id")}
    # The persisted row is authoritative from here on.
    source = current
    gym_id = source.get("gym_id")
    folder_id = source.get("folder_id")
    # STALE-KEY GUARD (audit round 5 MAJOR 2 + the 2026-10-05 tenant-source binding
    # guard): a direct caller (the Tough Temple re-stage recipe: media_source under
    # toughtemple086f51, media_asset under toughtemple52040e) used to get the raw row,
    # list ZERO existing assets under the stale key, and re-insert every file. The
    # identity check is still applied HERE (not only in run()) so every entry point
    # hits the same rule — but the rule is now REFUSAL, not silent in-memory
    # re-keying: a stored gym_id that does not resolve to the registered tenant
    # fails closed before any walk/insert. The check uses _tenant_identity (NOT the
    # request-path wrapper gym_media_routes._resolve_stale_fingerprint, which swallows
    # resolver failures and returns the key unchanged): the sync must fail closed when
    # identity cannot be ESTABLISHED at all, not only when the established identity
    # DIFFERS from the stored key.
    stored_gym_id = gym_id
    # Identity verification: a TRUSTED per-run batch result (verified_keys,
    # produced once per run() by _resolve_verified_keys) is used as-is — the
    # fresh complete plane read already happened this run. A DIRECT caller (no
    # verified_keys) gets its own fresh complete verification via
    # _tenant_identity every call: no caching, no stale cross-call trust.
    if verified_keys is None:
        established, resolved = _tenant_identity(gym_id)
    else:
        established, resolved = _identity_from_verified(verified_keys, gym_id)
    if not established:
        # IDENTITY UNESTABLISHED (2026-10-05, independent-review P2): the identity
        # plane was unreadable/truncated, or the stored key is not a uniquely
        # registered tenant. Treating the row as legitimate here is exactly the
        # hole the binding guard exists to close, so the source is refused for
        # this pass — never walked, never inserted, row never rewritten. The next
        # scheduled run re-checks, so a transient plane outage only skips passes,
        # never data. Normal legitimate sources (key proven live by a complete
        # read) are unaffected.
        log(f"REFUSED source {source_id}: tenant identity for stored gym_id "
            f"{stored_gym_id!r} could not be established (identity plane "
            f"unreadable or key not a uniquely-registered tenant); no assets "
            f"indexed and the source row was NOT rewritten (retry on the next "
            f"scheduled pass; if the key is genuinely stale, reconcile it by an "
            f"evidence-reviewed source rebind or the controlled migration in "
            f"migrations/DRAFT_media_asset_source_gym_guard_20261005.sql — the "
            f"PR #262 immutable-gym guard forbids a plain UPDATE of "
            f"media_source.gym_id, and a silent rewrite is never allowed)")
        return {"ok": False, "refused": "tenant_identity_unestablished",
                "gym_id": stored_gym_id}
    if resolved != stored_gym_id:
        # TENANT-SOURCE BINDING GUARD (2026-10-05): a source row whose stored
        # gym_id does NOT resolve to the currently-registered tenant is refused
        # for this pass — never walked, never inserted. Silently syncing it under
        # the resolved tenant is exactly how production ended up with media_asset
        # rows whose gym_id differs from their linked media_source.gym_id (95 rows
        # at last count): those rows must not be silently used OR multiplied.
        # The media_source row itself is NEVER rewritten here (ownership fixes
        # are an evidence-reviewed rebind / controlled migration — the PR #262
        # immutable-gym guard forbids a plain UPDATE); the alert this log fires
        # is the operator's cue.
        log(f"REFUSED source {source_id}: stored gym_id {stored_gym_id!r} does not "
            f"match resolved tenant {resolved!r}; no assets indexed and the source "
            f"row was NOT rewritten (reconcile by an evidence-reviewed source "
            f"rebind or the controlled migration in "
            f"migrations/DRAFT_media_asset_source_gym_guard_20261005.sql — the "
            f"PR #262 immutable-gym guard forbids a plain UPDATE of "
            f"media_source.gym_id, and a silent rewrite is never allowed — then "
            f"request a re-sync)")
        return {"ok": False, "refused": "source_gym_mismatch",
                "gym_id": stored_gym_id, "resolved_gym_id": resolved}

    # 1. walk (403 -> revoked_externally + notify, no crash)
    try:
        files = drive.walk(folder_id, max_depth=config.gym_drive_sync_max_depth(),
                           use_cache=False)
    except Exception as e:  # noqa: BLE001
        status = _dc._http_status(e)  # noqa: SLF001 - shared status classifier
        if status in (403, 404):
            revoke_hold = None
            try:
                # Fenced when armed: the revoked-mark is a source-row effect and
                # settles under its own receipt; unfenced it runs directly as it
                # always has. A hold still notifies + reports revoked (the walk
                # 403s again next pass); the row is never certified from an
                # uncertain write.
                _run_mutation(gym_id, "gym_media_source_revoke",
                              {"gym_id": gym_id, "source_id": source_id,
                               "drive_status": status},
                              lambda conn: _update_source_verified(
                                  store, gym_id, source_id,
                                  {"revoked_externally": True}))
            except _mutation.MutationHold as hold:
                revoke_hold = str(hold)
                log(f"source {source_id}: revoked-mark write HELD for "
                    f"reconciliation ({hold}); notice still fires and the next "
                    f"pass re-checks")
            except Exception:
                pass
            msg = (f"Google Drive access for {gym_id} was revoked (the shared "
                   f"folder is no longer shared to Echo). Reconnect it in the "
                   f"portal to resume pulling photos. Nothing was lost.")
            if emit_digest:
                _post_digest(msg, channel=_client_channel_if_armed(gym_id))
            log(f"source {source_id} for {gym_id} revoked_externally (Drive {status})")
            result = {"ok": False, "revoked": True, "gym_id": gym_id}
            if revoke_hold is not None:
                result.update(held=True, hold_reason=revoke_hold, source_id=source_id)
            return result
        log(f"walk failed for {gym_id}: {type(e).__name__}: {e}")
        return {"ok": False, "error": type(e).__name__, "gym_id": gym_id}

    # Steps 2-6b (classify/dedupe, insert/patch, removed-sweep + pending flip,
    # probe, pre-render, classifier sort) are ALL the index/asset mutations of
    # this pass. They run inside _index_effects, fenced by ONE inventory
    # mutation receipt when AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED=true: any
    # uncertain listing/read, write, duplicate/collision, or readback holds the
    # exact mutation pending and this source returns a held summary -- no
    # digest, no COMPLETE. Fence OFF (default): identical legacy behavior.
    try:
        effects = _run_mutation(
            gym_id, "gym_media_sync",
            {"gym_id": gym_id, "source_id": source_id, "folder_id": folder_id,
             "now_iso": now_iso,
             "walked": sorted(str(getattr(f, "id", "")) for f in files)},
            lambda conn: _index_effects(
                source, files=files, drive=drive, store=store,
                probe_fn=probe_fn, log=log, now_iso=now_iso,
                probe_budget=probe_budget, render_budget=render_budget,
                host_fn=host_fn, sweep_missing=sweep_missing))
    except _mutation.MutationHold as hold:
        log(f"source {source_id} for {gym_id}: index mutation HELD for "
            f"reconciliation ({hold}); no digest, no complete -- the exact "
            f"mutation stays pending until an operator settles it")
        return {"ok": False, "held": True, "gym_id": gym_id,
                "source_id": source_id, "hold_reason": str(hold)}

    summary = {"ok": True, "gym_id": gym_id, "source_id": source_id, **effects}
    inserted = effects["inserted"]
    photos = effects["photos"]
    videos = effects["videos"]
    newly_eligible = effects["newly_eligible"]
    reject_counts = effects["rejected"]
    # 7. per-gym new-asset digest (only when something new arrived)
    if emit_digest and inserted:
        rejected_txt = ", ".join(f"{k} x{v}" for k, v in sorted(reject_counts.items())) \
            or "none"
        _post_digest(
            f"New team media synced for {gym_id}: {inserted} new "
            f"({photos} photos, {videos} videos this scan), {newly_eligible} newly "
            f"ready to use, rejected: {rejected_txt}. Review or hide any in the "
            f"portal media tab.",
            channel=_client_channel_if_armed(gym_id))
    # "Sort these" coach digest (spec §0.3): fires ONLY when the queue is non-empty.
    # story_sort_queue.post_digest is a no-op on an empty queue, so this never
    # storms the channel. Best effort: a digest failure never sinks the sync.
    if emit_digest and config.story_classifier_enabled():
        try:
            from .. import story_sort_queue as _q
            _q.post_digest(gym_id, channel=_client_channel_if_armed(gym_id))
        except Exception as e:  # noqa: BLE001
            log(f"sort-queue digest skipped for {gym_id}: {type(e).__name__}: {e}")
    return summary


def run(drive=None, store=None, probe_fn=None, log=None, now_iso=None,
        sleep=None, probe_budget=None, render_budget=None):
    """One sync pass over every active gym-drive source the lane is armed for.
    Returns a roll-up summary; never raises out of a normal degrade path."""
    log = log or (lambda m: print(f"[gym-media] {m}"))
    sleep = sleep if sleep is not None else time.sleep

    from ..integrations import drive_client as _dc
    drive = drive or _dc.DriveClient()
    if not drive.available():
        log("skipped: no GOOGLE_DRIVE_SA_JSON (lane unarmed; nothing synced)")
        return {"ok": False, "reason": "drive unavailable (no service-account key)"}
    store = store or _idx.default_store()
    if not store.available():
        log("skipped: no Supabase creds for media tables (nothing synced)")
        return {"ok": False, "reason": "media store unavailable"}

    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    try:
        raw_sources = [s for s in store.list_sources()
                       if s.get("kind", "gym_drive") == "gym_drive"]
    except Exception as e:  # noqa: BLE001
        log(f"could not list sources: {type(e).__name__}: {e}")
        return {"ok": False, "reason": f"source list failed: {type(e).__name__}"}

    # TENANT-SOURCE BINDING GUARD (2026-10-05, supersedes the 2026-08-31 in-memory
    # remap): a source can land with a STALE account-key fingerprint (a portal
    # connect-link self-decodes its OWN key from its signed payload, so it keeps
    # working under whatever key it was minted with even after the gym is later
    # re-canonicalized). The remap is gone because it silently produced
    # media_asset rows under a gym_id that differs from their source's
    # media_source.gym_id (95 such rows in production at last count — they must
    # not be silently used or multiplied). Now: resolve for the ARMING check only,
    # pass the row through with its STORED gym_id, and let sync_source refuse it
    # (fail closed, no walk, no insert). The media_source row itself is never
    # rewritten here — reconciliation is an evidence-reviewed source rebind or
    # the controlled migration in
    # migrations/DRAFT_media_asset_source_gym_guard_20261005.sql (the PR #262
    # immutable-gym guard forbids a plain UPDATE); the refusal log is the
    # operator's cue to reconcile the row.
    #
    # ONE complete fresh identity-plane read for the WHOLE run (independent-
    # review 2026-10-05): resolve_known_source_keys is a full-fleet snapshot, so
    # resolving per source would multiply that fleet-wide read by the fleet
    # size. The SAME trusted per-run result drives BOTH the pilot-allowlist
    # arming check below AND sync_source's fail-closed guard (independent-review
    # P1): the request-path wrapper gym_media_routes._resolve_stale_fingerprint
    # is NEVER used here — it swallows resolver failures and returns the key
    # unchanged, which would let the allowlist arm a gym the guard then refuses
    # (or vice versa). A gym whose identity the batch could not prove stays
    # UNARMED this run: no walk, no insert. A failure returns {} and every
    # source is skipped fail-closed (one skipped pass, never a write on an
    # unproven identity). Direct sync_source callers (no verified_keys) still
    # get their own fresh complete verification.
    verified_keys = _resolve_verified_keys(
        [s.get("gym_id") for s in raw_sources if s.get("gym_id")], log=log)
    sources = []
    for s in raw_sources:
        gym_id = s.get("gym_id")
        established, resolved = _identity_from_verified(verified_keys, gym_id)
        if not established:
            # UNPROVEN identity -> UNARMED this run (the allowlist and the
            # guard read the SAME fresh batch, so they can never disagree; an
            # unarmed gym stays unarmed).
            log(f"source {s.get('id')}: tenant identity for stored gym_id "
                f"{gym_id!r} could not be established this run; the source "
                f"stays UNARMED (no walk, no insert — fail closed, retry on "
                f"the next scheduled pass)")
            continue
        if resolved != gym_id:
            # NOT remapped in memory anymore: sync_source refuses sources whose
            # stored gym_id does not resolve to the registered tenant (the
            # 2026-10-05 tenant-source binding guard). The arming flag is still
            # checked against the RESOLVED tenant so a stale-keyed source does
            # not silently bypass the per-gym pilot allowlist either way.
            log(f"source {s.get('id')} carries stale key {gym_id!r} (resolves to "
                f"{resolved!r}); it will be REFUSED by the sync, not silently "
                f"re-keyed (the media_source row itself is never rewritten here)")
        if config.gym_drive_connect_active_for(resolved):
            sources.append(s)

    results = []
    for i, source in enumerate(sources):
        if i:
            sleep(_STAGGER_SEC)   # stagger 30s so a cold run does not spike Drive
        try:
            results.append(sync_source(
                source, drive=drive, store=store, probe_fn=probe_fn, log=log,
                now_iso=now_iso, probe_budget=probe_budget,
                render_budget=render_budget, verified_keys=verified_keys))
        except Exception as e:  # noqa: BLE001 - one source never sinks the run
            log(f"source {source.get('id')} failed: {type(e).__name__}: {e}")
            results.append({"ok": False, "error": type(e).__name__,
                            "gym_id": source.get("gym_id")})

    # deny sweep (best effort, isolated)
    rolled_back = 0
    try:
        from .. import gym_media_selector as _sel
        rolled_back = _sel.observe_denials(store=store).get("rolled_back", 0)
    except Exception as e:  # noqa: BLE001
        log(f"deny sweep skipped: {type(e).__name__}: {e}")

    ok = sum(1 for r in results if r.get("ok"))
    inserted = sum(r.get("inserted", 0) for r in results)
    summary = {"ok": True, "sources": len(sources), "synced_ok": ok,
               "inserted": inserted, "rolled_back": rolled_back,
               "results": results}
    log(f"gym-media sync: {len(sources)} source(s), {ok} ok, {inserted} new "
        f"asset(s), {rolled_back} denied asset(s) returned to pool")
    return summary
