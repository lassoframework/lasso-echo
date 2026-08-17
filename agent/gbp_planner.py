"""
GBP planner lane (Phase 3). Plans a month of Google Business posts for one gym from
its OWN library + sources, in GBP copy style, and writes them as PENDING rows to
content_calendar via the existing Echo write path (insert_rows). Nothing publishes;
the owner still taps Approve in the portal.

Reuses the FB/IG machinery WITHOUT modifying it: client_content source rotation +
pick_image for the photo, gbp.crop_4x3 for the 1200x900 crop, and a GBP-specific LLM
caption (different system prompt: 80-char hook, city, no hashtags, no phone, CTA carries
the ask). Every caption clears gbp.caption_issues (A+) or the slot is skipped — A+ or
nothing, never a sub-par GBP post.

Offer/CTA source (discovered 2026-08-15): the live front-end offer NAME comes from
onboarding_intake.offers (jsonb array); the redeem/CTA URL from onboarding_intake
.ghl_link (the GHL funnel). There is NO CTA-type override column and NO coupon / offer-
window / terms source — CTA defaults to LEARN_MORE and the offer window is a planner
default (7-14 days, validator cap 30). See PROGRESS.md for the flagged gaps. Never
fabricate an offer: no offer name or no redeem URL -> the OFFER slot is skipped.
"""

import os
from datetime import date, timedelta

from . import client_content, client_sources, config, gbp, media_host, rotation
from .content_categories import filter_platform_copy
from .drafter import _call_llm_caption, _output_claims_cleared

# §5.1 cadence per connected location per month
CADENCE = {"STANDARD": 8, "OFFER": 1, "EVENT_MAX": 2, "PHOTO": 4}
OFFER_WINDOW_DAYS = 10          # planner default within the 7-14 band (validator cap 30)

GBP_SYSTEM = (
    "You are a local-SEO copywriter for a boutique gym. Write ONE Google Business "
    "Profile post caption for people searching 'gym near me' on Google Search and Maps "
    "(strangers, not followers).\n"
    "HARD RULES:\n"
    "- The first 80 characters carry the whole message: lead with the outcome AND the "
    "city. Google truncates there in Search.\n"
    "- 150 to 300 characters total.\n"
    "- Name the city or neighborhood once, naturally.\n"
    "- NO hashtags. NO phone numbers (a CALL button handles that). NO emojis spam.\n"
    "- No em dashes, en dashes, or hyphens used as dashes.\n"
    "- Draw ONLY from the brand voice doc and the fact provided. Invent nothing: no "
    "stats, prices, offers, or claims not in the sources.\n"
    "- Do NOT write a CTA or a link in the body; the CTA button is separate.\n"
    "- Output ONLY the caption text. No labels, no headers, no quotes."
)


def generate_gbp_caption(fact_text, voice, city):
    """A GBP-style caption grounded in one approved fact + the gym's voice doc, city
    named. Returns a caption that PASSES gbp.caption_issues(city), or None when the LLM
    is unavailable / the output cannot be made A+ (caller skips the slot: A+ or nothing).
    Figure-fabrication gated exactly like the FB/IG SB7 path."""
    user = (f"BRAND VOICE DOC:\n{getattr(voice, 'raw', '') or ''}\n\n"
            f"CITY: {city}\n\nTODAY'S FACT (the only source of specifics):\n{fact_text}\n\n"
            "Write the GBP caption now.")
    try:
        from .drafter import _strip_llm_scaffold
        cap = _strip_llm_scaffold(_call_llm_caption(GBP_SYSTEM, user) or "")
        cap = filter_platform_copy(cap).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[gbp-planner] caption LLM failed: {type(exc).__name__}")
        return None
    if not cap:
        return None
    if not _output_claims_cleared(cap, voice, fact_text):
        print("[gbp-planner] caption carried an unapproved figure; skipping slot")
        return None
    if gbp.caption_issues(cap, city=city):
        print(f"[gbp-planner] caption not A+ ({gbp.caption_issues(cap, city=city)[0]}); "
              "skipping slot")
        return None
    return cap


def resolve_offer(offers_json, ghl_link):
    """(offer_name, offer_dict) from the gym record, or (None, None) to SKIP the OFFER
    slot. offer_dict is the content_calendar.gbp_offer payload (redeemOnlineUrl only —
    coupon/terms have no source and are omitted, never invented). Requires BOTH a real
    offer name AND a redeem URL; either missing -> skip (never a dead offer)."""
    def _name_of(item):
        # a jsonb offer element may be a plain string or an object; pull a real name
        # field from an object, never stringify the dict into the caption.
        if isinstance(item, dict):
            return str(item.get("name") or item.get("title") or item.get("label")
                       or "").strip()
        return str(item or "").strip()

    name = ""
    if isinstance(offers_json, list) and offers_json:
        name = _name_of(offers_json[0])
    elif isinstance(offers_json, str):
        name = offers_json.strip()
    url = (ghl_link or "").strip()
    if not name or not url:
        return None, None
    return name, {"redeemOnlineUrl": url}


def _offer_window(start):
    """gbp_event.schedule offer window: OFFER_WINDOW_DAYS from `start`."""
    end = start + timedelta(days=OFFER_WINDOW_DAYS)
    return {"schedule": {"startDate": start.isoformat(), "endDate": end.isoformat()}}


def _cropped_image_url(account_key, image, day_key):
    """Crop the picked library photo to 1200x900 at PLANNING time, host it, return the
    hosted url (the exact pixels the owner approves + that publish). None on failure."""
    try:
        cache = os.path.join(rotation._cache_dir(None) if hasattr(rotation, "_cache_dir")
                             else "/tmp", "gbp_crops")
    except Exception:
        cache = "/tmp/gbp_crops"
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, f"{account_key}_{os.path.basename(image.path)}_gbp.jpg")
    try:
        gbp.crop_4x3(image.path, out)
    except Exception as exc:  # noqa: BLE001
        print(f"[gbp-planner] crop failed for {image.path}: {type(exc).__name__}")
        return None
    if config.hosting_enabled():
        hosted = media_host.host_media(out, account_key)
        if hosted:
            return hosted
    return None


def _row(portal_gym_key, account_gen_key, day_key, caption, image_url, *,
         topic_type, pillar, cta_type=gbp.DEFAULT_CTA, cta_url="",
         event=None, offer=None, gbp_location_id=None, fmt="update",
         status="pending"):
    """One content_calendar GBP row dict (no id; DB mints it). account is the literal
    'googlebusiness'; gym_id is the portal_gym_key canonical join. status is 'pending'
    (owner-visible) normally, or 'coach_review' (withheld from the owner) for a gym's
    first month under GATE 2."""
    row = {
        "gym_id": portal_gym_key,
        "account": gbp.PLATFORM,             # 'googlebusiness'
        "post_date": day_key,
        "pillar": pillar,
        "format": fmt,                        # update | event | offer | photo
        "caption": caption,
        "image_url": image_url,
        "status": status,
        "gbp_topic_type": topic_type,
    }
    if topic_type != "OFFER":
        row["gbp_cta_type"] = cta_type
        row["gbp_cta_url"] = cta_url
    if event is not None:
        row["gbp_event"] = event
    if offer is not None:
        row["gbp_offer"] = offer
    if gbp_location_id:
        row["gbp_location_id"] = gbp_location_id
    return row


def plan_gbp_month(portal_gym_key, account_gen_key, *, voice, library_path, city,
                   store, start=None, days=30, offer=None, events=(),
                   gbp_location_id=None, cta_url="", caption_fn=None, image_fn=None,
                   facts=None, offer_confirmed=False, initial_status="pending",
                   logger=None):
    """Plan one GBP month for a gym and WRITE it as PENDING rows (Echo write path).

    Cadence (§5.1): 8 STANDARD + 1 OFFER (if a real offer resolves) + up to 2 EVENT
    (only real events passed in) + 4 photo drops. Every STANDARD/EVENT caption must
    clear the A+ gate or its slot is SKIPPED (A+ or nothing). One distinct photo per
    post (no reuse), cropped to 1200x900 at plan time. Returns
    {ok, planned, standard, offer, event, photo, skipped}.

    offer: (name, offer_dict) from resolve_offer, or None. events: list of
    {title, schedule, fact} dicts (real, from the gym record). caption_fn/image_fn are
    injectable for tests; production uses generate_gbp_caption + client_content.pick_image
    + crop-and-host.

    facts: OPTIONAL list of (pillar, fact_text) tuples — a bespoke, already-approved fact
    source (e.g. LASSO's own lasso_now.md copy bank) for a tenant whose content does NOT
    live in the client_sources pipeline. When given, the STANDARD loop draws its facts
    from this list (cycled) instead of client_sources, and satisfies the presence guard.
    It is REAL approved material only; every A+ / figure / no-dash gate still runs on the
    generated caption, so no gate is weakened and nothing is fabricated.

    GATE 1 offer_confirmed: the OFFER slot is planned ONLY when this is True AND a real
    offer resolves. A gym whose live offer is not confirmed gets NO OFFER post (a wrong
    offer to Google is a failure we cannot eat). Local updates / events / photo drops are
    unaffected. GATE 2 initial_status: the status every planned row is written with —
    'pending' (owner-visible) normally, or 'coach_review' (withheld from the owner until a
    coach screens and releases it) for a gym's first month."""
    log = logger or (lambda m: print(f"[gbp-planner] {m}"))
    start = start or date.today()
    caption_fn = caption_fn or (lambda fact: generate_gbp_caption(fact, voice, city))
    facts = list(facts or [])
    present = client_sources.categories_present(account_gen_key)
    if not present and not facts and not offer and not events:
        return {"ok": False, "reason": "no approved sources / facts / offer / events",
                "planned": 0}

    used = set()
    rows = []
    counts = {"standard": 0, "offer": 0, "event": 0, "photo": 0, "skipped": 0}

    def _image_url(day_key):
        if image_fn is not None:
            return image_fn(day_key, used)
        img = client_content.pick_image(account_gen_key, day_key, library_path,
                                        exclude_keys=used)
        if img is None:
            return None
        key = os.path.basename(img.path)
        url = _cropped_image_url(account_gen_key, img, day_key)
        if url:
            used.add(key)
        return url

    # ---- 8 STANDARD (2/week ~ every 3-4 days) ----
    day = start
    fact_i = 0
    while counts["standard"] < CADENCE["STANDARD"] and (day - start).days < days:
        if facts:
            pillar, fact = facts[fact_i % len(facts)]
            fact_i += 1
        else:
            cat = client_content.category_for_day(account_gen_key, day.isoformat(),
                                                  present) if present else None
            src = client_content._source_for_day(account_gen_key, day.isoformat(), cat,
                                                  present) if cat else None
            fact = getattr(src, "text", "") if src else ""
            pillar = cat or "update"
        img_url = _image_url(day.isoformat()) if fact else None
        cap = caption_fn(fact) if (fact and img_url) else None
        if cap and img_url:
            rows.append(_row(portal_gym_key, account_gen_key, day.isoformat(), cap,
                             img_url, topic_type="STANDARD", pillar=pillar,
                             cta_type=gbp.DEFAULT_CTA, cta_url=cta_url,
                             gbp_location_id=gbp_location_id, fmt="update",
                             status=initial_status))
            counts["standard"] += 1
        else:
            counts["skipped"] += 1
        day += timedelta(days=3)

    # ---- 1 OFFER (GATE 1: only when a real offer + redeem url resolved AND confirmed) ----
    if offer and offer[0] and offer[1] and offer_confirmed:
        oname, odict = offer
        od = day.isoformat()
        img_url = _image_url(od)
        cap = caption_fn(f"Our current offer: {oname}") if img_url else None
        if cap and img_url:
            rows.append(_row(portal_gym_key, account_gen_key, od, cap, img_url,
                             topic_type="OFFER", pillar="offer", offer=odict,
                             event=_offer_window(day), gbp_location_id=gbp_location_id,
                             fmt="offer", status=initial_status))
            counts["offer"] += 1
        else:
            counts["skipped"] += 1
        day += timedelta(days=2)
    elif offer and offer[0] and offer[1] and not offer_confirmed:
        log(f"{portal_gym_key}: offer '{offer[0]}' resolved but NOT confirmed -> OFFER "
            "slot skipped (GATE 1: offer-only-when-confirmed)")

    # ---- 0-2 EVENT (real events only) ----
    for ev in list(events)[:CADENCE["EVENT_MAX"]]:
        ed = day.isoformat()
        img_url = _image_url(ed)
        cap = caption_fn(ev.get("fact") or ev.get("title") or "") if img_url else None
        if cap and img_url and ev.get("schedule"):
            rows.append(_row(portal_gym_key, account_gen_key, ed, cap, img_url,
                             topic_type="EVENT", pillar="event", cta_type=gbp.DEFAULT_CTA,
                             cta_url=cta_url,
                             event={"title": ev.get("title") or "", "schedule": ev["schedule"]},
                             gbp_location_id=gbp_location_id, fmt="event",
                             status=initial_status))
            counts["event"] += 1
        else:
            counts["skipped"] += 1
        day += timedelta(days=2)

    # ---- 4 PHOTO drops (gallery uploads; image only, no caption gate) ----
    pday = start + timedelta(days=1)
    while counts["photo"] < CADENCE["PHOTO"] and (pday - start).days < days:
        img_url = _image_url(pday.isoformat())
        if img_url:
            rows.append(_row(portal_gym_key, account_gen_key, pday.isoformat(),
                             "", img_url, topic_type="STANDARD", pillar="photo",
                             gbp_location_id=gbp_location_id, fmt="photo",
                             status=initial_status))
            counts["photo"] += 1
        pday += timedelta(days=7)

    if not rows:
        return {"ok": False, "reason": "nothing planned (no A+ captions or media)",
                "planned": 0, **counts}

    if not hasattr(store, "insert_rows"):
        # never report a phantom success: a store that cannot persist means 0 rows landed
        return {"ok": False, "reason": "store cannot persist rows (no insert_rows)",
                "planned": 0, **counts}
    inserted = store.insert_rows(portal_gym_key, rows)
    log(f"{portal_gym_key}: planned {len(rows)} GBP rows "
        f"(std {counts['standard']}, offer {counts['offer']}, event {counts['event']}, "
        f"photo {counts['photo']}, skipped {counts['skipped']})")
    return {"ok": True, "planned": len(inserted) or len(rows), **counts}
