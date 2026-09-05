"""
B5 — the photo repeats the nightly sweep is RIGHT to refuse, and WRONG to swallow.

MEASURED LIVE 2026-09-05, with agent/media_guard.py armed and
agent/jobs/media_repeat_sweep.py running nightly:

  zanshin   5 photo repeats, dates_fixed 0, approved_left 5, small library
            -41.jpg on 09-03 (LIVE), 09-08 (approved), 09-09 (approved):
            the SAME photo three times inside seven days
  lasso    28 photo repeats, dates_fixed 0, small library

The stage-time guard works: zero cross-day repeats among the rows created after it
deployed. What is left is rows it cannot see, and the sweep is correctly forbidden
from touching them -- an APPROVED row's media is never swapped (the gym approved
that exact card) and a small library is never handed fabricated media. Both
refusals are right. Counting them as an integer on a stdout table is the defect:
approved_left has existed since the job was written and never reached a person.

Note: agent/jobs/media_repeat_sweep.py had NO test file at all before this one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs import media_repeat_sweep as mrs   # noqa: E402


# The exact shape sweep_gym returns for zanshin on production, 2026-09-05.
def _zanshin_result():
    return {
        "gym": "zanshinfitness630e22", "photos_repeated": 5, "dates_fixed": 0,
        "rows_repointed": 0, "stories_reburned": 0, "approved_left": 5,
        "small_library": True,
        "detail": [
            "20260830T211442Z_Zanshin_Fitness-205.jpg 2026-09-12: APPROVED "
            "duplicate (left; the gym approved this card)",
            "20260830T211442Z_Zanshin_Fitness-41.jpg 2026-09-03: LIVE row also "
            "carries it (left)",
            "20260830T211442Z_Zanshin_Fitness-41.jpg 2026-09-08: APPROVED "
            "duplicate (left; the gym approved this card)",
            "20260830T211442Z_Zanshin_Fitness-41.jpg 2026-09-09: APPROVED "
            "duplicate (left; the gym approved this card)",
        ],
    }


def _clean_result():
    return {"gym": "topfuel", "photos_repeated": 0, "dates_fixed": 0,
            "rows_repointed": 0, "stories_reburned": 0, "approved_left": 0,
            "small_library": False, "detail": []}


class _KV:
    def __init__(self, durable=True):
        self.data = {}
        self._durable = durable

    def kv_get(self, key, default=""):
        return self.data.get(key, default)

    def kv_set(self, key, value):
        self.data[key] = str(value)

    def kv_is_durable(self):
        return self._durable


@pytest.fixture()
def _report_on(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_REPORT", "true")


# ---- the sentence itself (pure) --------------------------------------------

def test_a_clean_gym_says_nothing():
    assert mrs.unfixable_report(_clean_result()) == ""
    assert mrs.unfixable_report({}) == ""


def test_the_report_names_the_approved_side_and_asks_for_a_decision():
    msg = mrs.unfixable_report(_zanshin_result())
    assert "5 photo(s) repeat" in msg
    assert "APPROVED" in msg
    assert "a person has to decide" in msg
    assert "no approval was changed" in msg


def test_a_small_library_is_named_as_the_cause_with_what_to_do():
    msg = mrs.unfixable_report(_zanshin_result())
    assert "fewer usable photos than it has posting days" in msg
    assert "Drive" in msg or "upload" in msg


def test_repeats_inside_seven_days_are_called_out_separately():
    # B5's own bar. -41.jpg on 09-08 and 09-09 is one day apart: a follower sees it.
    msg = mrs.unfixable_report(_zanshin_result())
    assert "Inside 7 days" in msg
    assert "20260830T211442Z_Zanshin_Fitness-41.jpg" in msg
    assert "1 days apart" in msg


def test_a_repeat_spaced_beyond_the_window_is_not_called_urgent():
    r = _clean_result()
    r["photos_repeated"] = 1
    r["detail"] = ["photo_a.jpg 2026-09-01: APPROVED duplicate (left)",
                   "photo_a.jpg 2026-10-01: APPROVED duplicate (left)"]
    r["approved_left"] = 2
    msg = mrs.unfixable_report(r)
    assert msg and "Inside 7 days" not in msg


# ---- when it speaks --------------------------------------------------------

def test_flag_off_says_nothing_and_reads_no_kv():
    kv = _KV()
    said = []
    out = mrs.report_unfixable(_zanshin_result(), today_iso="2026-09-05",
                               alert_fn=said.append, db=kv)
    assert out == "" and said == [] and kv.data == {}


def test_flag_on_speaks_once_per_gym_per_month(_report_on):
    kv = _KV()
    said = []
    for _ in range(4):
        mrs.report_unfixable(_zanshin_result(), today_iso="2026-09-05",
                             alert_fn=said.append, db=kv)
    assert len(said) == 1, "a nightly job must not storm the channel"


def test_it_speaks_again_when_the_repeats_get_worse(_report_on):
    kv = _KV()
    said = []
    mrs.report_unfixable(_zanshin_result(), today_iso="2026-09-05",
                         alert_fn=said.append, db=kv)
    worse = _zanshin_result()
    worse["photos_repeated"] = 9
    mrs.report_unfixable(worse, today_iso="2026-09-20", alert_fn=said.append, db=kv)
    assert len(said) == 2, "getting worse is new information"


def test_an_ephemeral_kv_stays_silent_rather_than_alerting_every_night(_report_on):
    kv = _KV(durable=False)
    said = []
    mrs.report_unfixable(_zanshin_result(), today_iso="2026-09-05",
                         alert_fn=said.append, db=kv)
    assert said == [], "durable-or-silent, the repo's alert-dedup convention"


def test_an_alert_failure_never_breaks_the_sweep(_report_on):
    def _boom(_text):
        raise RuntimeError("slack is down")

    kv = _KV()
    assert mrs.report_unfixable(_zanshin_result(), today_iso="2026-09-05",
                                alert_fn=_boom, db=kv) == ""


def test_the_report_carries_no_dashes_and_never_says_vendor(_report_on):
    msg = mrs.unfixable_report(_zanshin_result())
    body = msg.replace("20260830T211442Z_Zanshin_Fitness-205.jpg", "") \
              .replace("20260830T211442Z_Zanshin_Fitness-41.jpg", "") \
              .replace("2026-09-03", "").replace("2026-09-08", "") \
              .replace("2026-09-09", "").replace("2026-09-12", "")
    assert "—" not in body and "–" not in body
    assert "vendor" not in body.lower()
