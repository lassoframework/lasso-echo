"""
onboarding_demo.py — a SAMPLE month a brand-new gym sees on day one.

THE PROBLEM: a gym signs, opens its portal, and sees nothing. Echo cannot build a real
month until the gym's social intake produces APPROVED SOURCES, and it must never invent
facts to fill the gap. So a correctly-behaving Echo shows a new client an empty calendar
for days — which reads as "this thing is broken". Five gyms were sitting in exactly that
state on 2026-08-30 (hillcountry, theboltonclub, crossfitlocal, zanshinfitness630e22,
district_h: zero approved AND zero pending sources).

THE ANSWER: seed a clearly-labelled SAMPLE month so the client can see the SHAPE of what
they bought (the cadence, the pillar rotation, a feed and a paired story each day, the
voice) while their real intake lands. Samples are replaced by real content the moment a
genuine month builds.

HARD RAILS (this module exists inside the no-fabrication gate, not around it):
  * A sample caption makes ZERO factual claims about the gym. No price, no offer, no
    stat, no member name, no class time, no promise. It describes what WILL go here.
    That is how this stays inside "no invented facts": it invents none.
  * Every sample row is marked TWICE — pillar='sample' AND a 'SAMPLE: ' caption prefix —
    so it is unmistakable to the client, queryable by ops, and detectable by the
    publisher.
  * A sample row can NEVER publish. calendar_autopublish skips is_sample_row() before
    any claim, regardless of status, autonomy or approval. Marking alone is not trusted.
  * Samples NEVER overwrite real content: seeding refuses if the gym already has any
    non-sample row, and clearing only ever deletes sample rows.
  * Behind AGENT_ONBOARDING_DEMO, default OFF.
"""

from datetime import date, timedelta

from . import config

# A sample row is marked in BOTH of these ways. Either one identifies it; we write both
# so a client reading the portal and a query reading the table agree, and so a caption
# edit can never accidentally un-mark a row the publisher must skip.
SAMPLE_PILLAR = "sample"
SAMPLE_PREFIX = "SAMPLE: "

# The pillar rotation a real month uses, so the sample shows the actual shape of the
# product rather than a flat week of the same thing.
#
# EVERY ENTRY MUST BE DISTINCT, and there must be at least as many as the default day
# count: insert_rows runs a never-verbatim-twice caption belt, so a repeated caption is
# DROPPED at stage time. With a six-entry bank a fourteen-day sample landed six feed
# days and fourteen story days (stories are exempt from that belt) — a lopsided
# calendar that looked broken. Keep this list >= the default `days`.
_SHAPE = (
    ("what your members are proud of",
     "A member win goes here, in your words, from what you tell us."),
    ("who you are",
     "The story of your gym goes here, written from your own intake."),
    ("what you offer",
     "Your real offer goes here, exactly as you describe it to us."),
    ("a face from your floor",
     "One of your coaches or members goes here, from a photo you upload."),
    ("why people stay",
     "What keeps your community together goes here, in your voice."),
    ("the result someone got",
     "A real result goes here once you have shared it with us."),
    ("the thing beginners worry about",
     "The question new members ask you most goes here, answered your way."),
    ("what a first visit is like",
     "How someone's first session actually goes here, as you run it."),
    ("the coach behind the programme",
     "A coach introduction goes here, from what you tell us about them."),
    ("a moment from the floor",
     "Something that happened in your gym this week goes here."),
    ("who this is really for",
     "The people you built this gym for go here, described in your words."),
    ("the habit that changes things",
     "The one thing you tell members to focus on goes here."),
    ("what people say after a month",
     "A member's own words go here, once you have shared them with us."),
    ("the invitation",
     "Your real call to come in goes here, worded the way you say it."),
)

# One per day, so a client does not scroll fourteen identical story cards. Stories are
# exempt from the caption belt, but a repeated card still reads as broken.
_STORY_LINES = (
    "The paired story for this day goes here.",
    "A behind the scenes moment goes here.",
    "A quick word from a coach goes here.",
    "A look at today's session goes here.",
    "A member shout out goes here.",
    "A reminder of what is on this week goes here.",
    "Something from your floor goes here.",
)


def enabled():
    return config.onboarding_demo_enabled()


def is_sample_row(row):
    """True when a content_calendar row is a seeded SAMPLE. Checks BOTH markers so a
    row stays detectable if a caption is edited or a pillar is re-written."""
    if not row:
        return False
    if str(row.get("pillar") or "").strip().lower() == SAMPLE_PILLAR:
        return True
    return str(row.get("caption") or "").lstrip().startswith(SAMPLE_PREFIX)


def build_rows(base_key, *, days=14, start=None, account="instagram",
               mirror_facebook=True, image_for_day=None):
    """The sample rows for one gym. Pure: no I/O, so the shape is fully testable.

    image_for_day(day_index) -> url or "" lets the caller attach the gym's OWN photos
    when it has any (a sample month that shows their real gym is far more useful). It
    is optional: with no image the row still shows the cadence and the copy.
    """
    start = start or date.today()
    rows = []
    for i in range(max(0, int(days))):
        day = (start + timedelta(days=i)).isoformat()
        headline, body = _SHAPE[i % len(_SHAPE)]
        img = ""
        if callable(image_for_day):
            try:
                img = image_for_day(i) or ""
            except Exception:  # noqa: BLE001 - a sample never fails on media
                img = ""
        feed_caption = (f"{SAMPLE_PREFIX}This is where {headline} will go.\n\n{body}\n\n"
                        "This is a sample so you can see your calendar before your "
                        "content is ready. It will be replaced by your real post.")
        rows.append({
            "gym_id": base_key,
            "account": account,
            "post_date": day,
            "pillar": SAMPLE_PILLAR,
            "format": "feed",
            "caption": feed_caption,
            "image_url": img,
            "status": "draft",
        })
        if mirror_facebook:
            rows.append({**rows[-1], "account": "facebook"})
        rows.append({
            "gym_id": base_key,
            "account": account,
            "post_date": day,
            "pillar": SAMPLE_PILLAR,
            "format": "story",
            "caption": (f"{SAMPLE_PREFIX}"
                        f"{_STORY_LINES[i % len(_STORY_LINES)]}"),
            "image_url": img,
            "status": "draft",
        })
    return rows


def seed(base_key, *, days=14, store=None, start=None, image_for_day=None, log=None):
    """Seed a sample month for a gym that has NOTHING yet. Returns a summary dict.

    REFUSES when the gym already has any NON-sample row: a sample must never sit beside
    or on top of real content. Re-seeding an already-sampled gym is a no-op, so the
    frequent scan can call this every pass."""
    log = log or (lambda m: print(f"[onboarding-demo] {m}"))
    if not enabled():
        return {"ok": False, "reason": "flag off", "seeded": 0}
    if not base_key:
        return {"ok": False, "reason": "no gym", "seeded": 0}
    if store is None:
        return {"ok": False, "reason": "no store", "seeded": 0}
    existing = _existing_rows(store, base_key)
    if existing is None:
        return {"ok": False, "reason": "calendar unreadable", "seeded": 0}
    real = [r for r in existing if not is_sample_row(r)]
    if real:
        return {"ok": True, "reason": "gym already has real content", "seeded": 0}
    if existing:
        return {"ok": True, "reason": "already sampled", "seeded": 0}
    rows = build_rows(base_key, days=days, start=start, image_for_day=image_for_day)
    try:
        written = store.insert_rows(base_key, rows) or []
    except Exception as exc:  # noqa: BLE001 - a demo must never sink the scan
        log(f"{base_key}: sample seed failed ({type(exc).__name__})")
        return {"ok": False, "reason": f"insert failed ({type(exc).__name__})",
                "seeded": 0}
    log(f"{base_key}: seeded {len(written)} SAMPLE row(s) so the portal is not empty "
        "while intake lands (none of them can publish)")
    return {"ok": True, "reason": "", "seeded": len(written)}


def clear(base_key, *, store=None, log=None):
    """Delete this gym's SAMPLE rows. Called the moment real content is about to land,
    so a client never sees a sample next to a real post. Only ever removes rows that
    is_sample_row() identifies; a real row is never touched."""
    log = log or (lambda m: print(f"[onboarding-demo] {m}"))
    if not base_key or store is None:
        return 0
    existing = _existing_rows(store, base_key)
    if not existing:
        return 0
    ids = [r.get("id") for r in existing if is_sample_row(r) and r.get("id")]
    if not ids:
        return 0
    deleter = getattr(store, "delete_rows", None)
    removed = 0
    if callable(deleter):
        try:
            removed = int(deleter(base_key, ids) or 0)
        except Exception as exc:  # noqa: BLE001
            log(f"{base_key}: sample clear failed ({type(exc).__name__})")
            return 0
    if removed:
        log(f"{base_key}: cleared {removed} sample row(s); real content is taking over")
    return removed


def _existing_rows(store, base_key):
    """Every current+forward row for the gym, or None when the calendar is unreadable
    (which must NEVER be read as 'the gym is empty' — that would seed samples on top of
    a real calendar)."""
    reader = getattr(store, "list_month", None)
    if not callable(reader):
        return None
    today = date.today()
    months = sorted({(today + timedelta(days=d)).strftime("%Y-%m")
                     for d in (0, 15, 30, 45)})
    out = []
    for month in months:
        try:
            out.extend(reader(base_key, month) or [])
        except Exception:  # noqa: BLE001 - unreadable: refuse to guess
            return None
    return out
