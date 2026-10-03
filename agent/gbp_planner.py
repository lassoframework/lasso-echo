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
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import client_content, client_sources, config, gbp, gym_media_selector, media_host, rotation
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
    from .drafter import _strip_llm_scaffold

    def _attempt(user_prompt):
        try:
            cap = _strip_llm_scaffold(_call_llm_caption(GBP_SYSTEM, user_prompt) or "")
            return filter_platform_copy(cap).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"[gbp-planner] caption LLM failed: {type(exc).__name__}")
            return None

    voice_raw = getattr(voice, 'raw', '') or ''
    base_user = (f"BRAND VOICE DOC:\n{voice_raw}\n\n"
                 f"CITY: {city}\n\nTODAY'S FACT (the only source of specifics):\n{fact_text}\n\n"
                 "Write the GBP caption now.")
    cap = _attempt(base_user)
    if cap and gbp.caption_issues(cap, city=city):
        # City not named — retry once with an explicit reminder
        retry_user = (base_user +
                      f"\n\nIMPORTANT: Your previous attempt did not name the city. "
                      f"The caption MUST include '{city}' to rank on Google Maps. "
                      "Start the caption with the city or include it in the first sentence.")
        cap = _attempt(retry_user)

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
    # CROP CACHE (2026-09-02): re-cropping a photo whose 1200x900 output is already on
    # disk and newer than its source produces byte-identical pixels, so skip the work.
    # This matters now that the mirror crops one photo per FEED POST on every build
    # (~268 fleet-wide) rather than a handful per planned month. A source edited after
    # its crop still re-crops, because the mtime comparison fails.
    try:
        fresh = (os.path.isfile(out) and os.path.getsize(out) > 0
                 and os.path.getmtime(out) >= os.path.getmtime(image.path))
    except OSError:
        fresh = False
    if not fresh:
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


def _drive_photo_candidate(account_key, day_key, used_ids):
    """Materialize one unused approved Drive photo without consuming it yet.

    The selector owns tenant isolation, review eligibility, and global once-used
    enforcement. The caller stamps usage only after its row is durably inserted.
    """
    if str(account_key or "").startswith("lasso"):
        return None
    if not (config.gym_drive_stage_enabled()
            and config.gym_drive_connect_active_for(account_key)):
        return None
    try:
        from . import gym_media_index, gym_media_selector
        from .integrations.drive_client import DriveClient
        media_store = gym_media_index.default_store()
        drive = DriveClient()
        if not media_store.available() or not drive.available():
            return {"hold": True}
        base = gym_media_selector.base_gym_key(account_key)
        list_sources = getattr(media_store, "list_sources", None)
        if not callable(list_sources):
            return {"hold": True}
        sources = list_sources(base) or []
        ready = [s for s in sources
                 if str(s.get("kind") or "") == "gym_drive"
                 and s.get("active") is not False
                 and not s.get("revoked_externally")
                 and str(s.get("sync_status") or "").lower() == "ready"
                 and s.get("sync_finished_at")]
        if not ready:
            return {"hold": True}
        assets = media_store.list_assets(base)
        class Snapshot:
            def available(self):
                return True
            def list_assets(self, _base):
                return assets
        asset = gym_media_selector.pick_media(
            base, kind_preference="photo", store=Snapshot(),
            exclude_ids=tuple(used_ids))
        if asset is None:
            return None
        with tempfile.TemporaryDirectory(prefix="gbp_drive_") as work:
            raw = Path(work) / os.path.basename(
                asset.get("title") or f"{asset['id']}.jpg")
            drive.download(asset["id"], raw)
            source = raw
            if gym_media_index.needs_rendition(asset):
                rendition_url, _ = gym_media_index.ensure_rendition(
                    asset, raw, store=media_store)
                if not rendition_url:
                    return None
                source = Path(work) / f"{raw.stem}.jpg"
                gym_media_index.heic_to_jpeg(raw, source)

            class DrivePhoto:
                path = str(source)
                media_type = "image"

            url = _cropped_image_url(account_key, DrivePhoto(), day_key)
        if not url:
            return None
        used_ids.add(str(asset["id"]))
        return {"url": url, "kind": "drive", "asset": asset,
                "base": base, "store": media_store, "day_key": day_key}
    except Exception as exc:  # noqa: BLE001 - GBP falls through to local media
        print(f"[gbp-planner] Drive photo failed for {account_key} on {day_key}: "
              f"{type(exc).__name__}")
        return {"hold": True}


def _drive_claim_id(base, asset):
    from . import gym_media_selector
    return gym_media_selector.drive_content_claim_id(base, asset)


def _claim_drive_pick(pick, account_key):
    """Atomically reserve one Drive asset across concurrent GBP planners."""
    from . import gym_media_selector
    try:
        claim_id = gym_media_selector.claim_drive_content(
            pick["base"], pick["asset"], pick["store"])
    except Exception:
        return False
    if claim_id is None:
        return False
    claim_account = f"{pick['base']}_gbp"
    pick["claim_id"] = claim_id
    pick["claim_account"] = claim_account
    return True


def _release_drive_claim(pick):
    claim_id = (pick or {}).get("claim_id")
    if not claim_id:
        return True
    from . import db
    db.socialapi_claim_release(claim_id, pick["claim_account"])
    pick.pop("claim_id", None)
    pick.pop("claim_account", None)
    return True


def _complete_drive_claim(pick):
    from . import db
    db.socialapi_claim_done(
        pick["claim_id"], pick["claim_account"], str(pick["asset"]["id"]))


def _calendar_row_key(row):
    return (str((row or {}).get("post_date") or ""),
            str((row or {}).get("format") or ""),
            str((row or {}).get("image_url") or ""),
            str((row or {}).get("account") or ""))


def _readback_inserted_rows(store, portal_gym_key, proposed):
    """Return exact landed rows, [] for proven-none, or None when unreadable.

    Supabase may commit an insert and lose the HTTP response. A normal month
    listing is not absence proof: it is active-row filtered and has no exact
    result count. Use either an explicitly authoritative keyed reader (tests or
    another store implementation) or one count=exact query per proposed row.
    Missing counts, malformed responses, and read failures are uncertainty.
    """
    authoritative = getattr(store, "authoritative_rows_for_keys", None)
    if callable(authoritative):
        try:
            rows = authoritative(portal_gym_key, proposed)
        except Exception:  # noqa: BLE001
            return None
        return None if rows is None else list(rows)

    base = getattr(store, "_s", store)
    client_fn = getattr(base, "_client", None)
    rest_fn = getattr(base, "_rest", None)
    headers_fn = getattr(base, "_headers", None)
    if not all(callable(fn) for fn in (client_fn, rest_fn, headers_fn)):
        return None
    try:
        visible = []
        for row in proposed:
            response = client_fn().get(
                rest_fn("content_calendar"),
                params={
                    "gym_id": f"eq.{portal_gym_key}",
                    "account": f"eq.{row.get('account') or ''}",
                    "post_date": f"eq.{row.get('post_date') or ''}",
                    "format": f"eq.{row.get('format') or ''}",
                    "image_url": f"eq.{row.get('image_url') or ''}",
                    "select": "*",
                },
                headers=headers_fn({"Prefer": "count=exact"}), timeout=30)
            if response.status_code >= 400:
                return None
            content_range = str(response.headers.get("content-range") or "")
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except (IndexError, ValueError):
                return None
            found = response.json() or []
            if total == 0:
                if found:
                    return None
                continue
            matches = [item for item in found
                       if _calendar_row_key(item) == _calendar_row_key(row)]
            if not matches:
                return None
            visible.extend(matches)
    except Exception:  # noqa: BLE001 - uncertainty is a first-class result
        return None
    return visible


def _alert_drive_claim_hold(portal_gym_key, picks, reason):
    """Emit an explicit operational hold while atomic claims prevent re-offer."""
    ids = [str(p.get("asset", {}).get("id") or "") for p in picks]
    ids = [asset_id for asset_id in ids if asset_id]
    if not ids:
        return
    try:
        from . import ops_alerts
        ops_alerts.alert(
            f"GBP media claim HOLD for {portal_gym_key}: {', '.join(ids)}; {reason}. "
            "Claims remain in-flight so these assets cannot be offered again; "
            "reconcile the calendar row and media usage receipt before release.")
    except Exception as exc:  # noqa: BLE001 - the claim itself remains the safety belt
        print(f"[gbp-planner] claim hold alert failed for {portal_gym_key}: "
              f"{type(exc).__name__}")


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


# ---- B9: an empty GBP month must name what is MISSING ---------------------------
# ENG, 2026-09: the month sweep failed for four gyms with the single line "nothing
# planned (no A+ captions or media)". That sentence is true and useless -- it names
# two possible causes and tells the gym which of them applies for neither. The planner
# already knows: every skipped slot has exactly one reason. This turns the ledger into
# one sentence a gym owner can act on today.
#
# GBP KEEPS ITS OWN BAR. The A+ caption gate is NOT relaxed and no threshold is lowered
# here: a slot that cannot clear the gate is still skipped, and nothing is ever
# fabricated to fill it. The only thing that changes is what we SAY about the empty
# month. "Give GBP its own threshold" was the alternative; naming the gap is the honest
# half of that choice, and it is the half that does not put weaker copy in front of
# Google.
_SKIP_LANGUAGE = (
    ("no_media", "add photos (connect the gym's Drive folder or upload in the portal)"),
    ("no_fact", "add approved content sources (the intake form's about / offers / "
                "events sections are empty)"),
    ("caption_rejected", "the drafted captions did not clear the quality gate"),
    ("no_event_schedule", "the events on file have no schedule, so they cannot be posted"),
)


def empty_month_reason(skips, counts=None):
    """One sentence naming the LARGEST real cause of an empty GBP month, in the gym's
    own language. Falls back to the historical wording only when the ledger is empty
    (a month with no slots attempted at all), so no caller loses its reason string."""
    skips = dict(skips or {})
    ranked = [(n, name, text) for name, text in _SKIP_LANGUAGE
              for n in (int(skips.get(name) or 0),) if n > 0]
    if not ranked:
        return "nothing planned (no A+ captions or media)"
    ranked.sort(key=lambda r: (-r[0], r[1]))
    top_n, _top_name, top_text = ranked[0]
    total = sum(n for n, _nm, _t in ranked)
    rest = ""
    if len(ranked) > 1:
        rest = " Also skipped for: " + ", ".join(
            f"{t} ({n})" for n, _nm, t in ranked[1:]) + "."
    return (f"nothing planned: {total} Google Business slot(s) were skipped. "
            f"The biggest gap is {top_n} slot(s) where {top_text}." + rest)


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
    used_drive_ids = set()
    used_drive_hashes = set()
    global_local_hold = [False]
    rows = []
    media_claims = []
    counts = {"standard": 0, "offer": 0, "event": 0, "photo": 0, "skipped": 0}
    # B9 (ENG, month sweep): a bare "nothing planned (no A+ captions or media)"
    # told four gyms nothing they could act on. Every skipped slot now records WHY,
    # so an empty month names the ONE thing the gym has to fix. Counting only; no
    # slot is filled differently and nothing is fabricated to fill one.
    skips = {"no_fact": 0, "no_media": 0, "caption_rejected": 0,
             "no_event_schedule": 0}

    # GBP uses its own rotation namespace (_gbp suffix) so that IG/FB calendar
    # placement history doesn't block photos from appearing on Google Business.
    # _vision_base recognises the _gbp suffix, so vision scoring still applies.
    _gbp_rotation_key = account_gen_key.replace("_ig", "_gbp").replace("_fb", "_gbp")

    def _image_pick(day_key, pillar=None):
        if image_fn is not None:
            url = image_fn(day_key, used)
            return {"url": url, "kind": "injected", "day_key": day_key} if url else None
        drive_pick = _drive_photo_candidate(
            account_gen_key, day_key, used_drive_ids)
        if drive_pick and drive_pick.get("hold"):
            return None
        if drive_pick:
            if drive_pick.get("kind") == "drive":
                from . import gym_media_selector as _media_selector
                digest = _media_selector._byte_hash(drive_pick["asset"])
                if not digest or digest in used_drive_hashes:
                    return None
                try:
                    aliases = drive_pick["store"].list_assets(drive_pick["base"])
                    used_drive_ids.update(
                        str(a["id"]) for a in aliases
                        if _media_selector._byte_hash(a) == digest
                        and str(a.get("gym_id")) == drive_pick["base"])
                except Exception:
                    return None
                used_drive_hashes.add(digest)
            return drive_pick
        # §4: pass the slot pillar so vision content-scores the pick (no-op for non-vision).
        try:
            img = client_content.pick_image(_gbp_rotation_key, day_key, library_path,
                                            exclude_keys=used, pillar=pillar,
                                            prefer_photos=True)
        except client_content.LocalPhotoGlobalLedgerUnavailable:
            global_local_hold[0] = True
            log(f"{day_key}: held GBP local-photo pick; global byte history is unreadable")
            return None
        if img is None:
            return None
        if getattr(img, "media_type", "") == "video":
            return None
        key = os.path.basename(img.path)
        url = _cropped_image_url(account_gen_key, img, day_key)
        if url:
            used.add(key)
            from . import dam
            return {"url": url, "kind": "local", "day_key": day_key,
                    "rotation_key": dam.rotation_key(img.path), "pillar": pillar,
                    "path": img.path}
        return None

    def _accept(row, pick):
        rows.append(row)
        media_claims.append((row, pick))

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
        pick = _image_pick(day.isoformat(), pillar=pillar) if fact else None
        img_url = pick["url"] if pick else None
        cap = caption_fn(fact) if (fact and img_url) else None
        if cap and img_url:
            _accept(_row(portal_gym_key, account_gen_key, day.isoformat(), cap,
                         img_url, topic_type="STANDARD", pillar=pillar,
                         cta_type=gbp.DEFAULT_CTA, cta_url=cta_url,
                         gbp_location_id=gbp_location_id, fmt="update",
                         status=initial_status), pick)
            counts["standard"] += 1
        else:
            counts["skipped"] += 1
            if not fact:
                skips["no_fact"] += 1
            elif not img_url:
                skips["no_media"] += 1
            else:
                skips["caption_rejected"] += 1
        day += timedelta(days=3)

    # ---- 1 OFFER (GATE 1: only when a real offer + redeem url resolved AND confirmed) ----
    if offer and offer[0] and offer[1] and offer_confirmed:
        oname, odict = offer
        od = day.isoformat()
        pick = _image_pick(od, pillar="offer")
        img_url = pick["url"] if pick else None
        cap = caption_fn(f"Our current offer: {oname}") if img_url else None
        if cap and img_url:
            _accept(_row(portal_gym_key, account_gen_key, od, cap, img_url,
                         topic_type="OFFER", pillar="offer", offer=odict,
                         event=_offer_window(day), gbp_location_id=gbp_location_id,
                         fmt="offer", status=initial_status), pick)
            counts["offer"] += 1
        else:
            counts["skipped"] += 1
            if not img_url:
                skips["no_media"] += 1
            else:
                skips["caption_rejected"] += 1
        day += timedelta(days=2)
    elif offer and offer[0] and offer[1] and not offer_confirmed:
        log(f"{portal_gym_key}: offer '{offer[0]}' resolved but NOT confirmed -> OFFER "
            "slot skipped (GATE 1: offer-only-when-confirmed)")

    # ---- 0-2 EVENT (real events only) ----
    for ev in list(events)[:CADENCE["EVENT_MAX"]]:
        ed = day.isoformat()
        pick = _image_pick(ed, pillar="event")
        img_url = pick["url"] if pick else None
        cap = caption_fn(ev.get("fact") or ev.get("title") or "") if img_url else None
        if cap and img_url and ev.get("schedule"):
            _accept(_row(portal_gym_key, account_gen_key, ed, cap, img_url,
                         topic_type="EVENT", pillar="event", cta_type=gbp.DEFAULT_CTA,
                         cta_url=cta_url,
                         event={"title": ev.get("title") or "", "schedule": ev["schedule"]},
                         gbp_location_id=gbp_location_id, fmt="event",
                         status=initial_status), pick)
            counts["event"] += 1
        else:
            counts["skipped"] += 1
            if not img_url:
                skips["no_media"] += 1
            elif not ev.get("schedule"):
                skips["no_event_schedule"] += 1
            else:
                skips["caption_rejected"] += 1
        day += timedelta(days=2)

    # ---- 4 PHOTO drops (gallery uploads; image only, no caption gate) ----
    pday = start + timedelta(days=1)
    while counts["photo"] < CADENCE["PHOTO"] and (pday - start).days < days:
        pick = _image_pick(pday.isoformat(), pillar="photo")
        img_url = pick["url"] if pick else None
        if img_url:
            _accept(_row(portal_gym_key, account_gen_key, pday.isoformat(),
                         "", img_url, topic_type="STANDARD", pillar="photo",
                         gbp_location_id=gbp_location_id, fmt="photo",
                         status=initial_status), pick)
            counts["photo"] += 1
        pday += timedelta(days=7)

    if global_local_hold[0]:
        # Do not partially write a GBP plan after a local photo tier became
        # unprovable. A later rerun with a readable global ledger is the only
        # safe way to decide those slots.
        return {"ok": False, "reason": "global local-photo history unreadable",
                "planned": 0, "skips": dict(skips), **counts}

    if not rows:
        return {"ok": False, "reason": empty_month_reason(skips, counts),
                "planned": 0, "skips": dict(skips), **counts}

    if not hasattr(store, "insert_rows"):
        # never report a phantom success: a store that cannot persist means 0 rows landed
        return {"ok": False, "reason": "store cannot persist rows (no insert_rows)",
                "planned": 0, **counts}
    # Claim Drive assets atomically across concurrent planners. This is a
    # reservation only: usage is still stamped after the row insert succeeds.
    drive_claims = []
    for _row_obj, pick in media_claims:
        if pick.get("kind") != "drive":
            continue
        if not _claim_drive_pick(pick, _gbp_rotation_key):
            for prior in drive_claims:
                _release_drive_claim(prior)
            return {"ok": False, "reason": "Drive photo already claimed",
                    "planned": 0, "skips": dict(skips), **counts}
        drive_claims.append(pick)

    # Reserve local photos before the batch write, then release the exact
    # reservation for every row that does not land. A successful run keeps the
    # durable record so every later platform/planner run sees the photo consumed.
    local_reservations = {}
    stamp_failures = []
    claim_receipt_failures = []
    insert_readback_recovered = False
    insert_readback_complete = False
    for row, pick in media_claims:
        if pick.get("kind") != "local":
            continue
        rid = rotation.reserve_local_photo_once(
            _gbp_rotation_key, pick.get("rotation_key"),
            pick.get("pillar") or "photo", pick["day_key"], path=pick["path"])
        if rid is None:
            for prior in local_reservations.values():
                rotation.release_served(prior)
            for prior in drive_claims:
                _release_drive_claim(prior)
            return {"ok": False, "reason": "local photo reservation failed",
                    "planned": 0, "skips": dict(skips), **counts}
        local_reservations[id(row)] = rid
    try:
        inserted = store.insert_rows(portal_gym_key, rows) or []
    except Exception as exc:
        readback = _readback_inserted_rows(store, portal_gym_key, rows)
        if readback is None:
            # The remote may have committed. Releasing would make landed media
            # immediately reofferable, so retain all claims/reservations.
            _alert_drive_claim_hold(
                portal_gym_key, drive_claims,
                f"insert raised {type(exc).__name__} and authoritative readback failed")
            raise
        if not readback:
            # Even an exact zero can race a late POST commit. The exception
            # leaves the write outcome unknown, so every claim stays held.
            _alert_drive_claim_hold(
                portal_gym_key, drive_claims,
                f"insert raised {type(exc).__name__}; immediate readback was empty")
            raise
        # The response was lost after at least one row committed. Stamp the
        # visible rows, but keep claims for every unseen row: a late commit can
        # still follow this readback.
        inserted = readback
        insert_readback_recovered = True
        insert_readback_complete = (
            {_calendar_row_key(r) for r in inserted}
            == {_calendar_row_key(r) for r in rows})
    inserted_keys = {_calendar_row_key(r) for r in inserted}
    for row, pick in media_claims:
        landed = _calendar_row_key(row) in inserted_keys
        if pick.get("kind") == "local" and not landed and not insert_readback_recovered:
            rotation.release_served(local_reservations.get(id(row)))
        if pick.get("kind") == "drive" and not landed and not insert_readback_recovered:
            _release_drive_claim(pick)
        if pick.get("kind") == "drive" and landed:
            # A transient usage write gets one bounded retry. Never stamp a
            # caption-rejected or unpersisted candidate. stamp_use spans the
            # media store and its kv receipt, so inspect the asset after an
            # exception before retrying to avoid a double increment when only
            # the receipt write failed.
            original_count = int(pick["asset"].get("used_count") or 0)
            stamped = False
            last_exc = None
            for attempt in range(2):
                try:
                    from . import gym_media_selector
                    gym_media_selector.stamp_use(
                        pick["asset"], pick["base"], pick["day_key"],
                        store=pick["store"])
                    stamped = True
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    try:
                        fresh = next(
                            (a for a in pick["store"].list_assets(pick["base"])
                             if str(a.get("id")) == str(pick["asset"].get("id"))),
                            None)
                        if fresh and int(fresh.get("used_count") or 0) > original_count:
                            stamped = True
                            break
                    except Exception:  # noqa: BLE001 - safe bounded retry below
                        pass
            if not stamped:
                log(f"{portal_gym_key}: durable Drive usage stamp failed for "
                    f"{pick['asset'].get('id')} after insert: "
                    f"{type(last_exc).__name__}: {last_exc}")
                stamp_failures.append(str(pick["asset"].get("id") or ""))
            else:
                try:
                    _complete_drive_claim(pick)
                except Exception as exc:  # noqa: BLE001
                    # The atomic claim remains in-flight, so the asset is still
                    # non-reofferable even if its completion receipt fails.
                    log(f"{portal_gym_key}: Drive claim completion failed for "
                        f"{pick['asset'].get('id')}: {type(exc).__name__}")
                    claim_receipt_failures.append(
                        str(pick["asset"].get("id") or ""))
    if not inserted:
        return {"ok": False, "reason": "store inserted zero rows",
                "planned": 0, "skips": dict(skips), **counts}
    if stamp_failures:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and str(pick.get("asset", {}).get("id") or "") in stamp_failures],
            "calendar row is durable but the media usage receipt could not be stamped")
        return {"ok": False,
                "reason": "durable rows inserted but Drive usage stamp failed: "
                          + ", ".join(stamp_failures),
                "planned": len(inserted), "operational_hold": True,
                "claim_ids": [p.get("claim_id") for p in drive_claims
                              if p.get("claim_id")],
                "skips": dict(skips), **counts}
    if claim_receipt_failures:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and str(pick.get("asset", {}).get("id") or "")
             in claim_receipt_failures],
            "media usage was stamped but the completed-claim receipt failed")
        return {"ok": False,
                "reason": "Drive usage stamped but claim receipt remains in-flight: "
                          + ", ".join(claim_receipt_failures),
                "planned": len(inserted), "operational_hold": True,
                "claim_ids": [p.get("claim_id") for p in drive_claims
                              if p.get("claim_id")],
                "skips": dict(skips), **counts}
    if insert_readback_recovered and not insert_readback_complete:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and _calendar_row_key(row) not in inserted_keys],
            "insert response failed; unseen rows may commit after readback")
        return {"ok": False,
                "reason": "insert response failed; partial durable rows reconciled by readback",
                "planned": len(inserted), "operational_hold": True,
                "skips": dict(skips), **counts}
    log(f"{portal_gym_key}: planned {len(rows)} GBP rows "
        f"(std {counts['standard']}, offer {counts['offer']}, event {counts['event']}, "
        f"photo {counts['photo']}, skipped {counts['skipped']})")
    return {"ok": True, "planned": len(inserted) or len(rows),
            "skips": dict(skips), **counts}
