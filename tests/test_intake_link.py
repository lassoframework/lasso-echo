"""
Phase 3: the mint path. intake_web.link_for() mints signed intake/upload links
from the shared secret (no per-gym env var); ghl_intake.upload_link_for() mints
the same way and honors a legacy env override during the cutover; a minted link
round-trips back to its client key through the verify path. The secret is never
part of a link.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, ghl_intake, intake_tokens, intake_web  # noqa: E402

SECRET = "intake-signing-secret-for-link-tests"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, SECRET)
    monkeypatch.delenv("AGENT_UPLOAD_BASE_URL", raising=False)
    for name in list(os.environ):
        if name.startswith("AGENT_INTAKE_TOKEN_"):
            monkeypatch.delenv(name, raising=False)
    yield


def test_link_for_mints_absolute_when_base_set(monkeypatch):
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://intake.lasso.test/")
    upload = intake_web.link_for("gym_alpha_ig", kind="u")
    form = intake_web.link_for("gym_alpha_ig", kind="intake")
    assert upload.startswith("https://intake.lasso.test/u/")
    assert form.startswith("https://intake.lasso.test/intake/")
    assert SECRET not in upload and SECRET not in form


def test_link_for_relative_when_no_base():
    upload = intake_web.link_for("gym_alpha_ig", kind="u")
    assert upload.startswith("/u/")


def test_minted_link_round_trips_to_client_key(monkeypatch):
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://intake.lasso.test")
    upload = intake_web.link_for("gym_alpha_ig", kind="u")
    token = upload.rsplit("/u/", 1)[1]
    assert intake_web.client_for_token(token) == "gym_alpha_ig"


def test_link_for_empty_without_secret(monkeypatch):
    monkeypatch.delenv(config.INTAKE_SIGNING_SECRET_ENV, raising=False)
    assert intake_web.link_for("gym_alpha_ig") == ""


def test_ghl_upload_link_mints_from_secret(monkeypatch):
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://intake.lasso.test")
    link = ghl_intake.upload_link_for("gym_alpha_ig")
    assert link.startswith("https://intake.lasso.test/u/")
    token = link.rsplit("/u/", 1)[1]
    assert intake_web.client_for_token(token) == "gym_alpha_ig"


def test_ghl_legacy_env_override_wins(monkeypatch):
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://intake.lasso.test")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYM_ALPHA_IG", "pinned_legacy_tok")
    assert ghl_intake.upload_link_for("gym_alpha_ig") == \
        "https://intake.lasso.test/u/pinned_legacy_tok"


def test_ghl_empty_without_base(monkeypatch):
    monkeypatch.delenv("AGENT_UPLOAD_BASE_URL", raising=False)
    assert ghl_intake.upload_link_for("gym_alpha_ig") == ""
