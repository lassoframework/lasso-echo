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
                         exclude_keys=(), avoid_openings=(), allow_reuse=False,
                         angle="", avoid_angles=(), avoid_captions=()):
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
    with the same hook as its neighbours. STYLE-only guidance; never blocks a day.

    angle / avoid_angles (Bryan/Pierce, 2026-08, AGENT_CAPTION_ANGLE_ROTATION): the SB7
    problem/entry angle this day should LEAD from and the recent angles to avoid, threaded
    to the caption generator so the underlying angle varies across the month (not just the
    opening). STYLE-only; never a fact, never blocks a day. Empty (flag OFF) => unchanged.

    avoid_captions (2x cadence, CADENCE_SPEC.md D5): captions already placed on THIS
    day (the first slot's caption on a 2x day). A draft whose caption matches one is
    treated like a banned draft — the neighbour-day walk finds a DIFFERENT approved
    source — so the two slots of one day are never the same concept. HARD check
    (a dup is rejected), unlike the STYLE-only opening guidance. Empty => unchanged."""
    from . import post_quality

    def _norm_caption(text):
        return " ".join((text or "").split()).strip().lower()

    _avoid = {_norm_caption(c) for c in (avoid_captions or ()) if (c or "").strip()}

    def _accept(d):
        if d is None:
            return False
        # 2x uniqueness: never the same concept twice in one day (CADENCE_SPEC D5).
        if _avoid and _norm_caption(getattr(d, "caption", "")) in _avoid:
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
                                              avoid_openings=avoid_openings,
                                              allow_reuse=allow_reuse,
                                              angle=angle, avoid_angles=avoid_angles,
                                              record_serve=False)
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
                                                avoid_openings=avoid_openings,
                                                allow_reuse=allow_reuse,
                                                angle=angle, avoid_angles=avoid_angles,
                                                record_serve=False)
        if _accept(alt):
            # Re-home the alternative draft onto the real day so the calendar row sits
            # on day_key (only the day is re-pointed; the caption/source/photo are the
            # real approved ones the builder produced).
            alt.day_key = day_key
            alt.scheduled_for = draft.scheduled_for
            return alt, None
    return None, f"not A+: {'; '.join(first_issues)}"


def _record_feed_served(account, feed, day_key):
    """Record an ACCEPTED feed's photo as served for rotation — only once the day has cleared
    the A+ gate, the real-creative check, and the no-reuse check, i.e. it is actually KEPT.
    This replaces the old pick-time record inside build_client_draft that poisoned the ledger
    (see build_client_draft record_serve). Best effort; never raises, never blocks a build."""
    try:
        from . import rotation, dam
        path = (getattr(feed, "creative_path", "") or "").strip()
        if not path:
            return
        rotation.record_served(account.key, dam.rotation_key(path),
                               getattr(feed, "category", "") or "", day_key)
    except Exception as e:  # noqa: BLE001
        print(f"[client-month] served-record skipped for {day_key}: {type(e).__name__}: {e}")


def _row_from_draft(base_key, draft):
    """One draft folded into a content_calendar row using the SAME mapping the real
    month/mirror use (gym_id=base_key). PAUSED status by construction (the draft is
    PENDING; _real_row maps that to 'pending')."""
    return _mirror._real_row(base_key, draft)


def _finish_feed_with_story(account, feed, library_path, log, *, day_key="",
                            story_caption_override=None):
    """Run an accepted FEED draft through its media-processing lanes (action-cut reel,
    video poster, feed autofit) and build its PAIRED STORY on the same photo, returning
    the drafts to emit for the day: [feed] or [feed, story].

    Extracted VERBATIM from build_client_month's per-day tail so the denied-slot backfill
    produces IDENTICAL feed+story cards (same lanes, same captionless-story guard). Mutates
    feed.creative_public_url in place via the lanes. The caller owns loop state (built_days,
    opening variety); this helper is stateless beyond the drafts it returns."""
    # ACTION-CUT REEL (AGENT_CLIENT_VIDEO_EDIT, OFF by default): a VIDEO draft is edited
    # into a fast-cut 9:16 reel and the draft's creative swaps to the hosted edit. Any
    # failure keeps the raw video; approval gate unchanged.
    _maybe_edit_video(account, feed, library_path, log)
    # VIDEO PREVIEW: a video shows BLANK in the calendar slot; host a poster frame so the
    # client sees a real frame. Display-only; best effort.
    _attach_video_poster(account, feed, library_path, log)
    # FEED AUTOFIT (AGENT_FEED_AUTOFIT, OFF by default): an out-of-spec feed PHOTO is
    # re-framed to 1080x1080. Snapshot the pre-autofit media FIRST so the paired story
    # never inherits the square feed card.
    _pre_autofit_url = getattr(feed, "creative_public_url", "")
    _maybe_format_feed(account, feed, library_path, log)
    _mark_feed(feed)
    out = [feed]

    # PAIRED STORY on the SAME photo (cloned from the feed; no second media consumed).
    story = _story_from_feed(feed)
    # The story must NOT carry the feed's SQUARE autofit reframe: restore the pre-autofit
    # media (story-format ON rebuilds a fresh 1080x1920; this keeps it correct when OFF).
    if getattr(story, "creative_public_url", "") != _pre_autofit_url:
        try:
            story.creative_public_url = _pre_autofit_url
        except Exception:  # noqa: BLE001 - a frozen/edge draft never blocks the build
            pass
    _mark_story(story)
    # Honor a client-edited story caption when one was passed in.
    if story_caption_override:
        story.caption = story_caption_override
    # STORY FORMATTING (AGENT_STORY_FORMAT): a story publishes empty-body, so the caption
    # must be burned onto the media; if it cannot be, DROP the story (never a captionless
    # post). Flag OFF (baseline) keeps the raw media and always keeps the story.
    if _maybe_format_story(account, story, feed, library_path, log):
        out.append(story)
    else:
        log(f"drop {day_key} story: cannot carry its caption (a story publishes "
            "empty-body; refusing to ship a captionless story)")
    return out


def _is_first_month(base_key, store, log):
    """GATE 2: True when this gym has NO owner-visible content_calendar row yet (its first,
    not-yet-released month). A store without has_owner_visible_rows (test fakes, legacy) is
    treated as ESTABLISHED (returns False) so the gate only ever engages against the real
    Supabase store — nothing withheld by accident."""
    checker = getattr(store, "has_owner_visible_rows", None)
    if not callable(checker):
        return False
    try:
        return not checker(base_key)
    except Exception as exc:  # noqa: BLE001 - a check failure must never withhold blindly
        log(f"{base_key}: first-month check failed ({type(exc).__name__}); treating as "
            "established (not withheld)")
        return False


# The gym-drive lane fills these people-forward slots (spec §7). Kept in the order
# a month rotates through them so consecutive Drive days do not repeat one pillar.
_GYM_DRIVE_PILLARS = ("faces", "community", "results")


def _gym_drive_source_for(account_key, day_key):
    """One APPROVED source (the day's verbatim fact) to hand the gym-media builder so
    the Drive caption's CLAIMS still come only from approved material — the frame only
    shapes the SCENE. Resolved against the GENERATION account key (client_sources is
    keyed by account, exactly like build_client_draft), NOT the tenant base. Returns
    None when the gym has no approved source at all (the builder is then skipped: a
    Drive photo never posts without an approved fact behind the copy). Best effort; any
    resolution error yields None (lane skipped, never a fabricated post)."""
    try:
        from . import client_sources
        present = client_content._pillars_for(account_key)  # noqa: SLF001
        if not present:
            return None
        # Prefer a source in the day's rotated client category; fall back to any one
        # approved source so a Drive photo day is never starved when the pillar has
        # none. The fact is always an approved source — never invented.
        category = client_content.category_for_day(account_key, day_key, present)
        src = None
        if category:
            src = client_content._source_for_day(  # noqa: SLF001
                account_key, day_key, category, present)
        if src is None:
            for cat in present:
                items = client_sources.approved_sources(account_key, category=cat)
                if items:
                    src = items[0]
                    break
        return src
    except Exception:  # noqa: BLE001 - no approved source resolvable -> skip the lane
        return None


def append_gym_drive_drafts(account, base_key, start, days, voice, *, log,
                            covered_days, drive=None, store=None):
    """Widen the month with PENDING posts built FROM THE GYM'S CONNECTED DRIVE POOL
    (gym_media_drive spec §7). This is the production caller of
    gym_media_builder.build_gym_media_draft: for each day in the span that the
    uploaded-media path did NOT already fill, try to stage ONE Drive-sourced
    faces/community/results post (grounded caption, A+ gates, cooldowns, tenant
    isolation all enforced inside the builder). Every staged draft lands PENDING and
    carries source_media_asset_id so hide / removed-from-Drive flips it back.

    Layered under two flags (both default OFF, both checked by the CALLER before this
    runs): GYM_DRIVE_STAGE (the lane exists) AND the per-gym GYM_DRIVE_CONNECT arming.
    Returns the list of extra drafts to append (possibly empty). Never raises: the
    Drive lane must never sink a client's uploaded-media month.

    covered_days: the day_keys the uploaded-media loop already placed a feed on, so the
    Drive lane FILLS THE GAPS instead of doubling up a day."""
    from datetime import timedelta
    from . import gym_media_builder
    extra = []
    covered = {str(d)[:10] for d in (covered_days or ())}
    platform = getattr(account, "platform", None) or ""
    account_key = getattr(account, "key", "") or base_key
    pillar_i = 0
    for i in range(days):
        day_key = (start + timedelta(days=i)).isoformat()
        if day_key in covered:
            continue
        pillar = _GYM_DRIVE_PILLARS[pillar_i % len(_GYM_DRIVE_PILLARS)]
        source = _gym_drive_source_for(account_key, day_key)
        if source is None:
            # No approved fact for the copy: a Drive photo never posts on imagination.
            continue
        try:
            draft = gym_media_builder.build_gym_media_draft(
                account, day_key, pillar, voice, source, store=store, drive=drive)
        except Exception as e:  # noqa: BLE001 - the lane never sinks the month
            log(f"[gym-drive] builder failed for {base_key} {day_key}: "
                f"{type(e).__name__}: {e}")
            draft = None
        if draft is None:
            continue  # empty pool / gate miss: the builder already alerted if needed
        # Cross-post platform parity with the uploaded-media feed (the FB mirror in
        # _to_rows keys off an ig/empty account); leave the platform as the account's.
        if not (getattr(draft, "platform", "") or "").strip():
            try:
                draft.platform = platform
            except Exception:  # noqa: BLE001 - a frozen draft never blocks the build
                pass
        draft.day_key = day_key
        _mark_feed(draft)
        extra.append(draft)
        covered.add(day_key)
        pillar_i += 1
        log(f"[gym-drive] {base_key} {day_key}: staged a {pillar} post from the "
            f"connected Drive pool (asset {draft.source_media_asset_id}), PENDING")
    return extra


def build_client_month(account, base_key, start_date, days=30, *, voice,
                       library_path=None, store, banned_words=(), logger=None,
                       allow_reshape=False):
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

    # POSTING CADENCE (CADENCE_SPEC.md): 1 or 2 feed+story pairs per day. Resolved
    # ONCE per build; ECHO_CADENCE_2X_ENABLED off -> always 1 (byte-for-byte today).
    from .cadence import resolve_posts_per_day
    slots_per_day = resolve_posts_per_day(base_key, store)

    # MEDIA-CAPPED: never build past the media the gym has. `days` is only an UPPER
    # bound on COVERED DAYS; the feed budget is days * slots_per_day (at 1x exactly
    # the pre-cadence cap), still bounded by the media not already locked to an
    # approved row (one distinct creative per feed, no reuse). A 2-photo gym gets
    # 2 feeds, never 30. At 2x each day consumes two photos, so a thin library
    # covers half the days — never padded, never reused.
    max_feed_days = min(days * slots_per_day, max(0, media_count - len(used_keys)))

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
    from .drafter import opening_signature, angle_for_index
    recent_openings = []            # accepted opening signatures, oldest..newest
    _OPENING_WINDOW = 6             # how many recent openings each new day must avoid
    # ANGLE ROTATION (Bryan/Pierce, 2026-08, AGENT_CAPTION_ANGLE_ROTATION): when armed,
    # each accepted feed also gets a DISTINCT SB7 problem/entry angle round-robin (varied by
    # a build-local index that only advances on ACCEPTED days, so the spread is dense) plus
    # the recent angles to avoid, threaded into the caption generator. It also WIDENS the
    # opening-avoid window to ~12 so consecutive days diverge harder. STYLE-only, never a
    # fact, never a block. Flag OFF => no angle guidance and the window stays 6 (unchanged).
    _angle_rotation = config.caption_angle_rotation_enabled()
    _ANGLE_WINDOW = 3               # how many recent angles each new day must avoid
    _WIDE_OPENING_WINDOW = 12       # widened opening-avoid window when angle rotation is on
    recent_angles = []              # accepted angles, oldest..newest
    angle_idx = 0                   # advances only on an ACCEPTED feed (dense round-robin)
    built_feeds = 0
    # Days the uploaded-media path placed a feed on. The gym-drive lane (below) fills
    # only the GAPS, so a Drive post never doubles up a day that already has a photo.
    covered_days = set(locked_feed_days)
    # Walk day keys as an UPPER bound (days), but STOP emitting feeds once we have
    # placed one per unique photo (max_feed_days). Stories reuse the feed's photo (a
    # feed + its paired story are the same asset), so stories do not consume the cap.
    i = 0
    while i < days and built_feeds < max_feed_days:
        day_key = (start + timedelta(days=i)).isoformat()
        i += 1

        # A day whose feed a human already owns keeps its approved content; the
        # rebuild never plans a competing feed/story for it.
        if day_key in locked_feed_days:
            log(f"locked {day_key}: day already has approved/published content")
            continue

        day_captions = []          # captions placed on THIS day (2x uniqueness, D5)
        day_built = 0
        for slot_i in range(slots_per_day):
            if built_feeds >= max_feed_days:
                break
            # Choose this slot's angle (round-robin by the accepted-feed index) + the
            # recent angles to avoid, and widen the opening window, only when angle
            # rotation is armed.
            if _angle_rotation:
                day_angle = angle_for_index(angle_idx)
                day_avoid_angles = recent_angles[-_ANGLE_WINDOW:]
                opening_window = _WIDE_OPENING_WINDOW
            else:
                day_angle, day_avoid_angles, opening_window = "", (), _OPENING_WINDOW
            feed, feed_drop = _clean_draft_for_day(
                account, day_key, voice, library_path, banned_words, log,
                exclude_keys=used_keys,
                avoid_openings=recent_openings[-opening_window:],
                angle=day_angle, avoid_angles=day_avoid_angles,
                avoid_captions=tuple(day_captions))
            if feed is None:
                if feed_drop:
                    skipped_banned += 1
                    log(f"drop {day_key} feed slot {slot_i + 1}: {feed_drop}")
                elif slot_i == 0:
                    log(f"skip {day_key} feed: no approved source could build the day")
                else:
                    # NEVER the same concept twice in one day: a 2x day that can only
                    # produce one distinct concept emits ONE pair (honest, logged).
                    log(f"{day_key}: only one distinct concept available; "
                        "single post on a 2x day")
                continue
            # MEDIA-ONLY: emit a slot only when it carries a REAL uploaded creative.
            # A slot with no client photo is SKIPPED (held), NEVER infographic-filled.
            if not _has_real_creative(feed):
                log(f"held: no client photo for the day {day_key} feed")
                continue
            # NO REUSE: a photo already placed on an earlier feed is never reused.
            # Skip the slot; a later pick fills a still-unused photo.
            feed_path = (getattr(feed, "creative_path", "") or "").strip()
            if feed_path and feed_path in used_paths:
                log(f"skip {day_key} feed: photo already used by an earlier feed "
                    "(no reuse)")
                continue
            if feed_path:
                used_paths.add(feed_path)
                used_keys.add(os.path.basename(feed_path))
            pub = (getattr(feed, "creative_public_url", "") or "").strip()
            if pub:
                used_keys.add(_url_basename(pub))
            # PAIRED STORY on the SAME photo: N photos -> N feeds + N stories (the
            # story reuses the feed's creative, never a second photo). All media
            # lanes + the captionless-story guard live in the shared helper so the
            # denied-slot backfill emits IDENTICAL cards. A client-edited story
            # caption belongs to the day's FIRST (pre-existing) story only.
            story_caption_override = (edited_story_caps.get(str(day_key)[:10])
                                      if slot_i == 0 else None)
            # ACCEPTED: the feed survived every gate and is being placed. Record its
            # photo as served NOW (not at pick time) so rotation reflects only KEPT
            # days — never the picked-then-dropped attempts.
            _record_feed_served(account, feed, day_key)
            day_drafts = _finish_feed_with_story(
                account, feed, library_path, log, day_key=day_key,
                story_caption_override=story_caption_override)
            # 2x rows carry their slot ordinal so publish-time slot times are
            # deterministic (07:30 / 18:30, config.cadence_slot_times). 1x days carry
            # NO ordinal: the row shape (and publish hashing) stays byte-for-byte.
            if slots_per_day == 2:
                for d in day_drafts:
                    try:
                        d.cadence_slot_index = slot_i
                    except Exception:  # noqa: BLE001 - a frozen draft never blocks
                        pass
            drafts.extend(day_drafts)
            built_feeds += 1
            day_built += 1
            covered_days.add(day_key)   # the gym-drive lane skips days already filled
            day_captions.append(getattr(feed, "caption", "") or "")
            # Record this accepted feed's opening so the NEXT slot/day avoids leading
            # the same way (the cross-day repetition Ryan flagged).
            sig = opening_signature(getattr(feed, "caption", "") or "")
            if sig:
                recent_openings.append(sig)
            # Record this accepted feed's angle + advance the round-robin so the NEXT
            # accepted slot gets a DISTINCT angle (angle rotation ON only).
            if _angle_rotation:
                recent_angles.append(day_angle)
                angle_idx += 1
        if day_built:
            built_days += 1

    # §4 weak_match: no image cleared the content-score floor for these slots — the best
    # available was planned and must reach the coach (never silent). One summary staff alert
    # per build, not per day.
    weak = sum(1 for d in drafts if getattr(d, "weak_match", False))
    if weak:
        try:
            from . import ops_alerts
            ops_alerts.alert(f"{base_key}: {weak} post(s) this month are a WEAK photo match "
                             "(no strong image for the slot) — review or ask the gym for "
                             "fresher material for those pillars")
        except Exception:  # noqa: BLE001
            pass
        log(f"{base_key}: {weak} weak_match pick(s) flagged for the coach")

    # GYM-DRIVE LANE (GYM_DRIVE_STAGE, default OFF): a gym that connected Google Drive
    # gets PENDING posts built from its synced photo pool for the days the uploaded-
    # media path did not fill (spec §7). Layered UNDER the per-gym GYM_DRIVE_CONNECT
    # arming, so a gym must be connected + indexed first. Both flags OFF => this block
    # is inert and the uploaded-media month is byte-for-byte unchanged. Every Drive
    # draft lands PENDING (the human tap is untouched) and carries source_media_asset_id
    # so hide / removed-from-Drive flips it back to needs_media. Never raises out here:
    # the Drive lane must never sink the client's real uploaded-media calendar.
    if (config.gym_drive_stage_enabled()
            and config.gym_drive_connect_active_for(base_key)):
        try:
            drive_extra = append_gym_drive_drafts(
                account, base_key, start, days, voice, log=log,
                covered_days=covered_days)
            if drive_extra:
                drafts.extend(drive_extra)
                log(f"{base_key}: +{len(drive_extra)} post(s) from the connected "
                    "Drive pool (PENDING, gap-fill)")
        except Exception as e:  # noqa: BLE001 - the lane never sinks the month
            log(f"{base_key}: gym-drive lane skipped ({type(e).__name__}: {e})")

    rows = _to_rows(base_key, drafts)
    # GATE 2 (coach-screens-first-month, Blake 2026-08-17): a CLIENT gym's FIRST month on
    # every platform is WITHHELD from the owner ('coach_review') until a coach screens and
    # releases it — the coach SOP enforced in software. Established gyms (any owner-visible
    # row already) are grandfathered, never re-withheld on a rebuild. LASSO's own account is
    # exempt (not a client gym). Safe default: a store lacking the signal is treated as
    # established (no withhold), so nothing changes for it.
    if (config.coach_screen_first_month_enabled() and base_key != "lasso"
            and _is_first_month(base_key, store, log)):
        for r in rows:
            r["status"] = "coach_review"
        log(f"{base_key}: FIRST month -> written 'coach_review' (withheld from owner "
            "until a coach releases it; GATE 2)")
    result = _apply(base_key, rows, start, days, store, log,
                    locked_days=locked_feed_days, allow_reshape=allow_reshape)
    result["days"] = built_days
    result["feeds"] = built_feeds
    result["posts_per_day"] = slots_per_day
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


_INFOGRAPHIC_MARKERS = ("no_creative_",)   # house-rendered fallback card filename prefix


def _is_infographic_creative(draft):
    """True when a story's creative is a HOUSE-RENDERED INFOGRAPHIC (a finished, story-
    sized card that already carries its own text) rather than a real uploaded PHOTO/VIDEO.

    Blake, 2026-08-20: an infographic story must NEVER get a caption burned on top (it
    would overlay the card's own copy); a real photo/video story MUST (a story publishes
    empty-body, else it goes out captionless). Detection is the house-render filename
    PREFIX on the local path or the hosted url.

    PREFIX only, never a substring: a real client upload is stored timestamp-prefixed
    ('20260812T163147Z_<name>', intake_web._safe_name), so it can never START with
    'no_creative_' — but it COULD contain the word 'infographic' in its own name (e.g.
    '..._gym_infographic.jpg'). A substring match there would skip the burn and publish
    that real photo captionless (the exact bug we fix). The house renderer always emits
    the 'no_creative_' prefix (even its empty-slug fallback is 'no_creative_infographic_
    story.png'), so the prefix alone catches every infographic with zero false positives."""
    for attr in ("creative_path", "creative_public_url"):
        val = str(getattr(draft, attr, "") or "")
        base = val.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
        if any(base.startswith(m) for m in _INFOGRAPHIC_MARKERS):
            return True
    return False


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
    # INFOGRAPHIC vs PHOTO (Blake, 2026-08-20): only a real uploaded PHOTO/VIDEO gets a
    # caption burned in. A house-rendered INFOGRAPHIC is already a finished, story-sized
    # card carrying its own text, so a burned caption would sit ON TOP of it ("takes over
    # the caption") — skip the burn and keep the card as-is. Real client uploads never
    # match the house-render marker, so a genuine photo/video is never mistaken for one
    # (and today infographics never even reach this path — this is the intent made
    # explicit + future-proofed, never a regression for photo/video stories).
    if _is_infographic_creative(feed):
        log("story is a house infographic (already story-sized w/ its own text); "
            "keeping as-is, no caption burned")
        return True
    path = (getattr(feed, "creative_path", "") or "").strip()
    is_video = bool(path) and path.lower().endswith(_VIDEO_EXTS)
    # The STORY's own caption wins when it was overridden by a client edit; otherwise it
    # equals the feed caption (the paired story is cloned from the feed). This is what
    # lets a saved story caption actually get BURNED onto the media on re-render.
    caption = (getattr(story, "caption", "") or getattr(feed, "caption", "") or "")
    # Task #28 (§5c): keep the RAW (un-captioned) source url so an edited story caption can
    # re-burn IMMEDIATELY instead of only on the next monthly rebuild. Gated: written to
    # content_calendar.source_media_url only when AGENT_STORY_SOURCE_MEDIA is on (the column
    # exists). feed.creative_public_url is the raw feed media before the story burn swaps
    # story.creative_public_url to the captioned asset.
    if config.story_source_media_enabled():
        story.source_media_url = (getattr(feed, "creative_public_url", "") or "")
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


def _maybe_format_feed(account, feed, library_path, log):
    """AGENT_FEED_AUTOFIT: re-frame an OUT-OF-SPEC feed PHOTO into an in-spec 1080x1080 card
    so the platform never hard-crops the subject. ENHANCE-only: an in-spec photo, a video,
    hosting-off, or any failure keeps the raw media (this never DROPS a post, unlike the story
    caption guard). Mutates feed.creative_public_url in place on success."""
    if not config.feed_autofit_enabled():
        return
    path = (getattr(feed, "creative_path", "") or "").strip()
    if not path:
        return
    try:
        from . import feed_image, media_host
        asset = feed_image.get_or_make_feed_image(path, library_path, logger=log)
        if not asset:
            return                                    # in-spec / video / render skipped
        if not config.hosting_enabled():
            return                                    # cannot host the reframe -> keep raw
        hosted = media_host.host_media(asset, account.key)
        if hosted:
            feed.creative_public_url = hosted
            if getattr(feed, "thumbnail_url", ""):
                feed.thumbnail_url = ""               # the reframe IS the media
            log(f"feed autofit applied for {os.path.basename(path)} (odd ratio -> 1080x1080)")
    except Exception as exc:  # noqa: BLE001 - never crash the build; keep the raw photo
        log(f"feed autofit lane failed for {os.path.basename(path)}: {type(exc).__name__}")


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


def _apply(base_key, rows, start, days, store, log, locked_days=(),
           allow_reshape=False):
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
        # NEVER WIPE TO EMPTY (TopFuel, 2026-08-25): a rebuild that produced NO rows must not
        # delete an existing calendar. This happens when the grow-guard re-triggers a build
        # for a gym that is effectively built out (its build_target counts photo clusters, but
        # some are un-plannable), then every plannable photo is inside its reuse window from
        # the PRIOR build, so pick_image returns None and the build is empty. Deleting-then-
        # inserting-nothing wiped the whole calendar every listener cycle. An empty build is a
        # NO-OP on the store; the existing calendar (drafts + approvals) is preserved intact.
        if not clean_rows:
            log(f"{base_key}: rebuild produced no rows; keeping the existing calendar "
                "(no delete — never wipe to empty)")
            return {"ok": True, "upserted": 0, "inserted": 0, "deleted": 0,
                    "months": months, "noop_empty": True}
        # NEVER SHRINK (TopFuel 2026-08-25): a grow-to-cap rebuild must only GROW, never
        # replace a good calendar with a SMALLER one. The grow-guard can re-trigger a build
        # for a gym that is already built out (build_target counts photo clusters, some of
        # which are un-plannable), and the reuse window then blocks re-picking most photos, so
        # the rebuild yields only a FEW feeds. Deleting-then-inserting-fewer shrank the
        # calendar every cycle (TopFuel drifted 39 -> 21 -> 3). If this build placed fewer
        # feeds than already exist, keep the existing calendar untouched. Feeds are counted as
        # distinct instagram feed post_dates (the same unit the grow-guard uses).
        new_feeds = len({r.get("post_date") for r in clean_rows
                         if r.get("format") == "feed" and r.get("account") == "instagram"})
        # POST-MERGE comparison (audit 2026-08-25 MAJOR): a grow build EXCLUDES locked
        # (human-owned approved/published) days from its own rows — their feeds survive the
        # delete via preserve_dates. Comparing only new_feeds against existing_feeds
        # (which counts the locked ones) wrongly read every incremental grow as a shrink
        # and no-op'd it, so a built gym could never grow. Compare what the calendar will
        # hold AFTER the write: this build's feeds + the preserved locked-day feeds.
        locked_in_span = {str(d)[:10] for d in (locked_days or ())
                          if str(d)[:7] in set(months)}
        post_merge_feeds = new_feeds + len(locked_in_span)
        try:
            from .client_media_sync import _existing_feed_count
            existing_feeds, count_ok = _existing_feed_count(store, base_key, start, days)
        except Exception:  # noqa: BLE001 - a count failure must never block a legit build
            existing_feeds, count_ok = 0, False
        # CADENCE RESHAPE EXCEPTION (audit 2026-08-27 MAJOR): the guard counts
        # distinct feed DATES, so a legitimate 1x->2x flip on a gym whose media sits
        # between days and 2x days reads as a shrink (same or more feeds, fewer
        # dates) and was silently no-op'd — and the caller then stamped the cadence
        # as applied, dropping the client's toggle forever. A cadence-change rebuild
        # (allow_reshape=True, passed ONLY when the scan detected a cadence flip) is
        # a deliberate one-time reshape: the guard is skipped for it. Every other
        # rebuild keeps the guard exactly as before; the cadence_applied stamp
        # (written only on a real apply) prevents repeat reshapes.
        if (not allow_reshape and count_ok and existing_feeds > 0
                and post_merge_feeds < existing_feeds):
            log(f"{base_key}: rebuild would SHRINK feeds {existing_feeds} -> "
                f"{post_merge_feeds} ({new_feeds} new + {len(locked_in_span)} locked); "
                "keeping the existing calendar (grow-only, never shrink)")
            return {"ok": True, "upserted": 0, "inserted": 0, "deleted": 0,
                    "months": months, "noop_shrink": True,
                    "existing_feeds": existing_feeds, "new_feeds": new_feeds}
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


def backfill_denied_slots(account, base_key, start_date, days=30, *, voice,
                          library_path=None, store, banned_words=(), logger=None):
    """Give each DENIED feed day a FRESH replacement (a NEW caption on a REUSED photo) for
    a gym that is AT its creative cap — where the monthly grow-to-cap build is a no-op and
    the denied slot would otherwise stay empty forever (the portal's "recreating" state
    never resolving; Dale / ENG, 2026-08-19).

    A denied FEED day is replaced ONLY when it has no active (pending/approved/published)
    feed on that date yet, so this is IDEMPOTENT: once a pending replacement lands the next
    pass skips it. The replacement REUSES a photo (allow_reuse — the gym has no fresh
    creative left) but NEVER the denied post's own photo and NEVER a photo consumed by an
    approved/published row. Every replacement clears the same A+ / banned-word / fabrication
    gates as a normal build and is written PENDING (owner-visible, awaits approval).
    INSERT-only: the existing calendar is never deleted. Behind AGENT_DENY_BACKFILL (OFF by
    default) — flag off -> returns ok:False and touches nothing.

    Returns {ok, backfilled, days_needing, skipped[, rows]}."""
    log = logger or (lambda m: print(f"[deny-backfill] {m}"))
    if not config.deny_backfill_enabled():
        return {"ok": False, "reason": "AGENT_DENY_BACKFILL off", "backfilled": 0}
    if account is None or not base_key or store is None or voice is None:
        return {"ok": False, "reason": "missing account, base_key, store, or voice",
                "backfilled": 0}
    list_month = getattr(store, "list_month", None)
    insert_rows = getattr(store, "insert_rows", None)
    if list_month is None or insert_rows is None:
        return {"ok": False, "reason": "store cannot read/insert", "backfilled": 0}

    from datetime import date, timedelta
    start = start_date if isinstance(start_date, date) \
        else date.fromisoformat(str(start_date)[:10])
    win_start = start.isoformat()
    win_end = (start + timedelta(days=max(1, days) - 1)).isoformat()
    months = sorted({(start + timedelta(days=i)).isoformat()[:7]
                     for i in range(max(1, days))})

    denied_photo_by_day = {}    # date -> the denied feed's own image basename (never re-served)
    active_feed_days = set()    # dates already covered by a non-denied/killed feed
    live_photo_keys = set()     # photos on approved/published/publishing rows (never reused)
    for month in months:
        try:
            rows = list_month(base_key, month) or []
        except Exception as exc:  # noqa: BLE001 - a read failure must never write blindly
            log(f"{base_key}: calendar read failed ({type(exc).__name__}); no backfill")
            return {"ok": False, "reason": "calendar unreadable", "backfilled": 0}
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").lower()
            # A LIVE photo is never reused, no matter WHERE it sits in the calendar:
            # collect it regardless of the window so a replacement can never double-post
            # a photo already approved/published on any other day.
            if status in _PHOTO_CONSUMING_STATUSES:
                k = _url_basename(row.get("image_url") or "")
                if k:
                    live_photo_keys.add(k)
            pd = str(row.get("post_date") or "")[:10]
            if not pd or pd < win_start or pd > win_end:
                continue
            fmt = str(row.get("format") or "").lower()
            acct = str(row.get("account") or "").lower()
            if fmt == "feed" and acct in ("instagram", "ig", ""):
                if status == "denied":
                    denied_photo_by_day.setdefault(
                        pd, _url_basename(row.get("image_url") or ""))
                elif status not in ("killed", "deleted"):
                    active_feed_days.add(pd)

    # A denied day still needs a replacement only if nothing active already covers it.
    todo = sorted(d for d in denied_photo_by_day if d not in active_feed_days)
    if not todo:
        return {"ok": True, "backfilled": 0, "days_needing": 0, "skipped": 0}

    banned_words = tuple(banned_words or ())
    drafts = []
    skipped = 0
    for day_key in todo:
        # Exclude the denied post's OWN photo (never hand the same one back) + every photo
        # already live on the page. Everything else may be REUSED.
        exclude = set(live_photo_keys)
        own = denied_photo_by_day.get(day_key)
        if own:
            exclude.add(own)
        feed, drop = _clean_draft_for_day(
            account, day_key, voice, library_path, banned_words, log,
            exclude_keys=exclude, allow_reuse=True)
        if feed is None or not _has_real_creative(feed):
            skipped += 1
            log(f"{base_key} {day_key}: no A+ replacement could be built "
                f"({drop or 'no usable creative'})")
            continue
        _record_feed_served(account, feed, day_key)   # KEPT: record only accepted backfills
        drafts.extend(_finish_feed_with_story(account, feed, library_path, log,
                                              day_key=day_key))

    if not drafts:
        return {"ok": True, "backfilled": 0, "days_needing": len(todo),
                "skipped": skipped}

    rows = _to_rows(base_key, drafts)
    # GATE 2 safety: withhold a first-month gym's replacement exactly as its month would be.
    # (A gym with a denied post is established by construction, so this is a guard.)
    if (config.coach_screen_first_month_enabled() and base_key != "lasso"
            and _is_first_month(base_key, store, log)):
        for r in rows:
            r["status"] = "coach_review"
    clean_rows = [{k: v for k, v in r.items() if k != "id"}
                  for r in rows if str(r.get("gym_id")) == str(base_key)]
    try:
        inserted = len(insert_rows(base_key, clean_rows) or [])
    except Exception as exc:  # noqa: BLE001
        log(f"{base_key}: backfill insert failed: {type(exc).__name__}")
        return {"ok": False, "reason": f"insert failed: {type(exc).__name__}",
                "backfilled": 0, "days_needing": len(todo), "skipped": skipped}
    days_done = len({r.get("post_date") for r in clean_rows
                     if r.get("format") == "feed"})
    log(f"{base_key}: backfilled {days_done} denied slot(s) with a fresh caption on a "
        f"reused photo ({inserted} row(s), {skipped} day(s) unbuildable)")
    return {"ok": True, "backfilled": days_done, "rows": inserted,
            "days_needing": len(todo), "skipped": skipped}
