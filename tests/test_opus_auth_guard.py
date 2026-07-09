"""
Opus auth guard tests (Parts 1-4 of the scan-honest diagnostic).

Part 1: a mocked 401 makes opus-pull report AUTH ERROR, not "0 drafted".
Part 2: the key is read from the env at call time; changing the env between
        _default_api() calls produces different keys sent; the prefix is logged.
Part 3: opus-doctor surfaces key prefix, HTTP status, and project count.
Part 4: normalize_clip accepts finished-status aliases; verbose scan logs raw
        status values of excluded clips.

Fully OFFLINE. No network. No Slack. No file writes.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, opus_ingest, opus_factory  # noqa: E402


# ---- helpers -----------------------------------------------------------------------

class _FakeResponse:
    """Minimal requests.Response stand-in."""
    def __init__(self, status_code, body=""):
        self.status_code = status_code
        self.text = body

    def json(self):
        import json as _json
        return _json.loads(self.text)


def _arm_factory(monkeypatch):
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.setenv("OPUS_API_KEY", "sk-test123456")


# ---- Part 1: OpusScanError is raised and opus-pull surfaces it ----------------------

def test_opus_scan_error_on_401(monkeypatch):
    """_get raises OpusScanError with the HTTP status when the API returns 401."""
    _arm_factory(monkeypatch)
    api = opus_ingest.OpusAPI("sk-badkey")

    def _fake_get(url, params=None, headers=None, timeout=30):
        return _FakeResponse(401, '{"error":"Unauthorized"}')

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_get)
    try:
        api._get("/api/projects")
        assert False, "expected OpusScanError"
    except opus_ingest.OpusScanError as exc:
        assert exc.http_status == 401
        assert "Unauthorized" in exc.body_snippet
        # the key must not appear in the error attrs
        assert "badkey" not in str(exc)


def test_opus_pull_cli_reports_auth_error_not_zero(monkeypatch, capsys):
    """A mocked 401 makes opus-pull print AUTH ERROR, not the clean '0 drafted' line."""
    _arm_factory(monkeypatch)

    import requests as _req

    def _fake_get(url, params=None, headers=None, timeout=30):
        return _FakeResponse(401, '{"error":"Unauthorized"}')

    monkeypatch.setattr(_req, "get", _fake_get)

    opus_factory.opus_pull_cli()
    out = capsys.readouterr().out
    assert "AUTH ERROR" in out
    assert "HTTP 401" in out
    # must NOT look like a clean zero-results run
    assert "0 drafted" not in out


def test_scan_propagates_opus_scan_error(monkeypatch):
    """scan() raises OpusScanError instead of returning [] on auth failure."""
    _arm_factory(monkeypatch)

    class _FailingAPI:
        def list_collections_detailed(self):
            raise opus_ingest.OpusScanError(403, "forbidden")

    try:
        opus_factory.scan(api=_FailingAPI())
        assert False, "expected OpusScanError to propagate"
    except opus_ingest.OpusScanError as exc:
        assert exc.http_status == 403


def test_scan_still_returns_empty_on_non_auth_error(monkeypatch):
    """Non-OpusScanError exceptions in scan still return [] (old resilient behaviour
    for unexpected shapes, not auth failures)."""
    _arm_factory(monkeypatch)

    class _BrokenAPI:
        def list_collections_detailed(self):
            raise ValueError("unexpected json shape")

    result = opus_factory.scan(api=_BrokenAPI())
    assert result == []


# ---- Part 2: key read at call time, prefix logged ----------------------------------

def test_default_api_reads_key_at_call_time(monkeypatch):
    """Each _default_api() call reads OPUS_API_KEY fresh from the environment."""
    monkeypatch.setenv("OPUS_API_KEY", "key-version-one")
    api1 = opus_ingest._default_api()
    assert api1 is not None
    assert api1._key == "key-version-one"

    monkeypatch.setenv("OPUS_API_KEY", "key-version-two")
    api2 = opus_ingest._default_api()
    assert api2 is not None
    assert api2._key == "key-version-two"

    # the two instances hold the value at their construction time
    assert api1._key != api2._key


def test_default_api_logs_key_prefix(monkeypatch, capsys):
    """_default_api prints only the first 6 chars of the key, never more."""
    monkeypatch.setenv("OPUS_API_KEY", "sk-myS3cr3tKey")
    opus_ingest._default_api()
    out = capsys.readouterr().out
    assert "sk-myS" in out              # first 6 chars printed
    assert "3cr3tKey" not in out        # rest of key NOT printed


def test_opus_api_base_reads_at_call_time(monkeypatch):
    """config.opus_api_base() picks up env changes after module import."""
    monkeypatch.setenv("AGENT_OPUS_API_BASE", "https://staging.opus.example")
    assert config.opus_api_base() == "https://staging.opus.example"
    monkeypatch.setenv("AGENT_OPUS_API_BASE", "https://api.opus.pro")
    assert config.opus_api_base() == "https://api.opus.pro"


def test_opus_org_id_reads_at_call_time(monkeypatch):
    """config.opus_org_id() picks up env changes after module import."""
    monkeypatch.delenv("AGENT_OPUS_ORG_ID", raising=False)
    assert config.opus_org_id() == ""
    monkeypatch.setenv("AGENT_OPUS_ORG_ID", "org-abc123")
    assert config.opus_org_id() == "org-abc123"


def test_get_uses_current_api_base(monkeypatch):
    """_get() uses config.opus_api_base() at request time, not an import-time constant."""
    _arm_factory(monkeypatch)
    monkeypatch.setenv("AGENT_OPUS_API_BASE", "https://custom.opus.test")
    api = opus_ingest.OpusAPI("sk-testkey")
    captured_urls = []

    def _fake_get(url, params=None, headers=None, timeout=30):
        captured_urls.append(url)
        return _FakeResponse(200, "[]")

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_get)
    api._get("/api/collections")
    assert captured_urls[0].startswith("https://custom.opus.test")


# ---- Part 2: the scan hits the PROVEN routes (not the 404 /api/projects) ------------

def test_scan_hits_collections_route_not_projects(monkeypatch):
    """At the HTTP layer, the factory scan queries /api/collections and
    /api/exportable-clips, and NEVER the non-existent /api/projects path."""
    _arm_factory(monkeypatch)
    monkeypatch.delenv("AGENT_OPUS_PROJECT_IDS", raising=False)
    captured = []

    def _fake_get(url, params=None, headers=None, timeout=30):
        captured.append((url, dict(params or {})))
        if "/api/collections" in url:
            return _FakeResponse(200, '{"data":[{"id":"COL1","title":"Show"}]}')
        if "/api/exportable-clips" in url:
            return _FakeResponse(
                200,
                '{"data":[{"id":"COL1.C1","title":"Clip","durationMs":30000,'
                '"uriForExport":"https://cdn.opus/c1.mp4","score":91,'
                '"transcript":"a clip"}]}')
        return _FakeResponse(404, '{"error":"NotFoundException"}')

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_get)
    records = opus_factory.scan(api=opus_ingest.OpusAPI("sk-testkey"))
    paths = [u for u, _ in captured]
    assert any("/api/collections" in p for p in paths)
    assert any("/api/exportable-clips" in p for p in paths)
    assert not any("/api/projects" in p for p in paths)   # the 404 route is gone
    # the clip route used findByCollectionId
    clip_calls = [prm for u, prm in captured if "/api/exportable-clips" in u]
    assert clip_calls and clip_calls[0].get("q") == "findByCollectionId"
    assert {r.clip_id for r in records} == {"COL1.C1"}


# ---- Part 3: opus-doctor output -----------------------------------------------------

def test_opus_doctor_flag_off(monkeypatch, capsys):
    """opus-doctor prints a clear message when the factory flag is OFF."""
    monkeypatch.delenv("AGENT_OPUS_FACTORY_ENABLED", raising=False)
    out = opus_ingest.opus_doctor()
    printed = capsys.readouterr().out
    assert out["enabled"] is False
    assert "AGENT_OPUS_FACTORY_ENABLED" in printed


def test_opus_doctor_no_key(monkeypatch, capsys):
    """opus-doctor prints a clear message when OPUS_API_KEY is absent."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.delenv("OPUS_API_KEY", raising=False)
    out = opus_ingest.opus_doctor()
    printed = capsys.readouterr().out
    assert out["key_present"] is False
    assert "OPUS_API_KEY" in printed


def test_opus_doctor_hits_collections_not_projects(monkeypatch):
    """opus-doctor calls the proven /api/collections route, never /api/projects."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.setenv("OPUS_API_KEY", "sk-testkey99")
    captured = []

    def _fake_http_get(url, params=None, headers=None, timeout=30):
        captured.append(url)
        return _FakeResponse(200, '{"data":[]}')

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_http_get)
    opus_ingest.opus_doctor()
    assert any("/api/collections" in u for u in captured)
    assert not any("/api/projects" in u for u in captured)


def test_opus_doctor_404_is_endpoint_wrong_not_auth(monkeypatch, capsys):
    """A 404 must read as ENDPOINT WRONG, never as an auth problem."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.setenv("OPUS_API_KEY", "sk-goodkey99")

    def _fake_http_get(url, params=None, headers=None, timeout=30):
        return _FakeResponse(404, '{"error":"NotFoundException"}')

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_http_get)
    result = opus_ingest.opus_doctor()
    printed = capsys.readouterr().out
    assert result["status"] == 404
    assert result["endpoint_ok"] is False
    assert result["auth_ok"] is None            # not collapsed into auth
    assert "ENDPOINT WRONG" in printed
    assert "AUTH" not in printed.split("ENDPOINT WRONG")[0]  # not called auth


def test_opus_doctor_401_is_auth_wrong_not_endpoint(monkeypatch, capsys):
    """A 401 must read as AUTH WRONG, distinct from the 404 endpoint case."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.setenv("OPUS_API_KEY", "sk-2vtUfBADKEY")

    def _fake_http_get(url, params=None, headers=None, timeout=30):
        return _FakeResponse(401, '{"error":"Unauthorized"}')

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_http_get)
    result = opus_ingest.opus_doctor()
    printed = capsys.readouterr().out
    assert result["auth_ok"] is False
    assert result["endpoint_ok"] is True        # the route exists
    assert result["status"] == 401
    assert "AUTH WRONG" in printed
    assert "ENDPOINT WRONG" not in printed
    # key prefix shown but full key not printed
    assert "sk-2vtU" in printed
    assert "BADKEY" not in printed


def test_opus_doctor_success(monkeypatch, capsys):
    """opus-doctor prints base URL, collection count, and first collection fields
    on 200 with a real-shape response."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.setenv("OPUS_API_KEY", "sk-goodkey99")
    monkeypatch.setenv("AGENT_OPUS_API_BASE", "https://api.opus.pro")

    import json as _json

    def _fake_http_get(url, params=None, headers=None, timeout=30):
        body = _json.dumps({"data": [{"id": "COL1", "title": "Gym Marketing Made Simple",
                                      "status": "ready"}]})
        return _FakeResponse(200, body)

    import requests as _req
    monkeypatch.setattr(_req, "get", _fake_http_get)
    result = opus_ingest.opus_doctor()
    printed = capsys.readouterr().out
    assert result["auth_ok"] is True
    assert result["endpoint_ok"] is True
    assert result["collections"] == 1
    assert result["base_url"] == "https://api.opus.pro"
    assert "COL1" in printed
    assert "Gym Marketing Made Simple" in printed
    assert "https://api.opus.pro" in printed          # resolved base URL shown
    assert "ready" in printed                          # first collection raw status
    # first 6 chars shown; rest not printed
    assert "sk-goo" in printed        # "sk-goodkey99"[:6] == "sk-goo"
    assert "key99" not in printed


# ---- Part 4: finished-status filter + verbose raw-status logging --------------------

def test_normalize_clip_accepts_standard_export_url(monkeypatch):
    """A clip with uriForExport is accepted regardless of status field."""
    clip = {"id": "C1", "uriForExport": "https://cdn.opus/c1.mp4",
            "durationMs": 30000, "score": 91}
    rec = opus_factory.normalize_clip(clip, "PROJ1")
    assert rec is not None
    assert rec.clip_id == "C1"


def test_normalize_clip_accepts_export_url_alias(monkeypatch):
    """exportUrl (alternate key name) is accepted."""
    clip = {"id": "C2", "exportUrl": "https://cdn.opus/c2.mp4",
            "durationMs": 25000, "score": 88}
    rec = opus_factory.normalize_clip(clip, "PROJ1")
    assert rec is not None


def test_normalize_clip_excludes_no_url(monkeypatch):
    """A clip with no export URL is always excluded."""
    clip = {"id": "C3", "durationMs": 20000, "score": 95}
    assert opus_factory.normalize_clip(clip, "PROJ1") is None


def test_normalize_clip_excludes_processing_status(monkeypatch):
    """A clip with status='processing' and no URL is excluded."""
    clip = {"id": "C4", "durationMs": 20000, "score": 95, "status": "processing"}
    assert opus_factory.normalize_clip(clip, "PROJ1") is None


def test_scan_verbose_logs_excluded_raw_statuses(monkeypatch, capsys):
    """Verbose scan prints the raw status values of clips that were excluded."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.delenv("AGENT_OPUS_PROJECT_IDS", raising=False)

    class _FakeOpus:
        def list_collections_detailed(self):
            return [{"id": "COL1", "title": "Test"}]

        def list_exportable_clips(self, q, source_id):
            assert q == "findByCollectionId"
            return [
                {"id": "C_DONE", "uriForExport": "https://cdn.opus/done.mp4",
                 "durationMs": 30000, "score": 91},
                {"id": "C_PROC", "durationMs": 20000, "status": "processing"},
            ]

    opus_factory.scan(api=_FakeOpus(), verbose=True)
    out = capsys.readouterr().out
    assert "1 clip(s) included" in out
    assert "processing" in out


# ---- Part 4: filter matches the DOCUMENTED exportable-clips response shape ----------
# Ground truth (no live call available): the documented Opus API endpoint is
# /api/exportable-clips and, by its name and contract, returns ONLY exportable
# (finished) clips, each carrying id/title/description/durationMs/uriForExport/
# createdAt (see the legacy poller docstring). So the presence of uriForExport IS
# the finished signal; the status-alias branch in normalize_clip is a defensive
# fallback for shapes that omit the URL. If a live run ever returns a different
# shape, opus-doctor + the scan's raw-status logging surface it.

def test_documented_exportable_clip_shape_passes_filter(monkeypatch):
    """A clip with the exact documented field set normalizes and survives the
    finished-clip filter."""
    documented = {
        "id": "COL1.C1",
        "title": "The follow up problem",
        "description": "Most gyms have a follow up problem.",
        "durationMs": 38000,
        "uriForExport": "https://cdn.opus/col1c1.mp4",
        "createdAt": "2026-07-01T10:00:00Z",
        "score": 92,
        "transcript": "Most gyms do not have a lead problem.",
    }
    rec = opus_factory.normalize_clip(documented, "COL1", "Gym Marketing Made Simple")
    assert rec is not None
    assert rec.clip_id == "COL1.C1"
    assert rec.duration_s == 38.0
    assert rec.source_title == "Gym Marketing Made Simple"


def test_documented_shape_flows_through_scan(monkeypatch):
    """End to end: a collection of documented-shape clips scans into records."""
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")
    monkeypatch.delenv("AGENT_OPUS_PROJECT_IDS", raising=False)

    class _FakeOpus:
        def list_collections_detailed(self):
            return [{"id": "COL1", "title": "Gym Marketing Made Simple"}]

        def list_exportable_clips(self, q, source_id):
            return [{
                "id": "COL1.C1", "title": "Clip", "description": "d",
                "durationMs": 40000, "uriForExport": "https://cdn.opus/c.mp4",
                "createdAt": "2026-07-01T10:00:00Z", "score": 93,
                "transcript": "We book 71.9 percent of leads.",
            }]

    records = opus_factory.scan(api=_FakeOpus())
    assert {r.clip_id for r in records} == {"COL1.C1"}
    assert records[0].opus_score == 93.0
