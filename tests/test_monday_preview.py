"""
monday-preview tests (readiness Part C). Offline (injected feed + http).
Asserts: one line per check and a GO verdict when everything is ready; each
failure mode flips to NO GO with the blocking reason named (feed unreachable,
zero runway, missing/expiring token, needed flag off); the forecast line
matches the mod 4 rotation; publish armed is surfaced loudly but is not a
blocker; the run has ZERO side effects (store byte identical); output dash
free.
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, monday_preview  # noqa: E402

from test_podcast_status import _anchor_feed  # noqa: E402

_DASH_RE = re.compile(r"[—–]")


class _Resp:
    def __init__(self, expires_in_days):
        self._exp = time.time() + expires_in_days * 86400
        self.status_code = 200

    def json(self):
        return {"data": {"expires_at": self._exp}}


class _Http:
    def __init__(self, days=45):
        self.days = days

    def get(self, url, params=None, timeout=None):
        return _Resp(self.days)


def _ready(monkeypatch, tmp_path):
    """Arm a fully GO Monday: flags on, tokens present, one eligible creative."""
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_LASSO_IG_TOKEN", "tok-ig-000001")
    monkeypatch.setenv("AGENT_LASSO_FB_TOKEN", "tok-fb-000001")
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    for name in ("lasso_p1_a.jpg", "lasso_p2_b.jpg"):
        (lib / name).write_bytes(b"img")
        (lib / name.replace(".jpg", ".txt")).write_text("A story.")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib))
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_go_when_everything_ready(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    out = monday_preview.run(fetch=lambda: _anchor_feed(3), http=_Http(45))
    printed = capsys.readouterr().out
    assert out["go"] is True and out["blockers"] == []
    assert printed.strip().endswith("MONDAY: GO")
    # one line per check family
    for marker in ("podcast feed:", "runway lasso_ig:", "runway lasso_fb:",
                   "token lasso_ig:", "token lasso_fb:", "heartbeat lasso_ig:",
                   "pending approvals:", "flag master:", "flag publish:"):
        assert marker in printed, marker
    # the forecast matches the rotation math (ep 3 mod 4 = 3 -> template c... )
    from agent.podcast_release import template_for_episode
    assert f"podcast_release_{template_for_episode(3)}" in printed
    assert not _DASH_RE.search(printed)


def test_no_go_names_each_blocker(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("refused")

    out = monday_preview.run(fetch=boom, http=_Http(2))   # feed down, token 2d
    printed = capsys.readouterr().out
    assert out["go"] is False
    assert "podcast feed unreachable" in printed
    assert "expires in 2 day(s)" in printed
    # a needed flag off is a named blocker too
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_LASSO_IG_TOKEN", raising=False)
    out = monday_preview.run(fetch=lambda: _anchor_feed(3), http=_Http(45))
    assert not out["go"]
    joined = "; ".join(out["blockers"])
    assert "AGENT_PODCAST_ENABLED is off" in joined
    assert "no token for lasso_ig" in joined


def test_zero_runway_blocks(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path)
    from agent import runway as _runway
    monkeypatch.setattr(_runway, "v2_library_concepts", lambda lib: [])
    empty = tmp_path / "empty_lib"
    empty.mkdir()
    monkeypatch.setattr(config, "LIBRARY_PATH", str(empty))
    out = monday_preview.run(fetch=lambda: _anchor_feed(3), http=_Http(45))
    assert not out["go"]
    assert any("runway is ZERO" in b for b in out["blockers"])


def test_publish_armed_is_loud_but_not_a_blocker(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    out = monday_preview.run(fetch=lambda: _anchor_feed(3), http=_Http(45))
    printed = capsys.readouterr().out
    assert "publish: ARMED" in printed and "deliberate" in printed
    assert out["go"] is True                        # a human armed it; not a block


def test_zero_side_effects(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path)
    from agent import podcast_feed, rotation
    podcast_feed.poll(fetch=lambda: _anchor_feed(3),
                      transcript_fetch=lambda u: "One clean spoken sentence.")
    rotation.record_canvas("lasso_ig", "2026-07-05", "navy")   # some real state
    with db.connect() as conn:
        before = "\n".join(conn.iterdump())
    monday_preview.run(fetch=lambda: _anchor_feed(3), http=_Http(45))
    with db.connect() as conn:
        assert "\n".join(conn.iterdump()) == before   # store byte identical
