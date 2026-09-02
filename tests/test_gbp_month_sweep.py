"""
gbp_month_sweep tests (agent/jobs/gbp_month_sweep.py), fully offline.

The gap this job closes (2026-09-02): plan_gbp_month was reachable ONLY from the manual
one-gym gbp_dogfood entrypoint, so ENG had zero published Google Business posts in its
entire history on a perfectly healthy connection, and nine other connected gyms were in
the same state. Covers: only 'connected' gyms are swept, the real city is read from the
gym's own Google listing address (and a gym whose address will not parse is SKIPPED with
an alert rather than planned against a guess), idempotency and block reasons are reported
honestly, one gym's failure never sinks the sweep, and the flag gate.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.jobs import gbp_month_sweep as sweep_mod  # noqa: E402


class _FakeStore:
    def __init__(self, conns, ok=True):
        self._conns = conns
        self._ok = ok

    def available(self):
        return self._ok

    def all_connections(self):
        return self._conns


def _conn(key, status="connected"):
    return {"portal_gym_key": key, "status": status,
            "gbp_location_id": f"loc_{key}", "location_name": key}


# ---- city extraction (never a guess) ---------------------------------------------------

def test_city_from_address_reads_real_google_listing_shapes():
    # every one of these is a REAL production locationAddress shape
    assert sweep_mod.city_from_address(
        "326 Southwest 2nd Terrace, Cape Coral, Florida") == "Cape Coral"
    assert sweep_mod.city_from_address(
        "64 Hobbs Street #3, Conway, New Hampshire") == "Conway"
    assert sweep_mod.city_from_address("1 Main St, Austin, TX 78701") == "Austin"


def test_city_from_address_refuses_to_guess():
    for bad in ("", None, "just one part", "two parts, only", "   ,  , "):
        assert sweep_mod.city_from_address(bad) == "", bad


# ---- sweep selection -------------------------------------------------------------------

def test_only_connected_gyms_are_swept():
    store = _FakeStore([_conn("eng"), _conn("pierce", "needs_reconnect"),
                        _conn("ghost", "none")])
    planned = []

    def _runner(base, city=None):
        planned.append((base, city))
        return {"ok": True, "planned": 12}

    out = sweep_mod.sweep(store=store, runner=_runner,
                          address_fn=lambda b: "1 A St, Cape Coral, Florida",
                          alert=lambda *_a, **_k: None)
    assert [p[0] for p in planned] == ["eng"], "needs_reconnect / none are never planned"
    assert out["gyms_planned"] == 1 and out["planned"] == 12


def test_a_gym_with_an_unparseable_address_is_skipped_and_alerted_not_guessed():
    store = _FakeStore([_conn("eng")])
    planned, alerts = [], []

    def _runner(base, city=None):
        planned.append(base)
        return {"ok": True, "planned": 12}

    out = sweep_mod.sweep(store=store, runner=_runner,
                          address_fn=lambda b: "no commas here",
                          alert=lambda m: alerts.append(m))
    assert planned == [], "never planned against a fabricated city"
    assert out["no_city"] == 1
    assert len(alerts) == 1 and "did not yield a city" in alerts[0]


def test_city_is_passed_through_from_the_real_listing():
    store = _FakeStore([_conn("eng")])
    got = {}

    def _runner(base, city=None):
        got["city"] = city
        return {"ok": True, "planned": 8}

    sweep_mod.sweep(store=store, runner=_runner,
                    address_fn=lambda b: "326 SW 2nd Ter, Cape Coral, Florida",
                    alert=lambda *_a, **_k: None)
    assert got["city"] == "Cape Coral"


# ---- honest reporting ------------------------------------------------------------------

def test_already_planned_gym_is_reported_as_skipped_not_planned():
    store = _FakeStore([_conn("eng")])
    out = sweep_mod.sweep(
        store=store, runner=lambda b, city=None: {"ok": True, "planned": 0,
                                                  "skipped_existing": True},
        address_fn=lambda b: "1 A St, Cape Coral, Florida",
        alert=lambda *_a, **_k: None)
    assert out["skipped_existing"] == 1 and out["gyms_planned"] == 0


def test_a_blocked_gym_is_reported_and_alerted_never_silently_dropped():
    store = _FakeStore([_conn("eng")])
    alerts = []
    out = sweep_mod.sweep(
        store=store,
        runner=lambda b, city=None: {"ok": False, "reason": "voice doc missing",
                                     "planned": 0},
        address_fn=lambda b: "1 A St, Cape Coral, Florida",
        alert=lambda m: alerts.append(m))
    assert out["blocked"] == 1
    assert any("voice doc missing" in a for a in alerts)


def test_one_gym_exploding_never_sinks_the_sweep():
    store = _FakeStore([_conn("bad"), _conn("good")])
    calls = []

    def _runner(base, city=None):
        calls.append(base)
        if base == "bad":
            raise RuntimeError("boom")
        return {"ok": True, "planned": 12}

    out = sweep_mod.sweep(store=store, runner=_runner,
                          address_fn=lambda b: "1 A St, Cape Coral, Florida",
                          alert=lambda *_a, **_k: None)
    assert calls == ["bad", "good"]
    assert out["errors"] == 1 and out["gyms_planned"] == 1


def test_limit_and_gym_filter_scope_a_smoke_test():
    store = _FakeStore([_conn("a"), _conn("b"), _conn("c")])
    planned = []

    def _runner(base, city=None):
        planned.append(base)
        return {"ok": True, "planned": 1}

    sweep_mod.sweep(store=store, runner=_runner, gyms={"b"},
                    address_fn=lambda b: "1 A St, Cape Coral, Florida",
                    alert=lambda *_a, **_k: None)
    assert planned == ["b"]

    planned.clear()
    sweep_mod.sweep(store=store, runner=_runner, limit=2,
                    address_fn=lambda b: "1 A St, Cape Coral, Florida",
                    alert=lambda *_a, **_k: None)
    assert planned == ["a", "b"]


def test_unavailable_store_is_a_clean_noop():
    out = sweep_mod.sweep(store=_FakeStore([], ok=False))
    assert out["ok"] is False and out["planned"] == 0


# ---- flag gate -------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_GBP_MONTH_SWEEP", raising=False)
    assert config.gbp_month_sweep_enabled() is False


def test_run_is_a_noop_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("AGENT_GBP_MONTH_SWEEP", raising=False)
    out = sweep_mod.run()
    assert out["ok"] is False and out["reason"] == "flag off"
