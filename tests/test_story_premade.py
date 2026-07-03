"""
Premade story-variant tests. Asserts: flag OFF keeps today's behavior exactly
(the premade file is ignored, the re-render path runs); flag ON prefers the
*_story render with zero generation; the card stays labeled STORY; the feed
cadence is untouched (a story draft is additional, never the feed slot).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, stories  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402


class FakeNano:
    def __init__(self):
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def _acct():
    return Account(key="lasso_ig", display_name="IG", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="I")


def _feed_draft(tmp_path):
    feed = tmp_path / "nano_one_screen.png"
    feed.write_bytes(b"feed image")
    return Draft(draft_id="f1", account_key="lasso_ig", platform="instagram",
                 caption="c", hashtags=[], creative_path=str(feed),
                 creative_public_url="https://cdn/x.png",
                 scheduled_for="t", status=DraftStatus.PENDING,
                 source_fragments=["Headline", "body line"])


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))


def test_flag_off_ignores_premade_and_rerenders(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_STORY_PREMADE_ENABLED", raising=False)
    feed = _feed_draft(tmp_path)
    (tmp_path / "nano_one_screen_story.png").write_bytes(b"premade story")
    nano = FakeNano()
    d = stories.build_story_draft(_acct(), "2026-07-06", feed_draft=feed,
                                  nano_client=nano, s3_client=FakeS3())
    assert d is not None and d.is_story is True
    assert nano.calls == 1                              # today's re-render path
    assert "_story" not in os.path.basename(d.creative_path) or "nano" in d.creative_path


def test_flag_on_prefers_premade_zero_generation(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_STORY_PREMADE_ENABLED", "true")
    feed = _feed_draft(tmp_path)
    premade = tmp_path / "nano_one_screen_story.png"
    premade.write_bytes(b"premade story")
    nano = FakeNano()
    d = stories.build_story_draft(_acct(), "2026-07-06", feed_draft=feed,
                                  nano_client=nano, s3_client=FakeS3())
    assert d.creative_path == str(premade)              # the premade render
    assert nano.calls == 0                              # nothing generated
    assert d.is_story is True                           # STORY label path intact
    assert d.status == DraftStatus.PENDING              # still cards for approval


def test_flag_on_without_premade_falls_back(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_STORY_PREMADE_ENABLED", "true")
    feed = _feed_draft(tmp_path)                        # no *_story sibling
    nano = FakeNano()
    d = stories.build_story_draft(_acct(), "2026-07-06", feed_draft=feed,
                                  nano_client=nano, s3_client=FakeS3())
    assert d is not None and nano.calls == 1            # the normal re-render


def test_cadence_untouched_story_is_additional(monkeypatch, tmp_path):
    """A story draft never replaces or consumes the feed slot: it derives FROM
    the day's PENDING feed draft and both exist side by side."""
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_STORY_PREMADE_ENABLED", "true")
    feed = _feed_draft(tmp_path)
    (tmp_path / "nano_one_screen_story.png").write_bytes(b"premade")
    d = stories.build_story_draft(_acct(), "2026-07-06", feed_draft=feed,
                                  nano_client=FakeNano(), s3_client=FakeS3())
    assert feed.status == DraftStatus.PENDING           # feed draft untouched
    assert d.draft_id != feed.draft_id                  # a second, separate card
