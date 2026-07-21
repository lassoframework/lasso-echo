"""Deployed Monday auto-ingest: flag gate + week-spread scheduling (mocked edit)."""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_auto, config  # noqa: E402
from agent.clipper import Moment  # noqa: E402


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_PODCAST_AUTO_ENABLED", raising=False)
    assert config.podcast_auto_enabled() is False
    assert podcast_auto.run(source="x") is None      # no-op when OFF


def test_next_posting_days_skips_configured_days(monkeypatch):
    monkeypatch.setattr(config, "POSTING_SKIP_DAYS", ["sat", "sun"])
    start = datetime.date(2026, 7, 24)   # Friday
    days = podcast_auto._next_posting_days(3, start=start)
    from agent import schedule
    assert all(schedule.weekday_abbr(d) not in ("sat", "sun") for d in days)
    assert days[0] == "2026-07-24"       # Fri
    assert days[1] == "2026-07-27"       # skips Sat/Sun -> Mon


def test_run_spreads_clips_across_week_as_held(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    monkeypatch.setattr(config, "POSTING_SKIP_DAYS", [])
    # a fake rendered file per clip
    f1 = str(tmp_path / "a.mp4"); open(f1, "wb").write(b"x")
    f2 = str(tmp_path / "b.mp4"); open(f2, "wb").write(b"x")
    m1 = Moment(start_ts=10, end_ts=40, duration=30, hook="one", rationale="r",
                bucket="sales", score=80, transcript_text="t")
    m2 = Moment(start_ts=60, end_ts=90, duration=30, hook="two", rationale="r",
                bucket="sales", score=80, transcript_text="t")
    from agent import video_editor, clipper
    monkeypatch.setattr(video_editor, "edit_episode", lambda *a, **k: {
        "staged": {"r2_key": "ep99.mp4"},
        "clips": [{"moment": m1, "files": {"9:16_cap": f1}},
                  {"moment": m2, "files": {"9:16_cap": f2}}]})
    saved = []
    monkeypatch.setattr(clipper, "save_clip_draft",
                        lambda m, p, u, a, **k: type("D", (), {
                            "draft_id": f"d{m.start_ts:.0f}",
                            "scheduled_for": k.get("scheduled_for")})())
    out = podcast_auto.run(source="ep.mp4", today=datetime.date(2026, 7, 21))
    assert out and len(out["scheduled"]) == 2
    # two different posting days, in order
    days = [s["day"] for s in out["scheduled"]]
    assert days == ["2026-07-21", "2026-07-22"]
    assert all("18:30" in s["scheduled_for"] for s in out["scheduled"])


def test_podcast_auto_due_monday_only():
    """Weekly trigger fires Monday at/after target hour, once per day."""
    from agent import listener
    from datetime import datetime, timezone
    mon = datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)   # Monday 14:00
    tue = datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)   # Tuesday
    mon_early = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    assert listener._podcast_auto_due(mon, None, 14) is True
    assert listener._podcast_auto_due(tue, None, 14) is False        # not Monday
    assert listener._podcast_auto_due(mon_early, None, 14) is False   # before hour
    # already fired today -> not due again
    assert listener._podcast_auto_due(mon, "2026-07-20", 14) is False
