"""Scheduled day context must reach Drive availability and budget reads."""
from agent import runner


class _Account:
    key = "pierce_ig"


def test_photo_first_availability_checks_the_scheduled_day(monkeypatch):
    monkeypatch.setattr(runner.config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "gym_drive_connect_active_for", lambda _key: True)

    class Store:
        def available(self):
            return True
        def list_assets(self, _base):
            return []
        def list_sources(self, _base):
            return []

    from agent import gym_media_index, gym_media_selector
    monkeypatch.setattr(gym_media_index, "default_store", lambda: Store())
    seen = {}
    monkeypatch.setattr(gym_media_selector, "base_gym_key", lambda _key: "pierce")
    monkeypatch.setattr(
        gym_media_selector, "pickable",
        lambda *args, **kwargs: seen.update(kwargs) or [object()])

    assert runner._client_drive_kind_available(
        _Account(), "photo", "2026-10-11") is True
    assert seen["post_date"] == "2026-10-11"


def test_unknown_scheduled_day_keeps_legacy_availability_call(monkeypatch):
    monkeypatch.setattr(runner.config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "gym_drive_connect_active_for", lambda _key: True)

    class Store:
        def available(self):
            return True
        def list_assets(self, _base):
            return []
        def list_sources(self, _base):
            return []

    from agent import gym_media_index, gym_media_selector
    monkeypatch.setattr(gym_media_index, "default_store", lambda: Store())
    seen = {}
    monkeypatch.setattr(gym_media_selector, "base_gym_key", lambda _key: "pierce")
    monkeypatch.setattr(
        gym_media_selector, "pickable",
        lambda *args, **kwargs: seen.update(kwargs) or [])

    assert runner._client_drive_kind_available(_Account(), "photo") is False
    assert "post_date" not in seen


def test_month_range_budget_deduplicates_later_date_assets(monkeypatch):
    from datetime import date
    from agent import client_month_run, gym_media_selector
    monkeypatch.setattr(gym_media_selector, "scene_guard_flag", lambda: True)
    seen = []
    def pool(_base, *, post_date):
        seen.append(post_date)
        return [] if post_date == "2026-10-10" else [{"id": "later", "kind": "photo"}]
    monkeypatch.setattr(gym_media_selector, "pickable", pool)
    assert client_month_run._drive_range_pickable("pierce", date(2026, 10, 10), 3) == [{"id": "later", "kind": "photo"}]
    assert seen == ["2026-10-10", "2026-10-11", "2026-10-12"]
    seen.clear()
    assert client_month_run._drive_range_pickable("pierce", date(2026, 10, 10), 3,
                                                {"2026-10-11", "2026-10-12"}) == []
    assert seen == ["2026-10-10"]


def test_month_range_scene_off_preserves_first_day_budget_read(monkeypatch):
    from datetime import date
    from agent import client_month_run, gym_media_selector
    monkeypatch.setattr(gym_media_selector, "scene_guard_flag", lambda: False)
    seen = []
    monkeypatch.setattr(gym_media_selector, "pickable", lambda _base, **kw: seen.append(kw) or [{"id": "one"}])
    assert client_month_run._drive_range_pickable("pierce", date(2026, 10, 10), 3) == [{"id": "one"}]
    assert seen == [{"post_date": "2026-10-10"}]
