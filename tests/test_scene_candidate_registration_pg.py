"""Real PostgreSQL checks for the atomic candidate/calendar draft RPC.

These tests require a task-owned, disposable Unix-socket database named
``echo_scene_atomic_write_20261004``. They never connect to a network host and
skip unless VISUAL_SCENE_ATOMIC_TEST_DSN is explicitly provided.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import visual_scene_register as vsr


DSN = os.environ.get("VISUAL_SCENE_ATOMIC_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
ATOMIC = MIGRATIONS / "DRAFT_visual_scene_calendar_atomic_write_20261004.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_calendar_atomic_write_20261004.sql",
]

pytestmark = pytest.mark.skipif(
    not DSN or not ATOMIC.exists() or not PSQL,
    reason="atomic scene checks need the named disposable local PostgreSQL DB",
)


def _run(statement, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", statement], text=True, capture_output=True, timeout=90)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _sql(statement):
    return _run(statement).stdout.strip()


def _one(statement):
    out = _sql(statement)
    return out.splitlines()[-1] if out else ""


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture(scope="module", autouse=True)
def _scratch_stack():
    if not DSN or not ATOMIC.exists():
        pytest.skip("atomic draft migration or disposable DSN unavailable")
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ "
        r"dbname=echo_scene_atomic_write_20261004(?: user=[A-Za-z0-9_-]+)?",
        DSN,
    ), "only the task-owned Unix-socket scratch DB is allowed"
    assert _one("select current_database()") == "echo_scene_atomic_write_20261004"
    _sql("drop schema public cascade; create schema public; "
         "grant usage on schema public to public")
    _sql("do $$ begin "
         "if not exists (select 1 from pg_roles where rolname='anon') then "
         "create role anon nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='authenticated') then "
         "create role authenticated nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='service_role') then "
         "create role service_role nologin; end if; end $$")
    _sql("create table public.content_calendar ("
         "id uuid primary key default gen_random_uuid(),"
         "gym_id text, account text, post_date date, format text, caption text,"
         "status text, variant_status text default 'active',"
         "published_at timestamptz, publish_claim_token uuid,"
         "publish_reservation_day date, late_post_id text,"
         "image_url text, thumbnail_url text, source_media_url text,"
         "source_media_asset_id text, drive_file_id text, byte_hash text,"
         "r2_key text, media_not_ready_reason text)")
    _sql("create table public.media_asset ("
         "id text primary key, gym_id text, content_hash text)")
    _sql("create or replace function public.visual_group_row_active(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    _sql("create or replace function public.visual_group_row_ambiguous(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    script = "".join((MIGRATIONS / name).read_text() + "\n" for name in STACK)
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=240)
    assert done.returncode == 0, done.stderr


@pytest.fixture(autouse=True)
def _isolate_scenarios():
    _sql("drop trigger if exists zz_atomic_mutate on public.content_calendar; "
         "drop function if exists public._atomic_mutate_after_guard(); "
         "drop trigger if exists aa_atomic_concurrency_pause on public.content_calendar; "
         "drop function if exists public._atomic_concurrency_pause()")
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold, "
         "public.visual_scene_phash_occupied, public.visual_scene_candidate, "
         "public.visual_global_usage, public.visual_global_usage_member, "
         "public.visual_group_usage_ledger, public.visual_group_usage_sibling, "
         "public.gym_visual_guard_settings, public.visual_group_activation cascade")
    _sql("drop function if exists public._try_atomic_scene_write()")


def _seed_tenant(*, activate=False):
    tenant = str(uuid.uuid4())
    alias = "alias_" + uuid.uuid4().hex[:12]
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql("insert into public.tenant_alias(alias_key, tenant_id) values "
         f"({_lit(tenant)}, {_lit(tenant)}), ({_lit(alias)}, {_lit(tenant)})")
    _sql("insert into public.visual_group(gym_id, group_key) values "
         f"({_lit(tenant)}, {_lit(group)})")
    if activate:
        _sql("select public.visual_group_activate_guard("
             f"{_lit(tenant)}, 'atomic-writer-test')")
    return tenant, alias, group


def _seed_attestation(tenant, group, url, fingerprint):
    receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"({_lit(tenant)}, {_lit(url)}, {_lit(fingerprint)}, 1024,"
        " 'verified_object_read', 'atomic-test', 'atomic-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"({_lit(url)}, {_lit(tenant)}, {_lit(group)}, {_lit(fingerprint)}, 1024,"
         f" 'verified_object_read', 'atomic-test', {_lit(receipt)}, 'atomic-test')")
    for role in ("source", "delivered"):
        _sql("insert into public.visual_global_scene_object_member"
             "(tenant_id, group_key, exact_url, fingerprint, object_role) values "
             f"({_lit(tenant)}, {_lit(group)}, {_lit(url)}, {_lit(fingerprint)}, "
             f"{_lit(role)})")
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"({_lit(tenant)}, 'canonical_url', {_lit(url)}, {_lit(group)}), "
         f"({_lit(tenant)}, 'byte_hash', {_lit('derived:' + fingerprint)}, {_lit(group)})")


def _seed_lineage(tenant, group, source_url, source_fingerprint,
                  delivered_url, delivered_fingerprint):
    source_receipt = _one(
        "select receipt_id from public.visual_global_object_read_receipt where "
        f"tenant_id={_lit(tenant)} and exact_url={_lit(source_url)} and "
        f"fingerprint={_lit(source_fingerprint)}")
    delivered_receipt = _one(
        "select receipt_id from public.visual_global_object_read_receipt where "
        f"tenant_id={_lit(tenant)} and exact_url={_lit(delivered_url)} and "
        f"fingerprint={_lit(delivered_fingerprint)}")
    render_receipt = _one(
        "insert into public.visual_global_render_receipt"
        "(tenant_id,source_read_receipt,delivered_read_receipt,source_exact_url,"
        "delivered_exact_url,source_fingerprint,delivered_fingerprint,operation,"
        "evidence_ref,rendered_by) values "
        f"({_lit(tenant)}, {_lit(source_receipt)}::uuid, "
        f"{_lit(delivered_receipt)}::uuid, {_lit(source_url)}, "
        f"{_lit(delivered_url)}, {_lit(source_fingerprint)}, "
        f"{_lit(delivered_fingerprint)}, 'render', 'atomic-test', 'atomic-test') "
        "returning receipt_id")
    _sql("insert into public.visual_global_object_lineage"
         "(tenant_id,group_key,source_exact_url,delivered_exact_url,"
         "source_fingerprint,delivered_fingerprint,render_receipt) values "
         f"({_lit(tenant)}, {_lit(group)}, {_lit(source_url)}, "
         f"{_lit(delivered_url)}, {_lit(source_fingerprint)}, "
         f"{_lit(delivered_fingerprint)}, {_lit(render_receipt)}::uuid)")


def _candidate(tenant_key, group, role, url, phash, fingerprint):
    payload_role = "poster" if role == "poster" else "delivered"
    return {
        "kind": "visual_scene_candidate", "stage": "candidate",
        "tenant_id": tenant_key, "group_key": group,
        "usage_claimed": False, "counts_as_use": False,
        "observed_by": "visual_writer_prepare", "evidence_ref": "atomic-test",
        "objects": [{"role": payload_role, "phash": phash, "exact_url": url,
                     "fingerprint": fingerprint, "byte_length": 1024,
                     "scene_fingerprint": f"scene:phash64:{phash}",
                     "stageable": True}],
    }


def _item(alias, tenant_key, group, *, image, phash, fingerprint,
          thumbnail=None, row_id=None):
    row = {"gym_id": alias, "account": "ig", "post_date": "2026-10-04",
           "format": "feed", "caption": "Atomic", "status": "pending",
           "variant_status": "active", "image_url": image,
           "source_media_url": image,
           "byte_hash": "derived:" + fingerprint,
           "visual_group_key": group}
    if thumbnail is not None:
        row["thumbnail_url"] = thumbnail
    if row_id is not None:
        row["id"] = row_id
    role, exact_url = vsr.row_delivered_object(row)
    candidate = _candidate(tenant_key, group, role, exact_url, phash, fingerprint)
    return vsr.build_items([row], [candidate])[0]


def _rpc(alias, items):
    raw = _one("select public.visual_scene_insert_calendar_batch("
               f"{_lit(alias)}, {_lit(json.dumps(items))}::jsonb, 'atomic-test')")
    return json.loads(raw)


def _attempt(alias, items):
    call = ("public.visual_scene_insert_calendar_batch("
            f"{_lit(alias)}, {_lit(json.dumps(items))}::jsonb, 'atomic-test')")
    _sql("create or replace function public._try_atomic_scene_write() returns text "
         "language plpgsql as $f$ begin perform " + call +
         "; return '00000'; exception when others then return SQLSTATE; end $f$")
    return _one("select public._try_atomic_scene_write()")


def _count(table):
    return int(_one(f"select count(*) from public.{table}"))


def test_alias_candidate_is_staged_before_insert_and_staging_is_not_use():
    tenant, alias, group = _seed_tenant(activate=False)
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    # Candidate tenant may itself arrive through an alias; the RPC canonicalizes
    # it and requires it to equal the row/account alias's canonical tenant.
    item = _item(alias, alias, group, image=url, phash="0123456789abcdef",
                 fingerprint=fingerprint)

    (result,) = _rpc(alias, [item])

    assert result["disposition"] == "inserted"
    assert result["row"]["gym_id"] == alias
    assert result["row"]["status"] == "pending"
    assert _count("visual_scene_candidate") == 1
    assert _count("visual_scene_phash_occupied") == 0
    evidence = json.loads(_one(
        "select evidence::text from public.visual_scene_candidate limit 1"))
    assert evidence["verified_bytes"] == fingerprint
    assert evidence["exact_url"] == url
    assert evidence["object_role"] == "display"
    assert evidence["calendar_row_id"] == result["row"]["id"]
    assert evidence["first_calendar_row_id"] == result["row"]["id"]
    assert evidence["registration_provenance"] == "first_atomic_calendar_registration"
    assert result["candidate_binding"] == {
        "candidate_id": result["candidate_id"],
        "registration_reused": False,
        "tenant_id": tenant,
        "group_key": group,
        "object_role": "display",
        "exact_url": url,
        "phash": "0123456789abcdef",
        "fingerprint": fingerprint,
    }


def test_armed_insert_trigger_sees_pre_staged_candidate_and_claims_clean():
    tenant, alias, group = _seed_tenant(activate=True)
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    item = _item(alias, tenant, group, image=url, phash="1111222233334444",
                 fingerprint=fingerprint)

    (result,) = _rpc(alias, [item])

    assert result["disposition"] == "inserted"
    assert _count("content_calendar") == 1
    assert _count("visual_scene_candidate") == 1
    assert _count("visual_scene_phash_occupied") == 1


def test_same_date_channel_siblings_reuse_one_stable_candidate_in_batch():
    tenant, alias, group = _seed_tenant(activate=True)
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    first = _item(alias, tenant, group, image=url, phash="0101010101010101",
                  fingerprint=fingerprint)
    second = _item(alias, tenant, group, image=url, phash="0101010101010101",
                   fingerprint=fingerprint)
    second["calendar_row"]["account"] = "gbp"

    results = _rpc(alias, [first, second])

    assert len(results) == 2
    assert {result["candidate_id"] for result in results} == {
        results[0]["candidate_id"]}
    assert all(result["candidate_binding"]["candidate_id"] == results[0]["candidate_id"]
               for result in results)
    assert [result["candidate_binding"]["registration_reused"] for result in results] == [
        False, True]
    assert all(result["disposition"] == "inserted" for result in results)
    assert _count("content_calendar") == 2
    assert _count("visual_scene_candidate") == 1
    assert _count("visual_scene_phash_occupied") == 1
    assert _count("visual_scene_review_hold") == 0


def test_later_atomic_rpc_retry_reuses_stable_candidate_and_occupancy():
    tenant, alias, group = _seed_tenant(activate=True)
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    first_item = _item(alias, tenant, group, image=url,
                       phash="0202020202020202", fingerprint=fingerprint)
    (first,) = _rpc(alias, [first_item])
    retry_item = _item(alias, tenant, group, image=url,
                       phash="0202020202020202", fingerprint=fingerprint)
    retry_item["calendar_row"]["account"] = "facebook"

    (retry,) = _rpc(alias, [retry_item])

    assert retry["candidate_id"] == first["candidate_id"]
    assert retry["candidate_binding"]["candidate_id"] == first["candidate_binding"]["candidate_id"]
    assert first["candidate_binding"]["registration_reused"] is False
    assert retry["candidate_binding"]["registration_reused"] is True
    assert retry["disposition"] == "inserted"
    assert _count("content_calendar") == 2
    assert _count("visual_scene_candidate") == 1
    assert _count("visual_scene_phash_occupied") == 1
    assert _count("visual_scene_review_hold") == 0


def test_concurrent_identical_atomic_writes_converge_on_stable_candidate():
    tenant, alias, group = _seed_tenant(activate=True)
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    item_a = _item(alias, tenant, group, image=url, phash="0303030303030303",
                   fingerprint=fingerprint)
    item_b = _item(alias, tenant, group, image=url, phash="0303030303030303",
                   fingerprint=fingerprint)
    item_b["calendar_row"]["account"] = "gbp"
    _sql("create function public._atomic_concurrency_pause() returns trigger "
         "language plpgsql as $$ begin perform pg_sleep(0.75); return new; end $$; "
         "create trigger aa_atomic_concurrency_pause before insert "
         "on public.content_calendar for each row execute function "
         "public._atomic_concurrency_pause()")

    def start(item):
        statement = ("select public.visual_scene_insert_calendar_batch("
                     f"{_lit(alias)}, {_lit(json.dumps([item]))}::jsonb, "
                     "'atomic-concurrency-test')")
        return subprocess.Popen(
            [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-d", DSN, "-c", statement], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    processes = [start(item_a), start(item_b)]
    completed = [process.communicate(timeout=90) for process in processes]
    assert all(process.returncode == 0 for process in processes), completed
    results = [json.loads(stdout.strip().splitlines()[-1])[0]
               for stdout, _ in completed]

    assert results[0]["candidate_id"] == results[1]["candidate_id"]
    assert all(result["candidate_binding"]["candidate_id"] == results[0]["candidate_id"]
               for result in results)
    assert sorted(result["candidate_binding"]["registration_reused"] for result in results) == [
        False, True]
    assert all(result["disposition"] == "inserted" for result in results)
    assert _count("content_calendar") == 2
    assert _count("visual_scene_candidate") == 1
    assert _count("visual_scene_phash_occupied") == 1
    assert _count("visual_scene_review_hold") == 0


def test_armed_video_row_binds_exact_poster_candidate_to_attested_source_group():
    tenant, alias, group = _seed_tenant(activate=True)
    video = f"https://scratch.example/{uuid.uuid4().hex}.mp4"
    poster = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    video_fingerprint = "md5:" + uuid.uuid4().hex
    poster_fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, video, video_fingerprint)
    _seed_attestation(tenant, group, poster, poster_fingerprint)
    _seed_lineage(tenant, group, video, video_fingerprint,
                  poster, poster_fingerprint)
    item = _item(alias, tenant, group, image=video, thumbnail=poster,
                 phash="abcdef0123456789", fingerprint=poster_fingerprint)
    item["calendar_row"]["byte_hash"] = "derived:" + video_fingerprint

    (result,) = _rpc(alias, [item])

    bound = _one("select object_role || '|' || exact_url || '|' || fingerprint "
                 "from public.visual_scene_candidate")
    assert bound == f"poster|{poster}|{poster_fingerprint}"
    assert result["candidate_binding"]["candidate_id"] == result["candidate_id"]
    assert result["candidate_binding"]["object_role"] == "poster"
    assert result["candidate_binding"]["exact_url"] == poster
    assert result["row"]["image_url"] == video
    assert result["row"]["thumbnail_url"] == poster
    assert _count("visual_scene_phash_occupied") == 1


def test_post_trigger_media_mutation_fails_candidate_rebind_and_rolls_back():
    tenant, alias, group = _seed_tenant()
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    item = _item(alias, tenant, group, image=url, phash="1234567890abcdef",
                 fingerprint=fingerprint)
    _sql("create function public._atomic_mutate_after_guard() returns trigger "
         "language plpgsql as $$ begin new.image_url := "
         "'https://scratch.example/changed-after-guard.jpg'; return new; end $$; "
         "create trigger zz_atomic_mutate before insert on public.content_calendar "
         "for each row execute function public._atomic_mutate_after_guard()")

    assert _attempt(alias, [item]) == "23514"
    assert _count("content_calendar") == 0
    assert _count("visual_scene_candidate") == 0


def test_second_candidate_failure_rolls_back_first_candidate_and_row():
    tenant, alias, group = _seed_tenant()
    url1 = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    url2 = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fp1 = "md5:" + uuid.uuid4().hex
    fp2 = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url1, fp1)
    _seed_attestation(tenant, group, url2, fp2)
    first = _item(alias, tenant, group, image=url1, phash="0000000000000000",
                  fingerprint=fp1)
    # Exact URL is attested only to fp2; this candidate's different md5 fails.
    second = _item(alias, tenant, group, image=url2, phash="aaaaaaaaaaaaaaaa",
                   fingerprint="md5:" + uuid.uuid4().hex)

    assert _attempt(alias, [first, second]) == "23514"
    assert _count("content_calendar") == 0
    assert _count("visual_scene_candidate") == 0
    assert _count("visual_scene_phash_occupied") == 0


def test_second_row_failure_rolls_back_both_candidates_and_first_row():
    tenant, alias, group = _seed_tenant()
    url1 = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    url2 = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fp1 = "md5:" + uuid.uuid4().hex
    fp2 = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url1, fp1)
    _seed_attestation(tenant, group, url2, fp2)
    duplicate_id = str(uuid.uuid4())
    first = _item(alias, tenant, group, image=url1, phash="0000000000000000",
                  fingerprint=fp1, row_id=duplicate_id)
    second = _item(alias, tenant, group, image=url2, phash="aaaaaaaaaaaaaaaa",
                   fingerprint=fp2, row_id=duplicate_id)

    assert _attempt(alias, [first, second]) == "23505"
    assert _count("content_calendar") == 0
    assert _count("visual_scene_candidate") == 0


def test_unknown_dynamic_column_is_rejected_and_candidate_rolls_back():
    tenant, alias, group = _seed_tenant()
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    item = _item(alias, tenant, group, image=url, phash="3333444455556666",
                 fingerprint=fingerprint)
    item["calendar_row"]["id) values (gen_random_uuid()); select 1 --"] = "x"

    assert _attempt(alias, [item]) == "42703"
    assert _count("content_calendar") == 0
    assert _count("visual_scene_candidate") == 0


def test_near_conflict_returns_persisted_hold_disposition_not_ready():
    tenant_a, alias_a, group_a = _seed_tenant(activate=True)
    url_a = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fp_a = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant_a, group_a, url_a, fp_a)
    _rpc(alias_a, [_item(alias_a, tenant_a, group_a, image=url_a,
                         phash="0000000000000000", fingerprint=fp_a)])

    tenant_b, alias_b, group_b = _seed_tenant(activate=True)
    url_b = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fp_b = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant_b, group_b, url_b, fp_b)
    item = _item(alias_b, tenant_b, group_b, image=url_b,
                 phash="0000000000000003", fingerprint=fp_b)

    (result,) = _rpc(alias_b, [item])

    assert result["disposition"] == "scene_review_hold"
    assert result["row"]["status"] == "pending"
    assert result["row"]["variant_status"] == "archived"
    assert result["row"]["media_not_ready_reason"] == "scene_review_hold"
    assert result["row"]["publish_claim_token"] is None
    assert result["row"]["publish_reservation_day"] is None
    assert _count("visual_scene_review_hold") == 1
    assert _one("select count(*) from public.visual_scene_phash_occupied "
                f"where tenant_id={_lit(tenant_b)}") == "0"


def test_static_security_and_order_contract():
    sql = ATOMIC.read_text()
    register = sql.index("v_candidate_id := public.visual_scene_register_candidate(")
    insert = sql.index("insert into public.content_calendar as cc")
    assert register < insert
    assert "security definer" in sql.lower()
    assert "set search_path = public, pg_temp" in sql
    assert "grant execute on function public.visual_scene_insert_calendar_batch" in sql
    assert "visual_scene_candidate_canonical_binding_uq" in sql
    assert ("revoke execute on function public.visual_scene_register_candidate(" in sql)
    assert "unsafe direct service-role scene registration is enabled" in sql
    assert "duplicate candidate bindings require review" in sql
    assert "visual_scene_calendar_install_receipt" in sql
    assert "exists with wrong catalog shape" in sql
    assert sql.count("i.indisvalid and i.indisready") == 2
    assert "delete from public.content_calendar" not in sql.lower()


def test_runtime_acl_denies_untrusted_roles_and_allows_service_role():
    tenant, alias, group = _seed_tenant()
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _seed_attestation(tenant, group, url, fingerprint)
    item = _item(alias, tenant, group, image=url, phash="fedcba9876543210",
                 fingerprint=fingerprint)
    call = ("public.visual_scene_insert_calendar_batch("
            f"{_lit(alias)}, {_lit(json.dumps([item]))}::jsonb, 'acl-test')")

    assert _one("select has_function_privilege('anon', "
                "'public.visual_scene_insert_calendar_batch(text,jsonb,text)', "
                "'execute')") == "f"
    assert _one("select has_function_privilege('authenticated', "
                "'public.visual_scene_insert_calendar_batch(text,jsonb,text)', "
                "'execute')") == "f"
    assert _one("select has_function_privilege('service_role', "
                "'public.visual_scene_insert_calendar_batch(text,jsonb,text)', "
                "'execute')") == "t"
    assert _one("select has_function_privilege('service_role', "
                "'public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)', "
                "'execute')") == "f"
    denied = _run("set role anon; select " + call, check=False)
    assert denied.returncode != 0
    assert "permission denied for function visual_scene_insert_calendar_batch" in denied.stderr
    allowed = _run("set role service_role; select " + call)
    assert allowed.returncode == 0
    assert _count("content_calendar") == 1
    result = json.loads(allowed.stdout.strip().splitlines()[-1])[0]
    direct = ("public.visual_scene_register_candidate("
              f"{_lit(tenant)}, {_lit(group)}, '0000000000000000', "
              f"{_lit(url)}, {_lit(fingerprint)}, "
              f"jsonb_build_object('verified_bytes', {_lit(fingerprint)}), "
              "'acl-test', 'display')")
    denied_direct = _run("set role service_role; select " + direct, check=False)
    assert denied_direct.returncode != 0
    assert "permission denied for function visual_scene_register_candidate" in denied_direct.stderr
    owner_conflict = _run("select " + direct, check=False)
    assert owner_conflict.returncode != 0
    assert "visual_scene_candidate_canonical_binding_uq" in owner_conflict.stderr
    assert _count("visual_scene_candidate") == 1
    assert _one("select candidate_id from public.visual_scene_candidate") == result["candidate_id"]
    _sql("grant execute on function public.visual_scene_register_candidate("
         "text,text,text,text,text,jsonb,text,text) to service_role")
    try:
        drifted = _run("set role service_role; select " + call, check=False)
        assert drifted.returncode != 0
        assert "unsafe direct service-role scene registration is enabled" in drifted.stderr
    finally:
        _sql("revoke execute on function public.visual_scene_register_candidate("
             "text,text,text,text,text,jsonb,text,text) from service_role")
