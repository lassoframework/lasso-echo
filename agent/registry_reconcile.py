"""Repair an Echo client's missing publisher registry row without publishing.

The calendar publisher enumerates ``all_accounts()``, so an approved row is
invisible when its gym is missing from ``gym_accounts.json``. This reconciler
reads the positive Echo client markers and the active approved calendar rows,
then writes only an unambiguous gym UUID/base pair through ``register_gym``.
An intake token by itself is never evidence of an Echo purchase.
"""

import re

from . import accounts, config, echo_clients

_BASE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_PLATFORMS = frozenset(("instagram", "facebook"))


def _approved_calendar_bases(rows, clients):
    """{gym UUID: {calendar bases}} for owned, active approved IG/FB rows."""
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (str(row.get("status") or "").lower() != "approved"
                or row.get("published_at") is not None
                or str(row.get("variant_status") or "").lower() != "active"
                or str(row.get("account") or "").lower() not in _PLATFORMS
                or not row.get("post_date")):
            continue
        base = str(row.get("gym_id") or "").strip()
        if not _BASE.fullmatch(base):
            continue
        gid = clients.key_to_gym.get(base)
        if gid in clients.gym_ids:
            out.setdefault(gid, set()).add(base)
    return out


def _calendar_rows(http=None):
    """Complete approved calendar reading, or None. A partial page is not proof."""
    url, key = config.supabase_url(), config.supabase_service_key()
    if not url or not key:
        return None
    if http is None:
        import requests  # lazy
        http = requests
    rows, complete = echo_clients._read_all(  # noqa: SLF001 - same bounded REST reader
        http, url.rstrip("/"), key, "content_calendar",
        "gym_id,account,post_date,status,variant_status,published_at",
        {"status": "eq.approved", "published_at": "is.null",
         "variant_status": "eq.active", "account": "in.(instagram,facebook)",
         "order": "id.asc"})
    return rows if complete else None


def reconcile(*, http=None, clients=None, calendar_rows=None):
    """Register missing, uniquely identified Echo clients. No publish or client send.

    Returns ``{ok, registered, held, error}``. Every external read must complete
    before the first write. A gym with competing aliases, a colliding registry
    base, an unknown name, or no positive Echo marker is held for an operator.
    """
    result = {"ok": False, "registered": [], "held": [], "error": ""}
    if not config.dynamic_accounts_enabled():
        result["error"] = "dynamic accounts disabled"
        return result
    clients = clients if clients is not None else echo_clients.snapshot(http=http, fresh=True)
    if not clients.ok:
        result["error"] = clients.error or "Echo client universe unreadable"
        return result
    calendar_rows = calendar_rows if calendar_rows is not None else _calendar_rows(http=http)
    if calendar_rows is None:
        result["error"] = "approved calendar unreadable or incomplete"
        return result
    if (not isinstance(calendar_rows, list)
            or any(not isinstance(row, dict) for row in calendar_rows)):
        result["error"] = "approved calendar returned invalid rows"
        return result
    try:
        registry = accounts._load_registry_rows(strict=True)  # noqa: SLF001
    except accounts.RegistryUnreadable:
        result["error"] = "publisher registry unreadable"
        return result
    if not all(isinstance(row, dict) for row in registry):
        result["error"] = "publisher registry contains invalid rows"
        return result

    by_id, by_base = {}, {}
    for row in registry:
        gid = echo_clients.normalize_key(row.get("gym_id"))
        base = str(row.get("base") or "").strip()
        if gid:
            by_id.setdefault(gid, []).append(row)
        if base:
            by_base.setdefault(base, []).append(row)
    calendar = _approved_calendar_bases(calendar_rows, clients)
    hardcoded = {echo_clients.normalize_key(key)
                 for key in echo_clients.hardcoded_bases()}

    for gid in sorted(clients.gym_ids):
        if not clients.is_client(gid):
            continue  # positive Echo marker is required at the write boundary
        if by_id.get(gid):
            if len(by_id[gid]) > 1:
                result["held"].append((gid, "duplicate registry gym_id"))
            continue
        name = str(clients.names.get(gid) or "").strip()
        if not name:
            result["held"].append((gid, "missing portal gym name"))
            continue
        scheduled = calendar.get(gid, set())
        issued = set(clients.token_keys_by_gym.get(gid, ()))
        candidates = scheduled if scheduled else issued
        if len(candidates) != 1:
            reason = "competing calendar bases" if scheduled else "no unique issued key"
            result["held"].append((gid, reason))
            continue
        base = next(iter(candidates))
        # A bare name/slug alias is not enough to bind a calendar row to a
        # tenant: an ads-only gym may share it. The issued token or a key with
        # this UUID's fingerprint is the narrow onboarding identity contract.
        owned_keys = issued | {
            echo_clients._echo_key(gid, name),  # noqa: SLF001
            echo_clients._portal_key(gid, name),  # noqa: SLF001
        }
        if (not _BASE.fullmatch(base) or base.startswith("lasso")
                or base == "blake_personal" or base in hardcoded
                or base not in owned_keys
                or base in clients.ambiguous_keys
                or clients.key_to_gym.get(base) != gid):
            result["held"].append((gid, "unsafe or ambiguous base"))
            continue
        if by_base.get(base):
            result["held"].append((gid, "base already registered"))
            continue
        try:
            keys = accounts.register_gym(base, name=name, gym_id=gid,
                                         door="registry_reconcile", require_absent=True)
            verified = accounts.find_base_for_gym_id(gid) == base
        except Exception as exc:  # noqa: BLE001 - one failed gym never guesses success
            result["held"].append((gid, f"registry write failed: {type(exc).__name__}"))
            continue
        if keys != [f"{base}_ig", f"{base}_fb"] or not verified:
            result["held"].append((gid, "registry write refused or unverified"))
            continue
        result["registered"].append(base)
        by_id[gid] = [{"base": base, "gym_id": gid}]
        by_base[base] = by_id[gid]
    result["ok"] = True
    return result
