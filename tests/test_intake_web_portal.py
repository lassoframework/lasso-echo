"""
Tests for Stage 2 T3: intake-web data-store token lookup, per-token rate limit,
and portal gym endpoint.

All tests are OFFLINE: mock db, injectable R2, no live network calls.
"""

import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


# ---- helpers -------------------------------------------------------------------

def _sha256(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _make_db():
    """In-memory SQLite db with the gyms table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gyms (
            account_key TEXT PRIMARY KEY,
            gym_name TEXT,
            token_sha256 TEXT,
            token_status TEXT DEFAULT 'NOT_SET',
            upload_link TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def _insert_gym(conn, account_key, token=None, status="ACTIVE", upload_link=None,
                gym_name=None):
    fp = _sha256(token) if token else None
    conn.execute(
        "INSERT OR REPLACE INTO gyms "
        "(account_key, gym_name, token_sha256, token_status, upload_link) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_key, gym_name, fp, status, upload_link)
    )
    conn.commit()


class FakeR2WithList:
    def __init__(self, keys=()):
        self.objects = {}
        self._keys = list(keys)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, content_type)

    def list_keys(self, prefix):
        return [k for k in self._keys if k.startswith(prefix)]


# ---- 1. client_for_token checks data store when automint ON --------------------

def test_client_for_token_checks_data_store(monkeypatch):
    """When automint ON, client_for_token calls client_for_token_data first."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    # No env token set for this gym.
    monkeypatch.delenv("AGENT_INTAKE_TOKEN_GYMSTORE", raising=False)

    conn = _make_db()
    token = "store-tok-abcdef1234"
    _insert_gym(conn, "gymstore", token=token, status="ACTIVE")

    # Import the real function once to avoid recursive patching.
    from agent.intake_tokens import client_for_token_data as _real_cftd

    # Patch to call the real implementation with our in-memory db connection.
    monkeypatch.setattr("agent.intake_tokens.client_for_token_data",
                        lambda t, db_conn=None: _real_cftd(t, db_conn=conn))

    result = intake_web.client_for_token(token)
    assert result == "gymstore"


def test_client_for_token_env_fallback(monkeypatch):
    """When automint OFF, env fallback still works (flag-OFF path byte-identical)."""
    monkeypatch.delenv("AGENT_ONBOARD_AUTOMINT", raising=False)
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMENV", "env-tok-xyz9999")

    result = intake_web.client_for_token("env-tok-xyz9999")
    assert result == "gymenv"


def test_client_for_token_env_fallback_when_automint_on(monkeypatch):
    """When automint ON but token not in db, env var fallback still resolves."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMFALLBACK", "fallback-tok-8888")

    # Patch data store to return None (token not in db).
    monkeypatch.setattr("agent.intake_tokens.client_for_token_data",
                        lambda t, db_conn=None: None)

    result = intake_web.client_for_token("fallback-tok-8888")
    assert result == "gymfallback"


# ---- 2. Revoked token rejected -------------------------------------------------

def test_revoked_token_rejected(monkeypatch):
    """A REVOKED token returns None from client_for_token."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.delenv("AGENT_INTAKE_TOKEN_GYMREVOKED", raising=False)

    conn = _make_db()
    token = "revoked-tok-54321xyz"
    _insert_gym(conn, "gymrevoked", token=token, status="REVOKED")

    from agent.intake_tokens import client_for_token_data as _real_cftd
    monkeypatch.setattr("agent.intake_tokens.client_for_token_data",
                        lambda t, db_conn=None: _real_cftd(t, db_conn=conn))

    result = intake_web.client_for_token(token)
    assert result is None


# ---- 3. Per-token rate limit ---------------------------------------------------

def test_per_token_rate_limit(monkeypatch):
    """21 calls on same token hash prefix triggers 429 on the 21st."""
    # Clear the token hits dict for a clean test.
    intake_web._token_hits.clear()

    token = "rate-limit-test-token-abc"
    hp = intake_web._token_hash_prefix(token)

    # First 20 should pass.
    for i in range(20):
        allowed = intake_web.allow_token_request(hp, now=1000.0 + i)
        assert allowed is True, f"call {i+1} should be allowed"

    # 21st should be denied.
    denied = intake_web.allow_token_request(hp, now=1019.0)
    assert denied is False

    # A different token hash is unaffected.
    other_token = "completely-different-token"
    other_hp = intake_web._token_hash_prefix(other_token)
    assert intake_web.allow_token_request(other_hp, now=1019.0) is True

    # After 60 seconds, the window rolls and the original token is free again.
    rolled = intake_web.allow_token_request(hp, now=1061.0)
    assert rolled is True


def test_per_token_rate_limit_constant():
    """The per-token rate limit constant is 20."""
    assert intake_web._TOKEN_RATE_PER_MINUTE == 20


# ---- 4. Portal endpoint returns gym info when flag ON --------------------------

def test_portal_endpoint_returns_gym_info(monkeypatch):
    """GET /portal/gym/<key> returns JSON with account_key and intake_status."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    conn = _make_db()
    _insert_gym(conn, "gymportal", token="portal-tok-11111",
                status="ACTIVE", upload_link="https://intake.example.com/u/gymportal",
                gym_name="Portal Gym")

    from agent import db as _db

    def _fake_gym_get(account_key):
        row = conn.execute(
            "SELECT account_key, gym_name, token_sha256, token_status, "
            "upload_link, created_at, updated_at FROM gyms WHERE account_key=?",
            (account_key,)
        ).fetchone()
        return dict(row) if row else None

    monkeypatch.setattr("agent.db.gym_get", _fake_gym_get)

    status_code, body = intake_web.handle_portal_gym_status("gymportal")
    assert status_code == 200
    assert body["account_key"] == "gymportal"
    assert body["intake_status"] == "ACTIVE"
    assert body["token_status"] == "ACTIVE"
    assert body["upload_link"] == "https://intake.example.com/u/gymportal"
    # R2 not provided: null values.
    assert body["last_upload_at"] is None
    assert body["upload_count"] is None


def test_portal_endpoint_returns_gym_info_with_r2(monkeypatch):
    """Portal endpoint populates upload_count and last_upload_at from R2 listing."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    conn = _make_db()
    _insert_gym(conn, "gymr2", token="r2-tok-22222", status="ACTIVE",
                upload_link="https://intake.example.com/u/gymr2")

    monkeypatch.setattr("agent.db.gym_get", lambda k: (
        dict(conn.execute(
            "SELECT account_key, gym_name, token_sha256, token_status, "
            "upload_link, created_at, updated_at FROM gyms WHERE account_key=?",
            (k,)
        ).fetchone()) if conn.execute(
            "SELECT account_key FROM gyms WHERE account_key=?", (k,)
        ).fetchone() else None
    ))

    r2 = FakeR2WithList(keys=[
        "intake/gymr2/incoming/20240601T120000Z_photo.jpg",
        "intake/gymr2/incoming/20240601T120000Z_upload.json",
        "intake/gymr2/incoming/20240605T090000Z_clip.mp4",
    ])

    status_code, body = intake_web.handle_portal_gym_status("gymr2", r2=r2)
    assert status_code == 200
    # 3 objects in incoming.
    assert body["upload_count"] == 3
    # Timestamp from the alphabetically last non-sidecar key.
    assert body["last_upload_at"] == "20240605T090000Z"


def test_portal_endpoint_not_found(monkeypatch):
    """Returns 404 when account_key not in gyms table."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)

    status_code, body = intake_web.handle_portal_gym_status("unknowngym")
    assert status_code == 404
    assert "not found" in body["error"]


# ---- 5. Portal endpoint 403 when flag OFF --------------------------------------

def test_portal_endpoint_flag_off_returns_403(monkeypatch):
    """AGENT_PORTAL_APPROVALS OFF returns 403."""
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)

    status_code, body = intake_web.handle_portal_gym_status("anygym")
    assert status_code == 403
    assert "disabled" in body["error"]
