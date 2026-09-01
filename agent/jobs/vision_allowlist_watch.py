"""vision_allowlist_watch.py — say out loud when AGENT_VISION_GYMS and what Echo is
actually analyzing have drifted apart (audit item 5, 2026-08-31).

THE DRIFT, live. AGENT_VISION_GYMS was `district_h,eng,gritx,topfuel`, but the month's
vision-spend ledger told a different story:

  * crossfitreverb30b5b2 (173 calls) and hillcountry (35) were being analyzed while NOT
    on the allowlist — the Google-Drive staging lane (agent/gym_media_builder.py ->
    vision.analyze_and_store) is gated by GYM_DRIVE_STAGE + GYM_DRIVE_CONNECT and never
    consults AGENT_VISION_GYMS at all;
  * district_h was allowlisted with nothing to analyze (no content library, no Drive
    media source) — a dead entry;
  * gritx sat AT its 400/month cap with analysis silently paused, because that same
    Drive lane passed no `alert=`, so the runaway guard could refuse but never speak.

Two failure directions, both invisible: money spent on a gym nobody armed, and a gym
armed that gets nothing. This watchdog reports BOTH, read-only, once a day.

It deliberately does NOT change the allowlist or gate the Drive lane. Narrowing that
gate would silently switch two live gyms from vision-scored picks back to legacy
rotation, and widening the env would arm a full-library sweep on gyms Blake never armed
(~190 fresh Gemini calls). Which way that goes is Blake's ruling; this job's job is to
make sure the question is never invisible again.

Flag: AGENT_VISION_ALLOWLIST_WATCH, default ON (read-only: it reads the env, the kv
spend ledger, and nothing else). One deduped alert per (drift set, month).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from agent import config


def _log(msg):
    print(f"[vision-watch] {msg}")


def spend_by_gym(month=None, kv_iter=None):
    """{gym: calls} from the kv vision-spend ledger for `month` (YYYY-MM).

    The ledger key is `vision_spend_<gym>_<YYYY-MM>` (agent/vision.within_gym_budget).
    It is the only honest record of which gyms Echo actually spent vision on — the
    sidecars and media_asset.vision_json live in two different stores and neither one
    covers both lanes."""
    month = month or date.today().isoformat()[:7]
    prefix = "vision_spend_"
    suffix = f"_{month}"
    out = {}
    for key, value in (kv_iter or _default_kv_iter)():
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        gym = key[len(prefix):-len(suffix)]
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            continue
        if gym and n > 0:
            out[gym] = n
    return out


def _default_kv_iter():
    import sqlite3
    from agent import db
    with db.connect() as conn:
        conn.row_factory = sqlite3.Row
        return [(r["key"], r["value"]) for r in
                conn.execute("SELECT key, value FROM kv WHERE key LIKE 'vision_spend_%'")]


def drift(allowlist, spending):
    """(analyzing_unarmed, armed_idle) — the two directions the drift can run.

    analyzing_unarmed: gyms burning vision calls that are NOT on AGENT_VISION_GYMS.
    armed_idle:        gyms on the allowlist that spent nothing this month.
    Pure, sorted, so the alert text is stable and dedupes cleanly."""
    allow = {str(g).strip().lower() for g in (allowlist or set()) if str(g).strip()}
    spent = {str(g).strip().lower() for g in (spending or {})}
    return sorted(spent - allow), sorted(allow - spent)


def run(month=None, allowlist=None, spending=None, alert=None, kv=None, seen=None):
    """One read-only drift check. Behind AGENT_VISION_ALLOWLIST_WATCH (default ON).

    `seen` is the dedupe seam (get/set on a kv-like mapping); production uses the kv
    table so the same drift alerts ONCE per month, not once per nightly run."""
    if not config.vision_allowlist_watch_enabled():
        return {"ok": False, "reason": "AGENT_VISION_ALLOWLIST_WATCH is OFF."}
    month = month or date.today().isoformat()[:7]
    allowlist = config.vision_gyms() if allowlist is None else allowlist
    spending = spend_by_gym(month, kv_iter=kv) if spending is None else spending
    unarmed, idle = drift(allowlist, spending)
    out = {"ok": True, "month": month, "allowlist": sorted(allowlist),
           "spending": spending, "analyzing_unarmed": unarmed, "armed_idle": idle}
    if not unarmed and not idle:
        return out

    parts = []
    if unarmed:
        parts.append("SPENDING WITHOUT BEING ARMED: "
                     + ", ".join(f"{g} ({spending.get(g, 0)} calls)" for g in unarmed)
                     + " — the Google Drive staging lane runs vision without "
                       "consulting AGENT_VISION_GYMS")
    if idle:
        parts.append("ARMED BUT ANALYZING NOTHING: " + ", ".join(idle)
                     + " — either no media to analyze, or already at the monthly cap")
    msg = (f"vision allowlist drift ({month}). AGENT_VISION_GYMS="
           f"{','.join(sorted(allowlist)) or '(empty)'}. " + ". ".join(parts)
           + ". Read-only report: nothing was armed or disarmed.")
    _log(msg)

    stamp = f"vision_drift_alert_{month}_{'|'.join(unarmed)}_{'|'.join(idle)}"
    store = seen if seen is not None else _kv_seen()
    try:
        if not store.get(stamp):
            (alert or _default_alert)(msg)
            store.set(stamp, "1")
            out["alerted"] = True
    except Exception:  # noqa: BLE001 - alerting never fails the watch
        pass
    return out


class _kv_seen:
    def get(self, key):
        from agent import db
        return db.kv_get(key)

    def set(self, key, value):
        from agent import db
        db.kv_set(key, value)


def _default_alert(msg):
    try:
        from agent import ops_alerts
        ops_alerts.alert(msg)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--month", default=None, help="YYYY-MM (default: this month)")
    args = p.parse_args(argv)
    print(json.dumps(run(month=args.month), indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
