"""
Approval handling. This is the hard human gate.

  - Only APPROVER_SLACK_ID can act. Anyone else is denied, logged, ignored.
  - Approve -> publish (or 'would publish' in draft-only) + log.
  - Edit    -> apply Blake's note, re-draft, re-post the card for approval.
  - Skip    -> drop the draft.

Nothing here can publish on its own. Publishing requires (a) a human Approve
from the right person AND (b) the publish flag armed. Both. Always.
"""

from dataclasses import dataclass

from . import config, meta_publisher, gbp_publisher, ops_alerts, postlog, publish_confirm
from .accounts import get_account, Platform
from .drafter import Draft, DraftStatus


def _publisher_for(account):
    """Route by platform: Google Business Profile -> gbp_publisher; everything else
    (Instagram, Facebook) -> meta_publisher. The Meta path is unchanged."""
    if account is not None and getattr(account, "platform", "") == Platform.GOOGLE_BUSINESS:
        return gbp_publisher
    return meta_publisher


@dataclass
class ActionResult:
    ok: bool
    action: str
    draft_id: str
    detail: str = ""
    redraft: object = None  # a new Draft to re-post (Edit path)


def _is_approver(actor_slack_id, account=None):
    """The global approver can act on every account; an account's own
    approvers (per-client, from the Account record) can act on theirs.
    account.approver_ids() already falls back to the global approver when
    the per-account list is empty, so today's behavior is unchanged until a
    client account sets its approvers."""
    if actor_slack_id == config.APPROVER_SLACK_ID:
        return True
    if account is not None:
        try:
            return actor_slack_id in account.approver_ids()
        except Exception:
            return False
    return False


def handle_action(action, draft, actor_slack_id, note="",
                  redraft_fn=None, publisher=None, logger=None, account=None,
                  confirmer=None, **kwargs):
    """
    Apply an approval action.

    redraft_fn(draft, note) -> new Draft   (used by the Edit path; injectable)
    publisher.publish(draft, account)      (injectable; defaults to meta_publisher)
    logger.log_post(...)                   (injectable; defaults to postlog)
    account                                (optional; falls back to registry lookup)
    confirmer(draft, account, result)      (injectable; defaults to publish_confirm,
                                            which is a no-op unless its flag is armed)
    """
    # --- approver gate: global approver, or this account's own approvers ---
    gate_acct = account or get_account(getattr(draft, "account_key", "") or "")
    if not _is_approver(actor_slack_id, account=gate_acct):
        return ActionResult(ok=False, action=action, draft_id=getattr(draft, "draft_id", ""),
                            detail=f"Denied: {actor_slack_id} is not the approver.")

    if draft.status == DraftStatus.BLOCKED:
        return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                            detail="Draft is blocked; nothing to act on.")

    if draft.status == DraftStatus.SUPERSEDED:
        # A superseded draft can never publish. One clear line, nothing else happens.
        return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                            detail="This draft was superseded by a newer draft for the "
                                   "same account and day, so nothing was published. "
                                   "Use the newest card instead.")

    if draft.status == DraftStatus.EXPIRED:
        # An expired draft can never publish. Same friendly no-op as a supersede.
        return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                            detail="This draft expired (its posting day has passed), "
                                   "so nothing was published. Use today's card instead.")

    action = (action or "").lower()

    if action == "kill":
        confirmed = kwargs.get("confirmed", False) if kwargs else False
        if not confirmed:
            return ActionResult(ok=False, action="kill", draft_id=draft.draft_id,
                                detail="Kill requires confirmation. Pass confirmed=True to proceed.")
        account_key = getattr(draft, "account_key", "") or ""
        creative_key = getattr(draft, "creative_path", "") or ""
        import os as _os
        creative_key = _os.path.basename(creative_key)
        try:
            from . import tenant_brain
            tenant_brain.record_event(account_key, "kill", concept=creative_key)
        except Exception:
            pass
        return ActionResult(ok=True, action="kill", draft_id=draft.draft_id,
                            detail="Concept banned for this gym. It will not be drafted again.")

    if action == "deny":
        account_key = getattr(draft, "account_key", "") or ""
        note = kwargs.get("note", "") if kwargs else ""
        draft.status = DraftStatus.BLOCKED
        try:
            from . import tenant_brain
            tenant_brain.record_event(account_key, "deny_reason", reason=note)
        except Exception:
            pass
        return ActionResult(ok=True, action="deny", draft_id=draft.draft_id,
                            detail="Draft denied. Recreate queued.")

    if action == "skip":
        draft.status = DraftStatus.SKIPPED
        return ActionResult(ok=True, action="skip", draft_id=draft.draft_id,
                            detail="Dropped.")

    if action == "edit":
        if not redraft_fn:
            return ActionResult(ok=False, action="edit", draft_id=draft.draft_id,
                                detail="No redraft function wired.")
        new_draft = redraft_fn(draft, note)
        new_draft.status = DraftStatus.PENDING
        return ActionResult(ok=True, action="edit", draft_id=draft.draft_id,
                            detail="Revised; re-posted for approval.", redraft=new_draft)

    if action == "approve":
        acct = account or get_account(draft.account_key)
        if acct is None:
            return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                                detail=f"Unknown account {draft.account_key}.")
        pub = publisher or _publisher_for(acct)
        try:
            result = pub.publish(draft, acct)   # draft-only guard lives inside publish()
        except meta_publisher.MediaNotReady as e:
            # KNOWN, RETRYABLE: the media container never finished processing on
            # Meta's side, so NOTHING published. Hold the card for a retry with a
            # clear, non-alarming note — never the loud "publish attempt failed"
            # alarm, because no post went out. The draft stays PENDING (we return
            # before marking APPROVED), so tapping Approve again retries it.
            ops_alerts.alert(f"held (media not ready) for {draft.account_key} draft "
                             f"{draft.draft_id}: {e} Nothing published; the card is "
                             "held. Approve again in a minute to retry.")
            return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                                detail="Held: the media was still processing on "
                                       "Meta's side after the wait window. Nothing "
                                       "was published. Approve again in a minute to "
                                       "retry.")
        except Exception as e:
            # Behavior unchanged (the error still raises to the caller); with
            # AGENT_OPS_ALERTS_ENABLED it also posts one loud ops alert first.
            ops_alerts.alert(f"publish attempt failed for {draft.account_key} draft "
                             f"{draft.draft_id}: {type(e).__name__}: {e}")
            raise
        draft.status = DraftStatus.APPROVED
        # Meta returns media_id, GBP returns post_id — log whichever the result carries.
        post_ref = getattr(result, "media_id", "") or getattr(result, "post_id", "")
        log = logger or postlog
        # reporting enrichment (best effort, never blocks the approve path)
        import os as _os
        creative_key = _os.path.basename(draft.creative_path or "")
        try:
            from .rotation import sidecar_archetype, sidecar_set
            archetype = sidecar_archetype(draft.creative_path)
            set_name = sidecar_set(draft.creative_path)
        except Exception:
            archetype = set_name = ""
        log.log_post(
            account_key=draft.account_key,
            platform=draft.platform,
            caption=draft.caption,
            media_id=post_ref,
            mode=result.mode,                  # "published" or "would_publish"
            draft_id=draft.draft_id,
            creative_key=creative_key,
            archetype=archetype,
            set_name=set_name,
        )
        # Publish confirmation loop: dormant behind AGENT_PUBLISH_CONFIRM_ENABLED
        # (returns None immediately when OFF, and only ever READS when ON).
        confirm = confirmer or publish_confirm.confirm_publish
        confirm(draft, acct, result)
        return ActionResult(ok=True, action="approve", draft_id=draft.draft_id,
                            detail=f"{result.mode}: media_id={post_ref or '-'}")

    return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                        detail=f"Unknown action '{action}'.")
