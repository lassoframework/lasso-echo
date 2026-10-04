"""Python contract for the atomic scene-candidate calendar write."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs
from agent import visual_scene_register as vsr


TENANT = str(uuid.uuid4())
GROUP = "vg_testgroup"
PHASH = "abcdef0123456789"
FP = "md5:" + "0123456789abcdef" * 2
POSTER_PHASH = "1234567890abcdef"
POSTER_FP = "md5:" + "fedcba9876543210" * 2
IMAGE = "https://media.example/post.jpg"
VIDEO = "https://media.example/clip.mp4"
POSTER = "https://media.example/clip.jpg"


class _Response:
    text = ""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HTTP:
    def __init__(self, *, rpc_status=200, rpc_payload=None, held_indices=()):
        self.rest_calls = []
        self.rpc_calls = []
        self.rpc_status = rpc_status
        self.rpc_payload = rpc_payload
        self.held_indices = set(held_indices)

    def post(self, url, *, headers, json, timeout):
        if url.endswith("/rpc/visual_scene_insert_calendar_batch"):
            self.rpc_calls.append(json)
            if self.rpc_status >= 400:
                return _Response({}, status=self.rpc_status)
            if self.rpc_payload is not None:
                return _Response(self.rpc_payload)
            wrappers = []
            for index, item in enumerate(json["p_items"]):
                row = {**item["calendar_row"], "id": str(uuid.uuid4())}
                disposition = "inserted"
                if index in self.held_indices:
                    row.update({"status": "pending", "variant_status": "archived",
                                "media_not_ready_reason": "scene_review_hold",
                                "publish_claim_token": None,
                                "publish_reservation_day": None})
                    disposition = "scene_review_hold"
                wrappers.append({
                    "input_index": index,
                    "candidate_id": None,
                    "candidate_binding": None,
                    "disposition": disposition,
                    "row": row,
                })
                if item["candidate"] is not None:
                    candidate_id = str(uuid.uuid4())
                    wrappers[-1]["candidate_id"] = candidate_id
                    wrappers[-1]["candidate_binding"] = {
                        "candidate_id": candidate_id,
                        "registration_reused": False,
                        "tenant_id": item["candidate"]["tenant_id"],
                        "group_key": item["candidate"]["group_key"],
                        "object_role": item["candidate"]["object_role"],
                        "exact_url": item["candidate"]["exact_url"],
                        "phash": item["candidate"]["phash"],
                        "fingerprint": item["candidate"]["fingerprint"],
                    }
            return _Response(wrappers)
        self.rest_calls.append(json)
        return _Response([{**row, "id": str(uuid.uuid4())} for row in json], status=201)

    def delete(self, *args, **kwargs):  # pragma: no cover - a regression tripwire
        raise AssertionError("atomic scene writes must never run compensating DELETE")


def _store(monkeypatch, http):
    """Isolate the persistence payload from unrelated staging belts."""
    for name in ("_media_stage_belt", "_preserve_held_slots"):
        monkeypatch.setattr(pcs, name, lambda *args: args[-1])
    monkeypatch.setattr(pcs, "_stage_belts", lambda account_key, rows: rows)
    monkeypatch.setattr(pcs, "_dedupe_slots", lambda store, account_key, rows: rows)
    monkeypatch.setattr(pcs, "_reconcile_story_media_holds",
                        lambda store, account_key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_retry_story_hold_provenance", lambda *args: None)
    monkeypatch.setattr("agent.plan_horizon.belt_filter",
                        lambda account_key, rows: (rows, []))
    return pcs.SupabaseCalendarStore(
        url="https://db.example", service_key="test", http=http)


def _entry(role, url, phash=PHASH, fp=FP, stageable=True):
    return {"role": role, "phash": phash, "exact_url": url, "fingerprint": fp,
            "byte_length": 1024,
            "scene_fingerprint": f"scene:phash64:{phash}" if phash else None,
            "stageable": stageable}


def _candidate(objects, *, tenant=TENANT, group=GROUP):
    return {"kind": "visual_scene_candidate", "stage": "candidate",
            "tenant_id": tenant, "group_key": group,
            "usage_claimed": False, "counts_as_use": False,
            "observed_by": "visual_writer_prepare",
            "evidence_ref": "visual_writer_prepare:candidate_scene_evidence",
            "objects": objects}


def _prepare_with(candidates_by_image):
    def prepare(store, account_key, row, **kwargs):
        out = dict(row)
        candidate = candidates_by_image.get(row.get("image_url"))
        if candidate is not None:
            out["visual_group_key"] = candidate["group_key"]
            out["scene_candidate"] = candidate
        return out
    return prepare


def _row(image=IMAGE, thumbnail=None):
    row = {"post_date": "2026-10-04", "status": "pending", "format": "feed",
           "image_url": image}
    if thumbnail is not None:
        row["thumbnail_url"] = thumbnail
    return row


def _arm(monkeypatch, register=True):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    if register:
        monkeypatch.setenv("AGENT_VISUAL_SCENE_REGISTER", "1")
        monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "1")
    else:
        monkeypatch.delenv("AGENT_VISUAL_SCENE_REGISTER", raising=False)
        monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)


@pytest.mark.parametrize("prep,candidate,missing", [
    (None, "1", "AGENT_VISUAL_GLOBAL_WRITER_PREP=true"),
    ("1", None, "AGENT_VISUAL_SCENE_CANDIDATE=true"),
    ("1", "maybe", "AGENT_VISUAL_SCENE_CANDIDATE=true"),
])
def test_register_on_requires_real_prepare_and_explicit_candidate_config(
        monkeypatch, prep, candidate, missing):
    """Exercise real config/prepare routing; no preparation function mock."""
    monkeypatch.setenv("AGENT_VISUAL_SCENE_REGISTER", "1")
    if prep is None:
        monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    else:
        monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", prep)
    if candidate is None:
        monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    else:
        monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", candidate)
    http = _HTTP()

    with pytest.raises(pcs.PortalStoreError, match=missing):
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert http.rpc_calls == [] and http.rest_calls == []


def test_flag_off_keeps_normal_rest_insert_and_makes_zero_scene_rpc_calls(monkeypatch):
    _arm(monkeypatch, register=False)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    http = _HTTP()

    inserted = _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert len(inserted) == 1
    assert len(http.rest_calls) == 1
    assert "scene_candidate" not in http.rest_calls[0][0]
    assert http.rpc_calls == []


def test_ambiguous_registration_flag_stays_on_normal_rest_path(monkeypatch):
    _arm(monkeypatch, register=False)
    monkeypatch.setenv("AGENT_VISUAL_SCENE_REGISTER", "maybe")
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    http = _HTTP()

    _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert len(http.rest_calls) == 1 and http.rpc_calls == []


def test_photo_batch_uses_one_atomic_rpc_with_exact_displayed_bytes(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    http = _HTTP()

    inserted = _store(monkeypatch, http).insert_rows("old-gym-alias", [_row()])

    assert len(inserted) == 1 and http.rest_calls == []
    assert len(http.rpc_calls) == 1
    call = http.rpc_calls[0]
    assert call["p_account_key"] == "old-gym-alias"
    registration = call["p_items"][0]["candidate"]
    assert registration["tenant_id"] == TENANT
    assert registration["group_key"] == GROUP
    assert registration["object_role"] == "display"
    assert registration["exact_url"] == IMAGE
    assert registration["phash"] == PHASH
    assert registration["fingerprint"] == FP
    assert registration["evidence"]["verified_bytes"] == FP
    assert registration["usage_claimed"] is False
    assert registration["counts_as_use"] is False


def test_video_binds_only_poster_on_exact_thumbnail(monkeypatch):
    _arm(monkeypatch)
    candidate = _candidate([
        _entry("delivered", VIDEO),
        _entry("poster", POSTER, phash=POSTER_PHASH, fp=POSTER_FP),
    ])
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({VIDEO: candidate}))
    http = _HTTP()

    _store(monkeypatch, http).insert_rows(
        "gym-a", [_row(image=VIDEO, thumbnail=POSTER)])

    registration = http.rpc_calls[0]["p_items"][0]["candidate"]
    assert registration["object_role"] == "poster"
    assert registration["exact_url"] == POSTER
    assert registration["phash"] == POSTER_PHASH
    assert registration["fingerprint"] == POSTER_FP


def test_multi_row_batch_is_one_rpc_not_per_candidate(monkeypatch):
    _arm(monkeypatch)
    other = "https://media.example/other.jpg"
    monkeypatch.setattr("agent.visual_writer_prepare.prepare", _prepare_with({
        IMAGE: _candidate([_entry("delivered", IMAGE)]),
        other: _candidate([_entry("same_object", other)]),
    }))
    http = _HTTP()

    inserted = _store(monkeypatch, http).insert_rows(
        "gym-a", [_row(), _row(image=other)])

    assert len(inserted) == 2
    assert len(http.rpc_calls) == 1
    assert len(http.rpc_calls[0]["p_items"]) == 2
    assert http.rest_calls == []


def test_non_media_row_can_share_atomic_batch_without_candidate(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare", _prepare_with({}))
    http = _HTTP()
    row = {"post_date": "2026-10-04", "status": "pending", "format": "feed"}

    inserted = _store(monkeypatch, http).insert_rows("gym-a", [row])

    assert len(inserted) == 1
    assert http.rpc_calls[0]["p_items"][0]["candidate"] is None


@pytest.mark.parametrize("candidate", [
    None,
    _candidate([_entry("delivered", IMAGE, stageable=False)]),
    _candidate([_entry("delivered", "https://media.example/wrong.jpg")]),
])
def test_invalid_or_missing_display_candidate_fails_before_any_write(monkeypatch, candidate):
    _arm(monkeypatch)
    mapping = {} if candidate is None else {IMAGE: candidate}
    monkeypatch.setattr("agent.visual_writer_prepare.prepare", _prepare_with(mapping))
    http = _HTTP()

    with pytest.raises(pcs.PortalStoreError):
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert http.rpc_calls == [] and http.rest_calls == []


def test_candidate_group_must_equal_prepared_row_group():
    row = {**_row(), "visual_group_key": "vg_other"}
    with pytest.raises(vsr.SceneRegistrationError):
        vsr.build_items([row], [_candidate([_entry("delivered", IMAGE)])])


def test_conflicting_duplicate_exact_object_fails_before_any_write(monkeypatch):
    _arm(monkeypatch)
    conflicting = _candidate([
        _entry("delivered", IMAGE),
        _entry("same_object", IMAGE, phash="0000000000000000",
               fp="md5:" + "a" * 32),
    ])
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: conflicting}))
    http = _HTTP()

    with pytest.raises(pcs.PortalStoreError, match="conflicting staged scene candidates"):
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert http.rpc_calls == [] and http.rest_calls == []


def test_rpc_failure_never_attempts_compensating_delete(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    http = _HTTP(rpc_status=400)

    with pytest.raises(pcs.PortalStoreError) as excinfo:
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert excinfo.value.status == 502
    assert len(http.rpc_calls) == 1 and http.rest_calls == []


def test_incomplete_rpc_response_is_rejected(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    http = _HTTP(rpc_payload=[])

    with pytest.raises(pcs.PortalStoreError):
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])


def test_persisted_scene_hold_is_returned_as_held_not_ready(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    http = _HTTP(held_indices={0})

    (held,) = _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert held["status"] == "pending"
    assert held["variant_status"] == "archived"
    assert held["media_not_ready_reason"] == "scene_review_hold"
    assert held["publish_claim_token"] is None
    assert held["publish_reservation_day"] is None


def test_mislabeled_or_incomplete_scene_hold_response_is_rejected(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    row = {**_row(), "gym_id": "gym-a", "visual_group_key": GROUP,
           "id": str(uuid.uuid4()), "variant_status": "archived",
           "media_not_ready_reason": "scene_review_hold"}
    http = _HTTP(rpc_payload=[{"input_index": 0, "candidate_id": str(uuid.uuid4()),
                               "disposition": "inserted", "row": row}])

    with pytest.raises(pcs.PortalStoreError):
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])


def test_persisted_row_candidate_rebind_is_verified(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: _candidate([_entry("delivered", IMAGE)])}))
    candidate_id = str(uuid.uuid4())
    row = {**_row(image="https://media.example/changed.jpg"), "gym_id": "gym-a",
           "visual_group_key": GROUP, "id": str(uuid.uuid4())}
    binding = {"candidate_id": candidate_id, "tenant_id": TENANT,
               "registration_reused": False,
               "group_key": GROUP, "object_role": "display", "exact_url": IMAGE,
               "phash": PHASH, "fingerprint": FP}
    http = _HTTP(rpc_payload=[{"input_index": 0, "candidate_id": candidate_id,
                               "candidate_binding": binding,
                               "disposition": "inserted", "row": row}])

    with pytest.raises(pcs.PortalStoreError, match="rebind the persisted row"):
        _store(monkeypatch, http).insert_rows("gym-a", [_row()])

    assert http.rest_calls == []


def test_variant_candidate_insert_uses_same_atomic_rpc(monkeypatch):
    _arm(monkeypatch)
    candidate = _candidate([_entry("delivered", IMAGE)])
    monkeypatch.setattr("agent.visual_writer_prepare.prepare",
                        _prepare_with({IMAGE: candidate}))
    http = _HTTP()
    store = pcs.SupabaseCalendarStore(
        url="https://db.example", service_key="test", http=http)
    anchor = {"id": str(uuid.uuid4()), "gym_id": "gym-a", "account": "ig",
              "post_date": "2026-10-04", "format": "feed", "pillar": "story",
              "caption": "Anchor", "variant_of": None}
    monkeypatch.setattr(store, "get_row", lambda account, row_id: dict(anchor))

    inserted = store.create_variant_candidate("gym-a", anchor, IMAGE)

    assert inserted["variant_status"] == "candidate"
    assert len(http.rpc_calls) == 1 and http.rest_calls == []


def test_row_delivered_object_mirrors_sql_rule():
    assert vsr.row_delivered_object({"image_url": IMAGE}) == ("display", IMAGE)
    assert vsr.row_delivered_object(
        {"image_url": VIDEO, "thumbnail_url": POSTER}) == ("poster", POSTER)
    assert vsr.row_delivered_object(
        {"image_url": IMAGE, "thumbnail_url": IMAGE}) == ("display", IMAGE)
    assert vsr.row_delivered_object({"image_url": "  "}) is None
