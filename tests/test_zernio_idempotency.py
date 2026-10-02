"""
Zernio strong Idempotency-Key client support (changelog 2026-09-25).

Fully OFFLINE: a fake HTTP layer records the exact request, so the tests assert
header exactness — the caller-provided UUID goes out as the literal
`Idempotency-Key` header value, nothing normalised, nothing invented.

Asserts:
  - omitting idempotency_key sends NO Idempotency-Key header (default behavior
    unchanged; only x-request-id, still a fresh UUID per call);
  - a caller-provided key is sent EXACTLY as given, alongside x-request-id;
  - the SAME caller key is passed through unchanged on repeated caller
    invocations (key stability is the caller's job; the client must not
    regenerate or mutate it);
  - a non-UUID (or >255-char) key raises ValueError BEFORE any network POST;
  - the returned post JSON shape is unchanged (the client returns r.json()
    untouched — no 409 swallowing, no retry).

This tests CLIENT SUPPORT only: an optional header does not by itself resolve
ambiguous sends — durable attempt IDs + provider readback are wired elsewhere.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio  # noqa: E402

KEY = "3f6f8f8a-2a1b-4c5d-9e0f-1234567890ab"


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """Records the last POST so tests can inspect exact headers + payload."""

    def __init__(self):
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": dict(headers)})
        return FakeResponse({"_id": "zpost_1", "status": "published"})


@pytest.fixture
def client():
    http = FakeHttp()
    c = zernio.ZernioClient(api_key="k", base="https://api.zernio.test", http=http)
    return c, http


def _assert_uuid(s):
    uuid.UUID(str(s))


def test_default_sends_no_idempotency_key(client):
    c, http = client
    out = c.create_post("acct_1", "hi")
    hdrs = http.posts[0]["headers"]
    assert "Idempotency-Key" not in hdrs
    _assert_uuid(hdrs["x-request-id"])  # existing behavior preserved


def test_default_raw_sends_no_idempotency_key(client):
    c, http = client
    c.create_post_raw({"content": "hi"})
    assert "Idempotency-Key" not in http.posts[0]["headers"]


def test_key_sent_exactly_on_create_post(client):
    c, http = client
    c.create_post("acct_1", "hi", idempotency_key=KEY)
    hdrs = http.posts[0]["headers"]
    assert hdrs["Idempotency-Key"] == KEY  # exact, no normalisation
    assert hdrs["x-request-id"]  # existing fingerprint header still present
    _assert_uuid(hdrs["x-request-id"])


def test_key_sent_exactly_on_create_post_raw(client):
    c, http = client
    c.create_post_raw({"content": "hi"}, idempotency_key=KEY)
    assert http.posts[0]["headers"]["Idempotency-Key"] == KEY


def test_same_caller_key_across_repeated_invocations(client):
    c, http = client
    c.create_post("acct_1", "one", idempotency_key=KEY)
    c.create_post("acct_1", "one", idempotency_key=KEY)
    keys = [p["headers"].get("Idempotency-Key") for p in http.posts]
    assert keys == [KEY, KEY]  # client passes the caller's key through unchanged


def test_bad_key_rejected_before_any_send(client):
    c, http = client
    for bad in ("not-a-uuid", "", "12345", " " * 36):
        with pytest.raises(ValueError):
            c.create_post("acct_1", "hi", idempotency_key=bad)
        with pytest.raises(ValueError):
            c.create_post_raw({"content": "hi"}, idempotency_key=bad)
    assert http.posts == []  # validation happens before the network POST


def test_overlong_key_rejected(client):
    c, http = client
    with pytest.raises(ValueError):
        c.create_post_raw({"content": "hi"},
                          idempotency_key=str(uuid.uuid4()) + "x" * 300)
    assert http.posts == []


def test_response_shape_unchanged(client):
    c, http = client
    out = c.create_post("acct_1", "hi", idempotency_key=KEY)
    assert out == {"_id": "zpost_1", "status": "published"}  # r.json() untouched
    payload = http.posts[0]["json"]
    assert payload["platforms"][0]["accountId"] == "acct_1"
    assert payload["publishNow"] is True


def test_payload_semantics_unchanged_with_key(client):
    c, http = client
    c.create_post("acct_1", "hi", media_urls=["https://r2/a.jpg"],
                  scheduled_for="2026-10-03T14:00:00-04:00",
                  idempotency_key=KEY)
    payload = http.posts[0]["json"]
    assert payload["scheduledFor"] == "2026-10-03T18:00:00Z"
    assert payload["timezone"] == "UTC"
    assert payload["mediaItems"] == [{"type": "image", "url": "https://r2/a.jpg"}]
