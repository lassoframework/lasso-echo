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
    lane, err = q.resolve("westside", "amb_r", "raw", resolved_by="coach1")
    assert lane == "raw"
    # The kv record existed here, so the tap IS recorded even with no shared plane.
    assert err is None
    assert q.pending("westside") == []  # no longer pending


def test_resolve_reports_an_error_when_nothing_was_recorded(monkeypatch):
    """The portal process has its own SQLite, so from there the kv branch is always a
    miss and the SHARED write is the only one that counts. A tap that recorded nothing
    must NOT come back as success — the coach would move on and the same file would
    reappear unsorted on the next sync."""
    _no_supabase(monkeypatch)          # no shared plane AND no local record
    lane, err = q.resolve("westside", "never_queued", "raw", resolved_by="coach1")
    assert lane == "raw"
    assert err, "a tap that recorded nothing must report an error"


def test_pending_prefers_the_shared_plane(monkeypatch):
    """The rows are enqueued by the WORKER; a kv-only read from the portal always
    returned [] and the gym's Sort these tab was permanently empty."""
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")

    class _Resp:
        status_code = 200

        def json(self):
            return [{"gym_id": "westside", "asset_id": "from_worker",
                     "status": "pending", "enqueued_at": "2026-08-30T00:00:00Z",
                     "reasons": '["borderline"]'}]

    class _Http:
        def get(self, url, params=None, headers=None, timeout=None):
            return _Resp()

    rows = q.pending("westside", http=_Http())
    assert [r["asset_id"] for r in rows] == ["from_worker"]
    assert rows[0]["reasons"] == ["borderline"], "the jsonb blob is decoded"
