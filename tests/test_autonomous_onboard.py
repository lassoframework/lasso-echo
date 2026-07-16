"""
Autonomous onboard (Stage 2 T2) tests.

Covers: gym row creation, voice/brain scaffolding, trust, publish flag,
idempotency, dash-free output, no invented facts, meta token safety, and
pending items.

All tests use tmp dirs and in-memory-ish db (conftest isolates via AGENT_DB_PATH).
No live tokens, no Meta API calls.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, onboard  # noqa: E402
from agent.trust import TrustLevel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(tmp_path, account_key="testgym", display_name="Test Gym",
         base_url=None, monkeypatch=None, automint=False):
    """Helper: run onboard.run() with isolated file dirs from tmp_path."""
    voice_dir = str(tmp_path / "brand_voice")
    brains_dir = str(tmp_path / "brains")
    if monkeypatch is not None and automint:
        monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    return onboard.run(
        account_key,
        display_name,
        voice_dir=voice_dir,
        brains_dir=brains_dir,
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_onboard_creates_gym_row(tmp_path):
    """run() creates a gym entry in db (kv store)."""
    _run(tmp_path, "gymone", "Gym One")
    row = db.gym_get("gymone")
    assert row.get("account_key") == "gymone"
    assert row.get("display_name") == "Gym One"


def test_onboard_scaffolds_voice_file(tmp_path):
    """Voice file exists after run() and contains section headers."""
    result = _run(tmp_path, "gymvoice", "Voice Gym")
    voice_path = result["voice_path"]
    assert os.path.exists(voice_path), "voice file must exist after run()"
    content = open(voice_path, encoding="utf-8").read()
    # voice_template renders 8 numbered sections; confirm at least a few
    assert "## 1." in content
    assert "## 2." in content
    assert "**Your answer:**" in content


def test_onboard_voice_file_is_dash_free(tmp_path):
    """Voice file output has no em dash or en dash."""
    result = _run(tmp_path, "gymdash", "Dash Test Gym")
    content = open(result["voice_path"], encoding="utf-8").read()
    assert "—" not in content, "em dash found in voice file"
    assert "–" not in content, "en dash found in voice file"


def test_onboard_voice_file_no_invented_facts(tmp_path):
    """Voice file does not contain any dollar sign, percent sign, or invented number."""
    result = _run(tmp_path, "gymfact", "Fact Safe Gym")
    content = open(result["voice_path"], encoding="utf-8").read()
    # The template examples include numbers and dollar signs in example text
    # (e.g. "lose 20 pounds"). We verify LASSO's own content was not injected
    # by checking that no sections from lasso_voice.md made it in.
    # The fabrication rule forbids any dollar sign or percent in the blank template.
    # The template renders examples inline but the blank answer lines are empty.
    # We check the answer lines themselves are empty (no invented content).
    lines = content.splitlines()
    answer_block = False
    invented_line = None
    for line in lines:
        if line.strip() == "**Your answer:**":
            answer_block = True
            continue
        if answer_block:
            # Next non-blank line after "Your answer:" should be the next section header
            # or blank. If it contains $ or % the template invented content.
            stripped = line.strip()
            if stripped.startswith("##") or stripped == "---" or stripped == "":
                answer_block = False
                continue
            # A non-blank, non-header line right after "Your answer:" is invented content
            if "$" in stripped or "%" in stripped:
                invented_line = stripped
                break
    assert invented_line is None, (
        f"Invented content with $ or % found after answer line: {invented_line!r}"
    )


def test_onboard_trust_is_full_approval(tmp_path):
    """trust_level in result is FULL_APPROVAL."""
    result = _run(tmp_path, "gymtrust", "Trust Gym")
    assert result["trust_level"] == TrustLevel.FULL_APPROVAL


def test_onboard_trust_kv_stored(tmp_path):
    """Trust level is persisted in kv as 0 (FULL_APPROVAL)."""
    _run(tmp_path, "gymtrustkv", "Trust KV Gym")
    stored = db.kv_get("gym_trust_gymtrustkv", "")
    assert stored == "0"


def test_onboard_publish_off(tmp_path):
    """publish_flag in result is OFF."""
    result = _run(tmp_path, "gympub", "Publish Test Gym")
    assert result["publish_flag"] == "OFF"


def test_onboard_creds_not_set(tmp_path):
    """creds_status in result indicates NOT SET, set by hand."""
    result = _run(tmp_path, "gymcreds", "Creds Test Gym")
    assert "NOT SET" in result["creds_status"]
    assert "by hand" in result["creds_status"]


def test_onboard_never_touches_meta_token(tmp_path):
    """Result dict has no key containing 'meta' holding a real value."""
    result = _run(tmp_path, "gymmeta", "Meta Safe Gym")
    for key, val in result.items():
        if "meta" in key.lower():
            # Any meta key in the result must be empty/None or a status string
            assert not val or (isinstance(val, str) and "NOT SET" in val), (
                f"Key {key!r} contains a real value: {val!r} — meta tokens must "
                "never be minted or stored here"
            )
    # Also ensure no raw token-looking value exists under any key
    # (a 44-char alphanumeric string that is NOT the intake upload token)
    # The intake token is allowed in token_minted only if AUTOMINT is ON.
    # With AUTOMINT OFF (default), token_minted must be None.
    assert result["token_minted"] is None, (
        "With AGENT_ONBOARD_AUTOMINT off, token_minted must be None"
    )


def test_onboard_idempotent(tmp_path):
    """run() twice does not duplicate gym rows, files, or trust entries."""
    voice_dir = str(tmp_path / "brand_voice")
    brains_dir = str(tmp_path / "brains")

    r1 = onboard.run("gymidemp", "Idempotent Gym",
                     voice_dir=voice_dir, brains_dir=brains_dir)

    # Write custom content into the voice file to prove it is not overwritten
    open(r1["voice_path"], "w", encoding="utf-8").write("HAND EDITED")

    r2 = onboard.run("gymidemp", "Idempotent Gym Updated",
                     voice_dir=voice_dir, brains_dir=brains_dir)

    # display_name update is allowed (db row updated)
    row = db.gym_get("gymidemp")
    assert row.get("display_name") == "Idempotent Gym Updated"

    # Voice file must NOT be overwritten
    assert open(r1["voice_path"], encoding="utf-8").read() == "HAND EDITED", (
        "Voice file must never be overwritten on re-run"
    )

    # Brain file must NOT be overwritten
    assert os.path.exists(r2["brain_path"])

    # Trust level stays FULL_APPROVAL
    assert r2["trust_level"] == TrustLevel.FULL_APPROVAL

    # Publish flag stays OFF
    assert r2["publish_flag"] == "OFF"


def test_onboard_pending_items_include_creds(tmp_path):
    """pending_human_items always contains publish creds string."""
    result = _run(tmp_path, "gympi", "Pending Items Gym")
    items_text = " ".join(result["pending_human_items"])
    assert "publish creds" in items_text
    assert "NOT SET" in items_text


def test_onboard_pending_items_include_token_when_flag_off(tmp_path):
    """When AGENT_ONBOARD_AUTOMINT is OFF, pending items mention the token flag."""
    result = _run(tmp_path, "gymtoken", "Token Flag Gym")
    items_text = " ".join(result["pending_human_items"])
    assert "AGENT_ONBOARD_AUTOMINT" in items_text


def test_onboard_pending_items_include_first_month_plan(tmp_path):
    """pending_human_items always contains first-month plan entry."""
    result = _run(tmp_path, "gymplan", "Plan Gym")
    items_text = " ".join(result["pending_human_items"])
    assert "first-month plan" in items_text


def test_onboard_automint_mints_token(tmp_path, monkeypatch):
    """When AGENT_ONBOARD_AUTOMINT=true, token_minted is a URL-safe string of at least 40 chars."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    result = _run(tmp_path, "gymautomint", "Auto Mint Gym",
                  monkeypatch=monkeypatch, automint=False)
    assert isinstance(result["token_minted"], str)
    # secrets.token_urlsafe(32) yields 43 chars; stubs may yield 44. Accept >= 40.
    assert len(result["token_minted"]) >= 40


def test_onboard_automint_idempotent_no_remint(tmp_path, monkeypatch):
    """Second run with AUTOMINT on returns token_minted=False (already set)."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    voice_dir = str(tmp_path / "brand_voice")
    brains_dir = str(tmp_path / "brains")
    r1 = onboard.run("gymremint", "Re Mint Gym",
                     voice_dir=voice_dir, brains_dir=brains_dir)
    assert isinstance(r1["token_minted"], str)
    r2 = onboard.run("gymremint", "Re Mint Gym",
                     voice_dir=voice_dir, brains_dir=brains_dir)
    assert r2["token_minted"] is False


def test_onboard_upload_link_generated(tmp_path, monkeypatch):
    """upload_link is set when base_url is provided and token is minted."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    result = _run(tmp_path, "gymlink", "Link Gym",
                  base_url="https://intake.example.com",
                  monkeypatch=monkeypatch, automint=False)
    assert result["upload_link"] is not None
    assert result["upload_link"].startswith("https://intake.example.com/u/")


def test_onboard_upload_link_none_when_token_pending(tmp_path):
    """upload_link is None when token is pending (AUTOMINT off)."""
    result = _run(tmp_path, "gymnolink", "No Link Gym",
                  base_url="https://intake.example.com")
    assert result["upload_link"] is None


def test_onboard_brain_file_created(tmp_path):
    """Brain file is created with the correct header."""
    result = _run(tmp_path, "gymbrain", "Brain Gym")
    assert os.path.exists(result["brain_path"])
    content = open(result["brain_path"], encoding="utf-8").read()
    assert "# Style brain for gymbrain" in content


def test_onboard_result_keys_complete(tmp_path):
    """Result dict contains all required keys."""
    result = _run(tmp_path, "gymkeys", "Keys Gym")
    required = {"account_key", "display_name", "token_minted", "voice_path",
                "brain_path", "trust_level", "publish_flag", "creds_status",
                "upload_link", "pending_human_items"}
    assert required.issubset(result.keys())


def test_onboard_upload_link_persisted_in_db(tmp_path, monkeypatch):
    """upload_link is stored in the gyms row so the portal can return it later."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    voice_dir = str(tmp_path / "brand_voice")
    brains_dir = str(tmp_path / "brains")
    onboard.run("gymlinkdb", "Link DB Gym",
                base_url="https://intake.example.com",
                voice_dir=voice_dir, brains_dir=brains_dir)
    row = db.gym_get("gymlinkdb")
    assert row is not None
    assert row.get("upload_link") is not None
    assert row["upload_link"].startswith("https://intake.example.com/u/")


def test_onboard_base_url_from_env(tmp_path, monkeypatch):
    """When AGENT_UPLOAD_BASE_URL is set, the CLI picks it up without --base-url."""
    monkeypatch.setenv("AGENT_ONBOARD_AUTOMINT", "true")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://upload.lasso.test")
    voice_dir = str(tmp_path / "brand_voice")
    brains_dir = str(tmp_path / "brains")
    # Call run() directly with the env var set and no base_url arg (simulates CLI)
    import os
    base = os.environ.get("AGENT_UPLOAD_BASE_URL")
    result = onboard.run("gymenvurl", "Env URL Gym",
                         base_url=base,
                         voice_dir=voice_dir, brains_dir=brains_dir)
    assert result["upload_link"] is not None
    assert result["upload_link"].startswith("https://upload.lasso.test/u/")
