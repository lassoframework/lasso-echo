"""Human review is the only path that makes a client upload usable."""

import os
import sys
import http.client
import threading

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_media_sync, dam, db, media_bridge, upload_media_approvals as approvals
from agent.accounts import Account, Platform
from agent.library import Creative


def _jpeg(path):
    image = Image.new("RGB", (12, 12), color=(30, 90, 130))
    image.save(path, format="JPEG")


def _account():
    return Account(key="gymx_ig", display_name="Gym X", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y", library_prefix="", approvers=["U_REVIEWER"])


def _setup(monkeypatch, tmp_path, *, consent="granted"):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr(approvals.config, "LIBRARY_PATH", str(tmp_path))
    account = _account()
    monkeypatch.setattr(approvals, "get_account",
                        lambda key: account if key == "gymx_ig" else None)
    library = tmp_path / "gymx"
    library.mkdir()
    asset = library / "upload.jpg"
    _jpeg(asset)
    dam.write_sidecar(str(asset), {"consent": consent})
    return asset


def test_human_approval_records_clean_moderation_and_rearms_only_after_write(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(media_bridge, "rearm_for_new_upload",
                        lambda base, identity, *, usable: calls.append((base, identity, usable)))

    status, body = approvals.approve("gymx", "upload.jpg", "U_REVIEWER", moderation="clean")

    assert status == 200 and body == {"ok": True, "asset": "upload.jpg", "already_approved": False}
    side = dam.read_sidecar(str(asset))
    assert side["consent"] == "granted"
    assert side["approved"] is True and side["review"] is False
    assert side["moderation"] == "clean"
    assert side["approved_by"] == "U_REVIEWER"
    assert calls == [("gymx", "upload.jpg", True)]


def test_upload_cannot_be_autoapproved_or_bypass_consent_or_moderation(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path, consent="pending")
    calls = []
    monkeypatch.setattr(media_bridge, "rearm_for_new_upload",
                        lambda base, identity, *, usable: calls.append((base, identity, usable)))

    status, _ = approvals.approve("gymx", "upload.jpg", "U_REVIEWER", moderation="clean")
    assert status == 409
    assert dam.read_sidecar(str(asset)).get("approved") is not True

    dam.write_sidecar(str(asset), {"consent": "granted"})
    status, _ = approvals.approve("gymx", "upload.jpg", "U_REVIEWER", moderation="")
    assert status == 400
    assert dam.read_sidecar(str(asset)).get("approved") is not True
    assert calls == []


def test_only_configured_human_approver_can_transition_media(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(media_bridge, "rearm_for_new_upload",
                        lambda base, identity, *, usable: calls.append((base, identity, usable)))

    status, _ = approvals.approve("gymx", "upload.jpg", "U_OTHER", moderation="clean")

    assert status == 403
    assert dam.read_sidecar(str(asset)).get("approved") is not True
    assert calls == []


def test_forged_portal_request_cannot_approve_even_with_approver_id(monkeypatch, tmp_path):
    from agent import intake_web
    asset = _setup(monkeypatch, tmp_path)
    server = intake_web.build_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("POST", "/portal/Z3lteA.signature/uploaded-media/upload.jpg/approve",
                     body='{"actor_id":"U_REVIEWER","moderation":"clean"}',
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status in (404, 405)
        assert dam.read_sidecar(str(asset)).get("approved") is not True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_audit_failure_fails_closed(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(approvals, "_audit_approval", lambda *args: (_ for _ in ()).throw(OSError("down")))
    status, _ = approvals.approve("gymx", "upload.jpg", "U_REVIEWER", moderation="clean")
    assert status == 503
    assert dam.read_sidecar(str(asset)).get("approved") is not True


def test_retry_repairs_failed_rearm(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    calls = []
    state = media_bridge.episode("gymx")
    real_rearm = media_bridge.rearm_for_new_upload

    def rearm(base, identity, *, usable):
        calls.append((base, identity, usable))
        if len(calls) == 1:
            raise OSError("temporarily down")
        return real_rearm(base, identity, usable=usable)

    monkeypatch.setattr(media_bridge, "rearm_for_new_upload", rearm)
    first, _ = approvals.approve("gymx", "upload.jpg", "U_REVIEWER", moderation="clean")
    assert media_bridge.episode("gymx", create=False)["id"] == state["id"]
    second, body = approvals.approve("gymx", "upload.jpg", "U_REVIEWER", moderation="clean")
    assert first == 503 and second == 200 and body["already_approved"] is True
    assert calls == [("gymx", "upload.jpg", True), ("gymx", "upload.jpg", True)]
    assert dam.read_sidecar(str(asset))["approved"] is True
    assert len(db.audit_rows(account_key="gymx")) == 1
    assert media_bridge.episode("gymx", create=False) is None


def test_held_upload_becomes_usable_and_closes_depletion_episode(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(client_media_sync.config, "vision_enabled_for", lambda key: False)
    creative = Creative(path=str(asset), media_type="image")
    assert client_media_sync.usable_local_creative(creative, "gymx_ig", used=set()) is False
    state = media_bridge.episode("gymx")
    assert state is not None

    status, _ = approvals.approve("gymx", asset.name, "U_REVIEWER", moderation="clean")

    assert status == 200
    assert client_media_sync.usable_local_creative(creative, "gymx_ig", used=set()) is True
    assert media_bridge.episode("gymx", create=False) is None


def test_reapproving_seen_asset_cannot_clear_a_later_depletion_episode(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(client_media_sync.config, "vision_enabled_for", lambda _: False)
    first = media_bridge.episode("gymx")
    assert approvals.approve("gymx", asset.name, "U_REVIEWER", moderation="clean")[0] == 200
    assert media_bridge.episode("gymx", create=False) is None

    later = media_bridge.episode("gymx")
    assert later["id"] != first["id"]
    status, body = approvals.approve("gymx", asset.name, "U_REVIEWER", moderation="clean")
    assert status == 200 and body["already_approved"] is True
    assert media_bridge.episode("gymx", create=False)["id"] == later["id"]


def test_style_excluded_approval_cannot_clear_a_depletion_episode(monkeypatch, tmp_path):
    asset = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(client_media_sync.config, "vision_enabled_for", lambda _: False)
    (asset.parent / "style_exclusions.json").write_text('{"off_style":["upload.jpg"]}')
    state = media_bridge.episode("gymx")

    status, _ = approvals.approve("gymx", asset.name, "U_REVIEWER", moderation="clean")

    assert status == 200
    assert dam.read_sidecar(str(asset))["approved"] is True
    assert media_bridge.episode("gymx", create=False)["id"] == state["id"]
