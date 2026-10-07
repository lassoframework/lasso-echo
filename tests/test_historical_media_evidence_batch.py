import json
import os
import stat

import pytest

from agent.historical_media_evidence_batch import (
    BatchError,
    _partition,
    run_batches,
)


def _fixtures(count):
    calendar, assets, sources = [], [], []
    for i in range(count):
        row_id, asset_id, source_id, gym_id = (
            f"row-{i}", f"drive-{i}", f"source-{i}", f"gym-{i}")
        calendar.append({"id": row_id, "gym_id": gym_id, "status": "published",
                         "published_at": "2025-01-01T00:00:00Z",
                         "post_date": "2025-01-01", "late_post_id": f"post-{i}",
                         "source_media_asset_id": asset_id,
                         "source_media_url": f"https://source.invalid/{i}",
                         "image_url": f"https://media.invalid/{i}"})
        assets.append({"id": asset_id, "source_id": source_id, "gym_id": gym_id})
        sources.append({"id": source_id, "gym_id": gym_id, "kind": "gym_drive",
                         "folder_id": f"folder-{i}"})
    return {"rows": calendar}, {"rows": assets}, {"rows": sources}, {
        "row_ids": [row["id"] for row in calendar]}


def test_partition_respects_5_to_10_rows_and_rebalances_tail():
    assert [len(batch) for batch in _partition([str(i) for i in range(11)], 10)] == [6, 5]
    sizes = [len(batch) for batch in _partition([str(i) for i in range(517)], 10)]
    assert len(sizes) == 52 and min(sizes) >= 5 and max(sizes) <= 10
    assert sum(sizes) == 517


def test_whole_allowlist_validates_before_any_reader_or_output(tmp_path):
    calendar, assets, sources, allowlist = _fixtures(11)
    calendar["rows"][10]["published_at"] = None
    reads = []
    out = tmp_path / "not-created"
    with pytest.raises(Exception, match="selected_row_not_published"):
        run_batches(calendar, assets, sources, allowlist, out,
                    delivered_reader=lambda url: reads.append(url) or b"delivered",
                    drive_reader=lambda file_id: reads.append(file_id) or b"original")
    assert reads == []
    assert not out.exists()


def test_writes_hash_only_private_manifests_and_resumes_completed_batches(tmp_path):
    calendar, assets, sources, allowlist = _fixtures(11)
    calls = []
    def delivered(url):
        calls.append(("delivered", url))
        return b"same object"
    def original(file_id):
        calls.append(("original", file_id))
        return b"same object"

    out = tmp_path / "private" / "evidence"
    first = run_batches(calendar, assets, sources, allowlist, out,
                        delivered_reader=delivered, drive_reader=original)
    assert first["selected_rows"] == 11
    assert first["batch_count"] == 2
    assert first["resumed_batches"] == 0
    assert first["cleared"] == 0
    prior_calls = len(calls)
    assert prior_calls == 22
    files = sorted(out.glob("batch-*.json"))
    assert len(files) == 2
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert data["cleared"] == 0
        assert all(row["decision"] == "unresolved" for row in data["evidence"]["rows"])
        assert "same object" not in path.read_text()

    second = run_batches(calendar, assets, sources, allowlist, out,
                         delivered_reader=delivered, drive_reader=original)
    assert second["resumed_batches"] == 2
    assert len(calls) == prior_calls


def test_source_mismatch_in_final_batch_prevents_all_remote_reads(tmp_path):
    calendar, assets, sources, allowlist = _fixtures(11)
    sources["rows"][10]["gym_id"] = "other-gym"
    calls = []
    with pytest.raises(Exception, match="media_source_tenant_mismatch"):
        run_batches(calendar, assets, sources, allowlist, tmp_path / "out",
                    delivered_reader=lambda url: calls.append(url) or b"delivered",
                    drive_reader=lambda file_id: calls.append(file_id) or b"original")
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_corrupt_existing_manifest_fails_before_any_remote_read(tmp_path):
    calendar, assets, sources, allowlist = _fixtures(5)
    out = tmp_path / "out"
    run_batches(calendar, assets, sources, allowlist, out,
                delivered_reader=lambda _: b"d", drive_reader=lambda _: b"o")
    manifest = next(out.glob("batch-*.json"))
    manifest.write_text("not json")
    os.chmod(manifest, 0o600)
    calls = []
    with pytest.raises(BatchError, match="existing_manifest_unreadable"):
        run_batches(calendar, assets, sources, allowlist, out,
                    delivered_reader=lambda url: calls.append(url) or b"d",
                    drive_reader=lambda file_id: calls.append(file_id) or b"o")
    assert calls == []


@pytest.mark.parametrize("batch_size", [4, 11])
def test_batch_size_is_bounded(batch_size):
    with pytest.raises(BatchError, match="batch_size_must_be_5_to_10"):
        _partition([str(i) for i in range(10)], batch_size)
