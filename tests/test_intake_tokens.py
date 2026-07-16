"""
Intake token store tests.

All tests use in-memory or tmp SQLite so no real /data is touched, and no real
tokens are ever printed or stored as raw values.
"""

import hashlib
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, intake_tokens  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_conn(tmp_path):
    """Open a test-isolated SQLite connection with the full schema."""
    path = str(tmp_path / "tokens_test.db")
    return db.connect(path)


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_mint_returns_raw_token_and_stores_hash(monkeypatch, tmp_path):
    """mint() returns a non-empty string; the db stores the SHA-256 hash, NOT the raw value."""
    _arm(monkeypatch)
    conn = _fresh_conn(tmp_path)
    raw = intake_tokens.mint("gym_alpha", db_conn=conn)

    assert raw and len(raw) > 10, "raw token must be non-empty"

    row = conn.execute(
        "SELECT intake_token_hash FROM gyms WHERE account_key = 'gym_alpha'"
    ).fetchone()
    assert row is not None, "gym row must exist after mint"

    stored_hash = row["intake_token_hash"]
    expected_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert stored_hash == expected_hash, "stored value must be the SHA-256 hash"
    assert stored_hash != raw, "the raw token must not be stored"
    conn.close()


def test_rotate_invalidates_old_hash(monkeypatch, tmp_path):
    """After rotate(), the old hash no longer resolves via client_for_token_data."""
    _arm(monkeypatch)
    conn = _fresh_conn(tmp_path)

    raw_old = intake_tokens.mint("gym_beta", db_conn=conn)
    raw_new = intake_tokens.rotate("gym_beta", db_conn=conn)

    assert raw_old != raw_new, "rotate must produce a new token"

    result_old = intake_tokens.client_for_token_data(raw_old, db_conn=conn)
    assert result_old is None, "old token must not resolve after rotate"

    result_new = intake_tokens.client_for_token_data(raw_new, db_conn=conn)
    assert result_new == "gym_beta", "new token must resolve to the gym"
    conn.close()


def test_revoke_clears_lookup(monkeypatch, tmp_path):
    """After revoke(), client_for_token_data returns None for any token."""
    _arm(monkeypatch)
    conn = _fresh_conn(tmp_path)

    raw = intake_tokens.mint("gym_gamma", db_conn=conn)
    assert intake_tokens.client_for_token_data(raw, db_conn=conn) == "gym_gamma"

    intake_tokens.revoke("gym_gamma", db_conn=conn)
    assert intake_tokens.client_for_token_data(raw, db_conn=conn) is None
    conn.close()


def test_client_for_token_data_constant_time(monkeypatch, tmp_path):
    """client_for_token_data must use hmac.compare_digest for constant-time comparison."""
    source = inspect.getsource(intake_tokens.client_for_token_data)
    assert "hmac.compare_digest" in source, (
        "client_for_token_data must use hmac.compare_digest for constant-time comparison"
    )


def test_double_mint_raises(monkeypatch, tmp_path):
    """Minting twice on the same account without --rotate raises ValueError."""
    _arm(monkeypatch)
    conn = _fresh_conn(tmp_path)

    intake_tokens.mint("gym_delta", db_conn=conn)
    with pytest.raises(ValueError, match="already has an active token"):
        intake_tokens.mint("gym_delta", db_conn=conn)
    conn.close()


def test_flag_off_mint_raises_or_returns_none(monkeypatch, tmp_path):
    """When AGENT_ONBOARD_AUTOMINT=false, mint() raises RuntimeError (flag-off guard)."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "false")
    conn = _fresh_conn(tmp_path)

    with pytest.raises(RuntimeError, match="AGENT_ONBOARD_AUTOMINT is OFF"):
        intake_tokens.mint("gym_epsilon", db_conn=conn)
    conn.close()


def test_token_status_active_revoked(monkeypatch, tmp_path):
    """token_status returns ACTIVE after mint, REVOKED after revoke."""
    _arm(monkeypatch)
    conn = _fresh_conn(tmp_path)

    intake_tokens.mint("gym_zeta", db_conn=conn)
    st_before = intake_tokens.token_status("gym_zeta", db_conn=conn)
    assert st_before["status"] == "ACTIVE"
    assert st_before["rotated_at"] is not None

    intake_tokens.revoke("gym_zeta", db_conn=conn)
    st_after = intake_tokens.token_status("gym_zeta", db_conn=conn)
    assert st_after["status"] == "REVOKED"

    conn.close()


# ---- Encryption at rest (AGENT_INTAKE_ENC_KEY) --------------------------------

def test_encrypted_token_stored_when_key_set(monkeypatch, tmp_path):
    """When AGENT_INTAKE_ENC_KEY is set, mint stores an encrypted blob."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_ENC_KEY", key)
    conn = _fresh_conn(tmp_path)

    raw = intake_tokens.mint("gym_enc", db_conn=conn)
    row = conn.execute(
        "SELECT intake_token_encrypted FROM gyms WHERE account_key='gym_enc'"
    ).fetchone()
    assert row is not None
    assert row["intake_token_encrypted"] is not None, "encrypted blob must be stored"

    recovered = intake_tokens.decrypt_token("gym_enc", db_conn=conn)
    assert recovered == raw, "decrypt_token must recover the original raw token"
    conn.close()


def test_no_encrypted_blob_when_key_not_set(monkeypatch, tmp_path):
    """Without AGENT_INTAKE_ENC_KEY, intake_token_encrypted stays NULL."""
    _arm(monkeypatch)
    monkeypatch.delenv("AGENT_INTAKE_ENC_KEY", raising=False)
    conn = _fresh_conn(tmp_path)

    intake_tokens.mint("gym_noenc", db_conn=conn)
    row = conn.execute(
        "SELECT intake_token_encrypted FROM gyms WHERE account_key='gym_noenc'"
    ).fetchone()
    assert row is not None
    assert row["intake_token_encrypted"] is None, "no encrypted blob without key"
    assert intake_tokens.decrypt_token("gym_noenc", db_conn=conn) is None
    conn.close()


def test_revoke_clears_encrypted_blob(monkeypatch, tmp_path):
    """After revoke(), intake_token_encrypted is NULL so the link cannot be recovered."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_ENC_KEY", key)
    conn = _fresh_conn(tmp_path)

    intake_tokens.mint("gym_revenc", db_conn=conn)
    assert intake_tokens.decrypt_token("gym_revenc", db_conn=conn) is not None
    intake_tokens.revoke("gym_revenc", db_conn=conn)
    assert intake_tokens.decrypt_token("gym_revenc", db_conn=conn) is None
    conn.close()
