import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from exact_byte_cutover_audit import _load, audit

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "exact_byte_cutover_audit.py"

H = "a" * 64
H2 = "b" * 64
REV = "sha256:" + "c" * 64
REV2 = "sha256:" + "d" * 64


def _digest(prefix):
    return "sha256:" + prefix * 64


def _row(row_id="row-1", gym="gym-1", status="published", image_url="https://cdn.example.com/a.jpg",
         post_date="2026-09-01", thumbnail_url=None, revision=REV):
    row = {
        "id": row_id, "gym_id": gym, "account": "instagram", "format": "feed",
        "post_date": post_date, "status": status, "published_at": "2026-09-01T12:00:00Z",
        "late_post_id": None, "image_url": image_url, "thumbnail_url": thumbnail_url,
        "row_revision": revision,
    }
    return row


def _obs(obs_id="obs-1", digest=None, url="https://cdn.example.com/a.jpg",
         tenant="tenant-a", post_date="2026-09-01", row="row-1", revision=REV,
         kind="db_published_url_bytes"):
    return {
        "observation_id": obs_id, "calendar_row_id": row, "row_revision": revision,
        "canonical_tenant": tenant, "post_date": post_date,
        "shared_posting_identity": None, "exact_url": url,
        "sha256": digest or _digest("a"), "byte_length": 1234,
        "observation_kind": kind, "evidence_ref": "evidence://op/1",
        "observed_at": "2026-10-10T00:00:00Z", "recorded_at": "2026-10-10T00:00:01Z",
    }


def _coverage(row_id="row-1", revision=REV, complete=True, no_images=False):
    return {
        "calendar_row_id": row_id, "row_revision": revision, "complete": complete,
        "no_images_verified": no_images, "evidence_ref": "evidence://cov/1",
    }


def _sibling_proof():
    return {
        "shared_posting_identity": "11111111-1111-1111-1111-111111111111",
        "canonical_tenant": "tenant-a", "post_date": "2026-09-01",
        "logical_post_id": "22222222-2222-2222-2222-222222222222",
        "evidence_ref": "evidence://sibling/1",
    }


def _sibling_member(row_id, revision=REV):
    return {
        "calendar_row_id": row_id, "row_revision": revision,
        "shared_posting_identity": "11111111-1111-1111-1111-111111111111",
        "evidence_ref": "evidence://sibling/1",
    }


def _export(**over):
    data = {
        "tenants": [{
            "calendar_gym_id": "gym-1", "canonical_tenant": "tenant-a",
            "posting_timezone": "America/Indiana/Indianapolis",
            "evidence_ref": "evidence://tenant/1",
        }],
        "targets": [{
            "calendar_gym_id": "gym-1", "account": "instagram", "format": "feed",
            "provider_target": {"provider": "meta", "platform": "instagram",
                                "account_id": "ig-1"},
            "image_fields": ["image_url"], "evidence_ref": "evidence://target/1",
        }],
        "sibling_proofs": [], "sibling_members": [],
        "coverage": [_coverage()], "observations": [_obs()], "outcomes": [],
        "calendar_rows": [_row()],
        "cutover": {
            "runtime_commit": "e" * 40,
            "deployment_evidence_ref": "evidence://deploy/1",
            "complete_history_evidence_ref": "evidence://history/1",
        },
    }
    data.update(over)
    if "census" not in over:
        rows = data["calendar_rows"]
        data["census"] = {
            "calendar_rows": len(rows),
            "published_rows": sum(1 for r in rows
                                  if r.get("status") == "published"
                                  or r.get("published_at") is not None
                                  or r.get("late_post_id") is not None),
        }
    return data


def _codes(report):
    return sorted({b["code"] for b in report["blockers"]})


def test_complete_seed_is_ready():
    report = audit(_load(json.dumps(_export())))
    assert report["ready"] is True
    assert report["blockers"] == []
    assert report["counts"]["published_rows"] == 1
    assert report["counts"]["observation_only_db_published"] == 1
    assert report["notes"]  # db-published observation flagged as not a provider receipt


def test_cross_tenant_duplicate_digest_blocks():
    data = _export(observations=[
        _obs("obs-1", digest=_digest("a"), tenant="tenant-a", post_date="2026-09-01"),
        _obs("obs-2", digest=_digest("a"), tenant="tenant-b", post_date="2026-09-02",
             row="row-2", revision=REV2, url="https://cdn.example.com/b.jpg"),
    ])
    report = audit(_load(json.dumps(data)))
    assert "cross_date_digest_conflict" in _codes(report)
    assert report["ready"] is False


def test_same_digest_same_day_sibling_proof_passes():
    # Two rows sharing ONE exact digest on one posting day are one logical
    # post; proven sibling membership satisfies the requirement.
    shared = "https://cdn.example.com/shared.jpg"
    data = _export(
        observations=[
            _obs("obs-1", digest=_digest("a"), url=shared, row="row-1"),
            _obs("obs-2", digest=_digest("a"), url=shared, row="row-2"),
        ],
        calendar_rows=[_row("row-1", image_url=shared),
                       _row("row-2", image_url=shared)],
        coverage=[_coverage("row-1"), _coverage("row-2")],
        sibling_proofs=[_sibling_proof()],
        sibling_members=[_sibling_member("row-1"), _sibling_member("row-2")],
    )
    report = audit(_load(json.dumps(data)))
    assert "missing_sibling_evidence" not in _codes(report)
    assert report["ready"] is True


def test_same_digest_same_day_without_sibling_proof_blocks():
    shared = "https://cdn.example.com/shared.jpg"
    data = _export(
        observations=[
            _obs("obs-1", digest=_digest("a"), url=shared, row="row-1"),
            _obs("obs-2", digest=_digest("a"), url=shared, row="row-2"),
        ],
        calendar_rows=[_row("row-1", image_url=shared),
                       _row("row-2", image_url=shared)],
        coverage=[_coverage("row-1"), _coverage("row-2")],
    )
    report = audit(_load(json.dumps(data)))
    assert "missing_sibling_evidence" in _codes(report)
    assert report["ready"] is False


def test_same_day_distinct_digests_do_not_require_sibling_proof():
    # False-hold regression: distinct posts (distinct digests) on the same
    # tenant/day must NOT be forced into sibling evidence.
    data = _export(
        observations=[
            _obs("obs-1", digest=_digest("a"), row="row-1"),
            _obs("obs-2", digest=_digest("b"), url="https://cdn.example.com/b.jpg",
                 row="row-2"),
        ],
        calendar_rows=[_row("row-1"),
                       _row("row-2", image_url="https://cdn.example.com/b.jpg")],
        coverage=[_coverage("row-1"), _coverage("row-2")],
    )
    report = audit(_load(json.dumps(data)))
    assert "missing_sibling_evidence" not in _codes(report)
    assert report["ready"] is True


def test_unknown_target_blocks():
    data = _export(targets=[{
        "calendar_gym_id": "gym-9", "account": "instagram", "format": "feed",
        "provider_target": {"provider": "meta", "platform": "instagram",
                            "account_id": "ig-9"},
        "image_fields": ["image_url"], "evidence_ref": "evidence://target/9",
    }])
    report = audit(_load(json.dumps(data)))
    assert "unknown_target_tenant" in _codes(report)
    assert "missing_provider_target" in _codes(report)
    assert report["ready"] is False


def test_uncertain_send_blocks():
    data = _export(observations=[
        _obs("obs-1"),
        _obs("obs-2", digest=_digest("f"), url="https://cdn.example.com/f.jpg",
             row="row-2", kind="uncertain_bytes"),
    ], calendar_rows=[_row("row-1"), _row("row-2")])
    report = audit(_load(json.dumps(data)))
    assert "uncertain_send" in _codes(report)
    assert report["counts"]["uncertain_attempts"] == 1
    assert report["ready"] is False


def test_malformed_export_fails_closed():
    for bad in ('{"tenants": "nope"}', '{"observations": [{"sha256": 1}]}', "not json"):
        try:
            _load(bad)
        except Exception:
            pass
        else:
            raise AssertionError("malformed export accepted: %r" % bad)
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "/dev/stdin"], input="not json",
        capture_output=True, text=True)
    assert out.returncode == 2
    assert json.loads(out.stdout)["ready"] is False


def test_missing_observation_and_cutover_block():
    data = _export(observations=[], cutover=None)
    report = audit(_load(json.dumps(data)))
    assert "missing_observation" in _codes(report)
    assert "missing_cutover" in _codes(report)


def test_missing_required_export_keys_rejected():
    # False-ready regression: absent authority arrays were silently treated
    # as empty; every expected array key must be present.
    for missing in ("calendar_rows", "observations", "tenants", "coverage"):
        data = _export()
        del data[missing]
        try:
            _load(json.dumps(data))
        except Exception:
            pass
        else:
            raise AssertionError("export without %r accepted" % missing)


def test_missing_zero_or_mismatched_census_blocks():
    # No census at all.
    data = _export()
    del data["census"]
    report = audit(_load(json.dumps(data)))
    assert "incomplete_export" in _codes(report)
    assert report["ready"] is False

    # Zero census (empty snapshot) must never read as ready.
    data = _export(calendar_rows=[], observations=[], census={"calendar_rows": 0,
                                                              "published_rows": 0})
    report = audit(_load(json.dumps(data)))
    assert "incomplete_export" in _codes(report)
    assert report["ready"] is False

    # Frozen census that disagrees with the exported rows.
    data = _export(census={"calendar_rows": 5, "published_rows": 5})
    report = audit(_load(json.dumps(data)))
    assert "census_mismatch" in _codes(report)
    assert report["ready"] is False


def test_stale_observation_revision_does_not_satisfy_current_row():
    # False-ready regression: observation recorded against an older row
    # revision must not satisfy the current published row.
    data = _export(observations=[_obs("obs-1", revision=REV2)])
    report = audit(_load(json.dumps(data)))
    assert "missing_observation" in _codes(report)
    assert report["ready"] is False


def test_coverage_revision_is_authoritative_for_observation_match():
    # False-ready regression: the exported row revision is the current
    # authority. Coverage frozen at a different (stale) revision must block
    # even when an observation matches the row's own revision.
    data = _export(coverage=[_coverage("row-1", revision=REV2)],
                   observations=[_obs("obs-1", revision=REV)])
    report = audit(_load(json.dumps(data)))
    assert "coverage_revision_mismatch" in _codes(report)
    assert report["ready"] is False


def test_published_row_without_coverage_blocks():
    # False-ready regression: the SQL authority requires exact-byte history
    # coverage for every published row, even when byte observations exist.
    data = _export(coverage=[])
    report = audit(_load(json.dumps(data)))
    assert "missing_coverage" in _codes(report)
    assert report["ready"] is False


def test_stale_coverage_revision_blocks_despite_matching_observation():
    # False-ready regression: coverage at a stale revision must not make a
    # published row ready, even with a matching current observation.
    data = _export(coverage=[_coverage("row-1", revision=REV2)],
                   observations=[_obs("obs-1", revision=REV2)])
    report = audit(_load(json.dumps(data)))
    assert "coverage_revision_mismatch" in _codes(report)
    assert report["ready"] is False


def test_unsupported_media_columns_block_even_with_coverage_and_observation():
    # False-ready regression: historical slide_urls are unsupported media
    # columns rejected by the SQL authority for every published row,
    # regardless of coverage or image_url observations.
    row = _row()
    row["slide_urls"] = ["https://cdn.example.com/old/1.jpg",
                         "https://cdn.example.com/old/2.jpg"]
    data = _export(calendar_rows=[row])
    report = audit(_load(json.dumps(data)))
    assert "unsupported_media" in _codes(report)
    assert report["ready"] is False


def test_image_free_published_row_requires_explicit_no_images_coverage():
    row = _row(image_url=None)
    data = _export(calendar_rows=[row], coverage=[_coverage(no_images=False)],
                   observations=[])
    report = audit(_load(json.dumps(data)))
    assert "unverified_no_images" in _codes(report)
    assert report["ready"] is False


def test_thumbnail_only_published_row_blocks_even_when_no_images_is_verified():
    row = _row(image_url=None, thumbnail_url="https://cdn.example.com/thumb.jpg")
    data = _export(calendar_rows=[row], coverage=[_coverage(no_images=True)],
                   observations=[])
    report = audit(_load(json.dumps(data)))
    assert "unsupported_thumbnail_only" in _codes(report)
    assert report["ready"] is False


def test_empty_object_in_alternate_media_column_blocks():
    row = _row()
    row["media_items"] = {}
    data = _export(calendar_rows=[row])
    report = audit(_load(json.dumps(data)))
    assert "unsupported_media" in _codes(report)
    assert report["ready"] is False


def test_whitespace_padded_image_url_does_not_match_trimmed_observation():
    row = _row(image_url=" https://cdn.example.com/a.jpg ")
    data = _export(calendar_rows=[row], observations=[_obs()])
    report = audit(_load(json.dumps(data)))
    assert "missing_observation" in _codes(report)
    assert report["ready"] is False


def test_terminal_success_after_reserved_uncertain_is_resolved():
    # Append-only attempts always start reserved_uncertain; a later
    # provider_accepted outcome is the deterministic latest and must not block.
    data = _export(outcomes=[
        {"attempt_id": "att-1", "outcome": "reserved_uncertain",
         "evidence_ref": "evidence://out/1", "recorded_at": "2026-10-01T00:00:00Z",
         "outcome_id": "out-1"},
        {"attempt_id": "att-1", "outcome": "provider_accepted",
         "evidence_ref": "evidence://out/2", "recorded_at": "2026-10-01T00:01:00Z",
         "outcome_id": "out-2"},
    ])
    report = audit(_load(json.dumps(data)))
    assert "uncertain_send" not in _codes(report)
    assert "ambiguous_outcome" not in _codes(report)
    assert report["ready"] is True


def test_unresolved_latest_uncertain_outcome_blocks():
    # An old provider_accepted followed by a later uncertain outcome means the
    # latest outcome is unresolved and must block.
    data = _export(outcomes=[
        {"attempt_id": "att-1", "outcome": "provider_accepted",
         "evidence_ref": "evidence://out/1", "recorded_at": "2026-10-01T00:00:00Z",
         "outcome_id": "out-1"},
        {"attempt_id": "att-1", "outcome": "uncertain",
         "evidence_ref": "evidence://out/2", "recorded_at": "2026-10-01T00:01:00Z",
         "outcome_id": "out-2"},
    ])
    report = audit(_load(json.dumps(data)))
    assert "uncertain_send" in _codes(report)
    assert report["counts"]["uncertain_attempts"] == 1
    assert report["ready"] is False


def test_outcome_ordering_compares_timezone_normalized_instants():
    # Lexical ordering would incorrectly select the older accepted result:
    # 01:00 at +02:00 is 23:00Z, before the newer uncertain 00:30Z.
    data = _export(outcomes=[
        {"attempt_id": "att-1", "outcome": "provider_accepted",
         "evidence_ref": "evidence://out/1", "recorded_at": "2026-10-01T01:00:00+02:00",
         "outcome_id": "out-1"},
        {"attempt_id": "att-1", "outcome": "uncertain",
         "evidence_ref": "evidence://out/2", "recorded_at": "2026-10-01T00:30:00Z",
         "outcome_id": "out-2"},
    ])
    report = audit(_load(json.dumps(data)))
    assert "uncertain_send" in _codes(report)
    assert report["ready"] is False


def test_malformed_or_naive_outcome_timestamp_blocks():
    for recorded_at in ("not-a-timestamp", "2026-10-01T00:30:00"):
        data = _export(outcomes=[{
            "attempt_id": "att-1", "outcome": "provider_accepted",
            "evidence_ref": "evidence://out/1", "recorded_at": recorded_at,
            "outcome_id": "out-1",
        }])
        report = audit(_load(json.dumps(data)))
        assert "invalid_outcome" in _codes(report)
        assert report["ready"] is False


def test_ambiguous_tied_latest_outcomes_block():
    # False-ready regression: identical ordering evidence (recorded_at plus
    # outcome_id) with different outcomes cannot pick a latest outcome and
    # must fail closed.
    data = _export(outcomes=[
        {"attempt_id": "att-1", "outcome": "reserved_uncertain",
         "evidence_ref": "evidence://out/1", "recorded_at": "2026-10-01T00:00:00Z"},
        {"attempt_id": "att-1", "outcome": "provider_accepted",
         "evidence_ref": "evidence://out/2", "recorded_at": "2026-10-01T00:00:00Z"},
    ])
    report = audit(_load(json.dumps(data)))
    assert "ambiguous_outcome" in _codes(report)
    assert "uncertain_send" not in _codes(report)
    assert report["ready"] is False


def test_missing_current_revision_blocks():
    row = _row()
    del row["row_revision"]
    data = _export(calendar_rows=[row])
    report = audit(_load(json.dumps(data)))
    assert "missing_row_revision" in _codes(report)
    assert report["ready"] is False


def test_duplicate_coverage_rows_block():
    data = _export(coverage=[_coverage("row-1"), _coverage("row-1")])
    report = audit(_load(json.dumps(data)))
    assert "duplicate_coverage_row" in _codes(report)
    assert report["ready"] is False


def test_duplicate_observation_ids_block():
    data = _export(observations=[_obs("obs-1"), _obs("obs-1", url="https://cdn.example.com/b.jpg")])
    report = audit(_load(json.dumps(data)))
    assert "duplicate_observation" in _codes(report)
    assert report["ready"] is False


def test_blank_canonical_tenant_blocks():
    data = _export(tenants=[{
        "calendar_gym_id": "gym-1", "canonical_tenant": "  ",
        "posting_timezone": "America/Indiana/Indianapolis",
        "evidence_ref": "evidence://tenant/1",
    }])
    report = audit(_load(json.dumps(data)))
    assert "invalid_tenant" in _codes(report)
    assert report["ready"] is False


def test_observation_cross_row_reference_validation():
    # Observation pointing at a row id that is not in the export.
    data = _export(observations=[_obs("obs-1", row="row-ghost")])
    report = audit(_load(json.dumps(data)))
    assert "unknown_observation_row" in _codes(report)
    assert "missing_observation" in _codes(report)
    assert report["ready"] is False

    # Observation whose tenant/date disagree with the referenced row.
    data = _export(observations=[_obs("obs-1", tenant="tenant-b",
                                      post_date="2026-09-02")])
    report = audit(_load(json.dumps(data)))
    assert "observation_tenant_mismatch" in _codes(report)
    assert "observation_date_mismatch" in _codes(report)
    assert report["ready"] is False


def test_incomplete_coverage_still_blocks():
    data = _export(coverage=[_coverage("row-1", complete=False)])
    report = audit(_load(json.dumps(data)))
    assert "incomplete_coverage" in _codes(report)
    assert report["ready"] is False


def test_cli_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_export()))
    out = subprocess.run([sys.executable, str(SCRIPT), str(good)],
                         capture_output=True, text=True)
    assert out.returncode == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_export(observations=[])))
    out = subprocess.run([sys.executable, str(SCRIPT), str(bad)],
                         capture_output=True, text=True)
    assert out.returncode == 1
