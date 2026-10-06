#!/usr/bin/env python3
"""Receipt-bound permutation of Nine7's existing machine-owned photo assignments.

Dry run is read-only. Apply is OFF by default and requires the exact dry-run
digest, ticket/request binding, and a fresh private receipt. No new photo is
introduced or reused: the source multiset inside the explicit window is fixed.
An interrupted receipt requires reconciliation, never an automatic retry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import burst_spacing, dam, media_guard, media_swap
from agent.library import list_creatives
from agent.portal_calendar_store import SupabaseCalendarStore
from scripts.hold_future_media_repeats import _receipt

GYM = "crossfitnine7f7dadc"
TICKET = "b355c2cf-3b1d-4eec-8b23-282062f662f9"
REQUEST_KEY = "1fba4eac7339d4c882b8392b7ca3f955d991b27b96cbd5af51bd596a7c8b8cae"
TARGET_FIRST, TARGET_LAST = "2026-10-21", "2026-10-26"
TARGET_ROOTS = {
    "2026-10-21": "9fd93969-e650-4c33-8c7f-7c6bca367173",
    "2026-10-22": "5704732c-49bb-4d65-ad57-1f23969a9f9f",
    "2026-10-23": "4fd97f5c-b725-4a3b-ba36-628ccf07ce94",
    "2026-10-24": "9ffec1ae-331d-4c18-a0a0-7d0ed3af7689",
    "2026-10-25": "c9d2f3a4-fdce-4b39-a7ad-044620093fda",
    "2026-10-26": "046d8cf2-173d-4492-a9c7-0e5ccde29308",
}
TARGET_SEQUENCES = {
    day: ("img", number) for day, number in zip(
        TARGET_ROOTS, (2715, 2717, 2719, 2720, 2728, 2731))
}
_MACHINE = {"pending", "draft", "queued"}
_SNAPSHOT = ("id", "gym_id", "post_date", "status", "variant_status", "account",
             "format", "caption", "image_url", "source_media_url", "source_media_asset_id",
             "thumbnail_url", "media_not_ready_reason", "created_at", "scheduled_at",
             "slot_index", "published_at", "late_post_id", "publish_claim_token",
             "publish_reservation_day")
_MEDIA = {"image_url", "source_media_url", "source_media_asset_id", "thumbnail_url",
          "visual_group_key", "byte_hash", "r2_key", "drive_file_id", "updated_at"}


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _snapshot(row):
    if not isinstance(row, dict) or any(key not in row for key in _SNAPSHOT):
        raise ValueError("incomplete calendar row")
    return {key: row[key] for key in _SNAPSHOT}


def _outside_book_digest(store, gym, first, last):
    outside = [row for row in store.list_photo_restage_book(gym)
               if row.get("gym_id") == gym
               and not first <= str(row.get("post_date") or "")[:10] <= last]
    return _digest(outside)


def _verify_receipt_readback(store, gym, first, last, window_hashes, readback,
                             outside_digest):
    """Prove the complete window and untouched book still match a receipt."""
    if (not isinstance(window_hashes, dict) or not window_hashes
            or not isinstance(readback, list) or not readback
            or not isinstance(outside_digest, str)):
        raise ValueError("incomplete reflow receipt")
    changed = {}
    for saved in readback:
        snap = _snapshot(saved)
        rid = str(snap["id"])
        if rid in changed or rid not in window_hashes:
            raise ValueError("receipt readback identities are incomplete or duplicated")
        changed[rid] = snap
    observed_rows = store.rows_in_range_complete(gym, first, last, all_statuses=True)
    observed = {str(row["id"]): _snapshot(row) for row in observed_rows}
    if len(observed) != len(observed_rows) or set(observed) != set(window_hashes):
        raise ValueError("complete window readback identities changed")
    for rid, before_hash in window_hashes.items():
        if rid in changed:
            if observed[rid] != changed[rid]:
                raise ValueError("changed window row drifted")
        elif _digest(observed[rid]) != before_hash:
            raise ValueError("unmoved window row drifted")
    if _outside_book_digest(store, gym, first, last) != outside_digest:
        raise ValueError("out-of-window book drifted")


def _path_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _library(lib):
    if not lib or not os.path.isdir(lib):
        raise ValueError("exact Nine7 library unavailable")
    photos = [c for c in list_creatives(lib) if c.media_type == "image"]
    cohorts = burst_spacing.cohort_map(photos)
    by_name = {}
    for creative in photos:
        path = creative.path
        side = dam.read_sidecar(path)
        if (path not in cohorts or side.get("approved") is not True
                or side.get("review") or side.get("moderation") not in ("clean", "approved")):
            continue
        name = os.path.basename(path)
        if name in by_name:
            raise ValueError("ambiguous photo basename")
        by_name[name] = {"path": path, "sha256": _path_hash(path),
                         "cohort": cohorts[path]}
    return by_name


def _penalty(days, assignments, cohorts, *, first=None, last=None):
    ordered = [day for day in days if first is None or first <= day <= last]
    return sum(cohorts[assignments[a]] == cohorts[assignments[b]]
               for a, b in zip(ordered, ordered[1:])
               if (date.fromisoformat(b) - date.fromisoformat(a)).days == 1)


def plan(store, *, gym, first, last, ticket, request_key, library_path, today=None):
    if (gym != GYM or ticket != TICKET or request_key != REQUEST_KEY
            or first > TARGET_FIRST or last < TARGET_LAST
            or (date.fromisoformat(last) - date.fromisoformat(first)).days > 30
            or first < (today or datetime.now(timezone.utc).date().isoformat())):
        raise ValueError("exact Nine7 ticket, future bounded window and request key required")
    rows = store.rows_in_range_complete(gym, first, last, all_statuses=True)
    by_day = {}
    for row in rows:
        _snapshot(row)
        if row["gym_id"] != gym or row["variant_status"] != "active":
            raise ValueError("out-of-scope row in complete window")
        by_day.setdefault(str(row["post_date"])[:10], []).append(row)
    photos = _library(library_path)
    keys = {media_guard.row_media_key(r) for r in rows}
    reframes = media_guard.reframe_map(library_path, keys)
    groups, assignment, cohorts = {}, {}, {}
    for day, siblings in sorted(by_day.items()):
        sources = {reframes.get(media_guard.row_media_key(r), media_guard.row_media_key(r))
                   for r in siblings}
        if len(sources) != 1:
            raise ValueError(f"multiple or ambiguous logical photo groups on {day}")
        source = sources.pop()
        if source not in photos:
            raise ValueError(f"calendar source cannot be proven against local photo on {day}")
        if any(r.get("source_media_asset_id") for r in siblings):
            raise ValueError("Drive identity cannot be permuted by this local-photo operator")
        for row in siblings:
            if row["format"] not in ("feed", "story"):
                raise ValueError("unsupported platform render format")
        groups[day] = sorted(siblings, key=lambda r: str(r["id"]))
        assignment[day] = source
        cohorts[source] = photos[source]["cohort"]
    if len(set(assignment.values())) != len(assignment):
        raise ValueError("existing window repeats a source across days")
    book = store.list_photo_restage_book(gym)
    outside = [r for r in book if r.get("gym_id") == gym
               and not first <= str(r.get("post_date") or "")[:10] <= last]
    outside_keys = {media_guard.row_media_key(r) for r in outside}
    outside_reframes = media_guard.reframe_map(library_path, outside_keys)
    for row in outside:
        if row.get("variant_status") not in (None, "active") or row.get("status") in ("denied", "killed"):
            continue
        key = media_guard.row_media_key(row)
        if outside_reframes.get(key, key) in assignment.values():
            raise ValueError("source is also held by an out-of-window calendar row")
    outside_digest = _digest(outside)
    if any(day not in groups or not any(r["id"] == rid and r["account"] == "instagram"
                                         and r["format"] == "feed" and r["status"] == "pending"
                                         for r in groups[day])
           for day, rid in TARGET_ROOTS.items()):
        raise ValueError("exact audited target root missing or changed")
    target_shape = {("instagram", "feed"), ("facebook", "feed"),
                    ("instagram", "story"), ("googlebusiness", "feed")}
    if any(len(groups[day]) != 4 or
           {(r["account"], r["format"]) for r in groups[day]} != target_shape
           for day in TARGET_ROOTS):
        raise ValueError("audited coupled platform rows missing or changed")
    if any(burst_spacing.parse_camera_sequence(assignment[day]) != sequence
           for day, sequence in TARGET_SEQUENCES.items()):
        raise ValueError("audited Nine7 camera sequence changed")
    movable = [day for day, group in groups.items()
               if all(r["status"] in _MACHINE and r["media_not_ready_reason"] is None
                      and r["published_at"] is None and r["late_post_id"] is None
                      and r["publish_claim_token"] is None
                      and r["publish_reservation_day"] is None for r in group)]
    if any(day not in movable for day in TARGET_ROOTS):
        raise ValueError("protected or held target row")
    if any(len(groups[day]) != 4 or
           {(r["account"], r["format"]) for r in groups[day]} != target_shape
           for day in movable):
        raise ValueError("incomplete machine-owned coupled platform rows in reflow window")
    days = sorted(groups)
    baseline = (_penalty(days, assignment, cohorts, first=TARGET_FIRST, last=TARGET_LAST),
                _penalty(days, assignment, cohorts))
    selected = dict(assignment)
    # A swap retains every photo exactly once. Prefer a strict target improvement,
    # then whole-window improvement; never accept a worse whole-window score.
    while True:
        best = None
        current = (_penalty(days, selected, cohorts, first=TARGET_FIRST, last=TARGET_LAST),
                   _penalty(days, selected, cohorts))
        for i, left in enumerate(movable):
            for right in movable[i + 1:]:
                if selected[left] == selected[right]:
                    continue
                trial = dict(selected)
                trial[left], trial[right] = trial[right], trial[left]
                score = (_penalty(days, trial, cohorts, first=TARGET_FIRST, last=TARGET_LAST),
                         _penalty(days, trial, cohorts))
                if score < current and score[1] <= baseline[1] and (best is None or score < best[0]):
                    best = score, trial
        if best is None:
            break
        selected = best[1]
    after = (_penalty(days, selected, cohorts, first=TARGET_FIRST, last=TARGET_LAST),
             _penalty(days, selected, cohorts))
    if after[0] >= baseline[0] or after[1] > baseline[1]:
        raise ValueError("thin library: no safe material adjacent-cohort improvement")
    moves = [{"day": day, "rows": [_snapshot(r) for r in groups[day]],
              "from": assignment[day], "to": selected[day],
              "source_sha256": photos[selected[day]]["sha256"]}
             for day in days if selected[day] != assignment[day]]
    if not moves or sorted(m["from"] for m in moves) != sorted(m["to"] for m in moves):
        raise ValueError("not a pure source permutation")
    image = {"operation": "nine7_burst_media_reflow", "gym": gym, "ticket": ticket,
             "request_key": request_key, "first": first, "last": last,
             "baseline": baseline, "after": after, "moves": moves,
             "outside_book_digest": outside_digest,
             "window_row_hashes": {str(r["id"]): _digest(_snapshot(r)) for r in rows},
             "window_digest": _digest([_snapshot(r) for r in rows])}
    return {**image, "target_digest": _digest(image)}, photos


def _prepare(gym, root, siblings, photo):
    """Re-render every platform variant from the attested original bytes."""
    import tempfile
    from agent import config, feed_image, story_image
    from agent.jobs.media_repeat_sweep import _gym_name
    from scripts.restage_future_igfill_photos import _host_verified
    from PIL import Image
    if not config.story_format_enabled() or _path_hash(photo["path"]) != photo["sha256"]:
        raise ValueError("story renderer off or original photo changed")
    with Image.open(photo["path"]) as image:
        image.verify()
    with tempfile.TemporaryDirectory(prefix="nine7-reflow-") as work:
        def host(path):
            return _host_verified(path, f"{gym}_ig")
        def feed(path):
            rendered = feed_image.get_or_make_feed_image(path, work)
            with Image.open(rendered or path) as image:
                if feed_image.needs_autofit(*image.size):
                    raise ValueError("feed geometry invalid")
                image.verify()
            return host(rendered or path)
        def story(_base, row, path, _lib):
            rendered = story_image.get_or_make_story_image(
                path, (row.get("caption") or "").strip(), _gym_name(gym), work)
            if not rendered:
                raise ValueError("captioned story render failed")
            with Image.open(rendered) as image:
                if image.size != (1080, 1920):
                    raise ValueError("story geometry invalid")
                image.verify()
            return host(rendered)
        return media_swap.pick_replacement(
            gym, root, store=None, library_path=os.path.dirname(photo["path"]),
            siblings=siblings, candidates_fn=lambda *_: [{"source": "local", "kind": "photo",
                "key": os.path.basename(photo["path"]), "path": photo["path"]}],
            materialize_fn=lambda _candidate: {"path": photo["path"]},
            host_fn=host, feed_fn=feed, reburn_fn=story)


def run(*, store=None, gym, first, last, ticket, request_key, library_path=None,
        apply=False, expected_digest=None, receipt_path=None, prepare_fn=None, today=None):
    store = store or SupabaseCalendarStore()
    library_path = library_path or media_swap.library_path_for(gym)
    receipt = Path(receipt_path) if receipt_path else None
    if apply and receipt is not None and receipt.exists():
        try:
            prior = json.loads(receipt.read_text())
        except Exception:
            return {"ok": False, "reason": "existing unreadable receipt requires reconciliation"}
        if (prior.get("operation") == "nine7_burst_media_reflow"
                and prior.get("gym") == gym and prior.get("ticket") == ticket
                and prior.get("first") == first and prior.get("last") == last
                and prior.get("target_digest") == expected_digest
                and prior.get("request_key") == request_key
                and prior.get("state") == "readback_verified"):
            try:
                _verify_receipt_readback(
                    store, gym, first, last, prior.get("window_row_hashes"),
                    prior.get("readback"), prior.get("outside_book_digest"))
                if sorted(prior.get("changed_ids", [])) != sorted(
                        str(row["id"]) for row in prior["readback"]):
                    raise ValueError("receipt changed IDs differ from readback")
            except Exception:
                return {"ok": False,
                        "reason": "receipt replay full readback drifted or is unavailable; reconciliation required"}
            return {"ok": True, "replay": True, "receipt": str(receipt),
                    "changed_ids": prior.get("changed_ids", [])}
        return {"ok": False, "reason": "existing receipt requires reconciliation"}
    proposed, photos = plan(store, gym=gym, first=first, last=last, ticket=ticket,
                            request_key=request_key, library_path=library_path, today=today)
    if not apply:
        return {"ok": True, "dry_run": True, "preflight": proposed}
    if os.environ.get("ECHO_NINE7_BURST_REFLOW_ENABLED", "").lower() != "true":
        return {"ok": False, "reason": "reflow apply flag OFF"}
    if expected_digest != proposed["target_digest"] or not receipt_path:
        return {"ok": False, "reason": "exact dry-run digest and new receipt required"}
    progress = {**proposed, "state": "before_write", "changed_ids": [], "readback": [],
                "inflight_ids": [], "prepared": [], "started_at": datetime.now(timezone.utc).isoformat()}
    progress["rollback"] = ("Manual reconciliation only: compare each before-image row and "
                            "readback, then use a fresh exact CAS and freshly rendered "
                            "platform assets. Never replay this receipt as a write.")
    _receipt(receipt, progress, create=True)
    prepare = prepare_fn or _prepare
    try:
        for move in proposed["moves"]:
            rows = move["rows"]
            root = next((r for r in rows if r["account"] == "instagram" and r["format"] == "feed"), rows[0])
            siblings = [r for r in rows if r["id"] != root["id"]]
            pick = prepare(gym, root, siblings, photos[move["to"]])
            variants = [(root, pick)] + [(r, (pick.get("siblings") or {}).get(str(r["id"])))
                                        for r in siblings]
            if any(not v or not v.get("ok") or v.get("kind") != "photo"
                   or v.get("key") != move["to"] or not str(v.get("image_url") or "").startswith("https://")
                   for _, v in variants):
                raise ValueError("incomplete coupled render")
            progress["prepared"] = [{"row_id": r["id"], "image_url": v["image_url"],
                                     "source_media_url": v.get("source_media_url")}
                                    for r, v in variants]
            progress["inflight_ids"] = [r["id"] for r, _ in variants]
            progress["state"] = "write_intent"
            _receipt(receipt, progress)
            for before, variant in variants:
                current = store.get_row(gym, str(before["id"]))
                if _snapshot(current) != before:
                    raise ValueError("concurrent row drift before CAS")
                updated = store.reflow_pending_burst_media(
                    gym, current, image_url=variant["image_url"],
                    source_media_url=variant.get("source_media_url"),
                    source_media_asset_id=variant.get("source_media_asset_id"),
                    first=first, last=last, ticket=ticket, request_key=request_key,
                    thumbnail_url=variant.get("thumbnail_url") or None,
                    render_evidence=variant.get("render_evidence"))
                if updated is None:
                    raise ValueError("exact media CAS conflict")
                observed = store.get_row(gym, str(before["id"]))
                if (_snapshot(observed) != _snapshot(updated)
                        or any(observed.get(k) != current.get(k) for k in current if k not in _MEDIA)):
                    raise ValueError("independent readback mismatch")
                progress["changed_ids"].append(str(before["id"]))
                progress["readback"].append(_snapshot(observed))
                progress["inflight_ids"].remove(str(before["id"]))
                progress["state"] = "partial_reflow"
                _receipt(receipt, progress)
        _verify_receipt_readback(
            store, gym, first, last, proposed["window_row_hashes"],
            progress["readback"], proposed["outside_book_digest"])
        progress["state"] = "readback_verified"
        _receipt(receipt, progress)
    except Exception as exc:
        progress["state"] = "reconcile_required"
        progress["error"] = f"{type(exc).__name__}: {exc}"
        try:
            _receipt(receipt, progress)
        except Exception:
            pass
        return {"ok": False, "reason": "reflow interrupted; private receipt requires reconciliation",
                "receipt": str(receipt), "changed_ids": progress["changed_ids"],
                "inflight_ids": progress["inflight_ids"]}
    return {"ok": True, "receipt": str(receipt), "changed_ids": progress["changed_ids"],
            "before_score": proposed["baseline"], "after_score": proposed["after"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gym", "first", "last", "ticket", "request-key"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--library-path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt")
    args = parser.parse_args(argv)
    try:
        result = run(gym=args.gym, first=args.first, last=args.last, ticket=args.ticket,
                     request_key=args.request_key, library_path=args.library_path,
                     apply=args.apply, expected_digest=args.expected_digest,
                     receipt_path=args.receipt)
    except Exception as exc:
        result = {"ok": False, "reason": f"preflight failed:{type(exc).__name__}: {exc}"}
    displayed = dict(result)
    if "preflight" in displayed:
        plan_value = displayed["preflight"]
        displayed["preflight"] = {
            key: value for key, value in plan_value.items()
            if key not in ("moves", "window_row_hashes")}
        displayed["preflight"]["moves"] = [
            {"day": move["day"], "row_ids": [row["id"] for row in move["rows"]],
             "from": move["from"], "to": move["to"],
             "source_sha256": move["source_sha256"]}
            for move in plan_value["moves"]]
    print(json.dumps(displayed, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
