"""
The media upload GALLERY page + the dotted-token resolution fix.

Two things under test, both born from Dale Suslick's real 'not found':

BUG 1 (token resolution): a signed onboard token is b64url(key).b64url(sig) and
so contains a DOT. A texted link routinely arrives with that dot percent-encoded
(%2E), a trailing slash, or trailing whitespace, and the old raw-path regex missed
every one of those and 404'd a valid link. token_from_path now URL-decodes and
trims before matching, so /u/<token> and /intake/<token> resolve the SAME
onboard-minted token that /portal/<token> does, while path traversal still 404s.

BUG 2 (gallery + per-item captions): the page lets a gym pick MULTIPLE photos and
videos, gives each its own optional one-line caption, and persists a per-file
caption map in the R2 sidecar (keyed by stored basename) alongside the legacy
batch note. A plain single note with no captions still works unchanged.

Real HTTP against an ephemeral port for the routing tests; handle_upload is driven
directly (fake R2) for the persistence tests. Fully OFFLINE.
"""

import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, intake_tokens, intake_web  # noqa: E402
from agent.intake_web import build_server, handle_upload, token_from_path  # noqa: E402

SECRET = "gallery-tests-shared-signing-secret"
# An onboard-style token for a real gym slug (this is exactly what onboard.run /
# POST /portal/onboard mint and what the portal stores encrypted, then decrypts
# into the /u/ link). The DOT is intrinsic to the format.
TOKEN = intake_tokens.mint("dalesuslick", secret=SECRET.encode())


class FakeR2:
    def __init__(self):
        self.objects = {}

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get_bytes(self, key):
        return self.objects.get(key)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, SECRET)
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setattr(intake_web, "_hits", {})
    monkeypatch.setattr(intake_web, "_token_hits", {})
    yield


@pytest.fixture
def server():
    srv = build_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _get(srv, path):
    port = srv.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post_multipart(srv, path, parts):
    """parts: ordered list of ('media', filename, ctype, bytes) or ('field', name, value)."""
    port = srv.server_address[1]
    boundary = "----echo" + uuid.uuid4().hex
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode() if isinstance(s, str) else s)

    for p in parts:
        w(f"--{boundary}\r\n")
        if p[0] == "media":
            _, fn, ctype, data = p
            w(f'Content-Disposition: form-data; name="media"; filename="{fn}"\r\n')
            w(f"Content-Type: {ctype}\r\n\r\n")
            w(data)
            w("\r\n")
        else:
            _, name, value = p
            w(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
            w(value)
            w("\r\n")
    w(f"--{boundary}--\r\n")
    body = buf.getvalue()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


JPG = b"\xff\xd8\xff fake jpg bytes"
MP4 = b"\x00\x00\x00\x18ftypmp42 fake mp4"


# ============================ BUG 1: token resolution ============================

def test_dotted_onboard_token_resolves_on_u(server):
    """The core regression: a dotted onboard token 200s on /u/ (was 404)."""
    assert "." in TOKEN
    status, body = _get(server, f"/u/{TOKEN}")
    assert status == 200
    assert "content" in body.lower()


def test_dotted_onboard_token_resolves_on_intake(server):
    status, _ = _get(server, f"/intake/{TOKEN}")
    assert status == 200


def test_percent_encoded_dot_resolves_on_u(server):
    """The exact Dale failure mode: the dot arrives as %2E from the messaging app."""
    enc = TOKEN.replace(".", "%2E")
    assert _get(server, f"/u/{enc}")[0] == 200
    assert _get(server, "/u/" + TOKEN.replace(".", "%2e"))[0] == 200


def test_trailing_slash_and_whitespace_resolve_on_u(server):
    assert _get(server, f"/u/{TOKEN}/")[0] == 200
    assert _get(server, f"/u/{TOKEN}%20")[0] == 200
    assert _get(server, f"/u/{TOKEN}%0A")[0] == 200


def test_query_string_ignored(server):
    assert _get(server, f"/u/{TOKEN}?utm=sms")[0] == 200


def test_bad_token_still_404(server):
    assert _get(server, "/u/ZGFsZXN1c2xpY2s.deadbeefdeadbeefdeadbeef")[0] == 404
    assert _get(server, "/u/wrongtoken0000")[0] == 404


def test_feature_off_still_404(server, monkeypatch):
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    assert _get(server, f"/u/{TOKEN}")[0] == 404
    assert _get(server, f"/u/{TOKEN.replace('.', '%2E')}")[0] == 404


def test_token_from_path_pure():
    canonical = token_from_path(f"/u/{TOKEN}", "u")
    assert canonical == TOKEN
    assert token_from_path("/u/" + TOKEN.replace(".", "%2E"), "u") == TOKEN
    assert token_from_path(f"/u/{TOKEN}/", "u") == TOKEN
    assert token_from_path(f"/u/{TOKEN}?x=1", "u") == TOKEN
    assert token_from_path(f"/intake/{TOKEN}", "intake") == TOKEN
    # wrong prefix, short, and empty all None
    assert token_from_path(f"/u/{TOKEN}", "intake") is None
    assert token_from_path("/u/short", "u") is None
    assert token_from_path("/u/", "u") is None


def test_token_from_path_no_traversal():
    """Decoding must never open path traversal: a decoded slash is rejected."""
    assert token_from_path("/u/../../etc/passwd", "u") is None
    assert token_from_path("/u/%2e%2e%2fadmin", "u") is None
    assert token_from_path("/u/aaaaaaaa%2Fbbbbbbbb", "u") is None


# ======================= BUG 2: gallery + per-item captions ======================

def test_gallery_page_is_multi_select_with_captions(server):
    """The page supports multiple files and per-item caption inputs; copy is clean."""
    _, body = _get(server, f"/u/{TOKEN}")
    assert "multiple" in body                       # multi select
    assert "caption" in body.lower()                # per-item caption field name
    assert "gallery" in body.lower()                # the thumbnail gallery
    assert "Remove" in body                         # per-item remove
    # client facing copy law
    assert "vendor" not in body.lower()
    # no dash or hyphen characters in the DISPLAYED prose (ignore code/JS/CSS).
    # Check the human sentences we authored.
    for phrase in ["Send us your", "We take it from there",
                   "What is happening in this one"]:
        assert phrase in body
        assert "-" not in phrase


def test_multi_file_upload_persists_per_file_caption_map():
    r2 = FakeR2()
    files = [("crowd.jpg", "image/jpeg", JPG),
             ("lift.mp4", "video/mp4", MP4),
             ("board.png", "image/png", JPG)]
    caps = ["Saturday open house", "", "new PR board"]
    status, body = handle_upload(TOKEN, files, note="batch note", captions=caps, r2=r2)
    assert status == 200 and body["stored"] == 3

    sidecars = [k for k in r2.objects if k.endswith("_upload.json")]
    assert len(sidecars) == 1
    sc = json.loads(r2.objects[sidecars[0]])
    assert sc["client"] == "dalesuslick"
    assert sc["note"] == "batch note"
    # per-file caption map, keyed by STORED basename, order-aligned to files
    caps_map = sc["captions"]
    names = sc["filenames"]
    assert caps_map[names[0]] == "Saturday open house"
    assert names[1] not in caps_map          # blank caption omitted, never required
    assert caps_map[names[2]] == "new PR board"
    # raw token never persisted; fingerprint only
    assert TOKEN not in json.dumps(sc)
    assert len(sc["token_sha256"]) == 64


def test_captions_backward_compatible_note_only():
    """A plain single note with no captions still works; captions map is empty."""
    r2 = FakeR2()
    status, body = handle_upload(TOKEN, [("a.jpg", "image/jpeg", JPG)],
                                 note="just one line", r2=r2)
    assert status == 200
    sc = json.loads(r2.objects[[k for k in r2.objects if k.endswith("_upload.json")][0]])
    assert sc["note"] == "just one line"
    assert sc["captions"] == {}


def test_multipart_post_pairs_captions_to_files(server, monkeypatch):
    """End to end over HTTP: the gallery submission pairs each caption to its file."""
    r2 = FakeR2()
    monkeypatch.setattr(intake_web, "_default_r2", lambda: r2)
    parts = [
        ("media", "one.jpg", "image/jpeg", JPG),
        ("field", "caption", "front desk smiling"),
        ("media", "two.mp4", "video/mp4", MP4),
        ("field", "caption", ""),                     # optional: left blank
        ("media", "three.jpg", "image/jpeg", JPG),
        ("field", "caption", "kettlebell class"),
        ("field", "note", "whole batch from Saturday"),
    ]
    status, _ = _post_multipart(server, f"/u/{TOKEN}", parts)
    assert status == 200
    sc = json.loads(r2.objects[[k for k in r2.objects if k.endswith("_upload.json")][0]])
    names = sc["filenames"]
    assert len(names) == 3
    assert sc["note"] == "whole batch from Saturday"
    assert sc["captions"][names[0]] == "front desk smiling"
    assert names[1] not in sc["captions"]
    assert sc["captions"][names[2]] == "kettlebell class"


# --------------------------- guardrails still enforced ---------------------------

def test_bad_type_rejected():
    r2 = FakeR2()
    status, body = handle_upload(
        TOKEN, [("evil.exe", "application/x-msdownload", b"MZ")],
        captions=["x"], r2=r2)
    assert status == 400 and "not allowed" in body["error"]


def test_no_files_rejected():
    assert handle_upload(TOKEN, [], captions=[], r2=FakeR2())[0] == 400


def test_oversize_file_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_MAX_FILE_MB", "1")
    big = ("big.jpg", "image/jpeg", b"x" * (2 * 1024 * 1024))
    status, body = handle_upload(TOKEN, [big], captions=["huge"], r2=FakeR2())
    assert status == 400 and "too large" in body["error"]


def test_upload_feature_off_404():
    # handle_upload itself gates on the flag independent of the route.
    orig = os.environ.pop("AGENT_INTAKE_ENABLED", None)
    try:
        assert handle_upload(TOKEN, [("a.jpg", "image/jpeg", JPG)],
                             captions=["x"], r2=FakeR2())[0] == 404
    finally:
        if orig is not None:
            os.environ["AGENT_INTAKE_ENABLED"] = orig


def test_unknown_token_upload_404():
    assert handle_upload("totally-unknown-token", [("a.jpg", "image/jpeg", JPG)],
                         captions=["x"], r2=FakeR2())[0] == 404
