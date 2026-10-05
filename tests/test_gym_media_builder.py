"""gym_media_drive §7: every staged row PENDING, HEIC stages via rendition,
unprobed video never stages, tenant assertion blocks a cross-gym asset."""
import os
import sys
import hashlib
from io import BytesIO

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_builder as builder  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


class _Acct:
    key = "pierce_ig"
    platform = "instagram"


def _wire(monkeypatch, analysis=None):
    """Stub vision + caption + hosting so the builder path is exercised offline."""
    # `alert=` is REQUIRED by this lane (audit item 5): without it the per-gym monthly
    # vision cap can refuse but never speak, which is how gritx sat silently at 400/400.
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None, alert=None: analysis or {
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


def _jpeg_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="JPEG")
    return output.getvalue()


def test_video_poster_with_evidence_renders_from_exact_hosted_video_bytes(monkeypatch, tmp_path):
    source_url = "https://media.example/echo/pierce/source/clip.mp4"
    delivered_url = "https://media.example/echo/pierce/poster/poster.jpg"
    source_bytes = b"exact-hosted-video-bytes"
    poster_bytes = _jpeg_bytes()
    reads = {source_url: source_bytes, delivered_url: poster_bytes}
    seen = {}

    monkeypatch.setattr(builder.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url", lambda url: reads.get(url))

    def frame(path, out):
        seen["source_bytes"] = open(path, "rb").read()
        with open(out, "wb") as fh:
            fh.write(poster_bytes)

    monkeypatch.setattr("agent.action_reel.poster_frame", frame)
    monkeypatch.setattr("agent.media_host.host_media", lambda path, tenant: delivered_url)

    result = builder.video_poster_with_evidence(
        tmp_path / "untrusted-local.mp4", tmp_path, "pierce", source_url)

    assert result is not None
    url, evidence = result
    assert url == delivered_url
    assert seen["source_bytes"] == source_bytes
    assert evidence["source_exact_url"] == source_url
    assert evidence["delivered_exact_url"] == delivered_url
    assert evidence["source_fingerprint"] == "md5:" + hashlib.md5(source_bytes).hexdigest()
    assert evidence["delivered_fingerprint"] == "md5:" + hashlib.md5(poster_bytes).hexdigest()
    assert evidence["source_byte_length"] == len(source_bytes)
    assert evidence["delivered_byte_length"] == len(poster_bytes)
    assert evidence["operation"] == "render"
    assert evidence["rendered_by"] == "gym_media_builder.video_poster_with_evidence"
    assert evidence["evidence_ref"].startswith("gym_media_builder:poster_render:")


def test_video_poster_with_evidence_fails_closed_when_hosted_poster_differs(monkeypatch, tmp_path):
    source_url = "https://media.example/echo/pierce/source/clip.mp4"
    delivered_url = "https://media.example/echo/pierce/poster/poster.jpg"
    local_poster = _jpeg_bytes()
    changed_poster = _jpeg_bytes() + b"changed"
    monkeypatch.setattr(builder.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url",
                        lambda url: {source_url: b"source", delivered_url: changed_poster}.get(url))
    monkeypatch.setattr("agent.action_reel.poster_frame",
                        lambda _path, out: open(out, "wb").write(local_poster))
    monkeypatch.setattr("agent.media_host.host_media", lambda path, tenant: delivered_url)

    assert builder.video_poster_with_evidence(tmp_path / "clip.mp4", tmp_path,
                                              "pierce", source_url) is None


def test_video_poster_with_evidence_rejects_magic_prefixed_junk(monkeypatch, tmp_path):
    source_url = "https://media.example/echo/pierce/source/clip.mp4"
    monkeypatch.setattr(builder.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url", lambda url: b"source")
    monkeypatch.setattr("agent.action_reel.poster_frame",
                        lambda _path, out: open(out, "wb").write(b"\xff\xd8\xffjunk"))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("must not host")))

    assert builder.video_poster_with_evidence(tmp_path / "clip.mp4", tmp_path,
                                              "pierce", source_url) is None


def test_video_poster_with_evidence_rejects_oversized_local_poster(monkeypatch, tmp_path):
    source_url = "https://media.example/echo/pierce/source/clip.mp4"
    monkeypatch.setattr(builder.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url", lambda url: b"source")
    monkeypatch.setattr("agent.visual_writer_prepare.MAX_VISUAL_BYTES", 1)
    monkeypatch.setattr("agent.action_reel.poster_frame",
                        lambda _path, out: open(out, "wb").write(_jpeg_bytes()))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("must not host")))

    assert builder.video_poster_with_evidence(tmp_path / "clip.mp4", tmp_path,
                                              "pierce", source_url) is None


def test_video_poster_with_evidence_removes_source_temp_after_write_failure(monkeypatch, tmp_path):
    source_url = "https://media.example/echo/pierce/source/clip.mp4"
    allocated = tmp_path / "poster-source-write-fail.mp4"
    allocated.write_bytes(b"partial")

    class FailingSourceFile:
        name = str(allocated)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, _data):
            raise OSError("write failed")

    monkeypatch.setattr(builder.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url", lambda url: b"source")
    monkeypatch.setattr(builder.tempfile, "NamedTemporaryFile", lambda **_kwargs: FailingSourceFile())

    assert builder.video_poster_with_evidence(tmp_path / "clip.mp4", tmp_path,
                                              "pierce", source_url) is None
    assert not allocated.exists()


def test_video_poster_with_evidence_is_off_when_hosting_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(builder.config, "hosting_enabled", lambda: False)
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url",
                        lambda _url: (_ for _ in ()).throw(AssertionError("must not read")))

    assert builder.video_poster_with_evidence(tmp_path / "clip.mp4", tmp_path, "pierce",
                                              "https://media.example/source.mp4") is None


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
    assert draft.source_media_url == "https://cdn.fake/served.jpg"
    # usage was stamped at stage time.
    assert store.assets["p1"]["used_count"] == 1


def test_builder_loses_claim_race_after_select(monkeypatch, tmp_path):
    from agent import db, gym_media_selector
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    _wire(monkeypatch)
    store = FakeMediaStore(assets=[make_asset("p1", gym_id="pierce", kind="photo")])
    drive = FakeDrive(blobs={"p1": b"jpgbytes"})
    original = db.socialapi_claim

    def concurrent_gbp_claim(claim_id, account_key):
        original(claim_id, account_key)
        return original(claim_id, account_key)

    monkeypatch.setattr(db, "socialapi_claim", concurrent_gbp_claim)
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is None
    assert store.assets["p1"]["used_count"] == 0
    assert gym_media_selector.pickable("pierce", store=store) == []


def test_usage_stamp_failure_holds_before_returning_a_durable_draft(monkeypatch,
                                                                    tmp_path):
    _wire(monkeypatch)
    store = FakeMediaStore(assets=[make_asset("p1", gym_id="pierce", kind="photo")])
    drive = FakeDrive(blobs={"p1": b"jpgbytes"})
    alerts = []
    monkeypatch.setattr("agent.gym_media_selector.stamp_use",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("stamp down")))
    monkeypatch.setattr(builder, "_vision_alert", lambda msg: alerts.append(msg))
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is None
    assert alerts and "held" in alerts[0]


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
    assert draft.source_media_url == "https://cdn.fake/served.jpg"
    assert draft.source_media_url != draft.creative_public_url


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
                        exclude_ids=(), post_date=None: foreign if "x" not in exclude_ids else None)
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


def test_exclude_ids_keeps_a_caller_named_asset_out_of_the_pick(monkeypatch, tmp_path):
    """Independent audit, 2026-09-08: build_gym_media_draft had no way for a caller
    to say "never this specific asset" -- only its own per-call retry list. Without
    it, a denied-slot backfill (client_month_run.backfill_denied_slots) could hand
    the SAME Drive asset right back as its own "fresh" replacement (its used_count
    is reset by gym_media_selector.rollback_use the moment it's denied, making it
    the pool's least-used candidate again)."""
    _wire(monkeypatch)
    store = FakeMediaStore(assets=[
        make_asset("denied_one", gym_id="pierce", kind="photo"),
        make_asset("fresh_two", gym_id="pierce", kind="photo"),
    ])
    drive = FakeDrive(blobs={"denied_one": b"jpgbytes", "fresh_two": b"jpgbytes"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path),
        exclude_ids=("denied_one",))
    assert draft is not None
    assert store.assets["fresh_two"]["used_count"] == 1
    assert store.assets["denied_one"]["used_count"] == 0, (
        "the excluded asset must never be picked or stamped used"
    )


# ---- vision allowlist gate (vision_allowlist_watch drift report, 2026-09) --------------

def test_vision_never_called_for_a_gym_not_on_the_allowlist(monkeypatch, tmp_path):
    """The exact live drift: AGENT_VISION_GYMS gates every other vision caller, but
    this Drive lane called vision.analyze_and_store unconditionally -- confirmed live,
    real gyms (crossfitlocal, crossfitreverb30b5b2, hillcountry, zanshinfitness630e22,
    others) burning vision spend despite never being armed. A gym not on the allowlist
    must get zero vision calls, and still stage successfully (ungrounded caption)."""
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)   # pierce is armed nowhere
    calls = []
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None, alert=None: calls.append(gym) or {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "three people at the gym"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: (_ for _ in ()).throw(
                            AssertionError("crop_verify must not run without an analysis")))
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("An ungrounded caption, no vision analysis", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, gym: "https://cdn.fake/served.jpg")

    store = FakeMediaStore(assets=[make_asset("p1", gym_id="pierce", kind="photo")])
    drive = FakeDrive(blobs={"p1": b"jpgbytes"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))

    assert calls == [], "vision.analyze_and_store must never be called for an unarmed gym"
    assert draft is not None, "the slot must still stage, just without vision grounding"
    assert draft.status == DraftStatus.PENDING
    assert draft.caption == "An ungrounded caption, no vision analysis"


def test_vision_still_called_for_a_gym_on_the_allowlist(monkeypatch, tmp_path):
    """Regression guard for the fix above: an ARMED gym must keep getting vision
    exactly as before -- the allowlist gate must not accidentally turn OFF vision
    for gyms that are supposed to have it."""
    monkeypatch.setenv("AGENT_VISION_GYMS", "pierce")
    calls = []
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None, alert=None: calls.append(gym) or {
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

    store = FakeMediaStore(assets=[make_asset("p1", gym_id="pierce", kind="photo")])
    drive = FakeDrive(blobs={"p1": b"jpgbytes"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))

    assert calls == ["pierce"], "an armed gym must still get vision, exactly as before"
    assert draft is not None
    assert draft.status == DraftStatus.PENDING
