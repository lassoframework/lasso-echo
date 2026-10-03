"""
media_swap.py — SWAPPING A WRONG PHOTO IS FREE (B6), AND IT HAS TO BE WORTH DOING.

THE BUDGET DESIGN BUG (Pete, zanshin): a gym gets 15 recreates a month
(portal_social.MONTHLY_RECREATE_BUDGET). The portal's only levers are approve /
edit / deny / kill, so "use a different photo" and "the caption needs work" are
BOTH a deny, and both burn one of the 15. Pete ran out of recreates swapping
photos and then could not fix a caption. That is not a broken counter: the
counter is correct, the two actions were never separated.

THE SPLIT:
  * MEDIA SWAP  -> free and unlimited. The caption Echo wrote is fine; only the
    pixels are wrong. Nothing is regenerated, so nothing is spent.
  * CAPTION RECREATE -> still one of 15. Regenerating copy is the expensive act.

THE SECOND BUG (John Weeks / Tough Temple, 2026-09-10): the free swap picked the
FIRST ALPHABETICALLY SORTED unused image in the local library, ignored the served
ledger, ignored the gym's connected Drive pool (57 eligible videos) and refused
videos outright. "Edit image" handed back another equally unengaging still, every
time, in filename order. What this module does now:

  CANDIDATES  = the gym's local library (images AND videos) + its Drive pool
                (eligible, not hidden by the coach, outside the 90-day cooldown).
  EXCLUDED    = the row's current media, everything on the gym's book (the
                pending/approved/publishing/coach_review rows plus anything
                PUBLISHED inside the repeat window; media_guard.book_state, read
                once by photo key and once by Drive asset id), and any local
                creative served inside the repeat window (rotation ledger).
  ORDER       = least recently used first (never used wins), then least used,
                then name for determinism.
  EXHAUSTION  = for gyms without an explicit long-term reuse promise, a manual
                swap may relax only the generic rotation cooldown and choose the
                least-recently-used safe asset outside the live forward book.
                Zanshin's nine-month policy remains a hard gate.
  VIDEO FIRST = when the row's current media is a still and a video candidate
                exists, the swap hands back a video. A follower asking for a
                different picture of the same nine stills is asking for footage.

WHAT THIS MODULE DOES: pick + materialize + host the replacement for ONE waiting
row and return the urls (and the media identity columns) to point it at. It does
NOT write, does NOT publish, and does NOT touch the budget: the caller
(portal_social.handle_swap_media) owns the write through
SupabaseCalendarStore.swap_media, which is status-guarded server-side to pending /
coach_review so an approved or live row can never be repointed. after_swap settles
the usage ledgers once that write has actually happened.

A swapped-in VIDEO gets exactly the shape the Drive builder gives a video row: the
hosted video (or its H.264 rendition) as image_url, a hosted poster frame as
thumbnail_url, a caption-burned 9:16 story video for a story row. The publisher
types mediaItems by URL extension (zernio._media_type), so a .mp4/.mov image_url
publishes as a video on IG + FB and is never sent as an image.

Flag: config.media_swap_free_enabled() (ECHO_MEDIA_SWAP_FREE, DEFAULT OFF).
"""

import os
import tempfile

from . import config, media_guard
from .media_types import (VIDEO_EXTS as _VIDEO_EXTS,           # ONE definition (audit D1)
                          is_video_url, is_publishable_video)

# Why a reason code and not an exception: the portal answers a client, and every
# outcome here is a normal thing that can happen to a real gym.
REASON_NO_LIBRARY = "no_library"
REASON_NO_FRESH_PHOTO = "no_fresh_photo"
REASON_ASSET_PREP = "asset_preparation_failed"
REASON_HOSTING = "hosting_unavailable"
REASON_STORY_REBURN = "story_reburn_failed"

REASON_TIMEOUT = "swap_timeout"

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SWAP_TRANSCODE_TIMEOUT_SEC = 45    # the ONE transcode a portal request may wait on
SWAP_REQUEST_DEADLINE_SEC = 75     # the whole request: downloads + probes + transcode + burn
SWAP_DOWNLOAD_TIMEOUT_SEC = 20     # one Drive download


class SwapDeadline(Exception):
    """The request-level deadline passed before the swap could finish a step."""


class _Deadline:
    """A request budget checked BEFORE every expensive step (download, probe,
    transcode, burn), so a portal request completes in bounded time no matter how
    many candidates it walks (audit round 4 #1). `clock` is injectable for tests."""

    def __init__(self, seconds, clock=None):
        import time as _time
        self._clock = clock or _time.monotonic
        self._end = self._clock() + float(seconds)

    def remaining(self):
        return max(0.0, self._end - self._clock())

    def expired(self):
        return self.remaining() <= 0.0

    def check(self, step):
        if self.expired():
            raise SwapDeadline(f"request deadline passed before {step}")


def enabled():
    return config.media_swap_free_enabled()


def _log(msg):
    print(f"[media-swap] {msg}")


def is_video(name_or_url):
    return is_video_url(name_or_url)


def library_path_for(base_key):
    """The gym's OWN media folder. STRICT lookup (the same posture
    portal_calendar_store._media_library_path takes): a gym with an empty
    library_prefix returns '' rather than falling back to the shared parent,
    because swapping in another gym's photo is worse than not swapping."""
    try:
        from . import accounts as _accounts
        for key in (base_key, f"{base_key}_ig", f"{base_key}_fb"):
            acct = _accounts.get_account(key)
            prefix = str(getattr(acct, "library_prefix", "") or "") if acct else ""
            if prefix:
                return prefix
    except Exception:  # noqa: BLE001 - a resolution failure is "no library", never a raise
        return ""
    return ""


# ---- candidates ---------------------------------------------------------------------
def _last_served_local(base_key):
    """{rotation_key: newest served date} across the gym's IG/FB/base accounts."""
    from . import rotation
    served = rotation.load_served_strict()
    out = {}
    for acct in (base_key, f"{base_key}_ig", f"{base_key}_fb", f"{base_key}_gbp"):
        for e in served.get(acct, []) or []:
            k, d = e.get("key"), str(e.get("date") or "")
            if k and d > out.get(k, ""):
                out[k] = d
            digest = e.get("content_hash") or ""
            if digest and d > out.get(f"sha256:{digest}", ""):
                out[f"sha256:{digest}"] = d
    return out


def _real_file(path, kind):
    """A pickable local file: a decodable image over 2 KB (never a leaked 11-byte
    test fixture) or a video file over 2 KB."""
    try:
        if os.path.getsize(path) < 2048:
            return False
        if kind == "video":
            return True
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def _probe(path, timeout):
    """gym_media_index.probe_video with the bounded timeout when it accepts one (the
    real one does); injected stubs without the parameter still work."""
    import inspect
    from . import gym_media_index as _idx
    fn = _idx.probe_video
    try:
        params = inspect.signature(fn).parameters
        accepts = "timeout" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        accepts = False
    return fn(path, timeout=timeout) if accepts else fn(path)


def _local_video_servable(path):
    """A local-library video may be swapped in only when its codec is web-playable and
    its container publishable: ffprobe the codec (bounded) and refuse HEVC / .webm /
    .avi / .mkv, exactly as the Drive pool does (audit round 5 minor: never host raw
    HEVC from the local library either). An unprobed file is refused (fail closed)."""
    from . import gym_media_index as _idx
    try:
        info = _probe(path, SWAP_DOWNLOAD_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001
        info = None
    if not info:
        return False
    pseudo = {"kind": _idx.KIND_VIDEO, "title": os.path.basename(path)}
    return not _idx.needs_rendition(pseudo, info)


def local_candidates(base_key, lib, post_date, blocked_keys, *, allow_recent=False):
    """The gym's local-library creatives a swap may use for a row on post_date.

    The served ledger is permanent for swaps: a locally served photo is never
    offered again. ``allow_recent`` remains for caller compatibility and cannot
    override this rule. Live-book media remains blocked by ``blocked_keys``.
    """
    if not lib or not os.path.isdir(lib):
        return []
    from . import dam, rotation
    try:
        excl = set(rotation.style_exclusions(lib))
    except Exception:  # noqa: BLE001
        excl = set()
    try:
        served = _last_served_local(base_key)
    except Exception as exc:  # noqa: BLE001 - unknown history cannot make local media fresh
        _log(f"{base_key}: served ledger unreadable ({type(exc).__name__}); "
             "local swap candidates suppressed")
        return []
    out = []
    for key in sorted(media_guard.library_keys(lib)):
        if key in blocked_keys or key in excl:
            continue
        ext = os.path.splitext(key)[1].lower()
        kind = "video" if ext in _VIDEO_EXTS else ("photo" if ext in _IMG_EXTS else "")
        if not kind:
            continue
        path = os.path.join(lib, key)
        if not os.path.isfile(path) or not _real_file(path, kind):
            continue
        if kind == "video" and not _local_video_servable(path):
            continue                      # HEVC / odd container in the library: never raw
        try:
            rk = dam.rotation_key(path)
        except Exception:  # noqa: BLE001
            rk = key
        try:
            digest = rotation.local_content_hash(path)
        except OSError:
            continue
        last = served.get(rk, "") or served.get(f"sha256:{digest}", "")
        if last:
            continue                      # once served, never offer again
        out.append({"source": "local", "kind": kind, "key": key, "path": path,
                    "last_used": last, "used_count": 1 if last else 0, "name": key})
    return out


def drive_candidates(base_key, blocked_ids, *, media_store=None, now=None,
                     allow_cooling=False):
    """The gym's Drive-pool assets a swap may use: gym_media_selector.pickable (eligible,
    not hidden, outside the 90-day cooldown, not used this month), minus everything
    already on the book. Never raises; an unarmed store is an empty list."""
    try:
        from . import gym_media_selector as _sel
        picker = _sel.cooldown_fallback if allow_cooling else _sel.pickable
        kwargs = {"store": media_store, "exclude_ids": tuple(blocked_ids)}
        if not allow_cooling:
            kwargs["now"] = now
        assets = picker(base_key, **kwargs)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in assets:
        kind = str(a.get("kind") or "")
        asset_id = a.get("id")
        if (kind not in ("photo", "video") or not isinstance(asset_id, str)
                or not asset_id.strip()):
            continue
        # Cooldown fallback is for locating genuinely unused assets missed by a
        # selector freshness read. It must never recycle a Drive asset whose
        # persistent use counters say it has already been staged.
        used_count = a.get("used_count")
        last_used_at = a.get("last_used_at")
        if (isinstance(used_count, bool) or not isinstance(used_count, int)
                or used_count != 0 or last_used_at not in (None, "")):
            continue
        out.append({"source": "drive", "kind": kind, "key": asset_id,
                    "asset": a, "last_used": "", "used_count": 0,
                    "name": str(a.get("title") or asset_id)})
    return out


def has_rendition(cand):
    """A video candidate the swap can serve WITHOUT a transcode: a local-library video
    (served as-is, as Lane A always has) or a Drive asset that already carries a
    rendition_url."""
    if cand.get("kind") != "video":
        return False
    if cand.get("source") == "local":
        return True
    return bool((cand.get("asset") or {}).get("rendition_url"))


def _tier(cand):
    """Drive photos first, local photos second, then usable videos.

    Ready videos precede videos needing a transcode. Used media is excluded by
    ``order_candidates`` and cannot be a last-resort candidate.
    """
    if cand.get("kind") == "photo":
        return 0 if cand.get("source") == "drive" else 1
    if cand.get("kind") == "video":
        return 2 if has_rendition(cand) else 3
    return 4


def _never_used_candidate(cand):
    """Require explicit unused metadata before a swap candidate can be returned."""
    count = (cand or {}).get("used_count")
    last = (cand or {}).get("last_used")
    return (isinstance(count, int) and not isinstance(count, bool) and count == 0
            and isinstance(last, str) and not last.strip()
            and (cand or {}).get("source") in ("drive", "local")
            and (cand or {}).get("kind") in ("photo", "video"))


def order_candidates(cands, *, current_is_video=False):
    """Tier first (see _tier), then least recently used, then least used, then name."""
    del current_is_video   # kept for callers; the tier order supersedes the filter
    cands = [cand for cand in (cands or []) if _never_used_candidate(cand)]
    cands.sort(key=lambda c: (_tier(c), c.get("last_used") or "",
                              int(c.get("used_count") or 0), str(c.get("name") or "")))
    return cands


def candidates_for(base_key, row, *, store, lib, book_state=None, asset_state=None,
                   media_store=None, now=None, log=None):
    """Every creative this row could swap to, ordered. The two book reads are keyed
    by photo basename and by Drive asset id because the two pools live in different
    id spaces; both are excluded wholesale (anything on the book, any date) so a swap
    never trades one repeat for another, including against the day's other post."""
    say = log or _log
    pd = str((row or {}).get("post_date") or "")[:10]
    start = None
    try:
        from datetime import date as _date
        start = _date.fromisoformat(pd) if pd else None
    except ValueError:
        start = None
    if book_state is None:
        book_state = (media_guard.book_state(base_key, store, start, 1, log=say,
                                            library_path=lib) if start else {})
    if asset_state is None:
        asset_state = (media_guard.book_state(base_key, store, start, 1, log=say,
                                             key_fn=media_guard.row_asset_key)
                       if start else {})
    blocked_keys = set((book_state or {}).keys())
    current = media_guard.row_media_key(row)
    if current:
        blocked_keys.add(current)
    blocked_ids = set((asset_state or {}).keys())
    current_asset = media_guard.row_asset_key(row)
    if current_asset:
        blocked_ids.add(current_asset)
    cands = local_candidates(base_key, lib, pd, blocked_keys)
    cands += drive_candidates(base_key, blocked_ids, media_store=media_store, now=now)
    if not cands:
        # A user-requested swap must not deadlock a small library merely because
        # every otherwise-safe asset is inside the generic 30/90-day rotation
        # clocks. Keep anything on another active/in-flight day excluded, then use
        # the least-recently-used safe asset. Explicit client policies remain hard:
        # Zanshin's nine-month no-repeat promise never reaches this fallback.
        from .media_reuse_policy import reuse_months
        if not reuse_months(base_key):
            cands = local_candidates(base_key, lib, pd, blocked_keys,
                                     allow_recent=True)
            cands += drive_candidates(base_key, blocked_ids, media_store=media_store,
                                      now=now, allow_cooling=True)
            for cand in cands:
                cand["reuse_fallback"] = True
            if cands:
                say(f"{base_key}: fresh swap pool exhausted; using least-recently-used "
                    "media outside the live forward book")
    return order_candidates(cands, current_is_video=is_video(current))


# ---- materialize (a local file + maybe an already-hosted url) ------------------------
def _download_bounded(drive, file_id, path, timeout):
    """drive.download in a DAEMON worker thread, waited on for at most `timeout`
    seconds. Returns True on success; False on failure or timeout (the request moves
    on; a still-running download is abandoned to the daemon thread, which can never
    hold the process open at exit, and its temp file is cleaned up with the work
    dir)."""
    import threading
    box = {}

    def _run():
        try:
            drive.download(file_id, path)
            box["ok"] = True
        except Exception as exc:  # noqa: BLE001 - reported as a failed download
            box["err"] = exc
    t = threading.Thread(target=_run, name="media-swap-download", daemon=True)
    t.start()
    t.join(timeout=max(0.1, float(timeout)))
    return bool(box.get("ok")) and not t.is_alive()


def _materialize(base_key, cand, work_dir, *, drive=None, media_store=None, budget=None,
                 deadline=None):
    """{"path": local file, "hosted": url or None} for one candidate, or None when it
    cannot be used (download failed, video failed its probe gate). A Drive asset is
    downloaded into work_dir; a HEIC/HEVC original gets its cached rendition (already
    hosted) via gym_media_index.ensure_rendition, exactly like the Drive builder.

    budget: the ONE shared RenditionBudget(1) for the whole request (audit round 4 #1:
    a per-candidate budget let three candidates mean three transcodes). deadline: the
    request _Deadline, checked before the download, the probe and the transcode;
    raises SwapDeadline when it has passed."""
    if cand["source"] == "local":
        return {"path": cand["path"], "hosted": None}
    from . import gym_media_index as _idx
    from .integrations import drive_client as _dc
    asset = cand["asset"]
    drive = drive or _dc.DriveClient()
    if not drive.available():
        return None
    title = asset.get("title") or f"{asset['id']}.bin"
    path = os.path.join(work_dir, os.path.basename(title))
    if deadline is not None:
        deadline.check(f"download of {title!r}")
    dl_timeout = SWAP_DOWNLOAD_TIMEOUT_SEC
    if deadline is not None:
        dl_timeout = min(dl_timeout, deadline.remaining())
    if not _download_bounded(drive, asset["id"], path, dl_timeout):
        _log(f"{base_key}: Drive download of {title!r} failed or ran past "
             f"{dl_timeout:.0f}s; trying the next candidate")
        return None
    store = media_store or _idx.default_store()
    info = None
    if asset.get("kind") == _idx.KIND_VIDEO:
        if deadline is not None:
            deadline.check(f"probe of {title!r}")
        probe_timeout = _idx.PROBE_TIMEOUT_SEC
        if deadline is not None:
            probe_timeout = min(probe_timeout, deadline.remaining())
        info = _probe(path, probe_timeout)
        if not info:
            return None                       # unprobed never ships (fail closed)
        el, _reason, _label = _idx.video_eligibility(
            os.path.getsize(path) or asset.get("size_bytes"),
            info["duration_sec"], info["width"], info["height"])
        if el is not True:
            return None
    # BOUNDED (audit R-D1 #4 + round 4 #1): this runs inside the portal request. At
    # most ONE transcode per REQUEST (the shared budget), capped at
    # SWAP_TRANSCODE_TIMEOUT_SEC and at the time the request has left; a spent budget
    # or a timeout means "next candidate", never a hung request and never raw HEVC.
    timeout = SWAP_TRANSCODE_TIMEOUT_SEC
    if deadline is not None and _idx.needs_rendition(asset, info):
        deadline.check(f"transcode of {title!r}")
        timeout = max(1.0, min(timeout, deadline.remaining()))
    try:
        hosted, _converted = _idx.ensure_rendition(
            asset, path, store=store, probe_info=info,
            budget=budget if budget is not None else _idx.RenditionBudget(1),
            timeout=timeout)
    except (_idx.RenditionBudgetExhausted, _idx.RenditionTimeout) as exc:
        _log(f"{base_key}: {title!r} not renditioned in time ({type(exc).__name__}); "
             "trying the next candidate")
        return None
    if not hosted and _idx.needs_rendition(asset, info):
        return None                           # HEVC / odd container / HEIC, no rendition
    return {"path": path, "hosted": hosted}


def sibling_rows(row, rows, lib=None):
    """The same-date rows that are the SAME POST as `row` and carry the same media: the
    FB mirror of an IG feed and its paired IG story (or, when the clicked row is the
    story, both feed rows). A swap that moves only the clicked row leaves FB publishing
    the rejected still and lets the old Drive asset look free while a sibling still
    carries it (audit 3c).

    Match = same gym + same post_date + a different id + an IG/FB feed/story row +
    (the same Drive asset id when both carry one, else the same raw media key, with
    autofit reframe names resolved back to the library photo when `lib` is known).
    The day's OTHER, unrelated posts (a 2x day's second slot, a rolled-forward
    backfill) share neither and are never touched. Statuses are NOT filtered here:
    the caller reports approved/live siblings as left in place."""
    row = row or {}
    pd = str(row.get("post_date") or "")[:10]
    gym = str(row.get("gym_id") or "")
    rid = str(row.get("id") or "")
    old_asset = media_guard.row_asset_key(row)
    old_key = media_guard.row_media_key(row)
    keys = {old_key} | {media_guard.row_media_key(r) for r in (rows or []) if isinstance(r, dict)}
    rmap = media_guard.reframe_map(lib, keys) if lib else {}
    old_raw = rmap.get(old_key, old_key)
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or str(r.get("id") or "") == rid:
            continue
        if str(r.get("gym_id") or "") != gym or str(r.get("post_date") or "")[:10] != pd:
            continue
        if str(r.get("format") or "").lower() not in ("feed", "story"):
            continue
        if str(r.get("account") or "").lower() not in ("instagram", "ig", "facebook", "fb", ""):
            continue
        sib_asset = media_guard.row_asset_key(r)
        if old_asset and sib_asset:
            same = sib_asset == old_asset
        else:
            k = media_guard.row_media_key(r)
            same = bool(old_raw) and rmap.get(k, k) == old_raw
        if same:
            out.append(r)
    return out


def book_carries_asset(rows, asset_id, *, except_ids=()):
    """True when any LIVE row (pending/approved/publishing/coach_review/published) other
    than `except_ids` still carries this Drive asset: the asset must then stay stamped."""
    if not asset_id:
        return False
    skip = {str(i) for i in (except_ids or ())}
    live = set(media_guard.FORWARD_STATUSES) | {"published"}
    for r in rows or []:
        if not isinstance(r, dict) or str(r.get("id") or "") in skip:
            continue
        if str(r.get("status") or "").lower() not in live:
            continue
        if media_guard.row_asset_key(r) == str(asset_id):
            return True
    return False


def pick_replacement(base_key, row, *, store, library_path=None, book_state=None,
                     asset_state=None, candidates_fn=None, materialize_fn=None,
                     host_fn=None, feed_fn=None, reburn_fn=None, poster_fn=None,
                     media_store=None, drive=None, now=None, log=None,
                     siblings=(), clock=None):
    """A genuinely fresh creative for ONE waiting row (and the same creative shaped for
    its same-date siblings, see `siblings`).

    Returns {"ok": True, "image_url", "source_media_url", "key", "kind", "source",
    "thumbnail_url", "source_media_asset_id", "path", "siblings": {row_id: variant}}
    or {"ok": False, "reason": <REASON_*>}. Never writes, never publishes, never spends
    budget. Every piece of I/O is injectable so this is testable offline.

    A story is re-burned with its OWN caption onto the new media (a still card or a
    9:16 story video); a photo feed gets the same autofit reframe the original
    shipped with; a video feed ships the hosted video itself with a poster frame.

    siblings: the same-post rows (sibling_rows) that must move WITH the clicked row.
    Each gets its own variant built from the SAME materialized file while it still
    exists (a story sibling is re-burned with its own caption), keyed by row id."""
    say = log or _log
    lib = library_path if library_path is not None else library_path_for(base_key)

    from .jobs import media_repeat_sweep as _sweep
    reburn_fn = reburn_fn or _sweep._reburn_story
    if poster_fn is None:
        from . import gym_media_builder as _gmb
        poster_fn = _gmb.video_poster_url

    cands = (candidates_fn(base_key, row) if candidates_fn is not None
             else candidates_for(base_key, row, store=store, lib=lib,
                                 book_state=book_state, asset_state=asset_state,
                                 media_store=media_store, now=now, log=say))
    if not cands:
        # Nothing at all to draw from is a different message than "everything is
        # already on the book": tell the owner which one it is.
        has_lib = bool(lib) and os.path.isdir(lib) and bool(media_guard.library_keys(lib))
        has_pool = False
        if not has_lib:
            try:
                from . import gym_media_selector as _sel
                has_pool = bool(_sel.pickable(base_key, store=media_store, now=now))
            except Exception:  # noqa: BLE001
                has_pool = False
        return {"ok": False, "reason": (REASON_NO_FRESH_PHOTO if (has_lib or has_pool)
                                        else REASON_NO_LIBRARY)}

    fmt = str((row or {}).get("format") or "feed").strip().lower()
    # ONE request budget (audit round 4 #1): a single transcode for the whole request
    # and a wall-clock deadline every expensive step checks first, so the portal call
    # returns in bounded time however many candidates it walks. Deadline hit -> 409
    # swap_timeout with nothing written (the caller writes only on ok).
    from . import gym_media_index as _idx
    budget = _idx.RenditionBudget(1)
    deadline = _Deadline(SWAP_REQUEST_DEADLINE_SEC, clock)
    work = tempfile.mkdtemp(prefix="mediaswap_")
    try:
        # Swift River, 2026-09-23: the old loop tried only the first three
        # candidates and then reported no_fresh_photo ("all photos were used") even
        # when ~500 eligible assets remained and only preparation had failed. Walk
        # EVERY ordered candidate, bounded by the finite selector result and, far
        # earlier, by the shared request deadline and the one-transcode budget. Only say
        # no_fresh_photo when the SELECTOR returned nothing pickable (handled above).
        # Candidates that existed but could not be prepared in time are a retryable
        # asset-preparation failure, never a false exhaustion report.
        prep_failures = 0
        for cand in cands:
            if deadline.expired():
                say(f"{base_key}: swap request deadline passed before candidate "
                    f"{cand.get('key')}; nothing written")
                return {"ok": False, "reason": REASON_TIMEOUT}
            try:
                mat = (materialize_fn(cand) if materialize_fn is not None
                       else _materialize(base_key, cand, work, drive=drive,
                                         media_store=media_store, budget=budget,
                                         deadline=deadline))
            except SwapDeadline as exc:
                say(f"{base_key}: {exc}; nothing written")
                return {"ok": False, "reason": REASON_TIMEOUT}
            if not mat or not mat.get("path"):
                prep_failures += 1
                continue
            path = mat["path"]
            if (_visual_writer_enabled() and cand["source"] == "drive"
                    and mat.get("hosted")):
                # ensure_rendition only returns (URL, newly_converted). A cached
                # HEIC/HEVC rendition has no source URL or render receipt here.
                # Its bytes cannot satisfy the Drive original's content_hash as
                # a raw same-byte asset. Activation needs real conversion lineage.
                say(f"{base_key}: Drive rendition has no verified render provenance; "
                    "writer preparation blocked")
                prep_failures += 1
                continue
            # Drive assets host under the gym base (builder parity, so the same bytes
            # dedupe to one object); local library photos keep the _ig tenant the
            # sweep and the month build have always used.
            tenant = base_key if cand["source"] == "drive" else f"{base_key}_ig"
            hosted = mat.get("hosted") or ""
            if not hosted:
                if deadline.expired():
                    say(f"{base_key}: swap request deadline passed before hosting "
                        f"{cand.get('key')}; nothing written")
                    return {"ok": False, "reason": REASON_TIMEOUT}
                if host_fn is not None:
                    hosted = host_fn(path) or ""
                else:
                    try:
                        from . import media_host
                        if config.hosting_enabled():
                            hosted = media_host.host_media(path, tenant) or ""
                    except Exception as exc:  # noqa: BLE001
                        say(f"{base_key}: hosting failed for {cand['key']} "
                            f"({type(exc).__name__})")
            if not hosted:
                return {"ok": False, "reason": REASON_HOSTING}
            # One poster per swap (audit D4): computed once, attached only to FEED
            # variants; a story's media is the captioned card/video itself.
            poster = ""
            try:
                if cand["kind"] == "video":
                    deadline.check(f"poster frame for {cand.get('key')}")
                    poster = poster_fn(path, work, tenant) or ""
                out = _finish(base_key, row, fmt, cand, path, hosted, lib, work, tenant,
                              poster=poster, feed_fn=feed_fn, reburn_fn=reburn_fn, log=say,
                              deadline=deadline)
                if not out.get("ok"):
                    return out
                # ALL OR NOTHING (audit 3c residual): every sibling variant is computed
                # here, BEFORE the caller writes anything. One failed variant (a story
                # re-burn) fails the whole swap with its reason, so the clicked row and
                # its siblings can never end up carrying different media.
                out["siblings"] = {}
                for sib in siblings or ():
                    sfmt = str((sib or {}).get("format") or "feed").strip().lower()
                    var = _finish(base_key, sib, sfmt, cand, path, hosted, lib, work,
                                  tenant, poster=poster, feed_fn=feed_fn,
                                  reburn_fn=reburn_fn, log=say, deadline=deadline)
                    if not var.get("ok"):
                        return {"ok": False,
                                "reason": var.get("reason") or REASON_STORY_REBURN,
                                "failed_sibling": str(sib.get("id"))}
                    out["siblings"][str(sib.get("id"))] = var
            except SwapDeadline as exc:
                say(f"{base_key}: {exc}; nothing written")
                return {"ok": False, "reason": REASON_TIMEOUT}
            return out
        say(f"{base_key}: {prep_failures} of {len(cands)} "
            "swap candidates could not be prepared (download/probe/conversion); "
            "retryable, nothing written")
        return {"ok": False, "reason": REASON_ASSET_PREP,
                "candidates_tried": prep_failures}
    finally:
        _cleanup(work)


def _finish(base_key, row, fmt, cand, path, hosted, lib, work, tenant, *, poster,
            feed_fn, reburn_fn, log, deadline=None):
    """Shape the hosted replacement for the row's format: a story is re-burned with
    its caption (still card or 9:16 story video), a video feed ships the hosted video,
    a photo feed gets the autofit reframe. A failed story re-burn is a hard stop
    (REASON_STORY_REBURN), matching the pre-existing contract: never a bare story.
    deadline (request _Deadline) is checked before a story burn, the other expensive
    step; raises SwapDeadline."""
    video = cand["kind"] == "video"
    base = {"key": cand["key"], "kind": cand["kind"], "source": cand["source"],
            "thumbnail_url": poster if (video and fmt != "story") else "", "path": path,
            "source_media_asset_id": cand["key"] if cand["source"] == "drive" else ""}
    raw_source = hosted
    if fmt == "story":
        # A story publishes empty-body, so its caption lives ON the media. Swapping
        # the pixels without re-burning would ship a captionless story.
        evidence = None
        if config.story_format_enabled():
            if deadline is not None:
                deadline.check(f"story burn for row {row.get('id')}")
            if _visual_writer_enabled():
                if video:
                    burned, evidence = _reburn_story_video_with_evidence(
                        base_key, row, path, lib, hosted, deadline=deadline)
                else:
                    burned, evidence = _reburn_story_with_evidence(
                        base_key, row, path, lib, hosted, deadline=deadline)
            else:
                burned = (_reburn_story_video(base_key, row, path, lib) if video
                          else reburn_fn(base_key, row, path, lib))
            if not burned:
                return {"ok": False, "reason": REASON_STORY_REBURN}
            target = burned
        else:
            target = hosted
        src = hosted if config.story_source_media_enabled() else None
        out = {"ok": True, "image_url": target, "source_media_url": src, **base}
        if evidence is not None:
            out["render_evidence"] = _evidence_dict(evidence)
            out["source_media_url"] = raw_source
        elif _visual_writer_enabled():
            # Even an unburned Story points at its exact raw object.
            out["source_media_url"] = raw_source
        return out

    if video:
        # A video feed ships the hosted video itself (autofit is a still-photo lane).
        return {"ok": True, "image_url": hosted,
                "source_media_url": hosted if _visual_writer_enabled() else None, **base}

    # FEED AUTOFIT PARITY: the original shipped through the square reframe, so the
    # replacement gets it too. Any failure keeps the raw hosted photo (never a drop).
    target = hosted
    feed_rendered_bytes = None
    if deadline is not None and (feed_fn is not None or config.feed_autofit_enabled()):
        deadline.check(f"autofit for row {row.get('id')}")
    if feed_fn is not None:
        feed_result = feed_fn(path)
        # Injected URL-only renderers cannot attest their local output bytes.
        target = feed_result if isinstance(feed_result, str) and feed_result else hosted
    elif config.feed_autofit_enabled():
        try:
            from . import feed_image, media_host
            asset = feed_image.get_or_make_feed_image(path, lib or work, logger=log)
            if asset:
                reframed = media_host.host_media(asset, tenant)
                if reframed:
                    target = reframed
                    if _visual_writer_enabled():
                        with open(asset, "rb") as rendered:
                            feed_rendered_bytes = rendered.read()
        except Exception:  # noqa: BLE001 - the raw hosted photo is a correct answer
            pass
    out = {"ok": True, "image_url": target,
           "source_media_url": hosted if _visual_writer_enabled() else None, **base}
    if target != hosted and _visual_writer_enabled():
        evidence = _feed_render_evidence(path, hosted, target,
                                         rendered_bytes=feed_rendered_bytes,
                                         deadline=deadline)
        if evidence is None:
            return {"ok": False, "reason": REASON_ASSET_PREP}
        out["render_evidence"] = evidence
        out["source_media_url"] = hosted
    elif _visual_writer_enabled():
        out["source_media_url"] = hosted
    return out


def _visual_writer_enabled():
    from . import visual_writer_prepare
    return visual_writer_prepare.enabled()


def _evidence_dict(evidence):
    return evidence.as_dict() if hasattr(evidence, "as_dict") else evidence


def _read_evidence_url(url, deadline):
    """Bound a remote read by the request clock without executor shutdown waits."""
    from . import visual_writer_prepare
    if deadline is None:
        return visual_writer_prepare._bytes_for_url(url)
    import threading
    deadline.check(f"evidence read of {url!r}")
    result = {}

    def _read():
        try:
            result["bytes"] = visual_writer_prepare._bytes_for_url(url)
        except Exception as exc:  # noqa: BLE001 - caller treats an unreadable URL as no evidence
            result["error"] = exc

    worker = threading.Thread(target=_read, name="media-swap-evidence", daemon=True)
    worker.start()
    worker.join(deadline.remaining())
    deadline.check(f"evidence read of {url!r}")
    if worker.is_alive():
        raise SwapDeadline(f"request deadline passed during evidence read of {url!r}")
    if "error" in result:
        raise result["error"]
    return result.get("bytes")


def _render_evidence(source_url, delivered_url, source_bytes, delivered_bytes, operation,
                     deadline=None):
    import hashlib
    import uuid
    if not source_bytes or not delivered_bytes or source_url == delivered_url:
        return None
    if _read_evidence_url(source_url, deadline) != source_bytes:
        return None
    if _read_evidence_url(delivered_url, deadline) != delivered_bytes:
        return None
    return {"source_exact_url": source_url, "delivered_exact_url": delivered_url,
            "source_fingerprint": "md5:" + hashlib.md5(source_bytes).hexdigest(),
            "delivered_fingerprint": "md5:" + hashlib.md5(delivered_bytes).hexdigest(),
            "source_byte_length": len(source_bytes), "delivered_byte_length": len(delivered_bytes),
            "operation": operation, "evidence_ref": "media_swap:" + str(uuid.uuid4()),
            "observed_by": "media_swap", "rendered_by": "media_swap"}


def _reburn_story_with_evidence(base_key, row, path, lib, raw_url, *, deadline=None):
    """Reburn with exact bytes observed at raw and hosted URLs."""
    try:
        from . import media_host, story_image
        from .jobs.media_repeat_sweep import _gym_name
        caption = (row.get("caption") or "").strip()
        asset = story_image.get_or_make_story_image(path, caption, _gym_name(base_key),
                                                     lib, logger=_log)
        if not asset or not config.hosting_enabled():
            return None, None
        delivered_url = media_host.host_media(asset, f"{base_key}_ig")
        if not raw_url or not delivered_url:
            return None, None
        with open(path, "rb") as fh:
            source_bytes = fh.read()
        with open(asset, "rb") as fh:
            rendered = fh.read()
        evidence = _render_evidence(raw_url, delivered_url, source_bytes, rendered,
                                    "reburn", deadline=deadline)
        return (delivered_url, evidence) if evidence else (None, None)
    except SwapDeadline:
        raise
    except Exception as exc:  # noqa: BLE001
        _log(f"{base_key}: story evidence unavailable ({type(exc).__name__})")
        return None, None


def _reburn_story_video_with_evidence(base_key, row, path, lib, raw_url, *, deadline=None):
    """Video counterpart of _reburn_story_with_evidence."""
    try:
        from . import media_host, story_image
        from .jobs.media_repeat_sweep import _gym_name
        caption = (row.get("caption") or "").strip()
        asset = story_image.get_or_make_story_video(path, caption, _gym_name(base_key),
                                                     lib, logger=_log)
        if not asset or not config.hosting_enabled():
            return None, None
        delivered_url = media_host.host_media(asset, f"{base_key}_ig")
        if not raw_url or not delivered_url:
            return None, None
        with open(path, "rb") as fh:
            source_bytes = fh.read()
        with open(asset, "rb") as fh:
            rendered = fh.read()
        evidence = _render_evidence(raw_url, delivered_url, source_bytes, rendered,
                                    "reburn", deadline=deadline)
        return (delivered_url, evidence) if evidence else (None, None)
    except SwapDeadline:
        raise
    except Exception as exc:  # noqa: BLE001
        _log(f"{base_key}: story video evidence unavailable ({type(exc).__name__})")
        return None, None


def _feed_render_evidence(path, source_url, delivered_url, *, rendered_bytes=None,
                          deadline=None):
    """Verify exact source and locally rendered bytes against the hosted delivery."""
    try:
        with open(path, "rb") as fh:
            source_bytes = fh.read()
        if rendered_bytes is None:
            return None
        delivered_bytes = _read_evidence_url(delivered_url, deadline)
        import hashlib, uuid
        if not source_bytes or not rendered_bytes or not delivered_bytes or not delivered_url:
            return None
        if _read_evidence_url(source_url, deadline) != source_bytes:
            return None
        if delivered_bytes != rendered_bytes:
            return None
        return {"source_exact_url": source_url, "delivered_exact_url": delivered_url,
                "source_fingerprint": "md5:" + hashlib.md5(source_bytes).hexdigest(),
                "delivered_fingerprint": "md5:" + hashlib.md5(delivered_bytes).hexdigest(),
                "source_byte_length": len(source_bytes),
                "delivered_byte_length": len(delivered_bytes), "operation": "render",
                "evidence_ref": "media_swap:" + str(uuid.uuid4()),
                "observed_by": "media_swap", "rendered_by": "media_swap"}
    except SwapDeadline:
        raise
    except Exception:
        return None


def _reburn_story_video(base_key, row, video_path, lib):
    """Burn the story's caption onto the NEW video (9:16 story video) and host it.
    None on failure. The video counterpart of media_repeat_sweep._reburn_story."""
    try:
        from . import media_host, story_image
        from .jobs.media_repeat_sweep import _gym_name
        caption = (row.get("caption") or "").strip()
        asset = story_image.get_or_make_story_video(
            video_path, caption, _gym_name(base_key), lib or os.path.dirname(video_path),
            logger=_log)
        if not asset or not config.hosting_enabled():
            return None
        return media_host.host_media(asset, f"{base_key}_ig") or None
    except Exception as exc:  # noqa: BLE001
        _log(f"{base_key}: story video re-burn error ({type(exc).__name__})")
        return None


def _cleanup(work):
    try:
        for name in os.listdir(work):
            try:
                os.unlink(os.path.join(work, name))
            except OSError:
                pass
        os.rmdir(work)
    except OSError:
        pass


# ---- after the write --------------------------------------------------------------
def swap_fields(pick):
    """The extra content_calendar columns a successful pick must carry onto the row
    (SupabaseCalendarStore.swap_media extra_fields). ALWAYS both keys: a video row
    that becomes a photo must have its stale poster CLEARED, and a Drive row that
    becomes a local-library row must stop being tracked as that asset."""
    return {"thumbnail_url": (pick or {}).get("thumbnail_url") or None,
            "source_media_asset_id": (pick or {}).get("source_media_asset_id") or None}


def after_swap(base_key, row, pick, *, media_store=None, now=None, book_rows=None,
               swapped_ids=()):
    """Settle the usage ledgers once the row write actually happened: stamp the Drive
    asset now on the row (so the once-used rule and the deny bookkeeping see it), and
    settle the use-record of the asset the row USED to carry on this date ONLY when no
    live row on the book still carries it (audit 3c: the FB mirror / paired story keep
    the old asset when they were approved or the sibling swap failed). Stage-use is
    PERMANENT (2026-10-02): settling no longer restores the old asset's counters —
    a swapped-out asset is never offered again, exactly like a published one — the
    record is only marked rolled_back so repeated settles are idempotent. Record a
    local pick as served. Best effort, never raises.

    book_rows: the gym's rows after the swaps (the caller re-reads). None means the
    read FAILED = unknown: the old asset's record is left unsettled (harmless; its
    permanent stamp already keeps it out of the pool). An empty list is a real
    "nothing else carries it".
    swapped_ids: the rows this swap just repointed (they now carry the NEW asset even
    if the caller's read predates the write)."""
    pd = str((row or {}).get("post_date") or "")[:10]
    if not pd:
        return
    try:
        from . import gym_media_selector as _sel
        old = media_guard.row_asset_key(row)
        new = (pick or {}).get("source_media_asset_id") or ""
        if book_rows is None:
            still_carried = bool(old)            # unknown book: never roll back
        else:
            still_carried = book_carries_asset(book_rows, old, except_ids=swapped_ids)
        if old and old != new and not still_carried:
            _sel.rollback_use(base_key, pd, store=media_store, asset_id=old)
        elif old and old != new:
            _log(f"{base_key}: asset {old} "
                 + ("book unreadable" if book_rows is None
                    else f"still carried by a sibling row on {pd}") + "; left stamped")
        if (new and (pick or {}).get("source") == "drive"
                and not (pick or {}).get("_drive_stamped")):
            store = media_store
            if store is None:
                from . import gym_media_index as _idx
                store = _idx.default_store()
            asset = store.get_asset(new) or {"id": new}
            _sel.stamp_use(asset, base_key, pd, store=store, now=now)
    except Exception as exc:  # noqa: BLE001
        _log(f"{base_key}: Drive usage ledger not settled ({type(exc).__name__})")
    # The row write is confirmed by the caller. The prewrite claim remains
    # protective if this receipt fails; no retry can re-offer the same asset.
    if (pick or {}).get("_drive_stamped") and (pick or {}).get("_drive_claim_id"):
        try:
            from . import db
            db.socialapi_claim_done(
                pick["_drive_claim_id"], pick["_drive_claim_account"],
                str(pick["source_media_asset_id"]))
        except Exception as exc:  # noqa: BLE001
            _log(f"{base_key}: Drive swap claim receipt failed ({type(exc).__name__})")
    if ((pick or {}).get("source") == "local" and (pick or {}).get("path")
            and not (pick or {}).get("_served_reserved")):
        try:
            from . import dam, rotation
            # Compatibility callers reaching post-write settlement without the
            # prewrite reservation cannot create a duplicate served entry.
            reserve = (rotation.reserve_local_media_once if pick.get("kind") == "video"
                       else rotation.reserve_local_photo_once)
            if reserve(f"{base_key}_ig", dam.rotation_key(pick["path"]),
                       "", pd, path=pick["path"]) is None:
                _log(f"{base_key}: local swap had no prewrite reservation; "
                     "served ledger held or already consumed")
        except Exception:  # noqa: BLE001
            pass


def reserve_local_pick(base_key, row, pick):
    """Durably reserve a swap candidate before any calendar row is changed.

    Drive claims use a SQLite unique key across planners and swaps. A failed
    claim or usage stamp holds the write; the claim survives ambiguous outcomes.
    """
    source = (pick or {}).get("source")
    day = str((row or {}).get("post_date") or "")[:10]
    if source == "drive":
        asset_id = str((pick or {}).get("source_media_asset_id") or "")
        if not asset_id or not day:
            return False
        try:
            from . import db, gym_media_index as _idx, gym_media_selector as _sel
            base = _sel.base_gym_key(base_key)
            store = _idx.default_store()
            asset = store.get_asset(asset_id)
            if asset is None:
                return False
            claim_id = _sel.claim_drive_content(base, asset, store)
            if claim_id is None:
                return False
            claim_account = f"{base}_gbp"
            pick["_drive_claim_id"] = claim_id
            pick["_drive_claim_account"] = claim_account
            _sel.stamp_use(asset, base_key, day, store=store)
        except Exception as exc:  # noqa: BLE001
            _log(f"{base_key}: Drive swap usage stamp failed before row write "
                 f"({type(exc).__name__}); swap held")
            return False
        pick["_drive_stamped"] = True
        pick["_drive_stamp_store"] = store
        pick["_drive_stamp_day"] = day
        pick["_drive_stamp_base"] = base_key
        return True
    if source != "local":
        return True
    path = str((pick or {}).get("path") or "")
    if not path or not day:
        return False
    try:
        from . import dam, rotation
        reserve = (rotation.reserve_local_media_once if pick.get("kind") == "video"
                   else rotation.reserve_local_photo_once)
        reservation_id = reserve(
            f"{base_key}_ig", dam.rotation_key(path), "", day, path=path)
    except Exception:  # noqa: BLE001
        reservation_id = None
    if reservation_id:
        pick["_served_reserved"] = True
        pick["_served_reservation_id"] = reservation_id
    return bool(reservation_id)


def release_local_pick(pick):
    """Release an exact reservation only when no calendar row exposed it."""
    if (pick or {}).get("_drive_stamped"):
        try:
            from . import gym_media_selector as _sel
            released = _sel.rollback_use(
                pick.get("_drive_stamp_base"), pick.get("_drive_stamp_day"),
                store=pick.get("_drive_stamp_store"),
                asset_id=pick.get("source_media_asset_id"),
                restore_unstaged=True)
        except Exception:  # noqa: BLE001
            return False
        if released:
            from . import db
            claim_id = pick.get("_drive_claim_id")
            if claim_id:
                db.socialapi_claim_release(claim_id, pick["_drive_claim_account"])
                pick.pop("_drive_claim_id", None)
                pick.pop("_drive_claim_account", None)
            pick.pop("_drive_stamped", None)
            pick.pop("_drive_stamp_store", None)
            pick.pop("_drive_stamp_day", None)
            pick.pop("_drive_stamp_base", None)
        return released
    reservation_id = (pick or {}).get("_served_reservation_id")
    if not reservation_id:
        return (pick or {}).get("source") != "local"
    from . import rotation
    released = rotation.release_served(reservation_id)
    if released:
        pick.pop("_served_reservation_id", None)
        pick.pop("_served_reserved", None)
    return released


def client_message(reason, base_key=""):
    """What the gym owner reads when a swap could not happen. Plain, actionable,
    and never blaming them for a system gap."""
    del base_key
    if reason == REASON_NO_FRESH_PHOTO:
        return ("No unused approved photo or video is available for this swap. "
                "Echo does not reuse media once it has been placed on your calendar. "
                "Add fresh photos or videos (connect your Drive folder or upload "
                "in the portal) and try again. Your post is unchanged and your "
                "recreates were not touched.")
    if reason == REASON_NO_LIBRARY:
        return ("Your media library is not connected yet, so there is nothing to "
                "swap in. Upload photos or videos in the portal and try again. Your "
                "recreates were not touched.")
    if reason == REASON_STORY_REBURN:
        return ("Echo could not rebuild the story card on the new media, so nothing "
                "was changed. Try again shortly. Your recreates were not touched.")
    if reason == REASON_ASSET_PREP:
        return ("Echo found fresh media to swap in but could not get it ready in "
                "time (a download or conversion did not finish), so nothing was "
                "changed. Try again in a few minutes. Your recreates were not "
                "touched.")
    if reason == REASON_TIMEOUT:
        return ("Echo could not prepare a fresh photo or video within the time it "
                "allows itself, so nothing was changed. Try again in a minute. Your "
                "recreates were not touched.")
    return ("Echo could not swap the media right now, so nothing was changed. Try "
            "again shortly. Your recreates were not touched.")


__all__ = ["enabled", "pick_replacement", "candidates_for", "order_candidates",
           "local_candidates", "drive_candidates", "swap_fields", "after_swap",
           "reserve_local_pick", "release_local_pick",
           "sibling_rows", "book_carries_asset", "has_rendition",
           "SWAP_TRANSCODE_TIMEOUT_SEC", "SWAP_REQUEST_DEADLINE_SEC",
           "SWAP_DOWNLOAD_TIMEOUT_SEC", "REASON_TIMEOUT", "SwapDeadline",
           "library_path_for", "client_message", "is_video",
           "REASON_NO_LIBRARY", "REASON_NO_FRESH_PHOTO", "REASON_HOSTING",
           "REASON_STORY_REBURN", "REASON_ASSET_PREP"]
