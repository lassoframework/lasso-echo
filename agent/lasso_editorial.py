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
    # Explicit user artwork dates take precedence over the baseline mix.
    from .lasso_campaign_assets import MANIFEST
    import json
    manifest=json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    entries=manifest.get('assets',[])
    overrides=manifest.get('category_overrides',{})
    by_slot={(e['date'],e['slot_index']):e for e in entries}
    for i,slot in enumerate(normalized):
        e=by_slot.get((slot.post_date,slot.cadence_slot))
        replacement=overrides.get(slot.post_date)
        if not e and replacement and slot.category=='book':
            normalized[i]=replace(slot,category=replacement,video_preferred=False)
        if e and bool(e.get('is_sprint'))==slot.is_sprint:
            normalized[i]=replace(slot,category=e['category'],video_preferred=False)
    for day in {s.post_date for s in normalized}:
        for fmt in ('feed','story'):
            ix=[i for i,s in enumerate(normalized) if s.post_date==day and s.fmt==fmt]
            if len(ix)==2 and normalized[ix[0]].category==normalized[ix[1]].category:
                for i in ix:
                    if (day,normalized[i].cadence_slot) not in by_slot:
                        normalized[i]=replace(normalized[i],category='doctrine',video_preferred=False)
                        break
    return normalized


def source_pillar(category, day_key, doc):
    names = doc.pillars_with_copy()
    import json
    from .lasso_campaign_assets import MANIFEST
    explicit=(json.loads(MANIFEST.read_text()).get('pillar_overrides',{}).get(day_key)
              if MANIFEST.exists() else None)
    if explicit and explicit in names and (category=='doctrine' or explicit.startswith(('Websites:' if category=='website' else category.title()+':'))):
        return explicit
    if category == "echo":
        pool = [n for n in names if n.startswith("Echo:")]
    elif category == "website":
        pool = [n for n in names if n.startswith("Websites:")]
    elif category == "summit":
        pool = [n for n in names if n.startswith("Summit:")]
        dates=refresh_dates()
        if day_key in dates and pool:
            return pool[dates.index(day_key) % len(pool)]
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


def refresh_dates():
    import json
    from .lasso_campaign_assets import MANIFEST
    return json.loads(MANIFEST.read_text()).get('refresh_dates',[]) if MANIFEST.exists() else []


def editorial_caption(draft):
    """Format sourced copy and give Echo/site explainers the approved call path."""
    import re
    caption=draft.caption
    if draft.category in ('echo','website') and draft.source_fragments:
        fragments=draft.source_fragments
        subject='Echo for your gym' if draft.category=='echo' else 'your gym website'
        caption=fragments[0]+'\n\n'+' '.join(fragments[1:])
        # LASSO Brain website-kb.md section 4: site-wide Growth Call CTA.
        caption+='\n\nBook a call to talk about '+subject+': https://lassoframework.com/growth-call'
        if draft.hashtags:
            caption+='\n\n'+' '.join(draft.hashtags)
    elif '\n' not in caption:
        parts=re.split(r'(?<=[.!?])\s+',caption,maxsplit=1)
        caption='\n\n'.join(parts)
    return caption
