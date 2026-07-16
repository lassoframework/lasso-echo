"""
Intake token store: mint, rotate, revoke, and look up per-gym routing tokens.

SECURITY MODEL
--------------
Raw token  : 32 random bytes, URL-safe base64 (secrets.token_urlsafe(32)).
             Shown to the gym ONCE on mint. Never stored anywhere.
Stored     : SHA-256 hex digest of the raw token only.
Lookup     : hash the inbound token and compare to the stored hash with
             hmac.compare_digest (constant-time; no timing oracle).

This is a routing token given to the gym so they can upload content. Hashing
at rest means a database leak cannot be replayed to impersonate a gym.

Flag gate  : AGENT_ONBOARD_AUTOMINT (default OFF). Every public function checks
             this flag first. When OFF the caller gets None or a clear exception;
             the AGENT_INTAKE_TOKEN_<KEY> env vars remain the sole authority.

THE ONE HUMAN LINE: the Meta publishing token is set by Blake by hand only.
Onboarding NEVER creates, reads, prints, or infers it. publish_creds_status is
recorded as NOT SET (by hand) and is never touched here.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

from . import config


# ---- Reversible encryption helpers (AGENT_INTAKE_ENC_KEY) --------------------

def _fernet_encrypt(raw_token: str) -> str | None:
    """Encrypt raw_token with Fernet if AGENT_INTAKE_ENC_KEY is set.
    Returns the encrypted string, or None when the key is absent (dev mode)."""
    key_str = os.environ.get(config.INTAKE_ENC_KEY_ENV, "").strip()
    if not key_str:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key_str.encode()).encrypt(raw_token.encode()).decode()
    except Exception:
        return None


def decrypt_token(account_key: str, db_conn=None) -> str | None:
    """Recover the raw token for account_key via Fernet decryption.
    Returns the raw token string, or None when the key is absent or the
    encrypted blob is missing. Never raises."""
    key_str = os.environ.get(config.INTAKE_ENC_KEY_ENV, "").strip()
    if not key_str:
        return None
    conn, owned = _connect(db_conn)
    try:
        row = conn.execute(
            "SELECT intake_token_encrypted FROM gyms WHERE account_key = ?",
            (account_key,)
        ).fetchone()
        if row is None:
            return None
        enc = row["intake_token_encrypted"] if hasattr(row, "__getitem__") else None
        if not enc:
            return None
        try:
            from cryptography.fernet import Fernet
            return Fernet(key_str.encode()).decrypt(enc.encode()).decode()
        except Exception:
            return None
    finally:
        if owned:
            conn.close()


def _flag_check():
    """Raise RuntimeError when the automint flag is OFF."""
    if not config.onboard_automint_enabled():
        raise RuntimeError(
            "AGENT_ONBOARD_AUTOMINT is OFF. Set AGENT_ONBOARD_AUTOMINT=true to arm "
            "the intake token store. AGENT_INTAKE_TOKEN_<KEY> env vars remain "
            "authoritative while the flag is OFF."
        )


def _connect(db_conn=None):
    """Return an open connection. If db_conn is a string path (e.g. ':memory:'),
    open it with schema migration. If it is already an open connection, return
    it as-is. If None, use the default db.connect()."""
    if db_conn is None:
        from . import db
        return db.connect(), True
    if isinstance(db_conn, str):
        from . import db
        return db.connect(db_conn), True
    return db_conn, False


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint(account_key: str, db_conn=None) -> str:
    """Mint a new 32-byte URL-safe token for the gym. Store the SHA-256 hash.
    Return the RAW token (shown once; never stored).

    Raises ValueError if the gym already has a non-revoked token (use rotate instead).
    Raises RuntimeError when AGENT_ONBOARD_AUTOMINT is OFF.
    Writes audit: kind=intake_token_mint.
    """
    _flag_check()
    conn, owned = _connect(db_conn)
    try:
        existing = conn.execute(
            "SELECT intake_token_hash, token_revoked FROM gyms WHERE account_key = ?",
            (account_key,)
        ).fetchone()
        if existing and existing["intake_token_hash"] and not existing["token_revoked"]:
            raise ValueError(
                f"Gym {account_key!r} already has an active token; use rotate to replace it."
            )
        raw = secrets.token_urlsafe(32)
        token_hash = _sha256(raw)
        enc = _fernet_encrypt(raw)  # None when AGENT_INTAKE_ENC_KEY not set
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO gyms (account_key, intake_token_hash, token_rotated_at,
                              token_revoked, intake_token_encrypted,
                              publish_creds_status, updated_at)
            VALUES (?, ?, ?, 0, ?, 'NOT SET (by hand)', datetime('now'))
            ON CONFLICT(account_key) DO UPDATE SET
                intake_token_hash = excluded.intake_token_hash,
                token_rotated_at = excluded.token_rotated_at,
                token_revoked = 0,
                intake_token_encrypted = excluded.intake_token_encrypted,
                updated_at = datetime('now')
            """,
            (account_key, token_hash, now, enc)
        )
        conn.commit()
        # Audit: kind, subject, reason — never log the raw token or hash
        try:
            from . import db as _db
            _db.audit(
                kind="intake_token_mint",
                subject=account_key,
                reason="initial mint",
                account_key=account_key,
            )
        except Exception:
            pass
        return raw
    finally:
        if owned:
            conn.close()


def rotate(account_key: str, db_conn=None) -> str:
    """Mint a new token for the gym, replacing the old hash immediately.
    Returns the new raw token (shown once; never stored).

    Raises RuntimeError when AGENT_ONBOARD_AUTOMINT is OFF.
    Writes audit: kind=intake_token_rotate.
    """
    _flag_check()
    conn, owned = _connect(db_conn)
    try:
        raw = secrets.token_urlsafe(32)
        token_hash = _sha256(raw)
        enc = _fernet_encrypt(raw)
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO gyms (account_key, intake_token_hash, token_rotated_at,
                              token_revoked, intake_token_encrypted,
                              publish_creds_status, updated_at)
            VALUES (?, ?, ?, 0, ?, 'NOT SET (by hand)', datetime('now'))
            ON CONFLICT(account_key) DO UPDATE SET
                intake_token_hash = excluded.intake_token_hash,
                token_rotated_at = excluded.token_rotated_at,
                token_revoked = 0,
                intake_token_encrypted = excluded.intake_token_encrypted,
                updated_at = datetime('now')
            """,
            (account_key, token_hash, now, enc)
        )
        conn.commit()
        try:
            from . import db as _db
            _db.audit(
                kind="intake_token_rotate",
                subject=account_key,
                reason="rotate",
                account_key=account_key,
            )
        except Exception:
            pass
        return raw
    finally:
        if owned:
            conn.close()


def revoke(account_key: str, db_conn=None) -> None:
    """Set token_revoked=1 and clear intake_token_hash. The gym can no longer upload.

    Raises RuntimeError when AGENT_ONBOARD_AUTOMINT is OFF.
    Writes audit: kind=intake_token_revoke.
    """
    _flag_check()
    conn, owned = _connect(db_conn)
    try:
        conn.execute(
            """
            INSERT INTO gyms (account_key, intake_token_hash, token_revoked,
                              intake_token_encrypted, publish_creds_status, updated_at)
            VALUES (?, NULL, 1, NULL, 'NOT SET (by hand)', datetime('now'))
            ON CONFLICT(account_key) DO UPDATE SET
                intake_token_hash = NULL,
                token_revoked = 1,
                intake_token_encrypted = NULL,
                updated_at = datetime('now')
            """,
            (account_key,)
        )
        conn.commit()
        try:
            from . import db as _db
            _db.audit(
                kind="intake_token_revoke",
                subject=account_key,
                reason="revoked by operator",
                account_key=account_key,
            )
        except Exception:
            pass
    finally:
        if owned:
            conn.close()


def client_for_token_data(token: str, db_conn=None):
    """Given a raw token, return the account_key of the gym it belongs to, or None.

    Uses hmac.compare_digest for constant-time comparison (no timing oracle).
    Returns None if revoked or not found.
    """
    if not token:
        return None
    token_hash = _sha256(token)
    conn, owned = _connect(db_conn)
    try:
        rows = conn.execute(
            "SELECT account_key, intake_token_hash, token_revoked FROM gyms "
            "WHERE intake_token_hash IS NOT NULL AND token_revoked = 0"
        ).fetchall()
        for row in rows:
            stored = row["intake_token_hash"] or ""
            if hmac.compare_digest(stored, token_hash):
                return row["account_key"]
        return None
    finally:
        if owned:
            conn.close()


def token_status(account_key: str, db_conn=None) -> dict:
    """Returns dict: {account_key, status: ACTIVE|REVOKED|NOT_SET, rotated_at: ISO or None}"""
    conn, owned = _connect(db_conn)
    try:
        row = conn.execute(
            "SELECT intake_token_hash, token_revoked, token_rotated_at "
            "FROM gyms WHERE account_key = ?",
            (account_key,)
        ).fetchone()
        if row is None:
            return {"account_key": account_key, "status": "NOT_SET", "rotated_at": None}
        if row["token_revoked"]:
            return {
                "account_key": account_key,
                "status": "REVOKED",
                "rotated_at": row["token_rotated_at"],
            }
        if row["intake_token_hash"]:
            return {
                "account_key": account_key,
                "status": "ACTIVE",
                "rotated_at": row["token_rotated_at"],
            }
        return {"account_key": account_key, "status": "NOT_SET", "rotated_at": None}
    finally:
        if owned:
            conn.close()
