"""Textual/structural proof of the publish/caption forward-lock-entry DRAFT.

Validates migrations/DRAFT_fixer_forward_lock_entry_publish_caption_20261008.sql
against the frozen production definitions in
evidence/portal-function-definitions-20261008.sql and the prosrc-md5 inventory
in evidence/portal-legacy-function-inventory-20261008.json:

  * helper prerequisite guard (fail closed, errcode 42883),
  * prosrc-md5 drift guard covering the three redefined functions AND the two
    inspected no-op wrappers (errcode 23514),
  * the entry-lock call is the first statement of each redefined body, before
    any advisory lock, FOR UPDATE / FOR SHARE row lock, table lock or write,
  * each redefinition equals the frozen production definition verbatim except
    for the inserted entry-lock lines (signatures, defaults, SECURITY DEFINER,
    search_path, return contract and business body all preserved),
  * the two wrappers are documented in the header but NOT redefined.

No database is required; this is a draft-file contract test.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "DRAFT_fixer_forward_lock_entry_publish_caption_20261008.sql"
FROZEN = ROOT.parent / "evidence" / "portal-function-definitions-20261008.sql"
INVENTORY = ROOT.parent / "evidence" / "portal-legacy-function-inventory-20261008.json"

REDEFINED = (
    "calendar_patch_caption_autonomous_clean",
    "calendar_patch_caption_manual_format",
    "claim_calendar_publish_slot",
)
WRAPPERS = (
    "claim_calendar_gbp_publish_with_mode_owned",
    "claim_calendar_publish_slot_proven_owned",
)
HELPER = "fixer_forward_calendar_entry_lock_20261008"
ENTRY_CALL = f"perform public.{HELPER}();"

# Lines inserted after the body's "begin" for each redefined function; kept in
# lockstep with the migration so the verbatim-reconstruction check is exact.
INSERTED = {
    "calendar_patch_caption_autonomous_clean": [
        "  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census",
        "  -- exclusive 'C', before the tenant advisory lock below.",
        f"  {ENTRY_CALL}",
    ],
    "calendar_patch_caption_manual_format": [
        "  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census",
        "  -- exclusive 'C', before the tenant advisory lock and the FOR SHARE row",
        "  -- locks below.",
        f"  {ENTRY_CALL}",
    ],
    "claim_calendar_publish_slot": [
        "  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census",
        "  -- exclusive 'C', before the tenant advisory lock and the FOR UPDATE row",
        "  -- lock below.",
        f"  {ENTRY_CALL}",
    ],
}

LOCK_TOKENS = (
    "pg_advisory_xact_lock",
    "pg_advisory_lock",
    "for update",
    "for share",
    "for no key update",
    "lock table",
)


def _migration_text():
    return MIGRATION.read_text()


def _frozen_block(name):
    """Exact frozen CREATE OR REPLACE ... ; block for one function."""
    text = FROZEN.read_text()
    marker = f"CREATE OR REPLACE FUNCTION public.{name}("
    start = text.index(marker)
    end = text.index("\n;", start)
    return text[start:end + 2]


def _expected_block(name):
    """Frozen block with ONLY the entry-lock lines inserted after 'begin'."""
    lines = _frozen_block(name).splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "begin":
            return "\n".join(lines[:index + 1] + INSERTED[name] + lines[index + 1:])
    raise AssertionError(f"{name}: no bare 'begin' line in the frozen body")


def test_draft_markers_and_transaction_wrapper():
    text = _migration_text()
    assert "DRAFT / UNAPPLIED / DEFAULT OFF" in text
    assert re.search(r"^begin;$", text, re.MULTILINE)
    assert re.search(r"^commit;$", text, re.MULTILINE)
    # Exactly the three intended redefinitions, in the frozen signature form.
    assert text.count("CREATE OR REPLACE FUNCTION public.") == len(REDEFINED)
    for name in REDEFINED:
        assert f"CREATE OR REPLACE FUNCTION public.{name}(" in text


def test_helper_prerequisite_guard_fails_closed():
    text = _migration_text()
    guard = text[text.index("do $$"):text.index("end $$;")]
    assert f"'{HELPER}'" in guard
    assert "pg_get_function_identity_arguments(p.oid) = ''" in guard
    assert "p.prorettype = 'void'::regtype" in guard
    assert "p.prosecdef" in guard
    assert "join pg_language l on l.oid = p.prolang" in guard
    assert "l.lanname = 'plpgsql'" in guard
    assert "'plpgsql'::regproc" not in text
    assert "42883" in guard
    # The prerequisite is checked before the drift loop (no replace can run
    # against a database that lacks the helper).
    assert guard.index(HELPER) < guard.index("for v_name, v_expected, v_expected_identity in")


def test_drift_guard_hashes_match_inventory():
    inventory = {row["proname"]: row["body_md5"]
                 for row in json.loads(INVENTORY.read_text())["rows"]}
    guard = _migration_text()
    guard = guard[guard.index("do $$"):guard.index("end $$;")]
    for name in REDEFINED + WRAPPERS:
        assert f"('{name}'" in guard, name
        assert inventory[name] in guard, name
    assert "md5(p.prosrc)" in guard
    assert "v_count is distinct from 1" in guard  # duplicate overload fails
    assert "23514" in guard


def test_redefinitions_are_frozen_plus_entry_lock_only():
    text = _migration_text()
    for name in REDEFINED:
        expected = _expected_block(name)
        assert expected in text, (
            f"{name}: migration block is not the frozen production definition "
            "plus exactly the entry-lock insertion")


def test_entry_lock_precedes_every_other_lock_or_write():
    text = _migration_text()
    for name in REDEFINED:
        marker = f"CREATE OR REPLACE FUNCTION public.{name}("
        start = text.index(marker)
        end = text.index("\n;", start)
        block = text[start:end].lower()
        # Drop the inserted comment lines so their prose ('FOR UPDATE' etc.)
        # is not mistaken for a real lock statement.
        for line in INSERTED[name][:-1]:
            block = block.replace(line.lower() + "\n", "", 1)
        entry = block.index(ENTRY_CALL)
        assert entry > block.index("begin"), name
        for token in LOCK_TOKENS:
            if token in block:
                assert entry < block.index(token), (
                    f"{name}: entry lock must precede '{token}'")
        # No write statement before the entry lock either.
        head = block[block.index("begin"):entry]
        for write in ("update ", "insert ", "delete ", "perform "):
            assert write not in head or ENTRY_CALL.startswith(write), (
                f"{name}: unexpected '{write}' before the entry lock")


def test_wrappers_documented_but_not_redefined():
    text = _migration_text()
    header = text[:text.index("begin;")]
    assert "WRAPPER NO-OP EVIDENCE" in header
    for name in WRAPPERS:
        assert name in header
        assert f"CREATE OR REPLACE FUNCTION public.{name}(" not in text
    # The header quotes the decisive prosrc reasoning: pure delegation, no
    # pre-delegation lock of any kind (inventory flags).
    assert "has_for_update=false" in header
    assert "has_advisory_lock=false" in header
    assert "select * into v_row from public.claim_calendar_gbp_publish_owned(" in header
    assert "v_token := public.claim_calendar_publish_slot_owned(" in header


def test_wrapper_noop_evidence_matches_frozen_source():
    """Independently confirm the wrappers take no lock before delegating, so
    the migration's no-op decision is grounded in the frozen source."""
    for name, callee in (
        ("claim_calendar_gbp_publish_with_mode_owned",
         "public.claim_calendar_gbp_publish_owned("),
        ("claim_calendar_publish_slot_proven_owned",
         "public.claim_calendar_publish_slot_owned("),
    ):
        block = _frozen_block(name).lower()
        body = block[block.index("begin"):]
        delegation = body.index(callee)
        before = body[:delegation]
        for token in LOCK_TOKENS + ("update public.", "insert into",
                                    "delete from"):
            assert token not in before, f"{name}: '{token}' before delegation"
    inventory = {row["proname"]: row
                 for row in json.loads(INVENTORY.read_text())["rows"]}
    for name in WRAPPERS:
        assert inventory[name]["has_for_update"] is False
        assert inventory[name]["has_advisory_lock"] is False


def test_helper_contract_documented_and_search_path_preserved():
    text = _migration_text()
    header = text[:text.index("begin;")]
    assert "pg_advisory_xact_lock_shared" in header  # G shared
    assert "pg_advisory_xact_lock" in header  # C exclusive
    assert "read committed" in header
    for name in REDEFINED:
        block = _frozen_block(name)
        assert "SECURITY DEFINER" in block
        assert "SET search_path TO 'public'" in block
        signature = block.splitlines()[0]
        assert signature in text


def test_exact_identity_owner_and_accepted_helper_guards():
    text = _migration_text()
    guard = text[text.index("do $$"):text.index("end $$;")]
    rows = json.loads((ROOT.parent / "evidence/portal-entry-function-identities-p2b-20261008.json").read_text())["rows"]
    for row in rows:
        if row["proname"] in REDEFINED + WRAPPERS:
            assert f"'{row['identity_args']}'" in guard
            assert row["body_md5"] in guard
    assert "current_user is distinct from 'postgres'" in guard
    assert "v_identity is distinct from v_expected_identity" in guard
    assert "v_owner is distinct from 'postgres'" in guard
    assert "pg_get_userbyid(p.proowner) = 'postgres'" in guard
    assert "p.proconfig = array['search_path=pg_catalog, public']::text[]" in guard
    assert "md5(p.prosrc) = 'f7804f90164613bff2e70d7f7bc5b4e2'" in guard
    assert "v_helper_valid is distinct from true" in guard

# Disposable PG17 behavior checks for BOTH sibling draft guards. These use
# existing local tools and socket-only clusters; no production DSN or installs.
import shutil
import subprocess
import tempfile
import random
from contextlib import ExitStack
import pytest


@pytest.mark.parametrize('tranche,target,original_argument,changed_argument,type_args,changed_type_args', [
    ('publish_caption', 'calendar_patch_caption_autonomous_clean',
     'p_clean_caption text', 'p_clean_caption varchar',
     'uuid,text,text,text,text', 'uuid,text,text,text,varchar'),
    ('lasso', 'stage_lasso_campaign_row',
     'p_artifact_tenant text', 'p_artifact_tenant varchar',
     'jsonb,text,text,text,text', 'jsonb,varchar,text,text,text'),
])
def test_pg17_guards_reject_identity_owner_and_helper_drift(
        tranche, target, original_argument, changed_argument, type_args, changed_type_args):
    if not all(shutil.which(tool) for tool in ('initdb', 'pg_ctl', 'psql')):
        pytest.skip('existing local PostgreSQL tools unavailable; no install attempted')
    helper_migration = ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql'
    if not helper_migration.exists():
        helper_migration = ROOT.parent / 'echo-b-entry-locks-kimi-20261008/migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql'
    helper_definition = re.search(
        r'create function public.fixer_forward_calendar_entry_lock_20261008\(\).*?\$\$;',
        helper_migration.read_text(), re.DOTALL).group(0)
    helper_signature = f'public.{HELPER}()'
    migration = (ROOT / f'migrations/DRAFT_fixer_forward_lock_entry_{tranche}_20261008.sql').read_text()
    frozen = FROZEN.read_text()
    target_definition = _frozen_block(target)
    rows = {row['proname']: row for row in json.loads(INVENTORY.read_text())['rows']}
    targets = list(REDEFINED + WRAPPERS) if tranche == 'publish_caption' else [
        'release_lasso_backlog_feed_hold', 'release_lasso_paired_story_hold',
        'repair_lasso_paired_story', 'stage_lasso_campaign_row',
        'stage_lasso_paired_story', 'stage_lasso_third_story']
    with tempfile.TemporaryDirectory(prefix='fixer_p2b_guard_pg_', dir='/tmp') as tmp, ExitStack() as cleanup:
        work = Path(tmp)
        socket_dir = work / 'sock'
        socket_dir.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run(['initdb', '-D', str(work / 'data'), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, text=True, timeout=60)
        subprocess.run(['pg_ctl', '-D', str(work / 'data'), '-l', str(work / 'pg.log'),
                        '-o', f"-k {socket_dir} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, text=True, timeout=60)
        cleanup.callback(subprocess.run,
                         ['pg_ctl', '-D', str(work / 'data'), '-m', 'fast', '-w', 'stop'],
                         check=False, capture_output=True, timeout=60)
        base = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(socket_dir),
                '-p', str(port), '-U', 'postgres', '-d', 'postgres']

        def sql(statement, error=None):
            result = subprocess.run(base, input=statement, text=True, capture_output=True, timeout=60)
            if error:
                assert result.returncode != 0, 'unexpected SQL success'
                assert error in result.stderr, result.stderr
            else:
                assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        sql('create role service_role; '
            'create table public.content_calendar(id uuid, gym_id text); '
            'create table public.media_source(id text); '
            'create table public.media_asset(id text, gym_id text); '
            'create table public.portal_action_receipt(id uuid); '
            'create table public.support_tickets(id uuid); '
            'set check_function_bodies=off;\n' + frozen)
        sql(migration, error='prerequisite helper')
        sql(helper_definition)
        # Successful full draft application validates actual pg_language and
        # proconfig checks. Wrapper bodies must remain the frozen definitions.
        sql('set check_function_bodies=off;\n' + migration)
        if tranche == 'publish_caption':
            for wrapper in WRAPPERS:
                assert sql(f"select md5(prosrc) from pg_proc where proname='{wrapper}';") == rows[wrapper]['body_md5']
        sql('set check_function_bodies=off;\n' + frozen)

        def assert_frozen_targets():
            for name in targets:
                assert sql(f"select md5(prosrc) from pg_proc where proname='{name}';") == rows[name]['body_md5']

        # Reproduce same-body signature drift: count and prosrc still match,
        # but replacement must abort without creating the original overload.
        sql(f'drop function public.{target}({type_args}); '
            'set check_function_bodies=off;\n' + target_definition.replace(original_argument, changed_argument))
        assert sql(f"select md5(prosrc) from pg_proc where proname='{target}';") == rows[target]['body_md5']
        sql(migration, error=f'production definition of {target} drifted')
        assert sql(f"select to_regprocedure('public.{target}({type_args})') is null;") == 't'
        assert sql(f"select count(*) from pg_proc where proname='{target}';") == '1'
        assert_frozen_targets()
        sql(f'drop function public.{target}({changed_type_args}); '
            'set check_function_bodies=off;\n' + target_definition)
        sql(f'alter function public.{target}({type_args}) owner to service_role;')
        sql(migration, error='owner service_role')
        assert_frozen_targets()
        sql(f'alter function public.{target}({type_args}) owner to postgres;')
        sql('set role service_role;\n' + migration, error='current_user must be postgres')
        assert_frozen_targets()

        # Helper mutations all fail before any business function replacement.
        for mutation in (
            f'alter function {helper_signature} owner to service_role;',
            f'alter function {helper_signature} security invoker;',
            f"alter function {helper_signature} set search_path=public;",
            f'create or replace function {helper_signature} returns void language plpgsql '
            'security definer set search_path=pg_catalog,public as $$ begin return; end; $$;',
            f'create function public.{HELPER}(integer) returns void language plpgsql '
            'security definer as $$ begin return; end; $$;',
        ):
            sql(mutation)
            sql(migration, error='prerequisite helper')
            assert_frozen_targets()
            sql(f'drop function if exists public.{HELPER}(integer); '
                f'drop function {helper_signature};\n' + helper_definition)
        sql('set check_function_bodies=off;\n' + migration)
