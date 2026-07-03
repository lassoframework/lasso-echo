"""
Per-account trust ladder, AS DATA (Stage 2/4 spine). ENFORCEMENT UNCHANGED.

Trust is earned PER ACCOUNT, never globally. A brand new account always starts
at FULL_APPROVAL no matter what any other account has earned. Levels change
ONLY by a hand-edited config (the Account entry in accounts.py), never by code.

  level 0  FULL_APPROVAL  every post waits for a human tap. The default forever
                          for new accounts. LASSO stays here until Blake raises
                          it by hand.
  level 1  ROUTINE_AUTO   routine calendar posts may auto-publish AFTER a human
                          approved the monthly calendar; anything off-template
                          still cards. NOT armed for anyone.

DOUBLE GATE: level 1 behavior additionally requires AGENT_TRUST_LADDER_ENABLED
(default OFF), so today nothing changes for any account even if a level typo
happens. A typo'd/unknown level fails SAFE to level 0. The runner still cards
every draft: `requires_approval` is the decision function the publish path
reads; the auto-publish branch itself is a deliberate by-hand wiring step for
the day Blake arms a level 1 account (nothing unattended flips it).
"""

import json
import os
from enum import IntEnum

from . import config


class TrustLevel(IntEnum):
    FULL_APPROVAL = 0      # every post waits for a human. The default, forever.
    ROUTINE_AUTO = 1       # routine posts inside a human-approved monthly calendar
    TRUSTED = 2            # (future) wider auto-publish, off-template still surfaces


def default_trust_for_new_account() -> TrustLevel:
    """Every new account/client starts here. Non-negotiable."""
    return TrustLevel.FULL_APPROVAL


def effective_level(account) -> TrustLevel:
    """The account's trust level, coerced FAIL-SAFE: anything that is not a clean
    known level (a typo, a string, None, an out-of-range int) is level 0."""
    raw = getattr(account, "trust", TrustLevel.FULL_APPROVAL)
    try:
        level = TrustLevel(int(raw))
    except (ValueError, TypeError):
        return TrustLevel.FULL_APPROVAL
    return level


def approved_calendar(account_key, month):
    """The human-approved monthly calendar for (account, month): a list of
    creative keys a human explicitly approved, stored in the /data kv store by a
    BY-HAND step. Missing or unreadable = empty = everything cards."""
    from . import db
    try:
        raw = db.kv_get(f"approved_calendar_{account_key}_{month}", "")
        entries = json.loads(raw) if raw else []
        return set(entries) if isinstance(entries, list) else set()
    except Exception:
        return set()


def requires_approval(account, draft) -> bool:
    """
    Does this draft need a human tap before it can publish?

    Level 0 (default, everyone today): ALWAYS True.
    Level 1: True unless AGENT_TRUST_LADDER_ENABLED is armed AND the draft's
    creative is inside the human-approved calendar for its month. Anything
    off-template still cards. Unknown levels fail safe to level 0.
    """
    if not config.trust_ladder_enabled():
        return True  # the double gate: flag OFF means level data changes nothing
    if effective_level(account) < TrustLevel.ROUTINE_AUTO:
        return True
    month = (getattr(draft, "day_key", "") or "")[:7]
    if not month:
        return True  # no day context: card it
    creative_key = os.path.basename(getattr(draft, "creative_path", "") or "")
    if not creative_key:
        return True
    return creative_key not in approved_calendar(account.key, month)


def _has_published_before(account_key):
    """The FIRST-POST GATE data read: has this account EVER really published?"""
    try:
        from . import db
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE account_key=? AND mode='published'",
                (account_key,)).fetchone()
            return (row[0] or 0) > 0
    except Exception:
        return False  # unknown history reads as NO history: fail safe


def auto_eligibility(account, draft):
    """
    (eligible, reason) for trust auto-publish. HARD EXCLUSIONS, regardless of
    level or calendar: book campaign posts, comments, stories, any first post to
    a new audience (an account with zero real publishes), anything off template.
    Per account, never global.
    """
    if effective_level(account) < TrustLevel.ROUTINE_AUTO:
        return False, "account is level 0: full approval on everything"
    draft_type = getattr(draft, "draft_type", "") or ""
    if draft_type == "book":
        return False, "book campaign posts always card for approval"
    if draft_type.startswith("comment"):
        return False, "comment replies always card for approval"
    if getattr(draft, "is_story", False):
        return False, "stories are not calendar routine; always card"
    if not _has_published_before(account.key):
        return False, ("first post to this audience is never automated; "
                       "a human sends the first one")
    month = (getattr(draft, "day_key", "") or "")[:7]
    if not month:
        return False, "no day context; card it"
    creative_key = os.path.basename(getattr(draft, "creative_path", "") or "")
    if not creative_key or creative_key not in approved_calendar(account.key, month):
        return False, "off template: not in the human-approved monthly calendar"
    return True, ("calendar routine inside the approved monthly calendar "
                  f"for {month}, account level {int(effective_level(account))}")
