"""restamp_ask_type — closing the ask_type corpus discontinuity.

Blake's ruling (2026-09-06): the weekly cross-gym brain and the learning loop
should rank "what's working" by REAL engagement outcomes (learning_score.score:
likes/comments/shares/saves/clicks/follows), never by the ask_type label. That
already holds structurally -- no engagement number is ever wrong. What a stale
label corrupts is which FORM bucket a post's real engagement counts toward.

These tests are offline and deterministic: a fake store, no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.jobs import backfill_levers as bl


REVERB_BOOKING = "Book a FREE NO SWEAT intro this week."  # old regex: 'none'; new: 'booking_link'


class _FakeStore:
    def __init__(self, rows_by_gym):
        self._rows = rows_by_gym
        self.patched = []

    def rows_with_ask_type(self, gym_id, limit=5000):
        return list(self._rows.get(gym_id, []))

    def patch_levers(self, gym_id, row_id, levers):
        self.patched.append((gym_id, row_id, dict(levers)))
        return True


def _rows():
    return {
        "reverb": [
            {"id": "r1", "gym_id": "reverb", "caption": REVERB_BOOKING, "ask_type": "none"},
            {"id": "r2", "gym_id": "reverb", "caption": "Just a nice photo, no ask here.",
             "ask_type": "none"},
            {"id": "r3", "gym_id": "reverb", "caption": "DM us to get started today",
             "ask_type": "dm"},
        ],
    }


def test_flag_off_is_a_no_op(monkeypatch):
    monkeypatch.delenv("AGENT_LEARNING_LOOP", raising=False)
    store = _FakeStore(_rows())
    out = bl.restamp_ask_type(gyms=["reverb"], store=store, dry_run=False)
    assert out["ok"] is False
    assert store.patched == []


def test_dry_run_reports_the_change_but_writes_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    store = _FakeStore(_rows())
    out = bl.restamp_ask_type(gyms=["reverb"], store=store, dry_run=True)
    assert out["dry_run"] is True
    gym = out["gyms"][0]
    assert gym["changed"] == 1, gym          # only r1 (none -> booking_link)
    assert gym["unchanged"] == 2, gym        # r2 stays none, r3 stays dm
    ex = gym["examples"][0]
    assert ex["id"] == "r1"
    assert ex["old"] == "none"
    assert ex["new"] == "booking_link"
    assert store.patched == [], "dry_run must never write"


def test_apply_writes_only_the_rows_that_actually_changed(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    store = _FakeStore(_rows())
    out = bl.restamp_ask_type(gyms=["reverb"], store=store, dry_run=False)
    assert out["gyms"][0]["changed"] == 1
    assert store.patched == [("reverb", "r1", {"ask_type": "booking_link"})]


def test_a_row_whose_label_is_already_correct_is_never_touched(monkeypatch):
    """r2 and r3 above are already correctly classified under the CURRENT
    lever_stamp.ask_type; re-running must be a no-op for them, every time."""
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    store = _FakeStore(_rows())
    bl.restamp_ask_type(gyms=["reverb"], store=store, dry_run=False)
    touched_ids = {row_id for _g, row_id, _l in store.patched}
    assert "r2" not in touched_ids
    assert "r3" not in touched_ids


def test_only_ask_type_is_ever_written_never_other_levers(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    store = _FakeStore(_rows())
    bl.restamp_ask_type(gyms=["reverb"], store=store, dry_run=False)
    for _g, _id, levers in store.patched:
        assert set(levers) == {"ask_type"}, levers


def test_a_read_failure_for_one_gym_does_not_abort_the_others(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")

    class _FlakyStore(_FakeStore):
        def rows_with_ask_type(self, gym_id, limit=5000):
            if gym_id == "broken":
                raise RuntimeError("supabase 503")
            return super().rows_with_ask_type(gym_id, limit)

    store = _FlakyStore(_rows())
    out = bl.restamp_ask_type(gyms=["broken", "reverb"], store=store, dry_run=True)
    by_gym = {g["gym_id"]: g for g in out["gyms"]}
    assert by_gym["broken"]["ok"] is False
    assert by_gym["reverb"]["ok"] is True
    assert by_gym["reverb"]["changed"] == 1
