"""gym_media_drive §7: every staged row PENDING, HEIC stages via rendition,
unprobed video never stages, tenant assertion blocks a cross-gym asset."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_builder as builder  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


class _Acct:
    key = "pierce_ig"
    platform = "instagram"


def _wire(monkeypatch, analysis=None):
    """Stub vision + caption + hosting so the builder path is exercised offline."""
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None: analysis or {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "three people at the gym"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption about the class", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, gym: "https://cdn.fake/served.jpg")


def test_photo_stages_pending(monkeypatch, tmp_path):
    _wire(monkeypatch)
    store = FakeMediaStore(assets=[make_asset("p1", gym_id="pierce", kind="photo")])
    drive = FakeDrive(blobs={"p1": b"jpgbytes"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is not None
    assert draft.status == DraftStatus.PENDING           # human tap untouched
    assert draft.draft_type == "gym_media"
    assert draft.creative_public_url == "https://cdn.fake/served.jpg"
    # usage was stamped at stage time.
    assert store.assets["p1"]["used_count"] == 1


def test_heic_photo_stages_via_rendition(monkeypatch, tmp_path):
    _wire(monkeypatch)
    monkeypatch.setattr("agent.gym_media_index.heic_to_jpeg",
                        lambda src, dest: open(dest, "wb").write(b"jpg") or dest)
    monkeypatch.setattr("agent.gym_media_index.ensure_rendition",
                        lambda asset, src, **k: ("https://cdn.fake/rend.jpg", True))
    store = FakeMediaStore(assets=[make_asset("h1", gym_id="pierce", kind="photo",
                                             title="IMG.HEIC", mime="image/heic")])
    drive = FakeDrive(blobs={"h1": b"heic"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "community", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is not None and draft.status == DraftStatus.PENDING
    assert draft.creative_public_url == "https://cdn.fake/rend.jpg"


def test_unprobed_video_never_stages(monkeypatch, tmp_path):
    _wire(monkeypatch)
    monkeypatch.setattr("agent.gym_media_index.probe_video", lambda p: None)
    store = FakeMediaStore(assets=[make_asset("v1", gym_id="pierce", kind="video",
                                             title="clip.mp4", mime="video/mp4")])
    drive = FakeDrive(blobs={"v1": b"vid"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "results", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    # No probe -> not staged (fail closed). Pool then exhausted -> None.
    assert draft is None


def test_tenant_assertion_blocks_cross_gym(monkeypatch, tmp_path):
    """A cross-gym asset that reaches the builder loop (e.g. a store bug slipped it
    past pick_media's own filter) is blocked by the stage-time assertion in the
    publish path, ops-alerted, and never staged (spec §1.5d)."""
    _wire(monkeypatch)
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append((k, m)) or True)
    # Force a foreign-gym asset straight into the builder loop, bypassing the
    # selector's own filter, so the builder's stage-time assertion is what runs.
    foreign = make_asset("x", gym_id="other_gym", kind="photo")
    monkeypatch.setattr("agent.gym_media_selector.pick_media",
                        lambda gym_id, kind_preference=None, store=None, now=None,
                        exclude_ids=(): foreign if "x" not in exclude_ids else None)
    store = FakeMediaStore()
    drive = FakeDrive(blobs={"x": b"jpg"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is None
    assert fired and any("tenant" in m.lower() for _, m in fired)


def test_selector_also_filters_cross_gym(monkeypatch, tmp_path):
    """Defense in depth: even before the builder's assertion, a leaky store's
    foreign asset is dropped by pick_media's own gym re-assertion (pool empty)."""
    _wire(monkeypatch)
    monkeypatch.setattr("agent.gym_media_index.dedup_alert", lambda k, m: True)

    class LeakyStore(FakeMediaStore):
        def list_assets(self, gym_id, source_id=None):
            return [make_asset("x", gym_id="other_gym", kind="photo")]

    store = LeakyStore()
    drive = FakeDrive(blobs={"x": b"jpg"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is None


def test_assert_tenant_helper():
    assert builder.assert_tenant({"id": "a", "gym_id": "pierce"}, "pierce") is True


def test_empty_pool_falls_through(monkeypatch, tmp_path):
    _wire(monkeypatch)
    monkeypatch.setattr("agent.gym_media_index.dedup_alert", lambda k, m: True)
    store = FakeMediaStore(assets=[])
    drive = FakeDrive()
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is None
