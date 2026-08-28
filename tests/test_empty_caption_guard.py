"""
tests/test_empty_caption_guard.py — the empty-caption double belt
(report-card build, 2026-08-28). LASSO's audited feed shipped ONE post with an
EMPTY caption; both belts make that impossible:

  * DRAFT/STAGE belt (AGENT_EMPTY_CAPTION_GUARD, default OFF):
    portal_calendar_store.insert_rows drops an empty-caption FEED row with an
    honest alert; STORY rows are exempt (empty body by design).
  * PUBLISH belt (always on): publish_guard.check flags empty_caption,
    meta_publisher and zernio_publisher both raise before the network call,
    and the chat gate blocks with an honest reason.
All offline.
"""
from __future__ import annotations

import pytest

from agent import publish_guard as pg


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 201
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    def post(self, url, headers=None, json=None, params=None, timeout=None):
        return _FakeResp(list(json or []))


def _store(http=None):
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="https://sb.test", service_key="k",
                                 http=http or _FakeHTTP())


def _row(caption, fmt="feed", post_date="2026-09-10"):
    return {"post_date": post_date, "account": "instagram", "format": fmt,
            "caption": caption, "image_url": "https://cdn/x.jpg",
            "status": "pending"}


# ---------------------------------------------------------------------------
# Belt 1: draft/stage time (insert_rows)
# ---------------------------------------------------------------------------

def test_stage_belt_drops_empty_and_whitespace_feed_rows(monkeypatch):
    monkeypatch.setenv("AGENT_EMPTY_CAPTION_GUARD", "true")
    inserted = _store().insert_rows("lasso", [
        _row(""),                       # empty -> dropped
        _row("   \n\t "),               # whitespace -> dropped
        _row("... 🎉"),                  # zero VISIBLE characters -> dropped
        _row("A real caption with real words."),
    ])
    assert [r["caption"] for r in inserted] == ["A real caption with real words."]


def test_stage_belt_exempts_stories_and_defaults_off(monkeypatch):
    monkeypatch.setenv("AGENT_EMPTY_CAPTION_GUARD", "true")
    inserted = _store().insert_rows("lasso", [
        _row("", fmt="story"),           # story: empty body BY DESIGN -> kept
        _row("A real caption."),
    ])
    assert len(inserted) == 2
    # default OFF: byte-for-byte today (the empty feed row still stages)
    monkeypatch.delenv("AGENT_EMPTY_CAPTION_GUARD", raising=False)
    monkeypatch.delenv("AGENT_CAPTION_COOLDOWN", raising=False)
    inserted = _store().insert_rows("lasso", [_row(""), _row("A real caption.")])
    assert len(inserted) == 2


# ---------------------------------------------------------------------------
# Belt 2: publish time
# ---------------------------------------------------------------------------

def test_publish_guard_flags_empty_caption_feed_only():
    empty = pg.PublishPayload(row_id="r", gym_id="g", platform="instagram",
                              caption="", media_ready=True)
    assert pg.EMPTY_CAPTION in pg.check(empty)
    story = pg.PublishPayload(row_id="r", gym_id="g", platform="instagram",
                              caption="", media_ready=True, is_story=True)
    assert pg.EMPTY_CAPTION not in pg.check(story)


def test_meta_publisher_refuses_empty_feed_caption(monkeypatch):
    # publishing armed so the gate would otherwise reach the dispatch
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    from agent import meta_publisher

    class _Acct:
        key = "lasso_ig"
        platform = "instagram"

        def get_token(self):
            return "tok"

    class _Draft:
        caption = "   "
        hashtags = []
        is_story = False
        creative_public_url = "https://cdn/x.jpg"

    with pytest.raises(ValueError, match="empty"):
        meta_publisher._publish_gated(_Draft(), _Acct())


def test_chat_gate_blocks_empty_feed_caption():
    from agent import chat_publish

    class _Draft:
        caption = "..."
        account_key = "lasso_ig"
        day_key = "2026-09-01"
        is_story = False

    reason = chat_publish.caption_belts(_Draft())
    assert reason and "empty caption" in reason

    class _Story(_Draft):
        is_story = True

    assert chat_publish.caption_belts(_Story()) is None
