"""
gbp_month_sweep.py — plan a GBP month for EVERY Google-connected gym, on a schedule.

WHY THIS EXISTS (Blake, 2026-09-02): ENG asked why its Google Business posts had gone
quiet. The real answer, from Zernio (the publish system of record): ENG had ZERO published
GBP posts, ever. Not a cadence problem, a wiring gap. `gbp_planner.plan_gbp_month` was
only ever reachable from `agent/gbp_dogfood.py` — a manual, single-gym, flag-gated
entrypoint — so GBP content only went out when a human ran it by hand, for one gym at a
time. Ten gyms are connected to Google Business today; none of them were on any schedule.
Blake's ruling: "all gyms that are connected to google should get this... no coach review
on anything!!! and yes wire it."

WHAT IT DOES, per connected gym:
  1. Enumerates `gym_gbp_connections` (GbpStore.all_connections) and takes only gyms whose
     connection status is 'connected'. A 'needs_reconnect' gym is left alone — the planner
     itself also pauses on it (gbp_dogfood G4), so queueing rows that cannot publish is
     impossible from either side.
  2. Resolves that gym's REAL city from its own verified Google listing (the Zernio
     googlebusiness account's `metadata.locationAddress`, e.g. "326 Southwest 2nd Terrace,
     Cape Coral, Florida" -> "Cape Coral"). `gbp_dogfood.run` REQUIRES a real city and
     raises rather than fabricate one; a gym whose address will not parse is SKIPPED with a
     named alert, never planned against a guessed location.
  3. Calls the existing, proven `gbp_dogfood.run(gym, city=...)` unchanged — same A+ caption
     gate, same crop-and-host, same cadence (8 standard + 1 offer + up to 2 event + 4
     photo), same idempotency (a gym with future GBP rows is skipped, never double-planned),
     same block-on-missing-voice (no fabrication).

WHAT IT DOES NOT DO, by design:
  - Never publishes. Every row lands status='pending' and waits for the gym's own approval,
    exactly like a feed post. This job only fills the queue.
  - Never writes 'coach_review'. AGENT_GBP_COACH_SCREEN is already false in production and
    Blake ruled it out explicitly; gbp_dogfood.run honors that flag, so the owner sees their
    own month directly. The APPROVAL gate is untouched and is a different gate entirely.
  - Never plans an OFFER post unless that gym is in config.gbp_offer_confirmed_gyms()
    (GATE 1, unchanged) — a wrong offer on a gym's Google listing is not a recoverable
    mistake.

Per-gym isolation: one gym's failure (bad address, missing voice, flaky read) never blocks
the rest, and never takes the daily run down. Behind AGENT_GBP_MONTH_SWEEP, default OFF.
"""
from __future__ import annotations

import argparse
import sys

from .. import config, ops_alerts


def city_from_address(address) -> str:
    """The city from a US postal address string, or "" when it cannot be read.

    Google returns the gym's own verified listing address, e.g.
    "326 Southwest 2nd Terrace, Cape Coral, Florida" or
    "64 Hobbs Street #3, Conway, New Hampshire" or "1 Main St, Austin, TX 78701".
    In every one of those shapes the city is the SECOND-TO-LAST comma part (the last is
    the state, with or without a ZIP). Pure; never raises.

    Returns "" rather than a guess for anything that does not have at least a
    street/city/state shape — the caller SKIPS that gym instead of planning a month
    against a fabricated location.
    """
    parts = [p.strip() for p in str(address or "").split(",") if p.strip()]
    if len(parts) < 3:
        return ""
    return parts[-2]


def _gbp_address_for(base, *, db=None, zernio=None) -> str:
    """The gym's `metadata.locationAddress` from its LIVE Zernio googlebusiness account,
    or "". This is the gym's OWN verified Google listing address — the most authoritative
    city source available, and the same field gbp_conn_sync already reads for timezone.
    Never raises."""
    try:
        if db is None:
            from .. import db as db
        if zernio is None:
            from ..zernio import ZernioClient
            zernio = ZernioClient()
        pid = ((db.gym_get(base) or {}).get("zernio_profile_id") or "").strip()
        if not pid:
            return ""
        for a in (zernio.list_accounts(pid) or {}).get("accounts") or []:
            if (a.get("platform") or "").lower() != "googlebusiness":
                continue
            return str((a.get("metadata") or {}).get("locationAddress") or "")
    except Exception:  # noqa: BLE001 - a resolution failure is a SKIP, never a guess
        return ""
    return ""


def sweep(*, store=None, runner=None, address_fn=None, gyms=None, limit=None,
          alert=None, logger=None):
    """Plan a GBP month for every connected gym. Returns a summary dict.

    store/runner/address_fn are injectable for tests: `runner(gym, city=...)` defaults to
    gbp_dogfood.run (the real, proven single-gym path) and `address_fn(base)` to the live
    Zernio lookup. `gyms` restricts to an explicit list (a single-gym smoke test).
    """
    log = logger or (lambda m: print(f"[gbp-month-sweep] {m}"))
    alert = alert if alert is not None else ops_alerts.alert

    if store is None:
        from ..gbp_store import GbpStore
        store = GbpStore()
    if not store.available():
        log("skipped: portal store unavailable")
        return {"ok": False, "reason": "store unavailable", "planned": 0}
    if runner is None:
        from ..gbp_dogfood import run as runner
    address_fn = address_fn or _gbp_address_for

    try:
        conns = store.all_connections() or []
    except Exception as exc:  # noqa: BLE001
        log(f"skipped: connection read failed ({type(exc).__name__})")
        return {"ok": False, "reason": "connection read failed", "planned": 0}

    connected = []
    seen = set()
    for c in conns:
        key = (c.get("portal_gym_key") or "").strip()
        if not key or key in seen:
            continue
        if str(c.get("status", "")).lower() != "connected":
            continue
        if gyms and key not in gyms:
            continue
        seen.add(key)
        connected.append(key)
    if limit:
        connected = connected[:limit]

    results = []
    totals = {"planned": 0, "gyms_planned": 0, "skipped_existing": 0,
              "no_city": 0, "blocked": 0, "errors": 0}
    for base in connected:
        try:
            address = address_fn(base)
            city = city_from_address(address)
            if not city:
                totals["no_city"] += 1
                results.append({"gym": base, "status": "no_city"})
                log(f"{base}: no readable city on the Google listing "
                    f"({address!r}) -> SKIPPED (never planned against a guess)")
                alert(f"GBP month sweep: {base} is connected to Google Business but its "
                      f"listing address did not yield a city ({address!r}), so no GBP month "
                      "was planned for it. Fix the address on the Google listing, or plan it "
                      "by hand with the real city: python -m agent.gbp_dogfood "
                      f"{base} '<City>'")
                continue
            res = runner(base, city=city) or {}
            if res.get("skipped_existing"):
                totals["skipped_existing"] += 1
                results.append({"gym": base, "status": "already_planned"})
                log(f"{base}: future GBP rows already present -> skip")
                continue
            if not res.get("ok"):
                totals["blocked"] += 1
                reason = res.get("reason") or "unknown"
                results.append({"gym": base, "status": f"blocked: {reason}"})
                log(f"{base}: BLOCKED ({reason}) -> nothing written")
                alert(f"GBP month sweep: {base} is connected to Google Business but its "
                      f"month could not be planned ({reason}). Nothing was written and "
                      "nothing was fabricated.")
                continue
            n = int(res.get("planned") or 0)
            totals["planned"] += n
            totals["gyms_planned"] += 1
            results.append({"gym": base, "status": "planned", "planned": n,
                            "city": city})
            log(f"{base}: planned {n} PENDING GBP row(s) for {city}")
        except Exception as exc:  # noqa: BLE001 - one gym never sinks the sweep
            totals["errors"] += 1
            results.append({"gym": base, "status": f"error: {type(exc).__name__}"})
            log(f"{base}: sweep failed ({type(exc).__name__}: {exc})")

    log(f"done: {totals['gyms_planned']} gym(s) planned ({totals['planned']} rows), "
        f"{totals['skipped_existing']} already planned, {totals['no_city']} no city, "
        f"{totals['blocked']} blocked, {totals['errors']} error(s)")
    return {"ok": True, "connected": len(connected), "results": results, **totals}


def run(**kwargs):
    """Flag-gated entry point for the scheduled lane. OFF -> no-op."""
    if not config.gbp_month_sweep_enabled():
        return {"ok": False, "reason": "flag off", "planned": 0}
    return sweep(**kwargs)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--gym", action="append", default=None,
                   help="restrict to this gym base (repeatable) — use for a smoke test")
    p.add_argument("--limit", type=int, default=None, help="cap gyms swept")
    p.add_argument("--force", action="store_true",
                   help="run even when AGENT_GBP_MONTH_SWEEP is off (manual use)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    fn = sweep if args.force else run
    res = fn(gyms=set(args.gym) if args.gym else None, limit=args.limit)
    print(f"[gbp-month-sweep] result: "
          f"{ {k: v for k, v in res.items() if k != 'results'} }")
    for r in res.get("results") or []:
        print(f"  {r['gym']:<28} {r['status']}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
