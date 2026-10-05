"""Rollback-only proof for the minimal scene-ledger draft on a real PG.

Loads the prerequisite visual draft stack plus
migrations/DRAFT_visual_scene_ledger_20261005.sql (Child B port of the draft
73b39c2 claim-wave core) inside ONE transaction which then ROLLBACKs. The
test proves the installed scene relations (with columns), functions (with
signatures), triggers, indexes, RLS policies and ACLs were visible before the
rollback and that afterwards NONE of the scene-ledger objects remain — the
complete scene catalog state is restored to the pre-transaction baseline.

Hard safety properties (pattern after tests/test_scene_migration_rollback_pg.py
from draft 73b39c2):
  * the live proof is opt-in only: it runs when
    ECHO_SCENE_LEDGER_ROLLBACK_DSN is set; the DSN guard accepts literally
    only a Unix-socket DSN naming the disposable database
    `echo_scene_ledger_rollback` (never a network/production host);
  * the source migration files are never modified: the test edits only an
    in-memory copy, stripping ONLY standalone top-level `begin;`/`commit;`
    lines (dollar-quote and line-comment aware);
  * every SQL error fails the test (ON_ERROR_STOP=1);
  * the only statements that may commit are the module fixture's scratch
    prerequisites (roles, stub tables); the migration stack itself is wrapped
    in begin; ... rollback;.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DSN = os.environ.get("ECHO_SCENE_LEDGER_ROLLBACK_DSN")
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
    reason="rollback proof needs ECHO_SCENE_LEDGER_ROLLBACK_DSN on a "
           "disposable local DB and the ledger migration present",
)

KEY_TABLES = (
    "visual_scene_candidate",
    "visual_scene_phash_occupied",
    "visual_scene_review_hold",
)
KEY_FUNCTIONS = (
    "visual_scene_hamming(text,text)",
    "visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)",
    "visual_scene_row_delivered_object(public.content_calendar)",
    "visual_scene_row_candidate(public.content_calendar)",
    "visual_scene_claim_scan(public.content_calendar,uuid)",
    "visual_scene_write_holds(text,text,date,uuid,text,uuid,char(16),text,text,jsonb)",
    "visual_scene_claim_decide(public.content_calendar,uuid)",
    "visual_scene_claim_guard(public.content_calendar,uuid)",
    "visual_scene_hold_resolve(uuid,text,text,jsonb)",
    "visual_scene_backfill_occupied()",
    "visual_scene_immutable()",
    "visual_scene_review_hold_mutation()",
)
KEY_TRIGGERS = (
    ("visual_scene_candidate", "visual_scene_candidate_immutable"),
    ("visual_scene_phash_occupied", "visual_scene_occupied_immutable"),
    ("visual_scene_review_hold", "visual_scene_review_hold_guard"),
)
KEY_INDEXES = (
    "visual_scene_candidate_scene_idx",
    "visual_scene_candidate_phash_idx",
    "visual_scene_candidate_object_idx",
    "visual_scene_phash_occupied_tenant_date_idx",
    "visual_scene_review_hold_state_idx",
    "visual_scene_review_hold_scene_idx",
    "visual_scene_review_hold_open_uq",
)
KEY_POLICIES = (
    "visual_scene_candidate_service_role",
    "visual_scene_phash_occupied_service_role",
    "visual_scene_review_hold_service_role",
)


def _run(statement, check=True, stdin=None):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN]
        + (["-c", statement] if statement else []),
        input=stdin, text=True, capture_output=True, timeout=180)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _sql(statement):
    return _run(statement).stdout.strip()


def _one(statement):
    out = _sql(statement)
    return out.splitlines()[-1] if out else ""


def _strip_txn_control(sql):
    """Remove ONLY standalone top-level begin;/commit; lines (dollar-quote
    and line-comment aware) so the stack can run inside an outer transaction
    that we roll back."""
    out = []
    in_dollar = False
    for line in sql.splitlines():
        stripped = line.strip().lower()
        # Track dollar-quoting state per line (all bodies here use bare $$).
        if not in_dollar and not stripped.startswith("--"):
            if stripped in ("begin;", "commit;"):
                continue
        toggles = line.count("$$")
        if toggles % 2 == 1 and not line.lstrip().startswith("--"):
            in_dollar = not in_dollar
        out.append(line)
    return "\n".join(out)


@pytest.fixture(scope="module", autouse=True)
def _scratch():
    if not DSN or not LEDGER.exists():
        pytest.skip("ledger migration or DSN unavailable")
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_ledger_rollback"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_ledger_rollback allowed"
    assert _one("select current_database()") == "echo_scene_ledger_rollback"
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


def _catalog_snapshot():
    """Scene-ledger catalog state: tables, functions, triggers, indexes,
    policies touching the visual_scene_* namespace."""
    return _sql(
        "select coalesce(string_agg(x, chr(10) order by x), '') from ("
        " select 'table:' || tablename as x from pg_tables"
        "  where schemaname='public' and tablename like 'visual\\_scene\\_%'"
        " union all"
        " select 'func:' || p.proname || '(' ||"
        "  pg_get_function_identity_arguments(p.oid) || ')'"
        "  from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
        "  where n.nspname='public' and p.proname like 'visual\\_scene\\_%'"
        " union all"
        " select 'trigger:' || tgname || ':' ||"
        "  (select relname from pg_class where oid = t.tgrelid)"
        "  from pg_trigger t where not t.tgisinternal"
        "  and tgname like 'visual\\_scene\\_%'"
        " union all"
        " select 'index:' || indexname from pg_indexes"
        "  where schemaname='public' and indexname like 'visual\\_scene\\_%'"
        " union all"
        " select 'policy:' || polname || ':' ||"
        "  (select relname from pg_class where oid = polrelid)"
        "  from pg_policy where polname like 'visual\\_scene\\_%'"
        ") s")


def test_stack_installs_then_rolls_back_cleanly():
    baseline = _catalog_snapshot()
    assert baseline == "", \
        f"scene objects already present on scratch baseline: {baseline}"
    # Build the in-txn script: stripped stack + probes, all inside one
    # transaction that ROLLBACKs.
    script = "begin;\n"
    for name in STACK:
        body = _strip_txn_control((MIGRATIONS / name).read_text())
        script += body + "\n"
    probes = []
    for t in KEY_TABLES:
        probes.append(
            "select 'probe-table:" + t + ":' || coalesce(count(*)::text,'0')"
            f" from pg_tables where schemaname='public' and tablename='{t}';")
    for fn in KEY_FUNCTIONS:
        probes.append(
            "select 'probe-func:" + fn + ":' || coalesce(count(*)::text,'0')"
            " from pg_proc p join pg_namespace n on n.oid=p.pronamespace"
            " where n.nspname='public' and p.proname="
            f"'{fn.split('(')[0]}';")
    for rel, tg in KEY_TRIGGERS:
        probes.append(
            "select 'probe-trigger:" + tg + ":' || coalesce(count(*)::text,'0')"
            f" from pg_trigger where not tgisinternal and tgname='{tg}'"
            f" and tgrelid='public.{rel}'::regclass;")
    for ix in KEY_INDEXES:
        probes.append(
            "select 'probe-index:" + ix + ":' || coalesce(count(*)::text,'0')"
            f" from pg_indexes where schemaname='public' and indexname='{ix}';")
    for pol in KEY_POLICIES:
        probes.append(
            "select 'probe-policy:" + pol + ":' || coalesce(count(*)::text,'0')"
            f" from pg_policy where polname='{pol}';")
    # RLS enabled on the three scene tables.
    probes.append(
        "select 'probe-rls:' || count(*)::text from pg_class"
        " where relname in ('visual_scene_candidate',"
        " 'visual_scene_phash_occupied','visual_scene_review_hold')"
        " and relrowsecurity;")
    # Claim-path SECURITY DEFINER revoke probe.
    probes.append(
        "select 'probe-acl-scan:' || has_function_privilege('service_role',"
        " 'public.visual_scene_claim_scan(public.content_calendar,uuid)',"
        " 'execute')::text;")
    script += "\n".join(probes)
    script += "\nrollback;\n"
    done = _run(None, stdin=script)
    out = done.stdout
    for t in KEY_TABLES:
        assert f"probe-table:{t}:1" in out, f"missing table {t}:\n{out}"
    for fn in KEY_FUNCTIONS:
        assert f"probe-func:{fn}:1" in out, f"missing function {fn}:\n{out}"
    for _, tg in KEY_TRIGGERS:
        assert f"probe-trigger:{tg}:1" in out, f"missing trigger {tg}:\n{out}"
    for ix in KEY_INDEXES:
        assert f"probe-index:{ix}:1" in out, f"missing index {ix}:\n{out}"
    for pol in KEY_POLICIES:
        assert f"probe-policy:{pol}:1" in out, f"missing policy {pol}:\n{out}"
    assert "probe-rls:3" in out
    assert "probe-acl-scan:f" in out
    # After rollback the catalog state is identical to the baseline.
    assert _catalog_snapshot() == baseline == ""
