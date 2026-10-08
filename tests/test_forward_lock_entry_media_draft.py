"""Static checks for migrations/DRAFT_fixer_forward_lock_entry_media_20261008.sql.

Follows the existing migration-text test convention (see
tests/test_media_runway_migration.py): read the DRAFT migration and assert on
its normalized content. No live database is required.
"""

import re
from pathlib import Path

SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "DRAFT_fixer_forward_lock_entry_media_20261008.sql"
).read_text()

FUNCTIONS = {
    "record_gym_media_review": "626aedcebdd94728ead0475897246004",
    "claim_gym_media_sync": "f218677db408370afc141b3163767cb6",
    "request_gym_media_sync": "12b2084799cf8c5f9084096f62ef3f3b",
    "finish_gym_media_sync": "7b98042e5e057b97821789b62e6d89d2",
    "portal_action_receipt_begin": "8f77330dc8cb84a2f1e60d96544c4337",
    "portal_action_receipt_claim_selection": "d7e91f2feee4ae48b307272f55279c5e",
    "portal_action_receipt_apply": "832506e101c49f4c39ff7b016cefa4e3",
}

HELPER_CALL = "perform public.fixer_forward_calendar_entry_lock_20261008();"


def _function_sections() -> dict:
    """Map function name -> migration text of its CREATE OR REPLACE block."""
    sections = {}
    pattern = re.compile(
        r"CREATE OR REPLACE FUNCTION public\.(\w+)\(.*?\$function\$\n;",
        re.DOTALL,
    )
    for match in pattern.finditer(SQL):
        sections[match.group(1)] = match.group(0)
    return sections


def test_draft_header_marks_unapplied_and_documents_helper_dependency():
    normalized = " ".join(SQL.lower().split())
    assert "draft / unapplied / default off" in normalized
    # Child 0's calendar tranche defines the shared least-privilege helper;
    # this migration calls it and must document the apply-after ordering.
    assert "draft_fixer_forward_lock_entry_calendar_20261008.sql first" in normalized
    assert "public.fixer_forward_calendar_entry_lock_20261008() returns void" in normalized


def test_drift_guard_covers_every_function_with_inventory_hashes():
    assert "forward lock entry drift guard" in SQL
    assert "md5(" in SQL and "pg_proc" in SQL
    for name, md5 in FUNCTIONS.items():
        assert f'"{name}": "{md5}"' in SQL


def test_drift_guard_requires_child_zero_helper_first():
    normalized = " ".join(SQL.lower().split())
    assert "'fixer_forward_calendar_entry_lock_20261008'" in normalized
    assert "p.pronargs = 0" in normalized
    assert "p.prosecdef" in normalized
    assert "helper public.fixer_forward_calendar_entry_lock_20261008() missing" in normalized


def test_every_covered_function_is_replaced_once_with_helper_entry():
    sections = _function_sections()
    assert set(sections) == set(FUNCTIONS)
    for name, body in sections.items():
        assert SQL.count(f"CREATE OR REPLACE FUNCTION public.{name}(") == 1
        assert body.count(HELPER_CALL) == 1, name
        # The helper itself owns the G-then-C sequence; bodies must not take
        # either entry lock directly.
        assert "fixer_forward_graph_20261006" not in body.replace(HELPER_CALL, ""), name
        assert "fixer_forward_photo_census_20261007" not in body, name


def test_entry_call_precedes_any_other_lock_acquisition():
    lock_markers = re.compile(
        r"pg_advisory_xact_lock|for update|for share|for no key update"
        # UPDATE/INSERT/DELETE take row locks implicitly; the sync functions
        # have no explicit FOR UPDATE clause.
        r"|for key share|lock table|\bupdate\b|\binsert\b|\bdelete\b",
        re.IGNORECASE,
    )
    for name, body in _function_sections().items():
        # Strip line comments so prose like "the FOR UPDATE pick below" in the
        # frozen function comments is not mistaken for a lock acquisition.
        code = "\n".join(
            line.split("--", 1)[0] for line in body.splitlines()
        )
        entry_at = code.index(HELPER_CALL)
        others = [m.start() for m in lock_markers.finditer(code)]
        assert others, f"{name}: expected an existing lock after the entry"
        assert min(others) > entry_at, (
            f"{name}: an existing lock is acquired before the entry helper call"
        )


def test_signatures_security_and_return_contracts_preserved():
    normalized = " ".join(SQL.lower().split())
    expected_fragments = [
        "public.record_gym_media_review(p_gym_id text, p_asset_id text, p_expected_hash text, p_expected_status text, p_expected_reviewed_at timestamp with time zone, p_fields jsonb) returns boolean",
        "public.claim_gym_media_sync() returns setof media_source",
        "public.request_gym_media_sync(p_source_id text, p_gym_id text) returns boolean",
        "public.finish_gym_media_sync(p_source_id text, p_token text, p_ok boolean, p_error text default null::text) returns boolean",
        "public.portal_action_receipt_begin(p_gym_id text, p_action_id text, p_action text, p_row_id uuid, p_actor_id text, p_request_fingerprint text) returns portal_action_receipt",
        "public.portal_action_receipt_claim_selection(p_gym_id text, p_action_id text, p_request_fingerprint text, p_selected_asset jsonb, p_planned_siblings jsonb) returns portal_action_receipt",
        "public.portal_action_receipt_apply(p_gym_id text, p_action_id text, p_request_fingerprint text, p_prepared jsonb) returns portal_action_receipt",
    ]
    for fragment in expected_fragments:
        assert fragment in normalized, fragment
    # Every covered function keeps SECURITY DEFINER and the pinned search_path.
    for body in _function_sections().values():
        lowered = " ".join(body.lower().split())
        assert "security definer" in lowered
        assert "set search_path to 'public'" in lowered


def test_guard_pins_exact_frozen_argument_identity_and_postgres_owner():
    guard = SQL.split("do $guard$", 1)[1].split("$guard$;", 1)[0]
    normalized = " ".join(guard.lower().split())
    assert "current_user is distinct from 'postgres'" in normalized
    assert "pg_get_function_identity_arguments(p.oid)" in guard
    assert "v_identity is distinct from v_expected_identity->>v_name" in guard
    assert "v_owner is distinct from 'postgres'" in guard
    assert "pg_get_userbyid(p.proowner) = 'postgres'" in guard
    # Pin every target to its CREATE identity, excluding DEFAULT clauses.
    # This prevents a same-body varchar overload from satisfying the guard.
    for name, section in _function_sections().items():
        arguments = section.split("(", 1)[1].split(")", 1)[0]
        identity = re.sub(r" DEFAULT [^,)]*", "", arguments)
        assert f'"{name}": "{identity}"' in guard, name
    assert "p_expected_reviewed_at timestamp with time zone" in guard
    assert "v_count is distinct from 1" in guard
