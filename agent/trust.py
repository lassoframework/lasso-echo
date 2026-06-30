"""
Per-account trust ladder.

Trust is earned PER ACCOUNT, never globally. A brand new account always starts
at FULL_APPROVAL no matter what any other account has earned. That rule is what
keeps Stage 2 clients safe even after LASSO is on autopilot.

Stage 1: everything is FULL_APPROVAL and `requires_approval` always returns True.
The higher rungs exist in code so Stage 4 can climb the ladder without a rewrite,
but they are inert today.
"""

from enum import IntEnum


class TrustLevel(IntEnum):
    FULL_APPROVAL = 0      # every post waits for a human. Stage 1 default.
    ROUTINE_AUTO = 1       # (future) routine posts inside an approved calendar auto-publish
    TRUSTED = 2            # (future) wider auto-publish, off-template still surfaces


def default_trust_for_new_account() -> TrustLevel:
    """Every new account/client starts here. Non-negotiable."""
    return TrustLevel.FULL_APPROVAL


def requires_approval(account, draft) -> bool:
    """
    Does this draft need a human tap before it can publish?

    Stage 1: ALWAYS True. No exceptions.

    Stage 4 (not active): would return False only when the account has earned
    ROUTINE_AUTO or higher AND the draft matches an approved calendar template.
    Anything off-template always returns True.
    """
    # --- Stage 1 hard gate ---
    return True

    # --- Stage 4 sketch (intentionally unreachable until the ladder is armed) ---
    # if account.trust >= TrustLevel.ROUTINE_AUTO and draft.matches_approved_template:
    #     return False
    # return True
