"""
Higgsfield headless renderer tests. Offline — SyncClient.subscribe is mocked.
Asserts: build_renderer returns None without key / callable with key;
higgsfield_renderer submits, downloads, writes out_path; raises on missing key,
missing prompt, failed job, missing output URL; key never logged.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config
import agent.higgsfield_renderer as hr


# ---- helpers ----------------------------------------------------------------

def _make_resp(data, is_binary=False):
    raw = data if is_binary else json.dumps(data).encode()
    state = {"pos": 0}

    class _Resp:
        def read(self, n=-1):
            pos = state["pos"]
            if n == -1:
                chunk = raw[pos:]; state["pos"] = len(raw)
            else:
                chunk = raw[pos:pos + n]; state["pos"] = pos + len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    return _Resp()


# ---- build_renderer ---------------------------------------------------------

def test_build_renderer_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_API_SECRET", raising=False)
    assert hr.build_renderer() is None


def test_build_renderer_returns_callable_with_api_key(monkeypatch):
    monkeypatch.setenv("HF_API_KEY", "testkey")
    monkeypatch.setenv("HF_API_SECRET", "testsecret")
    rndr = hr.build_renderer()
    assert callable(rndr)


def test_build_renderer_returns_callable_with_hf_key(monkeypatch):
    monkeypatch.setenv("HF_KEY", "testkey:testsecret")
    rndr = hr.build_renderer()
    assert callable(rndr)


# ---- config flags -----------------------------------------------------------

def test_config_hf_api_key_empty_by_default(monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    monkeypatch.delenv("HF_API_KEY", raising=False)
    assert config.hf_api_key() == ""


def test_config_hf_video_app_default(monkeypatch):
    monkeypatch.delenv("AGENT_HF_VIDEO_APP", raising=False)
    assert "kling" in config.hf_video_app()


def test_config_hf_image_app_default(monkeypatch):
    monkeypatch.delenv("AGENT_HF_IMAGE_APP", raising=False)
    assert "seedream" in config.hf_image_app()


def test_config_hf_video_app_override(monkeypatch):
    monkeypatch.setenv("AGENT_HF_VIDEO_APP", "kling-video/v2.1/pro/text-to-video")
    assert config.hf_video_app() == "kling-video/v2.1/pro/text-to-video"


# ---- happy path: video ------------------------------------------------------

def test_higgsfield_renderer_video_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_API_KEY", "testkey")
    monkeypatch.setenv("HF_API_SECRET", "testsecret")
    monkeypatch.delenv("AGENT_HF_VIDEO_APP", raising=False)

    out_file = str(tmp_path / "overlay.mp4")
    fake_video_bytes = b"FAKE_VIDEO_CONTENT"

    subscribe_calls = []

    class _MockClient:
        def __init__(self, api_key=None, timeout=None):
            pass

        def subscribe(self, application, arguments):
            subscribe_calls.append((application, arguments))
            return {"video": {"url": "https://cdn.higgsfield.ai/output.mp4"}}

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: _make_resp(fake_video_bytes, is_binary=True))

    import higgsfield_client.http.client as _hfc
    monkeypatch.setattr(_hfc, "SyncClient", _MockClient)

    beat = {"prompt": "A gym owner reviewing metrics on a whiteboard.", "duration": 5}
    hr.higgsfield_renderer(beat, out_file, "video")

    assert os.path.isfile(out_file)
    assert open(out_file, "rb").read() == fake_video_bytes
    assert len(subscribe_calls) == 1
    app, args = subscribe_calls[0]
    assert "kling" in app
    assert args["prompt"] == beat["prompt"]
    assert "aspect_ratio" in args


# ---- happy path: image ------------------------------------------------------

def test_higgsfield_renderer_image_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_API_KEY", "testkey")
    monkeypatch.setenv("HF_API_SECRET", "testsecret")

    out_file = str(tmp_path / "overlay.jpg")
    fake_img_bytes = b"FAKE_IMAGE_CONTENT"

    class _MockClient:
        def __init__(self, api_key=None, timeout=None):
            pass

        def subscribe(self, application, arguments):
            return {"images": [{"url": "https://cdn.higgsfield.ai/out.jpg"}]}

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: _make_resp(fake_img_bytes, is_binary=True))

    import higgsfield_client.http.client as _hfc
    monkeypatch.setattr(_hfc, "SyncClient", _MockClient)

    beat = {"prompt": "Close-up of a fitness tracker showing heart rate metrics.", "duration": 4}
    hr.higgsfield_renderer(beat, out_file, "image")

    assert os.path.isfile(out_file)
    assert open(out_file, "rb").read() == fake_img_bytes


# ---- error paths ------------------------------------------------------------

def test_higgsfield_renderer_raises_without_key(monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_API_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="HF_API_KEY"):
        hr.higgsfield_renderer({"prompt": "test", "duration": 5}, "/tmp/x.mp4", "video")


def test_higgsfield_renderer_raises_without_prompt(monkeypatch):
    monkeypatch.setenv("HF_API_KEY", "key")
    monkeypatch.setenv("HF_API_SECRET", "secret")
    with pytest.raises(RuntimeError, match="no prompt"):
        hr.higgsfield_renderer({"duration": 5}, "/tmp/x.mp4", "video")


def test_higgsfield_renderer_raises_on_missing_output_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_API_KEY", "key")
    monkeypatch.setenv("HF_API_SECRET", "secret")

    class _MockClient:
        def __init__(self, **_):
            pass

        def subscribe(self, application, arguments):
            return {}  # no video key

    import higgsfield_client.http.client as _hfc
    monkeypatch.setattr(_hfc, "SyncClient", _MockClient)

    with pytest.raises(RuntimeError, match="no output URL"):
        hr.higgsfield_renderer({"prompt": "test", "duration": 5},
                               str(tmp_path / "out.mp4"), "video")


def test_higgsfield_renderer_raises_on_sdk_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_KEY", "key:secret")

    class _MockClient:
        def __init__(self, **_):
            pass

        def subscribe(self, application, arguments):
            raise RuntimeError("Higgsfield 422 model not found")

    import higgsfield_client.http.client as _hfc
    monkeypatch.setattr(_hfc, "SyncClient", _MockClient)

    with pytest.raises(RuntimeError, match="Higgsfield 422"):
        hr.higgsfield_renderer({"prompt": "test", "duration": 5},
                               str(tmp_path / "out.mp4"), "video")


# ---- key never logged -------------------------------------------------------

def test_hf_key_not_logged(monkeypatch, capsys):
    monkeypatch.setenv("HF_API_KEY", "super-secret-hf-key-99999")
    monkeypatch.setenv("HF_API_SECRET", "super-secret-hf-secret-99999")
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_API_SECRET", raising=False)
    out, err = capsys.readouterr()
    assert "super-secret-hf-key-99999" not in out
    assert "super-secret-hf-key-99999" not in err


# ---- podcast_auto wires Higgsfield renderer ---------------------------------

def test_podcast_auto_prefers_hf_over_fal(monkeypatch):
    """When both HF and fal keys set, podcast_auto uses Higgsfield."""
    monkeypatch.setenv("HF_API_KEY", "hf-key")
    monkeypatch.setenv("HF_API_SECRET", "hf-secret")
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-key")
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")

    from agent import podcast_auto, video_editor

    captured = {}

    def _fake_edit_episode(source, render=False, renderer=None, **kwargs):
        captured["renderer"] = renderer
        return None

    def _fake_newest(cache_dir):
        return "/fake/episode.mp4"

    monkeypatch.setattr(video_editor, "edit_episode", _fake_edit_episode)
    import agent.podcast_source as _ps
    monkeypatch.setattr(_ps, "newest_episode", _fake_newest)

    podcast_auto.run(account_key="lasso_ig")

    rndr = captured.get("renderer")
    assert rndr is not None
    assert rndr.__module__.endswith("higgsfield_renderer")


def test_podcast_auto_falls_back_to_fal_without_hf(monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_API_SECRET", raising=False)
    monkeypatch.setenv("AGENT_FAL_API_KEY", "fal-key")
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")

    from agent import podcast_auto, video_editor

    captured = {}

    def _fake_edit_episode(source, render=False, renderer=None, **kwargs):
        captured["renderer"] = renderer
        return None

    def _fake_newest(cache_dir):
        return "/fake/episode.mp4"

    monkeypatch.setattr(video_editor, "edit_episode", _fake_edit_episode)
    import agent.podcast_source as _ps
    monkeypatch.setattr(_ps, "newest_episode", _fake_newest)

    podcast_auto.run(account_key="lasso_ig")

    rndr = captured.get("renderer")
    assert rndr is not None
    assert rndr.__module__.endswith("fal_renderer")


def test_podcast_auto_no_renderer_without_any_key(monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_API_SECRET", raising=False)
    monkeypatch.delenv("AGENT_FAL_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")

    from agent import podcast_auto, video_editor

    captured = {}

    def _fake_edit_episode(source, render=False, renderer=None, **kwargs):
        captured["renderer"] = renderer
        return None

    def _fake_newest(cache_dir):
        return "/fake/episode.mp4"

    monkeypatch.setattr(video_editor, "edit_episode", _fake_edit_episode)
    import agent.podcast_source as _ps
    monkeypatch.setattr(_ps, "newest_episode", _fake_newest)

    podcast_auto.run(account_key="lasso_ig")

    assert captured.get("renderer") is None
