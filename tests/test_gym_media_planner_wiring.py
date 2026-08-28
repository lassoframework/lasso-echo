"""gym_media_drive §7 WIRING: a gym that connected Google Drive actually gets
PENDING content_calendar posts built from its synced photo pool.

This is the regression guard for the audit's CRITICAL #1: build_gym_media_draft had
NO production caller, so a synced Drive photo never became a post. It is now wired
into client_month_run.build_client_month behind GYM_DRIVE_STAGE + the per-gym
GYM_DRIVE_CONNECT arming. Also covers CRITICAL #2: the staged row carries
source_media_asset_id so the portal hide + the removed-from-Drive sweep flip it back
to needs_media.

Fully OFFLINE: injected media store + drive fakes, stubbed vision/caption/hosting.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr, client_sources as cs  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


class _FakeCalStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, base_key, month):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        return rows


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n=2):
    """A tiny uploaded-media library so the MEDIA-REQUIRED guard passes and the
    uploaded-media loop places a couple feeds; the Drive lane then fills the GAP days."""
    import json
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stock_sources(account_key="gritx_ig"):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source(account_key, "service", "Small group training",
                  "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s",
                  "client social intake")


def _arm_drive_lane(monkeypatch, drive, store):
    """Flip GYM_DRIVE_STAGE on + arm the gym, and point the builder at the fakes +
    stub the vision/caption/host lanes so the pick->caption->host path runs offline."""
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")
    # The builder resolves its own store/drive from these factories when the wiring
    # calls it without injecting them (the production call path).
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None: {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "members in a class"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption about the class", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, gym: "https://cdn.fake/drive_served.jpg")


# ---- CRITICAL #1: a synced Drive photo becomes a PENDING calendar row -------------
def test_connected_drive_photo_becomes_pending_post(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic1", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic1": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True

    # A row sourced from the Drive asset landed, PENDING, on the real gym (gym_id=base),
    # carrying its served url AND the source_media_asset_id stamp.
    drive_rows = [r for r in cal.inserted
                  if r.get("source_media_asset_id") == "drivepic1"]
    assert drive_rows, "no content_calendar row was built from the connected Drive photo"
    for r in drive_rows:
        assert r["status"] == "pending"           # the human tap is untouched
        assert r["gym_id"] == "gritx"
        assert r["image_url"] == "https://cdn.fake/drive_served.jpg"
    # usage was stamped on the media asset at stage time (90-day reuse cooldown basis).
    assert store.assets["drivepic1"]["used_count"] == 1


# ---- flag OFF -> the Drive lane is inert (uploaded-media month unchanged) ----------
def test_drive_lane_inert_when_flag_off(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic1", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic1": b"jpgbytes"})
    # Arm the fakes/stubs but keep GYM_DRIVE_STAGE OFF.
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    monkeypatch.delenv("GYM_DRIVE_STAGE", raising=False)
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert not [r for r in cal.inserted if r.get("source_media_asset_id")]
    # the media asset was never touched (no stage, no usage stamp)
    assert store.assets["drivepic1"]["used_count"] == 0


# ---- flag ON but gym NOT armed -> lane inert --------------------------------------
def test_drive_lane_inert_when_gym_not_armed(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic1", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic1": b"jpgbytes"})
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    # GYM_DRIVE_CONNECT off AND gritx not in the pilot allowlist -> not armed.
    monkeypatch.delenv("GYM_DRIVE_CONNECT", raising=False)
    monkeypatch.delenv("GYM_DRIVE_CONNECT_GYMS", raising=False)

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert not [r for r in cal.inserted if r.get("source_media_asset_id")]


# ---- pool empty -> no Drive rows, uploaded-media month intact ---------------------
def test_empty_pool_adds_no_drive_rows(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[])          # nothing synced yet
    drive = FakeDrive(blobs={})
    _arm_drive_lane(monkeypatch, drive, store)

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert not [r for r in cal.inserted if r.get("source_media_asset_id")]
    # the uploaded-media month still built its own real-photo rows
    assert any(r.get("image_url", "").startswith("https://gritx.media/")
               for r in cal.inserted)


# ---- no approved source -> a Drive photo NEVER posts on imagination ---------------
def test_no_approved_source_builds_no_drive_post(monkeypatch, tmp_path):
    # A synced Drive photo + an armed lane, but the gym has NO approved sources at all.
    # The Drive caption's fact must come from an approved source, so the lane declines
    # to stage (a photo never posts without an approved fact behind the copy). We still
    # need the uploaded-media guard to pass, so seed sources for the UPLOADED month via
    # a DIFFERENT gym is not possible; instead assert the Drive lane specifically added
    # nothing by resolving the helper directly against a source-less account.
    from datetime import date
    store = FakeMediaStore(assets=[
        make_asset("drivepicX", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepicX": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    # gritx_ig has no client_sources rows in this test (none added).
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 3, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store)
    assert extra == []                       # nothing staged without an approved fact
    assert store.assets["drivepicX"]["used_count"] == 0


# ---- the helper marks the draft PENDING + stamps the asset id ---------------------
def test_append_helper_builds_pending_with_asset_id(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic9", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic9": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    from datetime import date
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 3, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store)
    assert extra, "the helper built no Drive drafts"
    d = extra[0]
    assert d.status == DraftStatus.PENDING
    assert d.draft_type == "gym_media"
    assert d.source_media_asset_id == "drivepic9"
