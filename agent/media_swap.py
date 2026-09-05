"""
media_swap.py — SWAPPING A WRONG PHOTO IS FREE (B6).

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

WHAT THIS MODULE DOES: pick a genuinely fresh photo for ONE waiting row and
return the urls to point it at. It does NOT write, does NOT publish, and does
NOT touch the budget — the caller (portal_social.handle_swap_media) owns the
write through SupabaseCalendarStore.swap_media, which is status-guarded
server-side to pending / coach_review so an approved or live row can never be
repointed.

ONE RECIPE, NOT TWO. The pick / near-dupe / re-burn rules are IMPORTED from
agent.jobs.media_repeat_sweep rather than restated here, so the client-initiated
swap and the nightly cross-day sweep can never drift apart on what counts as a
fresh photo. media_guard supplies the book state, so a swap can never land a
photo that already sits on another day (the exact repeat B5 is about).

Flag: config.media_swap_free_enabled() (ECHO_MEDIA_SWAP_FREE, DEFAULT OFF).
This is a NEW client capability, so it ships dark and is armed by hand.
"""

from . import config, media_guard

# Why a reason code and not an exception: the portal answers a client, and every
# outcome here is a normal thing that can happen to a real gym.
REASON_NO_LIBRARY = "no_library"
REASON_NO_FRESH_PHOTO = "no_fresh_photo"
REASON_HOSTING = "hosting_unavailable"
REASON_STORY_REBURN = "story_reburn_failed"


def enabled():
    return config.media_swap_free_enabled()


def _log(msg):
    print(f"[media-swap] {msg}")


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


def pick_replacement(base_key, row, *, store, library_path=None, book_state=None,
                     fresh_fn=None, host_fn=None, feed_fn=None, reburn_fn=None,
                     log=None):
    """A fresh photo for ONE waiting row.

    Returns {"ok": True, "image_url":..., "source_media_url":..., "key":...} or
    {"ok": False, "reason": <REASON_*>}. Never writes, never publishes, never
    spends budget. Every piece of I/O is injectable so this is testable offline.

    The replacement is chosen from photos that appear NOWHERE on the gym's book
    (media_guard.book_state) and are not near-dupes of one, so a swap can never
    trade one repeat for another. A story is re-burned with its OWN caption onto
    the new photo; a feed gets the same autofit reframe the original shipped
    with, so the swap is visually like for like.
    """
    say = log or _log
    lib = library_path if library_path is not None else library_path_for(base_key)
    if not lib:
        return {"ok": False, "reason": REASON_NO_LIBRARY}

    from .jobs import media_repeat_sweep as _sweep
    fresh_fn = fresh_fn or _sweep._fresh_photo
    reburn_fn = reburn_fn or _sweep._reburn_story

    pd = str((row or {}).get("post_date") or "")[:10]
    if book_state is None:
        try:
            from datetime import date as _date
            start = _date.fromisoformat(pd) if pd else None
        except ValueError:
            start = None
        book_state = media_guard.book_state(base_key, store, start, 1, log=say,
                                            library_path=lib) if start else {}

    # The row's CURRENT photo is excluded explicitly: "give me a different one"
    # must never hand back the same picture.
    current = media_guard.row_media_key(row)
    key, path = fresh_fn(lib, book_state or {}, {current} if current else set())
    if not key or not path:
        return {"ok": False, "reason": REASON_NO_FRESH_PHOTO}

    hosted = ""
    if host_fn is not None:
        hosted = host_fn(path) or ""
    else:
        try:
            from . import media_host
            if config.hosting_enabled():
                hosted = media_host.host_media(path, f"{base_key}_ig") or ""
        except Exception as exc:  # noqa: BLE001
            say(f"{base_key}: hosting failed for {key} ({type(exc).__name__})")
    if not hosted:
        return {"ok": False, "reason": REASON_HOSTING}

    fmt = str((row or {}).get("format") or "feed").strip().lower()
    if fmt == "story":
        # A story publishes empty-body, so its caption lives ON the photo. Swapping
        # the pixels without re-burning would ship a captionless story.
        if config.story_format_enabled():
            burned = reburn_fn(base_key, row, path, lib)
            if not burned:
                return {"ok": False, "reason": REASON_STORY_REBURN}
            target = burned
        else:
            target = hosted
        src = hosted if config.story_source_media_enabled() else None
        return {"ok": True, "image_url": target, "source_media_url": src, "key": key}

    # FEED AUTOFIT PARITY: the original shipped through the square reframe, so the
    # replacement gets it too. Any failure keeps the raw hosted photo (never a drop).
    target = hosted
    if feed_fn is not None:
        target = feed_fn(path) or hosted
    elif config.feed_autofit_enabled():
        try:
            from . import feed_image, media_host
            asset = feed_image.get_or_make_feed_image(path, lib, logger=say)
            if asset:
                reframed = media_host.host_media(asset, f"{base_key}_ig")
                if reframed:
                    target = reframed
        except Exception:  # noqa: BLE001 - the raw hosted photo is a correct answer
            pass
    return {"ok": True, "image_url": target, "source_media_url": None, "key": key}


def client_message(reason, base_key=""):
    """What the gym owner reads when a swap could not happen. Plain, actionable,
    and never blaming them for a system gap."""
    del base_key
    if reason == REASON_NO_FRESH_PHOTO:
        return ("Every other photo in your library is already used on another day "
                "of this month. Add photos (connect your Drive folder or upload in "
                "the portal) and try again. Your post is unchanged and your "
                "recreates were not touched.")
    if reason == REASON_NO_LIBRARY:
        return ("Your photo library is not connected yet, so there is nothing to "
                "swap in. Upload photos in the portal and try again. Your recreates "
                "were not touched.")
    if reason == REASON_STORY_REBURN:
        return ("Echo could not rebuild the story card on the new photo, so nothing "
                "was changed. Try again shortly. Your recreates were not touched.")
    return ("Echo could not swap the photo right now, so nothing was changed. Try "
            "again shortly. Your recreates were not touched.")


__all__ = ["enabled", "pick_replacement", "library_path_for", "client_message",
           "REASON_NO_LIBRARY", "REASON_NO_FRESH_PHOTO", "REASON_HOSTING",
           "REASON_STORY_REBURN"]
