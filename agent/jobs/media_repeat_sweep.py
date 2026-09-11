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
import re
import sys
from datetime import date, timedelta

from agent import config, media_guard
from agent.portal_calendar_store import SupabaseCalendarStore

FIXABLE = ("pending", "coach_review")
UNTOUCHABLE = ("published", "publishing")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# At most this many dates per gym per night may go through the Drive fallback, which
# downloads and may transcode real media. See sweep_gym's NIGHTLY BUDGET note.
DRIVE_FALLBACK_MAX_PER_GYM = 5


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


# The suffixes a duplicate file picks up on its way into a library: "IMG_6771 (1).jpg"
# from a browser/Finder copy, "IMG_6771-copy.jpg", "IMG_6771_1.jpg" from a re-upload or
# a Drive sync collision.
#
# INDEPENDENT AUDIT ROUND 2 (2026-09-11, CRITICAL). _cluster_key only ever reaches
# dam.rotation_key's dupe_group when the sidecar was MARKED, and dam.mark_near_dupes is
# called in exactly one place -- intake_onboard.py, once at onboarding. Nothing re-marks
# a library after a portal upload or a Drive sync, so for the media that actually causes
# repeats this degraded to the case-folded stem, which catches only "IMG_6771.JPG" vs
# "IMG_6771.jpg". Measured: 'IMG_6771 (1).jpg', 'IMG_6771-copy.jpg' and 'IMG_6771_1.jpg'
# were all NOT blocked -- and "IMG_6771 (1).jpg" is the exact example the near-dupe guard
# was written for. _fresh_photo shares this function, so the hole was in the DEFAULT
# (flag OFF) lane too: the sweep could replace a repeat with a byte-identical copy of
# itself and report the date fixed.
def _content_print(lib, key):
    """A library file's CONTENT identity (size + sha256, cached by path/mtime/size via
    media_guard), or None when it cannot be read.

    THE FILENAME HEURISTIC IS GONE (independent audit rounds 2-5). Three rounds went into
    spotting a duplicate from its NAME, and every version was wrong in one of two
    directions: too narrow and "IMG_6771 (1).jpg" slipped through as fresh; too wide and
    "gym.jpg" + "gym-1.jpg".."gym-5.jpg" collapsed into one cluster, so _fresh_photo
    returned None for a gym with five usable stills and the sweep declared "small
    library" -- on the DEFAULT path, where origin/main had swapped the repeat. Round 5
    found BOTH failure modes still live, either side of a threshold.

    Names cannot answer this question. Bytes can, and _is_real_image already opens every
    candidate, so the cost is a cached hash. A RE-ENCODED near-dupe still slips through;
    that is the honest limit of a content check, and it is strictly better than the
    name-guessing it replaces, which could starve a whole library."""
    try:
        path = os.path.join(lib, key)
        h = media_guard._library_hash(path)
        return (os.path.getsize(path), h) if h else None
    except OSError:
        return None


def _content_prints(lib, keys, lib_names, images_only=False):
    """The content identities of `keys` that are real files in this library.

    images_only skips keys _fresh_photo could never return anyway (videos, burned
    cards): hashing a booked 180 MB mp4 to seed an image-only pick is pure cost."""
    out = set()
    for k in keys:
        if k not in lib_names:
            continue
        if images_only and os.path.splitext(k)[1].lower() not in _IMG_EXTS:
            continue
        fp = _content_print(lib, k)
        if fp:
            out.add(fp)
    return out


def _cluster_key(lib, key):
    """The near-dupe identity of a library file: dam.rotation_key when the gym is
    vision-clustered, else the case-folded stem (catches IMG_6771.JPG vs IMG_6771.jpg --
    the same photo uploaded twice).

    UNCHANGED FROM origin/main on purpose. Everything this briefly tried to infer from a
    filename now lives in _content_print, behind AGENT_MEDIA_DEDUPE_BY_CONTENT, so with
    both new flags off this module behaves exactly as it always has."""
    try:
        from agent import dam
        rk = dam.rotation_key(os.path.join(lib, key))
        if rk and rk != key:
            return rk
    except Exception:  # noqa: BLE001
        pass
    return os.path.splitext(key)[0].lower()


def _fresh_photo(lib, state, exclude):
    """A genuinely unused, VALIDATED library image (never on any book/window date, never
    in exclude, never a NEAR-DUPE of one, never BYTE-IDENTICAL to one), deterministic.
    (None, None) when nothing unused."""
    used = set(state.keys()) | set(exclude)
    lib_names = media_guard.library_keys(lib)
    used_clusters = {_cluster_key(lib, k) for k in used if k in lib_names}
    # BEHIND ITS OWN FLAG (independent audit round 6): this changes the DEFAULT path --
    # where the only spare is a byte-identical copy, OFF swaps it in and calls the date
    # fixed, ON leaves it and reports a small library. More truthful, but a behavior
    # change, and CLAUDE.md is explicit that a new capability ships OFF.
    used_prints = (_content_prints(lib, used, lib_names, images_only=True)
                   if config.media_dedupe_by_content_enabled() else set())
    for key in sorted(lib_names):
        if key in used:
            continue
        if os.path.splitext(key)[1].lower() not in _IMG_EXTS:
            continue                       # image swaps only; videos need their lanes
        if _cluster_key(lib, key) in used_clusters:
            continue                       # a near-dupe of a used photo repeats visually
        # BYTE-IDENTICAL to a photo already on the book, whatever it is called:
        # "IMG_6771 (1).jpg" beside "IMG_6771.jpg" is the same picture to a follower,
        # and no filename rule could tell those apart without starving real sequences.
        if _content_print(lib, key) in used_prints:
            continue
        path = os.path.join(lib, key)
        if os.path.isfile(path) and _is_real_image(path):
            return key, path
    return None, None


def _asset_state(rows):
    """{drive_asset_id: {(iso_date, 'x'), ...}} for the Drive assets on the rows this
    sweep read. The Drive pool is keyed by asset id, a different id space from library
    basenames, so blocking "already on the book" needs its own map
    (media_guard.row_asset_key). Grows as the sweep places assets, so one run never
    hands the same clip to two dates.

    SCOPE, stated honestly (independent audit round 2, 2026-09-11): this is the sweep's
    own window only -- today-repeat_window .. today+horizon, 62 days by default. Round 1
    flagged that an asset staged BEYOND that horizon is invisible, and round 2 caught the
    attempted fix doing nothing: media_guard.book_state(start=today, days=1) reads
    BACKWARDS from today, so it never saw a forward row either, while costing an extra
    list_month per gym per night ON THE FLAG-OFF PATH. It is reverted rather than
    widened, because the gap is theoretical: plan_horizon clamps every build to about one
    month out, so there are no rows past +62 days to collide with. If that clamp is ever
    raised, widen this read (with a forward span) and gate it on the flag."""
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


def _blocked_book_state(base, state, current_key):
    """The book state handed to the picker, WIDENED to every library file that is a
    NEAR DUPE of something already on the book.

    Independent audit 2026-09-11 (CRITICAL). `_fresh_photo` refuses a candidate for two
    different reasons: nothing unused is left, OR everything unused is a `_cluster_key`
    near-dupe of a photo already placed. This fallback treats both as "local library
    exhausted" and hands off to media_swap, whose `local_candidates` blocks only by
    EXACT BASENAME and by the SERVED ledger -- so a cluster sibling sitting on a PENDING
    row (on the book, never served) is blocked by neither, and the picker returns the
    very file `_fresh_photo` just refused. The client complaint is "these look like the
    same image"; swapping IMG_6771.jpg for IMG_6771 (1).jpg and reporting the date fixed
    is that complaint, made silent. Never raises; a library read failure returns the
    state unchanged (the exact-basename block still applies)."""
    blocked = dict(state or {})
    try:
        lib = _lib_dir(base)
        lib_names = media_guard.library_keys(lib)
        if not lib_names:
            return blocked
        seeds = (set(state or {}) | {current_key}) & lib_names
        used_clusters = {_cluster_key(lib, k) for k in seeds}
        used_prints = (_content_prints(lib, seeds, lib_names, images_only=True)
                       if config.media_dedupe_by_content_enabled() else set())
        if not (used_clusters or used_prints):
            return blocked
        for name in lib_names:
            if name in blocked:
                continue
            # `used_prints and ...` SHORT-CIRCUITS (independent audit round 8, MAJOR):
            # with the content flag OFF used_prints is empty, so the hash could not
            # change the answer -- but it still ran, sha256ing every library file
            # including booked video. Measured 83.9 MB read for a library of one jpg and
            # two clips, once per repeated date, inside the nightly draft run.
            if (_cluster_key(lib, name) in used_clusters
                    or (used_prints and _content_print(lib, name) in used_prints)):
                blocked.setdefault(name, set()).add(("near-dupe", "x"))
    except Exception as exc:  # noqa: BLE001 - never let this block a swap entirely
        _log(f"{base}: near-dupe widening skipped ({type(exc).__name__})")
    return blocked


def _restore_rows(base, store, undo):
    """Put back the rows a partially failed swap already re-pointed, so a post never
    ships half on the new clip and half on the repeat. Returns the row ids it could NOT
    restore (never raises).

    Two things round 2 of the audit caught here:

    * swap_media returning None is a REFUSAL, not a success -- it is the same
      status guard ("this row was approved or went live") that caused the rollback in
      the first place, so it is the likeliest outcome, and swallowing it let the caller
      log "rolling back N rows so the post is never half swapped" without keeping that
      promise. A refusal is now named, loudly, as needing a person.
    * `source_media_url=None` does not CLEAR the column -- portal_calendar_store only
      writes it when the value is not None. A row whose original had none (older story
      rows, and any row whose forward variant set one) kept the NEW clip's
      source_media_url on top of the OLD image_url. media_guard.row_media_key reads
      source_media_url FIRST, so from then on every guard and every future sweep keys
      that row by media it does not carry: the repeat goes invisible to Echo while the
      gym still sees it. An absent original is now written as "" explicitly."""
    stuck = []
    for rid, before in undo:
        try:
            done = store.swap_media(
                base, rid, before["image_url"],
                source_media_url=(before.get("source_media_url") or ""),
                extra_fields=before.get("extra_fields") or {})
        except Exception as exc:  # noqa: BLE001
            _log(f"{base}: ROLLBACK FAILED for row {rid} ({type(exc).__name__})")
            done = None
        if done is None:
            stuck.append(str(rid))
    if stuck:
        msg = (f"{base}: ROLLBACK REFUSED for row(s) {', '.join(stuck)} (approved or "
               "live since the read). They carry the NEW media while a sibling carries "
               "the old one: a MIXED POST that needs a person.")
        _log(msg)
        try:
            from agent import ops_alerts
            ops_alerts.alert(msg)
        except Exception:  # noqa: BLE001 - alerting never breaks a sweep
            pass
    return stuck


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

    Returns the pick's SOURCE ("drive" or "local") when at least one row was
    re-pointed, else "" -- the caller needs the distinction because this function
    legitimately returns a LOCAL pick (pick_replacement draws from both pools, and a
    local VIDEO is a fine answer here since _fresh_photo only ever looked at images).
    Counting those as Drive coverage is what made unfixable_report tell a gym its
    "connected Drive folder covered 1 day(s)" on a night the Drive lane did nothing
    (independent audit round 5). Never raises."""
    ordered = _feed_first(fixable)
    row, siblings = ordered[0], ordered[1:]
    try:
        from agent import media_swap
        pick = (picker or media_swap.pick_replacement)(
            base, row, store=store, library_path=_lib_dir(base),
            book_state=_blocked_book_state(base, state, key),
            asset_state=asset_state, siblings=siblings)
    except Exception as exc:  # noqa: BLE001 - a picker failure is "no Drive swap"
        _log(f"{base}: Drive replacement failed for {key} {pd} ({type(exc).__name__})")
        return ""
    # A picker that returns a non-dict must not raise out of sweep_gym and skip every
    # remaining gym for the night (run() walks gyms in one unguarded loop).
    if not isinstance(pick, dict) or not pick.get("ok"):
        return ""
    from agent import media_swap
    variants = pick.get("siblings") or {}
    # ALL OR NOTHING, exactly like the portal swap: a sibling the picker could not
    # shape means this date is left alone rather than half re-pointed.
    missing = [str(s.get("id")) for s in siblings
               if not (variants.get(str(s.get("id"))) or {}).get("ok")]
    if missing:
        _log(f"{base}: {key} {pd}: Drive swap skipped, a sibling row could not be "
             "shaped from the same clip")
        return ""

    # ALL OR NOTHING AT WRITE TIME TOO (independent audit 2026-09-11, MAJOR). The
    # picker's own all-or-nothing only covers SHAPING. Each swap_media below is a
    # separate network write, and a failure on one of them used to leave the rest
    # re-pointed: the IG feed and FB mirror on the fresh clip, the paired story still
    # on the repeat with a stale source_media_url -- a mixed post, counted as fixed.
    # A row that fails now rolls the already-written rows back to exactly what they
    # carried before, and the date is left for the next run.
    targets = [(row, pick)] + [(s, variants[str(s.get("id"))]) for s in siblings]
    swapped, undo, stories = [], [], 0
    failed_row = None
    for target, var in targets:
        rid = target.get("id")
        before = {"image_url": target.get("image_url") or "",
                  "source_media_url": target.get("source_media_url"),
                  "extra_fields": {"thumbnail_url": target.get("thumbnail_url") or None,
                                   "source_media_asset_id":
                                       target.get("source_media_asset_id") or None}}
        try:
            # "" not None (independent audit round 3, MAJOR): portal_calendar_store
            # writes this column only when the value is not None, so a variant that
            # carries no source (a story with AGENT_STORY_SOURCE_MEDIA off) used to
            # STRAND the row's previous one. media_guard.row_media_key reads
            # source_media_url FIRST, so that row would key as the old photo forever --
            # invisible to the client, and blocking that photo from every future pick.
            # _restore_rows got this fix in round 2; the forward path did not.
            _src = var.get("source_media_url")
            if _src is None and not (target.get("source_media_url") or ""):
                _src = None          # nothing to clear; never create the column
            else:
                _src = _src or ""    # clear a stale one rather than strand it
            done = store.swap_media(base, rid, var["image_url"],
                                    source_media_url=_src,
                                    extra_fields=media_swap.swap_fields(var))
        except Exception as exc:  # noqa: BLE001
            _log(f"{base}: swap_media failed for {rid} ({type(exc).__name__})")
            done = None
        if done is None:
            # A row that matched nothing was approved or went live between the read
            # and the write (swap_media is status-guarded server-side). Either way the
            # post can no longer move as one post.
            failed_row = str(rid)
            break
        swapped.append(str(rid))
        undo.append((rid, before))
        if (str(target.get("format") or "").lower() == "story"
                and config.story_format_enabled()):
            stories += 1
    if failed_row is not None:
        _log(f"{base}: {key} {pd}: row {failed_row} could not be re-pointed; rolling "
             f"back {len(undo)} sibling row(s) so the post is never half swapped")
        _restore_rows(base, store, undo)
        return ""
    if not swapped:
        return ""
    result["rows_repointed"] += len(swapped)
    result["stories_reburned"] += stories

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
    return "drive" if pick.get("source") == "drive" else "local"


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
              or "no unused photo left" in d or "hosting unavailable" in d
              or "past-dated" in d or "story re-burn failed" in d
              or "kept the repeat" in d]
    capped = int((result or {}).get("budget_capped") or 0)
    if not detail and not capped:
        return ""
    gym = (result or {}).get("gym", "")
    approved = int((result or {}).get("approved_left") or 0)
    small = bool((result or {}).get("small_library"))
    if not detail:
        small = False            # budget-only: nothing was left for lack of media
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
                     "approve a swap, or leave the repeat.")
    if small:
        # NEVER tell a gym to connect a folder it already connected (John Weeks /
        # Tough Temple: a full Drive folder and this line still asked for photos).
        # drive_pool is what sweep_gym measured, not a guess: assets the sweep could
        # have used but did not reach.
        # The pool as first SEEN, not what is left after the run drained it
        # (independent audit round 2): a run that used the last asset left drive_pool
        # at 0 and fell through to "Add photos", the exact sentence this branch exists
        # to stop sending a gym whose folder is full.
        pool = int((result or {}).get("drive_pool_seen")
                   or (result or {}).get("drive_pool") or 0)
        armed = bool((result or {}).get("drive_armed"))
        if pool and armed:
            # ARMED and still stuck. Say which of the three it actually was, because
            # round 3 caught this branch claiming "could not prepare one" on a run that
            # had prepared five. Nothing here may be a guess.
            # drive_fixed, NOT dates_fixed: the latter mixes in LOCAL swaps, so a run
            # where the Drive lane failed every attempt still credited it (round 4).
            # And print what is LEFT (drive_pool), not the pre-run count we branch on.
            fixed = int((result or {}).get("drive_fixed") or 0)
            left = int((result or {}).get("drive_pool") or 0)
            if fixed:
                lines.append(f"This gym's uploaded photos are all on the book. Its "
                             f"connected Drive folder covered {fixed} day(s) tonight "
                             f"and holds {left} unused item(s) for the rest. Nothing "
                             "more is needed from the gym.")
            else:
                lines.append(f"This gym's uploaded photos are all on the book. Its "
                             f"connected Drive folder holds {pool} unused item(s) and "
                             "tonight's run could not prepare one; it retries on the "
                             "next run. Nothing more is needed from the gym.")
        elif pool:
            lines.append(f"This gym's uploaded photos are all on the book, but its "
                         f"connected Drive folder holds {pool} unused item(s) the "
                         "nightly sweep cannot reach yet. Nothing more is needed from "
                         "the gym.")
        else:
            lines.append("This gym has fewer usable photos than it has posting days, "
                         "so there is nothing fresh to swap in. Add photos (connect "
                         "the gym's Drive folder or upload in the portal).")
    if capped:
        lines.append(f"{capped} more day(s) are queued behind tonight's per gym limit "
                     f"of {DRIVE_FALLBACK_MAX_PER_GYM} and clear on the next runs. "
                     "Nothing more is needed from the gym.")
    # Named, not silent (independent audit round 6). AGENT_HOSTING_ENABLED defaults
    # FALSE, so on a default-posture box EVERY swap dies at hosting and this report used
    # to come back empty while the repeats stood.
    if any("hosting unavailable" in d for d in detail):
        lines.append("Some of these could not be moved because media hosting was "
                     "unavailable on this run. That is ours to fix, not the gym's.")
    if any("story re-burn failed" in d or "kept the repeat" in d for d in detail):
        lines.append("On at least one day the post could not be moved as a whole, so "
                     "part of it still carries the old photo. A person needs to look "
                     "at that day.")
    if any("past-dated" in d for d in detail):
        lines.append("Some sit on dates that have already passed; the expired sweep "
                     "owns those, not this one.")
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
              "small_library": False, "drive_pool": 0, "drive_pool_seen": 0,
              "drive_armed": False, "budget_capped": 0, "drive_fixed": 0,
              "mixed_posts": 0, "detail": []}
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
    # NIGHTLY BUDGET (independent audit round 2). media_swap.pick_replacement is an
    # HTTP-request-sized engine: real Drive downloads, a probe and possibly a transcode,
    # with a 75s deadline tuned for one portal click. The sweep runs in the same process
    # as the draft run and this module's own notes record a gym with 28 repeats, so an
    # uncapped fallback is ~35 minutes on one gym. Bounded per gym per night; the rest
    # are reported and picked up by the next run.
    drive_fixes_left = DRIVE_FALLBACK_MAX_PER_GYM
    budget_capped = 0                # dates the cap deferred to the next run
    drive_fixed = 0                  # dates the DRIVE lane covered (not local swaps)
    if config.media_repeat_sweep_drive_enabled():
        # BEFORE the loop, so the report can tell a gym with a FULL folder apart from
        # one with an empty folder even after this run drains the pool (independent
        # audit round 3, CRITICAL: drive_pool_seen was written nowhere and always
        # equalled the post-run count, so a run that used the last asset reported 0 and
        # sent the exact "Add photos, connect your Drive folder" line it exists to stop).
        result["drive_armed"] = True
        result["drive_pool_seen"] = _drive_candidate_count(base, asset_state)

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
                # library. Flag OFF => the old lane (see AGENT_MEDIA_DEDUPE_BY_CONTENT for the one
                    # unflagged delta: a stale source_media_url is now cleared).
                if config.media_repeat_sweep_drive_enabled():
                    if drive_fixes_left <= 0:
                        # BUDGET SPENT is not a small library (independent audit round
                        # 3, MAJOR): falling through used to set small_library, fire the
                        # small-library ops alert, and tell the gym "tonight's run could
                        # not prepare one" while the run had in fact prepared five.
                        budget_capped += 1
                        result["detail"].append(
                            f"{key} {pd}: Drive fallback budget for tonight is spent "
                            f"({DRIVE_FALLBACK_MAX_PER_GYM}/gym); next run picks it up")
                        continue
                    if not apply:
                        # A dry run places nothing, so the pool is read ONCE per gym --
                        # but it must still be SPENT DOWN as it reports (independent
                        # audit 2026-09-11, CRITICAL). Reusing the same count for every
                        # repeated date said all three of John's days were fixable from
                        # a pool holding one asset; apply would fix one. The dry run is
                        # the instrument we verify against a client's gym, so it has to
                        # predict what apply will actually do.
                        if pool_dry is None:
                            pool_dry = _drive_candidate_count(base, asset_state)
                        if pool_dry > 0:
                            pool_dry -= 1
                            drive_fixes_left -= 1    # the SAME budget apply spends
                            drive_fixed += 1         # parity with apply's counter
                            result["stories_reburned"] += sum(
                                1 for r in fixable
                                if str(r.get("format") or "").lower() == "story"
                                and config.story_format_enabled())
                            result["dates_fixed"] += 1
                            result["rows_repointed"] += len(fixable)
                            result["detail"].append(
                                f"{key} {pd}: -> connected Drive pool has an asset "
                                f"({pool_dry} left after this); DELIVERABILITY NOT "
                                "TESTED in a dry run (no download, probe or host) "
                                "[dry-run]")
                            continue
                    else:
                        # EVERY ATTEMPT SPENDS IT (independent audit round 4, MAJOR).
                        # Decrementing only on success meant a gym whose picker keeps
                        # failing -- a hosting outage, the 75s deadline, no fresh media,
                        # exactly what the deadline exists for -- called
                        # pick_replacement once per repeated date with real Drive
                        # downloads, uncapped, inside the nightly draft run, and
                        # reported budget_capped 0 so nothing said why.
                        drive_fixes_left -= 1
                        _src = _swap_from_drive_pool(
                            base, store, fixable, state=state, asset_state=asset_state,
                            rows=rows, result=result, key=key, pd=pd)
                        if _src:
                            # ONLY a real Drive pick counts as Drive coverage: the same
                            # call can return a LOCAL video, and crediting the gym's
                            # Drive folder for that is the falsehood round 5 caught.
                            if _src == "drive":
                                drive_fixed += 1
                            result["dates_fixed"] += 1
                            continue
                result["small_library"] = True
                result["detail"].append(f"{key} {pd}: no unused photo left "
                                        "(small library; left with spacing)")
                continue
            if not apply:
                result["detail"].append(
                    f"{key} {pd}: -> {new_key} ({len(fixable)} row(s)) [dry-run]")
                result["dates_fixed"] += 1
                result["rows_repointed"] += len(fixable)
                state.setdefault(new_key, set()).add((pd, "x"))
                continue
            # The "-> new_key" line is appended only once a write has LANDED
            # (independent audit round 6): announcing it here and then appending
            # "hosting unavailable; left" below showed the operator a fix that never
            # happened -- and AGENT_HOSTING_ENABLED defaults FALSE, so on a default
            # box that is every single swap.
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
            swapped_local, burn_failed = [], False
            for r in fixable:
                rid = r.get("id")
                fmt = str(r.get("format") or "").lower()
                target_url = feed_url
                # "" not None, so a row that already carries a source_media_url (from a
                # portal edit-image swap, or an earlier armed Drive sweep) does not keep
                # pointing at media it no longer has. portal_calendar_store only writes
                # the column when the value is not None, and media_guard.row_media_key
                # reads source_media_url FIRST -- a stranded one makes the row key as a
                # photo it does not carry, invisible to every future guard and sweep,
                # and blocks that photo from every future pick. Round 3 fixed this on
                # the Drive path; the LOCAL path -- the one that runs with every flag
                # off -- still had it (independent audit round 7). Only sent when the
                # row actually has something to clear, so a gym whose schema predates
                # the column is never written to.
                src_url = "" if (r.get("source_media_url") or "") else None
                reburned = False
                if fmt == "story":
                    if config.story_format_enabled():
                        burned = _reburn_story(base, r, new_path, lib)
                        if not burned:
                            result["detail"].append(
                                f"{key} {pd}: story re-burn failed; left")
                            burn_failed = True
                            continue
                        target_url = burned
                        reburned = True
                    else:
                        target_url = hosted            # raw photo, never the square
                    if config.story_source_media_enabled():
                        src_url = hosted
                # swap_media is status-guarded server-side (pending/coach_review
                # only), so an approval or publish landing mid-sweep wins the race.
                if store.swap_media(base, rid, target_url,
                                    source_media_url=src_url) is not None:
                    result["rows_repointed"] += 1
                    swapped_local.append(str(rid))
                    # counted only once the WRITE landed (round 8 minor): a refused
                    # write left stories_reburned claiming a row that still carries
                    # the repeat.
                    if reburned:
                        result["stories_reburned"] += 1
            if swapped_local:
                # THE COUNT IS WHAT LANDED (independent audit round 7). This printed
                # len(fixable) unconditionally, so a failed story re-burn reported
                # "2 row(s)" on a post where the feed moved and the story kept the
                # repeat -- a MIXED POST, counted as a clean fix.
                _n = len(swapped_local)
                _of = (f"{_n} of {len(fixable)} row(s); the rest kept the repeat"
                       if _n < len(fixable) else f"{_n} row(s)")
                result["detail"].append(f"{key} {pd}: -> {new_key} ({_of})")
                if burn_failed or _n < len(fixable):
                    # NOT a fixed date (independent audit round 8, MAJOR). Round 7 made
                    # the DETAIL line honest but left the aggregate counter claiming a
                    # date where the feed moved and the story kept the repeat. The
                    # operator table and dates_fixed must agree with the detail.
                    _log(f"{base}: {key} {pd}: MIXED POST -- {_n} of {len(fixable)} "
                         "rows moved; a person needs to look at this date")
                    result["mixed_posts"] = int(result.get("mixed_posts") or 0) + 1
                else:
                    result["dates_fixed"] += 1
                state.setdefault(new_key, set()).add((pd, "x"))
                try:
                    from agent import dam, rotation
                    rotation.record_served(f"{base}_ig", dam.rotation_key(new_path),
                                           "", pd)
                except Exception:  # noqa: BLE001
                    pass
    result["budget_capped"] = budget_capped
    result["drive_fixed"] = drive_fixed
    if result["small_library"]:
        # MEASURE what the sweep could not reach, so the report tells the truth about
        # WHY (a genuinely thin library, or a full Drive folder behind an unarmed lane)
        # instead of asking a connected gym for photos it already gave us.
        #
        # BEHIND THE FLAG (independent audit 2026-09-11, MAJOR): this used to run on
        # every small-library gym regardless, which is a new Supabase read per gym per
        # night and new client-readable copy on the DEFAULT path. Flag OFF must be the
        # old behavior byte for byte -- CLAUDE.md's rule, and this module's own promise.
        if config.media_repeat_sweep_drive_enabled():
            # What is LEFT now. drive_pool_seen (captured before the loop) is what the
            # report branches on. In a dry run the running count is already exact.
            result["drive_pool"] = (pool_dry if pool_dry is not None
                                    else _drive_candidate_count(base, asset_state))
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
        # ONE GYM NEVER TAKES THE NIGHT (independent audit 2026-09-11). This loop was
        # unguarded, so anything escaping sweep_gym skipped every gym after it and the
        # ops alert blamed the whole sweep. A gym that raises is recorded and the rest
        # still get swept.
        try:
            r = sweep_gym(base, store, apply=apply, horizon=horizon)
        except Exception as exc:  # noqa: BLE001
            _log(f"{base}: sweep raised ({type(exc).__name__}); other gyms continue")
            results.append({"gym": base, "error": type(exc).__name__})
            continue
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
          f"{'reburned':>9}{'approved_left':>14}{'small_lib':>10}"
          f"{'pool_seen':>11}{'capped':>7}{'mixed':>6}")
    for r in results:
        if r.get("error"):
            print(f"{r['gym']:<14} ERROR {r['error']}")
            continue
        # drive_pool / capped so the operator can tell "thin library" apart from
        # "the Drive lane could not deliver tonight" without reading the detail lines.
        print(f"{r['gym']:<14}{r['photos_repeated']:>7}{r['dates_fixed']:>12}"
              f"{r['rows_repointed']:>6}{r['stories_reburned']:>9}"
              f"{r['approved_left']:>14}{str(r['small_library']):>10}"
              f"{r.get('drive_pool_seen', 0):>11}{r.get('budget_capped', 0):>7}"
              f"{r.get('mixed_posts', 0):>6}")
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
