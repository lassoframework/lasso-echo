"""Focused tests for scripts/visual_asset_snapshot.py."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import stat

import pytest

from scripts import visual_asset_snapshot as snapshot


def asset_row(rid, **overrides):
    """A row using the 15 selected live public.media_asset columns."""
    row = {
        "id": rid,
        "source_id": "source-1",
        "gym_id": "gym-1",
        "kind": "photo",
        "title": "front desk",
        "mime_type": "image/jpeg",
        "content_hash": "a" * 32,  # Drive MD5 hint; NOT original-byte proof.
        "rendition_key": f"rend-{rid}",
        "rendition_url": f"https://cdn.test/{rid}.jpg",  # NOT a source URL.
        "eligible": True,
        "excluded_by_coach": False,
        "used_count": 2,
        "last_used_at": "2026-09-30T12:00:00Z",
        "indexed_at": "2026-10-01T00:00:00Z",
        "review_status": "approved",
    }
    row.update(overrides)
    return row


class Response:
    def __init__(self, rows, total, start, status=200, *, malformed=False,
                 truncate=False):
        self.status_code = status
        self._rows = rows
        self.headers = {"Content-Range": "bad" if malformed else
                        (f"{start}-{start + len(rows) - 1}/{total}" if rows else
                         f"*/{total}")}
        self._truncate = truncate

    def json(self):
        if self._truncate:
            return self._rows[:-1]
        return self._rows


class FakeHTTP:
    """Serves pages from ``passes``: a list of row-sets, one per read pass."""

    def __init__(self, passes, *, failure_at=None, malformed_at=None,
                 truncate_at=None):
        self.passes = passes
        self.failure_at = failure_at
        self.malformed_at = malformed_at
        self.truncate_at = truncate_at
        self.pass_index = -1
        self.calls = []

    def _rows(self):
        if self.pass_index >= len(self.passes):
            return self.passes[-1]
        return self.passes[self.pass_index]

    def get(self, url, *, params, headers, timeout):
        offset = int(params["offset"])
        limit = int(params["limit"])
        self.calls.append((url, params, headers, timeout))
        if offset == 0:
            self.pass_index += 1
        if offset == self.failure_at:
            return Response([], len(self._rows()), offset, status=503)
        page = self._rows()[offset:offset + limit]
        return Response(page, len(self._rows()), offset,
                        malformed=offset == self.malformed_at,
                        truncate=offset == self.truncate_at)


class FakeStore:
    _url = "https://unit.test"
    _key = "mock-only"

    def __init__(self, http):
        self.http = http

    def _client(self):
        return self.http

    def _rest(self, path):
        return f"{self._url}/rest/v1/{path}"

    def _headers(self, extra=None):
        return {"apikey": self._key, **(extra or {})}


def test_fetch_snapshot_selects_only_real_schema_fields_in_stable_id_order():
    rows = [asset_row("c"), asset_row("a"), asset_row("b")]
    rows = sorted(rows, key=lambda item: str(item["id"]))  # models id.asc
    store = FakeStore(FakeHTTP([rows]))
    result = snapshot.fetch_snapshot(
        store, page_size=2, now=datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert result["format"] == "visual-asset-snapshot-v1"
    assert [r["id"] for r in result["rows"]] == ["a", "b", "c"]
    assert result["row_count"] == 3
    assert result["snapshot_at"] == "2026-10-04T00:00:00Z"
    assert result["consistency"] == "non_atomic_observed_scan_reconciled"
    assert result["reconciliation_passes"] == 2
    # Only these 15 live columns are selected — never sha256/md5/source_media_url.
    assert tuple(snapshot.FIELDS) == (
        "id", "source_id", "gym_id", "kind", "title", "mime_type",
        "content_hash", "rendition_key", "rendition_url", "eligible",
        "excluded_by_coach", "used_count", "last_used_at", "indexed_at",
        "review_status")
    for field in ("sha256", "md5", "source_media_url"):
        assert field not in snapshot.FIELDS
    for _, params, headers, _ in store.http.calls:
        assert params["select"] == ",".join(snapshot.FIELDS)
        assert params["order"] == "id.asc"
        assert params["limit"] == "2"
        assert headers["Prefer"] == "count=exact"
    assert "media_asset" in store.http.calls[0][0]
    # content_hash is exported only as an MD5 hint, never as a proof field.
    assert "MD5 hint" in result["warning"]
    assert "not a source URL" in result["warning"]


def test_nullable_fields_are_preserved_but_schema_not_null_fields_must_be_present():
    rows = [asset_row("a", mime_type=None, content_hash=None,
                      rendition_key=None, rendition_url=None,
                      last_used_at=None)]
    store = FakeStore(FakeHTTP([rows]))
    result = snapshot.fetch_snapshot(store)
    assert result["rows"][0]["rendition_key"] is None
    assert result["rows"][0]["content_hash"] is None

    for field, value in (("id", None), ("source_id", "  "),
                         ("gym_id", None), ("kind", ""),
                         ("title", None), ("excluded_by_coach", None),
                         ("used_count", None), ("indexed_at", None),
                         ("review_status", "")):
        with pytest.raises(ValueError, match="required NOT NULL"):
            snapshot.fetch_snapshot(
                FakeStore(FakeHTTP([[asset_row("a", **{field: value})]])))


def test_snapshot_written_owner_only_and_reconciler_accepts_real_shape(tmp_path):
    from scripts import visual_media_evidence_reconcile as reconcile_mod

    data = snapshot.fetch_snapshot(FakeStore(FakeHTTP([[asset_row("a")]])))
    path = tmp_path / "private" / "assets.json"
    snapshot.write_snapshot(path, data)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["rows"][0]["gym_id"] == "gym-1"
    assert not list(path.parent.glob("*.tmp"))
    # Real-schema snapshot rows feed the reconciler unchanged.
    cal_obs, asset_obs = reconcile_mod.reconcile([], saved["rows"])
    assert cal_obs == []
    assert asset_obs[0]["resolution"] == "unresolved"
    assert asset_obs[0]["evidence"]["rendition_identity"] == "rend-a"


@pytest.mark.parametrize("kwargs, message", [
    ({"failure_at": 0}, "HTTP 503"),
    ({"malformed_at": 0}, "Content-Range"),
    ({"truncate_at": 0}, "truncated"),
])
def test_query_pagination_and_truncation_errors_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        snapshot.fetch_snapshot(FakeStore(FakeHTTP([[asset_row("a")]], **kwargs)))


def test_missing_selected_field_fails_closed():
    incomplete = asset_row("a")
    del incomplete["review_status"]
    with pytest.raises(ValueError, match="missing a required"):
        snapshot.fetch_snapshot(FakeStore(FakeHTTP([[incomplete]])))


def test_changed_rows_between_passes_fail_closed():
    first = [asset_row("a")]
    second = [asset_row("a"), asset_row("b")]
    with pytest.raises(ValueError, match="changed between reconciliation"):
        snapshot.fetch_snapshot(FakeStore(FakeHTTP([first, second])))


def test_pagination_exhausts_all_pages_of_both_passes():
    rows = [asset_row(str(i)) for i in range(5)]
    store = FakeStore(FakeHTTP([rows]))
    result = snapshot.fetch_snapshot(store, page_size=2)
    assert result["row_count"] == 5
    # 3 pages per pass x 2 passes.
    assert len(store.http.calls) == 6
