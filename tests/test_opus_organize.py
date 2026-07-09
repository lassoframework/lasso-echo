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

from agent import config, opus_ingest, opus_organize  # noqa: E402


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


# ---- Part 2: opus-organize CLI ------------------------------------------------------

class _FakeApi:
    """Records create/add calls; serves project clips and collection contents."""

    def __init__(self, project_clips, collections=None, collection_contents=None):
        self.project_clips = project_clips          # {pid: [clip dict,...]}
        self.collections = list(collections or [])  # [{"id","title"}]
        self.contents = {k: list(v) for k, v in (collection_contents or {}).items()}
        self.created = []
        self.added = []                             # (collection_id, content_id)

    def list_project_clips(self, project_id):
        return list(self.project_clips.get(project_id, []))

    def list_collections_detailed(self):
        return list(self.collections)

    def list_exportable_clips(self, q, source_id):
        assert q == "findByCollectionId"
        return [{"id": cid} for cid in self.contents.get(source_id, [])]

    def create_collection(self, name):
        new_id = f"COL_{len(self.created) + 1}"
        self.created.append((new_id, name))
        self.collections.append({"id": new_id, "title": name})
        self.contents.setdefault(new_id, [])
        return new_id

    def add_clips_to_collection(self, collection_id, clip_ids):
        out = []
        for cid in clip_ids:
            self.added.append((collection_id, cid))
            self.contents.setdefault(collection_id, []).append(cid)
            out.append(cid)
        return out


def _clip(cid, url="https://cdn/x.mp4"):
    return {"id": cid, "uriForExport": url, "durationMs": 30000}


def test_organize_flag_off(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_OPUS_FACTORY_ENABLED", raising=False)
    assert opus_organize.organize(api=_FakeApi({})) is None
    assert "OFF" in capsys.readouterr().out


def test_organize_no_pinned_projects_writes_nothing(monkeypatch, capsys):
    _arm(monkeypatch)
    monkeypatch.delenv("AGENT_OPUS_PROJECT_IDS", raising=False)
    api = _FakeApi({"projone": [_clip("projone.c1")]})
    plan = opus_organize.organize(api=api, write=True)
    assert plan["projects"] == [] and plan["added"] == []
    assert api.created == [] and api.added == []
    assert "no project ids pinned" in capsys.readouterr().out


def test_organize_dry_run_writes_nothing(monkeypatch, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_OPUS_PROJECT_IDS", "projone,projtwo")
    api = _FakeApi({"projone": [_clip("projone.c1"), _clip("projone.c2")],
                    "projtwo": [_clip("projtwo.c1")]})
    plan = opus_organize.organize(api=api, write=False)
    assert sorted(plan["to_add"]) == ["projone.c1", "projone.c2", "projtwo.c1"]
    assert api.created == [] and api.added == []          # dry run: no writes
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "score n/a" in out                              # honest: no API score


def test_organize_write_creates_and_adds(monkeypatch, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_OPUS_PROJECT_IDS", "projone")
    monkeypatch.setenv("AGENT_OPUS_PODCAST_SHOW", "Gym Marketing Made Simple")
    api = _FakeApi({"projone": [_clip("projone.c1"), _clip("projone.c2")]})
    plan = opus_organize.organize(api=api, write=True)
    assert plan["created"] is True
    assert api.created[0][1] == "Gym Marketing Made Simple"   # name from show
    assert {cid for _, cid in api.added} == {"projone.c1", "projone.c2"}
    assert plan["final_count"] == 2


def test_organize_idempotent_skips_existing(monkeypatch, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_OPUS_PROJECT_IDS", "projone")
    # collection already exists (named "LASSO Clips") and already holds projone.c1;
    # target_name passed explicitly so the test does not depend on the show default
    api = _FakeApi(
        project_clips={"projone": [_clip("projone.c1"), _clip("projone.c2")]},
        collections=[{"id": "COL9", "title": "LASSO Clips"}],
        collection_contents={"COL9": ["projone.c1"]})
    plan = opus_organize.organize(api=api, write=True, target_name="LASSO Clips")
    assert plan["created"] is False
    assert plan["collection_id"] == "COL9"                        # reused, not created
    assert plan["already_in"] == ["projone.c1"]
    assert [cid for _, cid in api.added] == ["projone.c2"]        # only the new one
    assert plan["final_count"] == 2
    # a second run adds nothing
    api.added.clear()
    plan2 = opus_organize.organize(api=api, write=True, target_name="LASSO Clips")
    assert api.added == [] and plan2["to_add"] == []


def test_organize_skips_unfinished_clips(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_OPUS_PROJECT_IDS", "projone")
    api = _FakeApi({"projone": [_clip("projone.c1"),
                           {"id": "projone.c2", "durationMs": 20000}]})  # no export url
    plan = opus_organize.organize(api=api, write=False)
    assert plan["to_add"] == ["projone.c1"]                       # unfinished excluded
