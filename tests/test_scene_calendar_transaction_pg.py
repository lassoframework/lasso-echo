"""DRAFT transaction wiring acceptance on a fresh, private local PG17 cluster.

No supplied DSN is accepted. CI without PG17 skips these optional integration
checks; a release requires a separate recorded PG17 run. Existing scene-ledger
fixture helpers are reused for exact-byte attestation setup. Every server is
stopped on exit. Historical coverage and production activation remain release gates.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scene_fixture", ROOT / "tests/test_scene_ledger_claim_pg.py")
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)
_PG17 = Path(os.environ.get("ECHO_SCENE_TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
_PATH_POSTGRES = shutil.which("postgres")
BIN = _PG17 if (_PG17 / "postgres").exists() else (
    Path(_PATH_POSTGRES).parent if _PATH_POSTGRES else Path("/nonexistent"))
DRAFT = ROOT / "migrations/DRAFT_visual_scene_calendar_transaction_20261005.sql"


def command(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=180, **kwargs)


@pytest.fixture(scope="module", autouse=True)
def cluster():
    available = (BIN / "postgres").exists()
    if available:
        available = "17." in command([str(BIN / "postgres"), "--version"]).stdout
    if not available and os.environ.get("ECHO_SCENE_REQUIRE_PG17") == "1":
        pytest.fail("PG17 required for release acceptance but unavailable")
    if not available:
        pytest.skip("PG17 unavailable; release acceptance requires a recorded private PG17 run")
    with tempfile.TemporaryDirectory(prefix="echo-scene-txn-") as temp:
        base = Path(temp)
        data = base / "data"
        command([str(BIN / "initdb"), "-D", str(data), "-A", "trust", "--no-locale"])
        command([str(BIN / "pg_ctl"), "-D", str(data), "-l", str(base / "server.log"),
                 "-o", f"-k {base} -p 55479 -h ''", "-w", "start"])
        try:
            command([str(BIN / "createdb"), "-h", str(base), "-p", "55479", "echo_scene_ledger_test"])
            ledger.PSQL = str(BIN / "psql")
            ledger.DSN = f"host={base} port=55479 dbname=echo_scene_ledger_test"
            ledger._scratch_stack.__wrapped__()
            ledger._sql("alter table public.content_calendar add column format text, add column caption text, add column scheduled_at timestamptz")
            ledger._sql("create table public.gyms(id uuid primary key, slug text, name text); "
                        "create table public.echo_intake_tokens(gym_id uuid, echo_account_key text); "
                        "create table public.echo_gym_settings(gym_id uuid primary key, autonomous boolean)")
            # Production order: dated capacity patches, authoritative provenance,
            # THEN scene composition. Do not test against obsolete RPC stubs.
            for name in ("lasso_bounded_catchup_capacity_20261005.sql",
                         "lasso_immediate_backlog_capacity_20261005.sql",
                         "calendar_approval_provenance_20261005.sql"):
                ledger._sql((ROOT / "migrations" / name).read_text())
            authority = ROOT / "migrations/DRAFT_visual_scene_activation_authority_20261006.sql"
            # Both authority DDL and trigger composition must roll back cleanly.
            authority_body = authority.read_text().replace("begin;", "", 1).rsplit("commit;", 1)[0]
            ledger._sql("begin; " + authority_body + "; rollback;")
            assert ledger._one("select to_regclass('public.gym_visual_scene_guard_settings') is null") == "t"
            ledger._sql(authority.read_text())
            assert ledger._one("select count(*) from public.gym_visual_scene_guard_settings") == "0"
            # Rehearse the complete composition under rollback first: every
            # function replacement and obsolete-signature drop must roll back.
            before = ledger._one("select md5(string_agg(proname||prosrc, '' order by oid)) from pg_proc where pronamespace='public'::regnamespace")
            draft_body = DRAFT.read_text().replace("begin;", "", 1).rsplit("commit;", 1)[0]
            ledger._sql("begin; " + draft_body + "; rollback;")
            assert ledger._one("select md5(string_agg(proname||prosrc, '' order by oid)) from pg_proc where pronamespace='public'::regnamespace") == before
            ledger._sql(DRAFT.read_text())
            assert ledger._one("select count(*) from pg_trigger where tgrelid='public.content_calendar'::regclass and not tgisinternal and tgname='content_calendar_visual_group_guard'") == "1"
            yield
        finally:
            command([str(BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"])


@pytest.fixture(autouse=True)
def reset(cluster):
    ledger._isolate_scenarios.__wrapped__()
    # This expanded module also creates terminal reconciliation/member events
    # and setting audit history. Clear them only on the disposable test server,
    # so a published fixture cannot contaminate the next activation scenario.
    ledger._sql("set session_replication_role=replica; truncate "
                "public.visual_group_member_event,public.visual_group_reconciliation,"
                "public.gym_visual_scene_guard_settings,public.visual_scene_disarm_event")


def scratch_scene_on(tenant):
    # Synthetic ON-state fixture only. The actual arming path is deliberately
    # absent/refused; tests never fabricate a coverage receipt or call activation.
    # This server is the private named Unix-socket disposable DB created above.
    ledger._sql("set session_replication_role=replica; "
                f"insert into public.gym_visual_scene_guard_settings(gym_id,enforce) values ('{tenant}',true)")


def seed(phash="0000000000000000", account="ig", active=True, scene_on=True):
    tenant, group = ledger._seed_tenant()
    if scene_on:
        scratch_scene_on(tenant)
    url, fp, candidate = ledger._seed_object(tenant, group, phash)
    row = ledger._one("insert into public.content_calendar(gym_id,account,post_date,status,variant_status,image_url,source_media_url,visual_group_key) values "
        f"('{tenant}','{account}','2026-10-10','pending','{'active' if active else 'archived'}','{url}','{url}','{group}') returning id")
    return tenant, group, url, fp, candidate, row


def add_conflict(phash="0000000000000001"):
    tenant, group = ledger._seed_tenant()
    url, fp, candidate = ledger._seed_object(tenant, group, phash)
    ledger._sql("insert into public.visual_scene_phash_occupied(phash,tenant_id,group_key,used_date,fingerprint,evidence) values "
                f"('{phash}','{tenant}','{group}','2026-10-11','{fp}','{{}}')")
    return tenant, group


def state(row):
    return json.loads(ledger._one(f"select row_to_json(c) from public.content_calendar c where id='{row}'"))


def assert_held(row):
    current = state(row)
    assert (current["status"], current["variant_status"], current["media_not_ready_reason"]) == ("pending", "archived", "scene_review_hold")
    assert current["publish_claim_token"] is None
    assert current["publish_reservation_day"] is None
    assert all(current[key] is None for key in
               ("approval_kind", "approved_by", "approved_at", "approval_digest"))


def claim(row, tenant):
    return ledger._one(f"select public.claim_calendar_publish_slot_owned('{row}','{tenant}','2026-10-10','UTC',2,false)")


def test_clean_claim_and_cas_token():
    tenant, group, url, fp, candidate, row = seed()
    token = claim(row, tenant)
    assert token and state(row)["publish_claim_token"] == token
    assert state(row)["status"] == "publishing"
    assert claim(row, tenant) == ""
    assert ledger._occupied_count() == 1
    assert ledger._one("select count(*) from public.visual_global_usage") == "1"


@pytest.mark.parametrize("account,bits", [("ig", 1), ("gbp", 7)])
def test_claim_returning_never_mints_token_for_committed_hold(account, bits):
    tenant, group, url, fp, candidate, row = seed(account=account)
    add_conflict(ledger._near("0000000000000000", bits))
    assert claim(row, tenant) == ""
    assert_held(row)
    assert len(ledger._holds("open")) == 1
    # No false new occupancy from the blocked publishing attempt.
    assert ledger._occupied_count() == 2
    assert claim(row, tenant) == ""
    assert len(ledger._holds("open")) == 1


def test_approval_returning_filters_trigger_held_row():
    tenant, group, url, fp, candidate, row = seed()
    add_conflict()
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "0"
    assert_held(row)
    assert len(ledger._holds()) == 1
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "0"


@pytest.mark.parametrize("account", ["ig", "gbp"])
def test_legacy_direct_patch_and_idempotent_reactivation(account):
    tenant, group, url, fp, candidate, row = seed(account=account, active=False)
    add_conflict()
    ledger._sql(f"update public.content_calendar set status='publishing',variant_status='active',publish_claim_token=gen_random_uuid(),publish_reservation_day='2026-10-10' where id='{row}'")
    assert_held(row)
    assert ledger._occupied_count() == 1
    assert ledger._one("select count(*) from public.visual_global_usage") == "0"
    ledger._sql(f"update public.content_calendar set status='approved',variant_status='active',media_not_ready_reason=null where id='{row}'")
    assert_held(row)
    assert len(ledger._holds()) == 1


def test_unknown_candidate_fails_closed():
    tenant, group = ledger._seed_tenant()
    scratch_scene_on(tenant)
    url, fp, _ = ledger._seed_object(tenant, group)
    row = ledger._insert_row(tenant, group, url)
    assert_held(row)
    assert ledger._occupied_count() == 0
    assert ledger._one("select count(*) from public.visual_global_usage") == "0"
    assert ledger._holds() == []  # no fabricated match identity


def test_rollback_removes_row_hold_and_consumption():
    tenant, group, url, fp, candidate, row = seed(active=False)
    add_conflict()
    ledger._sql(f"begin; update public.content_calendar set variant_status='active' where id='{row}'; rollback;")
    assert state(row)["media_not_ready_reason"] is None
    assert ledger._holds() == []
    assert ledger._occupied_count() == 1
    assert ledger._one("select count(*) from public.visual_global_usage") == "0"
    # Clean claim rolled back removes BOTH exact-byte and scene consumption.
    ledger._sql("set session_replication_role=replica; truncate public.visual_scene_phash_occupied")
    ledger._sql(f"begin; update public.content_calendar set variant_status='active' where id='{row}'; rollback;")
    assert ledger._occupied_count() == 0
    assert ledger._one("select count(*) from public.visual_global_usage") == "0"


def test_ambiguous_attempt_is_not_erased_to_save_hold():
    tenant, group, url, fp, candidate, row = seed()
    token = claim(row, tenant)
    add_conflict()
    result = ledger._run(f"update public.content_calendar set status='published',published_at=now() where id='{row}'", check=False)
    assert result.returncode != 0
    assert state(row)["publish_claim_token"] == token
    assert state(row)["status"] == "publishing"
    assert ledger._holds() == []


def test_fleet_lock_concurrency_fails_closed_then_retry_holds():
    tenant, group, url, fp, candidate, row = seed(active=False)
    # One transaction owns fleet scan serialization and records a conflicting
    # use while another tenant tries its independent component locks.
    other, other_group = ledger._seed_tenant()
    other_url, other_fp, _ = ledger._seed_object(other, other_group, "0000000000000001")
    marker = "scene_global_lock_ready"
    proc = subprocess.Popen([ledger.PSQL,"-X","-q","-A","-t","-v","ON_ERROR_STOP=1","-d",ledger.DSN],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.stdin.write("begin; select pg_advisory_xact_lock(hashtextextended(jsonb_build_array('visual_scene_global')::text,0)); "
                     f"insert into public.visual_scene_phash_occupied(phash,tenant_id,group_key,used_date,fingerprint) values ('0000000000000001','{other}','{other_group}','2026-10-11','{other_fp}'); select '{marker}';\n")
    proc.stdin.flush()
    try:
        while marker not in proc.stdout.readline():
            assert proc.poll() is None
        attempt = ledger._run(f"update public.content_calendar set variant_status='active' where id='{row}'", check=False)
        assert attempt.returncode != 0 and 'busy' in attempt.stderr
        assert state(row)["media_not_ready_reason"] is None
        assert ledger._holds() == []
        proc.stdin.write("commit;\n\\q\n")
        proc.stdin.flush()
        proc.wait(timeout=20)
        assert proc.returncode == 0
        ledger._sql(f"update public.content_calendar set variant_status='active' where id='{row}'")
        assert_held(row)
        assert len(ledger._holds()) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_transaction_draft_does_not_arm_or_backfill():
    assert ledger._run("select public.visual_scene_backfill_occupied()", check=False).returncode != 0
    assert "create trigger" not in DRAFT.read_text().lower()
    assert ledger._one("select count(*) from public.visual_scene_phash_occupied") == "0"


def test_legal_siblings_preserve_raw_tenant_alias():
    tenant = str(uuid.uuid4())
    group = 'raw_alias_group'
    alias = 'scratch-raw-alias'
    # Alias inventory must precede activation; the existing exact-byte guard
    # correctly refuses attaching a new authority alias to an already armed gym.
    ledger._sql(f"insert into public.tenant_alias(alias_key,tenant_id) values ('{tenant}','{tenant}'),('{alias}','{tenant}')")
    ledger._sql(f"insert into public.visual_group(gym_id,group_key) values ('{tenant}','{group}')")
    ledger._sql(f"select public.visual_group_activate_guard('{tenant}','transaction-test')")
    scratch_scene_on(tenant)
    url, fp, candidate = ledger._seed_object(tenant, group, '0000000000000000')
    row = ledger._insert_row(tenant, group, url)
    sibling = ledger._one("insert into public.content_calendar(gym_id,account,post_date,status,variant_status,image_url,source_media_url,visual_group_key) values "
        f"('{alias}','gbp','2026-10-10','pending','active','{url}','{url}','{group}') returning id")
    assert state(sibling)["gym_id"] == alias
    assert state(sibling)["media_not_ready_reason"] is None
    assert ledger._occupied_count() == 1
    assert ledger._one("select count(*) from public.visual_global_usage") == "1"
    assert ledger._one("select count(*) from public.visual_group_usage_sibling where state='active'") == "2"
    assert claim(sibling, alias)


def test_same_tenant_other_date_scene_is_held():
    tenant, group, url, fp, candidate, row = seed()
    new_group = 'second_date_scene'
    ledger._sql(f"insert into public.visual_group(gym_id,group_key) values ('{tenant}','{new_group}')")
    new_url, _, _ = ledger._seed_object(tenant, new_group, "0000000000000001")
    next_row = ledger._insert_row(tenant, new_group, new_url, date="2026-10-11")
    assert_held(next_row)
    assert ledger._occupied_count() == 1
    assert ledger._one("select count(*) from public.visual_global_usage") == "1"
    assert len(ledger._holds()) == 1


def test_exact_byte_collision_rolls_back_clean_scene_claim():
    tenant, group, url, fp, candidate, row = seed()
    other, other_group = ledger._seed_tenant()
    other_url = 'https://scratch.example/same-exact-bytes.jpg'
    receipt = ledger._one("insert into public.visual_global_object_read_receipt"
        "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by) values "
        f"('{other}','{other_url}','{fp}',1024,'verified_object_read','scratch','transaction-test') returning receipt_id")
    ledger._sql("insert into public.visual_global_object_attestation"
        "(exact_url,tenant_id,group_key,fingerprint,byte_length,acquisition_method,evidence_ref,read_receipt,attested_by) values "
        f"('{other_url}','{other}','{other_group}','{fp}',1024,'verified_object_read','scratch','{receipt}','transaction-test')")
    for role in ('source', 'delivered'):
        ledger._sql("insert into public.visual_global_scene_object_member(tenant_id,group_key,exact_url,fingerprint,object_role) values "
            f"('{other}','{other_group}','{other_url}','{fp}','{role}')")
    ledger._sql("insert into public.visual_group_alias(gym_id,alias_kind,alias_value,group_key) values "
        f"('{other}','canonical_url','{other_url}','{other_group}')")
    ledger._sql("insert into public.visual_scene_owner_phash_receipt "
        "(receipt_id,tenant_id,group_key,object_role,exact_url,fingerprint,phash,byte_length,algorithm) "
        f"values (gen_random_uuid(),'{other}','{other_group}','display','{other_url}','{fp}','aaaaaaaaaaaaaaaa',1024,'echo-dct-phash64-v1')")
    ledger._sql("select public.visual_scene_register_candidate("
        f"'{other}','{other_group}','aaaaaaaaaaaaaaaa','{other_url}','{fp}',"
        f"jsonb_build_object('verified_bytes','{fp}','owner_phash_receipt',(select receipt_id::text from public.visual_scene_owner_phash_receipt where exact_url='{other_url}')),'transaction-test','display')")
    failed = ledger._run("insert into public.content_calendar(gym_id,account,post_date,status,variant_status,image_url,source_media_url,visual_group_key) values "
        f"('{other}','ig','2026-10-10','pending','active','{other_url}','{other_url}','{other_group}')", check=False)
    assert failed.returncode != 0
    assert ledger._occupied_count() == 1
    assert ledger._one("select count(*) from public.visual_global_usage") == "1"
    assert ledger._one(f"select count(*) from public.visual_group_usage_ledger where gym_id='{other}'") == "0"
    assert ledger._holds() == []
    assert ledger._one(f"select count(*) from public.content_calendar where gym_id='{other}'") == "0"


def test_owned_rpc_wrong_tenant_and_archived_approval_fail_closed():
    tenant, group, url, fp, candidate, row = seed(active=False)
    wrong, _ = ledger._seed_tenant()
    assert claim(row, wrong) == ""
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "0"
    assert state(row)["variant_status"] == "archived"
    assert ledger._occupied_count() == 0


def test_review_approval_requires_fresh_reactivation_and_new_conflict_reholds():
    tenant, group, url, fp, candidate, row = seed(active=False)
    add_conflict()
    ledger._sql(f"update public.content_calendar set variant_status='active' where id='{row}'")
    hold = ledger._holds()[0]["hold_id"]
    ledger._sql(f"select public.visual_scene_hold_resolve('{hold}','approved','transaction-reviewer','{{\"review\":\"specific pair only\"}}')")
    assert_held(row)  # approving evidence is not calendar activation
    ledger._sql(f"update public.content_calendar set variant_status='active',media_not_ready_reason=null where id='{row}'")
    assert state(row)["variant_status"] == "active"
    assert state(row)["media_not_ready_reason"] is None
    assert ledger._occupied_count() == 2
    add_conflict("0000000000000003")
    assert claim(row, tenant) == ""
    assert_held(row)
    assert len(ledger._holds("approved")) == 1
    assert len(ledger._holds("open")) == 1


def proof_stamp(row, tenant):
    ledger._sql(f"insert into public.gyms values ('{tenant}','scene-proof','Scene proof') on conflict do nothing; "
                f"insert into public.echo_intake_tokens values ('{tenant}','{tenant}') on conflict do nothing; "
                f"insert into public.echo_gym_settings values ('{tenant}',false) on conflict do nothing")
    digest = state(row)["approval_digest"]
    return ledger._one(f"select count(*) from public.calendar_stamp_verified_approval('{tenant}','{row}','clerk-test','{digest}')")


def test_defaulted_rpc_signatures_are_single_and_acl_preserved():
    for name, count in (("claim_calendar_publish_slot_owned", 7),
                        ("approve_calendar_row_if_media_ready", 3)):
        assert ledger._one(f"select count(*) from pg_proc where pronamespace='public'::regnamespace and proname='{name}'") == "1"
        assert ledger._one(f"select pronargs||':'||pronargdefaults from pg_proc where pronamespace='public'::regnamespace and proname='{name}'") == f"{count}:1"
    assert ledger._one("select has_function_privilege('service_role','public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean,boolean)','EXECUTE')") == "t"
    assert ledger._one("select has_function_privilege('authenticated','public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean,boolean)','EXECUTE')") == "f"


def test_expected_card_proof_reset_and_manual_digest_are_preserved():
    tenant, group, url, fp, candidate, row = seed()
    ledger._sql(f"update public.content_calendar set caption='Visible caption' where id='{row}'")
    expected = dict(caption="stale", media_url=url, day_key="2026-10-10", format="feed", platform="ig")
    def approve():
        return ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}','{json.dumps(expected)}'::jsonb)")
    assert approve() == "0" and state(row)["status"] == "pending"
    expected["caption"] = "Visible caption"
    ledger._sql(f"update public.content_calendar set approval_kind='automatic',approved_by='old',approved_at=now(),approval_digest='old' where id='{row}'")
    assert approve() == "1"
    current = state(row)
    assert all(current[key] is None for key in ("approval_kind", "approved_by", "approved_at"))
    assert current["approval_digest"] == ledger._one(f"select public.calendar_approval_digest(c) from public.content_calendar c where id='{row}'")
    assert ledger._one(f"select public.claim_calendar_publish_slot_owned('{row}','{tenant}','2026-10-10','UTC',2,true,true)") == ""
    assert proof_stamp(row, tenant) == "1"
    ledger._sql(f"update public.content_calendar set caption='Changed after review' where id='{row}'")
    assert ledger._one(f"select public.claim_calendar_publish_slot_proven_owned('{row}','{tenant}','2026-10-10','UTC',2,true)") == ""
    assert state(row)["status"] == "approved"


def test_manual_proven_claim_commits_hold_without_snapshot_or_proof():
    tenant, group, url, fp, candidate, row = seed()
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "1"
    assert proof_stamp(row, tenant) == "1"
    add_conflict()
    assert ledger._one(f"select public.claim_calendar_publish_slot_proven_owned('{row}','{tenant}','2026-10-10','UTC',2,true)") == ""
    assert_held(row)
    assert len(ledger._holds("open")) == 1


def test_proof_stamp_filters_new_scene_hold():
    tenant, group, url, fp, candidate, row = seed()
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "1"
    add_conflict()
    assert proof_stamp(row, tenant) == "0"
    assert_held(row)
    assert len(ledger._holds("open")) == 1


def test_gbp_claim_commits_hold_and_returns_no_mode_snapshot():
    tenant, group, url, fp, candidate, row = seed(account="googlebusiness")
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "1"
    assert proof_stamp(row, tenant) == "1"
    add_conflict()
    assert ledger._one(f"select public.claim_calendar_gbp_publish_with_mode_owned('{row}','{tenant}')") == ""
    assert_held(row)
    assert len(ledger._holds("open")) == 1


def test_stale_reservations_and_null_proof_argument_remain_rejected():
    tenant, group, url, fp, candidate, row = seed()
    assert ledger._one(f"select public.claim_calendar_publish_slot_owned('{row}','{tenant}','2026-10-10','UTC',2,false,null)") == ""
    ledger._sql(f"set session_replication_role=replica; update public.content_calendar set publish_reservation_day='2026-10-09' where id='{row}'")
    assert claim(row, tenant) == ""
    assert state(row)["status"] == "pending"


def test_prerequisite_failure_and_composition_rollback():
    # The migration must not fabricate older RPCs when provenance is absent.
    before = ledger._one("select md5(prosrc) from pg_proc where oid='public.visual_group_guard_trigger()'::regprocedure")
    body = DRAFT.read_text().replace("begin;", "", 1).rsplit("commit;", 1)[0]
    result = ledger._run("begin; drop function public.approve_calendar_row_if_media_ready(uuid,text,jsonb); " + body, check=False)
    assert result.returncode != 0 and "Apply current calendar approval provenance" in result.stderr
    assert ledger._one("select md5(prosrc) from pg_proc where oid='public.visual_group_guard_trigger()'::regprocedure") == before
    ledger._sql("begin; " + body + "; rollback;")
    assert ledger._one("select md5(prosrc) from pg_proc where oid='public.visual_group_guard_trigger()'::regprocedure") == before


@pytest.mark.parametrize("capacity,day,current_limit,backlog_limit", [(3,"2026-10-10",3,0),(5,"2026-10-10",3,2),(15,"2026-10-06",3,12)])
def test_current_lasso_story_and_dated_capacity_envelopes(capacity, day, current_limit, backlog_limit):
    tenant = str(uuid.uuid4())
    ledger._sql(f"delete from public.tenant_alias where alias_key='lasso'; "
                f"insert into public.tenant_alias(alias_key,tenant_id) values ('{tenant}','{tenant}'),('lasso','{tenant}')")
    ledger._sql(f"select public.visual_group_activate_guard('{tenant}','capacity-test')")
    scratch_scene_on(tenant)
    # Distinct days have distinct exact objects/scenes; same-day rows are legal
    # siblings. Capacity is per channel/format, independent of scene identity.
    def rows(post_date, word, number):
        group = 'capacity_' + post_date
        ledger._sql(f"insert into public.visual_group(gym_id,group_key) values ('{tenant}','{group}')")
        url, fp, candidate = ledger._seed_object(tenant, group, ledger._word(word))
        return [ledger._one("insert into public.content_calendar(gym_id,account,format,post_date,status,variant_status,image_url,source_media_url,visual_group_key) values "
                f"('lasso','ig','story','{post_date}','pending','active','{url}','{url}','{group}') returning id") for _ in range(number)]
    def take(row):
        return ledger._one(f"select public.claim_calendar_publish_slot_owned('{row}','lasso','{day}','America/New_York',{capacity},false)")
    current = rows(day, 3, current_limit+1)
    assert all(take(row) for row in current[:current_limit])
    assert take(current[-1]) == ""
    if backlog_limit:
        older = rows("2026-10-02", 5, backlog_limit+1)
        assert all(take(row) for row in older[:backlog_limit])
        assert take(older[-1]) == ""


def test_application_barrier_refuses_other_disposable_database():
    ledger._sql("create database echo_scene_forbidden_test")
    other_dsn = ledger.DSN.replace("dbname=echo_scene_ledger_test", "dbname=echo_scene_forbidden_test")
    done = subprocess.run([ledger.PSQL,"-X","-q","-v","ON_ERROR_STOP=1","-d",other_dsn],
                          input=DRAFT.read_text(), text=True, capture_output=True, timeout=30)
    try:
        assert done.returncode != 0 and "SCRATCH ONLY" in done.stderr
        authority=(ROOT / "migrations/DRAFT_visual_scene_activation_authority_20261006.sql").read_text()
        rejected=subprocess.run([ledger.PSQL,"-X","-q","-v","ON_ERROR_STOP=1","-d",other_dsn],
                                input=authority,text=True,capture_output=True,timeout=30)
        assert rejected.returncode != 0 and "SCRATCH ONLY" in rejected.stderr
        check = command([ledger.PSQL,"-X","-qAt","-d",other_dsn,"-c",
                         "select count(*) from pg_proc where pronamespace='public'::regnamespace"])
        assert check.stdout.strip() == "0"
    finally:
        ledger._sql("drop database echo_scene_forbidden_test")


def test_claim_rebases_authoritative_predicates_without_drift():
    authoritative = (ROOT / "migrations/calendar_approval_provenance_20261005.sql").read_text()
    marker = "create or replace function public.claim_calendar_publish_slot_owned("
    base = authoritative[authoritative.index(marker):].split("\n$$;", 1)[0]
    composed = DRAFT.read_text()[DRAFT.read_text().index(marker):].split("\n$$;", 1)[0]
    # Only the final mutation/result check is changed. This pins autonomy,
    # manual proof, stale reservations, and all dated capacity predicates.
    assert base.split("  v_token := gen_random_uuid();", 1)[0] == composed.split("  v_token := gen_random_uuid();", 1)[0]


def test_scene_authority_missing_row_defaults_off_without_weakening_exact_bytes():
    tenant, group, url, fp, candidate, row = seed(phash=None, scene_on=False)
    assert ledger._one(f"select public.visual_scene_enforcement_on('{tenant}')") == "f"
    assert ledger._one(f"select public.visual_group_enforcement_on('{tenant}')") == "t"
    assert state(row)["media_not_ready_reason"] is None
    assert ledger._occupied_count() == 0 and ledger._holds() == []
    assert ledger._one("select count(*) from public.visual_global_usage") == "1"
    assert claim(row, tenant)
    assert ledger._occupied_count() == 0
    # Exact-byte authority still refuses this same group's different date.
    denied = ledger._run("insert into public.content_calendar(gym_id,account,post_date,status,variant_status,image_url,source_media_url,visual_group_key) values "
              f"('{tenant}','ig','2026-10-12','pending','active','{url}','{url}','{group}')",check=False)
    assert denied.returncode != 0
    assert ledger._occupied_count() == 0


def test_explicit_scene_off_ignores_scene_nearness_and_keeps_proof_gate():
    tenant, group, url, fp, candidate, row = seed(scene_on=False)
    ledger._sql(f"insert into public.gym_visual_scene_guard_settings(gym_id) values ('{tenant}')")
    add_conflict()
    assert ledger._one(f"select public.visual_scene_enforcement_on('{tenant}')") == "f"
    assert ledger._one(f"select count(*) from public.approve_calendar_row_if_media_ready('{row}','{tenant}')") == "1"
    assert ledger._one(f"select public.claim_calendar_publish_slot_proven_owned('{row}','{tenant}','2026-10-10','UTC',2,true)") == ""
    assert proof_stamp(row, tenant) == "1"
    assert ledger._one(f"select public.claim_calendar_publish_slot_proven_owned('{row}','{tenant}','2026-10-10','UTC',2,true)")
    assert state(row)["status"] == "publishing"
    assert ledger._holds() == []
    assert ledger._occupied_count() == 1  # only the independently seeded history


def test_scene_direct_arming_and_guc_arming_are_refused():
    tenant, group = ledger._seed_tenant()
    insert = ledger._run(f"insert into public.gym_visual_scene_guard_settings(gym_id,enforce) values ('{tenant}',true)",check=False)
    assert insert.returncode != 0 and "verified historical occupied backfill" in insert.stderr
    ledger._sql(f"insert into public.gym_visual_scene_guard_settings(gym_id) values ('{tenant}')")
    update = ledger._run(f"set echo.scene_activation='on'; update public.gym_visual_scene_guard_settings set enforce=true where gym_id='{tenant}'",check=False)
    assert update.returncode != 0 and "verified historical occupied backfill" in update.stderr
    assert ledger._one(f"select public.visual_scene_enforcement_on('{tenant}')") == "f"
    assert ledger._run(f"select public.visual_scene_backfill_occupied()",check=False).returncode != 0
    assert ledger._one("select to_regprocedure('public.visual_scene_activate_guard(text,text)') is null") == "t"


def test_scene_settings_and_audit_are_service_read_only():
    tenant, group = ledger._seed_tenant()
    for statement in (
        f"insert into public.gym_visual_scene_guard_settings(gym_id,enforce) values ('{tenant}',true)",
        f"insert into public.visual_scene_disarm_event(gym_id,actor,reason,was_enforced) values ('{tenant}','forged','forged',true)",
    ):
        result=ledger._run("set role service_role; "+statement,check=False)
        assert result.returncode != 0 and "permission denied" in result.stderr
    assert ledger._one("select has_function_privilege('authenticated','public.visual_scene_disarm_guard(text,text,text)','EXECUTE')") == "f"
    assert ledger._one("select has_function_privilege('service_role','public.visual_scene_disarm_guard(text,text,text)','EXECUTE')") == "t"


def test_controlled_disarm_rolls_back_or_commits_without_erasing_hold_evidence():
    tenant, group, url, fp, candidate, row = seed()
    add_conflict()
    assert claim(row, tenant) == ""
    assert_held(row)
    occupied, holds = ledger._occupied_count(), ledger._holds()
    before=ledger._one("select count(*) from public.visual_scene_disarm_event")
    # Raw updates/deletes cannot remove the authority. Nor can invalid reasons.
    for statement in (
        f"update public.gym_visual_scene_guard_settings set enforce=false where gym_id='{tenant}'",
        f"delete from public.gym_visual_scene_guard_settings where gym_id='{tenant}'",
        f"select public.visual_scene_disarm_guard('{tenant}','reviewer','')",
    ):
        assert ledger._run(statement,check=False).returncode != 0
    ledger._sql(f"begin; select public.visual_scene_disarm_guard('{tenant}','reviewer','rollback rehearsal'); rollback;")
    assert ledger._one(f"select public.visual_scene_enforcement_on('{tenant}')") == "t"
    assert ledger._one("select count(*) from public.visual_scene_disarm_event") == before
    ledger._sql(f"set role service_role; select public.visual_scene_disarm_guard('{tenant}','reviewer','stop scene admission')")
    assert ledger._one(f"select public.visual_scene_enforcement_on('{tenant}')") == "f"
    assert ledger._one(f"select public.visual_group_enforcement_on('{tenant}')") == "t"
    assert ledger._occupied_count()==occupied and ledger._holds()==holds
    assert_held(row)
    assert claim(row, tenant) == ""
    assert ledger._one(f"select actor||':'||reason from public.visual_scene_disarm_event where gym_id='{tenant}'") == "reviewer:stop scene admission"
    for statement in (
        f"delete from public.visual_scene_disarm_event where gym_id='{tenant}'",
        "truncate public.visual_scene_disarm_event",
        "truncate public.gym_visual_scene_guard_settings",
    ):
        assert ledger._run(statement,check=False).returncode != 0


def test_scene_authority_alias_and_off_destination_cannot_bypass_on_guard():
    tenant, group, url, fp, candidate, row = seed(active=False)
    other, other_group=ledger._seed_tenant()
    result=ledger._run(f"update public.content_calendar set gym_id='{other}' where id='{row}'",check=False)
    assert result.returncode != 0 and "scene-OFF tenant" in result.stderr
    result=ledger._run(f"insert into public.gym_visual_scene_guard_settings(gym_id) values ('unmapped')",check=False)
    assert result.returncode != 0 and "canonical mapped tenant" in result.stderr


@pytest.mark.parametrize("busy_table", ["content_calendar", "gym_visual_scene_guard_settings", "settings_row"])
def test_scene_disarm_refuses_contended_writer_without_partial_audit(busy_table):
    tenant, group, url, fp, candidate, row = seed()
    before=ledger._one("select count(*) from public.visual_scene_disarm_event")
    marker="scene_disarm_writer_ready"
    proc=subprocess.Popen([ledger.PSQL,"-X","-q","-A","-t","-v","ON_ERROR_STOP=1","-d",ledger.DSN],
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    lock=(f"select enforce from public.gym_visual_scene_guard_settings where gym_id='{tenant}' for share"
          if busy_table=="settings_row" else f"lock table public.{busy_table} in row exclusive mode")
    proc.stdin.write(f"begin; {lock}; select '{marker}';\n")
    proc.stdin.flush()
    try:
        while marker not in proc.stdout.readline():
            assert proc.poll() is None
        result=ledger._run(f"select public.visual_scene_disarm_guard('{tenant}','reviewer','contention test')",check=False)
        assert result.returncode != 0 and "could not obtain lock" in result.stderr
        assert ledger._one(f"select public.visual_scene_enforcement_on('{tenant}')") == "t"
        assert ledger._one("select count(*) from public.visual_scene_disarm_event") == before
        assert state(row)["status"]=="pending"
    finally:
        proc.stdin.write("rollback;\n\\q\n")
        proc.stdin.flush()
        proc.wait(timeout=20)
        assert proc.returncode==0


def test_scene_disarm_preserves_published_original_token_and_both_ledgers():
    tenant, group, url, fp, candidate, row=seed()
    token=claim(row,tenant)
    # Synthetic terminal-provider fixture through the real reconciliation RPC;
    # never loosen finalization or create a real-provider receipt outside scratch.
    ledger._sql(f"select public.visual_group_reconcile_ambiguous('{tenant}','{row}','confirmed_published','{group}','2026-10-10',"
                "jsonb_build_object('source','provider_terminal_readback',"
                f"'gym_id','{tenant}','calendar_row_id','{row}','group_key','{group}',"
                "'calendar_date','2026-10-10','provider','synthetic-local-test',"
                "'request_id','synthetic-request','receipt_ref','synthetic-test-only',"
                "'terminal',true,'will_retry',false,'delivery','delivered','provider_status','published',"
                f"'provider_post_id','synthetic-post','delivered_url','{url}',"
                "'checked_at',now(),'published_at',now(),'hold_claims','[]'::jsonb,"
                "'claims',(select coalesce(jsonb_agg(jsonb_build_object('group_key',s.group_key,"
                "'attempt_id',s.attempt_id::text,'claim_token',s.original_claim_token::text,"
                "'provider_post_id',s.original_provider_post_id,'image_url',s.original_image_url) order by s.group_key),'[]'::jsonb) "
                f"from public.visual_group_usage_sibling s where s.gym_id='{tenant}' and s.calendar_row_id='{row}' and s.ambiguous)),"
                "'synthetic-test-reviewer')")
    before=state(row)
    occupied=ledger._occupied_count()
    exact=ledger._one("select count(*) from public.visual_global_usage")
    ledger._sql(f"select public.visual_scene_disarm_guard('{tenant}','reviewer','rollback publish guard')")
    assert state(row)==before and state(row)["publish_claim_token"]==token
    assert ledger._occupied_count()==occupied
    assert ledger._one("select count(*) from public.visual_global_usage")==exact
    assert ledger._one(f"select public.visual_group_enforcement_on('{tenant}')")=="t"


def test_scene_on_refuses_loss_of_exact_byte_authority():
    tenant, group, url, fp, candidate, row=seed()
    ledger._sql(f"update public.gym_visual_guard_settings set enforce=false where gym_id='{tenant}'")
    result=ledger._run(f"update public.content_calendar set caption='another caption' where id='{row}'",check=False)
    assert result.returncode != 0 and "requires exact-byte authority" in result.stderr
    assert state(row)["caption"] is None
