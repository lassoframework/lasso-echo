"""Synthetic offline tests for agent/social_source_capture.py and the additive
fetch_posts_raw path in agent/social_baseline.py. No live network calls:
the Apify client and its transport are fakes."""
import gzip
import hashlib
import json
from datetime import datetime, timezone

import pytest

from agent import social_source_capture as ssc
from agent.social_baseline import ApifyClient, ApifyError

TOKEN = "apify_test_token_secret_123"
HANDLE = "testgym"
PID = "17841400000000001"
LOCATOR = "https://www.instagram.com/testgym/"
RESPONSE_ID = "apify-run-abc123"
MAP_EVIDENCE = {"portal_mapping": "verified", "revision": "rev-7"}


def _items(handle=HANDLE, owner_id=PID, n=2):
    return [
        {
            "id": f"post{i}",
            "ownerUsername": handle,
            "ownerId": owner_id,
            "caption": f"caption {i}",
            "timestamp": "2026-09-01T12:00:00.000Z",
        }
        for i in range(n)
    ]


def _raw(n=2, **kw):
    return json.dumps(_items(n=n, **kw)).encode("utf-8")


class FakeClient:
    """Stands in for ApifyClient: serves fixed raw bytes, claims a token."""

    def __init__(self, raw=None, token=TOKEN, exc=None,
                 response_id=RESPONSE_ID):
        self._raw = _raw() if raw is None else raw
        self._token = token
        self._exc = exc
        self._response_id = response_id
        self.calls = []

    def token(self):
        return self._token

    def fetch_posts_raw(self, handle, newer_than_days,
                        results_limit=None, max_bytes=None):
        self.calls.append({
            "handle": handle,
            "newer_than_days": newer_than_days,
            "results_limit": results_limit,
            "max_bytes": max_bytes,
        })
        if self._exc is not None:
            raise self._exc
        return self._raw, self._response_id


def _capture(client, **kw):
    args = dict(
        mapped_handle=HANDLE,
        mapped_provider_account_id=PID,
        mapped_source_locator=LOCATOR,
        gym_id="gym-uuid-1",
        echo_account_key="testgym_ig",
        mapping_revision="rev-7",
        mapping_evidence=dict(MAP_EVIDENCE),
        source_revision="src-rev-3",
        client=client,
        now=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc),
    )
    args.update(kw)
    return ssc.capture_social_source(**args)


# ---------------------------------------------------------------------------
# capture_social_source
# ---------------------------------------------------------------------------


def test_capture_returns_exact_raw_bytes_and_contract_provenance():
    raw = _raw()
    client = FakeClient(raw=raw)
    res = _capture(client)
    assert res.ok
    assert res.raw_bytes == raw  # byte-for-byte, not reconstructed JSON
    assert res.sha256 == hashlib.sha256(raw).hexdigest()
    p = res.provenance
    # echo_source_captures contract fields (portal bundle @ 8579400).
    assert p["gym_id"] == "gym-uuid-1"
    assert p["echo_account_key"] == "testgym_ig"
    assert p["source_kind"] == "social"
    # Provider endpoint provenance strictly separate from the account locator.
    assert p["source_url"].startswith("https://api.apify.com/v2/")
    assert p["capture_provider"] == "apify"
    assert p["provider_response_id"] == RESPONSE_ID
    assert p["provider_actor_id"] == "apify~instagram-post-scraper"
    assert p["source_locator"] == LOCATOR
    assert p["provider_account_id"] == PID
    assert p["account_handle"] == HANDLE
    assert p["source_revision"] == "src-rev-3"
    assert p["mapping_revision"] == "rev-7"
    assert p["mapping_evidence"] == MAP_EVIDENCE
    assert p["byte_count"] == len(raw)
    assert p["items_count"] == 2
    assert p["identity_verified"] is True
    assert p["fetched_at"].startswith("2026-10-07T12:00:00")


def test_provenance_never_contains_token_or_authorization():
    res = _capture(FakeClient())
    blob = json.dumps(res.provenance).lower()
    assert TOKEN.lower() not in blob
    assert "authorization" not in blob
    assert "bearer" not in blob
    assert res.provenance.get("token") is None


def test_uses_only_the_mapped_locator():
    client = FakeClient()
    res = _capture(client)
    assert res.ok
    assert len(client.calls) == 1
    assert client.calls[0]["handle"] == HANDLE  # exactly the mapped handle


def test_handle_normalization_strips_at_and_case():
    raw = _raw(handle="TestGym")
    res = _capture(FakeClient(raw=raw), mapped_handle="@TESTGYM")
    assert res.ok
    assert res.provenance["account_handle"] == "testgym"


def test_handle_mismatch_fails_closed():
    res = _capture(FakeClient(raw=_raw(handle="someoneelse")))
    assert not res.ok
    assert res.raw_bytes == b""
    assert res.sha256 == ""
    assert "not the mapped" in res.reason
    assert TOKEN not in res.reason


def test_provider_account_id_mismatch_fails_closed():
    res = _capture(FakeClient(raw=_raw(owner_id="999999999999")))
    assert not res.ok
    assert "exactly match" in res.reason


def test_missing_item_identity_fails_closed():
    raw = json.dumps([{"id": "post0", "caption": "no identity"}]).encode()
    res = _capture(FakeClient(raw=raw))
    assert not res.ok
    assert "missing a verifiable account identity" in res.reason


def test_item_missing_owner_id_fails_closed():
    raw = json.dumps([{"id": "p", "ownerUsername": HANDLE}]).encode()
    res = _capture(FakeClient(raw=raw))
    assert not res.ok
    assert "missing a verifiable account identity" in res.reason


def test_empty_dataset_holds():
    res = _capture(FakeClient(raw=b"[]"))
    assert not res.ok
    assert res.raw_bytes == b""
    assert "empty dataset" in res.reason


def test_pid_is_required():
    res = _capture(FakeClient(), mapped_provider_account_id=None)
    assert not res.ok
    assert "required" in res.reason
    res2 = _capture(FakeClient(), mapped_provider_account_id="   ")
    assert not res2.ok


def test_locator_must_be_verified_instagram_https():
    for bad in (None, "", "http://www.instagram.com/testgym/",
                "https://evil.com/testgym/", "https://www.instagram.com/"):
        res = _capture(FakeClient(), mapped_source_locator=bad)
        assert not res.ok, bad
        assert "locator" in res.reason


def test_gym_and_account_key_and_revisions_required():
    for kw in ({"gym_id": ""}, {"echo_account_key": " "},
               {"source_revision": ""}, {"mapping_revision": ""}):
        res = _capture(FakeClient(), **kw)
        assert not res.ok, kw


def test_mapping_evidence_must_be_nonempty_dict():
    for bad in (None, {}, "rev-7", ["x"], 0):
        res = _capture(FakeClient(), mapping_evidence=bad)
        assert not res.ok, bad
        assert "mapping_evidence" in res.reason


def test_missing_handle_fails_closed():
    res = _capture(FakeClient(), mapped_handle="")
    assert not res.ok
    assert "mapped account locator" in res.reason


def test_implausible_handle_fails_closed():
    res = _capture(FakeClient(), mapped_handle="bad handle!!")
    assert not res.ok


def test_no_token_is_inert():
    res = _capture(FakeClient(token=""))
    assert not res.ok
    assert "APIFY_TOKEN not set" in res.reason


def test_apify_error_reason_is_token_scrubbed():
    exc = ApifyError(f"apify 402: bad token {TOKEN}")
    res = _capture(FakeClient(exc=exc))
    assert not res.ok
    assert TOKEN not in res.reason
    assert "***" in res.reason


def test_unexpected_client_error_reports_type_only():
    res = _capture(FakeClient(exc=RuntimeError(f"boom {TOKEN}")))
    assert not res.ok
    assert res.reason == "apify pull failed: RuntimeError"
    assert TOKEN not in res.reason


def test_unparseable_json_fails_closed():
    res = _capture(FakeClient(raw=b"\xff\xfe not json"))
    assert not res.ok
    assert "not parseable JSON" in res.reason


def test_non_list_dataset_fails_closed():
    res = _capture(FakeClient(raw=b'{"error": "x"}'))
    assert not res.ok
    assert "non-list dataset" in res.reason


def test_missing_provider_response_identity_holds():
    res = _capture(FakeClient(response_id=None))
    assert not res.ok
    assert "provider response identity" in res.reason


def test_oversize_response_fails_closed_via_max_bytes():
    raw = _raw(n=50)
    res = _capture(FakeClient(raw=raw), max_bytes=len(raw) - 1)
    # The fake ignores max_bytes (the real client enforces it mid-stream);
    # capture re-checks the cap itself.
    assert not res.ok
    assert "size cap" in res.reason


def test_max_bytes_cannot_exceed_contract_ceiling():
    res = _capture(FakeClient(), max_bytes=ssc.MAX_CAPTURE_BYTES + 1)
    assert not res.ok
    assert "raw_bytes ceiling" in res.reason


def test_max_bytes_must_be_positive():
    res = _capture(FakeClient(), max_bytes=0)
    assert not res.ok


def test_lookback_days_threaded_to_client():
    client = FakeClient()
    _capture(client, lookback_days=30)
    assert client.calls[0]["newer_than_days"] == 30
    assert client.calls[0]["max_bytes"] == ssc.MAX_CAPTURE_BYTES


# ---------------------------------------------------------------------------
# ApifyClient.fetch_posts_raw + _bounded_response_bytes (additive path)
# ---------------------------------------------------------------------------


class FakeRawStream:
    """Mirrors urllib3's HTTPResponse.stream(amt, decode_content=...)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.calls = []

    def stream(self, amt=65536, decode_content=False):
        self.calls.append({"amt": amt, "decode_content": decode_content})
        yield from self._chunks


class FakeResponse:
    def __init__(self, status_code=200, content=b"[]", text=None,
                 headers=None, stream_chunks=None, raw=True):
        self.status_code = status_code
        self.content = content
        self.headers = ({"x-apify-act-run-id": RESPONSE_ID}
                        if headers is None else headers)
        self.text = text if text is not None else content.decode(
            "utf-8", "replace") if isinstance(content, bytes) else str(content)
        if stream_chunks is None:
            stream_chunks = [content] if isinstance(content, bytes) else []
        self.raw = FakeRawStream(stream_chunks) if raw else None
        self.closed = False

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def close(self):
        self.closed = True


class FakeHTTP:
    def __init__(self, response):
        self._response = response
        self.posts = []

    def post(self, url, params=None, json=None, timeout=None, **kw):
        self.posts.append({"url": url, "params": params, "json": json,
                           "timeout": timeout, **kw})
        return self._response


def _raw_client(response):
    return ApifyClient(token=TOKEN, http=FakeHTTP(response))


def test_fetch_posts_raw_returns_exact_bytes_and_response_id():
    # Content that is NOT valid JSON proves no parsing happens on this path.
    raw = b'[{"ownerUsername": "gym", ]  trailing garbage'
    resp = FakeResponse(200, raw, headers={"x-apify-act-run-id": "run-9"})
    out, rid = _raw_client(resp).fetch_posts_raw("gym", 90)
    assert out == raw
    assert rid == "run-9"
    assert resp.closed is True
    # The bytes come from the RAW stream with decoding disabled.
    assert resp.raw.calls
    assert all(c["decode_content"] is False for c in resp.raw.calls)


def test_fetch_posts_raw_gzip_captures_compressed_entity_bytes():
    # THE DEFECT REPRO: a gzipped response must be captured as the original
    # compressed entity bytes, NOT the decompressed JSON text that
    # requests' iter_content/content would yield (decode_content=True).
    entity = gzip.compress(_raw(), compresslevel=9)
    assert entity != _raw()  # genuinely compressed on the wire
    resp = FakeResponse(
        200, entity,
        headers={"content-encoding": "gzip",
                 "content-length": str(len(entity)),
                 "x-apify-act-run-id": "run-gz"},
        stream_chunks=[entity[:17], entity[17:]],
    )
    out, rid = _raw_client(resp).fetch_posts_raw("gym", 90)
    assert out == entity                      # compressed bytes, byte-for-byte
    assert out != _raw()                      # not the decompressed text
    assert gzip.decompress(out) == _raw()     # and it IS the original entity
    assert rid == "run-gz"


def test_fetch_posts_raw_gzip_capture_round_trips_through_capture():
    # End-to-end: gzipped entity bytes land in CaptureResult.raw_bytes exactly.
    entity = gzip.compress(_raw())
    class GzipClient(FakeClient):
        def fetch_posts_raw(self, handle, newer_than_days,
                            results_limit=None, max_bytes=None):
            return entity, RESPONSE_ID
    res = _capture(GzipClient())
    assert res.ok
    assert res.raw_bytes == entity
    assert res.sha256 == hashlib.sha256(entity).hexdigest()


def test_fetch_posts_raw_reads_stream_bounded_and_closes():
    chunks = [b"a" * 60, b"b" * 60]
    resp = FakeResponse(200, b"", stream_chunks=chunks,
                        headers={"content-length": "120",
                                 "x-apify-dataset-id": "ds-1"})
    out, rid = _raw_client(resp).fetch_posts_raw("gym", 90)
    assert out == b"a" * 60 + b"b" * 60
    assert rid == "ds-1"
    assert resp.closed is True


def test_fetch_posts_raw_content_length_over_cap_fails_before_read():
    resp = FakeResponse(200, b"x" * 10,
                        headers={"content-length": str(10_000_000)})
    with pytest.raises(ApifyError, match="size cap"):
        _raw_client(resp).fetch_posts_raw("gym", 90)
    assert resp.raw.calls == []  # rejected before the stream was read


def test_fetch_posts_raw_stream_over_cap_fails_mid_stream():
    chunks = [b"x" * 100, b"y" * 100]
    resp = FakeResponse(200, b"", stream_chunks=chunks)
    with pytest.raises(ApifyError, match="size cap"):
        _raw_client(resp).fetch_posts_raw("gym", 90, max_bytes=150)
    assert resp.closed is True  # stream still closed on the failure path


def test_fetch_posts_raw_exactly_at_cap_passes():
    resp = FakeResponse(200, b"x" * 100)
    out, _ = _raw_client(resp).fetch_posts_raw("gym", 90, max_bytes=100)
    assert out == b"x" * 100


def test_fetch_posts_raw_fails_closed_without_raw_stream():
    # A non-streaming response (or a transport lacking .raw) must HOLD:
    # decoded content is never substituted for the original entity bytes.
    resp = FakeResponse(200, b"[]", raw=False)
    with pytest.raises(ApifyError, match="no raw byte stream"):
        _raw_client(resp).fetch_posts_raw("gym", 90)


def test_fetch_posts_raw_refuses_non_bytes_chunk():
    resp = FakeResponse(200, b"", stream_chunks=["decoded text chunk"])
    with pytest.raises(ApifyError, match="not bytes"):
        _raw_client(resp).fetch_posts_raw("gym", 90)
    assert resp.closed is True


def test_fetch_posts_raw_no_response_id_header_returns_none():
    resp = FakeResponse(200, b"[]", headers={})
    out, rid = _raw_client(resp).fetch_posts_raw("gym", 90)
    assert out == b"[]"
    assert rid is None


def test_fetch_posts_raw_bytearray_chunk_coerced():
    resp = FakeResponse(200, b"", stream_chunks=[bytearray(b"[")
                                                 , bytearray(b"]")])
    out, _ = _raw_client(resp).fetch_posts_raw("gym", 90)
    assert out == b"[]"


def test_fetch_posts_raw_requires_token():
    client = ApifyClient(token="", http=FakeHTTP(FakeResponse()))
    with pytest.raises(ApifyError, match="APIFY_TOKEN not set"):
        client.fetch_posts_raw("gym", 90)


def test_fetch_posts_raw_requires_handle():
    client = _raw_client(FakeResponse())
    with pytest.raises(ApifyError, match="empty instagram handle"):
        client.fetch_posts_raw("  @ ", 90)


def test_fetch_posts_raw_error_scrubs_token():
    resp = FakeResponse(402, f"bad token {TOKEN}".encode())
    with pytest.raises(ApifyError) as ei:
        _raw_client(resp).fetch_posts_raw("gym", 90)
    assert TOKEN not in str(ei.value)
    assert "***" in str(ei.value)


def test_fetch_posts_still_parses_and_validates():
    # The existing method's behavior is unchanged: parse + list validation.
    resp = FakeResponse(200, _raw())
    items = _raw_client(resp).fetch_posts("gym", 90)
    assert isinstance(items, list) and items[0]["ownerUsername"] == HANDLE
    resp2 = FakeResponse(200, b'{"not": "a list"}')
    with pytest.raises(ApifyError, match="non-list"):
        _raw_client(resp2).fetch_posts("gym", 90)
