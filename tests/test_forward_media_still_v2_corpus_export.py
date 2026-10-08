"""Synthetic offline reconciliation only; no production access or approval."""
import copy
import hashlib
import json

import pytest

from agent import forward_media_still_v2_corpus_export as export
from agent.historical_published_visual_corpus import load_snapshot


def fixture():
    saved = {"schema_version": 1, "complete": True, "row_count": 2, "rows": [
        {"row_id": "still-a", "revision": "r1", "gym_id": "gym-a",
         "published_at": "2026-09-01T12:00:00+00:00", "image_url": "https://test.invalid/a"},
        {"row_id": "video-b", "revision": "r2", "gym_id": "gym-b",
         "published_at": None, "image_url": "https://test.invalid/b"},
    ]}
    normalized, saved_sha = load_snapshot(saved)
    records = {}
    for row in normalized:
        ref = export.digest([row["row_id"], row["revision"]])[7:]
        base = {"row_ref_sha256": ref, "revision_sha256": export.digest(row["revision"])[7:],
                "gym_sha256": export.digest(row["gym_id"])[7:],
                "published_date_known": row["published_at"] is not None,
                "published_date_sha256": export.digest(row["published_at"][:10] if row["published_at"] else "null")[7:]}
        # Collector hashes raw strings, NOT JSON string literals.
        for field, value in (("revision_sha256", row["revision"]), ("gym_sha256", row["gym_id"]),
                             ("published_date_sha256", row["published_at"][:10] if row["published_at"] else "null")):
            base[field] = hashlib.sha256(value.encode()).hexdigest()
        if row["row_id"] == "still-a":
            base.update(status="hashed", image_sha256="a" * 64, byte_length=13,
                        dhash64="0" * 16, width=1, height=1)
        else:
            base.update(status="unknown", unknown_reason="not_decodable_as_still")
        records[ref] = base
    manifest = {"schema_version": 1, "snapshot_sha256": saved_sha, "row_count": 2,
                "complete": True, "hashed_count": 1, "unknown_count": 1, "records": records}
    current = copy.deepcopy(saved)
    current["schema_version"] = 2
    for i, row in enumerate(current["rows"]):
        row["calendar_visual_digest"] = "sha256:" + str(i + 1) * 64
    reviews = {"schema_version": 1, "snapshot_sha256": saved_sha,
               "manifest_digest": export.digest(manifest), "records": []}
    for row in current["rows"]:
        record = {"row_ref_sha256": export.digest([row["row_id"], row["revision"]])[7:],
                  "calendar_visual_digest": row["calendar_visual_digest"],
                  "published_binding_ref": "calendar:" + row["row_id"],
                  "review_evidence_ref": "SYNTHETIC saved explicit classification"}
        if row["row_id"] == "still-a":
            record.update(media_kind="still_photo", inspected_sha256="sha256:" + "a" * 64,
                          immutable_bytes_ref="SYNTHETIC frozen historical bytes")
        else:
            record.update(media_kind="video", video_classification_ref="SYNTHETIC individually reviewed video identity")
        reviews["records"].append(record)
    return current, saved, manifest, reviews


def reconcile(inputs):
    return export.reconcile(*inputs, expected_cohort_digest=export.cohort_digest(inputs[0]))


def test_unchanged_evidence_proposes_exact_still_and_individual_video_exclusion():
    result = reconcile(fixture())
    assert result["scope_complete"] is False and result["policy_approved"] is False
    assert result["independent_approval_required"] and result["claims_reconciliation_required"]
    assert result["candidate_absence_means_unused"] is False
    assert result["declared_full_fleet_row_count"] == 2 and result["hold_counts"] == {}
    still, video = result["rows_json"][0], result["excluded_rows_json"][0]
    assert still["history_key"] == "calendar:still-a"
    assert still["visual_sha256"] == "sha256:" + "a" * 64 and still["resolved"] is True
    assert video["media_kind"] == "reviewed_video_scope_exclusion"
    assert video["visual_sha256"] is None and video["video_frames_reviewed"] is False
    assert result["candidate_worksheet"]["candidate"] is None
    assert all(r["candidate_disposition"] is None for r in result["candidate_worksheet"]["rows"])


@pytest.mark.parametrize("field,value", [("revision", "new-version"), ("image_url", "https://test.invalid/changed"),
    ("gym_id", "another-tenant"), ("published_at", "2026-09-02T12:00:00Z")])
def test_changed_identity_version_url_tenant_or_date_never_reuses_sha(field, value):
    inputs = fixture()
    inputs[0]["rows"][0][field] = value
    result = reconcile(inputs)
    row = result["rows_json"][0]
    assert row["resolved"] is False and row["visual_sha256"] is None
    assert row["hold_reason"] == "identity_revision_tenant_date_or_url_changed"


def test_new_current_rows_are_explicit_unresolved_not_silently_dropped():
    inputs = fixture()
    row = copy.deepcopy(inputs[0]["rows"][0])
    row["row_id"] = "new-c"
    inputs[0]["rows"].append(row)
    inputs[0]["row_count"] = 3
    result = reconcile(inputs)
    assert result["declared_full_fleet_row_count"] == 3
    assert len(result["rows_json"]) + len(result["excluded_rows_json"]) == 3
    added = next(r for r in result["rows_json"] if r["history_key"] == "calendar:new-c")
    assert added["hold_reason"] == "added_since_saved_snapshot" and added["visual_sha256"] is None


def test_removed_saved_rows_report_hash_only_and_require_new_scope_review():
    inputs = fixture()
    inputs[0]["rows"] = inputs[0]["rows"][:1]
    inputs[0]["row_count"] = 1
    result = reconcile(inputs)
    assert result["removed_saved_row_refs_sha256"] == [export.digest(["video-b", "r2"])]
    assert result["excluded_rows_json"] == [] and result["scope_complete"] is False


@pytest.mark.parametrize("kind", ["unknown", "carousel", "animated_gif", None, "image/jpeg"])
def test_ambiguous_and_header_based_classification_never_excludes_or_binds_sha(kind):
    inputs = fixture()
    inputs[3]["records"][0]["media_kind"] = kind
    row = reconcile(inputs)["rows_json"][0]
    assert row["hold_reason"] == "unknown_or_ambiguous_media_kind"
    assert row["media_kind"] == "unknown" and row["visual_sha256"] is None


@pytest.mark.parametrize("field,value", [("calendar_visual_digest", "sha256:" + "9" * 64),
    ("published_binding_ref", "calendar:wrong"), ("review_evidence_ref", "")])
def test_stale_or_missing_review_binding_holds(field, value):
    inputs = fixture()
    inputs[3]["records"][0][field] = value
    row = reconcile(inputs)["rows_json"][0]
    assert row["hold_reason"] == "review_visual_digest_or_binding_stale" and row["visual_sha256"] is None


@pytest.mark.parametrize("field,value", [("inspected_sha256", "sha256:" + "b" * 64), ("immutable_bytes_ref", "")])
def test_still_requires_exact_inspected_byte_hash_and_immutable_evidence_ref(field, value):
    inputs = fixture()
    inputs[3]["records"][0][field] = value
    assert reconcile(inputs)["rows_json"][0]["hold_reason"] == "review_inspected_bytes_unbound"


def test_missing_byte_record_and_review_are_holds_not_a_photo_approval():
    inputs = fixture()
    ref = inputs[3]["records"][0]["row_ref_sha256"]
    del inputs[2]["records"][ref]
    inputs[2].update(complete=False, hashed_count=0)
    inputs[3]["manifest_digest"] = export.digest(inputs[2])
    assert reconcile(inputs)["rows_json"][0]["hold_reason"] == "saved_immutable_byte_evidence_missing_or_unknown"
    inputs[3]["records"] = inputs[3]["records"][1:]
    assert reconcile(inputs)["rows_json"][0]["hold_reason"] == "review_evidence_missing"


def test_video_exclusion_requires_explicit_individual_classification():
    inputs = fixture()
    del inputs[3]["records"][1]["video_classification_ref"]
    result = reconcile(inputs)
    assert result["excluded_rows_json"] == []
    assert result["rows_json"][1]["hold_reason"] == "video_classification_evidence_missing"


@pytest.mark.parametrize("url", [None, "", "http://test.invalid/a", "https://user:pass@test.invalid/a", "https://test.invalid/a#part"])
def test_missing_or_ambiguous_url_never_binds_saved_bytes(url):
    inputs = fixture()
    # Both snapshots agree on the bad URL; the URL must still be usable.
    inputs[0]["rows"][0]["image_url"] = url
    inputs[1]["rows"][0]["image_url"] = url
    _, snapshot_sha = load_snapshot(inputs[1])
    inputs[2]["snapshot_sha256"] = snapshot_sha
    inputs[3]["snapshot_sha256"] = snapshot_sha
    inputs[3]["manifest_digest"] = export.digest(inputs[2])
    row = reconcile(inputs)["rows_json"][0]
    assert row["hold_reason"] == "published_url_missing_or_ambiguous" and row["visual_sha256"] is None


@pytest.mark.parametrize("location", ["current", "saved", "reviews"])
def test_duplicate_identity_and_review_rejected_as_ambiguous(location):
    inputs = fixture()
    if location == "reviews":
        inputs[3]["records"].append(copy.deepcopy(inputs[3]["records"][0]))
    else:
        obj = inputs[0 if location == "current" else 1]
        row = copy.deepcopy(obj["rows"][0])
        row["revision"] = "second-version"
        obj["rows"].append(row)
        obj["row_count"] += 1
    with pytest.raises(export.CorpusExportHold, match="duplicate"):
        reconcile(inputs)


def test_unanchored_current_and_stale_saved_manifest_or_review_rejected():
    inputs = fixture()
    with pytest.raises(export.CorpusExportHold, match="stale_or_substituted"):
        export.reconcile(*inputs, expected_cohort_digest="sha256:" + "0" * 64)
    inputs[2]["snapshot_sha256"] = "0" * 64
    with pytest.raises(export.CorpusExportHold, match="byte_manifest_stale"):
        reconcile(inputs)
    inputs = fixture()
    inputs[3]["manifest_digest"] = "sha256:" + "0" * 64
    with pytest.raises(export.CorpusExportHold, match="review_manifest_stale"):
        reconcile(inputs)


@pytest.mark.parametrize("mutation", ["incomplete", "wrong_count", "missing_digest", "oversized", "schema_bool"])
def test_invalid_or_truncated_current_cohort_refused(mutation):
    inputs = fixture()
    current = inputs[0]
    if mutation == "incomplete": current["complete"] = False
    elif mutation == "wrong_count": current["row_count"] = 1
    elif mutation == "missing_digest": del current["rows"][0]["calendar_visual_digest"]
    elif mutation == "schema_bool": current["schema_version"] = True
    else:
        current["rows"] = [dict(current["rows"][0], row_id=str(i)) for i in range(export.MAX_CORPUS_ROWS + 1)]
        current["row_count"] = len(current["rows"])
    with pytest.raises(export.CorpusExportHold):
        reconcile(inputs)


def test_sql_export_contract_preserves_legacy_revision_and_requires_frozen_digest():
    raw = {"id": "still-a", "gym_id": "gym-a", "status": "published",
           "published_at": None, "image_url": "https://test.invalid/a", "late_post_id": None,
           "post_date": "2026-09-01", "source_media_asset_id": None, "source_media_url": None}
    frozen = {"schema_version": 2, "complete": True, "row_count": 1,
              "rows": [{"legacy_projection": raw, "calendar_visual_digest": "sha256:" + "7" * 64}]}
    frozen["db_rows_json"] = json.dumps(frozen["rows"])
    frozen["db_cohort_digest"] = "sha256:" + hashlib.sha256(frozen["db_rows_json"].encode()).hexdigest()
    current = export.cohort_from_sql_export(frozen, expected_db_cohort_digest=frozen["db_cohort_digest"])
    assert current["rows"][0]["revision"] == export.digest(raw)[7:]
    assert current["rows"][0]["calendar_visual_digest"] == "sha256:" + "7" * 64
    with pytest.raises(export.CorpusExportHold, match="frozen_sql_export_invalid"):
        export.cohort_from_sql_export(frozen, expected_db_cohort_digest="sha256:" + "0" * 64)
    tampered = copy.deepcopy(frozen)
    tampered["rows"][0]["calendar_visual_digest"] = "sha256:" + "0" * 64
    with pytest.raises(export.CorpusExportHold, match="frozen_sql_export_digest_mismatch"):
        export.cohort_from_sql_export(tampered, expected_db_cohort_digest=frozen["db_cohort_digest"])
    tampered = copy.deepcopy(frozen)
    tampered["db_rows_json"] += " "
    with pytest.raises(export.CorpusExportHold, match="frozen_sql_export_digest_mismatch"):
        export.cohort_from_sql_export(tampered, expected_db_cohort_digest=frozen["db_cohort_digest"])
    sql = export.CURRENT_COHORT_SQL
    assert "FROM public.content_calendar" in sql and "sha256(convert_to(visual_content::text" in sql
    assert "ORDER BY id" in sql and "LIMIT" not in sql and "<=2500" in sql
    assert all(x not in sql.lower() for x in ("insert ", "update ", "delete "))


def test_cli_private_proposal_and_hash_only_stdout_refuses_overwrite(tmp_path, capsys):
    inputs = fixture()
    argv = []
    for option, obj in zip(("current", "saved-snapshot", "saved-manifest", "reviews"), inputs):
        path = tmp_path / (option + ".json")
        path.write_text(json.dumps(obj))
        path.chmod(0o600)
        argv.extend(["--" + option, str(path)])
    output = tmp_path / "proposal.json"
    argv.extend(["--expected-cohort-digest", export.cohort_digest(inputs[0]), "--output", str(output)])
    assert export.main(argv) == 0
    stdout = capsys.readouterr().out
    assert "https://" not in stdout and "still-a" not in stdout and "gym-a" not in stdout
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["scope_complete"] is False
    frozen = output.read_bytes()
    assert export.main(argv) == 2 and output.read_bytes() == frozen


def test_private_json_refuses_duplicate_keys_public_permissions_and_nonfinite(tmp_path):
    path = tmp_path / "input.json"
    for content, reason in [(' {"records":{},"records":{}}', "duplicate_key"), ('{"value":NaN}', "nonfinite")]:
        path.write_text(content)
        path.chmod(0o600)
        with pytest.raises(export.CorpusExportHold, match=reason):
            export.load_private_json(path)
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(export.CorpusExportHold, match="permissions_not_private"):
        export.load_private_json(path)
