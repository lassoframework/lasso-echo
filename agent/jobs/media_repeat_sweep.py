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
              "small_library": False, "detail": []}
    if not dupes:
        return result

    # Full occupancy state for the fresh pick (every raw key on the book/window).
    state = {}
    for key, by_date in _grouped(rows, lib).items():
        for pd in by_date:
            state.setdefault(key, set()).add((pd, "x"))

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
    if result["small_library"] and apply:
        media_guard.alert_small_library(base, today_iso, _log)
    return result


def run(gyms, *, apply=False, horizon=62):
    store = SupabaseCalendarStore()
    if not gyms:
        from agent.calendar_autopublish import client_gym_bases
        gyms = client_gym_bases() + ["lasso"]
    results = []
    for base in gyms:
        results.append(sweep_gym(base, store, apply=apply, horizon=horizon))
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
