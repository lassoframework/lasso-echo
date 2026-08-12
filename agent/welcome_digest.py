"""
Daily NEW-CLIENT welcome digest to Slack (Blake, 2026-08-12: "start sending the new
client post to my slack with the templates already made").

The welcome_queue already renders every brand-new client's welcome post (feed + story)
from the on-brand template and hosts it. The drip serves ONE per day to LASSO's IG/FB.
This digest is the VISIBILITY layer on top: once a day it posts to Slack the full list
of new-client welcome posts — today's served one, plus everything still queued — each
with its gym name, tier, the exact template caption, and the hosted feed image link
(Slack unfurls it), so nothing sits unseen behind a one-a-day drip.

Behind AGENT_WELCOME_DIGEST (default OFF). Read-only over the welcome_queue table (no
publish, no queue mutation). Deduped once per day via kv. Nothing here is fabricated:
it shows exactly the template caption + hosted image the queue already produced.
"""

from datetime import datetime, timezone

from . import config, ops_alerts


def _queue_rows_default():
    """All welcome_queue rows with the fields the digest shows. Read-only."""
    from . import db
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT gym_key, name, owner, tier, caption, feed_url, story_url, "
            "status, served_day FROM welcome_queue ORDER BY status, id").fetchall()
    return [dict(r) for r in rows]


def build_digest(today, rows):
    """The digest text for `today` from welcome_queue `rows`. Shows today's served
    welcome + every still-queued one (upcoming). Returns {queued, served_today, text}
    or None when there is nothing new to show."""
    queued = [r for r in rows if r.get("status") == "queued"]
    served_today = [r for r in rows if r.get("status") == "served"
                    and (r.get("served_day") or "") == today]
    if not queued and not served_today:
        return None

    def _one(r, tag):
        name = r.get("name") or r.get("gym_key") or "(unnamed gym)"
        owner = f" ({r['owner']})" if r.get("owner") else ""
        tier = f" [{r['tier']}]" if r.get("tier") else ""
        cap = (r.get("caption") or "").strip()
        cap_line = f"\n    “{cap[:200]}{'…' if len(cap) > 200 else ''}”" if cap else ""
        # BOTH the feed post AND its 9:16 story (the welcome_queue renders both). Show
        # each hosted image so nothing is hidden; a missing one is named honestly.
        media = []
        media.append(f"feed: {r['feed_url']}" if r.get("feed_url")
                     else "feed: (not rendered yet)")
        media.append(f"story: {r['story_url']}" if r.get("story_url")
                     else "story: (not rendered yet)")
        media_line = "\n    " + "\n    ".join(media)
        return f"{tag} *{name}*{owner}{tier}{cap_line}{media_line}"

    lines = [f"*New-client welcome posts* ({today}) — "
             f"{len(served_today)} going out today, {len(queued)} queued"]
    for r in served_today:
        lines.append(_one(r, "✅ TODAY:"))
    for r in queued:
        lines.append(_one(r, "🗓️ QUEUED:"))
    return {"queued": len(queued), "served_today": len(served_today),
            "text": "\n".join(lines)}


def run_daily(now=None, kv=None, alert=None, rows=None):
    """Post today's new-client welcome digest to Slack, once per day. Flag-gated,
    kv-deduped, read-only. Returns the digest dict, or None (flag off / already sent /
    nothing new)."""
    if not config.welcome_digest_enabled():
        return None
    from . import db as _db
    kv_get = (kv.get if kv is not None else _db.kv_get)
    kv_set = (kv.set if kv is not None else _db.kv_set)
    alert = alert or ops_alerts.alert
    now_dt = now or datetime.now(timezone.utc)
    today = now_dt.date().isoformat()
    if kv_get("welcome_digest_sent", "") == today:
        return None
    rows = rows if rows is not None else _queue_rows_default()
    digest = build_digest(today, rows)
    if digest is None:
        return None
    alert(digest["text"], force=True)
    kv_set("welcome_digest_sent", today)
    return digest
