"""
ISSUE 5 (Dale, CrossFit ENG, round 2, 2026-08-17): the Monday Aug 17 story showed no
caption even though Dale added a story caption and saved.

Root cause: a story publishes empty-body, so its caption lives only on the burned MEDIA.
Editing a story caption updates content_calendar.caption but the already-hosted image_url
still carries the OLD/absent caption, and the publisher shipped image_url verbatim.

Fixes pinned here:
  (a) story_image.story_media_carries_caption detects a stale story (the burned media's
      filename embeds the caption key), schema-free and cross-service.
  (b) the publisher HOLDS a story whose media does not carry the current caption (never
      ships stale/blank) — calendar_autopublish._story_media_is_stale.
  (c) the calendar rebuild RE-RENDERS a story with the CLIENT'S edited caption instead of
      overwriting it with the freshly generated feed caption — client_month_run.

Fully offline.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_image                # noqa: E402
from agent import calendar_autopublish as cap  # noqa: E402
from agent import client_month_run as cmr     # noqa: E402


# ---- (a) provenance: does the burned media carry THIS caption? ------------------

def test_story_media_carries_caption_matches_and_mismatches():
    cap_text = "Your kid's confidence is built here."
    key = story_image._caption_key(cap_text)
    good = f"https://cdn/echo/eng/abc123/deadbeef_{key}__story.jpg"
    # matching cap_key -> media carries this caption
    assert story_image.story_media_carries_caption(good, cap_text) is True
    # a DIFFERENT (edited) caption -> the media is stale
    assert story_image.story_media_carries_caption(good, "A totally new caption") is False


def test_story_media_carries_caption_video_marker():
    cap_text = "New story words."
    key = story_image._caption_key(cap_text)
    vid = f"https://cdn/echo/eng/abc/xyz_{key}__storyvid.mp4"
    assert story_image.story_media_carries_caption(vid, cap_text) is True
    assert story_image.story_media_carries_caption(vid, "different") is False


def test_non_burned_media_is_never_judged():
    # a raw url that is not our burned story asset is never flagged (no false hold)
    assert story_image.story_media_carries_caption(
        "https://cdn/echo/eng/abc/raw_photo.jpg", "anything") is True
    assert story_image.story_media_carries_caption("", "anything") is True


# ---- (b) publisher HOLDS a stale story, never ships it blank --------------------

def test_publisher_flags_stale_story_row():
    key = story_image._caption_key("OLD caption that was burned")
    row = {"id": "s1", "gym_id": "eng", "format": "story",
           "image_url": f"https://cdn/echo/eng/a/b_{key}__story.jpg",
           "caption": "NEW edited caption the client just saved"}
    assert cap._story_media_is_stale(row) is True


def test_publisher_allows_matching_story_row():
    text = "The caption that was actually burned in"
    key = story_image._caption_key(text)
    row = {"id": "s1", "gym_id": "eng", "format": "story",
           "image_url": f"https://cdn/echo/eng/a/b_{key}__story.jpg",
           "caption": text}
    assert cap._story_media_is_stale(row) is False


def test_publisher_never_flags_a_feed_row():
    key = story_image._caption_key("x")
    row = {"id": "f1", "gym_id": "eng", "format": "feed",
           "image_url": f"https://cdn/echo/eng/a/b_{key}__story.jpg",
           "caption": "different"}
    # a feed is never subject to the story-media guard
    assert cap._story_media_is_stale(row) is False


def test_publish_due_holds_stale_story(monkeypatch):
    """End to end at the publisher: a stale story is left WAITING, never published."""
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")

    key = story_image._caption_key("OLD")
    stale = {"id": "s1", "gym_id": "eng", "format": "story", "status": "approved",
             "post_date": "2026-08-17", "account": "instagram",
             "image_url": f"https://cdn/echo/eng/a/b_{key}__story.jpg",
             "caption": "NEW edited caption", "published_at": None,
             "scheduled_at": "2026-08-17T00:00:00+00:00"}

    class _Store:
        def due_rows(self, gym_id, run_date, catchup_days=0):
            return [dict(stale)]

        def stamp_scheduled(self, *a, **k):
            return None

        def mark_publishing(self, row_id):
            raise AssertionError("a stale story must NOT be claimed for publish")

    published = []

    def _pub(draft, account, **k):
        published.append(draft)
        class _R:
            ok = True
            mode = "published"
            media_id = "x"
        return _R()

    # a mapper so the row resolves to an account; publisher should never be reached
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    res = cap.publish_due("2026-08-17", gym_id="eng", store=_Store(),
                          publisher=_pub, zernio_publish=_pub, catch_all=True,
                          approved_only=True)
    assert published == []                       # nothing was published
    assert "s1" in res.get("waiting", [])        # the stale story is held


class _Acct:
    key = "eng"
    platform = "instagram"


# ---- (c) rebuild RE-RENDERS the story with the CLIENT's edited caption ----------

def test_edited_story_captions_reads_only_edited_slots():
    """A story caption that differs from its paired feed is an edit; an identical one
    (unedited paired story) is not."""
    class _Store:
        def list_month(self, base_key, month):
            return [
                {"format": "feed", "post_date": "2026-08-17", "caption": "feed cap A"},
                {"format": "story", "post_date": "2026-08-17",
                 "caption": "CLIENT edited story caption"},   # edited (differs)
                {"format": "feed", "post_date": "2026-08-18", "caption": "feed cap B"},
                {"format": "story", "post_date": "2026-08-18", "caption": "feed cap B"},
            ]

    from datetime import date
    edited = cmr._edited_story_captions(
        "eng", date(2026, 8, 17), 2, _Store(), lambda m: None)
    assert edited == {"2026-08-17": "CLIENT edited story caption"}


def test_maybe_format_story_uses_story_caption_override(monkeypatch):
    monkeypatch.setattr(cmr.config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(cmr.config, "hosting_enabled", lambda: True)
    seen = {}

    class _Feed:
        creative_path = "photo.jpg"
        caption = "the FEED caption (should NOT be burned)"

    class _Story:
        creative_path = "photo.jpg"
        caption = "the CLIENT edited story caption"
        creative_public_url = ""
        thumbnail_url = ""

    import agent.story_image as si
    import agent.media_host as mh
    monkeypatch.setattr(si, "get_or_make_story_image",
                        lambda path, caption, gym, lib, **k: seen.setdefault("cap", caption) or "/tmp/out.jpg")
    monkeypatch.setattr(mh, "host_media", lambda *a, **k: "https://cdn/hosted_story.jpg")

    class _Acct2:
        key = "eng_ig"
        display_name = "CrossFit ENG"

    kept = cmr._maybe_format_story(_Acct2(), _Story(), _Feed(), "lib", lambda m: None)
    assert kept is True
    # the CLIENT's edited caption is what got burned, not the feed caption
    assert seen["cap"] == "the CLIENT edited story caption"
