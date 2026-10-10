"""Offline issuer/readback tests; no production credentials or hosted objects."""
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from agent import generated_hosted_byte_authority as a


TENANT = "gym-a"
VERSION = str(uuid.uuid4())
URL = "https://cdn.example/gym-a/generated/a.png"
DATA = b"SYNTHETIC exact image bytes"
SHA = hashlib.sha256(DATA).hexdigest()
MANIFEST = dict(gym_id=TENANT, artifact_version_id=VERSION, hosted_url=URL,
                delivered_sha256=SHA, engine="synthetic-test-only", recipe={"crop": False})
MANIFEST_BYTES = json.dumps(MANIFEST, separators=(",", ":")).encode()
RECEIPT = dict(receipt_id=str(uuid.uuid4()), gym_id=TENANT, artifact_version_id=VERSION,
               hosted_url=URL, delivered_sha256=SHA, render_manifest=MANIFEST)


class SyntheticReader(a.HostedObjectReader):
    def __init__(self, events, data=DATA, error=None):
        super().__init__({TENANT: ["https://cdn.example/gym-a/generated/"]})
        self.events, self.data, self.error = events, data, error

    def read(self, tenant, url):
        assert (tenant, url) == (TENANT, URL)
        self.events.append("GET")
        if self.error:
            raise self.error
        return self.data


class Connection:
    def __init__(self, events, *, receipt=None, authorized=True, commit_error=False):
        self.events, self.receipt, self.authorized = events, receipt or RECEIPT, authorized
        self.commit_error = commit_error
        self.autocommit = False
        self.info = SimpleNamespace(transaction_status=0)

    def execute(self, sql, args):
        if "authorized" in sql:
            self.events.append("authorize")
            value = self.authorized
        else:
            self.events.append("reconcile" if "reconcile" in sql
                               else "lookup" if "lookup" in sql else "issue")
            value = self.receipt
            if "reconcile" in sql:
                assert args == (TENANT, VERSION, URL, SHA, hashlib.sha256(MANIFEST_BYTES).hexdigest())
            if "issue" in sql:
                assert args == (TENANT, VERSION, URL, SHA, hashlib.sha256(MANIFEST_BYTES).hexdigest(), DATA, MANIFEST_BYTES)
        return SimpleNamespace(fetchone=lambda: (value,))

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")

    def commit(self):
        self.events.append("commit")
        if self.commit_error:
            raise RuntimeError("sensitive database error")


def issuer(events, **kw):
    return a.GeneratedHostedByteAuthority(lambda: Connection(events, **kw),
        tenant_id=TENANT, reader=SyntheticReader(events))


def issue(authority, **over):
    args = dict(artifact_version_id=VERSION, hosted_url=URL, expected_sha256=SHA,
                manifest_bytes=MANIFEST_BYTES)
    args.update(over)
    return authority.issue(**args)


def lookup(authority, **over):
    args = dict(artifact_version_id=VERSION, hosted_url=URL, expected_sha256=SHA,
                manifest_bytes=MANIFEST_BYTES, receipt_id=RECEIPT["receipt_id"])
    args.update(over)
    return authority.lookup(**args)


def reconcile(authority, **over):
    args = dict(artifact_version_id=VERSION, hosted_url=URL, expected_sha256=SHA,
                manifest_bytes=MANIFEST_BYTES)
    args.update(over)
    return authority.reconcile(**args)


def test_issue_fresh_read_and_commit_exact_receipt_without_locks_during_get():
    events = []
    assert issue(issuer(events)) == RECEIPT
    assert events == ["authorize", "rollback", "close", "GET", "authorize", "issue", "commit", "close"]


def test_lookup_checks_durable_exact_authority_not_producer_shape():
    events = []
    assert lookup(issuer(events)) == RECEIPT
    assert "GET" not in events and "issue" not in events and "commit" not in events
    with pytest.raises(a.HostedByteHold, match="receipt_binding_invalid"):
        lookup(issuer([]), receipt_id=str(uuid.uuid4()))


@pytest.mark.parametrize("over", [dict(gym_id="gym-b"), dict(artifact_version_id=str(uuid.uuid4())),
    dict(hosted_url=URL + "changed"), dict(delivered_sha256="f" * 64),
    dict(render_manifest={"forged": True}), dict(receipt_id="invented"), dict(extra="forged")])
def test_forged_or_wrong_database_readback_is_rejected(over):
    receipt = {**RECEIPT, **over}
    with pytest.raises(a.HostedByteHold):
        lookup(issuer([], receipt=receipt))


def test_hosted_byte_mismatch_and_inaccessible_never_reach_write():
    for reader in (SyntheticReader([], b"different bytes"),
                   SyntheticReader([], error=a.HostedByteHold("generated_hosted_read_unavailable"))):
        events = reader.events
        authority = a.GeneratedHostedByteAuthority(lambda: Connection(events), tenant_id=TENANT, reader=reader)
        with pytest.raises(a.HostedByteHold):
            issue(authority)
        assert "issue" not in events and "commit" not in events


def test_unauthorized_principal_never_reads_hosted_object():
    events = []
    with pytest.raises(a.HostedByteHold, match="identity_unavailable"):
        issue(issuer(events, authorized=False))
    assert "GET" not in events


def test_commit_uncertainty_is_static_and_never_retried():
    events = []
    with pytest.raises(a.HostedByteHold, match="commit_uncertain"):
        issue(issuer(events, commit_error=True))
    assert events.count("GET") == events.count("issue") == events.count("commit") == 1
    assert events[-1] == "close"


@pytest.mark.parametrize("over", [dict(artifact_version_id="invented"), dict(expected_sha256=SHA.upper()),
    dict(manifest_bytes=b"{}"), dict(manifest_bytes=MANIFEST_BYTES.replace(b'"gym-a"', b'"gym-b"')),
    dict(manifest_bytes=MANIFEST_BYTES[:-1] + b',"gym_id":"gym-a"}'),
    dict(manifest_bytes=MANIFEST_BYTES[:-1] + b',"numeric":NaN}'),
    dict(hosted_url="http://cdn.example/gym-a/a.png")])
def test_invalid_or_unbound_manifest_never_reaches_authority(over):
    events = []
    with pytest.raises(a.HostedByteHold):
        issue(issuer(events), **over)
    assert not events


def test_reconcile_returns_the_one_committed_receipt_without_receipt_uuid():
    events = []
    assert reconcile(issuer(events)) == RECEIPT
    assert events == ["authorize", "reconcile", "rollback", "close"]
    assert "GET" not in events and "issue" not in events and "commit" not in events


def test_lost_ack_commit_uncertainty_reconciles_without_retry_or_new_uuid():
    events = []
    with pytest.raises(a.HostedByteHold, match="commit_uncertain"):
        issue(issuer(events, commit_error=True))
    assert events.count("issue") == 1
    # No automatic retry or invented replacement UUID; one exact readback.
    assert reconcile(issuer(events)) == RECEIPT
    assert events.count("issue") == 1 and events.count("reconcile") == 1


def test_reconcile_rejects_absent_changed_or_forged_readback():
    class Absent(Connection):
        def execute(self, sql, args):
            if "reconcile" in sql:
                raise RuntimeError("no committed hosted byte receipt")
            return super().execute(sql, args)
    with pytest.raises(a.HostedByteHold, match="reconcile_unavailable"):
        reconcile(a.GeneratedHostedByteAuthority(lambda: Absent([]), tenant_id=TENANT,
                                                 reader=SyntheticReader([])))
    for over in (dict(gym_id="gym-b"), dict(artifact_version_id=str(uuid.uuid4())),
                 dict(hosted_url=URL + "changed"), dict(delivered_sha256="f" * 64),
                 dict(render_manifest={"forged": True}), dict(extra="forged")):
        with pytest.raises(a.HostedByteHold, match="receipt_binding_invalid"):
            reconcile(issuer([], receipt={**RECEIPT, **over}))


def test_reconcile_unauthorized_and_invalid_input_hold_statically():
    events = []
    with pytest.raises(a.HostedByteHold, match="identity_unavailable"):
        reconcile(issuer(events, authorized=False))
    assert "reconcile" not in events
    for over in (dict(artifact_version_id="not-a-uuid"), dict(expected_sha256="F" * 64),
                 dict(hosted_url="http://cdn.example/gym-a/generated/a.png"),
                 dict(manifest_bytes=b"{}")):
        fresh = []
        with pytest.raises(a.HostedByteHold):
            reconcile(issuer(fresh), **over)
        assert not fresh


def test_reconcile_database_error_is_static_and_leaks_no_detail():
    class Broken(Connection):
        def execute(self, sql, args):
            if "reconcile" in sql:
                raise RuntimeError("sensitive DSN password detail")
            return super().execute(sql, args)
    authority = a.GeneratedHostedByteAuthority(lambda: Broken([]), tenant_id=TENANT)
    with pytest.raises(a.HostedByteHold, match="reconcile_unavailable") as error:
        reconcile(authority)
    assert "password" not in str(error.value) and "DSN" not in str(error.value)


def test_reader_cannot_be_producer_callback():
    with pytest.raises(a.HostedByteHold, match="trusted_reader_required"):
        a.GeneratedHostedByteAuthority(lambda: None, tenant_id=TENANT, reader=lambda *_: DATA)
    with pytest.raises(a.HostedByteHold, match="trusted_reader_required"):
        issue(a.GeneratedHostedByteAuthority(lambda: None, tenant_id=TENANT))


def test_busy_connection_is_rejected():
    connection = Connection([])
    connection.info.transaction_status = 2
    with pytest.raises(a.HostedByteHold, match="identity_unavailable"):
        issue(a.GeneratedHostedByteAuthority(lambda: connection, tenant_id=TENANT, reader=SyntheticReader([])))


@pytest.mark.parametrize("method", ["rollback", "close"])
def test_cleanup_errors_are_static_and_close_is_always_attempted(method):
    events = []
    class Broken(Connection):
        def rollback(self):
            super().rollback()
            if method == "rollback":
                raise RuntimeError("sensitive DSN")
        def close(self):
            super().close()
            if method == "close":
                raise RuntimeError("sensitive DSN")
    authority = a.GeneratedHostedByteAuthority(lambda: Broken(events), tenant_id=TENANT)
    with pytest.raises(a.HostedByteHold, match="cleanup_uncertain"):
        lookup(authority)
    assert events[-1] == "close"


@pytest.mark.parametrize("url", ["http://cdn.example/gym-a/a.png", "https://user:secret@cdn.example/gym-a/a.png",
    "https://cdn.example/gym-a/a.png?token=secret", "https://cdn.example/gym-a/../gym-b/a.png",
    "https://cdn.example/gym-a/%2e%2e/gym-b/a.png", "https://cdn.example/gym-a/%2fb.png",
    "https://cdn.example/gym-a/a.png#x", "https://cdn.example:444/gym-a/a.png",
    "https://cdn.example/gym-b/a.png", "https://cdn.example/gym-a/a.png\n",
    "https://cdn.example/gym-a/%252e%252e/gym-b/a.png", "https://cdn.example/gym-a//a.png"])
def test_reader_rejects_unsafe_or_cross_tenant_url_before_dns(monkeypatch, url):
    reader = a.HostedObjectReader({TENANT: ["https://cdn.example/gym-a/"]})
    monkeypatch.setattr(a.socket, "getaddrinfo", lambda *_args, **_kw: pytest.fail("DNS called"))
    with pytest.raises(a.HostedByteHold):
        reader.read(TENANT, url)


def test_cross_tenant_url_scopes_must_not_overlap():
    with pytest.raises(a.HostedByteHold, match="url_scopes_required"):
        a.HostedObjectReader({"gym-a": ["https://cdn.example/generated/"],
                              "gym-b": ["https://cdn.example/generated/gym-b/"]})


@pytest.mark.parametrize("alias", ["CDN.EXAMPLE", "cdn.example:443", "CDN.EXAMPLE:443",
                                    "cdn.example.", "cdn.example.:443", "cdn.example:", "cdn.example:0443"])
def test_same_object_origin_alias_cannot_create_second_tenant_scope_or_read(monkeypatch, alias):
    # These URLs identify the same HTTPS origin/directory. They must never
    # become separate tenant namespaces, including the reviewed case+:443 bug.
    with pytest.raises(a.HostedByteHold):
        a.HostedObjectReader({"gym-a": ["https://cdn.example/shared/"],
                              "gym-b": [f"https://{alias}/shared/"]})
    reader = a.HostedObjectReader({"gym-a": ["https://cdn.example/shared/"],
                                   "gym-b": ["https://cdn.example/other/"]})
    monkeypatch.setattr(a.socket, "getaddrinfo", lambda *_args, **_kw: pytest.fail("DNS called"))
    with pytest.raises(a.HostedByteHold):
        reader.read("gym-b", f"https://{alias}/shared/a.png")
    with pytest.raises(a.HostedByteHold):
        reader.read("gym-a", f"https://{alias}/shared/a.png")


@pytest.mark.parametrize("alias", ["CDN.EXAMPLE", "cdn.example:443", "CDN.EXAMPLE:443", "cdn.example."])
def test_issuer_rejects_hosted_url_alias_reuse_before_authority(alias):
    events = []
    url = URL.replace("cdn.example", alias)
    manifest = json.dumps({**MANIFEST, "hosted_url": url}).encode()
    with pytest.raises(a.HostedByteHold, match="hosted_url_invalid"):
        issue(issuer(events), hosted_url=url, manifest_bytes=manifest)
    assert not events


@pytest.mark.parametrize("scheme", ["HTTPS", "Https"])
def test_normalized_scheme_alias_cannot_reuse_hosted_url_or_namespace(scheme):
    events = []
    url = URL.replace("https", scheme, 1)
    with pytest.raises(a.HostedByteHold, match="hosted_url_invalid"):
        issue(issuer(events), hosted_url=url, manifest_bytes=json.dumps({**MANIFEST, "hosted_url": url}).encode())
    with pytest.raises(a.HostedByteHold):
        a.HostedObjectReader({"gym-a": ["https://cdn.example/shared/"],
                              "gym-b": [f"{scheme}://cdn.example/shared/"]})
    assert not events


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"])
def test_nonpublic_dns_fails_closed(monkeypatch, address):
    monkeypatch.setattr(a.socket, "getaddrinfo", lambda *_args, **_kw: [(None, None, None, None, (address, 443))])
    reader = a.HostedObjectReader({TENANT: ["https://cdn.example/gym-a/"]})
    with pytest.raises(a.HostedByteHold, match="address_not_public"):
        reader.read(TENANT, URL)


@pytest.mark.parametrize("status,encoding,length,body,content_type,success", [
    (200, "identity", str(len(DATA)), DATA, "image/png", True),
    (302, "identity", str(len(DATA)), DATA, "image/png", False),
    (403, "identity", str(len(DATA)), DATA, "image/png", False),
    (200, "gzip", str(len(DATA)), DATA, "image/png", False),
    (200, "identity", "8388609", DATA, "image/png", False),
    (200, "identity", "999", DATA, "image/png", False),
    (200, "identity", "0", b"", "image/png", False),
    (200, "identity", str(len(DATA)), DATA, "text/html", False)])
def test_http_read_pins_public_address_and_refuses_redirect_encoding_size(monkeypatch, status, encoding, length, body, content_type, success):
    calls = []
    monkeypatch.setattr(a.socket, "getaddrinfo", lambda *_args, **_kw: [(None, None, None, None, ("8.8.8.8", 443))])
    class HTTP:
        def __init__(self, host, address, timeout):
            calls.append((host, address, timeout))
        def request(self, method, path, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return SimpleNamespace(status=status, getheader=lambda k, default=None: {
                "Content-Encoding": encoding, "Content-Length": length, "Content-Type": content_type}.get(k, default),
                read=lambda maximum: body)
        def close(self):
            calls.append("close")
    monkeypatch.setattr(a, "_PinnedHTTPSConnection", HTTP)
    reader = a.HostedObjectReader({TENANT: ["https://cdn.example/gym-a/"]})
    if success:
        assert reader.read(TENANT, URL) == DATA
    else:
        with pytest.raises(a.HostedByteHold):
            reader.read(TENANT, URL)
    assert calls[0] == ("cdn.example", "8.8.8.8", 15)
    assert calls[1] == ("GET", "/gym-a/generated/a.png", {"Accept-Encoding": "identity"})
    assert calls[-1] == "close"


def test_unconditional_generated_hold_remains_even_for_new_receipt():
    from agent.drafter import Draft, DraftStatus, generated_hold_reason
    d = Draft("synthetic", TENANT, "instagram", "caption", [], "a.png", URL, "", DraftStatus.PENDING,
        creative_origin="generated", generated_artifact_version_id=VERSION,
        generated_artifact_sha256=SHA, generated_receipt=RECEIPT)
    assert "no trusted hosted-byte receipt authority" in generated_hold_reason(d)
