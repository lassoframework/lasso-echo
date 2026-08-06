"""
real_month_planner.py — assemble a full month of REAL LASSO drafts (two per day,
one feed 1080x1080 + one paired 9:16 story) spanning EVERY LASSO content type, as
content_calendar rows for gym_id='lasso'. This is the calendar a salesperson walks a
prospect through: the ACTUAL LASSO plan, not the demo.

Behind AGENT_REAL_MONTH_PLAN (config.real_month_plan_enabled(), default OFF). OFF ->
nothing here is invoked and today is byte-for-byte unchanged.

THREE PURE LAYERS + a thin apply (all injectable, offline-testable):

  1. plan_month(account_key, start_date, days=30) -> a deterministic list of PlanSlot
     objects. For each date, the category is the weekly rotation slot
     (content_categories._DAILY_SCHEDULE: Mon podcast, Tue platform, Wed b2b, Thu
     podcast clip, Fri summit, Sat platform, Sun podcast infographic) with the
     book-release-day / Summit run-up / new-client welcome OVERRIDES folded in on the
     days they occur. Every date yields EXACTLY two slots: a "feed" slot and a paired
     "story" slot. Pure: no I/O, no Date.now, no writes. days <= 0 -> [].

  2. build_month_drafts(plan, builders, *, story_builder=None) -> for each slot, call
     the EXISTING category draft/creative builder (an injected map of category ->
     callable) to produce a real Draft (real caption from the bible/source, real
     creative path, correct format). A feed slot and its paired story slot become
     SEPARATE Draft objects. A slot whose builder returns None, or whose source/creative
     is missing, is SKIPPED (logged) and NEVER fabricated. A story slot is only ever
     built from a genuine 9:16 asset via the injected story_builder anchored to that
     day's feed draft (the same honesty guard stories.py already enforces).

  3. to_calendar_rows(drafts, account_key) -> content_calendar row dicts
     (gym_id=account_key, account=platform, post_date, pillar=category, format
     feed|story, caption, image_url, status='pending'). Reuses real_calendar_mirror's
     row mapping so the shape is identical to the live mirror.

  4. apply_month_plan(account_key, drafts, sb_store) -> upsert the real rows through the
     injectable SupabaseCalendarStore AND delete ALL demo rows for the gym (closing the
     month-range gap the mirror audit flagged: the mirror only swept the months real
     drafts landed in; here we sweep the FULL planned span PLUS every month that holds a
     demo row for the gym). Gym-scoped: never touches another gym. Writes calendar rows
     only; NOTHING here publishes.

HARD RULES (never weakened):
  * NO fabricated facts, stats, offers, or prices. Every draft is produced by an
    existing approved builder; a builder that cannot draft from an approved source
    returns None and that slot is dropped.
  * Feed and story are separate drafts; a story is 9:16 (1080x1920) and never a cropped
    feed card (the injected story_builder is the sole story source, reusing stories.py's
    genuine-9:16 guard).
  * gym_id is FORCED to account_key on every emitted row; a real gym never keeps a demo
    id after apply.
  * Behind AGENT_REAL_MONTH_PLAN. Flag off -> the runner never calls this; today is
    unchanged. Nothing here publishes or hosts.
"""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config
from . import content_categories as _cats
from . import real_calendar_mirror as _mirror

# The weekly rotation, keyed by weekday abbr. This is the SAME seven-day schedule the
# runner drives from (content_categories._DAILY_SCHEDULE); we read the category leg of
# it so the month calendar matches exactly what Echo posts day to day:
#   Mon podcast, Tue platform, Wed b2b, Thu podcast clip, Fri summit,
#   Sat platform, Sun podcast infographic.
_WEEKDAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

FEED = "feed"
STORY = "story"


@dataclass(frozen=True)
class PlanSlot:
    """One planned slot. post_date is YYYY-MM-DD; category is the resolved content
    category (rotation slot with book/summit/welcome overrides applied); fmt is
    'feed' or 'story'; base_category is the underlying weekly-rotation category before
    any override (kept for audit, never invented). Pure data, no I/O."""
    post_date: str
    category: str
    fmt: str
    base_category: str = ""
    overridden: bool = False


# ---- category resolution (pure) --------------------------------------------------------

def _weekday_category(day_key):
    """The weekly-rotation category for a date, from content_categories._DAILY_SCHEDULE.
    Reads the schedule table directly (not schedule_for_day, which returns None while
    AGENT_CATEGORY_ROTATION is OFF) so the month plan is well defined regardless of that
    flag. Returns 'doctrine' for any weekday not in the table (defensive; all 7 are)."""
    abbr = _WEEKDAY_ABBR[date.fromisoformat(day_key).weekday()]
    entry = _cats._DAILY_SCHEDULE.get(abbr)
    return entry[0] if entry else "doctrine"


def _override_category(day_key, base_category, *, book_dates=None,
                       summit_day_fn=None, welcome_dates=None):
    """Resolve the day's category after folding in the book / summit / welcome overrides.

    Override precedence (a day only ever carries ONE category, so overrides are ordered):
      1. book-release-day content: a date that has a dated book_queue post (book_dates)
         becomes 'book'. This is REAL, pre written, dated content and takes its day.
      2. summit run-up: a date that is a summit day inside the campaign window
         (summit_day_fn(day_key) is True) becomes 'summit'.
      3. new-client welcome: a date flagged in welcome_dates becomes 'welcome'.
    None of these INVENT a day: the caller passes the real dated sets. When no override
    applies, the weekly-rotation base_category stands. Pure.

    Returns (category, overridden)."""
    if book_dates and day_key in book_dates:
        return "book", True
    if summit_day_fn is not None and summit_day_fn(day_key):
        return "summit", True
    if welcome_dates and day_key in welcome_dates:
        return "welcome", True
    return base_category, False


def _default_summit_day_fn(day_key):
    """The default summit-day predicate: the summit weekly slot inside the campaign
    window (through SUMMIT_END_DATE). Pure over config; the campaign flag is NOT read
    here (the PLAN is well defined regardless), only the fixed weekly slot + end date.
    A test injects its own predicate."""
    abbr = _WEEKDAY_ABBR[date.fromisoformat(day_key).weekday()]
    if abbr != config.SUMMIT_DAY:
        return False
    try:
        return date.fromisoformat(day_key) <= date.fromisoformat(config.SUMMIT_END_DATE)
    except ValueError:
        return False


def plan_month(account_key, start_date, days=30, *, book_dates=None,
               summit_day_fn=None, welcome_dates=None):
    """A deterministic month plan: for each of `days` consecutive dates from start_date,
    the resolved category (weekly rotation + book/summit/welcome overrides) and BOTH a
    feed slot and a paired story slot.

    PURE: no I/O, no Date.now, no writes. `start_date` is YYYY-MM-DD (a date or str).
    days <= 0 -> []. The book/summit/welcome inputs are injectable sets/predicates so
    the plan is fully deterministic and testable; the defaults use the real dated sets.

    Returns a flat list ordered date-ascending, feed then story within each date, so it
    always contains exactly 2 * max(days, 0) slots."""
    if days is None or days <= 0:
        return []
    start = start_date if isinstance(start_date, date) else date.fromisoformat(str(start_date))
    if summit_day_fn is None:
        summit_day_fn = _default_summit_day_fn
    book_dates = set(book_dates) if book_dates is not None else _default_book_dates()
    welcome_dates = set(welcome_dates or ())

    slots = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        base = _weekday_category(d)
        category, overridden = _override_category(
            d, base, book_dates=book_dates, summit_day_fn=summit_day_fn,
            welcome_dates=welcome_dates)
        slots.append(PlanSlot(post_date=d, category=category, fmt=FEED,
                              base_category=base, overridden=overridden))
        slots.append(PlanSlot(post_date=d, category=category, fmt=STORY,
                              base_category=base, overridden=overridden))
    return slots


def _default_book_dates():
    """The real dated book-release-day content dates from book_queue.BOOK_POSTS. Read
    lazily so importing this module never drags the book queue in; a missing or broken
    book queue degrades to no book overrides (never fabricated)."""
    try:
        from . import book_queue
        return {p["date"] for p in book_queue.BOOK_POSTS}
    except Exception:
        return set()


# ---- draft assembly (injectable builders; missing source is SKIPPED, never faked) -----

def build_month_drafts(plan, builders, *, story_builder=None, account=None,
                       logger=None):
    """For each slot in `plan`, produce a real Draft via the injected builder for that
    slot's category. Feed and story become SEPARATE Draft objects.

    `builders` is a map category -> feed_builder(account_or_key, day_key) -> Draft|None.
    The builders ARE the existing category draft builders (podcast_release,
    book_campaign, summit, daily_studio/rotation, welcome_queue, ...): this module never
    invents content, it only sequences the real ones. A builder that returns None (no
    approved source, studio dark, nothing queued) means that slot is SKIPPED and dropped
    from the calendar; it is NEVER replaced with fabricated copy.

    `story_builder(account_or_key, day_key, feed_draft) -> Draft|None` builds the paired
    9:16 story from the day's feed draft (the injected stories.py-style builder, which
    only ever emits a genuine 9:16 asset). A story slot with no feed draft for its date,
    or whose story_builder returns None, is SKIPPED (a story is never a cropped feed).

    PURE-ISH: no network here beyond whatever the injected builders do; no writes. Returns
    a flat list of real Draft objects (feed + story), skipped slots omitted."""
    log = logger or (lambda m: print(f"[real-month-planner] {m}"))
    target = account if account is not None else None

    # First pass: build feed drafts, keyed by date so the story pass can anchor to them.
    feed_by_date = {}
    drafts = []
    for slot in plan:
        if slot.fmt != FEED:
            continue
        builder = builders.get(slot.category)
        if builder is None:
            log(f"skip {slot.post_date} {slot.category} feed: no builder wired "
                f"for category {slot.category!r}")
            continue
        draft = _safe_call(builder, target, slot.post_date, log,
                           f"{slot.post_date} {slot.category} feed")
        if draft is None:
            log(f"skip {slot.post_date} {slot.category} feed: builder produced "
                "nothing (missing source/creative); not fabricated")
            continue
        draft = _stamp(draft, slot, FEED)
        feed_by_date[slot.post_date] = draft
        drafts.append(draft)

    # Second pass: build the paired story for each date that got a feed draft.
    for slot in plan:
        if slot.fmt != STORY:
            continue
        feed_draft = feed_by_date.get(slot.post_date)
        if feed_draft is None:
            log(f"skip {slot.post_date} {slot.category} story: no feed draft for "
                "the day to pair to")
            continue
        if story_builder is None:
            log(f"skip {slot.post_date} {slot.category} story: no story_builder wired")
            continue
        story = _safe_call_story(story_builder, target, slot.post_date, feed_draft,
                                 log, f"{slot.post_date} {slot.category} story")
        if story is None:
            log(f"skip {slot.post_date} {slot.category} story: no genuine 9:16 asset "
                "(never a cropped feed card)")
            continue
        story = _stamp(story, slot, STORY)
        drafts.append(story)
    return drafts


def _safe_call(builder, target, day_key, log, label):
    """Call a feed builder, tolerating either signature (account object or key). Any
    exception is logged and the slot is skipped (one bad slot never sinks the month)."""
    try:
        return builder(target, day_key)
    except Exception as exc:  # noqa: BLE001 - one bad slot must not fail the whole month
        log(f"skip {label}: builder raised {type(exc).__name__}: {exc}")
        return None


def _safe_call_story(story_builder, target, day_key, feed_draft, log, label):
    try:
        return story_builder(target, day_key, feed_draft)
    except Exception as exc:  # noqa: BLE001
        log(f"skip {label}: story builder raised {type(exc).__name__}: {exc}")
        return None


def _stamp(draft, slot, fmt):
    """Stamp the plan's category + format + date onto the draft so to_calendar_rows maps
    them straight through, WITHOUT inventing content. category is set to the slot's
    resolved category (the pillar the calendar shows); draft_type/is_story are set to the
    slot's format so a story is never mislabeled a feed. The draft's own caption /
    creative / platform are left exactly as the builder produced them."""
    try:
        draft.category = draft.category or slot.category
        draft.day_key = draft.day_key or slot.post_date
        if fmt == STORY:
            draft.is_story = True
            draft.draft_type = "story"
        else:
            # A feed slot: only set draft_type if the builder left it empty, and never
            # to "story". Some builders already tag draft_type (podcast/book/summit) with
            # the CATEGORY; leave those, they are not "story" and map to feed downstream.
            if not (draft.draft_type or "").strip():
                draft.draft_type = "feed"
    except Exception:
        pass  # a builder returning a non-Draft is already skipped upstream
    return draft


# ---- calendar rows (reuse the live mirror's mapping) -----------------------------------

def to_calendar_rows(drafts, account_key):
    """Map the real drafts to content_calendar rows (gym_id=account_key, account=platform,
    post_date, pillar=category, format feed|story, caption, image_url, status).

    Reuses real_calendar_mirror._real_row so the row shape is byte-identical to the live
    mirror (the portal reads exactly these keys). A draft with no resolvable post_date is
    dropped (it cannot sit on a calendar day). PURE: no I/O.

    NOTE: the format leg is derived by the mirror from is_story/draft_type, and pillar
    from the draft's category, both of which _stamp() set from the plan slot, so a feed
    row reads 'feed' and a story row reads 'story' with the plan's pillar."""
    rows = []
    for draft in drafts or []:
        row = _mirror._real_row(account_key, draft)
        if not row["post_date"]:
            continue
        # status is normalized to the portal vocabulary by the mirror; the planner's
        # freshly built drafts are PENDING, so this reads 'pending'.
        rows.append(row)
    return rows


# ---- apply: upsert real rows, delete ALL demo rows for the gym -------------------------

def _months_in_span(rows, extra_months=None):
    """The set of YYYY-MM months the planned rows land in, plus any extra months passed
    (the full planned span). Ordered for determinism."""
    months = {r["post_date"][:7] for r in rows if r.get("post_date")}
    for m in (extra_months or ()):
        months.add(m)
    return sorted(months)


def plan_span_months(start_date, days=30):
    """The YYYY-MM months a `days`-long span from start_date touches. Used so apply sweeps
    the WHOLE planned range for demo rows, not just months a real draft landed in (the
    month-range gap the mirror audit flagged). Pure."""
    if days is None or days <= 0:
        return []
    start = start_date if isinstance(start_date, date) else date.fromisoformat(str(start_date))
    months = set()
    for i in range(days):
        months.add((start + timedelta(days=i)).isoformat()[:7])
    return sorted(months)


def apply_month_plan(account_key, drafts, sb_store, *, span_months=None):
    """Upsert the real planned rows AND delete ALL demo rows for the gym, through the
    injectable SupabaseCalendarStore. GYM SCOPED: only ever touches account_key's rows.

    Refuses to run for the demo gym id (demo content's one valid home). Reconciles across
    every month the real rows land in PLUS the full planned span (`span_months`, from
    plan_span_months) so a demo row anywhere in the planned window is cleared, closing the
    month-range gap where the live mirror only swept months a real draft happened to land
    in. Every existing DEMO row for the gym (is_demo_draft_id) in those months is deleted;
    a real gym never keeps a demo id after apply.

    Writes calendar rows only. NOTHING here publishes. Returns a summary dict; never
    raises out (a store error is reported, not a partial silent failure)."""
    if not account_key or sb_store is None:
        return {"ok": False, "reason": "missing account_key or store",
                "upserted": 0, "deleted": 0}
    if account_key == config.demo_calendar_gym_id():
        return {"ok": False, "reason": "refusing to plan over the demo gym id",
                "upserted": 0, "deleted": 0}

    rows = [r for r in to_calendar_rows(drafts, account_key)
            # belt and braces: a planner row must never carry a demo id, and gym scope
            # is forced here too so a foreign gym_id can never be upserted.
            if not _mirror._demo.is_demo_draft_id(r.get("id"))
            and str(r.get("gym_id")) == str(account_key)]

    months = _months_in_span(rows, extra_months=span_months)

    # Read the gym's current rows across every month we reconcile, so we can find the
    # demo rows to delete (mirror._existing_demo_ids is gym-scoped + id-namespaced).
    existing = []
    seen_ids = set()
    try:
        for month in months:
            for r in (sb_store.list_month(account_key, month) or []):
                rid = r.get("id")
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                existing.append(r)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"store read failed: {type(exc).__name__}",
                "upserted": 0, "deleted": 0}

    delete_ids = _mirror._existing_demo_ids(account_key, existing)

    upserted = 0
    deleted = 0
    try:
        upsert = getattr(sb_store, "upsert_row", None)
        for row in rows:
            if upsert is not None:
                upsert(account_key, row)
                upserted += 1
        delete = getattr(sb_store, "delete_row", None)
        for rid in delete_ids:
            if delete is not None:
                delete(account_key, rid)
                deleted += 1
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"store write failed: {type(exc).__name__}",
                "upserted": upserted, "deleted": deleted}

    return {"ok": True, "upserted": upserted, "deleted": deleted,
            "delete_ids": list(delete_ids), "months": months}
