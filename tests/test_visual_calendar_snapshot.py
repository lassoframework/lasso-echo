import json
import stat
from datetime import datetime, timezone

import pytest

from scripts import visual_calendar_snapshot as snapshot


class Response:
    def __init__(self, rows, total, start, status=200, *, malformed=False):
        self.status_code = status
        self._rows = rows
        self.headers = {"Content-Range": "bad" if malformed else
                        (f"{start}-{start + len(rows) - 1}/{total}" if rows else
                         f"*/{total}")}

    def json(self):
        return self._rows


class FakeHTTP:
    def __init__(self, rows, *, failure_at=None, malformed_at=None):
        self.rows = rows
        self.failure_at = failure_at
        self.malformed_at = malformed_at
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        offset = int(params["offset"])
        limit = int(params["limit"])
        self.calls.append((url, params, headers, timeout))
        if offset == self.failure_at:
            return Response([], len(self.rows), offset, status=503)
        page = self.rows[offset:offset + limit]
        return Response(page, len(self.rows), offset,
                        malformed=offset == self.malformed_at)


class FakeStore:
    _url = "https://unit.test"
    _key = "mock-only"

    def __init__(self, rows, **kwargs):
        self.http = FakeHTTP(rows, **kwargs)

    def _client(self):
        return self.http

    def _rest(self, path):
        return f"{self._url}/rest/v1/{path}"

    def _headers(self, extra=None):
        return {"apikey": self._key, **(extra or {})}


def row(rid, *, status="published", variant="archived"):
    return {"id": rid, "gym_id": "gym-1", "status": status,
            "variant_status": variant, "post_date": "2026-10-01",
            "image_url": None, "source_media_url": "https://media.test/a.jpg",
            "source_media_asset_id": "asset-1"}


def test_fetch_snapshot_pages_every_status_and_variant_in_stable_id_order():
    rows = [row("c", status="failed", variant="candidate"),
            row("a", status="published", variant="active"),
            row("b", status="queued", variant="archived")]
    # Server order models `id.asc`; the exporter rejects responses that violate it.
    rows = sorted(rows, key=lambda item: item["id"])
    store = FakeStore(rows)
    result = snapshot.fetch_snapshot(
        store, page_size=2, now=datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert [r["id"] for r in result["rows"]] == ["a", "b", "c"]
    assert {r["status"] for r in result["rows"]} == {"published", "queued", "failed"}
    assert {r["variant_status"] for r in result["rows"]} == {"active", "archived", "candidate"}
    assert result["row_count"] == 3
    assert result["snapshot_at"] == "2026-10-03T00:00:00Z"
    assert result["scan_started_at"] == result["scan_completed_at"]
    assert result["consistency"] == "non_atomic_observed_scan_reconciled"
    assert result["reconciliation_passes"] == 2
    assert "deleted/orphan ledger history" in result["warning"]
    assert "byte proof" in result["warning"]
    assert len(store.http.calls) == 4
    for _, params, headers, _ in store.http.calls:
        assert params["order"] == "id.asc"
        assert params["select"] == ",".join(snapshot.FIELDS)
        assert headers["Prefer"] == "count=exact"


def test_snapshot_is_atomically_written_owner_only_and_byte_inventory_compatible(tmp_path):
    from scripts.visual_byte_inventory import build_manifest

    path = tmp_path / "private" / "calendar.json"
    data = snapshot.fetch_snapshot(FakeStore([row("a")]))
    snapshot.write_snapshot(path, data)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["rows"][0]["gym_id"] == "gym-1"
    assert saved["rows"][0]["image_url"] is None
    manifest = build_manifest(saved, reader=lambda _url: b"mock-bytes")
    assert manifest["input_row_count"] == 1
    assert manifest["rows"][0]["row_ref"] == "a"
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("kwargs, message", [
    ({"failure_at": 0}, "HTTP 503"),
    ({"malformed_at": 0}, "Content-Range"),
])
def test_query_and_pagination_errors_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        snapshot.fetch_snapshot(FakeStore([row("a")], **kwargs))


def test_missing_required_field_fails_closed():
    incomplete = row("a")
    del incomplete["source_media_asset_id"]
    with pytest.raises(ValueError, match="missing a required"):
        snapshot.fetch_snapshot(FakeStore([incomplete]))


def test_null_status_is_preserved_for_drafts():
    result = snapshot.fetch_snapshot(FakeStore([row("a", status=None)]))
    assert result["rows"][0]["status"] is None


def test_pagination_total_change_fails_closed():
    class ChangingHTTP(FakeHTTP):
        def get(self, url, *, params, headers, timeout):
            offset = int(params["offset"])
            limit = int(params["limit"])
            total = 3 if offset == 0 else 4
            return Response(self.rows[offset:offset + limit], total, offset)

    store = FakeStore([row("a"), row("b"), row("c")])
    store.http = ChangingHTTP(store.http.rows)
    with pytest.raises(ValueError, match="total changed"):
        snapshot.fetch_snapshot(store, page_size=2)


def test_balanced_delete_insert_between_passes_fails_reconciliation():
    class MutatingHTTP(FakeHTTP):
        def get(self, url, *, params, headers, timeout):
            offset = int(params["offset"])
            limit = int(params["limit"])
            self.calls.append((url, params, headers, timeout))
            pass_rows = self.rows if len(self.calls) <= 2 else [
                self.rows[0], self.rows[1], row("d")]
            page = pass_rows[offset:offset + limit]
            return Response(page, len(pass_rows), offset)

    store = FakeStore([row("a"), row("b"), row("c")])
    store.http = MutatingHTTP(store.http.rows)
    with pytest.raises(ValueError, match="changed between reconciliation"):
        snapshot.fetch_snapshot(store, page_size=2)


def test_cli_error_output_does_not_expose_exception_text(monkeypatch, tmp_path, capsys):
    from agent import portal_calendar_store

    class SecretFailureStore:
        _url = "https://unit.test"
        _key = "looks-valid"

        def _client(self):
            class ExplodingHTTP:
                @staticmethod
                def get(*_args, **_kwargs):
                    raise RuntimeError("service-role-secret-value")
            return ExplodingHTTP()

    monkeypatch.setattr(portal_calendar_store, "SupabaseCalendarStore",
                        SecretFailureStore)
    assert snapshot.main([str(tmp_path / "no-write.json")]) == 2
    output = capsys.readouterr().err
    assert "query_or_reconciliation_error" in output
    assert "service-role-secret-value" not in output


def test_max_pages_bounds_query():
    with pytest.raises(ValueError, match="max_pages"):
        snapshot.fetch_snapshot(FakeStore([row(str(i)) for i in range(5)]),
                                page_size=1, max_pages=2)
