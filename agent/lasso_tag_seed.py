"""lasso_tag_seed.py — keep LASSO's gym_tag_allowlist current from the LIVE roster.

seed_lasso_allowlist() upserts LASSO's rows in gym_tag_allowlist from the live
Zernio client roster: every connected client gym's handle as kind='partner'
(the handles LASSO proof posts tag — thecrossfiteng.official, traingritx,
topfuelcrossfit_valpo_, piercefitnesskitchener, hillcountrymvmt, theboltonclub,
...) plus lassoframework itself as kind='own'. Without this, publish_guard's
missing_mention rail would hold every LASSO proof/results post because the
allowlist went stale as clients joined.

REUSES agent/jobs/seed_tag_allowlist.py (the one-shot seed job) for the Zernio
roster read and the Supabase upsert — one implementation, two entry points.
Behind AGENT_MENTIONS (the existing flag posture): flag OFF -> no-op. The
nightly draw calls this once per day (kv-stamped) so the list tracks the
roster without a human re-running the seed job.

CLI:  python -m agent.lasso_tag_seed [--dry-run]
"""
from __future__ import annotations

import json


def seed_lasso_allowlist(zernio_client=None, dry_run=False, upsert=None):
    """Upsert LASSO's own + partner handles into gym_tag_allowlist from the live
    Zernio roster. Returns a summary dict; dry_run reads the roster and reports
    the rows WITHOUT writing. Never raises for a roster/creds problem — it
    reports honestly and writes nothing (an empty roster never wipes rows:
    this only ever upserts, it never deletes)."""
    from . import config
    from .jobs import seed_tag_allowlist as _seed_job

    if not config.mentions_enabled():
        return {"ok": False, "reason": "AGENT_MENTIONS off", "rows": []}

    own = _seed_job._lasso_ig_handle()
    partners = _seed_job._connected_client_handles(zernio_client=zernio_client)
    rows = [{"gym_id": "lasso", "handle": own, "kind": "own", "consent": True}]
    rows += [{"gym_id": "lasso", "handle": h, "kind": "partner", "consent": True}
             for h in partners if h and h != own]

    if dry_run:
        return {"ok": True, "dry_run": True, "rows": rows,
                "handles": [r["handle"] for r in rows]}

    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return {"ok": False, "reason": "supabase creds absent", "rows": []}
    upsert = upsert or _seed_job._supabase_upsert
    try:
        upsert(url, key, rows)
    except Exception as e:  # noqa: BLE001 - a seed failure must report, not crash
        return {"ok": False, "reason": f"{type(e).__name__}: {e}", "rows": []}
    return {"ok": True, "dry_run": False, "seeded": len(rows),
            "handles": [r["handle"] for r in rows]}


def run_nightly(now_date=None):
    """The nightly-draw hook: once per day (kv-stamped), refresh LASSO's
    allowlist from the live roster. Self-gated on AGENT_MENTIONS; isolated —
    an error never takes the draft run down."""
    from . import config, db
    if not config.mentions_enabled():
        return {"ok": False, "reason": "AGENT_MENTIONS off"}
    from datetime import date as _date
    day = now_date or _date.today().isoformat()
    stamp = f"lasso_tag_seed_{day}"
    if db.kv_get(stamp):
        return {"ok": True, "reason": "already ran today"}
    out = seed_lasso_allowlist()
    if out.get("ok"):
        db.kv_set(stamp, day)
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Seed LASSO's gym_tag_allowlist from the live Zernio roster.")
    parser.add_argument("--dry-run", action="store_true",
                        help="read the roster and print the rows; write nothing")
    args = parser.parse_args()
    print(json.dumps(seed_lasso_allowlist(dry_run=args.dry_run), indent=2))
