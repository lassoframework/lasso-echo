"""
Phase 0 tests: prereq detection and render-flag config.
All offline — no ffmpeg invoked, no key values read.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import clipper, config  # noqa: E402


def test_detect_prereqs_returns_required_keys():
    result = clipper.detect_prereqs()
    for k in ("HAS_FFMPEG", "FFMPEG_PATH", "HAS_FASTER_WHISPER", "HAS_TRANSCRIBE_API_KEY"):
        assert k in result


def test_detect_prereqs_ffmpeg_present():
    result = clipper.detect_prereqs()
    assert result["HAS_FFMPEG"] is True
    assert result["FFMPEG_PATH"] is not None
    assert "ffmpeg" in (result["FFMPEG_PATH"] or "")


def test_detect_prereqs_faster_whisper_absent():
    result = clipper.detect_prereqs()
    assert result["HAS_FASTER_WHISPER"] is False


def test_detect_prereqs_api_key_absent(monkeypatch):
    monkeypatch.delenv(config.CLIPPER_TRANSCRIBE_KEY_ENV, raising=False)
    result = clipper.detect_prereqs()
    assert result["HAS_TRANSCRIBE_API_KEY"] is False


def test_detect_prereqs_api_key_present(monkeypatch):
    monkeypatch.setenv(config.CLIPPER_TRANSCRIBE_KEY_ENV, "dummy-name-only")
    result = clipper.detect_prereqs()
    assert result["HAS_TRANSCRIBE_API_KEY"] is True


def test_detect_prereqs_never_returns_key_value(monkeypatch):
    monkeypatch.setenv(config.CLIPPER_TRANSCRIBE_KEY_ENV, "sk-secret-value-NEVER-LOG")
    result = clipper.detect_prereqs()
    for v in result.values():
        assert "sk-secret-value" not in str(v or "")


def test_render_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_CLIPPER_RENDER_ENABLED", raising=False)
    assert config.clipper_render_enabled() is False


def test_render_flag_on(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED", "true")
    assert config.clipper_render_enabled() is True


def test_render_output_dir_default():
    d = config.clipper_render_output_dir()
    assert "clipper" in d


def test_render_output_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_DIR", str(tmp_path))
    assert config.clipper_render_output_dir() == str(tmp_path)
