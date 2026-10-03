"""Writer opt-in must not turn converted Drive media into a raw asset claim."""

import threading
import time
import os
import sys
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, media_swap, visual_writer_prepare


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
