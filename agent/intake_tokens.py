"""
Intake token stub (Track 2 local copy).

The real implementation is being built in Track 1 (agent/intake_tokens.py).
This stub satisfies the interface so Track 2 tests can run without T1.

Interface:
  mint(account_key, db_conn=None) -> str  (44-char token string)
  rotate(account_key, db_conn=None) -> str
  revoke(account_key, db_conn=None) -> None
  token_status(account_key, db_conn=None) -> dict
    keys: account_key, has_token, revoked, token_prefix (first 6 chars or "")

NEVER logs, prints, or stores a raw token value anywhere.
"""

import hashlib
import os
import time


def _fake_token(account_key: str) -> str:
    """Deterministic 44-char URL-safe string from account_key + a random nonce.
    Not cryptographically safe for production; this is a test stub only."""
    nonce = os.urandom(16).hex()
    raw = f"{account_key}:{nonce}:{time.time()}"
    digest = hashlib.sha256(raw.encode()).hexdigest()  # 64 hex chars
    # Return 44-char URL-safe base64-ish slice (letters + digits only, no dashes)
    safe = (digest + hashlib.md5(raw.encode()).hexdigest())[:44]
    return safe


def mint(account_key: str, db_conn=None) -> str:
    """Create and return a new intake token for account_key.
    If a live token already exists, return the existing one (idempotent).
    The raw token is returned to the caller ONLY; never stored in a file."""
    from . import db
    existing = db.kv_get(f"intake_token_active_{account_key}", "")
    if existing:
        return existing
    token = _fake_token(account_key)
    db.kv_set(f"intake_token_active_{account_key}", token)
    return token


def rotate(account_key: str, db_conn=None) -> str:
    """Revoke the current token and mint a new one."""
    revoke(account_key, db_conn=db_conn)
    return mint(account_key, db_conn=db_conn)


def revoke(account_key: str, db_conn=None) -> None:
    """Mark the token as revoked. Clears the active token kv entry."""
    from . import db
    db.kv_set(f"intake_token_active_{account_key}", "")
    db.kv_set(f"intake_token_revoked_{account_key}", "1")


def token_status(account_key: str, db_conn=None) -> dict:
    """Return status dict. Never includes the raw token value."""
    from . import db
    active = db.kv_get(f"intake_token_active_{account_key}", "")
    revoked = db.kv_get(f"intake_token_revoked_{account_key}", "")
    return {
        "account_key": account_key,
        "has_token": bool(active),
        "revoked": bool(revoked and not active),
        "token_prefix": active[:6] if active else "",
    }
