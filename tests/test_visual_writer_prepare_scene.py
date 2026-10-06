"""Scene-fingerprint evidence at the visual writer-prepare boundary.

With both scene flags OFF, exact-byte preparation performs no pHash work and
adds no scene evidence. Candidate-only mode emits advisory metadata without a
scene RPC. The DRAFT guard, when explicitly armed, requires owner-attested
staging for the displayed object; unknown guard state and undecodable display
bytes fail closed. Staging itself never records use.
"""
import io
import re
import uuid

import pytest

from agent import portal_calendar_store as pcs
from agent import visual_owner_receipts as owner
from agent import visual_writer_prepare as prep

TENANT = "11111111-1111-4111-8111-111111111111"
URL = "https://media.example/selected.jpg"
FP_RE = re.compile(r"^scene:phash64:[0-9a-f]{16}$")


class Response:
    def __init__(self, value, status_code=200, text=""):
        self.value = value
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.value


class HTTP:
    def __init__(self, fingerprint=None):
        self.calls = []
        self.registered = False
        self.fingerprint = fingerprint

    def post(self, url, *, headers, json, timeout):
        name = url.rsplit("/", 1)[-1]
        self.calls.append(("post", name, json))
        if name == "visual_global_prepare_bundle":
            self.registered = True
            return Response({"group_key": "vg_same", "fingerprint": json["p_fingerprint"]})
        if name == "visual_scene_register_candidate":
            return Response("33333333-3333-4333-8333-333333333333")
        if name == "visual_global_prepare_source_rendition":
            return Response({"group_key": json["p_group_key"],
                             "source_fingerprint": self.fingerprint,
                             "delivered_fingerprint": self.fingerprint,
                             "usage_claimed": False})
        return Response([json[0]] if isinstance(json, list) else [])

    def get(self, url, *, params, headers, timeout):
        self.calls.append(("get", url.rsplit("/", 1)[-1], params))
        if url.endswith("tenant_alias"):
            return Response([{"alias_key": params["alias_key"][3:], "tenant_id": TENANT}])
        if url.endswith("visual_group_alias"):
            return Response([{"group_key": "vg_same"}] if self.registered else [])
        return Response([])


def _png_bytes():
    Image = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw
    img = Image.new("L", (128, 128), 30)
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 70, 60], fill=220)
    d.ellipse([70, 60, 120, 110], fill=200)
    for i in range(0, 60, 8):
        d.line([(10 + i, 118), (10, 118 - i)], fill=180, width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def own_host(monkeypatch):
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")


@pytest.fixture
def writer_calls(monkeypatch):
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return {"read_receipt": str(uuid.uuid4()), "render_receipt": None}

    monkeypatch.setattr(owner, "default_same_object_writer", lambda: writer)
    def scene_writer(**args):
        return {"receipt_id": str(uuid.uuid4()),
                "phash": prep._scene_fingerprint(args["exact_bytes"]).rsplit(":", 1)[-1],
                "fingerprint": prep._md5(args["exact_bytes"])}
    monkeypatch.setattr(owner, "default_scene_writer", lambda: scene_writer)
    return calls


def _store(http):
    return pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=http)


def _same_object_row(**extra):
    return {"image_url": URL, "source_media_url": URL, "status": "pending", **extra}


def _md5(data):
    import hashlib
    return "md5:" + hashlib.md5(data).hexdigest()


def _no_scene_surface(http):
    """No scene RPC is posted and no p_scene_* key appears on ANY payload."""
    assert not any(call[1] == "visual_scene_record_use" for call in http.calls)
    for call in http.calls:
        if call[0] == "post":
            assert not any(str(k).startswith("p_scene_") for k in call[2])


def test_guarded_scene_evidence_registers_owner_attested_candidate(
        monkeypatch, writer_calls):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", "true")
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    data = _png_bytes()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    http = HTTP(fingerprint=_md5(data))
    # No post_date is needed to stage verified evidence.
    row = prep.prepare(_store(http), "old-key", _same_object_row())
    assert row["visual_group_key"] == "vg_same"
    assert writer_calls, "same-object owner writer must have produced a receipt"
    fp = writer_calls[0]["evidence"]["scene_fingerprint"]
    assert FP_RE.fullmatch(fp)
    scene_calls = [call for call in http.calls if call[1] == "visual_scene_register_candidate"]
    assert len(scene_calls) == 1
    assert row["scene_candidate"]["candidate_id"] == "33333333-3333-4333-8333-333333333333"
    assert scene_calls[0][2]["p_evidence"]["owner_phash_receipt"]
    assert not row["scene_candidate"]["counts_as_use"]


def test_scene_flags_off_preserve_exact_byte_evidence_and_skip_phash(
        monkeypatch, writer_calls):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    data = _png_bytes()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    fingerprints = []
    monkeypatch.setattr(prep, "_scene_fingerprint",
                        lambda value: fingerprints.append(value) or "scene:phash64:0123456789abcdef")
    http = HTTP(fingerprint=_md5(data))
    row = prep.prepare(_store(http), "old-key", _same_object_row())
    assert row["visual_group_key"] == "vg_same"
    assert fingerprints == []
    assert "scene_fingerprint" not in writer_calls[0]["evidence"]
    bundle = next(call[2] for call in http.calls
                  if call[0] == "post" and call[1] == "visual_global_prepare_bundle")
    assert "scene_fingerprint" not in bundle["p_evidence"]
    assert "scene_candidate" not in row
    assert not any(call[1] == "visual_scene_register_candidate" for call in http.calls)


def test_scene_candidate_phash_is_computed_once_per_exact_object(
        monkeypatch, writer_calls):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "true")
    monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    data = _png_bytes()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    fingerprints = []

    def fingerprint(value):
        fingerprints.append(value)
        return "scene:phash64:0123456789abcdef"

    monkeypatch.setattr(prep, "_scene_fingerprint", fingerprint)
    http = HTTP(fingerprint=_md5(data))
    row = prep.prepare(_store(http), "old-key", _same_object_row())
    assert row["scene_candidate"]["objects"][0]["scene_fingerprint"] == (
        "scene:phash64:0123456789abcdef")
    assert fingerprints == [data]
    assert writer_calls[0]["evidence"]["scene_fingerprint"] == (
        "scene:phash64:0123456789abcdef")
    assert not any(call[1] == "visual_scene_register_candidate" for call in http.calls)


def test_candidate_only_undecodable_bytes_record_null_without_gating(
        monkeypatch, writer_calls):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "true")
    monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    data = b"exact bytes, but not an image"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    http = HTTP(fingerprint=_md5(data))
    row = prep.prepare(_store(http), "old-key", _same_object_row())
    assert row["visual_group_key"] == "vg_same"
    evidence = writer_calls[0]["evidence"]
    assert "scene_fingerprint" in evidence
    assert evidence["scene_fingerprint"] is None
    assert row["scene_candidate"]["objects"][0]["stageable"] is False
    assert not any(call[1] == "visual_scene_register_candidate" for call in http.calls)


def test_guarded_undecodable_displayed_bytes_fail_closed(monkeypatch, writer_calls):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", "true")
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    data = b"exact bytes, but not an image"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    http = HTTP(fingerprint=_md5(data))
    with pytest.raises(prep.VisualPreparationError, match="pHash"):
        prep.prepare(_store(http), "old-key", _same_object_row())
    assert not any(call[1] == "visual_scene_register_candidate" for call in http.calls)


def test_writer_prep_flag_off_is_legacy_passthrough(monkeypatch, writer_calls):
    """Both flags OFF: the row passes through untouched, no RPCs, no evidence."""
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    http = HTTP()
    row = _same_object_row()
    assert prep.prepare(_store(http), "old-key", row) is row
    assert http.calls == []
    assert writer_calls == []


def test_ambiguous_scene_guard_refuses_preparation_before_owner_write(monkeypatch, writer_calls):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", "maybe")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="ambiguous"):
        prep.prepare(_store(http), "old-key", _same_object_row())
    assert http.calls == []
    assert writer_calls == []


class CalendarHTTP(HTTP):
    """Echo calendar responses without accepting non-column advisory fields."""

    def __init__(self, row, fingerprint):
        super().__init__(fingerprint=fingerprint)
        self.row = row

    def get(self, url, *, params, headers, timeout):
        if url.endswith("content_calendar"):
            return Response([dict(self.row)])
        return super().get(url, params=params, headers=headers, timeout=timeout)

    def post(self, url, *, headers, json, timeout):
        if url.endswith("content_calendar"):
            self.calls.append(("post", "content_calendar", json))
            assert all("scene_candidate" not in row for row in json)
            return Response([{**row, "id": "inserted-row"} for row in json])
        return super().post(url, headers=headers, json=json, timeout=timeout)

    def patch(self, url, *, params, headers, json, timeout):
        self.calls.append(("patch", "content_calendar", json))
        assert "scene_candidate" not in json
        return Response([{**self.row, **json}])


@pytest.fixture
def calendar_without_stage_belts(monkeypatch):
    from agent import plan_horizon
    monkeypatch.setattr(plan_horizon, "belt_filter", lambda key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_stage_belts", lambda key, rows: rows)
    monkeypatch.setattr(pcs, "_media_stage_belt", lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, "_retry_story_hold_provenance", lambda store, key, rows: None)
    monkeypatch.setattr(pcs, "_reconcile_story_media_holds", lambda store, key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_preserve_held_slots", lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, "_dedupe_slots", lambda store, key, rows: rows)


@pytest.mark.parametrize("write_path", ["insert", "variant", "image_patch", "media_patch"])
def test_scene_candidate_stays_internal_to_preparation_at_calendar_writes(
        monkeypatch, writer_calls, calendar_without_stage_belts, write_path):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "true")
    data = _png_bytes()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    row = _same_object_row(id="row-1", gym_id="old-key", variant_status="active",
                           account="instagram", format="feed", caption="Ready today",
                           post_date="2026-10-06")
    if write_path == "media_patch":
        row["image_url"] = ""
    elif write_path == "image_patch":
        row["format"] = "story"
    http = CalendarHTTP(row, _md5(data))
    calendar = _store(http)
    prepare = prep.prepare
    prepared_rows = []

    def capture_prepared(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_rows.append(prepared)
        return prepared

    monkeypatch.setattr(prep, "prepare", capture_prepared)
    if write_path == "insert":
        result = calendar.insert_rows("old-key", [row])
    elif write_path == "variant":
        result = calendar.create_variant_candidate(
            "old-key", row, URL, source_media_url=URL)
    elif write_path == "image_patch":
        result = calendar.patch_image_url("old-key", row["id"], URL)
    else:
        result = calendar.patch_media("old-key", row["id"], URL)
    assert result
    writes = [call for call in http.calls if call[1] == "content_calendar"]
    assert len(writes) == 1
    payloads = writes[0][2] if writes[0][0] == "post" else [writes[0][2]]
    assert all("scene_candidate" not in payload for payload in payloads)
    assert all(payload["visual_group_key"] == "vg_same" for payload in payloads)
    candidate = prepared_rows[0]["scene_candidate"]
    assert candidate["tenant_id"] == TENANT
    assert candidate["group_key"] == "vg_same"
    assert candidate["counts_as_use"] is False
    assert FP_RE.fullmatch(candidate["objects"][0]["scene_fingerprint"])
    assert FP_RE.fullmatch(writer_calls[0]["evidence"]["scene_fingerprint"])


def test_prepared_candidate_is_not_inserted_when_writer_prep_disabled(
        monkeypatch, calendar_without_stage_belts):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    candidate = {"kind": "visual_scene_candidate", "stage": "candidate"}
    row = _same_object_row(gym_id="old-key", scene_candidate=candidate)
    http = CalendarHTTP(row, None)
    result = _store(http).insert_rows("old-key", [row])
    assert result
    assert row["scene_candidate"] is candidate
    assert "scene_candidate" not in result[0]
