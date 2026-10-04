"""Scene ledger coverage gate: static source contracts + OPTIONAL
real-PostgreSQL fail-closed checks.

Static tests always run. PG scenarios run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_ledger_coverage_gate_20261004.sql` exists,
  * SCENE_LEDGER_GATE_TEST_DSN names a disposable Unix-socket database
    literally named `echo_scene_ledger_gate_test` (same house rule as
    tests/test_scene_claim_wave_pg.py — never a network or production host),
  * local psql is on PATH.

The module fixture applies the full draft stack PLUS the ledger gate draft,
committed, onto that scratch database. Every scenario is committed to the
disposable scratch database only. Host PostgreSQL here cannot start
(shmget ENOSPC/EPERM): without the DSN every PG test skips and only the
static source contracts run — they never touch a database.
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("SCENE_LEDGER_GATE_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
BACKFILL = MIGRATIONS / "DRAFT_visual_scene_history_backfill_20261004.sql"
GATE = MIGRATIONS / "DRAFT_visual_scene_ledger_coverage_gate_20261004.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_history_backfill_20261004.sql",
    "DRAFT_visual_scene_ledger_coverage_gate_20261004.sql",
]

PG_READY = bool(DSN) and PSQL is not None and GATE.exists()


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


def _check_dsn():
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_ledger_gate_test"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_ledger_gate_test allowed"
    assert _one("select current_database()") == "echo_scene_ledger_gate_test"


def _fresh_scratch_schema():
    # Drafts are not re-appliable (policies have no IF NOT EXISTS): start clean.
    _sql("drop schema public cascade; create schema public; "
         "grant usage on schema public to public;")
    _sql("do $$ begin "
         "if not exists (select 1 from pg_roles where rolname='anon') then "
         "create role anon nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='authenticated') then "
         "create role authenticated nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='service_role') then "
         "create role service_role nologin; end if; end $$")
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
    _sql("create or replace function public.visual_group_row_active(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")
    _sql("create or replace function public.visual_group_row_ambiguous(public.content_calendar)"
         " returns boolean language sql stable as $$ select false $$")


def _apply(script):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr


def _gate_body():
    """The draft file minus ONLY its own outer begin;/commit; wrapper."""
    lines = GATE.read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().lower() == "begin;"]
    ends = [i for i, ln in enumerate(lines) if ln.strip().lower() == "commit;"]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]
    return "\n".join(lines[starts[0] + 1:ends[0]])


def _catalog_snapshot():
    return _one(
        "select coalesce(string_agg(x, E'\\n' order by x), '') from ("
        "select 'p:'||p.proname||':'||p.prosrc as x from pg_proc p "
        " join pg_namespace n on n.oid=p.pronamespace "
        " where n.nspname='public' "
        "union all "
        "select 'r:'||c.relname||':'||c.relkind from pg_class c "
        " join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' and c.relkind in ('r','v','m','S','i') "
        "union all "
        "select 'g:'||t.tgname from pg_trigger t where not t.tgisinternal) s")


@pytest.fixture(scope="module")
def install_probes():
    """Rollback-only installation probe + forged-receipt late-install
    refusal. Runs BEFORE any committed scenario setup on the scratch DB."""
    if not PG_READY:
        pytest.skip("ledger-gate PG checks need SCENE_LEDGER_GATE_TEST_DSN on "
                    "a disposable local DB and the gate migration present")
    _check_dsn()
    _fresh_scratch_schema()
    # Base stack WITHOUT the gate draft, committed.
    _apply("".join((MIGRATIONS / n).read_text() + "\n" for n in STACK[:-1]))

    # (a) Rollback-only probe: run the gate body in ONE transaction and roll
    # it back; the public catalog must be byte-identical afterwards.
    before = _catalog_snapshot()
    _apply("begin;\n" + _gate_body() + "\nrollback;\n")
    assert _catalog_snapshot() == before, \
        "rollback-only install probe left catalog changes behind"

    # (b) Forged-receipt late install: an armed tenant carrying a stored JSON
    # receipt that CLAIMS ledger-aware coverage (forgable under the prior
    # hook) must still refuse installation, with nothing applied.
    tid, group = _seed_tenant()
    forged = json.dumps({"scene_history": {
        "scope": "fleet", "coverage": "calendar_and_ledger",
        "unresolved": 0, "transaction_id": 1}})
    _sql("set session_replication_role = replica; "
         "insert into public.visual_group_activation(gym_id, proof, actor) "
         f"values ('{tid}', '{forged}'::jsonb, 'ledger-gate-test'); "
         "insert into public.gym_visual_guard_settings(gym_id, enforce) "
         f"values ('{tid}', true);")
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=GATE.read_text(), text=True, capture_output=True, timeout=300)
    assert done.returncode != 0
    assert "must be installed before any tenant is armed" in done.stderr
    # install rolled back: no gate function exists and the base backfill
    # function was not replaced
    assert _one("select count(*) from pg_proc p join pg_namespace n "
                "on n.oid=p.pronamespace where n.nspname='public' and "
                "p.proname='visual_scene_ledger_obligations'") == "0"
    assert "calendar_and_ledger" not in _one(
        "select prosrc from pg_proc "
        "where proname='visual_scene_history_backfill_locked'")
    assert _catalog_snapshot() == before


@pytest.fixture(scope="module")
def scratch_stack(install_probes):
    if not PG_READY:
        pytest.skip("ledger-gate PG checks need SCENE_LEDGER_GATE_TEST_DSN on "
                    "a disposable local DB and the gate migration present")
    _check_dsn()
    _fresh_scratch_schema()
    script = ""
    for name in STACK:
        script += (MIGRATIONS / name).read_text() + "\n"
    _apply(script)


@pytest.fixture()
def scratch(scratch_stack):
    """Scenario isolation: clear every table the gate reads or writes.
    session_replication_role=replica bypasses immutability triggers — scratch
    database only, never production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold, "
         "public.visual_scene_phash_occupied, public.visual_scene_candidate, "
         "public.visual_global_usage, public.visual_global_usage_member, "
         "public.visual_global_release_history, "
         "public.visual_group_usage_ledger, "
         "public.visual_group_usage_sibling, public.visual_group, "
         "public.visual_group_alias, public.tenant_alias, "
         "public.visual_global_object_read_receipt, "
         "public.visual_global_object_attestation, "
         "public.visual_global_scene_object_member, "
         "public.visual_global_object_lineage, "
         "public.visual_group_activation, "
         "public.gym_visual_guard_settings cascade")


# ---- deterministic Walsh-Hadamard pHash codebook -----------------------------

def _codebook():
    """word_i bit j = parity(i & j); distinct pairs are exactly 32 apart."""
    words = []
    for i in range(16):
        word = sum((bin(i & j).count("1") & 1) << j for j in range(64))
        words.append(format(word, "016x"))
    for a in range(16):
        for b in range(a + 1, 16):
            assert bin(int(words[a], 16) ^ int(words[b], 16)).count("1") == 32
    return words


_CODEBOOK = _codebook()


def _phash(i):
    assert i < len(_CODEBOOK), "scratch phash codebook exhausted"
    return _CODEBOOK[i]


# ---- seeding helpers (all committed to the scratch DB) -----------------------

def _seed_tenant():
    """Unarmed canonical tenant + visual group on the scratch DB."""
    tid = str(uuid.uuid4())
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    return tid, group


def _seed_object(tid, group, phash=None):
    """Fully attested object: read receipt -> attestation -> source+delivered
    scene members -> canonical_url alias -> (when phash given) bound 'display'
    candidate. Returns (url, fingerprint, candidate_id|None)."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    receipt = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'ledger-gate-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{receipt}',"
         " 'ledger-gate-test')")
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
            " 'ledger-gate-test', 'display')")
    return url, fp, cand


def _occupy(tid, group, phash, fp, date="2026-09-01", cal=None):
    _sql("insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, evidence) "
         f"values ('{phash}', '{tid}', '{group}', '{date}', '{fp}', "
         + (f"'{cal}'" if cal else "null") + ", '{}'::jsonb)")


def _owner(tid, fp, date="2026-09-01", state="published"):
    _sql("insert into public.visual_global_usage"
         "(fingerprint, tenant_id, used_date, state) "
         f"values ('{fp}', '{tid}', "
         + (f"'{date}'" if date else "null") + f", '{state}')")


def _member(tid, group, fp, date="2026-09-01", state="published", cal=None):
    _sql("insert into public.visual_global_usage_member"
         "(tenant_id, group_key, fingerprint, calendar_row_id, channel,"
         " used_date, state) values "
         f"('{tid}', '{group}', '{fp}', "
         + (f"'{cal}'" if cal else "null") + ", 'ig', "
         + (f"'{date}'" if date else "null") + f", '{state}')")


def _release(tid, group, fp, date="2026-09-01", cal=None):
    _sql("insert into public.visual_global_release_history"
         "(fingerprint, tenant_id, group_key, used_date, calendar_row_id) "
         f"values ('{fp}', '{tid}', '{group}', '{date}', "
         + (f"'{cal}'" if cal else "null") + ")")


def _ledger(tid, group, date="2026-09-01", state="reserved", cal=None):
    _sql("insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, calendar_row_id, channel, state)"
         f" values ('{tid}', '{group}', "
         + (f"'{date}'" if date else "null") + ", "
         + (f"'{cal}'" if cal else "null") + f", 'ig', '{state}')")


def _receipt():
    return json.loads(_one(
        "select public.visual_scene_history_backfill_locked(null)::text"))


def _ledger_rows(receipt):
    return [r for r in receipt["rows"] if "kind" in r]


def _activate(tid):
    """Attempt arming through the sanctioned RPC; returns CompletedProcess."""
    return _run(
        f"select public.visual_group_activate_guard('{tid}',"
        " 'ledger-gate-test')", check=False)


def _insert_published_row(tid, group, url, date="2026-10-10"):
    """Committed published row; replica mode bypasses the claim guard so the
    historical row is seeded exactly as given (scratch DB only)."""
    return _one(
        "set session_replication_role = replica; "
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status,"
        " image_url, source_media_url, visual_group_key, published_at) values "
        f"('{tid}', 'ig', '{date}', 'published', 'active',"
        f" '{url}', '{url}', '{group}', now()) returning id")


# ---- PG scenarios --------------------------------------------------------------

def test_stack_applies_and_gate_functions_exist(scratch_stack):
    out = _one("select string_agg(proname, ',' order by proname) from pg_proc "
               "where proname in ('visual_scene_ledger_obligations',"
               "'visual_scene_ledger_obligation_evaluate',"
               "'visual_scene_ledger_coverage_audit',"
               "'visual_scene_history_backfill_locked',"
               "'visual_scene_history_activation_receipt')")
    assert out == ("visual_scene_history_activation_receipt,"
                   "visual_scene_history_backfill_locked,"
                   "visual_scene_ledger_coverage_audit,"
                   "visual_scene_ledger_obligation_evaluate,"
                   "visual_scene_ledger_obligations")
    # clean fleet: receipt is ledger-aware and unresolved-free
    r = _receipt()
    assert r["coverage"] == "calendar_and_ledger"
    assert r["scope"] == "fleet"
    assert r["unresolved"] == 0
    assert r["ledger_obligations"] == 0


def test_deleted_published_row_ledger_use_is_enumerated(scratch):
    tid, group = _seed_tenant()
    url, fp, _ = _seed_object(tid, group)
    _owner(tid, fp)
    _member(tid, group, fp)          # published member, calendar row deleted
    r = _receipt()
    assert r["ledger_obligations"] == 2
    rows = _ledger_rows(r)
    member = [x for x in rows if x["kind"] == "global_usage_member"]
    owner = [x for x in rows if x["kind"] == "global_usage_owner"]
    assert len(member) == 1 and len(owner) == 1
    # no surviving calendar row and no occupancy proof -> unresolved, never
    # silently dropped by a join
    assert r["unresolved"] == 2
    assert member[0]["reason"] == "no_scene_occupancy_proof"
    assert owner[0]["reason"] == "no_scene_occupancy_proof"
    # retained identity tied to ACTUAL occupancy resolves it
    _occupy(tid, group, _phash(0), fp)
    r2 = _receipt()
    assert r2["unresolved"] == 0
    assert all(x["reason"] in ("scene_occupancy_proof",)
               for x in _ledger_rows(r2))


def test_denied_released_member_still_an_obligation(scratch):
    tid, group = _seed_tenant()
    url, fp, _ = _seed_object(tid, group)
    _owner(tid, fp, state="released")
    _member(tid, group, fp, state="released")   # denied/released usage
    r = _receipt()
    assert r["unresolved"] == 2
    _occupy(tid, group, _phash(1), fp)
    assert _receipt()["unresolved"] == 0


def test_swapped_old_media_fingerprint_mismatch(scratch):
    tid, group = _seed_tenant()
    url, fp_old, _ = _seed_object(tid, group)
    _owner(tid, fp_old)
    _member(tid, group, fp_old)
    # occupancy exists for the same tenant/date/group but over the CURRENT
    # replacement media's bytes — never proof of the swapped-out old media
    fp_new = "md5:" + uuid.uuid4().hex
    _occupy(tid, group, _phash(2), fp_new)
    r = _receipt()
    rows = _ledger_rows(r)
    member = [x for x in rows if x["kind"] == "global_usage_member"][0]
    owner = [x for x in rows if x["kind"] == "global_usage_owner"][0]
    assert member["reason"] == "fingerprint_mismatch"
    assert owner["reason"] == "fingerprint_mismatch"
    assert r["unresolved"] == 2


def test_ledger_only_reservation_blocks(scratch):
    tid, group = _seed_tenant()
    _ledger(tid, group, state="reserved")     # no calendar row, no occupancy
    r = _receipt()
    rows = _ledger_rows(r)
    assert len(rows) == 1 and rows[0]["kind"] == "group_usage_ledger"
    assert rows[0]["reason"] == "historical_fingerprint_unproven"
    assert r["unresolved"] == 1


def test_orphan_owner_without_member_blocks(scratch):
    tid, group = _seed_tenant()
    fp = "md5:" + uuid.uuid4().hex
    _owner(tid, fp)                            # owner, no member at all
    r = _receipt()
    rows = _ledger_rows(r)
    assert len(rows) == 1 and rows[0]["kind"] == "global_usage_owner"
    assert rows[0]["reason"] == "no_scene_occupancy_proof"
    # orphan owner (no group) resolves only via exact-fingerprint occupancy
    _occupy(tid, group, _phash(3), fp)
    assert _receipt()["unresolved"] == 0


def test_release_only_history_is_an_obligation(scratch):
    tid, group = _seed_tenant()
    url, fp, _ = _seed_object(tid, group)
    _release(tid, group, fp)                   # no surviving usage/member
    r = _receipt()
    rows = _ledger_rows(r)
    assert len(rows) == 1 and rows[0]["kind"] == "release_history"
    assert rows[0]["reason"] == "no_scene_occupancy_proof"
    assert r["unresolved"] == 1
    _occupy(tid, group, _phash(4), fp)
    assert _receipt()["unresolved"] == 0


def test_null_date_never_inferred(scratch):
    tid, group = _seed_tenant()
    # base schema allows a NULL reserved_date only for state='published'
    _ledger(tid, group, date=None, state="published")
    r = _receipt()
    rows = _ledger_rows(r)
    assert rows[0]["reason"] == "missing_date"
    assert r["unresolved"] == 1
    # occupancy on SOME date never resolves a NULL-date obligation
    url, fp, _ = _seed_object(tid, group)
    _occupy(tid, group, _phash(5), fp)
    assert _receipt()["unresolved"] == 1


def test_other_tenant_unresolved_blocks_activation_fleetwide(scratch):
    tid_a, group_a = _seed_tenant()
    tid_b, group_b = _seed_tenant()
    _ledger(tid_b, group_b, state="reserved")  # tenant B's unresolved ledger
    done = _activate(tid_a)                    # arming A must still refuse
    assert done.returncode != 0
    assert "unresolved scene ledger coverage" in done.stderr
    assert _one("select count(*) from public.visual_group_activation") == "0"
    assert _one("select count(*) from public.gym_visual_guard_settings "
                "where enforce") == "0"


def test_staged_candidate_is_never_proof(scratch):
    tid, group = _seed_tenant()
    url, fp, cand = _seed_object(tid, group, phash=_phash(6))
    assert cand is not None                    # staged candidate exists
    _owner(tid, fp)
    _member(tid, group, fp)
    r = _receipt()
    assert r["unresolved"] == 2
    assert all(x["reason"] == "no_scene_occupancy_proof"
               for x in _ledger_rows(r))


def test_wrong_fingerprint_occupied_row_is_unproven_identity(scratch):
    tid, group = _seed_tenant()
    _ledger(tid, group, state="reserved")      # carries no fingerprint
    # occupied row for same tenant/group/date whose fingerprint is NOT an
    # attested delivered object of this group: same group/date alone is not
    # identity — and a fingerprint-free obligation can never prove WHICH
    # bytes it used from any occupancy row, so it stays unresolved
    fp_stray = "md5:" + uuid.uuid4().hex
    _occupy(tid, group, _phash(7), fp_stray)
    r = _receipt()
    rows = _ledger_rows(r)
    assert rows[0]["reason"] == "historical_fingerprint_unproven"
    assert r["unresolved"] == 1


def test_fingerprint_free_ledger_row_is_never_proven_by_row_id(scratch):
    """SAME calendar row id holds old local ledger use AND new replacement
    occupancy; a current published row with the new bytes SURVIVES. None of
    that proves WHICH bytes the fingerprint-free ledger entry used — a
    matching row id is not original-byte evidence, so the obligation stays
    unresolved for operator review."""
    tid, group = _seed_tenant()
    # replacement media B: fully attested, staged, published row SURVIVES
    url_new, fp_new, cand = _seed_object(tid, group, phash=_phash(10))
    assert cand is not None
    row_id = _insert_published_row(tid, group, url_new)
    # old media A's durable local ledger use carries the SAME calendar row id
    _ledger(tid, group, date="2026-10-10", state="published", cal=row_id)
    # replacement occupancy recorded under the SAME calendar row id (new
    # bytes) — plus an attested DELIVERED member for those new bytes
    _occupy(tid, group, _phash(10), fp_new, date="2026-10-10", cal=row_id)
    r = _receipt()
    rows = [x for x in _ledger_rows(r) if x["kind"] == "group_usage_ledger"]
    assert len(rows) == 1
    # the surviving published row with new bytes resolves itself...
    assert any(x.get("status") in ("inserted", "already_recorded")
               for x in r["rows"] if "kind" not in x)
    # ...but the fingerprint-free ledger obligation stays UNRESOLVED: neither
    # Path 1 (surviving published row) nor Path 2 (occupied row) proves
    # original bytes for a shared calendar row id
    assert rows[0]["status"] == "unresolved"
    assert rows[0]["reason"] == "historical_fingerprint_unproven"
    # even occupancy recorded under the ledger's own exact calendar_row_id
    # over attested delivered OLD bytes is not immutable byte-bound proof of
    # the original use — the row id is shared by both byte versions
    url_old, fp_old, _ = _seed_object(tid, group)
    _occupy(tid, group, _phash(11), fp_old, date="2026-10-10", cal=row_id)
    r2 = _receipt()
    rows2 = [x for x in _ledger_rows(r2) if x["kind"] == "group_usage_ledger"]
    assert rows2[0]["status"] == "unresolved"
    assert rows2[0]["reason"] == "historical_fingerprint_unproven"
    # nothing was silently cleared: local usage ledger row is intact
    assert _one("select count(*) from public.visual_group_usage_ledger") == "1"


def test_already_recorded_requires_exact_fingerprint(scratch):
    tid, group = _seed_tenant()
    url, fp, cand = _seed_object(tid, group, phash=_phash(12))
    row_id = _insert_published_row(tid, group, url)
    # same phash/tenant/group/date key but over DIFFERENT stored bytes
    fp_other = "md5:" + uuid.uuid4().hex
    _occupy(tid, group, _phash(12), fp_other, date="2026-10-10")
    r = _receipt()
    assert r["inserted"] == 0
    assert r["already_recorded"] == 0
    assert r["unresolved"] == 1
    mism = [x for x in r["rows"]
            if x.get("reason") == "occupancy_fingerprint_mismatch"]
    assert len(mism) == 1
    assert mism[0]["fingerprint"] == fp
    assert mism[0]["occupied_fingerprint"] == fp_other
    # activation refuses fleet-wide: no write survives
    done = _activate(tid)
    assert done.returncode != 0
    assert "unresolved scene ledger coverage" in done.stderr
    assert _one("select count(*) from public.visual_group_activation") == "0"
    assert _one("select count(*) from public.gym_visual_guard_settings "
                "where enforce") == "0"


def test_valid_verified_history_backfills_and_arms(scratch):
    tid, group = _seed_tenant()
    url, fp, cand = _seed_object(tid, group, phash=_phash(8))
    row_id = _insert_published_row(tid, group, url)
    _owner(tid, fp, date="2026-10-10")
    _member(tid, group, fp, date="2026-10-10", cal=row_id)
    # NOTE: a fingerprint-free ledger row could NEVER arm here even with its
    # published calendar row surviving — a shared calendar_row_id is not
    # original-byte evidence (historical_fingerprint_unproven), so this
    # happy path keeps only fingerprint-bearing obligations
    r = _receipt()
    assert r["coverage"] == "calendar_and_ledger"
    assert r["unresolved"] == 0, r
    assert r["inserted"] == 1
    rows = _ledger_rows(r)
    member = [x for x in rows if x["kind"] == "global_usage_member"][0]
    assert member["reason"] == "calendar_row_proof"
    assert member["status"] == "resolved"
    assert not [x for x in rows if x["kind"] == "group_usage_ledger"]
    # activation succeeds and persists the authoritative recomputed receipt
    done = _activate(tid)
    assert done.returncode == 0, done.stderr
    proof = json.loads(_one(
        "select proof::text from public.visual_group_activation "
        f"where gym_id = '{tid}'"))
    sh = proof["scene_history"]
    assert sh["coverage"] == "calendar_and_ledger"
    assert sh["scope"] == "fleet"
    assert sh["unresolved"] == 0
    assert "ledger_obligations" in sh


def test_incoming_receipt_is_never_trusted(scratch):
    # bogus clean receipt + real unresolved obligation -> refused
    tid, group = _seed_tenant()
    _ledger(tid, group, state="reserved")
    fake = json.dumps({"scene_history": {
        "scope": "fleet", "coverage": "calendar_and_ledger",
        "unresolved": 0, "transaction_id": 1}})
    done = _run(
        "begin; lock table public.content_calendar in share row exclusive mode; "
        "insert into public.visual_group_activation(gym_id, proof, actor) "
        f"values ('{tid}', '{fake}'::jsonb, 'ledger-gate-test'); commit;",
        check=False)
    assert done.returncode != 0
    assert "unresolved scene ledger coverage" in done.stderr
    assert _one("select count(*) from public.visual_group_activation") == "0"
    # without the calendar barrier the hook refuses outright
    done2 = _run(
        "insert into public.visual_group_activation(gym_id, proof, actor) "
        f"values ('{tid}', '{fake}'::jsonb, 'ledger-gate-test')", check=False)
    assert done2.returncode != 0
    assert "calendar write barrier" in done2.stderr


def test_receipt_overwritten_with_authoritative_recompute(scratch):
    tid, group = _seed_tenant()
    fake = json.dumps({"scene_history": {
        "scope": "fleet", "coverage": "calendar_and_ledger",
        "unresolved": 0, "transaction_id": 1}})
    _sql("begin; lock table public.content_calendar in share row exclusive mode; "
         "insert into public.visual_group_activation(gym_id, proof, actor) "
         f"values ('{tid}', '{fake}'::jsonb, 'ledger-gate-test'); commit;")
    proof = json.loads(_one(
        "select proof::text from public.visual_group_activation "
        f"where gym_id = '{tid}'"))
    sh = proof["scene_history"]
    # the stored receipt is the recomputed one, not the incoming fake
    assert int(sh["transaction_id"]) != 1
    assert sh["coverage"] == "calendar_and_ledger"
    assert "ledger_obligations" in sh
    assert sh["unresolved"] == 0


def test_failed_activation_rolls_back_every_write(scratch):
    tid, group = _seed_tenant()
    url, fp, cand = _seed_object(tid, group, phash=_phash(9))
    row_id = _insert_published_row(tid, group, url)   # fully provable row
    tid_b, group_b = _seed_tenant()
    _ledger(tid_b, group_b, state="reserved")          # fleet blocker
    done = _activate(tid)
    assert done.returncode != 0
    # the failed activation transaction rolled back atomically: no activation
    # row, no armed setting, and no occupied write from the recomputation
    assert _one("select count(*) from public.visual_group_activation") == "0"
    assert _one("select count(*) from public.gym_visual_guard_settings "
                "where enforce") == "0"
    assert _one("select count(*) from public.visual_scene_phash_occupied") == "0"


def test_audit_view_reports_ledger_obligations_read_only(scratch):
    tid, group = _seed_tenant()
    _ledger(tid, group, state="reserved")
    out = json.loads(_one(
        "select coalesce(jsonb_agg(row_to_json(a)), '[]'::jsonb)::text "
        "from public.visual_scene_ledger_coverage_audit(null) a"))
    assert len(out) == 1
    assert out[0]["kind"] == "group_usage_ledger"
    assert out[0]["audit_status"] == "unresolved"
    assert out[0]["reason"] == "historical_fingerprint_unproven"
    # read-only: the audit wrote nothing
    assert _one("select count(*) from public.visual_scene_phash_occupied") == "0"


# ---- static source contracts (no database required) ----------------------------

def _function(sql, signature):
    return sql.split(f"create or replace function public.{signature}", 1)[1].split(
        "$$;", 1
    )[0].lower()


def test_gate_is_additive_draft_ordered_after_backfill():
    assert GATE.exists()
    assert GATE.name > BACKFILL.name
    header = GATE.read_text().lower().split("begin;", 1)[0]
    assert "draft / unapplied / off" in header
    assert ("apply strictly after "
            "draft_visual_scene_history_backfill_20261004.sql") in header
    sql = GATE.read_text().lower()
    assert sql.count("begin;") == 1 and sql.rstrip().endswith("commit;")


def test_gate_enumerates_every_ledger_independently():
    sql = GATE.read_text().lower()
    obligations = _function(sql, "visual_scene_ledger_obligations(")
    for table in ("visual_group_usage_ledger", "visual_global_usage_member",
                  "visual_global_usage", "visual_global_release_history"):
        assert f"from public.{table}" in obligations
    # UNION ALL only: no join may erase an obligation
    assert obligations.count("union all") == 3
    assert " join " not in obligations
    # all states enumerated: no state filter anywhere
    assert "state=" not in obligations and "state =" not in obligations


def test_gate_null_dates_and_no_invented_identity():
    sql = GATE.read_text().lower()
    evaluate = _function(sql, "visual_scene_ledger_obligation_evaluate(")
    assert evaluate.index("o_reason := 'missing_date'") < evaluate.index(
        "visual_scene_phash_occupied")
    for token in ("fingerprint_mismatch", "no_scene_occupancy_proof",
                  "historical_fingerprint_unproven",
                  "calendar_row_identity_mismatch", "unmapped_tenant"):
        assert token in evaluate
    # fingerprint-free obligations fail closed immediately after date/tenant
    # validation, before ANY occupancy or surviving-row proof path
    fp_null = evaluate.index("if v_fp is null then")
    assert evaluate.index("o_reason := 'unmapped_tenant'") < fp_null
    assert fp_null < evaluate.index("visual_scene_phash_occupied")
    assert fp_null < evaluate.index("select c.* into v_row")
    # a calendar_row_id / occupied row is never treated as original-byte
    # evidence for fingerprint-free obligations
    assert "o.calendar_row_id=o_calendar_row_id" not in evaluate
    assert "object_role='delivered'" not in evaluate
    # ledger obligations never write occupancy rows
    writer = _function(sql, "visual_scene_history_backfill_locked(")
    ledger_loop = writer.split("for v_obligation in", 1)[1]
    assert "insert into" not in ledger_loop


def test_gate_preserves_lock_order_and_drift_is_fail_closed():
    sql = GATE.read_text().lower()
    writer = _function(sql, "visual_scene_history_backfill_locked(")
    assert writer.index("lock table public.content_calendar") < writer.index(
        "for update")
    assert writer.index("visual_scene_history_lock_components_nowait") < writer.index(
        "lock table public.visual_scene_candidate")
    assert writer.index("lock table public.visual_scene_candidate") < writer.index(
        "pg_try_advisory_xact_lock")
    lock_list = writer.split("lock table public.visual_scene_candidate", 1)[1]
    for relation in ("visual_group_usage_ledger", "visual_global_usage",
                     "visual_global_usage_member",
                     "visual_global_release_history"):
        assert f"public.{relation}" in lock_list.split("nowait", 1)[0]
    # drift re-read happens after the global lock, before any write, and
    # skips all writes when detected
    live_read = writer.index("into v_obligations_live",
                             writer.index("pg_try_advisory_xact_lock"))
    assert writer.index("pg_try_advisory_xact_lock") < live_read
    assert live_read < writer.index(
        "insert into public.visual_scene_phash_occupied")
    assert "ledger_drift_under_lock" in writer
    assert "if not v_ledger_drift then" in writer
    assert "'coverage','calendar_and_ledger'" in writer
    assert "v_unresolved + v_ledger_unresolved" in writer
    # already_recorded requires exact fingerprint equality; a same-key row
    # over different bytes is unresolved and never written
    assert "occupancy_fingerprint_mismatch" in writer
    assert "v_occupied_fp is distinct from v_eval.o_fingerprint" in writer


def test_gate_receipt_recomputes_and_never_trusts_incoming():
    sql = GATE.read_text().lower()
    hook = _function(sql, "visual_scene_history_activation_receipt()")
    assert "sharerowexclusivelock" in hook
    assert "visual_scene_history_backfill_locked(null)" in hook
    assert "'calendar_and_ledger'" in hook
    assert "(v_receipt->>'unresolved')::integer,-1) <> 0" in hook
    assert "unresolved scene ledger coverage requires review" in hook
    assert "jsonb_build_object('scene_history',v_receipt)" in hook
    # no branch preserves an incoming scene_history receipt
    assert "new.proof ? 'scene_history'" not in hook


def test_gate_owner_only_mutations_service_read_audit():
    sql = GATE.read_text().lower()
    for signature in (
        "visual_scene_ledger_obligations(text)",
        "visual_scene_ledger_obligation_evaluate(jsonb)",
        "visual_scene_history_backfill_locked(text)",
        "visual_scene_backfill_occupied()",
        "visual_scene_history_activation_receipt()",
    ):
        assert f"revoke all on function public.{signature}" in sql
    assert ("revoke all on function "
            "public.visual_scene_ledger_coverage_audit(text)") in sql
    assert ("grant execute on function "
            "public.visual_scene_ledger_coverage_audit(text)") in sql
    assert "to service_role" in sql
    # late-install guard refuses ANY already-armed tenant; a stored JSON
    # coverage label on an activation receipt is never consulted
    assert "scene ledger coverage gate must be installed" in sql
    assert "'calendar_and_ledger'" in sql
    install_guard = sql.split("do $$", 1)[1].split("$$;", 1)[0]
    assert "gym_visual_guard_settings" in install_guard
    assert "visual_group_activation" not in install_guard
    assert "proof" not in install_guard
