"""
social_baseline.py — BEFORE/AFTER social metrics from the PUBLIC Instagram feed
(flag AGENT_SOCIAL_BASELINE, default OFF).

Blake's ask (2026-08-28): every gym's social metrics should show the feed BEFORE
Echo started posting for them vs AFTER — measured the same way, from the public
Instagram feed via Apify, so the comparison is apples-to-apples and undeniable.

This module is DISTINCT from agent/baseline.py (the Graph-API posts-per-week
pre-Echo lock). This one mirrors the LASSO Social Report Card rubric over the
public feed, per 90-day window:

  posts count, posts/week, longest gap days, reels/video share %, total video
  plays, median plays per reel, median caption length, posts carrying an ask
  (count + %), duplicate-caption count.

MEASUREMENT HONESTY (the Report Card's own rules, enforced here):
  - "Plays" is Instagram's videoPlayCount, for reels only. One source, one
    field, named. Never estimated, never mixed with views or reach.
  - Pinned posts are excluded (skipPinnedPosts server-side + isPinned dropped
    defensively in compute_measures).
  - A measure a window cannot back is None, never a fabricated 0: no posts ->
    no medians/percentages; no reel with a play count -> plays are None.
  - Longest gap includes the window edges (window_start -> first post and
    last post -> window_end), so a dark month at either edge is a finding,
    not an artifact. Zero posts -> the gap is the whole window.
  - Duplicate captions: posts whose normalized caption (lowercased,
    whitespace-collapsed, non-empty) already appeared earlier in the window.

BASELINE IMMUTABILITY: a gym's baseline (the 90 days ending at its Echo start)
is captured ONCE and never recaptured or overwritten — that is the whole point
of a "before". capture_baseline refuses when a row exists; the insert is a
plain POST (no upsert), so even a race dies on the primary key.

ECHO START: the gym's first PUBLISHED content_calendar row date; falls back to
the gym's first calendar row date (Echo's first planned day); honest None when
the gym has no calendar history at all — then there is no before window and the
gym is skipped with a reason, never a guess.

INERT WITHOUT APIFY_TOKEN: every network path checks the token first and
returns {"ok": False, "reason": "APIFY_TOKEN not set; social baseline is
inert"} — never a crash, never a fabricated number. The token is read from env
at call time and never logged, printed, or stored in any row.

READ-ONLY on the social side: this module only READS the public Instagram feed
through Apify and writes to the social_baseline table. Nothing here publishes,
approves, or touches any account. Tenant-scoped: every read and write carries
the gym filter; one gym's baseline can never mix with another's.

COST: Apify's instagram-post-scraper is pay-per-result (~$1.50–$2.70 per 1,000
items). A 90-day gym pull is a couple hundred items at most — well under a
dollar per gym per run.

All I/O is injectable (Apify client, calendar store, baseline store, today) so
the whole path is unit-tested offline with fixture posts.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

from . import config

WINDOW_DAYS = 90

# Apify actor (proven contract — the Report Card skill's Path A).
APIFY_ACTOR_ID = "apify~instagram-post-scraper"
APIFY_RUN_SYNC_URL = (
    "https://api.apify.com/v2/acts/" + APIFY_ACTOR_ID + "/run-sync-get-dataset-items"
)
# Generous ceiling for a 90-day boutique-gym feed; the onlyPostsNewerThan filter
# bounds the pull server-side, this only caps a pathological account.
RESULTS_LIMIT = 500

# Ask detection (the Report Card's regex, verbatim). Tuned per gym on the card;
# here it is applied identically to both windows so the comparison is fair even
# where the regex is imperfect.
ASK_RE = re.compile(
    r"(link in bio|no sweat intro|book|schedule|sign ?up|dm us|dm me|"
    r"free (?:class|intro|trial|week|consult)|register|claim|call us|text us|"
    r"click|apply|get started|join us|reach out)",
    re.IGNORECASE,
)

# The numeric measure keys deltas are computed over, in report order.
MEASURE_KEYS = (
    "posts_count",
    "posts_per_week",
    "longest_gap_days",
    "reels_share_pct",
    "total_video_plays",
    "median_plays_per_reel",
    "median_caption_length",
    "posts_with_ask",
    "ask_pct",
    "duplicate_caption_count",
)

_MEASURE_LABELS = {
    "posts_count": "posts",
    "posts_per_week": "posts/week",
    "longest_gap_days": "longest gap (days)",
    "reels_share_pct": "reel/video share %",
    "total_video_plays": "video plays (total)",
    "median_plays_per_reel": "median plays/reel",
    "median_caption_length": "median caption length",
    "posts_with_ask": "posts with an ask",
    "ask_pct": "ask %",
    "duplicate_caption_count": "duplicate captions",
}


class ApifyError(Exception):
    """An Apify call failed. The message never carries the token."""


# ---------------------------------------------------------------------------
# pure measurement core
# ---------------------------------------------------------------------------


def _as_date(v):
    """Coerce a date / datetime / ISO string to a date. None stays None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _post_ts(post):
    """A post's timestamp as an aware UTC datetime, or None when unparseable.
    Apify emits ISO strings like 2026-05-01T12:00:00.000Z."""
    raw = (post or {}).get("timestamp")
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_reel(post):
    """Reel/video: productType 'clips' (a reel) or type 'Video'. Carousels and
    images are not, even when a carousel contains a video slide (Apify does not
    expose per-slide play counts, so counting it would fabricate)."""
    pt = str((post or {}).get("productType") or "").lower()
    if pt == "clips":
        return True
    return str((post or {}).get("type") or "").lower() == "video"


def _norm_caption(caption):
    """Lowercased, whitespace-collapsed caption for duplicate detection.
    Empty (or whitespace-only) captions never count as duplicates of each
    other — two postless captions are absence, not repetition."""
    return " ".join(str(caption or "").lower().split())


def _median(values):
    vals = sorted(values)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def compute_measures(posts, window_start, window_end):
    """The rubric over raw Apify post dicts, for the window [start, end).

    Pure: no I/O, no env, no clock. `posts` is the raw item list from
    apify/instagram-post-scraper; window_start/window_end are dates (or ISO
    strings). A post counts when window_start <= timestamp.date() < window_end.
    Pinned posts and posts without a parseable timestamp are excluded.

    Returns a dict of the MEASURE_KEYS plus window_start / window_end /
    window_days. Every measure a window cannot back is None, never 0.
    """
    start = _as_date(window_start)
    end = _as_date(window_end)
    if start is None or end is None or end <= start:
        raise ValueError("compute_measures needs window_start < window_end")
    window_days = (end - start).days

    in_window = []
    for p in posts or []:
        if (p or {}).get("isPinned"):
            continue
        ts = _post_ts(p)
        if ts is None:
            continue
        d = ts.date()
        if start <= d < end:
            in_window.append((d, p))
    in_window.sort(key=lambda t: t[0])

    n = len(in_window)
    posts_per_week = round(n / (window_days / 7.0), 2)

    # Longest gap, edges included. Zero posts -> the whole window is the gap.
    if n == 0:
        longest_gap = window_days
    else:
        days = [d for d, _ in in_window]
        gaps = [(days[0] - start).days]
        gaps.extend((days[i] - days[i - 1]).days for i in range(1, n))
        gaps.append((end - days[-1]).days)
        longest_gap = max(gaps)

    reels = [p for _, p in in_window if _is_reel(p)]
    reels_share_pct = round(100.0 * len(reels) / n, 1) if n else None

    # Plays: videoPlayCount, reels only, known values only. int/float but not bool.
    plays = [
        p.get("videoPlayCount")
        for p in reels
        if isinstance(p.get("videoPlayCount"), (int, float))
        and not isinstance(p.get("videoPlayCount"), bool)
    ]
    total_plays = int(sum(plays)) if plays else None
    median_plays = _median(plays)

    caption_lengths = [len(str(p.get("caption") or "")) for _, p in in_window]
    median_caption_len = _median(caption_lengths) if n else None

    asks = sum(1 for _, p in in_window if ASK_RE.search(str(p.get("caption") or "")))
    ask_pct = round(100.0 * asks / n, 1) if n else None

    seen, dupes = set(), 0
    for _, p in in_window:
        norm = _norm_caption(p.get("caption"))
        if not norm:
            continue
        if norm in seen:
            dupes += 1
        else:
            seen.add(norm)

    return {
        "posts_count": n,
        "posts_per_week": posts_per_week,
        "longest_gap_days": longest_gap,
        "reels_share_pct": reels_share_pct,
        "total_video_plays": total_plays,
        "median_plays_per_reel": median_plays,
        "median_caption_length": median_caption_len,
        "posts_with_ask": asks,
        "ask_pct": ask_pct,
        "duplicate_caption_count": dupes,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_days": window_days,
    }


# ---------------------------------------------------------------------------
# echo start date
# ---------------------------------------------------------------------------


def echo_start_date(gym, store=None):
    """The gym's Echo start: the date of its first PUBLISHED content_calendar
    row; falls back to the first calendar row of any status (Echo's first
    planned day); honest None when the gym has no calendar history at all."""
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    first_published = store.first_calendar_date(gym, status="published")
    if first_published:
        return _as_date(first_published)
    return _as_date(store.first_calendar_date(gym))


# ---------------------------------------------------------------------------
# Apify client (thin, injectable)
# ---------------------------------------------------------------------------


def apify_token():
    """APIFY_TOKEN from env at call time. Never logged, never stored."""
    return os.environ.get("APIFY_TOKEN", "").strip()


class ApifyClient:
    """run-sync wrapper over apify/instagram-post-scraper. `http` is injectable
    (defaults to lazy `requests`, the repo pattern) so all logic tests offline."""

    def __init__(self, token=None, http=None):
        self._token = token
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, matches the repo pattern
        return requests

    def token(self):
        return self._token if self._token is not None else apify_token()

    def fetch_posts(self, handle, newer_than_days, results_limit=RESULTS_LIMIT):
        """Public feed posts for @handle newer than N days. Raises ApifyError on
        any failure; the token never appears in the error."""
        tok = self.token()
        if not tok:
            raise ApifyError("APIFY_TOKEN not set")
        handle = str(handle or "").strip().lstrip("@")
        if not handle:
            raise ApifyError("empty instagram handle")
        payload = {
            "username": [handle],
            "resultsLimit": int(results_limit),
            "skipPinnedPosts": True,
            "onlyPostsNewerThan": f"{int(newer_than_days)} days",
            # must be exactly basicData|detailedData ("detailed" fails validation)
            "dataDetailLevel": "detailedData",
        }
        r = self._client().post(
            APIFY_RUN_SYNC_URL,
            params={"token": tok},
            json=payload,
            timeout=600,
        )
        if r.status_code >= 400:
            detail = str(getattr(r, "text", "") or "")[:200].replace(tok, "***")
            raise ApifyError(f"apify {r.status_code}: {detail}")
        items = r.json()
        if not isinstance(items, list):
            raise ApifyError("apify returned a non-list dataset")
        return items


# ---------------------------------------------------------------------------
# baseline storage (Supabase social_baseline, PostgREST)
# ---------------------------------------------------------------------------

_TABLE = "social_baseline"


class BaselineStore:
    """PostgREST client over social_baseline. Insert-once by design: the write
    is a plain POST (no upsert), so a second capture dies on the primary key.
    `http` injectable; the service key is read at call time and never logged."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = (service_key if service_key is not None
                     else config.supabase_service_key())
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy
        return requests

    def _headers(self, extra=None):
        h = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _rest(self):
        return f"{self._url}/rest/v1/{_TABLE}"

    def _scrub(self, text):
        if self._key and text:
            text = text.replace(self._key, "***")
        return text

    def get(self, gym_id):
        """The gym's stored baseline row, or None. Gym-scoped."""
        r = self._client().get(
            self._rest(),
            params={"gym_id": f"eq.{gym_id}", "limit": "1"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise ApifyError(
                f"social_baseline read {r.status_code}: "
                f"{self._scrub(str(getattr(r, 'text', '') or '')[:200])}")
        rows = r.json() or []
        return rows[0] if rows else None

    def insert_once(self, row):
        """INSERT the baseline row. (ok, reason). A duplicate (409 / PK
        conflict) is refused — baselines are immutable once captured."""
        r = self._client().post(
            self._rest(),
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json=row,
            timeout=30,
        )
        if r.status_code == 409:
            return False, "baseline already captured (immutable); refusing overwrite"
        if r.status_code >= 400:
            return False, (
                f"social_baseline write {r.status_code}: "
                f"{self._scrub(str(getattr(r, 'text', '') or '')[:200])}")
        return True, "stored"


# ---------------------------------------------------------------------------
# capture / compare
# ---------------------------------------------------------------------------


def _resolve_handle(gym, handle, store):
    """The gym's Instagram handle: an explicit override, else the live-truth
    `handle` the reverify sweep stamps on echo_social_connections. Never
    guessed; None -> honest skip upstream."""
    h = str(handle or "").strip().lstrip("@")
    if h:
        return h
    got = store.social_connection_handle(gym, platform="instagram")
    return str(got or "").strip().lstrip("@") or None


def capture_baseline(gym, handle=None, *, client=None, store=None,
                     baseline_store=None, today=None):
    """Capture the gym's BEFORE window (the 90 days ending at its Echo start)
    from the public feed via Apify and store it ONCE. Immutable: an existing
    baseline is never recaptured or overwritten. Returns a dict with ok /
    captured / reason (+ the row when stored)."""
    gym = str(gym or "").strip()
    if not gym:
        return {"ok": False, "gym": gym, "reason": "empty gym base"}
    if client is None:
        client = ApifyClient()
    if not client.token():
        return {"ok": False, "gym": gym,
                "reason": "APIFY_TOKEN not set; social baseline is inert"}
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    if baseline_store is None:
        baseline_store = BaselineStore()
    today = _as_date(today) or datetime.now(timezone.utc).date()

    existing = baseline_store.get(gym)
    if existing:
        return {"ok": True, "gym": gym, "captured": False,
                "reason": "baseline already captured (immutable)",
                "row": existing}

    ig = _resolve_handle(gym, handle, store)
    if not ig:
        return {"ok": False, "gym": gym, "captured": False,
                "reason": ("no instagram handle on file "
                           "(echo_social_connections); skipped")}

    start = echo_start_date(gym, store=store)
    if start is None:
        return {"ok": False, "gym": gym, "captured": False,
                "reason": ("no content_calendar history; echo start unknown, "
                           "no before window; skipped")}

    window_end = start
    window_start = start - timedelta(days=WINDOW_DAYS)
    if window_end > today:
        # Echo start in the future (planned month not yet live): the before
        # window would include days that have not happened. Clamp honestly.
        return {"ok": False, "gym": gym, "captured": False,
                "reason": f"echo start {start.isoformat()} is in the future; "
                          "capture once posting has actually started"}

    days_back = (today - window_start).days + 1
    try:
        posts = client.fetch_posts(ig, days_back)
    except ApifyError as exc:
        return {"ok": False, "gym": gym, "captured": False,
                "reason": f"apify pull failed: {exc}"}

    measures = compute_measures(posts, window_start, window_end)
    row = {
        "gym_id": gym,
        "ig_handle": ig,
        "echo_start": start.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "measures": measures,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    ok, reason = baseline_store.insert_once(row)
    return {"ok": ok, "gym": gym, "captured": ok, "reason": reason,
            "row": row if ok else None}


def before_after(gym, handle=None, *, client=None, store=None,
                 baseline_store=None, today=None):
    """The comparison: stored (immutable) baseline vs a fresh Apify pull of the
    LAST 90 whole days, same rubric. Returns {before, after, deltas} — deltas
    per measure, honest None where either window lacks the number."""
    gym = str(gym or "").strip()
    if not gym:
        return {"ok": False, "gym": gym, "reason": "empty gym base"}
    if client is None:
        client = ApifyClient()
    if not client.token():
        return {"ok": False, "gym": gym,
                "reason": "APIFY_TOKEN not set; social baseline is inert"}
    if baseline_store is None:
        baseline_store = BaselineStore()
    today = _as_date(today) or datetime.now(timezone.utc).date()

    base_row = baseline_store.get(gym)
    if not base_row:
        return {"ok": False, "gym": gym,
                "reason": "no baseline captured; run social-before-after --capture first"}

    # Same feed as the baseline: the stored handle wins so before and after
    # measure the SAME account even if the connection row changed since.
    ig = str(base_row.get("ig_handle") or "").strip().lstrip("@")
    if not ig:
        if store is None:
            from .portal_calendar_store import SupabaseCalendarStore
            store = SupabaseCalendarStore()
        ig = _resolve_handle(gym, handle, store)
    if not ig:
        return {"ok": False, "gym": gym,
                "reason": "no instagram handle on file; skipped"}

    after_end = today                      # whole days only: today excluded
    after_start = after_end - timedelta(days=WINDOW_DAYS)
    try:
        posts = client.fetch_posts(ig, WINDOW_DAYS + 1)
    except ApifyError as exc:
        return {"ok": False, "gym": gym, "reason": f"apify pull failed: {exc}"}

    after = compute_measures(posts, after_start, after_end)
    before = dict(base_row.get("measures") or {})

    deltas = {}
    for key in MEASURE_KEYS:
        b, a = before.get(key), after.get(key)
        if (isinstance(b, (int, float)) and not isinstance(b, bool)
                and isinstance(a, (int, float)) and not isinstance(a, bool)):
            deltas[key] = round(a - b, 2)
        else:
            deltas[key] = None

    return {
        "ok": True,
        "gym": gym,
        "handle": ig,
        "echo_start": base_row.get("echo_start"),
        "before": before,
        "after": after,
        "deltas": deltas,
    }


# ---------------------------------------------------------------------------
# rendering (CLI table + digest block)
# ---------------------------------------------------------------------------


def _fmt(v):
    if v is None:
        return "no data"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _fmt_delta(v):
    if v is None:
        return ""
    s = f"{v:+g}" if isinstance(v, float) else f"{v:+d}"
    return s


def render_table(result):
    """The before -> after table for one gym, as printable lines."""
    lines = []
    if not result.get("ok"):
        lines.append(f"{result.get('gym', '?')}: {result.get('reason', 'failed')}")
        return lines
    before = result.get("before") or {}
    after = result.get("after") or {}
    deltas = result.get("deltas") or {}
    lines.append(
        f"{result['gym']} (@{result['handle']}) — echo start {result.get('echo_start')}")
    lines.append(
        f"  before: {before.get('window_start')} to {before.get('window_end')}"
        f"  |  after: {after.get('window_start')} to {after.get('window_end')}"
        "  (public IG feed via Apify; plays = videoPlayCount, reels only)")
    header = f"  {'measure':<24}{'before':>12}{'after':>12}{'delta':>10}"
    lines.append(header)
    for key in MEASURE_KEYS:
        lines.append(
            f"  {_MEASURE_LABELS[key]:<24}"
            f"{_fmt(before.get(key)):>12}"
            f"{_fmt(after.get(key)):>12}"
            f"{_fmt_delta(deltas.get(key)):>10}")
    return lines


def since_echo_lines(gym, *, result=None, client=None, store=None,
                     baseline_store=None, today=None):
    """The 'SINCE ECHO STARTED' digest block for one gym, or None when the
    comparison cannot be made honestly (flag OFF, no token, no baseline, no
    handle, pull failure). Never raises; never fabricates."""
    if not config.social_baseline_enabled():
        return None
    try:
        if result is None:
            result = before_after(gym, client=client, store=store,
                                  baseline_store=baseline_store, today=today)
    except Exception as exc:  # noqa: BLE001
        print(f"[social-baseline] since-echo failed for {gym}: "
              f"{type(exc).__name__}")
        return None
    if not result.get("ok"):
        return None
    before = result.get("before") or {}
    after = result.get("after") or {}
    lines = [
        (f"SINCE ECHO STARTED (Instagram @{result['handle']}, echo start "
         f"{result.get('echo_start')}; the 90 days before vs the last 90, "
         "public feed via Apify; plays = videoPlayCount, reels only):")
    ]
    for key in MEASURE_KEYS:
        b, a = before.get(key), after.get(key)
        d = (result.get("deltas") or {}).get(key)
        suffix = f" ({_fmt_delta(d)})" if d is not None else ""
        lines.append(f"  {_MEASURE_LABELS[key]}: {_fmt(b)} -> {_fmt(a)}{suffix}")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# The LASSO tenant is excluded from client_gym_bases() because it has its own
# Meta-direct PUBLISHING lane — but measurement is not publishing. Its public
# Instagram feed carries the same BEFORE/AFTER story as any client's, and Echo
# posts for it. Building --all from client_gym_bases() alone meant LASSO's own
# baseline could never be captured by the documented command, at any time.
LASSO_BASE = "lasso"

# Registry keys that are gym tenants for social purposes: an account on a social
# platform. Anything else in the registry (a personal account, a non-social key)
# has no public feed to measure and only ever produced a skip + exit code 1,
# which masks the real failures --all is supposed to surface.
_SOCIAL_PLATFORMS = ("instagram", "facebook_page")


def all_baseline_gyms():
    """Every tenant `--all` should try: the client gym bases PLUS LASSO, minus
    registry keys that are not social tenants at all. Stable order, deduped.
    Read-only over the account registry; falls back to no platform filtering if
    the registry cannot be read, so a registry hiccup never silently shrinks the
    fleet."""
    from .calendar_autopublish import client_gym_bases

    social = set()
    try:
        from .accounts import all_accounts
        for a in all_accounts():
            if str(getattr(a, "platform", "") or "").lower() not in _SOCIAL_PLATFORMS:
                continue
            k = a.key or ""
            for suf in ("_ig", "_fb"):
                if k.endswith(suf):
                    k = k[: -len(suf)]
                    break
            if k:
                social.add(k)
    except Exception:  # noqa: BLE001 — registry unreadable: do not filter
        social = set()

    out, seen = [], set()
    for gym in [LASSO_BASE] + list(client_gym_bases() or []):
        if not gym or gym in seen:
            continue
        if social and gym not in social:
            continue
        seen.add(gym)
        out.append(gym)
    return out


def cli(argv, *, client=None, store=None, baseline_store=None, today=None):
    """python -m agent social-before-after (--gym <base> | --all) [--capture]

    Default is READ/report: stored baseline + a fresh after-pull, printed as a
    clean before -> after table per gym. --capture stores MISSING baselines
    first (existing ones are immutable and never touched). Gated on
    AGENT_SOCIAL_BASELINE. Returns an exit code."""
    gyms, do_all, do_capture = [], False, False
    i = 0
    argv = list(argv or [])
    while i < len(argv):
        a = argv[i]
        if a in ("--gym", "--account", "--base") and i + 1 < len(argv):
            gyms.append(argv[i + 1]); i += 2; continue
        if a == "--all":
            do_all = True; i += 1; continue
        if a == "--capture":
            do_capture = True; i += 1; continue
        i += 1

    if not config.social_baseline_enabled():
        print("social-before-after is OFF (set AGENT_SOCIAL_BASELINE=true to arm; "
              "APIFY_TOKEN must also be set on the worker)")
        return 1
    if not do_all and not gyms:
        print("usage: python -m agent social-before-after "
              "(--gym <base> | --all) [--capture]")
        return 1
    if do_all:
        gyms = all_baseline_gyms()
        if not gyms:
            print("no client gym bases found")
            return 1

    exit_code = 0
    for gym in gyms:
        # FAULT ISOLATION: one gym's failure must never abort the sweep. This is
        # a manual, run-once rail with no retry — an exception escaping here
        # (a store 5xx, a socket timeout) left every gym AFTER it silently
        # uncaptured. Only the exception TYPE is printed: a raw requests error
        # can carry the full request URL, and the Apify token rides in the query
        # string, so the message itself is never safe to print.
        if do_capture:
            try:
                cap = capture_baseline(gym, client=client, store=store,
                                       baseline_store=baseline_store, today=today)
            except Exception as exc:  # noqa: BLE001
                cap = {"ok": False, "captured": False,
                       "reason": f"capture failed: {type(exc).__name__}"}
            print(f"capture {gym}: "
                  f"{'stored' if cap.get('captured') else cap.get('reason')}")
            if not cap.get("ok"):
                exit_code = 1
                continue
        try:
            res = before_after(gym, client=client, store=store,
                               baseline_store=baseline_store, today=today)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "gym": gym,
                   "reason": f"report failed: {type(exc).__name__}"}
        for line in render_table(res):
            print(line)
        if not res.get("ok"):
            exit_code = 1
    return exit_code
