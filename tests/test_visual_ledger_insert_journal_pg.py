"""Visual ledger INSERT observation journal: static source contracts + OPTIONAL
real-PostgreSQL scenario checks against a disposable scratch database.

Static tests always run. PG scenarios run only when ALL of the following hold:
  * `migrations/DRAFT_visual_ledger_insert_journal_20261004.sql` exists,
  * VISUAL_LEDGER_JOURNAL_TEST_DSN names a disposable Unix-socket database
    literally named `echo_insert_journal_test` (same house rule as
    tests/test_scene_ledger_coverage_gate_pg.py — never a network/production
    host),
  * local psql is on PATH.

The module fixture applies the FULL draft stack (schema, claim trigger,
global history / importer, backfill, activation, claim wave, history backfill,
ledger coverage gate) committed, so importer/coverage/activation/claim
functions are present and exercised — not a base-only subset.

Scope honesty: the journal is an INSERT OBSERVATION log only. No test here
asserts original-use clearance, provider-byte verification, or any change to
coverage evaluators, activation, import interpretation or writers; one
scenario explicitly verifies those preexisting functions are byte-identical
after installation.
"""

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("VISUAL_LEDGER_JOURNAL_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
JOURNAL = MIGRATIONS / "DRAFT_visual_ledger_insert_journal_20261004.sql"
STACK = [
    # Order is dependency-resolved: the claim trigger defines
    # public.visual_group_row_active(content_calendar) (DRAFT_..._claim_trigger
    # line ~189), which the global history SQL-body functions reference at
    # creation time, so the claim trigger MUST be applied first.
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_history_backfill_20261004.sql",
    "DRAFT_visual_scene_ledger_coverage_gate_20261004.sql",
]
JOURNAL_FUNCTIONS = (
    "visual_group_ledger_assign_insert_event",
    "visual_group_ledger_record_insert",
    "visual_group_ledger_event_id_immutable",
    "visual_group_insert_journal_immutable",
)

PG_READY = bool(DSN) and PSQL is not None and JOURNAL.exists()


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
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    assert set(parts) <= {"host", "port", "dbname", "user"}, \
        "only host/port/dbname/user DSN keys allowed"
    assert re.fullmatch(r"/[A-Za-z0-9_./-]+", parts["host"]), \
        "only a Unix-socket host directory allowed"
    assert parts["dbname"] == "echo_insert_journal_test" and parts["port"].isdigit()
    assert _one("select current_database()") == "echo_insert_journal_test"


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


def _apply(script):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr


def _apply_stack():
    _apply("".join((MIGRATIONS / name).read_text() + "\n" for name in STACK))


def _journal_body():
    """The draft file minus ONLY its own outer begin;/commit; wrapper."""
    lines = JOURNAL.read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().lower() == "begin;"]
    ends = [i for i, ln in enumerate(lines) if ln.strip().lower() == "commit;"]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]
    return "\n".join(lines[starts[0] + 1:ends[0]])


def _catalog_snapshot():
    """Complete relevant public-schema catalog, one line per object part:
    functions (identity args, kind, security, volatility, acl, FULL
    pg_get_functiondef), relations (kind, RLS flag, acl), columns (type,
    nullability, default), constraints (full definition), policies (command,
    roles, qual, check) and non-internal triggers (full definition).
    Compared as a whole — never a single line — so a body, column,
    constraint, policy or grant change anywhere is detected."""
    return _sql(
        "select coalesce(string_agg(x, E'\\n' order by x), '') from ("
        "select 'f:'||n.nspname||'.'||p.proname||'('||"
        " oidvectortypes(p.proargtypes)||')'||'|kind='||p.prokind::text||"
        " '|sec='||p.prosecdef||'|vol='||p.provolatile::text||"
        " '|acl='||coalesce(p.proacl::text,'')||'|def='||"
        " replace(pg_get_functiondef(p.oid), E'\\n', E'\\\\n') "
        "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
        " where n.nspname='public' "
        "union all "
        "select 'r:'||c.relname||'|'||c.relkind::text||'|rls='||"
        " c.relrowsecurity||'|acl='||coalesce(c.relacl::text,'') "
        "from pg_class c join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' and c.relkind in ('r','v','m','S') "
        "union all "
        "select 'c:'||a.attrelid::regclass||'.'||a.attname||'|'||"
        " a.atttypid::regtype||'|nn='||a.attnotnull||'|def='||"
        " coalesce(pg_get_expr(d.adbin, d.adrelid), '') "
        "from pg_attribute a "
        " left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum "
        " join pg_class c on c.oid=a.attrelid "
        " join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' and a.attnum>0 and not a.attisdropped "
        "union all "
        "select 'k:'||con.conrelid::regclass||'.'||con.conname||'|'||"
        " pg_get_constraintdef(con.oid) "
        "from pg_constraint con join pg_class c on c.oid=con.conrelid "
        " join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' "
        "union all "
        "select 'y:'||pol.polrelid::regclass||'.'||pol.polname||'|cmd='||"
        " pol.polcmd::text||'|roles='||pol.polroles::text||'|qual='||"
        " coalesce(pg_get_expr(pol.polqual, pol.polrelid), '')||'|check='||"
        " coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid), '') "
        "from pg_policy pol join pg_class c on c.oid=pol.polrelid "
        " join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' "
        "union all "
        "select 't:'||t.tgrelid::regclass||'.'||t.tgname||'|'||"
        " pg_get_triggerdef(t.oid) "
        "from pg_trigger t join pg_class c on c.oid=t.tgrelid "
        " join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' and not t.tgisinternal) s(x)")


def _function_defs():
    """Complete structured function definitions keyed by
    schema.name(identity-args) — overloads never collapse, and the FULL
    pg_get_functiondef (attributes + body, newlines escaped) is compared,
    never a first line or a name alone."""
    out = _sql(
        "select n.nspname||'.'||p.proname||'('||oidvectortypes(p.proargtypes)"
        "||')'||E'\\t'||replace(pg_get_functiondef(p.oid), E'\\n', E'\\\\n') "
        "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
        "where n.nspname='public' order by 1")
    defs = {}
    for line in out.splitlines():
        key, sep, val = line.partition("\t")
        assert sep, f"malformed function row: {line[:80]}"
        defs[key] = val
    return defs


# ---------------------------------------------------------------------------
# Static source contracts (always run; never touch a database)
# ---------------------------------------------------------------------------

def test_static_draft_exists_and_single_transaction():
    text = JOURNAL.read_text()
    lines = [ln.strip().lower() for ln in text.splitlines()]
    assert lines.count("begin;") == 1 and lines.count("commit;") == 1
    assert lines.index("begin;") < lines.index("commit;")


def test_static_nullable_column_no_backfill():
    text = JOURNAL.read_text().lower()
    assert "add column if not exists observed_insert_event_id uuid" in text
    assert "not null" not in text.split(
        "add column if not exists observed_insert_event_id uuid")[1].split(";")[0]
    # No backfill of any kind for preexisting rows.
    assert not re.search(
        r"update\s+public\.visual_group_usage_ledger\s+set\s+observed_insert_event_id",
        text)


def test_static_server_generated_uuid_caller_cannot_select():
    body = re.search(
        r"function public\.visual_group_ledger_assign_insert_event\(\).*?\$\$;",
        JOURNAL.read_text(), re.S).group(0).lower()
    assert "security definer" in body
    assert "new.observed_insert_event_id := gen_random_uuid()" in body
    # No conditional acceptance of a caller-supplied value.
    assert "coalesce" not in body and "if new.observed_insert_event_id" not in body


def test_static_after_insert_records_raw_snapshot_same_transaction():
    text = JOURNAL.read_text().lower()
    assert "after insert on public.visual_group_usage_ledger" in text
    body = re.search(
        r"function public\.visual_group_ledger_record_insert\(\).*?\$\$;",
        JOURNAL.read_text(), re.S).group(0)
    for needle in ("new.gym_id", "new.group_key", "new.reserved_date",
                   "new.calendar_row_id", "new.channel", "new.state",
                   "to_jsonb(new)"):
        assert needle in body.lower()
    # Trigger writes in the same transaction as the ledger INSERT.
    assert "dblink" not in body.lower() and "pg_background" not in body.lower()


def _code_only(text):
    """SQL with -- line comments stripped (contract checks scan code only)."""
    return "\n".join(ln.split("--")[0] for ln in text.splitlines())


def test_static_journal_has_no_mutable_fk_and_deferred_binding():
    text = _code_only(JOURNAL.read_text().lower())
    journal_block = text.split(
        "create table if not exists public.visual_group_insert_journal")[1]
    journal_block = journal_block.split(");")[0]
    assert "references" not in journal_block, \
        "journal must not FK to ledger or calendar"
    assert "content_calendar" not in journal_block
    assert "deferrable initially deferred" in text
    assert re.search(
        r"foreign key \(observed_insert_event_id\)\s*references "
        r"public\.visual_group_insert_journal \(event_id\)", text)


def test_static_updates_create_no_event_and_event_id_immutable():
    text = JOURNAL.read_text().lower()
    assert "before update on public.visual_group_usage_ledger" in text
    assert "is distinct from old.observed_insert_event_id" in text
    # No UPDATE trigger ever writes the journal.
    upd = re.search(
        r"function public\.visual_group_ledger_event_id_immutable\(\).*?\$\$;",
        JOURNAL.read_text(), re.S).group(0).lower()
    assert "insert into" not in upd


def test_static_journal_caller_lockout():
    text = JOURNAL.read_text().lower()
    assert "enable row level security" in text
    assert re.search(r"revoke all on public\.visual_group_insert_journal\s+from "
                     r"public, anon, authenticated, service_role", text)
    assert "grant select on public.visual_group_insert_journal to service_role" in text
    assert not re.search(r"grant (insert|update|delete|truncate).*"
                         r"visual_group_insert_journal", text)
    assert "before truncate on public.visual_group_insert_journal" in text


def test_static_function_execute_revoked_from_all_callers():
    """All four trigger functions lose the default PUBLIC EXECUTE grant and
    receive no role grant: no caller can invoke or attach them."""
    text = JOURNAL.read_text().lower()
    for fn in JOURNAL_FUNCTIONS:
        assert re.search(
            rf"revoke all on function public\.{fn}\(\)\s+from\s+"
            r"public, anon, authenticated, service_role", text), fn
        assert not re.search(
            rf"grant\s+\w[^;]*on function public\.{fn}\(", text), fn


def test_static_trigger_functions_guard_relation_op_timing_level():
    """Every trigger function verifies its exact source relation, operation,
    timing and level, so a same-shaped TEMP table can never fire it."""
    ledger = ("visual_group_ledger_assign_insert_event",
              "visual_group_ledger_record_insert",
              "visual_group_ledger_event_id_immutable")
    for fn in ledger:
        body = re.search(rf"function public\.{fn}\(\).*?\$\$;",
                         JOURNAL.read_text(), re.S).group(0).lower()
        for needle in ("tg_relid", "tg_op", "tg_when", "tg_level",
                       "'public.visual_group_usage_ledger'::regclass"):
            assert needle in body, (fn, needle)
    body = re.search(
        r"function public\.visual_group_insert_journal_immutable\(\).*?\$\$;",
        JOURNAL.read_text(), re.S).group(0).lower()
    assert "tg_relid" in body and "tg_op" in body
    assert "'public.visual_group_insert_journal'::regclass" in body


def test_static_observation_only_no_clearance_wiring():
    text = _code_only(JOURNAL.read_text().lower())
    # Never claims original use / byte clearance, and touches none of the
    # coverage, activation, import or writer functions.
    for forbidden in ("create or replace function public.visual_group_activation",
                      "visual_scene_ledger_obligations",
                      "visual_global_import_history",
                      "visual_scene_history_backfill",
                      "xmin", "age(xmin"):
        assert forbidden not in text
    # The disclaimer language is present in comments (code scan above
    # stripped them, so check the raw text here).
    assert "observation journal ONLY" in JOURNAL.read_text()


# ---------------------------------------------------------------------------
# Disposable-PostgreSQL scenarios (skipped without the DSN)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scratch_stack():
    if not PG_READY:
        pytest.skip("journal PG checks need VISUAL_LEDGER_JOURNAL_TEST_DSN on "
                    "a disposable local DB and the journal migration present")
    _check_dsn()
    _fresh_scratch_schema()
    _apply_stack()


@pytest.fixture()
def scratch(scratch_stack):
    """Scenario isolation. session_replication_role=replica is the privileged
    scratch-only bypass of the immutability triggers — disposable DB only,
    never production."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, "
         "public.visual_group_usage_ledger, "
         "public.visual_group cascade")
    # The journal table may not exist yet (pre-migration scenarios).
    if _one("select to_regclass('public.visual_group_insert_journal')"
            " is not null") == "t":
        _sql("set session_replication_role = replica; "
             "truncate public.visual_group_insert_journal cascade")


def _seed_group(tid=None):
    tid = tid or "gym_" + uuid.uuid4().hex[:10]
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    return tid, group


def _insert_ledger(tid, group, date="2026-10-05", state="reserved", cal=None,
                   event=None):
    cols = "(gym_id, group_key, reserved_date, calendar_row_id, channel, state"
    vals = (f"'{tid}', '{group}', " + (f"'{date}'" if date else "null") + ", "
            + (f"'{cal}'" if cal else "null") + f", 'ig', '{state}'")
    if event:
        cols += ", observed_insert_event_id"
        vals += f", '{event}'"
    return _one(
        f"insert into public.visual_group_usage_ledger{cols}) values ({vals}) "
        "returning observed_insert_event_id")


def _privileged_delete_ledger(tid):
    """Privileged scratch-only deletion: replica mode bypasses the base
    permanence guard, which rejects every ordinary DELETE."""
    _sql("set session_replication_role = replica; "
         "delete from public.visual_group_usage_ledger "
         f"where gym_id='{tid}'")


def test_rollback_catalog_equality(scratch_stack):
    """Rollback-only install probe leaves the complete public catalog
    (functions, relations, columns, constraints, policies, grants, triggers)
    byte-identical."""
    _fresh_scratch_schema()
    _apply_stack()
    before = _catalog_snapshot()
    _apply("begin;\n" + _journal_body() + "\nrollback;\n")
    assert _catalog_snapshot() == before


def test_preexisting_row_seeded_before_migration_stays_null(scratch):
    """A ledger row committed BEFORE the journal migration is applied keeps
    observed_insert_event_id NULL forever — no backfill, no event."""
    tid, group = _seed_group()
    # Seeded BEFORE the journal migration exists in this schema: the column
    # itself does not exist yet.
    _sql("insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, channel, state) values "
         f"('{tid}', '{group}', '2026-10-01', 'ig', 'reserved')")
    _apply(JOURNAL.read_text() + "\n")
    assert _one("select observed_insert_event_id is null from "
                "public.visual_group_usage_ledger where gym_id="
                f"'{tid}'") == "t"
    assert _one("select count(*) from public.visual_group_insert_journal") == "0"


@pytest.fixture()
def installed(scratch_stack):
    """Committed install of the full draft stack + journal on the scratch DB.
    A previous scenario may already have applied the journal; only apply when
    missing (the draft's policies lack IF NOT EXISTS)."""
    if _one("select to_regclass('public.visual_group_insert_journal') is null") == "t":
        _apply(JOURNAL.read_text() + "\n")
    yield


def test_new_exact_insert_records_raw_event(scratch, installed):
    tid, group = _seed_group()
    cal = str(uuid.uuid4())
    _sql("insert into public.content_calendar(id, gym_id) "
         f"values ('{cal}', '{tid}')")
    event = _insert_ledger(tid, group, "2026-10-05", "reserved", cal)
    assert re.fullmatch(r"[0-9a-f-]{36}", event)
    row = _one("select ledger_gym_id||'|'||ledger_group_key||'|'||reserved_date"
               "||'|'||calendar_row_id||'|'||channel||'|'||state from "
               f"public.visual_group_insert_journal where event_id='{event}'")
    assert row == f"{tid}|{group}|2026-10-05|{cal}|ig|reserved"
    snap = _one("select (insert_snapshot->>'reserved_date')||'|'||"
                "(insert_snapshot->>'state')||'|'||"
                "(insert_snapshot ? 'observed_insert_event_id') from "
                f"public.visual_group_insert_journal where event_id='{event}'")
    assert snap == "2026-10-05|reserved|false"


def test_caller_supplied_uuid_is_discarded(scratch, installed):
    tid, group = _seed_group()
    caller = str(uuid.uuid4())
    event = _insert_ledger(tid, group, event=caller)
    assert event != caller, "caller-selected UUID must be replaced"
    assert _one("select count(*) from public.visual_group_insert_journal "
                f"where event_id='{caller}'") == "0"


def test_update_creates_no_event_and_cannot_change_event_id(scratch, installed):
    tid, group = _seed_group()
    event = _insert_ledger(tid, group)
    _sql("update public.visual_group_usage_ledger set channel='fb' "
         f"where gym_id='{tid}'")
    assert _one("select observed_insert_event_id from "
                "public.visual_group_usage_ledger where gym_id="
                f"'{tid}'") == event
    assert _one("select count(*) from public.visual_group_insert_journal") == "1"
    done = _run("update public.visual_group_usage_ledger set "
                f"observed_insert_event_id=gen_random_uuid() where gym_id='{tid}'",
                check=False)
    assert done.returncode != 0 and "immutable" in done.stderr
    done = _run("update public.visual_group_usage_ledger set "
                f"observed_insert_event_id=null where gym_id='{tid}'",
                check=False)
    assert done.returncode != 0 and "immutable" in done.stderr


def test_same_transaction_update_keeps_original_snapshot(scratch, installed):
    tid, group = _seed_group()
    script = (
        "begin;\n"
        "insert into public.visual_group_usage_ledger"
        "(gym_id, group_key, reserved_date, channel, state) values "
        f"('{tid}', '{group}', '2026-10-05', 'ig', 'reserved');\n"
        "update public.visual_group_usage_ledger set channel='fb' "
        f"where gym_id='{tid}';\n"
        "commit;\n")
    _apply(script)
    assert _one("select channel from public.visual_group_usage_ledger "
                f"where gym_id='{tid}'") == "fb"
    assert _one("select count(*) from public.visual_group_insert_journal") == "1"
    assert _one("select (insert_snapshot->>'channel') from "
                "public.visual_group_insert_journal") == "ig"


def test_failed_claim_rollback_leaves_no_journal(scratch, installed):
    tid, group = _seed_group()
    script = (
        "begin;\n"
        "insert into public.visual_group_usage_ledger"
        "(gym_id, group_key, reserved_date, channel, state) values "
        f"('{tid}', '{group}', '2026-10-05', 'ig', 'reserved');\n"
        "rollback;\n")
    _apply(script)
    assert _one("select count(*) from public.visual_group_usage_ledger") == "0"
    assert _one("select count(*) from public.visual_group_insert_journal") == "0"


def test_failing_claim_through_full_stack_rolls_back_both(scratch, installed):
    """An ACTUAL claim RPC from the full stack fails for an unevidenced group;
    the failed claim's transaction rolls back BOTH the ledger row and its
    journal observation."""
    tid = str(uuid.uuid4())
    tid, group = _seed_group(tid)
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    # The claim genuinely fails: no verified group fingerprint exists.
    done = _run(f"select public.visual_global_claim('{tid}', '{group}', "
                "'2026-10-05', null, 'ig', true)", check=False)
    assert done.returncode != 0 and "fingerprint" in done.stderr
    # Ledger insert + failing claim in one subtransaction: both vanish.
    _sql("do $$ begin "
         "insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, channel, state) values "
         f"('{tid}', '{group}', '2026-10-05', 'ig', 'reserved'); "
         "if (select count(*) from public.visual_group_insert_journal "
         f"where ledger_gym_id='{tid}') <> 1 then "
         "raise exception 'insert journal row missing before claim'; end if; "
         f"perform public.visual_global_claim('{tid}', '{group}', "
         "'2026-10-05', null, 'ig', true); "
         "exception when check_violation then "
         "if sqlerrm not ilike '%fingerprint%' then raise; end if; "
         "end $$")
    assert _one("select count(*) from public.visual_group_usage_ledger "
                f"where gym_id='{tid}'") == "0"
    assert _one("select count(*) from public.visual_group_insert_journal "
                f"where ledger_gym_id='{tid}'") == "0"


def test_set_constraints_all_immediate_validates_deferred_fk(scratch, installed):
    """SET CONSTRAINTS ALL IMMEDIATE forces the deferred ledger->journal FK
    check inside the transaction; the AFTER-trigger journal row written in
    the same statement satisfies it."""
    tid, group = _seed_group()
    script = (
        "begin;\n"
        "insert into public.visual_group_usage_ledger"
        "(gym_id, group_key, reserved_date, channel, state) values "
        f"('{tid}', '{group}', '2026-10-05', 'ig', 'reserved');\n"
        "set constraints all immediate;\n"
        "commit;\n")
    _apply(script)
    event = _one("select observed_insert_event_id from "
                 f"public.visual_group_usage_ledger where gym_id='{tid}'")
    assert re.fullmatch(r"[0-9a-f-]{36}", event)
    assert _one("select count(*) from public.visual_group_insert_journal "
                f"where event_id='{event}'") == "1"


def test_ordinary_ledger_delete_rejected(scratch, installed):
    """The preexisting base permanence guard rejects EVERY ordinary ledger
    DELETE — including rows that have a journal event. Nothing is removed."""
    tid, group = _seed_group()
    event = _insert_ledger(tid, group)
    done = _run("delete from public.visual_group_usage_ledger "
                f"where gym_id='{tid}'", check=False)
    assert done.returncode != 0 and "permanent" in done.stderr
    assert _one("select count(*) from public.visual_group_usage_ledger "
                f"where gym_id='{tid}'") == "1"
    assert _one("select count(*) from public.visual_group_insert_journal "
                f"where event_id='{event}'") == "1"


def test_deletion_retention_ledger_and_calendar(scratch, installed):
    """Privileged scratch-only deletion: the journal observation survives
    both calendar-row deletion and ledger deletion (no FKs to either)."""
    tid, group = _seed_group()
    cal = str(uuid.uuid4())
    _sql("insert into public.content_calendar(id, gym_id) "
         f"values ('{cal}', '{tid}')")
    event = _insert_ledger(tid, group, cal=cal)
    _sql(f"delete from public.content_calendar where id='{cal}'")
    _privileged_delete_ledger(tid)
    row = _one("select calendar_row_id from public.visual_group_insert_journal "
               f"where event_id='{event}'")
    assert row == cal, "journal must survive ledger and calendar deletion"


def test_same_key_reinsertion_creates_new_event(scratch, installed):
    """Privileged scratch-only delete + ordinary reinsert of the same
    (gym_id, group_key) creates a NEW event; both journal rows are kept."""
    tid, group = _seed_group()
    first = _insert_ledger(tid, group)
    _privileged_delete_ledger(tid)
    second = _insert_ledger(tid, group, date="2026-10-06")
    assert first != second
    assert _one("select count(*) from public.visual_group_insert_journal "
                f"where ledger_gym_id='{tid}'") == "2"


def test_raw_alias_values_preserved_verbatim(scratch, installed):
    # gym_id/group_key that LOOK like resolvable aliases must be journaled
    # raw, never canonicalized.
    tid, group = "alias:raw-name-01", "vg alias/raw.02"
    _sql("insert into public.visual_group(gym_id, group_key) values "
         f"('{tid}', '{group}')")
    event = _insert_ledger(tid, group)
    row = _one("select ledger_gym_id||'|'||ledger_group_key from "
               f"public.visual_group_insert_journal where event_id='{event}'")
    assert row == f"{tid}|{group}"


def test_trigger_functions_unforgeable_and_execute_revoked(scratch, installed):
    """P1 lockdown: EXECUTE revoked from PUBLIC and every caller role; a
    same-shaped TEMP table cannot be used to forge a journal row — a
    non-owner cannot even attach the recorder, and even the table owner's
    forged attachment is rejected by the TG_RELID/OP/WHEN/LEVEL guard."""
    for fn in JOURNAL_FUNCTIONS:
        # Real caller roles are checked directly...
        for role in ("anon", "authenticated", "service_role"):
            assert _one(f"select has_function_privilege('{role}', "
                        f"'public.{fn}()', 'EXECUTE')") == "f", (fn, role)
        # ...and the PUBLIC pseudo-role (not a role: has_function_privilege
        # cannot take it) is checked through its ACL grantee 0, expanding
        # acldefault so a never-revoked default PUBLIC EXECUTE grant would
        # also be caught.
        assert _one(
            "select count(*) from pg_proc p cross join lateral "
            "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            f"where p.oid='public.{fn}()'::regprocedure "
            "and a.grantee=0 and a.privilege_type='EXECUTE'") == "0", fn
    forged = ("create temp table forged_ledger("
              "observed_insert_event_id uuid, gym_id text, group_key text,"
              "reserved_date date, calendar_row_id uuid, channel text,"
              "state text); ")
    attach = ("create trigger forged_record after insert on forged_ledger "
              "for each row execute function "
              "public.visual_group_ledger_record_insert(); ")
    # Non-owner: cannot attach at all (no EXECUTE privilege).
    done = _run("set role service_role; " + forged + attach, check=False)
    assert done.returncode != 0
    # Owner: attach succeeds but firing is rejected by the source guard.
    done = _run(forged + attach +
                "insert into forged_ledger values (gen_random_uuid(),"
                "'gym_x', 'vg_x', null, null, 'ig', 'reserved')",
                check=False)
    assert done.returncode != 0 and "AFTER INSERT" in done.stderr
    assert _one("select count(*) from public.visual_group_insert_journal "
                "where ledger_gym_id='gym_x'") == "0"


def test_authorized_writer_path_still_journals(scratch, installed):
    """Positive control: the authorized writer (migration/table owner, and
    SECURITY DEFINER RPCs) inserts normally — trigger firing never checks
    EXECUTE, so the revoked grants do not block legitimate observation."""
    tid, group = _seed_group()
    event = _insert_ledger(tid, group)
    assert re.fullmatch(r"[0-9a-f-]{36}", event)
    assert _one("select count(*) from public.visual_group_insert_journal "
                f"where event_id='{event}'") == "1"
    # service_role is SELECT-only on the ledger: direct writes are denied by
    # table grants, independent of the journal.
    done = _run("set role service_role; "
                "insert into public.visual_group_usage_ledger"
                "(gym_id, group_key, reserved_date, channel, state) values "
                f"('{tid}', 'vg_denied_{uuid.uuid4().hex[:6]}', "
                "'2026-10-05', 'ig', 'reserved')", check=False)
    assert done.returncode != 0


def test_direct_journal_mutation_and_truncate_rejected(scratch, installed):
    tid, group = _seed_group()
    event = _insert_ledger(tid, group)
    for stmt, kind in (
            ("insert into public.visual_group_insert_journal"
             "(event_id, ledger_gym_id, ledger_group_key, state,"
             " insert_snapshot) values "
             f"('{uuid.uuid4()}', 'x', 'y', 'reserved', '{{}}')", "insert"),
            ("update public.visual_group_insert_journal set state='released'",
             "update"),
            ("delete from public.visual_group_insert_journal", "delete"),
            ("truncate public.visual_group_insert_journal", "truncate")):
        done = _run(f"set role service_role; {stmt}", check=False)
        assert done.returncode != 0, f"service_role {kind} must be rejected"
    assert _one("select count(*) from public.visual_group_insert_journal") == "1"
    # Even the owner hits the append-only trigger on row mutation.
    done = _run("delete from public.visual_group_insert_journal", check=False)
    assert done.returncode != 0 and "append-only" in done.stderr


def test_post_install_importer_and_evaluators_unchanged(scratch_stack):
    """Installing the journal changes NO preexisting public function:
    importer, coverage evaluators, activation, claim and writer functions are
    byte-identical (full pg_get_functiondef, keyed by schema/name/identity
    args), and the complete catalog delta is strictly additive journal
    objects (observation only)."""
    _fresh_scratch_schema()
    _apply_stack()
    before_defs = _function_defs()
    before_cat = set(_catalog_snapshot().splitlines())
    _apply(JOURNAL.read_text() + "\n")
    after_defs = _function_defs()
    for key, val in before_defs.items():
        assert after_defs.get(key) == val, f"preexisting function changed: {key}"
    new = set(after_defs) - set(before_defs)
    assert new == {f"public.{fn}()" for fn in JOURNAL_FUNCTIONS}
    added = set(_catalog_snapshot().splitlines()) - before_cat
    assert before_cat <= set(_catalog_snapshot().splitlines()), \
        "preexisting catalog object changed or vanished"
    marker = re.compile(r"journal|observed_insert_event|insert_event|visual_group_ledger_(?:event_id_immutable|record_insert)")
    for line in added:
        assert marker.search(line), f"unexpected catalog addition: {line}"
    # Leave the scratch DB in the standard committed state (stack + journal).
    _fresh_scratch_schema()
    _apply_stack()
    _apply(JOURNAL.read_text() + "\n")


def test_negative_controls_detect_body_column_constraint_policy_changes(
        scratch, installed):
    """The comparisons above are not vacuous: a function-body change, a
    column change, a constraint change and a policy change are each
    DETECTED, then the draft is re-applied and the catalog restored."""
    defs0 = _function_defs()
    cat0 = _catalog_snapshot()
    # 1. Function body change.
    _sql("create or replace function "
         "public.visual_group_ledger_record_insert() returns trigger "
         "language plpgsql security definer set search_path=public "
         "as $f$ begin return null; end $f$;")
    assert _function_defs() != defs0, "body change not detected"
    # 2. Column change.
    _apply(JOURNAL.read_text() + "\n")
    assert _function_defs() == defs0, "baseline functions not restored before column control"
    assert _catalog_snapshot() == cat0, "baseline catalog not restored before column control"
    _sql("alter table public.visual_group_usage_ledger "
         "drop column observed_insert_event_id")
    assert _catalog_snapshot() != cat0, "column change not detected"
    # Restore the full draft (functions, column, FK, policy), then mutate the
    # constraint and the policy specifically.
    _apply(JOURNAL.read_text() + "\n")
    # 3. Constraint change.
    assert _function_defs() == defs0, "baseline functions not restored before constraint control"
    assert _catalog_snapshot() == cat0, "baseline catalog not restored before constraint control"
    _sql("alter table public.visual_group_usage_ledger "
         "drop constraint visual_group_ledger_insert_event_fkey")
    assert _catalog_snapshot() != cat0, "constraint change not detected"
    _apply(JOURNAL.read_text() + "\n")
    # 4. Policy change.
    assert _function_defs() == defs0, "baseline functions not restored before policy control"
    assert _catalog_snapshot() == cat0, "baseline catalog not restored before policy control"
    _sql("drop policy visual_group_insert_journal_service_read "
         "on public.visual_group_insert_journal")
    assert _catalog_snapshot() != cat0, "policy change not detected"
    _apply(JOURNAL.read_text() + "\n")
    # Full restore verified against the ORIGINAL baseline.
    assert _function_defs() == defs0
    assert _catalog_snapshot() == cat0
