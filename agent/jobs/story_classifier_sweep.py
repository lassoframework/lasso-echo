"""
story_classifier_sweep.py — fleet-wide ONE-OFF reclassification of the EXISTING
eligible video pool with the hardened story_classifier (Blake, 2026-09-01: tonight's
Story Studio proof run found 3 already-finished, captioned clips sitting eligible
in Pierce Fitness's raw pool; "same class as page 4, same treatment: the claim
stays, make it true. Fix this for real, fleet-wide.").

WHY THIS EXISTS SEPARATELY from the nightly sync_gym_media classifier pass: that
pass only classifies NEWLY-inserted rows (6b: `to_sort = ... for r in new_rows`).
Every asset indexed BEFORE tonight's hardening landed with eligible=true and was
NEVER reclassified — the nightly pass will never touch it again on its own. This
sweep runs the SAME hardened classifier (real OCR via story_classifier.
default_ocr_reader, real cut-density via default_cut_probe) against every gym's
EXISTING eligible video pool, once, and reports EXACT per-gym counts.

WHAT IT DOES, per gym:
  1. Lists eligible=true, kind=video, excluded_by_coach=false assets not already
     quarantined by this sweep or the re-ingest guard (reject_reason not already
     REJECT_FINISHED_CONTENT / REJECT_CONSENT_REVIEW — idempotent re-runs).
  2. Downloads each (agent/integrations/drive_client.download — the SAME download
     the nightly sync and the story lane use, read-only), runs gather_signals with
     REAL local_path + default_ocr_reader + default_cut_probe, classify().
  3. A confident FINISHED verdict QUARANTINES the asset the SAME way any other
     ineligible asset is quarantined: eligible=False + reject_reason=
     REJECT_FINISHED_CONTENT (agent/gym_media_index.py). NEVER a delete, NEVER a
     new gate — story_candidates._eligible_raw / gym_media_selector.pick_media
     both already fail closed on eligible is not True.
  4. RAW and AMBIGUOUS verdicts are left exactly as they are (AMBIGUOUS still
     defers to the existing "Sort these" / echo-auto-sort machinery on the NEXT
     nightly sync of that asset's row-change path; this sweep does not invent a
     new ambiguous-handling path).

Dry-run by default; --apply makes the writes. Prints a per-gym before/after table
(Blake asked for EXACT sweep counts per gym — this is that table). Read/write
goes through the SAME media_source_store the nightly sync uses (gym+id scoped).

Usage:
  python -m agent.jobs.story_classifier_sweep [--apply] [--limit N] [gym ...]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .. import config, gym_media_index as _idx, story_classifier as _sc

_ALREADY_QUARANTINED = {_idx.REJECT_FINISHED_CONTENT, _idx.REJECT_CONSENT_REVIEW}


def _log(msg):
    print(f"[classifier-sweep] {msg}")


def _candidates(store, gym_id):
    """Eligible, not-coach-hidden, not-already-quarantined VIDEO assets for one
    gym — the exact pool story_candidates._eligible_raw draws its raw pool from."""
    out = []
    for a in store.list_assets(gym_id):
        if a.get("kind") != _idx.KIND_VIDEO:
            continue
        if a.get("eligible") is not True:
            continue
        if a.get("excluded_by_coach"):
            continue
        if (a.get("reject_reason") or "") in _ALREADY_QUARANTINED:
            continue
        out.append(a)
    return out


def sweep_gym(base, store, drive, *, apply=False, limit=None):
    result = {"gym": base, "checked": 0, "quarantined": 0, "raw": 0,
              "ambiguous": 0, "errors": 0, "detail": []}
    try:
        candidates = _candidates(store, base)
    except Exception as exc:  # noqa: BLE001
        _log(f"{base}: read failed ({type(exc).__name__}); skipped")
        result["error"] = type(exc).__name__
        return result
    if limit:
        candidates = candidates[:limit]
    for asset in candidates:
        aid = asset.get("id") or ""
        title = asset.get("title") or aid
        tmp_dir = tempfile.mkdtemp(prefix="classifiersweep_")
        tmp_path = Path(tmp_dir) / "asset.bin"
        try:
            drive.download(aid, tmp_path)
            sig = _sc.gather_signals(
                asset, local_path=str(tmp_path),
                ocr_reader=_sc.default_ocr_reader, cut_probe=_sc.default_cut_probe)
            verdict = _sc.classify(sig)
        except Exception as exc:  # noqa: BLE001 - one bad file never sinks the sweep
            _log(f"{base}: {title!r} sweep read failed ({type(exc).__name__}: {exc})")
            result["errors"] += 1
            continue
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
                os.rmdir(tmp_dir)
            except OSError:
                pass
        result["checked"] += 1
        if verdict.verdict == _sc.FINISHED:
            result["quarantined"] += 1
            result["detail"].append(
                f"{aid} ({title}): FINISHED -> quarantined "
                f"[{'; '.join(verdict.reasons)}]" + ("" if apply else " [dry-run]"))
            if apply:
                try:
                    store.update_asset(aid, {
                        "eligible": False,
                        "reject_reason": _idx.REJECT_FINISHED_CONTENT})
                except Exception as exc:  # noqa: BLE001
                    _log(f"{base}: quarantine write failed for {title!r} "
                        f"({type(exc).__name__})")
                    result["errors"] += 1
        elif verdict.verdict == _sc.RAW:
            result["raw"] += 1
        else:
            result["ambiguous"] += 1
    return result


def run(gyms, *, apply=False, limit=None):
    from .. import media_source_store
    from ..integrations import drive_client as _dc

    store = media_source_store.default_store()
    drive = _dc.DriveClient()
    if not store.available():
        _log("skipped: no Supabase creds for media tables")
        return []
    if not drive.available():
        _log("skipped: no GOOGLE_DRIVE_SA_JSON (cannot download to reclassify)")
        return []

    if not gyms:
        gyms = sorted({s.get("gym_id") for s in store.list_sources()
                       if s.get("gym_id")})

    results = [sweep_gym(g, store, drive, apply=apply, limit=limit) for g in gyms]
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n=== story_classifier_sweep [{mode}] ===")
    print(f"{'gym':<28}{'checked':>9}{'quarantined':>13}{'raw':>6}"
          f"{'ambiguous':>11}{'errors':>8}")
    for r in results:
        if r.get("error"):
            print(f"{r['gym']:<28} ERROR {r['error']}")
            continue
        print(f"{r['gym']:<28}{r['checked']:>9}{r['quarantined']:>13}{r['raw']:>6}"
              f"{r['ambiguous']:>11}{r['errors']:>8}")
    for r in results:
        for line in r.get("detail") or []:
            print(f"  {r['gym']}: {line}")
    total_quarantined = sum(r.get("quarantined", 0) for r in results)
    print(f"\nTOTAL quarantined: {total_quarantined}"
          + ("" if apply else " (dry-run — pass --apply to write)"))
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--apply", action="store_true", help="make the writes")
    p.add_argument("--limit", type=int, default=None,
                   help="cap checked assets per gym (safety valve)")
    p.add_argument("gyms", nargs="*")
    args = p.parse_args(argv)
    results = run(args.gyms, apply=args.apply, limit=args.limit)
    if any(r.get("error") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
