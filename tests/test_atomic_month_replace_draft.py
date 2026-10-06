"""Offline dispatch and fail-closed coverage for the draft atomic month path."""

from datetime import date

import pytest

from agent import client_month_run as cmr
from agent import portal_calendar_store as pcs
from agent import real_calendar_mirror as rcm
from agent import config


class _AtomicStore:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def atomic_month_replace_enabled(self):
        return True

    def list_month(self, *_args):
        return []

    def replace_months_atomic(self, gym, months, rows, **kwargs):
        self.calls.append((gym, months, rows, kwargs))
        if callable(kwargs.get("on_write_start")):
            kwargs["on_write_start"]()
        if self.fail:
            raise RuntimeError("rpc unavailable")
        return {"deleted": 2, "inserted": len(rows), "rows": rows}

    def delete_month(self, *_args, **_kwargs):
        pytest.fail("atomic path must never call delete_month")

    def insert_rows(self, *_args, **_kwargs):
        pytest.fail("atomic path must never call insert_rows")


def _row():
    return {"gym_id": "gym", "post_date": "2026-10-15",
            "account": "instagram", "format": "feed",
            "caption": "Real words for this gym.", "image_url": "https://example.test/a.jpg",
            "status": "pending"}


def test_client_apply_routes_to_one_atomic_call(monkeypatch):
    monkeypatch.setattr(pcs, "preserve_and_prune",
                        lambda _store, _gym, _months, rows: (rows, []))
    store = _AtomicStore()
    result = cmr._apply("gym", [_row()], date(2026, 10, 15), 1,
                        store, lambda _message: None)
    assert result["ok"] is True
    assert result["inserted"] == 1
    assert len(store.calls) == 1
    assert store.calls[0][1] == ["2026-10"]


def test_client_apply_rpc_failure_never_uses_split_writer(monkeypatch):
    monkeypatch.setattr(pcs, "preserve_and_prune",
                        lambda _store, _gym, _months, rows: (rows, []))
    store = _AtomicStore(fail=True)
    result = cmr._apply("gym", [_row()], date(2026, 10, 15), 1,
                        store, lambda _message: None)
    assert result["ok"] is False
    assert result["insert_outcome_unknown"] is True
    assert len(store.calls) == 1


class _AtomicPreflightStore(_AtomicStore):
    """Expose the same HTTP boundary as SupabaseCalendarStore for _apply."""
    def __init__(self, fail_before_post=False, fail_post=False):
        super().__init__()
        self.fail_before_post = fail_before_post
        self.fail_post = fail_post

    def _client(self):
        return self

    def post(self, *_args, **_kwargs):
        if self.fail_post:
            raise TimeoutError("connection lost after request dispatch")
        return object()

    def replace_months_atomic(self, gym, months, rows, **kwargs):
        self.calls.append((gym, months, rows, kwargs))
        if self.fail_before_post:
            raise ValueError("pre-POST validation rejected batch")
        kwargs["on_write_start"]()
        self._client().post("rpc")
        if self.fail_post:
            raise TimeoutError("connection lost after request dispatch")
        return {"deleted": 0, "inserted": len(rows), "rows": rows}


def test_pre_post_atomic_validation_is_not_reported_as_unknown_write(monkeypatch):
    monkeypatch.setattr(pcs, "preserve_and_prune",
                        lambda _store, _gym, _months, rows: (rows, []))
    store = _AtomicPreflightStore(fail_before_post=True)
    result = cmr._apply("gym", [_row()], date(2026, 10, 15), 1,
                        store, lambda _message: None)
    assert result["ok"] is False
    assert result["insert_outcome_unknown"] is False


def test_atomic_transport_failure_after_post_is_unknown(monkeypatch):
    monkeypatch.setattr(pcs, "preserve_and_prune",
                        lambda _store, _gym, _months, rows: (rows, []))
    store = _AtomicPreflightStore(fail_post=True)
    result = cmr._apply("gym", [_row()], date(2026, 10, 15), 1,
                        store, lambda _message: None)
    assert result["ok"] is False
    assert result["insert_outcome_unknown"] is True


def test_real_mirror_routes_to_one_atomic_call(monkeypatch):
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: False)
    monkeypatch.setattr(config, "source_media_content_hash_enabled", lambda: False)
    monkeypatch.setattr(rcm, "collect_real_drafts",
                        lambda *_args, **_kwargs: [_row()])
    monkeypatch.setattr(pcs, "preserve_and_prune",
                        lambda _store, _gym, _months, rows: (rows, []))
    store = _AtomicStore()
    result = rcm.mirror_to_supabase("gym", object(), store)
    assert result["ok"] is True
    assert result["inserted"] == 1
    assert len(store.calls) == 1


class _Response:
    status_code = 200
    text = ""

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _Http:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        row = dict(kwargs["json"]["p_rows"][0], id="00000000-0000-4000-8000-000000000001")
        return _Response({"deleted": 1, "inserted": 1, "rows": [row]})


def test_store_sends_frozen_snapshot_and_prepared_rows(monkeypatch):
    monkeypatch.setenv("ECHO_ATOMIC_MONTH_REPLACE_DRAFT", "1")
    monkeypatch.setattr(pcs, "_record_confirmed_story_holds", lambda *_: None)
    monkeypatch.setattr(pcs, "_story_incident_targets", lambda *_: set())
    http = _Http()
    store = pcs.SupabaseCalendarStore("https://example.test", "test-key", http)
    store.list_month = lambda *_: [{
        "id": "00000000-0000-4000-8000-000000000002", "gym_id": "gym",
        "post_date": "2026-10-15", "status": "pending", "variant_status": "active",
        "media_not_ready_reason": None, "account": "instagram", "format": "feed"}]
    result = store.replace_months_atomic("gym", ["2026-10"], [_row()])
    assert result["deleted"] == 1
    url, request = http.calls[0]
    assert url.endswith("/rpc/echo_replace_calendar_months_atomic_draft")
    assert request["json"]["p_expected"][0]["status"] == "pending"
    assert request["json"]["p_rows"][0]["gym_id"] == "gym"


def test_store_refuses_partial_month_read_before_rpc(monkeypatch):
    monkeypatch.setenv("ECHO_ATOMIC_MONTH_REPLACE_DRAFT", "1")
    http = _Http()
    store = pcs.SupabaseCalendarStore("https://example.test", "test-key", http)
    store.list_month = lambda *_: [{}] * 1000
    with pytest.raises(pcs.PortalStoreError, match="partial"):
        store.replace_months_atomic("gym", ["2026-10"], [_row()])
    assert http.calls == []


def test_store_client_preparation_failure_precedes_write_start(monkeypatch):
    monkeypatch.setenv("ECHO_ATOMIC_MONTH_REPLACE_DRAFT", "1")
    monkeypatch.setattr(pcs, "_record_confirmed_story_holds", lambda *_: None)
    monkeypatch.setattr(pcs, "_story_incident_targets", lambda *_: set())
    http = _Http()
    store = pcs.SupabaseCalendarStore("https://example.test", "test-key", http)
    store.list_month = lambda *_: []
    store._client = lambda: (_ for _ in ()).throw(RuntimeError("client setup failed"))
    started = []
    with pytest.raises(RuntimeError, match="client setup failed"):
        store.replace_months_atomic("gym", ["2026-10"], [_row()],
                                    on_write_start=lambda: started.append(True))
    assert started == []
    assert http.calls == []
