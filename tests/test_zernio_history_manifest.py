import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import zernio_history_manifest as history


def _indexed_fixture(tmp_path):
    root = tmp_path / "evidence"
    (root / "published").mkdir(parents=True)
    page = {"rows": [
        {"id": "r1", "gym_id": "gym-a", "account": "instagram", "status": "published",
         "late_post_id": "post-exact-1", "source_media_url": None,
         "source_media_asset_id": None, "post_date": "2026-09-01"},
        {"id": "r2", "gym_id": "gym-a", "late_post_id": None,
         "source_media_url": "https://example/source.jpg", "source_media_asset_id": "asset-2"},
        {"id": "r3", "gym_id": "gym-a", "late_post_id": "../other-endpoint"},
    ]}
    raw = json.dumps(page, separators=(",", ":")).encode()
    (root / "published/page-00.json").write_bytes(raw)
    index = {"project_id": "synthetic", "snapshot_scope": "paginated fixture", "groups": {
        "published": {"files": [{"path": "published/page-00.json",
                                     "sha256": hashlib.sha256(raw).hexdigest(), "rows": 3}]}}}
    index_path = tmp_path / "MANIFEST_INDEX.json"
    index_path.write_text(json.dumps(index))
    return root, index_path


def test_offline_classifier_verifies_hash_and_uses_only_exact_late_post_id(tmp_path):
    root, index = _indexed_fixture(tmp_path)
    summary, first, second, unsafe = history.classify_index(index, root)
    assert summary["row_count"] == 3
    assert first["classification"] == "exact_post_lookup_candidate"
    assert first["late_post_id"] == "post-exact-1"
    assert second["classification"] == "missing_exact_post_id"
    assert second["late_post_id"] is None
    assert second["source_media_asset_id"] == "asset-2"
    assert second["source_lineage"] == "explicit_calendar_fields_only"
    assert unsafe["classification"] == "unsafe_exact_post_id"

    (root / "published/page-00.json").write_text('{"rows": []}')
    with pytest.raises(ValueError, match="sha256 mismatch"):
        history.classify_index(index, root)


def test_capture_is_bounded_deduplicated_resumeable_and_hashes_raw_response(tmp_path):
    records = [
        {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p1"},
        {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p1"},
        {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p2"},
        {"record_type": "row", "classification": "missing_exact_post_id", "late_post_id": None},
    ]
    ledger, raw_dir = tmp_path / "ledger.jsonl", tmp_path / "raw"
    body = b'{"post":{"_id":"p1","mediaItems":[]}}'
    calls = []

    def get_json(pid):
        calls.append(pid)
        return {"post": {"_id": pid, "mediaItems": []}}

    result = history.capture_posts(records, ledger=ledger, raw_dir=raw_dir, max_items=1,
                                   timeout=1, get_json=get_json, get_raw=lambda: body,
                                   now=lambda: "2026-10-06T00:00:00Z")
    assert calls == ["p1"]
    assert result[0]["raw_sha256"] == hashlib.sha256(body).hexdigest()
    assert Path(result[0]["raw_response_path"]).read_bytes() == body
    assert result[0]["response_hash_basis"] == "exact_http_response_body"

    calls.clear()
    result = history.capture_posts(records, ledger=ledger, raw_dir=raw_dir, max_items=5,
                                   timeout=1, get_json=get_json)
    assert calls == ["p2"]
    assert result[0]["late_post_id"] == "p2"

    # A resume marker is trusted only while the content-addressed evidence still matches.
    Path(result[0]["raw_response_path"]).write_bytes(b"tampered")
    calls.clear()
    result = history.capture_posts([records[2]], ledger=ledger, raw_dir=raw_dir, max_items=1,
                                   timeout=1, get_json=get_json)
    assert calls == ["p2"]

    # An edited ledger cannot bind this response body to a different post ID.
    ledger_lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    ledger_lines[-1]["late_post_id"] = "p3"
    ledger.write_text("\n".join(json.dumps(row) for row in ledger_lines) + "\n")
    assert "p3" not in history._completed_ids(ledger, raw_dir)


def test_capture_records_errors_but_does_not_mark_them_complete(tmp_path):
    record = {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p1"}
    ledger, raw_dir = tmp_path / "ledger.jsonl", tmp_path / "raw"
    result = history.capture_posts([record], ledger=ledger, raw_dir=raw_dir, max_items=1,
                                   timeout=1, get_json=lambda _pid: (_ for _ in ()).throw(RuntimeError("temporary")))
    assert result[0]["status"] == "error"
    assert history._completed_ids(ledger, raw_dir) == set()


def test_timeout_arguments_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        history.capture_posts([], ledger=tmp_path / "l", raw_dir=tmp_path / "r", max_items=0,
                              timeout=1, get_json=lambda _pid: {})


def test_capture_revalidates_ids_at_request_boundary(tmp_path):
    records = [{"record_type": "row", "classification": "exact_post_lookup_candidate",
                "late_post_id": "../v1/accounts"}]
    calls = []
    result = history.capture_posts(records, ledger=tmp_path / "ledger", raw_dir=tmp_path / "raw",
                                  max_items=5, timeout=1,
                                  get_json=lambda pid: calls.append(pid))
    assert calls == []
    assert result == []


def _slow_worker(marker, pid, timeout, output_dir):
    import time
    marker = Path(marker)
    while True:
        with marker.open("a") as stream:
            stream.write(pid + "\n")
        time.sleep(0.01)


def _success_worker(pid, timeout, output_dir):
    root = Path(output_dir)
    (root / "response").write_bytes(b'{"post": {"_id": "p1"}}')
    (root / "result.json").write_text('{"ok": true}')


def _error_worker(pid, timeout, output_dir):
    (Path(output_dir) / "result.json").write_text(
        '{"ok": false, "error_type": "ZernioError", "http_status": 503}')


def test_capture_enforces_wall_deadline_and_kills_worker_before_retry(tmp_path):
    import functools
    import multiprocessing
    import time
    records = [
        {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p1"},
        {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p2"},
    ]
    marker = tmp_path / "worker-activity"
    getter = history._ProcessGet(0.5, worker=functools.partial(_slow_worker, str(marker)))
    children_before = {child.pid for child in multiprocessing.active_children()}
    for _ in range(2):
        started = time.monotonic()
        result = history.capture_posts(records, ledger=tmp_path / "ledger", raw_dir=tmp_path / "raw",
                                      max_items=5, timeout=0.5, get_json=getter,
                                      get_raw=lambda: getter.raw)
        assert time.monotonic() - started < 1.5
        assert len(result) == 1 and result[0]["error_type"] == "TimeoutError"
        assert marker.exists()  # the request worker actually started before being killed
        frozen = marker.read_bytes()
        time.sleep(0.05)
        assert marker.read_bytes() == frozen
        assert set(marker.read_text().splitlines()) == {"p1"}
        assert {child.pid for child in multiprocessing.active_children()} == children_before
        assert getter.raw is None
    assert history._completed_ids(tmp_path / "ledger", tmp_path / "raw") == set()
    assert not list((tmp_path / "raw").glob("*"))


def test_process_get_preserves_raw_bytes_and_safe_error_metadata(tmp_path):
    getter = history._ProcessGet(3, worker=_success_worker)
    assert getter("p1") == {"post": {"_id": "p1"}}
    assert getter.raw == b'{"post": {"_id": "p1"}}'
    getter.worker = _error_worker
    row = {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p1"}
    result = history.capture_posts([row], ledger=tmp_path / "ledger", raw_dir=tmp_path / "raw",
                                  max_items=1, timeout=3, get_json=getter,
                                  get_raw=lambda: getter.raw)
    assert result[0]["error_type"] == "ZernioError"
    assert result[0]["http_status"] == 503
    assert getter.raw is None
    assert not list((tmp_path / "raw").glob("*"))
    with pytest.raises(ValueError, match="unsafe"):
        getter("../other")


def test_capture_rejects_response_for_different_post_id(tmp_path):
    row = {"record_type": "row", "classification": "exact_post_lookup_candidate", "late_post_id": "p1"}
    result = history.capture_posts([row], ledger=tmp_path / "ledger", raw_dir=tmp_path / "raw",
                                  max_items=1, timeout=1,
                                  get_json=lambda _pid: {"post": {"_id": "p2"}})
    assert result[0]["status"] == "error"
    assert result[0]["error_type"] == "ValueError"
    assert not list((tmp_path / "raw").glob("*"))


def test_get_adapter_caps_response_and_closes_without_other_http_methods(monkeypatch):
    import requests

    class Response:
        closed = False

        def iter_content(self, chunk_size):
            yield b"x" * history.MAX_RESPONSE_BYTES
            yield b"x"

        def close(self):
            self.closed = True

    response = Response()
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    adapter = history._RequestsGet(1)
    with pytest.raises(ValueError, match="10 MiB"):
        adapter.get("https://synthetic.invalid/v1/posts/p1")
    assert response.closed and adapter.raw is None
    assert len(calls) == 1 and calls[0][1]["stream"] is True
    assert all(not hasattr(adapter, method) for method in ("post", "put", "patch", "delete"))


def test_fetch_worker_does_not_save_provider_error_body_or_message(tmp_path, monkeypatch):
    from agent import zernio

    class Client:
        def __init__(self, http):
            pass

        def get_post(self, pid):
            raise zernio.ZernioError(503, "synthetic credential and error body")

    monkeypatch.setattr(zernio, "ZernioClient", Client)
    history._fetch_post_worker("p1", 1, str(tmp_path))
    assert list(tmp_path.iterdir()) == [tmp_path / "result.json"]
    assert json.loads((tmp_path / "result.json").read_text()) == {
        "ok": False, "error_type": "ZernioError", "http_status": 503}
