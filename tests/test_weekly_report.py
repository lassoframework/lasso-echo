"""
Sunday operator report tests (readiness Part D). Offline. Asserts: flag OFF is
zero behavior anywhere (no build, no post, no kv write, store byte identical);
armed, the card renders from fixture data with EVERY section; it fires only
Sundays at 6 PM ET and once per Sunday (restart safe); an IG frequency
comparison never appears (adversarial seed); missing insights read as honest
no data; the flags delta reads vs last week; dash free.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, weekly_report  # noqa: E402

_DASH_RE = re.compile(r"[—–]")
SUNDAY_6PM_ET = datetime(2026, 7, 5, 22, 30, tzinfo=timezone.utc)  # 6:30 PM ET Sunday


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _seed_week(account_key, n, with_metrics=True):
    base = SUNDAY_6PM_ET - timedelta(hours=1)   # inside the 7 day window
    with db.connect() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO posts (draft_id, account_key, platform, caption, "
                "media_id, mode, published_at, likes, comments, saves, shares, "
                "views, reach) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{account_key}d{i}", account_key, "instagram", f"c{i}",
                 f"m{i}", "published",
                 (base - timedelta(days=i)).isoformat(),
                 20 if with_metrics else None, 1 if with_metrics else None,
                 2 if with_metrics else None, 0 if with_metrics else None,
                 800 if with_metrics else None, 500 if with_metrics else None))
        conn.commit()


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WEEKLY_REPORT_ENABLED", "true")
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    (lib / "lasso_p1_a.jpg").write_bytes(b"img")
    (lib / "lasso_p1_a.txt").write_text("A story.")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib))
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_LASSO_IG_TOKEN", "tok-ig-000001")
    monkeypatch.setenv("AGENT_LASSO_FB_TOKEN", "tok-fb-000001")


# ---- flag off = zero behavior -------------------------------------------------------------
def test_flag_off_zero_behavior(monkeypatch):
    monkeypatch.delenv("AGENT_WEEKLY_REPORT_ENABLED", raising=False)
    with db.connect() as conn:
        before = "\n".join(conn.iterdump())
    poster = RecordingPoster()
    assert weekly_report.maybe_send(poster, now=SUNDAY_6PM_ET) is None
    assert poster.notices == []
    with db.connect() as conn:
        assert "\n".join(conn.iterdump()) == before   # not even a kv stamp


# ---- the card: every section, honest gaps, IG frequency never -----------------------------
def test_report_renders_every_section(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    _seed_week("lasso_ig", 5)
    _seed_week("lasso_fb", 7)
    poster = RecordingPoster()
    text = weekly_report.maybe_send(poster, now=SUNDAY_6PM_ET)
    assert text and poster.notices == [text]
    for marker in ("ECHO WEEK", "lasso_ig: 5 post(s) published",
                   "lasso_fb: 7 post(s) published", "engagement rate on views",
                   "runway lasso_ig:", "runway lasso_fb:",
                   "approvals pending:", "flags vs last week:",
                   "by hand this week:"):
        assert marker in text, marker
    assert not _DASH_RE.search(text)
    # IG (engagement framed): no frequency comparison, no per week language
    ig_line = next(l for l in text.splitlines() if l.startswith("lasso_ig:"))
    assert "per week" not in ig_line and "cadence" not in ig_line
    # FB (frequency framed) may carry its cadence line
    fb_line = next(l for l in text.splitlines() if l.startswith("lasso_fb:"))
    assert "cadence: 7 post(s) in 7 days" in fb_line
    # debounce: the same Sunday never double sends
    assert weekly_report.maybe_send(poster, now=SUNDAY_6PM_ET) is None
    assert len(poster.notices) == 1


def test_missing_insights_read_no_data(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    _seed_week("lasso_ig", 3, with_metrics=False)
    text, _flags = weekly_report.build_report(now=SUNDAY_6PM_ET)
    ig_line = next(l for l in text.splitlines() if l.startswith("lasso_ig:"))
    assert "no data" in ig_line                     # honest, never fabricated
    assert "3 post(s) published" in ig_line


def test_fires_only_sunday_6pm_et(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    poster = RecordingPoster()
    monday = SUNDAY_6PM_ET + timedelta(days=1)
    sunday_noon_et = SUNDAY_6PM_ET - timedelta(hours=7)
    assert weekly_report.maybe_send(poster, now=monday) is None
    assert weekly_report.maybe_send(poster, now=sunday_noon_et) is None
    assert weekly_report.maybe_send(poster, now=SUNDAY_6PM_ET) is not None


def test_flags_delta_vs_last_week(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    text, _ = weekly_report.build_report(now=SUNDAY_6PM_ET)
    assert "first snapshot" in text                 # no prior week yet
    # a stored prior snapshot with podcast off -> the delta names the change
    prior = {name: False for name, _fn in weekly_report._WATCHED_FLAGS}
    db.kv_set("weekly_flags_snapshot", json.dumps(prior))
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    text2, _ = weekly_report.build_report(now=SUNDAY_6PM_ET)
    assert "podcast off to on" in text2


def test_by_hand_item_severity(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_LASSO_IG_TOKEN", raising=False)   # worst case first
    text, _ = weekly_report.build_report(now=SUNDAY_6PM_ET)
    assert "set the lasso_ig token by hand" in text
