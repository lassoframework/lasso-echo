"""Scene-fingerprint evidence at the visual writer-prepare boundary.

FINAL CONTRACT (Astra rejection, 2026-10-03 — see
docs/VISUAL_SCENE_GUARD_DRAFT.md): the prep-time scene writer architecture was
rejected, so scene evidence here is ADVISORY METADATA ONLY. The
`scene_fingerprint` evidence field rides alongside the md5 identity on
receipts the writer builds from already-verified exact bytes; it never gates,
never raises, and there is no scene RPC, no p_scene_* payload and no
post_date requirement in ANY flag state. Undecodable bytes simply record null
evidence; the md5 identity stays the only authority.
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


@pytest.mark.parametrize("scene_flag", [None, "true", "maybe"])
def test_scene_evidence_is_advisory_in_every_flag_state(
        monkeypatch, writer_calls, scene_flag):
    """Real image bytes: the scene_fingerprint evidence field is present and
    namespaced, whatever AGENT_VISUAL_SCENE_GUARD says — it never gates."""
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    if scene_flag is None:
        monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    else:
        monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", scene_flag)
    data = _png_bytes()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    http = HTTP(fingerprint=_md5(data))
    # No post_date on the row: scene evidence must never require one.
    row = prep.prepare(_store(http), "old-key", _same_object_row())
    assert row["visual_group_key"] == "vg_same"
    assert writer_calls, "same-object owner writer must have produced a receipt"
    fp = writer_calls[0]["evidence"]["scene_fingerprint"]
    assert FP_RE.fullmatch(fp)
    _no_scene_surface(http)


@pytest.mark.parametrize("scene_flag", [None, "true", "maybe"])
def test_undecodable_bytes_record_null_evidence_and_never_raise(
        monkeypatch, writer_calls, scene_flag):
    """Undecodable bytes: scene_fingerprint is None and preparation succeeds —
    even with the scene flag armed or ambiguous. md5 stays the only authority."""
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    if scene_flag is None:
        monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    else:
        monkeypatch.setenv("AGENT_VISUAL_SCENE_GUARD", scene_flag)
    data = b"exact bytes, but not an image"
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: data)
    http = HTTP(fingerprint=_md5(data))
    row = prep.prepare(_store(http), "old-key", _same_object_row())
    assert row["visual_group_key"] == "vg_same"
    evidence = writer_calls[0]["evidence"]
    assert "scene_fingerprint" in evidence
    assert evidence["scene_fingerprint"] is None
    _no_scene_surface(http)


def test_writer_prep_flag_off_is_legacy_passthrough(monkeypatch, writer_calls):
    """Both flags OFF: the row passes through untouched, no RPCs, no evidence."""
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.delenv("AGENT_VISUAL_SCENE_GUARD", raising=False)
    http = HTTP()
    row = _same_object_row()
    assert prep.prepare(_store(http), "old-key", row) is row
    assert http.calls == []
    assert writer_calls == []
