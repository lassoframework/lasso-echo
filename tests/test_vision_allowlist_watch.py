"""tests/test_vision_allowlist_watch.py — audit item 5, 2026-08-31.

AGENT_VISION_GYMS said `district_h,eng,gritx,topfuel`. The month's vision-spend ledger
said crossfitreverb30b5b2 (173 calls) and hillcountry (35) — neither on the allowlist —
while district_h spent nothing at all. The Google-Drive staging lane runs
vision.analyze_and_store without ever consulting the allowlist, so the env value and the
actual spend had been diverging silently for weeks.

These tests pin the watchdog that makes that impossible to miss again: BOTH directions
of the drift are reported, the alert dedupes per month, and the whole thing stays
read-only (it never arms or disarms a gym — that ruling is Blake's).

Fully offline: injected ledger, injected alert, injected dedupe store.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.jobs import vision_allowlist_watch as watch  # noqa: E402

LIVE_ALLOWLIST = {"district_h", "eng", "gritx", "topfuel"}
LIVE_SPEND = {"gritx": 400, "topfuel": 23, "eng": 288,
              "crossfitreverb30b5b2": 173, "hillcountry": 35}


class _Seen(dict):
    def get(self, key):
        return dict.get(self, key)

    def set(self, key, value):
        self[key] = value


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.delenv("AGENT_VISION_ALLOWLIST_WATCH", raising=False)


# ---- 1. the ledger read --------------------------------------------------------

def test_spend_by_gym_reads_the_months_ledger_keys():
    kv = lambda: [                                        # noqa: E731
        ("vision_spend_eng_2026-08", "288"),
        ("vision_spend_gritx_2026-08", "400"),
        ("vision_spend_eng_2026-07", "12"),               # a different month
        ("vision_spend_topfuel_2026-08", "0"),            # zero is not spending
        ("vision_budget_alarm_gritx_2026-08", "1"),       # not a spend key
    ]
    assert watch.spend_by_gym("2026-08", kv_iter=kv) == {"eng": 288, "gritx": 400}


def test_a_gym_key_containing_underscores_survives_parsing():
    kv = lambda: [("vision_spend_district_h_2026-08", "5")]   # noqa: E731
    assert watch.spend_by_gym("2026-08", kv_iter=kv) == {"district_h": 5}


# ---- 2. both directions of the drift -------------------------------------------

def test_drift_reports_the_live_2026_08_31_state():
    unarmed, idle = watch.drift(LIVE_ALLOWLIST, LIVE_SPEND)
    assert unarmed == ["crossfitreverb30b5b2", "hillcountry"]
    assert idle == ["district_h"]


def test_no_drift_when_env_and_spend_agree():
    assert watch.drift({"eng"}, {"eng": 10}) == ([], [])


def test_alert_names_both_directions_and_the_call_counts():
    seen = []
    out = watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST, spending=LIVE_SPEND,
                    alert=seen.append, seen=_Seen())
    assert out["analyzing_unarmed"] == ["crossfitreverb30b5b2", "hillcountry"]
    assert out["armed_idle"] == ["district_h"]
    assert len(seen) == 1
    msg = seen[0]
    assert "crossfitreverb30b5b2 (173 calls)" in msg
    assert "district_h" in msg
    assert "AGENT_VISION_GYMS=" in msg


def test_no_alert_when_there_is_no_drift():
    seen = []
    out = watch.run(month="2026-08", allowlist={"eng"}, spending={"eng": 10},
                    alert=seen.append, seen=_Seen())
    assert seen == []
    assert out["ok"] is True


# ---- 3. read-only, deduped, and safe --------------------------------------------

def test_the_same_drift_alerts_once_per_month():
    seen = []
    store = _Seen()
    for _ in range(4):
        watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST, spending=LIVE_SPEND,
                  alert=seen.append, seen=store)
    assert len(seen) == 1


def test_a_changed_drift_set_alerts_again():
    seen = []
    store = _Seen()
    watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST, spending=LIVE_SPEND,
              alert=seen.append, seen=store)
    watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST,
              spending={**LIVE_SPEND, "piercefitness": 9},
              alert=seen.append, seen=store)
    assert len(seen) == 2


def test_the_watch_never_changes_the_allowlist(monkeypatch):
    """It reports; Blake rules. Arming a gym here would spend money nobody approved."""
    monkeypatch.setenv("AGENT_VISION_GYMS", "eng")
    watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST, spending=LIVE_SPEND,
              alert=lambda m: None, seen=_Seen())
    assert config.vision_gyms() == {"eng"}


def test_an_alert_failure_never_raises():
    def boom(_msg):
        raise RuntimeError("slack down")

    out = watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST, spending=LIVE_SPEND,
                    alert=boom, seen=_Seen())
    assert out["ok"] is True


def test_flag_defaults_on():
    assert config.vision_allowlist_watch_enabled() is True


def test_flag_off_is_a_true_noop(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_ALLOWLIST_WATCH", "false")
    seen = []
    out = watch.run(month="2026-08", allowlist=LIVE_ALLOWLIST, spending=LIVE_SPEND,
                    alert=seen.append, seen=_Seen())
    assert out["ok"] is False
    assert seen == []
