"""
tests/test_grade_self_fix.py — AGENT_GRADE_SELF_FIX: forward-book self-remediation
plus the quiet alert policy (Blake's 2026-08-27 ruling: fix it on its own without
spamming Slack).

All tests are deterministic and offline: fake store, injected alert_fn, injected
caption_regen / gap_filler, per-test sqlite kv (conftest sets AGENT_DB_PATH).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from agent.jobs import grade_fix, grade_sweep


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class _FakeStore:
    """Offline calendar store: rows_in_range + insert_grade + patch_pending_plan
    with the same wipeable-only guard the real store enforces server-side."""

    def __init__(self, rows):
        self.rows = rows
        self.grades = []
        self.patched_ids = []

    def rows_in_range(self, gym_id, start_iso, end_iso):
        return [r for r in self.rows
                if r.get("gym_id") == gym_id
                and start_iso <= r.get("post_date", "") <= end_iso
                and str(r.get("status") or "").lower() != "denied"]

    def insert_grade(self, record):
        self.grades.append(record)

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None):
        for r in self.rows:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                if str(r.get("status") or "").lower() not in ("pending", "draft", "queued"):
                    return None                    # server-side wipeable guard
                if caption is not None:
                    r["caption"] = caption
                if pillar is not None:
                    r["pillar"] = pillar
                r["status"] = "pending"
                self.patched_ids.append(row_id)
                return dict(r)
        return None


def _a_caption(i):
    """A clean A-grade caption: short hook line, 150+ chars, booking ask, no dashes."""
    hook = f"Feeling stuck at {i + 20}? Real change starts small."
    body = (
        "\nBusy parents and working professionals thrive in our 30-minute "
        "format. No experience needed and every class is beginner friendly. "
        f"Get started today and book your free intro class. Post {i}."
    )
    return hook + body


def _row(i, post_date, caption, *, status="pending", pillar="community",
         gym_id="gritx", account="instagram", fmt="feed"):
    return {
        "id": f"row_{i}",
        "gym_id": gym_id,
        "post_date": post_date,
        "caption": caption,
        "pillar": pillar,
        "category": pillar,
        "format": fmt,
        "account": account,
        "status": status,
        "image_url": f"https://cdn.example.com/photo_{i}.jpg",
        "media_kind": "photo",
    }


def _forward_rows_with_dups(gym_id="gritx"):
    """A forward book that grades below A purely from cross-date duplicate
    captions: days 1..10, days 6..10 repeat day 1..5 captions."""
    rows = []
    pillars = ["community", "education", "coach", "invite", "story"]
    for i in range(10):
        cap = _a_caption(i % 5)                    # 5 captions repeated twice
        rows.append(_row(i, f"2026-09-{(i + 1):02d}", cap,
                         pillar=pillars[i % 5], gym_id=gym_id))
    return rows


TODAY = "2026-08-31"


def _sweep(store, alerts, gyms=("gritx",)):
    return grade_sweep.run(gyms=list(gyms), store=store, now=TODAY,
                           alert_fn=alerts.append)


@pytest.fixture()
def _grade_on(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")


@pytest.fixture()
def _self_fix_on(monkeypatch, _grade_on):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")


# ---------------------------------------------------------------------------
# Flag OFF: byte-for-byte today (legacy per-gym-per-window alerts, no patches)
# ---------------------------------------------------------------------------

def test_flag_off_sweep_and_alerts_unchanged(_grade_on):
    rows = _forward_rows_with_dups()
    # Trailing rows that also fail (dup captions in the past window)
    past = [_row(100 + i, f"2026-08-{(i + 1):02d}", _a_caption(0))
            for i in range(5)]
    for r in past:
        r["status"] = "published"
    store = _FakeStore(past + rows)
    alerts = []
    result = _sweep(store, alerts)

    assert result["ok"] is True
    # Legacy behavior: one alert per failing window, the old text, every sweep
    assert len(alerts) == 2, alerts
    assert all(a.startswith("calendar grade sweep:") for a in alerts)
    assert any("trailing_30" in a for a in alerts)
    assert any("forward_book" in a for a in alerts)
    # No remediation, no patches, no new result keys
    assert store.patched_ids == []
    assert "self_fixed" not in result and "held" not in result
    assert "self_fix" not in result["gyms"]["gritx"]
    # Grades still stored for both windows
    assert {g["window"] for g in store.grades} == {"trailing_30", "forward_book"}


def test_grade_fix_flag_off_is_noop():
    store = _FakeStore(_forward_rows_with_dups())
    out = grade_fix.remediate_forward_book(
        "gritx", store.rows, store, profile="GYM", defects=[],
        today_iso=TODAY)
    assert out["ok"] is False
    assert "off" in out["reason"]
    assert store.patched_ids == []


# ---------------------------------------------------------------------------
# trailing_30 never alerts when self-fix is armed
# ---------------------------------------------------------------------------

def test_trailing_never_alerts_when_self_fix_on(_self_fix_on):
    past = [_row(100 + i, f"2026-08-{(i + 1):02d}", _a_caption(0),
                 status="published") for i in range(5)]
    store = _FakeStore(past)
    alerts = []
    result = _sweep(store, alerts)
    assert result["ok"] is True
    # Trailing graded + stored, but zero alerts (history is not fixable)
    assert [g["window"] for g in store.grades] == ["trailing_30"]
    assert alerts == []


# ---------------------------------------------------------------------------
# Duplicate-caption remediation: pending rows only, approved never touched
# ---------------------------------------------------------------------------

def test_dup_remediation_patches_pending_only_keeps_earliest(monkeypatch):
    rows = [
        _row(1, "2026-09-01", _a_caption(1)),               # keeper (earliest)
        _row(2, "2026-09-05", _a_caption(1)),               # dup -> rewritten
        _row(3, "2026-09-09", _a_caption(1)),               # dup -> rewritten
    ]
    store = _FakeStore(rows)
    fresh = iter([_a_caption(50), _a_caption(51)])

    def regen(row, avoid, avoid_category=""):
        return next(fresh), "education"

    # Flag OFF -> no-op
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=regen, gap_filler=lambda *a, **k: "none")
    assert out["ok"] is False

    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=regen, gap_filler=lambda *a, **k: "none")

    assert out["ok"] is True
    assert out["captions_fixed"] == 2
    # Keeper untouched; the two later dates rewritten with distinct captions
    assert rows[0]["caption"] == _a_caption(1)
    assert rows[1]["caption"] != _a_caption(1)
    assert rows[2]["caption"] != _a_caption(1)
    assert rows[1]["caption"] != rows[2]["caption"]
    assert sorted(store.patched_ids) == ["row_2", "row_3"]


def test_dup_remediation_never_touches_approved(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    approved_cap = _a_caption(7)
    rows = [
        _row(1, "2026-09-01", approved_cap),                          # pending dup
        _row(2, "2026-09-06", approved_cap, status="approved"),       # human-owned
        _row(3, "2026-09-11", approved_cap),                          # pending dup
    ]
    store = _FakeStore(rows)
    fresh = iter([_a_caption(60), _a_caption(61)])

    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=lambda r, a, c="": (next(fresh), "education"),
        gap_filler=lambda *a, **k: "none")

    assert out["captions_fixed"] == 2
    # The APPROVED date keeps the caption; BOTH pending dates were rewritten
    assert rows[1]["caption"] == approved_cap
    assert rows[1]["status"] == "approved"
    assert "row_2" not in store.patched_ids
    assert rows[0]["caption"] != approved_cap
    assert rows[2]["caption"] != approved_cap


def test_same_date_mirrors_move_together(monkeypatch):
    """A dup day's IG feed + FB mirror + story share the fresh caption (the day
    stays ONE post); only wipeable rows are patched."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    cap = _a_caption(3)
    rows = [
        _row(1, "2026-09-01", cap),
        _row(2, "2026-09-04", cap),                              # dup day: IG
        _row(3, "2026-09-04", cap, account="facebook"),          # dup day: FB
        _row(4, "2026-09-04", cap, fmt="story"),                 # dup day: story
    ]
    store = _FakeStore(rows)
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=lambda r, a, c="": (_a_caption(70), "education"),
        gap_filler=lambda *a, **k: "none")
    assert out["captions_fixed"] == 1
    assert rows[0]["caption"] == cap                             # keeper day
    assert rows[1]["caption"] == rows[2]["caption"] == rows[3]["caption"] == _a_caption(70)


# ---------------------------------------------------------------------------
# Category over-cap: excess wipeable days re-pillared from a different source
# ---------------------------------------------------------------------------

def test_overcap_repillars_excess_pending_days(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    # 4 of 6 days 'about' (66% > 25%); day 4 approved (never moved)
    rows = [
        _row(1, "2026-09-01", _a_caption(1), pillar="about"),
        _row(2, "2026-09-02", _a_caption(2), pillar="about"),
        _row(3, "2026-09-03", _a_caption(3), pillar="about"),
        _row(4, "2026-09-04", _a_caption(4), pillar="about", status="approved"),
        _row(5, "2026-09-05", _a_caption(5), pillar="community"),
        _row(6, "2026-09-06", _a_caption(6), pillar="education"),
    ]
    store = _FakeStore(rows)
    defects = [("content_mix", "about", "about is 67% of posts (over 25%)")]
    fresh = iter([_a_caption(80), _a_caption(81), _a_caption(82)])

    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=defects, today_iso=TODAY,
        caption_regen=lambda r, a, c="": (next(fresh), "coach"),
        gap_filler=lambda *a, **k: "none")

    assert out["repillared"] >= 1
    # The approved day is never re-pointed
    assert rows[3]["pillar"] == "about"
    assert "row_4" not in store.patched_ids
    # Moved rows now carry the pillar that actually wrote the caption
    moved = [r for r in rows if r["pillar"] == "coach"]
    assert moved, rows
    for r in moved:
        assert r["status"] == "pending"


def test_overcap_skips_when_regen_lands_on_same_category(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [_row(i, f"2026-09-{i:02d}", _a_caption(i), pillar="about")
            for i in range(1, 5)]
    store = _FakeStore(rows)
    defects = [("content_mix", "about", "about is 100% of posts (over 25%)")]
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=defects, today_iso=TODAY,
        caption_regen=lambda r, a, c="": (_a_caption(90), "about"),
        gap_filler=lambda *a, **k: "none")
    # The content does not support a different pillar: nothing is re-pointed
    assert out["repillared"] == 0
    assert all(r["pillar"] == "about" for r in rows)


# ---------------------------------------------------------------------------
# Day gaps: unfillable gaps recorded once, never a storm
# ---------------------------------------------------------------------------

def test_unfillable_gap_recorded_once(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")

    class _KV:
        def __init__(self):
            self.data = {}

        def kv_get(self, key, default=""):
            return self.data.get(key, default)

        def kv_set(self, key, value):
            self.data[key] = str(value)

        def kv_is_durable(self):
            return True

    kv = _KV()
    rows = [
        _row(1, "2026-09-01", _a_caption(1)),
        _row(2, "2026-09-09", _a_caption(2)),        # 7-day gap before 09-09
    ]
    store = _FakeStore(rows)
    defects = [("consistency", "2026-09-09", "gap of 7 days before 2026-09-09")]
    logs = []
    for _ in range(2):
        grade_fix.remediate_forward_book(
            "gritx", rows, store, profile="GYM", defects=defects,
            today_iso=TODAY, caption_regen=lambda r, a, c="": None,
            gap_filler=lambda *a, **k: "no_media", db=kv,
            logger=logs.append)
    # Recorded exactly once, no matter how many sweeps see the same gap
    assert kv.data.get("grade_gap_known_gritx_2026-09-09") == "1"
    assert sum("recorded once" in l for l in logs) == 1


# ---------------------------------------------------------------------------
# Sweep alert policy: dedup by (score, defect set), one per gym per day,
# one aggregated summary line, self-fixed gyms alert nothing
# ---------------------------------------------------------------------------

def test_held_alert_fires_once_then_dedups(_self_fix_on, monkeypatch):
    # No regen context in tests -> remediation runs but cannot fix; the book
    # stays below A. First sweep: one held alert + one summary. Second sweep
    # (same state, same day): silence.
    rows = _forward_rows_with_dups()
    store = _FakeStore(rows)

    alerts1 = []
    r1 = _sweep(store, alerts1)
    assert r1["held"] == ["gritx"]
    held = [a for a in alerts1 if a.startswith("calendar grade: gritx")]
    assert len(held) == 1
    # <= 3 lines: score, auto-fixed, remaining
    assert held[0].count("\n") <= 2
    assert "Auto-fixed:" in held[0] and "Remaining:" in held[0]
    summary = [a for a in alerts1 if a.startswith("grade sweep:")]
    assert len(summary) == 1
    assert "0 self-fixed to A" in summary[0] and "gritx" in summary[0]

    alerts2 = []
    r2 = _sweep(store, alerts2)
    assert r2["held"] == ["gritx"]
    assert alerts2 == [], f"same state must not re-alert: {alerts2}"


def test_held_alert_max_one_per_gym_per_day_even_if_state_changes(_self_fix_on):
    rows = _forward_rows_with_dups()
    store = _FakeStore(rows)
    alerts1 = []
    _sweep(store, alerts1)
    assert any(a.startswith("calendar grade: gritx") for a in alerts1)

    # Change the state (drop a dup day) but stay below A: same day -> silent
    store.rows = [r for r in store.rows if r["id"] != "row_9"]
    alerts2 = []
    _sweep(store, alerts2)
    assert not any(a.startswith("calendar grade: gritx") for a in alerts2), alerts2


def test_self_fixed_to_a_alerts_only_summary(_self_fix_on, monkeypatch):
    rows = _forward_rows_with_dups()
    store = _FakeStore(rows)

    # Inject a working regen through the default seam: each call yields a
    # fresh, A-grade caption so remediation lifts the book to A.
    counter = {"n": 100}

    def fake_default_regen(gym_id, profile, log):
        def _regen(row, avoid, avoid_category=""):
            counter["n"] += 1
            return _a_caption(counter["n"]), "education"
        return _regen

    monkeypatch.setattr(grade_fix, "_default_caption_regen", fake_default_regen)

    alerts = []
    result = _sweep(store, alerts)
    assert result["self_fixed"] == ["gritx"], result
    assert result["held"] == []
    # The stored forward grade is the POST-fix grade (A)
    fwd = [g for g in store.grades if g["window"] == "forward_book"]
    assert fwd and fwd[-1]["letter"] == "A"
    # No held alert; exactly one aggregated summary line
    assert len(alerts) == 1, alerts
    assert alerts[0].startswith("grade sweep:")
    assert "1 self-fixed to A" in alerts[0]


def test_all_a_books_stay_silent(_self_fix_on):
    rows = [_row(i, f"2026-09-{(i + 1):02d}", _a_caption(i),
                 pillar=["community", "education", "coach", "invite", "story"][i % 5])
            for i in range(10)]
    store = _FakeStore(rows)
    alerts = []
    result = _sweep(store, alerts)
    assert result["ok"] is True
    assert result["self_fixed"] == [] and result["held"] == []
    assert alerts == [], f"an A book must produce zero Slack lines: {alerts}"
    assert "self_fix" not in result["gyms"]["gritx"]
