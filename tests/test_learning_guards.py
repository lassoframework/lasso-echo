"""tests/test_learning_guards.py — Wave 7.4 honesty guards, all HARD requirements.

Required by spec:
  - sample floor (5 posts -> not comparable, 6 -> comparable)
  - persistence rule (one good month < 30% -> observation not adoption; two
    consecutive months >= 30% -> adopt; one month with 12+ per side -> adopt)
  - taint exclusion (a tainted month contributes nothing)
  - drift cap (plus or minus 20% per weight per month)
  - the synthetic viral-fluke regression: one 807-like outlier post in an
    otherwise flat month -> the playbook does NOT move.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import learning_guards as guards
from agent import playbook as pb_mod
from agent.jobs import monthly_retro

NOW = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. sample floor
# ---------------------------------------------------------------------------

def test_sample_floor_five_not_comparable_six_comparable():
    assert guards.sample_floor(5) is False
    assert guards.sample_floor(6) is True
    assert guards.sample_floor([{"score": 1}] * 5) is False
    assert guards.sample_floor([{"score": 1}] * 6) is True
    assert guards.MIN_SAMPLE == 6


# ---------------------------------------------------------------------------
# 2. persistence rule
# ---------------------------------------------------------------------------

def _month(w, a, wn=8, an=8):
    return {"winner_score": w, "alternative_score": a,
            "winner_n": wn, "alternative_n": an}


def test_one_month_under_30pct_is_observation_not_adoption():
    assert guards.persistence_rule([_month(1.2, 1.0)]) is False  # 20% lift


def test_one_good_month_over_30pct_is_still_not_adoption():
    assert guards.persistence_rule([_month(1.5, 1.0)]) is False  # 50%, one month


def test_two_consecutive_months_over_30pct_adopts():
    assert guards.persistence_rule([_month(1.4, 1.0), _month(1.5, 1.0)]) is True


def test_gap_month_breaks_the_streak():
    assert guards.persistence_rule(
        [_month(1.5, 1.0), _month(1.1, 1.0), _month(1.5, 1.0)]) is False


def test_one_month_with_12_per_side_adopts():
    assert guards.persistence_rule([_month(1.4, 1.0, wn=12, an=12)]) is True


def test_below_sample_floor_month_never_counts():
    assert guards.persistence_rule(
        [_month(2.0, 1.0, wn=5, an=8), _month(2.0, 1.0, wn=8, an=5)]) is False


# ---------------------------------------------------------------------------
# 3. taint exclusion
# ---------------------------------------------------------------------------

def test_month_is_tainted_signals():
    assert guards.month_is_tainted("g", "2026-08",
                                   {"second_publisher_active": True}) is True
    assert guards.month_is_tainted("g", "2026-08",
                                   {"follower_spike_pct": 0.25}) is True
    assert guards.month_is_tainted("g", "2026-08",
                                   {"follower_spike_pct": 0.10}) is False
    assert guards.month_is_tainted("g", "2026-08", {"paid_boosts": True}) is True
    assert guards.month_is_tainted("g", "2026-08", {}) is False


class _FakePlaybookStore:
    def __init__(self):
        self.rows = []
        self.inserts = []

    def latest(self, gym_id):
        rows = [r for r in self.rows if r["gym_id"] == gym_id]
        return max(rows, key=lambda r: r["version"]) if rows else None

    def insert_version(self, row):
        self.rows.append(row)
        self.inserts.append(row)
        return row


class _FakeRetroStore:
    def __init__(self, months, signals=None):
        self.months = months
        self.signals = signals or {}
        self.retros = []
        self.playbook_store = _FakePlaybookStore()

    def month_metrics(self, gym_id, month):
        return list(self.months.get(month, []))

    def taint_signals(self, gym_id, month):
        return dict(self.signals)

    def insert_retro(self, row):
        self.retros.append(row)
        return row


def _metric_row(pid, hook, likes, published, external=False, fmt="photo"):
    return {"gym_id": "gym1", "platform": "instagram",
            "platform_post_id": pid, "snapshot_day": 7,
            "external": external, "format": fmt, "pillar": "community",
            "hook_family": hook, "ask_type": "booking_link",
            "time_slot": "morning", "caption_len_band": "mid",
            "published_at": published, "reach": 500, "likes": likes,
            "comments": 0, "shares": 0, "saves": 0, "clicks": 0, "follows": 0,
            "followers_at_snapshot": 1000}


def _strong_month(month_prefix):
    """A month where question hooks beat bold_claim by well over 30% with
    both sides above the sample floor (but below 12 per side)."""
    rows = []
    for i in range(6):
        rows.append(_metric_row(f"{month_prefix}q{i}", "question", 60,
                                f"{month_prefix}-{10 + i:02d}T12:00:00Z"))
        rows.append(_metric_row(f"{month_prefix}b{i}", "bold_claim", 30,
                                f"{month_prefix}-{10 + i:02d}T15:00:00Z"))
    return rows


def test_tainted_month_contributes_nothing(monkeypatch):
    """A month with an active second publisher is observed (retro row stored,
    tainted=true) but trains NOTHING: no playbook version is written even when
    the levers would otherwise qualify."""
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    store = _FakeRetroStore(
        {"2026-08": _strong_month("2026-08"), "2026-07": _strong_month("2026-07")},
        signals={"second_publisher_active": True})
    result = monthly_retro.run(month="2026-08", gyms=["gym1"], store=store,
                               now=NOW, notifier=lambda g, t: None)
    assert result["ok"] is True
    retro = store.retros[0]
    assert retro["tainted"] is True
    assert retro["playbook_diff"] == {}
    assert store.playbook_store.inserts == []  # trained on nothing


# ---------------------------------------------------------------------------
# 4. drift cap
# ---------------------------------------------------------------------------

def test_drift_cap_constant_and_clamp():
    assert guards.DRIFT_CAP == 0.20
    assert pb_mod.DRIFT_CAP == 0.20
    assert pb_mod.clamp_drift(1.0, 2.0) == 1.2       # clamped up
    assert pb_mod.clamp_drift(1.0, 0.1) == 0.8       # clamped down
    assert pb_mod.clamp_drift(1.0, 1.1) == 1.1       # inside the cap, untouched
    assert pb_mod.clamp_drift(None, 0.7) == 0.7      # seeding a new weight


# ---------------------------------------------------------------------------
# 5. the synthetic viral fluke: the playbook does NOT move on noise
# ---------------------------------------------------------------------------

def test_viral_fluke_does_not_move_the_playbook(monkeypatch):
    """One 807-like coaching-reel outlier in an otherwise flat month. The
    guards must hold: the fluke's own lever group is real, but ONE month
    (below 12 per side) never satisfies the persistence rule, so the playbook
    does not move — the fluke stays an observation."""
    monkeypatch.setenv("AGENT_LEARNING_LOOP", "true")
    rows = []
    # a flat month: 7 question posts and 6 bold_claim posts, all ~18 likes
    for i in range(7):
        rows.append(_metric_row(f"fq{i}", "question", 18,
                                f"2026-08-{8 + i:02d}T12:00:00Z"))
    for i in range(6):
        rows.append(_metric_row(f"fb{i}", "bold_claim", 18,
                                f"2026-08-{8 + i:02d}T15:00:00Z"))
    # the fluke: one bold_claim reel with 807 likes
    rows.append(_metric_row("fluke", "bold_claim", 807,
                            "2026-08-20T12:00:00Z"))
    store = _FakeRetroStore({"2026-08": rows})  # no prior month at all
    result = monthly_retro.run(month="2026-08", gyms=["gym1"], store=store,
                               now=NOW, notifier=lambda g, t: None)
    assert result["ok"] is True
    retro = store.retros[0]
    assert retro["tainted"] is False
    assert retro["playbook_diff"] == {}          # the playbook did not move
    assert store.playbook_store.inserts == []    # no version written
    assert "Playbook unchanged" in retro["digest"]


def test_recency_weight_window():
    """Guard 3: outside the rolling 90-day window a post carries zero weight —
    one old viral fluke cannot own the playbook forever."""
    assert guards.recency_weight("2026-09-04T12:00:00Z", NOW) > 0.98
    assert guards.recency_weight("2026-05-01T12:00:00Z", NOW) == 0.0  # >90d old
    assert guards.recency_weight(None, NOW) == 0.0


def test_format_stratification():
    """Guard 5: reels compare against reels, photos against photos."""
    posts = [{"format": "reel", "score": 1.0}, {"format": "photo", "score": 2.0},
             {"format": "reel", "score": 3.0}, {"score": 4.0}]
    strata = guards.stratify_by_format(posts)
    assert len(strata["reel"]) == 2
    assert len(strata["photo"]) == 1
    assert len(strata["unknown"]) == 1
