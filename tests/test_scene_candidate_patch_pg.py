"""scene_candidate_patch: OPTIONAL real-PostgreSQL atomic media PATCH checks.

These run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_calendar_atomic_patch_20261004.sql` exists,
  * the canonical insert-writer migration
    `migrations/DRAFT_visual_scene_calendar_atomic_write_20261004.sql`
    exists IN THIS REPOSITORY (the patch RPC depends on its canonical unique
    binding and its revocation of the direct service_role registration
    route),
  * VISUAL_SCENE_PATCH_TEST_DSN names a disposable Unix-socket database
    literally named `echo_scene_patch_test` (same house rule as
    tests/test_scene_claim_wave_pg.py — never a network or production host),
  * local psql is on PATH.

They pin the contract of public.visual_scene_atomic_media_patch
(KIMI_SCENE_PATCH_SWARM_20261004.md, 2026-10-04, repaired after independent
review rejection): canonical scene-candidate staging (stable same-evidence
reuse under visual_scene_candidate_canonical_binding_uq, contradictory
pHash/fingerprint fails closed, concurrent identical registration converges)
and the existing full-row/media compare-and-swap happen in ONE PostgreSQL
transaction; staging happens BEFORE the UPDATE; p_expected must carry EVERY
_VISUAL_MEDIA_CAS_COLUMNS key (explicit JSON nulls allowed, absent keys
refused); only UNSENT active pending/coach_review/approved rows are
patchable — published/claimed/reserved/late_post_id, archived, currently
scene-held rows and rows with an OPEN review hold are refused before any
write, while resolved hold history never blocks (independent-review P1
repair, 2026-10-04); a persisted scene hold returns a distinct 'held'
outcome with the
actual committed row (never 'patched'); 'patched' requires every patch value
persisted and the rebound row candidate equal to the stable candidate; a
stale CAS rolls the staging back with the subtransaction (no compensating
DELETE) and returns a clearly distinguishable 'stale'/'not_found' outcome;
the RPC never writes scene occupancy (only the merged guard trigger does, on
the UPDATE); and EXECUTE is granted to service_role only. Everything runs
committed against the disposable scratch database named above — nowhere else.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("VISUAL_SCENE_PATCH_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
# The canonical insert writer MUST resolve from THIS repository's own
# migrations/ directory: integrated PR/CI runs against the integrated stack,
# never a sibling worktree on one developer machine.
PATCH = MIGRATIONS / "DRAFT_visual_scene_calendar_atomic_patch_20261004.sql"
WRITE = MIGRATIONS / "DRAFT_visual_scene_calendar_atomic_write_20261004.sql"
# Exact apply order. DRAFT_visual_scene_phash_20261003.sql is a SUPERSEDED
# design sketch (its record functions raise 0A000) and is NOT applied. The
# canonical insert writer MUST be applied before the patch migration.
STACK = [
    MIGRATIONS / "DRAFT_visual_group_schema_20261002.sql",
    MIGRATIONS / "DRAFT_visual_global_history_20261002.sql",
    MIGRATIONS / "DRAFT_visual_group_claim_trigger_20261002.sql",
    MIGRATIONS / "DRAFT_visual_group_backfill_20261002.sql",
    MIGRATIONS / "DRAFT_visual_group_activation_20261002.sql",
    MIGRATIONS / "DRAFT_visual_scene_claim_wave_20261003.sql",
    WRITE,
    PATCH,
]

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="pg atomic media patch checks are opt-in: set "
           "VISUAL_SCENE_PATCH_TEST_DSN on a disposable local DB",
)

_RPC = ("public.visual_scene_atomic_media_patch"
        "(uuid, text, jsonb, jsonb, jsonb)")
_REGISTER = ("public.visual_scene_register_candidate"
             "(text,text,text,text,text,jsonb,text,text)")

# Deterministic pHashes. 0x0000.. vs 0xaaaa.. differ in 32 bits (> 30: clean);
# 0xbbbb.. vs 0xaaaa.. differ in exactly 16 bits (7..30: uncertain -> hold).
_PHASH_A = "0000000000000000"
_PHASH_B = "aaaaaaaaaaaaaaaa"
_PHASH_HOLD = "bbbbbbbbbbbbbbbb"
_PHASH_C = "cccccccccccccccc"


def _run(statement, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", statement], text=True, capture_output=True, timeout=120)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _sql(statement):
    return _run(statement).stdout.strip()


def _one(statement):
    out = _sql(statement)
    return out.splitlines()[-1] if out else ""


def _lit(value):
    """A safely-quoted SQL literal for jsonb parameter blobs."""
    return "$patch$" + value + "$patch$"


@pytest.fixture(scope="module", autouse=True)
def _scratch_stack():
    assert PATCH.exists(), f"patch migration missing: {PATCH}"
    assert WRITE.exists(), (f"canonical insert migration missing: {WRITE} — "
                            "it must live in THIS repository's migrations/ "
                            "directory (integration prerequisite)")
    assert PSQL, "psql not on PATH"
    for path in STACK:
        assert path.exists(), f"stack migration missing: {path}"
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_patch_test"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_patch_test allowed"
    assert _one("select current_database()") == "echo_scene_patch_test"
    # Reset the scratch schema: the drafts are not re-appliable (policies
    # have no IF NOT EXISTS), so each run starts clean.
    _sql("drop schema public cascade; create schema public; "
         "grant usage on schema public to public;")
    # Roles referenced by the drafts' revoke/grant statements.
    _sql("do $$ begin "
         "if not exists (select 1 from pg_roles where rolname='anon') then "
         "create role anon nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='authenticated') then "
         "create role authenticated nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='service_role') then "
         "create role service_role nologin; end if; end $$")
    # Stub content_calendar with the full CAS column surface the RPC compares
    # (visual_group_key itself is added by the schema draft's ALTER, so it is
    # deliberately absent here) plus the columns the trigger chain reads.
    _sql("create table if not exists public.content_calendar ("
         "id uuid primary key default gen_random_uuid(),"
         "gym_id text, account text, post_date date,"
         "status text, variant_status text, format text, caption text,"
         "time_slot text, slot_index integer,"
         "scheduled_at timestamptz, created_at timestamptz default now(),"
         "published_at timestamptz, publish_claim_token uuid,"
         "publish_reservation_day date,"
         "late_post_id text, image_url text, thumbnail_url text,"
         "source_media_url text, source_media_asset_id text,"
         "drive_file_id text, byte_hash text, r2_key text,"
         "media_not_ready_reason text)")
    _sql("create table if not exists public.media_asset ("
         "id text primary key, gym_id text, content_hash text)")
    # Inert row-state stubs; the claim-trigger draft's CREATE OR REPLACE
    # installs the real versions later in the stack.
    _sql("create or replace function public.visual_group_row_active(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    _sql("create or replace function public.visual_group_row_ambiguous(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    script = ""
    for path in STACK:
        script += path.read_text() + "\n"
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=240)
    assert done.returncode == 0, done.stderr


@pytest.fixture(autouse=True)
def _isolate_scenarios():
    """Scenario isolation on the scratch DB. session_replication_role=replica
    bypasses the immutability triggers — scratch database only, never
    production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold, "
         "public.visual_scene_phash_occupied, public.visual_scene_candidate, "
         "public.visual_global_usage, public.visual_global_usage_member, "
         "public.visual_group_usage_ledger, "
         "public.visual_group_usage_sibling cascade")


# ---- seeding helpers ----------------------------------------------------------

def _seed_tenant():
    """Armed canonical tenant + visual group on the scratch DB. Committed."""
    tid = str(uuid.uuid4())
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    # Arm enforcement through the activation draft's own RPC.
    _sql(f"select public.visual_group_activate_guard('{tid}', 'scene-patch-test')")
    return tid, group


def _add_group(tid):
    """A second real visual group for the same tenant (occupied-scene FKs)."""
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    return group


def _seed_object(tid, group, phash=None):
    """Fully attested delivered object for the scene; when phash is given, a
    bound 'display' candidate is staged too. Returns (url, fingerprint)."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'scene-patch-test') returning receipt_id")
    # Bind through the real preparation RPC (same URL as both source and
    # delivered bytes, no render receipt) instead of hand-inserting
    # attestation/member/alias rows: direct inserts leave the scene's
    # exact-byte global staged history stale, which breaks the frozen
    # release/guard SQL's history lookups for held rows prepared after an
    # earlier object already claimed the group.
    _one("select public.visual_global_prepare_source_rendition("
         f"'{tid}', '{group}', '{receipt}', '{receipt}', NULL,"
         " 'scene-patch-test')")
    if phash is not None:
        _one("select public.visual_scene_register_candidate("
             f"'{tid}', '{group}', '{phash}', '{url}', '{fp}',"
             f" jsonb_build_object('verified_bytes', '{fp}'),"
             " 'scene-patch-test', 'display')")
    return url, fp


def _insert_row(tid, group, url, date="2026-10-10", status="pending"):
    return _one(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status, format,"
        " caption, image_url, source_media_url, visual_group_key) values "
        f"('{tid}', 'ig', '{date}', '{status}', 'active', 'feed',"
        f" 'scratch caption', '{url}', '{url}', '{group}') returning id")


def _insert_ineligible_row(tid, group, url, status, variant_status):
    """Scratch-only fixture insert: session_replication_role=replica bypasses
    the content_calendar guard trigger so ineligible before-image states
    (variant_status 'candidate'/NULL, NULL status) that production's real
    constraints and trigger would never persist can be staged as test input.
    Disposable DB only — this does NOT weaken the production guard; the RPC
    under test must refuse these rows itself."""
    status_sql = "null" if status is None else f"'{status}'"
    variant_sql = "null" if variant_status is None else f"'{variant_status}'"
    return _one(
        "set session_replication_role = replica; "
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status, format,"
        " caption, image_url, source_media_url, visual_group_key) values "
        f"('{tid}', 'ig', '2026-10-10', {status_sql}, {variant_sql}, 'feed',"
        f" 'scratch caption', '{url}', '{url}', '{group}') returning id")


def _set_send_marker(row_id, column, literal):
    """Scratch-only: stamp ONE send marker onto an otherwise eligible active
    unsent row so the RPC's pre-write guard can be exercised directly. The
    single statement runs under session_replication_role=replica on a fresh
    psql connection, bypassing the content_calendar guard trigger for the
    fixture setup only (disposable DB — the production guard is untouched);
    the RPC under test runs in its own fresh NORMAL connection and must
    refuse the row itself."""
    assert column in ("published_at", "publish_claim_token",
                      "publish_reservation_day", "late_post_id")
    _sql("set session_replication_role = replica; "
         f"update public.content_calendar set {column} = {literal} "
         f"where id = '{row_id}'")


def _insert_open_hold(tid, group, row_id, url, fp, phash, matched_group):
    """A scratch-only OPEN review hold on the row (bypasses the hold
    mutation trigger via session_replication_role=replica — disposable DB
    only). Requires a staged candidate for (url, phash) to satisfy the FK."""
    cand = _candidate_id_for(url)
    assert cand
    _sql("set session_replication_role = replica; "
         "insert into public.visual_scene_review_hold"
         "(tenant_id, group_key, claim_date, calendar_row_id, channel,"
         " candidate_id, exact_url, fingerprint, candidate_phash,"
         " matched_phash, hamming, matched_tenant_id, matched_group_key,"
         " matched_used_date, hold_kind, evidence) values "
         f"('{tid}', '{group}', '2026-10-10', '{row_id}', 'ig',"
         f" '{cand}', '{url}', '{fp}', '{phash}',"
         f" '{_PHASH_HOLD}',"
         # Exact recomputed distance (visual_scene_hamming): _PHASH_A vs
         # _PHASH_HOLD differ in 3 bits per hex digit -> 48, not the
         # stale hardcoded 16 (that was the _PHASH_B vs _PHASH_HOLD
         # distance). The hold-release RPC re-derives hamming and
         # rejects drift, so the fixture must carry the true value.
         f" public.visual_scene_hamming('{phash}', '{_PHASH_HOLD}'),"
         f" '{tid}', '{matched_group}',"
         " '2026-10-09', 'uncertain', '{}')")


def _expected(row_id):
    """The exact observed before-image (the CAS the Python lanes build)."""
    return _one("select to_jsonb(c)::text from public.content_calendar c "
                f"where c.id = '{row_id}'")


def _candidate_arg(tid, group, phash, url, fp):
    """The same staging-only evidence shape the atomic insert writer accepts."""
    return json.dumps({
        "kind": "visual_scene_candidate", "stage": "candidate",
        "usage_claimed": False, "counts_as_use": False,
        "tenant_id": tid, "group_key": group, "phash": phash,
        "exact_url": url, "fingerprint": fp,
        "evidence": {"verified_bytes": fp},
        "actor": "scene-patch-test", "object_role": "display"})


def _call(row_id, gym_id, expected, patch, candidate=None, check=True):
    # `candidate` may be given as a dict or an already-encoded JSON string.
    if candidate is None:
        cand_sql = "null"
    else:
        cand_sql = _lit(candidate if isinstance(candidate, str)
                        else json.dumps(candidate))
    return _run(
        "select public.visual_scene_atomic_media_patch("
        f"'{row_id}'::uuid, '{gym_id}', {_lit(expected)}, {_lit(patch)}, "
        f"{cand_sql})", check=check)


def _candidates_for(url):
    return int(_one("select count(*) from public.visual_scene_candidate "
                    f"where exact_url = '{url}'"))


def _candidate_id_for(url):
    return _one("select candidate_id::text from public.visual_scene_candidate "
                f"where exact_url = '{url}'")


def _row(row_id):
    return json.loads(_one(
        "select jsonb_build_object('status', status,"
        " 'variant_status', variant_status, 'image_url', image_url,"
        " 'media_not_ready_reason', media_not_ready_reason,"
        " 'visual_group_key', visual_group_key)::text "
        f"from public.content_calendar where id = '{row_id}'"))


def _occupied(tid):
    return json.loads(_one(
        "select coalesce(jsonb_agg(phash), '[]'::jsonb) "
        f"from public.visual_scene_phash_occupied where tenant_id = '{tid}'"))


# ---- scenarios ----------------------------------------------------------------

def test_stack_applies_and_rpc_exists_with_service_role_only_execute():
    assert _one("select count(*) from pg_proc p join pg_namespace n "
                "on n.oid = p.pronamespace where n.nspname = 'public' and "
                "p.proname = 'visual_scene_atomic_media_patch'") == "1"
    assert _one("select has_function_privilege('service_role', "
                f"'{_RPC}', 'execute')") == "t"
    for role in ("anon", "authenticated"):
        assert _one(f"select has_function_privilege('{role}', "
                    f"'{_RPC}', 'execute')") == "f"
    # The canonical binding from the insert-writer draft is installed.
    assert _one("select to_regclass("
                "'public.visual_scene_candidate_canonical_binding_uq')"
                " is not null") == "t"


def test_direct_service_role_registration_stays_revoked():
    """The patch RPC must never re-open the frozen random-UUID direct route:
    the write draft revoked service_role EXECUTE on register_candidate and
    this stack must keep it that way (unchanged authorization)."""
    assert _one("select has_function_privilege('service_role', "
                f"'{_REGISTER}', 'execute')") == "f"
    tid, group = _seed_tenant()
    url, fp = _seed_object(tid, group)
    done = _run(f"set role service_role; "
                f"select public.visual_scene_register_candidate("
                f"'{tid}', '{group}', '{_PHASH_B}', '{url}', '{fp}',"
                f" jsonb_build_object('verified_bytes', '{fp}'),"
                " 'scene-patch-test', 'display')", check=False)
    assert done.returncode != 0
    assert "permission denied" in done.stderr


def test_happy_path_stages_candidate_then_cas_update_in_one_transaction():
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    assert _row(row_id)["variant_status"] == "active"
    url_b, fp_b = _seed_object(tid, group)  # attested, candidate NOT staged
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, expected, patch, candidate).stdout.strip())
    assert out["outcome"] == "patched"
    assert out["candidate_id"]
    assert out["candidate_reused"] is False
    row = out["row"]
    # persisted post-trigger row: the media patch applied, approval state and
    # tenant scope untouched
    assert row["id"] == row_id
    assert row["gym_id"] == tid
    assert row["image_url"] == url_b
    assert row["status"] == "pending"
    assert row["variant_status"] == "active"
    # the candidate IS staged by the RPC (preparation only)
    assert _candidates_for(url_b) == 1
    assert _candidate_id_for(url_b) == out["candidate_id"]
    # occupancy is written ONLY by the merged guard trigger on the UPDATE,
    # never by staging: B's scene is recorded by the claim path on the now-
    # clean patched row, A's by the original insert.
    assert sorted(_occupied(tid)) == sorted([_PHASH_A, _PHASH_B])


def test_same_object_retry_reuses_stable_candidate_uuid():
    """A race-safe same-object retry converges on the ONE canonical binding:
    the second identical registration reuses the stable UUID and never
    inserts a duplicate."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    first = json.loads(_call(row_id, tid, _expected(row_id), patch,
                             candidate).stdout.strip())
    assert first["outcome"] == "patched"
    # Simulate the portal retrying the same patch with a freshly re-read
    # expected image (e.g. after a transient client failure): the candidate
    # payload is identical, so the canonical binding is reused.
    second = json.loads(_call(row_id, tid, _expected(row_id), patch,
                              candidate).stdout.strip())
    assert second["outcome"] == "patched"
    assert second["candidate_id"] == first["candidate_id"]
    assert second["candidate_reused"] is True
    assert _candidates_for(url_b) == 1


def test_concurrent_identical_registration_converges():
    """Two concurrent RPC calls staging the SAME candidate object: both
    converge on one canonical candidate row; the unique-index loser reads
    the committed winner. Here both callers race the same row, so exactly
    one CAS wins and the other reports a distinguishable stale."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    results, errors = [], []

    def worker():
        try:
            results.append(json.loads(
                _call(row_id, tid, expected, patch, candidate).stdout.strip()))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes == ["patched", "stale"]
    # ONE canonical candidate, and the patched row is bound to it.
    assert _candidates_for(url_b) == 1
    patched = next(r for r in results if r["outcome"] == "patched")
    assert patched["candidate_id"] == _candidate_id_for(url_b)
    assert _row(row_id)["image_url"] == url_b


def test_conflicting_candidate_identity_fails_closed():
    """Same canonical binding (tenant, group, role, exact URL) with a
    DIFFERENT pHash must fail closed — the stable candidate's evidence is
    never silently replaced."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    good = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, _expected(row_id), patch,
                           good).stdout.strip())
    assert out["outcome"] == "patched"
    # A retry carrying a contradictory pHash for the same displayed object.
    bad = _candidate_arg(tid, group, _PHASH_C, url_b, fp_b)
    done = _call(row_id, tid, _expected(row_id), patch, bad, check=False)
    assert done.returncode != 0
    assert "conflicts with the canonical candidate identity" in done.stderr
    # still exactly one canonical candidate with the ORIGINAL evidence
    assert _candidates_for(url_b) == 1
    assert _one("select btrim(phash::text) from public.visual_scene_candidate "
                f"where exact_url = '{url_b}'") == _PHASH_B


def test_changed_group_url_or_role_rejected():
    """A bare tenant match is never sufficient: the candidate must bind the
    post-patch group AND the exact delivered object role/URL."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "media_not_ready_reason": None})
    # changed group
    other_group = _add_group(tid)
    bad = _candidate_arg(tid, other_group, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, expected, patch, bad, check=False)
    assert done.returncode != 0
    assert "does not match the patched row visual group" in done.stderr
    # changed exact URL (candidate names a different displayed object)
    url_c, fp_c = _seed_object(tid, group)
    bad = _candidate_arg(tid, group, _PHASH_B, url_c, fp_c)
    done = _call(row_id, tid, expected, patch, bad, check=False)
    assert done.returncode != 0
    assert "does not bind the patched row exact delivered object" in done.stderr
    # changed object role (poster candidate for a display row)
    bad = json.loads(_candidate_arg(tid, group, _PHASH_B, url_b, fp_b))
    bad["object_role"] = "poster"
    done = _call(row_id, tid, expected, patch, json.dumps(bad), check=False)
    assert done.returncode != 0
    assert "does not bind the patched row exact delivered object" in done.stderr
    # nothing staged, nothing patched
    assert _candidates_for(url_b) == 0
    assert _candidates_for(url_c) == 0
    assert _row(row_id)["image_url"] == url_a


def test_scene_hold_returns_held_with_persisted_row_and_preserves_hold():
    """An uncertain scene match persists an archived hold row through the
    merged guard trigger. The RPC must report a distinct 'held' outcome with
    the ACTUAL committed row — never 'patched' — and must not raise after
    the committed hold."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    # A permanently occupied UNCERTAIN scene (16 bits from _PHASH_B) owned by
    # another group on another date of the same tenant.
    other_group = _add_group(tid)
    _sql("insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, channel, evidence) values "
         f"('{_PHASH_HOLD}', '{tid}', '{other_group}', '2026-10-09',"
         f" 'md5:{uuid.uuid4().hex}', '{uuid.uuid4()}', 'ig', '{{}}')")
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, _expected(row_id), patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "held"
    assert out["candidate_id"]
    row = out["row"]
    assert row["id"] == row_id
    assert row["status"] == "pending"
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["image_url"] == url_b
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    # the committed hold is preserved: durable review-hold row exists and the
    # candidate stayed staged; no occupancy was fabricated for B's scene
    assert _candidates_for(url_b) == 1
    assert int(_one("select count(*) from public.visual_scene_review_hold "
                    f"where calendar_row_id = '{row_id}'")) >= 1
    assert _PHASH_B not in _occupied(tid)
    # the persisted row really is the held row (not a synthesized payload)
    assert _row(row_id)["media_not_ready_reason"] == "scene_review_hold"
    assert _row(row_id)["variant_status"] == "archived"


def test_preexisting_held_row_refused_before_any_write():
    """An already-archived/held row is NOT patchable through this RPC: the
    frozen scene guard only scans ACTIVE rows and the old hold evidence
    still names the old media, so silently re-patching media under an
    existing hold would leave stale evidence. The RPC must fail closed
    BEFORE candidate staging/update; media, candidates and hold rows are
    all untouched. A NEWLY trigger-created hold (see
    test_scene_hold_returns_held_with_persisted_row_and_preserves_hold)
    remains the intended 'held' result and does not raise."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    # Uncertain scene permanently occupied by another group/date -> the
    # first patch persists a REAL hold through the merged guard trigger.
    other_group = _add_group(tid)
    _sql("insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, channel, evidence) values "
         f"('{_PHASH_HOLD}', '{tid}', '{other_group}', '2026-10-09',"
         f" 'md5:{uuid.uuid4().hex}', '{uuid.uuid4()}', 'ig', '{{}}')")
    patch_b = json.dumps({"image_url": url_b, "source_media_url": url_b,
                          "media_not_ready_reason": None})
    cand_b = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, _expected(row_id), patch_b,
                           cand_b).stdout.strip())
    assert out["outcome"] == "held"
    holds_before = int(_one(
        "select count(*) from public.visual_scene_review_hold "
        f"where calendar_row_id = '{row_id}'"))
    assert holds_before >= 1
    # A second patch attempt against the now preexisting archived/held row
    # must be refused BEFORE staging or update.
    url_c, fp_c = _seed_object(tid, group)
    patch_c = json.dumps({"image_url": url_c, "source_media_url": url_c,
                          "media_not_ready_reason": None})
    cand_c = _candidate_arg(tid, group, _PHASH_C, url_c, fp_c)
    done = _call(row_id, tid, _expected(row_id), patch_c, cand_c,
                 check=False)
    assert done.returncode != 0
    assert "pending/coach_review" in done.stderr
    # no media, candidate or hold changes
    row = _row(row_id)
    assert row["image_url"] == url_b
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert _candidates_for(url_c) == 0
    assert int(_one("select count(*) from public.visual_scene_review_hold "
                    f"where calendar_row_id = '{row_id}'")) == holds_before


def test_candidate_variant_refused_before_staging_and_mutation():
    """Independent-review P1 repair (2026-10-04): a row with
    variant_status='candidate' is NOT scanned by the frozen scene guard
    (visual_group_row_active requires variant_status='active'), so a media
    patch on it would return 'patched' without any scene re-decision. The
    RPC must refuse BEFORE candidate staging and BEFORE any row mutation.
    The ineligible state is staged scratch-only (trigger bypassed via
    session_replication_role=replica on the disposable DB; see
    _insert_ineligible_row) — the production guard is untouched."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_ineligible_row(tid, group, url_a, "pending",
                                    "candidate")
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, _expected(row_id), patch, candidate,
                 check=False)
    assert done.returncode != 0
    assert "variant_status" in done.stderr and "exactly" in done.stderr
    # refusal happened BEFORE staging and BEFORE any mutation
    assert _candidates_for(url_b) == 0
    row = _row(row_id)
    assert row["image_url"] == url_a
    assert row["variant_status"] == "candidate"


def test_null_variant_refused_before_staging_and_mutation():
    """A NULL variant_status slips a plain `is not distinct from 'archived'`
    check and is never scanned by the frozen guard (active requires
    variant_status='active'): it must be refused positively, before staging
    or mutation. Scratch-only fixture (see _insert_ineligible_row)."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_ineligible_row(tid, group, url_a, "pending", None)
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, _expected(row_id), patch, candidate,
                 check=False)
    assert done.returncode != 0
    assert "variant_status" in done.stderr and "exactly" in done.stderr
    assert _candidates_for(url_b) == 0
    row = _row(row_id)
    assert row["image_url"] == url_a
    assert row["variant_status"] is None


def test_null_status_refused_before_staging_and_mutation():
    """A NULL status makes `status not in (...)` evaluate to NULL (not TRUE),
    so the old negative check passed it; the NULL-safe positive check must
    refuse it before staging or mutation. The scratch content_calendar stub
    has a nullable status column, so this state is insertable scratch-only
    (see _insert_ineligible_row)."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_ineligible_row(tid, group, url_a, None, "active")
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, _expected(row_id), patch, candidate,
                 check=False)
    assert done.returncode != 0
    assert "pending/coach_review/approved" in done.stderr
    assert _candidates_for(url_b) == 0
    row = _row(row_id)
    assert row["image_url"] == url_a
    assert row["status"] is None


def _send_marker_refused_before_staging_and_mutation(column, literal):
    """Shared body: an otherwise eligible active pending UNSENT row carrying
    exactly one send marker must be refused by the RPC's pre-write guard —
    before candidate staging and before any row mutation — and the marker
    must survive the refused call untouched."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)  # pending, variant active, unsent
    _set_send_marker(row_id, column, literal)
    marker_before = _one(f"select {column}::text "
                         f"from public.content_calendar where id = '{row_id}'")
    assert marker_before
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    # expected is re-read AFTER the marker is set, so the row pre-image is
    # otherwise fully consistent — only the pre-write guard can refuse here.
    done = _call(row_id, tid, _expected(row_id), patch, candidate,
                 check=False)
    assert done.returncode != 0
    assert "atomic media patch refuses this row" in done.stderr
    # refused BEFORE candidate staging and BEFORE any mutation
    assert _candidates_for(url_b) == 0
    row = _row(row_id)
    assert row["image_url"] == url_a
    assert row["status"] == "pending"
    assert row["variant_status"] == "active"
    # the send marker remains exactly as set
    assert _one(f"select {column}::text "
                f"from public.content_calendar where id = '{row_id}'") == marker_before


def test_published_at_row_refused_before_staging_and_mutation():
    """Independent-review repair (2026-10-04): direct regression for the
    published_at pre-write send marker — a published-at stamped, otherwise
    eligible active pending row must never reach candidate staging."""
    _send_marker_refused_before_staging_and_mutation(
        "published_at", "'2026-10-10 12:00:00+00'::timestamptz")


def test_publish_claim_token_row_refused_before_staging_and_mutation():
    """Direct regression for the publish_claim_token pre-write send marker
    (claimed row) on an otherwise eligible active pending row."""
    _send_marker_refused_before_staging_and_mutation(
        "publish_claim_token", "gen_random_uuid()")


def test_publish_reservation_day_row_refused_before_staging_and_mutation():
    """Direct regression for the publish_reservation_day pre-write send
    marker (reserved row) on an otherwise eligible active pending row."""
    _send_marker_refused_before_staging_and_mutation(
        "publish_reservation_day", "'2026-10-10'::date")


def test_late_post_id_row_refused_before_staging_and_mutation():
    """Direct regression for the late_post_id pre-write send marker on an
    otherwise eligible active pending row."""
    _send_marker_refused_before_staging_and_mutation(
        "late_post_id", f"'late_{uuid.uuid4().hex}'")


def test_approved_unsent_row_patches_with_approval_preserved():
    """Independent-review P1 repair: the frozen scene guard's active
    predicate (visual_group_row_active) scans APPROVED active rows, so an
    approved UNSENT row is re-decided on a media update and must be
    patchable under the full CAS. The patch never touches status, so the
    approval state is preserved exactly."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a, status="approved")
    assert _row(row_id)["status"] == "approved"
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, _expected(row_id), patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "patched"
    assert out["candidate_reused"] is False
    row = out["row"]
    assert row["id"] == row_id
    assert row["image_url"] == url_b
    # approval state preserved — the RPC can never rewrite status
    assert row["status"] == "approved"
    assert row["variant_status"] == "active"
    assert row["publish_claim_token"] is None
    assert _candidates_for(url_b) == 1
    assert sorted(_occupied(tid)) == sorted([_PHASH_A, _PHASH_B])


def test_approved_row_with_open_scene_hold_refused_before_any_write():
    """Approval alone does not make a row patchable: an approved row that
    carries an OPEN scene review hold (a currently held conflict) must be
    refused BEFORE candidate staging or update — the open hold's evidence
    still names the OLD media and the guard never re-scans held rows."""
    tid, group = _seed_tenant()
    url_a, fp_a = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a, status="approved")
    other_group = _add_group(tid)
    _insert_open_hold(tid, group, row_id, url_a, fp_a, _PHASH_A,
                      other_group)
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, _expected(row_id), patch, candidate,
                 check=False)
    assert done.returncode != 0
    assert "OPEN scene review hold" in done.stderr
    # nothing staged, nothing patched; the open hold is untouched
    assert _candidates_for(url_b) == 0
    assert _row(row_id)["image_url"] == url_a
    assert _row(row_id)["status"] == "approved"
    assert _one("select state from public.visual_scene_review_hold "
                f"where calendar_row_id = '{row_id}'") == "open"


def test_closed_hold_history_never_blocks_reactivated_row():
    """Only OPEN holds block. A row held through the guard, then approved
    and reactivated through the review RPCs, keeps a RESOLVED
    (state='approved') hold in its history — that closed history must NOT
    refuse a later clean media patch."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    # Uncertain scene permanently occupied by another group/date -> the
    # first patch persists a REAL hold through the merged guard trigger.
    other_group = _add_group(tid)
    _sql("insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, channel, evidence) values "
         f"('{_PHASH_HOLD}', '{tid}', '{other_group}', '2026-10-09',"
         f" 'md5:{uuid.uuid4().hex}', '{uuid.uuid4()}', 'ig', '{{}}')")
    patch_b = json.dumps({"image_url": url_b, "source_media_url": url_b,
                          "media_not_ready_reason": None})
    cand_b = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, _expected(row_id), patch_b,
                           cand_b).stdout.strip())
    assert out["outcome"] == "held"
    hold_id = _one("select hold_id::text from "
                   "public.visual_scene_review_hold "
                   f"where calendar_row_id = '{row_id}' and state = 'open'")
    assert hold_id
    # Review: approve the exact reviewed conflict, then reactivate the row
    # through the wave-3 review RPCs (scratch DB, committed).
    resolved = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_id}'::uuid, 'approved', 'scene-patch-test',"
        " '{" + chr(34) + "note" + chr(34) + ": "
        + chr(34) + "scratch approval" + chr(34) + "}'::jsonb)::text"))
    assert resolved  # outcome payload; the persisted state is pinned below
    assert _one("select state from public.visual_scene_review_hold "
                f"where hold_id = '{hold_id}'") == "approved"
    reactivated = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold_id}'::uuid, 'scene-patch-test')::text"))
    assert reactivated["reactivated"] is True, reactivated
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    # The closed hold history stays on the row — and must NOT block a clean
    # patch of NEW media (C is 32 bits from both occupied scenes: clean).
    url_c, fp_c = _seed_object(tid, group)
    patch_c = json.dumps({"image_url": url_c, "source_media_url": url_c,
                          "media_not_ready_reason": None})
    cand_c = _candidate_arg(tid, group, _PHASH_C, url_c, fp_c)
    out = json.loads(_call(row_id, tid, _expected(row_id), patch_c,
                           cand_c).stdout.strip())
    assert out["outcome"] == "patched"
    assert out["row"]["image_url"] == url_c
    assert out["row"]["variant_status"] == "active"
    assert _one("select state from public.visual_scene_review_hold "
                f"where hold_id = '{hold_id}'") == "approved"


def test_incomplete_expected_image_rejected_explicit_nulls_allowed():
    """EVERY _VISUAL_MEDIA_CAS_COLUMNS key must be present in p_expected —
    including keys whose observed value is JSON null. An absent key widens
    the CAS silently and is refused with 22023."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    for missing in ("thumbnail_url", "publish_claim_token", "created_at",
                    "media_not_ready_reason", "slot_index"):
        expected = json.loads(_expected(row_id))
        del expected[missing]
        done = _call(row_id, tid, json.dumps(expected), patch, candidate,
                     check=False)
        assert done.returncode != 0
        assert f"missing CAS key {missing}" in done.stderr
    assert _row(row_id)["image_url"] == url_a
    assert _candidates_for(url_b) == 0
    # the complete image — with its explicit JSON nulls — is accepted
    expected = json.loads(_expected(row_id))
    assert expected["thumbnail_url"] is None  # explicit null, present key
    out = json.loads(_call(row_id, tid, json.dumps(expected), patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "patched"


def test_stale_cas_rolls_back_candidate_staging_and_reports_stale():
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    expected = json.loads(_expected(row_id))
    expected["caption"] = "someone else edited this row first"  # stale image
    patch = json.dumps({"image_url": url_b, "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    # distinguishable stale result, NOT an error
    out = json.loads(_call(row_id, tid, json.dumps(expected), patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "stale"
    assert out["row_id"] == row_id and out["gym_id"] == tid
    assert "row" not in out
    # staging rolled back with the subtransaction — no compensating DELETE,
    # and the canonical binding holds NO row for the rolled-back candidate
    assert _candidates_for(url_b) == 0
    # the row is untouched and no occupancy was fabricated
    assert _row(row_id)["image_url"] == url_a
    assert _occupied(tid) == [_PHASH_A]


def test_missing_or_cross_tenant_row_is_not_found_and_stages_nothing():
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    # a random id in this tenant's scope
    out = json.loads(_call(str(uuid.uuid4()), tid, expected, patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "not_found"
    # tenant B's row id under tenant A's scope is also 'not_found' (never
    # reveals the row exists)
    tid_b, group_b = _seed_tenant()
    url_c, _fpc = _seed_object(tid_b, group_b, _PHASH_B)
    row_b = _insert_row(tid_b, group_b, url_c)
    out = json.loads(_call(row_b, tid, expected, patch, candidate).stdout.strip())
    assert out["outcome"] == "not_found"
    assert _candidates_for(url_b) == 0
    assert _row(row_b)["image_url"] == url_c


def test_candidate_tenant_mismatch_raises_and_touches_nothing():
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    tid_b, group_b = _seed_tenant()
    url_b, fp_b = _seed_object(tid_b, group_b)  # attested to tenant B
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "media_not_ready_reason": None})
    # candidate names tenant B but the row scope resolves to tenant A
    candidate = _candidate_arg(tid_b, group_b, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, expected, patch, candidate, check=False)
    assert done.returncode != 0
    assert "does not match the calendar row tenant" in done.stderr
    assert _candidates_for(url_b) == 0
    assert _row(row_id)["image_url"] == url_a


def test_unattested_candidate_fails_closed_before_any_write():
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b = f"https://scratch.example/{uuid.uuid4().hex}.jpg"  # no attestation
    fp_b = "md5:" + uuid.uuid4().hex
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    done = _call(row_id, tid, expected, patch, candidate, check=False)
    assert done.returncode != 0
    assert "not backed by owner-attested exact bytes" in done.stderr
    assert _row(row_id)["image_url"] == url_a
    assert _occupied(tid) == [_PHASH_A]


def test_non_staging_candidate_evidence_rejected():
    """The candidate must be the same staging-only evidence shape the atomic
    insert writer accepts (kind/stage/usage_claimed/counts_as_use)."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    expected = _expected(row_id)
    patch = json.dumps({"image_url": url_b, "media_not_ready_reason": None})
    for mutate in (lambda c: c.update(kind="scene_use"),
                   lambda c: c.update(stage="used"),
                   lambda c: c.update(usage_claimed=True),
                   lambda c: c.update(counts_as_use=True)):
        bad = json.loads(_candidate_arg(tid, group, _PHASH_B, url_b, fp_b))
        mutate(bad)
        done = _call(row_id, tid, expected, patch, json.dumps(bad),
                     check=False)
        assert done.returncode != 0
        assert "not staging-only evidence" in done.stderr
    assert _row(row_id)["image_url"] == url_a
    assert _candidates_for(url_b) == 0


def test_patch_allowlist_refuses_non_media_columns():
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    expected = _expected(row_id)
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    for bad in ('{"status": "published"}', '{"caption": "hijack"}',
                '{"post_date": "2026-12-25"}', '{"image_url": "%s", "status": "approved"}' % url_b):
        done = _call(row_id, tid, expected, bad, candidate, check=False)
        assert done.returncode != 0
        assert "media identity columns" in done.stderr
    assert _row(row_id)["image_url"] == url_a
    assert _candidates_for(url_b) == 0


def test_stale_then_fresh_retry_patches_cleanly():
    """The rolled-back staging leaves no residue: after a stale attempt the
    caller re-reads and retries with the fresh image, and the SAME candidate
    payload stages again (the rollback left nothing on the canonical
    binding, so the retry is a fresh insert, not a reuse)."""
    tid, group = _seed_tenant()
    url_a, _fpa = _seed_object(tid, group, _PHASH_A)
    row_id = _insert_row(tid, group, url_a)
    url_b, fp_b = _seed_object(tid, group)
    stale = json.loads(_expected(row_id))
    stale["status"] = "approved"  # the row is actually pending
    patch = json.dumps({"image_url": url_b, "source_media_url": url_b,
                        "media_not_ready_reason": None})
    candidate = _candidate_arg(tid, group, _PHASH_B, url_b, fp_b)
    out = json.loads(_call(row_id, tid, json.dumps(stale), patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "stale"
    assert _candidates_for(url_b) == 0
    out = json.loads(_call(row_id, tid, _expected(row_id), patch,
                           candidate).stdout.strip())
    assert out["outcome"] == "patched"
    assert out["candidate_reused"] is False
    assert out["row"]["image_url"] == url_b
    assert _candidates_for(url_b) == 1
