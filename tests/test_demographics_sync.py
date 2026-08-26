"""Per-gym engaged-audience demographics (agent/jobs/demographics_sync.py,
flag AGENT_AUDIENCE_DEMOGRAPHICS). Fully offline via injected fakes.

Required by spec: weekly kv gate; storage shape (gym_id, captured_at, kind,
breakdown jsonb, kind in followers|engaged); digest line only when rows
exist; flag OFF -> no-op.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs import demographics_sync as ds  # noqa: E402
from agent.jobs import monthly_retro  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

DEMO_RESPONSE = {
    "success": True, "accountId": "ig1", "platform": "instagram",
    "metric": "engaged_audience_demographics", "timeframe": "this_month",
    "demographics": {
        "age": {"25-34": 40, "35-44": 55, "45-54": 20},
        "gender": {"F": 70, "M": 42, "U": 3},
        "city": {"Valparaiso, Indiana": 60},
        "country": {"US": 110},
    },
}


class FakeZernio:
    def __init__(self, profiles=None, broken=False, no_ig=False):
        self.profiles = profiles or {"topfuel": "prof_tf"}
        self.broken = broken
        self.no_ig = no_ig
        self.demo_calls = []

    def find_profile_id(self, name):
        return self.profiles.get(name)

    def list_accounts(self, profile_id):
        if self.broken:
            raise RuntimeError("zernio down")
        if self.no_ig:
            return {"accounts": [{"_id": "fb1", "platform": "facebook"}]}
        return {"accounts": [{"_id": "ig1", "platform": "instagram"},
                             {"_id": "fb1", "platform": "facebook"}]}

    def instagram_demographics(self, account_id, metric="follower_demographics",
                               timeframe="this_month", breakdown=None):
        self.demo_calls.append((account_id, metric))
        resp = dict(DEMO_RESPONSE)
        resp["metric"] = metric
        return resp


class FakeStore:
    def __init__(self):
        self.rows = []

    def upsert_rows(self, rows):
        self.rows.extend(rows)
        return len(rows)

    def latest(self, gym_id, kind="engaged"):
        matches = [r for r in self.rows
                   if r["gym_id"] == gym_id and r["kind"] == kind]
        return matches[-1] if matches else None


class FakeKv:
    def __init__(self):
        self.store = {}

    def get(self, key, default=""):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = str(value)


def _run(monkeypatch, zernio=None, store=None, kv=None, now=NOW, gyms=("topfuel",)):
    monkeypatch.setenv("AGENT_AUDIENCE_DEMOGRAPHICS", "true")
    kv = kv or FakeKv()
    store = store or FakeStore()
    zernio = zernio or FakeZernio()
    result = ds.run(gyms=list(gyms), zernio=zernio, store=store, now=now,
                    kv_get=kv.get, kv_set=kv.set)
    return result, store, kv, zernio


# ---- flag gate ----------------------------------------------------------------------

def test_flag_off_is_a_noop(monkeypatch):
    monkeypatch.delenv("AGENT_AUDIENCE_DEMOGRAPHICS", raising=False)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("flag OFF must not touch the client")

    out = ds.run(gyms=["topfuel"], zernio=Boom(), store=Boom(),
                 kv_get=lambda k, d="": "", kv_set=lambda k, v: None)
    assert out["ok"] is False
    assert "OFF" in out["reason"]


# ---- storage shape ------------------------------------------------------------------

def test_both_kinds_stored_with_the_migration_shape(monkeypatch):
    result, store, kv, zernio = _run(monkeypatch)
    assert result["ok"] is True
    assert sorted(r["kind"] for r in store.rows) == ["engaged", "followers"]
    # both Zernio metrics were requested against the IG account
    assert set(zernio.demo_calls) == {
        ("ig1", "follower_demographics"),
        ("ig1", "engaged_audience_demographics")}
    for row in store.rows:
        assert set(row) == {"gym_id", "captured_at", "kind", "breakdown"}
        assert row["gym_id"] == "topfuel"
        assert row["captured_at"] == "2026-08-26"
        assert row["breakdown"]["gender"]["F"] == 70  # verbatim, never reshaped


def test_no_demographics_returned_stores_nothing(monkeypatch):
    class EmptyZernio(FakeZernio):
        def instagram_demographics(self, account_id, **kw):
            return {"success": False}

    result, store, _, _ = _run(monkeypatch, zernio=EmptyZernio())
    assert store.rows == []
    assert result["gyms"][0]["ok"] is False
    assert "no demographics" in result["gyms"][0]["reason"]


def test_gym_failures_reported_not_guessed_and_never_block_the_rest(monkeypatch):
    z = FakeZernio(profiles={"gritx": "prof_gx", "topfuel": "prof_tf"})
    orig = z.list_accounts

    def flaky(profile_id):
        if profile_id == "prof_gx":
            raise RuntimeError("boom")
        return orig(profile_id)

    z.list_accounts = flaky
    result, store, _, _ = _run(monkeypatch, zernio=z, gyms=("gritx", "topfuel"))
    by_gym = {r["gym_id"]: r for r in result["gyms"]}
    assert by_gym["gritx"]["ok"] is False
    assert by_gym["topfuel"]["ok"] is True
    assert {r["gym_id"] for r in store.rows} == {"topfuel"}


# ---- weekly kv gate -----------------------------------------------------------------

def test_weekly_kv_gate(monkeypatch):
    kv = FakeKv()
    store = FakeStore()
    r1, _, _, _ = _run(monkeypatch, store=store, kv=kv)
    assert r1["gyms"][0]["ok"] is True
    assert len(store.rows) == 2

    # next day: gated (synced 1 day ago)
    r2, _, _, _ = _run(monkeypatch, store=store, kv=kv,
                       now=NOW + timedelta(days=1))
    assert r2["gyms"][0].get("skipped")
    assert len(store.rows) == 2

    # 8 days later: due again
    r3, _, _, _ = _run(monkeypatch, store=store, kv=kv,
                       now=NOW + timedelta(days=8))
    assert r3["gyms"][0]["ok"] is True
    assert len(store.rows) == 4


def test_failed_week_is_not_stamped_so_it_retries(monkeypatch):
    kv = FakeKv()
    r1, _, _, _ = _run(monkeypatch, zernio=FakeZernio(no_ig=True), kv=kv)
    assert r1["gyms"][0]["ok"] is False
    assert kv.store == {}  # no stamp -> tomorrow retries


# ---- digest line --------------------------------------------------------------------

def test_digest_line_from_stored_row_no_dashes():
    row = {"gym_id": "topfuel", "captured_at": "2026-08-26", "kind": "engaged",
           "breakdown": DEMO_RESPONSE["demographics"]}
    line = ds.digest_line(row)
    assert line == "Engaged audience: 61% women, peak 35 to 44"
    assert "-" not in line  # client-facing copy law


def test_digest_line_none_when_no_row():
    assert ds.digest_line(None) is None
    assert ds.digest_line({"breakdown": {}}) is None


def test_retro_digest_carries_demographics_only_when_rows_exist(monkeypatch):
    """monthly_retro cites the stored engaged row when the flag is ON and a
    row exists; no row (or flag OFF) -> no line."""
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    monkeypatch.setenv("AGENT_AUDIENCE_DEMOGRAPHICS", "true")

    class RetroStore:
        def __init__(self, engaged_row):
            self.engaged_row = engaged_row
            self.retros = []
            from agent import playbook as pb

            class _PB:
                def latest(self, gym_id):
                    return None

                def insert_version(self, row):
                    return row

            self.playbook_store = _PB()
            del pb

        def month_metrics(self, gym_id, month):
            return []

        def taint_signals(self, gym_id, month):
            return {}

        def insert_retro(self, row):
            self.retros.append(row)
            return row

        def latest_demographics(self, gym_id, kind):
            return self.engaged_row

    engaged_row = {"gym_id": "gym1", "captured_at": "2026-08-26",
                   "kind": "engaged",
                   "breakdown": DEMO_RESPONSE["demographics"]}
    with_row = monthly_retro.run(month="2026-08", gyms=["gym1"],
                                 store=RetroStore(engaged_row), now=NOW,
                                 notifier=lambda g, t: None)
    digest = with_row["gyms"][0]["digest"]
    assert "Engaged audience: 61% women, peak 35 to 44" in digest

    without_row = monthly_retro.run(month="2026-08", gyms=["gym1"],
                                    store=RetroStore(None), now=NOW,
                                    notifier=lambda g, t: None)
    assert "Engaged audience" not in without_row["gyms"][0]["digest"]

    # flag OFF -> no line even when a row exists (and no store constructed)
    monkeypatch.setenv("AGENT_AUDIENCE_DEMOGRAPHICS", "false")
    dark = monthly_retro.run(month="2026-08", gyms=["gym1"],
                             store=RetroStore(engaged_row), now=NOW,
                             notifier=lambda g, t: None)
    assert "Engaged audience" not in dark["gyms"][0]["digest"]
