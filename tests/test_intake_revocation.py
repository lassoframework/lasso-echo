"""
Phase 5: per-gym revocation via an R2 denylist (intake-web is R2 only, never
/data). revoke() kills ONE gym's signed link without rotating the shared secret;
a revoked link is a 404 everywhere (form, upload, portal API), exactly like an
unknown token; unrevoke() restores it. Read fresh each call so a kill is
immediate; the verify path fails OPEN so a denylist read error never takes every
link down.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, intake_tokens, intake_web  # noqa: E402
from agent.intake_web import (handle_intake_form, handle_portal_intake,  # noqa: E402
                              handle_upload, is_revoked, revoke, unrevoke)

SECRET = "intake-signing-secret-for-revocation-tests"
TOKEN = intake_tokens.mint("gym_alpha_ig", secret=SECRET.encode())
_JPEG = ("a.jpg", "image/jpeg", b"\xff\xd8\xffFAKE")
_PORTAL_BODY = {"gym": {"name": "Gym Alpha"}, "offers": {"front_door_offer": "x"}}
_FORM = {"gym_name": "Gym Alpha", "offers": "6 week challenge"}


class FakeR2:
    """get_bytes returns None on a missing key (the real _R2 contract), so the
    denylist writer can tell 'empty' from 'unreadable'."""

    def __init__(self):
        self.objects = {}

    def get_bytes(self, key):
        return self.objects.get(key)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete(self, key):
        self.objects.pop(key, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, SECRET)
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.delenv("AGENT_INTAKE_TOKEN_GYM_ALPHA_IG", raising=False)
    monkeypatch.setattr(intake_web, "_hits", {})
    yield


# ---- 1. revoke round-trip through the denylist -----------------------------------
def test_revoke_then_unrevoke(monkeypatch):
    r2 = FakeR2()
    assert is_revoked("gym_alpha_ig", r2) is False
    assert revoke("gym_alpha_ig", r2=r2) == ["gym_alpha_ig"]
    assert is_revoked("gym_alpha_ig", r2) is True
    # it landed in R2 as the denylist object, not /data
    data = json.loads(r2.objects["intake/_control/denylist.json"])
    assert data["revoked"] == ["gym_alpha_ig"] and data["updated"]
    assert unrevoke("gym_alpha_ig", r2=r2) == []
    assert is_revoked("gym_alpha_ig", r2) is False


def test_revoke_is_idempotent_and_isolated(monkeypatch):
    r2 = FakeR2()
    revoke("gym_alpha_ig", r2=r2)
    revoke("gym_alpha_ig", r2=r2)              # twice, still once
    revoke("gym_beta_ig", r2=r2)              # a different gym, untouched by alpha
    data = json.loads(r2.objects["intake/_control/denylist.json"])
    assert data["revoked"] == ["gym_alpha_ig", "gym_beta_ig"]
    unrevoke("gym_alpha_ig", r2=r2)          # kills only alpha
    data = json.loads(r2.objects["intake/_control/denylist.json"])
    assert data["revoked"] == ["gym_beta_ig"]


# ---- 2. a revoked link is a 404 on every route ----------------------------------
def test_revoked_link_404s_every_handler(monkeypatch):
    r2 = FakeR2()
    # live before revocation
    assert handle_upload(TOKEN, [_JPEG], r2=r2)[0] == 200
    assert handle_intake_form(TOKEN, _FORM, r2=r2)[0] == 200
    assert handle_portal_intake(TOKEN, _PORTAL_BODY, r2=r2)[0] == 200
    revoke("gym_alpha_ig", r2=r2)
    # dead after revocation: the same 404 as an unknown token, on purpose
    assert handle_upload(TOKEN, [_JPEG], r2=r2)[0] == 404
    assert handle_intake_form(TOKEN, _FORM, r2=r2)[0] == 404
    assert handle_portal_intake(TOKEN, _PORTAL_BODY, r2=r2)[0] == 404
    # restore brings it back
    unrevoke("gym_alpha_ig", r2=r2)
    assert handle_portal_intake(TOKEN, _PORTAL_BODY, r2=r2)[0] == 200


# ---- 3. fail OPEN: a denylist read error never kills every link ------------------
def test_verify_fails_open_on_read_error():
    class BoomR2:
        def get_bytes(self, key):
            raise RuntimeError("R2 down")

    assert is_revoked("gym_alpha_ig", BoomR2()) is False   # not revoked, link works


# ---- 4. the WRITE path never clobbers a denylist it could not read ---------------
def test_revoke_refuses_to_clobber_on_read_error():
    class BoomR2:
        def __init__(self):
            self.put_calls = 0

        def get_bytes(self, key):
            raise RuntimeError("R2 read down")

        def put_bytes(self, key, data, content_type="application/octet-stream"):
            self.put_calls += 1

    boom = BoomR2()
    with pytest.raises(RuntimeError):
        revoke("gym_alpha_ig", r2=boom)
    assert boom.put_calls == 0     # never wrote a fresh list over the unreadable one


# ---- 5. no storage = a clean error, not a crash ---------------------------------
def test_revoke_no_storage_raises(monkeypatch):
    monkeypatch.setattr(intake_web, "_default_r2", lambda: None)
    with pytest.raises(RuntimeError):
        revoke("gym_alpha_ig")


def test_empty_client_key_raises():
    with pytest.raises(ValueError):
        revoke("", r2=FakeR2())
