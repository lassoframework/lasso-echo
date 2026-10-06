import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import historical_media_recovery_manifest as manifest


def test_manifest_separates_delivered_candidates_from_source_lineage(tmp_path):
    media_dir = tmp_path / "bytes"
    media_dir.mkdir()
    delivered = b"hosted rendition bytes"
    (media_dir / "row-1").write_bytes(delivered)
    digest = hashlib.md5(delivered).hexdigest()
    snapshot = {
        "published_rows": [{
            "id": "row-1", "gym_id": "gym-a", "post_date": "2026-08-12",
            "provider": "instagram", "image_url": "https://r2.example/delivered.jpg",
        }],
        "media_assets": [{
            "asset_id": "asset-a", "gym_id": "gym-a", "content_hash": digest,
            "source_media_url": "https://drive.example/original.jpg",
        }],
    }

    records = manifest.build_manifest(snapshot, media_dir, {"snapshot": "calendar-7"})
    summary, row = records
    assert summary["input_row_count"] == summary["manifest_row_count"] == 1
    assert row["row_ref"] == "row-1"
    assert row["gym_id"] == "gym-a"
    assert row["date"] == "2026-08-12"
    assert row["provider"] == "instagram"
    assert row["delivered_bytes"] == {
        "status": "read", "md5": digest, "byte_length": len(delivered),
    }
    assert row["candidate_matches"] == {
        "basis": "delivered_md5_vs_asset_content_hash",
        "cardinality": 1, "asset_ids": ["asset-a"],
    }
    # A unique R2-byte hash candidate and delivered URL do not establish that
    # the Drive source URL was used for this row.
    assert row["source_lineage_evidence"]["asset_ids"] == []
    assert row["source_lineage_evidence"]["status"] == "source_reference_missing"
    assert "source_reference_missing" in row["unresolved_reasons"]


def test_manifest_reports_malformed_and_duplicate_rows_and_assets(tmp_path):
    media_dir = tmp_path / "bytes"
    media_dir.mkdir()
    (media_dir / "repeat").write_bytes(b"bytes")
    snapshot = {
        "published_rows": [
            {"id": "repeat", "gym_id": "gym-a", "image_url": "https://r2.example/x"},
            {"id": "repeat", "gym_id": "gym-a", "image_url": "https://r2.example/x"},
            None,
        ],
        "media_assets": [
            {"asset_id": "a", "gym_id": "gym-a", "content_hash": "bad"},
            {"asset_id": "a", "gym_id": "gym-a", "content_hash": "bad"},
            "malformed",
        ],
    }
    records = manifest.build_manifest(snapshot, media_dir)
    summary, *rows = records
    assert summary["input_row_count"] == summary["manifest_row_count"] == 3
    assert summary["malformed_row_count"] == 1
    assert summary["duplicate_row_ref_count"] == 2
    assert summary["malformed_asset_count"] == 3
    assert summary["duplicate_asset_id_count"] == 1
    assert rows[-1]["unresolved_reasons"] == ["malformed_row"]
    assert all("duplicate_row_ref" in row["unresolved_reasons"] for row in rows[:2])


def test_cli_writes_replayable_jsonl_with_snapshot_hash(tmp_path):
    media_dir = tmp_path / "bytes"
    media_dir.mkdir()
    raw = json.dumps({"rows": [{"id": "absent", "gym_id": "g"}]}).encode()
    source = tmp_path / "snapshot.json"
    source.write_bytes(raw)
    output = tmp_path / "manifest.jsonl"

    assert manifest.main(["--input", str(source), "--media-dir", str(media_dir),
                          "--output", str(output)]) == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["snapshot_identity"] == {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert records[1]["delivered_bytes"]["reason"] == "missing_or_unsafe_local_bytes"
