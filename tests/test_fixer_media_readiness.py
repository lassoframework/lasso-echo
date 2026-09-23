"""Read-only media-swap candidate readiness for the Scout -> Echo handoff."""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import fixer_ops as FO  # noqa: E402


SECRET = "readiness-secret"
GYM = "swift-river"
ROW = "row001"
NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _headers(value=SECRET):
    return lambda key, default=None: value if key == FO.HEADER else default


def _row(**changes):
    row = {"id": ROW, "gym_id": GYM, "status": "pending", "post_date": "2026-09-23",
           "account": "instagram", "source_media_asset_id": "asset-current"}
    row.update(changes)
    return row


def _asset(asset_id, **changes):
    asset = {
        "id": asset_id, "gym_id": GYM, "kind": "photo", "eligible": True,
        "excluded_by_coach": False, "review_status": "approved", "reviewed_by": "coach-1",
        "reviewed_at": "2026-09-01T00:00:00+00:00", "content_hash": f"hash-{asset_id}",
        "review_content_hash": f"hash-{asset_id}", "moderation_status": "clean",
        "moderation_json": {"verdict": "clean", "provider": "scanner",
                            "content_hash": f"hash-{asset_id}", "asset_id": asset_id,
                            "gym_id": GYM, "people_detected": False,
                            "observed_at": "2026-09-01T00:00:00+00:00"},
        "people_detected": False, "consent_status": "not_required",
        "used_count": 0, "last_used_at": None,
    }
    asset.update(changes)
    return asset


class _Calendar:
    def __init__(self, row, rows=()):
        self.row, self.rows = row, [row, *rows]
        self.calls = []

    def get_row(self, gym_key, row_id):
        self.calls.append(("get_row", gym_key, row_id))
        return dict(self.row) if row_id == ROW else None

    def list_month(self, gym_key, month):
        self.calls.append(("list_month", gym_key, month))
        return [dict(row) for row in self.rows]

    def __getattr__(self, name):
        pytest.fail(f"readiness must not call calendar.{name}")


class _Assets:
    def __init__(self, assets):
        self.assets = assets
        self.calls = []

    def available(self):
        self.calls.append("available")
        return True

    def list_assets(self, gym_key):
        self.calls.append(("list_assets", gym_key))
        return [dict(asset) for asset in self.assets]

    def __getattr__(self, name):
        pytest.fail(f"readiness must not call media_store.{name}")


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv(FO.SECRET_ENV, SECRET)


def _get(path, calendar, assets):
    return FO.handle("GET", path, _headers(), deps={"calendar_store": calendar,
                                                       "media_store": assets}, now=NOW)


def _path(row_id=ROW):
    return f"{FO.ROUTE_PREFIX}/swap_media/candidates/{GYM}?row_id={row_id}"


def test_media_readiness_requires_the_ops_secret():
    status, body = FO.handle("GET", _path(), _headers("wrong"), deps={})
    assert status == 401 and body == {"error": "unauthorized"}


def test_media_readiness_returns_only_a_real_eligible_asset_and_never_writes():
    calendar = _Calendar(_row(), [_row(id="row002", source_media_asset_id="asset-book")])
    assets = _Assets([
        _asset("asset-ok"), _asset("asset-book"),
        _asset("asset-unreviewed", review_status="pending"),
        _asset("asset-used", last_used_at="2026-09-10T00:00:00+00:00"),
    ])
    status, body = _get(_path(), calendar, assets)
    assert status == 200
    assert body == {"ok": True, "gym_key": GYM, "candidates": [
            {"id": "asset-ok", "row_id": ROW, "source": "drive",
             "review_state": "reviewed", "selectable": True}]}
    assert all(call[0] in {"get_row", "list_month"} for call in calendar.calls)
    assert assets.calls.count("available") >= 1


def test_media_readiness_includes_new_local_swap_candidate(monkeypatch):
    from agent import media_swap

    monkeypatch.setattr(media_swap, "local_candidates", lambda *args, **kwargs: [
        {"source": "local", "key": "new-photo.jpg", "path": "/library/new-photo.jpg",
         "kind": "photo", "last_used": "", "used_count": 0, "name": "new-photo.jpg"}
    ])
    status, body = _get(_path(), _Calendar(_row()), _Assets([]))
    assert status == 200
    assert body["candidates"] == [{"id": "new-photo.jpg", "row_id": ROW,
                                     "source": "local", "selectable": True}]


def test_media_readiness_zero_is_a_definitive_success_when_every_asset_is_excluded():
    calendar = _Calendar(_row(), [_row(id="row002", source_media_asset_id="asset-book")])
    assets = _Assets([
        _asset("asset-book"),
        _asset("asset-unreviewed", review_status="pending"),
        _asset("asset-used", last_used_at="2026-09-10T00:00:00+00:00"),
        _asset("asset-current"),
    ])
    status, body = _get(_path(), calendar, assets)
    assert status == 200
    assert body == {"ok": True, "gym_key": GYM, "candidates": []}


def test_media_readiness_refuses_cross_tenant_or_non_waiting_rows():
    assets = _Assets([_asset("asset-ok")])
    status, body = _get(_path(), _Calendar(_row(gym_id="other-gym")), assets)
    assert status == 503 and body["error"] == "calendar_store_unavailable"
    status, body = _get(_path(), _Calendar(_row(status="approved")), assets)
    assert status == 409 and body["error"] == "row_not_swappable"


@pytest.mark.parametrize("path", [
    f"{FO.ROUTE_PREFIX}/swap_media/candidates/{GYM}",
    f"{FO.ROUTE_PREFIX}/swap_media/candidates/{GYM}?row_id=",
    f"{FO.ROUTE_PREFIX}/swap_media/candidates/{GYM}?row_id={ROW}&row_id=row002",
    f"{FO.ROUTE_PREFIX}/swap_media/candidates/{GYM}?row_id={ROW}&extra=1",
    f"{FO.ROUTE_PREFIX}/swap_media/candidates/{GYM}?row_id",
])
def test_media_readiness_rejects_malformed_query(path):
    status, body = FO.handle("GET", path, _headers(), deps={})
    assert status == 400 and body["error"] == "bad_request"
