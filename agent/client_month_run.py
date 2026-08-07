"""
client_month_run.py: assemble a full month of APPROVABLE DRAFT calendar rows for a
CLIENT gym that has NO photo library, rendering each day as a house INFOGRAPHIC from
that gym's OWN approved sources, and upsert them to the shared content_calendar.

Behind AGENT_CLIENT_MONTH (config.client_month_enabled(), default OFF) AND it also
requires config.client_sources_enabled(). Flag off -> build_client_month returns
ok:False and touches nothing: no render, no host, no calendar write.

WHAT IT DOES (mirrors real_month_planner / real_calendar_mirror exactly):
  * For each of `days` days, build a FEED draft and a paired STORY draft via the
    existing client_content.build_client_draft, passing an infographic template_fn
    (these gyms have no library, so the thin-library grace path renders the house card).
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
never emitted), nothing publishes, no gate weakened, every draft PAUSED for approval.
Studio / host are injectable so the whole path is offline-testable.
"""

import re

from . import client_content, config
from . import real_calendar_mirror as _mirror


def infographic_template_fn(account, *, brand=None, studio=None, host=None,
                            is_story=False):
    """Build a `template_fn(account, source, day_key) -> url` that renders the house
    infographic from source.text and returns its hosted public url (or None).

    Rendering path (each piece injectable for tests):
      1. studio.generate(headline, facts, ...) -> {"path": ...} the Gemini Pro house
         card. aspect/pixels are the FEED size (1:1, 1080x1080) or, when is_story,
         the STORY size (9:16, 1080x1920). Default studio is creative_studio.
      2. summit_rebuild._normalize_to_canvas(path, expected) forces the exact canvas
         (Gemini returns its native ~928x1152, so normalize before use).
      3. host.host_media(path, base_tenant) -> public url. Default host is media_host.

    The `account` bound here names the tenant base for hosting (its _ig/_fb suffix is
    stripped) so a client's cards host under its own tenant prefix. No fabrication: the
    card's only text is the approved source.text passed through; an empty render or a
    failed host returns None (the draft then falls back to needs-media, never a blank
    published card)."""
    if studio is None:
        from . import creative_studio as studio  # noqa: PLW0127
    if host is None:
        from . import media_host as host  # noqa: PLW0127

    if is_story:
        aspect, pixels, expected = "9:16", "1080x1920", (1080, 1920)
        surface = "story"
    else:
        aspect, pixels, expected = "1:1", "1080x1080", (1080, 1080)
        surface = "feed"

    base_tenant = _base_of(getattr(account, "key", "") or "")

    def _template_fn(acct, source, day_key):
        text = (getattr(source, "text", "") or "").strip()
        if not text:
            return None
        result = studio.generate(
            headline=text, facts=[text], aspect=aspect, pixels=pixels,
            surface=surface, account_key=getattr(acct, "key", None))
        if not result:
            return None
        path = result.get("path") if isinstance(result, dict) else result
        if not path:
            return None
        try:
            from . import summit_rebuild
            summit_rebuild._normalize_to_canvas(path, expected)
        except Exception:
            # A normalization failure is not fabrication; the render still exists.
            # Host it as-is rather than dropping (the size guard lives on the render
            # path, not here). But a genuinely absent file cannot host, handled below.
            pass
        return host.host_media(path, base_tenant)

    return _template_fn


def _base_of(account_key):
    """The tenant base for an account key ('gritx_ig' -> 'gritx')."""
    account_key = (account_key or "").strip()
    for suffix in ("_ig", "_fb"):
        if account_key.endswith(suffix):
            return account_key[: -len(suffix)]
    return account_key


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


def _clean_draft_for_day(account, day_key, voice, library_path, template_fn,
                         banned_words, log):
    """Build a draft for the day whose caption carries NO banned word, preferring a
    different approved source/category over dropping the day.

    client_content.build_client_draft rotates category+source deterministically per
    day. To try the OTHER sources for the day without duplicating that private logic,
    we ask the builder for the day; if its caption is banned, we temporarily HIDE the
    offending source's category from the account's present-set by shifting the day key
    by whole weeks (which advances the source cycle within/around the categories) and
    re-ask, up to a bounded number of attempts. Any draft whose caption still carries a
    banned word is DROPPED (never emitted).

    Returns (draft, dropped_reason). draft is None when no clean draft could be built."""
    # Primary attempt on the real day.
    draft = build_fn = None
    build_fn = template_fn
    draft = client_content.build_client_draft(
        account, day_key, voice, library_path, template_fn=build_fn)
    if draft is None:
        return None, None
    if not _has_banned_word(draft.caption, banned_words):
        return draft, None

    # The day's rotated source hit a banned word. Try alternative approved sources by
    # walking neighbouring day keys (same weekday cadence advances source cycle) so a
    # DIFFERENT real approved source fills the day before we drop it. Bounded, no I/O
    # beyond the same builder call, never fabricated.
    from datetime import date, timedelta
    base = date.fromisoformat(str(day_key)[:10])
    for step in range(1, 8):
        alt_key = (base + timedelta(days=step)).isoformat()
        alt = client_content.build_client_draft(
            account, alt_key, voice, library_path, template_fn=build_fn)
        if alt is None:
            continue
        if not _has_banned_word(alt.caption, banned_words):
            # Re-home the alternative draft onto the real day so the calendar row sits
            # on day_key (only the day is re-pointed; the caption/source are the real
            # approved ones the builder produced).
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
                       library_path=None, feed_template_fn, story_template_fn,
                       store, banned_words=(), logger=None):
    """Assemble a month of PAUSED client calendar rows and apply them via `store`.

    account       the GENERATION Account (e.g. gritx_ig) build_client_draft is keyed by.
    base_key      the TENANT base (e.g. gritx): content_calendar.gym_id AND the delete/
                  insert scope.
    start_date    'YYYY-MM-DD' (or date). days consecutive days from it.
    voice         a loaded VoiceDoc for the account.
    library_path  None for these no-library gyms (the infographic path fills the slot).
    feed/story_template_fn  the infographic renderers (see infographic_template_fn);
                  injectable so tests pass fakes and no Gemini/host call happens.
    store         an injectable SupabaseCalendarStore (delete_month + insert_rows).
    banned_words  the gym's never-use words; a caption carrying one is DROPPED.

    Returns {ok, upserted, days, skipped_banned, months}: or {ok:False, reason} when a
    flag is off or an input is missing (and nothing is touched)."""
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
            account, day_key, voice, library_path, feed_template_fn,
            banned_words, log)
        if feed is None:
            if feed_drop:
                skipped_banned += 1
                log(f"drop {day_key} feed: {feed_drop}")
            else:
                log(f"skip {day_key} feed: no approved source could build the day")
            continue
        _mark_feed(feed)
        drafts.append(feed)
        built_days += 1

        story, story_drop = _clean_draft_for_day(
            account, day_key, voice, library_path, story_template_fn,
            banned_words, log)
        if story is None:
            if story_drop:
                skipped_banned += 1
                log(f"drop {day_key} story: {story_drop}")
            else:
                log(f"skip {day_key} story: no approved source could build the day")
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
