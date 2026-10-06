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


def test_existing_drive_card_with_asset_id_and_exact_original_but_no_source_url(enabled):
    current = row()
    current.update(source_media_asset_id="asset-old", source_media_url=None)
    identity = media_guard.swap_original_identity("gym", current, asset_store())
    assert identity["sha256"] == hashlib.sha256(b"old original").hexdigest()
    assert identity["source_url"] == current["image_url"]
    assert identity["source_asset_id"] == "asset-old"


def test_existing_drive_card_without_source_url_requires_exact_asset_bytes(enabled):
    current = row()
    current.update(source_media_asset_id="asset-old", source_media_url=None)
    with pytest.raises(ValueError, match="original asset bytes mismatch"):
        media_guard.swap_original_identity("gym", current, asset_store(content=b"different original"))


@pytest.mark.parametrize("missing_columns", [False, True])
def test_url_only_legacy_raw_original_can_swap_with_exact_tenant_library_bytes(enabled, missing_columns):
    current = row()
    for field in ("source_media_url", "source_media_asset_id"):
        if missing_columns:
            current.pop(field)
        else:
            current[field] = None
    code, body = run(Store(current))
    assert code == 200 and body["media_swap_proof"]["readback_verified"]
    assert body["media_swap_proof"]["old_original_sha256"] == hashlib.sha256(b"old original").hexdigest()


@pytest.mark.parametrize("delivered", ["https://cdn/legacy-card.jpg", "https://cdn/new.jpg"])
def test_url_only_legacy_unknown_rendered_object_still_refuses(enabled, monkeypatch, delivered):
    current = row()
    current.update(source_media_url=None, source_media_asset_id=None, image_url=delivered)
    monkeypatch.setattr(vp, "_bytes_for_url", lambda *a: b"unproved rendered pixels")
    store = Store(current)
    code, body = run(store)
    assert code == 409 and body["reason"] == "media_evidence_unavailable"
    assert not store.calls and "media_swap_proof" not in body


def test_url_only_legacy_basename_requires_byte_equality(enabled, monkeypatch):
    current = row()
    current.update(source_media_url=None, source_media_asset_id=None)
    monkeypatch.setattr(vp, "_bytes_for_url", lambda *a: b"different pixels at same basename")
    with pytest.raises(ValueError, match="current local original bytes mismatch"):
        media_guard.swap_original_identity("gym", current, asset_store())


@pytest.fixture
def legacy_feed(enabled, monkeypatch, tmp_path):
    from PIL import Image
    from agent import feed_image
    lib = tmp_path / "legacy-feed-library"
    lib.mkdir()
    original = lib / "panorama.jpg"
    Image.new("RGB", (320, 60), (40, 80, 120)).save(original)
    original_bytes = original.read_bytes()
    key = hashlib.sha256(original_bytes).hexdigest()[:12] + "__feed.jpg"
    rendered = tmp_path / key
    feed_image.build_feed_image(str(original), str(rendered))
    url = "https://cdn/" + key
    content = {url: rendered.read_bytes(), "https://cdn/new.jpg": b"new original"}
    monkeypatch.setattr(media_swap, "library_path_for", lambda *a: str(lib))
    monkeypatch.setattr(vp, "_bytes_for_url", lambda url: content[url])
    current = row()
    current.update(image_url=url, source_media_url=None, source_media_asset_id=None)
    return current, lib, original_bytes, content


def test_legacy_feed_reframe_proves_full_local_original_before_swap(legacy_feed):
    current, lib, original, content = legacy_feed
    code, body = run(Store(current))
    assert code == 200 and body["media_swap_proof"]["readback_verified"]
    assert body["media_swap_proof"]["old_original_sha256"] == hashlib.sha256(original).hexdigest()
    assert body["media_swap_proof"]["old_original_sha256"] != hashlib.sha256(content[current["image_url"]]).hexdigest()
    assert sorted(p.name for p in lib.iterdir()) == ["panorama.jpg"]


@pytest.mark.parametrize("conflict", ["arbitrary_render", "missing_tenant_source", "ambiguous_prefix", "story", "drive_alias"])
def test_legacy_feed_hash_filename_alone_never_proves_original(legacy_feed, monkeypatch, conflict):
    current, lib, original, content = legacy_feed
    if conflict == "arbitrary_render":
        content[current["image_url"]] = b"unrelated bytes under legitimate hash filename"
    elif conflict == "missing_tenant_source":
        (lib / "panorama.jpg").unlink()
    elif conflict == "ambiguous_prefix":
        (lib / "another.jpg").write_bytes(original)
        # Even two possible local names are held rather than chosen arbitrarily.
    elif conflict == "story":
        current["format"] = "story"
    else:
        current["drive_file_id"] = "unresolved-drive-original"
    store = Store(current)
    code, body = run(store)
    assert code == 409 and body["reason"] == "media_evidence_unavailable"
    assert not store.calls and "media_swap_proof" not in body


def test_legacy_reframe_cannot_swap_for_same_original_at_raw_url(legacy_feed):
    current, lib, original, content = legacy_feed
    replacement = pick()
    content[replacement["image_url"]] = original
    replacement["original_sha256"] = hashlib.sha256(original).hexdigest()
    store = Store(current)
    code, body = run(store, replacement)
    assert code == 409 and not store.calls and "media_swap_proof" not in body


def test_server_updated_at_does_not_override_preserved_fields(enabled):
    before = {**row(), "updated_at": "2026-10-06T00:00:00Z"}
    store = Store(before, readback=lambda r: r.update(updated_at="2026-10-06T01:00:00Z"))
    original_swap = store.swap_media
    def swap(*args, **kwargs):
        result = original_swap(*args, **kwargs)
        store.row["updated_at"] = "2026-10-06T00:30:00Z"
        result["updated_at"] = store.row["updated_at"]
        return result
    store.swap_media = swap
    code, body = run(store)
    assert code == 200 and body["media_swap_proof"]["invariants_preserved"]
    assert store.row["caption"] == before["caption"]
    assert store.row["media_not_ready_reason"] == before["media_not_ready_reason"]


@pytest.mark.parametrize("failure_count,expected_code", [(1, 200), (2, 503)])
def test_primary_get_outage_keeps_sibling_work_without_replaying_patch(
        enabled, monkeypatch, failure_count, expected_code):
    before = row()
    sibling = {**before, "id": "sibling", "account": "facebook"}
    snapshots = {"post": deepcopy(before), "sibling": deepcopy(sibling)}
    writes, reads, released, settled = [], [], [], []
    failures = [failure_count]
    def get(gym, rid):
        reads.append(rid)
        if writes and rid == "post" and failures[0]:
            failures[0] -= 1
            raise OSError("transient independent GET failure")
        return deepcopy(snapshots[rid])
    def swap(gym, rid, image, **kwargs):
        writes.append(rid)
        assert snapshots[rid] == kwargs["expected_row"]
        snapshots[rid].update(image_url=image, source_media_url=kwargs["source_media_url"],
                              **kwargs["extra_fields"])
        return deepcopy(snapshots[rid])
    store = SimpleNamespace(get_row=get, swap_media=swap,
        list_month=lambda *a: [deepcopy(r) for r in snapshots.values()])
    monkeypatch.setattr(media_swap, "sibling_rows", lambda *a, **k: [deepcopy(sibling)])
    monkeypatch.setattr(media_swap, "release_local_pick", lambda *a: released.append(True))
    monkeypatch.setattr(media_swap, "after_swap", lambda *a, **k: settled.append(True))
    replacement = pick()
    replacement["siblings"] = {"sibling": pick()}
    code, body = run(store, replacement)
    assert code == expected_code
    assert writes == ["post", "sibling"]
    assert reads == ["post", "post", "post", "sibling"]
    assert body["siblings_swapped"] == ["sibling"] and body["siblings_left"] == []
    assert not released
    if expected_code == 200:
        assert body["media_swap_proof"]["readback_verified"] and settled == [True]
    else:
        assert not body["ok"] and body["reason"] == "swap_outcome_unknown"
        assert "media_swap_proof" not in body and not settled
    for snapshot in snapshots.values():
        assert snapshot["caption"] == before["caption"]
        assert snapshot["media_not_ready_reason"] == before["media_not_ready_reason"]


@pytest.mark.parametrize("patch_result", ["missing", "wrong_tenant", "wrong_id", "caption_drift", "missing_approval"])
def test_unproved_primary_patch_never_moves_siblings_on_get_outage(enabled, monkeypatch, patch_result):
    before = row()
    sibling = {**before, "id": "sibling", "account": "facebook"}
    store = Store(before)
    monkeypatch.setattr(media_swap, "sibling_rows", lambda *a, **k: [deepcopy(sibling)])
    original_swap = store.swap_media
    def swap(*args, **kwargs):
        result = original_swap(*args, **kwargs)
        if patch_result == "missing":
            return None
        if patch_result == "missing_approval":
            result.pop("approved_by")
        else:
            field = {"wrong_tenant": "gym_id", "wrong_id": "id", "caption_drift": "caption"}[patch_result]
            result[field] = "other"
        return result
    def get(*args):
        if store.calls:
            raise OSError("independent GET unavailable")
        return deepcopy(store.row)
    store.swap_media, store.get_row = swap, get
    replacement = pick()
    replacement["siblings"] = {"sibling": pick()}
    code, body = run(store, replacement)
    assert code == 503 and body["reason"] == "swap_outcome_unknown"
    assert len(store.calls) == 1 and "media_swap_proof" not in body


@pytest.mark.parametrize("failure_count,expected_code", [(1, 200), (2, 503)])
def test_committed_sibling_get_outage_is_unknown_and_never_reported_left(
        enabled, monkeypatch, failure_count, expected_code):
    before = row()
    sibling = {**before, "id": "sibling", "account": "facebook"}
    snapshots = {"post": deepcopy(before), "sibling": deepcopy(sibling)}
    writes, released, settled = [], [], []
    failures = [failure_count]
    def get(gym, rid):
        if rid in writes and rid == "sibling" and failures[0]:
            failures[0] -= 1
            raise OSError("sibling independent GET failure")
        return deepcopy(snapshots[rid])
    def swap(gym, rid, image, **kwargs):
        writes.append(rid)
        assert snapshots[rid] == kwargs["expected_row"]
        snapshots[rid].update(image_url=image, source_media_url=kwargs["source_media_url"],
                              **kwargs["extra_fields"])
        return deepcopy(snapshots[rid])
    store = SimpleNamespace(get_row=get, swap_media=swap,
        list_month=lambda *a: [deepcopy(r) for r in snapshots.values()])
    monkeypatch.setattr(media_swap, "sibling_rows", lambda *a, **k: [deepcopy(sibling)])
    monkeypatch.setattr(media_swap, "release_local_pick", lambda *a: released.append(True))
    monkeypatch.setattr(media_swap, "after_swap", lambda *a, **k: settled.append(True))
    replacement = pick()
    replacement["siblings"] = {"sibling": pick()}
    code, body = run(store, replacement)
    assert code == expected_code and writes == ["post", "sibling"]
    assert snapshots["sibling"]["image_url"] == replacement["image_url"]
    assert body["siblings_left"] == [] and not released
    if expected_code == 503:
        assert body["siblings_unknown"] == ["sibling"]
        assert body["siblings_swapped"] == [] and body["sibling_results"] == []
        assert not body["ok"] and body["reason"] == "swap_outcome_unknown"
        assert "media_swap_proof" not in body and not settled
    else:
        assert body["siblings_swapped"] == ["sibling"]
        assert body["media_swap_proof"]["readback_verified"] and settled == [True]


@pytest.mark.parametrize("mode,read_failure_count,expected_code", [
    ("committed", 0, 200), ("committed", 1, 200), ("prewrite", 0, 200),
    ("committed", 2, 503), ("prewrite", 2, 503),
    ("caption_drift", 0, 503), ("wrong_tenant", 0, 503),
    ("wrong_id", 0, 503), ("missing_approval", 0, 503),
    ("prepared", 0, 503), ("scene_presence", 0, 503)])
def test_sibling_mutation_exception_recovers_only_exact_known_outcomes(
        enabled, monkeypatch, mode, read_failure_count, expected_code):
    before = row()
    sibling = {**before, "id": "sibling", "account": "facebook"}
    snapshots = {"post": deepcopy(before), "sibling": deepcopy(sibling)}
    writes, reads, released, settled = [], [], [], []
    failures = [read_failure_count]
    if mode == "prepared":
        monkeypatch.setattr(vp, "enabled", lambda: True)
    def get(gym, rid):
        reads.append((gym, rid))
        if rid in writes and rid == "sibling" and failures[0]:
            failures[0] -= 1
            raise OSError("sibling read failure")
        result = deepcopy(snapshots[rid])
        if rid in writes and rid == "sibling":
            if mode == "caption_drift":
                result["caption"] = "concurrent edit"
            elif mode in ("wrong_tenant", "wrong_id"):
                result["gym_id" if mode == "wrong_tenant" else "id"] = "other"
            elif mode == "missing_approval":
                result.pop("approved_by")
            elif mode == "scene_presence":
                result["byte_hash"] = None
        return result
    def swap(gym, rid, image, **kwargs):
        writes.append(rid)
        assert snapshots[rid] == kwargs["expected_row"]
        if rid == "sibling" and mode == "prewrite":
            raise pcs.PreWriteCASError(422, "no mutation issued")
        snapshots[rid].update(image_url=image, source_media_url=kwargs["source_media_url"],
                              **kwargs["extra_fields"])
        if rid == "sibling":
            raise OSError("PATCH response lost after commit")
        return deepcopy(snapshots[rid])
    store = SimpleNamespace(get_row=get, swap_media=swap,
        list_month=lambda *a: [deepcopy(r) for r in snapshots.values()])
    monkeypatch.setattr(media_swap, "sibling_rows", lambda *a, **k: [deepcopy(sibling)])
    monkeypatch.setattr(media_swap, "release_local_pick", lambda *a: released.append(True))
    monkeypatch.setattr(media_swap, "after_swap", lambda *a, **k: settled.append(k["swapped_ids"]))
    replacement = pick()
    replacement["siblings"] = {"sibling": pick()}
    code, body = run(store, replacement)
    assert code == expected_code and writes == ["post", "sibling"]
    assert reads == [("gym", "post"), ("gym", "post")] + [("gym", "sibling")] * (1 + bool(read_failure_count))
    assert not released
    if expected_code == 503:
        assert body["siblings_unknown"] == ["sibling"]
        assert body["siblings_swapped"] == body["siblings_left"] == []
        assert body["reason"] == "swap_outcome_unknown" and not body["ok"]
        assert "media_swap_proof" not in body and not settled
    elif mode == "prewrite":
        assert body["siblings_left"] == ["sibling"] and body["siblings_swapped"] == []
        assert snapshots["sibling"] == sibling and settled == [["post"]]
    else:
        assert body["siblings_swapped"] == ["sibling"] and body["siblings_left"] == []
        assert body["sibling_results"][0]["id"] == "sibling"
        assert body["media_swap_proof"]["readback_verified"] and settled == [["post", "sibling"]]


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


def test_lost_primary_cas_cannot_claim_another_actors_matching_media(enabled):
    store = Store(row())
    def lost_cas(gym, rid, image, **kwargs):
        store.calls.append(kwargs)
        store.row.update(image_url=image, source_media_url=kwargs["source_media_url"],
                         **kwargs["extra_fields"])
        return None  # Another actor landed these bytes, not this CAS.
    store.swap_media = lost_cas
    code, body = run(store)
    assert code == 503 and body["reason"] == "swap_outcome_unknown"
    assert "media_swap_proof" not in body


def test_lost_sibling_cas_is_left_even_when_matching_media_is_visible(enabled, monkeypatch):
    before = row()
    sibling = {**before, "id": "sibling", "account": "facebook"}
    store = Store(before)
    monkeypatch.setattr(media_swap, "sibling_rows", lambda *a, **k: [deepcopy(sibling)])
    real_swap = store.swap_media
    sibling_fresh = deepcopy(sibling)
    def swap(gym, rid, image, **kwargs):
        if rid == "sibling":
            sibling_fresh.update(image_url=image, source_media_url=kwargs["source_media_url"],
                                 **kwargs["extra_fields"])
            return None
        return real_swap(gym, rid, image, **kwargs)
    real_get = store.get_row
    store.get_row = lambda gym, rid: deepcopy(sibling_fresh) if rid == "sibling" else real_get(gym, rid)
    store.swap_media = swap
    replacement = pick()
    replacement["siblings"] = {"sibling": pick()}
    code, body = run(store, replacement)
    assert code == 200
    assert body["siblings_swapped"] == [] and body["siblings_left"] == ["sibling"]
    assert body["sibling_results"] == []
