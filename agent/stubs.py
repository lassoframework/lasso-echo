"""
Clean hooks for later stages. NOT built in Stage 1.

Each raises NotImplementedYet so nothing silently half-runs. The docstrings
describe the eventual behavior AND the gate each must honor when it ships.
Slot real logic in here; the rest of the agent already calls the right seams.
"""


class NotImplementedYet(Exception):
    pass


# ---- Stories (later) ---------------------------------------------------------
def post_story(account, creative, voice):
    """
    Draft and (on approval) publish a Story to IG/FB.
    Gate: same as feed — first story to any audience waits for approval; honors
    the per-account trust ladder once armed.
    """
    raise NotImplementedYet("Stories posting is a later stage.")


# ---- Comment handling (later, public + risky) --------------------------------
def handle_comment(account, comment):
    """
    Comment policy:
      Tier 1 (auto-safe): like/heart positive comments; templated thank-you on
        approval.
      Tier 2 (surface): questions, complaints, price, hours, injuries, refunds,
        anything negative -> agent DRAFTS a reply and HOLDS it. Never auto-sends
        a substantive reply early.
      DMs: never auto-handle a DM as first contact. Same rule as leads.
    """
    raise NotImplementedYet("Comment handling is a later stage.")


# ---- 30-day creative refresh loop (the product) ------------------------------
def run_monthly_refresh(account):
    """
    Every 30 days: read what worked vs flopped (from the reporting data), propose
    new creative angles grounded in that data, and ask the client for fresh raw
    material to fill the gaps. This monthly loop is THE product. Proposes only;
    never invents brand claims or offers.
    """
    raise NotImplementedYet("Monthly creative refresh is a later stage.")


# ---- Portal wiring (later) ---------------------------------------------------
def read_portal_library(account):
    """
    Later: read the client's uploaded creative library from the LASSO Ops portal
    instead of a local folder. Replaces library.py's local read. Read-only.
    """
    raise NotImplementedYet("Portal library read is a later stage.")


def write_portal_report(account, metrics):
    """
    Later: write the per-account, per-30-day metrics to the portal's reporting
    dashboard (engagement, saves, likes, comments, reach, follower growth,
    posting frequency before/after, top 3 / bottom 3 posts, health read).
    """
    raise NotImplementedYet("Portal reporting write is a later stage.")
