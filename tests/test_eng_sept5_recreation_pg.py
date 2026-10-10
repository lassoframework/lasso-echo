"""Real PG17 contract tests for the draft, one-ticket ENG repair authority.

The forward finalizer is a deliberately narrow fixture here. Its own visual,
approval, and slot authority has separate PG coverage in the forward stack.
"""
from __future__ import annotations

import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/DRAFT_eng_sept5_exact_recreation.sql"
TICKET = "35e066d0-d9bc-40e6-aef8-86719a010590"
LOGICAL = "6d21c00f-349c-5a06-9328-5a570a81babb"
ORIGINALS = (
    ("2ad9f097-e7cc-49a2-b348-30c8e1cda80d", "facebook", "feed"),
    ("b1bb4d63-fda7-4b1e-9303-dff3483f4387", "instagram", "feed"),
    ("ff792b3e-be08-4c4e-b0f5-0347e075a9ba", "instagram", "story"),
)


def _psql(db: str, sql: str, *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["psql", "-X", "-h", "/tmp", "-p", "5432", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        text=True, capture_output=True,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


SCHEMA = f"""
create table public.support_tickets (
 id uuid primary key, client_id uuid, product text, source text,
 request_version integer, status text, resolved_at timestamptz
);
create table public.support_messages (
 id uuid primary key, ticket_id uuid references public.support_tickets(id),
 direction text, body text, created_at timestamptz default now()
);
create table public.content_calendar (
 id uuid primary key, gym_id text, account text, format text, post_date date,
 status text, variant_status text, logical_post_id uuid,
 media_not_ready_reason text, source_media_asset_id text,
 source_media_url text, caption text,
 approved_at timestamptz, approved_by text, published_at timestamptz,
 late_post_id text, publish_claim_token uuid
);
create table public.forward_schedule_stage_batch_20261008 (
 batch_id uuid primary key, state text, tenant_id text
);
create table public.forward_schedule_stage_member_20261008 (
 batch_id uuid, calendar_row_id uuid, observation_digest text
);
create table public.forward_schedule_stage_old_row_20261008 (
 batch_id uuid, calendar_row_id uuid
);
create function public.finalize_forward_schedule_staged_batch_20261008(
 p_tenant_id text,p_batch_id uuid,p_candidates jsonb,p_expected_old_rows jsonb
) returns jsonb language plpgsql as $$
declare ids jsonb;
begin
 if p_tenant_id<>'eng' or p_expected_old_rows<>'[]'::jsonb then
  raise exception 'fixture finalizer refused'; end if;
 if exists(select 1 from public.content_calendar c
   where c.post_date=(select post_date from public.content_calendar
     where id=(p_candidates->0->>'calendar_row_id')::uuid)
     and c.variant_status='active' and c.status='pending'
     and c.account='instagram' and c.format='feed') then
  raise exception 'slot occupied'; end if;
 update public.content_calendar set variant_status='active',media_not_ready_reason=null
 where id in (select (x->>'calendar_row_id')::uuid from jsonb_array_elements(p_candidates) x);
 update public.forward_schedule_stage_batch_20261008 set state='finalized' where batch_id=p_batch_id;
 select jsonb_agg(x->>'calendar_row_id') into ids from jsonb_array_elements(p_candidates) x;
 return jsonb_build_object('state','finalized','batch_id',p_batch_id,'tenant_id','eng',
   'row_ids',ids,'archived_old_row_ids','[]'::jsonb);
end;
$$;
insert into public.support_tickets values
 ('{TICKET}','6ee04ee4-13a5-47db-8416-7b8ee3e61ab8','echo','website_tab',0,'resolved',null);
insert into public.support_messages(id,ticket_id,direction,body) values
 ('18ea0770-e978-4a56-a85e-bdf720671d97','{TICKET}','inbound','Denied post did not recreate');
""" + "\n".join(
    "insert into public.content_calendar(id,gym_id,account,format,post_date,status,"
    "variant_status,source_media_asset_id) values "
    f"('{row_id}','eng','{account}','{fmt}','2026-09-04','denied','active',"
    "'1BNl4pJqTFF21JYk-RIDS9-YHz9YYXLKt');"
    for row_id, account, fmt in ORIGINALS
)


@pytest.fixture
def pg():
    db = "eng_sep5_" + uuid.uuid4().hex[:16]
    _psql("postgres", f'create database "{db}"')
    try:
        _psql(db, SCHEMA)
        _psql(db, MIGRATION.read_text())
        yield db
    finally:
        _psql("postgres", f'drop database if exists "{db}" with (force)')


def _gate(db: str) -> None:
    _psql(db, "update public.eng_sept5_recreation_gate set enabled=true")


def _begin(db: str, days: int = 2, *, check: bool = True):
    return _psql(db, f"select state from public.eng_sept5_recreation_begin(current_date+{days})",
                 check=check)


def _stage(db: str, batch: str | None = None) -> tuple[str, list[str]]:
    batch = batch or str(uuid.uuid4())
    rows = [str(uuid.uuid4()) for _ in range(3)]
    _psql(db, f"insert into public.forward_schedule_stage_batch_20261008 values ('{batch}','staged','eng')")
    for row_id, (_, account, fmt) in zip(rows, ORIGINALS):
        _psql(db, "insert into public.content_calendar(id,gym_id,account,format,post_date,"
              "status,variant_status,logical_post_id,media_not_ready_reason,"
              "source_media_asset_id,source_media_url,caption) values "
              f"('{row_id}','eng','{account}','{fmt}',current_date+2,'pending','candidate',"
              f"'{LOGICAL}','forward_reservation_staged','new-asset',"
              "'https://example.test/new.jpg','New approved copy')")
        _psql(db, "insert into public.forward_schedule_stage_member_20261008 values "
              f"('{batch}','{row_id}','{'a' * 64}')")
    return batch, rows


def _candidate_json(rows: list[str]) -> str:
    return "'[" + ",".join(f'{{"calendar_row_id":"{row_id}"}}' for row_id in rows) + "]'::jsonb"


def test_admission_off_and_only_future_date(pg):
    assert _begin(pg, check=False).returncode != 0
    _gate(pg)
    assert _begin(pg, 0, check=False).returncode != 0
    assert _begin(pg, 32, check=False).returncode != 0
    assert _begin(pg).stdout.strip() == "reserved"


def test_exact_begin_replay_and_different_target_refused(pg):
    _gate(pg)
    assert _begin(pg).stdout.strip() == "reserved"
    assert _begin(pg).stdout.strip() == "reserved"
    assert _begin(pg, 3, check=False).returncode != 0
    assert _psql(pg, "select count(*) from public.eng_sept5_recreation_receipt").stdout.strip() == "1"


def test_concurrent_targets_one_durable_reservation(pg):
    _gate(pg)
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda day: _begin(pg, day, check=False), (2, 3)))
    assert sorted(result.returncode == 0 for result in outcomes) == [False, True]
    assert _psql(pg, "select count(*) from public.eng_sept5_recreation_receipt").stdout.strip() == "1"


@pytest.mark.parametrize("mutation", [
    "update public.support_tickets set request_version=1",
    f"insert into public.support_messages(id,ticket_id,direction,body) values ('{uuid.uuid4()}', '{TICKET}', 'inbound','new request')",
    f"update public.content_calendar set status='pending' where id='{ORIGINALS[0][0]}'",
])
def test_stale_ticket_inbound_or_original_refuses_begin(pg, mutation):
    _gate(pg)
    _psql(pg, mutation)
    assert _begin(pg, check=False).returncode != 0
    assert _psql(pg, "select count(*) from public.eng_sept5_recreation_receipt").stdout.strip() == "0"


def test_exact_stage_bind_and_atomic_finalization_replay(pg):
    _gate(pg)
    _begin(pg)
    batch, rows = _stage(pg)
    assert _psql(pg, f"select state from public.eng_sept5_recreation_bind('{batch}')").stdout.strip() == "staged"
    candidates = _candidate_json(rows)
    assert _psql(pg, f"select state from public.eng_sept5_recreation_finalize('{batch}',{candidates})").stdout.strip() == "finalized"
    assert _psql(pg, f"select state from public.eng_sept5_recreation_finalize('{batch}',{candidates})").stdout.strip() == "finalized"
    assert _psql(pg, "select count(*) from public.content_calendar where logical_post_id='"
                 + LOGICAL + "' and variant_status='active' and status='pending'").stdout.strip() == "3"
    assert _psql(pg, "select count(*) from public.content_calendar where status='denied'").stdout.strip() == "3"


def test_stale_request_or_concurrent_slot_refuses_finalization(pg):
    _gate(pg)
    _begin(pg)
    batch, rows = _stage(pg)
    _psql(pg, f"select public.eng_sept5_recreation_bind('{batch}')")
    _psql(pg, f"update public.support_tickets set status='working' where id='{TICKET}'")
    assert _psql(pg, f"select public.eng_sept5_recreation_finalize('{batch}',{_candidate_json(rows)})",
                 check=False).returncode != 0
    assert _psql(pg, "select state from public.eng_sept5_recreation_receipt").stdout.strip() == "staged"


def test_concurrent_planner_slot_refuses_and_preserves_staged_rows(pg):
    _gate(pg)
    _begin(pg)
    batch, rows = _stage(pg)
    _psql(pg, f"select public.eng_sept5_recreation_bind('{batch}')")
    _psql(pg, "insert into public.content_calendar(id,gym_id,account,format,post_date,"
              "status,variant_status) values "
              f"('{uuid.uuid4()}','eng','instagram','feed',current_date+2,'pending','active')")
    assert _psql(pg, f"select public.eng_sept5_recreation_finalize('{batch}',{_candidate_json(rows)})",
                 check=False).returncode != 0
    assert _psql(pg, "select state from public.eng_sept5_recreation_receipt").stdout.strip() == "staged"
    assert _psql(pg, "select count(*) from public.content_calendar where logical_post_id='"
                 + LOGICAL + "' and variant_status='candidate'").stdout.strip() == "3"


def test_bind_refuses_extra_or_wrong_candidate(pg):
    _gate(pg)
    _begin(pg)
    batch, rows = _stage(pg)
    _psql(pg, f"update public.content_calendar set account='googlebusiness' where id='{rows[0]}'")
    assert _psql(pg, f"select public.eng_sept5_recreation_bind('{batch}')", check=False).returncode != 0


def test_service_role_only_and_default_off(pg):
    for role in ("anon", "authenticated"):
        assert _psql(pg, f"select has_function_privilege('{role}',"
                     "'public.eng_sept5_recreation_begin(date)','execute')").stdout.strip() == "f"
    assert _psql(pg, "select enabled from public.eng_sept5_recreation_gate").stdout.strip() == "f"
