"""AGENT_CTA_VARIETY -- the craft pass's failure cutoff, and budget fairness.

MEASURED ON PRODUCTION, 2026-09-06. `remediate_forward_book` chains its repair
passes and every LLM-backed one used to share ONE deadline, first come first
served. hillcountry run with llm_budget_s=400.0 (4.4x the 90s default) returned
`craft_attempted: 18, craft_fixed: 0` -- eighteen consecutive regens, none of
which cleared the craft bar -- and then `_fix_body_sameness`, which runs LAST by
design, logged *"body repair stopped, the pass is out of LLM budget"* with
`body_pairs: 8, body_fixed: 0`. topfuel and train7164ae502 starved identically
(train716 ran 25+ straight craft failures). Body max-similarity on all three
live books measured 1.0000 before and 1.0000 after a full `grade_sweep.run()`.

Raising the budget was tried and disproved: at a 100% regen failure rate, more
budget buys more failures. The two repairs under test here are

  1. `_CRAFT_LLM_FAILURE_CAP` -- after N consecutive LLM regens clear nothing,
     the craft pass stops making LLM calls on this book (mechanics continue),
  2. `_LlmBudget` -- each LLM-backed pass draws its own sub-deadline from one
     shared clock, with a T/N floor reserved for every pass that runs later.

Every test is deterministic and offline: fake store, injected regen, injected
monotonic clock. Nothing here sleeps and nothing here calls a model.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import caption_variety as cv
from agent.jobs import grade_fix


TODAY = "2026-09-06"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _PermissiveStore:
    """Writes whatever it is handed, onto any row.

    THE TRAP THIS AVOIDS (D68): a fake that enforces the same rule as the code
    under test holds the line instead of the code, so reverting the code leaves
    the test green and the test asserts the fake. Nothing here guards anything;
    every guard asserted below has to come from `grade_fix`.
    """

    def __init__(self, rows):
        self.rows = rows

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None,
                           levers=None):
        for r in self.rows:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                if caption is not None:
                    r["caption"] = caption
                if pillar is not None:
                    r["pillar"] = pillar
                return dict(r)
        return None


class _Clock:
    """An injectable monotonic clock. The regen advances it by `per_call`
    seconds, which is what a real 6-to-8 second `_clean_draft_for_day` costs."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += float(secs)


# ---------------------------------------------------------------------------
# Synthetic vocabulary (the fixture must not accidentally score similar)
# ---------------------------------------------------------------------------

_SYL_A = ("bar", "cad", "dor", "fen", "gil", "hol", "jun", "kes", "lum", "mor",
          "nev", "orb", "pel", "quar", "rid", "sel", "tor", "vin", "wex", "yar")
_SYL_B = ("an", "el", "in", "or", "us", "ay", "ic", "om", "ur", "ex")


def _w(k):
    k = int(k)
    return _SYL_A[k % 20] + _SYL_B[(k // 20) % 10] + ("" if k < 200 else str(k // 200))


def _phrase(start, n):
    return " ".join(_w(start + j) for j in range(n))


def _hook(k, width=6):
    return _phrase(k * width, width).capitalize() + "."


def _tail(i):
    """The caption's LAST line (the one `body_text` strips), carrying the ask."""
    return (f"Note {i}. Coaches walk the room before a single rep is loaded and "
            "the whiteboard already carries your name for the day. Book your "
            "free intro class to see exactly how that works.")


def _clean_post(k, i):
    """A craft-CLEAN caption whose BODY is its hook alone."""
    return f"{_hook(k)}\n\n{_tail(i)}"


def _thin(i):
    """A craft-flagged caption `_mechanical_repair` cannot help with: too short
    (thin_caption) and ask-less, so with no approved CTA to append mechanics
    hands the caption straight back unchanged and only the LLM lane is left."""
    return f"{_hook(900 + i)}\n{_phrase(9000 + i * 4, 4)}."


def _long_clean(k):
    """A caption a regen might hand back: clean, ask-less, 150 to 500 chars."""
    body = _phrase(k * 40, 34)
    return f"{_hook(k)}\n\n{body.capitalize()}."


def _rows(specs, gym_id="reverb"):
    pillars = ("community", "education", "proof", "behind_scenes", "offer")
    return [{"id": f"row_{i}", "gym_id": gym_id, "post_date": d,
             "caption": cap, "pillar": pillars[i % 5],
             "category": pillars[i % 5], "format": "feed",
             "account": "instagram", "status": "pending"}
            for i, (d, cap) in enumerate(specs)]


def _flagged_book(n, gym_id="reverb"):
    """`n` craft-flagged days that only the LLM lane can repair."""
    return _rows([(f"2026-09-{6 + i:02d}", _thin(i)) for i in range(n)],
                 gym_id=gym_id)


def _run_craft(gym_id, rows, store, regen, deadline=None):
    return grade_fix._fix_craft(gym_id, rows, store, "GYM", regen, set(),
                                lambda m: None, deadline=deadline)


def _regen(outcomes):
    """An injected regen. `outcomes` is an iterable of bools: True hands back a
    caption that clears the craft bar, False one that cannot (too thin)."""
    calls = []
    seq = list(outcomes)

    def regen(row, avoid, avoid_category=""):
        i = len(calls)
        calls.append(str((row or {}).get("post_date") or ""))
        ok = seq[i] if i < len(seq) else seq[-1]
        return ((_long_clean(2000 + i), "education") if ok
                else (_thin(7000 + i), "education"))
    return regen, calls


# ---------------------------------------------------------------------------
# 0. The fixture is what this file claims it is
# ---------------------------------------------------------------------------

def test_the_fixture_only_leaves_the_llm_lane_open(monkeypatch):
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    cap = _thin(0)
    assert grade_fix._craft_flags(cap)                 # the day IS craft flagged
    assert grade_fix._mechanical_repair(cap, None) == cap.strip()   # mechanics: no help
    assert not grade_fix._clears_craft(_thin(7000), allow_no_ask=True)
    assert grade_fix._clears_craft(_long_clean(2000), allow_no_ask=True)


# ---------------------------------------------------------------------------
# 1. The consecutive-failure cutoff
# ---------------------------------------------------------------------------

def test_craft_stops_calling_the_llm_after_n_consecutive_failures(monkeypatch):
    """The hillcountry shape: every regen fails. 18 calls became N."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _flagged_book(18)
    regen, calls = _regen([False])
    fixed, attempted, _added = _run_craft("reverb", rows, _PermissiveStore(rows),
                                          regen)
    assert fixed == 0
    assert attempted == 18            # every flagged day is still CONSIDERED
    assert len(calls) == grade_fix._CRAFT_LLM_FAILURE_CAP == 4


def test_a_regen_success_resets_the_consecutive_failure_counter(monkeypatch):
    """The counter detects "this book cannot clear the bar", not "enough work".

    A book where the regen keeps working must never be cut off, so three
    failures followed by a success has to leave the pass free to keep going --
    forever, if it keeps succeeding. Reverting the reset makes this book stop
    at 4 calls instead of running all 20.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _flagged_book(20)
    # F F F S repeated: never four failures in a row, so never cut off.
    regen, calls = _regen([False, False, False, True] * 5)
    fixed, attempted, _added = _run_craft("reverb", rows, _PermissiveStore(rows),
                                          regen)
    assert attempted == 20
    assert len(calls) == 20           # not one day was skipped
    assert fixed == 5                 # the five successes landed


def test_mechanical_repair_keeps_running_after_the_llm_is_cut_off(monkeypatch):
    """The cutoff is about not paying for LLM calls that do not work. The free,
    deterministic lane is not rationed and must still repair the days it can.

    Days 0..5 are LLM-only (thin, ask-less, no CTA available). Days 6..9 carry
    an over-long hook, which `_mechanical_repair` fixes by moving a line break
    and no LLM call at all. Those four must land AFTER the cutoff has fired.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    # A short opening sentence followed by a long continuation on the SAME
    # line: the hook is over the band, and mechanics clears it by moving the
    # line break to the first sentence boundary. Not one word changes.
    long_hook = (_phrase(4000, 8) + ". " + _phrase(4100, 24) + ".")
    specs = [(f"2026-09-{6 + i:02d}", _thin(i)) for i in range(6)]
    specs += [(f"2026-09-{12 + i:02d}", f"{long_hook}\n\n{_tail(50 + i)}")
              for i in range(4)]
    rows = _rows(specs)
    assert all(grade_fix._craft_flags(c) for _d, c in specs)
    regen, calls = _regen([False])
    fixed, attempted, _added = _run_craft("reverb", rows, _PermissiveStore(rows),
                                          regen)
    assert len(calls) == grade_fix._CRAFT_LLM_FAILURE_CAP   # LLM conceded
    assert attempted == 10
    assert fixed == 4                 # every mechanically fixable day landed
    assert all(len(rows[6 + i]["caption"].splitlines()[0]) <= grade_fix._HOOK_MAX
               for i in range(4))


def test_the_cutoff_is_inert_with_the_flag_off(monkeypatch):
    """OFF by default = zero behaviour change, the repo's standing rule."""
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _flagged_book(18)
    regen, calls = _regen([False])
    _fixed, attempted, _added = _run_craft("reverb", rows,
                                           _PermissiveStore(rows), regen)
    assert attempted == 18
    assert len(calls) == 18           # unbudgeted, uncapped: the old behaviour


# ---------------------------------------------------------------------------
# 2. Budget fairness: the invariant
# ---------------------------------------------------------------------------

def test_every_pass_is_guaranteed_a_floor_of_total_over_pass_count(monkeypatch):
    """THE INVARIANT, at the allocator: an earlier pass that consumes the whole
    clock cannot leave a later pass with a deadline in the past."""
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    budget = grade_fix._LlmBudget(90.0)
    floor = 90.0 / len(grade_fix._LLM_PASS_ORDER)
    assert floor == 18.0

    # The craft pass asks first and is handed the budget minus the floors owed
    # to the two LLM passes after it.
    craft = budget.deadline_for("craft")
    assert craft - clock.now == 90.0 - 2 * floor == 54.0

    # It then burns the ENTIRE clock (and overruns it: a call in flight when
    # the deadline passes still finishes).
    clock.advance(200.0)
    body = budget.deadline_for("body")
    assert body > clock.now                       # NOT already expired
    assert body - clock.now >= floor
    assert grade_fix._budget_left(body)


def test_a_non_positive_budget_still_switches_the_llm_off(monkeypatch):
    """`llm_budget_s=-1` is the idiom callers use to disable the LLM entirely
    (tests/test_grade_self_fix.py). The allocator must not resurrect it."""
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    budget = grade_fix._LlmBudget(-1.0)
    for name in grade_fix._LLM_PASS_ORDER:
        assert not grade_fix._budget_left(budget.deadline_for(name))


# ---------------------------------------------------------------------------
# 3. Budget fairness, end to end through remediate_forward_book
# ---------------------------------------------------------------------------

def _starvation_book(gym_id="reverb"):
    """20 craft-flagged days whose regen SUCCEEDS (so the failure cutoff never
    fires and the craft pass really would spend everything), plus three
    craft-clean posts that share one hook verbatim -- body similarity 1.0, the
    live hillcountry / topfuel / train7164ae502 defect, and the last pass's
    only work."""
    specs = [(f"2026-09-{6 + i:02d}", _thin(i)) for i in range(20)]
    specs += [(f"2026-10-{1 + i:02d}", _clean_post(0, 700 + i)) for i in range(3)]
    return _rows(specs, gym_id=gym_id)


def _remediate(rows, store, regen, monkeypatch, clock, per_call=7.0):
    def timed(row, avoid, avoid_category=""):
        clock.advance(per_call)
        return regen(row, avoid, avoid_category)
    monkeypatch.setattr(time, "monotonic", clock)
    return grade_fix.remediate_forward_book(
        "reverb", rows, store, profile="GYM", defects=[], today_iso=TODAY,
        caption_regen=timed, gap_filler=lambda *a, **k: "none",
        booking_cta=None)


def test_the_last_llm_pass_is_not_starved_by_an_earlier_one(monkeypatch):
    """THE PRODUCTION BUG, reproduced and fixed.

    The craft pass has 20 days to repair at 7 seconds a call: 140 seconds of
    work against a 90 second budget. Sharing one deadline it spends everything
    and `_fix_body_sameness` gets a deadline already in the past -- which is
    precisely what hillcountry, topfuel and train7164ae502 did on production.
    With the floor reserved, the body pass still gets a usable share and the
    1.0 body pair actually comes down.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    rows = _starvation_book()
    before = cv.report(rows)["body_similarity"]
    assert before["max"] == 1.0 and before["pairs_over_threshold"] >= 3

    regen, calls = _regen([True])
    out = _remediate(rows, _PermissiveStore(rows), regen, monkeypatch, _Clock())

    assert out["craft_fixed"] > 0, "the craft pass must really have run"
    assert out["body_pairs"] >= 3
    assert out["body_fixed"] > 0, (
        "the body pass got zero LLM budget: an earlier pass starved it")
    after = cv.report(rows)["body_similarity"]
    assert after["max"] < before["max"]
    assert after["pairs_over_threshold"] < before["pairs_over_threshold"]
