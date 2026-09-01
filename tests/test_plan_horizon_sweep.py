"""tests/test_plan_horizon_sweep.py — audit item 2, 2026-08-31.

agent/plan_horizon.py clamps BUILDS and filters INSERTS, but nothing ever retired a
row that was already sitting past the horizon. Three days after the cap shipped the
live calendar still carried 68 non-exempt PENDING rows beyond today+31 (LASSO platform
43, doctrine 17, b2b 7, podcast 1, out to 2026-12-04), plus 25 exempt LASSO summit rows
and 5 event-anchored CrossFit Zanshin offer rows that were correct to keep.

These tests pin the retirement sweep against exactly that shape: what it deletes, and —
far more important — everything it must refuse to touch.

Fully offline: a fake store, an injected `now`, no network, no Supabase.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, plan_horizon  # noqa: E402
from agent.jobs import plan_horizon_sweep as sweep  # noqa: E402

NOW = date(2026, 8, 31)          # horizon end = 2026-10-01 at the default 31 days
INSIDE = "2026-09-20"
BEYOND = "2026-10-15"


def _row(rid, *, gym="lasso", status="pending", pillar="platform",
         post_date=BEYOND, event_id=None):
    return {"id": rid, "gym_id": gym, "status": status, "pillar": pillar,
            "post_date": post_date, "event_id": event_id}


class FakeStore:
    """Records what was read and deleted; deletes only rows it actually holds."""

    def __init__(self, rows_by_gym):
        self.rows_by_gym = rows_by_gym
        self.deleted = []          # list of (gym, [ids])
        self.reads = []

    def rows_in_range(self, gym, start_iso, end_iso):
        self.reads.append((gym, start_iso, end_iso))
        return [r for r in self.rows_by_gym.get(gym, [])
                if start_iso <= str(r.get("post_date"))[:10] <= end_iso]

    def delete_rows(self, gym, ids):
        ids = list(ids)
        self.deleted.append((gym, ids))
        return len(ids)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.delenv("AGENT_PLAN_HORIZON_SWEEP", raising=False)
    monkeypatch.delenv("AGENT_PLAN_HORIZON_DAYS", raising=False)


# ---- 1. the pure selector -----------------------------------------------------

def test_selector_retires_only_beyond_horizon_pending():
    rows = [_row("a", post_date=INSIDE), _row("b", post_date=BEYOND)]
    retire, exempt, protected = plan_horizon.select_retirable(rows, now=NOW)
    assert [r["id"] for r in retire] == ["b"]
    assert exempt == [] and protected == []


@pytest.mark.parametrize("status", ["approved", "publishing", "published",
                                    "denied", "killed", "failed"])
def test_selector_never_retires_a_human_owned_or_live_row(status):
    """THE rail: the sweep may not delete a post someone approved, a post that is
    going out, a post that went out, or a decision someone already made."""
    rows = [_row("x", status=status)]
    retire, _exempt, protected = plan_horizon.select_retirable(rows, now=NOW)
    assert retire == []
    assert [r["id"] for r in protected] == ["x"]


@pytest.mark.parametrize("pillar", ["summit", "book", "welcome"])
def test_selector_never_retires_a_lasso_dated_lane(pillar):
    """LASSO's 25 pending summit rows past the horizon are correct: the Summit is a
    real dated event (Nov 7-8), not relearn churn."""
    rows = [_row("s", gym="lasso", pillar=pillar)]
    retire, exempt, _protected = plan_horizon.select_retirable(rows, now=NOW)
    assert retire == []
    assert [r["id"] for r in exempt] == ["s"]


def test_selector_never_retires_an_event_anchored_row():
    """CrossFit Zanshin's 5 pending offer rows out to 2026-10-10 carry an event_id."""
    rows = [_row("e", gym="zanshinfitness630e22", pillar="offer",
                 post_date="2026-10-10", event_id="evt-1")]
    retire, exempt, _protected = plan_horizon.select_retirable(rows, now=NOW)
    assert retire == []
    assert [r["id"] for r in exempt] == ["e"]


def test_client_gym_summit_pillar_is_not_exempt():
    """The dated-lane exemption is LASSO's alone — it is not a pillar name any gym
    can ride past the horizon."""
    rows = [_row("c", gym="eng", pillar="summit")]
    retire, exempt, _p = plan_horizon.select_retirable(rows, now=NOW)
    assert [r["id"] for r in retire] == ["c"]
    assert exempt == []


def test_selector_ignores_rows_with_no_usable_post_date():
    rows = [_row("n", post_date=""), _row("bad", post_date="not-a-date")]
    retire, exempt, protected = plan_horizon.select_retirable(rows, now=NOW)
    assert (retire, exempt, protected) == ([], [], [])


def test_selector_is_inert_when_the_cap_is_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_HORIZON_DAYS", "0")
    rows = [_row("a", post_date="2027-01-01")]
    assert plan_horizon.select_retirable(rows, now=NOW) == ([], [], [])


def test_selector_does_not_mutate_its_input():
    rows = [_row("a"), _row("b", status="approved")]
    before = [dict(r) for r in rows]
    plan_horizon.select_retirable(rows, now=NOW)
    assert rows == before


# ---- 2. the job ---------------------------------------------------------------

def test_dry_run_deletes_nothing():
    store = FakeStore({"lasso": [_row("a"), _row("b")]})
    out = sweep.run(["lasso"], apply=False, store=store, now=NOW, alert=lambda m: None)
    assert out["ok"] is True
    assert out["retirable"] == 2
    assert out["retired"] == 0
    assert store.deleted == []


def test_apply_deletes_exactly_the_retirable_rows():
    store = FakeStore({"lasso": [
        _row("keep-inside", post_date=INSIDE),
        _row("retire-1"), _row("retire-2", pillar="doctrine"),
        _row("summit", pillar="summit"),
        _row("approved", status="approved"),
    ]})
    out = sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=lambda m: None)
    gym_result = out["gyms"][0]
    assert gym_result["retirable"] == 2
    assert gym_result["exempt"] == 1
    assert gym_result["protected"] == 1
    assert out["retired"] == 2
    deleted_ids = [i for _gym, ids in store.deleted for i in ids]
    assert sorted(deleted_ids) == ["retire-1", "retire-2"]


def test_read_window_starts_the_day_after_the_horizon():
    """The sweep must never even READ a row inside the horizon — a bug there would
    put live rows in front of the delete path."""
    store = FakeStore({"lasso": []})
    sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=lambda m: None)
    _gym, start, end = store.reads[0]
    assert start == "2026-10-02"          # horizon end 2026-10-01, exclusive
    assert end > start


def test_one_digest_alert_per_run_not_per_row():
    store = FakeStore({"lasso": [_row(str(i)) for i in range(43)]})
    seen = []
    sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=seen.append)
    assert len(seen) == 1
    assert "43" in seen[0]
    assert "lasso" in seen[0]


def test_no_alert_when_there_is_nothing_to_retire():
    store = FakeStore({"lasso": [_row("s", pillar="summit")]})
    seen = []
    sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=seen.append)
    assert seen == []


def test_flag_off_is_a_true_noop(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_HORIZON_SWEEP", "false")
    store = FakeStore({"lasso": [_row("a")]})
    out = sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=lambda m: None)
    assert out["ok"] is False
    assert store.reads == [] and store.deleted == []


def test_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("AGENT_PLAN_HORIZON_SWEEP", raising=False)
    assert config.plan_horizon_sweep_enabled() is True


def test_disabled_cap_disables_the_sweep(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_HORIZON_DAYS", "0")
    store = FakeStore({"lasso": [_row("a", post_date="2027-01-01")]})
    out = sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=lambda m: None)
    assert out["ok"] is False
    assert store.deleted == []


def test_one_gym_read_failure_never_stops_the_fleet():
    class Boom(FakeStore):
        def rows_in_range(self, gym, start_iso, end_iso):
            if gym == "broken":
                raise RuntimeError("postgrest down")
            return super().rows_in_range(gym, start_iso, end_iso)

    store = Boom({"lasso": [_row("a")], "broken": []})
    out = sweep.run(["broken", "lasso"], apply=True, store=store, now=NOW,
                    alert=lambda m: None)
    assert out["gyms"][0]["error"] == "RuntimeError"
    assert out["retired"] == 1


def test_delete_failure_is_reported_not_raised():
    class Boom(FakeStore):
        def delete_rows(self, gym, ids):
            raise RuntimeError("delete blew up")

    store = Boom({"lasso": [_row("a")]})
    out = sweep.run(["lasso"], apply=True, store=store, now=NOW, alert=lambda m: None)
    assert out["retired"] == 0
    assert any("delete failed" in d for d in out["gyms"][0]["detail"])
