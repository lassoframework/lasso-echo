"""
Intake token data store (Stage 2 T3).

Stub module: provides client_for_token_data() and token_status() for the
onboarding auto-mint path. The raw token is NEVER stored here; only the
upload link (built at mint time) and a status flag live in the gyms table.

The gyms table schema lives in db.py. A token is identified by its SHA-256
fingerprint (the first lookup key). Status values: ACTIVE, REVOKED, NOT_SET.

This module is imported lazily inside intake_web.client_for_token() ONLY when
AGENT_ONBOARD_AUTOMINT is ON; the flag-OFF path is byte-identical to today.
"""

import hashlib


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_for_token_data(token: str, db_conn=None) -> "str | None":
    """
    Look up the client key for a raw token via the gyms table.

    Returns the account_key string when the token fingerprint matches an ACTIVE
    row. Returns None when the token is unknown, REVOKED, or the table is absent.
    The raw token is never stored or logged; only a SHA-256 fingerprint is used
    for the lookup.

    db_conn is accepted for testing (pass a sqlite3 connection directly); when
    None the module-level connect() is used (live path).
    """
    if not token:
        return None
    fingerprint = _sha256(token)
    try:
        if db_conn is not None:
            row = db_conn.execute(
                "SELECT account_key, token_status FROM gyms "
                "WHERE token_sha256=?", (fingerprint,)
            ).fetchone()
        else:
            from . import db as _db
            with _db.connect() as conn:
                row = conn.execute(
                    "SELECT account_key, token_status FROM gyms "
                    "WHERE token_sha256=?", (fingerprint,)
                ).fetchone()
        if row is None:
            return None
        status = (row["token_status"] if hasattr(row, "__getitem__") else row[1]) or ""
        if status.upper() != "ACTIVE":
            return None
        return row["account_key"] if hasattr(row, "__getitem__") else row[0]
    except Exception:
        # Table absent or any db error: fall through to env-var lookup.
        return None


def token_status(account_key: str, db_conn=None) -> dict:
    """
    Return the intake token status dict for an account.

    Shape: {account_key, intake_status, upload_link, last_upload_at, upload_count}
    intake_status is one of: ACTIVE, REVOKED, NOT_SET.
    upload_link is the stored link string or None (raw token never stored).
    last_upload_at and upload_count are None (R2 metadata; filled by the caller).
    """
    result = {
        "account_key": account_key,
        "intake_status": "NOT_SET",
        "upload_link": None,
        "last_upload_at": None,
        "upload_count": None,
    }
    try:
        if db_conn is not None:
            row = db_conn.execute(
                "SELECT token_status, upload_link FROM gyms WHERE account_key=?",
                (account_key,)
            ).fetchone()
        else:
            from . import db as _db
            with _db.connect() as conn:
                row = conn.execute(
                    "SELECT token_status, upload_link FROM gyms WHERE account_key=?",
                    (account_key,)
                ).fetchone()
        if row is not None:
            status = (row["token_status"] if hasattr(row, "__getitem__") else row[0]) or "NOT_SET"
            link = (row["upload_link"] if hasattr(row, "__getitem__") else row[1]) or None
            result["intake_status"] = status.upper() or "NOT_SET"
            result["upload_link"] = link
    except Exception:
        pass
    return result
