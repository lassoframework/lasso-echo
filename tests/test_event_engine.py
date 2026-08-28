"""
event_engine: the ONE window-timed engine, gym-parameterized. Proves LASSO Summit is
just another gym_event on the same engine (no second scheduler), and that the engine
plans a client gym's event identically. Offline (injected today).

Covers EVENT_CAMPAIGNS_BUILD.md DoD:
  * Summit runs on the same engine (summit_as_event + plan_event_arc + summit_still_plans)
  * a client event and Summit go through the identical code path (plan_event_arc)
  * the Summit arc grounds only in verified summit facts (no fabrication)
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import event_engine as ee
from agent import gym_event as ge


def test_summit_is_expressible_as_gym_event():
    ev = ee.summit_as_event()
    assert ev.gym_id == "lasso"
    assert ev.starts_on == "2026-11-07" and ev.ends_on == "2026-11-08"
    # the verified summit facts ride in the offer text (never re-invented).
    assert "Virgin Hotel Nashville" in ev.offer_text
    assert "100 seats" in ev.offer_text


def test_one_engine_plans_summit_arc():
    ev = ee.summit_as_event()
    rows = ee.plan_event_arc(ev, today=date(2026, 10, 1))
    assert rows
    kinds = {r["arc_kind"] for r in rows}
    # a full run-up arc a month out.
    assert ge.ANNOUNCE in kinds and ge.RECAP in kinds


def test_summit_still_plans_regression_guard():
    # the refactor invariant: the engine plans a non-empty Summit arc.
    assert ee.summit_still_plans() is True


def test_summit_arc_has_no_fabricated_facts():
    ev = ee.summit_as_event()
    rows = ee.plan_event_arc(ev, today=date(2026, 10, 1))
    for r in rows:
        bad = ge.fact_ok(r["caption"], ev)
        assert not bad, (r["arc_kind"], bad, r["caption"])


def test_client_event_and_summit_same_code_path():
    # a client Bring-a-Friend Week and LASSO Summit both go through plan_event_arc and
    # yield the same arc-row SHAPE (event_id, arc_kind, pending status) — one engine.
    client = ge.GymEvent.from_row(dict(
        id="evt_client", gym_id="pete", name="Bring a Friend Week",
        type="bring_a_friend", starts_on="2026-09-22", ends_on="2026-09-28",
        tz="America/New_York", offer_text="Your partner trains free",
        link="", brief="", media_ids=("m1",), status="scheduled"))
    crows = ee.plan_event_arc(client, today=date(2026, 9, 1))
    srows = ee.plan_event_arc(ee.summit_as_event(), today=date(2026, 10, 1))
    for rows, gym in ((crows, "pete"), (srows, "lasso")):
        assert rows
        for r in rows:
            assert r["gym_id"] == gym
            assert r["status"] == "pending"
            assert r["event_id"]
            assert set(("arc_kind", "post_date", "caption", "pillar")) <= set(r)
