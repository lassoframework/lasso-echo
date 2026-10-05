"""Contracts for migrations/portal_action_receipt_draft_20261004.sql (v3).

Static contract tests always run. The live tests spin up a DISPOSABLE
PostgreSQL 17 instance (initdb into a mkdtemp dir, Unix socket only, no
network listener) and execute the real migration against a minimal
content_calendar/media_asset schema including the foundation
content_calendar.logical_post_id column. They never touch a network or
production host; if local PG17 binaries are missing they skip.
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "migrations" / "portal_action_receipt_draft_20261004.sql").read_text()
LOW = SQL.lower()

PGBIN_CANDIDATES = [Path("/opt/homebrew/opt/postgresql@17/bin"),
                    Path("/usr/local/opt/postgresql@17/bin")]


def _pgbin():
    for cand in PGBIN_CANDIDATES:
        if (cand / "postgres").exists() and (cand / "initdb").exists():
            return cand
    return None


# --------------------------------------------------------------------------
# Static release contracts (always run)
# --------------------------------------------------------------------------

def test_begin_is_conflict_safe_tenant_scoped_and_requires_logical_post():
    assert "on conflict (gym_id, action_id) do nothing" in LOW
    begin = LOW.split("function public.portal_action_receipt_begin(", 1)[1]
    assert "for update" in begin
    assert "conflicting reuse of action_id" in begin
    assert "request_fingerprint is distinct from p_request_fingerprint" in begin
    # binds post + actor, never a mutable current-media fingerprint
    assert "v_receipt.row_id is distinct from p_row_id" in begin
    assert "v_receipt.actor_id is distinct from" in begin
    # NULL logical_post_id (historical) is held for manual review, never grouped
    assert "primary row has no logical_post_id; conflicting request held for manual review" in begin
    assert "select c.* into v_row" in begin
    assert "v_logical := v_row.logical_post_id" in begin


def test_begin_has_no_caller_before_state_and_captures_itself():
    # Portal PR750 sends only action_id: begin takes NO p_before_state, the
    # existing-receipt conflict/replay check runs BEFORE any calendar lookup,
    # and on a genuinely new action SQL captures the persisted before_state
    # itself in the same transaction as the insert.
    begin = LOW.split("function public.portal_action_receipt_begin(", 1)[1]
    begin = begin.split("$$;", 1)[0]
    assert "p_before_state" not in begin
    assert "p_gym_id text, p_action_id text, p_action text, p_row_id uuid," in begin
    assert "p_actor_id text, p_request_fingerprint text" in begin
    assert "jsonb" not in begin.split(") returns", 1)[0]
    # conflict/replay check precedes the calendar read
    assert begin.index("conflicting reuse of action_id") < \
        begin.index("from public.content_calendar")
    # and the insert (with the captured snapshot) follows that same read
    assert begin.index("from public.content_calendar") < begin.index("insert into public.portal_action_receipt")
    for field in ("'status', v_row.status", "'caption', v_row.caption",
                  "'image_url', v_row.image_url", "'thumbnail_url', v_row.thumbnail_url",
                  "'source_media_url', v_row.source_media_url",
                  "'source_media_asset_id', v_row.source_media_asset_id"):
        assert field in begin


def test_claim_is_exclusive_cas_with_winner_replay_and_frozen_manifest():
    claim = LOW.split("function public.portal_action_receipt_claim_selection(", 1)[1]
    assert "for update" in claim
    assert "and selected_asset is null" in claim
    assert "status in ('started', 'uncertain')" in claim
    # loser receives the winner's stored selection
    assert "if v_receipt.status in ('succeeded', 'failed') or v_receipt.selected_asset is not null then" in claim
    # frozen selection + manifest backstops
    assert "a stored selection is frozen and cannot be overwritten" in LOW
    assert "the frozen member manifest cannot be rewritten" in LOW
    # exact active group: (gym_id, logical_post_id, variant_status='active')
    assert "and c.logical_post_id = v_logical" in claim
    assert "and c.variant_status = 'active'" in claim
    assert "member_manifest = v_manifest" in claim
    # per-row identity + before-state captured at selection
    for field in ("'format', c.format", "'account', c.account", "'status', c.status",
                  "'caption', c.caption", "'image_url', c.image_url",
                  "'thumbnail_url', c.thumbnail_url", "'source_media_url', c.source_media_url",
                  "'source_media_asset_id', c.source_media_asset_id"):
        assert field in claim
    # own-tenant allowlist + no query/fragment/token identity
    assert "from public.media_asset" in claim
    assert "excluded_by_coach is not true" in claim
    # the candidate must REMAIN eligible (null/false fail closed), the asset
    # row is locked through the CAS, and 'local:' library provenance is the
    # only asset id without a media_asset row
    assert "eligible is true" in claim
    assert "for update;" in claim
    assert "not like 'local:%'" in claim
    # hosted-URL tenant binding to the trusted configured origin: a valid
    # own asset id cannot launder a cross-gym or foreign-host public URL
    # (strpos-anchored literal prefix, not LIKE: '_' is a LIKE wildcard)
    assert "portal_action_receipt_hosted_url(p_selected_asset->>'image_url', v_origin, v_slug)" in claim
    assert "portal_action_receipt_hosted_url(v_media->>'image_url', v_origin, v_slug)" in claim
    assert "planned sibling url is not under this tenant''s hosted media prefix" in claim
    assert "^https://[^?#[:space:]]+$" in LOW
    # no picker dict / local path / raw response: strict key allowlists
    assert "'asset_id', 'image_url', 'source_media_url'," in claim
    assert "'image_url', 'source_media_url'," in claim


def test_no_date_or_media_group_inference_anywhere():
    # the rejected v2 inference must be gone entirely
    assert "c.post_date = v_post_date" not in LOW
    assert "same persisted media identity" not in LOW
    assert "visual_group_swap_siblings_media" not in LOW
    apply = LOW.split("function public.portal_action_receipt_apply(", 1)[1]
    assert "active logical-post membership changed since selection" in apply
    assert "prepared rows must match the frozen member manifest exactly" in apply


def test_apply_is_one_transaction_with_documented_phantom_exclusion():
    apply = LOW.split("function public.portal_action_receipt_apply(", 1)[1]
    assert "lock table public.content_calendar in share row exclusive mode" in apply
    assert "phantom exclusion" in apply
    assert "order by id" in apply  # deterministic row lock order
    # receipt lock precedes the calendar barrier (lock ordering contract)
    assert apply.index("for update;  -- receipt lock first") < \
        apply.index("lock table public.content_calendar in share row exclusive mode") < \
        apply.index("deterministic row locks, id ascending")
    # exact current member set re-verified against the frozen manifest
    assert "v_current_ids is distinct from v_ids" in apply
    assert "and c.logical_post_id = v_logical" in apply
    assert "and c.variant_status = 'active'" in apply
    # per-row CAS against each row's OWN manifest before-state
    assert "v_row.status is distinct from (v_member->>'status')" in apply
    assert "v_row.image_url is distinct from (v_member->>'image_url')" in apply
    assert "v_row.thumbnail_url is distinct from (v_member->>'thumbnail_url')" in apply
    assert "v_row.source_media_asset_id is distinct from (v_member->>'source_media_asset_id')" in apply
    # approved/publishing/published + media-held rows never mutated
    assert "status in ('pending', 'coach_review')" in apply
    assert "published_at is null and late_post_id is null" in apply
    assert "publish_claim_token is null" in apply
    assert "media_not_ready_reason is null" in apply
    # stale sibling / media hold aborts the whole group
    assert "stale eligible sibling or media hold on row" in apply
    assert "stale sibling row % aborted the swap group" in apply
    # frozen account/format/post_date row identity is part of the CAS
    assert "v_row.account is distinct from (v_member->>'account')" in apply
    assert "v_row.format is distinct from (v_member->>'format')" in apply
    assert "v_row.post_date is distinct from (v_member->>'post_date')::date" in apply
    assert "and account is not distinct from (v_member->>'account')" in apply
    assert "and format is not distinct from (v_member->>'format')" in apply
    assert "and post_date is not distinct from (v_member->>'post_date')::date" in apply
    # prepared media must equal the frozen per-row intended identity, checked
    # in a no-write validation phase before any mutation, with own-tenant
    # asset ownership and clean public URLs for image/source/thumbnail
    assert apply.index("validation phase (no writes)") < apply.index("write phase")
    assert "prepared media for row % does not match the frozen selection" in apply
    assert "prepared media for row % is not an own-tenant allowlisted media asset" in apply
    # eligibility is re-verified at APPLY (a flip between claim and apply
    # aborts the group) with the asset row locked FOR SHARE through the
    # calendar writes, so a concurrent coach flip is serialized or refused;
    # 'local:' provenance skips the media_asset lookup
    assert "eligible is true" in apply
    assert "for share" in apply
    assert apply.index("for share") < apply.index("write phase")
    assert "not like 'local:%'" in apply
    assert "from jsonb_each(v_receipt.planned_siblings)" in apply
    assert "v_media->>'image_url' is distinct from (v_intended->>'image_url')" in apply
    assert "v_media->>'source_media_asset_id' is distinct from (v_intended->>'source_media_asset_id')" in apply
    assert "not public.portal_action_receipt_clean_url(v_media->>'source_media_url')" in apply
    assert "not public.portal_action_receipt_clean_url(v_media->>'thumbnail_url')" in apply
    # post-write persisted re-read: a soft BEFORE UPDATE trigger altering NEW
    # leaves ROW_COUNT = 1, so the persisted row itself is asserted
    assert "post-write drift on row % aborted the swap group" in apply
    assert "select * into v_persisted from public.content_calendar where id = v_id" in apply
    assert "v_persisted.image_url is distinct from (v_media->>'image_url')" in apply
    assert "v_persisted.media_not_ready_reason is not null" in apply
    assert "v_persisted.publish_claim_token is not null" in apply
    assert "v_persisted.variant_status is distinct from 'active'" in apply
    # terminal success persisted in the same transaction; replay idempotent
    assert "set status = 'succeeded'" in apply
    assert "if v_receipt.status in ('succeeded', 'failed') then\n    return v_receipt;" in apply
    # never finalizes failed from a row read
    assert "set status = 'failed'" not in apply


def test_claim_binds_urls_to_trusted_configured_origin_fail_closed():
    # locked-down config table, EMPTY by default: no host is hardcoded or
    # seeded by the migration itself (environment origins differ)
    assert "create table if not exists public.portal_action_receipt_config" in LOW
    assert all(line.strip().startswith("--")
               for line in LOW.splitlines()
               if "insert into public.portal_action_receipt_config" in line)
    cfg = LOW.split("create table if not exists public.portal_action_receipt_config", 1)[1]
    cfg = cfg.split(";", 1)[0]
    assert "primary key" in cfg and "'public_origin'" in cfg
    # bare origin only: https, lowercase host, optional port, no
    # userinfo/path/query/fragment/trailing slash
    assert "^https://[a-z0-9.-]+(:[0-9]+)?$" in cfg
    assert "revoke all on public.portal_action_receipt_config\n  from public, anon, authenticated, service_role" in LOW
    # claim fails CLOSED while the origin is unset or malformed
    claim = LOW.split("function public.portal_action_receipt_claim_selection(", 1)[1]
    assert "no trusted public media origin configured; refusing closed" in claim
    assert claim.index("no trusted public media origin configured") < \
        claim.index("selected asset must be an allowlisted public object identity")
    # the validator anchors the LITERAL origin+tenant prefix at position 1
    fn = LOW.split("function public.portal_action_receipt_hosted_url(", 1)[1]
    fn = fn.split("$$;", 1)[0]
    assert "strpos(p_url, p_origin || '/echo/' || p_slug || '/') = 1" in fn
    assert "strpos(p_url, p_origin || '/echo/' || p_slug || '_ig/') = 1" in fn
    # anchored path = 16-hex content-address segment + file, no traversal
    assert "^[0-9a-f]{16}/[^/?#[:space:]]+$" in fn
    assert "strpos(p_url, '..') = 0" in fn
    assert "strpos(lower(p_url), '%2e') = 0" in fn
    assert "strpos(lower(p_url), '%2f') = 0" in fn
    # service-only helper, same as clean_url
    assert "revoke all on function public.portal_action_receipt_hosted_url(text, text, text)\n  from public, anon, authenticated, service_role" in LOW


def test_no_arbitrary_response_json_and_service_role_only():
    assert "response jsonb" not in LOW
    assert "response_status integer" in LOW
    assert "enable row level security" in LOW
    for fn in ("portal_action_receipt_begin(text, text, text, uuid, text, text)",
               "portal_action_receipt_claim_selection(text, text, text, jsonb, jsonb)",
               "portal_action_receipt_apply(text, text, text, jsonb)"):
        assert f"revoke all on function public.{fn}\n  from public, anon, authenticated;" in LOW
        assert f"grant execute on function public.{fn}\n  to service_role;" in LOW
    assert "revoke all on public.portal_action_receipt from public, anon, authenticated, service_role" in LOW
    assert "grant delete" not in LOW
    # no direct UPDATE path at all, even for service_role
    assert "grant select on public.portal_action_receipt to service_role" in LOW
    assert "grant update" not in LOW
    for fn in ("portal_action_receipt_begin", "portal_action_receipt_claim_selection",
               "portal_action_receipt_apply"):
        body = LOW.split(f"function public.{fn}(", 1)[1].split("$$;", 1)[0]
        assert "security definer set search_path = public" in body
        assert ") returns public.portal_action_receipt" in body


def test_terminal_and_binding_immutability_backstops():
    assert "receipt identity and request binding are immutable" in LOW
    assert "a terminal receipt cannot be rewritten" in LOW
    assert "check (status in ('started', 'selected', 'succeeded', 'failed', 'uncertain'))" in LOW
    assert "member_manifest jsonb" in LOW


# --------------------------------------------------------------------------
# Live disposable-PG17 checks (rollback-only, local Unix socket)
# --------------------------------------------------------------------------

PGBIN = _pgbin()
pytestmark_live = pytest.mark.skipif(PGBIN is None, reason="no local PostgreSQL 17 binaries")

SOCK = None
DB = "echo_swap_receipt_test"


def _run(cmd, **kw):
    done = subprocess.run(cmd, text=True, capture_output=True, timeout=60, **kw)
    return done


def sql(statement, expect_error=False):
    assert SOCK
    done = _run([str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                 "-h", SOCK, "-d", DB, "-c", statement])
    if expect_error:
        assert done.returncode != 0, f"expected failure, got: {done.stdout}"
        return done.stderr
    if done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


@pytest.fixture(scope="module")
def pg():
    global SOCK
    if PGBIN is None:
        pytest.skip("no local PostgreSQL 17 binaries")
    tmp = Path(tempfile.mkdtemp(prefix="echo-receipt-pg-"))
    SOCK = str(tmp)
    init = _run([str(PGBIN / "initdb"), "-D", str(tmp / "data"), "-U", "postgres",
                 "--auth=trust", "-E", "UTF8", "--no-instructions"])
    if init.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        pytest.skip(f"disposable PostgreSQL unavailable in this sandbox: "
                    f"{init.stderr.strip().splitlines()[-1] if init.stderr else 'initdb failed'}")
    start = _run([str(PGBIN / "pg_ctl"), "-D", str(tmp / "data"), "-l", str(tmp / "log"),
                  "-o", f"-k {tmp} -c listen_addresses='' -c fsync=off", "-w", "start"])
    assert start.returncode == 0, start.stderr
    try:
        sql_admin = lambda s: _run([str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                                    "-h", SOCK, "-d", "postgres", "-c", s])
        assert sql_admin(f"create database {DB}").returncode == 0
        sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if;"
            "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if;"
            "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
        sql("""create table public.content_calendar(
            id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
            logical_post_id uuid,
            status text default 'pending', account text default 'instagram', format text default 'feed',
            variant_status text default 'active', caption text, image_url text, thumbnail_url text,
            source_media_url text, source_media_asset_id text, drive_file_id text, byte_hash text,
            r2_key text, media_not_ready_reason text, published_at timestamptz, late_post_id text,
            publish_reservation_day date, publish_claim_token uuid,
            created_at timestamptz default now());""")
        sql("""create table public.media_asset(id text primary key, gym_id text, content_hash text,
            eligible boolean, excluded_by_coach boolean not null default false);""")
        mig = ROOT / "migrations" / "portal_action_receipt_draft_20261004.sql"
        done = _run([str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                     "-h", SOCK, "-d", DB, "-f", str(mig)])
        assert done.returncode == 0, done.stderr
        # operator rollout seed: the trusted public media origin the tests
        # mint URLs under (the migration itself ships no host)
        sql("insert into public.portal_action_receipt_config(key, value)"
            " values('public_origin','https://cdn.example.com')")
        yield
    finally:
        _run([str(PGBIN / "pg_ctl"), "-D", str(tmp / "data"), "-m", "immediate", "stop"])
        shutil.rmtree(tmp, ignore_errors=True)


OLD = "https://cdn.example.com/old.jpg"
ORIGIN = "https://cdn.example.com"
HEX16 = "0123456789abcdef"  # sha1-16 content-address segment (media_host._build_key)


def NEW_FOR(gym, name="new.jpg"):
    """A hosted URL under the trusted origin and the gym's tenant-scoped,
    content-addressed Echo object prefix
    (<origin>/echo/<slug(gym)>/<sha1-16>/<file>), the only shape claim/apply
    accept after the trusted-origin URL binding."""
    return f"{ORIGIN}/echo/{gym}/{HEX16}/{name}"


def _seed_gym(gym="gym-a"):
    """One logical post: IG feed primary + IG story sibling (same rendition),
    plus an unrelated same-date post in a DIFFERENT logical group."""
    asset = "asset-" + uuid.uuid4().hex[:8]
    sql(f"insert into public.media_asset(id, gym_id, content_hash, eligible)"
        f" values('{asset}','{gym}','h',true)")
    lp, lp_other = uuid.uuid4(), uuid.uuid4()
    primary, sibling, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url, source_media_url, source_media_asset_id) values"
        f" ('{primary}','{gym}','2026-10-10','{lp}','pending','instagram','feed','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{sibling}','{gym}','2026-10-10','{lp}','pending','instagram','story','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{other}','{gym}','2026-10-10','{lp_other}','pending','instagram','feed','other','https://cdn.example.com/other.jpg','https://cdn.example.com/other.jpg','other-asset')")
    return asset, str(primary), str(sibling), str(other)


def _fp(gym, row_id):
    return hashlib.sha256(f"{gym}:{row_id}".encode()).hexdigest()


def _begin(gym, action_id, row_id, fp=None):
    fp = fp or _fp(gym, row_id)
    out = sql(f"select id, status from public.portal_action_receipt_begin("
              f"'{gym}','{action_id}','swap-media','{row_id}','actor-1','{fp}')")
    return fp, out


def _claim(gym, action_id, fp, asset, new_url, primary, sibling):
    selected = json.dumps({"asset_id": asset, "image_url": new_url,
                           "source_media_url": new_url, "thumbnail_url": None,
                           "kind": "photo", "key": "k1"})
    planned = json.dumps({sibling: {"image_url": new_url, "source_media_url": new_url,
                                    "thumbnail_url": None, "source_media_asset_id": asset}})
    # Returns the STORED (winner's) selection: status, stable asset id, image_url.
    return sql(f"select status, selected_asset->>'asset_id', selected_asset->>'image_url'"
               f" from public.portal_action_receipt_claim_selection("
               f"'{gym}','{action_id}','{fp}','{selected}'::jsonb,'{planned}'::jsonb)")


def _rows_json(row_ids, asset, url):
    return json.dumps({"rows": [
        {"calendar_row_id": r, "media": {"image_url": url, "source_media_url": url,
                                         "thumbnail_url": None, "source_media_asset_id": asset}}
        for r in row_ids]})


def _apply(gym, action_id, fp, row_ids, asset, url=None):
    url = url or NEW_FOR(gym)
    rows = json.dumps({"rows": [
        {"calendar_row_id": r, "media": {"image_url": url, "source_media_url": url,
                                         "thumbnail_url": None, "source_media_asset_id": asset}}
        for r in row_ids]})
    return sql(f"select status from public.portal_action_receipt_apply('{gym}','{action_id}','{fp}','{rows}'::jsonb)")


@pytestmark_live
def test_live_duplicate_begin_returns_same_receipt(pg):
    asset, primary, sibling, _ = _seed_gym("gym-dup")
    fp, first = _begin("gym-dup", "act-dup", primary)
    _, second = _begin("gym-dup", "act-dup", primary, fp)
    assert first == second
    count = sql("select count(*) from public.portal_action_receipt where gym_id='gym-dup'")
    assert count == "1"


@pytestmark_live
def test_live_conflicting_begin_rejected(pg):
    _, primary, _, _ = _seed_gym("gym-conf")
    _begin("gym-conf", "act-conf", primary)
    err = sql(f"select public.portal_action_receipt_begin('gym-conf','act-conf','swap-media',"
              f"'{uuid.uuid4()}','actor-1','{_fp('gym-conf', primary)}')", expect_error=True)
    assert "conflicting reuse" in err
    # different actor on the same post + action_id is also a conflicting reuse
    err = sql(f"select public.portal_action_receipt_begin('gym-conf','act-conf','swap-media',"
              f"'{primary}','actor-2','{_fp('gym-conf', primary)}')", expect_error=True)
    assert "conflicting reuse" in err
    # conflicting replay aimed at a HISTORICAL NULL row still reports the
    # immutable action conflict, never the manual-review hold (receipt
    # binding takes precedence over any primary-row lookup)
    null_row = uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, status, account, format,"
        f" caption, image_url) values ('{null_row}','gym-conf','2026-10-10','pending','instagram','feed','cap','{OLD}')")
    err = sql(f"select public.portal_action_receipt_begin('gym-conf','act-conf','swap-media',"
              f"'{null_row}','actor-1','{_fp('gym-conf', null_row)}')", expect_error=True)
    assert "conflicting reuse" in err
    # cross-tenant replay of the same action_id is a distinct receipt, not a read
    _, other_primary, _, _ = _seed_gym("gym-conf-b")
    _, out = _begin("gym-conf-b", "act-conf", other_primary)
    assert "started" in out


@pytestmark_live
def test_live_begin_replay_returns_binding_when_primary_row_gone(pg):
    # an exact-binding replay returns the stored receipt even after the
    # primary row was removed; only a CHANGED binding is a conflicting reuse
    _, primary, _, _ = _seed_gym("gym-gone")
    fp, first = _begin("gym-gone", "act-gone", primary)
    sql(f"delete from public.content_calendar where id='{primary}'")
    fp2, replayed = _begin("gym-gone", "act-gone", primary, fp)
    assert fp2 == fp and replayed == first
    # the replay returns the ORIGINAL SQL-captured before_state verbatim,
    # even though the primary row no longer exists to re-read
    assert sql("select before_state->>'image_url' from public.portal_action_receipt"
               " where gym_id='gym-gone' and action_id='act-gone'") == OLD
    assert sql("select before_state->>'source_media_asset_id' from public.portal_action_receipt"
               " where gym_id='gym-gone' and action_id='act-gone'") == "old-asset"
    # a conflicting replay of the same action against a DIFFERENT binding
    # (nonexistent row, or a different existing row of the same gym) raises
    err = sql(f"select public.portal_action_receipt_begin('gym-gone','act-gone','swap-media',"
              f"'{uuid.uuid4()}','actor-1','{fp}')", expect_error=True)
    assert "conflicting reuse" in err
    _, other, _, _ = _seed_gym("gym-gone")
    other_fp = _fp("gym-gone", other)
    err = sql(f"select public.portal_action_receipt_begin('gym-gone','act-gone','swap-media',"
              f"'{other}','actor-1','{other_fp}')", expect_error=True)
    assert "conflicting reuse" in err


@pytestmark_live
def test_live_null_logical_post_held_for_manual_review(pg):
    # historical row: never backfilled with a logical_post_id
    row = uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, status, account, format,"
        f" caption, image_url) values ('{row}','gym-null','2026-10-10','pending','instagram','feed','cap','{OLD}')")
    err = sql(f"select public.portal_action_receipt_begin('gym-null','act-null','swap-media',"
              f"'{row}','actor-1','{_fp('gym-null', row)}')", expect_error=True)
    assert "held for manual review" in err
    # no receipt was created
    assert sql("select count(*) from public.portal_action_receipt where gym_id='gym-null'") == "0"


@pytestmark_live
def test_live_concurrent_claim_single_winner(pg):
    asset, primary, sibling, _ = _seed_gym("gym-race")
    fp, _ = _begin("gym-race", "act-race", primary)
    results, errors = [], []

    def claim(url):
        try:
            results.append(_claim("gym-race", "act-race", fp, asset, url, primary, sibling))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    a, b = NEW_FOR("gym-race", "new-a.jpg"), NEW_FOR("gym-race", "new-b.jpg")
    t1 = threading.Thread(target=claim, args=(a,))
    t2 = threading.Thread(target=claim, args=(b,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors
    assert len(results) == 2
    # both callers observe the SAME winning selection; exactly one candidate won
    winners = {r.split("|")[2] for r in results}
    assert len(winners) == 1
    assert re.search(r"new-[ab]", winners.pop() + "|"), winners
    assert all(r.startswith("selected|") for r in results)
    winners = {r.split("|")[2] for r in results}
    stored = sql("select selected_asset->>'image_url' from public.portal_action_receipt"
                 " where gym_id='gym-race' and action_id='act-race'")
    assert winners.pop() in stored


@pytestmark_live
def test_live_frozen_selection_and_manifest_cannot_be_overwritten(pg):
    asset, primary, sibling, _ = _seed_gym("gym-frz")
    fp, _ = _begin("gym-frz", "act-frz", primary)
    _claim("gym-frz", "act-frz", fp, asset, NEW_FOR("gym-frz", "new-a.jpg"), primary, sibling)
    # RPC replay with a different candidate returns the stored winner
    out = _claim("gym-frz", "act-frz", fp, asset, NEW_FOR("gym-frz", "new-b.jpg"), primary, sibling)
    assert "new-a" in out
    # the manifest froze with the selection: exactly the two active rows
    n = sql("select jsonb_array_length(member_manifest->'members') from public.portal_action_receipt"
            " where gym_id='gym-frz' and action_id='act-frz'")
    assert n == "2"
    # direct UPDATE (service_role path) is stopped by the immutability trigger
    err = sql("update public.portal_action_receipt set selected_asset='{}'::jsonb"
              " where gym_id='gym-frz' and action_id='act-frz'", expect_error=True)
    assert "frozen" in err
    err = sql("update public.portal_action_receipt set member_manifest='{}'::jsonb"
              " where gym_id='gym-frz' and action_id='act-frz'", expect_error=True)
    assert "frozen" in err or "manifest" in err


@pytestmark_live
def test_live_claim_rejects_token_url_foreign_asset_and_picker_dict(pg):
    asset, primary, sibling, _ = _seed_gym("gym-tok")
    fp, _ = _begin("gym-tok", "act-tok", primary)
    signed = json.dumps({"asset_id": asset, "image_url": "https://x.example.com/a.jpg?sig=t"})
    err = sql(f"select public.portal_action_receipt_claim_selection('gym-tok','act-tok','{fp}',"
              f"'{signed}'::jsonb,'{{}}'::jsonb)", expect_error=True)
    assert "allowlisted public object identity" in err
    foreign = json.dumps({"asset_id": "not-mine", "image_url": "https://x.example.com/a.jpg"})
    err = sql(f"select public.portal_action_receipt_claim_selection('gym-tok','act-tok','{fp}',"
              f"'{foreign}'::jsonb,'{{}}'::jsonb)", expect_error=True)
    assert "own-tenant allowlisted" in err
    # picker-dict extra keys refused
    extra = json.dumps({"asset_id": asset, "image_url": "https://x.example.com/a.jpg",
                        "path": "/tmp/local.jpg", "raw": {"headers": {}}})
    err = sql(f"select public.portal_action_receipt_claim_selection('gym-tok','act-tok','{fp}',"
              f"'{extra}'::jsonb,'{{}}'::jsonb)", expect_error=True)
    assert "allowlisted" in err


@pytestmark_live
def test_live_apply_writes_group_and_terminal_receipt_atomically(pg):
    asset, primary, sibling, other = _seed_gym("gym-ok")
    fp, _ = _begin("gym-ok", "act-ok", primary)
    _claim("gym-ok", "act-ok", fp, asset, NEW_FOR("gym-ok"), primary, sibling)
    assert _apply("gym-ok", "act-ok", fp, [primary, sibling], asset) == "succeeded"
    moved = sql(f"select count(*) from public.content_calendar where id in ('{primary}','{sibling}')"
                f" and image_url='{NEW_FOR("gym-ok")}' and source_media_asset_id='{asset}'")
    assert moved == "2"
    # the unrelated same-date post is untouched
    assert sql(f"select image_url from public.content_calendar where id='{other}'") == \
        "https://cdn.example.com/other.jpg"
    outcomes = sql("select jsonb_array_length(sibling_outcomes) from public.portal_action_receipt"
                   " where gym_id='gym-ok' and action_id='act-ok'")
    assert outcomes == "2"
    # replay after a lost response is idempotent: same terminal receipt, no
    # repick/rewrite -- even with a DIFFERENT, malformed, or missing new
    # prepared payload (terminal replay is reconciled before payload shape)
    assert _apply("gym-ok", "act-ok", fp, [primary, sibling], asset,
                  url="https://cdn.example.com/never.jpg") == "succeeded"
    out = sql(f"select status from public.portal_action_receipt_apply("
              f"'gym-ok','act-ok','{fp}','\"not-an-object\"'::jsonb)")
    assert out == "succeeded"
    out = sql(f"select status from public.portal_action_receipt_apply("
              f"'gym-ok','act-ok','{fp}','null'::jsonb)")
    assert out == "succeeded"
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == NEW_FOR("gym-ok")
    # a different fingerprint on the same action_id conflicts even after
    # success, and the fingerprint/binding conflict takes precedence over a
    # malformed new payload
    err = sql(f"select public.portal_action_receipt_apply('gym-ok','act-ok','{hashlib.sha256(b'x').hexdigest()}',"
              f"'{{\"rows\":[]}}'::jsonb)", expect_error=True)
    assert "fingerprint" in err


@pytestmark_live
def test_live_independent_posts_same_date_and_media_not_coupled(pg):
    # two INDEPENDENT posts sharing the same date AND the same media identity:
    # v2 date/media inference would have coupled them; logical-post grouping
    # must not.
    gym = "gym-ind"
    asset = "asset-" + uuid.uuid4().hex[:8]
    sql(f"insert into public.media_asset(id, gym_id, content_hash, eligible)"
        f" values('{asset}','{gym}','h',true)")
    lp1, lp2 = uuid.uuid4(), uuid.uuid4()
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url, source_media_url, source_media_asset_id) values"
        f" ('{p1}','{gym}','2026-10-10','{lp1}','pending','instagram','feed','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{p2}','{gym}','2026-10-10','{lp2}','pending','instagram','feed','cap','{OLD}','{OLD}','old-asset')")
    fp, _ = _begin(gym, "act-ind", str(p1))
    _claim(gym, "act-ind", fp, asset, NEW_FOR(gym), str(p1), str(p1))
    # the manifest contains ONLY p1 despite p2 sharing date + media identity
    n = sql("select jsonb_array_length(member_manifest->'members') from public.portal_action_receipt"
            f" where gym_id='{gym}' and action_id='act-ind'")
    assert n == "1"
    assert _apply(gym, "act-ind", fp, [str(p1)], asset) == "succeeded"
    assert sql(f"select image_url from public.content_calendar where id='{p1}'") == NEW_FOR(gym)
    assert sql(f"select image_url from public.content_calendar where id='{p2}'") == OLD


@pytestmark_live
def test_live_archived_and_candidate_variants_excluded(pg):
    gym = "gym-var"
    asset = "asset-" + uuid.uuid4().hex[:8]
    sql(f"insert into public.media_asset(id, gym_id, content_hash, eligible)"
        f" values('{asset}','{gym}','h',true)")
    lp = uuid.uuid4()
    active, archived, candidate = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, variant_status, caption, image_url, source_media_url, source_media_asset_id) values"
        f" ('{active}','{gym}','2026-10-10','{lp}','pending','instagram','feed','active','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{archived}','{gym}','2026-10-10','{lp}','pending','instagram','feed','archived','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{candidate}','{gym}','2026-10-10','{lp}','pending','instagram','story','candidate','cap','{OLD}','{OLD}','old-asset')")
    fp, _ = _begin(gym, "act-var", str(active))
    _claim(gym, "act-var", fp, asset, NEW_FOR(gym), str(active), str(active))
    n = sql("select jsonb_array_length(member_manifest->'members') from public.portal_action_receipt"
            f" where gym_id='{gym}' and action_id='act-var'")
    assert n == "1"
    assert _apply(gym, "act-var", fp, [str(active)], asset) == "succeeded"
    assert sql(f"select image_url from public.content_calendar where id='{active}'") == NEW_FOR(gym)
    assert sql(f"select image_url from public.content_calendar where id='{archived}'") == OLD
    assert sql(f"select image_url from public.content_calendar where id='{candidate}'") == OLD


@pytestmark_live
def test_live_story_different_rendition_included(pg):
    # same logical post, but the Story row is a DIFFERENT rendition (different
    # media identity). Media-identity inference would have missed it; the
    # logical-post group must include it.
    gym = "gym-story"
    asset = "asset-" + uuid.uuid4().hex[:8]
    sql(f"insert into public.media_asset(id, gym_id, content_hash, eligible)"
        f" values('{asset}','{gym}','h',true)")
    lp = uuid.uuid4()
    feed, story = uuid.uuid4(), uuid.uuid4()
    story_old = "https://cdn.example.com/old-story-crop.jpg"
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url, source_media_url, source_media_asset_id) values"
        f" ('{feed}','{gym}','2026-10-10','{lp}','pending','instagram','feed','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{story}','{gym}','2026-10-10','{lp}','pending','instagram','story','cap',"
        f"  '{story_old}','{story_old}','old-story-asset')")
    fp, _ = _begin(gym, "act-story", str(feed))
    _claim(gym, "act-story", fp, asset, NEW_FOR(gym), str(feed), str(story))
    n = sql("select jsonb_array_length(member_manifest->'members') from public.portal_action_receipt"
            f" where gym_id='{gym}' and action_id='act-story'")
    assert n == "2"
    assert _apply(gym, "act-story", fp, [str(feed), str(story)], asset) == "succeeded"
    assert sql(f"select image_url from public.content_calendar where id='{feed}'") == NEW_FOR(gym)
    assert sql(f"select image_url from public.content_calendar where id='{story}'") == NEW_FOR(gym)


@pytestmark_live
def test_live_stale_sibling_aborts_whole_group_and_rolls_back(pg):
    asset, primary, sibling, _ = _seed_gym("gym-stale")
    fp, _ = _begin("gym-stale", "act-stale", primary)
    _claim("gym-stale", "act-stale", fp, asset, NEW_FOR("gym-stale"), primary, sibling)
    # sibling approved between claim and apply: the group is stale
    sql(f"update public.content_calendar set status='approved' where id='{sibling}'")
    rows = json.dumps({"rows": [
        {"calendar_row_id": primary, "media": {"image_url": NEW_FOR("gym-stale"), "source_media_url": NEW_FOR("gym-stale"), "thumbnail_url": None, "source_media_asset_id": asset}},
        {"calendar_row_id": sibling, "media": {"image_url": NEW_FOR("gym-stale"), "source_media_url": NEW_FOR("gym-stale"), "thumbnail_url": None, "source_media_asset_id": asset}}]})
    err = sql(f"select public.portal_action_receipt_apply('gym-stale','act-stale','{fp}','{rows}'::jsonb)",
              expect_error=True)
    assert "stale eligible sibling" in err
    # rollback: receipt still selected (NOT failed), primary media untouched
    assert sql("select status from public.portal_action_receipt"
               " where gym_id='gym-stale' and action_id='act-stale'") == "selected"
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    # fixing the staleness lets the SAME receipt apply cleanly
    sql(f"update public.content_calendar set status='pending' where id='{sibling}'")
    assert _apply("gym-stale", "act-stale", fp, [primary, sibling], asset) == "succeeded"


@pytestmark_live
def test_live_published_and_claimed_rows_never_mutated(pg):
    gym = "gym-pub"
    asset = "asset-" + uuid.uuid4().hex[:8]
    sql(f"insert into public.media_asset(id, gym_id, content_hash, eligible)"
        f" values('{asset}','{gym}','h',true)")
    lp = uuid.uuid4()
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url, source_media_url, source_media_asset_id) values"
        f" ('{p1}','{gym}','2026-10-10','{lp}','pending','instagram','feed','cap','{OLD}','{OLD}','old-asset'),"
        f" ('{p2}','{gym}','2026-10-10','{lp}','pending','instagram','story','cap','{OLD}','{OLD}','old-asset')")
    fp, _ = _begin(gym, "act-pub", str(p1))
    _claim(gym, "act-pub", fp, asset, NEW_FOR(gym), str(p1), str(p2))
    # p2 gets published (marker) between claim and apply
    sql(f"update public.content_calendar set published_at=now(), status='published' where id='{p2}'")
    rows = json.dumps({"rows": [
        {"calendar_row_id": str(p1), "media": {"image_url": NEW_FOR(gym), "source_media_url": NEW_FOR(gym), "thumbnail_url": None, "source_media_asset_id": asset}},
        {"calendar_row_id": str(p2), "media": {"image_url": NEW_FOR(gym), "source_media_url": NEW_FOR(gym), "thumbnail_url": None, "source_media_asset_id": asset}}]})
    err = sql(f"select public.portal_action_receipt_apply('{gym}','act-pub','{fp}','{rows}'::jsonb)",
              expect_error=True)
    assert "stale eligible sibling" in err
    assert sql(f"select image_url from public.content_calendar where id='{p1}'") == OLD
    assert sql("select status from public.portal_action_receipt"
               f" where gym_id='{gym}' and action_id='act-pub'") == "selected"


@pytestmark_live
def test_live_media_hold_and_membership_drift_abort(pg):
    asset, primary, sibling, _ = _seed_gym("gym-hold")
    fp, _ = _begin("gym-hold", "act-hold", primary)
    _claim("gym-hold", "act-hold", fp, asset, NEW_FOR("gym-hold"), primary, sibling)
    sql(f"update public.content_calendar set media_not_ready_reason='needs_media' where id='{primary}'")
    rows = json.dumps({"rows": [
        {"calendar_row_id": primary, "media": {"image_url": NEW_FOR("gym-hold"), "source_media_url": NEW_FOR("gym-hold"), "thumbnail_url": None, "source_media_asset_id": asset}},
        {"calendar_row_id": sibling, "media": {"image_url": NEW_FOR("gym-hold"), "source_media_url": NEW_FOR("gym-hold"), "thumbnail_url": None, "source_media_asset_id": asset}}]})
    err = sql(f"select public.portal_action_receipt_apply('gym-hold','act-hold','{fp}','{rows}'::jsonb)",
              expect_error=True)
    assert "stale eligible sibling or media hold" in err
    # rollback: nothing moved, receipt not finalized
    assert sql(f"select image_url from public.content_calendar where id='{sibling}'") == OLD
    assert sql("select status from public.portal_action_receipt"
               " where gym_id='gym-hold' and action_id='act-hold'") == "selected"
    sql(f"update public.content_calendar set media_not_ready_reason=null where id='{primary}'")
    # membership drift: a new active sibling joins the logical post after selection
    lp = sql(f"select logical_post_id from public.content_calendar where id='{primary}'")
    extra = uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url) values"
        f" ('{extra}','gym-hold','2026-10-10','{lp}','pending','facebook','feed','cap','{OLD}')")
    err = sql(f"select public.portal_action_receipt_apply('gym-hold','act-hold','{fp}','{rows}'::jsonb)",
              expect_error=True)
    assert "active logical-post membership changed since selection" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD


@pytestmark_live
def test_live_cross_tenant_apply_cannot_touch_receipt(pg):
    asset, primary, sibling, _ = _seed_gym("gym-xa")
    fp, _ = _begin("gym-xa", "act-xa", primary)
    _claim("gym-xa", "act-xa", fp, asset, NEW_FOR("gym-xa"), primary, sibling)
    _seed_gym("gym-xb")
    # another tenant with the same action_id has no receipt and cannot read/apply
    rows = json.dumps({"rows": [{"calendar_row_id": primary, "media": {"image_url": NEW_FOR("gym-xa")}},
                                {"calendar_row_id": sibling, "media": {"image_url": NEW_FOR("gym-xa")}}]})
    err = sql(f"select public.portal_action_receipt_apply('gym-xb','act-xa','{fp}','{rows}'::jsonb)",
              expect_error=True)
    assert "no receipt for this tenant" in err
    # and a foreign planned sibling is refused at claim time
    _, fb_primary, fb_sibling, _ = _seed_gym("gym-xc")
    fpc, _ = _begin("gym-xc", "act-xc", fb_primary)
    selected = json.dumps({"asset_id": asset, "image_url": NEW_FOR("gym-xc")})
    # asset belongs to gym-xa, not gym-xc
    err = sql(f"select public.portal_action_receipt_claim_selection('gym-xc','act-xc','{fpc}',"
              f"'{selected}'::jsonb,'{{}}'::jsonb)", expect_error=True)
    assert "own-tenant allowlisted" in err


@pytestmark_live
def test_live_apply_rejects_repicked_foreign_or_tokenized_media(pg):
    asset, primary, sibling, _ = _seed_gym("gym-repk")
    fp, _ = _begin("gym-repk", "act-repk", primary)
    _claim("gym-repk", "act-repk", fp, asset, NEW_FOR("gym-repk"), primary, sibling)

    def rows_for(url, asset_id):
        return json.dumps({"rows": [
            {"calendar_row_id": r, "media": {"image_url": url, "source_media_url": url,
                                             "thumbnail_url": None,
                                             "source_media_asset_id": asset_id}}
            for r in (primary, sibling)]})

    # repicked media: valid shape and clean URL, but not the frozen selection
    repicked = "https://cdn.example.com/repicked.jpg"
    err = sql(f"select public.portal_action_receipt_apply('gym-repk','act-repk','{fp}',"
              f"'{rows_for(repicked, asset)}'::jsonb)", expect_error=True)
    assert "does not match the frozen selection" in err
    # rollback: nothing moved, receipt not finalized
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    assert sql(f"select image_url from public.content_calendar where id='{sibling}'") == OLD
    assert sql("select status from public.portal_action_receipt"
               " where gym_id='gym-repk' and action_id='act-repk'") == "selected"
    # tokenized prepared URL is refused before mutation
    err = sql(f"select public.portal_action_receipt_apply('gym-repk','act-repk','{fp}',"
              f"'{rows_for(NEW_FOR("gym-repk") + '?sig=t', asset)}'::jsonb)", expect_error=True)
    assert "not allowlisted" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    # foreign asset id in the prepared media is refused before mutation
    sql("insert into public.media_asset(id, gym_id, content_hash, eligible)"
        " values('foreign-asset','gym-other','h',true)")
    err = sql(f"select public.portal_action_receipt_apply('gym-repk','act-repk','{fp}',"
              f"'{rows_for(NEW_FOR("gym-repk"), 'foreign-asset')}'::jsonb)", expect_error=True)
    assert "frozen selection" in err or "own-tenant allowlisted" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    # the exact frozen identity still applies cleanly
    assert _apply("gym-repk", "act-repk", fp, [primary, sibling], asset) == "succeeded"


@pytestmark_live
def test_live_apply_cas_catches_account_format_post_date_drift(pg):
    asset, primary, sibling, _ = _seed_gym("gym-idc")
    fp, _ = _begin("gym-idc", "act-idc", primary)
    _claim("gym-idc", "act-idc", fp, asset, NEW_FOR("gym-idc"), primary, sibling)
    # the manifest froze account/format/post_date per row; retargeting a row
    # after selection makes it stale even though status and media match
    sql(f"update public.content_calendar set account='facebook' where id='{sibling}'")
    err = _apply_err("gym-idc", "act-idc", fp, [primary, sibling], asset)
    assert "stale eligible sibling or media hold" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    assert sql("select status from public.portal_action_receipt"
               " where gym_id='gym-idc' and action_id='act-idc'") == "selected"
    sql(f"update public.content_calendar set account='instagram', post_date='2026-10-11'"
        f" where id='{sibling}'")
    err = _apply_err("gym-idc", "act-idc", fp, [primary, sibling], asset)
    assert "stale eligible sibling or media hold" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    sql(f"update public.content_calendar set post_date='2026-10-10' where id='{sibling}'")
    assert _apply("gym-idc", "act-idc", fp, [primary, sibling], asset) == "succeeded"


@pytestmark_live
def test_live_soft_before_update_trigger_cannot_alter_media(pg):
    # A soft BEFORE UPDATE guard that rewrites NEW leaves ROW_COUNT = 1; only
    # the post-write persisted re-read can catch it. The whole transaction
    # must roll back: no row keeps the media and the receipt stays 'selected'.
    asset, primary, sibling, _ = _seed_gym("gym-soft")
    fp, _ = _begin("gym-soft", "act-soft", primary)
    _claim("gym-soft", "act-soft", fp, asset, NEW_FOR("gym-soft"), primary, sibling)
    sql("""create or replace function public.echo_receipt_soft_guard()
        returns trigger language plpgsql as $g$
        begin
          -- soft guard: silently archives the row and re-points the media
          new.status := 'archived';
          new.variant_status := 'archived';
          new.image_url := 'https://cdn.example.com/guard-rewritten.jpg';
          new.media_not_ready_reason := 'guard hold';
          return new;
        end $g$;""")
    sql("create trigger echo_receipt_soft_guard before update on public.content_calendar"
        " for each row when (old.gym_id = 'gym-soft')"
        " execute function public.echo_receipt_soft_guard()")
    try:
        err = _apply_err("gym-soft", "act-soft", fp, [primary, sibling], asset)
        assert "post-write drift" in err
        # full rollback: every row kept its original persisted state
        for row in (primary, sibling):
            assert sql(f"select image_url from public.content_calendar where id='{row}'") == OLD
            assert sql(f"select status from public.content_calendar where id='{row}'") == "pending"
            assert sql(f"select variant_status from public.content_calendar where id='{row}'") == "active"
            assert sql(f"select media_not_ready_reason is null from"
                       f" public.content_calendar where id='{row}'") == "t"
        assert sql("select status from public.portal_action_receipt"
                   " where gym_id='gym-soft' and action_id='act-soft'") == "selected"
        assert sql("select sibling_outcomes is null from public.portal_action_receipt"
                   " where gym_id='gym-soft' and action_id='act-soft'") == "t"
    finally:
        sql("drop trigger if exists echo_receipt_soft_guard on public.content_calendar")
    # with the soft guard gone the frozen selection applies cleanly
    assert _apply("gym-soft", "act-soft", fp, [primary, sibling], asset) == "succeeded"


def _apply_err(gym, action_id, fp, row_ids, asset, url=None):
    url = url or NEW_FOR(gym)
    rows = json.dumps({"rows": [
        {"calendar_row_id": r, "media": {"image_url": url, "source_media_url": url,
                                         "thumbnail_url": None, "source_media_asset_id": asset}}
        for r in row_ids]})
    return sql(f"select public.portal_action_receipt_apply('{gym}','{action_id}','{fp}','{rows}'::jsonb)",
               expect_error=True)


@pytestmark_live
def test_live_phantom_insert_blocked_by_apply_lock_strategy(pg):
    # The documented lock strategy: SHARE ROW EXCLUSIVE (taken by apply) blocks
    # a concurrent INSERT, so a newly inserted same-post sibling cannot slip in.
    _seed_gym("gym-lock")
    holder = subprocess.Popen(
        [str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-h", SOCK, "-d", DB],
        stdin=subprocess.PIPE, text=True)
    holder.stdin.write("begin; lock table public.content_calendar in share row exclusive mode;\n")
    holder.stdin.flush()
    time.sleep(0.5)
    err = sql(f"set statement_timeout=1000; insert into public.content_calendar"
              f"(gym_id, post_date, status, format, image_url) values"
              f"('gym-lock','2026-10-10','pending','story','https://cdn.example.com/phantom.jpg')",
              expect_error=True)
    assert "statement timeout" in err
    holder.stdin.write("commit; \\q\n")
    holder.stdin.flush()
    holder.wait(timeout=30)
    # after the barrier is released the insert succeeds (lock was the only blocker)
    sql(f"insert into public.content_calendar(gym_id, post_date, status, format, image_url) values"
        f"('gym-lock','2026-10-10','pending','story','https://cdn.example.com/phantom.jpg')")


@pytestmark_live
def test_live_grants_and_rls_deny_clients(pg):
    sigs = {"portal_action_receipt_begin": "text, text, text, uuid, text, text",
            "portal_action_receipt_claim_selection": "text, text, text, jsonb, jsonb",
            "portal_action_receipt_apply": "text, text, text, jsonb"}
    for fn, sig in sigs.items():
        assert sql(f"select has_function_privilege('anon', 'public.{fn}({sig})', 'EXECUTE')") == "f"
        assert sql(f"select has_function_privilege('authenticated', 'public.{fn}({sig})', 'EXECUTE')") == "f"
        assert sql(f"select has_function_privilege('service_role', 'public.{fn}({sig})', 'EXECUTE')") == "t"
    err = sql("set role anon; select public.portal_action_receipt_apply("
              "'g','a','f','{}'::jsonb)", expect_error=True)
    assert "permission denied" in err
    err = sql("set role authenticated; select * from public.portal_action_receipt", expect_error=True)
    assert "permission denied" in err
    sql("set role service_role; select count(*) from public.portal_action_receipt; reset role")
    # service_role can read but has NO direct write: receipts move
    # only through the SECURITY DEFINER RPCs
    assert sql("select has_table_privilege('service_role',"
               " 'public.portal_action_receipt', 'SELECT')") == "t"
    assert sql("select has_table_privilege('service_role',"
               " 'public.portal_action_receipt', 'INSERT')") == "f"
    assert sql("select has_table_privilege('service_role',"
               " 'public.portal_action_receipt', 'UPDATE')") == "f"
@pytestmark_live
def test_live_begin_captures_persisted_before_state_and_refuses_forgery(pg):
    # SQL begin is authoritative for the first before_state: the caller sends
    # only (gym, action_id, action, row, actor, fingerprint) and the stored
    # snapshot is captured from the persisted row, never from the caller.
    _, primary, _, _ = _seed_gym("gym-bfs")
    fp, _ = _begin("gym-bfs", "act-bfs", primary)
    for col, want in (("status", "pending"), ("caption", "cap"),
                      ("post_date", "2026-10-10"), ("format", "feed"),
                      ("image_url", OLD), ("source_media_url", OLD),
                      ("source_media_asset_id", "old-asset")):
        got = sql(f"select before_state->>'{col}' from public.portal_action_receipt"
                  f" where gym_id='gym-bfs' and action_id='act-bfs'")
        assert got == want, (col, got)
    assert sql("select before_state->>'id' from public.portal_action_receipt"
               " where gym_id='gym-bfs' and action_id='act-bfs'") == primary
    # a forged caller-supplied before_state cannot even be expressed: the old
    # 7-argument signature no longer exists
    forged = json.dumps({"id": primary, "status": "published",
                         "image_url": "https://evil.example.com/forged.jpg"})
    err = sql(f"select public.portal_action_receipt_begin('gym-bfs','act-bfs2','swap-media',"
              f"'{primary}','actor-1','{_fp('gym-bfs', primary)}','{forged}'::jsonb)",
              expect_error=True)
    assert "does not exist" in err
    # and no caller can rewrite the captured snapshot directly either
    err = sql("update public.portal_action_receipt set before_state='{}'::jsonb"
              " where gym_id='gym-bfs' and action_id='act-bfs'", expect_error=True)
    assert "immutable" in err
    assert sql("select before_state->>'image_url' from public.portal_action_receipt"
               " where gym_id='gym-bfs' and action_id='act-bfs'") == OLD


@pytestmark_live
def test_live_terminal_receipt_frozen_against_same_status_direct_update(pg):
    # Regression: a terminal receipt is FULLY frozen. Even a same-status
    # direct UPDATE (the previously-granted service_role path, exercised here
    # as the table owner) cannot rewrite after_state, sibling_outcomes,
    # response_status or error; only the updated_at touch is allowed.
    asset, primary, sibling, _ = _seed_gym("gym-tfz")
    fp, _ = _begin("gym-tfz", "act-tfz", primary)
    _claim("gym-tfz", "act-tfz", fp, asset, NEW_FOR("gym-tfz"), primary, sibling)
    assert _apply("gym-tfz", "act-tfz", fp, [primary, sibling], asset) == "succeeded"
    where = " where gym_id='gym-tfz' and action_id='act-tfz'"
    for stmt, col in (("after_state='{}'::jsonb", "after_state"),
                      ("sibling_outcomes='[]'::jsonb", "sibling_outcomes"),
                      ("response_status=500", "response_status"),
                      ("error='{}'::jsonb", "error")):
        # same-status rewrite (status left 'succeeded') must raise
        err = sql(f"update public.portal_action_receipt set {stmt}{where}",
                  expect_error=True)
        assert "terminal receipt cannot be rewritten" in err, col
        # explicit same-status set must raise too
        err = sql(f"update public.portal_action_receipt set {stmt},"
                  f" status='succeeded'{where}", expect_error=True)
        assert "terminal receipt cannot be rewritten" in err, col
    # unchanged readback: the terminal receipt survived every rewrite attempt
    assert sql(f"select status from public.portal_action_receipt{where}") == "succeeded"
    assert sql(f"select after_state->>'image_url' from public.portal_action_receipt{where}") == NEW_FOR("gym-tfz")
    assert sql(f"select jsonb_array_length(sibling_outcomes) from"
               f" public.portal_action_receipt{where}") == "2"
    assert sql(f"select response_status from public.portal_action_receipt{where}") == "200"
    assert sql(f"select error is null from public.portal_action_receipt{where}") == "t"
    # the optional updated_at touch is still allowed
    sql(f"update public.portal_action_receipt set updated_at=now(){where}")


# ---------------------------------------------------------------------------
# Repair wave 2026-10-04 (Sol gaps 1 + 4): eligibility, local provenance,
# hosted-URL tenant binding
# ---------------------------------------------------------------------------

@pytestmark_live
def test_live_claim_rejects_ineligible_or_unprobed_asset(pg):
    """eligible IS TRUE is required at claim: a coach-visible but
    gate-failed (false) or unprobed (NULL) asset can never be claimed."""
    gym = "gym-inel"
    primary = uuid.uuid4()
    lp = uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url) values"
        f" ('{primary}','{gym}','2026-10-10','{lp}','pending','instagram','feed','cap','{OLD}')")
    for suffix, eligible in (("false", "false"), ("null", "null")):
        asset = f"asset-inel-{suffix}"
        sql(f"insert into public.media_asset(id, gym_id, content_hash, eligible)"
            f" values('{asset}','{gym}','h',{eligible})")
        fp, _ = _begin(gym, f"act-inel-{suffix}", str(primary))
        selected = json.dumps({"asset_id": asset, "image_url": NEW_FOR(gym)})
        err = sql(f"select public.portal_action_receipt_claim_selection('{gym}','act-inel-{suffix}',"
                  f"'{fp}','{selected}'::jsonb,'{{}}'::jsonb)", expect_error=True)
        assert "own-tenant allowlisted" in err
        assert sql("select count(*) from public.portal_action_receipt"
                   f" where gym_id='{gym}' and action_id='act-inel-{suffix}'"
                   " and selected_asset is not null") == "0"


@pytestmark_live
def test_live_claim_rejects_cross_gym_url_paired_with_own_asset(pg):
    """A valid OWN asset id can never launder another gym's public URL: the
    hosted URL must sit under the claiming tenant's Echo object prefix."""
    asset, primary, sibling, _ = _seed_gym("gym-urlb")
    fp, _ = _begin("gym-urlb", "act-urlb", primary)
    foreign_url = json.dumps({"asset_id": asset,
                              "image_url": f"https://cdn.example.com/echo/othergym/{HEX16}/x.jpg"})
    err = sql(f"select public.portal_action_receipt_claim_selection('gym-urlb','act-urlb','{fp}',"
              f"'{foreign_url}'::jsonb,'{{}}'::jsonb)", expect_error=True)
    assert "hosted media prefix" in err
    # the tenant's own prefix (and the _ig local/story variant) is accepted
    out = _claim("gym-urlb", "act-urlb", fp, asset, NEW_FOR("gym-urlb"), primary, sibling)
    assert out.startswith("selected|")
    # a planned sibling URL under a foreign prefix is rejected the same way
    asset2, primary2, sibling2, _ = _seed_gym("gym-urlc")
    fp2, _ = _begin("gym-urlc", "act-urlc", primary2)
    selected = json.dumps({"asset_id": asset2, "image_url": NEW_FOR("gym-urlc")})
    planned = json.dumps({sibling2: {"image_url": f"https://cdn.example.com/echo/othergym/{HEX16}/x.jpg",
                                     "source_media_url": None, "thumbnail_url": None,
                                     "source_media_asset_id": asset2}})
    err = sql(f"select public.portal_action_receipt_claim_selection('gym-urlc','act-urlc','{fp2}',"
              f"'{selected}'::jsonb,'{planned}'::jsonb)", expect_error=True)
    assert "hosted media prefix" in err


@pytestmark_live
def test_live_local_library_provenance_claims_and_applies(pg):
    """Gap 1: a tenant-owned local pick carries 'local:<library key>'
    provenance -- no media_asset row exists or is required, and the
    provenance id is written onto every applied row."""
    gym = "gym-loc"
    lp = uuid.uuid4()
    primary, sibling = uuid.uuid4(), uuid.uuid4()
    sql(f"insert into public.content_calendar(id, gym_id, post_date, logical_post_id, status,"
        f" account, format, caption, image_url) values"
        f" ('{primary}','{gym}','2026-10-10','{lp}','pending','instagram','feed','cap','{OLD}'),"
        f" ('{sibling}','{gym}','2026-10-10','{lp}','pending','instagram','story','cap','{OLD}')")
    fp, _ = _begin(gym, "act-loc", str(primary))
    url = f"{ORIGIN}/echo/{gym}_ig/{HEX16}/fresh.jpg"
    selected = json.dumps({"asset_id": "local:fresh.jpg", "image_url": url,
                           "kind": "photo", "key": "fresh.jpg"})
    planned = json.dumps({str(sibling): {"image_url": url, "source_media_url": None,
                                         "thumbnail_url": None,
                                         "source_media_asset_id": "local:fresh.jpg"}})
    out = sql(f"select status, selected_asset->>'asset_id'"
              f" from public.portal_action_receipt_claim_selection("
              f"'{gym}','act-loc','{fp}','{selected}'::jsonb,'{planned}'::jsonb)")
    assert out == "selected|local:fresh.jpg"
    rows = json.dumps({"rows": [
        {"calendar_row_id": str(r), "media": {"image_url": url, "source_media_url": None,
                                              "thumbnail_url": None,
                                              "source_media_asset_id": "local:fresh.jpg"}}
        for r in (primary, sibling)]})
    assert sql(f"select status from public.portal_action_receipt_apply("
               f"'{gym}','act-loc','{fp}','{rows}'::jsonb)") == "succeeded"
    n = sql(f"select count(*) from public.content_calendar"
            f" where id in ('{primary}','{sibling}')"
            " and source_media_asset_id='local:fresh.jpg'")
    assert n == "2"


@pytestmark_live
def test_live_apply_rechecks_eligibility_flip(pg):
    """The candidate must REMAIN eligible: an exclusion/eligibility flip
    between claim and apply aborts the whole group (no writes, receipt stays
    selected) even though the frozen identity matches exactly."""
    asset, primary, sibling, _ = _seed_gym("gym-flip")
    fp, _ = _begin("gym-flip", "act-flip", primary)
    _claim("gym-flip", "act-flip", fp, asset, NEW_FOR("gym-flip"), primary, sibling)
    sql(f"update public.media_asset set eligible=false where id='{asset}'")
    err = _apply_err("gym-flip", "act-flip", fp, [primary, sibling], asset)
    assert "own-tenant allowlisted" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    assert sql("select status from public.portal_action_receipt"
               " where gym_id='gym-flip' and action_id='act-flip'") == "selected"
    sql(f"update public.media_asset set eligible=true where id='{asset}'")
    assert _apply("gym-flip", "act-flip", fp, [primary, sibling], asset) == "succeeded"


# ---------------------------------------------------------------------------
# Repair wave 2026-10-04 (Sol P2 recheck): trusted-origin URL binding and
# the apply-phase media_asset FOR SHARE serialization
# ---------------------------------------------------------------------------

@pytestmark_live
def test_live_claim_fails_closed_without_trusted_origin(pg):
    """No operator seed (or a malformed one) => claim refuses closed; no
    selection is ever persisted against an unconfigured environment."""
    sql("delete from public.portal_action_receipt_config")
    try:
        asset, primary, sibling, _ = _seed_gym("gym-noorg")
        fp, _ = _begin("gym-noorg", "act-noorg", primary)
        selected = json.dumps({"asset_id": asset, "image_url": NEW_FOR("gym-noorg")})
        err = sql(f"select public.portal_action_receipt_claim_selection('gym-noorg','act-noorg',"
                  f"'{fp}','{selected}'::jsonb,'{{}}'::jsonb)", expect_error=True)
        assert "no trusted public media origin configured; refusing closed" in err
        # a non-bare origin (path / uppercase / trailing slash / userinfo /
        # non-https) is refused by the table CHECK itself
        for bad in ("https://cdn.example.com/media", "https://CDN.example.com",
                    "https://cdn.example.com/", "http://cdn.example.com",
                    "https://user@cdn.example.com"):
            seeded = sql(f"insert into public.portal_action_receipt_config(key, value)"
                         f" values('public_origin','{bad}')", expect_error=True)
            assert "portal_action_receipt_config_public_origin_shape" in seeded or "violates" in seeded
        assert sql("select count(*) from public.portal_action_receipt"
                   " where gym_id='gym-noorg' and selected_asset is not null") == "0"
    finally:
        sql("delete from public.portal_action_receipt_config")
        sql("insert into public.portal_action_receipt_config(key, value)"
            " values('public_origin','https://cdn.example.com')")
    # ...and with the operator seed restored the exact same claim succeeds
    out = _claim("gym-noorg", "act-noorg", fp, asset, NEW_FOR("gym-noorg"), primary, sibling)
    assert out.startswith("selected|")


@pytestmark_live
def test_live_claim_rejects_evil_host_and_origin_path_tricks(pg):
    """strpos-anywhere is gone: the URL must BE <trusted origin>/echo/<slug
    or slug_ig>/<16-hex>/<file>. Evil/lookalike hosts, userinfo, embedded
    tenant paths, slug-prefix tricks, cross-gym paths and traversal all
    fail; only the exact trusted shape is accepted."""
    asset, primary, sibling, _ = _seed_gym("gym-evil")
    fp, _ = _begin("gym-evil", "act-evil", primary)
    good = NEW_FOR("gym-evil", "x.jpg")
    bad_urls = [
        f"https://evil.example.com/echo/gym-evil/{HEX16}/x.jpg",   # evil host
        f"https://cdn.example.com.evil.com/echo/gym-evil/{HEX16}/x.jpg",  # host suffix
        f"https://cdn.example.com@evil.example.com/echo/gym-evil/{HEX16}/x.jpg",  # userinfo
        f"https://cdn2.example.com/echo/gym-evil/{HEX16}/x.jpg",   # wrong configured origin
        f"https://cdn.example.com/static/echo/gym-evil/{HEX16}/x.jpg",  # not anchored
        f"https://cdn.example.com/echo/gym-evilx/{HEX16}/x.jpg",   # slug-prefix trick
        f"https://cdn.example.com/echo/gym-evilx_ig/{HEX16}/x.jpg",  # _ig slug-prefix trick
        f"https://cdn.example.com/echo/othergym/{HEX16}/x.jpg",    # cross-gym path
        f"https://cdn.example.com/echo/gym-evil/{HEX16}/x.jpg?sig=tok",  # query/token
        f"https://cdn.example.com/echo/gym-evil/{HEX16}/x.jpg#frag",     # fragment
        f"https://cdn.example.com/echo/gym-evil/{HEX16}/../other/x.jpg",  # traversal
        f"https://cdn.example.com/echo/gym-evil/{HEX16}/%2e%2e/other/x.jpg",  # encoded traversal
        f"https://cdn.example.com/echo/gym-evil/{HEX16}/a%2fb.jpg",      # encoded slash
        f"https://cdn.example.com/echo/gym-evil/nothex16/x.jpg",   # no content address
        f"https://evil.example.com/?next={good}",                  # good URL only embedded
        f"http://cdn.example.com/echo/gym-evil/{HEX16}/x.jpg",     # wrong scheme
    ]
    for url in bad_urls:
        selected = json.dumps({"asset_id": asset, "image_url": url})
        err = sql(f"select public.portal_action_receipt_claim_selection('gym-evil','act-evil',"
                  f"'{fp}','{selected}'::jsonb,'{{}}'::jsonb)", expect_error=True)
        assert "hosted media prefix" in err or "allowlisted public object identity" in err, url
        assert sql("select selected_asset is null from public.portal_action_receipt"
                   " where gym_id='gym-evil' and action_id='act-evil'") == "t"
    # the exact trusted shape (and the _ig local/story variant) is accepted
    out = _claim("gym-evil", "act-evil", fp, asset, good, primary, sibling)
    assert out.startswith("selected|")
    ig_url = f"{ORIGIN}/echo/gym-evil_ig/{HEX16}/x.jpg"
    assert sql(f"select public.portal_action_receipt_hosted_url('{ig_url}','{ORIGIN}','gym-evil')") == "t"


@pytestmark_live
def test_live_apply_media_asset_for_share_serializes_concurrent_flip(pg):
    """Sol P2: the apply-phase eligibility recheck locks the asset FOR SHARE
    through the calendar writes. A coach flip committed between claim and
    the recheck is REFUSED; a flip racing the recheck BLOCKS until apply
    finishes -- it can never slip between the recheck and the writes."""
    asset, primary, sibling, _ = _seed_gym("gym-alock")
    fp, _ = _begin("gym-alock", "act-alock", primary)
    _claim("gym-alock", "act-alock", fp, asset, NEW_FOR("gym-alock"), primary, sibling)

    # concurrent coach exclusion holds the asset row lock (uncommitted):
    # apply's FOR SHARE recheck must WAIT, then time out -- never write
    holder = subprocess.Popen(
        [str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-h", SOCK, "-d", DB],
        stdin=subprocess.PIPE, text=True)
    holder.stdin.write("begin;\n")
    holder.stdin.write(f"update public.media_asset set excluded_by_coach=true where id='{asset}';\n")
    holder.stdin.flush()
    time.sleep(0.6)
    rows = _rows_json([primary, sibling], asset, NEW_FOR("gym-alock"))
    err = sql(f"set statement_timeout=1500; select status from public.portal_action_receipt_apply("
              f"'gym-alock','act-alock','{fp}','{rows}'::jsonb)", expect_error=True)
    assert "statement timeout" in err or "canceling statement" in err
    # serialized: nothing was written while the flip was in flight
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    assert sql("select status from public.portal_action_receipt"
               " where gym_id='gym-alock' and action_id='act-alock'") == "selected"
    # the flip commits; the recheck now SEES it and refuses the group
    holder.stdin.write("commit; \\q\n")
    holder.stdin.flush()
    holder.wait(timeout=30)
    err = _apply_err("gym-alock", "act-alock", fp, [primary, sibling], asset)
    assert "own-tenant allowlisted" in err
    assert sql(f"select image_url from public.content_calendar where id='{primary}'") == OLD
    # flip reverted: the exact frozen selection applies cleanly
    sql(f"update public.media_asset set excluded_by_coach=false where id='{asset}'")
    assert _apply("gym-alock", "act-alock", fp, [primary, sibling], asset) == "succeeded"


@pytestmark_live
def test_live_media_asset_for_share_blocks_concurrent_update(pg):
    """Lock-mode proof: the FOR SHARE row lock apply takes conflicts with a
    concurrent coach UPDATE row lock (and is released at commit)."""
    asset, _, _, _ = _seed_gym("gym-fshare")
    holder = subprocess.Popen(
        [str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-h", SOCK, "-d", DB],
        stdin=subprocess.PIPE, text=True)
    holder.stdin.write("begin;\n")
    holder.stdin.write(f"select 1 from public.media_asset where id='{asset}' for share;\n")
    holder.stdin.flush()
    time.sleep(0.6)
    err = sql(f"set statement_timeout=1000; update public.media_asset"
              f" set excluded_by_coach=true where id='{asset}'", expect_error=True)
    assert "statement timeout" in err or "canceling statement" in err
    holder.stdin.write("commit; \\q\n")
    holder.stdin.flush()
    holder.wait(timeout=30)
    # after the FOR SHARE lock is released the coach update goes through
    sql(f"update public.media_asset set excluded_by_coach=true where id='{asset}'")
