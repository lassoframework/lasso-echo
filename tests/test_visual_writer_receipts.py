"""Focused fail-closed source/rendition writer contracts."""
import hashlib
import uuid

import pytest

from agent import story_reburn, visual_writer_prepare as prep


TENANT = "11111111-1111-4111-8111-111111111111"
RAW = "https://media.example/raw.jpg?version=1"
BURN = "https://media.example/burn.jpg?version=2"
SOURCE = b"original pixels"
DELIVERED = b"caption pixels"


def md5(data):
    return "md5:" + hashlib.md5(data).hexdigest()


class Response:
    status_code = 200

    def __init__(self, value, status_code=200):
        self.value, self.status_code = value, status_code

    def json(self):
        return self.value


class HTTP:
    def __init__(self, *, reject=False):
        self.posts = []
        self.reject = reject

    def get(self, url, *, params, headers, timeout):
        if url.endswith("tenant_alias"):
            return Response([{"alias_key": "gym", "tenant_id": TENANT}])
        if url.endswith("visual_group_alias"):
            return Response([{"group_key": "vg_scene"}])
        if url.endswith("media_asset"):
            return Response([{"id": "asset-1", "gym_id": "gym",
                              "content_hash": hashlib.md5(SOURCE).hexdigest()}])
        raise AssertionError(url)

    def post(self, url, *, headers, json, timeout):
        self.posts.append((url, json))
        if self.reject:
            return Response({"message": "receipt mismatch"}, 400)
        return Response({"group_key": json["p_group_key"],
                         "source_fingerprint": md5(SOURCE),
                         "delivered_fingerprint": md5(DELIVERED),
                         "usage_claimed": False})


class Store:
    def __init__(self, http):
        self.http = http

    def _client(self):
        return self.http

    def _rest(self, path):
        return "https://db.example/" + path

    def _headers(self, extra=None):
        return extra or {}


def evidence(**changes):
    value = {"source_exact_url": RAW, "delivered_exact_url": BURN,
             "source_fingerprint": md5(SOURCE), "delivered_fingerprint": md5(DELIVERED),
             "source_byte_length": len(SOURCE), "delivered_byte_length": len(DELIVERED),
             "operation": "reburn"}
    value.update(changes)
    return value


def reader(url):
    return {RAW: SOURCE, BURN: DELIVERED}[url]


def receipts(**kwargs):
    assert kwargs["source_bytes"] == SOURCE
    assert kwargs["delivered_bytes"] == DELIVERED
    assert kwargs["render_evidence"] == evidence()
    return {name: str(uuid.uuid4()) for name in (
        "source_read_receipt", "delivered_read_receipt", "render_receipt")}


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    # Isolated-test sentinel for injected byte readers/receipt writers; never a
    # usable database DSN and deliberately absent from production configuration.
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "receipt_owner")


def test_distinct_pair_requires_owner_receipt_producer():
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="owner receipt producer"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=reader, render_evidence=evidence(), isolated_test_callbacks=True)
    assert http.posts == []


@pytest.mark.parametrize("missing", [RAW, BURN])
def test_unknown_object_bytes_never_reach_receipt_producer(missing):
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="bytes could not be verified"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=lambda url: None if url == missing else reader(url),
                     render_evidence=evidence(),
                     receipt_writer=lambda **kwargs: pytest.fail("should not issue receipts"),
                     isolated_test_callbacks=True)
    assert http.posts == []


def test_distinct_pair_uses_verified_source_and_delivered_receipts():
    http = HTTP()
    row = prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW,
                                            "source_media_asset_id": "asset-1"},
                       read_bytes=reader, render_evidence=evidence(), receipt_writer=receipts, isolated_test_callbacks=True)
    assert row["source_media_url"] == RAW
    assert row["byte_hash"] == "derived:" + md5(DELIVERED)
    assert row["visual_group_key"] == "vg_scene"
    assert len(http.posts) == 1
    assert http.posts[0][0].endswith("visual_global_prepare_source_rendition")
    assert http.posts[0][1]["p_tenant"] == TENANT
    assert http.posts[0][1]["p_group_key"] == "vg_scene"


@pytest.mark.parametrize("change", [
    {"source_fingerprint": md5(b"wrong")},
    {"delivered_fingerprint": md5(b"stale")},
    {"delivered_byte_length": 1},
    {"source_exact_url": RAW.split("?")[0]},
])
def test_render_evidence_must_match_exact_reads(change):
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="rendition lineage"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=reader, render_evidence=evidence(**change),
                     receipt_writer=receipts, isolated_test_callbacks=True)
    assert http.posts == []


def test_owner_rpc_rejection_leaves_calendar_write_unprepared():
    http = HTTP(reject=True)
    with pytest.raises(prep.VisualPreparationError, match="RPC"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=reader, render_evidence=evidence(),
                     receipt_writer=receipts, isolated_test_callbacks=True)
    assert len(http.posts) == 1


def test_drive_asset_hash_checks_raw_bytes_not_burned_bytes():
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="Drive asset MD5"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW,
                                            "source_media_asset_id": "asset-1"},
                     read_bytes=lambda url: b"changed" if url == RAW else DELIVERED,
                     render_evidence=evidence(), receipt_writer=receipts, isolated_test_callbacks=True)
    assert http.posts == []


def test_reburn_evidence_requires_hosted_readback(monkeypatch, tmp_path):
    from agent import story_image, media_host
    from agent import config
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    source = tmp_path / "source.jpg"
    output = tmp_path / "burned.jpg"
    source.write_bytes(SOURCE)
    output.write_bytes(DELIVERED)
    monkeypatch.setattr(story_reburn, "_download", lambda url, log: str(source))
    monkeypatch.setattr(story_image, "get_or_make_story_image", lambda *a, **k: str(output))
    monkeypatch.setattr(media_host, "host_media", lambda *a, **k: BURN)
    monkeypatch.setattr(prep, "_bytes_for_url", reader)
    result = story_reburn.reburn_with_evidence(RAW, "caption", "Gym", "gym")
    assert result[0] == BURN
    observed = result[1].as_dict()
    assert {key: observed[key] for key in evidence()} == evidence()
    assert observed["evidence_ref"].startswith("story_reburn:")
    assert observed["observed_by"] == observed["rendered_by"] == "story_reburn"
    assert not source.exists()


def test_reburn_evidence_rejects_changed_hosted_bytes(monkeypatch, tmp_path):
    from agent import story_image, media_host, config
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    source = tmp_path / "source.jpg"
    output = tmp_path / "burned.jpg"
    source.write_bytes(SOURCE)
    output.write_bytes(DELIVERED)
    monkeypatch.setattr(story_reburn, "_download", lambda url, log: str(source))
    monkeypatch.setattr(story_image, "get_or_make_story_image", lambda *a, **k: str(output))
    monkeypatch.setattr(media_host, "host_media", lambda *a, **k: BURN)
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: SOURCE if url == RAW else b"changed")
    assert story_reburn.reburn_with_evidence(RAW, "caption", "Gym", "gym", logger=lambda m: None) is None


@pytest.mark.parametrize("filename", ["Zanshin Fitness-194.jpg", "IMG_4957.jpeg"])
def test_reburn_download_accepts_minted_filename_spaces_and_exact_object_key(
        monkeypatch, filename):
    import io
    from pathlib import Path
    from types import SimpleNamespace
    from agent import config, media_host

    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://media.example")
    key = "echo/gym/65b27f25c0240b05/" + filename
    url = media_host.public_url_for(key)
    assert prep._own_media_url(url)
    assert ("%20" in url) == (" " in filename)
    calls, bodies = [], []

    def get_object(**kwargs):
        calls.append(kwargs)
        body = io.BytesIO(SOURCE)
        bodies.append(body)
        return {"Body": body, "ContentLength": len(SOURCE)}

    monkeypatch.setattr(media_host, "_default_client", lambda: SimpleNamespace(
        _bucket="test-bucket", _s3=SimpleNamespace(get_object=get_object)))
    path = story_reburn._download(url, lambda message: pytest.fail(message))
    try:
        assert path is not None
        assert Path(path).read_bytes() == SOURCE
        assert calls == [{"Bucket": "test-bucket", "Key": key}]
        assert bodies[0].closed
    finally:
        if path:
            Path(path).unlink()


@pytest.mark.parametrize("path", [
    "raw space.jpg", "raw%09.jpg", "raw%0a.jpg", "raw%0d.jpg", "raw%00.jpg",
    "raw%7f.jpg", "raw%C2%85.jpg", "raw%C2%A0.jpg", "raw%E2%80%83.jpg",
    "%2e/raw.jpg", "%2e%2e/raw.jpg", "raw%2Fname.jpg", "raw%5Cname.jpg",
])
def test_encoded_spaces_do_not_allow_unsafe_bucket_paths(monkeypatch, path):
    from agent import config
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: pytest.fail("must not fetch"))
    url = "https://media.example/echo/gym/" + path
    assert not prep._own_media_url(url)
    assert story_reburn._download(url, lambda message: None) is None


def test_reburn_download_rejects_external_and_lookalike_hosts(monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: pytest.fail("must not fetch"))
    for url in ("http://127.0.0.1/private", "https://media.example.evil.test/raw.jpg",
                "https://media.example@evil.test/raw.jpg", "https://media.example/raw.jpg#part",
                "https://media.example/redirect/../raw.jpg",
                "https://media.example/redirect/%2e%2e/raw.jpg",
                "https://media.example/raw%0d%0a.jpg"):
        # Dot segments can be normalized by HTTP intermediaries; they are not
        # accepted as exact object paths for this trust boundary.
        assert story_reburn._download(url, lambda message: None) is None


def test_query_object_read_disables_redirects_and_caps_stream(monkeypatch):
    from agent import config
    import requests
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setattr(prep, "MAX_VISUAL_BYTES", 6)
    calls = []

    class HTTPResponse:
        def __init__(self, status, chunks):
            self.status_code, self.chunks = status, chunks

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, *, chunk_size):
            assert chunk_size == prep._READ_CHUNK
            yield from self.chunks

    responses = iter([HTTPResponse(302, [b"ignored"]),
                      HTTPResponse(200, [b"1234", b"5678"]),
                      HTTPResponse(200, [b"123", b"456"])])

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(requests, "get", get)
    assert prep._bytes_for_url(RAW) is None  # redirect never followed
    assert prep._bytes_for_url(RAW) is None  # over the byte cap
    assert prep._bytes_for_url(RAW) == b"123456"
    assert all(call[1] == {"timeout": (5, 30), "allow_redirects": False,
                           "stream": True} for call in calls)


def test_injected_reader_cannot_bypass_byte_cap(monkeypatch):
    monkeypatch.setattr(prep, "MAX_VISUAL_BYTES", 5)
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="bytes could not be verified"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=lambda url: b"123456", render_evidence=evidence(),
                     receipt_writer=receipts, isolated_test_callbacks=True)
    assert http.posts == []


def test_bucket_object_read_streams_with_cap_and_closes_body(monkeypatch):
    from agent import config, media_host
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setattr(prep, "MAX_VISUAL_BYTES", 5)

    class Body:
        def __init__(self):
            self.chunks = iter([b"123", b"456", b""])
            self.closed = False

        def read(self, size):
            assert size == prep._READ_CHUNK
            return next(self.chunks)

        def close(self):
            self.closed = True

    body = Body()

    class S3:
        def get_object(self, *, Bucket, Key):
            assert Bucket == "bucket" and Key == "raw.jpg"
            return {"Body": body, "ContentLength": 5}

    class Client:
        _s3 = S3()
        _bucket = "bucket"

    monkeypatch.setattr(media_host, "_default_client", lambda: Client())
    assert prep._bytes_for_url("https://media.example/raw.jpg") is None
    assert body.closed


def test_transformed_write_without_render_evidence_fails_closed():
    # Distinct source and delivered bytes are a transformation; without
    # verified render lineage no receipts are issued and no RPC is attempted.
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="rendition lineage"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=reader, render_evidence=None,
                     receipt_writer=lambda **kwargs: pytest.fail("no receipts without lineage"),
                     isolated_test_callbacks=True)
    assert http.posts == []


def test_injected_rendition_callbacks_require_isolated_configuration(monkeypatch):
    # Production configuration has no test sentinel: injected byte readers or
    # receipt writers on the transformed path fail closed before any byte read.
    monkeypatch.delenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", raising=False)
    monkeypatch.delenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", raising=False)
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="isolated test configuration"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=lambda url: pytest.fail("no production injected reads"),
                     render_evidence=evidence(),
                     receipt_writer=receipts, isolated_test_callbacks=True)
    assert http.posts == []


def test_isolated_sentinel_still_requires_explicit_callback_flag():
    # The sentinel environment alone never authorizes injected callbacks.
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="isolated test configuration"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=reader, render_evidence=evidence(),
                     receipt_writer=receipts)
    assert http.posts == []


def test_wrong_sentinel_values_never_authorize_injection(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "service_role")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="isolated test configuration"):
        prep.prepare(Store(http), "gym", {"image_url": BURN, "source_media_url": RAW},
                     read_bytes=reader, render_evidence=evidence(),
                     receipt_writer=receipts, isolated_test_callbacks=True)
    assert http.posts == []
