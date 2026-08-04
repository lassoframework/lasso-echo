"""Targeted test for the listener.py fix made alongside the welcome-post
build: _redraft_with_note must preserve draft_type and is_story, or editing a
special-cased draft (claim_promotion, welcome_multi) silently loses its type
and the next Approve falls through to plain single-target publish behavior.

(_act / on_edit_submit themselves are closures inside run_listener() wired to
slack_bolt's App + Socket Mode and are not independently unit-testable without
that scaffolding -- a pre-existing gap, not one this test attempts to close.)
"""

from agent.drafter import Draft, DraftStatus
from agent.listener import _redraft_with_note


def test_redraft_preserves_welcome_multi_draft_type():
    old = Draft(draft_id="wel_abc", account_key="lasso_ig", platform="instagram",
               caption="old caption", hashtags=[], creative_path="", creative_public_url="u",
               scheduled_for="2026-08-04", status=DraftStatus.PENDING,
               draft_type="welcome_multi")
    new = _redraft_with_note(old, "new caption")
    assert new.draft_type == "welcome_multi"
    assert new.caption == "new caption"
    assert new.draft_id == "wel_abce"


def test_redraft_preserves_is_story():
    old = Draft(draft_id="wel_abc_story", account_key="lasso_ig", platform="instagram",
               caption="", hashtags=[], creative_path="", creative_public_url="u",
               scheduled_for="2026-08-04", status=DraftStatus.PENDING, is_story=True)
    new = _redraft_with_note(old, "note")
    assert new.is_story is True


def test_redraft_default_draft_type_unaffected():
    old = Draft(draft_id="feed_1", account_key="lasso_ig", platform="instagram",
               caption="old", hashtags=[], creative_path="", creative_public_url="u",
               scheduled_for="2026-08-04", status=DraftStatus.PENDING)
    new = _redraft_with_note(old, "note")
    assert new.draft_type == ""
    assert new.is_story is False
