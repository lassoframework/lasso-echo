"""
ghl_intake.upload_link_for base-url and per-tenant token guards (intake item 4).

Root cause covered here: upload_link_for read AGENT_UPLOAD_BASE_URL RAW, so a
leftover setup placeholder ("<paste the Step 7 domain here>") became the link
host, and a base that was unset/non-http yielded no usable link. It now routes
through intake_web's ONE placeholder-safe resolver.

Also asserts distinct tenant keys mint distinct tokens/links, and that a legacy
AGENT_INTAKE_TOKEN_<KEY> override is honored ONLY for the exact tenant it names.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ghl_intake, intake_web  # noqa: E402

_DEFAULT = intake_web._DEFAULT_UPLOAD_BASE_URL
_SIGNING = "AGENT_INTAKE_SIGNING_SECRET"


def _clear_legacy(monkeypatch):
    for name in list(os.environ):
        if name.startswith("AGENT_INTAKE_TOKEN_"):
            monkeypatch.delenv(name, raising=False)


# ---- base-url resolution: placeholder / unset / non-http fall back to default ----

def test_placeholder_base_falls_back_to_default_host(monkeypatch):
    _clear_legacy(monkeypatch)
    monkeypatch.setenv(_SIGNING, "s3cret-signing-value")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "<paste the Step 7 domain here>")
    link = ghl_intake.upload_link_for("gritx")
    assert "<paste" not in link and "<" not in link       # no dead placeholder
    assert link.startswith(_DEFAULT + "/u/")


def test_unset_base_falls_back_to_default_host(monkeypatch):
    _clear_legacy(monkeypatch)
    monkeypatch.setenv(_SIGNING, "s3cret-signing-value")
    monkeypatch.delenv("AGENT_UPLOAD_BASE_URL", raising=False)
    link = ghl_intake.upload_link_for("gritx")
    assert link.startswith(_DEFAULT + "/u/")


def test_non_http_base_falls_back_to_default_host(monkeypatch):
    _clear_legacy(monkeypatch)
    monkeypatch.setenv(_SIGNING, "s3cret-signing-value")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "gritx.example.com")   # no scheme
    link = ghl_intake.upload_link_for("gritx")
    assert link.startswith(_DEFAULT + "/u/")


def test_valid_https_base_is_honored(monkeypatch):
    _clear_legacy(monkeypatch)
    monkeypatch.setenv(_SIGNING, "s3cret-signing-value")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://up.echo.test/")
    link = ghl_intake.upload_link_for("gritx")
    assert link.startswith("https://up.echo.test/u/")     # trailing slash trimmed
    assert "//u/" not in link.replace("https://", "")


# ---- distinct tenants -> distinct tokens/links ----------------------------------

def test_distinct_tenants_get_distinct_links(monkeypatch):
    _clear_legacy(monkeypatch)
    monkeypatch.setenv(_SIGNING, "s3cret-signing-value")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://up.echo.test")
    gritx = ghl_intake.upload_link_for("gritx")
    topfuel = ghl_intake.upload_link_for("topfuel")
    assert gritx and topfuel
    assert gritx != topfuel
    gritx_token = gritx.rsplit("/u/", 1)[1]
    topfuel_token = topfuel.rsplit("/u/", 1)[1]
    assert gritx_token != topfuel_token                   # distinct by construction


# ---- legacy override is pinned to the exact tenant it names ----------------------

def test_legacy_override_honored_only_for_its_own_tenant(monkeypatch):
    _clear_legacy(monkeypatch)
    monkeypatch.setenv(_SIGNING, "s3cret-signing-value")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://up.echo.test")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GRITX", "legacy_gritx_pin")

    gritx = ghl_intake.upload_link_for("gritx")
    topfuel = ghl_intake.upload_link_for("topfuel")

    # gritx keeps its pinned legacy token
    assert gritx == "https://up.echo.test/u/legacy_gritx_pin"
    # topfuel never inherits gritx's legacy token; it gets its own signed token
    assert "legacy_gritx_pin" not in topfuel
    assert topfuel.startswith("https://up.echo.test/u/")
    assert topfuel != gritx
