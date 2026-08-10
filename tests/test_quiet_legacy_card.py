"""
FIX 2: quiet the legacy LASSO daily-draft card when autopublish is on.

When AGENT_CALENDAR_AUTOPUBLISH is armed, content_calendar is the source of truth
for LASSO, so run_daily must NOT also build/card the legacy LASSO daily rotation/
infographic/library-fallback draft. The book/welcome/demo queues and client-gym
drafting are untouched.

All offline: the end-of-cycle publish_due() self-guards on AGENT_PUBLISH_ENABLED
(left OFF here), so it no-ops before constructing any real store. No network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.drafter import Draft
from agent.runner import run_daily
from agent.store import PendingStore


DAY = "2026-07-13"  # a Monday
SCHEDULED = f"{DAY}T18:30:00+00:00"

_VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class _FakePoster:
    def __init__(self):
        self.cards = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"channel": "C1", "ts": "ts1"}

    def post_notice(self, text):
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        pass


def _lasso_account():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")


def _client_account(tmp_path):
    # A client gym with its own slack channel so the channel-ownership guard passes.
    return Account(key="gritx", display_name="GritX",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT",
                   slack_channel="C_CLIENT")


def _legacy_feed_draft(account, day_key, **kwargs):
    return Draft(draft_id=f"legacy-{account.key}-{day_key}", account_key=account.key,
                 platform="instagram", caption="Legacy daily.",
                 hashtags=["#x"], creative_path="", creative_public_url="",
                 scheduled_for=SCHEDULED, draft_type="feed")


def _arm(monkeypatch, tmp_path, autopublish):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    # Legacy chain (rotation OFF) so the LASSO daily draft comes from the
    # social-proof / infographic / library legs the guard covers.
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "false")
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "false")
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "false")
    monkeypatch.setenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", "false")
    if autopublish:
        monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    else:
        monkeypatch.delenv("AGENT_CALENDAR_AUTOPUBLISH", raising=False)
    # Keep publish OFF so the end-of-cycle publish_due() no-ops before any network.
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")
    return str(voice), str(lib), db_path


class _Spy:
    def __init__(self, ret=None):
        self.calls = []
        self._ret = ret

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._ret


def _spies(monkeypatch, legacy_ret=None):
    """Patch every leg of the legacy LASSO daily draft chain with a spy."""
    social = _Spy(ret=legacy_ret)
    info = _Spy(ret=legacy_ret)
    lib_fallback = _Spy(ret=legacy_ret)
    monkeypatch.setattr("agent.runner.build_social_proof_draft", social)
    monkeypatch.setattr("agent.runner.build_daily_infographic_draft", info)
    monkeypatch.setattr("agent.runner.draft_post", lib_fallback)
    # No stories: keep the cycle to just the feed leg under test.
    monkeypatch.setattr("agent.runner.build_story_draft", _Spy(ret=None))
    return social, info, lib_fallback


# ---------------------------------------------------------------------------

def test_autopublish_on_skips_legacy_lasso_daily_card(monkeypatch, tmp_path):
    voice, lib, db_path = _arm(monkeypatch, tmp_path, autopublish=True)
    social, info, lib_fallback = _spies(
        monkeypatch, legacy_ret=_legacy_feed_draft(_lasso_account(), DAY))
    poster = _FakePoster()

    out = run_daily(poster=poster, voice_path=voice, library_path=lib,
                    scheduled_for=SCHEDULED, accounts=[_lasso_account()],
                    store=PendingStore(path=db_path))

    # NONE of the legacy LASSO daily builders were invoked.
    assert social.calls == []
    assert info.calls == []
    assert lib_fallback.calls == []
    # No legacy feed draft produced, no card posted for it.
    feeds = [d for d in out.get("drafts", [])
             if not getattr(d, "is_story", False)]
    assert feeds == []
    assert poster.cards == []


def test_autopublish_off_keeps_legacy_lasso_daily_card(monkeypatch, tmp_path):
    voice, lib, db_path = _arm(monkeypatch, tmp_path, autopublish=False)
    legacy = _legacy_feed_draft(_lasso_account(), DAY)
    # Social-proof leg returns the legacy draft; the later legs never run once it does.
    social, info, lib_fallback = _spies(monkeypatch, legacy_ret=legacy)
    poster = _FakePoster()

    out = run_daily(poster=poster, voice_path=voice, library_path=lib,
                    scheduled_for=SCHEDULED, accounts=[_lasso_account()],
                    store=PendingStore(path=db_path))

    # The legacy chain ran and produced the daily feed draft + its card.
    assert len(social.calls) == 1
    feeds = [d for d in out.get("drafts", [])
             if not getattr(d, "is_story", False)]
    assert [d.draft_id for d in feeds] == [legacy.draft_id]
    assert legacy in poster.cards


def test_autopublish_on_does_not_touch_client_gym_drafting(monkeypatch, tmp_path):
    # A client (non-LASSO) account still drafts its daily card even with the LASSO
    # calendar autopublisher armed. The guard is LASSO-only.
    voice, lib, db_path = _arm(monkeypatch, tmp_path, autopublish=True)
    client = _client_account(tmp_path)
    client_legacy = _legacy_feed_draft(client, DAY)
    # For a client, the LASSO builders never fire anyway; the library fallback
    # (draft_post) is the path that produces its daily draft.
    social, info, lib_fallback = _spies(monkeypatch, legacy_ret=None)
    monkeypatch.setattr("agent.runner.draft_post", _Spy(ret=client_legacy))

    poster = _FakePoster()
    out = run_daily(poster=poster, voice_path=voice, library_path=lib,
                    scheduled_for=SCHEDULED, accounts=[client],
                    store=PendingStore(path=db_path))

    feeds = [d for d in out.get("drafts", [])
             if not getattr(d, "is_story", False)]
    assert [d.draft_id for d in feeds] == [client_legacy.draft_id]
    assert client_legacy in poster.cards


def test_autopublish_on_leaves_book_and_welcome_queues_running(monkeypatch, tmp_path):
    # The book queue and welcome drip run BEFORE the guard and are NOT gated by it:
    # they are still invoked when autopublish is armed. (Their drafts feed the same
    # approval path as always; the guard only suppresses the legacy rotation/
    # infographic/library legs that come after.)
    voice, lib, db_path = _arm(monkeypatch, tmp_path, autopublish=True)
    _spies(monkeypatch, legacy_ret=None)
    book_spy = _Spy(ret=None)
    welcome_spy = _Spy(ret=None)
    monkeypatch.setattr("agent.book_queue.build_book_queue_draft", book_spy)
    monkeypatch.setattr("agent.welcome_queue.build_welcome_queue_draft", welcome_spy)

    run_daily(poster=_FakePoster(), voice_path=voice, library_path=lib,
              scheduled_for=SCHEDULED, accounts=[_lasso_account()],
              store=PendingStore(path=db_path))

    # Both queue builders were reached (the guard sits AFTER them, not around them).
    assert len(book_spy.calls) == 1
    assert len(welcome_spy.calls) == 1
