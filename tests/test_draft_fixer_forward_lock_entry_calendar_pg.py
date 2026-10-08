"""Standalone disposable PG acceptance for the 2026-10-08 forward-lock-entry
calendar DRAFT; run python3 this_file.py.

Uses only stdlib and existing initdb/pg_ctl/psql. No DSN, network, production
connection, installs or persisted cluster. The frozen production definitions
are installed from the read-only evidence snapshot, the DRAFT migration is
applied, and the lock-entry ordering, preserved contracts, helper privilege
and the definition-drift precondition guard are verified.
"""
from pathlib import Path
from contextlib import ExitStack
import json
import random
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/forward_lock_entry'
EVIDENCE = FIXTURES / 'portal-function-definitions-20261008.sql'
INVENTORY = FIXTURES / 'portal-legacy-function-inventory-20261008.json'
MIGRATION = ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql'
HELPER = 'public.fixer_forward_calendar_entry_lock_20261008()'
GRAPH_KEY = 'fixer_forward_graph_20261006'
CENSUS_KEY = 'fixer_forward_photo_census_20261007'
TARGETS = [
    'approve_calendar_row_if_media_ready',
    'calendar_recover_unproved_approval',
    'calendar_stamp_verified_approval',
    'claim_calendar_gbp_publish_owned',
    'claim_calendar_publish_slot_owned',
    'content_calendar_swap_variant',
]


def main():
    for name in ('initdb', 'pg_ctl', 'psql'):
        if not shutil.which(name):
            raise SystemExit(f'BLOCKED: existing {name} unavailable; no install attempted')
    assert EVIDENCE.exists() and INVENTORY.exists(), 'frozen evidence files required'
    port = random.randint(41000, 59000)
    with tempfile.TemporaryDirectory(prefix='fixer_entry_lock_pg_', dir='/tmp') as tmp, ExitStack() as cleanup:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        boot = subprocess.run(['initdb', '-D', str(work / 'data'), '-U', 'postgres', '--no-sync'],
                              capture_output=True, timeout=60, text=True)
        if boot.returncode:
            raise SystemExit('BLOCKED: disposable initdb failed: ' + boot.stderr.strip())
        subprocess.run(['pg_ctl', '-D', str(work / 'data'), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        cleanup.callback(subprocess.run,
                         ['pg_ctl', '-D', str(work / 'data'), '-m', 'fast', '-w', 'stop'],
                         check=False, capture_output=True, timeout=60)
        base = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                '-p', str(port), '-U', 'postgres', '-d', 'postgres']

        def sql(s, ok=True):
            result = subprocess.run(base, input=s, text=True, capture_output=True, timeout=60)
            if ok and result.returncode:
                raise AssertionError(result.stderr)
            if not ok:
                assert result.returncode != 0, 'unexpected SQL success'
            return result.stdout.strip() if ok else result.stderr

        # Minimal runtime stubs: entry-lock checks execute real functions, and
        # the migration's REVOKE names the standard portal roles.
        sql("create role anon; create role authenticated; create role service_role;"
            "create table public.content_calendar("
            "id uuid primary key, gym_id text, variant_status text, status text,"
            "variant_of uuid);"
            "create table public.media_source(id text);"
            "create table public.media_asset(id text, gym_id text);"
            "create table public.portal_action_receipt(id uuid);"
            "create table public.support_tickets(id uuid);")

        # Frozen production definitions, installed exactly as captured. Function
        # bodies are the authority under test, so catalog validation is off
        # (the evidence references the full production schema).
        sql("set check_function_bodies=off;\n" + EVIDENCE.read_text())

        # The evidence install must reproduce the inventory hashes; this pins
        # the migration's precondition constants to the captured production
        # bodies before any replace runs.
        inventory = {r['proname']: r['body_md5'] for r in json.loads(INVENTORY.read_text())['rows']}
        for name in TARGETS:
            actual = sql(f"select md5(prosrc) from pg_proc p join pg_namespace n "
                         f"on n.oid=p.pronamespace where n.nspname='public' and p.proname='{name}';")
            assert actual == inventory[name], (name, actual, inventory[name])

        identity_before = {}
        for name in TARGETS:
            identity_before[name] = sql(
                "select pg_get_function_identity_arguments(p.oid)||'|'||"
                "p.prosecdef::text||'|'||p.prorettype::regtype::text "
                "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                f"where n.nspname='public' and p.proname='{name}';")

        # A signature edit preserves prosrc and the old name/count/hash guard
        # would pass, then CREATE OR REPLACE would add an unlocked overload.
        # Exercise the complete migration before the first successful apply.
        frozen_swap = re.search(
            r"CREATE OR REPLACE FUNCTION public.content_calendar_swap_variant\(.*?\$function\$\n;",
            EVIDENCE.read_text(), re.DOTALL).group(0)
        sql("drop function public.content_calendar_swap_variant(text, uuid, text);"
            "set check_function_bodies=off;\n" + frozen_swap.replace(
                "p_actor text DEFAULT NULL::text", "p_actor varchar DEFAULT NULL::varchar"))
        assert sql("select md5(prosrc) from pg_proc where proname="
                   "'content_calendar_swap_variant';") == inventory['content_calendar_swap_variant']
        err = sql(MIGRATION.read_text(), ok=False)
        assert 'drifted from the frozen 2026-10-08 inventory' in err, err
        assert sql("select count(*) from pg_proc where proname="
                   "'content_calendar_swap_variant';") == '1'
        assert sql("select to_regprocedure('public.content_calendar_swap_variant(text,uuid,text)') "
                   "is null;") == 't'
        assert sql("select to_regprocedure('public.fixer_forward_calendar_entry_lock_20261008()') "
                   "is null;") == 't'
        # No earlier target was replaced before rejection.
        for name in TARGETS[:-1]:
            assert sql(f"select md5(prosrc) from pg_proc where proname='{name}';") == inventory[name]
        sql("drop function public.content_calendar_swap_variant(text, uuid, varchar);"
            "set check_function_bodies=off;\n" + frozen_swap)

        # Owner drift and a non-postgres applying role fail closed as well.
        sql("alter function public.content_calendar_swap_variant(text,uuid,text) owner to service_role;")
        err = sql(MIGRATION.read_text(), ok=False)
        assert 'owner service_role' in err, err
        sql("alter function public.content_calendar_swap_variant(text,uuid,text) owner to postgres;")
        err = sql("set role service_role;\n" + MIGRATION.read_text(), ok=False)
        assert 'current_user must be postgres' in err, err

        sql(MIGRATION.read_text())

        # Execute the media prerequisite and frozen-identity guard against the
        # same disposable catalog, without applying its replacements.
        media_sql = (ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_media_20261008.sql').read_text()
        media_guard = 'do $guard$' + media_sql.split('do $guard$', 1)[1].split('$guard$;', 1)[0] + '$guard$;'
        sql(media_guard)
        frozen_finish = re.search(
            r"CREATE OR REPLACE FUNCTION public.finish_gym_media_sync\(.*?\$function\$\n;",
            EVIDENCE.read_text(), re.DOTALL).group(0)
        sql("drop function public.finish_gym_media_sync(text,text,boolean,text);"
            "set check_function_bodies=off;\n" + frozen_finish.replace(
                "p_error text DEFAULT NULL::text", "p_error varchar DEFAULT NULL::varchar"))
        assert sql("select md5(prosrc) from pg_proc where proname='finish_gym_media_sync';") == inventory['finish_gym_media_sync']
        err = sql(media_guard, ok=False)
        assert 'production definition of finish_gym_media_sync drifted' in err, err
        sql("drop function public.finish_gym_media_sync(text,text,boolean,varchar);"
            "set check_function_bodies=off;\n" + frozen_finish)
        sql(f"alter function {HELPER} owner to service_role;")
        err = sql(media_guard, ok=False)
        assert 'missing or mis-shaped' in err, err
        sql(f"alter function {HELPER} owner to postgres;")
        # Even a valid helper plus a same-name overload is a prerequisite drift.
        sql("create function public.fixer_forward_calendar_entry_lock_20261008(integer) "
            "returns void language plpgsql security definer as $$ begin return; end; $$;")
        err = sql(media_guard, ok=False)
        assert 'missing or mis-shaped' in err, err
        sql("drop function public.fixer_forward_calendar_entry_lock_20261008(integer);")
        sql(media_guard)

        # Signatures, SECURITY DEFINER and return types are unchanged.
        for name in TARGETS:
            after = sql(
                "select pg_get_function_identity_arguments(p.oid)||'|'||"
                "p.prosecdef::text||'|'||p.prorettype::regtype::text "
                "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                f"where n.nspname='public' and p.proname='{name}';")
            assert after == identity_before[name], (name, identity_before[name], after)

        # Every target body now enters through the helper, and the entry call
        # precedes every pre-existing advisory/row/table lock acquisition.
        lock_markers = ('pg_advisory_xact_lock(hashtextextended(p_gym_id', 'for update',
                        'for share', 'lock table')
        for name in TARGETS:
            body = sql(f"select prosrc from pg_proc p join pg_namespace n "
                       f"on n.oid=p.pronamespace where n.nspname='public' and p.proname='{name}';")
            # A few frozen bodies describe their row locks in comments before
            # BEGIN. Compare executable lines so those comments are not
            # mistaken for a lock acquired before the helper.
            low = '\n'.join(line.split('--', 1)[0] for line in body.lower().splitlines())
            entry = low.index('perform public.fixer_forward_calendar_entry_lock_20261008();')
            for marker in lock_markers:
                pos = low.find(marker)
                if pos != -1:
                    assert entry < pos, (name, marker, entry, pos)
            if name == 'approve_calendar_row_if_media_ready':
                lang = sql("select l.lanname from pg_proc p join pg_namespace n "
                           "on n.oid=p.pronamespace join pg_language l on l.oid=p.prolang "
                           "where n.nspname='public' and p.proname='approve_calendar_row_if_media_ready';")
                assert lang == 'plpgsql', lang
                assert entry < low.index('update public.content_calendar'), name
                assert 'PORTAL VISIBLE-CARD SNAPSHOT COMPARE' in body, name

        # Helper privilege: SECURITY DEFINER, and no runtime role may call it.
        assert sql("select p.prosecdef from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                   "where n.nspname='public' and p.proname='fixer_forward_calendar_entry_lock_20261008';") == 't'
        for role in ('anon', 'authenticated', 'service_role'):
            assert sql(f"select has_function_privilege('{role}', '{HELPER}', 'execute');") == 'f', role
        err = sql(f"set role service_role; select {HELPER};", ok=False)
        assert 'permission denied' in err, err

        # The helper itself enforces G-then-C: its body takes the shared graph
        # lock before the exclusive census lock.
        helper_body = sql("select prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                          "where n.nspname='public' and p.proname='fixer_forward_calendar_entry_lock_20261008';").lower()
        assert helper_body.index(f"pg_advisory_xact_lock_shared(hashtextextended('{GRAPH_KEY}'") \
            < helper_body.index(f"pg_advisory_xact_lock(hashtextextended('{CENSUS_KEY}'")

        # Runtime check: a real call holds BOTH entry locks (shared graph,
        # exclusive census) transaction-scoped, before its not_found exit.
        locks = sql(
            "begin; "
            "select public.content_calendar_swap_variant('gym', gen_random_uuid()); "
            "select mode from pg_locks where pid=pg_backend_pid() and locktype='advisory' "
            "and granted order by mode; "
            "rollback;").splitlines()
        assert json.loads(locks[0]) == {'ok': False, 'error': 'not_found'}, locks
        assert 'ExclusiveLock' in locks[1:] and 'ShareLock' in locks[1:], locks

        # Drift precondition: any production body change aborts the migration.
        sql("create or replace function public.content_calendar_swap_variant("
            "p_gym_id text, p_candidate_id uuid, p_actor text default null) returns jsonb "
            "language plpgsql security definer set search_path to 'public' as $f$ "
            "begin return jsonb_build_object('ok',false,'error','tampered'); end; $f$;")
        err = sql(MIGRATION.read_text(), ok=False)
        assert 'drifted from the frozen 2026-10-08 inventory' in err, err
        tampered = sql("select md5(prosrc) from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                       "where n.nspname='public' and p.proname='content_calendar_swap_variant';")
        assert tampered != inventory['content_calendar_swap_variant']
        # The abort rolled the whole migration back: the other five targets are
        # still the migrated versions from the successful first apply, and the
        # tampered function was never replaced.
        other = sql("select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                    "where n.nspname='public' and p.proname in ("
                    "'approve_calendar_row_if_media_ready','calendar_recover_unproved_approval',"
                    "'calendar_stamp_verified_approval','claim_calendar_gbp_publish_owned',"
                    "'claim_calendar_publish_slot_owned') "
                    "and position('fixer_forward_calendar_entry_lock_20261008' in p.prosrc) > 0;")
        assert other == '5', other
        assert 'tampered' in sql("select public.content_calendar_swap_variant('gym', gen_random_uuid());")

    print('PASS: forward lock entry calendar DRAFT (entry order, preserved contracts, helper privilege, calendar/media identity and owner drift guards)')


if __name__ == '__main__':
    main()
