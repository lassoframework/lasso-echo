"""Writer opt-in must not turn converted Drive media into a raw asset claim."""

import threading
import time
import os
import sys
import hashlib
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, media_guard, media_swap, portal_social, visual_writer_prepare


def _candidate(source="local", kind="photo"):
    return {"source": source, "kind": kind, "key": "drive-id" if source == "drive" else "photo",
            "asset": {"id": "drive-id", "kind": kind, "title": "original.heic"}}


def _pick(monkeypatch, tmp_path, *, candidate=None, hosted=None, row=None, siblings=()):
    path = tmp_path / "original.jpg"
    path.write_bytes(b"original bytes")
    cand = candidate or _candidate()
    return media_swap.pick_replacement(
        "gym", row or {"id": "feed", "format": "feed", "caption": "caption"},
        store=None, library_path=str(tmp_path), candidates_fn=lambda *_: [cand],
        materialize_fn=lambda _: {"path": str(path), "hosted": hosted},
        host_fn=lambda _: "https://cdn/raw.jpg", poster_fn=lambda *a: "",
        siblings=siblings)


def test_converted_drive_rendition_fails_closed_under_writer_flag(
        monkeypatch, tmp_path):
    # The boolean from ensure_rendition says only whether this call converted;
    # a cache hit still has no source URL or verified render provenance.
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    out = _pick(monkeypatch, tmp_path, candidate=_candidate("drive"),
                hosted="https://cdn/rendition.jpg")
    assert out == {"ok": False, "reason": media_swap.REASON_ASSET_PREP,
                   "candidates_tried": 1}


def test_unproven_drive_rendition_does_not_hide_later_raw_candidate(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    source = tmp_path / "raw.jpg"
    source.write_bytes(b"raw photo")
    converted = _candidate("drive")
    local = _candidate("local")
    out = media_swap.pick_replacement(
        "gym", {"id": "feed", "format": "feed", "caption": "caption"},
        store=None, library_path=str(tmp_path),
        candidates_fn=lambda *_: [converted, local],
        materialize_fn=lambda cand: {"path": str(source),
                                     "hosted": "https://cdn/converted.jpg"
                                     if cand["source"] == "drive" else None},
        host_fn=lambda _path: "https://cdn/raw.jpg", poster_fn=lambda *a: "")
    assert out["ok"] is True
    assert out["source"] == "local"
    assert out["image_url"] == "https://cdn/raw.jpg"


def test_converted_drive_swap_keeps_existing_flag_off_behavior(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    out = _pick(monkeypatch, tmp_path, candidate=_candidate("drive"),
                hosted="https://cdn/rendition.jpg")
    assert out["ok"] is True
    assert out["image_url"] == "https://cdn/rendition.jpg"


def test_matching_remote_bytes_still_produce_render_evidence(monkeypatch):
    objects = {"https://cdn/raw.jpg": b"raw bytes",
               "https://cdn/rendered.jpg": b"rendered bytes"}
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", objects.get)
    evidence = media_swap._render_evidence(
        "https://cdn/raw.jpg", "https://cdn/rendered.jpg",
        objects["https://cdn/raw.jpg"], objects["https://cdn/rendered.jpg"],
        "render", deadline=media_swap._Deadline(1))
    assert evidence["source_fingerprint"] == "md5:" + hashlib.md5(b"raw bytes").hexdigest()
    assert evidence["delivered_byte_length"] == len(b"rendered bytes")


def test_prepared_video_swap_attests_poster_from_exact_hosted_video(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"local-video-is-not-the-source-of-truth")
    seen = {}
    poster_evidence = {"source_exact_url": "https://cdn/served.mp4",
                       "delivered_exact_url": "https://cdn/poster.jpg",
                       "operation": "render"}

    def poster_with_evidence(path, work, tenant, source_exact_url):
        seen.update(path=path, tenant=tenant, source_exact_url=source_exact_url)
        return "https://cdn/poster.jpg", poster_evidence

    monkeypatch.setattr("agent.gym_media_builder.video_poster_with_evidence",
                        poster_with_evidence)
    out = media_swap.pick_replacement(
        "gym", {"id": "feed", "format": "feed", "caption": "caption"},
        store=None, library_path=str(tmp_path),
        candidates_fn=lambda *_: [_candidate(kind="video")],
        materialize_fn=lambda _: {"path": str(source), "hosted": None},
        host_fn=lambda _: "https://cdn/served.mp4",
        poster_fn=lambda *a: (_ for _ in ()).throw(AssertionError("legacy poster path")),
        siblings=[{"id": "fb", "format": "feed", "caption": "caption"}])

    assert seen["source_exact_url"] == "https://cdn/served.mp4"
    assert out["thumbnail_url"] == "https://cdn/poster.jpg"
    assert out["poster_render_evidence"] == poster_evidence
    assert out["siblings"]["fb"]["poster_render_evidence"] == poster_evidence


def test_prepared_video_swap_holds_when_poster_evidence_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr("agent.gym_media_builder.video_poster_with_evidence",
                        lambda *a, **k: None)

    out = media_swap.pick_replacement(
        "gym", {"id": "feed", "format": "feed", "caption": "caption"},
        store=None, library_path=str(tmp_path),
        candidates_fn=lambda *_: [_candidate(kind="video")],
        materialize_fn=lambda _: {"path": str(source), "hosted": None},
        host_fn=lambda _: "https://cdn/served.mp4", poster_fn=lambda *a: "legacy.jpg")

    assert out == {"ok": False, "reason": media_swap.REASON_ASSET_PREP,
                   "candidates_tried": 1}


def test_prepared_story_only_video_swap_does_not_require_unused_poster(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "story_format_enabled", lambda: False)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    calls = []

    def unexpected_poster(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr("agent.gym_media_builder.video_poster_with_evidence",
                        unexpected_poster)
    out = media_swap.pick_replacement(
        "gym", {"id": "story", "format": "story", "caption": "caption"},
        store=None, library_path=str(tmp_path),
        candidates_fn=lambda *_: [_candidate(kind="video")],
        materialize_fn=lambda _: {"path": str(source), "hosted": None},
        host_fn=lambda _: "https://cdn/served.mp4", poster_fn=lambda *a: "legacy.jpg")

    assert out["ok"] is True
    assert out["image_url"] == "https://cdn/served.mp4"
    assert out["thumbnail_url"] == ""
    assert "poster_render_evidence" not in out
    assert calls == []


def test_video_swap_keeps_legacy_poster_path_when_writer_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: False)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr("agent.gym_media_builder.video_poster_with_evidence",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prepared path")))

    out = media_swap.pick_replacement(
        "gym", {"id": "feed", "format": "feed", "caption": "caption"},
        store=None, library_path=str(tmp_path),
        candidates_fn=lambda *_: [_candidate(kind="video")],
        materialize_fn=lambda _: {"path": str(source), "hosted": None},
        host_fn=lambda _: "https://cdn/served.mp4",
        poster_fn=lambda *a: "https://cdn/legacy-poster.jpg")

    assert out["thumbnail_url"] == "https://cdn/legacy-poster.jpg"
    assert "poster_render_evidence" not in out


@pytest.mark.parametrize("mismatch", [
    "source_url", "delivered_url", "source_hash", "delivered_hash",
    "source_bytes", "delivered_bytes",
])
def test_portal_rejects_mismatched_poster_before_reservation(monkeypatch, mismatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    monkeypatch.setattr(portal_social, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(portal_social, "_published_is_final", lambda *a: None)
    monkeypatch.setattr(portal_social, "_month_rows_for", lambda *a: [])
    monkeypatch.setattr(media_swap, "enabled", lambda: True)
    monkeypatch.setattr(portal_social.config, "portal_calendar_supabase_enabled",
                        lambda: True)
    # Original-source identity is covered by test_ordinary_swap_proof; isolate
    # the poster evidence gate so each malformed poster variant reaches it.
    monkeypatch.setattr(media_guard, "swap_original_identity",
                        lambda account, row, store, *, pick=None:
                        {"sha256": "b" * 64 if pick else "a" * 64,
                         "source_asset_id": None,
                         "source_url": (pick or row).get("source_media_url")})

    image_url = "https://cdn/clip.mp4"
    poster_url = "https://cdn/poster.jpg"
    source_bytes = b"selected hosted video bytes"
    poster_bytes = b"rendered poster bytes"
    objects = {image_url: source_bytes, poster_url: poster_bytes}
    evidence = {
        "source_exact_url": image_url,
        "delivered_exact_url": poster_url,
        "source_fingerprint": "md5:" + hashlib.md5(source_bytes).hexdigest(),
        "delivered_fingerprint": "md5:" + hashlib.md5(poster_bytes).hexdigest(),
        "source_byte_length": len(source_bytes),
        "delivered_byte_length": len(poster_bytes),
        "operation": "render",
    }
    if mismatch == "source_url":
        evidence["source_exact_url"] = "https://cdn/other.mp4"
    elif mismatch == "delivered_url":
        evidence["delivered_exact_url"] = "https://cdn/other-poster.jpg"
    elif mismatch == "source_hash":
        evidence["source_fingerprint"] = "md5:" + "0" * 32
    elif mismatch == "delivered_hash":
        evidence["delivered_fingerprint"] = "md5:" + "0" * 32
    elif mismatch == "source_bytes":
        objects[image_url] = b"changed hosted video bytes"
    elif mismatch == "delivered_bytes":
        objects[poster_url] = b"changed poster bytes"
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", objects.get)

    pick = {
        "ok": True, "image_url": image_url, "source_media_url": image_url,
        "thumbnail_url": poster_url, "poster_render_evidence": evidence,
        "kind": "video", "source": "local", "key": "clip.mp4",
        "source_media_asset_id": "", "siblings": {},
    }
    reserve_calls = []
    monkeypatch.setattr(
        media_swap, "reserve_local_pick",
        lambda *a, **k: reserve_calls.append((a, k)) or True)

    class Store:
        row = {"id": "p1", "gym_id": "gym", "status": "pending",
               "format": "feed", "image_url": "https://cdn/old.jpg",
               "post_date": "2026-10-03", "account": "instagram",
               "caption": "kept caption"}

        def get_row(self, account, row_id):
            return dict(self.row) if account == "gym" and row_id == "p1" else None

        def list_month(self, account, month):
            return [dict(self.row)]

        def swap_media(self, *args, **kwargs):
            raise AssertionError("invalid poster proof reached the calendar writer")

    status, body = portal_social.handle_swap_media(
        "gym", "p1", "actor", sb_store=Store(), picker=lambda *a, **k: pick)

    assert status == 409
    assert body["reason"] == "poster_evidence_unavailable"
    assert reserve_calls == []


def test_remote_feed_evidence_read_stops_at_request_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(media_swap, "SWAP_REQUEST_DEADLINE_SEC", 0.04)
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: True)
    from agent import feed_image, media_host
    rendered = tmp_path / "rendered.jpg"
    rendered.write_bytes(b"rendered bytes")
    monkeypatch.setattr(feed_image, "get_or_make_feed_image", lambda *a, **k: str(rendered))
    monkeypatch.setattr(media_host, "host_media", lambda *a, **k: "https://cdn/rendered.jpg")
    release = threading.Event()
    entered = threading.Event()

    def slow_read(_url):
        entered.set()
        release.wait(1)
        return b"rendered bytes"

    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", slow_read)
    start = time.monotonic()
    try:
        out = _pick(monkeypatch, tmp_path)
    finally:
        release.set()
    assert entered.is_set()
    assert out == {"ok": False, "reason": media_swap.REASON_TIMEOUT}
    assert time.monotonic() - start < 0.5


def test_story_sibling_remote_read_uses_remaining_request_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(media_swap, "SWAP_REQUEST_DEADLINE_SEC", 0.04)
    monkeypatch.setattr(config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    from agent import story_image, media_host
    rendered = tmp_path / "burned.mp4"
    rendered.write_bytes(b"burned bytes")
    monkeypatch.setattr(story_image, "get_or_make_story_video", lambda *a, **k: str(rendered))
    monkeypatch.setattr(media_host, "host_media", lambda *a, **k: "https://cdn/burned.mp4")
    # Keep this test on its intended story-evidence deadline path. Poster evidence
    # has its own focused guarded-success/failure coverage above.
    monkeypatch.setattr(
        "agent.gym_media_builder.video_poster_with_evidence",
        lambda *a, **k: ("https://cdn/poster.jpg", {
            "source_exact_url": "https://cdn/raw.jpg",
            "delivered_exact_url": "https://cdn/poster.jpg", "operation": "render"}))
    entered = threading.Event()
    release = threading.Event()

    def slow_read(_url):
        entered.set()
        release.wait(1)
        return b"original bytes"

    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", slow_read)
    start = time.monotonic()
    try:
        out = _pick(monkeypatch, tmp_path, candidate=_candidate(kind="video"),
                    row={"id": "feed", "format": "feed"},
                    siblings=[{"id": "story", "format": "story", "caption": "caption"}])
    finally:
        release.set()
    assert entered.is_set()
    assert out == {"ok": False, "reason": media_swap.REASON_TIMEOUT}
    assert time.monotonic() - start < 0.5
