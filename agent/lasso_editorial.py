"""LASSO's two daily editorial themes, using approved source copy only."""
from dataclasses import replace
from datetime import date

# Three podcast posts in fourteen feed slots; Summit twice, book three times.
WEEK = (
    ("book", "echo"),
    ("podcast", "website"),
    ("book", "summit"),
    ("echo", "podcast"),
    ("website", "doctrine"),
    ("book", "summit"),
    ("doctrine", "podcast"),
)


def editorial_slots(slots):
    out = []
    for slot in slots:
        # Dated campaigns and welcome posts remain authoritative.
        if slot.is_sprint or (slot.overridden and slot.category in ("book", "welcome")
                              and slot.cadence_slot != 1):
            out.append(slot)
            continue
        cat = WEEK[date.fromisoformat(slot.post_date).weekday()][slot.cadence_slot or 0]
        from .config import SUMMIT_END_DATE
        if cat == "summit" and slot.post_date > SUMMIT_END_DATE:
            cat = "doctrine"
        out.append(replace(slot, category=cat, video_preferred=cat == "podcast"))
    # Sprint + varied pairs also need distinct AM/PM identities. The legacy
    # sprint planner leaves both cadence ordinals empty.
    from collections import Counter, defaultdict
    counts=Counter((s.post_date,s.fmt) for s in out)
    seen=defaultdict(int)
    normalized=[]
    for slot in out:
        key=(slot.post_date,slot.fmt)
        ordinal=seen[key]
        seen[key]+=1
        normalized.append(replace(slot,cadence_slot=ordinal) if counts[key]==2 else slot)
    return normalized


def source_pillar(category, day_key, doc):
    names = doc.pillars_with_copy()
    if category == "echo":
        pool = [n for n in names if n.startswith("Echo:")]
    elif category == "website":
        pool = [n for n in names if n.startswith("Websites:")]
    elif category == "book":
        pool = [n for n in names if n.startswith("Book:")]
    else:
        pool = [n for n in names if not n.startswith(("Echo:", "Websites:"))]
    day = date.fromisoformat(day_key)
    anchor = date(2026, 9, 14)
    weeks, rem = divmod((day - anchor).days, 7)
    per_week = sum(pair.count(category) for pair in WEEK)
    ordinal = weeks * max(1, per_week) + sum(pair.count(category) for pair in WEEK[:rem])
    return pool[ordinal % len(pool)] if pool else None
