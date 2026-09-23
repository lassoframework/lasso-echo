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


def normalize_summit_daily(slots, enabled_fn):
    """Finalize the dated 2 regular + 1 Summit feed shape after editorial labels.

    Editorial and campaign overrides run first. On each enabled day, any Summit
    already occupying a regular or sprint slot is moved to cadence ordinal 2 and
    its original sprint identity/asset index is retained. The vacated regular
    slot is filled by a non-Summit editorial theme. Exactly two regular stories
    remain paired to ordinals 0/1; the additive Summit feed has no third story.
    """
    from collections import defaultdict

    by_day = defaultdict(list)
    day_order = []
    for slot in slots:
        if slot.post_date not in by_day:
            day_order.append(slot.post_date)
        by_day[slot.post_date].append(slot)

    result = []
    regular_pool = ("echo", "website", "podcast", "doctrine", "book")
    for day_key in day_order:
        day_slots = by_day[day_key]
        if not enabled_fn(day_key):
            result.extend(day_slots)
            continue

        feeds = [s for s in day_slots if s.fmt == "feed"]
        stories = [s for s in day_slots if s.fmt == "story"]
        # Prefer a real sprint/campaign Summit asset, then an editorial Summit,
        # then the provisional additive marker if an older planner supplied one.
        summit = next((s for s in feeds if s.is_sprint and s.category == "summit"), None)
        if summit is None:
            summit = next((s for s in feeds if s.category == "summit"), None)
        if summit is None:
            summit = next((s for s in feeds if s.summit_daily), None)
        if summit is None:
            prototype = feeds[0]
            summit = replace(prototype, category="summit", fmt="feed",
                             overridden=True, is_sprint=False, slot_index=0)
        summit = replace(summit, category="summit", fmt="feed", cadence_slot=2,
                         summit_daily=True, overridden=True)

        regular = [s for s in feeds if s is not summit and not s.is_sprint
                   and s.category != "summit" and not s.summit_daily]
        # Object identity is lost when `summit` is replaced above, so remove the
        # chosen source by its original scheduling identity as well.
        chosen_key = (getattr(summit, "is_sprint", False), summit.slot_index,
                      summit.category if summit.is_sprint else None)
        if summit.is_sprint:
            regular = [s for s in regular
                       if not (s.is_sprint and s.slot_index == chosen_key[1])]

        selected = {}
        for slot in regular:
            ordinal = slot.cadence_slot
            if ordinal in (0, 1) and ordinal not in selected:
                selected[ordinal] = slot
        used = {s.category for s in selected.values()}
        prototype = next(iter(selected.values()), feeds[0])
        weekday_pair = WEEK[date.fromisoformat(day_key).weekday()]
        for ordinal in (0, 1):
            if ordinal in selected:
                continue
            preferred = weekday_pair[ordinal]
            candidates = (preferred,) + regular_pool
            category = next((c for c in candidates
                             if c != "summit" and c not in used), "doctrine")
            selected[ordinal] = replace(
                prototype, category=category, fmt="feed", cadence_slot=ordinal,
                is_sprint=False, slot_index=0, summit_daily=False,
                overridden=True, video_preferred=category == "podcast")
            used.add(category)

        # A Summit that occupied a regular ordinal must be replaced there.
        for ordinal, slot in list(selected.items()):
            if slot.category == "summit" or slot.summit_daily:
                category = next(c for c in regular_pool if c not in used)
                selected[ordinal] = replace(
                    slot, category=category, is_sprint=False, slot_index=0,
                    summit_daily=False, overridden=True,
                    video_preferred=category == "podcast")
                used.add(category)

        story_by_ordinal = {s.cadence_slot: s for s in stories
                            if not s.is_sprint and s.cadence_slot in (0, 1)}
        story_proto = next(iter(story_by_ordinal.values()), stories[0] if stories else prototype)
        regular_stories = []
        for ordinal in (0, 1):
            feed = selected[ordinal]
            story = story_by_ordinal.get(ordinal, story_proto)
            regular_stories.append(replace(
                story, category=feed.category, fmt="story", cadence_slot=ordinal,
                is_sprint=False, slot_index=0, summit_daily=False,
                overridden=feed.overridden, video_preferred=feed.video_preferred))

        result.extend([selected[0], selected[1], summit])
        result.extend(regular_stories)
    return result


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
