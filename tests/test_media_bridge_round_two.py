"""Round 2 media eligibility and durable rearm contracts."""

import importlib

from PIL import Image

from agent import client_infographic_fill, config, dam, media_bridge, upload_media_approvals
from agent.accounts import Account, Platform


def _photo(path):
    image = Image.new("RGB", (32, 32), "navy")
    image.save(path, "JPEG")


def test_local_inventory_rearms_once_only_after_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    gym = tmp_path / "gymx"
    gym.mkdir()
    photo = gym / "one.jpg"
    _photo(photo)
    assert client_infographic_fill.real_media_depleted("gymx")
    first = media_bridge.episode("gymx")["id"]
    dam.write_sidecar(str(photo), {"approved": True, "moderation": "clean",
                                   "people": False})
    assert not client_infographic_fill.real_media_depleted("gymx")
    assert media_bridge.episode("gymx", create=False) is None
    second = media_bridge.episode("gymx")["id"]
    assert second != first
    assert not client_infographic_fill.real_media_depleted("gymx")
    assert media_bridge.episode("gymx", create=False)["id"] == second


def test_real_depletion_observer_cannot_rearm_after_approval_consumed_asset(monkeypatch, tmp_path):
    """Exercise the production depletion observer across approval and restart-safe state.

    The asset is approved first, then style-excluded to create a later depletion
    episode. Removing that exclusion makes the old asset observable again, but
    it must not consume the later episode a second time.
    """
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    account = Account(key="gymx_ig", display_name="Gym X", platform=Platform.INSTAGRAM,
                      token_env="X", target_id_env="Y", approvers=["U_REVIEWER"])
    monkeypatch.setattr(upload_media_approvals, "get_account",
                        lambda key: account if key == "gymx_ig" else None)
    gym = tmp_path / "gymx"
    gym.mkdir()
    photo = gym / "approved.jpg"
    _photo(photo)
    dam.write_sidecar(str(photo), {"consent": "granted"})

    initial = media_bridge.episode("gymx")
    assert upload_media_approvals.approve(
        "gymx", photo.name, "U_REVIEWER", moderation="clean")[0] == 200
    assert media_bridge.episode("gymx", create=False) is None

    (gym / "style_exclusions.json").write_text('{"off_style":["approved.jpg"]}')
    assert client_infographic_fill.real_media_depleted("gymx") is True
    later = media_bridge.episode("gymx")
    assert later["id"] != initial["id"]

    # A worker restart only reloads code; its identity ledger remains in SQLite.
    importlib.reload(media_bridge)
    (gym / "style_exclusions.json").unlink()
    assert client_infographic_fill.real_media_depleted("gymx") is False
    assert media_bridge.episode("gymx", create=False)["id"] == later["id"]


def test_corrupt_and_unapproved_are_not_usable(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    gym = tmp_path / "gymx"
    gym.mkdir()
    photo = gym / "bad.jpg"
    photo.write_bytes(b"bad")
    dam.write_sidecar(str(photo), {"approved": True, "moderation": "clean",
                                   "people": False})
    assert client_infographic_fill.real_media_depleted("gymx")
    _photo(photo)
    dam.write_sidecar(str(photo), {"approved": False, "moderation": "clean"})
    assert client_infographic_fill.real_media_depleted("gymx")


def test_drive_pickable_new_id_rearms_once(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _: True)
    from agent import gym_media_index, gym_media_selector
    class Store:
        def available(self):
            return True
        def list_assets(self, gym):
            return rows
    monkeypatch.setattr(gym_media_index, "default_store", Store)
    rows = []
    monkeypatch.setattr(gym_media_selector, "pickable", lambda *a, **k: rows)
    assert client_infographic_fill.real_media_depleted("gymx")
    first = media_bridge.episode("gymx")["id"]
    rows.append({"id": "new-approved-drive-media"})
    assert not client_infographic_fill.real_media_depleted("gymx")
    assert media_bridge.episode("gymx", create=False) is None
    second = media_bridge.episode("gymx")["id"]
    assert not client_infographic_fill.real_media_depleted("gymx")
    assert media_bridge.episode("gymx", create=False)["id"] == second


def test_armed_planner_count_and_alert_share_eligibility(monkeypatch, tmp_path):
    from agent import client_content, client_month_run, rotation
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    monkeypatch.setattr(rotation, "load_served", lambda: {})
    gym = tmp_path / "gymx"
    gym.mkdir()
    photo = gym / "one.jpg"
    _photo(photo)
    assert client_month_run._client_media_count(str(gym)) == 0
    assert client_content.pick_image("gymx_ig", "2026-09-19", str(gym)) is None
    assert client_infographic_fill.real_media_depleted("gymx")
    dam.write_sidecar(str(photo), {"approved": True, "moderation": "clean",
                                   "people": False})
    assert client_month_run._client_media_count(str(gym)) == 1
    assert client_content.pick_image("gymx_ig", "2026-09-19", str(gym)) is not None
    assert not client_infographic_fill.real_media_depleted("gymx")


def test_style_exclusion_is_absent_from_planner_count_and_depletion(monkeypatch, tmp_path):
    from agent import client_content, client_month_run, rotation
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    monkeypatch.setattr(rotation, "load_served", lambda: {})
    gym = tmp_path / "gymx"
    gym.mkdir()
    photo = gym / "one.jpg"
    _photo(photo)
    dam.write_sidecar(str(photo), {"approved": True, "moderation": "clean",
                                   "people": False})
    (gym / "style_exclusions.json").write_text('{"off_style":["one.jpg"]}')
    assert client_content.pick_image("gymx_ig", "2026-09-19", str(gym)) is None
    assert client_month_run._client_media_count(str(gym)) == 0
    assert client_infographic_fill.real_media_depleted("gymx")


def test_explicit_consent_denial_stays_out_with_guard_and_bridge_off(monkeypatch, tmp_path):
    from agent import client_content, client_month_run, rotation
    monkeypatch.delenv("AGENT_CONSENT_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_MEDIA_BRIDGE_ALERTS", raising=False)
    monkeypatch.setattr(rotation, "load_served", lambda: {})
    gym = tmp_path / "gymx"
    gym.mkdir()
    photo = gym / "one.jpg"
    _photo(photo)
    dam.write_sidecar(str(photo), {"approved": True, "moderation": "clean",
                                   "people": True, "consent": "denied"})
    # macOS can create this resource-fork sidecar on a non-APFS volume.  It
    # must not look like a consent-free second image beside the denied photo.
    (gym / "._one.jpg").write_bytes(b"AppleDouble metadata")
    assert client_content.pick_image("gymx_ig", "2026-09-19", str(gym)) is None
    assert client_month_run._client_media_count(str(gym)) == 0
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    assert client_content.pick_image("gymx_ig", "2026-09-19", str(gym)) is None
    assert client_month_run._client_media_count(str(gym)) == 0
    assert client_infographic_fill.real_media_depleted("gymx")


def test_bridge_inventory_fails_closed_for_unknown_or_pending_people(monkeypatch, tmp_path):
    """Neither unclassified nor pending-consent media can hide depletion."""
    from agent import client_content, client_media_sync, client_month_run, rotation
    from agent.library import Creative
    monkeypatch.delenv("AGENT_CONSENT_GUARD_ENABLED", raising=False)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)
    monkeypatch.setattr(config, "vision_enabled_for", lambda _: False)
    monkeypatch.setattr(rotation, "load_served", lambda: {})
    gym = tmp_path / "gymx"
    gym.mkdir()
    unknown = gym / "unknown.jpg"
    pending = gym / "pending.jpg"
    _photo(unknown)
    _photo(pending)
    dam.write_sidecar(str(unknown), {"approved": True, "moderation": "clean"})
    dam.write_sidecar(str(pending), {"approved": True, "moderation": "clean",
                                     "people": True, "consent": "pending"})

    # Feature off retains the legacy handling of an unclassified asset.
    assert client_media_sync.usable_local_creative(
        Creative(path=str(unknown), media_type="image"), "gymx_ig", used=set())

    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    assert client_month_run._client_media_count(str(gym)) == 0
    assert client_content.pick_image("gymx_ig", "2026-09-19", str(gym)) is None
    assert client_infographic_fill.real_media_depleted("gymx") is True


def test_drive_explicit_refusals_are_not_pickable_or_usable():
    from agent import gym_media_selector
    base = {"id": "one", "gym_id": "gymx", "kind": "photo",
            "eligible": True, "excluded_by_coach": False}
    class Store:
        def available(self):
            return True
        def list_assets(self, gym):
            return [dict(base, **refusal) for refusal in
                    ({"approved": False}, {"moderation": "rejected"},
                     {"consent": "denied"})]
    assert gym_media_selector.pickable("gymx", store=Store()) == []
    for refusal in ({"approved": False}, {"moderation": "rejected"},
                    {"consent": "denied"}):
        assert not gym_media_selector.is_usable(dict(base, **refusal))


def test_failed_drive_inventory_read_cannot_confirm_depletion(monkeypatch, tmp_path):
    from agent import gym_media_index
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _: True)
    class Broken:
        def available(self):
            return True
        def list_assets(self, gym):
            raise OSError("read failed")
    monkeypatch.setattr(gym_media_index, "default_store", Broken)
    assert not client_infographic_fill.real_media_depleted("gymx")
    assert media_bridge.episode("gymx", create=False) is None


def test_bridge_off_keeps_legacy_planner_count(monkeypatch, tmp_path):
    from agent import client_month_run
    monkeypatch.delenv("AGENT_MEDIA_BRIDGE_ALERTS", raising=False)
    gym = tmp_path / "gymx"
    gym.mkdir()
    (gym / "legacy.jpg").write_bytes(b"legacy fixture")
    (gym / "._legacy.jpg").write_bytes(b"AppleDouble metadata")
    assert client_month_run._client_media_count(str(gym)) == 1


def test_old_unresolved_notice_remains_reconcilable_after_rearm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    first = media_bridge.episode("gymx")
    conn = media_bridge.db.connect()
    with conn:
        media_bridge._put(conn, media_bridge._outbox_key("gymx", first), {
            "status": "unresolved", "channel": "C123CLIENT",
            "text": media_bridge.notice_text(), "created_at": "2026-09-18T09:00:00Z"})
    conn.close()
    media_bridge.rearm_for_new_upload("gymx", "approved-new-upload", usable=True)
    rows = media_bridge.unresolved_notices("gymx")
    assert rows[0]["episode_id"] == first["id"]
    assert media_bridge.reconcile_notice(
        "gymx", episode_id=first["id"], delivered=True,
        channel="C123CLIENT", ts="123.456")["status"] == "sent"
    assert media_bridge.notice_status("gymx")[0]["ts"] == "123.456"


def test_failed_listing_or_download_does_not_claim_upload(monkeypatch, tmp_path):
    from agent import client_media_sync
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    key = "intake/gymx/incoming/new.jpg"
    class R2:
        def __init__(self):
            self.fail_list = True
        def list_keys(self, prefix):
            if self.fail_list and prefix.endswith("pending_caption/"):
                raise OSError("listing failed")
            return [key] if prefix.endswith("incoming/") else []
        def get_bytes(self, object_key):
            raise OSError("download failed")
    r2 = R2()
    assert client_media_sync.sync_uploads("gymx", r2=r2)["synced"] == 0
    r2.fail_list = False
    assert client_media_sync.sync_uploads("gymx", r2=r2)["synced"] == 0
    assert not (tmp_path / "gymx" / "new.jpg").exists()
    conn = media_bridge.db.connect()
    try:
        assert conn.execute("SELECT value FROM kv WHERE key=?",
                            ("media_bridge_uploads_gymx",)).fetchone() is None
    finally:
        conn.close()
