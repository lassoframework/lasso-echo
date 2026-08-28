"""
The portal intake API: POST /intake/<token> with a JSON body (the ops portal's
backend call; the portal renders its own UI). Same token gate as every route
(invalid token 404, flag off 404). The submission lands in R2; the listener's
ingest routes fact sections through submit_intake() as PENDING per-account
sources (never auto trusted); gym basics + approver are HELD as an account
proposal that a re-POST updates in place; sources never duplicate on a second
POST; unapproved sources never draft. CORS: only AGENT_INTAKE_PORTAL_ORIGIN may
call cross-origin; an unlisted origin is rejected. Real HTTP on an ephemeral
port; ingest fully OFFLINE (fake R2, tmp sqlite).
"""

import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_sources as cs, config, db  # noqa: E402
from agent import intake_ingest, intake_tokens, intake_web  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.intake_web import build_server, handle_portal_intake  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402

# Signed token (the new default path): one shared secret mints it, it carries the
# client key + HMAC and contains a '.', which the widened route regex accepts.
SECRET = "intake-signing-secret-for-portal-tests"
TOKEN = intake_tokens.mint("gym_alpha_ig", secret=SECRET.encode())
PORTAL = "https://portal.lassoframework.test"


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


_BODY = {
    "gym": {"name": "Gym Alpha", "locations": ["Carmel IN"],
            "website": "https://gymalpha.test", "ig_handle": "@gymalpha",
            "fb_page": "GymAlphaFitness"},
    "voice": {"vibe": "Warm and direct", "words_to_use": ["community", "coach"],
              "words_to_never_use": ["cheap", "grind"],
              "sample_post_links": ["https://instagram.com/p/abc"]},
    "offers": {"front_door_offer": "6 week challenge for $199",
               "services": ["Small group personal training",
                            "Nutrition coaching"],
               "exact_pricing_wording": "Memberships start at $99 a month"},
    "audience": {"ideal_member": "Busy parents over 30",
                 "prior_struggles": "Big box gyms with no guidance"},
    "proof": {"wins": ["Sarah lost 30 pounds in 3 months"],
              "verifiable_numbers": ["Average member stays 26 months"]},
    "media_notes": "No photos of the kids area",
    "approver": {"name": "Alex Alpha", "role": "Owner",
                 "cell": "3175550100", "email": "alex@gymalpha.test"},
}
# offer facts: front_door(1) + pricing_rule(1); services(2); proof wins(1)+nums(1)
_EXPECTED_FACTS = 6


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    # Ingest writes the gym's brand bible now, and config.data_dir() silently falls back
    # to "." when its dir is absent, which would drop test bibles into the repo's
    # brand_voice/. Pin the documented override for every test in this file.
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "brand_voice"))
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, SECRET)
    monkeypatch.delenv("AGENT_INTAKE_TOKEN_GYM_ALPHA_IG", raising=False)
    monkeypatch.delenv("AGENT_INTAKE_PORTAL_ORIGIN", raising=False)
    monkeypatch.delenv("AGENT_UPLOAD_BASE_URL", raising=False)
    monkeypatch.setattr(intake_web, "_hits", {})
    yield


@pytest.fixture
def server():
    srv = build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _post_json(srv, path, obj, origin=None, method="POST"):
    port = srv.server_address[1]
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode() if obj is not None else b"",
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def _ingest(r2, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    return intake_ingest.process_all(
        r2=r2, poster=None, converter=lambda d, n: (d, n),
        phash=lambda d, n: None, moderator=lambda d, n: (True, ""))


# ---- 1. valid token lands pending sources for the right account -----------------
def test_valid_token_lands_pending_sources(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    # No AGENT_UPLOAD_BASE_URL: the builder falls back to the canonical service
    # origin (never a relative or placeholder link).
    monkeypatch.delenv("AGENT_UPLOAD_BASE_URL", raising=False)
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    status, _h, body = _post_json(server, f"/intake/{TOKEN}", _BODY)
    assert status == 200
    resp = json.loads(body)
    assert resp["status"] == "received"
    assert resp["account_key"] == "gym_alpha_ig"
    assert resp["pending_source_count"] == _EXPECTED_FACTS
    assert resp["upload_url"] == \
        f"{intake_web._DEFAULT_UPLOAD_BASE_URL}/u/{TOKEN}"
    assert TOKEN not in json.dumps(json.loads(
        r2.objects[[k for k in r2.objects if k.endswith("_intake.json")][0]]))

    out = _ingest(r2, monkeypatch)
    assert out["gym_alpha_ig"]["intake_forms"] == 1
    pending = cs.pending_sources("gym_alpha_ig")
    assert len(pending) == _EXPECTED_FACTS
    assert cs.approved_sources("gym_alpha_ig") == []      # never auto trusted
    texts = {s.text for s in pending}
    assert "Memberships start at $99 a month" in texts    # exact pricing wording
    assert "Average member stays 26 months" in texts
    # the HELD proposal carries basics + approver, not applied live
    prop = json.loads(db.kv_get("account_proposal_gym_alpha_ig"))
    assert prop["gym_name"] == "Gym Alpha"
    assert prop["ig_handle"] == "gymalpha"  # @ stripped at normalize
    assert prop["approver_name"] == "Alex Alpha (Owner)"
    assert "3175550100" in prop["approver_contact"]


def test_upload_url_uses_public_base_when_set(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL",
                       "https://echo-intake-web.up.railway.app/")
    r2 = FakeR2()
    status, resp = handle_portal_intake(TOKEN, _BODY, r2=r2)
    assert status == 200
    assert resp["upload_url"] == \
        f"https://echo-intake-web.up.railway.app/u/{TOKEN}"


def test_upload_url_ignores_setup_placeholder(monkeypatch):
    # A forgotten setup placeholder must NEVER reach a client link; it falls back
    # to the canonical service origin instead of "<paste ...>/u/<token>".
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "<paste the Step 7 domain here>")
    r2 = FakeR2()
    status, resp = handle_portal_intake(TOKEN, _BODY, r2=r2)
    assert status == 200
    assert resp["upload_url"] == \
        f"{intake_web._DEFAULT_UPLOAD_BASE_URL}/u/{TOKEN}"
    assert "<" not in resp["upload_url"] and "paste" not in resp["upload_url"]


def test_upload_url_ignores_non_http_base(monkeypatch):
    # A non-http value (e.g. a bare hostname) is also treated as unset.
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "echo-intake-web.up.railway.app")
    r2 = FakeR2()
    status, resp = handle_portal_intake(TOKEN, _BODY, r2=r2)
    assert status == 200
    assert resp["upload_url"] == \
        f"{intake_web._DEFAULT_UPLOAD_BASE_URL}/u/{TOKEN}"


# ---- 2. invalid token / flag off are the same 404 --------------------------------
def test_invalid_token_404(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    status, _h, _b = _post_json(server, "/intake/wrongtoken000", _BODY)
    assert status == 404


def test_flag_off_404(server, monkeypatch):
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    status, _h, _b = _post_json(server, f"/intake/{TOKEN}", _BODY)
    assert status == 404


# ---- 3. unapproved sources never draft -------------------------------------------
def test_unapproved_sources_never_draft(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    assert handle_portal_intake(TOKEN, _BODY, r2=r2)[0] == 200
    _ingest(r2, monkeypatch)
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
    cs.approve_all("gym_alpha_ig")                       # the human gate
    d = client_content.build_client_draft(acct, "2026-08-01", voice, str(lib))
    assert d is not None and d.caption.strip()


# ---- 4. a second POST updates the proposal in place, never duplicates ------------
def test_second_post_updates_not_duplicates(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    from datetime import datetime, timezone
    t1 = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 16, 9, 5, tzinfo=timezone.utc)
    assert handle_portal_intake(TOKEN, _BODY, r2=r2, now=t1)[0] == 200
    changed = json.loads(json.dumps(_BODY))
    changed["approver"]["cell"] = "3175550199"           # the gym fixed a typo
    assert handle_portal_intake(TOKEN, changed, r2=r2, now=t2)[0] == 200
    _ingest(r2, monkeypatch)
    # sources landed once, not twice
    assert len(cs.pending_sources("gym_alpha_ig")) == _EXPECTED_FACTS
    assert len(cs.all_sources("gym_alpha_ig")) == _EXPECTED_FACTS
    # the held proposal was UPDATED in place (latest submission wins)
    prop = json.loads(db.kv_get("account_proposal_gym_alpha_ig"))
    assert "3175550199" in prop["approver_contact"]


# ---- 5. CORS: only the configured portal origin passes ---------------------------
def test_cors_rejects_unlisted_origin(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_PORTAL_ORIGIN", PORTAL)
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    status, headers, _b = _post_json(server, f"/intake/{TOKEN}", _BODY,
                                     origin="https://evil.example")
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers
    assert not r2.objects                                # nothing landed


def test_cors_rejects_any_cross_origin_when_unconfigured(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")   # default: same-origin only
    status, headers, _b = _post_json(server, f"/intake/{TOKEN}", _BODY,
                                     origin=PORTAL)
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers


def test_cors_allows_the_portal_origin(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_PORTAL_ORIGIN", PORTAL)
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    # preflight
    status, headers, _b = _post_json(server, f"/intake/{TOKEN}", None,
                                     origin=PORTAL, method="OPTIONS")
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == PORTAL
    assert "POST" in headers["Access-Control-Allow-Methods"]
    # the POST itself
    status, headers, body = _post_json(server, f"/intake/{TOKEN}", _BODY,
                                       origin=PORTAL)
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == PORTAL
    assert json.loads(body)["account_key"] == "gym_alpha_ig"


def test_no_origin_server_to_server_passes(server, monkeypatch):
    """curl / backend calls send no Origin header: CORS is not in play."""
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    status, _h, _b = _post_json(server, f"/intake/{TOKEN}", _BODY)
    assert status == 200


# ---- 5b. legacy per-gym env token still verifies (zero-downtime cutover) ---------
def test_legacy_env_token_still_works(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.delenv(config.INTAKE_SIGNING_SECRET_ENV, raising=False)  # no secret
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYM_ALPHA_IG", "legacy_env_token_1")
    r2 = FakeR2()
    status, resp = handle_portal_intake("legacy_env_token_1", _BODY, r2=r2)
    assert status == 200
    assert resp["account_key"] == "gym_alpha_ig"


# ---- 6. body validation -----------------------------------------------------------
def test_bad_bodies_are_400(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    assert handle_portal_intake(TOKEN, {}, r2=r2)[0] == 400          # empty
    assert handle_portal_intake(TOKEN, {"gym": {"name": "X"}}, r2=r2)[0] == 400
    assert handle_portal_intake(TOKEN, ["not", "a", "dict"], r2=r2)[0] == 400


# ---------------------------------------------------------------------------
# Storage failures must be HONEST 503s, never "received".
#
# R2 IS the durable capture: the listener ingests that object to land sources and
# the brand docs, so with no successful write the intake does not exist. This used
# to log the field NAMES and return 200 {"status": "received"}. The portal read 2xx
# as delivered, stamped echo_forwarded=true, and decideReforward then skipped the
# row forever as already_forwarded — the gym saw "Intake submitted" and its answers
# were gone for good. A 503 leaves echo_forwarded=false so the portal re-forwards.
# ---------------------------------------------------------------------------
class _BlowingR2(FakeR2):
    def put_bytes(self, key, data, content_type="application/octet-stream"):
        raise RuntimeError("r2 timeout")


def test_no_storage_returns_503_not_received(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    status, resp = handle_portal_intake(TOKEN, _BODY, r2=None)
    assert status == 503, "an unarchived intake must never report success"
    assert resp.get("status") != "received"
    assert resp.get("error") == "storage unavailable"


def test_r2_write_failure_returns_503_and_does_not_raise(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    # An R2 timeout used to raise straight out of do_POST, killing the socket, and the
    # access log prints only "POST -> done" so a crash looked identical to a delivery.
    status, resp = handle_portal_intake(TOKEN, _BODY, r2=_BlowingR2())
    assert status == 503
    assert resp.get("status") != "received"


def test_a_healthy_write_still_reports_received(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    status, resp = handle_portal_intake(TOKEN, _BODY, r2=r2)
    assert status == 200 and resp["status"] == "received"
    assert any(k.endswith("_intake.json") for k in r2.objects)


# ---------------------------------------------------------------------------
# The healthy lane must produce a BRAND BIBLE.
#
# The one automatic bible writer (onboard_from_social) runs from the unrouted
# sweeper, whose lister filters echo_forwarded=false. A successful portal forward
# sets that true, so a gym got a bible exactly when its delivery FAILED. Every gym
# that onboarded cleanly had no voice doc, and the drafter then wrote captions with
# no avatar, no pillars and no CTAs. Ingest now writes the docs on the healthy path.
# ---------------------------------------------------------------------------
def test_landed_intake_writes_the_brand_bible(server, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    assert _post_json(server, f"/intake/{TOKEN}", _BODY)[0] == 200
    _ingest(r2, monkeypatch)

    bible = os.path.join(config.client_voice_dir(), "gym_alpha", "lasso_voice.md")
    assert os.path.exists(bible), "a cleanly delivered intake must still get a bible"
    text = open(bible, encoding="utf-8").read()
    assert "Gym Alpha" in text
    # the gym's OWN words reached the doc, not a generic template
    assert "Warm and direct" in text
    assert os.path.exists(
        os.path.join(config.client_voice_dir(), "gym_alpha", "social_proof.md"))


def test_bible_write_is_idempotent_and_never_clobbers_a_reviewed_doc(server, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    assert _post_json(server, f"/intake/{TOKEN}", _BODY)[0] == 200
    _ingest(r2, monkeypatch)

    bible = os.path.join(config.client_voice_dir(), "gym_alpha", "lasso_voice.md")
    with open(bible, "w", encoding="utf-8") as fh:
        fh.write("REVIEWED BY A HUMAN, DO NOT OVERWRITE")

    # a second submission must land its sources but leave the reviewed doc alone
    assert _post_json(server, f"/intake/{TOKEN}", _BODY)[0] == 200
    _ingest(r2, monkeypatch)
    assert open(bible, encoding="utf-8").read() == "REVIEWED BY A HUMAN, DO NOT OVERWRITE"


def test_a_failing_bible_write_never_loses_the_landed_intake(server, monkeypatch, tmp_path):
    # Sources and the held proposal are committed before the doc write, so a bible
    # failure must not dead-letter the form or undo them.
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    from agent import social_intake_reader as sir

    def _blow(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sir, "write_brand_docs", _blow)
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    assert _post_json(server, f"/intake/{TOKEN}", _BODY)[0] == 200
    out = _ingest(r2, monkeypatch)
    assert out["gym_alpha_ig"]["intake_forms"] == 1
    assert len(cs.pending_sources("gym_alpha_ig")) == _EXPECTED_FACTS


def test_texted_link_lane_gets_no_bible_and_no_false_alarm(monkeypatch, tmp_path):
    # handle_intake_form (the urlencoded /f/<token> lane) archives a FLAT answers dict with
    # no "portal" key. Feeding that to the mapper raises (it calls .get() on strings) and,
    # if it did not, would lay down an unclobberable bible for "the gym" — worse than none,
    # because a hollow bible looks like a satisfied precondition and blocks the real one.
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    from agent import intake_ingest as ii

    flat = {"kind": "intake_form", "client": "gym_alpha_ig", "timestamp": "20260828T000000Z",
            "answers": {"gym_name": "Gym Alpha", "voice": "warm", "offers": "6 week challenge"}}
    assert ii._is_section_shaped(flat.get("portal")) is False
    assert ii._is_section_shaped(flat["answers"]) is False, "flat answers must not qualify"

    nested = {"gym": {"name": "Gym Alpha"}, "voice": {"vibe": "warm"}}
    assert ii._is_section_shaped(nested) is True

    alerts = []
    monkeypatch.setattr(ii.ops_alerts, "alert", lambda m, *a, **k: alerts.append(m))
    r2 = FakeR2()
    ii._land_intake_form("gym_alpha_ig", flat, r2, "intake/gym_alpha_ig/incoming/x_intake.json",
                         {"processed": []})
    assert not any("brand bible could NOT" in m for m in alerts), \
        "the texted-link lane must not cry wolf on every submission"
    bible = os.path.join(config.client_voice_dir(), "gym_alpha", "lasso_voice.md")
    assert not os.path.exists(bible), "must not write a hollow, unclobberable bible"
