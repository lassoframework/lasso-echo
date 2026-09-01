"""
publish_billing_gate (agent/publish_billing_gate.py), fully offline.

Blake 2026-08-25: a canceled gym must stop publishing. Polarity: block ONLY on positive
evidence of cancellation; every doubt (no customer id, no key, read error) stays OPEN so
a paying gym is never held by a flaky read. kv-cached; one deduped alert per flip.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import publish_billing_gate as pbg  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")


class _Reader:
    def __init__(self, active):
        self._active = active

    def available(self):
        return True

    def social_active(self, customer_id, product_id):
        if isinstance(self._active, Exception):
            raise self._active
        return self._active


def _gym(monkeypatch, base="gymx", customer="cus_1", product="prod_social"):
    from agent import db
    db.gym_upsert(base, stripe_customer_id=customer)
    from agent import portal_social as ps
    monkeypatch.setattr(ps, "social_product_id", lambda: product)


def test_flag_off_never_blocks(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "false")
    assert pbg.publishing_blocked("gymx") is False


def test_canceled_gym_blocks_and_alerts_once(monkeypatch):
    _gym(monkeypatch)
    alerts = []
    assert pbg.publishing_blocked("gymx", reader=_Reader(False), now=1000.0,
                                  alert=alerts.append) is True
    assert len(alerts) == 1 and "CANCELED" in alerts[0]
    # second call: still blocked (cached), no second alert
    assert pbg.publishing_blocked("gymx", reader=_Reader(False), now=1100.0,
                                  alert=alerts.append) is True
    assert len(alerts) == 1


def test_active_gym_never_blocks(monkeypatch):
    _gym(monkeypatch)
    assert pbg.publishing_blocked("gymx", reader=_Reader(True), now=1000.0) is False


def test_no_customer_id_fails_open(monkeypatch):
    from agent import db
    db.gym_upsert("gymy")                                   # no stripe_customer_id
    assert pbg.publishing_blocked("gymy", reader=_Reader(False), now=1000.0) is False


def test_stripe_error_fails_open(monkeypatch):
    _gym(monkeypatch)
    assert pbg.publishing_blocked(
        "gymx", reader=_Reader(RuntimeError("stripe down")), now=1000.0) is False


def test_reactivation_clears_the_alert_dedup(monkeypatch):
    _gym(monkeypatch)
    alerts = []
    assert pbg.publishing_blocked("gymx", reader=_Reader(False), now=1000.0,
                                  alert=alerts.append) is True
    # subscription restored; cache expired -> re-read -> open again + dedup cleared
    assert pbg.publishing_blocked("gymx", reader=_Reader(True), now=1000.0 + 7 * 3600,
                                  alert=alerts.append) is False
    # cancels AGAIN later -> a fresh alert fires
    assert pbg.publishing_blocked("gymx", reader=_Reader(False), now=1000.0 + 14 * 3600,
                                  alert=alerts.append) is True
    assert len(alerts) == 2


def test_lasso_is_never_gated(monkeypatch):
    assert pbg.publishing_blocked("lasso") is False


# ---- the gate must never look like protection it isn't ----------------------------
# THE INERT-GATE PROBLEM (audit 2026-08-31): the gate decides entirely on
# gyms.stripe_customer_id, and on the Echo side NOTHING WRITES THAT COLUMN. With
# AGENT_PUBLISH_BILLING_GATE armed in production, _live_state returns OK at its very
# first branch for every gym: no errors, no blocks, no possibility of a block. It reads
# as protection while being a total no-op, which is strictly worse than being off,
# because an off gate is honest. These pin that the gate SAYS SO.

def _cov(bases, with_ids=()):  # noqa: D103
    return pbg.coverage_report(
        bases=bases,
        gym_reader=lambda b: ({"stripe_customer_id": "cus_x"} if b in with_ids else {}))


def test_coverage_report_counts_who_the_gate_can_actually_block(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")
    rep = _cov(["eng", "gritx", "topfuel"], with_ids=("gritx",))
    assert rep["total"] == 3
    assert rep["with_customer"] == 1
    assert rep["without_customer"] == 2
    assert sorted(rep["uncovered"]) == ["eng", "topfuel"]


def test_lasso_is_not_counted_as_a_coverage_gap(monkeypatch):
    """publishing_blocked returns False for any lasso* base by design, so counting
    LASSO as uncovered would overstate the gap in the alert every single day."""
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")
    rep = _cov(["lasso", "lasso_ig", "eng"])
    assert rep["total"] == 1 and rep["uncovered"] == ["eng"]


def test_an_armed_gate_covering_nothing_says_so(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")
    msg = pbg.inertness_message(_cov(["eng", "gritx"]))
    assert "INERT" in msg and "EVERY client gym" in msg
    assert "eng" in msg and "gritx" in msg
    # It must be honest about NOT being a request to wire billing.
    assert "needs Blake" in msg


def test_partial_coverage_is_reported_too(monkeypatch):
    """A gate that protects 1 of 3 gyms is not protection; silence here would let a
    partly-wired gate read as fully wired."""
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")
    msg = pbg.inertness_message(_cov(["eng", "gritx", "topfuel"], with_ids=("gritx",)))
    assert "2 of 3 client gyms" in msg


def test_a_disarmed_gate_stays_quiet(monkeypatch):
    """An OFF gate is honest — nobody is being misled, so there is nothing to say."""
    monkeypatch.delenv("AGENT_PUBLISH_BILLING_GATE", raising=False)
    assert pbg.inertness_message(_cov(["eng", "gritx"])) == ""


def test_full_coverage_stays_quiet(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")
    assert pbg.inertness_message(_cov(["eng"], with_ids=("eng",))) == ""


def test_the_inertness_alert_fires_once_a_day_and_rearms_on_change(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")

    class _Db:
        def __init__(self):
            self.kv = {}

        def kv_get(self, k, default=""):
            return self.kv.get(k, default)

        def kv_set(self, k, v):
            self.kv[k] = v

    db = _Db()
    alerts = []
    rep = _cov(["eng", "gritx"])
    for _ in range(3):
        pbg.report_inertness(alert=alerts.append, db=db, today="2026-08-31", report=rep)
    assert len(alerts) == 1, f"one alert per day, got {alerts}"
    # a NEW day re-states it (the gap is still real)
    pbg.report_inertness(alert=alerts.append, db=db, today="2026-09-01", report=rep)
    assert len(alerts) == 2
    # coverage CHANGING re-states it the same day, rather than hiding behind the stamp
    rep2 = _cov(["eng", "gritx", "topfuel"])
    pbg.report_inertness(alert=alerts.append, db=db, today="2026-09-01", report=rep2)
    assert len(alerts) == 3


def test_the_self_report_never_writes_or_raises(monkeypatch):
    """HARD RAIL: this is a REPORT. It must never touch billing, and a crash here would
    be worse than the gap it describes."""
    monkeypatch.setenv("AGENT_PUBLISH_BILLING_GATE", "true")

    class _BoomDb:
        def kv_get(self, k, default=""):
            raise RuntimeError("kv down")

        def kv_set(self, k, v):
            raise RuntimeError("kv down")

    out = pbg.report_inertness(alert=lambda m: None, db=_BoomDb(), today="2026-08-31",
                               report=_cov(["eng"]))
    assert out["reported"] is True          # a kv fault must not silence a real gap
    import inspect
    src = inspect.getsource(pbg.coverage_report) + inspect.getsource(pbg.report_inertness)
    for forbidden in ("stripe.", "Subscription", "gym_upsert", "kv_set(f\"billgate_{"):
        assert forbidden not in src, f"the self-report must never touch billing ({forbidden})"
