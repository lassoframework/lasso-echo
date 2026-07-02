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
