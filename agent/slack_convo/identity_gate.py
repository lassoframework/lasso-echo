"""
identity_gate.py — who is this Slack user to us? staff, client, coach, or unknown.

Blake (spec item 3): "resolve the Slack user to a known account (LASSO staff, a client gym,
or a coach). Unknown user gets one templated reply asking them to use the portal support
page, and the ticket routes to me only. No fix, no answer, no client message for an
unresolved identity."

Direction: the REVERSE of scout-listener's slack-directory.js (which maps an account to a
Slack user for outbound). Here a Slack user id arrives and we need the account behind it:

    Slack users.info (email, guest flags)
      -> portal app_users by email (role)
      -> gym_assignments (relationship, gym)
      -> echo_intake_tokens.echo_account_key (the Echo account for that gym)

Rules, in order, first match wins:
  1. a bot user                            -> BOT       (the adapter ignores it)
  2. id in the operator list (Blake, team) -> STAFF     (no lookup needed; a pinned fact)
  3. portal role owner / executive         -> STAFF
  4. portal role coach                     -> COACH
  5. portal client with a client_owner gym -> CLIENT    (carries account_key + gym_id)
  6. anything else, or ANY lookup failure  -> UNKNOWN

"Anything else" deliberately includes a full workspace member with no portal row and a
guest with no matching assignment. Unknown is the SAFE direction: it gets a templated
redirect and a route to Blake, never a fix, never an answer, never account data. Resolution
failures fall the same way -- a Slack outage must never promote a stranger to a client.

All I/O is injected; this module holds no tokens and makes no network calls of its own.
"""
from dataclasses import dataclass, field

BOT = "bot"
STAFF = "staff"
COACH = "coach"
CLIENT = "client"
UNKNOWN = "unknown"

STAFF_ROLES = frozenset({"owner", "executive"})
COACH_ROLES = frozenset({"coach"})


@dataclass(frozen=True)
class Identity:
    kind: str                        # BOT | STAFF | COACH | CLIENT | UNKNOWN
    slack_user_id: str
    email: str = ""
    display: str = ""
    account_key: str = ""            # Echo account key, CLIENT only
    gym_id: str = ""                 # portal gym uuid, CLIENT only
    reason: str = ""                 # why we landed here (audit trail, never shown to clients)

    @property
    def is_human_known(self) -> bool:
        return self.kind in (STAFF, COACH, CLIENT)

    @property
    def is_client(self) -> bool:
        return self.kind == CLIENT


def resolve(slack_user_id, *, slack_user_info, portal_lookup, operator_ids=()):
    """Resolve one Slack user id. Never raises; never guesses.

    slack_user_info(user_id) -> dict with keys: is_bot, email, real_name,
                                 is_restricted, is_ultra_restricted   (or None)
    portal_lookup(email)     -> dict with keys: role, gyms=[{gym_id, relationship,
                                 account_key}]                         (or None)
    operator_ids             -> iterable of Slack user ids that are LASSO staff by fiat
    """
    uid = str(slack_user_id or "").strip()
    if not uid:
        return Identity(UNKNOWN, uid, reason="empty user id")

    ops = {str(o).strip() for o in (operator_ids or ()) if str(o).strip()}
    if uid in ops:
        return Identity(STAFF, uid, reason="operator list")

    try:
        info = slack_user_info(uid) or {}
    except Exception as e:  # noqa: BLE001 - a lookup failure is UNKNOWN, never a promotion
        return Identity(UNKNOWN, uid, reason=f"slack lookup failed: {type(e).__name__}")

    if info.get("is_bot") or info.get("id") == "USLACKBOT":
        return Identity(BOT, uid, display=str(info.get("real_name") or ""), reason="bot user")

    email = str(info.get("email") or "").strip().lower()
    display = str(info.get("real_name") or info.get("name") or "").strip()
    if not email:
        return Identity(UNKNOWN, uid, display=display, reason="no email on slack profile")

    try:
        portal = portal_lookup(email) or None
    except Exception as e:  # noqa: BLE001
        return Identity(UNKNOWN, uid, email=email, display=display,
                        reason=f"portal lookup failed: {type(e).__name__}")
    if not portal:
        return Identity(UNKNOWN, uid, email=email, display=display,
                        reason="no portal user for this email")

    role = str(portal.get("role") or "").strip().lower()
    if role in STAFF_ROLES:
        return Identity(STAFF, uid, email=email, display=display, reason=f"portal role {role}")
    if role in COACH_ROLES:
        return Identity(COACH, uid, email=email, display=display, reason="portal role coach")

    gyms = [g for g in (portal.get("gyms") or [])
            if str(g.get("relationship") or "") == "client_owner"]
    if len(gyms) == 1 and gyms[0].get("gym_id"):
        g = gyms[0]
        return Identity(CLIENT, uid, email=email, display=display,
                        account_key=str(g.get("account_key") or ""),
                        gym_id=str(g.get("gym_id") or ""),
                        reason="portal client_owner")
    if len(gyms) > 1:
        # A multi-location owner is real, but WHICH gym this thread is about is not
        # knowable from identity alone. Unknown is safer than picking one gym's data.
        return Identity(UNKNOWN, uid, email=email, display=display,
                        reason=f"ambiguous: {len(gyms)} client_owner gyms")
    return Identity(UNKNOWN, uid, email=email, display=display,
                    reason=f"portal role {role or '(none)'} with no client_owner gym")
