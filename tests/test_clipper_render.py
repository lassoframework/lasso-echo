"""
Phase 2 render tests: cut, frame, captions, brand frame.
These tests run ffmpeg for real (tiny synthetic fixtures, ~2-3s each).
The whole module is skipped when ffmpeg is absent.
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import clipper_render, config  # noqa: E402

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg not found"
)


# ---- shared fixtures ----------------------------------------------------------------

def _make_test_video(path, duration=3, w=320, h=240):
    """Create a tiny synthetic video with a blue background and a 440 Hz tone."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s={w}x{h}:r=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(duration),
        "-c:v", "libx264", "-c:a", "aac",
        path,
    ], capture_output=True, check=True)


def _make_test_audio(path, duration=3):
    """Create a tiny synthetic audio file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(duration),
        "-c:a", "aac",
        path,
    ], capture_output=True, check=True)


def _probe_streams(path):
    """Return list of stream dicts from ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True)
    try:
        return json.loads(result.stdout).get("streams", [])
    except Exception:
        return []


def _probe_dims(path):
    """Return (width, height) of the first video stream."""
    for s in _probe_streams(path):
        if s.get("codec_type") == "video":
            return s.get("width"), s.get("height")
    return None, None


def _probe_duration(path):
    """Return duration in seconds from the first stream that has one."""
    for s in _probe_streams(path):
        d = s.get("duration")
        if d:
            return float(d)
    return None


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED", "true")


def _fake_transcript(start_offset=0.0):
    """Short word-level transcript for caption tests."""
    words = [
        {"word": "Most",    "start": start_offset + 0.0, "end": start_offset + 0.3},
        {"word": "gyms",    "start": start_offset + 0.3, "end": start_offset + 0.7},
        {"word": "ignore",  "start": start_offset + 0.7, "end": start_offset + 1.1},
        {"word": "follow",  "start": start_offset + 1.1, "end": start_offset + 1.5},
        {"word": "up",      "start": start_offset + 1.5, "end": start_offset + 2.0},
    ]
    return {"words": words, "segments": []}


# ---- Part 5: cut_segment ------------------------------------------------------------

class TestCutSegment:
    def test_cut_produces_output_file(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src, duration=4)
        out_dir = str(tmp_path / "clips")
        result = clipper_render.cut_segment(src, 0.5, 2.5, out_dir)
        assert os.path.isfile(result)
        assert os.path.getsize(result) > 0

    def test_cut_duration_within_range(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src, duration=5)
        out_dir = str(tmp_path / "clips")
        result = clipper_render.cut_segment(src, 1.0, 3.0, out_dir)
        d = _probe_duration(result)
        assert d is not None
        # Stream-copy starts at the nearest I-frame before the seek point; with
        # a long GOP (default libx264) the actual duration can be larger than
        # requested. Verify it's non-trivial and not longer than the full source.
        assert d > 0.5
        assert d < 5.5

    def test_cut_raises_when_render_flag_off(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_CLIPPER_RENDER_ENABLED", raising=False)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src)
        with pytest.raises(clipper_render.RenderError, match="render is OFF"):
            clipper_render.cut_segment(src, 0.0, 1.0, str(tmp_path / "clips"))

    def test_cut_raises_when_ffmpeg_absent(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        monkeypatch.setattr(shutil, "which", lambda _: None)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src)
        with pytest.raises(clipper_render.RenderError, match="ffmpeg not found"):
            clipper_render.cut_segment(src, 0.0, 1.0, str(tmp_path / "clips"))


# ---- Part 6: frame_vertical ---------------------------------------------------------

class TestFrameVertical:
    def test_video_input_produces_1080x1920(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src, duration=2)
        out = str(tmp_path / "framed.mp4")
        clipper_render.frame_vertical(src, out, media_kind="video")
        w, h = _probe_dims(out)
        assert w == clipper_render.REEL_W, f"expected width {clipper_render.REEL_W}, got {w}"
        assert h == clipper_render.REEL_H, f"expected height {clipper_render.REEL_H}, got {h}"

    def test_audio_input_produces_1080x1920_audiogram(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "ep.m4a")
        _make_test_audio(src, duration=2)
        out = str(tmp_path / "audiogram.mp4")
        clipper_render.frame_vertical(src, out, media_kind="audio")
        w, h = _probe_dims(out)
        assert w == clipper_render.REEL_W
        assert h == clipper_render.REEL_H

    def test_frame_raises_when_render_flag_off(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_CLIPPER_RENDER_ENABLED", raising=False)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src)
        with pytest.raises(clipper_render.RenderError, match="render is OFF"):
            clipper_render.frame_vertical(src, str(tmp_path / "out.mp4"))


# ---- Part 7: burn_captions ----------------------------------------------------------

class TestBurnCaptions:
    def test_burn_produces_output_file(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "framed.mp4")
        _make_test_video(src, duration=3, w=clipper_render.REEL_W, h=clipper_render.REEL_H)
        out = str(tmp_path / "captioned.mp4")
        transcript = _fake_transcript(start_offset=0.0)
        clipper_render.burn_captions(src, out, transcript, 0.0, 3.0)
        assert os.path.isfile(out)
        assert os.path.getsize(out) > 0

    def test_scrub_onscreen_removes_dashes_and_vendor(self):
        # dashes -> space, vendor -> partner, in both cases
        assert "-" not in clipper_render.scrub_onscreen("twenty-five")
        assert clipper_render.scrub_onscreen("TWENTY-FIVE") == "TWENTY FIVE"
        assert "—" not in clipper_render.scrub_onscreen("a—b")
        assert clipper_render.scrub_onscreen("VENDOR") == "PARTNER"
        assert clipper_render.scrub_onscreen("vendors") == "partners"

    def test_captions_never_burn_hyphens(self):
        """A hyphenated spoken word must not reach on-screen caption text."""
        transcript = {
            "words": [
                {"word": "twenty-five", "start": 0.0, "end": 0.4},
                {"word": "follow-up", "start": 0.4, "end": 0.8},
            ],
            "segments": [],
        }
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False, mode="w") as tf:
            ass_path = tf.name
        try:
            clipper_render._make_ass_subtitles(transcript, 0.0, 2.0, ass_path)
            content = open(ass_path, encoding="utf-8").read()
            events = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
            body = "\n".join(events)
            assert "-" not in body  # no hyphen in any burned caption token
            assert "TWENTY FIVE" in body and "FOLLOW UP" in body
        finally:
            try:
                os.unlink(ass_path)
            except OSError:
                pass

    def test_ass_only_includes_words_in_segment(self):
        """_make_ass_subtitles excludes words outside [start_ts, end_ts]."""
        transcript = {
            "words": [
                {"word": "before", "start": 0.0, "end": 0.5},
                {"word": "inside", "start": 1.0, "end": 1.5},
                {"word": "also_inside", "start": 1.5, "end": 2.0},
                {"word": "after",  "start": 5.0, "end": 5.5},
            ],
            "segments": [],
        }
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False, mode="w") as tf:
            ass_path = tf.name
        try:
            clipper_render._make_ass_subtitles(transcript, 0.9, 2.1, ass_path)
            content = open(ass_path, encoding="utf-8").read()
            assert "INSIDE" in content
            assert "ALSO_INSIDE" in content
            assert "BEFORE" not in content
            assert "AFTER" not in content
        finally:
            try:
                os.unlink(ass_path)
            except OSError:
                pass

    def test_ass_timestamps_are_relative_to_segment_start(self):
        transcript = {
            "words": [{"word": "gym", "start": 10.0, "end": 10.4}],
            "segments": [],
        }
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False, mode="w") as tf:
            ass_path = tf.name
        try:
            clipper_render._make_ass_subtitles(transcript, 10.0, 11.0, ass_path)
            content = open(ass_path, encoding="utf-8").read()
            # relative to start=10.0: word starts at 0.0 → "0:00:00.00"
            assert "0:00:00.00" in content
        finally:
            try:
                os.unlink(ass_path)
            except OSError:
                pass

    def test_burn_raises_when_render_flag_off(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_CLIPPER_RENDER_ENABLED", raising=False)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src, w=clipper_render.REEL_W, h=clipper_render.REEL_H)
        with pytest.raises(clipper_render.RenderError, match="render is OFF"):
            clipper_render.burn_captions(
                src, str(tmp_path / "out.mp4"), _fake_transcript(), 0.0, 2.0)


# ---- Part 8: add_brand_frame --------------------------------------------------------

class TestAddBrandFrame:
    def test_frame_produces_correct_dimensions(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "framed.mp4")
        _make_test_video(src, duration=2, w=clipper_render.REEL_W, h=clipper_render.REEL_H)
        out = str(tmp_path / "branded.mp4")
        clipper_render.add_brand_frame(src, out)
        w, h = _probe_dims(out)
        assert w == clipper_render.REEL_W
        assert h == clipper_render.REEL_H

    def test_frame_output_file_exists(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        src = str(tmp_path / "framed.mp4")
        _make_test_video(src, duration=2, w=clipper_render.REEL_W, h=clipper_render.REEL_H)
        out = str(tmp_path / "branded.mp4")
        result = clipper_render.add_brand_frame(src, out)
        assert result == out
        assert os.path.isfile(out)

    def test_frame_raises_when_render_flag_off(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_CLIPPER_RENDER_ENABLED", raising=False)
        src = str(tmp_path / "src.mp4")
        _make_test_video(src, w=clipper_render.REEL_W, h=clipper_render.REEL_H)
        with pytest.raises(clipper_render.RenderError, match="render is OFF"):
            clipper_render.add_brand_frame(src, str(tmp_path / "out.mp4"))
