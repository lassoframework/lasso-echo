"""
Gym-facing SUPPORT INBOX.

A gym owner sends a support message from their portal; it lands as ONE Slack
message in the LASSO support channel, stamped with WHO it is from (gym display
name + account_key + any known owner contact) so Blake can reply ASAP. The gym
never sees a token or a channel id — everything here is server-side.

Rails (never weaken):
  - INERT without config.support_channel_id(): no channel -> {ok:false}, Slack
    never touched. The HTTP route additionally gates on
    config.support_inbox_enabled() (default OFF), so the route is dark until armed.
  - Rate-limited per gym (same sliding-window limiter the intake/studio writes use)
    and length-capped (SUPPORT_MSG_MAX chars) so an unthrottled loop can't hammer
    Slack.
  - copy_gate is NOT applied (it's the client's own words), but the message and the
    resolved header are scrubbed of any secret-looking env value via ops_alerts.scrub
    before anything leaves this module.
  - NEVER raises out: a Slack failure returns {ok:false, ...} and is logged, never
    crashes the request. The bot token and the raw portal token are never logged.
  - Tenant isolation: the message is stamped with THIS token's gym only. The gym
    identity is resolved from the shared plane; we never fabricate a name — an
    unknown gym falls back to the account_key.
"""

from datetime import datetime, timezone

from . import config, ops_alerts

# Length cap on the client's message. Slack's own text limit is ~40k; we cap far
# lower so a single support request stays one scannable message and a runaway
# paste can't balloon the post.
SUPPORT_MSG_MAX = 4000

# Per-gym rate limit: at most this many support posts per rolling minute, keyed by
# the gym's account_key hash prefix. Reuses intake_web's sliding-window limiter so
# there is ONE rate-limit implementation in the codebase.
SUPPORT_RATE_PER_MINUTE = 5

# In-process sliding-window hits, keyed by account_key hash prefix. Separate from
# intake_web's token limiter so a gym's normal portal traffic doesn't starve its
# support budget (and vice versa).
_support_hits: dict = {}


def _acct_hash_prefix(account_key: str) -> str:
    """First 16 hex chars of the SHA-256 of the account_key (never a raw token)."""
    import hashlib
    return hashlib.sha256((account_key or "").encode()).hexdigest()[:16]


def _allow(account_key: str, now=None) -> bool:
    """Sliding-window rate limit, keyed by the account_key hash prefix. False when
    over SUPPORT_RATE_PER_MINUTE in the last 60s; True otherwise."""
    import time
    now = now if now is not None else time.monotonic()
    key = _acct_hash_prefix(account_key)
    window = [t for t in _support_hits.get(key, []) if now - t < 60.0]
    if len(window) >= SUPPORT_RATE_PER_MINUTE:
        _support_hits[key] = window
        return False
    window.append(now)
    _support_hits[key] = window
    return True


def _refund(account_key: str) -> None:
    """Give back the rate-limit hit a FAILED send consumed. Without this, five
    consecutive Slack failures ate the gym's whole per-minute budget and the sixth
    attempt was refused as 'rate_limited' — so an outage silently turned into a
    lockout on the one surface a stuck client uses to reach us."""
    key = _acct_hash_prefix(account_key)
    window = _support_hits.get(key) or []
    if window:
        _support_hits[key] = window[:-1]


def _escalate_undelivered(account_key, display_name, text_in, reason):
    """A support message we could not deliver must NOT evaporate. Re-post it to the
    OPS channel (a different channel from the support one, so the single most likely
    failure — SUPPORT_CHANNEL_ID unset or the bot not in that channel — is covered)
    and make the failure visible. Best effort; never raises."""
    try:
        ops_alerts.alert(
            f"SUPPORT MESSAGE UNDELIVERED ({reason}) from {display_name} "
            f"({account_key}). The gym could not reach #echosupport, so their words "
            f"are here instead. Reply to them directly:\n{text_in}",
            force=True)
        return True
    except Exception as e:  # noqa: BLE001 - the request must still return honestly
        print(f"[support-inbox] escalation also failed: {type(e).__name__}: {e}")
        return False


def resolve_gym_identity(account_key: str):
    """
    (display_name, owner) for a gym, resolved from what we actually know — never
    fabricated. Layered, cheapest first, each layer wrapped so a miss/outage is an
    honest fallback and never a crash:

      1. local echo.db gym_get -> display_name/gym_name (the same source the
         connect page uses);
      2. the shared plane: SupabaseCalendarStore.resolve_gym_uuid(account_key) ->
         the gyms.name for that uuid, and PortalGymsReader.owner_names -> owner_name.

    display_name falls back to the account_key when nothing clean is known. owner is
    "" when unknown (a missing owner never blocks a support request).
    """
    display_name = ""
    owner = ""

    # Layer 1: local db (no creds needed; present for onboarded gyms).
    try:
        from . import db as _db
        row = _db.gym_get(account_key) or {}
        display_name = (row.get("display_name") or row.get("gym_name") or "").strip()
    except Exception:  # noqa: BLE001 - a lookup failure is an honest fallback
        pass

    # Layer 2: the shared plane (name + owner). Only reached when creds are set.
    try:
        if config.portal_calendar_supabase_enabled():
            from . import portal_calendar_store as _pcs
            from . import portal_gyms as _pg
            store = _pcs.SupabaseCalendarStore()
            gym_uuid = store.resolve_gym_uuid(account_key)
            if gym_uuid:
                if not display_name:
                    name = _shared_gym_name(store, gym_uuid)
                    if name:
                        display_name = name
                reader = _pg.PortalGymsReader()
                if reader.available():
                    owner = (reader.owner_names([gym_uuid]) or {}).get(
                        str(gym_uuid), "") or ""
    except Exception:  # noqa: BLE001 - never let identity resolution crash a request
        pass

    if not display_name:
        display_name = account_key or "unknown gym"
    return display_name, owner


def _shared_gym_name(store, gym_uuid) -> str:
    """The gyms.name for a uuid on the shared plane, or "". Read-only, never raises."""
    try:
        r = store._client().get(
            store._rest("gyms"),
            params={"id": f"eq.{gym_uuid}", "select": "name", "limit": "1"},
            headers=store._headers(), timeout=30)
        if r.status_code >= 400:
            return ""
        rows = r.json() or []
        return (rows[0].get("name") or "").strip() if rows else ""
    except Exception:  # noqa: BLE001
        return ""


def _default_poster():
    """Injection seam for tests; the real SlackPoster in production, built with the
    DEDICATED support token (config.support_slack_bot_token()). The #echosupport
    channel is private and the default Echo bot is not a member — the member bot's
    xoxb token lives in AGENT_SUPPORT_SLACK_BOT_TOKEN and is used for THIS post only.
    Passing token= scopes the choice to this poster; every other alert/approval path
    keeps its own default token. The token is read by name inside config and never
    logged here."""
    from .slack_surface import SlackPoster
    return SlackPoster(token=config.support_slack_bot_token())


def _format_message(display_name: str, account_key: str, message: str,
                    owner: str) -> str:
    """The one support-request Slack message. Scannable header + body + contact +
    timestamp. Every interpolated value is scrubbed of secret env values before it
    is assembled (the body is the client's own words; the identity comes from our
    own store, but scrub is belt-and-suspenders)."""
    header = (f"New support request — {display_name} ({account_key})")
    lines = [header, ""]
    lines.append(ops_alerts.scrub(message))
    if owner:
        lines.append("")
        lines.append(f"Owner: {ops_alerts.scrub(owner)}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append("")
    lines.append(f"Received {stamp}")
    return ops_alerts.scrub("\n".join(lines))


def submit_support_message(account_key, message, *, poster=None, store=None):
    """
    Post ONE support request to the configured Slack support channel, stamped with
    the gym's identity. Returns {ok, delivered, reason}:

      {ok:false, delivered:false, reason:"empty"}          - blank message
      {ok:false, delivered:false, reason:"no_channel"}     - channel id unset (inert)
      {ok:false, delivered:false, reason:"rate_limited"}   - over the per-gym budget
      {ok:false, delivered:false, reason:"slack_failed"}   - Slack said no / outage
      {ok:true,  delivered:true,  reason:""}               - posted

    `store` is unused today (identity is resolved internally) but kept in the
    signature so a future caller can inject a resolver; `poster` is the injection
    seam the tests use. NEVER raises: a Slack failure returns {ok:false} and logs.
    """
    text_in = (str(message) if message is not None else "").strip()
    if not text_in:
        return {"ok": False, "delivered": False, "reason": "empty"}

    channel = config.support_channel_id()
    if not channel:
        # No support channel configured. This used to drop the gym's message on the
        # floor with nobody told — the most likely silent-drop mode in production,
        # because the client just sees "try again" and retries forever. Escalate to
        # ops so a human still gets the words.
        display_name, _owner = resolve_gym_identity(account_key)
        _escalate_undelivered(account_key, display_name, text_in, "no support channel configured")
        return {"ok": False, "delivered": False, "reason": "no_channel"}

    if not _allow(account_key):
        return {"ok": False, "delivered": False, "reason": "rate_limited"}

    # Length cap: keep the request one scannable message.
    if len(text_in) > SUPPORT_MSG_MAX:
        text_in = text_in[:SUPPORT_MSG_MAX].rstrip() + " …(truncated)"

    display_name, owner = resolve_gym_identity(account_key)
    text = _format_message(display_name, account_key, text_in, owner)

    poster = poster or _default_poster()
    try:
        # Post to the SUPPORT channel explicitly (not the poster's default channel),
        # via the same transport ops_alerts / cards ride on.
        resp = poster._chat_post(text=text, blocks=None, channel=channel)
    except Exception as e:  # noqa: BLE001 - a Slack failure must never crash the request
        print(f"[support-inbox] post failed: {type(e).__name__}")
        _refund(account_key)
        _escalate_undelivered(account_key, display_name, text_in,
                              f"slack transport failed ({type(e).__name__})")
        return {"ok": False, "delivered": False, "reason": "slack_failed"}

    if not isinstance(resp, dict) or not resp.get("ok"):
        # Transport degraded / Slack rejected: honest failure, already logged inside
        # the poster; surface {ok:false} without leaking the response.
        print("[support-inbox] Slack did not confirm the support post "
              f"(gym={account_key})")
        _refund(account_key)
        _escalate_undelivered(account_key, display_name, text_in,
                              "slack did not confirm the post")
        return {"ok": False, "delivered": False, "reason": "slack_failed"}

    return {"ok": True, "delivered": True, "reason": ""}
