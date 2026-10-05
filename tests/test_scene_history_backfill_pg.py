"""scene_history_backfill: fail-closed historical occupied-scene coverage
report and published-row backfill checks on a fresh, private, disposable
PostgreSQL 17 cluster (migrations/DRAFT_visual_scene_history_backfill_20261005.sql,
Child A of the DRAFT/OFF Echo history milestone).

Ephemeral PG17 only: the module spins up its own throwaway cluster under a
TemporaryDirectory (no supplied DSN is ever accepted, no network host) and
stops it on exit. If PG17 server binaries are unavailable in this environment
the whole module SKIPS cleanly with an explicit reason — a skip is never
counted as acceptance.

The fixture applies the prerequisite drafts (group schema, claim trigger,
global history, group backfill, group activation, scene ledger) plus the
history-backfill draft, committed, with a stub content_calendar carrying the
columns the stack needs. Tenants are seeded UNARMED (the backfill is a
pre-activation import and refuses to run once any tenant is armed).

Scenarios prove: fully-evidenced published rows are 'backfillable' and the
backfill records exactly their owner-receipt pHash occupancy (idempotent on
retry, 'covered' afterwards); every failure mode (unmapped tenant,
unresolved group, missing date, no delivered object, no/ambiguous owner
receipt, unverified lineage) stays an EXPLICIT named blocker and is never
written; the backfill refuses (0A000) once any tenant is armed; it writes no
candidates, holds or scene links; and the privilege posture keeps the
backfill owner-only while the coverage report is service_role-readable.
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
DRAFT = MIGRATIONS / "DRAFT_visual_scene_history_backfill_20261005.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_ledger_20261005.sql",
    "DRAFT_visual_scene_history_backfill_20261005.sql",
]
BIN_CANDIDATES = [
    Path("/opt/homebrew/opt/postgresql@17/bin"),
    Path("/usr/local/opt/postgresql@17/bin"),
    Path("/opt/homebrew/opt/libpq/bin"),
]
BIN = next(
    (b for b in BIN_CANDIDATES
     if (b / "postgres").exists() and (b / "initdb").exists()),
    None,
)

PG17_AVAILABLE = False
if BIN is not None:
    try:
        PG17_AVAILABLE = "17." in subprocess.run(
            [str(BIN / "postgres"), "--version"],
            text=True, capture_output=True, timeout=30).stdout
    except Exception:
        PG17_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PG17_AVAILABLE or not DRAFT.exists(),
    reason="PG17 server binaries unavailable in this environment (or the "
           "history-backfill draft is missing); ephemeral-PG checks skip "
           "cleanly and are not counted as acceptance",
)

PORT = "55481"


def command(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True,
                          timeout=180, **kwargs)


def _run(statement, check=True):
    done = subprocess.run(
        [str(BIN / "psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
         "-p", PORT, "-h", _SOCK, "-d", "echo_scene_history_test",
         "-c", statement], text=True, capture_output=True, timeout=60)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _sql(statement):
    return _run(statement).stdout.strip()


def _one(statement):
    out = _sql(statement)
    return out.splitlines()[-1] if out else ""


_SOCK = None


@pytest.fixture(scope="module", autouse=True)
def cluster():
    global _SOCK
    if not PG17_AVAILABLE or not DRAFT.exists():
        pytest.skip("PG17 unavailable; clean skip, no acceptance claimed")
    with tempfile.TemporaryDirectory(prefix="echo-scene-history-") as temp:
        base = Path(temp)
        data = base / "data"
        _SOCK = str(base)
        try:
            command([str(BIN / "initdb"), "-D", str(data), "-A", "trust",
                     "--no-locale"])
        except subprocess.CalledProcessError as exc:
            pytest.skip(
                "initdb failed in this environment (e.g. sandboxed SysV "
                "shmget EPERM); the disposable PG17 checks must run "
                "unsandboxed — a skip is never acceptance. stderr: "
                + (exc.stderr or "")[-400:])
        command([str(BIN / "pg_ctl"), "-D", str(data),
                 "-l", str(base / "server.log"),
                 "-o", f"-k {base} -p {PORT} -h ''", "-w", "start"])
        try:
            command([str(BIN / "createdb"), "-h", str(base), "-p", PORT,
                     "echo_scene_history_test"])
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
            script = ""
            for name in STACK:
                script += (MIGRATIONS / name).read_text() + "\n"
            done = subprocess.run(
                [str(BIN / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1",
                 "-p", PORT, "-h", str(base), "-d", "echo_scene_history_test"],
                input=script, text=True, capture_output=True, timeout=180)
            assert done.returncode == 0, done.stderr
            yield
        finally:
            command([str(BIN / "pg_ctl"), "-D", str(data), "-m", "immediate",
                     "-w", "stop"])
    _SOCK = None


@pytest.fixture(autouse=True)
def reset(cluster):
    """Scenario isolation on the scratch cluster. session_replication_role=
    replica bypasses the immutability/truncate guards — scratch database
    only, never production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold,"
         " public.visual_scene_phash_occupied, public.visual_scene_candidate,"
         " public.visual_scene_owner_phash_receipt,"
         " public.visual_global_usage, public.visual_global_usage_member,"
         " public.visual_group_usage_ledger,"
         " public.visual_group_usage_sibling,"
         " public.visual_global_object_lineage,"
         " public.visual_global_scene_object_member,"
         " public.visual_global_object_attestation,"
         " public.visual_global_render_receipt,"
         " public.visual_global_object_read_receipt,"
         " public.visual_group_scene_link,"
         " public.visual_group_alias, public.visual_group,"
         " public.gym_visual_guard_settings,"
         " public.tenant_alias cascade")


# ---- seeding helpers ---------------------------------------------------------

def _seed_tenant():
    """Canonical UNARMED tenant + visual group on the scratch cluster."""
    tid = str(uuid.uuid4())
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    return tid, group


def _seed_object(tid, group, phash=None, receipt=True):
    """Fully attested delivered object: read receipt -> attestation -> both
    scene roles -> canonical_url alias -> (optionally) owner pHash receipt."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    receipt_id = _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'scene-history-test') returning receipt_id")
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{receipt_id}',"
         " 'scene-history-test')")
    for role in ("source", "delivered"):
        _sql("insert into public.visual_global_scene_object_member"
             "(tenant_id, group_key, exact_url, fingerprint, object_role) "
             f"values ('{tid}', '{group}', '{url}', '{fp}', '{role}')")
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"('{tid}', 'canonical_url', '{url}', '{group}')")
    if phash is not None and receipt:
        _sql("insert into public.visual_scene_owner_phash_receipt "
             "(receipt_id,tenant_id,group_key,object_role,exact_url,"
             "fingerprint,phash,byte_length,algorithm) values "
             f"(gen_random_uuid(),'{tid}','{group}','display','{url}',"
             f"'{fp}','{phash}',1024,'echo-dct-phash64-v1')")
    return url, fp


def _insert_published(tid, url, date="2026-09-20", source_url="__same__"):
    source = f"'{url}'" if source_url == "__same__" else (
        f"'{source_url}'" if source_url else "null")
    img = f"'{url}'" if url else "null"
    return _one(
        "insert into public.content_calendar"
        "(gym_id, account, post_date, status, variant_status, published_at,"
        " image_url, source_media_url) values "
        f"('{tid}', 'ig', {f'{date!r}' if date else 'null'}, 'published',"
        f" 'active', '2026-09-20T12:00:00Z', {img}, {source}) returning id")


def _coverage():
    return [json.loads(r) for r in _sql(
        "select jsonb_build_object('calendar_row_id', calendar_row_id,"
        " 'state', state, 'blocker_reason', blocker_reason,"
        " 'phash', phash, 'tenant_id', tenant_id, 'group_key', group_key,"
        " 'post_date', post_date)::text"
        " from public.visual_scene_history_coverage()").splitlines() if r]


def _occupied():
    return [json.loads(r) for r in _sql(
        "select jsonb_build_object('phash', phash, 'tenant_id', tenant_id,"
        " 'group_key', group_key, 'used_date', used_date,"
        " 'fingerprint', fingerprint, 'calendar_row_id', calendar_row_id,"
        " 'evidence', evidence)::text"
        " from public.visual_scene_phash_occupied").splitlines() if r]


_PHASH_A = "aaaaaaaaaaaaaaaa"
_PHASH_B = "5555555555555555"


# ---- tests -------------------------------------------------------------------

def test_fully_evidenced_published_row_backfills_and_is_idempotent():
    tid, group = _seed_tenant()
    url, fp = _seed_object(tid, group, _PHASH_A)
    row = _insert_published(tid, url)
    cov = _coverage()
    assert len(cov) == 1
    assert cov[0]["state"] == "backfillable"
    assert cov[0]["calendar_row_id"] == row
    assert cov[0]["phash"] == _PHASH_A
    assert (cov[0]["tenant_id"], cov[0]["group_key"]) == (tid, group)
    assert _one("select public.visual_scene_backfill_occupied()") == "1"
    occ = _occupied()
    assert len(occ) == 1
    assert (occ[0]["phash"].strip(), occ[0]["tenant_id"],
            occ[0]["group_key"], occ[0]["used_date"],
            occ[0]["fingerprint"]) == (_PHASH_A, tid, group, "2026-09-20", fp)
    assert occ[0]["calendar_row_id"] == row
    assert occ[0]["evidence"]["source"] == "historical_backfill_draft_20261005"
    assert occ[0]["evidence"]["owner_phash_receipt"]
    # Idempotent retry: nothing new, row now reported covered.
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    assert [c["state"] for c in _coverage()] == ["covered"]
    # Backfill never stages candidates, holds or scene links.
    assert _one("select count(*) from public.visual_scene_candidate") == "0"
    assert _one("select count(*) from public.visual_scene_review_hold") == "0"
    assert _one("select count(*) from public.visual_group_scene_link") == "0"


@pytest.mark.parametrize("mode,reason", [
    ("unmapped_tenant", "unmapped_tenant"),
    ("unresolved_group", "unresolved_group"),
    ("missing_date", "missing_date"),
    # A published row with no image_url cannot even resolve a group; the
    # missing delivered object surfaces as the group blocker.
    ("no_object", "unresolved_group"),
    ("no_receipt", "no_owner_phash_receipt"),
    ("ambiguous_receipt", "ambiguous_owner_receipt"),
    ("bad_lineage", "lineage_unverified"),
])
def test_unresolved_rows_stay_explicit_blockers(mode, reason):
    tid, group = _seed_tenant()
    if mode == "unmapped_tenant":
        url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
        row = _insert_published(str(uuid.uuid4()), url)
    elif mode == "unresolved_group":
        url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
        row = _insert_published(tid, url)
    elif mode == "missing_date":
        url, _ = _seed_object(tid, group, _PHASH_A)
        row = _insert_published(tid, url, date=None)
    elif mode == "no_object":
        url, _ = _seed_object(tid, group, _PHASH_A)
        row = _insert_published(tid, None)
    elif mode == "no_receipt":
        url, _ = _seed_object(tid, group, phash=None)
        row = _insert_published(tid, url)
    elif mode == "ambiguous_receipt":
        url, fp = _seed_object(tid, group, _PHASH_A)
        _sql("insert into public.visual_scene_owner_phash_receipt "
             "(receipt_id,tenant_id,group_key,object_role,exact_url,"
             "fingerprint,phash,byte_length,algorithm) values "
             f"(gen_random_uuid(),'{tid}','{group}','display','{url}',"
             f"'{fp}','{_PHASH_A}',1024,'echo-dct-phash64-v1')")
        row = _insert_published(tid, url)
    else:  # bad_lineage: source URL aliased into the group but unattested
        url, _ = _seed_object(tid, group, _PHASH_A)
        bad_source = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
        _sql("insert into public.visual_group_alias"
             "(gym_id, alias_kind, alias_value, group_key) values "
             f"('{tid}', 'canonical_url', '{bad_source}', '{group}')")
        row = _insert_published(tid, url, source_url=bad_source)
    cov = _coverage()
    assert len(cov) == 1
    assert cov[0]["state"] == "blocked"
    assert cov[0]["blocker_reason"] == reason
    assert cov[0]["calendar_row_id"] == row
    # Fail closed: the backfill writes NOTHING for a blocked row.
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    assert _occupied() == []
    # The blocker remains named after the backfill run.
    assert _coverage()[0]["state"] == "blocked"


def test_mixed_history_backfills_only_evidenced_rows():
    tid, group = _seed_tenant()
    url_a, _ = _seed_object(tid, group, _PHASH_A)
    row_ok = _insert_published(tid, url_a)
    url_b, _ = _seed_object(tid, group, phash=None)  # no receipt: blocker
    row_blocked = _insert_published(tid, url_b, date="2026-09-21")
    assert _one("select public.visual_scene_backfill_occupied()") == "1"
    states = {c["calendar_row_id"]: (c["state"], c["blocker_reason"])
              for c in _coverage()}
    assert states[row_ok] == ("covered", None)
    assert states[row_blocked] == ("blocked", "no_owner_phash_receipt")


def test_backfill_refuses_once_any_tenant_is_armed():
    tid, group = _seed_tenant()
    url, _ = _seed_object(tid, group, _PHASH_A)
    _insert_published(tid, url)
    # Arm directly with the arm-guard trigger bypassed (scratch-only path;
    # the real activation RPC is out of scope for this guard test).
    _sql("set session_replication_role = replica; "
         "insert into public.gym_visual_guard_settings(gym_id, enforce) "
         f"values ('{tid}', true)")
    bad = _run("select public.visual_scene_backfill_occupied()", check=False)
    assert bad.returncode != 0
    assert "0A000" in bad.stderr or "pre-activation only" in bad.stderr
    assert _occupied() == []


def test_backfill_requires_read_committed_isolation():
    bad = _run("set transaction isolation level repeatable read; "
               "select public.visual_scene_backfill_occupied()", check=False)
    assert bad.returncode != 0
    assert "requires READ COMMITTED isolation" in bad.stderr


def test_privilege_posture():
    # Coverage report: service_role-readable only.
    assert _one("select has_function_privilege('service_role',"
                " 'public.visual_scene_history_coverage()', 'execute')") == "t"
    assert _one("select has_function_privilege('anon',"
                " 'public.visual_scene_history_coverage()', 'execute')") == "f"
    assert _one("select has_function_privilege('authenticated',"
                " 'public.visual_scene_history_coverage()', 'execute')") == "f"
    # Backfill: owner-only, revoked from every application role.
    for role in ("anon", "authenticated", "service_role"):
        assert _one(f"select has_function_privilege('{role}',"
                    " 'public.visual_scene_backfill_occupied()', 'execute')") == "f"


def test_mismatched_second_receipt_cannot_contaminate_backfill():
    """One VALID owner receipt (fingerprint/byte_length attested) plus one
    MISMATCHED receipt on the same tenant/group/role/URL: only the validated
    receipt may feed coverage and the backfill write."""
    tid, group = _seed_tenant()
    url, fp = _seed_object(tid, group, _PHASH_A)
    valid_receipt = _one(
        "select receipt_id from public.visual_scene_owner_phash_receipt "
        f"where fingerprint = '{fp}'")
    # The receipt FK requires the attested fingerprint. A mismatched byte
    # length remains insertable but must fail the coverage validation join.
    _sql("insert into public.visual_scene_owner_phash_receipt "
         "(receipt_id,tenant_id,group_key,object_role,exact_url,"
         "fingerprint,phash,byte_length,algorithm) values "
         f"(gen_random_uuid(),'{tid}','{group}','display','{url}',"
         f"'{fp}','{_PHASH_B}',999999,"
         " 'echo-dct-phash64-v1')")
    row = _insert_published(tid, url)
    cov = _coverage()
    assert len(cov) == 1
    assert cov[0]["state"] == "backfillable"
    assert cov[0]["phash"] == _PHASH_A
    assert _one("select public.visual_scene_backfill_occupied()") == "1"
    occ = _occupied()
    assert len(occ) == 1
    assert occ[0]["phash"].strip() == _PHASH_A
    assert occ[0]["fingerprint"] == fp
    assert occ[0]["calendar_row_id"] == row
    assert occ[0]["evidence"]["owner_phash_receipt"] == valid_receipt


def test_backfill_revalidates_under_row_lock_when_row_changes_midflight():
    """TOCTOU regression: a candidate classified 'backfillable' whose calendar
    row changes before the write must be re-evaluated under the row lock and
    skipped — no stale-classification insert."""
    tid, group = _seed_tenant()
    url, _ = _seed_object(tid, group, _PHASH_A)
    row = _insert_published(tid, url)
    psql = [str(BIN / "psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-p", PORT, "-h", _SOCK, "-d", "echo_scene_history_test"]
    holder = subprocess.Popen(psql, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
    try:
        holder.stdin.write(
            "begin;\n"
            f"select id from public.content_calendar where id='{row}' "
            "for update;\n")
        holder.stdin.flush()
        time.sleep(0.5)  # let the row lock land before the backfill starts
        backfill = subprocess.Popen(
            psql + ["-c", "select public.visual_scene_backfill_occupied()"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # The backfill takes the advisory lock first, then blocks on the row
        # lock held above — i.e. its candidate classification is now stale.
        deadline = time.time() + 30
        while time.time() < deadline:
            if _one("select count(*) from pg_locks "
                    "where locktype='advisory' and granted") not in ("", "0"):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("backfill never reached the advisory lock")
        # Change the row underneath the in-flight run: NULL date is a named
        # blocker, so the locked re-classification must refuse the write.
        holder.stdin.write(
            f"update public.content_calendar set post_date=null "
            f"where id='{row}';\ncommit;\n")
        holder.stdin.flush()
        out, err = backfill.communicate(timeout=60)
        assert backfill.returncode == 0, err
        assert out.strip().splitlines()[-1] == "0"
        assert _occupied() == []
        cov = _coverage()
        assert len(cov) == 1
        assert cov[0]["state"] == "blocked"
        assert cov[0]["blocker_reason"] == "missing_date"
    finally:
        if holder.poll() is None:
            holder.stdin.write("rollback;\n\\q\n")
            holder.stdin.flush()
            holder.communicate(timeout=30)
