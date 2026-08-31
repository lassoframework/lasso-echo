"""
Portal calendar, library, and draft-action endpoints.

All routes gated by AGENT_PORTAL_APPROVALS (default OFF).
Token→account resolution happens in intake_web.py before these handlers
are called, so every function here receives a validated account_key.

Action routes (approve/edit/deny/kill) delegate to portal_approvals, which
owns per-gym scoping, actor authorization, and approvals.handle_action —
the same path Slack uses, so portal and Slack act on the same draft records.
"""

from . import config, db as _db
from . import portal_approvals as _pa
from . import portal_calendar_store as _pcs
from .accounts import get_account
from .library import list_creatives


def _flag_off(route):
    return 403, {"error": "AGENT_PORTAL_APPROVALS is OFF", "route": route}


def handle_portal_calendar(account_key, month, store=None):
    """
    GET /portal/<token>/calendar?month=YYYY-MM

    Returns up to 60 days of drafts for the account in the given calendar
    month. `month` must match YYYY-MM exactly; absent or malformed → 400.

    Response:
        {account_key, month, drafts: [{draft_id, day_key, draft_type,
          status, platform, caption, creative_public_url,
          scheduled_for, blocked_reason}]}
    """
    if not config.portal_approvals_enabled():
        return _flag_off("calendar")

    if not account_key:
        return 400, {"error": "missing account_key"}

    if not month or len(month) != 7 or month[4] != "-":
        return 400, {"error": "month must be YYYY-MM"}

    prefix = month + "-"

    # Shared Supabase data plane wins when creds are present (the live portal
    # path). No creds -> the existing SQLite behavior below, unchanged.
    if store is None and config.portal_calendar_supabase_enabled():
        try:
            sb = _pcs.SupabaseCalendarStore()
            rows = sb.list_month(account_key, month)
            drafts = [_pcs.map_row(r) for r in rows]
        except Exception as exc:
            return 500, {"error": f"store error: {type(exc).__name__}"}
        return 200, {"account_key": account_key, "month": month, "drafts": drafts}

    if store is not None:
        pending = store.list_pending()
        rows = [d for d in pending if d.account_key == account_key
                and (d.day_key or "").startswith(prefix)]
        drafts = [_draft_to_dict(d) for d in rows]
    else:
        try:
            with _db.connect() as conn:
                from .store import _SELECT, _row_to_draft
                results = conn.execute(
                    f"SELECT {_SELECT} FROM drafts "
                    "WHERE account_key=? AND day_key LIKE ?",
                    (account_key, prefix + "%")
                ).fetchall()
            drafts = [_draft_to_dict(_row_to_draft(r))
                      for r in results if _row_to_draft(r) is not None]
        except Exception as exc:
            return 500, {"error": f"db error: {type(exc).__name__}"}

    return 200, {"account_key": account_key, "month": month, "drafts": drafts}


def handle_portal_library(account_key):
    """
    GET /portal/<token>/library

    Returns the account's creative library (local-disk path resolved via
    account.library_path). Each item carries path, media_type, public_url,
    client_note.

    Response:
        {account_key, creatives: [{stem, path, media_type,
          public_url, client_note}]}
    """
    if not config.portal_approvals_enabled():
        return _flag_off("library")

    if not account_key:
        return 400, {"error": "missing account_key"}

    account = get_account(account_key)
    if account is None:
        return 404, {"error": f"unknown account: {account_key}"}

    try:
        library_path = account.library_path
    except Exception:
        library_path = None

    creatives = list_creatives(library_path) if library_path else []

    return 200, {
        "account_key": account_key,
        "creatives": [
            {
                "stem": c.stem,
                "path": c.path,
                "media_type": c.media_type,
                "public_url": c.public_url,
                "client_note": c.client_note,
            }
            for c in creatives
        ],
    }


def handle_portal_action(action, account_key, draft_id, actor_id, note="",
                         store=None, confirm=False, reason="", gbp=None):
    """
    POST /portal/<token>/{approve|edit|deny|kill}

    Body: {draft_id, actor_id, note?, confirm?}

    Delegates to portal_approvals, which owns per-gym scoping +
    actor authorization + approvals.handle_action (same path as Slack).

    action must be one of: approve, edit, deny, kill.

    KILL REQUIRES confirm=true (Blake ruling 2026-08-13): kill is permanent, so a
    one-click kill is refused with 400 on BOTH data planes — matching the Part-B
    /posts/<id>/kill contract, so no route family offers an unconfirmed kill.
    """
    if not config.portal_approvals_enabled():
        return _flag_off(action)

    if action not in ("approve", "edit", "deny", "kill", "requeue"):
        return 400, {"error": f"unknown action: {action}"}

    if not draft_id:
        return 400, {"error": "draft_id required"}
    if not actor_id:
        return 400, {"error": "actor_id required"}

    if action == "kill" and not confirm:
        return 400, {"ok": False, "action": "kill", "draft_id": draft_id,
                     "error": "kill is permanent and requires confirm=true"}

    # Shared Supabase data plane wins when creds are present (the live portal
    # path). No creds -> the existing portal_approvals/SQLite path, unchanged.
    if store is None and config.portal_calendar_supabase_enabled():
        return _handle_action_supabase(action, account_key, draft_id, note,
                                       reason=reason, gbp=gbp)

    # requeue (G2) is a content_calendar-only action (failed GBP/FB/IG rows live there).
    # The legacy SQLite drafts plane has no failed-row recovery, so it is unsupported there.
    if action == "requeue":
        return 400, {"ok": False, "action": "requeue", "draft_id": draft_id,
                     "error": "requeue requires the content_calendar data plane"}

    fn = getattr(_pa, action)
    if action in ("edit", "deny", "kill"):
        result = fn(account_key, draft_id, actor_id, note=note, store=store)
    else:
        result = fn(account_key, draft_id, actor_id, store=store)

    ok = result.get("ok", False)
    return (200 if ok else 403), result


def handle_portal_report(account_key, days):
    """
    GET /portal/<token>/report?days=N

    Live analytics are not connected yet, so every metric answers null (never a
    fabricated 0). Shape matches the portal's mapReport exactly. Gated by
    AGENT_PORTAL_APPROVALS.
    """
    if not config.portal_approvals_enabled():
        return _flag_off("report")

    if not account_key:
        return 400, {"error": "missing account_key"}

    try:
        window = int(days)
    except (TypeError, ValueError):
        window = 30
    if window < 1:
        window = 30

    return 200, {
        "account_key": account_key,
        "window_days": window,
        "posts_published": None,
        "engagement_rate": None,
        "likes": None,
        "comments": None,
        "saves": None,
        "shares": None,
        "views": None,
        "reach": None,
        "follower_delta": None,
        "gaps": ["Live analytics are not connected yet; no numbers are shown "
                 "rather than a made up zero."],
        "health": {"label": None},
        "top_posts": [],
    }


# ---- helpers -------------------------------------------------------------------

_GBP_FIELD_MAP = {
    "topicType": "gbp_topic_type", "topic_type": "gbp_topic_type",
    "gbp_topic_type": "gbp_topic_type",
    "ctaType": "gbp_cta_type", "cta_type": "gbp_cta_type", "gbp_cta_type": "gbp_cta_type",
    "ctaUrl": "gbp_cta_url", "cta_url": "gbp_cta_url", "gbp_cta_url": "gbp_cta_url",
    "event": "gbp_event", "gbp_event": "gbp_event",
    "offer": "gbp_offer", "gbp_offer": "gbp_offer",
    "locationId": "gbp_location_id", "location_id": "gbp_location_id",
    "gbp_location_id": "gbp_location_id",
}


def _normalize_gbp_fields(gbp):
    """Map a portal `gbp` edit object (topicType/ctaType/... OR the column names) to
    content_calendar columns. Unknown keys are ignored. Returns {} when nothing maps."""
    if not isinstance(gbp, dict):
        return {}
    out = {}
    for k, v in gbp.items():
        col = _GBP_FIELD_MAP.get(k)
        if col:
            out[col] = v
    return out


def _rollback_denied_asset(account_key, row):
    """Portal-deny belt-and-suspenders: return a denied row's staged asset to its
    selector pool right now, rather than leaving it to the once-a-day
    observe_denials sweep (agent.runner.run_daily). Mirrors the SAME type test
    each selector's Slack-lane on_draft_denied hook runs on a Draft object --
    but read off the columns content_calendar ACTUALLY has. Measured against the
    live table 2026-08-30, because the obvious test was wrong: there is NO
    draft_type column at all (it reads None on every row), so keying on it would
    have made this whole function a silent no-op for gym media, which is the
    main case it exists for. The real signals, from 229 live ENG rows and 483
    LASSO rows:
      - podcast_selector  : pillar == 'podcast'. All 9 LASSO podcast rows carry
                            NO source_media_asset_id, so this test is checked
                            FIRST and can never be shadowed by the next one.
      - gym_media_selector: a non-empty source_media_asset_id. Present on
                            exactly the photo pillars (faces/community/results,
                            172 of 229) and absent on every generated pillar
                            (about/service/testimonial/offer).
    A row that matches NEITHER (or a missing post_date) is left alone: guessing
    the wrong selector would re-pool the WRONG asset, which is worse than the
    24h delay this function exists to close. Returns True only when a rollback
    actually ran.

    DATE-SCOPED, on purpose (prior incident: a cross-date rollback re-pooled a
    photo that was LIVE and already published on a different day). Both
    gym_media_selector.rollback_use(gym_id, post_date, *, asset_id=None) and
    podcast_selector.rollback_use(gym_id, post_date) key their kv record off
    THIS row's own post_date, so a use-record from any other date is never
    touched. For gym_media, asset_id further narrows the rollback to THIS
    row's own source_media_asset_id -- required because a 2x day can stage two
    gym-media posts on one date, and denying one must return only ITS photo,
    leaving the day's other (still-standing) post's stamp alone.

    IDEMPOTENT: both rollback_use implementations short circuit on their
    record's own `rolled_back` flag, so calling this twice for the same deny
    (or the nightly sweep running again afterward) is a no-op the second time,
    never a double-rollback."""
    pillar = str(row.get("pillar") or "").strip().lower()
    post_date = row.get("post_date")
    if not post_date:
        return False
    if pillar == "podcast":
        from . import podcast_selector
        return podcast_selector.rollback_use(account_key, post_date)
    asset_id = str(row.get("source_media_asset_id") or "").strip()
    if asset_id:
        from . import gym_media_selector
        return gym_media_selector.rollback_use(account_key, post_date,
                                               asset_id=asset_id)
    return False


def _handle_action_supabase(action, account_key, draft_id, note, reason="", gbp=None):
    """
    Supabase content_calendar action path. approve/deny/kill flip status; edit
    keeps status pending and echoes the note (no schema change, never fails on a
    missing note column). TOKEN ISOLATION is double guarded: a pre fetch by id
    scoped to gym_id, plus the gym_id filter on the PATCH itself. A row whose
    gym_id != account_key (or a missing row) is a 404 that never reveals it
    exists and never issues a write. NOTHING here publishes.

    reason (optional): the approver's explicit 'reason why', distinct from the new
    caption; recorded into the gym's brain as the edit's style rule (Dale, 2026-08-15).
    """
    try:
        sb = _pcs.SupabaseCalendarStore()
        # Pre check: the row must exist AND belong to this gym.
        row = sb.get_row(account_key, draft_id)
        if row is None:
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}

        # PUBLISHED IS FINAL: the creative is already live on the gym's page. No
        # portal action may rewrite it — an edit here would show a pending draft the
        # client believes changed the live post (it never will), and approve/deny/kill
        # would corrupt the publish record. 'publishing' is mid-claim: an action now
        # would flip the row back to claimable and DOUBLE-POST.
        _status_now = str(row.get("status") or "").lower()
        if _status_now == "published":
            return 409, {"ok": False, "action": action, "draft_id": draft_id,
                         "error": "this post is already published; it can no longer "
                                  "be edited, denied, or killed from the portal"}
        if _status_now == "publishing":
            return 409, {"ok": False, "action": action, "draft_id": draft_id,
                         "error": "this post is publishing right now; try again in "
                                  "a minute once it lands"}

        if action == "edit":
            # G1: an edit may change the caption (note), the GBP structured fields (gbp),
            # or both. At least one is required.
            gbp_fields = _normalize_gbp_fields(gbp)
            if not note and not gbp_fields:
                return 400, {"ok": False, "action": "edit", "draft_id": draft_id,
                             "error": "a new caption (note) or gbp fields is required "
                                      "for edit"}
            # Validate the GBP structured fields BEFORE any write (the worker re-validates
            # the full payload at send, but reject an obviously bad topic/CTA here).
            if gbp_fields:
                from . import gbp as _gbp
                tt = gbp_fields.get("gbp_topic_type")
                if tt is not None and tt not in _gbp.TOPIC_TYPES:
                    return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                                 "error": f"invalid gbp topic type: {tt}"}
                ct = gbp_fields.get("gbp_cta_type")
                if ct is not None and ct not in _gbp.CTA_TYPES:
                    return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                                 "error": f"invalid gbp cta type: {ct}"}
            before = row.get("caption") or ""
            updated = None
            reburned = None
            if note:
                # FABRICATION GATE (same gate as the Part-B route): a note that introduces
                # a stat/percentage/price with no approved receipt NEVER enters the caption.
                # Gated against THIS gym's own approved claims + dash-cleaned first
                # (audit 2026-08-25: LASSO's global stats must not clear a client claim,
                # and a dashed edit must not reach media).
                from . import rotation as _rotation
                from .portal_social import (_clean_edit_note, _edit_gate_claims,
                                            _split_note_meta)
                note = _clean_edit_note(note)
                # a '[why] ...' rationale pasted into the note is captured as the
                # reason, never saved into the caption (ENG 2026-08-23 leak class).
                note, reason = _split_note_meta(note, reason)
                if not note:
                    return 400, {"ok": False, "action": "edit", "draft_id": draft_id,
                                 "error": "the note is only an edit-rationale block "
                                          "([why]/[reason]); send the new caption "
                                          "text itself"}
                if not _rotation.is_gate_clean(
                        note, approved_claims=_edit_gate_claims(account_key)):
                    return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                                 "error": "fabrication gate: the note carries a claim "
                                          "with no approved receipt. Cite an approved "
                                          "source or drop the figure."}
                updated = sb.patch_caption(account_key, draft_id, note)
                if updated is None:
                    return 404, {"ok": False, "error": "draft not found",
                                 "draft_id": draft_id}
                # DURABLE-FIRST (Dale, 2026-08-17): the caption is saved. LEARN best-effort
                # so future captions match the approver's taste; the reason (when sent) is
                # captured as the edit's rule. Guarded so a slow brain write can NEVER flip
                # a persisted edit into an error the client keeps retrying.
                try:
                    from .portal_social import _learn_from_edit
                    _learn_from_edit(account_key, before, note, reason=reason)
                except Exception:
                    pass
                # Task #28 (§5c): a STORY caption edit re-burns onto fresh media at once
                # (gated + best-effort; the caption is already saved).
                try:
                    from .portal_social import maybe_reburn_story
                    reburned = maybe_reburn_story(account_key, row, note, sb)
                except Exception:
                    reburned = None
            if gbp_fields:
                updated = sb.patch_gbp_fields(account_key, draft_id, gbp_fields)
                if updated is None:
                    return 404, {"ok": False, "error": "draft not found",
                                 "draft_id": draft_id}
            return 200, {"ok": True, "action": "edit", "draft_id": draft_id,
                         "caption": (updated.get("caption", "") if updated else ""),
                         "status": (updated.get("status", "pending") if updated else "pending"),
                         "day_key": (updated.get("post_date", "") if updated else ""),
                         # Task #28: echo the reason so the portal repopulates the "Why"
                         # field and shows it separately (never appended to the caption).
                         "reason": (reason or ""),
                         "reason_captured": bool((reason or "").strip()),
                         "story_reburned": bool(reburned),
                         "gbp_updated": sorted(gbp_fields.keys())}

        if action == "requeue":
            # G2: a coach fixes a FAILED row and requeues. If the caption WORDS changed,
            # it re-enters OWNER approval ('pending'); otherwise it goes straight back to
            # the publish queue ('approved'). reject_reason is cleared either way. Only a
            # failed row can be requeued.
            if _status_now != "failed":
                return 409, {"ok": False, "action": "requeue", "draft_id": draft_id,
                             "error": "only a failed post can be requeued"}
            before = row.get("caption") or ""
            # same source rule as edit: a pasted rationale block moves to `reason`
            # and can never re-enter the caption through a requeue.
            from .portal_social import _split_note_meta
            note, reason = _split_note_meta(note, reason)
            changed = bool((note or "").strip()) and note.strip() != before.strip()
            if changed:
                from . import rotation as _rotation
                if not _rotation.is_gate_clean(note):
                    return 422, {"ok": False, "action": "requeue", "draft_id": draft_id,
                                 "error": "fabrication gate: the new caption carries a "
                                          "claim with no approved receipt. Cite an "
                                          "approved source or drop the figure."}
                updated = sb.requeue(account_key, draft_id, new_status="pending",
                                     new_caption=note)
                if updated is not None:
                    try:
                        from .portal_social import _learn_from_edit
                        _learn_from_edit(account_key, before, note, reason=reason)
                    except Exception:
                        pass
            else:
                updated = sb.requeue(account_key, draft_id, new_status="approved")
            if updated is None:
                return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
            return 200, {"ok": True, "action": "requeue", "draft_id": draft_id,
                         "status": updated.get("status", ""),
                         "words_changed": changed,
                         "caption": updated.get("caption", "")}

        new_status = _pcs.action_status(action)
        if new_status is None:
            return 400, {"error": f"unknown action: {action}"}

        updated = sb.set_status(account_key, draft_id, new_status)
        if updated is None:
            # Zero rows matched the id+gym_id filter -> treat as not found.
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
        if action == "deny":
            # SYNCHRONOUS POOL RETURN (the 24h-refill-gap bug): the Slack/SQLite deny
            # lane returns a denied asset to its selector pool immediately via
            # approvals.handle_action (gym_media_selector.on_draft_denied /
            # podcast_selector.on_draft_denied), but the portal deny writes straight
            # to content_calendar and never runs through that hook. Without this call
            # a gym at its creative cap that denies a photo in the portal could not
            # refill the day it just freed until the once-a-day observe_denials sweep
            # (agent.runner.run_daily) caught up -- up to 24h later. Best effort: a
            # rollback failure must never break the deny response the client is
            # waiting on.
            try:
                _rollback_denied_asset(account_key, row)
            except Exception as exc:
                from . import ops_alerts
                ops_alerts.alert(
                    f"portal deny rollback failed for {account_key} draft "
                    f"{draft_id}: {type(exc).__name__}: {exc}")
        # Task #28 (false-approval fix): return the AUTHORITATIVE status + day_key of the
        # row actually written, so the portal updates ONLY that card from server truth and
        # never carries the badge onto the next post.
        return 200, {"ok": True, "action": action, "draft_id": draft_id,
                     "status": updated.get("status", ""),
                     "day_key": updated.get("post_date", "")}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


def _draft_to_dict(draft):
    if draft is None:
        return None
    return {
        "draft_id": draft.draft_id,
        "day_key": draft.day_key,
        "draft_type": getattr(draft, "draft_type", None),
        "status": draft.status.value if hasattr(draft.status, "value") else str(draft.status),
        "platform": getattr(draft, "platform", None),
        "caption": getattr(draft, "caption", None),
        "creative_public_url": getattr(draft, "creative_public_url", None),
        "scheduled_for": getattr(draft, "scheduled_for", None),
        "blocked_reason": getattr(draft, "blocked_reason", None),
    }
