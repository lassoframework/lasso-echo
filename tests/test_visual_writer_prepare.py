"""Offline contract tests for the draft, default-off Python writer boundary."""
import hashlib
import uuid

import pytest

from agent import portal_calendar_store as pcs
from agent import visual_owner_receipts as owner
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
    def __init__(self, asset=None, known=None, bundle=None, patch_failure=False, row=None,
                 fingerprint=None, rendition=None):
        self.calls = []
        self.asset = asset
        self.known = known or {}
        self.bundle = bundle
        self.patch_failure = patch_failure
        self.row = row
        self.fingerprint = fingerprint
        self.rendition = rendition
        self.registered = False

    def post(self, url, *, headers, json, timeout):
        self.calls.append(("post", url.rsplit("/", 1)[-1], json))
        if url.endswith("visual_global_prepare_bundle"):
            self.registered = True
            return Response(self.bundle if self.bundle is not None else
                            {"group_key": "vg_same", "fingerprint": json["p_fingerprint"]})
        if url.endswith("visual_global_prepare_source_rendition"):
            return Response(self.rendition if self.rendition is not None else
                            {"group_key": json["p_group_key"],
                             "source_fingerprint": self.fingerprint,
                             "delivered_fingerprint": self.fingerprint,
                             "usage_claimed": False})
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
            if group:
                return Response([{"group_key": group}])
            return Response([{"group_key": "vg_same"}] if self.registered else [])
        return Response([self.row or {"id": "row-1", "gym_id": "old-key", "status": "pending",
                                      "variant_status": "active", "image_url": "https://old.example/a.jpg"}])

    def patch(self, url, *, params, headers, json, timeout):
        self.calls.append(("patch", url.rsplit("/", 1)[-1], json))
        if self.patch_failure:
            self.patch_failure = False
            return Response([], status_code=409)
        return Response([{**(self.row or {}), "id": "row-1", "gym_id": "old-key", **json}])


def store(http):
    return pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=http)


@pytest.fixture(autouse=True)
def own_host(monkeypatch):
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")


def _owner_writer(monkeypatch):
    """Production-shaped configured owner writer: exact bytes in, one receipt out."""
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return {"read_receipt": str(uuid.uuid4()), "render_receipt": None}

    monkeypatch.setattr(owner, "default_same_object_writer", lambda: writer)
    return calls


def test_flag_off_preserves_payload_and_does_no_rpc(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    http = HTTP()
    row = {"image_url": URL, "thumbnail_url": "https://other.example/poster.jpg",
           "status": "pending"}
    assert prep.prepare(store(http), "old-key", row) is row
    assert http.calls == []


def test_guarded_row_rejects_distinct_thumbnail_before_lookup_or_rpc(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        prep.prepare(store(http), "old-key", {
            "image_url": URL, "source_media_url": URL,
            "thumbnail_url": "https://media.example/another-scene-poster.jpg",
        })
    assert http.calls == []


@pytest.mark.parametrize("thumbnail_url", [None, "", "   ", URL])
def test_guarded_row_allows_blank_or_same_object_thumbnail(monkeypatch, thumbnail_url):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"same delivered and poster object"
    digest = "md5:" + hashlib.md5(data).hexdigest()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    _owner_writer(monkeypatch)
    http = HTTP(known={("canonical_url", URL): "vg_same"}, fingerprint=digest)
    prepared = prep.prepare(store(http), "old-key", {
        "image_url": URL, "source_media_url": URL,
        "thumbnail_url": thumbnail_url,
    })
    assert prepared["thumbnail_url"] == thumbnail_url
    assert prepared["visual_group_key"] == "vg_same"


def test_guarded_same_object_entrypoint_rejects_distinct_thumbnail(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        prep.prepare_same_object(store(http), "old-key", {
            "image_url": URL, "source_media_url": URL,
            "thumbnail_url": "https://media.example/video-poster.jpg",
        })
    assert http.calls == []


def test_raw_row_prepares_through_owner_receipt_source_rendition_rpc(monkeypatch):
    """Production-shaped: default byte reader plus the configured owner writer.

    A guarded raw row must carry owner source+delivered scene object members,
    so the same-object path never uses the legacy bundle RPC for the row.
    """
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    data = b"exact delivered image"
    digest = "md5:" + hashlib.md5(data).hexdigest()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    writer_calls = _owner_writer(monkeypatch)
    http = HTTP(known={("canonical_url", URL): "vg_same"}, fingerprint=digest)
    row = prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_url": URL,
                                                  "status": "pending"})
    assert row["visual_group_key"] == "vg_same"
    assert row["byte_hash"] == "derived:" + digest
    posts = [call for call in http.calls if call[0] == "post"]
    assert [call[1] for call in posts] == ["visual_global_prepare_source_rendition"]
    rendition = posts[0][2]
    assert rendition["p_tenant"] == TENANT
    assert rendition["p_group_key"] == "vg_same"
    assert rendition["p_source_read_receipt"] == rendition["p_delivered_read_receipt"]
    assert rendition["p_render_receipt"] is None
    assert writer_calls[0]["tenant"] == TENANT
    assert writer_calls[0]["group_key"] == "vg_same"
    assert writer_calls[0]["exact_bytes"] == data


def test_unverifiable_active_media_refuses_without_registration(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: None)
    _owner_writer(monkeypatch)
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="bytes"):
        prep.prepare(store(http), "old-key", {"image_url": URL,
                                               "source_media_url": URL, "status": "pending"})
    assert not any(call[0] == "post" for call in http.calls)
    with pytest.raises(prep.VisualPreparationError, match="no delivered media"):
        prep.prepare(store(HTTP()), "old-key", {"image_url": "", "status": "pending",
                     "media_not_ready_reason": "awaiting_media"})


def test_drive_md5_requires_same_tenant_asset_and_matching_final_bytes(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"Drive original"
    digest = "md5:" + hashlib.md5(data).hexdigest()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    writer_calls = _owner_writer(monkeypatch)
    http = HTTP({"id": "drive-1", "gym_id": "old-key",
                 "content_hash": hashlib.md5(data).hexdigest()},
                known={("canonical_url", URL): "vg_same"}, fingerprint=digest)
    prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_url": URL,
                                           "source_media_asset_id": "drive-1"})
    assert writer_calls[0]["asset_id"] == "drive-1"
    assert writer_calls[0]["exact_bytes"] == data
    assert [call[1] for call in http.calls if call[0] == "post"] == [
        "visual_global_prepare_source_rendition"]

    bad = HTTP({"id": "drive-1", "gym_id": "another-key",
                "content_hash": hashlib.md5(data).hexdigest()})
    with pytest.raises(prep.VisualPreparationError, match="tenant"):
        prep.prepare(store(bad), "old-key", {"image_url": URL, "source_media_url": URL,
                                               "source_media_asset_id": "drive-1"})
    assert not any(call[0] == "post" for call in bad.calls)

    mismatch = HTTP({"id": "drive-1", "gym_id": "old-key", "content_hash": "0" * 32})
    with pytest.raises(prep.VisualPreparationError, match="MD5"):
        prep.prepare(store(mismatch), "old-key",
                     {"image_url": URL, "source_media_url": URL,
                      "source_media_asset_id": "drive-1"})
    assert not any(call[0] == "post" for call in mismatch.calls)


def test_raw_same_object_write_needs_no_render_lineage(monkeypatch):
    # An unchanged raw photo (source object IS the delivered object) is not a
    # transformation: it prepares through the owner one-read receipt path with
    # no render evidence and a null render receipt -- never the bundle RPC.
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"unchanged raw photo"
    digest = "md5:" + hashlib.md5(data).hexdigest()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    _owner_writer(monkeypatch)
    http = HTTP(known={("canonical_url", URL): "vg_same"}, fingerprint=digest)
    prepared = prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_url": URL})
    assert prepared["visual_group_key"] == "vg_same"
    assert prepared["byte_hash"] == "derived:" + digest
    posts = [call for call in http.calls if call[0] == "post"]
    assert [call[1] for call in posts] == ["visual_global_prepare_source_rendition"]
    assert posts[0][2]["p_render_receipt"] is None


@pytest.mark.parametrize("source_media_url", [None, "", "   "])
def test_guarded_row_missing_explicit_source_url_refuses_even_with_asset(monkeypatch, source_media_url):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="explicit source media URL"):
        prep.prepare(store(http), "old-key", {
            "image_url": URL,
            "source_media_url": source_media_url,
            "source_media_asset_id": "drive-1",
        })
    assert [call[1] for call in http.calls] == ["tenant_alias"]


def test_same_object_row_refuses_without_owner_receipt_producer(monkeypatch):
    # Fail closed: no configured owner receipt boundary means no preparation
    # and no registration RPC at all.
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    for env in ("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS", "AGENT_VISUAL_RECEIPT_OWNER_DSN",
                "AGENT_VISUAL_RECEIPT_OWNER_ROLE"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: b"exact")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="owner receipt producer"):
        prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_url": URL})
    assert not any(call[0] == "post" for call in http.calls)


def test_reuses_registered_delivered_url_group(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"exact"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    _owner_writer(monkeypatch)
    http = HTTP(known={("canonical_url", URL): "vg_same"},
                fingerprint="md5:" + hashlib.md5(data).hexdigest())
    prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_url": URL})
    # The already-registered group is reused: no raw-source bundle bootstrap,
    # exactly one owner-receipt source/rendition registration.
    assert [call[1] for call in http.calls if call[0] == "post"] == [
        "visual_global_prepare_source_rendition"]


def test_media_patch_missing_source_fails_closed_before_patch(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"new pixels"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    _owner_writer(monkeypatch)
    http = HTTP(known={("canonical_url", URL): "vg_same"},
                fingerprint="md5:" + hashlib.md5(data).hexdigest())
    with pytest.raises(prep.VisualPreparationError, match="explicit source media URL"):
        store(http).patch_image_url("old-key", "row-1", URL)
    assert not any(call[0] == "patch" for call in http.calls)


def test_media_patch_without_source_never_reaches_calendar_write(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    data = b"same pixels"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    _owner_writer(monkeypatch)
    http = HTTP(patch_failure=True, known={("canonical_url", URL): "vg_same"},
                fingerprint="md5:" + hashlib.md5(data).hexdigest())
    with pytest.raises(prep.VisualPreparationError, match="explicit source media URL"):
        store(http).patch_image_url("old-key", "row-1", URL)
    assert not any(call[0] == "patch" for call in http.calls)
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
    data = b"story pixels"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    _owner_writer(monkeypatch)
    http = HTTP(row={"id": "row-1", "gym_id": "old-key", "status": "pending",
                     "format": "story", "variant_status": "active",
                     "image_url": "https://media.example/old.jpg", "source_media_url": URL},
                known={("canonical_url", URL): "vg_same"},
                fingerprint="md5:" + hashlib.md5(data).hexdigest())
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


class _CASHTTP:
    """PostgREST-shaped fake that can swap one row field just before PATCH."""

    def __init__(self, row, concurrent=None):
        self.row = dict(row)
        self.concurrent = concurrent or {}
        self.params = None

    def get(self, url, *, params, headers, timeout):
        return Response([dict(self.row)])

    @staticmethod
    def _matches(actual, predicate):
        if predicate == "is.null":
            return actual is None
        if predicate.startswith('eq."') and predicate.endswith('"'):
            return str(actual) == predicate[4:-1]
        if predicate.startswith("eq."):
            return str(actual) == predicate[3:]
        if predicate.startswith("in.("):
            return str(actual) in predicate[4:-1].split(",")
        return True

    def patch(self, url, *, params, headers, json, timeout):
        self.row.update(self.concurrent)
        self.params = dict(params)
        if not all(self._matches(self.row.get(key), value)
                   for key, value in params.items() if key not in ("id", "gym_id")):
            return Response([])
        self.row.update(json)
        return Response([dict(self.row)])


def _prepared_payload(_account_key, _row_id, payload, *, current=None,
                      render_evidence=None):
    return {**payload, "visual_group_key": "vg_prepared",
            "byte_hash": "derived:md5:" + "b" * 32}


def test_guarded_image_patch_refuses_concurrent_same_status_slot_swap(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    current = {"id": "row-1", "gym_id": "old-key", "status": "pending",
               "format": "story", "image_url": "https://media.example/old.jpg",
               "source_media_url": "https://media.example/raw.jpg", "caption": "new",
               "slot_index": None, "thumbnail_url": "https://media.example/thumb-old.jpg",
               "visual_group_key": "vg_old", "byte_hash": "derived:md5:" + "a" * 32}
    http = _CASHTTP(current, {"slot_index": 2,
                              "thumbnail_url": "https://media.example/thumb-new.jpg",
                              "visual_group_key": "vg_concurrent"})
    calendar = store(http)
    monkeypatch.setattr(calendar, "_prepare_visual_media", _prepared_payload)

    assert calendar.patch_image_url("old-key", "row-1",
                                    "https://media.example/reburn.jpg") is None
    assert http.row["image_url"] == current["image_url"]
    assert http.row["visual_group_key"] == "vg_concurrent"
    assert http.params["slot_index"] == "is.null"
    assert http.params["thumbnail_url"] == 'eq."https://media.example/thumb-old.jpg"'
    assert http.params["visual_group_key"] == 'eq."vg_old"'


def test_guarded_image_patch_succeeds_when_observed_visual_row_is_unchanged(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    current = {"id": "row-1", "gym_id": "old-key", "status": "pending",
               "format": "story", "image_url": "https://media.example/old.jpg",
               "source_media_url": "https://media.example/raw.jpg", "caption": "new",
               "slot_index": None, "visual_group_key": "vg_old",
               "byte_hash": "derived:md5:" + "a" * 32}
    http = _CASHTTP(current)
    calendar = store(http)
    monkeypatch.setattr(calendar, "_prepare_visual_media", _prepared_payload)

    saved = calendar.patch_image_url("old-key", "row-1",
                                     "https://media.example/reburn.jpg")
    assert saved["image_url"] == "https://media.example/reburn.jpg"
    assert saved["visual_group_key"] == "vg_prepared"
    assert saved["caption"] == "new"


def test_guarded_image_patch_without_expected_row_preserves_status_allowlist(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    current = {"id": "row-1", "gym_id": "old-key", "status": "approved",
               "format": "story", "image_url": "https://media.example/old.jpg",
               "source_media_url": "https://media.example/raw.jpg"}
    http = _CASHTTP(current)
    calendar = store(http)
    monkeypatch.setattr(calendar, "_prepare_visual_media", _prepared_payload)

    assert calendar.patch_image_url("old-key", "row-1",
                                    "https://media.example/reburn.jpg") is None
    assert http.params is None
    assert http.row["image_url"] == current["image_url"]


def test_guarded_story_hold_recovery_refuses_concurrent_thumbnail_swap(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    current = {"id": "row-1", "gym_id": "old-key", "status": "pending",
               "format": "story", "account": "instagram", "post_date": "2026-10-04",
               "time_slot": "morning", "slot_index": None, "variant_status": "active",
               "created_at": "2026-10-03T12:00:00Z", "caption": "new",
               "image_url": None, "thumbnail_url": None, "source_media_url": None,
               "media_not_ready_reason": "Story media not ready: retry"}
    proposed = {**current, "image_url": "https://media.example/recovered.jpg",
                "source_media_url": "https://media.example/raw.jpg",
                "media_not_ready_reason": None}
    http = _CASHTTP(current, {"thumbnail_url": "https://media.example/concurrent-thumb.jpg"})
    calendar = store(http)
    monkeypatch.setattr(calendar, "_prepare_visual_media", _prepared_payload)

    assert calendar.recover_story_media_hold("old-key", current, proposed) is None
    assert http.row["image_url"] is None
    assert http.row["thumbnail_url"] == "https://media.example/concurrent-thumb.jpg"
    assert http.params["thumbnail_url"] == "is.null"


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
    _owner_writer(monkeypatch)
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="bytes"):
        store(http).insert_rows("old-key", [{"image_url": URL, "source_media_url": URL,
                                               "status": "pending"}])
    assert not any(call[1] == "content_calendar" and call[0] == "post" for call in http.calls)


def test_rejects_unverified_source_locator_and_hash_hint(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "receipt_owner")
    http = HTTP()
    digest = "md5:" + hashlib.md5(b"final").hexdigest()
    evidence = {"source_exact_url": "https://source.example/file",
                "delivered_exact_url": URL,
                "source_fingerprint": digest, "delivered_fingerprint": digest,
                "source_byte_length": 5, "delivered_byte_length": 5,
                "operation": "rehost"}
    with pytest.raises(prep.VisualPreparationError, match="source URL"):
        prep.prepare(store(http), "old-key", {"image_url": URL,
                     "source_media_url": "https://source.example/file"},
                     read_bytes=lambda url: b"final", render_evidence=evidence,
                     receipt_writer=lambda **kwargs: pytest.fail("no receipts for external source"),
                     isolated_test_callbacks=True)
    _owner_writer(monkeypatch)
    with pytest.raises(prep.VisualPreparationError, match="byte_hash"):
        prep.prepare(store(HTTP(known={("canonical_url", URL): "vg_same"},
                              fingerprint="md5:" + hashlib.md5(b"final").hexdigest())),
                     "old-key", {"image_url": URL, "source_media_url": URL,
                                 "byte_hash": "source:sha256:" + "0" * 64},
                     read_bytes=lambda url: b"final", isolated_test_callbacks=True)


def test_bundle_response_conflict_or_failure_aborts_preparation(monkeypatch):
    # The bundle RPC is now only the raw-source group bootstrap for an
    # unregistered scene; a conflicting bundle identity still aborts before
    # any owner receipt or source/rendition registration.
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: b"final")
    _owner_writer(monkeypatch)
    for response, message in [({"group_key": "vg_same", "fingerprint": "md5:" + "0" * 32}, "fingerprint"),
                              ({"group_key": "wrong", "fingerprint": "md5:" + hashlib.md5(b"final").hexdigest()}, "group")]:
        http = HTTP(bundle=response)
        with pytest.raises(prep.VisualPreparationError, match=message):
            prep.prepare(store(http), "old-key", {"image_url": URL, "source_media_url": URL})
        assert len([c for c in http.calls if c[1] == "visual_global_prepare_bundle"]) == 1
        assert not any(c[1] == "visual_global_prepare_source_rendition" for c in http.calls)
