"""Offline contract tests for the server-only shared media runway projection."""
from copy import deepcopy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.shared_media_runway_store import SharedMediaRunwayStore


def _fallback(active=False, episode_id=None):
    return {"active": active, "episode_id": episode_id, "depleted_on": "2026-09-18",
            "dates": ["2026-09-19", "2026-09-20"] if active else [],
            "drafts_need_review": active, "status": None}


def _notice(status="none", episode_id=None):
    return {"status": status, "episode_id": episode_id, "created_at": None,
            "delivery_confirmed": False}


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload

    def json(self):
        return deepcopy(self._payload)


class _Postgrest:
    """Schema-faithful enough to verify gym_id filters and conflict upserts."""

    def __init__(self, rows=None, status=200, raises=False):
        self.rows = {row["gym_id"]: deepcopy(row) for row in (rows or [])}
        self.status = status
        self.raises = raises
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append(("get", url, deepcopy(params), deepcopy(headers), timeout))
        if self.raises:
            raise OSError("offline")
        key = params["gym_id"].removeprefix("eq.")
        row = self.rows.get(key)
        return _Response(self.status, [row] if row else [])

    def post(self, url, *, params, headers, json, timeout):
        self.calls.append(("post", url, deepcopy(params), deepcopy(headers), deepcopy(json), timeout))
        if self.raises:
            raise OSError("offline")
        if self.status >= 400:
            return _Response(self.status)
        row = deepcopy(json[0])
        self.rows[row["gym_id"]] = row
        return _Response(201, [row])


def _store(http):
    return SharedMediaRunwayStore(url="https://example.supabase.co", service_key="service-role-test", http=http)


def test_read_scopes_by_canonical_tenant_key_and_returns_portal_safe_projection():
    http = _Postgrest(rows=[{
        "gym_id": "gymx", "fallback_episode": _fallback(True, "episode_1"),
        "notice_state": _notice("unresolved", "episode_1"), "updated_at": "2026-09-18T10:00:00Z",
    }])
    out = _store(http).read("gymx")
    assert out["gym_id"] == "gymx"
    assert out["fallback_episode"]["episode_id"] == "episode_1"
    assert out["notice_state"]["status"] == "unresolved"
    assert http.calls[0][2]["gym_id"] == "eq.gymx"
    assert _store(http).read("other") is None


def test_read_is_neutral_when_unavailable_missing_or_transport_fails():
    no_creds = SharedMediaRunwayStore(url="", service_key="", http=_Postgrest())
    assert no_creds.read("gymx") is None
    assert _store(_Postgrest()).read("gymx") is None
    assert _store(_Postgrest(status=500)).read("gymx") is None
    assert _store(_Postgrest(raises=True)).read("gymx") is None


def test_upsert_is_tenant_scoped_idempotent_and_carries_only_two_safe_projections():
    http = _Postgrest()
    store = _store(http)
    assert store.upsert("gymx", _fallback(True, "episode_1"), _notice("ready", "episode_1"))
    assert store.upsert("gymx", _fallback(False), _notice())
    row = http.rows["gymx"]
    assert set(row) == {"gym_id", "fallback_episode", "notice_state", "updated_at"}
    assert row["fallback_episode"]["active"] is False
    assert http.calls[0][2] == {"on_conflict": "gym_id"}
    assert http.calls[0][3]["Prefer"] == "resolution=merge-duplicates,return=representation"
    assert http.calls[0][4][0]["gym_id"] == "gymx"


def test_upsert_allows_an_explicit_null_fallback_to_mirror_a_replenished_runway():
    http = _Postgrest()
    store = _store(http)
    assert store.upsert("gymx", None, _notice())
    assert http.rows["gymx"]["fallback_episode"] is None
    shared = store.read("gymx")
    assert shared is not None
    assert shared["fallback_episode"] is None
    assert shared["notice_state"]["status"] == "none"


def test_upsert_refuses_secret_or_transport_fields_before_any_network_write():
    http = _Postgrest()
    fallback = _fallback(True, "episode_1")
    notice = _notice("sent", "episode_1")
    notice["channel"] = "C123CLIENT"
    assert not _store(http).upsert("gymx", fallback, notice)
    assert http.calls == []

    # Invalid tenant keys and unavailable stores are similarly inert.
    assert not _store(http).upsert("gym x", _fallback(), _notice())
    assert not SharedMediaRunwayStore(url="", service_key="", http=http).upsert(
        "gymx", _fallback(), _notice())
    assert http.calls == []


def test_read_rejects_rows_with_unexpected_transport_data_as_neutral():
    unsafe_notice = _notice("sent", "episode_1")
    unsafe_notice["text"] = "client notice text must never be shared"
    http = _Postgrest(rows=[{"gym_id": "gymx", "fallback_episode": _fallback(),
                             "notice_state": unsafe_notice}])
    assert _store(http).read("gymx") is None


def test_upsert_rejects_incoherent_active_and_delivery_states():
    http = _Postgrest()
    active_without_dates = _fallback(True, "episode_1")
    active_without_dates["dates"] = []
    assert not _store(http).upsert("gymx", active_without_dates,
                                   _notice("unresolved", "episode_1"))

    false_sent = _notice("sent", "episode_1")
    false_sent["delivery_confirmed"] = False
    assert not _store(http).upsert("gymx", _fallback(True, "episode_1"), false_sent)
    assert http.calls == []
