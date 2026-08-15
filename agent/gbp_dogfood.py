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


def plan_gbp_dogfood(portal_gym_key, account_gen_key, *, voice, library_path, city,
                     store, offer=None, cta_url="", gbp_location_id=None, days=30,
                     start=None, events=(), caption_fn=None, image_fn=None,
                     now=None, logger=None):
    """Idempotently plan + write one gym's PENDING GBP month. Skips (no-op) when the gym
    already has googlebusiness rows dated on/after `start` (never a duplicate month).
    Blocks (does not fabricate) when voice is missing. Returns the planner result dict
    (plus {'skipped_existing': True} on the idempotency no-op)."""
    log = logger or (lambda m: print(f"[gbp-dogfood] {m}"))
    start = start or (now or date.today())
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
        image_fn=image_fn, logger=log)


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


def run(portal_gym_key="lasso", *, city=None, days=30, now=None, logger=None):
    """Production one-shot: resolve the gym's REAL voice + library + offer + connection,
    then plan its PENDING GBP month. city is REQUIRED (no fabrication of a location); the
    caller passes the gym's real city. Returns the planner result dict."""
    log = logger or (lambda m: print(f"[gbp-dogfood] {m}"))
    if not config.gbp_publish_enabled() and not config.hosting_enabled():
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
    account_gen_key = f"{_base_of(portal_gym_key)}_ig"
    voice = _resolve_voice(portal_gym_key)
    library_path = _resolve_library(portal_gym_key)
    # the GbpStore itself is the offer reader (it has onboarding_intake + _get)
    offer = _resolve_offer_for(portal_gym_key, store)
    loc, offer_url = _resolve_connection_location(portal_gym_key, store)
    cta_url = offer_url or (offer[1]["redeemOnlineUrl"] if offer and offer[1] else "")
    return plan_gbp_dogfood(
        portal_gym_key, account_gen_key, voice=voice, library_path=library_path,
        city=city, store=store, offer=offer, cta_url=cta_url, gbp_location_id=loc,
        days=days, now=now, logger=log)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    gym = argv[0] if argv else "lasso"
    # city passed as 2nd arg (real); LASSO's is Carmel.
    city = argv[1] if len(argv) > 1 else ("Carmel" if gym == "lasso" else None)
    if not city:
        print("usage: python3 -m agent.gbp_dogfood <portal_gym_key> <city>")
        return 2
    res = run(gym, city=city)
    print(f"[gbp-dogfood] result: {res}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
