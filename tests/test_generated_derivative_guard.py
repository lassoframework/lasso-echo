"""Generated gap-filler derivatives are never client media (Swift River regression).

2026-10-02: swiftrivercrossfite5c9db had 13 approved Drive photos (all used inside
90 days) and a bridge-OFF planner that counted/picked Echo-generated igfill_*
infographic cards as if they were the gym's uploads, producing a month of local
derivative rows with source_media_asset_id null and visual repeats across days.
These tests pin the guard at the two central helpers, independent of media_bridge.
"""

from PIL import Image

from agent import client_content, client_month_run, config, rotation


def _photo(path):
    image = Image.new("RGB", (32, 32), "navy")
    image.save(path, "JPEG")


def _bridge_off_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "false")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    monkeypatch.setattr(rotation, "load_served", lambda: {})


def test_bridge_off_count_ignores_generated_derivatives(monkeypatch, tmp_path):
    _bridge_off_env(monkeypatch, tmp_path)
    gym = tmp_path / "gymx"
    gym.mkdir()
    _photo(gym / "igfill_2026-09-30_card_a.png")
    _photo(gym / "no_media_2026-09-30.png")
    _photo(gym / "seed_demo.png")
    _photo(gym / "real.jpg")
    assert client_month_run._client_media_count(str(gym)) == 1
    assert client_month_run.client_awaiting_media("gymx", str(gym)) is False
    (gym / "real.jpg").unlink()
    assert client_month_run._client_media_count(str(gym)) == 0
    assert client_month_run.client_awaiting_media("gymx", str(gym)) is True


def test_bridge_off_pick_image_never_returns_generated_derivative(monkeypatch, tmp_path):
    _bridge_off_env(monkeypatch, tmp_path)
    gym = tmp_path / "gymx"
    gym.mkdir()
    _photo(gym / "igfill_2026-09-30_card_a.png")
    _photo(gym / "no_media_2026-09-30.png")
    _photo(gym / "seed_demo.png")
    # Only derivatives exist: nothing plannable, even bridge-off and not refused.
    assert client_content.pick_image("gymx_ig", "2026-10-03", str(gym)) is None
    # A real photo is still picked; derivatives never shadow it.
    _photo(gym / "real.jpg")
    picked = client_content.pick_image("gymx_ig", "2026-10-03", str(gym))
    assert picked is not None
    assert "igfill_" not in picked.path
    assert "no_media_" not in picked.path
    assert "seed_" not in picked.path


def test_bridge_on_count_and_pick_also_ignore_derivatives(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    monkeypatch.setattr(rotation, "load_served", lambda: {})
    gym = tmp_path / "gymx"
    gym.mkdir()
    _photo(gym / "igfill_2026-09-30_card_a.png")
    _photo(gym / "real.jpg")
    assert client_month_run._client_media_count(str(gym)) == 0
    assert client_content.pick_image("gymx_ig", "2026-10-03", str(gym)) is None
