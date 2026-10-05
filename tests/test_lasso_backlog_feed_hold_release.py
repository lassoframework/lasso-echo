"""Bounded October backlog hold release; no provider calls."""

from pathlib import Path
from types import SimpleNamespace

from agent.jobs import lasso_backlog_feed_hold_release as job

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "migrations" /
       "lasso_backlog_feed_hold_release_20261005.sql").read_text()

FEED_ID = "11111111-1111-4111-8111-111111111111"
STORY_ID = "22222222-2222-4222-8222-222222222222"


def row(fmt, row_id, *, hold=None):
    return {"id": row_id, "gym_id": "lasso", "account": "instagram",
            "post_date": "2026-10-03", "slot_index": 0, "format": fmt,
            "status": "pending", "variant_status": "active", "caption": "" if fmt == "story" else "An approved source caption",
            "image_url": "https://media.example/story.png" if fmt == "story" else "https://media.example/feed.png",
            "source_media_url": "https://media.example/story.png" if fmt == "story" else None,
            "pillar": "systems", "scheduled_at": "2026-10-03T12:30:00+00:00" if fmt == "story" else "2026-10-03T12:15:00+00:00",
            "logical_post_id": "33333333-3333-4333-8333-333333333333",
            "media_not_ready_reason": hold, "published_at": None,
            "late_post_id": None, "publish_claim_token": None,
            "publish_reservation_day": None}


class FakeStore:
    def __init__(self, feed=None, story=None, *, linked=True, rpc_result="released"):
        self.feed = feed or row("feed", FEED_ID, hold=job.HOLD)
        self.story = story or row("story", STORY_ID, hold="paired_feed_not_ready")
        self.linked = linked
        self.rpc_result = rpc_result
        self.calls = []

    def rows_in_range_complete(self, gym, first, last, *, all_statuses=False):
        assert (gym, first, last, all_statuses) == ("lasso", job.FIRST, job.LAST, True)
        return [self.feed, self.story]

    def _client(self):
        return self

    def _rest(self, table):
        return table

    def _headers(self, more=None):
        return more or {}

    def get(self, table, *, params, headers, timeout):
        self.calls.append(("GET", table, params))
        if table == "lasso_managed_paired_stories":
            data = ([{"story_id": STORY_ID, "feed_id": FEED_ID}]
                    if self.linked else [])
        elif table == "content_calendar":
            data = [dict(self.feed, media_not_ready_reason=None)]
        else:
            raise AssertionError(table)
        return SimpleNamespace(status_code=200, json=lambda: data)

    def post(self, table, *, json, headers, timeout):
        self.calls.append(("POST", table, json))
        assert table == "rpc/release_lasso_backlog_feed_hold"
        assert json["p_feed_id"] == FEED_ID and json["p_story_id"] == STORY_ID
        assert json["p_expected_feed"]["media_not_ready_reason"] == job.HOLD
        assert json["p_expected_story"]["image_url"] == self.story["image_url"]
        data = {"result": self.rpc_result, "feed_id": FEED_ID,
                "story_id": STORY_ID}
        return SimpleNamespace(status_code=200, json=lambda: data)


def test_release_requires_link_and_reads_back_exact_feed():
    store = FakeStore()
    result = job.run(account="instagram", store=store, today="2026-10-06")
    assert result == {"account": "instagram", "attempted": 1,
                      "released": 1, "idempotent": 0, "blocked": 0}
    assert [call[0] for call in store.calls] == ["GET", "POST", "GET"]


def test_missing_registry_or_held_story_cannot_call_release():
    for store in (FakeStore(linked=False),
                  FakeStore(story=row("story", STORY_ID, hold="bad_hold"))):
        result = job.run(account="instagram", store=store, today="2026-10-06")
        assert result["released"] == 0 and result["blocked"] == 1
        assert not any(call[0] == "POST" for call in store.calls)


def test_window_and_account_are_bounded():
    store = FakeStore()
    assert job.run(account="instagram", store=store, today="2026-10-12")["attempted"] == 0
    assert store.calls == []
    try:
        job.run(account="other_gym", store=store, today="2026-10-06")
    except ValueError:
        pass
    else:
        raise AssertionError("unscoped account accepted")


def test_sql_requires_exact_story_proof_and_narrow_hold_cas():
    assert "lasso_managed_paired_stories m" in SQL
    assert "public.lasso_story_current_source(s.id)" in SQL
    assert "prepared_backlog_waiting_for_story_and_capacity" in SQL
    assert "f.status <> 'pending'" in SQL
    assert "s.status <> 'pending'" in SQL
    assert "f.publish_claim_token is not null" in SQL
    assert "s.publish_claim_token is not null" in SQL
    assert "media_not_ready_reason = null" in SQL
    assert "grant execute on function public.release_lasso_backlog_feed_hold" in SQL
    assert "to service_role" in SQL
