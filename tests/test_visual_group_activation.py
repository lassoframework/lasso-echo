"""Transactional activation RPC: static checks + real PostgreSQL harness.

Real tests require psql and VISUAL_GROUP_TEST_DSN (libpq keyword DSN, absolute
Unix socket host, dbname=echo_visual_ledger_test), exactly as in
tests/test_visual_group_concurrency.py. They are skipped without it; never
point them at a network or production database.

Review-aligned expectations (2026-10-02, Sol's SQL repair wave):
- Backfill never self-registers unknown published media: every published or
  ambiguous media URL must be independently pre-registered before activation,
  otherwise activation refuses and rolls everything back.
- Known-group published history with two distinct post dates refuses
  activation (per-published-row date parity); rows with a NULL post_date are
  counted separately and also refuse.
- Auxiliary mutators (alias registration, scene link, backfill) take one
  common canonical tenant advisory; activation takes pg_try_advisory_xact_lock
  AFTER the calendar barrier and fails closed on contention, so a bounded
  caller retry succeeds once the contender commits.
- member_event is append-only: review holds close via a NEWER confirmation
  row, never via DELETE.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get('VISUAL_GROUP_TEST_DSN')
PSQL = shutil.which('psql')
ROOT = Path(__file__).parents[1]
MIGRATIONS = ROOT / 'migrations'
ACTIVATION = (MIGRATIONS / 'DRAFT_visual_group_activation_20261002.sql').read_text()
SCHEMA = (MIGRATIONS / 'DRAFT_visual_group_schema_20261002.sql').read_text()
BACKFILL = (MIGRATIONS / 'DRAFT_visual_group_backfill_20261002.sql').read_text()
LOW = ACTIVATION.lower()

# Message families Sol's repaired SQL raises. Regexes (not exact strings) so
# wording stays SQL-owner territory; behavior, not phrasing, is asserted.
CONTENTION = re.compile(r'contention|try again|temporaril|busy|lock not available|55P03', re.I)
DATE_PARITY = re.compile(r'date parity|cross-date occupied visual scene|published usage.*date|distinct.*date|more than one.*date', re.I)
NULL_DATE = re.compile(r'null.*(posting|post_?)?date|date.*null|missing.*post.*date|undated', re.I)

# --------------------------------------------------------------------------
# Static checks (run everywhere; no database)
# --------------------------------------------------------------------------


def test_activation_is_unapplied_draft_with_rollback():
    assert 'draft' in LOW and 'unapplied' in LOW and 'rollback' in LOW


def test_no_production_connections_or_provider_actions():
    for forbidden in ('supabase.co', 'dblink(', 'http_post(', 'net.http', 'copy program'):
        assert forbidden not in LOW


def test_default_stays_off_and_no_direct_arming_statement():
    # The RPC arms only via guarded insert/on-conflict after a same-tx receipt;
    # nothing in this file changes the column default or arms unconditionally.
    assert 'default true' not in LOW
    assert 'update public.gym_visual_guard_settings set enforce' not in LOW
    assert 'on conflict (gym_id) do update set enforce = true' in LOW  # RPC-only path


def test_barrier_is_first_and_share_row_exclusive():
    assert 'lock table public.content_calendar in share row exclusive mode' in LOW
    body = LOW[LOW.index('create or replace function public.visual_group_activate_guard'):]
    assert body.index('lock table public.content_calendar') < body.index('pg_try_advisory_xact_lock')


def test_try_advisory_after_barrier_and_common_canonical_tenant_advisory():
    # P1 repair: activation fails closed instead of deadlock-waiting. The
    # try-lock is taken only after the calendar barrier.
    assert 'pg_try_advisory_xact_lock' in ACTIVATION
    body = LOW[LOW.index('create or replace function public.visual_group_activate_guard'):]
    assert body.index('lock table public.content_calendar') < body.index('pg_try_advisory_xact_lock')
    # Every auxiliary mutator participates in the SAME canonical tenant
    # advisory so alias/scene-link/backfill RPCs cannot race activation.
    assert 'pg_try_advisory_xact_lock' in SCHEMA  # shared helper uses TRY under the barrier
    for source, label in ((SCHEMA, 'schema'), (BACKFILL, 'backfill'), (ACTIVATION, 'activation')):
        assert "jsonb_build_array('visual_tenant'" in source, f'{label} lacks common tenant lock'


def test_stable_lock_order_and_per_alias_backfill():
    assert "jsonb_build_array('visual_tenant'" in ACTIVATION
    assert "jsonb_build_array('visual_backfill'" in ACTIVATION
    assert 'order by 1' in LOW  # sorted key iteration for advisory locks + backfill
    assert 'public.visual_group_backfill_gym(v_key, false)' in ACTIVATION  # real, per-key


def test_fresh_read_committed_required():
    assert "current_setting('transaction_isolation') <> 'read committed'" in LOW


def test_all_refusal_paths_raise_inside_transaction():
    for needle in ('unknown, ambiguous or review-pending media identity',
                   'missing permanent published usage ledger coverage',
                   'missing dated ledger reservation or active sibling coverage',
                   'unresolved historical review events',
                   'unresolved ambiguous usage',
                   'cross-date occupied visual scene',
                   'unmapped or foreign tenant calendar key'):
        assert needle in LOW
    # Every refusal is a raise inside the RPC: the caller's transaction rolls
    # back alias registrations, backfill writes, receipt and settings write.
    assert LOW.count("using errcode = '23514'") >= 7


def test_direct_arming_denied_without_same_tx_receipt():
    assert 'create table if not exists public.visual_group_activation' in LOW
    assert 'a.transaction_id = txid_current()' in ACTIVATION
    assert 'arming the visual guard requires the transactional activation rpc' in LOW
    assert 'create or replace function public.visual_group_settings_arm_guard()' in ACTIVATION


def test_fresh_alias_blocked_under_armed_tenant():
    assert 'create trigger tenant_alias_arm_guard before insert on public.tenant_alias' in LOW
    assert 'requires disarm and reactivation with fresh coverage proof' in LOW


def test_service_role_only_surface():
    assert 'revoke all on function public.visual_group_activate_guard(text,text) from public,anon,authenticated' in LOW
    assert 'grant execute on function public.visual_group_activate_guard(text,text) to service_role' in LOW
    assert 'revoke all on public.visual_group_activation from public,anon,authenticated,service_role' in LOW
    assert 'create policy visual_group_activation_service_read' in LOW


def test_original_pr230_functions_are_not_redefined():
    assert 'create or replace function public.claim_calendar_publish_slot_owned' not in LOW
    assert 'create or replace function public.approve_calendar_row_if_media_ready' not in LOW


# --------------------------------------------------------------------------
# Real PostgreSQL harness (skipped without the disposable local DSN)
# --------------------------------------------------------------------------

needs_db = pytest.mark.skipif(not DSN, reason='isolated local VISUAL_GROUP_TEST_DSN unset')


def q(value):
    return 'NULL' if value is None else "'" + str(value).replace("'", "''") + "'"


def sql(statement, role=None):
    prefix = f'set role {role}; ' if role else ''
    proc = subprocess.run([PSQL, '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                           '-d', DSN, '-c', prefix + statement], text=True, capture_output=True, timeout=30)
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()


def session_holding(statements, app_name, hold_seconds=3):
    """Open an independent psql session that runs statements in one transaction,
    holds them through pg_sleep, then commits. Returns the live Popen."""
    proc = subprocess.Popen([PSQL, '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-d', DSN],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    proc.stdin.write(f"set application_name={q(app_name)};\n"
                     f"begin;\n{statements}\n"
                     f"select pg_sleep({hold_seconds});\ncommit;\n")
    proc.stdin.flush()
    return proc


def wait_until_sleeping(app_name, deadline_seconds=10):
    deadline = time.monotonic() + deadline_seconds
    while True:
        state = sql("select coalesce((select state||':'||coalesce(wait_event,'') from pg_stat_activity "
                    f"where application_name={q(app_name)}),'absent')")
        if state.endswith(':PgSleep'):
            return
        assert time.monotonic() < deadline, f'contender session never reached sleep (state={state})'
        assert not state.startswith('idle'), f'contender transaction ended early (state={state})'
        time.sleep(.02)


@pytest.fixture(scope='module')
def db():
    if not DSN:
        pytest.skip('local DSN unset')
    assert PSQL, 'psql required'
    assert re.fullmatch(r'host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_visual_ledger_test(?: user=[A-Za-z0-9_-]+)?', DSN), 'only named disposable Unix-socket DB allowed'
    assert sql('select current_database()') == 'echo_visual_ledger_test'
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql('drop schema public cascade; create schema public; grant usage on schema public to anon,authenticated,service_role;')
    sql("""create table public.content_calendar(
      id uuid primary key default gen_random_uuid(), gym_id text,
      post_date date, status text not null default 'draft' check(status in ('draft','pending','approved','published','denied','killed','failed','publishing','deleted','coach_review')), account text,
      format text, variant_status text not null default 'active' check(variant_status in ('active','candidate','archived')), image_url text,
      source_media_url text,source_media_asset_id text,drive_file_id text,byte_hash text,r2_key text,
      media_not_ready_reason text,published_at timestamptz,late_post_id text,
      publish_reservation_day date,publish_claim_token uuid,created_at timestamptz not null default now()
    ); grant select,insert,update,delete on public.content_calendar to service_role;""")
    for filename in ['DRAFT_visual_group_schema_20261002.sql',
                     'DRAFT_visual_group_claim_trigger_20261002.sql',
                     'DRAFT_visual_group_backfill_20261002.sql',
                     'calendar_claim_media_guard_20261002.sql',
                     'DRAFT_visual_group_activation_20261002.sql']:
        sql((MIGRATIONS / filename).read_text())
    yield


def tenant():
    name = 'g_' + uuid.uuid4().hex
    tid = str(uuid.uuid4())
    sql(f'select public.visual_group_tenant_register({q(name)},{q(tid)}::uuid)')
    return name, tid


def activate(key, actor='tester'):
    return json.loads(sql(f'select public.visual_group_activate_guard({q(key)},{q(actor)})'))


def enforced(tid):
    return sql(f"select coalesce((select enforce::text from public.gym_visual_guard_settings where gym_id={q(tid)}),'false')")


def counts(tid):
    out = {}
    for table in ('visual_group', 'visual_group_alias', 'visual_group_usage_ledger',
                  'visual_group_usage_sibling', 'visual_group_member_event'):
        out[table] = int(sql(f'select count(*) from public.{table} where gym_id={q(tid)}'))
    return out


@needs_db
def test_real_role_denial(db):
    key, _ = tenant()
    for role in ('anon', 'authenticated'):
        with pytest.raises(RuntimeError, match='permission denied'):
            sql(f'select public.visual_group_activate_guard({q(key)})', role=role)


@needs_db
def test_real_clean_activation_and_idempotent_recall(db):
    key, tid = tenant()
    sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/arm.jpg')")
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status) values({q(key)},'2026-10-05','instagram','https://test/arm.jpg','pending')")
    proof = activate(key)
    assert proof['enforced'] is True and proof['tenant'] == tid and proof['calendar_rows'] == 1
    assert enforced(tid) == 'true'
    again = activate(key)
    assert again['idempotent'] is True
    # Armed tenants refuse NEW calendar alias keys until disarm + reactivation.
    with pytest.raises(RuntimeError, match='new calendar alias key requires disarm'):
        sql(f"select public.visual_group_tenant_register('k_{uuid.uuid4().hex}',{q(tid)}::uuid)")
    # Direct re-arming after disarm is denied without a fresh receipt.
    sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={q(tid)}')
    with pytest.raises(RuntimeError, match='transactional activation RPC'):
        sql(f'update public.gym_visual_guard_settings set enforce=true where gym_id={q(tid)}')
    # A fresh RPC re-activation (coverage still clean) arms again.
    assert activate(key)['enforced'] is True


@needs_db
def test_real_unknown_media_refusal_rolls_back_everything(db):
    key, tid = tenant()
    before_aliases = sql(f'select count(*) from public.tenant_alias where tenant_id={q(tid)}')
    # Published row whose URL was never registered: backfill must NOT
    # self-register it; activation refuses and the whole tx rolls back.
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status,published_at) values({q(key)},'2026-10-05','instagram','https://test/unregistered.jpg','published',now())")
    with pytest.raises(RuntimeError, match='unknown, ambiguous or review-pending media identity'):
        activate(key)
    assert enforced(tid) == 'false'
    assert counts(tid) == {t: 0 for t in ('visual_group', 'visual_group_alias', 'visual_group_usage_ledger',
                                          'visual_group_usage_sibling', 'visual_group_member_event')}
    assert sql(f'select count(*) from public.tenant_alias where tenant_id={q(tid)}') == before_aliases
    assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}') == '0'
    # Calendar backfill markers rolled back too.
    assert sql(f"select count(*) from public.content_calendar where gym_id={q(key)} and visual_group_key is not null") == '0'
    # Independent pre-registration makes this published row eligible for review.
    sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/unregistered.jpg')")
    assert activate(key)['enforced'] is True


@needs_db
def test_real_held_unknown_media_cannot_become_ready_during_activation(db):
    key, tid = tenant()
    row_id = sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status,media_not_ready_reason) "
                 f"values({q(key)},'2026-10-05','instagram','https://test/held-unknown.jpg','pending',"
                 f"'visual_group_identity_unresolved') returning id")
    with pytest.raises(RuntimeError, match='unknown|unresolved|review-pending'):
        activate(key)
    assert enforced(tid) == 'false'
    assert sql(f"select media_not_ready_reason from public.content_calendar where id={q(row_id)}") == 'visual_group_identity_unresolved'
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(tid)}") == '0'
    assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}') == '0'


@needs_db
def test_real_registered_two_date_published_history_refuses_activation(db):
    key, tid = tenant()
    # Pre-registered media, KNOWN group, but two published rows on two dates:
    # per-row date parity must refuse even though the identity is registered.
    group = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/hist.jpg')")
    for date, account in (('2026-10-05', 'instagram'), ('2026-10-06', 'facebook')):
        sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status,published_at) "
            f"values({q(key)},{q(date)},{q(account)},'https://test/hist.jpg',{q(group)},'published',now())")
    with pytest.raises(RuntimeError, match=DATE_PARITY):
        activate(key)
    assert enforced(tid) == 'false'
    assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}') == '0'


@needs_db
def test_real_null_post_date_published_row_refuses_activation(db):
    key, tid = tenant()
    # A published row with no post_date is counted separately and can never
    # satisfy date parity; activation must refuse, not silently arm.
    sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/nodate.jpg')")
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status,published_at) "
        f"values({q(key)},NULL,'instagram','https://test/nodate.jpg','published',now())")
    with pytest.raises(RuntimeError, match=NULL_DATE):
        activate(key)
    assert enforced(tid) == 'false'


@needs_db
def test_real_review_hold_append_only_closed_by_newer_confirmation(db):
    key, tid = tenant()
    group = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/hold.jpg')")
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) "
        f"values({q(key)},'2026-10-05','instagram','https://test/hold.jpg',{q(group)},'pending')")
    # Historical unresolved review hold on this identity blocks activation.
    sql(f"insert into public.visual_group_member_event(gym_id,group_key,alias_kind,alias_value,action,actor) "
        f"values({q(tid)},{q(group)},'canonical_url','https://test/hold.jpg','review_hold','backfill_published_review')")
    with pytest.raises(RuntimeError, match='review-pending|unresolved historical review events'):
        activate(key)
    assert enforced(tid) == 'false'
    events_before = counts(tid)['visual_group_member_event']
    assert events_before >= 1
    # Append-only resolution: a NEWER confirmation row closes the hold.
    # member_event must never be DELETE'd to clear a hold.
    sql(f"select public.visual_group_confirm({q(key)},{q(group)},'canonical_url','https://test/hold.jpg','reviewer-1')")
    events_after = counts(tid)['visual_group_member_event']
    assert events_after == events_before + 1, 'confirmation must append, never rewrite history'
    assert activate(key)['enforced'] is True
    assert enforced(tid) == 'true'
    # The hold row itself is still present as durable audit evidence.
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(tid)} and action='review_hold'") == '1'


@needs_db
def test_real_unmapped_key_cannot_activate(db):
    with pytest.raises(RuntimeError, match='no canonical tenant mapping'):
        activate('zz-retired-' + uuid.uuid4().hex)


@needs_db
def test_real_barrier_serializes_with_calendar_writer(db):
    key, tid = tenant()
    sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/lock.jpg')")
    # Pre-register every media URL this test introduces (backfill no longer
    # self-registers unknown media), including the concurrent writer's row.
    sql(f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/lock2.jpg')")
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status) values({q(key)},'2026-10-05','instagram','https://test/lock.jpg','pending')")
    name = 'act_writer_' + uuid.uuid4().hex
    errors = []

    def writer():
        try:
            sql(f"begin; set application_name={q(name)}; insert into public.content_calendar(gym_id,post_date,account,image_url,status) values({q(key)},'2026-10-07','story','https://test/lock2.jpg','draft'); select pg_sleep(2); commit;")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    thread = threading.Thread(target=writer)
    thread.start()
    deadline = time.monotonic() + 5
    while sql(f"select count(*) from pg_stat_activity where application_name={q(name)} and wait_event='PgSleep'") != '1':
        assert time.monotonic() < deadline, 'writer never reached sleep with its ROW EXCLUSIVE lock'
        time.sleep(.02)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r'55P03|could not obtain lock'):
        activate(key)  # NOWAIT must refuse while the calendar writer holds ROW EXCLUSIVE.
    elapsed = time.monotonic() - started
    thread.join(timeout=15)
    assert not thread.is_alive() and errors == []
    assert elapsed < 1.5, f'activation waited on the in-flight calendar writer ({elapsed:.2f}s)'
    assert enforced(tid) == 'false', 'failed activation must not partially arm the tenant'
    proof = activate(key)
    assert proof['enforced'] is True and proof['calendar_rows'] == 2


@needs_db
def test_real_tenant_advisory_contention_fails_closed_then_retries(db):
    key, tid = tenant()
    url = 'https://test/race.jpg'
    group = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url)})")
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) "
        f"values({q(key)},'2026-10-05','instagram',{q(url)},{q(group)},'pending')")
    # An uncommitted alias mutation for the SAME tenant holds the canonical
    # tenant advisory. Activation must fail closed immediately (try-lock), not
    # wait 3s and deadlock.
    name = 'act_alias_contender_' + uuid.uuid4().hex
    proc = session_holding(
        f"select public.visual_group_register_alias({q(key)},'canonical_url','https://test/race2.jpg');", name)
    try:
        wait_until_sleeping(name)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=CONTENTION):
            activate(key)
        elapsed = time.monotonic() - started
        assert elapsed < 2.5, f'activation waited on contention instead of failing closed ({elapsed:.2f}s)'
        assert enforced(tid) == 'false'
        # Nothing partial persisted from the failed attempt.
        assert sql(f'select count(*) from public.visual_group_activation where gym_id={q(tid)}') == '0'
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    # Bounded retry after the contender commits succeeds.
    assert activate(key)['enforced'] is True


@needs_db
def test_real_backfill_lock_contention_fails_closed_then_retries(db):
    key, tid = tenant()
    url = 'https://test/bflock.jpg'
    group = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url)})")
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) "
        f"values({q(key)},'2026-10-05','instagram',{q(url)},{q(group)},'pending')")
    # Hold only the per-key advisory. A full backfill also writes calendar
    # rows, so activation correctly waits at its table barrier in that case.
    # This isolates the post-barrier TRY path instead of conflating it with
    # calendar writer serialization.
    name = 'act_backfill_contender_' + uuid.uuid4().hex
    lock_stmt = ("select pg_advisory_xact_lock(hashtextextended("
                 f"jsonb_build_array('visual_backfill',{q(key)})::text,0));")
    proc = session_holding(lock_stmt, name)
    try:
        wait_until_sleeping(name)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=CONTENTION):
            activate(key)
        elapsed = time.monotonic() - started
        assert elapsed < 2.5, f'activation queued on backfill lock instead of failing closed ({elapsed:.2f}s)'
        assert enforced(tid) == 'false'
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    assert activate(key)['enforced'] is True


@needs_db
def test_real_auxiliary_rpc_with_prior_calendar_for_update_uses_try_lock(db):
    key, tid = tenant()
    url = 'https://test/row-lock.jpg'
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status) "
        f"values({q(key)},'2026-10-05','instagram',{q(url)},'pending')")
    # Another transaction owns the canonical auxiliary mutex without touching
    # the calendar. This transaction takes a calendar tuple lock first, as a
    # service caller could before invoking registration. Waiting on the mutex
    # would invert activation's barrier -> mutex -> row order.
    name = 'act_aux_contender_' + uuid.uuid4().hex
    lock_stmt = ("select pg_advisory_xact_lock(hashtextextended("
                 f"jsonb_build_array('visual_tenant',{q(tid)})::text,0));")
    proc = session_holding(lock_stmt, name)
    try:
        wait_until_sleeping(name)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='auxiliary visual lock busy'):
            sql("begin; set local statement_timeout='1500ms'; "
                f"select id from public.content_calendar where gym_id={q(key)} for update; "
                f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url)}); commit;")
        assert time.monotonic() - started < 1.2
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    assert sql(f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url)})")


@needs_db
def test_real_scene_link_race_fails_closed_then_retries(db):
    key, tid = tenant()
    url_a, url_b = 'https://test/scene_a.jpg', 'https://test/scene_b.jpg'
    group_a = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url_a)})")
    group_b = sql(f"select public.visual_group_register_alias({q(key)},'canonical_url',{q(url_b)})")
    for url, group in ((url_a, group_a), (url_b, group_b)):
        sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) "
            f"values({q(key)},'2026-10-05','instagram',{q(url)},{q(group)},'pending')")
    # An uncommitted same-scene union (human-evidenced) holds the canonical
    # tenant advisory; activation must fail closed and win on retry.
    name = 'act_scene_contender_' + uuid.uuid4().hex
    proc = session_holding(
        f"select public.visual_group_link_scene({q(key)},{q(group_a)},{q(group_b)},"
        f"'{{\"note\":\"race-test\"}}'::jsonb,'reviewer-1');", name)
    try:
        wait_until_sleeping(name)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=CONTENTION):
            activate(key)
        elapsed = time.monotonic() - started
        assert elapsed < 2.5, f'activation raced the scene link instead of failing closed ({elapsed:.2f}s)'
        assert enforced(tid) == 'false'
    finally:
        proc.communicate(timeout=30)
        assert proc.returncode == 0, proc.stdout.read()
    assert activate(key)['enforced'] is True


@needs_db
def test_real_cross_tenant_isolation(db):
    key_a, tid_a = tenant()
    key_b, tid_b = tenant()
    sql(f"insert into public.content_calendar(gym_id,post_date,account,image_url,status,published_at) values({q(key_b)},'2026-10-01','instagram','https://test/foreign.jpg','published',now())")
    # Tenant A has no rows at all and activates cleanly.
    assert activate(key_a)['enforced'] is True
    assert enforced(tid_a) == 'true'
    # Tenant B is untouched: still OFF and its unknown-media row blocks ITS activation.
    assert enforced(tid_b) == 'false'
    with pytest.raises(RuntimeError, match='unknown, ambiguous or review-pending media identity'):
        activate(key_b)
    assert enforced(tid_b) == 'false'
    # A's receipt never authorizes arming B.
    with pytest.raises(RuntimeError, match='transactional activation RPC|unmapped calendar key'):
        sql(f'insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid_b)},true)')
