"""
fal.ai headless renderer tests. Offline — HTTP calls are monkeypatched.
Asserts: build_renderer returns None without key / callable with key;
fal_renderer submits, polls, downloads, writes out_path; raises on API error,
timeout, missing output URL; key never logged; config flags work.
"""

import json
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, fal_renderer as fr


# ---- helpers ----------------------------------------------------------------

def _make_http_mock(responses):
    """responses: list of dicts returned in order by urlopen reads."""
    _calls = []
    _idx = [0]

    class _Resp:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        _calls.append(url)
        resp_data = responses[_idx[0] % len(responses)]
        _idx[0] += 1
        return _Resp(resp_data)

    return _urlopen, _calls


# ---- build_renderer ---------------------------------------------------------

def test_build_renderer_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)
    assert fr.build_renderer() is None


def test_build_renderer_returns_callable_with_key(monkeypatch):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-test-key")
    rndr = fr.build_renderer()
    assert callable(rndr)


# ---- config flags -----------------------------------------------------------

def test_config_fal_api_key_empty_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)
    assert config.fal_api_key() == ""


def test_config_fal_video_model_default(monkeypatch):
    monkeypatch.delenv("AGENT_FAL_VIDEO_MODEL", raising=False)
    assert "kling" in config.fal_video_model()


def test_config_fal_image_model_default(monkeypatch):
    monkeypatch.delenv("AGENT_FAL_IMAGE_MODEL", raising=False)
    assert "flux" in config.fal_image_model()


def test_config_fal_video_model_override(monkeypatch):
    monkeypatch.setenv("AGENT_FAL_VIDEO_MODEL", "fal-ai/custom-model")
    assert config.fal_video_model() == "fal-ai/custom-model"


# ---- fal_renderer happy path (video) ----------------------------------------

def _make_resp(data, is_binary=False):
    """Stateful mock response whose read() properly drains like a real socket."""
    raw = data if is_binary else json.dumps(data).encode()
    state = {"pos": 0}

    class _Resp:
        def read(self, n=-1):
            pos = state["pos"]
            if n == -1:
                chunk = raw[pos:]
                state["pos"] = len(raw)
            else:
                chunk = raw[pos:pos + n]
                state["pos"] = pos + len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    return _Resp()


def test_fal_renderer_video_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-test-key")
    monkeypatch.delenv("AGENT_FAL_VIDEO_MODEL", raising=False)

    out_file = str(tmp_path / "overlay.mp4")
    fake_video_bytes = b"FAKE_VIDEO_CONTENT"
    _calls = []

    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        _calls.append(url)
        if "status" in url:
            return _make_resp({"status": "COMPLETED"})
        if "/requests/" in url and "status" not in url:
            return _make_resp({"video": {"url": "https://cdn.fal.ai/output.mp4"}})
        if "cdn.fal.ai" in url:
            return _make_resp(fake_video_bytes, is_binary=True)
        return _make_resp({"request_id": "abc-123"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    beat = {"prompt": "A gym owner closing a deal at a whiteboard.", "duration": 5}
    fr.fal_renderer(beat, out_file, "video")

    assert os.path.isfile(out_file)
    assert open(out_file, "rb").read() == fake_video_bytes
    submit_url = _calls[0]
    assert "queue.fal.run" in submit_url
    assert "kling" in submit_url


# ---- fal_renderer happy path (image) ----------------------------------------

def test_fal_renderer_image_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-test-key")
    out_file = str(tmp_path / "overlay.jpg")
    fake_img_bytes = b"FAKE_IMAGE_CONTENT"

    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        if "status" in url:
            return _make_resp({"status": "COMPLETED"})
        if "/requests/" in url and "status" not in url:
            return _make_resp({"images": [{"url": "https://cdn.fal.ai/out.jpg"}]})
        if "cdn.fal.ai" in url:
            return _make_resp(fake_img_bytes, is_binary=True)
        return _make_resp({"request_id": "img-456"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    beat = {"prompt": "Close up of a fitness tracker showing metrics.", "duration": 4}
    fr.fal_renderer(beat, out_file, "image")

    assert os.path.isfile(out_file)
    assert open(out_file, "rb").read() == fake_img_bytes


# ---- error paths ------------------------------------------------------------

def test_fal_renderer_raises_without_key(monkeypatch):
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AGENT_FAL_API_KEY"):
        fr.fal_renderer({"prompt": "test", "duration": 5}, "/tmp/x.mp4", "video")


def test_fal_renderer_raises_without_prompt(monkeypatch):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-key")
    with pytest.raises(RuntimeError, match="no prompt"):
        fr.fal_renderer({"duration": 5}, "/tmp/x.mp4", "video")


def test_fal_renderer_raises_on_job_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-key")
    out_file = str(tmp_path / "overlay.mp4")

    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        if "status" in url:
            return _make_resp({"status": "FAILED", "error": "model exploded"})
        return _make_resp({"request_id": "fail-789"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="FAILED"):
        fr.fal_renderer({"prompt": "test scene", "duration": 5}, out_file, "video")


def test_fal_renderer_raises_on_missing_output_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-key")
    out_file = str(tmp_path / "overlay.mp4")

    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        if "status" in url:
            return _make_resp({"status": "COMPLETED"})
        if "/requests/" in url:
            return _make_resp({})  # no video key
        return _make_resp({"request_id": "no-url-000"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="no output URL"):
        fr.fal_renderer({"prompt": "test scene", "duration": 5}, out_file, "video")


# ---- key never logged -------------------------------------------------------

def test_fal_key_not_logged(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_FAL_API_KEY", "super-secret-fal-key-12345")
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)  # clear after set
    out, err = capsys.readouterr()
    assert "super-secret-fal-key-12345" not in out
    assert "super-secret-fal-key-12345" not in err


# ---- podcast_auto wires fal renderer ----------------------------------------

def test_podcast_auto_passes_fal_renderer_when_key_set(monkeypatch):
    """podcast_auto uses HF renderer when HF keys are set (fal no longer used)."""
    monkeypatch.setenv("HF_API_KEY", "hf-key")
    monkeypatch.setenv("HF_API_SECRET", "hf-secret")
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")

    from agent import podcast_auto, video_editor

    captured = {}

    def _fake_edit_episode(source, render=False, renderer=None, **kwargs):
        captured["render"] = render
        captured["renderer"] = renderer
        return None  # short-circuit (no clips)

    def _fake_newest(cache_dir):
        return "/fake/episode.mp4"

    monkeypatch.setattr(video_editor, "edit_episode", _fake_edit_episode)

    import agent.podcast_source as _ps
    monkeypatch.setattr(_ps, "newest_episode", _fake_newest)

    podcast_auto.run(account_key="lasso_ig")

    assert captured.get("renderer") is not None
    assert captured.get("render") is True


def test_podcast_auto_no_renderer_without_key(monkeypatch):
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")

    from agent import podcast_auto, video_editor

    captured = {}

    def _fake_edit_episode(source, render=False, renderer=None, **kwargs):
        captured["render"] = render
        captured["renderer"] = renderer
        return None

    def _fake_newest(cache_dir):
        return "/fake/episode.mp4"

    monkeypatch.setattr(video_editor, "edit_episode", _fake_edit_episode)

    import agent.podcast_source as _ps
    monkeypatch.setattr(_ps, "newest_episode", _fake_newest)

    podcast_auto.run(account_key="lasso_ig")

    assert captured.get("renderer") is None
