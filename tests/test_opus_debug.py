"""
Opus debug visibility tests. Offline. Asserts: --verbose prints discovery route,
per-source counts, and a WHY for every skipped clip; the API key never appears in
any output; opus-check handles 200-with-empty-body, 401, and non-JSON bodies and
scrubs the key from anything echoed back.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, opus_ingest  # noqa: E402
from tests.test_opus_ingest import CLIP_A, CLIP_B, FakeOpus, FakeS3, _arm  # noqa: E402


# ---- verbose pull -----------------------------------------------------------------
def test_verbose_prints_counts_and_filter_reasons(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("OPUS_API_KEY", "opus-secret-key-99999")
    api = FakeOpus()
    opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib, verbose=True)
    first = capsys.readouterr().out
    assert "discovery: collections endpoint" in first
    assert "1 collection id(s)" in first
    assert "scanning 1 source(s)" in first
    assert "2 clip(s) listed" in first

    # second pull: every clip now reports WHY it was skipped
    opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib, verbose=True)
    second = capsys.readouterr().out
    assert "SKIPPED" in second
    assert ("watermark" in second) or ("already ingested" in second)
    # the key never appears in verbose output either run
    assert "opus-secret-key-99999" not in first + second


def test_verbose_reports_missing_export_url(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    broken = dict(CLIP_A)
    broken.pop("uriForExport")
    opus_ingest.pull(api=FakeOpus(clips=[broken]), s3_client=FakeS3(),
                     out_dir=lib, verbose=True)
    out = capsys.readouterr().out
    assert "not exportable (missing id or export URL)" in out


def test_non_verbose_stays_quiet(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    opus_ingest.pull(api=FakeOpus(), s3_client=FakeS3(), out_dir=lib)
    out = capsys.readouterr().out
    assert "discovery" not in out and "SKIPPED" not in out


# ---- opus-check -------------------------------------------------------------------
class _Resp:
    def __init__(self, status, text="", json_body=None):
        self.status_code = status
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _Http:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self.resp


def _key(monkeypatch):
    monkeypatch.setenv("OPUS_API_KEY", "opus-secret-key-99999")


def test_opus_check_200_empty(monkeypatch, capsys):
    _key(monkeypatch)
    out = opus_ingest.opus_check(http=_Http(_Resp(200, text='{"data": []}',
                                                  json_body={"data": []})))
    printed = capsys.readouterr().out
    assert out == {"status": 200, "collections": 0}
    assert "HTTP 200" in printed and "0 collection(s)" in printed
    assert "raw body" in printed                      # zero -> show the body
    assert "opus-secret-key-99999" not in printed


def test_opus_check_401(monkeypatch, capsys):
    _key(monkeypatch)
    body = '{"error": "unauthorized, key opus-secret-key-99999 rejected"}'
    out = opus_ingest.opus_check(http=_Http(_Resp(401, text=body,
                                                  json_body={"error": "unauthorized"})))
    printed = capsys.readouterr().out
    assert out["status"] == 401
    assert "HTTP 401" in printed and "raw body" in printed
    # a key echoed back BY THE SERVER is scrubbed before printing
    assert "opus-secret-key-99999" not in printed


def test_opus_check_non_json(monkeypatch, capsys):
    _key(monkeypatch)
    out = opus_ingest.opus_check(http=_Http(_Resp(200, text="<html>gateway</html>")))
    printed = capsys.readouterr().out
    assert out["collections"] is None
    assert "not JSON" in printed and "gateway" in printed


def test_opus_check_without_key(monkeypatch, capsys):
    monkeypatch.delenv("OPUS_API_KEY", raising=False)
    out = opus_ingest.opus_check(http=_Http(_Resp(200)))
    assert out == {"status": None, "collections": None}
    assert "not set" in capsys.readouterr().out


# ---- part G: discovery paths + honest empty messaging ------------------------------
def test_pinned_project_ids_path(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", ["P1", "P2"])
    monkeypatch.setattr(config, "OPUS_COLLECTION_IDS", [])

    class PinnedApi(FakeOpus):
        def list_collections(self):
            raise AssertionError("collections listed despite pinned ids")

        def list_exportable_clips(self, q, source_id):
            assert q == "findByProjectId" and source_id in ("P1", "P2")
            return []

    opus_ingest.pull(api=PinnedApi(), s3_client=FakeS3(), out_dir=lib, verbose=True)
    out = capsys.readouterr().out
    assert "PINNED ids (2 project" in out
    assert "scanned 2 source(s), zero new clips" in out    # clear, never silent


def test_collection_discovery_path(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", [])
    monkeypatch.setattr(config, "OPUS_COLLECTION_IDS", [])
    api = FakeOpus()                                       # one collection, two clips
    out = opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert out["pulled"] == 2                              # collections path pulls


def test_zero_sources_prints_remediation(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", [])
    monkeypatch.setattr(config, "OPUS_COLLECTION_IDS", [])

    class EmptyApi(FakeOpus):
        def list_collections(self):
            return []

    opus_ingest.pull(api=EmptyApi(), s3_client=FakeS3(), out_dir=lib)
    out = capsys.readouterr().out
    assert "ZERO sources" in out
    assert "Projects are not collections" in out
    assert "AGENT_OPUS_PROJECT_IDS" in out                 # the exact env var named


def test_opus_check_remediation_per_case(monkeypatch, capsys):
    _key(monkeypatch)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", [])
    # case: 200 empty, nothing pinned -> the projects-are-not-collections fix
    opus_ingest.opus_check(http=_Http(_Resp(200, text='{"data": []}',
                                            json_body={"data": []})))
    out = capsys.readouterr().out
    assert "REMEDIATION" in out and "AGENT_OPUS_PROJECT_IDS" in out
    # case: 200 empty but ids pinned -> fine, scan directly
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", ["P1"])
    opus_ingest.opus_check(http=_Http(_Resp(200, text='{"data": []}',
                                            json_body={"data": []})))
    out = capsys.readouterr().out
    assert "pinned project ids will be scanned" in out
    # case: 401 -> key remediation naming the env vars
    opus_ingest.opus_check(http=_Http(_Resp(401, text='{"error":"x"}',
                                            json_body={"error": "x"})))
    out = capsys.readouterr().out
    assert "OPUS_API_KEY" in out and "AGENT_OPUS_ORG_ID" in out
    # case: collections visible -> READY
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", [])
    opus_ingest.opus_check(http=_Http(_Resp(200, text='{"data":[{"id":"C1"}]}',
                                            json_body={"data": [{"id": "C1"}]})))
    assert "READY" in capsys.readouterr().out
