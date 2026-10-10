"""Offline tests for protected exact-byte media preparation (20261010).

Unit tests run with: python -m pytest tests/test_exact_byte_media_prepare.py
They use only in-memory fakes; no live R2, no network, no credentials.

Optional disposable-PostgreSQL 17 regressions run only when
ECHO_EXACT_BYTE_PREPARE_PG_DSN points at a disposable local database that is
empty except for what this file creates; never production. They drive the
``psql`` client (no psycopg dependency) and require the psql binary on PATH:
  ECHO_EXACT_BYTE_PREPARE_PG_DSN="host=/tmp port=5432 dbname=echo_exact_byte_test user=blakeruff" \
      python -m pytest tests/test_exact_byte_media_prepare.py -k pg
"""
import base64
import io
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent.exact_byte_media_prepare import (
    MediaPrepareError, prepare_calendar_row_media, prepare_media_object,
)
from agent.r2_immutable_media import ImmutableMediaError, SignedLockRuleSource

ROOT = Path(__file__).resolve().parents[1]
NOW = 2000000000
ACCOUNT = "account"
BUCKET = "bucket"
BASE = "https://media.example.test"
PREFIX = "echo-exact-byte-protected/"
SOURCE_URL = "https://origin.example.test/reviewed/image-one.png"
THUMB_URL = "https://origin.example.test/reviewed/thumb-one.jpg"
SOURCE_ALLOWLIST = ("origin.example.test",)
PUBLIC_IP = "93.184.216.34"  # globally routable; returned by the fake resolver
IMAGE_BYTES = b"exact reviewed image bytes \x00\x01\xff" * 32
THUMB_BYTES = b"exact reviewed thumbnail bytes" * 16

import hashlib
IMAGE_DIGEST = hashlib.sha256(IMAGE_BYTES).hexdigest()
THUMB_DIGEST = hashlib.sha256(THUMB_BYTES).hexdigest()
IMAGE_KEY = PREFIX + "sha256/" + IMAGE_DIGEST + ".png"
THUMB_KEY = PREFIX + "sha256/" + THUMB_DIGEST + ".jpg"


def fake_resolver(addresses=(PUBLIC_IP,)):
    """Injectable resolver: maps every host to fixed answers (no real DNS)."""
    import socket

    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in addresses]
    return resolve


def lock_document(prefix=PREFIX, retention=NOW + 86400):
    return {"schema": "echo-r2-lock-attestation-v1", "account_id": ACCOUNT,
            "bucket": BUCKET, "public_base_url": BASE,
            "public_serving": "controlled-direct-byte-serving",
            "observed_at": NOW, "expires_at": NOW + 300,
            "rules": [{"id": "lock", "enabled": True, "prefix": prefix,
                       "protection": "overwrite-and-delete",
                       "retention_until": retention}]}


def lock_source(doc):
    key = Ed25519PrivateKey.generate()
    payload = json.dumps(doc).encode()
    envelope = json.dumps({"payload": base64.b64encode(payload).decode(),
                           "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return SignedLockRuleSource(lambda: envelope, pub)


class FakeBody:
    def __init__(self, data):
        self._io = io.BytesIO(data)
        self.decode_content = False

    def read(self, n=-1):
        return self._io.read(n)

    def close(self):
        pass


class FakeResponse:
    def __init__(self, url, data, content_type="image/png", status=200):
        self.url = url
        self.status_code = status
        self.history = []
        self.headers = {"Content-Length": str(len(data)), "Content-Type": content_type}
        self.raw = FakeBody(data)

    def close(self):
        pass


class FakeSession:
    """Routes source and protected public URLs to fixed byte payloads."""
    def __init__(self, payloads, statuses=None):
        self.payloads = payloads
        self.statuses = statuses or {}
        self.requested = []

    def get(self, url, **kwargs):
        assert kwargs.get("allow_redirects") is False
        self.requested.append(url)
        status = self.statuses.get(url)
        if status is not None:
            return FakeResponse(url, b"", status=status)
        data, ctype = self.payloads[url]
        return FakeResponse(url, data, ctype)


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = 0
        self.meta = SimpleNamespace(
            endpoint_url="https://%s.r2.cloudflarestorage.com" % ACCOUNT)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            e = Exception("NotFound")
            e.response = {"Error": {"Code": "404"},
                          "ResponseMetadata": {"HTTPStatusCode": 404}}
            raise e
        return {"ContentLength": len(self.objects[Key][0])}

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None):
        assert IfNoneMatch == "*"  # write-once create-if-absent only
        if Key in self.objects:
            raise PreconditionFailed("conditional write failed")
        self.puts += 1
        self.objects[Key] = (bytes(Body), ContentType)
        return {}

    def get_object(self, Bucket, Key):
        data, _ = self.objects[Key]
        return {"Body": FakeBody(data), "ContentLength": len(data)}


def session_with(public_overrides=None):
    payloads = {
        SOURCE_URL: (IMAGE_BYTES, "image/png"),
        THUMB_URL: (THUMB_BYTES, "image/jpeg"),
        BASE + "/" + IMAGE_KEY: (IMAGE_BYTES, "image/png"),
        BASE + "/" + THUMB_KEY: (THUMB_BYTES, "image/jpeg"),
    }
    payloads.update(public_overrides or {})
    return FakeSession(payloads)


def prepare_one(s3=None, session=None, doc=None, url=SOURCE_URL, role="image",
                ordinal=0, source_allowlist=SOURCE_ALLOWLIST, resolver=None):
    s3 = s3 or FakeS3()
    return prepare_media_object(
        url, role=role, ordinal=ordinal, bucket=BUCKET, account_id=ACCOUNT,
        public_base_url=BASE, protected_prefix=PREFIX, s3=s3,
        lock_source=lock_source(doc or lock_document()),
        required_retention_until=NOW + 3600, now=NOW,
        source_allowlist=source_allowlist,
        resolver=resolver if resolver is not None else fake_resolver(),
        session=session or session_with()), s3


def test_prepare_copies_exact_bytes_and_proves_lock():
    binding, s3 = prepare_one()
    assert binding.role == "image" and binding.ordinal == 0
    assert binding.source_url == SOURCE_URL
    assert binding.prepared_url == BASE + "/" + IMAGE_KEY
    assert binding.sha256 == "sha256:" + IMAGE_DIGEST
    assert binding.byte_length == len(IMAGE_BYTES)
    assert binding.lock_rule_id == "lock"
    assert binding.retention_until == NOW + 86400
    # No transformation: stored object is byte-identical to the reviewed source.
    assert s3.objects[IMAGE_KEY] == (IMAGE_BYTES, "image/png")
    receipt = binding.receipt()
    assert receipt["prepared_url"] == binding.prepared_url
    assert len(receipt["attestation_sha256"]) == 64


def test_prepare_row_image_and_thumbnail():
    s3 = FakeS3()
    bindings = prepare_calendar_row_media(
        {"image_url": SOURCE_URL, "thumbnail_url": THUMB_URL},
        image_fields=("image_url", "thumbnail_url"),
        bucket=BUCKET, account_id=ACCOUNT, public_base_url=BASE,
        protected_prefix=PREFIX, s3=s3, lock_source=lock_source(lock_document()),
        source_allowlist=SOURCE_ALLOWLIST, resolver=fake_resolver(),
        required_retention_until=NOW + 3600, now=NOW, session=session_with())
    assert [b.role for b in bindings] == ["image", "thumbnail"]
    assert [b.ordinal for b in bindings] == [0, 1]
    assert s3.objects[THUMB_KEY] == (THUMB_BYTES, "image/jpeg")


def test_write_once_create_if_absent_and_conflict_race():
    s3 = FakeS3()
    session = session_with()
    first, _ = prepare_one(s3=s3, session=session)
    assert s3.puts == 1
    # Second prepare sees the existing object and never overwrites it.
    second, _ = prepare_one(s3=s3, session=session)
    assert s3.puts == 1
    assert second.prepared_url == first.prepared_url
    # A lost create race (concurrent identical prepare) resolves by proof.
    s3.puts = 0

    class RacingS3(FakeS3):
        def head_object(self, Bucket, Key):
            e = Exception("NotFound")
            e.response = {"Error": {"Code": "404"},
                          "ResponseMetadata": {"HTTPStatusCode": 404}}
            raise e

    racing = RacingS3()
    racing.objects[IMAGE_KEY] = (IMAGE_BYTES, "image/png")  # created mid-race
    third, _ = prepare_one(s3=racing, session=session)
    assert third.sha256 == "sha256:" + IMAGE_DIGEST


def test_source_redirect_refused():
    session = FakeSession({SOURCE_URL: (IMAGE_BYTES, "image/png")},
                          statuses={SOURCE_URL: 302})
    with pytest.raises(MediaPrepareError, match="redirects or unavailable"):
        prepare_one(session=session)


def test_source_oversize_refused():
    big = b"x" * (1024 * 1024 + 1)
    session = FakeSession({SOURCE_URL: (big, "image/png")})
    with pytest.raises(MediaPrepareError):
        prepare_media_object(
            SOURCE_URL, role="image", ordinal=0, bucket=BUCKET, account_id=ACCOUNT,
            public_base_url=BASE, protected_prefix=PREFIX, s3=FakeS3(),
            lock_source=lock_source(lock_document()),
            required_retention_until=NOW + 3600, max_bytes=1024 * 1024,
            source_allowlist=SOURCE_ALLOWLIST, resolver=fake_resolver(),
            now=NOW, session=session)


def test_source_content_type_refused():
    session = FakeSession({SOURCE_URL: (IMAGE_BYTES, "text/html")})
    with pytest.raises(MediaPrepareError, match="content type"):
        prepare_one(session=session)


def test_non_https_and_bad_prefix_refused():
    with pytest.raises(MediaPrepareError):
        prepare_media_object(
            "http://origin.example.test/x.png", role="image", ordinal=0,
            bucket=BUCKET, account_id=ACCOUNT, public_base_url=BASE,
            protected_prefix=PREFIX, s3=FakeS3(),
            lock_source=lock_source(lock_document()),
            required_retention_until=NOW + 3600, now=NOW,
            source_allowlist=SOURCE_ALLOWLIST, resolver=fake_resolver(),
            session=session_with())
    with pytest.raises(MediaPrepareError, match="protected prefix"):
        prepare_media_object(
            SOURCE_URL, role="image", ordinal=0, bucket=BUCKET, account_id=ACCOUNT,
            public_base_url=BASE, protected_prefix="no-trailing-slash", s3=FakeS3(),
            lock_source=lock_source(lock_document()),
            required_retention_until=NOW + 3600, now=NOW,
            source_allowlist=SOURCE_ALLOWLIST, resolver=fake_resolver(),
            session=session_with())


# --- SSRF source-host guard (operator-pinned allowlist, DNS rebinding) ---

def test_source_host_not_in_allowlist_refused():
    session = session_with()
    session.payloads["https://evil.example.test/x.png"] = (IMAGE_BYTES, "image/png")
    with pytest.raises(MediaPrepareError, match="host not allowed"):
        prepare_one(url="https://evil.example.test/x.png", session=session)


def test_source_allowlist_empty_fails_closed():
    with pytest.raises(MediaPrepareError, match="allowlist"):
        prepare_one(source_allowlist=())
    with pytest.raises(MediaPrepareError, match="allowlist"):
        prepare_one(source_allowlist=None)


def test_source_allowlist_suffix_match():
    url = "https://cdn.origin.example.test/reviewed/image-one.png"
    session = session_with()
    session.payloads[url] = (IMAGE_BYTES, "image/png")
    binding, _ = prepare_one(url=url, session=session,
                             source_allowlist=("origin.example.test",))
    assert binding.source_url == url


def test_source_literal_ip_refused():
    with pytest.raises(MediaPrepareError, match="host invalid"):
        prepare_one(url="https://169.254.169.254/latest/meta-data.png")


def test_source_private_resolution_refused():
    for private in ("10.0.0.9", "192.168.1.10", "127.0.0.1", "169.254.1.1",
                    "fd00::1", "224.0.0.1"):
        with pytest.raises(MediaPrepareError, match="non-public"):
            prepare_one(resolver=fake_resolver((private,)))


def test_source_unresolvable_host_refused():
    import socket

    def dead_resolver(host, port, *a, **k):
        raise socket.gaierror("no answer")

    with pytest.raises(MediaPrepareError, match="unresolvable"):
        prepare_one(resolver=dead_resolver)


def test_source_dns_rebinding_refused():
    """First answer is public (validation), second flips private (rebind)."""
    calls = []

    def rebinding_resolver(addresses=[PUBLIC_IP]):
        import socket

        def resolve(host, port, *a, **k):
            calls.append(1)
            current = PUBLIC_IP if len(calls) == 1 else "10.0.0.9"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (current, port))]
        return resolve

    with pytest.raises(MediaPrepareError, match="non-public|resolution changed"):
        prepare_one(resolver=rebinding_resolver())

    calls.clear()

    def drifting_resolver():
        import socket

        def resolve(host, port, *a, **k):
            calls.append(1)
            current = PUBLIC_IP if len(calls) == 1 else "8.8.8.8"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (current, port))]
        return resolve

    with pytest.raises(MediaPrepareError, match="resolution changed"):
        prepare_one(resolver=drifting_resolver())


def test_cli_allowlist_env_parsing(monkeypatch):
    cli = load_cli()
    for name in cli.ENV_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("EXACT_BYTE_R2_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("EXACT_BYTE_R2_PROTECTED_PREFIX", PREFIX)
    monkeypatch.setenv("EXACT_BYTE_SOURCE_HOST_ALLOWLIST",
                       "pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev, images.unsplash.com")
    config = cli.require_env()
    assert config["_source_allowlist"] == (
        "pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev", "images.unsplash.com")
    monkeypatch.setenv("EXACT_BYTE_SOURCE_HOST_ALLOWLIST", " , ,")
    with pytest.raises(cli.CliError, match="SOURCE_HOST_ALLOWLIST"):
        cli.require_env()


def test_video_and_gallery_rows_refused():
    kwargs = dict(bucket=BUCKET, account_id=ACCOUNT, public_base_url=BASE,
                  protected_prefix=PREFIX, s3=FakeS3(),
                  lock_source=lock_source(lock_document()),
                  source_allowlist=SOURCE_ALLOWLIST, resolver=fake_resolver(),
                  required_retention_until=NOW + 3600, now=NOW, session=session_with())
    with pytest.raises(MediaPrepareError, match="video"):
        prepare_calendar_row_media({"image_url": "https://origin.example.test/clip.mp4"}, **kwargs)
    with pytest.raises(MediaPrepareError, match="outbound image shape"):
        prepare_calendar_row_media(
            {"image_url": SOURCE_URL, "image_urls": [SOURCE_URL]}, **kwargs)


def test_public_origin_mismatch_is_not_immutability():
    # Two matching reads never prove immutability; a divergent public readback
    # (or a missing lock) must fail closed even after a successful upload.
    session = session_with({BASE + "/" + IMAGE_KEY: (b"tampered bytes", "image/png")})
    with pytest.raises(ImmutableMediaError):
        prepare_one(session=session)


def test_lock_prefix_must_cover_protected_prefix():
    doc = lock_document(prefix="some-other-prefix/")
    with pytest.raises(ImmutableMediaError):
        prepare_one(doc=doc)


def test_retention_horizon_must_cover_fetch_window():
    doc = lock_document(retention=NOW + 1800)  # shorter than required horizon
    with pytest.raises(ImmutableMediaError):
        prepare_one(doc=doc)


# --- address-pinned default HTTPS transport (DNS rebinding fail-closed) ---

def test_make_pin_for_allowlisted_global_addresses_only():
    from agent.exact_byte_media_prepare import _make_pin_for
    pin = _make_pin_for(("origin.example.test",), resolver=fake_resolver())
    assert pin("origin.example.test", 443) == PUBLIC_IP
    # Suffix allowlist entry pins subdomains too.
    assert pin("cdn.origin.example.test", 443) == PUBLIC_IP
    # Unlisted hosts fail closed even when they resolve publicly.
    with pytest.raises(MediaPrepareError, match="host not allowed"):
        pin("evil.example.test", 443)
    # Private/loopback/multicast answers are never pinned.
    for private in ("10.0.0.9", "127.0.0.1", "169.254.1.1", "fd00::1", "224.0.0.1"):
        bad = _make_pin_for(("origin.example.test",),
                            resolver=fake_resolver((private,)))
        with pytest.raises(MediaPrepareError, match="non-public"):
            bad("origin.example.test", 443)


def test_pinned_session_mounts_address_pinned_https_adapter():
    import requests
    from agent.exact_byte_media_prepare import _make_pin_for, _pinned_https_session
    session = _pinned_https_session(
        _make_pin_for(("origin.example.test",), resolver=fake_resolver()))
    try:
        assert isinstance(session, requests.Session)
        assert session.trust_env is False
        adapter = session.get_adapter("https://origin.example.test/x.png")
        pool = adapter.poolmanager
        pool_cls = pool.pool_classes_by_scheme["https"]
        assert pool_cls is not __import__(
            "urllib3.connectionpool", fromlist=["HTTPSConnectionPool"]).HTTPSConnectionPool
    finally:
        session.close()


def test_pinned_pool_dials_pinned_ip_but_keeps_real_hostname():
    # Pool construction pins a validated IP while keeping the real hostname
    # for Host, TLS SNI and certificate verification.
    from agent.exact_byte_media_prepare import _make_pin_for, _pinned_https_session
    session = _pinned_https_session(
        _make_pin_for(("origin.example.test",), resolver=fake_resolver()))
    try:
        adapter = session.get_adapter("https://origin.example.test/x.png")
        pool = adapter.poolmanager.connection_from_host("origin.example.test", 443, "https")
        conn = pool._new_conn()
        assert conn.pinned_ip == PUBLIC_IP  # dial target: pinned validated IP
        assert conn.host == "origin.example.test"  # SNI/Host/cert unchanged
        assert conn._dns_host == "origin.example.test"  # hostname not rewritten
    finally:
        session.close()


def test_pinned_connection_actually_dials_validated_ip(monkeypatch):
    from agent.exact_byte_media_prepare import _make_pin_for, _pinned_https_session
    import urllib3.util.connection

    calls = []
    sentinel = object()

    def fake_create_connection(address, timeout, source_address=None,
                               socket_options=None):
        calls.append(address)
        return sentinel

    monkeypatch.setattr(urllib3.util.connection, "create_connection",
                        fake_create_connection)
    session = _pinned_https_session(
        _make_pin_for(("origin.example.test",), resolver=fake_resolver()))
    try:
        adapter = session.get_adapter("https://origin.example.test/x.png")
        pool = adapter.poolmanager.connection_from_host(
            "origin.example.test", 443, "https")
        conn = pool._new_conn()
        assert conn._new_conn() is sentinel
        assert calls == [(PUBLIC_IP, 443)]
        assert conn.host == "origin.example.test"
    finally:
        session.close()


def test_pinned_pool_refuses_unlisted_or_private_host():
    from agent.exact_byte_media_prepare import _make_pin_for, _pinned_https_session
    session = _pinned_https_session(
        _make_pin_for(("origin.example.test",), resolver=fake_resolver()))
    try:
        adapter = session.get_adapter("https://origin.example.test/x.png")
        pool = adapter.poolmanager.connection_from_host("evil.example.test", 443, "https")
        with pytest.raises(MediaPrepareError, match="host not allowed"):
            pool._new_conn()
        # A connection that never received a pin fails closed at dial time.
        from agent.exact_byte_media_prepare import _pinned_https_session as _s  # noqa
        bare = type(pool).ConnectionCls(host="origin.example.test", port=443)
        with pytest.raises(MediaPrepareError, match="host not allowed"):
            bare._new_conn()
    finally:
        session.close()


def test_prepare_default_transport_builds_one_pinned_session(monkeypatch):
    # With no injected session, preparation builds ONE pinned session and
    # hands it to BOTH the source read and the protected public readback
    # (verify_immutable_media), pinning the allowlisted source host plus the
    # operator-pinned public base host.
    import agent.exact_byte_media_prepare as m
    built = []

    def fake_build(pin_for):
        built.append(pin_for)
        session = session_with()
        session.close = lambda: None
        return session

    monkeypatch.setattr(m, "_pinned_https_session", fake_build)
    binding, _ = prepare_media_object(
        SOURCE_URL, role="image", ordinal=0, bucket=BUCKET, account_id=ACCOUNT,
        public_base_url=BASE, protected_prefix=PREFIX, s3=FakeS3(),
        lock_source=lock_source(lock_document()),
        required_retention_until=NOW + 3600, now=NOW,
        source_allowlist=SOURCE_ALLOWLIST, resolver=fake_resolver(),
        session=None), None
    assert binding.sha256 == "sha256:" + IMAGE_DIGEST
    assert len(built) == 1
    pin_for = built[0]
    assert pin_for("origin.example.test", 443) == PUBLIC_IP
    assert pin_for("media.example.test", 443) == PUBLIC_IP  # public base host pinned
    with pytest.raises(MediaPrepareError, match="host not allowed"):
        pin_for("evil.example.test", 443)


# --- operator CLI (scripts/exact_byte_prepare_media.py), offline paths only ---

def load_cli():
    spec = importlib.util.spec_from_file_location(
        "exact_byte_prepare_media", ROOT / "scripts" / "exact_byte_prepare_media.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_requires_env(monkeypatch):
    cli = load_cli()
    for name in cli.ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(cli.CliError, match="missing environment"):
        cli.require_env()


def test_cli_row_id_validation():
    cli = load_cli()
    assert cli.main(["--plan", "--row-id", "not-a-uuid"]) == 2
    assert cli.main(["--execute", "--row-id", "0" * 8 + "-0000-0000-0000-000000000000"]) == 2


def test_cli_eligibility_prechecks():
    cli = load_cli()
    # Preparation works only on pending/unapproved rows; a prepared row must
    # return through the normal client reapproval flow.
    good = {"status": "pending", "variant_status": "active",
            "image_url": SOURCE_URL, "publish_claim_token": None,
            "publish_reservation_day": None, "published_at": None,
            "late_post_id": None, "approval_digest": None,
            "approval_kind": None, "approved_by": None, "approved_at": None}
    assert cli.client_side_eligible(good) == []
    for change in ({"status": "published"}, {"status": "denied"}, {"status": "killed"},
                   # Legacy approved rows -- even with a NULL approval_digest --
                   # are never prepared: their URL rebind must go back through
                   # client reapproval.
                   {"status": "approved"},
                   {"status": "approved", "approval_digest": None},
                   {"approval_digest": "0" * 64},
                   {"approval_kind": "human"},
                   {"approved_by": "clerk-user-1"},
                   {"approved_at": "2026-10-01T00:00:00+00"},
                   {"publish_claim_token": str(__import__("uuid").uuid4())},
                   {"published_at": "2026-01-01"}, {"late_post_id": "x"},
                   {"variant_status": "superseded"}, {"image_url": None}):
        assert cli.client_side_eligible({**good, **change})
    assert cli.image_fields_for(good) == ("image_url",)
    assert cli.image_fields_for({**good, "thumbnail_url": THUMB_URL}) == (
        "image_url", "thumbnail_url")


def test_cli_unknown_commit_outcome_aborts_without_retry(monkeypatch):
    cli = load_cli()
    config = {name: "x" for name in cli.ENV_VARS}
    config.update({"EXACT_BYTE_R2_PUBLIC_BASE_URL": BASE,
                   "EXACT_BYTE_R2_PROTECTED_PREFIX": PREFIX,
                   "EXACT_BYTE_SOURCE_HOST_ALLOWLIST": "origin.example.test"})
    config["_source_allowlist"] = ("origin.example.test",)
    row = {"status": "pending", "variant_status": "active", "image_url": SOURCE_URL,
           "publish_claim_token": None, "publish_reservation_day": None,
           "published_at": None, "late_post_id": None, "approval_digest": None,
           "approval_kind": None, "approved_by": None, "approved_at": None}
    monkeypatch.setattr(cli, "fetch_row", lambda conn, row_id: dict(row))
    monkeypatch.setattr(cli, "build_s3", lambda c: object())
    monkeypatch.setattr(cli, "build_lock_source", lambda c: object())
    binding_calls = []

    class FakeBinding:
        role = "image"
        prepared_url = BASE + "/" + IMAGE_KEY

        def receipt(self):
            return {"ordinal": 0, "role": "image", "source_url": SOURCE_URL,
                    "prepared_url": self.prepared_url, "sha256": "sha256:" + "0" * 64,
                    "byte_length": 10, "content_type": "image/png",
                    "lock_rule_id": "lock", "retention_until": NOW + 86400,
                    "attestation_sha256": "0" * 64}

    def fake_prepare(*a, **k):
        binding_calls.append(1)
        return [FakeBinding()]

    monkeypatch.setattr(cli, "prepare_calendar_row_media", fake_prepare)

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            pass

        def fetchone(self):
            return ({"prepared": True},)

    class CommitLostConn:
        commits = 0

        def cursor(self):
            return FakeCursor()

        def commit(self):
            type(self).commits += 1
            if type(self).commits > 1:  # first commit ends the read-only txn
                raise Exception("connection lost during commit")

    with pytest.raises(cli.CliError, match="UNKNOWN COMMIT OUTCOME"):
        cli.execute(CommitLostConn(), config,
                    ["0" * 8 + "-0000-0000-0000-000000000000"],
                    "evidence", 3600)
    # Aborted at the first unknown outcome; no retry of the same row.
    assert CommitLostConn.commits == 2  # read commit + single lost write commit
    assert len(binding_calls) == 1


# --- disposable PostgreSQL 17 regressions (never production) ----------------

PG_DSN = os.environ.get("ECHO_EXACT_BYTE_PREPARE_PG_DSN")
OWNER = "exact_byte_owner_20261010"
FENCE_SQL = ROOT / "migrations" / "delivered_byte_send_fence_20261010.sql"
PREPARE_SQL = ROOT / "migrations" / "exact_byte_media_prepare_20261010.sql"


def _psql(sql_text, expect_error=None):
    """One psql session; ON_ERROR_STOP so any error fails the statement."""
    import subprocess
    result = subprocess.run(
        ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", PG_DSN,
         "-c", sql_text], capture_output=True, text=True)
    if expect_error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0 and expect_error in result.stderr, (
            result.returncode, result.stdout, result.stderr)
    return result.stdout.strip()


def _psql_file(path):
    import subprocess
    result = subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", PG_DSN, "-f", str(path)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _jl(obj):
    text = json.dumps(obj)
    assert "$ebm$" not in text
    return "$ebm$" + text + "$ebm$"


@pytest.mark.skipif(not PG_DSN, reason="disposable PG DSN not provided")
@pytest.mark.skipif(not __import__("shutil").which("psql"),
                    reason="psql client not on PATH")
def test_pg_prepare_bind_rpc():
    import uuid

    _psql("drop schema public cascade; create schema public")
    _psql("drop role if exists %s" % OWNER)
    # Fresh PG17 lacks the Supabase roles the migrations reference in their
    # REVOKE statements; bootstrap them before applying either migration.
    for role in ("anon", "authenticated", "service_role"):
        _psql("do $$ begin if not exists(select 1 from pg_roles where rolname='%s') "
              "then create role %s nologin; end if; end $$" % (role, role))
    _psql("""
        create table public.content_calendar(
          id uuid primary key, gym_id text, account text, format text,
          image_url text, thumbnail_url text, post_date date,
          publish_reservation_day date, publish_claim_token uuid,
          status text, variant_status text, published_at timestamptz,
          late_post_id text, logical_post_id uuid, reject_reason text,
          gbp_location_id text, caption text,
          approval_kind text, approved_by text, approved_at timestamptz,
          approval_digest text,
          updated_at timestamptz
        )""")
    _psql_file(FENCE_SQL)
    _psql_file(PREPARE_SQL)

    def seed_row(**overrides):
        row = {"id": str(uuid.uuid4()), "gym_id": "gym-a", "account": "instagram",
               "format": "feed", "image_url": SOURCE_URL, "thumbnail_url": None,
               "post_date": "2026-10-20", "publish_reservation_day": None,
               "publish_claim_token": None, "status": "pending",
               "variant_status": "active", "published_at": None,
               "late_post_id": None, "caption": "reviewed caption",
               "approval_kind": None, "approved_by": None, "approved_at": None,
               "approval_digest": None, "updated_at": None}
        row.update(overrides)
        cols = tuple(row.keys())
        values = ", ".join("null" if row[k] is None else "'%s'" % row[k] for k in cols)
        _psql("insert into content_calendar(%s) values(%s)"
              % (",".join(cols), values))
        return row

    def row_image(row_id):
        return json.loads(_psql(
            "select to_jsonb(c) from content_calendar c where id='%s'" % row_id))

    prepared_url = BASE + "/" + IMAGE_KEY
    thumb_prepared_url = BASE + "/" + THUMB_KEY

    def make_bindings(thumbnail=False):
        bindings = [{"ordinal": 0, "role": "image", "source_url": SOURCE_URL,
                     "prepared_url": prepared_url, "sha256": "sha256:" + IMAGE_DIGEST,
                     "byte_length": len(IMAGE_BYTES), "content_type": "image/png",
                     "lock_rule_id": "lock", "retention_until": NOW + 86400,
                     "attestation_sha256": "a" * 64}]
        if thumbnail:
            bindings.append(
                {"ordinal": 1, "role": "thumbnail", "source_url": THUMB_URL,
                 "prepared_url": thumb_prepared_url, "sha256": "sha256:" + THUMB_DIGEST,
                 "byte_length": len(THUMB_BYTES), "content_type": "image/jpeg",
                 "lock_rule_id": "lock", "retention_until": NOW + 86400,
                 "attestation_sha256": "b" * 64})
        return bindings

    def bind(row, before=None, bindings=None, thumbnail_url="null",
             expect_error=None):
        before = before if before is not None else row_image(row["id"])
        call = ("select public.exact_byte_prepare_bind_20261010('%s','%s',%s::jsonb,"
                "'%s',%s,%s::jsonb,'operator evidence')"
                % (str(uuid.uuid4()), row["id"], _jl(before), prepared_url,
                   thumbnail_url, _jl(bindings if bindings is not None
                                      else make_bindings())))
        out = _psql("set role %s; %s" % (OWNER, call),
                    expect_error=expect_error)
        if expect_error is None:
            return json.loads(out)
        return None

    # Owner-only read path: the owner role has no content_calendar grant, so
    # the CLI snapshot goes through exact_byte_prepare_read_row_20261010.
    row = seed_row()
    snapshot = _psql("set role %s; select public.exact_byte_prepare_read_row_20261010('%s')"
                     % (OWNER, row["id"]))
    assert json.loads(snapshot)["image_url"] == SOURCE_URL
    assert _psql("set role %s; select public.exact_byte_prepare_read_row_20261010('%s')"
                 % (OWNER, str(uuid.uuid4()))) == ""
    _psql("set role service_role; select public.exact_byte_prepare_read_row_20261010('%s')"
          % row["id"], expect_error="permission denied")
    _psql("set role %s; select image_url from content_calendar where id='%s'"
          % (OWNER, row["id"]), expect_error="permission denied")

    # Service role cannot call the owner-only bind RPC at all.
    _psql("set role service_role; select public.exact_byte_prepare_bind_20261010("
          "'%s','%s','{}'::jsonb,'%s',null,'[]'::jsonb,'x')"
          % (str(uuid.uuid4()), str(uuid.uuid4()), prepared_url),
          expect_error="permission denied")

    # Happy path: exact before image binds only image_url; receipt recorded.
    row = seed_row()
    result = bind(row)
    assert result["prepared"] is True
    after = row_image(row["id"])
    assert after["image_url"] == prepared_url
    # Status stays pending: the prepared row must go back through the normal
    # client reapproval flow before it can be approved and sent.
    assert after["status"] == "pending" and after["caption"] == "reviewed caption"
    receipts = _psql("select jsonb_agg(bindings) from "
                     "exact_byte_media_prepare_20261010 where calendar_row_id='%s'"
                     % row["id"])
    assert json.loads(receipts)[0][0]["prepared_url"] == prepared_url
    # One terminal preparation per row; a second bind refuses.
    bind(row, expect_error="already recorded")
    # Receipt is append-only: the owner role holds no DELETE grant at all,
    # and even a privileged delete is refused by the immutability trigger.
    _psql("set role %s; delete from exact_byte_media_prepare_20261010" % OWNER,
          expect_error="permission denied")
    _psql("delete from exact_byte_media_prepare_20261010",
          expect_error="append-only")

    # Before-image CAS: any changed field refuses and writes nothing.
    row = seed_row()
    tampered = row_image(row["id"])
    tampered["caption"] = "changed after review"
    bind(row, before=tampered, expect_error="before-image mismatch")
    assert row_image(row["id"])["image_url"] == SOURCE_URL

    # Refused states.
    bind(seed_row(status="published"), expect_error="published/historical")
    bind(seed_row(status="denied"), expect_error="published/historical")
    bind(seed_row(status="killed"), expect_error="published/historical")
    bind(seed_row(published_at="2026-01-01T00:00:00+00"), expect_error="published/historical")
    bind(seed_row(publish_claim_token=str(uuid.uuid4())), expect_error="active claim/lease")
    bind(seed_row(variant_status="superseded"), expect_error="non-active variant")

    # Approved/provenance fail-closed (2026-10-10 independent-review ruling):
    # ANY approved or provenance-bearing row -- including legacy 'approved'
    # rows with a NULL approval_digest -- is refused; a media rebind under a
    # stale approval is never silently applied. No recompute, no forge.
    bind(seed_row(status="approved"), expect_error="approved/provenance")
    bind(seed_row(status="approved", approval_digest="0" * 64),
         expect_error="approved/provenance")
    bind(seed_row(approval_digest="0" * 64), expect_error="approved/provenance")
    bind(seed_row(approval_kind="human"), expect_error="approved/provenance")
    bind(seed_row(approval_kind="automatic"), expect_error="approved/provenance")
    bind(seed_row(approved_by="clerk-user-1"), expect_error="approved/provenance")
    bind(seed_row(approved_at="2026-10-01T00:00:00+00"),
         expect_error="approved/provenance")

    # Existing exact-byte attempt refuses preparation.
    clean = seed_row()
    _psql("set role %s; insert into exact_byte_tenant_20261010 "
          "values('gym-a','tenant-a','UTC','e')" % OWNER)
    # exact_byte_cutover_20261010 / exact_byte_send_attempt_20261010 carry no
    # owner INSERT grant by design; the fixture seeds them as the harness
    # superuser (no grant weakening).
    _psql("insert into exact_byte_cutover_20261010 "
          "values('%s','sha256:%s','sha256:%s','%s','sha256:%s','deploy evidence',"
          "'history evidence')"
          % (str(uuid.uuid4()), "1" * 64, "2" * 64, "a" * 40, "3" * 64))
    cutover = _psql("select cutover_id from exact_byte_cutover_20261010")
    _psql("insert into exact_byte_send_attempt_20261010(attempt_id,"
          "calendar_row_id,claim_token,row_revision,canonical_tenant,post_date,"
          "reservation_day,provider_target,context,cutover_id) values('%s','%s',"
          "'%s','sha256:%s','tenant-a','2026-10-20','2026-10-20',%s::jsonb,"
          "'{}'::jsonb,'%s')"
          % (str(uuid.uuid4()), clean["id"], str(uuid.uuid4()), "4" * 64,
             _jl({"provider": "late", "platform": "instagram", "account_id": "a"}),
             cutover))
    bind(clean, expect_error="attempted row")

    # Binding/URL mismatch refuses.
    row = seed_row()
    bad = make_bindings()
    bad[0] = dict(bad[0], prepared_url=BASE + "/other.png")
    bind(row, bindings=bad, expect_error="binding URL mismatch")
    bad = make_bindings()
    bad[0] = dict(bad[0], source_url="https://evil.example/x.png")
    bind(row, bindings=bad, expect_error="source binding mismatch")

    # Complete outbound binding: a reviewed thumbnail must be rebound with its
    # own audited binding (never left mutable), and a thumbnail-less row must
    # not gain one.
    row = seed_row(thumbnail_url=THUMB_URL)
    bind(row, expect_error="requires complete thumbnail binding")
    bind(row, thumbnail_url="null", bindings=make_bindings(),
         expect_error="requires complete thumbnail binding")
    result = bind(row, thumbnail_url="'%s'" % thumb_prepared_url,
                  bindings=make_bindings(thumbnail=True))
    assert result["prepared"] is True
    after = row_image(row["id"])
    assert after["image_url"] == prepared_url
    assert after["thumbnail_url"] == thumb_prepared_url
    row = seed_row()
    bind(row, thumbnail_url="'%s'" % thumb_prepared_url,
         expect_error="thumbnail URL invalid")

    # Post-update persistence: an updated_at trigger fires during bind; the
    # receipt must record the exact persisted row, not a synthetic projection.
    _psql("""
        create or replace function public.test_set_updated_at()
        returns trigger language plpgsql as $t$
        begin new.updated_at := clock_timestamp(); return new; end $t$""")
    _psql("create trigger touch_updated_at before update on content_calendar "
          "for each row execute function public.test_set_updated_at()")
    row = seed_row()
    result = bind(row)
    persisted = row_image(row["id"])
    receipt = json.loads(_psql(
        "select jsonb_build_object('before', before_image, 'after', after_image) "
        "from exact_byte_media_prepare_20261010 where calendar_row_id='%s'"
        % row["id"]))
    assert receipt["after"] == persisted
    assert receipt["before"]["image_url"] == SOURCE_URL
    assert persisted["updated_at"] is not None
    assert persisted["image_url"] == prepared_url
    assert persisted["caption"] == "reviewed caption"
    _psql("drop trigger touch_updated_at on content_calendar")
