"""Offline contract tests for the draft, default-off Python writer boundary."""
import hashlib

import pytest

from agent import portal_calendar_store as pcs
from agent import visual_writer_prepare as prep


TENANT = "11111111-1111-4111-8111-111111111111"
URL = "https://media.example/selected.jpg"


class Response:
    status_code = 200
    text = ""

    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code

    def json(self):
        return self.value


class HTTP:
    def __init__(self, asset=None, known=None, bundle=None, patch_failure=False, row=None):
        self.calls = []
        self.asset = asset
        self.known = known or {}
        self.bundle = bundle
        self.patch_failure = patch_failure
        self.row = row

    def post(self, url, *, headers, json, timeout):
        self.calls.append(("post", url.rsplit("/", 1)[-1], json))
        if url.endswith("visual_global_prepare_bundle"):
            return Response(self.bundle if self.bundle is not None else
                            {"group_key": "vg_same", "fingerprint": json["p_fingerprint"]})
        return Response([json[0]] if isinstance(json, list) else [])

    def get(self, url, *, params, headers, timeout):
        self.calls.append(("get", url.rsplit("/", 1)[-1], params))
        if url.endswith("tenant_alias"):
            key = params["alias_key"][3:]
            return Response([{"alias_key": key, "tenant_id": TENANT}]
                            if key in ("old-key", TENANT) else [])
        if url.endswith("media_asset"):
            return Response([self.asset] if self.asset else [])
        if url.endswith("visual_group_alias"):
            group = self.known.get((params["alias_kind"][3:], params["alias_value"][3:]))
            return Response([{"group_key": group}] if group else [])
        return Response([self.row or {"id": "row-1", "gym_id": "old-key", "status": "pending",
                                      "variant_status": "active", "image_url": "https://old.example/a.jpg"}])

    def patch(self, url, *, params, headers, json, timeout):
        self.calls.append(("patch", url.rsplit("/", 1)[-1], json))
        if self.patch_failure:
            self.patch_failure = False
            return Response([], status_code=409)
        return Response([{"id": "row-1", "gym_id": "old-key", **json}])


def store(http):
    return pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=http)


def test_flag_off_preserves_payload_and_does_no_rpc(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    http = HTTP()
    row = {"image_url": URL, "status": "pending"}
    assert prep.prepare(store(http), "old-key", row) is row
    assert http.calls == []


def test_registers_final_byte_hashes_and_delivered_url_before_write(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    http = HTTP()
    data = b"exact delivered image"
    row = prep.prepare(store(http), "old-key", {"image_url": URL, "status": "pending"},
                       read_bytes=lambda url: data)
    assert row["visual_group_key"] == "vg_same"
    bundles = [call[2] for call in http.calls if call[1] == "visual_global_prepare_bundle"]
    assert len(bundles) == 1
    assert [(item["alias_kind"], item["alias_value"]) for item in bundles[0]["p_aliases"]] == [
        ("byte_hash", "derived:md5:" + hashlib.md5(data).hexdigest()),
        ("canonical_url", URL),
    ]
    assert bundles[0]["p_tenant"] == TENANT
    assert bundles[0]["p_fingerprint"] == "md5:" + hashlib.md5(data).hexdigest()
    assert row["byte_hash"] == "derived:md5:" + hashlib.md5(data).hexdigest()
    assert [call[1] for call in http.calls if call[0] == "post"] == ["visual_global_prepare_bundle"]


def test_unverifiable_active_media_refuses_without_registration(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="bytes"):
        prep.prepare(store(http), "old-key", {"image_url": URL, "status": "pending"},
                     read_bytes=lambda url: None)
    assert not any(call[1] == "visual_global_prepare_bundle" for call in http.calls)
    with pytest.raises(prep.VisualPreparationError, match="no delivered media"):
        prep.prepare(store(HTTP()), "old-key", {"image_url": "", "status": "pending",
                     "media_not_ready_reason": "awaiting_media"})


def test_drive_md5_requires_same_tenant_asset_and_matching_final_bytes(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"Drive original"
    http = HTTP({"id": "drive-1", "gym_id": "old-key",
                 "content_hash": hashlib.md5(data).hexdigest()})
    prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_asset_id": "drive-1"},
                 read_bytes=lambda url: data)
    aliases = [item["alias_value"] for call in http.calls if call[1] == "visual_global_prepare_bundle"
               for item in call[2]["p_aliases"]]
    assert "source:md5:" + hashlib.md5(data).hexdigest() in aliases
    assert "drive-1" in aliases
    assert not any(value.startswith("source:sha256:") for value in aliases)

    bad = HTTP({"id": "drive-1", "gym_id": "another-key", "content_hash": hashlib.md5(data).hexdigest()})
    with pytest.raises(prep.VisualPreparationError, match="tenant"):
        prep.prepare(store(bad), "old-key", {"image_url": URL, "source_media_asset_id": "drive-1"},
                     read_bytes=lambda url: data)
    assert not any(call[1] == "visual_global_prepare_bundle" for call in bad.calls)

    mismatch = HTTP({"id": "drive-1", "gym_id": "old-key", "content_hash": "0" * 32})
    with pytest.raises(prep.VisualPreparationError, match="MD5"):
        prep.prepare(store(mismatch), "old-key", {"image_url": URL, "source_media_asset_id": "drive-1"},
                     read_bytes=lambda url: data)
    assert not any(call[1] == "visual_global_prepare_bundle" for call in mismatch.calls)


def test_reuses_registered_delivered_url_group(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP(known={("canonical_url", URL): "vg_same"})
    prep.prepare(store(http), "old-key", {"image_url": URL}, read_bytes=lambda url: b"exact")
    assert len([call for call in http.calls if call[1] == "visual_global_prepare_bundle"]) == 1


def test_media_patch_registers_before_patch_and_clears_old_source(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: b"new pixels")
    http = HTTP()
    result = store(http).patch_image_url("old-key", "row-1", URL)
    assert result["visual_group_key"] == "vg_same"
    assert http.calls[-1][0] == "patch"
    assert http.calls[-1][2]["image_url"] == URL
    assert http.calls[-1][2]["byte_hash"] == "derived:md5:" + hashlib.md5(b"new pixels").hexdigest()


def test_failed_calendar_patch_leaves_reusable_registration_without_usage(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: b"same pixels")
    http = HTTP(patch_failure=True)
    with pytest.raises(pcs.PortalStoreError):
        store(http).patch_image_url("old-key", "row-1", URL)
    assert store(http).patch_image_url("old-key", "row-1", URL)["image_url"] == URL
    bundles = [call[2] for call in http.calls if call[1] == "visual_global_prepare_bundle"]
    assert len(bundles) == 2 and bundles[0] == bundles[1]
    assert not any("visual_global_usage" in call[1] for call in http.calls)


def test_story_reburn_does_not_clear_unverified_raw_source(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    raw = "https://media.example/raw.jpg"
    http = HTTP(row={"id": "row-1", "gym_id": "old-key", "status": "pending",
                     "format": "story", "variant_status": "active",
                     "image_url": "https://media.example/old-burn.jpg", "source_media_url": raw})
    with pytest.raises(prep.VisualPreparationError, match="rendition lineage"):
        store(http).patch_image_url("old-key", "row-1", URL)
    assert not any(call[0] == "patch" or call[1] == "visual_global_prepare_bundle"
                   for call in http.calls)


def test_story_replacement_preserves_source_when_it_equals_delivered_url(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: b"story pixels")
    http = HTTP(row={"id": "row-1", "gym_id": "old-key", "status": "pending",
                     "format": "story", "variant_status": "active",
                     "image_url": "https://media.example/old.jpg", "source_media_url": URL})
    result = store(http).patch_image_url("old-key", "row-1", URL)
    assert result["image_url"] == URL
    assert "source_media_url" not in [call[2] for call in http.calls if call[0] == "patch"][0]


def test_story_patch_forwards_verified_rendition_evidence_and_preserves_raw_source(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    raw, burned = "https://media.example/raw.jpg", "https://media.example/burn.jpg"
    current = {"id": "row-1", "gym_id": "old-key", "status": "pending",
               "format": "story", "variant_status": "active",
               "image_url": "https://media.example/old.jpg", "source_media_url": raw}
    evidence = {"source_exact_url": raw, "delivered_exact_url": burned,
                "source_fingerprint": "md5:" + "a" * 32,
                "delivered_fingerprint": "md5:" + "b" * 32,
                "source_byte_length": 10, "delivered_byte_length": 11,
                "operation": "reburn", "evidence_ref": "story_reburn:test",
                "observed_by": "story_reburn", "rendered_by": "story_reburn"}
    captured = {}

    def prepared(store_value, account_key, candidate, **kwargs):
        captured.update(account_key=account_key, candidate=dict(candidate), **kwargs)
        return {**candidate, "visual_group_key": "vg_scene",
                "byte_hash": "derived:md5:" + "b" * 32}

    monkeypatch.setattr(prep, "prepare", prepared)
    http = HTTP(row=current)
    result = store(http).patch_image_url("old-key", "row-1", burned,
                                         render_evidence=evidence)
    assert result["image_url"] == burned
    assert captured["render_evidence"] == evidence
    assert captured["candidate"]["source_media_url"] == raw
    patch = [call[2] for call in http.calls if call[0] == "patch"][0]
    assert patch["visual_group_key"] == "vg_scene"


def test_story_patch_rejects_evidence_for_another_source_or_delivered_url(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    current = {"id": "row-1", "gym_id": "old-key", "status": "pending",
               "format": "story", "variant_status": "active",
               "image_url": "https://media.example/old.jpg",
               "source_media_url": "https://media.example/raw.jpg"}
    http = HTTP(row=current)
    with pytest.raises(prep.VisualPreparationError, match="scoped story replacement"):
        store(http).patch_image_url("old-key", "row-1", "https://media.example/burn.jpg",
                                    render_evidence={"source_exact_url": "https://media.example/other.jpg",
                                                     "delivered_exact_url": "https://media.example/burn.jpg"})
    assert not any(call[0] == "patch" for call in http.calls)


def test_insert_rows_refuses_unverifiable_media_before_calendar_post(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    from agent import plan_horizon
    monkeypatch.setattr(plan_horizon, "belt_filter", lambda key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_stage_belts", lambda key, rows: rows)
    monkeypatch.setattr(pcs, "_media_stage_belt", lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, "_retry_story_hold_provenance", lambda store, key, rows: None)
    monkeypatch.setattr(pcs, "_reconcile_story_media_holds", lambda store, key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_preserve_held_slots", lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, "_dedupe_slots", lambda store, key, rows: rows)
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: None)
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="bytes"):
        store(http).insert_rows("old-key", [{"image_url": URL, "status": "pending"}])
    assert not any(call[1] == "content_calendar" and call[0] == "post" for call in http.calls)


def test_rejects_unverified_source_locator_and_hash_hint(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="source URL"):
        prep.prepare(store(http), "old-key", {"image_url": URL,
                     "source_media_url": "https://source.example/file"}, read_bytes=lambda url: b"final")
    with pytest.raises(prep.VisualPreparationError, match="byte_hash"):
        prep.prepare(store(HTTP()), "old-key", {"image_url": URL,
                     "byte_hash": "source:sha256:" + "0" * 64}, read_bytes=lambda url: b"final")


def test_bundle_response_conflict_or_failure_aborts_preparation(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    for response, message in [({"group_key": "vg_same", "fingerprint": "md5:" + "0" * 32}, "fingerprint"),
                              ({"group_key": "wrong", "fingerprint": "md5:" + hashlib.md5(b"final").hexdigest()}, "group")]:
        http = HTTP(bundle=response)
        with pytest.raises(prep.VisualPreparationError, match=message):
            prep.prepare(store(http), "old-key", {"image_url": URL}, read_bytes=lambda url: b"final")
        assert len([c for c in http.calls if c[1] == "visual_global_prepare_bundle"]) == 1
