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

import json
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


def _media_reuse_hold(row, *, now=None, history_store=None, media_store=None):
    """Nine-month media-reuse guard at the GBP outbound boundary (same policy as the
    calendar_autopublish lane). Only gyms with a nonzero reuse_months policy touch the
    database — and only then, only for a real (non-draft) send; every other client and
    every rehearsal runs with zero extra reads."""
    from .media_reuse_policy import publish_hold_reason, reuse_months
    gym_id = row.get("gym_id")
    if not reuse_months(gym_id):
        return None
    if history_store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        history_store = SupabaseCalendarStore()
    return publish_hold_reason(row, gym_id, history_store, now=now,
                               media_store=media_store)


def publish_gbp_row(row, connection, *, client, draft=True, now=None,
                    history_store=None, media_store=None, idempotency_key=None):
    """Send one approved GBP row through Zernio. Re-validates the hard rails at send
    time (belt-and-suspenders over the planner) and refuses to ship a violation.

    Returns a dict: {ok, status, late_post_id, reject_reason, mode}.
      * rail violation / bad payload -> {ok False, status 'failed', reject_reason ...}
        (the caller writes 'failed' + alerts; NEVER a silent hold)
      * sent -> {ok True, status 'published', late_post_id, mode 'draft'|'live'}

    draft=True (autonomous build + validation) sends isDraft — Zernio stores it and
    publishes NOTHING. The armed worker passes draft=False, human-tap gated upstream."""
    from .publish_billing_gate import publishing_blocked
    if not draft and publishing_blocked(row.get("gym_id")):
        return {"ok": False, "status": "approved", "late_post_id": "",
                "reject_reason": "Echo access revoked or subscription canceled",
                "held": "echo_access", "mode": ""}
    # MEDIA HOLD GUARD (2026-10-02, Sol review release-blocker): a nonempty
    # media_not_ready_reason means the row's media was never confirmed ready. Refuse
    # BEFORE any provider call and return a visible HOLD (status stays 'approved'),
    # never a 'failed' outcome — nothing was attempted and nothing was rejected.
    _media_hold = (row.get("media_not_ready_reason") or "").strip()
    if _media_hold:
        return {"ok": False, "status": "approved", "late_post_id": "",
                "reject_reason": f"media not ready: {_media_hold}"[:400],
                "held": "media_hold", "mode": ""}
    caption = row.get("caption") or ""
    # INTERNAL EDIT-RATIONALE FINAL GATE (CrossFit ENG '[why]' leak, 2026-08-23): a
    # bracketed meta block after the real caption is stripped here so GBP can never
    # ship internal reasoning; an ALL-meta caption strips to "" and fails the empty
    # caption rail below (failed + reject_reason, a human rewrites it — never silent).
    from . import post_quality as _pq
    _body, _meta = _pq.split_meta_suffix(caption)
    if _meta:
        if not draft and config.approval_proof_enabled():
            # This snapshot already passed the atomic proof claim. Removing
            # metadata now would send different text from the approved digest.
            # No provider call was attempted; the orchestrator releases the
            # token and holds this creative until it is cleaned and reapproved.
            return {"ok": False, "status": "approved", "late_post_id": "",
                    "reject_reason": "caption metadata requires cleanup and fresh approval",
                    "held": "approval_creative_change", "mode": ""}
        caption = _body.strip()
        row = dict(row)
        row["caption"] = caption
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
    if not draft:
        hold = _media_reuse_hold(row, now=now, history_store=history_store,
                                 media_store=media_store)
        if hold:
            return {"ok": False, "status": "approved", "late_post_id": "",
                    "reject_reason": hold, "held": "media_reuse", "mode": ""}
    # A transport exception does not prove that Zernio rejected the create. It may
    # have accepted the post before the response was lost. Never issue a second
    # create or release the claimed row until provider readback resolves it.
    from .zernio import post_id_of

    def _ambiguous(reason):
        return {"ok": False, "status": "publishing", "late_post_id": "",
                "reject_reason": reason, "held": "ambiguous_send", "mode": ""}


    def _dedup_success(exc):
        """Zernio 409 = its 24h content-hash dedup: this exact content ALREADY posted.
        That IS success for exactly-once (audit 2026-08-25 MAJOR: it was classified as
        a failure, so a LIVE post got a client-visible 'failed' + reject_reason and the
        coach requeued the same duplicate forever)."""
        if getattr(exc, "status", None) != 409:
            return None
        from .zernio_publisher import _existing_post_id
        existing = _existing_post_id(getattr(exc, "detail", ""))
        if not existing:
            return _ambiguous("Zernio duplicate response omitted the existing post id")
        return {"ok": True, "status": "published",
                "late_post_id": existing,
                "reject_reason": "", "mode": "draft" if draft else "live",
                "dedup": True}

    try:
        # The persisted claim token (live lane only) is the logical-attempt
        # Idempotency-Key; without a verified token no key is sent. Legacy fake
        # clients that predate the kwarg keep the historical call shape.
        if idempotency_key:
            resp = client.create_post_raw(payload, draft=draft,
                                          idempotency_key=idempotency_key)
        else:
            resp = client.create_post_raw(payload, draft=draft)
    except Exception as exc:  # noqa: BLE001
        dedup = _dedup_success(exc)
        if dedup:
            return dedup
        no_post = _no_post_validation(exc)
        if no_post is not None:
            return {"ok": False, "status": "failed", "late_post_id": "",
                    "reject_reason": no_post, "mode": ""}
        if getattr(exc, "definitive_no_post", False):
            return {"ok": False, "status": "failed", "late_post_id": "",
                    "reject_reason": _plain_reason(str(exc)) or "send error", "mode": ""}
        return _ambiguous(f"Zernio create outcome unknown: {type(exc).__name__}")
    post_id = post_id_of(resp)
    if not post_id:
        return _ambiguous("Zernio create returned no post id")
    return {"ok": True, "status": "published", "late_post_id": post_id,
            "reject_reason": "", "mode": "draft" if draft else "live"}


def _no_post_validation(exc):
    """Per Zernio error-handling docs, branch on the stable JSON envelope type/code,
    never on error text. A received ZernioError with HTTP 400 or 422 AND a parsed JSON
    detail body whose type is 'invalid_request_error' proves validation/precondition
    failure — the create was rejected and NOTHING was stored. That is the only
    conclusive no-post branch: 409 is handled separately when it names an existing post,
    platform_error may be provider-side, 429/5xx are transient, and an unparseable
    body stays ambiguous."""
    from .zernio import ZernioError
    if not isinstance(exc, ZernioError):
        return None
    if exc.status not in (400, 422):
        return None
    try:
        body = json.loads(exc.detail)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("type") != "invalid_request_error":
        return None
    reason = body.get("message") or body.get("code") or "invalid request"
    return f"zernio validation rejected the create: {_plain_reason(str(reason))}"


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


def publish_photo_drop(row, connection, *, client, draft=True, alert=None,
                       now=None, history_store=None, media_store=None):
    """§6.4 photo drop: add the image to the GBP gallery via Zernio gmb-media. This
    endpoint is SYNCHRONOUS with NO webhook and no caption — 2xx -> published now,
    error -> failed + reason + alert. No caption gate (a gallery photo has no text). In
    the DRAFT build we do NOT call gmb-media (it would upload live); we simulate a
    published result so the dogfood shows the photo card without touching Google."""
    from .publish_billing_gate import publishing_blocked
    if not draft and publishing_blocked(row.get("gym_id")):
        return {"ok": False, "status": "approved", "late_post_id": "",
                "reject_reason": "Echo access revoked or subscription canceled",
                "held": "echo_access", "mode": ""}
    # MEDIA HOLD GUARD (2026-10-02, Sol review release-blocker): refuse a held row
    # before the draft simulation and before any gmb-media call; a hold is not an
    # attempt, so the result is a visible hold, never 'failed'.
    _media_hold = (row.get("media_not_ready_reason") or "").strip()
    if _media_hold:
        return {"ok": False, "status": "approved", "late_post_id": "",
                "reject_reason": f"media not ready: {_media_hold}"[:400],
                "held": "media_hold", "mode": ""}
    if not (row.get("image_url") or "").strip():
        return {"ok": False, "status": "failed", "late_post_id": "",
                "reject_reason": "photo drop has no image", "mode": ""}
    if draft:
        return {"ok": True, "status": "published", "late_post_id": "",
                "reject_reason": "", "mode": "draft"}
    hold = _media_reuse_hold(row, now=now, history_store=history_store,
                             media_store=media_store)
    if hold:
        return {"ok": False, "status": "approved", "late_post_id": "",
                "reject_reason": hold, "held": "media_reuse", "mode": ""}
    try:
        resp = client.create_gmb_media(connection["zernio_account_id"],
                                       row["image_url"])
    except Exception as e:  # noqa: BLE001 - an error here is NOT always the outcome
        # CARRY THE MESSAGE, not just the class (2026-09-03). This recorded only
        # type(e).__name__, so the live failure on crossfitnine7f7dadc read "photo
        # upload: ZernioError" in the row AND in the alert -- naming the exception
        # while discarding the one thing that says WHY (a rejected image size, an
        # unlinked location, an expired grant all arrive as the same class). Nothing
        # else logged it either, so the cause was unrecoverable after the fact.
        # scrub() is what makes this safe to surface: a provider error can quote the
        # credential it rejected.
        # C14: carry the STRUCTURE, not just the message. A bare "ZernioError" (the
        # live crossfitnine7f7dadc failure, row 333f90a3) names the exception class and
        # discards the status code, the endpoint, the account and whether trying again
        # could ever help. describe_error unwraps all four, and the retry sweep
        # (agent/gbp_failed_retry.py) reads `retryable` to decide whether this row is
        # worth another attempt or is waiting on a human.
        # AMBIGUITY (2026-10-02): the gmb-media create can time out or lose its
        # response AFTER Zernio accepted the upload. An unstructured exception is
        # not proof of no-post, so it must NEVER stamp terminal 'failed' and must
        # NEVER trigger an automatic second upload: hold the row in 'publishing'
        # (the orchestrator retains the persisted claim + token) for manual
        # provider readback. Only a structured, authoritative definite no-post
        # (the shared invalid_request_error validator, or an exception that
        # explicitly attests definitive_no_post) may fail. error_summary feeds
        # the ALERT in both cases; it never decides the status by itself.
        from .zernio import describe_error, error_summary
        desc = describe_error(e, endpoint="/v1/gmb-media",
                              account_id=connection.get("zernio_account_id"))
        summary = error_summary(desc)
        no_post = _no_post_validation(e)
        # A 4xx alone does not prove Google did not accept a photo. The
        # provider may report a platform error after partial processing. Only
        # parsed validation evidence or an explicit no-post attestation may
        # clear this claim; an unstructured Google wrapper stays held.
        definite = (no_post is not None
                    or getattr(e, "definitive_no_post", False))
        if alert:
            try:
                alert(f"GBP photo drop {'failed' if definite else 'outcome unknown'}"
                      f" for {row.get('gym_id')} row {row.get('id')}: {summary}")
            except Exception:  # noqa: BLE001 - an alert must never decide an outcome
                pass
        if definite:
            return {"ok": False, "status": "failed", "late_post_id": "",
                    "reject_reason": f"photo upload: {summary}"[:400], "mode": "",
                    "error": desc}
        return {"ok": False, "status": "publishing", "late_post_id": "",
                "reject_reason":
                    f"photo upload outcome unknown (provider readback required): "
                    f"{summary}"[:400],
                "held": "ambiguous_send", "mode": "", "error": desc}
    from .zernio import post_id_of
    post_id = post_id_of(resp)
    if not post_id:
        # A 2xx without a post id cannot be confirmed: the media may exist on the
        # provider. Hold for manual readback — do not auto-retry (a second upload
        # would duplicate the gallery photo) and do not stamp failed.
        return {"ok": False, "status": "publishing", "late_post_id": "",
                "reject_reason": "gmb-media returned no post id; manual provider "
                                 "readback required",
                "held": "ambiguous_send", "mode": ""}
    return {"ok": True, "status": "published", "late_post_id": post_id,
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


def publish_one(row, connections, *, client, draft=True, alert=None, now=None,
                history_store=None, media_store=None, idempotency_key=None):
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
    res = (publish_photo_drop(row, conn, client=client, draft=draft, alert=alert,
                             now=now, history_store=history_store,
                             media_store=media_store)
           if is_photo
           else publish_gbp_row(row, conn, client=client, draft=draft, now=now,
                                history_store=history_store,
                                media_store=media_store,
                                idempotency_key=idempotency_key))
    if not res["ok"] and not res.get("held") and alert and not is_photo:
        alert(f"GBP send failed for {row.get('gym_id')} row {row.get('id')}: "
              f"{res['reject_reason']}")
    return {"status": res["status"], "late_post_id": res["late_post_id"],
            "reject_reason": res["reject_reason"],
            "held": res.get("held") or "",
            # CARRY THE MODE. publish_gbp_row returns status='published' for a DRAFT
            # too, and the ONLY thing distinguishing a rehearsal from a real post is
            # this field — which this dict used to drop on the floor. That is why
            # publish_due_gbp could not tell them apart and stamped rows Published for
            # posts that existed only as Zernio drafts.
            "mode": res.get("mode", ""),
            "gbp_location_id": conn.get("gbp_location_id")}   # for the G3 metrics bump


def publish_due_gbp(store, client, *, run_date, draft=True, alert=None, now=None,
                    history_store=None, media_store=None):
    """Publish lane: send every APPROVED, due googlebusiness row (draft in this run).
    Groups by gym, reads its connections once, routes + sends each row, and writes the
    status back (published / failed+reason; needs_reconnect holds silently). Returns a
    summary. A per-row failure never blocks the others."""
    from .zernio import _to_utc_iso  # reuse the tz normalizer for published_at
    rows = store.approved_gbp_rows(run_date) or []
    require_proof = config.approval_proof_enabled()
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
            # EXACTLY-ONCE CLAIM (audit 2026-08-25 MAJOR): flip approved -> publishing
            # BEFORE the network call, mirroring the IG/FB lane. A lost claim (another
            # worker/run owns the row, or its status changed) skips — a crash between
            # send and mark can no longer re-send, and two workers can never double-send.
            # Older stores/fakes without the method keep the historical behavior.
            # A DRAFT run is a rehearsal: it must never mutate the row, so it
            # never claims (a claim would strand the row in 'publishing' and block
            # the real run) and never receives an idempotency key.
            claim = getattr(store, "claim_publishing", None)
            claim_token = None
            claim_won = False
            if require_proof and not draft and claim is None:
                held += 1
                continue  # no atomic proof-capable claim: no provider call
            if claim is not None and not draft:
                try:
                    if require_proof:
                        claimed = claim(row.get("id"), gym_id=gym, require_proof=True)
                    else:
                        claimed = claim(row.get("id"))
                except Exception as e:  # noqa: BLE001
                    print(f"[gbp] claim failed for row {row.get('id')}: "
                          f"{type(e).__name__}; skipping this tick")
                    continue
                if not claimed:
                    continue                      # someone else owns it: skip
                if require_proof:
                    # Publish the exact locked creative returned by the proof
                    # claim, never the potentially stale prefetch snapshot.
                    if (not isinstance(claimed, dict)
                            or str(claimed.get("id")) != str(row.get("id"))
                            or claimed.get("gym_id") != gym
                            or claimed.get("status") != "publishing"
                            or not claimed.get("publish_claim_token")):
                        held += 1
                        continue
                    row = claimed
                    claim_token = claimed["publish_claim_token"]
                claim_won = True
                # New stores return the PERSISTED publish_claim_token; it becomes
                # the Zernio Idempotency-Key. Legacy fakes return True — proceed
                # without a key (never invent one after the claim).
                if isinstance(claimed, str) and claimed.strip():
                    claim_token = claimed
            try:
                res = publish_one(row, conns, client=client, draft=draft, alert=alert,
                                  now=(now or _utcnow()),
                                  history_store=history_store,
                                  media_store=media_store,
                                  idempotency_key=claim_token)
            except Exception as e:  # noqa: BLE001
                # An exception escaping publish_one is NOT proof of a definite
                # no-post: it may come AFTER a provider call (a post-send alert
                # callback, response parsing, the media-reuse history read). Never
                # stamp terminal failed on an unknown outcome. A won claim keeps
                # its durable token in 'publishing' for manual provider readback;
                # when no claim was made (a draft rehearsal) write NO status at all.
                if not claim_won:
                    print(f"[gbp] publish error for row {row.get('id')}: "
                          f"{type(e).__name__}; no claim held — row left unchanged")
                    continue
                held += 1
                print(f"[gbp] WARNING: publish outcome unknown for row "
                      f"{row.get('id')}: {type(e).__name__}; publishing claim and "
                      "token retained for manual provider readback")
                if alert:
                    try:
                        alert(f"GBP publish outcome unknown for {gym} row "
                              f"{row.get('id')}: {type(e).__name__}; the publishing "
                              "claim is retained — manual provider readback required.")
                    except Exception:  # noqa: BLE001 - alerts never decide outcomes
                        pass
                continue
            if res.get("held"):
                held += 1
                if res["held"] == "ambiguous_send":
                    # Keep the durable publishing claim. Only provider readback
                    # can decide whether this attempt was delivered.
                    if alert:
                        try:
                            alert(f"GBP send outcome unknown for {gym} row "
                                  f"{row.get('id')}; publishing claim retained for "
                                  "manual provider readback. "
                                  f"{res.get('reject_reason') or ''}")
                        except Exception:  # noqa: BLE001 - alerts never decide outcomes
                            pass
                    continue
                if claim_won:
                    # release the claim: a held row (needs_reconnect etc.) must go back
                    # to 'approved' so it retries once the hold clears, never strand in
                    # 'publishing'. No provider attempt was made, so the token clears.
                    try:
                        release = getattr(store, "release_publishing_claim", None)
                        if claim_token is not None and release is not None:
                            if release(row.get("id"), claim_token,
                                       "approved") is None:
                                print(f"[gbp] WARNING: claim release matched no row "
                                      f"for {row.get('id')}; claim changed elsewhere")
                        else:
                            store.mark_status(row.get("id"), "approved")
                    except Exception as e:  # noqa: BLE001
                        print(f"[gbp] WARNING: claim release failed for "
                              f"{row.get('id')}: {type(e).__name__}")
                continue
            if res.get("reverted"):
                # G6: lapsed OFFER -> back to pending for a human; alert staff (not client)
                # A draft rehearsal is read-only: no claim exists, write nothing.
                if draft:
                    print(f"[gbp] {gym} row {row.get('id')}: OFFER window lapsed; "
                          "draft mode — row left unchanged.")
                    continue
                reverted += 1
                release = getattr(store, "release_publishing_claim", None)
                if claim_token is not None and release is not None:
                    if release(row.get("id"), claim_token, "pending") is None:
                        print(f"[gbp] WARNING: claim release matched no row for "
                              f"{row.get('id')}; claim changed elsewhere")
                else:
                    store.mark_status(row.get("id"), "pending")
                if alert:
                    alert(f"GBP OFFER for {gym} row {row.get('id')} reverted to pending: "
                          "its offer window lapsed during an outage.")
                continue
            # A DRAFT IS NOT A PUBLISH (published-but-not-posted, the GBP flavour).
            # publish_gbp_row returns status='published' for a DRAFT too, carrying
            # mode='draft' — and this branch used to read `status` alone. So with
            # AGENT_GBP_PUBLISH armed while AGENT_PUBLISH_ENABLED was OFF (the exact
            # posture a draft-only day uses), Zernio stored a DRAFT, Echo stamped the
            # row Published, and the client's portal showed a live post that existed
            # nowhere. Draft mode is a rehearsal: leave the row exactly as it was so
            # the real run can still claim and publish it.
            if res["status"] == "published" and res.get("mode") == "draft":
                print(f"[gbp] {gym} row {row.get('id')}: DRAFT accepted by Zernio; "
                      "NOT marking published (draft mode).")
                continue
            if res["status"] == "published":
                published += 1
                _pub_at = (now or _utcnow())
                stamp = _to_utc_iso(_pub_at.isoformat())
                _loc = res.get("gbp_location_id")
                # G3: stamp the connection's location onto the row so the reconcile top-post
                # ranker keys on the SAME (gym, location, month) as this bump.
                # Confirmed success is terminal: token-scoped CAS clears the token.
                # A failed CAS (claim changed/lost) is visible and does NOT overwrite.
                mpc = getattr(store, "mark_published_claimed", None)
                try:
                    if claim_token is not None and mpc is not None:
                        mpc(row.get("id"), claim_token, res["late_post_id"], stamp,
                            gbp_location_id=_loc)
                    else:
                        store.mark_published(row.get("id"), res["late_post_id"], stamp,
                                             gbp_location_id=_loc)
                except Exception as e:  # noqa: BLE001
                    held += 1
                    published -= 1
                    print(f"[gbp] WARNING: publish stamp rejected for row "
                          f"{row.get('id')} (post id {res['late_post_id']}): "
                          f"{type(e).__name__}; row left in publishing for readback")
                    if alert:
                        alert(f"GBP publish stamp failed for {gym} row "
                              f"{row.get('id')}: {type(e).__name__}; provider post "
                              f"{res['late_post_id']} exists but the claim changed — "
                              "manual reconciliation required.")
                    continue
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
                # A draft rehearsal is read-only: report the would-be failure,
                # never stamp the row.
                if draft:
                    print(f"[gbp] {gym} row {row.get('id')}: would fail "
                          f"({res['reject_reason']}); draft mode — row left unchanged.")
                    continue
                mfc = getattr(store, "mark_failed_claimed", None)
                try:
                    if claim_token is not None and mfc is not None:
                        mfc(row.get("id"), claim_token, res["reject_reason"])
                    else:
                        store.mark_failed(row.get("id"), res["reject_reason"])
                except Exception as e:  # noqa: BLE001
                    held += 1
                    print(f"[gbp] WARNING: failed stamp rejected for row "
                          f"{row.get('id')}: {type(e).__name__}; row left in "
                          "publishing for readback")
                    if alert:
                        alert(f"GBP failed stamp rejected for {gym} row "
                              f"{row.get('id')}: {type(e).__name__}; the claim "
                              "changed — manual reconciliation required.")
                    continue
                failed += 1
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
