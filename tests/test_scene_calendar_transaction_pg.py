"""DRAFT transaction wiring acceptance on a fresh, private local PG17 cluster.

No supplied DSN is accepted. Missing PG17/pytest dependencies FAIL this suite;
no skip can be mistaken for acceptance. Existing scene-ledger fixture helpers
are reused for exact-byte attestation setup. Every server is stopped on exit.
Unknown historical coverage/backfill and cloud Ultra Review remain release gates.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scene_fixture", ROOT / "tests/test_scene_ledger_claim_pg.py")
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)
BIN = Path("/opt/homebrew/opt/postgresql@17/bin")
DRAFT = ROOT / "migrations/DRAFT_visual_scene_calendar_transaction_20261005.sql"


def command(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=180, **kwargs)


@pytest.fixture(scope="module", autouse=True)
def cluster():
    assert (BIN / "postgres").exists(), "PG17 missing; fail closed, no acceptance"
    assert "17." in command([str(BIN / "postgres"), "--version"]).stdout
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
            ledger._sql("alter table public.content_calendar add column format text")
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
