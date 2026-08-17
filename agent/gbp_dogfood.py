"""
GBP dogfood entrypoint (Phase 3 close-out). Plans ONE gym's full spec-cadence GBP
month from its OWN real material and writes it to content_calendar as PENDING rows.
Nothing publishes; the owner still taps Approve, and Connect binds the location.

WHY a separate entrypoint (not the FB/IG scan lane): the GBP rail dogfoods on ONE gym
(LASSO) first before it fans out, so this is gym-scoped and flag-gated. It reuses the
Phase 3 planner (gbp_planner.plan_gbp_month) unchanged — same A+ gate, same crop, same
cadence — and only adds the real-material RESOLUTION (voice, library, city, offer,
connection) plus an idempotency guard.

Hard limits (unchanged): zero live publishes. Rows land status='pending'. The OFFER
slot is planned only when a REAL offer resolves from the gym record (never fabricated);
LASSO has no onboarding offer row, so its OFFER slot is skipped by design. The image
crop hosts to R2 via the planner's own path, so this MUST run where R2 hosting is
configured (the deployed worker); a host-less environment yields no image_url and the
planner plans nothing rather than writing broken rows.
"""

import os
import sys
from datetime import date

from . import config, gbp, gbp_planner
from .gbp_planner import resolve_offer


def _base_of(gym_key):
    k = (gym_key or "").strip()
    for suffix in ("_ig", "_fb"):
        if k.endswith(suffix):
            return k[: -len(suffix)]
    return k


def _resolve_offer_for(portal_gym_key, clients):
    """(name, offer_dict) from onboarding_intake.offers + ghl_link for this gym, or
    (None, None) to SKIP the OFFER slot. Never fabricates: no gym record, or no offer
    name / redeem url, -> skip. `clients` is an object exposing onboarding_intake(gym_key)
    -> {offers, ghl_link} or None (injected in tests; production reads Supabase)."""
    if clients is None:
        return None, None
    try:
        rec = clients.onboarding_intake(portal_gym_key)
    except Exception:  # noqa: BLE001
        return None, None
    if not rec:
        return None, None
    return resolve_offer(rec.get("offers"), rec.get("ghl_link"))


def _resolve_connection_location(portal_gym_key, store):
    """The gym's connected gbp_location_id, or None when it is not connected yet. A None
    location is fine at PLAN time: the worker resolves the connection by gym at publish
    time (after the owner clicks Connect), so the month can be planned before Connect."""
    try:
        conns = store.connections_for(portal_gym_key)
    except Exception:  # noqa: BLE001
        return None, ""
    for c in conns or []:
        if str(c.get("status", "")).lower() == "connected" and c.get("gbp_location_id"):
            return c["gbp_location_id"], (c.get("ghl_link") or "")
    return None, ""


def _connection_status(portal_gym_key, store):
    """The gym's GBP connection posture (G4): 'connected' if ANY connection is connected,
    else 'needs_reconnect' if any is needs_reconnect, else 'none' (never connected). A
    'needs_reconnect'-only gym must NOT be planned — the queue would fill with posts that
    cannot publish. 'none' still plans (by design: rows plan before Connect, the worker
    binds the single connection at publish). Read failure -> 'none' (never blocks on a
    flaky read; the worker holds needs_reconnect at publish as the backstop)."""
    try:
        conns = store.connections_for(portal_gym_key) or []
    except Exception:  # noqa: BLE001
        return "none"
    statuses = {str(c.get("status", "")).lower() for c in conns}
    if "connected" in statuses:
        return "connected"
    if "needs_reconnect" in statuses:
        return "needs_reconnect"
    return "none"


def plan_gbp_dogfood(portal_gym_key, account_gen_key, *, voice, library_path, city,
                     store, offer=None, cta_url="", gbp_location_id=None, days=30,
                     start=None, events=(), caption_fn=None, image_fn=None,
                     facts=None, offer_confirmed=False, initial_status="pending",
                     connection_status="none", now=None, logger=None):
    """Idempotently plan + write one gym's PENDING GBP month. Skips (no-op) when the gym
    already has googlebusiness rows dated on/after `start` (never a duplicate month).
    Blocks (does not fabricate) when voice is missing, and PAUSES (G4) when the gym's GBP
    connection is 'needs_reconnect'. Returns the planner result dict (plus
    {'skipped_existing': True} on the idempotency no-op)."""
    log = logger or (lambda m: print(f"[gbp-dogfood] {m}"))
    start = start or (now or date.today())
    # G4: never plan for a gym whose connection needs reconnecting — the posts could not
    # publish and the queue would fill with unpublishable rows.
    if connection_status == "needs_reconnect":
        log(f"{portal_gym_key}: GBP connection needs_reconnect -> planner PAUSED "
            "(nothing written; reconnect, then re-run)")
        return {"ok": False, "reason": "connection needs_reconnect", "planned": 0}
    if voice is None:
        log(f"{portal_gym_key}: voice doc missing -> BLOCK (no fabrication)")
        return {"ok": False, "reason": "voice doc missing", "planned": 0}
    # idempotency: a future GBP month already present -> leave untouched. A read FAILURE
    # must NOT be swallowed into "absent" — that would re-plan and double-write the month
    # (insert_rows has no upsert). On a failed read we abort WITHOUT writing.
    try:
        existing = store.future_gbp_rows(portal_gym_key, start.isoformat())
    except Exception as exc:  # noqa: BLE001
        log(f"{portal_gym_key}: idempotency read failed ({type(exc).__name__}); "
            "aborting without write (no duplicate month)")
        return {"ok": False, "reason": "idempotency read failed", "planned": 0}
    if existing:
        log(f"{portal_gym_key}: {len(existing)} future GBP rows already present -> skip")
        return {"ok": True, "planned": 0, "skipped_existing": True}
    return gbp_planner.plan_gbp_month(
        portal_gym_key, account_gen_key, voice=voice, library_path=library_path,
        city=city, store=store, start=start, days=days, offer=offer, events=events,
        gbp_location_id=gbp_location_id, cta_url=cta_url, caption_fn=caption_fn,
        image_fn=image_fn, facts=facts, offer_confirmed=offer_confirmed,
        initial_status=initial_status, logger=log)


# ---- LASSO bespoke material (its content is not in the client_sources pipeline) -----

# LASSO's own GBP destination for the LEARN_MORE button. Its real website; UTM is added
# by the planner. Override with the 3rd CLI arg / cta_url= if a specific funnel is wanted.
LASSO_DEFAULT_CTA_URL = "https://lassoframework.com"


def _repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lasso_facts(now_doc_path=None):
    """Real, already-approved (pillar, fact) pairs parsed from brand_voice/lasso_now.md —
    the pillar copy bank (Hook + Body) and the verified proof-point receipts. 100% real
    approved material; the planner's A+ / figure / no-dash gates still run on every
    generated caption. Returns [] if the doc is missing (caller then plans nothing rather
    than inventing)."""
    import os
    import re
    path = now_doc_path or os.path.join(_repo_root(), "brand_voice", "lasso_now.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return []
    facts = []
    # pillar blocks: '### Pillar: NAME' then Hook:/Body: lines until the next heading
    for m in re.finditer(r"###\s*Pillar:\s*(.+?)\n(.*?)(?=\n#|\Z)", raw, re.DOTALL):
        pillar = m.group(1).strip()
        parts = []
        for line in m.group(2).splitlines():
            mm = re.match(r"\s*(?:Hook|Body)\s*:\s*(.+)", line)
            if mm:
                parts.append(mm.group(1).strip())
        if parts:
            facts.append((pillar, " ".join(parts)))
    # proof-point receipts: quoted claim lines under the Proof points section
    proof = re.search(r"##\s*Proof points.*?\n(.*?)(?=\n##\s|\Z)", raw, re.DOTALL)
    if proof:
        for q in re.findall(r'-\s*"([^"]+)"', proof.group(1)):
            facts.append(("Proof", q.strip()))
    return facts


def _make_lasso_image_fn(library_path, account_key, prefix="lasso_"):
    """An image_fn(day_key, used) over LASSO's flat committed cards (content_library/
    lasso_*.png). pick_image is dir-based with no prefix filter, so LASSO (whose cards sit
    flat beside every other gym's) needs this explicit prefix glob. Each pick is cropped to
    1200x900 and hosted (R2) via the planner's own crop-and-host path; None when the pool
    is exhausted."""
    import glob
    import os
    import types
    from . import gbp_planner
    paths = sorted(p for p in glob.glob(os.path.join(library_path, f"{prefix}*"))
                   if p.lower().endswith((".png", ".jpg", ".jpeg")))

    def _image_fn(day_key, used):
        for p in paths:
            key = os.path.basename(p)
            if key in used:
                continue
            url = gbp_planner._cropped_image_url(account_key,
                                                 types.SimpleNamespace(path=p), day_key)
            if url:
                used.add(key)
                return url
        return None

    return _image_fn


# ---- production wiring (real resolvers) -------------------------------------

def _resolve_voice(portal_gym_key):
    """Load the gym's brand bible, durable-first then repo. None -> caller BLOCKS."""
    import os
    from . import voice as voice_mod
    base = _base_of(portal_gym_key)
    for path in (os.path.join(config.client_voice_dir(), base, "lasso_voice.md"),
                 os.path.join("brand_voice", base, "lasso_voice.md"),
                 os.path.join("brand_voice", f"{base}_voice.md")):
        v = voice_mod.load_voice(path)
        if v is not None:
            return v
    return None


def _resolve_library(portal_gym_key):
    """The gym's photo library dir: content_library/<base> if it exists, else the flat
    content_library (LASSO's own committed images live flat, prefixed lasso_*)."""
    import os
    base = _base_of(portal_gym_key)
    sub = os.path.join(config.LIBRARY_PATH, base)
    return sub if os.path.isdir(sub) else config.LIBRARY_PATH


def run(portal_gym_key="lasso", *, city=None, cta_url=None, days=30, now=None,
        logger=None):
    """Production one-shot: resolve the gym's REAL voice + library + offer + connection,
    then plan its PENDING GBP month. city is REQUIRED (no fabrication of a location); the
    caller passes the gym's real city. Returns the planner result dict."""
    log = logger or (lambda m: print(f"[gbp-dogfood] {m}"))
    if not config.hosting_enabled():
        # not a publish, but hosting is what turns crops into usable urls; without it the
        # planner would plan nothing. Surface the real dependency loudly.
        log("WARNING: media hosting is not configured; crops cannot be hosted and the "
            "planner will produce no image_url. Run where R2 hosting is set.")
    if not city:
        raise ValueError("city is required (real location; never fabricated)")
    from .gbp_store import GbpStore
    store = GbpStore()
    if not store.available():
        return {"ok": False, "reason": "portal store unavailable", "planned": 0}
    base = _base_of(portal_gym_key)
    account_gen_key = f"{base}_ig"
    voice = _resolve_voice(portal_gym_key)
    # the GbpStore itself is the offer reader (it has onboarding_intake + _get)
    offer = _resolve_offer_for(portal_gym_key, store)
    loc, offer_url = _resolve_connection_location(portal_gym_key, store)
    conn_status = _connection_status(portal_gym_key, store)   # G4

    # GATE 1: OFFER only for a gym whose live offer a human has confirmed (default: none).
    offer_confirmed = base in config.gbp_offer_confirmed_gyms()
    # GATE 2: a CLIENT gym's FIRST GBP month is withheld in 'coach_review' until a coach
    # releases it. First month == the gym has NO prior googlebusiness rows at all.
    # LASSO (base 'lasso') is EXEMPT BY DESIGN: it is the dogfood account, Blake is both
    # owner and coach, and approving the raw month IS the client-experience test. Client
    # gyms always get GATE 2; the dogfood skips it deliberately, not by accident.
    is_dogfood = base == "lasso"
    is_first_month = True
    try:
        is_first_month = not store.any_gbp_rows(portal_gym_key)
    except Exception:  # noqa: BLE001
        is_first_month = True
    initial_status = "coach_review" if (config.gbp_coach_screen_enabled()
                                        and is_first_month and not is_dogfood) else "pending"
    if initial_status == "coach_review":
        log(f"{portal_gym_key}: first GBP month -> written as 'coach_review' "
            "(withheld from owner until a coach releases it; GATE 2)")
    elif is_dogfood:
        log(f"{portal_gym_key}: dogfood account -> GATE 2 skipped by design "
            "(owner==coach; approving raw IS the test)")

    facts = None
    if base == "lasso":
        # LASSO's content is bespoke: facts from lasso_now.md, cards flat in the repo
        # content_library (prefix lasso_), and its own site as the LEARN_MORE target.
        library_path = os.path.join(_repo_root(), "content_library")
        facts = _lasso_facts()
        image_fn = _make_lasso_image_fn(library_path, account_gen_key)
        resolved_cta = cta_url or offer_url or LASSO_DEFAULT_CTA_URL
    else:
        library_path = _resolve_library(portal_gym_key)
        image_fn = None
        resolved_cta = cta_url or offer_url or \
            (offer[1]["redeemOnlineUrl"] if offer and offer[1] else "")

    return plan_gbp_dogfood(
        portal_gym_key, account_gen_key, voice=voice, library_path=library_path,
        city=city, store=store, offer=offer, cta_url=resolved_cta, gbp_location_id=loc,
        days=days, now=now, facts=facts, image_fn=image_fn,
        offer_confirmed=offer_confirmed, initial_status=initial_status,
        connection_status=conn_status, logger=log)


def release(portal_gym_key, *, logger=None):
    """GATE 2 coach release: after a coach screens a gym's withheld first month, flip its
    'coach_review' rows to 'pending' so the owner can see and approve them. Releases the
    gym's WHOLE first month across EVERY platform (GBP + FB/IG) in one shot — the coach
    walks the owner through their first approvals once, per the SOP."""
    log = logger or (lambda m: print(f"[gbp-dogfood] {m}"))
    from .portal_calendar_store import SupabaseCalendarStore
    store = SupabaseCalendarStore()
    if not (getattr(store, "_url", "") and getattr(store, "_key", "")):
        return {"ok": False, "reason": "portal store unavailable", "released": 0}
    released = store.release_coach_review(portal_gym_key)
    log(f"{portal_gym_key}: released {len(released)} coach_review rows -> pending "
        "(all platforms)")
    return {"ok": True, "released": len(released)}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    # coach release: `python3 -m agent.gbp_dogfood release <gym>`
    if argv and argv[0] == "release":
        if len(argv) < 2:
            print("usage: python3 -m agent.gbp_dogfood release <portal_gym_key>")
            return 2
        res = release(argv[1])
        print(f"[gbp-dogfood] result: {res}")
        return 0 if res.get("ok") else 1
    gym = argv[0] if argv else "lasso"
    # city passed as 2nd arg (real); LASSO's is Carmel.
    city = argv[1] if len(argv) > 1 else ("Carmel" if gym == "lasso" else None)
    cta_url = argv[2] if len(argv) > 2 else None    # optional LEARN_MORE target override
    if not city:
        print("usage: python3 -m agent.gbp_dogfood <portal_gym_key> <city> [cta_url]")
        print("       python3 -m agent.gbp_dogfood release <portal_gym_key>")
        return 2
    res = run(gym, city=city, cta_url=cta_url)
    print(f"[gbp-dogfood] result: {res}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
