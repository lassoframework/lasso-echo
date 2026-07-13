"""
intake-onboard chain (launch hardening Part 2). One command runs the whole
onboarding chain on an intake payload: bible drafted and HELD, sources landed
PENDING (never auto-approved), library scanned when media exists, month plan
reported, preflight printed. Idempotent: a re-run adds no duplicate sources and
never overwrites the bible draft. Unapproved sources never reach a draft.
Fully OFFLINE (tmp sqlite, tmp drafts dir, tmp library).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import accounts as _accounts  # noqa: E402
from agent import bible_drafter, client_content, client_sources as cs  # noqa: E402
from agent import intake_onboard  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402

_INTAKE = """\
## 1. Who we are
Family owned gym coaching our community since 2015.

## 2. Who we talk to
Busy parents and beginners.

## 3. Voice and tone
Warm, direct, encouraging.

# offer
- 6 week challenge for $199 (website /pricing)
- Free intro session for new members

# service
- Small group personal training (website /services)

# faq
- Do you offer childcare? Yes, mornings.
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_PLAN_MONTH_ENABLED", raising=False)
    monkeypatch.setattr(bible_drafter, "DRAFTS_DIR", str(tmp_path / "drafts"))
    lib = tmp_path / "alpha_lib"
    lib.mkdir()
    for i in range(3):
        (lib / f"photo_{i}.jpg").write_bytes(b"\xff\xd8\xffFAKE")
    acct = Account(key="gym_alpha_ig", display_name="Gym Alpha",
                   platform=Platform.INSTAGRAM, token_env="T_ALPHA",
                   target_id_env="TID_ALPHA", slack_channel="C_ALPHA",
                   library_prefix=str(lib),
                   voice_doc=str(tmp_path / "alpha_voice.md"))
    monkeypatch.setattr(_accounts, "ACCOUNTS", [acct])
    intake = tmp_path / "intake.md"
    intake.write_text(_INTAKE, encoding="utf-8")
    yield {"intake": str(intake), "tmp": tmp_path, "acct": acct}


# ---- 1. the full chain on a sample intake --------------------------------------
def test_full_chain(_env):
    out = intake_onboard.run_onboard("gym_alpha_ig", _env["intake"],
                                     month="2026-08")
    steps = out["steps"]
    # bible drafted and held (in the drafts dir, never the active path)
    assert "held for approval" in steps["bible"]
    bible_path = os.path.join(str(_env["tmp"] / "drafts"), "gym_alpha_ig",
                              "lasso_voice.md")
    assert os.path.exists(bible_path)
    assert not os.path.exists(_env["acct"].voice_doc)   # active bible untouched
    # sources landed PENDING, per category, never auto-approved
    assert "4 landed PENDING" in steps["sources"]
    assert len(cs.pending_sources("gym_alpha_ig")) == 4
    assert cs.approved_sources("gym_alpha_ig") == []
    offers = cs.pending_sources("gym_alpha_ig", category="offer")
    assert {s.citation for s in offers} == {"website /pricing",
                                            "intake:gym_alpha_ig"}
    # library scanned (media present), regen honestly reported not applicable
    assert "3 media item(s)" in steps["library"]
    # plan respects its own flag (off in this test)
    assert "AGENT_PLAN_MONTH_ENABLED off" in steps["plan"]


# ---- 2. re-run: no duplicate sources, bible draft not overwritten ---------------
def test_rerun_is_idempotent(_env):
    intake_onboard.run_onboard("gym_alpha_ig", _env["intake"], month="2026-08")
    first_bible = open(os.path.join(str(_env["tmp"] / "drafts"), "gym_alpha_ig",
                                    "lasso_voice.md"), encoding="utf-8").read()
    out2 = intake_onboard.run_onboard("gym_alpha_ig", _env["intake"],
                                      month="2026-08")
    # no source duplicated: still exactly 4, and the step says nothing new
    assert len(cs.all_sources("gym_alpha_ig")) == 4
    assert "nothing new" in out2["steps"]["sources"]
    assert "4 already stored" in out2["steps"]["sources"]
    # the bible draft was not overwritten
    assert "not overwritten" in out2["steps"]["bible"]
    second_bible = open(os.path.join(str(_env["tmp"] / "drafts"), "gym_alpha_ig",
                                     "lasso_voice.md"), encoding="utf-8").read()
    assert second_bible == first_bible


# ---- 3. unapproved sources never draft ------------------------------------------
def test_unapproved_sources_never_draft(_env, monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    intake_onboard.run_onboard("gym_alpha_ig", _env["intake"], month="2026-08")
    voice = VoiceDoc(raw="v\n#Tag", hashtags=["#Tag"], ctas=["Save this post."])
    d = client_content.build_client_draft(
        _env["acct"], "2026-08-01", voice, _env["acct"].library_prefix)
    assert d is None                     # 4 pending sources, 0 approved: no draft
    # after a human approves, the same account drafts
    cs.approve_all("gym_alpha_ig")
    d2 = client_content.build_client_draft(
        _env["acct"], "2026-08-01", voice, _env["acct"].library_prefix)
    assert d2 is not None and d2.caption.strip()


# ---- 4. unknown account is a clear error ----------------------------------------
def test_unknown_account_raises(_env):
    with pytest.raises(ValueError):
        intake_onboard.run_onboard("gym_ghost_ig", _env["intake"])


# ---- 5. CLI: full chain prints every step + the preflight report, exit 0 --------
def test_cli_prints_chain_and_preflight(_env, capsys):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["intake-onboard", "--account", "gym_alpha_ig",
                 "--file", _env["intake"], "--month", "2026-08"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for step in ("bible", "sources", "library", "plan"):
        assert step in out
    assert "preflight: gym_alpha_ig" in out          # the preflight report printed
    assert "verdict:" in out


# ---- 6. CLI: missing args / missing file are clear errors -----------------------
def test_cli_missing_file_exits_2(_env, capsys):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["intake-onboard", "--account", "gym_alpha_ig",
                 "--file", "/nope/intake.md"])
    assert e.value.code == 2


def test_cli_missing_account_arg_exits_2(_env):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["intake-onboard", "--file", _env["intake"]])
    assert e.value.code == 2
