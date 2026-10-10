"""Disposable PG17 CAS adverse cases with real predecessor lock/auth triggers.

Narrow contract fixture, not full assembled fleet migration acceptance. Tests
install DRAFT SQL in private temporary PG only; no production DSN or activation.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile
import uuid

import pytest

ROOT=Path(__file__).resolve().parents[1]
PG=Path('/opt/homebrew/opt/postgresql@17/bin')
MIGRATION=ROOT/'migrations/DRAFT_fixer_remote_drive_use_cas_20261008.sql'


def function(filename,name):
    text=(ROOT/'migrations'/filename).read_text()
    return re.search(r'create(?: or replace)? function public\.'+name+r'\([\s\S]*?\$\$;',text).group()


@pytest.fixture
def pg(request):
    psycopg=pytest.importorskip('psycopg')
    if not PG.is_dir(): pytest.skip('PG17 unavailable')
    assert shutil.disk_usage('/tmp').free>5*1024**3
    with tempfile.TemporaryDirectory(prefix='remote_drive_cas_pg_',dir='/tmp') as tmp:
        temp=Path(tmp)
        socket=temp/'socket'; socket.mkdir()
        port=random.randint(41000,59000)
        subprocess.run([str(PG/'initdb'),'-D',str(temp/'data'),'-U','bootstrap','--no-sync'],check=True,capture_output=True,timeout=60)
        subprocess.run([str(PG/'pg_ctl'),'-D',str(temp/'data'),'-l',str(temp/'log'),'-o',f"-k {socket} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
        connections=[]
        try:
            def connect(role='postgres',autocommit=True):
                c=psycopg.connect(f'host={socket} port={port} dbname=postgres user={role}',autocommit=autocommit)
                connections.append(c)
                return c
            bootstrap=connect('bootstrap')
            bootstrap.execute('''create role postgres login nosuperuser createrole createdb;
            grant all on schema public to postgres;
            create role anon;create role authenticated;create role service_role login bypassrls;
            create role fixer_forward_media_owner_20261006 nologin;
            create role fixer_forward_media_attester_20261006 nologin;
            create role generated_authority_publisher_20261007 nologin;
            create role generated_send_reconciler_20261007 nologin;
            create role writer login;create role mixed login;
            create role inherited_acl nologin;
            create role other_owner nologin;
            grant service_role to mixed;''')
            admin=connect()
            # Match production PG17's non-superuser CREATEROLE installation:
            # Its role setting adds ADMIN/SET, non-INHERIT creator membership.
            admin.execute("set createrole_self_grant='set'")
            admin.execute('''create role fixer_inventory_mutator_20261008 nologin noinherit;
            grant fixer_inventory_mutator_20261008 to writer,mixed;
            create table fixer_inventory_generation_20261008(gym_id text primary key,generation bigint not null);
            create table fixer_still_cutover_20261007(singleton boolean primary key,enabled boolean,epoch_id uuid);
            create table media_source(id text primary key,gym_id text,kind text,active boolean,folder_id text);
            create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,
             eligible boolean,excluded_by_coach boolean,used_count integer,last_used_at timestamptz);
            ''')
            protocol='DRAFT_fixer_inventory_mutation_protocol_20261008.sql'
            for name in ('fixer_inventory_protocol_lock_private_20261008','fixer_inventory_runtime_caller_private_20261008',
                         'fixer_inventory_mutator_check_private_20261008','fixer_inventory_bump_private_20261008',
                         'fixer_inventory_state_write_lock_20261008','fixer_inventory_database_write_20261008'):
                admin.execute(function(protocol,name))
            admin.execute(function('DRAFT_fixer_generated_owner_20261007.sql','fixer_generated_inventory_lock_20261007'))
            for table in ('media_source','media_asset'):
                admin.execute(f'create trigger generated_inventory_lock before insert or update or delete or truncate on {table} for each statement execute function fixer_generated_inventory_lock_20261007()')
                admin.execute(f'create trigger inventory_generation_20261008 after insert or update or delete on {table} for each row execute function fixer_inventory_database_write_20261008()')
            # Snapshot unrelated predecessor function bodies and ACLs before install.
            before=admin.execute("select oid,prosrc,proacl::text from pg_proc where proname like 'fixer_inventory_%' order by oid").fetchall()
            # Reproduce hostile creator default ACLs, including transitive
            # privileges inherited by the mutator through another group role.
            bootstrap.execute('grant inherited_acl to fixer_inventory_mutator_20261008 with inherit true')
            admin.execute('''
             alter default privileges grant all on tables to fixer_inventory_mutator_20261008,inherited_acl,writer,service_role;
             alter default privileges grant all on sequences to fixer_inventory_mutator_20261008,inherited_acl,writer,service_role;
             alter default privileges grant all on functions to fixer_inventory_mutator_20261008,inherited_acl,writer,service_role;''')
            failure=getattr(request,'param',None)
            if failure=='owner_inherited':
                bootstrap.execute('grant postgres to writer with inherit true')
            elif failure=='lock_owner_mismatch':
                bootstrap.execute('alter function fixer_inventory_protocol_lock_private_20261008() owner to other_owner')
            elif failure=='new_object_owner_mismatch':
                # Run installation with a different creator while the accepted
                # auth helper remains owned by postgres. This must not bless
                # current_user as the installation identity.
                bootstrap.execute('grant all on media_source,media_asset to bootstrap')
            if failure:
                installer=bootstrap if failure=='new_object_owner_mismatch' else admin
                message='retains raw table authority' if failure=='owner_inherited' else 'installation owner mismatch'
                with pytest.raises(psycopg.Error,match=message) as denied:
                    installer.execute(MIGRATION.read_text())
                assert denied.value.sqlstate=='42501'
                installer.execute('rollback')
                assert admin.execute(
                 "select to_regclass('public.fixer_remote_drive_use_20261008')").fetchone()==(None,)
                assert admin.execute(
                 "select to_regclass('public.fixer_remote_drive_version_20261008')").fetchone()==(None,)
                assert admin.execute("select count(*) from pg_attribute where attrelid in ('media_asset'::regclass,'media_source'::regclass) and attname='drive_use_version'").fetchone()==(0,)
                assert admin.execute("select count(*) from pg_proc where proname like 'fixer_remote_drive_%'").fetchone()==(0,)
                yield None
                return
            admin.execute(MIGRATION.read_text())
            assert admin.execute("select rolsuper,rolcreaterole from pg_roles where rolname='postgres'").fetchone()==(False,True)
            # PG17 records the automatic ADMIN grant and optional self SET
            # grant separately, with different grantors. Inspect both rows.
            assert admin.execute("select admin_option,inherit_option,set_option from pg_auth_members where roleid='fixer_inventory_mutator_20261008'::regrole and member='postgres'::regrole order by admin_option").fetchall()==[(False,False,True),(True,False,False)]
            assert before==admin.execute("select oid,prosrc,proacl::text from pg_proc where proname like 'fixer_inventory_%' order by oid").fetchall()
            epoch=uuid.uuid4()
            admin.execute('insert into fixer_still_cutover_20261007 values(true,true,%s)',(epoch,))
            admin.execute("insert into media_source values('source','gym','gym_drive',true,'folder',default)")
            admin.execute("insert into media_asset values('asset','source','gym','hash',true,false,0,null,default)")
            def request():
                asset=admin.execute("select to_jsonb(a) from media_asset a where id='asset'").fetchone()[0]
                source=admin.execute("select to_jsonb(s) from media_source s where id='source'").fetchone()[0]
                return dict(use_id=str(uuid.uuid4()),gym_id='gym',epoch_id=str(epoch),post_date='2026-10-10',
                            asset_id='asset',source_id='source',content_hash='hash',asset_before=asset,source_before=source)
            def rpc(c,name,r):
                from psycopg.types.json import Jsonb
                return c.execute('select public.'+name+'(%s)',(Jsonb(r),)).fetchone()[0]
            yield admin,connect,request,rpc,psycopg
        finally:
            for c in connections: c.close()
            subprocess.run([str(PG/'pg_ctl'),'-D',str(temp/'data'),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)


def test_atomic_idempotent_and_stale_cas(pg):
    admin,connect,request,rpc,psycopg=pg
    writer=connect('writer')
    r=request()
    with pytest.raises(psycopg.Error,match='disabled'):
        rpc(writer,'fixer_remote_drive_use_apply_20261008',r)
    assert admin.execute("select used_count from media_asset where id='asset'").fetchone()==(0,)
    admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC disposable fixture'")
    receipt=rpc(writer,'fixer_remote_drive_use_apply_20261008',r)
    assert receipt['asset_after']['used_count']==1
    assert receipt['asset_after']['drive_use_version']>r['asset_before']['drive_use_version']
    assert rpc(writer,'fixer_remote_drive_use_apply_20261008',r)==receipt
    assert rpc(writer,'fixer_remote_drive_use_receipt_20261008',r)==receipt
    assert admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone()==(1,)
    altered=dict(r,post_date='2026-10-11')
    with pytest.raises(psycopg.Error,match='conflict'):
        rpc(writer,'fixer_remote_drive_use_apply_20261008',altered)
    stale=dict(r,use_id=str(uuid.uuid4()))
    with pytest.raises(psycopg.Error,match='CAS conflict'):
        rpc(writer,'fixer_remote_drive_use_apply_20261008',stale)
    admin.execute("update media_source set active=false where id='source'")
    admin.execute('update fixer_remote_drive_use_control_20261008 set enabled=false')
    assert rpc(writer,'fixer_remote_drive_use_receipt_20261008',r)==receipt
    assert rpc(writer,'fixer_remote_drive_use_apply_20261008',r)==receipt
    # Neither service-role nor a mixed writer nor a superuser session can use RPC.
    for role in ('service_role','mixed','bootstrap'):
        with pytest.raises(psycopg.Error):
            rpc(connect(role),'fixer_remote_drive_use_apply_20261008',r)
    with pytest.raises(psycopg.Error):
        writer.execute('delete from fixer_remote_drive_use_20261008')


@pytest.mark.parametrize('change',['asset_edit','source_edit','tenant','source_inactive','hash','used','aba'])
def test_source_asset_before_image_adverse(pg,change):
    admin,connect,request,rpc,psycopg=pg
    r=request()
    admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC disposable fixture'")
    if change=='asset_edit': admin.execute("update media_asset set eligible=eligible where id='asset'")
    if change=='source_edit': admin.execute("update media_source set folder_id='different' where id='source'")
    if change=='tenant': r['gym_id']='neighbor'
    if change=='source_inactive': admin.execute("update media_source set active=false where id='source'")
    if change=='hash': admin.execute("update media_asset set content_hash='newhash' where id='asset'")
    if change=='used': admin.execute("update media_asset set used_count=1 where id='asset'")
    if change=='aba':
        admin.execute("delete from media_asset where id='asset'")
        admin.execute("insert into media_asset values('asset','source','gym','hash',true,false,0,null,default)")
    with pytest.raises(psycopg.Error,match='CAS conflict'):
        rpc(connect('writer'),'fixer_remote_drive_use_apply_20261008',r)
    assert admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone()==(0,)


def test_concurrent_duplicate_and_transaction_rollback(pg):
    admin,connect,request,rpc,psycopg=pg
    admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC disposable fixture'")
    r=request()
    first,second=connect('writer'),connect('writer')
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda c:rpc(c,'fixer_remote_drive_use_apply_20261008',r),(first,second)))
    assert outcomes[0]==outcomes[1]
    assert admin.execute('select used_count from media_asset').fetchone()==(1,)
    assert admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone()==(1,)
    # Forced receipt insertion failure must roll back the counter and generation.
    admin.execute("insert into media_asset values('second','source','gym','secondhash',true,false,0,null,default)")
    r2=request(); r2['asset_id']='second'; r2['content_hash']='secondhash'
    r2['asset_before']=admin.execute("select to_jsonb(a) from media_asset a where id='second'").fetchone()[0]
    admin.execute("create function receipt_fail() returns trigger language plpgsql as $$begin raise exception 'SYNTHETIC receipt failure'; end$$;create trigger receipt_fail before insert on fixer_remote_drive_use_20261008 for each row execute function receipt_fail()")
    generation=admin.execute('select generation from fixer_inventory_generation_20261008').fetchone()
    with pytest.raises(psycopg.Error,match='receipt failure'):
        rpc(first,'fixer_remote_drive_use_apply_20261008',r2)
    assert admin.execute("select used_count,last_used_at from media_asset where id='second'").fetchone()==(0,None)
    assert admin.execute('select generation from fixer_inventory_generation_20261008').fetchone()==generation


def test_real_pg_authority_sqlite_lost_ack_readback(pg,tmp_path,monkeypatch):
    import sqlite3
    from agent import config as settings, remote_drive_use as remote
    from agent.local_inventory_mutation import MutationConfig,MutationHold
    admin,connect,request,rpc,psycopg=pg
    admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC disposable fixture'")
    library=tmp_path/'library'; gym=library/'gym'; gym.mkdir(parents=True)
    database=tmp_path/'local.db';sqlite3.connect(database).close()
    monkeypatch.setattr(settings,'LIBRARY_PATH',str(library))
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED','true')
    r=request()
    cfg=MutationConfig('gym',r['epoch_id'],gym,database,tmp_path/'journal',tmp_path/'locks')
    connection=connect('writer',autocommit=False)
    class LostAck(remote.DriveUseAuthority):
        def apply_use(self,req):
            super().apply_use(req)
            raise OSError('SYNTHETIC acknowledgement lost after commit')
    authority=LostAck(connection,'writer')
    with pytest.raises(MutationHold,match='outcome_unknown'):
        remote.apply(cfg,authority,r)
    assert admin.execute('select used_count from media_asset').fetchone()==(1,)
    assert admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone()==(1,)
    recovered=remote.apply(cfg,authority,r)
    assert recovered==rpc(connect('writer'),'fixer_remote_drive_use_receipt_20261008',r)
    with sqlite3.connect(database) as local:
        assert local.execute('select state from remote_drive_use_attempt').fetchone()==('confirmed',)


def test_concurrent_distinct_attempts_consume_once(pg):
    admin,connect,request,rpc,psycopg=pg
    admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC disposable fixture'")
    first=request();second=dict(first,use_id=str(uuid.uuid4()))
    connections=[connect('writer'),connect('writer')]
    def consume(pair):
        c,r=pair
        try:
            return rpc(c,'fixer_remote_drive_use_apply_20261008',r)['state']
        except psycopg.Error as e:
            assert e.sqlstate=='23514'
            return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume,zip(connections,(first,second))))==['applied','held']
    assert admin.execute('select used_count from media_asset').fetchone()==(1,)
    assert admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone()==(1,)


def test_default_and_inherited_acl_scrub_and_immutable_owner_dml(pg):
    admin,connect,request,rpc,psycopg=pg
    writer=connect('writer')
    for role in ('writer','fixer_inventory_mutator_20261008','inherited_acl','service_role'):
        for table in ('fixer_remote_drive_use_20261008','fixer_remote_drive_use_control_20261008'):
            assert admin.execute('select has_table_privilege(%s,%s,%s)',
             (role,table,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')).fetchone()==(False,)
            assert admin.execute('select has_any_column_privilege(%s,%s,%s)',
             (role,table,'SELECT,INSERT,UPDATE,REFERENCES')).fetchone()==(False,)
        assert admin.execute('select has_sequence_privilege(%s,%s,%s)',
         (role,'fixer_remote_drive_version_20261008','USAGE,SELECT,UPDATE')).fetchone()==(False,)
        for function_name in ('fixer_remote_drive_version_private_20261008()',
                              'fixer_remote_drive_use_immutable_20261008()'):
            assert admin.execute('select has_function_privilege(%s,%s,%s)',
             (role,function_name,'EXECUTE')).fetchone()==(False,)
    admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC disposable fixture'")
    r=request()
    receipt=rpc(writer,'fixer_remote_drive_use_apply_20261008',r)
    version=admin.execute('select last_value from fixer_remote_drive_version_20261008').fetchone()
    for statement in (
        'truncate fixer_remote_drive_use_20261008',
        'delete from fixer_remote_drive_use_20261008',
        "update fixer_remote_drive_use_20261008 set receipt='{}'",
        "select setval('fixer_remote_drive_version_20261008',1)",
        "select nextval('fixer_remote_drive_version_20261008')"):
        with pytest.raises(psycopg.Error) as denied:
            writer.execute(statement)
        assert denied.value.sqlstate=='42501'
    # Owner ordinarily bypasses RLS, so protect destructive DML with a trigger.
    for statement in ('truncate fixer_remote_drive_use_20261008',
                      'delete from fixer_remote_drive_use_20261008',
                      "update fixer_remote_drive_use_20261008 set receipt='{}'"):
        with pytest.raises(psycopg.Error,match='immutable') as denied:
            admin.execute(statement)
        assert denied.value.sqlstate=='23514'
    assert admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone()==(1,)
    assert admin.execute('select last_value from fixer_remote_drive_version_20261008').fetchone()==version
    assert rpc(writer,'fixer_remote_drive_use_receipt_20261008',r)==receipt
    assert rpc(writer,'fixer_remote_drive_use_apply_20261008',r)==receipt
    assert admin.execute('select used_count from media_asset').fetchone()==(1,)
    # Ordinary index writers need no sequence privilege for before-trigger defaults.
    admin.execute('grant insert on media_source,media_asset to writer')
    writer.execute("insert into media_source(id,gym_id,kind,active,folder_id) values('new','gym','gym_drive',true,'newfolder')")
    writer.execute("insert into media_asset(id,source_id,gym_id,content_hash,eligible,excluded_by_coach,used_count) values('new','new','gym','newhash',true,false,0)")
    assert admin.execute("select drive_use_version>0 from media_source where id='new'").fetchone()==(True,)
    assert admin.execute("select drive_use_version>0 from media_asset where id='new'").fetchone()==(True,)


@pytest.mark.parametrize('pg',['owner_inherited','lock_owner_mismatch','new_object_owner_mismatch'],indirect=True)
def test_installation_holds_unrevokable_owner_inheritance(pg):
    assert pg is None
