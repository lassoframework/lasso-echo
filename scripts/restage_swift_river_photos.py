#!/usr/bin/env python3
"""Restage Swift River's pending calendar from newly moderated Drive photos.

Dry-run is the default. ``--apply`` is the only write path and remains bounded
to this one gym. It never approves or publishes a calendar row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, time, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "swiftrivercrossfite5c9db"
_IG_KEY = f"{_BASE}_ig"
_DAYS = 30
_MIN_NEW_PHOTOS = 9
_TARGET_DAYS = tuple(f"2026-10-{day:02d}" for day in range(3, 12))
_DIGEST_FIELDS = ("id", "post_date", "account", "format", "status", "image_url",
                  "source_media_url", "media_not_ready_reason", "source_media_asset_id")


def _target_digest(rows):
    canonical = [{key: row.get(key) for key in _DIGEST_FIELDS} for row in rows]
    canonical.sort(key=lambda row: str(row["id"]))
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _target_rows(rows):
    target = [row for row in rows if str(row.get("post_date") or "")[:10] in _TARGET_DAYS]
    held = [row for row in target if _is_held_infographic(row)]
    if (len(held) != 36 or len({str(row.get("id")) for row in held}) != 36
            or len(target) != 36):
        return None
    for day in _TARGET_DAYS:
        group = [row for row in held if str(row.get("post_date"))[:10] == day]
        if sorted((str(row.get("account") or "").lower(),
                   str(row.get("format") or "").lower()) for row in group) != sorted([
                       ("instagram", "feed"), ("instagram", "story"),
                       ("facebook", "feed"), ("googlebusiness", "update")]):
            return None
    return held


def _parse_since(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--moderated-since must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--moderated-since must include a timezone")
    return parsed.astimezone(timezone.utc)


def _default_since():
    return datetime.combine(date.today(), time.min, tzinfo=timezone.utc)


def _months(start, days):
    from datetime import timedelta
    return sorted({(start + timedelta(days=offset)).isoformat()[:7]
                   for offset in range(days)})


def _calendar_rows(store, start, days):
    rows = []
    for month in _months(start, days):
        rows.extend(store.list_month(_BASE, month) or [])
    return rows


def _calendar_counts(rows):
    """Count active calendar provenance without guessing from URLs."""
    counts = {"drive": 0, "non_drive": 0, "no_creative": 0,
              "holds": 0, "human_owned": 0}
    wipeable = {"", "pending", "draft", "queued"}
    for row in rows:
        if row.get("source_media_asset_id"):
            counts["drive"] += 1
        elif row.get("image_url") or row.get("creative_public_url"):
            counts["non_drive"] += 1
        else:
            counts["no_creative"] += 1
        if row.get("media_not_ready_reason") is not None:
            counts["holds"] += 1
        if str(row.get("status") or "").lower() not in wipeable:
            counts["human_owned"] += 1
    return counts


def _new_pickable_photos(store, since):
    from agent import gym_media_selector

    photos = gym_media_selector.pickable(_BASE, "photo", store=store)
    result = []
    for asset in photos:
        evidence = asset.get("moderation_json") or {}
        observed = evidence.get("observed_at")
        try:
            observed_at = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                continue
            observed_at = observed_at.astimezone(timezone.utc)
        except ValueError:
            continue
        if observed_at >= since:
            result.append(asset)
    return result


def _is_held_infographic(row):
    """The narrow operator target: a pending Swift River igfill card with a hold."""
    if str(row.get("gym_id") or "") != _BASE:
        return False
    if str(row.get("status") or "").lower() != "pending":
        return False
    if row.get("media_not_ready_reason") is None or row.get("source_media_asset_id"):
        return False
    # Production Swift River provenance is ``igfill_``: feed/GBP cards carry it
    # in image_url; a Story's captioned image ends in ``__story.jpg`` but keeps its
    # raw igfill source in source_media_url. Do not infer from caption or status.
    values = (row.get("image_url"), row.get("source_media_url"))
    return any("igfill_" in urlparse(str(value or "")).path.lower() for value in values)


def _held_groups(rows):
    """{day: pending held infographic rows}; a day gets one photo across its posts."""
    groups = {}
    for row in rows:
        if _is_held_infographic(row) and row.get("id") and row.get("post_date"):
            groups.setdefault(str(row["post_date"])[:10], []).append(row)
    return groups


def _drive_candidates(photos, allocated):
    """media_swap candidate shape, restricted to this catch-up's fresh photo pool."""
    return [{"source": "drive", "kind": "photo", "key": str(asset["id"]),
             "asset": asset, "last_used": str(asset.get("last_used_at") or "")[:10],
             "used_count": int(asset.get("used_count") or 0),
             "name": str(asset.get("title") or asset["id"])}
            for asset in photos if str(asset["id"]) not in allocated]


def _replace_held_groups(calendar, media_store, rows, photos, book_rows):
    """Replace only exact held infographic rows, one newly approved photo per day.

    ``pick_replacement`` prepares every same-day variant before the first write. Each
    ``swap_media`` is additionally tenant/status guarded by the store; an approval
    racing this operator pass therefore stays untouched. There is no cross-row DB
    transaction, so a failed guarded write stops later days and reports partial work
    instead of attempting a risky rollback that could overwrite a newer human edit.
    """
    from agent import media_swap

    allocated = {str(row["source_media_asset_id"]) for row in book_rows
                 if row.get("source_media_asset_id")}
    replaced, failures = [], []
    staged = {}
    for day_key, group in sorted(_held_groups(rows).items()):
        root, siblings = group[0], group[1:]
        candidates = lambda _base, _row, pool=photos, used=allocated: _drive_candidates(pool, used)
        pick = media_swap.pick_replacement(
            _BASE, root, store=calendar, media_store=media_store, siblings=siblings,
            candidates_fn=candidates)
        if not pick.get("ok"):
            failures.append({"day": day_key, "reason": pick.get("reason", "pick_failed")})
            break

        planned = [(root, pick)] + [(row, (pick.get("siblings") or {}).get(str(row["id"])))
                                    for row in siblings]
        if any(not variant or not variant.get("ok") for _, variant in planned):
            failures.append({"day": day_key, "reason": "same_day_variant_unprepared"})
            break
        changed = []
        for row, variant in planned:
            try:
                updated = calendar.restage_held_media(
                    _BASE, row, image_url=variant["image_url"],
                    source_media_url=variant.get("source_media_url"),
                    extra_fields=media_swap.swap_fields(variant))
            except Exception as exc:  # a transport error may leave a partial stage
                failures.append({"day": day_key, "row_id": str(row["id"]),
                                 "reason": f"stage_exception:{type(exc).__name__}"})
                break
            if updated is None:
                failures.append({"day": day_key, "reason": "row_changed_or_not_pending"})
                break
            changed.append(str(row["id"]))
            staged[str(row["id"])] = {"image_url": variant["image_url"],
                                     "source_media_url": variant.get("source_media_url"),
                                     "source_media_asset_id": variant["source_media_asset_id"],
                                     "hold": row["media_not_ready_reason"]}
        if len(changed) != len(planned):
            # A guarded race can only leave an already-successful same-day sibling
            # replaced. Stop here; no later date gets touched and no human edit is
            # rolled back.
            break
        allocated.add(str(pick["source_media_asset_id"]))
        # Stamp usage only after all variants for the day confirmed their replacement.
        replaced.append({"day": day_key, "rows": changed,
                         "asset_id": str(pick["source_media_asset_id"])})
    return replaced, failures, staged


def _resolve():
    from agent import accounts, config
    from agent.client_media_sync import _banned_words_for, _resolve_client_voice_path
    from agent.portal_calendar_store import SupabaseCalendarStore
    from agent.voice import load_voice

    account = accounts.get_account(_IG_KEY)
    voice_path = os.path.join("brand_voice", _BASE, "lasso_voice.md")
    if account is not None:
        voice_path = getattr(account, "voice_doc_path", lambda: voice_path)()
    voice = load_voice(_resolve_client_voice_path(_BASE, voice_path))
    calendar_store = SupabaseCalendarStore() if config.portal_calendar_supabase_enabled() else None
    from agent.media_source_store import default_store
    return {"account": account, "voice": voice, "calendar": calendar_store,
            "media": default_store(), "banned_words": _banned_words_for(_BASE),
            "library_path": os.path.join(config.LIBRARY_PATH, _BASE)}


def _report(label, rows):
    print(f"{label} calendar provenance: {json.dumps(_calendar_counts(rows), sort_keys=True)}")


def run(*, apply=False, moderated_since=None, start=None, days=_DAYS, ctx=None,
        expected_digest=None):
    """Perform a read-only readiness check, optionally followed by one safe rebuild."""
    moderated_since = moderated_since or _default_since()
    start = start or date.today()
    ctx = ctx or _resolve()
    calendar = ctx["calendar"]
    if ctx["account"] is None or ctx["voice"] is None or calendar is None:
        return {"ok": False, "reason": "missing account, voice, or calendar store"}
    if not ctx["media"].available():
        return {"ok": False, "reason": "media store unavailable"}

    before = _calendar_rows(calendar, start, days)
    _report("before", before)
    photos = _new_pickable_photos(ctx["media"], moderated_since)
    print(f"newly moderated pickable Drive photos: {len(photos)} (minimum {_MIN_NEW_PHOTOS})")
    if len(photos) < _MIN_NEW_PHOTOS:
        _report("after (unchanged)", before)
        return {"ok": False, "reason": "insufficient newly moderated Drive photos",
                "pickable_new_photos": len(photos), "before": _calendar_counts(before)}

    target = _target_rows(before)
    if target is None:
        return {"ok": False, "reason": "exact 36-row Oct 3-11 held target absent"}
    digest = _target_digest(target)
    if not apply:
        return {"ok": True, "dry_run": True, "expected_digest": digest,
                "held_infographic_days": 9, "target_rows": 36,
                "pickable_new_photos": len(photos), "before": _calendar_counts(before)}
    if not expected_digest or expected_digest.lower() != digest:
        return {"ok": False, "reason": "target digest missing or changed", "observed_digest": digest}
    # Read the surrounding active book as well as the target month before choosing
    # any Drive asset. A pickable asset can still sit on a scheduled row.
    book = list(before)
    for month in ("2026-08", "2026-09", "2026-11", "2026-12"):
        book.extend(calendar.list_month(_BASE, month) or [])
    replaced, failures, staged = _replace_held_groups(calendar, ctx["media"], target, photos, book)
    if failures or len(staged) != 36:
        return {"ok": False, "reason": "partial staging; holds retained", "replaced": replaced,
                "failures": failures, "staged_ids": sorted(staged)}
    verified = {}
    for row in target:
        rid = str(row["id"])
        current = calendar.get_row(_BASE, rid)
        expected = staged[rid]
        if (current is None or current.get("status") != "pending"
                or current.get("media_not_ready_reason") != expected["hold"]
                or any(current.get(key) != expected[key] for key in
                       ("image_url", "source_media_url", "source_media_asset_id"))):
            return {"ok": False, "reason": "staged readback mismatch; holds retained",
                    "row_id": rid, "staged_ids": sorted(staged)}
        verified[rid] = current
    # Reserve every selected photo before any hold is released. If stamping
    # fails, all 36 cards stay held; successful reservations are conservative
    # and prevent another planner pass from reusing these photos meanwhile.
    from agent import gym_media_selector
    ledger_failures = []
    for group in replaced:
        try:
            asset = ctx["media"].get_asset(group["asset_id"])
            if not asset:
                raise ValueError("staged asset missing from media store")
            gym_media_selector.stamp_use(asset, _BASE, group["day"], store=ctx["media"])
        except Exception as exc:
            ledger_failures.append({"day": group["day"],
                                    "reason": f"ledger_exception:{type(exc).__name__}"})
    if ledger_failures:
        return {"ok": False, "reason": "usage reservation failed; holds retained",
                "replaced": replaced, "ledger_failures": ledger_failures,
                "staged_ids": sorted(staged)}
    released = []
    for rid in sorted(verified):
        try:
            release = calendar.restage_held_media(_BASE, verified[rid], release=True)
        except Exception as exc:
            return {"ok": False, "reason": f"release_exception:{type(exc).__name__}; reconciliation required",
                    "released_ids": released, "uncertain_id": rid,
                    "held_ids": sorted(set(verified) - set(released) - {rid})}
        if release is None:
            return {"ok": False, "reason": "partial release requires reconciliation",
                    "released_ids": released, "held_ids": sorted(set(verified) - set(released))}
        released.append(rid)
    for rid in sorted(verified):
        current = calendar.get_row(_BASE, rid)
        if (current is None or current.get("media_not_ready_reason") is not None
                or any(current.get(key) != staged[rid][key] for key in
                       ("image_url", "source_media_url", "source_media_asset_id"))):
            return {"ok": False, "reason": "released readback mismatch; reconciliation required",
                    "row_id": rid, "released_ids": released}
    return {"ok": True, "replaced": replaced, "released_ids": released,
            "expected_digest": digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the bounded rebuild")
    parser.add_argument("--expected-digest", help="SHA256 from independently reviewed target rows")
    parser.add_argument("--moderated-since", type=_parse_since, default=_default_since(),
                        help="only count clean photo evidence at or after this UTC timestamp")
    args = parser.parse_args(argv)
    result = run(apply=args.apply, moderated_since=args.moderated_since,
                 expected_digest=args.expected_digest)
    print(json.dumps(result, default=str, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
