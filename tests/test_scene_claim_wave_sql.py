"""scene_claim_wave: static SQL-text contracts for the claim-wave draft.

Follows the style of tests/test_visual_group_global_sql.py: plain text
inspection of the UNAPPLIED draft migrations. No database is touched.

`migrations/DRAFT_visual_scene_claim_wave_20261003.sql` is authored by the
SQL subagent in the same wave; these tests pin its REQUIRED contract after
the 2026-10-04 P0 repair pass (Sol/Astra SCENE_AUDIT_GAPS.md) and skip
cleanly if the file is ever absent. Contracts against SQL that predates
this wave (the frozen exact-byte ledger and the rejected sketch) always run.

Pinned contract highlights:
  * candidates carry object_role ('display'|'poster') bound to the row's
    exact delivered object via exact_url+fingerprint attestation (P0-1);
  * claim functions take the content_calendar ROW composite;
  * visual_scene_claim_scan writes NOTHING; visual_scene_claim_decide /
    the BEFORE trigger are the only hold writers, idempotent via the
    partial unique index visual_scene_review_hold_open_uq (P0-3);
  * the committed held-row contract: the trigger NEVER raises on conflict —
    it mutates NEW (variant_status='archived', status='pending',
    media_not_ready_reason='scene_review_hold', publish_claim_token
    cleared), syncs, and RETURNS NEW so the held row commits with its holds;
  * ALL claim-path EXECUTE is revoked from service_role (P0-2); only the
    safe read/review surfaces are granted;
  * occupancy is PERMANENT — never freed on release, denial or swap (P1-3);
  * publish/approval guarded wrappers read PERSISTED state (P0-5);
  * hold_resolve / hold_reactivate are validated, one-shot, re-evaluating
    (P0-7); backfill stays a 0A000 stub.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MIGRATIONS = ROOT / "migrations"
WAVE = "DRAFT_visual_scene_claim_wave_20261003.sql"


def _sql(name):
    return (MIGRATIONS / name).read_text().lower()


@pytest.fixture(scope="module")
def wave():
    if not (MIGRATIONS / WAVE).exists():
        pytest.skip(f"{WAVE} not present (SQL subagent owns it); "
                    "contract recorded for when it lands")
    return _sql(WAVE)


def _fn_body(wave, signature):
    return wave.split(
        f"create or replace function public.{signature}", 1
    )[1].split("end;\n$$;", 1)[0]


@pytest.fixture(scope="module")
def wave_scan(wave):
    return _fn_body(wave, "visual_scene_claim_scan(")


@pytest.fixture(scope="module")
def wave_decide(wave):
    return _fn_body(wave, "visual_scene_claim_decide(")


@pytest.fixture(scope="module")
def wave_guard(wave):
    return _fn_body(wave, "visual_scene_claim_guard(")


@pytest.fixture(scope="module")
def wave_trigger(wave):
    return _fn_body(wave, "visual_scene_calendar_claim_guard()")


# ---- contracts against SQL that predates this wave (always run) --------------

def test_exact_byte_migration_contract_is_untouched():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    assert "fingerprint ~ '^md5:[0-9a-f]{32}$'" in sql
    assert "create or replace function public.visual_global_claim_scene(" in sql
    assert "create or replace function public.visual_global_claim_fingerprint_set(" in sql
    assert "visual byte fingerprint already used by another client or date" in sql
    claim = sql.split(
        "create or replace function public.visual_global_claim_fingerprint_set(", 1
    )[1].split("end;\n$$;", 1)[0]
    assert "phash" not in claim


def test_rejected_prep_time_record_functions_still_raise():
    sql = _sql("DRAFT_visual_scene_phash_20261003.sql")
    assert "errcode='0a000'" in sql
    assert "rejected prep-time write path" in sql


# ---- wave file status, ordering and rollback ---------------------------------

def test_wave_is_draft_unapplied_off(wave):
    assert "draft / unapplied / off" in wave
    assert "do not apply" in wave
    assert "scene_guard_operational stays false" in wave
    assert "agent_visual_scene_guard stays off" in wave
    assert "activation blockers" in wave


def test_wave_migration_order_is_exact_and_after_the_exact_byte_ledger(wave):
    header = wave.split("begin;", 1)[0]
    assert "migration order (exact)" in header
    history = header.index("draft_visual_global_history_20261002.sql")
    sketch = header.index("draft_visual_scene_phash_20261003.sql")
    this_file = header.index("draft_visual_scene_claim_wave_20261003.sql")
    activation = header.index("must remain last")
    assert history < sketch < this_file < activation


def test_wave_rollback_drops_only_its_own_objects(wave):
    header = wave.split("begin;", 1)[0]
    assert header.index("rollback") < header.index("what this file implements")
    assert "drop trigger if exists content_calendar_scene_wave_claim_guard" in header
    for obj in ("visual_scene_candidate", "visual_scene_phash_occupied",
                "visual_scene_review_hold", "visual_scene_calendar_claim_guard",
                "visual_scene_claim_scan", "visual_scene_claim_decide",
                "visual_scene_claim_guard", "visual_scene_row_candidate",
                "visual_scene_write_holds", "visual_scene_hold_resolve",
                "visual_scene_hold_reactivate",
                "visual_scene_publish_claim_guarded",
                "visual_scene_approval_guarded",
                "visual_scene_hamming", "visual_scene_register_candidate",
                "visual_scene_backfill_occupied"):
        assert f"drop table if exists public.{obj}" in header or \
               f"drop function if exists public.{obj}" in header
    assert "not touched by this file and needs no\n-- rollback" in header


# ---- (a) candidate staging binds to the row's exact delivered object ---------

def test_wave_candidate_carries_object_role_bound_to_attestation(wave):
    staging = wave.split(
        "create table if not exists public.visual_scene_candidate", 1)[1].split(");", 1)[0]
    assert "object_role  text        not null default 'display' " \
        "check (object_role in ('display','poster'))" in staging
    assert "phash        char(16)" in staging
    assert "visual_global_object_attestation" in staging
    assert "used_date" not in staging
    assert "a candidate never counts as use" in wave
    assert "visual_scene_candidate_object_idx" in wave


def test_wave_candidate_registration_validates_role_and_attestation(wave):
    reg = _fn_body(wave, "visual_scene_register_candidate(")
    assert "p_object_role text default 'display'" in reg
    assert "p_object_role not in ('display','poster')" in reg
    assert "candidate needs canonical tenant, group, object role, phash, attested bytes and actor" in reg
    assert "p_evidence->>'verified_bytes' is distinct from p_fingerprint" in reg
    assert "candidate phash is not backed by owner-attested exact bytes" in reg
    assert "visual_scene_phash_occupied" not in reg
    assert "visual_global_usage" not in reg


def test_wave_row_candidate_binds_display_and_poster_objects(wave):
    row_cand = _fn_body(wave, "visual_scene_row_candidate(")
    assert "p_row public.content_calendar" in row_cand
    assert "c.object_role = 'display'" in row_cand
    assert "c.exact_url = p_row.image_url" in row_cand
    assert "c.object_role = 'poster'" in row_cand
    assert "c.exact_url = p_row.thumbnail_url" in row_cand
    assert "p_row.thumbnail_url is distinct from p_row.image_url" in row_cand
    # ambiguous staged evidence fails closed
    assert "conflicting staged scene candidates for one delivered object" in row_cand
    # binding resolution writes nothing
    assert "insert" not in row_cand and "update public." not in row_cand


# ---- (b)+(c)+(d) claim path: row-composite scan core writes nothing ----------

def test_wave_claim_functions_take_the_calendar_row_composite(wave):
    for fn in ("visual_scene_claim_scan", "visual_scene_claim_decide",
               "visual_scene_claim_guard"):
        assert f"create or replace function public.{fn}(\n  p_row public.content_calendar, p_candidate_id uuid" in wave
    assert "create or replace function public.visual_scene_row_candidate(\n  p_row public.content_calendar" in wave


def test_wave_scan_core_writes_nothing(wave_scan):
    assert "insert" not in wave_scan
    assert "update public." not in wave_scan
    assert "delete from" not in wave_scan


def test_wave_scan_serializes_with_the_fleet_advisory_lock(wave, wave_scan):
    assert "pg_try_advisory_xact_lock" in wave_scan
    assert "visual_scene_global" in wave_scan
    assert wave_scan.index("pg_try_advisory_xact_lock") < \
        wave_scan.index("from public.visual_scene_phash_occupied o")
    assert "fleet lock order" in wave


def test_wave_unbound_candidate_is_fail_closed_blocked_without_holds(wave_scan, wave_decide):
    assert "o_worst_band := 'fail_closed'" in wave_scan
    assert wave_scan.index("o_worst_band := 'fail_closed'") < \
        wave_scan.index("from public.visual_scene_phash_occupied o")
    fc = wave_decide.index("'fail_closed_no_bound_candidate'")
    holds_call = wave_decide.index("visual_scene_write_holds(")
    assert fc < holds_call
    assert "'matches', '[]'::jsonb" in wave_decide[:holds_call]
    assert "'hold_ids', '[]'::jsonb" in wave_decide[:holds_call]


def test_wave_hamming_server_side_full_table_scan_no_prefilter(wave, wave_scan):
    assert "language plpgsql immutable" in wave.split(
        "create or replace function public.visual_scene_hamming(", 1)[1].split("$$;", 1)[0]
    assert "visual_scene_hamming" in wave_scan
    assert "from public.visual_scene_phash_occupied o" in wave_scan
    assert "no exact-match" in wave


def test_wave_band_boundaries_match_the_policy(wave, wave_scan):
    assert "hamming <= 6" in wave
    assert "hamming 7..30" in wave
    assert "hamming > 30" in wave
    assert "v_match.dist <= 6" in wave_scan
    assert "v_match.dist is null or v_match.dist > 30" in wave_scan


def test_wave_same_date_same_group_siblings_stay_legal(wave, wave_scan):
    assert "same-tenant same-date same-group" in wave
    skip = wave_scan.index(
        "if v_match.tenant_id = o_tenant and v_match.used_date = p_row.post_date")
    assert "v_match.group_key = p_row.visual_group_key" in wave_scan[skip:skip + 200]
    assert wave_scan.index("v_match.group_key = p_row.visual_group_key") < \
        wave_scan.index("o_detail := o_detail ||")


def test_wave_approved_hold_exempts_only_the_exact_reviewed_pair(wave, wave_scan):
    exempt = wave_scan.index("h.state = 'approved'")
    scope = wave_scan[exempt:exempt + 700]
    for col in ("h.claim_date = p_row.post_date",
                "h.candidate_phash = v_candidate.phash",
                "h.matched_phash = v_match.phash",
                "h.matched_tenant_id = v_match.tenant_id",
                "h.matched_group_key = v_match.group_key",
                "h.matched_used_date = v_match.used_date"):
        assert col in scope
    # the exemption is a scan-level continue, not a hold delete
    assert "exempts only" in wave
    assert "later or different\n    -- conflicts still hold" in wave or \
        "later or different conflicts still hold" in wave


def test_wave_occupied_written_only_on_claimed_paths(wave, wave_decide, wave_guard, wave_trigger):
    writers = [part.split("(", 1)[0] for part in
               wave.split("create or replace function public.")[1:]
               if "insert into public.visual_scene_phash_occupied" in part]
    assert sorted(writers) == ["visual_scene_calendar_claim_guard",
                               "visual_scene_claim_decide",
                               "visual_scene_claim_guard"]
    # decide: both blocked returns precede the occupied insert
    assert wave_decide.index("return jsonb_build_object('decision', 'blocked'") < \
        wave_decide.index("insert into public.visual_scene_phash_occupied")
    assert "raise exception" not in wave_decide
    # guard: only the clean-path occupied write, no holds at all
    assert "visual_scene_review_hold" not in wave_guard
    assert wave_guard.count("insert into") == 1
    for msg in ("scene near-frame conflict blocks the visual claim",
                "uncertain scene match requires review; visual claim held",
                "no scene candidate bound to this row"):
        assert msg in wave_guard
    assert "detail = v_scan.o_detail::text" in wave_guard
    # trigger: occupancy only after the clean-path sync
    clean = wave_trigger.index("perform public.visual_group_sync_row(",
                               wave_trigger.index("return new;"))
    assert clean < wave_trigger.index("insert into public.visual_scene_phash_occupied")


def test_wave_occupancy_is_permanent_never_freed(wave):
    assert "delete from public.visual_scene_phash_occupied" not in wave
    assert "update public.visual_scene_phash_occupied" not in wave
    assert "never freed on release, denial or swap" in wave or \
        "never freed on release" in wave
    assert "before update or delete on public.visual_scene_phash_occupied" in wave


# ---- (c) idempotent holds + committed held-row contract ----------------------

def test_wave_open_hold_uniqueness_index_is_stable(wave):
    assert "create unique index if not exists visual_scene_review_hold_open_uq" in wave
    idx = wave.split("create unique index if not exists visual_scene_review_hold_open_uq", 1)[1]
    assert "(tenant_id, group_key, claim_date, calendar_row_id, candidate_phash, matched_phash)" in idx
    assert "where state = 'open'" in idx


def test_wave_write_holds_is_idempotent_and_internal(wave):
    writer = _fn_body(wave, "visual_scene_write_holds(")
    assert "insert into public.visual_scene_review_hold" in writer
    assert "on conflict (tenant_id, group_key, claim_date, calendar_row_id,\n                   candidate_phash, matched_phash) where state = 'open'\n      do nothing" in writer
    hold_writers = [part.split("(", 1)[0] for part in
                    wave.split("create or replace function public.")[1:]
                    if "insert into public.visual_scene_review_hold" in part]
    assert hold_writers == ["visual_scene_write_holds"]
    # decide and the trigger both route holds through the shared writer
    decide = _fn_body(wave, "visual_scene_claim_decide(")
    trigger = _fn_body(wave, "visual_scene_calendar_claim_guard()")
    assert "visual_scene_write_holds(" in decide
    assert "visual_scene_write_holds(" in trigger


def test_wave_hold_table_is_reviewable_and_conflict_scoped(wave):
    hold = wave.split(
        "create table if not exists public.visual_scene_review_hold", 1)[1].split(");", 1)[0]
    assert "hold_kind        text        not null check (hold_kind in ('near_frame','uncertain'))" in hold
    assert "state            text        not null default 'open'" in hold
    assert "matched_tenant_id text       not null" in hold
    assert "matched_group_key text       not null" in hold
    assert "matched_used_date date       not null" in hold
    assert "only a one-time open->terminal resolution may change a scene hold" in wave


def test_wave_trigger_installs_committed_held_row_contract(wave, wave_trigger):
    assert "create trigger content_calendar_scene_wave_claim_guard before insert or update\n  on public.content_calendar" in wave
    # conflict path: holds first, then held-state mutation, sync, RETURN NEW
    conflict = wave_trigger.index(
        "if v_scan.o_worst_band is not null and v_scan.o_worst_band <> 'fail_closed'")
    held = wave_trigger[conflict:]
    end = held.index("return new;")
    segment = held[:end]
    assert "visual_scene_write_holds(" in segment
    assert "new.variant_status := 'archived';" in segment
    assert "new.status := 'pending';" in segment
    assert "new.media_not_ready_reason := 'scene_review_hold';" in segment
    assert "new.publish_claim_token := null;" in segment
    assert "perform public.visual_group_sync_row(" in segment
    # the conflict path NEVER raises — raising would erase the holds
    assert "raise exception" not in segment
    assert segment.index("visual_scene_write_holds(") < \
        segment.index("new.variant_status := 'archived';")
    # fail-closed binding (candidate vanished) is the only claim-outcome raise
    assert "no scene candidate bound to this row''s delivered object" in wave_trigger
    # engages only on armed tenants with active, keyed, unsent rows and
    # scenes that actually have staged candidates (inert otherwise)
    assert "visual_group_enforcement_on(new.gym_id)" in wave_trigger
    assert "visual_group_row_active(new)" in wave_trigger
    assert "new.visual_group_key is null or new.post_date is null" in wave_trigger
    # decision-first: scene scan before any local/exact-byte claim
    assert wave_trigger.index("visual_scene_claim_scan(new") < \
        wave_trigger.index("perform public.visual_group_sync_row(")
    # fleet lock order: component locks before the scan's advisory lock
    assert wave_trigger.index("visual_group_lock_scene_components") < \
        wave_trigger.index("visual_scene_claim_scan(new")


def test_wave_header_states_realized_hold_rollback_semantics(wave):
    header = wave.split("begin;", 1)[0]
    assert "hold-rollback: enforced semantics (realized)" in header
    assert "a hold row exists if and only if the claim transaction commits in a\n-- blocked/held state" in header
    assert "commits the held\n-- row with its holds" in header or \
        "commit the held row" in header
    assert "dblink" in header and "rejected" in header
    assert "an integration\n    -- that raises gets no hold" in header or \
        "gets no hold, by construction" in header


# ---- (P0-2) EXECUTE revocation: claim path is trigger/internal only ----------

def test_wave_claim_path_execute_revoked_from_service_role(wave):
    for fn in ("public.visual_scene_claim_scan(public.content_calendar, uuid)",
               "public.visual_scene_claim_decide(public.content_calendar, uuid)",
               "public.visual_scene_claim_guard(public.content_calendar, uuid)",
               "public.visual_scene_row_candidate(public.content_calendar)",
               "public.visual_scene_calendar_claim_guard()",
               "public.visual_scene_backfill_occupied()"):
        assert f"revoke all on function {fn}\n  from public, anon, authenticated, service_role" in wave
    assert "revoke all on function public.visual_scene_write_holds(text, text, date, uuid, text, uuid, char(16), text, text, jsonb)\n  from public, anon, authenticated, service_role" in wave
    for fn in ("visual_scene_claim_scan", "visual_scene_claim_decide",
               "visual_scene_claim_guard", "visual_scene_row_candidate",
               "visual_scene_write_holds", "visual_scene_calendar_claim_guard",
               "visual_scene_backfill_occupied"):
        assert f"grant execute on function public.{fn}" not in wave


def test_wave_safe_review_surfaces_are_service_role_only(wave):
    for fn in ("public.visual_scene_hamming(text, text)",
               "public.visual_scene_register_candidate(text, text, text, text, text, jsonb, text, text)",
               "public.visual_scene_hold_resolve(uuid, text, text, jsonb)",
               "public.visual_scene_hold_reactivate(uuid, text)",
               "public.visual_scene_publish_claim_guarded(uuid)",
               "public.visual_scene_approval_guarded(uuid)"):
        assert f"revoke all on function {fn}\n  from public, anon, authenticated" in wave
        assert f"grant execute on function {fn}" in wave + "\n" and \
            f"grant execute on function {fn}" in wave
    for table in ("visual_scene_candidate", "visual_scene_phash_occupied",
                  "visual_scene_review_hold"):
        assert f"alter table public.{table} enable row level security" in wave
    assert "grant select on public.visual_scene_candidate, public.visual_scene_phash_occupied" in wave


# ---- (P0-5) guarded wrappers read persisted state ----------------------------

def test_wave_publish_claim_guarded_returns_null_for_persisted_held(wave):
    pub = _fn_body(wave, "visual_scene_publish_claim_guarded(")
    assert "select * into v_row from public.content_calendar where id = p_row_id" in pub
    assert "v_row.status = 'pending' and v_row.variant_status = 'archived'" in pub
    assert "v_row.media_not_ready_reason = 'scene_review_hold'" in pub
    assert pub.index("return null") < pub.index("errcode='0a000'")
    assert "calendar row not found" in pub


def test_wave_approval_guarded_excludes_held_rows(wave):
    appr = _fn_body(wave, "visual_scene_approval_guarded(")
    assert "select * into v_row from public.content_calendar where id = p_row_id" in appr
    assert "v_row.media_not_ready_reason is not null" in appr
    assert "v_row.status = 'pending' and v_row.variant_status = 'archived'" in appr
    assert "insert" not in appr and "update public." not in appr


# ---- (P0-7) hold review path -------------------------------------------------

def test_wave_hold_resolve_validates_actor_evidence_and_oneshot(wave):
    res = _fn_body(wave, "visual_scene_hold_resolve(")
    assert "p_decision not in ('approved','rejected')" in res
    assert "nullif(btrim(p_actor),'') is null" in res
    assert "p_evidence = '{}'::jsonb" in res
    assert "hold resolution needs decision approved|rejected, a named actor and evidence" in res
    assert "scene hold is already resolved" in res
    # current-scene re-evaluation before any approval takes effect
    assert "reviewed scene candidate changed; hold conflict scope drifted" in res
    assert "reviewed occupied scene no longer present; hold conflict scope drifted" in res
    assert "reviewed conflict distance drifted; refusing resolution" in res
    assert "pg_try_advisory_xact_lock" in res
    # approved resolutions return the exact exemption scope
    assert "'exemption_scope'" in res
    assert res.index("return jsonb_build_object('hold_id'") > \
        res.index("update public.visual_scene_review_hold")


def test_wave_hold_reactivate_requires_approval_and_clean_rescan(wave):
    re = _fn_body(wave, "visual_scene_hold_reactivate(")
    assert "hold reactivation needs a named actor" in re
    assert "only an approved scene hold can reactivate its calendar row" in re
    assert "calendar row is not in scene-review-held state" in re
    # fresh bound-candidate re-scan; refusal mutates nothing
    assert "visual_scene_row_candidate(v_row)" in re
    assert "visual_scene_claim_scan(v_row, v_candidate_id)" in re
    assert "'still_blocked'" in re
    assert re.index("visual_scene_claim_scan(v_row") < \
        re.index("update public.content_calendar")
    assert "set variant_status = 'active', status = 'pending',\n        media_not_ready_reason = null" in re


# ---- (e) backfill stays a fail-closed stub -----------------------------------

def test_wave_backfill_is_a_stub_that_fails_closed(wave):
    stub = wave.split(
        "create or replace function public.visual_scene_backfill_occupied()", 1
    )[1].split("$$;", 1)[0]
    assert "errcode='0a000'" in stub
    assert "before activation" in stub
    assert "insert" not in stub


# ---- safety rails ------------------------------------------------------------

def test_wave_does_not_mutate_the_exact_byte_ledger(wave):
    assert "similarity evidence" in wave
    assert "never byte identity" in wave
    assert "never writes visual_group_scene_link" in wave
    assert "delete from public.visual_global_usage" not in wave
    assert "update public.visual_global_usage" not in wave
    assert "human-confirmed only via visual_group_link_scene" in wave


def test_wave_evidence_tables_are_immutable(wave):
    assert "before update or delete on public.visual_scene_candidate" in wave
    assert "before update or delete on public.visual_scene_phash_occupied" in wave
    assert "before update or delete on public.visual_scene_review_hold" in wave
    assert "append-only and immutable" in wave
    assert "visual scene review holds are permanent" in wave
