"""
Story Studio Wave 1: the "Sort these" ambiguous queue + coach-channel digest.
Ambiguous files are queued for a human, NEVER auto-staged; the digest fires only
when the queue is non-empty.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_sort_queue as q  # noqa: E402


def _no_supabase(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")


def test_enqueue_is_idempotent(monkeypatch):
    _no_supabase(monkeypatch)
    assert q.enqueue("pierce", "asset_amb_1", reasons=["no strong signal"]) is True
    assert q.enqueue("pierce", "asset_amb_1", reasons=["no strong signal"]) is False
    items = q.pending("pierce")
    assert any(i["asset_id"] == "asset_amb_1" for i in items)


def test_digest_silent_when_empty(monkeypatch):
    _no_supabase(monkeypatch)
    calls = []

    class _P:
        def post_notice(self, m):
            calls.append(m)

    n = q.post_digest("gym_with_empty_queue", poster=_P())
    assert n == 0
    assert calls == []


def test_digest_fires_when_non_empty(monkeypatch):
    _no_supabase(monkeypatch)
    calls = []

    class _P:
        def post_notice(self, m):
            calls.append(m)

    q.enqueue("northgate", "amb_x", reasons=["9:16 22s no text"])
    n = q.post_digest("northgate", poster=_P())
    assert n == 1
    assert len(calls) == 1
    assert "Sort these" in calls[0]


def test_resolve_marks_resolved_and_returns_lane(monkeypatch):
    _no_supabase(monkeypatch)
    q.enqueue("westside", "amb_r", reasons=["borderline"])
    lane = q.resolve("westside", "amb_r", "raw", resolved_by="coach1")
    assert lane == "raw"
    assert q.pending("westside") == []  # no longer pending
