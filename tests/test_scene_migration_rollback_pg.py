"""Rollback-only proof for the frozen visual scene draft stack on a real PG.

Scope (2026-10-04 Kimi K2.8 tests-only package, task SCENE_ACTIVATION_REPAIR_SPEC
"Required verification" item): load the SAME ordered draft stack that
tests/test_scene_claim_wave_pg.py applies committed, plus
DRAFT_visual_scene_history_backfill_20261004.sql (already in that stack) and
DRAFT_visual_scene_rpc_persisted_state_20261004.sql. Per the 8th draft's own
header, its realistic predecessor is calendar_claim_media_guard_20261002.sql,
so that guard file is installed as the committed scratch baseline FIRST; the
proof then shows the 8th draft replaces those RPC definitions and ACLs inside
the transaction and that rollback restores the baseline byte-for-byte at the
catalog level. The whole stack runs inside ONE transaction which then
ROLLBACKs. The test proves the installed relations (with columns and ACLs),
functions (with signatures, bodies, security flags and ACLs) and triggers
(with full definitions) were visible before the rollback and that the complete
catalog state afterwards is identical to the pre-transaction baseline.

Hard safety properties:
  * source-integrity tests never skip: the frozen SHA256 check and the
    transaction-control stripper check run with no DSN and FAIL (never skip)
    on any missing draft file or drift;
  * the live proof is opt-in only: it runs when ECHO_SCENE_ROLLBACK_TEST_DSN is
    set; a SET DSN with missing prerequisites (no psql, missing draft files,
    hash drift) is a hard FAILURE, not a skip. The DSN guard accepts literally
    only a Unix-socket DSN naming the disposable database
    `echo_scene_rollback_test` (never a network/production host);
  * no destructive scratch reset happens before the frozen source hashes pass;
  * the source migration files are never modified: the test hashes them
    against the frozen SHA256 table below, edits only an in-memory copy, and
    strips ONLY standalone top-level `begin;`/`commit;` lines (dollar-quote,
    line-comment and string aware, so `$$` inside a `--` comment or a quoted
    string is never mistaken for a body delimiter). Exactly eight such lines
    exist across the frozen stack; any other count or any other top-level
    transaction-control line is a hard failure, never a skip;
  * every SQL error fails the test (ON_ERROR_STOP=1);
  * the only statements that may commit are the module fixture's scratch
    prerequisites (roles, stub tables, the calendar_claim_media_guard baseline)
    — the migration stack itself is wrapped in begin; ... rollback;;
  * scratch evidence is preserved: the fixture does not drop the schema at
    teardown and no scratch path is recursively deleted.

Receipts of the intended live run live outside the repo (see task report).
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DSN = os.environ.get("ECHO_SCENE_ROLLBACK_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# Exact ordered stack from tests/test_scene_claim_wave_pg.py, plus the
# persisted-state RPC draft (applies after the frozen scene-wave stack per its
# own header; standalone, additive `create or replace`).
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_history_backfill_20261004.sql",
    "DRAFT_visual_scene_rpc_persisted_state_20261004.sql",
]

# Realistic predecessor required by the 8th draft's header: installed as the
# committed scratch baseline so the proof shows replacement of preexisting RPC
# definitions/permissions inside the transaction and exact restoration after
# rollback.
BASELINE = "calendar_claim_media_guard_20261002.sql"

# Frozen source hashes for the reviewed draft stack, including the
# claim-time guard at 8ab1885.
FROZEN_SHA256 = {
    "DRAFT_visual_group_schema_20261002.sql":
        "36418af9933bba651b16bb214e21c5f72f4a51a7c3cc2c5b6566927c4ebe7c26",
    "DRAFT_visual_global_history_20261002.sql":
        "f062b3b7d346f3e2e22e203608c9cbba25eda1a3bc8a4eebb28ef5b6c836566d",
    "DRAFT_visual_group_claim_trigger_20261002.sql":
        "f96b53341cc2dcf6dbb90be30da40a3b4af27a51c3359776f3c663e104ef3bf8",
    "DRAFT_visual_group_backfill_20261002.sql":
        "aa13d21d18e8375ff32d708757a89d11fdfb86fa37112d7f14b650b952bff95e",
    "DRAFT_visual_group_activation_20261002.sql":
        "9add1ad6c93d2eeac89eee58f5c15a5c17ed21750ea5847a72d49e213f1f302f",
    "DRAFT_visual_scene_claim_wave_20261003.sql":
        "8fae2bdc788ed874e34a13d9f46ae77262bd93ef928cc2f5a4d8d4de9a5ee570",
    "DRAFT_visual_scene_history_backfill_20261004.sql":
        "86548ae1e6d28ac50f467cc1f1065b8f3e113bd39d96bc1a8572b9e566c6d499",
    "DRAFT_visual_scene_rpc_persisted_state_20261004.sql":
        "38b8aafc6e22bcab2124e1304d77bf4bafa9fca7bdfa01b1b95557145d2bec01",
}

# Exactly four drafts carry one `begin;` and one `commit;` each at top level
# (verified against the frozen hashes above): the stripper must remove
# exactly this many lines across the whole stack, no more, no fewer.
EXPECTED_STRIPPED_LINES = 8

# Key contract objects the stack must install (visible in-txn, absent after).
KEY_TABLES = (
    "visual_group", "tenant_alias", "visual_global_usage",
    "visual_global_usage_member", "visual_group_usage_ledger",
    "visual_group_usage_sibling", "visual_scene_candidate",
    "visual_scene_phash_occupied", "visual_scene_review_hold",
)
KEY_FUNCTIONS = (
    "visual_group_activate_guard", "visual_scene_register_candidate",
    "claim_calendar_publish_slot_owned", "approve_calendar_row_if_media_ready",
    # visual_group_guard_trigger is a TRIGGER FUNCTION installed by
    # DRAFT_visual_group_claim_trigger_20261002.sql (create or replace
    # function public.visual_group_guard_trigger() returns trigger), not a
    # pg_trigger row; it is probed via pg_proc like the other functions.
    "visual_group_guard_trigger",
)
# Real pg_trigger rows the frozen stack installs (verified against the
# migration DDL): the content_calendar guard pair from the group claim
# trigger draft, the immutable/guard triggers on the scene staging tables
# and the scene object member table from the claim-wave/global-history
# drafts, plus the history backfill activation receipt on
# visual_group_activation. The scene claim wave merges into the group guard;
# its separate trigger name appears only in a superseded comment.
KEY_TRIGGERS = (
    "content_calendar_visual_group_guard",
    "content_calendar_visual_group_truncate_guard",
    "visual_scene_candidate_immutable",
    "visual_scene_occupied_immutable",
    "visual_scene_review_hold_guard",
    "visual_global_scene_object_member_immutable",
    "visual_scene_history_activation_receipt",
)

# The two RPCs whose preexisting baseline definitions/ACLs the 8th draft
# replaces inside the transaction.
REPLACED_RPCS = (
    "claim_calendar_publish_slot_owned",
    "approve_calendar_row_if_media_ready",
)

DSN_GUARD = re.compile(
    r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_rollback_test"
    r"(?: user=[A-Za-z0-9_-]+)?$")
TX_CONTROL = re.compile(r"^(begin|start\s+transaction|commit|rollback|abort)"
                        r"\s*;?$", re.IGNORECASE)
STRIP_LINE = re.compile(r"^(begin|commit)\s*;?$", re.IGNORECASE)
DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

# Full-state catalog snapshot: object identity PLUS relevant state — relation
# kind, ACLs and column lists; function signature, security flag, ACLs and
# body hash; trigger definition. Rollback must restore this byte-for-byte.
SNAPSHOT_SQL = (
    "select 'r:'||c.relname||':'||c.relkind::text||':'"
    "||coalesce(c.relacl::text,'')||':'||coalesce(("
    "select string_agg(a.attname||':'||format_type(a.atttypid,a.atttypmod)"
    "||':'||a.attnotnull, ',' order by a.attnum)"
    " from pg_attribute a where a.attrelid=c.oid and a.attnum>0"
    " and not a.attisdropped),'')"
    " from pg_class c join pg_namespace n on n.oid=c.relnamespace"
    " where n.nspname='public' and c.relkind in ('r','v','m','S')"
    " union all"
    " select 'f:'||p.proname||':('||pg_get_function_arguments(p.oid)||')'"
    "||pg_get_function_result(p.oid)||':sec='||p.prosecdef||':'"
    "||coalesce(p.proacl::text,'')||':'||md5(p.prosrc)"
    " from pg_proc p join pg_namespace n on n.oid=p.pronamespace"
    " where n.nspname='public'"
    " union all"
    " select 't:'||t.tgname||':'||pg_get_triggerdef(t.oid)"
    " from pg_trigger t where not t.tgisinternal"
    " and t.tgname not like 'RI_ConstraintTrigger%'"
)

# In-transaction probe capturing full definition + ACL of a baseline RPC so
# the proof can show the 8th draft replaced it inside the transaction.
RPC_STATE_SQL = (
    "select 'RPC_STATE:{name}:'||md5(p.prosrc)||':'"
    "||coalesce(p.proacl::text,'') from pg_proc p join pg_namespace n"
    " on n.oid=p.pronamespace where n.nspname='public'"
    " and p.proname='{name}'"
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


def _verify_frozen_sources():
    """Fail (never skip) unless every frozen draft exists and hashes exactly.
    Returns the verified source texts. No database required."""
    sources = {}
    for name in STACK:
        path = MIGRATIONS / name
        assert path.is_file(), \
            f"frozen draft {name} is missing from {MIGRATIONS}; refusing"
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert digest == FROZEN_SHA256[name], \
            f"{name} drifted from the frozen stack; refusing to run"
        sources[name] = raw.decode()
    return sources


def _strip_transaction_control(source, name):
    """Return source with ONLY standalone top-level begin;/commit; lines
    removed. The scanner is dollar-quote aware AND line-comment/string aware:
    `$$` appearing inside a `--` comment or a single-quoted string is never
    treated as a dollar-quote delimiter, and begin;/commit; inside dollar
    bodies (plpgsql, do blocks) are never touched."""
    out_lines = []
    stripped = 0
    tag = None        # None = outside dollar quotes; else the active tag
    in_string = False  # inside a '...' string ('' is the escape)
    for lineno, line in enumerate(source.splitlines(), 1):
        # Determine the top-level state at the START of this line, then scan
        # the line so comment/string/$-tag transitions are tracked exactly.
        for m in DOLLAR_TAG.finditer(line):
            if tag is None:
                # Outside a dollar body: a tag only counts if it is not
                # inside a line comment or a string. Scan the prefix.
                prefix = line[:m.start()]
                if _prefix_leaves_top_level(prefix, in_string):
                    tag = m.group(1) or ""
                    in_string = False
            elif (m.group(1) or "") == tag:
                tag = None
        if tag is None:
            # Update in_string for the NEXT line only when still top-level:
            # a string opened and not closed on this line carries over.
            in_string = _string_state_at_eol(line, in_string)
            if not in_string:
                stripped_line = line.strip()
                if STRIP_LINE.match(stripped_line):
                    stripped += 1
                    continue
                if TX_CONTROL.match(stripped_line):
                    raise AssertionError(
                        f"{name}:{lineno}: unexpected top-level transaction-"
                        f"control line {stripped_line!r}; refusing to mask it")
        else:
            in_string = False
        out_lines.append(line)
    assert tag is None, f"{name}: unbalanced dollar-quoting while stripping"
    return "\n".join(out_lines) + "\n", stripped


def _prefix_leaves_top_level(prefix, in_string):
    """True when `prefix` (the text before a candidate $$ on its line) leaves
    the scanner at top level: not inside a string and no `--` comment started.
    """
    if in_string:
        # Already inside a multi-line string: only a closing quote (not
        # followed by another quote) returns to top level.
        i = 0
        while i < len(prefix):
            if prefix[i] == "'":
                if i + 1 < len(prefix) and prefix[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
            i += 1
        if in_string:
            return False
        prefix_after = prefix[i:]
    else:
        prefix_after = prefix
    quote = False
    i = 0
    while i < len(prefix_after):
        ch = prefix_after[i]
        if quote:
            if ch == "'":
                if i + 1 < len(prefix_after) and prefix_after[i + 1] == "'":
                    i += 2
                    continue
                quote = False
        elif ch == "'":
            quote = True
        elif ch == "-" and i + 1 < len(prefix_after) \
                and prefix_after[i + 1] == "-":
            return False  # rest of the line is a comment: $$ is inert
        i += 1
    return not quote


def _string_state_at_eol(line, in_string):
    """Whether the line ends inside a single-quoted string (strings may span
    lines in these drafts). Line comments are honored outside strings."""
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
        elif ch == "'":
            in_string = True
        elif ch == "-" and i + 1 < len(line) and line[i + 1] == "-":
            break  # line comment swallows the rest
        i += 1
    return in_string


def _strip_stack(sources):
    stripped_parts = []
    total = 0
    for name in STACK:
        stripped, n = _strip_transaction_control(sources[name], name)
        total += n
        stripped_parts.append(stripped)
    return "".join(stripped_parts), total


def _snapshot():
    """Full relevant object state of public (identity + kind + ACLs + columns
    + function signatures/bodies/security flags + trigger definitions)."""
    return set(_sql(SNAPSHOT_SQL).splitlines())


def _rpc_state(name):
    return _one(RPC_STATE_SQL.format(name=name))


def _one(statement):
    out = _sql(statement)
    return out.splitlines()[-1] if out else ""


def test_frozen_source_hashes_unchanged():
    """Source integrity: runs with NO DSN; missing files or drift FAIL."""
    sources = _verify_frozen_sources()
    assert len(sources) == len(STACK)


def test_transaction_control_stripper_exact():
    """Stripper contract: runs with NO DSN. Exactly eight standalone top-level
    begin;/commit; lines exist across the frozen stack (verified above); the
    stripper must remove exactly those eight and nothing else, without ever
    mistaking `$$` in comments or strings for a dollar-quoted body."""
    sources = _verify_frozen_sources()
    script, total = _strip_stack(sources)
    assert total == EXPECTED_STRIPPED_LINES, (
        f"expected exactly {EXPECTED_STRIPPED_LINES} stripped top-level "
        f"begin;/commit; lines across the frozen stack, got {total}; "
        "source drift or stripper defect — refusing")
    # No standalone transaction-control line may survive at top level.
    _, residual = _strip_transaction_control(script, "<stripped stack>")
    assert residual == 0
    # Sanity: begin/end INSIDE dollar-quoted bodies must survive intact.
    assert script.count("begin") > EXPECTED_STRIPPED_LINES
    # The stripper must not remove anything but full lines: re-joining the
    # kept lines of each file plus its removed lines reproduces the source.
    for name in STACK:
        stripped, n = _strip_transaction_control(sources[name], name)
        assert len(sources[name].splitlines()) - len(stripped.splitlines()) == n


@pytest.fixture(scope="module")
def _scratch_cluster():
    """Committed scratch prerequisites ONLY. Runs the frozen-hash check FIRST:
    no destructive scratch reset may happen before source integrity passes.
    A set DSN with any missing prerequisite is a hard failure, not a skip."""
    if not DSN:
        pytest.skip("ECHO_SCENE_ROLLBACK_TEST_DSN not set")
    # Source integrity gate (fail, never skip) BEFORE any destructive reset.
    sources = _verify_frozen_sources()
    assert PSQL, "ECHO_SCENE_ROLLBACK_TEST_DSN is set but psql is unavailable"
    baseline_path = MIGRATIONS / BASELINE
    assert baseline_path.is_file(), \
        f"DSN set but predecessor {BASELINE} missing from {MIGRATIONS}"
    assert DSN_GUARD.fullmatch(DSN), \
        "only a Unix-socket DSN naming disposable echo_scene_rollback_test"
    assert " host=localhost" not in DSN and " host=127." not in DSN
    assert _one("select current_database()") == "echo_scene_rollback_test"
    # Fresh scratch schema; roles the drafts' revoke/grant statements name.
    _sql("drop schema public cascade; create schema public; "
         "grant usage on schema public to public;")
    _sql("do $$ begin "
         "if not exists (select 1 from pg_roles where rolname='anon') then "
         "create role anon nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='authenticated') "
         "then create role authenticated nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='service_role') "
         "then create role service_role nologin; end if; end $$")
    # Stub content_calendar (columns the trigger chain and the persisted-state
    # RPCs read; `format` is required by the RPC draft's body) and media_asset.
    _sql("create table if not exists public.content_calendar ("
         "id uuid primary key default gen_random_uuid(),"
         "gym_id text, account text, post_date date,"
         "status text, variant_status text, format text,"
         "published_at timestamptz, publish_claim_token uuid,"
         "publish_reservation_day date,"
         "late_post_id text, image_url text, thumbnail_url text,"
         "source_media_url text, source_media_asset_id text,"
         "drive_file_id text, byte_hash text, r2_key text,"
         "media_not_ready_reason text)")
    _sql("create table if not exists public.media_asset ("
         "id text primary key, gym_id text, content_hash text)")
    # Row-state helper stubs satisfy the ledger's coverage views at apply time
    # (the claim-trigger draft later CREATE OR REPLACEs the real versions).
    _sql("create or replace function public.visual_group_row_active("
         "public.content_calendar) returns boolean"
         " language sql stable as $$ select false $$")
    _sql("create or replace function public.visual_group_row_ambiguous("
         "public.content_calendar) returns boolean"
         " language sql stable as $$ select false $$")
    # Realistic predecessor baseline required by the 8th draft's header:
    # calendar_claim_media_guard_20261002.sql, loaded verbatim and committed.
    baseline = baseline_path.read_text()
    assert not TX_CONTROL.match(baseline.strip()), \
        f"{BASELINE} carries transaction control; cannot be a committed baseline"
    _run(baseline)
    # Permission-replacement marker: the 8th draft revokes `authenticated`
    # from both RPCs. Grant it here (committed scratch setup) so the proof
    # can observe the draft's revoke replacing preexisting permissions INSIDE
    # the transaction and rollback restoring them exactly.
    _sql("grant execute on function"
         " public.claim_calendar_publish_slot_owned("
         "uuid, text, date, text, integer, boolean) to authenticated")
    _sql("grant execute on function"
         " public.approve_calendar_row_if_media_ready(uuid, text)"
         " to authenticated")
    # Sanity: baseline RPCs exist before the migration stack runs.
    for name in REPLACED_RPCS:
        assert _rpc_state(name), f"baseline RPC {name} missing after setup"
    yield {"sources": sources, "baseline_path": baseline_path}
    # Preserve scratch evidence: no schema drop, no recursive deletion.


def test_migration_stack_visible_in_transaction_and_absent_after_rollback(
        _scratch_cluster):
    sources = _scratch_cluster["sources"]
    before = _snapshot()
    baseline_rpc_state = {name: _rpc_state(name) for name in REPLACED_RPCS}

    script, stripped_total = _strip_stack(sources)
    assert stripped_total == EXPECTED_STRIPPED_LINES, (
        f"exactly {EXPECTED_STRIPPED_LINES} top-level begin;/commit; lines "
        f"must be stripped from the frozen stack, got {stripped_total}")

    probes = []
    for t in KEY_TABLES:
        probes.append(
            f"select 'NEW_OBJECT:r:{t}' where exists (select 1 from pg_class"
            f" join pg_namespace on pg_namespace.oid=pg_class.relnamespace"
            f" where nspname='public' and relname='{t}');")
    for f in KEY_FUNCTIONS:
        probes.append(
            f"select 'NEW_OBJECT:f:{f}' where exists (select 1 from pg_proc"
            f" join pg_namespace on pg_namespace.oid=pg_proc.pronamespace"
            f" where nspname='public' and proname='{f}');")
    for t in KEY_TRIGGERS:
        probes.append(
            f"select 'NEW_OBJECT:t:{t}' where exists"
            f" (select 1 from pg_trigger where tgname='{t}'"
            f" and not tgisinternal);")
    # Full in-transaction state of the replaced RPCs (body hash + ACL).
    for name in REPLACED_RPCS:
        probes.append(RPC_STATE_SQL.format(name=name) + ";")

    session = "begin;\n" + script + "\n" + "\n".join(probes) + "\nrollback;\n"
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=session, text=True, capture_output=True, timeout=300)
    assert done.returncode == 0, (
        "migration stack failed inside the rollback-wrapped transaction: "
        + done.stderr)
    seen = {ln for ln in done.stdout.splitlines()
            if ln.startswith("NEW_OBJECT:")}
    expected = {f"NEW_OBJECT:r:{t}" for t in KEY_TABLES} | \
               {f"NEW_OBJECT:f:{f}" for f in KEY_FUNCTIONS} | \
               {f"NEW_OBJECT:t:{t}" for t in KEY_TRIGGERS}
    missing = expected - seen
    assert not missing, f"objects not visible before rollback: {sorted(missing)}"
    installed = {ln.removeprefix("NEW_OBJECT:") for ln in seen}

    # Replacement of the preexisting RPC definitions/permissions must be
    # visible INSIDE the transaction: in-txn state differs from the committed
    # calendar_claim_media_guard baseline.
    in_txn_rpc = {}
    for ln in done.stdout.splitlines():
        if ln.startswith("RPC_STATE:"):
            _, name, body_hash, acl = ln.split(":", 3)
            in_txn_rpc[name] = (body_hash, acl)
    assert set(in_txn_rpc) == set(REPLACED_RPCS), \
        f"in-transaction RPC state probes missing: {sorted(set(REPLACED_RPCS) - set(in_txn_rpc))}"
    for name in REPLACED_RPCS:
        base_line = baseline_rpc_state[name].split(":", 2)[2]
        base_body, base_acl = base_line.split(":", 1)
        assert in_txn_rpc[name][0] != base_body, (
            f"{name}: 8th draft did not replace the baseline definition "
            "inside the transaction")
        assert in_txn_rpc[name][1] != base_acl, (
            f"{name}: 8th draft did not replace the baseline permissions "
            "inside the transaction (the draft's revoke of the scratch "
            "`authenticated` marker grant must be visible in-transaction)")
        assert "authenticated" not in in_txn_rpc[name][1], (
            f"{name}: revoked `authenticated` grant still present "
            "in-transaction")

    after = _snapshot()
    leaked = {ln.split(":", 2)[0] + ":" + ln.split(":", 2)[1]
              for ln in after - before} & installed
    assert not leaked, f"objects survived rollback: {sorted(leaked)}"
    # Exact restoration: full relevant object state (columns, ACLs, function
    # signatures/bodies/security flags, trigger definitions) after rollback is
    # identical to the pre-transaction baseline — including the restored
    # calendar_claim_media_guard RPC definitions and permissions.
    assert before == after, (
        "catalog state differs after rollback; unexpected delta: "
        f"{sorted((before - after) | (after - before))}")
    for name in REPLACED_RPCS:
        assert _rpc_state(name) == baseline_rpc_state[name], \
            f"{name}: baseline definition/ACL not restored after rollback"
    # Explicit contract spot-checks, post-rollback.
    for t in KEY_TABLES:
        assert _one("select count(*) from pg_class c join pg_namespace n"
                    " on n.oid=c.relnamespace where n.nspname='public'"
                    f" and c.relname='{t}'") == "0", \
            f"table {t} survived rollback"
    for t in KEY_TRIGGERS:
        assert _one(f"select count(*) from pg_trigger where"
                    f" tgname='{t}' and not tgisinternal") == "0", \
            f"trigger {t} survived rollback"
