"""
REAL end-to-end Story Studio render proof (ECHO_STORY_STUDIO_BUILD §3).

Drives agent.story_studio.create_story with REAL render primitives that invoke
ffmpeg, on synthetic multi-segment source clips, and asserts a genuine 1080x1920
H.264 file is produced, lands PENDING, its content_hash is in render_ledger, the
overlay passed copy_gate (no dashes) + the per-gym avatar rail, and music carries
track_id + license_ref.

The WHOLE module is skipped when ffmpeg/ffprobe are absent (the armed-env-only
case): the composer's render boundary is fully covered offline by
test_story_composer.py / test_story_studio_staging.py; this module proves the real
ffmpeg lane where it is available. Mirrors test_clipper_render.py's skip gate.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio, story_music as sm, story_composer as comp  # noqa: E402
from agent import story_ledger, clipper_render  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not found (real-render proof runs only in an armed env)",
)

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


def _run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed:\n" + (r.stderr[-600:] or "(no stderr)"))
    return r


def _make_source(path, seconds, hz):
    """A real 1280x720 (16:9) source clip so the 9:16 reframe genuinely crops."""
    _run([_FFMPEG, "-y",
          "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=30:duration={seconds}",
          "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest", path],
         "source clip")
    return path


def _make_music(path, seconds=30):
    _run([_FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
          "-c:a", "libmp3lame", "-b:a", "128k", path], "music")
    return path


def _probe(path):
    r = subprocess.run(
        [_FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True)
    return json.loads(r.stdout)


class _RealLibrary(sm.StubMusicLibrary):
    def __init__(self, audio_path):
        super().__init__(tracks=[sm.Track(
            track_id="hype_proof", license_ref="lasso-lib:LIC-PROOF",
            shelf=sm.SHELF_HYPE, title="Proof Hype", path=audio_path)])
        self._audio = audio_path

    def resolve_path(self, track):
        return self._audio


class _MemStore:
    def __init__(self):
        self.requests, self.renders = [], []

    def available(self):
        return True

    def insert_request(self, row):
        self.requests.append(dict(row)); return dict(row)

    def insert_render(self, row):
        self.renders.append(dict(row)); return dict(row)

    def update_request(self, rid, fields):
        return True


def test_real_render_produces_pending_1080x1920_h264(tmp_path, monkeypatch):
    # Arm the ffmpeg render layer + the render pilot for pierce; force the KV
    # ledger fallback (no Supabase) so the content_hash stamp is local + verifiable.
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED", "true")
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", "pierce")
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    monkeypatch.setattr("agent.story_studio._host", lambda p, g: "https://r2/s.mp4")

    gym = "pierce"
    srcs = {}
    for i, (secs, hz) in enumerate([(8, 300), (8, 440), (8, 600)]):
        aid = f"clip{i}"
        srcs[aid] = _make_source(str(tmp_path / f"{aid}.mp4"), secs, hz)
    music = _make_music(str(tmp_path / "bed.mp3"), 30)

    candidates = [
        {"asset_id": aid, "gym_id": gym, "start_ts": 0.0, "end_ts": 7.0,
         "score": 90 - i} for i, aid in enumerate(srcs)]

    # Bind each segment's real source path, then defer to the module's DEFAULT
    # reframe. Every OTHER heavy step (normalize / assemble / end-frame / music
    # burn) uses the PRODUCTION default primitives — this proves the real armed
    # path, not a test-only render.
    _orig_reframe = comp._default_reframe

    def _bind_reframe(segment, out_dir):
        segment.source_path = srcs[segment.asset_id]
        return _orig_reframe(segment, out_dir)

    monkeypatch.setattr("agent.story_composer._default_reframe", _bind_reframe)

    def _render_fn(plan, *, output_dir, ask_frame_text="", music_path=""):
        # No primitives injected -> the module defaults (real ffmpeg lane) run.
        return comp.render_compose(
            plan, output_dir=output_dir, ask_frame_text=ask_frame_text,
            music_path=music_path)

    request = {
        "gym_id": "pierce_ig", "account_key": "pierce_ig",
        "asset_ids": list(srcs.keys()),
        "brief": "Members crushed the workout today",
        "identity_tokens": ["Pierce"], "requested_by": "coach_proof",
    }
    res = story_studio.create_story(
        request, candidates=candidates, store=_MemStore(),
        music_library=_RealLibrary(music), render_fn=_render_fn,
        output_dir=str(tmp_path / "out"))

    assert res["status"] == "staged", res
    draft, sr = res["draft"], res["story_render"]

    # a real 1080x1920 H.264 file on disk
    out_path = draft.creative_path
    assert os.path.exists(out_path)
    meta = _probe(out_path)
    v = next(s for s in meta["streams"] if s["codec_type"] == "video")
    assert v["codec_name"] == "h264"
    assert (v["width"], v["height"]) == (1080, 1920)
    # the licensed bed was actually burned in -> the output carries audio.
    assert any(s["codec_type"] == "audio" for s in meta["streams"])

    # PENDING (the human tap is untouched)
    assert str(draft.status).lower().endswith("pending")
    assert sr["status"] == "pending"

    # content_hash recorded in render_ledger (the re-ingest guard)
    ch = sr["content_hash"]
    assert ch and story_ledger.is_echo_render(ch)

    # overlay passed copy_gate (no dashes) + avatar rail (no hyrox for pierce)
    overlay = sr["overlay_text_final"]
    assert "-" not in overlay and "—" not in overlay and "–" not in overlay
    assert "hyrox" not in overlay.lower()

    # music carries a track_id + license_ref
    assert sr["track_id"] == "hype_proof"
    assert sr["license_ref"] == "lasso-lib:LIC-PROOF"
