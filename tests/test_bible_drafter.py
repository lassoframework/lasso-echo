"""
Intake-to-bible drafter tests. Offline, tmp files. Asserts: a filled intake maps
into the lasso_voice.md structure with the client's own words; missing sections
become explicit TODO blocks (never guessed content); section 6 permission gating
(yes -> entry, anything else -> Skipped block with reason); output lands under
brand_voice/drafts/<client>/ only, never auto-activated.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import bible_drafter  # noqa: E402


FILLED = """# Intake for Iron Path Gym

## 1. Who you are
We are Iron Path, a strength gym for busy parents in Boise.

## 2. Who you talk to
Parents 30 to 50 who want energy back and one hour that is theirs.

## 3. Voice and tone
Straight talk, warm, zero hype. We say train, not workout.

## 4. Hard guardrails
Never show member weigh-in numbers. Never mention the old location.

## 5. Content pillars
Strength for parents. Hook: One hour that is yours.
Body: Three sessions a week beats zero perfect plans.

## 6. Social proof
Quote: I got my energy back in six weeks.
Attribution: Dana P., member since 2025
Permission: yes
Verified: 2026-06-15

Quote: Best gym in Boise hands down.
Attribution: Anonymous review
Permission: no

## 7. CTAs, links, and hashtags
Book your intro at ironpath.example/book. Save this post. #IronPathBoise
"""


def test_filled_intake_maps_into_bible_structure(tmp_path):
    bible, proof = bible_drafter.draft_bible("iron_path", FILLED)
    # lasso_voice.md structure headings
    for heading in ("## 1. Who Iron Path is", "## 2. Who we talk TO (the avatar)",
                    "## 3. Voice and tone", "## 4. Hard guardrails (never violate)",
                    "## 5. Content pillars", "### CTA rotation"):
        assert heading in bible, heading
    # the client's own words, verbatim
    assert "strength gym for busy parents in Boise" in bible
    assert "We say train, not workout." in bible
    assert "Never show member weigh-in numbers." in bible
    assert "One hour that is yours." in bible
    assert "ironpath.example/book" in bible
    # marked draft, never auto-activated
    assert "DRAFT" in bible and "by hand" in bible


def test_missing_sections_become_todo_never_guessed(tmp_path):
    thin = "## 1. Who you are\nWe are a gym.\n"
    bible, _ = bible_drafter.draft_bible("thin_gym", thin)
    assert bible.count("TODO") >= 4          # avatar, voice, guardrails, pillars, CTAs
    assert "We are a gym." in bible
    # nothing invented for the empty sections
    assert "avatar" not in bible.split("## 2.")[1].split("## 3.")[0].replace(
        "Who we talk TO (the avatar)", "")


def test_section6_permission_gating():
    _, proof = bible_drafter.draft_bible("iron_path", FILLED)
    assert "## Entry" in proof
    assert "I got my energy back in six weeks." in proof
    assert "Permission: yes" in proof
    assert "Verified: 2026-06-15" in proof
    # the no-permission quote is skipped with its reason, never an entry
    assert "## Skipped" in proof
    assert "Best gym in Boise" in proof.split("## Skipped")[1]
    assert "permission is not yes" in proof
    assert "Best gym in Boise" not in proof.split("## Skipped")[0]


def test_run_writes_only_under_drafts_client_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bible_drafter, "DRAFTS_DIR", str(tmp_path / "drafts"))
    intake = tmp_path / "intake.md"
    intake.write_text(FILLED, encoding="utf-8")
    bible_path, proof_path = bible_drafter.run("iron_path", str(intake))
    assert bible_path.startswith(str(tmp_path / "drafts" / "iron_path"))
    assert proof_path.startswith(str(tmp_path / "drafts" / "iron_path"))
    assert os.path.exists(bible_path) and os.path.exists(proof_path)
    # only the two draft files, nothing else touched
    assert sorted(os.listdir(os.path.dirname(bible_path))) == ["lasso_voice.md", "social_proof.md"]


# ---- patch_section7: the backfill mechanism's core safety check -------------------
def _bible_with(cta_body, hashtag_body):
    return (
        "# Some Gym Brand Bible\n\n"
        "## 6. Platform rules\n\n"
        f"{bible_drafter.CTA_HEADER}\n{cta_body}\n\n"
        f"{bible_drafter.HASHTAG_HEADER}\n{hashtag_body}\n"
    )


def test_patch_section7_fills_both_todo_blocks():
    text = _bible_with(bible_drafter.TODO, bible_drafter.TODO)
    patched, changed = bible_drafter.patch_section7(text, "Real CTA: book a free tour")
    assert changed == {"cta": True, "hashtags": True}
    assert "Real CTA: book a free tour" in patched
    assert bible_drafter.TODO not in patched


def test_patch_section7_leaves_human_edited_cta_untouched():
    """A block that no longer holds the literal TODO (a human already edited it, or
    a different mechanism already filled it, e.g. a generic fallback CTA) must be
    left BYTE FOR BYTE untouched -- this is the whole safety contract."""
    text = _bible_with("- Learn more at gym.com  (generic fallback, already filled)",
                        bible_drafter.TODO)
    patched, changed = bible_drafter.patch_section7(text, "Real CTA: book a free tour")
    assert changed == {"cta": False, "hashtags": True}
    assert "Learn more at gym.com  (generic fallback, already filled)" in patched
    assert "Real CTA" not in patched.split(bible_drafter.HASHTAG_HEADER)[0]


def test_patch_section7_no_op_when_nothing_is_todo():
    text = _bible_with("- real cta already here", "#realtag")
    patched, changed = bible_drafter.patch_section7(text, "would-be new content")
    assert changed == {"cta": False, "hashtags": False}
    assert patched == text
