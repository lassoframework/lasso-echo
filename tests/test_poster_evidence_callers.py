"""AGENT_VISUAL_GLOBAL_WRITER_PREP poster callers (bounded package 2026-10-03).

Drive (gym_media_builder) and podcast (podcast_library_builder) video poster
creators: under the flag the poster must be rendered by
``video_poster_with_evidence`` from the EXACT hosted video URL, the byte-bound
receipt rides the draft as the NON-DB side-channel attribute
``draft.poster_render_evidence``, and an evidence failure HOLDS the slot.
Flag OFF keeps the legacy ``video_poster_url`` best-effort path unchanged.
Offline only: every byte read/host/upload is faked.
"""
import os
import sys
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_builder as gym_builder                       # noqa: E402
from agent import podcast_library_builder as pod_builder                 # noqa: E402
from agent import visual_writer_prepare                                  # noqa: E402
from agent.drafter import DraftStatus                                    # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402
from tests.podcast_fakes import (FakeDrive as PodFakeDrive, FakeStore,    # noqa: E402
                                 FakeZernio, NOTES_DOC_TEXT, make_asset as
                                 make_clip)

GYM_VIDEO_URL = "https://cdn.fake/pierce/video/clip.mp4"
GYM_POSTER_URL = "https://cdn.fake/pierce/poster/poster.jpg"
SOURCE_BYTES = b"exact-hosted-video-bytes"


def _jpeg_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="JPEG")
    return output.getvalue()


POSTER_BYTES = _jpeg_bytes()


def _wire_gym_video(monkeypatch, delivered_mismatch=False):
    """Stub everything around the Drive builder's video path."""
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None, alert=None: {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "people at the gym"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption", []))
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: {"duration_sec": 12.0, "width": 1920,
                                   "height": 1080})
    monkeypatch.setattr("agent.gym_media_index.video_eligibility",
                        lambda *a, **k: (True, "", ""))
    monkeypatch.setattr("agent.gym_media_index.needs_rendition",
                        lambda asset, info: False)
    monkeypatch.setattr("agent.gym_media_index.ensure_rendition",
                        lambda asset, src, **k: ("", False))
    monkeypatch.setattr(gym_builder.config, "hosting_enabled", lambda: True)
    # media_host serves the VIDEO url for .mp4 inputs and the POSTER url for
    # .jpg inputs; both are then read back byte-exact by the evidence path.
    monkeypatch.setattr(
        "agent.media_host.host_media",
        lambda path, tenant: (GYM_VIDEO_URL if str(path).endswith(".mp4")
                              else GYM_POSTER_URL))

    reads = {GYM_VIDEO_URL: SOURCE_BYTES,
             GYM_POSTER_URL: (POSTER_BYTES + b"tampered") if delivered_mismatch
             else POSTER_BYTES}
    monkeypatch.setattr("agent.visual_writer_prepare._bytes_for_url",
                        lambda url: reads.get(url))

    seen = {}

    def frame(path, out):
        seen["source_bytes"] = open(path, "rb").read()
        with open(out, "wb") as fh:
            fh.write(POSTER_BYTES)

    monkeypatch.setattr("agent.action_reel.poster_frame", frame)
    return seen


def _gym_video_draft(monkeypatch, tmp_path):
    store = FakeMediaStore(assets=[make_asset(
        "v9", gym_id="pierce", kind="video", title="clip.mp4",
        mime="video/mp4")])
    drive = FakeDrive(blobs={"v9": b"local-download-bytes"})
    draft = gym_builder.build_gym_media_draft(
        SimpleNamespace(key="pierce_ig", platform="instagram"),
        "2026-10-03", "results", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    return draft, store


def test_drive_video_poster_uses_evidence_from_exact_hosted_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    seen = _wire_gym_video(monkeypatch)
    draft, store = _gym_video_draft(monkeypatch, tmp_path)

    assert draft is not None and draft.status == DraftStatus.PENDING
    # the poster ffmpeg input was the EXACT hosted video bytes, not the local
    # download (which differs: b"local-download-bytes").
    assert seen["source_bytes"] == SOURCE_BYTES
    assert draft.thumbnail_url == GYM_POSTER_URL
    # the served media is the same exact hosted video the poster attests.
    assert draft.creative_public_url == GYM_VIDEO_URL
    assert draft.source_media_url == GYM_VIDEO_URL
    # NON-DB side-channel receipt rides the draft object.
    evidence = getattr(draft, "poster_render_evidence", None)
    assert evidence is not None
    assert evidence["source_exact_url"] == GYM_VIDEO_URL
    assert evidence["delivered_exact_url"] == GYM_POSTER_URL
    assert evidence["operation"] == "render"
    assert store.assets["v9"]["used_count"] == 1


def test_drive_video_holds_when_poster_evidence_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    _wire_gym_video(monkeypatch, delivered_mismatch=True)
    draft, store = _gym_video_draft(monkeypatch, tmp_path)

    # Evidence failed: the slot is HELD, no durable draft, no usage stamp.
    assert draft is None
    assert store.assets["v9"]["used_count"] == 0


def test_drive_video_flag_off_keeps_legacy_best_effort_poster(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None, alert=None: {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "people at the gym"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption", []))
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: {"duration_sec": 12.0, "width": 1920,
                                   "height": 1080})
    monkeypatch.setattr("agent.gym_media_index.video_eligibility",
                        lambda *a, **k: (True, "", ""))
    monkeypatch.setattr("agent.gym_media_index.needs_rendition",
                        lambda asset, info: False)
    monkeypatch.setattr("agent.gym_media_index.ensure_rendition",
                        lambda asset, src, **k: ("", False))
    monkeypatch.setattr(gym_builder.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, tenant: "https://cdn.fake/served.jpg")
    # Legacy path reads ONLY the local file; it never touches hosted bytes.
    monkeypatch.setattr(
        "agent.visual_writer_prepare._bytes_for_url",
        lambda url: (_ for _ in ()).throw(AssertionError("must not read hosted bytes")))
    monkeypatch.setattr("agent.action_reel.poster_frame",
                        lambda path, out: open(out, "wb").write(POSTER_BYTES))

    draft, store = _gym_video_draft(monkeypatch, tmp_path)

    assert draft is not None and draft.status == DraftStatus.PENDING
    assert draft.thumbnail_url == "https://cdn.fake/served.jpg"
    assert not hasattr(draft, "poster_render_evidence")
    assert store.assets["v9"]["used_count"] == 1


# ---- podcast caller ---------------------------------------------------------

POD_PROVIDER_VIDEO_URL = "https://zernio-cdn.fake/pod/clips/clip140s1.mp4"
POD_OWN_BASE_URL = "https://r2.echo.test/media"
POD_OWN_VIDEO_URL = POD_OWN_BASE_URL + "/echo/lasso/source/clip140s1.mp4"
POD_POSTER_URL = POD_OWN_BASE_URL + "/echo/lasso/posters/poster.jpg"

_ACCT = SimpleNamespace(key="lasso_ig", platform="instagram")


def _probe_ok(path):
    return {"duration_sec": 42.0, "width": 1080, "height": 1920}


def _wire_podcast_poster(monkeypatch, delivered_mismatch=False,
                         source_mismatch=False):
    monkeypatch.setattr(pod_builder.config, "hosting_enabled", lambda: True)
    zc = FakeZernio()
    # Zernio returns a provider URL outside the writer's configured media origin.
    monkeypatch.setattr(zc, "media_generate_upload_link",
                        lambda filename, content_type: {
                            "uploadUrl": f"https://upload.fake/{filename}",
                            "publicUrl": POD_PROVIDER_VIDEO_URL})
    monkeypatch.setattr(pod_builder.config, "S3_PUBLIC_BASE_URL", POD_OWN_BASE_URL)
    seen = {}

    # The fake upload populates the fake object store from the actual local
    # clip. A mismatch deliberately corrupts that object after upload.
    objects = {
        "echo/lasso/posters/poster.jpg": (
            POSTER_BYTES + b"tampered" if delivered_mismatch else POSTER_BYTES),
    }

    def host(path, tenant):
        if str(path).endswith(".mp4"):
            with open(path, "rb") as clip_file:
                seen["hosted_video_bytes"] = clip_file.read()
            objects["echo/lasso/source/clip140s1.mp4"] = (
                b"wrong-hosted-video" if source_mismatch
                else seen["hosted_video_bytes"])
            return POD_OWN_VIDEO_URL
        return POD_POSTER_URL

    monkeypatch.setattr("agent.media_host.host_media", host)

    # Exercise the real _own_media_url/_bytes_for_url boundary. The fake S3 client
    # supplies the own-host objects; an external Zernio URL cannot reach it.
    class _S3:
        def get_object(self, Bucket, Key):
            data = objects[Key]
            return {"Body": BytesIO(data), "ContentLength": len(data)}

    client = SimpleNamespace(_s3=_S3(), _bucket="test-bucket")
    monkeypatch.setattr("agent.media_host._default_client", lambda: client)

    def frame(path, out):
        seen["source_bytes"] = open(path, "rb").read()
        with open(out, "wb") as fh:
            fh.write(POSTER_BYTES)

    monkeypatch.setattr("agent.action_reel.poster_frame", frame)
    return seen, zc


def _pod_draft(monkeypatch, zc):
    store = FakeStore([make_clip()])
    drive = PodFakeDrive(docs={"doc140": NOTES_DOC_TEXT},
                         blobs={"clip140s1": SOURCE_BYTES})
    return pod_builder.build_podcast_clip_draft(
        _ACCT, "2026-10-03", store=store, drive=drive, zernio_client=zc,
        probe_fn=_probe_ok, feed_map={}), store


def test_podcast_video_poster_uses_evidence_from_exact_hosted_url(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    seen, zc = _wire_podcast_poster(monkeypatch)
    draft, store = _pod_draft(monkeypatch, zc)

    assert draft is not None and draft.status == DraftStatus.PENDING
    assert visual_writer_prepare._own_media_url(POD_PROVIDER_VIDEO_URL) is False
    assert visual_writer_prepare._own_media_url(POD_OWN_VIDEO_URL) is True
    assert draft.creative_public_url == POD_OWN_VIDEO_URL
    assert draft.zernio_public_url == POD_PROVIDER_VIDEO_URL
    assert seen["hosted_video_bytes"] == SOURCE_BYTES
    # ffmpeg consumed the exact hosted clip bytes, not the local download.
    assert seen["source_bytes"] == SOURCE_BYTES
    assert draft.thumbnail_url == POD_POSTER_URL
    evidence = getattr(draft, "poster_render_evidence", None)
    assert evidence is not None
    assert evidence["source_exact_url"] == POD_OWN_VIDEO_URL
    assert evidence["delivered_exact_url"] == POD_POSTER_URL
    assert store.assets["clip140s1"]["used_count"] == 1


def test_podcast_video_holds_when_poster_evidence_fails(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    _, zc = _wire_podcast_poster(monkeypatch, delivered_mismatch=True)
    draft, store = _pod_draft(monkeypatch, zc)

    assert draft is None
    assert store.assets["clip140s1"]["used_count"] == 0


def test_podcast_video_holds_when_own_host_changes_source_bytes(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    _, zc = _wire_podcast_poster(monkeypatch, source_mismatch=True)
    draft, store = _pod_draft(monkeypatch, zc)

    assert draft is None
    assert store.assets["clip140s1"]["used_count"] == 0


def test_podcast_video_flag_off_keeps_legacy_best_effort_poster(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.setattr(pod_builder.config, "hosting_enabled", lambda: True)
    zc = FakeZernio()
    # The EXACT hosted clip URL is the presign publicUrl; status check only
    # confirms readiness and its return value is discarded by _upload_clip.
    monkeypatch.setattr(zc, "media_generate_upload_link",
                        lambda filename, content_type: {
                            "uploadUrl": f"https://upload.fake/{filename}",
                            "publicUrl": POD_PROVIDER_VIDEO_URL})
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, tenant: POD_POSTER_URL)
    monkeypatch.setattr(
        "agent.visual_writer_prepare._bytes_for_url",
        lambda url: (_ for _ in ()).throw(AssertionError("must not read hosted bytes")))
    monkeypatch.setattr("agent.action_reel.poster_frame",
                        lambda path, out: open(out, "wb").write(POSTER_BYTES))

    draft, store = _pod_draft(monkeypatch, zc)

    assert draft is not None and draft.status == DraftStatus.PENDING
    assert draft.creative_public_url == POD_PROVIDER_VIDEO_URL
    assert draft.thumbnail_url == POD_POSTER_URL
    assert not hasattr(draft, "poster_render_evidence")
    assert store.assets["clip140s1"]["used_count"] == 1
