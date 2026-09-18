"""Independent failure cases for the fixed client media bridge episode."""

from datetime import datetime, timezone

from agent import config


def test_display_does_not_create_after_episode_expires(monkeypatch, tmp_path):
    from agent import calendar_autopublish, media_bridge, no_creative_fallback

    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda _: False)
    monkeypatch.setattr(
        calendar_autopublish,
        "_local_now",
        lambda *args: datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        media_bridge,
        "episode",
        lambda *args, **kwargs: {
            "id": "original_episode",
            "start": "2026-09-19",
            "end": "2026-09-20",
        },
    )
    no_creative_fallback._URL_CACHE.clear()
    calls = []

    def render(*args, **kwargs):
        calls.append("render")
        return str(tmp_path / "rendered.png")

    def host(*args, **kwargs):
        calls.append("host")
        return "https://cdn.example.com/late.png"

    post = {
        "id": "late_post",
        "day_key": "2026-09-24",
        "caption": "A gym fact approved by the client.",
        "pillar": "education",
        "format": "feed",
        "image_url": None,
        "account_key": "gymx",
    }
    assert no_creative_fallback.display_image_for(
        post, renderer=render, host=host, tenant="gymx"
    ) is None
    assert calls == []


def test_revocation_read_outage_refuses_client_route(monkeypatch):
    from agent import intake_web, media_bridge_route

    class BrokenR2:
        def get_bytes(self, key):
            raise OSError("storage unavailable")

    monkeypatch.setattr(intake_web, "_default_r2", lambda: BrokenR2())
    route = media_bridge_route.resolve_client_route("gymx")
    assert not route.ok
    assert route.reason == "revocation unavailable"


def test_corrupt_photo_is_not_usable_inventory(monkeypatch, tmp_path):
    from agent import client_infographic_fill

    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    gym_dir = tmp_path / "gymx"
    gym_dir.mkdir()
    (gym_dir / "photo.jpg").write_bytes(b"not a photograph")
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    assert client_infographic_fill.real_media_depleted("gymx") is True
