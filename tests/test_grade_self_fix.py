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


# ---------------------------------------------------------------------------
# Craft/path pass: flagged captions regenerated on the same photo; the fresh
# caption must ACTUALLY clear the flags or the row keeps its caption
# ---------------------------------------------------------------------------

def _no_ask_caption(i):
    """150+ chars, short hook, NO ask and NO booking term."""
    hook = f"Small wins add up fast at our gym. Number {i}."
    body = (
        "\nOur members show what steady effort looks like in real life. "
        "Coaches guide every class with care and the community keeps the "
        "energy warm for beginners and busy parents alike."
    )
    return hook + body


def _thin_caption(i):
    """Under 120 chars (thin_caption) but carries exactly one ask."""
    return f"Great vibes in class today. Sign up today. Post {i}."


def _long_hook_caption(i):
    """First line over 125 chars (hook_too_long); one ask in the body."""
    hook = (
        "Every single person who walks through our doors this month is "
        "going to find out exactly why our members keep telling their "
        f"friends about class number {i} here at the gym."
    )
    body = "\nOur coaches meet you where you are. Sign up today."
    return hook + body


def _remediate(rows, store, regen, *, defects=(), booking_cta=None,
               profile="GYM"):
    return grade_fix.remediate_forward_book(
        "gritx", rows, store, profile=profile, defects=list(defects),
        today_iso=TODAY, caption_regen=regen,
        gap_filler=lambda *a, **k: "none", booking_cta=booking_cta)


def test_craft_no_ask_regen_gets_exactly_one_ask(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [
        _row(1, "2026-09-01", _no_ask_caption(1)),            # flagged: no_ask
        _row(2, "2026-09-02", _a_caption(2)),                 # passes: untouched
    ]
    store = _FakeStore(rows)
    out = _remediate(rows, store, lambda r, a, c="": (_a_caption(40), "education"))
    assert out["craft_attempted"] == 1
    assert out["craft_fixed"] == 1
    assert rows[0]["caption"] == _a_caption(40)
    assert grade_fix._ask_count(rows[0]["caption"]) == 1
    # The passing row was never touched
    assert rows[1]["caption"] == _a_caption(2)
    assert store.patched_ids == ["row_1"]


def test_craft_thin_caption_regen_meets_length(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [_row(1, "2026-09-01", _thin_caption(1))]
    store = _FakeStore(rows)
    out = _remediate(rows, store, lambda r, a, c="": (_a_caption(41), "education"))
    assert out["craft_fixed"] == 1
    assert 150 <= len(rows[0]["caption"]) <= 500


def test_craft_long_hook_regen_shortens_hook(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [_row(1, "2026-09-01", _long_hook_caption(1))]
    store = _FakeStore(rows)
    assert len(_long_hook_caption(1).splitlines()[0]) > 125
    out = _remediate(rows, store, lambda r, a, c="": (_a_caption(42), "education"))
    assert out["craft_fixed"] == 1
    assert len(rows[0]["caption"].splitlines()[0]) <= 125


def test_craft_never_swaps_in_worse_caption(monkeypatch):
    """A regen that cannot clear the bar (thin, ask-free) leaves the row
    exactly as it was: attempted, not fixed."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    original = _thin_caption(1)
    rows = [_row(1, "2026-09-01", original)]
    store = _FakeStore(rows)
    out = _remediate(rows, store, lambda r, a, c="": ("Nice day at the gym.", ""))
    assert out["craft_attempted"] == 1
    assert out["craft_fixed"] == 0
    assert rows[0]["caption"] == original
    assert store.patched_ids == []


def test_craft_never_touches_approved_rows(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    flagged = _thin_caption(7)
    rows = [
        _row(1, "2026-09-01", flagged, status="approved"),    # human-owned
        _row(2, "2026-09-02", _thin_caption(8)),              # wipeable, flagged
    ]
    store = _FakeStore(rows)
    out = _remediate(rows, store, lambda r, a, c="": (_a_caption(43), "education"))
    assert rows[0]["caption"] == flagged
    assert rows[0]["status"] == "approved"
    assert "row_1" not in store.patched_ids
    assert out["craft_fixed"] == 1 and store.patched_ids == ["row_2"]


def test_craft_mirrors_move_together(monkeypatch):
    """A flagged day's IG feed + FB mirror + story get the same fresh caption."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    cap = _no_ask_caption(9)
    rows = [
        _row(1, "2026-09-01", cap),
        _row(2, "2026-09-01", cap, account="facebook"),
        _row(3, "2026-09-01", cap, fmt="story"),
    ]
    store = _FakeStore(rows)
    out = _remediate(rows, store, lambda r, a, c="": (_a_caption(44), "education"))
    assert out["craft_fixed"] == 1
    assert rows[0]["caption"] == rows[1]["caption"] == rows[2]["caption"] == _a_caption(44)


def test_craft_pass_b2b_gets_mechanics_but_never_the_llm_regen(monkeypatch):
    """B2B/LASSO: the LLM regen stays out of scope (its caption CONTENT is owned
    by the pillar builders), but the MECHANICAL repair now runs — re-lineating a
    hook and appending LASSO's OWN approved CTA invents nothing, and B2B having
    no repair path at all is what left 116 of LASSO's 121 forward rows flagged
    'no ask' with nothing able to clear them (2026-08-31)."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    cta = "Book a call and we will look at your numbers."
    calls = []

    def _regen(r, a, c=""):
        calls.append(r)                      # must never happen on a B2B book
        return (_a_caption(45), "education")

    rows = [_row(1, "2026-09-01", _no_ask_caption(5), gym_id="lasso")]
    store = _FakeStore(rows)
    out = grade_fix.remediate_forward_book(
        "lasso", rows, store, profile="B2B", defects=[], today_iso=TODAY,
        caption_regen=_regen, gap_filler=lambda *a, **k: "none",
        booking_cta=cta)
    assert calls == [], "B2B must never spend an LLM regen on caption craft"
    assert out["craft_fixed"] == 1
    assert rows[0]["caption"].endswith(cta)
    assert grade_fix._ask_count(rows[0]["caption"]) == 1


def test_craft_flag_off_is_noop():
    rows = [_row(1, "2026-09-01", _thin_caption(1))]
    store = _FakeStore(rows)
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=lambda r, a, c="": (_a_caption(46), "education"),
        gap_filler=lambda *a, **k: "none")
    assert out["ok"] is False
    assert rows[0]["caption"] == _thin_caption(1)
    assert store.patched_ids == []


# ---------------------------------------------------------------------------
# Booking-term asks (path_to_join GYM leg): the gym's REAL CTA, never invented
# ---------------------------------------------------------------------------

def test_booking_cta_carried_onto_flagged_rows(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    cta = "Book your intro class this week."
    # Zero booking-term rows on the book; the flagged day is ask-free after
    # regen, so the gym's real CTA becomes its single (booking) ask.
    rows = [
        _row(1, "2026-09-01", _no_ask_caption(1)),            # flagged
        _row(2, "2026-09-02", _no_ask_caption(2)),            # flagged
    ]
    store = _FakeStore(rows)
    fresh = iter([_no_ask_caption(60), _no_ask_caption(61)])
    out = _remediate(rows, store,
                     lambda r, a, c="": (next(fresh), "education"),
                     booking_cta=cta)
    assert out["craft_fixed"] == 2
    assert out["booking_asks_added"] == 2
    for r in rows:
        assert r["caption"].endswith(cta)
        assert grade_fix._ask_count(r["caption"]) == 1
        assert grade_fix._BOOKING_RE.search(r["caption"])


def test_mechanics_repair_an_ask_free_day_without_calling_the_llm(monkeypatch):
    """THE CONVERGENCE FIX (2026-08-31). A measured LLM regen costs 6 to 8
    SECONDS; the deterministic mechanical repair costs nothing and clears the
    same bar for the overwhelming majority of flagged days. So mechanics is
    tried FIRST and the regen is consulted only where mechanics genuinely
    cannot help. The old order paid the LLM on every flagged day before trying
    the free fix, which is why a nightly pass across ten gyms never finished.

    Exactly-one-ask still holds: the ask-free day ends with the gym's approved
    booking CTA as its single ask."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    cta = "Book your intro class this week."
    calls = []

    def _regen(r, a, c=""):
        calls.append(r)
        return (_a_caption(62), "education")

    rows = [_row(1, "2026-09-01", _no_ask_caption(1))]
    store = _FakeStore(rows)
    out = _remediate(rows, store, _regen, booking_cta=cta)
    assert out["craft_fixed"] == 1
    assert calls == [], (
        "mechanics cleared this day, so the LLM regen must not have been called")
    assert rows[0]["caption"].endswith(cta)
    assert grade_fix._ask_count(rows[0]["caption"]) == 1
    assert out["booking_asks_added"] == 1


def test_booking_cta_never_invented(monkeypatch):
    """No real booking CTA available (test env has no voice doc): an ask-free
    regen cannot clear the bar and the row is honestly left unchanged."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    original = _no_ask_caption(1)
    rows = [_row(1, "2026-09-01", original)]
    store = _FakeStore(rows)
    out = _remediate(rows, store,
                     lambda r, a, c="": (_no_ask_caption(63), "education"))
    assert out["craft_attempted"] == 1
    assert out["craft_fixed"] == 0
    assert out["booking_asks_added"] == 0
    assert rows[0]["caption"] == original


def test_rows_with_booking_asks_already_passing_untouched(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [_row(i, f"2026-09-{i:02d}", _a_caption(i)) for i in range(1, 7)]
    store = _FakeStore(rows)
    out = _remediate(rows, store,
                     lambda r, a, c="": (_a_caption(70), "education"),
                     booking_cta="Book your intro class this week.")
    assert out["craft_attempted"] == 0
    assert out["booking_asks_added"] == 0
    assert store.patched_ids == []


# ---------------------------------------------------------------------------
# Over-cap iteration: converge to <= 25% per category, or stop honestly
# ---------------------------------------------------------------------------

def test_overcap_iterates_until_under_cap(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [_row(i, f"2026-09-{i:02d}", _a_caption(i), pillar="offer")
            for i in range(1, 9)]                             # 8 rows, 100% offer
    store = _FakeStore(rows)
    defects = [("content_mix", "offer", "offer is 100% of posts (over 25%)")]
    outs = iter([(_a_caption(80), "community"), (_a_caption(81), "community"),
                 (_a_caption(82), "education"), (_a_caption(83), "education"),
                 (_a_caption(84), "coach"), (_a_caption(85), "coach"),
                 (_a_caption(86), "story"), (_a_caption(87), "story")])
    out = _remediate(rows, store, lambda r, a, c="": next(outs),
                     defects=defects)
    assert out["repillared"] == 6
    from collections import Counter
    counts = Counter(r["pillar"] for r in rows)
    n = len(rows)
    assert all(c / n <= 0.25 for c in counts.values()), counts


def test_overcap_stops_honestly_when_no_headroom(monkeypatch):
    """Regen only ever offers one target category: the pass moves what fits
    under that target's own 25% cap, then stops (no ping-pong, no loop)."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = [_row(i, f"2026-09-{i:02d}", _a_caption(i), pillar="offer")
            for i in range(1, 9)]
    store = _FakeStore(rows)
    defects = [("content_mix", "offer", "offer is 100% of posts (over 25%)")]
    counter = {"n": 90}

    def regen(row, avoid, avoid_category=""):
        counter["n"] += 1
        return _a_caption(counter["n"]), "community"

    out = _remediate(rows, store, regen, defects=defects)
    # community holds 2 of 8 (25%); the rest cannot honestly move
    assert out["repillared"] == 2
    from collections import Counter
    counts = Counter(r["pillar"] for r in rows)
    assert counts["community"] == 2 and counts["offer"] == 6


# ---------------------------------------------------------------------------
# Sweep multi-pass: up to 3 remediation passes while the score improves
# ---------------------------------------------------------------------------

def _multi_pass_book():
    """9 consecutive days; one caption repeated on days 1..5 (consistency 0 +
    dup cap -> 59) and a 3-of-9 community pillar (mix defect that no pass
    fixes). Un-dupping one day at a time walks the score 59 -> 78 -> 87 -> A."""
    pillars = ["community", "community", "community", "education", "coach",
               "invite", "story", "offer", "welcome"]
    rows = []
    for i in range(9):
        cap = _a_caption(1) if i < 5 else _a_caption(i + 1)
        rows.append(_row(i + 1, f"2026-09-{(i + 1):02d}", cap,
                         pillar=pillars[i]))
    return rows


def test_multi_pass_improves_then_stops_at_a(_self_fix_on, monkeypatch):
    rows = _multi_pass_book()
    store = _FakeStore(rows)
    calls = []
    # Each fake pass honestly un-dups part of the book: pass 1 fixes two dup
    # days, passes 2 and 3 one each. Score improves every pass, reaching A
    # exactly at the 3-pass cap.
    plan = iter([["row_2", "row_3"], ["row_4"], ["row_5"]])

    def fake_remediate(gym_id, f_rows, f_store, *, profile, defects,
                       today_iso, **kw):
        calls.append(gym_id)
        fixed = 0
        for rid in next(plan):
            for r in f_store.rows:
                if r["id"] == rid:
                    r["caption"] = _a_caption(200 + len(calls) * 10 + fixed)
                    fixed += 1
        return {"ok": True, "captions_fixed": fixed, "repillared": 0,
                "craft_fixed": 0, "craft_attempted": 0,
                "booking_asks_added": 0, "gap_fill": "none", "skipped": 0,
                "actions": [f"pass {len(calls)}"]}

    monkeypatch.setattr(grade_fix, "remediate_forward_book", fake_remediate)
    alerts = []
    result = _sweep(store, alerts)
    assert calls == ["gritx"] * 3, calls
    assert result["self_fixed"] == ["gritx"]
    fix = result["gyms"]["gritx"]["self_fix"]
    assert fix["passes"] == 3
    assert fix["captions_fixed"] == 4
    fwd = [g for g in store.grades if g["window"] == "forward_book"]
    assert fwd and fwd[-1]["letter"] == "A"


def test_multi_pass_stops_when_score_does_not_improve(_self_fix_on, monkeypatch):
    rows = _forward_rows_with_dups()
    store = _FakeStore(rows)
    calls = []

    def fake_remediate(gym_id, f_rows, f_store, **kw):
        calls.append(gym_id)                      # changes nothing
        return {"ok": True, "captions_fixed": 0, "repillared": 0,
                "craft_fixed": 0, "craft_attempted": 0,
                "booking_asks_added": 0, "gap_fill": "none", "skipped": 0,
                "actions": []}

    monkeypatch.setattr(grade_fix, "remediate_forward_book", fake_remediate)
    alerts = []
    result = _sweep(store, alerts)
    assert calls == ["gritx"], "no improvement must stop after one pass"
    assert result["held"] == ["gritx"]


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


def test_mechanical_repair_fixes_hook_and_ask_without_fabrication():
    """2026-08-31: the LLM regen cleared 0/56 flagged ENG days. Mechanics now fix what
    mechanics can: an over-long first line is re-lineated at its sentence boundary (no
    words change) and a missing ask gets the gym's APPROVED CTA appended."""
    from agent.jobs.grade_fix import _mechanical_repair, _clears_craft
    long_hook = ("You are juggling work and kids and the list never ends so the gym "
                 "always slips to the bottom of the pile every single week. We built "
                 "coaching for exactly that life.")
    body = "Small group sessions that fit real schedules with a coach who knows you."
    cap = f"{long_hook}\n{body}"
    out = _mechanical_repair(cap, "Book your intro session today?")
    assert out is not None
    first = out.splitlines()[0]
    assert len(first) <= 125
    # zero fabrication: every original word survives, in order
    import re
    orig_words = re.findall(r"[A-Za-z']+", cap)
    new_words = re.findall(r"[A-Za-z']+", out)
    assert new_words[:len(orig_words)] == orig_words
    assert _clears_craft(out), f"repair must clear the bar, got: {out!r}"


def test_mechanical_repair_gives_up_on_unbreakable_hook():
    from agent.jobs.grade_fix import _mechanical_repair
    one_long_sentence = ("a single breathless sentence that just keeps going and going "
                         "without any punctuation at all so no honest split point exists "
                         "anywhere within the hook band limit")
    assert _mechanical_repair(one_long_sentence, "Book now?") is None


def test_booking_cta_falls_back_to_approved_source_sentence(monkeypatch, tmp_path):
    """ENG 2026-08-31: the bible's CTA rotation was an onboarding TODO, so no_ask days
    had nothing approved to carry. A VERBATIM booking-ask sentence from the gym's own
    APPROVED sources is approved copy — the fallback uses the shortest qualifying one."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    from agent import client_sources as cs
    from agent.jobs.grade_fix import _booking_cta_for
    cs.add_source("gymz_ig", "offer",
                  "Our six week kickstart fills fast every season. "
                  "Book your intro session today?",
                  "website /start")
    # voice doc resolution will find nothing (no bible on disk for gymz)
    cta = _booking_cta_for("gymz", lambda m: None)
    assert cta == "Book your intro session today?"


# ---------------------------------------------------------------------------
# THE CONVERGENCE CONTRACT (Blake, 2026-08-31: "run it repeatedly and the
# defect count strictly decreases to a floor, and that floor is documented")
# ---------------------------------------------------------------------------

def _book_needing_repair(gym_id="gritx", n=12):
    """A forward book whose every day is ask-free: exactly the shape that held
    five of seven live gyms at C or worse."""
    return [_row(i, f"2026-09-{(i + 1):02d}", _no_ask_caption(i), gym_id=gym_id)
            for i in range(n)]


def test_repair_loop_strictly_decreases_defects_then_holds_a_floor(monkeypatch):
    """Repair -> regrade -> repair -> regrade must MONOTONICALLY reduce defects
    and then stop moving. A loop that oscillates (topfuel went 74 -> 64 -> 67 ->
    71 -> 74 across live runs) or that plateaus above its real floor is the bug
    this pins shut."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    from agent.calendar_grade import grade_month
    rows = _book_needing_repair()
    store = _FakeStore(rows)
    cta = "Book your intro class this week."

    counts = [len(grade_month(rows, profile="GYM").defects)]
    for _ in range(4):
        grade_fix.remediate_forward_book(
            "gritx", rows, store, profile="GYM",
            defects=grade_month(rows, profile="GYM").defects, today_iso=TODAY,
            caption_regen=None, gap_filler=lambda *a, **k: "none",
            booking_cta=cta)
        counts.append(len(grade_month(rows, profile="GYM").defects))

    assert counts[0] > 0, "the fixture must start with real defects"
    # strictly non-increasing, and the first pass must make real progress
    assert all(b <= a for a, b in zip(counts, counts[1:])), counts
    assert counts[1] < counts[0], f"first pass made no progress: {counts}"
    # and it settles: once it stops improving it never climbs again
    assert counts[-1] == min(counts), counts


def test_repair_loop_reaches_A_on_a_book_mechanics_can_fully_fix(monkeypatch):
    """The floor is only honest if the loop actually lands an A when nothing
    blocks it. Mechanics alone (no LLM) must take an ask-free book to A."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    from agent.calendar_grade import A_THRESHOLD, grade_month
    rows = _book_needing_repair()
    store = _FakeStore(rows)
    for _ in range(3):
        grade_fix.remediate_forward_book(
            "gritx", rows, store, profile="GYM",
            defects=grade_month(rows, profile="GYM").defects, today_iso=TODAY,
            caption_regen=None, gap_filler=lambda *a, **k: "none",
            booking_cta="Book your intro class this week.")
    g = grade_month(rows, profile="GYM")
    assert g.total >= A_THRESHOLD, f"{g.total} ({g.letter}) scores={g.scores} defects={g.defects[:5]}"


def test_approved_rows_are_the_documented_floor(monkeypatch):
    """A human-approved row is NEVER rewritten (pierce: 29 of 35 flagged posts
    are approved). The loop must reach that floor and stop there, leaving the
    approved captions byte-for-byte intact rather than churning forever."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    from agent.calendar_grade import grade_month
    locked = _no_ask_caption(99)
    rows = _book_needing_repair(n=6)
    rows.append(_row(90, "2026-09-20", locked, status="approved"))
    store = _FakeStore(rows)
    counts = []
    for _ in range(3):
        grade_fix.remediate_forward_book(
            "gritx", rows, store, profile="GYM",
            defects=grade_month(rows, profile="GYM").defects, today_iso=TODAY,
            caption_regen=None, gap_filler=lambda *a, **k: "none",
            booking_cta="Book your intro class this week.")
        counts.append(len(grade_month(rows, profile="GYM").defects))
    assert rows[-1]["caption"] == locked, "an approved caption must never be rewritten"
    assert counts[-1] == counts[-2], f"loop must settle at its floor, got {counts}"


def test_llm_budget_exhausted_still_runs_mechanics(monkeypatch):
    """A spent wall-clock budget must not stop the pass: mechanics is free and
    keeps working. A bounded pass that always finishes beats an unbounded one
    that never does."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    calls = []

    def _regen(r, a, c=""):
        calls.append(r)
        return (_a_caption(70), "education")

    rows = _book_needing_repair(n=4)
    store = _FakeStore(rows)
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=_regen, gap_filler=lambda *a, **k: "none",
        booking_cta="Book your intro class this week.", llm_budget_s=-1)
    assert calls == [], "no LLM spend once the budget is gone"
    assert out["craft_fixed"] == 4, "mechanics must still repair every day"


# ---------------------------------------------------------------------------
# Grader honesty: caption-less posts are exempt EXPLICITLY, never dropped
# ---------------------------------------------------------------------------

def test_captionless_posts_are_exempt_and_counted_not_silently_dropped():
    """A story's caption is burned onto its media and a GBP photo post carries
    none. Judging them by feed-caption rules invented 51 unfixable defects on
    LASSO's book. They are exempt AND counted, so the digest can say so."""
    from agent.calendar_grade import grade_month
    rows = []
    for d in range(1, 11):
        day = f"2026-09-{d:02d}"
        rows.append(_row(d, day, _a_caption(d)))
        rows.append(_row(100 + d, day, "", fmt="story"))       # burned caption
    g = grade_month(rows, profile="GYM")
    assert not [x for x in g.defects if "no ask" in str(x[2])], g.defects
    assert g.exempt, "the exemption must be REPORTED, not silent"
    assert any(v == 10 for v in g.exempt.values()), g.exempt
    assert g.total >= 90, f"{g.total} {g.scores} {g.defects}"


def test_one_caption_mirrored_to_three_rows_is_one_defect_not_three():
    """ENG's single HYROX hook produced 3 right_audience defects because the IG
    feed, its FB mirror and the paired story were each judged separately."""
    from agent.calendar_grade import grade_month
    bad = "You signed up for HYROX but haven't trained.\n" + _a_caption(3)
    rows = [
        _row(1, "2026-09-01", bad),
        _row(2, "2026-09-01", bad, account="facebook"),
        _row(3, "2026-09-01", bad, fmt="story"),
    ]
    g = grade_month(rows, profile="GYM")
    leaks = [d for d in g.defects if d[0] == "right_audience"]
    assert len(leaks) == 1, f"one post, one defect; got {leaks}"


def test_partial_repair_moves_the_score(monkeypatch):
    """The absolute per-row penalties meant a book that repaired 27 of its 30
    bad posts scored exactly the same as one that repaired none, so the nightly
    loop looked stuck even when it was working. Rate scoring must register it."""
    from agent.calendar_grade import grade_month
    def _book(bad):
        rows = [_row(i, f"2026-09-{(i + 1):02d}", _no_ask_caption(i))
                for i in range(bad)]
        rows += [_row(50 + i, f"2026-10-{(i + 1):02d}", _a_caption(i))
                 for i in range(30 - bad)]
        return rows
    all_bad = grade_month(_book(30), profile="GYM").total
    mostly_fixed = grade_month(_book(3), profile="GYM").total
    assert mostly_fixed > all_bad, (
        f"repairing 27 of 30 posts must raise the score: {all_bad} -> {mostly_fixed}")


def test_overcap_pass_respects_the_llm_budget(monkeypatch):
    """The over-cap move genuinely needs the LLM (it must find a DIFFERENT
    approved category), and it iterates up to 6 waves. Unbounded, one gym could
    eat the whole nightly run — hillcountry's over-cap pass alone outran a ten
    minute ceiling. A spent budget stops it honestly instead."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    calls = []

    def _regen(r, a, c=""):
        calls.append(r)
        return (_a_caption(80), "education")

    rows = [_row(i, f"2026-09-{(i + 1):02d}", _a_caption(i), pillar="offer")
            for i in range(12)]
    store = _FakeStore(rows)
    out = grade_fix.remediate_forward_book(
        "gritx", rows, store, profile="GYM",
        defects=[("content_mix", "offer", "offer is 100% of posts (over 25%)")],
        today_iso=TODAY, caption_regen=_regen,
        gap_filler=lambda *a, **k: "none", llm_budget_s=-1)
    assert calls == [], "no LLM spend on the over-cap pass once the budget is gone"
    assert out["repillared"] == 0
    assert out["ok"] is True, "a spent budget is an honest stop, not a failure"


# ---------------------------------------------------------------------------
# REGRESSION GUARD: alert when a book's grade DROPS between runs
# ---------------------------------------------------------------------------

class _GradeHistoryStore(_FakeStore):
    """A store that can answer 'what did this gym score last run?' — the real
    Supabase store is queried over REST for the same thing."""

    def latest_grade(self, gym_id, window):
        rows = [g for g in self.grades
                if g["gym_id"] == gym_id and g["window"] == window]
        return rows[-1] if rows else None


def _clean_book(gym_id="gritx", n=8):
    return [_row(i, f"2026-09-{(i + 1):02d}", _a_caption(i), gym_id=gym_id)
            for i in range(n)]


def test_alert_fires_when_a_books_grade_drops_between_runs(_self_fix_on):
    """The failure mode Blake named: 'the alert fires nightly and everyone
    stops reading it'. Below-B is a standing condition; a DROP is an event —
    it means a build is re-creating defects faster than the repair clears
    them (topfuel went 74 -> 64 -> 67 -> 71 -> 74 live and nothing said so)."""
    rows = _clean_book()
    store = _GradeHistoryStore(rows)
    alerts = []
    _sweep(store, alerts)
    first = [g for g in store.grades if g["window"] == "forward_book"][-1]["total"]
    assert not [a for a in alerts if "DROPPED" in a], alerts

    # A later build lands off-avatar hooks on days that were clean.
    for r in rows[:3]:
        r["caption"] = "Compete with the best athletes in town.\n" + _a_caption(9)
    alerts.clear()
    _sweep(store, alerts)
    second = [g for g in store.grades if g["window"] == "forward_book"][-1]["total"]
    assert second < first, f"fixture must actually get worse: {first} -> {second}"
    drops = [a for a in alerts if "DROPPED" in a]
    assert len(drops) == 1, alerts
    assert f"{first} -> {second}" in drops[0], drops[0]


def test_drop_alert_does_not_fire_when_a_book_improves(_self_fix_on):
    """A book that got BETTER must never trip the regression guard."""
    rows = [_row(i, f"2026-09-{(i + 1):02d}",
                 "Compete with the best athletes in town.\n" + _a_caption(i))
            for i in range(8)]
    store = _GradeHistoryStore(rows)
    alerts = []
    _sweep(store, alerts)
    for i, r in enumerate(rows):
        r["caption"] = _a_caption(i)
    alerts.clear()
    _sweep(store, alerts)
    assert not [a for a in alerts if "DROPPED" in a], alerts


def test_drop_alert_is_deduped_to_once_per_gym_per_day(_self_fix_on):
    """One drop, one alert. The guard must not become the nightly noise it
    exists to replace."""
    rows = _clean_book()
    store = _GradeHistoryStore(rows)
    alerts = []
    _sweep(store, alerts)
    for r in rows[:3]:
        r["caption"] = "Compete with the best athletes in town.\n" + _a_caption(9)
    alerts.clear()
    _sweep(store, alerts)
    _sweep(store, alerts)
    _sweep(store, alerts)
    assert len([a for a in alerts if "DROPPED" in a]) == 1, alerts


# ---------------------------------------------------------------------------
# Hard copy violations: one bad dash zeroed a whole book's craft leg
# ---------------------------------------------------------------------------

def test_one_banned_dash_zeroes_the_leg_and_the_scrub_pass_clears_it(monkeypatch):
    """calendar_grade hard-zeroes the ENTIRE caption_craft leg (20 points) on
    the first offending row, and every other repair path only looked at soft
    flags — so a single intraword hyphen cost LASSO its whole craft leg with
    nothing able to clear it. The house scrubber fixes it deterministically."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    from agent.calendar_grade import grade_month
    rows = [_row(i, f"2026-09-{(i + 1):02d}", _a_caption(i)) for i in range(6)]
    rows[2]["caption"] = _a_caption(2).replace("Real change", "Real—change")
    before = grade_month(rows, profile="GYM")
    assert before.scores["caption_craft"] == 0, "one dash must zero the leg"

    out = grade_fix.remediate_forward_book(
        "gritx", rows, store := _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=None, gap_filler=lambda *a, **k: "none")
    assert out["scrubbed"] == 1
    after = grade_month(rows, profile="GYM")
    assert after.scores["caption_craft"] == 20, after.defects
    assert after.total > before.total
    # zero fabrication: only the dash changed
    assert "change" in rows[2]["caption"] and "—" not in rows[2]["caption"]


def test_scrub_pass_never_touches_an_approved_caption(monkeypatch):
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    dirty = _a_caption(1).replace("Real change", "Real—change")
    rows = [_row(1, "2026-09-01", dirty, status="approved")]
    out = grade_fix.remediate_forward_book(
        "gritx", rows, _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=None, gap_filler=lambda *a, **k: "none")
    assert out["scrubbed"] == 0
    assert rows[0]["caption"] == dirty


# ---------------------------------------------------------------------------
# THE INVARIANT: a repair pass must never LOWER the grade
# ---------------------------------------------------------------------------

def test_overcap_move_never_creates_a_new_over_cap_category(monkeypatch):
    """Live 2026-08-31: sunnyside went 71 (C) -> 67 (D) BECAUSE of a repair.
    The grader counts mix per POST; this pass counted ROWS, so its headroom
    guard believed a target pillar had room it did not have and moved 3 days
    out of one over-cap pillar straight into a new one. Post-space arithmetic
    on both sides, and the guard holds."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    from agent.calendar_grade import grade_month
    # 7 posts, each mirrored to IG + FB + story: row space is 3x post space.
    rows = []
    for i in range(7):
        day = f"2026-09-{(i + 1):02d}"
        cap = _a_caption(i)
        rows.append(_row(i * 3, day, cap, pillar="service"))
        rows.append(_row(i * 3 + 1, day, cap, pillar="service", account="facebook"))
        rows.append(_row(i * 3 + 2, day, cap, pillar="service", fmt="story"))
    before = grade_month(rows, profile="GYM")
    out = grade_fix.remediate_forward_book(
        "gritx", rows, _FakeStore(rows), profile="GYM",
        defects=[("content_mix", "service", "service is 100% of posts (over 25%)")],
        today_iso=TODAY,
        # every regen offers the SAME target pillar, so only the first move
        # could ever be honest and the rest must be refused for lack of headroom
        caption_regen=lambda r, a, c="": (_a_caption(90 + len(a)), "educational"),
        gap_filler=lambda *a, **k: "none")
    after = grade_month(rows, profile="GYM")
    assert after.total >= before.total, (
        f"a repair must never lower the grade: {before.total} -> {after.total}; "
        f"repillared={out['repillared']}, defects={after.defects}")
    cats = {r.get("pillar") for r in rows}
    assert "educational" not in cats or after.scores["content_mix"] >= before.scores["content_mix"]


def test_repair_never_lowers_the_grade_across_repeated_passes(monkeypatch):
    """The general contract, not one shape: run the loop and the score must be
    monotonically non-decreasing pass over pass."""
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    from agent.calendar_grade import grade_month
    rows = []
    for i in range(9):
        day = f"2026-09-{(i + 1):02d}"
        cap = _no_ask_caption(i) if i % 2 else _a_caption(i)
        pillar = ["service", "about", "service"][i % 3]
        rows.append(_row(i * 2, day, cap, pillar=pillar))
        rows.append(_row(i * 2 + 1, day, cap, pillar=pillar, account="facebook"))
    store = _FakeStore(rows)
    totals = [grade_month(rows, profile="GYM").total]
    for _ in range(3):
        g = grade_month(rows, profile="GYM")
        grade_fix.remediate_forward_book(
            "gritx", rows, store, profile="GYM", defects=g.defects,
            today_iso=TODAY,
            caption_regen=lambda r, a, c="": (_a_caption(200 + len(a)), "education"),
            gap_filler=lambda *a, **k: "none",
            booking_cta="Book your intro class this week.")
        totals.append(grade_month(rows, profile="GYM").total)
    assert all(b >= a for a, b in zip(totals, totals[1:])), totals
