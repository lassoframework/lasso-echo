"""tests/test_monthly_retro.py — Wave 7.8 the monthly retro, on SYNTHETIC months
only (the real run waits for Blake's flag taps; see WAVE6_HUMAN_TAPS.md TAP 3).

Required by spec:
  - a synthetic month produces deterministic findings and a BOUNDED diff
  - the digest text contains only numbers backed by evidence rows
  - a tainted month is observed and the playbook is unchanged
"""
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import playbook as pb_mod
from agent.jobs import monthly_retro

NOW = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)


class FakePlaybookStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserts = []

    def latest(self, gym_id):
        rows = [r for r in self.rows if r["gym_id"] == gym_id]
        return max(rows, key=lambda r: r["version"]) if rows else None

    def insert_version(self, row):
        self.rows.append(row)
        self.inserts.append(row)
        return row


class FakeRetroStore:
    def __init__(self, months, signals=None):
        self.months = months
        self.signals = signals or {}
        self.retros = []
        self.playbook_store = FakePlaybookStore()

    def month_metrics(self, gym_id, month):
        return list(self.months.get(month, []))

    def taint_signals(self, gym_id, month):
        return dict(self.signals)

    def insert_retro(self, row):
        self.retros.append(row)
        return row


def _row(pid, hook, likes, published, external=False, fmt="photo",
         experiment_label=None, is_ad=False):
    return {"gym_id": "gym1", "platform": "instagram",
            "platform_post_id": pid, "snapshot_day": 7,
            "external": external, "is_ad": is_ad,
            "format": fmt, "pillar": "community",
            "hook_family": hook, "ask_type": "booking_link",
            "time_slot": "morning", "caption_len_band": "mid",
            "published_at": published, "reach": 500, "likes": likes,
            "comments": 0, "shares": 0, "saves": 0, "clicks": 0, "follows": 0,
            "followers_at_snapshot": 1000,
            "experiment_label": experiment_label}


def _month_rows(prefix, q_likes=60, b_likes=30, n=6):
    rows = []
    for i in range(n):
        rows.append(_row(f"{prefix}q{i}", "question", q_likes,
                         f"{prefix}-{10 + i:02d}T12:00:00Z"))
        rows.append(_row(f"{prefix}b{i}", "bold_claim", b_likes,
                         f"{prefix}-{10 + i:02d}T15:00:00Z"))
    return rows


def _run(store, monkeypatch, month="2026-08", notifier=None):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    return monthly_retro.run(month=month, gyms=["gym1"], store=store, now=NOW,
                             notifier=notifier or (lambda g, t: None))


# ---------------------------------------------------------------------------
# deterministic findings + bounded diff
# ---------------------------------------------------------------------------

def test_synthetic_month_produces_deterministic_findings(monkeypatch):
    months = {"2026-08": _month_rows("2026-08"), "2026-07": _month_rows("2026-07")}
    r1 = _run(FakeRetroStore(dict(months)), monkeypatch)
    r2 = _run(FakeRetroStore(dict(months)), monkeypatch)
    assert r1["ok"] and r2["ok"]
    f1, f2 = r1["gyms"][0]["findings"], r2["gyms"][0]["findings"]
    assert f1 == f2  # byte-identical findings on identical inputs
    assert len(f1["keep_doing"]) <= 3
    assert len(f1["stop_doing"]) <= 3
    keeps = {(k["lever"], k["value"]) for k in f1["keep_doing"]}
    assert ("hook_family", "question") in keeps


def test_two_persistent_months_update_playbook_within_bounds(monkeypatch):
    """question beats bold_claim by 100% in two consecutive months (6/side):
    the persistence rule passes and the playbook moves — but only within the
    plus or minus 20% drift cap, with evidence rows attached."""
    store = FakeRetroStore({"2026-08": _month_rows("2026-08"),
                            "2026-07": _month_rows("2026-07")})
    result = _run(store, monkeypatch)
    retro = store.retros[0]
    diff = retro["playbook_diff"]
    assert "hook_family_weights" in diff
    new_w = diff["hook_family_weights"]["question"]
    # bounded: a fresh weight seeds at 1.0 * (1 + 0.20) and can never exceed it
    assert new_w <= 1.0 * (1.0 + pb_mod.DRIFT_CAP) + 1e-9
    written = store.playbook_store.inserts
    assert len(written) == 1
    assert written[0]["version"] == 1
    assert written[0]["updated_by"] == "monthly_retro"
    assert written[0]["evidence"]  # never a playbook write without evidence
    # every evidence key points at a real metrics row from the synthetic months
    all_keys = {monthly_retro.row_key(r)
                for rows in store.months.values() for r in rows}
    assert set(written[0]["evidence"]) <= all_keys


def test_single_month_does_not_move_playbook(monkeypatch):
    """One good month (under 12 per side) is an observation, not an adoption."""
    store = FakeRetroStore({"2026-08": _month_rows("2026-08")})
    _run(store, monkeypatch)
    retro = store.retros[0]
    assert retro["playbook_diff"] == {}
    assert store.playbook_store.inserts == []


# ---------------------------------------------------------------------------
# digest: only numbers backed by evidence rows
# ---------------------------------------------------------------------------

def test_digest_numbers_are_backed_by_evidence_rows(monkeypatch):
    store = FakeRetroStore({"2026-08": _month_rows("2026-08"),
                            "2026-07": _month_rows("2026-07")})
    result = _run(store, monkeypatch)
    gym = result["gyms"][0]
    findings = gym["findings"]
    digest = gym["digest"]
    all_keys = {monthly_retro.row_key(r)
                for rows in store.months.values() for r in rows}
    for finding in findings["keep_doing"] + findings["stop_doing"]:
        # every evidence key is a real post_metrics row key
        assert set(finding["evidence"]) <= all_keys
        # the n the digest cites IS the evidence row count — never invented
        assert finding["n"] == len(finding["evidence"])
        assert f"n={finding['n']} posts" in digest
        assert str(finding["mean_score"]) in digest
    # no dashes in the client-facing digest (copy gate holds)
    from agent.copy_gate import violations
    assert violations(digest) == []


def test_experiment_verdict_needs_the_sample_floor(monkeypatch):
    """3 experiment rows vs 12 control rows: below the floor on the experiment
    side -> honestly inconclusive, never a causal claim."""
    rows = _month_rows("2026-08")
    for i in range(3):
        rows.append(_row(f"exp{i}", "story_open", 80,
                         f"2026-08-{20 + i:02d}T12:00:00Z",
                         experiment_label="hook_family:2026-08"))
    store = FakeRetroStore({"2026-08": rows})
    result = _run(store, monkeypatch)
    exp = result["gyms"][0]["findings"]["experiment"]
    assert exp["status"] == "inconclusive"
    assert "floor" in exp["detail"]


def test_experiment_verdict_evaluated_with_enough_posts(monkeypatch):
    rows = _month_rows("2026-08")
    for i in range(6):
        rows.append(_row(f"exp{i}", "story_open", 80,
                         f"2026-08-{18 + i:02d}T12:00:00Z",
                         experiment_label="hook_family:2026-08"))
    store = FakeRetroStore({"2026-08": rows})
    result = _run(store, monkeypatch)
    exp = result["gyms"][0]["findings"]["experiment"]
    assert exp["status"] == "evaluated"
    assert exp["experiment_n"] == 6
    assert exp["control_n"] == 12
    assert exp["lever"] == "hook_family"


# ---------------------------------------------------------------------------
# tainted month: observed, playbook unchanged
# ---------------------------------------------------------------------------

def test_tainted_month_is_observed_but_playbook_unchanged(monkeypatch):
    store = FakeRetroStore(
        {"2026-08": _month_rows("2026-08"), "2026-07": _month_rows("2026-07")},
        signals={"follower_spike_pct": 0.35})
    result = _run(store, monkeypatch)
    retro = store.retros[0]
    assert retro["tainted"] is True
    assert retro["playbook_diff"] == {}
    assert store.playbook_store.inserts == []
    # still OBSERVED: the retro row landed with the month's findings
    assert retro["findings"]["scored_posts"] == 12
    assert "TAINTED" in retro["digest"]


# ---------------------------------------------------------------------------
# external posts never train
# ---------------------------------------------------------------------------

def test_external_posts_inform_baseline_but_never_train(monkeypatch):
    """A pile of external story_open posts must not put story_open into the
    findings or the playbook — we don't learn from posts we didn't shape."""
    months = {"2026-08": _month_rows("2026-08"),
              "2026-07": _month_rows("2026-07")}
    for i in range(8):
        months["2026-08"].append(
            _row(f"ext{i}", "story_open", 500,
                 f"2026-08-{5 + i:02d}T12:00:00Z", external=True))
    store = FakeRetroStore(months)
    result = _run(store, monkeypatch)
    findings = result["gyms"][0]["findings"]
    claimed = {(f["lever"], f["value"])
               for f in findings["keep_doing"] + findings["stop_doing"]}
    assert ("hook_family", "story_open") not in claimed
    diff = store.retros[0]["playbook_diff"]
    assert "story_open" not in str(diff.get("hook_family_weights", {}))


def test_is_ad_posts_inform_baseline_but_never_train(monkeypatch):
    """is_ad rows get the SAME treatment as external rows (20260827): a pile
    of boosted boost_hook posts must not put boost_hook into the findings or
    the playbook — paid reach never trains the organic playbook."""
    months = {"2026-08": _month_rows("2026-08"),
              "2026-07": _month_rows("2026-07")}
    for i in range(8):
        months["2026-08"].append(
            _row(f"ad{i}", "boost_hook", 500,
                 f"2026-08-{5 + i:02d}T12:00:00Z", is_ad=True))
    store = FakeRetroStore(months)
    result = _run(store, monkeypatch)
    findings = result["gyms"][0]["findings"]
    claimed = {(f["lever"], f["value"])
               for f in findings["keep_doing"] + findings["stop_doing"]}
    assert ("hook_family", "boost_hook") not in claimed
    diff = store.retros[0]["playbook_diff"]
    assert "boost_hook" not in str(diff.get("hook_family_weights", {}))
    # ...but the ad rows DO inform the baseline (whole-feed reality), so the
    # baseline with ads present differs from the organic-only one.
    organic_only = FakeRetroStore({"2026-08": _month_rows("2026-08"),
                                   "2026-07": _month_rows("2026-07")})
    organic_result = _run(organic_only, monkeypatch)
    assert (result["gyms"][0]["findings"]["baseline"]
            != organic_result["gyms"][0]["findings"]["baseline"])


def test_is_ad_experiment_rows_never_reach_the_verdict():
    """A labeled experiment row that was boosted is excluded from the verdict
    on BOTH sides (same as external)."""
    scored = monthly_retro.scoring_rows(
        [_row(f"e{i}", "question", 60, f"2026-08-{10 + i:02d}T12:00:00Z",
              experiment_label="hook_family:2026-08") for i in range(6)]
        + [_row(f"c{i}", "bold_claim", 30, f"2026-08-{10 + i:02d}T15:00:00Z")
           for i in range(6)]
        + [_row("adx", "question", 900, "2026-08-20T12:00:00Z",
                experiment_label="hook_family:2026-08", is_ad=True)])
    verdict = monthly_retro.experiment_verdict(scored, "2026-08")
    assert verdict["status"] == "evaluated"
    assert verdict["experiment_n"] == 6  # the boosted row did not sneak in
    assert "instagram:adx:d7" not in verdict["evidence"]


# ---------------------------------------------------------------------------
# flag OFF -> no-op; notifier fires per gym
# ---------------------------------------------------------------------------

def test_flag_off_is_a_noop(monkeypatch):
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "false")
    sentinel = FakeRetroStore({})
    result = monthly_retro.run(month="2026-08", gyms=["gym1"], store=sentinel,
                               now=NOW, notifier=lambda g, t: None)
    assert result["ok"] is False
    assert "AGENT_LEARNING_LOOP" in result["reason"]
    assert sentinel.retros == []


def test_digest_posts_to_the_gym_channel(monkeypatch):
    posted = []
    store = FakeRetroStore({"2026-08": _month_rows("2026-08")})
    _run(store, monkeypatch, notifier=lambda g, t: posted.append((g, t)))
    assert len(posted) == 1
    assert posted[0][0] == "gym1"
    assert "monthly retro" in posted[0][1]


def test_month_defaults_to_prior_month():
    assert monthly_retro.prior_month(NOW) == "2026-08"
    assert monthly_retro.next_month("2026-12") == "2027-01"
