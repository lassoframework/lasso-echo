"""The REPEAT gate, and the morning that caused it.

2026-09-04, Blake, looking at a screen of #echosupport: "why do i keep getting this?"

That day the fleet raised 45 ops alerts and every single one was DISTINCT -- there was no
within-day duplication to dedupe. The flood was day-over-day: three gyms with zero
connected platforms and two split account keys announced themselves at 08:03, exactly as
they had the morning before, because nothing ever asked "have I already said this?".

These alerts are all real work. The noise gate (test_alert_noise_gate.py) correctly keeps
every one of them. This file pins the different question:

  * the FIRST telling always fires
  * an unchanged repeat inside the window does not
  * a STATE CHANGE fires immediately, without waiting the window out
  * a systemic outage is exempt -- recurrence is news, not repetition
  * the audit row is still written for a suppressed alert
  * every failure path fails OPEN
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import alert_repeat as ar  # noqa: E402

# ---- verbatim production lines from 2026-09-04 ----------------------------------------

NOT_CONNECTED = (
    "crossfitnewtown: not set up to post (not_connected). a Zernio profile exists but "
    "ZERO platforms are connected. Send the gym its connect link "
    "(python -m agent intake-link --account <key>).")

HEARTBEAT_0904 = ("no daily draft heartbeat for lasso_ig by 10:00 ET on 2026-09-04. "
                  "The scheduled run may have missed; check the listener.")
HEARTBEAT_0905 = ("no daily draft heartbeat for lasso_ig by 10:00 ET on 2026-09-05. "
                  "The scheduled run may have missed; check the listener.")
HEARTBEAT_OTHER_ACCOUNT = (
    "no daily draft heartbeat for lasso_fb by 10:00 ET on 2026-09-04. "
    "The scheduled run may have missed; check the listener.")

GRADE_DROP_75 = ("calendar grade DROPPED: topfuel forward book went 82 -> 75 (C) since "
                 "the last run.")
GRADE_DROP_70 = ("calendar grade DROPPED: topfuel forward book went 82 -> 70 (C) since "
                 "the last run.")

SYSTEMIC = ("gym topfuel is STALLED at 'calendar_unreadable': the shared calendar could "
            "not be read.")


class _Store:
    """A kv the gate can claim slots in, with a clock the test drives."""

    def __init__(self):
        self.kv = {}

    def kv_get(self, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


def _arm_gate(monkeypatch, hours=72):
    from agent import config
    monkeypatch.setattr(config, "alert_repeat_gate_enabled", lambda: True)
    monkeypatch.setattr(config, "alert_repeat_window_hours", lambda: float(hours))


# ---- the fingerprint ------------------------------------------------------------------

def test_the_same_condition_on_a_different_day_is_one_fingerprint():
    """The heartbeat line carries tomorrow's date but names the same unfixed condition.
    Keying on raw text would make this gate a no-op for the alerts that repeat most."""
    assert ar.fingerprint(HEARTBEAT_0904) == ar.fingerprint(HEARTBEAT_0905)


def test_a_different_account_is_a_different_fingerprint():
    """lasso_ig and lasso_fb both missing is two facts, not one repeated."""
    assert ar.fingerprint(HEARTBEAT_0904) != ar.fingerprint(HEARTBEAT_OTHER_ACCOUNT)


def test_a_changed_number_is_a_different_fingerprint():
    """The invariant that makes suppression safe. Only clock tokens are normalised --
    every count, grade and id stays in the fingerprint, so a state change is never
    mistaken for a repeat."""
    assert ar.fingerprint(GRADE_DROP_75) != ar.fingerprint(GRADE_DROP_70)


def test_fingerprint_ignores_whitespace_and_case_only():
    assert ar.fingerprint(NOT_CONNECTED) == ar.fingerprint("  " + NOT_CONNECTED.upper())


# ---- the gate itself ------------------------------------------------------------------

def test_the_first_occurrence_always_fires(monkeypatch):
    _arm_gate(monkeypatch)
    assert ar.should_fire(NOT_CONNECTED, db=_Store()) is True


def test_an_unchanged_repeat_inside_the_window_does_not_fire(monkeypatch):
    _arm_gate(monkeypatch)
    store, t0 = _Store(), datetime(2026, 9, 4, 8, 3, tzinfo=timezone.utc)
    assert ar.should_fire(NOT_CONNECTED, now=t0, db=store) is True
    assert ar.should_fire(NOT_CONNECTED, now=t0 + timedelta(days=1), db=store) is False
    assert ar.should_fire(NOT_CONNECTED, now=t0 + timedelta(days=2), db=store) is False


def test_the_window_lapsing_re_fires_it(monkeypatch):
    """72h is quiet, not silent: a condition nobody acted on is still raised twice a
    week, so it can never fall off the radar entirely."""
    _arm_gate(monkeypatch)
    store, t0 = _Store(), datetime(2026, 9, 4, 8, 3, tzinfo=timezone.utc)
    assert ar.should_fire(NOT_CONNECTED, now=t0, db=store) is True
    assert ar.should_fire(NOT_CONNECTED, now=t0 + timedelta(hours=71), db=store) is False
    assert ar.should_fire(NOT_CONNECTED, now=t0 + timedelta(hours=73), db=store) is True


def test_a_state_change_fires_immediately_without_waiting_the_window(monkeypatch):
    """Blake's own condition on the gate: it re-fires the moment the state changes."""
    _arm_gate(monkeypatch)
    store, t0 = _Store(), datetime(2026, 9, 4, 8, 51, tzinfo=timezone.utc)
    assert ar.should_fire(GRADE_DROP_75, now=t0, db=store) is True
    assert ar.should_fire(GRADE_DROP_75, now=t0 + timedelta(hours=2), db=store) is False
    assert ar.should_fire(GRADE_DROP_70, now=t0 + timedelta(hours=2), db=store) is True


def test_the_same_condition_tomorrow_is_still_suppressed(monkeypatch):
    """The heartbeat miss is the alert this gate exists for: identical condition, new
    date in the text, every single morning."""
    _arm_gate(monkeypatch)
    store, t0 = _Store(), datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)
    assert ar.should_fire(HEARTBEAT_0904, now=t0, db=store) is True
    assert ar.should_fire(HEARTBEAT_0905, now=t0 + timedelta(days=1), db=store) is False


def test_gate_off_never_suppresses_anything(monkeypatch):
    """Ships inert: byte-for-byte today's behaviour until it is armed."""
    from agent import config
    monkeypatch.setattr(config, "alert_repeat_gate_enabled", lambda: False)
    store = _Store()
    for _ in range(5):
        assert ar.should_fire(NOT_CONNECTED, db=store) is True


def test_a_broken_store_fails_open(monkeypatch):
    """A dedupe that swallows a real break is far worse than a duplicate line."""
    _arm_gate(monkeypatch)

    class _Broken:
        def kv_get(self, key, default=""):
            raise RuntimeError("store down")

        def kv_set(self, key, value):
            raise RuntimeError("store down")

    assert ar.should_fire(NOT_CONNECTED, db=_Broken()) is True


def test_an_unparseable_stamp_fails_open(monkeypatch):
    _arm_gate(monkeypatch)
    store = _Store()
    store.kv[ar._KEY_PREFIX + ar.fingerprint(NOT_CONNECTED)] = "not-a-timestamp"
    assert ar.should_fire(NOT_CONNECTED, db=store) is True


def test_a_naive_stamp_is_read_as_utc_not_crashed_on(monkeypatch):
    _arm_gate(monkeypatch)
    store, t0 = _Store(), datetime(2026, 9, 4, 8, 3, tzinfo=timezone.utc)
    store.kv[ar._KEY_PREFIX + ar.fingerprint(NOT_CONNECTED)] = "2026-09-04T08:00:00"
    assert ar.should_fire(NOT_CONNECTED, now=t0, db=store) is False


def test_a_bad_window_value_falls_back_rather_than_silencing_forever(monkeypatch):
    """A typo in AGENT_ALERT_REPEAT_WINDOW_HOURS must not mute the fleet."""
    from agent import config
    monkeypatch.setenv("AGENT_ALERT_REPEAT_WINDOW_HOURS", "not-a-number")
    assert config.alert_repeat_window_hours() == 72.0
    monkeypatch.setenv("AGENT_ALERT_REPEAT_WINDOW_HOURS", "0")
    assert config.alert_repeat_window_hours() == 72.0


# ---- wired into ops_alerts.alert -------------------------------------------------------

class _Poster:
    def __init__(self):
        self.posts = []

    def post_notice(self, text):
        self.posts.append(text)
        return {"ok": True, "ts": "1.0"}

    def _chat_post(self, **kw):
        return {"ok": True}


def _arm_alert(monkeypatch, store):
    from agent import config, db as db_mod
    monkeypatch.setattr(config, "ops_alerts_enabled", lambda: True)
    monkeypatch.setattr(config, "ops_alerts_noise_filter_enabled", lambda: True)
    monkeypatch.setattr(config, "ops_fix_triage_enabled", lambda: False)
    monkeypatch.setattr(db_mod, "kv_get", store.kv_get)
    monkeypatch.setattr(db_mod, "kv_set", store.kv_set)
    _arm_gate(monkeypatch)


def test_alert_posts_once_then_goes_quiet(monkeypatch):
    from agent import ops_alerts as oa
    store = _Store()
    _arm_alert(monkeypatch, store)
    p = _Poster()
    oa.alert(NOT_CONNECTED, poster=p)
    oa.alert(NOT_CONNECTED, poster=p)
    oa.alert(NOT_CONNECTED, poster=p)
    assert len(p.posts) == 1, "the same unchanged condition must be told once"


def test_alert_still_writes_the_audit_row_for_a_suppressed_line(monkeypatch):
    """The record is never what gets dropped -- only the notification."""
    from agent import ops_alerts as oa, db as db_mod
    store = _Store()
    _arm_alert(monkeypatch, store)
    rows = []
    monkeypatch.setattr(db_mod, "audit",
                        lambda *a, **k: rows.append((a, k)))
    p = _Poster()
    oa.alert(NOT_CONNECTED, poster=p)
    oa.alert(NOT_CONNECTED, poster=p)
    assert len(p.posts) == 1
    assert len(rows) == 2, "both tellings stay on the permanent record"


def test_a_systemic_alert_is_exempt(monkeypatch):
    """A shared-dependency outage recurring days later is news, not repetition. Its
    fan-out is already collapsed to one cross-post per 30-minute window elsewhere."""
    from agent import ops_alerts as oa
    store = _Store()
    _arm_alert(monkeypatch, store)
    p = _Poster()
    oa.alert(SYSTEMIC, poster=p)
    oa.alert(SYSTEMIC, poster=p)
    assert len(p.posts) == 2


def test_a_forced_alert_bypasses_the_repeat_gate(monkeypatch):
    """force is for watchdogs carrying their own flag; they bypass every gate."""
    from agent import ops_alerts as oa
    store = _Store()
    _arm_alert(monkeypatch, store)
    p = _Poster()
    oa.alert(NOT_CONNECTED, poster=p, force=True)
    oa.alert(NOT_CONNECTED, poster=p, force=True)
    assert len(p.posts) == 2


def test_a_changed_alert_is_heard_the_same_day(monkeypatch):
    from agent import ops_alerts as oa
    store = _Store()
    _arm_alert(monkeypatch, store)
    p = _Poster()
    oa.alert(GRADE_DROP_75, poster=p)
    oa.alert(GRADE_DROP_75, poster=p)
    oa.alert(GRADE_DROP_70, poster=p)
    assert len(p.posts) == 2


def test_a_gate_fault_never_eats_an_alert(monkeypatch):
    from agent import ops_alerts as oa, alert_repeat as repeat
    store = _Store()
    _arm_alert(monkeypatch, store)
    monkeypatch.setattr(repeat, "should_fire",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    p = _Poster()
    oa.alert(NOT_CONNECTED, poster=p)
    assert len(p.posts) == 1, "a broken repeat gate must fail toward posting"


def test_noise_is_still_dropped_and_never_charged_a_repeat_slot(monkeypatch):
    """Ordering invariant: the noise gate runs first, so a NOISE line never consumes a
    slot it did not need."""
    from agent import ops_alerts as oa
    store = _Store()
    _arm_alert(monkeypatch, store)
    noise = ("GBP month sweep: crossfitreverb30b5b2 is connected to Google Business but "
             "its month could not be planned (nothing planned (no A+ captions or "
             "media)). Nothing was written and nothing was fabricated.")
    p = _Poster()
    assert oa.alert(noise, poster=p) is None
    assert p.posts == []
    assert store.kv == {}, "a dropped noise line must not claim a repeat slot"
