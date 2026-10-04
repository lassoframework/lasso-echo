"""Scene original-use receipt: static source contracts + OPTIONAL
real-PostgreSQL fail-closed checks.

Static tests always run. PG scenarios run only when ALL of the following hold:
  * `migrations/DRAFT_visual_scene_original_use_receipt_20261004.sql` exists,
  * SCENE_RECEIPT_TEST_DSN names a disposable Unix-socket database literally
    named `echo_scene_receipt_test` (same house rule as
    tests/test_scene_ledger_coverage_gate_pg.py — never a network or
    production host),
  * local psql is on PATH.

The module fixture applies the full draft stack PLUS the ledger gate PLUS the
receipt draft, committed, onto that scratch database. Every scenario is
committed to the disposable scratch database only. Host PostgreSQL here cannot
start (shmget ENOSPC/EPERM): without the DSN every PG test skips and only the
static source contracts run — they never touch a database.

Honesty note: the PG scenarios SIMULATE the claim-path discipline (ledger row
insert + the claim path's own visual_scene_phash_occupied write + capture in
one transaction under the fleet advisory lock, with real read/render receipt
rows and the calendar row's real publish_claim_token). They are NOT the
actual claim path: no claim-wave function calls capture yet (asserted by
test_static_claim_wave_not_wired). That wiring is explicitly outstanding
integration work.
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
RECEIPT = MIGRATIONS / "DRAFT_visual_scene_original_use_receipt_20261004.sql"
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
ADVISORY = ("hashtextextended(jsonb_build_array('visual_scene_global')::text,0)")


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
    BEFORE any committed scenario setup on the scratch DB."""
    if not PG_READY:
        pytest.skip("receipt PG checks need SCENE_RECEIPT_TEST_DSN on a "
                    "disposable local DB and the receipt migration present")
    _check_dsn()
    _fresh_scratch_schema()
    _apply("".join((MIGRATIONS / n).read_text() + "\n" for n in STACK[:-1]))

    # (a) Rollback-only probe: run the receipt body in ONE transaction and
    # roll it back; the public catalog must be byte-identical afterwards.
    before = _catalog_snapshot()
    _apply("begin;\n" + _receipt_body() + "\nrollback;\n")
    assert _catalog_snapshot() == before, \
        "rollback-only install probe left catalog changes behind"

    # (b) Late install over an armed tenant must refuse, with nothing applied.
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
         "public.visual_global_object_read_receipt, "
         "public.visual_global_render_receipt, "
         "public.visual_global_object_attestation, "
         "public.visual_global_scene_object_member, "
         "public.visual_global_object_lineage, "
         "public.visual_group_activation, "
         "public.gym_visual_guard_settings, "
         "public.visual_scene_original_use_receipt cascade")


# ---- seeding helpers (all committed to the scratch DB) -----------------------
# These simulate the claim-path data shapes; they are NOT the claim path.

def _seed_tenant(raw=None):
    tid = str(uuid.uuid4())
    raw = raw or tid
    group = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{raw}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{raw}', '{group}')")
    return tid, raw, group


def _alias(tid, raw, group):
    """Second raw gym key mapping to the SAME canonical tenant."""
    _sql(f"insert into public.tenant_alias(alias_key, tenant_id) "
         f"values ('{raw}', '{tid}')")
    _sql(f"insert into public.visual_group(gym_id, group_key) "
         f"values ('{raw}', '{group}')")


def _seed_calendar_row(tid, group=None):
    """Returns (row_id, publish_claim_token) — the real claim token. `group`
    sets the row's visual_group_key: the claim guard writes occupancy under
    it, and capture now requires it to equal the claimed ledger group."""
    vg = f"'{group}'" if group else "null"
    out = _one(
        "insert into public.content_calendar(gym_id, post_date, status,"
        " published_at, publish_claim_token, visual_group_key) values "
        f"('{tid}', '2026-09-01', 'published', now(), gen_random_uuid(),"
        f" {vg}) "
        "returning id::text || '|' || publish_claim_token::text")
    return tuple(out.split("|"))


def _read_receipt(tid, url, fp):
    return _one(
        "insert into public.visual_global_object_read_receipt"
        "(tenant_id, exact_url, fingerprint, byte_length, acquisition_method,"
        " evidence_ref, observed_by) values "
        f"('{tid}', '{url}', '{fp}', 1024, 'verified_object_read',"
        f" 'scratch-evidence', 'receipt-test') returning receipt_id")


def _attest(tid, raw, group, url, fp, rr):
    _sql("insert into public.visual_global_object_attestation"
         "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
         " acquisition_method, evidence_ref, read_receipt, attested_by) values "
         f"('{url}', '{tid}', '{group}', '{fp}', 1024,"
         f" 'verified_object_read', 'scratch-evidence', '{rr}',"
         " 'receipt-test')")


def _member(tid, group, url, fp, role):
    _sql("insert into public.visual_global_scene_object_member"
         "(tenant_id, group_key, exact_url, fingerprint, object_role) "
         f"values ('{tid}', '{group}', '{url}', '{fp}', '{role}')")


def _seed_object(tid, raw, group):
    """Attested object with a REAL owner read receipt. Source == delivered
    (no render), so no render receipt is needed or allowed."""
    fp = "md5:" + uuid.uuid4().hex
    url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    rr = _read_receipt(tid, url, fp)
    _attest(tid, raw, group, url, fp, rr)
    for role in ("source", "delivered"):
        _member(tid, group, url, fp, role)
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"('{raw}', 'canonical_url', '{url}', '{group}')")
    return url, fp, rr


def _seed_rendered_object(tid, raw, group):
    """Source != delivered: two read receipts chained by a REAL render
    receipt and lineage row. Returns (s_url, s_fp, s_rr, d_url, d_fp, d_rr,
    render_rr)."""
    s_fp = "md5:" + uuid.uuid4().hex
    d_fp = "md5:" + uuid.uuid4().hex
    s_url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    d_url = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    s_rr = _read_receipt(tid, s_url, s_fp)
    d_rr = _read_receipt(tid, d_url, d_fp)
    _attest(tid, raw, group, s_url, s_fp, s_rr)
    _attest(tid, raw, group, d_url, d_fp, d_rr)
    _member(tid, group, s_url, s_fp, "source")
    _member(tid, group, d_url, d_fp, "delivered")
    render_rr = _one(
        "insert into public.visual_global_render_receipt"
        "(tenant_id, source_read_receipt, delivered_read_receipt,"
        " source_exact_url, delivered_exact_url, source_fingerprint,"
        " delivered_fingerprint, operation, evidence_ref, rendered_by) values "
        f"('{tid}', '{s_rr}', '{d_rr}', '{s_url}', '{d_url}', '{s_fp}',"
        f" '{d_fp}', 'render', 'scratch-evidence', 'receipt-test')"
        " returning receipt_id")
    _sql("insert into public.visual_global_object_lineage"
         "(tenant_id, group_key, source_exact_url, delivered_exact_url,"
         " source_fingerprint, delivered_fingerprint, render_receipt) values "
         f"('{tid}', '{group}', '{s_url}', '{d_url}', '{s_fp}', '{d_fp}',"
         f" '{render_rr}')")
    _sql("insert into public.visual_group_alias"
         "(gym_id, alias_kind, alias_value, group_key) values "
         f"('{raw}', 'canonical_url', '{s_url}', '{group}')")
    return s_url, s_fp, s_rr, d_url, d_fp, d_rr, render_rr


def _occupy(tid, group, phash, fp, date="2026-09-01", cal=None):
    _sql("insert into public.visual_scene_phash_occupied"
         "(phash, tenant_id, group_key, used_date, fingerprint,"
         " calendar_row_id, evidence) "
         f"values ('{phash}', '{tid}', '{group}', '{date}', '{fp}', "
         + (f"'{cal}'" if cal else "null") + ", '{}'::jsonb)")


def _ledger(raw, group, date="2026-09-01", state="reserved", cal=None):
    _sql("insert into public.visual_group_usage_ledger"
         "(gym_id, group_key, reserved_date, calendar_row_id, channel, state)"
         f" values ('{raw}', '{group}', "
         + (f"'{date}'" if date else "null") + ", "
         + (f"'{cal}'" if cal else "null") + f", 'ig', '{state}')")


def _capture_payload(tid, raw, group, cal, token, url, fp, phash, rr,
                     s_url=None, s_fp=None, s_rr=None, render_rr=None):
    return {
        "capture_mode": "claim",
        "tenant_id": tid, "group_key": group, "used_date": "2026-09-01",
        "ledger_gym_id": raw,
        "calendar_row_id": cal,
        "claim_attempt_id": token,
        "source_url": s_url or url, "source_md5": s_fp or fp,
        "delivered_url": url, "delivered_md5": fp,
        "delivered_phash": phash,
        "source_read_receipt": s_rr or rr,
        "delivered_read_receipt": rr,
        "render_receipt": render_rr,
    }


def _capture_sql(payload):
    return ("select public.visual_scene_original_use_capture("
            f"'{json.dumps(payload)}'::jsonb)")


def _claim_capture(tid, raw, group, cal, token, url, fp, phash, rr, **kw):
    """SIMULATED claim discipline (not the real claim path): insert the
    ledger row, the claim path's OWN occupancy write (exactly the delivered
    bytes, this calendar row) and the receipt capture in ONE transaction
    holding the fleet advisory lock."""
    payload = _capture_payload(tid, raw, group, cal, token, url, fp, phash,
                               rr, **kw)
    script = (
        "begin;\n"
        f"select pg_advisory_xact_lock({ADVISORY});\n"
        "insert into public.visual_group_usage_ledger"
        "(gym_id, group_key, reserved_date, calendar_row_id, channel, state)"
        f" values ('{raw}', '{group}', '2026-09-01', '{cal}', 'ig',"
        " 'published');\n"
        "insert into public.visual_scene_phash_occupied"
        "(phash, tenant_id, group_key, used_date, fingerprint,"
        " calendar_row_id, evidence) values "
        f"('{phash}', '{tid}', '{group}', '2026-09-01', '{fp}', '{cal}',"
        f" jsonb_build_object('exact_url', '{url}'));\n"
        + _capture_sql(payload) + ";\ncommit;\n")
    return _script(script), payload


def _coverage():
    # Full multiline output: both obligations (raw A resolved, raw B
    # unresolved) must be visible; _one() would keep only the last line.
    return _sql(
        "select coalesce(string_agg(kind||'|'||audit_status||'|'||reason,"
        " E'\\n'), '') from public.visual_scene_ledger_coverage_audit(null)")


# ============================ static contracts ================================

def test_static_draft_off_and_additive():
    text = RECEIPT.read_text()
    assert "DRAFT / UNAPPLIED / OFF" in text
    assert "DRAFT_visual_scene_ledger_coverage_gate_20261004.sql" in text
    lines = text.splitlines()
    assert [ln.strip().lower() for ln in lines].count("begin;") == 1
    assert [ln.strip().lower() for ln in lines].count("commit;") == 1
    assert "create table if not exists public.visual_scene_original_use_receipt" in text
    # No destructive or wide-alter statements against frozen objects.
    assert "drop table" not in text.lower()
    assert "alter table public.visual_group_usage_ledger" not in text.lower()


def test_static_immutable_append_only_owner_only():
    text = RECEIPT.read_text()
    assert "before update or delete or truncate on public.visual_scene_original_use_receipt" in text
    assert "revoke all on public.visual_scene_original_use_receipt" in text
    assert "grant select on public.visual_scene_original_use_receipt to service_role" in text
    assert "revoke all on function public.visual_scene_original_use_capture(jsonb)" in text
    # CAS keys: EXACT RAW identity and the real claim token only.
    assert "unique (ledger_gym_id, group_key, used_date, calendar_row_id)" in text
    assert "unique (claim_attempt_id)" in text
    # The wrongly-broad delivered-object unique key must be gone.
    assert "unique (delivered_url, delivered_md5)" not in text


def test_static_exact_raw_ledger_binding():
    """P0 repair: the capture binds the exact raw ledger PK; the evaluator
    matches the obligation's raw_gym_key."""
    text = RECEIPT.read_text()
    assert "where l.gym_id=v_ledger_gym and l.group_key=v_group" in text
    assert "order by l.gym_id limit 1" not in text
    assert "foreign key (ledger_gym_id, group_key)\n      references public.visual_group_usage_ledger" in text
    assert "r.ledger_gym_id=v_raw" in text
    assert "p_obligation->>'raw_gym_key'" in text


def test_static_no_unattested_evidence():
    """P0 repair: byte-read/render lineage is bound by immutable FK into
    owner-created records; no free-form attestation, no historical mode, no
    free-form provider id."""
    text = RECEIPT.read_text()
    assert "references public.visual_global_object_read_receipt (receipt_id)" in text
    assert "references public.visual_global_render_receipt (receipt_id)" in text
    # No free-form attestation/provider column, variable or parameter remains
    # (the header comment explains their removal; that mention is allowed).
    assert "byte_attestation jsonb" not in text
    assert "byte_attestation  jsonb" not in text
    assert "p->'byte_attestation'" not in text
    assert "provider_attempt_id text" not in text
    assert "p->>'provider_attempt_id'" not in text
    assert "v_provider_attempt" not in text
    assert "check (capture_mode = 'claim')" in text
    assert "no historical path exists" in text
    # The removed historical mode survives only as a removal note in the
    # header comment; it is never an accepted mode or branch.
    assert "capture_mode in" not in text
    assert "v_mode='historical" not in text
    # Real claim token binding, not a caller-invented uuid.
    assert "publish_claim_token" in text
    assert ("claim_attempt_id must equal the calendar row publish_claim_token"
            in text)


def test_static_capture_lock_and_same_transaction_proof():
    text = RECEIPT.read_text()
    assert "original-use receipt capture requires the fleet scene advisory lock" in text
    # P1 repair: epoch-safe same-transaction proof. xmin is a 32-bit xid;
    # txid_current() is the 64-bit epoch-qualified id, so the old raw
    # comparison only held in epoch 0. age(xmin) is PostgreSQL's epoch-aware
    # comparison; the old expression must be gone from executable code.
    assert "age(v_ledger.xmin) <> 0" in text
    assert "v_ledger.xmin::text::bigint <>" not in text
    assert "claim receipt must be captured in the same transaction as the new ledger entry" in text
    # Old entries are never auto-cleared.
    assert "historical_fingerprint_unproven" in text
    assert "original_use_receipt_occupancy_unproven" in text
    assert "'original_use_receipt'" in text


def test_static_causal_byte_binding():
    """P0 repair: capture proves the claimed delivered bytes are the bytes
    THIS claim transaction actually used (the claim path's own same-transaction
    occupancy write for this calendar row), and the evaluator ties occupancy
    to the bound claim's calendar row plus the raw key's CURRENT tenant
    mapping."""
    text = RECEIPT.read_text()
    # Capture-side causal proof.
    assert ("original-use receipt delivered bytes were not recorded by this "
            "claim transaction") in text
    assert "o.calendar_row_id=v_row_id and o.tenant_id=v_tenant" in text
    # P1 repair: epoch-safe same-transaction proof (see lock test above).
    assert "age(o.xmin) = 0" in text
    assert "o.xmin::text::bigint = txid_current()" not in text
    # P1 repair: occupancy group must equal BOTH the exact claimed ledger
    # group and the calendar row's visual_group_key (the group the claim
    # guard writes occupancy under) — never a scene-linked sibling.
    assert "o.group_key=v_group and o.group_key=v_row.visual_group_key" in text
    assert "o.evidence->>'exact_url'=v_delivered_url" in text
    # Calendar row identity agreement (tenant/date).
    assert ("calendar row identity does not match the claimed tenant/date"
            in text)
    assert "v_row.post_date is distinct from v_date" in text
    # Evaluator-side: current canonical mapping + the bound claim's own
    # occupancy row, never an unrelated occupancy.
    assert "public.visual_group_tenant_id(r.ledger_gym_id)::text=o_tenant" in text
    assert "o.calendar_row_id=v_receipt.calendar_row_id" in text
    # P1 repair: the evaluator's receipt path binds occupancy to the
    # receipt's EXACT claimed ledger group, never scene-linked siblings.
    assert "o.group_key=v_receipt.group_key" in text


def test_static_late_install_refusal_and_preserved_evaluator():
    text = RECEIPT.read_text()
    assert "must be installed before any tenant is armed" in text
    # Evaluator keeps the gate's fail-closed paths verbatim.
    for kept in ("missing_date", "unmapped_tenant", "calendar_row_proof",
                 "calendar_row_identity_mismatch", "scene_occupancy_proof",
                 "fingerprint_mismatch", "no_scene_occupancy_proof"):
        assert kept in text
    # Frozen claim-wave functions are NOT replaced by this draft.
    assert "visual_scene_claim_guard" not in text
    assert "visual_scene_history_backfill_locked" not in text


def test_static_claim_wave_not_wired():
    """P0 honesty: NO claim-path wiring exists yet. Neither the claim-wave
    draft nor this draft calls capture from the claim path; docs must not
    call this integrated."""
    wave = CLAIM_WAVE.read_text() if CLAIM_WAVE.exists() else ""
    assert "visual_scene_original_use_capture" not in wave
    doc = (Path(__file__).resolve().parents[1]
           / "docs" / "SCENE_ORIGINAL_USE_RECEIPT.md").read_text()
    assert "NOT integrated" in doc
    assert "activation NOT ready" in doc


# ============================ PG scenarios ===================================

def test_pg_old_ledger_entry_never_cleared_without_receipt(scratch):
    """No false historical clearance: a fingerprint-free ledger entry plus
    same group/date occupancy but NO receipt stays unresolved exactly as the
    ledger gate left it."""
    tid, raw, group = _seed_tenant()
    cal, _token = _seed_calendar_row(tid, group)
    url, fp, _rr = _seed_object(tid, raw, group)
    _ledger(raw, group, state="published", cal=cal)
    _occupy(tid, group, "0000000000000001", fp, cal=cal)
    out = _coverage()
    assert "group_usage_ledger|unresolved|historical_fingerprint_unproven" in out


def test_pg_claim_capture_resolves_with_real_occupancy(scratch):
    """Simulated claim discipline: receipt captured in the ledger-creation
    transaction + occupancy over the receipt's exact delivered bytes resolves
    the fingerprint-free obligation."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _claim_capture(tid, raw, group, cal, token, url, fp, "0000000000000001", rr)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out


def test_pg_rendered_capture_resolves_with_chained_render_receipt(scratch):
    """Source != delivered: capture requires and verifies a REAL render
    receipt chaining the two read receipts; resolution uses delivered bytes."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    s_url, s_fp, s_rr, d_url, d_fp, d_rr, render_rr = \
        _seed_rendered_object(tid, raw, group)
    _claim_capture(tid, raw, group, cal, token, d_url, d_fp,
                   "0000000000000001", d_rr,
                   s_url=s_url, s_fp=s_fp, s_rr=s_rr, render_rr=render_rr)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out
    # Source bytes occupying instead of delivered bytes is not proof.
    _sql("set session_replication_role = replica; "
         "truncate public.visual_scene_phash_occupied cascade")
    _occupy(tid, group, "0000000000000001", s_fp, cal=cal)
    out = _coverage()
    assert "group_usage_ledger|unresolved|original_use_receipt_occupancy_unproven" in out


def test_pg_render_receipt_required_when_bytes_differ(scratch):
    """Distinct source/delivered bytes without a chaining render receipt are
    refused — render lineage can never be free-form."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    s_url, s_fp, s_rr, d_url, d_fp, d_rr, _render_rr = \
        _seed_rendered_object(tid, raw, group)
    payload = _capture_payload(tid, raw, group, cal, token, d_url, d_fp,
                               "0000000000000001", d_rr,
                               s_url=s_url, s_fp=s_fp, s_rr=s_rr,
                               render_rr=None)
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   "insert into public.visual_group_usage_ledger"
                   "(gym_id, group_key, reserved_date, calendar_row_id,"
                   f" channel, state) values ('{raw}', '{group}',"
                   f" '2026-09-01', '{cal}', 'ig', 'published');\n"
                   # The claim path's OWN same-transaction occupancy write,
                   # over EXACTLY the claimed delivered bytes — otherwise the
                   # causal-binding precondition fires before the render
                   # lineage check this test exercises.
                   "insert into public.visual_scene_phash_occupied"
                   "(phash, tenant_id, group_key, used_date, fingerprint,"
                   " calendar_row_id, evidence) values "
                   f"('0000000000000001', '{tid}', '{group}', '2026-09-01',"
                   f" '{d_fp}', '{cal}',"
                   f" jsonb_build_object('exact_url', '{d_url}'));\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert "render lineage is not bound" in done.stderr


def test_pg_replay_is_refused(scratch):
    """CAS: replaying the same identity or the same claim token fails."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _done, payload = _claim_capture(tid, raw, group, cal, token, url, fp,
                                    "0000000000000001", rr)
    # Replay same identity: the ledger row is now pre-existing (same-tx proof
    # fails) and the identity/claim-token CAS also refuses.
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert ("same transaction as the new ledger entry" in done.stderr
            or "already exists" in done.stderr)


def test_pg_repeat_delivered_object_blocked(scratch):
    """Global no-repeat: visual_global_object_attestation has exact_url as
    its PRIMARY KEY, so reusing an already-delivered exact URL for a second
    tenant's claim is blocked outright — the second attestation (and hence
    the second capture) can never mint an original-use receipt for bytes
    already delivered. A previously drafted 'second use is allowed' case
    assumed an illegitimate reuse; the schema forbids it."""
    tid_a, raw_a, group_a = _seed_tenant()
    tid_b, raw_b, group_b = _seed_tenant()
    cal_a, token_a = _seed_calendar_row(tid_a, group_a)
    url, fp, rr_a = _seed_object(tid_a, raw_a, group_a)
    _claim_capture(tid_a, raw_a, group_a, cal_a, token_a, url, fp,
                   "0000000000000001", rr_a)
    # Tenant B cannot re-attest the SAME exact delivered URL.
    rr_b = _read_receipt(tid_b, url, fp)
    done = _run(
        "insert into public.visual_global_object_attestation"
        "(exact_url, tenant_id, group_key, fingerprint, byte_length,"
        " acquisition_method, evidence_ref, read_receipt, attested_by) values "
        f"('{url}', '{tid_b}', '{group_b}', '{fp}', 1024,"
        " 'verified_object_read', 'scratch-evidence', "
        f"'{rr_b}', 'receipt-test')", check=False)
    assert done.returncode != 0
    # The no-repeat is the global primary key on exact_url (schema contract).
    text = (MIGRATIONS / "DRAFT_visual_global_history_20261002.sql").read_text()
    assert "exact_url text primary key" in text
    n = _one("select count(*) from public.visual_scene_original_use_receipt")
    assert n == "1"


def test_pg_swap_keeps_permanent_original_occupancy(scratch):
    """Media swap: scene occupancy is PERMANENT (append-only, never freed on
    swap), so the bound claim's occupancy over the ORIGINAL bytes survives
    and the receipt resolves the obligation — that is exactly the evidence
    class this package adds. The replacement bytes occupying too does not
    weaken the proof (the evaluator requires the receipt's exact bytes)."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _claim_capture(tid, raw, group, cal, token, url, fp, "0000000000000001", rr)
    # Post-swap replacement media also occupies the scene (different bytes).
    new_fp = "md5:" + uuid.uuid4().hex
    _occupy(tid, group, "0000000000000002", new_fp, cal=cal)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out


def test_pg_receipt_without_bound_occupancy_stays_unresolved(scratch):
    """Fail-closed branch: a receipt whose bound claim's occupancy row is
    absent (scratch-only trigger bypass simulates the pre-integration state
    where no claim ever recorded those bytes) and occupancy covers only
    OTHER bytes stays unresolved."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _claim_capture(tid, raw, group, cal, token, url, fp, "0000000000000001", rr)
    _sql("set session_replication_role = replica; "
         "truncate public.visual_scene_phash_occupied cascade")
    new_fp = "md5:" + uuid.uuid4().hex
    _occupy(tid, group, "0000000000000002", new_fp, cal=cal)
    out = _coverage()
    assert "group_usage_ledger|unresolved|original_use_receipt_occupancy_unproven" in out
    # Same phash but different fingerprint is also not proof.
    _sql("set session_replication_role = replica; "
         "truncate public.visual_scene_phash_occupied cascade")
    _occupy(tid, group, "0000000000000001", new_fp, cal=cal)
    out = _coverage()
    assert "group_usage_ledger|unresolved|original_use_receipt_occupancy_unproven" in out


def test_pg_capture_refuses_bytes_this_claim_did_not_use(scratch):
    """P0 adversarial: the caller holds REAL owner read receipts for bytes Y
    but the claim transaction's own occupancy write recorded DIFFERENT bytes
    X (what the claim actually used). Claiming Y must be refused — the
    receipt can never bind bytes the claim did not use."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    # Bytes X: what the claim path actually scanned and recorded.
    url_x, fp_x, _rr_x = _seed_object(tid, raw, group)
    # Bytes Y: unrelated bytes with a REAL owner read receipt for this tenant.
    fp_y = "md5:" + uuid.uuid4().hex
    url_y = f"https://scratch.example/{uuid.uuid4().hex}.jpg"
    rr_y = _read_receipt(tid, url_y, fp_y)
    payload = _capture_payload(tid, raw, group, cal, token, url_y, fp_y,
                               "0000000000000001", rr_y)
    script = ("begin;\n"
              f"select pg_advisory_xact_lock({ADVISORY});\n"
              "insert into public.visual_group_usage_ledger"
              "(gym_id, group_key, reserved_date, calendar_row_id, channel,"
              f" state) values ('{raw}', '{group}', '2026-09-01', '{cal}',"
              " 'ig', 'published');\n"
              # The claim path's OWN occupancy write: bytes X, not Y.
              "insert into public.visual_scene_phash_occupied"
              "(phash, tenant_id, group_key, used_date, fingerprint,"
              " calendar_row_id, evidence) values "
              f"('0000000000000001', '{tid}', '{group}', '2026-09-01',"
              f" '{fp_x}', '{cal}',"
              f" jsonb_build_object('exact_url', '{url_x}'));\n"
              + _capture_sql(payload) + ";\ncommit;\n")
    done = _script(script, check=False)
    assert done.returncode != 0
    assert "not recorded by this claim transaction" in done.stderr
    assert _one("select count(*) from "
                "public.visual_scene_original_use_receipt") == "0"


def test_pg_unrelated_occupancy_same_bytes_does_not_resolve(scratch):
    """P0 adversarial: after capture, an occupancy row over the SAME bytes,
    tenant, date and group but written for ANOTHER calendar row (another
    claim's occupancy) must NOT resolve the obligation — the evaluator ties
    occupancy to the bound claim's calendar row only."""
    tid, raw, group = _seed_tenant()
    cal_a, token_a = _seed_calendar_row(tid, group)
    cal_b, _token_b = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _claim_capture(tid, raw, group, cal_a, token_a, url, fp,
                   "0000000000000001", rr)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out
    # Replace the bound claim's occupancy with an UNRELATED row: same bytes/
    # tenant/date/group, but calendar_row_id = cal_b (scratch-only bypass).
    _sql("set session_replication_role = replica; "
         "truncate public.visual_scene_phash_occupied cascade")
    _occupy(tid, group, "0000000000000001", fp, cal=cal_b)
    out = _coverage()
    assert "group_usage_ledger|unresolved|original_use_receipt_occupancy_unproven" in out


def test_pg_wrong_group_sibling_occupancy_never_proves(scratch):
    """P1 adversarial (Terra review): occupancy recorded for a scene-LINKED
    SIBLING group over the SAME calendar row and SAME bytes must never mint
    or satisfy a receipt. The claim guard writes occupancy with group_key =
    p_row.visual_group_key; capture requires occupancy.group_key to equal
    BOTH the claimed ledger group and the row's visual_group_key, and the
    evaluator requires occupancy.group_key = receipt.group_key EXACTLY."""
    tid, raw, group_a = _seed_tenant()
    group_b = "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.visual_group(gym_id, group_key) values "
         f"('{raw}', '{group_b}')")
    # Link the groups into ONE scene: under the pre-repair evaluator a
    # sibling group's occupancy row would have wrongly cleared.
    lo, hi = sorted((group_a, group_b))
    _sql("insert into public.visual_group_scene_link"
         "(gym_id, group_key_a, group_key_b, evidence, created_by) values "
         f"('{raw}', '{lo}', '{hi}', '{{}}'::jsonb, 'receipt-test')")
    cal, token = _seed_calendar_row(tid, group_a)
    url, fp, rr = _seed_object(tid, raw, group_a)
    # (a) Capture side: the claim path's occupancy written for the SIBLING
    # group (same row, same bytes) must be refused — rolled back.
    payload = _capture_payload(tid, raw, group_a, cal, token, url, fp,
                               "0000000000000001", rr)
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   "insert into public.visual_group_usage_ledger"
                   "(gym_id, group_key, reserved_date, calendar_row_id,"
                   f" channel, state) values ('{raw}', '{group_a}',"
                   f" '2026-09-01', '{cal}', 'ig', 'published');\n"
                   "insert into public.visual_scene_phash_occupied"
                   "(phash, tenant_id, group_key, used_date, fingerprint,"
                   " calendar_row_id, evidence) values "
                   f"('0000000000000001', '{tid}', '{group_b}', '2026-09-01',"
                   f" '{fp}', '{cal}',"
                   f" jsonb_build_object('exact_url', '{url}'));\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert "not recorded by this claim transaction" in done.stderr
    # (b) Evaluator side: a validly captured receipt must NOT be satisfied
    # by sibling-group occupancy over the same row/bytes either.
    _claim_capture(tid, raw, group_a, cal, token, url, fp,
                   "0000000000000001", rr)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out
    _sql("set session_replication_role = replica; "
         "truncate public.visual_scene_phash_occupied cascade")
    _occupy(tid, group_b, "0000000000000001", fp, cal=cal)
    out = _coverage()
    assert "group_usage_ledger|unresolved|original_use_receipt_occupancy_unproven" in out


def test_pg_alias_remap_is_blocked_and_receipt_stays_bound(scratch):
    """P0 adversarial, honestly staged: the alias-remap attack cannot even
    be attempted — tenant_alias is IMMUTABLE (identity trigger), so raw A
    can never be remapped to tenant B to launder a receipt minted under
    tenant A. Assert the remap mutation itself is refused; tenant B then
    records occupancy over the exact same bytes, and the original receipt
    stays bound to raw A with the obligation still resolved."""
    tid_a, raw_a, group_a = _seed_tenant()
    tid_b, _raw_b, _group_b = _seed_tenant()
    cal_a, token_a = _seed_calendar_row(tid_a, group_a)
    url, fp, rr_a = _seed_object(tid_a, raw_a, group_a)
    _claim_capture(tid_a, raw_a, group_a, cal_a, token_a, url, fp,
                   "0000000000000001", rr_a)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out
    # The remap attack is blocked at the schema: identity bindings are
    # immutable — no UPDATE can silently retarget an existing alias.
    done = _run(f"update public.tenant_alias set tenant_id='{tid_b}' "
                f"where alias_key='{raw_a}'", check=False)
    assert done.returncode != 0
    assert "visual identity and decision history are immutable" in done.stderr
    # Tenant B even has the group and occupancy over the exact same bytes:
    # same bytes + same group/date cannot disturb the bound receipt.
    _sql(f"insert into public.visual_group(gym_id, group_key) values "
         f"('{tid_b}', '{group_a}')")
    _occupy(tid_b, group_a, "0000000000000001", fp)
    # The original receipt is still the only receipt and raw A's
    # obligation stays resolved by its own bound claim.
    n = _one("select count(*) from public.visual_scene_original_use_receipt")
    assert n == "1"
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out


def test_pg_cross_tenant_receipt_never_clears(scratch):
    """A receipt captured by tenant A never resolves tenant B's obligation,
    even with identical group/date/bytes."""
    tid_a, raw_a, group_a = _seed_tenant()
    tid_b, raw_b, group_b = _seed_tenant()
    cal_a, token_a = _seed_calendar_row(tid_a, group_a)
    cal_b, _token_b = _seed_calendar_row(tid_b, group_b)
    url, fp, rr_a = _seed_object(tid_a, raw_a, group_a)
    _claim_capture(tid_a, raw_a, group_a, cal_a, token_a, url, fp,
                   "0000000000000001", rr_a)
    _ledger(raw_b, group_b, state="published", cal=cal_b)
    _occupy(tid_b, group_b, "0000000000000001", fp, cal=cal_b)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out
    rows = [r for r in out.splitlines() if r]
    b_rows = [r for r in rows if "historical_fingerprint_unproven" in r]
    assert b_rows, out


def test_pg_raw_gym_key_binding(scratch):
    """P0 repair: two raw gym keys mapping to the SAME canonical tenant. The
    receipt bound to raw A's ledger row resolves ONLY raw A's obligation;
    raw B's identical-looking obligation stays unresolved."""
    tid, raw_a, group_a = _seed_tenant()
    raw_b = str(uuid.uuid4())
    # Same canonical tenant AND group: raw B is a second ledger key for the
    # same tenant/group, so group A's occupancy row already covers it.
    group_b = group_a
    _alias(tid, raw_b, group_b)
    cal_a, token_a = _seed_calendar_row(tid, group_a)
    cal_b, _token_b = _seed_calendar_row(tid, group_a)
    url, fp, rr_a = _seed_object(tid, raw_a, group_a)
    _claim_capture(tid, raw_a, group_a, cal_a, token_a, url, fp,
                   "0000000000000001", rr_a)
    # Raw B: same canonical tenant/group, same-date ledger entry — but NO
    # receipt bound to raw B's ledger key, and no duplicate occupancy row.
    _ledger(raw_b, group_b, state="published", cal=cal_b)
    out = _coverage()
    assert "group_usage_ledger|resolved|original_use_receipt" in out
    unresolved = [r for r in out.splitlines()
                  if r == "group_usage_ledger|unresolved|"
                           "historical_fingerprint_unproven"]
    assert len(unresolved) == 1, out


def test_pg_capture_refuses_wrong_raw_ledger_key(scratch):
    """P0 repair: capture addressed at a DIFFERENT raw ledger key is refused
    even when the canonical tenant/group matches another real ledger row."""
    tid, raw_a, group_a = _seed_tenant()
    raw_b = str(uuid.uuid4())
    group_b = "vg_" + uuid.uuid4().hex[:12]
    _alias(tid, raw_b, group_b)
    cal, token = _seed_calendar_row(tid, group_a)
    url, fp, rr = _seed_object(tid, raw_a, group_a)
    payload = _capture_payload(tid, raw_b, group_a, cal, token, url, fp,
                               "0000000000000001", rr)
    script = ("begin;\n"
              f"select pg_advisory_xact_lock({ADVISORY});\n"
              "insert into public.visual_group_usage_ledger"
              "(gym_id, group_key, reserved_date, calendar_row_id, channel,"
              f" state) values ('{raw_a}', '{group_a}', '2026-09-01',"
              f" '{cal}', 'ig', 'published');\n"
              + _capture_sql(payload) + ";\ncommit;\n")
    done = _script(script, check=False)
    assert done.returncode != 0
    assert "does not match the exact ledger row" in done.stderr


def test_pg_capture_refuses_invented_claim_token(scratch):
    """P0 repair: a caller-invented claim_attempt_id that is not the calendar
    row's real publish_claim_token is refused."""
    tid, raw, group = _seed_tenant()
    cal, _token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    payload = _capture_payload(tid, raw, group, cal, str(uuid.uuid4()),
                               url, fp, "0000000000000001", rr)
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   "insert into public.visual_group_usage_ledger"
                   "(gym_id, group_key, reserved_date, calendar_row_id,"
                   f" channel, state) values ('{raw}', '{group}',"
                   f" '2026-09-01', '{cal}', 'ig', 'published');\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert "publish_claim_token" in done.stderr


def test_pg_capture_refuses_unbound_byte_evidence(scratch):
    """P0 repair: read-receipt ids that do not name owner-created records
    matching the claimed tenant/url/fingerprint are refused — free-form
    byte evidence can never mint a receipt."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    payload = _capture_payload(tid, raw, group, cal, token, url, fp,
                               "0000000000000001", str(uuid.uuid4()))
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   "insert into public.visual_group_usage_ledger"
                   "(gym_id, group_key, reserved_date, calendar_row_id,"
                   f" channel, state) values ('{raw}', '{group}',"
                   f" '2026-09-01', '{cal}', 'ig', 'published');\n"
                   # The claim path's OWN same-transaction occupancy write,
                   # over EXACTLY the claimed delivered bytes — otherwise the
                   # causal-binding precondition fires before the read-receipt
                   # FK check this test exercises.
                   "insert into public.visual_scene_phash_occupied"
                   "(phash, tenant_id, group_key, used_date, fingerprint,"
                   " calendar_row_id, evidence) values "
                   f"('0000000000000001', '{tid}', '{group}', '2026-09-01',"
                   f" '{fp}', '{cal}',"
                   f" jsonb_build_object('exact_url', '{url}'));\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert "not bound to an owner read receipt" in done.stderr


def test_pg_historical_mode_is_refused(scratch):
    """P0 repair: there is NO historical capture mode. Any non-claim mode —
    including the removed 'historical_verified' — is refused outright, so
    unattested historical 'proof' can never fabricate clearance."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _ledger(raw, group, state="published", cal=cal)
    payload = _capture_payload(tid, raw, group, cal, token, url, fp,
                               "0000000000000001", rr)
    payload["capture_mode"] = "historical_verified"
    payload["operator_attestation"] = "op-review-2026-10-04"
    payload["verification"] = {"send_import_proof": "zernio-export-row-7"}
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert "no historical path exists" in done.stderr
    # And the old obligation stays unresolved exactly as the gate left it.
    out = _coverage()
    assert "group_usage_ledger|unresolved|historical_fingerprint_unproven" in out


def test_pg_capture_requires_advisory_lock(scratch):
    """Concurrency/lock failure: without the fleet advisory lock the capture
    is refused, even with otherwise valid evidence."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    payload = _capture_payload(tid, raw, group, cal, token, url, fp,
                               "0000000000000001", rr)
    script = ("begin;\n"
              "insert into public.visual_group_usage_ledger"
              "(gym_id, group_key, reserved_date, calendar_row_id, channel,"
              f" state) values ('{raw}', '{group}', '2026-09-01', '{cal}',"
              " 'ig', 'published');\n"
              + _capture_sql(payload) + ";\ncommit;\n")
    done = _script(script, check=False)
    assert done.returncode != 0
    assert "requires the fleet scene advisory lock" in done.stderr


def test_pg_claim_capture_requires_same_transaction_ledger(scratch):
    """A pre-existing ledger row (an old entry) can never mint a claim-mode
    receipt: the same-transaction xmin proof fails closed."""
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _ledger(raw, group, state="published", cal=cal)  # committed earlier
    payload = _capture_payload(tid, raw, group, cal, token, url, fp,
                               "0000000000000001", rr)
    done = _script("begin;\n" f"select pg_advisory_xact_lock({ADVISORY});\n"
                   + _capture_sql(payload) + ";\ncommit;\n", check=False)
    assert done.returncode != 0
    assert "same transaction as the new ledger entry" in done.stderr


def test_pg_receipt_is_immutable(scratch):
    tid, raw, group = _seed_tenant()
    cal, token = _seed_calendar_row(tid, group)
    url, fp, rr = _seed_object(tid, raw, group)
    _claim_capture(tid, raw, group, cal, token, url, fp, "0000000000000001", rr)
    for stmt in ("update public.visual_scene_original_use_receipt "
                 "set delivered_phash='ffffffffffffffff'",
                 "delete from public.visual_scene_original_use_receipt"):
        done = _run(stmt, check=False)
        assert done.returncode != 0
        assert "append-only and immutable" in done.stderr
