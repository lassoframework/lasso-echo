"""tests/test_learning_loop_wiring.py — audit item 1, 2026-08-31.

THE FINDING. AGENT_LEARNING_LOOP was on, monthly_retro had written 17 rows, and
gym_playbook had never held a single row. The loop was not "waiting for evidence" — it
was structurally incapable of producing any:

  * lever stamping was wired into agent/real_month_planner.py (LASSO's lane) ONLY.
    Every client gym stages through agent/client_month_run.py, which never stamped.
  * so all 1681 live content_calendar rows carried hook_family / ask_type / time_slot /
    caption_len_band NULL;
  * agent/metrics_sync.py copies those columns from the joined calendar row, so all 272
    post_metrics rows were lever-less too;
  * monthly_retro.propose_changes compares hook_family, pillar and time_slot pairs, so
    it had nothing to compare, and no playbook could ever move.
  * and none of that was visible: a retro that cannot learn looked exactly like a retro
    whose honesty guards correctly held.

These tests pin all three legs of the fix: BOTH staging lanes stamp, the failure is
never silent, and a fleet with zero stamped evidence raises a LOUD alert that names the
real cause instead of blending into the guards.

Fully offline: no store, no network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import lever_stamp, playbook as pb_mod  # noqa: E402
from agent.jobs import monthly_retro as retro  # noqa: E402

LEVERS = ("hook_family", "ask_type", "caption_len_band", "time_slot")


def _rows(n=3):
    return [{"gym_id": "eng", "post_date": f"2026-09-0{i + 1}", "format": "feed",
             "caption": "Tired of starting over every January? Book your intro call."}
            for i in range(n)]


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    """load_playbook must never touch the network in these tests."""
    monkeypatch.setattr(pb_mod, "_default_store", lambda: _EmptyStore())


class _EmptyStore:
    def latest(self, gym_id):
        return None


# ---- 1. the shared stamper -----------------------------------------------------

def test_stamping_is_a_noop_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("AGENT_LEARNING_LOOP", raising=False)
    rows = _rows()
    before = [dict(r) for r in rows]
    lever_stamp.apply_learning_stamps("eng", rows)
    assert rows == before


def test_stamping_fills_every_lever_when_armed(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    rows = _rows()
    lever_stamp.apply_learning_stamps("eng", rows)
    for row in rows:
        for lever in LEVERS:
            assert row.get(lever), f"{lever} still unstamped"
    assert rows[0]["hook_family"] == "question"
    assert rows[0]["ask_type"] == "booking_link"


def test_stamping_never_touches_content_status_or_dates(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    rows = [{"gym_id": "eng", "post_date": "2026-09-01", "format": "feed",
             "caption": "Real caption.", "status": "pending",
             "image_url": "https://cdn/x.jpg"}]
    lever_stamp.apply_learning_stamps("eng", rows)
    assert rows[0]["caption"] == "Real caption."
    assert rows[0]["status"] == "pending"
    assert rows[0]["post_date"] == "2026-09-01"
    assert rows[0]["image_url"] == "https://cdn/x.jpg"


def test_a_stamping_failure_is_loud_not_silent(monkeypatch):
    """The original inline block swallowed every exception with a bare `except: pass`,
    which is why three weeks of unstamped rows produced not one log line."""
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")

    def boom(*_a, **_k):
        raise RuntimeError("playbook store down")

    monkeypatch.setattr(pb_mod, "load_playbook", boom)
    seen = []
    rows = _rows()
    lever_stamp.apply_learning_stamps("eng", rows, logger=seen.append)
    assert seen and "UNSTAMPED" in seen[0]


def test_an_already_stamped_row_is_never_overwritten(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    rows = _rows(1)
    rows[0]["hook_family"] = "story_open"
    lever_stamp.apply_learning_stamps("eng", rows)
    assert rows[0]["hook_family"] == "story_open"


# ---- 2. BOTH staging lanes call it ---------------------------------------------

def test_both_staging_lanes_call_the_shared_stamper():
    """The regression guard for the actual defect: the client lane (which stages
    almost every row Echo owns) must not drift back to having no stamping."""
    import inspect

    from agent import client_month_run, real_month_planner
    for mod in (client_month_run, real_month_planner):
        src = inspect.getsource(mod)
        assert "apply_learning_stamps" in src, (
            f"{mod.__name__} stages calendar rows without stamping levers — the retro "
            "cannot learn from anything it writes")
    # the client lane stamps BOTH of its insert paths (month build + denied backfill)
    assert inspect.getsource(client_month_run).count("apply_learning_stamps") >= 2


# ---- 3. lever coverage + the loud gap alert ------------------------------------

def _scored(n, *, stamped=True, external=False, is_ad=False):
    out = []
    for i in range(n):
        row = {"key": f"ig:p{i}:d7", "score": 1.0, "external": external,
               "is_ad": is_ad, "format": "feed"}
        if stamped:
            row.update({"hook_family": "question", "ask_type": "booking_link",
                        "time_slot": "morning", "caption_len_band": "mid"})
        out.append(row)
    return out


def test_lever_coverage_counts_only_learnable_posts():
    scored = (_scored(3) + _scored(2, external=True) + _scored(1, is_ad=True))
    assert retro.lever_coverage(scored) == {"learnable": 3, "stamped": 3}


def test_lever_coverage_sees_the_unstamped_month():
    assert retro.lever_coverage(_scored(5, stamped=False)) == {
        "learnable": 5, "stamped": 0}


def _retro_row(gym, learnable, stamped, moved=False):
    return {"gym_id": gym,
            "findings": {"lever_coverage": {"learnable": learnable,
                                            "stamped": stamped}},
            "playbook_diff": {"pillar_weights": {"x": 1.2}} if moved else {}}


def test_zero_stamped_evidence_raises_a_loud_alert():
    """The audit's exact live state: rows written, nothing consumable."""
    seen = []
    results = [_retro_row("eng", 20, 0), _retro_row("lasso", 30, 0)]
    rep = retro.alert_unconsumed(results, "2026-08", alert=seen.append,
                                 logger=lambda m: None)
    assert rep == {"rows": 2, "learnable": 50, "stamped": 0,
                   "playbooks_moved": 0, "gyms_with_evidence": 0}
    assert len(seen) == 1
    assert "backfill_levers" in seen[0]
    assert "hook_family" in seen[0]


def test_guards_holding_on_real_evidence_does_not_alert():
    """A thin-but-stamped month is the design working, not a defect. Alerting on it
    would train everyone to ignore the alert that matters."""
    seen = []
    logged = []
    retro.alert_unconsumed([_retro_row("eng", 20, 20)], "2026-08",
                           alert=seen.append, logger=logged.append)
    assert seen == []
    assert logged and "working as designed" in logged[0].lower()


def test_a_moved_playbook_is_quiet():
    seen = []
    logged = []
    retro.alert_unconsumed([_retro_row("eng", 20, 20, moved=True)], "2026-08",
                           alert=seen.append, logger=logged.append)
    assert seen == [] and logged == []


def test_no_retro_rows_means_no_alert():
    seen = []
    assert retro.alert_unconsumed([], "2026-08", alert=seen.append)["rows"] == 0
    assert seen == []


def test_alert_failure_never_breaks_the_retro():
    def boom(_msg):
        raise RuntimeError("slack down")

    retro.alert_unconsumed([_retro_row("eng", 5, 0)], "2026-08", alert=boom,
                           logger=lambda m: None)
