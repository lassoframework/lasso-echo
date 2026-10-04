"""scene_claim_wave: OPTIONAL real-PostgreSQL committed-held-row checks.

These run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_claim_wave_20261003.sql` exists,
  * VISUAL_SCENE_WAVE_TEST_DSN names a disposable Unix-socket database
    literally named `echo_scene_wave_test` (same house rule as
    tests/test_visual_writer_bundle_pg.py — never a network or production
    host),
  * local psql is on PATH.

These scenarios pin the committed-held-row contract (2026-10-04 P0 repair
pass, wave-3 architecture): a hold row exists IF AND ONLY IF the claim
transaction commits in a blocked/held state — realized by the MERGED
exact-byte guard (visual_group_guard_trigger, the ONE authoritative
content_calendar BEFORE trigger since the separate early scene trigger was
removed in wave 3) mutating NEW to the held state, writing the hold LAST
(after visual_group_sync_row, with nothing raising after the hold insert)
and RETURNING NEW so the held row COMMITS with its holds. Every
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

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("VISUAL_SCENE_WAVE_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
WAVE = MIGRATIONS / "DRAFT_visual_scene_claim_wave_20261003.sql"
HISTORY_BACKFILL = MIGRATIONS / "DRAFT_visual_scene_history_backfill_20261004.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_history_backfill_20261004.sql",
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
    """Scenario isolation on the scratch DB: clear the wave scene tables and
    the calendar between tests. session_replication_role=replica bypasses
    the immutability triggers — scratch database only, never production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold, "
         "public.visual_scene_phash_occupied, public.visual_scene_candidate, "
         "public.visual_global_usage, public.visual_global_usage_member, "
         "public.visual_group_usage_ledger, "
         "public.visual_group_usage_sibling cascade")
    # each test re-seeds from an empty scratch DB, so the bounded codebook
    # allocator restarts per scenario
    global _phash_next
    _phash_next = 0


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


def _insert_row_ex(tid, group, url, date="2026-10-10", keyed=True,
                   reservation=None, thumbnail=None, source=None, gym=None):
    """Generalized committed row insert: `keyed=False` leaves
    visual_group_key NULL so the wave-2 trigger must resolve the group
    itself; `reservation` pre-sets publish_reservation_day; `thumbnail`
    makes the row a video row (distinct poster object); `gym` overrides
    the raw calendar key (raw alias scenarios)."""
    cols = "gym_id, account, post_date, status, variant_status," \
        " image_url, source_media_url"
    vals = (f"'{gym or tid}', 'ig', '{date}', 'pending', 'active',"
            f" '{url}', '{source or url}'")
    if keyed:
        cols += ", visual_group_key"
        vals += f", '{group}'"
    if reservation:
        cols += ", publish_reservation_day"
        vals += f", '{reservation}'"
    if thumbnail:
        cols += ", thumbnail_url"
        vals += f", '{thumbnail}'"
    return _one(f"insert into public.content_calendar({cols}) "
                f"values ({vals}) returning id")


def _seed_poster(tid, group, image_url, image_fp, phash=None):
    """Fully attested poster (video thumbnail) object for the scene: read
    receipt -> attestation -> delivered member -> render receipt -> lineage
    edge image->poster -> (when phash given) bound 'poster' candidate on the
    exact thumbnail URL. Returns (poster_url, poster_fp, candidate_id|None)."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    image_receipt = _one(
        "select receipt_id from public.visual_global_object_read_receipt "
        f"where tenant_id = '{tid}' and exact_url = '{image_url}' "
        f"and fingerprint = '{image_fp}'")
    receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 512, 'verified_object_read',"
        f" 'scratch-evidence', 'scene-wave-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"('{url}', '{tid}', '{group}', '{fp}', 512,"
         f" 'verified_object_read', 'scratch-evidence', '{receipt}',"
         " 'scene-wave-test')")
    _sql("insert into public.visual_global_scene_object_member"
         "(tenant_id, group_key, exact_url, fingerprint, object_role) "
         f"values ('{tid}', '{group}', '{url}', '{fp}', 'delivered')")
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"('{tid}', 'canonical_url', '{url}', '{group}')")
    render = _one(
        "insert into public.visual_global_render_receipt"
        "(tenant_id, source_read_receipt, delivered_read_receipt,"
        " source_exact_url, delivered_exact_url,"
        " source_fingerprint, delivered_fingerprint,"
        " operation, evidence_ref, rendered_by) values "
        f"('{tid}', '{image_receipt}', '{receipt}',"
        f" '{image_url}', '{url}', '{image_fp}', '{fp}',"
        " 'render', 'scratch-evidence', 'scene-wave-test')"
        " returning receipt_id")
    _sql("insert into public.visual_global_object_lineage"
         "(tenant_id, group_key, source_exact_url, delivered_exact_url,"
         " source_fingerprint, delivered_fingerprint, render_receipt) values "
         f"('{tid}', '{group}', '{image_url}', '{url}',"
         f" '{image_fp}', '{fp}', '{render}')")
    cand = None
    if phash is not None:
        cand = _one(
            "select public.visual_scene_register_candidate("
            f"'{tid}', '{group}', '{phash}', '{url}', '{fp}',"
            f" jsonb_build_object('verified_bytes', '{fp}'),"
            " 'scene-wave-test', 'poster')")
    return url, fp, cand


def _row(row_id):
    out = _one(
        "select jsonb_build_object('status', status,"
        " 'variant_status', variant_status,"
        " 'media_not_ready_reason', media_not_ready_reason,"
        " 'publish_claim_token', publish_claim_token,"
        " 'publish_reservation_day', publish_reservation_day,"
        " 'visual_group_key', visual_group_key)::text "
        f"from public.content_calendar where id = '{row_id}'")
    return json.loads(out)


def _holds(tid):
    return json.loads(_one(
        "select coalesce(jsonb_agg(jsonb_build_object('hold_id', hold_id,"
        " 'hold_kind', hold_kind, 'state', state,"
        " 'candidate_phash', candidate_phash,"
        " 'exact_url', exact_url, 'fingerprint', fingerprint,"
        " 'matched_phash', matched_phash,"
        " 'matched_tenant_id', matched_tenant_id,"
        " 'calendar_row_id', calendar_row_id,"
        " 'candidate_id', candidate_id,"
        " 'resolved_by', resolved_by,"
        " 'resolution_evidence', resolution_evidence)"
        " order by created_at), '[]'::jsonb) "
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
               "'visual_scene_row_candidate','visual_scene_row_delivered_object',"
               "'visual_scene_write_holds',"
               "'visual_scene_hold_resolve','visual_scene_hold_reactivate',"
               "'visual_scene_publish_claim_guarded',"
               "'visual_scene_approval_guarded')")
    assert out == ("visual_scene_approval_guarded,"
                   "visual_scene_claim_decide,visual_scene_claim_guard,"
                   "visual_scene_claim_scan,visual_scene_hold_reactivate,"
                   "visual_scene_hold_resolve,"
                   "visual_scene_publish_claim_guarded,"
                   "visual_scene_row_candidate,"
                   "visual_scene_row_delivered_object,"
                   "visual_scene_write_holds")
    # wave-3: the separate early scene trigger and its function are REMOVED
    assert _one("select count(*) from pg_proc where proname = "
                "'visual_scene_calendar_claim_guard'") == "0"
    assert _one("select count(*) from pg_trigger where tgname = "
                "'content_calendar_scene_wave_claim_guard'") == "0"
    # the ONE authoritative content_calendar BEFORE trigger is the merged
    # exact-byte guard, and it is the ONLY row trigger on the table
    assert _one("select string_agg(t.tgname, ',' order by t.tgname) "
                "from pg_trigger t join pg_class c on c.oid = t.tgrelid "
                "where c.relname = 'content_calendar' and not t.tgisinternal "
                "and t.tgtype::int & 1 = 1") == \
        "content_calendar_visual_group_guard"


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


# ---- wave-2 repair scenarios (2026-10-04) -------------------------------------

def test_unkeyed_row_group_auto_resolved_and_clean_claim_commits():
    # wave-2 item 1: the trigger no longer skips scene evaluation when
    # new.visual_group_key was not pre-populated — it resolves the canonical
    # group itself (visual_group_resolve_row) and claims.
    tid, group = _seed_tenant()
    url, _fp, _cand = _seed_object(tid, group, _phash())
    row_id = _insert_row_ex(tid, group, url, keyed=False)
    row = _row(row_id)
    assert row["visual_group_key"] == group
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert len(_occupied(tid)) == 1
    assert _holds(tid) == []


def test_held_row_clears_both_token_and_reservation():
    # wave-2 item 2: the held mutation clears publish_claim_token AND the
    # real reservation column publish_reservation_day, committed.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row_ex(tid_b, group_b, url_b,
                            reservation="2026-10-10")
    row = _row(row_id)
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    assert len(_holds(tid_b)) == 1


def test_invalid_row_attestation_hard_rejects_before_any_hold_write():
    # wave-2 item 1: byte attestation is validated BEFORE any hold write.
    # The row WOULD conflict (near candidate staged), but its selected source
    # object is unattested, so the whole insert raises 23514 and no hold,
    # no row and no occupancy is ever produced.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    bogus_source = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    done = _run(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, source_media_url, visual_group_key) values "
        f"('{tid_b}', 'ig', '2026-10-10', 'pending', 'active',"
        f" '{url_b}', '{bogus_source}', '{group_b}') returning id",
        check=False)
    assert done.returncode != 0
    assert "delivered-row byte attestation missing or invalid" in done.stderr
    assert _one("select count(*) from public.content_calendar "
                f"where gym_id = '{tid_b}'") == "0"
    assert _holds(tid_b) == []
    assert _occupied(tid_b) == []


def test_raw_alias_gym_key_is_evaluated_and_held_not_skipped():
    # wave-2 item 1: a raw alias calendar key resolves to the canonical
    # tenant inside the guard — the row is scene-evaluated (and here held
    # against an occupied cross-tenant conflict), never silently skipped.
    # (The alias must exist BEFORE arming: the alias-arm guard refuses new
    # calendar alias keys for an already-armed tenant.)
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)
    assert len(_occupied(tid_a)) == 1
    tid_b = str(uuid.uuid4())
    alias = "alias_" + uuid.uuid4().hex[:12]
    group_b = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid_b}', '{tid_b}'), ('{alias}', '{tid_b}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid_b}', '{group_b}')")
    _sql(f"select public.visual_group_activate_guard('{tid_b}',"
         " 'scene-wave-test')")
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, _near(base, 2))
    row_id = _insert_row_ex(tid_b, group_b, url_b, keyed=False, gym=alias)
    row = _row(row_id)
    assert row["visual_group_key"] == group_b
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    # the hold is recorded under the CANONICAL tenant, not the raw alias
    assert len(_holds(tid_b)) == 1
    assert _occupied(tid_b) == []


def test_same_day_sibling_identical_scene_is_single_occupancy_no_hold():
    tid, group = _seed_tenant()
    base = _phash()
    url, _fp, _cand = _seed_object(tid, group, base)
    row1 = _insert_row(tid, group, url)
    row2 = _insert_row(tid, group, url)
    assert _row(row1)["variant_status"] == "active"
    assert _row(row2)["variant_status"] == "active"
    # re-recording the same sibling scene is an idempotent no-op
    assert _occupied(tid) == [base]
    assert _holds(tid) == []


def test_distinct_row_same_conflict_gets_its_own_open_hold():
    # Stable open-hold uniqueness is keyed per calendar_row_id: a retry on
    # the SAME row never duplicates (covered above), but a DISTINCT row
    # hitting the same conflict must get its own hold.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(3)
    row1 = _insert_row(tid_b, group_b, url_b)
    row2 = _insert_row_ex(tid_b, group_b, url_b, date="2026-10-10",
                          source=url_b)
    holds = _holds(tid_b)
    assert len(holds) == 2
    assert {h["calendar_row_id"] for h in holds} == {row1, row2}
    assert all(h["state"] == "open" for h in holds)


def test_held_update_releases_old_local_membership():
    # wave-2 item 2: the conflict path still passes NEW through
    # visual_group_sync_row, so the row's OLD active local membership is
    # released in the same committed transaction that writes the hold.
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)  # occupied conflict source
    tid_b, group_b1 = _seed_tenant()
    own = _phash()
    url1, _f1, _c1 = _seed_object(tid_b, group_b1, own)
    row_id = _insert_row(tid_b, group_b1, url1)
    assert _one("select state from public.visual_group_usage_ledger "
                f"where gym_id = '{tid_b}' and group_key = '{group_b1}'") \
        == "reserved"
    # Re-point the row at a different scene whose candidate is near the
    # occupied conflict: the UPDATE commits HELD...
    group_b2 = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid_b}', '{group_b2}')")
    url2, _f2, _c2 = _seed_object(tid_b, group_b2, _near(base, 2))
    _sql("update public.content_calendar set image_url = "
         f"'{url2}', source_media_url = '{url2}', "
         f"visual_group_key = '{group_b2}' where id = '{row_id}'")
    row = _row(row_id)
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert len(_holds(tid_b)) == 1
    # ...and the OLD local membership was released by the same transaction
    assert _one("select state from public.visual_group_usage_ledger "
                f"where gym_id = '{tid_b}' and group_key = '{group_b1}'") \
        == "released"
    assert _one("select count(*) from public.visual_group_usage_ledger "
                f"where gym_id = '{tid_b}' and group_key = '{group_b2}' "
                "and state = 'reserved'") == "0"
    # occupancy is PERMANENT: the row's earlier clean claim stays consumed;
    # the held UPDATE adds nothing
    assert _occupied(tid_b) == [own]


def test_approval_exemption_is_exact_and_reactivation_records_only_exempted():
    # wave-2 items 2+3: approving ONE hold exempts ONLY that exact reviewed
    # conflict; a DIFFERENT occupied conflict still blocks reactivation
    # (still_blocked, no mutation). Once both reviewed conflicts are
    # approved, reactivation returns the row to active and records occupancy
    # for the claimant's candidate only. Resolution stores actor + evidence.
    # Hand-computed pHash geometry (no codebook words needed):
    #   O1 = 0x0                tenant A's occupied scene
    #   O2 = 0x07ffffff0000000f tenant C's occupied scene — popcount 31, so
    #                           dist(O1,O2)=31 > 30: C claims clean despite A
    #   B  = 0x3f               claimant candidate: dist(B,O1)=6 (near_frame)
    #                           and dist(B,O2)=29 (uncertain): TWO conflicts
    o1, o2, cand = ("0000000000000000", "07ffffff0000000f",
                    "000000000000003f")
    tid_a, group_a = _seed_tenant()
    url_a, _fa, _ca = _seed_object(tid_a, group_a, o1)
    _insert_row(tid_a, group_a, url_a)
    assert _occupied(tid_a) == [o1]
    tid_c, group_c = _seed_tenant()
    url_c, _fc, _cc = _seed_object(tid_c, group_c, o2)
    _insert_row(tid_c, group_c, url_c)
    assert _occupied(tid_c) == [o2]  # clean: 31 > 30 from O1
    tid_b, group_b = _seed_tenant()
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, cand)
    row_id = _insert_row(tid_b, group_b, url_b)
    holds = _holds(tid_b)
    assert len(holds) == 2
    assert {h["matched_phash"] for h in holds} == {o1, o2}
    assert _row(row_id)["media_not_ready_reason"] == "scene_review_hold"
    hold_base = next(h for h in holds if h["matched_phash"] == o1)
    hold_near = next(h for h in holds if h["matched_phash"] == o2)
    out = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_base['hold_id']}', 'approved', 'reviewer',"
        " jsonb_build_object('notes', 'owner confirmed reuse'))::text"))
    assert out["state"] == "approved"
    assert out["exemption_scope"]["matched_phash"] == o1
    # actor AND evidence stored one-shot on the resolved hold
    stored = json.loads(_one(
        "select jsonb_build_object('resolved_by', resolved_by,"
        " 'resolution_evidence', resolution_evidence)::text "
        "from public.visual_scene_review_hold "
        f"where hold_id = '{hold_base['hold_id']}'"))
    assert stored["resolved_by"] == "reviewer"
    assert stored["resolution_evidence"] == {"notes": "owner confirmed reuse"}
    # the DIFFERENT conflict still holds: reactivation refuses, no mutation
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold_base['hold_id']}', 'reviewer')::text"))
    assert out["reactivated"] is False
    assert out["reason"] == "still_blocked"
    assert _row(row_id)["media_not_ready_reason"] == "scene_review_hold"
    assert _occupied(tid_b) == []
    # approving the second reviewed conflict unblocks reactivation
    _one("select public.visual_scene_hold_resolve("
         f"'{hold_near['hold_id']}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed reuse'))")
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold_base['hold_id']}', 'reviewer')::text"))
    assert out["reactivated"] is True
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    # occupancy for the exempted claimant candidate only — never a blanket
    # approval and never a "used" mark on the staged candidates
    assert _occupied(tid_b) == [cand]
    assert _one("select public.visual_scene_approval_guarded("
                f"'{row_id}')") == "t"


def test_busy_fleet_advisory_lock_raises_55P03():
    # wave-2 item 3: hold resolution takes the single fleet-wide scene
    # advisory lock; while a peer session holds it, the resolution raises
    # 55P03 'global scene claim busy; retry transaction' instead of
    # queueing behind it.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    locker = subprocess.Popen(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN, "-c",
         "select pg_advisory_lock(hashtextextended("
         "jsonb_build_array('visual_scene_global')::text, 0)); "
         "select pg_sleep(10);"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # wait until the peer session actually holds the advisory lock
        for _ in range(50):
            held = _one("select count(*) from pg_locks where locktype = "
                        "'advisory' and pid <> pg_backend_pid()")
            if held == "1":
                break
            locker.poll()
            assert locker.returncode is None
            import time
            time.sleep(0.1)
        assert held == "1"
        done = _run("select public.visual_scene_hold_resolve("
                    f"'{hold_id}', 'approved', 'reviewer',"
                    " jsonb_build_object('notes','x'))", check=False)
        assert done.returncode != 0
        assert "global scene claim busy; retry transaction" in done.stderr
        # the hold is untouched: still open, no resolution written
        holds = _holds(tid_b)
        assert len(holds) == 1 and holds[0]["state"] == "open"
        assert holds[0]["resolved_by"] is None
    finally:
        locker.kill()
        locker.wait(timeout=30)
        # wait until the killed session's advisory lock is actually gone so
        # no later scenario can see a phantom 'busy' fleet lock
        import time
        for _ in range(100):
            left = _one("select count(*) from pg_locks where locktype = "
                        "'advisory' and pid <> pg_backend_pid()")
            if left == "0":
                break
            time.sleep(0.1)
        assert left == "0"


def test_video_row_binds_only_poster_candidate_display_candidate_fails_closed():
    # wave-2 item 4: a video row (distinct non-blank thumbnail_url) is
    # displayed by its POSTER. A 'display' candidate on the video file URL
    # does NOT satisfy the row: fail-closed 23514, no row, no hold.
    tid, group = _seed_tenant()
    url, fp, _nocand = _seed_object(tid, group)
    # display candidate on the video file URL only — must NOT satisfy the row
    _one("select public.visual_scene_register_candidate("
         f"'{tid}', '{group}', '{_phash()}', '{url}', '{fp}',"
         f" jsonb_build_object('verified_bytes', '{fp}'),"
         " 'scene-wave-test', 'display')")
    poster_url, _pfp, _pc = _seed_poster(tid, group, url, fp)
    done = _run(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, thumbnail_url, source_media_url, visual_group_key)"
        " values "
        f"('{tid}', 'ig', '2026-10-10', 'pending', 'active',"
        f" '{url}', '{poster_url}', '{url}', '{group}') returning id",
        check=False)
    assert done.returncode != 0
    assert "no scene candidate bound to this row" in done.stderr
    assert _one("select count(*) from public.content_calendar "
                f"where gym_id = '{tid}'") == "0"
    assert _holds(tid) == [] and _occupied(tid) == []


def test_video_row_with_bound_poster_candidate_claims_clean():
    # wave-2 item 4 (positive): the SAME video row claims cleanly once a
    # 'poster' candidate is staged against the exact thumbnail URL.
    tid, group = _seed_tenant()
    url, fp, _nocand = _seed_object(tid, group)
    poster_url, _pfp, _pc = _seed_poster(tid, group, url, fp, _phash())
    row_id = _insert_row_ex(tid, group, url, thumbnail=poster_url)
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert len(_occupied(tid)) == 1
    assert _holds(tid) == []


# ---- wave-3 repair scenarios (2026-10-04) -------------------------------------

def test_forged_group_hint_is_overwritten_by_resolved_group():
    # Sol independent-audit P0 (hint semantics rewrite): a forged/stale
    # non-null visual_group_key hint disagreeing with a NON-NULL resolution
    # is OVERWRITTEN with the resolved group and the row is fully
    # scene-evaluated under the RESOLVED group. The hint can never null its
    # way past the scene decision.
    # Case 1: forged hint + CONFLICTING scene -> committed held row + open
    # hold under the RESOLVED group (never the hint, never NULL).
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)
    assert len(_occupied(tid_a)) == 1
    tid_b, group_b = _seed_tenant()
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, _near(base, 2))
    forged = "vg_forged_" + uuid.uuid4().hex[:10]
    row_id = _insert_row(tid_b, forged, url_b)  # hint forged; aliases resolve group_b
    row = _row(row_id)
    assert row["visual_group_key"] == group_b
    assert row["status"] == "pending"
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    holds = _holds(tid_b)
    assert len(holds) == 1
    assert holds[0]["state"] == "open"
    assert _occupied(tid_b) == []
    # Case 2: forged hint + CLEAN scene -> active claim + occupancy under
    # the RESOLVED group.
    tid_c, group_c = _seed_tenant()
    own = _phash()
    url_c, _fpc, _cc = _seed_object(tid_c, group_c, own)
    row_c = _insert_row(tid_c, forged, url_c)
    row = _row(row_c)
    assert row["visual_group_key"] == group_c
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert _occupied(tid_c) == [own]
    assert _holds(tid_c) == []
    # Case 3: genuinely unresolved + UNKEYED -> the identity-unresolved mark
    # (no claim/hold/occupancy), never a scene skip under a hint.
    unattested = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    row_u = _insert_row_ex(tid_c, group_c, unattested, keyed=False)
    row = _row(row_u)
    assert row["visual_group_key"] is None
    assert row["media_not_ready_reason"] == "visual_group_identity_unresolved"
    assert _occupied(tid_c) == [own]
    assert _holds(tid_c) == []
    # Case 4: genuinely unresolved + KEYED -> the parity attestation check
    # fails closed (23514); no row, no hold, no occupancy.
    done = _run(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, source_media_url, visual_group_key) values "
        f"('{tid_c}', 'ig', '2026-10-10', 'pending', 'active',"
        f" '{unattested}', '{unattested}', '{group_c}') returning id",
        check=False)
    assert done.returncode != 0
    assert "delivered-row byte attestation missing or invalid" in done.stderr
    assert _one("select count(*) from public.content_calendar "
                f"where gym_id = '{tid_c}' and image_url = '{unattested}'") == "1"
    assert _occupied(tid_c) == [own]
    assert _holds(tid_c) == []


def test_sync_raise_on_conflict_path_leaves_neither_row_nor_hold():
    # wave-3 item 2 (ordering invariant, negative probe): on the conflict
    # path visual_group_sync_row runs BEFORE the hold insertion and may still
    # raise. A raise there must roll back BOTH the held calendar mutation and
    # the (never-yet-written) hold — durable holds are impossible after a
    # failed sync. The forced raise is a SCRATCH-ONLY override of the
    # exact-byte release authority (restored in finally); no migration file
    # is modified.
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)  # occupied cross-tenant conflict source
    tid_b, group_b1 = _seed_tenant()
    own = _phash()
    url1, _f1, _c1 = _seed_object(tid_b, group_b1, own)
    row_id = _insert_row(tid_b, group_b1, url1)
    assert _occupied(tid_b) == [own]
    group_b2 = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid_b}', '{group_b2}')")
    url2, _f2, _c2 = _seed_object(tid_b, group_b2, _near(base, 2))
    original = _run(
        "select pg_get_functiondef("
        "'public.visual_global_release(text,text)'::regprocedure)").stdout
    # SCRATCH-ONLY override: force the exact-byte release inside sync_row to
    # raise on the conflict path (the release of the OLD membership).
    _sql("drop function public.visual_global_release(text,text); "
         "create function public.visual_global_release(text,text) "
         "returns boolean language plpgsql security definer "
         "set search_path = public as $$ begin "
         "raise exception 'scratch forced release failure "
         "(wave-3 ordering probe)' using errcode='23514'; end; $$")
    try:
        done = _run(
            "update public.content_calendar set image_url = "
            f"'{url2}', source_media_url = '{url2}', "
            f"visual_group_key = '{group_b2}' where id = '{row_id}'",
            check=False)
        assert done.returncode != 0
        assert "scratch forced release failure" in done.stderr
        # the whole UPDATE rolled back: the row persists in its ORIGINAL
        # active state, no hold was ever written, occupancy is unchanged
        row = _row(row_id)
        assert row["visual_group_key"] == group_b1
        assert row["variant_status"] == "active"
        assert row["media_not_ready_reason"] is None
        assert _holds(tid_b) == []
        assert _occupied(tid_b) == [own]
        assert _one("select state from public.visual_group_usage_ledger "
                    f"where gym_id = '{tid_b}' and group_key = '{group_b1}'") \
            == "reserved"
    finally:
        _sql(original)
    # sanity after restore: the same conflicting UPDATE now commits HELD with
    # a durable hold (proves the probe, not the design, caused the failure)
    _sql("update public.content_calendar set image_url = "
         f"'{url2}', source_media_url = '{url2}', "
         f"visual_group_key = '{group_b2}' where id = '{row_id}'")
    assert _row(row_id)["media_not_ready_reason"] == "scene_review_hold"
    assert len(_holds(tid_b)) == 1


def test_stale_binding_resolution_is_rejected():
    # wave-3 item 3: resolution re-reads the LIVE calendar row under the
    # row-first lock order; a row that drifted from the reviewed held state
    # (or vanished) refuses the resolution and the hold stays open.
    # (Trigger bypass via session_replication_role=replica is scratch-only
    # instrumentation, the same mechanism the isolation fixture uses.)
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    # date drift
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set post_date = '2026-10-12' "
         f"where id = '{row_id}'")
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold_id}', 'approved', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "calendar row mutated since the hold; refusing stale resolution" \
        in done.stderr
    assert _holds(tid_b)[0]["state"] == "open"
    # row deletion
    _sql("set session_replication_role = replica; "
         f"delete from public.content_calendar where id = '{row_id}'")
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold_id}', 'approved', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "held calendar row no longer present; refusing stale resolution" \
        in done.stderr
    assert _holds(tid_b)[0]["state"] == "open"


def test_cross_tenant_resolution_is_rejected():
    # wave-3 item 3: the row's CURRENT canonical tenant must equal the
    # reviewed hold tenant; cross-tenant review is refused and the hold
    # stays open. (Replica-role gym_id rewrite is scratch-only.)
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    tid_c, _group_c = _seed_tenant()
    _sql("set session_replication_role = replica; "
         f"update public.content_calendar set gym_id = '{tid_c}' "
         f"where id = '{row_id}'")
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold_id}', 'approved', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "calendar row moved tenants since the hold; " \
        "cross-tenant review refused" in done.stderr
    assert _holds(tid_b)[0]["state"] == "open"


def test_resolution_lock_order_row_first_is_deadlock_free():
    # wave-3 item 3: hold resolution locks the CALENDAR ROW FOR UPDATE FIRST,
    # then components, then the fleet advisory lock — the same order a normal
    # UPDATE takes locks. A concurrent normal UPDATE holding the row lock
    # makes the resolution WAIT on the row (lock timeout here, never a
    # deadlock), and once the writer commits the resolution succeeds.
    import time
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    writer = subprocess.Popen(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN, "-c",
         "begin; "
         f"update public.content_calendar set account = 'ig' "
         f"where id = '{row_id}'; "
         "select pg_sleep(20); commit;"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # wait until the writer actually holds its row lock on the calendar
        for _ in range(50):
            held = _one("select count(*) from pg_locks where locktype = "
                        "'relation' and relation = "
                        "'public.content_calendar'::regclass "
                        "and mode = 'RowExclusiveLock' "
                        "and pid <> pg_backend_pid()")
            if held == "1":
                break
            writer.poll()
            assert writer.returncode is None
            time.sleep(0.1)
        assert held == "1"
        # the resolution blocks on the ROW lock (row-first order) and times
        # out — it must NEVER report a deadlock against the writer
        done = _run("set lock_timeout = '1200ms'; "
                    "select public.visual_scene_hold_resolve("
                    f"'{hold_id}', 'approved', 'reviewer',"
                    " jsonb_build_object('notes','x'))", check=False)
        assert done.returncode != 0
        assert "canceling statement due to lock timeout" in done.stderr
        assert "deadlock detected" not in done.stderr
        # nothing mutated: the hold is still open, the row still held
        assert _holds(tid_b)[0]["state"] == "open"
        assert _row(row_id)["media_not_ready_reason"] == "scene_review_hold"
    finally:
        writer.kill()
        writer.wait(timeout=30)
        # the killed client may leave a backend finishing pg_sleep; terminate
        # it so no phantom lock leaks into later scenarios
        _sql("select pg_terminate_backend(pid) from pg_stat_activity "
             "where pid <> pg_backend_pid() "
             "and datname = current_database()")
        import time as _time
        for _ in range(300):
            left = _one("select count(*) from pg_locks where locktype = "
                        "'relation' and relation = "
                        "'public.content_calendar'::regclass "
                        "and mode = 'RowExclusiveLock' "
                        "and pid <> pg_backend_pid()")
            if left == "0":
                break
            _time.sleep(0.1)
        assert left == "0"
    # with the writer gone the same resolution succeeds cleanly
    out = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_id}', 'approved', 'reviewer',"
        " jsonb_build_object('notes', 'owner confirmed'))::text"))
    assert out["state"] == "approved"


def test_reactivation_converted_back_to_held_is_reported_not_faked():
    # wave-3 item 3: the reactivation UPDATE re-enters the MERGED calendar
    # trigger, which may legitimately convert the row back to held; the
    # function must then re-read the PERSISTED row and report
    # converted_back_to_held instead of faking success. Constructing a
    # re-hold requires a second decision between the function's clean
    # pre-scan and the trigger's own scan in the same statement, which is
    # not reachable from stock draft behavior — so this uses a CLEARLY
    # MARKED SCRATCH-ONLY instrumentation trigger (GUC-gated, dropped in
    # finally) that re-holds NEW after the merged guard ran, emulating the
    # trigger's conversion. No migration file is modified.
    _ta, tid_b, group_b, url_b, base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    _one("select public.visual_scene_hold_resolve("
         f"'{hold_id}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed'))")
    # SCRATCH-ONLY re-hold instrumentation (GUC-gated so it can only fire in
    # the session that opts in; zz_ prefix keeps it after the merged guard).
    _sql("create or replace function public.scratch_wave3_rehold() "
         "returns trigger language plpgsql as $$ begin "
         "if current_setting('scratch.wave3_rehold', true) = 'on' then "
         "new.variant_status := 'archived'; new.status := 'pending'; "
         "new.media_not_ready_reason := 'scene_review_hold'; end if; "
         "return new; end; $$; "
         "drop trigger if exists zz_scratch_wave3_rehold "
         "on public.content_calendar; "
         "create trigger zz_scratch_wave3_rehold before update "
         "on public.content_calendar for each row "
         "execute function public.scratch_wave3_rehold()")
    try:
        out = json.loads(_one(
            "set scratch.wave3_rehold = 'on'; "
            "select public.visual_scene_hold_reactivate("
            f"'{hold_id}', 'reviewer')::text"))
        assert out["reactivated"] is False
        assert out["reason"] == "converted_back_to_held"
        assert out["persisted"]["media_not_ready_reason"] == \
            "scene_review_hold"
        assert out["persisted"]["variant_status"] == "archived"
        # the PERSISTED row really is held — no fake success
        row = _row(row_id)
        assert row["variant_status"] == "archived"
        assert row["media_not_ready_reason"] == "scene_review_hold"
    finally:
        _sql("drop trigger if exists zz_scratch_wave3_rehold "
             "on public.content_calendar; "
             "drop function if exists public.scratch_wave3_rehold()")
    # with the instrumentation gone the same approved reactivation succeeds
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold_id}', 'reviewer')::text"))
    assert out["reactivated"] is True
    assert _row(row_id)["variant_status"] == "active"
    assert _occupied(tid_b) == [_near(base, 2)]


# ---- frozen-hash P0: upgrade/replay idempotency + review re-resolution --------

UPG_DB = "echo_scene_wave_test_upg"
WAVE2_REV = "7c3539dfff9228afc66d60fac17325a22adf0e5d"
WAVE2_SHA256 = "23f49f843a8b0be30e32534b0b85543b25b93e32d13a886aad86bee393ba1024"


def _upg_dsn(db=UPG_DB):
    return DSN.replace("dbname=echo_scene_wave_test", f"dbname={db}")


def _apply_sql(dsn, text, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", dsn],
        input=text, text=True, capture_output=True, timeout=180)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _db_sql(dsn, statement, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", dsn,
         "-c", statement], text=True, capture_output=True, timeout=60)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def _db_one(dsn, statement):
    out = _db_sql(dsn, statement)
    return out.splitlines()[-1] if out else ""


@pytest.fixture()
def upgrade_db():
    """A SECOND disposable scratch database on the same disposable cluster
    (same Unix socket, dbname echo_scene_wave_test_upg), used so wave-2 ->
    wave-3 upgrade simulations never disturb the module's main scratch DB.
    Dropped on teardown; never a network or production host."""
    admin = _upg_dsn("postgres")
    _db_sql(admin, f"drop database if exists {UPG_DB}")
    _db_sql(admin, f"create database {UPG_DB}")
    dsn = _upg_dsn()
    _db_sql(dsn, "create table if not exists public.content_calendar ("
                 "id uuid primary key default gen_random_uuid(),"
                 "gym_id text, account text, post_date date,"
                 "status text, variant_status text,"
                 "published_at timestamptz, publish_claim_token uuid,"
                 "publish_reservation_day date,"
                 "late_post_id text, image_url text, thumbnail_url text,"
                 "source_media_url text, source_media_asset_id text,"
                 "drive_file_id text, byte_hash text, r2_key text,"
                 "media_not_ready_reason text)")
    _db_sql(dsn, "create table if not exists public.media_asset ("
                 "id text primary key, gym_id text, content_hash text)")
    _db_sql(dsn, "create or replace function public.visual_group_row_active(public.content_calendar)"
                 " returns boolean language sql stable as $$ select false $$")
    _db_sql(dsn, "create or replace function public.visual_group_row_ambiguous(public.content_calendar)"
                 " returns boolean language sql stable as $$ select false $$")
    prereqs = "".join((MIGRATIONS / name).read_text() + "\n"
                      for name in STACK[:-2])
    _apply_sql(dsn, prereqs)
    yield dsn
    _db_sql(admin, f"drop database if exists {UPG_DB}")


def _wave2_text():
    """Load the immutable predecessor that actually installed the separate
    scene trigger and legacy hold shape. HEAD is wave 3 and cannot model the
    upgrade source these tests exercise."""
    done = subprocess.run(
        ["git", "show",
         f"{WAVE2_REV}:migrations/DRAFT_visual_scene_claim_wave_20261003.sql"],
        text=True, capture_output=True, timeout=30,
        cwd=MIGRATIONS.parent)
    assert done.returncode == 0, done.stderr
    assert hashlib.sha256(done.stdout.encode()).hexdigest() == WAVE2_SHA256
    assert "create trigger content_calendar_scene_wave_claim_guard" in done.stdout
    legacy_hold = done.stdout.split(
        "create table if not exists public.visual_scene_review_hold", 1
    )[1].split(");", 1)[0]
    assert "candidate_id" not in legacy_hold
    return done.stdout


def test_scene_file_replay_is_idempotent():
    # (a) REPLAY: applying the working wave-3 scene file a second time over
    # the already-applied stack exits 0 (drops are IF EXISTS NOTICEs, all
    # creates are IF NOT EXISTS / OR REPLACE / idempotent do-blocks).
    assert _one("select count(*) from pg_trigger where tgname = "
                "'content_calendar_scene_wave_claim_guard'") == "0"
    done = _apply_sql(DSN, WAVE.read_text() + "\n" + HISTORY_BACKFILL.read_text())
    assert done.returncode == 0
    # still exactly ONE authoritative calendar row trigger afterwards
    assert _one("select count(*) from pg_trigger t join pg_class c "
                "on c.oid = t.tgrelid where c.relname = 'content_calendar' "
                "and not t.tgisinternal and t.tgtype::int & 1 = 1") == "1"
    assert _one("select count(*) from pg_proc where proname = "
                "'visual_scene_calendar_claim_guard'") == "0"


def test_upgrade_from_committed_wave2_retires_leftovers_and_converges(upgrade_db):
    # (b) UPGRADE SIM: immutable committed predecessor revision then
    # the working wave-3 file. Exit 0; the wave-2 separate early trigger and
    # its zero-arg function are retired by the after-begin drops; the hold
    # table converges onto the named checks and the candidate-scoped open_uq.
    dsn = upgrade_db
    _apply_sql(dsn, _wave2_text())
    # wave-2 leftovers really exist before the upgrade
    assert _db_one(dsn, "select count(*) from pg_trigger where tgname = "
                        "'content_calendar_scene_wave_claim_guard'") == "1"
    assert _db_one(dsn, "select count(*) from pg_proc where proname = "
                        "'visual_scene_calendar_claim_guard'") == "1"
    done = _apply_sql(dsn, WAVE.read_text())
    assert done.returncode == 0
    assert _db_one(dsn, "select count(*) from pg_trigger where tgname = "
                        "'content_calendar_scene_wave_claim_guard'") == "0"
    assert _db_one(dsn, "select count(*) from pg_proc where proname = "
                        "'visual_scene_calendar_claim_guard'") == "0"
    # named constraints converged
    cons = _db_one(dsn, "select string_agg(conname, ',' order by conname) "
                        "from pg_constraint where conrelid = "
                        "'public.visual_scene_review_hold'::regclass "
                        "and conname in ('visual_scene_review_hold_exact_url_ck',"
                        "'visual_scene_review_hold_fingerprint_ck',"
                        "'visual_scene_review_hold_open_terminal_ck')")
    assert cons == ("visual_scene_review_hold_exact_url_ck,"
                    "visual_scene_review_hold_fingerprint_ck,"
                    "visual_scene_review_hold_open_terminal_ck")
    # open-terminal check covers resolution_evidence; open_uq is
    # candidate-scoped; new identity columns are NOT NULL with the FK
    assert "resolution_evidence" in _db_one(
        dsn, "select pg_get_constraintdef(oid) from pg_constraint "
             "where conrelid = 'public.visual_scene_review_hold'::regclass "
             "and conname = 'visual_scene_review_hold_open_terminal_ck'")
    idxdef = _db_one(dsn, "select pg_get_indexdef(indexrelid) from pg_index "
                          "where indexrelid = "
                          "'public.visual_scene_review_hold_open_uq'::regclass")
    assert "candidate_id" in idxdef and "matched_tenant_id" in idxdef
    for col in ("candidate_id", "exact_url", "fingerprint"):
        assert _db_one(dsn, "select is_nullable from information_schema.columns "
                            "where table_schema = 'public' and table_name = "
                            f"'visual_scene_review_hold' and column_name = '{col}'") == "NO"
    assert _db_one(dsn, "select count(*) from pg_constraint where contype = 'f' "
                        "and conrelid = 'public.visual_scene_review_hold'::regclass "
                        "and confrelid = 'public.visual_scene_candidate'::regclass") == "1"
    # and the upgraded database replays the wave-3 file cleanly too
    assert _apply_sql(dsn, WAVE.read_text()).returncode == 0


def test_upgrade_refuses_legacy_holds_lacking_candidate_identity(upgrade_db):
    # (c) NORMALIZATION REFUSAL: a wave-2 hold row has no candidate identity;
    # the upgrade must hard-refuse (23514, NOT lossless) instead of carrying
    # unscoped holds into the wave-3 contract.
    dsn = upgrade_db
    _apply_sql(dsn, _wave2_text())
    tid = str(uuid.uuid4())
    _db_sql(dsn, f"insert into public.tenant_alias(alias_key, tenant_id) "
                 f"values ('{tid}', '{tid}')")
    _db_sql(dsn, f"insert into public.visual_group(gym_id, group_key) "
                 f"values ('{tid}', 'vg_legacy')")
    _db_sql(dsn, "insert into public.visual_scene_review_hold"
                 "(tenant_id, group_key, claim_date, calendar_row_id,"
                 " candidate_phash, matched_phash, hamming,"
                 " matched_tenant_id, matched_group_key, matched_used_date,"
                 " hold_kind) values "
                 f"('{tid}', 'vg_legacy', '2026-10-10', null,"
                 f" '{_CODEBOOK[0]}', '{_CODEBOOK[1]}', 32,"
                 f" '{tid}', 'vg_legacy', '2026-10-10', 'uncertain')")
    done = _apply_sql(dsn, WAVE.read_text(), check=False)
    assert done.returncode != 0
    assert "wave-2 scene review holds lack exact candidate identity" in done.stderr
    assert "draft upgrade is not lossless" in done.stderr
    # the failed upgrade rolled back: legacy trigger/function still present,
    # no candidate_id column was left behind
    assert _db_one(dsn, "select count(*) from pg_trigger where tgname = "
                        "'content_calendar_scene_wave_claim_guard'") == "1"
    assert _db_one(dsn, "select count(*) from information_schema.columns "
                        "where table_schema = 'public' and table_name = "
                        "'visual_scene_review_hold' "
                        "and column_name = 'candidate_id'") == "0"


def test_persisted_key_forgery_still_resolves_on_the_reresolved_group():
    # (d) REVIEW RE-RESOLUTION: the persisted visual_group_key on the held row
    # is NOT authority. Forge it (delivered object untouched, so the exact
    # aliases still resolve the REAL reviewed group); the resolution
    # re-resolves the group from the live row and succeeds.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    forged = "vg_forged_" + uuid.uuid4().hex[:10]
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set visual_group_key = "
         f"'{forged}' where id = '{row_id}'")
    assert _row(row_id)["visual_group_key"] == forged
    out = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_id}', 'approved', 'reviewer',"
        " jsonb_build_object('notes', 'owner confirmed'))::text"))
    assert out["state"] == "approved"
    assert out["exemption_scope"]["group_key"] == group_b


def test_delivered_object_drift_refuses_resolution_and_reactivation():
    # (e) DELIVERED-OBJECT DRIFT: re-point the row's media (replica-role
    # scratch instrumentation) so the exact aliases resolve a DIFFERENT (or
    # no) group. Resolution refuses 'calendar row mutated since the hold';
    # an approved hold's reactivation refuses 'calendar row identity
    # drifted from the reviewed hold'. Neither mutates anything.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    # case 1: drift BEFORE resolution -> stale-resolution refusal
    row1 = _insert_row(tid_b, group_b, url_b)
    hold1 = _holds(tid_b)[0]["hold_id"]
    bogus = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set image_url = "
         f"'{bogus}', source_media_url = '{bogus}' where id = '{row1}'")
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold1}', 'approved', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "calendar row mutated since the hold; refusing stale resolution" \
        in done.stderr
    assert _holds(tid_b)[0]["state"] == "open"
    # case 2: drift AFTER approval -> reactivation identity-drift refusal
    row2 = _insert_row_ex(tid_b, group_b, url_b, date="2026-10-10",
                          source=url_b)
    hold2 = [h for h in _holds(tid_b)
             if h["calendar_row_id"] == row2][0]["hold_id"]
    _one("select public.visual_scene_hold_resolve("
         f"'{hold2}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed'))")
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set image_url = "
         f"'{bogus}', source_media_url = '{bogus}' where id = '{row2}'")
    done = _run("select public.visual_scene_hold_reactivate("
                f"'{hold2}', 'reviewer')", check=False)
    assert done.returncode != 0
    assert "calendar row identity drifted from the reviewed hold; " \
        "refusing reactivation" in done.stderr
    assert _row(row2)["media_not_ready_reason"] == "scene_review_hold"


# ---- Sol independent-audit P0/P1: unique-candidate + persisted verification ----

def test_second_bound_candidate_refuses_resolution():
    # Sol P1: the reviewed candidate must be UNIQUELY bound to the live row's
    # delivered object. A second candidate on the same object — even with
    # IDENTICAL evidence (same pHash) — changes the reviewed binding set:
    # 23514 'bound scene candidate ... not uniquely the reviewed candidate'.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold = _holds(tid_b)[0]
    # duplicate-evidence second candidate on the SAME delivered object
    _one("select public.visual_scene_register_candidate("
         f"'{tid_b}', '{group_b}', '{hold['candidate_phash']}',"
         f" '{hold['exact_url']}', '{hold['fingerprint']}',"
         f" jsonb_build_object('verified_bytes', '{hold['fingerprint']}'),"
         " 'scene-wave-test', 'display')")
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold['hold_id']}', 'approved', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "bound scene candidate for the live row is not uniquely the " \
        "reviewed candidate; refusing resolution" in done.stderr
    assert _holds(tid_b)[0]["state"] == "open"


def test_conflicting_phash_second_candidate_fails_closed_on_resolution():
    # Sol P1 (conflicting-evidence variant): a second candidate on the same
    # delivered object with a DIFFERENT pHash trips the ambiguous-evidence
    # fail-closed raise from visual_scene_row_candidate.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(3)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold = _holds(tid_b)[0]
    _one("select public.visual_scene_register_candidate("
         f"'{tid_b}', '{group_b}', '{_phash()}',"
         f" '{hold['exact_url']}', '{hold['fingerprint']}',"
         f" jsonb_build_object('verified_bytes', '{hold['fingerprint']}'),"
         " 'scene-wave-test', 'display')")
    done = _run("select public.visual_scene_hold_resolve("
                f"'{hold['hold_id']}', 'approved', 'reviewer',"
                " jsonb_build_object('notes','x'))", check=False)
    assert done.returncode != 0
    assert "conflicting staged scene candidates for one delivered object" \
        in done.stderr or \
        "bound scene candidate for the live row is not uniquely the " \
        "reviewed candidate" in done.stderr
    assert _holds(tid_b)[0]["state"] == "open"


def test_reactivate_success_carries_full_persisted_facts():
    # Sol P0: a successful reactivation verifies the FULL persisted reviewed
    # scope and returns it under 'persisted' — tenant, re-resolved group,
    # claim date and the bound candidate identity, never a fake success.
    _ta, tid_b, group_b, url_b, base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold = _holds(tid_b)[0]
    _one("select public.visual_scene_hold_resolve("
         f"'{hold['hold_id']}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed'))")
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold['hold_id']}', 'reviewer')::text"))
    assert out["reactivated"] is True
    persisted = out["persisted"]
    assert persisted["status"] == "pending"
    assert persisted["variant_status"] == "active"
    assert persisted["media_not_ready_reason"] is None
    assert persisted["tenant_id"] == tid_b
    assert persisted["group_key"] == group_b
    assert persisted["post_date"] == "2026-10-10"
    assert persisted["candidate_id"] == hold["candidate_id"]
    assert persisted["exact_url"] == hold["exact_url"]
    assert persisted["fingerprint"] == hold["fingerprint"]
    # and the persisted row really matches those facts
    row = _row(row_id)
    assert row["variant_status"] == "active"
    assert row["visual_group_key"] == group_b
    assert _occupied(tid_b) == [_near(base, 2)]


def test_post_lock_identity_drift_raises_55P03():
    # Sol P0 post-lock stability: identity is re-resolved AFTER the
    # component+fleet locks; drift between the pre-lock and post-lock
    # resolution raises 55P03 'visual identity changed while locking; retry
    # transaction'. Hitting that interleaving with real concurrency is a
    # timing race (the window is microseconds inside one function call), so
    # this uses a CLEARLY MARKED SCRATCH-ONLY emulation: a GUC-gated override
    # of visual_group_resolve_row that returns the REAL resolution on the
    # first (pre-lock) call and a flipped group on the second (post-lock)
    # call of the same resolve invocation — exactly the drift the stability
    # check exists to catch. The original function is captured via
    # pg_get_functiondef and restored in finally; no migration file is
    # modified.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    _insert_row(tid_b, group_b, url_b)
    hold_id = _holds(tid_b)[0]["hold_id"]
    original = _run(
        "select pg_get_functiondef("
        "'public.visual_group_resolve_row(public.content_calendar)'::regprocedure)"
    ).stdout
    # keep the real logic callable under a scratch name
    _sql("create table if not exists public.scratch_flip(n integer); "
         "delete from public.scratch_flip; "
         "insert into public.scratch_flip values (0); "
         + original.replace("visual_group_resolve_row",
                            "scratch_real_resolve_row", 1))
    try:
        # SCRATCH-ONLY override: real on first call, flipped on later calls,
        # only when the GUC opts in (set in the resolve session below).
        _sql("create or replace function public.visual_group_resolve_row("
             "p_row public.content_calendar) returns text "
             "language plpgsql volatile security definer "
             "set search_path = public as $$ "
             "declare cnt integer; begin "
             "if current_setting('scratch.flip_resolve', true) = 'on' then "
             "  update public.scratch_flip set n = scratch_flip.n + 1 "
             "  returning scratch_flip.n into cnt; "
             "  if cnt > 1 then return 'vg_scratch_flipped'; end if; "
             "end if; "
             "return public.scratch_real_resolve_row(p_row); "
             "end; $$")
        done = _run("set scratch.flip_resolve = 'on'; "
                    "select public.visual_scene_hold_resolve("
                    f"'{hold_id}', 'approved', 'reviewer',"
                    " jsonb_build_object('notes','x'))", check=False)
        assert done.returncode != 0
        assert "visual identity changed while locking; retry transaction" \
            in done.stderr
        # nothing mutated: the hold is still open
        assert _holds(tid_b)[0]["state"] == "open"
    finally:
        _sql(original)
        _sql("drop function if exists "
             "public.scratch_real_resolve_row(public.content_calendar); "
             "drop table if exists public.scratch_flip")
    # sanity after restore: the same resolution succeeds cleanly
    out = json.loads(_one(
        "select public.visual_scene_hold_resolve("
        f"'{hold_id}', 'approved', 'reviewer',"
        " jsonb_build_object('notes', 'owner confirmed'))::text"))
    assert out["state"] == "approved"


# ---- reactivation raise fixes: precondition + raising postcondition -----------

def test_reactivate_refuses_when_live_row_binds_a_different_clean_candidate():
    # Lead-mandated P1 precondition: an approved held row whose delivered
    # media now binds a DIFFERENT (clean) candidate in the SAME group/date
    # must fail reactivation with the raising precondition — reactivating on
    # unreviewed candidate evidence would commit a row and occupancy the
    # reviewer never approved. The raise happens BEFORE any mutation.
    _ta, tid_b, group_b, url_b, _base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold = _holds(tid_b)[0]
    _one("select public.visual_scene_hold_resolve("
         f"'{hold['hold_id']}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed'))")
    occupied_before = _one("select count(*) from public.visual_scene_phash_occupied")
    # swap the row's delivered object to a NEW attested object in the SAME
    # group with its own CLEAN candidate (replica-role rewrite is the same
    # scratch-only instrumentation the isolation fixture uses)
    clean_url, _cfp, clean_cand = _seed_object(tid_b, group_b, _phash())
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set image_url = "
         f"'{clean_url}', source_media_url = '{clean_url}' "
         f"where id = '{row_id}'")
    done = _run("select public.visual_scene_hold_reactivate("
                f"'{hold['hold_id']}', 'reviewer')", check=False)
    assert done.returncode != 0
    assert "bound scene candidate for the live row is not uniquely the " \
        "approved candidate; refusing reactivation" in done.stderr
    # nothing committed: the row is still in its exact reviewed held state
    # with the original date, occupancy is unchanged, the hold stays approved
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert _one("select count(*) from public.visual_scene_phash_occupied") \
        == occupied_before
    assert _holds(tid_b)[0]["state"] == "approved"


def test_reactivation_postcondition_raise_rolls_back_row_and_occupancy():
    # Sol frozen-hash P0 (verified construction): an active-but-wrong-
    # identity outcome may NEVER commit. The tamper is emulated with a
    # CLEARLY MARKED SCRATCH-ONLY GUC-gated zz_ trigger that shifts
    # NEW.post_date AFTER the merged guard ran — the raising postcondition
    # must roll back the UPDATE and the occupancy it wrote.
    _ta, tid_b, group_b, url_b, base = _conflicting_row(2)
    row_id = _insert_row(tid_b, group_b, url_b)
    hold = _holds(tid_b)[0]
    _one("select public.visual_scene_hold_resolve("
         f"'{hold['hold_id']}', 'approved', 'reviewer',"
         " jsonb_build_object('notes', 'owner confirmed'))")
    # happy path: clean reactivation succeeds
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold['hold_id']}', 'reviewer')::text"))
    assert out["reactivated"] is True
    assert _occupied(tid_b) == [_near(base, 2)]

    def _reset_to_held():
        # scratch-only reset (replica role bypasses the immutability
        # triggers): row back to the reviewed held state with the ORIGINAL
        # date; the tenant's occupancy removed. Delivered media untouched.
        _sql("set session_replication_role = replica; "
             "update public.content_calendar set variant_status = 'archived',"
             " status = 'pending',"
             " media_not_ready_reason = 'scene_review_hold',"
             " post_date = '2026-10-10' "
             f"where id = '{row_id}'; "
             "delete from public.visual_scene_phash_occupied "
             f"where tenant_id = '{tid_b}'")

    # clean-exempt pre-scan proof: after a reset, an UNTAMPERED reactivation
    # succeeds again (the exemption covers exactly this reviewed pair)
    _reset_to_held()
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold['hold_id']}', 'reviewer')::text"))
    assert out["reactivated"] is True
    # reset once more, then arm the tamper emulation
    _reset_to_held()
    assert _occupied(tid_b) == []
    occupied_before = _one("select count(*) from public.visual_scene_phash_occupied")
    # SCRATCH-ONLY tamper emulation (GUC-gated; zz_ prefix guarantees it
    # fires AFTER the merged content_calendar_visual_group_guard). Dropped
    # in finally. No migration file is modified.
    _sql("create or replace function public.scratch_wave3_tamper() "
         "returns trigger language plpgsql as $$ begin "
         "if current_setting('scratch.tamper_date', true) = 'on' then "
         "new.post_date := new.post_date + 1; end if; "
         "return new; end; $$; "
         "drop trigger if exists zz_emulated_tamper "
         "on public.content_calendar; "
         "create trigger zz_emulated_tamper before update "
         "on public.content_calendar for each row "
         "execute function public.scratch_wave3_tamper()")
    try:
        done = _run("set scratch.tamper_date = 'on'; "
                    "select public.visual_scene_hold_reactivate("
                    f"'{hold['hold_id']}', 'reviewer')", check=False)
        assert done.returncode != 0
        assert "persisted row identity failed reactivation verification; " \
            "rolling back reactivation" in done.stderr
        assert "DETAIL" in done.stderr
        # the raise rolled back EVERYTHING: row still in the exact reviewed
        # held state with the ORIGINAL date, occupancy unchanged (the merged
        # guard's occupancy insert rolled back with it), hold still approved
        row = _row(row_id)
        assert row["status"] == "pending"
        assert row["variant_status"] == "archived"
        assert row["media_not_ready_reason"] == "scene_review_hold"
        assert _one("select post_date from public.content_calendar "
                    f"where id = '{row_id}'") == "2026-10-10"
        assert _one("select count(*) from public.visual_scene_phash_occupied") \
            == occupied_before
        assert _occupied(tid_b) == []
        assert _holds(tid_b)[0]["state"] == "approved"
    finally:
        _sql("drop trigger if exists zz_emulated_tamper "
             "on public.content_calendar; "
             "drop function if exists public.scratch_wave3_tamper()")
    # with the emulation dropped the clean reactivation succeeds, no residue
    out = json.loads(_one(
        "select public.visual_scene_hold_reactivate("
        f"'{hold['hold_id']}', 'reviewer')::text"))
    assert out["reactivated"] is True
    assert _occupied(tid_b) == [_near(base, 2)]
    assert _one("select count(*) from pg_trigger where tgname = "
                "'zz_emulated_tamper'") == "0"
    assert _one("select count(*) from pg_proc where proname = "
                "'scratch_wave3_tamper'") == "0"


# ---- additive historical scene backfill + activation receipt (2026-10-04) ----

def _insert_published_history(tid, group, url, source_marker=True,
                              date="2026-09-01"):
    """Scratch-only historical row inserted without the runtime claim trigger."""
    source_col = ", source_media_url" if source_marker else ""
    source_val = f", '{url}'" if source_marker else ""
    return _one(
        "set session_replication_role=replica; "
        "insert into public.content_calendar"
        "(gym_id,account,post_date,status,variant_status,published_at,image_url,"
        f" visual_group_key{source_col}) values "
        f"('{tid}','ig','{date}','published','active',now(),'{url}',"
        f" '{group}'{source_val}) returning id")


def _history_audit(row_id):
    return json.loads(_one(
        "select to_jsonb(a)::text from public.visual_scene_history_audit(null) a "
        f"where a.calendar_row_id='{row_id}'"))


def test_history_backfill_clean_activation_persists_full_receipt():
    tid, group = _seed_tenant()
    phash = _phash()
    url, fingerprint, candidate = _seed_object(tid, group, phash)
    row_id = _insert_published_history(tid, group, url)
    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id='{tid}'")

    returned = json.loads(_one(
        f"select public.visual_group_activate_guard('{tid}',"
        " 'scene-history-test')::text"))
    assert returned["enforced"] is True
    persisted = json.loads(_one(
        "select proof::text from public.visual_group_activation "
        f"where gym_id='{tid}'"))
    scene = persisted["scene_history"]
    assert "scene_history" not in returned  # trigger receipt requires readback
    assert scene["scope"] == "fleet"
    assert scene["tenant_id"] is None
    assert scene["covered_published_rows"] == 1
    assert int(scene["transaction_id"]) > 0
    assert scene["inserted"] == 1
    assert scene["unresolved"] == 0
    assert scene["barrier"] == "content_calendar share row exclusive"
    recorded = scene["rows"][0]
    assert recorded["calendar_row_id"] == row_id
    assert recorded["tenant_id"] == tid
    assert recorded["group_key"] == group
    assert recorded["candidate_id"] == candidate
    assert recorded["phash"] == phash
    assert recorded["fingerprint"] == fingerprint
    assert recorded["exact_url"] == url
    assert recorded["object_role"] == "display"
    assert recorded["source_url"] == url
    assert recorded["source_fingerprint"] == fingerprint
    assert _occupied(tid) == [phash]


def test_history_backfill_source_null_in_other_tenant_blocks_activation_fleetwide():
    tid_a, _group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url, _fingerprint, _candidate = _seed_object(tid_b, group_b, _phash())
    row_id = _insert_published_history(tid_b, group_b, url,
                                       source_marker=False)
    prior_receipt = _one(
        "select proof::text from public.visual_group_activation "
        f"where gym_id='{tid_a}'")
    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id='{tid_a}'")

    # Exercise the additive receipt hook directly under the same table barrier
    # the activation RPC holds. This isolates the scene gate from the older
    # global importer, which may independently reject source-null history first.
    hook = _run(
        "begin; lock table public.content_calendar in share row exclusive mode; "
        "update public.visual_group_activation set proof='{}'::jsonb "
        f"where gym_id='{tid_a}'; commit", check=False)
    assert hook.returncode != 0
    assert "unresolved scene history requires review" in hook.stderr

    done = _run(
        f"select public.visual_group_activate_guard('{tid_a}',"
        " 'scene-history-test')", check=False)
    assert done.returncode != 0
    assert ("unresolved scene history requires review" in done.stderr
            or "global history import refused" in done.stderr)
    audit = _history_audit(row_id)
    assert audit["backfill_status"] == "unresolved"
    assert audit["reason"] == "source_null"
    assert _occupied(tid_b) == []
    assert _one("select enforce::text from public.gym_visual_guard_settings "
                f"where gym_id='{tid_a}'") == "false"
    # Failed activation rolls back the attempted replacement receipt.
    assert _one("select proof::text from public.visual_group_activation "
                f"where gym_id='{tid_a}'") == prior_receipt


def test_activation_backfills_other_tenant_before_first_near_claim():
    base = _phash()
    tid_a, group_a = _seed_tenant()
    url_a, _fp_a, _candidate_a = _seed_object(
        tid_a, group_a, _near(base, 2))
    tid_b, group_b = _seed_tenant()
    url_b, _fp_b, _candidate_b = _seed_object(tid_b, group_b, base)
    historical_b = _insert_published_history(tid_b, group_b, url_b)
    # Model the real predecessor state: the visual-group history ledger was
    # already backfilled, while the later pHash scene-history ledger was not.
    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id='{tid_b}'")
    _one(f"select public.visual_group_backfill_gym('{tid_b}', false)::text")
    assert _occupied(tid_b) == []

    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id='{tid_a}'")
    _one(f"select public.visual_group_activate_guard('{tid_a}',"
         " 'scene-history-fleet-test')::text")

    # Tenant A's activation receipt must have occupied B before A's first
    # runtime claim. A's near match therefore commits held, never unused.
    assert _occupied(tid_b) == [base]
    receipt = json.loads(_one(
        "select proof->'scene_history' from public.visual_group_activation "
        f"where gym_id='{tid_a}'"))
    assert receipt["scope"] == "fleet"
    assert any(r["calendar_row_id"] == historical_b
               and r["tenant_id"] == tid_b
               and r["status"] in ("recorded", "already_recorded")
               for r in receipt["rows"])

    row_a = _insert_row(tid_a, group_a, url_a)
    assert _row(row_a)["media_not_ready_reason"] == "scene_review_hold"
    assert len(_holds(tid_a)) == 1
    assert _occupied(tid_a) == []


def test_history_backfill_ambiguous_candidate_blocks_even_same_phash():
    tid, group = _seed_tenant()
    phash = _phash()
    url, fingerprint, _candidate = _seed_object(tid, group, phash)
    _one("select public.visual_scene_register_candidate("
         f"'{tid}','{group}','{phash}','{url}','{fingerprint}',"
         f"jsonb_build_object('verified_bytes','{fingerprint}'),"
         "'scene-history-test','display')")
    row_id = _insert_published_history(tid, group, url)
    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id='{tid}'")

    done = _run(
        f"select public.visual_group_activate_guard('{tid}',"
        " 'scene-history-test')", check=False)
    assert done.returncode != 0
    assert "unresolved scene history requires review" in done.stderr
    audit = _history_audit(row_id)
    assert audit["backfill_status"] == "unresolved"
    assert audit["reason"] == "ambiguous_candidate"
    assert _occupied(tid) == []


def test_history_backfill_uses_current_live_row_not_prior_audit_snapshot():
    tid, group = _seed_tenant()
    url, _fingerprint, _candidate = _seed_object(tid, group, _phash())
    row_id = _insert_published_history(tid, group, url)
    assert _history_audit(row_id)["backfill_status"] == "ready"
    # Drift after the read-only audit. The owner backfill must reload the live
    # calendar row and refuse the now-source-null proof, never use old facts.
    _sql("set session_replication_role=replica; update public.content_calendar "
         f"set source_media_url=null where id='{row_id}'")
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    audit = _history_audit(row_id)
    assert audit["backfill_status"] == "unresolved"
    assert audit["reason"] == "source_null"
    assert _occupied(tid) == []


def test_history_backfill_fleet_lock_contention_fails_closed():
    tid, group = _seed_tenant()
    url, _fingerprint, _candidate = _seed_object(tid, group, _phash())
    _insert_published_history(tid, group, url)
    locker = subprocess.Popen(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN, "-c",
         "select pg_advisory_lock(hashtextextended("
         "jsonb_build_array('visual_scene_global')::text,0)); "
         "select pg_sleep(20);"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            held = _one("select count(*) from pg_locks where locktype='advisory' "
                        "and pid<>pg_backend_pid()")
            if held == "1":
                break
            assert locker.poll() is None
            time.sleep(.05)
        assert held == "1"
        done = _run("select public.visual_scene_backfill_occupied()", check=False)
        assert done.returncode != 0
        assert "global scene claim busy; retry transaction" in done.stderr
        assert _occupied(tid) == []
    finally:
        locker.kill()
        locker.wait(timeout=30)
        _sql("select pg_terminate_backend(pid) from pg_stat_activity "
             "where pid<>pg_backend_pid() and datname=current_database()")


def test_same_component_exact_prepare_makes_backfill_retry_without_deadlock():
    tid, group = _seed_tenant()
    url, _fingerprint, _candidate = _seed_object(tid, group, _phash())
    _insert_published_history(tid, group, url)
    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id='{tid}'")

    prepare_url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    prepare_fp = "md5:" + uuid.uuid4().hex
    prepare_receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,"
        " evidence_ref,observed_by) values "
        f"('{tid}','{prepare_url}','{prepare_fp}',2048,"
        " 'verified_object_read','prepare-race','scene-history-test') "
        "returning receipt_id")
    prepare_sql = (
        "begin; "
        "select public.visual_group_lock_scene_components("
        "jsonb_build_array(jsonb_build_object("
        f"'gym_id','{tid}','group_key','{group}'))); "
        "select pg_sleep(1.5); "
        "select public.visual_global_prepare_source_rendition("
        f"'{tid}','{group}','{prepare_receipt}','{prepare_receipt}',"
        "null,'scene-history-test'); commit")
    env = os.environ.copy()
    env["PGAPPNAME"] = "scene_history_prepare_order"
    prepare = subprocess.Popen(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", prepare_sql], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env)
    try:
        sleeping = "0"
        for _ in range(100):
            sleeping = _one(
                "select count(*) from pg_stat_activity where "
                "application_name='scene_history_prepare_order' "
                "and wait_event='PgSleep'")
            if sleeping == "1":
                break
            assert prepare.poll() is None
            time.sleep(.02)
        assert sleeping == "1"

        first = _run("select public.visual_scene_backfill_occupied()",
                     check=False)
        assert first.returncode != 0
        assert "scene component busy; retry transaction" in first.stderr
        assert _occupied(tid) == []

        stdout, stderr = prepare.communicate(timeout=30)
        assert prepare.returncode == 0, stderr or stdout
        assert _one("select public.visual_scene_backfill_occupied()") == "1"
        assert _occupied(tid) != []
        assert _one("select count(*) from "
                    "public.visual_global_object_attestation where "
                    f"exact_url='{prepare_url}'") == "1"
    finally:
        if prepare.poll() is None:
            prepare.kill()
            prepare.wait(timeout=30)
        _sql("select pg_terminate_backend(pid) from pg_stat_activity "
             "where pid<>pg_backend_pid() and datname=current_database()")


def test_unrelated_exact_prepare_makes_backfill_retry_then_succeed():
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    url_a, _fingerprint_a, _candidate_a = _seed_object(
        tid_a, group_a, _phash())
    _insert_published_history(tid_a, group_a, url_a)
    _sql("update public.gym_visual_guard_settings set enforce=false "
         f"where gym_id in ('{tid_a}','{tid_b}')")

    prepare_url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    prepare_fp = "md5:" + uuid.uuid4().hex
    prepare_receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,"
        " evidence_ref,observed_by) values "
        f"('{tid_b}','{prepare_url}','{prepare_fp}',2048,"
        " 'verified_object_read','prepare-retry','scene-history-test') "
        "returning receipt_id")
    prepare_sql = (
        "begin; select public.visual_global_prepare_source_rendition("
        f"'{tid_b}','{group_b}','{prepare_receipt}','{prepare_receipt}',"
        "null,'scene-history-test'); select pg_sleep(1.5); commit")
    env = os.environ.copy()
    env["PGAPPNAME"] = "scene_history_prepare_retry"
    prepare = subprocess.Popen(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", prepare_sql], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env)
    try:
        sleeping = "0"
        for _ in range(100):
            sleeping = _one(
                "select count(*) from pg_stat_activity where "
                "application_name='scene_history_prepare_retry' "
                "and wait_event='PgSleep'")
            if sleeping == "1":
                break
            assert prepare.poll() is None
            time.sleep(.02)
        assert sleeping == "1"

        first = _run("select public.visual_scene_backfill_occupied()",
                     check=False)
        assert first.returncode != 0
        assert "scene proof writer busy; retry transaction" in first.stderr
        assert _occupied(tid_a) == []

        stdout, stderr = prepare.communicate(timeout=30)
        assert prepare.returncode == 0, stderr or stdout
        assert _one("select public.visual_scene_backfill_occupied()") == "1"
        assert _occupied(tid_a) != []
    finally:
        if prepare.poll() is None:
            prepare.kill()
            prepare.wait(timeout=30)
        _sql("select pg_terminate_backend(pid) from pg_stat_activity "
             "where pid<>pg_backend_pid() and datname=current_database()")


# ---- publish-claim transition freshness (Astra/Sol finding, 2026-10-04) ----
#
# The authoritative claim RPC turns an eligible OLD pending/approved row
# into a NEW publishing row carrying its claim token + reservation, which
# makes NEW ambiguous BY CONSTRUCTION. The merged guard's scene branch used
# to require `not visual_group_row_ambiguous(new)`, skipping the claim-time
# freshness scan. `is_publish_claim_transition` now admits exactly that one
# transition to the SAME locks/scan/held-mutation/sync/hold-last path.

def _claim_update(row_id, date="2026-10-10", reservation=True):
    """Simulated PR230 claim-RPC write shape (the real token mint is an
    activation-draft 0A000 stub; the persisted-state transition is what the
    guard sees). Runs with triggers ENABLED in an ordinary session."""
    res = f", publish_reservation_day = '{date}'" if reservation else ""
    _sql("update public.content_calendar set status = 'publishing',"
         " publish_claim_token = gen_random_uuid()"
         f"{res} where id = '{row_id}'")


def _forced_clean_insert(tid, group, url, date="2026-10-10",
                         status="pending", keyed=True):
    """FORCED-DRIFT staging only: insert a clean, keyed, ready row with the
    trigger chain disabled (session_replication_role=replica is scratch-DB
    only, never production). This stages the post-staging drift the
    claim-time freshness scan defends against; it is NOT a reachable normal
    write path — armed writers record occupancy at staging time.
    keyed=False leaves visual_group_key NULL to stage the Terra P0 shape:
    alias-resolvable media whose key is only hydrated by the BEFORE trigger
    at claim time."""
    cols = "(gym_id, account, post_date, status, variant_status," \
           " image_url, source_media_url"
    vals = f"('{tid}', 'ig', '{date}', '{status}', 'active'," \
           f" '{url}', '{url}'"
    if keyed:
        cols += ", visual_group_key"
        vals += f", '{group}'"
    return _one(
        "set session_replication_role = replica; "
        "insert into public.content_calendar"
        f"{cols}) values {vals}) returning id")


def test_publish_claim_transition_clean_claim_passes_and_keeps_token():
    # Clean publish claim through the freshness gate: no conflict, the row
    # commits publishing with its token and reservation, no hold, and the
    # staging-time occupancy is unchanged (idempotent re-record).
    tid, group = _seed_tenant()
    url, _fp, _cand = _seed_object(tid, group, _phash())
    row_id = _insert_row(tid, group, url)
    assert len(_occupied(tid)) == 1
    _claim_update(row_id)
    row = _row(row_id)
    assert row["status"] == "publishing"
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert row["publish_claim_token"] is not None
    assert row["publish_reservation_day"] == "2026-10-10"
    assert _holds(tid) == []
    assert len(_occupied(tid)) == 1


def test_publish_claim_transition_forced_drift_conflict_commits_held_row():
    # FORCED-DRIFT DEFENSE (staged via owner SQL with the trigger disabled;
    # not a reachable normal exploit — armed writers record occupancy at
    # staging time). Tenant A occupies a scene; tenant B's near-match row is
    # staged clean by bypass; the authoritative publish-claim transition
    # must run the freshness scan and commit HELD with a durable open hold.
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fpa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)
    assert len(_occupied(tid_a)) == 1
    tid_b, group_b = _seed_tenant()
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, _near(base, 2))
    row_id = _forced_clean_insert(tid_b, group_b, url_b)
    # drift staged: clean pending row, no occupancy, no hold
    assert _row(row_id)["media_not_ready_reason"] is None
    assert _occupied(tid_b) == []
    assert _holds(tid_b) == []
    # the authoritative claim transition hits the freshness scan: HELD.
    _claim_update(row_id)
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    holds = _holds(tid_b)
    assert len(holds) == 1
    assert holds[0]["state"] == "open"
    assert holds[0]["hold_kind"] == "near_frame"
    assert holds[0]["calendar_row_id"] == row_id
    # no occupancy for the rejected claim; the persisted claimant returns
    # NULL and the held row is never approvable
    assert _occupied(tid_b) == []
    assert _one("select public.visual_scene_publish_claim_guarded("
                f"'{row_id}') is null") == "t"
    assert _one("select public.visual_scene_approval_guarded("
                f"'{row_id}')") == "f"


def test_publish_claim_transition_hydrated_null_key_conflict_commits_held():
    # TERRA P0-1 REGRESSION — FORCED-DRIFT DEFENSE (owner-only staging with
    # triggers disabled; not a reachable normal write path). OLD row has
    # NULL visual_group_key but alias-resolvable media; the BEFORE trigger
    # hydrates the authoritative resolved key at claim time. The claim-time
    # freshness scan must run against that RESOLVED NEW identity: the
    # near-frame collision with tenant A commits HELD with no token — a
    # keyed-only predicate used to skip the scan and return the token.
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fpa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)
    assert len(_occupied(tid_a)) == 1
    tid_b, group_b = _seed_tenant()
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, _near(base, 2))
    row_id = _forced_clean_insert(tid_b, None, url_b, keyed=False)
    # drift staged: clean pending row, NULL key, no occupancy, no hold
    assert _row(row_id)["visual_group_key"] is None
    assert _occupied(tid_b) == []
    assert _holds(tid_b) == []
    # the authoritative claim transition hydrates the resolved key and hits
    # the freshness scan on that identity: HELD.
    _claim_update(row_id)
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    # the held row carries the trigger-resolved authoritative key, proving
    # the scan ran on the resolved identity, not a caller hint
    assert row["visual_group_key"] == group_b
    holds = _holds(tid_b)
    assert len(holds) == 1
    assert holds[0]["state"] == "open"
    assert holds[0]["hold_kind"] == "near_frame"
    assert holds[0]["calendar_row_id"] == row_id
    assert _occupied(tid_b) == []
    assert _one("select public.visual_scene_publish_claim_guarded("
                f"'{row_id}') is null") == "t"


def test_claim_on_unreconciled_ambiguous_old_sibling_returns_no_token():
    # TERRA P0-2 REGRESSION: an OLD pending row whose usage sibling is
    # ambiguous with NULL original_claim_token passes the claim RPC pre-read
    # and no earlier guard (the original-IDs guard needs known IDs). The
    # claim must be REFUSED outright — the UPDATE raises and rolls back, so
    # no token is committed or returned and no provider send can reference
    # it. A false is_publish_claim_transition that merely skips the
    # freshness scan is not sufficient.
    tid, group = _seed_tenant()
    url, _fp, _cand = _seed_object(tid, group, _phash())
    row_id = _insert_row(tid, group, url)
    # Stage the unreconciled ambiguous prior-attempt evidence directly
    # (owner-only staging of persisted sibling state; sibling rows are
    # normally written by the guard's sync path). NULL original_claim_token
    # is exactly the Terra P0 shape.
    _sql("insert into public.visual_group_usage_sibling"
         "(gym_id, group_key, calendar_row_id, channel, ambiguous,"
         " original_claim_token, original_image_url) values "
         f"('{tid}', '{group}', '{row_id}', 'ig', true, null, '{url}')"
         " on conflict (gym_id, group_key, calendar_row_id) do update set"
         " ambiguous = true, original_claim_token = null")
    # Real claimant update shape: status + reservation + fresh token.
    res = _run("update public.content_calendar set status = 'publishing',"
               " publish_claim_token = gen_random_uuid(),"
               " publish_reservation_day = '2026-10-10'"
               f" where id = '{row_id}'", check=False)
    assert res.returncode != 0
    assert ("publish claim refused: unreconciled ambiguous send identity"
            in res.stderr)
    # rollback: no token, no reservation, no status change, no hold writes
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    assert _holds(tid) == []


def test_claim_shape_without_reservation_is_rejected_and_returns_no_token():
    # TERRA P0-3 REGRESSION — narrowness pin turned fail-closed: a fresh-token
    # UPDATE that does NOT match the exact claim shape (here: token WITHOUT
    # reservation) is not admitted by is_publish_claim_transition, and the
    # merged trigger now REFUSES it outright instead of merely skipping the
    # freshness scan and committing the token. The UPDATE raises and rolls
    # back: no token, no reservation, no status change, no hold writes.
    tid_a, group_a = _seed_tenant()
    base = _phash()
    url_a, _fpa, _ca = _seed_object(tid_a, group_a, base)
    _insert_row(tid_a, group_a, url_a)
    tid_b, group_b = _seed_tenant()
    url_b, _fpb, _cb = _seed_object(tid_b, group_b, _near(base, 2))
    # FORCED-DRIFT staging only (owner SQL, triggers disabled; see
    # _forced_clean_insert): stages the clean unoccupied OLD row.
    row_id = _forced_clean_insert(tid_b, group_b, url_b)
    res = _run("update public.content_calendar set status = 'publishing',"
               " publish_claim_token = gen_random_uuid()"
               f" where id = '{row_id}'", check=False)
    assert res.returncode != 0
    assert ("publish claim refused: not an authoritative claim transition"
            in res.stderr)
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    assert _holds(tid_b) == []


def _assert_claim_with_identity_change_rejected(row_id, tid, extra_set):
    """Shared assertion: a clean OLD row written with the full claim shape
    (status + fresh token + reservation) but a CHANGED identity column must
    be refused by the fail-closed fresh-token guard with rollback."""
    res = _run("update public.content_calendar set status = 'publishing',"
               " publish_claim_token = gen_random_uuid(),"
               " publish_reservation_day = '2026-10-10',"
               f" {extra_set} where id = '{row_id}'", check=False)
    assert res.returncode != 0
    assert ("publish claim refused: not an authoritative claim transition"
            in res.stderr)
    row = _row(row_id)
    assert row["status"] == "pending"
    assert row["variant_status"] == "active"
    assert row["media_not_ready_reason"] is None
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    assert _holds(tid) == []


def test_claim_with_changed_media_is_rejected_and_returns_no_token():
    # TERRA P0-3 REGRESSION: clean OLD keyed row, full claim shape, but the
    # SAME write swaps the delivered media. Both URLs are valid registered,
    # attested objects in this tenant, so the ONLY reason to refuse is the
    # fresh-token fail-closed guard (a false is_publish_claim_transition may
    # never merely skip the scan).
    tid, group_a = _seed_tenant()
    url_a, _fpa, _ca = _seed_object(tid, group_a, _phash())
    row_id = _insert_row(tid, group_a, url_a)
    url_b, _fpb, _cb = _seed_object(tid, group_a, _phash())
    _assert_claim_with_identity_change_rejected(
        row_id, tid,
        f"image_url = '{url_b}', source_media_url = '{url_b}'")
    row = _row(row_id)
    assert row["visual_group_key"] == group_a


def test_claim_with_changed_post_date_is_rejected_and_returns_no_token():
    # TERRA P0-3 REGRESSION: clean OLD row, full claim shape, but the SAME
    # write moves the post date. Refused by the fresh-token guard; rollback
    # leaves the row pending with no token.
    tid, group = _seed_tenant()
    url, _fp, _cand = _seed_object(tid, group, _phash())
    row_id = _insert_row(tid, group, url)
    _assert_claim_with_identity_change_rejected(
        row_id, tid, "post_date = '2026-10-11'")


def test_claim_with_changed_account_is_rejected_and_returns_no_token():
    # TERRA P0-3 REGRESSION: clean OLD row, full claim shape, but the SAME
    # write moves the account. Refused by the fresh-token guard; rollback
    # leaves the row pending with no token.
    tid, group = _seed_tenant()
    url, _fp, _cand = _seed_object(tid, group, _phash())
    row_id = _insert_row(tid, group, url)
    _assert_claim_with_identity_change_rejected(
        row_id, tid, "account = 'fb'")
