"""Offline candidate observations cannot stand in for owner authority."""
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from agent import client_month_run as month
from agent import forward_media_attester as attester
from agent import gbp_planner, gym_media_builder, visual_writer_prepare


def photo(fmt="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (1200, 400), "blue").save(stream, fmt)
    return stream.getvalue()


@pytest.mark.parametrize("name,kwargs", [
    ("identity", {}), ("feed_autofit_4x5", {}),
    ("story_photo", {"caption": "Build strength together", "gym_name": "Test Gym"}),
    ("gbp_crop_4x3", {}),
])
def test_candidate_replays_actual_still_and_stays_unverified(name, kwargs):
    source = photo()
    recipe = attester.make_still_recipe(name, **kwargs)
    delivered = attester.replay_still_recipe(source, recipe)["image_bytes"]
    objects = {"https://host/source": source, "https://host/image": delivered}
    observation = gym_media_builder.still_materialization_observation(
        source, delivered, "https://host/image", tenant="gym_ig",
        source_asset_id="drive-id", source_url="https://host/source",
        image_name=name, bytes_fn=objects.__getitem__, **kwargs)
    assert observation["recipe"] == recipe
    assert observation["source_asset_id"] == "drive-id"
    assert observation["provenance_status"] == "unverified"
    assert "authoritative_original_registry_receipt_required" in observation["hold_reasons"]
    assert "authoritative_render_manifest_receipt_required" in observation["hold_reasons"]
    assert "runtime_verified" not in observation["recipe"]
    assert "render_manifest_digest" not in observation


def test_cached_derivative_drift_refuses_candidate():
    with pytest.raises(ValueError, match="replay differs"):
        gym_media_builder.still_materialization_observation(
            photo(), b"stale cached bytes", "https://host/image", tenant="gym",
            image_name="gbp_crop_4x3", bytes_fn=lambda url: b"stale cached bytes")


def test_unsupported_source_refuses_identity_candidate():
    with pytest.raises(Exception, match="unsupported still source"):
        gym_media_builder.still_materialization_observation(
            photo("GIF"), photo("GIF"), "https://host/image", tenant="gym")


def test_hosted_readback_mismatch_refuses_candidate():
    source = photo()
    with pytest.raises(ValueError, match="hosted readback differs"):
        gym_media_builder.still_materialization_observation(
            source, source, "https://host/image", tenant="gym",
            bytes_fn=lambda url: b"different hosted bytes")


def test_raw_source_candidate_attached_without_authority(monkeypatch):
    source = photo()
    monkeypatch.setattr(month, "_visual_writer_guard_enabled", lambda: True)
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", lambda url: source)
    draft = SimpleNamespace(creative_public_url="https://host/source", account_key="gym_ig")
    assert month._capture_raw_hosted_source(draft, lambda msg: None, "day") == "https://host/source"
    assert draft.media_provenance_status == "unverified"
    assert draft.media_materialization_observations[0]["recipe"]["image"]["name"] == "identity"
    assert not hasattr(draft, "render_manifest_digest")


def test_default_off_does_not_read_or_emit_candidates(monkeypatch):
    monkeypatch.setattr(month, "_visual_writer_guard_enabled", lambda: False)
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", lambda url: pytest.fail("unexpected read"))
    draft = SimpleNamespace(creative_public_url="https://host/source", account_key="gym_ig")
    assert month._capture_raw_hosted_source(draft, lambda msg: None, "day") == ""
    assert not hasattr(draft, "media_materialization_observations")


def test_gbp_evidence_channel_preserves_strict_candidate(tmp_path, monkeypatch):
    source = photo()
    recipe = attester.make_still_recipe("gbp_crop_4x3")
    delivered = attester.replay_still_recipe(source, recipe)["image_bytes"]
    src, dst = tmp_path / "source.png", tmp_path / "crop.jpg"
    src.write_bytes(source)
    dst.write_bytes(delivered)
    objects = {"https://host/source": source, "https://host/image": delivered}
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", objects.__getitem__)
    evidence = gbp_planner._render_evidence_dict(
        "https://host/source", "https://host/image", src, dst, tenant="gym_gbp")
    observation = evidence["materialization_observation"]
    assert observation["tenant"] == "gym_gbp"
    assert observation["recipe"] == recipe
    assert observation["provenance_status"] == "unverified"
    dst.write_bytes(b"stale")
    assert gbp_planner._render_evidence_dict(
        "https://host/source", "https://host/image", src, dst) is None
