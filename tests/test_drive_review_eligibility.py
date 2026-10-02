import pytest

from agent import gym_media_review, gym_media_selector
from agent.gym_media_routes import handle_list_assets
from tests.gym_media_fakes import FakeMediaStore, make_asset


def asset(**fields):
    row = make_asset(fid="drive1", gym_id="gym1")
    row.update(review_status="pending_review", moderation_status="pending",
               moderation_json=None, people_detected=None,
               consent_status="pending", reviewed_by=None, reviewed_at=None)
    row.update(fields)
    return row


def approved(**fields):
    row = asset(review_status="approved", reviewed_by="operator",
                reviewed_at="2026-09-18T00:00:00Z", moderation_status="clean",
                people_detected=False, consent_status="not_required")
    row["review_content_hash"] = row["content_hash"]
    row["moderation_json"] = {"provider": "test", "verdict": "clean",
                              "content_hash": row["content_hash"],
                              "asset_id": row["id"], "gym_id": row["gym_id"],
                              "people_detected": False,
                              "observed_at": "2026-09-18T00:00:00Z"}
    row.update(fields)
    return row


def test_existing_unreviewed_asset_stays_visible_but_not_pickable(monkeypatch):
    row = asset()
    store = FakeMediaStore(assets=[row])
    monkeypatch.setattr("agent.gym_media_routes._armed", lambda _: True)
    status, body = handle_list_assets("gym1", store=store)
    assert status == 200 and body["assets"][0]["review_status"] == "pending_review"
    assert body["assets"][0]["review_ready"] is False
    assert gym_media_selector.pickable("gym1", store=store) == []
    assert store.get_asset("drive1") is not None


def test_reviewed_clean_no_people_asset_is_selectable(monkeypatch):
    store = FakeMediaStore(assets=[approved()])
    monkeypatch.setattr("agent.gym_media_routes._armed", lambda _: True)
    status, body = handle_list_assets("gym1", store=store)
    assert status == 200 and body["assets"][0]["review_ready"] is True
    assert gym_media_selector.pickable("gym1", store=store)[0]["id"] == "drive1"


@pytest.mark.parametrize("legacy_consent", [
    {}, {"consent_status": "pending"}, {"consent_status": "denied"},
    {"consent_status": "granted", "release_ref": "old-release",
     "consent_member_ref": "old-member", "consent_expires_at": "2020-01-01T00:00:00Z"},
])
def test_clean_reviewed_people_asset_never_requires_release(legacy_consent):
    row = approved()
    row.update(people_detected=True, **legacy_consent)
    row["moderation_json"]["people_detected"] = True
    assert gym_media_selector.is_usable(row)


@pytest.mark.parametrize("status", ["pending", "flagged", "rejected", None])
def test_moderation_must_be_clean(status):
    assert not gym_media_selector.is_usable(approved(moderation_status=status))


def test_operator_approval_requires_evidence_and_records_actor():
    row = approved()
    row.update(review_status="pending_review", reviewed_by=None, reviewed_at=None,
               review_content_hash=None)
    store = FakeMediaStore(assets=[row])
    result = gym_media_review.review_asset("gym1", "drive1", "approve",
                                          store=store, operator="local-user")
    assert result["review_status"] == "approved"
    assert store.get_asset("drive1")["reviewed_by"] == "local-user"
    assert store.get_asset("drive1")["consent_status"] == "not_required"
    with pytest.raises(ValueError):
        gym_media_review.review_asset("other-gym", "drive1", "reject",
                                      store=store, operator="local-user")


def test_release_request_is_not_a_supported_review_action():
    store = FakeMediaStore(assets=[asset()])
    with pytest.raises(ValueError, match="unknown action"):
        gym_media_review.review_asset("gym1", "drive1", "request_release",
                                      store=store, operator="local-user")
