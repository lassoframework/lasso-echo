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


def _has_real_creative(draft):
    """True when the draft carries a REAL uploaded creative: a hosted/public url AND it
    is NOT a needs-media (no-image) draft. This is what makes a day emit a row; a day
    with no client photo has no real creative and is skipped, never infographic-filled."""
    if draft is None:
        return False
    if getattr(draft, "needs_media", False):
        return False
    return bool((getattr(draft, "creative_public_url", "") or "").strip())


def _clean_draft_for_day(account, day_key, voice, library_path, banned_words, log):
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

    Returns (draft, dropped_reason). draft is None when no clean draft could be built."""
    # Primary attempt on the real day. NO template_fn: the day uses the gym's real photo.
    draft = client_content.build_client_draft(account, day_key, voice, library_path)
    if draft is None:
        return None, None
    if not _has_banned_word(draft.caption, banned_words):
        return draft, None

    # The day's rotated source hit a banned word. Try alternative approved sources by
    # walking neighbouring day keys so a DIFFERENT real approved source fills the day
    # before we drop it. Bounded, no I/O beyond the same builder call, never fabricated.
    from datetime import date, timedelta
    base = date.fromisoformat(str(day_key)[:10])
    for step in range(1, 8):
        alt_key = (base + timedelta(days=step)).isoformat()
        alt = client_content.build_client_draft(account, alt_key, voice, library_path)
        if alt is None:
            continue
        if not _has_banned_word(alt.caption, banned_words):
            # Re-home the alternative draft onto the real day so the calendar row sits
            # on day_key (only the day is re-pointed; the caption/source/photo are the
            # real approved ones the builder produced).
            alt.day_key = day_key
            alt.scheduled_for = draft.scheduled_for
            return alt, None
    return None, "banned-word: every candidate source for the day carried a banned word"


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

    Returns {ok, upserted, days, skipped_banned, months}: or {ok:False, reason} when a
    flag is off, an input is missing, or the gym is awaiting media (nothing touched)."""
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
    if _client_media_count(library_path) <= 0:
        log(f"{base_key}: waiting for client media (no photos/videos uploaded yet); "
            "nothing rendered, nothing written")
        return {"ok": False,
                "reason": "waiting for client media (no photos/videos uploaded yet)",
                "awaiting_media": True, "upserted": 0, "days": 0, "skipped_banned": 0}

    from datetime import date, timedelta
    start = start_date if isinstance(start_date, date) \
        else date.fromisoformat(str(start_date)[:10])

    drafts = []
    skipped_banned = 0
    built_days = 0
    banned_words = tuple(banned_words or ())
    for i in range(days):
        day_key = (start + timedelta(days=i)).isoformat()

        feed, feed_drop = _clean_draft_for_day(
            account, day_key, voice, library_path, banned_words, log)
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
        _mark_feed(feed)
        drafts.append(feed)
        built_days += 1

        story, story_drop = _clean_draft_for_day(
            account, day_key, voice, library_path, banned_words, log)
        if story is None:
            if story_drop:
                skipped_banned += 1
                log(f"drop {day_key} story: {story_drop}")
            else:
                log(f"skip {day_key} story: no approved source could build the day")
            continue
        if not _has_real_creative(story):
            log(f"held: no client photo for the day {day_key} story")
            continue
        _mark_story(story)
        drafts.append(story)

    rows = _to_rows(base_key, drafts)
    result = _apply(base_key, rows, start, days, store, log)
    result["days"] = built_days
    result["skipped_banned"] = skipped_banned
    return result


def _mark_feed(draft):
    draft.is_story = False
    if not (getattr(draft, "draft_type", "") or "").strip():
        draft.draft_type = "feed"


def _mark_story(draft):
    draft.is_story = True
    draft.draft_type = "story"


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


def _apply(base_key, rows, start, days, store, log):
    """Delete-then-insert, gym-scoped, across every month the rows land in PLUS the full
    planned span. Rows are inserted WITHOUT an id (DB mints the uuid). Mirrors
    apply_month_plan. Refuses the demo gym id. Never raises out."""
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
        delete_month = getattr(store, "delete_month", None)
        for month in months:
            if delete_month is not None:
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
