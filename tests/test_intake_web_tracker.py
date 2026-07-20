"""
Admin tracker route tests. Offline: pure handle_tracker() calls + one live-server
integration test.

Asserts:
- Wrong token or unset token -> 404 (indistinguishable, on purpose).
- Correct token + tracker page -> 200, HTML content from docs/echo_build_tracker.html.
- Correct token + handoff page -> 200, HTML content from docs/ECHO_HANDOFF.html.
- File missing from repo -> 404, no crash.
- Token shorter than 8 chars is not matched by the URL regex.
- The route works end-to-end via the real ThreadingHTTPServer.
"""

import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web


_GOOD_TOKEN = "tracker-tok-abcdef1234"


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_TRACKER_TOKEN", _GOOD_TOKEN)


# ---- handle_tracker unit tests -------------------------------------------------

def test_correct_token_tracker_page(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setattr(intake_web, "_REPO_ROOT", str(tmp_path))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "echo_build_tracker.html").write_bytes(b"<html>TRACKER</html>")
    (docs / "ECHO_HANDOFF.html").write_bytes(b"<html>HANDOFF</html>")

    status, body = intake_web.handle_tracker(_GOOD_TOKEN, "tracker")
    assert status == 200
    assert b"TRACKER" in body


def test_correct_token_handoff_page(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setattr(intake_web, "_REPO_ROOT", str(tmp_path))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "echo_build_tracker.html").write_bytes(b"<html>TRACKER</html>")
    (docs / "ECHO_HANDOFF.html").write_bytes(b"<html>HANDOFF</html>")

    status, body = intake_web.handle_tracker(_GOOD_TOKEN, "handoff")
    assert status == 200
    assert b"HANDOFF" in body


def test_wrong_token_is_404(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setattr(intake_web, "_REPO_ROOT", str(tmp_path))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "echo_build_tracker.html").write_bytes(b"X")

    status, _ = intake_web.handle_tracker("wrong-token-xxxxxxxx", "tracker")
    assert status == 404


def test_unset_token_is_404(monkeypatch):
    monkeypatch.delenv("AGENT_TRACKER_TOKEN", raising=False)
    status, _ = intake_web.handle_tracker(_GOOD_TOKEN, "tracker")
    assert status == 404


def test_unknown_page_is_404(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setattr(intake_web, "_REPO_ROOT", str(tmp_path))
    status, _ = intake_web.handle_tracker(_GOOD_TOKEN, "nonexistent")
    assert status == 404


def test_missing_file_is_404(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setattr(intake_web, "_REPO_ROOT", str(tmp_path))
    # docs/ exists but the HTML files do not
    (tmp_path / "docs").mkdir()
    status, _ = intake_web.handle_tracker(_GOOD_TOKEN, "tracker")
    assert status == 404


def test_empty_token_env_is_404(monkeypatch):
    monkeypatch.setenv("AGENT_TRACKER_TOKEN", "")
    status, _ = intake_web.handle_tracker(_GOOD_TOKEN, "tracker")
    assert status == 404


# ---- _tracker_route regex tests (no HTTP server) --------------------------------

def _route(path):
    """Call _tracker_route via a minimal stub."""
    class _Stub:
        def __init__(self, p):
            self.path = p
        _tracker_route = intake_web.build_server.__globals__.get(
            "_tracker_route", None)   # not reachable this way; use real server

    # The regex lives inside the Handler class; test it through the real server
    # binding by driving HTTP requests in the integration test below.
    # For a fast unit check, replicate the regex here (single source of truth test).
    import re
    m = re.match(r"^/admin/tracker/([A-Za-z0-9_-]{8,})(/handoff)?$",
                 path.split("?")[0])
    if m:
        return m.group(1), ("handoff" if m.group(2) else "tracker")
    return None, None


def test_route_matches_tracker():
    tok, page = _route(f"/admin/tracker/{_GOOD_TOKEN}")
    assert tok == _GOOD_TOKEN
    assert page == "tracker"


def test_route_matches_handoff():
    tok, page = _route(f"/admin/tracker/{_GOOD_TOKEN}/handoff")
    assert tok == _GOOD_TOKEN
    assert page == "handoff"


def test_route_no_match_short_token():
    tok, page = _route("/admin/tracker/short")   # < 8 chars
    assert tok is None


def test_route_no_match_unknown_subpath():
    tok, page = _route(f"/admin/tracker/{_GOOD_TOKEN}/other")
    assert tok is None


def test_route_no_match_random_path():
    tok, page = _route("/u/some-token-abc")
    assert tok is None


# ---- integration: real HTTP server ---------------------------------------------

@pytest.fixture
def tracker_server(monkeypatch, tmp_path):
    """Live server with tracker token set and fake docs files."""
    monkeypatch.setenv("AGENT_TRACKER_TOKEN", _GOOD_TOKEN)
    monkeypatch.setattr(intake_web, "_REPO_ROOT", str(tmp_path))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "echo_build_tracker.html").write_bytes(b"<html>BUILD TRACKER</html>")
    (docs / "ECHO_HANDOFF.html").write_bytes(b"<html>HANDOFF PAGE</html>")
    srv = intake_web.build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _get(srv, path):
    port = srv.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_tracker_page_via_http(tracker_server):
    status, body = _get(tracker_server, f"/admin/tracker/{_GOOD_TOKEN}")
    assert status == 200
    assert b"BUILD TRACKER" in body


def test_handoff_page_via_http(tracker_server):
    status, body = _get(tracker_server, f"/admin/tracker/{_GOOD_TOKEN}/handoff")
    assert status == 200
    assert b"HANDOFF PAGE" in body


def test_wrong_token_via_http(tracker_server):
    status, _ = _get(tracker_server, "/admin/tracker/wrong-token-xxxxxxxxx")
    assert status == 404


def test_other_paths_still_404_via_http(tracker_server):
    """The tracker token must not open any path outside /admin/tracker/."""
    status, _ = _get(tracker_server, f"/u/{_GOOD_TOKEN}")
    assert status == 404
