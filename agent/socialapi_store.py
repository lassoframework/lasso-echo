"""
Per-account SocialAPI.ai identifiers, stored at rest in the kv table.

What lives here:
  - the gym's SocialAPI brand id (one brand per gym; IG + FB grouped under it)
  - the connected account ids SocialAPI returns per platform (instagram / facebook)
  - a small connection-status cache the portal reads

The SocialAPI API KEY never lives here. It is read by name from env in
socialapi_client only, never stored, never logged.

At-rest encryption: when AGENT_SOCIALAPI_ENC_KEY is set, values are Fernet
encrypted before they touch the kv table (same pattern as intake token
encryption). When it is unset, values are stored in plaintext (dev mode). Reads
transparently handle both, so turning encryption on later never strands old rows.
"""

import json

from . import config, db


def _fernet():
    """A Fernet built from AGENT_SOCIALAPI_ENC_KEY, or None when unset / the
    cryptography package is unavailable. Never raises."""
    import os
    raw = os.environ.get(config.SOCIALAPI_ENC_KEY_ENV, "")
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except Exception:
        return None


_ENC_PREFIX = "enc:"


def _encrypt(value):
    """Encrypt a string for at-rest storage. Falls back to plaintext when no key
    is configured. The token/id is never logged either way."""
    f = _fernet()
    if f is None:
        return value
    try:
        return _ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")
    except Exception:
        return value


def _decrypt(stored):
    """Decrypt a stored value. Plaintext (no prefix) passes straight through, so
    rows written before encryption was armed still read correctly."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    f = _fernet()
    if f is None:
        # Encrypted at rest but the key is gone: refuse to guess, return empty.
        return ""
    try:
        return f.decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


# ---- brand id --------------------------------------------------------------

def _brand_key(account_key):
    return f"socialapi_brand_id_{account_key}"


def set_brand_id(account_key, brand_id):
    db.kv_set(_brand_key(account_key), _encrypt(str(brand_id)))


def get_brand_id(account_key):
    return _decrypt(db.kv_get(_brand_key(account_key), ""))


# ---- connected account ids (per platform) ----------------------------------

def _accounts_key(account_key):
    return f"socialapi_accounts_{account_key}"


def set_account_id(account_key, platform, sapi_account_id):
    """Record the SocialAPI account id for one platform ('instagram'/'facebook')
    under this gym. Merges into the existing map so IG and FB coexist."""
    current = _load_accounts(account_key)
    current[str(platform)] = str(sapi_account_id)
    db.kv_set(_accounts_key(account_key), _encrypt(json.dumps(current)))


def get_account_id(account_key, platform):
    return _load_accounts(account_key).get(str(platform), "")


def _load_accounts(account_key):
    raw = _decrypt(db.kv_get(_accounts_key(account_key), ""))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---- connection status cache (portal read) ---------------------------------

def _status_key(account_key):
    return f"socialapi_connstatus_{account_key}"


def set_connection_status(account_key, status_map):
    """status_map: {"instagram": "connected"|"disconnected"|"expired", ...}.
    Encrypted at rest like the brand id / account ids, honoring the module's
    single at-rest promise (plaintext only when no enc key is set)."""
    db.kv_set(_status_key(account_key), _encrypt(json.dumps(status_map)))


def get_connection_status(account_key):
    raw = _decrypt(db.kv_get(_status_key(account_key), ""))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
