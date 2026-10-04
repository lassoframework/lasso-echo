"""scene_claim_wave: OPTIONAL real-PostgreSQL committed-held-row checks.

These run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_claim_wave_20261003.sql` exists,
  * VISUAL_SCENE_WAVE_TEST_DSN names a disposable Unix-socket database
    literally named `echo_scene_wave_test` (same house rule as
    tests/test_visual_writer_bundle_pg.py — never a network or production
    host),
  * local psql is on PATH.

These scenarios pin the committed-held-row contract (2026-10-04 P0 repair
pass): a hold row exists IF AND ONLY IF the claim transaction commits in a
blocked/held state — realized by the BEFORE trigger mutating NEW to the
held state and RETURNING NEW so the held row COMMITS with its holds. Every
statement below is committed to the disposable scratch database — never
anywhere else (the DSN guard refuses anything but a local-socket DB
literally named `echo_scene_wave_test`). The module fixture applies the
prereq drafts (visual_group_schema, visual_global_history,
visual_group_claim_trigger) and the wave draft, committed, onto that
scratch database, with a stub content_calendar carrying the columns the
trigger chain needs.

Scenario pHashes come from a deterministic Walsh-Hadamard codebook: word_i
(i in 0..15) has bit j (j in 0..63) = parity(i & j), so every distinct pair
is EXACTLY Hamming 32 apart (distinct band, safely outside 7..30) and every
word is exactly 16 hex chars. `_near(word, bits)` flips `bits` low bits for
exact band control (<=6 near_frame, 7..30 uncertain). Allocation is a
bounded iterator — an assert fires if exhausted; there is no loop-search.
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("VISUAL_SCENE_WAVE_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
WAVE = MIGRATIONS / "DRAFT_visual_scene_claim_wave_20261003.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
]

pytestmark = pytest.mark.skipif(
    not DSN or not WAVE.exists() or not PSQL,
    reason="pg committed-held-row checks need VISUAL_SCENE_WAVE_TEST_DSN on "
           "a disposable local DB and the wave migration present",
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
    if not DSN or not WAVE.exists():
        pytest.skip("wave migration or DSN unavailable")
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_wave_test"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_wave_test allowed"
    assert _one("select current_database()") == "echo_scene_wave_test"
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
    # Stub content_calendar with the columns the trigger chain reads/writes
    # (visual_group_key is added by the schema draft's ALTER).
    _sql("create table if not exists public.content_calendar ("
         "id uuid primary key default gen_random_uuid(),"
         "gym_id text, account text, post_date date,"
         "status text, variant_status text,"
         "published_at timestamptz, publish_claim_token uuid,"
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
    """Scenario isolation on the scratch DB: clear the wave scene tables and
    the calendar between tests. session_replication_role=replica bypasses
    the immutability triggers — scratch database only, never production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold, "
         "public.visual_scene_phash_occupied, public.visual_scene_candidate, "
         "public.visual_global_usage, public.visual_global_usage_member, "
         "public.visual_group_usage_ledger, "
         "public.visual_group_usage_sibling cascade")


# ---- deterministic Walsh-Hadamard pHash codebook -----------------------------

def _codebook():
    """word_i bit j = parity(i & j); distinct pairs are exactly 32 apart."""
    words = []
    for i in range(16):
        word = sum((bin(i & j).count("1") & 1) << j for j in range(64))
        words.append(format(word, "016x"))
    assert all(len(w) == 16 for w in words)
    for a in range(16):
        for b in range(a + 1, 16):
            dist = int(words[a], 16) ^ int(words[b], 16)
            assert bin(dist).count("1") == 32
    return words


_CODEBOOK = _codebook()
_phash_next = 0


def _phash():
    """Bounded allocator: hands out the next codebook word; never searches."""
    global _phash_next
    assert _phash_next < len(_CODEBOOK), "scratch phash codebook exhausted"
    word = _CODEBOOK[_phash_next]
    _phash_next += 1
    return word


def _near(phash, bits):
    """Same word with `bits` low bits flipped: exact Hamming distance `bits`
    (<=6 near_frame band, 7..30 uncertain band)."""
    return format(int(phash, 16) ^ ((1 << bits) - 1), "016x")


def test_codebook_words_are_exactly_16_hex_and_32_apart():
    assert len(_CODEBOOK) == 16
    assert _CODEBOOK[0] == "0000000000000000"
    assert _CODEBOOK[1] == "aaaaaaaaaaaaaaaa"


# ---- seeding helpers ----------------------------------------------------------

def _seed_tenant():
    """Armed canonical tenant + visual group on the scratch DB. Committed."""
    tid = str(uuid.uuid4())
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    # Arm enforcement through the activation draft's own RPC (write barrier,
    # coverage re-read, arming receipt — the only sanctioned arming path).
    _sql(f"select public.visual_group_activate_guard('{tid}', 'scene-wave-test')")
    return tid, group


def _seed_object(tid, group, phash=None):
    """Fully attested delivered object for the scene: read receipt ->
    attestation -> source+delivered scene members -> canonical_url alias ->
    (when phash given) bound 'display' candidate. Returns (url, fingerprint,
    candidate_id|None)."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'scene-wave-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{receipt}',"
         " 'scene-wave-test')")
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
            " 'scene-wave-test', 'display')")
    return url, fp, cand


def _insert_row(tid, group, url, date="2026-10-10"):
    return _one(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, source_media_url, visual_group_key) values "
        f"('{tid}', 'ig', '{date}', 'pending', 'active',"
        f" '{url}', '{url}', '{group}') returning id")


def _row(row_id):
    out = _one(
        "select jsonb_build_object('status', status,"
        " 'variant_status', variant_status,"
        " 'media_not_ready_reason', media_not_ready_reason,"
        " 'publish_claim_token', publish_claim_token,"
        " 'visual_group_key', visual_group_key)::text "
        f"from public.content_calendar where id = '{row_id}'")
    return json.loads(out)


def _holds(tid):
    return json.loads(_one(
        "select coalesce(jsonb_agg(jsonb_build_object('hold_id', hold_id,"
        " 'hold_kind', hold_kind, 'state', state,"
        " 'candidate_phash', candidate_phash,"
        " 'matched_phash', matched_phash) order by created_at), '[]'::jsonb) "
        f"from public.visual_scene_review_hold where tenant_id = '{tid}'"))


def _occupied(tid):
    return json.loads(_one(
        "select coalesce(jsonb_agg(phash), '[]'::jsonb) "
        f"from public.visual_scene_phash_occupied where tenant_id = '{tid}'"))


# ---- scenarios: real committed transactions -----------------------------------

def test_stack_applies_and_claim_path_functions_exist():
    out = _one("select string_agg(proname, ',' order by proname) from pg_proc "
               "where proname in ('visual_scene_claim_scan',"
               "'visual_scene_claim_decide','visual_scene_claim_guard',"
               "'visual_scene_row_candidate','visual_scene_write_holds',"
               "'visual_scene_calendar_claim_guard',"
               "'visual_scene_hold_resolve','visual_scene_hold_reactivate',"
               "'visual_scene_publish_claim_guarded',"
               "'visual_scene_approval_guarded')")
    assert out == ("visual_scene_approval_guarded,"
                   "visual_scene_calendar_claim_guard,"
                   "visual_scene_claim_decide,visual_scene_claim_guard,"
                   "visual_scene_claim_scan,visual_scene_hold_reactivate,"
                   "visual_scene_hold_resolve,"
                   "visual_scene_publish_claim_guarded,"
                   "visual_scene_row_candidate,visual_scene_write_holds")


def test_clean_claim_commits_active_row_and_records_occupancy():
    tid, group = _seed_tenant()
    url, _fp, _cand = _seed_object(tid, group, _phash())
    row_id = _insert_row(tid, group, url)
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert len(_occupied(tid)) == 1
    assert _holds(tid) == []
    assert _one("select public.visual_scene_approval_guarded("
                f"'{row_id}')") == "t"


def _conflicting_row(bits):
    """Tenant A clean-claims a scene; tenant B inserts a row whose bound
    candidate is `bits` away. Returns (tid_b, row_b, decision evidence)."""
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fpa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)
    assert len(_occupied(tid_a)) == 1
    tid_b, group_b = _seed_tenant()
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, _near(base, bits))
    return tid_a, tid_b, group_b, url_b, base


def test_cross_tenant_near_match_commits_held_row_with_durable_hold():
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)  # COMMITS as held
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["publish_claim_token"] is None
    holds = _holds(tid_b)
    assert len(holds) == 1
    assert holds[0]["hold_kind"] == "near_frame"
    assert holds[0]["state"] == "open"
    # no occupancy and no false "used" mark for the rejected claim
    assert _occupied(tid_b) == []
    # persisted held row: publish claim denied, approval denied
    assert _one("select public.visual_scene_publish_claim_guarded("
                f"'{row_id}') is null") == "t"
    assert _one("select public.visual_scene_approval_guarded("
                f"'{row_id}')") == "f"


def test_same_tenant_other_date_near_match_commits_held_row():
    tid, group = _seed_tenant()
    base = _phash()
    url1, _f1, _c1 = _seed_object(tid, group, base)
    _insert_row(tid, group, url1, date="2026-10-10")
    url2, _f2, _c2 = _seed_object(tid, group, _near(base, 1))
    row_id = _insert_row(tid, group, url2, date="2026-10-11")
    row = _row(row_id)
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert len(_holds(tid)) == 1
    assert _occupied(tid) == [base]


def test_uncertain_band_commits_held_row_with_uncertain_hold():
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(12)  # 7..30 band
    row_id = _insert_row(tid_b, group_b, url_b)
    row = _row(row_id)
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    holds = _holds(tid_b)
    assert len(holds) == 1 and holds[0]["hold_kind"] == "uncertain"
    assert _occupied(tid_b) == []


def test_retry_of_same_conflict_inserts_no_duplicate_hold():
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(3)
    row_id = _insert_row(tid_b, group_b, url_b)
    assert len(_holds(tid_b)) == 1
    # Re-running the decision for the same row (caller retry) must not
    # duplicate the open hold (stable open-hold uniqueness key).
    for _ in range(2):
        decision = json.loads(_one(
            "select public.visual_scene_claim_decide(c, c2.candidate_id)::text "
            "from public.content_calendar c "
            "join public.visual_scene_candidate c2 "
            "  on c2.tenant_id = public.visual_group_tenant_id(c.gym_id)::text "
            " and c2.group_key = c.visual_group_key "
            " and c2.object_role = 'display' and c2.exact_url = c.image_url "
            f"where c.id = '{row_id}'"))
        assert decision["decision"] == "blocked"
        assert decision["hold_ids"] == []
        assert len(_holds(tid_b)) == 1


def test_same_tenant_same_date_same_group_sibling_is_legal():
    tid, group = _seed_tenant()
    base = _phash()
    url1, _f1, _c1 = _seed_object(tid, group, base)
    _insert_row(tid, group, url1)
    url2, _f2, _c2 = _seed_object(tid, group, _near(base, 4))
    row_id = _insert_row(tid, group, url2)
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert sorted(_occupied(tid)) == sorted([base, _near(base, 4)])
    assert _holds(tid) == []


def test_scene_with_candidates_but_unbound_row_object_hard_rejects():
    tid, group = _seed_tenant()
    _seed_object(tid, group, _phash())          # scene HAS a staged candidate
    url2, _f2, _c2 = _seed_object(tid, group)   # attested object, NO candidate
    done = _run(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, source_media_url, visual_group_key) values "
        f"('{tid}', 'ig', '2026-10-10', 'pending', 'active',"
        f" '{url2}', '{url2}', '{group}') returning id", check=False)
    assert done.returncode != 0
    assert "no scene candidate bound to this row" in done.stderr
    assert _one("select count(*) from public.content_calendar "
                f"where gym_id = '{tid}'") == "0"
    assert _holds(tid) == [] and _occupied(tid) == []


def test_claim_path_execute_revoked_from_service_role():
    tid, group = _seed_tenant()
    url, _fp, cand = _seed_object(tid, group, _phash())
    row_id = _insert_row(tid, group, url)
    for fn in ("visual_scene_claim_scan", "visual_scene_claim_decide",
               "visual_scene_claim_guard"):
        done = _run(
            "set role service_role; "
            f"select public.{fn}(c, '{cand}') "
            f"from public.content_calendar c where c.id = '{row_id}'",
            check=False)
        assert done.returncode != 0
        assert "permission denied" in done.stderr
    # service_role CAN use the safe review surfaces
    assert _one("set role service_role; select public.visual_scene_hamming("
                "'0000000000000000', 'ffffffffffffffff')") == "64"
    assert _one("set role service_role; select count(*) >= 0 "
                "from public.visual_scene_review_hold") == "t"


def test_hold_resolve_validates_then_approves_once():
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    for stmt, msg in (
            (f"'{hold_id}', 'approved', '', '{{}}'", "named actor and evidence"),
            (f"'{hold_id}', 'approved', 'reviewer', '{{}}'::jsonb",
             "named actor and evidence"),
            (f"'{hold_id}', 'maybe', 'reviewer',"
             " jsonb_build_object('notes','x')", "named actor and evidence")):
        done = _run(f"select public.visual_scene_hold_resolve({stmt})",
                    check=False)
        assert done.returncode != 0
        assert msg in done.stderr
    out = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_id}', 'approved', 'reviewer',"
        " jsonb_build_object('notes', 'owner confirmed reuse'))::text"))
    assert out["state"] == "approved"
    assert out["exemption_scope"]["candidate_phash"]
    # one-shot: a second resolution is refused
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold_id}', 'rejected', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "scene hold is already resolved" in done.stderr


def test_approved_resolve_and_reactivate_records_occupancy_for_exempted_pair():
    _ta, tid_b, group_b, url_b, base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    _one("select public.visual_scene_hold_resolve("
         f"'{hold_id}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed'))")
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold_id}', 'reviewer')::text"))
    assert out["reactivated"] is True
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    # occupancy recorded for the exempted pair only: tenant B's candidate
    # phash, never a blanket approval
    assert _occupied(tid_b) == [_near(base, 2)]
    assert _one("select public.visual_scene_approval_guarded("
                f"'{row_id}')") == "t"


def test_open_hold_reactivation_is_refused():
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    done = _run("select public.visual_scene_hold_reactivate("
                f"'{hold_id}', 'reviewer')", check=False)
    assert done.returncode != 0
    assert "only an approved scene hold can reactivate" in done.stderr


def test_row_delete_denial_never_frees_occupancy():
    tid, group = _seed_tenant()
    base = _phash()
    url, _fp, _cand = _seed_object(tid, group, base)
    row_id = _insert_row(tid, group, url)
    assert _occupied(tid) == [base]
    _sql(f"delete from public.content_calendar where id = '{row_id}'")
    assert _one("select count(*) from public.content_calendar "
                f"where id = '{row_id}'") == "0"
    # occupancy is PERMANENT across denial/swap
    assert _occupied(tid) == [base]
