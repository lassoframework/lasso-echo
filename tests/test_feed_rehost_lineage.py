"""Offline feed autofit receipts, scoped persistence, and publish hold checks."""
import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from agent import calendar_autopublish as cap
from agent import config, media_host, portal_calendar_store as pcs
from agent import visual_owner_receipts, visual_writer_prepare as prep


TENANT = "11111111-1111-1111-1111-111111111111"
RAW = "https://media.example/raw.jpg"
INPUT = "https://media.example/rendition.jpg?version=2"
OUTPUT = "https://media.example/fitted.jpg"
ACCOUNT = SimpleNamespace(key="gym_ig")


def image_bytes(w, h):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (40, 60, 80)).save(out, "JPEG")
    return out.getvalue()


def row():
    return {"id": "feed-1", "gym_id": "gym", "format": "feed", "status": "approved",
            "caption": 'caption, "quoted"', "account": "instagram", "post_date": "2026-10-03",
            "image_url": INPUT, "source_media_url": RAW, "source_media_asset_id": "asset-a",
            "drive_file_id": "asset-a", "visual_group_key": "vg_scene", "r2_key": "rendition.jpg",
            "byte_hash": "derived:md5:" + hashlib.md5(image_bytes(600, 1080)).hexdigest(),
            "variant_status": "active", "published_at": None, "late_post_id": None,
            "media_not_ready_reason": None, "publish_claim_token": None,
            "publish_reservation_day": None, "slot_index": 1}


class Response:
    status_code = 200

    def __init__(self, result):
        self.result = result

    def json(self):
        return self.result


class ConditionalHTTP:
    """Apply exact PostgREST predicates to the independently stored row."""

    def __init__(self, saved, evidence):
        self.saved = dict(saved)
        self.evidence = evidence
        self.calls = []

    def get(self, url, *, params, **kwargs):
        self.calls.append(("get", url, params))
        if url.endswith("tenant_alias"):
            return Response([{"alias_key": "gym", "tenant_id": TENANT}])
        if url.endswith("visual_group_alias"):
            assert params["alias_value"] == "eq." + INPUT
            return Response([{"group_key": "vg_scene"}])
        if url.endswith("media_asset"):
            return Response([{"id": "asset-a", "gym_id": "gym",
                              "content_hash": hashlib.md5(image_bytes(600, 1080)).hexdigest()}])
        raise AssertionError("unexpected read: " + url)

    def post(self, url, *, json, **kwargs):
        self.calls.append(("post", url, json))
        assert url.endswith("visual_global_prepare_source_rendition")
        return Response({"group_key": "vg_scene", "usage_claimed": False,
                         "source_fingerprint": self.evidence["source_fingerprint"],
                         "delivered_fingerprint": self.evidence["delivered_fingerprint"]})

    def patch(self, url, *, params, json, **kwargs):
        self.calls.append(("patch", params, json))
        for key, condition in params.items():
            if condition == "is.null":
                matches = self.saved.get(key) is None
            elif condition.startswith('eq."') and condition.endswith('"'):
                value = condition[4:-1].replace('\\"', '"').replace('\\\\', '\\')
                matches = str(self.saved.get(key)) == value
            else:
                matches = condition == f"eq.{self.saved.get(key)}"
            if not matches:
                return Response([])
        self.saved.update(json)
        return Response([dict(self.saved)])


def store(http):
    result = pcs.SupabaseCalendarStore.__new__(pcs.SupabaseCalendarStore)
    result._client = lambda: http
    result._rest = lambda table: table
    result._headers = lambda extra=None: extra or {}
    return result


@pytest.fixture
def rehost(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    objects = {INPUT: image_bytes(600, 1080), RAW: image_bytes(600, 1080)}
    evidence, observations = {}, {}
    monkeypatch.setattr(prep, "_bytes_for_url", objects.get)

    def host(path, key):
        assert key == ACCOUNT.key
        with open(path, "rb") as rendered:
            objects[OUTPUT] = rendered.read()
        return OUTPUT

    def receipts(**kwargs):
        observations.update(kwargs)
        evidence.update(kwargs["render_evidence"])
        return {"source_read_receipt": "22222222-2222-2222-2222-222222222222",
                "delivered_read_receipt": "33333333-3333-3333-3333-333333333333",
                "render_receipt": "44444444-4444-4444-4444-444444444444"}

    monkeypatch.setattr(media_host, "host_media", host)
    monkeypatch.setattr(visual_owner_receipts, "default_writer", lambda: receipts)
    http = ConditionalHTTP(row(), evidence)
    return objects, evidence, observations, http, store(http)


def test_feed_rendition_rehost_attests_real_edge_and_preserves_raw_source(rehost):
    objects, evidence, observations, http, calendar = rehost
    result = cap._normalize_feed_image(row(), ACCOUNT, calendar)
    assert result == http.saved
    assert result["image_url"] == OUTPUT
    assert result["source_media_url"] == RAW
    assert result["source_media_asset_id"] == result["drive_file_id"] == "asset-a"
    assert result["visual_group_key"] == "vg_scene"
    assert result["byte_hash"] == "derived:md5:" + hashlib.md5(objects[OUTPUT]).hexdigest()
    assert result["r2_key"] is None
    assert evidence == {"source_exact_url": INPUT, "delivered_exact_url": OUTPUT,
                        "source_fingerprint": "md5:" + hashlib.md5(objects[INPUT]).hexdigest(),
                        "delivered_fingerprint": "md5:" + hashlib.md5(objects[OUTPUT]).hexdigest(),
                        "source_byte_length": len(objects[INPUT]),
                        "delivered_byte_length": len(objects[OUTPUT]),
                        "operation": "rehost", "evidence_ref": evidence["evidence_ref"],
                        "observed_by": "calendar_autopublish.feed_autofit",
                        "rendered_by": "calendar_autopublish.feed_autofit"}
    assert evidence["evidence_ref"].startswith("feed_autofit:")
    assert observations["source_bytes"] == objects[INPUT]
    assert observations["delivered_bytes"] == objects[OUTPUT]
    assert observations["asset_id"] is None  # asset-a belongs to raw A, not rendition B
    assert [call[0] for call in http.calls][-2:] == ["post", "patch"]
    _, params, payload = http.calls[-1]
    assert params["status"] == 'eq."approved"'
    assert params["source_media_url"] == f'eq."{RAW}"'
    assert params["image_url"] == f'eq."{INPUT}"'
    assert params["post_date"] == 'eq."2026-10-03"'
    assert params["published_at"] == params["late_post_id"] == "is.null"
    assert payload["source_media_url"] == RAW


def test_feed_first_rehost_keeps_raw_input_and_asset(rehost):
    _, _, observations, http, calendar = rehost
    observed = row()
    observed["source_media_url"] = None
    http.saved = dict(observed)
    result = cap._normalize_feed_image(observed, ACCOUNT, calendar)
    assert result["source_media_url"] == INPUT
    assert result["source_media_asset_id"] == "asset-a"
    assert observations["asset_id"] == "asset-a"


@pytest.mark.parametrize("field,value", [
    ("caption", "new caption"), ("image_url", RAW), ("source_media_url", INPUT),
    ("status", "publishing"), ("published_at", "2026-10-03T10:00:00Z"),
    ("late_post_id", "external-id"), ("post_date", "2026-10-04"),
    ("account", "facebook"), ("visual_group_key", "vg_other"),
    ("byte_hash", "derived:md5:" + "f" * 32), ("source_media_asset_id", "other"),
    ("variant_status", "archived"), ("slot_index", 2), ("publish_claim_token", "claim"),
    ("media_not_ready_reason", "held"),
])
def test_feed_rehost_holds_on_competing_calendar_update(rehost, field, value):
    _, _, _, http, calendar = rehost
    http.saved[field] = value
    assert cap._normalize_feed_image(row(), ACCOUNT, calendar) is None
    assert http.saved["image_url"] != OUTPUT


@pytest.mark.parametrize("field,value", [
    ("image_url", INPUT), ("status", "publishing"), ("caption", "new caption"),
    ("source_media_url", INPUT), ("byte_hash", "derived:md5:" + "f" * 32),
    ("visual_group_key", "vg_other"), ("post_date", "2026-10-04"),
    ("late_post_id", "provider-id"), ("media_not_ready_reason", "held"),
])
def test_feed_rehost_rejects_conflicting_returned_row(rehost, field, value):
    _, _, _, _, calendar = rehost
    original = calendar.patch_image_url

    def conflicting(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, field: value}

    calendar.patch_image_url = conflicting
    assert cap._normalize_feed_image(row(), ACCOUNT, calendar) is None


@pytest.mark.parametrize("failure", ["no_patch", "no_result", "missing_receipts", "source_changed", "output_changed"])
def test_feed_rehost_holds_without_persistence_or_exact_evidence(rehost, monkeypatch, failure):
    objects, _, _, http, calendar = rehost
    if failure == "no_patch":
        calendar.patch_image_url = None
    elif failure == "no_result":
        calendar.patch_image_url = lambda *a, **k: None
    elif failure == "missing_receipts":
        monkeypatch.setattr(visual_owner_receipts, "default_writer", lambda: None)
    elif failure == "source_changed":
        reads = []

        def reader(url):
            reads.append(url)
            return objects[url] if len(reads) == 1 else b"changed input"

        monkeypatch.setattr(prep, "_bytes_for_url", reader)
    else:
        def mismatched_host(path, key):
            objects[OUTPUT] = b"changed output"
            return OUTPUT

        monkeypatch.setattr(media_host, "host_media", mismatched_host)
    assert cap._normalize_feed_image(row(), ACCOUNT, calendar) is None
    assert http.saved["image_url"] == INPUT
    assert not any(call[0] == "patch" for call in http.calls)


def test_feed_patch_refuses_fabricated_raw_to_output_edge(rehost):
    _, _, _, http, calendar = rehost
    with pytest.raises(prep.VisualPreparationError, match="scoped feed replacement"):
        calendar.patch_image_url("gym", "feed-1", OUTPUT, expected_row=row(),
                                 render_evidence={"source_exact_url": RAW,
                                                  "delivered_exact_url": OUTPUT,
                                                  "operation": "rehost"})
    assert http.calls == []


@pytest.mark.parametrize("bad_response", ["empty", "multiple", "not_a_list", "foreign", "source", "hash", "group", "held",
                                        "missing_publish_state", "missing_hold_state"])
def test_real_feed_store_rejects_unverified_patch_representation(rehost, bad_response):
    _, _, _, http, calendar = rehost
    original = http.patch
    results = []
    real_patch = calendar.patch_image_url

    def record(*args, **kwargs):
        result = real_patch(*args, **kwargs)
        results.append(result)
        return result

    calendar.patch_image_url = record

    def respond(*args, **kwargs):
        rows = original(*args, **kwargs).json()
        if bad_response == "empty":
            return Response([])
        if bad_response == "multiple":
            return Response(rows * 2)
        if bad_response == "not_a_list":
            return Response({"row": rows[0]})
        if bad_response in ("missing_publish_state", "missing_hold_state"):
            key = "published_at" if bad_response == "missing_publish_state" else "media_not_ready_reason"
            del rows[0][key]
            return Response(rows)
        wrong = {"foreign": ("gym_id", "other"), "source": ("source_media_url", INPUT),
                 "hash": ("byte_hash", "derived:md5:" + "f" * 32),
                 "group": ("visual_group_key", "vg_other"),
                 "held": ("media_not_ready_reason", "held")}
        key, value = wrong[bad_response]
        return Response([{**rows[0], key: value}])

    http.patch = respond
    assert cap._normalize_feed_image(row(), ACCOUNT, calendar) is None
    assert results == [None]


def test_story_patch_verifies_effective_payload_when_preparation_clears_asset_ids(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    observed = {**row(), "format": "story", "image_url": INPUT}
    captured = {}

    def prepared(_store, account_key, candidate, **kwargs):
        captured.update(candidate)
        return {**candidate, "visual_group_key": "vg_scene", "byte_hash": "derived:md5:" + "c" * 32}

    monkeypatch.setattr(prep, "prepare", prepared)
    http = ConditionalHTTP(observed, {})
    calendar = store(http)
    result = calendar.patch_image_url("gym", observed["id"], OUTPUT, expected_row=observed,
                                      render_evidence={"source_exact_url": RAW,
                                                       "delivered_exact_url": OUTPUT})
    assert observed["source_media_asset_id"] == observed["drive_file_id"] == "asset-a"
    assert result == http.saved
    assert result["source_media_asset_id"] is result["drive_file_id"] is None
    assert result["source_media_url"] == RAW
    assert result["status"] == "approved"
    assert captured["source_media_asset_id"] is captured["drive_file_id"] is None
