"""PR235 global exact-byte read guard for local client-library photos.

The database draft is keyed by MD5 while the existing local served table keeps
SHA-256.  These checks make sure the local picker and the Astra depletion gate
ask the shared ledger only when explicitly armed, and hold on uncertainty.
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_infographic_fill, config, rotation, runner  # noqa: E402
from agent import gym_media_selector as selector  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv(selector.GLOBAL_LEDGER_FLAG_ENV, raising=False)
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    yield


def _library(tmp_path, names=("photo.jpg",)):
    root = tmp_path / "gymx"
    root.mkdir()
    for name in names:
        # list_creatives only needs an image suffix for this focused selector test.
        (root / name).write_bytes(b"exact local photo bytes:" + name.encode())
    return root


def test_global_guard_off_does_not_read_ledger(monkeypatch, tmp_path):
    root = _library(tmp_path)
    monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                        lambda *args, **kwargs: pytest.fail("ledger read while OFF"))

    pick = client_content.pick_image("gymx_ig", "2026-10-03", str(root))

    assert pick is not None and os.path.basename(pick.path) == "photo.jpg"


def test_global_guard_excludes_matching_local_bytes(monkeypatch, tmp_path):
    root = _library(tmp_path, ("used.jpg", "free.jpg"))
    used_path = str(root / "used.jpg")
    used = rotation.local_global_fingerprint(used_path)
    seen = {}
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "on")

    def _ledger(base, fingerprints, **kwargs):
        seen["base"] = base
        seen["fingerprints"] = set(fingerprints)
        return {used}

    monkeypatch.setattr(selector, "cross_client_used_fingerprints", _ledger)
    pick = client_content.pick_image("gymx_ig", "2026-10-03", str(root))

    assert pick is not None and os.path.basename(pick.path) == "free.jpg"
    assert seen["base"] == "gymx"
    assert used in seen["fingerprints"]
    assert all(item.startswith("md5:") for item in seen["fingerprints"])


@pytest.mark.parametrize("flag", ("maybe", "true"))
def test_global_guard_uncertainty_holds_local_pick(monkeypatch, tmp_path, flag):
    root = _library(tmp_path)
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, flag)
    if flag == "true":
        monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                            lambda *args, **kwargs: (_ for _ in ()).throw(
                                selector.GlobalLedgerUnavailable("unreadable")))

    with pytest.raises(client_content.LocalPhotoGlobalLedgerUnavailable):
        client_content.pick_image("gymx_ig", "2026-10-03", str(root))


def test_uncertain_local_global_ledger_holds_daily_video_tier(monkeypatch, tmp_path):
    """``None`` means depleted; global uncertainty must instead produce ``None``
    from the runner probe so its photo-first state machine never reaches video."""
    from types import SimpleNamespace

    root = _library(tmp_path)
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
    monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            selector.GlobalLedgerUnavailable("unreadable")))
    checks = []

    def _drive(_account, kind):
        checks.append(kind)
        return False if kind == "photo" else pytest.fail("video tier must hold")

    monkeypatch.setattr(runner, "_client_drive_kind_available", _drive)
    monkeypatch.setattr(client_content, "build_client_draft",
                        lambda *args, **kwargs: pytest.fail("video fallback must hold"))
    account = SimpleNamespace(key="gymx_ig")

    assert runner._client_local_photo_available(account, "2026-10-03", str(root)) is None
    assert runner._client_photo_first_draft(account, "2026-10-03", object(), str(root)) is None
    assert checks == ["photo"]


def test_depletion_does_not_unlock_astra_when_global_local_read_is_uncertain(
        monkeypatch, tmp_path):
    root = _library(tmp_path)
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
    monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            selector.GlobalLedgerUnavailable("unreadable")))
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: False)

    # A known local file exists, but unproven global state is not proof it is
    # depleted. The fallback must therefore stay held.
    assert client_infographic_fill.real_media_depleted(root.name) is False


def test_local_fingerprint_is_exact_bytes_not_filename(tmp_path):
    root = _library(tmp_path, ("one.jpg", "two.jpg"))
    second = root / "two.jpg"
    second.write_bytes((root / "one.jpg").read_bytes())

    assert rotation.local_global_fingerprint(str(root / "one.jpg")) == \
        rotation.local_global_fingerprint(str(second))
    assert rotation.local_global_fingerprint(str(second)) == "md5:" + hashlib.md5(
        second.read_bytes()).hexdigest()


def test_carousel_uses_each_slide_and_does_not_starve_other_photos(monkeypatch, tmp_path):
    root = _library(tmp_path, ("standalone.jpg",))
    carousel = root / "carousel"
    carousel.mkdir()
    slide_one, slide_two = carousel / "one.jpg", carousel / "two.jpg"
    slide_one.write_bytes(b"slide one")
    slide_two.write_bytes(b"slide two")
    used = rotation.local_global_fingerprint(str(slide_one))
    seen = set()
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")

    def _ledger(_base, fingerprints, **_kwargs):
        seen.update(fingerprints)
        return {used}

    monkeypatch.setattr(selector, "cross_client_used_fingerprints", _ledger)
    picked = client_content.pick_image("gymx_ig", "2026-10-03", str(root))

    assert picked is not None and os.path.basename(picked.path) == "standalone.jpg"
    assert rotation.local_global_fingerprint(str(slide_one)) in seen
    assert rotation.local_global_fingerprint(str(slide_two)) in seen
    assert all(os.path.isfile(path) for path in (slide_one, slide_two))


def test_global_photo_guard_does_not_block_video_only_local_supply(monkeypatch, tmp_path):
    root = _library(tmp_path, ("class.mp4",))
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
    monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                        lambda *args, **kwargs: pytest.fail("video must not query photo ledger"))

    picked = client_content.pick_image("gymx_ig", "2026-10-03", str(root))

    assert picked is not None and picked.media_type == "video"


def test_video_only_local_supply_blocks_infographic_depletion(monkeypatch, tmp_path):
    root = _library(tmp_path, ("class.mp4",))
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _base: False)
    from agent import client_media_sync
    monkeypatch.setattr(client_media_sync, "usable_local_creative", lambda *args, **kwargs: True)

    assert client_infographic_fill.real_media_depleted(root.name) is False


def test_local_global_ledger_batches_101_fingerprints_over_http(monkeypatch, tmp_path):
    """One local-library check makes one tenant read plus 100/1 usage pages."""
    root = tmp_path / "batch"
    root.mkdir()
    paths = []
    for index in range(101):
        path = root / f"{index:03d}.jpg"
        path.write_bytes(f"exact bytes {index}".encode())
        paths.append(str(path))
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-test")

    class Response:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows
            self.headers = {"Content-Range": (f"*/0" if not rows
                             else f"0-{len(rows) - 1}/{len(rows)}")}

        def json(self):
            return self._rows

    class Http:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append((url, params))
            if url.endswith("tenant_alias"):
                return Response([{"alias_key": "gymx",
                                  "tenant_id": "11111111-2222-3333-4444-555555555555"}])
            # PR385 fail-closed: the selector also reads
            # visual_global_historical_incident, one bounded page per 100
            # fingerprints. Model a complete-but-empty read (no incident rows
            # prove these bytes were ever globally consumed).
            assert url.endswith(("visual_global_usage",
                                 "visual_global_historical_incident"))
            return Response([])

    http = Http()
    available = rotation.globally_available_local_paths("gymx_ig", paths,
                                                         ledger_http=http)

    usage_calls = [call for call in http.calls if call[0].endswith("visual_global_usage")]
    incident_calls = [call for call in http.calls
                      if call[0].endswith("visual_global_historical_incident")]
    assert available == set(paths)
    # one tenant mapping + two usage pages + two incident pages
    assert len(http.calls) == 5
    assert [len(call[1]["fingerprint"][4:-1].split(",")) for call in usage_calls] == [100, 1]
    assert [len(call[1]["fingerprint"][4:-1].split(",")) for call in incident_calls] == [100, 1]
