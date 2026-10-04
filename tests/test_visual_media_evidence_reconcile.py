"""Focused tests for scripts/visual_media_evidence_reconcile.py."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.visual_media_evidence_reconcile import (  # noqa: E402
    InputError, build_report, main, reconcile, write_report)

SHA = "a" * 64
MD5 = "b" * 32


def _calendar_row(row_id, gym="gym-a", post_date="2026-09-01",
                  asset_id="asset-1", source_url="https://x/src1",
                  image_url="https://x/delivered1"):
    return {
        "id": row_id, "gym_id": gym, "status": "published",
        "variant_status": "published", "post_date": post_date,
        "image_url": image_url, "source_media_url": source_url,
        "source_media_asset_id": asset_id,
    }


def _asset(asset_id="asset-1", gym="gym-a", sha256=SHA, state="active",
           source_url="https://x/src1"):
    return {"asset_id": asset_id, "gym_id": gym, "sha256": sha256,
            "state": state, "source_media_url": source_url}


def _byte_row(ref, delivered_sha, source_sha, delivered_status="observed",
              source_status="observed",
              delivered_url="https://x/delivered1",
              source_url="https://x/src1"):
    delivered = {"exact_url": delivered_url, "status": delivered_status}
    if delivered_sha:
        delivered["sha256"] = delivered_sha
    source = {"exact_url": source_url, "status": source_status}
    if source_sha:
        source["sha256"] = source_sha
    return {"row_ref": ref, "delivered": delivered, "source_observation": source}


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rows(calendar, assets, **kwargs):
    return reconcile(calendar, assets, **kwargs)[0]


# --- Zero historical clearance ---------------------------------------------

def test_byte_identity_is_evidence_only_never_resolves():
    calendar = [_calendar_row("row-1")]
    assets = [_asset()]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert "original_use_unproven" in obligations[0]["reasons"]
    assert obligations[0]["evidence"]["byte_identity_observed"] is True
    assert obligations[0]["evidence"]["original_use_receipt"] is None


def test_every_obligation_always_unresolved():
    calendar = [_calendar_row("row-1"), _calendar_row("row-2")]
    assets = [_asset()]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    provider = [{"row_ref": "row-1", "obligation": "cleared"}]
    cal_obs, asset_obs = reconcile(
        calendar, assets, byte_rows=bytes_manifest, provider_rows=provider)
    for item in cal_obs + asset_obs:
        assert item["resolution"] == "unresolved"
        assert "original_use_unproven" in item["reasons"]


def test_provider_manifest_omitted_still_unresolved():
    calendar = [_calendar_row("row-1")]
    assets = [_asset()]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert obligations[0]["evidence"]["provider_obligation"] is None


def test_missing_gym_stays_unresolved_even_with_byte_identity():
    calendar = [_calendar_row("row-1", gym="gym-a")]
    asset = _asset()
    del asset["gym_id"]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    obligations = _rows(calendar, [asset], byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert "missing_tenant" in obligations[0]["reasons"]


def test_asset_obligations_cover_assets_with_no_calendar_row():
    calendar = [_calendar_row("row-1")]
    lonely = _asset(asset_id="asset-2", source_url="https://x/src2")
    cal_obs, asset_obs = reconcile(calendar, [_asset(), lonely])
    refs = {o["asset_ref"]: o for o in asset_obs}
    assert set(refs) == {"asset-1", "asset-2"}
    assert refs["asset-2"]["resolution"] == "unresolved"
    assert "no_matching_asset" in refs["asset-2"]["reasons"]
    assert "original_use_unproven" in refs["asset-2"]["reasons"]
    assert refs["asset-2"]["evidence"]["calendar_row_refs_by_asset_id"] == []
    assert refs["asset-1"]["evidence"]["calendar_row_refs_by_asset_id"] == ["row-1"]


def test_empty_calendar_still_emits_per_asset_obligations():
    cal_obs, asset_obs = reconcile([], [_asset()])
    assert cal_obs == []
    assert len(asset_obs) == 1
    assert asset_obs[0]["resolution"] == "unresolved"


def test_url_match_is_hint_only_not_proof():
    calendar = [_calendar_row("row-1")]
    assets = [{"asset_id": "asset-1", "gym_id": "gym-a",
               "source_media_url": "https://x/src1", "state": "active"}]
    obligations = _rows(calendar, assets)
    assert obligations[0]["resolution"] == "unresolved"
    assert "no_rendition_identity" in obligations[0]["reasons"]


def test_hint_only_when_hashes_present_but_unmatched():
    calendar = [_calendar_row("row-1")]
    assets = [_asset(sha256="b" * 64)]
    bytes_manifest = [_byte_row("row-1", "c" * 64, "d" * 64)]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert "source_match_hint_only" in obligations[0]["reasons"]


def test_tenant_mismatch_blocks_even_with_byte_identity():
    calendar = [_calendar_row("row-1", gym="gym-a")]
    assets = [_asset(gym="gym-b")]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert "tenant_mismatch" in obligations[0]["reasons"]


@pytest.mark.parametrize("state", ["deleted", "swapped", "replaced", "purged"])
def test_deleted_or_swapped_asset_never_resolved(state):
    calendar = [_calendar_row("row-1")]
    assets = [_asset(state=state)]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert "deleted_or_swapped_asset" in obligations[0]["reasons"]


def test_provider_obligation_unknown():
    calendar = [_calendar_row("row-1")]
    assets = [_asset()]
    bytes_manifest = [_byte_row("row-1", SHA, SHA)]
    provider = [{"row_ref": "row-1", "obligation": "unknown"}]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest,
                        provider_rows=provider)
    assert obligations[0]["resolution"] == "unresolved"
    assert "provider_obligation_unknown" in obligations[0]["reasons"]
    obligations = _rows(calendar, assets, byte_rows=bytes_manifest,
                        provider_rows=[])
    assert "provider_obligation_unknown" in obligations[0]["reasons"]


def test_unknown_date_blocks():
    for bad in (None, "", "not-a-date", "2026-13-01", "2026-02-30"):
        calendar = [_calendar_row("row-1", post_date=bad)]
        obligations = _rows(calendar, [_asset()],
                            byte_rows=[_byte_row("row-1", SHA, SHA)])
        assert obligations[0]["resolution"] == "unresolved"
        assert "unknown_date" in obligations[0]["reasons"], bad


@pytest.mark.parametrize("row", ["garbage", {}, {"id": "  "}])
def test_missing_or_blank_persisted_calendar_id_aborts(row):
    with pytest.raises(InputError,
                       match="persisted (calendar )?id|expected an object|missing or blank id"):
        reconcile([row], [])


# --- Fail-closed on explicitly blank primary persisted reference -----------

@pytest.mark.parametrize("row", [
    {"id": "  ", "row_ref": "row-1"},
    {"id": None, "row_ref": "row-1"},
    {"id": "", "row_ref": "row-1"},
])
def test_blank_primary_calendar_id_does_not_fall_back_to_alias(row):
    with pytest.raises(InputError, match="missing or blank id"):
        reconcile([row], [])


@pytest.mark.parametrize("indexer", ["provider", "postlog"])
@pytest.mark.parametrize("row", [
    {"row_ref": "  ", "id": "row-1"},
    {"row_ref": None, "calendar_row_id": "row-1"},
    {"row_ref": "", "id": "row-1"},
])
def test_blank_primary_manifest_ref_does_not_fall_back_to_alias(indexer, row):
    kwargs = {f"{indexer}_rows": [row]}
    with pytest.raises(InputError, match="missing or blank row_ref"):
        reconcile([_calendar_row("row-1")], [_asset()], **kwargs)


def test_alias_ref_accepted_when_primary_field_absent():
    # ``id`` absent entirely: supported aliases still resolve the ref.
    row = {k: v for k, v in _calendar_row("row-1").items() if k != "id"}
    row["row_ref"] = "row-1"
    cal_obs, _ = reconcile([row], [_asset()])
    assert cal_obs[0]["row_ref"] == "row-1"


def test_provider_alias_ref_accepted_when_row_ref_absent():
    # ``row_ref`` absent entirely: the ``id`` alias still resolves the ref.
    obligations = _rows([_calendar_row("row-1")], [_asset()],
                        provider_rows=[{"id": "row-1", "obligation": "unknown"}])
    assert "provider_obligation_unknown" in obligations[0]["reasons"]


# --- Byte-observation manifest rows must be objects with nonblank refs -----

@pytest.mark.parametrize("row", [
    "garbage",
    {},
    {"row_ref": "  "},
    {"row_ref": None},
    {"row_ref": ""},
    {"row_ref": 7},
])
def test_byte_manifest_row_must_be_object_with_nonblank_row_ref(row):
    with pytest.raises(InputError,
                       match="byte_observation_manifest row 1: (expected an object|missing or blank)"):
        reconcile([_calendar_row("row-1")], [_asset()], byte_rows=[row])


def test_byte_manifest_row_ref_needs_no_alias_fallback():
    # No alias support for byte rows: a blank explicit row_ref fails closed
    # even when another key carries a nonblank value.
    row = _byte_row("  ", SHA, SHA)
    row["id"] = "row-1"
    with pytest.raises(InputError, match="missing or blank row_ref"):
        reconcile([_calendar_row("row-1")], [_asset()], byte_rows=[row])


def test_byte_observation_error_blocks():
    calendar = [_calendar_row("row-1")]
    bytes_manifest = [_byte_row("row-1", None, None, delivered_status="error")]
    obligations = _rows(calendar, [_asset()], byte_rows=bytes_manifest)
    assert obligations[0]["resolution"] == "unresolved"
    assert "byte_observation_error" in obligations[0]["reasons"]


def test_sorted_output_by_row_ref():
    calendar = [_calendar_row("row-9"), _calendar_row("row-1"),
                _calendar_row("row-5")]
    cal_obs, _ = reconcile(calendar, [])
    assert [o["row_ref"] for o in cal_obs] == ["row-1", "row-5", "row-9"]


# --- Adversarial fail-closed integrity checks ------------------------------

def test_duplicate_calendar_row_refs_abort():
    calendar = [_calendar_row("row-1"), _calendar_row("row-1")]
    with pytest.raises(InputError, match="duplicate row ref"):
        reconcile(calendar, [])


def test_duplicate_asset_ids_abort():
    assets = [_asset(), _asset()]
    with pytest.raises(InputError, match="duplicate asset_id"):
        reconcile([], assets)


def test_duplicate_asset_source_urls_abort():
    assets = [_asset(asset_id="asset-1"), _asset(asset_id="asset-2")]
    with pytest.raises(InputError, match="duplicate source_media_url"):
        reconcile([], assets)


def test_duplicate_byte_row_refs_abort():
    bytes_manifest = [_byte_row("row-1", SHA, SHA),
                      _byte_row("row-1", SHA, SHA)]
    with pytest.raises(InputError, match="duplicate row_ref"):
        reconcile([_calendar_row("row-1")], [_asset()],
                  byte_rows=bytes_manifest)


@pytest.mark.parametrize("indexer, manifest", [
    ("provider", [{"row_ref": "row-1"}, {"row_ref": "row-1"}]),
    ("postlog", [{"row_ref": "row-1"}, {"row_ref": "row-1"}]),
])
def test_duplicate_provider_or_postlog_row_refs_abort(indexer, manifest):
    kwargs = {f"{indexer}_rows": manifest}
    with pytest.raises(InputError, match=fr"{indexer}_manifest: duplicate row_ref"):
        reconcile([_calendar_row("row-1")], [_asset()], **kwargs)


@pytest.mark.parametrize("indexer", ["provider", "postlog"])
@pytest.mark.parametrize("row", [{}, {"row_ref": "  "}, "not-a-row"])
def test_missing_or_blank_provider_or_postlog_row_refs_abort(indexer, row):
    kwargs = {f"{indexer}_rows": [row]}
    with pytest.raises(InputError, match=fr"{indexer}_manifest row 1: (missing or blank|expected an object)"):
        reconcile([_calendar_row("row-1")], [_asset()], **kwargs)


@pytest.mark.parametrize("bad", ["xyz", "a" * 63, "a" * 65, 12345, "g" * 64])
def test_malformed_digest_aborts(bad):
    with pytest.raises(InputError, match="malformed sha256 digest"):
        reconcile([_calendar_row("row-1")], [_asset(sha256=bad)])
    byte = _byte_row("row-1", SHA, SHA)
    byte["delivered"]["sha256"] = bad
    with pytest.raises(InputError, match="malformed sha256 digest"):
        reconcile([_calendar_row("row-1")], [_asset()], byte_rows=[byte])


def test_malformed_md5_aborts():
    asset = _asset()
    asset["md5"] = "not-md5"
    with pytest.raises(InputError, match="malformed md5 digest"):
        reconcile([_calendar_row("row-1")], [asset])


def test_delivered_url_mismatch_aborts():
    byte = _byte_row("row-1", SHA, SHA,
                     delivered_url="https://evil.example/other")
    with pytest.raises(InputError, match="delivered.*exact_url"):
        reconcile([_calendar_row("row-1")], [_asset()], byte_rows=[byte])


def test_source_url_mismatch_aborts():
    byte = _byte_row("row-1", SHA, SHA, source_url="https://x/other-src")
    with pytest.raises(InputError, match="source.*exact_url"):
        reconcile([_calendar_row("row-1")], [_asset()], byte_rows=[byte])


def test_md5_sha256_disagreement_aborts():
    # Delivered sha256 matches source sha256 but delivered md5 differs from
    # source md5: the two algorithms disagree, so fail closed.
    byte = {"row_ref": "row-1",
            "delivered": {"exact_url": "https://x/delivered1",
                          "status": "observed", "sha256": SHA, "md5": MD5},
            "source_observation": {"exact_url": "https://x/src1",
                                   "status": "observed", "sha256": SHA,
                                   "md5": "c" * 32}}
    with pytest.raises(InputError, match="md5/sha256 disagreement"):
        reconcile([_calendar_row("row-1")], [_asset()], byte_rows=[byte])


def test_md5_sha256_agreement_is_evidence_only():
    byte = {"row_ref": "row-1",
            "delivered": {"exact_url": "https://x/delivered1",
                          "status": "observed", "sha256": SHA, "md5": MD5},
            "source_observation": {"exact_url": "https://x/src1",
                                   "status": "observed", "sha256": SHA,
                                   "md5": MD5}}
    obligations = _rows([_calendar_row("row-1")], [_asset()], byte_rows=[byte])
    assert obligations[0]["evidence"]["byte_identity_observed"] is True
    assert obligations[0]["resolution"] == "unresolved"


# --- Report-level behavior ---------------------------------------------------

def _build_all(tmp_path):
    calendar_path = _write(tmp_path, "calendar.json",
                           {"format": "visual-calendar-snapshot-v1",
                            "rows": [_calendar_row("row-1")]})
    asset_path = _write(tmp_path, "assets.json", {"rows": [_asset()]})
    byte_path = _write(tmp_path, "bytes.json",
                       {"rows": [_byte_row("row-1", SHA, SHA)]})
    out = tmp_path / "report.json"
    assert main([str(calendar_path), str(asset_path), str(out),
                 "--byte-observation-manifest", str(byte_path)]) == 0
    return calendar_path, asset_path, byte_path, out


def test_report_hashes_inputs_and_is_0600(tmp_path):
    calendar_path, asset_path, byte_path, out = _build_all(tmp_path)
    report = json.loads(out.read_text())
    assert report["format"] == "visual-media-evidence-reconciliation-v1"
    for label, path in (("calendar_snapshot", calendar_path),
                        ("asset_snapshot", asset_path),
                        ("byte_observation_manifest", byte_path)):
        entry = report["inputs"][label]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["row_count"] == 1
    assert report["inputs"]["provider_manifest"] is None
    assert os.stat(out).st_mode & 0o777 == 0o600
    summary = report["summary"]
    assert summary["resolved"] == 0
    assert summary["unresolved"] == summary["obligations"] == 2
    assert summary["calendar_obligations"] == 1
    assert summary["asset_obligations"] == 1
    assert summary["reason_counts"]["original_use_unproven"] == 2
    assert report["obligations"][0]["evidence"]["byte_identity_observed"] is True
    assert report["asset_obligations"][0]["resolution"] == "unresolved"


def test_deterministic_replay_byte_identical(tmp_path):
    calendar_path, asset_path, byte_path, out1 = _build_all(tmp_path)
    first = out1.read_bytes()
    out2 = tmp_path / "report2.json"
    assert main([str(calendar_path), str(asset_path), str(out2),
                 "--byte-observation-manifest", str(byte_path)]) == 0
    assert out2.read_bytes() == first
    text = out1.read_text()
    assert text.index('"calendar_snapshot"') < text.index('"provider_manifest"')


def test_malformed_input_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    good = _write(tmp_path, "good.json", {"rows": []})
    out = tmp_path / "report.json"
    assert main([str(bad), str(good), str(out)]) == 2
    assert "malformed JSON input" in capsys.readouterr().err
    assert not out.exists()
    wrong = _write(tmp_path, "wrong.json", {"no_rows": True})
    assert main([str(wrong), str(good), str(out)]) == 2
    assert not out.exists()
    with pytest.raises(InputError):
        build_report(wrong, good)


def test_integrity_failure_exits_2_without_output(tmp_path, capsys):
    calendar_path = _write(tmp_path, "calendar.json",
                           {"rows": [_calendar_row("row-1"),
                                     _calendar_row("row-1")]})
    asset_path = _write(tmp_path, "assets.json", {"rows": [_asset()]})
    out = tmp_path / "report.json"
    assert main([str(calendar_path), str(asset_path), str(out)]) == 2
    assert "duplicate row ref" in capsys.readouterr().err
    assert not out.exists()


def test_write_report_atomic_permissions(tmp_path):
    out = tmp_path / "nested" / "report.json"
    write_report(out, {"b": 1, "a": 2})
    assert os.stat(out).st_mode & 0o777 == 0o600
    assert json.loads(out.read_text()) == {"a": 2, "b": 1}


# --- Real public.media_asset schema acceptance --------------------------------
# Live rows carry id/source_id/gym_id/kind/title/mime_type/content_hash/
# rendition_key/rendition_url/eligible/excluded_by_coach/used_count/
# last_used_at/indexed_at/review_status. There is NO source_media_url,
# sha256, or md5. content_hash is a Drive MD5 hint, never original-byte
# proof; rendition_url is never a source URL.

def _media_asset_row(rid="asset-1", gym="gym-a", *, rendition_key="rend-1",
                     content_hash="a" * 32):
    return {
        "id": rid, "source_id": "source-1", "gym_id": gym, "kind": "photo",
        "title": "front desk", "mime_type": "image/jpeg",
        "content_hash": content_hash, "rendition_key": rendition_key,
        "rendition_url": "https://cdn.test/1.jpg", "eligible": True,
        "excluded_by_coach": False, "used_count": 1,
        "last_used_at": "2026-09-30T12:00:00Z",
        "indexed_at": "2026-10-01T00:00:00Z", "review_status": "approved",
    }


def test_real_schema_asset_accepted_rendition_key_is_hint_only():
    calendar = [_calendar_row("row-1")]
    asset = _media_asset_row()
    cal_obs, asset_obs = reconcile(calendar, [asset])
    assert cal_obs[0]["resolution"] == "unresolved"
    # rendition_key satisfies rendition identity, so a matched asset is a
    # hint-only match — never no_rendition_identity, never resolved.
    assert "source_match_hint_only" in cal_obs[0]["reasons"]
    assert "no_rendition_identity" not in cal_obs[0]["reasons"]
    assert "original_use_unproven" in cal_obs[0]["reasons"]
    assert asset_obs[0]["evidence"]["rendition_identity"] == "rend-1"
    assert asset_obs[0]["resolution"] == "unresolved"


def test_real_schema_missing_rendition_key_reported_per_asset():
    calendar = [_calendar_row("row-1")]
    asset = _media_asset_row(rendition_key=None)
    cal_obs, asset_obs = reconcile(calendar, [asset])
    # Row level: matched by asset id with no identity anywhere to compare.
    assert "no_rendition_identity" in cal_obs[0]["reasons"]
    # Asset level: missing rendition identity reported explicitly.
    assert asset_obs[0]["evidence"]["rendition_identity"] is None
    assert "no_rendition_identity" in asset_obs[0]["reasons"]
    assert asset_obs[0]["resolution"] == "unresolved"
    assert "original_use_unproven" in asset_obs[0]["reasons"]


def test_content_hash_is_not_treated_as_digest_evidence():
    # A malformed-looking content_hash must NOT trip digest validation, and
    # must never produce byte_identity_observed evidence.
    calendar = [_calendar_row("row-1")]
    asset = _media_asset_row(content_hash="not-a-real-md5-!!")
    cal_obs, _ = reconcile(calendar, [asset])
    assert cal_obs[0]["evidence"]["byte_identity_observed"] is False
    assert "no_rendition_identity" not in cal_obs[0]["reasons"]


def test_rendition_url_is_not_a_source_url():
    # rendition_url must never be indexed or matched as source_media_url:
    # an asset whose only URL is rendition_url does not URL-match a calendar
    # row with a different source URL, and duplicate rendition_urls across
    # assets with distinct keys do not abort.
    calendar = [_calendar_row("row-1", asset_id=None,
                                source_url="https://x/original1")]
    assets = [_media_asset_row("asset-1", rendition_key="rend-1"),
              _media_asset_row("asset-2", rendition_key="rend-2")]
    cal_obs, _ = reconcile(calendar, assets)
    assert cal_obs[0]["evidence"]["asset_match"] is None
    assert "no_matching_asset" in cal_obs[0]["reasons"]


def test_duplicate_rendition_keys_abort():
    assets = [_media_asset_row("asset-1"), _media_asset_row("asset-2")]
    with pytest.raises(InputError, match="duplicate rendition_key"):
        reconcile([], assets)


def test_real_schema_snapshot_object_accepted_by_main(tmp_path):
    calendar_path = _write(tmp_path, "calendar.json",
                           {"rows": [_calendar_row("row-1")]})
    asset_path = _write(tmp_path, "assets.json",
                        {"format": "visual-asset-snapshot-v1",
                         "rows": [_media_asset_row()]})
    out = tmp_path / "report.json"
    assert main([str(calendar_path), str(asset_path), str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["summary"]["resolved"] == 0
    assert report["asset_obligations"][0]["evidence"]["rendition_identity"] == "rend-1"
