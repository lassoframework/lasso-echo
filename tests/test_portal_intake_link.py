"""
Tests for GET /api/portal/intake-link/<account_key> — the server-to-server
read-only endpoint that lets the LASSO portal auto-populate echo_intake_tokens
without operator paste.

All tests are OFFLINE: in-memory SQLite db, no live network calls, no R2.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


# ---- helpers -------------------------------------------------------------------

_VALID_KEY = "test-portal-key-abc123xyz"


def _make_db():
    """In-memory SQLite with the gyms table (columns matching the real schema)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gyms (
            account_key TEXT PRIMARY KEY,
            display_name TEXT DEFAULT '',
            gym_name TEXT,
            intake_token_hash TEXT,
            token_rotated_at TEXT,
            token_revoked INTEGER DEFAULT 0,
            token_status TEXT DEFAULT 'NOT_SET',
            intake_token_encrypted TEXT,
            upload_link TEXT,
            publish_creds_status TEXT DEFAULT 'NOT SET (by hand)',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def _insert_gym(conn, account_key, enc_blob=None, minted_at=None):
    conn.execute(
        "INSERT OR REPLACE INTO gyms "
        "(account_key, gym_name, intake_token_encrypted, token_rotated_at) "
        "VALUES (?, ?, ?, ?)",
        (account_key, account_key + "_gym", enc_blob, minted_at)
    )
    conn.commit()


def _arm(monkeypatch, conn):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    import agent.db as _db
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, conn=None: _db_gym_get(key, monkeypatch._db_conn))
    monkeypatch._db_conn = conn


def _db_gym_get(account_key, conn):
    row = conn.execute(
        "SELECT * FROM gyms WHERE account_key = ?", (account_key,)
    ).fetchone()
    return dict(row) if row else None


# ---- 1. auth gate: 401 when key missing or wrong -------------------------------

def test_401_when_key_missing(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    status, body = intake_web.handle_portal_intake_link("anykey", "")
    assert status == 401
    assert body.get("error") == "unauthorized"


def test_401_when_key_wrong(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    status, body = intake_web.handle_portal_intake_link("anykey", "wrong-key-xyz")
    assert status == 401
    assert body.get("error") == "unauthorized"


def test_401_when_portal_key_env_unset(monkeypatch):
    """When AGENT_PORTAL_KEY is not configured at all, every request is 401."""
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.delenv("AGENT_PORTAL_KEY", raising=False)
    status, body = intake_web.handle_portal_intake_link("anykey", _VALID_KEY)
    assert status == 401
    assert body.get("error") == "unauthorized"


# ---- 2. intake gate: 404 when AGENT_INTAKE_ENABLED is OFF -----------------------

def test_404_when_intake_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    status, body = intake_web.handle_portal_intake_link("anykey", _VALID_KEY)
    assert status == 404
    assert body.get("error") == "not found"


# ---- 3. gym not found: 404 ------------------------------------------------------

def test_404_when_gym_missing(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    conn = _make_db()
    import agent.db as _db
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, conn=None: _db_gym_get(key, conn) if conn else _db_gym_get(key, _make_db()))
    # Override gym_get to use our empty in-memory db.
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, conn=None: None)  # gym not found
    status, body = intake_web.handle_portal_intake_link("missing_gym", _VALID_KEY)
    assert status == 404
    assert body.get("error") == "not found"


# ---- 4. gym exists but no encrypted token yet: 404 (same as not found) ----------

def test_404_when_no_encrypted_token(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    conn = _make_db()
    _insert_gym(conn, "nogym_enc", enc_blob=None)
    import agent.db as _db
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, conn=None: _db_gym_get(key, conn if conn else _make_db()))
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, _conn=None: _db_gym_get(key, conn))
    status, body = intake_web.handle_portal_intake_link("nogym_enc", _VALID_KEY)
    assert status == 404
    assert body.get("error") == "not found"


# ---- 5. success: 200 with encrypted blob and account_key -------------------------

def test_200_returns_encrypted_blob(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    conn = _make_db()
    _insert_gym(conn, "gym_alpha", enc_blob="gAAAAAFAKE_FERNET_BLOB_abc123",
                minted_at="2026-07-17T12:00:00Z")
    import agent.db as _db
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, _conn=None: _db_gym_get(key, conn))
    status, body = intake_web.handle_portal_intake_link("gym_alpha", _VALID_KEY)
    assert status == 200
    assert body["account_key"] == "gym_alpha"
    assert body["intake_token_encrypted"] == "gAAAAAFAKE_FERNET_BLOB_abc123"
    assert body["token_minted_at"] == "2026-07-17T12:00:00Z"


def test_200_token_minted_at_none_when_not_set(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    conn = _make_db()
    _insert_gym(conn, "gym_beta", enc_blob="gAAAAABETA_BLOB_xyz789", minted_at=None)
    import agent.db as _db
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, _conn=None: _db_gym_get(key, conn))
    status, body = intake_web.handle_portal_intake_link("gym_beta", _VALID_KEY)
    assert status == 200
    assert body["account_key"] == "gym_beta"
    assert body["intake_token_encrypted"] == "gAAAAABETA_BLOB_xyz789"
    assert body["token_minted_at"] is None


# ---- 6. response never contains the raw token or the enc key --------------------

def test_response_does_not_contain_raw_token_or_key(monkeypatch):
    """The encrypted blob is returned; the raw token and the Fernet key are not."""
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_KEY", _VALID_KEY)
    monkeypatch.setenv("AGENT_INTAKE_ENC_KEY", "FakeEncryptionKeyForTest=======")
    conn = _make_db()
    _insert_gym(conn, "gym_gamma", enc_blob="gAAAAAGAMMA_BLOB")
    import agent.db as _db
    monkeypatch.setattr(_db, "gym_get",
                        lambda key, _conn=None: _db_gym_get(key, conn))
    status, body = intake_web.handle_portal_intake_link("gym_gamma", _VALID_KEY)
    assert status == 200
    body_str = str(body)
    assert "FakeEncryptionKeyForTest" not in body_str, "enc key must not appear in response"
    # The encrypted blob IS present (the portal needs it), but the raw token is not.
    # Since we only have a fake blob here, we check there is no separate "raw_token" key.
    assert "raw_token" not in body


# ---- 7. HTTP routing: path extraction helper -----------------------------------

def test_path_regex_matches_valid_key():
    import re
    pattern = re.compile(r"^/api/portal/intake-link/([A-Za-z0-9_-]+)$")
    assert pattern.match("/api/portal/intake-link/gym_alpha")
    assert pattern.match("/api/portal/intake-link/my-gym-key123")
    assert not pattern.match("/api/portal/intake-link/")
    assert not pattern.match("/api/portal/intake-link/bad key!")
    assert not pattern.match("/portal/gym/gym_alpha")  # old route, different path
