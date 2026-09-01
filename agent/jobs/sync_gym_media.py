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

Degrades cleanly: no SA key / no Supabase creds -> one log line, no-op. Nothing
here stages, publishes, or writes calendar rows (beyond flipping a pending row
whose media vanished). NOTHING here logs a secret.
"""
from __future__ import annotations

import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .. import config, gym_media_index as _idx

# The indexer-owned columns compared for the changed-row PATCH. Probe columns
# (duration/width/height/aspect/crop_hint), vision_json, rendition_*, and selector
# columns (used_count/last_used_at) are deliberately absent: a re-sync may never
# clobber them.
_OWNED_FIELDS = ("kind", "title", "mime_type", "size_bytes", "content_hash",
                 "drive_modified", "source_id")

_STAGGER_SEC = 30.0


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
                flipped += 1
            else:
                # A 4xx here used to be invisible: only exceptions were logged, so a
                # rejected flip looked exactly like "no row was using that asset".
                log(f"flip-pending REJECTED {r.status_code} for {aid}: "
                    f"{(r.text or '')[:200]}")
        except Exception as e:  # noqa: BLE001
            log(f"flip-pending failed for {aid}: {type(e).__name__}: {e}")
    return flipped


def sync_source(source, *, drive=None, store=None, probe_fn=None, log=None,
                now_iso=None, probe_budget=None):
    """Sync ONE media_source. Returns a per-source summary dict. Never raises out of
    a normal degrade path; a 403 on the walk marks the source revoked_externally and
    returns a revoked summary."""
    log = log or (lambda m: print(f"[gym-media] {m}"))
    from ..integrations import drive_client as _dc
    drive = drive or _dc.DriveClient()
    store = store or _idx.default_store()
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()

    gym_id = source.get("gym_id")
    source_id = source.get("id")
    folder_id = source.get("folder_id")

    # 1. walk (403 -> revoked_externally + notify, no crash)
    try:
        files = drive.walk(folder_id, max_depth=config.gym_drive_sync_max_depth(),
                           use_cache=False)
    except Exception as e:  # noqa: BLE001
        status = _dc._http_status(e)  # noqa: SLF001 - shared status classifier
        if status in (403, 404):
            try:
                store.update_source(source_id, {"revoked_externally": True})
            except Exception:
                pass
            msg = (f"Google Drive access for {gym_id} was revoked (the shared "
                   f"folder is no longer shared to Echo). Reconnect it in the "
                   f"portal to resume pulling photos. Nothing was lost.")
            _post_digest(msg, channel=_coach_channel(gym_id))
            log(f"source {source_id} for {gym_id} revoked_externally (Drive {status})")
            return {"ok": False, "revoked": True, "gym_id": gym_id}
        log(f"walk failed for {gym_id}: {type(e).__name__}: {e}")
        return {"ok": False, "error": type(e).__name__, "gym_id": gym_id}

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
    seen_ids = {r["id"] for r in rows}

    # 3. insert new / patch changed indexer-owned fields
    new_rows = [r for r in rows if r["id"] not in existing]
    inserted = store.insert_assets(new_rows)
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
            store.update_asset(r["id"], changes)
            updated += 1

    # 4. vanished Drive ids -> not eligible, removed_from_drive; flip pending rows
    removed = 0
    vanished = []
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
        if el:
            newly_eligible += 1
        elif reason:
            reject_counts[reason] += 1

    for r in new_rows:
        if r.get("eligible") is False and r.get("reject_reason"):
            reject_counts[r["reject_reason"]] += 1

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

    photos = sum(1 for r in rows if r["kind"] == _idx.KIND_PHOTO)
    videos = sum(1 for r in rows if r["kind"] == _idx.KIND_VIDEO)
    summary = {
        "ok": True, "gym_id": gym_id, "source_id": source_id,
        "photos": photos, "videos": videos, "inserted": inserted,
        "updated": updated, "probed": probed, "newly_eligible": newly_eligible,
        "removed": removed, "skipped": len(skipped),
        "rejected": dict(reject_counts), "new_rows": len(new_rows),
        "queued_ambiguous": queued_ambiguous}
    # 7. per-gym new-asset digest (only when something new arrived)
    if inserted:
        rejected_txt = ", ".join(f"{k} x{v}" for k, v in sorted(reject_counts.items())) \
            or "none"
        _post_digest(
            f"New team media synced for {gym_id}: {inserted} new "
            f"({photos} photos, {videos} videos this scan), {newly_eligible} newly "
            f"ready to use, rejected: {rejected_txt}. Review or hide any in the "
            f"portal media tab.",
            channel=_coach_channel(gym_id))
    # "Sort these" coach digest (spec §0.3): fires ONLY when the queue is non-empty.
    # story_sort_queue.post_digest is a no-op on an empty queue, so this never
    # storms the channel. Best effort: a digest failure never sinks the sync.
    if config.story_classifier_enabled():
        try:
            from .. import story_sort_queue as _q
            _q.post_digest(gym_id, channel=_coach_channel(gym_id))
        except Exception as e:  # noqa: BLE001
            log(f"sort-queue digest skipped for {gym_id}: {type(e).__name__}: {e}")
    return summary


def run(drive=None, store=None, probe_fn=None, log=None, now_iso=None,
        sleep=None, probe_budget=None):
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

    # DEFENSIVE READ-SIDE RESOLUTION (the CrossFit Reverb class, live 2026-08-31): a
    # source can land with a STALE account-key fingerprint (a portal connect-link
    # self-decodes its OWN key from its signed payload, so it keeps working under
    # whatever key it was minted with even after the gym is later re-canonicalized).
    # A stray source's gym_id is remapped to the currently-registered gym IN MEMORY
    # ONLY for this sync pass -- the media_source row itself is never rewritten here
    # (that is a by-hand fix or a fresh bind), so the alert fired by
    # _resolve_stale_fingerprint is the operator's cue to actually fix the row.
    # See gym_media_routes._resolve_stale_fingerprint for the resolution rule.
    from .. import gym_media_routes as _gm_routes
    sources = []
    for s in raw_sources:
        gym_id = s.get("gym_id")
        resolved = _gm_routes._resolve_stale_fingerprint(gym_id)
        if resolved != gym_id:
            log(f"source {s.get('id')} carries stale key {gym_id!r}; resolved to "
                f"{resolved!r} for this sync (the media_source row itself was NOT "
                f"rewritten)")
            s = dict(s)
            s["gym_id"] = resolved
        if config.gym_drive_connect_active_for(s.get("gym_id")):
            sources.append(s)

    results = []
    for i, source in enumerate(sources):
        if i:
            sleep(_STAGGER_SEC)   # stagger 30s so a cold run does not spike Drive
        try:
            results.append(sync_source(
                source, drive=drive, store=store, probe_fn=probe_fn, log=log,
                now_iso=now_iso, probe_budget=probe_budget))
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
