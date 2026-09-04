"""
story_studio_store: the gym scoping its own docstring always claimed.

Until 2026-09-04 only list_requests actually filtered on gym_id — get_request,
get_render, update_request and update_render addressed a row by id ALONE, so a
request id belonging to another gym resolved, and (through story_studio.deny) could
be PATCHED. These tests drive the REAL store against a fake http and assert the
gym_id reaches PostgREST on every single-row read and write.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio_store as sss  # noqa: E402


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else []
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload


class _FakeHttp:
    """Records every call so the test can read back the exact query params sent."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        return _Resp(self.rows)

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(("PATCH", url, dict(params or {})))
        return _Resp(None)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, {}))
        return _Resp(json or [])


def _store(rows=None):
    http = _FakeHttp(rows)
    return sss.SupabaseStoryStudioStore(url="https://sb.test", service_key="k",
                                        http=http), http


# ---- single-row reads REQUIRE a gym ----------------------------------------
def test_get_request_refuses_an_unscoped_read():
    store, http = _store()
    with pytest.raises(sss.StoryStudioStoreError) as e:
        store.get_request("sr_1")
    assert e.value.status == 400
    assert not http.calls, "an unscoped read must not reach PostgREST at all"


def test_get_render_refuses_an_unscoped_read():
    store, _http = _store()
    with pytest.raises(sss.StoryStudioStoreError):
        store.get_render("sr_1")


def test_render_for_request_refuses_an_unscoped_read():
    store, _http = _store()
    with pytest.raises(sss.StoryStudioStoreError):
        store.render_for_request("sr_1", "")


def test_list_renders_refuses_an_unscoped_read():
    store, _http = _store()
    with pytest.raises(sss.StoryStudioStoreError):
        store.list_renders("")


# ---- the gym filter actually reaches the query ------------------------------
def test_get_request_filters_on_gym_id():
    store, http = _store([{"id": "sr_1", "gym_id": "pierce"}])
    row = store.get_request("sr_1", gym_id="pierce")
    assert row["id"] == "sr_1"
    params = http.calls[0][2]
    assert params["id"] == "eq.sr_1"
    assert params["gym_id"] == "eq.pierce"


def test_get_render_filters_on_gym_id():
    store, http = _store([{"id": "sr_1", "gym_id": "pierce"}])
    store.get_render("sr_1", gym_id="pierce")
    assert http.calls[0][2]["gym_id"] == "eq.pierce"


def test_render_for_request_looks_up_by_the_fk_not_the_id():
    """The render id happens to equal the request id in this build; the read must not
    depend on that."""
    store, http = _store([{"id": "sr_1", "request_id": "sr_1", "gym_id": "pierce"}])
    store.render_for_request("sr_1", "pierce")
    params = http.calls[0][2]
    assert params["request_id"] == "eq.sr_1"
    assert "id" not in params
    assert params["gym_id"] == "eq.pierce"


def test_list_renders_scopes_and_can_filter_status():
    store, http = _store([])
    store.list_renders("pierce", status="pending")
    params = http.calls[0][2]
    assert params["gym_id"] == "eq.pierce"
    assert params["status"] == "eq.pending"
    assert params["order"] == "created_at.desc"


# ---- single-row writes carry the gym too ------------------------------------
def test_update_request_scopes_the_patch_when_given_a_gym():
    store, http = _store()
    store.update_request("sr_1", {"status": "denied"}, gym_id="pierce")
    verb, _url, params = http.calls[0]
    assert verb == "PATCH"
    assert params["id"] == "eq.sr_1"
    assert params["gym_id"] == "eq.pierce"


def test_update_render_scopes_the_patch_when_given_a_gym():
    store, http = _store()
    store.update_render("sr_1", {"status": "denied"}, gym_id="pierce")
    assert http.calls[0][2]["gym_id"] == "eq.pierce"


def test_a_foreign_gym_read_returns_none_not_content():
    """PostgREST answers an empty set when the gym filter excludes the row; the store
    must surface that as None rather than reaching for rows[0]."""
    store, _http = _store([])
    assert store.get_request("sr_1", gym_id="northgate") is None
    assert store.render_for_request("sr_1", "northgate") is None
