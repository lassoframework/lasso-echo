"""Whole calendar overlay and atomic cutover against the live Oct7 claim body.

PG17 private disposable Unix socket; stdlib harness with a pytest entry, no production connections.
Other tranches use their tracked actual entry definitions; schema and autonomy /
digest resolvers are synthetic. Run python3 tests/test_calendar_oct7_overlay_cutover_pg17.py.
"""
import hashlib
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import uuid
import pytest

from test_forward_corpus_atomic_cutover_pg import entry_definitions, function

ROOT = Path(__file__).resolve().parents[1]
CLAIM = 'claim_calendar_publish_slot_owned'
ENTRY = '  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared \'G\', then census\n  -- exclusive \'C\', before the tenant advisory lock and any row lock below.\n  perform public.fixer_forward_calendar_entry_lock_20261008();\n'


def pg17_bin_dir():
    """Find a complete PostgreSQL 17 installation without accepting other majors."""
    candidates = []
    for env_name in ('POSTGRESQL_17_BIN', 'PG17_BIN'):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value))
    pg_config = shutil.which('pg_config')
    if pg_config:
        try:
            bindir = subprocess.run([pg_config, '--bindir'], check=True, capture_output=True,
                                    text=True, timeout=5).stdout.strip()
            if bindir:
                candidates.append(Path(bindir))
        except (OSError, subprocess.SubprocessError):
            pass
    candidates.extend((Path('/opt/homebrew/opt/postgresql@17/bin'),
                       Path('/usr/local/opt/postgresql@17/bin'),
                       Path('/usr/lib/postgresql/17/bin'), Path('/usr/pgsql-17/bin')))
    initdb = shutil.which('initdb')
    if initdb:
        candidates.append(Path(initdb).resolve().parent)
    for directory in candidates:
        if any(not (directory / name).is_file() for name in ('initdb', 'pg_ctl', 'postgres', 'psql')):
            continue
        try:
            version = subprocess.run([str(directory / 'initdb'), '--version'], check=True,
                                      capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if '(PostgreSQL) 17.' in version or 'PostgreSQL 17.' in version:
            return directory
    return None


def main(pg=None):
    pg = pg or pg17_bin_dir()
    if pg is None:
        print('SKIP: PostgreSQL 17 server binaries unavailable; set PG17_BIN')
        return
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    live_sql = (ROOT / 'migrations/lasso_october7_catchup_capacity_20261008.sql').read_text()
    live_body = live_sql.split('$$')[1]
    assert hashlib.md5(live_body.encode()).hexdigest() == 'c624eedcee819496129639108be991f6'
    overlay = (ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql').read_text()
    entry_body = function(overlay, CLAIM).split('$function$')[1]
    assert entry_body.replace(ENTRY, '', 1) == live_body
    assert entry_body.count(ENTRY) == 1
    cutover = (ROOT / 'migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql').read_text()
    assert hashlib.md5(entry_body.encode()).hexdigest() in cutover
    work = Path(tempfile.mkdtemp(prefix='calendar_oct7_compat_pg17_', dir='/tmp'))
    data, sock = work / 'data', work / 'sock'
    sock.mkdir()
    port = random.randint(41000, 59000)
    started = False
    try:
        subprocess.run([str(pg / 'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'], check=True, capture_output=True, timeout=60)
        started = True
        subprocess.run([str(pg / 'pg_ctl'), '-D', str(data), '-l', str(work / 'pg.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True, timeout=60)
        base = [str(pg / 'psql'), '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock), '-p', str(port), '-U', 'postgres']

        def sql(statement, fail=None):
            result = subprocess.run(base, input='set check_function_bodies=off;\n' + statement, text=True, capture_output=True, timeout=60)
            if fail:
                assert result.returncode and fail in result.stderr, result.stderr
            else:
                assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        assert int(sql('show server_version_num')) // 10000 == 17
        sql('create role anon; create role authenticated; create role service_role;'
            'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,'
            'format text,gbp_location_id text,status text,variant_status text,variant_of uuid,published_at timestamptz,'
            'publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,'
            'thumbnail_url text,media_not_ready_reason text,caption text,logical_post_id uuid,'
            'pillar text,slot_index integer,scheduled_at timestamptz,approval_kind text,approved_by text,'
            'approved_at timestamptz,approval_digest text);'
            'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
            'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,'
            'rendition_url text,eligible boolean,excluded_by_coach boolean,review_status text,'
            'reviewed_at timestamptz,reviewed_by text,review_note text,review_content_hash text,'
            'consent_status text,release_ref text,consent_member_ref text,consent_expires_at timestamptz);'
            'create table media_asset_review_event(gym_id text,asset_id text,content_hash text,prior_status text,'
            'decision text,reviewed_by text,reviewed_at timestamptz,review_note text);'
            'create table echo_infographic_artifacts(tenant text,image_url text,image_sha256 text,evidence jsonb,source_identity jsonb);'
            'create table support_tickets(id uuid);')
        for name in ('DRAFT_fixer_forward_media_claim_20261006.sql', 'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                     'DRAFT_fixer_forward_media_source_history_20261007.sql', 'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                     'DRAFT_fixer_owner_photo_clearance_20261007.sql'):
            sql((ROOT / 'migrations' / name).read_text())
        receipt = (ROOT / 'migrations/portal_action_receipt_draft_20261004.sql').read_text()
        sql(receipt[receipt.index('CREATE TABLE IF NOT EXISTS public.portal_action_receipt ('):receipt.index('-- Trusted public media origin:')])
        sql((ROOT / 'tests/fixtures/forward_lock_entry/portal-function-definitions-20261008.sql').read_text())
        # The old frozen claim is no longer a compatible live prerequisite.
        sql(overlay, fail='claim_calendar_publish_slot_owned drifted')
        assert sql("select to_regprocedure('fixer_forward_calendar_entry_lock_20261008()') is null") == 't'
        sql(live_sql)
        metadata_query = "select oid,proowner,proacl,prosecdef,proconfig,pg_get_function_identity_arguments(oid),pg_get_function_result(oid),pronargdefaults from pg_proc where proname='claim_calendar_publish_slot_owned'"
        before = sql(metadata_query)
        sql(overlay)
        assert sql(metadata_query) == before
        assert sql("select md5(prosrc) from pg_proc where proname='claim_calendar_publish_slot_owned'") == hashlib.md5(entry_body.encode()).hexdigest()
        for role in ('anon', 'authenticated', 'service_role'):
            assert sql(f"select has_function_privilege('{role}','fixer_forward_calendar_entry_lock_20261008()','execute')") == 'f'
        assert sql("select has_function_privilege('service_role','claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean,boolean)','execute')") == 't'
        # Only install unrelated actual B definitions after the complete overlay.
        for name, definition in entry_definitions().items():
            if name not in (CLAIM, 'fixer_forward_calendar_entry_lock_20261008') and not name.startswith('fixer_'):
                sql(definition)
        sql('grant truncate on content_calendar to anon;'
            'create table test_flags(auto boolean); insert into test_flags values(true);'
            'create or replace function calendar_gym_is_autonomous(text) returns boolean language sql stable as $$select auto from test_flags$$;'
            "create or replace function calendar_approval_digest(p_row content_calendar) returns text language sql stable as $$select md5(p_row.id::text||coalesce(p_row.caption,'')||coalesce(p_row.image_url,''))$$;")
        # A stale whole claim rejects every cutover section atomically.
        sql(live_sql)
        sql(cutover, fail='atomic corpus cutover definition drift: claim_calendar_publish_slot_owned')
        assert sql("select count(*) from pg_trigger where tgname='000_fixer_forward_corpus_entry_20261008'") == '0'
        assert sql("select has_table_privilege('anon','content_calendar','truncate')") == 't'
        assert sql("select position('SHARE ROW EXCLUSIVE' in prosrc)>0 from pg_proc where proname='portal_action_receipt_apply'") == 't'
        sql(function(overlay, CLAIM))
        sql(cutover)
        assert sql(metadata_query) == before
        assert sql("select count(*) from pg_trigger where tgname='000_fixer_forward_corpus_entry_20261008'") == '7'
        assert sql("select has_table_privilege('anon','content_calendar','truncate')") == 'f'

        def row(day='2026-10-08', account='instagram', fmt='feed', slot='0', gym='lasso', extra=''):
            ident = str(uuid.uuid4())
            sql(f"insert into content_calendar(id,gym_id,post_date,account,format,slot_index,status,variant_status,image_url,caption) values('{ident}','{gym}','{day}','{account}','{fmt}',{slot},'pending','active','https://example.test/photo','caption');" + extra.replace('ROW', ident))
            return ident

        def claim(ident, day='2026-10-08', gym='lasso', tz='America/New_York', capacity='6', proof='false', approved='false'):
            return sql(f"select coalesce(claim_calendar_publish_slot_owned('{ident}','{gym}','{day}','{tz}',{capacity},{approved},{proof})::text,'NULL')")

        for day in ('2026-10-08', '2026-10-09'):
            sql('delete from content_calendar')
            for account in ('instagram', 'facebook'):
                for fmt in ('feed', 'story'):
                    for post_day in (day, '2026-10-07'):
                        tokens = [claim(row(post_day, account, fmt, str(i % 3)), day) for i in range(4)]
                        assert all(t != 'NULL' for t in tokens[:3]) and tokens[3] == 'NULL', tokens
            assert sql('select count(*) from content_calendar where status=\'publishing\'') == '24'
        sql('delete from content_calendar')
        for kwargs in ({'day':'2026-10-07'}, {'day':'2026-10-10'}, {'tz':'UTC'}, {'capacity':'7'},
                       {'capacity':'null'}, {'proof':'null'}, {'approved':'null'}, {'gym':'client'}):
            assert claim(row(), **kwargs) == 'NULL', kwargs
        for kwargs in ({'day':'2026-10-06'}, {'day':'2026-10-09'}, {'account':'googlebusiness'}, {'fmt':'reel'}, {'slot':'3'}, {'slot':'null'}):
            assert claim(row(**kwargs)) == 'NULL', kwargs
        for extra in ("update content_calendar set image_url=null where id='ROW'", "update content_calendar set media_not_ready_reason='hold' where id='ROW'",
                      "update content_calendar set publish_claim_token=gen_random_uuid() where id='ROW'", "update content_calendar set publish_reservation_day='2026-10-08' where id='ROW'"):
            assert claim(row(extra=extra)) == 'NULL'
        sql('update test_flags set auto=false')
        assert claim(row(), proof='true') == 'NULL'
        ident = row(extra="update content_calendar set status='approved' where id='ROW'")
        assert claim(ident, proof='true') == 'NULL'
        sql(f"update content_calendar set approval_kind='human',approved_by='actor',approved_at=now(),approval_digest=calendar_approval_digest(content_calendar) where id='{ident}'")
        assert claim(ident, proof='true') != 'NULL'
        stale = row(extra="update content_calendar set status='approved',approval_kind='human',approved_by='actor',approved_at=now(),approval_digest=calendar_approval_digest(content_calendar) where id='ROW'; update content_calendar set image_url='changed' where id='ROW'")
        assert claim(stale, proof='true') == 'NULL'
        sql('update test_flags set auto=true;delete from content_calendar')
        # Receipt timestamps without reservation days consume current class.
        for _ in range(3):
            row('2026-10-09', extra="update content_calendar set status='published',published_at='2026-10-09 17:00:00+00' where id='ROW'")
        assert claim(row('2026-10-09'), day='2026-10-09') == 'NULL'
        assert claim(row('2026-10-07'), day='2026-10-09') != 'NULL'
        # Both preceding dated envelopes retain their exact class ceilings.
        for day, capacity, backlog_max in (('2026-10-10', '5', 2), ('2026-10-06', '15', 12)):
            sql('delete from content_calendar')
            current = [claim(row(day), day=day, capacity=capacity) for _ in range(4)]
            backlog = [claim(row('2026-10-02'), day=day, capacity=capacity) for _ in range(backlog_max + 1)]
            assert all(t != 'NULL' for t in current[:3]) and current[3] == 'NULL'
            assert all(t != 'NULL' for t in backlog[:backlog_max]) and backlog[-1] == 'NULL'
        sql('delete from content_calendar')
        tokens = [claim(row('2026-10-12'), day='2026-10-12', capacity='3') for _ in range(4)]
        assert all(t != 'NULL' for t in tokens[:3]) and tokens[3] == 'NULL'
        sql("begin isolation level repeatable read; select claim_calendar_publish_slot_owned(gen_random_uuid(),'lasso','2026-10-08','America/New_York',6,false,false);", fail='requires read committed')
        print('PASS: PG17 complete Oct7 body preservation, whole calendar overlay, whole atomic cutover, stale body atomic refusal, OID/owner/ACL/config/signature preservation, 24-lane capacity on both days, negative cases, proof and isolation gates')
        print('LIMIT: synthetic schema and autonomy/digest resolvers; unrelated B entries loaded as full definitions; no production mutation, provider delivery, release acceptance or activation')
    finally:
        if started and (data / 'postmaster.pid').exists():
            subprocess.run([str(pg / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'], check=True, capture_output=True, timeout=30)
        if (data / 'postmaster.pid').exists():
            raise RuntimeError('active disposable cluster preserved: ' + str(work))
        shutil.rmtree(work)


if __name__ == '__main__':
    main()


def test_calendar_oct7_overlay_cutover_pg17():
    pg = pg17_bin_dir()
    if pg is None:
        pytest.skip('requires existing PostgreSQL 17 server binaries (initdb, pg_ctl, postgres, psql); set PG17_BIN')
    main(pg)
