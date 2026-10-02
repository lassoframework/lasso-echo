import pytest

from agent import gym_media_review, gym_media_selector
from agent.jobs import sync_gym_media
from tests.gym_media_fakes import FakeDrive, FakeMediaStore, make_asset, make_source, photo


def _ready(asset_id="drive1", gym_id="gym1", content_hash="bytes-v1"):
    row = make_asset(asset_id, gym_id=gym_id, content_hash=content_hash)
    row["review_content_hash"] = content_hash
    row["moderation_json"] = {
        "provider": "offline-test", "verdict": "clean", "asset_id": asset_id,
        "gym_id": gym_id, "content_hash": content_hash,
        "people_detected": False, "observed_at": "2026-09-18T00:00:00Z"}
    return row


def test_copied_evidence_and_changed_bytes_fail_closed():
    original = _ready()
    assert gym_media_selector.is_usable(original)
    for change in ({"content_hash": "bytes-v2"}, {"review_content_hash": None},
                   {"id": "other-id"}, {"gym_id": "other-gym"},
                   {"people_detected": True}, {"moderation_json": {
                       **original["moderation_json"], "content_hash": "bytes-v2"}}):
        assert not gym_media_selector.is_usable(dict(original, **change))


def test_sync_byte_swap_revokes_all_old_evidence(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)
    old = _ready("p1", "pierce", "bytes-v1")
    old.update(rendition_url="https://example.invalid/old", vision_json={"old": True})
    store = FakeMediaStore(assets=[old])
    result = sync_gym_media.sync_source(
        make_source("src1", gym_id="pierce", folder_id="fold1"),
        drive=FakeDrive(files=[photo("p1", md5="bytes-v2")]), store=store,
        sweep_missing=False, emit_digest=False)
    assert result["ok"]
    changed = store.get_asset("p1")
    assert changed["content_hash"] == "bytes-v2"
    assert changed["review_status"] == "pending_review"
    assert changed["review_content_hash"] is None
    assert changed["moderation_json"] is None
    assert changed["people_detected"] is None
    assert changed["rendition_url"] is None
    assert changed["vision_json"] is None
    assert not gym_media_selector.is_usable(changed)


def test_concurrent_hash_change_cannot_commit_review_or_history():
    row = _ready()
    row.update(review_status="pending_review", reviewed_by=None, reviewed_at=None,
               review_content_hash=None)

    class RacingStore(FakeMediaStore):
        def update_review_asset(self, gym_id, asset_id, fields, **expected):
            self.assets[asset_id]["content_hash"] = "bytes-v2"
            return super().update_review_asset(gym_id, asset_id, fields, **expected)

    store = RacingStore(assets=[row])
    with pytest.raises(RuntimeError, match="changed during review"):
        gym_media_review.review_asset("gym1", "drive1", "approve",
                                      store=store, operator="operator")
    assert store.review_events == []
    assert store.get_asset("drive1")["review_status"] == "pending_review"


def test_review_history_appends_without_rewriting_prior_decision():
    row = _ready()
    row.update(review_status="pending_review", reviewed_by=None, reviewed_at=None,
               review_content_hash=None)
    store = FakeMediaStore(assets=[row])
    gym_media_review.review_asset("gym1", "drive1", "approve",
                                  store=store, operator="first")
    prior = dict(store.review_events[0])
    gym_media_review.review_asset("gym1", "drive1", "reject",
                                  store=store, operator="second")
    assert len(store.review_events) == 2
    assert store.review_events[0] == prior
    assert store.review_events[1]["prior_status"] == "approved"
    assert store.review_events[1]["decision"] == "rejected"
    assert store.get_asset("drive1")["review_status"] == "rejected"
