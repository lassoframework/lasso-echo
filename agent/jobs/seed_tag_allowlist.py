"""seed_tag_allowlist.py — one-shot seed job for gym_tag_allowlist.

For each gym in client_gym_bases() plus 'lasso', seeds gym_tag_allowlist with:
  - The gym's own IG handle (kind='own', consent=true)
  - For LASSO: every connected client gym handle from Zernio accounts_list (kind='partner', consent=true)

Reads gym IG handles from the accounts.py registry (ig_handle field in dynamic registry,
or inferred from the account key for hardcoded accounts).

Behind AGENT_MENTIONS flag — no-op when OFF.
Has run() function callable standalone or from CLI.
"""
from __future__ import annotations
import json
import os
import urllib.request
from datetime import datetime


LOG_PATH = os.path.join(os.path.dirname(__file__), "seed_log_allowlist.txt")


def _supabase_upsert(url: str, key: str, rows: list[dict]) -> None:
    """Upsert rows into gym_tag_allowlist via Supabase REST (on conflict do update)."""
    if not rows:
        return
    data = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{url}/rest/v1/gym_tag_allowlist",
        data=data,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        _ = resp.read()


def _log(lines: list[str], path: str = LOG_PATH) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"[{ts}] {line}\n")


def _ig_handle_for_base(base: str) -> str:
    """Best-effort IG handle from the dynamic registry for this base.
    Returns '' when not found (handle not required for own-account records to be useful)."""
    from agent import config as _config
    try:
        path = _config.gym_registry_path()
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        for row in rows:
            if (row.get("base") or "").strip() == base:
                return (row.get("ig_handle") or "").strip().lstrip("@")
    except Exception:
        pass
    return ""


def _lasso_ig_handle() -> str:
    """LASSO's own IG handle: from env AGENT_LASSO_IG_HANDLE, else 'lassoframework'."""
    return (os.environ.get("AGENT_LASSO_IG_HANDLE") or "lassoframework").strip().lstrip("@")


def _connected_client_handles(zernio_client=None) -> list[str]:
    """Handles of all connected client gyms visible in Zernio (partner seeds for LASSO)."""
    from agent import config as _config
    if not _config.zernio_enabled():
        return []
    try:
        if zernio_client is None:
            from agent import zernio as _z
            zernio_client = _z.ZernioClient()
        # List all profiles, then for each get the IG handle from their connected accounts
        profiles = zernio_client.list_profiles() or []
        handles = []
        for profile in profiles:
            pid = (profile.get("_id") or profile.get("id") or "")
            if not pid:
                continue
            try:
                accounts = zernio_client.list_accounts(pid)
                for acct in (accounts or []):
                    h = (acct.get("handle") or acct.get("username") or "").strip().lstrip("@")
                    if h and h not in handles:
                        handles.append(h)
            except Exception:
                pass
        return handles
    except Exception:
        return []


def run(zernio_client=None, log_path: str = LOG_PATH) -> dict:
    """Seed gym_tag_allowlist. Returns {seeded: int, skipped: int, gyms: list}.

    Behind AGENT_MENTIONS: no-op when OFF.
    """
    from agent import config as _config
    from agent.calendar_autopublish import client_gym_bases

    if not _config.mentions_enabled():
        _log(["SKIP: AGENT_MENTIONS is OFF"], path=log_path)
        return {"seeded": 0, "skipped": 0, "gyms": []}

    url = _config.supabase_url()
    skey = _config.supabase_service_key()
    if not url or not skey:
        _log(["ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set"], path=log_path)
        return {"seeded": 0, "skipped": 0, "gyms": []}

    log_lines: list[str] = []
    seeded = 0
    skipped = 0
    gym_report: list[str] = []

    # --- Seed each client gym with its own IG handle (kind='own') ---
    bases = client_gym_bases()
    for base in bases:
        handle = _ig_handle_for_base(base)
        if not handle:
            log_lines.append(f"SKIP {base}: no ig_handle in registry")
            skipped += 1
            continue
        row = {"gym_id": base, "handle": handle, "kind": "own", "consent": True}
        try:
            _supabase_upsert(url, skey, [row])
            log_lines.append(f"SEEDED {base}: own handle @{handle}")
            gym_report.append(base)
            seeded += 1
        except Exception as exc:
            log_lines.append(f"ERROR {base}: {exc}")
            skipped += 1

    # --- Seed LASSO with its own handle ---
    lasso_handle = _lasso_ig_handle()
    row = {"gym_id": "lasso", "handle": lasso_handle, "kind": "own", "consent": True}
    try:
        _supabase_upsert(url, skey, [row])
        log_lines.append(f"SEEDED lasso: own handle @{lasso_handle}")
        gym_report.append("lasso")
        seeded += 1
    except Exception as exc:
        log_lines.append(f"ERROR lasso own: {exc}")
        skipped += 1

    # --- Seed LASSO with connected client gym handles (kind='partner') ---
    partner_handles = _connected_client_handles(zernio_client=zernio_client)
    if partner_handles:
        partner_rows = [
            {"gym_id": "lasso", "handle": h, "kind": "partner", "consent": True}
            for h in partner_handles
        ]
        try:
            _supabase_upsert(url, skey, partner_rows)
            for h in partner_handles:
                log_lines.append(f"SEEDED lasso: partner handle @{h}")
                seeded += 1
        except Exception as exc:
            log_lines.append(f"ERROR lasso partners: {exc}")
            skipped += len(partner_handles)
    else:
        log_lines.append("INFO lasso: no partner handles found via Zernio (skipping partner seed)")

    _log(log_lines, path=log_path)
    return {"seeded": seeded, "skipped": skipped, "gyms": gym_report}


if __name__ == "__main__":
    import sys
    result = run()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["skipped"] == 0 else 1)
