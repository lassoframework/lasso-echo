"""
Phase 3 tests: held drafts (Part 9) and cost logging (Part 10).
All offline: fake store, fake poster, in-memory or tmp db.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import clipper  # noqa: E402
from agent.clipper import Moment  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402


# ---- shared helpers -----------------------------------------------------------------

def _moment(score=90, bucket="doctrine", start=0.0, end=45.0):
    return Moment(
        start_ts=start, end_ts=end, duration=round(end - start, 2),
        hook="Most gyms ignore follow-up completely.",
        rationale="Strong opening claim; stands alone; maps to LASSO's core lesson.",
        bucket=bucket, score=score,
        transcript_text="Most gyms ignore follow-up completely.",
    )


class _FakeStore:
    def __init__(self):
        self._data = {}

    def put(self, draft):
        self._data[draft.draft_id] = draft
        return draft

    def get(self, draft_id):
        return self._data.get(draft_id)

    def list_pending(self):
        from agent.drafter import DraftStatus
        return [d for d in self._data.values()
                if d.status == DraftStatus.PENDING]


class _FakePoster:
    def __init__(self, ok=True):
        self.posted = []
        self._ok = ok

    def post_approval_card(self, draft):
        self.posted.append(draft)
        if self._ok:
            return {"ok": True, "ts": "1234567890.123456", "channel": "C123TEST"}
        return {"ok": False}


# ---- Part 9: save_clip_draft --------------------------------------------------------

class TestSaveClipDraft:
    def test_draft_is_pending(self):
        store = _FakeStore()
        poster = _FakePoster()
        draft = clipper.save_clip_draft(
            _moment(), "/data/clipper/render/clip_00000_00045_reel.mp4",
            "https://cdn.echo.test/clip.mp4",
            "lasso_ig", store=store, poster=poster)
        assert draft.status == DraftStatus.PENDING

    def test_draft_type_is_clipper_reel(self):
        store = _FakeStore()
        poster = _FakePoster()
        draft = clipper.save_clip_draft(
            _moment(), "/data/clipper/render/reel.mp4",
            "https://cdn.test/reel.mp4",
            "lasso_ig", store=store, poster=poster)
        assert draft.draft_type == "clipper_reel"

    def test_draft_lands_in_store_pending(self):
        store = _FakeStore()
        poster = _FakePoster()
        clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "https://cdn/reel.mp4",
            "lasso_ig", store=store, poster=poster)
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0].status == DraftStatus.PENDING

    def test_slack_card_is_posted(self):
        store = _FakeStore()
        poster = _FakePoster()
        clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "https://cdn/reel.mp4",
            "lasso_ig", store=store, poster=poster)
        assert len(poster.posted) == 1

    def test_slack_ts_saved_to_draft(self):
        store = _FakeStore()
        poster = _FakePoster()
        draft = clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "https://cdn/reel.mp4",
            "lasso_ig", store=store, poster=poster)
        assert draft.slack_ts == "1234567890.123456"
        assert draft.slack_channel == "C123TEST"

    def test_source_fragment_carries_metadata(self):
        store = _FakeStore()
        poster = _FakePoster()
        draft = clipper.save_clip_draft(
            _moment(score=92, bucket="doctrine"),
            "/tmp/reel.mp4", "", "lasso_ig", store=store, poster=poster)
        combined = " ".join(draft.source_fragments)
        assert "source=clipper" in combined
        assert "kind=reel" in combined
        assert "score=92" in combined
        assert "bucket=doctrine" in combined

    def test_caption_is_moment_hook(self):
        store = _FakeStore()
        draft = clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "", "lasso_ig",
            store=store, poster=_FakePoster())
        assert draft.caption == "Most gyms ignore follow-up completely."

    def test_is_story_is_false(self):
        store = _FakeStore()
        draft = clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "", "lasso_ig",
            store=store, poster=_FakePoster())
        assert draft.is_story is False

    def test_full_approval_even_with_no_autopublish_check(self):
        """Clipper drafts are always PENDING; trust ladder never elevates them."""
        store = _FakeStore()
        draft = clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "", "lasso_ig",
            store=store, poster=_FakePoster())
        assert draft.status == DraftStatus.PENDING

    def test_evergreen_warning_added_for_newness_phrase(self):
        store = _FakeStore()
        m = _moment()
        m.hook = "New episode is out now — follow up faster"
        draft = clipper.save_clip_draft(
            m, "/tmp/reel.mp4", "", "lasso_ig",
            store=store, poster=_FakePoster())
        warnings = draft.warnings or []
        assert any("recency" in w.lower() or "imply" in w.lower() for w in warnings)

    def test_raises_without_account_key(self):
        with pytest.raises(clipper.ClipperError):
            clipper.save_clip_draft(
                _moment(), "/tmp/reel.mp4", "", "")

    def test_slack_failure_does_not_raise(self):
        """A failed Slack post must not crash the pipeline."""
        store = _FakeStore()
        poster = _FakePoster(ok=False)
        draft = clipper.save_clip_draft(
            _moment(), "/tmp/reel.mp4", "", "lasso_ig",
            store=store, poster=poster)
        assert draft.draft_id  # draft was still saved


# ---- Part 10: cost logging ----------------------------------------------------------

class TestLogEpisodeCost:
    def test_log_returns_cost_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        result = clipper.log_episode_cost(
            "ep_test_001", tokens_in=5000, tokens_out=1000, transcribe_sec=120.0)
        assert result["tokens_in"] == 5000
        assert result["tokens_out"] == 1000
        assert result["transcribe_sec"] == 120.0
        assert "estimated_usd" in result

    def test_log_writes_to_db_kv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        clipper.log_episode_cost("ep_abc123", tokens_in=3000, tokens_out=800)
        from agent import db
        # verify something was stored under a clipper_cost key
        from agent.db import connect
        with connect(str(tmp_path / "echo.db")) as conn:
            rows = conn.execute(
                "SELECT key FROM kv WHERE key LIKE 'clipper_cost_%'").fetchall()
        assert len(rows) >= 1

    def test_log_estimated_cost_positive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        result = clipper.log_episode_cost("ep_x", tokens_in=10000, tokens_out=2000)
        assert result["estimated_usd"] > 0
