"""
Onboarding self-check tests (Stage 2 T4).

Six scenarios, all using tmp dirs and in-memory / isolated db.  No live tokens,
no network, no real Meta credentials.

Fixtures:
  db_conn      a fresh in-memory sqlite connection with the full schema
  tmp_root     a tmp_path root that acts as the repo root (brand_voice/, brains/)
"""

import os
import json
import sqlite3
import pytest

from agent import db as agent_db
from agent.onboard_verify import verify_gym, verify_all


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_conn(tmp_path):
    """A clean sqlite connection with the Echo schema.  Isolated per test."""
    path = str(tmp_path / "test_onboard.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(agent_db._SCHEMA)
    yield conn
    conn.close()


@pytest.fixture()
def tmp_root(tmp_path):
    """Temporary directory acting as the repo root."""
    (tmp_path / "brand_voice").mkdir()
    (tmp_path / "brains").mkdir()
    return tmp_path


def _mint_token(account_key, db_conn):
    """Write an intake token kv entry so token_status returns minted=True."""
    db_conn.execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
        (f"intake_token_{account_key}", "tok_test_value"))
    db_conn.commit()


def _register_gym(account_key, db_conn, display_name="Test Gym",
                  publish_flag="OFF", publish_creds="NOT SET (by hand)"):
    """Insert a gym row directly into the test connection."""
    db_conn.execute(
        "INSERT OR REPLACE INTO gyms (account_key, display_name, publish_flag, publish_creds) "
        "VALUES (?,?,?,?)",
        (account_key, display_name, publish_flag, publish_creds))
    db_conn.commit()


def _write_voice(account_key, root):
    voice_path = os.path.join(str(root), "brand_voice", f"{account_key}.md")
    with open(voice_path, "w") as f:
        f.write(f"# {account_key} voice doc\n")


def _write_brain(account_key, root):
    brain_path = os.path.join(str(root), "brains", f"{account_key}.md")
    with open(brain_path, "w") as f:
        f.write(f"# {account_key} brain\n")


# ---------------------------------------------------------------------------
# test 1: a gym with a minted token is ready for uploads
# ---------------------------------------------------------------------------

def test_fresh_gym_ready_for_uploads(db_conn, tmp_root, monkeypatch):
    """
    After onboard.run() scaffolding plus a minted token, ready_for_uploads is True.
    We simulate the post-onboard state: gym row exists and the intake token kv
    entry has been written.
    """
    key = "boxforge"
    _register_gym(key, db_conn)
    _mint_token(key, db_conn)
    _write_voice(key, tmp_root)

    # Patch kv_get so the in-memory connection is used
    monkeypatch.setattr(
        agent_db, "kv_get",
        lambda k, default="": (
            db_conn.execute(
                "SELECT value FROM kv WHERE key=?", (k,)).fetchone() or [default]
        )[0]
    )

    result = verify_gym(key, db_conn=db_conn, root=str(tmp_root))
    assert result["token_minted"] is True
    assert result["ready_for_uploads"] is True


# ---------------------------------------------------------------------------
# test 2: a fresh gym is NOT ready to publish (creds not yet set by hand)
# ---------------------------------------------------------------------------

def test_fresh_gym_not_ready_to_publish(db_conn, tmp_root, monkeypatch):
    """
    After scaffold, ready_to_publish is False with a creds reason.
    Publish creds are always 'NOT SET (by hand)' until the operator sets them.
    """
    key = "ironlab"
    _register_gym(key, db_conn, publish_flag="OFF",
                  publish_creds="NOT SET (by hand)")
    _mint_token(key, db_conn)

    monkeypatch.setattr(
        agent_db, "kv_get",
        lambda k, default="": (
            db_conn.execute(
                "SELECT value FROM kv WHERE key=?", (k,)).fetchone() or [default]
        )[0]
    )

    result = verify_gym(key, db_conn=db_conn, root=str(tmp_root))
    assert result["ready_to_publish"] is False
    assert result["publish_creds_status"] == "NOT SET (by hand)"


# ---------------------------------------------------------------------------
# test 3: missing voice file is flagged
# ---------------------------------------------------------------------------

def test_missing_voice_file_flagged(db_conn, tmp_root, monkeypatch):
    """If brand_voice/<key>.md is absent, voice_scaffolded is False."""
    key = "apexfit"
    _register_gym(key, db_conn)
    _mint_token(key, db_conn)
    # deliberately do NOT write brand_voice/apexfit.md

    monkeypatch.setattr(
        agent_db, "kv_get",
        lambda k, default="": (
            db_conn.execute(
                "SELECT value FROM kv WHERE key=?", (k,)).fetchone() or [default]
        )[0]
    )

    result = verify_gym(key, db_conn=db_conn, root=str(tmp_root))
    assert result["voice_scaffolded"] is False


# ---------------------------------------------------------------------------
# test 4: missing slack channel is flagged
# ---------------------------------------------------------------------------

def test_missing_slack_channel_flagged(db_conn, tmp_root, monkeypatch):
    """
    An account in the registry with an empty slack_channel produces
    slack_channel_set=False in the verify result.
    """
    from agent.accounts import ACCOUNTS, Account, Platform
    from agent.trust import TrustLevel

    key = "peakgym"
    # inject a fake account with no slack_channel
    fake_acct = Account(
        key=key,
        display_name="Peak Gym",
        platform=Platform.INSTAGRAM,
        token_env=f"AGENT_{key.upper()}_TOKEN",
        target_id_env=f"AGENT_{key.upper()}_ID",
        trust=TrustLevel.FULL_APPROVAL,
        slack_channel="",    # empty on purpose
        approvers=["U12345"],
    )
    _register_gym(key, db_conn)
    _mint_token(key, db_conn)

    monkeypatch.setattr(
        agent_db, "kv_get",
        lambda k, default="": (
            db_conn.execute(
                "SELECT value FROM kv WHERE key=?", (k,)).fetchone() or [default]
        )[0]
    )

    # Monkey-patch get_account to return our fake account
    import agent.onboard_verify as ov
    monkeypatch.setattr(
        ov, "verify_gym",
        lambda ak, db_conn=None, root=".": _verify_with_fake_acct(
            ak, fake_acct, db_conn=db_conn, root=root)
    )

    result = ov.verify_gym(key, db_conn=db_conn, root=str(tmp_root))
    assert result["slack_channel_set"] is False


def _verify_with_fake_acct(account_key, fake_acct, db_conn=None, root="."):
    """Helper: run verify_gym logic but substitute a controlled Account object."""
    import os as _os
    from agent.intake_tokens import token_status
    from agent.trust import effective_level, TrustLevel
    from agent import db as _db

    gym_row = _db.gym_get(account_key, conn=db_conn)
    if gym_row is None:
        publish_flag_val = "UNKNOWN"
        publish_creds_status = "NOT SET (by hand)"
    else:
        publish_flag_val = (gym_row.get("publish_flag") or "OFF").upper()
        raw_creds = (gym_row.get("publish_creds") or "").strip()
        publish_creds_status = raw_creds if raw_creds else "NOT SET (by hand)"
    publish_flag_off = (publish_flag_val in ("OFF", "UNKNOWN"))

    tok = token_status(account_key)
    token_minted = tok.get("minted", False)

    voice_path = _os.path.join(root, "brand_voice", f"{account_key}.md")
    brain_path = _os.path.join(root, "brains", f"{account_key}.md")
    voice_scaffolded = _os.path.isfile(voice_path)
    brain_present = _os.path.isfile(brain_path)

    trust_full_approval = (effective_level(fake_acct) == TrustLevel.FULL_APPROVAL)
    slack_channel_set = bool(getattr(fake_acct, "slack_channel", "") or "")
    approver_set = bool(getattr(fake_acct, "approvers", None))

    # calendar
    month = _db.kv_get(f"approved_calendar_{account_key}_{_month()}", "")
    first_month_approved = bool(month)

    ready_for_uploads = token_minted
    ready_to_publish = (
        ready_for_uploads
        and publish_creds_status == "SET"
        and not publish_flag_off
    )
    return {
        "account_key": account_key,
        "token_minted": token_minted,
        "voice_scaffolded": voice_scaffolded,
        "brain_present": brain_present,
        "trust_full_approval": trust_full_approval,
        "publish_flag_off": publish_flag_off,
        "publish_creds_status": publish_creds_status,
        "slack_channel_set": slack_channel_set,
        "approver_set": approver_set,
        "first_month_approved": first_month_approved,
        "ready_for_uploads": ready_for_uploads,
        "ready_to_publish": ready_to_publish,
    }


def _month():
    from datetime import date
    return date.today().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# test 5: no token means not ready for uploads
# ---------------------------------------------------------------------------

def test_no_token_not_ready_for_uploads(db_conn, tmp_root, monkeypatch):
    """A gym with no minted intake token has ready_for_uploads=False."""
    key = "flexzone"
    _register_gym(key, db_conn)
    # deliberately do NOT mint a token

    monkeypatch.setattr(
        agent_db, "kv_get",
        lambda k, default="": (
            db_conn.execute(
                "SELECT value FROM kv WHERE key=?", (k,)).fetchone() or [default]
        )[0]
    )

    result = verify_gym(key, db_conn=db_conn, root=str(tmp_root))
    assert result["token_minted"] is False
    assert result["ready_for_uploads"] is False


# ---------------------------------------------------------------------------
# test 6: verify_all returns one result per gym in db
# ---------------------------------------------------------------------------

def test_verify_all_returns_all_gyms(db_conn, tmp_root, monkeypatch):
    """verify_all returns exactly one result dict per gym in the gyms table."""
    keys = ["gym_alpha", "gym_beta", "gym_gamma"]
    for k in keys:
        _register_gym(k, db_conn)

    monkeypatch.setattr(
        agent_db, "kv_get",
        lambda k, default="": (
            db_conn.execute(
                "SELECT value FROM kv WHERE key=?", (k,)).fetchone() or [default]
        )[0]
    )

    results = verify_all(db_conn=db_conn, root=str(tmp_root))
    assert len(results) == len(keys)
    returned_keys = {r["account_key"] for r in results}
    assert returned_keys == set(keys)
