"""Static release-boundary checks; behavior is tested on real PostgreSQL.

These checks deliberately avoid claiming SQL text shape proves concurrency.
"""
from pathlib import Path

ROOT = Path(__file__).parents[1]
NAMES = ['schema', 'claim_trigger', 'backfill']


def migration(name):
    return (ROOT / 'migrations' / f'DRAFT_visual_group_{name}_20261002.sql').read_text()


def test_all_migrations_are_unapplied_drafts_with_rollback():
    for name in NAMES:
        text = migration(name).lower()
        assert 'draft' in text and 'unapplied' in text and 'rollback' in text


def test_schema_default_off_no_arming_statement():
    text = migration('schema').lower()
    assert 'enforce    boolean     not null default false' in text
    for name in NAMES:
        text = migration(name).lower()
        assert 'set enforce=true' not in text and 'set enforce = true' not in text


def test_no_production_connections_or_provider_actions():
    for name in NAMES:
        text = migration(name).lower()
        for forbidden in ('supabase.co', 'dblink(', 'http_post(', 'net.http', 'copy program'):
            assert forbidden not in text


def test_original_pr230_functions_are_not_redefined():
    for name in NAMES:
        text = migration(name).lower()
        assert 'create or replace function public.claim_calendar_publish_slot_owned' not in text
        assert 'create or replace function public.approve_calendar_row_if_media_ready' not in text


def test_real_tests_use_baseline_calendar_columns_and_refuse_network_dsn():
    text = (ROOT / 'tests' / 'test_visual_group_concurrency.py').read_text()
    assert 'scheduled_date' not in text.replace('NO scheduled_date/channel substitute columns.', '')
    assert "'calendar_claim_media_guard_20261002.sql'" in text
    assert 'only named disposable Unix-socket DB allowed' in text
    assert 'dbname=echo_visual_ledger_test' in text


def test_canonical_tenant_alias_mapping_is_service_role_only_and_fail_closed():
    schema = migration('schema').lower()
    assert 'create table if not exists public.tenant_alias' in schema
    assert 'tenant_alias  uuid        not null' not in schema  # sanity: column is tenant_id
    assert 'tenant_id  uuid        not null' in schema
    assert 'create policy tenant_alias_service_role' in schema
    assert "revoke all on public.tenant_alias from public,anon,authenticated,service_role" in schema
    # resolver fails closed (null) and strict paths raise for unmapped keys
    assert 'has no canonical tenant mapping' in schema
    assert 'unmapped calendar key cannot arm the visual guard' in schema
    # bindings immutable: tenant_alias is in the identity-immutable loop
    assert "'tenant_alias'" in schema


def test_internal_tables_use_canonical_key_calendar_keeps_raw_alias():
    trig = migration('claim_trigger')
    back = migration('backfill')
    for text in (trig, back):
        assert 'visual_group_tenant_id' in text
    # raw content_calendar.gym_id is preserved (no rewrites of the column)
    schema = migration('schema').lower()
    assert 'alter table public.content_calendar' in schema
    assert 'visual_group_tenant_strict' in trig and 'visual_group_tenant_strict' in back


def test_scene_union_is_manual_persistent_and_service_role_only():
    schema = migration('schema')
    low = schema.lower()
    assert 'create table if not exists public.visual_group_scene_link' in low
    assert 'check (group_key_a < group_key_b)' in low
    # Append-only evidence: immutable trigger loop covers links; no reassign/delete.
    assert "'visual_group_scene_link'" in low
    assert "'scene_linked'" in low
    # Human-confirmed only, never pHash inference.
    assert 'never inferred from phash' in low or 'never inferred from pHash'.lower() in low
    assert 'jck_6328' in low and 'distance 28' in low
    # Same-tenant only + evidence/actor required.
    assert 'must both be registered under this canonical tenant' in low
    assert 'human confirmation evidence is required' in low
    # Service-role only surface.
    assert 'create policy visual_group_scene_link_service_role' in low
    assert 'revoke all on public.visual_group_scene_link from public,anon,authenticated,service_role' in low
    assert 'grant execute on function public.visual_group_link_scene(text,text,text,jsonb,text) to service_role' in low
    assert 'grant execute on function public.visual_group_scene_members(text,text) to service_role' in low


def test_scene_union_claim_authority_and_conflict_reporting():
    trig = migration('claim_trigger')
    back = migration('backfill')
    # Claim, swap and confirmed-delivery paths consult the linked component.
    assert trig.count('visual_group_scene_members') >= 4
    assert 'linked visual scene is used on another date' in trig
    assert 'confirmed scene date conflicts with linked scene component' in trig
    # Backfill holds unsafe new rows under linked scene authority.
    assert 'linked_scene_cross_date_hold' in back
    # Conflicts are reported and block activation; history is never rewritten.
    assert 'scene_cross_date_conflicts' in back
    assert "'activation_ready',false" in back.replace(' ', '')
    assert 'owner review of reported scene cross-date conflicts' in back


def test_component_locks_precede_endpoint_locks_and_are_private():
    schema = migration('schema')
    trig = migration('claim_trigger')
    assert 'visual_group_lock_scene_components' in schema
    assert "order by component_group.gym_id,component_group.group_key" in schema
    assert "scene component changed while locking; retry transaction" in schema
    assert 'v_iterations' not in schema  # no retry while retaining stale locks
    assert 'revoke all on function public.visual_group_lock_scene_components(jsonb) from public,anon,authenticated,service_role' in schema
    # Guard, sync, media swap, date swap and reconcile all use complete closure.
    assert trig.count('perform public.visual_group_lock_scene_components') == 5
    assert 'visual identity changed while locking; retry transaction' in trig
    assert 'order by gym_id,group_key for update' not in trig


def test_published_metadata_repair_is_evidence_only_and_union_refuses_armed_conflict():
    schema = migration('schema')
    trig = migration('claim_trigger')
    assert "(to_jsonb(new)-'ambiguous') = (to_jsonb(old)-'ambiguous')" in schema
    assert 'public.visual_group_group_reconciled(old.gym_id,old.group_key)' in schema
    assert "tg_op = 'UPDATE' and new.state = 'published' and old.state = 'published'" not in schema
    assert "state='published' and ambiguous" in trig
    union = schema[schema.index('create or replace function public.visual_group_link_scene('):]
    assert union.index('armed tenant scene union conflicts') < union.index('insert into public.visual_group_scene_link')


# These cases share the existing disposable-DB validation/setup implementation,
# but only explicit real tests request it. Static release checks never connect.
import importlib.util
import json
import threading
import time
import uuid

import pytest


@pytest.fixture(scope='module')
def scene_database():
    spec = importlib.util.spec_from_file_location('_visual_scene_pg_harness', ROOT / 'tests' / 'test_visual_group_concurrency.py')
    pg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pg)
    setup = pg.database.__wrapped__()
    next(setup)
    try:
        yield pg
    finally:
        next(setup, None)


@pytest.mark.parametrize('orphan_unsent', [False, True])
def test_real_confirmed_delivery_clears_only_resolved_uncertainty(scene_database, orphan_unsent):
    pg = scene_database
    g = pg.gym(); group = pg.alias(g, 'https://test/one.jpg')
    sent = pg.uncertain(g, group)
    unsent = pg.uncertain(g, group, channel='story') if orphan_unsent else None
    proof = pg.evidence(g, sent, group, outcome='confirmed_published')
    pg.reconcile(g, sent, group, proof, outcome='confirmed_published', role='service_role')
    frozen = pg.ledger(g)[0]
    assert frozen['state'] == 'published' and frozen['ambiguous'] is orphan_unsent
    # Direct writes cannot downgrade/re-date permanent usage or clear any
    # remaining uncertainty without the last sibling's exact terminal evidence.
    for change in ["state='released'", "reserved_date='2026-10-06'", "published_at=now()"]:
        with pytest.raises(RuntimeError, match='immutable|evidence-based'):
            pg.sql(f'update public.visual_group_usage_ledger set {change} where gym_id={pg.q(pg.canon(g))}')
    if unsent:
        with pytest.raises(RuntimeError, match='evidence-based'):
            pg.sql(f'update public.visual_group_usage_ledger set ambiguous=false where gym_id={pg.q(pg.canon(g))}')
        proof = pg.evidence(g, unsent, group)
        pg.sql(f'delete from public.content_calendar where id={pg.q(unsent)}')
        pg.reconcile(g, unsent, group, proof, role='service_role')
        final = pg.ledger(g)[0]
        assert final == {**frozen, 'ambiguous': False}
    pg.sql(f'delete from public.content_calendar where gym_id={pg.q(g)}')
    assert pg.ledger(g)[0]['state'] == 'published'
    assert pg.ledger(g)[0]['ambiguous'] is False
    with pytest.raises(RuntimeError):
        pg.insert(g, group, date='2026-10-06')


@pytest.mark.parametrize('unknown_date', [False, True])
def test_real_armed_union_refuses_conflict_and_off_history_reports(scene_database, unknown_date):
    pg = scene_database
    g = pg.gym(); a = pg.alias(g, 'https://test/one.jpg'); b = pg.alias(g, 'https://test/two.jpg')
    r = pg.insert(g, a)
    pg.sql(f"update public.content_calendar set status='published',published_at=now() where id={pg.q(r)}")
    if unknown_date:
        # Exact representation of immutable undated historical publication.
        pg.sql(f"insert into public.visual_group_usage_ledger(gym_id,group_key,reserved_date,state,published_at) values({pg.q(pg.canon(g))},{pg.q(b)},null,'published',now())")
    else:
        pg.insert(g, b, date='2026-10-06', url='https://test/two.jpg')
    frozen = pg.ledger(g); calendar = pg.rows(g)
    with pytest.raises(RuntimeError, match='armed tenant scene union conflicts'):
        pg.link(g, a, b)
    assert pg.links(g) == [] and pg.ledger(g) == frozen and pg.rows(g) == calendar
    assert pg.sql(f"select count(*) from public.visual_group_member_event where gym_id={pg.q(pg.canon(g))} and action='scene_linked'") == '0'
    pg.sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={pg.q(pg.canon(g))}')
    result = pg.link(g, a, b)
    assert result['linked'] is True and pg.ledger(g) == frozen
    if not unknown_date:
        assert result['cross_date_conflicts'] == ['2026-10-05', '2026-10-06']
        report = json.loads(pg.sql(f'select public.visual_group_conflict_report({pg.q(g)})'))
        assert report['activation_ready'] is False and len(report['scene_cross_date_conflicts']) == 1
    else:
        assert sorted(result['published_group_keys']) == sorted([a, b])


@pytest.mark.parametrize('operation', ['reconcile', 'guard'])
def test_real_component_lock_order_does_not_invert_endpoint_then_member(scene_database, operation):
    pg = scene_database
    g = pg.gym()
    a = pg.alias(g, 'https://test/two.jpg', 'a_scene')
    b = pg.alias(g, 'https://test/one.jpg', 'z_scene')
    pg.link(g, a, b)
    rid = pg.uncertain(g, b) if operation == 'reconcile' else pg.insert(g, b)
    statement = pg.reconcile_statement(g, rid, b, pg.evidence(g, rid, b, outcome='confirmed_published'), outcome='confirmed_published') if operation == 'reconcile' else f"update public.content_calendar set status='approved' where id={pg.q(rid)}"
    tenant = pg.canon(g); name = 'scene_lock_' + uuid.uuid4().hex
    # Force the other transaction to hold the LOWER member first. Previously
    # reconcile/guard took z_scene before waiting for a_scene; this holder then
    # waited for z_scene, producing a real PostgreSQL deadlock.
    holder = f"begin; set application_name={pg.q(name)}; set local statement_timeout='6s'; select 1 from public.visual_group where gym_id={pg.q(tenant)} and group_key={pg.q(a)} for update; select pg_sleep(1); select 1 from public.visual_group where gym_id={pg.q(tenant)} and group_key={pg.q(b)} for update; commit;"
    errors = []
    def run_holder():
        try:
            pg.sql(holder)
        except Exception as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=run_holder); thread.start()
    deadline = time.monotonic() + 4
    while pg.sql(f"select count(*) from pg_stat_activity where application_name={pg.q(name)} and wait_event='PgSleep'") != '1':
        assert time.monotonic() < deadline, 'holder never reached lower-key lock'
        time.sleep(.02)
    try:
        pg.sql("begin; set local statement_timeout='6s'; " + statement + '; commit;')
    finally:
        thread.join(timeout=8)
    assert not thread.is_alive() and errors == []
    assert pg.rows(g)[0]['status'] == ('published' if operation == 'reconcile' else 'approved')


def test_real_component_growth_retries_without_adding_lower_locks(scene_database):
    pg = scene_database
    g = pg.gym()
    low = pg.alias(g, 'https://test/two.jpg', 'a_new_member')
    high = pg.alias(g, 'https://test/one.jpg', 'z_original_member')
    tenant = pg.canon(g); name = 'scene_growth_' + uuid.uuid4().hex
    union = f"select public.visual_group_link_scene({pg.q(g)},{pg.q(low)},{pg.q(high)},'{{\"basis\":\"human\"}}'::jsonb,'reviewer')"
    holder = f"begin; set application_name={pg.q(name)}; set local statement_timeout='6s'; select 1 from public.visual_group where gym_id={pg.q(tenant)} and group_key={pg.q(high)} for update; select pg_sleep(1); {union}; commit;"
    errors = []
    def run_holder():
        try:
            pg.sql(holder)
        except Exception as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=run_holder); thread.start()
    deadline = time.monotonic() + 4
    while pg.sql(f"select count(*) from pg_stat_activity where application_name={pg.q(name)} and wait_event='PgSleep'") != '1':
        assert time.monotonic() < deadline, 'holder never reached original-member lock'
        time.sleep(.02)
    try:
        with pytest.raises(RuntimeError, match='scene component changed while locking; retry transaction'):
            pg.insert(g, high)
    finally:
        thread.join(timeout=8)
    assert not thread.is_alive() and errors == []
    assert pg.rows(g) == [] and pg.ledger(g) == [] and len(pg.links(g)) == 1
    # Retrying after rollback sees the stable complete component and succeeds.
    pg.insert(g, high)
    assert len(pg.rows(g)) == len(pg.ledger(g)) == 1


def test_scene_lock_inputs_are_qualified_away_from_procedural_variables():
    trig = migration('claim_trigger')
    reconcile_sql = trig[trig.index('create or replace function public.visual_group_reconcile_ambiguous('):trig.index('-- RPCs are private')]
    assert 'from unnest(groups) scene_key(group_key)' in reconcile_sql
    assert "'group_key',scene_key.group_key" in reconcile_sql
    assert 'v_reconciled_group text' in reconcile_sql
    assert 'foreach v_reconciled_group in array groups loop' in reconcile_sql
    assert 'keys(k)' not in trig
    assert 'k text' not in reconcile_sql
