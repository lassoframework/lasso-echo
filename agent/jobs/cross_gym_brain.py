"""cross_gym_brain.py — the WEEKLY CROSS GYM brain (flag AGENT_CROSS_GYM_BRAIN,
default OFF -> no-op, no store constructed, nothing read or written).

Blake's ask (2026-09-06): "i want echo to build a brain for all gyms it digest
weekly the best post and then is able to digest all the post for the week see
trends on what is working and use that + the gyms brain to create the best post."

WHAT IT DOES
  Once a week: pull every gym's matured post_metrics (the Zernio ingestion lane,
  agent/metrics_sync.py), score each post with the SAME scorer the per-gym
  monthly retro uses (agent/learning_score.py via monthly_retro.scoring_rows),
  pool the fleet, and ask TWO questions:

    1. PER LEVER: does this FORM choice actually correlate with better engagement
       across gyms, or is it noise? (evaluate / lever_cells)
    2. THE BEST POSTS: do the fleet's TOP PERFORMING posts share a FORM the rest
       do not? (top_posts_digest) — Blake's "digest the best post".

  Both answers, with their statistics, land in cross_gym_brain (append only) and
  the handful that clear the bar become FORM guidance the caption generator can
  read through agent/cross_gym_guidance.py.

WHY WEEKLY AND NOT NIGHTLY (changed 2026-09-06)
  Blake asked for weekly, and weekly is also the right statistical call. The
  learnable pool is ~90 posts across 5 contributing gyms and grows by a handful a
  day. A nightly job re-runs the SAME underpowered comparison every 24 hours on
  almost exactly the same rows and produces the same zero guidance items, while
  paying a full fleet read every night and writing a rollup row a day that says
  nothing new. The cadence gate lives in `run_weekly()` (an ISO week key in
  agent/db's kv table, the agent/weekly_report.py pattern), and runner.run_daily
  calls THAT, not run(). A missed night does not skip a week: the first nightly
  tick of a new ISO week fires it.

WHY THE STATISTICS ARE THE HEADLINE
  A ~19 gym fleet posting a few times a week produces tiny cells. Most observed
  differences are noise, and a loop that "learns" from noise is worse than no
  loop: it launders randomness into instructions. So:

  * SAMPLE FLOOR. Carried over from monthly_retro / learning_guards.MIN_SAMPLE
    (6). BOTH sides of every comparison must clear it, not just the winner.
  * A REAL TEST. Welch's unequal variance t test on log1p transformed engagement
    scores, with the two sided p value from the regularized incomplete beta
    function. Pure stdlib (math only) — no scipy, no numpy, no new dependency.
  * MULTIPLE COMPARISONS. Every lever value is tested one against the rest,
    within its format stratum, so one run performs dozens of tests. At alpha
    0.05 that alone would manufacture "findings". The run applies a
    BENJAMINI HOCHBERG false discovery rate correction across every test in the
    run and reports the q value on every finding.
  * EFFECT SIZE. A significant but trivial difference is not guidance. Cohen's d
    must clear MIN_EFFECT as well.
  * VERDICTS. insufficient_data | not_significant | directional | supported.
    ONLY `supported` becomes guidance. "Not enough data yet" is a correct and
    expected output of this job, and on today's fleet volume it is the output for
    most levers.

HONESTY RAILS CARRIED OVER FROM monthly_retro (non negotiable)
  * external=true rows NEVER train. We do not learn from posts we did not shape,
    and we do not let a second publisher poison the fleet.
  * is_ad=true rows NEVER train. Paid reach poisons organic lever comparisons.
  * Format stratified: reels compare against reels, feed against feed. The two
    levers that ARE format (format, media_product_type) are the only unstratified
    tests.
  * A post with no honest denominator is dropped by the scorer, never zero filled.

THE BEST POST DIGEST (top_posts_digest), and why it usually says "no"
  "Digest the best post" is answered as a FORM question, because that is the only
  version of it that can cross a gym boundary: take the fleet's top decile by the
  same engagement score, and ask whether any FORM token is over or under
  represented there relative to every other post — hook shape, caption length
  band, sentence structure band, ask presence and type, pillar, slot, format and
  media product type, member face.

  Each such comparison is a 2x2 table with tiny counts, so the test is FISHER'S
  EXACT test (stdlib math.comb, no approximation), the effect size is Cohen's h,
  and the digest's tests carry their own Benjamini Hochberg correction. A digest
  finding is a SECOND look at the same posts the lever tests already used, so it
  is a WEAKER claim than a lever finding and is reported in its own section
  rather than mixed into the lever findings.

  At today's fleet volume the top decile is single digits, so the honest verdict
  is `insufficient_data` or `not_significant` and `distinguishable: false` — the
  artifact SAYS the best posts are not distinguishable from noise rather than
  crowning one. That string is the deliverable, not a failure.

  A TOP POST'S CONTENT NEVER CROSSES. The digest stores counts and whitelisted
  FORM tokens only: no caption text, no permalink, no post id, no gym id, no
  offer, no stat, no member name. The same form_only_violations() whitelist that
  guards the lever findings walks the digest, and the run REFUSES TO WRITE when
  anything in it is not a known token — a poisoned "best post" entry carrying
  caption text is refused, never sanitised.

FORM ONLY, ENFORCED BY WHITELIST (hard boundary)
  The brain emits guidance about FORM: hook shape, caption length band, sentence
  count band, pillar/topic label, whether an ask is present and of what type,
  posting slot, format, member face presence. It NEVER emits a caption fragment,
  a stat, a result, an offer, a member name, a handle, or any verbatim text from
  any gym's posts.

  This is enforced with a WHITELIST of allowed lever names and allowed lever
  VALUES (ALLOWED_LEVER_VALUES below), never a blacklist. A value that is not a
  known token is DROPPED from the analysis entirely. That is not theoretical
  hygiene: content_calendar.pillar in production already contains free text rows
  whose "pillar" is a caption fragment ("We do the heavy lifting", "All in one
  offer", "Sales are now", "The portal"). A blacklist would ship those into every
  gym's prompt. The whitelist cannot.

  Caption TEXT is read (to derive length and sentence bands, because the stored
  caption_len_band is unreliable and frequently null) and is discarded at the
  point of derivation — it never reaches a post record, a finding, or the stored
  artifact. `form_only_violations()` re-walks the finished artifact and the run
  REFUSES TO WRITE if any string in it is not a whitelisted token, an ISO
  timestamp, or one of this module's own fixed notes.

CROSS GYM ISOLATION (hard boundary)
  The rollup is FORM STATISTICS ONLY. On top of the whitelist:
  * every cell of every comparison must draw on at least MIN_CELL_GYMS (2)
    DISTINCT gyms, on BOTH sides. A finding that only one gym's posts could
    produce is a proxy for that gym's content and is refused as
    insufficient_data, however large its n. The SAME rule governs the best post
    digest: a top decile drawn from fewer than MIN_CELL_GYMS gyms is
    insufficient_data, and so is any single FORM token inside it.
  * the artifact carries no gym_id anywhere, only fleet counts.
  * agent/cross_gym_guidance.guidance_for() returns the identical fleet form
    guidance for every gym, so there is no routing path along which one gym's
    data could reach another gym's post.

APIFY / social_baseline
  Apify (agent/social_baseline.py) measures a gym's PUBLIC feed before and
  outside Echo. That is BASELINE / reporting context, not a training signal, and
  those posts arrive in post_metrics as external=true anyway. The brain records
  only the fleet COUNT of gyms that have a stored social_baseline row, tagged
  used_for_learning: false. No Apify measure is ever fed to a test.

WIRING
  runner.run_daily calls run_weekly() nightly, in its own try/except (a brain
  failure never breaks the draft run); run_weekly fires run() at most ONCE per
  ISO week. Read only everywhere: post_metrics, content_calendar captions,
  social_baseline. The only write is the append only cross_gym_brain row (plus
  the one kv marker that records which ISO week last ran). Nothing here
  publishes, approves, or touches any social account.

Everything is injectable (store, now, window_days, alpha) so the entire path is
unit tested offline with no network call.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone

from agent import config, learning_guards as guards
from agent.jobs import monthly_retro

# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------

WINDOW_DAYS = 90              # rollup window (post published_at)
MIN_CELL_N = guards.MIN_SAMPLE          # 6 — carried over from the per gym retro
MIN_CELL_GYMS = 2             # cross gym isolation: no finding from one gym

# PSEUDOREPLICATION GUARD (added 2026-09-06 after review).
#
# ">= 2 distinct gyms" alone is satisfied by a cell of 99 posts from one gym and
# 1 post from another. Welch's t test would then treat 99 correlated posts from a
# single gym — one voice, one audience, one posting schedule, one photographer —
# as 99 independent draws, and the fleet would "learn" that gym's habits and hand
# them to everyone else as a cross gym finding. Two cheap structural rules, both
# applied to BOTH cells of every comparison:
#
#   MIN_GYM_CELL_N      a gym only COUNTS toward the distinct gym floor when it
#                       contributed at least this many posts to the cell. One
#                       post is a token, not a second source; two is also the
#                       minimum for a gym to have any within gym variance at all.
#   MAX_GYM_CELL_SHARE  no single gym may supply more than this share of a cell.
#
# Both thresholds are judgment calls, not derivations, and are deliberately
# stated as constants so a future reader can argue with them. 0.70 is the loosest
# value that still forbids a cell being ESSENTIALLY one gym: at 0.70 the minority
# gyms together always carry at least ~a third of the evidence. The rule was
# fixed BEFORE looking at which live findings survive it, and it is not to be
# tuned to keep a result alive.
MIN_GYM_CELL_N = 2
MAX_GYM_CELL_SHARE = 0.70

ALPHA = 0.05                  # FDR level for Benjamini Hochberg
MIN_EFFECT = 0.30             # |Cohen's d| floor — significance is not enough
MAX_FINDINGS = 200            # sanity ceiling on one artifact

# THE BEST POST DIGEST. "Top performing" is the top decile of the fleet's pooled
# engagement scores, never a hand picked winner. Both sides of the split must
# still clear MIN_CELL_N, and the top side must still draw on MIN_CELL_GYMS
# distinct gyms, or the digest is insufficient_data and says so.
TOP_FRACTION = 0.10
MAX_TOP_FINDINGS = 200

# Cadence. The job runs at most once per ISO week; the marker lives in agent/db's
# kv table under this key (the agent/weekly_report.py pattern).
CADENCE_DAYS = 7
WEEK_KV_KEY = "cross_gym_brain_week"

VERDICTS = ("insufficient_data", "not_significant", "directional", "supported")
DIRECTIONS = ("favor", "avoid")
UNSTRATIFIED = "*"            # stratum token for the format levers themselves

# Where a guidance item came from. Both are FORM statistics; a top_posts item is
# the weaker claim (a second look at the same posts) and is labeled so a reader
# can tell them apart without inspecting the findings.
SOURCE_LEVERS = "lever_findings"
SOURCE_TOP_POSTS = "top_posts"


# ---------------------------------------------------------------------------
# THE WHITELIST. Lever names, and for each lever the allowed VALUE tokens.
# A value outside this table is dropped, never emitted, never analysed.
# ---------------------------------------------------------------------------

# Pillar/topic labels. Slug tokens only. Production content_calendar carries free
# text in this column for a handful of rows (caption fragments used as a
# "pillar"); those rows are dropped here rather than leaked.
_PILLARS = (
    "offer", "about", "service", "summit", "faces", "results", "sample",
    "community", "testimonial", "educational", "education", "platform",
    "doctrine", "photo", "book", "b2b", "hype_montage", "podcast", "proof",
    "transformation", "class_promo", "event", "member_win", "athlete_stat",
    "promo", "faq", "seo",
)

# Format tokens (content_calendar.format / the scorer's normalized format).
_FORMATS = ("feed", "reel", "reels", "story", "carousel", "video", "image",
            "update", "unknown")

# Instagram media product types, lowercased.
_MEDIA_PRODUCT_TYPES = ("feed", "reels", "story", "ad", "carousel_container",
                        "unknown")

# Time slot bands — agent/lever_stamp.time_slot_band's own vocabulary.
_TIME_SLOTS = ("early_morning", "morning", "midday", "afternoon", "evening",
               "night", "unknown")

# Sentence structure bands, derived here from the caption text.
SENTENCE_BANDS = ("one_or_two", "three_to_five", "six_plus")

ALLOWED_LEVER_VALUES = {
    # lever_stamp.HOOK_FAMILIES
    "hook_family": frozenset(
        ("question", "bold_claim", "story_open", "number_lead", "pain_callout")),
    # lever_stamp.LEN_BANDS, re-derived from the caption text
    "caption_len_band": frozenset(("short", "mid", "long")),
    "sentence_band": frozenset(SENTENCE_BANDS),
    "pillar": frozenset(_PILLARS),
    "ask_present": frozenset(("yes", "no")),
    # lever_stamp.ASK_TYPES
    "ask_type": frozenset(
        ("booking_link", "dm", "comment_keyword", "bio", "none")),
    "time_slot": frozenset(_TIME_SLOTS),
    "format": frozenset(_FORMATS),
    "media_product_type": frozenset(_MEDIA_PRODUCT_TYPES),
    "has_member_face": frozenset(("yes", "no")),
}

LEVERS = tuple(sorted(ALLOWED_LEVER_VALUES))

# The two levers that ARE the format axis; stratifying them by format would
# compare a stratum against itself.
FORMAT_LEVERS = ("format", "media_product_type")


# ---------------------------------------------------------------------------
# pure statistics — stdlib only, no scipy, no numpy
# ---------------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / float(len(xs)) if xs else None


def _variance(xs):
    """Sample variance (n-1). None when fewer than two observations."""
    if not xs or len(xs) < 2:
        return None
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / float(len(xs) - 1)


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    maxit, eps, fpmin = 300, 3.0e-12, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    """Regularized incomplete beta I_x(a, b). Pure math module."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_sf_two_sided(t, df):
    """Two sided p value for Student's t with `df` degrees of freedom.

    p = I_{df/(df + t^2)}(df/2, 1/2). Exact, stdlib only. t=0 -> 1.0."""
    if t is None or df is None or df <= 0:
        return None
    tt = float(t) * float(t)
    return _betai(0.5 * float(df), 0.5, float(df) / (float(df) + tt))


def welch_t_test(a, b):
    """Welch's unequal variance t test over two samples.

    Returns {"t", "df", "p", "mean_a", "mean_b"}. A degenerate comparison (fewer
    than two observations on a side, or zero standard error) yields p=1.0 — no
    evidence, never a fabricated significance. Pure."""
    a = [float(x) for x in (a or [])]
    b = [float(x) for x in (b or [])]
    na, nb = len(a), len(b)
    ma, mb = _mean(a), _mean(b)
    if na < 2 or nb < 2 or ma is None or mb is None:
        return {"t": None, "df": None, "p": 1.0, "mean_a": ma, "mean_b": mb}
    va, vb = _variance(a) or 0.0, _variance(b) or 0.0
    sa, sb = va / na, vb / nb
    se2 = sa + sb
    if se2 <= 0.0:
        return {"t": None, "df": None, "p": 1.0, "mean_a": ma, "mean_b": mb}
    t = (ma - mb) / math.sqrt(se2)
    denom = 0.0
    if na > 1:
        denom += (sa * sa) / (na - 1)
    if nb > 1:
        denom += (sb * sb) / (nb - 1)
    df = (se2 * se2) / denom if denom > 0 else None
    return {"t": t, "df": df, "p": student_t_sf_two_sided(t, df),
            "mean_a": ma, "mean_b": mb}


def cohens_d(a, b):
    """Cohen's d with the pooled standard deviation. None when it cannot be
    computed; 0.0 when both samples are constant (no spread, no effect)."""
    a = [float(x) for x in (a or [])]
    b = [float(x) for x in (b or [])]
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    va, vb = _variance(a) or 0.0, _variance(b) or 0.0
    pooled = ((na - 1) * va + (nb - 1) * vb) / float(na + nb - 2)
    if pooled <= 0:
        return 0.0
    return (_mean(a) - _mean(b)) / math.sqrt(pooled)


def fisher_exact_two_sided(a, b, c, d):
    """Two sided Fisher exact p for the 2x2 table [[a, b], [c, d]].

    Used by the BEST POST digest, where every cell is a single digit count and a
    normal approximation would be a lie. Exact hypergeometric enumeration over
    every table with the same margins, summing those no more likely than the one
    observed. Pure stdlib (math.comb), no scipy.

    Returns None on a degenerate table (nothing observed), 1.0 when a margin is
    empty (no evidence is possible, never a fabricated significance)."""
    try:
        a, b, c, d = int(a), int(b), int(c), int(d)
    except (TypeError, ValueError):
        return None
    if min(a, b, c, d) < 0:
        return None
    n = a + b + c + d
    if n <= 0:
        return None
    row1, row2, col1 = a + b, c + d, a + c
    col2 = b + d
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return 1.0
    denom = math.comb(n, col1)

    def _p(x):
        return (math.comb(row1, x) * math.comb(row2, col1 - x)) / denom

    p_obs = _p(a)
    lo, hi = max(0, col1 - row2), min(row1, col1)
    total = 0.0
    for x in range(lo, hi + 1):
        p = _p(x)
        # the classic two sided rule: every table at most as likely as observed,
        # with a relative tolerance so float noise does not drop the mirror table
        if p <= p_obs * (1.0 + 1e-9):
            total += p
    return min(1.0, total)


def cohens_h(p1, p2):
    """Cohen's h, the effect size for a difference of two PROPORTIONS.

    The digest compares shares (what fraction of the top posts carry this token
    vs what fraction of the rest do), so Cohen's d — a difference of means over a
    pooled sd — is the wrong measure. h is the arcsine transformed difference and
    is the standard one. None when either share is not a real number. Pure."""
    def _ok(p):
        return (isinstance(p, (int, float)) and not isinstance(p, bool)
                and 0.0 <= float(p) <= 1.0)

    if not _ok(p1) or not _ok(p2):
        return None
    return (2.0 * math.asin(math.sqrt(float(p1)))
            - 2.0 * math.asin(math.sqrt(float(p2))))


def benjamini_hochberg(pvalues, alpha=ALPHA):
    """Benjamini Hochberg FDR control across one run's tests.

    `pvalues` is a list where an entry may be None (a test that was not run).
    Returns (rejected, qvalues), both the same length and order as the input:
      rejected[i]  True when test i survives FDR control at `alpha`
      qvalues[i]   the BH adjusted p value (monotone), None where p was None
    Pure."""
    idx = [i for i, p in enumerate(pvalues or []) if isinstance(p, (int, float))
           and not isinstance(p, bool)]
    n = len(pvalues or [])
    rejected = [False] * n
    qvalues = [None] * n
    m = len(idx)
    if m == 0:
        return rejected, qvalues
    order = sorted(idx, key=lambda i: (pvalues[i], i))
    # adjusted p values, enforced monotone from the largest rank down
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        q = min(1.0, (m / float(rank)) * float(pvalues[i]))
        running = min(running, q)
        qvalues[i] = running
    # the classic step up rejection: largest k with p_(k) <= (k/m) * alpha
    k = 0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        if float(pvalues[i]) <= (rank / float(m)) * float(alpha):
            k = rank
            break
    for rank in range(1, k + 1):
        rejected[order[rank - 1]] = True
    return rejected, qvalues


# ---------------------------------------------------------------------------
# FORM derivation from caption text. Text goes IN, only band tokens come OUT.
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")
# Same ask families the copy gate and lever_stamp agree on.
_ASK_RE = re.compile(
    r"(link in bio|no sweat intro|free intro|book|schedule|sign ?up|dm us|"
    r"dm me|comment |claim|reserve|join us|get started|apply)", re.I)


def caption_len_band(caption):
    """short (<150) | mid (150-500) | long (>500), the lever_stamp bands, derived
    from the caption TEXT. None for an empty/absent caption. Returns a band
    token only; the text never leaves this function."""
    text = str(caption or "").strip()
    if not text:
        return None
    n = len(text)
    if n < 150:
        return "short"
    if n <= 500:
        return "mid"
    return "long"


def sentence_band(caption):
    """one_or_two | three_to_five | six_plus, from the caption TEXT. This is the
    STRUCTURE lever: how many beats the caption is broken into. None for an
    empty/absent caption. Returns a band token only."""
    text = str(caption or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text)]
    n = len([p for p in parts if p])
    if n <= 2:
        return "one_or_two"
    if n <= 5:
        return "three_to_five"
    return "six_plus"


def ask_present_from(caption, stored_ask_type=None):
    """yes | no. The STORED ask_type wins when it is a known token (it was
    stamped at draft time against the copy gate's own families); otherwise the
    caption text is scanned. None when neither source can answer."""
    stored = str(stored_ask_type or "").strip().lower()
    if stored in ALLOWED_LEVER_VALUES["ask_type"]:
        return "no" if stored == "none" else "yes"
    text = str(caption or "").strip()
    if not text:
        return None
    return "yes" if _ASK_RE.search(text) else "no"


def _token(value, lever):
    """The whitelisted token for `value` on `lever`, or None. Lowercased and
    underscored; anything outside the whitelist is DROPPED (never emitted)."""
    allowed = ALLOWED_LEVER_VALUES.get(lever)
    if allowed is None or value is None:
        return None
    if isinstance(value, bool):
        value = "yes" if value else "no"
    tok = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return tok if tok in allowed else None


# ---------------------------------------------------------------------------
# ingestion -> learnable posts (FORM tokens + score only)
# ---------------------------------------------------------------------------

def learnable_posts(gym_id, metric_rows, captions=None):
    """One gym's LEARNABLE posts as pure FORM records.

    Reuses monthly_retro.scoring_rows for the day 7 / day 28 merge and the
    learning_score scoring (lifted, not reinvented), then:
      * DROPS external=true and is_ad=true rows — the monthly_retro rail. Apify
        baseline / second publisher / boosted posts never train anything.
      * re-derives caption_len_band and sentence_band from the caption TEXT
        (the stored band is unreliable and usually null) and derives ask_present;
        the text itself is discarded here and never stored on the record.
      * maps every lever value through the whitelist; an unknown value becomes
        None (the post simply does not contribute to that lever).

    Returns (posts, excluded) where excluded counts the honesty rail drops.
    Pure over its inputs."""
    rows = [r for r in (metric_rows or []) if isinstance(r, dict)]
    raw_by_key = {monthly_retro.row_key(r): r for r in rows}
    captions = captions or {}
    scored = monthly_retro.scoring_rows(rows)

    posts = []
    excluded = {"external": 0, "is_ad": 0}
    for s in scored:
        if s.get("external"):
            excluded["external"] += 1
            continue
        if s.get("is_ad"):
            excluded["is_ad"] += 1
            continue
        raw = raw_by_key.get(s["key"]) or {}
        cal_id = raw.get("calendar_id")
        caption = captions.get(cal_id) if cal_id else None
        levers = {
            "hook_family": _token(s.get("hook_family"), "hook_family"),
            # derived from the TEXT first, stored band only as the fallback
            "caption_len_band": _token(
                caption_len_band(caption) or s.get("caption_len_band"),
                "caption_len_band"),
            "sentence_band": _token(sentence_band(caption), "sentence_band"),
            "pillar": _token(s.get("pillar"), "pillar"),
            "ask_type": _token(s.get("ask_type"), "ask_type"),
            "ask_present": _token(
                ask_present_from(caption, s.get("ask_type")), "ask_present"),
            "time_slot": _token(s.get("time_slot"), "time_slot"),
            "format": _token(s.get("format"), "format"),
            "media_product_type": _token(
                raw.get("media_product_type"), "media_product_type"),
            "has_member_face": _token(
                raw.get("has_member_face"), "has_member_face"),
        }
        posts.append({
            "gym_id": gym_id,
            "score": float(s["score"]),
            # the stratum every non format lever is compared within
            "stratum": levers["format"] or "unknown",
            "levers": levers,
        })
    return posts, excluded


def lever_cells(posts):
    """{(lever, stratum): {value: {"scores": [...], "gyms": set()}}}.

    Non format levers are FORMAT STRATIFIED (guard 5, carried over): reels
    compare against reels. The two format levers themselves are pooled under
    UNSTRATIFIED. Scores are log1p transformed here — engagement rates are right
    skewed and a t test on the raw rate is a t test on an outlier. Pure."""
    cells = {}
    for p in posts or []:
        levers = p.get("levers") or {}
        for lever in LEVERS:
            val = levers.get(lever)
            if not val:
                continue
            stratum = (UNSTRATIFIED if lever in FORMAT_LEVERS
                       else (p.get("stratum") or "unknown"))
            if stratum != UNSTRATIFIED and stratum not in _FORMATS:
                continue          # unknown stratum token: refuse to compare
            bucket = cells.setdefault((lever, stratum), {}).setdefault(
                val, {"scores": [], "gyms": set(), "by_gym": {}})
            bucket["scores"].append(math.log1p(max(0.0, float(p["score"]))))
            gym = p.get("gym_id")
            bucket["gyms"].add(gym)
            # per gym post counts, for the pseudoreplication guard in evaluate()
            bucket["by_gym"][gym] = bucket["by_gym"].get(gym, 0) + 1
    return cells


def qualified_gyms(by_gym, min_gym_n=MIN_GYM_CELL_N):
    """How many gyms contributed at least `min_gym_n` posts to a cell.

    This, not the raw count of distinct gyms, is what the cross gym floor is
    measured against: a gym that put ONE post in a cell is a token appearance,
    and counting it lets a single gym's cohort masquerade as a fleet finding.
    Pure."""
    return sum(1 for c in (by_gym or {}).values() if int(c or 0) >= int(min_gym_n))


def top_gym_share(by_gym):
    """The share of a cell supplied by its single largest contributing gym, or
    1.0 for an empty cell (an empty cell is maximally dominated, so the guard
    fails closed). A count ratio, never an engagement measure. Pure."""
    total = sum(int(c or 0) for c in (by_gym or {}).values())
    if total <= 0:
        return 1.0
    return max(int(c or 0) for c in by_gym.values()) / float(total)


# ---------------------------------------------------------------------------
# the tests + verdicts
# ---------------------------------------------------------------------------

def _round(v, places=4):
    return None if v is None else round(float(v), places)


def evaluate(cells, alpha=ALPHA, min_n=MIN_CELL_N, min_gyms=MIN_CELL_GYMS,
             min_effect=MIN_EFFECT, min_gym_n=MIN_GYM_CELL_N,
             max_gym_share=MAX_GYM_CELL_SHARE):
    """Every lever value against the rest of its lever, within its stratum.

    Verdicts:
      insufficient_data  either side below the sample floor (min_n), OR drawn
                         from fewer than min_gyms gyms that each contributed at
                         least min_gym_n posts, OR dominated by a single gym past
                         max_gym_share. THE CROSS GYM RULE: a finding one gym
                         alone could produce is a proxy for that gym's content and
                         is refused however large n is; the two per gym rules
                         close the "99 posts from one gym plus 1 from another"
                         hole in the plain distinct gym count.
      not_significant    tested, raw p >= alpha.
      directional        raw p < alpha but the finding does not survive the
                         Benjamini Hochberg FDR correction across the run, or the
                         effect size is below min_effect.
      supported          survives BH at alpha AND |Cohen's d| >= min_effect.

    NO MEASURED VALUE IS WRITTEN FOR A CELL THAT DID NOT QUALIFY FOR A TEST.
    An insufficient_data finding carries its counts and its verdict and nothing
    else: mean_log_engagement is None. That is a RE-IDENTIFICATION fix, not
    tidiness. Before it, a cell of n=1 from gyms=1 stored log1p of ONE
    identifiable post from ONE identifiable gym, and an unusual lever token
    ("summit", say) narrows that to a specific gym on a ~19 gym fleet, so the
    "no gym_id in the artifact" guarantee leaked through the measurement. An
    untested cell's mean is not actionable anyway; only its counts explain why it
    was refused.

    Returns a list of finding dicts, sorted deterministically. ONLY `supported`
    findings are eligible to become guidance. Pure."""
    findings = []
    for (lever, stratum), values in sorted(cells.items()):
        if len(values) < 2:
            continue              # nothing to compare this value against
        for value in sorted(values):
            cell = values[value]
            other_scores = []
            other_gyms = set()
            other_by_gym = {}
            for ov, oc in values.items():
                if ov == value:
                    continue
                other_scores.extend(oc["scores"])
                other_gyms |= oc["gyms"]
                for g, c in (oc.get("by_gym") or {}).items():
                    other_by_gym[g] = other_by_gym.get(g, 0) + c
            n_a, n_b = len(cell["scores"]), len(other_scores)
            g_a, g_b = len(cell["gyms"]), len(other_gyms)
            qa = qualified_gyms(cell.get("by_gym"), min_gym_n)
            qb = qualified_gyms(other_by_gym, min_gym_n)
            sa, sb = top_gym_share(cell.get("by_gym")), top_gym_share(other_by_gym)
            base = {
                "lever": lever, "format_stratum": stratum, "value": value,
                "n": n_a, "gyms": g_a, "n_other": n_b, "gyms_other": g_b,
                "gyms_qualified": qa, "gyms_qualified_other": qb,
                "top_gym_share": _round(sa), "top_gym_share_other": _round(sb),
            }
            if (n_a < min_n or n_b < min_n
                    or qa < min_gyms or qb < min_gyms
                    or sa > max_gym_share or sb > max_gym_share):
                # counts and the verdict only — see the re-identification note
                findings.append(dict(
                    base, mean_log_engagement=None,
                    mean_log_engagement_other=None,
                    effect_size=None, p_value=None, q_value=None,
                    verdict="insufficient_data"))
                continue
            res = welch_t_test(cell["scores"], other_scores)
            findings.append(dict(
                base,
                mean_log_engagement=_round(res["mean_a"]),
                mean_log_engagement_other=_round(res["mean_b"]),
                effect_size=_round(cohens_d(cell["scores"], other_scores)),
                p_value=_round(res["p"], 6), q_value=None,
                verdict="not_significant"))

    findings = findings[:MAX_FINDINGS]
    rejected, qvalues = benjamini_hochberg(
        [f.get("p_value") for f in findings], alpha=alpha)
    for i, f in enumerate(findings):
        if f["verdict"] == "insufficient_data":
            continue
        f["q_value"] = _round(qvalues[i], 6)
        p = f.get("p_value")
        d = f.get("effect_size")
        if p is None or p >= alpha:
            f["verdict"] = "not_significant"
        elif rejected[i] and d is not None and abs(d) >= min_effect:
            f["verdict"] = "supported"
        else:
            f["verdict"] = "directional"
    findings.sort(key=lambda f: (f["lever"], f["format_stratum"], f["value"]))
    return findings


def guidance_from(findings):
    """The FORM guidance items: one per `supported` finding, and nothing else.
    A directional, not_significant, or insufficient_data finding NEVER produces
    guidance — that is the whole point of the bar. Pure."""
    out = []
    for f in findings or []:
        if f.get("verdict") != "supported":
            continue
        d = f.get("effect_size")
        if d is None or d == 0:
            continue
        out.append({
            "lever": f["lever"],
            "value": f["value"],
            "format_stratum": f["format_stratum"],
            "direction": "favor" if d > 0 else "avoid",
            "effect_size": f.get("effect_size"),
            "n": f.get("n"),
            "gyms": f.get("gyms"),
            "q_value": f.get("q_value"),
            "source": SOURCE_LEVERS,
        })
    out.sort(key=lambda g: (-abs(g["effect_size"]), g["lever"], g["value"]))
    return out


# ---------------------------------------------------------------------------
# THE BEST POST DIGEST — "digest weekly the best post" (Blake, 2026-09-06)
# ---------------------------------------------------------------------------

TOP_POSTS_NOTE = ("top performers are described by whitelisted form tokens and "
                  "counts only: no caption text, permalink, post id or gym id "
                  "enters this section, and no top post's offer, stat or story "
                  "can reach another gym")


def split_top_posts(posts, top_fraction=TOP_FRACTION, min_n=MIN_CELL_N):
    """(top, rest) — the fleet's TOP PERFORMING posts and everything else.

    "Top performing" is the top `top_fraction` of the pooled engagement scores,
    never a hand pick. Two deliberate choices:

      * the cut is on a SCORE VALUE, not on a position: every post tied with the
        last one in is also in, so a tie at the boundary cannot make the split
        depend on input order.
      * BOTH sides must clear min_n or there is no split at all and ([], []) comes
        back — the caller then reports insufficient_data rather than describing
        four posts as a trend.

    Pure over its inputs; `posts` is not mutated."""
    rows = [p for p in (posts or []) if isinstance(p, dict)]
    n_all = len(rows)
    if n_all < 2 * int(min_n):
        return [], []
    ordered = sorted(rows, key=lambda p: -float(p.get("score") or 0.0))
    k = max(int(min_n), int(round(n_all * float(top_fraction))))
    k = min(k, n_all)
    cutoff = float(ordered[k - 1].get("score") or 0.0)
    top = [p for p in ordered if float(p.get("score") or 0.0) >= cutoff]
    rest = [p for p in ordered if float(p.get("score") or 0.0) < cutoff]
    if len(top) < int(min_n) or len(rest) < int(min_n):
        return [], []
    return top, rest


def _share(count, total):
    return None if not total else round(count / float(total), 4)


def top_posts_digest(posts, alpha=ALPHA, top_fraction=TOP_FRACTION,
                     min_n=MIN_CELL_N, min_gyms=MIN_CELL_GYMS,
                     min_effect=MIN_EFFECT, min_gym_n=MIN_GYM_CELL_N,
                     max_gym_share=MAX_GYM_CELL_SHARE):
    """What the fleet's BEST posts have in common, IN FORM — or an honest "no".

    For every whitelisted FORM token carried by at least one top post, a 2x2
    table (carries it / does not, in the top decile / in the rest) is tested with
    FISHER'S EXACT test, the effect size is Cohen's h, and Benjamini Hochberg is
    applied across the digest's own tests. A token becomes `supported` only when
    it clears ALL of:

      * the split itself exists (both sides at or above min_n), and
      * the top side draws on at least min_gyms DISTINCT gyms (the cross gym
        rule: a "best posts" profile one gym could produce alone is that gym's
        content wearing a statistic's clothes), and
      * this token appears in the top side across at least min_gyms gyms that
        EACH contributed min_gym_n or more of those posts, and no single gym
        supplies more than max_gym_share of them (the same pseudoreplication
        guard evaluate() applies), and
      * both rows of its own table clear min_n, and
      * it survives the FDR correction, and |Cohen's h| >= min_effect.

    Anything short of that is insufficient_data / not_significant / directional
    and produces NO guidance. `distinguishable` is False unless something is
    `supported`, and at the fleet's current volume False is the expected, correct
    answer: the artifact then SAYS the best posts are not distinguishable from
    noise instead of crowning one.

    Returns a dict of counts, whitelisted tokens and numbers. NOTHING from a post
    itself — no caption text, no permalink, no id, no gym id — is representable
    in the return value. Pure over its inputs."""
    top, rest = split_top_posts(posts, top_fraction=top_fraction, min_n=min_n)
    digest = {
        "n_top": len(top),
        "n_rest": len(rest),
        "gyms_top": len({p.get("gym_id") for p in top}),
        "gyms_rest": len({p.get("gym_id") for p in rest}),
        "top_fraction": float(top_fraction),
        "distinguishable": False,
        "verdict": "insufficient_data",
        "form": [],
        "note": TOP_POSTS_NOTE,
    }
    if not top or digest["gyms_top"] < int(min_gyms):
        return digest

    items = []
    for lever in LEVERS:
        allowed = ALLOWED_LEVER_VALUES[lever]
        # only posts that actually carry a value for this lever take part; a post
        # with no token here is silent, never counted as "does not carry it"
        top_vals = [(p.get("levers") or {}).get(lever) for p in top]
        rest_vals = [(p.get("levers") or {}).get(lever) for p in rest]
        top_known = [(p, v) for p, v in zip(top, top_vals) if v in allowed]
        rest_known = [(p, v) for p, v in zip(rest, rest_vals) if v in allowed]
        n_top_known, n_rest_known = len(top_known), len(rest_known)
        for value in sorted({v for _, v in top_known}):
            a = sum(1 for _, v in top_known if v == value)
            b = n_top_known - a
            c = sum(1 for _, v in rest_known if v == value)
            d = n_rest_known - c
            by_gym = {}
            for p, v in top_known:
                if v == value:
                    g = p.get("gym_id")
                    by_gym[g] = by_gym.get(g, 0) + 1
            gyms_here = len(by_gym)
            # the SAME pseudoreplication guard the lever tests use: a token that
            # is really one gym's habit must not be published as a fleet trait
            qual = qualified_gyms(by_gym, min_gym_n)
            dom = top_gym_share(by_gym)
            share_top, share_rest = _share(a, a + b), _share(c, c + d)
            base = {
                "lever": lever, "value": value, "format_stratum": UNSTRATIFIED,
                "n": a, "gyms": gyms_here, "n_other": c,
                "gyms_qualified": qual, "top_gym_share": _round(dom),
            }
            if (qual < int(min_gyms) or dom > float(max_gym_share)
                    or (a + b) < int(min_n) or (c + d) < int(min_n)):
                # counts only, no measured shares — the same re-identification
                # rule evaluate() applies to an untested cell
                items.append(dict(base, share_top=None, share_rest=None,
                                  effect_size=None, p_value=None,
                                  q_value=None, verdict="insufficient_data"))
                continue
            items.append(dict(
                base, share_top=share_top, share_rest=share_rest,
                effect_size=_round(cohens_h(share_top, share_rest)),
                p_value=_round(fisher_exact_two_sided(a, b, c, d), 6),
                q_value=None, verdict="not_significant"))

    items = items[:MAX_TOP_FINDINGS]
    rejected, qvalues = benjamini_hochberg(
        [i.get("p_value") for i in items], alpha=alpha)
    tested = False
    for i, item in enumerate(items):
        if item["verdict"] == "insufficient_data":
            continue
        tested = True
        item["q_value"] = _round(qvalues[i], 6)
        p, h = item.get("p_value"), item.get("effect_size")
        if p is None or p >= alpha:
            item["verdict"] = "not_significant"
        elif rejected[i] and h is not None and abs(h) >= min_effect:
            item["verdict"] = "supported"
        else:
            item["verdict"] = "directional"
    items.sort(key=lambda i: (i["lever"], i["value"]))
    digest["form"] = items
    digest["distinguishable"] = any(i["verdict"] == "supported" for i in items)
    if digest["distinguishable"]:
        digest["verdict"] = "supported"
    elif tested:
        digest["verdict"] = "not_significant"
    return digest


def guidance_from_top_posts(digest):
    """The FORM guidance items the BEST POST digest earned, and nothing else.

    Same shape as guidance_from's items so agent/cross_gym_guidance re-validates
    both through the identical whitelist, tagged source=top_posts so a reader can
    tell the weaker claim apart. `favor` when the token is OVER represented among
    the top posts, `avoid` when it is under represented. Pure."""
    out = []
    for item in (digest or {}).get("form") or []:
        if item.get("verdict") != "supported":
            continue
        h = item.get("effect_size")
        if h is None or h == 0:
            continue
        out.append({
            "lever": item["lever"],
            "value": item["value"],
            "format_stratum": item.get("format_stratum", UNSTRATIFIED),
            "direction": "favor" if h > 0 else "avoid",
            "effect_size": h,
            "n": item.get("n"),
            "gyms": item.get("gyms"),
            "q_value": item.get("q_value"),
            "source": SOURCE_TOP_POSTS,
        })
    out.sort(key=lambda g: (-abs(g["effect_size"]), g["lever"], g["value"]))
    return out


# ---------------------------------------------------------------------------
# the form only verifier (whitelist, applied to the FINISHED artifact)
# ---------------------------------------------------------------------------

CONTEXT_NOTE = ("apify social_baseline measures the public feed before and "
                "outside echo: reporting context only, excluded from every test")

_ALLOWED_KEYS = frozenset((
    "run_at", "window", "start", "end", "days", "fleet", "gyms_contributing",
    "posts_analyzed", "posts_excluded_external", "posts_excluded_is_ad",
    "gyms_seen", "thresholds", "min_cell_n", "min_cell_gyms", "alpha",
    "min_effect", "correction", "window_days", "findings", "guidance",
    "lever", "format_stratum", "value", "n", "gyms", "n_other", "gyms_other",
    "mean_log_engagement", "mean_log_engagement_other", "effect_size",
    "p_value", "q_value", "verdict", "direction", "reporting_context",
    "source", "gyms_with_baseline", "used_for_learning", "note",
    # THE BEST POST DIGEST. Counts, shares and whitelisted form tokens only —
    # there is deliberately no key here for caption text, a permalink, a post id
    # or a gym id, so such a value has nowhere legal to sit in the artifact.
    "top_posts", "n_top", "n_rest", "gyms_top", "gyms_rest", "top_fraction",
    "share_top", "share_rest", "distinguishable", "form", "test",
    # the pseudoreplication guard's reported counts (count ratios, never an
    # engagement measure) — they are what explains WHY a cell was refused
    "gyms_qualified", "gyms_qualified_other", "top_gym_share",
    "top_gym_share_other", "min_gym_cell_n", "max_gym_cell_share",
))

_ALLOWED_STRINGS = frozenset(
    set(LEVERS)
    | {v for vals in ALLOWED_LEVER_VALUES.values() for v in vals}
    | set(VERDICTS) | set(DIRECTIONS) | set(_ALLOWED_KEYS)
    | {UNSTRATIFIED, "benjamini_hochberg", "apify social_baseline",
       "fisher_exact", SOURCE_LEVERS, SOURCE_TOP_POSTS,
       CONTEXT_NOTE, TOP_POSTS_NOTE}
)

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}[\d.:+Z-]*)?$")


def form_only_violations(node, path="artifact"):
    """Every string in the artifact must be a whitelisted token, an ISO
    date/timestamp, or one of this module's fixed notes. Anything else is a
    violation and the run REFUSES to write.

    This is a whitelist, not a scan for bad words: a caption fragment, a member
    name, a handle, a price, a stat, or a free text "pillar" cannot pass it,
    because passing requires being a known token. Returns a list of
    "path: value" strings; empty means clean. Pure."""
    bad = []
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str) or k not in _ALLOWED_KEYS:
                bad.append(f"{path}.{k}: key not whitelisted")
                continue
            bad.extend(form_only_violations(v, f"{path}.{k}"))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            bad.extend(form_only_violations(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        if node not in _ALLOWED_STRINGS and not _ISO_RE.match(node):
            bad.append(f"{path}: {node[:40]!r} not whitelisted")
    elif node is None or isinstance(node, (bool, int, float)):
        pass
    else:
        bad.append(f"{path}: {type(node).__name__} not a form only value")
    return bad


# ---------------------------------------------------------------------------
# default store (PostgREST; injectable) — READ ONLY except the rollup insert
# ---------------------------------------------------------------------------

class SupabaseBrainStore:
    """post_metrics + content_calendar caption + social_baseline reads, and the
    append only cross_gym_brain insert. `http` injectable (the metrics_sync /
    monthly_retro store pattern)."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = (service_key if service_key is not None
                     else config.supabase_service_key())
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, repo pattern
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _get(self, table, params):
        r = self._client().get(f"{self._url}/rest/v1/{table}", params=params,
                               headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            # LOG IT. Returning [] on an error made a broken read look exactly
            # like an empty table, which is the one thing the read path must be
            # able to tell apart: an armed brain whose migration is missing reads
            # 400 forever and, silently, is byte-for-byte a dormant brain. The
            # status code only; no response body, so no row content is ever
            # printed. The caller still degrades to empty (a read failure must
            # never break a draft run), it is simply no longer invisible.
            print(f"[cross-gym-brain] read of {table} failed with HTTP "
                  f"{r.status_code}; treating as empty")
            return []
        return r.json() or []

    def metrics_window(self, gym_id, start_iso, end_iso):
        """One gym's post_metrics rows published inside the window. READ ONLY."""
        return self._get("post_metrics", {
            "gym_id": f"eq.{gym_id}",
            "published_at": [f"gte.{start_iso}", f"lt.{end_iso}"],
            "order": "published_at"})

    def captions_for(self, gym_id, calendar_ids):
        """{calendar_id -> caption} for the gym's matched rows. READ ONLY, and
        the text is used ONLY to derive length/sentence bands before being
        discarded (see learnable_posts)."""
        ids = [str(i) for i in (calendar_ids or []) if i]
        out = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            rows = self._get("content_calendar", {
                "gym_id": f"eq.{gym_id}",
                "id": "in.(" + ",".join(chunk) + ")",
                "select": "id,caption"})
            for r in rows:
                if r.get("id"):
                    out[r["id"]] = r.get("caption")
        return out

    def baseline_gym_count(self):
        """How many gyms have a stored Apify social_baseline row. A COUNT only —
        no measure and no gym id ever enters the artifact."""
        rows = self._get("social_baseline", {"select": "gym_id"})
        return len({r.get("gym_id") for r in rows if r.get("gym_id")})

    def insert_rollup(self, row):
        """APPEND ONLY: a plain INSERT, never an upsert. History is the point."""
        r = self._client().post(
            f"{self._url}/rest/v1/cross_gym_brain",
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=row, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"cross_gym_brain insert failed: {r.status_code}")
        out = r.json() or []
        return out[0] if out else row

    def latest_rollup(self):
        """The newest cross_gym_brain row, or None. READ ONLY (the read API)."""
        rows = self._get("cross_gym_brain", {
            "order": "run_at.desc", "limit": "1"})
        return rows[0] if rows else None


def _default_gyms():
    """The whole fleet (the metrics_sync roster)."""
    from agent.metrics_sync import _default_gyms as _roster
    return _roster()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def build_artifact(findings, fleet, window, thresholds, baseline_gyms,
                   top_posts=None):
    """The stored rollup artifact. Built ONLY from whitelisted tokens and
    numbers — there is no code path here that copies text from a post.

    `guidance` is the union of the lever findings' guidance and the BEST POST
    digest's guidance, each item tagged with the `source` it came from. Both
    halves went through the same bar and are re-validated through the same
    whitelist by agent/cross_gym_guidance on the way back out."""
    top_posts = top_posts or {}
    return {
        "run_at": window["run_at"],
        "window": {"start": window["start"], "end": window["end"],
                   "days": window["days"]},
        "fleet": {
            "gyms_seen": int(fleet.get("gyms_seen") or 0),
            "gyms_contributing": int(fleet.get("gyms_contributing") or 0),
            "posts_analyzed": int(fleet.get("posts_analyzed") or 0),
            "posts_excluded_external": int(fleet.get("external") or 0),
            "posts_excluded_is_ad": int(fleet.get("is_ad") or 0),
        },
        "thresholds": thresholds,
        "findings": findings,
        "top_posts": top_posts,
        "guidance": guidance_from(findings) + guidance_from_top_posts(top_posts),
        "reporting_context": {
            "source": "apify social_baseline",
            "gyms_with_baseline": int(baseline_gyms or 0),
            "used_for_learning": False,
            "note": CONTEXT_NOTE,
        },
    }


def run(gyms=None, now=None, store=None, window_days=None, alpha=ALPHA,
        write=True):
    """The nightly cross gym brain.

    Behind AGENT_CROSS_GYM_BRAIN (default OFF -> no-op: no store constructed,
    nothing read, nothing written, zero behavior change). Fully injectable.

    Returns {"ok", "reason", ...}. On a successful run the payload carries the
    artifact's findings and guidance so the caller can log without a re-read."""
    if not config.cross_gym_brain_enabled():
        return {"ok": False,
                "reason": "AGENT_CROSS_GYM_BRAIN is OFF (default). "
                          "No cross gym rollup performed.",
                "findings": [], "guidance": []}
    now = now or datetime.now(timezone.utc)
    window_days = int(window_days or WINDOW_DAYS)
    store = store or SupabaseBrainStore()
    gyms = list(gyms) if gyms else _default_gyms()

    start = (now - timedelta(days=window_days)).date().isoformat()
    end = now.date().isoformat()

    posts = []
    excluded = {"external": 0, "is_ad": 0}
    contributing = set()
    for gym_id in gyms:
        try:
            rows = store.metrics_window(gym_id, start, end) or []
        except Exception as exc:  # noqa: BLE001 — one gym never fails the fleet
            print(f"[cross-gym-brain] metrics read failed for {gym_id}: "
                  f"{type(exc).__name__}")
            continue
        cal_ids = sorted({r.get("calendar_id") for r in rows
                          if isinstance(r, dict) and r.get("calendar_id")})
        try:
            captions = store.captions_for(gym_id, cal_ids) if cal_ids else {}
        except Exception as exc:  # noqa: BLE001 — no captions: bands stay null
            print(f"[cross-gym-brain] caption read failed for {gym_id}: "
                  f"{type(exc).__name__}")
            captions = {}
        gym_posts, gym_excluded = learnable_posts(gym_id, rows, captions)
        excluded["external"] += gym_excluded["external"]
        excluded["is_ad"] += gym_excluded["is_ad"]
        if gym_posts:
            contributing.add(gym_id)
            posts.extend(gym_posts)

    findings = evaluate(lever_cells(posts), alpha=alpha)
    top_posts = top_posts_digest(posts, alpha=alpha)
    try:
        baseline_gyms = store.baseline_gym_count()
    except Exception:  # noqa: BLE001 — context only, never fails the run
        baseline_gyms = 0

    artifact = build_artifact(
        findings,
        {"gyms_seen": len(gyms), "gyms_contributing": len(contributing),
         "posts_analyzed": len(posts), **excluded},
        {"run_at": now.isoformat(), "start": start, "end": end,
         "days": window_days},
        {"min_cell_n": MIN_CELL_N, "min_cell_gyms": MIN_CELL_GYMS,
         "min_gym_cell_n": MIN_GYM_CELL_N,
         "max_gym_cell_share": MAX_GYM_CELL_SHARE,
         "alpha": float(alpha), "min_effect": MIN_EFFECT,
         "correction": "benjamini_hochberg", "window_days": window_days,
         "top_fraction": TOP_FRACTION, "test": "fisher_exact"},
        baseline_gyms,
        top_posts,
    )

    violations = form_only_violations(artifact)
    if violations:
        # Refuse to write rather than ship anything that is not pure form.
        # `failed` (not merely ok=False) so runner.run_daily raises a human
        # ALERT: a violation means something upstream is producing non form
        # values, which is exactly the condition that must not pass unnoticed.
        print(f"[cross-gym-brain] REFUSED: {len(violations)} form only "
              f"violation(s); nothing written")
        return {"ok": False, "failed": True,
                "reason": "form only whitelist violation; nothing written",
                "violations": violations[:10], "findings": [], "guidance": []}

    row = {
        "run_at": artifact["run_at"],
        "window_start": start,
        "window_end": end,
        "window_days": window_days,
        "gyms_contributing": artifact["fleet"]["gyms_contributing"],
        "posts_analyzed": artifact["fleet"]["posts_analyzed"],
        "findings": artifact["findings"],
        "top_posts": artifact["top_posts"],
        "guidance": artifact["guidance"],
        "context": artifact["reporting_context"],
    }
    wrote = False
    if write:
        try:
            store.insert_rollup(row)
            wrote = True
        except Exception as exc:  # noqa: BLE001
            # `failed` so runner.run_daily ALERTS a human. Before this flag the
            # runner could only tell "ok" from "not ok" and printed every not ok
            # as a benign "skipped", so a real write failure — the 400 a premature
            # arming produces when the top_posts migration has not been applied —
            # alerted nobody and looked exactly like a dormant flag.
            print(f"[cross-gym-brain] rollup write failed: "
                  f"{type(exc).__name__}")
            return {"ok": False, "failed": True,
                    "reason": f"rollup write failed: {type(exc).__name__}",
                    "findings": artifact["findings"],
                    "guidance": artifact["guidance"]}
    return {
        "ok": True,
        "reason": "",
        "wrote": wrote,
        "window": artifact["window"],
        "fleet": artifact["fleet"],
        "findings": artifact["findings"],
        "top_posts": artifact["top_posts"],
        "guidance": artifact["guidance"],
    }


# ---------------------------------------------------------------------------
# WEEKLY cadence (Blake, 2026-09-06: "digest weekly")
# ---------------------------------------------------------------------------

def iso_week_key(now=None):
    """The ISO year and week `now` falls in, e.g. "2026-W36". Pure.

    An ISO week key rather than a fixed weekday on purpose: a missed nightly tick
    (a deploy, an outage) must not skip a whole week's digest. Whichever nightly
    tick lands first in a new ISO week fires the run."""
    now = now or datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{int(week):02d}"


def run_weekly(now=None, force=False, kv_get=None, kv_set=None, **kwargs):
    """The scheduled entry point: run() at most ONCE per ISO week.

    runner.run_daily calls THIS every night; six nights out of seven it returns
    without constructing a store, without a single fleet read and without a
    write. `force=True` is the manual override for a human running the job by
    hand out of band.

    The marker is one kv row (WEEK_KV_KEY) holding the ISO week key of the last
    COMPLETED run, and it is stamped only after run() reports ok — a failed or
    refused run does not burn the week. kv_get/kv_set are injectable so the whole
    cadence is unit tested with no database.

    Every safety rail is run()'s and is unchanged by the cadence: the flag check,
    the FORM ONLY whitelist, the sample and distinct gym floors, and the refuse
    to write on violation all still happen inside run()."""
    if not config.cross_gym_brain_enabled():
        return {"ok": False,
                "reason": "AGENT_CROSS_GYM_BRAIN is OFF (default). "
                          "No cross gym rollup performed.",
                "findings": [], "guidance": [], "ran": False}
    now = now or datetime.now(timezone.utc)
    week = iso_week_key(now)
    if kv_get is None or kv_set is None:
        from agent import db as _db
        kv_get = kv_get or (lambda k: _db.kv_get(k))
        kv_set = kv_set or (lambda k, v: _db.kv_set(k, v))
    try:
        last = kv_get(WEEK_KV_KEY)
    except Exception as exc:  # noqa: BLE001 — an unreadable marker is not a run
        return {"ok": False, "ran": False, "failed": True,
                "reason": f"cadence marker unreadable: {type(exc).__name__}",
                "findings": [], "guidance": []}
    if not force and str(last or "") == week:
        return {"ok": True, "ran": False, "week": week,
                "reason": f"cross gym brain already ran in ISO week {week}; "
                          f"the digest is weekly",
                "findings": [], "guidance": []}
    out = run(now=now, **kwargs)
    out["ran"] = True
    out["week"] = week
    if out.get("ok"):
        try:
            kv_set(WEEK_KV_KEY, week)
        except Exception as exc:  # noqa: BLE001 — a lost marker re-runs, never lies
            print(f"[cross-gym-brain] cadence marker not stamped: "
                  f"{type(exc).__name__}")
    return out


if __name__ == "__main__":
    print(json.dumps(run_weekly(force=True), indent=2, default=str))
