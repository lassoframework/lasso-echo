"""DRAFT durable staging binds owner-prepared displayed bytes and retry identity."""
import uuid
import pytest
from agent import visual_owner_receipts as owner
from agent import visual_writer_prepare as prep

TENANT = "11111111-1111-4111-8111-111111111111"
GROUP = "vg_scene"
RAW, DISPLAY, POSTER = ["https://media.example/" + name + ".jpg" for name in ("raw", "display", "poster")]
DATA = {RAW: b"source", DISPLAY: b"display", POSTER: b"poster"}
PHASH = {data: f"{index:016x}" for index, data in enumerate(DATA.values(), 1)}

class Response:
    def __init__(self, value, status=200):
        self.value, self.status_code = value, status
    def json(self):
        return self.value

class Store:
    def __init__(self):
        self.calls, self.receipts, self.attested, self.candidates = [], {}, set(), {}
        self.skip_attestation, self.registration_status, self.registration_result = False, 200, None
    def _client(self):
        return self
    def _rest(self, path):
        return path
    def _headers(self, value=None):
        return value or {}
    def post(self, url, *, headers, json, timeout):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, json))
        if name == "visual_global_prepare_source_rendition":
            source = self.receipts[json["p_source_read_receipt"]]
            delivered = self.receipts[json["p_delivered_read_receipt"]]
            if not self.skip_attestation:
                self.attested.update((source, delivered))
            return Response({"group_key": GROUP, "source_fingerprint": source[1],
                             "delivered_fingerprint": delivered[1], "usage_claimed": False})
        assert name == "visual_scene_register_candidate"
        if self.registration_status != 200:
            return Response({}, self.registration_status)
        if (json["p_exact_url"], json["p_fingerprint"]) not in self.attested:
            return Response({"message": "not backed by owner-attested exact bytes"}, 400)
        if self.registration_result is not None:
            return Response(self.registration_result)
        key = tuple(json[k] for k in ("p_tenant", "p_group_key", "p_object_role", "p_exact_url"))
        if key in self.candidates:
            candidate_id, previous = self.candidates[key]
            assert previous == json, "retry evidence must remain identical"
        else:
            candidate_id = str(uuid.uuid4())
            self.candidates[key] = candidate_id, json.copy()
        return Response(candidate_id)


def render(source, delivered):
    return {"source_exact_url": source, "delivered_exact_url": delivered,
            "source_fingerprint": prep._md5(DATA[source]),
            "delivered_fingerprint": prep._md5(DATA[delivered]),
            "source_byte_length": len(DATA[source]), "delivered_byte_length": len(DATA[delivered]),
            "operation": "render"}

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", "true")
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    monkeypatch.setattr(prep, "_tenant", lambda *args: TENANT)
    monkeypatch.setattr(prep, "_known_group", lambda *args, **kwargs: GROUP)
    monkeypatch.setattr(prep, "_own_media_url", lambda url: url in DATA)
    monkeypatch.setattr(prep, "_bytes_for_url", DATA.__getitem__)
    monkeypatch.setattr(prep, "_scene_fingerprint", lambda data: "scene:phash64:" + PHASH[data])
    store = Store()
    def receipt(url, data):
        receipt_id = str(uuid.uuid4())
        store.receipts[receipt_id] = (url, prep._md5(data))
        return receipt_id
    def same_writer(**args):
        assert args["tenant"] == TENANT and args["group_key"] == GROUP
        return {"read_receipt": receipt(args["evidence"]["exact_url"], args["exact_bytes"]),
                "render_receipt": None}
    def rendition_writer(**args):
        assert args["tenant"] == TENANT and args["group_key"] == GROUP
        evidence = args["render_evidence"]
        return {"source_read_receipt": receipt(evidence["source_exact_url"], args["source_bytes"]),
                "delivered_read_receipt": receipt(evidence["delivered_exact_url"], args["delivered_bytes"]),
                "render_receipt": str(uuid.uuid4())}
    monkeypatch.setattr(owner, "default_same_object_writer", lambda: same_writer)
    monkeypatch.setattr(owner, "default_writer", lambda: rendition_writer)
    return store

def registrations(store):
    return [args for name, args in store.calls if name == "visual_scene_register_candidate"]


def test_same_object_registers_after_owner_preparation_and_retries(setup):
    source = {"image_url": RAW, "source_media_url": RAW}
    first, second = [prep.prepare(setup, "gym", source) for _ in range(2)]
    assert first["scene_candidate"]["candidate_id"] == second["scene_candidate"]["candidate_id"]
    assert registrations(setup)[0] == {
        "p_tenant": TENANT, "p_group_key": GROUP, "p_phash": PHASH[DATA[RAW]],
        "p_exact_url": RAW, "p_fingerprint": prep._md5(DATA[RAW]),
        "p_evidence": {"source": "owner_prepared_exact_object_bytes",
                       "verified_bytes": prep._md5(DATA[RAW]), "byte_length": len(DATA[RAW])},
        "p_actor": "visual_writer_prepare", "p_object_role": "display"}
    assert [name for name, _ in setup.calls[:2]] == [
        "visual_global_prepare_source_rendition", "visual_scene_register_candidate"]
    assert first["scene_candidate"]["usage_claimed"] is False
    assert "scene_candidate" not in source


def test_rendition_stages_display_never_raw_source(setup):
    row = prep.prepare(setup, "gym", {"image_url": DISPLAY, "source_media_url": RAW},
                       render_evidence=render(RAW, DISPLAY))
    args, = registrations(setup)
    assert (args["p_exact_url"], args["p_phash"], args["p_object_role"]) == (
        DISPLAY, PHASH[DATA[DISPLAY]], "display")
    assert row["scene_candidate"]["candidate_id"]


def test_video_stages_only_owner_attested_poster(setup, monkeypatch):
    monkeypatch.setattr(prep, "_scene_fingerprint", lambda data: (
        "scene:phash64:" + PHASH[data] if data == DATA[POSTER] else None))
    row = prep.prepare(setup, "gym", {"image_url": DISPLAY, "source_media_url": RAW,
                                      "thumbnail_url": POSTER},
                       render_evidence=render(RAW, DISPLAY),
                       poster_render_evidence=render(DISPLAY, POSTER))
    assert [name for name, _ in setup.calls] == ["visual_global_prepare_source_rendition",
        "visual_global_prepare_source_rendition", "visual_scene_register_candidate"]
    args, = registrations(setup)
    assert (args["p_object_role"], args["p_exact_url"], args["p_phash"]) == (
        "poster", POSTER, PHASH[DATA[POSTER]])
    assert row["scene_candidate"]["object_role"] == "poster"

@pytest.mark.parametrize("value", [None, "scene:phash64:gggggggggggggggg", "scene:phash64:123"])
def test_missing_malformed_phash_fails_closed(setup, monkeypatch, value):
    monkeypatch.setattr(prep, "_scene_fingerprint", lambda data: value)
    with pytest.raises(prep.VisualPreparationError, match="pHash"):
        prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})
    assert registrations(setup) == []


def test_missing_owner_attestation_rejected_by_rpc(setup):
    setup.skip_attestation = True
    with pytest.raises(prep.VisualPreparationError, match="visual_scene_register_candidate failed"):
        prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})
    assert len(registrations(setup)) == 1

@pytest.mark.parametrize("bad_result", [None, {}, "not-a-uuid"])
def test_failed_registration_fails_closed(setup, bad_result):
    if bad_result is None:
        setup.registration_status = 503
    else:
        setup.registration_result = bad_result
    with pytest.raises(prep.VisualPreparationError, match="registration|RPC"):
        prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})

@pytest.mark.parametrize("candidate", ["off", "true"])
def test_guard_off_preserves_advisory_behavior_without_rpc(setup, monkeypatch, candidate):
    monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", "off")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", candidate)
    monkeypatch.setattr(prep, "_scene_fingerprint", lambda data: None)
    row = prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})
    assert registrations(setup) == []
    assert ("scene_candidate" in row) == (candidate == "true")


def test_guard_on_requires_owner_writer(setup, monkeypatch):
    monkeypatch.setattr(owner, "default_same_object_writer", lambda: None)
    with pytest.raises(prep.VisualPreparationError, match="owner receipt producer"):
        prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})
    assert setup.calls == []

@pytest.mark.parametrize("guard", ["true", "maybe"])
def test_armed_guard_cannot_bypass_preparation(setup, monkeypatch, guard):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "off")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", guard)
    with pytest.raises(prep.VisualPreparationError, match="scene guard"):
        prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})
    assert setup.calls == []


def test_distinct_poster_without_owner_render_lineage_fails_before_rpc(setup):
    with pytest.raises(prep.VisualPreparationError, match="poster scene lineage"):
        prep.prepare(setup, "gym", {"image_url": DISPLAY, "source_media_url": RAW,
                                    "thumbnail_url": POSTER},
                     render_evidence=render(RAW, DISPLAY))
    assert setup.calls == []


def test_same_thumbnail_is_display_role(setup):
    prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW,
                                "thumbnail_url": RAW})
    args, = registrations(setup)
    assert args["p_object_role"] == "display"
    assert args["p_exact_url"] == RAW


def test_candidate_transport_failure_is_preparation_error(setup, monkeypatch):
    rpc = prep._rpc
    def fail_candidate(store, name, arguments):
        if name == "visual_scene_register_candidate":
            raise OSError("network unavailable")
        return rpc(store, name, arguments)
    monkeypatch.setattr(prep, "_rpc", fail_candidate)
    with pytest.raises(prep.VisualPreparationError, match="candidate registration failed"):
        prep.prepare(setup, "gym", {"image_url": RAW, "source_media_url": RAW})
