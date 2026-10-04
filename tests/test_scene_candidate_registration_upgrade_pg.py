"""Upgrade/refusal checks for the additive atomic calendar installer."""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


DSN = os.environ.get("VISUAL_SCENE_ATOMIC_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
ATOMIC = MIGRATIONS / "DRAFT_visual_scene_calendar_atomic_write_20261004.sql"
DEDUPE = MIGRATIONS / "DRAFT_visual_scene_calendar_candidate_dedupe_20261004.sql"
BASE = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
]

pytestmark = pytest.mark.skipif(
    not DSN or not PSQL or not ATOMIC.exists() or not DEDUPE.exists(),
    reason="upgrade checks need the named disposable local PostgreSQL DB",
)


def _run(statement=None, *, script=None, check=True):
    command = [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN]
    if statement is not None:
        command += ["-c", statement]
    done = subprocess.run(command, input=script, text=True, capture_output=True, timeout=240)
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


def _apply(path, *, check=True):
    return _run(script=path.read_text(), check=check)


@pytest.fixture(autouse=True)
def _frozen_upgrade_state():
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ "
        r"dbname=echo_scene_atomic_write_20261004(?: user=[A-Za-z0-9_-]+)?",
        DSN,
    )
    assert _one("select current_database()") == "echo_scene_atomic_write_20261004"
    _sql("drop schema public cascade; create schema public; grant usage on schema public to public")
    _sql("do $$ begin "
         "if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin; end if; end $$")
    _sql("create table public.content_calendar ("
         "id uuid primary key default gen_random_uuid(), gym_id text, account text,"
         "post_date date, format text, caption text, status text,"
         "variant_status text default 'active', published_at timestamptz,"
         "publish_claim_token uuid, publish_reservation_day date, late_post_id text,"
         "image_url text, thumbnail_url text, source_media_url text,"
         "source_media_asset_id text, drive_file_id text, byte_hash text, r2_key text,"
         "media_not_ready_reason text)")
    _sql("create table public.media_asset (id text primary key, gym_id text, content_hash text)")
    _sql("create function public.visual_group_row_active(public.content_calendar) "
         "returns boolean language sql stable as $$ select false $$")
    _sql("create function public.visual_group_row_ambiguous(public.content_calendar) "
         "returns boolean language sql stable as $$ select false $$")
    script = "".join((MIGRATIONS / name).read_text() + "\n" for name in BASE)
    _run(script=script)


def _binding(*, evidence_note="same", phash="1111222233334444"):
    tenant = str(uuid.uuid4())
    alias = "alias_" + uuid.uuid4().hex[:12]
    group = "vg_" + uuid.uuid4().hex[:12]
    url = f"https://upgrade.example/{uuid.uuid4().hex}.jpg"
    fingerprint = "md5:" + uuid.uuid4().hex
    _sql("insert into public.tenant_alias(alias_key,tenant_id) values "
         f"({_lit(tenant)},{_lit(tenant)}),({_lit(alias)},{_lit(tenant)})")
    _sql("insert into public.visual_group(gym_id,group_key) values "
         f"({_lit(tenant)},{_lit(group)})")
    receipt = _one("insert into public.visual_global_object_read_receipt"
                   "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,"
                   "evidence_ref,observed_by) values "
                   f"({_lit(tenant)},{_lit(url)},{_lit(fingerprint)},123,"
                   "'verified_object_read','upgrade-test','upgrade-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url,tenant_id,group_key,fingerprint,byte_length,acquisition_method,"
         "evidence_ref,read_receipt,attested_by) values "
         f"({_lit(url)},{_lit(tenant)},{_lit(group)},{_lit(fingerprint)},123,"
         f"'verified_object_read','upgrade-test',{_lit(receipt)},'upgrade-test')")
    evidence = {"verified_bytes": fingerprint, "review_note": evidence_note}
    return tenant, alias, group, url, fingerprint, phash, evidence


def _register(binding, *, phash=None, evidence=None):
    tenant, _, group, url, fingerprint, default_phash, default_evidence = binding
    return _one("select public.visual_scene_register_candidate("
                f"{_lit(tenant)},{_lit(group)},{_lit(phash or default_phash)},"
                f"{_lit(url)},{_lit(fingerprint)},"
                f"{_lit(json.dumps(evidence or default_evidence))}::jsonb,"
                "'upgrade-test','display')")


def _hold(binding, candidate_id):
    tenant, _, group, url, fingerprint, phash, _ = binding
    return _one("insert into public.visual_scene_review_hold("
                "tenant_id,group_key,claim_date,channel,candidate_id,exact_url,"
                "fingerprint,candidate_phash,matched_phash,hamming,matched_tenant_id,"
                "matched_group_key,matched_used_date,hold_kind,evidence) values "
                f"({_lit(tenant)},{_lit(group)},'2026-10-04','ig',{_lit(candidate_id)}::uuid,"
                f"{_lit(url)},{_lit(fingerprint)},{_lit(phash)},{_lit(phash)},0,"
                f"{_lit(tenant)},{_lit(group)},'2026-10-03','near_frame','{{}}'::jsonb) "
                "returning hold_id")


def _frozen_receipt():
    return {
        "register_acl": _one("select has_function_privilege('service_role',"
                             "'public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)',"
                             "'execute')"),
        "register_definition": _one("select md5(pg_get_functiondef("
                                    "'public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)'::regprocedure))"),
    }


def _install_prior_atomic_sentinel():
    _sql("create function public.visual_scene_insert_calendar_batch(text,jsonb,text) "
         "returns jsonb language sql as $$ select '[{\"prior\":true}]'::jsonb $$; "
         "grant execute on function public.visual_scene_insert_calendar_batch("
         "text,jsonb,text) to service_role")
    return {
        "definition": _one("select md5(pg_get_functiondef("
                           "'public.visual_scene_insert_calendar_batch(text,jsonb,text)'::regprocedure))"),
        "service_acl": _one("select has_function_privilege('service_role',"
                            "'public.visual_scene_insert_calendar_batch(text,jsonb,text)',"
                            "'execute')"),
    }


def _prior_atomic_receipt():
    return {
        "definition": _one("select md5(pg_get_functiondef("
                           "'public.visual_scene_insert_calendar_batch(text,jsonb,text)'::regprocedure))"),
        "service_acl": _one("select has_function_privilege('service_role',"
                            "'public.visual_scene_insert_calendar_batch(text,jsonb,text)',"
                            "'execute')"),
    }


def test_duplicate_inventory_refuses_atomically_with_actionable_ids_and_holds():
    identical = _binding()
    identical_ids = [_register(identical), _register(identical)]
    hold_id = _hold(identical, identical_ids[1])
    evidence_conflict = _binding()
    conflict_evidence_ids = [
        _register(evidence_conflict),
        _register(evidence_conflict, evidence={
            "verified_bytes": evidence_conflict[4], "review_note": "different"}),
    ]
    identity_conflict = _binding()
    conflict_identity_ids = [
        _register(identity_conflict),
        _register(identity_conflict, phash="9999999999999999"),
    ]
    before = _frozen_receipt()
    prior_atomic = _install_prior_atomic_sentinel()

    refused = _apply(ATOMIC, check=False)

    assert refused.returncode != 0
    for marker in ("identical_evidence", "conflicting_evidence", "conflicting_identity",
                   hold_id, *identical_ids, *conflict_evidence_ids, *conflict_identity_ids):
        assert marker in refused.stderr
    assert _frozen_receipt() == before
    assert _prior_atomic_receipt() == prior_atomic
    assert _one("select to_regclass('public.visual_scene_candidate_canonical_binding_uq') is null") == "t"
    assert _one("select count(*) from public.visual_scene_candidate") == "6"
    assert _one("select candidate_id from public.visual_scene_review_hold where hold_id="
                f"{_lit(hold_id)}::uuid") == identical_ids[1]


def test_reviewed_identical_remediation_rebinds_hold_then_installs_and_replays():
    binding = _binding()
    ids = sorted([_register(binding), _register(binding)])
    hold_id = _hold(binding, ids[1])
    assert _apply(ATOMIC, check=False).returncode != 0
    _apply(DEDUPE)

    remediation_receipt = _one(
        "select public.visual_scene_review_identical_candidate_duplicate("
        f"{_lit(ids[0])}::uuid,{_lit(ids[1])}::uuid,'upgrade-reviewer',"
        "'{\"ticket\":\"SCENE-UPGRADE-TEST\",\"decision\":\"identical\"}'::jsonb)")

    assert _one("select candidate_id from public.visual_scene_review_hold where hold_id="
                f"{_lit(hold_id)}::uuid") == ids[0]
    archived_holds = json.loads(_one(
        "select rebound_review_holds::text from public.visual_scene_candidate_dedupe_receipt "
        f"where receipt_id={_lit(remediation_receipt)}::uuid"))
    assert archived_holds[0]["hold_id"] == hold_id
    assert archived_holds[0]["candidate_id"] == ids[1]
    assert _one("select count(*) from public.visual_scene_candidate") == "1"

    _apply(ATOMIC)
    first_definition = _one("select md5(pg_get_functiondef("
                            "'public.visual_scene_insert_calendar_batch(text,jsonb,text)'::regprocedure))")
    _apply(ATOMIC)

    assert _one("select count(*) from public.visual_scene_calendar_install_receipt "
                "where duplicate_binding_count=0 and candidate_count=1") == "2"
    assert _one("select md5(pg_get_functiondef("
                "'public.visual_scene_insert_calendar_batch(text,jsonb,text)'::regprocedure))") == first_definition
    assert _one("select has_function_privilege('service_role',"
                "'public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)',"
                "'execute')") == "f"


def test_wrong_shape_named_index_refuses_without_acl_or_function_change():
    before = _frozen_receipt()
    prior_atomic = _install_prior_atomic_sentinel()
    _sql("create index visual_scene_candidate_canonical_binding_uq "
         "on public.visual_scene_candidate(tenant_id)")

    refused = _apply(ATOMIC, check=False)

    assert refused.returncode != 0
    assert "exists with wrong catalog shape" in refused.stderr
    assert _frozen_receipt() == before
    assert _prior_atomic_receipt() == prior_atomic
    assert _one("select pg_get_indexdef("
                "'public.visual_scene_candidate_canonical_binding_uq'::regclass)") == (
                    "CREATE INDEX visual_scene_candidate_canonical_binding_uq "
                    "ON public.visual_scene_candidate USING btree (tenant_id)")
