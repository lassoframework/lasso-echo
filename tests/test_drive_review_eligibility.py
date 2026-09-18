from datetime import datetime, timedelta, timezone

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
                moderation_json={"provider": "test", "verdict": "clean"},
                people_detected=False, consent_status="not_required")
    row.update(fields)
    return row


def test_existing_unreviewed_asset_stays_visible_but_not_pickable(monkeypatch):
    row = asset()
    store = FakeMediaStore(assets=[row])
    monkeypatch.setattr("agent.gym_media_routes._armed", lambda _: True)
    status, body = handle_list_assets("gym1", store=store)
    assert status == 200 and body["assets"][0]["review_status"] == "pending_review"
    assert gym_media_selector.pickable("gym1", store=store) == []
    assert store.get_asset("drive1") is not None


def test_reviewed_clean_no_people_asset_is_selectable():
    store = FakeMediaStore(assets=[approved()])
    assert gym_media_selector.pickable("gym1", store=store)[0]["id"] == "drive1"


@pytest.mark.parametrize("change", [
    {"consent_status": "pending"}, {"consent_status": "denied"},
    {"release_ref": None}, {"consent_member_ref": None},
    {"consent_expires_at": None},
    {"consent_expires_at": "2020-01-01T00:00:00Z"},
])
def test_people_asset_requires_live_release(change):
    row = approved()
    row.update(people_detected=True, consent_status="granted",
               consent_member_ref="member-1", release_ref="release-1",
               consent_expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
    assert gym_media_selector.is_usable(row)
    row.update(change)
    assert not gym_media_selector.is_usable(row)


@pytest.mark.parametrize("status", ["pending", "flagged", "rejected", None])
def test_moderation_must_be_clean(status):
    assert not gym_media_selector.is_usable(approved(moderation_status=status))


def test_operator_approval_requires_evidence_and_records_actor():
    store = FakeMediaStore(assets=[asset(moderation_status="clean",
                                        moderation_json={"provider": "test", "verdict": "clean"},
                                        people_detected=False,
                                        consent_status="not_required")])
    store.update_review_asset = lambda gym, aid, fields: store.update_asset(aid, fields)
    result = gym_media_review.review_asset("gym1", "drive1", "approve",
                                          store=store, operator="local-user")
    assert result["review_status"] == "approved"
    assert store.get_asset("drive1")["reviewed_by"] == "local-user"
    with pytest.raises(ValueError):
        gym_media_review.review_asset("other-gym", "drive1", "reject",
                                      store=store, operator="local-user")
