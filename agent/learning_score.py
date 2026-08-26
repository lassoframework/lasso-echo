"""learning_score.py — Wave 7: ONE number per post, comparable within a gym.

    engagement_value = 1*likes + 3*comments + 4*shares + 4*saves + 3*clicks + 5*follows
    score = engagement_value / max(reach, 0.10 * followers_at_snapshot)

Saves and shares outweigh likes because they predict non-follower distribution;
follows are the business outcome. Day-7 is the scoring snapshot (day-28 is used
only for follows attribution). The reach floor stops a post that reached 3
people from posting a fake 200% rate. Reels additionally track
watch_ratio = avg_watch_time / duration.

PURE: no I/O. NULL-NOT-ZERO on inputs: a missing metric contributes 0 to the
weighted sum (an absent engagement is genuinely zero engagement observed), but a
post with NO reach and NO follower base cannot be scored and returns None —
never a fabricated rate.
"""
from __future__ import annotations

SCORING_SNAPSHOT_DAY = 7      # the one snapshot scores are computed from
FOLLOWS_SNAPSHOT_DAY = 28     # follows attribution only

WEIGHTS = {
    "likes": 1,
    "comments": 3,
    "shares": 4,
    "saves": 4,
    "clicks": 3,
    "follows": 5,
}

REACH_FLOOR_FRACTION = 0.10   # max(reach, 0.10 * followers_at_snapshot)


def _num(v):
    """A real number or 0 (bools rejected — they are ints in Python, never a metric)."""
    if isinstance(v, bool):
        return 0
    return v if isinstance(v, (int, float)) else 0


def engagement_value(metrics: dict) -> float:
    """The weighted engagement sum for one post's metrics dict."""
    m = metrics or {}
    return float(sum(w * _num(m.get(k)) for k, w in WEIGHTS.items()))


def score(metrics: dict) -> float | None:
    """The post's score: engagement_value / max(reach, 0.10 * followers).

    Returns None when BOTH reach and followers_at_snapshot are missing or
    non-positive (no honest denominator exists — we never invent one)."""
    m = metrics or {}
    reach = _num(m.get("reach"))
    followers = _num(m.get("followers_at_snapshot"))
    denom = max(float(reach), REACH_FLOOR_FRACTION * float(followers))
    if denom <= 0:
        return None
    return engagement_value(m) / denom


def watch_ratio(avg_watch_time_ms, duration_seconds) -> float | None:
    """Reels only: avg watch time / duration. Inputs: avg watch time in ms
    (post_metrics.watch_time_ms), duration in seconds (post_metrics.video_seconds).
    Returns None when either leg is missing or non-positive."""
    ms = _num(avg_watch_time_ms)
    dur = _num(duration_seconds)
    if ms <= 0 or dur <= 0:
        return None
    return (float(ms) / 1000.0) / float(dur)


# ---- hook-quality fields (20260827), reels only. PURE, null-not-zero. -----------

def reel_skip_rate(metrics: dict) -> float | None:
    """The stored platform skip rate (post_metrics.reels_skip_rate) as a real
    number, or None when Zernio did not report one — never a fabricated 0."""
    v = (metrics or {}).get("reels_skip_rate")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def reel_watch_ratio(metrics: dict) -> float | None:
    """The reel's watch ratio from a post_metrics row. Prefers the direct avg
    watch time (watch_time_ms); when absent, derives the average from
    watch_total_ms / views (total watch time spread over the views that
    produced it). Returns None when no honest computation exists."""
    m = metrics or {}
    dur = _num(m.get("video_seconds"))
    if dur <= 0:
        return None
    avg_ms = _num(m.get("watch_time_ms"))
    if avg_ms > 0:
        return watch_ratio(avg_ms, dur)
    total_ms = _num(m.get("watch_total_ms"))
    views = _num(m.get("views"))
    if total_ms <= 0 or views <= 0:
        return None
    return watch_ratio(total_ms / views, dur)
