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
import uuid

from . import client_content, config, day_shape
from . import cta_self_question_gate
from .jobs import day_shape_block_alarm
from . import real_calendar_mirror as _mirror

# Media extensions that count as a client having uploaded usable creative.
_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}


class InvalidLogicalPostIdentity(ValueError):
    """A draft reached the month build carrying an unverifiable pre-stamped
    logical_post_id.  This is FATAL for the whole rebuild: it must propagate out
    of every lane's broad `except Exception` media-hold guard so _apply never
    runs its month-grained delete.  Callers release local reservations / roll
    back Drive assets and then re-raise; the outer build's finally releases any
    unlanded reservations and restores released Drive stamps.  Subclasses
    ValueError so the row-writer's fail-closed ValueError contract is unchanged."""


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
    from .media_bridge import enabled as bridge_enabled
    if bridge_enabled():
        from .library import list_creatives
        from .client_media_sync import usable_local_creative
        base = os.path.basename(os.path.normpath(path))
        return sum(usable_local_creative(c, base + "_ig")
                   for c in list_creatives(path))
    count = 0
    from .client_media_sync import (explicitly_refused_local,
                                    is_generated_derivative)
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            if (not name.startswith("._")
                    and os.path.splitext(name)[1].lower() in _MEDIA_EXTS
                    and not explicitly_refused_local(full)
                    and not is_generated_derivative(full)):
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


def _caption_repeats_history(base_key, caption, day_key):
    """Use the writer's 180 day rule before a photo gets paired with a Story.

    Retries choose approved source material; changing case, spacing or adding an
    arbitrary suffix never establishes a fresh caption.
    """
    if not config.caption_cooldown_enabled():
        return False
    from . import caption_ledger
    return caption_ledger.is_verbatim_blocked(base_key, caption, day_key)


def _empty_fill_has_unmet_slots(store, base_key, start, days, slots_per_day,
                                locked_days):
    """An empty rebuild is a legitimate no-op only when its span is covered."""
    from datetime import timedelta
    from .onboarding_demo import is_sample_row
    dates = {(start + timedelta(days=i)).isoformat() for i in range(days)}
    unmet = dates - set(locked_days)
    if not unmet:
        return False
    slots = {day: set() for day in unmet}
    try:
        for month in sorted({day[:7] for day in unmet}):
            for row in store.list_month(base_key, month) or []:
                day = str(row.get("post_date") or "")[:10]
                if (day not in slots or is_sample_row(row)
                        or str(row.get("status") or "").lower() in
                        ("denied", "killed", "deleted")
                        or str(row.get("account") or "").lower() not in
                        ("instagram", "ig", "")
                        or row.get("format") != "feed"
                        or not row.get("image_url") or row.get("media_not_ready_reason")):
                    continue
                slots[day].add(row.get("slot_index") or 0)
    except Exception:
        return True  # an unreadable span cannot certify a successful fill
    return any(len(placed) < slots_per_day for placed in slots.values())


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


def _feed_slot_index(row, slots_per_day=1):
    """The cadence ordinal a human-owned feed occupies.

    Legacy 1x rows have no slot_index and occupy slot 0, so a gym that later
    flips to 2x can still fill the open evening slot without replacing the
    approved morning post. An ordinal outside the current cadence (a leftover
    PM row after a 2x->1x toggle) still occupies a real slot: treat it as slot 0
    so a 1x rebuild cannot place a competing feed beside it.
    """
    capacity = 2 if int(slots_per_day or 1) >= 2 else 1
    raw = (row or {}).get("slot_index")
    try:
        slot = int(raw)
    except (TypeError, ValueError):
        return 0
    if slot in range(capacity):
        return slot
    return 0


def _locked_calendar_state(base_key, start, days, store, log, library_path=None):
    """(locked_feed_days, used_keys, locked_feed_slots) from EXISTING human-owned
    calendar rows across the planned span.

    locked_feed_days: post_dates where any feed/Story sibling is human-owned
    (approved/published/denied/killed — anything not machine-wipeable), so a
    rebuild never separates a retained Story from its feed. locked_feed_slots:
    (post_date, slot_index) pairs whose FEED a human already owns; the rebuild
    never replaces those slots, but a 2x day with only slot 0 owned can still
    receive a slot 1 feed. used_keys: media carried by rows whose photo is truly
    consumed (approved/published/publishing, any format), so a live photo is
    never re-picked; a denied/killed photo stays available.
    Read-only; a read failure returns empty state (the store-level preserve_and_prune
    backstop still guards the write)."""
    from datetime import timedelta
    from .portal_calendar_store import _WIPEABLE_STATUSES
    from .cadence import resolve_posts_per_day
    try:
        slots_per_day = resolve_posts_per_day(base_key, store)
    except Exception:  # noqa: BLE001 - lock-state cadence is best-effort
        slots_per_day = 1
    locked_days, locked_slots, used = set(), set(), set()
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return locked_days, used, locked_slots
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
            fmt = str(row.get("format") or "").lower()
            if fmt in ("feed", "story"):
                day = str(row.get("post_date") or "")[:10]
                if day:
                    locked_days.add(day)
                    if fmt == "feed":
                        locked_slots.add((day, _feed_slot_index(row, slots_per_day)))
            if status in _PHOTO_CONSUMING_STATUSES:
                key = _url_basename(row.get("image_url") or "")
                if key:
                    used.add(key)
    locked_days.discard("")
    # A human-owned Story with no owned feed still locks its WHOLE day (main #327-#330
    # semantics): a rebuild must never place a new feed beside a retained Story.
    feed_slot_days = {d for d, _s in locked_slots}
    for day in locked_days - feed_slot_days:
        locked_slots.add((day, 0))
        locked_slots.add((day, 1))
    # CROSS-DAY MEDIA GUARD (Blake, 2026-08-31: a client saw the same photo across
    # different weeks): also exclude every photo that will SURVIVE this rebuild on
    # the gym's book — published within the trailing repeat window (the span-months
    # read above missed last month's publishes), coach_review rows (NOT wipeable, so
    # they survive the delete), and wipeable rows OUTSIDE the span months. Wipeable
    # rows INSIDE the span are about to be replaced, so their photos stay free —
    # excluding them would starve the very rebuild that releases them. Read failure
    # degrades open (the rotation window stays the backstop).
    try:
        from . import media_guard
        used |= media_guard.surviving_keys(base_key, store, start, days, log=log,
                                           library_path=library_path)
    except Exception as exc:  # noqa: BLE001 - the guard must never sink a build
        log(f"cross-day media guard read skipped ({type(exc).__name__})")
    # Resolve autofit reframe basenames (sha12__feed.jpg) in `used` back to the
    # original library file basename that pick_image's os.path.basename() exclusion
    # checks against. surviving_keys does this via resolve_raw_keys, but if
    # surviving_keys partially degrades (Supabase blink, store read failure) reframe
    # names land in `used` without their raw counterparts. pick_image keys by
    # os.path.basename(creative.path) and never sees __feed.jpg names in the library,
    # so a reframe in `used` without its raw counterpart silently fails to exclude the
    # original file, and the planner re-picks a photo already on the gym's book.
    if library_path:
        try:
            from . import media_guard as _mg
            for raw in _mg.reframe_map(library_path, used).values():
                used.add(raw)
        except Exception as exc:  # noqa: BLE001 - resolution is best-effort, never a block
            log(f"reframe-to-basename resolution skipped ({type(exc).__name__})")
    return locked_days, used, locked_slots


def _surviving_pillar_counts(base_key, start, days, store, log):
    """Pillar counts for human-owned posts that survive this rebuild.

    Count POSTS in the same units as calendar_grade: same-date IG/FB/story rows with
    the same caption are one post. Pending/draft/queued rows are excluded because this
    rebuild replaces them. A read failure returns an empty count; the build-local
    balancing guard still prevents a new single-pillar batch.
    """
    from collections import Counter
    from datetime import timedelta
    from .caption_ledger import caption_hash
    from .portal_calendar_store import _WIPEABLE_STATUSES

    counts = Counter()
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return counts
    months = sorted({(start + timedelta(days=i)).isoformat()[:7]
                     for i in range(max(1, days))})
    seen = set()
    for month in months:
        try:
            rows = list_month(base_key, month) or []
        except Exception as exc:  # noqa: BLE001 - balancing degrades to build-local state
            log(f"pillar-mix read failed for {month}: {type(exc).__name__}")
            continue
        for row in rows:
            status = str((row or {}).get("status") or "").lower()
            if (not status or status in _WIPEABLE_STATUSES
                    or status not in ("approved", "publishing", "published", "coach_review")):
                continue
            category = str(row.get("pillar") or row.get("category") or "").strip().lower()
            if not category:
                continue
            key = (str(row.get("post_date") or "")[:10],
                   caption_hash(row.get("caption") or ""))
            if key in seen:
                continue
            seen.add(key)
            counts[category] += 1
    return counts


def _edited_story_captions(base_key, start, days, store, log):
    """{(post_date, slot_index) -> caption} for client-edited STORY rows which
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
        feeds_by_slot = {}
        stories_by_slot = {}
        for row in rows:
            fmt = str(row.get("format") or "").lower()
            pd = str(row.get("post_date") or "")[:10]
            account = str(row.get("account") or "").strip().lower()
            slot = row.get("slot_index")
            if not pd:
                continue
            key = (pd, slot)
            if fmt == "feed" and account in ("instagram", "ig", ""):
                feeds_by_slot.setdefault(key, row.get("caption") or "")
            elif fmt == "story" and account in ("instagram", "ig", ""):
                stories_by_slot[key] = row.get("caption") or ""
        for key, story_cap in stories_by_slot.items():
            story_cap = (story_cap or "").strip()
            feed_cap = (feeds_by_slot.get(key) or "").strip()
            # An edited story caption is one that differs from the paired feed caption
            # (an unedited paired story is cloned FROM the feed, so it matches).
            if story_cap and story_cap != feed_cap:
                edited[key] = story_cap
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
                         angle="", avoid_angles=(), avoid_captions=(),
                         recent_formulas=(), require_media=True,
                         avoid_categories=(), prefer_photos=False):
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
    (a dup is rejected), unlike the STYLE-only opening guidance. Empty => unchanged.

    recent_formulas (ECHO_OPENING_FORMULA_CAP, Tough Temple 2026-09-05): the opening
    FORMULAS of this build's recently accepted posts, oldest to newest. A draft that
    would extend an unbroken run of config.opening_formula_max_run() posts sharing one
    frame ("You walk in ...", "You showed up ...", "You've been ..." fifteen days
    running) is passed over and the neighbour-day walk looks for a different frame.
    PREFERENCE, not a block: if the walk finds nothing that varies the frame, the best
    otherwise-acceptable draft is still returned, so this can never thin a calendar.
    Flag OFF or empty => unchanged.

    require_media (2026-09-07): DEFAULT TRUE, and every calendar-building caller keeps
    it, because a post without a photo is not a post. FALSE is the CAPTION-ONLY lane:
    grade_fix's repair passes rewrite the caption of a day that already carries its own
    approved photo, so they borrow the draft's words and discard its creative. On a gym
    whose library is fully served (hillcountry, live, 2026-09-07) the builder can still
    write a clean caption but has no unused image to attach, and the media check alone
    was throwing that caption away -- which is why every body-sameness repair reported
    'source material too thin' on books whose source material was fine."""
    from . import post_quality

    def _norm_caption(text):
        return " ".join((text or "").split()).strip().lower()

    _avoid = {_norm_caption(c) for c in (avoid_captions or ()) if (c or "").strip()}
    _avoid_categories = {str(c or "").strip().lower()
                         for c in (avoid_categories or ()) if str(c or "").strip()}
    _formula_cap = bool(recent_formulas) and config.opening_formula_cap_enabled()
    _formula_max = config.opening_formula_max_run() if _formula_cap else 0
    # The best draft that cleared every HARD gate but repeats the opening frame. It is
    # returned only if nothing better turns up, so the formula cap never drops a day.
    _formula_fallback = [None]

    def _formula_repeats(d):
        if not _formula_cap:
            return False
        from .drafter import formula_run_exceeded
        return formula_run_exceeded(getattr(d, "caption", "") or "",
                                    recent_formulas, _formula_max)

    def _accept(d):
        if d is None:
            return False
        # FORWARD-BOOK MIX: when a pillar is already heavier than another approved
        # pillar, keep walking the real source rotation instead of adding another post
        # to the heavy pillar. This is selection only: the accepted pillar still comes
        # from its own approved source and its label remains truthful.
        category = str(getattr(d, "category", "") or "").strip().lower()
        if category and category in _avoid_categories:
            return False
        # 2x uniqueness: never the same concept twice in one day (CADENCE_SPEC D5).
        if _avoid and _norm_caption(getattr(d, "caption", "")) in _avoid:
            return False
        # Check against the REAL target date, including when the approved-source
        # walk below generates on a neighbouring day and re-homes its draft.
        if config.caption_cooldown_enabled():
            caption_base = re.sub(
                r"_(?:ig|fb|gbp)$", "", getattr(account, "key", "") or "")
            # Caption-only/offline callers may have no account when cooldown is
            # off. An armed history guard requires the real tenant identity.
            if not caption_base or _caption_repeats_history(
                    caption_base, d.caption, day_key):
                return False
        # A+ caption gate is enforced whenever the real-caption engine (SB7) is on —
        # the production posture. With SB7 OFF the system is in its documented
        # deterministic baseline mode (source + CTA), where only the banned-word bar
        # applies, so a thin source is not dropped and the baseline stays usable.
        if config.sb7_enabled():
            hard_ok = post_quality.is_a_plus(d, banned_words,
                                             require_media=require_media)
        else:
            hard_ok = not _has_banned_word(d.caption, banned_words)
        if not hard_ok:
            return False
        # Every HARD gate is cleared. The opening formula cap is a PREFERENCE, not a
        # gate: remember this draft and keep looking for one that varies the frame.
        # If nothing does, this one is placed anyway (a day is never dropped for it).
        if _formula_repeats(d):
            if _formula_fallback[0] is None:
                _formula_fallback[0] = d
            return False
        return True

    # Primary attempt on the real day. NO template_fn: the day uses the gym's real photo.
    draft = client_content.build_client_draft(account, day_key, voice, library_path,
                                              exclude_keys=exclude_keys,
                                              avoid_openings=avoid_openings,
                                              allow_reuse=allow_reuse,
                                              angle=angle, avoid_angles=avoid_angles,
                                              prefer_photos=prefer_photos,
                                              record_serve=False)
    if draft is None:
        return None, None
    if _accept(draft):
        return draft, None
    first_issues = post_quality.post_issues(draft, banned_words,
                                            require_media=require_media)

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
                                                prefer_photos=prefer_photos,
                                                record_serve=False)
        if _accept(alt):
            # Re-home the alternative draft onto the real day so the calendar row sits
            # on day_key (only the day is re-pointed; the caption/source/photo are the
            # real approved ones the builder produced).
            alt.day_key = day_key
            alt.scheduled_for = draft.scheduled_for
            return alt, None
    # Nothing in the walk varied the opening frame. The formula cap NEVER drops a day:
    # place the best draft that cleared every hard gate and say the frame repeated.
    if _formula_fallback[0] is not None:
        keep = _formula_fallback[0]
        keep.day_key = day_key
        keep.scheduled_for = draft.scheduled_for
        log(f"{day_key}: no approved source varied the opening frame; keeping the "
            "best post and letting the run stand (never a dropped day)")
        return keep, None
    return None, f"not A+: {'; '.join(first_issues)}"


def _approved_gym_ask(voice):
    """The gym's OWN approved CTA to use as the ask-coverage default, or "" when it
    has none usable.

    Only a CTA the gym already approved in its voice doc is eligible, and only one
    that reads as EXACTLY ONE ask family (publish_guard.ask_families) so the lane
    cannot emit a multi-ask caption that publish_guard would then refuse. A CTA
    carrying a dash is passed over, per the copy rules. No approved CTA qualifies
    -> "" and the caller skips the lane: a gym never gets an invented ask."""
    try:
        from .publish_guard import ask_families
    except Exception:  # noqa: BLE001
        return ""
    for cta in (getattr(voice, "ctas", None) or ()):
        text = str(cta or "").strip()
        if not text or "-" in text or "–" in text or "—" in text:
            continue
        try:
            if len(ask_families(text)) == 1:
                return text
        except Exception:  # noqa: BLE001
            continue
    return ""


def _record_feed_served(account, feed, day_key):
    """Record an ACCEPTED feed's photo as served for rotation — only once the day has cleared
    the A+ gate, the real-creative check, and the no-reuse check, i.e. it is actually KEPT.
    This replaces the old pick-time record inside build_client_draft that poisoned the ledger
    (see build_client_draft record_serve). Returns False when the durable write cannot
    be proved; callers must drop that feed before the calendar write."""
    try:
        from . import rotation, dam
        path = (getattr(feed, "creative_path", "") or "").strip()
        if not path:
            return False
        reserve = (rotation.reserve_local_media_once if os.path.splitext(path)[1].lower()
                   in (".mp4", ".mov", ".m4v", ".webm")
                   else rotation.reserve_local_photo_once)
        reservation_id = reserve(
            account.key, dam.rotation_key(path),
            getattr(feed, "category", "") or "", day_key, path=path)
        if reservation_id:
            setattr(feed, "_served_reservation_id", reservation_id)
        return bool(reservation_id)
    except Exception as e:  # noqa: BLE001
        print(f"[client-month] served-record failed for {day_key}: {type(e).__name__}: {e}")
        return False


def _row_from_draft(base_key, draft):
    """One draft folded into a content_calendar row using the SAME mapping the real
    month/mirror use (gym_id=base_key). PAUSED status by construction (the draft is
    PENDING; _real_row maps that to 'pending')."""
    row = _mirror._real_row(base_key, draft)
    reservation_id = getattr(draft, "_served_reservation_id", None)
    if reservation_id:
        row["_served_reservation_id"] = reservation_id
    # New client pairs receive their identity at _finish_feed_with_story, before the
    # Story is cloned.  Existing drafts have no attribute and stay untouched: this
    # forward-only writer never invents an identity while rebuilding old calendar rows.
    if config.logical_post_id_enabled():
        logical_post_id = (getattr(draft, "logical_post_id", "") or "").strip()
        if logical_post_id:
            try:
                uuid.UUID(logical_post_id)
            except (AttributeError, TypeError, ValueError) as exc:
                # Fail closed BEFORE _to_rows/_apply: an unverifiable identity
                # must never reach a row the store validates after a month has
                # already been deleted.
                raise ValueError(
                    "invalid pre-stamped logical_post_id on draft "
                    f"{getattr(draft, 'draft_id', '?')}: {logical_post_id!r}") from exc
            row["logical_post_id"] = logical_post_id
    return row


def _release_unlanded_reservations(drafts, retained=()):
    """Release exact served rows for drafts that never became calendar rows."""
    from . import rotation
    keep = {int(r) for r in (retained or ()) if r}
    seen = set()
    for draft in drafts or ():
        rid = getattr(draft, "_served_reservation_id", None)
        if not rid or int(rid) in keep or int(rid) in seen:
            continue
        seen.add(int(rid))
        rotation.release_served(rid)


def _release_feed_reservation(feed):
    """Release one feed reserved before finishing could add it to ``drafts``."""
    _release_unlanded_reservations([feed])
    try:
        delattr(feed, "_served_reservation_id")
    except (AttributeError, TypeError):
        pass


def _finish_feed_with_story(account, feed, library_path, log, *, day_key="",
                            story_caption_override=None):
    """Run an accepted FEED draft through its media-processing lanes (action-cut reel,
    video poster, feed autofit) and build its PAIRED STORY on the same photo, returning
    the drafts to emit for the day: [feed] or [feed, story].

    Extracted VERBATIM from build_client_month's per-day tail so the denied-slot backfill
    produces IDENTICAL feed+story cards (same lanes, same captionless-story guard). Mutates
    feed.creative_public_url in place via the lanes. The caller owns loop state (built_days,
    opening variety); this helper is stateless beyond the drafts it returns."""
    # The global visual writer needs the exact hosted object that existed before any
    # local rendition lane runs.  Keep that identity on both paired drafts while its
    # guard is armed; the writer, not this planner, performs the later byte proof.
    # No URL is inferred from a rendered output.
    raw_source = _capture_raw_hosted_source(feed, log, day_key)
    if _visual_writer_guard_enabled() and not raw_source:
        log(f"held {day_key} feed: visual provenance requires a raw hosted source URL")
        return []
    if (_visual_writer_guard_enabled()
            and raw_source != (getattr(feed, "creative_public_url", "") or "").strip()):
        # A pre-rendered Drive/legacy draft is already a distinct rendition.  This
        # producer has no route to attach its owner receipt to insert_rows, so retain
        # its explicit raw source but hold the slot rather than stage unproven lineage.
        log(f"held {day_key} feed: transformed media lacks owner-attested render evidence")
        return []

    # ACTION-CUT REEL (AGENT_CLIENT_VIDEO_EDIT, OFF by default): a VIDEO draft is edited
    # into a fast-cut 9:16 reel and the draft's creative swaps to the hosted edit. Any
    # failure keeps the raw video; approval gate unchanged.
    _maybe_edit_video(account, feed, library_path, log)
    # VIDEO PREVIEW: a video shows BLANK in the calendar slot; host a poster frame so the
    # client sees a real frame. Display-only; best effort. Under the armed writer-prep
    # guard a video preview must carry byte-bound render evidence: a failure HOLDS the
    # whole slot (return []) rather than stage a distinct unproven thumbnail.
    if not _attach_video_poster(account, feed, library_path, log):
        log(f"held {day_key} feed: video poster lacks owner-attested render evidence")
        return []
    # FEED AUTOFIT (AGENT_FEED_AUTOFIT, OFF by default): an out-of-spec feed PHOTO is
    # re-framed to 1080x1350. Snapshot the pre-autofit media FIRST so the paired story
    # never inherits the formatted feed card.
    _pre_autofit_url = getattr(feed, "creative_public_url", "")
    _maybe_format_feed(account, feed, library_path, log)
    _mark_feed(feed)
    # A feed and its paired Story are one logical post even though they become three
    # calendar rows after the Facebook mirror.  Mint before cloning so both drafts
    # carry the same opaque identity without relying on image, date, or caption.
    # A retry can pass the same draft through this helper again, so preserve its
    # existing identity and mint only for a new pair.
    if config.logical_post_id_enabled():
        existing = (getattr(feed, "logical_post_id", "") or "").strip()
        if existing:
            try:
                uuid.UUID(existing)
            except (AttributeError, TypeError, ValueError) as exc:
                # FATAL, never a media hold: callers treat [] as "held slot" and
                # the build would continue to _apply's delete/insert with the bad
                # draft still staged.  Rollback happens in the callers (local
                # reservation release / Drive asset rollback) which re-raise.
                log(f"abort {day_key} feed: invalid pre-stamped logical_post_id "
                    "(an unverifiable identity aborts the whole rebuild BEFORE "
                    "any delete)")
                raise InvalidLogicalPostIdentity(
                    f"invalid pre-stamped logical_post_id on feed "
                    f"{getattr(feed, 'draft_id', '?')} for {day_key}: "
                    f"{existing!r}") from exc
            feed.logical_post_id = existing
        else:
            feed.logical_post_id = str(uuid.uuid4())
    out = [feed]

    # PAIRED STORY on the SAME photo (cloned from the feed; no second media consumed).
    story = _story_from_feed(feed)
    # dataclasses.replace intentionally drops dynamic fields, so retain the pair
    # identity explicitly on the Story clone.
    if config.logical_post_id_enabled():
        story.logical_post_id = feed.logical_post_id
    # The story must NOT carry the feed's 4:5 autofit reframe: restore the pre-autofit
    # media (story-format ON rebuilds a fresh 1080x1920; this keeps it correct when OFF).
    if getattr(story, "creative_public_url", "") != _pre_autofit_url:
        try:
            story.creative_public_url = _pre_autofit_url
        except Exception:  # noqa: BLE001 - a frozen/edge draft never blocks the build
            pass
    if _visual_writer_guard_enabled() and raw_source:
        # dataclasses.replace deliberately drops dynamic draft fields, so copy the
        # exact raw source explicitly.  The story's existing source-media path below
        # still owns caption re-burn provenance when that separate flag is armed.
        try:
            story.source_media_url = raw_source
        except Exception:  # noqa: BLE001 - a frozen draft is held below
            log(f"held {day_key} story: visual provenance could not retain raw source")
            return [feed]
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
_PHOTO_KIND = "photo"        # gym_media_index.KIND_PHOTO, without the import cycle
_VIDEO_KIND = "video"        # gym_media_index.KIND_VIDEO, without the import cycle


def _gym_drive_source_for(account_key, day_key, slot_i=0):
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
        #
        # slot_i OFFSETS the rotation so a 2x day's two posts draw DIFFERENT approved
        # facts. category_for_day is deterministic per (account, day), so without the
        # offset both slots got the same source and the generator produced the same
        # caption twice on one day.
        offset = int(slot_i or 0)
        src = None
        for step in range(len(present)):
            cat = present[(client_content._day_ordinal(day_key)  # noqa: SLF001
                           + offset + step) % len(present)]
            src = client_content._source_for_day(  # noqa: SLF001
                account_key, day_key, cat, present)
            if src is not None:
                break
        if src is None:
            ordered = (present[offset % len(present):] + present[:offset % len(present)])
            for cat in ordered:
                items = client_sources.approved_sources(account_key, category=cat)
                if items:
                    src = items[offset % len(items)]
                    break
        return src
    except Exception:  # noqa: BLE001 - no approved source resolvable -> skip the lane
        return None


def _rollback_drive_asset(draft, day_key, log):
    """Return a dropped Drive draft's asset to the pool. build_gym_media_draft stamps
    used_count/last_used_at at BUILD time, so a draft we then decline to stage would
    otherwise burn that photo for the 90-day reuse cooldown. Best effort, never raises.

    Scoped to THIS asset on THIS date, never a cross-date rollback_asset: use-records
    are not cleared on publish, so the same photo re-staged after its cooldown still
    carries the record of its earlier PUBLISHED post, and undoing that would re-pool
    an image currently live on the gym's feed."""
    asset_id = (getattr(draft, "source_media_asset_id", "") or "").strip()
    account_key = getattr(draft, "account_key", "") or ""
    if not asset_id or not account_key or not day_key:
        return
    try:
        from . import db, gym_media_selector
        restored = gym_media_selector.rollback_use(
            account_key, day_key, asset_id=asset_id, restore_unstaged=True)
        if restored:
            base = gym_media_selector.base_gym_key(account_key)
            claim_id = getattr(draft, "_drive_claim_id", None)
            if not claim_id:
                from . import gym_media_index
                asset = gym_media_index.default_store().get_asset(asset_id)
                claim_id = gym_media_selector.drive_content_claim_id(base, asset)
            db.socialapi_claim_release(
                claim_id, f"{base}_gbp")
    except Exception as exc:  # noqa: BLE001 - a rollback failure never sinks the build
        log(f"[gym-drive] could not return asset {asset_id} to the pool "
            f"({type(exc).__name__})")


def _release_wipeable_drive_assets(base_key, start, days, store, log, locked_days=()):
    """Prior calendar placements permanently consume their Drive assets.

    Keep this compatibility hook so callers do not restore or double-stamp rows
    that survive a no-op rebuild. Only a new draft that never reached the calendar
    may restore its stamp via _rollback_drive_asset.
    """
    return []


def _restore_released_drive_assets(base_key, released, log):
    """Compatibility no-op: a rebuild never releases calendar-used assets."""
    return None


def _rollback_new_drive_drafts(drafts, log):
    """The build wrote nothing, so every Drive draft it built (and stamped at build
    time) never landed: return those assets to the pool."""
    seen = set()
    for d in drafts or []:
        aid = (getattr(d, "source_media_asset_id", "") or "").strip()
        day = (getattr(d, "day_key", "") or "")[:10]
        if aid and day and (aid, day) not in seen:
            seen.add((aid, day))
            _rollback_drive_asset(d, day, log)


def _fill_uncovered_days(account, base_key, voice, library_path, banned_words, log, *,
                         deferred_days, covered_days, locked_keys, used_keys, drafts,
                         store, start, days, edited_story_caps=None, max_fill=None,
                         local_days=0):
    """NO EMPTY DAYS (audit 2c, 2026-09-10), UNDER THE MEDIA CAP (audit R-A1). Lane A
    deferred these days to the Drive pool (every local creative was inside its repeat
    window and the pool had at least one pickable asset) and the Drive lane did not
    cover them all: a pool of 3 assets on a 10-day span would otherwise turn 10 repeats
    into 3 posts and 7 EMPTY days. Place the stale_reuse pick on each still-uncovered
    deferred day, spaced as far from its other appearances as the book allows
    (media_guard.spaced_choice), exactly the fallback the denied-slot backfill already
    uses. One feed+story pair per day: a repeat is a floor, never a full 2x day. Never
    fabricated; every A+ gate still runs.

    max_fill: Blake's standing rule, N photos -> at most N feeds, never pad. The
    caller passes max_feed_days minus every feed this build already placed (Lane A
    AND the Drive lane count against the same cap); beyond it days stay uncovered
    exactly as before this PR. None = no cap (callers always pass one).
    Returns the number of days filled."""
    todo = sorted(d for d in (deferred_days or ()) if d not in covered_days)
    if max_fill is not None:
        if max_fill <= 0:
            if todo:
                log(f"{base_key}: {len(todo)} deferred day(s) left uncovered, the "
                    "media cap is already met (N photos, at most N feeds)")
            return 0
        if len(todo) > max_fill:
            log(f"{base_key}: filling {max_fill} of {len(todo)} deferred day(s); the "
                "rest stay uncovered under the media cap")
            todo = todo[:max_fill]
    if not todo:
        return 0
    from . import media_guard
    guard_state = {}
    if media_guard.enabled():
        try:
            from datetime import timedelta
            span_dates = {(start + timedelta(days=i)).isoformat()[:10]
                          for i in range(max(1, days))}
            guard_state = media_guard.book_state(base_key, store, start, days, log=log,
                                                 skip_wipeable_dates=span_dates,
                                                 library_path=library_path)
        except Exception as exc:  # noqa: BLE001 - the guard never sinks a fill
            log(f"{base_key}: fallback guard read skipped ({type(exc).__name__})")
    filled = 0
    for day_key in todo:
        # 1. a repeat that at least differs from every photo this build already
        #    placed (used_keys) and from anything live (locked_keys)
        feed, drop = _clean_draft_for_day(
            account, day_key, voice, library_path, banned_words, log,
            exclude_keys=set(used_keys) | set(locked_keys), allow_reuse=True)
        if (feed is None or not _has_real_creative(feed)):
            # 2. the library is smaller than the days: the photo whose other
            #    appearances are FARTHEST from this day, never a live one
            choice = media_guard.spaced_choice(library_path, guard_state, day_key,
                                               hard_exclude=set(locked_keys))
            if choice:
                force_only = media_guard.library_keys(library_path) - {choice}
                feed, drop = _clean_draft_for_day(
                    account, day_key, voice, library_path, banned_words, log,
                    exclude_keys=set(locked_keys) | force_only, allow_reuse=True)
        if feed is None or not _has_real_creative(feed):
            log(f"{base_key} {day_key}: left empty, no A+ repeat could be built "
                f"({drop or 'no usable creative'})")
            continue
        feed_path = (getattr(feed, "creative_path", "") or "").strip()
        key = (_url_basename(getattr(feed, "creative_public_url", "") or "")
               or os.path.basename(feed_path))
        if not _record_feed_served(account, feed, day_key):
            log(f"{base_key} {day_key}: served ledger write failed; local feed held")
            continue
        raw_basename = os.path.basename(feed_path) if feed_path else ""
        media_guard.note_placed(guard_state, raw_basename or key, day_key)
        story_override = ((edited_story_caps or {}).get((str(day_key)[:10], None))
                          or (edited_story_caps or {}).get((str(day_key)[:10], 0)))
        try:
            finished = _finish_feed_with_story(
                account, feed, library_path, log, day_key=day_key,
                story_caption_override=story_override)
        except Exception:
            _release_feed_reservation(feed)
            raise
        if not finished:
            _release_feed_reservation(feed)
            log(f"{base_key} {day_key}: held; visual provenance could not stage the feed")
            continue
        drafts.extend(finished)
        covered_days.add(day_key)
        if key:
            used_keys.add(key)
        if raw_basename:
            used_keys.add(raw_basename)
        filled += 1
        log(f"{base_key} {day_key}: Drive pool exhausted for the day; placed a spaced "
            f"repeat ({key}) so the day is never empty")
    if filled:
        # TRUTHFUL DIGEST (audit R-A3 + round 4 #4): the reason these days repeated is
        # that the Drive pool ran short, not that the local library is small. The
        # small-library digest fires ONLY when the local library really is smaller
        # than the days IT covers (Lane A days + these fills); Drive-covered days are
        # distinct media and never count against the stills.
        log(f"{base_key}: Drive pool ran short; {filled} day(s) filled with spaced "
            "repeats from the uploaded library")
        try:
            lib_n = len(media_guard.library_keys(library_path))
            if lib_n and lib_n < int(local_days or 0) + filled:
                media_guard.alert_small_library(base_key, todo[0], log)
        except Exception:  # noqa: BLE001
            pass
    return filled


def _recaption_drive_draft(account, draft, voice, account_key, day_key, slot_i, log,
                           *, source=None, angle=""):
    """One fresh caption for a Drive draft that failed the A+ gate, on the SAME asset:
    the next approved source in the day's rotation (else the same one) and the failed
    opening as an avoid hint, through the same generator. Mutates draft.caption /
    hashtags in place; True when a different caption was produced. Never raises."""
    try:
        from .drafter import opening_signature
        prev = (getattr(draft, "caption", "") or "").strip()
        source = source or (_gym_drive_source_for(account_key, day_key, slot_i + 1)
                            or _gym_drive_source_for(account_key, day_key, slot_i))
        if source is None:
            return False
        avoid = tuple(s for s in (opening_signature(prev),) if s)
        # SAME GROUNDING as the builder's first attempt (audit round 5 minor): the
        # frame's name hint + the crop-verify result it recorded on the draft, so the
        # retry is written against the same shot, never from nothing.
        from .gym_media_builder import _PickedCreative
        grounding = getattr(draft, "caption_grounding", None) or {}
        creative = _PickedCreative(grounding.get("creative_name")
                                   or getattr(draft, "creative_path", "") or "")
        caption, tags = client_content.make_caption(
            account, source, voice, getattr(draft, "creative_path", "") or "",
            creative=creative, verified=grounding.get("verified"),
            avoid_openings=avoid, **({"angle": angle} if angle else {}))
        caption = (caption or "").strip()
        if not caption or caption == prev:
            return False
        draft.caption = caption
        draft.hashtags = tags or []
        if getattr(source, "category", ""):
            draft.category = source.category
        log(f"[gym-drive] {account_key} {day_key} slot {slot_i}: "
            "retried once with a fresh caption on the same asset")
        return True
    except Exception as exc:  # noqa: BLE001 - a retry failure is just "no retry"
        log(f"[gym-drive] {account_key} {day_key}: recaption failed ({type(exc).__name__})")
        return False


# One ordinary recaption plus at most seven distinct approved-source retries.
_DRIVE_HISTORY_RETRY_LIMIT = 7
# With SB7 disabled, copy is deterministic: scanning approved baselines does not
# call an LLM or claim another photo. Do not spend the seven retries on facts
# whose exact source+CTA copy is already known to repeat history.
_DRIVE_HISTORY_BASELINE_SCAN_LIMIT = 64


def _drive_history_sources(account_key, day_key, slot_i,
                           limit=_DRIVE_HISTORY_RETRY_LIMIT):
    """Walk distinct approved facts, interleaved across deterministic pillars.

    The old slot offset changes only the category. The within-category day index
    stays fixed, stranding the other facts whenever those first two captions were
    in history. Advance that index as well, without changing the scheduled date.
    """
    from . import client_sources, caption_ledger
    present = client_content._pillars_for(account_key)
    if not present:
        return []
    ordinal = client_content._day_ordinal(day_key)
    start = (ordinal + int(slot_i or 0) + 2) % len(present)
    categories = present[start:] + present[:start]
    pools = []
    for category in categories:
        items = client_sources.approved_sources(account_key, category=category)
        if items:
            offset = (ordinal // len(present) + 1) % len(items)
            pools.append(items[offset:] + items[:offset])
    tried = [_gym_drive_source_for(account_key, day_key, slot_i),
             _gym_drive_source_for(account_key, day_key, slot_i + 1)]
    # Source dataclasses are mutable, so identity uses approved fact text instead.
    seen = {caption_ledger.verbatim_normalize(getattr(source, "text", ""))
            for source in tried if source is not None}
    out = []
    for index in range(max((len(pool) for pool in pools), default=0)):
        for pool in pools:
            if index >= len(pool):
                continue
            source = pool[index]
            fact = caption_ledger.verbatim_normalize(source.text)
            if not fact or fact in seen:
                continue
            seen.add(fact)
            out.append(source)
    return out[:limit]


def _retry_drive_caption_history(account, draft, voice, base_key, account_key,
                                  day_key, slot_i, banned_words, day_captions, log):
    """Bounded genuine copy regeneration on the already selected photo."""
    from . import post_quality, caption_ledger
    from .drafter import angle_for_index
    sb7 = config.sb7_enabled()
    try:
        sources = _drive_history_sources(
            account_key, day_key, slot_i,
            _DRIVE_HISTORY_RETRY_LIMIT if sb7 else _DRIVE_HISTORY_BASELINE_SCAN_LIMIT)
    except Exception as exc:
        log(f"[gym-drive] {base_key} {day_key}: approved-source walk unavailable "
            f"({type(exc).__name__})")
        return False
    day_hashes = {caption_ledger.verbatim_hash(c) for c in day_captions}

    def rejections(candidate):
        """Classification only: never log caption text or confidential banned words."""
        caption = getattr(candidate, "caption", "") or ""
        reasons = []
        try:
            quality_ok = (post_quality.is_a_plus(candidate, tuple(banned_words or ()),
                                                 require_media=True)
                          if sb7 else not _has_banned_word(caption, tuple(banned_words or ())))
            if not quality_ok:
                reasons.append("a_plus" if sb7 else "banned_word")
        except Exception:
            reasons.append("quality_gate_error")
        if not caption.strip():
            reasons.append("empty_caption")
        if _caption_repeats_history(base_key, caption, day_key):
            reasons.append("history_180d")
        if caption_ledger.verbatim_hash(caption) in day_hashes:
            reasons.append("same_day")
        return reasons

    if not sb7:
        from copy import copy
        candidates = []
        rejected = {}
        scanned = 0
        for source in sources[:_DRIVE_HISTORY_BASELINE_SCAN_LIMIT]:
            scanned += 1
            # EXACT same creative key as _recaption_drive_draft: the approved CTA
            # is photo-keyed, so testing an arbitrary key could admit the wrong copy.
            candidate = copy(draft)
            try:
                candidate.caption, _ = client_content.compose_caption(
                    account, source, voice, getattr(draft, "creative_path", "") or "")
                reasons = rejections(candidate)
            except Exception:
                reasons = ["baseline_error"]
            if reasons:
                for reason in reasons:
                    rejected[reason] = rejected.get(reason, 0) + 1
                continue
            candidates.append(source)
            if len(candidates) >= _DRIVE_HISTORY_RETRY_LIMIT:
                break
        log(f"[gym-drive] {base_key} {day_key}: SB7 disabled; approved baseline "
            f"scan checked {scanned} facts, found {len(candidates)} candidates; "
            f"rejections={rejected}")
        sources = candidates

    for attempt, source in enumerate(sources[:_DRIVE_HISTORY_RETRY_LIMIT], 1):
        # Different facts and entry angles produce real copy, never a suffix.
        if not _recaption_drive_draft(
                account, draft, voice, account_key, day_key, slot_i, log,
                source=source, angle=angle_for_index(attempt + int(slot_i or 0))):
            reasons = rejections(draft)
            log(f"[gym-drive] {base_key} {day_key}: approved-source attempt "
                f"{attempt} produced no changed copy "
                f"({', '.join(reasons) or 'generation_unavailable'}); same photo retained")
            continue
        reasons = rejections(draft)
        if not reasons:
            log(f"[gym-drive] {base_key} {day_key}: approved-source walk admitted "
                f"caption on attempt {attempt}; same photo retained")
            return True
        log(f"[gym-drive] {base_key} {day_key}: approved-source attempt "
            f"{attempt} rejected ({', '.join(reasons)}); same photo retained")
    log(f"[gym-drive] {base_key} {day_key}: approved-source walk exhausted "
        f"{min(len(sources), _DRIVE_HISTORY_RETRY_LIMIT)} candidate facts; "
        "photo remains unplaced")
    return False


def append_gym_drive_drafts(account, base_key, start, days, voice, *, log,
                            covered_days, drive=None, store=None,
                            library_path="", slots_per_day=1, banned_words=(),
                            rendition_budget=None, covered_slots=None,
                            video_beats_only=False, kind_prefs=None,
                            day_captions_seed=None, failed_assets=None,
                            max_feed_count=None):
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
    Drive lane FILLS THE GAPS instead of doubling up a day.

    Emits the SAME card shape as the uploaded-media loop: every feed goes out through
    _finish_feed_with_story (paired story on the same asset) and honors slots_per_day,
    so a gym on 2x gets two Drive pairs a day and its rows carry the slot ordinal.
    (Before 2026-08-30 this lane emitted a bare feed and hard-coded one post per day,
    which is what left Dale/ENG with no stories and a single daily post after his 2x
    toggle — the Drive lane had quietly taken over his whole forward month.)

    TWO MODES (audit round 5 MAJOR 1). Gap-fill (default): every slot of every day
    the uploaded-media loop left uncovered. VIDEO PRE-PASS (video_beats_only=True,
    kind_prefs=("video",)): runs BEFORE Lane A and claims only the (day, slot) video
    beats of the mix (gym_media_builder.is_video_slot) with a Drive VIDEO; a beat the
    pool cannot serve is simply left to Lane A (no photo fallback, no day break).
    Without this, Lane A's precedence made the mix inert for any gym with enough fresh
    local stills (Tough Temple: 95 stills, 57 videos, zero video days).

    covered_slots: a shared set of (day_key, slot_index) this call must skip and to
    which it ADDS every slot it stages, so Lane A, the pre-pass and the gap-fill never
    place two feeds in one slot (2x: one slot may be a Lane A still, the other a Drive
    video). day_captions_seed: {day_key: [captions]} already placed on a day by another
    lane, so the same-concept guard sees them.

    failed_assets: a BUILD-LOCAL set (shared by the pre-pass and the gap-fill call) of
    Drive asset ids whose caption failed the A+ gate twice this build. They are passed
    to the builder as exclude_ids so a poisoned asset (least-used again the moment it
    is rolled back) cannot be re-picked on every later beat and starve the month
    (final verification g).

    max_feed_count: optional cap across this call's feed drafts (stories excluded).
    The month pre-passes use it to preserve the existing whole-build media ceiling.

    NEVER LOSES WORK (final verification h): an exception escaping any step after the
    builder returns rolls the in-flight draft's asset back and RETURNS the drafts
    already finished, so covered_slots, the returned drafts and the usage stamps stay
    consistent; the caller (and Lane A) carry on from the partial result."""
    from datetime import timedelta
    from . import gym_media_builder
    extra = []
    covered = {str(d)[:10] for d in (covered_days or ())}
    platform = getattr(account, "platform", None) or ""
    account_key = getattr(account, "key", "") or base_key
    pillar_i = 0
    slots = 2 if int(slots_per_day or 1) == 2 else 1
    failed = failed_assets if failed_assets is not None else set()
    staged_feeds = 0
    for i in range(days):
        if (max_feed_count is not None
                and staged_feeds >= max(0, int(max_feed_count))):
            break
        day_key = (start + timedelta(days=i)).isoformat()
        if day_key in covered:
            continue
        # captions already placed on THIS day (2x uniqueness), seeded with the other
        # lanes' placements on the same day
        day_captions = list((day_captions_seed or {}).get(day_key, []))
        for slot_i in range(slots):
            if (max_feed_count is not None
                    and staged_feeds >= max(0, int(max_feed_count))):
                break
            if covered_slots is not None and (day_key, slot_i) in covered_slots:
                continue                   # another lane already owns this slot
            if video_beats_only and not gym_media_builder.is_video_slot(day_key, slot_i):
                continue                   # the pre-pass claims video beats only
            pillar = _GYM_DRIVE_PILLARS[pillar_i % len(_GYM_DRIVE_PILLARS)]
            source = _gym_drive_source_for(account_key, day_key, slot_i)
            if source is None:
                # No approved fact for the copy: a Drive photo never posts on imagination.
                break
            try:
                # slot_index feeds the media mix (gym_media_builder.kinds_for_slot) so
                # the AM and PM slots of a 2x day can differ in kind and a re-run stages
                # the same photo/video shape.
                draft = gym_media_builder.build_gym_media_draft(
                    account, day_key, pillar, voice, source, store=store, drive=drive,
                    slot_index=slot_i, rendition_budget=rendition_budget,
                    kind_prefs=kind_prefs, exclude_ids=tuple(sorted(failed)))
            except Exception as e:  # noqa: BLE001 - the lane never sinks the month
                log(f"[gym-drive] builder failed for {base_key} {day_key}: "
                    f"{type(e).__name__}: {e}")
                draft = None
            if draft is None:
                if video_beats_only:
                    continue               # this beat goes to Lane A; try the next slot
                # Empty pool / gate miss: the builder already alerted if needed. Stop
                # this day rather than retry the same starved pool for slot 2.
                break
            try:
                placed = _stage_drive_draft(
                    account, base_key, account_key, platform, draft, day_key, slot_i,
                    slots, voice, banned_words, day_captions, library_path, log,
                    failed, extra, covered_slots, pillar, video_beats_only)
            except InvalidLogicalPostIdentity:
                # FATAL identity defect: return the in-flight Drive asset to the
                # pool, then abort the WHOLE rebuild (never a media hold).
                _rollback_drive_asset(draft, day_key, log)
                raise
            except Exception as e:  # noqa: BLE001 - never lose the drafts already made
                log(f"[gym-drive] {base_key} {day_key} slot {slot_i}: staging raised "
                    f"{type(e).__name__}: {e}; returning the {len(extra)} draft(s) already "
                    "finished, the in-flight asset returned to the pool")
                _rollback_drive_asset(draft, day_key, log)
                return extra
            if not placed:
                break
            staged_feeds += 1
            pillar_i += 1
        if not video_beats_only:
            covered.add(day_key)
    return extra


def _stage_drive_draft(account, base_key, account_key, platform, draft, day_key, slot_i,
                       slots, voice, banned_words, day_captions, library_path, log,
                       failed, extra, covered_slots, pillar, video_beats_only):
    """Gate, guard, finish and append ONE built Drive draft. True when it was placed;
    False when it was dropped (asset rolled back) and the caller should stop this day.
    Split out of append_gym_drive_drafts so its exception path is one place."""
    # A+ GATE (Blake grades calendars; the denied-slot backfill already
    # enforces this on its Drive-first replacement and the uploaded-media
    # loop on every pick): a Drive caption that fails A+ / carries a banned
    # word is DROPPED, its asset returned to the pool, never staged because
    # it came from a different lane.
    try:
        from . import post_quality as _pq
        _gate_ok = (_pq.is_a_plus(draft, tuple(banned_words or ()), require_media=True)
                    if config.sb7_enabled()
                    else not _has_banned_word(getattr(draft, "caption", "") or "",
                                              tuple(banned_words or ())))
    except Exception as e:  # noqa: BLE001 - a gate error is a fail, never a pass
        log(f"[gym-drive] {base_key} {day_key}: A+ gate errored "
            f"({type(e).__name__}); dropping the draft")
        _gate_ok = False
    _history_repeat = _caption_repeats_history(
        base_key, getattr(draft, "caption", "") or "", day_key)
    _history_walk_needed = _history_repeat
    if _history_repeat:
        log(f"[gym-drive] {base_key} {day_key}: caption repeats 180 day history; "
            "retrying approved copy on the same photo")
        _gate_ok = False
    if not _gate_ok:
        # RETRY ONCE WITH A FRESH CAPTION ON THE SAME ASSET (audit round 4 #3):
        # dropping the day here sent a video beat to a still repeat over a
        # caption problem the asset had nothing to do with.
        if _recaption_drive_draft(account, draft, voice, account_key, day_key,
                                  slot_i, log):
            try:
                _gate_ok = (_pq.is_a_plus(draft, tuple(banned_words or ()),
                                          require_media=True)
                            if config.sb7_enabled()
                            else not _has_banned_word(
                                getattr(draft, "caption", "") or "",
                                tuple(banned_words or ())))
            except Exception:  # noqa: BLE001
                _gate_ok = False
            _history_repeat = _caption_repeats_history(
                base_key, getattr(draft, "caption", "") or "", day_key)
            if _history_repeat:
                _gate_ok = False
    if not _gate_ok and (_history_walk_needed or _history_repeat):
        _gate_ok = _retry_drive_caption_history(
            account, draft, voice, base_key, account_key, day_key, slot_i,
            banned_words, day_captions, log)
    if not _gate_ok:
        failure = ("failed the caption history gate twice" if _history_repeat
                   else "failed the A+/banned-word gate twice")
        log(f"[gym-drive] {base_key} {day_key} slot {slot_i}: dropped, the caption "
            f"{failure}")
        _rollback_drive_asset(draft, day_key, log)
        # POISONED ASSET (final verification g): rolled back, it is the pool's
        # least-used candidate again; keep it out of every later beat this build.
        aid = (getattr(draft, "source_media_asset_id", "") or "").strip()
        if aid:
            failed.add(aid)
        return False
    # NEVER THE SAME CONCEPT TWICE IN ONE DAY (the uploaded loop's rule). The
    # slot-offset source rotation above should already differ, but this is the
    # hard guard: a repeat caption is DROPPED rather than staged, so a 2x day
    # can never publish the same words twice.
    _cap = (getattr(draft, "caption", "") or "").strip()
    if _cap and _cap in day_captions:
        log(f"[gym-drive] {base_key} {day_key} slot {slot_i}: dropped, its "
            "caption repeats the day's other post")
        _rollback_drive_asset(draft, day_key, log)
        return False
    day_captions.append(_cap)
    # Cross-post platform parity with the uploaded-media feed (the FB mirror in
    # _to_rows keys off an ig/empty account); leave the platform as the account's.
    if not (getattr(draft, "platform", "") or "").strip():
        try:
            draft.platform = platform
        except Exception:  # noqa: BLE001 - a frozen draft never blocks the build
            pass
    draft.day_key = day_key
    # SAME cards as the uploaded-media loop: feed + its paired story on the one
    # asset, through the shared helper (video edit, poster, autofit, captionless
    # guard). A story that cannot carry its caption is still dropped in there.
    day_drafts = _finish_feed_with_story(
        account, draft, library_path, log, day_key=day_key)
    if not day_drafts:
        _rollback_drive_asset(draft, day_key, log)
        # The rollback makes this unattestable rendition least-used again. Exclude it
        # for the remainder of this build so the next day can reach a different,
        # provable Drive asset instead of repeatedly starving the pool.
        aid = (getattr(draft, "source_media_asset_id", "") or "").strip()
        if aid:
            failed.add(aid)
        log(f"[gym-drive] {base_key} {day_key} slot {slot_i}: held; visual provenance "
            "could not stage the feed")
        return False
    if slots == 2:
        for d in day_drafts:
            try:
                d.cadence_slot_index = slot_i
            except Exception:  # noqa: BLE001 - a frozen draft never blocks
                pass
    extra.extend(day_drafts)
    if covered_slots is not None:
        covered_slots.add((day_key, slot_i))
    log(f"[gym-drive] {base_key} {day_key}: staged a {pillar} "
        f"{'video ' if video_beats_only else ''}post from the connected Drive "
        f"pool (asset {draft.source_media_asset_id}), PENDING")
    return True


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
    #
    # EXCEPT a gym connected via Google Drive ONLY (zero content_library uploads, the
    # exact self-serve portal flow — Dean Holcomb / CrossFit Reverb, 2026-08-30, 190
    # Drive-synced assets and 0 in content_library): the GYM-DRIVE LANE further down
    # (GYM_DRIVE_STAGE + gym_drive_connect_active_for) is ALREADY correctly gated and
    # already no-ops safely on an empty Drive pool, so it deserves a chance to run
    # instead of this function returning awaiting_media forever with no path forward
    # except a human noticing and building the month by hand. Both Drive flags stay
    # exactly as strict as today; this only stops the LOCAL-media short-circuit from
    # pre-empting them.
    media_count = _client_media_count(library_path)
    drive_lane_may_cover = (config.gym_drive_stage_enabled()
                            and config.gym_drive_connect_active_for(base_key))
    if media_count <= 0 and not drive_lane_may_cover:
        log(f"{base_key}: waiting for client media (no photos/videos uploaded yet); "
            "nothing rendered, nothing written")
        return {"ok": False,
                "reason": "waiting for client media (no photos/videos uploaded yet)",
                "awaiting_media": True, "upserted": 0, "days": 0, "skipped_banned": 0,
                "media_count": 0}
    if media_count <= 0:
        log(f"{base_key}: no content_library uploads but a connected Drive pool is "
            "active; giving the Drive lane a chance to build the month")

    from datetime import date, timedelta
    start = start_date if isinstance(start_date, date) \
        else date.fromisoformat(str(start_date)[:10])

    # HARD PLANNING HORIZON (Blake, 2026-08-28): Echo builds at most one month out —
    # the monthly relearn rebuilds anything further, so a longer span is pure token
    # waste. One clamp, one place (plan_horizon.horizon_clamp): a days=60 request is
    # clamped with ONE honest log line; clamping only shortens the TAIL of the span
    # (cadence never gaps inside the month). Existing rows are untouched (the cap
    # governs what gets BUILT, never a retroactive sweep). AGENT_PLAN_HORIZON_DAYS=0
    # disables (emergency only).
    from .plan_horizon import horizon_clamp
    days = horizon_clamp(start, days, logger=log, label=base_key)
    if days <= 0:
        log(f"{base_key}: the whole requested window starts beyond the planning "
            "horizon; nothing built")
        return {"ok": False, "reason": "plan window is beyond the planning horizon",
                "upserted": 0, "days": 0, "skipped_banned": 0,
                "media_count": media_count}

    # PER-GYM BUILD LOCK (ticket 4941e162, CrossFit Reverb, 2026-09-11): refuse a
    # SECOND concurrent rebuild for the same gym rather than race it. Ground truth
    # in content_calendar showed five separate insert timestamps inside two hours,
    # several landing near-duplicate captions on the SAME post_date side by side --
    # only possible when two build_client_month calls (the nightly client_media_sync
    # scan, a FIXER-triggered manual restage, a stale-run retry that was never
    # actually killed) overlapped: delete_month then insert is idempotent ACROSS
    # serial reruns, never across CONCURRENT ones. See agent/build_lock.py. A gym
    # already mid-build answers a clean no-op, never a partial/duplicated write.
    from . import build_lock as _build_lock
    _lock_holder = f"{os.getpid()}:{id(store)}"
    if not _build_lock.acquire(base_key, holder=_lock_holder):
        log(f"{base_key}: rebuild already in progress for this gym; skipping "
            "this call rather than racing it (see agent/build_lock.py)")
        return {"ok": False, "reason": "build_in_progress", "upserted": 0,
                "days": 0, "skipped_banned": 0, "media_count": media_count}
    # HEARTBEAT (2026-09-11 follow-up audit): a legitimate build can run well
    # past any static timeout (transcodes + captions + vision calls stack up,
    # see agent/build_lock.py's module docstring for the cited worst case).
    # Renewing on an interval, independent of build phase, means staleness is
    # judged by "still heartbeating" rather than "still under some fixed
    # ceiling" -- this MUST be stopped before release() in the finally below.
    _heartbeat = _build_lock.start_heartbeat(base_key, holder=_lock_holder)

    # LOCKED-CALENDAR AWARENESS: read the gym's EXISTING human-owned rows (approved /
    # published / denied / killed — anything a rebuild must preserve) across the span
    # BEFORE planning, so the rebuild composes with them instead of fighting them:
    #   * locked_feed_slots: a (day, slot) whose feed a human already owns is SKIPPED
    #     (no replacement feed, no orphan story/FB-mirror alongside the approved post);
    #     a 2x day with only one slot owned can still fill the other;
    #   * locked_feed_days: dates with any locked feed, so pending siblings of the
    #     owned slot survive the delete and the empty-day filler does not double-post;
    #   * used_keys: the photos those rows carry are EXCLUDED from every pick, so an
    #     already-approved photo is never re-placed on another day (no double-post).
    # Without this the builder re-picked approved photos (double-place) and photos
    # consumed by pruned colliding rows were lost forever (under-build).
    locked_feed_days, used_keys, locked_feed_slots = _locked_calendar_state(
        base_key, start, days, store, log, library_path=library_path)
    # The LIVE photos (approved/published/surviving) as read above, before this build's
    # own picks join used_keys: the no-empty-day fallback may repeat a photo this build
    # placed, never one that is live elsewhere.
    locked_keys = set(used_keys)
    # REBUILD RELEASES ITS OWN ROWS' DRIVE ASSETS (audit D3): the wipeable rows _apply
    # will delete return their assets to the pool BEFORE we pick, so a second build in
    # the month sees the same pool the first one did. Re-stamped below if nothing is
    # written. The per-gym pool answer cache is cleared so the gate reads fresh.
    released_drive = _release_wipeable_drive_assets(
        base_key, start, days, store, log, locked_days=locked_feed_days)
    client_content.clear_drive_pool_cache()
    # Days Lane A handed to the Drive pool instead of placing a stale repeat; whatever
    # the Drive lane does not cover is filled by _fill_uncovered_days (never empty).
    drive_deferred_days = set()
    # ONE transcode budget for the whole build (audit R-D1 #3).
    from . import gym_media_index as _gmi
    rendition_budget = _gmi.RenditionBudget(config.rendition_max_per_build())
    drafts = []
    # RELEASE -> APPLY is one transaction from the pool's point of view (audit D3
    # residual): an uncaught raise anywhere between here and the write must put the
    # released stamps back and undo this build's own unlanded picks.
    _applied = {"result": None}
    try:
        _result = _build_client_month_body(
            account, base_key, start, days, voice=voice, library_path=library_path,
            store=store, banned_words=banned_words, log=log, allow_reshape=allow_reshape,
            media_count=media_count, locked_feed_days=locked_feed_days,
            locked_feed_slots=locked_feed_slots,
            used_keys=used_keys, locked_keys=locked_keys, drafts=drafts,
            drive_deferred_days=drive_deferred_days, rendition_budget=rendition_budget,
            apply_state=_applied)
        _applied["result"] = _result
        return _result
    finally:
        _res = _applied["result"] or {}
        if _applied["result"] is None:
            # _apply catches remote write failures; an exception escaping the
            # build before a result is returned is a prewrite planning failure.
            _release_unlanded_reservations(drafts)
            _rollback_new_drive_drafts(drafts, log)
            # apply_state records the result immediately after _apply returns,
            # before any post-write reporting can raise, so result-is-None means
            # NO calendar write happened: the OLD wipeable rows survive, and
            # their released Drive assets must be stamped again (an
            # InvalidLogicalPostIdentity prewrite abort previously stranded them).
            _restore_released_drive_assets(base_key, released_drive, log)
        wrote = bool(_res.get("inserted"))
        if not wrote and _applied["result"] is not None and not _res.get("insert_outcome_unknown"):
            # Nothing landed (a no-op, a gate refusal, or a definite pre-insert
            # failure): this build's own Drive picks never became rows.
            _rollback_new_drive_drafts(drafts, log)
            if (_res.get("rollback_restored")
                    or not _res.get("deleted_total", _res.get("deleted"))):
                # ...and the OLD rows survive, so their released assets are stamped
                # again. (deleted_total>0 with no insert: the old rows are gone, so
                # their assets stay free, which is correct. The raw month-grained
                # count is used here, not the span-scoped verifier claim: a delete
                # that wiped only out-of-span rows still destroyed those rows.)
                _restore_released_drive_assets(base_key, released_drive, log)
        client_content.clear_drive_pool_cache()
        _heartbeat.stop()
        _build_lock.release(base_key, holder=_lock_holder)


def _build_client_month_body(account, base_key, start, days, *, voice, library_path,
                             store, banned_words, log, allow_reshape, media_count,
                             locked_feed_days, used_keys, locked_keys, drafts,
                             drive_deferred_days, rendition_budget, apply_state=None,
                             locked_feed_slots=None):
    """The picking + apply half of build_client_month, split out so the caller can
    wrap release -> apply in ONE try/finally (see build_client_month)."""
    from datetime import timedelta
    # Client-EDITED story captions per day: honor them on re-render so a saved story
    # caption is not discarded by the rebuild (Dale, 2026-08-17).
    edited_story_caps = _edited_story_captions(base_key, start, days, store, log)

    # POSTING CADENCE (CADENCE_SPEC.md): 1 or 2 feed+story pairs per day. Resolved
    # ONCE per build; ECHO_CADENCE_2X_ENABLED off -> always 1 (byte-for-byte today).
    from .cadence import resolve_posts_per_day
    slots_per_day = resolve_posts_per_day(base_key, store)
    locked_feed_slots = set(locked_feed_slots or ())
    # Days where every cadence slot is already human-owned: Drive/Lane A skip
    # them wholesale. A 2x day with only the morning post approved is NOT fully
    # locked and can still receive the evening slot.
    fully_locked_days = {d for d in locked_feed_days
                         if all((d, s) in locked_feed_slots
                                for s in range(slots_per_day))}

    # MEDIA-CAPPED: never build past the media the gym has. `days` is only an UPPER
    # bound on COVERED DAYS; the feed budget is days * slots_per_day (at 1x exactly
    # the pre-cadence cap), still bounded by the media not already locked to an
    # approved row (one distinct creative per feed, no reuse). A 2-photo gym gets
    # 2 feeds, never 30. At 2x each day consumes two photos, so a thin library
    # covers half the days — never padded, never reused.
    # Only used keys that are ACTUAL library files reduce the cap: the cross-day
    # media guard also collects non-library media (Drive assets, infographic cards)
    # whose keys rightly block a re-pick but consume none of this library's photos.
    try:
        from . import media_guard as _mg
        _lib_names = _mg.library_keys(library_path)
        _used_in_lib = len(used_keys & _lib_names) if _lib_names else len(used_keys)
    except Exception:  # noqa: BLE001 - fall back to the raw count, never block
        _used_in_lib = len(used_keys)
    # The ceiling covers every distinct creative this build can actually select.  A
    # Drive-only gym has no local files, but its eligible Drive assets are still real
    # media and must not collapse the entire build to a zero-feed budget.  pickable()
    # already applies the selector's reviewed, moderation, once-used and cooldown
    # gates, so this is a bounded snapshot of the usable Drive budget rather than a
    # broad asset-table count.
    from . import gym_media_selector as _media_selector
    _drive_budget = (len(_media_selector.pickable(base_key))
                     if (config.gym_drive_stage_enabled()
                         and config.gym_drive_connect_active_for(base_key)) else 0)
    max_feed_days = min(days * slots_per_day,
                        max(0, media_count - _used_in_lib) + _drive_budget)

    # `drafts` is the caller's list (build_client_month's try/finally reads it back to
    # roll unlanded Drive picks out of the pool); never rebound here.
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
    from .drafter import opening_signature, angle_for_index, opening_formula
    recent_openings = []            # accepted opening signatures, oldest..newest
    _OPENING_WINDOW = 6             # how many recent openings each new day must avoid
    # OPENING FORMULA CAP (ECHO_OPENING_FORMULA_CAP, Tough Temple): the coarse opening
    # FRAME of each accepted post, oldest..newest. openings_collide compares four words
    # and so never fired on fifteen straight "You ..." captions; this sees the frame.
    recent_formulas = []
    _FORMULA_WINDOW = 8             # enough history to measure any sane run cap
    # ANGLE ROTATION (Bryan/Pierce, 2026-08, AGENT_CAPTION_ANGLE_ROTATION): when armed,
    # each accepted feed also gets a DISTINCT SB7 problem/entry angle round-robin (varied by
    # a build-local index that only advances on ACCEPTED days, so the spread is dense) plus
    # the recent angles to avoid, threaded into the caption generator. It also WIDENS the
    # opening-avoid window to ~12 so consecutive days diverge harder. STYLE-only, never a
    # fact, never a block. Flag OFF => no angle guidance and the window stays 6 (unchanged).
    _angle_rotation = config.caption_angle_rotation_enabled()
    # DAY SHAPE ROLES: resolved ONCE per build (see the slot loop below).
    _day_shape_roles = config.day_shape_roles_enabled()
    _ANGLE_WINDOW = 3               # how many recent angles each new day must avoid
    _WIDE_OPENING_WINDOW = 12       # widened opening-avoid window when angle rotation is on
    recent_angles = []              # accepted angles, oldest..newest
    angle_idx = 0                   # advances only on an ACCEPTED feed (dense round-robin)
    # FORWARD-BOOK PILLAR BALANCE. Seed from approved/published posts that the rebuild
    # must preserve, then update once per accepted feed. The least-used approved pillar
    # is always eligible; heavier pillars are passed over while the bounded neighbour
    # walk asks the existing source builder for a real underweight alternative.
    pillar_counts = _surviving_pillar_counts(base_key, start, days, store, log)
    available_pillars = tuple(str(p or "").strip().lower()
                              for p in client_content._pillars_for(  # noqa: SLF001
                                  getattr(account, "key", "") or base_key)
                              if str(p or "").strip())
    built_feeds = 0
    # Days the uploaded-media path placed a feed on. The gym-drive lane (below) fills
    # only the GAPS, so a Drive post never doubles up a day that already has a photo.
    covered_days = set(locked_feed_days)
    # The Drive-photo pre-pass, Lane A and video pre-pass claim exact slots. Drive
    # photos lead, local photos use the remaining slots, and video is considered only
    # after Lane A has exhausted its eligible local-photo supply.
    covered_slots = set(locked_feed_slots)
    pre_captions = {}
    # SOURCE ORDER: approved Drive photos, then local real photos, then Drive videos.
    # Pin this first pass to photos so a mixed pool cannot spill into video when its
    # approved photos run out. Lane A and the later video pass share slot ownership.
    _drive_armed = (config.gym_drive_stage_enabled()
                    and config.gym_drive_connect_active_for(base_key))

    def _record_drive_prepass(pre, label):
        if not pre:
            return 0
        drafts.extend(pre)
        feed_count = 0
        for d in pre:
            if getattr(d, "is_story", False):
                continue
            feed_count += 1
            day = str(getattr(d, "day_key", "") or "")[:10]
            pre_captions.setdefault(day, []).append(
                (getattr(d, "caption", "") or "").strip())
            category = str(getattr(d, "category", "") or "").strip().lower()
            if category:
                pillar_counts[category] += 1
        # A day is covered only when every cadence slot has an owner. This still lets
        # Lane A and the later video pass fill a partial 2x day.
        for dk, _slot in covered_slots:
            if all((dk, s) in covered_slots for s in range(slots_per_day)):
                covered_days.add(dk)
        log(f"{base_key}: {label} pre-pass claimed {feed_count} feed(s) from the "
            "connected Drive pool")
        return feed_count

    _drive_kinds = _media_selector.pool_kinds(base_key) if _drive_armed else set()
    if "photo" in _drive_kinds:
        try:
            photo_pre = append_gym_drive_drafts(
                account, base_key, start, days, voice, log=log,
                covered_days=fully_locked_days, library_path=library_path,
                slots_per_day=slots_per_day, banned_words=banned_words,
                rendition_budget=rendition_budget, covered_slots=covered_slots,
                kind_prefs=(_PHOTO_KIND,), max_feed_count=max_feed_days)
            drive_photo_feeds = _record_drive_prepass(photo_pre, "Drive photo")
        except InvalidLogicalPostIdentity:
            raise  # FATAL: an unverifiable identity aborts the rebuild pre-delete
        except Exception as e:  # noqa: BLE001 - the pre-pass never sinks the month
            drive_photo_feeds = 0
            log(f"{base_key}: Drive photo pre-pass skipped ({type(e).__name__}: {e})")
    else:
        drive_photo_feeds = 0
    # Captions already accepted anywhere in this build.  The stage-time caption
    # cooldown is the final safety net, but it must not be the first place a duplicate
    # is discovered: dropping a feed there used to leave its paired Story behind and
    # silently thin a 2x month.  Seed this with Drive-photo pre-pass feeds, then make
    # Lane A walk its existing approved-source alternatives before companions exist.
    month_captions = [caption for captions in pre_captions.values()
                      for caption in captions if caption]
    # Walk day keys as an UPPER bound (days), but STOP emitting feeds once we have
    # placed one per unique photo (max_feed_days). Stories reuse the feed's photo (a
    # feed + its paired story are the same asset), so stories do not consume the cap.
    i = 0
    while i < days and built_feeds + drive_photo_feeds < max_feed_days:
        day_key = (start + timedelta(days=i)).isoformat()
        i += 1

        # captions placed on THIS day (2x uniqueness, D5), seeded with Drive-photo
        # placements so a Lane A slot never repeats a Drive concept
        day_captions = list(pre_captions.get(day_key, []))
        day_built = 0
        for slot_i in range(slots_per_day):
            if built_feeds + drive_photo_feeds >= max_feed_days:
                break
            # A slot a human already owns keeps its approved content (D7). A 2x
            # day with only the morning post approved can still fill the evening
            # slot; the rebuild never replaces the owned slot's feed/story/FB.
            if (day_key, slot_i) in locked_feed_slots:
                log(f"locked {day_key} slot {slot_i}: already has "
                    "approved/published content")
                continue
            if (day_key, slot_i) in covered_slots:
                continue                   # the Drive-photo pass owns this slot
            # Choose this slot's angle (round-robin by the accepted-feed index) + the
            # recent angles to avoid, and widen the opening window, only when angle
            # rotation is armed.
            if _angle_rotation:
                day_angle = angle_for_index(angle_idx)
                day_avoid_angles = recent_angles[-_ANGLE_WINDOW:]
                opening_window = _WIDE_OPENING_WINDOW
            else:
                day_angle, day_avoid_angles, opening_window = "", (), _OPENING_WINDOW
            # DAY SHAPE ROLES (ECHO_DAY_SHAPE_ROLES, default OFF). On a 2x day the two
            # slots have two different JOBS, not one job at two times: slot 0 in the
            # morning is PROOF (a real member moment, a soft ask) and slot 1 in the
            # evening is the INVITATION (the named next step, a hard ask). Each role
            # leads from its OWN pool of SB7 entry angles, so the second slot is asked
            # for a genuinely different post instead of leaning on the repeat guard to
            # drop a near copy. Without this the guard keeps a 2x gym safe but thin: it
            # reliably receives ONE post a day on a two post cadence (Dale's B8).
            # Flag OFF, or a 1x gym, leaves the angle choice above byte for byte.
            if _day_shape_roles and slots_per_day == 2:
                day_angle = day_shape.angle_for_slot(slot_i, rotation=angle_idx)
                day_avoid_angles = tuple(day_shape.angles_for_role(
                    day_shape.role_for_slot(1 - slot_i)))
                opening_window = _WIDE_OPENING_WINDOW
            # Select from an underweight approved pillar before queuing the row. This
            # keeps 2x builds from choosing one day-level pillar for both slots and
            # prevents surviving approved rows from being amplified by a rebuild.
            _mix_floor = (min(pillar_counts.get(p, 0) for p in available_pillars)
                          if available_pillars else 0)
            _heavy_pillars = {p for p in available_pillars
                              if pillar_counts.get(p, 0) > _mix_floor}
            feed, feed_drop = _clean_draft_for_day(
                account, day_key, voice, library_path, banned_words, log,
                exclude_keys=used_keys,
                avoid_openings=recent_openings[-opening_window:],
                angle=day_angle, avoid_angles=day_avoid_angles,
                avoid_captions=tuple(day_captions + (
                    month_captions if config.caption_cooldown_enabled() else [])),
                recent_formulas=tuple(recent_formulas[-_FORMULA_WINDOW:]),
                avoid_categories=_heavy_pillars,
                prefer_photos=True)
            if feed is None:
                if feed_drop:
                    skipped_banned += 1
                    log(f"drop {day_key} feed slot {slot_i + 1}: {feed_drop}")
                elif slot_i == 0:
                    if client_content.drive_pool_can_fill(
                            getattr(account, "key", "") or base_key):
                        # pick_image returned no pick on purpose: every local
                        # creative is inside its repeat window and the Drive pool
                        # can fill the day (append_gym_drive_drafts below).
                        drive_deferred_days.add(day_key)
                        log(f"skip {day_key} feed: local library exhausted within its "
                            "repeat window; leaving the day for the connected Drive pool")
                    else:
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
            # A Drive-photo pre-pass can fail transiently before it claims an asset.
            # `prefer_photos` only filters the LOCAL library, so without this final
            # eligibility check Lane A could stage a local video while an approved,
            # unused Drive photo remained pickable. Keep that slot open for the final
            # Drive retry (or hold it if the retry also fails); a clip never outranks
            # a still merely because the first Drive attempt blinked.
            if (is_video_url(feed_path or getattr(feed, "creative_public_url", ""))
                    and _drive_armed
                    and "photo" in _media_selector.pool_kinds(base_key)):
                log(f"held: local video for {day_key} feed while an approved Drive "
                    "photo remains pickable")
                continue
            if feed_path and feed_path in used_paths:
                log(f"skip {day_key} feed: photo already used by an earlier feed "
                    "(no reuse)")
                continue
            # STALE REUSE + a connected Drive pool (Pete/Zanshin, Dean/Reverb,
            # 2026-09-07): this pick is a repeat from an exhausted small library
            # (client_content.pick_image flagged it). When the gym has NO Drive
            # connection, there is nothing better to try, so the repeat still
            # places below (same as before this fix -- see
            # test_polluted_ledger_still_places_distinct_photos, a gym with no
            # Drive pool). When a Drive pool IS connected, skip placing the stale
            # repeat here and leave the day uncovered: append_gym_drive_drafts
            # below only fills days the uploaded-media loop left uncovered, so
            # this is what actually gives the Drive lane's fresh, unused photos a
            # chance instead of a small stale library silently claiming the day
            # forever.
            # BOTH Drive flags, same pair the actual fallback below is gated on
            # (line ~983) -- GYM_DRIVE_STAGE off would otherwise turn a stale
            # repeat into a genuinely EMPTY day (found in independent review,
            # 2026-09-07): connected-but-not-staged means append_gym_drive_drafts
            # never runs, so skipping here without checking staging too would
            # leave the gap unfilled by anything at all -- worse than the repeat
            # this fix exists to replace.
            # ... AND the pool can actually fill it (2026-09-10): with GYM_DRIVE_CONNECT
            # globally ON, the two flags alone said yes for EVERY gym, including gyms
            # with no Drive source or a pool fully on cooldown, and their stale repeat
            # became an empty day. client_content.drive_pool_can_fill checks both
            # flags AND a pickable asset; it is the same gate pick_image itself now
            # applies, so this block is the belt for a pick that predates the gate.
            if (getattr(feed, "stale_reuse", False)
                    and client_content.drive_pool_can_fill(
                        getattr(account, "key", "") or base_key)):
                drive_deferred_days.add(day_key)
                log(f"skip {day_key} feed: stale repeat from an exhausted library, "
                    "leaving the day for the connected Drive pool")
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
            # caption belongs to the exact logical slot it was edited on. A
            # slotless legacy edit can only map to the first cadence slot.
            story_caption_override = edited_story_caps.get(
                (str(day_key)[:10], slot_i))
            if story_caption_override is None and slot_i == 0:
                story_caption_override = edited_story_caps.get(
                    (str(day_key)[:10], None))
            # ACCEPTED: the feed survived every gate and is being placed. Record its
            # photo as served NOW (not at pick time) so rotation reflects only KEPT
            # days — never the picked-then-dropped attempts.
            if not _record_feed_served(account, feed, day_key):
                log(f"{base_key} {day_key}: served ledger write failed; local feed held")
                continue
            try:
                day_drafts = _finish_feed_with_story(
                    account, feed, library_path, log, day_key=day_key,
                    story_caption_override=story_caption_override)
            except Exception:
                _release_feed_reservation(feed)
                raise
            if not day_drafts:
                _release_feed_reservation(feed)
                log(f"{base_key} {day_key}: held; visual provenance could not stage the feed")
                continue
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
            _accepted_pillar = str(getattr(feed, "category", "") or "").strip().lower()
            if _accepted_pillar:
                pillar_counts[_accepted_pillar] += 1
            built_feeds += 1
            day_built += 1
            covered_days.add(day_key)   # the gym-drive lane skips days already filled
            covered_slots.add((day_key, slot_i))
            day_captions.append(getattr(feed, "caption", "") or "")
            month_captions.append(getattr(feed, "caption", "") or "")
            # The video pass runs after Lane A. Keep its same-day uniqueness map
            # current so it cannot stage a Drive caption that Lane A already used.
            pre_captions.setdefault(day_key, []).append(
                getattr(feed, "caption", "") or "")
            # Record this accepted feed's opening so the NEXT slot/day avoids leading
            # the same way (the cross-day repetition Ryan flagged).
            sig = opening_signature(getattr(feed, "caption", "") or "")
            if sig:
                recent_openings.append(sig)
            # Record this accepted feed's opening FRAME so the run cap can see a
            # fifteen day streak of one formula that no four word compare would catch.
            frame = opening_formula(getattr(feed, "caption", "") or "")
            if frame:
                recent_formulas.append(frame)
            # Record this accepted feed's angle + advance the round-robin so the NEXT
            # accepted slot gets a DISTINCT angle (angle rotation ON only).
            if _angle_rotation:
                recent_angles.append(day_angle)
                angle_idx += 1
        if day_built:
            built_days += 1

    # VIDEO PRE-PASS: only after the Drive-photo pass and Lane A have consumed every
    # photo slot they can. Restrict this pass to video beats and a video-only currently
    # pickable pool, so a clip cannot outrank any approved Drive or local photo.
    _drive_kinds = _media_selector.pool_kinds(base_key) if _drive_armed else set()
    if "video" in _drive_kinds and "photo" not in _drive_kinds:
        try:
            video_pre = append_gym_drive_drafts(
                account, base_key, start, days, voice, log=log,
                # Slot ownership, rather than day coverage, allows a video to fill a
                # genuinely open PM slot after a local or Drive-photo AM post.
                covered_days=fully_locked_days, library_path=library_path,
                slots_per_day=slots_per_day, banned_words=banned_words,
                rendition_budget=rendition_budget, covered_slots=covered_slots,
                video_beats_only=True, kind_prefs=(_VIDEO_KIND,),
                day_captions_seed=pre_captions,
                max_feed_count=max(0, max_feed_days - drive_photo_feeds - built_feeds))
            _record_drive_prepass(video_pre, "video")
        except InvalidLogicalPostIdentity:
            raise  # FATAL: an unverifiable identity aborts the rebuild pre-delete
        except Exception as e:  # noqa: BLE001 - the pre-pass never sinks the month
            log(f"{base_key}: video pre-pass skipped ({type(e).__name__}: {e})")

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

    # Days the UPLOADED library covered (Lane A only, locked days excluded): the
    # small-library digest compares the library against these + the fallback fills,
    # never against Drive-covered days (audit round 4 #4).
    lane_a_days = len(set(covered_days) - set(locked_feed_days))

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
                # Exact slot ownership decides whether a 2x slot is open.  Passing
                # a day with one Lane-A feed here used to skip its open second slot.
                covered_days=fully_locked_days, library_path=library_path,
                slots_per_day=slots_per_day, banned_words=banned_words,
                rendition_budget=rendition_budget, covered_slots=covered_slots,
                day_captions_seed=pre_captions,
                max_feed_count=max(0, max_feed_days - sum(
                    1 for d in drafts if not getattr(d, "is_story", False))))
            if drive_extra:
                drafts.extend(drive_extra)
                # append_gym_drive_drafts tracks coverage on its own copy; the
                # no-empty-day fallback below must see the days Drive actually filled.
                covered_days.update(str(getattr(d, "day_key", "") or "")[:10]
                                    for d in drive_extra if getattr(d, "day_key", ""))
                log(f"{base_key}: +{len(drive_extra)} post(s) from the connected "
                    "Drive pool (PENDING, gap-fill)")
        except InvalidLogicalPostIdentity:
            raise  # FATAL: an unverifiable identity aborts the rebuild pre-delete
        except Exception as e:  # noqa: BLE001 - the lane never sinks the month
            log(f"{base_key}: gym-drive lane skipped ({type(e).__name__}: {e})")

    # NO EMPTY DAYS (audit 2c): a day Lane A deferred to the Drive pool that the Drive
    # lane then could not cover (a partial pool: 3 pickable assets on a 30-day span)
    # gets the stale repeat it would have had before this gate existed, spaced across
    # the book. A repeat is worse than fresh footage; an empty day is worse than both.
    if drive_deferred_days:
        try:
            # THE MEDIA CAP HOLDS (audit R-A1): Lane A feeds + Drive feeds + these
            # repeats together never exceed max_feed_days (N photos -> at most N feeds).
            _drive_feeds = sum(
                1 for d in drafts
                if getattr(d, "source_media_asset_id", "") and not getattr(d, "is_story", False))
            filled = _fill_uncovered_days(
                account, base_key, voice, library_path, banned_words, log,
                deferred_days=drive_deferred_days, covered_days=covered_days,
                locked_keys=locked_keys, used_keys=used_keys, drafts=drafts,
                store=store, start=start, days=days,
                edited_story_caps=edited_story_caps,
                max_fill=max_feed_days - built_feeds - _drive_feeds,
                local_days=lane_a_days)
            if filled:
                built_feeds += filled
                built_days += filled
                log(f"{base_key}: +{filled} spaced repeat day(s) the Drive pool "
                    "could not cover (never an empty day)")
        except InvalidLogicalPostIdentity:
            raise  # FATAL: an unverifiable identity aborts the rebuild pre-delete
        except Exception as e:  # noqa: BLE001 - the fallback never sinks the month
            log(f"{base_key}: no-empty-day fallback skipped ({type(e).__name__}: {e})")

    # GYM ASK COVERAGE (ECHO_GYM_ASK_COVERAGE, default OFF). ask_coverage has run on
    # the LASSO B2B month since 2026-08-28, but its ONLY call site guards on the B2B
    # profile (real_month_planner.apply_month_plan), so no client gym has ever had a
    # single ask enforced. Measured on production 2026-09-05 with the real grader:
    # Tough Temple path_to_join 0/10, 'no ask in caption' on every eligible post, and
    # crossfitnine7f7dadc also 0/10. That is half of Blake's D grade, and it is a
    # wiring gap, not a copy problem.
    #
    # The gym's OWN approved CTA is used, never LASSO's "Book a call today." and never
    # an invented one: if the voice doc carries no CTA that reads as exactly one ask
    # family, the lane is SKIPPED and the month is unchanged.
    if config.gym_ask_coverage_enabled():
        _gym_ask = _approved_gym_ask(voice)
        if _gym_ask:
            try:
                from . import ask_coverage as _ask
                _summary = _ask.enforce_drafts(drafts, default_ask=_gym_ask)
                log(f"{base_key}: ask coverage now "
                    f"{_summary['coverage']:.0%} of feeds "
                    f"({_summary['floor_added']} raised to the floor, "
                    f"{_summary['reels_fixed']} reel(s) given exactly one ask)")
            except Exception as e:  # noqa: BLE001 - never sinks the month
                log(f"{base_key}: ask coverage lane skipped "
                    f"({type(e).__name__}: {e})")
        else:
            log(f"{base_key}: ask coverage lane skipped, the voice doc carries no "
                "approved CTA that reads as a single ask (never an invented one)")

    rows = _to_rows(base_key, drafts)
    # Blake 2026-10-06 ruling: no NEW row is ever withheld in 'coach_review' — a first
    # month now lands as 'pending' like every other draft so the client approves it
    # directly. Historical coach_review rows remain readable/editable where existing
    # guards reference the status; this build path simply no longer creates them.
    for r in rows:
        r["status"] = "pending"
    # GOOGLE BUSINESS MIRROR (AGENT_GBP_MIRROR, default OFF; Blake 2026-09-02: "anytime
    # you post to ig, fb or whatever goes to google as well"). The same build-time
    # cross-post the Facebook leg does, with the two things a Google post cannot share
    # with an Instagram post done properly: a 1200x900 crop hosted BEFORE approval, and a
    # Google-native caption that must clear the A+ gate or the row is skipped. Appended
    # after the pending-stamp on purpose: a GBP row is never 'coach_review' (Blake ruled it
    # out for Google), it is always the owner's own 'pending' tap. See agent/gbp_mirror.py.
    from . import gbp_mirror as _gbp_mirror
    # NOTE: no store= is passed. `store` here is the calendar store; the mirror needs a
    # GbpStore (connection posture + the CTA link live only there) and builds its own.
    # Drive-provenance side channel: byte-bound source->crop render evidence for
    # mirrored GBP rows, forwarded to the prepared writer keyed by the cropped
    # image_url (evidence is NOT a content_calendar column). Empty when the
    # global writer is disarmed.
    _gbp_render_evidence = {}
    _gbp_rows = _gbp_mirror.rows_for(base_key, drafts, library_path=library_path,
                                     logger=log,
                                     render_evidence_sink=_gbp_render_evidence)
    if _gbp_rows:
        rows.extend(_gbp_rows)
    try:
        result = _apply(base_key, rows, start, days, store, log,
                        locked_days=locked_feed_days, allow_reshape=allow_reshape,
                        poster_render_evidence_by_url=_poster_render_evidence_by_url(
                            drafts),
                        render_evidence_by_url=_gbp_render_evidence or None)
    except Exception:
        # _apply catches remote write failures and returns their unknown-outcome
        # flag. An exception escaping its contract is a prewrite planning failure.
        _release_unlanded_reservations(drafts)
        raise
    if (result.get("noop_empty") and _empty_fill_has_unmet_slots(
            store, base_key, start, days, slots_per_day, locked_feed_days)):
        # No required photo post was assembled. Preserving the calendar is safe,
        # but an empty generation is not a successful fill (Swift Oct 15).
        result.update(ok=False, reason="no admissible photo posts",
                      awaiting_content=True)
    if apply_state is not None:
        # Reporting below can still raise. Preserve the write outcome before that
        # happens so the outer cleanup never mistakes a landed insert for prewrite.
        apply_state["result"] = result
    _release_unlanded_reservations(
        drafts, retained=result.get("retained_reservation_ids", ()))
    # NOTHING WRITTEN (never-wipe-to-empty, never-shrink, a gate refusal, a store
    # failure): build_client_month's try/finally reads `inserted` / `deleted` off this
    # result and restores the released stamps / rolls back this build's unlanded picks
    # (audit D3 + residual). Nothing to do here.
    # Report the plan we actually assembled, rather than the Lane-A counters.  Drive
    # pre-passes stage feeds before Lane A, and a 2x day can hold two feed slots, so
    # `built_feeds` / `built_days` alone undercount a valid Drive-first plan.
    _planned_feeds = [d for d in drafts if not getattr(d, "is_story", False)]
    result["gbp_mirrored"] = len(_gbp_rows)
    result["days"] = len({str(getattr(d, "day_key", "") or "")[:10]
                          for d in _planned_feeds
                          if str(getattr(d, "day_key", "") or "")[:10]})
    result["feeds"] = len(_planned_feeds)
    result["posts_per_day"] = slots_per_day
    result["skipped_banned"] = skipped_banned
    result["media_count"] = media_count
    # ONE needs-media digest for the whole month build, never per day (the eng
    # 18-alert storm, 2026-08-28). No-op when nothing was buffered this run.
    client_content.flush_needs_media_alerts(account.key)
    return result


from .media_types import VIDEO_EXTS as _VIDEO_EXTS, is_video_url   # ONE definition (D1)


def _visual_writer_guard_enabled():
    """Whether calendar writes require owner-attested visual provenance."""
    try:
        from . import visual_writer_prepare
        return bool(visual_writer_prepare.enabled())
    except Exception:  # noqa: BLE001 - an armed guard must never silently open
        return os.environ.get("AGENT_VISUAL_GLOBAL_WRITER_PREP", "").lower() in (
            "1", "true", "yes", "on")


def _capture_raw_hosted_source(draft, log, day_key):
    """Attach the pre-transform hosted URL without guessing a source identity.

    The visual writer verifies same-object bytes at its privileged boundary.  This
    producer preserves an explicit source when one already exists, and otherwise
    carries the exact URL it received before its own rendition lanes can swap
    ``creative_public_url``.  A Drive asset without explicit source provenance may
    already be a HEIC/HEVC rendition, so it is held instead of being relabeled raw.
    """
    if not _visual_writer_guard_enabled():
        return ""
    existing = (getattr(draft, "source_media_url", "") or "").strip()
    if existing:
        return existing
    if (getattr(draft, "source_media_asset_id", "") or "").strip():
        log(f"held {day_key} feed: Drive rendition has no explicit raw source URL")
        return ""
    raw = (getattr(draft, "creative_public_url", "") or "").strip()
    if not raw:
        return ""
    try:
        draft.source_media_url = raw
    except Exception:  # noqa: BLE001 - never silently lose provenance on an immutable draft
        log(f"held {day_key} feed: visual provenance could not retain raw source")
        return ""
    return raw


def _maybe_edit_video(account, feed, library_path, log):
    """Swap a VIDEO feed draft's creative for its action-cut reel (edited + HOSTED).
    No-op unless AGENT_CLIENT_VIDEO_EDIT is armed, the creative is a video, and both
    the edit and the hosting succeed — otherwise the raw video posts as-is (editing
    may never block a post). Mutates feed.creative_public_url in place; the paired
    story is cloned FROM the feed afterwards, so it inherits the same reel."""
    if not config.client_video_edit_enabled():
        return
    if _visual_writer_guard_enabled():
        # This producer has no owner receipt for the rendered reel.  Keep the exact
        # raw hosted video; writing a distinct rendition without that evidence is
        # forbidden by the calendar writer.
        log("reel edit held: visual provenance lacks owner-attested render evidence")
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
    card. Returns True when the draft may ship (poster attached, already present, or
    no poster needed). Returns False ONLY under the armed AGENT_VISUAL_GLOBAL_WRITER_PREP
    guard, when a distinct thumbnail could not be produced WITH byte-bound render
    evidence: the caller then HOLDS the slot instead of staging an unproven rendition.
    Flag off: legacy best effort, never a blocked post."""
    path = (getattr(draft, "creative_path", "") or "").strip()
    if not path or not path.lower().endswith(_VIDEO_EXTS):
        return True
    # A Drive-lane video already carries the poster its builder made while the
    # download was on disk; its creative_path is the asset TITLE, not a file. Under
    # the guard that builder also stashes draft.poster_render_evidence; if a distinct
    # thumbnail reaches the guarded writer without proof the WRITER fails closed and
    # holds the row -- this lane never clears or relabels it.
    if getattr(draft, "thumbnail_url", "") or not os.path.isfile(path):
        return True
    if _visual_writer_guard_enabled():
        return _attach_video_poster_guarded(account, draft, path, library_path, log)
    try:
        from . import action_reel, media_host
        poster = action_reel.get_or_make_poster(path, library_path, logger=log)
        if poster and config.hosting_enabled():
            hosted = media_host.host_media(poster, account.key)
            if hosted:
                draft.thumbnail_url = hosted
    except Exception as exc:  # noqa: BLE001 - a preview must never block a post
        log(f"poster lane failed for {os.path.basename(path)}: {type(exc).__name__}")
    return True


def _attach_video_poster_guarded(account, draft, path, library_path, log):
    """Guarded poster lane: the preview frame must be an EVIDENCED render of the EXACT
    hosted video object (gym_media_builder.video_poster_with_evidence reads the hosted
    bytes, renders, re-reads the hosted JPEG, and returns a byte-bound receipt). On any
    failure the slot is HELD (False) -- an unattested distinct thumbnail is never
    staged. The evidence rides the draft as the dynamic attribute
    poster_render_evidence (NOT a content_calendar column); the insert side channel
    forwards it to the calendar writer keyed by (image_url, thumbnail_url)."""
    source_url = (getattr(draft, "creative_public_url", "") or "").strip()
    if not source_url:
        log(f"poster held for {os.path.basename(path)}: video draft carries no exact "
            "hosted url to evidence a poster render against")
        return False
    try:
        import tempfile
        from . import gym_media_builder
        work_dir = library_path or tempfile.mkdtemp(prefix="echo-poster-")
        result = gym_media_builder.video_poster_with_evidence(
            path, work_dir, account.key, source_url)
    except Exception as exc:  # noqa: BLE001 - an unverifiable preview must hold, not crash
        log(f"poster held for {os.path.basename(path)}: evidenced render lane failed "
            f"({type(exc).__name__})")
        return False
    if not result:
        log(f"poster held for {os.path.basename(path)}: no byte-evidenced poster render "
            f"for {source_url}")
        return False
    poster_url, evidence = result
    draft.thumbnail_url = poster_url
    try:
        draft.poster_render_evidence = evidence
    except Exception:  # noqa: BLE001 - a frozen/edge draft: evidence lost is a hold
        log(f"poster held for {os.path.basename(path)}: evidence could not be retained "
            "on the draft")
        try:
            draft.thumbnail_url = ""
        except Exception:  # noqa: BLE001
            pass
        return False
    log(f"evidenced poster attached for {os.path.basename(path)}")
    return True


def _poster_render_evidence_by_url(drafts):
    """Composite-key side channel for insert_rows: (image_url, thumbnail_url) ->
    poster render evidence, built from the ACTUAL draft objects (the row mapping never
    carries evidence as a content_calendar payload field). Strict composite key, never
    a thumbnail-only fallback: two videos can share one poster URL while each needs its
    own image -> poster rendition proof."""
    out = {}
    for draft in drafts or ():
        evidence = getattr(draft, "poster_render_evidence", None)
        if not evidence:
            continue
        image = (getattr(draft, "creative_public_url", "") or "").strip()
        thumb = (getattr(draft, "thumbnail_url", "") or "").strip()
        if image and thumb:
            out[(image, thumb)] = evidence
    return out


def _insert_rows_with_poster_evidence(insert_rows, base_key, rows, evidence_by_url,
                                      render_evidence_by_url=None,
                                      required_feed_slots=None,
                                      prevalidated_cadence=False):
    """Forward poster proof through the prepared writer boundary.

    A TypeError from a prepared call is ambiguous: the store may have written before
    failing internally.  Never retry that write without the proof.  Flag-off retains
    the plain legacy call for older stores and test fakes.
    """
    kwargs = {}
    if required_feed_slots is not None:
        import inspect
        try:
            parameters = inspect.signature(insert_rows).parameters
            if "required_feed_slots" in parameters:
                kwargs["required_feed_slots"] = required_feed_slots
            if "prevalidated_cadence" in parameters:
                kwargs["prevalidated_cadence"] = prevalidated_cadence
        except (TypeError, ValueError):
            pass
    if _visual_writer_guard_enabled():
        if evidence_by_url:
            kwargs["poster_render_evidence_by_url"] = evidence_by_url
        if render_evidence_by_url:
            kwargs["render_evidence_by_url"] = render_evidence_by_url
    if kwargs:
        return insert_rows(base_key, rows, **kwargs)
    return insert_rows(base_key, rows)


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


def _localize_creative(story, feed, path, log):
    """Download the STORY's hosted media to a temp file and return that path, or None.

    For lanes whose draft carries a remote asset title instead of a local file (the
    gym-drive lane), this is what lets the story caption actually be burned. Best
    effort and never raises: no url, a failed fetch, or empty bytes all return None,
    and the caller then falls through to the existing captionless guard.

    Reads story.creative_public_url FIRST, never the feed's: _finish_feed_with_story
    snapshots the pre-autofit media onto the story precisely so a story is not built
    from the 4:5 1080x1350 feed card. Localizing from the feed would re-introduce
    that bug for every Drive story whenever AGENT_FEED_AUTOFIT is armed (it is)."""
    url = ((getattr(story, "creative_public_url", "") or "").strip()
           or (getattr(feed, "creative_public_url", "") or "").strip())
    if not url:
        return None
    try:
        from . import media_host
        data = media_host.download_bytes(url)
        if not data:
            return None
        import tempfile
        ext = os.path.splitext(path)[1] or os.path.splitext(url.split("?")[0])[1] or ".jpg"
        fd, tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return tmp
    except Exception as exc:  # noqa: BLE001 - never block a build on a fetch
        log(f"could not localize hosted media for the story burn "
            f"({type(exc).__name__}); the story will be dropped, not shipped bare")
        return None


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
    if _visual_writer_guard_enabled():
        # A caption burn is a distinct rendition, and this planner cannot attest to
        # one.  Keep the story only when it still points at the exact raw object the
        # guarded writer will verify later.  Do not silently lose the paired Story
        # merely because a transformed rendition is unavailable, and never relabel a
        # different object as its source.
        raw_source = (getattr(story, "source_media_url", "") or "").strip()
        story_media = (getattr(story, "creative_public_url", "") or "").strip()
        if raw_source and raw_source == story_media:
            log("story format deferred: keeping raw same-object story pending "
                "visual-writer verification")
            return True
        log("story format held: visual provenance lacks a same-object raw source")
        return False
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
    # REMOTE-MEDIA STORIES (gym-drive lane): a Drive-sourced feed carries the asset
    # TITLE in creative_path, not a readable local file (its temp download is cleaned
    # up in the builder), so the burn below would fail and the captionless guard would
    # silently drop every Drive story. Materialize the hosted media to a temp file and
    # burn from that — the same download_bytes lane the publish-time aspect preflight
    # uses, because pub-*.r2.dev 403s a plain GET.
    _tmp_story_src = None
    if path and not os.path.isfile(path):
        _tmp_story_src = _localize_creative(story, feed, path, log)
        if _tmp_story_src:
            path = _tmp_story_src
    # The STORY's own caption wins when it was overridden by a client edit; otherwise it
    # equals the feed caption (the paired story is cloned from the feed). This is what
    # lets a saved story caption actually get BURNED onto the media on re-render.
    caption = (getattr(story, "caption", "") or getattr(feed, "caption", "") or "")
    # The calendar write formats copy later, but these words become pixels NOW.
    # Use the same caption for the burn and for the Story row so an edited Story
    # keeps priority and the persisted text describes the rendered media.
    from .copy_gate import format_caption
    try:
        caption = format_caption(caption)
    except ValueError:
        log("story format held: caption cannot meet the copy rule")
        return False
    story.caption = caption
    # Task #28 (§5c): keep the RAW (un-captioned) source url so an edited story caption can
    # re-burn IMMEDIATELY instead of only on the next monthly rebuild. Gated: written to
    # content_calendar.source_media_url only when AGENT_STORY_SOURCE_MEDIA is on (the column
    # exists). Read the STORY's url, not the feed's: _finish_feed_with_story has already
    # restored the PRE-AUTOFIT media onto the story, whereas feed.creative_public_url may
    # by now be the 4:5 1080x1350 autofit card — storing that made every edited-caption
    # re-burn come back cropped to the feed shape (both flags are armed in production).
    if config.story_source_media_enabled():
        story.source_media_url = ((getattr(story, "creative_public_url", "") or "")
                                  or (getattr(feed, "creative_public_url", "") or ""))
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
    finally:
        if _tmp_story_src:
            try:
                os.unlink(_tmp_story_src)
            except OSError:
                pass


def _maybe_format_feed(account, feed, library_path, log):
    """AGENT_FEED_AUTOFIT: re-frame an OUT-OF-SPEC feed PHOTO into an in-spec 1080x1350 card
    so the platform never hard-crops the subject. ENHANCE-only: an in-spec photo, a video,
    hosting-off, or any failure keeps the raw media (this never DROPS a post, unlike the story
    caption guard). Mutates feed.creative_public_url in place on success."""
    if not config.feed_autofit_enabled():
        return
    if _visual_writer_guard_enabled():
        # Do not swap to a reframe unless this producer can supply the distinct
        # source/rendition evidence the guarded calendar writer requires.
        log("feed autofit held: visual provenance lacks owner-attested render evidence")
        return
    path = (getattr(feed, "creative_path", "") or "").strip()
    hosted_src = (getattr(feed, "creative_public_url", "") or "").strip()
    if not path and not hosted_src:
        return
    # A VIDEO is never reframed (audit D2): short-circuit BEFORE localizing, or every
    # Drive video is downloaded and kept in media_src only for feed_image to reject it
    # by extension.
    if is_video_url(path) or is_video_url(hosted_src):
        return
    try:
        from . import feed_image, media_host, media_localize
        # DRIVE LANE (2026-09-02, The Bolton Club): a Drive-sourced creative carries a
        # creative_path in the record but NOTHING on disk at this point, so every autofit
        # died on FileNotFoundError and logged "posting the raw photo". All 36 of Bolton's
        # Drive photos did exactly that, which meant the reframe silently never ran for ANY
        # Drive-lane gym and every out-of-spec photo fell through to the publish-time belt
        # instead of the cached build path. Localize from the creative's OWN hosted url
        # (deterministic name, kept for the next build) before autofitting.
        src = media_localize.local_source_for(path, hosted_src, logger=log)
        if not src:
            return                                    # nothing local to reframe: keep raw
        asset = feed_image.get_or_make_feed_image(src, library_path, logger=log)
        if not asset:
            return                                    # in-spec / video / render skipped
        if not config.hosting_enabled():
            return                                    # cannot host the reframe -> keep raw
        hosted = media_host.host_media(asset, account.key)
        if hosted:
            feed.creative_public_url = hosted
            if getattr(feed, "thumbnail_url", ""):
                feed.thumbnail_url = ""               # the reframe IS the media
            log(f"feed autofit applied for {os.path.basename(src)} "
                "(odd ratio -> 1080x1350)")
    except Exception as exc:  # noqa: BLE001 - never crash the build; keep the raw photo
        # name whichever source we actually had: a Drive creative has no local path.
        label = os.path.basename(path) or os.path.basename(hosted_src.split("?")[0]) \
            or "(no local path)"
        log(f"feed autofit lane failed for {label}: {type(exc).__name__}")


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
    # carry the dynamic poster attrs (not dataclass fields, so replace() drops them)
    # so a video story card shows the same frame preview as its feed AND the paired
    # story row forwards the same composite-key poster proof at insert.
    thumb = getattr(feed, "thumbnail_url", "") or ""
    if thumb:
        story.thumbnail_url = thumb
    poster_evidence = getattr(feed, "poster_render_evidence", None)
    if poster_evidence:
        try:
            story.poster_render_evidence = poster_evidence
        except Exception:  # noqa: BLE001 - a frozen/edge draft never blocks the build
            pass
    return story



def _logical_post_ids_valid(rows, log):
    """Pre-write guard (ECHO_LOGICAL_POST_ID_ENABLED ON only): every pre-stamped
    logical_post_id must be a valid UUID BEFORE the first delete or insert. A
    malformed identity would otherwise reach store validation only after _apply
    has already deleted a month — fail closed here with zero deletes/inserts.
    Flag OFF: no new failure mode, always valid."""
    if not config.logical_post_id_enabled():
        return True
    for row in rows or []:
        value = str((row or {}).get("logical_post_id") or "").strip()
        if not value:
            continue
        try:
            uuid.UUID(value)
        except (AttributeError, TypeError, ValueError):
            log(f"invalid pre-stamped logical_post_id {value!r}; aborting "
                "before any delete or insert")
            return False
    return True


def _to_rows(base_key, drafts):
    """Map drafts -> content_calendar rows, mirroring real_month_planner.to_calendar_rows:
    a FEED row is cross-posted to instagram AND facebook; a STORY row is instagram-only.
    Rows carry NO id. gym_id is forced to base_key. A draft with no post_date is dropped."""
    rows = []
    for draft in drafts or []:
        clean_caption = getattr(draft, "caption", "") or ""
        row = _row_from_draft(base_key, draft)
        if not row.get("post_date"):
            continue
        rows.append(row)
        # FB mirror: a feed also lands on Facebook (same cross-post the real month does).
        if row.get("format") == "feed" and (row.get("account") or "").lower() in (
                "instagram", "ig", ""):
            fb = dict(row)
            fb["account"] = "facebook"
            fb["caption"] = clean_caption
            rows.append(fb)
    return rows


def _instagram_feed_slots(rows):
    """Return the logical Instagram feed slots represented by calendar rows.

    At 1x the slot index is null and the date is the slot.  At 2x the two feeds
    share a date and differ by slot_index.  Facebook mirrors and Stories are not
    cadence units.
    """
    slots = set()
    for row in rows or ():
        item = row or {}
        if str(item.get("format") or "").strip().lower() != "feed":
            continue
        if str(item.get("account") or "").strip().lower() not in (
                "instagram", "ig", ""):
            continue
        post_date = str(item.get("post_date") or "")[:10]
        if post_date:
            slots.add((post_date, item.get("slot_index")))
    return slots


def _span_scoped_delete_claim(store, base_key, months, span_first, span_last,
                              preserve_dates=(), log=None):
    """How many of THIS gym's rows the month-grained delete in _apply will remove
    INSIDE the planned day-span [span_first, span_last] — the exact unit the
    independent restage verification measures with its own before/after
    readback (agent/fixer_ops.run_restage_month's terminal job).

    portal_calendar_store.delete_month wipes the WHOLE calendar month, so its
    raw return count also covers wipeable rows on days outside the requested
    span (2026-09-01..09-18 when restaging 21 days from 2026-09-19). Reporting
    that month-scoped number as the build's deleted claim made span-scoped
    evidence impossible: the verifier's snapshots only ever contain in-span
    rows, so the claimed and independently measured deleted counts could never
    agree and a genuine whole-month restage could never verify.

    The count mirrors delete_month's filter exactly (wipeable or NULL status,
    active variant, no recorded media hold, post_date not preserved), restricted
    to the planned span and to rows belonging to base_key (a foreign row is never
    this build's deletion). It is a CLAIM, never self-certification: the
    verifier recounts independently, so any divergence from what the delete
    actually removed in-span verifies False (fail closed).

    Returns None when the store cannot support a bounded read (no list_month,
    a read failure, or a partial row); the caller then reports the store's own
    count, exactly as before, and the verifier's comparison still decides."""
    from .portal_calendar_store import _WIPEABLE_STATUSES
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return None
    keep = {str(d)[:10] for d in (preserve_dates or ()) if str(d or "")[:10]}
    total = 0
    for month in months:
        try:
            rows = list_month(base_key, month) or []
        except Exception as exc:  # noqa: BLE001 - an unreadable store claims nothing new
            if log:
                log(f"{base_key}: span-scoped delete evidence read failed for "
                    f"{month} ({type(exc).__name__}); reporting the store's own count")
            return None
        for row in rows:
            if not isinstance(row, dict):
                return None
            if str(row.get("gym_id")) != str(base_key):
                continue                    # tenant binding: never another gym's row
            pd = str(row.get("post_date") or "")[:10]
            if not pd or pd < span_first or pd > span_last or pd in keep:
                continue
            status = str(row.get("status") or "").lower()
            if status and status not in _WIPEABLE_STATUSES:
                continue                    # human-owned rows survive (preserve_human)
            variant = str(row.get("variant_status") or "active").lower()
            if variant != "active":
                continue                    # candidates/archived rows are never deleted
            if row.get("media_not_ready_reason") is not None:
                continue                    # delete_month preserves recorded media holds
            total += 1
    return total


def _out_of_span_preserve_dates(months, span_first, span_last,
                                preserve_dates=()):
    """Return *every* calendar date outside the requested replacement span.

    The complete date set closes the read/delete race: even an out-of-span row
    inserted on a previously empty day after our snapshot is protected by the
    database delete predicate.  Existing rows do not need to be enumerated.
    """
    import calendar
    from datetime import date
    keep = {str(d)[:10] for d in (preserve_dates or ()) if str(d or "")[:10]}
    for month in months:
        year, number = (int(part) for part in month.split("-", 1))
        for day in range(1, calendar.monthrange(year, number)[1] + 1):
            pd = date(year, number, day).isoformat()
            if pd < span_first or pd > span_last:
                keep.add(pd)
    return keep


def _apply(base_key, rows, start, days, store, log, locked_days=(),
           allow_reshape=False, poster_render_evidence_by_url=None,
           render_evidence_by_url=None):
    """Delete-then-insert, gym-scoped, across every month the rows land in PLUS the full
    planned span. Rows are inserted WITHOUT an id (DB mints the uuid). Mirrors
    apply_month_plan. Refuses the demo gym id. Never raises out.

    Result counts: "deleted" is the SPAN-SCOPED claim (rows the delete removed
    inside the planned day-span, see _span_scoped_delete_claim) so the
    independent restage verification can confirm it against its own span-scoped
    readback; "deleted_total" is the store's raw month-grained count (the full
    truth of what was wiped, kept for the build's own release/restore
    bookkeeping). Stores that cannot support a bounded read report the raw
    count in both, exactly as before.

    locked_days: post_dates the builder SKIPPED because a human owns their feed. Those
    days' still-pending sibling rows (FB mirror + story on the approved feed's photo)
    are preserved from the delete — the builder emits no replacement for them, so
    wiping them would orphan the approved post's cross-post and story forever."""
    if base_key == config.demo_calendar_gym_id():
        return {"ok": False, "reason": "refusing to plan over the demo gym id",
                "upserted": 0, "deleted": 0}
    if not _logical_post_ids_valid(rows, log):
        return {"ok": False,
                "reason": "invalid pre-stamped logical_post_id; aborted before "
                          "any delete",
                "upserted": 0, "inserted": 0, "deleted": 0}
    from datetime import timedelta
    span = {(start + timedelta(days=i)).isoformat()[:7] for i in range(days)}
    for r in rows:
        pd = r.get("post_date")
        if pd:
            span.add(pd[:7])
    months = sorted(span)
    clean_rows = [{k: v for k, v in r.items() if k != "id"}
                  for r in rows if str(r.get("gym_id")) == str(base_key)]
    # WAVE 7 LEVER STAMPING (audit item 1, 2026-08-31; behind AGENT_LEARNING_LOOP).
    # This is the CLIENT month build — the lane that stages almost every row Echo
    # owns — and it never stamped a lever. The stamping lived only in LASSO's
    # planner, so content_calendar carried 1681 rows with hook_family / ask_type /
    # time_slot / caption_len_band NULL on every single one, post_metrics inherited
    # the nulls through the calendar join, and the monthly retro had nothing to
    # compare: gym_playbook has never held a row. Classification columns only —
    # captions, creative, status, dates and the approval path are untouched.
    from . import lever_stamp as _levers
    _levers.apply_learning_stamps(base_key, clean_rows, logger=log)
    deleted = inserted = 0
    deleted_row_snapshots = []
    insert_started = False
    planned_reservation_ids = set()
    try:
        # PRESERVE APPROVALS: drop any incoming row that would collide with a slot the
        # gym has already approved/published, and let delete_month keep those rows in
        # place (it only wipes fresh drafts). A rebuild can no longer revert an approval.
        from .portal_calendar_store import preserve_and_prune
        clean_rows, _locked = preserve_and_prune(store, base_key, months, clean_rows)
        planned_feed_slots = _instagram_feed_slots(clean_rows)
        from . import cadence as _cadence
        _slot_capacity = _cadence.resolve_posts_per_day(base_key, store)
        preflight = getattr(store, "preflight_cadence_rows", None)
        cadence_prevalidated = False
        # Every cadence must admit its required photo posts before deletion.
        # A 1x one-day build used to report success with zero inserted rows when
        # the caption belt first ran after delete (Swift River Oct 15/16).
        if callable(preflight) and planned_feed_slots:
            replace_dates = {
                (start + timedelta(days=i)).isoformat()
                for i in range(max(1, int(days)))
            } - {str(day)[:10] for day in (locked_days or ())}
            import inspect
            try:
                supports_dates = "replace_dates" in inspect.signature(
                    preflight).parameters
            except (TypeError, ValueError):
                supports_dates = False
            admitted_rows = (preflight(base_key, clean_rows,
                                       replace_dates=replace_dates)
                             if supports_dates else preflight(base_key, clean_rows))
            admitted_feed_slots = _instagram_feed_slots(admitted_rows)
            if not planned_feed_slots.issubset(admitted_feed_slots):
                log(f"{base_key}: cadence preflight refused "
                    f"{len(planned_feed_slots) - len(admitted_feed_slots)} required "
                    "Instagram feed slot(s); keeping the existing calendar")
                return {"ok": False, "reason": "incomplete cadence preflight",
                        "incomplete_cadence": True,
                        "expected_feed_slots": len(planned_feed_slots),
                        "admitted_feed_slots": len(admitted_feed_slots),
                        "inserted_feed_slots": 0,
                        "upserted": 0, "inserted": 0, "deleted": 0,
                        "months": months}
            clean_rows = admitted_rows
            cadence_prevalidated = True
        planned_reservation_ids = {
            int(r["_served_reservation_id"]) for r in clean_rows
            if r.get("_served_reservation_id")}
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
        # DAY SHAPE ASSERTION (ECHO_DAY_SHAPE_ASSERT, default ON). Run HERE, on the
        # exact rows about to be inserted (after preserve_and_prune has dropped the
        # human owned slots, so a day a coach already owns can never trip it), and
        # BEFORE the first delete. Two rows on one (gym_id, account, post_date,
        # format) must differ in BOTH caption and image_url.
        #
        # This is the check that was missing on 2026-08-30, when this lane wrote
        # slot_index 0 and slot_index 1 of piercefitness 2026-09-27 carrying one
        # identical caption, and that is the plan shape which published Tough Temple
        # six times in forty seconds. A violation FAILS the pass: nothing is deleted,
        # nothing is inserted, the existing calendar is left intact, and the broken
        # days are named. A client seeing the same post twice on their own account is
        # worse than a build that stops and asks for a human.
        try:
            day_shape.assert_day_distinct(
                clean_rows, enabled=config.day_shape_assert_enabled())
        except day_shape.DayShapeViolation as exc:
            for v in exc.violations:
                log(f"DAY SHAPE FAIL: {v.message()}")
            try:
                from . import ops_alerts
                ops_alerts.alert(
                    f"{base_key}: month build STOPPED and wrote nothing. "
                    f"{len(exc.violations)} day(s) would have put the same post "
                    f"twice on one account. First: {exc.violations[0].message()}")
            except Exception:  # noqa: BLE001 - the alert never sinks the report
                pass
            # DAY SHAPE BLOCK ALARM (queue item 4, 2026-09-06): name the gym's
            # remaining runway, track consecutive blocked days, escalate to
            # SOCIAL the day the streak first reaches threshold. The existing
            # ops_alerts.alert above already re-fires daily on its own (this
            # except branch runs every time the plan pass hits the same
            # violation); this block only adds the runway context and the
            # one extra escalation. Never lets a fetch failure block the
            # day-shape refusal itself.
            try:
                existing_rows = []
                for m in months:
                    existing_rows.extend(store.list_month(base_key, m) or [])
                day_shape_block_alarm.record_and_maybe_escalate(
                    base_key, exc.violations, existing_rows)
            except Exception as e:  # noqa: BLE001 - alarm failure never sinks the refusal
                log(f"day-shape block alarm failed for {base_key}: "
                    f"{type(e).__name__}: {e}")
            return {"ok": False,
                    "reason": "day shape: same post twice in one day",
                    "day_shape_violations": [v.message() for v in exc.violations],
                    "upserted": 0, "inserted": 0, "deleted": 0, "months": months}

        # SLOT CAPACITY (2026-09-11, the 141-group lasso duplicate-active audit).
        # day_shape.assert_day_distinct only refuses two rows sharing a caption or
        # photo; it does nothing for two rows that are each genuinely distinct
        # content but still both land 'active' in the same (account, post_date,
        # format) slot. That is the exact shape of the 2026-08-08 seed defect
        # (see day_shape.py's SLOT CAPACITY section) -- ported here so every
        # delete-then-insert rebuild lane carries the same belt, not just
        # real_month_planner's.
        try:
            day_shape.assert_slot_capacity(
                clean_rows, enabled=config.day_shape_assert_enabled(),
                capacity=_slot_capacity)
        except day_shape.SlotCapacityViolation as exc:
            for v in exc.violations:
                log(f"SLOT CAPACITY FAIL: {v.message()}")
            try:
                from . import ops_alerts
                ops_alerts.alert(
                    f"{base_key}: month build STOPPED and wrote nothing. "
                    f"{len(exc.violations)} slot(s) would have held more posts "
                    f"than the day's cadence allows. "
                    f"First: {exc.violations[0].message()}")
            except Exception:  # noqa: BLE001 - the alert never sinks the report
                pass
            return {"ok": False,
                    "reason": "day shape: slot over capacity",
                    "slot_capacity_violations": [v.message() for v in exc.violations],
                    "upserted": 0, "inserted": 0, "deleted": 0, "months": months}

        # CTA SELF-QUESTION GATE (ECHO_CTA_SELF_QUESTION_GATE, default ON). Same
        # plan-time, same fail-closed shape as the day-shape assertion above:
        # nothing is deleted, nothing is inserted, when any row carries the
        # banned Reverb FAQ line or an FAQ-mined self-question closing CTA. See
        # agent/cta_self_question_gate.py for why this is scoped to CTA form
        # only, never to audience/topic words (HYROX and competitive CrossFit
        # remain a valid Echo audience, Blake 2026-09-06 -- this gate does not
        # touch that).
        from . import accounts as _accounts
        _gym_name_for_gate = _display_name_for(
            _accounts.get_account(f"{base_key}_ig")
            or _accounts.get_account(f"{base_key}_fb"))
        try:
            cta_self_question_gate.assert_no_self_question_cta(
                clean_rows, _gym_name_for_gate,
                enabled=config.cta_self_question_gate_enabled())
        except cta_self_question_gate.CtaSelfQuestionGateViolation as exc:
            for v in exc.violations:
                log(f"CTA SELF-QUESTION FAIL: {v.message()}")
            try:
                from . import ops_alerts
                ops_alerts.alert(
                    f"{base_key}: month build STOPPED and wrote nothing. "
                    f"{len(exc.violations)} row(s) carried a banned or "
                    f"self-question CTA. First: {exc.violations[0].message()}")
            except Exception:  # noqa: BLE001 - the alert never sinks the report
                pass
            return {"ok": False,
                    "reason": "cta self-question gate: banned or self-question CTA",
                    "cta_gate_violations": [v.message() for v in exc.violations],
                    "upserted": 0, "inserted": 0, "deleted": 0, "months": months}
        # NEVER SHRINK (TopFuel 2026-08-25): a grow-to-cap rebuild must only GROW, never
        # replace a good calendar with a SMALLER one. The grow-guard can re-trigger a build
        # for a gym that is already built out (build_target counts photo clusters, some of
        # which are un-plannable), and the reuse window then blocks re-picking most photos, so
        # the rebuild yields only a FEW feeds. Deleting-then-inserting-fewer shrank the
        # calendar every cycle (TopFuel drifted 39 -> 21 -> 3). If this build placed fewer
        # feeds than already exist, keep the existing calendar untouched. Feeds are counted as
        # logical Instagram slots: date alone at 1x, date + slot_index at 2x.
        new_feed_slots = _instagram_feed_slots(clean_rows)
        new_feeds = len(new_feed_slots)
        # POST-MERGE comparison (audit 2026-08-25 MAJOR): a grow build EXCLUDES locked
        # (human-owned approved/published) days from its own rows — their feeds survive the
        # delete via preserve_dates. Comparing only new_feeds against existing_feeds
        # (which counts the locked ones) wrongly read every incremental grow as a shrink
        # and no-op'd it, so a built gym could never grow. Compare what the calendar will
        # hold AFTER the write: this build's feeds + the preserved locked-day feeds.
        locked_span_first = start.isoformat()
        locked_span_after = (start + timedelta(days=max(1, int(days)))).isoformat()
        locked_in_span = {
            str(d)[:10] for d in (locked_days or ())
            if locked_span_first <= str(d)[:10] < locked_span_after}
        locked_feed_slots = set()
        locked_feed_slots_known = not locked_in_span
        list_month = getattr(store, "list_month", None)
        if callable(list_month) and locked_in_span:
            try:
                locked_rows = []
                for month in months:
                    locked_rows.extend(list_month(base_key, month) or [])
                from .onboarding_demo import is_sample_row
                locked_feed_slots = _instagram_feed_slots([
                    row for row in locked_rows
                    if str((row or {}).get("status") or "").lower()
                    not in ("denied", "killed", "deleted")
                    and not is_sample_row(row)
                ])
                locked_feed_slots = {
                    slot for slot in locked_feed_slots
                    if slot[0] in locked_in_span}
                locked_feed_slots_known = True
            except Exception:  # noqa: BLE001 - handled by cadence-aware guard below
                locked_feed_slots_known = False
        else:
            locked_feed_slots_known = not locked_in_span
        if not locked_feed_slots_known:
            if int(_slot_capacity or 1) > 1:
                log(f"{base_key}: locked feed slot count unavailable for a "
                    "multi-slot cadence; keeping the existing calendar")
                return {"ok": False,
                        "reason": "locked feed slot count unavailable",
                        "incomplete_cadence": True,
                        "upserted": 0, "inserted": 0, "deleted": 0,
                        "months": months}
            # One post/day has exactly one logical feed slot per locked day.
            locked_feed_slots = {(day, None) for day in locked_in_span}
        post_merge_feeds = new_feeds + len(locked_feed_slots)
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
                f"{post_merge_feeds} ({new_feeds} new + "
                f"{len(locked_feed_slots)} locked); "
                "keeping the existing calendar (grow-only, never shrink)")
            return {"ok": True, "upserted": 0, "inserted": 0, "deleted": 0,
                    "months": months, "noop_shrink": True,
                    "existing_feeds": existing_feeds, "new_feeds": new_feeds}
        # SPAN-SCOPED DELETED CLAIM (restage postcondition): computed from a
        # bounded read BEFORE the delete, in the same unit the independent
        # verifier measures. None when the store cannot support the read --
        # the raw count is then reported, and the verifier still decides.
        span_first = start.isoformat()
        span_last = (start + timedelta(days=max(1, int(days)) - 1)).isoformat()
        span_claim = _span_scoped_delete_claim(store, base_key, months,
                                               span_first, span_last,
                                               preserve_dates=locked_days, log=log)
        delete_preserve = _out_of_span_preserve_dates(
            months, span_first, span_last, preserve_dates=locked_days)
        bounded_delete_read = callable(getattr(store, "list_month", None))
        delete_month = getattr(store, "delete_month", None)
        for month in months:
            if delete_month is not None:
                try:
                    import inspect
                    supports_rows = "return_rows" in inspect.signature(
                        delete_month).parameters
                    result = delete_month(
                        base_key, month, preserve_dates=delete_preserve,
                        **({"return_rows": True} if supports_rows else {}))
                    if isinstance(result, list):
                        deleted_row_snapshots.extend(result)
                        deleted += len(result)
                    else:
                        deleted += result or 0
                except TypeError:      # older store/test fakes without the kwarg
                    if bounded_delete_read:
                        raise RuntimeError("bounded store cannot preserve dates")
                    deleted += delete_month(base_key, month) or 0
        insert_rows = getattr(store, "insert_rows", None)
        if insert_rows is not None and clean_rows:
            store_rows = [{k: v for k, v in r.items()
                           if k != "_served_reservation_id"} for r in clean_rows]
            insert_started = True
            inserted_rows = _insert_rows_with_poster_evidence(
                insert_rows, base_key, store_rows,
                poster_render_evidence_by_url,
                render_evidence_by_url=render_evidence_by_url,
                required_feed_slots=(new_feed_slots
                                     if cadence_prevalidated else None),
                prevalidated_cadence=cadence_prevalidated) or []
            inserted += len(inserted_rows)
            inserted_feed_slots = _instagram_feed_slots(inserted_rows)
            # Require the write path to return every planned feed slot.  Human-owned
            # rows and retained media holds were removed above, so any remaining
            # shortfall means a stage-time belt dropped required cadence.
            expected_feed_count = len(new_feed_slots)
            actual_feed_count = len(inserted_feed_slots)
            if actual_feed_count < expected_feed_count:
                log(f"{base_key}: cadence staging incomplete: inserted "
                    f"{actual_feed_count}/{expected_feed_count} Instagram "
                    "feed slots; cadence remains pending and the scan will retry")
                return {"ok": False, "reason": "incomplete cadence staging",
                        "incomplete_cadence": True,
                        "expected_feed_slots": expected_feed_count,
                        "inserted_feed_slots": actual_feed_count,
                        "upserted": inserted, "inserted": inserted,
                        "deleted": deleted if span_claim is None else span_claim,
                        "deleted_total": deleted, "months": months,
                        "retained_reservation_ids": sorted(planned_reservation_ids)}
    except Exception as exc:  # noqa: BLE001
        rollback_ok = False
        rollback_attempted = False
        try:
            from .portal_calendar_store import CalendarInsertNotStartedError
            known_pre_post_refusal = isinstance(
                exc, CalendarInsertNotStartedError)
        except Exception:  # noqa: BLE001
            known_pre_post_refusal = False
        restore = getattr(store, "restore_deleted_rows", None)
        definite_pre_post_failure = not insert_started
        if ((known_pre_post_refusal or definite_pre_post_failure)
                and deleted_row_snapshots
                and callable(restore)):
            rollback_attempted = True
            try:
                restored = restore(base_key, deleted_row_snapshots) or []
                rollback_ok = len(restored) == len(deleted_row_snapshots)
                log(f"{base_key}: cadence changed after preflight; restored "
                    f"{len(restored)}/{len(deleted_row_snapshots)} deleted row(s)")
            except Exception as rollback_exc:  # noqa: BLE001
                log(f"{base_key}: calendar rollback failed: "
                    f"{type(rollback_exc).__name__}")
        log(f"store write failed: {type(exc).__name__}")
        return {"ok": False, "reason": f"store write failed: {type(exc).__name__}",
                "upserted": inserted, "deleted": deleted, "months": months,
                "deleted_total": deleted,
                "effective_deleted": 0 if rollback_ok else deleted,
                "rollback_restored": rollback_ok,
                "rollback_failed": bool(rollback_attempted and not rollback_ok),
                # Once the insert request started, its outcome may be unknown. Keep
                # those exact reservations fail closed; pre-insert failures retain none.
                "retained_reservation_ids": sorted(planned_reservation_ids)
                if (insert_started and not rollback_ok
                    and not known_pre_post_refusal) else [],
                "insert_outcome_unknown": bool(
                    insert_started and not rollback_ok
                    and not known_pre_post_refusal)}
    return {"ok": True, "upserted": inserted, "inserted": inserted,
            "deleted": deleted if span_claim is None else span_claim,
            "deleted_total": deleted, "months": months,
            "retained_reservation_ids": sorted(planned_reservation_ids)
            if insert_started else []}


def backfill_denied_slots(account, base_key, start_date, days=30, *, voice,
                          library_path=None, store, banned_words=(), logger=None):
    """Thin per-gym-locked wrapper around _backfill_denied_slots_body (below).

    PER-GYM BUILD LOCK (independent audit, 2026-09-11, CrossFit Reverb): this
    function is INSERT-only and was never wrapped by agent/build_lock.py, unlike
    build_client_month. Ground truth in the deployed echo service's own logs
    (2026-09-11 18:29-19:27 UTC) showed a full build_client_month rebuild placing
    fresh rows on 2026-09-24 at 18:49:34, then THIS function independently
    replacing a denied 2026-09-21 slot by "rolling forward" onto that SAME
    2026-09-24 at 19:20:40 -- a second, fully independent 4-row batch stacked on
    a day the rebuild had just filled seconds/minutes earlier. Not a classic
    race (the two calls did not overlap in time): backfill_denied_slots's own
    day-collision check (denybf_dayused_<base>_<day>) deliberately ignores rows
    from ANY other path by design (the Dale/ENG fix above), so it had no way to
    know the rebuild had just claimed that day. Serializing this function against
    build_client_month (and against itself) with the SAME per-gym lock closes the
    window: a backfill pass that starts while a rebuild for the same gym is still
    writing (or another backfill pass is) is refused cleanly instead of stacking
    a duplicate batch onto whatever day the other call just placed content on.
    """
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
    from . import build_lock as _build_lock
    _lock_holder = f"{os.getpid()}:{id(store)}"
    if not _build_lock.acquire(base_key, holder=_lock_holder):
        log(f"{base_key}: a build or another backfill pass is already in progress "
            "for this gym; skipping this backfill rather than stacking onto "
            "whatever day it just placed content on (see agent/build_lock.py)")
        return {"ok": False, "reason": "build_in_progress", "backfilled": 0}
    # HEARTBEAT: same reasoning as build_client_month above -- a backfill pass
    # calls the same caption/vision paths per slot, so it gets the same
    # renew-on-interval treatment instead of racing a long pass's own lock.
    _heartbeat = _build_lock.start_heartbeat(base_key, holder=_lock_holder)
    reservation_state = {"drafts": [], "insert_started": False}
    try:
        return _backfill_denied_slots_body(
            account, base_key, start_date, days, voice=voice,
            library_path=library_path, store=store, banned_words=banned_words,
            logger=log, _reservation_state=reservation_state)
    except Exception:
        if not reservation_state["insert_started"]:
            _release_unlanded_reservations(reservation_state["drafts"])
            _rollback_new_drive_drafts(reservation_state["drafts"], log)
        raise
    finally:
        _heartbeat.stop()
        _build_lock.release(base_key, holder=_lock_holder)


def _backfill_denied_slots_body(account, base_key, start_date, days=30, *, voice,
                                library_path=None, store, banned_words=(), logger=None,
                                _reservation_state=None):
    """Give each DENIED feed POST a FRESH 1:1 replacement (a NEW caption on a REUSED photo)
    for a gym that is AT its creative cap — where the monthly grow-to-cap build is a no-op
    and the denied slot would otherwise stay empty forever (the portal's "recreating" state
    never resolving; Dale / ENG, 2026-08-19).

    ALWAYS 1:1 (Blake, 2026-09-05): every denied row gets its own replacement, regardless
    of whether its day already has OTHER active content. This used to skip a denied row
    whenever its day had any active (pending/approved/published) feed at all — correct
    when that active feed WAS the row's own prior replacement, but wrong whenever a day
    legitimately carries more than one independently-scheduled post: found live when a
    real client (Dale/ENG) denied one of two same-day Instagram posts and the OTHER,
    unrelated published post on that day silently blocked his denied one from ever being
    replaced, for weeks. Idempotency is therefore now keyed to the denied ROW's own id (a
    kv marker, denybf_done_<row id>), not to day coverage — a row already replaced is never
    retried, but an unrelated active post on the same day no longer blocks a different
    denied post's own replacement.

    SPREAD ACROSS DAYS, NOT STACKED (Pete/Zanshin, 2026-09-08): "always 1:1, regardless of
    the day" is correct for Dale's case (an UNRELATED active post must never block a denied
    row's own replacement) but was silently letting MULTIPLE denied rows that happened to
    share an original post_date each write their own fresh replacement onto that SAME date
    — nothing capped it, so a gym that had denied several different posts originally
    scheduled for one day accumulated 3-4 stacked posts on that single day over repeated
    runs, while other days sat comparatively thin. Reproduced live: content_calendar for
    zanshinfitness630e22 carried up to 4 independent captions all target-dated to the same
    day, each one a legitimate 1:1 replacement of a DIFFERENT denied row, none a duplicate
    of each other.

    The fix is narrower than day-coverage ever was: a day is skipped for a NEW backfill
    placement only once THIS FUNCTION has already placed a backfill replacement there (a
    durable kv marker, denybf_dayused_<base_key>_<day>, set only after that day's insert
    genuinely succeeds) — an unrelated published/approved/pending post from any OTHER path
    still never blocks anything, so Dale's case is untouched. When a denied row's own day
    already carries this function's own prior placement, the replacement rolls forward to
    the next day inside the backfill window that does not yet have one; if the whole window
    is already saturated, it falls back to the row's own original day rather than dropping
    the replacement (a rare stack is better than a denied slot silently never being filled).

    The replacement REUSES a photo (allow_reuse — the gym has no fresh creative left) but
    NEVER the denied post's own photo and NEVER a photo consumed by an approved/published
    row. Every replacement clears the same A+ / banned-word / fabrication gates as a normal
    build and is written PENDING (owner-visible, awaits approval). INSERT-only: the
    existing calendar is never deleted. Behind AGENT_DENY_BACKFILL (OFF by default) — flag
    off -> returns ok:False and touches nothing.

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
    # HARD PLANNING HORIZON: a denied slot beyond one month out is relearn churn (the
    # monthly rebuild replaces it anyway) — never backfill past the horizon. Clamping
    # shortens only the window's tail; denied days inside the month still backfill.
    from .plan_horizon import horizon_clamp
    days = horizon_clamp(start, days, logger=log, label=f"{base_key} deny-backfill")
    if days <= 0:
        return {"ok": True, "backfilled": 0, "days_needing": 0, "skipped": 0,
                "reason": "backfill window is beyond the planning horizon"}
    win_start = start.isoformat()
    win_end = (start + timedelta(days=max(1, days) - 1)).isoformat()
    months = sorted({(start + timedelta(days=i)).isoformat()[:7]
                     for i in range(max(1, days))})

    denied_rows = []            # [{"day", "photo", "row_id"}] -- every denied row, each
                                 # replaced 1:1 regardless of other same-day active content.
    live_photo_keys = set()     # photos on approved/published/publishing rows (never reused)
    # DRIVE ASSET EXCLUSION (independent audit, 2026-09-08): live_photo_keys is a set of
    # LOCAL-library basenames; gym_media_builder.build_gym_media_draft picks from the
    # separate Drive asset pool by Drive file id, a different id space entirely, so the
    # local exclusion set can never protect it. Tracked in parallel so the Drive-first
    # replacement (below) can be told the same two things the local-reuse path already
    # enforces: never a photo live elsewhere in the book, never the denied post's own
    # photo -- without this, a denied Drive asset's used_count/last_used_at is reset by
    # gym_media_selector.rollback_use the moment it's denied, making it the pool's
    # LEAST-used candidate again and letting the exact photo just denied come right back
    # as its own "fresh" replacement.
    live_drive_asset_ids = set()
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
                aid = row.get("source_media_asset_id")
                if aid:
                    live_drive_asset_ids.add(str(aid))
            pd = str(row.get("post_date") or "")[:10]
            if not pd or pd < win_start or pd > win_end:
                continue
            fmt = str(row.get("format") or "").lower()
            acct = str(row.get("account") or "").lower()
            if fmt == "feed" and acct in ("instagram", "ig", "") and status == "denied":
                denied_rows.append({"day": pd,
                                    "photo": _url_basename(row.get("image_url") or ""),
                                    "row_id": row.get("id"),
                                    "asset_id": row.get("source_media_asset_id")})

    # Per-ROW idempotency: a denied row already replaced (kv-marked after a successful
    # insert below) is never retried, no matter what else is or isn't active on its day.
    from . import db as _db
    todo = []
    for d in sorted(denied_rows, key=lambda r: (r["day"], str(r.get("row_id") or ""))):
        rid = d.get("row_id")
        if rid and _db.kv_get(f"denybf_done_{rid}"):
            continue
        todo.append(d)
    if not todo:
        return {"ok": True, "backfilled": 0, "days_needing": 0, "skipped": 0}

    banned_words = tuple(banned_words or ())
    # CROSS-DAY MEDIA GUARD (Blake, 2026-08-31): a replacement may REUSE a photo —
    # that is this lane's whole point — but never one already sitting on ANOTHER
    # day of the gym's forward book (pending rows included; the old exclude list
    # only knew approved/published/publishing) or published within the trailing
    # repeat window. Read once per run; a read failure degrades open.
    from . import media_guard
    guard_state = {}
    if media_guard.enabled():
        try:
            guard_state = media_guard.book_state(base_key, store, start, days, log=log,
                                                 library_path=library_path)
        except Exception as exc:  # noqa: BLE001 - the guard never sinks a backfill
            log(f"{base_key}: cross-day media guard read skipped ({type(exc).__name__})")
    # DRIVE-FIRST REPLACEMENT (Pete/Zanshin, 2026-09-07): a denied slot used to go
    # straight to reusing a local photo (allow_reuse=True below), even for a gym
    # with a connected Drive pool full of fresh, unused material -- this is the
    # exact "I deny a photo and a repeat comes back" experience Pete reported.
    # append_gym_drive_drafts (the month-build's own Drive fallback) is never
    # called from this function, so a denied-slot replacement never got the same
    # chance. Try ONE fresh Drive-sourced draft per denied day first, same gating
    # (both flags) and same builder append_gym_drive_drafts already uses; fall
    # through to the existing local-reuse path unchanged when Drive can't cover
    # it (source missing, lane unarmed, or the builder itself declines).
    drive_first = (config.gym_drive_stage_enabled()
                  and config.gym_drive_connect_active_for(
                      getattr(account, "key", "") or base_key))
    drive_pillar_i = 0
    drafts = []
    if _reservation_state is not None:
        _reservation_state["drafts"] = drafts
    skipped = 0
    done_row_ids = []
    used_days_for_marker = []    # every day a replacement was actually placed onto this
                                 # pass, marked durable AFTER insert succeeds -- unconditional
                                 # on the denied row having an id, unlike done_row_ids
    day_used_this_pass = set()   # days this run has already placed a backfill row onto

    def _day_already_used(day_iso):
        if day_iso in day_used_this_pass:
            return True
        try:
            return bool(_db.kv_get(f"denybf_dayused_{base_key}_{day_iso}"))
        except Exception:  # noqa: BLE001 - a read failure never blocks a placement
            return False

    def _next_open_day(original_day):
        """The first day >= original_day, inside [win_start, win_end], that this
        function has not already placed a backfill replacement onto. Falls back to
        original_day (accepting a rare stack) when the whole window is saturated --
        never leaves a denied row unreplaced for lack of an open day."""
        d = date.fromisoformat(original_day)
        end = date.fromisoformat(win_end)
        while d <= end:
            iso = d.isoformat()
            if not _day_already_used(iso):
                return iso
            d += timedelta(days=1)
        return original_day

    for denied in todo:
        day_key = _next_open_day(denied["day"])
        if day_key != denied["day"]:
            log(f"{base_key}: denied row on {denied['day']} already has a backfill "
                f"replacement there -- rolling this one forward to {day_key} instead "
                "of stacking")
        # Exclude the denied post's OWN photo (never hand the same one back) + every photo
        # already live on the page. Everything else may be REUSED.
        exclude = set(live_photo_keys)
        own = denied.get("photo")
        if own:
            exclude.add(own)
        blocked = (media_guard.blocked_keys(guard_state, day_key)
                   if guard_state else set())
        feed = drop = None
        if drive_first:
            try:
                from . import gym_media_builder, post_quality
                pillar = _GYM_DRIVE_PILLARS[drive_pillar_i % len(_GYM_DRIVE_PILLARS)]
                source = _gym_drive_source_for(
                    getattr(account, "key", "") or base_key, day_key)
                if source is not None:
                    # Same two exclusions the local-reuse path enforces below, in the
                    # Drive pool's own id space (independent audit, 2026-09-08): the
                    # denied post's OWN Drive asset (its used_count is reset by
                    # gym_media_selector.rollback_use the moment it's denied, making
                    # it the pool's least-used candidate again) + every asset already
                    # live elsewhere in the book.
                    drive_exclude = set(live_drive_asset_ids)
                    own_asset = denied.get("asset_id")
                    if own_asset:
                        drive_exclude.add(str(own_asset))
                    feed = gym_media_builder.build_gym_media_draft(
                        account, day_key, pillar, voice, source,
                        exclude_ids=drive_exclude)
                    # SAME HARD GATE the local-reuse path enforces (this function's
                    # own docstring promises it fleet-wide): a Drive-sourced draft
                    # that fails A+/banned-word is never silently placed just
                    # because it came from a different lane.
                    if feed is not None:
                        gate_ok = (post_quality.is_a_plus(feed, banned_words,
                                                          require_media=True)
                                  if config.sb7_enabled()
                                  else not _has_banned_word(feed.caption, banned_words))
                        if not gate_ok:
                            log(f"{base_key} {day_key}: drive-first replacement "
                                "failed the A+/banned-word gate; falling back to "
                                "local reuse")
                            _rollback_drive_asset(feed, day_key, log)
                            feed = None
                    if feed is not None:
                        drive_pillar_i += 1
                        log(f"{base_key} {day_key}: denied slot replaced from the "
                            "connected Drive pool (asset "
                            f"{getattr(feed, 'source_media_asset_id', '')})")
            except Exception as exc:  # noqa: BLE001 - Drive lane never sinks the backfill
                if feed is not None:
                    # The returned draft has not reached a calendar row yet.
                    _rollback_drive_asset(feed, day_key, log)
                log(f"{base_key} {day_key}: drive-first replacement failed "
                    f"({type(exc).__name__}); falling back to local reuse")
                feed = None
        if feed is None:
            feed, drop = _clean_draft_for_day(
                account, day_key, voice, library_path, banned_words, log,
                exclude_keys=exclude | blocked, allow_reuse=True)
        if (feed is None or not _has_real_creative(feed)) and blocked:
            # SMALL LIBRARY: every reusable photo already sits on another day of the
            # book. Do not leave the denied slot empty — fall back to the photo whose
            # other appearances are FARTHEST from this day (maximum spacing) and say
            # so once (kv-deduped digest). Never fabricated, still every A+ gate.
            choice = media_guard.spaced_choice(library_path, guard_state, day_key,
                                               hard_exclude=exclude)
            if choice:
                media_guard.alert_small_library(base_key, day_key, log)
                force_only = media_guard.library_keys(library_path) - {choice}
                feed, drop = _clean_draft_for_day(
                    account, day_key, voice, library_path, banned_words, log,
                    exclude_keys=exclude | force_only, allow_reuse=True)
                if feed is not None:
                    log(f"{base_key} {day_key}: small library — reusing {choice} "
                        "with maximum spacing (no unused photo remained)")
        if feed is None or not _has_real_creative(feed):
            if feed is not None:
                _rollback_drive_asset(feed, day_key, log)
            skipped += 1
            log(f"{base_key} {day_key}: no A+ replacement could be built "
                f"({drop or 'no usable creative'})")
            continue
        if not _record_feed_served(account, feed, day_key):
            _rollback_drive_asset(feed, day_key, log)
            skipped += 1
            log(f"{base_key} {day_key}: served ledger write failed; backfill held")
            continue
        # This run's placement joins the guard state so the NEXT denied day in the
        # same pass cannot pick the same photo (the store read happened before any
        # insert).
        media_guard.note_placed(
            guard_state, _url_basename(getattr(feed, "creative_public_url", ""))
            or os.path.basename(getattr(feed, "creative_path", "") or ""), day_key)
        try:
            finished = _finish_feed_with_story(
                account, feed, library_path, log, day_key=day_key)
        except Exception:
            _release_feed_reservation(feed)
            _rollback_drive_asset(feed, day_key, log)
            raise
        if not finished:
            _release_feed_reservation(feed)
            _rollback_drive_asset(feed, day_key, log)
            skipped += 1
            log(f"{base_key} {day_key}: held; visual provenance could not stage the feed")
            continue
        drafts.extend(finished)
        day_used_this_pass.add(day_key)   # so the NEXT denied row this pass rolls past it too
        used_days_for_marker.append(day_key)
        if denied.get("row_id"):
            done_row_ids.append(denied["row_id"])

    if not drafts:
        return {"ok": True, "backfilled": 0, "days_needing": len(todo),
                "skipped": skipped}

    try:
        rows = _to_rows(base_key, drafts)
        # First-month replacements land 'pending' like any new draft (Blake
        # 2026-10-06: nothing new is ever coach_review).
        from . import gbp_mirror as _gbp_mirror
        rows.extend(_gbp_mirror.rows_for(base_key, drafts, library_path=library_path,
                                         logger=log))
        clean_rows = [{k: v for k, v in r.items() if k != "id"}
                      for r in rows if str(r.get("gym_id")) == str(base_key)]
        from . import lever_stamp as _levers
        _levers.apply_learning_stamps(base_key, clean_rows, logger=log)
    except Exception:
        # No insert request has started, so every exact reservation is unlanded.
        _release_unlanded_reservations(drafts)
        raise
    if not _logical_post_ids_valid(clean_rows, log):
        # Nothing has been requested from the store: every exact reservation is
        # unlanded and the denied rows stay eligible for a repaired retry.
        _release_unlanded_reservations(drafts)
        return {"ok": False, "reason": "invalid pre-stamped logical_post_id; "
                "aborted before insert", "backfilled": 0,
                "days_needing": len(todo), "skipped": skipped}
    try:
        store_rows = [{k: v for k, v in r.items()
                       if k != "_served_reservation_id"} for r in clean_rows]
        if _reservation_state is not None:
            _reservation_state["insert_started"] = True
        inserted_rows = _insert_rows_with_poster_evidence(
            insert_rows, base_key, store_rows,
            _poster_render_evidence_by_url(drafts)) or []
        inserted = len(inserted_rows)
    except Exception as exc:  # noqa: BLE001
        # The insert request may have reached the remote store before the exception;
        # keep reservations fail closed when its outcome is unknown.
        log(f"{base_key}: backfill insert failed: {type(exc).__name__}")
        return {"ok": False, "reason": f"insert failed: {type(exc).__name__}",
                "backfilled": 0, "days_needing": len(todo), "skipped": skipped}
    if inserted == 0:
        # A completed insert call returning zero rows is a proven no-op. Release only
        # this attempt's exact reservations; an exception above remains unknown and
        # deliberately retains them fail closed.
        _release_unlanded_reservations(drafts)
        _rollback_new_drive_drafts(drafts, log)
        return {"ok": True, "backfilled": 0, "rows": 0,
                "days_needing": len(todo), "skipped": skipped}

    # Stamp per-row AND per-day idempotency ONLY after the insert genuinely succeeded --
    # a failed insert must leave every denied row (and its target day) eligible for retry
    # next pass, not silently marked done/used with no actual replacement ever written.
    for rid in done_row_ids:
        try:
            _db.kv_set(f"denybf_done_{rid}", "1")
        except Exception:  # noqa: BLE001 - a marker failure never blocks the backfill itself
            pass
    for used_day in used_days_for_marker:
        try:
            _db.kv_set(f"denybf_dayused_{base_key}_{used_day}", "1")
        except Exception:  # noqa: BLE001 - a marker failure never blocks the backfill itself
            pass
    days_done = len({r.get("post_date") for r in clean_rows
                     if r.get("format") == "feed"})
    log(f"{base_key}: backfilled {days_done} denied slot(s) with a fresh caption on a "
        f"reused photo ({inserted} row(s), {skipped} day(s) unbuildable)")
    return {"ok": True, "backfilled": days_done, "rows": inserted,
            "days_needing": len(todo), "skipped": skipped}
