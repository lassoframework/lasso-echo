"""posting_tz_backfill.py — give every gym its OWN posting timezone, and shout when
one has none (audit item 4, 2026-08-31).

THE DEFECT. `config.posting_timezone_for()` reads gyms.posting_timezone and, when it is
empty, falls back to the GLOBAL default (POSTING_TIMEZONE, America/New_York). On
2026-08-31 nine of the sixteen gyms in the worker registry had it NULL — crossfitlocal,
crossfitnewtown, crossfitsunnyside2616ac, lasso, swiftrivercrossfitd23567, theboltonclub,
toughtemple52040e, train7164ae502, zanshinfitness630e22 — so their posts land at the
default hour, not the gym's local hour. Nothing ever wrote the column except a human
running `python -m agent set-timezone`, and nothing ever noticed it was empty.

TWO JOBS, one pass:
  1. BACKFILL from real evidence, in strict priority:
       a. the gym's CONNECTED Google Business location (gym_gbp_connections.timezone) —
          Google's own record of where the gym is, the strongest evidence we hold;
       b. a US "City, ST" in the gym's OWN approved brand bible / brain doc, resolved
          through the SAME address->tz map the GBP sync uses
          (agent/gbp_conn_sync._tz_from_address).
     A gym with neither is LEFT NULL. Never a guess, never a default written into the
     column (writing the fallback would destroy the very signal the watchdog reads).
  2. WATCHDOG: every active gym still carrying no timezone gets ONE deduped alert line
     naming it and saying what evidence is missing, so this class cannot go quiet again.

RAILS: writes gyms.posting_timezone and nothing else; never overwrites a timezone a
human already set; validates every value through ZoneInfo before writing, so a bad map
entry can never reach the publish lane; publishes nothing.

Flag: AGENT_POSTING_TZ_WATCH, default ON (the watchdog half is read-only; the backfill
half only ever fills a NULL). Dry-run by default from the CLI.

Usage:
  python -m agent.jobs.posting_tz_backfill            # dry run: show the plan
  python -m agent.jobs.posting_tz_backfill --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from agent import config

# A US "City, ST" (optionally + ZIP) inside the gym's own approved copy. Deliberately
# narrow: a two-letter token only counts when it directly follows a comma after a
# capitalized place name, which is the shape _tz_from_address is built to read.
_PLACE_RE = re.compile(
    r"\b([A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+){0,2}),\s*([A-Z]{2})\b(?:\s+\d{5})?")

# Bible/brain files that count as the gym's own words about where it is.
_DOC_NAMES = ("lasso_voice.md", "social_proof.md")


def _log(msg):
    print(f"[posting-tz] {msg}")


def _valid(tz):
    """True when `tz` is a real IANA zone. A typo must never reach the publish lane."""
    if not tz:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(str(tz))
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- evidence sources --------------------------------------------------------------

def gbp_timezones(store=None):
    """{portal_gym_key: (timezone, location_name)} for every CONNECTED Google Business
    location. Read-only; an unavailable store yields {} rather than raising."""
    out = {}
    try:
        if store is None:
            from agent.gbp_store import GbpStore
            store = GbpStore()
        if not store.available():
            return out
        for row in store.all_connections() or []:
            key = str((row or {}).get("portal_gym_key") or "").strip()
            tz = str((row or {}).get("timezone") or "").strip()
            status = str((row or {}).get("status") or "").strip().lower()
            if key and tz and status == "connected":
                out[key] = (tz, str(row.get("location_name") or ""))
    except Exception as exc:  # noqa: BLE001
        _log(f"GBP connection read failed ({type(exc).__name__}); "
             "falling back to document evidence only")
    return out


def _doc_paths(base, data_dir=None):
    """Every place a gym's own approved copy lives on the volume."""
    data = data_dir or os.environ.get("AGENT_DATA_DIR", "/data")
    paths = [os.path.join(data, "brains", f"{base}.md")]
    for name in _DOC_NAMES:
        paths.append(os.path.join(data, "brand_voice", base, name))
    return paths


def tz_from_own_documents(base, data_dir=None, read=None):
    """(timezone, evidence) resolved from a US 'City, ST' in the gym's OWN brand bible
    or brain doc, or (None, None). The gym wrote these words about itself, so this is
    evidence, not inference — and every candidate still has to survive the same
    address->tz map the GBP sync uses."""
    from agent.gbp_conn_sync import _tz_from_address
    reader = read or _read_text
    for path in _doc_paths(base, data_dir):
        text = reader(path)
        if not text:
            continue
        for match in _PLACE_RE.finditer(text):
            place = match.group(0)
            tz = _tz_from_address(place)
            if _valid(tz):
                return tz, f"{os.path.basename(path)}: \"{place}\""
    return None, None


def _read_text(path):
    try:
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return ""


def resolve(base, gbp_map, data_dir=None, read=None):
    """(timezone, evidence) for ONE gym, strongest evidence first, or (None, None)."""
    tz, name = (gbp_map or {}).get(base, (None, None))
    if _valid(tz):
        label = f"Google Business location \"{name}\"" if name else "Google Business location"
        return tz, label
    return tz_from_own_documents(base, data_dir=data_dir, read=read)


# ---- the pass ----------------------------------------------------------------------

def plan(rows, gbp_map, data_dir=None, read=None):
    """Pure planner over registry rows. Returns (writes, already_set, unresolved).

    `rows` are gyms-table dicts (account_key, posting_timezone, display_name).
    A gym that ALREADY has a timezone is never re-decided — a human's `set-timezone`
    always wins over anything found here."""
    writes, already, unresolved = [], [], []
    for row in rows or []:
        base = str((row or {}).get("account_key") or "").strip()
        if not base:
            continue
        current = str((row or {}).get("posting_timezone") or "").strip()
        if current:
            already.append({"gym": base, "tz": current})
            continue
        tz, evidence = resolve(base, gbp_map, data_dir=data_dir, read=read)
        if _valid(tz):
            writes.append({"gym": base, "tz": tz, "evidence": evidence})
        else:
            unresolved.append({"gym": base,
                               "name": str((row or {}).get("display_name") or "")})
    return writes, already, unresolved


def _registry_rows():
    import sqlite3
    from agent import db
    with db.connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT account_key, display_name, posting_timezone FROM gyms "
            "ORDER BY account_key")]


def run(*, apply=False, rows=None, gbp_map=None, data_dir=None, read=None,
        writer=None, alert=None):
    """Backfill + watchdog. Behind AGENT_POSTING_TZ_WATCH (default ON).

    Returns {"ok", "written", "writes", "already", "unresolved"}."""
    if not config.posting_tz_watch_enabled():
        return {"ok": False, "reason": "AGENT_POSTING_TZ_WATCH is OFF.",
                "writes": [], "already": [], "unresolved": []}
    if rows is None:
        rows = _registry_rows()
    if gbp_map is None:
        gbp_map = gbp_timezones()
    writes, already, unresolved = plan(rows, gbp_map, data_dir=data_dir, read=read)

    written = 0
    if apply and writes:
        if writer is None:
            from agent import db as _db

            def writer(gym, tz):
                _db.gym_upsert(gym, posting_timezone=tz)
        for w in writes:
            try:
                writer(w["gym"], w["tz"])
                written += 1
                _log(f"{w['gym']}: posting_timezone = {w['tz']}  ({w['evidence']})")
            except Exception as exc:  # noqa: BLE001 - one gym never blocks the rest
                _log(f"{w['gym']}: write failed ({type(exc).__name__})")

    # WATCHDOG: one honest line naming every gym still without a timezone.
    if unresolved:
        names = ", ".join(u["gym"] for u in unresolved)
        msg = (f"posting timezone missing for {len(unresolved)} active gym(s): {names}. "
               f"Their posts schedule at the global default "
               f"({config.POSTING_TIMEZONE}), not the gym's local hour. Echo found no "
               "evidence to backfill from: no connected Google Business location and "
               "no city/state in the gym's own brand bible. Fix by connecting GBP, or "
               "by hand: python -m agent set-timezone --account <key> --tz <IANA>.")
        _log(msg)
        try:
            (alert or _default_alert)(msg)
        except Exception:  # noqa: BLE001 - alerting never fails the sweep
            pass
    return {"ok": True, "written": written, "writes": writes,
            "already": already, "unresolved": unresolved}


def _default_alert(msg):
    try:
        from agent import ops_alerts
        ops_alerts.alert(msg)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="write the backfilled values")
    args = p.parse_args(argv)
    out = run(apply=args.apply)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
