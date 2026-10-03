"""Offline coverage for the gym-scoped pending moderation catch-up job."""
from datetime import datetime, timezone
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key_resolve, config
from agent.jobs import moderate_pending_gym_media as job


class _Store:
    def __init__(self):
        self.sources = [
            {"id": "swift-source", "gym_id": "swift", "active": True},
            {"id": "other-source", "gym_id": "other", "active": True},
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
            {"id": asset_id, "gym_id": gym_id, "source_id": source_id,
             "kind": "photo",
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
    monkeypatch.setattr(account_key_resolve, "resolve_known_source_keys",
                        lambda keys: {key: key for key in keys})
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


def test_stale_source_key_moderates_only_canonical_tenant_assets(monkeypatch):
    store = _Store()
    store.sources = [{"id": "swift-source", "gym_id": "swift-old", "active": True}]
    seen = []
    monkeypatch.setattr(account_key_resolve, "resolve_known_source_keys",
                        lambda keys: {"swift-old": "swift"})
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda gym: gym == "swift")
    monkeypatch.setattr(job.moderation, "moderate_asset",
                        lambda gym, asset_id, **_kw: seen.append((gym, asset_id))
                        or {"ok": True})
    result = job.run(store=store, drive=_Drive(), vision=object(), gym_id="swift",
                     limit=2)
    assert result["pending"] == 3
    assert seen == [("swift", "s-01"), ("swift", "s-02")]


@pytest.mark.parametrize("source_change", [
    {"active": False}, {"revoked_externally": True}, {"kind": "other"},
])
def test_inactive_revoked_or_wrong_kind_source_is_skipped(monkeypatch, source_change):
    store = _Store()
    store.sources = [dict(store.sources[0], **source_change)]
    monkeypatch.setattr(account_key_resolve, "resolve_known_source_keys",
                        lambda keys: {"swift": "swift"})
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda gym: True)
    monkeypatch.setattr(job.moderation, "moderate_asset",
                        lambda *_args, **_kw: pytest.fail("must not moderate"))
    result = job.run(store=store, drive=_Drive(), vision=object(), gym_id="swift")
    assert result["pending"] == result["attempted"] == 0


def test_unknown_or_ambiguous_source_identity_is_skipped(monkeypatch):
    store = _Store()
    store.sources = [{"id": "swift-source", "gym_id": "swift-old", "active": True}]
    monkeypatch.setattr(account_key_resolve, "resolve_known_source_keys",
                        lambda keys: {})
    monkeypatch.setattr(job.moderation, "moderate_asset",
                        lambda *_args, **_kw: pytest.fail("must not moderate"))
    result = job.run(store=store, drive=_Drive(), vision=object(), gym_id="swift")
    assert result["pending"] == result["attempted"] == 0


def test_asset_must_match_exact_source_and_canonical_gym(monkeypatch):
    store = _Store()
    def mixed_assets(gym_id, *, source_id):
        return [
            {"id": "good", "gym_id": gym_id, "source_id": source_id,
             "kind": "photo", "review_status": "pending_review",
             "moderation_status": "pending", "content_hash": "hash"},
            {"id": "wrong-source", "gym_id": gym_id, "source_id": "other",
             "kind": "photo", "review_status": "pending_review",
             "moderation_status": "pending", "content_hash": "hash"},
            {"id": "wrong-tenant", "gym_id": "other", "source_id": source_id,
             "kind": "photo", "review_status": "pending_review",
             "moderation_status": "pending", "content_hash": "hash"},
        ]
    store.list_assets = mixed_assets
    seen = []
    monkeypatch.setattr(account_key_resolve, "resolve_known_source_keys",
                        lambda keys: {"swift": "swift"})
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda gym: gym == "swift")
    monkeypatch.setattr(job.moderation, "moderate_asset",
                        lambda gym, asset_id, **_kw: seen.append((gym, asset_id))
                        or {"ok": True})
    result = job.run(store=store, drive=_Drive(), vision=object(), gym_id="swift")
    assert result["pending"] == 1
    assert seen == [("swift", "good")]


def test_strict_identity_helper_requires_complete_fresh_plane():
    gym_id = "11111111-1111-4111-8111-111111111111"
    from agent.account_key import _base_key
    old = _base_key(gym_id, "Swift")
    calls = []
    def get(path, params):
        calls.append(path)
        if path == "echo_intake_tokens":
            return [{"gym_id": gym_id, "echo_account_key": "current"}], True
        return [{"id": gym_id, "name": "Swift"}], True
    assert account_key_resolve.resolve_known_source_keys(
        [old, "current", "unknown"], get=get) == {
            old: "current", "current": "current"}
    assert calls == ["echo_intake_tokens", "gyms"]
    def incomplete(path, params):
        if path == "echo_intake_tokens":
            return [{"gym_id": gym_id, "echo_account_key": "current"}], True
        return [], False
    assert account_key_resolve.resolve_known_source_keys(
        [old, "current"], get=incomplete) == {}


def test_strict_identity_helper_rejects_shared_live_key():
    gym_ids = ["11111111-1111-4111-8111-111111111111",
               "22222222-2222-4222-8222-222222222222"]
    def get(path, params):
        if path == "echo_intake_tokens":
            return ([{"gym_id": gym_id, "echo_account_key": "sharedkey"}
                     for gym_id in gym_ids], True)
        return ([{"id": gym_id, "name": f"Gym {i}"}
                 for i, gym_id in enumerate(gym_ids)], True)
    assert account_key_resolve.resolve_known_source_keys(
        ["sharedkey"], get=get) == {}


def test_strict_identity_helper_rejects_split_gym_token_keys():
    gym_id = "11111111-1111-4111-8111-111111111111"
    def get(path, params):
        if path == "echo_intake_tokens":
            return ([{"gym_id": gym_id, "echo_account_key": "oldkey"},
                     {"gym_id": gym_id, "echo_account_key": "newkey"}], True)
        return [{"id": gym_id, "name": "Swift"}], True
    assert account_key_resolve.resolve_known_source_keys(
        ["oldkey", "newkey"], get=get) == {}
