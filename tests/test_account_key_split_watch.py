"""
account_key_split_watch tests (agent/account_key_split_watch.py), fully offline.

THE INCIDENT THIS PINS (Swift River CrossFit + CrossFit Sunnyside, live 2026-08-31).
Both gyms onboarded the same day and ended up carrying TWO account keys each:

    portal token row : swiftrivercrossfite5c9db   0 calendar rows
    content + Zernio : swiftrivercrossfitd23567   14 pending rows

Every portal-side read saw an empty gym; every content-side lane built a month nobody
could see. Neither side errored, neither alerted, and both gyms would simply never have
posted. Crucially, THREE existing detectors all graded them healthy:

  * account_key_reconcile  — its idempotency rule returns any non-collided current key
    verbatim, so the split key grades OK.
  * account_key_doctor     — asks only "does this base resolve to one live gym". Both
    keys do, to the SAME gym. That IS the split, and it reads as fine.
  * onboarding_watch       — compares the portal token key to the INTAKE key. They
    agreed with each other, so key_mismatch never fired.

So the tests below pin the one question none of them ask: does the key the PORTAL
recorded actually own this gym's content?
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key_split_watch as sw  # noqa: E402


# The live shape, verbatim, so a regression reads as the real incident.
SWIFT_UUID = "e5c9db81-110d-4308-9bb7-3ad3bf563a0b"
SUNNY_UUID = "f574c06c-498a-45f8-a599-b2a8863fadfb"
ENG_UUID = "6ee04ee4-13a5-47db-8416-7b8ee3e61ab8"


def _resolver(mapping):
    """base -> gym uuid, mirroring SupabaseCalendarStore.resolve_gym_uuid's contract:
    an unknown base resolves to None (never a guess)."""
    return lambda base: mapping.get(base)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_SPLIT_WATCH", "true")


class _FakeDb:
    def __init__(self, kv=None):
        self.kv = dict(kv or {})

    def kv_get(self, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


# ---- the classifier ---------------------------------------------------------------

def test_the_live_swift_river_split_is_caught():
    """The exact live shape: the portal key owns nothing, another key for the SAME gym
    owns the month. This must be reported, or the gym goes quiet forever."""
    roster = [(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River CrossFit")]
    counts = {"swiftrivercrossfitd23567": 14}
    out = sw.build_report(roster, counts, _resolver({
        "swiftrivercrossfitd23567": SWIFT_UUID,
        "swiftrivercrossfite5c9db": SWIFT_UUID,
    }))
    assert len(out) == 1
    f = out[0]
    assert f["reason"] == sw.REASON_ORPHAN_PORTAL_KEY
    assert f["portal_key"] == "swiftrivercrossfite5c9db"
    assert f["content_key"] == "swiftrivercrossfitd23567"
    assert f["portal_rows"] == 0 and f["content_rows"] == 14
    # The alert must carry the gym name AND a runnable command, not just a complaint.
    text = sw._alert_text(f)  # noqa: SLF001
    assert "Swift River CrossFit" in text
    assert "account-key-reconcile" in text and SWIFT_UUID in text


def test_a_healthy_gym_is_never_flagged():
    """A gym whose portal key owns its own calendar is not split. No rival key, no
    finding — a watchdog that cries wolf on the healthy fleet gets muted and then it
    protects nothing."""
    roster = [(ENG_UUID, "eng", "ENG")]
    counts = {"eng": 229}
    assert sw.build_report(roster, counts, _resolver({"eng": ENG_UUID})) == []


def test_both_keys_live_is_reported_as_the_worse_case():
    """When BOTH keys own rows the month is being built twice and whichever key the
    publisher iterates decides what posts. That must NOT be reported as a simple
    orphan, because the blind repoint that fixes an orphan would strand real data."""
    roster = [(SUNNY_UUID, "crossfitsunnysidef574c0", "CrossFit Sunnyside")]
    counts = {"crossfitsunnysidef574c0": 8, "crossfitsunnyside2616ac": 20}
    out = sw.build_report(roster, counts, _resolver({
        "crossfitsunnysidef574c0": SUNNY_UUID,
        "crossfitsunnyside2616ac": SUNNY_UUID,
    }))
    assert len(out) == 1 and out[0]["reason"] == sw.REASON_TWO_LIVE_KEYS
    assert out[0]["portal_rows"] == 8 and out[0]["content_rows"] == 20
    assert "Do NOT re-point blind" in out[0]["fix"]


def test_another_gyms_key_is_never_mistaken_for_a_split():
    """Attribution is by RESOLVED gym uuid, never a name-prefix guess. Two gyms sharing
    a name stem must not be reported as one gym split in two — that would send a human
    to repoint a key onto a DIFFERENT tenant's calendar."""
    roster = [(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River CrossFit")]
    counts = {"swiftrivercrossfitnorth9911aa": 40}          # a genuinely different gym
    out = sw.build_report(roster, counts, _resolver({
        "swiftrivercrossfitnorth9911aa": "some-other-gym-uuid",
    }))
    assert out == []


def test_an_unresolvable_content_key_is_never_attributed():
    """A key the resolver cannot place belongs to nobody. Attributing it would invent a
    split; the honest answer is silence."""
    roster = [(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River CrossFit")]
    out = sw.build_report(roster, {"zzclaudetest0831": 42}, _resolver({}))
    assert out == []


def test_an_empty_rival_key_is_not_a_split():
    """A key that exists but owns ZERO rows is not holding the gym's content hostage."""
    roster = [(ENG_UUID, "eng", "ENG")]
    out = sw.build_report(roster, {"eng": 229, "engold": 0},
                          _resolver({"eng": ENG_UUID, "engold": ENG_UUID}))
    assert out == []


# ---- the sweep --------------------------------------------------------------------

def test_flag_off_is_a_total_noop(monkeypatch):
    monkeypatch.delenv("AGENT_ACCOUNT_KEY_SPLIT_WATCH", raising=False)
    alerts = []
    out = sw.run(roster=[(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River")],
                 content_counts={"swiftrivercrossfitd23567": 14},
                 resolve_uuid=_resolver({"swiftrivercrossfitd23567": SWIFT_UUID}),
                 alert=alerts.append, db=_FakeDb())
    assert out["enabled"] is False and out["findings"] == [] and alerts == []


def test_one_alert_per_gym_per_day(armed):
    """A nightly sweep on a still-split gym must not storm the channel."""
    alerts = []
    db = _FakeDb()
    args = dict(roster=[(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River")],
                content_counts={"swiftrivercrossfitd23567": 14},
                resolve_uuid=_resolver({"swiftrivercrossfitd23567": SWIFT_UUID}),
                alert=alerts.append, db=db, today="2026-08-31")
    sw.run(**args)
    sw.run(**args)
    sw.run(**args)
    assert len(alerts) == 1, f"expected one alert per day, got {alerts}"
    # a new day re-arms it (the split is still real and still needs fixing)
    args["today"] = "2026-09-01"
    sw.run(**args)
    assert len(alerts) == 2


def test_a_reader_fault_makes_the_watch_a_noop_not_a_false_alarm(armed):
    """A resolver that raises must never be read as "nothing owns this key", which would
    manufacture splits across the whole fleet the first time Supabase hiccuped."""
    def _boom(_base):
        raise RuntimeError("supabase down")

    alerts = []
    out = sw.run(roster=[(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River")],
                 content_counts={"swiftrivercrossfitd23567": 14},
                 resolve_uuid=_boom, alert=alerts.append, db=_FakeDb())
    assert out["findings"] == [] and alerts == []


def test_an_alert_failure_never_blocks_the_remaining_gyms(armed):
    """One gym's alert failing must not swallow the rest of the fleet's findings."""
    calls = []

    def _flaky(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise RuntimeError("slack down")

    out = sw.run(
        roster=[(SWIFT_UUID, "swiftrivercrossfite5c9db", "Swift River"),
                (SUNNY_UUID, "crossfitsunnysidef574c0", "CrossFit Sunnyside")],
        content_counts={"swiftrivercrossfitd23567": 14, "crossfitsunnyside2616ac": 20},
        resolve_uuid=_resolver({"swiftrivercrossfitd23567": SWIFT_UUID,
                                "crossfitsunnyside2616ac": SUNNY_UUID}),
        alert=_flaky, db=_FakeDb())
    assert len(out["findings"]) == 2
    assert len(calls) == 2                       # both attempted
    assert out["alerted"] == [SUNNY_UUID]        # only the one that landed is stamped


def test_the_watch_never_writes_a_key(armed, monkeypatch):
    """HARD RAIL: this module reports, it never repoints. Repointing stays a deliberate
    human act through account_key_reconcile, whose writer is itself flag-gated and
    refuses to strand data. Any import of a writer here is a bug."""
    import inspect
    src = inspect.getsource(sw)
    for forbidden in ("gym_upsert", "requests.patch", "requests.post", ".patch(",
                      "set_status", "insert_rows"):
        assert forbidden not in src, f"the split watch must never write ({forbidden})"
