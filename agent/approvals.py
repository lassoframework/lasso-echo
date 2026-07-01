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

from . import config, meta_publisher, gbp_publisher, postlog
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


def _is_approver(actor_slack_id):
    return actor_slack_id == config.APPROVER_SLACK_ID


def handle_action(action, draft, actor_slack_id, note="",
                  redraft_fn=None, publisher=None, logger=None, account=None):
    """
    Apply an approval action.

    redraft_fn(draft, note) -> new Draft   (used by the Edit path; injectable)
    publisher.publish(draft, account)      (injectable; defaults to meta_publisher)
    logger.log_post(...)                   (injectable; defaults to postlog)
    account                                (optional; falls back to registry lookup)
    """
    # --- approver gate ---
    if not _is_approver(actor_slack_id):
        return ActionResult(ok=False, action=action, draft_id=getattr(draft, "draft_id", ""),
                            detail=f"Denied: {actor_slack_id} is not the approver.")

    if draft.status == DraftStatus.BLOCKED:
        return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                            detail="Draft is blocked; nothing to act on.")

    action = (action or "").lower()

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
        result = pub.publish(draft, acct)   # draft-only guard lives inside publish()
        draft.status = DraftStatus.APPROVED
        # Meta returns media_id, GBP returns post_id — log whichever the result carries.
        post_ref = getattr(result, "media_id", "") or getattr(result, "post_id", "")
        log = logger or postlog
        log.log_post(
            account_key=draft.account_key,
            platform=draft.platform,
            caption=draft.caption,
            media_id=post_ref,
            mode=result.mode,                  # "published" or "would_publish"
            draft_id=draft.draft_id,
        )
        return ActionResult(ok=True, action="approve", draft_id=draft.draft_id,
                            detail=f"{result.mode}: media_id={post_ref or '-'}")

    return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                        detail=f"Unknown action '{action}'.")
