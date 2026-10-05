"""scene_ledger: OPTIONAL real-PostgreSQL claim-prevention checks for the
minimal ported scene ledger (migrations/DRAFT_visual_scene_ledger_20261005.sql,
Child B port of draft 73b39c2 claim-wave core).

These run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_ledger_20261005.sql` exists,
  * ECHO_SCENE_LEDGER_TEST_DSN names a disposable Unix-socket database
    literally named `echo_scene_ledger_test` (never a network or production
    host),
  * local psql is on PATH.

The module fixture applies the prerequisite drafts (visual_group_schema,
visual_global_history, visual_group_claim_trigger, visual_group_backfill,
visual_group_activation) plus the scene-ledger draft, committed, onto that
scratch database, with a stub content_calendar carrying the columns the stack
needs. Every scenario then exercises the INTERNAL claim-path functions
directly as the database owner (EXECUTE is revoked from
public/anon/authenticated/service_role by design; the owner is unaffected),
proving: tenant-scoped occupancy recording on clean claims, legal same-scene
siblings, near_frame/uncertain blocking with durable idempotent holds, the
approval-scoped exemption, fail-closed unbound candidates, the raising guard
writing no holds, append-only immutability, and the 0A000 backfill stub.

Scenario pHashes come from a deterministic Walsh-Hadamard codebook: word_i
(i in 0..15) has bit j (j in 0..63) = parity(i & j), so every distinct pair is
EXACTLY Hamming 32 apart (distinct band, safely outside 7..30) and every word
is exactly 16 hex chars. `_near(word, bits)` flips `bits` low bits for exact
band control (<=6 near_frame, 7..30 uncertain).
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("ECHO_SCENE_LEDGER_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
LEDGER = MIGRATIONS / "DRAFT_visual_scene_ledger_20261005.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_ledger_20261005.sql",
]

pytestmark = pytest.mark.skipif(
    not DSN or not LEDGER.exists() or not PSQL,
    reason="pg scene-ledger checks need ECHO_SCENE_LEDGER_TEST_DSN on a "
           "disposable local DB and the ledger migration present",
)


def _run(statement, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", statement], text=True, capture_output=True, timeout=60)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _sql(statement):
    return _run(statement).stdout.strip()


def _one(statement):
    out = _sql(statement)
    return out.splitlines()[-1] if out else ""


@pytest.fixture(scope="module", autouse=True)
def _scratch_stack():
    if not DSN or not LEDGER.exists():
        pytest.skip("ledger migration or DSN unavailable")
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_ledger_test"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_ledger_test allowed"
    assert _one("select current_database()") == "echo_scene_ledger_test"
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
    # Stub content_calendar with the columns the stack reads/writes
    # (visual_group_key is added by the schema draft's ALTER).
    _sql("create table if not exists public.content_calendar ("
         "id uuid primary key default gen_random_uuid(),"
         "gym_id text, account text, post_date date,"
         "status text, variant_status text,"
         "published_at timestamptz, publish_claim_token uuid,"
         "publish_reservation_day date,"
         "late_post_id text, image_url text, thumbnail_url text,"
         "source_media_url text, source_media_asset_id text,"
         "drive_file_id text, byte_hash text, r2_key text,"
         "media_not_ready_reason text)")
    _sql("create table if not exists public.media_asset ("
         "id text primary key, gym_id text, content_hash text)")
    # Row-state helpers are owned by the claim-trigger draft (applied later
    # in the stack, where CREATE OR REPLACE installs the real versions);
    # inert stubs satisfy the ledger's coverage views at apply time.
    _sql("create or replace function public.visual_group_row_active(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    _sql("create or replace function public.visual_group_row_ambiguous(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    script = ""
    for name in STACK:
        script += (MIGRATIONS / name).read_text() + "\n"
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=180)
    assert done.returncode == 0, done.stderr


@pytest.fixture(autouse=True)
def _isolate_scenarios():
    """Scenario isolation on the scratch DB: clear the scene tables and the
    calendar stub between tests. session_replication_role=replica bypasses
    the immutability and truncate-guard triggers — scratch database only,
    never production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold,"
         " public.visual_scene_phash_occupied, public.visual_scene_candidate,"
         " public.visual_global_usage, public.visual_global_usage_member,"
         " public.visual_group_usage_ledger,"
         " public.visual_group_usage_sibling,"
         " public.visual_global_object_lineage,"
         " public.visual_global_scene_object_member,"
         " public.visual_global_object_attestation,"
         " public.visual_global_render_receipt,"
         " public.visual_global_object_read_receipt,"
         " public.visual_group_alias, public.visual_group,"
         " public.tenant_alias cascade")


# ---- deterministic pHash codebook -------------------------------------------

def _word(i):
    bits = 0
    for j in range(64):
        if bin(i & j).count("1") % 2:
            bits |= (1 << j)
    return f"{bits:016x}"


_CODEBOOK = [_word(i) for i in range(16)]


def _near(word, bits):
    """Flip `bits` low bits for exact hamming-band control."""
    return f"{int(word, 16) ^ ((1 << bits) - 1):016x}"


# ---- seeding helpers ----------------------------------------------------------

def _seed_tenant():
    """Canonical tenant + visual group + armed guard on the scratch DB."""
    tid = str(uuid.uuid4())
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    _sql(f"select public.visual_group_activate_guard('{tid}', 'scene-ledger-test')")
    return tid, group


def _seed_object(tid, group, phash=None):
    """Fully attested delivered object: read receipt -> attestation ->
    canonical_url alias -> (when phash given) bound 'display' candidate."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'scene-ledger-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{receipt}',"
         " 'scene-ledger-test')")
    for role in ("source", "delivered"):
        _sql("insert into public.visual_global_scene_object_member"
             "(tenant_id, group_key, exact_url, fingerprint, object_role) "
             f"values ('{tid}', '{group}', '{url}', '{fp}', '{role}')")
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"('{tid}', 'canonical_url', '{url}', '{group}')")
    cand = None
    if phash is not None:
        cand = _one(
            "select public.visual_scene_register_candidate("
            f"'{tid}', '{group}', '{phash}', '{url}', '{fp}',"
            f" jsonb_build_object('verified_bytes', '{fp}'),"
            " 'scene-ledger-test', 'display')")
    return url, fp, cand


def _insert_row(tid, group, url, date="2026-10-10"):
    return _one(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, source_media_url, visual_group_key) values "
        f"('{tid}', 'ig', '{date}', 'pending', 'active',"
        f" '{url}', '{url}', '{group}') returning id")


def _decide(row_id, cand):
    return json.loads(_one(
        "select public.visual_scene_claim_decide(c, "
        f"'{cand}'::uuid)::text from public.content_calendar c "
        f"where c.id = '{row_id}'"))


def _occupied_count():
    return int(_one("select count(*) from public.visual_scene_phash_occupied"))


def _holds(state=None):
    where = f"where state = '{state}'" if state else ""
    return [json.loads(r) for r in _sql(
        "select jsonb_build_object('hold_id', hold_id, 'state', state,"
        " 'hold_kind', hold_kind, 'hamming', hamming)::text"
        f" from public.visual_scene_review_hold {where}"
        " order by created_at").splitlines() if r]


# ---- tests --------------------------------------------------------------------

def test_hamming_pure():
    assert _one("select public.visual_scene_hamming('0000000000000000',"
                " '0000000000000000')") == "0"
    assert _one("select public.visual_scene_hamming('0000000000000000',"
                " 'ffffffffffffffff')") == "64"
    assert _one("select public.visual_scene_hamming('0000000000000000',"
                " 'aaaaaaaaaaaaaaaa')") == "32"
    assert _one("select public.visual_scene_hamming(null, 'aaaaaaaaaaaaaaaa')") == ""
    assert _one("select public.visual_scene_hamming('zz', 'aaaaaaaaaaaaaaaa')") == ""


def test_codebook_words_are_exactly_16_hex_and_32_apart():
    assert len(_CODEBOOK) == 16
    assert _CODEBOOK[0] == "0000000000000000"
    assert _CODEBOOK[1] == "aaaaaaaaaaaaaaaa"
    for w in _CODEBOOK:
        assert re.fullmatch(r"[0-9a-f]{16}", w)


def test_register_candidate_requires_attested_bytes():
    tid, group = _seed_tenant()
    # Unattested object (evidence verified_bytes matches the fingerprint, but
    # no attestation row exists): registration must raise 23514.
    fp = "md5:" + uuid.uuid4().hex
    bad = _run(
        "select public.visual_scene_register_candidate("
        f"'{tid}', '{group}', '{_CODEBOOK[0]}',"
        " 'https://scratch.example/unattested.jpg',"
        f" '{fp}',"
        f" jsonb_build_object('verified_bytes', '{fp}'),"
        " 'scene-ledger-test', 'display')", check=False)
    assert bad.returncode != 0
    assert "not backed by owner-attested exact bytes" in bad.stderr
    # Malformed phash must raise 22023.
    bad = _run(
        "select public.visual_scene_register_candidate("
        f"'{tid}', '{group}', 'not-a-phash',"
        " 'https://scratch.example/x.jpg',"
        f" 'md5:{uuid.uuid4().hex}',"
        " jsonb_build_object('verified_bytes', 'md5:" + uuid.uuid4().hex + "'),"
        " 'scene-ledger-test', 'display')", check=False)
    assert bad.returncode != 0
    # Registration NEVER consumes a scene.
    _, _, cand = _seed_object(tid, group, _CODEBOOK[0])
    assert cand
    assert _occupied_count() == 0
    assert _holds() == []


def test_clean_claim_records_occupancy_and_legal_siblings():
    tid, group = _seed_tenant()
    url, fp, cand = _seed_object(tid, group, _CODEBOOK[1])
    row = _insert_row(tid, group, url)
    out = _decide(row, cand)
    assert out["decision"] == "claimed"
    assert out["phash"].strip() == _CODEBOOK[1]
    assert _occupied_count() == 1
    assert _holds() == []
    # Same-tenant same-date same-group sibling with the SAME scene phash is
    # legal: claimed, occupancy re-record is an idempotent no-op.
    row2 = _insert_row(tid, group, url)
    out2 = _decide(row2, cand)
    assert out2["decision"] == "claimed"
    assert _occupied_count() == 1
    # Retry of the first claim is likewise idempotent.
    out3 = _decide(row, cand)
    assert out3["decision"] == "claimed"
    assert _occupied_count() == 1


def test_cross_tenant_near_frame_blocks_with_durable_idempotent_hold():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[2])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"
    # Tenant B claims a 2-bit-away (near_frame) scene on the same date.
    url_b, _, cand_b = _seed_object(tid_b, group_b, _near(_CODEBOOK[2], 2))
    row_b = _insert_row(tid_b, group_b, url_b)
    out = _decide(row_b, cand_b)
    assert out["decision"] == "blocked"
    assert out["reason"] == "near_frame_conflict"
    assert len(out["hold_ids"]) == 1
    holds = _holds("open")
    assert len(holds) == 1 and holds[0]["hold_kind"] == "near_frame"
    assert holds[0]["hamming"] == 2
    # No occupancy recorded for the blocked claim.
    assert _occupied_count() == 1
    # Retry inserts NO duplicate open hold (stable uniqueness key).
    out2 = _decide(row_b, cand_b)
    assert out2["decision"] == "blocked"
    assert out2["hold_ids"] == []
    assert len(_holds("open")) == 1


def test_same_tenant_different_date_near_frame_blocks():
    tid, group = _seed_tenant()
    url1, _, cand1 = _seed_object(tid, group, _CODEBOOK[3])
    row1 = _insert_row(tid, group, url1, date="2026-10-10")
    assert _decide(row1, cand1)["decision"] == "claimed"
    # Same tenant, DIFFERENT group (the exact-byte ledger already owns
    # same-group cross-date conflicts), different date, 4-bit-away scene.
    group2 = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group2}')")
    url2, _, cand2 = _seed_object(tid, group2, _near(_CODEBOOK[3], 4))
    row2 = _insert_row(tid, group2, url2, date="2026-10-11")
    out = _decide(row2, cand2)
    assert out["decision"] == "blocked"
    assert out["reason"] == "near_frame_conflict"
    assert _occupied_count() == 1


def test_uncertain_band_blocks_with_review_hold():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[4])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"
    # 20-bit-away scene: uncertain band (7..30) -> blocked with review hold.
    url_b, _, cand_b = _seed_object(tid_b, group_b, _near(_CODEBOOK[4], 20))
    row_b = _insert_row(tid_b, group_b, url_b)
    out = _decide(row_b, cand_b)
    assert out["decision"] == "blocked"
    assert out["reason"] == "uncertain_match_review"
    holds = _holds("open")
    assert len(holds) == 1 and holds[0]["hold_kind"] == "uncertain"
    assert holds[0]["hamming"] == 20
    assert _occupied_count() == 1


def test_distinct_band_claims_cleanly():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[5])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"
    # Codebook pairs are exactly 32 apart (> 30): no conflict at all.
    url_b, _, cand_b = _seed_object(tid_b, group_b, _CODEBOOK[6])
    row_b = _insert_row(tid_b, group_b, url_b)
    out = _decide(row_b, cand_b)
    assert out["decision"] == "claimed"
    assert _occupied_count() == 2
    assert _holds() == []


def test_approved_hold_exempts_only_exact_reviewed_scope():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[7])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"
    url_b, _, cand_b = _seed_object(tid_b, group_b, _near(_CODEBOOK[7], 3))
    row_b = _insert_row(tid_b, group_b, url_b, date="2026-10-12")
    out = _decide(row_b, cand_b)
    assert out["decision"] == "blocked"
    hold_id = out["hold_ids"][0]
    # Approve via the one-shot trigger-permitted open->terminal transition
    # (named actor + non-empty evidence), mirroring what the validated review
    # RPC performs after its drift checks.
    _sql("update public.visual_scene_review_hold set state='approved',"
         " resolved_by='scene-ledger-test', resolved_at=now(),"
         " resolution_evidence=jsonb_build_object('review','ok')"
         f" where hold_id='{hold_id}'")
    # Exact reviewed scope for the exact claim date: now claimed.
    out2 = _decide(row_b, cand_b)
    assert out2["decision"] == "claimed"
    assert _occupied_count() == 2
    # A LATER date for the same pair is NOT exempted: still blocked. Use a
    # different group in tenant B (the exact-byte ledger already owns
    # same-group cross-date conflicts).
    group_b2 = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid_b}', '{group_b2}')")
    url_b2, _, cand_b2 = _seed_object(tid_b, group_b2, _near(_CODEBOOK[7], 3))
    row_b2 = _insert_row(tid_b, group_b2, url_b2, date="2026-10-13")
    out3 = _decide(row_b2, cand_b2)
    assert out3["decision"] == "blocked"
    # A resolved hold never reopens: a second terminal transition raises.
    bad = _run("update public.visual_scene_review_hold set state='rejected',"
               " resolved_by='x', resolved_at=now(),"
               " resolution_evidence=jsonb_build_object('r','x')"
               f" where hold_id='{hold_id}'", check=False)
    assert bad.returncode != 0


def test_unbound_candidate_fails_closed():
    tid, group = _seed_tenant()
    url, fp, _ = _seed_object(tid, group)  # no candidate staged
    row = _insert_row(tid, group, url)
    out = _decide(row, uuid.uuid4())
    assert out["decision"] == "blocked"
    assert out["reason"] == "fail_closed_no_bound_candidate"
    assert out["hold_ids"] == []
    assert _occupied_count() == 0
    assert _holds() == []


def test_raising_guard_writes_no_hold_on_conflict():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[8])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"
    url_b, _, cand_b = _seed_object(tid_b, group_b, _near(_CODEBOOK[8], 1))
    row_b = _insert_row(tid_b, group_b, url_b)
    bad = _run(
        "select public.visual_scene_claim_guard(c, "
        f"'{cand_b}'::uuid)::text from public.content_calendar c "
        f"where c.id = '{row_b}'", check=False)
    assert bad.returncode != 0
    assert "near-frame conflict" in bad.stderr
    # Raising guard: no hold, no occupancy for the blocked claim.
    assert _holds() == []
    assert _occupied_count() == 1


def test_raising_guard_clean_claim_records_occupancy():
    tid, group = _seed_tenant()
    url, _, cand = _seed_object(tid, group, _CODEBOOK[9])
    row = _insert_row(tid, group, url)
    out = json.loads(_one(
        "select public.visual_scene_claim_guard(c, "
        f"'{cand}'::uuid)::text from public.content_calendar c "
        f"where c.id = '{row}'"))
    assert out["verdict"] == "claimed"
    assert _occupied_count() == 1


def test_append_only_immutability():
    tid, group = _seed_tenant()
    url, _, cand = _seed_object(tid, group, _CODEBOOK[10])
    row = _insert_row(tid, group, url)
    assert _decide(row, cand)["decision"] == "claimed"
    # Occupied rows are permanent: UPDATE and DELETE both raise 23514.
    bad = _run("update public.visual_scene_phash_occupied"
               " set evidence = '{}'::jsonb", check=False)
    assert bad.returncode != 0
    bad = _run("delete from public.visual_scene_phash_occupied", check=False)
    assert bad.returncode != 0
    # Candidates are append-only too.
    bad = _run("update public.visual_scene_candidate"
               f" set phash = '{_CODEBOOK[11]}'", check=False)
    assert bad.returncode != 0
    bad = _run("delete from public.visual_scene_candidate", check=False)
    assert bad.returncode != 0
    assert _occupied_count() == 1


def test_holds_are_permanent_and_open_holds_cannot_be_edited():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[12])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"
    url_b, _, cand_b = _seed_object(tid_b, group_b, _near(_CODEBOOK[12], 2))
    row_b = _insert_row(tid_b, group_b, url_b)
    out = _decide(row_b, cand_b)
    assert out["decision"] == "blocked"
    hold_id = out["hold_ids"][0]
    # DELETE is refused; identity columns on an open hold are immutable.
    bad = _run("delete from public.visual_scene_review_hold"
               f" where hold_id = '{hold_id}'", check=False)
    assert bad.returncode != 0
    bad = _run("update public.visual_scene_review_hold set hamming = 99"
               f" where hold_id = '{hold_id}'", check=False)
    assert bad.returncode != 0
    assert len(_holds("open")) == 1


def test_backfill_stub_fails_closed():
    bad = _run("select public.visual_scene_backfill_occupied()", check=False)
    assert bad.returncode != 0
    assert "0A000" in bad.stderr or "activation-draft deliverable" in bad.stderr


def test_hold_resolve_validates_inputs_and_missing_hold():
    bad = _run("select public.visual_scene_hold_resolve("
               f"'{uuid.uuid4()}'::uuid, 'approved', 'reviewer',"
               " jsonb_build_object('r','x'))", check=False)
    assert bad.returncode != 0
    assert "scene hold not found" in bad.stderr
    bad = _run("select public.visual_scene_hold_resolve("
               f"'{uuid.uuid4()}'::uuid, 'maybe', 'reviewer',"
               " jsonb_build_object('r','x'))", check=False)
    assert bad.returncode != 0
    assert "decision approved|rejected" in bad.stderr


def test_hold_resolve_ignores_unrelated_same_role_candidate_for_other_url():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _, cand_a = _seed_object(tid_a, group_a, _CODEBOOK[13])
    row_a = _insert_row(tid_a, group_a, url_a)
    assert _decide(row_a, cand_a)["decision"] == "claimed"

    url_b, fp_b, cand_b = _seed_object(
        tid_b, group_b, _near(_CODEBOOK[13], 3))
    row_b = _insert_row(tid_b, group_b, url_b, date="2026-10-12")
    blocked = _decide(row_b, cand_b)
    assert blocked["decision"] == "blocked"
    assert blocked["reason"] == "near_frame_conflict"
    hold_id = blocked["hold_ids"][0]
    _sql("update public.content_calendar set status='pending',"
         "variant_status='archived', media_not_ready_reason='scene_review_hold'"
         f" where id='{row_b}'")

    # Same tenant, group, and role as the reviewed candidate, but bound to a
    # distinct attested URL. This must not make the reviewed candidate
    # ambiguous or invalidate the hold's exact row binding.
    distractor_url, distractor_fp, distractor = _seed_object(
        tid_b, group_b, _CODEBOOK[14])
    assert distractor_url != url_b and distractor_fp != fp_b
    assert distractor != cand_b

    resolved = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_id}'::uuid, 'approved', 'scene-ledger-reviewer',"
        " jsonb_build_object('review','unrelated same-role candidate'))::text"))
    assert resolved["state"] == "approved"
    assert resolved["exemption_scope"]["candidate_id"] == cand_b
    assert resolved["exemption_scope"]["exact_url"] == url_b
    # A different candidate cannot consume the approval exemption.
    other_row = _insert_row(tid_b, group_b, distractor_url,
                            date="2026-10-12")
    other = _decide(other_row, distractor)
    assert other["decision"] == "claimed"
    assert _occupied_count() == 2


def test_internal_claim_paths_revoked_from_service_role():
    for fn in ("visual_scene_claim_scan(public.content_calendar,uuid)",
               "visual_scene_claim_decide(public.content_calendar,uuid)",
               "visual_scene_claim_guard(public.content_calendar,uuid)",
               "visual_scene_row_candidate(public.content_calendar)",
               "visual_scene_write_holds(text,text,date,uuid,text,uuid,"
               "char(16),text,text,jsonb)",
               "visual_scene_backfill_occupied()"):
        assert _one(
            "select has_function_privilege('service_role',"
            f" 'public.{fn}', 'execute')") == "f", fn
    # Safe read/review entry points stay service_role-executable.
    for fn in ("visual_scene_hamming(text,text)",
               "visual_scene_register_candidate(text,text,text,text,text,"
               "jsonb,text,text)",
               "visual_scene_hold_resolve(uuid,text,text,jsonb)"):
        assert _one(
            "select has_function_privilege('service_role',"
            f" 'public.{fn}', 'execute')") == "t", fn
