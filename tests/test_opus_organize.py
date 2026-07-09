"""
Opus collection management tests (opus-organize). Fully OFFLINE: a fake requests
transport, no network, no spend, no key value ever printed.

Routes under test (verified against help.opus.pro/api-reference/openapi.json):
  - POST /api/collections            {collectionName}      -> {collectionId,...}
  - POST /api/collection-contents    {collectionId, contentId}   (one clip)
  - GET  /api/exportable-clips        q=findByProjectId&projectId  (project clips)
The ExportableClipRepresentation carries NO score field; clips arrive in curation
rank order.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, opus_ingest  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeHttp:
    """Records every GET/POST (path + params/body) and replies from a routing fn."""

    def __init__(self, router):
        self.router = router
        self.gets = []
        self.posts = []

    def get(self, url, params=None, headers=None, timeout=30):
        self.gets.append((url, dict(params or {})))
        return self.router("GET", url, params or {}, None)

    def post(self, url, json=None, headers=None, timeout=30):
        self.posts.append((url, dict(json or {})))
        return self.router("POST", url, {}, json or {})


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.setenv("OPUS_API_KEY", "sk-testkey123")


# ---- Part 1: client methods ---------------------------------------------------------

def test_create_collection_posts_name_and_returns_id(monkeypatch):
    _arm(monkeypatch)

    def router(method, url, params, body):
        assert method == "POST" and url.endswith("/api/collections")
        assert body == {"collectionName": "LASSO Clips"}
        return _Resp(200, {"collectionId": "COL_NEW", "collectionName": "LASSO Clips"})

    http = _FakeHttp(router)
    monkeypatch.setattr("requests.get", http.get)
    monkeypatch.setattr("requests.post", http.post)
    api = opus_ingest.OpusAPI("sk-testkey123")
    assert api.create_collection("LASSO Clips") == "COL_NEW"


def test_create_collection_tolerates_data_wrapper(monkeypatch):
    _arm(monkeypatch)

    def router(method, url, params, body):
        return _Resp(200, {"data": {"collectionId": "COL_WRAPPED"}})

    http = _FakeHttp(router)
    monkeypatch.setattr("requests.post", http.post)
    api = opus_ingest.OpusAPI("k")
    assert api.create_collection("x") == "COL_WRAPPED"


def test_list_project_clips_uses_find_by_project(monkeypatch):
    _arm(monkeypatch)

    def router(method, url, params, body):
        assert url.endswith("/api/exportable-clips")
        assert params.get("q") == "findByProjectId"
        assert params.get("projectId") == "PROJ1"
        return _Resp(200, {"data": [
            {"id": "PROJ1.C1", "uriForExport": "https://cdn/1.mp4"},
            {"id": "PROJ1.C2", "uriForExport": "https://cdn/2.mp4"}]})

    http = _FakeHttp(router)
    monkeypatch.setattr("requests.get", http.get)
    api = opus_ingest.OpusAPI("k")
    clips = api.list_project_clips("PROJ1")
    assert {c["id"] for c in clips} == {"PROJ1.C1", "PROJ1.C2"}


def test_add_clip_to_collection_posts_content(monkeypatch):
    _arm(monkeypatch)

    def router(method, url, params, body):
        assert method == "POST" and url.endswith("/api/collection-contents")
        assert body == {"collectionId": "COL1", "contentId": "PROJ1.C1"}
        return _Resp(200, {"ok": True})

    http = _FakeHttp(router)
    monkeypatch.setattr("requests.post", http.post)
    api = opus_ingest.OpusAPI("k")
    api.add_clip_to_collection("COL1", "PROJ1.C1")
    assert http.posts[0][1]["contentId"] == "PROJ1.C1"


def test_add_clips_to_collection_one_post_per_clip(monkeypatch):
    _arm(monkeypatch)

    def router(method, url, params, body):
        return _Resp(200, {"ok": True})

    http = _FakeHttp(router)
    monkeypatch.setattr("requests.post", http.post)
    api = opus_ingest.OpusAPI("k")
    added = api.add_clips_to_collection("COL1", ["PROJ1.C1", "PROJ1.C2", ""])
    assert added == ["PROJ1.C1", "PROJ1.C2"]        # empty id skipped
    assert len(http.posts) == 2                       # one POST per real clip


def test_client_methods_raise_scan_error_on_non_2xx(monkeypatch):
    _arm(monkeypatch)

    def router(method, url, params, body):
        return _Resp(403, {}, text='{"error":"forbidden secret-should-not-leak"}')

    http = _FakeHttp(router)
    monkeypatch.setattr("requests.post", http.post)
    api = opus_ingest.OpusAPI("sk-secretkey")
    try:
        api.create_collection("x")
        assert False, "expected OpusScanError"
    except opus_ingest.OpusScanError as exc:
        assert exc.http_status == 403
        assert "sk-secretkey" not in str(exc)


def test_collection_id_from_response_shapes():
    f = opus_ingest._collection_id_from_response
    assert f({"collectionId": "A"}) == "A"
    assert f({"data": {"collectionId": "B"}}) == "B"
    assert f({"id": "C"}) == "C"
    assert f({"nothing": 1}) == ""
    assert f(None) == ""
