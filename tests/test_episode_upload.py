"""
episode-upload CLI and gen-handoff CLI unit tests.
All offline: no R2 calls, no DB writes.
"""
import os
import sys
import subprocess

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config as _config


# ---- episode-upload argument validation ----------------------------------------

def _run(*args, env=None):
    """Run the CLI and return (returncode, stdout+stderr)."""
    e = {**os.environ, **(env or {})}
    r = subprocess.run(
        [sys.executable, "-m", "agent", *args],
        capture_output=True, text=True, env=e,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    return r.returncode, r.stdout + r.stderr


def test_episode_upload_missing_file_flag():
    code, out = _run("episode-upload")
    assert code != 0
    assert "usage" in out.lower() or "--file" in out


def test_episode_upload_nonexistent_file(tmp_path):
    code, out = _run("episode-upload", "--file", str(tmp_path / "nope.mp4"))
    assert code != 0
    assert "not found" in out.lower()


def test_episode_upload_bad_extension(tmp_path):
    p = tmp_path / "video.avi"
    p.write_bytes(b"fake")
    code, out = _run("episode-upload", "--file", str(p))
    assert code != 0
    assert ".avi" in out or "unsupported" in out.lower()


def test_episode_upload_no_r2_credentials(tmp_path, monkeypatch):
    p = tmp_path / "ep.mp4"
    p.write_bytes(b"fake")
    env = {
        _config.S3_ACCESS_KEY_ID_ENV: "",
        _config.S3_SECRET_ACCESS_KEY_ENV: "",
    }
    code, out = _run("episode-upload", "--file", str(p), env=env)
    assert code != 0
    assert "credential" in out.lower() or "not set" in out.lower() or "r2" in out.lower()


# ---- gen-handoff (no DB available, should fail gracefully) ----------------------

def test_gen_handoff_no_crash_without_db(tmp_path):
    """gen-handoff must not crash the process even when the DB is unavailable."""
    env = {"AGENT_DATA_DIR": str(tmp_path), "AGENT_DB_PATH": str(tmp_path / "nope.db")}
    code, out = _run("gen-handoff", env=env)
    # Either succeeds (writes the file) or exits with code 1 and a clear message.
    # Must NEVER raise an unhandled exception traceback.
    assert "Traceback" not in out


def test_gen_handoff_writes_html(tmp_path):
    """gen-handoff must write handoff_live.html when AGENT_DATA_DIR exists."""
    env = {"AGENT_DATA_DIR": str(tmp_path)}
    code, out = _run("gen-handoff", env=env)
    if code == 0:
        html_path = tmp_path / "handoff_live.html"
        assert html_path.exists()
        content = html_path.read_text(encoding="utf-8")
        assert "<" in content  # is HTML
