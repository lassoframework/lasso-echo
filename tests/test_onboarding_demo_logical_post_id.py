from datetime import date
import uuid

import pytest

from agent import onboarding_demo as demo


@pytest.fixture(autouse=True)
def _demo_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_DEMO", "true")


def test_flag_off_preserves_old_shape_without_logical_post_id(monkeypatch):
    monkeypatch.setattr(demo.config, "logical_post_id_enabled", lambda: False,
                        raising=False)
    rows = demo.build_rows("gym", days=1, start=date(2026, 10, 4),
                           image_for_day=lambda _index: "https://img/shared.jpg")

    assert [row["account"] for row in rows] == ["instagram", "facebook", "instagram"]
    assert [row["format"] for row in rows] == ["feed", "feed", "story"]
    assert all("logical_post_id" not in row for row in rows)


def test_feed_and_explicit_facebook_mirror_share_id_but_story_is_singleton(monkeypatch):
    monkeypatch.setattr(demo.config, "logical_post_id_enabled", lambda: True,
                        raising=False)
    rows = demo.build_rows("gym", days=2, start=date(2026, 10, 4),
                           image_for_day=lambda _index: "https://img/shared.jpg")

    groups = {}
    for row in rows:
        logical_post_id = row["logical_post_id"]
        assert str(uuid.UUID(logical_post_id)) == logical_post_id
        groups.setdefault(row["post_date"], []).append(row)
    for day_rows in groups.values():
        ig_feed = next(r for r in day_rows
                       if r["account"] == "instagram" and r["format"] == "feed")
        fb_feed = next(r for r in day_rows if r["account"] == "facebook")
        story = next(r for r in day_rows if r["format"] == "story")
        assert ig_feed["logical_post_id"] == fb_feed["logical_post_id"]
        assert story["logical_post_id"] != ig_feed["logical_post_id"]
    feed_ids = [r["logical_post_id"] for r in rows if r["format"] == "feed"
                and r["account"] == "instagram"]
    assert len(set(feed_ids)) == len(feed_ids)


def test_same_object_retry_preserves_valid_uuid_and_rejects_invalid(monkeypatch):
    monkeypatch.setattr(demo.config, "logical_post_id_enabled", lambda: True,
                        raising=False)
    row = {}
    assert demo._ensure_logical_post_id(row)
    original = row["logical_post_id"]
    assert demo._ensure_logical_post_id(row)
    assert row["logical_post_id"] == original
    assert not demo._ensure_logical_post_id({"logical_post_id": "invalid"})


def test_unassignable_identity_fails_closed_before_insert(monkeypatch):
    monkeypatch.setattr(demo.config, "logical_post_id_enabled", lambda: True,
                        raising=False)
    monkeypatch.setattr(demo.uuid, "uuid4", lambda: (_ for _ in ()).throw(OSError()))
    store = _Store()

    result = demo.seed("gym", store=store, days=1,
                       start=date(2026, 10, 4))

    assert result["ok"] is False
    assert result["seeded"] == 0
    assert store.inserted == []


class _Store:
    def __init__(self):
        self.inserted = []

    def list_month(self, _gym, _month):
        return []

    def insert_rows(self, _gym, rows):
        self.inserted.extend(rows)
        return rows

