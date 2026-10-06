"""Ordinary self-service completion requires an original snapshot and fresh proof."""
import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent import media_guard, media_swap, portal_social as ps
from agent import portal_calendar_store as pcs, visual_writer_prepare as vp


def row():
    result = {key: None for key in pcs._CORE_VISUAL_MEDIA_CAS_COLUMNS}
    result.update(id="post", gym_id="gym", account="instagram", format="feed",
                  status="pending", caption="Approved copy", post_date="2026-10-06",
                  image_url="https://cdn/old.jpg", source_media_url="https://cdn/old.jpg",
                  variant_status="active", media_not_ready_reason="manual_hold",
                  approval_kind=None, approved_by=None, approved_at=None, approval_digest=None)
    return result


def pick():
    return {"ok": True, "image_url": "https://cdn/new.jpg", "source_media_url": "https://cdn/new.jpg",
            "source": "library", "key": "new.jpg", "kind": "image",
            "original_sha256": hashlib.sha256(b"new original").hexdigest(), "siblings": {}}


class Store:
    def __init__(self, before, *, mutate=None, readback=None):
        self.row = deepcopy(before)
        self.mutate = mutate
        self.readback = readback
        self.calls = []
    def get_row(self, gym, rid):
        out = deepcopy(self.row)
        if self.calls and self.readback:
            self.readback(out)
        return out
    def list_month(self, gym, month):
        return [deepcopy(self.row)]
    def swap_media(self, gym, rid, image, **kwargs):
        self.calls.append(kwargs)
        if self.mutate:
            self.mutate(self.row)
        if self.row != kwargs["expected_row"]:
            return None
        self.row.update(image_url=image, source_media_url=kwargs["source_media_url"], **kwargs["extra_fields"])
        return deepcopy(self.row)


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_budget_state", lambda *a: {"used": 0})
    monkeypatch.setattr(ps.config, "portal_calendar_supabase_enabled", lambda: True)
    monkeypatch.setattr(media_swap, "enabled", lambda: True)
    monkeypatch.setattr(media_swap, "reserve_local_pick", lambda *a: True)
    monkeypatch.setattr(media_swap, "release_local_pick", lambda *a: None)
    monkeypatch.setattr(media_swap, "after_swap", lambda *a, **k: None)
    monkeypatch.setattr(media_swap, "sibling_rows", lambda *a, **k: [])
    monkeypatch.setattr(vp, "enabled", lambda: False)
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "old.jpg").write_bytes(b"old original")
    monkeypatch.setattr(media_swap, "library_path_for", lambda *a: str(lib))
    monkeypatch.setattr(vp, "_bytes_for_url", lambda url: {
        "https://cdn/old.jpg": b"old original", "https://cdn/new.jpg": b"new original",
        "https://cdn/alias.jpg": b"old original"}[url])


def run(store, replacement=None):
    return ps.handle_swap_media("gym", "post", "actor", sb_store=store,
                                picker=lambda *a, **k: replacement or pick())


def test_success_independent_readback_preserves_hold_and_approval(enabled):
    before = row()
    store = Store(before)
    code, body = run(store)
    assert code == 200
    proof = body["media_swap_proof"]
    assert proof["account_key"] == "gym" and proof["row_id"] == "post"
    assert proof["readback_verified"] and proof["invariants_preserved"]
    assert proof["old_original_sha256"] != proof["new_original_sha256"]
    assert len(proof["original_snapshot_sha256"]) == 64
    assert store.calls[0]["expected_row"] == before
    assert store.row["media_not_ready_reason"] == "manual_hold"
    assert store.row["caption"] == before["caption"]


@pytest.mark.parametrize("field,value", [("caption", "concurrent edit"),
    ("status", "approved"), ("publish_claim_token", "claim"),
    ("media_not_ready_reason", "new hold"), ("approval_digest", "new proof")])
def test_concurrent_snapshot_change_cannot_succeed(enabled, field, value):
    store = Store(row(), mutate=lambda r: r.update({field: value}))
    code, body = run(store)
    assert code != 200 and not body["ok"] and "media_swap_proof" not in body
    assert store.row["image_url"] == row()["image_url"]


@pytest.mark.parametrize("url", ["https://cdn/old.jpg", "https://cdn/alias.jpg"])
def test_same_original_even_different_url_refuses_without_write(enabled, url):
    replacement = pick()
    replacement.update(image_url=url, source_media_url=url,
                       original_sha256=hashlib.sha256(b"old original").hexdigest())
    store = Store(row())
    code, body = run(store, replacement)
    assert code == 409 and not store.calls and "media_swap_proof" not in body


def test_missing_original_lineage_fails_closed(enabled):
    replacement = pick()
    replacement.pop("original_sha256")
    store = Store(row())
    assert run(store, replacement)[0] == 409
    assert not store.calls


@pytest.mark.parametrize("field,value", [("image_url", "https://cdn/old.jpg"),
    ("caption", "drift"), ("media_not_ready_reason", None), ("status", "approved"),
    ("approved_by", "other")])
def test_patch_representation_does_not_certify_stale_or_drifted_get(enabled, field, value):
    store = Store(row(), readback=lambda r: r.update({field: value}))
    code, body = run(store)
    assert code == 503 and not body["ok"] and "media_swap_proof" not in body


def test_render_candidate_requires_exact_lineage(enabled):
    replacement = pick()
    replacement["source_media_url"] = "https://cdn/new.jpg"
    replacement["image_url"] = "https://cdn/alias.jpg"
    store = Store(row())
    assert run(store, replacement)[0] == 409
    assert not store.calls


def test_real_store_pins_clicked_snapshot_with_visual_flag_off(monkeypatch):
    before = row()
    store = pcs.SupabaseCalendarStore()
    captured = {}
    def patch(url, **kwargs):
        captured.update(kwargs)
        updated = {**before, **kwargs["json"]}
        return SimpleNamespace(status_code=200, json=lambda: [updated])
    monkeypatch.setattr(store, "_client", lambda: SimpleNamespace(patch=patch))
    monkeypatch.setattr(store, "_rest", lambda *a: "https://example/table")
    monkeypatch.setattr(store, "_headers", lambda *a: {})
    monkeypatch.setattr(vp, "enabled", lambda: False)
    store.swap_media("gym", "post", "new", expected_row=before)
    assert captured["params"]["caption"] == "eq.Approved copy"
    assert captured["params"]["media_not_ready_reason"] == "eq.manual_hold"
    assert captured["params"]["publish_claim_token"] == "is.null"
    assert captured["params"]["approval_digest"] == "is.null"
    assert "media_not_ready_reason" not in captured["json"]


def asset_store(*, asset_gym="gym", source_gym="gym", content=b"old original"):
    asset = {"id": "asset-old", "gym_id": asset_gym, "source_id": "source-old",
             "content_hash": hashlib.md5(content).hexdigest()}
    source = {"id": "source-old", "gym_id": source_gym}
    def get(table, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: [asset if table == "media_asset" else source])
    return SimpleNamespace(_client=lambda: SimpleNamespace(get=get),
                           _rest=lambda table: table, _headers=lambda: {})


@pytest.mark.parametrize("conflict", ["asset_tenant", "source_tenant", "stale_url", "drive_id", "byte_hash"])
def test_current_drive_original_conflicting_linkage_refuses(enabled, conflict):
    current = row()
    current["source_media_asset_id"] = "asset-old"
    store = asset_store(asset_gym="other" if conflict == "asset_tenant" else "gym",
                        source_gym="other" if conflict == "source_tenant" else "gym")
    if conflict == "stale_url":
        current["source_media_url"] = "https://cdn/new.jpg"
    if conflict == "drive_id":
        current["drive_file_id"] = "different-original"
    if conflict == "byte_hash":
        current["byte_hash"] = "derived:sha256:" + "a" * 64
    with pytest.raises(ValueError):
        media_guard.swap_original_identity("gym", current, store)


def test_current_drive_original_reads_existing_asset_source_without_scene_ledger(enabled):
    current = row()
    current.update(source_media_asset_id="asset-old", image_url="https://cdn/legacy-card.jpg")
    identity = media_guard.swap_original_identity("gym", current, asset_store())
    assert identity["sha256"] == hashlib.sha256(b"old original").hexdigest()


def test_video_proof_binds_actual_media_and_keeps_poster_display(enabled, monkeypatch, tmp_path):
    lib = tmp_path / "video-library"
    lib.mkdir()
    (lib / "old.mp4").write_bytes(b"old video original")
    monkeypatch.setattr(media_swap, "library_path_for", lambda *a: str(lib))
    content = {"https://cdn/old.mp4": b"old video original",
               "https://cdn/new.mp4": b"new video original", "https://cdn/poster.jpg": b"poster pixels"}
    monkeypatch.setattr(vp, "_bytes_for_url", lambda url: content[url])
    current = row()
    current.update(image_url="https://cdn/old.mp4", source_media_url="https://cdn/old.mp4")
    replacement = pick()
    replacement.update(image_url="https://cdn/new.mp4", source_media_url="https://cdn/new.mp4",
                       original_sha256=hashlib.sha256(content["https://cdn/new.mp4"]).hexdigest(),
                       kind="video", thumbnail_url="https://cdn/poster.jpg",
                       poster_render_evidence={"source_exact_url": "https://cdn/new.mp4",
                           "delivered_exact_url": "https://cdn/poster.jpg",
                           "source_fingerprint": vp._md5(content["https://cdn/new.mp4"]),
                           "delivered_fingerprint": vp._md5(content["https://cdn/poster.jpg"]),
                           "source_byte_length": len(content["https://cdn/new.mp4"]),
                           "delivered_byte_length": len(content["https://cdn/poster.jpg"]),
                           "operation": "render"})
    code, body = run(Store(current), replacement)
    assert code == 200
    assert body["media_swap_proof"]["old_image_url"] == "https://cdn/old.mp4"
    assert body["media_swap_proof"]["new_image_url"] == body["video_url"] == "https://cdn/new.mp4"
    assert body["image_public_url"] == "https://cdn/poster.jpg"
