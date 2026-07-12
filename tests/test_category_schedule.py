"""
Category rotation Part 2: seven-day posting schedule tests.

Checks:
  1. schedule_for_day() returns the right (category, format, fallback) for each weekday.
  2. apply_daily_format() falls back to infographic and fires one ops alert when a
     video slot has no clip; fires no alert for infographic slots or flag-off runs.
  3. should_post_on() lets Saturday through when AGENT_CATEGORY_ROTATION is ON.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ops_alerts, schedule
from agent.content_categories import (
    CATEGORIES,
    _DAILY_SCHEDULE,
    apply_daily_format,
    schedule_for_day,
)

# Date anchors for the week of 2026-07-06 (Monday) through 2026-07-12 (Sunday)
_MON = "2026-07-06"
_TUE = "2026-07-07"
_WED = "2026-07-08"
_THU = "2026-07-09"
_FRI = "2026-07-10"
_SAT = "2026-07-11"
_SUN = "2026-07-12"


# ---- weekly pattern ------------------------------------------------------------------

def test_monday_podcast_infographic(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_MON)
    assert cat == "podcast" and fmt == "infographic"


def test_tuesday_platform_video(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_TUE)
    assert cat == "platform" and fmt == "video"


def test_wednesday_b2b_infographic(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_WED)
    assert cat == "b2b" and fmt == "infographic"


def test_thursday_podcast_video(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_THU)
    assert cat == "podcast" and fmt == "video"


def test_friday_summit_infographic(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_FRI)
    assert cat == "summit" and fmt == "infographic"


def test_saturday_platform_video(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_SAT)
    assert cat == "platform" and fmt == "video"


def test_sunday_podcast_infographic(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    cat, fmt, _ = schedule_for_day(_SUN)
    assert cat == "podcast" and fmt == "infographic"


def test_schedule_returns_none_when_flag_off(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "false")
    assert schedule_for_day(_TUE) is None


def test_all_seven_days_in_taxonomy(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    from datetime import date, timedelta
    start = date(2026, 7, 6)
    for i in range(7):
        day = (start + timedelta(days=i)).isoformat()
        entry = schedule_for_day(day)
        assert entry is not None, f"no schedule entry for {day}"
        cat, fmt, fallback = entry
        assert cat in CATEGORIES, f"unknown category {cat!r} on {day}"
        assert fmt in ("video", "infographic"), f"unknown format {fmt!r} on {day}"


def test_video_slots_have_infographic_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    from datetime import date, timedelta
    start = date(2026, 7, 6)
    for i in range(7):
        day = (start + timedelta(days=i)).isoformat()
        cat, fmt, fallback = schedule_for_day(day)
        if fmt == "video":
            assert fallback == "infographic", f"video slot {day} ({cat}) missing fallback"


# ---- empty-clip fallback + alert -------------------------------------------------------

def test_video_slot_no_clip_returns_infographic_and_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    result = apply_daily_format(_TUE, has_clip=False, account_key="lasso_ig")
    assert result == "infographic"
    assert len(alerts) == 1
    assert "TUE" in alerts[0]
    assert "platform" in alerts[0]


def test_video_slot_with_clip_returns_video_no_alert(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    result = apply_daily_format(_TUE, has_clip=True, account_key="lasso_ig")
    assert result == "video"
    assert len(alerts) == 0


def test_thursday_no_clip_alerts_podcast_slot(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    result = apply_daily_format(_THU, has_clip=False, account_key="lasso_fb")
    assert result == "infographic"
    assert len(alerts) == 1
    assert "THU" in alerts[0]
    assert "podcast" in alerts[0]
    assert "lasso_fb" in alerts[0]


def test_saturday_no_clip_alerts_platform_slot(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    result = apply_daily_format(_SAT, has_clip=False)
    assert result == "infographic"
    assert len(alerts) == 1
    assert "SAT" in alerts[0]


def test_infographic_slot_no_clip_no_alert(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    result = apply_daily_format(_WED, has_clip=False)  # b2b = infographic slot
    assert result == "infographic"
    assert len(alerts) == 0


def test_flag_off_no_alert_regardless_of_clip(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "false")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    result = apply_daily_format(_TUE, has_clip=False)
    assert result == "infographic"
    assert len(alerts) == 0


def test_alert_includes_account_key(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    apply_daily_format(_TUE, has_clip=False, account_key="lasso_ig")
    assert "lasso_ig" in alerts[0]


def test_alert_omits_account_when_not_given(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)

    apply_daily_format(_TUE, has_clip=False)
    assert len(alerts) == 1
    assert "for " not in alerts[0].split("no clip")[-1]


# ---- Saturday skip override ----------------------------------------------------------

def test_saturday_not_skipped_when_rotation_on(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    assert schedule.should_post_on(_SAT) is True


def test_saturday_still_skipped_when_rotation_off(monkeypatch):
    """With rotation OFF and sat manually added to skip list, Saturday is skipped."""
    from agent import config as _config
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "false")
    monkeypatch.setattr(_config, "POSTING_SKIP_DAYS", ["sat"])
    assert schedule.should_post_on(_SAT) is False


def test_other_days_unaffected_when_rotation_on(monkeypatch):
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    for day in (_MON, _TUE, _WED, _THU, _FRI, _SUN):
        assert schedule.should_post_on(day) is True, f"{day} should post"
