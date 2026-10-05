"""Guard the temporary LASSO claim envelope against a fleet-wide cap change."""

from pathlib import Path

from agent import calendar_autopublish as cap
from agent import cadence


SQL = (Path(__file__).parents[1] / "migrations" /
       "lasso_bounded_catchup_capacity_20261005.sql").read_text().lower()


def test_only_lasso_can_claim_temporary_capacity_five():
    assert "p_capacity > 3 and p_capacity <> 5" in SQL
    assert "p_capacity = 3 and p_gym_id <> 'lasso'" in SQL
    assert "p_capacity = 5 and not (\n      p_gym_id = 'lasso'" in SQL
    assert "p_day between date '2026-10-06' and date '2026-10-11'" in SQL
    assert "p_timezone = 'america/new_york'" in SQL
    assert "v_row.post_date = p_day" in SQL
    assert "v_row.post_date between date '2026-10-02' and date '2026-10-05'" in SQL


def test_three_current_plus_two_backlog_per_account_format_on_actual_day():
    assert "v_used >= p_capacity" in SQL
    assert "v_current_used >= 3" in SQL
    assert "v_backlog_used >= 2" in SQL
    assert "lower(btrim(coalesce(account, '')))" in SQL
    assert "coalesce(nullif(lower(btrim(format)), ''), 'feed')" in SQL
    assert "status = 'publishing' and publish_reservation_day = p_day" in SQL
    assert "(published_at at time zone p_timezone)::date = p_day" in SQL
    assert "pg_advisory_xact_lock(hashtextextended(p_gym_id, 0))" in SQL


def test_existing_claim_and_media_safety_are_preserved():
    for fragment in (
        "id = p_row_id and gym_id = p_gym_id",
        "status in ('pending', 'approved')",
        "published_at is null",
        "late_post_id is null",
        "variant_status = 'active'",
        "nullif(btrim(coalesce(image_url, '')), '') is not null",
        "media_not_ready_reason is null",
        "p_approved_only and v_row.status <> 'approved'",
        "publish_claim_token = v_token",
        "to service_role",
    ):
        assert fragment in SQL


def test_immediate_fifty_and_residual_twenty_keep_client_cap(monkeypatch):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled", lambda: True)
    # Oct 5-6 immediate drain: aggregate 50, lookback from Oct 2.
    assert cap._client_publish_limits("lasso", "2026-10-05", 8) == (7, 50)
    assert cap._client_publish_limits("lasso", "2026-10-06", 8) == (7, 50)
    # Residual Oct 7-11 mode is unchanged.
    assert cap._client_publish_limits("lasso", "2026-10-07", 8) == (7, 20)
    assert cap._client_publish_limits("lasso", "2026-10-11", 8) == (9, 20)
    assert cap._client_publish_limits("lasso", "2026-10-12", 8) == (7, 12)
    # Other tenants are never enlarged.
    assert cap._client_publish_limits("client-gym", "2026-10-05", 8) == (7, 8)
    assert cap._client_publish_limits("client-gym", "2026-10-06", 8) == (7, 8)


def test_flag_off_keeps_normal_twelve_on_immediate_days(monkeypatch):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled", lambda: False)
    monkeypatch.setattr(cap.config, "lasso_summit_daily_enabled",
                        lambda day: False)
    assert cap._client_publish_limits("lasso", "2026-10-05", 8) == (7, 8)
    assert cap._client_publish_limits("lasso", "2026-10-06", 8) == (7, 8)


def _publish_capacity(monkeypatch, gym, row, day, *, durable=True):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled",
                        lambda: durable)
    monkeypatch.setattr(cap.config, "lasso_summit_daily_enabled",
                        lambda d: False)
    monkeypatch.setattr(cadence, "resolve_posts_per_day",
                        lambda gym_id, store, day=None: 3)
    return cap._publish_capacity(gym, row, object(), day)


def test_immediate_fifteen_claims_current_and_strict_backlog_rows(monkeypatch):
    row = {"format": "feed", "post_date": "2026-10-02"}
    # Oct 5 publish day: Oct 5 rows are current, Oct 2-4 are backlog.
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-05") == 15
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-05"},
        "2026-10-05") == 15
    # Oct 6 publish day: Oct 2-5 are backlog, Oct 6 is current.
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-06") == 15
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "format": "story"}, "2026-10-06") == 15
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-05"},
        "2026-10-06") == 15
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-06"},
        "2026-10-06") == 15
    # Outside the outage dates or outside the publish window: normal 3.
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-01"},
        "2026-10-05") == 3
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-06"},
        "2026-10-05") == 3
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-12") == 3
    # Other tenants are never enlarged, even on Oct 5-6 with the flag on.
    assert _publish_capacity(monkeypatch, "client-gym", row, "2026-10-05") == 3
    assert _publish_capacity(monkeypatch, "client-gym", row, "2026-10-06") == 3


def test_flag_off_keeps_normal_three_on_immediate_days(monkeypatch):
    row = {"format": "feed", "post_date": "2026-10-02"}
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-05",
                             durable=False) == 3
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-06",
                             durable=False) == 3


def test_residual_five_mode_survives_only_after_immediate_window(monkeypatch):
    row = {"format": "feed", "post_date": "2026-10-02"}
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-07") == 5
    assert _publish_capacity(
        monkeypatch, "lasso", {**row, "post_date": "2026-10-07"},
        "2026-10-07") == 5
    assert _publish_capacity(monkeypatch, "lasso", row, "2026-10-11") == 5


# ---------------------------------------------------------------------------
# Live claim proof against a disposable PostgreSQL 17 instance (Unix socket
# only, no network). Skips when local PG17 binaries are missing. Applies the
# residual catchup migration, then the immediate-drain migration TWICE (the
# CREATE OR REPLACE must be idempotently re-appliable).
# ---------------------------------------------------------------------------

import subprocess as _subprocess
import tempfile as _tempfile
import uuid as _uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
PGBIN_CANDIDATES = [Path("/opt/homebrew/opt/postgresql@17/bin"),
                    Path("/usr/local/opt/postgresql@17/bin")]
NY = "America/New_York"


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

    with _tempfile.TemporaryDirectory(prefix="lasso-immediate-cap-") as path:
        root = Path(path)
        run(str(pgbin / "initdb"), "-D", str(root / "data"), "-U", "postgres",
            "--auth=trust", "--no-instructions")
        run(str(pgbin / "pg_ctl"), "-D", str(root / "data"), "-l", str(root / "log"),
            "-o", f"-k {root} -c listen_addresses='' -c fsync=off", "-w", "start")

        def sql(statement):
            return run(str(pgbin / "psql"), "-U", "postgres", "-X", "-q", "-A",
                       "-t", "-v", "ON_ERROR_STOP=1", "-h", str(root),
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
                publish_claim_token uuid);""")
            for name in ("lasso_bounded_catchup_capacity_20261005.sql",
                         "lasso_immediate_backlog_capacity_20261005.sql",
                         # Re-applied: CREATE OR REPLACE must be idempotent.
                         "lasso_immediate_backlog_capacity_20261005.sql"):
                run(str(pgbin / "psql"), "-U", "postgres", "-X", "-q",
                    "-v", "ON_ERROR_STOP=1", "-h", str(root), "-d", "postgres",
                    "-f", str(ROOT / "migrations" / name))
            yield sql
        finally:
            run(str(pgbin / "pg_ctl"), "-D", str(root / "data"),
                "-m", "immediate", "stop")


def _insert(sql, *, gym="lasso", account="instagram", fmt="feed",
            post_date, status="pending", reservation=None, published=False):
    row_id = str(_uuid.uuid4())
    published_cols = "now(), 'receipt'" if published else "null, null"
    sql(f"""insert into public.content_calendar
      (id,gym_id,account,post_date,format,caption,image_url,status,
       variant_status,publish_reservation_day,published_at,late_post_id)
      values ('{row_id}','{gym}','{account}','{post_date}','{fmt}','cap',
       'https://cdn.example/m.png','{status}','active',
       {f"'{reservation}'" if reservation else "null"},{published_cols});""")
    return row_id


def _claim(sql, row_id, *, gym="lasso", day, tz=NY, capacity=15,
           approved_only="false"):
    return sql(
        "select coalesce(public.claim_calendar_publish_slot_owned("
        f"'{row_id}'::uuid, '{gym}', "
        + (f"'{day}'::date" if day else "null") + ", "
        + (f"'{tz}'" if tz else "null") + ", "
        + (str(capacity) if capacity is not None else "null") + ", "
        + approved_only + ")::text, 'NULL')")


def test_rpc_rejects_null_and_invalid_arguments(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    row = _insert(sql, post_date="2026-10-05")
    # NULL-safe: a NULL capacity, approved_only, day or timezone claims nothing.
    assert _claim(sql, row, day="2026-10-05", capacity=None) == "NULL"
    assert _claim(sql, row, day="2026-10-05", approved_only="null") == "NULL"
    assert _claim(sql, row, day=None) == "NULL"
    assert _claim(sql, row, day="2026-10-05", tz=None) == "NULL"
    # Invalid window, timezone, capacity and tenant all fail closed.
    assert _claim(sql, row, day="2026-10-04") == "NULL"
    assert _claim(sql, row, day="2026-10-07") == "NULL"
    assert _claim(sql, row, day="2026-10-05", tz="UTC") == "NULL"
    assert _claim(sql, row, day="2026-10-05", capacity=16) == "NULL"
    assert _claim(sql, row, day="2026-10-05", gym="client-gym") == "NULL"
    # The residual 5-mode no longer covers Oct 5-6 but still covers Oct 7-11.
    assert _claim(sql, row, day="2026-10-06", capacity=5) == "NULL"
    # Nothing was claimed by any rejection above.
    assert sql(f"select status from public.content_calendar "
               f"where id='{row}'") == "pending"
    residual = _insert(sql, post_date="2026-10-07")
    assert _claim(sql, residual, day="2026-10-07", capacity=5) != "NULL"


def test_rpc_immediate_envelope_ceilings_and_no_oct5_overlap(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    # Oct 5 publish day: exactly three current-day rows claim.
    current = [_insert(sql, post_date="2026-10-05") for _ in range(4)]
    tokens = [_claim(sql, r, day="2026-10-05") for r in current]
    assert all(t != "NULL" for t in tokens[:3])
    # The fourth current-day row is over the current ceiling.
    assert tokens[3] == "NULL"
    # Backlog (Oct 2-4, strictly before Oct 5): the three current Oct-5 rows
    # already claimed must NOT count against the backlog ceiling, so a full
    # twelve backlog rows still claim; the thirteenth does not.
    backlog = [_insert(sql, post_date=f"2026-10-0{d}")
               for d in (2, 3, 4, 2, 3, 4, 2, 3, 4, 2, 3, 4, 2)]
    btokens = [_claim(sql, r, day="2026-10-05") for r in backlog]
    assert all(t != "NULL" for t in btokens[:12])
    assert btokens[12] == "NULL"
    # The envelope is exactly 15 for this account/format: the rejected rows
    # stay pending and unclaimed.
    assert sql("select count(*) from public.content_calendar "
               "where status='publishing' "
               "and publish_reservation_day='2026-10-05'") == "15"


def test_rpc_oct6_backlog_includes_oct5_and_ceilings_hold(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    # On the Oct 6 publish day, Oct 5 is backlog, not current.
    dates = ["2026-10-06"] * 4 + ["2026-10-05"] * 7 + ["2026-10-03"] * 6
    rows = [_insert(sql, post_date=d) for d in dates]
    tokens = [_claim(sql, r, day="2026-10-06") for r in rows]
    assert all(t != "NULL" for t in tokens[:3])   # 3 current
    assert tokens[3] == "NULL"                    # 4th current held
    assert all(t != "NULL" for t in tokens[4:16])  # 12 backlog
    assert tokens[16] == "NULL"                    # 13th backlog held
    assert sql("select count(*) from public.content_calendar "
               "where status='publishing'") == "15"


def test_rpc_account_and_format_independence(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    # Saturate instagram/feed on Oct 5 (3 current + 12 backlog).
    for d in ["2026-10-05"] * 3 + ["2026-10-02"] * 12:
        assert _claim(sql, _insert(sql, post_date=d),
                      day="2026-10-05") != "NULL"
    # instagram/story and facebook/feed keep their own full envelopes.
    story = _insert(sql, post_date="2026-10-05", fmt="story")
    fb = _insert(sql, post_date="2026-10-05", account="facebook")
    assert _claim(sql, story, day="2026-10-05") != "NULL"
    assert _claim(sql, fb, day="2026-10-05") != "NULL"


def test_rpc_claim_is_exactly_once_and_other_tenants_unchanged(pg):
    sql = pg
    sql("delete from public.content_calendar;")
    row = _insert(sql, post_date="2026-10-02")
    first = _claim(sql, row, day="2026-10-05")
    assert first != "NULL"
    # A second claim of the same row returns NULL (exactly-once CAS), and the
    # original token is preserved.
    assert _claim(sql, row, day="2026-10-05") == "NULL"
    assert sql(f"select publish_claim_token::text from "
               f"public.content_calendar where id='{row}'") == first
    # approved_only is enforced against the row's real status.
    approved = _insert(sql, post_date="2026-10-02", status="approved")
    pending = _insert(sql, post_date="2026-10-02")
    assert _claim(sql, pending, day="2026-10-05",
                  approved_only="true") == "NULL"
    assert _claim(sql, approved, day="2026-10-05",
                  approved_only="true") != "NULL"
    # Other tenants: normal capacity still claims, the enlarged one never does.
    client = _insert(sql, gym="client-gym", post_date="2026-10-02")
    assert _claim(sql, client, gym="client-gym", day="2026-10-05",
                  capacity=2) != "NULL"
    client2 = _insert(sql, gym="client-gym", post_date="2026-10-02")
    assert _claim(sql, client2, gym="client-gym", day="2026-10-05",
                  capacity=15) == "NULL"
