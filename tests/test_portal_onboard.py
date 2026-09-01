"""
Tests for the self-serve portal onboard endpoint (POST /portal/onboard).

Drives the REAL request handler via build_server(0).RequestHandlerClass and
Handler.__new__ (skips socket init), mirroring test_intake_web_portal.py. All
tests are OFFLINE: a tmp cwd for scaffolded files, a tmp AGENT_DB_PATH, a fixed
signing secret, and a fake rfile/wfile so no live socket is ever touched.

Auth env var: AGENT_PORTAL_ONBOARD_KEY (X-Portal-Key header, constant-time).
mint() is DETERMINISTIC (HMAC-SHA256 of the lowercased key under the shared
secret), so onboarding the same gym twice returns the SAME live token.
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


SIGNING_SECRET = "test-signing-secret-abc123"
PORTAL_KEY = "portal-shared-key-xyz789"


# ---- harness -------------------------------------------------------------------

def _make_handler():
    """The real request handler class, reached via the build_server() factory.
    Bind port 0 (never serve) and read RequestHandlerClass off the server."""
    server = intake_web.build_server(0)
    try:
        return server.RequestHandlerClass
    finally:
        server.server_close()


class _FakeWFile:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, data):
        self.buffer.write(data)


def _post_onboard(body_dict=None, portal_key=PORTAL_KEY, raw_override=None):
    """Drive do_POST for /portal/onboard. Returns (status_code, response_json_or_text)."""
    Handler = _make_handler()
    inst = Handler.__new__(Handler)  # skip __init__: it wants a live socket
    inst.path = "/portal/onboard"
    inst.command = "POST"

    if raw_override is not None:
        raw = raw_override
    else:
        raw = json.dumps(body_dict or {}).encode("utf-8")

    headers = {"Content-Length": str(len(raw))}
    if portal_key is not None:
        headers["X-Portal-Key"] = portal_key
    inst.headers = headers
    inst.rfile = io.BytesIO(raw)

    # Capture the response written by _send_json / _deny.
    captured = {}

    def _send_response(code):
        captured["status"] = code

    def _send_header(k, v):
        pass

    def _end_headers():
        pass

    wfile = _FakeWFile()
    inst.send_response = _send_response
    inst.send_header = _send_header
    inst.end_headers = _end_headers
    inst.wfile = wfile

    inst.do_POST()

    raw_out = wfile.buffer.getvalue()
    try:
        payload = json.loads(raw_out.decode("utf-8"))
    except Exception:
        payload = raw_out.decode("utf-8", "replace")
    return captured.get("status"), payload


def _base_env(monkeypatch, tmp_path):
    """Set up an isolated, offline env: tmp cwd (for scaffold files), tmp DB,
    the signing secret, and the shared portal key. Callers still toggle the
    APPROVALS gate per test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_INTAKE_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("AGENT_PORTAL_ONBOARD_KEY", PORTAL_KEY)


# ---- 1. Missing / wrong X-Portal-Key -> 401 ------------------------------------

def test_missing_key_returns_401(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"}, portal_key=None
    )
    assert status == 401
    assert "unauthorized" in body["error"]


def test_wrong_key_returns_401(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"},
        portal_key="the-wrong-key",
    )
    assert status == 401
    assert "unauthorized" in body["error"]


def test_unset_shared_key_fails_closed_401(monkeypatch, tmp_path):
    """No AGENT_PORTAL_ONBOARD_KEY configured => every request 401s (fail CLOSED)."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_PORTAL_ONBOARD_KEY", raising=False)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"}, portal_key="anything"
    )
    assert status == 401


# ---- 2. AGENT_PORTAL_APPROVALS off -> 403 --------------------------------------

def test_approvals_flag_off_returns_403(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"}
    )
    assert status == 403
    assert "disabled" in body["error"]


# ---- 3. Valid key + body -> 200, resolvable token ------------------------------

def test_valid_onboard_returns_resolvable_token(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")  # so client_for_token verifies

    status, body = _post_onboard(
        {"account_key": "newgym", "display_name": "New Gym Fitness"}
    )
    assert status == 200, body
    assert body["account_key"] == "newgym"
    assert body["publish_off"] is True
    assert body["onboarded"] is True
    token = body["raw_token"]
    assert isinstance(token, str) and token
    # The raw token resolves back to the account_key via the real verifier.
    assert intake_web.client_for_token(token) == "newgym"


# ---- 4. Idempotent: same account twice -> same live token ----------------------

def test_idempotent_same_token(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")

    s1, b1 = _post_onboard({"account_key": "repeatgym", "display_name": "Repeat Gym"})
    s2, b2 = _post_onboard({"account_key": "repeatgym", "display_name": "Repeat Gym"})
    assert s1 == 200 and s2 == 200, (b1, b2)
    # Both calls succeed and both tokens resolve to the same account.
    assert intake_web.client_for_token(b1["raw_token"]) == "repeatgym"
    assert intake_web.client_for_token(b2["raw_token"]) == "repeatgym"
    # mint() is deterministic under HMAC signing, so the token is identical and
    # a live token is never rotated on re-onboard.
    assert b1["raw_token"] == b2["raw_token"]


# ---- 5. Bad account_key slug -> 400 --------------------------------------------

def test_bad_slug_uppercase_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "BadGym", "display_name": "Bad Gym"}
    )
    assert status == 400
    assert "account_key" in body["error"]


def test_bad_slug_special_chars_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    for bad in ("gym-key", "gym key", "gym.key", "gym_key", "gym/key", ""):
        status, body = _post_onboard(
            {"account_key": bad, "display_name": "Gym"}
        )
        assert status == 400, f"{bad!r} should be rejected, got {status}"


def test_missing_display_name_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "   "}
    )
    assert status == 400
    assert "display_name" in body["error"]


def test_invalid_json_body_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(raw_override=b"{not json")
    assert status == 400


# ---- 6. Pure-function idempotency (handle_portal_onboard direct) ---------------

def test_handle_portal_onboard_pure_idempotent(monkeypatch, tmp_path):
    """The module-level handler is deterministic and never rotates a live token."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    s1, r1 = intake_web.handle_portal_onboard(
        {"account_key": "puregym", "display_name": "Pure Gym"}
    )
    s2, r2 = intake_web.handle_portal_onboard(
        {"account_key": "puregym", "display_name": "Pure Gym"}
    )
    assert s1 == 200 and s2 == 200
    assert r1["raw_token"] == r2["raw_token"]
    assert intake_web.client_for_token(r1["raw_token"]) == "puregym"


# ---- 7. PINNING: the returned key IS the key the returned token resolves to ------
#
# The Swift River CrossFit / CrossFit Sunnyside split-brain. onboard.run() re-keys the gym
# through account_key_mint.derive_mint_key (portal gyms.id UUID folded in) and reports the
# key it used in result["account_key"]. The handler used to ignore that: it echoed the
# PASSED key and, on the idempotent branch, re-minted a token from the PASSED key too. The
# portal stored account_key="swiftriver" beside a token authenticating as
# "swiftrivercrossfit6e87f3" while the gym row, voice doc, brain file, trust kv and publish
# kv all lived under the canonical key. Uploads landed on one key, every portal lookup used
# the other, and the gym silently never posted.
#
# WHY NOTHING IN THE SUITE CAUGHT IT: the offline test host has no Supabase creds, so
# _default_resolve_uuid returns None and derive_mint_key keeps the passed key verbatim. The
# two keys are identical in every existing test, so the divergence is invisible. These tests
# patch the resolver so a REAL canonical derivation happens, which is the only way the two
# keys can differ. They fail against the pre-fix handler.

_FAKE_GYM_UUID = "6e87f3aa-1111-4222-8333-444455556666"


def _force_canonical_derivation(monkeypatch):
    """Make derive_mint_key actually derive: hand it a resolvable portal gyms.id UUID.
    Patches the module-level default resolver (derive_mint_key looks it up at call time),
    so onboard.run's un-injected call picks it up. Read-only and offline: no Supabase."""
    from agent import account_key_mint

    monkeypatch.setenv("AGENT_CANONICAL_MINT", "true")
    monkeypatch.setattr(
        account_key_mint, "_default_resolve_uuid", lambda base: _FAKE_GYM_UUID
    )
    return account_key_mint


def test_returned_key_matches_token_key_when_canonical_derived(monkeypatch, tmp_path):
    """THE pin: response account_key == the key the response token actually authenticates.
    Under the old handler the response said "swiftriver" while the token said
    "swiftrivercrossfit<uuid-suffix>", which is the whole incident."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    akm = _force_canonical_derivation(monkeypatch)

    passed_key = "swiftriver"
    display_name = "Swift River CrossFit"

    # Sanity: this input really does derive a DIFFERENT key, otherwise the assertion
    # below would pass vacuously exactly the way the offline suite always did.
    expected_key, info = akm.derive_mint_key(passed_key, display_name)
    assert info["derived"] is True
    assert expected_key != passed_key

    status, body = intake_web.handle_portal_onboard(
        {"account_key": passed_key, "display_name": display_name}
    )
    assert status == 200, body

    token_key = intake_web.client_for_token(body["raw_token"])
    assert token_key == body["account_key"], (
        f"split brain: response says {body['account_key']!r} but the token "
        f"authenticates as {token_key!r}"
    )
    # And it is the CANONICAL key, never the passed one.
    assert body["account_key"] == expected_key
    assert body["account_key"] != passed_key


def test_no_second_key_left_behind(monkeypatch, tmp_path):
    """One string, three places: what onboard.run reports, what the response says, and what
    the token resolves to must all be the SAME key. Any second key is a stranded gym."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    _force_canonical_derivation(monkeypatch)

    seen = {}
    from agent import onboard as _onboard
    real_run = _onboard.run

    def _spy_run(account_key, display_name, **kwargs):
        result = real_run(account_key, display_name, **kwargs)
        seen["onboard_key"] = result["account_key"]
        return result

    monkeypatch.setattr(_onboard, "run", _spy_run)

    status, body = intake_web.handle_portal_onboard(
        {"account_key": "sunnyside", "display_name": "CrossFit Sunnyside"}
    )
    assert status == 200, body

    keys = {
        seen["onboard_key"],
        body["account_key"],
        intake_web.client_for_token(body["raw_token"]),
    }
    assert len(keys) == 1, f"more than one account_key in play: {keys}"
    # And the one survivor is the canonical key, not the ad-hoc key the portal passed.
    assert keys != {"sunnyside"}


def test_canonical_key_owns_the_gym_row_and_flags(monkeypatch, tmp_path):
    """The artifacts follow the returned key. If the portal records a key with no gym row,
    every later portal lookup misses and the gym silently never posts."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    _force_canonical_derivation(monkeypatch)

    status, body = intake_web.handle_portal_onboard(
        {"account_key": "swiftriver", "display_name": "Swift River CrossFit"}
    )
    assert status == 200, body
    key = body["account_key"]

    from agent import db
    assert db.gym_get(key) is not None, "no gym row under the key the portal was handed"
    # Publishing stays OFF for a brand new gym, under the SAME key.
    assert db.kv_get(f"gym_publish_{key}") == "OFF"
    assert body["publish_off"] is True
    # Nothing was stood up under the passed key.
    assert db.gym_get("swiftriver") is None


def test_idempotent_rerun_keeps_one_canonical_key(monkeypatch, tmp_path):
    """Second call takes the idempotent path (token recovered rather than freshly minted).
    That recovery must mint from the CANONICAL key: the pre-fix code called
    _current_token_for(passed_key) there, minting a token for a phantom gym."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    _force_canonical_derivation(monkeypatch)

    body_in = {"account_key": "swiftriver", "display_name": "Swift River CrossFit"}
    s1, r1 = intake_web.handle_portal_onboard(dict(body_in))
    s2, r2 = intake_web.handle_portal_onboard(dict(body_in))
    assert s1 == 200 and s2 == 200, (r1, r2)

    assert r1["account_key"] == r2["account_key"]
    assert r1["raw_token"] == r2["raw_token"]
    assert intake_web.client_for_token(r2["raw_token"]) == r2["account_key"]
    assert r2["account_key"] != "swiftriver"


def test_fallback_paths_keep_the_passed_key(monkeypatch, tmp_path):
    """Honesty rails unchanged: when derive_mint_key declines to derive, the passed key is
    still what comes back. Three declines covered: flag OFF, unresolved portal uuid, and a
    gym that already has a local row (never re-key a live gym, never strand its link)."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    from agent import account_key_mint

    # (a) flag OFF -> passed key verbatim.
    monkeypatch.setenv("AGENT_CANONICAL_MINT", "false")
    monkeypatch.setattr(
        account_key_mint, "_default_resolve_uuid", lambda base: _FAKE_GYM_UUID
    )
    status, body = intake_web.handle_portal_onboard(
        {"account_key": "flagoffgym", "display_name": "Flag Off Gym"}
    )
    assert status == 200, body
    assert body["account_key"] == "flagoffgym"
    assert intake_web.client_for_token(body["raw_token"]) == "flagoffgym"

    # (b) portal uuid unresolved (the real offline host) -> passed key verbatim,
    #     no fabricated gym_id.
    monkeypatch.setenv("AGENT_CANONICAL_MINT", "true")
    monkeypatch.setattr(account_key_mint, "_default_resolve_uuid", lambda base: None)
    status, body = intake_web.handle_portal_onboard(
        {"account_key": "nouuidgym", "display_name": "No Uuid Gym"}
    )
    assert status == 200, body
    assert body["account_key"] == "nouuidgym"
    assert intake_web.client_for_token(body["raw_token"]) == "nouuidgym"

    # (c) existing local gym row -> key kept even once the uuid resolves.
    monkeypatch.setattr(
        account_key_mint, "_default_resolve_uuid", lambda base: _FAKE_GYM_UUID
    )
    status, body = intake_web.handle_portal_onboard(
        {"account_key": "nouuidgym", "display_name": "No Uuid Gym"}
    )
    assert status == 200, body
    assert body["account_key"] == "nouuidgym"
    assert intake_web.client_for_token(body["raw_token"]) == "nouuidgym"


def test_missing_result_key_falls_back_to_passed_key(monkeypatch, tmp_path):
    """A result dict with no usable account_key is an unrecognised shape, not a licence to
    guess: we fall back to the passed key rather than returning a key nothing was built
    under."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")

    from agent import onboard as _onboard
    real_run = _onboard.run

    def _blank_key_run(account_key, display_name, **kwargs):
        result = real_run(account_key, display_name, **kwargs)
        result["account_key"] = "   "
        return result

    monkeypatch.setattr(_onboard, "run", _blank_key_run)
    status, body = intake_web.handle_portal_onboard(
        {"account_key": "blankgym", "display_name": "Blank Gym"}
    )
    assert status == 200, body
    assert body["account_key"] == "blankgym"
    assert intake_web.client_for_token(body["raw_token"]) == "blankgym"
