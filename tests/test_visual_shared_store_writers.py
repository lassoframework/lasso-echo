"""Offline acceptance for the opt-in shared calendar media boundaries."""
import hashlib
import uuid

import pytest

from agent import portal_calendar_store as pcs
from agent import visual_owner_receipts as owner
from agent import visual_writer_prepare as prep


KEY = "gym"
TENANT = "11111111-1111-4111-8111-111111111111"
RAW = "https://media.example/raw.jpg?version=1"
FINAL = "https://media.example/final.jpg?version=2"
POSTER = "https://media.example/poster.jpg?version=3"
ALT = "https://media.example/alt.jpg?version=4"
DATA = {RAW: b"raw photo", FINAL: b"burned photo", POSTER: b"video poster",
        ALT: b"alt photo"}


def md5(data):
    return "md5:" + hashlib.md5(data).hexdigest()


def evidence(**changes):
    return {
        "source_exact_url": RAW, "delivered_exact_url": FINAL,
        "source_fingerprint": md5(DATA[RAW]), "delivered_fingerprint": md5(DATA[FINAL]),
        "source_byte_length": len(DATA[RAW]), "delivered_byte_length": len(DATA[FINAL]),
        "operation": "render", "observed_by": "test_renderer",
        "rendered_by": "test_renderer", "evidence_ref": "test_renderer:1", **changes,
    }


def poster_evidence(**changes):
    return {
        "source_exact_url": FINAL, "delivered_exact_url": POSTER,
        "source_fingerprint": md5(DATA[FINAL]), "delivered_fingerprint": md5(DATA[POSTER]),
        "source_byte_length": len(DATA[FINAL]), "delivered_byte_length": len(DATA[POSTER]),
        "operation": "render", "observed_by": "test_renderer",
        "rendered_by": "test_renderer", "evidence_ref": "test_renderer:poster-1", **changes,
    }


# Same-object owner receipts are keyed by receipt UUID so the PostgREST fake
# can answer the source/rendition RPC with the exact observed fingerprint.
_SAME_OBJECT_HASHES = {}


def calendar_row(**changes):
    return {"id": "row-1", "gym_id": KEY, "image_url": "https://media.example/old.jpg",
            "source_media_url": None, "caption": "Approved copy stays fixed",
            "status": "pending", "format": "story", "variant_status": "active",
            "account": "ig", "post_date": "2026-10-03", "time_slot": "morning",
            "created_at": "2026-10-03T00:00:00Z", "updated_at": "before", **changes}


class Response:
    status_code = 200
    text = ""

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class HTTP:
    def __init__(self, current=None, race=None, bad_result=None, asset=None, patch_race=None):
        self.current = current or calendar_row()
        self.race = race
        self.bad_result = bad_result
        self.asset = asset
        self.patch_race = patch_race
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        name = url.rsplit("/", 1)[-1]
        self.calls.append(("get", name, dict(params)))
        if name == "tenant_alias":
            key = params["alias_key"][3:]
            tenant = TENANT if key == KEY else "22222222-2222-4222-8222-222222222222"
            return Response([{"alias_key": key, "tenant_id": tenant}])
        if name == "visual_group_alias":
            return Response([{"group_key": "vg_scene"}])
        if name == "media_asset":
            return Response([self.asset] if self.asset else [])
        return Response([dict(self.current)])

    def post(self, url, *, json, headers, timeout):
        name = url.rsplit("/", 1)[-1]
        self.calls.append(("post", name, json))
        if name == "visual_global_prepare_source_rendition":
            if self.race:
                self.current.update(self.race)
            if json.get("p_render_receipt") is None:
                fingerprint = _SAME_OBJECT_HASHES[json["p_source_read_receipt"]]
                return Response({"group_key": json["p_group_key"],
                                 "source_fingerprint": fingerprint,
                                 "delivered_fingerprint": fingerprint,
                                 "usage_claimed": False})
            registrations = sum(1 for call in self.calls
                                if call[:2] == ("post", "visual_global_prepare_source_rendition"))
            if registrations % 2 == 0:
                return Response({"group_key": "vg_scene", "source_fingerprint": md5(DATA[FINAL]),
                                 "delivered_fingerprint": md5(DATA[POSTER]), "usage_claimed": False})
            return Response({"group_key": "vg_scene", "source_fingerprint": md5(DATA[RAW]),
                             "delivered_fingerprint": md5(DATA[FINAL]), "usage_claimed": False})
        if name == "visual_global_prepare_bundle":
            if self.race:
                self.current.update(self.race)
            return Response({"group_key": "vg_scene", "fingerprint": json["p_fingerprint"]})
        rows = [{**row, "id": row.get("id", f"new-id-{i}")} for i, row in enumerate(json)]
        if self.bad_result:
            rows = self.bad_result(rows)
        return Response(rows)

    def patch(self, url, *, params, json, headers, timeout):
        self.calls.append(("patch", url.rsplit("/", 1)[-1], dict(params), dict(json)))
        if self.patch_race:
            self.current.update(self.patch_race)
        for key in pcs._VISUAL_MEDIA_CAS_COLUMNS:
            if key not in params or not (params[key] == "is.null" or params[key].startswith('eq."')):
                continue
            expected = None if params[key] == "is.null" else params[key][4:-1].replace('\\"', '"').replace('\\\\', '\\')
            actual = self.current.get(key)
            if (None if actual is None else str(actual)) != expected:
                return Response([])
        self.current.update(json)
        rows = [{**self.current, "updated_at": "after"}]
        if self.bad_result:
            rows = self.bad_result(rows)
        return Response(rows)


def store(http):
    return pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=http)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setattr(prep, "_bytes_for_url", DATA.get)
    _SAME_OBJECT_HASHES.clear()
    receipt_calls = []

    def receipts(**kwargs):
        receipt_calls.append(kwargs)
        return {name: "33333333-3333-4333-8333-333333333333" for name in (
            "source_read_receipt", "delivered_read_receipt", "render_receipt")}

    def same_object_receipts(**kwargs):
        receipt_id = str(uuid.uuid4())
        _SAME_OBJECT_HASHES[receipt_id] = md5(kwargs["exact_bytes"])
        receipt_calls.append(kwargs)
        return {"read_receipt": receipt_id, "render_receipt": None}

    monkeypatch.setattr(owner, "default_writer", lambda: receipts)
    monkeypatch.setattr(owner, "default_same_object_writer", lambda: same_object_receipts)
    return receipt_calls


@pytest.fixture
def staging(monkeypatch):
    from agent import plan_horizon
    monkeypatch.setattr(plan_horizon, "belt_filter", lambda key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_stage_belts", lambda key, rows: rows)
    monkeypatch.setattr(pcs, "_media_stage_belt", lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, "_retry_story_hold_provenance", lambda store, key, rows: None)
    monkeypatch.setattr(pcs, "_reconcile_story_media_holds", lambda store, key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_preserve_held_slots", lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, "_dedupe_slots", lambda store, key, rows: rows)


def write(boundary, http, render=None, source=RAW, poster=None, thumbnail=None):
    value = store(http)
    if boundary == "patch_media":
        return value.patch_media(KEY, "row-1", FINAL, source_media_url=source,
                                 render_evidence=render, poster_render_evidence=poster)
    if boundary == "swap_media":
        return value.swap_media(KEY, "row-1", FINAL, source_media_url=source,
                                extra_fields={"thumbnail_url": thumbnail} if thumbnail is not None else None,
                                render_evidence=render, poster_render_evidence=poster)
    if boundary == "candidate":
        return value.create_variant_candidate(KEY, calendar_row(), FINAL,
                                              source_media_url=source, thumbnail_url=thumbnail,
                                              render_evidence=render, poster_render_evidence=poster)
    return value.insert_rows(KEY, [{"image_url": FINAL, "source_media_url": source,
                                   "gym_id": "foreign-key", "format": "story", "status": "pending",
                                   "thumbnail_url": thumbnail}],
                             render_evidence_by_url={FINAL: render} if render else None,
                             poster_render_evidence_by_url={(FINAL, thumbnail): poster} if poster else None)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
def test_each_boundary_prepares_both_exact_objects_before_writing(armed, staging, boundary):
    http = HTTP(current=calendar_row(image_url=""))
    result = write(boundary, http, evidence())
    saved = result[0] if isinstance(result, list) else result
    assert saved["gym_id"] == KEY
    assert saved["source_media_url"] == RAW
    assert saved["image_url"] == FINAL
    assert saved["byte_hash"] == "derived:" + md5(DATA[FINAL])
    assert saved["visual_group_key"] == "vg_scene"
    assert len(armed) == 1
    assert armed[0]["source_bytes"] == DATA[RAW]
    assert armed[0]["delivered_bytes"] == DATA[FINAL]
    registration = next(i for i, c in enumerate(http.calls) if c[1] == "visual_global_prepare_source_rendition")
    calendar_write = next(i for i, c in enumerate(http.calls) if c[1] == "content_calendar" and c[0] != "get")
    assert registration < calendar_write
    payload = http.calls[calendar_write][-1]
    if isinstance(payload, list):
        payload = payload[0]
    assert "render_evidence" not in payload
    assert "render_evidence_by_url" not in payload


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
def test_distinct_poster_evidence_reaches_prepare_before_calendar_persistence(armed, staging, boundary):
    http = HTTP(current=calendar_row(image_url="", thumbnail_url=POSTER))
    result = write(boundary, http, evidence(), poster=poster_evidence(), thumbnail=POSTER)
    assert result
    assert len(armed) == 2
    assert armed[1]["source_bytes"] == DATA[FINAL]
    assert armed[1]["delivered_bytes"] == DATA[POSTER]
    registrations = [i for i, call in enumerate(http.calls)
                     if call[:2] == ("post", "visual_global_prepare_source_rendition")]
    calendar_write = next(i for i, call in enumerate(http.calls)
                          if call[1] == "content_calendar" and call[0] != "get")
    assert len(registrations) == 2
    assert registrations[1] < calendar_write
    persisted = http.calls[calendar_write][-1]
    if isinstance(persisted, list):
        persisted = persisted[0]
    assert "poster_render_evidence" not in persisted
    assert "poster_render_evidence_by_url" not in persisted


@pytest.mark.parametrize("thumbnail", [None, "", FINAL])
def test_insert_blank_or_same_thumbnail_needs_no_poster_evidence(armed, staging, thumbnail):
    http = HTTP()
    result = write("insert_rows", http, evidence(), thumbnail=thumbnail)
    assert result
    assert len(armed) == 1


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
@pytest.mark.parametrize("poster", [None, poster_evidence(source_exact_url=RAW)])
def test_distinct_poster_without_matching_evidence_refuses_before_calendar_write(
        armed, staging, boundary, poster):
    http = HTTP(current=calendar_row(image_url="", thumbnail_url=POSTER))
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        write(boundary, http, evidence(), poster=poster, thumbnail=POSTER)
    assert not any(call[1] == "content_calendar" and call[0] != "get" for call in http.calls)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
@pytest.mark.parametrize("invalid", ["missing_evidence", "source_bytes", "delivered_bytes", "wrong_hash", "wrong_url"])
def test_unproved_rendition_refuses_every_boundary(armed, staging, monkeypatch, boundary, invalid):
    http = HTTP(current=calendar_row(image_url=""))
    render = evidence()
    if invalid == "missing_evidence":
        render = None
    elif invalid in ("source_bytes", "delivered_bytes"):
        missing = RAW if invalid == "source_bytes" else FINAL
        monkeypatch.setattr(prep, "_bytes_for_url", lambda url: None if url == missing else DATA.get(url))
    elif invalid == "wrong_hash":
        render = evidence(source_fingerprint=md5(b"different source"))
    else:
        render = evidence(source_exact_url=RAW.split("?")[0])
    with pytest.raises(prep.VisualPreparationError):
        write(boundary, http, render)
    assert armed == []
    assert not any(call[1] == "content_calendar" and call[0] != "get" for call in http.calls)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media"])
@pytest.mark.parametrize("race", [{"post_date": "2026-10-04"}, {"source_media_url": FINAL},
                                  {"status": "approved"}, {"publish_claim_token": "concurrent"},
                                  {"variant_status": "archived"}, {"image_url": RAW}])
def test_prepared_patch_refuses_concurrent_identity_or_release_change(armed, boundary, race):
    http = HTTP(current=calendar_row(image_url=""), race=race)
    assert write(boundary, http, evidence()) is None
    assert http.current["image_url"] != FINAL
    patch = next(call for call in http.calls if call[0] == "patch")
    assert patch[2]["gym_id"] == "eq.gym"
    assert patch[2]["image_url"] == 'eq.""'
    assert patch[2]["source_media_url"] == "is.null"
    assert patch[2]["post_date"] == 'eq."2026-10-03"'
    assert patch[2]["publish_claim_token"] == "is.null"


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate"])
@pytest.mark.parametrize("alter", [lambda rows: rows + rows, lambda rows: [{**rows[0], "gym_id": "foreign"}],
                                  lambda rows: [{**rows[0], "image_url": RAW}],
                                  lambda rows: [{k: v for k, v in rows[0].items() if k != "byte_hash"}]])
def test_never_reports_unverified_persistence_as_success(armed, boundary, alter):
    http = HTTP(current=calendar_row(image_url=""), bad_result=alter)
    assert write(boundary, http, evidence()) is None


@pytest.mark.parametrize("fmt", ["feed", "story"])
@pytest.mark.parametrize("source", [None, ""])
def test_swap_cannot_erase_existing_raw_source_to_avoid_receipts(armed, fmt, source):
    http = HTTP(current=calendar_row(source_media_url=RAW, format=fmt))
    with pytest.raises(prep.VisualPreparationError, match="cannot be discarded"):
        write("swap_media", http, source=source)
    assert not any(call[0] == "patch" for call in http.calls)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media"])
def test_approved_media_never_gets_prepared_or_patched(armed, boundary):
    http = HTTP(current=calendar_row(image_url="", status="approved"))
    assert write(boundary, http, evidence()) is None
    assert not any(call[0] in ("post", "patch") for call in http.calls)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media"])
@pytest.mark.parametrize("release_field", ["published_at", "late_post_id", "publish_claim_token"])
def test_pending_row_with_publication_or_claim_evidence_cannot_be_swapped(armed, boundary, release_field):
    http = HTTP(current=calendar_row(image_url="", **{release_field: "existing"}))
    assert write(boundary, http, evidence()) is None
    assert not any(call[0] in ("post", "patch") for call in http.calls)


def held_restage_row(**changes):
    return calendar_row(media_not_ready_reason="operator hold", published_at=None,
                        late_post_id=None, publish_claim_token=None,
                        publish_reservation_day=None, **changes)


def held_infographic_row(**changes):
    return calendar_row(image_url="https://cdn.example/igfill_2026-10-03_card.png",
                        source_media_url=None, media_not_ready_reason=(
                            "Photo-first hold: unverified infographic placeholder; approved gym photo required"),
                        thumbnail_url=None, published_at=None, late_post_id=None,
                        publish_claim_token=None, publish_reservation_day=None,
                        slot_index=0, scheduled_at="2026-10-03T14:00:00Z",
                        source_media_asset_id=None, **changes)


def source_asset():
    return {"id": "asset-1", "gym_id": KEY,
            "content_hash": hashlib.md5(DATA[RAW]).hexdigest()}


def restage_held(store_value, current, render=None, poster=None, thumbnail=None):
    extra_fields = {"source_media_asset_id": "asset-1"}
    if thumbnail is not None:
        extra_fields["thumbnail_url"] = thumbnail
    return store_value.restage_held_media(
        KEY, current, image_url=FINAL, source_media_url=RAW,
        extra_fields=extra_fields, render_evidence=render,
        poster_render_evidence=poster)


def replace_held_infographic(store_value, current, render=None, poster=None, thumbnail=None):
    return store_value.replace_future_infographic_media(
        KEY, current, image_url=FINAL, source_media_url=RAW,
        source_media_asset_id="asset-1",
        reason="Photo-first hold: unverified infographic placeholder; approved gym photo required",
        thumbnail_url=thumbnail, render_evidence=render, poster_render_evidence=poster)


@pytest.mark.parametrize("writer,row_builder", [
    (restage_held, held_restage_row), (replace_held_infographic, held_infographic_row),
])
def test_operator_replacements_prepare_claims_and_use_full_visual_cas(armed, writer, row_builder):
    current = row_builder()
    http = HTTP(current=current, asset=source_asset())
    result = writer(store(http), current, evidence())
    assert result["image_url"] == FINAL
    assert result["source_media_url"] == RAW
    assert result["visual_group_key"] == "vg_scene"
    assert result["byte_hash"] == "derived:" + md5(DATA[FINAL])
    assert len(armed) == 1
    assert armed[0]["source_bytes"] == DATA[RAW]
    assert armed[0]["delivered_bytes"] == DATA[FINAL]
    patch = next(call for call in http.calls if call[0] == "patch")
    assert set(pcs._VISUAL_MEDIA_CAS_COLUMNS).issubset(patch[2])
    assert patch[2]["caption"] == 'eq."Approved copy stays fixed"'
    assert patch[2]["media_not_ready_reason"] == (
        'eq."Photo-first hold: unverified infographic placeholder; approved gym photo required"'
        if writer is replace_held_infographic else 'eq."operator hold"')


@pytest.mark.parametrize("writer,row_builder", [
    (restage_held, held_restage_row), (replace_held_infographic, held_infographic_row),
])
def test_operator_replacements_refuse_missing_render_evidence_before_patch(armed, writer, row_builder):
    current = row_builder()
    http = HTTP(current=current, asset=source_asset())
    with pytest.raises(prep.VisualPreparationError):
        writer(store(http), current)
    assert not any(call[0] == "patch" for call in http.calls)


@pytest.mark.parametrize("writer,row_builder", [
    (restage_held, held_restage_row), (replace_held_infographic, held_infographic_row),
])
def test_operator_replacement_wrappers_forward_distinct_poster_proof(
        armed, writer, row_builder):
    current = row_builder()
    http = HTTP(current=current, asset=source_asset())
    result = writer(store(http), current, evidence(), poster_evidence(), POSTER)
    assert result["thumbnail_url"] == POSTER
    assert len(armed) == 2
    assert armed[1]["source_bytes"] == DATA[FINAL]
    assert armed[1]["delivered_bytes"] == DATA[POSTER]


@pytest.mark.parametrize("writer,row_builder", [
    (restage_held, held_restage_row), (replace_held_infographic, held_infographic_row),
])
def test_operator_replacement_wrappers_fail_closed_without_distinct_poster_proof(
        armed, writer, row_builder):
    current = row_builder()
    http = HTTP(current=current, asset=source_asset())
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        writer(store(http), current, evidence(), thumbnail=POSTER)
    assert not any(call[0] == "patch" for call in http.calls)


def test_patch_image_url_forwards_distinct_poster_proof(armed):
    current = calendar_row(image_url="https://media.example/old-burn.jpg",
                           source_media_url=RAW, thumbnail_url=POSTER)
    http = HTTP(current=current)
    result = store(http).patch_image_url(
        KEY, "row-1", FINAL, render_evidence=evidence(),
        poster_render_evidence=poster_evidence())
    assert result["image_url"] == FINAL
    assert len(armed) == 2


def test_patch_image_url_fails_closed_without_distinct_poster_proof(armed):
    current = calendar_row(image_url="https://media.example/old-burn.jpg",
                           source_media_url=RAW, thumbnail_url=POSTER)
    http = HTTP(current=current)
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        store(http).patch_image_url(KEY, "row-1", FINAL, render_evidence=evidence())
    assert not any(call[0] == "patch" for call in http.calls)


def test_story_hold_recovery_forwards_distinct_poster_proof(armed):
    current = calendar_row(image_url="", source_media_url=None, thumbnail_url=None,
                           media_not_ready_reason="Story media not ready: retry")
    proposed = {**current, "image_url": FINAL, "source_media_url": FINAL,
                "thumbnail_url": POSTER, "media_not_ready_reason": None}
    http = HTTP(current=current)
    result = store(http).recover_story_media_hold(
        KEY, current, proposed, poster_render_evidence=poster_evidence())
    assert result["image_url"] == FINAL
    assert result["thumbnail_url"] == POSTER
    assert len(armed) == 2


def test_story_hold_recovery_fails_closed_without_distinct_poster_proof(armed):
    current = calendar_row(image_url="", source_media_url=None, thumbnail_url=None,
                           media_not_ready_reason="Story media not ready: retry")
    proposed = {**current, "image_url": FINAL, "source_media_url": FINAL,
                "thumbnail_url": POSTER, "media_not_ready_reason": None}
    http = HTTP(current=current)
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        store(http).recover_story_media_hold(KEY, current, proposed)
    assert not any(call[0] == "patch" for call in http.calls)


@pytest.mark.parametrize("writer,row_builder", [
    (restage_held, held_restage_row), (replace_held_infographic, held_infographic_row),
])
def test_operator_replacements_keep_flag_off_patch_parity(monkeypatch, writer, row_builder):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    current = row_builder()
    http = HTTP(current=current)
    result = writer(store(http), current)
    assert result["image_url"] == FINAL
    assert result.get("visual_group_key") is None
    assert result.get("byte_hash") is None
    assert not any(call[0] == "post" for call in http.calls)


@pytest.mark.parametrize("field", ["visual_group_key", "byte_hash"])
def test_held_release_cas_refuses_lineage_race_after_script_read(armed, field):
    staged = held_restage_row(visual_group_key="vg_scene", byte_hash="derived:md5:verified")
    http = HTTP(current=dict(staged), patch_race={field: "concurrent"})
    assert store(http).restage_held_media(KEY, staged, release=True) is None
    patch = next(call for call in http.calls if call[0] == "patch")
    assert patch[2]["visual_group_key"] == 'eq."vg_scene"'
    assert patch[2]["byte_hash"] == 'eq."derived:md5:verified"'
    assert patch[2]["caption"] == 'eq."Approved copy stays fixed"'
    assert patch[2]["media_not_ready_reason"] == 'eq."operator hold"'
    assert patch[3] == {"media_not_ready_reason": None}
    assert http.current["media_not_ready_reason"] == "operator hold"


def test_held_release_requires_verified_lineage_when_flag_on(armed):
    staged = held_restage_row(visual_group_key="vg_scene")
    http = HTTP(current=dict(staged))
    assert store(http).restage_held_media(KEY, staged, release=True) is None
    assert not any(call[0] == "patch" for call in http.calls)


def test_held_release_with_verified_lineage_keeps_readback(armed):
    staged = held_restage_row(visual_group_key="vg_scene", byte_hash="derived:md5:verified")
    http = HTTP(current=dict(staged))
    result = store(http).restage_held_media(KEY, staged, release=True)
    assert result["media_not_ready_reason"] is None
    assert result["visual_group_key"] == staged["visual_group_key"]
    assert result["byte_hash"] == staged["byte_hash"]


def test_held_release_flag_off_keeps_original_patch_and_readback(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    staged = held_restage_row(visual_group_key="vg_scene", byte_hash="derived:md5:verified")
    http = HTTP(current=dict(staged))
    result = store(http).restage_held_media(KEY, staged, release=True)
    assert result["media_not_ready_reason"] is None
    patch = next(call for call in http.calls if call[0] == "patch")
    assert patch[2] == {
        "id": "eq.row-1", "gym_id": "eq.gym", "status": "eq.pending",
        "post_date": "eq.2026-10-03", "image_url": "eq.https://media.example/old.jpg",
        "source_media_url": "is.null", "source_media_asset_id": "is.null",
        "media_not_ready_reason": "eq.operator hold", "account": "eq.ig",
        "format": "eq.story", "variant_status": "eq.active",
        "created_at": "eq.2026-10-03T00:00:00Z",
    }
    assert patch[3] == {"media_not_ready_reason": None}


def test_backfill_preserves_existing_raw_source_and_requires_its_evidence(armed):
    http = HTTP(current=calendar_row(image_url="", source_media_url=RAW))
    result = store(http).patch_media(KEY, "row-1", FINAL, render_evidence=evidence())
    assert result["source_media_url"] == RAW
    http = HTTP(current=calendar_row(image_url="", source_media_url=RAW))
    with pytest.raises(prep.VisualPreparationError, match="rendition lineage"):
        store(http).patch_media(KEY, "row-1", FINAL)
    assert not any(call[0] == "patch" for call in http.calls)


def test_insert_batch_never_posts_when_one_rendition_is_unproved(armed, staging):
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError):
        store(http).insert_rows(KEY, [{"image_url": RAW}, {"image_url": FINAL, "source_media_url": RAW}])
    assert not any(call[1] == "content_calendar" and call[0] == "post" for call in http.calls)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
def test_supplied_render_source_cannot_be_silently_omitted(armed, staging, boundary):
    http = HTTP(current=calendar_row(image_url=""))
    with pytest.raises(prep.VisualPreparationError, match="does not bind"):
        write(boundary, http, evidence(), source=None)
    assert not any(call[1] == "content_calendar" and call[0] != "get" for call in http.calls)


def test_candidate_cannot_link_a_foreign_anchor(armed):
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="anchor"):
        store(http).create_variant_candidate(KEY, calendar_row(gym_id="foreign"), FINAL)
    assert http.calls == []


@pytest.mark.parametrize("identity", [{"gym_id": "foreign-key"}, {"id": "different-row"}])
@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate"])
def test_unscoped_row_read_cannot_be_used_for_preparation(armed, boundary, identity):
    http = HTTP(current=calendar_row(image_url="", **identity))
    if boundary == "candidate":
        with pytest.raises(prep.VisualPreparationError, match="anchor"):
            write(boundary, http, evidence())
    else:
        assert write(boundary, http, evidence()) is None
    assert not any(call[0] in ("post", "patch") for call in http.calls)


@pytest.mark.parametrize("changed", [
    {"post_date": "2026-10-04"}, {"account": "fb"}, {"format": "feed"},
    {"pillar": "new-pillar"}, {"caption": "New client copy"}, {"variant_of": "new-group"},
])
def test_candidate_rejects_stale_caller_slot_and_copy(armed, changed):
    http = HTTP(current=calendar_row(**changed))
    with pytest.raises(prep.VisualPreparationError, match="anchor changed"):
        store(http).create_variant_candidate(KEY, calendar_row(), FINAL,
                                            source_media_url=RAW, render_evidence=evidence())
    assert not any(call[0] in ("post", "patch") for call in http.calls)


def test_candidate_from_existing_candidate_checks_its_snapshot_and_stable_group(armed):
    candidate = calendar_row(id="candidate-1", variant_of="row-1", caption="Candidate copy",
                             variant_status="candidate")

    class CandidateHTTP(HTTP):
        def get(self, url, *, params, headers, timeout):
            if url.endswith("content_calendar") and params["id"] == "eq.candidate-1":
                self.calls.append(("get", "content_calendar", dict(params)))
                return Response([dict(candidate)])
            return super().get(url, params=params, headers=headers, timeout=timeout)

    http = CandidateHTTP()
    result = store(http).create_variant_candidate(KEY, candidate, FINAL,
                                                 source_media_url=RAW, render_evidence=evidence())
    assert result["variant_of"] == "row-1"
    assert result["caption"] == "Candidate copy"
    reads = [call[2]["id"] for call in http.calls if call[:2] == ("get", "content_calendar")]
    assert reads == ["eq.candidate-1", "eq.row-1"]


def test_swap_cannot_overwrite_thumbnail_changed_during_preparation(armed):
    concurrent_thumbnail = "https://media.example/concurrent-poster.jpg"
    # The interim writer can attest only the selected image object. Use that
    # same object for the candidate so this test reaches the thumbnail CAS
    # race; a distinct unverified poster must be held before persistence.
    http = HTTP(current=calendar_row(thumbnail_url=FINAL),
                race={"thumbnail_url": concurrent_thumbnail})
    result = store(http).swap_media(
        KEY, "row-1", FINAL, source_media_url=RAW,
        extra_fields={"thumbnail_url": FINAL},
        render_evidence=evidence())
    assert result is None
    assert http.current["thumbnail_url"] == concurrent_thumbnail
    assert http.current["image_url"] != FINAL
    patch = next(call for call in http.calls if call[0] == "patch")
    assert patch[2]["thumbnail_url"] == f'eq."{FINAL}"'


def test_swap_checks_unchanged_thumbnail_in_persisted_result(armed):
    http = HTTP(current=calendar_row(thumbnail_url=FINAL),
                bad_result=lambda rows: [{**rows[0], "thumbnail_url": "https://media.example/other.jpg"}])
    assert write("swap_media", http, evidence()) is None


@pytest.mark.parametrize("alter", [lambda rows: rows + rows, lambda rows: [],
                                  lambda rows: [{**rows[0], "gym_id": "foreign"}],
                                  lambda rows: [{**rows[0], "byte_hash": "derived:md5:" + "0" * 32}]])
def test_insert_rejects_an_unverified_persistence_receipt(armed, staging, alter):
    http = HTTP(bad_result=alter)
    with pytest.raises(pcs.PortalStoreError, match="unverified visual rows"):
        write("insert_rows", http, evidence())


def test_insert_accepts_reordered_verified_rows_with_unique_ids(armed, staging):
    http = HTTP(bad_result=lambda rows: list(reversed(rows)))
    rows = store(http).insert_rows(KEY, [
        {"image_url": RAW, "source_media_url": RAW},
        {"image_url": FINAL, "source_media_url": FINAL},
    ])
    assert [r["image_url"] for r in rows] == [FINAL, RAW]
    assert len({r["id"] for r in rows}) == 2


def test_insert_scopes_shared_poster_evidence_to_the_exact_image_edge(armed, staging, monkeypatch):
    """A shared poster URL cannot let one video's proof attest another video."""
    other_image = "https://media.example/another-video.mp4"
    shared_poster = POSTER
    first_proof = {"edge": "first"}
    second_proof = {"edge": "second"}
    seen = []

    def prepare_row(_self, _account_key, row, render_evidence=None, poster_render_evidence=None):
        seen.append((row["image_url"], row["thumbnail_url"], poster_render_evidence))
        return {**row, "visual_group_key": "vg_scene", "byte_hash": "derived:edge"}

    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_prepare_visual_row", prepare_row)
    rows = store(HTTP()).insert_rows(
        KEY,
        [
            {"image_url": FINAL, "source_media_url": FINAL, "thumbnail_url": shared_poster},
            {"image_url": other_image, "source_media_url": other_image, "thumbnail_url": shared_poster},
        ],
        poster_render_evidence_by_url={
            (FINAL, shared_poster): first_proof,
            (other_image, shared_poster): second_proof,
        },
    )
    assert len(rows) == 2
    assert seen == [
        (FINAL, shared_poster, first_proof),
        (other_image, shared_poster, second_proof),
    ]


def test_insert_rejects_thumbnail_only_poster_evidence_lookup(armed, staging):
    """The former thumbnail-only map shape must not be silently accepted."""
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        store(http).insert_rows(
            KEY,
            [{"image_url": FINAL, "source_media_url": RAW, "thumbnail_url": POSTER}],
            render_evidence_by_url={FINAL: evidence()},
            poster_render_evidence_by_url={POSTER: poster_evidence()},
        )
    assert not any(call[1] == "content_calendar" and call[0] == "post" for call in http.calls)


def test_backfill_cannot_discard_existing_drive_asset_to_pass(armed):
    http = HTTP(current=calendar_row(image_url="", source_media_url=FINAL,
                                    source_media_asset_id="asset-1",
                                    drive_file_id="asset-1"),
                asset={"id": "asset-1", "gym_id": KEY, "content_hash": "0" * 32})
    with pytest.raises(prep.VisualPreparationError, match="MD5"):
        store(http).patch_media(KEY, "row-1", FINAL)
    assert not any(call[0] == "patch" for call in http.calls)


def test_backfill_retains_attested_existing_drive_identity(armed):
    http = HTTP(current=calendar_row(image_url="", source_media_url=FINAL,
                                    source_media_asset_id="asset-1",
                                    drive_file_id="asset-1"),
                asset={"id": "asset-1", "gym_id": KEY,
                       "content_hash": hashlib.md5(DATA[FINAL]).hexdigest()})
    result = store(http).patch_media(KEY, "row-1", FINAL)
    assert result["source_media_asset_id"] == "asset-1"
    assert result["drive_file_id"] == "asset-1"


def test_swap_rejects_foreign_tenant_source_asset(armed):
    http = HTTP(asset={"id": "asset-1", "gym_id": "foreign-key",
                       "content_hash": hashlib.md5(DATA[FINAL]).hexdigest()})
    with pytest.raises(prep.VisualPreparationError, match="another tenant"):
        store(http).swap_media(KEY, "row-1", FINAL, source_media_url=FINAL,
                              extra_fields={"source_media_asset_id": "asset-1"})
    assert not any(call[0] == "patch" for call in http.calls)


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
def test_flag_off_retains_legacy_write_without_preparation(monkeypatch, staging, boundary):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.setattr(prep, "prepare", lambda *args, **kwargs: pytest.fail("flag OFF must not prepare"))
    http = HTTP(current=calendar_row(image_url=""))
    assert write(boundary, http, evidence())
    assert not any(call[1].startswith("visual_global_") or call[1] == "tenant_alias" for call in http.calls)
    if boundary == "swap_media":
        assert not any(call[0] == "get" for call in http.calls)
    if boundary == "patch_media":
        assert sum(call[0] == "get" for call in http.calls) == 1
    if boundary == "candidate":
        assert not any(call[0] == "get" for call in http.calls)


def test_flag_off_retains_distinct_poster_without_preparation(monkeypatch, staging):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.setattr(prep, "prepare", lambda *args, **kwargs: pytest.fail("flag OFF must not prepare"))
    http = HTTP()
    assert write("insert_rows", http, thumbnail=POSTER)
    assert not any(call[1].startswith("visual_global_") or call[1] == "tenant_alias" for call in http.calls)



# ---- Drive original-source lineage (source_media_content_hash) ----------------
# A media replacement must never keep the OLD Drive asset's content hash once the
# source changes; the hash is retained only when the exact same source is kept.

HASH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _patch_payload(http):
    patch_call = next(c for c in http.calls if c[0] == "patch")
    return patch_call[-1]


def test_swap_payload_for_a_new_source_clears_the_old_drive_content_hash(armed, monkeypatch):
    # This unit covers the identity-clearing payload before the owner-only
    # receipt RPC. The RPC fake models the older two-edge sequence globally,
    # so using it for this one-edge case would assert fixture ordering rather
    # than source-lineage behavior.
    monkeypatch.setattr(
        prep, "prepare",
        lambda _store, _key, row, **_kwargs: {**row, "visual_group_key": "vg_scene",
                                                "byte_hash": "derived:test"})
    current = calendar_row(format="feed", source_media_url=RAW,
                           source_media_asset_id="asset-old", drive_file_id="drive-old",
                           source_media_content_hash=HASH)
    http = HTTP(current=current)
    payload = store(http)._prepare_visual_replacement(
        KEY, current, {"image_url": POSTER, "source_media_url": FINAL},
        poster_evidence())
    assert payload["source_media_url"] == FINAL
    assert payload["source_media_content_hash"] is None
    assert payload["source_media_asset_id"] is None


def test_backfill_without_asset_identity_clears_the_content_hash(armed):
    current = calendar_row(image_url="", format="feed", source_media_url=RAW,
                           source_media_content_hash=HASH)
    http = HTTP(current=current)
    result = store(http).patch_media(
        KEY, "row-1", FINAL, source_media_url=RAW, render_evidence=evidence())
    assert result is not None
    assert result["source_media_url"] == RAW
    assert result["source_media_content_hash"] is None
    payload = _patch_payload(http)
    assert payload["source_media_content_hash"] is None


def test_same_source_and_asset_identity_retains_content_hash(armed, monkeypatch):
    monkeypatch.setattr(
        prep, "prepare",
        lambda _store, _key, row, **_kwargs: {**row, "visual_group_key": "vg_scene",
                                                "byte_hash": "derived:test"})
    current = calendar_row(source_media_url=RAW, source_media_asset_id="asset-old",
                           source_media_content_hash=HASH)
    payload = store(HTTP(current=current))._prepare_visual_replacement(
        KEY, current,
        {"image_url": FINAL, "source_media_url": RAW,
         "source_media_asset_id": "asset-old"}, evidence())
    assert payload.get("source_media_content_hash", HASH) == HASH


def test_story_reburn_with_same_source_evidence_retains_the_content_hash(armed):
    current = calendar_row(format="story", source_media_url=RAW,
                           source_media_content_hash=HASH)
    http = HTTP(current=current)
    result = store(http).patch_image_url(KEY, "row-1", FINAL,
                                         render_evidence=evidence())
    assert result is not None
    assert result["source_media_url"] == RAW
    assert result["source_media_content_hash"] == HASH


def test_replacement_payload_clears_a_stale_content_hash(armed, monkeypatch):
    # Exercise the shared replacement payload loop independently of the owner
    # receipt boundary. A changed source must clear its old Drive byte identity.
    monkeypatch.setattr(
        prep, "prepare",
        lambda _store, _key, row, **_kwargs: {**row, "visual_group_key": "vg_scene",
                                                "byte_hash": "derived:test"})
    current = calendar_row(format="feed", variant_status="archived",
                           source_media_url=RAW, source_media_asset_id="asset-old",
                           source_media_content_hash=HASH)
    http = HTTP(current=current)
    payload = store(http)._prepare_visual_media(
        KEY, "row-1", {"image_url": FINAL}, current=current)
    assert payload["source_media_url"] is None
    assert payload["source_media_content_hash"] is None


def test_same_source_with_a_new_asset_clears_the_old_content_hash(armed, monkeypatch):
    monkeypatch.setattr(
        prep, "prepare",
        lambda _store, _key, row, **_kwargs: {**row, "visual_group_key": "vg_scene",
                                                "byte_hash": "derived:test"})
    current = calendar_row(source_media_url=RAW, source_media_asset_id="asset-old",
                           drive_file_id="asset-old", source_media_content_hash=HASH)
    payload = store(HTTP(current=current))._prepare_visual_replacement(
        KEY, current,
        {"image_url": FINAL, "source_media_url": RAW,
         "source_media_asset_id": "asset-new", "drive_file_id": "asset-new"},
        evidence())
    assert payload["source_media_asset_id"] == "asset-new"
    assert payload["source_media_content_hash"] is None


def _prepared_with_scene_candidate(_store, _key, row, **_kwargs):
    return {**row, "visual_group_key": "vg_scene", "byte_hash": "derived:test",
            "scene_candidate": {"candidate_id": "33333333-3333-4333-8333-333333333333",
                                "usage_claimed": False}}


def _calendar_write_payloads(http):
    writes = [call[-1] for call in http.calls
              if call[1] == "content_calendar" and call[0] in ("post", "patch")]
    return [row for payload in writes for row in (payload if isinstance(payload, list) else [payload])]


@pytest.mark.parametrize("boundary", ["patch_media", "swap_media", "candidate", "insert_rows"])
def test_prepared_scene_candidate_never_reaches_shared_calendar_writes(
        armed, staging, monkeypatch, boundary):
    monkeypatch.setattr(prep, "prepare", _prepared_with_scene_candidate)
    http = HTTP(current=calendar_row(image_url=""))
    assert write(boundary, http, evidence())
    assert _calendar_write_payloads(http)
    assert all("scene_candidate" not in payload for payload in _calendar_write_payloads(http))


def test_prepared_scene_candidate_never_reaches_patch_image_write(armed, monkeypatch):
    monkeypatch.setattr(prep, "prepare", _prepared_with_scene_candidate)
    http = HTTP(current=calendar_row(format="story", source_media_url=RAW))
    assert store(http).patch_image_url(KEY, "row-1", FINAL, render_evidence=evidence())
    assert _calendar_write_payloads(http)
    assert all("scene_candidate" not in payload for payload in _calendar_write_payloads(http))


def test_prepared_scene_candidate_never_reaches_feed_rehost_patch(armed, monkeypatch):
    monkeypatch.setattr(prep, "prepare", _prepared_with_scene_candidate)
    http = HTTP(current=calendar_row(format="feed", image_url=RAW, source_media_url=RAW))
    assert store(http).patch_image_url(
        KEY, "row-1", FINAL, render_evidence=evidence(operation="rehost"))
    assert _calendar_write_payloads(http)
    assert all("scene_candidate" not in payload for payload in _calendar_write_payloads(http))
