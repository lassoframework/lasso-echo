"""Fresh Drive asset review at the calendar's final outbound boundary."""

import pytest

from agent import calendar_autopublish as cap
from agent import media_source_store
from agent.meta_publisher import PublishResult
from tests.test_calendar_autopublish import (
    RUN_DATE, LATE_NOW, _FakePublisher, _FakeStore, _row,
)


class ReviewCalendarStore(_FakeStore):
    def __init__(self, rows):
        super().__init__(rows)
        self.rollbacks = []

    def mark_publish_failed(self, row_id, revert_status="pending", reject_reason=""):
        self.rollbacks.append((row_id, revert_status, reject_reason))
        self.rows[row_id].update(status=revert_status, reject_reason=reject_reason)


def asset(gym_id="lasso", **overrides):
    data = dict(id="asset-1", gym_id=gym_id, eligible=True,
                review_status="approved", reviewed_by="operator",
                reviewed_at="2026-08-26T00:00:00Z",
                moderation_status="clean", moderation_json={"provider": "test", "verdict": "clean"},
                people_detected=False, consent_status="not_required")
    data.update(overrides)
    return data


@pytest.fixture
def armed(monkeypatch):
    from agent import publish_billing_gate
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_VIA_ZERNIO", "false")
    monkeypatch.setattr(publish_billing_gate, "publishing_blocked", lambda _gym: False)


@pytest.mark.parametrize("review_asset", [
    asset(review_status="pending_review"), None, asset(gym_id="other"),
    asset(reviewed_by=None), RuntimeError("read failed"),
])
def test_pending_drive_asset_holds_before_meta_send(armed, monkeypatch, review_asset):
    row = _row("drive")
    row["source_media_asset_id"] = "asset-1"
    calendar = ReviewCalendarStore([row])
    publisher = _FakePublisher()
    reads = []

    class MediaStore:
        def get_asset(self, asset_id):
            reads.append(asset_id)
            if isinstance(review_asset, Exception):
                raise review_asset
            return review_asset

    monkeypatch.setattr(media_source_store, "default_store", MediaStore)
    result = cap.publish_due(RUN_DATE, store=calendar, publisher=publisher,
                             now=LATE_NOW, catch_all=True)
    assert result["failed"] == ["drive"]
    assert reads == ["asset-1"]
    assert publisher.calls == []
    assert calendar.rows["drive"]["status"] == "pending"
    assert calendar.rollbacks == [("drive", "pending", "media_asset_review_required")]


def test_revoked_approved_drive_asset_holds_before_zernio_send(armed, monkeypatch):
    class Account:
        key = "gymx_ig"
        platform = "instagram"
        display_name = "Gym X"

    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: Account())
    row = _row("client", status="approved")
    row.update(gym_id="gymx", source_media_asset_id="asset-1")
    calendar = ReviewCalendarStore([row])
    monkeypatch.setattr(media_source_store, "default_store",
                        lambda: type("MediaStore", (), {"get_asset": lambda self, _id:
                                    asset(gym_id="gymx", review_status="revoked")})())
    sends = []
    result = cap.publish_due(RUN_DATE, gym_id="gymx", store=calendar,
                             publisher=_FakePublisher(),
                             zernio_publish=lambda *a, **kw: sends.append((a, kw)),
                             approved_only=True, now=LATE_NOW, catch_all=True)
    assert result["failed"] == ["client"]
    assert sends == []
    assert calendar.rollbacks == [("client", "approved", "media_asset_review_required")]
    assert calendar.rows["client"]["status"] == "approved"


def test_affirmative_drive_asset_reaches_publisher_once(armed, monkeypatch):
    row = _row("drive")
    row["source_media_asset_id"] = "asset-1"
    calendar = ReviewCalendarStore([row])
    reads = []

    class MediaStore:
        def get_asset(self, asset_id):
            reads.append(asset_id)
            return asset()

    monkeypatch.setattr(media_source_store, "default_store", MediaStore)
    publisher = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    result = cap.publish_due(RUN_DATE, store=calendar, publisher=publisher,
                             now=LATE_NOW, catch_all=True)
    assert result["published"] == ["drive"]
    assert reads == ["asset-1"]
    assert len(publisher.calls) == 1


def test_non_drive_row_never_reads_media_store(armed, monkeypatch):
    def forbidden():
        raise AssertionError("non-Drive row touched media store")

    monkeypatch.setattr(media_source_store, "default_store", forbidden)
    publisher = _FakePublisher()
    result = cap.publish_due(RUN_DATE, store=ReviewCalendarStore([_row("ordinary")]),
                             publisher=publisher, now=LATE_NOW, catch_all=True)
    assert result["published"] == ["ordinary"]
    assert len(publisher.calls) == 1


@pytest.mark.parametrize("evidence", [
    {"provider": "test", "verdict": "rejected"},
    {"provider": "test", "verdict": "unsafe"},
    {"verdict": "clean"},
    {"provider": "", "verdict": "clean"},
    "clean", ["clean"],
])
def test_invalid_moderation_holds_before_send(armed, monkeypatch, evidence):
    row = _row("bad-moderation")
    row["source_media_asset_id"] = "asset-1"
    calendar = ReviewCalendarStore([row])
    monkeypatch.setattr(media_source_store, "default_store",
                        lambda: type("MediaStore", (), {"get_asset": lambda self, _id:
                                    asset(moderation_json=evidence)})())
    publisher = _FakePublisher()
    result = cap.publish_due(RUN_DATE, store=calendar, publisher=publisher,
                             now=LATE_NOW, catch_all=True)
    assert result["failed"] == ["bad-moderation"]
    assert publisher.calls == []


@pytest.mark.parametrize("marker", [
    {"draft_type": "gym_media"},
    {"source_fragments": ["drive_media:legacy-1"]},
    {"image_url": "https://cdn.example/drive/photo.jpg"},
    {"source_media_url": "https://drive.google.com/file/d/legacy-1"},
])
def test_legacy_drive_marker_without_asset_id_holds(armed, monkeypatch, marker):
    row = _row("legacy")
    row.update(marker)
    calendar = ReviewCalendarStore([row])
    monkeypatch.setattr(media_source_store, "default_store",
                        lambda: pytest.fail("missing asset ID must hold before store read"))
    publisher = _FakePublisher()
    result = cap.publish_due(RUN_DATE, store=calendar, publisher=publisher,
                             now=LATE_NOW, catch_all=True)
    assert result["failed"] == ["legacy"]
    assert publisher.calls == []
