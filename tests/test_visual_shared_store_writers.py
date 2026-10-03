"""Offline acceptance for the opt-in shared calendar media boundaries."""
import hashlib

import pytest

from agent import portal_calendar_store as pcs
from agent import visual_owner_receipts as owner
from agent import visual_writer_prepare as prep


KEY = "gym"
TENANT = "11111111-1111-4111-8111-111111111111"
RAW = "https://media.example/raw.jpg?version=1"
FINAL = "https://media.example/final.jpg?version=2"
DATA = {RAW: b"raw photo", FINAL: b"burned photo"}


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
    def __init__(self, current=None, race=None, bad_result=None, asset=None):
        self.current = current or calendar_row()
        self.race = race
        self.bad_result = bad_result
        self.asset = asset
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
    monkeypatch.setattr(prep, "_bytes_for_url", DATA.get)
    receipt_calls = []

    def receipts(**kwargs):
        receipt_calls.append(kwargs)
        return {name: "33333333-3333-4333-8333-333333333333" for name in (
            "source_read_receipt", "delivered_read_receipt", "render_receipt")}

    monkeypatch.setattr(owner, "default_writer", lambda: receipts)
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


def write(boundary, http, render=None, source=RAW):
    value = store(http)
    if boundary == "patch_media":
        return value.patch_media(KEY, "row-1", FINAL, source_media_url=source, render_evidence=render)
    if boundary == "swap_media":
        return value.swap_media(KEY, "row-1", FINAL, source_media_url=source, render_evidence=render)
    if boundary == "candidate":
        return value.create_variant_candidate(KEY, calendar_row(), FINAL,
                                              source_media_url=source, render_evidence=render)
    return value.insert_rows(KEY, [{"image_url": FINAL, "source_media_url": source,
                                   "gym_id": "foreign-key", "format": "story", "status": "pending"}],
                             render_evidence_by_url={FINAL: render} if render else None)


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
    http = HTTP(current=calendar_row(thumbnail_url="https://media.example/old-poster.jpg"),
                race={"thumbnail_url": concurrent_thumbnail})
    result = store(http).swap_media(
        KEY, "row-1", FINAL, source_media_url=RAW,
        extra_fields={"thumbnail_url": "https://media.example/new-poster.jpg"},
        render_evidence=evidence())
    assert result is None
    assert http.current["thumbnail_url"] == concurrent_thumbnail
    assert http.current["image_url"] != FINAL
    patch = next(call for call in http.calls if call[0] == "patch")
    assert patch[2]["thumbnail_url"] == 'eq."https://media.example/old-poster.jpg"'


def test_swap_checks_unchanged_thumbnail_in_persisted_result(armed):
    http = HTTP(current=calendar_row(thumbnail_url="https://media.example/old-poster.jpg"),
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
    rows = store(http).insert_rows(KEY, [{"image_url": RAW}, {"image_url": FINAL}])
    assert [r["image_url"] for r in rows] == [FINAL, RAW]
    assert len({r["id"] for r in rows}) == 2


def test_backfill_cannot_discard_existing_drive_asset_to_pass(armed):
    http = HTTP(current=calendar_row(image_url="", source_media_asset_id="asset-1",
                                    drive_file_id="asset-1"),
                asset={"id": "asset-1", "gym_id": KEY, "content_hash": "0" * 32})
    with pytest.raises(prep.VisualPreparationError, match="MD5"):
        store(http).patch_media(KEY, "row-1", FINAL)
    assert not any(call[0] == "patch" for call in http.calls)


def test_backfill_retains_attested_existing_drive_identity(armed):
    http = HTTP(current=calendar_row(image_url="", source_media_asset_id="asset-1",
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
        store(http).swap_media(KEY, "row-1", FINAL,
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
