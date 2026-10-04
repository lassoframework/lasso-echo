"""Forward-only logical-post identities for the client month writer."""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr  # noqa: E402
from agent.drafter import Draft  # noqa: E402


def _feed(draft_id="feed"):
    return Draft(
        draft_id=draft_id,
        account_key="gritx_ig",
        platform="instagram",
        caption="A real client caption.",
        hashtags=[],
        creative_path="/library/client-photo.jpg",
        creative_public_url="https://media.example/client-photo.jpg",
        scheduled_for="2026-10-15T08:00:00+00:00",
        day_key="2026-10-15",
    )


def _finish_without_media_lanes(monkeypatch, feed):
    monkeypatch.setattr(cmr, "_capture_raw_hosted_source", lambda *args: "")
    monkeypatch.setattr(cmr, "_maybe_edit_video", lambda *args: None)
    monkeypatch.setattr(cmr, "_attach_video_poster", lambda *args: True)
    monkeypatch.setattr(cmr, "_maybe_format_feed", lambda *args: None)
    monkeypatch.setattr(cmr, "_maybe_format_story", lambda *args: True)
    return cmr._finish_feed_with_story(
        object(), feed, "/library", lambda _message: None, day_key=feed.day_key)


def test_finish_pair_mints_one_uuid_and_rows_keep_it_on_ig_fb_and_story(monkeypatch):
    drafts = _finish_without_media_lanes(monkeypatch, _feed())

    assert len(drafts) == 2
    feed, story = drafts
    logical_post_id = feed.logical_post_id
    assert str(uuid.UUID(logical_post_id)) == logical_post_id
    assert story.logical_post_id == logical_post_id

    rows = cmr._to_rows("gritx", drafts)
    assert [(row["account"], row["format"]) for row in rows] == [
        ("instagram", "feed"), ("facebook", "feed"), ("instagram", "story"),
    ]
    assert {row["logical_post_id"] for row in rows} == {logical_post_id}


def test_same_day_and_photo_still_mint_distinct_logical_post_ids(monkeypatch):
    first = _finish_without_media_lanes(monkeypatch, _feed("first"))
    second = _finish_without_media_lanes(monkeypatch, _feed("second"))

    assert first[0].creative_public_url == second[0].creative_public_url
    assert first[0].day_key == second[0].day_key
    assert first[0].logical_post_id != second[0].logical_post_id
    assert {row["logical_post_id"] for row in cmr._to_rows("gritx", first)} == {
        first[0].logical_post_id
    }
    assert {row["logical_post_id"] for row in cmr._to_rows("gritx", second)} == {
        second[0].logical_post_id
    }


def test_retrying_the_same_feed_preserves_its_logical_post_id(monkeypatch):
    feed = _feed()
    first = _finish_without_media_lanes(monkeypatch, feed)
    second = _finish_without_media_lanes(monkeypatch, feed)

    assert first[0].logical_post_id == second[0].logical_post_id
    assert second[1].logical_post_id == first[0].logical_post_id


def test_row_writer_does_not_backfill_an_id_for_an_existing_draft():
    row = cmr._row_from_draft("gritx", _feed())

    assert "logical_post_id" not in row
