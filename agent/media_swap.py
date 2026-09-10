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
REASON_HOSTING = "hosting_unavailable"
REASON_STORY_REBURN = "story_reburn_failed"

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_MAX_MATERIALIZE_ATTEMPTS = 3      # a corrupt download / failed probe tries the next


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
    try:
        from . import rotation
        served = rotation.load_served()
    except Exception:  # noqa: BLE001 - a ledger read failure just means "never served"
        return {}
    out = {}
    for acct in (base_key, f"{base_key}_ig", f"{base_key}_fb"):
        for e in served.get(acct, []) or []:
            k, d = e.get("key"), str(e.get("date") or "")
            if k and d > out.get(k, ""):
                out[k] = d
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


def local_candidates(base_key, lib, post_date, blocked_keys):
    """The gym's local-library creatives a swap may use for a row on post_date."""
    if not lib or not os.path.isdir(lib):
        return []
    from . import dam, rotation
    from datetime import date as _date, timedelta
    window = config.media_repeat_window_days()
    try:
        floor = (_date.fromisoformat(post_date) - timedelta(days=window)).isoformat()
    except (TypeError, ValueError):
        floor = ""
    try:
        excl = set(rotation.style_exclusions(lib))
    except Exception:  # noqa: BLE001
        excl = set()
    served = _last_served_local(base_key)
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
        try:
            rk = dam.rotation_key(path)
        except Exception:  # noqa: BLE001
            rk = key
        last = served.get(rk, "")
        if last and floor and last >= floor:
            continue                      # served inside the repeat window
        out.append({"source": "local", "kind": kind, "key": key, "path": path,
                    "last_used": last, "used_count": 1 if last else 0, "name": key})
    return out


def drive_candidates(base_key, blocked_ids, *, media_store=None, now=None):
    """The gym's Drive-pool assets a swap may use: gym_media_selector.pickable (eligible,
    not hidden, outside the 90-day cooldown, not used this month), minus everything
    already on the book. Never raises; an unarmed store is an empty list."""
    try:
        from . import gym_media_selector as _sel
        assets = _sel.pickable(base_key, store=media_store, now=now,
                               exclude_ids=tuple(blocked_ids))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in assets:
        kind = str(a.get("kind") or "")
        if kind not in ("photo", "video"):
            continue
        out.append({"source": "drive", "kind": kind, "key": str(a.get("id")),
                    "asset": a, "last_used": str(a.get("last_used_at") or "")[:10],
                    "used_count": int(a.get("used_count") or 0),
                    "name": str(a.get("title") or a.get("id") or "")})
    return out


def order_candidates(cands, *, current_is_video):
    """Least recently used first (never used wins), then least used, then name.
    When the row holds a STILL and any video is available, only videos remain."""
    cands = list(cands or [])
    if not current_is_video and any(c["kind"] == "video" for c in cands):
        cands = [c for c in cands if c["kind"] == "video"]
    cands.sort(key=lambda c: (c.get("last_used") or "", int(c.get("used_count") or 0),
                              str(c.get("name") or "")))
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
    return order_candidates(cands, current_is_video=is_video(current))


# ---- materialize (a local file + maybe an already-hosted url) ------------------------
def _materialize(base_key, cand, work_dir, *, drive=None, media_store=None):
    """{"path": local file, "hosted": url or None} for one candidate, or None when it
    cannot be used (download failed, video failed its probe gate). A Drive asset is
    downloaded into work_dir; a HEIC/HEVC original gets its cached rendition (already
    hosted) via gym_media_index.ensure_rendition, exactly like the Drive builder."""
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
    try:
        drive.download(asset["id"], path)
    except Exception as exc:  # noqa: BLE001
        _log(f"{base_key}: Drive download failed for {title!r} ({type(exc).__name__})")
        return None
    store = media_store or _idx.default_store()
    hosted, _converted = _idx.ensure_rendition(asset, path, store=store,
                                               probe_fn=_idx.probe_video)
    if asset.get("kind") == _idx.KIND_VIDEO:
        info = _idx.probe_video(path)
        if not info:
            return None                       # unprobed never ships (fail closed)
        el, _reason, _label = _idx.video_eligibility(
            os.path.getsize(path) or asset.get("size_bytes"),
            info["duration_sec"], info["width"], info["height"])
        if el is not True:
            return None
        if not hosted and not is_publishable_video(title):
            return None                       # .webm/.avi/.mkv with no .mp4 rendition
    elif not hosted and _idx.is_heic(title, asset.get("mime_type")):
        return None                           # HEIC with no converter: not servable
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
                     siblings=()):
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
    work = tempfile.mkdtemp(prefix="mediaswap_")
    try:
        for cand in cands[:_MAX_MATERIALIZE_ATTEMPTS]:
            mat = (materialize_fn(cand) if materialize_fn is not None
                   else _materialize(base_key, cand, work, drive=drive,
                                     media_store=media_store))
            if not mat or not mat.get("path"):
                continue
            path = mat["path"]
            # Drive assets host under the gym base (builder parity, so the same bytes
            # dedupe to one object); local library photos keep the _ig tenant the
            # sweep and the month build have always used.
            tenant = base_key if cand["source"] == "drive" else f"{base_key}_ig"
            hosted = mat.get("hosted") or ""
            if not hosted:
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
            poster = (poster_fn(path, work, tenant) or "") if cand["kind"] == "video" else ""
            out = _finish(base_key, row, fmt, cand, path, hosted, lib, work, tenant,
                          poster=poster, feed_fn=feed_fn, reburn_fn=reburn_fn, log=say)
            if not out.get("ok"):
                return out
            out["siblings"] = {}
            for sib in siblings or ():
                sfmt = str((sib or {}).get("format") or "feed").strip().lower()
                out["siblings"][str(sib.get("id"))] = _finish(
                    base_key, sib, sfmt, cand, path, hosted, lib, work, tenant,
                    poster=poster, feed_fn=feed_fn, reburn_fn=reburn_fn, log=say)
            return out
        return {"ok": False, "reason": REASON_NO_FRESH_PHOTO}
    finally:
        _cleanup(work)


def _finish(base_key, row, fmt, cand, path, hosted, lib, work, tenant, *, poster,
            feed_fn, reburn_fn, log):
    """Shape the hosted replacement for the row's format: a story is re-burned with
    its caption (still card or 9:16 story video), a video feed ships the hosted video,
    a photo feed gets the autofit reframe. A failed story re-burn is a hard stop
    (REASON_STORY_REBURN), matching the pre-existing contract: never a bare story."""
    video = cand["kind"] == "video"
    base = {"key": cand["key"], "kind": cand["kind"], "source": cand["source"],
            "thumbnail_url": poster if (video and fmt != "story") else "", "path": path,
            "source_media_asset_id": cand["key"] if cand["source"] == "drive" else ""}
    if fmt == "story":
        # A story publishes empty-body, so its caption lives ON the media. Swapping
        # the pixels without re-burning would ship a captionless story.
        if config.story_format_enabled():
            burned = (_reburn_story_video(base_key, row, path, lib) if video
                      else reburn_fn(base_key, row, path, lib))
            if not burned:
                return {"ok": False, "reason": REASON_STORY_REBURN}
            target = burned
        else:
            target = hosted
        src = hosted if config.story_source_media_enabled() else None
        return {"ok": True, "image_url": target, "source_media_url": src, **base}

    if video:
        # A video feed ships the hosted video itself (autofit is a still-photo lane).
        return {"ok": True, "image_url": hosted, "source_media_url": None, **base}

    # FEED AUTOFIT PARITY: the original shipped through the square reframe, so the
    # replacement gets it too. Any failure keeps the raw hosted photo (never a drop).
    target = hosted
    if feed_fn is not None:
        target = feed_fn(path) or hosted
    elif config.feed_autofit_enabled():
        try:
            from . import feed_image, media_host
            asset = feed_image.get_or_make_feed_image(path, lib or work, logger=log)
            if asset:
                reframed = media_host.host_media(asset, tenant)
                if reframed:
                    target = reframed
        except Exception:  # noqa: BLE001 - the raw hosted photo is a correct answer
            pass
    return {"ok": True, "image_url": target, "source_media_url": None, **base}


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
    asset now on the row (so the 90-day cooldown and the deny rollback see it), roll
    back the asset the row USED to carry on this date ONLY when no live row on the
    book still carries it (audit 3c: the FB mirror / paired story keep the old asset
    when they were approved or the sibling swap failed; a rollback then would let
    pick_media re-stage the very asset the client just rejected), and record a local
    pick as served. Best effort, never raises.

    book_rows: the gym's rows after the swaps (the caller re-reads); swapped_ids: the
    rows this swap just repointed (they now carry the NEW asset even if the caller's
    read predates the write)."""
    pd = str((row or {}).get("post_date") or "")[:10]
    if not pd:
        return
    try:
        from . import gym_media_selector as _sel
        old = media_guard.row_asset_key(row)
        new = (pick or {}).get("source_media_asset_id") or ""
        still_carried = (book_rows is not None
                         and book_carries_asset(book_rows, old, except_ids=swapped_ids))
        if old and old != new and not still_carried:
            _sel.rollback_use(base_key, pd, store=media_store, asset_id=old)
        elif old and old != new:
            _log(f"{base_key}: asset {old} still carried by a sibling row on {pd}; "
                 "left stamped")
        if new and (pick or {}).get("source") == "drive":
            store = media_store
            if store is None:
                from . import gym_media_index as _idx
                store = _idx.default_store()
            asset = store.get_asset(new) or {"id": new}
            _sel.stamp_use(asset, base_key, pd, store=store, now=now)
    except Exception as exc:  # noqa: BLE001
        _log(f"{base_key}: Drive usage ledger not settled ({type(exc).__name__})")
    if (pick or {}).get("source") == "local" and (pick or {}).get("path"):
        try:
            from . import dam, rotation
            rotation.record_served(f"{base_key}_ig", dam.rotation_key(pick["path"]),
                                   "", pd)
        except Exception:  # noqa: BLE001
            pass


def client_message(reason, base_key=""):
    """What the gym owner reads when a swap could not happen. Plain, actionable,
    and never blaming them for a system gap."""
    del base_key
    if reason == REASON_NO_FRESH_PHOTO:
        return ("Every other photo and video Echo can reach is already on another day "
                "of this month or ran recently. Add media (connect your Drive folder or "
                "upload in the portal) and try again. Your post is unchanged and your "
                "recreates were not touched.")
    if reason == REASON_NO_LIBRARY:
        return ("Your media library is not connected yet, so there is nothing to "
                "swap in. Upload photos or videos in the portal and try again. Your "
                "recreates were not touched.")
    if reason == REASON_STORY_REBURN:
        return ("Echo could not rebuild the story card on the new media, so nothing "
                "was changed. Try again shortly. Your recreates were not touched.")
    return ("Echo could not swap the media right now, so nothing was changed. Try "
            "again shortly. Your recreates were not touched.")


__all__ = ["enabled", "pick_replacement", "candidates_for", "order_candidates",
           "local_candidates", "drive_candidates", "swap_fields", "after_swap",
           "sibling_rows", "book_carries_asset",
           "library_path_for", "client_message", "is_video",
           "REASON_NO_LIBRARY", "REASON_NO_FRESH_PHOTO", "REASON_HOSTING",
           "REASON_STORY_REBURN"]
