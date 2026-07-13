"""
The client intake form inside intake-web. A gym fills its private link instead of
emailing answers: GET /intake/<token> renders the seven-section LASSO form (same
token gate as the upload page: invalid token 404, flag off 404); POST lands the
submission in R2, the listener's ingest routes fact sections through
submit_intake() as PENDING per-account sources (never auto approved, deduped on a
second submission), the approver + basics are held as an account proposal, and
the confirmation page offers the upload link for the same token. Real HTTP
against an ephemeral port; the ingest side fully OFFLINE (fake R2, tmp sqlite).
"""

import json
import os
import sys
import threading
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_sources as cs, config, db  # noqa: E402
from agent import intake_ingest, intake_tokens, intake_web, ops_alerts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.intake_web import build_server, handle_intake_form  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402

# Signed token (the new default path): one shared secret, no per-gym env var. The
# token carries the client key + an HMAC and contains a '.', which the widened
# route regex accepts. Minted with an explicit secret so it is stable at import;
# the fixture sets the same secret in env so the running server verifies it.
SECRET = "intake-signing-secret-for-form-tests"
TOKEN = intake_tokens.mint("gym_alpha_ig", secret=SECRET.encode())


class FakeR2:
    def __init__(self):
        self.objects = {}

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get_bytes(self, key):
        return self.objects[key]

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def delete(self, key):
        self.objects.pop(key, None)


_ANSWERS = {
    "gym_name": "Gym Alpha", "city": "Carmel", "website": "https://gymalpha.test",
    "about": "Family owned and coaching since 2015",
    "voice": "Warm and direct",
    "offers": "6 week challenge for $199\nFree intro session",
    "services": "Small group personal training",
    "pricing_rule": "Memberships start at $99 a month",
    "audience": "Busy parents",
    "proof": "Sarah lost 30 pounds in 3 months",
    "media_notes": "No photos of the kids area",
    "approver_name": "Alex Alpha", "approver_contact": "alex@gymalpha.test",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, SECRET)
    monkeypatch.delenv("AGENT_INTAKE_TOKEN_GYM_ALPHA_IG", raising=False)
    monkeypatch.setattr(intake_web, "_hits", {})   # fresh rate-limit window
    yield


@pytest.fixture
def server(monkeypatch):
    srv = build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _get(srv, path):
    port = srv.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post_form(srv, path, fields):
    port = srv.server_address[1]
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---- 1. valid token renders the form -------------------------------------------
def test_valid_token_renders_form(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    status, body = _get(server, f"/intake/{TOKEN}")
    assert status == 200
    assert "LASSO" in body and "Gym basics" in body and "Approver" in body
    assert "pricing" in body.lower()                  # the exact-wording rule field
    # client facing copy law: never the word vendor
    assert "vendor" not in body.lower()


# ---- 2. invalid token 404s -------------------------------------------------------
def test_invalid_token_404(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    status, _ = _get(server, "/intake/wrongtoken000")
    assert status == 404


# ---- 3. flag off 404s (same as every route while dark) ---------------------------
def test_flag_off_404(server, monkeypatch):
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    status, _ = _get(server, f"/intake/{TOKEN}")
    assert status == 404


# ---- 4. POST lands in R2; the confirmation offers the upload page ---------------
def test_post_lands_in_r2_and_offers_upload(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    status, body = _post_form(server, f"/intake/{TOKEN}", _ANSWERS)
    assert status == 200
    assert f"/u/{TOKEN}" in body                      # the same-sitting upload link
    assert "approve" in body.lower()                  # receipt confirms the gate
    forms = [k for k in r2.objects if k.endswith("_intake.json")]
    assert len(forms) == 1
    payload = json.loads(r2.objects[forms[0]])
    assert payload["client"] == "gym_alpha_ig"
    assert payload["answers"]["gym_name"] == "Gym Alpha"
    assert TOKEN not in json.dumps(payload)           # raw token never persisted


# ---- 5. ingest: the submission becomes PENDING sources, right account -----------
def _ingest(r2, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    return intake_ingest.process_all(
        r2=r2, poster=None,
        converter=lambda d, n: (d, n),
        phash=lambda d, n: None,
        moderator=lambda d, n: (True, ""))


def test_submission_lands_pending_and_never_drafts_unapproved(monkeypatch, tmp_path):
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: alerts.append(m))
    r2 = FakeR2()
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    assert handle_intake_form(TOKEN, _ANSWERS, r2=r2)[0] == 200
    out = _ingest(r2, monkeypatch)
    assert out["gym_alpha_ig"]["intake_forms"] == 1
    # fact sections landed PENDING for the RIGHT account: 2 offers + pricing rule,
    # 1 service, 1 testimonial, 1 about = 6
    pending = cs.pending_sources("gym_alpha_ig")
    assert len(pending) == 6
    assert cs.approved_sources("gym_alpha_ig") == []
    assert cs.pending_sources("gym_beta_ig") == []
    texts = {s.text for s in pending}
    assert "Memberships start at $99 a month" in texts    # exact pricing wording
    # the account proposal is HELD (kv), not applied
    prop = json.loads(db.kv_get("account_proposal_gym_alpha_ig"))
    assert prop["approver_name"] == "Alex Alpha"
    # one alert names the review step
    assert any("intake form received" in a for a in alerts)
    # the payload is archived for the bible draft; incoming is consumed
    assert any(k.startswith("intake/gym_alpha_ig/forms/") for k in r2.objects)
    assert not any(k.startswith("intake/gym_alpha_ig/incoming/") for k in r2.objects)

    # NEVER drafts before approval
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.jpg").write_bytes(b"\xff\xd8\xffFAKE")
    acct = Account(key="gym_alpha_ig", display_name="Gym Alpha",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="TID",
                   slack_channel="C_ALPHA")
    voice = VoiceDoc(raw="v\n#Tag", hashtags=["#Tag"], ctas=["Save this post."])
    assert client_content.build_client_draft(acct, "2026-08-01", voice,
                                             str(lib)) is None
    # after a human approves, it drafts
    cs.approve_all("gym_alpha_ig")
    d = client_content.build_client_draft(acct, "2026-08-01", voice, str(lib))
    assert d is not None and d.caption.strip()


# ---- 6. a second submission does not duplicate ----------------------------------
def test_second_submission_does_not_duplicate(monkeypatch):
    r2 = FakeR2()
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    from datetime import datetime, timezone
    t1 = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 15, 9, 5, tzinfo=timezone.utc)
    assert handle_intake_form(TOKEN, _ANSWERS, r2=r2, now=t1)[0] == 200
    assert handle_intake_form(TOKEN, _ANSWERS, r2=r2, now=t2)[0] == 200
    _ingest(r2, monkeypatch)
    assert len(cs.pending_sources("gym_alpha_ig")) == 6   # not 12
    assert len(cs.all_sources("gym_alpha_ig")) == 6


# ---- 7b. legacy per-gym env token still verifies (zero-downtime cutover) ---------
def test_legacy_env_token_still_works(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.delenv(config.INTAKE_SIGNING_SECRET_ENV, raising=False)  # no secret
    legacy = "legacy_env_token_1"
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYM_ALPHA_IG", legacy)
    status, body = _get(server, f"/intake/{legacy}")
    assert status == 200
    assert "Gym basics" in body


# ---- 7. the pure handler gates exactly like the upload path ----------------------
def test_handler_gates(monkeypatch):
    r2 = FakeR2()
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    assert handle_intake_form(TOKEN, _ANSWERS, r2=r2)[0] == 404   # flag off
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    assert handle_intake_form("wrong_token_00", _ANSWERS, r2=r2)[0] == 404
    assert handle_intake_form(TOKEN, {}, r2=r2)[0] == 400          # empty form
    assert handle_intake_form(TOKEN, {"gym_name": "X"}, r2=r2)[0] == 400
