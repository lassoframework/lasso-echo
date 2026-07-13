"""
Signed intake tokens: one shared secret mints every gym's link, so we NEVER set a
per-gym AGENT_INTAKE_TOKEN_<CLIENTKEY> env var again.

A token is  b64url(client_key) + "." + b64url(sig)  where sig is
HMAC-SHA256(client_key) under AGENT_INTAKE_SIGNING_SECRET, truncated to 160 bits.
The whole token stays inside [A-Za-z0-9_.-], the charset the intake route regex
accepts. verify() recomputes the HMAC and compares in CONSTANT TIME: a good
signature yields the client key, anything else yields None (a 404, never a crash).

The secret lives ONLY on the intake-web / listener service, read lazily BY NAME
from config, never stored on an object, never logged. This module is pure: mint
and verify do no I/O beyond reading the secret env var, which is what makes them
trivially offline-testable and safe to call on every request.

Revocation (a per-gym kill switch) is a SEPARATE concern layered on top in
intake_web via an R2 denylist, because this service touches R2 only, never /data.
Minting is centralized here so a future authenticated mint endpoint reuses the
exact same code path as the CLI, without the caller ever holding the secret.
"""

import base64
import hashlib
import hmac
import os

from . import config

# 160-bit signature: forgery-resistant and compact. HMAC-SHA256 is 32 bytes; we
# keep the leading 20 (160 bits), well past the 128-bit floor for a MAC tag.
_SIG_BYTES = 20
_SEP = "."


def _secret():
    """The signing secret bytes, or None when unset. Read lazily BY NAME every
    call (so a rotation takes effect without a reimport); never logged, never
    stored on an object."""
    raw = os.environ.get(config.INTAKE_SIGNING_SECRET_ENV, "")
    return raw.encode("utf-8") if raw else None


def secret_present():
    """True when the shared signing secret is set. For the watchdog/doctor probes;
    reveals presence only, never the value."""
    return _secret() is not None


def _b64url(raw):
    """URL-safe base64, padding stripped ('=' is outside the token charset)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sig_for(client_key, secret):
    return hmac.new(secret, client_key.encode("utf-8"),
                    hashlib.sha256).digest()[:_SIG_BYTES]


def mint(client_key, secret=None):
    """The signed token for a client key. The client key is normalized to lower
    case (env-suffix convention) BEFORE signing, so mint and verify agree. Raises
    ValueError on a missing client key or missing secret (a caller/config bug, not
    a request error)."""
    client_key = (client_key or "").strip().lower()
    if not client_key:
        raise ValueError("client_key is required")
    secret = secret if secret is not None else _secret()
    if not secret:
        raise ValueError(f"{config.INTAKE_SIGNING_SECRET_ENV} is not set")
    sig = _sig_for(client_key, secret)
    return f"{_b64url(client_key.encode('utf-8'))}{_SEP}{_b64url(sig)}"


def verify(token, secret=None):
    """The client key a signed token authenticates, or None. None on: no secret,
    malformed token, or a bad signature. CONSTANT-TIME compare. Never raises on a
    bad token (that is a 404, not a crash); never logs the token."""
    secret = secret if secret is not None else _secret()
    if not secret or not token or _SEP not in token:
        return None
    key_part, _, sig_part = token.partition(_SEP)
    if not key_part or not sig_part:
        return None
    try:
        client_key = _b64url_decode(key_part).decode("utf-8")
        got_sig = _b64url_decode(sig_part)
    except Exception:
        return None
    if not client_key:
        return None
    want_sig = _sig_for(client_key, secret)
    if not hmac.compare_digest(got_sig, want_sig):
        return None
    return client_key
