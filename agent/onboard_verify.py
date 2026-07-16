"""
Onboarding self-check (Stage 2 T4).

Answers one question per gym: is this account ready to receive uploads, and is
it ready to publish?  Two readiness tiers:

    READY FOR UPLOADS   the intake token has been minted.  Content can flow in.
    READY TO PUBLISH    ALSO: publish creds were set by hand and the publish
                        flag is ON.  Nothing in this module arms the flag or
                        creates credentials.

Every check is a boolean or a short status string.  The function never raises
for a missing or unknown gym; missing data is reported as UNKNOWN or False so
the operator sees the gap instead of a crash.

THE ONE HUMAN LINE: publish credentials are set by Blake by hand, always.
This module records "NOT SET (by hand)" and stops.  It never reads, creates,
prints, or infers a Meta token or any publishing secret.
"""

import os
from datetime import date

from . import db as _db
from .intake_tokens import token_status as _token_status
from .trust import effective_level as _effective_level, TrustLevel


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _voice_path(account_key, root="."):
    """The expected brand_voice/<key>.md path."""
    return os.path.join(root, "brand_voice", f"{account_key}.md")


def _brain_path(account_key, root="."):
    """The expected brains/<key>.md path."""
    return os.path.join(root, "brains", f"{account_key}.md")


def _this_month():
    return date.today().strftime("%Y-%m")


def _check_approved_calendar(account_key, conn=None):
    """True when the kv store has a non-empty calendar approval for this month."""
    month = _this_month()
    kv_key = f"approved_calendar_{account_key}_{month}"
    if conn is not None:
        row = conn.execute(
            "SELECT value FROM kv WHERE key=?", (kv_key,)).fetchone()
        val = row["value"] if row else ""
    else:
        val = _db.kv_get(kv_key, "")
    return bool(val)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def verify_gym(account_key, db_conn=None, root="."):
    """
    Run the onboarding self-check for one gym.

    Returns a dict:
        token_minted          bool   gym has an active intake token
        voice_scaffolded      bool   brand_voice/<key>.md exists on disk
        brain_present         bool   brains/<key>.md exists on disk
        trust_full_approval   bool   trust level is 0 (FULL_APPROVAL)
        publish_flag_off      bool   publish_flag == 'OFF' in the gym row
        publish_creds_status  str    'NOT SET (by hand)' or 'SET'
        slack_channel_set     bool   account.slack_channel is non-empty
        approver_set          bool   account.approvers is non-empty
        first_month_approved  bool   approved_calendar kv key exists and is non-empty
        ready_for_uploads     bool   token_minted is True
        ready_to_publish      bool   also publish_creds_status == 'SET'
                                     and publish_flag_off is False

    Never raises; missing gym or missing fields are reported as UNKNOWN/False.
    """
    from .accounts import get_account

    # ---------- gym row (publish flag and creds) ----------
    gym_row = _db.gym_get(account_key, conn=db_conn)
    if gym_row is None:
        publish_flag_val = "UNKNOWN"
        publish_creds_status = "NOT SET (by hand)"
    else:
        publish_flag_val = (gym_row.get("publish_flag") or "OFF").upper()
        raw_creds = (gym_row.get("publish_creds") or "").strip()
        publish_creds_status = raw_creds if raw_creds else "NOT SET (by hand)"

    publish_flag_off = (publish_flag_val in ("OFF", "UNKNOWN"))

    # ---------- intake token ----------
    tok = _token_status(account_key)
    token_minted = tok.get("minted", False)

    # ---------- disk checks ----------
    voice_scaffolded = os.path.isfile(_voice_path(account_key, root))
    brain_present = os.path.isfile(_brain_path(account_key, root))

    # ---------- account checks ----------
    acct = get_account(account_key)
    if acct is not None:
        trust_full_approval = (_effective_level(acct) == TrustLevel.FULL_APPROVAL)
        slack_channel_set = bool(getattr(acct, "slack_channel", "") or "")
        approver_set = bool(getattr(acct, "approvers", None))
    else:
        # tenant system: load from tenants table if present
        trust_full_approval = True   # new gym default: full approval
        slack_channel_set = False
        approver_set = False
        # try tenant record
        try:
            from .tenants import load_tenant
            tenant = load_tenant(account_key)
            if tenant is not None:
                trust_val = tenant.get("trust", 0)
                try:
                    trust_full_approval = (TrustLevel(int(trust_val)) == TrustLevel.FULL_APPROVAL)
                except (ValueError, TypeError):
                    trust_full_approval = True
                approver_set = bool(
                    tenant.get("approver_name") or tenant.get("approver", {})
                )
        except Exception:
            pass

    # ---------- calendar approval ----------
    first_month_approved = _check_approved_calendar(account_key, conn=db_conn)

    # ---------- readiness tiers ----------
    ready_for_uploads = token_minted
    ready_to_publish = (
        ready_for_uploads
        and publish_creds_status == "SET"
        and not publish_flag_off
    )

    return {
        "account_key": account_key,
        "token_minted": token_minted,
        "voice_scaffolded": voice_scaffolded,
        "brain_present": brain_present,
        "trust_full_approval": trust_full_approval,
        "publish_flag_off": publish_flag_off,
        "publish_creds_status": publish_creds_status,
        "slack_channel_set": slack_channel_set,
        "approver_set": approver_set,
        "first_month_approved": first_month_approved,
        "ready_for_uploads": ready_for_uploads,
        "ready_to_publish": ready_to_publish,
    }


def verify_all(db_conn=None, root="."):
    """
    Run verify_gym for every gym in the gyms table.

    Returns a list of result dicts (one per gym).  If the table is empty,
    returns an empty list.
    """
    gyms = _db.gym_list(conn=db_conn)
    return [verify_gym(g["account_key"], db_conn=db_conn, root=root) for g in gyms]


# ---------------------------------------------------------------------------
# CLI output helpers (called by __main__)
# ---------------------------------------------------------------------------

def _yn(val):
    return "YES" if val else "NO"


def _ready_to_publish_reason(result):
    """One-line reason string when READY TO PUBLISH is NO."""
    if not result["ready_for_uploads"]:
        return "NO (reason: token not minted)"
    if result["publish_creds_status"] != "SET":
        return "NO (reason: publish creds pending by hand)"
    if result["publish_flag_off"]:
        return "NO (reason: publish flag is OFF)"
    return "YES"


def format_result(result):
    """Return a list of lines for one gym (no em dashes, no hyphens in copy)."""
    key = result["account_key"]
    lines = [
        f"=== {key} ===",
        f"Token minted:       {_yn(result['token_minted'])}",
        f"Voice scaffolded:   {_yn(result['voice_scaffolded'])}",
        f"Brain present:      {_yn(result['brain_present'])}",
        f"Trust level:        {'FULL APPROVAL' if result['trust_full_approval'] else 'ELEVATED'}",
        f"Publish flag:       {'OFF' if result['publish_flag_off'] else 'ON'}",
        f"Publish creds:      {result['publish_creds_status']}",
        f"Slack channel:      {'SET' if result['slack_channel_set'] else 'MISSING'}",
        f"Approver:           {'SET' if result['approver_set'] else 'MISSING'}",
        f"First-month plan:   {'APPROVED' if result['first_month_approved'] else 'PENDING'}",
        "",
        f"READY FOR UPLOADS:  {_yn(result['ready_for_uploads'])}",
        f"READY TO PUBLISH:   {_ready_to_publish_reason(result)}",
    ]
    if result["ready_to_publish"] and not result["trust_full_approval"]:
        lines.append(
            "WARNING: account is READY TO PUBLISH but trust is not FULL APPROVAL. "
            "Confirm the trust level is intentional before arming."
        )
    lines.append("")
    return lines
