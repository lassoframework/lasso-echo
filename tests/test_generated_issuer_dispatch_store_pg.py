"""Disposable PG17 queue/ledger checks for the generated issuer dispatch store.

Covers the frozen shared API: concurrent claim race, restricted role matrix,
conflicting replay, commit/rollback uncertainty, tenant spoofing and grant
revocation. No live database, no DSN/env discovery; follows the existing
local PG17 disposable-database convention.
"""
import hashlib
import json
from pathlib import Path
import random
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

from agent import generated_hosted_byte_authority as a
from agent import generated_hosted_byte_issuer_worker as w
from agent.generated_issuer_dispatch_store import (
    GeneratedIssuerDispatchHold,
    GeneratedIssuerDispatchStore,
)


ROOT = Path(__file__).resolve().parents[1]
PG = Path("/opt/homebrew/opt/postgresql@17/bin")
AUTHORITY_MIGRATION = ROOT / "migrations/DRAFT_generated_hosted_byte_authority_20261009.sql"
MIGRATION = ROOT / "migrations/DRAFT_generated_issuer_dispatch_20261009.sql"
VERIFY = ROOT / "migrations/DRAFT_generated_issuer_dispatch_20261009.verify.sql"
BOOTSTRAP = """
create role anon login; create role authenticated login; create role service_role login bypassrls;
alter default privileges in schema public grant all on tables to public,anon,authenticated,service_role;
alter default privileges in schema public grant all on functions to public,anon,authenticated,service_role;
create table public.calendar_generated_artifact_versions (
 id uuid primary key, gym_id text not null, image_url text not null,
 delivered_sha256 text not null check(delivered_sha256 ~ '^[0-9a-f]{64}$'),
 render_manifest_digest text not null check(render_manifest_digest ~ '^[0-9a-f]{64}$'),
 delivery_receipt jsonb not null check(jsonb_typeof(delivery_receipt)='object'),
 verified_at timestamptz not null default now(), check(btrim(gym_id)<>'' and btrim(image_url)<>'')
);
alter table public.calendar_generated_artifact_versions enable row level security;
create function public.calendar_generated_version_immutable() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin raise exception 'generated artifact versions are immutable' using errcode='23514'; end $$;
create trigger generated_version_immutable before update or delete on public.calendar_generated_artifact_versions
 for each row execute function public.calendar_generated_version_immutable();
create trigger generated_version_no_truncate before truncate on public.calendar_generated_artifact_versions
 for each statement execute function public.calendar_generated_version_immutable();
"""


@pytest.fixture(scope="module")
def local_pg():
    psycopg = pytest.importorskip("psycopg")
    if not all((PG / name).is_file() for name in ("initdb", "pg_ctl")):
        pytest.skip("existing PG17 binaries unavailable; no install")
    with tempfile.TemporaryDirectory(prefix="issuer_dispatch_pg_", dir="/tmp") as directory:
        root = Path(directory)
        sock = root / "sock"
        sock.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(PG / "initdb"), "-D", str(root / "data"), "-U", "postgres", "--no-sync"],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([str(PG / "pg_ctl"), "-D", str(root / "data"), "-l", str(root / "pg.log"),
                        "-o", f"-k {sock} -p {port} -c listen_addresses=''", "-w", "start"],
                       check=True, capture_output=True, timeout=60)
        connections = []
        try:
            def connect(role="postgres", autocommit=True):
                c = psycopg.connect(host=str(sock), port=port, dbname="postgres", user=role,
                                    autocommit=autocommit)
                connections.append(c)
                return c
            admin = connect()
            admin.execute(BOOTSTRAP)
            admin.execute(AUTHORITY_MIGRATION.read_text())
            admin.execute(MIGRATION.read_text())
            admin.execute(VERIFY.read_text())
            admin.execute("create role producer_a login; create role issuer_a login; create role issuer_b login; "
                          "grant generated_issuer_dispatch_producer_20261009 to producer_a; "
                          "grant generated_issuer_dispatch_issuer_20261009 to issuer_a,issuer_b; "
                          "grant generated_hosted_byte_issuer_20261009 to issuer_a,issuer_b;")
            admin.execute("insert into public.generated_issuer_dispatch_principals_20261009 values "
                          "('producer_a','gym-a',true,false),('issuer_a','gym-a',false,true),"
                          "('issuer_b','gym-b',false,true)")
            admin.execute("insert into public.generated_hosted_byte_principals_20261009 values "
                          "('issuer_a','gym-a',true,true),('issuer_b','gym-b',true,true)")
            yield admin, connect, psycopg
        finally:
            for c in connections:
                c.close()
            subprocess.run([str(PG / "pg_ctl"), "-D", str(root / "data"), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True, timeout=60)


def request(tenant="gym-a", version=None, url=None, data=b"SYNTHETIC exact delivered image"):
    version = version or str(uuid.uuid4())
    url = url or f"https://cdn.example/{tenant}/generated/{version}.png"
    sha = hashlib.sha256(data).hexdigest()
    manifest = dict(gym_id=tenant, artifact_version_id=version, hosted_url=url,
                    delivered_sha256=sha, engine="SYNTHETIC test", operation="same_object")
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    return w.IssuerDispatchRequest(tenant=tenant, artifact_version_id=version,
                                   hosted_url=url, expected_sha256=sha, manifest_bytes=raw)


def store(connect, role):
    return GeneratedIssuerDispatchStore(lambda: connect(role))


def issue_receipt(connect, req, data=b"SYNTHETIC exact delivered image"):
    """Commit a real authority receipt for req through the trusted issuer path."""

    class Reader(a.HostedObjectReader):
        def __init__(self):
            super().__init__({req.tenant: [f"https://cdn.example/{req.tenant}/generated/"]})

        def read(self, tenant, url):
            return data

    authority = a.GeneratedHostedByteAuthority(lambda: connect("issuer_a", False),
                                               tenant_id=req.tenant, reader=Reader())
    return authority.issue(artifact_version_id=req.artifact_version_id,
                           hosted_url=req.hosted_url,
                           expected_sha256=req.expected_sha256,
                           manifest_bytes=req.manifest_bytes)["receipt_id"]


def denied(fn, code):
    with pytest.raises(GeneratedIssuerDispatchHold, match=code):
        fn()


def test_submit_result_roundtrip_and_identical_replay(local_pg):
    _, connect, _ = local_pg
    producer = store(connect, "producer_a")
    req = request()
    submitted = producer.submit(req)
    assert submitted == {"dispatch_key": w.dispatch_key(req.tenant, req.artifact_version_id),
                         "status": "submitted", "receipt_id": None}
    assert producer.submit(req) == submitted
    assert producer.result(req.tenant, req.artifact_version_id, req.binding_digest()) == {
        "status": "submitted", "receipt_id": None}
    assert producer.result(req.tenant, str(uuid.uuid4()), req.binding_digest()) is None


def test_two_issuer_processes_racing_one_key_get_exactly_one_new_claim(local_pg):
    admin, connect, _ = local_pg
    req = request()
    store(connect, "producer_a").submit(req)
    key = w.dispatch_key(req.tenant, req.artifact_version_id)
    binding = req.binding_digest()
    s1, s2 = store(connect, "issuer_a"), store(connect, "issuer_a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [f.result(timeout=15) for f in
                  (pool.submit(s1.claim, key, binding), pool.submit(s2.claim, key, binding))]
    assert sum(c.is_new for c in claims) == 1
    assert all(c.binding_digest == binding and c.receipt_id is None for c in claims)
    assert admin.execute("select count(*) from public.generated_issuer_dispatch_ledger_20261009"
                         " where dispatch_key=%s", (key,)).fetchone()[0] == 1


def test_role_matrix_producer_cannot_issue_issuer_cannot_submit(local_pg):
    _, connect, _ = local_pg
    producer, issuer = store(connect, "producer_a"), store(connect, "issuer_a")
    req = request()
    producer.submit(req)
    key, binding = w.dispatch_key(req.tenant, req.artifact_version_id), req.binding_digest()
    # Producer: no pending-read, claim, commit or grant write.
    denied(lambda: producer.pending(["gym-a"], 10), "pending_rejected")
    denied(lambda: producer.claim(key, binding), "claim_rejected")
    denied(lambda: producer.commit(key, binding, str(uuid.uuid4())), "commit_rejected")
    denied(lambda: store(connect, "producer_a").submit(request(tenant="gym-b")), "submit_rejected")
    # Issuer: no submit or result read; pending/claim/commit work.
    denied(lambda: issuer.submit(request()), "submit_rejected")
    denied(lambda: issuer.result(req.tenant, req.artifact_version_id, binding), "result_rejected")
    pending = issuer.pending(["gym-a"], 10)
    assert any(r.artifact_version_id == req.artifact_version_id for r in pending)
    assert all(type(r) is w.IssuerDispatchRequest for r in pending)
    assert issuer.claim(key, binding).is_new is True
    receipt = issue_receipt(connect, req)
    assert issuer.commit(key, binding, receipt) is True
    assert issuer.commit(key, binding, receipt) is True  # idempotent same receipt
    # Admin-only grant dictionary is closed to both restricted roles.
    for role in ("producer_a", "issuer_a"):
        with pytest.raises(Exception):
            connect(role).execute("insert into public.generated_issuer_dispatch_principals_20261009"
                                  " values (%s,'gym-b',true,true)", (role,))
    # Cross-tenant issuer cannot touch gym-a dispatches.
    denied(lambda: store(connect, "issuer_b").claim(key, binding), "claim_rejected")
    denied(lambda: store(connect, "issuer_b").pending(["gym-a"], 10), "pending_rejected")


def test_conflicting_replay_same_version_changed_binding_never_second_issue(local_pg):
    admin, connect, _ = local_pg
    req = request()
    store(connect, "producer_a").submit(req)
    issuer = store(connect, "issuer_a")
    key = w.dispatch_key(req.tenant, req.artifact_version_id)
    assert issuer.claim(key, req.binding_digest()).is_new is True
    # Same tenant+version, changed URL: submit conflicts and the ledger keeps
    # the original committed binding; no second claim row can be captured.
    changed = request(version=req.artifact_version_id,
                      url=f"https://cdn.example/gym-a/generated/{uuid.uuid4()}.png")
    denied(lambda: store(connect, "producer_a").submit(changed), "binding_conflict")
    replay = issuer.claim(key, changed.binding_digest())
    assert replay.is_new is False and replay.binding_digest == req.binding_digest()
    assert replay.receipt_id is None
    assert admin.execute("select count(*) from public.generated_issuer_dispatch_requests_20261009"
                         " where dispatch_key=%s", (key,)).fetchone()[0] == 1


def test_crash_between_claim_and_commit_then_exact_commit_rules(local_pg):
    admin, connect, _ = local_pg
    req = request()
    store(connect, "producer_a").submit(req)
    issuer = store(connect, "issuer_a")
    key, binding = w.dispatch_key(req.tenant, req.artifact_version_id), req.binding_digest()
    first = issuer.claim(key, binding)
    assert first.is_new is True
    # The claiming process died before commit: the durable row exists with
    # receipt NULL and a later claim is not new and carries no receipt.
    later = issuer.claim(key, binding)
    assert later.is_new is False and later.receipt_id is None
    assert admin.execute("select receipt_id from public.generated_issuer_dispatch_ledger_20261009"
                         " where dispatch_key=%s", (key,)).fetchone()[0] is None
    # Commit rejects a missing key, a binding mismatch and a second receipt.
    denied(lambda: issuer.commit("f" * 64, binding, str(uuid.uuid4())), "commit_rejected")
    denied(lambda: issuer.commit(key, "f" * 64, str(uuid.uuid4())), "commit_rejected")
    receipt = issue_receipt(connect, req)
    assert issuer.commit(key, binding, receipt) is True
    denied(lambda: issuer.commit(key, binding, str(uuid.uuid4())), "commit_rejected")
    assert issuer.commit(key, binding, receipt) is True
    # Direct table writes are impossible for the restricted roles and admin.
    with pytest.raises(Exception):
        connect("issuer_a").execute("update public.generated_issuer_dispatch_ledger_20261009"
                                    " set receipt_id=%s where dispatch_key=%s", (uuid.uuid4(), key))
    with pytest.raises(Exception):
        admin.execute("update public.generated_issuer_dispatch_ledger_20261009"
                      " set binding_digest=%s where dispatch_key=%s", ("f" * 64, key))


def test_tenant_spoof_in_request_content_rejected_against_session_user(local_pg):
    _, connect, _ = local_pg
    producer = store(connect, "producer_a")
    # producer_a holds a gym-a grant only; request content claiming gym-b is
    # rejected by SQL authorization on session_user, not by the content.
    denied(lambda: producer.submit(request(tenant="gym-b")), "submit_rejected")
    denied(lambda: producer.result("gym-b", str(uuid.uuid4()), "f" * 64), "result_rejected")
    denied(lambda: store(connect, "issuer_a").pending(["gym-b"], 10), "pending_rejected")


def test_revoked_tenant_grant_blocks_submit_claim_and_commit(local_pg):
    admin, connect, _ = local_pg
    req = request()
    producer, issuer = store(connect, "producer_a"), store(connect, "issuer_a")
    producer.submit(req)
    key, binding = w.dispatch_key(req.tenant, req.artifact_version_id), req.binding_digest()
    admin.execute("update public.generated_issuer_dispatch_principals_20261009"
                  " set can_dispatch=false where principal='issuer_a'")
    try:
        denied(lambda: issuer.claim(key, binding), "claim_rejected")
        denied(lambda: issuer.pending(["gym-a"], 10), "pending_rejected")
    finally:
        admin.execute("update public.generated_issuer_dispatch_principals_20261009"
                      " set can_dispatch=true where principal='issuer_a'")
    assert issuer.claim(key, binding).is_new is True
    admin.execute("update public.generated_issuer_dispatch_principals_20261009"
                  " set can_submit=false where principal='producer_a'")
    try:
        denied(lambda: producer.submit(request()), "submit_rejected")
        denied(lambda: producer.result(req.tenant, req.artifact_version_id, binding), "result_rejected")
    finally:
        admin.execute("update public.generated_issuer_dispatch_principals_20261009"
                      " set can_submit=true where principal='producer_a'")


def test_end_to_end_worker_issue_replay_conflict_and_reconcile(local_pg, monkeypatch):
    admin, connect, _ = local_pg
    monkeypatch.setenv(w.FLAG, "true")
    req = request()
    data = b"SYNTHETIC exact delivered image"

    class Reader(a.HostedObjectReader):
        def __init__(self):
            super().__init__({"gym-a": ["https://cdn.example/gym-a/generated/"]})

        def read(self, tenant, url):
            return data

    authority = a.GeneratedHostedByteAuthority(lambda: connect("issuer_a", False),
                                               tenant_id="gym-a", reader=Reader())
    producer, ledger = store(connect, "producer_a"), store(connect, "issuer_a")
    worker = w.HostedByteIssuerWorker(authority=authority, ledger=ledger)
    producer.submit(req)
    issued = worker.dispatch(req, authenticated_tenant="gym-a")
    assert issued["status"] == "issued"
    assert producer.result(req.tenant, req.artifact_version_id,
                           req.binding_digest()) == {"status": "issued",
                                                     "receipt_id": issued["receipt_id"]}
    assert worker.dispatch(req, authenticated_tenant="gym-a") == {
        "receipt_id": issued["receipt_id"], "status": "replayed"}
    # Changed binding on the same tenant+version is a conflict, no re-issue.
    changed = request(version=req.artifact_version_id,
                      url=f"https://cdn.example/gym-a/generated/{uuid.uuid4()}.png")
    with pytest.raises(w.IssuerDispatchHold, match="binding_conflict"):
        worker.dispatch(changed, authenticated_tenant="gym-a")
    assert admin.execute("select count(*) from public.generated_hosted_byte_receipts_20261009"
                         " where artifact_version_id=%s",
                         (req.artifact_version_id,)).fetchone()[0] == 1
    # Crash between claim and commit: the worker reconciles instead of
    # issuing again and holds when the authority has no committed receipt.
    lost = request()
    producer.submit(lost)
    lost_key = w.dispatch_key(lost.tenant, lost.artifact_version_id)
    assert ledger.claim(lost_key, lost.binding_digest()).is_new is True
    with pytest.raises(w.IssuerDispatchHold, match="reconcile_unavailable"):
        worker.dispatch(lost, authenticated_tenant="gym-a")
    assert admin.execute("select count(*) from public.generated_hosted_byte_receipts_20261009"
                         " where artifact_version_id=%s",
                         (lost.artifact_version_id,)).fetchone()[0] == 0


def test_commit_rejects_receipt_not_committed_by_authority(local_pg):
    _, connect, _ = local_pg
    req = request()
    store(connect, "producer_a").submit(req)
    issuer = store(connect, "issuer_a")
    key, binding = w.dispatch_key(req.tenant, req.artifact_version_id), req.binding_digest()
    assert issuer.claim(key, binding).is_new is True
    # A fabricated UUID is not a committed authority receipt: fail closed.
    denied(lambda: issuer.commit(key, binding, str(uuid.uuid4())), "commit_rejected")
    # A real receipt committed for a DIFFERENT version is foreign to this
    # exact binding and must also be rejected.
    other = request()
    store(connect, "producer_a").submit(other)
    foreign = issue_receipt(connect, other)
    denied(lambda: issuer.commit(key, binding, foreign), "commit_rejected")
    # The ledger row stays uncommitted and the exact real receipt commits.
    assert issuer.claim(key, binding).receipt_id is None
    receipt = issue_receipt(connect, req)
    assert issuer.commit(key, binding, receipt) is True


def test_pending_excludes_issued_rows(local_pg):
    _, connect, _ = local_pg
    producer, issuer = store(connect, "producer_a"), store(connect, "issuer_a")
    keep, done = request(), request()
    producer.submit(keep)
    producer.submit(done)
    key = w.dispatch_key(done.tenant, done.artifact_version_id)
    assert issuer.claim(key, done.binding_digest()).is_new is True
    assert issuer.commit(key, done.binding_digest(), issue_receipt(connect, done)) is True
    pending = issuer.pending(["gym-a"], 10)
    versions = {r.artifact_version_id for r in pending}
    assert keep.artifact_version_id in versions
    assert done.artifact_version_id not in versions
    # Stable across repeated reads: an issued row never reappears.
    again = issuer.pending(["gym-a"], 10)
    assert done.artifact_version_id not in {r.artifact_version_id for r in again}


def test_sql_submit_binding_digest_matches_python_and_rejects_drift(local_pg):
    admin, connect, psycopg = local_pg
    req = request(url=f"https://cdn.example/gym-a/generated/q\"uote{uuid.uuid4()}.png")
    manifest_sha = hashlib.sha256(req.manifest_bytes).hexdigest()
    # The SQL recomputation of the canonical binding digest equals the frozen
    # Python digest byte for byte, including JSON string escaping in the URL.
    computed = admin.execute(
        "select encode(sha256(convert_to("
        " '{\"expected_sha256\":'||to_json(%s::text)::text||',\"hosted_url\":'||to_json(%s::text)::text"
        " ||',\"manifest_sha256\":'||to_json(%s::text)::text||'}','UTF8')),'hex')",
        (req.expected_sha256, req.hosted_url, manifest_sha)).fetchone()[0]
    assert computed == req.binding_digest()
    # Submit with the exact digest commits the row; a drifted caller-supplied
    # binding for the same content is rejected by SQL, never stored.
    store(connect, "producer_a").submit(req)
    key = w.dispatch_key(req.tenant, req.artifact_version_id)
    drifted = w._binding_digest(req.hosted_url + "x", req.expected_sha256, req.manifest_bytes)
    with pytest.raises(psycopg.Error):
        connect("producer_a").execute(
            "select public.generated_issuer_dispatch_submit_20261009(%s,%s,%s,%s,%s,%s,%s,%s)",
            (req.tenant, req.artifact_version_id, req.hosted_url, req.expected_sha256,
             manifest_sha, req.manifest_bytes, drifted, key))


def test_sql_submit_rejects_duplicate_key_and_unsafe_url(local_pg):
    _, connect, psycopg = local_pg
    req = request()
    store(connect, "producer_a").submit(req)
    key = w.dispatch_key(req.tenant, req.artifact_version_id)
    # Same tenant+version with a changed URL is a duplicate dispatch key and
    # a binding conflict, never a second stored row.
    changed = request(version=req.artifact_version_id,
                      url=f"https://cdn.example/gym-a/generated/{uuid.uuid4()}.png")
    denied(lambda: store(connect, "producer_a").submit(changed), "binding_conflict")
    # URLs outside the frozen Python _url() rules are rejected at submit by
    # SQL itself. Python request validation would also refuse these, so call
    # the SQL function directly as the producer to prove the poison row is
    # never stored then read back.
    for bad in ("https://cdn.example/gym-a//double.png",
                "https://cdn.example/gym-a/../up.png",
                "https://cdn.example/gym-a/generated/%2e%2e.png",
                "https://cdn.example/gym-a/generated/back\\slash.png"):
        data = b"SYNTHETIC exact delivered image"
        sha = hashlib.sha256(data).hexdigest()
        manifest = dict(gym_id="gym-a", artifact_version_id=str(uuid.uuid4()),
                        hosted_url=bad, delivered_sha256=sha)
        raw = json.dumps(manifest, separators=(",", ":")).encode()
        digest = w._binding_digest(bad, sha, raw)
        with pytest.raises(psycopg.Error):
            connect("producer_a").execute(
                "select public.generated_issuer_dispatch_submit_20261009(%s,%s,%s,%s,%s,%s,%s,%s)",
                ("gym-a", manifest["artifact_version_id"], bad, sha,
                 hashlib.sha256(raw).hexdigest(), raw, digest,
                 w.dispatch_key("gym-a", manifest["artifact_version_id"])))


def test_pending_accepts_frozenset_and_any_iterable(local_pg):
    _, connect, _ = local_pg
    producer, issuer = store(connect, "producer_a"), store(connect, "issuer_a")
    req = request()
    producer.submit(req)
    # The issuer job passes a frozenset; generators and sets normalize too.
    for tenants in (frozenset({"gym-a"}), {"gym-a"}, (t for t in ("gym-a",)),
                    ["gym-a", "gym-a"]):
        pending = issuer.pending(tenants, 10)
        assert any(r.artifact_version_id == req.artifact_version_id for r in pending)
    # A bare string is a tenant, not an iterable of tenants: fail closed.
    denied(lambda: issuer.pending("gym-a", 10), "pending_invalid")
    denied(lambda: issuer.pending(123, 10), "pending_invalid")
    denied(lambda: issuer.pending(frozenset(), 10), "pending_invalid")
    denied(lambda: issuer.pending(frozenset({"gym-a", " "}), 10), "pending_invalid")


def grant_tenant(admin, tenant):
    """Authorize producer_a and issuer_a for one unique test tenant."""
    admin.execute("insert into public.generated_issuer_dispatch_principals_20261009 values"
                  " (%s,%s,true,false),(%s,%s,false,true)",
                  ("producer_a", tenant, "issuer_a", tenant))


def unique_tenant(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def direct_submit(connect, tenant, version, url, sha, raw):
    return connect("producer_a").execute(
        "select public.generated_issuer_dispatch_submit_20261009(%s,%s,%s,%s,%s,%s,%s,%s)",
        (tenant, version, url, sha, hashlib.sha256(raw).hexdigest(), raw,
         w._binding_digest(url, sha, raw), w.dispatch_key(tenant, version)))


def test_sql_submit_rejects_duplicate_key_manifest_and_pending_survives(local_pg):
    admin, connect, psycopg = local_pg
    # Unique authorized tenant: this test is isolated from every other row in
    # the module-scoped fixture queue.
    tenant = unique_tenant("gym-dup")
    grant_tenant(admin, tenant)
    producer = store(connect, "producer_a")
    good = request(tenant=tenant)
    producer.submit(good)
    # A manifest with duplicate object keys is valid to jsonb (silently
    # collapsed) but rejected by the frozen Python validation; the native
    # PG17 IS JSON ... WITH UNIQUE KEYS guard must refuse it at submit so the
    # poison row is never stored and can never abort a pending batch. Python
    # request validation also refuses these, so call SQL directly.
    data = b"SYNTHETIC exact delivered image"
    sha = hashlib.sha256(data).hexdigest()

    def raw_of(template, version, url):
        return (template % (tenant, version, url, sha)).encode()

    dup_cases = [
        # Top-level duplicate key.
        ('{"gym_id":"%s","gym_id":"%s","artifact_version_id":"%s",'
         '"hosted_url":"%s","delivered_sha256":"%s"}', ("gym_id",)),
        # Nested duplicate key at a deeper object depth.
        ('{"gym_id":"%s","artifact_version_id":"%s","hosted_url":"%s",'
         '"delivered_sha256":"%s","meta":{"a":1,"a":2}}', None),
        # Duplicate spelled with an escaped key: "a" and "\\u0061" collide.
        ('{"gym_id":"%s","artifact_version_id":"%s","hosted_url":"%s",'
         '"delivered_sha256":"%s","a":1,"\\u0061":2}', None),
        # Duplicate inside an object nested in an array.
        ('{"gym_id":"%s","artifact_version_id":"%s","hosted_url":"%s",'
         '"delivered_sha256":"%s","items":[{"k":1,"k":2}]}', None),
    ]
    poison_versions = []
    for template, top in dup_cases:
        version = str(uuid.uuid4())
        poison_versions.append(version)
        url = f"https://cdn.example/{tenant}/generated/{version}.png"
        if top:
            raw = (template % (tenant, tenant, version, url, sha)).encode()
        else:
            raw = raw_of(template, version, url)
        with pytest.raises(psycopg.Error):
            direct_submit(connect, tenant, version, url, sha, raw)
        assert admin.execute("select count(*) from public.generated_issuer_dispatch_requests_20261009"
                             " where artifact_version_id=%s", (version,)).fetchone()[0] == 0
    # Malformed JSON and non-object roots are rejected by the same guard.
    for label, build in (
            ("malformed", lambda v, u: ('{"gym_id":"%s","artifact_version_id":"%s",' % (tenant, v)).encode()),
            ("array root", lambda v, u: b'[{"gym_id":"%s"}]' % tenant.encode()),
            ("scalar root", lambda v, u: b'"just a string"')):
        version = str(uuid.uuid4())
        url = f"https://cdn.example/{tenant}/generated/{version}.png"
        with pytest.raises(psycopg.Error):
            direct_submit(connect, tenant, version, url, sha, build(version, url))
        assert admin.execute("select count(*) from public.generated_issuer_dispatch_requests_20261009"
                             " where artifact_version_id=%s", (version,)).fetchone()[0] == 0
    # No false positives: keys repeated at different depths or in sibling
    # objects, structural characters inside strings, booleans, null and
    # exponents are all accepted, with exact original bytes stored verbatim.
    version = str(uuid.uuid4())
    url = f"https://cdn.example/{tenant}/generated/{version}.png"
    raw = ('{"gym_id":"%s","artifact_version_id":"%s","hosted_url":"%s",'
           '"delivered_sha256":"%s","meta":{"gym_id":"nested-ok"},'
           '"items":[{"k":1},{"k":2}],"s":"{}:,","b":true,"n":null,"e":1.5e3}'
           % (tenant, version, url, sha)).encode()
    result = direct_submit(connect, tenant, version, url, sha, raw).fetchone()[0]
    assert result["safe_status"] == "submitted"
    stored = admin.execute("select manifest_bytes from public.generated_issuer_dispatch_requests_20261009"
                           " where artifact_version_id=%s", (version,)).fetchone()[0]
    assert bytes(stored) == raw
    # Isolated queue: exactly the two healthy rows, no poison.
    pending = store(connect, "issuer_a").pending(frozenset({tenant}), 10)
    assert {r.artifact_version_id for r in pending} == {good.artifact_version_id, version}
    assert not any(v in {r.artifact_version_id for r in pending} for v in poison_versions)


def stage_row(admin, raw, tenant=None, version=None, url=None):
    """Superuser insert simulating a legacy row SQL accepted (pre-guards).

    Identity defaults are parsed from the raw manifest itself, so the staged
    row's tenant, version and exact hosted URL always match its bytes; the
    Python-side manifest-identity check then isolates the decoder behavior
    under test instead of failing on a mismatched generated identity.
    """
    identity = json.loads(raw)
    tenant = tenant or identity["gym_id"]
    version = version or identity["artifact_version_id"]
    url = url or identity["hosted_url"]
    sha = hashlib.sha256(b"SYNTHETIC exact delivered image").hexdigest()
    binding = w._binding_digest(url, sha, raw)
    key = w.dispatch_key(tenant, version)
    admin.execute(
        "insert into public.generated_issuer_dispatch_requests_20261009"
        "(dispatch_key,gym_id,artifact_version_id,binding_digest,hosted_url,expected_sha256,"
        " manifest_sha256,manifest_bytes,submitted_by)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,'postgres')",
        (key, tenant, version, binding, url, sha, hashlib.sha256(raw).hexdigest(), raw))
    return key, binding, version


def manifest_with(tenant, extra):
    version = str(uuid.uuid4())
    url = f"https://cdn.example/{tenant}/generated/{version}.png"
    sha = hashlib.sha256(b"SYNTHETIC exact delivered image").hexdigest()
    raw = ('{"gym_id":"%s","artifact_version_id":"%s","hosted_url":"%s",'
           '"delivered_sha256":"%s"%s}' % (tenant, version, url, sha, extra)).encode()
    return raw, version


def test_pending_quarantines_poison_rows_and_never_starves(local_pg):
    admin, connect, psycopg = local_pg
    tenant = unique_tenant("gym-quar")
    grant_tenant(admin, tenant)
    producer, issuer = store(connect, "producer_a"), store(connect, "issuer_a")
    # Poison SQL jsonb accepts but frozen Python rejects, staged FIRST (so it
    # heads the queue) as superuser to simulate legacy rows predating the
    # submit-time rejections.
    float_poison, float_v = manifest_with(tenant, ',"oversized_float":1e400')
    exp_poison, exp_v = manifest_with(tenant, ',"oversized_exponent":1e100000')
    dup_poison, dup_v = manifest_with(tenant, ',"a":1,"a":2')
    # These legacy poison variants exercise per-row quarantine independently
    # of submit-time JSON and URL validation.
    for raw in (float_poison, exp_poison, dup_poison):
        stage_row(admin, raw)
    # Frozen Python accepts arbitrarily large ints, so this staged variant is
    # NOT poison: it must be returned, never quarantined.
    bigint_ok, bigint_v = manifest_with(tenant, ',"big_int":1' + '0' * 300)
    stage_row(admin, bigint_ok)
    # Healthy rows submitted BEHIND the poison in submitted_at order must
    # still be returned: quarantined poison never starves the queue.
    healthy = request(tenant=tenant)
    later = request(tenant=tenant)
    producer.submit(healthy)
    producer.submit(later)

    def quarantine_count():
        return admin.execute(
            "select count(*) from public.generated_issuer_dispatch_quarantine_20261009"
            " where gym_id=%s", (tenant,)).fetchone()[0]

    first = issuer.pending(frozenset({tenant}), 10)
    versions = {r.artifact_version_id for r in first}
    # Isolated tenant: exactly the healthy set, nothing else.
    assert versions == {healthy.artifact_version_id, later.artifact_version_id, bigint_v}
    assert quarantine_count() == 3
    # Idempotent: a second pending re-quarantines nothing and poison never
    # reappears.
    second = issuer.pending([tenant], 10)
    assert {r.artifact_version_id for r in second} == versions
    assert quarantine_count() == 3
    # Quarantine never issues, never resets the ledger, never touches
    # authority state.
    for version in (float_v, exp_v, dup_v):
        key = w.dispatch_key(tenant, version)
        assert admin.execute("select count(*) from public.generated_issuer_dispatch_ledger_20261009"
                             " where dispatch_key=%s", (key,)).fetchone()[0] == 0
        assert admin.execute("select count(*) from public.generated_hosted_byte_receipts_20261009"
                             " where artifact_version_id=%s", (version,)).fetchone()[0] == 0
    # Producer role has no quarantine RPC rights.
    key, binding, _ = stage_row(admin, manifest_with(tenant, ',"x":1')[0])
    with pytest.raises(psycopg.Error):
        connect("producer_a").execute(
            "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
            (key, binding, "generated_dispatch_request_invalid"))
    # Forged quarantine attempts are rejected: wrong binding for a real key,
    # an unknown key, and a cross-tenant issuer.
    with pytest.raises(psycopg.Error):
        connect("issuer_a").execute(
            "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
            (key, "f" * 64, "generated_dispatch_request_invalid"))
    with pytest.raises(psycopg.Error):
        connect("issuer_a").execute(
            "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
            ("e" * 64, "f" * 64, "generated_dispatch_request_invalid"))
    with pytest.raises(psycopg.Error):
        connect("issuer_b").execute(
            "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
            (key, binding, "generated_dispatch_request_invalid"))
    # The exact issuer quarantine succeeds and is idempotent.
    assert connect("issuer_a").execute(
        "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
        (key, binding, "generated_dispatch_request_invalid")).fetchone()[0] is True
    assert connect("issuer_a").execute(
        "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
        (key, binding, "generated_dispatch_request_invalid")).fetchone()[0] is True
    assert quarantine_count() == 4


def test_pending_limit_one_tick_quarantines_head_poison_then_returns_healthy(local_pg):
    admin, connect, _ = local_pg
    tenant = unique_tenant("gym-tick")
    grant_tenant(admin, tenant)
    producer, issuer = store(connect, "producer_a"), store(connect, "issuer_a")
    # Poison at the head of the queue, healthy behind it.
    poison, poison_v = manifest_with(tenant, ',"oversized_float":1e400')
    stage_row(admin, poison)
    healthy = request(tenant=tenant)
    producer.submit(healthy)
    # Tick one with the smallest bounded limit: the head poison row is
    # quarantined and skipped; nothing healthy was fetched in this tick.
    assert issuer.pending([tenant], 1) == []
    assert admin.execute("select count(*) from public.generated_issuer_dispatch_quarantine_20261009"
                         " where gym_id=%s", (tenant,)).fetchone()[0] == 1
    # Bounded NEXT tick: SQL now excludes the quarantined row, so the healthy
    # row behind it is returned. No unbounded loop, no starvation.
    second = issuer.pending([tenant], 1)
    assert [r.artifact_version_id for r in second] == [healthy.artifact_version_id]


def test_pending_quarantine_rpc_failure_surfaces_static_hold_and_dispatches_nothing(local_pg):
    admin, connect, _ = local_pg
    tenant = unique_tenant("gym-qfail")
    grant_tenant(admin, tenant)
    issuer = store(connect, "issuer_a")
    poison, poison_v = manifest_with(tenant, ',"oversized_float":1e400')
    key, binding, _ = stage_row(admin, poison)
    # RPC failure path: with EXECUTE revoked the quarantine call is denied, so
    # pending must raise a static-safe hold instead of silently returning an
    # empty success while the head poison would starve healthy work forever.
    admin.execute("revoke execute on function"
                  " public.generated_issuer_dispatch_quarantine_20261009(text,text,text)"
                  " from generated_issuer_dispatch_issuer_20261009")
    try:
        denied(lambda: issuer.pending([tenant], 10), "quarantine_rejected")
    finally:
        admin.execute("grant execute on function"
                      " public.generated_issuer_dispatch_quarantine_20261009(text,text,text)"
                      " to generated_issuer_dispatch_issuer_20261009")
    # Nothing was quarantined, claimed, issued or dispatched.
    assert admin.execute("select count(*) from public.generated_issuer_dispatch_quarantine_20261009"
                         " where gym_id=%s", (tenant,)).fetchone()[0] == 0
    assert admin.execute("select count(*) from public.generated_issuer_dispatch_ledger_20261009"
                         " where dispatch_key=%s", (key,)).fetchone()[0] == 0
    assert admin.execute("select count(*) from public.generated_hosted_byte_receipts_20261009"
                         " where artifact_version_id=%s", (poison_v,)).fetchone()[0] == 0
    # Derivation-failure path: an underivable dispatch identity also raises
    # the static hold (exercise the helper directly; such a row cannot exist
    # under the table constraints).
    with pytest.raises(GeneratedIssuerDispatchHold, match="quarantine_unavailable"):
        issuer._quarantine_row(tenant, None, f"https://cdn.example/{tenant}/generated/x.png",
                               "f" * 64, poison)
