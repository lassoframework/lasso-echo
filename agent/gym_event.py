"""
gym_event.py — self-serve Events & Promos (EVENT_CAMPAIGNS_BUILD.md).

An event IS an offer record: a gym_event row with a start/end window in the GYM'S
timezone. The window-timed campaign engine that already runs Summit plans a dated
ARC of content_calendar rows against ONE gym_event; LASSO Summit is just another
gym_event on gym_id='lasso'. There is NO second scheduler.

This module holds THREE pure layers plus the grounding gates; the arc rows are
inserted into the existing month plan by event_calendar.py (Wave 3), and the portal
form / API live in portal_events.py + intake_web.py (Wave 6).

  1. GymEvent — a validated, immutable view of one gym_event row.
  2. plan_arc(event, *, today) -> [ArcPost]: the deterministic dated arc for the
     event's type, scaled to the window + lead time, with short-notice degradation.
     Pure: no I/O, no Date.now (today is injected). This is the analogue of
     real_month_planner.plan_month — same discipline, generalized to any gym_event.
  3. draft_arc(event, arc, *, avatar=None, brief=None) -> [dict]: ground each arc
     post's copy in ONLY the event's own facts (name, dates, offer_text, link,
     brief) in brand voice, run copy_gate + one-ask, attach the per-gym avatar
     profile, and stamp event_id + category 'offer'/'event'. Recap posts are
     BLOCKED (drafted only) until real event media exists (never stock, never
     invented). Every row lands 'pending'.

HARD RAILS (never weakened, property-tested):
  * Copy uses ONLY the form's facts. No invented prices/deadlines/perks. A fact
    that is not in the event record never appears in a draft (fact_ok()).
  * copy_gate (no banned dashes) + one-ask + per-gym avatar on every draft.
  * The event's start/end gate publishing (event_calendar handles the sweep):
    ended/cancelled -> pending arc rows flip denied with reject_reason.
  * Dated posts fire in the GYM'S tz (scheduled_for uses event.tz, never UTC).
  * Recap drafts ONLY from real event media; blocked until media_ids is non-empty.
  * A provided link is verified at each publish (event_calendar / publish guard).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import copy_gate

# The event types the portal form offers (EVENT_CAMPAIGNS_BUILD.md §1).
EVENT_TYPES = (
    "bring_a_friend", "challenge", "open_house", "anniversary",
    "holiday_sale", "new_offer", "party",
)

# Arc category: event posts ride the calendar as 'offer'/'event'. They DISPLACE
# doctrine/education slots first (event_calendar), never other proof/offer posts.
ARC_CATEGORY = "offer"

# Arc post kinds, in canonical arc order. Each maps to a copy template grounded in
# the event facts.
ANNOUNCE = "announce"
HOW_IT_WORKS = "how_it_works"
LAST_CALL = "last_call"
DURING = "during"
FINAL_DAY = "final_day"
RECAP = "recap"

# The DEFAULT full-lead arc offsets, relative to the event window. Negative = days
# BEFORE starts_on; a during post is placed inside the window; positive = days AFTER
# ends_on. This is the "make it an A not noise" cadence from §2.
#   Announce T-7, How-it-works T-4, Last-call T-1, During xN (1 per 1-2 days),
#   Final day (ends_on), Recap T+2.
_ANNOUNCE_LEAD = 7
_HOWTO_LEAD = 4
_LASTCALL_LEAD = 1
_RECAP_TRAIL = 2
# During posts: one every _DURING_STEP days across the window (1-2 days apart).
_DURING_STEP = 2


@dataclass(frozen=True)
class ArcPost:
    """One planned arc post. kind is one of the ANNOUNCE.. RECAP constants;
    post_date is YYYY-MM-DD (already in the gym's calendar days); note is a portal
    explanation when the arc degraded (short notice / created-after-start). Pure
    data, no I/O. Mirrors real_month_planner.PlanSlot."""
    kind: str
    post_date: str
    note: str = ""


@dataclass(frozen=True)
class GymEvent:
    """A validated, immutable view of one gym_event row. Construction NORMALIZES
    and VALIDATES; a bad record raises ValueError (the portal API turns that into a
    400, never a silent bad arc). media_ids is a tuple (immutable)."""
    id: str
    gym_id: str
    name: str
    type: str
    starts_on: str          # YYYY-MM-DD
    ends_on: str            # YYYY-MM-DD
    tz: str                 # IANA tz, the GYM'S
    offer_text: str = ""
    link: str = ""
    brief: str = ""
    media_ids: tuple = ()
    status: str = "draft"
    created_by: str = ""

    def __post_init__(self):
        if not self.gym_id:
            raise ValueError("gym_event.gym_id is required")
        if not (self.name or "").strip():
            raise ValueError("gym_event.name is required")
        if self.type not in EVENT_TYPES:
            raise ValueError(f"gym_event.type {self.type!r} is not a known event type")
        s = _as_date(self.starts_on)
        e = _as_date(self.ends_on)
        if e < s:
            raise ValueError("gym_event.ends_on is before starts_on")
        # tz must be a real IANA zone (the arc fires in it; a bad tz would fall back
        # to UTC and post a day late — refuse it up front).
        try:
            ZoneInfo(self.tz)
        except Exception:
            raise ValueError(f"gym_event.tz {self.tz!r} is not a valid IANA timezone")
        if self.status not in ("draft", "scheduled", "live", "ended", "cancelled"):
            raise ValueError(f"gym_event.status {self.status!r} is invalid")

    @property
    def starts(self) -> date:
        return _as_date(self.starts_on)

    @property
    def ends(self) -> date:
        return _as_date(self.ends_on)

    @property
    def has_media(self) -> bool:
        """True when the event carries at least one real media pool asset. The recap
        post is BLOCKED until this is True (real event media only, never stock)."""
        return bool(self.media_ids)

    @classmethod
    def from_row(cls, row):
        """Build a GymEvent from a gym_event DB row dict (or the portal form's parsed
        payload). Tolerates media_ids as a list/tuple/None. Raises ValueError on a
        bad record (the caller maps that to a 400)."""
        row = dict(row or {})
        media = row.get("media_ids") or ()
        if isinstance(media, (list, tuple)):
            media = tuple(str(m) for m in media if str(m).strip())
        else:
            media = ()
        return cls(
            id=str(row.get("id") or ""),
            gym_id=str(row.get("gym_id") or ""),
            name=str(row.get("name") or ""),
            type=str(row.get("type") or ""),
            starts_on=str(row.get("starts_on") or ""),
            ends_on=str(row.get("ends_on") or ""),
            tz=str(row.get("tz") or ""),
            offer_text=str(row.get("offer_text") or ""),
            link=str(row.get("link") or ""),
            brief=str(row.get("brief") or ""),
            media_ids=media,
            status=str(row.get("status") or "draft"),
            created_by=str(row.get("created_by") or ""),
        )


def _as_date(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def make_event_id(gym_id, name, starts_on):
    """A stable gym_event id from (gym, name, start): a re-submit of the same event
    is idempotent (same id), a recurrence with a new date gets a new id. Slug-safe."""
    import hashlib
    import re
    base = re.sub(r"[^a-z0-9]+", "-", f"{name}".strip().lower()).strip("-")[:32]
    h = hashlib.sha1(f"{gym_id}|{name}|{starts_on}".encode()).hexdigest()[:10]
    return f"evt_{base or 'event'}_{h}"


# ---------------------------------------------------------------------------
# Layer 2: the arc planner (pure). plan_arc scaled to window + lead time.
# ---------------------------------------------------------------------------

def _during_dates(starts: date, ends: date):
    """The 'during' post dates across the window: the day AFTER starts_on (so the
    Announce/HowTo/LastCall pre-window run is distinct from the during run), then
    every _DURING_STEP days, never past ends_on, never ON the final day (the Final
    day post owns ends_on). Empty for a one-day event (final day carries it)."""
    out = []
    if ends <= starts:
        return out
    d = starts + timedelta(days=1)
    while d < ends:
        out.append(d)
        d += timedelta(days=_DURING_STEP)
    return out


def plan_arc(event: GymEvent, *, today=None):
    """The deterministic dated arc for `event`, scaled to its window + lead time.

    FULL LEAD (created >= _ANNOUNCE_LEAD days before start): the full arc —
    Announce T-7, How-it-works T-4, Last-call T-1, During xN (1 per 1-2 days),
    Final day (ends_on), Recap T+2.

    SHORT-NOTICE DEGRADATION (§2):
      * created within the lead window but before start: drop any pre-window post
        whose date is already in the past relative to `today`; if Announce would be
        past but the event has not started, MERGE announce + how-it-works into a
        single compressed pre-window post on the earliest still-future day (never a
        'next week' post for an event starting tomorrow), carrying a note.
      * created ON/AFTER start: during + final + recap only, with a portal note why
        (no announce/how-it-works/last-call — the pre-window is gone).

    Recap is always planned; it is BLOCKED at draft time until real media exists
    (draft_arc). Pure: `today` is injected (defaults to the gym-tz today only when
    None AND at a call site that wants live behavior; tests always inject it).

    Returns a list of ArcPost ordered date-ascending."""
    if today is None:
        today = _today_in_tz(event.tz)
    today = _as_date(today)
    s, e = event.starts, event.ends

    recap_date = (e + timedelta(days=_RECAP_TRAIL)).isoformat()
    during = [d.isoformat() for d in _during_dates(s, e)]
    final_date = e.isoformat()

    # CREATED ON/AFTER START -> during + final + recap only, with a note.
    if today >= s:
        posts = []
        note = ("Created after the event started, so the pre event run "
                "(announce, how it works, last call) is skipped.")
        # Only during dates that are still today-or-future.
        for d in during:
            if _as_date(d) >= today:
                posts.append(ArcPost(DURING, d, note=note if not posts else ""))
        if e >= today:
            posts.append(ArcPost(FINAL_DAY, final_date,
                                 note=note if not posts else ""))
        posts.append(ArcPost(RECAP, recap_date))
        return _ordered(posts)

    # PRE-WINDOW (created before start). Candidate pre-window posts by lead:
    announce_date = s - timedelta(days=_ANNOUNCE_LEAD)
    howto_date = s - timedelta(days=_HOWTO_LEAD)
    lastcall_date = s - timedelta(days=_LASTCALL_LEAD)

    posts = []
    announce_past = announce_date < today
    howto_past = howto_date < today

    if not announce_past:
        # Full lead: the standard pre-window run.
        posts.append(ArcPost(ANNOUNCE, announce_date.isoformat()))
        if not howto_past:
            posts.append(ArcPost(HOW_IT_WORKS, howto_date.isoformat()))
    else:
        # Short notice: Announce's day has passed but the event has not started.
        # MERGE announce + how-it-works into ONE compressed pre-window post on the
        # earliest still-future day (never a "next week" post for a near event).
        merged_day = max(howto_date, today)
        if merged_day < s:  # only if there is still a pre-window day left
            posts.append(ArcPost(
                ANNOUNCE, merged_day.isoformat(),
                note=("Short notice: the announce and how it works posts are "
                      "merged into one, and the T minus 7 announce is dropped.")))
        # If even the merged day would land on/after start, the pre-window is gone;
        # last-call below (if still future) carries the pre-window, else during does.

    # Last-call T-1, only if still in the future and not colliding with a placed post.
    if lastcall_date >= today and lastcall_date < s:
        _placed = {p.post_date for p in posts}
        if lastcall_date.isoformat() not in _placed:
            posts.append(ArcPost(LAST_CALL, lastcall_date.isoformat()))

    # During + final + recap.
    for d in during:
        posts.append(ArcPost(DURING, d))
    posts.append(ArcPost(FINAL_DAY, final_date))
    posts.append(ArcPost(RECAP, recap_date))
    return _ordered(posts)


def _ordered(posts):
    """Arc posts date-ascending, stable within a date by canonical arc order."""
    order = {ANNOUNCE: 0, HOW_IT_WORKS: 1, LAST_CALL: 2, DURING: 3,
             FINAL_DAY: 4, RECAP: 5}
    return sorted(posts, key=lambda p: (p.post_date, order.get(p.kind, 9)))


def _today_in_tz(tz):
    """Today's date in the given IANA tz (the gym's local calendar day). Used only
    when a caller does not inject `today`; tests always inject."""
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return datetime.utcnow().date()


# ---------------------------------------------------------------------------
# Story Studio hook (Wave 4). The event template offers a story render from the
# SAME record. The Story Studio render pipeline is built in parallel on
# feat/story-studio; we depend on its interface LOOSELY and STUB when it is not
# merged, so this build never duplicates that pipeline.
# ---------------------------------------------------------------------------

def story_studio_request(event: GymEvent):
    """The one-tap Story Studio render REQUEST for an event, drawn from the same
    record. Returns a dict the Story Studio pipeline consumes:
      {gym_id, event_id, kind: 'event', headline, sub, link, media_ids}
    grounded ONLY in the event facts. This is the loose INTERFACE the event template
    hands to Story Studio; the render itself lives in feat/story-studio and is NOT
    duplicated here. When that pipeline is merged its renderer reads exactly this
    shape; until then the request is a stub the approval queue can carry as a
    one-tap offer (no render is forced)."""
    return {
        "gym_id": event.gym_id,
        "event_id": event.id,
        "kind": "event",
        "headline": event.name,
        "sub": _pretty_dates(event),
        "offer_text": event.offer_text or "",
        "link": event.link or "",
        "media_ids": list(event.media_ids),
    }


def render_event_story(request, *, renderer=None):
    """Render an event story via the Story Studio pipeline when it is available, else
    return None (the hook is offered but no render is forced). `renderer` is injectable
    (the Story Studio entry point) so this is testable without that pipeline; the live
    path resolves it loosely and returns None if the module is not merged yet.

    HONEST STUB: this NEVER fabricates a story image. It either delegates to the real
    Story Studio renderer or returns None so the caller shows the one-tap offer without
    a broken/placeholder render."""
    if renderer is None:
        try:
            from . import story_studio as _ss  # feat/story-studio, may not be merged
            renderer = getattr(_ss, "render_from_request", None)
        except Exception:
            renderer = None
    if renderer is None:
        return None
    try:
        return renderer(request)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Layer 3: draft_arc — ground the copy in ONLY the event facts, gates, avatar.
# ---------------------------------------------------------------------------

# The publish/posting time in the GYM'S tz. A dated post fires here, never UTC
# (a UTC "TOMORROW!" post lands a day late for a Pacific gym).
DEFAULT_POST_TIME = "10:00"       # morning local; the recap photo request rides T+0 am


def scheduled_for(event: GymEvent, post_date, time_hhmm=DEFAULT_POST_TIME):
    """ISO datetime for an arc post in the EVENT'S (gym's) tz, DST-correct. This is
    the one place the arc's firing time is computed; it never uses config's global
    POSTING_TIMEZONE. e.g. a 10:00 post for a Los_Angeles gym reads ...-07:00/-08:00."""
    d = _as_date(post_date)
    hh, mm = time_hhmm.split(":")
    tz = ZoneInfo(event.tz)
    return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=tz).isoformat()


def _sentence(s):
    s = (s or "").strip()
    if not s:
        return ""
    if s[-1] not in ".!?":
        s = s + "."
    return s


def _pretty_dates(event: GymEvent):
    """A human date phrase from the event window, verbatim from the record (a FACT).
    'September 22' for a one-day event, 'September 22 to 28' for a range. Month name
    and day numbers are the only tokens, all present in the record."""
    s, e = event.starts, event.ends
    months = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")
    if s == e:
        return f"{months[s.month - 1]} {s.day}"
    if s.month == e.month:
        return f"{months[s.month - 1]} {s.day} to {e.day}"
    return f"{months[s.month - 1]} {s.day} to {months[e.month - 1]} {e.day}"


# Distinct DURING openers so several during posts never repeat the same caption
# (a duplicate caption is a consistency defect AND reads as spam). Each is a plain
# community/energy line carrying NO fact — the facts come from offer_text/brief,
# which stay grounded. `variant` indexes into this list, wrapping.
_DURING_OPENERS = (
    "{name} is on.",
    "Day is here for {name}.",
    "{name} is rolling.",
    "The energy at {name} is real.",
    "Still time to join {name}.",
)


def _body_for(kind, event: GymEvent, variant=0):
    """The grounded body lines for one arc kind, drawn ONLY from the event record
    (name, dates, offer_text, brief, link). No invented price/deadline/perk: every
    concrete claim is a token already in the record. `variant` distinguishes repeated
    DURING posts so they never share a caption. Returns a list of lines."""
    dates = _pretty_dates(event)
    offer = _sentence(event.offer_text)
    brief = (event.brief or "").strip()
    lines = []
    if kind == ANNOUNCE:
        lines.append(f"Mark it: {dates}.")
        if brief:
            lines.append(_sentence(brief))
        if offer:
            lines.append(offer)
    elif kind == HOW_IT_WORKS:
        lines.append(f"Here is how {event.name} works.")
        if offer:
            lines.append(offer)
        if brief:
            lines.append(_sentence(brief))
    elif kind == LAST_CALL:
        lines.append("Starts tomorrow.")
        if offer:
            lines.append(offer)
    elif kind == DURING:
        opener = _DURING_OPENERS[variant % len(_DURING_OPENERS)]
        lines.append(opener.format(name=event.name))
        if offer:
            lines.append(offer)
        if brief and variant % 2 == 0:
            lines.append(_sentence(brief))
    elif kind == FINAL_DAY:
        lines.append("Last day.")
        if offer:
            lines.append(offer)
    elif kind == RECAP:
        lines.append(f"That was {event.name}.")
        if brief:
            lines.append(_sentence(brief))
    return [ln for ln in lines if ln]


def _one_ask(kind, event: GymEvent):
    """The single call-to-action line (copy_gate.ASK_RE must match it). A recap
    asks nothing salesy; every pre/during post carries exactly one ask. Grounded:
    when the event has a link, the ask points at it; otherwise a generic 'DM us'."""
    if kind == RECAP:
        return ""
    if event.link:
        return f"Sign up: {event.link}"
    return "DM us to claim your spot."


def draft_copy(kind, event: GymEvent, *, avatar=None, variant=0):
    """The full grounded caption for one arc post: body lines (event facts only) +
    exactly one ask, run through copy_gate.scrub (no banned dashes). The per-gym
    avatar profile is threaded in as a trailing audience frame when provided (never
    invented; it is the gym's own approved avatar line — it carries NO numeric fact, so
    it never trips fact_ok). `variant` distinguishes repeated during posts. Returns the
    caption string.

    NO FABRICATION: every concrete token comes from the event record; fact_ok()
    (property-tested) asserts no number/price/date outside the record appears."""
    lines = []
    body = _body_for(kind, event, variant=variant)
    lines.extend(body)
    ask = _one_ask(kind, event)
    if ask:
        lines.append(ask)
    caption = "\n\n".join(_sentence(ln) if not ln.endswith((":", event.link or "\0"))
                          else ln for ln in lines if ln)
    # Per-gym avatar frame: a short audience line the gym approved, appended so the
    # copy speaks to the gym's people. Stripped of any digit run first so it can never
    # introduce a fact the event record lacks (avatar text is descriptive, not factual).
    av = _avatar_frame(avatar)
    if av:
        caption = f"{caption}\n\n{av}"
    # copy_gate: rewrite banned dashes, never reject. URLs pass through untouched.
    return copy_gate.scrub(caption)


def _avatar_frame(avatar):
    """A safe trailing audience line from the per-gym avatar profile. Any numeric run
    is stripped (an avatar like 'busy professionals 30 to 50' must not leak '30'/'50'
    into the copy as if they were event facts). Empty when no avatar is provided."""
    if not avatar:
        return ""
    txt = str(avatar).strip()
    if not txt:
        return ""
    # drop digit runs so the frame never carries a fact.
    clean = _re.sub(r"\d[\d,\.]*", "", txt).strip()
    clean = _re.sub(r"\s{2,}", " ", clean).strip(" ,")
    if not clean:
        return ""
    return f"For {clean}."


import re as _re

# Numeric / currency tokens that must trace back to the event record. A price,
# a deadline count, a "free for 7 days" — any digit run in a draft must appear in
# the record's own facts, or the draft invented it. A trailing '.' or ',' is
# sentence punctuation, NOT part of the number (else 'September 28.' would read as
# the token '28.' and never match the record's '28').
_FACT_NUM_RE = _re.compile(r"\$?\d[\d,\.]*%?")


def _num_tokens(text):
    """Numeric/currency tokens in `text`, each stripped of trailing sentence
    punctuation ('.'/',') and any trailing '.' inside a decimal kept only when a
    digit follows (so '$19' and '19.99' survive, 'September 28.' yields '28')."""
    out = []
    for raw in _FACT_NUM_RE.findall(text or ""):
        tok = raw.rstrip(".,")
        if tok:
            out.append(tok)
    return out


def _record_facts(event: GymEvent):
    """The set of literal numeric/currency tokens the event record contains. The
    date phrase's day numbers are derived from starts_on/ends_on, so they count as
    record facts too (they are the window, not an invention)."""
    corpus = " ".join([
        event.name, event.offer_text or "", event.brief or "",
        event.link or "", _pretty_dates(event),
        str(event.starts.day), str(event.ends.day),
        str(event.starts.month), str(event.ends.month),
        str(event.starts.year),
    ])
    return set(_num_tokens(corpus))


def fact_ok(caption, event: GymEvent):
    """The numeric/currency tokens in `caption` that do NOT trace to the event record
    (an empty list means the caption invented nothing). Property-tested across
    generated arcs. A caption with a number the record does not contain is a
    fabrication and must never publish. NOTE: the name is historical; it returns the
    OFFENDING tokens (falsy == clean), which reads naturally at every call site."""
    facts = _record_facts(event)
    return [t for t in _num_tokens(caption or "") if t not in facts]


def draft_arc(event: GymEvent, arc, *, avatar=None, actor="", logger=None):
    """Ground every arc post into a content_calendar-ready draft dict (the shape
    real_calendar_mirror._real_row emits, plus event_id + a recap-blocked flag).

    Each post's caption is grounded ONLY in the event facts (draft_copy), gated by
    copy_gate (no banned dashes) + one-ask + avatar, and fact-checked (fact_ok): a
    post whose copy would carry a fact absent from the record is a BUG and is dropped
    with a loud log (never published). The RECAP post is drafted but flagged
    recap_blocked=True until real event media exists (event.has_media); the caller
    (event_calendar) holds it out of the calendar until media arrives.

    Every row lands status 'pending' with event_id stamped. Pure: no I/O, no writes,
    no network. Returns a list of row dicts.

    `avatar` is the gym's approved avatar profile line (never invented); `actor` is
    stamped for audit by the caller, not here."""
    log = logger or (lambda m: print(f"[gym-event] {m}"))
    rows = []
    during_i = 0
    for post in arc:
        variant = 0
        if post.kind == DURING:
            variant = during_i
            during_i += 1
        caption = draft_copy(post.kind, event, avatar=avatar, variant=variant)
        # HARD RAIL: no banned dashes survive.
        if copy_gate.violations(caption):
            log(f"drop {event.id} {post.kind} {post.post_date}: copy_gate violation "
                f"{copy_gate.violations(caption)} (never published)")
            continue
        # HARD RAIL: one ask on every non-recap post.
        if post.kind != RECAP and not copy_gate.ASK_RE.search(caption):
            log(f"drop {event.id} {post.kind} {post.post_date}: no one-ask "
                "(never published)")
            continue
        # HARD RAIL: no fabricated facts. A number outside the record is a bug.
        bad = fact_ok(caption, event)
        if bad:
            log(f"drop {event.id} {post.kind} {post.post_date}: caption carries "
                f"facts not in the event record {bad} (never published, never guessed)")
            continue
        row = {
            "gym_id": event.gym_id,
            "account": "instagram",     # feed rides IG; event_calendar mirrors to FB
            "post_date": post.post_date,
            "pillar": ARC_CATEGORY,     # 'offer' — displaces doctrine/education first
            "format": "feed",
            "caption": caption,
            "image_url": "",            # media picked by event_calendar from the pool
            "status": "pending",        # RAIL: every arc row lands pending
            "event_id": event.id,
            "scheduled_at": scheduled_for(event, post.post_date),
            "arc_kind": post.kind,      # display + the recap/dead-link logic
        }
        if post.note:
            row["arc_note"] = post.note
        if post.kind == RECAP:
            # RAIL: recap drafts ONLY from real event media; blocked until it exists.
            row["recap_blocked"] = not event.has_media
        rows.append(row)
    return rows
