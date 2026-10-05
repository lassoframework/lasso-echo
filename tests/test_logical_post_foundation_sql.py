"""Static release contracts + optional live-PostgreSQL checks for
migrations/logical_post_id_20261004.sql.

Static checks always run. Live checks need an isolated disposable Unix-socket
database via LOGICAL_POST_TEST_DSN (same posture as the visual-group bundle);
they are skipped otherwise and never connect to a network or production host.

Security-reviewed contract (2026-10-04 revision): the earlier backfill design
(audited-assignment RPC + app.logical_post_backfill GUC) was rejected because
the GUC is user-settable and the security-definer RPC let service_role forge
audit rows. The migration now contains ONLY a nullable column, an index, and
an UPDATE trigger that refuses ANY change to logical_post_id. Historical NULL
rows stay NULL; backfill is a separate provenance-reviewed operator migration.
"""
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "migrations" / "logical_post_id_20261004.sql").read_text()
LOW = SQL.lower()

DSN = os.environ.get("LOGICAL_POST_TEST_DSN")
PSQL = shutil.which("psql")


# ---- static release contracts -----------------------------------------------------

def test_additive_nullable_column_and_gym_scoped_index():
    assert "add column if not exists logical_post_id uuid" in LOW
    assert ("create index if not exists content_calendar_gym_logical_post_id_idx\n"
            "  on public.content_calendar (gym_id, logical_post_id)") in LOW
    assert "not null" not in re.search(
        r"add column if not exists logical_post_id uuid;", LOW).group(0)


def test_no_backfill_and_no_bypass_mechanism():
    # No UPDATE of the column anywhere in the migration: historical NULL rows
    # stay NULL and there is no backfill path at all.
    assert "update public.content_calendar" not in LOW
    assert "insert into public.content_calendar" not in LOW
    # The rejected mechanism must be gone entirely: no RPC, no audit table,
    # no user-settable custom GUC, no service_role grants to forge around.
    # (The migration header documents the rejection in comments, so check code
    # only; a name in prose is not a live mechanism.)
    code = re.sub(r"--[^\n]*", "", LOW)  # strip comment lines
    for token in ("assign_logical_post_id", "logical_post_id_backfill_audit",
                  "set_config", "current_setting", "app.logical_post_backfill",
                  "security definer"):
        assert token not in code
    assert not re.search(r"\bgrant\b", code)
    assert not re.search(r"\brevoke\b", code)


def test_immutability_guard_rejects_any_change_in_either_direction():
    assert "new.logical_post_id is distinct from old.logical_post_id" in LOW
    assert "logical_post_id cannot be set or changed by update" in LOW
    assert "before update on public.content_calendar" in LOW
    # Single-condition guard: no NULL exemption that would allow NULL->UUID.
    guard = LOW.split("create or replace function public.content_calendar_logical_post_guard", 1
                      )[1].split("$$", 1)[0]
    assert "old.logical_post_id is not null" not in guard
    assert "old.logical_post_id is null" not in guard


def test_guard_is_update_only_so_new_sibling_inserts_may_carry_id():
    trigger = LOW.split("create trigger content_calendar_logical_post_guard", 1
                        )[1].split(";", 1)[0]
    assert "before update on" in trigger
    assert "insert" not in trigger


# ---- optional live-PostgreSQL behavior --------------------------------------------

def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql(statement, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", statement], text=True, capture_output=True, timeout=30)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def val(statement):
    return sql(statement).stdout.strip()


live = pytest.mark.skipif(not DSN, reason="isolated local LOGICAL_POST_TEST_DSN unset")


@pytest.fixture(scope="module")
def database():
    if not DSN:
        pytest.skip("local DSN unset")
    assert PSQL
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_logical_post_test(?: user=[A-Za-z0-9_-]+)?",
        DSN), "only named disposable Unix-socket DB allowed"
    assert val("select current_database()") == "echo_logical_post_test"
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; "
        "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; "
        "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql("drop schema public cascade; create schema public; "
        "grant usage on schema public to anon, authenticated, service_role;")
    sql("""create table public.content_calendar(
        id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
        status text default 'pending', account text, format text,
        variant_status text default 'active', image_url text,
        created_at timestamptz default now());""")
    sql(SQL)
    yield


def _insert(gym="g1", logical=None):
    stmt = (f"insert into public.content_calendar(gym_id, post_date, logical_post_id) "
            f"values ({q(gym)}, '2026-10-06', "
            f"{q(logical) + '::uuid' if logical else 'null'}) returning id")
    return val(stmt)


@live
def test_live_new_insert_may_carry_id_and_updates_preserve_it(database):
    group = str(uuid.uuid4())
    row = _insert("g1", group)
    # ordinary updates (e.g. variant promotion touching variant_status) keep the id
    sql(f"update public.content_calendar set variant_status='candidate' where id={q(row)}")
    assert val(f"select logical_post_id from public.content_calendar where id={q(row)}") == group
    # re-asserting the SAME value is permitted (not a change)
    sql(f"update public.content_calendar set logical_post_id={q(group)}::uuid "
        f"where id={q(row)}")
    # changing the id is refused
    out = sql(f"update public.content_calendar set logical_post_id={q(str(uuid.uuid4()))} "
              f"where id={q(row)}", check=False)
    assert out.returncode != 0 and "cannot be set or changed" in out.stderr
    # clearing the id is refused
    out = sql(f"update public.content_calendar set logical_post_id=null where id={q(row)}",
              check=False)
    assert out.returncode != 0 and "cannot be set or changed" in out.stderr
    assert val(f"select logical_post_id from public.content_calendar where id={q(row)}") == group


@live
def test_live_any_direct_assignment_to_historical_null_row_refused(database):
    row = _insert("g1")  # historical-style row, logical_post_id NULL
    assert val(f"select logical_post_id is null from public.content_calendar "
               f"where id={q(row)}") == "t"
    out = sql(f"update public.content_calendar set logical_post_id={q(str(uuid.uuid4()))} "
              f"where id={q(row)}", check=False)
    assert out.returncode != 0 and "cannot be set or changed" in out.stderr
    # still NULL; nothing (function or GUC) exists to assign it
    assert val(f"select logical_post_id is null from public.content_calendar "
               f"where id={q(row)}") == "t"
    # no bypass artifacts exist in the catalog
    assert val("select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
               "where n.nspname='public' and p.proname='assign_logical_post_id'") == "0"
    assert val("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
               "where n.nspname='public' and c.relname='logical_post_id_backfill_audit'") == "0"


@live
def test_live_cross_gym_update_and_no_granted_bypass(database):
    group = str(uuid.uuid4())
    row = _insert("g1", group)
    other = _insert("g2", str(uuid.uuid4()))
    # swapping ids across rows/gyms is refused like any other change
    out = sql(f"update public.content_calendar set logical_post_id="
              f"(select logical_post_id from public.content_calendar where id={q(other)}) "
              f"where id={q(row)}", check=False)
    assert out.returncode != 0 and "cannot be set or changed" in out.stderr
    # guard function itself is ordinary (no security definer) and trigger exists
    assert val("select p.prosecdef from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
               "where n.nspname='public' and p.proname='content_calendar_logical_post_guard'"
               ) == "f"
    assert val("select count(*) from pg_trigger where tgname="
               "'content_calendar_logical_post_guard' and tgisinternal=false") == "1"
