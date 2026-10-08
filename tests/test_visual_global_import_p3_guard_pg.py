"""PG17-targeted checks for the import-history SRE NOWAIT guard (P3).

Covers the nonblocking SHARE ROW EXCLUSIVE barrier repair on
migrations/DRAFT_visual_global_history_20261002.sql,
public.visual_global_import_history():

(a) Static SQL checks (always run, no database): the calendar barrier inside
    the import function is acquired with NOWAIT, the freshness pre-check
    (55P03) still precedes the barrier, the auxiliary multi-table NOWAIT lock
    still follows the barrier, and every business refusal (ledger owner,
    original date, scene byte evidence, calendar coverage, post-import
    coverage) is preserved verbatim.

(b) Live disposable-PG checks (skipped without VISUAL_GROUP_TEST_DSN, same
    convention as tests/test_visual_global_history_pg.py): a concurrent
    ROW EXCLUSIVE calendar owner (the G/C writer position) makes the import
    fail with SQLSTATE 55P03 instead of deadlocking; a prior advisory lock in
    the caller's own transaction still raises the 55P03 freshness refusal;
    and a clean import still succeeds with unchanged business results.

Requires psql and VISUAL_GROUP_TEST_DSN (libpq keyword DSN, absolute Unix
socket host, dbname=echo_visual_ledger_test) for the live checks; never point
it at a network or production database.
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


DSN = os.environ.get("VISUAL_GROUP_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
HISTORY_SQL = (MIGRATIONS / "DRAFT_visual_global_history_20261002.sql").read_text()

D1 = "2026-09-01"


def _import_body():
    match = re.search(
        r"create or replace function public\.visual_global_import_history\(\)"
        r".*?\n\$\$;",
        HISTORY_SQL, re.IGNORECASE | re.DOTALL)
    assert match, "visual_global_import_history definition not found"
    return match.group(0)


# --- (a) static checks: NOWAIT order and unchanged business refusals --------

def test_static_barrier_is_nowait_and_ordered():
    body = _import_body().lower()
    # READ COMMITTED freshness gate first, then the pg_locks freshness
    # pre-check (55P03), then the barrier, then the auxiliary NOWAIT lock.
    i_isolation = body.index("read committed")
    i_freshness = body.index("requires calendar barrier before writer locks")
    i_barrier = body.index(
        "lock table public.content_calendar in share row exclusive mode nowait")
    i_aux = body.index("in share row exclusive mode nowait", i_barrier + 1)
    assert i_isolation < i_freshness < i_barrier < i_aux
    # The freshness pre-check keeps its 55P03 refusal.
    assert re.search(r"requires calendar barrier before writer locks'?"
                     r"\s*using errcode='55p03'", body)
    # No blocking (non-NOWAIT) SHARE ROW EXCLUSIVE acquisition of the calendar
    # table remains anywhere in the function.
    for lock in re.findall(r"lock table[^;]*;", body):
        if "content_calendar" in lock:
            assert "nowait" in lock, lock
        else:
            assert "nowait" in lock, lock


def test_static_business_refusals_unchanged():
    body = _import_body()
    for fragment in (
        "global history import requires READ COMMITTED",
        "global history import refused: published event lacks ledger owner",
        "global history import refused: staged history has no original date",
        "global history import refused: occupied scene has incomplete byte evidence",
        "global history import refused: calendar coverage incomplete",
        "global history import refused: published history is unresolved",
        "global history import refused: published history is unattested",
        "global history import refused: post-import coverage incomplete",
    ):
        assert fragment in body, fragment
    # Keyed full-set import still runs before null-key historical attribution.
    assert body.index("visual_global_claim_fingerprint_set") < body.index(
        "visual_global_claim_historical_row")
    assert body.index("visual_global_coverage()") < body.index(
        "visual_global_claim_fingerprint_set")
    # Return shape unchanged.
    assert "imported_local_groups" in body
    assert "imported_null_key_rows" in body
    assert "global_fingerprints" in body


# --- (b/c) live disposable-PG checks ----------------------------------------

live = pytest.mark.skipif(not DSN, reason="isolated local VISUAL_GROUP_TEST_DSN unset")


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql(statement):
    done = subprocess.run([PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-d", DSN, "-c", statement], text=True,
                          capture_output=True, timeout=30)
    if done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def sql_error_verbose(statement):
    done = subprocess.run([PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-v", "VERBOSITY=verbose",
                           "-d", DSN, "-c", statement], text=True,
                          capture_output=True, timeout=30)
    assert done.returncode, f"expected failure, got: {done.stdout!r}"
    return done.stderr


def wait_for_calendar_writer(app_name, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        state = sql("select coalesce((select state||':'||coalesce(wait_event,'') "
                    "from pg_stat_activity "
                    f"where application_name={q(app_name)}),'absent')")
        lock = sql("select coalesce((select granted::text from pg_locks l "
                   "join pg_stat_activity a using(pid) "
                   "where a.application_name=" + q(app_name) + " "
                   "and l.locktype='relation' "
                   "and l.relation='public.content_calendar'::regclass "
                   "and l.mode='RowExclusiveLock' limit 1),'absent')")
        if state.endswith(":PgSleep") and lock == "true":
            return
        assert time.monotonic() < deadline, (
            f"calendar holder did not acquire granted RowExclusiveLock and sleep "
            f"(state={state}, lock={lock})")
        time.sleep(.02)


@pytest.fixture
def database():
    if not DSN:
        pytest.skip("local DSN unset")
    assert PSQL
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_visual_ledger_test(?: user=[A-Za-z0-9_-]+)?",
        DSN), "only named disposable Unix-socket DB allowed"
    assert sql("select current_database()") == "echo_visual_ledger_test"
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; "
        "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; "
        "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql("drop schema public cascade; create schema public; grant usage on schema public to anon,authenticated,service_role;")
    sql("""create table public.content_calendar(
        id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
        status text default 'pending', account text, format text,
        variant_status text default 'active', image_url text, thumbnail_url text,
        source_media_url text,
        source_media_asset_id text, drive_file_id text, byte_hash text, r2_key text,
        media_not_ready_reason text, published_at timestamptz, late_post_id text,
        publish_reservation_day date, publish_claim_token uuid,
        created_at timestamptz default now());
        create table public.media_asset(id text primary key,gym_id text,content_hash text);
        grant select,insert,update,delete on public.content_calendar to service_role;""")
    for name in ("DRAFT_visual_group_schema_20261002.sql",
                 "DRAFT_visual_group_claim_trigger_20261002.sql",
                 "DRAFT_visual_group_backfill_20261002.sql",
                 "DRAFT_visual_global_history_20261002.sql"):
        sql((MIGRATIONS / name).read_text())


def attest_same_object(label):
    key, tid = "g_" + uuid.uuid4().hex, str(uuid.uuid4())
    sql(f"select public.visual_group_tenant_register({q(key)},{q(tid)}::uuid)")
    url = f"https://test/{label}-{uuid.uuid4().hex}.jpg"
    data = f"exact bytes for {label} {uuid.uuid4().hex}".encode()
    fingerprint = "md5:" + hashlib.md5(data).hexdigest()
    group = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',{q(url)})")
    receipt = str(uuid.uuid4())
    sql("insert into public.visual_global_object_read_receipt "
        "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by) "
        f"values({q(receipt)}::uuid,{q(tid)},{q(url)},{q(fingerprint)},{len(data)},"
        "'verified_object_read','object-read-test','fixture_owner')")
    sql("set role service_role; select public.visual_global_prepare_source_rendition("
        f"{q(tid)},{q(group)},{q(receipt)}::uuid,{q(receipt)}::uuid,null,'test_actor')")
    assert sql(f"select public.visual_global_scene_complete({q(tid)},{q(group)})") == "t"
    return {"key": key, "tid": tid, "group": group, "url": url, "fingerprint": fingerprint}


@live
def test_import_nowait_refuses_concurrent_calendar_writer_55p03(database):
    scene = attest_same_object("p3")
    sql("insert into public.content_calendar"
        "(gym_id,post_date,status,account,image_url,source_media_url,published_at) values("
        f"{q(scene['key'])},{q(D1)},'published','ig',"
        f"{q(scene['url'])},{q(scene['url'])},now())")
    # Hold the writer position: ROW EXCLUSIVE on content_calendar (what every
    # G/C-guarded calendar INSERT/UPDATE takes) conflicts with the import
    # barrier's SHARE ROW EXCLUSIVE. A blocking barrier would queue here and
    # deadlock against the writer's own G/C path; NOWAIT must raise 55P03.
    app_name = "visual_import_holder_" + uuid.uuid4().hex
    holder = subprocess.Popen([PSQL, "-X", "-q", "-d", DSN], text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    try:
        holder.stdin.write(f"set application_name={q(app_name)}; begin; "
                           "lock table public.content_calendar "
                           "in row exclusive mode; select pg_sleep(20); rollback;\n")
        holder.stdin.flush()
        wait_for_calendar_writer(app_name)
        err = sql_error_verbose("select public.visual_global_import_history()")
        assert "55P03" in err, err
        assert "content_calendar" in err
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.wait(timeout=15)
    # Nothing partial persisted from the refused import.
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert sql("select count(*) from public.visual_global_usage_member") == "0"


@live
def test_import_freshness_precheck_still_55p03(database):
    err = sql_error_verbose(
        "begin; select pg_advisory_xact_lock(hashtextextended('p3_freshness',0)); "
        "select public.visual_global_import_history(); rollback;")
    assert "55P03" in err, err
    assert "requires calendar barrier before writer locks" in err


@live
def test_import_business_behavior_unchanged(database):
    scene = attest_same_object("p3ok")
    sql("insert into public.content_calendar"
        "(gym_id,post_date,status,account,image_url,source_media_url,published_at) values("
        f"{q(scene['key'])},{q(D1)},'published','ig',"
        f"{q(scene['url'])},{q(scene['url'])},now())")
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_null_key_rows"] == 1
    assert result["global_fingerprints"] == 1
    assert sql("select state from public.visual_global_usage "
               f"where fingerprint={q(scene['fingerprint'])}") == "published"
    assert sql("select coalesce(string_agg(issue,',' order by issue),'') "
               "from public.visual_global_coverage()") == "ready"
    # Re-import reports the one already-attributed historical row again;
    # ledger, member, and attribution state remain unchanged.
    before = sql("select (select count(*) from public.visual_global_usage)||':'||"
                 "(select count(*) from public.visual_global_usage_member)||':'||"
                 "(select count(*) from public.visual_global_published_attribution)")
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_null_key_rows"] == 1
    assert result["global_fingerprints"] == 1
    after = sql("select (select count(*) from public.visual_global_usage)||':'||"
                "(select count(*) from public.visual_global_usage_member)||':'||"
                "(select count(*) from public.visual_global_published_attribution)")
    assert after == before
