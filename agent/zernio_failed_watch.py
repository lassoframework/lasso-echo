"""
zernio_failed_watch.py — notice when Zernio FAILED a post Echo believes went live.

THE BLIND SPOT THIS CLOSES. Echo's client lane treats "Zernio's POST /v1/posts returned
2xx" as publication. That is acceptance, not delivery. Zernio can accept a post and fail
it later (a revoked token, an expired page grant, a media the platform rejects), and
until now NOTHING in Echo ever looked again:

  * there was no failed-post read at all in the client IG/FB lane — no list_failed, no
    retry, no reconcile. The only post-acceptance follow-up anywhere was gbp_worker's
    GBP-only reconcile.
  * publish_confirm exists, but it is advisory (a failed verify posts a soft note and
    never reverts) and it routes to Zernio verification only for LASSO accounts, so a
    CLIENT gym takes the Meta Graph branch with a Zernio post id — a guaranteed 4xx and
    a guaranteed "unconfirmed" that nobody acts on.

So a client gym's post could sit "Published" in the portal, with a real-looking post id,
while Zernio's own record said FAILED. That is the published-but-not-posted class in its
purest form, and it was completely invisible.

WHAT THIS REPORTS (read-only, per gym, once a day):
  published_but_failed  a content_calendar row is status='published' and its late_post_id
                        names a post Zernio marks FAILED. The portal is lying to the
                        client. Loudest case, reported first.
  failed_pileup         N failed posts under this gym's profile inside the window. One
                        is a hiccup; a pile is a broken connection nobody noticed.

RAILS: READ-ONLY everywhere except its own kv dedup stamps. It never retries, never
republishes, never edits a row's status — a human decides whether a failed post should
go out again, because a blind retry can double-post. Behind AGENT_ZERNIO_FAILED_WATCH,
default OFF (house rule). All I/O injectable so the whole watch runs offline.

    python -m agent zernio-failed-watch      # read-only report
"""

from datetime import date

from . import config

REASON_PUBLISHED_BUT_FAILED = "published_but_failed"
REASON_FAILED_PILEUP = "failed_pileup"

# One failed post is noise (a transient media fetch, a momentary token blip that the
# next post clears). Two or more inside the window is a pattern worth a human.
PILEUP_THRESHOLD = 2

# How many of the newest posts to read per gym. list_posts is page-based and newest
# first, so one page covers far more than a day of a 1-2x/day gym.
PAGE_LIMIT = 50


def enabled():
    return config.zernio_failed_watch_enabled()


def _is_failed(post):
    """True when Zernio's own record says this post failed. Checks the post status and
    every per-platform entry, because a post can succeed on one platform and fail on
    another and the top-level status does not always demote."""
    if str((post or {}).get("status") or "").strip().lower() == "failed":
        return True
    for entry in (post or {}).get("platforms") or []:
        if str((entry or {}).get("status") or "").strip().lower() == "failed":
            return True
    return False


def _post_id(post):
    return str((post or {}).get("_id") or (post or {}).get("id") or "").strip()


# ---- the pure classifier ----------------------------------------------------------

def build_findings(gym_posts, published_ids_by_gym):
    """Pure. `gym_posts` is {base: [post, ...]} straight off Zernio;
    `published_ids_by_gym` is {base: {late_post_id, ...}} for rows Echo calls PUBLISHED.

    Returns [{base, reason, count, post_ids, fix}], worst first. A gym with no failed
    posts yields nothing."""
    findings = []
    for base, posts in sorted(gym_posts.items()):
        failed = [p for p in (posts or []) if _is_failed(p)]
        if not failed:
            continue
        failed_ids = {_post_id(p) for p in failed if _post_id(p)}
        claimed = set(published_ids_by_gym.get(base) or set())
        lying = sorted(failed_ids & claimed)
        if lying:
            findings.append({
                "base": base,
                "reason": REASON_PUBLISHED_BUT_FAILED,
                "count": len(lying),
                "post_ids": lying,
                "fix": (
                    f"{len(lying)} row(s) show PUBLISHED in the portal while Zernio "
                    "marks the same post FAILED, so the client sees a post that is not "
                    "on their feed. Check the gym's Zernio connection (an expired token "
                    "or a dropped Facebook page grant is the usual cause), then decide "
                    "per row whether to re-stage it. Do NOT blind-retry: a post Zernio "
                    "failed on ONE platform may be live on the other. Zernio post id(s): "
                    + ", ".join(lying)),
            })
        # A pile-up is worth saying even when none of them are being mis-reported: it
        # means this gym's posts are dying on the way out and nobody has been told.
        rest = sorted(failed_ids - set(lying))
        if len(failed_ids) >= PILEUP_THRESHOLD and rest:
            findings.append({
                "base": base,
                "reason": REASON_FAILED_PILEUP,
                "count": len(failed_ids),
                "post_ids": rest,
                "fix": (
                    f"{len(failed_ids)} failed post(s) under this gym's Zernio profile. "
                    "That is a pattern, not a hiccup — check the gym's connected "
                    "accounts (python -m agent zernio-status --account <base>) and its "
                    "Facebook page selection before more posts pile up."),
            })
    # published_but_failed first: it is the one the client can already see.
    findings.sort(key=lambda f: (f["reason"] != REASON_PUBLISHED_BUT_FAILED, f["base"]))
    return findings


def _alert_text(f):
    return f"Zernio {f['reason']} on {f['base']}: {f['fix']}"


# ---- live readers (injectable; all read-only) -------------------------------------

def _default_bases():
    try:
        from .calendar_autopublish import client_gym_bases
        return [b for b in (client_gym_bases() or []) if not str(b).startswith("lasso")]
    except Exception:  # noqa: BLE001
        return []


def _default_gym_posts(bases, client=None):
    """{base: [post, ...]} — the newest page of each gym's Zernio posts. A gym with no
    resolvable profile is SKIPPED (that is zernio_profile_link's alert to raise, not
    this one's). Any read failure yields no posts for that gym rather than a false
    all-clear for the fleet."""
    out = {}
    if not config.zernio_enabled():
        return out
    try:
        from . import zernio as _z
        client = client or _z.ZernioClient()
    except Exception:  # noqa: BLE001
        return out
    from .zernio_publisher import _default_profile_resolver as _resolve  # noqa: PLC0415
    for base in bases:
        try:
            pid = _resolve(base)
            if not pid:
                continue
            body = client.list_posts(pid, page=1, limit=PAGE_LIMIT) or {}
            out[base] = list(body.get("posts") or [])
        except Exception:  # noqa: BLE001 - one gym's read never blocks the rest
            continue
    return out


def _default_published_ids(bases):
    """{base: {late_post_id, ...}} for rows Echo currently calls PUBLISHED. Read-only.
    An unreadable store yields empty sets, which can only ever SUPPRESS a
    published_but_failed finding, never invent one."""
    out = {}
    try:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
        if not store.available():
            return out
    except Exception:  # noqa: BLE001
        return out
    for base in bases:
        try:
            r = store._client().get(  # noqa: SLF001
                store._rest("content_calendar"),  # noqa: SLF001
                params={"select": "late_post_id", "gym_id": f"eq.{base}",
                        "status": "eq.published", "limit": "500"},
                headers=store._headers(), timeout=30)  # noqa: SLF001
            if r.status_code >= 400:
                continue
            out[base] = {str(x.get("late_post_id") or "").strip()
                         for x in (r.json() or [])
                         if str(x.get("late_post_id") or "").strip()}
        except Exception:  # noqa: BLE001
            continue
    return out


# ---- the sweep --------------------------------------------------------------------

def run(bases=None, gym_posts=None, published_ids=None, alert=None, db=None, today=None):
    """READ-ONLY sweep. Returns {ok, enabled, findings:[...], alerted:[...]}.
    One alert per gym per reason per DAY. Never retries, never republishes, never
    changes a row's status. Never raises out."""
    if not enabled():
        return {"ok": True, "enabled": False, "findings": [], "alerted": []}
    if db is None:
        from . import db as db
    if alert is None:
        from .ops_alerts import alert as alert

    bases = _default_bases() if bases is None else bases
    gym_posts = _default_gym_posts(bases) if gym_posts is None else gym_posts
    published_ids = (_default_published_ids(bases) if published_ids is None
                     else published_ids)

    findings = build_findings(gym_posts, published_ids)
    stamp = str(today or date.today())
    alerted = []
    for f in findings:
        key = f"zernio_failed_{f['base']}_{f['reason']}"
        try:
            if (db.kv_get(key) or "") == stamp:
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            alert(_alert_text(f))
            alerted.append(f["base"])
        except Exception:  # noqa: BLE001
            continue
        try:
            db.kv_set(key, stamp)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "enabled": True, "findings": findings, "alerted": alerted}


def print_report(summary, printer=print):
    if not summary.get("enabled"):
        printer("zernio-failed-watch: flag OFF (AGENT_ZERNIO_FAILED_WATCH); nothing swept.")
        return
    findings = summary.get("findings") or []
    if not findings:
        printer("zernio-failed-watch: no failed posts, and nothing marked published that "
                "Zernio failed.")
        return
    printer(f"zernio-failed-watch: {len(findings)} finding(s):")
    for f in findings:
        printer(f"  {f['reason']:22} {f['base']}  ({f['count']})")
        printer(f"    {f['fix']}")
