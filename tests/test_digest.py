"""
Evening digest tests. Offline. Asserts: the line assembles from store fixtures;
the schedule wiring fires once at the digest hour (persisted mark, no
double-send, restart-safe); fully inert while AGENT_DIGEST_ENABLED is OFF.
Also tests per-account digest lines and runway nudge deduplication.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, digest  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _seed_day(day="2026-07-06"):
    store = PendingStore()
    for i, status in enumerate([DraftStatus.PENDING, DraftStatus.APPROVED,
                                DraftStatus.APPROVED, DraftStatus.BLOCKED]):
        store.put(Draft(draft_id=f"d{i}", account_key="lasso_ig",
                        platform="instagram", caption="c", hashtags=[],
                        creative_path="/x.png", creative_public_url="",
                        scheduled_for="t", status=status, day_key=day,
                        draft_type=f"t{i}"))
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at) VALUES ('d1','lasso_ig','instagram','c','M',"
            "'published', ?)", (day + "T19:00:00",))
        conn.commit()


def test_digest_line_from_store():
    _seed_day()
    line = digest.build_digest("2026-07-06")
    assert "drafted 4" in line
    assert "approved 2" in line
    assert "published 1" in line
    assert "blocked 1" in line
    assert line.startswith("ECHO DAY 2026-07-06")


def test_inert_when_off(monkeypatch):
    monkeypatch.delenv("AGENT_DIGEST_ENABLED", raising=False)
    poster = RecordingPoster()
    at_hour = datetime(2026, 7, 6, 23, 5, tzinfo=timezone.utc)
    assert digest.maybe_send(poster, now=at_hour) is None
    assert poster.notices == []


def test_fires_once_at_the_hour_and_never_doubles(monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST_ENABLED", "true")
    monkeypatch.setenv("AGENT_DIGEST_HOUR_UTC", "23")
    _seed_day()
    poster = RecordingPoster()
    before = datetime(2026, 7, 6, 22, 59, tzinfo=timezone.utc)
    at_hour = datetime(2026, 7, 6, 23, 5, tzinfo=timezone.utc)
    later = datetime(2026, 7, 6, 23, 45, tzinfo=timezone.utc)
    assert digest.maybe_send(poster, now=before) is None       # not the hour yet
    line = digest.maybe_send(poster, now=at_hour)
    assert line is not None                                     # fires at the hour
    assert digest.maybe_send(poster, now=later) is None        # same day: no double
    assert len(poster.notices) == 1
    # the sent mark persisted to the store (restart-safe)
    assert db.kv_get("digest_sent_date") == "2026-07-06"
    # next day fires again
    next_day = datetime(2026, 7, 7, 23, 5, tzinfo=timezone.utc)
    assert digest.maybe_send(poster, now=next_day) is not None


def test_per_account_digest_line():
    """build_account_digest returns correct per-account counts."""
    day = "2026-07-08"
    store = PendingStore()
    # seed two drafts for lasso_ig: one approved, one blocked
    store.put(Draft(draft_id="pa_d0", account_key="lasso_ig",
                    platform="instagram", caption="c", hashtags=[],
                    creative_path="/x.png", creative_public_url="",
                    scheduled_for="t", status=DraftStatus.APPROVED,
                    day_key=day, draft_type="feed"))
    store.put(Draft(draft_id="pa_d1", account_key="lasso_ig",
                    platform="instagram", caption="c", hashtags=[],
                    creative_path="/x.png", creative_public_url="",
                    scheduled_for="t", status=DraftStatus.BLOCKED,
                    day_key=day, draft_type="feed"))
    # seed one draft for lasso_fb (should not appear in lasso_ig line)
    store.put(Draft(draft_id="pa_d2", account_key="lasso_fb",
                    platform="facebook_page", caption="c", hashtags=[],
                    creative_path="/x.png", creative_public_url="",
                    scheduled_for="t", status=DraftStatus.APPROVED,
                    day_key=day, draft_type="feed"))
    # seed one published post for lasso_ig
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at) VALUES ('pa_d0','lasso_ig','instagram','c','M',"
            "'published', ?)", (day + "T19:00:00",))
        conn.commit()

    line = digest.build_account_digest("lasso_ig", day)
    assert f"ECHO lasso_ig {day}" in line
    assert "drafted 2" in line
    assert "approved 1" in line
    assert "published 1" in line
    assert "failed 1" in line
    # lasso_fb counts must not leak in
    assert "lasso_fb" not in line


def test_runway_nudge_fires_at_threshold(monkeypatch):
    """runway_nudge posts to Slack when runway is below threshold."""
    monkeypatch.setenv("AGENT_RUNWAY_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUNWAY_ALERT_DAYS", "7")
    # clear any existing nudge stamp for today
    today = datetime.now(timezone.utc).date().isoformat()
    nudge_key = "runway_nudge_lasso_ig_" + today
    with db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (nudge_key,))
        conn.commit()

    poster = RecordingPoster()
    with patch("agent.runway.runway_days", return_value=3):
        fired = digest.runway_nudge("lasso_ig", "/lib", poster, threshold=7)

    assert fired is True
    assert len(poster.notices) == 1
    assert "lasso_ig" in poster.notices[0]
    assert "3 days" in poster.notices[0]
    # kv stamp was set
    assert db.kv_get(nudge_key) == "sent"


def test_runway_nudge_deduped(monkeypatch):
    """runway_nudge fires at most once per account per day."""
    monkeypatch.setenv("AGENT_RUNWAY_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUNWAY_ALERT_DAYS", "7")
    today = datetime.now(timezone.utc).date().isoformat()
    nudge_key = "runway_nudge_lasso_ig2_" + today
    # clear any prior stamp
    with db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (nudge_key,))
        conn.commit()

    poster = RecordingPoster()
    with patch("agent.runway.runway_days", return_value=2):
        first = digest.runway_nudge("lasso_ig2", "/lib", poster, threshold=7)
        second = digest.runway_nudge("lasso_ig2", "/lib", poster, threshold=7)

    assert first is True
    assert second is False
    assert len(poster.notices) == 1
