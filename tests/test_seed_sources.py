"""
Onboarding seed (Part 5). seed-sources ingests a gym's intake bundle into its
client sources in one step: approved by default (happy path), or held pending with
--review. All-or-nothing on a bad category. Fully OFFLINE (tmp sqlite + tmp file).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_sources as cs, seed_sources  # noqa: E402

_BUNDLE = """\
# offer
- 6 week challenge for $199 (website /pricing)
- Free intro session for new members

# service
- Small group personal training (website /services)

# testimonial
- Sarah lost 30 pounds in 3 months (member Sarah M)

# faq
Do you offer childcare? Yes, all morning classes.
"""


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


def _bundle_file(tmp_path, text=_BUNDLE):
    p = tmp_path / "sources.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---- parser -------------------------------------------------------------------
def test_parse_bundle_categories_and_citations():
    b = seed_sources.parse_bundle(_BUNDLE)
    assert set(b) == {"offer", "service", "testimonial", "faq"}
    assert ("6 week challenge for $199", "website /pricing") in b["offer"]
    assert ("Free intro session for new members", "") in b["offer"]
    # a plain (non-bullet) line is still an item
    assert b["faq"][0][0].startswith("Do you offer childcare?")


# ---- happy path: auto-approve -------------------------------------------------
def test_seed_happy_path_auto_approves(tmp_path):
    created, _ = seed_sources.seed_from_file("gym_alpha_ig",
                                             _bundle_file(tmp_path), review=False)
    assert len(created) == 5
    assert all(s.status == "approved" for s in created)
    # immediately usable by the drafting path
    approved = cs.approved_sources("gym_alpha_ig")
    assert len(approved) == 5
    assert "6 week challenge for $199" in cs.approved_claims("gym_alpha_ig")
    # the citation from the bundle is preserved
    offer = cs.approved_sources("gym_alpha_ig", category="offer")
    cites = {s.text: s.citation for s in offer}
    assert cites["6 week challenge for $199"] == "website /pricing"
    assert cites["Free intro session for new members"] == "intake:gym_alpha_ig"


# ---- review-hold path ---------------------------------------------------------
def test_seed_review_holds_pending(tmp_path):
    created, _ = seed_sources.seed_from_file("gym_beta_ig",
                                             _bundle_file(tmp_path), review=True)
    assert len(created) == 5
    assert all(s.status == "pending" for s in created)
    # NOT usable until a human approves
    assert cs.approved_sources("gym_beta_ig") == []
    assert cs.approved_claims("gym_beta_ig") == []
    assert len(cs.pending_sources("gym_beta_ig")) == 5


# ---- all-or-nothing on a bad category ----------------------------------------
def test_seed_bad_category_stores_nothing(tmp_path):
    bad = _bundle_file(tmp_path, "# offer\n- kept?\n# nonsense\n- bad\n")
    with pytest.raises(ValueError):
        seed_sources.seed_from_file("gym_alpha_ig", bad)
    assert cs.all_sources("gym_alpha_ig") == []


# ---- CLI: happy path exits 0 and stocks the account --------------------------
def test_cli_happy_path_exits_zero(tmp_path, capsys):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["seed-sources", "--account", "gym_alpha_ig",
                 "--file", _bundle_file(tmp_path)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "total 5 source(s), approved" in out
    assert len(cs.approved_sources("gym_alpha_ig")) == 5


# ---- CLI: --review holds pending and says so ---------------------------------
def test_cli_review_holds_pending(tmp_path, capsys):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["seed-sources", "--account", "gym_beta_ig",
                 "--file", _bundle_file(tmp_path), "--review"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "held for review (pending)" in out
    assert cs.approved_sources("gym_beta_ig") == []
    assert len(cs.pending_sources("gym_beta_ig")) == 5


# ---- CLI: missing file / missing account are clear errors --------------------
def test_cli_missing_file_exits_2(tmp_path, capsys):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["seed-sources", "--account", "gym_alpha_ig",
                 "--file", str(tmp_path / "nope.md")])
    assert e.value.code == 2
    assert "no intake bundle" in capsys.readouterr().out


def test_cli_missing_account_exits_2(capsys):
    import agent.__main__ as mm
    with pytest.raises(SystemExit) as e:
        mm.main(["seed-sources", "--file", "whatever.md"])
    assert e.value.code == 2
