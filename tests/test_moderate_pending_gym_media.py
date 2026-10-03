"""Offline coverage for the gym-scoped pending moderation catch-up job."""
from datetime import datetime, timezone
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config
from agent.jobs import moderate_pending_gym_media as job


class _Store:
    def __init__(self):
        self.sources = [
            {"id": "swift-source", "gym_id": "swift"},
            {"id": "other-source", "gym_id": "other"},
        ]

    def available(self):
        return True

    def list_sources(self):
        return self.sources

    def list_assets(self, gym_id, *, source_id):
        ids = {
            "swift-source": ["s-03", "s-01", "s-02"],
            "other-source": ["o-01", "o-02"],
        }[source_id]
        return [
            {"id": asset_id, "gym_id": gym_id, "kind": "photo",
             "review_status": "pending_review", "moderation_status": "pending",
             "content_hash": f"hash-{asset_id}"}
            for asset_id in ids
        ]


class _Drive:
    def available(self):
        return True


def _run(monkeypatch, *, gym_id, limit, after=None):
    seen = []
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda gym: gym == "swift")
    monkeypatch.setattr(
        job.moderation, "moderate_asset",
        lambda gym, asset_id, **_kw: seen.append((gym, asset_id)) or {"ok": True},
    )
    result = job.run(store=_Store(), drive=_Drive(), vision=object(), gym_id=gym_id,
                     limit=limit, after=after,
                     now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    return result, seen


def test_gym_catchup_is_scoped_bounded_and_cursor_paginates(monkeypatch):
    first, seen = _run(monkeypatch, gym_id="swift", limit=2)
    assert seen == [("swift", "s-01"), ("swift", "s-02")]
    assert first == {"ok": True, "pending": 3, "attempted": 2, "recorded": 2,
                     "failures": [], "next_cursor": "s-02"}

    second, seen = _run(monkeypatch, gym_id="swift", limit=2,
                        after=first["next_cursor"])
    assert seen == [("swift", "s-03")]
    assert second["next_cursor"] is None


def test_gym_catchup_rejects_invalid_scope_or_cursor():
    with pytest.raises(ValueError, match="gym_id"):
        job.run(gym_id="", limit=1)
    with pytest.raises(ValueError, match="after"):
        job.run(gym_id="swift", after="", limit=1)


def test_cli_requires_gym_scope():
    with pytest.raises(SystemExit):
        job.main([])
