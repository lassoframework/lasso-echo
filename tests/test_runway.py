"""
Creative runway tests. Offline. Asserts: the math (eligible assets / posts per
day), thresholds (green/amber/red + projected zero date), the alert debounce,
the gate-clean and used filters, and full inertness while the flag is OFF.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, ops_alerts, rotation, runway  # noqa: E402


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


CLEAN = [("lasso_p1_a.jpg", "A story."), ("lasso_p2_b.jpg", "Another."),
         ("lasso_p3_c.jpg", "A third."), ("lasso_p4_d.jpg", "A fourth.")]


def test_inert_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_RUNWAY_ENABLED", raising=False)
    lib = _lib(tmp_path, CLEAN)
    assert runway.daily_runway("lasso_ig", lib, "2026-07-06") is None


def test_runway_math(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN)
    # default schedule skips Saturday: 6 posting days / 7 -> 4 assets / (6/7)
    assert runway.runway_days("lasso_ig", lib) == round(4 / (6 / 7), 1)


def test_filters_used_gate_dirty_and_off_style(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN + [
        ("lasso_p2_statcard.jpg", "Convert 80 percent more with speed."),  # gate-dirty
    ])
    import json
    (tmp_path / "library" / "style_exclusions.json").write_text(
        json.dumps({"off_style": ["lasso_p4_d.jpg"]}), encoding="utf-8")
    rotation.record_served("lasso_ig", "lasso_p1_a.jpg", "p1", "2026-07-05")  # used
    eligible = {os.path.basename(c.path)
                for c in runway.eligible_creatives("lasso_ig", lib)}
    assert eligible == {"lasso_p2_b.jpg", "lasso_p3_c.jpg"}


def test_status_line_colors_and_zero_date(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    monkeypatch.setenv("AGENT_RUNWAY_ALERT_DAYS", "7")
    lib = _lib(tmp_path, CLEAN)                                # ~4.7 days -> RED
    line, days = runway.status_line("lasso_ig", lib, "2026-07-06")
    assert "RED" in line and "Projected zero: 2026-07-10" in line
    big = _lib(tmp_path / "big", [(f"lasso_p{i % 4 + 1}_x{i}.jpg", "A story.")
                                  for i in range(20)])         # ~23 days -> GREEN
    line2, _ = runway.status_line("lasso_ig", big, "2026-07-06")
    assert "GREEN" in line2


def test_alert_debounce(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    lib = _lib(tmp_path, CLEAN[:1])                            # ~1 day left -> alert
    poster = RecordingPoster()
    runway.daily_runway("lasso_ig", lib, "2026-07-06", poster=poster)
    runway.daily_runway("lasso_ig", lib, "2026-07-07", poster=poster)
    runway.daily_runway("lasso_ig", lib, "2026-07-08", poster=poster)
    low_alerts = [n for n in rec.notices if "Runway is low" in n]
    assert len(low_alerts) == 1                                 # debounced to one
    assert len(poster.notices) == 3                             # daily line still posts
    # ...and after the debounce window it may fire again
    runway.daily_runway("lasso_ig", lib, "2026-07-20", poster=poster)
    assert len([n for n in rec.notices if "Runway is low" in n]) == 2
