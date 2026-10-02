#!/usr/bin/env python3
"""Enroll Swift River under its existing portal key, without changing its content.

Ticket a18b8587-909e-5fb6-b596-fc60b3f2ef83 reports not_registered,
key_mismatch and no_sources. Enrollment alone cannot repair stranded intake
answers. This one-off reads the portal identity, refuses conflicting registry
keys, and calls accounts.register_gym with the real name and stable gym ID.
Existing matching enrollment is a no-op. No name or identity is guessed.

Run on the Echo worker with its existing configuration and persistent volume:
    python scripts/register_swiftrivercrossfite5c9db.py --dry-run
    python scripts/register_swiftrivercrossfite5c9db.py

Dry-run performs reads only. Live mode writes only through register_gym and
keeps its client-eligibility, duplicate-identity and inactive-account defaults.
The script refuses all arguments other than --dry-run and --help. It cannot
change billing, Stripe, pixel/CAPI, ad budgets, ad targeting, published posts,
medical/legal data, secrets, configuration, approvals or Story Studio requests.

Exit 0 means enrollment and the checks below passed, NOT permission to publish.
Exit 2 means a refusal or remaining setup blocker; do not close the ticket.
Source and connection checks use the worker's existing SQLite snapshot without
schema initialization or gym_get's cross-service hydration. Missing/unreadable
state is a blocker, never proof of readiness. Normal publishing gates still apply.
Do not run this script from a developer copy and treat it as production evidence.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "swiftrivercrossfite5c9db"


class Blocked(RuntimeError):
    pass


def portal_rows(table, select, **filters):
    """Bounded, filtered GET only; never print response bodies or credentials."""
    from agent import config
    import requests

    if table not in {"echo_intake_tokens", "echo_social_intake", "gyms"}:
        raise Blocked("Refused a table outside this enrollment's read scope.")
    url, key = config.supabase_url(), config.supabase_service_key()
    if not url or not key:
        raise Blocked("Portal identity cannot be checked with this configuration.")
    response = requests.get(
        f"{url.rstrip('/')}/rest/v1/{table}",
        params={"select": select, "limit": "2", **filters},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise Blocked(f"Portal {table} read failed; no identity inferred.")
    rows = response.json()
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise Blocked(f"Portal {table} returned an unexpected shape.")
    return rows


def registry_rows():
    # Avoid _load_registry_rows: on corrupt input it emits an ops alert.
    from agent import config

    path = Path(config.gym_registry_path())
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise Blocked("Registry is malformed; nothing may be overwritten.")
    return rows


def preflight():
    from agent import accounts, config, echo_clients

    if not config.dynamic_accounts_enabled():
        raise Blocked("Dynamic accounts are disabled; configuration was not changed.")
    tokens = portal_rows("echo_intake_tokens", "gym_id,echo_account_key",
                         echo_account_key=f"eq.{BASE}")
    if len(tokens) != 1 or tokens[0].get("echo_account_key") != BASE:
        raise Blocked("The requested portal key does not have exactly one owner.")
    gid = str(tokens[0].get("gym_id") or "").strip()
    if not gid:
        raise Blocked("The portal key has no gym ID.")
    owned = portal_rows("echo_intake_tokens", "gym_id,echo_account_key",
                        gym_id=f"eq.{gid}")
    if len(owned) != 1 or owned[0].get("echo_account_key") != BASE:
        raise Blocked("This gym has conflicting portal keys; a teammate must resolve them.")
    gyms = portal_rows("gyms", "id,name", id=f"eq.{gid}")
    if len(gyms) != 1 or str(gyms[0].get("id") or "") != gid:
        raise Blocked("The portal gym record could not be identified uniquely.")
    name = str(gyms[0].get("name") or "").strip()
    if not name or not echo_clients.is_echo_client(gid):
        raise Blocked("A real portal name and verified Echo-client membership are required.")
    intakes = portal_rows("echo_social_intake", "client_key,echo_account_key",
                          client_key=f"eq.{gid}")
    if len(intakes) > 1:
        raise Blocked("Multiple intake records require identity review.")
    intake_key = str((intakes[0] if intakes else {}).get("echo_account_key") or "").strip()
    rows = registry_rows()
    matching = [r for r in rows if str(r.get("base") or "").strip() == BASE]
    if len(matching) > 1:
        raise Blocked("Duplicate target registry rows require review.")
    for row in rows:
        row_base = str(row.get("base") or "").strip()
        row_gid = str(row.get("gym_id") or "").strip()
        if row_base != BASE and (row_gid == gid or
                                (intake_key and row_base == intake_key)):
            raise Blocked("An existing registry key owns this gym or its intake; "
                          "resolve the split before enrollment.")
    if matching and str(matching[0].get("gym_id") or "").strip() != gid:
        raise Blocked("Existing target enrollment has missing or conflicting identity.")
    if any(a.key in {BASE + "_ig", BASE + "_fb"} for a in accounts.ACCOUNTS):
        raise Blocked("Hardcoded accounts shadow this enrollment; review required.")
    return gid, name, intake_key, bool(matching)


def setup_blockers(intake_key):
    """Inspect source and connection prerequisites with no database writes."""
    from agent import db, zernio

    blockers = []
    uri = Path(db.db_path()).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # These are the exact variants approved_sources(BASE + "_ig") searches.
        has_sources = conn.execute(
            "SELECT 1 FROM client_sources WHERE account_key IN (?, ?) "
            "AND status='approved' LIMIT 1", (BASE + "_ig", BASE)
        ).fetchone() is not None
        if not has_sources:
            blockers.append("no approved sources on the requested key")
            if intake_key and intake_key != BASE:
                blockers.append("intake key mismatch; source reconciliation needs review")
        for suffix, platform in (("_ig", "instagram"), ("_fb", "facebook")):
            rows = [conn.execute(
                "SELECT zernio_profile_id,zernio_default_fb_page_id FROM gyms "
                "WHERE account_key=?", (key,)
            ).fetchone() for key in (BASE + suffix, BASE)]
            profile = next((r["zernio_profile_id"] for r in rows
                            if r and r["zernio_profile_id"]), None)
            if not profile:
                blockers.append(f"{platform} profile unresolved on this worker")
                continue
            connected = zernio.ZernioClient().list_accounts(str(profile))
            if not zernio.account_id_for(connected, platform):
                blockers.append(f"{platform} connection missing")
            if platform == "facebook" and not any(
                    r and r["zernio_default_fb_page_id"] for r in rows):
                blockers.append("Facebook page selection missing")
    finally:
        conn.close()
    return blockers


def run(dry_run):
    from agent import accounts
    from agent.calendar_autopublish import client_gym_bases
    from agent.client_media_sync import _client_bases

    gid, name, intake_key, exists = preflight()
    print(f"{BASE}: " + ("already enrolled" if exists else "enrollment required"))
    if dry_run:
        print("DRY RUN: no enrollment written; lane discovery is current, not simulated.")
    elif not exists:
        keys = accounts.register_gym(BASE, name=name, gym_id=gid,
                                     door="ops_fix_a18b8587")
        if set(keys) != {BASE + "_ig", BASE + "_fb"}:
            raise Blocked("Registration did not return the exact requested account keys.")
        print("Enrollment written; checking remaining prerequisites.")
    else:
        print("Existing identity matches; no registry rewrite needed.")

    blockers = []
    if any(accounts.get_account(BASE + s) is None for s in ("_ig", "_fb")):
        blockers.append("IG/FB account resolution incomplete")
    if BASE not in _client_bases():
        blockers.append("build lane does not discover this key")
    if BASE not in client_gym_bases():
        blockers.append("publish lane does not discover this key")
    try:
        blockers.extend(setup_blockers(intake_key))
    except Exception as exc:
        # Do not turn a failed reader into a clean bill of health or print secrets.
        blockers.append(f"source/connection verification incomplete ({type(exc).__name__})")
    for reason in blockers:
        print(f"BLOCKED: {reason}")
    if blockers:
        print("Keep the ticket open. No sources, connections or gates were changed.")
        return 2
    print("Registry discovery and source/connection checks passed on this worker. "
          "No content was built, approved or published.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        return run(args.dry_run)
    except Blocked as exc:
        print(f"BLOCKED: {exc}")
    except Exception as exc:
        print(f"BLOCKED: verification failed ({type(exc).__name__}); keep ticket open.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
