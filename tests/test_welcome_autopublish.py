"""
Welcome-only auto-publish: new-client welcome posts (topic_type == "WELCOME") publish
hands-free when AGENT_WELCOME_AUTOPUBLISH is armed, WITHOUT enabling portfolio-wide
auto-approve. Every other LASSO post still cards for a tap. Offline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, runner  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402


class FakePoster:
    def __init__(self):
        self.cards = []
        self.notices = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True, "channel": "C1", "ts": "t1"}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def mark_expired(self, d):
        return {"ok": True}


def _draft(topic="WELCOME"):
    return Draft(draft_id="w1", account_key="lasso_ig", platform=Platform.INSTAGRAM,
                 caption="Welcome to the LASSO family, Iron Gym.", hashtags=[],
                 creative_path="welcome_iron.png",
                 creative_public_url="https://r2/welcome_iron.png",
                 scheduled_for="2026-08-13T18:30:00+00:00", status=DraftStatus.PENDING,
                 day_key="2026-08-13", draft_type="feed", topic_type=topic)


def _publisher(published):
    def publish(draft, account):
        published.append(draft.draft_id)

        class R:
            ok = True
            mode = "published"
            media_id = "m1"
        return R()
    return publish


def _env(monkeypatch, **flags):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    for k in ("AGENT_AUTO_APPROVE_ENABLED", "AGENT_WELCOME_AUTOPUBLISH",
              "AGENT_PUBLISH_ENABLED", "AGENT_PORTAL_SOCIAL_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    for k, v in flags.items():
        monkeypatch.setenv(k, v)


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_WELCOME_AUTOPUBLISH", raising=False)
    assert config.welcome_autopublish_enabled() is False


def test_welcome_autopublishes_when_armed(monkeypatch, tmp_path):
    _env(monkeypatch, AGENT_WELCOME_AUTOPUBLISH="true")
    published = []
    monkeypatch.setattr("agent.meta_publisher.publish", _publisher(published))
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    d = _draft("WELCOME")
    runner._post_and_save(d, store, poster, idempotent=False)
    assert published == ["w1"]                       # published hands-free
    assert d.status == DraftStatus.APPROVED
    assert poster.cards == []                          # no approval card
    assert any("Auto-published" in n for n in poster.notices)


def test_non_welcome_still_cards_when_only_welcome_flag_on(monkeypatch, tmp_path):
    _env(monkeypatch, AGENT_WELCOME_AUTOPUBLISH="true")
    published = []
    monkeypatch.setattr("agent.meta_publisher.publish", _publisher(published))
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    d = _draft("STANDARD")                             # a regular LASSO post
    runner._post_and_save(d, store, poster, idempotent=False)
    assert published == []                             # NOT auto-published
    assert d.status == DraftStatus.PENDING
    assert poster.cards and poster.cards[0].draft_id == "w1"   # cards for a tap


def test_welcome_flag_off_still_cards(monkeypatch, tmp_path):
    _env(monkeypatch)                                  # welcome autopub OFF
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    d = _draft("WELCOME")
    runner._post_and_save(d, store, poster, idempotent=False)
    assert d.status == DraftStatus.PENDING and poster.cards          # held for approval
