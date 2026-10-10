"""P3 nonblocking guard for visual_group_activate_guard: static + live PG17.

The activation barrier must never wait on a lock whose owner may be blocked
by that same barrier (the forward G(shared)/C(exclusive) entry guards vs the
calendar SHARE ROW EXCLUSIVE barrier inversion, PG17 40P01 cycle). This module
verifies DRAFT_visual_group_activation_20261002.sql:

  (a) the calendar barrier is SHARE ROW EXCLUSIVE NOWAIT, and the forward
      graph 'G' (shared) then census 'C' (exclusive) advisories are TRYed
      immediately after the barrier, before the tenant/backfill advisories
      and BEFORE any auxiliary write/backfill;
  (b) contention on the barrier, G or C raises SQLSTATE 55P03 and rolls back;
  (c) barrier, freshness, tenant, coverage and arming behavior are unchanged.

Static checks run everywhere. Live checks spin a disposable PostgreSQL 17
cluster in /tmp via initdb/pg_ctl (the repo's *_pg.py convention) and drive it
with psql (no psycopg dependency); they skip if the PG17 binaries are absent.
Never point this at a network or production database.
"""
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / 'migrations'
ACTIVATION = (MIGRATIONS / 'DRAFT_visual_group_activation_20261002.sql').read_text()
LOW = ACTIVATION.lower()

GRAPH_KEY = 'fixer_forward_graph_20261006'
CENSUS_KEY = 'fixer_forward_photo_census_20261007'

PG_BIN = Path('/opt/homebrew/opt/postgresql@17/bin')
PSQL = PG_BIN / 'psql'

BODY = LOW[LOW.index('create or replace function public.visual_group_activate_guard'):]

# --------------------------------------------------------------------------
# Static checks (no database)
# --------------------------------------------------------------------------


def test_calendar_barrier_is_share_row_exclusive_nowait():
    assert 'lock table public.content_calendar in share row exclusive mode nowait;' in LOW


def test_auxiliary_table_freeze_stays_nowait():
    assert re.search(r'lock table public\.visual_group_usage_ledger,[^;]*nowait;', LOW, re.S)


def test_forward_entry_locks_use_nonblocking_try_in_g_then_c_order():
    g = BODY.index("pg_try_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))")
    c = BODY.index("pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0))")
    barrier = BODY.index('lock table public.content_calendar in share row exclusive mode nowait;')
    aux = BODY.index('lock table public.visual_group_usage_ledger,')
    tenant = BODY.index("jsonb_build_array('visual_tenant'")
    backfill = BODY.index('public.visual_group_backfill_gym(v_key, false)')
    receipt = BODY.index('insert into public.visual_group_activation')
    # G and C are TRYed immediately after the barrier freeze, before the
    # tenant advisory, before any backfill write, before the receipt.
    assert barrier < aux < g < c < tenant < backfill < receipt


def test_no_blocking_acquisition_of_forward_entry_keys():
    assert "pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006'" not in ACTIVATION
    assert "pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007'" not in ACTIVATION
    assert "pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006'" not in ACTIVATION


def test_every_lock_contention_path_raises_55p03():
    assert ACTIVATION.count("errcode='55P03'") >= 4  # G, C, tenant mutex, per-key backfill
    assert 'activation refused: forward graph entry lock busy; retry transaction' in LOW
    assert 'activation refused: forward census entry lock busy; retry transaction' in LOW


def test_business_behavior_unchanged():
    # Freshness, tenant resolution, coverage and arming semantics preserved.
    assert "current_setting('transaction_isolation') <> 'read committed'" in LOW
    assert 'activation requires a fresh transaction without prior advisory or calendar write/row locks' in LOW
    assert 'public.visual_group_tenant_strict(p_gym_id)' in ACTIVATION
    assert "'idempotent', true" in ACTIVATION
    assert 'perform public.visual_global_import_history();' in ACTIVATION
    assert 'public.visual_global_coverage()' in ACTIVATION
    assert "public.visual_global_history_coverage() h" in ACTIVATION
    assert "where h.issue<>'ready'" in ACTIVATION
    assert 'a.transaction_id = txid_current()' in ACTIVATION
    for needle in ('unknown, ambiguous or review-pending media identity',
                   'published row missing immutable historical incident coverage',
                   'missing dated ledger reservation or active sibling coverage',
                   'unresolved historical review events',
                   'unresolved ambiguous usage',
                   'cross-date occupied staged visual scene',
                   'unmapped or foreign tenant calendar key',
                   'undated published row has no verified calendar post_date',
                   'legacy byte_hash lacks source/derived algorithm namespace'):
        assert needle in LOW
    assert 'on conflict (gym_id) do update set enforce = true' in LOW
    assert 'grant execute on function public.visual_group_activate_guard(text,text) to service_role' in LOW


# --------------------------------------------------------------------------
# Live disposable PG17 harness
# --------------------------------------------------------------------------

CLUSTER = None
DSN = None


def _pg17_available():
    return (PG_BIN / 'initdb').exists() and (PG_BIN / 'pg_ctl').exists() and PSQL.exists()


def sql(statement, on_error_stop=True):
    proc = subprocess.run([str(PSQL), '-X', '-q', '-A', '-t', '-d', DSN,
                           '-v', f'ON_ERROR_STOP={1 if on_error_stop else 0}',
                           '-c', statement], text=True, capture_output=True, timeout=30)
    if on_error_stop and proc.returncode:
        raise RuntimeError(proc.stderr)
    return proc


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def session_holding(statements, app_name, hold_seconds=6):
    proc = subprocess.Popen([str(PSQL), '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-d', DSN],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    proc.stdin.write(f'set application_name={q(app_name)};\nbegin;\n{statements}\n'
                     f'select pg_sleep({hold_seconds});\ncommit;\n')
    proc.stdin.flush()
    return proc


def wait_until_sleeping(app_name, deadline_seconds=10):
    deadline = time.monotonic() + deadline_seconds
    while True:
        out = sql("select coalesce((select state||':'||coalesce(wait_event,'') from pg_stat_activity "
                  f"where application_name={q(app_name)}),'absent')").stdout.strip()
        if out.endswith(':PgSleep'):
            return
        assert time.monotonic() < deadline, f'contender never reached sleep (state={out})'
        time.sleep(.02)


@pytest.fixture(scope='session')
def pg_cluster():
    global DSN
    if not _pg17_available():
        pytest.skip('PostgreSQL 17 binaries not present at /opt/homebrew/opt/postgresql@17')
    assert shutil.disk_usage('/tmp').free > 3 * 1024**3
    tmp = tempfile.mkdtemp(prefix='visual_group_p3_pg_', dir='/tmp')
    sock = Path(tmp) / 'sock'
    sock.mkdir()
    data = Path(tmp) / 'data'
    port = random.randint(43000, 59000)
    while True:
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) != 0:
                break
        port = random.randint(43000, 59000)
    subprocess.run([str(PG_BIN / 'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                   check=True, capture_output=True, timeout=60)
    started = False
    try:
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(data), '-l', str(Path(tmp) / 'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        started = True
        DSN = f'host={sock} port={port} user=postgres dbname=postgres'
        assert int(sql('show server_version_num').stdout.strip()) // 10000 == 17
        sql('create role anon; create role authenticated; create role service_role;')
        yield
    finally:
        if started:
            subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def cluster(pg_cluster):
    # Fresh schema per test: once any tenant is armed, the guard bars unarmed
    # tenants from staging authority rows, so tests cannot share a schema.
    sql('drop schema public cascade; create schema public;'
        ' grant usage on schema public to anon,authenticated,service_role;')
    sql("""create table public.content_calendar(
      id uuid primary key default gen_random_uuid(), gym_id text,
      post_date date, status text not null default 'draft' check(status in ('draft','pending','approved','published','denied','killed','failed','publishing','deleted','coach_review')), account text,
      format text, variant_status text not null default 'active' check(variant_status in ('active','candidate','archived')), image_url text,
      source_media_url text,source_media_asset_id text,drive_file_id text,byte_hash text,r2_key text,
      media_not_ready_reason text,published_at timestamptz,late_post_id text,
      publish_reservation_day date,publish_claim_token uuid,created_at timestamptz not null default now()
    ); grant select,insert,update,delete on public.content_calendar to service_role;""")
    sql("create table public.media_asset(id text primary key,gym_id text,source_id text,content_hash text,rendition_url text);"
        'grant select on public.media_asset to service_role;')
    for name in ('DRAFT_visual_group_schema_20261002.sql',
                 'DRAFT_visual_group_claim_trigger_20261002.sql',
                 'DRAFT_visual_group_backfill_20261002.sql',
                 'calendar_claim_media_guard_20261002.sql',
                 'DRAFT_visual_global_history_20261002.sql',
                 'DRAFT_visual_group_activation_20261002.sql'):
        sql((MIGRATIONS / name).read_text())


def tenant():
    key = 'g_' + uuid.uuid4().hex
    tid = str(uuid.uuid4())
    sql(f'select public.visual_group_tenant_register({q(key)},{q(tid)}::uuid)')
    return key, tid


def attest(key, tid, label, with_row=True):
    """Same-object attestation so the global import has complete byte evidence
    for the scene this tenant's calendar row occupies."""
    url = f'https://test/{label}-{uuid.uuid4().hex}.jpg'
    data = f'exact bytes for {label} {uuid.uuid4().hex}'.encode()
    fingerprint = 'md5:' + hashlib.md5(data).hexdigest()
    group = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url)})").stdout.strip()
    receipt = str(uuid.uuid4())
    sql("insert into public.visual_global_object_read_receipt "
        "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by) "
        f"values({q(receipt)}::uuid,{q(tid)},{q(url)},{q(fingerprint)},{len(data)},"
        "'verified_object_read','object-read-test','fixture_owner')")
    sql(f"set role service_role; select public.visual_global_prepare_source_rendition("
        f"{q(tid)},{q(group)},{q(receipt)}::uuid,{q(receipt)}::uuid,null,'test_actor')")
    if with_row:
        sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,source_media_url,visual_group_key,status) "
            f"values({q(key)},'2026-10-05','instagram',{q(url)},{q(url)},{q(group)},'pending')")
    return url, group


def armed(key, tid):
    attest(key, tid, 'p3')


def activate(key):
    return sql(f'select public.visual_group_activate_guard({q(key)},{q("tester")})', on_error_stop=False)


def assert_refused_fast(result, pattern, started, limit=3.0):
    assert result.returncode != 0, f'activation unexpectedly succeeded: {result.stdout}'
    assert re.search(pattern, result.stderr), f'unexpected error: {result.stderr}'
    assert time.monotonic() - started < limit, 'activation waited instead of failing closed'


def test_live_clean_activation_and_idempotent_recall(cluster):
    key, tid = tenant()
    armed(key, tid)
    result = activate(key)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout.strip())
    assert proof['enforced'] is True and proof['tenant'] == tid
    assert 'nowait' in proof['barrier'] and 'fixer_forward' not in proof['barrier']
    again = activate(key)
    assert again.returncode == 0 and json.loads(again.stdout.strip())['idempotent'] is True
    assert sql(f"select enforce from public.gym_visual_guard_settings where gym_id={q(tid)}").stdout.strip() == 't'


def test_live_calendar_writer_barrier_nowait_refuses_then_retries(cluster):
    key, tid = tenant()
    armed(key, tid)
    url_b, group_b = attest(key, tid, 'p3b', with_row=False)
    name = 'p3_writer_' + uuid.uuid4().hex
    proc = session_holding(
        f"insert into public.content_calendar(gym_id,post_date,account,image_url,source_media_url,visual_group_key,status) "
        f"values({q(key)},'2026-10-05','story',{q(url_b)},{q(url_b)},{q(group_b)},'draft');", name)
    try:
        wait_until_sleeping(name)  # writer holds ROW EXCLUSIVE on content_calendar
        started = time.monotonic()
        result = activate(key)
        assert_refused_fast(result, r'55P03|could not obtain lock|lock not available', started)
        assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}').stdout.strip() == '0'
        assert sql(f"select count(*) from public.gym_visual_guard_settings where gym_id={q(tid)}").stdout.strip() == '0'
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    result = activate(key)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())['calendar_rows'] == 2


def test_live_graph_g_exclusive_owner_blocks_activation_with_55p03(cluster):
    key, tid = tenant()
    armed(key, tid)
    name = 'p3_graph_' + uuid.uuid4().hex
    proc = session_holding(
        f"select pg_advisory_xact_lock(hashtextextended('{GRAPH_KEY}',0));", name)
    try:
        wait_until_sleeping(name)
        started = time.monotonic()
        result = activate(key)
        assert_refused_fast(result, r'forward graph entry lock busy', started)
        # Rollback was complete: nothing armed, no receipt, no backfill rows.
        assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}').stdout.strip() == '0'
        assert sql(f"select count(*) from public.visual_group_usage_ledger where gym_id={q(tid)}").stdout.strip() == '0'
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    result = activate(key)
    assert result.returncode == 0 and json.loads(result.stdout.strip())['enforced'] is True


def test_live_census_c_owner_blocks_activation_with_55p03(cluster):
    key, tid = tenant()
    armed(key, tid)
    name = 'p3_census_' + uuid.uuid4().hex
    proc = session_holding(
        f"select pg_advisory_xact_lock(hashtextextended('{CENSUS_KEY}',0));", name)
    try:
        wait_until_sleeping(name)
        started = time.monotonic()
        result = activate(key)
        assert_refused_fast(result, r'forward census entry lock busy', started)
        assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}').stdout.strip() == '0'
        assert sql(f"select count(*) from public.gym_visual_guard_settings where gym_id={q(tid)}").stdout.strip() == '0'
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    result = activate(key)
    assert result.returncode == 0 and json.loads(result.stdout.strip())['enforced'] is True


def test_live_unknown_media_refusal_unchanged(cluster):
    key, tid = tenant()
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status,published_at) "
        f"values({q(key)},'2026-10-05','instagram','https://test/p3-unregistered.jpg','published',now())")
    result = activate(key)
    assert result.returncode != 0
    assert re.search(r'unknown, ambiguous or review-pending media identity', result.stderr)
    assert sql(f"select count(*) from public.gym_visual_guard_settings where gym_id={q(tid)}").stdout.strip() == '0'
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(tid)}").stdout.strip() == '0'


def test_live_shared_graph_lock_holder_does_not_block_activation(cluster):
    # G is taken SHARED by activation: ordinary G(shared) entrants must not
    # deadlock with it; only an exclusive G owner or any C owner refuses.
    key, tid = tenant()
    armed(key, tid)
    name = 'p3_graph_shared_' + uuid.uuid4().hex
    proc = session_holding(
        f"select pg_advisory_xact_lock_shared(hashtextextended('{GRAPH_KEY}',0));", name)
    try:
        wait_until_sleeping(name)
        result = activate(key)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip())['enforced'] is True
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()


if os.environ.get('RUN_VISUAL_P3_STANDALONE'):
    # Optional direct-run entry: python tests/test_visual_group_activation_p3_guard_pg.py
    raise SystemExit(pytest.main([__file__, '-x', '-q']))
