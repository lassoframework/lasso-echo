"""
Native clipper tests (Phase 1: selection). Fully OFFLINE: fake R2 client, fake
transcriber, fake LLM. No network, no spend, no key value ever printed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import clipper, config  # noqa: E402


class _FakeClient:
    """R2 stand-in: records puts, answers exists from a known key set."""

    def __init__(self, present=()):
        self.present = set(present)
        self.puts = []

    def exists(self, key):
        return key in self.present

    def put(self, key, local_path):
        self.puts.append((key, local_path))
        self.present.add(key)


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    # S3_PUBLIC_BASE_URL is a module constant captured at import; set it directly.
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")


# ---- Part 1: episode intake ---------------------------------------------------------

def test_intake_resolves_local_path(monkeypatch, tmp_path):
    _arm(monkeypatch)
    ep = tmp_path / "episode.mp4"
    ep.write_bytes(b"FAKE VIDEO BYTES")
    client = _FakeClient()
    out = clipper.stage_episode(str(ep), tenant="lasso_episodes", client=client)
    assert out["staged"] is True
    assert out["r2_key"].startswith("echo/lasso_episodes/")
    assert out["r2_key"].endswith("episode.mp4")
    assert out["public_url"].startswith("https://cdn.echo.test/echo/lasso_episodes/")
    assert client.puts and client.puts[0][0] == out["r2_key"]   # uploaded once


def test_intake_resolves_existing_r2_key(monkeypatch):
    _arm(monkeypatch)
    key = "echo/lasso_episodes/abc123/episode.mp4"
    client = _FakeClient(present=[key])
    out = clipper.stage_episode(key, client=client)
    assert out["staged"] is False                    # already in R2, not re-uploaded
    assert out["r2_key"] == key
    assert out["public_url"] == "https://cdn.echo.test/" + key
    assert client.puts == []                          # read-only on an existing key


def test_intake_rejects_missing_source(monkeypatch):
    _arm(monkeypatch)
    client = _FakeClient()
    with pytest.raises(clipper.ClipperError):
        clipper.stage_episode("echo/does/not/exist.mp4", client=client)


def test_intake_rejects_non_video_file(monkeypatch, tmp_path):
    _arm(monkeypatch)
    doc = tmp_path / "notes.txt"
    doc.write_text("not a video")
    with pytest.raises(clipper.ClipperError):
        clipper.stage_episode(str(doc), client=_FakeClient())


def test_clip_episode_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CLIPPER_ENABLED", raising=False)
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"x")
    assert clipper.clip_episode(str(ep)) is None


def test_clip_episode_stages_when_on(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"FAKE")
    client = _FakeClient()
    out = clipper.clip_episode(str(ep), client=client, transcriber=_fake_transcriber())
    assert out["staged"]["staged"] is True
    assert out["transcript"]["words"]
    assert "staged episode" in capsys.readouterr().out


# ---- Part 2: transcription with word-level timestamps + caching ---------------------

def _fake_transcriber(calls=None):
    """A transcriber returning word-level timestamps; counts invocations if a list
    is passed in (to prove caching)."""
    def _t(media_path):
        if calls is not None:
            calls.append(media_path)
        return {
            "words": [
                {"word": "Most", "start": 0.0, "end": 0.3},
                {"word": "gyms", "start": 0.3, "end": 0.7},
                {"word": "have", "start": 0.7, "end": 1.0},
                {"word": "a", "start": 1.0, "end": 1.1},
                {"word": "follow", "start": 1.1, "end": 1.5},
                {"word": "up", "start": 1.5, "end": 1.8},
                {"word": "problem", "start": 1.8, "end": 2.4},
            ],
            "segments": [{"speaker": "SPEAKER_0", "start": 0.0, "end": 2.4,
                          "text": "Most gyms have a follow up problem"}],
        }
    return _t


def test_transcribe_returns_word_timestamps(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    media = tmp_path / "e.mp4"
    media.write_bytes(b"FAKE")
    t = clipper.transcribe("echo/ep/abc/e.mp4", media_path=str(media),
                           transcriber=_fake_transcriber())
    assert t["words"][0] == {"word": "Most", "start": 0.0, "end": 0.3}
    assert all("start" in w and "end" in w for w in t["words"])
    assert t["segments"][0]["speaker"] == "SPEAKER_0"


def test_transcribe_caches_on_r2_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    media = tmp_path / "e.mp4"
    media.write_bytes(b"FAKE")
    calls = []
    tr = _fake_transcriber(calls)
    clipper.transcribe("echo/ep/abc/e.mp4", media_path=str(media), transcriber=tr)
    clipper.transcribe("echo/ep/abc/e.mp4", media_path=str(media), transcriber=tr)
    assert len(calls) == 1                            # second run hit the cache


def test_transcribe_needs_media_on_cache_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    with pytest.raises(clipper.ClipperError):
        clipper.transcribe("echo/ep/none/e.mp4", media_path=None,
                           transcriber=_fake_transcriber())


def test_transcribe_rejects_missing_word_timestamps(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    media = tmp_path / "e.mp4"
    media.write_bytes(b"FAKE")

    def _bad(media_path):
        return {"words": [{"word": "hi"}], "segments": []}   # no start/end

    with pytest.raises(clipper.ClipperError):
        clipper.transcribe("echo/ep/bad/e.mp4", media_path=str(media), transcriber=_bad)
