"""Structural tests for the LASSO forward-lock-entry DRAFT migration.

Text-only checks (no database): the six LASSO staging/repair/hold-release
functions must call the shared entry-lock helper FIRST -- before any advisory,
row, or table lock in their frozen bodies -- with signatures, defaults,
SECURITY DEFINER, search_path and business logic preserved verbatim, plus
fail-closed prosrc-md5 drift guards and a helper prerequisite guard.
"""
import re
from pathlib import Path

SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "DRAFT_fixer_forward_lock_entry_lasso_20261008.sql"
).read_text()

HELPER = "public.fixer_forward_calendar_entry_lock_20261008()"
ENTRY_CALL = f"perform {HELPER};"

# Frozen 2026-10-08 inventory (evidence/portal-legacy-function-inventory-20261008.json).
FROZEN_MD5 = {
    "release_lasso_backlog_feed_hold": "064c5ab91745ed8fca62eccc5d98937c",
    "release_lasso_paired_story_hold": "3e8cc9923f8ba4c715bf2d8468c97e86",
    "repair_lasso_paired_story": "016c1f1d7f72f73b510b4e48c43eea08",
    "stage_lasso_campaign_row": "2c0dadddcffee17506675c0bdb11b59f",
    "stage_lasso_paired_story": "c151abbe81bdca29e9dee9ddf22a6089",
    "stage_lasso_third_story": "37872cec68ee59dda43344a0442855d2",
}

# Frozen signature headers (evidence/portal-function-definitions-20261008.sql).
FROZEN_SIGNATURES = {
    "release_lasso_backlog_feed_hold": "(p_feed_id uuid, p_story_id uuid, p_expected_feed jsonb, p_expected_story jsonb)",
    "release_lasso_paired_story_hold": "(p_story_id uuid)",
    "repair_lasso_paired_story": "(p_story_id uuid, p_feed_id uuid, p_expected_story jsonb, p_expected_feed jsonb, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone)",
    "stage_lasso_campaign_row": "(p_row jsonb, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_image_sha256 text)",
    "stage_lasso_paired_story": "(p_feed_id uuid, p_account text, p_day date, p_slot integer, p_feed_status text, p_feed_caption text, p_feed_image_url text, p_feed_scheduled_at timestamp with time zone, p_feed_logical_post_id uuid, p_story_id uuid, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone)",
    "stage_lasso_third_story": "(p_feed_id uuid, p_feed_caption text, p_feed_image_url text, p_story_id uuid, p_story_image_url text, p_story_source_url text, p_story_sha256 text, p_source_hash text, p_policy_version text, p_scheduled_at timestamp with time zone, p_caption_hash text DEFAULT NULL::text)",
}


def _function_block(name):
    start = SQL.index(f"CREATE OR REPLACE FUNCTION public.{name}(")
    end = SQL.index("$function$\n;", start) + len("$function$\n;")
    return SQL[start:end]


def test_migration_is_an_explicitly_unapplied_draft_in_one_transaction():
    assert SQL.startswith("-- DRAFT / UNAPPLIED / DEFAULT OFF.")
    assert "\nbegin;" in SQL
    assert SQL.rstrip().endswith("commit;")
    # The LASSO tranche consumes the calendar tranche's helper; it never
    # creates, replaces, grants or revokes it (statements only, not comments).
    statements = "\n".join(
        line for line in SQL.lower().splitlines() if not line.lstrip().startswith("--")
    )
    normalized = " ".join(statements.split())
    assert "create function public.fixer_forward_calendar_entry_lock" not in normalized
    assert "create or replace function public.fixer_forward_calendar_entry_lock" not in normalized
    assert "grant " not in normalized
    assert "revoke " not in normalized


def test_helper_prerequisite_guard_fails_closed():
    normalized = " ".join(SQL.lower().split())
    assert "prerequisite guard" in normalized
    assert "'fixer_forward_calendar_entry_lock_20261008'" in normalized
    assert "p.prosecdef" in SQL
    assert "missing or has the wrong shape" in normalized
    assert "errcode = '23514'" in normalized
    # Prerequisite guard runs before the drift guard and any replace.
    assert SQL.index("PREREQUISITE GUARD") < SQL.index("PRECONDITION GUARD")
    assert SQL.index("PREREQUISITE GUARD") < SQL.index("CREATE OR REPLACE FUNCTION")


def test_drift_guard_pins_every_frozen_prosrc_md5():
    guard = SQL[SQL.index("PRECONDITION GUARD"):SQL.index("CREATE OR REPLACE FUNCTION")]
    for name, digest in FROZEN_MD5.items():
        assert f"('{name}'" in guard
        assert digest in guard
    assert "md5(p.prosrc)" in guard
    assert "v_count is distinct from 1" in guard
    assert "v_actual is distinct from v_expected" in guard
    assert "errcode = '23514'" in guard


def test_each_function_signature_is_preserved_verbatim():
    for name, signature in FROZEN_SIGNATURES.items():
        block = _function_block(name)
        assert block.startswith(f"CREATE OR REPLACE FUNCTION public.{name}{signature}\n")
        assert " SECURITY DEFINER\n" in block
        assert " SET search_path TO 'public'\n" in block
        assert " LANGUAGE plpgsql\n" in block
        assert " RETURNS jsonb\n" in block


def test_entry_lock_call_precedes_every_other_lock_in_each_body():
    lock_patterns = ("pg_advisory_xact_lock", "for update", "lock table")
    for name in FROZEN_SIGNATURES:
        block = _function_block(name)
        # Exactly one entry-lock call, at the very top of the body: the first
        # statement after the outer begin.
        assert block.count(ENTRY_CALL) == 1, name
        body_start = block.index("\nbegin\n") + len("\nbegin\n")
        call_pos = block.index(ENTRY_CALL)
        between = block[body_start:call_pos]
        assert between.strip().startswith("-- FORWARD LOCK ENTRY"), name
        for pattern in lock_patterns:
            for match in re.finditer(pattern, block.lower()):
                assert match.start() > call_pos, (name, pattern)


def test_entry_call_uses_shared_helper_with_no_extra_advisory_keys():
    # G/C keys are taken only inside the shared helper (calendar tranche);
    # this migration adds no new advisory key of its own.
    assert "fixer_forward_graph_20261006" not in SQL.split("CREATE OR REPLACE FUNCTION")[1]
    for name in FROZEN_SIGNATURES:
        block = _function_block(name)
        assert "pg_advisory_xact_lock_shared" not in block
        assert "fixer_forward_photo_census_20261007" not in block
        assert "fixer_forward_graph_20261006" not in block


def test_lasso_business_logic_and_locks_are_unchanged():
    # Spot-check the frozen lock sites and LASSO behavior contracts survive.
    backlog = _function_block("release_lasso_backlog_feed_hold")
    assert "perform pg_advisory_xact_lock(hashtextextended(\n    'lasso|' || v_account || '|' || v_day::text, 0));" in backlog
    assert "'prepared_backlog_waiting_for_story_and_capacity'" in backlog
    assert "raise exception 'LASSO backlog feed hold CAS lost';" in backlog

    release = _function_block("release_lasso_paired_story_hold")
    assert "media_not_ready_reason = 'paired_feed_not_ready'" in release
    assert release.count("pg_advisory_xact_lock") == 1

    repair = _function_block("repair_lasso_paired_story")
    assert "p_artifact_tenant not in ('lasso', 'lasso_ig', 'lasso_fb')" in repair
    assert "insert into public.lasso_managed_paired_stories(story_id, feed_id)" in repair

    campaign = _function_block("stage_lasso_campaign_row")
    assert "lock table public.content_calendar in share row exclusive mode;" in campaign
    assert "when unique_violation then" in campaign

    paired = _function_block("stage_lasso_paired_story")
    assert "'content_calendar:' || p_feed_id::text || ':paired_story'" in paired
    assert "p_story_scheduled_at <> p_feed_scheduled_at + interval '15 minutes'" in paired

    third = _function_block("stage_lasso_third_story")
    assert "perform pg_advisory_xact_lock(hashtextextended('lasso|instagram|' || v_day::text, 0));" in third
    assert "lower(btrim(coalesce(v_feed.pillar, ''))) <> 'summit'" in third


def test_exact_identity_owner_and_accepted_helper_guards():
    guard = SQL[:SQL.index("CREATE OR REPLACE FUNCTION")]
    for name, signature in FROZEN_SIGNATURES.items():
        identity = re.sub(r" DEFAULT [^,)]*", "", signature[1:-1])
        assert f"'{identity}'" in guard, name
    assert "current_user is distinct from 'postgres'" in guard
    assert "v_identity is distinct from v_expected_identity" in guard
    assert "v_owner is distinct from 'postgres'" in guard
    assert "pg_get_userbyid(p.proowner) = 'postgres'" in guard
    assert "pg_get_function_identity_arguments(p.oid) = ''" in guard
    assert "p.prorettype = 'void'::regtype" in guard
    assert "join pg_language l on l.oid = p.prolang" in guard
    assert "l.lanname = 'plpgsql'" in guard
    assert "p.proconfig = array['search_path=pg_catalog, public']::text[]" in guard
    assert "md5(p.prosrc) = 'f7804f90164613bff2e70d7f7bc5b4e2'" in guard
    assert "v_count is distinct from 1 or v_helper_valid is distinct from true" in guard


def test_business_bodies_match_frozen_definitions_with_only_entry_insertion():
    frozen = (Path(__file__).resolve().parent / "fixtures/forward_lock_entry/portal-function-definitions-20261008.sql").read_text()
    for name in FROZEN_SIGNATURES:
        original = re.search(rf"CREATE OR REPLACE FUNCTION public\.{name}\(.*?\$function\$\n;", frozen, re.DOTALL).group(0)
        actual = _function_block(name)
        actual = re.sub(r"  -- FORWARD LOCK ENTRY .*?  perform public\.fixer_forward_calendar_entry_lock_20261008\(\);\n", "", actual, count=1, flags=re.DOTALL)
        assert actual == original, name
