"""
media_guard.py — ONE PHOTO, ONE DAY: the shared cross-day media guard.

THE RULE (Blake, 2026-08-31, after a client saw the same photo across different
weeks of their calendar): one photo must never appear on MULTIPLE DIFFERENT DAYS
of a gym's forward book — its pending / approved / publishing / coach_review
content_calendar rows — and must not be planned within the trailing repeat
window of a day it was PUBLISHED on. Same-DATE siblings (the FB mirror of a
feed, its paired story) are ONE post and legitimately share the photo: a row on
the SAME date never blocks.

WHY THE ROTATION LEDGER IS NOT ENOUGH: the served ledger is a heuristic that has
been cleared, pruned, and poisoned before; the calendar rows in Supabase are the
ground truth of what the client actually SEES. This module reads the BOOK.

SCOPE: instagram / facebook rows (and rows with an empty account, which the FB
mirror logic treats as instagram). GBP is deliberately OUT of scope — §3
(rotation.reuse_blocked) allows GBP to reuse an IG photo after 14 days by
design, and this guard must not break that.

Flag: config.media_cross_day_guard_enabled (AGENT_MEDIA_CROSS_DAY_GUARD,
DEFAULT ON — Blake ordered the rule, so it ships armed). The trailing published
window is config.media_repeat_window_days (default 30).

SMALL LIBRARIES never block a calendar: when every reusable photo already sits
on another day, callers fall back to spaced_choice (the photo whose other
appearances are FARTHEST from the target day) and post ONE kv-deduped info
digest (alert_small_library) in the existing needs-media language family.
Nothing here fabricates media, publishes, or weakens the approval gate.
"""

import os
from datetime import date, timedelta

from . import config

# Statuses that make a row part of the gym's live forward book: content a client
# can see (or has approved / is being published) that a NEW pick must not repeat.
FORWARD_STATUSES = ("pending", "approved", "publishing", "coach_review")

# Rebuild-wipeable statuses (mirrors portal_calendar_store._WIPEABLE_STATUSES):
# a month rebuild deletes these inside its span, so they do NOT survive it.
_WIPEABLE = ("pending", "draft", "queued")

# The guard's surface: the IG/FB feed+story book. '' rows mirror to instagram.
_GUARDED_ACCOUNTS = ("instagram", "ig", "facebook", "fb", "")


def enabled():
    return config.media_cross_day_guard_enabled()


def media_key(url_or_path):
    """The join key between a calendar row's image_url and a library creative:
    the basename, query string stripped (hosted client media keeps its library
    basename — same contract as client_month_run._url_basename)."""
    return (str(url_or_path or "")).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def row_media_key(row):
    """A calendar row's PHOTO IDENTITY: the raw source (source_media_url, kept on
    stories so an edited caption can re-burn) when present, else image_url. A
    story's image_url is a BURNED caption card with its own name — keying it by
    the raw source is what lets the guard see the actual photo."""
    row = row or {}
    return media_key(row.get("source_media_url") or row.get("image_url"))


# ---- feed-autofit reframe resolution -------------------------------------------------
# feed_image names a reframed feed card sha256(file bytes)[:12] + '__feed.jpg', so a
# row that shipped through autofit carries the REFRAME name, not the library photo's.
# Without resolving it back, an approved/published reframe would never block a re-pick
# of its own raw photo (the exact zanshin repeats). Hashes are cached by (path, mtime,
# size) so a scan does not re-read an unchanged library.
_REFRAME_SUFFIX = "__feed.jpg"
_hash_cache = {}


def _library_hash(path):
    try:
        st = os.stat(path)
        ck = (path, st.st_mtime, st.st_size)
    except OSError:
        return None
    h = _hash_cache.get(ck)
    if h is None:
        import hashlib
        try:
            with open(path, "rb") as fh:
                h = hashlib.sha256(fh.read()).hexdigest()[:12]
        except OSError:
            return None
        _hash_cache[ck] = h
    return h


def reframe_map(library_path, wanted_keys):
    """{reframe_key: raw_library_basename} for the '<sha12>__feed.jpg'-shaped keys
    among wanted_keys that resolve to a file in this gym's library. Only hashes
    until every wanted key is found; empty when nothing wants resolving."""
    wanted = {k for k in (wanted_keys or ()) if str(k).endswith(_REFRAME_SUFFIX)}
    if not wanted or not library_path:
        return {}
    out = {}
    for key in sorted(library_keys(library_path)):
        if not wanted:
            break
        h = _library_hash(os.path.join(library_path, key))
        if not h:
            continue
        rk = f"{h}{_REFRAME_SUFFIX}"
        if rk in wanted:
            out[rk] = key
            wanted.discard(rk)
    return out


def resolve_raw_keys(state, library_path):
    """Merge each reframe-named entry of a book state into its RAW library
    basename (the key every pick excludes by), keeping the reframe entry too for
    sweep-side matching. In place; returns state."""
    if not state or not library_path:
        return state
    rmap = reframe_map(library_path, state.keys())
    for rk, raw in rmap.items():
        state.setdefault(raw, set()).update(state.get(rk, set()))
    return state


def _months_between(start, end):
    """['YYYY-MM', ...] covering start..end inclusive."""
    months = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        months.append(cur.isoformat()[:7])
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def book_state(base_key, store, start, days, *, log=None, skip_wipeable_months=(),
               library_path=None):
    """{media_key: {(iso_date, status), ...}} for every guard-relevant row of the
    gym: forward-book statuses (any date) + PUBLISHED rows near the planned span
    (read back one repeat window before start). Reads via store.list_month
    (read-only); a read failure returns what was readable — the guard degrades
    open, never blocks planning on a flaky read (the rotation window remains the
    backstop).

    skip_wipeable_months: 'YYYY-MM' months a rebuild is about to delete-then-
    insert — a WIPEABLE (pending/draft/queued) row inside them will not survive
    the rebuild, so it must not block the very photos it is about to release.
    coach_review rows are NOT wipeable and always count."""
    _log = log or (lambda m: print(f"[media-guard] {m}"))
    list_month = getattr(store, "list_month", None)
    if list_month is None or start is None:
        return {}
    if not isinstance(start, date):
        start = _as_date(start)
        if start is None:
            return {}
    win = config.media_repeat_window_days()
    span_end = start + timedelta(days=max(1, int(days or 1)) - 1)
    read_start = start - timedelta(days=win)
    skip = {str(m) for m in (skip_wipeable_months or ())}
    state = {}
    for month in _months_between(read_start, span_end):
        try:
            rows = list_month(base_key, month) or []
        except Exception as exc:  # noqa: BLE001 - degrade open, never block a build
            _log(f"{base_key}: guard read failed for {month} ({type(exc).__name__})")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status not in FORWARD_STATUSES and status != "published":
                continue
            acct = str(row.get("account") or "").strip().lower()
            if acct not in _GUARDED_ACCOUNTS:
                continue                     # GBP keeps its own §3 windows
            pd = str(row.get("post_date") or "")[:10]
            if not pd:
                continue
            if status in _WIPEABLE and pd[:7] in skip:
                continue                     # this rebuild wipes it; photo is free
            key = row_media_key(row)
            if not key:
                continue
            state.setdefault(key, set()).add((pd, status))
    # Resolve autofit reframe names back to their raw library photos so the raw
    # basename (the key every pick excludes by) carries the block.
    return resolve_raw_keys(state, library_path)


def blocked_keys(state, day_key):
    """The media keys that must NOT be placed on day_key: any key with a
    forward-book appearance on a DIFFERENT day, or a PUBLISHED appearance within
    the repeat window of day_key. Same-date appearances never block (a feed, its
    FB mirror and its paired story share the photo by design)."""
    day = _as_date(day_key)
    if day is None:
        return set()
    target = day.isoformat()
    win = config.media_repeat_window_days()
    out = set()
    for key, occurrences in (state or {}).items():
        for pd, status in occurrences:
            if pd == target:
                continue                     # same-date sibling: one post, one photo
            if status == "published":
                d = _as_date(pd)
                if win > 0 and d is not None and abs((day - d).days) <= win:
                    out.add(key)
                    break
            else:
                out.add(key)
                break
    return out


def note_placed(state, key, day_key):
    """Record a placement made THIS run so the next pick in the same run sees it
    (the store read happened before any insert)."""
    if state is None or not key:
        return
    state.setdefault(key, set()).add((str(day_key)[:10], "pending"))


def surviving_keys(base_key, store, start, days, *, log=None, library_path=None):
    """For a MONTH REBUILD over [start, start+days): every media key that will
    still be on the gym's book AFTER the rebuild's delete-then-insert — so the
    rebuild must not re-place any of them on a (different) day. Wipeable rows
    inside the span months are excluded (the rebuild replaces them); everything
    else (approved / publishing / coach_review anywhere, published within the
    trailing window, wipeable rows OUTSIDE the span) blocks. Flag off => empty."""
    if not enabled():
        return set()
    if not isinstance(start, date):
        start = _as_date(start)
        if start is None:
            return set()
    span_end = start + timedelta(days=max(1, int(days or 1)) - 1)
    span_months = set(_months_between(start, span_end))
    state = book_state(base_key, store, start, days, log=log,
                       skip_wipeable_months=span_months,
                       library_path=library_path)
    win = config.media_repeat_window_days()
    keys = set()
    for key, occurrences in state.items():
        for pd, status in occurrences:
            if status == "published":
                d = _as_date(pd)
                # blocks if within the window of ANY day of the span
                if (win > 0 and d is not None
                        and start - timedelta(days=win) <= d
                        <= span_end + timedelta(days=win)):
                    keys.add(key)
                    break
            else:
                keys.add(key)
                break
    return keys


def library_keys(library_path):
    """Every usable media basename in the gym's library (images + videos)."""
    try:
        from .library import list_creatives
        return {os.path.basename(c.path) for c in list_creatives(library_path)
                if getattr(c, "media_type", "") in ("image", "video")}
    except Exception:  # noqa: BLE001
        return set()


def spaced_choice(library_path, state, day_key, hard_exclude=()):
    """SMALL-LIBRARY fallback: the library basename whose existing appearances
    are FARTHEST from day_key (maximum spacing), never one in hard_exclude
    (live photos, the denied post's own photo). A key with no appearance at all
    wins outright. None when nothing is choosable. Deterministic (name tiebreak).
    Callers pair this with alert_small_library so the thin library is never
    silent."""
    day = _as_date(day_key)
    if day is None:
        return None
    skip = {str(k) for k in (hard_exclude or ())}
    best_key, best_dist = None, -1
    for key in sorted(library_keys(library_path)):
        if key in skip:
            continue
        dists = []
        for pd, _status in (state or {}).get(key, ()):
            d = _as_date(pd)
            if d is not None:
                dists.append(abs((day - d).days))
        dist = min(dists) if dists else 10 ** 6      # never-used wins outright
        if dist > best_dist:
            best_key, best_dist = key, dist
    return best_key


def alert_small_library(base_key, day_key, log=None):
    """ONE kv-deduped info digest per gym per month (the needs-media language
    family): the library is smaller than the forward book, so photos will repeat
    with maximum spacing until more media arrives. Durable-or-silent, exactly
    like client_content._alert_needs_media (never a storm from an ephemeral kv).
    Never raises."""
    _log = log or (lambda m: print(f"[media-guard] {m}"))
    msg = (f"{base_key}: the photo library is smaller than the forward book, so "
           "some photos will REPEAT across days (spaced as far apart as "
           "possible). Add photos (connect the gym's Drive folder or upload in "
           "the portal) to stop the repeats. Not blocked; posts keep flowing.")
    try:
        from . import db, ops_alerts
        key = f"media_guard_small_lib_{base_key}_{str(day_key)[:7]}"
        if db.kv_get(key):
            return
        if not db.kv_is_durable():
            _log(f"{base_key}: small library (repeat with spacing); digest "
                 "suppressed: ephemeral kv cannot dedupe")
            return
        db.kv_set(key, "1")
        ops_alerts.alert(msg)
    except Exception as exc:  # noqa: BLE001 - an alert failure never blocks a build
        _log(f"{base_key}: small-library digest failed ({type(exc).__name__})")


def find_cross_day_repeats(rows, *, repeat_window_days=None):
    """AUDIT/SWEEP helper (pure): group guard-scope rows by media key and return
    {key: {date: [row, ...]}} for every key that appears on MORE THAN ONE
    distinct date and has at least one forward-book (fixable-side) appearance.
    Published rows participate so the sweep can see 'published earlier, pending
    again later' — the sweep itself never touches the published side. Rows
    outside the guarded accounts or without an image are ignored."""
    del repeat_window_days  # window filtering is the caller's concern
    by_key = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in FORWARD_STATUSES and status != "published":
            continue
        acct = str(row.get("account") or "").strip().lower()
        if acct not in _GUARDED_ACCOUNTS:
            continue
        pd = str(row.get("post_date") or "")[:10]
        key = row_media_key(row)
        if not pd or not key:
            continue
        by_key.setdefault(key, {}).setdefault(pd, []).append(row)
    out = {}
    for key, by_date in by_key.items():
        forward_dates = {d for d, rws in by_date.items()
                         if any(str(r.get("status") or "").lower() in FORWARD_STATUSES
                                for r in rws)}
        if len(by_date) > 1 and forward_dates:
            out[key] = by_date
    return out
