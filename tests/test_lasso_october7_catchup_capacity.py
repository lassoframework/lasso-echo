"""LASSO October 7 catchup: a bounded capacity-6 envelope on Oct 8-9 only.

Proves the selector (``_publish_capacity`` / ``_client_publish_limits``) and
the claim RPC both open exactly 3 current-day + 3 October-7 backlog slots per
account/format on the two America/New_York publish days, and that every other
tenant, window, class and guard is unchanged.
"""

import re
import subprocess as _subprocess
import tempfile as _tempfile
import uuid as _uuid
from pathlib import Path

import pytest

from agent import calendar_autopublish as cap
from agent import cadence

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "lasso_october7_catchup_capacity_20261008.sql"
NY = "America/New_York"


# ---------------------------------------------------------------------------
# Python selector proofs (no database).
# ---------------------------------------------------------------------------

def _publish_capacity(monkeypatch, gym, row, day, *, durable=True):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled",
                        lambda: durable)
    monkeypatch.setattr(cap.config, "lasso_summit_daily_enabled",
                        lambda d: False)
    monkeypatch.setattr(cadence, "resolve_posts_per_day",
                        lambda gym_id, store, day=None: 3)
    return cap._publish_capacity(gym, {"account": "instagram", "slot_index": 0, **row}, object(), day)


def test_six_only_inside_the_oct8_9_window(monkeypatch):
    row = {"format": "feed", "post_date": "2026-10-08"}
    backlog = {"format": "feed", "post_date": "2026-10-07"}
    story = {"format": "story", "post_date": "2026-10-08"}
    # Same-day and exact October 7 backlog rows enlarge on both window days.
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-08") == 6
    assert _publish_capacity(monkeypatch, "lasso", backlog, "2026-10-08") == 6
    assert _publish_capacity(monkeypatch, "lasso", story, "2026-10-08") == 6
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-09"},
        "2026-10-09") == 6
    assert _publish_capacity(monkeypatch, "lasso", backlog, "2026-10-09") == 6
    # October 7 rows are backlog only, strictly before the claim day: on
    # October 7 itself they keep the residual 5 envelope.
    assert _publish_capacity(monkeypatch, "lasso", backlog, "2026-10-07") == 5
    # Older backlog dates are NOT covered by the six envelope.
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-06"},
        "2026-10-08") == 3
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-02"},
        "2026-10-08") == 5
    # After the window the normal cadence capacity returns.
    assert _publish_capacity(monkeypatch, "lasso", backlog, "2026-10-10") == 3
    assert _publish_capacity(monkeypatch, "lasso", backlog, "2026-10-12") == 3
    # The dated 5 and 15 envelopes are untouched.
    assert _publish_capacity(
        monkeypatch, "lasso", {"format": "feed", "post_date": "2026-10-02"},
        "2026-10-06") == 15
    assert _publish_capacity(
        monkeypatch, "lasso", {"format": "feed", "post_date": "2026-10-02"},
        "2026-10-07") == 5
    assert _publish_capacity(
        monkeypatch, "lasso", {"format": "feed", "post_date": "2026-10-02"},
        "2026-10-11") == 5
    # Other tenants and other formats are never enlarged.
    assert _publish_capacity(monkeypatch, "client-gym", row, "2026-10-08") == 3
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "format": "reel"}, "2026-10-08") != 6


def test_flag_off_keeps_normal_three_on_window_days(monkeypatch):
    row = {"format": "feed", "post_date": "2026-10-08"}
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-08",
                             durable=False) == 3
    backlog = {"format": "feed", "post_date": "2026-10-07"}
    assert _publish_capacity(monkeypatch, "lasso", backlog, "2026-10-09",
                             durable=False) == 3


def test_client_limits_twenty_four_aggregate_in_window_only(monkeypatch):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(cap.config, "lasso_summit_daily_enabled",
                        lambda day: False)
    # The window lifts the aggregate to 24 with the existing lookback 7.
    assert cap._client_publish_limits("lasso", "2026-10-08", 8) == (7, 24)
    assert cap._client_publish_limits("lasso", "2026-10-09", 8) == (7, 24)
    # The residual and immediate envelopes keep their own aggregates.
    assert cap._client_publish_limits("lasso", "2026-10-07", 8) == (7, 20)
    assert cap._client_publish_limits("lasso", "2026-10-10", 8) == (8, 20)
    assert cap._client_publish_limits("lasso", "2026-10-11", 8) == (9, 20)
    assert cap._client_publish_limits("lasso", "2026-10-06", 8) == (7, 50)
    assert cap._client_publish_limits("lasso", "2026-10-12", 8) == (7, 12)
    # Other tenants are never enlarged.
    assert cap._client_publish_limits("client-gym", "2026-10-08", 8) == (7, 8)


# ---------------------------------------------------------------------------
# Live claim proof against a disposable PostgreSQL 17 instance (Unix socket
# only, no network). Skips when local PG17 binaries are missing. Loads the
# current live 7-arg claim function (control), then the new migration.
# ---------------------------------------------------------------------------

PGBIN_CANDIDATES = [Path("/opt/homebrew/opt/postgresql@17/bin"),
                    Path("/usr/local/opt/postgresql@17/bin")]
PG_LOG = Path("/tmp/lasso_oct7_catchup_pg.log")

_PROVENANCE = (ROOT / "migrations" /
               "calendar_approval_provenance_20261005.sql").read_text()
_start = _PROVENANCE.index(
    "create or replace function public.claim_calendar_publish_slot_owned(")
_end = _PROVENANCE.index("$$;", _start) + 3
BEFORE_SQL = _PROVENANCE[_start:_end]


def _pgbin():
    for cand in PGBIN_CANDIDATES:
        if (cand / "postgres").exists() and (cand / "initdb").exists():
            return cand
    return None


@pytest.fixture(scope="module")
def pg():
    pgbin = _pgbin()
    if pgbin is None:
        pytest.skip("PostgreSQL 17 unavailable")

    def run(*args):
        return _subprocess.run(args, text=True, capture_output=True, check=True)

    with _tempfile.TemporaryDirectory(prefix="lasso-oct7-cap-") as path:
        root = Path(path)
        try:
            run(str(pgbin / "initdb"), "-D", str(root / "data"), "-U",
                "postgres", "--auth=trust", "--no-instructions")
            run(str(pgbin / "pg_ctl"), "-D", str(root / "data"),
                "-l", str(PG_LOG),
                "-o", f"-k {root} -c listen_addresses='' -c fsync=off",
                "-w", "start")
        except Exception:
            # The startup log must survive the TemporaryDirectory cleanup.
            print(PG_LOG.read_text())
            raise

        def sql(statement):
            return run(str(pgbin / "psql"), "-U", "postgres", "-X", "-q",
                       "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", str(root),
                       "-d", "postgres", "-c", statement).stdout.strip()

        try:
            sql("create role service_role; create role anon; "
                "create role authenticated")
            sql("""create table public.content_calendar (
                id uuid primary key, gym_id text, account text, post_date date,
                pillar text, format text, caption text, image_url text,
                source_media_url text, source_media_asset_id text,
                thumbnail_url text, created_at timestamptz,
                publish_reservation_day date,
                status text, scheduled_at timestamptz, slot_index integer,
                variant_status text, logical_post_id uuid,
                media_not_ready_reason text,
                published_at timestamptz, late_post_id text,
                publish_claim_token uuid,
                approval_kind text, approved_by text, approved_at timestamptz,
                approval_digest text);""")
            # Mutable autonomy stub plus a deterministic digest stub.
            sql("create table public.test_flags (k text primary key, "
                "v boolean);")
            sql("""create function public.calendar_gym_is_autonomous(text)
                returns boolean language sql stable as
                $$ select coalesce((select v from public.test_flags
                     where k = 'auto'), true) $$;""")
            sql("""create function public.calendar_approval_digest(
                p_row public.content_calendar) returns text
                language sql stable as
                $$ select md5(p_row.id::text || coalesce(p_row.caption, '')
                     || coalesce(p_row.image_url, '')) $$;""")
            # Control: the current live 7-arg function first.
            sql(BEFORE_SQL)
            yield sql
        finally:
            run(str(pgbin / "pg_ctl"), "-D", str(root / "data"),
                "-m", "immediate", "stop")


def _insert(sql, *, gym="lasso", account="instagram", fmt="feed",
            post_date, slot=0, status="pending", reservation=None,
            published=False, claim_token=None, held=False, no_image=False,
            approved_proof=False):
    row_id = str(_uuid.uuid4())
    published_cols = "now(), 'receipt'" if published else "null, null"
    sql(f"""insert into public.content_calendar
      (id,gym_id,account,post_date,format,caption,image_url,status,
       variant_status,slot_index,publish_reservation_day,published_at,
       late_post_id,publish_claim_token,media_not_ready_reason)
      values ('{row_id}','{gym}',
       {f"'{account}'" if account is not None else "null"},
       {f"'{post_date}'" if post_date is not None else "null"},
       '{fmt}','cap',
       {'null' if no_image else "'https://cdn.example/m.png'"},
       '{status}','active',{slot},
       {f"'{reservation}'" if reservation else "null"},{published_cols},
       {f"'{claim_token}'::uuid" if claim_token else "null"},
       {'null' if not held else "'awaiting_media'"});""")
    if approved_proof:
        sql(f"""update public.content_calendar set
            status = 'approved', approval_kind = 'human',
            approved_by = 'user_blake', approved_at = now(),
            approval_digest = public.calendar_approval_digest(content_calendar)
            where id = '{row_id}';""")
    return row_id


def _claim(sql, row_id, *, gym="lasso", day, tz=NY, capacity=6,
           approved_only="false", require_proof="false"):
    return sql(
        "select coalesce(public.claim_calendar_publish_slot_owned("
        f"'{row_id}'::uuid, '{gym}', "
        + (f"'{day}'::date" if day else "null") + ", "
        + (f"'{tz}'" if tz else "null") + ", "
        + (str(capacity) if capacity is not None else "null") + ", "
        + approved_only + ", " + require_proof + ")::text, 'NULL')")


def test_before_function_rejects_capacity_six(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    row = _insert(sql, post_date="2026-10-08")
    # Control: the current live function has no six envelope at all.
    assert _claim(sql, row, day="2026-10-08", capacity=6) == "NULL"
    assert sql(f"select status from public.content_calendar "
               f"where id='{row}'") == "pending"
    # Load the new migration twice: CREATE OR REPLACE must be idempotent.
    for _ in range(2):
        _subprocess.run([str(_pgbin() / "psql"), "-U", "postgres", "-X", "-q",
                         "-v", "ON_ERROR_STOP=1",
                         "-h", str(Path(sql("show unix_socket_directories"))),
                         "-d", "postgres", "-f", str(MIGRATION)],
                        text=True, capture_output=True, check=True)


def test_six_claims_three_current_plus_three_backlog_all_lanes(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    claimed = 0
    for account in ("instagram", "facebook"):
        for fmt in ("feed", "story"):
            current = [_insert(sql, account=account, fmt=fmt,
                               post_date="2026-10-08", slot=s % 3)
                       for s in range(4)]
            backlog = [_insert(sql, account=account, fmt=fmt,
                               post_date="2026-10-07", slot=s % 3)
                       for s in range(4)]
            tokens = [_claim(sql, r, day="2026-10-08") for r in
                      current + backlog]
            # Exactly 3 current + 3 backlog claim; the 4th of each class is
            # denied even though the other class has room.
            assert all(t != "NULL" for t in tokens[:3])
            assert tokens[3] == "NULL"
            assert all(t != "NULL" for t in tokens[4:7])
            assert tokens[7] == "NULL"
            claimed += 6
    # The aggregate envelope is exactly 24 for the publish day.
    assert sql("select count(*) from public.content_calendar "
               "where status='publishing' "
               "and publish_reservation_day='2026-10-08'") == "24"
    assert claimed == 24


def test_oct9_publish_day_and_published_rows_count_against_envelope(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    # Two already-published current-day rows (receipts) count toward the
    # current-day class ceiling on Oct 9.
    for _ in range(2):
        _insert(sql, post_date="2026-10-09", status="published",
                published=True, reservation="2026-10-09")
    current = [_insert(sql, post_date="2026-10-09", slot=s) for s in range(2)]
    backlog = [_insert(sql, post_date="2026-10-07", slot=s % 3) for s in range(4)]
    tokens = [_claim(sql, r, day="2026-10-09") for r in current + backlog]
    assert tokens[0] != "NULL"    # third current slot, receipts counted
    assert tokens[1] == "NULL"    # current class is now at 3
    assert all(t != "NULL" for t in tokens[2:5])
    assert tokens[5] == "NULL"    # backlog class ceiling is 3


def test_refusals_fail_closed(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    row = _insert(sql, post_date="2026-10-08")
    # NULL-safe: a NULL capacity, approved_only, proof flag, day or timezone
    # claims nothing.
    assert _claim(sql, row, day="2026-10-08", capacity=None) == "NULL"
    assert _claim(sql, row, day="2026-10-08", approved_only="null") == "NULL"
    assert _claim(sql, row, day="2026-10-08", require_proof="null") == "NULL"
    assert _claim(sql, row, day=None) == "NULL"
    assert _claim(sql, row, day="2026-10-08", tz=None) == "NULL"
    # Wrong window, timezone, capacity and tenant all fail closed.
    assert _claim(sql, row, day="2026-10-07") == "NULL"
    assert _claim(sql, row, day="2026-10-10") == "NULL"
    assert _claim(sql, row, day="2026-10-08", tz="UTC") == "NULL"
    assert _claim(sql, row, day="2026-10-08", capacity=7) == "NULL"
    assert _claim(sql, row, day="2026-10-08", gym="client-gym") == "NULL"
    # Wrong row date classes: older backlog, future and foreign dates.
    assert _claim(sql, _insert(sql, post_date="2026-10-06"),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-09"),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-02"),
                  day="2026-10-08") == "NULL"
    # Wrong account (IG/FB only), wrong slot and a NULL slot all refuse.
    assert _claim(sql, _insert(sql, post_date="2026-10-08",
                               account="googlebusiness"),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-08", slot=3),
                  day="2026-10-08") == "NULL"
    null_slot = _insert(sql, post_date="2026-10-08")
    sql(f"update public.content_calendar set slot_index = null "
        f"where id = '{null_slot}'")
    assert _claim(sql, null_slot, day="2026-10-08") == "NULL"
    assert sql(f"select status || ':' || coalesce(publish_claim_token::text, 'NULL') "
               f"from public.content_calendar where id='{null_slot}'") == "pending:NULL"
    for field in ("account", "format", "post_date"):
        null_row = _insert(sql, post_date="2026-10-08")
        sql(f"update public.content_calendar set {field}=null where id='{null_row}'")
        assert _claim(sql, null_row, day="2026-10-08") == "NULL"
    for arg in ("null::uuid, 'lasso'", f"'{row}'::uuid, null"):
        assert sql("select coalesce(public.claim_calendar_publish_slot_owned("
                   + arg + ", '2026-10-08', 'America/New_York', 6, false, false)::text, 'NULL')") == "NULL"
    # Held media, missing media, receipts and stale claim state all refuse.
    assert _claim(sql, _insert(sql, post_date="2026-10-08", held=True),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-08", no_image=True),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-08", published=True,
                               status="published"),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-08",
                               claim_token=str(_uuid.uuid4())),
                  day="2026-10-08") == "NULL"
    assert _claim(sql, _insert(sql, post_date="2026-10-08",
                               reservation="2026-10-08"),
                  day="2026-10-08") == "NULL"
    # Nothing above claimed a row.
    assert sql("select count(*) from public.content_calendar "
               "where status='publishing'") == "0"
    # The original row still claims cleanly afterwards.
    assert _claim(sql, row, day="2026-10-08") != "NULL"


def test_manual_mode_proof_gate_is_not_bypassed(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    sql("insert into public.test_flags values ('auto', false) "
        "on conflict (k) do update set v = false;")
    try:
        proven = _insert(sql, post_date="2026-10-08", approved_proof=True)
        pending = _insert(sql, post_date="2026-10-08", slot=1)
        # A Manual gym needs status approved AND a fresh human proof.
        assert _claim(sql, pending, day="2026-10-08",
                      require_proof="true") == "NULL"
        unproven = _insert(sql, post_date="2026-10-08", slot=2,
                           status="approved")
        assert _claim(sql, unproven, day="2026-10-08",
                      require_proof="true") == "NULL"
        stale = _insert(sql, post_date="2026-10-07", approved_proof=True)
        # A post-approval media change invalidates the digest-bound proof.
        sql(f"update public.content_calendar set image_url = "
            f"'https://cdn.example/swapped.png' where id = '{stale}'")
        assert _claim(sql, stale, day="2026-10-08",
                      require_proof="true") == "NULL"
        # The exact proven row still claims at the elevated capacity.
        assert _claim(sql, proven, day="2026-10-08",
                      require_proof="true") != "NULL"
    finally:
        sql("update public.test_flags set v = true where k = 'auto';")


def test_normal_capacity_three_unchanged_after_expiry(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    rows = [_insert(sql, post_date="2026-10-12", slot=s) for s in range(4)]
    tokens = [_claim(sql, r, day="2026-10-12", capacity=3) for r in rows]
    assert all(t != "NULL" for t in tokens[:3])
    assert tokens[3] == "NULL"
    # The residual 5 envelope still covers October 10-11, and a client gym
    # can never claim the elevated capacity.
    residual = _insert(sql, post_date="2026-10-10")
    assert _claim(sql, residual, day="2026-10-10", capacity=5) != "NULL"
    client = _insert(sql, gym="client-gym", post_date="2026-10-08")
    assert _claim(sql, client, gym="client-gym", day="2026-10-08",
                  capacity=2) != "NULL"
    client2 = _insert(sql, gym="client-gym", post_date="2026-10-08")
    assert _claim(sql, client2, gym="client-gym", day="2026-10-08",
                  capacity=6) == "NULL"


def test_migration_preserves_existing_envelopes_byte_for_byte():
    text = MIGRATION.read_text().lower()
    # The 5 and 15 windows and their class ceilings are preserved verbatim.
    for fragment in (
        "p_day between date '2026-10-07' and date '2026-10-11'",
        "p_day between date '2026-10-05' and date '2026-10-06'",
        "v_row.post_date between date '2026-10-02' and date '2026-10-05'",
        "v_backlog_used >= 2",
        "v_backlog_used >= 12",
        "p_capacity = 3 and p_gym_id <> 'lasso'",
        "pg_advisory_xact_lock(hashtextextended(p_gym_id, 0))",
        "p_require_approval_proof boolean default false",
        "v_enforce_proof := p_require_approval_proof",
        "public.calendar_gym_is_autonomous(p_gym_id)",
        "approval_kind is distinct from 'human'",
        "public.calendar_approval_digest(v_row)",
        "media_not_ready_reason is null",
        "nullif(btrim(coalesce(image_url, '')), '') is not null",
        "v_row.publish_claim_token is not null",
        "security definer",
        "to service_role",
    ):
        assert fragment in text
    # The six envelope is additive only: dated, tenant-scoped, tz-scoped.
    assert "p_capacity not in (5, 6, 15)" in text
    assert "p_day between date '2026-10-08' and date '2026-10-09'" in text
    assert "v_row.post_date = date '2026-10-07'" in text
    assert "v_row.slot_index in (0, 1, 2)" in text
    assert "in ('instagram', 'facebook')" in text
    assert "v_current_used >= 3" in text
    assert re.search(r"v_row\.post_date < p_day and v_backlog_used >= 3", text)


@pytest.mark.parametrize("changes", [
    {"account": None}, {"account": "googlebusiness"},
    {"format": None}, {"format": ""}, {"format": "reel"},
    {"post_date": None}, {"slot_index": None}, {"slot_index": True},
    {"slot_index": "0"}, {"slot_index": -1}, {"slot_index": 3},
])
def test_selector_six_requires_literal_eligible_fields(monkeypatch, changes):
    row = {"account": "instagram", "format": "feed", "post_date": "2026-10-08", "slot_index": 0}
    assert _publish_capacity(monkeypatch, "lasso", {**row, **changes}, "2026-10-08") != 6


@pytest.mark.parametrize("post_date", ["2026-10-08", "2026-10-07"])
def test_each_class_stops_at_three_when_other_class_is_empty(pg, post_date):
    sql = pg
    sql("delete from public.content_calendar")
    rows = [_insert(sql, post_date=post_date, slot=index % 3) for index in range(4)]
    tokens = [_claim(sql, row, day="2026-10-08") for row in rows]
    assert all(token != "NULL" for token in tokens[:3])
    assert tokens[3] == "NULL"
    assert sql("select count(*) from public.content_calendar where status='publishing'") == "3"


def test_published_timestamp_without_reservation_consumes_current_capacity(pg):
    sql = pg
    sql("delete from public.content_calendar")
    for slot in range(3):
        row = _insert(sql, post_date="2026-10-09", slot=slot, status="published", published=True)
        sql(f"update public.content_calendar set published_at='2026-10-09 17:00:00+00' where id='{row}'")
    current = _insert(sql, post_date="2026-10-09")
    assert _claim(sql, current, day="2026-10-09") == "NULL"
    backlog = _insert(sql, post_date="2026-10-07")
    assert _claim(sql, backlog, day="2026-10-09") != "NULL"
