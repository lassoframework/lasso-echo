"""learning_guards.py — Wave 7 honesty guards. Small accounts lie loudly: a gym
with 18 median likes will generate noise every month. Every guard here is a
testable pure function, and every one is a HARD requirement with regression
tests (tests/test_learning_guards.py):

  1. sample_floor        — a lever value needs >= MIN_SAMPLE scored posts before
                           it may be compared at all.
  2. within-gym only     — structural: every function here takes ONE gym's posts.
                           Cross-gym data is priors only (agent/playbook.py 7.6).
  3. rolling 90-day recency-weighted window.
  4. persistence_rule    — adopt only on >= 30% relative lift in two consecutive
                           months, OR one month with >= 12 posts per side.
  5. format-stratified   — reels compare against reels, photos against photos.
  6. month_is_tainted    — active second publisher, follower spike > 20%, or paid
                           boosts -> the month is observed, never trained on.

The drift cap (plus or minus 20% per weight per month) is enforced in
agent/playbook.py apply_bounds; DRIFT_CAP is re-exported here so the guard suite
tests one constant.

PURE: no I/O anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime, timezone

MIN_SAMPLE = 6            # guard 1: minimum scored posts per lever value
WINDOW_DAYS = 90          # guard 3: rolling window
PERSISTENCE_MIN_LIFT = 0.30   # guard 4: >= 30% relative score lift
PERSISTENCE_MONTHS = 2        # guard 4: two consecutive months
PERSISTENCE_BIG_N = 12        # guard 4: or one month with >= 12 posts per side
FOLLOWER_SPIKE_TAINT = 0.20   # guard 6: follower spike > 20% taints the month

# Single source of truth for the drift cap lives with the enforcement
# (playbook.apply_bounds); re-exported so the guards regression suite pins it.
DRIFT_CAP = 0.20


# ---- 1. sample floor -----------------------------------------------------------

def sample_floor(posts, min_sample: int = MIN_SAMPLE) -> bool:
    """True when this lever value has enough SCORED posts to be compared at all.
    `posts` is the list of scored posts (or an int count) for ONE lever value in
    ONE gym. Below the floor the value is an observation, never a comparison."""
    n = posts if isinstance(posts, int) else len([p for p in (posts or []) if p is not None])
    return n >= int(min_sample)


# ---- 3. rolling 90-day recency-weighted window -----------------------------------

def _parse_ts(ts):
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def recency_weight(published_at, now, window_days: int = WINDOW_DAYS) -> float:
    """Linear recency weight in [0, 1]: 1.0 for a post published now, decaying to
    0.0 at the window edge, 0.0 outside the window (or unparseable — a post we
    cannot date never influences the playbook)."""
    ts = _parse_ts(published_at)
    ref = _parse_ts(now)
    if ts is None or ref is None:
        return 0.0
    age_days = (ref - ts).total_seconds() / 86400.0
    if age_days < 0 or age_days > window_days:
        return 0.0
    return 1.0 - (age_days / float(window_days))


def weighted_mean_score(posts, now, window_days: int = WINDOW_DAYS):
    """Recency-weighted mean score over posts [{'score','published_at'}, ...].
    Posts outside the window (weight 0) contribute nothing; returns None when no
    post carries weight (never a fabricated mean). One viral fluke ages out of
    influence instead of owning the playbook forever."""
    num = 0.0
    den = 0.0
    for p in posts or []:
        s = (p or {}).get("score")
        if not isinstance(s, (int, float)) or isinstance(s, bool):
            continue
        w = recency_weight((p or {}).get("published_at"), now, window_days)
        if w <= 0:
            continue
        num += w * float(s)
        den += w
    if den <= 0:
        return None
    return num / den


# ---- 4. persistence rule ---------------------------------------------------------

def relative_lift(winner_score, alternative_score):
    """(winner - alternative) / alternative. None when the alternative leg is
    missing or non-positive (no honest baseline to lift against)."""
    if not isinstance(winner_score, (int, float)) or isinstance(winner_score, bool):
        return None
    if not isinstance(alternative_score, (int, float)) or isinstance(alternative_score, bool):
        return None
    if alternative_score <= 0:
        return None
    return (float(winner_score) - float(alternative_score)) / float(alternative_score)


def persistence_rule(monthly_results, min_lift: float = PERSISTENCE_MIN_LIFT,
                     big_month_n: int = PERSISTENCE_BIG_N) -> bool:
    """Adopt a lever change ONLY when the winner beats the alternative by
    >= min_lift relative score in PERSISTENCE_MONTHS consecutive months, OR in
    one month with >= big_month_n scored posts on EACH side. Otherwise it stays
    an observation.

    `monthly_results` is a chronologically ordered list (oldest first) of dicts
    per month: {winner_score, alternative_score, winner_n, alternative_n}.
    Months below the sample floor on either side never count toward persistence.
    """
    results = [r for r in (monthly_results or []) if isinstance(r, dict)]
    streak = 0
    for r in results:
        wn = r.get("winner_n") or 0
        an = r.get("alternative_n") or 0
        lift = relative_lift(r.get("winner_score"), r.get("alternative_score"))
        qualifies = (lift is not None and lift >= min_lift
                     and sample_floor(wn) and sample_floor(an))
        if qualifies and wn >= big_month_n and an >= big_month_n:
            return True  # one big month is enough
        streak = streak + 1 if qualifies else 0
        if streak >= PERSISTENCE_MONTHS:
            return True
    return False


# ---- 5. format stratification -----------------------------------------------------

def stratify_by_format(posts) -> dict:
    """Group posts by their format/media product type so reels only ever compare
    against reels and photos against photos. Posts with no format land under
    'unknown' and are never cross-compared."""
    out = {}
    for p in posts or []:
        fmt = str((p or {}).get("format") or (p or {}).get("media_product_type")
                  or "unknown").lower()
        out.setdefault(fmt, []).append(p)
    return out


# ---- 6. taint ---------------------------------------------------------------------

def month_is_tainted(gym_id: str, month: str, signals: dict | None = None) -> bool:
    """True when the month cannot honestly train the playbook: an active second
    publisher, a follower spike > 20%, or paid boosts on organic posts.

    `signals` carries the month's observed facts (assembled by the caller from
    post_metrics + account data — this module does no I/O):
      second_publisher_active: bool  (external post activity from another tool)
      follower_spike_pct: float      (0.25 == +25% follower jump in the month)
      paid_boosts: bool              (paid boost detected on organic posts)

    NO SIGNALS -> NOT PROVEN TAINTED (False), but the retro stores the signals
    it checked so a missing check is visible, never silently trusted. A tainted
    month is OBSERVED (metrics stored, findings noted) and NEVER trained on."""
    s = signals or {}
    if s.get("second_publisher_active"):
        return True
    spike = s.get("follower_spike_pct")
    if isinstance(spike, (int, float)) and not isinstance(spike, bool) \
            and spike > FOLLOWER_SPIKE_TAINT:
        return True
    if s.get("paid_boosts"):
        return True
    return False
