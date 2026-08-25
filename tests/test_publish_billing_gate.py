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
