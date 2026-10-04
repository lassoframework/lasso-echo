"""Scene original-use receipt (TWO-PHASE redesign, ROUND-2 repairs): static
source contracts + OPTIONAL real-PostgreSQL adversarial checks on a disposable
scratch DB.

Review lineage:
  * Round 1 (Sol, /tmp/fixer_scene_receipt_sol_safety_20261004.log) killed
    the trigger-minted design: never mint via a raising terminal AFTER
    trigger after the provider send; same-day siblings share one ledger
    event/occupancy; published status is never provider-use proof.
  * Round 2 (independent rejection of the two-phase draft) — six findings
    repaired in the migration and covered by tests below:
      P0  BEFORE UPDATE OR DELETE OR TRUNCATE ... FOR EACH ROW is invalid
          PostgreSQL (TRUNCATE triggers are statement-level only); the draft
          could not install. Now row UPDATE/DELETE triggers plus separate
          FOR EACH STATEMENT TRUNCATE triggers (install probe proves it).
      P1  service_role could forge terminal delivery via the public RPC ->
          owner-only authoritative attestation gate
          (visual_scene_original_use_attestation); terminal outcomes are
          IMPOSSIBLE until the separate attester integration exists.
      P1  raw gym alias keys vs canonical ledger keys -> prepare resolves
          the canonical tenant exactly like the claim flow
          (visual_group_tenant_strict) and uses it for every key.
      P1  caller URLs could diverge from row/occupancy while md5 matched ->
          delivered_url bound to visual_scene_row_delivered_object(row) and
          occupancy evidence->>'exact_url'; source_url bound to the row's
          source_media_url (or source == delivered when none is recorded).
      P1  same-date same-image siblings share ONE occupancy PK -> prepare
          accepts the shared occupancy row for the ledger holder OR an
          active same-day sibling; the adversarial sibling test uses an
          IDENTICAL object/pHash/occupancy, not separate objects.
      P1  the old "provider failure" test proved only RPC rollback/retry ->
          renamed test_pg_terminal_rpc_rollback_retry_converges_one_receipt
          and re-scoped; provider send/readback/no-resend remain UNPROVEN
          application-integration properties.

  * Round 3 (independent P1 integration review) — every prior PG scenario
      ran with enforcement UNARMED, so two P1 integration defects survived
      static 11/11 + scratch 30/30; repaired and covered ARMED below:
      P1  terminate marked the row published without the current-tx
          visual_group_reconciliation receipt the ARMED claim trigger
          (visual_group_finalization_requires_evidence/_evidenced)
          demands -> terminate writes it in-band AFTER the attestation
          gate, from validated data only, for both terminal outcomes;
          armed scenarios prove valid proof succeeds, missing proof and
          service_role reconciliation forgery fail, and a direct marker
          update on a token-holding row still raises.
      P1  prepare required ledger.state='reserved', but the FIRST
          sibling's terminal publication moves the SHARED ledger to
          'published' -> prepare accepts a same-reserved_date published
          ledger; first-sibling-terminal THEN second-sibling-prepare is
          covered ARMED, plus wrong date/tenant refusals.
      P2  expected provider (caller-declared) and channel (row account,
          missing refuses) are bound immutably at prepare; the terminal
          attestation must match both exactly.

  * Round 4 (independent rejection of the round-3 draft) — one P1 + one P2:
      P1  terminate copied ALL sibling attempts and ALL review_hold ids of
          the row into visual_group_reconciliation, laundering unrelated
          HISTORICAL ambiguity as resolved by inclusion ->
          visual_scene_original_use_check_ambiguity refuses unresolved
          historical sibling/hold context (NULL/older/different claim
          token, unprovable original image/provider post, hold without
          per-claim evidence) at PREPARE (pre-send; no attempt row) and
          re-checks at TERMINATE after the attempt/row locks, so ambiguity
          inserted or changed between prepare and terminate cannot be
          laundered: the terminal transaction commits no receipt, no
          reconciliation, no publication, no historical clearance.
          Reconciliation attempts/groups/holds are built ONLY from
          validated current siblings/holds; the armed trigger is not
          weakened and the valid armed sibling path still passes.
      P2  provider_account_id was attester-asserted but unbound -> prepare
          requires a non-empty expected_provider_account_id (caller-
          declared from a verified provider connection, NOT inherently
          trusted), freezes it via the attempt guard, and the terminal
          attestation's provider_account_id must match it exactly.

Static tests always run. PG scenarios run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_original_use_receipt_20261004.sql` exists,
  * SCENE_RECEIPT_TEST_DSN names a disposable Unix-socket database literally
    named `echo_scene_receipt_test` (never a network or production host),
  * local psql is on PATH.

Honesty note: the PG scenarios SIMULATE the future integration's data shapes
(a claimed row, a reserved ledger row, the claim path's occupancy row, real
read/render receipts, owner-written attestations). Nothing in the live app
calls prepare/terminate yet and NO attester runtime exists (asserted by
test_static_no_live_wiring); both are explicitly outstanding integration
dependencies. Scenarios commit only to the disposable scratch database.
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("SCENE_RECEIPT_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
REPO = Path(__file__).resolve().parents[1]
RECEIPT = MIGRATIONS / "DRAFT_visual_scene_original_use_receipt_20261004.sql"
CLAIM_TRIGGER = MIGRATIONS / "DRAFT_visual_group_claim_trigger_20261002.sql"
CLAIM_WAVE = MIGRATIONS / "DRAFT_visual_scene_claim_wave_20261003.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_history_backfill_20261004.sql",
    "DRAFT_visual_scene_ledger_coverage_gate_20261004.sql",
    "DRAFT_visual_scene_original_use_receipt_20261004.sql",
]

PG_READY = bool(DSN) and PSQL is not None and RECEIPT.exists()


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


def _script(script, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=300)
    if check:
        assert done.returncode == 0, done.stderr
    return done


def _fails(script, needle):
    done = _script(script, check=False)
    assert done.returncode != 0, "expected failure, got success"
    assert needle in done.stderr, done.stderr
    return done.stderr


def _check_dsn():
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_receipt_test"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_receipt_test allowed"
    assert _one("select current_database()") == "echo_scene_receipt_test"


def _fresh_scratch_schema():
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


def _receipt_body():
    """The draft file minus ONLY its own outer begin;/commit; wrapper."""
    lines = RECEIPT.read_text().splitlines()
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
        "select 'r:'||c.relname||':'||c.relkind::text from pg_class c "
        " join pg_namespace n on n.oid=c.relnamespace "
        " where n.nspname='public' and c.relkind in ('r','v','m','S','i') "
        "union all "
        "select 'g:'||t.tgname from pg_trigger t where not t.tgisinternal) s")


@pytest.fixture(scope="module")
def install_probes():
    """Rollback-only installation probe + armed late-install refusal. Runs
    BEFORE any committed scenario setup on the scratch DB. The plain apply
    also proves the round-2 P0 (invalid TRUNCATE row triggers) is gone."""
    if not PG_READY:
        pytest.skip("receipt PG checks need SCENE_RECEIPT_TEST_DSN on a "
                    "disposable local DB and the receipt migration present")
    _check_dsn()
    _fresh_scratch_schema()
    _apply("".join((MIGRATIONS / n).read_text() + "\n" for n in STACK[:-1]))

    before = _catalog_snapshot()
    _apply("begin;\n" + _receipt_body() + "\nrollback;\n")
    assert _catalog_snapshot() == before, \
        "rollback-only install probe left catalog changes behind"

    tid, _raw, _group = _seed_tenant()
    _sql("set session_replication_role = replica; "
         "insert into public.visual_group_activation(gym_id, proof, actor) "
         f"values ('{tid}', '{{}}'::jsonb, 'receipt-test'); "
         "insert into public.gym_visual_guard_settings(gym_id, enforce) "
         f"values ('{tid}', true);")
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=RECEIPT.read_text(), text=True, capture_output=True, timeout=300)
    assert done.returncode != 0
    assert "must be installed before any tenant is armed" in done.stderr
    assert _one("select count(*) from pg_class c join pg_namespace n "
                "on n.oid=c.relnamespace where n.nspname='public' and "
                "c.relname='visual_scene_original_use_receipt'") == "0"
    assert _catalog_snapshot() == before


@pytest.fixture(scope="module")
def scratch_stack(install_probes):
    if not PG_READY:
        pytest.skip("receipt PG checks need SCENE_RECEIPT_TEST_DSN on a "
                    "disposable local DB and the receipt migration present")
    _check_dsn()
    _fresh_scratch_schema()
    _apply("".join((MIGRATIONS / n).read_text() + "\n" for n in STACK))


@pytest.fixture()
def scratch(scratch_stack):
    """Scenario isolation (scratch database only, never production)."""
    _sql("set session_replication_role = replica; "
         "truncate public.content_calendar, public.visual_scene_review_hold, "
         "public.visual_scene_phash_occupied, public.visual_scene_candidate, "
         "public.visual_global_usage, public.visual_global_usage_member, "
         "public.visual_global_release_history, "
         "public.visual_group_usage_ledger, "
         "public.visual_group_usage_sibling, public.visual_group, "
         "public.visual_group_alias, public.tenant_alias, "
         "public.visual_group_reconciliation, "
         "public.visual_group_member_event, "
         "public.visual_global_object_read_receipt, "
         "public.visual_global_render_receipt, "
         "public.visual_global_object_attestation, "
         "public.visual_global_scene_object_member, "
         "public.visual_global_object_lineage, "
         "public.visual_group_activation, "
         "public.gym_visual_guard_settings, "
         "public.visual_scene_original_use_attempt, "
         "public.visual_scene_original_use_attestation, "
         "public.visual_scene_original_use_receipt cascade")


# ---- seeding helpers (scratch DB only; SIMULATED claim-path shapes) --------

def _lit(v):
    return "NULL" if v is None else f"'{v}'"


def _seed_tenant(raw=None):
    """Canonical tenant + optional raw alias + canonical visual group. The
    claim stack keys visual_group (and ledger/sibling/occupancy) by the
    CANONICAL tenant id: the group-mint RPC canonicalizes with
    visual_group_tenant_strict and the exact-byte guard maps the raw
    calendar key at the trigger boundary."""
    tid = str(uuid.uuid4())
    raw = raw or tid
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{raw}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    return tid, raw, group


def _read_receipt(tid, url, fp):
    return _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'receipt-test') returning receipt_id")


def _object(tid):
    """A fresh object with a REAL owner read receipt (source == delivered)."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    rr = _read_receipt(tid, url, fp)
    return url, fp, rr


def _claimed_row(tid, group, used_date="2026-09-01", gym=None,
                 image_url=None, account="scratch-ig"):
    """A row mid-claim: status 'publishing', holding a real claim token.
    gym defaults to the canonical tenant id; pass a raw alias to exercise
    the claim flow's alias path. image_url is the row's delivered display
    object (visual_scene_row_delivered_object)."""
    return _one(
        "insert into public.content_calendar(gym_id, post_date, status,"
        " publish_claim_token, visual_group_key, image_url, account) values "
        f"({_lit(gym or tid)}, '{used_date}', 'publishing', gen_random_uuid(),"
        f" '{group}', {_lit(image_url)}, {_lit(account)}) "
        "returning id::text || '|' || publish_claim_token::text"
    ).split("|")


def _reserve(gym_key, group, row_id, used_date="2026-09-01"):
    """Ledger reservation keyed EXACTLY like the claim flow (canonical)."""
    _sql("insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, calendar_row_id, state) values "
         f"('{gym_key}', '{group}', '{used_date}', '{row_id}', 'reserved')")


def _occupy(tid, group, row_id, fp, phash, url, used_date="2026-09-01",
            replica=False):
    """Occupancy in the claim path's own shape: PK (phash, tenant, group,
    date) shared by same-date siblings; evidence carries the bound
    candidate's exact_url, exactly as visual_scene_claim_decide writes it.
    replica=True bypasses triggers for ARMED-tenant seeds only."""
    _sql(("set session_replication_role = replica; " if replica else "")
         + "insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, channel, evidence) values "
         f"('{phash}', '{tid}', '{group}', '{used_date}', '{fp}',"
         f" '{row_id}', 'scratch-ig',"
         f" jsonb_build_object('candidate_id', '{uuid.uuid4()}'::uuid,"
         f" 'exact_url', '{url}'))")


def _sibling(gym_key, group, row_id, ambiguous=False, token=None,
             channel="scratch-ig"):
    _sql("insert into public.visual_group_usage_sibling"
         "(gym_id, group_key, calendar_row_id, channel, ambiguous,"
         " original_claim_token) values "
         f"('{gym_key}', '{group}', '{row_id}', '{channel}',"
         f" {'true' if ambiguous else 'false'},"
         f" {_lit(token)})")


def _arm(tid):
    """Arm enforcement for this tenant (owner-only seed; the activation
    receipt flow itself is tested elsewhere). ONLY the seed bypasses
    triggers — every RPC under test below runs with the armed
    content_calendar trigger fully engaged."""
    _sql("set session_replication_role = replica; "
         "insert into public.visual_group_activation(gym_id, proof, actor) "
         f"values ('{tid}', '{{}}'::jsonb, 'receipt-test'); "
         "insert into public.gym_visual_guard_settings(gym_id, enforce) "
         f"values ('{tid}', true);")


def _global_object(tid, group, url, fp, rr):
    """Owner global byte evidence in the claim stack's own shape: object
    attestation (FK to a real owner read receipt) plus BOTH scene member
    roles, so visual_global_scene_complete and
    visual_global_row_bytes_verified hold for a same-object row. The armed
    trigger's finalization path calls visual_global_claim_scene, which
    hard-requires exactly this evidence."""
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by)"
         f" values ('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{rr}',"
         " 'receipt-test')")
    _sql("insert into public.visual_global_scene_object_member"
         "(tenant_id, group_key, exact_url, fingerprint, object_role)"
         f" values ('{tid}', '{group}', '{url}', '{fp}', 'source'),"
         f" ('{tid}', '{group}', '{url}', '{fp}', 'delivered')")


def _armed_claim(tid, group, url, used_date="2026-09-01",
                 account="scratch-ig", holder=False):
    """Claim-path shapes on an ARMED tenant, seeded with triggers bypassed
    (replica) so the scenario isolates the RPC-under-test: canonical_url
    alias, claimed row (source_media_url bound to the same object), the
    ledger reservation (holder=True) and the ambiguous active sibling
    bound to the row's own live token. Returns (row_id, token)."""
    _sql("set session_replication_role = replica; "
         "insert into public.visual_group_alias(gym_id, alias_kind,"
         " alias_value, group_key) values "
         f"('{tid}', 'canonical_url', '{url}', '{group}') "
         "on conflict do nothing")
    row_id, token = _one(
        "set session_replication_role = replica; "
        "insert into public.content_calendar(gym_id, post_date, status,"
        " publish_claim_token, visual_group_key, image_url,"
        " source_media_url, account) values "
        f"('{tid}', '{used_date}', 'publishing', gen_random_uuid(),"
        f" '{group}', '{url}', '{url}', '{account}') "
        "returning id::text || '|' || publish_claim_token::text").split("|")
    if holder:
        _sql("set session_replication_role = replica; "
             "insert into public.visual_group_usage_ledger(gym_id,"
             " group_key, reserved_date, calendar_row_id, state, ambiguous)"
             f" values ('{tid}', '{group}', '{used_date}', '{row_id}',"
             " 'reserved', true)")
    _sql("set session_replication_role = replica; "
         "insert into public.visual_group_usage_sibling(gym_id, group_key,"
         " calendar_row_id, channel, ambiguous, original_claim_token,"
         " original_image_url)"
         f" values ('{tid}', '{group}', '{row_id}', '{account}', true,"
         f" '{token}', '{url}')")
    return row_id, token


def _hist_sibling(tid, group2, row_id, token="old", image="set",
                  post=None, used_date="2026-09-01"):
    # Adversarial UNRESOLVED ambiguous sibling of the same calendar row on
    # a second group (its own group+ledger rows satisfy the sibling FK).
    # token: 'old' = a different (older) token, None = NULL, else explicit
    # uuid. image: 'set' = a stale old image URL, None = NULL, else
    # explicit URL. replica-seeded: the scenario isolates the RPC gate.
    tok = (f"'{uuid.uuid4()}'" if token == "old"
           else ("NULL" if token is None else f"'{token}'"))
    img = (f"'https://scratch.example/{uuid.uuid4().hex}.jpg'"
           if image == "set" else ("NULL" if image is None
                                   else f"'{image}'"))
    post_sql = "NULL" if post is None else f"'{post}'"
    _sql("set session_replication_role = replica; "
         f"insert into public.visual_group(gym_id, group_key) values "
         f"('{tid}', '{group2}'); "
         f"insert into public.visual_group_usage_ledger(gym_id, group_key,"
         f" reserved_date, state) values "
         f"('{tid}', '{group2}', '{used_date}', 'reserved'); "
         f"insert into public.visual_group_usage_sibling(gym_id, group_key,"
         f" calendar_row_id, channel, ambiguous, original_claim_token,"
         f" original_image_url, original_provider_post_id) values "
         f"('{tid}', '{group2}', '{row_id}', 'scratch-ig', true,"
         f" {tok}, {img}, {post_sql})")


def _hold(tid, row_id, reason):
    # Uncovered review_hold member event for the row. reason: raw text or
    # None (non-JSON / no per-claim evidence) or a dict (JSON per-claim
    # evidence). Returns the event id.
    reason_sql = ("NULL" if reason is None
                  else "'" + (json.dumps(reason)
                              if isinstance(reason, dict)
                              else reason).replace("'", "''") + "'")
    return _one("insert into public.visual_group_member_event(gym_id,"
                " alias_value, action, actor, reason) values "
                f"('{tid}', '{row_id}', 'review_hold',"
                f" 'runtime_ambiguous_review', {reason_sql}) returning id")


def _prepare_args(tid, group, row_id, token, url, fp, rr,
                  used_date="2026-09-01", phash=None):
    phash = phash or uuid.uuid4().hex[:16]
    return {
        "claim_attempt_id": token, "calendar_row_id": row_id,
        "group_key": group, "used_date": used_date,
        "provider": "zernio", "provider_account_id": "acct_1",
        "source_url": url, "source_md5": fp,
        "delivered_url": url, "delivered_md5": fp,
        "delivered_phash": phash,
        "source_read_receipt": rr, "delivered_read_receipt": rr,
    }, phash


def _prepare(args):
    return _one("select public.visual_scene_original_use_prepare("
                f"'{json.dumps(args)}'::jsonb)")


def _terminate(token, outcome, post=None):
    payload = {"claim_attempt_id": token, "outcome": outcome}
    if post:
        payload["provider_post_id"] = post
    return _one("select public.visual_scene_original_use_terminate("
                f"'{json.dumps(payload)}'::jsonb)")


def _attest(args, tid, outcome="delivered", post=None, **over):
    """OWNER-written authoritative provider outcome attestation. In scratch
    the suite role IS the owner; in production this row can only come from
    the separately reviewed attester integration (service_role has no
    INSERT). The readback payload is SIMULATED — never provider proof."""
    readback = over.get("readback") or {"simulated_readback": outcome,
                                        "scratch": True}
    post_sql = _lit(post) if outcome == "delivered" else "NULL"
    return _one(
        "insert into public.visual_scene_original_use_attestation"
        "(claim_attempt_id, tenant_id, calendar_row_id, outcome, provider,"
        " provider_account_id, channel, provider_post_id, delivered_url,"
        " delivered_md5, delivered_phash, readback_evidence, attested_by)"
        " values "
        f"('{args['claim_attempt_id']}', '{tid}',"
        f" '{over.get('row_id', args['calendar_row_id'])}', '{outcome}',"
        f" '{over.get('provider', 'zernio')}',"
        f" '{over.get('account_id', 'acct_1')}',"
        f" '{over.get('channel', 'scratch-ig')}', {post_sql},"
        f" '{over.get('url', args['delivered_url'])}',"
        f" '{over.get('fp', args['delivered_md5'])}',"
        f" '{over.get('phash', args['delivered_phash'])}',"
        f" '{json.dumps(readback)}'::jsonb, 'scratch-attester')"
        " returning attestation_id")


def _count(table):
    return int(_one(f"select count(*) from public.{table}"))


def _full_setup(tid, raw, group, used_date="2026-09-01", gym=None,
                account="scratch-ig"):
    """Claimed row (image_url = delivered object) + canonical reservation +
    claim-flow-shaped occupancy + real owner read receipt. Returns
    (row_id, token, args, phash)."""
    url, fp, rr = _object(tid)
    row_id, token = _claimed_row(tid, group, used_date, gym=gym,
                                 image_url=url, account=account)
    _reserve(tid, group, row_id, used_date)
    args, phash = _prepare_args(tid, group, row_id, token, url, fp, rr,
                                used_date)
    _occupy(tid, group, row_id, fp, phash, url, used_date)
    return row_id, token, args, phash


# ---- static source contracts (always run, never touch a database) ----------

def _code(text):
    """Migration text with comment lines removed (code only)."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("--"))


def test_static_draft_off_and_additive():
    text = RECEIPT.read_text()
    assert "DRAFT / UNAPPLIED / OFF" in text
    assert "create table if not exists" in text
    assert "drop table" not in _code(text).lower()
    # No GHL anywhere in this package (code; the header comment rules it out).
    assert "gohighlevel" not in _code(text).lower()
    assert "ghl" not in _code(text).lower()
    # No feature flag is enabled by this package: the only reference to the
    # arming table is the fail-closed late-install refusal.
    low = text.lower()
    assert "insert into public.gym_visual_guard_settings" not in low
    assert "update public.gym_visual_guard_settings" not in low


def test_static_two_phase_no_terminal_trigger():
    """Receipts are minted ONLY by the idempotent terminal RPC. There is no
    trigger on content_calendar at all, and nothing raises in a
    publishing->published path after a provider send."""
    text = _code(RECEIPT.read_text()).lower()
    assert "create trigger" in text  # immutability guards on OUR tables only
    assert "on public.content_calendar" not in text
    assert "after update" not in text
    assert "visual_scene_original_use_terminate(jsonb)" in text
    assert "visual_scene_original_use_prepare(jsonb)" in text


def test_static_no_for_each_row_truncate_trigger():
    """Round-2 P0 repair: PostgreSQL supports TRUNCATE only on FOR EACH
    STATEMENT triggers. No FOR EACH ROW trigger in this draft may mention
    TRUNCATE, and every append-only table must have BOTH guards."""
    code = _code(RECEIPT.read_text()).lower()
    stmts = re.findall(r"create trigger.*?;", code, re.S)
    assert stmts, "expected append-only guard triggers"
    for st in stmts:
        if "for each row" in st:
            assert "truncate" not in st, st
    for tbl in ("visual_scene_original_use_attempt",
                "visual_scene_original_use_receipt",
                "visual_scene_original_use_attestation"):
        assert re.search(
            rf"create trigger \S+\s+before truncate\s+on public\.{tbl}\s+"
            rf"for each statement", code, re.S), tbl
        assert re.search(
            rf"create trigger \S+\s+before update or delete\s+"
            rf"on public\.{tbl}\s+for each row", code, re.S), tbl


def test_static_published_status_is_not_proof():
    text = RECEIPT.read_text()
    assert "published status '\n      'is not provider-use proof" in text \
        or "is not provider-use proof" in text
    # prepare refuses rows already marked published
    assert "v_row.status = 'published' or v_row.published_at is not null" \
        in text


def test_static_one_receipt_per_ledger_event():
    """Same-day siblings share the ledger event; exactly one receipt per
    (ledger_gym_id, group_key, used_date), first confirmed delivery wins,
    later siblings are marked superseded, never a second receipt."""
    text = RECEIPT.read_text()
    assert "unique (ledger_gym_id, group_key, used_date)" in text
    assert "on conflict (ledger_gym_id, group_key, used_date) do nothing" \
        in text
    assert "superseded_by_receipt" in text


def test_static_unknown_outcome_stays_held():
    text = RECEIPT.read_text()
    assert "v_outcome not in ('delivered','confirmed_no_send')" in text
    assert "unknown outcomes stay held" in text
    assert "'prepared','confirmed_delivered','confirmed_no_send'" in text


def test_static_evidence_gated_owner_only():
    text = RECEIPT.read_text()
    for needle in (
        "source read receipt missing or mismatched",
        "delivered read receipt missing or mismatched",
        "render receipt missing or does not chain",
        "no current ledger reservation",
        "no current scene occupancy reservation",
        "claim token does not match the live row token",
        "a receipt is never minted without pre-send evidence",
        "visual_scene_original_use_receipt is append-only",
        "visual_scene_original_use_attestation is append-only",
        "prepared attempt evidence is immutable",
        "before any tenant is armed",
    ):
        assert needle in text, needle
    assert "revoke all on public.visual_scene_original_use_receipt" in text
    assert "revoke all on function public.visual_scene_original_use_check_ambiguity(" in text
    assert "revoke all on function public." \
        "visual_scene_original_use_terminate(jsonb)" in text


def test_static_attestation_gate_fail_closed():
    """Round-2 P1 repair: terminal outcomes require an owner-only
    authoritative attestation bound to the exact claim token, tenant, row,
    provider/account/channel/post id and delivered byte lineage, with
    non-empty readback evidence. service_role can read but never write it,
    so the public RPC cannot forge a delivery; until the (not yet built)
    attester integration exists, terminal delivery is impossible."""
    text = RECEIPT.read_text()
    code = _code(text)
    assert "create table if not exists " \
        "public.visual_scene_original_use_attestation" in code
    for needle in (
        "no authoritative provider outcome",
        "attestation for this claim token",
        "references public.visual_scene_original_use_attempt "
        "(claim_attempt_id)",
        "readback_evidence <> '{}'::jsonb",
        "attestation does not match",
        "revoke all on public.visual_scene_original_use_attestation",
        "grant select on public.visual_scene_original_use_attestation "
        "to service_role",
        "enable row level security",
        "references public.visual_scene_original_use_attestation "
        "(attestation_id)",
    ):
        assert needle in text, needle
    # No write grant to ANY non-owner role on the attestation table.
    assert not re.search(
        r"grant\s+(insert|update|delete|all)[^;]*"
        r"visual_scene_original_use_attestation", text, re.I)


def test_static_canonical_keys_match_claim_flow():
    """Round-2 P1 repair, checked against the CLAIM STACK's own source (not
    this file's claims): the exact-byte guard canonicalizes the raw calendar
    key at the trigger boundary, the scene scan resolves with
    visual_group_tenant_strict, and occupancy evidence carries the bound
    exact_url. prepare must resolve and use the same canonical key for
    ledger, sibling, occupancy and the stored ledger_gym_id."""
    claim_trigger = CLAIM_TRIGGER.read_text()
    assert "p_new.gym_id := public.visual_group_tenant_id(p_new.gym_id)::text" \
        in claim_trigger
    claim_wave = CLAIM_WAVE.read_text()
    assert "o_tenant := public.visual_group_tenant_strict(p_row.gym_id)::text" \
        in claim_wave
    assert "'exact_url', v_scan.o_exact_url" in claim_wave
    text = RECEIPT.read_text()
    assert "v_tenant := public.visual_group_tenant_strict(v_row.gym_id)::text" \
        in text
    assert "where gym_id = v_tenant and group_key = v_group for update" in text
    assert "s.gym_id = v_tenant" in text
    # The raw row key is never used as a ledger/sibling/occupancy key.
    assert "v_row.gym_id and group_key" not in _code(text)
    # The attempt stores the CANONICAL ledger key (token, tenant, ledger id).
    assert "v_token, v_tenant, v_tenant, v_group, v_date" in \
        " ".join(text.split())


def test_static_url_binding_to_row_and_occupancy():
    """Round-2 P1 repair: caller-supplied URLs are bound to the row's actual
    media fields (via the claim stack's own visual_scene_row_delivered_object)
    and to the occupancy row's claim-recorded exact_url."""
    text = RECEIPT.read_text()
    assert "public.visual_scene_row_delivered_object(v_row)" in text
    assert "evidence->>'exact_url'" in text
    assert "source_media_url" in text
    assert "delivered URL is not the row" in text
    assert "source URL does not match the row" in text
    # The bound helper is the claim stack's own function, not a copy.
    assert "create or replace function " \
        "public.visual_scene_row_delivered_object(" in CLAIM_WAVE.read_text()


def test_static_no_live_wiring():
    """Nothing in the app, publisher, or claim-wave calls these RPCs yet;
    the integration contract is documentation-only in this package."""
    pat = re.compile(r"visual_scene_original_use_(prepare|terminate)")
    for path in REPO.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        assert not pat.search(body), f"unexpected wiring in {path}"
    assert "visual_scene_original_use" not in CLAIM_WAVE.read_text()


def test_static_terminate_writes_current_tx_reconciliation_proof():
    """Round-3 P1 repair: terminate writes the visual_group_reconciliation
    receipt the ARMED claim trigger demands for an ambiguous row's
    finalization — in the SAME transaction, AFTER the owner-only
    attestation gate and BEFORE the calendar transition, from validated
    attempt/attestation/row data only. The existing armed trigger is not
    weakened; this package still installs no trigger on content_calendar."""
    text = RECEIPT.read_text()
    code = _code(text)
    for needle in (
        "insert into public.visual_group_reconciliation",
        "'confirmed_published' else 'confirmed_not_sent'",
        "original_claim",
        "hold_event_ids",
        "'calendar_date', v_row.post_date::text",
        "'delivered_url', v_row.image_url",
        "'provider_post_id', v_post",
        "published_at = v_pub_at",
        "'visual_scene_original_use_terminate'",
    ):
        assert needle in code, needle
    gate = code.index("no authoritative provider outcome")
    recon = code.index("insert into public.visual_group_reconciliation")
    publish = code.index("status = 'published', published_at = v_pub_at")
    assert gate < recon < publish, \
        "reconciliation proof must sit between the attestation gate and " \
        "the calendar publication"
    assert "on public.content_calendar" not in code.lower()
    claim = CLAIM_TRIGGER.read_text()
    assert "ambiguous publication requires terminal provider " \
        "reconciliation" in claim


def test_static_prepare_accepts_same_day_published_sibling_ledger():
    """Round-3 P1 repair: prepare accepts the SHARED ledger in 'published'
    state on THIS exact reserved_date (the first sibling's terminal
    publication moved it), while released/other-date/missing ledgers still
    refuse; tenant/date/bytes/occupancy binding is unchanged."""
    text = RECEIPT.read_text()
    assert "v_ledger.state not in ('reserved','published')" in text
    assert "v_ledger.reserved_date is distinct from v_date" in text
    assert "no current ledger reservation" in text


def test_static_expected_provider_channel_binding():
    """Round-3 P2: expected provider (caller-declared send provider) and
    expected channel (the row's account; a missing account refuses) are
    bound immutably at prepare, frozen by the attempt guard, and the
    terminal attestation must match both exactly."""
    text = RECEIPT.read_text()
    for needle in (
        "expected_provider     text not null",
        "expected_channel      text not null",
        "has no channel account",
        "v_att.provider is distinct from v_attempt.expected_provider",
        "v_att.channel is distinct from v_attempt.expected_channel",
        "new.expected_provider is distinct from old.expected_provider",
        "new.expected_channel is distinct from old.expected_channel",
        "row channel account changed since prepare",
        # Round-4 P2: expected provider ACCOUNT id is required, frozen and
        # matched exactly against the terminal attestation.
        "expected_provider_account_id text not null",
        "p->>'provider_account_id'",
        "and v_existing.expected_provider_account_id = v_acct",
        "v_att.provider_account_id",
        "is distinct from v_attempt.expected_provider_account_id",
        "new.expected_provider_account_id",
        "render_receipt, expected_provider, expected_provider_account_id,",
    ):
        assert needle in text, needle


def test_static_round4_ambiguity_fail_closed():
    """Round-4 P1: unresolved historical sibling/hold context is refused
    fail-closed at PREPARE (before the attempt row / any provider send)
    and re-checked at TERMINATE after the attempt+row locks (so ambiguity
    inserted or changed in between cannot be laundered). Reconciliation
    attempts/groups are built ONLY from ambiguous siblings (all validated
    current by the gate); the armed trigger is not weakened."""
    text = RECEIPT.read_text()
    code = _code(text)
    for needle in (
        "create or replace function "
        "public.visual_scene_original_use_check_ambiguity(",
        "not public.visual_group_sibling_reconciled(s)",
        "s.original_claim_token is null",
        "s.original_claim_token is distinct from p_token",
        "s.original_image_url is null",
        "s.original_image_url is distinct from p_image",
        "s.original_provider_post_id is not null",
        "unresolved historical sibling ambiguity",
        "unresolved review_hold without per-claim evidence",
        "e.id = any(c.hold_event_ids)",
        "'publish_claim_token'",
        "'late_post_id'",
        "'post_date'",
        "stays held for manual evidence recovery",
    ):
        assert needle in code, needle
    # prepare gate: BEFORE the attempt insert (pre-send, fail closed).
    prep_gate = code.index(
        "perform public.visual_scene_original_use_check_ambiguity("
        "\n    v_tenant, v_row_id, v_row.post_date, v_token, v_d_url, null)")
    prep_insert = code.index(
        "insert into public.visual_scene_original_use_attempt (")
    assert prep_gate < prep_insert
    # terminate gate: after the token/channel locks, BEFORE the
    # attestation gate and the reconciliation insert.
    term_gate = code.index(
        "perform public.visual_scene_original_use_check_ambiguity("
        "\n    v_attempt.tenant_id, v_row.id, v_row.post_date, v_token,")
    chan = code.index("row channel account changed since prepare")
    att_gate = code.index("no authoritative provider outcome")
    recon = code.index("insert into public.visual_group_reconciliation")
    assert chan < term_gate < att_gate < recon
    # Attempts/groups only from (validated) ambiguous siblings.
    assert code.count(
        "where s.gym_id = v_attempt.tenant_id "
        "and s.calendar_row_id = v_row.id\n      and s.ambiguous") == 2


# ---- adversarial PG scenarios (scratch DB only) ----------------------------

def test_pg_missing_evidence_blocks_before_provider_call(scratch):
    """Missing pre-send evidence => prepare RAISES; NO attempt row exists, so
    there is nothing a sender could authorize from. Fail closed, pre-send."""
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    row_id, token = _claimed_row(tid, group, image_url=url)
    _reserve(tid, group, row_id)
    args, phash = _prepare_args(tid, group, row_id, token, url, fp, rr)
    # No occupancy reservation written:
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args)}'::jsonb)",
           "no current scene occupancy reservation")
    # Occupancy exists but the delivered read receipt id is invented:
    _occupy(tid, group, row_id, fp, phash, url)
    bad = dict(args, delivered_read_receipt=str(uuid.uuid4()))
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(bad)}'::jsonb)",
           "delivered read receipt missing or mismatched")
    # No ledger reservation at all:
    tid2, raw2, group2 = _seed_tenant()
    url2, fp2, rr2 = _object(tid2)
    row2, token2 = _claimed_row(tid2, group2, image_url=url2)
    args2, ph2 = _prepare_args(tid2, group2, row2, token2, url2, fp2, rr2)
    _occupy(tid2, group2, row2, fp2, ph2, url2)
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args2)}'::jsonb)", "no current ledger reservation")
    assert _count("visual_scene_original_use_attempt") == 0
    assert _count("visual_scene_original_use_receipt") == 0


def test_pg_prepare_requires_exact_live_claim_token(scratch):
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ph = _full_setup(tid, raw, group)
    bad = dict(args, claim_attempt_id=str(uuid.uuid4()))
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(bad)}'::jsonb)",
           "claim token does not match the live row token")
    assert _count("visual_scene_original_use_attempt") == 0


def test_pg_published_status_is_not_proof(scratch):
    """A row marked published without a prepared attempt cannot terminate and
    cannot prepare; published status alone never mints a receipt."""
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ph = _full_setup(tid, raw, group)
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set status='published',"
         f" published_at=now() where id='{row_id}'")
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args)}'::jsonb)", "not provider-use proof")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'p1'})}'::jsonb)",
           "never minted without pre-send evidence")
    assert _count("visual_scene_original_use_receipt") == 0


def test_pg_happy_path_mints_exactly_one_receipt(scratch):
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ph = _full_setup(tid, raw, group)
    out = json.loads(_prepare(args))
    assert out["state"] == "prepared" and not out["replayed"]
    att = _attest(args, tid, post="zp_1")
    done = json.loads(_terminate(token, "delivered", "zp_1"))
    assert done["state"] == "confirmed_delivered"
    assert done["receipt_id"] and not done["superseded_by_receipt"]
    assert _count("visual_scene_original_use_receipt") == 1
    assert _one("select attestation_id from "
                "public.visual_scene_original_use_receipt") == att
    got = _one("select status||'|'||coalesce(late_post_id,'')||'|'||"
               "coalesce(publish_claim_token::text,'') "
               f"from public.content_calendar where id='{row_id}'")
    assert got == "published|zp_1|"


def test_pg_token_retry_and_release(scratch):
    """Same-token prepare retry returns the SAME attempt; drifted retry
    conflicts. Attested confirmed_no_send releases the token; a NEW token
    may prepare and its attested delivery mints the (first) receipt."""
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ph = _full_setup(tid, raw, group)
    first = json.loads(_prepare(args))
    again = json.loads(_prepare(args))
    assert again["replayed"] and again["attempt_id"] == first["attempt_id"]
    drift = dict(args, delivered_phash=uuid.uuid4().hex[:16])
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(drift)}'::jsonb)",
           "already prepared with different evidence")
    _attest(args, tid, outcome="confirmed_no_send")
    json.loads(_terminate(token, "confirmed_no_send"))
    assert _one("select coalesce(publish_claim_token::text,'') from "
                f"public.content_calendar where id='{row_id}'") == ""
    assert _count("visual_scene_original_use_receipt") == 0
    # New token, new attempt, attested delivery mints the first receipt.
    new_token = _one("update public.content_calendar set "
                     "publish_claim_token=gen_random_uuid() "
                     f"where id='{row_id}' returning publish_claim_token")
    args2 = dict(args, claim_attempt_id=new_token)
    out2 = json.loads(_prepare(args2))
    assert out2["attempt_id"] != first["attempt_id"]
    _attest(args2, tid, post="zp_retry")
    done = json.loads(_terminate(new_token, "delivered", "zp_retry"))
    assert done["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1


def test_pg_same_day_siblings_shared_occupancy_one_receipt(scratch):
    """Round-2 P1 adversarial: two same-day sibling rows use the IDENTICAL
    object, pHash and ONE shared occupancy row — the occupancy PK
    (phash, tenant, group, used_date) makes a second per-row occupancy row
    impossible by construction. Both prepare (ledger holder + active
    sibling), both deliver with owner attestations: exactly ONE receipt;
    the later delivery is recorded superseded on its own attempt."""
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    row1, token1 = _claimed_row(tid, group, image_url=url)
    _reserve(tid, group, row1)
    args1, phash = _prepare_args(tid, group, row1, token1, url, fp, rr)
    _occupy(tid, group, row1, fp, phash, url)  # ONE occupancy row only
    row2, token2 = _claimed_row(tid, group, image_url=url)
    _sibling(tid, group, row2)
    args2, _ = _prepare_args(tid, group, row2, token2, url, fp, rr,
                             phash=phash)
    json.loads(_prepare(args1))
    out2 = json.loads(_prepare(args2))  # sibling prepares off SHARED occupancy
    assert out2["state"] == "prepared"
    assert _count("visual_scene_phash_occupied") == 1
    _attest(args1, tid, post="zp_a")
    _attest(args2, tid, post="zp_b")
    d1 = json.loads(_terminate(token1, "delivered", "zp_a"))
    d2 = json.loads(_terminate(token2, "delivered", "zp_b"))
    assert _count("visual_scene_original_use_receipt") == 1
    winners = [d for d in (d1, d2) if d.get("receipt_id")]
    losers = [d for d in (d1, d2) if d.get("superseded_by_receipt")]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0]["superseded_by_receipt"] == winners[0]["receipt_id"]
    assert _count("visual_scene_phash_occupied") == 1


def test_pg_swap_before_and_after_claim(scratch):
    """Swap BEFORE claim: no attempt exists; the new staged bytes prepare
    cleanly. Swap AFTER claim (same token, new bytes): refused as evidence
    drift — the prepared snapshot stays bound to the original bytes."""
    tid, raw, group = _seed_tenant()
    url_a, fp_a, rr_a = _object(tid)
    url_b, fp_b, rr_b = _object(tid)
    # Pre-claim swap: the row now stages bytes B; prepare binds B.
    row_id, token = _claimed_row(tid, group, image_url=url_b)
    _reserve(tid, group, row_id)
    args_b, ph_b = _prepare_args(tid, group, row_id, token, url_b, fp_b, rr_b)
    _occupy(tid, group, row_id, fp_b, ph_b, url_b)
    out = json.loads(_prepare(args_b))
    assert out["state"] == "prepared"
    # Post-claim swap attempt: same token, different delivered bytes.
    args_a, _ph_a = _prepare_args(tid, group, row_id, token, url_a, fp_a, rr_a)
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args_a)}'::jsonb)",
           "already prepared with different evidence")
    got = _one("select delivered_md5 from "
               "public.visual_scene_original_use_attempt")
    assert got == fp_b


def test_pg_terminal_replay_and_conflict(scratch):
    tid, raw, group = _seed_tenant()
    _row, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_x")
    first = json.loads(_terminate(token, "delivered", "zp_x"))
    replay = json.loads(_terminate(token, "delivered", "zp_x"))
    assert replay["replayed"] and replay["receipt_id"] == first["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_OTHER'})}'::jsonb)",
           "different")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'confirmed_no_send'})}'::jsonb)",
           "finalized")


def test_pg_terminal_rpc_rollback_retry_converges_one_receipt(scratch):
    """Re-scoped (round-2 P1): what SQL can prove here, and ONLY this — a
    terminal RPC inside a transaction that ROLLS BACK leaves the attempt
    'prepared' with no receipt, and retrying ONLY the terminal RPC converges
    to exactly one receipt. This does NOT prove provider send success or
    failure, authoritative provider readback, or that the application never
    resends: those are application-integration properties this SQL package
    cannot observe, and they remain UNPROVEN (see
    docs/SCENE_ORIGINAL_USE_RECEIPT.md). The package itself contains no
    resend path."""
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_crash")
    payload = json.dumps({"claim_attempt_id": token, "outcome": "delivered",
                          "provider_post_id": "zp_crash"})
    _script("begin;\n"
            f"select public.visual_scene_original_use_terminate('{payload}'::jsonb);\n"
            "rollback;\n")
    assert _one("select state from public.visual_scene_original_use_attempt") \
        == "prepared"
    assert _count("visual_scene_original_use_receipt") == 0
    # Terminal RPC retry (never a provider resend) succeeds once.
    done = json.loads(_terminate(token, "delivered", "zp_crash"))
    assert done["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1


def test_pg_unknown_outcome_stays_held(scratch):
    tid, raw, group = _seed_tenant()
    _row, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'unknown'})}'::jsonb)",
           "unknown outcomes stay held")
    assert _one("select state from public.visual_scene_original_use_attempt") \
        == "prepared"
    assert _count("visual_scene_original_use_receipt") == 0


def test_pg_historical_unknown_stays_blocked(scratch):
    """A ledger row with no prepared attempt (historical/unknown) can never
    acquire a receipt: terminate refuses unknown tokens, and prepare refuses
    a published-ledger row."""
    tid, raw, group = _seed_tenant()
    row_id = _one("insert into public.content_calendar"
                  "(gym_id, post_date, status, published_at,"
                  " visual_group_key) values "
                  f"('{tid}', '2026-09-01', 'published', now(), '{group}')"
                  " returning id")
    _sql("insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, calendar_row_id, state,"
         " published_at) values "
         f"('{tid}', '{group}', '2026-09-01', '{row_id}', 'published',"
         " now())")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': str(uuid.uuid4()), 'outcome': 'delivered', 'provider_post_id': 'zp_h'})}'::jsonb)",
           "never minted without pre-send evidence")
    assert _count("visual_scene_original_use_receipt") == 0


def test_pg_deletion_retention(scratch):
    """Deleting the calendar row after terminal delivery MUST NOT remove the
    attempt, attestation or receipt (evidence is retained independently of
    the row)."""
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_del")
    json.loads(_terminate(token, "delivered", "zp_del"))
    _sql("set session_replication_role = replica; "
         f"delete from public.content_calendar where id='{row_id}'")
    assert _count("visual_scene_original_use_attempt") == 1
    assert _count("visual_scene_original_use_attestation") == 1
    assert _count("visual_scene_original_use_receipt") == 1


def test_pg_receipts_attempts_attestations_immutable(scratch):
    tid, raw, group = _seed_tenant()
    _row, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_imm")
    json.loads(_terminate(token, "delivered", "zp_imm"))
    _fails("update public.visual_scene_original_use_receipt set "
           "provider_post_id='x'", "append-only")
    _fails("delete from public.visual_scene_original_use_receipt",
           "append-only")
    _fails("truncate public.visual_scene_original_use_receipt",
           "append-only")
    _fails("delete from public.visual_scene_original_use_attempt",
           "append-only")
    _fails("truncate public.visual_scene_original_use_attempt cascade",
           "append-only")
    _fails("update public.visual_scene_original_use_attempt set "
           "delivered_md5='md5:" + "0" * 32 + "'", "final")
    _fails("update public.visual_scene_original_use_attestation set "
           "provider='x'", "append-only")
    _fails("delete from public.visual_scene_original_use_attestation",
           "append-only")
    _fails("truncate public.visual_scene_original_use_attestation cascade",
           "append-only")


def test_pg_internal_ambiguity_helper_denies_external_roles(scratch):
    signature = (
        "public.visual_scene_original_use_check_ambiguity("
        "text,uuid,date,uuid,text,text)"
    )
    for role in ("anon", "authenticated", "service_role"):
        assert _one(
            f"select has_function_privilege('{role}', '{signature}', 'EXECUTE')"
        ) == "f"
        _fails(
            f"set role {role}; select {signature.split('(')[0]}("
            "'tenant', '00000000-0000-0000-0000-000000000001'::uuid, "
            "'2026-09-01'::date, "
            "'00000000-0000-0000-0000-000000000002'::uuid, "
            "'https://example.invalid/photo.jpg', null)",
            "permission denied",
        )


def test_pg_service_role_cannot_forge_delivery(scratch):
    """Round-2 P1: service_role holds EXECUTE on the RPCs but cannot mint a
    false receipt: the terminal RPC fails closed without an owner-written
    attestation, and service_role cannot write the attestation (or the
    attempt/receipt tables) at all. The privilege contract is checked
    against the database catalog, not this file's claims."""
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ph = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    forged = json.dumps({"claim_attempt_id": token, "outcome": "delivered",
                         "provider_post_id": "zp_FORGED",
                         "outcome_evidence": {}})
    # 1. Public-RPC forgery with {} evidence and no attestation: refused.
    _fails(f"set role service_role;\n"
           f"select public.visual_scene_original_use_terminate"
           f"('{forged}'::jsonb)",
           "no authoritative provider outcome attestation")
    assert _count("visual_scene_original_use_receipt") == 0
    # 2. Direct attestation forgery as service_role: denied.
    _fails(f"set role service_role;\n"
           f"insert into public.visual_scene_original_use_attestation"
           f"(claim_attempt_id, tenant_id, calendar_row_id, outcome,"
           f" provider, provider_account_id, channel, provider_post_id,"
           f" delivered_url, delivered_md5, delivered_phash,"
           f" readback_evidence, attested_by) values "
           f"('{token}', '{tid}', '{row_id}', 'delivered', 'zernio',"
           f" 'acct_x', 'scratch-ig', 'zp_FORGED', '{args['delivered_url']}',"
           f" '{args['delivered_md5']}', '{args['delivered_phash']}',"
           f" '{{\"forged\": true}}'::jsonb, 'service_role')",
           "permission denied")
    # 3. Direct receipt forgery as service_role: denied.
    _fails("set role service_role;\n"
           "insert into public.visual_scene_original_use_receipt"
           "(ledger_gym_id, group_key, used_date, attempt_id,"
           " claim_attempt_id, attestation_id, tenant_id, calendar_row_id,"
           " delivered_url, delivered_md5, delivered_phash, provider_post_id)"
           " values ('x', 'y', '2026-09-01', gen_random_uuid(),"
           " gen_random_uuid(), gen_random_uuid(), 'x', gen_random_uuid(),"
           " 'https://scratch.example/x.jpg', 'md5:" + "0" * 32 + "', '"
           + "0" * 16 + "', 'zp')",
           "permission denied")
    # 4. Catalog privilege contract (schema, not behavior self-assertion).
    for tbl in ("visual_scene_original_use_attempt",
                "visual_scene_original_use_receipt",
                "visual_scene_original_use_attestation"):
        for priv in ("insert", "update", "delete"):
            assert _one(f"select has_table_privilege('service_role', "
                        f"'public.{tbl}', '{priv}')") == "f", (tbl, priv)
        assert _one(f"select has_table_privilege('service_role', "
                    f"'public.{tbl}', 'select')") == "t", tbl
    # 5. With a genuine owner-written attestation the SAME service_role call
    #    succeeds: the gate is the attestation, not the caller's label.
    _attest(args, tid, post="zp_real")
    payload = json.dumps({"claim_attempt_id": token, "outcome": "delivered",
                          "provider_post_id": "zp_real"})
    out = _script("set role service_role;\n"
                  f"select public.visual_scene_original_use_terminate"
                  f"('{payload}'::jsonb);\nreset role;\n").stdout.strip()
    done = json.loads(out.splitlines()[-1])
    assert done["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1


def test_pg_terminate_requires_matching_attestation(scratch):
    """Round-2 P1: the attestation must match the exact token, tenant, row,
    outcome, provider post id, channel and delivered byte lineage; empty
    readback evidence cannot be written at all."""
    # (a) delivered with NO attestation: refused (fail closed).
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_a'})}'::jsonb)",
           "no authoritative provider outcome attestation")
    # (b) confirmed_no_send with NO attestation: refused (fail closed).
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'confirmed_no_send'})}'::jsonb)",
           "no authoritative provider outcome attestation")
    # (c) provider_post_id mismatch: refused.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_a")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_b'})}'::jsonb)",
           "attestation does not match")
    # (d) delivered byte lineage drift (URL): refused.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_a",
            url=f"https://scratch.example/{uuid.uuid4().hex}.jpg")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_a'})}'::jsonb)",
           "attestation does not match")
    # (e) outcome mismatch (attested no_send, caller claims delivered).
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, outcome="confirmed_no_send")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_a'})}'::jsonb)",
           "attestation does not match")
    # (f) channel mismatch against the row's account: refused.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group, account="ig_a")
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_a", channel="ig_b")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_a'})}'::jsonb)",
           "attestation does not match")
    # (g) empty readback evidence cannot even be written.
    _fails(f"insert into public.visual_scene_original_use_attestation"
           f"(claim_attempt_id, tenant_id, calendar_row_id, outcome,"
           f" provider, provider_account_id, channel, provider_post_id,"
           f" delivered_url, delivered_md5, delivered_phash,"
           f" readback_evidence, attested_by) values "
           f"('{token}', '{tid}', '{row_id}', 'delivered', 'zernio',"
           f" 'acct_1', 'ig_a', 'zp_a', '{args['delivered_url']}',"
           f" '{args['delivered_md5']}', '{args['delivered_phash']}',"
           f" '{{}}'::jsonb, 'scratch-attester')",
           "readback_evidence_check")
    assert _count("visual_scene_original_use_receipt") == 0


def test_pg_raw_alias_row_canonical_ledger_keys(scratch):
    """Round-2 P1: the calendar row carries a RAW alias gym key while the
    claim stack keys groups/ledger/siblings/occupancy by the CANONICAL
    tenant. prepare resolves canonical keys for lookup AND store, exactly
    like the claim flow."""
    tid, raw, group = _seed_tenant(raw="raw-gym-alias-001")
    assert raw != tid
    row_id, token, args, _ = _full_setup(tid, raw, group, gym=raw)
    out = json.loads(_prepare(args))
    assert out["state"] == "prepared"
    assert _one("select ledger_gym_id from "
                "public.visual_scene_original_use_attempt") == tid
    _attest(args, tid, post="zp_alias")
    done = json.loads(_terminate(token, "delivered", "zp_alias"))
    assert done["receipt_id"]
    assert _one("select ledger_gym_id from "
                "public.visual_scene_original_use_receipt") == tid


def test_pg_raw_alias_ledger_under_alias_key_refuses(scratch):
    """Adversarial inverse: a ledger row keyed by the RAW alias (NOT the
    claim flow's canonical key) must NOT be found — prepare looks up the
    canonical key only."""
    tid, raw, group = _seed_tenant(raw="raw-gym-alias-002")
    url, fp, rr = _object(tid)
    row_id, token = _claimed_row(tid, group, gym=raw, image_url=url)
    args, phash = _prepare_args(tid, group, row_id, token, url, fp, rr)
    _occupy(tid, group, row_id, fp, phash, url)
    # A group + ledger keyed by the RAW alias (wrong key universe):
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{raw}', '{group}')")
    _reserve(raw, group, row_id)
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args)}'::jsonb)", "no current ledger reservation")
    assert _count("visual_scene_original_use_attempt") == 0


def test_pg_caller_urls_bound_to_row_and_occupancy(scratch):
    """Round-2 P1: caller-supplied URLs cannot diverge from the row's actual
    media fields or the occupancy row's claim-recorded exact_url while the
    md5 matches."""
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    row_id, token = _claimed_row(tid, group, image_url=url)
    _reserve(tid, group, row_id)
    args, phash = _prepare_args(tid, group, row_id, token, url, fp, rr)
    _occupy(tid, group, row_id, fp, phash, url)
    evil = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    # (a) delivered_url differs from the row's actual image_url (same md5):
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(dict(args, delivered_url=evil))}'::jsonb)",
           "delivered URL is not the row")
    # (b) delivered_url matches the row but NOT occupancy's exact_url:
    _sql("set session_replication_role = replica; "
         "update public.visual_scene_phash_occupied set evidence = "
         f"jsonb_build_object('candidate_id', '{uuid.uuid4()}'::uuid,"
         f" 'exact_url', '{evil}')")
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args)}'::jsonb)",
           "no current scene occupancy reservation")
    _sql("set session_replication_role = replica; "
         "update public.visual_scene_phash_occupied set evidence = "
         f"jsonb_build_object('candidate_id', '{uuid.uuid4()}'::uuid,"
         f" 'exact_url', '{url}')")
    # (c) source_url differs from the row's recorded source_media_url:
    _sql("set session_replication_role = replica; "
         f"update public.content_calendar set source_media_url='{evil}'"
         f" where id='{row_id}'")
    bad_source = dict(args, source_url=f"https://scratch.example/"
                      f"{uuid.uuid4().hex}.jpg")
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(bad_source)}'::jsonb)",
           "source URL does not match the row")
    # (d) no source_media_url recorded + distinct rendered source: refused.
    tid2, raw2, group2 = _seed_tenant()
    url2, fp2, rr2 = _object(tid2)          # delivered object
    s_url2 = f"https://scratch.example/{uuid.uuid4().hex}.png"
    s_fp2 = "md5:" + uuid.uuid4().hex
    s_rr2 = _read_receipt(tid2, s_url2, s_fp2)   # distinct source, NO row field
    row2, token2 = _claimed_row(tid2, group2, image_url=url2)
    _reserve(tid2, group2, row2)
    args2 = {"claim_attempt_id": token2, "calendar_row_id": row2,
             "group_key": group2, "used_date": "2026-09-01",
             "provider": "zernio", "provider_account_id": "acct_1",
             "source_url": s_url2, "source_md5": s_fp2,
             "delivered_url": url2, "delivered_md5": fp2,
             "delivered_phash": (ph2 := uuid.uuid4().hex[:16]),
             "source_read_receipt": s_rr2, "delivered_read_receipt": rr2}
    _occupy(tid2, group2, row2, fp2, ph2, url2)
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args2)}'::jsonb)", "no source_media_url")
    assert _count("visual_scene_original_use_attempt") == 0


def test_pg_rendered_source_chain_with_row_binding(scratch):
    """Positive rendered case: bytes differ, the source is bound to the
    row's source_media_url, delivered to image_url + occupancy exact_url,
    and a render receipt chains exactly the two owner reads."""
    tid, raw, group = _seed_tenant()
    s_url = f"https://scratch.example/{uuid.uuid4().hex}.png"
    s_fp = "md5:" + uuid.uuid4().hex
    s_rr = _read_receipt(tid, s_url, s_fp)
    d_url, d_fp, d_rr = _object(tid)
    render = _one("insert into public.visual_global_render_receipt"
                  "(tenant_id, source_read_receipt, delivered_read_receipt,"
                  " source_exact_url, delivered_exact_url, source_fingerprint,"
                  " delivered_fingerprint, operation, evidence_ref,"
                  " rendered_by) values "
                  f"('{tid}', '{s_rr}', '{d_rr}', '{s_url}', '{d_url}',"
                  f" '{s_fp}', '{d_fp}', 'render', 'scratch-evidence',"
                  " 'receipt-test') returning receipt_id")
    row_id, token = _claimed_row(tid, group, image_url=d_url)
    _sql("set session_replication_role = replica; "
         f"update public.content_calendar set source_media_url='{s_url}'"
         f" where id='{row_id}'")
    _reserve(tid, group, row_id)
    args = {"claim_attempt_id": token, "calendar_row_id": row_id,
            "group_key": group, "used_date": "2026-09-01",
            "provider": "zernio", "provider_account_id": "acct_1",
            "source_url": s_url, "source_md5": s_fp,
            "delivered_url": d_url, "delivered_md5": d_fp,
            "delivered_phash": (ph := uuid.uuid4().hex[:16]),
            "source_read_receipt": s_rr, "delivered_read_receipt": d_rr,
            "render_receipt": render}
    _occupy(tid, group, row_id, d_fp, ph, d_url)
    out = json.loads(_prepare(args))
    assert out["state"] == "prepared"
    _attest(args, tid, post="zp_render")
    done = json.loads(_terminate(token, "delivered", "zp_render"))
    assert done["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1


# ---- ARMED-enforcement integration scenarios (round-3 P1/P2) ----------------

def test_pg_armed_terminate_writes_current_tx_reconciliation(scratch):
    """Round-3 P1: with enforcement ARMED, the first terminal publication
    succeeds ONLY because terminate writes the current-transaction
    visual_group_reconciliation receipt after the attestation gate. The
    ARMED claim trigger itself moves the shared ledger to 'published'
    (terminate's SQL never touches the ledger), which also proves the
    trigger — not the RPC — owns the ledger transition."""
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    phash = uuid.uuid4().hex[:16]
    _global_object(tid, group, url, fp, rr)
    _arm(tid)
    row_id, token = _armed_claim(tid, group, url, holder=True)
    _occupy(tid, group, row_id, fp, phash, url, replica=True)
    args, _ = _prepare_args(tid, group, row_id, token, url, fp, rr,
                            phash=phash)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_armed")
    done = json.loads(_terminate(token, "delivered", "zp_armed"))
    assert done["state"] == "confirmed_delivered" and done["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1
    got = _one("select status||'|'||coalesce(late_post_id,'')||'|'||"
               "coalesce(publish_claim_token::text,'')||'|'||"
               "(published_at is not null)::text "
               f"from public.content_calendar where id='{row_id}'")
    assert got == "published|zp_armed||true"
    # The ARMED TRIGGER moved the shared ledger; terminate's SQL did not.
    assert _one("select state from public.visual_group_usage_ledger") \
        == "published"
    # Exactly one current-tx reconciliation receipt, exact binding.
    assert _count("visual_group_reconciliation") == 1
    rec = _one("select outcome||'|'||coalesce(delivered_group_key,'')||'|'||"
               "coalesce(evidence->>'provider_post_id','')||'|'||"
               "coalesce(evidence->>'delivered_url','')||'|'||"
               "coalesce(evidence->>'calendar_date','')||'|'||actor "
               "from public.visual_group_reconciliation")
    assert rec == (f"confirmed_published|{group}|zp_armed|{url}|"
                   "2026-09-01|visual_scene_original_use_terminate")
    # The ambiguous sibling attempt is covered by the receipt (reconciled).
    assert _one("select public.visual_group_sibling_reconciled(s) "
                "from public.visual_group_usage_sibling s") == "t"
    # Load-bearing: a DIRECT marker update on another armed token-holding
    # row (no attestation, no terminate) still fails — the trigger is
    # armed and the RPC's success was the proof, not a toothless trigger.
    row2, _token2 = _armed_claim(tid, group, url, account="fb_a")
    _fails(f"update public.content_calendar set status='published',"
           f" published_at=now() where id='{row2}'",
           "ambiguous publication requires terminal provider "
           "reconciliation")
    assert _one("select status from public.content_calendar "
                f"where id='{row2}'") == "publishing"
    # Forgery: service_role can never write the reconciliation receipt.
    _fails("set role service_role;\n"
           "insert into public.visual_group_reconciliation(gym_id,"
           " calendar_row_id, outcome, delivered_group_key,"
           " reserved_groups, attempts, hold_event_ids, original_claim,"
           " evidence, actor) values "
           f"('{tid}', '{row2}', 'confirmed_published', '{group}',"
           f" '{{{group}}}', '[]'::jsonb, '{{}}'::bigint[], '{{}}'::jsonb,"
           " '{\"calendar_date\":\"2026-09-01\"}'::jsonb, 'forged')",
           "permission denied")


def test_pg_armed_missing_attestation_cannot_publish(scratch):
    """Round-3 P1 (armed fail-closed): with enforcement ARMED, a terminal
    delivery claim WITHOUT an owner attestation raises BEFORE any
    reconciliation receipt or calendar transition — the row stays
    unpublished, the ledger stays reserved, no receipt, no reconciliation."""
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    phash = uuid.uuid4().hex[:16]
    _global_object(tid, group, url, fp, rr)
    _arm(tid)
    row_id, token = _armed_claim(tid, group, url, holder=True)
    _occupy(tid, group, row_id, fp, phash, url, replica=True)
    args, _ = _prepare_args(tid, group, row_id, token, url, fp, rr,
                            phash=phash)
    json.loads(_prepare(args))
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_forge'})}'::jsonb)",
           "no authoritative provider outcome attestation")
    assert _one("select status from public.content_calendar "
                f"where id='{row_id}'") == "publishing"
    assert _one("select state from public.visual_group_usage_ledger") \
        == "reserved"
    assert _count("visual_scene_original_use_receipt") == 0
    assert _count("visual_group_reconciliation") == 0
    # Same for the no-send outcome: no attestation, no reconciliation row.
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'confirmed_no_send'})}'::jsonb)",
           "no authoritative provider outcome attestation")
    assert _count("visual_group_reconciliation") == 0
    # Positive armed no-send: the attested authoritative absence writes the
    # confirmed_not_sent reconciliation, releases the token and leaves the
    # row unpublished with the ledger still reserved.
    _attest(args, tid, outcome="confirmed_no_send")
    done = json.loads(_terminate(token, "confirmed_no_send"))
    assert done["state"] == "confirmed_no_send"
    assert _one("select coalesce(publish_claim_token::text,'')||'|'||status "
                f"from public.content_calendar where id='{row_id}'") \
        == "|publishing"
    assert _one("select outcome||'|'||coalesce(delivered_group_key,'') "
                "from public.visual_group_reconciliation") \
        == "confirmed_not_sent|"
    assert _count("visual_scene_original_use_receipt") == 0
    assert _one("select state from public.visual_group_usage_ledger") \
        == "reserved"


def test_pg_armed_first_sibling_terminal_then_second_prepares(scratch):
    """Round-3 P1 (armed): the FIRST sibling's terminal publication moves
    the SHARED ledger to 'published'; a lawful LATER same-day channel
    sibling must still prepare off that published same-date ledger, then
    terminate superseded. Wrong-date and wrong-tenant claims still refuse
    at the ledger gate."""
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    phash = uuid.uuid4().hex[:16]
    _global_object(tid, group, url, fp, rr)
    _arm(tid)
    row1, token1 = _armed_claim(tid, group, url, account="ig_a",
                                holder=True)
    _occupy(tid, group, row1, fp, phash, url, replica=True)  # ONE shared row
    row2, token2 = _armed_claim(tid, group, url, account="fb_a")
    args1, _ = _prepare_args(tid, group, row1, token1, url, fp, rr,
                             phash=phash)
    args2, _ = _prepare_args(tid, group, row2, token2, url, fp, rr,
                             phash=phash)
    json.loads(_prepare(args1))
    _attest(args1, tid, post="zp_1", channel="ig_a")
    d1 = json.loads(_terminate(token1, "delivered", "zp_1"))
    assert d1["receipt_id"]
    assert _one("select state from public.visual_group_usage_ledger") \
        == "published"
    # Regression: previously raised 'no current ledger reservation' here.
    out2 = json.loads(_prepare(args2))
    assert out2["state"] == "prepared" and not out2["replayed"]
    _attest(args2, tid, post="zp_2", channel="fb_a")
    d2 = json.loads(_terminate(token2, "delivered", "zp_2"))
    assert d2["superseded_by_receipt"] == d1["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1
    assert _count("visual_group_reconciliation") == 2
    # Wrong DATE: the shared ledger sits published on 2026-09-01; a
    # 2026-09-02 claim on the same group/tenant refuses at the date gate.
    row3, token3 = _armed_claim(tid, group, url, used_date="2026-09-02",
                                account="story_a")
    args3 = dict(args1, claim_attempt_id=token3, calendar_row_id=row3,
                 used_date="2026-09-02")
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args3)}'::jsonb)",
           "no current ledger reservation")
    # Wrong TENANT: same group key, same date, different tenant has NO
    # ledger event — the canonical lookup must not find tenant A's ledger.
    tid2, _raw2, _group2 = _seed_tenant()
    row_t2, token_t2 = _one(
        "set session_replication_role = replica; "
        "insert into public.content_calendar(gym_id, post_date, status,"
        " publish_claim_token, visual_group_key, image_url, account)"
        f" values ('{tid2}', '2026-09-01', 'publishing', gen_random_uuid(),"
        f" '{group}', '{url}', 'ig_b') "
        "returning id::text || '|' || publish_claim_token::text").split("|")
    args_t2 = dict(args1, claim_attempt_id=token_t2,
                   calendar_row_id=row_t2)
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args_t2)}'::jsonb)",
           "no current ledger reservation")
    assert _count("visual_scene_original_use_attempt") == 2


def test_pg_expected_provider_channel_binding(scratch):
    """Round-3 P2: the attestation must match the prepare-bound expected
    provider and channel exactly; a row with no channel account cannot
    prepare; channel drift between prepare and terminate refuses."""
    # (a) provider mismatch: prepared for 'meta', attested 'zernio'.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    args = dict(args, provider="meta")
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_p", provider="zernio")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_p'})}'::jsonb)",
           "attestation does not match")
    # (b) matching provider/channel attestation succeeds (positive gate).
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    args = dict(args, provider="meta")
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_p", provider="meta", channel="scratch-ig")
    done = json.loads(_terminate(token, "delivered", "zp_p"))
    assert done["receipt_id"]
    # (c) missing channel account: prepare refuses, no attempt exists.
    tid, raw, group = _seed_tenant()
    url, fp, rr = _object(tid)
    row_id, token = _claimed_row(tid, group, image_url=url, account=None)
    _reserve(tid, group, row_id)
    args, phash = _prepare_args(tid, group, row_id, token, url, fp, rr)
    _occupy(tid, group, row_id, fp, phash, url)
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args)}'::jsonb)", "no channel account")
    assert _one("select count(*) from "
                "public.visual_scene_original_use_attempt "
                f"where claim_attempt_id='{token}'") == "0"
    # (d) channel drift AFTER prepare: attestation matches the immutable
    # binding, but the row's account changed — terminate refuses.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group, account="ig_a")
    json.loads(_prepare(args))
    _sql("set session_replication_role = replica; "
         "update public.content_calendar set account='ig_b' "
         f"where id='{row_id}'")
    _attest(args, tid, post="zp_d", channel="ig_a")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_d'})}'::jsonb)",
           "row channel account changed since prepare")
def _armed_valid_setup(group="grp_r4"):
    """Armed tenant with a provably CURRENT ambiguous sibling (exact live
    token + exact current image), occupancy and owner byte evidence —
    the valid path the round-4 gate must preserve."""
    tid, raw, grp = _seed_tenant()
    url, fp, rr = _object(tid)
    phash = uuid.uuid4().hex[:16]
    _global_object(tid, grp, url, fp, rr)
    _arm(tid)
    row_id, token = _armed_claim(tid, grp, url, holder=True)
    _occupy(tid, grp, row_id, fp, phash, url, replica=True)
    args, _ = _prepare_args(tid, grp, row_id, token, url, fp, rr,
                            phash=phash)
    return tid, grp, url, row_id, token, args


def test_pg_prepare_refuses_unresolved_historical_sibling(scratch):
    """Round-4 P1 adversarial (PRE-SEND): an unreconciled ambiguous sibling
    whose claim token is older/different, NULL, whose original image is
    stale (old image B) or missing, or which carries an old provider post
    (P-old) refuses PREPARE — BEFORE the attempt row exists, so there is
    nothing a sender could authorize from."""
    for label, kw in (
        ("older/different token U", {"token": "old"}),
        ("NULL token", {"token": None}),
        ("old image B", {"token": "current", "image": "set"}),
        ("missing image", {"token": "current", "image": None}),
        ("old provider post P-old",
         {"token": "current", "image": "current", "post": "p-old"}),
    ):
        tid, grp, url, row_id, token, args = _armed_valid_setup()
        kw = dict(kw)
        if kw.get("token") == "current":
            kw["token"] = token
        if kw.get("image") == "current":
            kw["image"] = url
        _hist_sibling(tid, grp + "_hist", row_id, **kw)
        _fails(f"select public.visual_scene_original_use_prepare"
               f"('{json.dumps(args)}'::jsonb)",
               "unresolved historical sibling ambiguity")
        # No attempt before send:
        assert _one("select count(*) from "
                    "public.visual_scene_original_use_attempt") == "0", label
        assert _count("visual_scene_original_use_receipt") == 0, label
        assert _count("visual_group_reconciliation") == 0, label


def test_pg_prepare_refuses_review_hold_without_claim_evidence(scratch):
    """Round-4 P1 adversarial (PRE-SEND): an unresolved review_hold with no
    per-claim evidence (non-JSON / NULL reason, or JSON without the exact
    current claim token / current image) refuses PREPARE; it must NOT be
    copied into a reconciliation as cleared."""
    for label, reason in (
        ("NULL reason", None),
        ("non-JSON reason", "operator note, no claim evidence"),
        ("JSON without claim token", {"note": "no token"}),
        ("older/different token",
         {"publish_claim_token": str(uuid.uuid4()),
          "image_url": "placeholder"}),
    ):
        tid, grp, url, row_id, token, args = _armed_valid_setup()
        if isinstance(reason, dict) and reason.get("image_url") ==                 "placeholder":
            reason = {"publish_claim_token": str(uuid.uuid4()),
                      "image_url": url}
        _hold(tid, row_id, reason)
        _fails(f"select public.visual_scene_original_use_prepare"
               f"('{json.dumps(args)}'::jsonb)",
               "unresolved review_hold without per-claim evidence")
        assert _one("select count(*) from "
                    "public.visual_scene_original_use_attempt") == "0", label


def test_pg_terminate_recheck_refuses_late_or_changed_ambiguity(scratch):
    """Round-4 P1 adversarial (TERMINAL recheck after locks): ambiguity
    INSERTED or CHANGED between prepare and terminate cannot be laundered
    — terminate raises and commits NO receipt, NO reconciliation, NO
    publication, NO historical clearance; the attempt stays prepared."""
    # (a) adversarial sibling inserted AFTER a valid prepare.
    tid, grp, url, row_id, token, args = _armed_valid_setup()
    json.loads(_prepare(args))
    _hist_sibling(tid, grp + "_hist", row_id, token="old")
    _attest(args, tid, post="zp_late")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_late'})}'::jsonb)",
           "unresolved historical sibling ambiguity")
    assert _count("visual_scene_original_use_receipt") == 0
    assert _count("visual_group_reconciliation") == 0
    assert _one("select status from public.content_calendar "
                f"where id='{row_id}'") == "publishing"
    assert _one("select state from public.visual_scene_original_use_attempt"
                ) == "prepared"
    assert _one("select state from public.visual_group_usage_ledger "
                f"where group_key='{grp}'") == "reserved"
    # The historical sibling was NOT cleared by inclusion:
    assert _one("select public.visual_group_sibling_reconciled(s) from "
                "public.visual_group_usage_sibling s where s.group_key="
                f"'{grp}_hist'") == "f"

    # (b) the CURRENT sibling's original image CHANGED after prepare.
    tid, grp, url, row_id, token, args = _armed_valid_setup()
    json.loads(_prepare(args))
    _sql("set session_replication_role = replica; "
         "update public.visual_group_usage_sibling set original_image_url="
         f"'https://scratch.example/{uuid.uuid4().hex}.jpg' "
         f"where gym_id='{tid}' and group_key='{grp}'")
    _attest(args, tid, post="zp_chg")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_chg'})}'::jsonb)",
           "unresolved historical sibling ambiguity")
    assert _count("visual_scene_original_use_receipt") == 0
    assert _count("visual_group_reconciliation") == 0
    assert _one("select status from public.content_calendar "
                f"where id='{row_id}'") == "publishing"

    # (c) hold without per-claim evidence inserted AFTER a valid prepare.
    tid, grp, url, row_id, token, args = _armed_valid_setup()
    json.loads(_prepare(args))
    _hold(tid, row_id, "operator note, no claim evidence")
    _attest(args, tid, post="zp_hold")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_hold'})}'::jsonb)",
           "unresolved review_hold without per-claim evidence")
    assert _count("visual_scene_original_use_receipt") == 0
    assert _count("visual_group_reconciliation") == 0
    assert _one("select status from public.content_calendar "
                f"where id='{row_id}'") == "publishing"


def test_pg_current_sibling_and_current_hold_are_covered(scratch):
    """Round-4 P1 (valid path preserved): an ambiguous sibling with the
    EXACT current token and EXACT current image, and a review_hold whose
    per-claim evidence matches the current token/image/date, are covered
    by the terminal reconciliation; the receipt mints and the armed
    trigger accepts the publication."""
    tid, grp, url, row_id, token, args = _armed_valid_setup()
    hold_id = _hold(tid, row_id, {"publish_claim_token": token,
                                  "image_url": url,
                                  "post_date": "2026-09-01"})
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_cov")
    done = json.loads(_terminate(token, "delivered", "zp_cov"))
    assert done["state"] == "confirmed_delivered" and done["receipt_id"]
    assert _count("visual_scene_original_use_receipt") == 1
    assert _one("select public.visual_group_sibling_reconciled(s) from "
                "public.visual_group_usage_sibling s where s.group_key="
                f"'{grp}'") == "t"
    assert _one(f"select '{hold_id}'::bigint = any(hold_event_ids) from "
                "public.visual_group_reconciliation") == "t"
    assert _one("select status from public.content_calendar "
                f"where id='{row_id}'") == "published"


def test_pg_expected_provider_account_binding(scratch):
    """Round-4 P2: expected_provider_account_id is REQUIRED and non-empty
    at prepare, frozen by the attempt guard (drifted replay conflicts),
    and the terminal attestation's provider_account_id must match it
    EXACTLY; a matching attestation succeeds."""
    # (a) missing key: refused pre-send, no attempt.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    del args["provider_account_id"]
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(args)}'::jsonb)",
           "missing required evidence fields")
    # (b) empty / blank value: refused.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    for bad in ("", "   "):
        _fails(f"select public.visual_scene_original_use_prepare"
               f"('{json.dumps(dict(args, provider_account_id=bad))}'::jsonb)",
               "missing required evidence fields")
    assert _count("visual_scene_original_use_attempt") == 0
    # (c) replay drift on the account id conflicts (immutably bound).
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _fails(f"select public.visual_scene_original_use_prepare"
           f"('{json.dumps(dict(args, provider_account_id='acct_2'))}'::jsonb)",
           "already prepared with different evidence")
    # (d) attestation account mismatch: refused.
    _attest(args, tid, post="zp_acct", account_id="acct_other")
    _fails(f"select public.visual_scene_original_use_terminate"
           f"('{json.dumps({'claim_attempt_id': token, 'outcome': 'delivered', 'provider_post_id': 'zp_acct'})}'::jsonb)",
           "attestation does not match")
    # (e) exact match succeeds.
    tid, raw, group = _seed_tenant()
    row_id, token, args, _ = _full_setup(tid, raw, group)
    json.loads(_prepare(args))
    _attest(args, tid, post="zp_acct", account_id="acct_1")
    done = json.loads(_terminate(token, "delivered", "zp_acct"))
    assert done["receipt_id"]
    assert _one("select expected_provider_account_id from "
                "public.visual_scene_original_use_attempt") == "acct_1"
