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


def seed(phash="0000000000000000", account="ig", active=True):
    tenant, group = ledger._seed_tenant()
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
