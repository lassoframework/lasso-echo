"""metrics_sync.py — Wave 7.1 metrics ingestion (flag AGENT_METRICS_SYNC, default OFF).

Nightly per gym: pull Zernio analytics (source=all, so EXTERNAL posts — posts
Echo did not publish — arrive too), join each post to content_calendar via
late_post_id, falling back to platformPostId, and snapshot its metrics at
post-age days 1, 3, 7, 28. Engagement is a decay curve: comparing a 2-day-old
post against a 3-week-old one is how naive loops lie to themselves.

HARD RULES (Blake's Wave 7 rails):
- DEDUPE BY platformPostId. The duplicate lassoframework IG connection returns
  the same post under two account ids; ONE row wins per (platform,
  platformPostId, snapshot_day).
- A post with NO calendar match is stored with calendar_id null and
  external=true. External rows inform the gym's baseline but NEVER train the
  playbook (we don't learn from posts we didn't shape, and we don't let the
  second publisher poison the data).
- READ ONLY on the social side: nothing here publishes, approves, or touches
  any account. Writes go to post_metrics only.
- NULL-NOT-ZERO: a metric Zernio does not report stays null, never a
  fabricated 0.

Everything is injectable (zernio client, store, now) so the whole path is unit
tested without a network call. Has run(gyms=None, now=None) per the spec.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config
from .zernio import ZernioClient, _parse_iso

SNAPSHOT_DAYS = (1, 3, 7, 28)
# A nightly job can miss a night; a snapshot day stays capturable for this many
# days past its threshold so one missed run does not lose the snapshot, while a
# 3-week-old post can never masquerade as a day-1 snapshot.
SNAPSHOT_GRACE_DAYS = 2
_WINDOW_DAYS = SNAPSHOT_DAYS[-1] + SNAPSHOT_GRACE_DAYS + 1


# ---- pure helpers ---------------------------------------------------------------------


def due_snapshot_days(published_at, now, existing_days=()):
    """The snapshot days (1|3|7|28) that are DUE for a post right now: the post
    is at least that many days old, not so old the snapshot would lie
    (SNAPSHOT_GRACE_DAYS past the threshold), and not already recorded.
    Unparseable publishedAt -> nothing due (we cannot age what we cannot date)."""
    ts = _parse_iso(published_at) if isinstance(published_at, str) else published_at
    if ts is None:
        return []
    age_days = (now - ts).total_seconds() / 86400.0
    have = set(existing_days or ())
    return [d for d in SNAPSHOT_DAYS
            if d not in have and d <= age_days < d + SNAPSHOT_GRACE_DAYS]


def _metric(analytics, key):
    """One present numeric metric or None (bools rejected; never a fabricated 0)."""
    v = (analytics or {}).get(key)
    if isinstance(v, bool):
        return None
    return v if isinstance(v, (int, float)) else None


def dedupe_posts(posts):
    """Dedupe a Zernio posts page by (platform, platformPostId): the duplicate
    lassoframework connection returns the same post under two account ids, and
    exactly ONE row may win. First occurrence wins (newest-first stream); a
    post with no platformPostId falls back to its Zernio _id so it still keys
    uniquely; a post with neither is dropped (it cannot be deduped or keyed)."""
    seen = set()
    out = []
    for p in posts or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("platformPostId") or p.get("_id")
        if not pid:
            continue
        key = (str(p.get("platform") or ""), str(pid))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _followers_for_post(post, accounts):
    """The followersCount of the post's own account (matched by accountId),
    else the first non-null followersCount for the post's platform, else None."""
    accounts = [a for a in (accounts or []) if isinstance(a, dict)]
    acct_id = post.get("accountId") or post.get("account_id")
    if acct_id:
        for a in accounts:
            if str(a.get("_id") or a.get("id")) == str(acct_id):
                v = a.get("followersCount")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
    plat = post.get("platform")
    for a in accounts:
        if a.get("platform") == plat:
            v = a.get("followersCount")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
    return None


def build_metric_row(gym_id, post, accounts, snapshot_day, calendar_row=None):
    """ONE post_metrics row for this post at this snapshot day. Lever columns
    (pillar, format, hook_family, ...) come from the matched calendar row when
    one exists; an external post carries calendar_id null and external=true.
    Metrics that Zernio does not report stay None (null-not-zero)."""
    a = post.get("analytics") or {}
    cal = calendar_row or {}
    matched = bool(cal.get("id"))
    watch = _metric(a, "igReelsAvgWatchTime")
    return {
        "gym_id": gym_id,
        "platform": post.get("platform") or "",
        "platform_post_id": str(post.get("platformPostId") or post.get("_id") or ""),
        "calendar_id": cal.get("id") if matched else None,
        "external": not matched,
        "pillar": cal.get("pillar"),
        "format": cal.get("format"),
        "hook_family": cal.get("hook_family"),
        "ask_type": cal.get("ask_type"),
        "time_slot": cal.get("time_slot"),
        "caption_len_band": cal.get("caption_len_band"),
        "has_member_face": cal.get("has_member_face"),
        "media_product_type": post.get("mediaProductType") or a.get("mediaProductType"),
        "published_at": post.get("publishedAt"),
        "snapshot_day": snapshot_day,
        "impressions": _metric(a, "impressions"),
        "reach": _metric(a, "reach"),
        "likes": _metric(a, "likes"),
        "comments": _metric(a, "comments"),
        "shares": _metric(a, "shares"),
        "saves": _metric(a, "saves"),
        "clicks": _metric(a, "clicks"),
        "views": _metric(a, "views"),
        "follows": _metric(a, "follows"),
        "watch_time_ms": watch,
        "video_seconds": _metric(a, "videoDurationSeconds"),
        "followers_at_snapshot": _followers_for_post(post, accounts),
    }


# ---- default store (PostgREST; injectable) ----------------------------------------------


class MetricsStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"post_metrics {status}: {detail}")


class SupabaseMetricsStore:
    """PostgREST client for post_metrics reads/writes and the calendar JOIN
    lookups. `http` injectable (portal_calendar_store pattern). The calendar is
    READ here only — content_calendar writes always go through Echo's store."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = service_key if service_key is not None else config.supabase_service_key()
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
            raise MetricsStoreError(r.status_code, (r.text or "")[:200])
        return r.json() or []

    def existing_days(self, gym_id, platform, platform_post_id):
        rows = self._get("post_metrics", {
            "gym_id": f"eq.{gym_id}", "platform": f"eq.{platform}",
            "platform_post_id": f"eq.{platform_post_id}",
            "select": "snapshot_day"})
        return {r.get("snapshot_day") for r in rows if r.get("snapshot_day") is not None}

    def find_calendar(self, gym_id, late_post_id=None, platform_post_id=None):
        """The gym's content_calendar row joined via late_post_id first, then
        the platformPostId fallback. READ ONLY."""
        for value in (late_post_id, platform_post_id):
            if not value:
                continue
            rows = self._get("content_calendar", {
                "gym_id": f"eq.{gym_id}", "late_post_id": f"eq.{value}",
                "limit": "1"})
            if rows:
                return rows[0]
        return None

    def insert_metrics(self, rows):
        """INSERT post_metrics rows; on-conflict rows are ignored (the primary
        key already dedupes re-runs). Returns the number sent."""
        if not rows:
            return 0
        r = self._client().post(
            f"{self._url}/rest/v1/post_metrics",
            params={"on_conflict": "gym_id,platform,platform_post_id,snapshot_day"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=minimal"}),
            json=rows, timeout=30)
        if r.status_code >= 400:
            raise MetricsStoreError(r.status_code, (r.text or "")[:200])
        return len(rows)


# ---- run ------------------------------------------------------------------------------


def _default_gyms():
    """All client gyms + lasso (the rollout_digest pattern). Degrades to
    ['lasso'] when the roster cannot be read."""
    try:
        from .calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
        if "lasso" not in gyms:
            gyms = ["lasso"] + gyms
        return gyms
    except Exception:
        return ["lasso"]


def sync_gym(gym_id, analytics_json, store, now):
    """Fold one gym's analytics JSON into post_metrics rows and insert them.
    Pure over its inputs except the store calls. Returns a summary dict."""
    aj = analytics_json or {}
    posts = dedupe_posts(aj.get("posts") or [])
    accounts = aj.get("accounts") or []
    rows = []
    external_count = 0
    for post in posts:
        pid = str(post.get("platformPostId") or post.get("_id") or "")
        platform = post.get("platform") or ""
        try:
            existing = store.existing_days(gym_id, platform, pid)
        except Exception:
            existing = set()
        due = due_snapshot_days(post.get("publishedAt"), now, existing)
        if not due:
            continue
        cal = None
        # External posts (isExternal) get NO calendar lookup shortcut — we still
        # try the join first; a real Echo post mislabeled external must not lose
        # its calendar link. No match (either way) -> external=true.
        try:
            cal = store.find_calendar(
                gym_id,
                late_post_id=post.get("_id"),
                platform_post_id=post.get("platformPostId"))
        except Exception:
            cal = None
        for day in due:
            row = build_metric_row(gym_id, post, accounts, day, calendar_row=cal)
            if row["external"]:
                external_count += 1
            rows.append(row)
    inserted = store.insert_metrics(rows) if rows else 0
    return {"gym_id": gym_id, "posts_seen": len(posts),
            "rows_inserted": inserted, "external_rows": external_count}


def run(gyms=None, now=None, zernio=None, store=None):
    """The nightly sync. Behind AGENT_METRICS_SYNC (default OFF -> no-op, no
    client constructed, no network touched). Per gym: resolve the gym's Zernio
    profile by name, pull analytics with source=all over the snapshot window,
    and land the due snapshots. A gym with no Zernio profile or a failed pull
    is REPORTED and skipped — never guessed."""
    if not config.metrics_sync_enabled():
        return {"ok": False, "reason": "AGENT_METRICS_SYNC is OFF (default). "
                                       "No pull performed.", "gyms": []}
    now = now or datetime.now(timezone.utc)
    zernio = zernio or ZernioClient()
    store = store or SupabaseMetricsStore()
    gyms = list(gyms) if gyms else _default_gyms()

    results = []
    for gym_id in gyms:
        try:
            profile_id = zernio.find_profile_id(gym_id)
        except Exception as exc:  # noqa: BLE001
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": f"profile lookup failed: {type(exc).__name__}"})
            continue
        if not profile_id:
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": "no Zernio profile for gym (reported, not guessed)"})
            continue
        try:
            aj = zernio.analytics_window(profile_id, days=_WINDOW_DAYS, source="all")
        except Exception as exc:  # noqa: BLE001
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": f"analytics pull failed: {type(exc).__name__}"})
            continue
        try:
            summary = sync_gym(gym_id, aj, store, now)
            summary["ok"] = True
            results.append(summary)
        except Exception as exc:  # noqa: BLE001
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": f"store write failed: {type(exc).__name__}"})
    return {"ok": True, "gyms": results}
