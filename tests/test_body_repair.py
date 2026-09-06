"""AGENT_CTA_VARIETY — the BODY-copy repair (`grade_fix._fix_body_sameness`).

Blake, 2026-09-06: *"Build the actual body-copy repair, not just detection ...
build the repair path that acts when a post's body copy is a near-duplicate of
another post in the same gym's book (not just the closing line, the actual
middle content/structure). Same rules as before apply: no forced/fake content,
real repair only, don't touch gyms where the pool/source material can't support
a genuinely different post, flag those honestly rather than papering over
them."*

PR #64 shipped the MEASUREMENT (caption_variety.body_text / body_shingles /
body_similarity and the "body" collision kind). Nothing acted on it. Measured
read-only on the four live books the morning this was written, over POSTS
(post_date + caption hash, the grouping every pass in grade_fix uses):

  gym               posts   body max   pairs >= 0.15
  gritx               41     0.1636          1
  hillcountry         25     1.0             8
  topfuel             40     1.0            11
  train7164ae502      26     1.0            18

The 1.0s are all the same shape and none of them is an exact duplicate caption
(`_fix_duplicates` would already own that): two posts open with the SAME HOOK
LINE and carry different second paragraphs, and `body_text` strips the last
non-empty line, so the hook IS the whole body. hillcountry says "You've been
meaning to get stronger." on 2026-09-11 and 2026-09-12; topfuel says "Find your
fitness community in Valparaiso." on 09-15 and 09-16; train7164ae502 says
"You've tried the big box gyms." on 09-18 and 09-30, TWELVE posts apart and
therefore invisible to a windowed check.

Every test here is deterministic and offline: fake store, injected regen,
per-test sqlite kv (conftest sets AGENT_DB_PATH).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import caption_variety as cv
from agent.jobs import grade_fix, grade_sweep


TODAY = "2026-09-06"
REVERB_CTA = "​How do I get started with training at CrossFit Reverb?"


class _FakeStore:
    """Writes only to a wipeable row, exactly as the real store's
    patch_pending_plan does server-side."""

    def __init__(self, rows):
        self.rows = rows

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None,
                           levers=None):
        for r in self.rows:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                if str(r.get("status") or "").lower() not in ("pending", "draft",
                                                              "queued"):
                    return None
                if caption is not None:
                    r["caption"] = caption
                if pillar is not None:
                    r["pillar"] = pillar
                return dict(r)
        return None


class _PermissiveStore(_FakeStore):
    """Writes whatever it is handed, onto any row, wipeable or not.

    THE TRAP THIS AVOIDS (D68, and it has bitten this repo twice): a fake that
    enforces the rule under test holds the line instead of the code, so
    reverting the code's own guard leaves the test green and the test asserts
    nothing. Guard tests here use a store that protects nothing.
    """

    def patch_pending_plan(self, gym_id, row_id, *, caption=None, pillar=None,
                           levers=None):
        for r in self.rows:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                if caption is not None:
                    r["caption"] = caption
                return dict(r)
        return None


# ---------------------------------------------------------------------------
# Book builders
#
# Captions are built from SYNTHETIC, provably distinct vocabulary rather than
# from realistic prose, because these tests assert on a similarity SCORE and
# hand written prose quietly shares phrases: an earlier draft of this file
# scored 0.70 between two captions meant to be unrelated and the tests measured
# the fixture, not the code. `_w(k)` yields a different token for every k, so
# two hooks built from disjoint k ranges score exactly 0.0 and two built from
# overlapping ranges score a value this file can compute by hand.
# ---------------------------------------------------------------------------

_SYL_A = ("bar", "cad", "dor", "fen", "gil", "hol", "jun", "kes", "lum", "mor",
          "nev", "orb", "pel", "quar", "rid", "sel", "tor", "vin", "wex", "yar")
_SYL_B = ("an", "el", "in", "or", "us", "ay", "ic", "om", "ur", "ex")


def _w(k):
    """A distinct token for every k. Deliberately not a real word: it cannot
    match copy_gate.ASK_RE or a filler opener by accident."""
    k = int(k)
    return _SYL_A[k % 20] + _SYL_B[(k // 20) % 10] + ("" if k < 200 else str(k // 200))


def _phrase(start, n):
    return " ".join(_w(start + j) for j in range(n))


def _hook(k, width=6):
    """A hook line built from words no other hook (at a different k) uses."""
    return _phrase(k * width, width).capitalize() + "."


def _tail(i):
    """The caption's LAST line, which body_text strips. Carries the gym's ask so
    the caption is not craft flagged, and a leading marker so two posts sharing
    a hook are still two DIFFERENT captions (and two different caption_hashes,
    which truncate at 200 chars)."""
    return (f"Note {i}. Coaches walk the room before a single rep is loaded and "
            "the whiteboard already carries your name for the day. Book your "
            "free intro class to see exactly how that works.")


def _post(k, i, closing=None):
    """A two line caption whose BODY is its HOOK alone.

    body_text strips the last non empty line, so a "hook / paragraph" caption
    has the hook for a body. That is not a quirk of the fixture: it is the live
    shape behind every 1.0 on the fleet -- hillcountry, topfuel and
    train7164ae502 each repeat a hook verbatim under a different paragraph."""
    cap = f"{_hook(k)}\n\n{_tail(i)}"
    return f"{cap}\n{closing}" if closing else cap


def _rows(specs, gym_id="reverb"):
    """specs: [(post_date, caption, status)] -> content_calendar shaped rows."""
    return [{"id": f"row_{i}", "gym_id": gym_id, "post_date": d,
             "caption": cap, "pillar": "community", "category": "community",
             "format": "feed", "account": "instagram", "status": status}
            for i, (d, cap, status) in enumerate(specs)]


def _same_hook_book(n=3, gym_id="reverb"):
    """`n` posts that all open on the SAME hook line under DIFFERENT closing
    paragraphs -- the live hillcountry / topfuel / train7164ae502 defect."""
    return _rows([(f"2026-09-{6 + i:02d}", _post(0, i), "pending")
                  for i in range(n)], gym_id=gym_id)


def _fresh(i):
    """A caption whose hook shares no vocabulary with any book hook above, so it
    scores 0.0 body similarity against the book it lands in."""
    return _post(500 + i, 900 + i)


def _regen_bank():
    """An injected regen with a genuinely deep library: every caption it hands
    back is distinct from the book and from its own earlier answers."""
    calls = []

    def regen(row, avoid, avoid_category=""):
        calls.append(str((row or {}).get("post_date") or ""))
        for i in range(50):
            cap = _fresh(len(calls) * 50 + i)
            if cap not in avoid:
                return cap, "education"
        return None
    return regen, calls


def _run(gym_id, rows, store, regen, threshold=None, deadline=None):
    return grade_fix._fix_body_sameness(gym_id, rows, store, regen, set(),
                                        lambda m: None, deadline=deadline,
                                        threshold=threshold)


# ---------------------------------------------------------------------------
# 1. The flag
# ---------------------------------------------------------------------------

def test_the_body_pass_is_inert_with_the_flag_off(monkeypatch):
    """OFF by default = zero behaviour change, the repo's standing rule."""
    monkeypatch.delenv("AGENT_CTA_VARIETY", raising=False)
    rows = _same_hook_book(3)
    before = [r["caption"] for r in rows]
    regen, calls = _regen_bank()
    assert _run("reverb", rows, _FakeStore(rows), regen) == (0, 0, 0)
    assert calls == []
    assert [r["caption"] for r in rows] == before


# ---------------------------------------------------------------------------
# 2. It actually repairs, and it is a REAL repair
# ---------------------------------------------------------------------------

def test_a_near_duplicate_body_is_rewritten_fresh(monkeypatch):
    """THE POINT. Two posts share a body; the later one gets a genuinely
    different caption and the measured sameness drops below the threshold."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(2)
    assert cv.body_similarity(rows[0]["caption"], rows[1]["caption"]) == 1.0
    regen, calls = _regen_bank()
    pairs, fixed, unrepairable = _run("reverb", rows, _FakeStore(rows), regen)
    assert (pairs, fixed, unrepairable) == (1, 1, 0)
    assert calls == ["2026-09-07"]
    assert cv.body_similarity(rows[0]["caption"], rows[1]["caption"]) < \
        cv.BODY_SIMILARITY_THRESHOLD


def test_only_the_later_date_is_rewritten(monkeypatch):
    """The earlier post keeps its caption -- the same keeper rule
    _fix_duplicates uses, so one file holds one answer."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(2)
    first = rows[0]["caption"]
    regen, _calls = _regen_bank()
    _run("reverb", rows, _FakeStore(rows), regen)
    assert rows[0]["caption"] == first
    assert rows[1]["caption"] != first


def test_detection_is_book_wide_not_window_limited(monkeypatch):
    """train7164ae502 repeats "You've tried the big box gyms." on 2026-09-18 and
    2026-09-30 -- TWELVE posts apart, outside caption_variety.DEFAULT_WINDOW.

    This is the deliberate departure from the other collision kinds and it
    follows report()'s own reasoning. Scope the pass to the window and this
    real, live repeat goes unrepaired forever.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    n = cv.DEFAULT_WINDOW + 4
    specs = [(f"2026-09-{6 + i:02d}", _post(i, i), "pending") for i in range(n)]
    # The first and last posts share a hook, and nothing else does.
    specs[-1] = (specs[-1][0], _post(0, 99), "pending")
    rows = _rows(specs)
    assert cv.body_similarity(rows[0]["caption"], rows[-1]["caption"]) == 1.0
    distance = len(rows) - 1
    assert distance > cv.DEFAULT_WINDOW, distance
    regen, calls = _regen_bank()
    pairs, fixed, unrepairable = _run("reverb", rows, _FakeStore(rows), regen)
    assert (pairs, fixed, unrepairable) == (1, 1, 0)
    assert calls == [rows[-1]["post_date"]]


# ---------------------------------------------------------------------------
# 3. The honest skips. No forced difference, ever.
# ---------------------------------------------------------------------------

def test_a_gym_whose_sources_cannot_supply_a_different_post_is_an_honest_skip(monkeypatch):
    """The regen has nothing to offer (thin library). The caption STAYS, the
    skip is counted, and the reason is logged."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(3)
    before = [r["caption"] for r in rows]
    logs = []
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "reverb", rows, _FakeStore(rows), lambda *a, **k: None, set(),
        logs.append)
    assert (pairs, fixed) == (3, 0)
    assert unrepairable == 2, unrepairable          # both later sides
    assert [r["caption"] for r in rows] == before
    assert any("source material too thin" in m for m in logs), logs


def test_a_replacement_that_is_still_the_same_template_is_refused(monkeypatch):
    """A regen that hands back ANOTHER post's template is not a repair.

    Without this check the pass would happily trade hillcountry's 1.0 pair for
    a different 1.0 pair and report it fixed.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(2)
    before = [r["caption"] for r in rows]
    logs = []
    echo = lambda row, avoid, cat="": (_post(0, 77), "education")
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "reverb", rows, _FakeStore(rows), echo, set(), logs.append)
    assert (pairs, fixed, unrepairable) == (1, 0, 1)
    assert [r["caption"] for r in rows] == before
    assert any("still reads as the same template" in m for m in logs), logs


def test_a_replacement_that_fails_the_craft_bar_is_refused(monkeypatch):
    """Same rule as every other repair here: never trade a caption for a worse
    one. This replacement is body-distinct but far under the length floor."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(2)
    before = [r["caption"] for r in rows]
    logs = []
    tiny = lambda row, avoid, cat="": ("Elephants migrate northward tonight.",
                                       "education")
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "reverb", rows, _FakeStore(rows), tiny, set(), logs.append)
    assert (pairs, fixed, unrepairable) == (1, 0, 1)
    assert [r["caption"] for r in rows] == before
    assert any("does not clear the craft bar" in m for m in logs), logs


def test_a_human_owned_day_is_never_rewritten_and_costs_no_llm(monkeypatch):
    """EVERY post is approved and the store protects nothing.

    The assertion that only THIS pass's guard can hold is `calls == []`: the
    regen is never invoked at all. `_patch_date_rows` would also refuse the
    write, so asserting only "the caption did not change" would stay green with
    this guard reverted -- and would have asserted nothing.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(3)
    for r in rows:
        r["status"] = "approved"
    before = [r["caption"] for r in rows]
    regen, calls = _regen_bank()
    logs = []
    pairs, fixed, unrepairable = grade_fix._fix_body_sameness(
        "reverb", rows, _PermissiveStore(rows), regen, set(), logs.append)
    assert (pairs, fixed, unrepairable) == (3, 0, 0)
    assert calls == [], calls
    assert [r["caption"] for r in rows] == before
    assert any("human owned" in m for m in logs), logs


def test_the_pass_stops_when_the_llm_budget_is_spent(monkeypatch):
    """Same bounded wall clock every other regen pass here shares: a single
    gym can never blow the nightly sweep's budget."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(3)
    before = [r["caption"] for r in rows]
    regen, calls = _regen_bank()
    spent = grade_fix._deadline(-1.0)
    pairs, fixed, unrepairable = _run("reverb", rows, _FakeStore(rows), regen,
                                      deadline=spent)
    assert (pairs, fixed, unrepairable) == (3, 0, 0)
    assert calls == [], calls
    assert [r["caption"] for r in rows] == before


# ---------------------------------------------------------------------------
# 4. Chains
# ---------------------------------------------------------------------------

def test_a_chain_of_three_regenerates_each_post_at_most_once(monkeypatch):
    """train7164ae502 has SIX posts opening "You've tried solo workouts and ...".
    Naive pairwise repair would rewrite the same day up to five times."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    rows = _same_hook_book(4)
    regen, calls = _regen_bank()
    pairs, fixed, unrepairable = _run("reverb", rows, _FakeStore(rows), regen)
    assert pairs == 6                      # every pair of four identical bodies
    assert (fixed, unrepairable) == (3, 0)
    assert calls == ["2026-09-07", "2026-09-08", "2026-09-09"], calls
    assert len(calls) == len(set(calls))


def test_a_repair_that_already_broke_the_chain_spares_the_posts_after_it(monkeypatch):
    """A SLIDING chain: A overlaps B, B overlaps C, A and C share nothing.

    Two pairs are found, but once B is rewritten C no longer matches anything,
    so C must NOT be regenerated. The pass re-checks each later side against the
    book AS REPAIRED SO FAR; acting on the stale pair list would burn an LLM
    call and churn a caption that is already clean.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    # Hooks over overlapping word windows: A = w0..w5, B = w2..w7, C = w4..w9.
    hooks = [_phrase(0, 6), _phrase(2, 6), _phrase(4, 6)]
    rows = _rows([(f"2026-09-{6 + i:02d}",
                   f"{hooks[i].capitalize()}.\n\n{_tail(i)}", "pending")
                  for i in range(3)])
    caps = [r["caption"] for r in rows]
    assert cv.body_similarity(caps[0], caps[1]) >= cv.BODY_SIMILARITY_THRESHOLD
    assert cv.body_similarity(caps[1], caps[2]) >= cv.BODY_SIMILARITY_THRESHOLD
    assert cv.body_similarity(caps[0], caps[2]) < cv.BODY_SIMILARITY_THRESHOLD
    regen, calls = _regen_bank()
    pairs, fixed, unrepairable = _run("reverb", rows, _FakeStore(rows), regen)
    assert pairs == 2                       # A<->B and B<->C
    assert (fixed, unrepairable) == (1, 0)
    assert calls == ["2026-09-07"], calls   # B only: C was spared by B's repair
    assert rows[2]["caption"] == caps[2]


# ---------------------------------------------------------------------------
# 5. The wiring, and the ORDER
# ---------------------------------------------------------------------------

def test_remediate_forward_book_actually_runs_the_body_pass(monkeypatch):
    """A pass that is built and never called is the failure this repo keeps
    repeating (D56/D68, five instances)."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _same_hook_book(2)
    regen, calls = _regen_bank()
    out = grade_fix.remediate_forward_book(
        "reverb", rows, _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=regen,
        gap_filler=lambda *a, **k: "none", logger=lambda m: None)
    assert out["body_pairs"] == 1, out
    assert out["body_fixed"] == 1, out
    assert out["body_unrepairable"] == 0, out
    assert any("read as a repeat of another post" in a
               for a in out["actions"]), out["actions"]


def test_an_unrepairable_body_is_reported_not_swallowed(monkeypatch):
    """Blake: *"flag those honestly rather than papering over them."* The
    result dict has to SAY the gym could not be fixed."""
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    rows = _same_hook_book(2)
    out = grade_fix.remediate_forward_book(
        "reverb", rows, _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=lambda *a, **k: None,
        gap_filler=lambda *a, **k: "none", logger=lambda m: None)
    assert out["body_pairs"] == 1, out
    assert out["body_fixed"] == 0, out
    assert out["body_unrepairable"] == 1, out
    assert out["skipped"] >= 1, out
    assert any("source material too thin" in a for a in out["actions"]), \
        out["actions"]


def test_the_body_pass_runs_after_the_closing_line_passes(monkeypatch):
    """THE ORDERING, asserted rather than described.

    `body_text` measures a caption with its CLOSING LINE STRIPPED, so every
    pass that touches a closing line moves the cut this pass measures from.
    Here each post carries an identical hook, a distinct second paragraph, and
    the same invalid stapled closing. WITH the closing on, the bodies are
    hook+paragraph and score BELOW the threshold. Once `_fix_invalid_closings`
    removes the closing, the body is the hook alone and the posts collide at
    1.0. Move the body pass above the closing passes and it finds nothing at
    all -- which is exactly the live shape on hillcountry, topfuel and
    train7164ae502.
    """
    monkeypatch.setenv("AGENT_CTA_VARIETY", "true")
    monkeypatch.setenv("AGENT_GRADE_SELF_FIX", "true")
    monkeypatch.setattr(grade_fix, "_booking_cta_pool", lambda g, log: [])
    specs = []
    for i in range(3):
        para = _phrase(3000 + i * 26, 26).capitalize() + "."
        cap = f"{_hook(0)}\n\n{para}\n{REVERB_CTA}"
        specs.append((f"2026-09-{6 + i:02d}", cap, "pending"))
    rows = _rows(specs)
    caps = [r["caption"] for r in rows]
    assert max(cv.body_similarity(a, b) for i, a in enumerate(caps)
               for b in caps[i + 1:]) < cv.BODY_SIMILARITY_THRESHOLD
    out = grade_fix.remediate_forward_book(
        "reverb", rows, _FakeStore(rows), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=lambda *a, **k: None,
        gap_filler=lambda *a, **k: "none", logger=lambda m: None)
    assert out["invalid_closings_removed"] == 3, out
    assert out["body_pairs"] == 3, out


def test_every_counter_the_fix_returns_reaches_the_sweep_report():
    """THE TWO-WAY GUARD (D68). grade_sweep._merge_fix folds only the keys named
    in _FIX_COUNT_KEYS into the per-gym report; a counter missing from that
    tuple is computed and silently dropped, which is "built but not wired" one
    layer down. Assert the tuple still COVERS the result dict, not merely that
    its members exist."""
    shape = grade_fix.remediate_forward_book(
        "reverb", [], _FakeStore([]), profile="GYM", defects=[],
        today_iso=TODAY, caption_regen=None, gap_filler=lambda *a, **k: "none",
        logger=lambda m: None)
    counters = {k for k, v in shape.items() if isinstance(v, int)
                and not isinstance(v, bool)}
    missing = counters - set(grade_sweep._FIX_COUNT_KEYS)
    assert not missing, f"counters dropped by the sweep report: {sorted(missing)}"
