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


def _delivery_fixture(tmp_path):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), (80, 100, 120)).save(buffer, format='PNG')
    data = buffer.getvalue()
    (tmp_path / 'saved.png').write_bytes(data)
    raw_hash = 'a' * 64
    obj = {'object_receipt_id': f'sha256:{raw_hash}:post:0',
           'exact_url': 'https://media.example/original.png', 'raw_sha256': raw_hash,
           'media_scope': 'post', 'media_index': 0}
    receipt = {'record_type': 'delivery_receipt', 'row_id': 'r1', 'gym_id': 'g',
               'post_date': '2026-08-01', 'account': 'instagram', 'late_post_id': 'p1',
               'delivery_status': 'verified_provider_delivery', 'delivered_objects': [obj],
               'raw_receipts': [{'raw_sha256': raw_hash}]}
    saved = {'object_receipt_id': obj['object_receipt_id'], 'exact_url': obj['exact_url'],
             'local_filename': 'saved.png', 'sha256': hashlib.sha256(data).hexdigest()}
    return receipt, saved, data


def test_delivery_bytes_hash_once_preserve_each_row_and_date(tmp_path):
    import copy
    row, saved, data = _delivery_fixture(tmp_path)
    sibling = copy.deepcopy(row)
    sibling.update(row_id='r2', post_date='2026-08-02', gym_id='another-gym')
    summary, first, second = manifest.build_delivery_byte_manifest([row, sibling], tmp_path, [saved])
    assert summary['input_row_count'] == summary['manifest_row_count'] == 2
    assert summary['unique_object_read_count'] == 1
    assert first['post_date'] != second['post_date']
    assert first['gym_id'] != second['gym_id']
    obj = first['objects'][0]
    assert obj['sha256'] == hashlib.sha256(data).hexdigest()
    assert obj['byte_length'] == len(data)
    assert len(obj['phash']) == 16
    assert obj['phash_algorithm'] == 'echo-dct-phash64-v1'
    assert obj['local_bytes_receipt_bound'] is True
    assert obj['delivered_bytes_verified'] is False
    assert first['source_lineage_verified'] is False
    assert first['clearance_authorized'] is False
    assert 'immutable_source_use_receipt_missing' in first['unresolved_reasons']
    assert 'saved_byte_acquisition_not_independently_verified' in first['unresolved_reasons']
    assert first['raw_receipts'] == row['raw_receipts']


def test_saved_receipt_hash_and_url_mismatch_never_verify(tmp_path):
    row, saved, _ = _delivery_fixture(tmp_path)
    wrong_hash = dict(saved, sha256='b' * 64)
    _, reviewed = manifest.build_delivery_byte_manifest([row], tmp_path, [wrong_hash])
    assert 'saved_object_sha256_mismatch' in reviewed['unresolved_reasons']
    assert reviewed['objects'][0]['local_bytes_receipt_bound'] is False
    wrong_url = dict(saved, exact_url='https://media.example/different.png')
    summary, reviewed = manifest.build_delivery_byte_manifest([row], tmp_path, [wrong_url])
    assert summary['unique_object_read_count'] == 0
    assert 'saved_object_url_mismatch' in reviewed['unresolved_reasons']


def test_conflicting_saved_or_provider_identity_holds_all_rows(tmp_path):
    import copy
    row, saved, _ = _delivery_fixture(tmp_path)
    summary, result = manifest.build_delivery_byte_manifest(
        [row], tmp_path, [saved, dict(saved, local_filename='other.png')])
    assert summary['unique_object_read_count'] == 0
    assert 'conflicting_object_receipt_binding' in result['unresolved_reasons']
    second = copy.deepcopy(row)
    second['row_id'] = 'r2'
    second['delivered_objects'][0]['exact_url'] = 'https://media.example/other.png'
    summary, *results = manifest.build_delivery_byte_manifest([row, second], tmp_path, [saved])
    assert summary['unique_object_read_count'] == 0
    assert all('conflicting_object_receipt_binding' in r['unresolved_reasons'] for r in results)


def test_unsafe_missing_and_oversize_bytes_hold(tmp_path):
    row, saved, data = _delivery_fixture(tmp_path)
    for filename in ('../saved.png', 'missing.png'):
        _, reviewed = manifest.build_delivery_byte_manifest(
            [row], tmp_path, [dict(saved, local_filename=filename)])
        assert 'missing_or_unsafe_local_bytes' in reviewed['unresolved_reasons']
    outside = tmp_path.parent / 'outside.png'
    outside.write_bytes(data)
    (tmp_path / 'outside-link').symlink_to(outside)
    _, reviewed = manifest.build_delivery_byte_manifest(
        [row], tmp_path, [dict(saved, local_filename='outside-link')])
    assert 'missing_or_unsafe_local_bytes' in reviewed['unresolved_reasons']
    _, reviewed = manifest.build_delivery_byte_manifest([row], tmp_path, [saved], max_bytes=10)
    assert 'local_bytes_exceed_limit' in reviewed['unresolved_reasons']
    assert reviewed['objects'][0].get('sha256') is None


def test_undecodable_bytes_do_not_invent_scene(tmp_path):
    row, saved, _ = _delivery_fixture(tmp_path)
    data = b'not an image'
    (tmp_path / 'saved.png').write_bytes(data)
    saved['sha256'] = hashlib.sha256(data).hexdigest()
    _, reviewed = manifest.build_delivery_byte_manifest([row], tmp_path, [saved])
    assert reviewed['objects'][0]['sha256'] == saved['sha256']
    assert reviewed['objects'][0]['phash'] is None
    assert 'image_scene_decode_unavailable' in reviewed['unresolved_reasons']


def test_unresolved_and_malformed_receipts_remain_accounted_for(tmp_path):
    row, saved, _ = _delivery_fixture(tmp_path)
    row['delivery_status'] = 'unresolved'
    row['unresolved_reason'] = 'delivery_precedence_ambiguous'
    summary, unresolved, malformed = manifest.build_delivery_byte_manifest(
        [{'record_type': 'delivery_summary'}, row, None], tmp_path, [saved])
    assert summary['input_row_count'] == summary['manifest_row_count'] == 2
    assert summary['unique_object_read_count'] == 0
    assert unresolved['provider_unresolved_reason'] == 'delivery_precedence_ambiguous'
    assert 'provider_delivery_unresolved' in unresolved['unresolved_reasons']
    assert malformed['unresolved_reasons'] == ['malformed_delivery_receipt']


def test_multiple_delivered_objects_and_duplicate_rows_preserved(tmp_path):
    import copy
    row, saved, _ = _delivery_fixture(tmp_path)
    extra = copy.deepcopy(row['delivered_objects'][0])
    extra.update(object_receipt_id='sha256:' + 'a' * 64 + ':post:1', media_index=1)
    row['delivered_objects'].append(extra)
    summary, first, second = manifest.build_delivery_byte_manifest([row, row], tmp_path, [saved])
    assert len(first['objects']) == len(second['objects']) == 2
    assert first['objects'][1]['media_index'] == 1
    assert 'saved_object_receipt_missing' in first['unresolved_reasons']
    assert 'duplicate_row_id' in first['unresolved_reasons']
    assert summary['unique_object_read_count'] == 1


def test_delivery_cli_and_missing_saved_contract(tmp_path):
    row, saved, _ = _delivery_fixture(tmp_path)
    source, saved_path, output = tmp_path / 'receipts.jsonl', tmp_path / 'saved.json', tmp_path / 'out.jsonl'
    source.write_text(json.dumps(row) + '\n')
    saved_path.write_text(json.dumps([saved]))
    args = ['--delivery-receipts', '--input', str(source), '--media-dir', str(tmp_path),
            '--output', str(output)]
    assert manifest.main(args) == 2
    assert not output.exists()
    assert manifest.main(args + ['--saved-objects', str(saved_path)]) == 0
    summary, result = [json.loads(line) for line in output.read_text().splitlines()]
    assert summary['snapshot_identity']['saved_objects_sha256'] == hashlib.sha256(saved_path.read_bytes()).hexdigest()
    assert result['objects'][0]['local_bytes_receipt_bound'] is True


def test_invalid_receipt_identity_and_unknown_account_remain_unresolved(tmp_path):
    row, saved, _ = _delivery_fixture(tmp_path)
    row['account_binding_status'] = 'unresolved_no_explicit_calendar_account'
    row['delivered_objects'][0]['media_index'] = 99
    summary, reviewed = manifest.build_delivery_byte_manifest([row], tmp_path, [saved])
    assert summary['unique_object_read_count'] == 0
    assert 'invalid_provider_object_receipt' in reviewed['unresolved_reasons']
    assert 'provider_account_binding_unresolved' in reviewed['unresolved_reasons']
    assert reviewed['account_binding_status'] == row['account_binding_status']


def test_decode_pixel_limit_retains_byte_hash_without_scene(tmp_path, monkeypatch):
    from PIL import Image
    row, saved, _ = _delivery_fixture(tmp_path)
    class OversizedImage:
        size = (manifest.DELIVERY_IMAGE_MAX_PIXELS + 1, 1)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(Image, 'open', lambda *args, **kwargs: OversizedImage())
    _, reviewed = manifest.build_delivery_byte_manifest([row], tmp_path, [saved])
    assert reviewed['objects'][0]['sha256'] == saved['sha256']
    assert reviewed['objects'][0]['phash'] is None
    assert 'image_scene_decode_unavailable' in reviewed['unresolved_reasons']
