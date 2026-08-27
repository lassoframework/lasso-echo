"""
index_podcast_library.py — the nightly podcast library indexer
(PODCAST_LIBRARY_BUILD_SPEC.md Wave 2.4). Runs in the daily draw behind
PODCAST_LIBRARY_INDEX (default ON per the spec; the lane is INERT without a
GOOGLE_DRIVE_SA_JSON key, so ON is safe) and on demand.

What one run does, idempotently (a re-run with nothing changed writes nothing):
  1. Fresh walk of the Podcast Episodes Drive root (never the 6h cache — the
     index must see tonight's tree).
  2. Classify every file (podcast_index.build_rows); unclassifiable names are
     logged and skipped, never raised.
  3. NEW assets are inserted; EXISTING assets get a targeted PATCH only when an
     indexer-owned field changed — probe data, used_count and last_used_at are
     NEVER touched by the indexer.
  4. Assets whose Drive id has VANISHED are marked postable=false,
     reject_reason='removed_from_drive'.
  5. Probe pass: up to PODCAST_PROBE_MAX_PER_RUN (default 20) unprobed
     clip/audiogram candidates are downloaded to a temp path, ffprobed
     (duration/width/height -> aspect), the §2.3 gate is computed and written
     back. Temp files are deleted win or lose. Unprobed stays unselectable.
  6. Deny sweep: podcast_selector.observe_denials() returns denied clips to the
     pool (usage stamps rolled back) — the same observe-the-denials shape the
     deny-backfill jobs use.
  7. ONE summary line to #ops (episodes seen, clips found, newly postable,
     rejected with reasons) — printed always, posted best-effort.

Degrades cleanly: no SA key -> one log line and a no-op; no Supabase creds ->
one log line and a no-op. Nothing here stages, publishes, or writes calendar
rows. NOTHING here logs a secret.
"""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .. import config, podcast_index as _idx

_PROBE_BUDGET_ENV = "PODCAST_PROBE_MAX_PER_RUN"
_PROBE_BUDGET_DEFAULT = 20

# The indexer-owned columns compared for the changed-row PATCH. Probe columns
# (duration/width/height/aspect) and selector columns (used_count/last_used_at)
# are deliberately absent: a re-index may never clobber them.
_OWNED_FIELDS = ("episode", "kind", "clip_index", "title", "size_bytes",
                 "notes_doc_id")


def _probe_budget():
    try:
        return max(0, int(os.environ.get(_PROBE_BUDGET_ENV, _PROBE_BUDGET_DEFAULT)))
    except (TypeError, ValueError):
        return _PROBE_BUDGET_DEFAULT


def _post_summary(text, poster=None):
    """Best-effort one-liner to the ops Slack channel; a Slack failure is only
    ever logged (the index result already printed)."""
    try:
        if poster is None:
            from ..slack_surface import SlackPoster
            poster = SlackPoster()
        poster.post_notice(text)
    except Exception as e:  # noqa: BLE001 - Slack must never sink the indexer
        print(f"[podcast-index] summary post skipped: {type(e).__name__}")


def run(drive=None, store=None, probe_fn=None, log=None, now_iso=None,
        poster=None, probe_budget=None):
    """One index pass. Returns a summary dict; never raises out of a normal
    degrade path (missing creds are reported, not thrown)."""
    log = log or (lambda m: print(f"[podcast-index] {m}"))

    from ..integrations import drive_client as _dc
    drive = drive or _dc.DriveClient()
    if not drive.available():
        log("skipped: no GOOGLE_DRIVE_SA_JSON (lane unarmed; nothing indexed)")
        return {"ok": False, "reason": "drive unavailable (no service-account key)"}

    store = store or _idx.default_store()
    if not store.available():
        log("skipped: no Supabase creds for podcast_asset (nothing indexed)")
        return {"ok": False, "reason": "podcast_asset store unavailable"}

    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    root = config.podcast_library_folder_id()

    # 1-2. fresh walk + classify
    files = drive.walk(root, max_depth=3, use_cache=False)
    rows, skipped = _idx.build_rows(files, now_iso=now_iso, log=log)

    existing = {a["id"]: a for a in store.list_assets()}
    seen_ids = {r["id"] for r in rows}

    # 3. insert new / patch changed
    new_rows = [r for r in rows if r["id"] not in existing]
    inserted = store.insert_assets(new_rows)
    updated = 0
    for r in rows:
        old = existing.get(r["id"])
        if old is None:
            continue
        changes = {f: r[f] for f in _OWNED_FIELDS if old.get(f) != r.get(f)}
        # An asset previously marked removed_from_drive that is back in the walk
        # is restored: recompute the gate from what is known (probe data kept).
        if old.get("reject_reason") == _idx.REJECT_REMOVED:
            postable, reject = _idx.postability(
                r["kind"], r["size_bytes"], old.get("duration_sec"), old.get("aspect"))
            changes.update({"postable": postable, "reject_reason": reject})
        if changes:
            changes["indexed_at"] = now_iso
            store.update_asset(r["id"], changes)
            updated += 1

    # 4. vanished Drive ids -> postable=false, removed_from_drive
    removed = 0
    for asset_id, old in existing.items():
        if asset_id in seen_ids:
            continue
        if old.get("reject_reason") == _idx.REJECT_REMOVED:
            continue  # already marked; idempotent
        store.update_asset(asset_id, {"postable": False,
                                      "reject_reason": _idx.REJECT_REMOVED,
                                      "indexed_at": now_iso})
        removed += 1

    # 5. probe pass (budgeted; converges across nights, never a 10 GB one-shot)
    probe_fn = probe_fn or _idx.probe_video
    budget = _probe_budget() if probe_budget is None else int(probe_budget)
    merged = {a["id"]: dict(a) for a in existing.values()}
    for r in rows:
        merged.setdefault(r["id"], {}).update(
            {k: r[k] for k in ("episode", "kind", "clip_index", "title",
                               "size_bytes")})
        merged[r["id"]].setdefault("duration_sec", None)
        merged[r["id"]].setdefault("postable", r["postable"])
        merged[r["id"]]["id"] = r["id"]
    candidates = [a for a in merged.values()
                  if a["id"] in seen_ids
                  and a.get("kind") in _idx.POSTABLE_KINDS
                  and a.get("duration_sec") is None
                  and a.get("postable") is not False]
    probed = newly_postable = 0
    reject_counts = Counter()
    for asset in candidates[:budget]:
        tmp_dir = tempfile.mkdtemp(prefix="podprobe_")
        tmp_path = Path(tmp_dir) / "probe.mp4"
        try:
            drive.download(asset["id"], tmp_path)
            info = probe_fn(tmp_path)
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
        aspect = _idx.aspect_of(info["width"], info["height"])
        postable, reject = _idx.postability(
            asset["kind"], asset.get("size_bytes"), info["duration_sec"], aspect)
        store.update_asset(asset["id"], {
            "duration_sec": info["duration_sec"], "width": info["width"],
            "height": info["height"], "aspect": aspect,
            "postable": postable, "reject_reason": reject,
            "indexed_at": now_iso,
        })
        probed += 1
        if postable:
            newly_postable += 1
        elif reject:
            reject_counts[reject] += 1

    # index-time rejects (size / kind) from tonight's NEW rows
    for r in new_rows:
        if r["postable"] is False and r["reject_reason"]:
            reject_counts[r["reject_reason"]] += 1

    # 6. deny sweep: denied clips return to the pool (best effort, isolated)
    rolled_back = 0
    try:
        from .. import podcast_selector as _sel
        rolled_back = _sel.observe_denials(store=store).get("rolled_back", 0)
    except Exception as e:  # noqa: BLE001
        log(f"deny sweep skipped: {type(e).__name__}: {e}")

    episodes = {r["episode"] for r in rows}
    clips = [r for r in rows if r["kind"] == _idx.KIND_CLIP]
    audiograms = [r for r in rows if r["kind"] == _idx.KIND_AUDIOGRAM]
    rejected_txt = ", ".join(f"{k} x{v}" for k, v in sorted(reject_counts.items())) or "none"
    summary = (
        f"podcast index: {len(episodes)} episodes seen, {len(clips)} clips + "
        f"{len(audiograms)} audiograms found, {inserted} new, {updated} updated, "
        f"{probed} probed, {newly_postable} newly postable, rejected: {rejected_txt}, "
        f"{removed} removed_from_drive, {len(skipped)} unclassifiable skipped, "
        f"{rolled_back} denied clip(s) returned to pool")
    log(summary)
    _post_summary(summary, poster=poster)  # spec §2.4: one line to #ops
    return {"ok": True, "summary": summary, "episodes": len(episodes),
            "clips": len(clips), "audiograms": len(audiograms),
            "inserted": inserted, "updated": updated, "probed": probed,
            "newly_postable": newly_postable,
            "rejected": dict(reject_counts), "removed": removed,
            "skipped": len(skipped), "rolled_back": rolled_back}
