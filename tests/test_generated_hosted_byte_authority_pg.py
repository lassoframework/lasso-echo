"""Disposable PG17 authority ACL/replay/immutability checks, no live database.

The fixture reproduces portal 0625's immutable version table only; this is not
the full calendar approval/cutover composition test. No DSN/env discovery.
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


ROOT = Path(__file__).resolve().parents[1]
PG = Path("/opt/homebrew/opt/postgresql@17/bin")
MIGRATION = ROOT / "migrations/DRAFT_generated_hosted_byte_authority_20261009.sql"
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
    with tempfile.TemporaryDirectory(prefix="generated_hosted_pg_", dir="/tmp") as directory:
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
            admin.execute(MIGRATION.read_text())
            admin.execute("create role issuer_a login; create role issuer_b login; create role reader_a login; "
                          "grant generated_hosted_byte_issuer_20261009 to issuer_a,issuer_b; "
                          "grant generated_hosted_byte_reader_20261009 to reader_a;")
            admin.execute("insert into public.generated_hosted_byte_principals_20261009 values "
                          "('issuer_a','gym-a',true,true),('issuer_b','gym-b',true,true),('reader_a','gym-a',false,true)")
            yield admin, connect, psycopg
        finally:
            for c in connections:
                c.close()
            subprocess.run([str(PG / "pg_ctl"), "-D", str(root / "data"), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True, timeout=60)


def binding(tenant="gym-a", version=None, url=None, data=b"SYNTHETIC exact delivered image"):
    version = version or str(uuid.uuid4())
    url = url or f"https://cdn.example/{tenant}/generated/{version}.png"
    sha = hashlib.sha256(data).hexdigest()
    manifest = dict(gym_id=tenant, artifact_version_id=version, hosted_url=url,
                    delivered_sha256=sha, engine="SYNTHETIC test", operation="same_object")
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    return tenant, version, url, sha, hashlib.sha256(raw).hexdigest(), data, raw


def rpc(c, values):
    return c.execute("select public.generated_hosted_byte_issue_20261009(%s,%s,%s,%s,%s,%s,%s)", values).fetchone()[0]


def look(c, b, receipt):
    return c.execute("select public.generated_hosted_byte_lookup_20261009(%s,%s,%s,%s,%s,%s)",
                     (*b[:5], receipt)).fetchone()[0]


def denied(pg, fn, code=None):
    with pytest.raises(pg.Error) as error:
        fn()
    if code:
        assert error.value.sqlstate == code


def test_producer_service_and_reader_cannot_issue_forge_grant_or_mutate(local_pg):
    admin, connect, pg = local_pg
    b = binding()
    for role in ("service_role", "anon", "authenticated", "reader_a"):
        c = connect(role)
        denied(pg, lambda: rpc(c, b), "42501")
        denied(pg, lambda: c.execute("insert into public.calendar_generated_artifact_versions values (%s,%s,%s,%s,%s,%s::jsonb,now())",
                                    (b[1], b[0], *b[2:5], "{}")), "42501")
        denied(pg, lambda: c.execute("insert into public.generated_hosted_byte_principals_20261009 values ('reader_a','gym-b',true,true)"), "42501")
        denied(pg, lambda: c.execute("select * from public.generated_hosted_byte_receipts_20261009"), "42501")
    c = connect("issuer_a")
    denied(pg, lambda: c.execute("select * from public.calendar_generated_artifact_versions"), "42501")
    denied(pg, lambda: c.execute("insert into public.generated_hosted_byte_receipts_20261009 default values"), "42501")
    # Producer bypasses RLS but still has only SELECT on the reviewed portal table.
    assert connect("service_role").execute("select count(*) from public.calendar_generated_artifact_versions").fetchone()[0] >= 0


def test_exact_byte_hash_manifest_and_cross_tenant_sql_rejections(local_pg):
    _, connect, pg = local_pg
    issuer = connect("issuer_a")
    b = binding()
    for index, value in ((3, "f" * 64), (4, "f" * 64), (5, b"wrong hosted bytes"), (6, b"{}")):
        bad = list(b)
        bad[index] = value
        denied(pg, lambda: rpc(issuer, bad), "23514")
    denied(pg, lambda: rpc(issuer, binding("gym-b")), "42501")
    wrong = json.loads(b[-1])
    wrong["gym_id"] = "gym-b"
    raw = json.dumps(wrong).encode()
    denied(pg, lambda: rpc(issuer, (*b[:4], hashlib.sha256(raw).hexdigest(), b[5], raw)), "23514")


def test_fresh_authority_adapter_read_replay_and_hosted_drift(local_pg):
    admin, connect, _ = local_pg
    b = binding()
    calls = []
    class Reader(a.HostedObjectReader):
        def __init__(self):
            super().__init__({"gym-a": ["https://cdn.example/gym-a/generated/"]})
            self.data = b[5]
        def read(self, tenant, url):
            calls.append((tenant, url))
            return self.data
    reader = Reader()
    authority = a.GeneratedHostedByteAuthority(lambda: connect("issuer_a", False), tenant_id="gym-a", reader=reader)
    args = dict(artifact_version_id=b[1], hosted_url=b[2], expected_sha256=b[3], manifest_bytes=b[-1])
    receipt = authority.issue(**args)
    before = admin.execute("select verified_at from public.calendar_generated_artifact_versions where id=%s", (b[1],)).fetchone()[0]
    assert authority.issue(**args) == receipt
    assert len(calls) == 2
    assert admin.execute("select verified_at from public.calendar_generated_artifact_versions where id=%s", (b[1],)).fetchone()[0] == before
    reader_authority = a.GeneratedHostedByteAuthority(lambda: connect("reader_a", False), tenant_id="gym-a")
    assert reader_authority.lookup(**args, receipt_id=receipt["receipt_id"]) == receipt
    reader.data = b"hosted object changed"
    with pytest.raises(a.HostedByteHold, match="hosted_bytes_mismatch"):
        authority.issue(**args)
    assert admin.execute("select count(*) from public.generated_hosted_byte_receipts_20261009 where artifact_version_id=%s", (b[1],)).fetchone()[0] == 1
    with pytest.raises(a.HostedByteHold):
        reader_authority.lookup(**args, receipt_id=str(uuid.uuid4()))


def test_version_tenant_url_receipt_and_manifest_cannot_be_reused(local_pg):
    _, connect, pg = local_pg
    issuer, other, reader = connect("issuer_a"), connect("issuer_b"), connect("reader_a")
    b = binding()
    receipt = rpc(issuer, b)
    assert look(reader, b, receipt["receipt_id"]) == receipt
    for i, v in ((1, str(uuid.uuid4())), (2, b[2] + "changed"), (3, "f" * 64), (4, "f" * 64)):
        bad = list(b)
        bad[i] = v
        assert look(reader, bad, receipt["receipt_id"]) is None
    denied(pg, lambda: look(reader, binding("gym-b", version=b[1]), receipt["receipt_id"]), "42501")
    assert look(other, binding("gym-b", version=b[1]), receipt["receipt_id"]) is None
    denied(pg, lambda: rpc(other, binding("gym-b", version=b[1])), "23514")
    denied(pg, lambda: rpc(issuer, binding(url=b[2])), "23514")
    denied(pg, lambda: rpc(issuer, binding(version=b[1], data=b"new rendition")), "23514")
    # Identical parsed manifest with different original-byte encoding is a conflict.
    raw = json.dumps(json.loads(b[-1]), indent=2).encode()
    denied(pg, lambda: rpc(issuer, (*b[:4], hashlib.sha256(raw).hexdigest(), b[5], raw)), "23514")


@pytest.mark.parametrize("alias", ["CDN.EXAMPLE", "cdn.example:443", "CDN.EXAMPLE:443",
                                    "cdn.example.", "cdn.example.:443", "cdn.example:", "cdn.example:0443"])
def test_sql_rejects_same_hosted_object_url_alias_reuse(local_pg, alias):
    _, connect, pg = local_pg
    issuer = connect("issuer_a")
    b = binding()
    receipt = rpc(issuer, b)
    aliased = b[2].replace("cdn.example", alias)
    # A new UUID and matching manifest cannot reuse the object by changing
    # hostname case/default-port/trailing-dot spelling at the SQL boundary.
    denied(pg, lambda: rpc(issuer, binding(url=aliased)), "23514")
    assert look(issuer, binding(version=b[1], url=aliased), receipt["receipt_id"]) is None


@pytest.mark.parametrize("scheme", ["HTTPS", "Https"])
def test_sql_rejects_scheme_alias_same_object_reuse(local_pg, scheme):
    _, connect, pg = local_pg
    issuer = connect("issuer_a")
    b = binding()
    rpc(issuer, b)
    denied(pg, lambda: rpc(issuer, binding(url=b[2].replace("https", scheme, 1))), "23514")


def test_unissued_portal_row_and_forged_receipt_never_gain_authority(local_pg):
    admin, connect, pg = local_pg
    b = binding()
    forged = dict(receipt_id=str(uuid.uuid4()), gym_id=b[0], artifact_version_id=b[1], hosted_url=b[2],
                  delivered_sha256=b[3], render_manifest=json.loads(b[-1]))
    admin.execute("insert into public.calendar_generated_artifact_versions(id,gym_id,image_url,delivered_sha256,render_manifest_digest,delivery_receipt) values (%s,%s,%s,%s,%s,%s::jsonb)",
                  (b[1], b[0], *b[2:5], json.dumps(forged)))
    assert look(connect("reader_a"), b, forged["receipt_id"]) is None
    denied(pg, lambda: rpc(connect("issuer_a"), b), "23514")


def test_both_receipt_and_portal_rows_immutable_including_admin(local_pg):
    admin, connect, pg = local_pg
    b = binding()
    rpc(connect("issuer_a"), b)
    for table, key in (("calendar_generated_artifact_versions", "id"),
                       ("generated_hosted_byte_receipts_20261009", "artifact_version_id")):
        denied(pg, lambda: admin.execute(f"update public.{table} set gym_id='gym-b' where {key}=%s", (b[1],)), "23514")
        denied(pg, lambda: admin.execute(f"delete from public.{table} where {key}=%s", (b[1],)), "23514")
        denied(pg, lambda: admin.execute(f"truncate public.{table} cascade"), "23514")
        denied(pg, lambda: connect("service_role").execute(f"update public.{table} set gym_id='gym-b' where {key}=%s", (b[1],)), "42501")


def test_concurrent_identical_replay_issues_one_receipt(local_pg):
    admin, connect, _ = local_pg
    b = binding()
    c1, c2 = connect("issuer_a"), connect("issuer_a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rpc, c, b) for c in (c1, c2)]
        r1, r2 = [f.result(timeout=15) for f in futures]
    assert r1 == r2
    assert admin.execute("select count(*) from public.generated_hosted_byte_receipts_20261009 where artifact_version_id=%s", (b[1],)).fetchone()[0] == 1


def test_preflight_tenant_revoked_before_final_write_holds(local_pg):
    admin, connect, _ = local_pg
    b = binding()
    class RevokingReader(a.HostedObjectReader):
        def __init__(self):
            super().__init__({"gym-a": ["https://cdn.example/gym-a/generated/"]})
        def read(self, tenant, url):
            admin.execute("update public.generated_hosted_byte_principals_20261009 set can_issue=false where principal='issuer_a'")
            return b[5]
    try:
        authority = a.GeneratedHostedByteAuthority(lambda: connect("issuer_a", False), tenant_id="gym-a", reader=RevokingReader())
        with pytest.raises(a.HostedByteHold, match="identity_unavailable"):
            authority.issue(artifact_version_id=b[1], hosted_url=b[2], expected_sha256=b[3], manifest_bytes=b[-1])
        assert admin.execute("select count(*) from public.calendar_generated_artifact_versions where id=%s", (b[1],)).fetchone()[0] == 0
    finally:
        admin.execute("update public.generated_hosted_byte_principals_20261009 set can_issue=true where principal='issuer_a'")


def test_grant_revocation_serializes_with_active_issuance(local_pg):
    admin, connect, pg = local_pg
    issuer = connect("issuer_a", False)
    revoker = connect()
    revoker.execute("set lock_timeout='200ms'")
    b = binding()
    try:
        rpc(issuer, b)
        denied(pg, lambda: revoker.execute("update public.generated_hosted_byte_principals_20261009 set can_issue=false where principal='issuer_a'"), "55P03")
        issuer.commit()
        revoker.execute("update public.generated_hosted_byte_principals_20261009 set can_issue=false where principal='issuer_a'")
        denied(pg, lambda: rpc(connect("issuer_a"), binding()), "42501")
    finally:
        issuer.rollback()
        admin.execute("update public.generated_hosted_byte_principals_20261009 set can_issue=true where principal='issuer_a'")
