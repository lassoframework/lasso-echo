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
    assert row["provider_post_id"] is None
    assert row["late_post_id"] is None
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
    assert row["local_bytes_read"] is True
    assert row["delivered_bytes_verified"] is False
    assert row["source_lineage_verified"] is False
    assert "source_reference_missing" in row["unresolved_reasons"]
    assert "missing_post_id" in row["unresolved_reasons"]
    assert "delivered_object_identity_unverified" in row["unresolved_reasons"]


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


def test_source_id_and_url_are_separate_and_must_match_same_tenant_asset(tmp_path):
    media_dir = tmp_path / "bytes"
    media_dir.mkdir()
    (media_dir / "same").write_bytes(b"delivered")
    (media_dir / "mismatch").write_bytes(b"delivered")
    digest = hashlib.md5(b"delivered").hexdigest()
    snapshot = {
        "rows": [
            {"id": "same", "gym_id": "g", "post_date": "2026-08-12",
             "late_post_id": "post-1", "source_media_asset_id": "a1",
             "source_media_url": "https://drive.example/a1", "image_url": "https://r2/same"},
            {"id": "mismatch", "gym_id": "g", "post_date": "2026-08-13",
             "source_media_asset_id": "a1", "source_media_url": "https://drive.example/a2",
             "image_url": "https://r2/mismatch"},
        ],
        "assets": [
            {"asset_id": "a1", "gym_id": "g", "source_media_url": "https://drive.example/a1",
             "content_hash": digest},
            {"asset_id": "a2", "gym_id": "g", "source_media_url": "https://drive.example/a2",
             "content_hash": digest},
        ],
    }
    _, matched, mismatched = manifest.build_manifest(snapshot, media_dir)
    assert matched["source_references"] == [
        {"field": "source_media_asset_id", "value": "a1"},
        {"field": "source_media_url", "value": "https://drive.example/a1"},
    ]
    assert "ambiguous_source_reference" not in matched["unresolved_reasons"]
    assert matched["source_lineage_evidence"]["asset_ids"] == ["a1"]
    assert matched["source_lineage_verified"] is False
    assert "source_asset_id_url_mismatch" in mismatched["unresolved_reasons"]
    assert mismatched["source_lineage_evidence"]["asset_ids"] == []
    assert mismatched["local_bytes_read"] is True
    assert mismatched["delivered_bytes_verified"] is False
    assert mismatched["source_lineage_verified"] is False


def test_missing_or_ambiguous_date_and_post_id_are_explicit(tmp_path):
    media_dir = tmp_path / "bytes"
    media_dir.mkdir()
    snapshot = {"rows": [
        {"id": "r1", "gym_id": "g", "post_date": "2026-08-01", "date": "2026-08-02"},
        {"id": "r2", "gym_id": "g", "late_post_id": "post-2"},
    ]}
    _, ambiguous, missing = manifest.build_manifest(snapshot, media_dir)
    assert ambiguous["date"] == "2026-08-01"
    assert "ambiguous_post_date" in ambiguous["unresolved_reasons"]
    assert "missing_post_id" in ambiguous["unresolved_reasons"]
    assert missing["date"] is None
    assert "missing_post_date" in missing["unresolved_reasons"]
    assert missing["provider_post_id"] == "post-2"
    assert "missing_post_id" not in missing["unresolved_reasons"]


def test_content_address_match_does_not_verify_that_bytes_were_fetched(tmp_path):
    media_dir = tmp_path / "bytes"
    media_dir.mkdir()
    correct = b"correct delivered bytes"
    wrong = b"different saved bytes"
    (media_dir / "correct").write_bytes(correct)
    (media_dir / "wrong").write_bytes(wrong)
    (media_dir / "legacy").write_bytes(correct)
    correct_prefix = hashlib.sha1(correct).hexdigest()[:16]
    wrong_prefix = hashlib.sha1(b"other content").hexdigest()[:16]
    snapshot = {"rows": [
        {"id": "correct", "gym_id": "Gym A", "post_date": "2026-08-01",
         "image_url": f"https://media.example/prefix/echo/gym-a/{correct_prefix}/image.jpg"},
        {"id": "wrong", "gym_id": "Gym A", "post_date": "2026-08-02",
         "image_url": f"https://media.example/echo/gym-a/{wrong_prefix}/image.jpg"},
        {"id": "legacy", "gym_id": "Gym A", "post_date": "2026-08-03",
         "image_url": "https://media.example/legacy/image.jpg"},
    ]}

    _, verified, wrong_bytes, legacy = manifest.build_manifest(snapshot, media_dir)
    assert verified["local_bytes_read"] is True
    assert verified["delivered_content_address_matches"] is True
    assert verified["delivered_bytes_verified"] is False
    assert "delivered_object_identity_unverified" in verified["unresolved_reasons"]
    assert wrong_bytes["local_bytes_read"] is True
    assert wrong_bytes["delivered_content_address_matches"] is False
    assert wrong_bytes["delivered_bytes_verified"] is False
    assert "delivered_object_identity_unverified" in wrong_bytes["unresolved_reasons"]
    assert legacy["local_bytes_read"] is True
    assert legacy["delivered_content_address_matches"] is False
    assert legacy["delivered_bytes_verified"] is False
    assert "delivered_object_identity_unverified" in legacy["unresolved_reasons"]
    assert manifest.build_manifest(snapshot, media_dir)[0]["delivered_content_address_match_count"] == 1
