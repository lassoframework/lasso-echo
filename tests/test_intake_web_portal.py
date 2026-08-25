"""
Tests for per-token rate limit and portal gym endpoint.

Token lookup tests for the HMAC-signed path live in test_intake_tokens.py and
test_portal_intake.py. The DB-backed client_for_token_data tests were removed
when HMAC signing superseded the per-gym token store.

All tests are OFFLINE: injectable R2, no live network calls.
"""

import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


# ---- helpers -------------------------------------------------------------------

def _sha256(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _make_db():
    """In-memory SQLite db with the gyms table schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gyms (
            account_key TEXT PRIMARY KEY,
            display_name TEXT DEFAULT '',
            gym_name TEXT,
            intake_token_hash TEXT,
            token_sha256 TEXT,
            token_rotated_at TEXT,
            token_revoked INTEGER DEFAULT 0,
            token_status TEXT DEFAULT 'NOT_SET',
            upload_link TEXT,
            publish_flag TEXT DEFAULT 'OFF',
            publish_creds_status TEXT DEFAULT 'NOT SET (by hand)',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def _insert_gym(conn, account_key, token=None, status="ACTIVE", upload_link=None,
                gym_name=None):
    fp = _sha256(token) if token else None
    revoked = 1 if status == "REVOKED" else 0
    conn.execute(
        "INSERT OR REPLACE INTO gyms "
        "(account_key, gym_name, intake_token_hash, token_sha256, token_revoked, "
        "token_status, upload_link) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_key, gym_name, fp, fp, revoked, status, upload_link)
    )
    conn.commit()


class FakeR2WithList:
    def __init__(self, keys=()):
        self.objects = {}
        self._keys = list(keys)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, content_type)

    def list_keys(self, prefix):
        return [k for k in self._keys if k.startswith(prefix)]


# ---- 1. client_for_token env fallback ------------------------------------------

def test_client_for_token_env_fallback(monkeypatch):
    """When automint OFF, env fallback still works (flag-OFF path byte-identical)."""
    monkeypatch.delenv("AGENT_ONBOARD_AUTOMINT", raising=False)
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMENV", "env-tok-xyz9999")

    result = intake_web.client_for_token("env-tok-xyz9999")
    assert result == "gymenv"


# ---- 2. Per-token rate limit ---------------------------------------------------

def test_per_token_rate_limit(monkeypatch):
    """21 calls on same token hash prefix triggers 429 on the 21st."""
    # Clear the token hits dict for a clean test.
    intake_web._token_hits.clear()

    token = "rate-limit-test-token-abc"
    hp = intake_web._token_hash_prefix(token)

    # First 20 should pass.
    for i in range(20):
        allowed = intake_web.allow_token_request(hp, now=1000.0 + i)
        assert allowed is True, f"call {i+1} should be allowed"

    # 21st should be denied.
    denied = intake_web.allow_token_request(hp, now=1019.0)
    assert denied is False

    # A different token hash is unaffected.
    other_token = "completely-different-token"
    other_hp = intake_web._token_hash_prefix(other_token)
    assert intake_web.allow_token_request(other_hp, now=1019.0) is True

    # After 60 seconds, the window rolls and the original token is free again.
    rolled = intake_web.allow_token_request(hp, now=1061.0)
    assert rolled is True


def test_per_token_rate_limit_constant():
    """The per-token rate limit constant is 20."""
    assert intake_web._TOKEN_RATE_PER_MINUTE == 20


# ---- 3. Portal endpoint returns gym info when flag ON --------------------------

def test_portal_endpoint_returns_gym_info(monkeypatch):
    """GET /portal/gym/<key> returns JSON with account_key and intake_status.
    Signing secret cleared so this test PINS the no-secret -> stored-plaintext
    link fallback (with a secret set, the minted reconstruction wins)."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.delenv("AGENT_INTAKE_SIGNING_SECRET", raising=False)

    conn = _make_db()
    _insert_gym(conn, "gymportal", token="portal-tok-11111",
                status="ACTIVE", upload_link="https://intake.example.com/u/gymportal",
                gym_name="Portal Gym")

    from agent import db as _db

    def _fake_gym_get(account_key):
        row = conn.execute(
            "SELECT * FROM gyms WHERE account_key=?",
            (account_key,)
        ).fetchone()
        return dict(row) if row else None

    monkeypatch.setattr("agent.db.gym_get", _fake_gym_get)

    status_code, body = intake_web.handle_portal_gym_status("gymportal")
    assert status_code == 200
    assert body["account_key"] == "gymportal"
    assert body["intake_status"] == "ACTIVE"
    assert body["token_status"] == "ACTIVE"
    assert body["upload_link"] == "https://intake.example.com/u/gymportal"
    # R2 not provided: null values.
    assert body["last_upload_at"] is None
    assert body["upload_count"] is None


def test_portal_endpoint_returns_gym_info_with_r2(monkeypatch):
    """Portal endpoint populates upload_count and last_upload_at from R2 listing."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    conn = _make_db()
    _insert_gym(conn, "gymr2", token="r2-tok-22222", status="ACTIVE",
                upload_link="https://intake.example.com/u/gymr2")

    monkeypatch.setattr("agent.db.gym_get", lambda k: (
        dict(conn.execute(
            "SELECT * FROM gyms WHERE account_key=?",
            (k,)
        ).fetchone()) if conn.execute(
            "SELECT account_key FROM gyms WHERE account_key=?", (k,)
        ).fetchone() else None
    ))

    r2 = FakeR2WithList(keys=[
        "intake/gymr2/incoming/20240601T120000Z_photo.jpg",
        "intake/gymr2/incoming/20240601T120000Z_upload.json",
        "intake/gymr2/incoming/20240605T090000Z_clip.mp4",
    ])

    status_code, body = intake_web.handle_portal_gym_status("gymr2", r2=r2)
    assert status_code == 200
    # 3 objects in incoming.
    assert body["upload_count"] == 3
    # Timestamp from the alphabetically last non-sidecar key.
    assert body["last_upload_at"] == "20240605T090000Z"


def test_portal_endpoint_not_found(monkeypatch):
    """Returns 404 when account_key is in neither sqlite nor the Supabase
    echo_intake_tokens fallback (creds cleared so the test stays hermetic)."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)

    status_code, body = intake_web.handle_portal_gym_status("unknowngym")
    assert status_code == 404
    assert "not found" in body["error"]


# ---- 4. Portal endpoint 403 when flag OFF --------------------------------------

def test_portal_endpoint_flag_off_returns_403(monkeypatch):
    """AGENT_PORTAL_APPROVALS OFF returns 403."""
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)

    status_code, body = intake_web.handle_portal_gym_status("anygym")
    assert status_code == 403
    assert "disabled" in body["error"]


# ---- 5. Route regex accepts SIGNED (dotted) tokens -----------------------------
# Regression for the 404 that hid every minted token: a signed token is
# base64url(account).signature, so it contains a '.'. If _portal_token_route's
# char class excludes the dot, the path never matches and the request 404s BEFORE
# client_for_token runs — so calendar/report/social-status/approve/kill are all
# dead for onboarded gyms while only legacy dotless env tokens work. Drives the
# real handler method, not a copy of the pattern.

def _make_portal_handler():
    """The real request handler class, reached via the build_server() factory.
    We bind port 0 (never serve) and read RequestHandlerClass off the server."""
    server = intake_web.build_server(0)
    try:
        return server.RequestHandlerClass
    finally:
        server.server_close()


def test_portal_token_route_accepts_signed_dotted_token():
    signed = "Y3Jvc3NmaXRuZXd0b3du.wU6hFMgriJWdA0W8g5zm-k1bMiY"  # base64url(acct).sig
    dotless = "a" * 43  # legacy env token
    Handler = _make_portal_handler()
    assert Handler is not None, "portal request handler not found on intake_web"
    inst = Handler.__new__(Handler)  # skip __init__: it wants a live socket
    for sub in ("calendar", "report", "social-status", "social-connect",
                "facebook-pages", "approve", "edit", "deny", "kill"):
        inst.path = f"/portal/{signed}/{sub}"
        tok, got = inst._portal_token_route()
        assert tok == signed and got == sub, f"signed token dropped for {sub}: {tok!r},{got!r}"
    # legacy dotless tokens must still route
    inst.path = f"/portal/{dotless}/social-status"
    tok, got = inst._portal_token_route()
    assert tok == dotless and got == "social-status"


# ---- self-serve CONNECT page (IG / FB / Google Business) ------------------------

def test_render_connect_page_has_all_three_platforms(monkeypatch):
    monkeypatch.setattr("agent.db.gym_get",
                        lambda k: {"display_name": "CrossFit and HYROX ENG"})
    html = intake_web.render_connect_page("eng-tok-12345678", "eng")
    # all three connect buttons, keyed by the platform the endpoint expects
    assert 'data-p="instagram"' in html
    assert 'data-p="facebook"' in html
    assert 'data-p="googlebusiness"' in html          # the new Google Business button
    # the token is injected so the buttons hit the right token-scoped endpoint
    assert "eng-tok-12345678" in html
    # gym name shown in the header
    assert "CrossFit and HYROX ENG" in html
    # NO secret in the page (no api key / bearer / oauth url baked in)
    low = html.lower()
    assert "authorization" not in low and "bearer" not in low
    assert "accounts.google.com" not in low           # oauth url is fetched at click time
    # copy law: no em/en/figure dash and no spaced hyphen in the copy (CSS custom
    # properties like --navy are not copy and are allowed, per the FORM_PAGE convention)
    import re as _re
    assert not _re.search(r"[‐-―−]|(?:\s-\s)", html)


def test_render_connect_page_escapes_gym_name(monkeypatch):
    monkeypatch.setattr("agent.db.gym_get",
                        lambda k: {"display_name": '<script>x</script>'})
    html = intake_web.render_connect_page("tok-abcdefgh", "evil")
    assert "<script>x</script>" not in html            # escaped, not injected raw
    assert "&lt;script&gt;" in html


def test_render_connect_page_generic_header_when_name_missing(monkeypatch):
    monkeypatch.setattr("agent.db.gym_get", lambda k: None)
    html = intake_web.render_connect_page("tok-abcdefgh", "nogym")
    assert "your gym" in html                          # graceful fallback
