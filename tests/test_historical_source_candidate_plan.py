import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import historical_source_candidate_plan as plan


def asset(aid="asset-a", gym="gym-a", sha="a" * 64):
    return {"id": aid, "gym_id": gym, "source_gym_id": gym, "kind": "photo",
            "content_hash": "b" * 32, "review_content_hash": "b" * 32,
            "review_status": "approved", "moderation_status": "clean",
            "moderation_json": {"sha256": sha, "asset_id": aid, "gym_id": gym,
                                "content_hash": "b" * 32, "verdict": "clean",
                                "provider": "saved-provider", "observed_at": "2026-09-01T12:00:00Z"}}


def row(rid="row-a", gym="gym-a", url=None, **extra):
    return {"id": rid, "gym_id": gym, "post_date": "2026-09-02",
            "late_post_id": "post-a", "image_url": url or "https://r2.example/echo/gym-a/1234567890abcdef/aaaaaaaaaaaa__feed.jpg", **extra}


def test_unique_source_prefix_is_candidate_never_clearance():
    result = plan.build_plan([row()], [asset()])
    record = result[1]
    assert result[0]["bound_raw_sha256_assets"] == 1
    assert record["candidates"][0]["source_sha256"] == "a" * 64
    assert not record["source_lineage_verified"]
    assert not record["historical_use_attested"]
    assert not record["clearance"]


@pytest.mark.parametrize("change", [
    {"review_content_hash": "c" * 32}, {"source_gym_id": "gym-b"},
    {"source_gym_id": None}, {"moderation_status": "unknown"},
    {"kind": "video"},
])
def test_invalid_asset_binding_cannot_supply_raw_prefix(change):
    a = asset(); a.update(change)
    result = plan.build_plan([row()], [a])
    assert result[0]["bound_raw_sha256_assets"] == 0
    assert result[1]["candidates"] == []


@pytest.mark.parametrize("field,value", [
    ("asset_id", "wrong"), ("gym_id", "wrong"), ("content_hash", "c" * 32),
    ("sha256", "a" * 12), ("observed_at", "bad"), ("observed_at", "2026-09-01T00:00:00"),
    ("provider", ""), ("verdict", "unknown"),
])
def test_invalid_nested_binding_is_rejected(field, value):
    a = asset(); a["moderation_json"][field] = value
    assert plan.bound_sha256(a) is None


def test_prefix_collision_and_duplicate_aliases_are_held():
    collision = asset("asset-b", sha="a" * 12 + "c" * 52)
    alias = asset("asset-c")
    for second in (collision, alias):
        result = plan.build_plan([row()], [asset(), second])
        assert result[1]["candidates"] == []
        assert "ambiguous_source_prefix_rejected" in result[1]["reasons"]


def test_cross_gym_matching_hash_never_associates():
    result = plan.build_plan([row()], [asset(gym="gym-b")])
    assert result[1]["candidates"] == []
    assert "cross_gym_source_prefix_rejected" in result[1]["reasons"]


def test_explicit_source_and_render_prefix_conflict_is_held():
    r = row(source_media_asset_id="asset-b")
    result = plan.build_plan([r], [asset(), asset("asset-b", sha="c" * 64)])
    assert result[1]["candidates"] == []
    assert "conflicting_candidate_assets_rejected" in result[1]["reasons"]


def test_source_url_hash_donor_requires_bound_asset_and_not_rendered_url_only():
    raw = "https://r2.example/raw.jpg"
    prefix = hashlib.sha1(raw.encode()).hexdigest()[:16]
    target = row(url=f"https://r2.example/src_{prefix}.jpg")
    donor = row("donor", url="https://r2.example/story.jpg", source_media_url=raw,
                source_media_asset_id="asset-a")
    records = plan.build_plan([target, donor], [asset()])
    candidate = next(r for r in records[1:] if r["row_id"] == "row-a")["candidates"][0]
    assert candidate["source_exact_url"] == raw
    assert candidate["donor_row_refs"] == ["donor"]
    donor["image_url"] = raw
    records = plan.build_plan([target, donor], [asset()])
    target_record = next(r for r in records[1:] if r["row_id"] == "row-a")
    assert target_record["candidates"] == []


def test_url_only_and_rendition_only_catalog_never_supply_identity():
    raw = "https://r2.example/raw.jpg"
    prefix = hashlib.sha1(raw.encode()).hexdigest()[:16]
    target = row(url=f"https://r2.example/src_{prefix}.jpg")
    donor = row("donor", source_media_url=raw)
    a = asset(); a.pop("moderation_json"); a["rendition_url"] = target["image_url"]
    result = plan.build_plan([target, donor], [a])
    assert all(not r["candidates"] for r in result[1:])


def test_duplicate_references_and_conflicting_snapshots_abort():
    with pytest.raises(ValueError, match="duplicate"):
        plan.build_plan([row(), row()], [asset()])
    with pytest.raises(ValueError, match="duplicate"):
        plan.build_plan([row()], [asset(), asset()])
    other = row(gym="gym-b")
    with pytest.raises(ValueError, match="disagree"):
        plan.build_plan([row()], [asset()], [other])


def test_capture_delivery_does_not_create_source_candidate():
    r = row(url="https://r2.example/rendered.jpg")
    cap = {**r, "delivery_status": "verified_provider_delivery", "raw_receipts": [{"raw_sha256": "e" * 64}]}
    result = plan.build_plan([r], [], captures=[cap])
    assert result[1]["candidates"] == []
    assert result[1]["capture_context"]["delivery_status"] == "verified_provider_delivery"
    assert not result[1]["source_lineage_verified"]
    cap["late_post_id"] = "different"
    with pytest.raises(ValueError, match="disagree"):
        plan.build_plan([r], [], captures=[cap])


def test_input_order_does_not_affect_report():
    rows = [row(), row("row-b")]
    assets = [asset(), asset("unmatched", sha="c" * 64)]
    assert plan.build_plan(rows, assets) == plan.build_plan(rows[::-1], assets[::-1])


def test_cli_saved_inputs_include_hashes_and_do_not_touch_inputs(tmp_path, capsys):
    classifier = tmp_path / "classifier.jsonl"
    classifier.write_text(json.dumps({"record_type": "summary"}) + "\n" + json.dumps({"record_type": "row", **row()}))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"rows": [asset()]}))
    before = classifier.read_bytes(), catalog.read_bytes()
    assert plan.main(["--classifier", str(classifier), "--catalog", str(catalog)]) == 0
    result = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(result) == 2
    assert result[0]["input_files"][0]["sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert before == (classifier.read_bytes(), catalog.read_bytes())


def test_missing_source_tenant_and_wrong_snapshot_count_fail_closed(tmp_path):
    a = asset(); a.pop("source_gym_id")
    assert plan.bound_sha256(a) is None
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"expected_count": 2, "rows": [asset()]}))
    with pytest.raises(ValueError, match="count mismatch"):
        plan.load(catalog)


def test_classifier_blank_optional_post_id_is_equivalent_to_null():
    r = row(); r["late_post_id"] = ""
    classified = {**r, "late_post_id": None}
    assert plan.build_plan([classified], [asset()], [r])[0]["row_count"] == 1
