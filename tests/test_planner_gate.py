"""
tests/test_planner_gate.py — Wave 5: apply_month_plan calendar grade gate tests.

All tests are deterministic and offline. They use monkeypatching to isolate the
calendar grade gate from the store, the real planner, and ops_alerts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

# ---------------------------------------------------------------------------
# Fakes / stubs
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, gym_id, month):
        self.deleted.append((gym_id, month))
        return 0

    def insert_rows(self, gym_id, rows):
        self.inserted.extend(rows)
        return rows

    def gym_autonomy(self, gym_id):
        return False

    rows_in_range = None  # not used in apply_month_plan


class _FakeDraft:
    def __init__(self, caption="Clean caption, book your free class today.",
                 day_key="2026-09-01", category="platform",
                 draft_type="feed", is_story=False):
        self.caption = caption
        self.day_key = day_key
        self.category = category
        self.draft_type = draft_type
        self.is_story = is_story
        self.id = "draft_fake_001"
        self.creative_public_url = "https://cdn.example.com/img.jpg"
        self.platform = "instagram"


_alerts_fired = []


def _fake_alert(msg):
    _alerts_fired.append(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_good_plan_rows():
    """Build 10 distinct clean rows that grade >= 90.

    Caption requirements for a clean grade:
    - First line <= 125 chars (no hook_too_long soft flag)
    - Total >= 150 chars (no thin_caption or median-length penalty)
    - Contains a booking ask (no no_ask soft flag or path_to_join penalty)
    - No banned dashes (no caption_craft hard block)
    - No athlete-avatar language (no right_audience penalty)
    - Distinct captions (no consistency dup penalty)
    """
    pillars = ["platform", "doctrine", "b2b", "podcast", "summit"]
    rows = []
    for i in range(10):
        # Short hook (< 125 chars), varied per row to avoid dup hashes
        hook = f"Feeling stuck at {i + 30}? Real results start here."
        body = (
            " Busy parents and working professionals thrive in our 30-minute format. "
            "No prior experience needed. Every class is beginner friendly. "
            f"Get started today and book your free intro session. #{i}"
        )
        cap = hook + body
        rows.append({
            "gym_id": "testgym",
            "post_date": f"2026-09-{(i + 1):02d}",
            "caption": cap,
            "pillar": pillars[i % len(pillars)],
            "category": pillars[i % len(pillars)],
            "format": "feed",
            "account": "instagram",
            "status": "pending",
            "vision_derived": True,
            "media_url": "https://cdn.example.com/img.jpg",
            "template_id": "tmpl_A",
            "media_kind": "photo",
        })
    return rows


def _make_bad_plan_rows():
    """Build rows that permanently fail grading (em-dash violations)."""
    return [
        {
            "gym_id": "testgym",
            "post_date": f"2026-09-{(i + 1):02d}",
            "caption": "Join us—reach your goals today.",   # em-dash: hard violation
            "pillar": "platform",
            "category": "platform",
            "format": "feed",
            "account": "instagram",
            "status": "pending",
            "vision_derived": False,
            "media_url": "https://stockphotos.com/img.jpg",
            "template_id": "tmpl_A",
            "media_kind": "photo",
        }
        for i in range(5)
    ]


# ---------------------------------------------------------------------------
# Test 1: AGENT_CALENDAR_GRADE=ON, plan grades >= 90 -> stages normally
# ---------------------------------------------------------------------------

def test_gate_on_good_plan_stages(monkeypatch):
    """When grade >= 90, apply_month_plan inserts rows and returns ok=True."""
    import agent.config as cfg
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")
    assert cfg.calendar_grade_enabled()

    from agent import real_month_planner as rmp

    good_rows = _make_good_plan_rows()
    store = _FakeStore()
    alerts = []

    # Patch: to_calendar_rows returns our good_rows directly
    monkeypatch.setattr(rmp, "to_calendar_rows", lambda drafts, key: good_rows)
    # Patch: _mirror._demo to never flag as demo
    from agent import real_calendar_mirror as _mirror
    monkeypatch.setattr(_mirror._demo, "is_demo_draft_id", lambda x: False)
    monkeypatch.setattr(_mirror, "_row_source_id", lambda d: "")
    # Patch: ops_alerts.alert
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda m: alerts.append(m))

    result = rmp.apply_month_plan("testgym", [], store)

    assert result["ok"] is True, f"Expected ok=True but got: {result}"
    assert not alerts, f"No alert should fire for a passing plan, got: {alerts}"


# ---------------------------------------------------------------------------
# Test 2: AGENT_CALENDAR_GRADE=ON, plan fails then remediation fixes it -> stages
# ---------------------------------------------------------------------------

def test_gate_on_remediation_fixes_plan(monkeypatch):
    """Remediation can fix a marginal plan within 4 passes."""
    import agent.config as cfg
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")

    from agent import real_month_planner as rmp
    from agent.calendar_grade import grade_month, A_THRESHOLD

    # We will track how many grade calls are made and return failing then passing
    call_count = [0]
    good_rows = _make_good_plan_rows()

    def fake_grade(rows, profile="GYM", quotas=None):
        from agent.calendar_grade import CalendarGrade, BANDS
        call_count[0] += 1
        # First call: fail; second call: pass
        if call_count[0] == 1:
            return CalendarGrade(total=70, letter="C", scores={
                "consistency": 20, "content_mix": 10, "caption_craft": 10,
                "visual_match": 10, "right_audience": 10, "path_to_join": 10,
            }, defects=[("path_to_join", "", "not enough booking asks")])
        return CalendarGrade(total=92, letter="A", scores={
            "consistency": 20, "content_mix": 20, "caption_craft": 20,
            "visual_match": 12, "right_audience": 10, "path_to_join": 10,
        }, defects=[])

    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")
    import agent.calendar_grade as cg
    monkeypatch.setattr(cg, "grade_month", fake_grade)

    from agent import real_month_planner as rmp2
    monkeypatch.setattr(rmp2, "to_calendar_rows", lambda drafts, key: good_rows)
    from agent import real_calendar_mirror as _mirror
    monkeypatch.setattr(_mirror._demo, "is_demo_draft_id", lambda x: False)
    monkeypatch.setattr(_mirror, "_row_source_id", lambda d: "")

    alerts = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda m: alerts.append(m))

    store = _FakeStore()
    result = rmp2.apply_month_plan("testgym", [], store)

    assert result["ok"] is True, f"Expected ok=True after remediation, got: {result}"
    assert call_count[0] == 2, f"Expected 2 grade calls (1 fail + 1 pass), got {call_count[0]}"
    assert not alerts


# ---------------------------------------------------------------------------
# Test 3: AGENT_CALENDAR_GRADE=ON, 4 passes can't fix it -> NOT staged, one alert
# ---------------------------------------------------------------------------

def test_gate_on_persistent_fail_blocks_staging(monkeypatch):
    """When all 4 remediation passes still score < 90, staging is blocked."""
    import agent.config as cfg
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")

    from agent import real_month_planner as rmp
    import agent.calendar_grade as cg

    call_count = [0]

    def always_fail(rows, profile="GYM", quotas=None):
        from agent.calendar_grade import CalendarGrade
        call_count[0] += 1
        return CalendarGrade(total=60, letter="D", scores={
            "consistency": 20, "content_mix": 10, "caption_craft": 0,
            "visual_match": 10, "right_audience": 10, "path_to_join": 10,
        }, defects=[("caption_craft", "", "banned_dash violation")])

    monkeypatch.setattr(cg, "grade_month", always_fail)

    good_rows = _make_good_plan_rows()
    monkeypatch.setattr(rmp, "to_calendar_rows", lambda drafts, key: good_rows)
    from agent import real_calendar_mirror as _mirror
    monkeypatch.setattr(_mirror._demo, "is_demo_draft_id", lambda x: False)
    monkeypatch.setattr(_mirror, "_row_source_id", lambda d: "")

    alerts = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda m: alerts.append(m))

    store = _FakeStore()
    result = rmp.apply_month_plan("testgym", [], store)

    assert result["ok"] is False, f"Expected ok=False when plan can't be fixed, got: {result}"
    assert "calendar grade gate" in result.get("reason", ""), (
        f"Expected 'calendar grade gate' in reason, got: {result.get('reason')}"
    )
    assert len(alerts) == 1, f"Expected exactly 1 alert, got {len(alerts)}: {alerts}"
    assert "NOT STAGING" in alerts[0], f"Expected 'NOT STAGING' in alert, got: {alerts[0]}"
    # grade_month called: 1 initial + 4 remediation = 5 total
    assert call_count[0] == 5, f"Expected 5 grade calls (1+4), got {call_count[0]}"
    # Store must NOT have been written to
    assert not store.inserted, "Store should not be written when plan fails gate"


# ---------------------------------------------------------------------------
# Test 4: AGENT_CALENDAR_GRADE=OFF -> no grade check, stages regardless
# ---------------------------------------------------------------------------

def test_gate_off_skips_grading(monkeypatch):
    """When the flag is OFF, apply_month_plan stages without any grade check."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "false")

    import agent.config as cfg
    assert not cfg.calendar_grade_enabled()

    from agent import real_month_planner as rmp
    import agent.calendar_grade as cg

    grade_called = [0]

    def spy_grade(*a, **kw):
        grade_called[0] += 1
        from agent.calendar_grade import CalendarGrade
        return CalendarGrade(total=0, letter="F", scores={}, defects=[])

    monkeypatch.setattr(cg, "grade_month", spy_grade)

    bad_rows = _make_bad_plan_rows()
    monkeypatch.setattr(rmp, "to_calendar_rows", lambda drafts, key: bad_rows)
    from agent import real_calendar_mirror as _mirror
    monkeypatch.setattr(_mirror._demo, "is_demo_draft_id", lambda x: False)
    monkeypatch.setattr(_mirror, "_row_source_id", lambda d: "")

    alerts = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda m: alerts.append(m))

    store = _FakeStore()
    result = rmp.apply_month_plan("testgym", [], store)

    assert result["ok"] is True, f"Expected ok=True when flag is OFF, got: {result}"
    assert grade_called[0] == 0, f"grade_month should not be called when flag is OFF, called {grade_called[0]} times"
    assert not alerts
