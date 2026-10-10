"""Default-off internal visibility for degraded support reminder reconciliation.

One attempt per hour per ops channel in this process, including ambiguous POSTs.
A restart resets this cooldown; this is a recurring health alert, not an exactly
once reminder transport. A timeout may have posted and remains unconfirmed.
No ticket, support message, client route, or client text is read or modified.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from uuid import UUID

from agent import config
from agent.jobs.client_support_scan_reminder import _enabled
from agent.slack_surface import SlackPoster

_LOCK = threading.Lock()
_LAST_ATTEMPT = {}
_MAX_IDS = 20
_COOLDOWN_SECONDS = 3600
_INTERNAL_SUPPORT_CHANNEL = "C0BTDAE1GLW"  # documented private #echosupport


def _degraded(report):
    if not isinstance(report, dict):
        return True, [], 0
    failed = report.get("ok") is not True
    items = []
    for field in ("skipped", "unrouted"):
        value = report.get(field)
        if isinstance(value, list):
            items.extend(value)
        elif value not in (None, [], 0):
            failed = True
    # Validate IDs by shape. Arbitrary report strings never become Slack copy.
    ids = set()
    for item in items:
        try:
            if isinstance(item, str):
                ids.add(str(UUID(item)))
        except (ValueError, AttributeError):
            pass
    return failed or bool(items), sorted(ids), len(items)


def run(report, *, poster=None, now=None):
    """Return confirmed/unconfirmed visibility; never report an uncertain send as success.

    ``poster`` is an offline test seam. Production uses the reviewed private
    support channel and its member bot token, never a per-ticket channel.
    """
    if not _enabled():
        return {"ok": True, "state": "disabled", "confirmed": False}
    degraded, ids, count = _degraded(report)
    if not degraded:
        return {"ok": True, "state": "healthy", "confirmed": False}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return {"ok": False, "state": "unconfirmed", "reason": "invalid_clock",
                "confirmed": False}
    channel = config.support_channel_id()
    token = config.support_slack_bot_token()
    # A typo or client-channel override must not turn a health alert into a
    # customer message. Changing the internal destination requires code review.
    if channel != _INTERNAL_SUPPORT_CHANNEL or not token:
        return {"ok": False, "state": "unconfirmed", "reason": "ops_route_unavailable",
                "confirmed": False}
    clock = now.timestamp()
    with _LOCK:
        last = _LAST_ATTEMPT.get(channel)
        if last is not None and clock - last < _COOLDOWN_SECONDS:
            return {"ok": False, "state": "cooldown", "confirmed": False}
        # Claim the attempt before any network call, including failed auth.
        _LAST_ATTEMPT[channel] = clock
    scan_failed = not isinstance(report, dict) or report.get("ok") is not True
    body = ("SUPPORT REMINDER HEALTH: internal reminder reconciliation is degraded. "
            f"Scan failed: {'yes' if scan_failed else 'no'}. "
            f"Skipped or unrouted entries: {count}. "
            f"Ticket IDs ({min(len(ids), _MAX_IDS)} of {len(ids)} verified IDs): "
            f"{', '.join(ids[:_MAX_IDS]) or 'none'}. "
            "A human should inspect the support reminder health. "
            "This alert does not change or close tickets.")
    poster = poster or SlackPoster(token=token, channel=channel)
    unconfirmed = {"ok": False, "state": "unconfirmed", "confirmed": False}
    try:
        auth = poster._send("https://slack.com/api/auth.test", {})
        sender = auth.get("user_id") if isinstance(auth, dict) else None
        if (not isinstance(auth, dict) or auth.get("ok") is not True
                or not isinstance(sender, str) or not sender.startswith(("U", "W"))
                or not auth.get("bot_id")):
            return {**unconfirmed, "reason": "bot_identity_unverified"}
        response = poster.post_notice(body)
        if (not isinstance(response, dict) or response.get("ok") is not True
                or response.get("channel") != channel
                or not isinstance(response.get("ts"), str) or not response["ts"]):
            return {**unconfirmed, "reason": "post_unconfirmed"}
        ts = response["ts"]
        readback = poster.read_conversation_messages(channel, ts=ts, oldest=ts)
        if (not isinstance(readback, dict) or readback.get("ok") is not True
                or readback.get("channel") != channel
                or not isinstance(readback.get("messages"), list)):
            return {**unconfirmed, "reason": "readback_unconfirmed"}
        matches = [message for message in readback["messages"]
                   if isinstance(message, dict) and message.get("ts") == ts
                   and message.get("text") == body and message.get("user") == sender
                   and message.get("thread_ts") in (None, "", ts)]
        if len(matches) != 1:
            return {**unconfirmed, "reason": "readback_mismatch"}
        return {"ok": True, "state": "confirmed", "confirmed": True,
                "channel": channel, "slack_ts": ts, "ticket_count": len(ids)}
    except Exception:
        return {**unconfirmed, "reason": "transport_unconfirmed"}
