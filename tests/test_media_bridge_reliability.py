"""Focused reliability checks for client media alerts and upload rearming."""

import json
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
from agent import client_media_sync as cms
from agent import config, media_bridge as bridge
from agent.media_bridge_route import Route


def _valid_jpeg():
    """A decoder-valid photo fixture, never placeholder bytes."""
    output = BytesIO()
    Image.new("RGB", (8, 8), color=(36, 92, 127)).save(output, format="JPEG")
    return output.getvalue()


def _approved_sidecar(path):
    path.write_text(json.dumps({
        "approved": True, "moderation": "clean", "review": False,
        "people": True, "consent": "granted",
    }))


class R2:
    def __init__(self, objects):
        self.objects = dict(objects)

    def list_keys(self, prefix):
        return [key for key in self.objects if key.startswith(prefix)]

    def get_bytes(self, key):
        return self.objects.get(key)


class Poster:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def _chat_post(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        time.sleep(0.03)
        return {"ok": True, "ts": str(len(self.calls))}


def test_rearm_only_after_a_new_intake_object(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_CHANNELS", '{"gymx":"C123CLIENT"}')
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "content_library"))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")

    first = "intake/gymx/incoming/20260918T090000Z_photo.jpg"
    r2 = R2({first: _valid_jpeg()})
    library = tmp_path / "content_library" / "gymx"
    library.mkdir(parents=True)
    _approved_sidecar(library / "20260918T090000Z_photo.json")
    assert cms.sync_uploads("gymx", r2=r2)["synced"] == 1
    assert bridge.episode("gymx", create=False) is None

    # Model the active depletion episode a client notice would have opened.
    assert bridge.episode("gymx", create=True)

    # A later object is observed first, then becomes usable after review. Only
    # that reviewed re-listing may re-arm this gym's notice episode.
    second = "intake/gymx/incoming/20260918T100000Z_photo.jpg"
    r2.objects[second] = _valid_jpeg()
    assert cms.sync_uploads("gymx", r2=r2)["synced"] == 1
    assert bridge.episode("gymx", create=False) is not None
    _approved_sidecar(library / "20260918T100000Z_photo.json")
    assert cms.sync_uploads("gymx", r2=r2)["synced"] == 0
    assert bridge.episode("gymx", create=False) is None

    # Re-listing the same R2 object, including after the local file is removed,
    # cannot re-arm the episode.
    (tmp_path / "content_library" / "gymx" / "20260918T100000Z_photo.jpg").unlink()
    assert cms.sync_uploads("gymx", r2=r2)["synced"] == 1
    assert bridge.episode("gymx", create=False) is None


def test_first_upload_after_empty_scan_rearms(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "content_library"))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    r2 = R2({})

    assert cms.sync_uploads("gymx", r2=r2) == {"synced": 0, "skipped": 0}
    assert bridge.episode("gymx", create=True)
    library = tmp_path / "content_library" / "gymx"
    library.mkdir(parents=True)
    r2.objects["intake/gymx/incoming/20260918T110000Z_photo.jpg"] = _valid_jpeg()
    assert cms.sync_uploads("gymx", r2=r2)["synced"] == 1
    assert bridge.episode("gymx", create=False) is not None
    _approved_sidecar(library / "20260918T110000Z_photo.json")
    assert cms.sync_uploads("gymx", r2=r2)["synced"] == 0
    assert bridge.episode("gymx", create=False) is None

def test_notice_delivery_is_atomic_per_gym(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_CHANNELS", '{"gymx":"C123CLIENT"}')
    monkeypatch.setattr(config, "slack_convo_client_reply_armed", lambda _: True)
    monkeypatch.setattr(config, "SLACK_CHANNEL_ID", "C999OPS")
    from agent import intake_web
    monkeypatch.setattr(intake_web, "is_revoked", lambda _: False)
    from agent import media_bridge_route
    monkeypatch.setattr(media_bridge_route, "resolve_client_route",
                        lambda _: Route(channel="C123CLIENT", gym_id="gymx"))

    poster = Poster()
    account = SimpleNamespace(slack_channel="")
    results = []

    def send():
        results.append(bridge.notify_bridge("gymx", account, poster=poster,
                                           now=datetime(2026, 9, 30, 16, tzinfo=timezone.utc)))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(poster.calls) == 1
    assert sorted(result.get("sent", False) for result in results) == [False, True]
    assert any(result.get("reason") == "unresolved send" for result in results)
