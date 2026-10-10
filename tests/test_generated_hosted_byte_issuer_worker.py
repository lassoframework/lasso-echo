"""Offline issuer dispatch-adapter tests; no production credentials or objects."""
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from agent import generated_hosted_byte_authority as a
from agent import generated_hosted_byte_issuer_worker as w


TENANT = "gym-a"
VERSION = str(uuid.uuid4())
URL = "https://cdn.example/gym-a/generated/a.png"
DATA = b"SYNTHETIC exact image bytes"
SHA = hashlib.sha256(DATA).hexdigest()
MANIFEST = dict(gym_id=TENANT, artifact_version_id=VERSION, hosted_url=URL,
                delivered_sha256=SHA, engine="synthetic-test-only")
MANIFEST_BYTES = json.dumps(MANIFEST, separators=(",", ":")).encode()
RECEIPT = dict(receipt_id=str(uuid.uuid4()), gym_id=TENANT, artifact_version_id=VERSION,
               hosted_url=URL, delivered_sha256=SHA, render_manifest=MANIFEST)

# Second frozen identity for the same tenant+version (changed URL/SHA/manifest).
URL2 = "https://cdn.example/gym-a/generated/b.png"
DATA2 = b"SYNTHETIC other image bytes"
SHA2 = hashlib.sha256(DATA2).hexdigest()
MANIFEST2 = dict(gym_id=TENANT, artifact_version_id=VERSION, hosted_url=URL2,
                 delivered_sha256=SHA2, engine="synthetic-test-only")
MANIFEST2_BYTES = json.dumps(MANIFEST2, separators=(",", ":")).encode()


class SyntheticReader(a.HostedObjectReader):
    def __init__(self, events, data=DATA, error=None):
        super().__init__({TENANT: ["https://cdn.example/gym-a/generated/"]})
        self.events, self.data, self.error = events, data, error

    def read(self, tenant, url):
        self.events.append("GET")
        if self.error:
            raise self.error
        return self.data


class Connection:
    def __init__(self, events, *, receipt=RECEIPT, authorized=True, commit_error=False,
                 reconcile_receipt="SAME"):
        self.events, self.receipt, self.authorized = events, receipt, authorized
        self.reconcile_receipt = receipt if reconcile_receipt == "SAME" else reconcile_receipt
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
            value = self.reconcile_receipt if "reconcile" in sql else self.receipt
        return SimpleNamespace(fetchone=lambda: (value,))

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")

    def commit(self):
        self.events.append("commit")
        if self.commit_error:
            raise RuntimeError("sensitive database error DSN=postgres://secret")


class MemoryLedger(w.DispatchLedger):
    """In-memory TEST DOUBLE only; not durable and never production-safe."""
    def __init__(self):
        self.rows = {}

    def claim(self, key, binding):
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = dict(binding=binding, receipt=None)
            return w.LedgerClaim(is_new=True, binding_digest=binding)
        return w.LedgerClaim(is_new=False, binding_digest=row["binding"],
                             receipt_id=row["receipt"])

    def commit(self, key, binding, receipt_id):
        row = self.rows[key]
        assert row["binding"] == binding and row["receipt"] in (None, receipt_id)
        row["receipt"] = receipt_id


def make_request(**over):
    args = dict(tenant=TENANT, artifact_version_id=VERSION, hosted_url=URL,
                expected_sha256=SHA, manifest_bytes=MANIFEST_BYTES)
    args.update(over)
    return w.IssuerDispatchRequest(**args)


def make_worker(events, *, ledger=None, conn_kw=None, reader=None):
    conn_kw = conn_kw or {}
    authority = a.GeneratedHostedByteAuthority(
        lambda: Connection(events, **conn_kw), tenant_id=TENANT,
        reader=reader or SyntheticReader(events))
    return w.HostedByteIssuerWorker(authority=authority,
                                    ledger=ledger if ledger is not None else MemoryLedger())


@pytest.fixture(autouse=True)
def flag_on(monkeypatch):
    monkeypatch.setenv(w.FLAG, "1")


def hold_code(excinfo):
    assert isinstance(excinfo.value, w.IssuerDispatchHold)
    return str(excinfo.value)


def test_off_means_no_issuance(monkeypatch):
    monkeypatch.delenv(w.FLAG)
    events, ledger = [], MemoryLedger()
    worker = make_worker(events, ledger=ledger)
    with pytest.raises(w.IssuerDispatchHold) as e:
        worker.dispatch(make_request(), authenticated_tenant=TENANT)
    assert hold_code(e) == "generated_issuer_off"
    assert events == [] and ledger.rows == {}


def test_unprovisioned_fails_closed():
    events = []
    readerless = a.GeneratedHostedByteAuthority(lambda: Connection(events), tenant_id=TENANT)
    with pytest.raises(w.IssuerDispatchHold) as e:
        w.HostedByteIssuerWorker(authority=readerless, ledger=MemoryLedger())
    assert hold_code(e) == "generated_issuer_authority_required"
    authority = a.GeneratedHostedByteAuthority(lambda: Connection(events), tenant_id=TENANT,
                                               reader=SyntheticReader(events))
    with pytest.raises(w.IssuerDispatchHold) as e:
        w.HostedByteIssuerWorker(authority=authority, ledger={})
    assert hold_code(e) == "generated_issuer_durable_ledger_required"


def test_malformed_requests_hold_before_ledger():
    bad = [dict(artifact_version_id="not-a-uuid"),
           dict(expected_sha256="zz"),
           dict(hosted_url="http://cdn.example/gym-a/generated/a.png"),
           dict(hosted_url=URL, manifest_bytes=b"{}"),
           dict(manifest_bytes="not-bytes"),
           dict(tenant=" gym-a")]
    for over in bad:
        with pytest.raises(w.IssuerDispatchHold):
            make_request(**over)


def test_cross_tenant_transport_identity_holds():
    events, ledger = [], MemoryLedger()
    worker = make_worker(events, ledger=ledger)
    with pytest.raises(w.IssuerDispatchHold) as e:
        worker.dispatch(make_request(), authenticated_tenant="gym-b")
    assert hold_code(e) == "generated_issuer_cross_tenant"
    assert events == [] and ledger.rows == {}


def test_issue_success_returns_only_receipt_and_status():
    events = []
    result = make_worker(events).dispatch(make_request(), authenticated_tenant=TENANT)
    assert result == {"receipt_id": RECEIPT["receipt_id"], "status": "issued"}
    assert events.count("issue") == 1 and events.count("GET") == 1


def test_replay_returns_one_receipt_without_reissue():
    events = []
    worker = make_worker(events)
    first = worker.dispatch(make_request(), authenticated_tenant=TENANT)
    second = worker.dispatch(make_request(), authenticated_tenant=TENANT)
    assert first["receipt_id"] == second["receipt_id"]
    assert second["status"] == "replayed"
    assert events.count("issue") == 1 and events.count("GET") == 1


def test_lost_commit_ack_reconciles_same_identity():
    events = []
    result = make_worker(events, conn_kw=dict(commit_error=True)).dispatch(
        make_request(), authenticated_tenant=TENANT)
    assert result == {"receipt_id": RECEIPT["receipt_id"], "status": "reconciled"}
    assert events.count("issue") == 1 and events.count("reconcile") == 1


def test_uncertain_then_absent_reconcile_holds_and_never_reissues():
    events = []
    worker = make_worker(events, conn_kw=dict(commit_error=True, reconcile_receipt=None))
    for _ in range(2):
        with pytest.raises(w.IssuerDispatchHold) as e:
            worker.dispatch(make_request(), authenticated_tenant=TENANT)
        assert hold_code(e) == "generated_issuer_reconcile_unavailable"
    assert events.count("issue") == 1  # replay reconciles, never issues blindly
    assert events.count("reconcile") == 2 and events.count("GET") == 1


def test_conflicting_replay_with_changed_manifest_holds():
    events = []
    worker = make_worker(events)
    worker.dispatch(make_request(), authenticated_tenant=TENANT)
    with pytest.raises(w.IssuerDispatchHold) as e:
        worker.dispatch(make_request(hosted_url=URL2, expected_sha256=SHA2,
                                     manifest_bytes=MANIFEST2_BYTES),
                        authenticated_tenant=TENANT)
    assert hold_code(e) == "generated_issuer_binding_conflict"
    assert events.count("issue") == 1


def test_changed_bytes_hold_without_issue():
    events = []
    reader = SyntheticReader(events, data=b"tampered bytes")
    worker = make_worker(events, reader=reader)
    with pytest.raises(w.IssuerDispatchHold) as e:
        worker.dispatch(make_request(), authenticated_tenant=TENANT)
    assert hold_code(e) == "generated_hosted_bytes_mismatch"
    assert "issue" not in events


def test_reader_scope_limits_preserved():
    events = []
    out_of_scope = "https://cdn.example/other-tenant/a.png"
    sha = hashlib.sha256(DATA).hexdigest()
    manifest = json.dumps(dict(gym_id=TENANT, artifact_version_id=VERSION,
                               hosted_url=out_of_scope, delivered_sha256=sha),
                          separators=(",", ":")).encode()
    reader = SyntheticReader(events, error=a.HostedByteHold(
        "generated_hosted_url_outside_tenant_scope"))
    worker = make_worker(events, reader=reader)
    with pytest.raises(w.IssuerDispatchHold) as e:
        worker.dispatch(make_request(hosted_url=out_of_scope, manifest_bytes=manifest),
                        authenticated_tenant=TENANT)
    assert hold_code(e) == "generated_hosted_url_outside_tenant_scope"
    assert "issue" not in events


def test_no_secret_or_exception_leak_in_holds():
    events = []
    worker = make_worker(events, conn_kw=dict(commit_error=True, reconcile_receipt=None))
    with pytest.raises(w.IssuerDispatchHold) as e:
        worker.dispatch(make_request(), authenticated_tenant=TENANT)
    message = str(e.value)
    assert message == "generated_issuer_reconcile_unavailable"
    for leak in ("secret", "DSN", "postgres", "sensitive", "RuntimeError", URL, SHA):
        assert leak not in message


def test_ledger_exceptions_and_bad_stored_receipt_are_static_holds():
    class LeakyLedger(MemoryLedger):
        def claim(self, key, binding):
            raise w.IssuerDispatchHold("DSN=postgres://secret")

    events = []
    with pytest.raises(w.IssuerDispatchHold) as error:
        make_worker(events, ledger=LeakyLedger()).dispatch(
            make_request(), authenticated_tenant=TENANT)
    assert hold_code(error) == "generated_issuer_ledger_unavailable"
    assert events == []

    ledger = MemoryLedger()
    key = w.dispatch_key(TENANT, VERSION)
    ledger.rows[key] = dict(binding=make_request().binding_digest(), receipt="bad UUID")
    with pytest.raises(w.IssuerDispatchHold) as error:
        make_worker(events, ledger=ledger).dispatch(make_request(), authenticated_tenant=TENANT)
    assert hold_code(error) == "generated_issuer_receipt_invalid"
    assert events == []


def test_lost_ledger_commit_ack_replays_without_reissue():
    class LostAckLedger(MemoryLedger):
        def __init__(self):
            super().__init__()
            self.lose_once = True

        def commit(self, key, binding, receipt_id):
            super().commit(key, binding, receipt_id)
            if self.lose_once:
                self.lose_once = False
                raise RuntimeError("sensitive ledger ACK")

    events, ledger = [], LostAckLedger()
    with pytest.raises(w.IssuerDispatchHold) as error:
        make_worker(events, ledger=ledger).dispatch(make_request(), authenticated_tenant=TENANT)
    assert hold_code(error) == "generated_issuer_ledger_unavailable"
    result = make_worker(events, ledger=ledger).dispatch(make_request(), authenticated_tenant=TENANT)
    assert result == {"receipt_id": RECEIPT["receipt_id"], "status": "replayed"}
    assert events.count("issue") == 1


def test_failed_ledger_commit_reconciles_after_worker_reconstruction():
    class FailedCommitLedger(MemoryLedger):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def commit(self, key, binding, receipt_id):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("sensitive ledger failure")
            super().commit(key, binding, receipt_id)

    events, ledger = [], FailedCommitLedger()
    with pytest.raises(w.IssuerDispatchHold) as error:
        make_worker(events, ledger=ledger).dispatch(make_request(), authenticated_tenant=TENANT)
    assert hold_code(error) == "generated_issuer_ledger_unavailable"
    result = make_worker(events, ledger=ledger).dispatch(make_request(), authenticated_tenant=TENANT)
    assert result == {"receipt_id": RECEIPT["receipt_id"], "status": "reconciled"}
    assert events.count("issue") == 1 and events.count("reconcile") == 1
