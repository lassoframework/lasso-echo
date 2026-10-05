"""LASSO's third Story is scoped to durable 3x and atomic DB claims."""

import re
from pathlib import Path

from agent import calendar_autopublish as cap
from agent import cadence


def _capacity(monkeypatch, gym, fmt, *, durable=False, summit=False):
    monkeypatch.setattr(cadence, "resolve_posts_per_day",
                        lambda gym_id, store, day=None: 3)
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled", lambda: durable)
    monkeypatch.setattr(cap.config, "lasso_summit_daily_enabled",
                        lambda day: summit)
    return cap._publish_capacity(gym, {"format": fmt}, object(), "2026-10-05")


def test_durable_lasso_three_stories(monkeypatch):
    assert _capacity(monkeypatch, "lasso", "story", durable=True) == 3


def test_summit_only_pairs_third_story(monkeypatch):
    assert _capacity(monkeypatch, "lasso", "story", summit=True) == 3
    assert _capacity(monkeypatch, "lasso", "feed", summit=True) == 3


def test_client_story_limit_stays_two_even_when_lasso_is_armed(monkeypatch):
    assert _capacity(monkeypatch, "client-gym", "story", durable=True) == 2


def test_lasso_story_slots_follow_each_feed_by_fifteen_minutes(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "false")
    for index, feed_time, story_time in (
            (0, "07:30", "07:45"),
            (1, "18:30", "18:45"),
            (2, "12:00", "12:15")):
        base = {"gym_id": "lasso", "post_date": "2026-10-05", "slot_index": index}
        assert cap.slot_time_for_row({**base, "format": "feed"}) == feed_time
        assert cap.slot_time_for_row({**base, "format": "story"}) == story_time


def test_lasso_story_slot_does_not_wrap_into_next_morning(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setenv("AGENT_CADENCE_SLOT_TIMES", "07:30,23:50")
    row = {"gym_id": "lasso", "format": "story", "slot_index": 1}
    assert cap.slot_time_for_row(row) == "23:59"


def test_story_slot_change_applies_during_summit_only_window(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "false")
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    lasso = {"gym_id": "lasso", "post_date": "2026-10-05",
             "format": "story", "slot_index": 2}
    assert cap.slot_time_for_row(lasso) == "12:15"
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    assert cap.slot_time_for_row({**lasso, "gym_id": "client-gym"}) == "12:30"


def test_story_claim_migration_only_widens_lasso_format_guard():
    root = Path(__file__).resolve().parents[1] / "migrations"
    previous = (root / "calendar_claim_media_guard_20261002.sql").read_text()
    current = (root / "lasso_three_story_capacity_20261005.sql").read_text()
    guard = "coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')"
    assert "p_capacity = 3 and p_gym_id <> 'lasso'" in current
    assert "claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean)" in current
    assert "slot_index = 2\n      and gym_id = 'lasso'" in current
    assert "'feed', 'story'" in current
    assert "media_not_ready_reason is null" in current

    def claim_body(sql):
        body = sql.split("create or replace function public.claim_calendar_publish_slot_owned(", 1)[1]
        body = body.split("revoke all on function", 1)[0]
        body = re.sub(r"--[^\n]*", "", body)
        return " ".join(body.split())

    # Keep the prior RPC's ownership, media, day, and format accounting exact.
    widened = claim_body(current).replace(
        f"{guard} not in ('feed', 'story')",
        f"{guard} <> 'feed'")
    assert widened == claim_body(previous)
