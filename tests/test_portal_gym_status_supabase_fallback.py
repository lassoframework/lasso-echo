"""GET /portal/gym/<key> Supabase fallback (2026-08-25 audit P2).

intake-web has no /data volume, so its local gyms table is empty and every real
account key 404'd. The fix: when sqlite misses, existence resolves via the
portal Supabase echo_intake_tokens table; the upload link reconstructs through
link_for (deterministic mint). Every failure path stays CLOSED (404) — a token
is never minted for a key the portal does not hold.
"""

import agent.intake_web as intake_web
from agent import intake_tokens


SECRET_ENV = "AGENT_INTAKE_SIGNING_SECRET"


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


class _Http:
    """Injectable requests stand-in; records the call it receives."""

    def __init__(self, resp=None, raises=None):
        self._resp = resp
        self._raises = raises
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if self._raises:
            raise self._raises
        return self._resp


# ---- handler: fallback serves a portal-known gym --------------------------------

def test_sqlite_miss_supabase_hit_serves_reconstructed_status(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv(SECRET_ENV, "test-signing-secret")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://intake.example.com")
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)
    monkeypatch.setattr(
        intake_web, "_supabase_token_gym",
        lambda k, http=None: {"gym_id": "uuid-1", "echo_account_key": k},
    )

    status_code, body = intake_web.handle_portal_gym_status("lasso")
    assert status_code == 200
    assert body["account_key"] == "lasso"
    # token_status shim: ACTIVE when the signing secret is configured.
    assert body["token_status"] == "ACTIVE"
    assert body["intake_status"] == "ACTIVE"
    # Upload link reconstructs via the deterministic mint.
    assert body["upload_link"] == (
        "https://intake.example.com/u/" + intake_tokens.mint("lasso")
    )


def test_sqlite_miss_supabase_hit_no_secret_serves_null_link(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)
    monkeypatch.setattr(
        intake_web, "_supabase_token_gym",
        lambda k, http=None: {"gym_id": "uuid-1", "echo_account_key": k},
    )

    status_code, body = intake_web.handle_portal_gym_status("lasso")
    assert status_code == 200
    assert body["token_status"] == "NOT_SET"
    assert body["upload_link"] is None


# ---- handler: fail CLOSED --------------------------------------------------------

def test_sqlite_miss_supabase_miss_is_404_even_with_secret(monkeypatch):
    """No blind-minting: a slug the portal does not hold 404s even though a
    minted token for it would verify."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv(SECRET_ENV, "test-signing-secret")
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)
    monkeypatch.setattr(intake_web, "_supabase_token_gym", lambda k, http=None: None)

    status_code, body = intake_web.handle_portal_gym_status("neveronboarded")
    assert status_code == 404
    assert "not found" in body["error"]


# ---- helper: _supabase_token_gym ------------------------------------------------

def test_helper_no_creds_returns_none_without_network(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    http = _Http(resp=_Resp(200, [{"echo_account_key": "lasso"}]))
    assert intake_web._supabase_token_gym("lasso", http=http) is None
    assert http.calls == []


def test_helper_hit_queries_echo_intake_tokens_by_eq_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    http = _Http(resp=_Resp(200, [{"gym_id": "u1", "echo_account_key": "lasso"}]))

    row = intake_web._supabase_token_gym("lasso", http=http)
    assert row == {"gym_id": "u1", "echo_account_key": "lasso"}
    call = http.calls[0]
    assert call["url"].endswith("/rest/v1/echo_intake_tokens")
    assert call["params"]["echo_account_key"] == "eq.lasso"


def test_helper_http_error_and_exception_return_none(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    assert intake_web._supabase_token_gym(
        "lasso", http=_Http(resp=_Resp(500, []))) is None
    assert intake_web._supabase_token_gym(
        "lasso", http=_Http(raises=RuntimeError("boom"))) is None
    assert intake_web._supabase_token_gym(
        "lasso", http=_Http(resp=_Resp(200, []))) is None


# ---- revocation honesty (audit MINORs 1+2) ---------------------------------------

def test_fallback_reports_revoked_from_denylist_with_no_link(monkeypatch):
    """The token_status shim only reflects secret presence; a denylisted gym must
    read REVOKED on this container too, and never show a live-looking link."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv(SECRET_ENV, "test-signing-secret")
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)
    monkeypatch.setattr(
        intake_web, "_supabase_token_gym",
        lambda k, http=None: {"gym_id": "uuid-1", "echo_account_key": k},
    )
    monkeypatch.setattr(intake_web, "is_revoked", lambda k, r2=None: True)

    status_code, body = intake_web.handle_portal_gym_status("revokedgym")
    assert status_code == 200
    assert body["token_status"] == "REVOKED"
    assert body["upload_link"] is None


def test_sqlite_hit_revoked_row_serves_no_link(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv(SECRET_ENV, "test-signing-secret")
    monkeypatch.setattr("agent.db.gym_get", lambda k: {
        "account_key": k, "token_status": "REVOKED",
        "upload_link": "https://old.example.com/u/stale-plaintext",
    })

    status_code, body = intake_web.handle_portal_gym_status("revokedhit")
    assert status_code == 200
    assert body["token_status"] == "REVOKED"
    assert body["upload_link"] is None


# ---- sqlite-hit path: link reconstruction now real -------------------------------

def test_sqlite_hit_prefers_minted_link_over_stored_plaintext(monkeypatch):
    """The old decrypt_token call never existed (AttributeError swallowed), so
    the stored plaintext always won. With a signing secret set, the minted
    reconstruction now wins; without one, plaintext still serves (pinned by the
    pre-existing test_portal_endpoint_returns_gym_info)."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv(SECRET_ENV, "test-signing-secret")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://intake.example.com")
    monkeypatch.setattr("agent.db.gym_get", lambda k: {
        "account_key": k, "token_status": "ACTIVE",
        "upload_link": "https://old.example.com/u/stale-plaintext",
    })

    status_code, body = intake_web.handle_portal_gym_status("gymhit")
    assert status_code == 200
    assert body["upload_link"] == (
        "https://intake.example.com/u/" + intake_tokens.mint("gymhit")
    )
