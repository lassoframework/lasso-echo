"""
Tests for AGENT_RUNWAY_ALERTS flag, _refill_ask(), and the glanceable runway card.
All offline. No live tokens.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, ops_alerts, runway  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_RUNWAY_ENABLED", "true")
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


CLEAN = [("lasso_p1_a.jpg", "A story."), ("lasso_p2_b.jpg", "Another.")]


# ---------------------------------------------------------------------------
# 1. Flag default
# ---------------------------------------------------------------------------

def test_runway_alerts_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_RUNWAY_ALERTS", raising=False)
    assert config.runway_alerts_enabled() is False


# ---------------------------------------------------------------------------
# 2. _refill_ask is dash free
# ---------------------------------------------------------------------------

def test_refill_ask_dash_free(monkeypatch, tmp_path):
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    result = runway._refill_ask("test_gym")
    # No em dash (U+2014), en dash (U+2013), or ASCII hyphen in the returned text
    assert "—" not in result, "em dash found in refill ask"
    assert "–" not in result, "en dash found in refill ask"
    assert "-" not in result, "hyphen found in refill ask"


# ---------------------------------------------------------------------------
# 3. _refill_ask never uses the word vendor
# ---------------------------------------------------------------------------

def test_refill_ask_no_vendor(monkeypatch, tmp_path):
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    result = runway._refill_ask("test_gym")
    assert "vendor" not in result.lower(), "word 'vendor' found in refill ask"


# ---------------------------------------------------------------------------
# 4. When AGENT_RUNWAY_ALERTS=true and days < threshold, poster.post_notice called
#    with the refill ask (in addition to the daily status line).
# ---------------------------------------------------------------------------

def test_daily_runway_alerts_sends_notice(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_RUNWAY_ALERTS", "true")
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    # Only 1 asset -> well below the 7-day threshold
    lib = _lib(tmp_path, CLEAN[:1])
    rec_ops = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec_ops)
    poster = RecordingPoster()
    runway.daily_runway("lasso_ig", lib, "2026-07-10", poster=poster)
    # poster.post_notice should have been called at least twice:
    # once for the status line AND once for the refill ask
    refill_notices = [n for n in poster.notices
                      if "content runway is getting short" in n]
    assert len(refill_notices) >= 1, (
        "Expected poster.post_notice called with refill ask, got: " +
        str(poster.notices))


# ---------------------------------------------------------------------------
# 5. Debounce: second call within the 7-day window does NOT fire a second notice
# ---------------------------------------------------------------------------

def test_daily_runway_debounce_blocks_second(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_RUNWAY_ALERTS", "true")
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN[:1])
    rec_ops = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec_ops)
    poster = RecordingPoster()
    # First call: should fire the refill notice
    runway.daily_runway("lasso_ig", lib, "2026-07-10", poster=poster)
    # Second call within the debounce window: should NOT fire again
    runway.daily_runway("lasso_ig", lib, "2026-07-11", poster=poster)
    refill_notices = [n for n in poster.notices
                      if "content runway is getting short" in n]
    assert len(refill_notices) == 1, (
        "Debounce failed: refill ask fired more than once within 7 days")


# ---------------------------------------------------------------------------
# 6. Runway card output contains a color tag (GREEN, AMBER, or RED)
# ---------------------------------------------------------------------------

def test_runway_card_shows_color(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN)
    days = runway.runway_days("lasso_ig", lib)
    threshold = int(os.environ.get("AGENT_RUNWAY_ALERT_DAYS", "7"))
    color = runway._color(days, threshold)
    assert color in ("GREEN", "AMBER", "RED"), f"Unexpected color: {color}"
