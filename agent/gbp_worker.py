"""
GBP publish worker (Phase 5) — send logic + the §7.2 reconcile classifier.

This is NOT the legacy agent/gbp_publisher.py (dead, direct-v4, do-not-extend). All GBP
publishing routes through Zernio here, reusing agent/gbp.py for the payload and rails.

Split so the pure decisions are unit-testable without the DB or network:
  * build_gbp_payload_for_row  — content_calendar row + connection -> Zernio body
  * publish_gbp_row            — re-validate rails at send, then Zernio create_post_raw
                                 (draft=True in the autonomous build; nothing goes live)
  * classify_reconcile         — a GET /v1/posts/{id} response -> next status (§7.2)
  * in_reconcile_window        — hourly-for-48h poll gate

The DB lanes (select approved GBP rows, resolve the connection row, write status back)
attach once the Phase 2 migration lands (gbp_* columns + gym_gbp_connections). They are
thin wrappers over these pure functions.
"""

from datetime import datetime, timedelta, timezone

from . import config, gbp

RECONCILE_HOURS = 48          # §7.2: poll hourly for the first 48h after publish


# --- row -> payload --------------------------------------------------------

def build_gbp_payload_for_row(row, connection):
    """Assemble the Zernio POST body for an approved GBP `content_calendar` row using
    its connection. Raises gbp.GbpPayloadError on any structural violation (bad topic,
    OFFER with a CTA, missing image), so a malformed post can never be sent.

    row keys: caption, image_url, gbp_topic_type, gbp_cta_type, gbp_cta_url, gbp_event,
              gbp_offer, pillar. connection: zernio_account_id, gbp_location_id."""
    pd = gbp.build_platform_data(
        account_id=connection["zernio_account_id"],
        topic_type=row.get("gbp_topic_type") or "STANDARD",
        location_id=connection["gbp_location_id"],
        pillar=row.get("pillar") or "",
        cta_type=row.get("gbp_cta_type") or gbp.DEFAULT_CTA,
        cta_url=row.get("gbp_cta_url") or "",
        event=row.get("gbp_event"),
        offer=row.get("gbp_offer"),
    )
    return gbp.build_post_payload(
        caption=row.get("caption") or "",
        image_url=row.get("image_url") or "",
        platform_data=pd,
    )


def publish_gbp_row(row, connection, *, client, draft=True):
    """Send one approved GBP row through Zernio. Re-validates the hard rails at send
    time (belt-and-suspenders over the planner) and refuses to ship a violation.

    Returns a dict: {ok, status, late_post_id, reject_reason, mode}.
      * rail violation / bad payload -> {ok False, status 'failed', reject_reason ...}
        (the caller writes 'failed' + alerts; NEVER a silent hold)
      * sent -> {ok True, status 'published', late_post_id, mode 'draft'|'live'}

    draft=True (autonomous build + validation) sends isDraft — Zernio stores it and
    publishes NOTHING. The armed worker passes draft=False, human-tap gated upstream."""
    caption = row.get("caption") or ""
    # §7.1 re-validate the hard rails (no dash/hashtag/phone, image present, length).
    # city is a planner-time signal; at send we enforce the platform hard rules only.
    issues = gbp.caption_issues(caption)
    if not (row.get("image_url") or "").strip():
        issues.append("no image on the row")
    if issues:
        return {"ok": False, "status": "failed", "late_post_id": "",
                "reject_reason": "rail check: " + "; ".join(issues), "mode": ""}
    try:
        payload = build_gbp_payload_for_row(row, connection)
    except gbp.GbpPayloadError as e:
        return {"ok": False, "status": "failed", "late_post_id": "",
                "reject_reason": f"payload: {e}", "mode": ""}
    # §7.2 / G7: ONE retry on a TRANSIENT transport error at SEND time. A send that raised
    # never went live, so re-sending once cannot double-post (unlike a reconcile re-send).
    # A policy/other error is NOT retried (it would just fail again). Second failure -> the
    # caller's failed path.
    from .zernio import post_id_of
    try:
        resp = client.create_post_raw(payload, draft=draft)
    except Exception as e1:  # noqa: BLE001
        if not _is_transient_error(e1):
            return {"ok": False, "status": "failed", "late_post_id": "",
                    "reject_reason": _plain_reason(str(e1)) or "send error", "mode": ""}
        try:
            resp = client.create_post_raw(payload, draft=draft)   # the one retry
        except Exception as e2:  # noqa: BLE001
            return {"ok": False, "status": "failed", "late_post_id": "",
                    "reject_reason": "Google could not publish this post after a retry.",
                    "mode": ""}
    return {"ok": True, "status": "published", "late_post_id": post_id_of(resp),
            "reject_reason": "", "mode": "draft" if draft else "live"}


def _is_transient_error(exc):
    """True when an exception's text looks like a transient transport error (5xx / timeout
    / rate limit) that a single retry might clear — never a policy rejection."""
    low = str(exc or "").lower()
    return any(w in low for w in _TRANSPORT_WORDS)


# --- §7.2 reconcile classifier --------------------------------------------

_POLICY_WORDS = ("policy", "phone", "image", "gimmick", "disallow", "rejected",
                 "not allowed", "violation", "prohibited", "url mismatch",
                 "invalid content", "spam")
_TRANSPORT_WORDS = ("timeout", "timed out", "temporar", "rate limit", "rate-limit",
                    "unavailable", "network", "5xx", "500", "502", "503", "504",
                    "try again", "internal error")


def _platform_state(post_json):
    """(status, error_text) for the googlebusiness platform in a Zernio post response.
    Falls back to the top-level status when there is no per-platform breakdown."""
    post = (post_json or {}).get("post") or post_json or {}
    for p in (post.get("platforms") or []):
        if str(p.get("platform")) == gbp.PLATFORM:
            return (str(p.get("status") or "").lower(),
                    str(p.get("error") or p.get("errorMessage") or ""))
    return str(post.get("status") or "").lower(), str(post.get("error") or "")


def classify_reconcile(post_json):
    """Map a GET /v1/posts/{id} response to the next status per §7.2. Returns
    (state, reject_reason) where state is one of:
      'published' | 'pending' | 'retry' | 'failed' | 'deleted'
    'pending' means keep polling (still in flight). 'retry' means one posts_retry; a
    second failure the caller escalates to 'failed'. A policy rejection is 'failed'
    with a plain-English reason and is NEVER retried."""
    status, err = _platform_state(post_json)
    low = (err or "").lower()
    if status in ("published", "live", "success", "succeeded", "posted"):
        return "published", ""
    if status in ("deleted", "cancelled", "canceled"):
        return "deleted", ""
    if status in ("failed", "error", "rejected"):
        if any(w in low for w in _POLICY_WORDS):
            return "failed", _plain_reason(err)
        if any(w in low for w in _TRANSPORT_WORDS):
            return "retry", ""
        # unknown failure: treat as policy (do NOT auto-retry into a loop); surface it
        return "failed", _plain_reason(err) or "Google rejected this post."
    # scheduled / processing / pending / queued / '' -> still settling
    return "pending", ""


def _plain_reason(err):
    """A short, client-safe reason from a raw platform error (scrubbed of ids/urls)."""
    import re
    txt = re.sub(r"https?://\S+", "", err or "").strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt[:200]


class RoutingError(Exception):
    """A GBP row resolved to 0 or 2+ connections. §7.1: never a silent hold — the row
    goes to 'failed' with reject_reason='connection routing' + a staff alert."""


def resolve_connection(connections, gbp_location_id=None):
    """Exactly ONE connection for a GBP row, or RoutingError. `connections` are the
    connected (status='connected') rows for the row's portal_gym_key. When the row
    carries a gbp_location_id (multi-location gym), match on it; otherwise there must be
    exactly one connection. 0 or 2+ -> RoutingError (§7.1)."""
    conns = list(connections or [])
    if gbp_location_id:
        conns = [c for c in conns if c.get("gbp_location_id") == gbp_location_id]
    if len(conns) == 1:
        return conns[0]
    raise RoutingError(f"resolved {len(conns)} connections "
                       f"(location={gbp_location_id or 'any'})")


def publish_photo_drop(row, connection, *, client, draft=True, alert=None):
    """§6.4 photo drop: add the image to the GBP gallery via Zernio gmb-media. This
    endpoint is SYNCHRONOUS with NO webhook and no caption — 2xx -> published now,
    error -> failed + reason + alert. No caption gate (a gallery photo has no text). In
    the DRAFT build we do NOT call gmb-media (it would upload live); we simulate a
    published result so the dogfood shows the photo card without touching Google."""
    if not (row.get("image_url") or "").strip():
        return {"ok": False, "status": "failed", "late_post_id": "",
                "reject_reason": "photo drop has no image", "mode": ""}
    if draft:
        return {"ok": True, "status": "published", "late_post_id": "",
                "reject_reason": "", "mode": "draft"}
    try:
        resp = client.create_gmb_media(connection["zernio_account_id"],
                                       row["image_url"])
    except Exception as e:  # noqa: BLE001 - synchronous: an error IS the outcome
        if alert:
            alert(f"GBP photo drop failed for {row.get('gym_id')} "
                  f"row {row.get('id')}: {type(e).__name__}")
        return {"ok": False, "status": "failed", "late_post_id": "",
                "reject_reason": f"photo upload: {type(e).__name__}", "mode": ""}
    from .zernio import post_id_of
    return {"ok": True, "status": "published", "late_post_id": post_id_of(resp),
            "reject_reason": "", "mode": "live"}


def offer_window_lapsed(row, now):
    """G6: True when an OFFER row's window (gbp_event.schedule.endDate) has ended before
    `now`. A lapsed offer must NOT publish (a dead offer in front of Google strangers);
    the worker reverts it to 'pending'. Non-OFFER rows and rows with no end date -> False."""
    if str(row.get("gbp_topic_type") or "").upper() != "OFFER":
        return False
    sched = (row.get("gbp_event") or {})
    sched = sched.get("schedule") if isinstance(sched, dict) else {}
    end = (sched or {}).get("endDate")
    if not end:
        return False
    try:
        from datetime import date as _date
        return _date.fromisoformat(str(end)[:10]) < now.date()
    except Exception:  # noqa: BLE001
        return False


def in_publish_window(now, tz_str):
    """§7.3 / G5: True when `now` falls in a weekday 8-10am window in the connection's
    timezone. A missing/invalid timezone -> True (cannot enforce a window without a zone;
    better to publish than to hold a post forever). `now` must be tz-aware (UTC)."""
    if not config.gbp_publish_window_enabled():
        return True
    tz = (tz_str or "").strip()
    if not tz:
        return True
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 - unknown zone -> do not hold forever
        return True
    return local.weekday() < 5 and 8 <= local.hour < 10


def publish_one(row, connections, *, client, draft=True, alert=None, now=None):
    """Publish one approved GBP row: connection precheck (§7.1) + routing + send. Returns
    the status transition dict {status, late_post_id, reject_reason}. A needs_reconnect
    gym HOLDS silently (status stays 'approved'); a routing failure or rail violation
    goes to 'failed' with a reason (+ alert), never a silent hold. A row outside the §7.3
    8-10am weekday window (connection timezone) HOLDS until the next in-window tick. A
    photo-drop row (format='photo') routes to the gmb-media path (§6.4), not the posts API."""
    # §7.1.1 hold silently if the only/target connection is needs_reconnect
    live = [c for c in (connections or []) if c.get("status") == "connected"]
    if not live and (connections or []):
        return {"status": "approved", "late_post_id": "", "reject_reason": "",
                "held": "needs_reconnect"}
    try:
        conn = resolve_connection(live, row.get("gbp_location_id"))
    except RoutingError as e:
        if alert:
            alert(f"GBP routing failure for {row.get('gym_id')} "
                  f"row {row.get('id')}: {e}")
        return {"status": "failed", "late_post_id": "",
                "reject_reason": "connection routing"}
    # G6: an OFFER whose window LAPSED during an outage must not ship a dead offer to
    # Google — revert it to 'pending' so a human refreshes or drops it.
    if now is not None and offer_window_lapsed(row, now):
        return {"status": "pending", "late_post_id": "",
                "reject_reason": "offer window lapsed", "reverted": True}
    # §7.3 / G5: hold until the connection's local weekday 8-10am window.
    if now is not None and not in_publish_window(now, conn.get("timezone")):
        return {"status": "approved", "late_post_id": "", "reject_reason": "",
                "held": "outside_window"}
    is_photo = str(row.get("format") or "").lower() == "photo"
    res = (publish_photo_drop(row, conn, client=client, draft=draft, alert=alert)
           if is_photo
           else publish_gbp_row(row, conn, client=client, draft=draft))
    if not res["ok"] and alert and not is_photo:
        alert(f"GBP send failed for {row.get('gym_id')} row {row.get('id')}: "
              f"{res['reject_reason']}")
    return {"status": res["status"], "late_post_id": res["late_post_id"],
            "reject_reason": res["reject_reason"],
            "gbp_location_id": conn.get("gbp_location_id")}   # for the G3 metrics bump


def publish_due_gbp(store, client, *, run_date, draft=True, alert=None, now=None):
    """Publish lane: send every APPROVED, due googlebusiness row (draft in this run).
    Groups by gym, reads its connections once, routes + sends each row, and writes the
    status back (published / failed+reason; needs_reconnect holds silently). Returns a
    summary. A per-row failure never blocks the others."""
    from .zernio import _to_utc_iso  # reuse the tz normalizer for published_at
    rows = store.approved_gbp_rows(run_date) or []
    by_gym = {}
    for r in rows:
        by_gym.setdefault(r.get("gym_id"), []).append(r)
    published = failed = held = reverted = 0
    for gym, gym_rows in by_gym.items():
        try:
            conns = store.connections_for(gym) or []
        except Exception as e:  # noqa: BLE001
            if alert:
                alert(f"GBP publish: could not read connections for {gym}: "
                      f"{type(e).__name__}")
            continue
        for row in gym_rows:
            try:
                res = publish_one(row, conns, client=client, draft=draft, alert=alert,
                                  now=(now or _utcnow()))
            except Exception as e:  # noqa: BLE001
                failed += 1
                store.mark_failed(row.get("id"), f"worker error: {type(e).__name__}")
                continue
            if res.get("held"):
                held += 1
                continue
            if res.get("reverted"):
                # G6: lapsed OFFER -> back to pending for a human; alert staff (not client)
                reverted += 1
                store.mark_status(row.get("id"), "pending")
                if alert:
                    alert(f"GBP OFFER for {gym} row {row.get('id')} reverted to pending: "
                          "its offer window lapsed during an outage.")
                continue
            if res["status"] == "published":
                published += 1
                _pub_at = (now or _utcnow())
                stamp = _to_utc_iso(_pub_at.isoformat())
                _loc = res.get("gbp_location_id")
                # G3: stamp the connection's location onto the row so the reconcile top-post
                # ranker keys on the SAME (gym, location, month) as this bump.
                store.mark_published(row.get("id"), res["late_post_id"], stamp,
                                     gbp_location_id=_loc)
                # G3: the publish rail owns posts_published (the portal cron omits it).
                # Best-effort: a metrics write must never fail or undo a publish.
                try:
                    month_iso = _pub_at.date().replace(day=1).isoformat()
                    store.bump_posts_published(
                        gym, res.get("gbp_location_id"), month_iso,
                        now_iso=stamp, seed_top_post_id=res.get("late_post_id"))
                except Exception as e:  # noqa: BLE001
                    print(f"[gbp] posts_published bump failed for {gym}: "
                          f"{type(e).__name__}")
            elif res["status"] == "failed":
                failed += 1
                store.mark_failed(row.get("id"), res["reject_reason"])
    return {"published": published, "failed": failed, "held": held,
            "reverted": reverted, "gyms": len(by_gym)}


def _post_clicks(post_json):
    """G3: a per-post click count from a Zernio GET /v1/posts/{id} response, or None when
    the response carries no click signal (top_post_id is NEVER ranked from a fabricated
    number — only from real click data). Tolerant of a few likely shapes."""
    if not isinstance(post_json, dict):
        return None
    for path in (("insights", "clicks"), ("metrics", "clicks"), ("analytics", "clicks"),
                 ("insights", "websiteClicks"), ("clicks",), ("clickThroughs",)):
        node = post_json
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, (int, float)):
            return int(node)
    return None


def reconcile_gbp(store, client, *, now=None, alert=None):
    """§7.2 reconcile: for each recently-PUBLISHED GBP row still inside the 48h window,
    poll GET /v1/posts/{id} and apply the classification. published/pending -> leave;
    policy rejection -> failed+reason+alert; deleted -> deleted. A TRANSIENT poll result
    KEEPS POLLING (never re-sends): the row was already accepted by Zernio, so re-sending
    here could double-post a post that is in fact live and would also bypass the draft/off
    posture. The single transport retry lives at SEND time in publish_gbp_row, where a
    failed send has NOT gone live. Never auto-requeues. G3: while polling, rank the top
    post BY CLICKS per (gym, location, month) from real per-post click data and set
    gym_gbp_metrics.top_post_id — a no-op when the poll carries no clicks. Returns a
    summary."""
    now = now or _utcnow()
    since = _iso(now - timedelta(hours=RECONCILE_HOURS))
    rows = store.recent_published_gbp(since) or []
    demoted = waiting = 0
    best_by_key = {}   # (gym, loc, month) -> (clicks, late_post_id) : G3 top-by-clicks
    for row in rows:
        if not in_reconcile_window(row.get("published_at"), now=now):
            continue
        pid = row.get("late_post_id")
        if not pid:
            continue
        try:
            post_json = client.get_post(pid)
            state, reason = classify_reconcile(post_json)
        except Exception:  # noqa: BLE001 - a poll error just waits for the next tick
            continue
        if state in ("published", "pending", "retry"):
            # transient ('retry') is treated like 'pending': keep polling, never re-send
            # (no double-post risk). G3: rank the top post by real clicks when present.
            if state == "retry":
                waiting += 1
            clicks = _post_clicks(post_json)
            if clicks is not None:
                key = (row.get("gym_id"), row.get("gbp_location_id") or "",
                       str(row.get("published_at") or "")[:7] + "-01")
                if clicks > best_by_key.get(key, (-1, None))[0]:
                    best_by_key[key] = (clicks, pid)
            continue
        if state == "deleted":
            store.mark_status(row.get("id"), "deleted")
            demoted += 1
        else:  # failed (policy or unknown)
            store.mark_failed(row.get("id"), reason)
            if alert:
                alert(f"GBP post {pid} for {row.get('gym_id')} rejected: {reason}")
            demoted += 1
    # G3: write the top-by-clicks winner per (gym, location, month). Best-effort — a
    # metrics write must never break the reconcile lane.
    ranked = 0
    for (gym, loc, month), (_clicks, top_pid) in best_by_key.items():
        try:
            mrow = store.top_post_by_clicks(gym, loc, month)
            if mrow and mrow.get("top_post_id") != top_pid:
                store.set_top_post(mrow["id"], top_pid, _iso(now))
                ranked += 1
        except Exception as e:  # noqa: BLE001
            print(f"[gbp] top_post_id rank failed for {gym}: {type(e).__name__}")
    return {"checked": len(rows), "demoted": demoted, "waiting": waiting,
            "top_ranked": ranked}


def _utcnow():
    from datetime import datetime as _dt
    return _dt.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def in_reconcile_window(published_at, now=None):
    """True while a post is inside the 48h post-publish poll window (§7.2). After 48h
    the post is settled and the hourly reconcile stops."""
    if not published_at:
        return False
    now = now or datetime.now(timezone.utc)
    pub = published_at
    if isinstance(pub, str):
        try:
            pub = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            return False
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return now - pub <= timedelta(hours=RECONCILE_HOURS)
