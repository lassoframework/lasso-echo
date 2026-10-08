import hashlib
import json
import os

import pytest

from agent.historical_media_evidence_collect import (
    EvidenceCollectionError,
    _drive_reader,
    collect_pilot,
    write_manifest,
)


def _fixtures(count=5):
    rows, assets = [], []
    delivered, originals = {}, {}
    for i in range(count):
        rid, aid, gym = f"row-{i}", f"drive-file-{i}", f"gym-{i}"
        rows.append({
            "id": rid, "gym_id": gym, "status": "published",
            "post_date": "2025-01-01", "published_at": "2025-01-01T00:00:00Z",
            "late_post_id": f"provider-{i}", "source_media_asset_id": aid,
            "source_media_url": f"https://source.example/{aid}?token=secret",
            "image_url": f"https://media.example/{rid}?signature=secret",
        })
        assets.append({"id": aid, "source_id": f"source-{i}", "gym_id": gym})
        delivered[rid] = f"same bytes {i}".encode()
        originals[aid] = (f"same bytes {i}" if i < count - 1 else "transformed").encode()
    sources = [{"id": f"source-{i}", "gym_id": f"gym-{i}",
                "kind": "gym_drive", "folder_id": f"folder-{i}"}
               for i in range(count)]
    return {"rows": rows}, {"rows": assets}, {"rows": sources}, delivered, originals


def test_pilot_records_only_hashes_refs_and_unresolved_candidate_matches():
    calendar, asset_snapshot, source_snapshot, delivered, originals = _fixtures()
    manifest = collect_pilot(
        calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
        delivered_reader=lambda url: delivered[url.rsplit("/", 1)[-1].split("?", 1)[0]],
        drive_reader=lambda file_id: originals[file_id],
    )
    assert manifest["summary"] == {
        "candidate_exact_byte_match": 4, "unknown": 1, "cleared": 0}
    assert all(row["decision"] == "unresolved" for row in manifest["rows"])
    assert manifest["rows"][-1]["classification"] == "unknown"
    serialized = json.dumps(manifest)
    assert "secret" not in serialized
    assert "same bytes" not in serialized
    assert "transformed" not in serialized
    assert manifest["rows"][0]["delivered"]["sha256"] == hashlib.sha256(b"same bytes 0").hexdigest()


@pytest.mark.parametrize("count", [0, 4, 11])
def test_refuses_out_of_bounds_pilot_size(count):
    calendar, asset_snapshot, source_snapshot, _, _ = _fixtures(max(count, 5))
    with pytest.raises(EvidenceCollectionError, match="pilot_must_name_5_to_10"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(count)],
                      delivered_reader=lambda _: b"x", drive_reader=lambda _: b"x")


def test_tenant_mismatch_aborts_without_returning_partial_coverage():
    calendar, asset_snapshot, source_snapshot, delivered, originals = _fixtures()
    asset_snapshot["rows"][2]["gym_id"] = "wrong-gym"
    with pytest.raises(EvidenceCollectionError, match="tenant_mismatch"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
                      delivered_reader=lambda url: delivered[url.rsplit("/", 1)[-1].split("?", 1)[0]],
                      drive_reader=lambda file_id: originals[file_id])


def test_unavailable_exact_bytes_aborts_closed():
    calendar, asset_snapshot, source_snapshot, _, originals = _fixtures()
    with pytest.raises(EvidenceCollectionError, match="object_read_failed"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
                      delivered_reader=lambda _: (_ for _ in ()).throw(RuntimeError("secret")),
                      drive_reader=lambda file_id: originals[file_id])


def test_missing_source_registry_ref_fails_closed():
    calendar, asset_snapshot, source_snapshot, delivered, originals = _fixtures()
    asset_snapshot["rows"][1]["source_id"] = None
    with pytest.raises(EvidenceCollectionError, match="drive_source_id_missing"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
                      delivered_reader=lambda url: delivered[url.rsplit("/", 1)[-1].split("?", 1)[0]],
                      drive_reader=lambda file_id: originals[file_id])


def test_source_mapping_must_exist_and_match_gym_before_any_read():
    calendar, asset_snapshot, source_snapshot, _, _ = _fixtures()
    source_snapshot["rows"][3]["gym_id"] = "other-gym"
    calls = []
    with pytest.raises(EvidenceCollectionError, match="media_source_tenant_mismatch"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
                      delivered_reader=lambda _: calls.append("delivered") or b"bytes",
                      drive_reader=lambda _: calls.append("drive") or b"bytes")
    assert calls == []


def test_all_rows_publication_and_urls_are_validated_before_first_read():
    calendar, asset_snapshot, source_snapshot, _, _ = _fixtures()
    calendar["rows"][4]["published_at"] = "not-a-date"
    calls = []
    with pytest.raises(EvidenceCollectionError, match="selected_row_not_published"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
                      delivered_reader=lambda _: calls.append("delivered") or b"bytes",
                      drive_reader=lambda _: calls.append("drive") or b"bytes")
    assert calls == []


def test_published_status_required_even_with_timestamp():
    calendar, asset_snapshot, source_snapshot, _, _ = _fixtures()
    calendar["rows"][0]["status"] = "approved"
    with pytest.raises(EvidenceCollectionError, match="selected_row_not_published"):
        collect_pilot(calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
                      delivered_reader=lambda _: b"bytes", drive_reader=lambda _: b"bytes")


def test_manifest_is_atomic_owner_only_and_contains_no_media():
    from pathlib import Path
    import stat
    calendar, asset_snapshot, source_snapshot, delivered, originals = _fixtures()
    manifest = collect_pilot(
        calendar, asset_snapshot, source_snapshot, [f"row-{i}" for i in range(5)],
        delivered_reader=lambda url: delivered[url.rsplit("/", 1)[-1].split("?", 1)[0]],
        drive_reader=lambda file_id: originals[file_id],
    )
    target = Path(os.getcwd()) / ".pytest_cache" / "historical-pilot.json"
    write_manifest(target, manifest)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text()) == manifest
    target.unlink()


def test_drive_reader_rejects_large_metadata_before_download(monkeypatch):
    class Files:
        def get(self, **kwargs):
            class Request:
                def execute(self):
                    return {"id": "file", "size": "6", "trashed": False}
            return Request()

    class Service:
        def files(self):
            return Files()

    class Transport:
        def __init__(self):
            self.downloaded = False
        def _service(self):
            return Service()
        def download_to(self, file_id, sink):
            self.downloaded = True

    transport = Transport()
    class Client:
        def available(self):
            return True
        def _t(self):
            return transport

    monkeypatch.setattr("agent.historical_media_evidence_collect.MAX_OBJECT_BYTES", 5)
    with pytest.raises(EvidenceCollectionError, match="object_too_large_or_empty"):
        _drive_reader("file", client_factory=Client)
    assert not transport.downloaded


def test_drive_reader_stream_enforces_limit_even_if_metadata_is_stale(monkeypatch):
    class Files:
        def get(self, **kwargs):
            class Request:
                def execute(self):
                    return {"id": "file", "size": "4", "trashed": False}
            return Request()

    class Service:
        def files(self):
            return Files()

    class Transport:
        def _service(self):
            return Service()
        def download_to(self, file_id, sink):
            sink.write(b"1234")
            sink.write(b"56")

    class Client:
        def available(self):
            return True
        def _t(self):
            return Transport()

    monkeypatch.setattr("agent.historical_media_evidence_collect.MAX_OBJECT_BYTES", 5)
    with pytest.raises(EvidenceCollectionError, match="object_too_large"):
        _drive_reader("file", client_factory=Client)
