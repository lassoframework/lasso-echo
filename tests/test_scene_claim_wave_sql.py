"""scene_claim_wave: static SQL-text contracts for the claim-wave draft.

Follows the style of tests/test_visual_group_global_sql.py: plain text
inspection of the UNAPPLIED draft migrations. No database is touched.

`migrations/DRAFT_visual_scene_claim_wave_20261003.sql` is authored by the
SQL subagent in the same wave; these tests pin its REQUIRED contract after
the 2026-10-04 P0 repair pass (Sol/Astra SCENE_AUDIT_GAPS.md) and skip
cleanly if the file is ever absent. Contracts against SQL that predates
this wave (the frozen exact-byte ledger and the rejected sketch) always run.

Pinned contract highlights (wave-3, 2026-10-04):
  * candidates carry object_role ('display'|'poster') bound to the row's
    exact delivered object via exact_url+fingerprint attestation (P0-1);
  * claim functions take the content_calendar ROW composite;
  * visual_scene_claim_scan writes NOTHING; visual_scene_claim_decide and
    the conflict branch of the MERGED exact-byte guard
    (visual_group_guard_trigger in
    DRAFT_visual_group_claim_trigger_20261002.sql) are the only hold
    writers, idempotent via the partial unique index
    visual_scene_review_hold_open_uq (P0-3);
  * WAVE-3: the separate early scene BEFORE trigger
    (content_calendar_scene_wave_claim_guard) and its function
    (visual_scene_calendar_claim_guard) are REMOVED. The scene decision is
    inside visual_group_guard_trigger with the ordering invariant:
    attestation (23514) -> bound candidate (23514) -> fleet-locked scan ->
    in-memory held mutation -> visual_group_sync_row (may still raise) ->
    idempotent hold insert as the LAST write -> immediate RETURN NEW;
  * a caller-prefilled visual_group_key is a HINT ONLY: a refuted hint is
    nulled and takes the visual_group_identity_unresolved mark;
  * the committed held-row contract: the conflict branch NEVER raises after
    the hold insert — it mutates NEW (variant_status='archived',
    status='pending', media_not_ready_reason='scene_review_hold',
    publish_claim_token AND publish_reservation_day cleared), syncs, writes
    the hold last, and RETURNS NEW so the held row commits with its holds;
  * review RPCs lock row -> component -> fleet (matching actual DML);
  * ALL claim-path EXECUTE is revoked from service_role (P0-2); only the
    safe read/review surfaces are granted;
  * occupancy is PERMANENT — never freed on release, denial or swap (P1-3);
  * publish/approval guarded wrappers read PERSISTED state (P0-5);
  * hold_resolve / hold_reactivate are validated, one-shot, re-evaluating
    (P0-7); reactivation reports converted_back_to_held when the merged
    trigger re-holds the row; backfill stays a 0A000 stub.
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


GUARD = "DRAFT_visual_group_claim_trigger_20261002.sql"


@pytest.fixture(scope="module")
def guard():
    """Wave-3: the scene decision lives INSIDE the exact-byte guard trigger in
    the claim-trigger draft; its text is part of the wave-3 contract."""
    return _sql(GUARD)


@pytest.fixture(scope="module")
def guard_trigger(guard):
    return _fn_body(guard, "visual_group_guard_trigger()")


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
    header = wave.split("\nbegin;\n", 1)[0]
    assert "migration order (exact)" in header
    history = header.index("draft_visual_global_history_20261002.sql")
    sketch = header.index("draft_visual_scene_phash_20261003.sql")
    this_file = header.index("draft_visual_scene_claim_wave_20261003.sql")
    activation = header.index("must remain last")
    assert history < sketch < this_file < activation


def test_wave_rollback_drops_only_its_own_objects(wave):
    header = wave.split("\nbegin;\n", 1)[0]
    assert header.index("rollback") < header.index("what this file implements")
    # Wave-3: the wave file installs NO calendar trigger anymore, so its
    # rollback drops no calendar trigger — the removed wave-2 trigger
    # (content_calendar_scene_wave_claim_guard) is documented as REMOVED and
    # its rollback belongs to the wave-2 design that no longer exists.
    assert "drop trigger if exists content_calendar_scene_wave_claim_guard" \
        not in header
    assert "content_calendar_scene_wave_claim_guard" in header  # as REMOVED
    for obj in ("visual_scene_candidate", "visual_scene_phash_occupied",
                "visual_scene_review_hold",
                "visual_scene_claim_scan", "visual_scene_claim_decide",
                "visual_scene_claim_guard", "visual_scene_row_candidate",
                "visual_scene_row_delivered_object",
                "visual_scene_write_holds", "visual_scene_hold_resolve",
                "visual_scene_hold_reactivate",
                "visual_scene_publish_claim_guarded",
                "visual_scene_approval_guarded",
                "visual_scene_hamming", "visual_scene_register_candidate",
                "visual_scene_immutable", "visual_scene_review_hold_mutation",
                "visual_scene_backfill_occupied"):
        assert f"drop table if exists public.{obj}" in header or \
               f"drop function if exists public.{obj}" in header
    # the wave file's own table triggers ARE dropped (immutable guards)
    for trg in ("visual_scene_occupied_immutable",
                "visual_scene_candidate_immutable",
                "visual_scene_review_hold_guard"):
        assert f"drop trigger if exists {trg}" in header
    # the removed wave-2 calendar trigger function has no drop of its own
    # (it no longer exists anywhere in the wave-3 design)
    assert "drop function if exists public.visual_scene_calendar_claim_guard" \
        not in header
    assert "visual_scene_calendar_claim_guard no longer exists" in header
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
    # wave-2 item 4: one delivered-object helper derives the row's ACTUAL media
    # kind; BOTH the row-candidate binding and the claim scan use it.
    helper = _fn_body(wave, "visual_scene_row_delivered_object(")
    assert "p_row public.content_calendar" in helper
    assert "object_role := 'poster'" in helper
    assert "object_role := 'display'" in helper
    assert "v_thumb is distinct from v_img" in helper
    row_cand = _fn_body(wave, "visual_scene_row_candidate(")
    assert "p_row public.content_calendar" in row_cand
    assert "from public.visual_scene_row_delivered_object(p_row)" in row_cand
    assert "c.object_role = v_obj.object_role" in row_cand
    assert "c.exact_url = v_obj.exact_url" in row_cand
    # ambiguous staged evidence fails closed
    assert "conflicting staged scene candidates for one delivered object" in row_cand
    # binding resolution writes nothing
    assert "insert" not in row_cand and "update public." not in row_cand
    # the scan joins the SAME helper, so binding and scan can never disagree
    scan = _fn_body(wave, "visual_scene_claim_scan(")
    assert "join public.visual_scene_row_delivered_object(p_row)" in scan
    assert "d.object_role = c.object_role and d.exact_url = c.exact_url" in scan


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
    scope = wave_scan[exempt:exempt + 1100]
    for col in ("h.claim_date = p_row.post_date",
                "h.candidate_id = v_candidate.candidate_id",
                "h.exact_url = v_candidate.exact_url",
                "h.fingerprint = v_candidate.fingerprint",
                "h.candidate_phash = v_candidate.phash",
                "h.matched_phash = v_match.phash",
                "h.matched_tenant_id = v_match.tenant_id",
                "h.matched_group_key = v_match.group_key",
                "h.matched_used_date = v_match.used_date"):
        assert col in scope
    # the exemption is a scan-level continue, not a hold delete
    assert "exempts only" in wave
    assert "later or\n-- different conflicts still hold" in wave or \
        "later or different conflicts still hold" in wave


def test_wave_occupied_written_only_on_claimed_paths(wave, wave_decide, wave_guard, guard_trigger):
    # wave-3: the wave file itself has exactly two occupancy writers (the
    # internal decide/guard functions); the calendar-path occupancy write
    # moved INTO the merged exact-byte guard trigger in the other draft.
    writers = [part.split("(", 1)[0] for part in
               wave.split("create or replace function public.")[1:]
               if "insert into public.visual_scene_phash_occupied" in part]
    assert sorted(writers) == ["visual_scene_claim_decide",
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
    # merged guard trigger: occupancy is written ONLY on the clean path
    # (elsif scene_engaged), AFTER visual_group_sync_row, and the occupancy
    # insert is the last write before RETURN NEW on that path.
    sync = guard_trigger.index(
        "perform public.visual_group_sync_row(case when tg_op='update' then old end,new,lower(tg_op));")
    occ = guard_trigger.index("insert into public.visual_scene_phash_occupied")
    assert sync < occ
    assert guard_trigger.index("elsif scene_engaged then") < occ
    assert occ < guard_trigger.index("return new;", occ)
    # and no occupancy write happens on the conflict (hold) path
    conflict_branch = guard_trigger[guard_trigger.index("if scene_conflict then"):
                                    guard_trigger.index("elsif scene_engaged then")]
    assert "insert into public.visual_scene_phash_occupied" not in conflict_branch


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
    assert "(tenant_id, group_key, claim_date, calendar_row_id, candidate_id,\n" \
        "   candidate_phash, matched_phash, matched_tenant_id, matched_group_key,\n" \
        "   matched_used_date)" in idx
    assert "where state = 'open'" in idx


def test_wave_write_holds_is_idempotent_and_internal(wave, guard_trigger):
    writer = _fn_body(wave, "visual_scene_write_holds(")
    assert "insert into public.visual_scene_review_hold" in writer
    assert "on conflict (tenant_id, group_key, claim_date, calendar_row_id,\n" \
        "                   candidate_id, candidate_phash, matched_phash,\n" \
        "                   matched_tenant_id, matched_group_key, matched_used_date)\n" \
        "      where state = 'open'\n      do nothing" in writer
    hold_writers = [part.split("(", 1)[0] for part in
                    wave.split("create or replace function public.")[1:]
                    if "insert into public.visual_scene_review_hold" in part]
    assert hold_writers == ["visual_scene_write_holds"]
    # decide routes holds through the shared writer; the calendar path no
    # longer has its own trigger function — the MERGED exact-byte guard
    # trigger (other draft) calls the same writer on its conflict branch.
    decide = _fn_body(wave, "visual_scene_claim_decide(")
    assert "visual_scene_write_holds(" in decide
    assert "perform public.visual_scene_write_holds(" in guard_trigger
    # the wave file installs no calendar trigger and defines no calendar
    # trigger function anymore. The ONLY references to the removed wave-2
    # trigger/function are (i) the UPGRADE/REPLAY drops immediately after
    # begin; (exact zero-arg dropped signature, matching the wave-2 creation)
    # and (ii) comment documentation of those drops.
    body = wave.split("\nbegin;\n", 1)[1]
    assert "create table if not exists public.visual_scene_candidate" in body
    first_create = body.index("create table if not exists public.visual_scene_candidate")
    prefix = body[:first_create]
    assert "drop trigger if exists content_calendar_scene_wave_claim_guard on public.content_calendar;" in prefix
    assert "drop function if exists public.visual_scene_calendar_claim_guard();" in prefix
    assert prefix.index("drop trigger if exists content_calendar_scene_wave_claim_guard") < \
        prefix.index("drop function if exists public.visual_scene_calendar_claim_guard();")
    # no LIVE creation of the removed function: every occurrence of its
    # create statement is inside a comment documenting the dropped signature
    for line in wave.splitlines():
        if "create or replace function public.visual_scene_calendar_claim_guard" in line \
                or "create trigger content_calendar_scene_wave_claim_guard" in line:
            assert line.strip().startswith("--"), line
    # the only content_calendar references in the body are the upgrade drop
    # and a comment documenting the dropped signature — no live trigger
    live_refs = [l for l in body.splitlines()
                 if "on public.content_calendar" in l
                 and not l.strip().startswith("--")]
    assert live_refs == ["drop trigger if exists "
                         "content_calendar_scene_wave_claim_guard "
                         "on public.content_calendar;"]


def test_wave_upgrade_normalization_converges_bootstrap_and_upgrade(wave):
    # UPGRADE NORMALIZATION (Sol frozen-hash audit, P0): after the hold-table
    # create, idempotent alter/add-column converges a wave-2 table; legacy
    # rows lacking candidate identity hard-refuse the upgrade (23514).
    hold_create = wave.index(
        "create table if not exists public.visual_scene_review_hold")
    norm = wave.index("alter table public.visual_scene_review_hold\n"
                      "  add column if not exists candidate_id uuid")
    assert hold_create < norm
    block = wave[norm:wave.index("create unique index if not exists "
                                 "visual_scene_review_hold_open_uq", norm)]
    assert "add column if not exists candidate_id uuid\n" \
        "    references public.visual_scene_candidate(candidate_id)" in block
    assert "add column if not exists exact_url text" in block
    assert "add column if not exists fingerprint text" in block
    assert "add column if not exists resolution_evidence jsonb" in block
    # hard 23514 refusal BEFORE any not-null enforcement on legacy rows
    refusal = block.index("wave-2 scene review holds lack exact candidate "
                          "identity; archive or re-drive them before applying "
                          "this revision (draft upgrade is not lossless)")
    assert "errcode='23514'" in block
    assert refusal < block.index("alter column candidate_id set not null")
    # legacy weaker artifacts are replaced: resolved_by check ignoring
    # resolution_evidence, and open_uq lacking candidate_id
    assert "pg_get_constraintdef(oid) like '%resolved_by%'" in block
    assert "pg_get_constraintdef(oid) not like '%resolution_evidence%'" in block
    assert "pg_get_indexdef(ic.oid) not like '%candidate_id%'" in block
    assert "drop index public.visual_scene_review_hold_open_uq" in block
    # named constraints are ensured idempotently (CREATE TABLE names them
    # too, so bootstrap and upgrade converge on the same names)
    for ck in ("visual_scene_review_hold_exact_url_ck",
               "visual_scene_review_hold_fingerprint_ck",
               "visual_scene_review_hold_open_terminal_ck"):
        assert f"conname = '{ck}'" in block
        assert f"add constraint {ck}" in block


def test_wave_hold_table_is_reviewable_and_conflict_scoped(wave):
    hold = wave.split(
        "create table if not exists public.visual_scene_review_hold", 1)[1].split(");", 1)[0]
    assert "hold_kind        text        not null check (hold_kind in ('near_frame','uncertain'))" in hold
    assert "state            text        not null default 'open'" in hold
    assert "matched_tenant_id text       not null" in hold
    assert "matched_group_key text       not null" in hold
    assert "matched_used_date date       not null" in hold
    # wave-2 item 2: exact candidate identity + delivered object are real
    # hold columns (FK), not just evidence jsonb
    assert "candidate_id     uuid        not null references public.visual_scene_candidate(candidate_id)" in hold
    assert "exact_url        text        not null\n" \
        "    constraint visual_scene_review_hold_exact_url_ck " \
        "check (btrim(exact_url) <> '')" in hold
    assert "fingerprint      text        not null\n" \
        "    constraint visual_scene_review_hold_fingerprint_ck " \
        "check (fingerprint ~ '^md5:[0-9a-f]{32}$')" in hold
    # wave-2 item 3: one-shot stored review evidence; the open/terminal
    # invariant is a NAMED constraint covering resolution_evidence
    assert "resolution_evidence jsonb" in hold
    assert "constraint visual_scene_review_hold_open_terminal_ck\n" \
        "    check ((state = 'open') = (resolved_by is null and resolved_at is null\n" \
        "                               and resolution_evidence is null))" in hold
    assert "only a one-time open->terminal resolution may change a scene hold" in wave


def test_wave_trigger_installs_committed_held_row_contract(wave, guard, guard_trigger):
    # wave-3 item 1: NO separate scene calendar trigger exists anywhere. The
    # scene decision lives INSIDE the exact-byte guard, which is the ONE
    # authoritative content_calendar BEFORE trigger.
    assert "content_calendar_scene_wave_claim_guard is removed" in wave
    assert "create trigger content_calendar_scene_wave_claim_guard" not in guard
    assert "drop trigger if exists content_calendar_scene_wave_claim_guard" not in guard
    assert "visual_scene_calendar_claim_guard" not in guard
    # no LIVE creation of the removed wave-2 trigger/function in the wave
    # file either — only the UPGRADE/REPLAY drops after begin; plus comments
    for line in wave.splitlines():
        if "create or replace function public.visual_scene_calendar_claim_guard" in line \
                or "create trigger content_calendar_scene_wave_claim_guard" in line:
            assert line.strip().startswith("--"), line
    assert "create trigger content_calendar_visual_group_guard before insert or update or delete\n" \
        "  on public.content_calendar for each row execute function " \
        "public.visual_group_guard_trigger();" in guard
    # the scene block is gated on the scene claim authority: without the
    # scene draft the guard behaves exactly as the pre-wave-3 guard
    assert "to_regprocedure('public.visual_scene_claim_scan(public.content_calendar,uuid)') is not null" \
        in guard_trigger
    # caller-prefilled key is a HINT only (Sol independent-audit P0 rewrite):
    # a hint disagreeing with a NON-NULL resolution is OVERWRITTEN with the
    # resolved group and the row is fully scene-evaluated under it — the hint
    # can NEVER null its way past the scene decision. Only a genuinely NULL
    # resolution nulls the key (identity-unresolved mark path).
    assert "scene_hint := new.visual_group_key;" in guard_trigger
    assert guard_trigger.index("resolved:=public.visual_group_resolve_row(new)") < \
        guard_trigger.index("scene_hint := new.visual_group_key;")
    assert "if resolved is not null then\n        new.visual_group_key := resolved;\n" \
        "      elsif new.visual_group_key is not null then\n" \
        "        new.visual_group_key := null;" in guard_trigger
    assert "new.media_not_ready_reason := 'visual_group_identity_unresolved';" in guard_trigger
    # ORDERING INVARIANT on the engaged conflict path:
    #   byte attestation (23514) -> bound candidate (23514) -> scan
    #   -> in-memory held mutation -> sync_row (may still raise)
    #   -> LAST write: idempotent hold insert -> immediate RETURN NEW
    attest = guard_trigger.index("if not public.visual_global_row_bytes_verified(new) then")
    assert "delivered-row byte attestation missing or invalid; scene guard fails closed" \
        in guard_trigger
    bound = guard_trigger.index("scene_candidate := public.visual_scene_row_candidate(new);")
    assert "no scene candidate bound to this row''s delivered object; scene guard fails closed" \
        in guard_trigger
    scan = guard_trigger.index(
        "select * into scene_scan from public.visual_scene_claim_scan(new, scene_candidate);")
    assert attest < bound < scan
    conflict = guard_trigger.index("if scene_scan.o_worst_band is not null then")
    assert scan < conflict
    segment = guard_trigger[conflict:]
    end = segment.index("return new;") + len("return new;")
    tail = segment[:end]
    held_mutation = tail.index("new.variant_status := 'archived';")
    for stmt in ("new.status := 'pending';",
                 "new.media_not_ready_reason := 'scene_review_hold';",
                 "new.publish_claim_token := null;",
                 "new.publish_reservation_day := null;"):
        assert stmt in tail
    sync = tail.index(
        "perform public.visual_group_sync_row(case when tg_op='update' then old end,new,lower(tg_op));")
    holds = tail.index("perform public.visual_scene_write_holds(")
    assert held_mutation < sync < holds
    # NOTHING raises after the hold insertion: no raise between the hold
    # write and RETURN NEW
    after_holds = tail[holds:]
    assert "raise exception" not in after_holds
    assert after_holds.index("visual_scene_write_holds(") < \
        after_holds.index("return new;")
    # raises are still allowed BEFORE sync_row (no hold exists yet — e.g. the
    # keyed-null parity attestation check), but NOTHING raises after sync:
    # the sync->hold-insert->RETURN NEW tail is raise-free
    assert "raise exception" not in tail[sync:]
    # the keyed-null parity check: the hint is only used to run the byte
    # attestation earlier (raising before any hold), then re-nulled
    parity = guard_trigger.index("new.visual_group_key := scene_hint;")
    assert "new.visual_group_key := null;" in guard_trigger[parity:]
    assert "visual_global_row_bytes_verified(new)" in guard_trigger[parity:]
    assert parity < guard_trigger.index(
        "perform public.visual_group_sync_row(case when tg_op='update' then old end,new,lower(tg_op));")
    # fleet lock order inside the merged guard: component locks are taken
    # BEFORE the scan (which takes the single fleet advisory lock)
    assert guard_trigger.index("perform public.visual_group_lock_scene_components") < scan
    # engages only on armed, active, unambiguous, unsent rows with a date
    # The claim-refusal preflight also checks for scene authority. Inspect
    # the scene decision's own gate, rather than the first occurrence.
    scene = guard_trigger.index("-- wave-3 scene decision")
    gate_start = guard_trigger.index("if not finalized", scene)
    engage = guard_trigger.index(
        "to_regprocedure('public.visual_scene_claim_scan(public.content_calendar,uuid)') is not null",
        gate_start)
    gate = guard_trigger[gate_start:engage]
    assert "public.visual_group_row_active(new)" in gate
    assert "not public.visual_group_row_ambiguous(new)" in gate
    assert "new.post_date is not null" in gate


def test_wave_header_states_realized_hold_rollback_semantics(wave):
    header = wave.split("\nbegin;\n", 1)[0]
    assert "hold-rollback: enforced semantics (realized)" in header
    assert "a hold row exists if and only if the claim transaction commits in a\n-- blocked/held state" in header
    # wave-3: the held row commits inside the ONE authoritative BEFORE path
    # (the merged exact-byte guard), holds written LAST, no raise after hold
    assert "commits\n-- the held row with its holds" in header
    assert "one authoritative before path (visual_group_guard_trigger)" in header
    assert "no raise after hold" in header
    assert "dblink" in header and "rejected" in header
    assert "an integration\n--     that raises gets no hold, by construction" in header


# ---- (P0-2) EXECUTE revocation: claim path is trigger/internal only ----------

def test_wave_claim_path_execute_revoked_from_service_role(wave):
    for fn in ("public.visual_scene_claim_scan(public.content_calendar, uuid)",
               "public.visual_scene_claim_decide(public.content_calendar, uuid)",
               "public.visual_scene_claim_guard(public.content_calendar, uuid)",
               "public.visual_scene_row_candidate(public.content_calendar)",
               "public.visual_scene_row_delivered_object(public.content_calendar)",
               "public.visual_scene_backfill_occupied()"):
        assert f"revoke all on function {fn}\n  from public, anon, authenticated, service_role" in wave
    assert "revoke all on function public.visual_scene_write_holds(text, text, date, uuid, text, uuid, char(16), text, text, jsonb)\n  from public, anon, authenticated, service_role" in wave
    # wave-3: the removed calendar trigger function has NO revoke (it does not
    # exist); its revocation is obsolete, not relocated to the wave file
    assert "revoke all on function public.visual_scene_calendar_claim_guard" not in wave
    for fn in ("visual_scene_claim_scan", "visual_scene_claim_decide",
               "visual_scene_claim_guard", "visual_scene_row_candidate",
               "visual_scene_row_delivered_object",
               "visual_scene_write_holds",
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
    # wave-3 item 3 lock order matches ACTUAL DML: calendar row FOR UPDATE
    # FIRST, then scene-component locks, then the fleet advisory lock
    row_lock = res.index("where id = v_hold.calendar_row_id for update")
    assert row_lock < res.index("visual_group_lock_scene_components")
    assert res.index("visual_group_lock_scene_components") < \
        res.index("pg_try_advisory_xact_lock")
    # wave-3: stale and cross-tenant review refusals before any resolution
    assert "held calendar row no longer present; refusing stale resolution" in res
    assert "calendar row moved tenants since the hold; cross-tenant review refused" in res
    assert "calendar row mutated since the hold; refusing stale resolution" in res
    assert "reviewed conflict no longer present in the live scene scan; refusing stale resolution" in res
    # frozen-hash P0: after the row lock, tenant and group are RE-RESOLVED
    # UNCONDITIONALLY from the live row (neither the persisted key nor the
    # hold is authority); component locks cover BOTH identities; the drift
    # check uses the re-resolved group; the re-resolved key is driven into
    # the row BEFORE binding/attestation/scan
    assert res.index("v_row_tenant := public.visual_group_tenant_id(v_row.gym_id)::text;") > row_lock
    assert res.index("v_resolved := public.visual_group_resolve_row(v_row);") > row_lock
    assert res.index("v_resolved := public.visual_group_resolve_row(v_row);") < \
        res.index("visual_group_lock_scene_components")
    assert "jsonb_build_object('gym_id', v_row_tenant, 'group_key', v_resolved)" in res
    assert "jsonb_build_object('gym_id', v_hold.tenant_id, 'group_key', v_hold.group_key)" in res
    assert "if v_row_tenant is null or v_row_tenant is distinct from v_hold.tenant_id then" in res
    assert "if v_resolved is null or v_resolved is distinct from v_hold.group_key" in res
    reassign = res.index("v_row.visual_group_key := v_resolved;")
    assert reassign < res.index("visual_scene_row_delivered_object(v_row)")
    assert reassign < res.index("visual_global_row_bytes_verified(v_row)")
    assert reassign < res.index("visual_scene_claim_scan(v_row, v_hold.candidate_id)")
    # POST-LOCK STABILITY (Sol P0): after component+fleet locks the identity
    # is re-resolved and any drift raises 55P03 retry, before the hold re-read
    stability = res.index("if public.visual_group_resolve_row(v_row) is distinct from v_resolved")
    assert "or public.visual_group_tenant_id(v_row.gym_id)::text is distinct from v_row_tenant then\n" \
        "    raise exception 'visual identity changed while locking; retry transaction' " \
        "using errcode='55p03';" in res
    assert res.index("pg_try_advisory_xact_lock") < stability
    assert stability < res.index("where hold_id = p_hold_id for update")
    # UNIQUE-CANDIDATE CHECK (Sol P1): the reviewed candidate must be the
    # UNIQUE candidate bound to the live row's delivered object
    uniq = res.index("bound scene candidate for the live row is not uniquely "
                     "the reviewed candidate; refusing resolution")
    assert "errcode='23514'" in res[uniq:]
    assert ") <> 1\n      or public.visual_scene_row_candidate(v_row) is distinct from v_hold.candidate_id then" in res
    assert reassign < uniq
    # wave-2 item 3: actor AND review evidence stored one-shot
    assert "resolution_evidence = p_evidence" in res
    # approved resolutions return the exact exemption scope
    assert "'exemption_scope'" in res
    assert res.index("return jsonb_build_object('hold_id'") > \
        res.index("update public.visual_scene_review_hold")


def test_wave_hold_reactivate_requires_approval_and_clean_rescan(wave):
    re = _fn_body(wave, "visual_scene_hold_reactivate(")
    assert "hold reactivation needs a named actor" in re
    assert "only an approved scene hold can reactivate its calendar row" in re
    assert "calendar row is not in scene-review-held state" in re
    # wave-3 item 3 lock order matches ACTUAL DML: the calendar row is locked
    # FOR UPDATE FIRST, then the scene-component locks, then the single
    # fleet-wide advisory lock, all before the fresh re-scan.
    row_lock = re.index("where id = v_hold.calendar_row_id for update")
    assert row_lock < re.index("visual_group_lock_scene_components")
    assert re.index("visual_group_lock_scene_components") < \
        re.index("pg_try_advisory_xact_lock")
    assert re.index("pg_try_advisory_xact_lock") < \
        re.index("visual_scene_claim_scan(v_row")
    # wave-3: null calendar_row_id is a hard stale-reactivation refusal
    assert "hold has no calendar row to reactivate; refusing stale reactivation" in re
    # cross-tenant and identity-drift refusals before any mutation
    assert "calendar row moved tenants since the hold; cross-tenant reactivation refused" in re
    assert "calendar row identity drifted from the reviewed hold; refusing reactivation" in re
    # frozen-hash P0: tenant/group RE-RESOLVED from the live locked row;
    # component locks cover BOTH identities; drift check uses the
    # re-resolved group; the re-resolved key drives binding and the re-scan
    assert re.index("v_row_tenant := public.visual_group_tenant_id(v_row.gym_id)::text;") > row_lock
    assert re.index("v_resolved := public.visual_group_resolve_row(v_row);") > row_lock
    assert re.index("v_resolved := public.visual_group_resolve_row(v_row);") < \
        re.index("visual_group_lock_scene_components")
    assert "jsonb_build_object('gym_id', v_row_tenant, 'group_key', v_resolved)" in re
    assert "jsonb_build_object('gym_id', v_hold.tenant_id, 'group_key', v_hold.group_key)" in re
    assert "if v_row_tenant is null or v_row_tenant is distinct from v_hold.tenant_id then" in re
    assert "if v_resolved is null or v_resolved is distinct from v_hold.group_key" in re
    reassign = re.index("v_row.visual_group_key := v_resolved;")
    assert reassign < re.index("visual_scene_row_candidate(v_row)")
    assert reassign < re.index("visual_scene_claim_scan(v_row, v_candidate_id)")
    # POST-LOCK STABILITY (Sol P0): identity re-resolved under the full lock
    # set; drift raises 55P03 retry before the hold re-read
    stability = re.index("if public.visual_group_resolve_row(v_row) is distinct from v_resolved")
    assert "visual identity changed while locking; retry transaction' using errcode='55p03'" in re
    assert re.index("pg_try_advisory_xact_lock") < stability
    assert stability < re.index("where hold_id = p_hold_id for update")
    # UNIQUE-CANDIDATE PRECONDITION (Sol P1, raising): exactly one candidate
    # may bind the live row's delivered object and it must BE the approved
    # hold's reviewed candidate — any other shape RAISES 23514 before any
    # mutation (the old no_bound_candidate false-return is gone).
    pre = re.index("bound scene candidate for the live row is not uniquely "
                   "the approved candidate; refusing reactivation")
    assert "errcode='23514'" in re[pre:]
    assert ") <> 1\n      or public.visual_scene_row_candidate(v_row) is distinct from v_hold.candidate_id then" in re
    assert "v_candidate_id is null" not in re
    assert "'reason', 'no_bound_candidate', 'hold_id', p_hold_id);" not in re
    assert reassign < pre
    assert pre < re.index("v_candidate_id := v_hold.candidate_id;")
    assert re.index("v_candidate_id := v_hold.candidate_id;") < \
        re.index("visual_scene_claim_scan(v_row, v_candidate_id)")
    # fresh bound-candidate re-scan; refusal mutates nothing. The ONLY
    # false-return paths left are the pre-scan blocked return
    # (still_blocked) and converted_back_to_held (merged guard re-held).
    assert "visual_scene_claim_scan(v_row, v_candidate_id)" in re
    assert "'still_blocked'" in re
    assert re.count("return jsonb_build_object('reactivated', false") == 2
    assert "'persisted_identity_mismatch'" not in re
    assert re.index("visual_scene_claim_scan(v_row") < \
        re.index("update public.content_calendar")
    assert "set variant_status = 'active', status = 'pending',\n        media_not_ready_reason = null" in re
    # wave-3: the UPDATE is guarded by ROW_COUNT — a concurrent mutation of
    # the held row refuses the whole reactivation
    assert "get diagnostics v_updated = row_count;" in re
    assert "calendar row mutated during reactivation; refusing reactivation" in re
    # wave-3: the UPDATE re-enters the merged trigger, which may convert the
    # row back to held; the PERSISTED row is re-read and failure is reported
    upd = re.index("update public.content_calendar")
    assert re.index("select * into v_row from public.content_calendar where id = v_row.id;", upd) > upd
    assert "'converted_back_to_held'" in re
    assert re.index("return jsonb_build_object('reactivated', true") > \
        re.index("'converted_back_to_held'")
    # RAISING POSTCONDITION (Sol frozen-hash P0): after the UPDATE the
    # identity is re-resolved from the persisted row again; an
    # active-but-wrong-identity outcome RAISES 23514 (rolling back the UPDATE
    # and the occupancy) with a DETAIL jsonb of the persisted facts — it may
    # NEVER commit as a false return.
    reload_ = re.index("select * into v_row from public.content_calendar where id = v_row.id;", upd)
    assert re.index("v_resolved := public.visual_group_resolve_row(v_row);", reload_) > reload_
    post = re.index("persisted row identity failed reactivation verification; "
                    "rolling back reactivation")
    assert "errcode='23514'" in re[post:]
    assert "detail = jsonb_build_object('hold_id', p_hold_id," in re
    assert "v_candidate.candidate_id is distinct from v_hold.candidate_id" in re
    assert "v_candidate.exact_url is distinct from v_hold.exact_url" in re
    assert "v_candidate.fingerprint is distinct from v_hold.fingerprint" in re
    assert reload_ < post < re.index("return jsonb_build_object('reactivated', true")
    # the result jsonb carries the full persisted facts on success, and the
    # raising postcondition carries them in DETAIL
    for frag in ("'tenant_id', v_row_tenant, 'group_key', v_resolved",
                 "'post_date', v_row.post_date",
                 "'candidate_id', v_candidate.candidate_id",
                 "'exact_url', v_candidate.exact_url",
                 "'fingerprint', v_candidate.fingerprint"):
        assert frag in re
    assert re.count("'persisted', jsonb_build_object('status', v_row.status") >= 3


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
