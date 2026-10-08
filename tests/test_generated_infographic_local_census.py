"""Offline census observations and disposable PostgreSQL owner authority proof."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import uuid

import pytest

from agent import forward_media_owner as owner
from agent import generated_infographic_local_census as census

ROOT = Path(__file__).resolve().parents[1]


def image_bytes():
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(output, format='PNG')
    return output.getvalue()


def envelope(row, gym='gym'):
    return dict(calendar_row_id=row, enabled=True, epoch_id=str(uuid.uuid4()), sources=[], assets=[],
                source_revision='sha256:' + 'a' * 64, asset_revision='sha256:' + 'b' * 64,
                snapshot=dict(gym_id=gym, format='feed', account='instagram', local_date='2026-10-10',
                              logical_post_id=str(uuid.uuid4()), group_key='vg_exact',
                              inventory_revision='sha256:' + 'c' * 64,
                              photo_inventory_complete=True, eligible_photo_count=0, history_complete=True,
                              history_revision='history-v1', history=dict(scope_complete=True, epoch={'generation': 1})))


class Conn:
    autocommit = False
    def __init__(self, current):
        self.current, self.events, self.records = current, [], {}
        self.fail_commit = False
        self.before_record = None
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, query, params=()):
        self.events.append(query)
        if 'current_user' in query:
            self.result = ('isolated_owner',)
        elif census.SNAPSHOT_RPC in query:
            self.result = (copy.deepcopy(self.current),)
        elif 'fixer_still_inventory_record_20261007' in query:
            if self.before_record:
                self.before_record()
            rid, row, revision, complete, available, ref, epoch, expected, observed = params
            if json.loads(expected) != self.current or epoch != self.current['epoch_id']:
                raise ValueError('binding changed')
            assert not (complete and available == 0), 'complete zero must never reach authority'
            self.records[rid] = dict(receipt_id=rid, epoch_id=epoch, gym_id=self.current['snapshot']['gym_id'],
                                    inventory_revision=revision, local_complete=complete, local_available=available,
                                    evidence_ref=ref, observed_at=observed)
            self.result = (rid,)
        elif census.RECEIPT_RPC in query:
            self.result = (self.records[params[0]],)
        else:
            raise AssertionError(query)
    def fetchone(self):
        return self.result
    def rollback(self):
        self.events.append('rollback')
    def commit(self):
        self.events.append('commit')
        if self.fail_commit:
            raise OSError('sensitive driver failure')


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setattr(owner, 'check_environment', lambda: None)
    root = tmp_path.resolve()
    lib = root / 'gym'
    lib.mkdir()
    db = root / 'rotation.db'
    with sqlite3.connect(db) as conn:
        conn.execute('create table served(id integer primary key,account_key text,key text,date text,content_hash text)')
    row = str(uuid.uuid4())
    current = envelope(row)
    conn = Conn(current)
    config = census.CensusConfig('gym', lib, db, root / 'receipts', 'SYNTHETIC complete migrated durable history', True)
    persistence = owner.ForwardMediaOwnerPersistence(conn, 'isolated_owner', None)
    return SimpleNamespace(config=config, p=persistence, row=row, current=current, conn=conn, lib=lib, db=db)


def run(s, **kwargs):
    return census.run_census(s.row, persistence=s.p, config=s.config, **kwargs)


def test_default_off_touches_nothing(system):
    s = system
    out = census.run_census(s.row, persistence=s.p, config=replace(s.config, enabled=False))
    assert out['reason'] == 'local_census_disabled'
    assert s.conn.events == [] and not s.config.receipt_directory.exists()


def test_positive_local_photo_is_exact_and_recorded(system):
    s = system
    (s.lib / 'new.png').write_bytes(image_bytes())
    out = run(s)
    assert out['ok'] and out['recorded'] and out['receipt']['available'] == 1
    stored = json.loads(Path(out['evidence_path']).read_text())
    assert stored == out['receipt'] and stored['complete']
    assert stored['epoch_id'] == s.current['epoch_id']
    assert stored['evidence_ref'] == 'local-census:' + census._digest({k: v for k, v in stored.items() if k != 'evidence_ref'})
    assert 'commit' in s.conn.events and s.conn.events[-1] == 'rollback'
    assert (Path(out['evidence_path']).stat().st_mode & 0o777) == 0o600


def test_clean_empty_inventory_never_certifies_zero(system):
    out = run(system)
    assert out['held'] and out['recorded'] and not out['receipt']['complete']
    assert out['receipt']['available'] is None and out['receipt']['observed_available'] == 0
    assert out['reason'] == 'local_census_all_writer_fence_unavailable'
    assert next(iter(system.conn.records.values()))['local_complete'] is False


def test_once_used_cross_platform_and_content_rename(system):
    s = system
    pixels = image_bytes()
    (s.lib / 'renamed.png').write_bytes(pixels)
    with sqlite3.connect(s.db) as conn:
        conn.execute('insert into served values(1,?,?,?,?)', ('gym_fb', 'old.png', '2025-01-01', hashlib.sha256(pixels).hexdigest()))
    out = run(s)
    assert out['receipt']['local']['available'] == 0 and not out['receipt']['complete']


def test_other_gym_history_does_not_consume_photo(system):
    s = system
    (s.lib / 'new.png').write_bytes(image_bytes())
    with sqlite3.connect(s.db) as conn:
        conn.execute('insert into served values(1,?,?,?,?)', ('other_ig', 'new.png', '2026-10-01', ''))
    assert run(s)['receipt']['available'] == 1


def test_wrong_gym_and_owner_environment_fail_closed(system, monkeypatch):
    s = system
    s.current['snapshot']['gym_id'] = 'other'
    assert not run(s)['recorded']
    s.current['snapshot']['gym_id'] = 'gym'
    def reject():
        raise owner.EnvironmentGuardError('shared publisher credential must stay private')
    monkeypatch.setattr(owner, 'check_environment', reject)
    out = run(s)
    assert out['reason'] == 'local_census_isolated_owner_required' and not out['recorded']


def test_invalid_configuration_has_no_filesystem_side_effect(system):
    s = system
    s.config = replace(s.config, library_path=s.lib.parent)
    out = run(s)
    assert out['reason'] == 'local_census_library_binding_invalid'
    assert not s.config.receipt_directory.exists() and not s.conn.records


@pytest.mark.parametrize('case', ['missing_db', 'missing_history_ref', 'corrupt_sidecar', 'pending_db_inventory', 'uncertain_history', 'nested'])
def test_uncertain_local_or_history_is_recorded_incomplete(system, case):
    s = system
    if case == 'missing_db':
        s.db.unlink()
    elif case == 'missing_history_ref':
        s.config = replace(s.config, rotation_history_ref='')
    elif case == 'corrupt_sidecar':
        (s.lib / 'new.png').write_bytes(image_bytes())
        (s.lib / 'new.json').write_text('{')
    elif case == 'pending_db_inventory':
        s.current['snapshot']['photo_inventory_complete'] = False
    elif case == 'uncertain_history':
        s.current['snapshot']['history_complete'] = False
    else:
        (s.lib / 'carousel').mkdir()
    out = run(s)
    assert out['held'] and out['recorded'] and out['receipt']['available'] is None
    assert next(iter(s.conn.records.values()))['local_complete'] is False
    assert not s.db.exists() if case == 'missing_db' else True


@pytest.mark.parametrize('field,value', [('enabled', False), ('epoch_id', None), ('sources', None)])
def test_missing_owner_snapshot_authority_never_records(system, field, value):
    system.current[field] = value
    out = run(system)
    assert out['held'] and not out['recorded'] and not system.conn.records


def connected(s, *, md5=None):
    s.current['sources'] = [dict(id='source', gym_id='gym', kind='gym_drive', active=True, folder_id='folder')]
    if md5:
        s.current['assets'] = [dict(id='photo', gym_id='gym', source_id='source', content_hash=md5)]
    return s.current['sources'][0]


def test_missing_connector_is_incomplete_and_transaction_ends_before_read(system):
    s = system
    connected(s)
    class Reader:
        def observe(self, source):
            assert s.conn.events[-1] == 'rollback'
            raise OSError('must not expose credentials or folder path')
    out = run(s, source_reader=Reader())
    assert out['reason'] == 'local_census_source_unavailable' and out['recorded']
    assert 'credentials' not in json.dumps(out)


def test_new_unindexed_connected_photo_is_incomplete_supply(system):
    s = system
    connected(s)
    reader = SimpleNamespace(observe=lambda source: [dict(id='new', name='new.jpg', mimeType='image/jpeg', md5Checksum='a' * 32)])
    out = run(s, source_reader=reader)
    assert out['reason'] == 'local_census_source_inventory_changed'
    assert out['recorded'] and out['receipt']['available'] is None


def test_fresh_connected_positive_observation(system):
    s = system
    connected(s, md5='a' * 32)
    s.current['snapshot']['eligible_photo_count'] = 1
    reader = SimpleNamespace(observe=lambda source: [dict(id='photo', name='photo.jpg', mimeType='image/jpeg', md5Checksum='a' * 32)])
    out = run(s, source_reader=reader)
    assert out['ok'] and out['receipt']['available'] == 1
    assert out['receipt']['sources'][0]['source_id'] == 'source'


def test_disconnected_source_cannot_be_omitted_by_injected_reader(system):
    s = system
    connected(s)
    s.current['sources'][0]['active'] = False
    out = run(s, source_reader=SimpleNamespace(observe=lambda _: []))
    assert out['reason'] == 'local_census_source_unavailable' and out['recorded']
    assert out['receipt']['complete'] is False


@pytest.mark.parametrize('change', ['epoch', 'source', 'calendar', 'inventory'])
def test_final_cas_rejects_binding_changes(system, change):
    s = system
    (s.lib / 'new.png').write_bytes(image_bytes())
    def mutate():
        if change == 'epoch':
            s.current['epoch_id'] = str(uuid.uuid4())
        elif change == 'source':
            s.current['source_revision'] = 'sha256:' + 'd' * 64
        elif change == 'inventory':
            s.current['snapshot']['inventory_revision'] = 'sha256:' + 'e' * 64
        else:
            s.current['snapshot']['logical_post_id'] = str(uuid.uuid4())
    s.conn.before_record = mutate
    out = run(s)
    assert out['held'] and not out['recorded'] and not s.conn.records
    assert 'commit' not in s.conn.events


def test_local_addition_between_final_read_and_cas_never_authorizes_zero(system):
    s = system
    s.conn.before_record = lambda: (s.lib / 'late.png').write_bytes(image_bytes())
    out = run(s)
    assert out['held'] and out['recorded']
    assert next(iter(s.conn.records.values()))['local_complete'] is False


def test_commit_unknown_keeps_evidence_and_never_retries(system):
    s = system
    (s.lib / 'new.png').write_bytes(image_bytes())
    s.conn.fail_commit = True
    out = run(s)
    assert out['reason'] == 'local_census_commit_uncertain' and not out['recorded']
    assert Path(out['evidence_path']).is_file()
    assert s.conn.events.count('commit') == 1


def test_stale_pass_and_receipt_reimport_are_not_authority(system):
    s = system
    start = datetime.now(timezone.utc)
    ticks = iter([start, start + timedelta(minutes=6)])
    out = run(s, clock=lambda: next(ticks))
    assert out['reason'] == 'local_census_observation_stale' and not out['recorded']
    assert not s.conn.records
    fresh = run(s)
    assert fresh['receipt']['receipt_id'] != out['receipt']['receipt_id']
    assert len(list(s.config.receipt_directory.glob('*.json'))) == 2


def test_never_changes_coach_review_or_approval(system):
    s = system
    (s.lib / 'new.png').write_bytes(image_bytes())
    metadata = dict(approved=False, review=True, moderation='rejected', consent='pending')
    path = s.lib / 'new.json'
    path.write_text(json.dumps(metadata))
    before = path.read_bytes()
    assert not run(s)['receipt']['complete']
    assert path.read_bytes() == before
    assert all('update' not in e and 'delete' not in e for e in s.conn.events)


class Request:
    def __init__(self, value):
        self.value = value
    def execute(self, **kwargs):
        assert kwargs == {'num_retries': 0}
        return self.value


def drive_reader(pages):
    class Files:
        def get(self, **kwargs):
            return Request(dict(id='folder', mimeType=census._FOLDER, trashed=False))
        def list(self, **kwargs):
            assert 'pageSize' in kwargs and 'incompleteSearch' in kwargs['fields']
            return Request(next(pages))
    service = SimpleNamespace(files=lambda: Files())
    return census.DedicatedDriveInventory(SimpleNamespace(_bounded_service=lambda: service))


@pytest.mark.parametrize('pages', [
    [{}], [dict(files=[], incompleteSearch=True)],
    [dict(files=[], nextPageToken='repeat'), dict(files=[], nextPageToken='repeat')],
    [dict(files=[dict(id='shortcut', name='shortcut', mimeType='application/vnd.google-apps.shortcut', parents=['folder'], trashed=False)])],
    [dict(files=[dict(id='other', name='photo.jpg', mimeType='image/jpeg', parents=['unrelated'], trashed=False)])],
])
def test_malformed_partial_shortcut_or_wrong_parent_drive_walk_holds(pages):
    reader = drive_reader(iter(pages))
    with pytest.raises(census.CensusHold):
        reader.observe(dict(id='source', kind='gym_drive', active=True, folder_id='folder'))


def test_drive_walk_consumes_all_pages_uncached():
    file = dict(id='photo', name='photo.png', mimeType='image/png', parents=['folder'], trashed=False, md5Checksum='a' * 32)
    reader = drive_reader(iter([dict(files=[], nextPageToken='second'), dict(files=[file])]))
    assert reader.observe(dict(id='source', kind='gym_drive', active=True, folder_id='folder')) == [file]


def test_disposable_pg_epoch_cas_and_partial_supersession(tmp_path, monkeypatch):
    """Real draft SQL, isolated roles, first epoch, immutable replay, no zero."""
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    if not pg.is_dir():
        pytest.skip('PostgreSQL 17 binaries unavailable')
    psycopg = pytest.importorskip('psycopg')
    portal = ROOT.parent / 'portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql'
    if not portal.is_file():
        pytest.skip('assembled sibling portal source-brand draft unavailable')
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    monkeypatch.setattr(owner, 'check_environment', lambda: None)
    with tempfile.TemporaryDirectory(prefix='local_census_pg_', dir='/tmp') as temp:
        root = Path(temp)
        sock = root / 'sock'
        sock.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(pg / 'initdb'), '-D', str(root / 'data'), '-U', 'postgres', '--no-sync'], check=True, capture_output=True, timeout=60)
        subprocess.run([str(pg / 'pg_ctl'), '-D', str(root / 'data'), '-l', str(root / 'pg.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True, timeout=60)
        connections = []
        try:
            def connect(role='postgres'):
                c = psycopg.connect(f'host={sock} port={port} dbname=postgres user={role}', autocommit=role == 'postgres')
                c.execute("set statement_timeout='5s'")
                if role != 'postgres':
                    c.commit()
                connections.append(c)
                return c
            admin = connect()
            admin.execute('''create role anon; create role authenticated; create role service_role login;
                create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);
                create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);
                create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);
                create table gyms(id uuid primary key);
                create table app_users(id uuid primary key,clerk_user_id text unique,role text,email text);
                create table echo_intake_tokens(gym_id uuid primary key,echo_account_key text unique);''')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql', 'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql', 'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                         'DRAFT_fixer_owner_photo_clearance_20261007.sql', 'DRAFT_fixer_generated_owner_20261007.sql'):
                admin.execute((ROOT / 'migrations' / name).read_text())
            admin.execute('create role census_owner login; grant fixer_forward_media_owner_20261006 to census_owner; create role mixed_owner login; grant fixer_forward_media_owner_20261006,service_role to mixed_owner')
            row, logical, baseline = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            admin.execute("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,'gym',%s,'2026-10-10','instagram','feed','pending','active','vg_group','SYNTHETIC caption')", (row, logical))
            admin.execute("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC',true,'complete_fleet_still_photo_history',null,'SYNTHETIC routes','SYNTHETIC reviewer')")
            admin.execute("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC',true,'[]','SYNTHETIC empty complete',0)", (baseline,))
            admin.execute("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'", (baseline,))
            c = connect('census_owner')
            def rpc(conn, name, *args):
                return conn.execute('select public.' + name + '(' + ','.join(['%s'] * len(args)) + ')', args).fetchone()[0]
            # Seed a historical zero using the exact legacy seam BEFORE upgrade.
            rpc(c, 'fixer_still_cutover_control_20261007', True, 'SYNTHETIC authorized disposable cutover')
            old_snap = rpc(c, 'fixer_generated_snapshot_20261007', row)
            old_id = str(uuid.uuid4())
            rpc(c, 'fixer_still_inventory_record_20261007', old_id, row, old_snap['inventory_revision'], True, 0, 'SYNTHETIC old zero')
            c.commit()
            producer_sql = (ROOT / 'migrations' / 'DRAFT_fixer_generated_local_census_producer_20261008.sql').read_text()
            # A lower ordered stack must fail before creating any producer RPC.
            with pytest.raises(psycopg.Error, match='bundle bridge must precede'):
                admin.execute(producer_sql)
            admin.execute('rollback')
            assert admin.execute("select to_regprocedure('public.fixer_generated_local_census_snapshot_20261008(uuid)')").fetchone()[0] is None
            for name in ('DRAFT_fixer_generated_gap_dispatch_20261007.sql',
                         'DRAFT_generated_source_palette_authority_20261007.sql',
                         'DRAFT_generated_send_lease_20261007.sql'):
                admin.execute((ROOT / 'migrations' / name).read_text())
            admin.execute(portal.read_text())
            admin.execute((ROOT / 'migrations' / 'DRAFT_fixer_generated_bundle_bridge_20261008.sql').read_text())
            consumer_names = ('fixer_generated_runtime_check_20261007', 'fixer_still_reservation_check_20261007',
                              'fixer_still_final_check_20261007')
            acl_sql = 'select proname,proacl::text from pg_proc where proname=any(%s) order by proname'
            before_acls = admin.execute(acl_sql, (list(consumer_names),)).fetchall()
            admin.execute(producer_sql)
            assert admin.execute(acl_sql, (list(consumer_names),)).fetchall() == before_acls
            def denied(conn, name, *args):
                with pytest.raises(psycopg.Error):
                    rpc(conn, name, *args)
                conn.rollback()
            service, mixed = connect('service_role'), connect('mixed_owner')
            denied(service, census.SNAPSHOT_RPC, row)
            denied(mixed, census.SNAPSHOT_RPC, row)
            denied(c, 'fixer_still_inventory_record_20261007', str(uuid.uuid4()), row, old_snap['inventory_revision'], True, 0, 'SYNTHETIC old entry')
            current = rpc(c, census.SNAPSHOT_RPC, row)
            assert current['enabled'] and str(current['epoch_id'])
            c.rollback()
            observed = datetime.now(timezone.utc)
            receipt_id = str(uuid.uuid4())
            args = (receipt_id, row, current['snapshot']['inventory_revision'], False, 0, 'local-census:sha256:' + 'a' * 64,
                    current['epoch_id'], json.dumps(current), observed)
            assert str(rpc(c, 'fixer_still_inventory_record_20261007', *args)) == receipt_id
            stored = rpc(c, census.RECEIPT_RPC, receipt_id, row)
            assert stored['local_complete'] is False and stored['local_available'] == 0
            c.commit()
            assert str(rpc(c, 'fixer_still_inventory_record_20261007', *args)) == receipt_id
            c.commit()
            assert admin.execute('select count(*) from fixer_still_inventory_20261007').fetchone()[0] == 2
            newest = admin.execute('select local_complete,receipt_id from fixer_still_inventory_20261007 order by observed_at desc,receipt_id desc limit 1').fetchone()
            assert newest == (False, uuid.UUID(receipt_id))
            denied(c, 'fixer_still_inventory_record_20261007', *args[:3], True, *args[4:])
            denied(c, 'fixer_still_inventory_record_20261007', *args[:-1], observed - timedelta(minutes=6))
            denied(c, 'fixer_still_inventory_record_20261007', *args[:-1], observed + timedelta(minutes=2))
            denied(c, 'fixer_still_inventory_record_20261007', *args[:6], str(uuid.uuid4()), *args[7:])
            denied(c, 'fixer_still_inventory_record_20261007', *args[:5], 'local-census:sha256:' + 'b' * 64, *args[6:])
            changed = copy.deepcopy(current)
            changed['snapshot']['logical_post_id'] = str(uuid.uuid4())
            denied(c, 'fixer_still_inventory_record_20261007', *args[:7], json.dumps(changed), observed)
            admin.execute("update content_calendar set caption='SYNTHETIC changed binding' where id=%s", (row,))
            denied(c, 'fixer_still_inventory_record_20261007', *args)
            # Actual Python -> owner RPC -> partial record -> readback, empty
            # local library, fresh complete rotation, and zero remains held.
            lib = tmp_path.resolve() / 'gym'
            lib.mkdir()
            db = tmp_path.resolve() / 'rotation.db'
            with sqlite3.connect(db) as local_db:
                local_db.execute('create table served(id integer primary key,account_key text,key text,date text,content_hash text)')
            config = census.CensusConfig('gym', lib, db, tmp_path.resolve() / 'receipts', 'SYNTHETIC complete local history', True)
            out = census.run_census(row, persistence=owner.ForwardMediaOwnerPersistence(c, 'census_owner', None), config=config)
            assert out['recorded'] and out['held'] and out['reason'] == 'local_census_all_writer_fence_unavailable'
            (lib / 'unused.png').write_bytes(image_bytes())
            positive = census.run_census(row, persistence=owner.ForwardMediaOwnerPersistence(c, 'census_owner', None), config=config)
            assert positive['ok'] and positive['recorded'] and positive['receipt']['available'] == 1
            assert admin.execute('select local_complete,local_available from fixer_still_inventory_20261007 where receipt_id=%s',
                                 (positive['receipt']['receipt_id'],)).fetchone() == (True, 1)
            assert admin.execute('select status,caption from content_calendar where id=%s', (row,)).fetchone() == ('pending', 'SYNTHETIC changed binding')
            # First epoch is visible even when a gym has no earlier receipt.
            other = str(uuid.uuid4())
            admin.execute("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,'other',%s,'2026-10-10','instagram','feed','pending','active','vg_other','SYNTHETIC other')", (other, str(uuid.uuid4())))
            first = rpc(c, census.SNAPSHOT_RPC, other)
            assert first['epoch_id'] == current['epoch_id'] and first['sources'] == []
            c.rollback()
            _assert_final_consumers_use_newest(admin, c, service, mixed, rpc, denied)
        finally:
            for conn in connections:
                conn.close()
            subprocess.run([str(pg / 'pg_ctl'), '-D', str(root / 'data'), '-m', 'immediate', '-w', 'stop'], check=True, capture_output=True, timeout=60)


def _assert_final_consumers_use_newest(admin, owner_conn, service, mixed, rpc, denied):
    """Actual ordered draft functions; trust fixtures inserted by DB owner only.

    Synthetic generated reservations model retained legacy authority. No fake
    provider delivery, source-brand approval, production DSN or paid generation.
    """
    for role_conn in (owner_conn, service, mixed):
        denied(role_conn, 'fixer_local_census_latest_private_20261008', 'gym')
        with pytest.raises(Exception, match='permission denied'):
            role_conn.execute('select * from fixer_still_inventory_20261007')
        role_conn.rollback()
    for private_name in ('fixer_generated_runtime_check_20261007', 'fixer_still_reservation_check_20261007'):
        signature = private_name + ('(uuid)' if 'runtime' in private_name else '(uuid,jsonb,text,uuid,uuid)')
        for role in ('anon', 'authenticated', 'service_role', 'fixer_forward_media_owner_20261006',
                     'fixer_forward_media_attester_20261006'):
            assert not admin.execute("select has_function_privilege(%s,%s,'EXECUTE')",
                                     (role, 'public.' + signature)).fetchone()[0]
    assert admin.execute("select has_function_privilege('fixer_forward_media_owner_20261006',"
                         "'public.fixer_still_final_check_20261007(uuid,uuid)','EXECUTE')").fetchone()[0]

    def make_row(gym):
        rid, logical = uuid.uuid4(), uuid.uuid4()
        admin.execute("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) "
                      "values(%s,%s,%s,'2026-10-10','instagram','feed','pending','active',%s,'SYNTHETIC consumer')",
                      (rid, gym, logical, 'vg_' + logical.hex))
        return rid

    def receipt(gym, revision, *, complete=True, available=0, epoch=None, age=1):
        epoch = epoch or admin.execute('select epoch_id from fixer_still_cutover_20261007').fetchone()[0]
        rid = uuid.uuid4()
        admin.execute("insert into fixer_still_inventory_20261007 values(%s,%s,%s,%s,%s,%s,"
                      "'SYNTHETIC owner observed census',clock_timestamp()-(%s * interval '1 second'))",
                      (rid, epoch, gym, revision, complete, available, age))
        return rid

    def original(gym, label):
        return dict(gym_id=gym, source_asset_id=label, source_url='https://owned.example/' + label + '.png',
                    sha256='sha256:' + hashlib.sha256(label.encode()).hexdigest(),
                    md5='md5:' + hashlib.md5(label.encode()).hexdigest(),
                    phash='scene:phash64:' + hashlib.sha256(label.encode()).hexdigest()[:16], length=100)

    def clearance(o):
        rid = uuid.uuid4()
        rpc(owner_conn, 'fixer_still_known_record_20261007', rid, 'cleared_fresh', json.dumps(o),
            'authenticated-post-epoch:SYNTHETIC exact trusted original')
        owner_conn.commit()
        return rid

    for case in ('incomplete', 'positive', 'revision', 'epoch', 'stale', 'future', 'superseded_valid'):
        gym = 'latest-' + case
        still_row, generated_row = make_row(gym), make_row(gym)
        o = original(gym, case + '-graphic')
        clear = clearance(o)
        snap = rpc(admin, 'fixer_generated_snapshot_20261007', generated_row)
        old_epoch = admin.execute('select epoch_id from fixer_still_cutover_20261007').fetchone()[0]
        old_id = receipt(gym, snap['inventory_revision'], age=1800 if case == 'stale' else 60)
        bound = uuid.uuid4()
        # A retained immutable still reservation bound to the old zero.
        admin.execute("insert into fixer_still_reservation_20261007 "
                      "select %s,%s,id,gym_id,post_date,logical_post_id,visual_group_key,'graphic',%s::jsonb,%s,%s,clock_timestamp() "
                      "from content_calendar where id=%s", (bound, old_epoch, json.dumps(o), old_id, clear, still_row))
        job = uuid.uuid4()
        generated_original = original(gym, case + '-generated')
        candidate_json = dict(job_id=str(job), gym_id=gym, local_date=snap['local_date'],
                              logical_post_id=snap['logical_post_id'], copy_revision=snap['copy_revision'],
                              inventory_revision=snap['inventory_revision'], original_url=generated_original['source_url'],
                              original_sha256=generated_original['sha256'][7:], original_md5=generated_original['md5'][4:],
                              original_phash=generated_original['phash'])
        manifest = dict(manifest_digest='sha256:' + hashlib.sha256(case.encode()).hexdigest())
        admin.execute("insert into fixer_generated_reservation_20261007 "
                      "select %s,id,visual_group_key,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s from content_calendar where id=%s",
                      (job, json.dumps(candidate_json), json.dumps(manifest), 'SYNTHETIC generated ' + case,
                       json.dumps(snap['history']['epoch']), 'client-source:sha256:' + 'a' * 64, generated_row))
        admin.execute("update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s,render_manifest_digest=%s where id=%s",
                      ('generated-astra:' + str(job), generated_original['source_url'], generated_original['source_url'],
                       manifest['manifest_digest'], generated_row))
        def reserve_check(inventory):
            rpc(admin, 'fixer_still_reservation_check_20261007', still_row, json.dumps(o), 'graphic', inventory, clear)
        def final_check():
            rpc(owner_conn, 'fixer_still_final_check_20261007', still_row, bound)
            owner_conn.rollback()
        def generated_check():
            assert rpc(admin, 'fixer_generated_runtime_check_20261007', generated_row) is True
        if case != 'stale':
            reserve_check(old_id)
            final_check()
            generated_check()
        if case == 'revision':
            admin.execute("insert into media_source values(%s,%s,'gym_drive','SYNTHETIC changed source',false,'ready',clock_timestamp())",
                          (case + '-source', gym))
            changed = rpc(admin, 'fixer_generated_snapshot_20261007', generated_row)['inventory_revision']
            assert changed != snap['inventory_revision']
            newer = receipt(gym, changed)
            admin.execute('delete from media_source where id=%s', (case + '-source',))
            assert rpc(admin, 'fixer_generated_snapshot_20261007', generated_row)['inventory_revision'] == snap['inventory_revision']
        elif case == 'epoch':
            new_epoch = uuid.uuid4()
            admin.execute('update fixer_still_cutover_20261007 set epoch_id=%s', (new_epoch,))
            newer = receipt(gym, snap['inventory_revision'], complete=False, epoch=new_epoch)
        else:
            newer = receipt(gym, snap['inventory_revision'], complete=case != 'incomplete',
                            available=1 if case == 'positive' else 0,
                            age=1200 if case == 'stale' else -60 if case == 'future' else 1)
        with pytest.raises(Exception, match='inventory authority unavailable'):
            reserve_check(old_id)
        if case == 'superseded_valid':
            # Explicit new reservation uses current ID; old immutable ID holds.
            reserve_check(newer)
            generated_check()
        else:
            with pytest.raises(Exception, match='inventory authority unavailable'):
                reserve_check(newer)
            with pytest.raises(Exception, match='fresh local depletion authority'):
                generated_check()
        denied(owner_conn, 'fixer_still_final_check_20261007', still_row, bound)
        assert admin.execute('select inventory_receipt from fixer_still_reservation_20261007 where receipt_id=%s',
                             (bound,)).fetchone()[0] == old_id
        if case == 'epoch':
            admin.execute('update fixer_still_cutover_20261007 set epoch_id=%s', (old_epoch,))
        if case == 'superseded_valid':
            restaged = uuid.uuid4()
            assert rpc(owner_conn, 'fixer_still_reserve_20261007', restaged, still_row, json.dumps(o),
                       'graphic', newer, clear) == restaged
            owner_conn.commit()
            rpc(owner_conn, 'fixer_still_final_check_20261007', still_row, restaged)
            owner_conn.rollback()

    # Eligible photo retains positive-inventory behavior and current review
    # checks; generated/graphic depletion must still hold when photo supply exists.
    gym = 'latest-photo'
    row = make_row(gym)
    o = original(gym, 'current-reviewed-photo')
    source_id = 'current-reviewed-photo-source'
    now = datetime.now(timezone.utc).isoformat()
    moderation = dict(verdict='clean', provider='SYNTHETIC moderator', content_hash=o['md5'][4:],
                      asset_id=o['source_asset_id'], gym_id=gym, people_detected=False, observed_at=now,
                      sha256=o['sha256'][7:])
    admin.execute("insert into media_source values(%s,%s,'gym_drive','SYNTHETIC folder',true,'ready',clock_timestamp())",
                  (source_id, gym))
    admin.execute("insert into media_asset(id,source_id,gym_id,content_hash,kind,eligible,excluded_by_coach,review_status,moderation_status,"
                  "review_content_hash,reviewed_by,reviewed_at,moderation_json,people_detected) "
                  "values(%s,%s,%s,%s,'photo',true,false,'approved','clean',%s,'SYNTHETIC reviewer',clock_timestamp(),%s::jsonb,false)",
                  (o['source_asset_id'], source_id, gym, o['md5'][4:], o['md5'][4:], json.dumps(moderation)))
    snap = rpc(admin, 'fixer_generated_snapshot_20261007', row)
    assert snap['photo_inventory_complete'] and snap['eligible_photo_count'] == 1
    positive = receipt(gym, snap['inventory_revision'], available=1)
    clear, bound = clearance(o), uuid.uuid4()
    assert rpc(owner_conn, 'fixer_still_reserve_20261007', bound, row, json.dumps(o), 'photo', positive, clear) == bound
    owner_conn.commit()
    rpc(owner_conn, 'fixer_still_final_check_20261007', row, bound)
    owner_conn.rollback()
    denied(owner_conn, 'fixer_still_reserve_20261007', uuid.uuid4(), row, json.dumps(o), 'graphic', positive, clear)
    admin.execute("update media_asset set review_status='pending_review',moderation_status='pending' where id=%s", (o['source_asset_id'],))
    denied(owner_conn, 'fixer_still_final_check_20261007', row, bound)
