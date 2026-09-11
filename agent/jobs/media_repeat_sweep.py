"""
media_repeat_sweep.py — find (and fix) the SAME PHOTO sitting on MULTIPLE
DIFFERENT DAYS of a gym's calendar (Blake, 2026-08-31: a client noticed one
photo across different weeks).

WHAT IT DOES, per gym:
  1. Reads the trailing repeat window + forward book (rows_in_range: pending /
     approved / publishing / published / coach_review).
  2. Keys every row by its RAW photo: source_media_url when present (stories
     carry it — their image_url is a burned caption card), else image_url, with
     feed-autofit reframe names ('<sha12>__feed.jpg') resolved back to the raw
     library photo. Same-DATE siblings (FB mirror, paired story) are ONE post —
     never a repeat.
  3. For each photo on more than one date, ONE date keeps it:
       - any date with a published / publishing / approved row wins (earliest
         such date when several — those rows are never touched);
       - else the earliest date keeps it.
     Every LATER date's PENDING/COACH_REVIEW rows get a FRESH, genuinely unused,
     PIL-VALIDATED library photo: feeds are re-pointed (autofit parity), the
     paired story is re-burned (caption on the new photo) when the story-format
     lane is armed, and source_media_url is updated so a later edited-caption
     re-burn uses the new photo.
  4. NEVER touches published or publishing rows. NEVER swaps an APPROVED row's
     media (the gym approved that exact card) — approved duplicates are
     REPORTED, not mutated (swap_media is status-guarded server-side anyway).
     A gym with no unused photo left is reported as SMALL LIBRARY (one deduped
     digest) and its rows are left alone — never fabricated media, never an
     emptied slot.

Dry-run by default; --apply makes the writes. Prints a per-gym before/after
table. Read/write goes through portal_calendar_store only (id+gym scoped).

Usage:
  python -m agent.jobs.media_repeat_sweep [--apply] [--horizon 62] [gym ...]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

from agent import config, media_guard
from agent.portal_calendar_store import SupabaseCalendarStore

FIXABLE = ("pending", "coach_review")
UNTOUCHABLE = ("published", "publishing")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _log(msg):
    print(f"[media-sweep] {msg}")


def _lib_dir(base):
    from agent.client_media_sync import _library_dir
    return _library_dir(base)


def _is_real_image(path):
    """A pickable replacement must be a real, decodable image — never a leaked
    test fixture (the 11-byte FAKEJPEGs found in gritx's production library) or
    a truncated upload."""
    try:
        if os.path.getsize(path) < 2048:
            return False
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def _owner_date(by_date):
    """The date that KEEPS the photo: the earliest date carrying a published/
    publishing/approved row wins; else the earliest date."""
    anchored = sorted(d for d, rows in by_date.items()
                      if any(str(r.get("status") or "").lower()
                             in ("published", "publishing", "approved")
                             for r in rows))
    if anchored:
        return anchored[0]
    return sorted(by_date)[0]


def _cluster_key(lib, key):
    """The near-dupe identity of a library file: dam.rotation_key when the gym
    is vision-clustered, else the case-folded stem (catches IMG_6771.JPG vs
    IMG_6771.jpg — the same photo uploaded twice)."""
    try:
        from agent import dam
        rk = dam.rotation_key(os.path.join(lib, key))
        if rk and rk != key:
            return rk
    except Exception:  # noqa: BLE001
        pass
    return os.path.splitext(key)[0].lower()


def _fresh_photo(lib, state, exclude):
    """A genuinely unused, VALIDATED library image (never on any book/window
    date, never in exclude, never a NEAR-DUPE of one), deterministic.
    (None, None) when nothing unused."""
    used = set(state.keys()) | set(exclude)
    lib_names = media_guard.library_keys(lib)
    used_clusters = {_cluster_key(lib, k) for k in used if k in lib_names}
    for key in sorted(lib_names):
        if key in used:
            continue
        if os.path.splitext(key)[1].lower() not in _IMG_EXTS:
            continue                       # image swaps only; videos need their lanes
        if _cluster_key(lib, key) in used_clusters:
            continue                       # a near-dupe of a used photo repeats visually
        path = os.path.join(lib, key)
        if os.path.isfile(path) and _is_real_image(path):
            return key, path
    return None, None


def _asset_state(rows):
    """{drive_asset_id: {(iso_date, 'x'), ...}} for the rows this sweep read. The Drive
    pool is keyed by asset id, a different id space from library basenames, so blocking
    "already on the book" needs its own map (media_guard.row_asset_key). Grows as the
    sweep places assets, so one run never hands the same clip to two dates."""
    state = {}
    for row in rows or []:
        aid = media_guard.row_asset_key(row)
        pd = str((row or {}).get("post_date") or "")[:10]
        if aid and pd:
            state.setdefault(aid, set()).add((pd, "x"))
    return state


def _drive_candidate_count(base, asset_state):
    """How many Drive-pool assets the sweep could swap in right now (pure store read,
    no download, no transcode, no spend) -- what a DRY RUN is allowed to know. 0 when
    the lane is unarmed or the pool is empty. Never raises."""
    try:
        from agent import media_swap
        return len(media_swap.drive_candidates(base, set(asset_state or {})))
    except Exception as exc:  # noqa: BLE001 - a pool read never breaks a sweep
        _log(f"{base}: Drive pool read failed ({type(exc).__name__})")
        return 0


def _feed_first(fixable):
    """The row a Drive pick is shaped for FIRST: the feed (its media is the post), so
    the story siblings are re-burned onto the same materialized file. Deterministic."""
    return sorted(fixable, key=lambda r: (str(r.get("format") or "").lower() != "feed",
                                          str(r.get("id") or "")))


def _swap_from_drive_pool(base, store, fixable, *, state, asset_state, rows, result,
                          key, pd, picker=None):
    """REPLACE A REPEAT FROM THE GYM'S CONNECTED DRIVE POOL when no unused LOCAL image
    is left (AGENT_MEDIA_REPEAT_SWEEP_DRIVE, default OFF).

    This is the half of PR #98 the nightly sweep never got. The month build learned to
    stage from the Drive pool; the sweep -- the only thing that can fix a row already on
    the book -- still drew from the local library alone, so a gym with a full connected
    Drive folder and exhausted local stills kept its repeats and was told to "add
    photos" (John Weeks / Tough Temple).

    Reuses media_swap.pick_replacement, the SAME engine the portal's edit-image button
    runs, so the Drive pool's guards are the ones already shipped and tested:
    eligibility, the 90-day cooldown, this-month exclusion, tenant isolation, and
    nothing already on the book (photo keys AND asset ids). Every sweep rail still
    holds: only `fixable` (pending / coach_review) rows are passed, the write is the
    status-guarded store.swap_media, and nothing is fabricated.

    True when at least one row was re-pointed. Never raises."""
    from agent import media_swap
    ordered = _feed_first(fixable)
    row, siblings = ordered[0], ordered[1:]
    try:
        pick = (picker or media_swap.pick_replacement)(
            base, row, store=store, library_path=_lib_dir(base),
            book_state=state, asset_state=asset_state, siblings=siblings)
    except Exception as exc:  # noqa: BLE001 - a picker failure is "no Drive swap"
        _log(f"{base}: Drive replacement failed for {key} {pd} ({type(exc).__name__})")
        return False
    if not pick.get("ok"):
        return False
    variants = pick.get("siblings") or {}
    # ALL OR NOTHING, exactly like the portal swap: a sibling the picker could not
    # shape means this date is left alone rather than half re-pointed.
    missing = [str(s.get("id")) for s in siblings
               if not (variants.get(str(s.get("id"))) or {}).get("ok")]
    if missing:
        _log(f"{base}: {key} {pd}: Drive swap skipped, a sibling row could not be "
             "shaped from the same clip")
        return False

    swapped = []
    for target, var in [(row, pick)] + [(s, variants[str(s.get("id"))]) for s in siblings]:
        rid = target.get("id")
        try:
            done = store.swap_media(base, rid, var["image_url"],
                                    source_media_url=var.get("source_media_url"),
                                    extra_fields=media_swap.swap_fields(var))
        except Exception as exc:  # noqa: BLE001 - one row never undoes the others
            _log(f"{base}: swap_media failed for {rid} ({type(exc).__name__})")
            done = None
        if done is not None:
            swapped.append(str(rid))
            result["rows_repointed"] += 1
            # Parity with the local path's counter: _finish re-burned the story's
            # caption onto the new media, so the table must count it.
            if (str(target.get("format") or "").lower() == "story"
                    and config.story_format_enabled()):
                result["stories_reburned"] += 1
    if not swapped:
        return False

    new_asset = str(pick.get("source_media_asset_id") or "")
    new_key = str(pick.get("key") or new_asset or "drive")
    if new_asset:
        # This run must not hand the same clip to the next repeated date.
        asset_state.setdefault(new_asset, set()).add((pd, "x"))
    if pick.get("source") == "local" and pick.get("key"):
        state.setdefault(str(pick["key"]), set()).add((pd, "x"))
    # Settle the usage ledgers. `rows` is the PRE-swap read, so the rows we just
    # re-pointed still show the old asset there -- swapped_ids excludes them, and any
    # row left carrying it (an approved sibling) correctly keeps it stamped.
    try:
        media_swap.after_swap(base, row, pick, book_rows=rows, swapped_ids=swapped)
    except Exception as exc:  # noqa: BLE001 - a ledger hiccup never undoes the swap
        _log(f"{base}: Drive usage ledger not settled ({type(exc).__name__})")
    # Say which pool it actually came from: pick_replacement draws from BOTH, and a
    # local VIDEO is a legitimate answer here (_fresh_photo only ever looked at images).
    where = ("the connected Drive pool" if pick.get("source") == "drive"
             else "the local library")
    result["detail"].append(
        f"{key} {pd}: -> {new_key} from {where} ({len(swapped)} row(s))")
    return True


def _gym_name(base):
    try:
        from agent.accounts import get_account
        acct = get_account(f"{base}_ig")
        name = getattr(acct, "display_name", "") if acct else ""
        for suf in (" IG", " FB", " Instagram", " Facebook"):
            if name.endswith(suf):
                name = name[: -len(suf)].strip()
        return name or base
    except Exception:  # noqa: BLE001
        return base


def _reburn_story(base, row, new_path, lib):
    """Burn the story's caption onto the NEW photo and host it. None on failure."""
    try:
        from agent import media_host, story_image
        caption = (row.get("caption") or "").strip()
        asset = story_image.get_or_make_story_image(
            new_path, caption, _gym_name(base), lib, logger=_log)
        if not asset or not config.hosting_enabled():
            return None
        return media_host.host_media(asset, f"{base}_ig") or None
    except Exception as exc:  # noqa: BLE001
        _log(f"{base}: story re-burn error ({type(exc).__name__})")
        return None


def _grouped(rows, lib):
    """{raw_key: {date: [row, ...]}} across guard-scope rows, autofit reframe
    names resolved back to raw library photos so a feed and its paired story
    share one key."""
    keyed = []
    raw_keys = set()
    for row in rows or []:
        status = str(row.get("status") or "").strip().lower()
        if status not in media_guard.FORWARD_STATUSES and status != "published":
            continue
        acct = str(row.get("account") or "").strip().lower()
        if acct not in ("instagram", "ig", "facebook", "fb", ""):
            continue
        pd = str(row.get("post_date") or "")[:10]
        key = media_guard.row_media_key(row)
        if not pd or not key:
            continue
        keyed.append((key, pd, row))
        raw_keys.add(key)
    rmap = media_guard.reframe_map(lib, raw_keys)
    out = {}
    for key, pd, row in keyed:
        out.setdefault(rmap.get(key, key), {}).setdefault(pd, []).append(row)
    return out


def cross_day_repeats(rows, lib):
    """The subset of _grouped with >1 distinct date and at least one
    forward-book row (something the sweep could act on or must report)."""
    out = {}
    for key, by_date in _grouped(rows, lib).items():
        forward = any(str(r.get("status") or "").lower() in media_guard.FORWARD_STATUSES
                      for rws in by_date.values() for r in rws)
        if len(by_date) > 1 and forward:
            out[key] = by_date
    return out


# ---------------------------------------------------------------------------
# B5 — the repeats the sweep is RIGHT to refuse, and WRONG to swallow
#
# Measured live on 2026-09-05 with the guard armed and this sweep running nightly:
#   zanshin  5 photo repeats, dates_fixed 0, approved_left 5, small library
#            -41.jpg sits on 09-03 (LIVE), 09-08 (approved) and 09-09 (approved):
#            the SAME photo three times inside seven days
#   lasso   28 photo repeats, dates_fixed 0, small library
#
# The stage-time guard works (zero repeats among rows created after it deployed).
# What is left is the rows it cannot see, and this sweep is correctly forbidden
# from fixing them: an APPROVED row's media is never swapped, because the gym
# approved that exact card, and a small library is never given fabricated media.
# Both refusals are right. Recording them as an integer on a stdout table is not:
# approved_left has been counted since the job was written and has never reached a
# person, so Pete's photos have repeated for weeks with the machine "working".
#
# This says it out loud, once, with the dates. It changes NO write behavior: the
# approved rows and the small library are still left exactly alone.
#
# Flag: config.media_repeat_report_enabled() (AGENT_MEDIA_REPEAT_REPORT, default
# OFF -- a new alert is a new capability).
_NEAR_DAYS = 7          # B5's own bar: never the same photo twice inside 7 days


def unfixable_report(result, *, near_days=_NEAR_DAYS):
    """One client-readable sentence per gym whose repeats this sweep left in place,
    or "" when there is nothing to say. PURE: no I/O, no kv, no alerting.

    Repeats inside `near_days` are called out separately because they are the ones a
    follower actually notices -- the same photo twice in a week reads as a bot."""
    detail = [d for d in (result or {}).get("detail") or []
              if "APPROVED duplicate" in d or "LIVE row also carries it" in d
              or "no unused photo left" in d]
    if not detail:
        return ""
    gym = (result or {}).get("gym", "")
    approved = int((result or {}).get("approved_left") or 0)
    small = bool((result or {}).get("small_library"))
    photos = int((result or {}).get("photos_repeated") or 0)

    # Group the left-behind dates per photo so "twice inside a week" is provable.
    by_photo = {}
    for line in detail:
        head = line.split(":", 1)[0].strip()
        parts = head.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        key, pd = parts[0], parts[1]
        if len(pd) == 10 and pd[4] == "-":
            by_photo.setdefault(key, set()).add(pd)
    near = []
    for key, dates in by_photo.items():
        ds = sorted(dates)
        best = None
        for i in range(1, len(ds)):
            try:
                gap = (date.fromisoformat(ds[i]) - date.fromisoformat(ds[i - 1])).days
            except ValueError:
                continue
            # The CLOSEST pair is the one a follower actually sees, so report that
            # one rather than whichever happened to come first in date order.
            if gap <= near_days and (best is None or gap < best[0]):
                best = (gap, ds[i - 1], ds[i])
        if best is not None:
            near.append(f"{key} on {best[1]} and {best[2]} ({best[0]} days apart)")

    lines = [f"{gym}: {photos} photo(s) repeat across different days of the book and "
             f"this sweep left them in place ON PURPOSE."]
    if approved:
        lines.append(f"{approved} of them sit on APPROVED posts. Echo will not change "
                     "a card the gym already approved, so a person has to decide: "
                     "re-approve a swap, or leave the repeat.")
    if small:
        # NEVER tell a gym to connect a folder it already connected (John Weeks /
        # Tough Temple: a full Drive folder and this line still asked for photos).
        # drive_pool is what sweep_gym measured, not a guess: assets the sweep could
        # have used but did not reach.
        pool = int((result or {}).get("drive_pool") or 0)
        if pool:
            lines.append(f"This gym's uploaded photos are all on the book, but its "
                         f"connected Drive folder holds {pool} unused item(s) the "
                         "nightly sweep cannot reach yet. Nothing more is needed from "
                         "the gym.")
        else:
            lines.append("This gym has fewer usable photos than it has posting days, "
                         "so there is nothing fresh to swap in. Add photos (connect "
                         "the gym's Drive folder or upload in the portal).")
    if near:
        lines.append(f"Inside {near_days} days (the ones a follower notices): "
                     + "; ".join(sorted(near)[:4]) + ".")
    lines.append("Nothing was published, nothing was fabricated and no approval was "
                 "changed.")
    return " ".join(lines)


def report_unfixable(result, *, today_iso="", alert_fn=None, db=None,
                     near_days=_NEAR_DAYS):
    """Raise `unfixable_report` once per gym per month, and AGAIN whenever the count
    gets worse. Durable-or-silent (the repo's alert-dedup convention: a process with
    an ephemeral kv would re-alert every night, so it stays quiet instead). Returns
    the text it alerted, else "". Never raises: reporting may not break a sweep."""
    if not config.media_repeat_report_enabled():
        return ""
    text = unfixable_report(result, near_days=near_days)
    if not text:
        return ""
    gym = (result or {}).get("gym", "")
    try:
        _db = db
        if _db is None:
            from agent import db as _dbmod
            _db = _dbmod
        if hasattr(_db, "kv_is_durable") and not _db.kv_is_durable():
            return ""
        key = f"media_repeat_left_{gym}_{str(today_iso or '')[:7]}"
        seen = _db.kv_get(key, "") or ""
        now_n = int((result or {}).get("photos_repeated") or 0)
        if seen:
            try:
                if now_n <= int(seen):
                    return ""            # same or better: already said this month
            except (TypeError, ValueError):
                return ""
        _db.kv_set(key, str(now_n))
    except Exception:  # noqa: BLE001
        return ""
    try:
        (alert_fn or _alert)(text)
    except Exception:  # noqa: BLE001
        return ""
    return text


def _alert(text):
    from agent import ops_alerts
    ops_alerts.alert(text)


def sweep_gym(base, store, *, apply=False, horizon=62, today=None):
    today = today or date.today()
    win = config.media_repeat_window_days()
    start = (today - timedelta(days=win)).isoformat()
    end = (today + timedelta(days=horizon)).isoformat()
    try:
        rows = store.rows_in_range(base, start, end) or []
    except Exception as exc:  # noqa: BLE001
        _log(f"{base}: read failed ({type(exc).__name__}); skipped")
        return {"gym": base, "error": type(exc).__name__}
    lib = _lib_dir(base)
    dupes = cross_day_repeats(rows, lib)
    result = {"gym": base, "photos_repeated": len(dupes), "dates_fixed": 0,
              "rows_repointed": 0, "stories_reburned": 0, "approved_left": 0,
              "small_library": False, "drive_pool": 0, "detail": []}
    if not dupes:
        return result

    # Full occupancy state for the fresh pick (every raw key on the book/window).
    state = {}
    for key, by_date in _grouped(rows, lib).items():
        for pd in by_date:
            state.setdefault(key, set()).add((pd, "x"))
    # The same occupancy in the DRIVE ASSET id space, so a Drive replacement is never
    # an asset already sitting on another day (and never the same clip twice in a run).
    asset_state = _asset_state(rows)
    pool_dry = None                  # dry-run only: the pool size, read once per gym

    today_iso = today.isoformat()
    for key, by_date in sorted(dupes.items()):
        owner = _owner_date(by_date)
        for pd in sorted(by_date):
            if pd == owner:
                continue
            group = by_date[pd]
            statuses = {str(r.get("status") or "").lower() for r in group}
            if statuses & set(UNTOUCHABLE):
                # live/publishing on a non-owner date: report only, never touch.
                result["detail"].append(f"{key} {pd}: LIVE row also carries it (left)")
                continue
            if "approved" in statuses:
                result["approved_left"] += 1
                result["detail"].append(
                    f"{key} {pd}: APPROVED duplicate (left; the gym approved this card)")
                continue
            if pd < today_iso:
                result["detail"].append(f"{key} {pd}: past-dated (left for the "
                                        "expired sweep)")
                continue
            fixable = [r for r in group
                       if str(r.get("status") or "").lower() in FIXABLE]
            if not fixable:
                continue
            new_key, new_path = _fresh_photo(lib, state, exclude={key})
            if not new_key:
                # THE LOCAL LIBRARY IS EXHAUSTED -- but the gym's CONNECTED DRIVE POOL
                # may not be (Tough Temple: every still on the book, 57 unused clips in
                # Drive). Ask the portal swap's engine before calling this a small
                # library. Flag OFF => the old behavior, byte for byte.
                if config.media_repeat_sweep_drive_enabled():
                    if not apply:
                        # A dry run places nothing, so the pool never changes: read it
                        # ONCE per gym, not once per repeated date.
                        if pool_dry is None:
                            pool_dry = _drive_candidate_count(base, asset_state)
                        n = pool_dry
                        if n:
                            result["dates_fixed"] += 1
                            result["rows_repointed"] += len(fixable)
                            result["detail"].append(
                                f"{key} {pd}: -> connected Drive pool "
                                f"({n} asset(s) available) [dry-run]")
                            continue
                    elif _swap_from_drive_pool(base, store, fixable, state=state,
                                               asset_state=asset_state, rows=rows,
                                               result=result, key=key, pd=pd):
                        result["dates_fixed"] += 1
                        continue
                result["small_library"] = True
                result["detail"].append(f"{key} {pd}: no unused photo left "
                                        "(small library; left with spacing)")
                continue
            result["detail"].append(
                f"{key} {pd}: -> {new_key} ({len(fixable)} row(s))"
                + ("" if apply else " [dry-run]"))
            if not apply:
                result["dates_fixed"] += 1
                result["rows_repointed"] += len(fixable)
                state.setdefault(new_key, set()).add((pd, "x"))
                continue
            hosted = ""
            try:
                from agent import media_host
                if config.hosting_enabled():
                    hosted = media_host.host_media(new_path, f"{base}_ig") or ""
            except Exception as exc:  # noqa: BLE001
                _log(f"{base}: hosting failed for {new_key} ({type(exc).__name__})")
            if not hosted:
                result["detail"].append(f"{key} {pd}: hosting unavailable; left")
                continue
            # FEED AUTOFIT PARITY: the original feeds shipped through the autofit
            # reframe; give the replacement the same treatment (raw on any failure).
            feed_url = hosted
            if config.feed_autofit_enabled():
                try:
                    from agent import feed_image, media_host
                    asset = feed_image.get_or_make_feed_image(new_path, lib,
                                                              logger=_log)
                    if asset:
                        reframed = media_host.host_media(asset, f"{base}_ig")
                        if reframed:
                            feed_url = reframed
                except Exception:  # noqa: BLE001 - keep the raw hosted photo
                    pass
            fixed_any = False
            for r in fixable:
                rid = r.get("id")
                fmt = str(r.get("format") or "").lower()
                target_url = feed_url
                src_url = None
                if fmt == "story":
                    if config.story_format_enabled():
                        burned = _reburn_story(base, r, new_path, lib)
                        if not burned:
                            result["detail"].append(
                                f"{key} {pd}: story re-burn failed; left")
                            continue
                        target_url = burned
                        result["stories_reburned"] += 1
                    else:
                        target_url = hosted            # raw photo, never the square
                    if config.story_source_media_enabled():
                        src_url = hosted
                # swap_media is status-guarded server-side (pending/coach_review
                # only), so an approval or publish landing mid-sweep wins the race.
                if store.swap_media(base, rid, target_url,
                                    source_media_url=src_url) is not None:
                    result["rows_repointed"] += 1
                    fixed_any = True
            if fixed_any:
                result["dates_fixed"] += 1
                state.setdefault(new_key, set()).add((pd, "x"))
                try:
                    from agent import dam, rotation
                    rotation.record_served(f"{base}_ig", dam.rotation_key(new_path),
                                           "", pd)
                except Exception:  # noqa: BLE001
                    pass
    if result["small_library"]:
        # MEASURE what the sweep could not reach, so the report tells the truth about
        # WHY (a genuinely thin library, or a full Drive folder behind an unarmed lane)
        # instead of asking a connected gym for photos it already gave us.
        result["drive_pool"] = _drive_candidate_count(base, asset_state)
        if apply:
            media_guard.alert_small_library(base, today_iso, _log)
    return result


def run(gyms, *, apply=False, horizon=62):
    store = SupabaseCalendarStore()
    if not gyms:
        from agent.calendar_autopublish import client_gym_bases
        gyms = client_gym_bases() + ["lasso"]
    results = []
    today_iso = date.today().isoformat()
    for base in gyms:
        r = sweep_gym(base, store, apply=apply, horizon=horizon)
        # B5: say out loud what this sweep deliberately did NOT fix. Flag-gated
        # (AGENT_MEDIA_REPEAT_REPORT, default OFF) and kv-deduped per gym per month.
        # Only in APPLY mode: a dry run must stay a dry run, including its alerts.
        if apply:
            try:
                report_unfixable(r, today_iso=today_iso)
            except Exception as exc:  # noqa: BLE001 - reporting never breaks a sweep
                _log(f"{base}: unfixable report failed ({type(exc).__name__})")
        results.append(r)
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n=== media_repeat_sweep [{mode}] ===")
    print(f"{'gym':<14}{'photos':>7}{'dates_fixed':>12}{'rows':>6}"
          f"{'reburned':>9}{'approved_left':>14}{'small_lib':>10}")
    for r in results:
        if r.get("error"):
            print(f"{r['gym']:<14} ERROR {r['error']}")
            continue
        print(f"{r['gym']:<14}{r['photos_repeated']:>7}{r['dates_fixed']:>12}"
              f"{r['rows_repointed']:>6}{r['stories_reburned']:>9}"
              f"{r['approved_left']:>14}{str(r['small_library']):>10}")
    for r in results:
        for line in r.get("detail") or []:
            print(f"  {r['gym']}: {line}")
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--apply", action="store_true", help="make the writes")
    p.add_argument("--horizon", type=int, default=62)
    p.add_argument("gyms", nargs="*")
    args = p.parse_args(argv)
    results = run(args.gyms, apply=args.apply, horizon=args.horizon)
    if any(r.get("error") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
