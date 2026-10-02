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


def canon(g):
    return sql(f'select public.visual_group_tenant_id({q(g)})') or None


def gym(enforce=True):
    # Every gym gets an explicit canonical tenant alias registration; internal
    # visual-group tables are keyed by that tenant UUID, not the calendar key.
    name = 'g_' + uuid.uuid4().hex
    tenant = str(uuid.uuid4())
    sql(f'select public.visual_group_tenant_register({q(name)},{q(tenant)}::uuid)')
    if enforce:
        sql(f'insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tenant)},true)')
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
    return json.loads(sql(f"select coalesce(jsonb_agg(to_jsonb(l)),'[]') from public.visual_group_usage_ledger l where gym_id={q(canon(g))}"))


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
    assert sql(f'select count(*) from public.visual_group where gym_id={q(canon(g))}') == '1'
    assert alias(g, 'drive_2', group, 'drive_id') == group
    with pytest.raises(RuntimeError): alias(g, 'drive_1','other','drive_id')
    with pytest.raises(RuntimeError): sql(f"update public.visual_group_alias set group_key='other' where gym_id={q(canon(g))}")


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
        sql(f"update public.content_calendar set status='publishing',publish_claim_token=gen_random_uuid() where id={q(sibling)}")
        proof=evidence(g,sibling,group,outcome='confirmed_published')
        assert reconcile(g,sibling,group,proof,outcome='confirmed_published')['calendar_updated'] is True
    assert len(rows(g))==4 and ledger(g)[0]['state']=='published'
    frozen=ledger(g)
    sql(f'delete from public.content_calendar where gym_id={q(g)}')
    assert ledger(g)==frozen
    with pytest.raises(RuntimeError): insert(g,group,date='2026-10-06')
    with pytest.raises(RuntimeError): sql(f'delete from public.visual_group_usage_ledger where gym_id={q(canon(g))}')


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
    sql(f'insert into public.gym_visual_guard_settings values({q(canon(off))},true,now())')
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
    events=sql(f'select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}')
    backfill(g)
    assert rows(g)==after and sql(f'select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}')==events
    coverage=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert coverage['per_gym'][0]['uncovered_unheld_rows']==0


def test_unknown_historical_publish_no_fabricated_date_blocks_all_dates():
    g=gym(False);insert(g,date=None,status='published')
    backfill(g)
    assert ledger(g)[0]['state']=='published' and ledger(g)[0]['reserved_date'] is None
    sql(f'insert into public.gym_visual_guard_settings values({q(canon(g))},true,now())')
    with pytest.raises(RuntimeError):insert(g,date='2026-10-06')


def test_backfill_alias_conflict_does_not_leave_partial_binding_or_orphan():
    g=gym(False);a=alias(g,'https://test/a.jpg');b=alias(g,'drive_b',kind='drive_id')
    rid=insert(g,url='https://test/a.jpg')
    sql(f"update public.content_calendar set drive_file_id='drive_b',byte_hash='brand_new_hash' where id={q(rid)}")
    backfill(g)
    assert rows(g)[0]['media_not_ready_reason']=='visual_group_alias_conflict'
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(canon(g))} and alias_value='brand_new_hash'")=='0'
    assert sql(f'select count(*) from public.visual_group where gym_id={q(canon(g))}')=='2'


def test_cross_date_historical_repeat_is_reported_in_backfill_and_conflict_report():
    # P2 review #7: a second published row of the same visual on a different
    # date must surface as historical_cross_date_visual_repeat in the backfill
    # report AND as a cross_date_conflicts entry in the conflict report, while
    # the permanent ledger keeps the original publication date and published
    # calendar history is untouched.
    g=gym(False)
    first=insert(g,date='2026-09-01',status='published') # NULL published_at
    second=insert(g,date='2026-10-06',status='published') # same canonical URL
    report=backfill(g)
    assert 'historical_cross_date_visual_repeat' in [c['reason'] for c in report['conflict_rows']]
    l=ledger(g)
    assert len(l)==1 and l[0]['state']=='published' and l[0]['reserved_date']=='2026-09-01'
    assert report['published_rows_finalized']==1 and report['rows_held_for_review']==1
    coverage=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    conflicts=[c for c in coverage['cross_date_conflicts'] if c['gym_id']==g and c['resolved_group']==l[0]['group_key']]
    assert len(conflicts)==1, 'expected exactly one reported cross-date conflict for the group'
    assert conflicts[0]['dates']==['2026-09-01','2026-10-06']
    assert set(conflicts[0]['row_ids'])=={first,second}
    assert len(coverage['unresolved_published_history'])==1
    assert coverage['activation_ready'] is False


def test_cross_alias_published_conflict_hold_counts_once_in_unresolved_history():
    # P2 review #8: a published row whose alias keys map to different groups is
    # held once, mints no new bindings, survives repeat backfills without
    # duplicate events, and the conflict report counts the row only once even
    # when a newer unresolved hold exists for it.
    g=gym(False)
    alias(g,'https://test/a.jpg');alias(g,'drive_b',kind='drive_id')
    rid=insert(g,url='https://test/a.jpg',status='published')
    sql(f"update public.content_calendar set drive_file_id='drive_b' where id={q(rid)}")
    report=backfill(g)
    assert report['rows_held_for_review']==1
    assert 'visual_group_alias_conflict' in [c['reason'] for c in report['conflict_rows']]
    # No partial bindings, no minted ledger rows, published calendar untouched.
    assert sql(f"select count(*) from public.visual_group where gym_id={q(canon(g))}")=='2'
    assert ledger(g)==[]
    assert rows(g)[0]['media_not_ready_reason'] is None
    backfill(g) # idempotent repeat: still exactly one hold event for the row
    holds=f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))} and alias_value={q(rid)} and action='review_hold'"
    assert sql(holds)=='1'
    # A newer unresolved hold for the same row must not double-count.
    sql(f"insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason) values({q(canon(g))},NULL,{q(rid)},'review_hold','backfill_published_review','owner_second_look')")
    coverage=json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert [e['alias_value'] for e in coverage['unresolved_published_history']].count(rid)==1
    assert coverage['activation_ready'] is False


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
    with pytest.raises(RuntimeError,match='permission denied for table visual_group_member_event'):
        sql(f"set role service_role; insert into public.visual_group_member_event(gym_id,action,reason) values({q(canon(g))},'review_hold','unknown identity')")
    with pytest.raises(RuntimeError):sql(f"set role anon; select * from public.visual_group where gym_id={q(canon(g))}")
    with pytest.raises(RuntimeError):sql(f"set role authenticated; select public.visual_group_register_alias({q(g)},'drive_id','private',null)")
    with pytest.raises(RuntimeError):sql(f"update public.visual_group set group_key='changed' where gym_id={q(canon(g))}")
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
    assert sql(f"select state from public.visual_group_usage_ledger where gym_id={q(canon(g))}")=='published'
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
    sql(f"insert into public.visual_group_member_event(gym_id,group_key,alias_kind,alias_value,action,reason) values({q(canon(g))},{q(group)},'canonical_url','https://test/one.jpg','review_hold','pHash 28 requires review')")
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
    sql(f'insert into public.gym_visual_guard_settings values({q(canon(g))},true,now())')
    held=insert(g,status='approved')
    assert rows(g)[0]['media_not_ready_reason'] is None and rows(g)[0]['status']=='approved'
    assert json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))['activation_ready'] is False


def test_scene_review_commit_while_claim_waits_is_rechecked_under_group_lock():
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group)
    hold=f"select 1 from public.visual_group where gym_id={q(canon(g))} and group_key={q(group)} for update; insert into public.visual_group_member_event(gym_id,group_key,alias_kind,alias_value,action) values({q(canon(g))},{q(group)},'canonical_url','https://test/one.jpg','review_hold'); select pg_sleep(0.4)"
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
    sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={q(canon(g))}')
    sql(f"update public.content_calendar set status='pending',publish_claim_token=null,late_post_id=null where id={q(rid)}")
    sql(f'update public.gym_visual_guard_settings set enforce=true where gym_id={q(canon(g))}')
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
    assert sql(f"select ambiguous from public.visual_group_usage_sibling where gym_id={q(canon(g))} and calendar_row_id={q(rid)}")=='t'
    sql(f'insert into public.gym_visual_guard_settings values({q(canon(g))},true,now())')
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
    sql(f'insert into public.gym_visual_guard_settings values({q(canon(g))},true,now())')
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
    with pytest.raises(RuntimeError):sql(f"update public.visual_group_usage_ledger set ambiguous=false,state='released' where gym_id={q(canon(g))}")


@pytest.mark.parametrize('marker',["status='published'","published_at=now()","status='published',published_at=now(),publish_claim_token=null"])
def test_sticky_ambiguous_claim_cannot_finalize_from_direct_marker(marker):
    g=gym();group=alias(g,'https://test/one.jpg');rid=insert(g,group,account='story')
    sql(f"update public.content_calendar set status='publishing',publish_claim_token=gen_random_uuid() where id={q(rid)}")
    before=rows(g);usage=ledger(g)
    with pytest.raises(RuntimeError):sql(f"update public.visual_group_usage_sibling set ambiguous=false where gym_id={q(canon(g))}")
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"update public.content_calendar set {marker} where id={q(rid)}")
    assert rows(g)==before and ledger(g)==usage
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True


def test_sticky_ambiguous_claim_reconciles_then_calendar_delete_is_permanent():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group,channel='story')
    proof=evidence(g,rid,group,outcome='confirmed_published')
    assert reconcile(g,rid,group,proof,outcome='confirmed_published')['calendar_updated'] is True
    sql(f'delete from public.content_calendar where id={q(rid)}')
    assert ledger(g)[0]['state']=='published'
    with pytest.raises(RuntimeError):insert(g,group,date='2026-10-06')


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
    claims=json.loads(sql(f"select coalesce(jsonb_agg(jsonb_build_object('group_key',s.group_key,'attempt_id',s.attempt_id::text,'claim_token',s.original_claim_token::text,'provider_post_id',s.original_provider_post_id,'image_url',s.original_image_url) order by group_key),'[]') from public.visual_group_usage_sibling s where gym_id={q(canon(g))} and calendar_row_id={q(rid)} and ambiguous"))
    holds=json.loads(sql(f"select coalesce(jsonb_agg(jsonb_build_object('hold_event_id',e.id,'claim_token',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'publish_claim_token' end,'provider_post_id',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'late_post_id' end,'calendar_date',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'post_date' end,'image_url',case when left(btrim(e.reason),1)='{{' then e.reason::jsonb->>'image_url' end) order by id),'[]') from public.visual_group_member_event e where gym_id={q(canon(g))} and alias_value={q(rid)} and action='review_hold' and actor in ('backfill_ambiguous_review','runtime_ambiguous_review') and not exists(select 1 from public.visual_group_reconciliation r where r.gym_id=e.gym_id and e.id=any(r.hold_event_ids))"))
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
    assert sql(f"select state||':'||ambiguous::text from public.visual_group_usage_sibling where gym_id={q(canon(g))} and calendar_row_id={q(rid)}")=='released:false'
    assert reconcile(g,rid,group,proof)['idempotent'] is True
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(canon(g))}')=='1'
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
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(canon(g))}')=='0'


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
    with pytest.raises(RuntimeError):sql(f'delete from public.visual_group_reconciliation where gym_id={q(canon(g))}')


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
    snapshot=rows(g);events=sql(f'select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}')
    backfill(g)
    assert rows(g)==snapshot and sql(f'select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}')==events
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
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(canon(g))}')=='1'


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
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(canon(g))}')=='1'


def test_backfill_new_attempt_captures_new_original_ids_after_prior_reconciliation():
    g=gym();group=alias(g,'https://test/one.jpg');rid=uncertain(g,group)
    reconcile(g,rid,group,evidence(g,rid,group))
    sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={q(canon(g))}')
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
    assert sql(f'select count(*) from public.visual_group_reconciliation where gym_id={q(canon(g))}')=='0'


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
    sql(f'insert into public.gym_visual_guard_settings values({q(canon(g))},true,now())')
    with pytest.raises(RuntimeError):insert(g,scene,date='2026-10-06',url='https://test/recovered-story.jpg')


def media_payload(items):
    return q(json.dumps([{'calendar_row_id': rid, 'media': media} for rid, media in items])) + '::jsonb'


def test_per_row_media_swap_preserves_distinct_feed_and_story_derivatives():
    g=gym();group=alias(g,'https://test/old-feed.jpg');alias(g,'https://test/old-story.jpg',group)
    feed=insert(g,group,url='https://test/old-feed.jpg')
    story=insert(g,group,url='https://test/old-story.jpg',account='facebook')
    sql(f"update public.content_calendar set format='story' where id={q(story)}")
    scene=alias(g,'https://test/new-feed.jpg')
    alias(g,'https://test/new-story-9x16.jpg',scene)
    alias(g,'asset-new-feed',scene,kind='source_asset')
    payload=media_payload([
        (feed,{'image_url':'https://test/new-feed.jpg','source_media_asset_id':'asset-new-feed'}),
        (story,{'image_url':'https://test/new-story-9x16.jpg','visual_group_key':scene}),
    ])
    assert sql(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")=='2'
    by_id={r['id']:r for r in rows(g)}
    assert by_id[feed]['image_url']=='https://test/new-feed.jpg'
    assert by_id[feed]['source_media_asset_id']=='asset-new-feed'
    assert by_id[story]['image_url']=='https://test/new-story-9x16.jpg' and by_id[story]['format']=='story'
    assert all(r['visual_group_key']==scene and r['post_date']=='2026-10-06' for r in by_id.values())
    led={l['group_key']:l for l in ledger(g)}
    assert led[group]['state']=='released'
    assert led[scene]['state']=='reserved' and led[scene]['reserved_date']=='2026-10-06'


def test_per_row_media_swap_missing_extra_duplicate_and_cross_tenant_rows_rejected():
    g=gym();group=alias(g,'https://test/one.jpg')
    one=insert(g,group);two=insert(g,group,account='facebook')
    alias(g,'https://test/new.jpg')
    ok={'image_url':'https://test/new.jpg'}
    before=rows(g);before_ledger=ledger(g)
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,ok)])},'2026-10-06')")
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,ok),(two,ok),(str(uuid.uuid4()),ok)])},'2026-10-06')")
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,ok),(one,ok)])},'2026-10-06')")
    other=gym();og=alias(other,'https://test/other.jpg');foreign=insert(other,og,url='https://test/other.jpg')
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,ok),(foreign,ok)])},'2026-10-06')")
    assert rows(g)==before and ledger(g)==before_ledger


def test_per_row_media_swap_conflict_rolls_back_everything():
    g=gym();group=alias(g,'https://test/one.jpg')
    one=insert(g,group);two=insert(g,group,account='facebook')
    occupied=alias(g,'https://test/taken.jpg')
    insert(g,occupied,date='2026-10-09',url='https://test/taken.jpg')
    alias(g,'https://test/free.jpg')
    before=rows(g);before_ledger=ledger(g)
    payload=media_payload([(one,{'image_url':'https://test/free.jpg'}),(two,{'image_url':'https://test/taken.jpg'})])
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")
    assert rows(g)==before and ledger(g)==before_ledger


def test_per_row_media_swap_rejects_conflicting_aliases_and_unready_states():
    g=gym();group=alias(g,'https://test/one.jpg')
    one=insert(g,group);two=insert(g,group,account='facebook')
    alias(g,'https://test/new.jpg')
    alias(g,'hash-b',kind='byte_hash')  # registered to a different group
    conflict={'image_url':'https://test/new.jpg','byte_hash':'hash-b'}
    ok={'image_url':'https://test/new.jpg'}
    before={r['id']:(r['image_url'],r['post_date'],r['visual_group_key']) for r in rows(g)}
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,conflict),(two,ok)])},'2026-10-06')")
    sql(f"update public.content_calendar set status='approved' where id={q(one)}")
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,ok),(two,ok)])},'2026-10-06')")
    sql(f"update public.content_calendar set status='pending' where id={q(one)}")
    sql(f"update public.content_calendar set status='publishing',publish_claim_token=gen_random_uuid() where id={q(two)}")
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(one,ok),(two,ok)])},'2026-10-06')")
    after={r['id']:(r['image_url'],r['post_date'],r['visual_group_key']) for r in rows(g)}
    assert after==before
    by_id={r['id']:r for r in rows(g)}
    assert by_id[one]['status']=='pending' and by_id[two]['status']=='publishing'


def test_per_row_media_swap_racing_cross_date_claim_has_one_authority():
    g=gym();group=alias(g,'https://test/one.jpg')
    one=insert(g,group);two=insert(g,group,account='facebook')
    scene=alias(g,'https://test/new.jpg')
    payload=media_payload([(one,{'image_url':'https://test/new.jpg'}),(two,{'image_url':'https://test/new.jpg'})])
    result=race(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')",
      f"insert into public.content_calendar(gym_id,post_date,image_url,status) values({q(g)},'2026-10-09','https://test/new.jpg','pending')")
    assert sum(err is None for _,err in result)==1,result
    led={l['group_key']:l for l in ledger(g)}
    moved={r['post_date'] for r in rows(g) if r['id'] in (one,two)}
    if result[0][1] is None:
        assert led[scene]['reserved_date']=='2026-10-06' and moved=={'2026-10-06'}
    else:
        assert led[scene]['reserved_date']=='2026-10-09' and moved=={'2026-10-05'}


def test_per_row_swap_internal_helper_cannot_bypass_public_wrapper_guards():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group);two=insert(g,group,account='facebook')
    sql(f"update public.content_calendar set status='approved' where id={q(one)}")
    alias(g,'https://test/new.jpg')
    payload=media_payload([(one,{'image_url':'https://test/new.jpg'})])
    before=rows(g);before_ledger=ledger(g)
    with pytest.raises(RuntimeError):
        sql(f"set role service_role; select public.visual_group_apply_media_swap({q(g)},'2026-10-06',{payload},{ids_array([one])},{q(group)})")
    assert sql("select has_function_privilege('service_role','public.visual_group_apply_media_swap(text,date,jsonb,uuid[],text)','EXECUTE')")=='f'
    assert rows(g)==before and ledger(g)==before_ledger


def test_per_row_swap_service_wrapper_allowed_and_authenticated_refused():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group);two=insert(g,group,account='facebook')
    alias(g,'https://test/new.jpg')
    payload=media_payload([(one,{'image_url':'https://test/new.jpg'}),(two,{'image_url':'https://test/new.jpg'})])
    with pytest.raises(RuntimeError):
        sql(f"set role authenticated; select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")
    assert sql(f"set role service_role; select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")=='2'


def test_per_row_swap_cannot_split_old_scene_into_unrelated_new_scenes():
    g=gym();group=alias(g,'https://test/one.jpg');one=insert(g,group);two=insert(g,group,account='facebook')
    alias(g,'https://test/new-feed.jpg');alias(g,'https://test/unrelated-story.jpg')
    before=rows(g);before_ledger=ledger(g)
    payload=media_payload([(one,{'image_url':'https://test/new-feed.jpg'}),(two,{'image_url':'https://test/unrelated-story.jpg'})])
    with pytest.raises(RuntimeError):sql(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")
    assert rows(g)==before and ledger(g)==before_ledger


def test_per_row_swap_clears_omitted_old_identity_and_preserves_supplied_derivative_lineage():
    g=gym();old=alias(g,'https://test/old-feed.jpg');alias(g,'https://test/old-story.jpg',old)
    alias(g,'old-asset',old,kind='source_asset');alias(g,'old-hash',old,kind='byte_hash')
    feed=insert(g,old,url='https://test/old-feed.jpg');story=insert(g,old,url='https://test/old-story.jpg',account='facebook')
    sql(f"update public.content_calendar set source_media_asset_id='old-asset',byte_hash='old-hash' where gym_id={q(g)}")
    sql(f"update public.content_calendar set format='story' where id={q(story)}")
    scene=alias(g,'https://test/new-feed.jpg');alias(g,'https://test/new-story.jpg',scene);alias(g,'https://test/new-raw.jpg',scene)
    payload=media_payload([(feed,{'image_url':'https://test/new-feed.jpg'}),
      (story,{'image_url':'https://test/new-story.jpg','source_media_url':'https://test/new-raw.jpg'})])
    assert sql(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")=='2'
    by_id={r['id']:r for r in rows(g)}
    assert all(r['source_media_asset_id'] is None and r['byte_hash'] is None for r in by_id.values())
    assert by_id[feed]['source_media_url'] is None and by_id[story]['source_media_url']=='https://test/new-raw.jpg'
    assert by_id[story]['format']=='story'


def test_per_row_swap_pending_scene_review_refuses_all_rows():
    g=gym();old=alias(g,'https://test/one.jpg');one=insert(g,old);two=insert(g,old,account='facebook')
    scene=alias(g,'https://test/new.jpg')
    sql(f"insert into public.visual_group_member_event(gym_id,group_key,alias_kind,alias_value,action) values({q(canon(g))},{q(scene)},'canonical_url','https://test/new.jpg','review_hold')")
    before=rows(g);before_ledger=ledger(g)
    payload=media_payload([(one,{'image_url':'https://test/new.jpg'}),(two,{'image_url':'https://test/new.jpg'})])
    with pytest.raises(RuntimeError):sql(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')")
    assert rows(g)==before and ledger(g)==before_ledger


@pytest.mark.parametrize('left_kind,right_kind,planner',[
    ('per_row','legacy','set local enable_seqscan=off'),
    ('legacy','per_row','set local enable_indexscan=off; set local enable_bitmapscan=off'),
    ('per_row','per_row','set local enable_sort=off'),
    ('legacy','legacy','set local enable_seqscan=off; set local enable_bitmapscan=off'),
])
def test_opposite_per_row_and_legacy_swaps_refuse_without_lock_inversion(left_kind,right_kind,planner):
    g=gym();a=alias(g,'https://test/a.jpg');b=alias(g,'https://test/b.jpg')
    one=insert(g,a,url='https://test/a.jpg');two=insert(g,b,url='https://test/b.jpg')
    def swap(kind,rid,url):
        if kind=='per_row':
            call=f"select public.visual_group_swap_siblings_media({q(g)},{media_payload([(rid,{'image_url':url})])},'2026-10-06')"
        else:
            call=f"select public.visual_group_swap_siblings({q(g)},{ids_array([rid])},'2026-10-06',{q(json.dumps({'image_url':url}))}::jsonb)"
        return planner+'; '+call
    left=swap(left_kind,one,'https://test/b.jpg');right=swap(right_kind,two,'https://test/a.jpg')
    before=rows(g);before_ledger=ledger(g)
    for _ in range(4):
        result=race(left,right)
        # Both targets are occupied: the only valid outcome is the domain
        # refusal after serialized locks, never a deadlock/timeout or success.
        assert all(error is not None and 'replacement group is reserved, published or ambiguous' in error for _,error in result),result
        assert rows(g)==before and ledger(g)==before_ledger


def test_per_row_swap_refuses_supplied_identity_column_absent_from_real_schema():
    g=gym();old=alias(g,'https://test/one.jpg');rid=insert(g,old)
    scene=alias(g,'https://test/new.jpg');alias(g,'conflicting-r2-key',kind='r2_key')
    payload=media_payload([(rid,{'image_url':'https://test/new.jpg','r2_key':'conflicting-r2-key'})])
    before=rows(g);before_ledger=ledger(g)
    with pytest.raises(RuntimeError):
        sql(f"begin; alter table public.content_calendar drop column r2_key; select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06'); commit")
    assert rows(g)==before and ledger(g)==before_ledger
    assert sql("select count(*) from information_schema.columns where table_schema='public' and table_name='content_calendar' and column_name='r2_key'")=='1'


def test_per_row_swap_full_membership_is_checked_under_old_group_lock():
    g=gym();old=alias(g,'https://test/one.jpg');one=insert(g,old);two=insert(g,old,account='facebook')
    scene=alias(g,'https://test/new.jpg')
    payload=media_payload([(one,{'image_url':'https://test/new.jpg'}),(two,{'image_url':'https://test/new.jpg'})])
    result=race(f"select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06')",
      f"insert into public.content_calendar(gym_id,post_date,image_url,status,account) values({q(g)},'2026-10-05','https://test/one.jpg','pending','google_business')")
    assert result[1][1] is None,result
    by_id={r['id']:r for r in rows(g)}
    if result[0][1] is None:
        assert all(by_id[rid]['visual_group_key']==scene and by_id[rid]['post_date']=='2026-10-06' for rid in (one,two))
        assert next(r for r in rows(g) if r['id'] not in (one,two))['visual_group_key']==old
    else:
        assert all(r['visual_group_key']==old and r['post_date']=='2026-10-05' for r in rows(g))
    assert len(rows(g))==3


def test_per_row_swap_uses_actual_optional_column_types():
    g=gym();old=alias(g,'https://test/one.jpg');rid=insert(g,old)
    scene=alias(g,'https://test/new.jpg');drive=str(uuid.uuid4());alias(g,drive,scene,kind='drive_id')
    payload=media_payload([(rid,{'image_url':'https://test/new.jpg','drive_file_id':drive})])
    before=rows(g);before_ledger=ledger(g)
    result=sql(f"begin; alter table public.content_calendar alter column drive_file_id type uuid using null::uuid; select public.visual_group_swap_siblings_media({q(g)},{payload},'2026-10-06'); select drive_file_id from public.content_calendar where id={q(rid)}; rollback")
    assert result.splitlines()==['1',drive]
    assert rows(g)==before and ledger(g)==before_ledger


def tenant_gym(tenant, enforce=True):
    # A second calendar alias key bound to an existing canonical tenant UUID.
    name = 'g_' + uuid.uuid4().hex
    sql(f'select public.visual_group_tenant_register({q(name)},{q(tenant)}::uuid)')
    if enforce:
        sql(f'insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tenant)},true) on conflict do nothing')
    return name


def test_cross_alias_same_tenant_shared_date_authority():
    # Old and current calendar alias keys of one real tenant share one
    # date-conflict authority: same scene + different date via the other alias
    # rejects; same-date channel siblings across both aliases share one ledger.
    g1 = gym()
    t = canon(g1)
    g2 = tenant_gym(t)
    assert canon(g2) == t
    group = alias(g1, 'https://test/one.jpg')
    insert(g1, group)
    with pytest.raises(RuntimeError): insert(g2, group, date='2026-10-06')
    sib = insert(g2, group, account='facebook')
    assert sib and len(ledger(g1)) == 1 and ledger(g1) == ledger(g2)
    assert ledger(g1)[0]['gym_id'] == t and ledger(g1)[0]['state'] == 'reserved'
    # Calendar rows keep their raw alias keys; portal/client behavior untouched.
    assert rows(g1)[0]['gym_id'] == g1 and rows(g2)[0]['gym_id'] == g2


def test_cross_alias_concurrent_first_reservation_one_winner():
    g1 = gym(); g2 = tenant_gym(canon(g1))
    group = alias(g1, 'https://test/one.jpg')
    stmt = lambda g, day: f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) values({q(g)},{q(day)},'instagram','https://test/one.jpg',{q(group)},'pending')"
    result = race(stmt(g1, '2026-10-05'), stmt(g2, '2026-10-06'))
    assert sum(err is None for _, err in result) == 1, result
    assert len(ledger(g1)) == 1
    assert len(rows(g1)) + len(rows(g2)) == 1


def test_unrelated_tenants_same_media_stay_isolated():
    one = gym(); two = gym()
    assert canon(one) != canon(two)
    g1 = alias(one, 'https://test/one.jpg')
    g2 = alias(two, 'https://test/one.jpg')
    assert g1 != g2
    insert(one, g1)
    insert(two, g2, date='2026-10-06')
    assert len(ledger(one)) == len(ledger(two)) == 1
    with pytest.raises(RuntimeError): insert(one, g1, date='2026-10-06')


def test_tenant_alias_conflict_idempotent_and_race():
    g = gym(); t = canon(g)
    key = 'g_' + uuid.uuid4().hex
    assert sql(f'select public.visual_group_tenant_register({q(key)},{q(t)}::uuid)') == t
    # Same binding again is idempotent; re-binding to another tenant raises.
    assert sql(f'select public.visual_group_tenant_register({q(key)},{q(t)}::uuid)') == t
    with pytest.raises(RuntimeError):
        sql(f'select public.visual_group_tenant_register({q(key)},{q(str(uuid.uuid4()))}::uuid)')
    # Bindings are immutable at the SQL layer too.
    with pytest.raises(RuntimeError):
        sql(f"update public.tenant_alias set tenant_id={q(str(uuid.uuid4()))}::uuid where alias_key={q(key)}")
    # Concurrent conflicting registration: exactly one tenant wins, and the
    # loser fails closed instead of minting a second identity for the key.
    contested = 'g_' + uuid.uuid4().hex
    ta, tb = str(uuid.uuid4()), str(uuid.uuid4())
    result = race(f'select public.visual_group_tenant_register({q(contested)},{q(ta)}::uuid)',
                  f'select public.visual_group_tenant_register({q(contested)},{q(tb)}::uuid)')
    winner = sql(f'select tenant_id from public.tenant_alias where alias_key={q(contested)}')
    assert winner in (ta, tb)
    assert sum(err is None and out == winner for out, err in result) == 1, result
    assert sum(err is not None for _, err in result) == 1, result


def test_unmapped_retired_key_stays_unarmed_and_never_mints():
    # The unresolved retired key from the preflight (42 rows, 0 mapped
    # tenants) must fail closed everywhere: no arming, no groups, no ledger.
    retired = 'zz-retired-20260904-f574c06c'
    assert canon(retired) is None
    with pytest.raises(RuntimeError):
        sql(f'insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(retired)},true)')
    with pytest.raises(RuntimeError):
        alias(retired, 'https://test/one.jpg')
    with pytest.raises(RuntimeError):
        sql(f'select public.visual_group_backfill_gym({q(retired)},false)')
    with pytest.raises(RuntimeError):
        sql(f'select public.visual_group_backfill_gym({q(retired)},true)')
    # Enforcement OFF means calendar writes pass through with no minted
    # identity and no ledger/sibling/event rows for the unmapped key.
    rid = insert(retired, url='https://test/unknown.jpg')
    assert rows(retired)[0]['media_not_ready_reason'] is None
    assert sql(f'select count(*) from public.visual_group where gym_id={q(retired)}') == '0'
    assert sql(f'select count(*) from public.visual_group_usage_ledger where gym_id={q(retired)}') == '0'
    assert sql(f'select count(*) from public.visual_group_alias a where not exists(select 1 from public.tenant_alias t where t.tenant_id::text=a.gym_id)') == '0'
    report = json.loads(sql(f'select public.visual_group_conflict_report({q(retired)})'))
    per_gym = [r for r in report['per_gym'] if r['gym_id'] == retired]
    assert per_gym and per_gym[0]['uncovered_unheld_rows'] == 1
    sql(f'delete from public.content_calendar where id={q(rid)}')


@pytest.mark.parametrize('status',['pending','published','publishing'])
def test_raw_alias_switch_within_tenant_preserves_calendar_and_reservation(status):
    g=gym();t=canon(g);other=tenant_gym(t);scene=alias(g,'https://test/one.jpg')
    rid=insert(g,scene,status=status)
    before=ledger(g)
    sql(f"update public.content_calendar set gym_id={q(other)} where id={q(rid)}")
    assert rows(g)==[] and rows(other)[0]['gym_id']==other
    assert ledger(g)==before and ledger(other)==before
    assert sql(f"select count(*) from public.visual_group_usage_sibling where gym_id={q(t)} and calendar_row_id={q(rid)}")=='1'
    sql(f"delete from public.content_calendar where id={q(rid)}")
    assert rows(other)==[]
    expected='released' if status=='pending' else ('published' if status=='published' else 'reserved')
    assert ledger(other)[0]['state']==expected
    if status=='publishing':assert ledger(other)[0]['ambiguous'] is True


def test_delete_across_raw_alias_siblings_releases_only_last_member():
    g=gym();other=tenant_gym(canon(g));scene=alias(g,'https://test/one.jpg')
    one=insert(g,scene);two=insert(other,scene,account='googlebusiness')
    sql(f"delete from public.content_calendar where id={q(one)}")
    assert ledger(g)[0]['state']=='reserved' and rows(other)[0]['id']==two
    sql(f"delete from public.content_calendar where id={q(two)}")
    assert ledger(g)[0]['state']=='released'
    insert(other,scene,date='2026-10-06')
    assert ledger(other)[0]['reserved_date']=='2026-10-06'


def test_unsent_cross_tenant_move_uses_destination_authority_and_keeps_raw_key():
    g=gym();other=gym();old=alias(g,'https://test/one.jpg');new=alias(other,'https://test/one.jpg')
    rid=insert(g,old)
    sql(f"update public.content_calendar set gym_id={q(other)},visual_group_key={q(new)} where id={q(rid)}")
    assert rows(g)==[] and rows(other)[0]['gym_id']==other and rows(other)[0]['visual_group_key']==new
    assert ledger(g)[0]['state']=='released' and ledger(other)[0]['state']=='reserved'
    assert ledger(other)[0]['gym_id']==canon(other)
    sql(f"delete from public.content_calendar where id={q(rid)}")
    assert ledger(other)[0]['state']=='released'


@pytest.mark.parametrize('status',['published','publishing','failed'])
def test_confirmed_or_ambiguous_row_cannot_cross_tenant(status):
    g=gym();other=gym();old=alias(g,'https://test/one.jpg');new=alias(other,'https://test/one.jpg')
    rid=insert(g,old,status=status);before=rows(g);usage=ledger(g)
    with pytest.raises(RuntimeError,match='confirmed or ambiguous send identity/date cannot change'):
        sql(f"update public.content_calendar set gym_id={q(other)},visual_group_key={q(new)} where id={q(rid)}")
    assert rows(g)==before and rows(other)==[] and ledger(g)==usage and ledger(other)==[]


@pytest.mark.parametrize('destination',['unmapped','null','off'])
def test_enforced_row_cannot_escape_to_unmapped_or_unarmed_tenant(destination):
    g=gym();scene=alias(g,'https://test/one.jpg');rid=insert(g,scene)
    target={'unmapped':'unknown_'+uuid.uuid4().hex,'null':None,'off':gym(False)}[destination]
    before=rows(g);usage=ledger(g)
    with pytest.raises(RuntimeError,match='cannot move to unmapped or unarmed tenant'):
        sql(f"update public.content_calendar set gym_id={q(target)} where id={q(rid)}")
    assert rows(g)==before and ledger(g)==usage


def test_unmapped_row_can_enter_enforced_tenant_only_with_ready_identity():
    raw='unknown_'+uuid.uuid4().hex;g=gym();scene=alias(g,'https://test/one.jpg')
    rid=insert(raw)
    sql(f"update public.content_calendar set gym_id={q(g)} where id={q(rid)}")
    assert rows(raw)==[] and rows(g)[0]['gym_id']==g and rows(g)[0]['visual_group_key']==scene
    assert ledger(g)[0]['gym_id']==canon(g)


def test_canonical_uuid_alias_cannot_bind_another_tenant():
    one,two=str(uuid.uuid4()),str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_tenant_register({q(two)},{q(one)}::uuid)")
    assert canon(two) is None and canon(one) is None
    assert sql(f"select count(*) from public.tenant_alias where alias_key in ({q(one)},{q(two)})")=='0'


def test_preexisting_conflicting_canonical_self_key_rolls_back_new_alias():
    # Emulate an old malformed registry inserted by its owner before the new
    # registration guard. Registration must refuse without rebinding history.
    one,two=str(uuid.uuid4()),str(uuid.uuid4());raw='g_'+uuid.uuid4().hex
    sql(f"insert into public.tenant_alias(alias_key,tenant_id) values({q(two)},{q(one)}::uuid)")
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_tenant_register({q(raw)},{q(two)}::uuid)")
    assert canon(two)==one and canon(raw) is None
    assert sql(f"select count(*) from public.tenant_alias where alias_key={q(raw)}")=='0'


def test_conflicting_canonical_self_key_registration_race_preserves_binding():
    one,two=str(uuid.uuid4()),str(uuid.uuid4());raw='g_'+uuid.uuid4().hex
    result=race(f"select public.visual_group_tenant_register({q(two)},{q(one)}::uuid)",
                f"select public.visual_group_tenant_register({q(raw)},{q(two)}::uuid)")
    assert result[0][1] is not None,result
    assert result[1][1] is None and result[1][0]==two,result
    assert canon(two)==two and canon(raw)==two and canon(one) is None


@pytest.mark.parametrize('operation',['per_row','legacy','redate'])
@pytest.mark.parametrize('caller',['old','current','canonical'])
def test_full_sibling_swap_spans_tenant_aliases_and_preserves_raw_keys(operation,caller):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/old-feed.jpg')
    alias(old,'https://test/old-story.jpg',scene)
    one=insert(old,scene,url='https://test/old-feed.jpg',account='googlebusiness')
    two=insert(current,scene,url='https://test/old-story.jpg' if operation!='legacy' else 'https://test/old-feed.jpg',account='instagram')
    if operation=='per_row':sql(f"update public.content_calendar set format='story' where id={q(two)}")
    target=alias(current,'https://test/new-feed.jpg');alias(current,'https://test/new-story.jpg',target)
    key={'old':old,'current':current,'canonical':canon(old)}[caller]
    if operation=='per_row':
        payload=media_payload([(one,{'image_url':'https://test/new-feed.jpg'}),(two,{'image_url':'https://test/new-story.jpg'})])
        call=f"select public.visual_group_swap_siblings_media({q(key)},{payload},'2026-10-06')"
    elif operation=='legacy':
        call=f"select public.visual_group_swap_siblings({q(key)},{ids_array([one,two])},'2026-10-06',{q(json.dumps({'image_url':'https://test/new-feed.jpg'}))}::jsonb)"
    else:call=f"select public.visual_group_swap_redate({q(key)},{ids_array([one,two])},'2026-10-06')"
    assert sql(call)=='2'
    by_id={r['id']:r for r in rows(old)+rows(current)}
    assert by_id[one]['gym_id']==old and by_id[two]['gym_id']==current
    assert all(r['post_date']=='2026-10-06' for r in by_id.values())
    expected=scene if operation=='redate' else target
    assert all(r['visual_group_key']==expected for r in by_id.values())
    if operation=='per_row':
        assert by_id[two]['image_url']=='https://test/new-story.jpg' and by_id[two]['format']=='story'
    assert next(l for l in ledger(old) if l['group_key']==expected)['reserved_date']=='2026-10-06'


@pytest.mark.parametrize('operation',['per_row','legacy','redate'])
def test_partial_or_foreign_cross_alias_sibling_swap_refuses_atomically(operation):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    one=insert(old,scene);two=insert(current,scene,account='googlebusiness')
    other=gym();foreign_scene=alias(other,'https://test/one.jpg');foreign=insert(other,foreign_scene)
    alias(old,'https://test/new.jpg');before=rows(old)+rows(current)+rows(other);usage=ledger(old)
    for ids in ([one],[one,two,foreign]):
        if operation=='per_row':
            call=f"select public.visual_group_swap_siblings_media({q(old)},{media_payload([(rid,{'image_url':'https://test/new.jpg'}) for rid in ids])},'2026-10-06')"
        elif operation=='legacy':
            call=f"select public.visual_group_swap_siblings({q(old)},{ids_array(ids)},'2026-10-06',{q(json.dumps({'image_url':'https://test/new.jpg'}))}::jsonb)"
        else:call=f"select public.visual_group_swap_redate({q(old)},{ids_array(ids)},'2026-10-06')"
        with pytest.raises(RuntimeError):sql(call)
        assert rows(old)+rows(current)+rows(other)==before and ledger(old)==usage


@pytest.mark.parametrize('outcome',['confirmed_not_sent','confirmed_published'])
@pytest.mark.parametrize('caller',['old','current','canonical'])
@pytest.mark.parametrize('evidence_key',['original','current'])
def test_terminal_evidence_after_same_tenant_alias_switch_binds_original_attempt(outcome,caller,evidence_key):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    rid=uncertain(old,scene,channel='story');proof=evidence(old,rid,scene,outcome=outcome)
    original_claims=json.loads(json.dumps(proof['claims']))
    sql(f"update public.content_calendar set gym_id={q(current)} where id={q(rid)}")
    if evidence_key=='current':proof['gym_id']=current
    key={'old':old,'current':current,'canonical':canon(old)}[caller]
    result=reconcile(key,rid,scene,proof,outcome=outcome)
    assert result['calendar_updated'] is True and rows(old)==[]
    row=rows(current)[0]
    assert row['gym_id']==current and row['status']==('published' if outcome=='confirmed_published' else 'killed')
    assert ledger(old)[0]['state']==('published' if outcome=='confirmed_published' else 'released')
    receipt=json.loads(sql(f"select to_jsonb(r) from public.visual_group_reconciliation r where id={q(result['receipt_id'])}::uuid"))
    assert receipt['gym_id']==canon(old) and receipt['attempts']==original_claims and receipt['evidence']==proof
    assert reconcile(old,rid,scene,proof,outcome=outcome)['idempotent'] is True


@pytest.mark.parametrize('change',['tenant','unknown_key','attempt','claim','provider','url'])
def test_alias_equivalence_alone_cannot_confirm_wrong_provider_attempt(change):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    rid=uncertain(old,scene);proof=evidence(old,rid,scene,outcome='confirmed_published')
    sql(f"update public.content_calendar set gym_id={q(current)} where id={q(rid)}")
    before=rows(current);usage=ledger(old)
    if change=='tenant':proof['gym_id']=gym()
    elif change=='unknown_key':proof['gym_id']='unknown_'+uuid.uuid4().hex
    elif change=='attempt':proof['claims'][0]['attempt_id']=str(uuid.uuid4())
    elif change=='claim':proof['claims'][0]['claim_token']=str(uuid.uuid4())
    elif change=='provider':proof['provider_post_id']='wrong-provider'
    else:proof['delivered_url']='https://test/not-delivered.jpg'
    with pytest.raises(RuntimeError):reconcile(current,rid,scene,proof,outcome='confirmed_published')
    assert rows(current)==before and ledger(old)==usage and ledger(old)[0]['ambiguous'] is True
    assert sql(f"select count(*) from public.visual_group_reconciliation where gym_id={q(canon(old))}")=='0'


@pytest.mark.parametrize('historical_day',['2026-09-01','2026-10-05'])
def test_backfill_historical_marker_cannot_finalize_sticky_ambiguity_or_abort_gym(historical_day):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    uncertain_row=uncertain(current,scene)
    assert ledger(old)[0]['group_key']==scene and ledger(old)[0]['state']=='reserved'
    assert ledger(old)[0]['ambiguous'] is True and ledger(old)[0]['reserved_date']=='2026-10-05'
    sql(f"update public.gym_visual_guard_settings set enforce=false where gym_id={q(canon(old))}")
    prior_scene=alias(old,'https://test/prior-confirmed.jpg')
    prior=insert(old,prior_scene,date='2026-08-01',url='https://test/prior-confirmed.jpg',status='published')
    historical=insert(old,scene,date=historical_day,url='https://test/one.jpg',status='published')
    later_scene=alias(current,'https://test/later-fresh.jpg')
    later=insert(old,later_scene,date='2026-10-07',url='https://test/later-fresh.jpg')
    before=rows(old)+rows(current);before_usage=ledger(old)
    before_events=sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(old))}")
    dry=backfill(old,True)
    assert rows(old)+rows(current)==before and ledger(old)==before_usage
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(old))}")==before_events
    assert dry['rows_held_for_review']>=1
    report=backfill(old)
    assert report['rows_held_for_review']>=1
    usage={l['group_key']:l for l in ledger(old)}
    assert usage[scene]['state']=='reserved' and usage[scene]['ambiguous'] is True
    assert usage[scene]['reserved_date']=='2026-10-05'
    assert usage[prior_scene]['state']=='published' and usage[later_scene]['state']=='reserved'
    after=rows(old)+rows(current);by_id={r['id']:r for r in after}
    assert by_id[prior]==next(r for r in before if r['id']==prior)
    assert by_id[uncertain_row]==next(r for r in before if r['id']==uncertain_row)
    assert by_id[historical]['status']=='published' and by_id[historical]['published_at'] is None
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(old))} and alias_value={q(historical)} and action='review_hold'")!='0'
    coverage=json.loads(sql(f"select public.visual_group_conflict_report({q(old)})"))
    assert coverage['activation_ready'] is False
    frozen_usage=ledger(old);frozen_events=sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(old))}")
    backfill(old)
    assert rows(old)+rows(current)==after and ledger(old)==frozen_usage
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(old))}")==frozen_events


def test_cross_alias_opposite_redates_lock_complete_rows_in_uuid_order():
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    one=insert(old,scene);two=insert(current,scene,account='googlebusiness')
    result=race(f"select public.visual_group_swap_redate({q(old)},{ids_array([one,two])},'2026-10-06')",
                f"select public.visual_group_swap_redate({q(current)},{ids_array([two,one])},'2026-10-07')")
    assert all(error is None and output.splitlines()[0]=='2' for output,error in result),result
    both=rows(old)+rows(current)
    assert len(both)==2 and len({r['post_date'] for r in both})==1
    assert {r['gym_id'] for r in both}=={old,current}
    assert ledger(old)[0]['reserved_date']==both[0]['post_date']


@pytest.mark.parametrize('outcome',['confirmed_not_sent','confirmed_published'])
def test_terminal_reconciliation_serializes_with_same_tenant_alias_switch(outcome):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    rid=uncertain(old,scene);proof=evidence(old,rid,scene,outcome=outcome)
    result=race(f"select public.visual_group_reconcile_ambiguous({q(old)},{q(rid)},{q(outcome)},{q(scene)},'2026-10-05',{q(json.dumps(proof))}::jsonb,'test-verifier')",
                f"update public.content_calendar set gym_id={q(current)} where id={q(rid)}")
    assert all(error is None for _,error in result),result
    assert rows(old)==[] and rows(current)[0]['gym_id']==current
    assert rows(current)[0]['status']==('published' if outcome=='confirmed_published' else 'killed')
    assert ledger(old)[0]['state']==('published' if outcome=='confirmed_published' else 'released')


@pytest.mark.parametrize('deleted',[False,True])
def test_unknown_media_original_hold_evidence_survives_alias_switch_and_orphaning(deleted):
    old=gym();current=tenant_gym(canon(old));rid=insert(old,url='https://test/unresolved.jpg')
    sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id='uncertain-provider' where id={q(rid)}")
    proof=evidence(old,rid,None)
    assert proof['claims']==[] and proof['hold_claims']
    sql(f"update public.content_calendar set gym_id={q(current)} where id={q(rid)}")
    if deleted:sql(f"delete from public.content_calendar where id={q(rid)}")
    result=reconcile(current,rid,None,proof)
    assert result['calendar_updated'] is (not deleted)
    if deleted:assert rows(current)==[]
    else:assert rows(current)[0]['status']=='killed'
    assert ledger(old)==[]
    receipt=json.loads(sql(f"select to_jsonb(r) from public.visual_group_reconciliation r where id={q(result['receipt_id'])}::uuid"))
    assert receipt['gym_id']==canon(old) and receipt['evidence']==proof


@pytest.mark.parametrize('marker',['status','published_at'])
def test_backfill_status_marker_cannot_finalize_unresolved_original_unknown_attempt(marker):
    g=gym();url='https://test/original-unknown.jpg';rid=insert(g,url=url)
    sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id='original-uncertain-provider' where id={q(rid)}")
    assert ledger(g)==[]
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))} and alias_value={q(rid)} and actor='runtime_ambiguous_review'")=='1'
    sql(f"update public.gym_visual_guard_settings set enforce=false where gym_id={q(canon(g))}")
    if marker=='status':sql(f"update public.content_calendar set status='published' where id={q(rid)}")
    else:sql(f"update public.content_calendar set published_at='2026-10-02T12:00:00Z' where id={q(rid)}")
    scene=alias(g,url)  # Identity hydration cannot attest provider delivery.
    prior_scene=alias(g,'https://test/prior.jpg')
    prior=insert(g,prior_scene,date='2026-08-01',url='https://test/prior.jpg',status='published',
                 extra=f"update public.content_calendar set published_at='2026-08-01T12:00:00Z' where gym_id={q(g)} and image_url='https://test/prior.jpg'")
    before=rows(g);events=sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}")
    # The stateless helper is false after the marker; persistent unresolved
    # original hold evidence must still prevent a missing-ledger publication.
    assert sql(f"select public.visual_group_row_ambiguous(c) from public.content_calendar c where id={q(rid)}")=='f'
    dry=backfill(g,True)
    assert dry['rows_held_for_review']>=1 and rows(g)==before and ledger(g)==[]
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}")==events
    report=backfill(g)
    assert report['rows_held_for_review']>=1
    assert not any(l['group_key']==scene and l['state']=='published' for l in ledger(g))
    assert next(l for l in ledger(g) if l['group_key']==prior_scene)['state']=='published'
    assert rows(g)==before
    assert not sql(f"select id from public.visual_group_member_event where gym_id={q(canon(g))} and alias_value={q(rid)} and actor='backfill_published' and action='confirmed'")
    after_usage=ledger(g);after_events=sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}")
    backfill(g)
    assert rows(g)==before and ledger(g)==after_usage
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}")==after_events


@pytest.mark.parametrize('marker',["status='published'","published_at=now()"])
def test_same_day_sibling_cannot_finalize_unresolved_ambiguous_group(marker):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    uncertain_row=uncertain(old,scene);sibling=insert(current,scene,account='story')
    before=rows(old)+rows(current);usage=ledger(old)
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"update public.content_calendar set {marker} where id={q(sibling)}")
    for supplied_group in (scene,None):
        with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
            insert(current,supplied_group,account='googlebusiness',status='published')
    assert rows(old)+rows(current)==before and ledger(old)==usage
    sql(f"delete from public.content_calendar where id={q(uncertain_row)}")
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"update public.content_calendar set {marker} where id={q(sibling)}")
    assert ledger(old)[0]['state']=='reserved' and ledger(old)[0]['ambiguous'] is True


def test_confirmed_non_delivery_clears_group_for_normal_sibling_publication():
    g=gym();scene=alias(g,'https://test/one.jpg');rid=uncertain(g,scene)
    sibling=insert(g,scene,account='story')
    proof=evidence(g,rid,scene)
    reconcile(g,rid,scene,proof)
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is False
    sql(f"update public.content_calendar set status='published',published_at=now() where id={q(sibling)}")
    assert ledger(g)[0]['state']=='published'


def test_one_confirmed_delivery_is_permanent_but_other_ambiguous_sibling_still_needs_evidence():
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    one=uncertain(old,scene);two=uncertain(current,scene,channel='story')
    proof=evidence(old,one,scene,outcome='confirmed_published')
    assert reconcile(old,one,scene,proof,outcome='confirmed_published')['calendar_updated'] is True
    assert ledger(old)[0]['state']=='published'
    sql(f"update public.content_calendar set gym_id={q(current)} where id={q(one)}")
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"update public.content_calendar set status='published' where id={q(two)}")
    proof=evidence(current,two,scene,outcome='confirmed_published')
    assert reconcile(current,two,scene,proof,outcome='confirmed_published')['calendar_updated'] is True
    sql(f"delete from public.content_calendar where gym_id={q(current)}")
    assert ledger(old)[0]['state']=='published'


@pytest.mark.parametrize('old_record',['c','null::public.content_calendar'])
def test_direct_sync_helper_cannot_promote_durable_ambiguity(old_record):
    g=gym();scene=alias(g,'https://test/one.jpg');rid=uncertain(g,scene);usage=ledger(g)
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"select public.visual_group_sync_row({old_record},jsonb_populate_record(c,'{{\"status\":\"published\"}}'::jsonb),'update') from public.content_calendar c where id={q(rid)}")
    assert ledger(g)==usage and rows(g)[0]['status']=='failed'


def test_user_guc_and_non_delivery_receipt_cannot_authorize_ambiguous_publication():
    g=gym();scene=alias(g,'https://test/one.jpg');rid=uncertain(g,scene)
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"begin; set local visual_group.reconciled='true'; update public.content_calendar set status='published' where id={q(rid)}; commit")
    other=uncertain(g,scene,channel='story');proof=evidence(g,rid,scene)
    # A valid non-delivery receipt for one sibling cannot attest delivery of
    # another, even inside the same transaction with matching tenant/date.
    with pytest.raises(RuntimeError,match='requires terminal provider reconciliation'):
        sql(f"begin; select public.visual_group_reconcile_ambiguous({q(g)},{q(rid)},'confirmed_not_sent',{q(scene)},'2026-10-05',{q(json.dumps(proof))}::jsonb,'test-verifier'); update public.content_calendar set status='published' where id={q(other)}; commit")
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True
    assert all(r['status']=='failed' for r in rows(g))


@pytest.mark.parametrize('mutation',[
    'group_insert','alias_insert','event_insert','ledger_insert','ledger_update',
    'settings_insert','settings_arm','sibling_insert','sibling_update',
])
def test_service_role_cannot_directly_mutate_visual_authority_tables(mutation):
    g=gym();t=canon(g);scene=alias(g,'https://test/one.jpg');rid=uncertain(g,scene)
    spare=alias(g,'https://test/spare.jpg')
    statements={
      'group_insert':("visual_group",f"insert into public.visual_group(gym_id,group_key) values({q(t)},'forged-group')"),
      'alias_insert':("visual_group_alias",f"insert into public.visual_group_alias(gym_id,group_key,alias_kind,alias_value) values({q(t)},{q(scene)},'canonical_url','https://test/forged.jpg')"),
      'event_insert':("visual_group_member_event",f"insert into public.visual_group_member_event(gym_id,group_key,action,actor) values({q(t)},{q(scene)},'confirmed','forged-verifier')"),
      'ledger_insert':("visual_group_usage_ledger",f"insert into public.visual_group_usage_ledger(gym_id,group_key,reserved_date,state) values({q(t)},{q(spare)},'2026-10-05','published')"),
      'ledger_update':("visual_group_usage_ledger",f"update public.visual_group_usage_ledger set state='published' where gym_id={q(t)} and group_key={q(scene)}"),
      'settings_insert':("gym_visual_guard_settings",f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(canon(gym(False)))},true)"),
      'settings_arm':("gym_visual_guard_settings",f"update public.gym_visual_guard_settings set enforce=true where gym_id={q(t)}"),
      'sibling_insert':("visual_group_usage_sibling",f"insert into public.visual_group_usage_sibling(gym_id,group_key,calendar_row_id) values({q(t)},{q(scene)},{q(str(uuid.uuid4()))}::uuid)"),
      'sibling_update':("visual_group_usage_sibling",f"update public.visual_group_usage_sibling set channel='forged-channel' where gym_id={q(t)} and calendar_row_id={q(rid)}::uuid"),
    }
    table,statement=statements[mutation]
    frozen=sql(f"select coalesce(jsonb_agg(to_jsonb(r) order by to_jsonb(r)::text),'[]') from public.{table} r")
    with pytest.raises(RuntimeError,match='permission denied for table '+table):
        sql('set role service_role; '+statement)
    assert sql(f"set role service_role; select coalesce(jsonb_agg(to_jsonb(r) order by to_jsonb(r)::text),'[]') from public.{table} r")==frozen
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True


def test_service_role_cannot_execute_internal_sync_mutator():
    g=gym();scene=alias(g,'https://test/one.jpg');rid=insert(g,scene);usage=ledger(g)
    assert sql("select has_function_privilege('service_role','public.visual_group_sync_row(public.content_calendar,public.content_calendar,text)','EXECUTE')")=='f'
    with pytest.raises(RuntimeError,match='permission denied for function visual_group_sync_row'):
        sql(f"set role service_role; select public.visual_group_sync_row(null::public.content_calendar,jsonb_populate_record(c,'{{\"status\":\"published\"}}'::jsonb),'update') from public.content_calendar c where id={q(rid)}")
    assert ledger(g)==usage


def test_service_role_validated_registration_claim_and_evidence_rpcs_still_work():
    g=gym();url='https://test/service-verified.jpg'
    scene=sql(f"set role service_role; select public.visual_group_register_alias({q(g)},'canonical_url',{q(url)},null)")
    sql(f"set role service_role; select public.visual_group_confirm({q(g)},{q(scene)},'canonical_url',{q(url)},'trusted-scene-reviewer')")
    rid=str(uuid.uuid4())
    sql(f"set role service_role; insert into public.content_calendar(id,gym_id,post_date,account,image_url,status) values({q(rid)}::uuid,{q(g)},'2026-10-05','googlebusiness',{q(url)},'pending')")
    token=sql(f"set role service_role; select public.claim_calendar_publish_slot_owned({q(rid)},{q(g)},'2026-10-05','UTC',2,false)")
    assert str(uuid.UUID(token))==token
    assert ledger(g)[0]['state']=='reserved' and ledger(g)[0]['ambiguous'] is True
    proof=evidence(g,rid,scene,outcome='confirmed_published')
    result=reconcile(g,rid,scene,proof,outcome='confirmed_published',role='service_role')
    assert result['calendar_updated'] is True and rows(g)[0]['status']=='published'
    assert ledger(g)[0]['state']=='published'


@pytest.mark.parametrize('deleted_unsent',[False,True])
@pytest.mark.parametrize('delivery_lane',['evidence','unambiguous_marker'])
def test_proven_unsent_sibling_can_reconcile_after_other_delivery_without_releasing_permanent_scene(deleted_unsent,delivery_lane):
    old=gym();current=tenant_gym(canon(old));scene=alias(old,'https://test/one.jpg')
    unsent=insert(old,scene);published=insert(current,scene,account='story')
    if delivery_lane=='evidence':
        sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id={q('unsent-'+unsent)} where id={q(unsent)}")
        sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id={q('sent-'+published)} where id={q(published)}")
        proof=evidence(current,published,scene,outcome='confirmed_published')
        reconcile(current,published,scene,proof,outcome='confirmed_published')
    else:
        sql(f"update public.content_calendar set status='published',published_at=now(),late_post_id={q('sent-'+published)} where id={q(published)}")
        sql(f"update public.content_calendar set status='failed',publish_claim_token=gen_random_uuid(),late_post_id={q('unsent-'+unsent)} where id={q(unsent)}")
    usage=ledger(old);confirmed=rows(current)[0];proof=evidence(old,unsent,scene)
    assert usage[0]['state']=='published' and usage[0]['calendar_row_id']==unsent
    if deleted_unsent:sql(f"delete from public.content_calendar where id={q(unsent)}")
    result=reconcile(current,unsent,scene,proof)
    assert result['calendar_updated'] is (not deleted_unsent)
    assert ledger(old)==[{**usage[0], 'ambiguous': False}] and rows(current)[0]==confirmed
    if deleted_unsent:assert rows(old)==[]
    else:assert rows(old)[0]['status']=='killed'
    membership=json.loads(sql(f"select to_jsonb(s) from public.visual_group_usage_sibling s where gym_id={q(canon(old))} and calendar_row_id={q(unsent)}::uuid"))
    assert membership['ambiguous'] is False and membership['state']=='released'
    assert reconcile(old,unsent,scene,proof)['idempotent'] is True
    with pytest.raises(RuntimeError):insert(old,scene,date='2026-10-06')
    with pytest.raises(RuntimeError):sql(f"set role service_role; update public.visual_group_usage_ledger set state='released' where gym_id={q(canon(old))}")


@pytest.mark.parametrize('bad_binding',['published_attempt','claim','provider','tenant','group'])
def test_permanent_scene_does_not_allow_false_non_delivery_reconciliation(bad_binding):
    g=gym();scene=alias(g,'https://test/one.jpg');sent=uncertain(g,scene);unsent=uncertain(g,scene,channel='story')
    delivery=evidence(g,sent,scene,outcome='confirmed_published')
    reconcile(g,sent,scene,delivery,outcome='confirmed_published')
    proof=evidence(g,unsent,scene);row_id=unsent;group=scene
    if bad_binding=='published_attempt':
        row_id=sent;proof=evidence(g,sent,scene)
    elif bad_binding=='claim':proof['claims'][0]['claim_token']=str(uuid.uuid4())
    elif bad_binding=='provider':proof['claims'][0]['provider_post_id']='not-original-provider'
    elif bad_binding=='tenant':proof['gym_id']=gym()
    else:
        group=alias(g,'https://test/other-scene.jpg');proof['group_key']=group
    before=rows(g);usage=ledger(g)
    with pytest.raises(RuntimeError):reconcile(g,row_id,group,proof)
    assert rows(g)==before and ledger(g)==usage and usage[0]['state']=='published'
    assert next(r for r in rows(g) if r['id']==sent)['status']=='published'


def link(g, a, b, actor='blake', proof=None):
    proof = proof or {'basis': 'human visual review of both exact files'}
    return json.loads(sql(f"select public.visual_group_link_scene({q(g)},{q(a)},{q(b)},{q(json.dumps(proof))}::jsonb,{q(actor)})"))


def links(g):
    return json.loads(sql(f"select coalesce(jsonb_agg(to_jsonb(l) order by l.group_key_a,l.group_key_b),'[]') from public.visual_group_scene_link l where gym_id={q(canon(g))}"))


def test_scene_link_requires_human_evidence_and_registered_tenant_groups():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    for stmt in [
        f"select public.visual_group_link_scene({q(g)},{q(a)},{q(b)},'{{}}'::jsonb,'blake')",
        f"select public.visual_group_link_scene({q(g)},{q(a)},{q(b)},null,'blake')",
        f"select public.visual_group_link_scene({q(g)},{q(a)},{q(b)},{q(json.dumps({'basis':'x'}))}::jsonb,'  ')",
        f"select public.visual_group_link_scene({q(g)},{q(a)},{q(a)},{q(json.dumps({'basis':'x'}))}::jsonb,'blake')",
        f"select public.visual_group_link_scene({q(g)},{q(a)},'vg_missing',{q(json.dumps({'basis':'x'}))}::jsonb,'blake')",
        f"select public.visual_group_link_scene({q(g)},null,{q(b)},{q(json.dumps({'basis':'x'}))}::jsonb,'blake')",
    ]:
        with pytest.raises(RuntimeError):
            sql(stmt)
    # Cross-tenant refusal: a key registered only under another canonical
    # tenant cannot be linked here, and the reverse call fails there too.
    g2 = gym(); foreign = alias(g2, 'https://test/foreign.jpg')
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_link_scene({q(g)},{q(a)},{q(foreign)},{q(json.dumps({'basis':'x'}))}::jsonb,'blake')")
    with pytest.raises(RuntimeError):
        sql(f"select public.visual_group_link_scene({q(g2)},{q(foreign)},{q(a)},{q(json.dumps({'basis':'x'}))}::jsonb,'blake')")
    assert links(g) == [] and links(g2) == []


def test_scene_union_two_group_shared_date_other_date_fails():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    result = link(g, a, b)
    assert result['linked'] is True and result['idempotent'] is False
    assert result['component'] == sorted([a, b]) and result['cross_date_conflicts'] == []
    insert(g, a, date='2026-10-05', account='instagram', url='https://test/one.jpg')
    # Pending members share one date across channels, even on a different key.
    insert(g, b, date='2026-10-05', account='facebook', url='https://test/two.jpg')
    insert(g, b, date='2026-10-05', account='story', url='https://test/two.jpg')
    # Another date on any member fails atomically: no row, no ledger write.
    with pytest.raises(RuntimeError):
        insert(g, b, date='2026-10-06', url='https://test/two.jpg')
    with pytest.raises(RuntimeError):
        insert(g, a, date='2026-10-06', url='https://test/one.jpg')
    assert len(rows(g)) == 3 and len(ledger(g)) == 2
    # Union metadata is persistent, immutable and idempotent.
    repeat = link(g, b, a)  # reverse argument order normalizes to one edge
    assert repeat['idempotent'] is True and len(links(g)) == 1
    with pytest.raises(RuntimeError):
        sql(f"update public.visual_group_scene_link set group_key_b={q(a)} where gym_id={q(canon(g))}")
    with pytest.raises(RuntimeError):
        sql(f"delete from public.visual_group_scene_link where gym_id={q(canon(g))}")


def test_scene_union_three_group_transitive_cycle_safe():
    g = gym()
    a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg'); c = alias(g, 'https://test/three.jpg')
    link(g, a, b); link(g, b, c)
    assert sorted(link(g, a, c)['component']) == sorted([a, b, c])
    insert(g, a, date='2026-10-05', url='https://test/one.jpg')
    # Transitivity: C sees A's reservation through B.
    insert(g, c, date='2026-10-05', account='story', url='https://test/three.jpg')
    with pytest.raises(RuntimeError):
        insert(g, c, date='2026-10-09', url='https://test/three.jpg')
    # Cycle-safe: re-linking an already connected pair is an idempotent no-op,
    # never an error, duplicate edge or infinite walk.
    assert link(g, a, c)['idempotent'] is True
    assert link(g, c, a)['idempotent'] is True
    assert len(links(g)) == 2
    assert sql(f"select count(*) from public.visual_group_member_event where gym_id={q(canon(g))} and action='scene_linked'") == '2'


def test_scene_union_published_old_group_new_group_refusal():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    rid = insert(g, a, date='2026-10-05', url='https://test/one.jpg')
    sql(f"update public.content_calendar set status='published',published_at=now() where id={q(rid)}")
    link(g, a, b)
    # The scene is permanently used: a linked NEW group key refuses any other
    # date, while the published old-group ledger stays byte-identical.
    with pytest.raises(RuntimeError):
        insert(g, b, date='2026-10-06', url='https://test/two.jpg')
    insert(g, b, date='2026-10-05', account='facebook', url='https://test/two.jpg')
    frozen = ledger(g)
    with pytest.raises(RuntimeError):
        sql(f"update public.visual_group_usage_ledger set reserved_date='2026-10-07' where gym_id={q(canon(g))} and group_key={q(a)}")
    assert ledger(g) == frozen
    # Link rows survive with published history; old aliases/groups untouched.
    assert len(links(g)) == 1 and len(rows(g)) == 2


def test_scene_union_race_independent_different_day_claim():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    insert(g, a, date='2026-10-05', url='https://test/one.jpg')
    claim = f"insert into public.content_calendar(gym_id,post_date,account,image_url,visual_group_key,status) values({q(g)},'2026-10-06','facebook','https://test/two.jpg',{q(b)},'pending')"
    union = f"select public.visual_group_link_scene({q(g)},{q(a)},{q(b)},{q(json.dumps({'basis':'human'}))}::jsonb,'blake')"
    (union_out, union_err), (claim_out, claim_err) = race(union, claim)
    assert (union_err is None) != (claim_err is None), (union_err, claim_err)
    if claim_err is None:
        # The different-day claim won. Armed union must refuse the conflicting
        # occupied dates rather than link them under live enforcement.
        assert 'armed tenant scene union conflicts' in union_err
        assert len(links(g)) == 0
    else:
        result = json.loads(union_out.splitlines()[-1])
        # The union won the serialization: the different-day claim fails
        # atomically and the component keeps one date.
        assert result['cross_date_conflicts'] == [], result
        assert json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))['scene_cross_date_conflicts'] == []
        assert {r['post_date'] for r in rows(g)} == {'2026-10-05'}


def test_scene_union_reverse_order_lock_race_no_deadlock():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    forward = f"select public.visual_group_link_scene({q(g)},{q(a)},{q(b)},{q(json.dumps({'basis':'h'}))}::jsonb,'blake')"
    reverse = f"select public.visual_group_link_scene({q(g)},{q(b)},{q(a)},{q(json.dumps({'basis':'h'}))}::jsonb,'blake')"
    result = race(forward, reverse)
    assert all(err is None for _, err in result), result
    outcomes = [json.loads(out.splitlines()[-1]) for out, _ in result]
    assert sorted(o['linked'] for o in outcomes) == [False, True]
    assert len(links(g)) == 1


def test_scene_union_ambiguous_member_retains_component_claim():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    rid = insert(g, a, date='2026-10-05', url='https://test/one.jpg')
    sql(f"update public.content_calendar set status='publishing',publish_claim_token=gen_random_uuid() where id={q(rid)}")
    assert ledger(g)[0]['ambiguous'] is True
    result = link(g, a, b)
    assert result['ambiguous_group_keys'] == [a]
    # The ambiguous member still holds the scene's date on every linked key.
    with pytest.raises(RuntimeError):
        insert(g, b, date='2026-10-06', url='https://test/two.jpg')
    insert(g, b, date='2026-10-05', account='story', url='https://test/two.jpg')
    assert len(ledger(g)) == 2


def test_scene_union_historical_conflict_reported_blocks_activation_no_rewrite():
    g = gym(); a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    r1 = insert(g, a, date='2026-10-05', url='https://test/one.jpg')
    r2 = insert(g, b, date='2026-10-06', url='https://test/two.jpg')
    sql(f"update public.content_calendar set status='published',published_at=now() where id in ({q(r1)},{q(r2)})")
    frozen = ledger(g)
    # Historical discovery can link conflicting occupied dates only while OFF.
    sql(f'update public.gym_visual_guard_settings set enforce=false where gym_id={q(canon(g))}')
    result = link(g, a, b)
    # Pre-existing cross-date published history is reported, never rewritten.
    assert result['cross_date_conflicts'] == ['2026-10-05', '2026-10-06']
    assert sorted(result['published_group_keys']) == sorted([a, b])
    report = json.loads(sql(f'select public.visual_group_conflict_report({q(g)})'))
    assert report['activation_ready'] is False
    scene = report['scene_cross_date_conflicts']
    assert len(scene) == 1 and sorted(scene[0]['group_keys']) == sorted([a, b])
    assert scene[0]['dates'] == ['2026-10-05', '2026-10-06']
    assert ledger(g) == frozen and len(links(g)) == 1
    # And no member can move to a third date while the conflict stands.
    sql(f'update public.gym_visual_guard_settings set enforce=true where gym_id={q(canon(g))}')
    with pytest.raises(RuntimeError):
        insert(g, a, date='2026-10-07', url='https://test/one.jpg')
    with pytest.raises(RuntimeError):
        insert(g, b, date='2026-10-07', url='https://test/two.jpg')


def test_backfill_holds_new_future_row_under_linked_scene_authority():
    g = gym(False)
    a = alias(g, 'https://test/one.jpg'); b = alias(g, 'https://test/two.jpg')
    link(g, a, b)
    insert(g, a, date='2026-10-05', url='https://test/one.jpg')
    future = insert(g, b, date='2026-10-06', account='facebook', url='https://test/two.jpg')
    report = backfill(g)
    assert report['rows_grouped'] == 1 and report['rows_held_for_review'] == 1
    held = [r for r in rows(g) if r['id'] == future][0]
    assert held['media_not_ready_reason'] == 'linked_scene_cross_date_hold'
    assert len(ledger(g)) == 1
    # Repeat runs stay idempotent: no duplicate holds, no new ledger rows.
    before = rows(g)
    events = sql(f'select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}')
    backfill(g)
    assert rows(g) == before and len(ledger(g)) == 1
    assert sql(f'select count(*) from public.visual_group_member_event where gym_id={q(canon(g))}') == events
