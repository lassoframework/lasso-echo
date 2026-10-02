"""Real isolated PostgreSQL tests, including independent connection races.

Requires installed psql and VISUAL_GROUP_TEST_DSN, a libpq keyword DSN with an
absolute Unix socket host and dbname=echo_visual_ledger_test. Never accepts a
network host/URL. The database is disposable: public is recreated once.
No production connection, credentials, provider or installed Python DB driver.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get('VISUAL_GROUP_TEST_DSN')
PSQL = shutil.which('psql')
ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(not DSN, reason='isolated local VISUAL_GROUP_TEST_DSN unset')


def q(value):
    return 'NULL' if value is None else "'" + str(value).replace("'", "''") + "'"


def sql(statement):
    proc = subprocess.run([PSQL, '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                           '-d', DSN, '-c', statement], text=True, capture_output=True, timeout=20)
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()


@pytest.fixture(scope='module', autouse=True)
def database():
    if not DSN:
        pytest.skip('local DSN unset')
    assert PSQL, 'psql required'
    # Validation happens BEFORE any connection or destructive fixture statement.
    assert re.fullmatch(r'host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_visual_ledger_test(?: user=[A-Za-z0-9_-]+)?', DSN), 'only named disposable Unix-socket DB allowed'
    assert sql('select current_database()') == 'echo_visual_ledger_test'
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql('drop schema public cascade; create schema public; grant usage on schema public to anon,authenticated,service_role;')
    # Fields and names used by the actual PR230 baseline functions, plus optional
    # source identity columns. NO scheduled_date/channel substitute columns.
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
                     'calendar_claim_media_guard_20261002.sql']:
        sql((ROOT / 'migrations' / filename).read_text())
    yield


def gym(enforce=True):
    name = 'g_' + uuid.uuid4().hex
    if enforce:
        sql(f'insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(name)},true)')
    return name


def alias(g, url, group=None, kind='canonical_url'):
    return sql(f'select public.visual_group_register_alias({q(g)},{q(kind)},{q(url)},{q(group)})')


def insert(g, group=None, date='2026-10-05', account='instagram', url='https://test/one.jpg', status='pending', extra=''):
    rid = str(uuid.uuid4())
    stmt = f"insert into public.content_calendar(id,gym_id,post_date,account,image_url,visual_group_key,status) values({q(rid)},{q(g)},{q(date)},{q(account)},{q(url)},{q(group)},{q(status)});"
    sql(stmt + extra)
    return rid


def rows(g):
    return json.loads(sql(f"select coalesce(jsonb_agg(to_jsonb(c) order by account),'[]') from public.content_calendar c where gym_id={q(g)}"))


def ledger(g):
    return json.loads(sql(f"select coalesce(jsonb_agg(to_jsonb(l)),'[]') from public.visual_group_usage_ledger l where gym_id={q(g)}"))


def race(a, b):
    barrier = threading.Barrier(2)
    out = [None, None]
    def run(i, statement):
        try:
            barrier.wait(timeout=5)
            out[i] = (sql("begin; set local statement_timeout='8s'; " + statement + "; select pg_sleep(0.1); commit;"), None)
        except Exception as exc:
            out[i] = (None, str(exc))
    threads = [threading.Thread(target=run,args=(i,s)) for i,s in enumerate([a,b])]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)
    assert all(not t.is_alive() for t in threads), 'race connection hung'
    assert all(item is not None for item in out)
    return out


def test_alias_race_no_orphan_and_stable_id():
    g = gym()
    call = f"select public.visual_group_register_alias({q(g)},'drive_id','drive_1',null)"
    result = race(call, call)
    assert all(err is None for _,err in result), result
    assert result[0][0] == result[1][0]
    group = result[0][0]
    assert sql(f'select count(*) from public.visual_group where gym_id={q(g)}') == '1'
    assert alias(g, 'drive_2', group, 'drive_id') == group
    with pytest.raises(RuntimeError): alias(g, 'drive_1','other','drive_id')
    with pytest.raises(RuntimeError): sql(f"update public.visual_group_alias set group_key='other' where gym_id={q(g)}")


def test_first_reservation_race_different_dates_one_winner():
    g=gym(); group=alias(g,'https://test/one.jpg')
    def statement(day):
        return f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) values({q(g)},{q(day)},'instagram','https://test/one.jpg',{q(group)},'pending')"
    result=race(statement('2026-10-05'),statement('2026-10-06'))
    assert sum(err is None for _,err in result)==1, result
    assert len(rows(g))==len(ledger(g))==1


def test_same_date_all_channels_after_first_publish():
    g=gym(); group=alias(g,'https://test/one.jpg')
    rid=insert(g,group)
    sql(f"update public.content_calendar set status='published',published_at=now() where id={q(rid)}")
    for account in ('facebook','story','google_business'):
        sibling=insert(g,group,account=account)
        sql(f"update public.content_calendar set status='publishing' where id={q(sibling)}")
        sql(f"update public.content_calendar set status='published',published_at=now() where id={q(sibling)}")
    assert len(rows(g))==4 and ledger(g)[0]['state']=='published'
    frozen=ledger(g)
    sql(f'delete from public.content_calendar where gym_id={q(g)}')
    assert ledger(g)==frozen
    with pytest.raises(RuntimeError): insert(g,group,date='2026-10-06')
    with pytest.raises(RuntimeError): sql(f'delete from public.visual_group_usage_ledger where gym_id={q(g)}')


def test_publish_finalize_cannot_bypass_identity_or_date():
    g=gym();group=alias(g,'https://test/one.jpg'); rid=insert(g,group)
    for changes in ["status='published',post_date='2026-10-06'", "status='published',post_date=null", "status='published',image_url='https://test/unknown.jpg'", "status='published',visual_group_key=null,image_url='https://test/unknown.jpg'"]:
        with pytest.raises(RuntimeError): sql(f'update public.content_calendar set {changes} where id={q(rid)}')
    assert rows(g)[0]['status']=='pending' and ledger(g)[0]['state']=='reserved'


def test_unknown_pending_hold_and_pr230_claim_rollback():
    g=gym(); rid=insert(g,url='https://test/unknown.jpg')
    assert rows(g)[0]['media_not_ready_reason']=='visual_group_identity_unresolved'
    assert sql(f"select public.claim_calendar_publish_slot_owned({q(rid)},{q(g)},'2026-10-05','UTC',2,false)")==''
    assert sql(f"select count(*) from public.approve_calendar_row_if_media_ready({q(rid)},{q(g)})")=='0'
    # Simulate stale approved row created with enforcement OFF. The PR230 RPC
    # pre-read sees no hold; BEFORE trigger must throw, never return its token.
    off=gym(False); stale=insert(off,status='approved',url='https://test/unknown.jpg')
    sql(f'insert into public.gym_visual_guard_settings values({q(off)},true,now())')
    with pytest.raises(RuntimeError): sql(f"select public.claim_calendar_publish_slot_owned({q(stale)},{q(off)},'2026-10-05','UTC',2,true)")
    assert rows(off)[0]['status']=='approved' and rows(off)[0]['publish_claim_token'] is None
    assert ledger(off)==[]


def test_null_date_pending_hold_not_invented_today():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group,date=None)
    assert rows(g)[0]['media_not_ready_reason']=='visual_group_date_unresolved' and ledger(g)==[]
    with pytest.raises(RuntimeError):sql(f"update public.content_calendar set status='published' where id={q(rid)}")


def test_stale_key_media_swap_and_hold_recovery():
    g=gym();one=alias(g,'https://test/one.jpg');rid=insert(g,one)
    sql(f"update public.content_calendar set image_url='https://test/two.jpg' where id={q(rid)}")
    assert rows(g)[0]['visual_group_key'] is None and rows(g)[0]['media_not_ready_reason']=='visual_group_identity_unresolved'
    assert ledger(g)[0]['state']=='released'
    two=alias(g,'https://test/two.jpg')
    sql(f'update public.content_calendar set visual_group_key={q(two)} where id={q(rid)}')
    assert rows(g)[0]['visual_group_key']==two and rows(g)[0]['media_not_ready_reason'] is None


def test_release_last_sibling_and_failed_ambiguous_retained():
    g=gym(); group=alias(g,'https://test/one.jpg')
    one=insert(g,group);two=insert(g,group,account='facebook')
    sql(f"update public.content_calendar set status='denied' where id={q(one)}")
    assert ledger(g)[0]['state']=='reserved'
    sql(f"update public.content_calendar set status='killed' where id={q(two)}")
    assert ledger(g)[0]['state']=='released'
    amb=insert(g,group,date='2026-10-06')
    sql(f"update public.content_calendar set status='failed' where id={q(amb)}")
    with pytest.raises(RuntimeError):sql(f"update public.content_calendar set status='killed' where id={q(amb)}")
    sql(f'delete from public.content_calendar where id={q(amb)}')
    assert ledger(g)[0]['state']=='reserved'
    with pytest.raises(RuntimeError): insert(g,group,date='2026-10-07')


def test_publish_vs_delete_other_sibling_realistic_race():
    g=gym(); group=alias(g,'https://test/one.jpg'); one=insert(g,group); two=insert(g,group,account='facebook')
    result=race(f"update public.content_calendar set status='published',published_at=now() where id={q(one)}",f'delete from public.content_calendar where id={q(two)}')
    assert all(err is None for _,err in result),result
    assert ledger(g)[0]['state']=='published'


def test_same_row_delete_vs_publish_only_confirmed_update_is_permanent():
    g=gym(); group=alias(g,'https://test/one.jpg'); one=insert(g,group)
    result=race(f"update public.content_calendar set status='published',published_at=now() where id={q(one)} returning id",f'delete from public.content_calendar where id={q(one)}')
    assert all(err is None for _,err in result),result
    # If DELETE won, UPDATE affected zero rows: no provider confirmation was
    # recorded and the test must not claim publication always wins.
    assert ledger(g)[0]['state']==('published' if result[0][0] else 'released')


def ids_array(ids):return 'array['+','.join(q(i)+'::uuid' for i in ids)+']'


def test_partial_sibling_redate_rejected_full_atomic_allowed():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group);two=insert(g,group,account='facebook')
    with pytest.raises(RuntimeError):sql(f"select public.visual_group_swap_redate({q(g)},{ids_array([one])},'2026-10-06')")
    assert {r['post_date'] for r in rows(g)}=={'2026-10-05'}
    assert sql(f"select public.visual_group_swap_redate({q(g)},{ids_array([one,two])},'2026-10-06')")=='2'
    assert {r['post_date'] for r in rows(g)}=={'2026-10-06'} and ledger(g)[0]['reserved_date']=='2026-10-06'


def test_bulk_media_swap_rolls_back_conflict_and_clears_old_aliases():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group);two=insert(g,group,account='facebook')
    new=alias(g,'https://test/new.jpg');other=insert(g,new,date='2026-10-07',url='https://test/new.jpg')
    media=q(json.dumps({'image_url':'https://test/new.jpg','visual_group_key':new}))+'::jsonb'
    with pytest.raises(RuntimeError):sql(f"select public.visual_group_swap_siblings({q(g)},{ids_array([one,two])},'2026-10-06',{media})")
    assert {r['post_date'] for r in rows(g) if r['id'] in [one,two]}=={'2026-10-05'}
    sql(f'delete from public.content_calendar where id={q(other)}')
    sql(f"select public.visual_group_swap_siblings({q(g)},{ids_array([one,two])},'2026-10-06',{media})")
    assert all(r['image_url']=='https://test/new.jpg' and r['visual_group_key']==new for r in rows(g))
    assert next(l for l in ledger(g) if l['group_key']==group)['state']=='released'


def test_cross_gym_isolation_and_default_off():
    one=gym();two=gym();g1=alias(one,'https://test/one.jpg');g2=alias(two,'https://test/one.jpg')
    insert(one,g1);insert(two,g2,date='2026-10-06')
    off=gym(False); insert(off,url='https://test/unknown.jpg',status='approved')
    assert ledger(off)==[] and rows(off)[0]['media_not_ready_reason'] is None


def backfill(g,dry=False):return json.loads(sql(f'select public.visual_group_backfill_gym({q(g)},{str(dry).lower()})'))


def test_backfill_history_active_future_unknown_and_dry_run_idempotence():
    g=gym(False)
    published=insert(g,date='2026-09-01',status='published') # NULL published_at
    sibling=insert(g,date='2026-09-01',account='facebook')
    repeat=insert(g,date='2026-10-06',account='google_business',status='approved')
    fresh=insert(g,date='2026-10-07',url='https://test/fresh.jpg')
    unknown=insert(g,date='2026-10-08',url=None)
    before=rows(g);report=backfill(g,True)
    assert rows(g)==before and ledger(g)==[] and report['rows_held_for_review']==2
    report=backfill(g)
    assert len(ledger(g))==2 and sum(l['state']=='published' for l in ledger(g))==1
    after=rows(g);pub=next(r for r in after if r['id']==published)
    assert pub==next(r for r in before if r['id']==published), 'published calendar history edited'
    later=next(r for r in after if r['id']==repeat)
    assert later['status']=='pending' and later['media_not_ready_reason']=='cross_date_media_repeat_needs_new_visual'
    assert next(r for r in after if r['id']==unknown)['media_not_ready_reason']=='visual_group_identity_unresolved'
    events=sql(f'select count(*) from public.visual_group_member_event where gym_id={q(g)}')
    backfill(g)
    assert rows(g)==after and sql(f'select count(*) from public.visual_group_member_event where gym_id={q(g)}')==events
    coverage=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert coverage['per_gym'][0]['uncovered_unheld_rows']==0


def test_unknown_historical_publish_no_fabricated_date_blocks_all_dates():
    g=gym(False);insert(g,date=None,status='published')
    backfill(g)
    assert ledger(g)[0]['state']=='published' and ledger(g)[0]['reserved_date'] is None
    sql(f'insert into public.gym_visual_guard_settings values({q(g)},true,now())')
    with pytest.raises(RuntimeError):insert(g,date='2026-10-06')


def test_backfill_alias_conflict_does_not_leave_partial_binding_or_orphan():
    g=gym(False);a=alias(g,'https://test/a.jpg');b=alias(g,'drive_b',kind='drive_id')
    rid=insert(g,url='https://test/a.jpg')
    sql(f"update public.content_calendar set drive_file_id='drive_b',byte_hash='brand_new_hash' where id={q(rid)}")
    backfill(g)
    assert rows(g)[0]['media_not_ready_reason']=='visual_group_alias_conflict'
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(g)} and alias_value='brand_new_hash'")=='0'
    assert sql(f'select count(*) from public.visual_group where gym_id={q(g)}')=='2'


def test_planner_rebuild_cannot_drop_cross_date_hold_or_upsert_old_media():
    g=gym();group=alias(g,'https://test/one.jpg');old=insert(g,group,date='2026-09-01')
    sql(f"update public.content_calendar set status='published',published_at=now() where id={q(old)}")
    # New row omits both group and hold: aliases still resolve the old visual,
    # so the database refuses the later-date planner rebuild atomically.
    with pytest.raises(RuntimeError):insert(g,date='2026-10-06',account='google_business')
    fresh=alias(g,'https://test/fresh.jpg');row=insert(g,fresh,date='2026-10-06',url='https://test/fresh.jpg')
    with pytest.raises(RuntimeError):sql(f"insert into public.content_calendar(id,gym_id,post_date,image_url) values({q(row)},{q(g)},'2026-10-06','https://test/one.jpg') on conflict(id) do update set image_url=excluded.image_url,visual_group_key=null,media_not_ready_reason=null")
    assert next(r for r in rows(g) if r['id']==row)['image_url']=='https://test/fresh.jpg'


def test_pr230_blank_story_and_specific_hold_preserved():
    g=gym();rid=insert(g,url=' ')
    assert sql(f"select public.claim_calendar_publish_slot_owned({q(rid)},{q(g)},'2026-10-05','UTC',2,false)")==''
    assert sql(f"select count(*) from public.approve_calendar_row_if_media_ready({q(rid)},{q(g)})")=='0'
    group=alias(g,'https://test/one.jpg');r=insert(g,group)
    sql(f"update public.content_calendar set media_not_ready_reason='story_render_failed' where id={q(r)}")
    with pytest.raises(RuntimeError):sql(f"update public.content_calendar set status='published',published_at=now() where id={q(r)}")
    assert next(x for x in rows(g) if x['id']==r)['media_not_ready_reason']=='story_render_failed'


def test_service_role_grants_anon_denied_review_event_and_group_immutability():
    g=gym();group=alias(g,'https://test/one.jpg')
    assert sql(f"set role service_role; select public.visual_group_confirm({q(g)},{q(group)},'manual_scene','JCK_6328_JCK_6331','reviewer')")
    sql(f"set role service_role; insert into public.visual_group_member_event(gym_id,action,reason) values({q(g)},'review_hold','unknown identity')")
    with pytest.raises(RuntimeError):sql(f"set role anon; select * from public.visual_group where gym_id={q(g)}")
    with pytest.raises(RuntimeError):sql(f"set role authenticated; select public.visual_group_register_alias({q(g)},'drive_id','private',null)")
    with pytest.raises(RuntimeError):sql(f"update public.visual_group set group_key='changed' where gym_id={q(g)}")
    assert sql("select has_table_privilege('service_role','public.visual_group_usage_ledger','TRUNCATE')")=='f'
    assert sql("select has_sequence_privilege('service_role','public.visual_group_member_event_id_seq','UPDATE')")=='f'


def test_phash_28_pair_not_merged_automatically():
    g=gym(False)
    insert(g,url='https://test/JCK_6328.jpg');insert(g,date='2026-10-06',url='https://test/JCK_6331.jpg')
    backfill(g)
    assert len({r['visual_group_key'] for r in rows(g)})==2


def test_committed_ledger_persists_across_fresh_connections():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group)
    sql(f"update public.content_calendar set status='published' where id={q(rid)}")
    # Every sql() opens a fresh backend, modeling worker restarts rather than
    # relying on process-local caches or connection-local state.
    assert sql(f"select state from public.visual_group_usage_ledger where gym_id={q(g)}")=='published'
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06')


def test_media_swap_cannot_be_blessed_by_unchanged_source_alias():
    g=gym();group=alias(g,'https://test/one.jpg');alias(g,'source_one',group,'source_asset')
    rid=insert(g,group)
    sql(f"update public.content_calendar set source_media_asset_id='source_one' where id={q(rid)}")
    sql(f"update public.content_calendar set image_url='https://test/unknown.jpg',visual_group_key=null,media_not_ready_reason=null where id={q(rid)}")
    assert rows(g)[0]['visual_group_key'] is None and rows(g)[0]['media_not_ready_reason']=='visual_group_identity_unresolved'
    assert ledger(g)[0]['state']=='released'


def test_variant_reactivation_date_conflict_and_draft_reserved():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group,status='draft')
    assert ledger(g)[0]['state']=='reserved'
    sql(f"update public.content_calendar set variant_status='archived' where id={q(one)}")
    assert ledger(g)[0]['state']=='released'
    insert(g,group,date='2026-10-06')
    with pytest.raises(RuntimeError):sql(f"update public.content_calendar set variant_status='active' where id={q(one)}")
    assert next(r for r in rows(g) if r['id']==one)['variant_status']=='archived'


def test_atomic_redate_racing_new_different_date_candidate_one_authority():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group);two=insert(g,group,account='facebook')
    result=race(f"select public.visual_group_swap_redate({q(g)},{ids_array([one,two])},'2026-10-06')",
      f"insert into public.content_calendar(gym_id,post_date,image_url,status) values({q(g)},'2026-10-07','https://test/one.jpg','pending')")
    assert result[0][1] is None and result[1][1] is not None,result
    assert {r['post_date'] for r in rows(g)}=={'2026-10-06'}


def test_two_workers_same_date_siblings_both_succeed():
    g=gym();group=alias(g,'https://test/one.jpg')
    stmt=lambda channel:f"insert into public.content_calendar(gym_id,post_date,account,image_url,status) values({q(g)},'2026-10-05',{q(channel)},'https://test/one.jpg','pending')"
    result=race(stmt('instagram'),stmt('facebook'))
    assert all(err is None for _,err in result),result
    assert len(rows(g))==2 and len(ledger(g))==1


def test_known_identity_unconfirmed_scene_is_held_until_decision():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group)
    sql(f"insert into public.visual_group_member_event(gym_id,group_key,alias_kind,alias_value,action,reason) values({q(g)},{q(group)},'canonical_url','https://test/one.jpg','review_hold','pHash 28 requires review')")
    sql(f'update public.content_calendar set media_not_ready_reason=null where id={q(rid)}')
    assert rows(g)[0]['media_not_ready_reason']=='visual_group_scene_review_required'
    with pytest.raises(RuntimeError):sql(f"update public.content_calendar set status='published' where id={q(rid)}")
    sql(f"select public.visual_group_confirm({q(g)},{q(group)},'canonical_url','https://test/one.jpg','reviewer')")
    sql(f'update public.content_calendar set visual_group_key={q(group)} where id={q(rid)}')
    assert rows(g)[0]['media_not_ready_reason'] is None


def test_unknown_published_identity_hold_survives_calendar_deletion():
    g=gym(False);rid=insert(g,url=None,status='published');backfill(g)
    sql(f'delete from public.content_calendar where id={q(rid)}')
    report=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert len(report['unresolved_published_history'])==1
    alias(g,'https://test/one.jpg')
    sql(f'insert into public.gym_visual_guard_settings values({q(g)},true,now())')
    held=insert(g,status='approved')
    assert rows(g)[0]['media_not_ready_reason'] is None and rows(g)[0]['status']=='approved'
    assert json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))['activation_ready'] is False


def test_scene_review_commit_while_claim_waits_is_rechecked_under_group_lock():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group)
    hold=f"select 1 from public.visual_group where gym_id={q(g)} and group_key={q(group)} for update; insert into public.visual_group_member_event(gym_id,group_key,alias_kind,alias_value,action) values({q(g)},{q(group)},'canonical_url','https://test/one.jpg','review_hold'); select pg_sleep(0.4)"
    claim=f"select pg_sleep(0.15); select public.claim_calendar_publish_slot_owned({q(rid)},{q(g)},'2026-10-05','UTC',2,false)"
    result=race(hold,claim)
    assert result[0][1] is None and result[1][1] is not None,result
    assert rows(g)[0]['publish_claim_token'] is None and rows(g)[0]['status']=='pending'


def test_reapplying_draft_migrations_preserves_usage_and_does_not_arm_gyms():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group)
    sql(f"update public.content_calendar set status='published' where id={q(rid)}")
    frozen=ledger(g)
    for name in ('schema','claim_trigger','backfill'):
        sql((ROOT/'migrations'/f'DRAFT_visual_group_{name}_20261002.sql').read_text())
    assert ledger(g)==frozen
    assert sql("select column_default from information_schema.columns where table_schema='public' and table_name='gym_visual_guard_settings' and column_name='enforce'")=='false'


@pytest.mark.parametrize('uncertain_status', ['publishing', 'failed'])
def test_ambiguity_cannot_be_cleared_by_resetting_status_token_or_provider(uncertain_status):
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group,account='google_business')
    sql(f"update public.content_calendar set status={q(uncertain_status)},publish_claim_token=gen_random_uuid(),late_post_id='uncertain-provider-id' where id={q(rid)}")
    assert ledger(g)[0]['ambiguous'] is True
    with pytest.raises(RuntimeError):
        sql(f"update public.content_calendar set status='pending',publish_claim_token=null,late_post_id=null where id={q(rid)}")
    assert rows(g)[0]['status']==uncertain_status
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06',account='google_business')


def test_sticky_ambiguity_survives_offline_marker_clear_and_calendar_delete():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group,account='story')
    sql(f"update public.content_calendar set status='publishing',publish_claim_token=gen_random_uuid() where id={q(rid)}")
    sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={q(g)}')
    sql(f"update public.content_calendar set status='pending',publish_claim_token=null,late_post_id=null where id={q(rid)}")
    sql(f'update public.gym_visual_guard_settings set enforce=true where gym_id={q(g)}')
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06',account='story')


def test_insert_unknown_delivered_image_cannot_use_different_known_sources():
    g=gym();one=alias(g,'source_one',kind='source_asset');two=alias(g,'source_two',kind='source_asset')
    for day,group,source,channel in [('2026-10-05',one,'source_one','google_business'),('2026-10-06',two,'source_two','story')]:
        rid=str(uuid.uuid4())
        sql(f"insert into public.content_calendar(id,gym_id,post_date,image_url,source_media_asset_id,visual_group_key,status,account) values({q(rid)},{q(g)},{q(day)},'https://test/unknown-delivered.jpg',{q(source)},{q(group)},'pending',{q(channel)})")
    assert all(r['visual_group_key'] is None and r['media_not_ready_reason']=='visual_group_identity_unresolved' for r in rows(g))
    assert ledger(g)==[]
    for r in rows(g):
        with pytest.raises(RuntimeError):sql(f"update public.content_calendar set status='publishing' where id={q(r['id'])}")


def test_registered_delivered_derivatives_require_complete_consistent_lineage():
    g=gym();group=alias(g,'source_one',kind='source_asset')
    alias(g,'https://test/feed.jpg',group);alias(g,'https://test/story-9x16.jpg',group)
    for url,channel in [('https://test/feed.jpg','google_business'),('https://test/story-9x16.jpg','story')]:
        rid=str(uuid.uuid4())
        sql(f"insert into public.content_calendar(id,gym_id,post_date,image_url,source_media_asset_id,status,account) values({q(rid)},{q(g)},'2026-10-05',{q(url)},'source_one','pending',{q(channel)})")
    assert len(ledger(g))==1 and all(r['visual_group_key']==group for r in rows(g))
    # Even a registered delivered URL cannot silently ignore a new unknown or
    # conflicting source/byte/Drive alias supplied on the calendar row.
    for source in ('unknown_source','source_other'):
        if source=='source_other':alias(g,source,kind='source_asset')
        rid=str(uuid.uuid4())
        sql(f"insert into public.content_calendar(id,gym_id,post_date,image_url,source_media_asset_id,status,account) values({q(rid)},{q(g)},'2026-10-06','https://test/story-9x16.jpg',{q(source)},'pending','story')")
        assert next(r for r in rows(g) if r['id']==rid)['media_not_ready_reason']=='visual_group_identity_unresolved'
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06',url='https://test/story-9x16.jpg',account='story')


@pytest.mark.parametrize('variant,status,channel', [('archived','publishing','google_business'),('candidate','failed','story')])
def test_backfill_retains_ambiguous_nonactive_variants(variant,status,channel):
    g=gym(False);rid=insert(g,status=status,account=channel)
    sql(f"update public.content_calendar set variant_status={q(variant)},publish_claim_token=gen_random_uuid(),late_post_id='uncertain' where id={q(rid)}")
    backfill(g)
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True
    assert sql(f"select ambiguous from public.visual_group_usage_sibling where gym_id={q(g)} and calendar_row_id={q(rid)}")=='t'
    sql(f'insert into public.gym_visual_guard_settings values({q(g)},true,now())')
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['state']=='reserved'
    with pytest.raises(RuntimeError):insert(g,date='2026-10-06',account=channel)


def test_backfill_unknown_archived_ambiguity_is_durable_activation_blocker():
    g=gym(False);rid=insert(g,status='failed',url=None,account='story')
    sql(f"update public.content_calendar set variant_status='archived',late_post_id='unknown-provider' where id={q(rid)}")
    report=backfill(g)
    assert report['rows_held_for_review']==1
    sql(f'delete from public.content_calendar where id={q(rid)}')
    report=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert len(report['unresolved_ambiguous_history'])==1 and report['activation_ready'] is False
    alias(g,'https://test/one.jpg')
    sql(f'insert into public.gym_visual_guard_settings values({q(g)},true,now())')
    held=insert(g,status='approved')
    assert rows(g)[0]['media_not_ready_reason'] is None and rows(g)[0]['status']=='approved'
    assert json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))['activation_ready'] is False


def test_runtime_unknown_failed_media_is_durable_hold_and_keeps_prior_claim():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group,account='google_business')
    sql(f"update public.content_calendar set status='failed',image_url='https://test/unknown-result.jpg',visual_group_key=null,late_post_id='uncertain-provider-result' where id={q(rid)}")
    assert rows(g)[0]['visual_group_key'] is None
    assert ledger(g)[0]['ambiguous'] is True and ledger(g)[0]['state']=='reserved'
    sql(f'delete from public.content_calendar where id={q(rid)}')
    report=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert len(report['unresolved_ambiguous_history'])==1
    assert ledger(g)[0]['state']=='reserved'
    with pytest.raises(RuntimeError):sql(f"update public.visual_group_usage_ledger set ambiguous=false,state='released' where gym_id={q(g)}")


def test_sticky_ambiguous_claim_can_finalize_then_calendar_delete_permanently():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group,account='story')
    sql(f"update public.content_calendar set status='publishing',publish_claim_token=gen_random_uuid() where id={q(rid)}")
    with pytest.raises(RuntimeError):sql(f"update public.visual_group_usage_sibling set ambiguous=false where gym_id={q(g)}")
    sql(f"update public.content_calendar set status='published',published_at=now() where id={q(rid)}")
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['state']=='published' and ledger(g)[0]['ambiguous'] is True


def test_shared_media_rpc_refuses_distinct_story_derivative_but_redate_preserves_it():
    g=gym();group=alias(g,'https://test/feed.jpg');alias(g,'https://test/story-9x16.jpg',group)
    alias(g,'https://test/replacement.jpg',group)
    feed=insert(g,group,url='https://test/feed.jpg',account='google_business')
    story=insert(g,group,url='https://test/story-9x16.jpg',account='instagram')
    sql(f"update public.content_calendar set format='story' where id={q(story)}")
    media=q(json.dumps({'image_url':'https://test/replacement.jpg','visual_group_key':group}))+'::jsonb'
    with pytest.raises(RuntimeError):sql(f"select public.visual_group_swap_siblings({q(g)},{ids_array([feed,story])},'2026-10-06',{media})")
    before={r['id']:r['image_url'] for r in rows(g)}
    assert sql(f"select public.visual_group_swap_redate({q(g)},{ids_array([feed,story])},'2026-10-06')")=='2'
    assert {r['id']:r['image_url'] for r in rows(g)}==before


def evidence(g, rid, group, day='2026-10-05', outcome='confirmed_not_sent'):
    claims=json.loads(sql(f"select coalesce(jsonb_agg(jsonb_build_object('group_key',s.group_key,'attempt_id',s.attempt_id::text,'claim_token',s.original_claim_token::text,'provider_post_id',s.original_provider_post_id,'image_url',s.original_image_url) order by group_key),'[]') from public.visual_group_usage_sibling s where gym_id={q(g)} and calendar_row_id={q(rid)} and ambiguous"))
    holds=json.loads(sql(f"select coalesce(jsonb_agg(jsonb_build_object('hold_event_id',e.id,'claim_token',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'publish_claim_token' end,'provider_post_id',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'late_post_id' end,'calendar_date',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'post_date' end,'image_url',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'image_url' end) order by id),'[]') from public.visual_group_member_event e where gym_id={q(g)} and alias_value={q(rid)} and action='review_hold' and actor in ('backfill_ambiguous_review','runtime_ambiguous_review') and not exists(select 1 from public.visual_group_reconciliation r where r.gym_id=e.gym_id and e.id=any(r.hold_event_ids))"))
    return {'source':'provider_terminal_readback','gym_id':g,'calendar_row_id':rid,'group_key':group,
      'calendar_date':day,'provider':'synthetic-isolated-test-provider','request_id':'terminal-request-'+rid,
      'receipt_ref':'fixture://terminal-receipt/'+rid,'terminal':True,'will_retry':False,
      'delivery':'not_delivered' if outcome=='confirmed_not_sent' else 'delivered',
      'provider_status':'failed_before_delivery' if outcome=='confirmed_not_sent' else 'published',
      'checked_at':sql('select now()'),'published_at':sql("select now()-interval '1 minute'"),
      'provider_post_id':claims[0]['provider_post_id'] if claims and claims[0]['provider_post_id'] else 'confirmed-provider-'+rid,
      'delivered_url':claims[0]['image_url'] if claims else None,'claims':claims,'hold_claims':holds}


def reconcile(g,rid,group,proof,outcome='confirmed_not_sent',day='2026-10-05',role=None):
    statement=f"select public.visual_group_reconcile_ambiguous({q(g)},{q(rid)},{q(outcome)},{q(group)},{q(day)},{q(json.dumps(proof))}::jsonb,'independent-provider-verifier')"
    return json.loads(sql((f'set role {role}; ' if role else '')+statement))


def uncertain(g, group, channel='google_business'):
    rid=insert(g,group,account=channel)
    sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id={q('provider-'+rid)} where id={q(rid)}")
    return rid


def test_explicit_non_delivery_evidence_releases_deleted_orphan_and_is_idempotent():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group)
    proof=evidence(g,rid,group)
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['ambiguous'] is True
    result=reconcile(g,rid,group,proof,role='service_role')
    assert result['outcome']=='confirmed_not_sent' and result['calendar_updated'] is False
    assert ledger(g)[0]['state']=='released' and ledger(g)[0]['ambiguous'] is False
    assert sql(f"select state||':'||ambiguous::text from public.visual_group_usage_sibling where gym_id={q(g)} and calendar_row_id={q(rid)}")=='released:false'
    assert reconcile(g,rid,group,proof)['idempotent'] is True
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(g)}')=='1'
    insert(g,group,date='2026-10-06')


def test_live_reconciliation_cancels_terminal_unsent_and_retains_other_sibling():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group,channel='story');other=insert(g,group,account='facebook')
    result=reconcile(g,rid,group,evidence(g,rid,group))
    assert result['calendar_updated'] is True
    affected=next(r for r in rows(g) if r['id']==rid)
    assert affected['status']=='killed' and affected['publish_claim_token'] is None and affected['late_post_id'] is None
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is False
    sql(f'delete from public.content_calendar where id={q(other)}')
    assert ledger(g)[0]['state']=='released'


@pytest.mark.parametrize('change',[{'source':'timeout'},{'provider_status':'not_found'}, {'terminal':False},{'will_retry':True},{'delivery':'unknown'},{'receipt_ref':''},{'calendar_row_id':'00000000-0000-0000-0000-000000000000'}])
def test_timeout_inconclusive_or_unbound_evidence_cannot_release(change):
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group)
    proof=evidence(g,rid,group);proof.update(change)
    with pytest.raises(RuntimeError):reconcile(g,rid,group,proof)
    assert ledger(g)[0]['ambiguous'] is True and ledger(g)[0]['state']=='reserved'
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(g)}')=='0'


def test_wrong_original_ids_and_readonly_receipt_table_do_not_bypass():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group);proof=evidence(g,rid,group)
    proof['claims'][0]['claim_token']=str(uuid.uuid4())
    with pytest.raises(RuntimeError):reconcile(g,rid,group,proof)
    proof=evidence(g,rid,group);proof['claims'][0]['provider_post_id']='foreign-provider'
    with pytest.raises(RuntimeError):reconcile(g,rid,group,proof)
    with pytest.raises(RuntimeError):sql("set role service_role; insert into public.visual_group_reconciliation(gym_id) values('forged')")
    with pytest.raises(RuntimeError):reconcile(g,rid,group,evidence(g,rid,group),role='authenticated')
    with pytest.raises(RuntimeError):sql(f"update public.content_calendar set publish_claim_token=gen_random_uuid() where id={q(rid)}")
    assert ledger(g)[0]['ambiguous'] is True


def test_confirmed_delivery_for_deleted_sibling_is_permanent():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group,channel='story')
    proof=evidence(g,rid,group,outcome='confirmed_published')
    sql(f'delete from public.content_calendar where id={q(rid)}')
    result=reconcile(g,rid,group,proof,outcome='confirmed_published')
    assert result['outcome']=='confirmed_published' and ledger(g)[0]['state']=='published'
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06')
    bad=evidence(g,rid,group)
    with pytest.raises(RuntimeError):reconcile(g,rid,group,bad)
    with pytest.raises(RuntimeError):sql(f'delete from public.visual_group_reconciliation where gym_id={q(g)}')


def test_confirmed_delivery_updates_live_calendar_with_original_provider_binding():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group)
    proof=evidence(g,rid,group,outcome='confirmed_published')
    result=reconcile(g,rid,group,proof,outcome='confirmed_published')
    assert result['calendar_updated'] is True and rows(g)[0]['status']=='published'
    assert ledger(g)[0]['state']=='published'


def test_old_receipt_cannot_release_a_later_attempt_on_same_calendar_row():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group);proof=evidence(g,rid,group)
    first_attempt=proof['claims'][0]['attempt_id'];reconcile(g,rid,group,proof)
    sql(f"update public.content_calendar set status='pending' where id={q(rid)}")
    sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id='next-provider-attempt' where id={q(rid)}")
    assert evidence(g,rid,group)['claims'][0]['attempt_id']!=first_attempt
    with pytest.raises(RuntimeError):reconcile(g,rid,group,proof)
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True


def test_backfill_unknown_history_does_not_demote_unrelated_approved_and_self_heals():
    g=gym(False);unknown=insert(g,status='published',url=None,date='2026-09-01')
    approved=insert(g,status='approved',url='https://test/unrelated-approved.jpg',date='2026-10-06',account='google_business')
    first=backfill(g)
    unaffected=next(r for r in rows(g) if r['id']==approved)
    assert unaffected['status']=='approved' and unaffected['media_not_ready_reason'] is None
    assert len(json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))['unresolved_published_history'])==1
    snapshot=rows(g);events=sql(f'select count(*) from public.visual_group_member_event where gym_id={q(g)}')
    backfill(g)
    assert rows(g)==snapshot and sql(f'select count(*) from public.visual_group_member_event where gym_id={q(g)}')==events
    # A stale visual scene hold from an older backfill can clear on verified
    # hydration; approval is preserved rather than silently revoked/regranted.
    sql(f"update public.content_calendar set media_not_ready_reason='visual_group_scene_review_required' where id={q(approved)}")
    backfill(g)
    assert next(r for r in rows(g) if r['id']==approved)['media_not_ready_reason'] is None
    assert next(r for r in rows(g) if r['id']==approved)['status']=='approved'


def test_deleted_unknown_ambiguous_hold_reconciles_only_captured_provider_ids():
    g=gym(False);rid=insert(g,status='failed',url=None,account='story')
    sql(f"update public.content_calendar set variant_status='archived',publish_claim_token=gen_random_uuid(),late_post_id='original-unknown-provider' where id={q(rid)}")
    backfill(g);proof=evidence(g,rid,None)
    assert proof['claims']==[] and proof['hold_claims'][0]['provider_post_id']=='original-unknown-provider'
    sql(f'delete from public.content_calendar where id={q(rid)}')
    bad=json.loads(json.dumps(proof));bad['hold_claims'][0]['provider_post_id']='foreign-provider'
    with pytest.raises(RuntimeError):reconcile(g,rid,None,bad)
    reconcile(g,rid,None,proof)
    report=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert report['unresolved_ambiguous_history']==[] and report['activation_ready'] is False


def reconcile_statement(g, rid, group, proof, outcome='confirmed_not_sent'):
    return f"select public.visual_group_reconcile_ambiguous({q(g)},{q(rid)},{q(outcome)},{q(group)},'2026-10-05',{q(json.dumps(proof))}::jsonb,'independent-provider-verifier')"


@pytest.mark.parametrize('delete_first', [False, True])
def test_reconciliation_serializes_with_calendar_deletion(delete_first):
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group,channel='google_business')
    proof=evidence(g,rid,group)
    cancel=f'delete from public.content_calendar where id={q(rid)}'
    certify=reconcile_statement(g,rid,group,proof)
    if delete_first:certify='select pg_sleep(0.05); '+certify
    else:cancel='select pg_sleep(0.05); '+cancel
    result=race(certify,cancel)
    assert all(err is None for _,err in result),result
    assert rows(g)==[] and ledger(g)[0]['state']=='released' and ledger(g)[0]['ambiguous'] is False
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(g)}')=='1'


def test_each_ambiguous_sibling_requires_its_own_terminal_evidence():
    g=gym();group=alias(g,'https://test/one.jpg');one=uncertain(g,group,channel='google_business');two=uncertain(g,group,channel='story')
    first=evidence(g,one,group);second=evidence(g,two,group)
    sql(f'delete from public.content_calendar where gym_id={q(g)}')
    reconcile(g,one,group,first)
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06')
    reconcile(g,two,group,second)
    assert ledger(g)[0]['state']=='released' and ledger(g)[0]['ambiguous'] is False
    insert(g,group,date='2026-10-06')


def test_unrelated_group_and_missing_original_request_markers_cannot_release():
    g=gym();group=alias(g,'https://test/one.jpg');other=alias(g,'https://test/other.jpg');rid=uncertain(g,group)
    sql(f'delete from public.content_calendar where id={q(rid)}')
    proof=evidence(g,rid,other)
    with pytest.raises(RuntimeError):reconcile(g,rid,other,proof)
    assert ledger(g)[0]['ambiguous'] is True
    absent=gym();scene=alias(absent,'https://test/one.jpg');row=insert(absent,scene,status='failed')
    with pytest.raises(RuntimeError):reconcile(absent,row,scene,evidence(absent,row,scene))
    assert ledger(absent)[0]['ambiguous'] is True


def test_deleted_unknown_receipts_serialize_and_conflicting_proof_cannot_double_resolve():
    g=gym(False);rid=insert(g,status='failed',url=None,account='story')
    sql(f"update public.content_calendar set late_post_id='unknown-original-provider' where id={q(rid)}")
    backfill(g);proof=evidence(g,rid,None)
    sql(f'delete from public.content_calendar where id={q(rid)}')
    statement=reconcile_statement(g,rid,None,proof)
    result=race(statement,statement)
    assert all(err is None for _,err in result),result
    assert {json.loads(output)['idempotent'] for output,_ in result}=={False,True}
    second=dict(proof,receipt_ref='different-terminal-readback')
    with pytest.raises(RuntimeError):reconcile(g,rid,None,second)
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(g)}')=='1'


def test_backfill_new_attempt_captures_new_original_ids_after_prior_reconciliation():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group)
    reconcile(g,rid,group,evidence(g,rid,group))
    sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={q(g)}')
    token=str(uuid.uuid4())
    sql(f"update public.content_calendar set status='failed',publish_claim_token={q(token)},late_post_id='next-offline-provider' where id={q(rid)}")
    backfill(g)
    claim=evidence(g,rid,group)['claims'][0]
    assert claim['claim_token']==token and claim['provider_post_id']=='next-offline-provider'
    reconcile(g,rid,group,evidence(g,rid,group))
    assert ledger(g)[0]['state']=='released' and ledger(g)[0]['ambiguous'] is False


@pytest.mark.parametrize('replay_rpc', [False, True])
def test_receipt_transaction_cannot_reset_new_unknown_attempt_with_reused_markers(replay_rpc):
    g=gym();rid=insert(g,url='https://test/unknown.jpg')
    token=str(uuid.uuid4())
    sql(f"update public.content_calendar set status='failed',publish_claim_token={q(token)},late_post_id='unknown-original-provider' where id={q(rid)}")
    proof=evidence(g,rid,None)
    reset=reconcile_statement(g,rid,None,proof) if replay_rpc else f"update public.content_calendar set status='pending',publish_claim_token=null,late_post_id=null where id={q(rid)}"
    with pytest.raises(RuntimeError):
        sql('begin; '+reconcile_statement(g,rid,None,proof)+f"; update public.content_calendar set status='failed',publish_claim_token={q(token)},late_post_id='unknown-original-provider' where id={q(rid)}; "+reset+'; commit')
    assert rows(g)[0]['status']=='failed'
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(g)}')=='0'


def test_unknown_deleted_delivery_binds_original_provider_and_becomes_permanent():
    g=gym(False);rid=insert(g,status='failed',url=None,account='story')
    sql(f"update public.content_calendar set late_post_id='unknown-original-provider' where id={q(rid)}")
    backfill(g)
    sql(f'delete from public.content_calendar where id={q(rid)}')
    scene=alias(g,'https://test/recovered-story.jpg')
    proof=evidence(g,rid,scene,outcome='confirmed_published')
    proof['delivered_url']='https://test/recovered-story.jpg'
    with pytest.raises(RuntimeError):reconcile(g,rid,scene,proof,outcome='confirmed_published')
    assert ledger(g)==[]
    proof['provider_post_id']='unknown-original-provider'
    result=reconcile(g,rid,scene,proof,outcome='confirmed_published')
    assert result['calendar_updated'] is False and ledger(g)[0]['state']=='published'
    assert json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))['unresolved_ambiguous_history']==[]
    sql(f'insert into public.gym_visual_guard_settings values({q(g)},true,now())')
    with pytest.raises(RuntimeError):insert(g,scene,date='2026-10-06',url='https://test/recovered-story.jpg')
