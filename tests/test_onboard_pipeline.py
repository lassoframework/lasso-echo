"""
One-command onboarding tests. Asserts: a complete fixture intake produces the
FULL scaffold (draft bible + proof, account entry printed, consent-guard
README, welcome kit PDF, go live checklist); a missing section BLOCKS with the
list and creates nothing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

pytest.importorskip(
    "reportlab",
    reason="reportlab not installed — run tests with .venv/bin/python "
           "(requirements.txt declares it); a missing dep must read as "
           "SKIPPED with this reason, never as red test debt")

from agent import bible_drafter, onboard_pipeline, pdf_report  # noqa: E402

FULL_INTAKE = """# Intake for Iron Path

## 1. Who you are (business + offers)
Strength gym for busy parents in Boise. Public offer wording: the Foundations
onboarding month.

## 2. Who you talk to (avatar)
Parents 30 to 50 who want energy back.

## 3. Voice and tone
Straight talk, warm, zero hype.

## 4. Hard guardrails and locked claims
Never show weigh-in numbers. The 90 percent retention figure is pending
verification and stays locked.

## 5. Content pillars
Strength for parents. Hook: One hour that is yours. Body: Three sessions beats
zero perfect plans.

## 6. Social proof
Quote: I got my energy back.
Attribution: Dana P.
Permission: yes
Verified: 2026-06-15

## 7. CTAs, links, and hashtags
Book your intro at ironpath.example/book. #IronPathBoise

## 8. Posting preferences
Post Tuesday through Saturday evenings. Skip Sundays.

## 9. Consent policy
Members sign a media release at signup; the front desk logs it. Only members
with a release on file may appear.
"""


def test_full_scaffold_from_fixture_intake(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bible_drafter, "DRAFTS_DIR", str(tmp_path / "drafts"))
    intake = tmp_path / "intake.md"
    intake.write_text(FULL_INTAKE, encoding="utf-8")
    out = onboard_pipeline.onboard(str(intake), "iron_path", "Iron Path",
                                   root=str(tmp_path))
    assert out is not None
    # draft bible + proof via the existing path
    assert os.path.exists(out["bible"]) and os.path.exists(out["proof"])
    bible = open(out["bible"], encoding="utf-8").read()
    assert "Straight talk, warm, zero hype." in bible
    # consent-guard README in the client library
    readme = open(os.path.join(str(tmp_path), "content_library", "iron_path",
                               "README.md"), encoding="utf-8").read()
    assert "CONSENT GUARD" in readme
    # welcome kit PDF, real and branded
    assert os.path.getsize(out["kit"]) > 1000
    text = pdf_report.pdf_text(out["kit"])
    assert "Iron Path" in text and "approved by a human" in text
    # account entry + go live checklist printed, with the by-hand trio
    printed = capsys.readouterr().out
    assert 'key="iron_path_ig"' in printed
    assert "GO LIVE CHECKLIST" in printed
    for step in ("SECRETS by hand", "CONNECT LINK", "FIRST APPROVAL"):
        assert step in printed
    assert "never automated" in printed


def test_missing_fields_block_with_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bible_drafter, "DRAFTS_DIR", str(tmp_path / "drafts"))
    partial = FULL_INTAKE.replace(
        "## 9. Consent policy\nMembers sign a media release at signup; the front desk logs it. Only members\nwith a release on file may appear.\n",
        "## 9. Consent policy\n")
    partial = partial.replace("Parents 30 to 50 who want energy back.", "TODO")
    intake = tmp_path / "intake.md"
    intake.write_text(partial, encoding="utf-8")
    out = onboard_pipeline.onboard(str(intake), "iron_path", root=str(tmp_path))
    assert out is None
    printed = capsys.readouterr().out
    assert "BLOCKED" in printed and "ever guessed" in printed
    assert "2. Who you talk to" in printed
    assert "9. Consent policy" in printed
    # nothing was created
    assert not (tmp_path / "drafts").exists()
    assert not (tmp_path / "content_library").exists()
