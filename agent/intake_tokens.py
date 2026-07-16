"""
Intake token status stub (Stage 2 T4 scaffold).

A real implementation reads the per-client intake token from the kv store
(key: intake_token_<account_key>) and reports whether it is present.  The
actual token value is NEVER returned, printed, or logged.  This module only
reports whether a token has been minted and recorded; the token itself is
always set by hand, never here.

token_status(account_key) is the only public surface: it returns a dict with
a single bool field, "minted", that is True when the kv store has a non-empty
intake token recorded for this account.
"""

from . import db as _db


def token_status(account_key: str) -> dict:
    """
    Check whether an intake token has been minted for account_key.

    Returns:
        {"minted": bool}

    Never reads the token value; never prints it; never logs it.
    The minted field is True only when the kv store contains a non-empty
    value for intake_token_<account_key>.
    """
    kv_key = f"intake_token_{account_key}"
    val = _db.kv_get(kv_key, "")
    return {"minted": bool(val)}
