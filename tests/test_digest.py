"""
Evening digest tests. Offline. Asserts: the line assembles from store fixtures;
the schedule wiring fires once at the digest hour (persisted mark, no
double-send, restart-safe); fully inert while AGENT_DIGEST_ENABLED is OFF.
"""

import os
import sys
from datetime import datetime, timezone

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
    assert line is not None and "ECHO DAY" in line             # fires at the hour
    assert digest.maybe_send(poster, now=later) is None        # same day: no double
    assert len(poster.notices) == 1
    # the sent mark persisted to the store (restart-safe)
    assert db.kv_get("digest_sent_date") == "2026-07-06"
    # next day fires again
    next_day = datetime(2026, 7, 7, 23, 5, tzinfo=timezone.utc)
    assert digest.maybe_send(poster, now=next_day) is not None
