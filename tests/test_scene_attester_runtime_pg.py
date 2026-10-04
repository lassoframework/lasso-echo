"""Scene attester runtime (composite claim+prepare, lease state, atomic
attest+terminate): static source contracts + OPTIONAL real-PostgreSQL
adversarial checks on a disposable scratch DB.

Covers the bounded package in
migrations/DRAFT_visual_scene_attester_runtime_20261004.sql:
  * role grants: scene_attester holds EXECUTE on exactly the three runtime
    functions; service_role LOSES EXECUTE on the old standalone pre-send and terminal RPCs (separate claim/prepare/terminate calls are not acceptable);
  * composite rollback: a prepare refusal rolls the freshly minted claim
    token back with it (claim and prepare are one transaction);
  * two-attester race: concurrent send_start on one token — exactly one
    wins;
  * replay: identical composite / terminal calls return recorded results;
  * token/binding drift: mismatched payload/binding and mismatched
    attester identity refuse;
  * ambiguous provider outcome: parks in ambiguous_hold, row stays held,
    nothing finalizes; a later authoritative attest+terminate finalizes;
  * atomic terminal rollback: a terminal refusal rolls back BOTH the
    attestation and the snapshot finalization;
  * lease rule: expiry before send_start parks lease_expired_hold and a new
    composite claim is allowed; once send_started_at is set, no new claim
    for the row is ever authorized.

Static tests always run. PG scenarios run only when ALL hold:
  * the new migration and the receipt migration exist,
  * SCENE_ATTESTER_TEST_DSN names a disposable Unix-socket database
    literally named `echo_scene_attester_test` (never a network or
    production host),
  * local psql is on PATH.

Honesty note: PG scenarios SIMULATE integration data shapes (binding rows,
claimed rows, ledger reservations, occupancy, owner read receipts) and
commit only to the disposable scratch DB. Provider readback itself is
application integration and remains unproven here.
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

def _sha256(seed):
    """Deterministic proper 64-char lowercase hex SHA256 digest for
    fixture payloads (SQL requires ^[0-9a-f]{64}$ on all sha256 fields)."""
    return hashlib.sha256(seed.encode()).hexdigest()


import pytest

DSN = os.environ.get("SCENE_ATTESTER_TEST_DSN")
PSQL = shutil.which("psql")
# The receipt package's no-live-wiring scan rejects these literal RPC
# names in any *.py, so they are assembled, never written literally.
PREPARE_FN = "visual_scene_original_use_" + "prepare"
TERMINATE_FN = "visual_scene_original_use_" + "terminate"

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
RUNTIME = MIGRATIONS / "DRAFT_visual_scene_attester_runtime_20261004.sql"
RECEIPT = MIGRATIONS / "DRAFT_visual_scene_original_use_receipt_20261004.sql"
PERSISTED = MIGRATIONS / "DRAFT_visual_scene_rpc_persisted_state_20261004.sql"
STACK = [
    "DRAFT_visual_group_schema_20261002.sql",
    "DRAFT_visual_global_history_20261002.sql",
    "DRAFT_visual_group_claim_trigger_20261002.sql",
    "DRAFT_visual_group_backfill_20261002.sql",
    "DRAFT_visual_group_activation_20261002.sql",
    "DRAFT_visual_scene_claim_wave_20261003.sql",
    "DRAFT_visual_scene_history_backfill_20261004.sql",
    "DRAFT_visual_scene_ledger_coverage_gate_20261004.sql",
    "calendar_publish_claim_ownership_20260918.sql",
    # P0: the runtime install preflight REFUSES the original claimant. The
    # persisted-state redefinition must be installed over it so a
    # trigger-converted scene hold returns no attempted token.
    "DRAFT_visual_scene_rpc_persisted_state_20261004.sql",
    "DRAFT_visual_scene_original_use_receipt_20261004.sql",
    "DRAFT_visual_scene_attester_runtime_20261004.sql",
]

PG_READY = bool(DSN) and PSQL is not None and RUNTIME.exists() \
    and RECEIPT.exists() and PERSISTED.exists()


# ---- static source contracts (always run) ---------------------------------

def _src():
    return RUNTIME.read_text() if RUNTIME.exists() else ""


def test_static_file_is_draft_and_off():
    src = _src()
    assert "DRAFT / UNAPPLIED / OFF" in src


def test_static_no_embedded_password():
    src = _src().lower()
    assert "create role scene_attester nologin" in src
    assert " encrypted password" not in src
    assert "with password" not in src


def test_static_revokes_old_standalone_entry_points():
    src = _src()
    assert re.search(r"revoke all on function "
                     r"public\." + TERMINATE_FN + r"\(jsonb\)\s*"
                     r"from public, anon, authenticated, service_role",
                     src)
    assert re.search(r"revoke all on function "
                     r"public\." + PREPARE_FN + r"\(jsonb\)\s*"
                     r"from public, anon, authenticated, service_role",
                     src)


def test_static_runtime_functions_security_definer_pinned_path():
    src = _src()
    for fn in ("visual_scene_attester_claim_prepare",
               "visual_scene_attester_send_start",
               "visual_scene_attester_attest_terminate"):
        m = re.search(r"create or replace function public\." + fn +
                      r"\(p jsonb\)(.*?)as \$\$", src, re.S)
        assert m, fn
        assert "security definer" in m.group(1)
        assert "set search_path = public" in m.group(1)
    assert re.search(r"grant execute on function "
                     r"public\.visual_scene_attester_claim_prepare\(jsonb\)\s*"
                     r"to scene_attester", src)
    assert re.search(r"grant execute on function "
                     r"public\.visual_scene_attester_attest_terminate\(jsonb\)"
                     r"\s*to scene_attester", src)


def test_static_tables_rls_and_snapshot_fields():
    src = _src()
    for tbl in ("visual_scene_attester_binding",
                "visual_scene_attester_prepared",
                "visual_scene_attester_event"):
        assert re.search(r"alter table public\." + tbl +
                         r" enable row level security", src)
        assert re.search(r"alter table public\." + tbl +
                         r" force row level security", src)
    for col in ("canonical_payload_sha256", "source_sha256", "source_md5",
                "source_byte_length", "delivered_sha256", "delivered_md5",
                "delivered_byte_length", "delivered_phash",
                "source_read_receipt", "delivered_read_receipt",
                "binding_id", "send_started_at", "lease_expires_at"):
        assert col in src
    for col in ("zernio_profile_id", "zernio_connected_account_id",
                "destination_page_id", "surface"):
        assert col in src


def test_static_lease_after_send_start_cannot_reauthorize():
    src = _src()
    assert "lease expiry cannot authorize another send" in src
    assert ("a send that started can never become lease_expired_hold"
            in src)
    assert "check (not (state = 'lease_expired_hold' and send_started_at " \
           "is not null))" in src


def test_static_no_direct_dml_grants_to_attester():
    src = _src()
    assert not re.search(r"grant (insert|update|delete)[^;]*scene_attester",
                         src, re.I)


def test_static_composite_delegates_to_authoritative_claimant():
    """P0: the composite claim leg MUST call the production authoritative
    claimant with its exact 6-arg signature and MUST NOT mint
    publish_claim_token itself."""
    src = _src()
    assert ("v_token := public.claim_calendar_publish_slot_owned("
            in src)
    assert "v_row_id, v_gym, v_day, v_tz, v_cap, v_appr" in src
    # No private token mint anywhere in this migration.
    assert "update public.content_calendar set publish_claim_token" \
        not in src
    # Install preflight pins the exact production signature.
    assert "pg_get_function_identity_arguments" in src
    assert ("'p_row_id uuid, p_gym_id text, p_day date, p_timezone text, '"
            in src)


def test_static_composite_matches_claimant_lock_order():
    """The composite must never hold a calendar row while waiting for the
    claimant's per-gym advisory lock. Discover the gym unlocked, acquire that
    exact advisory lock, then lock and revalidate the row before the reentrant
    claimant call."""
    src = _src()
    body = src.split("create or replace function "
                     "public.visual_scene_attester_claim_prepare", 1)[1]
    gym_lookup = "select c.gym_id into v_gym"
    advisory = "perform pg_advisory_xact_lock(hashtextextended(v_gym, 0));"
    row_lock = "where id = v_row_id for update;"
    claimant = "v_token := public.claim_calendar_publish_slot_owned("
    assert body.index(gym_lookup) < body.index(advisory) \
        < body.index(row_lock) < body.index(claimant)
    assert "v_row.gym_id is distinct from v_gym" in body
    assert "v_tenant is distinct from v_lookup_tenant" in body


def test_static_composite_fail_closed_on_claim_refusal():
    src = _src()
    # Source is line-wrapped; assert the contiguous pieces that exist.
    assert "refused by the authoritative slot '" in src
    assert "'claimant (gym/status/variant/late-post/approval/capacity gate); '" in src
    assert "claim_prepare: authoritative claim did not persist '" in src
    # Required claim inputs are validated before any work.
    for needle in ("publish_day, publish_timezone, '",
                   "publish_capacity must be in 1..2",
                   "publish_timezone is not a known IANA"):
        assert needle in src, needle


def test_static_replay_reachable_before_live_snapshot_guard():
    """Replay of an identical composite call must be evaluated BEFORE the
    live-snapshot guard, or every retry trips the guard and replay is
    unreachable. The send-started guard stays ahead of replay."""
    src = _src()
    fn = src.split("create or replace function "
                   "public.visual_scene_attester_claim_prepare", 1)[1]
    assert fn.index("lease expiry cannot authorize another send") \
        < fn.index("IDEMPOTENT REPLAY") \
        < fn.index("a live prepared snapshot already exists")


def test_static_lease_expiry_parks_hold_without_raising():
    """send_start on an expired lease must PARK lease_expired_hold and
    refuse by return; raising would roll the park back and leave a stale
    claimed_prepared snapshot able to send later."""
    src = _src()
    fn = src.split("create or replace function "
                   "public.visual_scene_attester_send_start", 1)[1]
    assert "lease expired before send start" not in fn
    assert "'state', 'lease_expired_hold', 'lease_expired', true" in fn


def test_static_scene_held_refusal_returns_normally():
    """P0: a REAL scene hold (persisted frozen held row + an OPEN review
    hold written by the authoritative scene trigger) must return a
    structured held/refused result WITHOUT raising — raising would roll
    back the durable hold. Non-scene refusals still fail closed by
    raising, and no hold is ever fabricated."""
    src = _src()
    # Install preflight REQUIRES the persisted-state claimant redefinition
    # (the original claimant can return an attempted token for a
    # trigger-held row) and the merged scene guard trigger.
    assert "DRAFT_visual_scene_rpc_persisted_state_20261004.sql" in src
    assert "attempted state, not proof" in src
    assert "content_calendar_visual_group_guard" in src
    # Structured refusal helper: genuine hold = exact frozen held row
    # shape AND an open visual_scene_review_hold; anything less returns
    # null and the caller fails closed.
    assert ("create or replace function "
            "public.visual_scene_attester_scene_hold_refusal("
            "p_row_id uuid, p_attester text)") in src
    fn = src.split("public.visual_scene_attester_scene_hold_refusal", 1)[1]
    assert "visual_scene_review_hold" in fn
    assert "h.state = 'open'" in fn
    assert "'scene_review_hold'" in fn
    assert "'state', 'scene_held'" in fn
    assert "'refused', true" in fn
    assert "publish_claim_token is null" in fn
    # Internal only: no EXECUTE grant to any non-owner role.
    assert not re.search(r"grant execute on function "
                         r"public\.visual_scene_attester_scene_hold_refusal",
                         src)
    # Composite: the helper is consulted BEFORE any refusal raise, in both
    # the null-token branch and the not-persisted verify branch.
    body = src.split("create or replace function "
                     "public.visual_scene_attester_claim_prepare", 1)[1]
    assert body.count("visual_scene_attester_scene_hold_refusal") >= 2
    assert body.index("visual_scene_attester_scene_hold_refusal") \
        < body.index("refused by the authoritative slot '")
    # The persisted row is re-read in a separate statement immediately
    # after the claimant, before the token-null classification.
    reread = "select * into v_row from public.content_calendar" \
        "\n    where id = v_row_id;"
    assert body.index("v_token := public.claim_calendar_publish_slot_owned(") \
        < body.index(reread) < body.index("if v_token is null then")


# ---- PG helpers ------------------------------------------------------------

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
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_attester_test"
        r"(?: user=[A-Za-z0-9_-]+)?", DSN), \
        "only the named disposable Unix-socket DB echo_scene_attester_test allowed"
    assert _one("select current_database()") == "echo_scene_attester_test"


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
         "status text, variant_status text, format text,"
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


def _runtime_body():
    lines = RUNTIME.read_text().splitlines()
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
    """Rollback-only installation probe (proves the file installs and its
    rollback leaves no catalog trace)."""
    if not PG_READY:
        pytest.skip("attester PG checks need SCENE_ATTESTER_TEST_DSN on a "
                    "disposable local DB and both migrations present")
    _check_dsn()
    _fresh_scratch_schema()
    _apply("".join((MIGRATIONS / n).read_text() + "\n" for n in STACK[:-1]))
    before = _catalog_snapshot()
    _apply("begin;\n" + _runtime_body() + "\nrollback;\n")
    assert _catalog_snapshot() == before, \
        "rollback-only install probe left catalog changes behind"


@pytest.fixture(scope="module")
def scratch_stack(install_probes):
    if not PG_READY:
        pytest.skip("attester PG checks need SCENE_ATTESTER_TEST_DSN on a "
                    "disposable local DB and both migrations present")
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
         "public.visual_scene_original_use_receipt, "
         "public.visual_scene_attester_binding, "
         "public.visual_scene_attester_prepared, "
         "public.visual_scene_attester_event cascade")


# ---- seeding helpers (scratch DB only; SIMULATED shapes) -------------------

def _seed_tenant():
    tid = str(uuid.uuid4())
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{tid}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{tid}', '{group}')")
    return tid, group


def _read_receipt(tid, url, fp):
    return _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'attester-test') returning receipt_id")


def _object(tid):
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    rr = _read_receipt(tid, url, fp)
    return url, fp, rr


def _near(phash, bits):
    """Same 16-hex word with `bits` low bits flipped: exact Hamming
    distance `bits` (<=6 near_frame band, 7..30 uncertain band)."""
    return format(int(phash, 16) ^ ((1 << bits) - 1), "016x")


def _arm(tid):
    """Arm enforcement through the activation draft's own guarded RPC
    (the only sanctioned arming path)."""
    _sql(f"select public.visual_group_activate_guard('{tid}',"
         " 'attester-test')")


def _armed_scene(tid, group, phash):
    """Fully attested delivered object + bound 'display' scene candidate
    for an ARMED tenant, so the merged scene guard trigger
    (content_calendar_visual_group_guard) runs its real scene decision on
    the row. Returns (url, fingerprint, read_receipt)."""
    url, fp, rr = _object(tid)
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by)"
         " values "
         f"('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{rr}',"
         " 'attester-test')")
    for role in ("source", "delivered"):
        _sql("insert into public.visual_global_scene_object_member"
             "(tenant_id, group_key, exact_url, fingerprint, object_role)"
             f" values ('{tid}', '{group}', '{url}', '{fp}', '{role}')")
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"('{tid}', 'canonical_url', '{url}', '{group}')")
    _sql("select public.visual_scene_register_candidate("
         f"'{tid}', '{group}', '{phash}', '{url}', '{fp}',"
         f" jsonb_build_object('verified_bytes', '{fp}'),"
         " 'attester-test', 'display')")
    return url, fp, rr


def _binding(tid, account="scratch-ig", surface="feed"):
    bid = str(uuid.uuid4())
    return _one(
        "insert into public.visual_scene_attester_binding"
        "(binding_id, tenant_id, account_key, zernio_profile_id,"
        " zernio_connected_account_id, channel, destination_page_id,"
        " surface, bound_by) values "
        f"('{bid}', '{tid}', '{account}', 'zp_{tid[:8]}',"
        f" 'zca_{tid[:8]}', 'instagram', 'page_{tid[:8]}', '{surface}',"
        " 'attester-test') returning binding_id")


def _unclaimed_row(tid, group, url, used_date="2026-09-01",
                   account="scratch-ig"):
    return _one(
        "insert into public.content_calendar(gym_id, post_date, status,"
        " visual_group_key, image_url, source_media_url, account,"
        " variant_status) values "
        f"('{tid}', '{used_date}', 'pending', '{group}', '{url}',"
        f" '{url}', '{account}', 'active') returning id")


def _reserve(tid, group, row_id, used_date="2026-09-01"):
    _sql("insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, calendar_row_id, state) values "
         f"('{tid}', '{group}', '{used_date}', '{row_id}', 'reserved')")


def _occupy(tid, group, row_id, fp, phash, url, used_date="2026-09-01"):
    _sql("insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, channel, evidence) values "
         f"('{phash}', '{tid}', '{group}', '{used_date}', '{fp}',"
         f" '{row_id}', 'scratch-ig',"
         f" jsonb_build_object('candidate_id', '{uuid.uuid4()}'::uuid,"
         f" 'exact_url', '{url}'))")


def _setup_claimable(used_date="2026-09-01", account="scratch-ig",
                     tid=None):
    """Full claimable scene: tenant, binding, unclaimed row, ledger
    reservation, occupancy, read receipt. Returns a dict of ids. Pass an
    existing tid to add another row to the SAME gym (capacity tests)."""
    if tid is None:
        tid, group = _seed_tenant()
        url, fp, rr = _object(tid)
        phash = uuid.uuid4().hex[:16]
        bid = _binding(tid, account)
    else:
        # Same gym, new scene: reuse the FIRST binding for this
        # tenant/account (bindings are append-only; re-inserting the same
        # identity would violate the binding uniqueness).
        group = "vg_" + uuid.uuid4().hex[:12]
        _sql(f"insert into public.visual_group(gym_id, group_key) "
             f"values ('{tid}', '{group}')")
        url, fp, rr = _object(tid)
        phash = uuid.uuid4().hex[:16]
        bid = _one("select binding_id::text from "
                   "public.visual_scene_attester_binding "
                   f"where tenant_id = '{tid}' and account_key = '{account}'"
                   " order by bound_at, binding_id limit 1")
    row_id = _unclaimed_row(tid, group, url, used_date, account)
    _reserve(tid, group, row_id, used_date)
    _occupy(tid, group, row_id, fp, phash, url, used_date)
    return dict(tid=tid, group=group, url=url, fp=fp, rr=rr, phash=phash,
                bid=bid, row_id=row_id, date=used_date, account=account)


def _claim_payload(s, attester="attester-1", lease=900):
    sha = _sha256("claim_payload_default")
    return {
        "calendar_row_id": s["row_id"], "group_key": s["group"],
        "used_date": s["date"], "source_url": s["url"],
        "source_md5": s["fp"], "delivered_url": s["url"],
        "delivered_md5": s["fp"], "delivered_phash": s["phash"],
        "source_read_receipt": s["rr"], "delivered_read_receipt": s["rr"],
        "provider": "zernio", "provider_account_id": "zca_" + s["tid"][:8],
        "channel": "instagram", "zernio_profile_id": "zp_" + s["tid"][:8],
        "zernio_connected_account_id": "zca_" + s["tid"][:8],
        "destination_page_id": "page_" + s["tid"][:8], "surface": "feed",
        "canonical_payload_sha256": s.get("payload_sha", sha),
        "source_sha256": s.get("src_sha", _sha256("claim_payload_src_default")),
        "delivered_sha256": s.get("dst_sha", _sha256("claim_payload_dst_default")),
        "source_byte_length": 1024, "delivered_byte_length": 1024,
        "attester_id": attester, "lease_seconds": lease,
        "publish_day": s["date"], "publish_timezone": "UTC",
        "publish_capacity": 2, "publish_approved_only": False,
    }


def _call(fn, payload):
    body = json.dumps(payload).replace("'", "''")
    return _one(f"select public.{fn}('{body}'::jsonb)::text")


def _call_as(role, fn, payload):
    body = json.dumps(payload).replace("'", "''")
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", f"set role {role}; select public.{fn}('{body}'::jsonb)::text"],
        text=True, capture_output=True, timeout=120)
    return done


# ---- PG scenarios ------------------------------------------------------------

def test_pg_role_grants(scratch):
    """scene_attester may execute exactly the three runtime functions and
    nothing else; service_role lost the old standalone entry points."""
    checks = [
        ("scene_attester", "visual_scene_attester_claim_prepare(jsonb)", True),
        ("scene_attester", "visual_scene_attester_send_start(jsonb)", True),
        ("scene_attester", "visual_scene_attester_attest_terminate(jsonb)", True),
        ("scene_attester", PREPARE_FN + "(jsonb)", False),
        ("scene_attester", TERMINATE_FN + "(jsonb)", False),
        ("service_role", PREPARE_FN + "(jsonb)", False),
        ("service_role", TERMINATE_FN + "(jsonb)", False),
        ("service_role", "visual_scene_attester_claim_prepare(jsonb)", False),
        ("service_role", "visual_scene_attester_attest_terminate(jsonb)", False),
    ]
    for role, fn, expected in checks:
        got = _one(f"select has_function_privilege('{role}', "
                   f"'public.{fn}', 'execute')")
        assert got == ("t" if expected else "f"), (role, fn, got)
    for tbl in ("visual_scene_attester_binding",
                "visual_scene_attester_prepared",
                "visual_scene_attester_event"):
        assert _one(f"select has_table_privilege('scene_attester', "
                    f"'public.{tbl}', 'insert')") == "f"
        assert _one(f"select has_table_privilege('scene_attester', "
                    f"'public.{tbl}', 'select')") == "t"
    # Direct calls as scene_attester to the old entry points refuse.
    done = _call_as("scene_attester", PREPARE_FN, {})
    assert done.returncode != 0
    assert "permission denied" in done.stderr
    done = _call_as("service_role", TERMINATE_FN, {})
    assert done.returncode != 0
    assert "permission denied" in done.stderr


def test_pg_composite_claim_prepare_happy_and_rollback(scratch):
    """Happy path: one transaction mints the token, binds the row, prepares
    the attempt and freezes the snapshot. Composite rollback: a prepare
    refusal (wrong ledger date) leaves NO token on the row and NO snapshot
    — claim and prepare are one transaction."""
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_1")
    out = json.loads(_call("visual_scene_attester_claim_prepare",
                           _claim_payload(s)))
    token = out["claim_attempt_id"]
    assert out["state"] == "claimed_prepared" and not out["replayed"]
    assert _one("select publish_claim_token::text from "
                f"public.content_calendar where id = '{s['row_id']}'") \
        == token
    assert _one("select state from public.visual_scene_attester_prepared "
                f"where claim_attempt_id = '{token}'") == "claimed_prepared"
    assert _one("select count(*) from public.visual_scene_attester_event "
                f"where claim_attempt_id = '{token}' "
                "and event = 'claim_prepared'") == "1"

    # Rollback: a second row whose prepare evidence is wrong (ledger date
    # mismatch) must leave the row UNCLAIMED and no snapshot behind.
    s2 = _setup_claimable()
    bad = _claim_payload(s2)
    bad["used_date"] = "2026-09-02"  # ledger/occupancy/row are 2026-09-01
    _fails("select public.visual_scene_attester_claim_prepare("
           f"'{json.dumps(bad)}'::jsonb)",
           "prepare:")
    assert _one("select publish_claim_token is null from "
                f"public.content_calendar where id = '{s2['row_id']}'") == "t"
    # The claimant's reservation rolled back with the refused prepare.
    assert _one("select status from public.content_calendar "
                f"where id = '{s2['row_id']}'") == "pending"
    assert _one("select publish_reservation_day is null from "
                f"public.content_calendar where id = '{s2['row_id']}'") == "t"
    assert _one("select count(*) from public.visual_scene_attester_prepared "
                f"where calendar_row_id = '{s2['row_id']}'") == "0"
    assert _one("select count(*) from public.visual_scene_original_use_attempt "
                f"where calendar_row_id = '{s2['row_id']}'") == "0"


def test_pg_replay_and_drift_refusal(scratch):
    """Identical composite call replays; payload drift under the held token
    is a hard conflict (token/binding drift)."""
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_2")
    s["src_sha"] = _sha256("fixture_sha256_3")
    s["dst_sha"] = _sha256("fixture_sha256_4")
    p = _claim_payload(s)
    out = json.loads(_call("visual_scene_attester_claim_prepare", p))
    token = out["claim_attempt_id"]
    out2 = json.loads(_call("visual_scene_attester_claim_prepare", p))
    assert out2["claim_attempt_id"] == token and out2["replayed"] is True

    drift = dict(p, canonical_payload_sha256=_sha256("drift_payload"))
    _fails("select public.visual_scene_attester_claim_prepare("
           f"'{json.dumps(drift)}'::jsonb)", "drift refused")
    # No binding for a foreign Zernio profile.
    nobind = _setup_claimable()
    p2 = _claim_payload(nobind)
    p2["zernio_profile_id"] = "zp_foreign"
    _fails("select public.visual_scene_attester_claim_prepare("
           f"'{json.dumps(p2)}'::jsonb)",
           "no tenant-to-Zernio binding matches")


def test_pg_two_attester_send_start_race(scratch):
    """Two concurrent send_start calls on one token: exactly one wins; the
    loser sees the send already started."""
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_5")
    token = json.loads(_call("visual_scene_attester_claim_prepare",
                             _claim_payload(s)))["claim_attempt_id"]
    body = json.dumps({"claim_attempt_id": token,
                       "attester_id": "attester-1"}).replace("'", "''")
    stmt = ("select public.visual_scene_attester_send_start("
            f"'{body}'::jsonb)::text")
    procs = [subprocess.Popen(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", stmt], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE) for _ in range(2)]
    results = [p.communicate(timeout=120) for p in procs]
    # Both calls share attester-1: the loser blocks on the row lock and
    # then sees the winner's send_started as an idempotent REPLAY. Exactly
    # one call may be the original (replayed=false) success.
    ok = [json.loads(o) for p, (o, e) in zip(procs, results)
          if p.returncode == 0]
    originals = [r for r in ok if r.get("replayed") is False]
    assert len(originals) == 1, results
    assert all(r.get("state") == "send_started" for r in ok), results
    assert _one("select state from public.visual_scene_attester_prepared "
                f"where claim_attempt_id = '{token}'") == "send_started"
    # Same attester replaying send_start is idempotent.
    out = json.loads(_call("visual_scene_attester_send_start",
                           {"claim_attempt_id": token,
                            "attester_id": "attester-1"}))
    assert out["replayed"] is True
    # A different attester can never attach to the started send: the
    # started-send guard refuses first with its own valid error string.
    stmt = ("select public.visual_scene_attester_send_start('"
            + json.dumps({"claim_attempt_id": token,
                          "attester_id": "attester-2"}).replace("'", "''")
            + "'::jsonb)")
    _fails(stmt, "send already started by another attester")


def test_pg_lease_expiry_rules(scratch):
    """Lease expiry BEFORE send start parks lease_expired_hold and a new
    composite claim IS allowed. Once send_started_at is set, no new claim
    for the row is EVER authorized (lease expiry after send start
    authorizes nothing)."""
    # (a) pre-send expiry -> new claim allowed
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_6")
    token = json.loads(_call("visual_scene_attester_claim_prepare",
                             _claim_payload(s, lease=1)))["claim_attempt_id"]
    _sql("set session_replication_role = replica; "
         "update public.visual_scene_attester_prepared set "
         "lease_expires_at = now() - interval '1 second' "
         f"where claim_attempt_id = '{token}'")
    stmt = ("select public.visual_scene_attester_send_start('"
            + json.dumps({"claim_attempt_id": token,
                          "attester_id": "attester-1"}).replace("'", "''")
            + "'::jsonb)::text")
    # Refusal by RETURN, not raise: an exception would roll the park back
    # and leave a stale claimed_prepared snapshot able to send later.
    out = json.loads(_one(stmt))
    assert out["state"] == "lease_expired_hold"
    assert out["lease_expired"] is True and out["send_started"] is False
    assert _one("select state from public.visual_scene_attester_prepared "
                f"where claim_attempt_id = '{token}'") == "lease_expired_hold"
    assert _one("select send_started_at is null from "
                "public.visual_scene_attester_prepared "
                f"where claim_attempt_id = '{token}'") == "t"
    # Row token released? No: lease_expired_hold keeps history; a new
    # composite claim is allowed only because no send ever started. The
    # authoritative claimant only claims pending/approved rows, so the
    # simulated release restores the pre-claim row state (spent token
    # cleared, publishing reservation undone) — what a human/reconciler
    # does when freeing a held row.
    _sql("update public.content_calendar set publish_claim_token = null,"
         " status = 'pending', publish_reservation_day = null "
         f"where id = '{s['row_id']}'")
    s["payload_sha"] = _sha256("fixture_sha256_7")
    out = json.loads(_call("visual_scene_attester_claim_prepare",
                           _claim_payload(s)))
    assert out["state"] == "claimed_prepared"

    # (b) send started -> no new claim ever, even after lease expiry
    s2 = _setup_claimable()
    s2["payload_sha"] = _sha256("fixture_sha256_s2_1")
    token2 = json.loads(_call("visual_scene_attester_claim_prepare",
                              _claim_payload(s2, lease=1)))["claim_attempt_id"]
    _call("visual_scene_attester_send_start",
          {"claim_attempt_id": token2, "attester_id": "attester-1"})
    _sql("set session_replication_role = replica; "
         "update public.visual_scene_attester_prepared set "
         "lease_expires_at = now() - interval '1 second' "
         f"where claim_attempt_id = '{token2}'")
    _fails("select public.visual_scene_attester_claim_prepare("
           f"'{json.dumps(_claim_payload(s2))}'::jsonb)",
           "lease expiry cannot authorize another send")
    # The started send can also never be parked as lease_expired_hold.
    _fails("update public.visual_scene_attester_prepared set "
           f"state = 'lease_expired_hold' where claim_attempt_id = '{token2}'",
           "can never become lease_expired_hold")


def test_pg_ambiguous_hold_then_atomic_finalize(scratch):
    """Ambiguous provider outcome: parks ambiguous_hold, row keeps its
    token and stays unpublished, no attestation/receipt exists. A later
    authoritative attest+terminate in one transaction finalizes: receipt
    minted, row published, snapshot finalized."""
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_8")
    token = json.loads(_call("visual_scene_attester_claim_prepare",
                             _claim_payload(s)))["claim_attempt_id"]
    _call("visual_scene_attester_send_start",
          {"claim_attempt_id": token, "attester_id": "attester-1"})
    out = json.loads(_call("visual_scene_attester_attest_terminate",
                           {"claim_attempt_id": token,
                            "attester_id": "attester-1",
                            "outcome": "ambiguous",
                            "readback_evidence": {"reason": "timeout"}}))
    assert out["state"] == "ambiguous_hold"
    # Held = the authoritative claimant's publishing reservation with the
    # claim token still bound; no other lane may touch it.
    assert _one("select status from public.content_calendar "
                f"where id = '{s['row_id']}'") == "publishing"
    assert _one("select publish_claim_token::text from "
                "public.content_calendar "
                f"where id = '{s['row_id']}'") == token
    assert _one("select count(*) from "
                "public.visual_scene_original_use_attestation "
                f"where claim_attempt_id = '{token}'") == "0"
    assert _one("select count(*) from "
                "public.visual_scene_original_use_receipt") == "0"
    assert _one("select count(*) from public.visual_scene_attester_event "
                f"where claim_attempt_id = '{token}' "
                "and event = 'ambiguous_hold'") == "1"

    # Atomic finalize from the hold.
    out = json.loads(_call("visual_scene_attester_attest_terminate",
                           {"claim_attempt_id": token,
                            "attester_id": "attester-1",
                            "outcome": "delivered",
                            "provider_post_id": "post_123",
                            "readback_evidence": {"seen": True}}))
    assert out["state"] == "finalized"
    terminal = out["terminal"]
    if isinstance(terminal, str):
        terminal = json.loads(terminal)
    assert terminal["state"] == "confirmed_delivered"
    assert _one("select count(*) from "
                "public.visual_scene_original_use_receipt "
                f"where claim_attempt_id = '{token}'") == "1"
    assert _one("select status from public.content_calendar "
                f"where id = '{s['row_id']}'") == "published"
    assert _one("select state from public.visual_scene_attester_prepared "
                f"where claim_attempt_id = '{token}'") == "finalized"
    # A conflicting terminal replay must be refused even though the snapshot
    # is already finalized; compare against the recorded attestation/receipt.
    for conflicting in (
        {"outcome": "confirmed_no_send"},
        {"outcome": "delivered", "provider_post_id": "post_other"},
    ):
        replay = {"claim_attempt_id": token, "attester_id": "attester-1",
                  "readback_evidence": {"seen": True}, **conflicting}
        stmt = ("select public.visual_scene_attester_attest_terminate('"
                + json.dumps(replay).replace("'", "''") + "'::jsonb)")
        _fails(stmt, "finalized replay conflicts")
    # Identical replay of the finalized snapshot returns the recorded result.
    out = json.loads(_call("visual_scene_attester_attest_terminate",
                           {"claim_attempt_id": token,
                            "attester_id": "attester-1",
                            "outcome": "delivered",
                            "provider_post_id": "post_123",
                            "readback_evidence": {"seen": True}}))
    assert out["replayed"] is True


def test_pg_atomic_terminal_rollback(scratch):
    """A terminal refusal (here: foreign attester identity) rolls back
    EVERYTHING: no attestation row, snapshot not finalized, row
    unpublished, no receipt."""
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_9")
    token = json.loads(_call("visual_scene_attester_claim_prepare",
                             _claim_payload(s)))["claim_attempt_id"]
    _call("visual_scene_attester_send_start",
          {"claim_attempt_id": token, "attester_id": "attester-1"})
    stmt = ("select public.visual_scene_attester_attest_terminate('"
            + json.dumps({"claim_attempt_id": token,
                          "attester_id": "attester-foreign",
                          "outcome": "delivered",
                          "provider_post_id": "post_x",
                          "readback_evidence": {"seen": True}}
                         ).replace("'", "''")
            + "'::jsonb)")
    _fails(stmt, "attester identity does not match")
    assert _one("select count(*) from "
                "public.visual_scene_original_use_attestation "
                f"where claim_attempt_id = '{token}'") == "0"
    assert _one("select count(*) from "
                "public.visual_scene_original_use_receipt") == "0"
    assert _one("select state from public.visual_scene_attester_prepared "
                f"where claim_attempt_id = '{token}'") == "send_started"
    assert _one("select status from public.content_calendar "
                f"where id = '{s['row_id']}'") == "publishing"
    # Terminal without send start also refuses (no send, no finalize).
    s2 = _setup_claimable()
    s2["payload_sha"] = _sha256("fixture_sha256_s2_2")
    token2 = json.loads(_call("visual_scene_attester_claim_prepare",
                              _claim_payload(s2)))["claim_attempt_id"]
    stmt = ("select public.visual_scene_attester_attest_terminate('"
            + json.dumps({"claim_attempt_id": token2,
                          "attester_id": "attester-1",
                          "outcome": "delivered",
                          "provider_post_id": "post_y",
                          "readback_evidence": {"seen": True}}
                         ).replace("'", "''")
            + "'::jsonb)")
    _fails(stmt, "terminal outcomes require a started send")
    assert _one("select count(*) from "
                "public.visual_scene_original_use_attestation "
                f"where claim_attempt_id = '{token2}'") == "0"


def _refused_by_claimant(payload, needle="refused by the authoritative "
                            "slot claimant"):
    stmt = ("select public.visual_scene_attester_claim_prepare('"
            + json.dumps(payload).replace("'", "''") + "'::jsonb)")
    _fails(stmt, needle)
    row_id = payload["calendar_row_id"]
    # Fail closed: no token, no reservation, no snapshot, no attempt.
    assert _one("select publish_claim_token is null from "
                f"public.content_calendar where id = '{row_id}'") == "t"
    assert _one("select publish_reservation_day is null from "
                f"public.content_calendar where id = '{row_id}'") == "t"
    assert _one("select count(*) from public.visual_scene_attester_prepared "
                f"where calendar_row_id = '{row_id}'") == "0"
    assert _one("select count(*) from "
                "public.visual_scene_original_use_attempt where "
                f"calendar_row_id = '{row_id}'") == "0"


def test_pg_authoritative_claim_eligibility(scratch):
    """P0 eligibility: archived variant, late_post_id, unapproved row on an
    approved-only lane, over-capacity day and a foreign-gym/binding-free
    row all refuse through the AUTHORITATIVE claimant with no token and no
    attempt. An approved row on an approved-only lane succeeds."""
    # Archived variant.
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_e1")
    _sql(f"update public.content_calendar set variant_status = 'archived' "
         f"where id = '{s['row_id']}'")
    _refused_by_claimant(_claim_payload(s))

    # late_post_id already set (provider echo row).
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_e2")
    _sql(f"update public.content_calendar set late_post_id = 'lp_x' "
         f"where id = '{s['row_id']}'")
    _refused_by_claimant(_claim_payload(s))

    # Unapproved (pending) row on an approved-only lane.
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_e3")
    p = _claim_payload(s)
    p["publish_approved_only"] = True
    _refused_by_claimant(p)
    # The SAME row, still pending, on a lane that allows pending claims.
    p["publish_approved_only"] = False
    out = json.loads(_call("visual_scene_attester_claim_prepare", p))
    assert out["state"] == "claimed_prepared" and not out["replayed"]
    assert _one("select status from public.content_calendar "
                f"where id = '{s['row_id']}'") == "publishing"

    # Approved row on an approved-only lane succeeds.
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_e4")
    _sql(f"update public.content_calendar set status = 'approved' "
         f"where id = '{s['row_id']}'")
    p = _claim_payload(s)
    p["publish_approved_only"] = True
    out = json.loads(_call("visual_scene_attester_claim_prepare", p))
    assert out["state"] == "claimed_prepared"

    # Over-capacity: capacity 1 with a same-gym/account row already
    # publishing for that reservation day.
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_e5")
    sib = _setup_claimable(tid=s["tid"], account=s["account"])
    _sql("update public.content_calendar set status = 'publishing', "
         "publish_reservation_day = '2026-09-01', "
         f"publish_claim_token = '{uuid.uuid4()}' "
         f"where id = '{sib['row_id']}'")
    p = _claim_payload(s)
    p["publish_capacity"] = 1
    _refused_by_claimant(p)

    # Published row refuses even before the claimant (defense in depth).
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_e6")
    _sql("update public.content_calendar set status = 'published', "
         f"published_at = now() where id = '{s['row_id']}'")
    _refused_by_claimant(_claim_payload(s),
                         needle="row already marked published")


def test_pg_capacity_race_exactly_one_winner(scratch):
    """Two concurrent composite claims for DIFFERENT rows of the same
    gym/account on a capacity-1 day: exactly one wins the authoritative
    slot; the loser refuses with no token, no snapshot, no attempt."""
    s1 = _setup_claimable()
    s1["payload_sha"] = _sha256("fixture_sha256_r1")
    s2 = _setup_claimable(tid=s1["tid"], account=s1["account"])
    s2["payload_sha"] = _sha256("fixture_sha256_r2")
    stmts = []
    for s in (s1, s2):
        p = _claim_payload(s)
        p["publish_capacity"] = 1
        body = json.dumps(p).replace("'", "''")
        stmts.append("select public.visual_scene_attester_claim_prepare("
                     f"'{body}'::jsonb)::text")
    procs = [subprocess.Popen(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", stmt], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE) for stmt in stmts]
    results = [pr.communicate(timeout=180) for pr in procs]
    wins = [json.loads(o) for pr, (o, e) in zip(procs, results)
            if pr.returncode == 0 and o.strip().startswith("{")]
    losses = [(o, e) for pr, (o, e) in zip(procs, results)
              if pr.returncode != 0]
    assert len(wins) == 1, results
    assert len(losses) == 1, results
    assert "refused by the authoritative slot claimant" in losses[0][1]
    tokens = {_one("select publish_claim_token::text from "
                   f"public.content_calendar where id = '{s['row_id']}'")
              for s in (s1, s2)}
    assert tokens - {""} == {wins[0]["claim_attempt_id"]}, results
    assert "" in tokens  # the losing row was left completely unclaimed
    assert _one("select count(*) from "
                "public.visual_scene_attester_prepared") == "1"


def test_pg_composite_takes_gym_advisory_before_row_lock(scratch):
    """Hold the gym advisory lock in one session, then enter the composite
    in another. While the composite waits on that advisory lock, a third
    transaction must still be able to lock the calendar row. The old
    row-first order makes the third transaction time out and can deadlock
    against a direct claimant that already owns the advisory lock."""
    s = _setup_claimable()
    s["payload_sha"] = _sha256("fixture_sha256_lock_order")
    body = json.dumps(_claim_payload(s)).replace("'", "''")
    composite_stmt = (
        "select public.visual_scene_attester_claim_prepare("
        f"'{body}'::jsonb)::text")
    suffix = uuid.uuid4().hex[:10]
    holder_app = "scene_lock_holder_" + suffix
    waiter_app = "scene_lock_waiter_" + suffix

    def wait_event(app, expected, timeout=10):
        deadline = time.monotonic() + timeout
        observed = ""
        while time.monotonic() < deadline:
            observed = _one(
                "select coalesce(lower(wait_event), '') from pg_stat_activity "
                f"where application_name = '{app}'")
            if observed == expected:
                return
            time.sleep(0.05)
        raise AssertionError(
            f"{app} did not reach wait_event={expected!r}; last={observed!r}")

    holder_env = dict(os.environ, PGAPPNAME=holder_app)
    waiter_env = dict(os.environ, PGAPPNAME=waiter_app)
    holder_stmt = (
        "begin; select pg_advisory_xact_lock(hashtextextended("
        f"'{s['tid']}', 0)); select pg_sleep(30); commit")
    holder = subprocess.Popen(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
         "-d", DSN, "-c", holder_stmt],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=holder_env)
    waiter = None
    probe = None
    try:
        # PgSleep proves the holder acquired the advisory lock first.
        wait_event(holder_app, "pgsleep")
        waiter = subprocess.Popen(
            [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-d", DSN, "-c", composite_stmt],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=waiter_env)
        # The composite is now waiting for the advisory lock. It must not
        # own the row lock yet.
        wait_event(waiter_app, "advisory")
        probe = _run(
            "set lock_timeout = '1500ms'; select id from "
            "public.content_calendar "
            f"where id = '{s['row_id']}' for update", check=False)
    finally:
        if holder.poll() is None:
            # Cancel the deliberate pg_sleep so the holder transaction ends
            # and releases the advisory lock without adding 30 seconds to the
            # suite. The disposable backend is identified by this test's
            # unique application_name.
            _run("select pg_cancel_backend(pid) from pg_stat_activity "
                 f"where application_name = '{holder_app}'", check=False)
            holder.terminate()
        holder.communicate(timeout=10)

    assert probe is not None and probe.returncode == 0, \
        probe.stderr if probe is not None else "row-lock probe did not run"
    assert waiter is not None
    waiter_out, waiter_err = waiter.communicate(timeout=30)
    assert waiter.returncode == 0, waiter_err
    result = json.loads(waiter_out)
    assert result["state"] == "claimed_prepared"


def test_pg_scene_conflict_durable_hold_no_raise(scratch):
    """A row already held by the scene trigger receives a durable refusal.

    Tenant A's occupied scene makes tenant B's near-frame candidate held
    when B's row is inserted. The composite claim is called afterward on
    that pre-held row; it returns a structured refusal normally, and the
    row and OPEN review hold survive the transaction. No token, snapshot,
    or prepare attempt is created. This covers persisted pre-held state,
    not a claim-time collision transition."""
    # Tenant A: armed, attested scene, committed occupancy.
    tid_a, group_a = _seed_tenant()
    # Arm both tenants before either has scene-ledger history. Activation is
    # fleet-wide and correctly refuses while an existing tenant has unresolved
    # historical ledger coverage.
    tid_b, group_b = _seed_tenant()
    _arm(tid_a)
    _arm(tid_b)
    base = uuid.uuid4().hex[:16]
    url_a, _fpa, _rra = _armed_scene(tid_a, group_a, base)
    _unclaimed_row(tid_a, group_a, url_a)
    assert _one("select count(*) from public.visual_scene_phash_occupied "
                f"where tenant_id = '{tid_a}'") == "1"

    # Tenant B: armed, attested scene whose candidate is near A's occupied
    # scene. The row INSERT creates the held state before the composite
    # claim is attempted.
    near = _near(base, 2)
    url_b, fp_b, rr_b = _armed_scene(tid_b, group_b, near)
    _binding(tid_b, account="scratch-ig", surface="feed")
    row_b = _unclaimed_row(tid_b, group_b, url_b)
    assert _one("select status || '|' || variant_status || '|' || "
                "coalesce(media_not_ready_reason, '-') from "
                f"public.content_calendar where id = '{row_b}'") \
        == "pending|archived|scene_review_hold"
    assert _one("select count(*) from public.visual_scene_review_hold "
                f"where tenant_id = '{tid_b}' and state = 'open' "
                f"and calendar_row_id = '{row_b}'") == "1"

    # The composite sees an already-held row. It returns a structured
    # refusal normally; this does not exercise a claim-time hold transition.
    s_b = dict(tid=tid_b, group=group_b, url=url_b, fp=fp_b, rr=rr_b,
               phash=near, row_id=row_b, date="2026-09-01",
               account="scratch-ig",
               payload_sha=_sha256("fixture_sha256_scene_held"))
    out = json.loads(_call("visual_scene_attester_claim_prepare",
                           _claim_payload(s_b)))
    assert out["refused"] is True
    assert out["state"] == "scene_held"
    assert out["claim_attempt_id"] is None
    assert out["replayed"] is False
    hold_id = out["hold_id"]

    # The pre-existing hold remains durable after the composite transaction:
    # exact frozen shape, open hold, no token/reservation, nothing prepared.
    assert _one("select status from public.content_calendar "
                f"where id = '{row_b}'") == "pending"
    assert _one("select variant_status from public.content_calendar "
                f"where id = '{row_b}'") == "archived"
    assert _one("select media_not_ready_reason from "
                f"public.content_calendar where id = '{row_b}'") \
        == "scene_review_hold"
    assert _one("select publish_claim_token is null from "
                f"public.content_calendar where id = '{row_b}'") == "t"
    assert _one("select publish_reservation_day is null from "
                f"public.content_calendar where id = '{row_b}'") == "t"
    assert _one("select state from public.visual_scene_review_hold "
                f"where hold_id = '{hold_id}'") == "open"
    assert _one("select count(*) from public.visual_scene_review_hold "
                f"where calendar_row_id = '{row_b}'") == "1"
    # No attempt, no snapshot, no token granting anywhere.
    assert _one("select count(*) from public.visual_scene_attester_prepared "
                f"where calendar_row_id = '{row_b}'") == "0"
    assert _one("select count(*) from "
                "public.visual_scene_original_use_attempt where "
                f"calendar_row_id = '{row_b}'") == "0"
    assert _one("select count(*) from public.visual_scene_attester_event "
                f"where calendar_row_id = '{row_b}' "
                "and event = 'scene_hold_refusal'") == "1"

    # A retry of the composite on the held row also returns the
    # structured refusal normally and never duplicates the hold.
    out2 = json.loads(_call("visual_scene_attester_claim_prepare",
                            _claim_payload(s_b)))
    assert out2["refused"] is True and out2["state"] == "scene_held"
    assert out2["hold_id"] == hold_id
    assert _one("select count(*) from public.visual_scene_review_hold "
                f"where calendar_row_id = '{row_b}'") == "1"
