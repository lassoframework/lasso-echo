"""
client_month_run.py: assemble a full month of APPROVABLE DRAFT calendar rows for a
CLIENT gym FROM THAT GYM'S OWN UPLOADED PHOTOS/VIDEOS, and upsert them to the shared
content_calendar.

NEW RULE (Blake, 2026-08): a CLIENT gym's Organic Social calendar is built ONLY from
the gym's OWN uploaded media. Echo NEVER renders an infographic-only calendar for a
client. A client with NO uploaded media does not get a calendar at all: the builder
WAITS (writes nothing) and reports awaiting_media, so the portal shows a red "upload
your media" banner. The house-infographic fallback is for LASSO's OWN dogfood calendar
only, never a client.

Behind AGENT_CLIENT_MONTH (config.client_month_enabled(), default OFF) AND it also
requires config.client_sources_enabled(). Flag off -> build_client_month returns
ok:False and touches nothing: no render, no host, no calendar write.

WHAT IT DOES (mirrors real_month_planner / real_calendar_mirror exactly):
  * MEDIA-REQUIRED GUARD: before anything, count the gym's uploaded media files in
    library_path. Zero usable media -> WAIT: return {ok:False, awaiting_media:True}
    and touch nothing (no render, no host, no calendar write, no delete).
  * With media, for each of `days` days build a FEED draft and a paired STORY draft
    via client_content.build_client_draft (NO template_fn: the day uses the gym's
    REAL uploaded photo via client_content.pick_image). A day is emitted ONLY when its
    draft carries a REAL creative (creative_public_url set AND not needs_media). A day
    with no photo is SKIPPED and logged ("held: no client photo for the day"), NEVER
    infographic-filled.
  * BANNED-WORD GUARD: a draft whose caption contains any of the gym's banned words
    (case-insensitive, word-boundary) is DROPPED for the day and logged: the word is
    NEVER emitted. The guard first tries the OTHER approved sources/categories for the
    day (a clean source fills the slot) before dropping the day entirely.
  * Map the surviving drafts to content_calendar rows using real_calendar_mirror's row
    shape (gym_id = the tenant BASE, account = platform, format feed|story, caption,
    image_url, status), with the SAME FB mirror the real month uses: a FEED lands on
    instagram AND facebook; a STORY is instagram-only. Rows carry NO id (the DB mints
    the uuid) and every row is PAUSED (status 'pending': never approved/published).
  * Apply via the injectable store: delete_month(base, month) then insert_rows(base,
    rows), gym-scoped, delete-then-insert: mirror apply_month_plan.

THREE KEYS (do not conflate): read intake by BASE; generate under Account.key
(gritx_ig); write content_calendar rows with gym_id = BASE (gritx).

HARD RULES: no fabrication (captions come only from approved sources; a banned word is
never emitted), NO infographic is ever produced for a client, nothing publishes, no
gate weakened, every draft PAUSED for approval. The store is injectable so the whole
path is offline-testable.
"""

import os
import re

from . import client_content, config
from . import real_calendar_mirror as _mirror

# Media extensions that count as a client having uploaded usable creative.
_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}


def _base_of(account_key):
    """The tenant base for an account key ('gritx_ig' -> 'gritx')."""
    account_key = (account_key or "").strip()
    for suffix in ("_ig", "_fb"):
        if account_key.endswith(suffix):
            return account_key[: -len(suffix)]
    return account_key


def _client_media_count(library_path):
    """Count the gym's uploaded media files (images + videos) in library_path.

    Counts only regular files whose extension is in _MEDIA_EXTS. A missing, empty, or
    unreadable directory (or an empty/None path) is 0. Never raises."""
    path = library_path
    if not path or not os.path.isdir(path):
        return 0
    count = 0
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(name)[1].lower() in _MEDIA_EXTS:
                count += 1
    except OSError:
        return 0
    return count


def client_awaiting_media(base_key, library_path):
    """True when a CLIENT gym has NO usable uploaded media (so Echo must WAIT and the
    portal must show the red "upload your media" banner). Callers/signal use this."""
    return _client_media_count(library_path) <= 0


def _has_banned_word(text, banned_words):
    """True when `text` contains any banned word as a whole word (case-insensitive).
    Word-boundary so 'compete' matches 'compete!' but not 'competent'. Empty banned
    list -> never True."""
    if not banned_words:
        return False
    low = (text or "").lower()
    for w in banned_words:
        w = (w or "").strip().lower()
        if not w:
            continue
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def _url_basename(url):
    """The filename a public media URL points at (query string stripped). Hosted client
    media keeps its library basename, so this is the join key between a calendar row's
    image_url and the library creative it came from."""
    return (url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


# Statuses whose PHOTO is genuinely consumed: the post is (or is about to be) live on
# the gym's page, so re-placing its photo on another day would double-post. A DENIED or
# KILLED row's photo is NOT consumed — the client rejected that caption, not the photo,
# and excluding it forever would silently shrink the gym's usable library.
_PHOTO_CONSUMING_STATUSES = ("approved", "published", "publishing")


def _locked_calendar_state(base_key, start, days, store, log):
    """(locked_feed_days, used_keys) from the gym's EXISTING human-owned calendar rows
    across the planned span. locked_feed_days: post_dates whose feed a human already
    owns (approved/published/denied/killed — anything not machine-wipeable), so the
    rebuild never plans a competing feed there. used_keys: the media basenames carried
    by rows whose photo is truly consumed (approved/published/publishing, any format),
    so a live photo is never re-picked; a denied/killed photo stays available.
    Read-only; a read failure returns empty state (the store-level preserve_and_prune
    backstop still guards the write)."""
    from datetime import timedelta
    from .portal_calendar_store import _WIPEABLE_STATUSES
    locked_days, used = set(), set()
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return locked_days, used
    months = sorted({(start + timedelta(days=i)).isoformat()[:7]
                     for i in range(max(1, days))})
    for month in months:
        try:
            rows = list_month(base_key, month) or []
        except Exception as exc:  # noqa: BLE001 - never block the build on a read
            log(f"locked-state read failed for {month}: {type(exc).__name__}")
            continue
        for row in rows:
            status = str((row or {}).get("status") or "").lower()
            if not status or status in _WIPEABLE_STATUSES:
                continue
            if str(row.get("format") or "").lower() == "feed":
                locked_days.add(str(row.get("post_date") or "")[:10])
            if status in _PHOTO_CONSUMING_STATUSES:
                key = _url_basename(row.get("image_url") or "")
                if key:
                    used.add(key)
    locked_days.discard("")
    return locked_days, used


def _edited_story_captions(base_key, start, days, store, log):
    """{post_date -> caption} for STORY rows the client edited in the portal but which
    have NOT been re-rendered yet. Editing a story caption (portal_calendar_store.
    patch_caption) resets the row to 'pending' and updates content_calendar.caption, but
    the burned media still carries the OLD caption. A rebuild would otherwise re-render
    the story from the FRESH feed caption and silently discard the client's edit. We
    read the client's edited story caption here so the rebuild RE-RENDERS the story with
    the CLIENT'S text (Dale, 2026-08-17: 'I added a story caption and saved but it did
    not show'). Only a story whose caption differs from its paired feed's caption is
    treated as edited (an unedited paired story matches the feed by construction).
    Read-only; a read failure returns {} (the rebuild falls back to the feed caption)."""
    from datetime import timedelta
    edited = {}
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return edited
    months = sorted({(start + timedelta(days=i)).isoformat()[:7]
                     for i in range(max(1, days))})
    for month in months:
        try:
            rows = list_month(base_key, month) or []
        except Exception as exc:  # noqa: BLE001 - never block the build on a read
            log(f"edited-story read failed for {month}: {type(exc).__name__}")
            continue
        feeds_by_date = {}
        stories_by_date = {}
        for row in rows:
            fmt = str(row.get("format") or "").lower()
            pd = str(row.get("post_date") or "")[:10]
            if not pd:
                continue
            if fmt == "feed":
                feeds_by_date.setdefault(pd, row.get("caption") or "")
            elif fmt == "story":
                stories_by_date[pd] = row.get("caption") or ""
        for pd, story_cap in stories_by_date.items():
            story_cap = (story_cap or "").strip()
            feed_cap = (feeds_by_date.get(pd) or "").strip()
            # An edited story caption is one that differs from the paired feed caption
            # (an unedited paired story is cloned FROM the feed, so it matches).
            if story_cap and story_cap != feed_cap:
                edited[pd] = story_cap
    return edited


def _has_real_creative(draft):
    """True when the draft carries a REAL uploaded creative: a hosted/public url AND it
    is NOT a needs-media (no-image) draft. This is what makes a day emit a row; a day
    with no client photo has no real creative and is skipped, never infographic-filled."""
    if draft is None:
        return False
    if getattr(draft, "needs_media", False):
        return False
    return bool((getattr(draft, "creative_public_url", "") or "").strip())


def _clean_draft_for_day(account, day_key, voice, library_path, banned_words, log,
                         exclude_keys=(), avoid_openings=()):
    """Build a draft for the day, from the gym's OWN uploaded photo (NO template_fn),
    whose caption carries NO banned word, preferring a different approved source/category
    over dropping the day.

    client_content.build_client_draft rotates category+source deterministically per day
    and pairs the day's fact with a real image from the gym's library (pick_image). To
    try the OTHER sources for the day without duplicating that private logic, we ask the
    builder for the day; if its caption is banned, we walk neighbouring day keys (same
    weekday cadence advances the source cycle) and re-ask, up to a bounded number of
    attempts, re-homing the clean draft onto the real day. Any draft whose caption still
    carries a banned word is DROPPED (never emitted).

    Returns (draft, dropped_reason). draft is None when no clean draft could be built.

    A+ GATE: a draft is accepted only when it passes post_quality.is_a_plus (a REAL
    caption + real media + no dash + no banned word), NOT merely the banned-word check.
    A thin caption (e.g. the raw 'HYROX' source when SB7 could not write a real one) is
    treated like a banned draft: we walk neighbouring days for a better source, and drop
    the day if none qualifies. No gym ever gets a sub-par post on its calendar.

    avoid_openings (Ryan Parr, 2026-08-17): opening phrases already used on this build's
    earlier accepted days, threaded to the caption generator so this day does not lead
    with the same hook as its neighbours. STYLE-only guidance; never blocks a day."""
    from . import post_quality

    def _accept(d):
        if d is None:
            return False
        # A+ caption gate is enforced whenever the real-caption engine (SB7) is on —
        # the production posture. With SB7 OFF the system is in its documented
        # deterministic baseline mode (source + CTA), where only the banned-word bar
        # applies, so a thin source is not dropped and the baseline stays usable.
        if config.sb7_enabled():
            return post_quality.is_a_plus(d, banned_words)
        return not _has_banned_word(d.caption, banned_words)

    # Primary attempt on the real day. NO template_fn: the day uses the gym's real photo.
    draft = client_content.build_client_draft(account, day_key, voice, library_path,
                                              exclude_keys=exclude_keys,
                                              avoid_openings=avoid_openings)
    if draft is None:
        return None, None
    if _accept(draft):
        return draft, None
    first_issues = post_quality.post_issues(draft, banned_words)

    # The day's draft is not A+ (banned word OR a thin/low-quality caption). Try
    # alternative approved sources by walking neighbouring day keys so a DIFFERENT real
    # approved source fills the day before we drop it. Bounded; never fabricated.
    from datetime import date, timedelta
    base = date.fromisoformat(str(day_key)[:10])
    for step in range(1, 8):
        alt_key = (base + timedelta(days=step)).isoformat()
        alt = client_content.build_client_draft(account, alt_key, voice, library_path,
                                                exclude_keys=exclude_keys,
                                                avoid_openings=avoid_openings)
        if _accept(alt):
            # Re-home the alternative draft onto the real day so the calendar row sits
            # on day_key (only the day is re-pointed; the caption/source/photo are the
            # real approved ones the builder produced).
            alt.day_key = day_key
            alt.scheduled_for = draft.scheduled_for
            return alt, None
    return None, f"not A+: {'; '.join(first_issues)}"


def _row_from_draft(base_key, draft):
    """One draft folded into a content_calendar row using the SAME mapping the real
    month/mirror use (gym_id=base_key). PAUSED status by construction (the draft is
    PENDING; _real_row maps that to 'pending')."""
    return _mirror._real_row(base_key, draft)


def build_client_month(account, base_key, start_date, days=30, *, voice,
                       library_path=None, store, banned_words=(), logger=None):
    """Assemble a month of PAUSED client calendar rows FROM THE GYM'S OWN UPLOADED
    MEDIA and apply them via `store`.

    account       the GENERATION Account (e.g. gritx_ig) build_client_draft is keyed by.
    base_key      the TENANT base (e.g. gritx): content_calendar.gym_id AND the delete/
                  insert scope.
    start_date    'YYYY-MM-DD' (or date). days consecutive days from it.
    voice         a loaded VoiceDoc for the account.
    library_path  the gym's uploaded media folder. NO media -> Echo WAITS (see below).
    store         an injectable SupabaseCalendarStore (delete_month + insert_rows).
    banned_words  the gym's never-use words; a caption carrying one is DROPPED.

    MEDIA-REQUIRED: a client with no uploaded photos/videos gets NO calendar. The
    builder returns {ok:False, awaiting_media:True, ...} and writes NOTHING (no render,
    no host, no delete, no insert). A client NEVER gets an infographic-only calendar.

    MEDIA-CAPPED (Blake, 2026-08): the calendar is at most as long as the gym's media
    supports. ONE PHOTO PER FEED, NO REUSE: a gym with N usable media items gets AT
    MOST N feed posts (each a DISTINCT photo) plus their paired stories. `days` is an
    UPPER bound, not a target: the real number of feed-days = min(days, unique media
    count). We never pad to `days` and never reuse a photo across feeds; once the
    library is exhausted the calendar simply ends.

    Returns {ok, upserted, days, skipped_banned, media_count, months}: or {ok:False,
    reason} when a flag is off, an input is missing, or the gym is awaiting media
    (nothing touched)."""
    log = logger or (lambda m: print(f"[client-month] {m}"))
    if not config.client_month_enabled():
        return {"ok": False, "reason": "AGENT_CLIENT_MONTH off", "upserted": 0,
                "days": 0, "skipped_banned": 0}
    if not config.client_sources_enabled():
        return {"ok": False, "reason": "AGENT_CLIENT_SOURCES off", "upserted": 0,
                "days": 0, "skipped_banned": 0}
    if account is None or not base_key or store is None or voice is None:
        return {"ok": False, "reason": "missing account, base_key, store, or voice",
                "upserted": 0, "days": 0, "skipped_banned": 0}
    if days is None or days <= 0:
        return {"ok": False, "reason": "days must be > 0", "upserted": 0, "days": 0,
                "skipped_banned": 0}

    # MEDIA-REQUIRED GUARD: a client with no uploaded media gets no calendar. WAIT and
    # write nothing (no render, no host, no delete, no insert). Never an infographic.
    media_count = _client_media_count(library_path)
    if media_count <= 0:
        log(f"{base_key}: waiting for client media (no photos/videos uploaded yet); "
            "nothing rendered, nothing written")
        return {"ok": False,
                "reason": "waiting for client media (no photos/videos uploaded yet)",
                "awaiting_media": True, "upserted": 0, "days": 0, "skipped_banned": 0,
                "media_count": 0}

    from datetime import date, timedelta
    start = start_date if isinstance(start_date, date) \
        else date.fromisoformat(str(start_date)[:10])

    # LOCKED-CALENDAR AWARENESS: read the gym's EXISTING human-owned rows (approved /
    # published / denied / killed — anything a rebuild must preserve) across the span
    # BEFORE planning, so the rebuild composes with them instead of fighting them:
    #   * locked_feed_days: a day whose feed a human already owns is SKIPPED outright
    #     (no replacement feed, no orphan story/FB-mirror alongside the approved post);
    #   * used_keys: the photos those rows carry are EXCLUDED from every pick, so an
    #     already-approved photo is never re-placed on another day (no double-post).
    # Without this the builder re-picked approved photos (double-place) and photos
    # consumed by pruned colliding rows were lost forever (under-build).
    locked_feed_days, used_keys = _locked_calendar_state(
        base_key, start, days, store, log)
    # Client-EDITED story captions per day: honor them on re-render so a saved story
    # caption is not discarded by the rebuild (Dale, 2026-08-17).
    edited_story_caps = _edited_story_captions(base_key, start, days, store, log)

    # MEDIA-CAPPED: never build past the media the gym has. `days` is only an UPPER
    # bound; the real number of NEW feed-days is at most the media not already locked
    # to an approved row (one distinct creative per feed, no reuse). A 2-photo gym
    # gets 2 feeds, never 30.
    max_feed_days = min(days, max(0, media_count - len(used_keys)))

    drafts = []
    skipped_banned = 0
    built_days = 0
    banned_words = tuple(banned_words or ())
    # ONE PHOTO PER FEED, NO REUSE: track the creative each feed consumed so no photo
    # is used by two feeds. used_keys (locked photos + this build's picks) is passed
    # INTO the pick so every day draws a genuinely fresh creative; used_paths stays as
    # the local-path backstop.
    used_paths = set()
    # OPENING-VARIETY (Ryan Parr, 2026-08-17): accumulate the OPENING of each accepted
    # feed caption and feed the recent window into the NEXT day's generation so several
    # days in a row do not lead with the same hook. STYLE-only, bounded, never a block;
    # with SB7 off (deterministic baseline) the generator ignores it, so nothing changes.
    from .drafter import opening_signature
    recent_openings = []            # accepted opening signatures, oldest..newest
    _OPENING_WINDOW = 6             # how many recent openings each new day must avoid
    # Walk day keys as an UPPER bound (days), but STOP emitting feeds once we have
    # placed one per unique photo (max_feed_days). Stories reuse the feed's photo (a
    # feed + its paired story are the same asset), so stories do not consume the cap.
    i = 0
    while i < days and built_days < max_feed_days:
        day_key = (start + timedelta(days=i)).isoformat()
        i += 1

        # A day whose feed a human already owns keeps its approved content; the
        # rebuild never plans a competing feed/story for it.
        if day_key in locked_feed_days:
            log(f"locked {day_key}: day already has approved/published content")
            continue

        feed, feed_drop = _clean_draft_for_day(
            account, day_key, voice, library_path, banned_words, log,
            exclude_keys=used_keys, avoid_openings=recent_openings[-_OPENING_WINDOW:])
        if feed is None:
            if feed_drop:
                skipped_banned += 1
                log(f"drop {day_key} feed: {feed_drop}")
            else:
                log(f"skip {day_key} feed: no approved source could build the day")
            continue
        # MEDIA-ONLY: emit a day only when it carries a REAL uploaded creative. A day
        # with no client photo is SKIPPED (held), NEVER infographic-filled.
        if not _has_real_creative(feed):
            log(f"held: no client photo for the day {day_key} feed")
            continue
        # NO REUSE: a photo already placed on an earlier feed is never reused. Skip
        # the day; a later day's rotated pick fills a still-unused photo.
        feed_path = (getattr(feed, "creative_path", "") or "").strip()
        if feed_path and feed_path in used_paths:
            log(f"skip {day_key} feed: photo already used by an earlier feed (no reuse)")
            continue
        if feed_path:
            used_paths.add(feed_path)
            used_keys.add(os.path.basename(feed_path))
        pub = (getattr(feed, "creative_public_url", "") or "").strip()
        if pub:
            used_keys.add(_url_basename(pub))
        # ACTION-CUT REEL (AGENT_CLIENT_VIDEO_EDIT, OFF by default): a VIDEO draft is
        # edited into an engaging fast-cut 9:16 reel (hook text from the day's own
        # approved caption) and the draft's creative swaps to the hosted edit. Editing
        # only ENHANCES: any failure keeps the raw video; approval gate unchanged.
        _maybe_edit_video(account, feed, library_path, log)
        # VIDEO PREVIEW: a video shows BLANK in the calendar's image slot. Generate +
        # host a poster frame so the client SEES the video (a real frame) instead of a
        # blank card. Display-only (the video still publishes); best effort.
        _attach_video_poster(account, feed, library_path, log)
        _mark_feed(feed)
        drafts.append(feed)
        built_days += 1
        # Record this accepted feed's opening so the NEXT day avoids leading the same
        # way (the cross-day repetition Ryan flagged). Blank signatures are skipped.
        sig = opening_signature(getattr(feed, "caption", "") or "")
        if sig:
            recent_openings.append(sig)

        # PAIRED STORY on the SAME photo: the story mirrors the feed's real creative
        # rather than re-picking (which would consume a SECOND photo and break the
        # one-photo-per-feed cap). It carries the feed's caption + creative, marked as
        # a story. No extra media is consumed, so N photos -> N feeds + N stories.
        story = _story_from_feed(feed)
        _mark_story(story)
        # HONOR A CLIENT-EDITED STORY CAPTION: if the client edited this day's story
        # caption in the portal, re-render the story with THEIR text (not the freshly
        # generated feed caption), so a saved story caption is never discarded by the
        # rebuild. Fabrication-safe: an edited caption already cleared the edit route's
        # fabrication gate before it was stored. The FEED keeps its own caption.
        story_caption_override = edited_story_caps.get(str(day_key)[:10])
        if story_caption_override:
            story.caption = story_caption_override
        # STORY FORMATTING (AGENT_STORY_FORMAT): a PHOTO story's creative becomes a
        # filled 1080x1920 card with the caption burned in; a VIDEO story's creative
        # becomes a 9:16 story video with the caption burned in. A story publishes with
        # an EMPTY body, so the caption MUST be on the media. If neither can be produced
        # (flag off, or the render fails) the story would go out CAPTIONLESS (Dale, 2026
        # -08-15) -> we DROP it instead of shipping a story with no caption. The feed
        # still posts; only the un-captionable story is held.
        if _maybe_format_story(account, story, feed, library_path, log):
            drafts.append(story)
        else:
            log(f"drop {day_key} story: cannot carry its caption (a story publishes "
                "empty-body; refusing to ship a captionless story)")

    rows = _to_rows(base_key, drafts)
    result = _apply(base_key, rows, start, days, store, log,
                    locked_days=locked_feed_days)
    result["days"] = built_days
    result["skipped_banned"] = skipped_banned
    result["media_count"] = media_count
    return result


_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def _maybe_edit_video(account, feed, library_path, log):
    """Swap a VIDEO feed draft's creative for its action-cut reel (edited + HOSTED).
    No-op unless AGENT_CLIENT_VIDEO_EDIT is armed, the creative is a video, and both
    the edit and the hosting succeed — otherwise the raw video posts as-is (editing
    may never block a post). Mutates feed.creative_public_url in place; the paired
    story is cloned FROM the feed afterwards, so it inherits the same reel."""
    if not config.client_video_edit_enabled():
        return
    path = (getattr(feed, "creative_path", "") or "").strip()
    if not path or not path.lower().endswith(_VIDEO_EXTS):
        return
    try:
        from . import action_reel, media_host
        reel = action_reel.get_or_make_reel(
            path, getattr(feed, "caption", "") or "", library_path, logger=log)
        if not reel:
            return
        hosted = None
        if config.hosting_enabled():
            hosted = media_host.host_media(reel, account.key)
        if hosted:
            feed.creative_public_url = hosted
            log(f"reel swapped in for {os.path.basename(path)}")
        else:
            log(f"reel edited but not hosted for {os.path.basename(path)}; "
                "keeping the raw video url")
    except Exception as exc:  # noqa: BLE001 - never block the day
        log(f"reel edit lane failed for {os.path.basename(path)}: "
            f"{type(exc).__name__}; posting the raw video")


def _attach_video_poster(account, draft, library_path, log):
    """For a VIDEO draft, generate + host a poster frame and stash its url on the draft
    (-> content_calendar.thumbnail_url) so the portal shows a real frame, not a blank
    card. Best effort: no poster just means the existing blank, never a blocked post."""
    path = (getattr(draft, "creative_path", "") or "").strip()
    if not path or not path.lower().endswith(_VIDEO_EXTS):
        return
    try:
        from . import action_reel, media_host
        poster = action_reel.get_or_make_poster(path, library_path, logger=log)
        if poster and config.hosting_enabled():
            hosted = media_host.host_media(poster, account.key)
            if hosted:
                draft.thumbnail_url = hosted
    except Exception as exc:  # noqa: BLE001 - a preview must never block a post
        log(f"poster lane failed for {os.path.basename(path)}: {type(exc).__name__}")


def _maybe_format_story(account, story, feed, library_path, log):
    """Give a story its CAPTION on the media (a story publishes empty-body, so the words
    must be burned in), and report whether the story may ship:

      * PHOTO story  -> a filled 1080x1920 card (photo on a blurred cover fill + caption).
      * VIDEO story  -> a 9:16 story video with the caption burned in.

    Returns True when the story carries its caption (keep it) and False when it CANNOT
    (drop it, never ship a captionless story). Mutates story.creative_public_url in place
    on success.

    CAPTIONLESS GUARD (Dale, 2026-08-15): when AGENT_STORY_FORMAT is ON (the production
    posture), a story that cannot get its caption onto the media is DROPPED (return False)
    rather than published captionless. A raw video story was the exact bug: video stories
    were 'left alone', so they went out with no caption at all.

    BASELINE UNCHANGED: with AGENT_STORY_FORMAT OFF (the documented deterministic
    baseline / tests), stories keep their raw media exactly as before and are always kept
    (return True) — this guard does not change flag-off behavior."""
    if not config.story_format_enabled():
        return True                              # baseline: unchanged, always keep
    path = (getattr(feed, "creative_path", "") or "").strip()
    is_video = bool(path) and path.lower().endswith(_VIDEO_EXTS)
    # The STORY's own caption wins when it was overridden by a client edit; otherwise it
    # equals the feed caption (the paired story is cloned from the feed). This is what
    # lets a saved story caption actually get BURNED onto the media on re-render.
    caption = (getattr(story, "caption", "") or getattr(feed, "caption", "") or "")
    try:
        from . import story_image, media_host
        gym_name = _display_name_for(account)
        if is_video:
            asset = story_image.get_or_make_story_video(
                path, caption, gym_name, library_path, logger=log)
        else:
            asset = story_image.get_or_make_story_image(
                path, caption, gym_name, library_path, logger=log)
        if not asset:
            # Could not put the caption on the media -> the story would be captionless.
            return False
        # The captioned asset must be HOSTED to publish. If hosting is off/failed we
        # cannot ship the captioned story, so we drop it rather than fall back to the
        # raw (captionless) media.
        if not config.hosting_enabled():
            log(f"story caption built but hosting is off for {os.path.basename(path)}; "
                "dropping the story (a story must not go out captionless)")
            return False
        hosted = media_host.host_media(asset, account.key)
        if not hosted:
            log(f"story caption built but hosting failed for {os.path.basename(path)}; "
                "dropping the story (a story must not go out captionless)")
            return False
        story.creative_public_url = hosted
        # the captioned asset IS the story's media; it needs no separate poster
        if getattr(story, "thumbnail_url", ""):
            story.thumbnail_url = ""
        log(f"story {'video ' if is_video else ''}captioned for "
            f"{os.path.basename(path)}")
        return True
    except Exception as exc:  # noqa: BLE001 - never crash the build
        log(f"story format lane failed for {os.path.basename(path)}: "
            f"{type(exc).__name__}; dropping the story to avoid a captionless post")
        return False


def _display_name_for(account):
    """A clean gym name for on-image branding: the account's display name minus a
    trailing IG/FB tag, or '' when it would be noise."""
    name = (getattr(account, "display_name", "") or "").strip()
    for suf in (" IG", " FB", " Instagram", " Facebook"):
        if name.endswith(suf):
            name = name[: -len(suf)].strip()
    return name


def _mark_feed(draft):
    draft.is_story = False
    if not (getattr(draft, "draft_type", "") or "").strip():
        draft.draft_type = "feed"


def _mark_story(draft):
    draft.is_story = True
    draft.draft_type = "story"


def _story_from_feed(feed):
    """A paired STORY draft on the SAME real photo as the feed. Cloned from the feed
    (same caption, creative, day, source), NOT re-picked, so a feed and its story share
    one photo and no second media item is consumed. A distinct draft_id keeps the two
    from colliding in the DB. Reuses the feed's own creative on purpose (a feed + its
    story are one asset), which is NOT the cross-feed reuse the cap forbids."""
    import dataclasses
    story = dataclasses.replace(feed)
    story.draft_id = f"{feed.draft_id}_story"
    # carry the dynamic poster attr (not a dataclass field, so replace() drops it) so
    # a video story card shows the same frame preview as its feed.
    thumb = getattr(feed, "thumbnail_url", "") or ""
    if thumb:
        story.thumbnail_url = thumb
    return story


def _to_rows(base_key, drafts):
    """Map drafts -> content_calendar rows, mirroring real_month_planner.to_calendar_rows:
    a FEED row is cross-posted to instagram AND facebook; a STORY row is instagram-only.
    Rows carry NO id. gym_id is forced to base_key. A draft with no post_date is dropped."""
    rows = []
    for draft in drafts or []:
        row = _row_from_draft(base_key, draft)
        if not row.get("post_date"):
            continue
        rows.append(row)
        # FB mirror: a feed also lands on Facebook (same cross-post the real month does).
        if row.get("format") == "feed" and (row.get("account") or "").lower() in (
                "instagram", "ig", ""):
            fb = dict(row)
            fb["account"] = "facebook"
            rows.append(fb)
    return rows


def _apply(base_key, rows, start, days, store, log, locked_days=()):
    """Delete-then-insert, gym-scoped, across every month the rows land in PLUS the full
    planned span. Rows are inserted WITHOUT an id (DB mints the uuid). Mirrors
    apply_month_plan. Refuses the demo gym id. Never raises out.

    locked_days: post_dates the builder SKIPPED because a human owns their feed. Those
    days' still-pending sibling rows (FB mirror + story on the approved feed's photo)
    are preserved from the delete — the builder emits no replacement for them, so
    wiping them would orphan the approved post's cross-post and story forever."""
    if base_key == config.demo_calendar_gym_id():
        return {"ok": False, "reason": "refusing to plan over the demo gym id",
                "upserted": 0, "deleted": 0}
    from datetime import timedelta
    span = {(start + timedelta(days=i)).isoformat()[:7] for i in range(days)}
    for r in rows:
        pd = r.get("post_date")
        if pd:
            span.add(pd[:7])
    months = sorted(span)
    clean_rows = [{k: v for k, v in r.items() if k != "id"}
                  for r in rows if str(r.get("gym_id")) == str(base_key)]
    deleted = inserted = 0
    try:
        # PRESERVE APPROVALS: drop any incoming row that would collide with a slot the
        # gym has already approved/published, and let delete_month keep those rows in
        # place (it only wipes fresh drafts). A rebuild can no longer revert an approval.
        from .portal_calendar_store import preserve_and_prune
        clean_rows, _locked = preserve_and_prune(store, base_key, months, clean_rows)
        delete_month = getattr(store, "delete_month", None)
        for month in months:
            if delete_month is not None:
                try:
                    deleted += delete_month(base_key, month,
                                            preserve_dates=locked_days) or 0
                except TypeError:      # older store/test fakes without the kwarg
                    deleted += delete_month(base_key, month) or 0
        insert_rows = getattr(store, "insert_rows", None)
        if insert_rows is not None and clean_rows:
            inserted += len(insert_rows(base_key, clean_rows) or [])
    except Exception as exc:  # noqa: BLE001
        log(f"store write failed: {type(exc).__name__}")
        return {"ok": False, "reason": f"store write failed: {type(exc).__name__}",
                "upserted": inserted, "deleted": deleted, "months": months}
    return {"ok": True, "upserted": inserted, "inserted": inserted,
            "deleted": deleted, "months": months}
