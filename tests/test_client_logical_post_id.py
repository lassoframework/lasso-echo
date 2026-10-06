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


# ---- rollout flag (ECHO_LOGICAL_POST_ID_ENABLED, default OFF) --------------

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _logical_post_id_flag_on(monkeypatch):
    """Existing tests in this file exercise the ON behavior."""
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")


def test_flag_defaults_off_mints_nothing_and_rows_carry_no_key(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    from agent import config
    assert config.logical_post_id_enabled() is False
    drafts = _finish_without_media_lanes(monkeypatch, _feed())
    assert len(drafts) == 2
    feed, story = drafts
    assert (getattr(feed, "logical_post_id", "") or "") == ""
    assert (getattr(story, "logical_post_id", "") or "") == ""
    rows = cmr._to_rows("gritx", drafts)
    assert [(row["account"], row["format"]) for row in rows] == [
        ("instagram", "feed"), ("facebook", "feed"), ("instagram", "story"),
    ]
    assert all("logical_post_id" not in row for row in rows)


def test_flag_off_pre_stamped_draft_row_omits_logical_post_id(monkeypatch):
    # The leak fix: a draft that already carries a valid logical_post_id (e.g. a
    # pre-stamped draft built elsewhere) must NOT forward the new column into
    # content_calendar rows while the rollout flag is OFF.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    drafts = _finish_without_media_lanes(monkeypatch, _feed())
    assert len(drafts) == 2
    for draft in drafts:
        draft.logical_post_id = "11111111-2222-3333-4444-555555555555"
    rows = cmr._to_rows("gritx", drafts)
    assert all("logical_post_id" not in row for row in rows)


def test_flag_on_pre_stamped_draft_row_keeps_logical_post_id(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    drafts = _finish_without_media_lanes(monkeypatch, _feed())
    assert len(drafts) == 2
    for draft in drafts:
        draft.logical_post_id = "11111111-2222-3333-4444-555555555555"
    rows = cmr._to_rows("gritx", drafts)
    assert {row["logical_post_id"] for row in rows} == {
        "11111111-2222-3333-4444-555555555555"}


def test_flag_on_invalid_pre_stamped_feed_raises_fatal_identity_error(monkeypatch):
    # P1 fix (hardened): a malformed pre-stamped identity is FATAL, not a media
    # hold.  Returning [] let callers treat the day as an ordinary held slot and
    # the build could still reach _apply's month-grained delete.  The helper now
    # raises a dedicated InvalidLogicalPostIdentity that aborts the whole rebuild
    # before any delete.
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    feed = _feed()
    feed.logical_post_id = "not-a-uuid"
    import pytest as _pt
    with _pt.raises(cmr.InvalidLogicalPostIdentity):
        _finish_without_media_lanes(monkeypatch, feed)


def test_public_build_with_invalid_pre_stamped_feed_aborts_with_zero_calendar_writes(
        monkeypatch, tmp_path):
    # P1 regression on the PUBLIC month build: one valid pre-stamped feed
    # finishes, then an invalid pre-stamped feed raises.  The whole rebuild
    # aborts BEFORE _apply: zero delete_month calls, zero inserted rows, even
    # though a valid feed was already staged.
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    import json
    import pytest as _pt
    from agent import client_sources as cs
    from agent.accounts import Account, Platform
    from agent.voice import VoiceDoc

    lib = tmp_path / "gritx_lib"
    lib.mkdir()
    for i in range(4):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG" + str(i).encode())
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    cs.add_source("gritx_ig", "offer", "21 day kickstart for busy parents",
                  "client social intake")

    class CountingStore:
        def __init__(self):
            self.deleted = []
            self.inserted = []

        def delete_month(self, base_key, month, preserve_dates=()):
            self.deleted.append((base_key, month))
            return 0

        def insert_rows(self, base_key, rows):
            self.inserted.extend(rows)
            return rows

    # Pre-stamp the FIRST picked feed with a VALID id and the SECOND with a
    # malformed one: the build must abort on the second, never reaching _apply.
    real_clean = cmr._clean_draft_for_day
    picked = []

    def _stamping_clean(*args, **kwargs):
        feed, drop = real_clean(*args, **kwargs)
        if feed is not None:
            picked.append(feed)
            feed.logical_post_id = (
                "11111111-2222-3333-4444-555555555555" if len(picked) == 1
                else "not-a-uuid")
        return feed, drop

    monkeypatch.setattr(cmr, "_clean_draft_for_day", _stamping_clean)
    store = CountingStore()
    account = Account(key="gritx_ig", display_name="GritX",
                      platform=Platform.INSTAGRAM, token_env="T",
                      target_id_env="TID")
    voice = VoiceDoc(raw="We help members win.\n#GetFit",
                     hashtags=["#GetFit"], ctas=["Save this post."])
    with _pt.raises(cmr.InvalidLogicalPostIdentity):
        cmr.build_client_month(account, "gritx", "2026-10-15", days=3,
                               voice=voice, library_path=str(lib), store=store,
                               banned_words=())
    assert len(picked) >= 2, "the valid first feed must have been staged first"
    assert store.deleted == [], "zero calendar deletes"
    assert store.inserted == [], "zero calendar inserts"


def test_drive_prepass_catch_path_reraises_and_rolls_back_the_drive_asset(
        monkeypatch, tmp_path):
    # P1 regression: the Drive pre-pass / gap-fill lanes wrap staging in a broad
    # `except Exception` that used to swallow the identity defect as a skipped
    # lane.  The dedicated exception must roll the in-flight Drive asset back and
    # then propagate, so the month build aborts before any delete.
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    from datetime import date
    import pytest as _pt
    from agent import gym_media_builder

    monkeypatch.setattr(cmr, "_capture_raw_hosted_source", lambda *args: "")
    monkeypatch.setattr(cmr, "_maybe_edit_video", lambda *args: None)
    monkeypatch.setattr(cmr, "_attach_video_poster", lambda *args: True)
    monkeypatch.setattr(cmr, "_maybe_format_feed", lambda *args: None)
    monkeypatch.setattr(cmr, "_maybe_format_story", lambda *args: True)

    draft = _feed()
    draft.logical_post_id = "not-a-uuid"
    draft.source_media_asset_id = "drive-asset-1"
    monkeypatch.setattr(gym_media_builder, "build_gym_media_draft",
                        lambda *args, **kwargs: draft)
    monkeypatch.setattr(cmr, "_gym_drive_source_for", lambda *args, **kwargs: object())
    rolled_back = []
    monkeypatch.setattr(cmr, "_rollback_drive_asset",
                        lambda d, day_key, log: rolled_back.append(d))

    with _pt.raises(cmr.InvalidLogicalPostIdentity):
        cmr.append_gym_drive_drafts(
            object(), "gritx", date(2026, 10, 15), 1, object(),
            log=lambda _message: None, covered_days=set(),
            library_path="/library")
    assert rolled_back == [draft], "the in-flight Drive asset must be rolled back"


def test_flag_on_invalid_pre_stamped_draft_row_fails_closed(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    feed = _feed()
    feed.logical_post_id = "not-a-uuid"
    import pytest as _pt
    with _pt.raises(ValueError):
        cmr._row_from_draft("gritx", feed)


def test_flag_on_invalid_pre_id_aborts_apply_with_zero_deletes_and_inserts(monkeypatch):
    # P1 fix: validation runs BEFORE the first delete_month — zero deletes,
    # zero inserts, existing calendar untouched.
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")

    class Store:
        def __init__(self):
            self.deleted = 0
            self.inserted = 0

        def list_month(self, gym_id, month):
            return []

        def delete_month(self, gym_id, month, **kwargs):
            self.deleted += 1
            return 0

        def insert_rows(self, gym_id, rows):
            self.inserted += len(rows)
            return rows

    rows = [{
        "gym_id": "gritx", "post_date": "2026-10-15", "account": "instagram",
        "format": "feed", "status": "pending", "caption": "real caption",
        "image_url": "https://media.example/x.jpg",
        "logical_post_id": "not-a-uuid",
    }]
    store = Store()
    from datetime import date
    result = cmr._apply("gritx", rows, date(2026, 10, 15), 1, store,
                        lambda _message: None)
    assert result["ok"] is False
    assert result["deleted"] == 0 and result["inserted"] == 0
    assert store.deleted == 0 and store.inserted == 0


def test_flag_on_valid_pre_stamped_ids_pass_the_apply_guard(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    rows = [{"logical_post_id": "11111111-2222-3333-4444-555555555555"},
            {"logical_post_id": ""}, {}]
    assert cmr._logical_post_ids_valid(rows, lambda _message: None)
    bad = [{"logical_post_id": "11111111-2222-3333-4444-555555555555"},
           {"logical_post_id": "garbage"}]
    assert not cmr._logical_post_ids_valid(bad, lambda _message: None)


# ---- prewrite identity abort restores released Drive assets (P1 regression) --

from types import SimpleNamespace  # noqa: E402


class _RecordingStore:
    """Calendar store double that records every mutation attempt."""

    def __init__(self):
        self.deleted = []
        self.inserted = []

    def list_month(self, _base, _month):
        return []

    def delete_month(self, _base, month):
        self.deleted.append(month)
        return 0

    def insert_rows(self, _base, rows):
        self.inserted.extend(rows)
        return rows


def test_prewrite_identity_abort_restores_released_drive_assets(monkeypatch):
    """InvalidLogicalPostIdentity escaping planning (no result, no calendar write)
    must restore the OLD rows' released Drive assets, not strand them."""
    from agent import build_lock
    from agent.accounts import Account, Platform

    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setattr(cmr, "_client_media_count", lambda _path: 3)
    monkeypatch.setattr(build_lock, "acquire", lambda *a, **k: True)
    monkeypatch.setattr(build_lock, "start_heartbeat",
                        lambda *a, **k: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(build_lock, "release", lambda *a, **k: None)
    monkeypatch.setattr(cmr, "_locked_calendar_state",
                        lambda *a, **k: (set(), set()))

    released = [("asset-1", "2026-10-15")]
    observations = {"restored": []}

    def _release(base_key, start, days, store, log, locked_days=()):
        return list(released)

    monkeypatch.setattr(cmr, "_release_wipeable_drive_assets", _release)
    monkeypatch.setattr(
        cmr, "_restore_released_drive_assets",
        lambda _base, rel, _log: observations["restored"].append(list(rel)))

    def _abort(*args, **kwargs):
        raise cmr.InvalidLogicalPostIdentity(
            "invalid pre-stamped logical_post_id on feed feed for 2026-10-15")

    monkeypatch.setattr(cmr, "_build_client_month_body", _abort)

    store = _RecordingStore()
    account = Account(key="gritx_ig", display_name="Grit X",
                      platform=Platform.INSTAGRAM,
                      token_env="GRITX_TOKEN", target_id_env="GRITX_IG_ID")

    with pytest.raises(cmr.InvalidLogicalPostIdentity):
        cmr.build_client_month(account, "gritx", "2026-10-15", days=2,
                               voice=object(), library_path="/library",
                               store=store, logger=lambda _m: None)

    # The released OLD assets were stamped back exactly once...
    assert observations["restored"] == [released]
    # ...and the abort happened BEFORE any calendar mutation.
    assert store.deleted == []
    assert store.inserted == []


def test_verified_calendar_rollback_restores_released_drive_assets(monkeypatch):
    """A failed apply whose deleted rows were restored keeps the old Drive claims."""
    from agent import build_lock
    from agent.accounts import Account, Platform

    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setattr(cmr, "_client_media_count", lambda _path: 3)
    monkeypatch.setattr(build_lock, "acquire", lambda *a, **k: True)
    monkeypatch.setattr(build_lock, "start_heartbeat",
                        lambda *a, **k: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(build_lock, "release", lambda *a, **k: None)
    monkeypatch.setattr(cmr, "_locked_calendar_state",
                        lambda *a, **k: (set(), set()))
    released = [("asset-1", "2026-10-15")]
    restored = []
    monkeypatch.setattr(
        cmr, "_release_wipeable_drive_assets",
        lambda *a, **k: list(released))
    monkeypatch.setattr(
        cmr, "_restore_released_drive_assets",
        lambda _base, rows, _log: restored.append(list(rows)))
    monkeypatch.setattr(
        cmr, "_build_client_month_body",
        lambda *a, **k: {
            "ok": False, "inserted": 0, "deleted": 2, "deleted_total": 2,
            "effective_deleted": 0, "rollback_restored": True,
            "insert_outcome_unknown": False,
        })

    account = Account(key="gritx_ig", display_name="Grit X",
                      platform=Platform.INSTAGRAM,
                      token_env="GRITX_TOKEN", target_id_env="GRITX_IG_ID")
    result = cmr.build_client_month(
        account, "gritx", "2026-10-15", days=2,
        voice=object(), library_path="/library", store=_RecordingStore(),
        logger=lambda _m: None)

    assert result["rollback_restored"] is True
    assert restored == [released]
