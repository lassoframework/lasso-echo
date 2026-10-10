"""ACTUAL PostgreSQL 17 tests for
migrations/DRAFT_historical_gbp_original_recovery.sql.

These run the REAL migration (no stubbed RPCs) against a real local PG17
server reached over the existing Unix socket /tmp port 5432 as the current
OS user, inside a unique disposable database that is created and dropped by
the test. psql is driven via subprocess (psycopg is not installed). If the
server is unreachable the whole module SKIPS -- a connection refusal is
expected in some sandboxes and is not a failure.

Generic fixtures only: no real gym ids, row ids, URLs or hashes.
"""
import getpass
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "migrations" / "DRAFT_historical_gbp_original_recovery.sql"
BASE_MIG = ROOT / "migrations" / "portal_action_receipt_draft_20261004.sql"
SQL = MIG.read_text()
LOW = SQL.lower()

PGBIN_CANDIDATES = [Path("/opt/homebrew/opt/postgresql@17/bin"),
                    Path("/usr/local/opt/postgresql@17/bin"),
                    Path("/usr/lib/postgresql/17/bin"),
                    Path("/usr/pgsql-17/bin")]


def _psql():
    for cand in PGBIN_CANDIDATES:
        if (cand / "psql").exists():
            return cand / "psql"
    return None


PSQL = _psql()
SOCK = "/tmp"
PORT = "5432"
PGUSER = getpass.getuser()
DB = "echo_hgr_test_" + uuid.uuid4().hex[:12]

ORIGIN = "https://cdn.example.test"
GYM = "gym-test"
RAW_SHA = hashlib.sha256(b"raw-bytes").hexdigest()
DELIVERED_SHA = hashlib.sha256(b"delivered-bytes").hexdigest()
SOURCE_URL = f"{ORIGIN}/echo/{GYM}/0123456789abcdef/raw.jpg"
DELIVERED_URL = f"{ORIGIN}/echo/{GYM}/fedcba9876543210/delivered.jpg"
TARGET = uuid.uuid4()
HIST = uuid.uuid4()
FP = hashlib.sha256(b"request").hexdigest()
HOLD = "cross_date_media_repeat_needs_new_visual"


def _run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, timeout=60, **kw)


def sql(statement, expect_error=False, db=DB):
    done = _run([str(PSQL), "-U", PGUSER, "-X", "-q", "-A", "-t",
                 "-v", "ON_ERROR_STOP=1", "-h", SOCK, "-p", PORT,
                 "-d", db, "-c", statement])
    if expect_error:
        assert done.returncode != 0, f"expected failure, got: {done.stdout}"
        return done.stderr
    if done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def _admin(statement):
    return _run([str(PSQL), "-U", PGUSER, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                 "-h", SOCK, "-p", PORT, "-d", "postgres", "-c", statement])


@pytest.fixture(scope="module")
def pg():
    if PSQL is None:
        pytest.skip("no local PostgreSQL 17 psql binary")
    probe = _run([str(PSQL), "-U", PGUSER, "-X", "-A", "-t", "-h", SOCK,
                  "-p", PORT, "-d", "postgres",
                  "-c", "show server_version_num"])
    if probe.returncode != 0 or not probe.stdout.strip().startswith("17"):
        pytest.skip("local PG17 on /tmp:5432 unavailable in this sandbox")
    assert _admin(f"create database {DB}").returncode == 0
    try:
        sql("""create table public.content_calendar(
            id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
            logical_post_id uuid,
            status text default 'pending', account text default 'instagram',
            format text default 'feed',
            variant_status text default 'active', caption text, image_url text,
            thumbnail_url text,
            source_media_url text, source_media_asset_id text, drive_file_id text,
            byte_hash text, r2_key text, media_not_ready_reason text,
            published_at timestamptz, publish_claim_token uuid,
            late_post_id text, approval_kind text, approved_by text,
            approved_at timestamptz, approval_digest text, scheduled_at timestamptz,
            extra_future_column text,
            created_at timestamptz default now(), updated_at timestamptz default now());
            create table public.media_asset(id text primary key,gym_id text,
                eligible boolean,excluded_by_coach boolean);
            """)
        done = _run([str(PSQL), "-U", PGUSER, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                     "-h", SOCK, "-p", PORT, "-d", DB, "-f", str(BASE_MIG)])
        assert done.returncode == 0, done.stderr
        done = _run([str(PSQL), "-U", PGUSER, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                     "-h", SOCK, "-p", PORT, "-d", DB, "-f", str(MIG)])
        assert done.returncode == 0, done.stderr
        yield
    finally:
        assert _admin(f"drop database {DB} with (force)").returncode == 0


def _snapshot_clause(row_desc):
    return row_desc


def _insert_rows(caption="hello"):
    sql(f"insert into public.content_calendar"
        f"(id, gym_id, post_date, status, account, format, caption, image_url,"
        f" media_not_ready_reason) values"
        f" ('{TARGET}','{GYM}','2026-09-01','pending','googlebusiness','update',"
        f" '{caption}','{DELIVERED_URL}','{HOLD}'),"
        f" ('{HIST}','{GYM}','2026-08-01','published','instagram','feed','hist',"
        f" '{SOURCE_URL}', NULL);")
    sql(f"update public.content_calendar set source_media_url='{SOURCE_URL}'"
        f" where id='{HIST}'")


def _seed_binding():
    before = sql(f"select to_jsonb(c) from public.content_calendar c where id='{TARGET}'")
    hist = sql(f"select to_jsonb(c) from public.content_calendar c where id='{HIST}'")
    sql("insert into public.historical_gbp_original_recovery_binding"
        "(gym_id, row_id, historical_row_id, tenant_slug, source_origin,"
        " source_url, source_sha256, delivered_sha256, recipe,"
        " expected_before, historical_snapshot, seeded_by) values"
        f"('{GYM}','{TARGET}','{HIST}','{GYM}','{ORIGIN}','{SOURCE_URL}',"
        f"'{RAW_SHA}','{DELIVERED_SHA}','gbp-crop-4x3-jpeg90-v1',"
        f"'{before}'::jsonb,'{hist}'::jsonb,'operator-test')")


def _proof():
    return json.dumps({
        "source_url": SOURCE_URL, "source_sha256": RAW_SHA,
        "delivered_sha256": DELIVERED_SHA,
        "recipe": "gbp-crop-4x3-jpeg90-v1", "tenant_slug": GYM})


def _clean():
    sql("update public.historical_gbp_original_recovery_gate set enabled=false")
    # TRUNCATE bypasses the row-level immutability triggers (owner-only test
    # cleanup; the receipt table can never be row-deleted through DML).
    sql("truncate public.historical_gbp_original_recovery_receipt,"
        " public.historical_gbp_original_recovery_binding,"
        " public.content_calendar")


def _seed_all():
    sql("update public.historical_gbp_original_recovery_gate set enabled=true")
    sql("insert into public.portal_action_receipt_config(key, value)"
        " values('public_origin','" + ORIGIN + "')"
        " on conflict (key) do nothing")
    _insert_rows()
    _seed_binding()


def _apply(request_id="req-1", fp=FP, proof=None):
    proof = proof or _proof()
    return sql(f"select row_id::text, status, request_id from"
               f" public.historical_gbp_original_recovery_apply("
               f"'{GYM}','{TARGET}','{request_id}','{fp}','{proof}'::jsonb)")


# ---------------------------------------------------------------------------
# Static contracts (always run)
# ---------------------------------------------------------------------------

def test_static_security_definer_service_role_only():
    assert "security definer" in LOW
    assert LOW.count("grant execute on function") == 2
    assert "to service_role" in LOW
    assert "revoke all on function public.historical_gbp_original_recovery_apply" in LOW
    assert "revoke all on public.historical_gbp_original_recovery_binding" in LOW
    assert "revoke all on public.historical_gbp_original_recovery_receipt" in LOW
    assert "enable row level security" in LOW
    # no direct write grant for any PostgREST role, service_role included
    assert not re.search(r"grant (insert|update|delete|all)", LOW)
    # one recovery per target + immutable receipts
    assert "unique (gym_id, row_id)" in LOW
    assert "before update or delete on public.historical_gbp_original_recovery_receipt" in LOW
    # the only calendar write is the pinned source URL
    writes = re.findall(r"update public\.content_calendar[^;]*;", LOW, re.S)
    assert len(writes) == 1 and "set source_media_url = v_binding.source_url" in writes[0]
    # no row ids or origins seeded by the migration
    assert "insert into public.historical_gbp_original_recovery_binding" not in LOW
    assert "portal_action_receipt_config" in LOW  # fixed trusted origin source


# ---------------------------------------------------------------------------
# Live PG17 behavior
# ---------------------------------------------------------------------------

def test_disabled_gate_fails_closed(pg):
    _insert_rows()
    _seed_binding()
    err = sql(f"select public.historical_gbp_original_recovery_apply("
              f"'{GYM}','{TARGET}','req-0','{FP}','{_proof()}'::jsonb)",
              expect_error=True)
    assert "recovery is OFF" in err
    _clean()


def test_role_grants(pg):
    sql("set role service_role; select count(*) from"
        " public.historical_gbp_original_recovery_binding")
    err = sql("set role service_role; insert into"
              " public.historical_gbp_original_recovery_binding"
              "(gym_id,row_id,historical_row_id,tenant_slug,source_origin,"
              " source_url,source_sha256,delivered_sha256,recipe,"
              " expected_before,historical_snapshot) values"
              f"('x','{uuid.uuid4()}','{uuid.uuid4()}','x','{ORIGIN}',"
              f"'{SOURCE_URL}','{RAW_SHA}','{DELIVERED_SHA}',"
              "'gbp-crop-4x3-jpeg90-v1','{}','{}')", expect_error=True)
    assert "permission denied" in err
    err = sql("set role authenticated; select public.historical_gbp_original_recovery_read"
              f"('{GYM}','{TARGET}')", expect_error=True)
    assert "permission denied" in err


def test_apply_success_and_idempotent_replay(pg):
    _seed_all()
    out = _apply()
    assert out == f"{TARGET}|succeeded|req-1"
    got = sql(f"select source_media_url from public.content_calendar"
              f" where id='{TARGET}'")
    assert got == SOURCE_URL
    # hold, status, caption and delivered bytes pointer preserved
    row = sql(f"select status, caption, image_url, media_not_ready_reason from"
              f" public.content_calendar where id='{TARGET}'")
    assert row == f"pending|hello|{DELIVERED_URL}|{HOLD}"
    # exact replay returns the same receipt, no second write
    again = _apply()
    assert again == out
    count = sql(f"select count(*) from public.historical_gbp_original_recovery_receipt")
    assert count == "1"
    # read RPC returns binding + persisted receipt (lost-response readback)
    read = sql(f"select public.historical_gbp_original_recovery_read('{GYM}','{TARGET}')")
    data = json.loads(read)
    assert data["binding"]["row_id"] == str(TARGET)
    assert data["receipt"]["request_id"] == "req-1"
    assert data["receipt"]["after_state"]["source_media_url"] == SOURCE_URL
    _clean()


def test_changed_request_denied(pg):
    _seed_all()
    _apply()
    err = sql(f"select public.historical_gbp_original_recovery_apply("
              f"'{GYM}','{TARGET}','req-2','{FP}','{_proof()}'::jsonb)",
              expect_error=True)
    assert "conflicting reuse" in err
    _clean()


def test_snapshot_drift_rolls_back(pg):
    _seed_all()
    sql(f"update public.content_calendar set caption='edited' where id='{TARGET}'")
    err = sql(f"select public.historical_gbp_original_recovery_apply("
              f"'{GYM}','{TARGET}','req-1','{FP}','{_proof()}'::jsonb)",
              expect_error=True)
    assert "drifted from the configured binding" in err
    got = sql(f"select source_media_url from public.content_calendar where id='{TARGET}'")
    assert got == ""
    assert sql("select count(*) from public.historical_gbp_original_recovery_receipt") == "0"
    _clean()


def test_trigger_soft_rewrite_rolls_back(pg):
    _seed_all()
    sql("""create or replace function public.test_soft_rewrite()
           returns trigger language plpgsql as $t$
           begin new.caption := 'rewritten'; return new; end $t$;""")
    sql("create trigger soft_rewrite before update on public.content_calendar"
        " for each row execute function public.test_soft_rewrite()")
    err = sql(f"select public.historical_gbp_original_recovery_apply("
              f"'{GYM}','{TARGET}','req-1','{FP}','{_proof()}'::jsonb)",
              expect_error=True)
    assert "drifted from the expected after snapshot" in err
    got = sql(f"select source_media_url, caption from public.content_calendar"
              f" where id='{TARGET}'")
    assert got == "|hello"
    assert sql("select count(*) from public.historical_gbp_original_recovery_receipt") == "0"
    sql("drop trigger soft_rewrite on public.content_calendar")
    sql("drop function public.test_soft_rewrite()")
    _clean()


def test_cross_tenant_historical_denied(pg):
    _seed_all()
    sql(f"update public.content_calendar set gym_id='other-gym' where id='{HIST}'")
    err = sql(f"select public.historical_gbp_original_recovery_apply("
              f"'{GYM}','{TARGET}','req-1','{FP}','{_proof()}'::jsonb)",
              expect_error=True)
    assert "historical source row missing or cross-tenant" in err
    assert sql("select count(*) from public.historical_gbp_original_recovery_receipt") == "0"
    _clean()


def test_proof_mismatch_denied(pg):
    _seed_all()
    bad = json.dumps({"source_url": f"{ORIGIN}/echo/{GYM}/0123456789abcdef/evil.jpg",
                      "source_sha256": RAW_SHA, "delivered_sha256": DELIVERED_SHA,
                      "recipe": "gbp-crop-4x3-jpeg90-v1", "tenant_slug": GYM})
    err = sql(f"select public.historical_gbp_original_recovery_apply("
              f"'{GYM}','{TARGET}','req-1','{FP}','{bad}'::jsonb)",
              expect_error=True)
    assert "proof does not match the configured binding" in err
    assert sql("select count(*) from public.historical_gbp_original_recovery_receipt") == "0"
    _clean()


def test_concurrent_apply_single_receipt(pg):
    _seed_all()
    results, errors = [], []

    def call():
        done = _run([str(PSQL), "-U", PGUSER, "-X", "-q", "-A", "-t",
                     "-v", "ON_ERROR_STOP=1", "-h", SOCK, "-p", PORT,
                     "-d", DB, "-c",
                     f"select status from public.historical_gbp_original_recovery_apply("
                     f"'{GYM}','{TARGET}','req-1','{FP}','{_proof()}'::jsonb)"])
        (results if done.returncode == 0 else errors).append(done)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4 and not errors, [e.stderr for e in errors]
    assert sql("select count(*) from public.historical_gbp_original_recovery_receipt") == "1"
    _clean()


def test_receipt_immutable(pg):
    _seed_all()
    _apply()
    err = sql("update public.historical_gbp_original_recovery_receipt"
              " set proof='{}'::jsonb", expect_error=True)
    assert "immutable" in err
    err = sql("update public.historical_gbp_original_recovery_binding"
              " set source_url='https://evil.test/x.jpg'", expect_error=True)
    assert "immutable" in err
    err = sql("delete from public.historical_gbp_original_recovery_receipt",
              expect_error=True)
    assert "immutable" in err
    _clean()


@pytest.mark.parametrize('field,value', [
    ('approval_kind', "'client_verified'"), ('approved_by', "'coach'"),
    ('approved_at', 'now()'), ('approval_digest', "'unproved'"),
    ('late_post_id', "'provider-receipt'"),
])
def test_ineligible_approval_is_refused_even_when_operator_snapshot_matches(pg, field, value):
    sql('update public.historical_gbp_original_recovery_gate set enabled=true')
    _insert_rows()
    sql(f'update public.content_calendar set {field}={value} where id=\'{TARGET}\'')
    _seed_binding()
    sql(f"select public.historical_gbp_original_recovery_apply('{GYM}','{TARGET}',"
        f"'req-approval','{FP}','{_proof()}'::jsonb)", expect_error=True)
    assert sql('select count(*) from public.historical_gbp_original_recovery_receipt') == '0'
    assert sql(f"select source_media_url is null from public.content_calendar where id='{TARGET}'") == 't'
    _clean()


def test_unlisted_future_column_cas_and_trigger_rollback(pg):
    _seed_all()
    sql(f"update public.content_calendar set extra_future_column='drift' where id='{TARGET}'")
    sql(f"select public.historical_gbp_original_recovery_apply('{GYM}','{TARGET}',"
        f"'req-future','{FP}','{_proof()}'::jsonb)", expect_error=True)
    _clean()
    _seed_all()
    sql("""create function public.future_rewrite() returns trigger language plpgsql as $$
      begin new.extra_future_column := 'soft rewrite'; return new; end $$;
      create trigger future_rewrite before update on public.content_calendar
        for each row execute function public.future_rewrite();""")
    sql(f"select public.historical_gbp_original_recovery_apply('{GYM}','{TARGET}',"
        f"'req-soft','{FP}','{_proof()}'::jsonb)", expect_error=True)
    assert sql(f"select source_media_url is null and extra_future_column is null from public.content_calendar where id='{TARGET}'") == 't'
    assert sql('select count(*) from public.historical_gbp_original_recovery_receipt') == '0'
    sql('drop trigger future_rewrite on public.content_calendar; drop function public.future_rewrite()')
    _clean()


def test_service_role_can_only_use_atomic_rpc(pg):
    _seed_all()
    result = sql(f"set role service_role; select status from public.historical_gbp_original_recovery_apply("
                 f"'{GYM}','{TARGET}','req-service','{FP}','{_proof()}'::jsonb)")
    assert result == 'succeeded'
    assert sql('select count(*) from public.historical_gbp_original_recovery_receipt') == '1'
    _clean()
