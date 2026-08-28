"""
account_key_guard.py — the tenant-isolation guard on the account_key -> Zernio profile bind.

THE HAZARD (Bird Dog CrossFit / Bolton Club, live): with account_key derived ad hoc, two
tenants could end up pointed at ONE account_key, and a bind would then wire one gym's
account_key to ANOTHER gym's Zernio profile — one gym's posts landing on another gym's
socials. This guard REFUSES any bind that would cross tenants and alerts loudly.

It gates the single choke point _persist_profile_id (zernio_routes.py), the one place a
resolved account_key -> zernio_profile_id is written to the local db AND the shared plane.
Two cross-tenant rebinds are blocked:

  1. REBIND-KEY: this account_key is already bound to a DIFFERENT zernio_profile_id.
     A key's profile is stable; silently repointing it would strand the old profile and
     hijack the new one. (A no-op re-bind to the SAME profile id is allowed — idempotent.)
  2. STEAL-PROFILE: this zernio_profile_id is already bound to a DIFFERENT account_key
     (a different tenant). Binding it to a second key is exactly "one gym's posts on
     another gym's socials". Blocked.

On a block it fires ONE loud ops alert to the approver channel (via ops_alerts, itself
behind AGENT_OPS_ALERTS) and returns a refusal; the caller MUST NOT proceed with the write.

RAILS: the whole guard is behind AGENT_ACCOUNT_KEY_GUARD (default OFF) so it ships dark and
is armed by hand; when OFF, check_bind returns ALLOW unconditionally and today's behaviour is
unchanged. The lookups are injected (existing_profile_for / key_for_profile), so the guard is
pure and fully testable offline. It NEVER merges tenants, NEVER deletes a profile, NEVER
writes — it only says allow / block.
"""

from . import config, ops_alerts


class BindDecision:
    """The guard's verdict. `allowed` gates the write; `reason` explains a block (safe to
    log — carries only account keys + profile ids, never a secret)."""

    __slots__ = ("allowed", "reason", "code")

    def __init__(self, allowed, reason="", code=""):
        self.allowed = allowed
        self.reason = reason
        self.code = code

    def __bool__(self):
        return bool(self.allowed)

    def __repr__(self):
        return f"<BindDecision allowed={self.allowed} code={self.code!r}>"


def check_bind(account_key, zernio_profile_id, *,
               existing_profile_for=None, key_for_profile=None, alert=None):
    """Decide whether binding account_key -> zernio_profile_id is safe.

    account_key         : the tenant key about to be bound.
    zernio_profile_id   : the Zernio profile id it would point at.
    existing_profile_for: callable(account_key) -> current bound profile id (or ""/None).
                          Injected so the guard stays pure; production passes the db reader.
    key_for_profile     : callable(profile_id) -> the account_key already bound to that
                          profile (or ""/None). Injected likewise.
    alert               : injectable ops-alert sink (defaults to ops_alerts.alert), for tests.

    Returns a BindDecision. When AGENT_ACCOUNT_KEY_GUARD is OFF, always ALLOW (ships dark).
    A blank account_key or profile id is a no-op ALLOW (nothing to protect / the caller's own
    guards handle emptiness)."""
    if not config.account_key_guard_enabled():
        return BindDecision(True, "guard disabled (AGENT_ACCOUNT_KEY_GUARD off)", "disabled")

    account_key = (str(account_key) if account_key is not None else "").strip()
    pid = (str(zernio_profile_id) if zernio_profile_id is not None else "").strip()
    if not account_key or not pid:
        return BindDecision(True, "nothing to bind (empty key or profile id)", "empty")

    fire = alert if alert is not None else ops_alerts.alert

    # 1) REBIND-KEY: this key already points at a DIFFERENT profile. Same profile = idempotent
    #    no-op (allowed); a different one would silently repoint the tenant (blocked).
    if existing_profile_for is not None:
        try:
            current = (str(existing_profile_for(account_key) or "")).strip()
        except Exception:  # noqa: BLE001 - a lookup hiccup must not silently allow a bad bind
            current = ""
        if current and current != pid:
            reason = (f"REFUSED account_key rebind: {account_key} is already bound to Zernio "
                      f"profile {current}, cannot repoint to {pid}")
            fire(f"account-key guard: {reason}")
            return BindDecision(False, reason, "rebind_key")

    # 2) STEAL-PROFILE: this profile is already owned by a DIFFERENT tenant key. Binding it to
    #    a second key is the cross-tenant leak (one gym's posts on another gym's socials).
    if key_for_profile is not None:
        try:
            owner = (str(key_for_profile(pid) or "")).strip()
        except Exception:  # noqa: BLE001
            owner = ""
        if owner and owner != account_key:
            reason = (f"REFUSED cross-tenant bind: Zernio profile {pid} is already bound to a "
                      f"DIFFERENT gym ({owner}); refusing to also bind it to {account_key}")
            fire(f"account-key guard: {reason}")
            return BindDecision(False, reason, "steal_profile")

    return BindDecision(True, "bind is tenant-safe", "ok")
