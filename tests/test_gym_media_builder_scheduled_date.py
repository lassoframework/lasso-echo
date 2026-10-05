"""The builder must give scene selection the scheduled slot date."""
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_builder as builder  # noqa: E402
from tests.gym_media_fakes import FakeDrive, FakeMediaStore, make_asset  # noqa: E402


class _Account:
    key = "pierce_ig"
    platform = "instagram"


def _wire_stage(monkeypatch):
    monkeypatch.setattr("agent.vision.analyze_and_store", lambda *args, **kwargs: {
        "version": 2, "quality": {"usable": True}, "safety_flags": [],
        "one_line": "members training together"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda analysis: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify", lambda *args, **kwargs: {
        "ok": True, "bucket": "small_group", "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *args, **kwargs: ("Grounded caption", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda *args, **kwargs: "https://cdn.fake/served.jpg")


def test_builder_passes_scheduled_day_to_pick_media(monkeypatch, tmp_path):
    _wire_stage(monkeypatch)
    picked = make_asset("photo-1", gym_id="pierce", kind="photo")
    seen = []
    pool_seen = []

    def pool_kinds(*args, **kwargs):
        pool_seen.append(kwargs["post_date"])
        return {"photo"}

    monkeypatch.setattr("agent.gym_media_selector.pool_kinds", pool_kinds)

    def pick_media(*args, **kwargs):
        seen.append(kwargs["post_date"])
        return picked if len(seen) == 1 else None

    monkeypatch.setattr("agent.gym_media_selector.pick_media", pick_media)
    draft = builder.build_gym_media_draft(
        _Account(), "2026-10-11", "faces", voice=object(), source=object(),
        store=FakeMediaStore(), drive=FakeDrive(blobs={"photo-1": b"jpg"}),
        library_dir=str(tmp_path))

    assert draft is None  # The isolated pick fixture deliberately has no source row.
    assert pool_seen == ["2026-10-11"]
    assert seen and set(seen) == {"2026-10-11"}


def test_rendition_limited_video_pick_uses_scheduled_day(monkeypatch, tmp_path):
    _wire_stage(monkeypatch)
    picked = make_asset("video-1", gym_id="pierce", kind="video")
    picked["rendition_url"] = "https://cdn.fake/video.mp4"
    seen = []

    monkeypatch.setattr("agent.gym_media_selector.pool_kinds",
                        lambda *args, **kwargs: {"video"})
    monkeypatch.setattr("agent.gym_media_selector.pick_media",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("budget-limited video must use pickable")))

    def pickable(*args, **kwargs):
        seen.append(kwargs["post_date"])
        return [picked]

    monkeypatch.setattr("agent.gym_media_selector.pickable", pickable)
    budget = builder._idx.RenditionBudget(0)
    draft = builder.build_gym_media_draft(
        _Account(), "2026-10-12", "results", voice=object(), source=object(),
        store=FakeMediaStore(), drive=FakeDrive(blobs={"video-1": b"video"}),
        library_dir=str(tmp_path), rendition_budget=budget, kind_prefs=("video",))

    assert draft is None  # Routing is asserted before stage-time source validation.
    assert seen and set(seen) == {"2026-10-12"}
