from scripts.visual_historical_review_queue import build_queue


def _calendar(**extra):
    return {"format": "visual-calendar-snapshot-v1", "snapshot_at": "2026-10-05T12:00:00Z",
            "rows": [{"id": "calendar-1", "gym_id": "gym-a", "post_date": "2026-10-01",
                      "variant_status": "active", "status": "published",
                      "source_media_asset_id": "asset-1", "source_media_url": "https://source/a",
                      "image_url": "https://delivered/a", **extra}]}


def _asset(**extra):
    return {"format": "visual-asset-snapshot-v1", "snapshot_at": "2026-10-05T12:01:00Z",
            "rows": [{"id": "asset-1", "gym_id": "gym-a", "source_media_url": "https://source/a",
                      "content_hash": "same-hash", "rendition_key": "phash-ish", **extra}]}


def test_direct_asset_id_and_observed_bytes_without_authenticated_lineage_is_hold():
    result = build_queue(_calendar(), _asset(), {"rows": [{"row_ref": "calendar-1",
        "delivered": {"status": "observed", "sha256": "abc"},
        "source_observation": {"status": "observed", "sha256": "abc"}}]})
    item = result["items"][0]
    assert item["asset_match"] == "asset_id"
    assert item["queue_status"] == "hold"
    assert item["evidence"]["delivered_source_bytes_match"] is True
    assert item["evidence"]["authenticated_lineage"] is False
    assert result["summary"]["auto_cleared"] == 0


def test_exact_source_url_is_review_required_and_tenant_scoped():
    calendar = _calendar()
    calendar["rows"][0].pop("source_media_asset_id")
    result = build_queue(calendar, _asset())
    item = result["items"][0]
    assert item["asset_match"] == "source_url"
    assert item["queue_status"] == "review_required"


def test_tenant_mismatch_holds_even_with_direct_id_and_byte_equality():
    result = build_queue(_calendar(), _asset(gym_id="gym-b"), {"rows": [{"row_ref": "calendar-1",
        "delivered": {"status": "observed", "sha256": "abc"},
        "source_observation": {"status": "observed", "sha256": "abc",
        "authenticated": True, "lineage_verified": True}}]})
    assert result["items"][0]["queue_status"] == "hold"
    assert result["items"][0]["reason"] == "tenant_mismatch"


def test_duplicate_asset_id_is_ambiguous_hold_and_content_hash_ignored():
    assets = _asset()["rows"] * 2
    result = build_queue(_calendar(), assets)
    assert result["items"][0]["queue_status"] == "hold"
    assert result["items"][0]["reason"] == "ambiguous_match"


def test_explicit_authenticated_lineage_can_be_high_confidence():
    result = build_queue(_calendar(), _asset(), {"rows": [{"row_ref": "calendar-1",
        "delivered": {"status": "observed", "sha256": "abc"},
        "source_observation": {"status": "observed", "sha256": "abc",
        "authenticated": True, "lineage_verified": True}}]})
    assert result["items"][0]["queue_status"] == "high_confidence"


def test_digest_equality_is_ignored_when_observation_failed():
    result = build_queue(_calendar(), _asset(), {"rows": [{"row_ref": "calendar-1",
        "delivered": {"status": "error", "sha256": "abc"},
        "source_observation": {"status": "observed", "sha256": "abc"}}]})
    assert result["items"][0]["evidence"]["delivered_source_bytes_match"] is False
    assert result["items"][0]["queue_status"] == "hold"


def test_same_id_in_another_tenant_does_not_make_local_match_ambiguous():
    assets = _asset()["rows"] + [{"id": "asset-1", "gym_id": "gym-b",
        "source_media_url": "https://source/a"}]
    result = build_queue(_calendar(), assets)
    assert result["items"][0]["asset_match"] == "asset_id"
    assert result["items"][0]["queue_status"] == "hold"


def test_cross_tenant_only_asset_match_is_held_as_tenant_mismatch():
    result = build_queue(_calendar(), _asset(gym_id="gym-b"))
    assert result["items"][0]["asset_match"] == "cross_tenant_asset_id"
    assert result["items"][0]["queue_status"] == "hold"
    assert result["items"][0]["reason"] == "tenant_mismatch"


def test_draft_calendar_rows_are_excluded_from_queue():
    calendar = _calendar()
    calendar["rows"][0]["variant_status"] = "active"
    calendar["rows"][0]["status"] = "pending"
    result = build_queue(calendar, _asset())
    assert result["items"] == []
    assert result["summary"]["excluded_unpublished"] == 1


def test_published_archived_variant_remains_historical_queue_item():
    calendar = _calendar()
    calendar["rows"][0]["variant_status"] = "archived"
    calendar["rows"][0]["status"] = "published"
    result = build_queue(calendar, _asset())
    assert len(result["items"]) == 1
    assert result["items"][0]["queue_status"] == "hold"


def test_missing_source_url_in_standard_asset_snapshot_does_not_match_by_url():
    calendar = _calendar()
    calendar["rows"][0].pop("source_media_asset_id")
    asset = _asset()
    asset["rows"][0].pop("source_media_url")
    result = build_queue(calendar, asset)
    assert result["items"][0]["queue_status"] == "hold"
    assert result["items"][0]["reason"] == "asset_unresolved"
