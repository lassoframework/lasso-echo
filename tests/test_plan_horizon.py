"""
HARD planning-horizon cap (Blake, 2026-08-28): Echo builds at most ONE FULL MONTH
(~31 days) past today — the monthly relearn rebuilds anything further out, so longer
spans are pure token waste. Fully OFFLINE. Asserts:

  * horizon_clamp: a days=60 request clamps to 31 with ONE honest log line
    (requested vs clamped); an in-horizon span passes through silently;
    AGENT_PLAN_HORIZON_DAYS=0 disables the cap entirely.
  * belt_filter (the insert belt): a row at today+40 is DROPPED (counted, ONE
    summary alert line for the batch — never per-row); an event-arc row (event_id
    set) at today+40 PASSES; the LASSO dated lanes (summit/book/welcome pillars on
    gym 'lasso') PASS; the exemption is NARROW (a non-lasso 'summit' row and a
    lasso 'doctrine' row beyond the horizon are still dropped).
  * the belt is wired into the REAL SupabaseCalendarStore.insert_rows (default ON).
  * build_client_month clamps a days=60 request: builds exactly the horizon's days,
    no staged row beyond today+31, and the clamp log fires.
  * approved existing far-future rows SURVIVE a re-plan untouched (the cap governs
    what gets BUILT, never a retroactive sweep).
  * backfill_denied_slots never backfills a denied day beyond the horizon.
  * real_month_run.plan_and_build (the LASSO lane) clamps its plan span.
  * lasso_remap.remap refuses a --month entirely beyond the horizon.
"""

import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr  # noqa: E402
from agent import client_sources as cs  # noqa: E402
from agent import plan_horizon as ph  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402

TODAY = date.today()


def _d(days_out):
    return (TODAY + timedelta(days=days_out)).isoformat()


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


# ---------------------------------------------------------------------------
# 1. horizon_clamp — the one span clamp
# ---------------------------------------------------------------------------

def test_clamp_60_to_31_with_one_log_line():
    logs = []
    days = ph.horizon_clamp(TODAY, 60, logger=logs.append)
    assert days == 31
    assert len(logs) == 1
    assert "requested 60" in logs[0]
    assert "clamped to 31" in logs[0]


def test_clamp_noop_inside_horizon_is_silent():
    logs = []
    assert ph.horizon_clamp(TODAY, 30, logger=logs.append) == 30
    assert logs == []


def test_clamp_past_start_gets_full_span():
    # A start in the past has MORE headroom, never less (existing fixed-date tests
    # and mid-month replans keep working).
    assert ph.horizon_clamp(TODAY - timedelta(days=10), 40, logger=lambda m: None) == 40


def test_clamp_start_beyond_horizon_is_zero():
    logs = []
    assert ph.horizon_clamp(TODAY + timedelta(days=45), 10, logger=logs.append) == 0
    assert len(logs) == 1


def test_horizon_zero_disables_clamp_and_belt(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_HORIZON_DAYS", "0")
    logs = []
    assert ph.horizon_clamp(TODAY, 60, logger=logs.append) == 60
    assert logs == []
    far = {"gym_id": "eng", "post_date": _d(90), "format": "feed",
           "pillar": "doctrine", "status": "pending"}
    kept, dropped = ph.belt_filter("eng", [far], alert=logs.append)
    assert kept == [far] and dropped == [] and logs == []


def test_horizon_days_default_and_override(monkeypatch):
    from agent import config
    assert config.plan_horizon_days() == 31
    monkeypatch.setenv("AGENT_PLAN_HORIZON_DAYS", "14")
    assert config.plan_horizon_days() == 14
    monkeypatch.setenv("AGENT_PLAN_HORIZON_DAYS", "junk")
    assert config.plan_horizon_days() == 31


# ---------------------------------------------------------------------------
# 2. belt_filter — the insert belt + the narrow dated exemptions
# ---------------------------------------------------------------------------

def _row(days_out, gym="eng", pillar="doctrine", event_id=None, fmt="feed"):
    r = {"gym_id": gym, "post_date": _d(days_out), "account": "instagram",
         "format": fmt, "caption": "Real caption. Sign up today.",
         "image_url": "https://cdn/x.jpg", "status": "pending", "pillar": pillar}
    if event_id:
        r["event_id"] = event_id
    return r


def test_belt_drops_beyond_horizon_one_summary_alert():
    alerts = []
    rows = [_row(10), _row(40), _row(41), _row(42)]
    kept, dropped = ph.belt_filter("eng", rows, alert=alerts.append)
    assert [r["post_date"] for r in kept] == [_d(10)]
    assert sorted(dropped) == [_d(40), _d(41), _d(42)]
    # ONE summary line for the whole batch, carrying the count — never per-row.
    assert len(alerts) == 1
    assert "3 eng row(s)" in alerts[0]


def test_belt_boundary_day_is_kept():
    # today+31 is the last allowed day; today+32 is the first dropped one.
    kept, dropped = ph.belt_filter("eng", [_row(31), _row(32)],
                                   alert=lambda m: None)
    assert [r["post_date"] for r in kept] == [_d(31)]
    assert dropped == [_d(32)]


def test_belt_exempts_event_arc_rows():
    alerts = []
    kept, dropped = ph.belt_filter(
        "eng", [_row(40, event_id="ev_blackfriday")], alert=alerts.append)
    assert len(kept) == 1 and dropped == [] and alerts == []


def test_belt_exempts_lasso_dated_lanes():
    # Summit sprint / book / welcome on LASSO are real DATED campaigns
    # (Summit is Nov 7-8) — never relearn churn.
    for pillar in ("summit", "book", "welcome"):
        kept, dropped = ph.belt_filter(
            "lasso", [_row(40, gym="lasso", pillar=pillar)], alert=lambda m: None)
        assert len(kept) == 1 and dropped == [], pillar


def test_belt_exemption_is_narrow():
    # A non-LASSO gym's 'summit' row is NOT exempt; neither is a LASSO row on an
    # everyday pillar. The exemption is a dated-lane allowance, not a loophole.
    kept, dropped = ph.belt_filter(
        "eng", [_row(40, gym="eng", pillar="summit")], alert=lambda m: None)
    assert kept == [] and len(dropped) == 1
    kept, dropped = ph.belt_filter(
        "lasso", [_row(40, gym="lasso", pillar="doctrine")], alert=lambda m: None)
    assert kept == [] and len(dropped) == 1


def test_belt_row_without_post_date_passes_through():
    row = {"gym_id": "eng", "format": "feed", "caption": "x", "status": "pending"}
    kept, dropped = ph.belt_filter("eng", [row], alert=lambda m: None)
    assert kept == [row] and dropped == []


# ---------------------------------------------------------------------------
# 3. the belt is wired into the REAL store's insert_rows (default ON)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self.status_code = 201
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self):
        self.posted = []

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.posted.append(json)
        return _FakeResp(list(json or []))


def _sb_store(http):
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="https://sb.test", service_key="k", http=http)


def test_store_insert_rows_applies_horizon_belt():
    http = _FakeHTTP()
    inserted = _sb_store(http).insert_rows("eng", [
        _row(10),                                # in-horizon: staged
        _row(40),                                # beyond: dropped by the belt
        _row(40, event_id="ev1"),                # beyond but DATED: staged
    ])
    dates = sorted(r["post_date"] for r in inserted)
    assert dates == sorted([_d(10), _d(40)])
    assert all(r.get("event_id") == "ev1" for r in inserted
               if r["post_date"] == _d(40))


def test_store_insert_rows_lets_lasso_sprint_row_through():
    http = _FakeHTTP()
    inserted = _sb_store(http).insert_rows("lasso", [
        _row(40, gym="lasso", pillar="summit"),   # Summit sprint: dated, staged
        _row(40, gym="lasso", pillar="platform"), # relearn churn: dropped
    ])
    assert [r["pillar"] for r in inserted] == ["summit"]


def test_store_insert_rows_belt_off_when_horizon_zero(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_HORIZON_DAYS", "0")
    http = _FakeHTTP()
    inserted = _sb_store(http).insert_rows("eng", [_row(10), _row(90)])
    assert len(inserted) == 2


# ---------------------------------------------------------------------------
# 4. build_client_month wiring (clamp + no far row staged + approved rows survive)
# ---------------------------------------------------------------------------

class _CalendarStore:
    """A fake store with a real row table so survival of existing rows is provable."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deleted_months = []

    def list_month(self, base_key, month):
        return [r for r in self.rows
                if r.get("gym_id") == base_key
                and str(r.get("post_date", ""))[:7] == month]

    def delete_month(self, base_key, month, *, preserve_dates=()):
        self.deleted_months.append(month)
        keep_dates = {str(d)[:10] for d in (preserve_dates or ())}
        wipeable = ("pending", "draft", "queued")
        kept, n = [], 0
        for r in self.rows:
            if (r.get("gym_id") == base_key
                    and str(r.get("post_date", ""))[:7] == month
                    and str(r.get("status", "")).lower() in wipeable
                    and str(r.get("post_date", ""))[:10] not in keep_dates):
                n += 1
                continue
            kept.append(r)
        self.rows = kept
        return n

    def insert_rows(self, base_key, rows):
        self.rows.extend(rows)
        return rows


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n):
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stock_sources(account_key):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source(account_key, "service", "Small group training",
                  "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s",
                  "client social intake")


@pytest.fixture
def _client_flags(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)


def test_build_client_month_clamps_60_to_horizon(tmp_path, _client_flags):
    _stock_sources("gritx_ig")
    lib = _lib(tmp_path, 40)
    store = _CalendarStore()
    logs = []
    out = cmr.build_client_month(
        _account(), "gritx", TODAY.isoformat(), days=60, voice=_voice(),
        library_path=lib, store=store, banned_words=(), logger=logs.append)
    assert out["ok"] is True
    # The clamp log fired once, honest about requested vs clamped.
    clamp_lines = [m for m in logs if "requested 60" in m and "clamped to 31" in m]
    assert len(clamp_lines) == 1
    # 31 buildable days, media permitting (40 photos > 31 days): no gap INSIDE the
    # month, and NOTHING staged past the horizon.
    assert out["days"] == 31
    end = (TODAY + timedelta(days=31)).isoformat()
    staged = [r for r in store.rows if r.get("status") == "pending"]
    assert staged and max(r["post_date"] for r in staged) <= end


def test_build_client_month_leaves_approved_far_future_rows(tmp_path, _client_flags):
    _stock_sources("gritx_ig")
    lib = _lib(tmp_path, 8)
    approved_far = {"id": "keepme", "gym_id": "gritx", "post_date": _d(45),
                    "account": "instagram", "format": "feed",
                    "caption": "Client approved this.", "status": "approved",
                    "image_url": "https://gritx.media/approved_far.jpg"}
    store = _CalendarStore(rows=[approved_far])
    out = cmr.build_client_month(
        _account(), "gritx", TODAY.isoformat(), days=60, voice=_voice(),
        library_path=lib, store=store, banned_words=(), logger=lambda m: None)
    assert out["ok"] is True
    # NEVER a retroactive sweep: the approved far-future row is byte-for-byte intact.
    assert approved_far in store.rows
    # ...and nothing was staged past the horizon. The delete span is a MONTH list,
    # so whenever today+31 crosses a month boundary the far row's month is legitimately
    # in it (2026-09-01: horizon 2026-10-01, approved row 2026-10-16). Asserting the
    # month was untouched therefore only held on dates where the horizon stayed inside
    # one month, and it went red on the 1st of the month rather than on a real
    # regression. The guarantee that matters is the one above plus this.
    assert all(r["post_date"] <= (TODAY + timedelta(days=31)).isoformat()
               for r in store.rows if r.get("status") == "pending")


def test_backfill_never_reaches_beyond_horizon(tmp_path, _client_flags, monkeypatch):
    monkeypatch.setenv("AGENT_DENY_BACKFILL", "true")
    _stock_sources("gritx_ig")
    lib = _lib(tmp_path, 8)
    denied_far = {"id": "d1", "gym_id": "gritx", "post_date": _d(40),
                  "account": "instagram", "format": "feed",
                  "caption": "Denied one.", "status": "denied",
                  "image_url": "https://gritx.media/photo_00.jpg"}
    store = _CalendarStore(rows=[denied_far])
    logs = []
    out = cmr.backfill_denied_slots(
        _account(), "gritx", TODAY.isoformat(), days=60, voice=_voice(),
        library_path=lib, store=store, banned_words=(), logger=logs.append)
    assert out["ok"] is True
    # The denied day sits beyond the clamped window: NOT backfilled (the monthly
    # relearn rebuild owns that day when it comes into the window).
    assert out["backfilled"] == 0
    assert out.get("days_needing", 0) == 0
    assert any("clamped to 31" in m for m in logs)
    assert store.rows == [denied_far]


def test_backfill_window_beyond_horizon_is_honest_noop(tmp_path, _client_flags,
                                                       monkeypatch):
    monkeypatch.setenv("AGENT_DENY_BACKFILL", "true")
    store = _CalendarStore()
    out = cmr.backfill_denied_slots(
        _account(), "gritx", _d(45), days=10, voice=_voice(),
        library_path=_lib(tmp_path, 2), store=store, banned_words=(),
        logger=lambda m: None)
    assert out["ok"] is True and out["backfilled"] == 0
    assert "horizon" in out.get("reason", "")


# ---------------------------------------------------------------------------
# 5. the LASSO lane: plan_and_build clamps; a far-month remap refuses
# ---------------------------------------------------------------------------

def test_plan_and_build_clamps_span(monkeypatch):
    monkeypatch.setenv("AGENT_REAL_MONTH_PLAN", "true")
    from agent import real_month_run as rmr
    seen = {}

    def _fake_plan_month(account_key, start_date, days, **kw):
        seen["days"] = days
        return []

    monkeypatch.setattr(rmr._rmp, "plan_month", _fake_plan_month)
    monkeypatch.setattr(rmr, "real_builders_map", lambda a: {})
    monkeypatch.setattr(rmr, "_real_story_builder", lambda a: None)
    monkeypatch.setattr(rmr, "sprint_builders",
                        lambda a, manifest=None: (None, None))
    out = rmr.plan_and_build("lasso_ig", TODAY.isoformat(), 60,
                             account="lasso_ig")
    assert out == []
    assert seen["days"] == 31


def test_plan_and_build_zero_span_returns_empty(monkeypatch):
    monkeypatch.setenv("AGENT_REAL_MONTH_PLAN", "true")
    from agent import real_month_run as rmr
    called = {"plan": False}
    monkeypatch.setattr(rmr._rmp, "plan_month",
                        lambda *a, **k: called.__setitem__("plan", True) or [])
    assert rmr.plan_and_build("lasso_ig", _d(45), 10, account="lasso_ig") == []
    assert called["plan"] is False  # never even planned


def test_lasso_remap_refuses_far_future_month(monkeypatch):
    monkeypatch.setenv("AGENT_REAL_MONTH_PLAN", "true")
    from agent import lasso_remap
    far = TODAY + timedelta(days=70)
    out = lasso_remap.remap("lasso", month=f"{far.year:04d}-{far.month:02d}",
                            write=False, logger=lambda m: None)
    assert out["ok"] is False
    assert "horizon" in out["reason"]
    assert out["upserted"] == 0 and out["deleted"] == 0
