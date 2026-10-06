"""Nine-month media-reuse regression tests: cross-platform source asset identity,
pagination + failure paths of list_media_publish_history, and the month math."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent import media_reuse_policy as p
from agent import gym_media_selector as sel
from agent.portal_calendar_store import PortalStoreError, SupabaseCalendarStore

GYM = 'zanshinfitness630e22'
NOW = datetime(2026, 9, 27, 20, tzinfo=timezone.utc)


# ---- month arithmetic (independently verified; see report) -------------------

def test_calendar_month_subtraction_preserves_valid_day():
    assert p.months_before(datetime(2026, 5, 31), 9) == datetime(2025, 8, 31)
    assert p.months_before(datetime(2026, 11, 30), 9) == datetime(2026, 2, 28)
    # End-of-month clamp: Mar 31 minus one month lands on Feb's last day.
    assert p.months_before(datetime(2026, 3, 31), 1) == datetime(2026, 2, 28)


# ---- base_gym_key must roll up _gbp ------------------------------------------

def test_base_gym_key_strips_gbp_suffix():
    assert sel.base_gym_key('pierce_gbp') == 'pierce'
    assert sel.base_gym_key('pierce_ig') == 'pierce'
    assert sel.base_gym_key('pierce_fb') == 'pierce'
    assert sel.base_gym_key('pierce') == 'pierce'
    assert p.reuse_months(GYM + '_gbp') == 9


def test_gbp_used_asset_blocks_other_platforms():
    """The SAME Drive asset posted on Google Business Profile must share reuse
    history with the IG/FB lanes — a _gbp lane key rolls up to the same base gym
    and the source_media_asset_id match holds the post."""
    row = dict(id='new', gym_id=GYM, account='instagram',
               source_media_asset_id='asset-1')
    old = dict(id='old', gym_id=GYM, account='googlebusiness',
               status='published', published_at=NOW.isoformat(),
               source_media_asset_id='asset-1')
    store = SimpleNamespace(list_media_publish_history=lambda *a: [old])
    assets = SimpleNamespace(list_assets=lambda gym: [])
    assert p.publish_hold_reason(row, GYM, store, now=NOW,
                                 media_store=assets) == 'media_reuse_nine_month_hold'


# ---- list_media_publish_history: pagination + failure paths ------------------

class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeHttp:
    """Serves canned pages per (offset) and records every request's params."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params))
        offset = int(params.get("offset", "0"))
        page = self._pages[offset // 500]
        return page() if callable(page) else page


def _store(http):
    return SupabaseCalendarStore(url="https://example.test",
                                 service_key="test-key", http=http)


def test_history_paginates_until_short_page():
    page1 = _Resp(payload=[{"id": str(i)} for i in range(500)])
    page2 = _Resp(payload=[{"id": str(500 + i)} for i in range(300)])
    http = _FakeHttp([page1, page2])
    rows = _store(http).list_media_publish_history(GYM, "2025-12-27")
    assert len(rows) == 800
    assert [c["offset"] for c in http.calls] == ["0", "500"]
    or_clause = http.calls[0]["or"]
    # Published rows with a NULL published_at conservatively participate.
    assert "and(status.eq.published,published_at.is.null)" in or_clause
    assert "published_at.gte.2025-12-27" in or_clause
    assert "status.eq.publishing" in or_clause


def test_history_failure_paths():
    with pytest.raises(PortalStoreError):
        _store(_FakeHttp([_Resp(status_code=500, payload={"message": "boom"})])
               ).list_media_publish_history(GYM, "2025-12-27")
    with pytest.raises(PortalStoreError):
        _store(_FakeHttp([_Resp(payload={"not": "a list"})])
               ).list_media_publish_history(GYM, "2025-12-27")
    # A non-list 2xx body is an invalid history, never silently empty.
    over = [_Resp(payload=[{"id": str(i)} for i in range(500)])] * 20
    with pytest.raises(PortalStoreError) as exc:
        _store(_FakeHttp(over)).list_media_publish_history(GYM, "2025-12-27")
    assert exc.value.status == 502


def test_published_row_without_published_at_still_holds():
    """The conservative-NULL arm is what lets this row reach publish_hold_reason
    at all; a row silently skipped by the gte filter would never hold."""
    row = dict(id='new', gym_id=GYM, image_url='https://cdn/photo.jpg')
    old = dict(id='old', gym_id=GYM, image_url='https://old/photo.jpg?x=1',
               status='published', published_at=None)
    store = SimpleNamespace(list_media_publish_history=lambda *a: [old])
    assets = SimpleNamespace(list_assets=lambda gym: [])
    assert p.publish_hold_reason(row, GYM, store, now=NOW,
                                 media_store=assets) == 'media_reuse_nine_month_hold'


def _approved_asset(asset_id, raw_sha256):
    md5 = "a" * 32
    return {
        "id": asset_id, "gym_id": GYM, "content_hash": md5,
        "review_content_hash": md5, "review_status": "approved",
        "moderation_status": "clean", "people_detected": False,
        "moderation_json": {
            "provider": "test", "verdict": "clean", "content_hash": md5,
            "asset_id": asset_id, "gym_id": GYM, "people_detected": False,
            "observed_at": NOW.isoformat(), "sha256": raw_sha256,
        },
    }


def test_legacy_reframe_same_raw_sha_holds_without_local_library():
    digest = "123456789abc" + "d" * 52
    row = {"id": "new", "gym_id": GYM,
           "source_media_asset_id": "drive-new",
           "image_url": "https://cdn/current.jpg"}
    old = {"id": "old", "gym_id": GYM,
           "image_url": "https://cdn/123456789abc__feed.jpg"}
    store = SimpleNamespace(list_media_publish_history=lambda *a: [old])
    assets = SimpleNamespace(
        list_assets=lambda gym: [_approved_asset("drive-new", digest)])

    assert p.publish_hold_reason(
        row, GYM, store, now=NOW, media_store=assets,
        library_path="/definitely/missing") == "media_reuse_nine_month_hold"


def test_legacy_reframe_different_raw_sha_is_allowed():
    row = {"id": "new", "gym_id": GYM,
           "source_media_asset_id": "drive-new",
           "image_url": "https://cdn/current.jpg"}
    old = {"id": "old", "gym_id": GYM,
           "image_url": "https://cdn/123456789abc__feed.jpg"}
    store = SimpleNamespace(list_media_publish_history=lambda *a: [old])
    assets = SimpleNamespace(list_assets=lambda gym: [
        _approved_asset("drive-new", "fedcba987654" + "d" * 52)])

    assert p.publish_hold_reason(
        row, GYM, store, now=NOW, media_store=assets,
        library_path="/definitely/missing") is None


def test_malformed_unresolved_legacy_reframe_fails_closed():
    row = {"id": "new", "gym_id": GYM,
           "source_media_asset_id": "drive-new"}
    old = {"id": "old", "gym_id": GYM,
           "image_url": "https://cdn/not-a-sha__feed.jpg"}
    store = SimpleNamespace(list_media_publish_history=lambda *a: [old])
    assets = SimpleNamespace(list_assets=lambda gym: [
        _approved_asset("drive-new", "fedcba987654" + "d" * 52)])

    assert p.publish_hold_reason(
        row, GYM, store, now=NOW, media_store=assets,
        library_path="/definitely/missing") == "media_reuse_history_unavailable"


def test_unbound_or_malformed_sha256_evidence_is_not_trusted():
    asset = _approved_asset("drive-new", "fedcba987654" + "d" * 52)
    assert p._evidence_sha256(asset) == "fedcba987654" + "d" * 52
    for mutation in (
        {"sha256": "bad"},
        {"asset_id": "another"},
        {"gym_id": "another"},
        {"content_hash": "b" * 32},
        {"verdict": "unsafe"},
    ):
        changed = {**asset, "moderation_json": {
            **asset["moderation_json"], **mutation}}
        assert p._evidence_sha256(changed) is None
