"""Tests for scripts/visual_legacy_obligation_manifest.py (read-only)."""

import hashlib
import importlib.util
import json
import os

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), os.pardir,
                      "scripts", "visual_legacy_obligation_manifest.py")
_spec = importlib.util.spec_from_file_location(
    "visual_legacy_obligation_manifest", SCRIPT)
vlom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vlom)

ASSET_FIELDS = {f: None for f in vlom.ASSET_REQUIRED_FIELDS}
PUB_FIELDS = {f: None for f in vlom.PUBLISHED_REQUIRED_FIELDS}
PROJECT = "test-project"


def _asset(i, gym="gymA", source_gym="gymA"):
    row = dict(ASSET_FIELDS)
    row.update({
        "id": f"asset-{i:03d}", "gym_id": gym, "source_gym_id": source_gym,
        "source_id": f"src-{i:03d}", "source_kind": "gym_drive",
        "kind": "photo", "review_status": "approved",
        "content_hash": "h", "review_content_hash": "h",
        "source_active": True, "eligible": True, "used_count": 0,
        "sync_status": "ready", "rendition_key": None, "rendition_url": None,
    })
    return row


def _pub(i, gym="gymA", asset_id=None, url=None):
    row = dict(PUB_FIELDS)
    row.update({
        "id": f"pub-{i:03d}", "gym_id": gym, "account": "googlebusiness",
        "status": "published", "image_url": "https://example.invalid/x.jpg",
        "source_media_asset_id": asset_id, "source_media_url": url,
        "published_at": "2026-09-10 00:00:00+00", "variant_status": "active",
        "publish_claim_token": None,
    })
    return row


def _write_evidence(root, asset_rows, pub_rows, asset_pages=1, pub_pages=1):
    root = str(root)
    manifest = {"assembled_utc": "2026-10-04T00:00:00+00:00",
                "project_id": PROJECT, "groups": {}}

    def _emit(group, kind, rows, npages):
        per = (len(rows) + npages - 1) // npages
        os.makedirs(os.path.join(root, group), exist_ok=True)
        files = []
        offset = 0
        for p in range(npages):
            chunk = rows[p * per:(p + 1) * per]
            page = {"snapshot_utc": f"2026-10-04T00:00:0{p}Z",
                    "project_id": PROJECT, "kind": kind,
                    "offset": offset, "rows": chunk}
            rel = f"{group}/page-{p:02d}.json"
            data = json.dumps(page, sort_keys=True).encode()
            with open(os.path.join(root, rel), "wb") as fh:
                fh.write(data)
            files.append({"path": rel,
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "rows": len(chunk),
                          "snapshot_utc": page["snapshot_utc"]})
            offset += len(chunk)
        manifest["groups"][group] = {
            "files": files, "rows": len(rows), "unique_ids": len(rows)}

    _emit("assets", "approved_photo_inventory", asset_rows, asset_pages)
    _emit("published", "published_calendar_inventory_nontransactional",
          pub_rows, pub_pages)
    idx = os.path.join(root, "MANIFEST_INDEX.json")
    with open(idx, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return idx, manifest


@pytest.fixture()
def evidence(tmp_path):
    assets = [_asset(i) for i in range(3)]
    pubs = [_pub(0, asset_id="asset-000"),
            _pub(1, url="https://example.invalid/src.jpg"),
            _pub(2)]  # no source reference at all
    idx, _ = _write_evidence(tmp_path / "ev", assets, pubs, 2, 2)
    return idx


def test_valid_manifest_counts(evidence, tmp_path):
    out = tmp_path / "out.json"
    assert vlom.main(["--manifest", evidence, "--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    s = payload["summary"]
    assert s["approved_photo_assets"] == 3
    assert s["published_rows"] == 3
    assert s["published_rows_with_neither_source_reference"] == 1
    assert s["published_rows_with_source_asset_id"] == 1
    assert s["distinct_source_asset_ids_in_published"] == 1
    assert s["source_asset_ids_present_in_approved_snapshot"] == 1
    assert s["cleared_assets"] == 0 and s["cleared_published_rows"] == 0
    assert "NOT a clearance receipt" in payload["disclaimer"]
    assert all(not o["cleared"] for o in payload["asset_obligations"])
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"


def test_every_obligation_unresolved_with_historical_reasons(evidence,
                                                             tmp_path):
    out = tmp_path / "out.json"
    vlom.main(["--manifest", evidence, "--out", str(out)])
    payload = json.loads(out.read_text())
    for coll in ("asset_obligations", "published_row_obligations"):
        for ob in payload[coll]:
            for reason in vlom.UNRESOLVED_HISTORICAL_REASONS:
                assert reason in ob["unresolved_reasons"]


def test_tampered_page_hash_rejected(evidence):
    root = os.path.dirname(evidence)
    page = os.path.join(root, "assets", "page-00.json")
    data = json.load(open(page))
    data["rows"][0]["used_count"] = 99
    with open(page, "w") as fh:
        json.dump(data, fh)
    with pytest.raises(vlom.ManifestError, match="sha256 mismatch"):
        vlom.load_and_validate_pages(evidence)


def test_tampered_page_kind_rejected(evidence):
    root = os.path.dirname(evidence)
    idx = json.load(open(evidence))
    page_rel = "assets/page-00.json"
    page_path = os.path.join(root, page_rel)
    page = json.load(open(page_path))
    page["kind"] = "something_else"
    data = json.dumps(page, sort_keys=True).encode()
    with open(page_path, "wb") as fh:
        fh.write(data)
    for f in idx["groups"]["assets"]["files"]:
        if f["path"] == page_rel:
            f["sha256"] = hashlib.sha256(data).hexdigest()
    with open(evidence, "w") as fh:
        json.dump(idx, fh)
    with pytest.raises(vlom.ManifestError, match="kind mismatch"):
        vlom.load_and_validate_pages(evidence)


def test_duplicate_id_rejected(tmp_path):
    assets = [_asset(0), _asset(0)]
    idx, _ = _write_evidence(tmp_path / "ev", assets, [_pub(0)])
    with pytest.raises(vlom.ManifestError, match="duplicate id"):
        vlom.load_and_validate_pages(idx)


def test_missing_page_rejected(tmp_path):
    idx, _ = _write_evidence(tmp_path / "ev", [_asset(0)], [_pub(0)])
    os.unlink(os.path.join(os.path.dirname(idx), "assets", "page-00.json"))
    with pytest.raises(vlom.ManifestError, match="missing page file"):
        vlom.load_and_validate_pages(idx)


def test_truncated_page_rejected(tmp_path):
    idx, manifest = _write_evidence(tmp_path / "ev",
                                    [_asset(0), _asset(1)], [_pub(0)])
    page_path = os.path.join(os.path.dirname(idx), "assets", "page-00.json")
    page = json.load(open(page_path))
    page["rows"] = page["rows"][:1]
    data = json.dumps(page, sort_keys=True).encode()
    with open(page_path, "wb") as fh:
        fh.write(data)
    manifest["groups"]["assets"]["files"][0]["sha256"] = \
        hashlib.sha256(data).hexdigest()
    with open(idx, "w") as fh:
        json.dump(manifest, fh)
    with pytest.raises(vlom.ManifestError, match="row count"):
        vlom.load_and_validate_pages(idx)


def test_tenant_source_mismatch_flagged(tmp_path):
    idx, _ = _write_evidence(tmp_path / "ev",
                             [_asset(0, gym="gymA", source_gym="gymB")],
                             [_pub(0)])
    payload = vlom.build_manifest(idx)
    ob = payload["asset_obligations"][0]
    assert ob["tenant_source_mismatch"] is True
    assert "tenant_source_mismatch" in ob["unresolved_reasons"]
    assert payload["summary"]["approved_photos_tenant_source_mismatch"] == 1


def test_missing_source_reference_reason(tmp_path):
    idx, _ = _write_evidence(tmp_path / "ev", [_asset(0)], [_pub(0)])
    payload = vlom.build_manifest(idx)
    ob = payload["published_row_obligations"][0]
    assert "missing_source_reference" in ob["unresolved_reasons"]
    assert ob["cleared"] is False


def test_source_id_absent_from_approved_snapshot(tmp_path):
    idx, _ = _write_evidence(tmp_path / "ev", [_asset(0)],
                             [_pub(0, asset_id="ghost-asset")])
    payload = vlom.build_manifest(idx)
    ob = payload["published_row_obligations"][0]
    assert ob["source_asset_in_approved_snapshot"] is False
    assert ("source_asset_id_absent_from_approved_snapshot"
            in ob["unresolved_reasons"])
    assert payload["summary"]["source_asset_ids_present_in_approved_snapshot"] == 0


def test_no_optional_historical_files(evidence, tmp_path):
    out = tmp_path / "out.json"
    assert vlom.main(["--manifest", evidence, "--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["coverage"]["optional_snapshots"] == {}


def test_optional_snapshots_recorded_but_never_clear(evidence, tmp_path):
    snap = tmp_path / "ledger.json"
    snap.write_text(json.dumps({"rows": []}))
    out = tmp_path / "out.json"
    assert vlom.main(["--manifest", evidence, "--out", str(out),
                      "--ledger-snapshot", str(snap)]) == 0
    payload = json.loads(out.read_text())
    rec = payload["coverage"]["optional_snapshots"]["usage_ledger"]
    assert len(rec["sha256"]) == 64
    assert payload["summary"]["cleared_assets"] == 0
    assert all(not o["cleared"] for o in payload["asset_obligations"])


def test_deterministic_repeated_output(evidence, tmp_path):
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    vlom.main(["--manifest", evidence, "--out", str(out1)])
    vlom.main(["--manifest", evidence, "--out", str(out2)])
    assert out1.read_bytes() == out2.read_bytes()


def test_offset_mismatch_rejected(tmp_path):
    idx, manifest = _write_evidence(tmp_path / "ev",
                                    [_asset(i) for i in range(4)],
                                    [_pub(0)], asset_pages=2)
    page_rel = "assets/page-01.json"
    page_path = os.path.join(os.path.dirname(idx), page_rel)
    page = json.load(open(page_path))
    page["offset"] = 0  # wrong: should follow page-00
    data = json.dumps(page, sort_keys=True).encode()
    with open(page_path, "wb") as fh:
        fh.write(data)
    for f in manifest["groups"]["assets"]["files"]:
        if f["path"] == page_rel:
            f["sha256"] = hashlib.sha256(data).hexdigest()
    with open(idx, "w") as fh:
        json.dump(manifest, fh)
    with pytest.raises(vlom.ManifestError, match="offset"):
        vlom.load_and_validate_pages(idx)


def test_saved_audit_counts():
    """Confirm against the real saved snapshot; non-atomic, not a receipt."""
    root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir, os.pardir,
        "evidence", "media-20261004"))
    idx = os.path.join(root, "MANIFEST_INDEX.json")
    if not os.path.isfile(idx):
        pytest.skip("saved audit evidence not present")
    payload = vlom.build_manifest(idx)
    s = payload["summary"]
    assert s["approved_photo_assets"] == 618
    assert s["published_rows"] == 1311
    assert s["published_rows_with_neither_source_reference"] == 787
    assert s["approved_photos_tenant_source_mismatch"] == 17
    assert s["published_rows_with_source_asset_id"] == 300
    assert s["published_rows_with_source_url"] == 316
    assert s["distinct_source_asset_ids_in_published"] == 115
    assert s["source_asset_ids_present_in_approved_snapshot"] == 57
    assert s["cleared_assets"] == 0
    assert "non-atomic" in payload["disclaimer"]


def _groups_only_manifest(tmp_path, group_names):
    idx, manifest = _write_evidence(tmp_path / "ev", [_asset(0)], [_pub(0)])
    manifest["groups"] = {
        name: manifest["groups"]["assets"] if name != "published"
        else manifest["groups"]["published"]
        for name in group_names}
    with open(idx, "w") as fh:
        json.dump(manifest, fh)
    return idx


def test_requires_exactly_assets_and_published_groups(tmp_path):
    # Missing group rejected
    idx = _groups_only_manifest(tmp_path / "a", ["assets"])
    with pytest.raises(vlom.ManifestError, match="exactly the groups"):
        vlom.load_and_validate_pages(idx)
    # Extra group rejected (even a named-but-unexpected one)
    idx = _groups_only_manifest(tmp_path / "b",
                                ["assets", "published", "extra"])
    with pytest.raises(vlom.ManifestError, match="exactly the groups"):
        vlom.load_and_validate_pages(idx)


def test_non_photo_kind_rejected(tmp_path):
    idx, manifest = _write_evidence(tmp_path / "ev", [_asset(0)], [_pub(0)])
    page_path = os.path.join(os.path.dirname(idx), "assets", "page-00.json")
    page = json.load(open(page_path))
    page["rows"][0]["kind"] = "video"
    data = json.dumps(page, sort_keys=True).encode()
    with open(page_path, "wb") as fh:
        fh.write(data)
    manifest["groups"]["assets"]["files"][0]["sha256"] = \
        hashlib.sha256(data).hexdigest()
    with open(idx, "w") as fh:
        json.dump(manifest, fh)
    with pytest.raises(vlom.ManifestError, match="non-photo kind"):
        vlom.load_and_validate_pages(idx)


def test_non_approved_review_status_rejected(tmp_path):
    idx, manifest = _write_evidence(tmp_path / "ev", [_asset(0)], [_pub(0)])
    page_path = os.path.join(os.path.dirname(idx), "assets", "page-00.json")
    page = json.load(open(page_path))
    page["rows"][0]["review_status"] = "pending"
    data = json.dumps(page, sort_keys=True).encode()
    with open(page_path, "wb") as fh:
        fh.write(data)
    manifest["groups"]["assets"]["files"][0]["sha256"] = \
        hashlib.sha256(data).hexdigest()
    with open(idx, "w") as fh:
        json.dump(manifest, fh)
    with pytest.raises(vlom.ManifestError, match="non-approved review_status"):
        vlom.load_and_validate_pages(idx)


def test_blank_ids_rejected(tmp_path):
    for field in ("gym_id", "source_gym_id", "source_id"):
        bad = _asset(0)
        bad[field] = ""
        idx, _ = _write_evidence(tmp_path / f"ev-{field}", [bad], [_pub(0)])
        with pytest.raises(vlom.ManifestError, match=field):
            vlom.load_and_validate_pages(idx)


def test_cross_tenant_linked_source_flagged_on_published(tmp_path):
    # Row gym matches asset gym but NOT asset source_gym -> cross-tenant.
    assets = [_asset(0, gym="gymA", source_gym="gymB")]
    pubs = [_pub(0, gym="gymA", asset_id="asset-000")]
    idx, _ = _write_evidence(tmp_path / "ev", assets, pubs)
    payload = vlom.build_manifest(idx)
    ob = payload["published_row_obligations"][0]
    assert ob["cross_tenant_linked_source"] is True
    assert ob["linked_asset_gym_id"] == "gymA"
    assert ob["linked_asset_source_gym_id"] == "gymB"
    assert "cross_tenant_linked_source" in ob["unresolved_reasons"]
    assert ob["cleared"] is False
    assert payload["summary"]["published_rows_cross_tenant_linked_source"] == 1


def test_same_tenant_linked_source_not_flagged(tmp_path):
    assets = [_asset(0, gym="gymA", source_gym="gymA")]
    pubs = [_pub(0, gym="gymA", asset_id="asset-000")]
    idx, _ = _write_evidence(tmp_path / "ev", assets, pubs)
    payload = vlom.build_manifest(idx)
    ob = payload["published_row_obligations"][0]
    assert ob["cross_tenant_linked_source"] is False
    assert "cross_tenant_linked_source" not in ob["unresolved_reasons"]
    assert payload["summary"]["published_rows_cross_tenant_linked_source"] == 0


def test_saved_pattern_cross_tenant_linked_source():
    """Saved 2026-10-04 snapshot: e.g. row tenant crossfitsunnysidef574c0
    linked to asset sourced from crossfitsunnyside2616ac. Saved-evidence
    pattern must be flagged, never cleared."""
    root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir, os.pardir,
        "evidence", "media-20261004"))
    idx = os.path.join(root, "MANIFEST_INDEX.json")
    if not os.path.isfile(idx):
        pytest.skip("saved audit evidence not present")
    payload = vlom.build_manifest(idx)
    s = payload["summary"]
    assert s["published_rows_cross_tenant_linked_source"] == 25
    flagged = [o for o in payload["published_row_obligations"]
               if o["cross_tenant_linked_source"]]
    assert len(flagged) == 25
    sample = next(o for o in flagged
                  if o["published_row_id"]
                  == "7c3ba93a-f3ae-40fd-aa4d-afaa332d49ba")
    assert sample["tenant_gym_id"] == "crossfitsunnysidef574c0"
    assert sample["linked_asset_gym_id"] == "crossfitsunnysidef574c0"
    assert sample["linked_asset_source_gym_id"] == "crossfitsunnyside2616ac"
    assert "cross_tenant_linked_source" in sample["unresolved_reasons"]
    for ob in flagged:
        assert ob["cleared"] is False


def test_checksum_note_is_consistency_only(evidence, tmp_path):
    out = tmp_path / "out.json"
    vlom.main(["--manifest", evidence, "--out", str(out)])
    payload = json.loads(out.read_text())
    note = payload["input_evidence_checksum_note"]
    assert "Consistency check only" in note
    assert "NOT tamper-proof" in note
