"""
Trust ladder as data. Asserts: level 0 always cards; level 1 WITHOUT the flag
always cards (the double gate); level 1 WITH the flag skips the card ONLY inside
an approved calendar fixture and cards everything else; config typos fail safe
to level 0; new accounts default to FULL_APPROVAL.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, trust  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.trust import TrustLevel  # noqa: E402


def _acct(level):
    return Account(key="lasso_ig", display_name="IG", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="I", trust=level)


def _draft(creative="lasso_v2_one_screen.png", day_key="2026-07-06"):
    return Draft(draft_id="d", account_key="lasso_ig", platform="instagram",
                 caption="c", hashtags=[], creative_path=f"/lib/{creative}",
                 creative_public_url="", scheduled_for="t",
                 status=DraftStatus.PENDING, day_key=day_key)


def _approve_calendar(keys, month="2026-07"):
    db.kv_set(f"approved_calendar_lasso_ig_{month}", json.dumps(keys))


def test_new_accounts_default_full_approval():
    assert trust.default_trust_for_new_account() == TrustLevel.FULL_APPROVAL


def test_level0_always_cards(monkeypatch):
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    _approve_calendar(["lasso_v2_one_screen.png"])
    assert trust.requires_approval(_acct(TrustLevel.FULL_APPROVAL), _draft()) is True


def test_level1_without_flag_always_cards(monkeypatch):
    monkeypatch.delenv("AGENT_TRUST_LADDER_ENABLED", raising=False)
    _approve_calendar(["lasso_v2_one_screen.png"])
    assert trust.requires_approval(_acct(TrustLevel.ROUTINE_AUTO), _draft()) is True


def test_level1_with_flag_auto_only_inside_approved_calendar(monkeypatch):
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    _approve_calendar(["lasso_v2_one_screen.png"])
    acct = _acct(TrustLevel.ROUTINE_AUTO)
    # inside the human-approved calendar: no card needed (the decision function)
    assert trust.requires_approval(acct, _draft("lasso_v2_one_screen.png")) is False
    # anything off-template still cards
    assert trust.requires_approval(acct, _draft("lasso_v2_new_thing.png")) is True
    # a different month (no approved calendar) still cards
    assert trust.requires_approval(acct, _draft(day_key="2026-08-03")) is True
    # a draft with no day context still cards
    assert trust.requires_approval(acct, _draft(day_key="")) is True


def test_config_typos_fail_safe_to_level0(monkeypatch):
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    _approve_calendar(["lasso_v2_one_screen.png"])
    for bad in ("banana", None, 99, "1; DROP", -3):
        acct = _acct(bad)
        assert trust.requires_approval(acct, _draft()) is True, bad
    assert trust.effective_level(_acct("banana")) == TrustLevel.FULL_APPROVAL


def test_unreadable_calendar_cards_everything(monkeypatch):
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    db.kv_set("approved_calendar_lasso_ig_2026-07", "{not json")
    assert trust.requires_approval(_acct(TrustLevel.ROUTINE_AUTO), _draft()) is True
