"""
Per-client approval routing (10-gym launch isolation).

H1: an approval card posts to the draft account's own slack_channel when the
Account sets one; accounts without one keep the default channel — so today's
LASSO accounts are unchanged.

H2: an account's own approvers may act on that account's drafts; the global
approver may act on everything; nobody else may act on anything. Accounts
with no approvers list keep the global-approver-only gate.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.drafter import Draft, DraftStatus
from agent.slack_surface import SlackPoster


def _client_account():
    return Account(key="gym_alpha_ig", display_name="Gym Alpha IG",
                   platform=Platform.INSTAGRAM,
                   token_env="GYM_ALPHA_TOK", target_id_env="GYM_ALPHA_TGT",
                   slack_channel="C_GYM_ALPHA",
                   approvers=["U_GYM_ALPHA_OWNER"])


def _draft(account_key, status=DraftStatus.PENDING):
    return Draft(draft_id=f"d-{account_key}", account_key=account_key,
                 platform="instagram", caption="c", hashtags=[],
                 creative_path="", creative_public_url="",
                 scheduled_for="2026-07-08T18:30:00+00:00", status=status,
                 day_key="2026-07-08", draft_type="feed")


class _RecordingHTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"url": url, "payload": json.loads(data)})

        class _R:
            @staticmethod
            def json():
                return {"ok": True, "channel": "Cx", "ts": "t1"}
        return _R()


# ---------------------------------------------------------------------------
# H1 — card routes to the account's own channel
# ---------------------------------------------------------------------------

def test_card_routes_to_client_channel(monkeypatch):
    acct = _client_account()
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    http = _RecordingHTTP()
    poster = SlackPoster(http=http, token="xoxb-test", channel="C_LASSO_DEFAULT")
    poster.post_approval_card(_draft("gym_alpha_ig"))
    assert http.calls, "card was not posted"
    assert http.calls[0]["payload"]["channel"] == "C_GYM_ALPHA"


def test_card_keeps_default_channel_without_account_channel(monkeypatch):
    acct = Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")  # no slack_channel
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    http = _RecordingHTTP()
    poster = SlackPoster(http=http, token="xoxb-test", channel="C_LASSO_DEFAULT")
    poster.post_approval_card(_draft("lasso_ig"))
    assert http.calls[0]["payload"]["channel"] == "C_LASSO_DEFAULT"


def test_notice_stays_on_default_channel(monkeypatch):
    """Ops notices are not per-client; they stay on the poster's channel."""
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [_client_account()])
    http = _RecordingHTTP()
    poster = SlackPoster(http=http, token="xoxb-test", channel="C_LASSO_DEFAULT")
    poster.post_notice("ops line")
    assert http.calls[0]["payload"]["channel"] == "C_LASSO_DEFAULT"


# ---------------------------------------------------------------------------
# H2 — per-account approver gate
# ---------------------------------------------------------------------------

def test_client_approver_can_act_on_own_account(monkeypatch):
    from agent.approvals import handle_action
    acct = _client_account()
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    res = handle_action("skip", _draft("gym_alpha_ig"),
                        actor_slack_id="U_GYM_ALPHA_OWNER", account=acct)
    assert res.ok, res.detail


def test_client_approver_cannot_act_on_other_account(monkeypatch):
    from agent.approvals import handle_action
    other = Account(key="gym_beta_ig", display_name="Gym Beta IG",
                    platform=Platform.INSTAGRAM,
                    token_env="B_TOK", target_id_env="B_TGT",
                    slack_channel="C_GYM_BETA", approvers=["U_GYM_BETA_OWNER"])
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [_client_account(), other])
    res = handle_action("skip", _draft("gym_beta_ig"),
                        actor_slack_id="U_GYM_ALPHA_OWNER", account=other)
    assert not res.ok
    assert "not the approver" in res.detail or "Denied" in res.detail


def test_global_approver_can_act_on_client_account(monkeypatch):
    from agent import config
    from agent.approvals import handle_action
    acct = _client_account()
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    res = handle_action("skip", _draft("gym_alpha_ig"),
                        actor_slack_id=config.APPROVER_SLACK_ID, account=acct)
    assert res.ok, res.detail


def test_stranger_denied_on_account_without_approvers(monkeypatch):
    from agent.approvals import handle_action
    acct = Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    res = handle_action("skip", _draft("lasso_ig"),
                        actor_slack_id="U_STRANGER", account=acct)
    assert not res.ok
