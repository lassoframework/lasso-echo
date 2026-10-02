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
