"""tests/test_ops_alerts_scrub_patterns.py — audit item 10, 2026-08-31.

ops_alerts.scrub() used to redact ONLY the values of this process's own
secret-looking env vars. A secret arriving inside a THIRD-PARTY response body —
a provider quoting the key it rejected, an upstream error echoing its own
Authorization header, a signed URL in a traceback — was never in os.environ, so
it went to Slack in the clear.

These tests pin the shape-based pass: credentials are redacted by what they LOOK
like, not only by whether we happen to hold them. They also pin the two things
that make eager redaction safe to ship: ordinary operational text (gym ids,
UUIDs, file names, plain words) survives, and the bearer rule runs BEFORE the
named-field rule so "Authorization: Bearer <token>" never ships the token.

Fully offline: scrub() is pure.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ops_alerts  # noqa: E402

R = ops_alerts.REDACTED


# ---- 1. the class the audit found: a secret we do NOT hold -------------------

def test_third_party_body_secret_is_redacted_even_though_not_in_env(monkeypatch):
    """The whole point: no env var carries this value, so the old env-value pass
    was blind to it."""
    monkeypatch.delenv("SOME_TOKEN", raising=False)
    leaked = "sk-" + "liveAAAAAAAABBBBBBBBCCCCCCCCDDDDDDDD"   # fabricated, assembled
    body = f'upstream 401: {{"error":"invalid key","key":"{leaked}"}}'
    out = ops_alerts.scrub(body)
    assert leaked not in out
    assert R in out


# NOTE: every "secret" below is fabricated, but the prefixes are exactly the ones
# real credentials use — which is the point, and also why GitHub's push-protection
# scanner blocks the file if they appear as literals. Each fixture is therefore
# assembled at runtime from a prefix and a body: same string reaches scrub(), no
# credential-shaped literal is ever committed.
@pytest.mark.parametrize("prefix,body", [
    ("sk-", "AAAAAAAABBBBBBBBCCCCCCCC"),
    ("sk_live_", "51AbCdEfGhIjKlMnOp"),
    ("pk_test_", "51AbCdEfGhIjKlMnOp"),
    ("xox" + "b-", "1234567890-1234567890-AbCdEfGhIjKlMnOpQrSt"),
    ("xox" + "p-", "9876543210-AbCdEfGhIjKl"),
    ("xapp-", "1-A01234567-1234567890-abcdef"),
    ("gh" + "p_", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
    ("github_" + "pat_", "11ABCDEFG0abcdefghijklmnop"),
    ("AK" + "IA", "IOSFODNN7EXAMPLE"),
    ("AIza", "SyA1B2C3D4E5F6G7H8I9J0KlMnOpQrStU"),
    ("ya29.", "a0AfH6SMAbCdEfGhIjKlMnOpQrStUvWxYz"),
    ("apify_" + "api_", "AbCdEfGhIjKlMnOpQrStUvWx"),
    ("gl" + "pat-", "AbCdEfGhIjKlMnOpQrSt"),
])
def test_provider_prefixed_keys_are_redacted(prefix, body):
    secret = prefix + body
    out = ops_alerts.scrub(f"publish failed: rejected credential {secret} (401)")
    assert secret not in out
    assert R in out


def test_meta_graph_token_is_redacted():
    tok = "EAA" + "Bc1DeF2gH3iJ4kL5mN6oP7qR8sT9uV0wX1yZ2aB3cD4eF5gH6i"
    out = ops_alerts.scrub(f"graph error for lasso_fb: token {tok} expired")
    assert tok not in out
    assert "lasso_fb" in out          # the useful part of the alert survives


def test_jwt_is_redacted():
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
           "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkVjaG8ifQ."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    out = ops_alerts.scrub(f"supabase said: JWT {jwt} is malformed")
    assert jwt not in out
    assert R in out


# ---- 2. bearer / basic, and the ORDER that makes it correct ------------------

def test_bearer_token_is_redacted():
    out = ops_alerts.scrub("retry with header Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert R in out


def test_authorization_bearer_never_ships_the_token():
    """REGRESSION GUARD on rule order. If the named-field rule ran first it would
    match 'Authorization: Bearer' and redact the WORD 'Bearer', leaving the token
    itself in the message."""
    tok = "abcdefghijklmnopqrstuvwxyz0123456789"
    out = ops_alerts.scrub(f"request failed. Authorization: Bearer {tok}")
    assert tok not in out


def test_basic_auth_is_redacted():
    out = ops_alerts.scrub("Basic ZWNobzpzdXBlcnNlY3JldHBhc3N3b3Jk")
    assert "ZWNobzpzdXBlcnNlY3JldHBhc3N3b3Jk" not in out


# ---- 3. named credential fields ----------------------------------------------

@pytest.mark.parametrize("field", [
    "token", "api_key", "apikey", "access_token", "refresh_token",
    "client_secret", "service_key", "password", "secret", "signature",
])
def test_named_credential_fields_are_redacted(field):
    out = ops_alerts.scrub(f"callback url ...?{field}=hunter2supersecretvalue&gym=eng")
    assert "hunter2supersecretvalue" not in out
    assert "gym=eng" in out           # the diagnostic context survives


def test_json_style_named_field_is_redacted():
    out = ops_alerts.scrub('body: {"access_token": "s3cr3tvaluegoeshere", "ok": true}')
    assert "s3cr3tvaluegoeshere" not in out


# ---- 4. long hex / base64 blobs ----------------------------------------------

def test_long_hex_signature_is_redacted():
    sig = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    out = ops_alerts.scrub(f"signed url mismatch X-Amz-Signature {sig}")
    assert sig not in out


def test_long_base64_blob_with_entropy_is_redacted():
    blob = "QWxwaGE5OEJldGE3N0dhbW1hNjZEZWx0YTU1RXBzaWxvbjQ0WmV0YTMz"
    out = ops_alerts.scrub(f"opaque session {blob} rejected")
    assert blob not in out


# ---- 5. eager, but not destructive: real alerts stay readable ----------------

def test_ordinary_alert_text_is_untouched():
    msg = ("media hosting failed for zanshinfitness630e22: ConnectionError: "
           "R2 unreachable after 3 tries. The draft run is unaffected.")
    assert ops_alerts.scrub(msg) == msg


def test_uuid_gym_ids_survive():
    """A UUID's dashes break every hex run at 8 chars, so gym ids and row ids are
    never mistaken for a key — Echo's alerts are useless without them."""
    msg = "stranded key 5a906124-0e6b-4ff3-b053-3f0207ec3c1f has no gyms row"
    assert ops_alerts.scrub(msg) == msg


def test_long_lowercase_words_are_not_treated_as_base64():
    msg = "a" * 60 + " looks long but carries no entropy"
    assert ops_alerts.scrub(msg) == msg


def test_env_value_pass_still_works(monkeypatch):
    """The original behavior is preserved, not replaced."""
    monkeypatch.setenv("ECHO_TEST_TOKEN", "correcthorsebatterystaple")
    out = ops_alerts.scrub("boom: correcthorsebatterystaple leaked")
    assert "correcthorsebatterystaple" not in out
    assert R in out


def test_scrub_accepts_non_string():
    assert ops_alerts.scrub(RuntimeError("plain failure")) == "plain failure"


# ---- 6. end to end: the alert that reaches Slack is scrubbed -----------------

class _Rec:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def test_alert_posts_scrubbed_text(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = _Rec()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    ops_alerts.alert("zernio 401: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert rec.notices
    assert "abcdefghijklmnopqrstuvwxyz012345" not in rec.notices[0]
    assert rec.notices[0].startswith("ECHO ALERT: ")
