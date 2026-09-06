"""
tests/test_cross_gym_brain.py — AGENT_CROSS_GYM_BRAIN: the nightly cross gym
brain (agent/jobs/cross_gym_brain.py) and its read API
(agent/cross_gym_guidance.py).

The rulings these tests pin, one test per rail:

  1. Flag default OFF -> no-op. No store constructed, nothing read or written.
  2. STATISTICAL HONESTY. Both cells of a comparison must clear the sample floor
     (6, carried over from learning_guards.MIN_SAMPLE); a low n input produces
     ZERO guidance. A multiple comparisons correction (Benjamini Hochberg) is
     applied across the levers tested in one run, so a raw p < alpha that does
     not survive the FDR correction is `directional` and produces no guidance.
  3. FORM ONLY, BY WHITELIST. A caption carrying a fabricated stat and a member
     name contributes its FORM and nothing else: neither string appears anywhere
     in the stored artifact. A free text "pillar" (production really does carry
     those) is dropped, not leaked.
  4. CROSS GYM ISOLATION. No finding may be derived from a single gym's data
     (>= 2 distinct gyms required per cell, both sides), and guidance_for()
     returns byte identical output for two different gyms.
  5. APIFY / EXTERNAL RAIL. external=true and is_ad=true rows never train.

All tests are deterministic and offline: hand rolled fake stores injected via
store=, no network, no clock dependence (now= is injected).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone

import pytest

from agent import cross_gym_guidance as guidance
from agent.jobs import cross_gym_brain as brain


NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeStore:
    """Offline brain store: metrics_window + captions_for + baseline_gym_count
    + insert_rollup / latest_rollup. Records every call so a no-op can be proven
    (not merely assumed)."""

    def __init__(self, rows_by_gym=None, captions=None, baseline_gyms=0,
                 rollup=None):
        self.rows_by_gym = rows_by_gym or {}
        self.captions = captions or {}
        self.baseline_gyms = baseline_gyms
        self.inserted = []
        self.calls = []
        self._rollup = rollup

    def metrics_window(self, gym_id, start_iso, end_iso):
        self.calls.append(("metrics_window", gym_id))
        return list(self.rows_by_gym.get(gym_id) or [])

    def captions_for(self, gym_id, calendar_ids):
        self.calls.append(("captions_for", gym_id))
        return {cid: self.captions.get(cid) for cid in calendar_ids or []
                if cid in self.captions}

    def baseline_gym_count(self):
        self.calls.append(("baseline_gym_count", None))
        return self.baseline_gyms

    def insert_rollup(self, row):
        self.calls.append(("insert_rollup", None))
        self.inserted.append(row)
        return row

    def latest_rollup(self):
        self.calls.append(("latest_rollup", None))
        return self._rollup


def _metric_row(gym_id, i, *, score_likes, hook="question", pillar="service",
                fmt="feed", ask="booking_link", slot="morning",
                external=False, is_ad=False, calendar_id=None,
                media_product_type="FEED", has_member_face=None,
                snapshot_day=7, reach=1000):
    """One post_metrics row. `score_likes` drives learning_score.score, which is
    engagement_value / max(reach, 0.10 * followers)."""
    return {
        "gym_id": gym_id,
        "platform": "instagram",
        "platform_post_id": f"{gym_id}-{i}",
        "calendar_id": calendar_id if calendar_id is not None else f"cal-{gym_id}-{i}",
        "external": external,
        "is_ad": is_ad,
        "pillar": pillar,
        "format": fmt,
        "hook_family": hook,
        "ask_type": ask,
        "time_slot": slot,
        "caption_len_band": None,
        "has_member_face": has_member_face,
        "media_product_type": media_product_type,
        "published_at": "2026-08-20T12:00:00+00:00",
        "snapshot_day": snapshot_day,
        "reach": reach,
        "likes": score_likes,
        "comments": 0, "shares": 0, "saves": 0, "clicks": 0,
        "views": None, "follows": None,
        "followers_at_snapshot": 1000,
    }


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_CROSS_GYM_BRAIN", "true")


# ---------------------------------------------------------------------------
# 1. the flag
# ---------------------------------------------------------------------------

def test_flag_defaults_off():
    from agent import config
    assert config.cross_gym_brain_enabled() is False


def test_flag_off_is_a_true_noop():
    """Flag OFF: the injected store is NEVER touched (no read, no write)."""
    store = _FakeStore(rows_by_gym={"a": [_metric_row("a", 1, score_likes=10)]})
    out = brain.run(gyms=["a"], now=NOW, store=store)
    assert out["ok"] is False
    assert "AGENT_CROSS_GYM_BRAIN is OFF" in out["reason"]
    assert out["guidance"] == [] and out["findings"] == []
    assert store.calls == []          # nothing read, nothing written
    assert store.inserted == []


def test_runner_schedules_the_lane_in_its_own_isolated_block():
    """The nightly lane must be wired into run_daily, and wrapped so a brain
    failure can never take the draft run down (the metrics-sync block's shape)."""
    import inspect

    from agent import runner
    src = inspect.getsource(runner.run_daily)
    assert "cross_gym_brain" in src
    block = src.split("cross_gym_brain", 1)[1]
    assert "except Exception" in block
    assert "ops_alerts.alert" in block
    assert "The draft run is unaffected." in block


def test_listener_lists_the_lane():
    """The lane must be visible at scheduler startup, armed or dormant."""
    import inspect

    from agent import listener
    src = inspect.getsource(listener._print_scheduled_lanes)
    assert "cross gym brain" in src
    assert "AGENT_CROSS_GYM_BRAIN" in src


# ---------------------------------------------------------------------------
# 2. statistical honesty
# ---------------------------------------------------------------------------

def test_low_n_input_produces_zero_guidance(monkeypatch):
    """THE HEADLINE RULING: not enough data yet is a correct output.

    Both hook values appear in BOTH gyms, so the cross gym floor is satisfied and
    the SAMPLE FLOOR is the only thing standing in the way: four posts per cell
    against a floor of six. The apparent difference is enormous and perfectly
    clean, and it still produces ZERO guidance."""
    _arm(monkeypatch)
    rows = {}
    for gym in ("gyma", "gymb"):
        rows[gym] = (
            [_metric_row(gym, i, score_likes=500, hook="question")
             for i in range(2)]
            + [_metric_row(gym, 100 + i, score_likes=1, hook="bold_claim")
               for i in range(2)])
    store = _FakeStore(rows_by_gym=rows)
    out = brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    assert out["ok"] is True
    assert out["guidance"] == []
    hooks = [f for f in out["findings"] if f["lever"] == "hook_family"]
    assert hooks and all(f["n"] == 4 and f["gyms"] == 2 for f in hooks)
    verdicts = {f["verdict"] for f in out["findings"]}
    assert verdicts <= {"insufficient_data"}, verdicts


def test_sample_floor_needs_both_cells(monkeypatch):
    """One side well above the floor and the other below it is still
    insufficient_data — the floor applies to BOTH cells, not just the winner."""
    _arm(monkeypatch)
    rows = {
        "gyma": ([_metric_row("gyma", i, score_likes=100, hook="question")
                  for i in range(10)]
                 + [_metric_row("gyma", 100 + i, score_likes=10,
                                hook="bold_claim") for i in range(2)]),
        "gymb": ([_metric_row("gymb", i, score_likes=90, hook="question")
                  for i in range(10)]
                 + [_metric_row("gymb", 100 + i, score_likes=12,
                                hook="bold_claim") for i in range(2)]),
    }
    store = _FakeStore(rows_by_gym=rows)
    out = brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    hooks = [f for f in out["findings"] if f["lever"] == "hook_family"]
    assert hooks, "hook_family should have been considered"
    # bold_claim has 4 posts total across both gyms: below the floor of 6.
    assert all(f["verdict"] == "insufficient_data" for f in hooks)
    assert out["guidance"] == []


def test_welch_t_and_df_match_a_hand_computed_case():
    """Exact arithmetic, no table needed. a=[1..5], b=[6..10]: both means are
    3 and 8, both sample variances 2.5, so se^2 = 2.5/5 + 2.5/5 = 1.0,
    t = (3-8)/1 = -5, and the Welch-Satterthwaite df is
    1 / ((0.5^2/4) + (0.5^2/4)) = 8 exactly."""
    res = brain.welch_t_test([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert res["t"] == pytest.approx(-5.0, abs=1e-12)
    assert res["df"] == pytest.approx(8.0, abs=1e-12)
    assert 0.0 < res["p"] < 0.005


def test_welch_df_is_satterthwaite_not_pooled():
    """The WELCH df, not the pooled n1+n2-2. a=[1..5] (variance 2.5) against
    b=[10,20,30,40,50] (variance 250): se^2 = 0.5 + 50 = 50.5, and
    df = 50.5^2 / (0.5^2/4 + 50^2/4) = 4.07999..., nowhere near the pooled 8.
    Unequal variances are the norm across a fleet of very differently sized
    gyms, which is exactly why the test has to be Welch's."""
    res = brain.welch_t_test([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
    assert res["t"] == pytest.approx(-3.7994267415, abs=1e-9)
    assert res["df"] == pytest.approx(4.07999200079992, abs=1e-9)
    assert res["df"] != pytest.approx(8.0, abs=0.5)


def test_t_distribution_p_values_match_the_published_critical_values():
    """The p machinery is a real Student's t survival function, verified against
    the standard two sided critical values (p = 0.05 at t_crit for each df, and
    p = 0.01 for df=8)."""
    for df, t_crit in ((1, 12.706205), (2, 4.302653), (8, 2.306004),
                       (10, 2.228139), (30, 2.042272)):
        assert brain.student_t_sf_two_sided(t_crit, df) == pytest.approx(
            0.05, abs=5e-6), df
    assert brain.student_t_sf_two_sided(3.355387, 8) == pytest.approx(
        0.01, abs=5e-6)


def test_t_distribution_df1_matches_the_cauchy_closed_form():
    """df=1 is the Cauchy distribution, whose two sided tail has the exact closed
    form 1 - (2/pi) * arctan(|t|). An independent check of the incomplete beta."""
    import math as _m
    for t in (0.1, 0.5, 1.0, 2.0, 5.0, 25.0):
        expected = 1.0 - (2.0 / _m.pi) * _m.atan(abs(t))
        assert brain.student_t_sf_two_sided(t, 1) == pytest.approx(
            expected, abs=1e-9), t


def test_welch_p_is_one_for_identical_samples():
    res = brain.welch_t_test([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0])
    assert res["p"] == pytest.approx(1.0, abs=1e-9)


def test_benjamini_hochberg_rejects_fewer_than_raw_alpha():
    """Ten tests, one at p=0.04: raw alpha would call it a finding; BH does
    not (0.04 > (1/10) * 0.05)."""
    ps = [0.04] + [0.6, 0.7, 0.8, 0.9, 0.95, 0.5, 0.55, 0.65, 0.75]
    rejected, q = brain.benjamini_hochberg(ps, alpha=0.05)
    assert rejected[0] is False
    assert q[0] == pytest.approx(0.4, abs=1e-9)   # 0.04 * 10/1
    assert not any(rejected)


def test_benjamini_hochberg_keeps_a_genuinely_strong_finding():
    ps = [0.0001] + [0.6, 0.7, 0.8, 0.9]
    rejected, q = brain.benjamini_hochberg(ps, alpha=0.05)
    assert rejected[0] is True
    assert q[0] == pytest.approx(0.0005, abs=1e-9)


def _cell(scores, gyms=("gyma", "gymb", "gymc")):
    return {"scores": [float(s) for s in scores], "gyms": set(gyms)}


# A modest, real separation: significant on its own (raw p well under 0.05) but
# not strong enough to survive a run that also tested twenty other levers.
_SIGNAL_A = [0.53, 0.49, 0.62, 0.46, 0.57, 0.51, 0.60, 0.48]
_SIGNAL_B = [0.44, 0.49, 0.41, 0.52, 0.46, 0.43, 0.50, 0.47]
# Two indistinguishable cells: the noise every real run is full of.
_NOISE_A = [0.50, 0.52, 0.48, 0.51, 0.49, 0.50, 0.52, 0.48]
_NOISE_B = [0.51, 0.49, 0.50, 0.48, 0.52, 0.49, 0.51, 0.50]


def test_a_lone_signal_is_supported():
    """Control for the correction test below: tested by itself, this separation
    clears every bar."""
    cells = {("hook_family", "feed"): {"question": _cell(_SIGNAL_A),
                                       "bold_claim": _cell(_SIGNAL_B)}}
    findings = brain.evaluate(cells)
    assert {f["verdict"] for f in findings} == {"supported"}
    assert brain.guidance_from(findings)


def test_multiple_comparisons_correction_downgrades_to_directional():
    """THE CORRECTION RULING. The SAME signal, in a run that also tested ten
    other noisy levers, no longer survives Benjamini Hochberg: its verdict drops
    to `directional` and it produces NO guidance. Testing many levers at once is
    exactly what this job does, so without the correction the fleet would be
    handed manufactured findings every night."""
    noisy_levers = ("pillar", "ask_type", "time_slot", "caption_len_band",
                    "sentence_band", "ask_present", "has_member_face")
    noisy_values = {
        "pillar": ("service", "about"), "ask_type": ("booking_link", "none"),
        "time_slot": ("morning", "evening"),
        "caption_len_band": ("short", "mid"),
        "sentence_band": ("one_or_two", "three_to_five"),
        "ask_present": ("yes", "no"), "has_member_face": ("yes", "no"),
    }
    cells = {("hook_family", "feed"): {"question": _cell(_SIGNAL_A),
                                       "bold_claim": _cell(_SIGNAL_B)}}
    for lever in noisy_levers:
        v1, v2 = noisy_values[lever]
        cells[(lever, "feed")] = {v1: _cell(_NOISE_A), v2: _cell(_NOISE_B)}

    findings = brain.evaluate(cells)
    hook = [f for f in findings if f["lever"] == "hook_family"]
    assert hook, "the hook comparison must still be reported"
    assert all(f["p_value"] < 0.05 for f in hook), \
        "raw p is still significant; only the correction demotes it"
    assert {f["verdict"] for f in hook} == {"directional"}
    assert brain.guidance_from(findings) == []


def test_effect_size_floor_demotes_a_significant_but_trivial_difference():
    """Significance is not guidance. With 200 posts a side, a difference of
    Cohen's d 0.26 is comfortably significant (p about 0.01) and survives the
    correction, but it is below MIN_EFFECT (0.30) so it stays `directional` and
    changes nothing. Nudge the same comparison past the floor and it becomes
    `supported` — the floor is the only thing separating the two cases."""
    base = [0.400 + 0.001 * i for i in range(200)]

    def _verdicts(delta):
        cells = {("hook_family", "feed"): {
            "question": _cell(base),
            "bold_claim": _cell([x - delta for x in base])}}
        return brain.evaluate(cells)

    small = _verdicts(0.015)
    assert all(f["p_value"] < 0.05 for f in small)
    assert all(abs(f["effect_size"]) < brain.MIN_EFFECT for f in small)
    assert {f["verdict"] for f in small} == {"directional"}
    assert brain.guidance_from(small) == []

    big = _verdicts(0.018)
    assert all(abs(f["effect_size"]) >= brain.MIN_EFFECT for f in big)
    assert {f["verdict"] for f in big} == {"supported"}
    assert brain.guidance_from(big)


def test_supported_finding_becomes_guidance(monkeypatch):
    """A well powered, multi gym, large effect signal DOES clear the bar — the
    bar is strict, not impossible."""
    _arm(monkeypatch)

    def _posts(gym, hook, likes_seq, offset=0):
        return [_metric_row(gym, offset + i, score_likes=v, hook=hook,
                            pillar="service", ask="none", slot="morning")
                for i, v in enumerate(likes_seq)]

    hi = [400, 420, 380, 410, 430, 395, 405, 415]
    lo = [40, 45, 38, 42, 44, 39, 41, 43]
    rows = {
        "gyma": _posts("gyma", "question", hi) + _posts("gyma", "bold_claim", lo, 100),
        "gymb": _posts("gymb", "question", hi) + _posts("gymb", "bold_claim", lo, 100),
        "gymc": _posts("gymc", "question", hi) + _posts("gymc", "bold_claim", lo, 100),
    }
    store = _FakeStore(rows_by_gym=rows)
    out = brain.run(gyms=["gyma", "gymb", "gymc"], now=NOW, store=store)
    assert out["ok"] is True
    supported = [f for f in out["findings"] if f["verdict"] == "supported"]
    assert supported, [f["verdict"] for f in out["findings"]]
    g = out["guidance"]
    assert g, "a supported finding must produce guidance"
    favor = [i for i in g if i["lever"] == "hook_family"
             and i["value"] == "question"]
    assert favor and favor[0]["direction"] == "favor"
    assert favor[0]["gyms"] >= brain.MIN_CELL_GYMS
    # every finding carries its full statistics
    for f in out["findings"]:
        for key in ("n", "gyms", "n_other", "gyms_other", "verdict"):
            assert key in f
        assert f["verdict"] in brain.VERDICTS
        if f["verdict"] != "insufficient_data":
            assert isinstance(f["p_value"], float)
            assert isinstance(f["q_value"], float)
            assert isinstance(f["effect_size"], float)


# ---------------------------------------------------------------------------
# 3. FORM ONLY
# ---------------------------------------------------------------------------

FABRICATED_STAT = "97.3% of our members lost 22 pounds in 6 weeks"
MEMBER_NAME = "Brenda Kowalczyk"
SECRET_OFFER = "$49 unlimited founding member rate"


def test_no_caption_fragment_stat_or_member_name_reaches_the_artifact(monkeypatch):
    """Feed in posts whose captions carry a fabricated stat, a member name and a
    private offer. The brain reads that text (to derive the length and sentence
    bands) and must emit NONE of it."""
    _arm(monkeypatch)
    # two DIFFERENT captions so the length/sentence bands actually have two
    # cells to compare; both carry the same forbidden strings.
    short_caption = f"{MEMBER_NAME} did it. {SECRET_OFFER}."
    long_caption = (f"{MEMBER_NAME} did it. {FABRICATED_STAT}. Claim the "
                    f"{SECRET_OFFER} today. Book your free intro class now. "
                    + ("Every single week our members show up and do the work. "
                       * 10))
    rows = {}
    captions = {}
    for gym in ("gyma", "gymb", "gymc"):
        rows[gym] = [_metric_row(gym, i, score_likes=100 + i,
                                 hook="question" if i % 2 else "bold_claim")
                     for i in range(16)]
        for i, r in enumerate(rows[gym]):
            captions[r["calendar_id"]] = short_caption if i % 2 else long_caption
    store = _FakeStore(rows_by_gym=rows, captions=captions)
    out = brain.run(gyms=["gyma", "gymb", "gymc"], now=NOW, store=store)

    assert out["ok"] is True
    assert store.inserted, "the rollup row should have been written"
    blob = json.dumps(store.inserted[0], default=str) + json.dumps(out, default=str)
    for leak in (MEMBER_NAME, "Brenda", "Kowalczyk", FABRICATED_STAT, "97.3",
                 SECRET_OFFER, "$49", "founding", "free intro"):
        assert leak not in blob, f"leaked {leak!r}"
    # the caption still did its job: the derived FORM bands are present
    levers = {f["lever"] for f in out["findings"]}
    assert "caption_len_band" in levers and "sentence_band" in levers


def test_free_text_pillar_is_dropped_not_leaked(monkeypatch):
    """Production content_calendar really carries caption fragments in the
    pillar column ("We do the heavy lifting", "All in one offer"). The whitelist
    drops them; a blacklist would have shipped them fleet wide."""
    _arm(monkeypatch)
    junk = {"gyma": "We do the heavy lifting", "gymb": "All in one offer"}
    rows = {gym: [_metric_row(gym, i, score_likes=100, pillar=junk[gym])
                  for i in range(8)]
            for gym in ("gyma", "gymb")}
    store = _FakeStore(rows_by_gym=rows)
    out = brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    # the run completes and writes: the junk simply never became a lever value
    assert out["ok"] is True, out.get("reason")
    assert store.inserted
    blob = json.dumps(store.inserted[0], default=str) + json.dumps(out, default=str)
    for leak in ("heavy lifting", "heavy_lifting", "All in one offer",
                 "all_in_one_offer"):
        assert leak not in blob, f"leaked {leak!r}"
    assert not [f for f in out["findings"] if f["lever"] == "pillar"]


def test_form_only_violations_catches_a_planted_string():
    artifact = {"findings": [{"lever": "hook_family", "value": "question",
                              "verdict": "supported"}]}
    assert brain.form_only_violations(artifact) == []
    dirty = {"findings": [{"lever": "hook_family", "value": FABRICATED_STAT,
                           "verdict": "supported"}]}
    assert brain.form_only_violations(dirty)


def test_whitelist_drops_an_unknown_lever_value():
    assert brain._token("question", "hook_family") == "question"
    assert brain._token("Brenda Kowalczyk", "hook_family") is None
    assert brain._token("not_a_real_pillar_xyz", "pillar") is None
    assert brain._token(True, "has_member_face") == "yes"


def test_run_refuses_to_write_when_the_artifact_is_not_form_only(monkeypatch):
    """The verifier is a real gate: a poisoned artifact is never stored."""
    _arm(monkeypatch)
    rows = {g: [_metric_row(g, i, score_likes=100) for i in range(8)]
            for g in ("gyma", "gymb")}
    store = _FakeStore(rows_by_gym=rows)
    real_build = brain.build_artifact

    def _poisoned(*a, **kw):
        art = real_build(*a, **kw)
        art["findings"].append({"lever": "pillar", "value": FABRICATED_STAT,
                                "verdict": "supported"})
        return art

    monkeypatch.setattr(brain, "build_artifact", _poisoned)
    out = brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    assert out["ok"] is False
    assert "form only" in out["reason"]
    assert store.inserted == []


def test_bands_are_derived_from_caption_text_when_the_stored_band_is_null():
    """post_metrics.caption_len_band is null on most production rows; the band
    must come from the TEXT."""
    short = "Short one."
    long_text = "So many beats here. " * 40      # 40 sentences, 800 characters
    rows = [_metric_row("gyma", 0, score_likes=10),
            _metric_row("gyma", 1, score_likes=10)]
    assert rows[0]["caption_len_band"] is None    # the stored band, as in prod
    captions = {rows[0]["calendar_id"]: short,
                rows[1]["calendar_id"]: long_text}
    posts, _ = brain.learnable_posts("gyma", rows, captions)
    by_cal = {p["levers"]["caption_len_band"]: p["levers"]["sentence_band"]
              for p in posts}
    assert sorted(by_cal) == ["long", "short"]
    assert by_cal["short"] == "one_or_two"
    assert by_cal["long"] == "six_plus"


def test_sentence_band_measures_structure():
    assert brain.sentence_band("One sentence only.") == "one_or_two"
    assert brain.sentence_band("A. B. C. D.") == "three_to_five"
    assert brain.sentence_band("A. B. C. D. E. F. G.") == "six_plus"
    assert brain.sentence_band("") is None


def test_ask_present_prefers_the_stamped_type_then_the_text():
    assert brain.ask_present_from("no ask here", "booking_link") == "yes"
    assert brain.ask_present_from("no ask here", "none") == "no"
    assert brain.ask_present_from("Book your free intro class.", None) == "yes"
    assert brain.ask_present_from("Just a thought for today", None) == "no"
    assert brain.ask_present_from("", None) is None


# ---------------------------------------------------------------------------
# 4. cross gym isolation
# ---------------------------------------------------------------------------

def test_a_single_gym_can_never_produce_a_finding(monkeypatch):
    """ONE gym, 40 posts, an enormous and perfectly clean difference. That is a
    proxy for that gym's own content, so it is refused as insufficient_data and
    produces no guidance however large n is."""
    _arm(monkeypatch)
    rows = {"gyma": (
        [_metric_row("gyma", i, score_likes=500, hook="question")
         for i in range(20)]
        + [_metric_row("gyma", 100 + i, score_likes=5, hook="bold_claim")
           for i in range(20)])}
    store = _FakeStore(rows_by_gym=rows)
    out = brain.run(gyms=["gyma"], now=NOW, store=store)
    assert out["ok"] is True
    assert out["guidance"] == []
    hooks = [f for f in out["findings"] if f["lever"] == "hook_family"]
    assert hooks
    assert all(f["verdict"] == "insufficient_data" for f in hooks)
    assert all(f["gyms"] == 1 for f in hooks)


def test_the_artifact_carries_no_gym_id(monkeypatch):
    _arm(monkeypatch)
    rows = {g: [_metric_row(g, i, score_likes=100) for i in range(8)]
            for g in ("gyma", "gymb")}
    store = _FakeStore(rows_by_gym=rows)
    brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    blob = json.dumps(store.inserted[0], default=str)
    assert "gyma" not in blob and "gymb" not in blob


def test_guidance_is_identical_for_every_gym(monkeypatch):
    """THE ISOLATION GUARANTEE, made structural: the read API has no branch on
    gym_id, so no gym's data can be routed into another gym's prompt."""
    monkeypatch.setenv("AGENT_CROSS_GYM_BRAIN", "true")
    row = {
        "run_at": "2026-09-06T02:00:00+00:00",
        "window_start": "2026-06-08", "window_end": "2026-09-06",
        "window_days": 90,
        "guidance": [{"lever": "hook_family", "value": "question",
                      "format_stratum": "feed", "direction": "favor",
                      "effect_size": 0.9, "n": 24, "gyms": 3,
                      "q_value": 0.001}],
    }
    store = _FakeStore(rollup=row)
    a = guidance.guidance_for("gyma", now=NOW, store=store)
    b = guidance.guidance_for("gymb", now=NOW, store=store)
    assert a["guidance"] and a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# 5. the external / is_ad (Apify + boosted) rail
# ---------------------------------------------------------------------------

def test_external_and_is_ad_rows_never_train(monkeypatch):
    """Apify/second publisher rows arrive as external=true and boosted posts as
    is_ad=true. They are COUNTED and then excluded — adding a pile of them that
    would flip the winner must change nothing about the guidance."""
    _arm(monkeypatch)

    def _clean_rows(gym):
        return ([_metric_row(gym, i, score_likes=400, hook="question")
                 for i in range(8)]
                + [_metric_row(gym, 100 + i, score_likes=40, hook="bold_claim")
                   for i in range(8)])

    clean = {g: _clean_rows(g) for g in ("gyma", "gymb", "gymc")}
    poisoned = {g: (list(_clean_rows(g))
                    + [_metric_row(g, 200 + i, score_likes=9000,
                                   hook="bold_claim", external=True)
                       for i in range(30)]
                    + [_metric_row(g, 300 + i, score_likes=9000,
                                   hook="bold_claim", is_ad=True)
                       for i in range(30)])
                for g in ("gyma", "gymb", "gymc")}

    gyms = ["gyma", "gymb", "gymc"]
    out_clean = brain.run(gyms=gyms, now=NOW, store=_FakeStore(rows_by_gym=clean))
    store_p = _FakeStore(rows_by_gym=poisoned)
    out_poison = brain.run(gyms=gyms, now=NOW, store=store_p)

    assert out_clean["guidance"] == out_poison["guidance"]
    assert out_clean["findings"] == out_poison["findings"]
    assert out_poison["fleet"]["posts_analyzed"] == out_clean["fleet"]["posts_analyzed"]
    assert out_poison["fleet"]["posts_excluded_external"] == 90
    assert out_poison["fleet"]["posts_excluded_is_ad"] == 90


def test_apify_baseline_is_reporting_context_only(monkeypatch):
    _arm(monkeypatch)
    rows = {g: [_metric_row(g, i, score_likes=100) for i in range(8)]
            for g in ("gyma", "gymb")}
    store = _FakeStore(rows_by_gym=rows, baseline_gyms=7)
    brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    ctx = store.inserted[0]["context"]
    assert ctx["gyms_with_baseline"] == 7
    assert ctx["used_for_learning"] is False
    assert ctx["source"] == "apify social_baseline"


# ---------------------------------------------------------------------------
# storage + the read API
# ---------------------------------------------------------------------------

def test_rollup_row_is_append_only_and_complete(monkeypatch):
    _arm(monkeypatch)
    rows = {g: [_metric_row(g, i, score_likes=100) for i in range(8)]
            for g in ("gyma", "gymb")}
    store = _FakeStore(rows_by_gym=rows)
    brain.run(gyms=["gyma", "gymb"], now=NOW, store=store)
    assert len(store.inserted) == 1
    row = store.inserted[0]
    for key in ("run_at", "window_start", "window_end", "window_days",
                "gyms_contributing", "posts_analyzed", "findings", "guidance",
                "context"):
        assert key in row, key
    assert row["window_days"] == brain.WINDOW_DAYS
    assert row["gyms_contributing"] == 2
    # append only: the store exposes no update path at all
    assert not hasattr(store, "update_rollup")
    assert not hasattr(brain.SupabaseBrainStore, "update_rollup")
    assert not hasattr(brain.SupabaseBrainStore, "upsert_rollup")


def test_guidance_for_is_empty_when_the_flag_is_off():
    store = _FakeStore(rollup={"run_at": "2026-09-06T02:00:00+00:00",
                               "guidance": [{"lever": "hook_family",
                                             "value": "question",
                                             "direction": "favor"}]})
    out = guidance.guidance_for("gyma", now=NOW, store=store)
    assert out["guidance"] == []
    assert "AGENT_CROSS_GYM_BRAIN is OFF" in out["reason"]
    assert store.calls == []           # nothing read while dark


def test_guidance_for_is_empty_when_nothing_cleared_the_bar(monkeypatch):
    _arm(monkeypatch)
    store = _FakeStore(rollup={"run_at": "2026-09-06T02:00:00+00:00",
                               "guidance": []})
    out = guidance.guidance_for("gyma", now=NOW, store=store)
    assert out["ok"] is True
    assert out["guidance"] == []
    assert "nothing cleared" in out["reason"]


def test_guidance_for_refuses_stale_rollups(monkeypatch):
    _arm(monkeypatch)
    store = _FakeStore(rollup={"run_at": "2026-01-01T02:00:00+00:00",
                               "guidance": [{"lever": "hook_family",
                                             "value": "question",
                                             "format_stratum": "feed",
                                             "direction": "favor",
                                             "effect_size": 0.9, "n": 20,
                                             "gyms": 3, "q_value": 0.001}]})
    out = guidance.guidance_for("gyma", now=NOW, store=store)
    assert out["guidance"] == []
    assert "older than" in out["reason"]


def test_guidance_for_drops_a_poisoned_stored_item(monkeypatch):
    """Belt on the read side: a stored item carrying free text is dropped, not
    sanitized and not served."""
    _arm(monkeypatch)
    store = _FakeStore(rollup={
        "run_at": "2026-09-06T02:00:00+00:00",
        "guidance": [
            {"lever": "pillar", "value": FABRICATED_STAT, "direction": "favor"},
            # a value that IS a whitelisted token, but not for THIS lever. Only
            # the per lever value check can catch this one.
            {"lever": "hook_family", "value": "morning", "direction": "favor",
             "format_stratum": "feed", "effect_size": 0.9, "n": 20,
             "gyms": 3, "q_value": 0.001},
            # an unknown lever entirely
            {"lever": "secret_sauce", "value": "question", "direction": "favor"},
            {"lever": "hook_family", "value": "question",
             "format_stratum": "feed", "direction": "favor",
             "effect_size": 0.9, "n": 20, "gyms": 3, "q_value": 0.001},
        ]})
    out = guidance.guidance_for("gyma", now=NOW, store=store)
    assert len(out["guidance"]) == 1
    assert out["guidance"][0]["lever"] == "hook_family"
    assert out["guidance"][0]["value"] == "question"
    assert FABRICATED_STAT not in json.dumps(out)
    assert "secret_sauce" not in json.dumps(out)


def test_guidance_for_never_raises_on_a_broken_store(monkeypatch):
    _arm(monkeypatch)

    class _Boom:
        def latest_rollup(self):
            raise RuntimeError("postgrest down")

    out = guidance.guidance_for("gyma", now=NOW, store=_Boom())
    assert out["guidance"] == []
    assert "rollup read failed" in out["reason"]


def test_prompt_lines_are_form_only(monkeypatch):
    _arm(monkeypatch)
    store = _FakeStore(rollup={
        "run_at": "2026-09-06T02:00:00+00:00",
        "guidance": [{"lever": "hook_family", "value": "question",
                      "format_stratum": "feed", "direction": "favor",
                      "effect_size": 0.9, "n": 24, "gyms": 3,
                      "q_value": 0.001}]})
    lines = guidance.prompt_lines("gyma", now=NOW, store=store)
    assert lines == ["favor hook_family question on feed posts "
                     "(fleet form signal, n=24 across 3 gyms)"]


def test_prompt_lines_empty_when_dark():
    assert guidance.prompt_lines("gyma", now=NOW, store=_FakeStore()) == []
