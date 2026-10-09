"""
outbox.py — the Wrangler outbound role. The ONLY code in this package that posts to Slack.

Blake (spec item 8): "Wrangler owns all outbound. The adapter never calls chat.postMessage
itself. It writes the row, Wrangler posts." Ground truth 2026-09-03: no Wrangler process
reads support_messages today, so this in-process loop IS the outbound role for now. Its
whole interface is `run_once(bus, post, ...)`; when a real Wrangler service exists, it takes
over by pointing at the same rows and this loop is disabled by config. Nothing else in the
package would change.

It reads rows in delivery_status='ready' and, for each, re-checks EVERY gate at post time
(not only at write time -- a flag can be flipped between the two):

  0. KNOWN KIND, KNOWN IDENTITY. A row whose kind is not one of ours, or that carries no
     identity stamp, is suppressed, not posted (V-M7: fail closed).
  1. FIRST CONTACT. The parent ticket must carry at least one inbound human row, or the row
     is suppressed. The bot never speaks first, structurally.
  2. VERIFICATION. A substantive reply (kind=answer) posts only if the parent ticket has
     verification_after populated. Missing -> suppressed, loudly. Never "should be fixed."
  3. NO RE-ENTRY. A conversational body carrying the ops-fix trigger prefix is suppressed;
     the bot's own reply must never be readable as a command by the ops-fix worker (RT-m2).
  4. FRESHNESS. A conversational row that sat in 'ready' longer than STALE_AFTER_SECONDS
     (outbox down, channel unset) is suppressed: a stale "checking that now" hours later is
     worse than silence (V-m2). Internal rows never go stale; a human still needs them.
  5. TRUST LADDER. A conversational reply to a client posts only if the identity's
     client-reply flag is armed; to staff only if the staff flag is armed. Otherwise the row
     is moved to 'held' AND a tap notice is written (V-M8), so nothing sits held unseen. A
     row Blake has explicitly released (release_held stamped released_by) skips this recheck
     once -- his tap IS the approval the trust ladder exists to collect (N2).
  6. CLAIM. The row is moved ready/held->posting with a conditional PATCH only one caller
     can win, immediately before post() (N4). This is what makes two consumers of the same
     row safe.
  7. DESTINATION. Internal kinds (escalation, hold_notice) go to the fixer channel and
     fixer_request goes to the ops-fix intake channel the existing worker watches. They
     never enter the person's thread. Conversational kinds go to the ticket's own channel:
     top-level in a DM or group DM (people do not thread there), in-thread for a mention or
     a channel thread.

Every SUPPRESSION writes an escalation row, so a human sees what the bot declined to say
(V-M5). A hold notice posts with a Block Kit button (action id slack_convo_release, value =
the held row id) so the tap actually exists (V-M2 / RT-m5), rendered across as many sections
as the full body needs (RA-M2: Blake must review exactly the text that will post on release,
never a truncated prefix of it). When an answer posts, its ticket is marked resolved -- the
ticket closes when the person has the answer, not before (V-M4).

A post failure marks the row 'failed' and moves on; one bad row never stalls the queue.
Client resolution sends additionally require portal 0624 durable admission for the
exact Railway deployment/build. Missing, paused or uncertain admission holds the
row. The message stores its ticket/source/version/destination/sender binding before
acquisition; uncertain attempts never return to the automatic send queue.

HARDENING (2026-09-03 re-audit wave 2):
  N2  Blake's tap on a held row used to be swallowed silently: release_held flipped it to
      'ready', but gate 5 re-read the SAME flag that had held it, found it still off (that
      IS why it was held), and held it again -- writing a fresh card each time, forever. A
      released row now skips the trust-ladder recheck once.
  N4  Read-then-post-then-mark had no claim step: two consumers (a redeploy overlap, a
      second Wrangler pointed at the same rows per D2) could post the same row twice, and a
      mark_message failure after a successful post left the row in 'ready' to be reposted
      forever. Every row is now claimed (ready/held -> posting, a conditional PATCH only one
      caller can win) immediately before post(); any row still in 'posting' at the START of
      a run_once call is orphaned from a crashed prior attempt (claim -> post -> mark is
      synchronous within one call, so nothing legitimate is ever mid-flight across calls)
      and is swept back to 'ready'. FIXER client rows instead reconcile against
      Slack or stay held because a successful send may have preceded the crash.
  RA-M2  hold_notice_blocks used to show only the first 2900 characters while release posted
      the full row -- an injected tail could sit invisible to the reviewer. Now it renders
      as many sections as the body needs; what Blake reviews is what posts.
  RA-m5  fixer_request always used the shared ops-fix worker channel (ground truth: only one
      worker exists, and it only trusts Echo's bot_id -- a cross-identity ruling for Blake,
      D10). escalation / hold_notice now honour the identity's OWN fixer_channel_env when
      set, instead of always the global default, so a second identity's holds do not land
      in Echo's channel.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import math
import json
import os
import re
import time
import uuid

from . import adapter as _a
from .. import config
from .. import support_sender_fence as _fence

# Surfaces where a reply goes TOP LEVEL rather than in a thread: DMs and group DMs (people do
# not thread there), and a portal-bridge ticket, whose Slack home is the group DM this system
# opened for it.
TOP_LEVEL_SURFACES = frozenset({"im", "mpim", "portal_ticket_bridge"})

STALE_AFTER_SECONDS = 6 * 3600
RELEASE_ACTION_ID = "slack_convo_release"
RESOLVE_ACTION_ID = "slack_convo_resolve"
_REENTRY_PREFIX = "OPS-FIX REQUEST"
_BLOCK_TEXT_CHARS = 2900
FIXER_ALERT_RETRY_DELAY = timedelta(minutes=5)

# D48 (Blake, 2026-09-05): a ticket the person submitted IN THE PORTAL has a second, real
# delivery surface that is not Slack -- the /my/support/[ticketId] thread they submitted it
# from. Migration 0310 already decides what a client may read there: an outbound row that is
# delivery_status='posted' and not an internal kind. So for these tickets "post" means
# "release the row into the thread they are already looking at", and gate 7 below marks it
# posted instead of failing it for having no Slack channel. Restricted to the two sources a
# client actually submits through the portal UI (a website_intake / engage_tenant_event
# ticket has no portal reader), and to tickets carrying a client_id, without which 0310's
# own predicate can never match the reader to the row.
PORTAL_THREAD_SOURCES = frozenset({"portal_form", "website_tab"})

RESOLVED_NOTICE = (
    "Update from the LASSO team: this one is handled. If that is not what you needed, "
    "reply here and we will pick it back up.")


SUPPORT_SEND_LANE = "support-resolution-send"
SUPPORT_SEND_ADMISSION_KEY = "support_resolution_send_admission"
_SUPPORT_BIND_FIELDS = (
    "id", "source", "product", "client_id", "request_version", "bot_identity",
    "slack_user_id", "slack_channel_id", "slack_thread_ts", "status",
    "classification", "escalated", "hold_tier", "verification_after",
)


class SupportResolutionAdmissionError(RuntimeError):
    """A resolution attempt must stay held; its durable effect is never replayed."""


def _verify_support_post_sender(post, identity):
    """Require auth.test proof from the exact transport captured by this POST."""
    expected = identity.bot_user_id()
    verifier = getattr(post, "verify_sender", None)
    if not isinstance(expected, str) or not expected.strip() or not callable(verifier):
        raise SupportResolutionAdmissionError("Support reply authenticated sender verifier unavailable")
    try:
        proof = verifier()
    except Exception as exc:
        raise SupportResolutionAdmissionError("Support reply sender authentication unavailable") from exc
    if (not isinstance(proof, dict) or proof.get("ok") is not True
            or proof.get("user_id") != expected):
        raise SupportResolutionAdmissionError("Support reply authenticated sender differs from ticket identity")
    return expected


def _support_resolution_row(ticket, att, kind, body):
    recipient = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
    return (recipient not in ("staff", "coach") and (
        att.get("resolve_notice") is True
        or kind == _a.KIND_ANSWER and not _a.promises_human_follow_up(body)))


def _support_send_rpc(bus, name, body):
    """Frozen portal 0624 contract. Never retry an uncertain acquisition."""
    response = bus._client().post(
        bus._rest("rpc/" + name), data=json.dumps(body),
        headers=bus._headers(), timeout=30)
    if response.status_code >= 400:
        raise SupportResolutionAdmissionError("Support send admission RPC unavailable")
    result = response.json()
    if not isinstance(result, dict):
        raise SupportResolutionAdmissionError("Support send admission receipt malformed")
    return result


def _support_send_cas(bus, row, *, meta=None, slack_ts=None, state=None, after_post=False):
    """Keep the exact row, body, request version and attachment claimant snapshot."""
    allowed_states = {"posting", "held"} if after_post else {"posting"}
    if row.get("delivery_status") not in allowed_states or not callable(getattr(bus, "_patch", None)):
        raise SupportResolutionAdmissionError("Support send requires durable message CAS")
    att = row.get("attachments")
    fields = {}
    if meta is not None:
        fields["attachments"] = {**(att or {}), **meta}
    if slack_ts is not None:
        fields["slack_ts"] = slack_ts
    if state is not None:
        fields["delivery_status"] = state
    match = {
        "id": "eq." + row["id"], "ticket_id": "eq." + row["ticket_id"],
        "delivery_status": "eq." + row["delivery_status"], "body": "eq." + row["body"],
        "delivery_request_version": ("is.null" if row.get("delivery_request_version") is None
                                     else "eq." + str(row["delivery_request_version"])),
        "slack_ts": "is.null" if not row.get("slack_ts") else "eq." + row["slack_ts"],
        "attachments": "is.null" if att is None else "eq." + json.dumps(
            att, sort_keys=True, separators=(",", ":")),
    }
    changed = bus._patch("support_messages", match, fields)
    if (not isinstance(changed, dict)
            or any(changed.get(k) != v for k, v in fields.items())
            or any(changed.get(k) != row.get(k) for k in (
                "id", "ticket_id", "body", "direction", "delivery_request_version"))):
        raise SupportResolutionAdmissionError("Support send message CAS unconfirmed")
    return changed


def _support_send_after_post(bus, frozen, ts, *, proof=None, meta=None):
    """Retain the correlated timestamp/proof across a known stale quarantine.

    This only records an effect that already happened; it never authorizes a
    POST or changes held back to posting. Snapshot retries cannot repeat Slack.
    """
    original_att = frozen.get("attachments") or {}
    quarantine_meta = {"held_why", "fixer_slack_delivery_uncertain",
                       "support_resolution_send_held", "fixer_reconcile_next_at"}
    updates = dict(meta or {})
    if proof is not None:
        updates["support_resolution_send_readback"] = proof
    for _ in range(3):
        current = bus.message(frozen["id"])
        att = (current or {}).get("attachments") or {}
        if (not current or current.get("delivery_status") not in {"posting", "held"}
                or any(current.get(k) != frozen.get(k) for k in (
                    "id", "ticket_id", "body", "direction", "delivery_request_version"))
                or any(att.get(k) != value for k, value in original_att.items()
                       if k not in quarantine_meta)
                or current.get("slack_ts") not in (None, "", ts)
                or current.get("delivery_status") == "held" and not (
                    att.get("fixer_slack_delivery_uncertain") is True
                    or att.get("support_resolution_send_held") is True)):
            raise SupportResolutionAdmissionError("Support send timestamp/proof claimant changed")
        if (current.get("slack_ts") == ts
                and all(att.get(k) == v for k, v in updates.items())):
            return current
        try:
            return _support_send_cas(bus, current, slack_ts=ts,
                meta=updates or None,
                after_post=True)
        except Exception:
            # The CAS may have committed before its ACK was lost, or a known
            # quarantine may have won it. Read the exact snapshot on the next
            # pass; no acquisition or Slack effect is retried here.
            continue
    raise SupportResolutionAdmissionError("Support send timestamp/proof persistence unconfirmed")


def _support_send_completed(row):
    """A proof-only recovery cannot clear an unknown durable send admission."""
    att = (row or {}).get("attachments") or {}
    lease = att.get(SUPPORT_SEND_ADMISSION_KEY)
    if lease is None:
        return True  # existing rows predate this durable admission contract
    receipt = att.get("support_resolution_send_completion")
    binding = lease.get("binding") if isinstance(lease, dict) else None
    return (isinstance(lease, dict) and isinstance(receipt, dict)
            and isinstance(binding, dict) and isinstance(binding.get("ticket"), dict)
            and binding.get("message_id") == row.get("id")
            and binding["ticket"].get("id") == row.get("ticket_id")
            and binding["ticket"].get("request_version") == row.get("delivery_request_version")
            and isinstance(lease.get("invocation_id"), str) and bool(lease["invocation_id"])
            and type(lease.get("generation")) is int and lease["generation"] >= 0
            and receipt.get("recorded") is True
            and receipt.get("lane") == lease.get("lane") == SUPPORT_SEND_LANE
            and receipt.get("invocation_id") == lease.get("invocation_id")
            and type(receipt.get("generation")) is int
            and receipt["generation"] == lease.get("generation"))


def _ordinary_support_reply_boundary(bus, post, row, ticket, identity, body,
                                     channel, thread_ts, kind, att,
                                     member_check, require_member):
    """Authentication is a network window. Recheck the complete send owner after it."""
    before = bus.message(row["id"])
    version = ticket.get("request_version")
    expected_thread = None if att.get("surface") in TOP_LEVEL_SURFACES else ticket.get("slack_thread_ts")
    stable_meta = ("identity", "kind", "released_by", "recipient_kind", "request_key",
                   "request_version", "resolve_notice", "surface")
    if (not before or before.get("delivery_status") != "posting"
            or before.get("ticket_id") != ticket.get("id") or before.get("body") != body
            or type(version) is not int or version < 0
            or type(before.get("delivery_request_version")) is not int
            or before["delivery_request_version"] != version or before.get("slack_ts")
            or ticket.get("slack_channel_id") != channel or expected_thread != thread_ts
            or not ticket.get("source") or ticket.get("bot_identity") != identity.name
            or any((before.get("attachments") or {}).get(k) != att.get(k) for k in stable_meta)):
        raise SupportResolutionAdmissionError("Ordinary support reply request/route/claimant unconfirmed")
    sender = _verify_support_post_sender(post, identity)
    if require_member and not (callable(member_check) and channel.startswith(("C", "G"))
                               and member_check(channel, config.APPROVER_SLACK_ID)):
        raise SupportResolutionAdmissionError("Ordinary support reply Blake membership changed")
    if not att.get("released_by"):
        recipient = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
        if not _recipient_armed(identity, recipient):
            raise SupportResolutionAdmissionError("Ordinary support reply safety flag revoked")
        if kind == _a.KIND_ANSWER:
            verdict = _a.auto_answer_verdict(ticket.get("raw_text") or "", body,
                                             grounded_by_fixer=bool(att.get("fixer")))
            if (not config.slack_convo_auto_answer_armed(identity.name)
                    or att.get("auto_answer_forbidden") or verdict.held):
                raise SupportResolutionAdmissionError("Ordinary support answer safety gate revoked")
        if att.get(_client_dm_lane_meta_key()):
            from ..client_dm_support import arming as _cdm_arm
            if _cdm_arm.preflight(identity.name).mode != _cdm_arm.MODE_LIVE:
                raise SupportResolutionAdmissionError("Ordinary client DM support lane revoked")
    from . import replay
    if not replay.dispatch_allowed(bus, before, identity.name):
        raise SupportResolutionAdmissionError("Ordinary support reply replay authority changed")
    # All network/safety/replay checks precede these exact final snapshots.
    if (bus.ticket(ticket["id"]) != ticket or bus.message(row["id"]) != before
            or identity.bot_user_id() != sender):
        raise SupportResolutionAdmissionError("Ordinary support reply changed during authentication")


def _hold_support_send(bus, row, reason, log):
    """No generic retry can release an admission with an uncertain ACK or send."""
    try:
        current = bus.message(row["id"])
        if (current or {}).get("delivery_status") != "posting":
            return False
        if (current.get("attachments") or {}).get("slack_replay_id"):
            return _hold_uncertain_replay(bus, current, reason, log)
        if _fixer_client_row(current):
            return _quarantine_fixer(bus, current["id"], reason, log)
        _support_send_cas(bus, current, state="held", meta={
            "held_why": reason, "support_resolution_send_held": True})
        return True
    except Exception as exc:  # claim/intent remain durable if quarantine fails
        log(f"[slack-convo/outbox] support admission hold unconfirmed "
            f"row={row.get('id')}: {type(exc).__name__}")
        return False


def _post_support_resolution(bus, post, row, ticket, identity, body, channel,
                             thread_ts, readback, *, kind, att,
                             member_check=None, require_member=False):
    """Admit each client resolution against the DB pause, then prove its exact send.

    0624 has no ticket parameters. Its immutable invocation id is derived from
    the binding stored by message CAS BEFORE acquisition. No local/preview
    identity fallback and no feature flag can bypass this send boundary.
    """
    if not _support_resolution_row(ticket, att, kind, body):
        recipient = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
        # A stale staff stamp cannot waive sender authentication in a client's
        # conversation. Only a ticket AND recipient known to be staff bypass it.
        if (recipient not in ("staff", "coach")
                or ticket.get("identity_kind") not in ("staff", "coach")):
            _ordinary_support_reply_boundary(
                bus, post, row, ticket, identity, body, channel, thread_ts, kind, att,
                member_check, require_member)
        return post(channel, body, thread_ts=thread_ts, blocks=None)
    lease = None
    try:
        deployment = os.environ.get("RAILWAY_DEPLOYMENT_ID", "").strip()
        build = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
        sender = identity.bot_user_id()
        version = ticket.get("request_version")
        if (not deployment or not build or not sender or not callable(readback)
                or type(version) is not int or version < 0
                or not isinstance(ticket.get("source"), str) or not ticket["source"].strip()
                or ticket.get("bot_identity") != identity.name
                or ticket.get("slack_channel_id") != channel):
            raise SupportResolutionAdmissionError("Support send exact deployment/request/sender/readback unavailable")
        uuid.UUID(ticket["id"])
        uuid.UUID(row["id"])
        _verify_support_post_sender(post, identity)
        current = bus.message(row["id"])
        if (not current or current.get("delivery_status") != "posting"
                or current.get("body") != body or current.get("ticket_id") != ticket["id"]
                or type(current.get("delivery_request_version")) is not int
                or current["delivery_request_version"] != version
                or current.get("slack_ts")
                or (current.get("attachments") or {}).get(SUPPORT_SEND_ADMISSION_KEY)
                or any((current.get("attachments") or {}).get(k) != att.get(k)
                       for k in ("identity", "kind", "released_by", "recipient_kind",
                                 "request_key", "request_version", "resolve_notice"))):
            raise SupportResolutionAdmissionError("Support send request/body/version claimant changed or already attempted")
        status = _support_send_rpc(bus, "support_admission_status_lane", {"p_lane": SUPPORT_SEND_LANE})
        generation, unresolved = status.get("generation"), status.get("unresolved")
        if (status.get("lane") != SUPPORT_SEND_LANE
                or type(generation) is not int or not 0 <= generation <= 2**53 - 1
                or type(unresolved) is not int or unresolved < 0
                or type(status.get("paused")) is not bool
                or type(status.get("drained")) is not bool
                or status["drained"] != (status["paused"] and unresolved == 0)
                or not isinstance(status.get("operation_id"), str) or not status["operation_id"]
                or status["paused"]):
            raise SupportResolutionAdmissionError("Support send admission paused or status unconfirmed")
        binding = {
            "ticket": {k: ticket.get(k) for k in _SUPPORT_BIND_FIELDS},
            "message_id": row["id"], "sender_identity": identity.name,
            "sender": sender, "channel": channel, "thread_ts": thread_ts,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "request_key": att.get("request_key"),
        }
        invocation = str(uuid.uuid5(uuid.NAMESPACE_URL, "lasso/support-resolution-send/v1/" +
            json.dumps(binding, sort_keys=True, separators=(",", ":"))))
        lease = {"lane": SUPPORT_SEND_LANE, "invocation_id": invocation,
                 "generation": generation, "deployment": deployment, "build": build,
                 "binding": binding, "not_before": datetime.now(timezone.utc).isoformat()}
        current = _support_send_cas(bus, current, meta={SUPPORT_SEND_ADMISSION_KEY: lease})
        receipt = _support_send_rpc(bus, "support_admission_acquire_lane", {
            "p_lane": SUPPORT_SEND_LANE, "p_expected_generation": generation,
            "p_invocation_id": invocation, "p_deployment": deployment, "p_build": build})
        if (receipt.get("admitted") is not True or receipt.get("lane") != SUPPORT_SEND_LANE
                or receipt.get("invocation_id") != invocation
                or type(receipt.get("generation")) is not int or receipt["generation"] != generation):
            raise SupportResolutionAdmissionError("Support send durable admission denied or receipt mismatch")
        if _verify_support_post_sender(post, identity) != sender:
            raise SupportResolutionAdmissionError("Support sender changed during send admission")
        if require_member and not (channel.startswith(("C", "G")) and callable(member_check)
                                   and member_check(channel, config.APPROVER_SLACK_ID)):
            raise SupportResolutionAdmissionError("Blake membership changed during support send admission")
        if not att.get("released_by") and att.get(_client_dm_lane_meta_key()):
            from ..client_dm_support import arming as _cdm_arm
            if _cdm_arm.preflight(identity.name).mode != _cdm_arm.MODE_LIVE:
                raise SupportResolutionAdmissionError("Client DM support lane revoked during send admission")
        # Replay authority includes inbound history and verification snapshots
        # that can change without advancing the ticket's request version.
        from . import replay
        if not replay.dispatch_allowed(bus, current, identity.name):
            raise SupportResolutionAdmissionError("Support send replay authority changed after admission")
        latest_ticket = bus.ticket(ticket["id"])
        latest = bus.message(row["id"])
        if (not latest_ticket or any(latest_ticket.get(k) != ticket.get(k) for k in _SUPPORT_BIND_FIELDS)
                or latest != current or identity.bot_user_id() != sender
                or (not att.get("released_by") and (
                    not _recipient_armed(identity, att.get("recipient_kind") or ticket.get("identity_kind") or "client")
                    or kind == _a.KIND_ANSWER and not config.slack_convo_auto_answer_armed(identity.name)))):
            raise SupportResolutionAdmissionError("Support send ticket, sender or release changed after admission")
        ts = post(channel, body, thread_ts=thread_ts, blocks=None)
        if not isinstance(ts, str) or not ts:
            raise SupportResolutionAdmissionError("Support Slack timestamp unconfirmed")
        current = _support_send_after_post(bus, current, ts)
        intent = {"channel": channel, "thread_ts": thread_ts, "body": body,
                  "sender": sender, "not_before": lease["not_before"],
                  "request_version": version, "request_key": att.get("request_key")}
        proof, reason = _readback_fixer_message(readback, intent, ts=ts)
        if not proof:
            raise SupportResolutionAdmissionError(reason)
        current = _support_send_after_post(bus, current, ts, proof=proof)
        finished = _support_send_rpc(bus, "support_admission_finish_lane", {
            "p_lane": SUPPORT_SEND_LANE, "p_invocation_id": invocation,
            "p_generation": generation, "p_outcome": "completed"})
        if (finished.get("recorded") is not True or finished.get("lane") != SUPPORT_SEND_LANE
                or finished.get("invocation_id") != invocation):
            raise SupportResolutionAdmissionError("Support send completion ACK unconfirmed")
        _support_send_after_post(bus, current, ts, meta={
            "support_resolution_send_completion": {**finished, "generation": generation}})
        return ts
    except Exception as exc:
        # An acquired-but-unconfirmed receipt or POST cannot be completed by
        # assumption. 0624 unknown remains unresolved; failed finish ACK leaves
        # a running invocation unresolved too. Never retry acquisition or POST.
        if lease:
            try:
                _support_send_rpc(bus, "support_admission_finish_lane", {
                    "p_lane": SUPPORT_SEND_LANE, "p_invocation_id": lease["invocation_id"],
                    "p_generation": lease["generation"], "p_outcome": "unknown"})
            except Exception:
                pass
        if isinstance(exc, SupportResolutionAdmissionError):
            raise
        raise SupportResolutionAdmissionError(
            f"Support send admission/effect unconfirmed: {type(exc).__name__}") from exc


def portal_deliverable(ticket):
    """True when the portal support thread is a real delivery surface for this ticket."""
    t = ticket or {}
    return (str(t.get("source") or "") in PORTAL_THREAD_SOURCES
            and bool(str(t.get("client_id") or "").strip()))


# ---- support-surface sender policy (Blake, 2026-10-08) ---------------------------
# Support replies in the Echo channel come from Echo, website support from Wrangler,
# and the overall Ops portal from Scout -- never Ranger (or any other bot) on those
# surfaces. The production outbox loop already requires ticket.bot_identity to match
# the row's identity, but a LEGACY 'ready' row can carry a stale-but-consistent
# identity (e.g. a Ranger row on a portal ticket whose bot_identity is also ranger)
# and would still post. The authoritative ticket product/source therefore names the
# sender at actual dispatch time:
#   source echosupport / portal_social -> echo
#   product echo -> echo, product websites -> wrangler, product portal -> scout
# Products outside this map keep their established identity behavior untouched. A
# ticket whose signals disagree, or a portal-deliverable thread no signal names, is
# ambiguous: hold it safely instead of guessing a sender.
_SUPPORT_SOURCE_IDENTITY = {"echosupport": "echo", "portal_social": "echo"}
_SUPPORT_PRODUCT_IDENTITY = {"echo": "echo", "websites": "wrangler", "portal": "scout"}


def _support_surface_sender(ticket):
    """Return (required_identity, ambiguous) for a support-surface ticket.

    Exactly one named sender -> (name, False). Conflicting product/source signals,
    or a portal-deliverable thread with no recognized signal, -> (None, True):
    ambiguous, fail closed. Unrelated products/sources -> (None, False): the
    established ticket/row identity is preserved.
    """
    t = ticket or {}
    source = str(t.get("source") or "").strip().lower()
    product = str(t.get("product") or "").strip().lower()
    # Fixer's identityFor treats portal_social as an explicit Echo source even
    # when a stale/misleading product field says "portal". Preserve that source
    # precedence; other contradictory product/source signals remain ambiguous.
    if source in ("portal_social", "echosupport"):
        return "echo", False
    signals = set()
    if source in _SUPPORT_SOURCE_IDENTITY:
        signals.add(_SUPPORT_SOURCE_IDENTITY[source])
    if product in _SUPPORT_PRODUCT_IDENTITY:
        signals.add(_SUPPORT_PRODUCT_IDENTITY[product])
    if len(signals) == 1:
        return next(iter(signals)), False
    if len(signals) > 1:
        return None, True
    if portal_deliverable(t):
        return None, True
    return None, False


def _hold_support_surface_sender(bus, row, ticket, identity, required, ambiguous,
                                 log, summary):
    """Fail-closed hold for a support-surface sender violation; never silent."""
    att = row.get("attachments") or {}
    if ambiguous:
        why = ("support-surface sender policy: ticket product/source does not name "
               "exactly one sender; refusing to guess")
    else:
        why = (f"support-surface sender policy: ticket product/source maps to "
               f"{required!r}, not {identity.name!r}")
    log(f"[slack-convo/outbox] HELD row {row['id']}: {why}")
    bus.mark_message(row["id"], "held", meta_update={
        "held_why": why,
        "support_surface_sender_hold": True,
        "support_surface_required_identity": required or "",
        "support_surface_sender_ambiguous": bool(ambiguous),
    })
    summary["held"] += 1
    _a.write_hold_notice(
        bus, ident_name=identity.name, tid=ticket["id"],
        recipient_kind=att.get("recipient_kind") or ticket.get("identity_kind") or "client",
        user=ticket.get("slack_user_id") or "?",
        account_key=None, kind=att.get("kind") or "", body=row.get("body") or "",
        held_message_id=row["id"], surface=att.get("surface") or "", why=why)


def _question_without_code_fix(ticket, att=None):
    t, a = ticket or {}, att or {}
    return (str(t.get("classification") or "").lower() == "answerable_question"
            and not t.get("fix_pr_url") and not a.get("pr_url")
            and a.get("triage") != "code_fix")


def _direct_answerable_question(ticket, body=""):
    """A grounded question answer is not a code-fix completion.

    It may skip deployment proof, but FIXER-authored rows are separately bound to
    the current durable requester hash below. Keep this predicate independent of
    authorship so ordinary Echo answers retain their existing behavior.
    """
    t = ticket or {}
    direct_question = (_question_without_code_fix(t)
                       and t.get("status") == "verification"
                       and t.get("escalated") is not True
                       and not t.get("hold_tier")
                       and not (t.get("verification_after") or {}).get("hold"))
    promised_work = (_a.answer_commits_to_action(str(body or ""))
                     or _a.promises_human_follow_up(str(body or "")))
    return direct_question and not promised_work


def _fixer_grounded_question_answer(ticket, att, kind, body=""):
    """True only for the narrow FIXER question-answer deployment exemption."""
    return (kind == _a.KIND_ANSWER
            and bool((att or {}).get("fixer"))
            and _question_without_code_fix(ticket, att))


def _customer_fix_reply(ticket, att, body=""):
    """Identify customer handoffs even after escalation clears classification.

    The FIXER poll requires classification NULL, so classification alone cannot
    protect held portal incidents. A direct grounded QUESTION keeps its explicit
    classification and remains eligible for the normal answer gates.
    """
    recipient = (att.get("recipient_kind") or ticket.get("identity_kind") or "client")
    if recipient in ("staff", "coach"):
        return False
    # A grounded answer remains an answer when Scout/FIXER authored it.  The
    # normal answer gates below still re-run the hard-line verdict, arming, and
    # conversation checks. Treating every `fixer: true` row as a code-fix
    # completion sent it into the deployment gate, where it could never pass
    # because an answer has no PR or release evidence.
    classification = str(ticket.get("classification") or "").lower()
    portal_handoff = (ticket.get("product") == "echo"
                      and portal_deliverable(ticket)
                      and (ticket.get("escalated") is True
                           or bool(ticket.get("hold_tier"))
                           or bool((ticket.get("verification_after") or {}).get("hold"))))
    # A held/escalated portal handoff still overrides the no-code question exception.
    if _question_without_code_fix(ticket, att) and not portal_handoff:
        return False
    return (classification == "code_fix" or bool(ticket.get("fix_pr_url"))
            or bool(att.get("pr_url")) or att.get("triage") == "code_fix"
            or bool(att.get("fixer")) or portal_handoff)


def _swap_siblings_verified(result, row_id):
    """Sibling evidence parity for a swap that legitimately moved sibling rows.

    fixer_ops._run_swap_media proves the postcondition by reading back the target row
    AND every sibling from the calendar store before it stamps postcondition_verified,
    so a nonempty siblings_swapped list is a legitimate, verified outcome -- requiring
    it to be empty made such a swap unclosable. Accept it only with the same evidence
    the producer's readback verified: exact unique nonempty ids, sibling_results whose
    entry ids match siblings_swapped one for one (no missing, no extra), each entry
    carrying the readback fields (display url, media kind, and the video pairing rule
    the target row itself is held to), and nothing left behind. The target row's own
    id is never a sibling -- its appearance in either list is a malformed or
    adversarial payload. Any deviation fails closed exactly like an empty-swap record
    that lost its readback."""
    swapped = result.get("siblings_swapped")
    if not isinstance(swapped, list) or result.get("siblings_left") != []:
        return False
    ids = []
    for sid in swapped:
        if not isinstance(sid, str) or not sid.strip() or sid == row_id:
            return False
        ids.append(sid)
    if len(set(ids)) != len(ids):
        return False  # duplicate sibling ids are never verified evidence
    evidence = result.get("sibling_results")
    if evidence is None:
        evidence = []  # legacy empty-swap records predate the key; [] must still pass
    if not isinstance(evidence, list):
        return False
    entries = []
    for entry in evidence:
        if not isinstance(entry, dict):
            return False
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid.strip() or sid == row_id:
            return False
        entries.append(entry)
    if sorted(entry["id"] for entry in entries) != sorted(ids):
        return False
    for entry in entries:
        url = entry.get("image_public_url")
        kind = entry.get("media_kind")
        if not (isinstance(url, str) and url.strip()):
            return False
        if kind == "image":
            if entry.get("video_url") is not None:
                return False
        elif kind == "video":
            video = entry.get("video_url")
            if not (isinstance(video, str) and video.strip()):
                return False
        else:
            return False
    return True


def _count_field(value):
    """An integer count field; a bool is never a count."""
    return isinstance(value, int) and not isinstance(value, bool)


def _release_denied_assets_verified(operation):
    """release_denied_assets closes a customer ticket only on the sweep's
    independently re-proven outcome.

    fixer_ops._run_release_denied_assets stamps postcondition_verified True ONLY
    when its pre-sweep derivation, the sweep's own claim, a post-sweep ledger
    re-read and per-asset counter re-reads all agree -- and a zero-rollback sweep
    verifies nothing by construction, so a genuine verified outcome always carries
    a nonzero rolled_back and at least one probed date. evidence_note exists ONLY
    on an unverified outcome: its presence, a bare verified flag without the
    measured counts, or prose offered as proof all fail closed."""
    result = operation.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("postcondition_verified") is not True or "evidence_note" in result:
        return False
    rolled = result.get("rolled_back")
    checked = result.get("checked")
    captured = result.get("captured_at")
    return (_count_field(rolled) and rolled >= 1
            and _count_field(checked) and checked >= 1
            and isinstance(captured, str) and bool(captured.strip()))


def _restage_month_verified(operation, ticket):
    """restage_month closes a customer ticket only on the background job's
    TERMINAL record, bound to this ticket.

    The 202 job-start payload proves nothing, and a running, timed_out or failed
    job never verifies. fixer_ops.run_restage_month stamps the terminal job
    result's postcondition_verified True ONLY when an independent before/after
    calendar snapshot comparison confirms the build's claimed row writes; the
    builder's own postcondition_verified is stripped on that path, so its
    presence here means the payload did not come from the truthful producer. A
    no-op build verifies nothing, so verified implies upserted >= 1. The job
    record must name this ticket and one gym, its inner result the same gym, and
    both must match the operation's recorded gym_key when one was stamped."""
    job = operation.get("result")
    if not isinstance(job, dict):
        return False
    if (job.get("action") != "restage_month" or job.get("status") != "done"
            or job.get("error") is not None):
        return False
    job_id = job.get("id")
    finished = job.get("finished_at")
    gym = job.get("gym_key")
    if not (isinstance(job_id, str) and job_id.strip()
            and isinstance(finished, str) and finished.strip()
            and isinstance(gym, str) and gym.strip()):
        return False
    if job.get("ticket_id") != (ticket or {}).get("id"):
        return False
    op_gym = operation.get("gym_key")
    if isinstance(op_gym, str) and op_gym.strip() and op_gym != gym:
        return False
    result = job.get("result")
    if not isinstance(result, dict):
        return False
    if (result.get("postcondition_verified") is not True
            or "evidence_note" in result
            or result.get("gym_key") != gym):
        return False
    captured = result.get("captured_at")
    build = result.get("build")
    if not isinstance(build, dict) or build.get("ok") is not True:
        return False
    if "postcondition_verified" in build:
        return False  # the truthful path strips the builder's self-certification
    upserted = build.get("upserted")
    return (_count_field(upserted) and upserted >= 1
            and isinstance(captured, str) and bool(captured.strip()))


# The stamped business_postcondition is a POINTER, never the proof: only its check_id
# and params are used, to re-run the independent observation at dispatch time through
# fixer_business_evidence. Any stamped verified / symptom_resolved / evidence prose is
# untrusted and ignored for the decision.
BUSINESS_EVIDENCE_MAX_AGE_SECONDS = STALE_AFTER_SECONDS
_BUSINESS_EVIDENCE_FUTURE_SKEW_SECONDS = 300
_RELEASE_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _business_evidence_deps(bus, check_id=None):
    """The bounded, read-only reader fixer_business_evidence.observe() requires,
    adapted from the support bus's own bounded PostgREST-style read (the same one
    _person_for_card uses). The registered checks pass their own explicit limit in
    the params and the evidence module rejects over-limit or partial payloads, so
    the adapter stays thin; a bus without that read yields no reader, and no reader
    fails closed at the caller."""
    get = getattr(bus, "_get", None)
    if not callable(get):
        return None

    def read(table, params):
        return get(table, params)

    deps = {"read": read}
    if check_id == "media_swap_completed":
        # The Slack outbox runs on the Echo worker with its durable SQLite
        # volume. Keyed FIXER swaps intended for automatic closure must run on
        # that same worker. A receipt written on intake-web's separate volume
        # will be absent here and the observer will fail closed.
        from .. import fixer_ops_receipts as receipts
        store = receipts.default_store()
        deps["receipt_read"] = lambda key, echo_key: receipts.get_receipt(
            store, key, echo_key)
    return deps


def _business_postcondition_observed(bus, ticket, business, merged_sha, now=None):
    """The dispatch-time authoritative read behind the business_postcondition branch.

    The stamped record supplies ONLY the check_id and params to re-run; the verdict
    comes from a fresh fixer_business_evidence.observe() through a bounded read-only
    reader, bound to this ticket's tenant, the CURRENT recomputed request key, and
    the exact release SHA. Any missing pointer field, unavailable reader, stale or
    future observation, non-VERIFIED outcome, or binding mismatch fails closed --
    an inline stamped dict can never close the ticket by itself."""
    check_id = business.get("check_id")
    params = business.get("params")
    if not (isinstance(check_id, str) and check_id.strip()
            and isinstance(params, dict)):
        return False  # a pointer without a check id and params points at nothing
    gym_key = (ticket or {}).get("client_id")
    if not (isinstance(gym_key, str) and gym_key.strip()):
        return False
    if bus is None:
        return False  # no reader can be constructed: fail closed, never skip
    try:
        current_key = _current_fixer_request_key(bus, ticket)
    except Exception:  # noqa: BLE001 - an unreadable thread cannot prove the request
        return False
    if not current_key:
        return False
    deps = _business_evidence_deps(bus, check_id)
    if deps is None:
        return False
    try:
        from .. import fixer_business_evidence as _fbe
        observed = _fbe.observe(check_id, gym_key=gym_key, request_key=current_key,
                                merged_sha=merged_sha, params=params, deps=deps,
                                ticket_id=str(ticket.get("id") or ""), now=now)
    except Exception:  # noqa: BLE001 - an observer fault is not evidence
        return False
    if not isinstance(observed, dict):
        return False
    captured = _parse_ts(observed.get("captured_at"))
    ref = now or datetime.now(timezone.utc)
    age = (ref - captured).total_seconds() if captured is not None else None
    return (observed.get("schema_version") == 1
            and observed.get("outcome") == _fbe.VERIFIED
            and observed.get("verified") is True
            and observed.get("symptom_resolved") is True
            and age is not None
            and -_BUSINESS_EVIDENCE_FUTURE_SKEW_SECONDS <= age
            <= BUSINESS_EVIDENCE_MAX_AGE_SECONDS
            and _fbe.binding_matches(observed, gym_key=gym_key,
                                     request_key=current_key, merged_sha=merged_sha))


def _ops_media_swap_observed(bus, ticket, row_id, now=None):
    """Re-read a direct keyed swap's receipt and business state at dispatch.

    The before-state supplies only the reservation pointer. A successful ops
    payload, or a copied business verdict, cannot certify itself. The observer
    checks the durable receipt on this worker and freshly reads the exact
    tenant's calendar row and approved asset.
    """
    before = (ticket.get("verification_before") or {}).get("fixer") or {}
    operation = before.get("ops_action") or {}
    reservation_key = operation.get("reservation_key") if isinstance(operation, dict) else None
    if (not isinstance(reservation_key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", reservation_key)
            or not isinstance(row_id, str) or not row_id
            or bus is None):
        return False
    gym_key = ticket.get("client_id")
    if not isinstance(gym_key, str) or not gym_key:
        return False
    try:
        current_key = _current_fixer_request_key(bus, ticket)
        deps = _business_evidence_deps(bus, "media_swap_completed")
        if not current_key or deps is None:
            return False
        from .. import fixer_business_evidence as _fbe
        observed = _fbe.observe_ops_media_swap(
            gym_key=gym_key, request_key=current_key,
            params={"reservation_key": reservation_key, "row_id": row_id},
            deps=deps, ticket_id=str(ticket.get("id") or ""), now=now)
    except Exception:  # noqa: BLE001 - receipt/read failure is not proof
        return False
    if not isinstance(observed, dict):
        return False
    captured = _parse_ts(observed.get("captured_at"))
    ref = now or datetime.now(timezone.utc)
    age = (ref - captured).total_seconds() if captured is not None else None
    return (observed.get("schema_version") == 1
            and observed.get("source") == _fbe.SOURCE
            and observed.get("check_id") == "media_swap_completed"
            and observed.get("gym_key") == gym_key
            and observed.get("request_key") == current_key
            and observed.get("merged_sha") == ""
            and observed.get("outcome") == _fbe.VERIFIED
            and observed.get("verified") is True
            and observed.get("symptom_resolved") is True
            and age is not None
            and -_BUSINESS_EVIDENCE_FUTURE_SKEW_SECONDS <= age
            <= BUSINESS_EVIDENCE_MAX_AGE_SECONDS)


def _verified_fix_notice(ticket, att, kind, *, bus=None, now=None):
    """Only the current fix's resolve notice may tell a customer it is handled.

    Most ops_action branches retain their existing payload gates. A swap_media
    resolution also requires the same durable business observation as the code
    fix branch; its handler result alone cannot prove the photo changed."""
    verification = ticket.get("verification_after") or {}
    release = verification.get("fixer") or {}
    deployment = release.get("deployment_check") or {}
    if kind != _a.KIND_STATUS or att.get("resolve_notice") is not True:
        return False
    operation = release.get("ops_action") or {}
    if att.get("ops_action") in {"resend_connect_link", "reset_recreate_budget",
                                 "requeue_failed_row", "swap_media",
                                 "release_denied_assets", "restage_month"}:
        common = (ticket.get("status") == "verification"
                and release.get("postcondition_verified") is True
                and operation.get("identityVerified") is True
                and operation.get("ok") is True
                and operation.get("action") == att.get("ops_action")
                and operation.get("tenantVerified") is True
                and bool(ticket.get("client_id"))
                and operation.get("tenantId") == ticket.get("client_id")
                and bool(release.get("request_key")))
        if not common:
            return False
        action = att.get("ops_action")
        if action == "release_denied_assets":
            return _release_denied_assets_verified(operation)
        if action == "restage_month":
            return _restage_month_verified(operation, ticket)
        if action != "swap_media":
            return True
        # A direct ops completion cannot borrow the code-fix lane or overrule a
        # fresh human hold. A queued notice is rechecked against the current
        # ticket at dispatch, after the FIXER produced its ops result.
        if (ticket.get("classification") not in {"ops_fix", "action_request"}
                or ticket.get("fix_pr_url")
                or ticket.get("escalated") is not False
                or ticket.get("hold_tier") is not None):
            return False
        args = operation.get("args") or {}
        result = operation.get("result") or {}
        if not isinstance(args, dict) or not isinstance(result, dict):
            return False
        row_id = args.get("row_id")
        media_url = result.get("image_public_url")
        media_kind = result.get("media_kind")
        payload_ok = (isinstance(row_id, str) and bool(row_id.strip())
                and result.get("ok") is True
                and result.get("action") == "swap-media"
                and result.get("postcondition_verified") is True
                and result.get("draft_id") == row_id
                and _swap_siblings_verified(result, row_id)
                and isinstance(media_url, str) and bool(media_url.strip())
                and (media_kind == "image" and result.get("video_url") is None
                     or media_kind == "video" and
                     isinstance(result.get("video_url"), str) and
                     bool(result["video_url"].strip())))
        if not payload_ok:
            return False
        return _ops_media_swap_observed(bus, ticket, row_id, now=now)
    # A healthy deployment proves the code is live, not that this owner's symptom
    # is gone. The stamped business_postcondition is only a pointer to the check that
    # must be re-run at dispatch time; its stamped verdict and prose are untrusted.
    business = release.get("business_postcondition") or {}
    if not isinstance(business, dict):
        return False
    merged_sha = release.get("merged_sha")
    if not (kind == _a.KIND_STATUS and att.get("resolve_notice") is True
            and ticket.get("status") == "merged"
            and verification.get("exit_code") == 0
            and verification.get("incomplete") is not True
            and bool(ticket.get("fix_pr_url"))
            and att.get("pr_url") == ticket.get("fix_pr_url")
            and isinstance(merged_sha, str) and _RELEASE_SHA.fullmatch(merged_sha)
            and deployment.get("verified") is True
            and deployment.get("sha") == merged_sha
            and bool(release.get("request_key"))):
        return False
    return _business_postcondition_observed(bus, ticket, business, merged_sha, now=now)


def _current_fixer_request_key(bus, ticket):
    """Recompute Scout's requestKey from all requester inbound rows, failing closed.

    A truncated message read cannot prove the current request. Require fewer
    than the normal PostgREST response cap and compare a separate inbound read.
    """
    rows = bus.messages(ticket["id"], limit=1000)
    if not isinstance(rows, list) or len(rows) >= 1000:
        return None
    inbound = [m for m in rows if m.get("direction") == "inbound"]
    if len(inbound) != bus.inbound_count(ticket["id"]):
        return None
    requester = []
    for m in inbound:
        att = m.get("attachments") or {}
        author = m.get("author_type")
        client = author == "client" or not author
        operator_mention = (author in ("staff", "blake")
                            and att.get("surface") == "mention"
                            and att.get("identity_reason") == "operator list")
        if client or operator_mention:
            requester.append([m.get("id"), m.get("created_at"), m.get("body")])
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    requester.sort(key=encode)
    payload = requester or [ticket.get("id"), ticket.get("created_at"),
                            ticket.get("raw_text")]
    return hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()


_FIXER_REQUEST_IDENTITY_FIELDS = (
    "product", "client_id", "bot_identity", "slack_user_id",
    "slack_channel_id", "slack_thread_ts",
)
_FIXER_REQUEST_STABLE_FIELDS = _FIXER_REQUEST_IDENTITY_FIELDS[:-2]


def _fresh_fixer_request(bus, ticket, att, *, body="", require_direct_answer=False):
    """Return the fresh ticket only when its requester identity matches the row.

    This is intentionally independent of deployment evidence. Grounded FIXER
    answers have no PR to prove, but they still must answer the request that is
    current at the moment of delivery and resolution. The monotonic database
    version closes the hash/read-to-resolve race; the hash still binds the exact
    requester transcript. Tenant, bot and user may not drift between fresh reads.
    An unbound portal route may become bound once; an already-bound destination
    may never change. Missing or unreadable durable identity fails closed.
    """
    stamped = (att or {}).get("request_key")
    version = (att or {}).get("request_version")
    if (not isinstance(stamped, str) or not stamped
            or not isinstance(version, int) or isinstance(version, bool)
            or version < 0 or not isinstance(ticket, dict)):
        return None
    try:
        fresh = bus.ticket(ticket["id"])
        current = _current_fixer_request_key(bus, fresh) if fresh else None
    except Exception:  # noqa: BLE001 - unreadable identity is never current proof
        return None
    old_channel = ticket.get("slack_channel_id")
    old_thread = ticket.get("slack_thread_ts")
    new_channel = fresh.get("slack_channel_id") if isinstance(fresh, dict) else None
    new_thread = fresh.get("slack_thread_ts") if isinstance(fresh, dict) else None
    route_matches = (new_channel == old_channel and new_thread == old_thread)
    route_newly_bound = (
        old_channel is None and old_thread is None
        and isinstance(new_channel, str) and new_channel.startswith(("C", "G"))
    )
    if (not isinstance(fresh, dict)
            or fresh.get("request_version") != version
            or any(fresh.get(field) != ticket.get(field)
                   for field in _FIXER_REQUEST_STABLE_FIELDS)
            or not (route_matches or route_newly_bound)
            or not current or stamped != current):
        return None
    if require_direct_answer and not _direct_answerable_question(fresh, body):
        return None
    return fresh


def _fresh_portal_progress(bus, ticket, row, identity):
    """A progress row may only become visible in its original portal thread.

    Recheck the durable request even after a human releases a held row. The
    marker grants no exception to the normal recipient arming/release gate.
    """
    att = row.get("attachments") or {}
    if (row.get("direction") != "outbound"
            or row.get("author_type") != identity.name
            or row.get("ticket_id") != (ticket or {}).get("id")
            or att.get("kind") != _a.KIND_STATUS
            or att.get("identity") != identity.name
            or att.get("recipient_kind") != "client"
            or att.get("surface") != "portal_ticket_bridge"
            or att.get("fixer") is not True
            or "resolve_notice" in att):
        return None
    if (not isinstance(ticket, dict)
            or ticket.get("source") != "website_tab"
            or ticket.get("classification") != "code_fix"
            or ticket.get("status") not in ("hold", "merged")
            or ticket.get("bot_identity") != identity.name
            or not isinstance(ticket.get("client_id"), str)
            or not ticket["client_id"].strip()
            or ticket.get("slack_channel_id")
            or ticket.get("slack_thread_ts")):
        return None
    fresh = _fresh_fixer_request(bus, ticket, att)
    if (not fresh or fresh.get("source") != "website_tab"
            or fresh.get("classification") != "code_fix"
            or fresh.get("status") not in ("hold", "merged")
            or fresh.get("bot_identity") != identity.name
            or not portal_deliverable(fresh)
            or fresh.get("slack_channel_id")
            or fresh.get("slack_thread_ts")
            or not _portal_progress_original_inbound(bus, fresh)):
        return None
    return fresh


def _portal_progress_original_inbound(bus, ticket):
    """Require one durable portal-origin client row for the ticket's original text."""
    reporter = ticket.get("reporter")
    original_text = ticket.get("raw_text")
    if (not isinstance(reporter, str) or not reporter.strip()
            or not isinstance(original_text, str) or not original_text.strip()):
        return False
    try:
        rows = bus.messages(ticket["id"], limit=1000)
        if not isinstance(rows, list) or len(rows) >= 1000:
            return False
        inbound = [m for m in rows if m.get("direction") == "inbound"]
        if len(inbound) != bus.inbound_count(ticket["id"]):
            return False
    except Exception:  # noqa: BLE001 - missing or partial provenance is not proof
        return False
    matches = [m for m in inbound
               if m.get("ticket_id") == ticket["id"]
               and m.get("author_type") == "client"
               and m.get("author_id") == reporter
               and m.get("body") == original_text
               and (m.get("attachments") or {}).get("surface") == "portal_ticket_bridge"]
    return len(matches) == 1


def _channel_for(kind, identity):
    if kind == _a.KIND_FIXER_REQUEST:
        return config.ops_fix_channel_id()
    return identity.fixer_channel() or config.fixer_channel_id()


def _person_for_card(bus, ticket, identity):
    """m2: a post-time hold card names the person and gym in words, like a draft-time one.

    The outbox has no identity_gate result to hand (it runs long after resolution), so this
    reconstructs the same line from the ticket row itself -- reporter, gym, identity kind --
    rather than falling back to a bare Slack id, which is exactly the unreadable card D53
    was written to get rid of."""
    t = ticket or {}
    uid = str(t.get("slack_user_id") or "") or "?"
    kind = str(t.get("identity_kind") or "unknown")
    email = str(t.get("reporter") or "").strip()
    gym = str(t.get("client_id") or "").strip()
    label = ""
    try:
        if gym:
            rows = bus._get("gyms", {"id": f"eq.{gym}", "select": "name", "limit": "1"})
            label = str((rows or [{}])[0].get("name") or "").strip()
    except Exception:  # noqa: BLE001 - a name lookup never blocks a card
        label = ""
    bits = [f"{kind} {_a._slack_escape(uid)}"]
    if email:
        bits.append(_a._slack_escape(email))
    bits.append(f"gym {_a._slack_escape(label or gym or 'not resolved')}")
    return ", ".join(bits)


def _client_dm_lane_meta_key():
    """The attachments key client_dm_support stamps on every row it writes
    (lane.py's LANE_META), imported by name rather than duplicated as a magic
    string here so the two can never drift apart."""
    from ..client_dm_support.lane import LANE_META
    return LANE_META


def _recipient_armed(identity, recipient_kind):
    if recipient_kind in ("staff", "coach"):
        return config.slack_convo_staff_reply_armed(identity.name)
    return config.slack_convo_client_reply_armed(identity.name)


def _parse_ts(value):
    try:
        s = str(value or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _age_seconds(row, now=None):
    att = row.get("attachments") or {}
    born = (_parse_ts(att.get("claimed_at")) or _parse_ts(att.get("released_at"))
           or _parse_ts(row.get("created_at")))
    if born is None:
        return 0
    return ((now or datetime.now(timezone.utc)) - born).total_seconds()


def hold_notice_blocks(row):
    """Block Kit for a hold notice: the FULL text across as many sections as it needs (RA-M2
    -- what Blake reviews before tapping must be everything that will post, never a
    truncated prefix), plus ONE button whose value is the held row id. listener_wiring
    routes that action id to release_held, operator-gated."""
    body = row.get("body") or ""
    att = row.get("attachments") or {}
    mid = att.get("held_message_id") or ""
    chunks = [body[i:i + _BLOCK_TEXT_CHARS] for i in range(0, len(body), _BLOCK_TEXT_CHARS)] or [""]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
    if mid:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RELEASE_ACTION_ID, "value": str(mid),
            "text": {"type": "plain_text", "text": "Release"}, "style": "primary"}]})
    return blocks


def escalation_blocks(row, ticket):
    """Block Kit for an escalation card: the full text, plus ONE button that closes the loop
    back to the person who wrote in (D48).

    An escalation used to be the end of the line for the submitter. The card reached #fixer,
    Blake dealt with it, and nothing ever went back -- no acknowledgement, no "this is
    handled". The button is the missing half: listener_wiring routes it (operator-gated) to
    resolve_and_notify below, which writes the person a status row and closes the ticket.

    Rendered only when there IS somewhere to send that notice (a portal thread or an already
    opened group DM) and the ticket is not already closed; a button that could only no-op is
    worse than no button."""
    body = row.get("body") or ""
    att = row.get("attachments") or {}
    informational_only = (
        att.get("surface") == "held_client_ticket_reconcile"
        and att.get("contract") == "held-client-ticket-reconcile-v1"
    )
    tid = str((ticket or {}).get("id") or "")
    chunks = [body[i:i + _BLOCK_TEXT_CHARS] for i in range(0, len(body), _BLOCK_TEXT_CHARS)] or [""]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
    reachable = portal_deliverable(ticket) or bool((ticket or {}).get("slack_channel_id"))
    if (not informational_only and tid and reachable
            and (ticket or {}).get("status") != "resolved"):
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RESOLVE_ACTION_ID, "value": tid,
            "text": {"type": "plain_text", "text": "Resolved, tell them"}}]})
    return blocks


CLAIM_TIMEOUT_SECONDS = 90
FIXER_DELIVERY_PROTOCOL = "intent-readback-v1"


class UncertainFixerDelivery(RuntimeError):
    """Slack may have accepted a FIXER client message; automatic resend is unsafe."""


def _fixer_client_row(row):
    att = (row or {}).get("attachments") or {}
    return (att.get("fixer_slack_delivery_intent") is not None
            or att.get("fixer") is True
            and att.get("portal_progress_status") is not True
            and att.get("recipient_kind", "client") not in ("staff", "coach"))


# A pipe is mrkdwn's target/label delimiter, never part of an auto-link
# alternative: wrapping a URL containing it would change the link's meaning.
_SLACK_URL = re.compile(r"https?://[^\s<>|]+", re.IGNORECASE)
_SLACK_EXPLICIT_LINK = re.compile(r"<(?:https?://|mailto:|[@!#])[^>\n]+>", re.IGNORECASE)
_SLACK_ESCAPED_MARKUP = re.compile(
    r"&lt;(?:https?://|mailto:|[@!#])(?:(?!&gt;)[^\n])*&gt;", re.IGNORECASE)
_SLACK_ENTITIES = ("&amp;", "&lt;", "&gt;")
_SLACK_RESERVED = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _slack_entity_escape(value):
    """Apply Slack's reserved-character storage escaping exactly once."""
    escaped = []
    index = 0
    while index < len(value):
        entity = next((item for item in _SLACK_ENTITIES
                       if value.startswith(item, index)), None)
        if entity:
            escaped.append(entity)
            index += len(entity)
            continue
        char = value[index]
        escaped.append(_SLACK_RESERVED.get(char, char))
        index += 1
    return "".join(escaped)


def _slack_readback_text_matches(intended, observed):
    """Match only documented Slack mrkdwn storage forms of the sent bytes.

    This is deliberately one-way: never HTML-unescape or strip link labels
    from the observed message. A different URL, label, or body must not become
    delivery proof merely because it renders similarly in the Slack client.
    """
    if not isinstance(intended, str) or not isinstance(observed, str):
        return False
    if intended == observed:
        return True
    # Slack does not auto-format text in inline/fenced code. Parsing every
    # mrkdwn code boundary is error-prone, so any backtick makes a normalized
    # (non-exact) readback unverified.
    if "`" in intended or "`" in observed:
        return False
    if len(intended) > 40000 or len(observed) > 80000:
        return False
    positions = {0}
    index = 0
    while index < len(intended):
        explicit = (_SLACK_EXPLICIT_LINK.match(intended, index)
                    or _SLACK_ESCAPED_MARKUP.match(intended, index))
        if explicit:
            token = explicit.group()
            if (_SLACK_ESCAPED_MARKUP.fullmatch(token)
                    or not token.lower().startswith(("<http://", "<https://", "<mailto:"))):
                alternatives = (token,)
            else:
                escaped = f"<{_slack_entity_escape(token[1:-1])}>"
                alternatives = tuple(dict.fromkeys((token, escaped)))
        else:
            url_match = (_SLACK_URL.match(intended, index)
                         if index == 0 or not (intended[index - 1].isalnum()
                                                or intended[index - 1] in "<|") else None)
            if url_match:
                # Keep terminal punctuation in the target. It may be part of
                # the URL; guessing Slack's boundary could accept a different
                # link target as delivery proof.
                token = url_match.group()
                if token:
                    escaped = _slack_entity_escape(token)
                    alternatives = tuple(dict.fromkeys((
                        token, escaped, f"<{token}>", f"<{escaped}>",
                        f"<{token}|{token}>", f"<{escaped}|{escaped}>",
                    )))
                else:
                    token = intended[index]
                    alternatives = (token,)
            else:
                entity = next((value for value in _SLACK_ENTITIES
                               if intended.startswith(value, index)), None)
                token = entity or intended[index]
                alternatives = ((token,) if entity or token not in _SLACK_RESERVED
                                else (token, _SLACK_RESERVED[token]))
        next_positions = {position + len(candidate)
                          for position in positions for candidate in alternatives
                          if observed.startswith(candidate, position)}
        # Ambiguous/pathological inputs remain unverified rather than making
        # a potentially expensive fuzzy comparison on a customer message.
        if not next_positions or len(next_positions) > 64:
            return False
        positions = next_positions
        index += len(token)
    return len(observed) in positions


def _readback_fixer_message(readback, intent, *, ts=None):
    """Require one Echo-authored message at the persisted API-response timestamp."""
    # Text, sender, destination and time cannot tie a Slack message to this
    # particular attempt. A different identical Echo post can satisfy all four.
    # Only a timestamp durably recorded from THIS post response is correlating
    # evidence. Without it, hold for manual reconciliation; never resolve.
    if not isinstance(ts, str) or not ts:
        return None, "Slack outcome has no persisted message timestamp; manual reconciliation required"
    if not callable(readback) or not isinstance(intent, dict):
        return None, "readback unavailable"
    channel = intent.get("channel")
    body = intent.get("body")
    sender = intent.get("sender")
    thread_ts = intent.get("thread_ts")
    # not_before is written only AFTER the first durable intent write returns.
    # An incomplete first write can be quarantined, but cannot search Slack.
    not_before = _parse_ts(intent.get("not_before"))
    if not (channel and isinstance(body, str) and body and sender and not_before):
        return None, "delivery intent incomplete"
    try:
        result = readback(channel, thread_ts=thread_ts, ts=ts, oldest=ts)
    except Exception as exc:  # noqa: BLE001
        return None, f"Slack readback failed: {type(exc).__name__}"
    if (not isinstance(result, dict) or result.get("ok") is not True
            or result.get("channel") != channel
            or not isinstance(result.get("messages"), list)):
        return None, f"Slack readback incomplete: {(result or {}).get('error', 'invalid') if isinstance(result, dict) else 'invalid'}"
    matches = []
    for message in result["messages"]:
        if not isinstance(message, dict):
            continue
        found_ts = str(message.get("ts") or "")
        # Slack ts values are Unix seconds, not ISO-8601 strings like the
        # intent's not_before value. Parse numerically and fail closed.
        try:
            observed_at = float(found_ts)
            intent_floor = not_before.timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        if (not found_ts or not math.isfinite(observed_at)
                or observed_at < intent_floor
                or (ts and found_ts != str(ts))
                or not _slack_readback_text_matches(body, message.get("text"))
                or message.get("user") != sender):
            continue
        observed_thread = message.get("thread_ts")
        if thread_ts:
            if observed_thread != thread_ts or found_ts == thread_ts:
                continue
        elif observed_thread not in (None, "", found_ts):
            continue
        matches.append(message)
    if len(matches) != 1:
        return None, ("no exact Echo message found" if not matches else
                      "multiple matching Echo messages; manual reconciliation required")
    found_ts = str(matches[0]["ts"])
    return {"delivery_readback_verified": True,
            "delivery_readback_at": datetime.now(timezone.utc).isoformat(),
            "delivery_readback_channel": channel,
            "delivery_readback_thread_ts": thread_ts,
            "delivery_readback_ts": found_ts,
            "delivery_readback_sender": sender,
            "delivery_readback_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "delivery_readback_request_key": intent.get("request_key"),
            "delivery_readback_request_version": intent.get("request_version")}, ""


def _finish_pending_route_notice(bus, row, proof, identity, log, summary):
    """Recover one already-sent first contact; never call Slack POST here."""
    att = row.get("attachments") or {}
    intent = att.get("fixer_slack_delivery_intent")
    ts = row.get("slack_ts")
    token = att.get("fixer_current_attempt_token")
    if (att.get("fixer_route_pending") is not True or not isinstance(intent, dict)
            or not token or not ts or proof.get("delivery_readback_ts") != ts
            or proof.get("delivery_readback_verified") is not True):
        return False
    try:
        if row.get("delivery_status") == "held":
            verified = bus.record_held_current_notice_readback(
                row["id"], proof, expected_intent=intent, expected_ts=ts)
        elif row.get("delivery_status") == "posting":
            verified = bus.transition_fixer_delivery(
                row["id"], "posting", slack_ts=ts, meta_update=proof,
                expected_intent=intent, expected_ts=ts)
        else:
            return False
        if not verified or (verified.get("attachments") or {}).get(
                "delivery_readback_verified") is not True:
            return False
        # An admitted 0624 send is closed only when its confirmed durable
        # completion receipt was persisted before this bind (the primary path
        # records it ahead of bind_current_notice_route). Absent, unknown or
        # mismatched receipts stay held -- never bind, promote or resolve on
        # uncertainty. Legacy proof-only rows predate the admission contract
        # and keep their existing proof-only recovery below.
        if not _support_send_completed(verified):
            log(f"[slack-convo/outbox] pending route notice lacks durable send "
                f"completion receipt row={row.get('id')}")
            return False
        current_ticket = bus.ticket(row["ticket_id"])
        if (not current_ticket
                or current_ticket.get("request_version") != row.get(
                    "delivery_request_version")
                or current_ticket.get("status") != att.get(
                    "delivery_expected_status")):
            # Preserve exact proof on the old row for audit. A new requester
            # cycle may never inherit this route or close from its notice.
            return False
        if not bus.bind_current_notice_route(
                row["ticket_id"], row["delivery_request_version"], row["id"],
                token, intent.get("channel"), ts):
            return False
        posted = bus.transition_fixer_delivery(
            row["id"], "posted", slack_ts=ts,
            expected_intent=intent, expected_ts=ts)
        if not posted or posted.get("delivery_status") != "posted":
            return False
        ticket = bus.ticket(row["ticket_id"])
        if ticket:
            resolved = bus.resolve_current_notice(
                ticket, row["id"], token, att.get("delivery_expected_status"))
            if isinstance(resolved, dict) and resolved.get("status") == "resolved":
                summary["resolved"] = int(summary.get("resolved") or 0) + 1
                _receipt(bus, ticket, posted, identity,
                         (posted.get("attachments") or {}).get("kind"),
                         posted.get("attachments") or {},
                         where=f"Slack {intent.get('channel')}", summary=summary)
                bus.finalize_fixer_delivery(row["id"],
                                            "resolved_after_verified_slack")
            elif ticket.get("request_version") != row.get("delivery_request_version"):
                bus.finalize_fixer_delivery(row["id"],
                                            "newer_request_preserved")
        return True
    except Exception as exc:  # noqa: BLE001 - exact readback can retry next sweep
        log(f"[slack-convo/outbox] pending route reconciliation failed "
            f"row={row.get('id')}: {type(exc).__name__}")
        return False


def _quarantine_fixer(bus, row_id, reason, log):
    try:
        held = bus.hold_uncertain_fixer_delivery(row_id, reason)
        if held and held.get("delivery_status") == "held":
            log(f"[slack-convo/outbox] FIXER delivery held row={row_id}: {reason}")
            return True
    except Exception as exc:  # noqa: BLE001 - stale sweep will retry quarantine
        log(f"[slack-convo/outbox] CRITICAL FIXER quarantine failed row={row_id}: "
            f"{type(exc).__name__}")
    return False


def _claim(bus, row, log):
    """Ready -> posting through a conditional PATCH only one caller can win (N4).

    Modern FIXER claims stamp their recovery protocol and timestamp in that same
    CAS. Legacy/non-FIXER callers retain the older best-effort timestamp write.
    """
    claimed_at = datetime.now(timezone.utc).isoformat()
    modern_claim = getattr(bus, "claim_fixer_message", None)
    try:
        if "slack_replay_id" in (row.get("attachments") or {}):
            from . import replay
            return replay.claim_delivery(bus, row)
        if _fixer_client_row(row) and callable(modern_claim):
            claimed = modern_claim(
                row["id"], row.get("attachments"), claimed_at,
                FIXER_DELIVERY_PROTOCOL)
        else:
            claimed = bus.claim_message(row["id"])
        if not claimed:
            return None
    except AttributeError:
        return True  # a bus without claim support (older fakes): best effort, no CAS
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] claim failed for row {row['id']}: {type(e).__name__}")
        return None
    if not (_fixer_client_row(row) and callable(modern_claim)):
        try:
            bus.mark_message(row["id"], "posting", meta_update={"claimed_at": claimed_at})
        except Exception:  # noqa: BLE001 - legacy/non-FIXER best-effort stamp
            pass
    if isinstance(claimed, dict):
        return claimed
    try:
        current = bus.message(row["id"])
    except Exception:  # noqa: BLE001 - compatibility adapters may not expose message()
        return True
    if _fixer_client_row(row) and callable(modern_claim):
        current_att = (current or {}).get("attachments") or {}
        if (not current or current.get("delivery_status") != "posting"
                or current_att.get("fixer_slack_delivery_protocol")
                != FIXER_DELIVERY_PROTOCOL
                or current_att.get("claimed_at") != claimed_at):
            return None
    return current or True


def _hold_uncertain_replay(bus, row, why, log):
    """A legacy replay send can have reached Slack. Never turn it back to ready."""
    current = bus.message(row["id"])
    if not current or current.get("delivery_status") != "posting":
        return False
    from . import replay
    held = replay.hold_delivery(bus, current, why)
    quarantined = bool(held and held.get("delivery_status") == "held")
    if quarantined:
        log(f"[slack-convo/outbox] replay delivery requires reconciliation "
            f"row={row['id']}: {why}")
    return quarantined


def _recover_stale_claims(bus, identity, log, now=None, readback=None, summary=None):
    """Reconcile rows orphaned in ``posting`` after CLAIM_TIMEOUT_SECONDS.

    A modern FIXER claim without an intent is provably pre-POST and can be CAS-requeued.
    A legacy unmarked FIXER claim has an unknowable send boundary and is quarantined.

    D26 (2026-09-03, MAJOR): this used to sweep EVERY 'posting' row unconditionally, with no
    staleness check. Under exactly the multi-consumer scenario it exists to protect against,
    a row genuinely mid-flight in process A (a slow Slack API call) would be un-claimed by
    process B's very next 5-second sweep and re-posted while A was still in flight -- a
    duplicate post, the opposite of the guarantee. The timeout is generous over realistic
    post() latency so a live post is never stolen out from under it."""
    try:
        stuck = bus.outbox("posting", limit=200, identity=identity.name)
    except TypeError:
        try:
            stuck = bus.outbox("posting", limit=200)
        except Exception:  # noqa: BLE001
            return 0
    except Exception:  # noqa: BLE001
        return 0
    n = 0

    def count(outcome):
        if isinstance(summary, dict):
            summary[outcome] = int(summary.get(outcome) or 0) + 1

    for row in stuck:
        if _age_seconds(row, now) < CLAIM_TIMEOUT_SECONDS:
            continue  # plausibly still in flight; do not steal it
        try:
            if (row.get("attachments") or {}).get(SUPPORT_SEND_ADMISSION_KEY):
                if _hold_support_send(bus, row, "Prior support resolution admission requires reconciliation", log):
                    n += 1
                    count("quarantined_held")
                continue
            if (row.get("attachments") or {}).get("slack_replay_id"):
                if _hold_uncertain_replay(
                        bus, row, "Replay claim expired; prior Slack outcome is unknown", log):
                    n += 1
                    count("quarantined_held")
                continue
            if _fixer_client_row(row):
                att = row.get("attachments") or {}
                intent = att.get("fixer_slack_delivery_intent")
                if not isinstance(intent, dict):
                    if att.get("fixer_current_attempt_token"):
                        reason = ("Designated FIXER notice claim expired before "
                                  "durable Slack intent; no customer message was sent")
                        suppressed = bus.suppress_unattempted_current_notice(
                            row["id"], reason)
                        if suppressed and suppressed.get("delivery_status") == "suppressed":
                            n += 1
                            count("suppressed")
                            try:
                                bus.ensure_suppressed_current_notice_alert(
                                    row["id"], identity.name)
                            except Exception as exc:  # noqa: BLE001
                                log(f"[slack-convo/outbox] FIXER pre-send alert failed "
                                    f"row={row['id']}: {type(exc).__name__}")
                        continue
                    if att.get("fixer_slack_delivery_protocol") == FIXER_DELIVERY_PROTOCOL:
                        requeue = getattr(bus, "requeue_unattempted_fixer_delivery", None)
                        if callable(requeue):
                            recovered_at = (now or datetime.now(timezone.utc)).isoformat()
                            reset = requeue(
                                row["id"], FIXER_DELIVERY_PROTOCOL, recovered_at)
                            if reset and reset.get("delivery_status") == "ready":
                                n += 1
                                count("requeued_ready")
                            continue
                    quarantined = _quarantine_fixer(
                        bus, row["id"], "legacy FIXER posting has no durable Slack intent; "
                        "check the client conversation before any resend", log)
                    n += int(quarantined)
                    if quarantined:
                        count("quarantined_held")
                    continue
                proof, reason = _readback_fixer_message(
                    readback, intent, ts=row.get("slack_ts") or None)
                if not proof:
                    quarantined = _quarantine_fixer(bus, row["id"], reason, log)
                    n += int(quarantined)
                    if quarantined:
                        count("quarantined_held")
                    continue
                if att.get("fixer_route_pending") is True:
                    if _finish_pending_route_notice(
                            bus, row, proof, identity, log,
                            summary if isinstance(summary, dict) else {"resolved": 0}):
                        n += 1
                        count("reconciled_posted")
                    else:
                        quarantined = _quarantine_fixer(
                            bus, row["id"],
                            "pending route bind awaits exact verified recovery", log)
                        n += int(quarantined)
                    continue
                posted = bus.transition_fixer_delivery(
                    row["id"], "posted", slack_ts=proof["delivery_readback_ts"],
                    meta_update=proof, expected_intent=intent,
                    expected_ts=proof["delivery_readback_ts"])
                if not posted:
                    current = bus.message(row["id"])
                    if ((current or {}).get("delivery_status") == "held"
                            and (current or {}).get("slack_ts") == proof[
                                "delivery_readback_ts"]):
                        if (current.get("attachments") or {}).get(
                                "fixer_current_attempt_token"):
                            staged = bus.record_held_fixer_readback(
                                row["id"], proof, expected_intent=intent,
                                expected_ts=proof["delivery_readback_ts"])
                            if not staged:
                                continue
                            fresh_ticket = bus.ticket(row["ticket_id"])
                            if (not fresh_ticket
                                    or fresh_ticket.get("request_version") != row.get(
                                        "delivery_request_version")
                                    or fresh_ticket.get("status") != att.get(
                                        "delivery_expected_status")):
                                continue
                        posted = bus.reconcile_held_fixer_delivery(
                            row["id"], proof, expected_intent=intent,
                            expected_ts=proof["delivery_readback_ts"])
                if not posted or posted.get("delivery_status") != "posted":
                    _quarantine_fixer(bus, row["id"],
                                      "Slack readback succeeded but database commit failed", log)
                    continue
                n += 1
                count("reconciled_posted")
                ticket = bus.ticket(row["ticket_id"])
                if ticket:
                    _finalize_fixer_post(bus, ticket, posted, identity, log,
                                         summary if isinstance(summary, dict)
                                         else {"resolved": 0})
                continue
            if (row.get("attachments") or {}).get("outreach") is True:
                # The direct outreach path posts to Slack before its final DB mark.
                # A crashed/failed mark may mean the person already got the DM.
                # Never return that uncertain row to the automatic send queue.
                quarantine = getattr(bus, "hold_uncertain_outreach", None)
                if not callable(quarantine):
                    log(f"[slack-convo/outbox] CRITICAL uncertain outreach row "
                        f"{row['id']} has no safe quarantine capability")
                    continue
                observed = quarantine(row["id"])
                if observed and observed.get("delivery_status") == "held":
                    n += 1
                    count("quarantined_held")
                continue
            bus.mark_message(row["id"], "ready", meta_update={"reclaimed_stale_posting": True})
            n += 1
            count("requeued_ready")
        except Exception:  # noqa: BLE001
            pass
    return n


def _report_suppressed_current_notices(bus, identity, log):
    """Retry missing staff alerts from terminal unsent notices, 20 rows per sweep."""
    reader = getattr(bus, "suppressed_unattempted_current_notices", None)
    if not callable(reader):
        return
    cursors = getattr(bus, "_fixer_suppressed_alert_cursors", None)
    if not isinstance(cursors, dict):
        cursors = {}
        setattr(bus, "_fixer_suppressed_alert_cursors", cursors)
    after = cursors.get(identity.name)
    try:
        rows = reader(identity.name, limit=20, after=after)
        if not rows and after:
            cursors.pop(identity.name, None)
            rows = reader(identity.name, limit=20, after=None)
    except Exception as exc:  # noqa: BLE001 - keep cursor and retry bounded read
        log(f"[slack-convo/outbox] suppressed notice alert scan failed: "
            f"{type(exc).__name__}")
        return
    for row in rows:
        cursors[identity.name] = {"created_at": row.get("created_at"), "id": row.get("id")}
        try:
            bus.ensure_suppressed_current_notice_alert(row["id"], identity.name)
        except Exception as exc:  # noqa: BLE001 - unchanged source is durable retry record
            log(f"[slack-convo/outbox] suppressed notice staff alert failed "
                f"row={row['id']}: {type(exc).__name__}")
    if len(rows) < 20:
        cursors.pop(identity.name, None)


def _report_uncertain_outreach(bus, identity, log):
    """Persist a staff card for each held outreach whose Slack outcome is uncertain."""
    try:
        rows = bus.outbox("held", limit=200, identity=identity.name)
    except Exception as exc:  # noqa: BLE001 - independent scans retry next run
        log(f"[slack-convo/outbox] uncertain outreach held scan failed: "
            f"{type(exc).__name__}")
        rows = []
    try:
        pending_route = _pending_fixer_hold_page(
            bus, identity, "fixer_route_uncertain",
            scan="pending_route_alert", limit=20)
    except Exception as exc:  # noqa: BLE001 - legacy outreach must still alert
        log(f"[slack-convo/outbox] uncertain outreach route scan failed: "
            f"{type(exc).__name__}")
        pending_route = []
    rows = list({row["id"]: row for row in [*rows, *pending_route]}.values())
    for row in rows:
        att = row.get("attachments") or {}
        route_uncertain = att.get("fixer_route_uncertain") is True
        if not (att.get("outreach_delivery_uncertain") or route_uncertain):
            continue
        if att.get("outreach_staff_alerted") and not route_uncertain:
            continue
        ticket_id = row.get("ticket_id")
        try:
            if not bus.uncertain_outreach_alert_exists(ticket_id, row["id"]):
                fresh = bus.ticket(ticket_id) or {}
                owner = fresh.get("bot_identity") or identity.name
                bus.record_outbound(
                    ticket_id=ticket_id, author_type="system",
                    body=(f"Outreach delivery needs staff reconciliation for ticket "
                          f"{ticket_id}, message {row['id']}. Slack may have delivered it. "
                          f"Check Slack and the ticket before any resend."),
                    delivery_status="ready", kind=_a.KIND_ESCALATION,
                    meta={"identity": owner, "outreach_uncertain_row_id": row["id"]})
            if not route_uncertain:
                # 0384's pending notice attachments cannot gain a housekeeping
                # marker; the exact alert-row lookup above deduplicates it.
                bus.mark_uncertain_outreach_alerted(row["id"])
        except Exception as e:  # noqa: BLE001 - keep held and retry staff alert
            log(f"[slack-convo/outbox] uncertain outreach staff alert failed "
                f"row={row['id']}: {type(e).__name__}")


def _pending_fixer_hold_page(bus, identity, marker, *, scan="default", limit=200):
    """Return the next bounded marker-specific page, wrapping after the tail.

    The cursor lives on the Bus instance used by the worker.  Rows that remain
    held are revisited after a complete pass, while a row behind any number of
    unrelated holds is never hidden by the generic outbox limit.
    """
    reader = getattr(bus, "pending_fixer_holds", None)
    if not callable(reader):
        # Compatibility for old test/adaptor buses. Production Bus implements
        # the marker-specific query above.
        return [row for row in bus.outbox("held", limit=limit, identity=identity.name)
                if (row.get("attachments") or {}).get(marker)]
    cursors = getattr(bus, "_fixer_hold_scan_cursors", None)
    if not isinstance(cursors, dict):
        cursors = {}
        setattr(bus, "_fixer_hold_scan_cursors", cursors)
    # Alerting and route recovery consume the same route-missing marker for
    # different work. Independent cursors prevent one consumer from repeatedly
    # advancing past pages the other has not examined.
    key = (identity.name, marker, scan)
    after = cursors.get(key)
    rows = reader(identity.name, marker, limit=limit, after=after)
    if not rows and after:
        cursors.pop(key, None)
        rows = reader(identity.name, marker, limit=limit, after=None)
    if rows:
        last = rows[-1]
        cursors[key] = {"created_at": last.get("created_at"), "id": last.get("id")}
        if len(rows) < limit:
            # We reached the tail; the next sweep starts a fresh pass.
            cursors.pop(key, None)
    return rows


def _report_uncertain_fixer(bus, identity, log, now=None):
    """Raise one internal card for a held client completion needing reconciliation."""
    try:
        rows = {
            row["id"]: row
            for marker in ("fixer_slack_delivery_uncertain",
                           "fixer_slack_route_missing")
            for row in _pending_fixer_hold_page(
                bus, identity, marker, scan="uncertainty_alert")
        }.values()
    except Exception:  # noqa: BLE001
        return
    for row in rows:
        att = row.get("attachments") or {}
        if (not (att.get("fixer_slack_delivery_uncertain")
                 or att.get("fixer_slack_route_missing"))
                or att.get("fixer_staff_alerted")):
            continue
        try:
            state = bus.uncertain_fixer_alert_status(row["id"])
            if state == "posted":
                bus.mark_uncertain_fixer_alerted(row["id"])
                continue
            if state == "pending":
                continue
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            retry_raw = att.get("fixer_alert_retry_after")
            retry_at = _parse_ts(retry_raw)
            next_at = (current + FIXER_ALERT_RETRY_DELAY).isoformat()
            if state == "failed":
                if retry_at is None:
                    bus.reserve_uncertain_fixer_alert_retry(row["id"], None, next_at)
                    continue
                if current < retry_at:
                    continue
                # Reserve the next retry window first. If this process crashes,
                # another sweep waits; only the CAS winner may rearm the alert.
                reserved = bus.reserve_uncertain_fixer_alert_retry(
                    row["id"], retry_raw, next_at)
                if not reserved:
                    continue
                rearmed = bus.rearm_uncertain_fixer_alert(row["id"])
                if not rearmed or rearmed.get("delivery_status") != "ready":
                    continue
                continue
            # Reserve initial creation too.  Two sweepers can both observe no
            # alert; only the held-row CAS winner may insert one.  A crash after
            # reservation but before insert is retried after the same bounded
            # deadline, while a successful insert is subsequently observed as
            # pending/posted/failed and never duplicated.
            if retry_at is not None and current < retry_at:
                continue
            reserved = bus.reserve_uncertain_fixer_alert_retry(
                row["id"], retry_raw if retry_at is not None else None, next_at)
            if not reserved:
                continue
            # The first notice is one row. A failed attempt is rearmed in place
            # above after a durable backoff; it never creates an alert storm.
            bus.record_outbound(
                ticket_id=row["ticket_id"], author_type="system",
                body=(f"FIXER client completion needs delivery reconciliation. "
                      f"Ticket {row['ticket_id']}, message {row['id']}. "
                      f"Reason: {att.get('held_why') or 'Slack outcome uncertain'}. "
                      "Check the exact client conversation before any resend; "
                      "this ticket has not been closed by this delivery."),
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": identity.name,
                      "fixer_uncertain_row_id": row["id"]})
        except Exception as exc:  # noqa: BLE001 - retry alert on next loop
            log(f"[slack-convo/outbox] FIXER delivery alert failed row={row['id']}: "
                f"{type(exc).__name__}")


def _recover_route_missing_fixer(bus, identity, member_check, log, now=None):
    """Requeue a never-attempted completion after its exact Slack route appears.

    Route absence is different from an uncertain Slack outcome: the row was
    claimed but no delivery intent was persisted and POST was never called.  We
    may therefore retry the same row, but only after re-reading the current
    request, deployment/business proof, release state, route and Blake's channel
    membership.  The message transition is a CAS and dispatch repeats all gates,
    so a concurrent ticket change can at worst leave the row ready, never send a
    stale completion.
    """
    try:
        rows = _pending_fixer_hold_page(
            bus, identity, "fixer_slack_route_missing", limit=5,
            scan="route_recovery")
    except Exception as exc:  # noqa: BLE001
        log(f"[slack-convo/outbox] FIXER route recovery scan failed: "
            f"{type(exc).__name__}")
        return 0
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    recovered = 0
    membership = {}
    for row in rows:
        att = row.get("attachments") or {}
        recipient = att.get("recipient_kind", "client")
        if (att.get("fixer_slack_route_missing") is not True
                or att.get("fixer_slack_delivery_intent") is not None
                or att.get("fixer_slack_delivery_uncertain")
                or row.get("slack_ts")
                or recipient in ("staff", "coach")):
            continue
        try:
            ticket = bus.ticket(row["ticket_id"])
            if (not ticket or ticket.get("bot_identity") != identity.name
                    or bus.inbound_count(ticket["id"]) < 1):
                continue
            kind = att.get("kind") or ""
            body = row.get("body") or ""
            customer_fix = _customer_fix_reply(ticket, att, body)
            grounded = _fixer_grounded_question_answer(ticket, att, kind, body)
            if not (customer_fix or grounded):
                continue
            if kind == _a.KIND_ANSWER and not ticket.get("verification_after"):
                continue
            if _REENTRY_PREFIX in body.upper():
                continue
            if not att.get("released_by"):
                if not _recipient_armed(identity, recipient):
                    continue
                if (kind == _a.KIND_ANSWER
                        and not config.slack_convo_auto_answer_armed(identity.name)):
                    continue
            fresh = _fresh_fixer_request(
                bus, ticket, att, body=body, require_direct_answer=grounded)
            if not fresh:
                continue
            channel = fresh.get("slack_channel_id")
            if not isinstance(channel, str) or not channel.startswith(("C", "G")):
                continue
            if customer_fix:
                release_key = (((fresh.get("verification_after") or {}).get("fixer")
                                or {}).get("request_key"))
                if (not _verified_fix_notice(fresh, att, kind, bus=bus, now=current)
                        or release_key != att.get("request_key")):
                    continue
                # Proof reads are mutation windows; bind the exact request and
                # route once more after them.
                fresh = _fresh_fixer_request(bus, fresh, att, body=body)
                if not fresh or fresh.get("slack_channel_id") != channel:
                    continue
            if channel not in membership:
                try:
                    membership[channel] = bool(member_check and member_check(
                        channel, config.APPROVER_SLACK_ID))
                except Exception:  # noqa: BLE001 - membership is mandatory
                    membership[channel] = False
            member = membership[channel]
            if not member:
                continue
            # One final request/route read immediately precedes the message CAS.
            latest = _fresh_fixer_request(
                bus, fresh, att, body=body, require_direct_answer=grounded)
            if (not latest or latest.get("slack_channel_id") != channel
                    or latest.get("slack_thread_ts") != fresh.get("slack_thread_ts")):
                continue
            requeued = bus.requeue_route_missing_fixer(
                row["id"], current.astimezone(timezone.utc).isoformat())
            if requeued and requeued.get("delivery_status") == "ready":
                recovered += 1
        except Exception as exc:  # noqa: BLE001 - held row remains fail closed
            log(f"[slack-convo/outbox] FIXER route recovery failed row={row.get('id')}: "
                f"{type(exc).__name__}")
    return recovered


def _recover_config_missing_fixer(bus, identity, readback, log, now=None):
    """Requeue never-attempted rows after required Slack proof config returns."""
    if not identity.bot_user_id() or not callable(readback):
        return 0
    try:
        rows = _pending_fixer_hold_page(
            bus, identity, "fixer_slack_config_missing", limit=5,
            scan="config_recovery")
    except Exception as exc:  # noqa: BLE001
        log(f"[slack-convo/outbox] FIXER config recovery scan failed: "
            f"{type(exc).__name__}")
        return 0
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    recovered = 0
    for row in rows:
        att = row.get("attachments") or {}
        if (att.get("fixer_slack_config_missing") is not True
                or att.get("fixer_slack_delivery_intent") is not None
                or att.get("fixer_slack_delivery_uncertain")
                or row.get("slack_ts")):
            continue
        try:
            requeued = bus.requeue_config_missing_fixer(
                row["id"], current.astimezone(timezone.utc).isoformat())
            if requeued and requeued.get("delivery_status") == "ready":
                recovered += 1
        except Exception as exc:  # noqa: BLE001 - held row remains safe
            log(f"[slack-convo/outbox] FIXER config recovery failed "
                f"row={row.get('id')}: {type(exc).__name__}")
    return recovered


def _finalize_fixer_post(bus, ticket, row, identity, log, summary):
    """Idempotently finish the ticket step after verified Slack delivery."""
    if not _support_send_completed(row):
        log(f"[slack-convo/outbox] FIXER resolution awaits durable send admission reconciliation "
            f"row={row.get('id')}")
        return
    att = row.get("attachments") or {}
    intent = att.get("fixer_slack_delivery_intent") or {}
    if (row.get("delivery_status") != "posted"
            or att.get("delivery_readback_verified") is not True
            or att.get("delivery_readback_ts") != row.get("slack_ts")
            or att.get("delivery_readback_channel") != intent.get("channel")
            or att.get("delivery_readback_thread_ts") != intent.get("thread_ts")
            or att.get("delivery_readback_sender") != intent.get("sender")
            or att.get("delivery_readback_body_sha256") != hashlib.sha256(
                str(intent.get("body") or "").encode()).hexdigest()
            or att.get("delivery_readback_request_key") != intent.get("request_key")
            or att.get("delivery_readback_request_version") != intent.get(
                "request_version")):
        log(f"[slack-convo/outbox] CRITICAL FIXER posted row lacks exact readback "
            f"row={row.get('id')}")
        return
    kind = att.get("kind")
    direct_notice = (att.get("outreach") is True
                     and att.get("fixer_current_attempt_token")
                     and ticket.get("product") == "echo"
                     and ticket.get("source") == "website_tab")
    if direct_notice:
        # The worker may have crashed after marking its one first-contact
        # message posted but before receiving the resolver response. The exact
        # persisted Slack proof above lets this sweep finish without resending.
        try:
            if ticket.get("request_version") == row.get("delivery_request_version"):
                resolved = bus.resolve_current_notice(
                    ticket, row["id"], att["fixer_current_attempt_token"],
                    att.get("delivery_expected_status"))
                if (isinstance(resolved, dict)
                        and resolved.get("status") == "resolved"
                        and ticket.get("status") != "resolved"):
                    summary["resolved"] += 1
            if kind in RECEIPT_KINDS:
                _receipt(bus, ticket, row, identity, kind, att,
                         where=f"Slack {intent.get('channel')}", summary=summary)
            current = bus.ticket(ticket["id"])
            if current and current.get("status") == "resolved":
                bus.finalize_fixer_delivery(row["id"],
                                            "resolved_after_verified_slack")
            elif current and current.get("request_version") != row.get(
                    "delivery_request_version"):
                bus.finalize_fixer_delivery(row["id"],
                                            "newer_request_preserved")
        except Exception as exc:  # noqa: BLE001 - next sweep retries exact row
            log(f"[slack-convo/outbox] direct current notice finalization pending "
                f"row={row['id']}: {type(exc).__name__}")
        return
    try:
        _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
    except Exception as exc:  # noqa: BLE001 - finalization sweep retries
        log(f"[slack-convo/outbox] FIXER ticket close pending row={row['id']}: "
            f"{type(exc).__name__}")
        return
    try:
        if kind in RECEIPT_KINDS:
            # Always cross the deterministic INSERT boundary.  A broad legacy
            # receipt_for match must never suppress creation of the exact,
            # ticket-bound receipt, and a duplicate INSERT validates the winner.
            _receipt(bus, ticket, row, identity, kind, att,
                     where=f"Slack {att['delivery_readback_channel']}", summary=summary)
        receipt_done = (kind not in RECEIPT_KINDS
                        or bus.fixer_receipt_exists(
                            row["id"], ticket["id"], _a.KIND_ESCALATION))
        current = bus.ticket(ticket["id"])
        if not receipt_done or not current:
            return
        if current.get("status") == "resolved":
            reason = "resolved_after_verified_slack"
        elif current.get("request_version") != att.get("request_version"):
            reason = "newer_request_preserved"
        else:
            return  # same request is still open; retry guarded resolver next loop
        bus.finalize_fixer_delivery(row["id"], reason)
    except Exception as exc:  # noqa: BLE001 - delivered row remains pending for next loop
        log(f"[slack-convo/outbox] FIXER finalization pending row={row['id']}: "
            f"{type(exc).__name__}")


def _reconcile_posted_fixer(bus, identity, log, summary):
    try:
        rows = bus.pending_fixer_finalization(identity.name, limit=100)
    except Exception as exc:  # noqa: BLE001
        log(f"[slack-convo/outbox] FIXER finalization scan failed: {type(exc).__name__}")
        return
    for row in rows:
        try:
            ticket = bus.ticket(row["ticket_id"])
            if ticket:
                _finalize_fixer_post(bus, ticket, row, identity, log, summary)
        except Exception as exc:  # noqa: BLE001
            log(f"[slack-convo/outbox] FIXER finalization failed row={row['id']}: "
                f"{type(exc).__name__}")


def _reserve_immutable_fixer_readback(bus, identity, row):
    """Throttle readback without changing an immutable pending or stale row."""
    now = time.monotonic()
    retries = getattr(bus, "_fixer_immutable_readback_retries", None)
    if not isinstance(retries, dict):
        retries = {}
        setattr(bus, "_fixer_immutable_readback_retries", retries)
    for key, deadline in list(retries.items()):
        if deadline <= now:
            retries.pop(key, None)
    key = (identity.name, row["id"])
    if key in retries:
        return False
    # Keep memory bounded even with an unusually large identity/row set.
    if len(retries) >= 1024:
        retries.pop(min(retries, key=retries.get))
    retries[key] = now + 60
    return True


def _reconcile_held_fixer(bus, identity, readback, log, summary):
    """Recover a late Slack success without ever sending the client row again."""
    page_limit = 200
    try:
        reader = bus.pending_held_fixer_delivery
        cursors = getattr(bus, "_fixer_held_reconcile_cursors", None)
        if not isinstance(cursors, dict):
            cursors = {}
            setattr(bus, "_fixer_held_reconcile_cursors", cursors)
        after = cursors.get(identity.name)
        try:
            rows = reader(identity.name, limit=page_limit, after=after)
        except TypeError:  # compatibility for bounded legacy/test adapters
            rows = reader(identity.name, limit=page_limit)
        if not rows and after:
            cursors.pop(identity.name, None)
            try:
                rows = reader(identity.name, limit=page_limit, after=None)
            except TypeError:
                rows = reader(identity.name, limit=page_limit)
    except Exception as exc:  # noqa: BLE001
        log(f"[slack-convo/outbox] FIXER held reconciliation scan failed: "
            f"{type(exc).__name__}")
        return
    now = datetime.now(timezone.utc)
    exhausted = True
    for row in rows:
        # Advance only through rows this sweep actually examined. In particular,
        # do not jump to the end of a full page before stopping after one Slack
        # readback; doing so skips every other due row on that page.
        cursors[identity.name] = {
            "created_at": row.get("created_at"), "id": row.get("id")}
        markers = row.get("attachments") or {}
        if not (markers.get("fixer_slack_delivery_uncertain") is True
                or markers.get("fixer_route_uncertain") is True):
            continue
        if not row.get("slack_ts"):
            # Text/time search cannot prove which attempt produced a message.
            continue
        retry_at = _parse_ts((row.get("attachments") or {}).get("fixer_reconcile_next_at"))
        if retry_at and retry_at > now:
            continue
        stale_cycle = False
        if markers.get("fixer_current_attempt_token"):
            current_ticket = bus.ticket(row["ticket_id"])
            if not current_ticket:
                continue
            stale_cycle = bool(
                current_ticket.get("request_version") != row.get(
                    "delivery_request_version")
                or current_ticket.get("status") != markers.get(
                    "delivery_expected_status"))
        if markers.get("fixer_route_pending") is True or stale_cycle:
            # The 0384 pending row is immutable except for exact proof and the
            # binder; stale rows can gain proof only. Reserve in memory before
            # readback because retry timestamps violate both SQL guards.
            if not _reserve_immutable_fixer_readback(bus, identity, row):
                continue
        else:
            try:
                reserved = bus.defer_held_fixer_reconcile(
                    row["id"], (now + timedelta(minutes=1)).isoformat())
                if not reserved or reserved.get("delivery_status") != "held":
                    continue
            except Exception as exc:  # noqa: BLE001
                log(f"[slack-convo/outbox] FIXER retry schedule failed row={row['id']}: "
                    f"{type(exc).__name__}")
                continue
        intent = (row.get("attachments") or {}).get("fixer_slack_delivery_intent")
        proof, _ = _readback_fixer_message(readback, intent,
                                           ts=row.get("slack_ts") or None)
        if proof:
            try:
                if (row.get("attachments") or {}).get("fixer_route_pending") is True:
                    if _finish_pending_route_notice(bus, row, proof, identity, log,
                                                    summary):
                        summary["reconciled_posted"] = int(
                            summary.get("reconciled_posted") or 0) + 1
                    exhausted = False
                    break
                if markers.get("fixer_current_attempt_token"):
                    verified = bus.record_held_fixer_readback(
                        row["id"], proof, expected_intent=intent,
                        expected_ts=row.get("slack_ts"))
                    if not verified:
                        exhausted = False
                        break
                    if stale_cycle:
                        exhausted = False
                        break
                posted = bus.reconcile_held_fixer_delivery(
                    row["id"], proof, expected_intent=intent,
                    expected_ts=row.get("slack_ts"))
                if posted and posted.get("delivery_status") == "posted":
                    ticket = bus.ticket(row["ticket_id"])
                    if ticket:
                        _finalize_fixer_post(bus, ticket, posted, identity, log, summary)
            except Exception as exc:  # noqa: BLE001 - retry exact read next loop
                log(f"[slack-convo/outbox] FIXER held reconciliation failed "
                    f"row={row['id']}: {type(exc).__name__}")
        exhausted = False
        break  # at most one Slack readback per 5-second outbox sweep
    if exhausted and len(rows) < page_limit:
        # Tail reached without a Slack attempt. Wrap so rows skipped only because
        # their retry time was in the future are reconsidered on the next pass.
        cursors.pop(identity.name, None)


def _blake_is_member(identity, channel, user):
    """Read every Slack membership page; an incomplete or unsupported read is not proof."""
    if not channel.startswith(("C", "G")) or not user:
        return False
    from slack_sdk import WebClient
    client = WebClient(token=identity.env(identity.bot_token_env), timeout=5)
    cursor = None
    for _ in range(100):
        args = {"channel": channel, "limit": 200}
        if cursor:
            args["cursor"] = cursor
        response = client.conversations_members(**args)
        if not response.get("ok"):
            return False
        if user in (response.get("members") or []):
            return True
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return False
    return False


@_fence.guarded("outbox", lambda: {"paused": 1})
def run_once(bus, post, *, identity, log=print, limit=50, now=None,
             member_check=None, readback=None):
    """Process up to `limit` ready rows for THIS identity.
    post(channel, text, thread_ts=None, blocks=None) -> slack ts.
    Returns a summary dict. Never raises out of the loop."""
    summary = {"posted": 0, "held": 0, "suppressed": 0, "failed": 0, "skipped": 0,
               "resolved": 0, "reclaimed": 0, "requeued_ready": 0,
               "quarantined_held": 0, "reconciled_posted": 0}
    readback = readback or getattr(post, "readback", None)
    member_check = member_check or (lambda channel, user: _blake_is_member(
        identity, channel, user))
    summary["reclaimed"] = _recover_stale_claims(
        bus, identity, log, now=now, readback=readback, summary=summary)
    _report_suppressed_current_notices(bus, identity, log)
    _report_uncertain_outreach(bus, identity, log)
    _reconcile_held_fixer(bus, identity, readback, log, summary)
    route_requeued = _recover_route_missing_fixer(
        bus, identity, member_check, log, now=now)
    summary["reclaimed"] += route_requeued
    summary["requeued_ready"] += route_requeued
    config_requeued = _recover_config_missing_fixer(
        bus, identity, readback, log, now=now)
    summary["reclaimed"] += config_requeued
    summary["requeued_ready"] += config_requeued
    _report_uncertain_fixer(bus, identity, log, now=now)
    _reconcile_posted_fixer(bus, identity, log, summary)
    try:
        rows = bus.outbox("ready", limit=limit, identity=identity.name)
    except TypeError:  # a bus without the identity filter (older fakes)
        rows = bus.outbox("ready", limit=limit)
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] read failed: {type(e).__name__}")
        return summary
    for row in rows:
        try:
            _dispatch_one(bus, post, row, identity=identity, log=log, summary=summary,
                          now=now, readback=readback, member_check=member_check)
        except SupportResolutionAdmissionError as exc:
            log(f"[slack-convo/outbox] support resolution held row={row.get('id')}: {exc}")
            if _hold_support_send(bus, row, str(exc), log):
                summary["held"] += 1
            else:
                summary["skipped"] += 1
        except UncertainFixerDelivery as exc:
            log(f"[slack-convo/outbox] FIXER delivery uncertain row={row.get('id')}: {exc}")
            try:
                current = bus.message(row["id"])
            except Exception as read_exc:  # noqa: BLE001 - DB may be unavailable too
                # The Slack outcome is already uncertain. If the database cannot
                # be read, neither retry nor quarantine is safe. Leave the claim
                # untouched for the stale readback reconciler and keep this row's
                # transport failure from aborting the rest of the outbox sweep.
                log(f"[slack-convo/outbox] CRITICAL FIXER uncertain-state read failed "
                    f"row={row.get('id')}: {type(read_exc).__name__}")
                summary["skipped"] += 1
                continue
            if ((current or {}).get("delivery_status") == "posting"
                    and isinstance(((current or {}).get("attachments") or {}).get(
                        "fixer_slack_delivery_intent"), dict)):
                # The request may have reached Slack. Keep its claim while the
                # bounded stale reconciler looks up the exact message; never resend.
                summary["skipped"] += 1
            elif _quarantine_fixer(bus, row["id"], str(exc), log):
                summary["held"] += 1
            else:
                summary["skipped"] += 1
        except Exception as e:  # noqa: BLE001 - one row never stalls the queue
            log(f"[slack-convo/outbox] row {row.get('id')} failed: {type(e).__name__}")
            if (row.get("attachments") or {}).get("slack_replay_id"):
                try:
                    if _hold_uncertain_replay(
                            bus, row, "Replay send or receipt outcome requires reconciliation", log):
                        summary["held"] += 1
                    else:
                        summary["skipped"] += 1
                except Exception:  # leave the durable claim for stale quarantine
                    summary["skipped"] += 1
                continue
            try:
                bus.mark_message(row["id"], "failed")
            except Exception:  # noqa: BLE001
                pass
            summary["failed"] += 1
    return summary


def _suppress(bus, row, ticket, identity, why, log, summary, *, escalate=True):
    att = row.get("attachments") or {}
    if att.get("fixer_current_attempt_token"):
        # A designated notice may only terminate through the exact unsent CAS.
        # Never let a generic failure handler overwrite its immutable terminal
        # state or mint a random alert after the source has been suppressed.
        try:
            if att.get("identity") != identity.name:
                summary["skipped"] = int(summary.get("skipped") or 0) + 1
                return
            canceled = bus.suppress_unattempted_current_notice(
                row["id"], why, expected_identity=identity.name,
                expected_token=att.get("fixer_current_attempt_token"))
            if not canceled or canceled.get("delivery_status") != "suppressed":
                summary["skipped"] = int(summary.get("skipped") or 0) + 1
                return
            summary["suppressed"] += 1
            log(f"[slack-convo/outbox] SUPPRESSED row {row['id']}: {why}")
            bus.ensure_suppressed_current_notice_alert(row["id"], identity.name)
        except Exception as exc:  # noqa: BLE001 - terminal source scan retries staff INSERT
            log(f"[slack-convo/outbox] designated suppression/alert failed "
                f"row={row['id']}: {type(exc).__name__}")
        return
    bus.mark_message(row["id"], "suppressed", meta_update={"suppressed_why": why})
    summary["suppressed"] += 1
    log(f"[slack-convo/outbox] SUPPRESSED row {row['id']}: {why}")
    if escalate and ticket:
        # V-M5: a human sees every reply the bot declined to send.
        bus.record_outbound(
            ticket_id=ticket["id"], author_type="system",
            body=(f"SUPPRESSED reply on ticket {ticket['id']} ({identity.name}): {why}. "
                  f"Nothing was posted; a person should look."),
            delivery_status="ready", kind=_a.KIND_ESCALATION,
            meta={"identity": identity.name, "suppressed_message_id": row["id"],
                  "recipient_kind": (row.get("attachments") or {}).get("recipient_kind")})


def _defer_held_reconcile_row(bus, row, reason, retry_after, *, log, summary, now=None):
    """Keep an unverified reconciler row retryable instead of permanently suppressing it."""
    retry_after = retry_after or (now or datetime.now(timezone.utc)) + timedelta(minutes=1)
    if retry_after.tzinfo is None:
        retry_after = retry_after.replace(tzinfo=timezone.utc)
    retry_meta = {
        "held_reconcile_retry_after": retry_after.astimezone(timezone.utc).isoformat(),
        "held_reconcile_retry_reason": reason,
    }
    try:
        bus.mark_message(row["id"], "ready", meta_update=retry_meta)
    except Exception as exc:  # noqa: BLE001 - ready/posting row remains retryable
        log(f"[slack-convo/outbox] held reconciliation row {row['id']} "
            f"retry scheduling failed: {type(exc).__name__}")
    summary["skipped"] += 1


def _dispatch_one(bus, post, row, *, identity, log, summary, now=None,
                  member_check=None, readback=None):
    att = row.get("attachments") or {}
    if att.get(SUPPORT_SEND_ADMISSION_KEY):
        # Even an operator release/status edit cannot mint another acquisition
        # after a lost ACK. The frozen invocation and Slack receipt need review.
        summary["skipped"] += 1
        return
    if att.get("fixer_route_pending") is True:
        # Direct first-contact completion owns this reserved row. An outbox
        # sweep must never turn it into a portal delivery or a second Slack POST.
        # A crash before claim (or failed cancellation) leaves it in ready. Only
        # cancel after the direct owner's grace period, via exact ready CAS. If
        # a concurrent owner claimed/prepared it, that CAS loses without mutation.
        if (att.get("identity") == identity.name
                and att.get("fixer_current_attempt_token")
                and _age_seconds(row, now) >= CLAIM_TIMEOUT_SECONDS):
            try:
                canceled = bus.suppress_unclaimed_current_notice(
                    row["id"], "Reserved FIXER notice expired before direct claim")
                if canceled and canceled.get("delivery_status") == "suppressed":
                    summary["suppressed"] = int(summary.get("suppressed") or 0) + 1
                    bus.ensure_suppressed_current_notice_alert(row["id"], identity.name)
            except Exception as exc:  # noqa: BLE001 - ready CAS or suppressed alert scan retries
                log(f"[slack-convo/outbox] reserved notice recovery/alert failed "
                    f"row={row['id']}: {type(exc).__name__}")
        summary["skipped"] += 1
        return
    if (att.get("fixer_current_attempt_token")
            and not config.slack_convo_echo_current_notice_enabled()):
        summary["skipped"] += 1
        return
    if (att.get("fixer_slack_delivery_intent") is not None
            or att.get("fixer_slack_delivery_uncertain")):
        # A release tap or manual status edit must not create a second send from
        # a row whose first Slack outcome is still being reconciled.
        bus.mark_message(row["id"], "held", meta_update={
            "fixer_slack_delivery_uncertain": True,
            "held_why": "Prior FIXER Slack attempt requires readback reconciliation; "
                        "this row cannot be resent",
        })
        summary["held"] += 1
        return
    kind = att.get("kind") or ""
    held_reconcile_candidate = (
        kind == _a.KIND_ESCALATION
        and (att.get("surface") == "held_client_ticket_reconcile"
             or att.get("contract") == "held-client-ticket-reconcile-v1"
             or (row.get("body") or "").startswith("UNRESOLVED CLIENT HOLD:")))
    if held_reconcile_candidate:
        from ..jobs.held_client_ticket_reconciler import held_reconcile_retry_pending
        if held_reconcile_retry_pending(row, now=now):
            summary["skipped"] += 1
            return
    try:
        ticket = bus.ticket(row["ticket_id"])
    except Exception as exc:  # noqa: BLE001 - a transient read must not poison the claim
        if not held_reconcile_candidate:
            raise
        from ..jobs.held_client_ticket_reconciler import RETRY_READ_FAILURE
        retry_after = (now or datetime.now(timezone.utc)) + RETRY_READ_FAILURE
        _defer_held_reconcile_row(bus, row, "dispatch_check_failed", retry_after,
                                  log=log, summary=summary, now=now)
        log(f"[slack-convo/outbox] held reconciliation row {row['id']} deferred after "
            f"ticket read failure: {type(exc).__name__}")
        return
    if not ticket:
        _suppress(bus, row, None, identity, "parent ticket missing", log, summary,
                  escalate=False)
        return
    # only rows for THIS identity; another identity's loop owns the rest
    row_ident = att.get("identity") or ""
    if row_ident and row_ident != identity.name:
        summary["skipped"] += 1
        return
    # Scout raises this system alert precisely when the portal bridge cannot
    # establish the ticket's bot identity. Portal tickets belong to Scout's
    # outbox, while Echo tickets belong to Echo's. Route only this stamped
    # internal escalation; never grant a conversational row this bypass.
    portal_provenance_alert = (
        kind == _a.KIND_ESCALATION
        and row.get("direction") == "outbound"
        and row.get("author_type") == "system"
        and att.get("surface") == "portal_bridge_provenance"
        and row_ident == identity.name
        and (ticket.get("product"), identity.name) in {
            ("echo", "echo"), ("portal", "scout")})
    verifier = getattr(bus, "verified_suppressed_current_notice_alert", None)
    suppressed_notice_alert = False
    if (kind == _a.KIND_ESCALATION and row.get("author_type") == "system"
            and att.get("suppressed_message_id") and callable(verifier)):
        try:
            suppressed_notice_alert = verifier(row, ticket, identity.name)
        except Exception as exc:  # noqa: BLE001 - preserve ready alert on transient source read
            log(f"[slack-convo/outbox] suppressed notice alert verification deferred "
                f"row={row['id']}: {type(exc).__name__}")
            summary["skipped"] += 1
            return
    if att.get("fixer_suppressed_notice_alert") is True and not suppressed_notice_alert:
        summary["skipped"] += 1
        return
    if ((ticket.get("bot_identity") or "") != identity.name
            and not portal_provenance_alert and not suppressed_notice_alert):
        if att.get("portal_progress_status") is True and row_ident == identity.name:
            _suppress(bus, row, ticket, identity,
                      "portal progress status bot identity changed before delivery",
                      log, summary, escalate=False)
            return
        summary["skipped"] += 1
        return
    # 0. fail closed on anything we do not recognise
    if kind not in _a.ALL_KINDS:
        _suppress(bus, row, ticket, identity, f"unknown kind {kind!r}", log, summary)
        return
    if not row_ident:
        _suppress(bus, row, ticket, identity, "row carries no identity stamp", log, summary)
        return
    portal_progress = att.get("portal_progress_status") is True
    if portal_progress:
        fresh_progress = _fresh_portal_progress(bus, ticket, row, identity)
        if not fresh_progress:
            _suppress(bus, row, ticket, identity,
                      "portal progress status no longer matches its open portal request",
                      log, summary)
            return
        ticket = fresh_progress

    # ---- internal kinds: fixer / ops-fix channels, never the person's thread ---------
    if kind in _a.INTERNAL_KINDS:
        held_reconcile = held_reconcile_candidate

        def suppress_stale_held_reconcile(reason):
            # This marker is deliberately excluded from _suppress(): making another
            # escalation row here would recreate the same stale reminder indefinitely.
            log(f"[slack-convo/outbox] held reconciliation row {row['id']} suppressed: "
                f"{reason}")
            bus.mark_message(row["id"], "suppressed",
                             meta_update={"suppressed_why": reason})
            summary["suppressed"] += 1

        if held_reconcile:
            from ..jobs.held_client_ticket_reconciler import dispatch_eligibility
            eligible, reason, retry_after = dispatch_eligibility(bus, row, now=now)
            if eligible is None:
                _defer_held_reconcile_row(bus, row, reason, retry_after,
                                          log=log, summary=summary, now=now)
                return
            if eligible is False:
                suppress_stale_held_reconcile(reason)
                return
        channel = _channel_for(kind, identity)
        if not channel:
            log(f"[slack-convo/outbox] no channel configured for {kind}; row "
                f"{row['id']} marked failed (set AGENT_FIXER_CHANNEL_ID / the ops-fix "
                "channel)")
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        if not _claim(bus, row, log):
            summary["skipped"] += 1
            return
        if suppressed_notice_alert:
            # Recheck the exact persisted alert/source pair after claim. This
            # bypass sends only plain internal text, never the old ticket's
            # customer route or resolve/release action buttons.
            try:
                current_alert = bus.message(row["id"])
                current_ticket = bus.ticket(row["ticket_id"])
                verified = verifier(current_alert, current_ticket, identity.name)
            except Exception as exc:  # noqa: BLE001 - pre-POST staff claim remains recoverable
                log(f"[slack-convo/outbox] claimed suppressed alert verification deferred "
                    f"row={row['id']}: {type(exc).__name__}")
                verified = False
            if not verified:
                summary["skipped"] += 1
                return
        if held_reconcile:
            # The ready row may have waited in the outbox while a requester replied
            # or an operator resolved/advanced the ticket. Recheck after the claim,
            # directly before the internal Slack post.
            eligible, reason, retry_after = dispatch_eligibility(bus, row, now=now)
            if eligible is None:
                _defer_held_reconcile_row(bus, row, reason, retry_after,
                                          log=log, summary=summary, now=now)
                return
            if eligible is False:
                suppress_stale_held_reconcile(reason)
                return
        if kind == _a.KIND_HOLD_NOTICE:
            blocks = hold_notice_blocks(row)
        elif kind == _a.KIND_ESCALATION and not portal_provenance_alert and not suppressed_notice_alert:
            blocks = escalation_blocks(row, ticket)
        else:
            blocks = None
        from . import replay as _replay
        if not _replay.dispatch_allowed(bus, row, identity.name):
            bus.mark_message(row["id"], "held", meta_update={
                "held_why": "Slack replay request or safety authority changed before dispatch"})
            summary["held"] += 1
            return
        try:
            ts = post(channel, row["body"], thread_ts=None, blocks=blocks)
        except Exception as e:  # noqa: BLE001
            # Audit 4, finding 2: a hold card or escalation that fails to post is the case
            # where a human never learns anything -- while the client may already have been
            # acknowledged. It must be LOUD; silence here is the whole failure mode.
            log(f"[slack-convo/outbox] CRITICAL internal {kind} FAILED to reach "
                f"{channel} for ticket {ticket['id']}: {type(e).__name__}: {e}. "
                f"Nobody has been told about this ticket.")
            raise
        if "slack_replay_id" in (row.get("attachments") or {}):
            finished = _replay.finish_delivery(bus, row, ts)
            if not finished or finished.get("delivery_status") != "posted":
                summary["held"] += 1
                return
        else:
            bus.mark_message(row["id"], "posted", slack_ts=ts)
        summary["posted"] += 1
        return

    # ---- conversational kinds: the gates ---------------------------------------------
    # Re-read deployment proof at dispatch time. A queued FIXER acknowledgement,
    # held-answer replacement, or stale notice must never reach a client.
    customer_fix = (not portal_progress
                    and _customer_fix_reply(ticket, att, row.get("body") or ""))
    fixer_grounded_answer = _fixer_grounded_question_answer(
        ticket, att, kind, row.get("body") or "")
    if customer_fix:
        if not _verified_fix_notice(ticket, att, kind, bus=bus, now=now):
            _suppress(bus, row, ticket, identity,
                      "customer fix reply requires current PR merged, deployed and verified",
                      log, summary)
            return
        release_key = ((ticket.get("verification_after") or {}).get("fixer") or {}).get("request_key")
        try:
            current_key = _current_fixer_request_key(bus, ticket)
        except Exception as e:  # noqa: BLE001 - unreadable thread must not close a ticket
            log(f"[slack-convo/outbox] request read failed for {ticket['id']}: "
                f"{type(e).__name__}")
            current_key = None
        fresh_request = _fresh_fixer_request(bus, ticket, att)
        if (not fresh_request or not current_key or release_key != current_key
                or att.get("request_key") != current_key):
            _suppress(bus, row, ticket, identity,
                      "customer fix reply does not match the current requester messages",
                      log, summary)
            return
        ticket = fresh_request
    elif fixer_grounded_answer and not _fresh_fixer_request(
            bus, ticket, att, body=row.get("body") or "", require_direct_answer=True):
        _suppress(bus, row, ticket, identity,
                  "grounded FIXER answer does not match the current requester messages",
                  log, summary)
        return
    # 1. first contact
    if bus.inbound_count(ticket["id"]) < 1:
        _suppress(bus, row, ticket, identity,
                  "ticket has no inbound human message; the bot never speaks first",
                  log, summary)
        return
    # 2. verification for anything substantive
    if kind == _a.KIND_ANSWER and not ticket.get("verification_after"):
        _suppress(bus, row, ticket, identity, "answer with no verification_after", log,
                  summary)
        return
    # 3. no re-entry into the ops-fix worker through our own mouth
    if _REENTRY_PREFIX in (row.get("body") or "").upper():
        _suppress(bus, row, ticket, identity, "reply body carries the ops-fix trigger prefix",
                  log, summary)
        return
    # 4. freshness
    if _age_seconds(row, now) > STALE_AFTER_SECONDS:
        _suppress(bus, row, ticket, identity,
                  f"row sat in ready for over {STALE_AFTER_SECONDS // 3600}h", log, summary)
        return
    # 5. trust ladder, re-checked at post time; held rows always get a card (V-M8). A row
    # Blake has explicitly released is the one exception: his tap already IS the approval.
    recipient_kind = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
    # 5a. D54 hard lines, re-checked at POST time (a flag can flip, and a row can be written
    # by an older process). A substantive ANSWER to a client posts on its own ONLY with that
    # identity's AUTO_ANSWER flag armed, and NEVER when the draft-time check marked it
    # forbidden. Blake's release tap is still the way any of these actually go out.
    if kind == _a.KIND_ANSWER and recipient_kind not in ("staff", "coach") \
            and not att.get("released_by"):
        # M2 (audit 2): this used to trust the stored marker alone, so a row written by any
        # writer that omits it -- a pre-D54 process, a future one -- posted a hard-line
        # answer to a client with no tap. The body is re-read here, which is what "checked
        # again at post time" has to mean for a rule that is about content.
        # Audit 4, finding 4: this re-read only the DENYLIST against the body, so the claim
        # "both are checked at draft time AND at post time" was false and the whole point of
        # the single may_auto_answer decision (that no path enforces half the rule) did not
        # hold for the post-time path. The ticket carries the question -- raw_text -- so the
        # identical decision can be, and now is, made here too.
        # D72 (2026-09-11): the verdict names the rule and carries a TIER. A row the FIXER
        # authored (attachments.fixer) is checked against the org floor only; an Echo
        # draft gets the structural checks too. Either way a hold is never silent:
        # hold_answer_for_team writes the team card and escalates the ticket.
        # Portal holds wait for verification and Blake's customer handoff.
        fixer_authored = bool(att.get("fixer"))
        verdict = _a.auto_answer_verdict(ticket.get("raw_text") or "", row.get("body"),
                                         grounded_by_fixer=fixer_authored)
        if att.get("auto_answer_forbidden") and verdict.ok:
            # A draft-time check marked this org floor; the marker is a hard signal even
            # when the body re-read passes (an older, stricter rule may have written it).
            verdict = _a.AnswerVerdict(False, _a.HOLD_TIER_ORG_FLOOR,
                                       "draft_time_forbidden_marker")
        if verdict.held:
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": f"{verdict.tier}: {verdict.rule}",
                                          "hold_tier": verdict.tier,
                                          "hold_rule": verdict.rule})
            summary["held"] += 1
            _a.hold_answer_for_team(
                bus, ticket=ticket, ident_name=identity.name, recipient_kind=recipient_kind,
                user=ticket.get("slack_user_id") or "?", account_key=None,
                surface=att.get("surface") or "", body=row.get("body") or "",
                held_message_id=row["id"], verdict=verdict,
                person=_person_for_card(bus, ticket, identity),
                fixer_authored=fixer_authored,
                client_notice=(att.get("surface") != "portal_ticket_bridge"
                               and not portal_deliverable(ticket)), log=log)
            return
        if not config.slack_convo_auto_answer_armed(identity.name):
            flag = f"SLACK_CONVO_{identity.name.upper()}_AUTO_ANSWER"
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": "auto answer not armed",
                                          "hold_tier": _a.HOLD_TIER_UNARMED})
            summary["held"] += 1
            _a.hold_answer_for_team(
                bus, ticket=ticket, ident_name=identity.name, recipient_kind=recipient_kind,
                user=ticket.get("slack_user_id") or "?", account_key=None,
                surface=att.get("surface") or "", body=row.get("body") or "",
                held_message_id=row["id"],
                verdict=_a.AnswerVerdict(False, _a.HOLD_TIER_UNARMED, "auto_answer_not_armed"),
                person=_person_for_card(bus, ticket, identity),
                fixer_authored=fixer_authored, unarmed_flag=flag,
                client_notice=(att.get("surface") != "portal_ticket_bridge"
                               and not portal_deliverable(ticket)), log=log)
            return
    # 5b. GAP 2 (audit of PR #68): a row THIS SPECIFIC LANE wrote must re-verify that
    # lane's OWN full three-flag interlock at dispatch time, not only the general
    # _recipient_armed check above. arming.preflight() checks AGENT_CLIENT_DM_AUTOFIX,
    # AGENT_CLIENT_DM_CLIENT_REPLY and a runtime-derived AGENT_CLIENT_DM_LIVE_ACK --
    # none of which _recipient_armed (SLACK_CONVO_<ID>_CLIENT_REPLY alone) has ever
    # read. Without this, a row written while the lane was briefly LIVE stays in
    # 'ready' after AGENT_CLIENT_DM_AUTOFIX is later revoked (or the ack goes stale),
    # and this same _recipient_armed check -- true the whole time, since it is a
    # different, pre-existing, already-armed flag -- would still release it with zero
    # awareness this lane, or its revocation, exists. Scoped to rows carrying this
    # lane's own provenance marker, so no other identity's or lane's delivery changes.
    if not att.get("released_by") and att.get(_client_dm_lane_meta_key()):
        from ..client_dm_support import arming as _cdm_arm
        lane_arm = _cdm_arm.preflight(identity.name)
        if lane_arm.mode != _cdm_arm.MODE_LIVE:
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": "client_dm_support lane no longer "
                                                       f"live at dispatch time: "
                                                       f"{lane_arm.reason}"})
            summary["held"] += 1
            _a.write_hold_notice(
                bus, ident_name=identity.name, tid=ticket["id"],
                recipient_kind=recipient_kind, user=ticket.get("slack_user_id") or "?",
                account_key=None, kind=kind, body=row.get("body") or "",
                held_message_id=row["id"], surface=att.get("surface") or "",
                person=_person_for_card(bus, ticket, identity),
                why=f"client_dm_support's own arming no longer holds at dispatch "
                    f"time (re-checked independently of SLACK_CONVO_"
                    f"{identity.name.upper()}_CLIENT_REPLY): {lane_arm.reason}")
            return
    if not att.get("released_by") and not _recipient_armed(identity, recipient_kind):
        bus.mark_message(row["id"], "held", meta_update={"held_why": "flag off at post time"})
        summary["held"] += 1
        _a.write_hold_notice(
            bus, ident_name=identity.name, tid=ticket["id"], recipient_kind=recipient_kind,
            user=ticket.get("slack_user_id") or "?", account_key=None, kind=kind,
            body=row.get("body") or "", held_message_id=row["id"],
            surface=att.get("surface") or "", why="flag off at post time")
        return
    # 6. claim, immediately before posting
    claim_state = _claim(bus, row, log)
    if not claim_state:
        summary["skipped"] += 1
        return
    if portal_progress:
        fresh_progress = _fresh_portal_progress(bus, ticket, row, identity)
        if not fresh_progress:
            _suppress(bus, row, ticket, identity,
                      "portal progress status changed before portal delivery",
                      log, summary)
            return
        ticket = fresh_progress
    # Membership reads and the claim itself are externally visible boundaries. A
    # correction arriving during either one invalidates a grounded FIXER answer
    # even though that answer legitimately has no deployment record.
    if fixer_grounded_answer or customer_fix:
        fresh = _fresh_fixer_request(
            bus, ticket, att, body=row.get("body") or "",
            require_direct_answer=fixer_grounded_answer)
        release_key = (((fresh or {}).get("verification_after") or {}).get("fixer") or {}).get(
            "request_key")
        if (not fresh
                or (customer_fix and (not _verified_fix_notice(
                    fresh, att, kind, bus=bus, now=now)
                    or release_key != att.get("request_key")))):
            _suppress(bus, row, ticket, identity,
                      "FIXER requester identity changed before delivery", log, summary)
            return
        ticket = fresh
    # 6b. SUPPORT-SURFACE SENDER POLICY, re-checked on the freshest ticket read
    # under the existing fresh request/tenant/version gates above, after the claim
    # and before any Slack post or portal-deliverable marking. A legacy ready row
    # can pass the bot_identity/attachment checks with a stale-but-consistent
    # identity; only the authoritative ticket product/source decides who may speak
    # on a support surface. Mismatch or ambiguity is HELD, never posted.
    try:
        fresh_policy_ticket = bus.ticket(ticket["id"])
    except Exception:  # noqa: BLE001 - an unreadable sender surface never posts
        fresh_policy_ticket = None
    if not isinstance(fresh_policy_ticket, dict):
        _hold_support_surface_sender(bus, row, ticket, identity, None, True,
                                     log, summary)
        return
    # This final read exists only to recheck sender policy. Do not rebind the
    # dispatch ticket from it: between the established freshness gates above
    # and this read, tenant or Slack routing may have changed. Bind the complete
    # dispatch identity and request cycle before accepting the refreshed data.
    _dispatch_identity_fields = (
        "id", "client_id", "slack_user_id", "slack_channel_id",
        "slack_thread_ts", "request_version", "bot_identity", "product",
        "source",
    )
    if any(fresh_policy_ticket.get(field) != ticket.get(field)
           for field in _dispatch_identity_fields):
        _hold_support_surface_sender(bus, row, ticket, identity, None, True,
                                     log, summary)
        return
    _required_sender, _sender_ambiguous = _support_surface_sender(fresh_policy_ticket)
    if _sender_ambiguous or (_required_sender and _required_sender != identity.name):
        _hold_support_surface_sender(bus, row, ticket, identity, _required_sender,
                                     _sender_ambiguous, log, summary)
        return
    # 7. destination.  Compute the FIXER transport from the freshly rebound
    # ticket, never from the pre-claim snapshot.  A portal ticket may acquire its
    # Slack conversation while this worker is claiming the row; that completion
    # must enter the durable intent/readback path rather than the portal-only path.
    channel = ticket.get("slack_channel_id")
    fixer_customer_slack = bool(
        channel
        and recipient_kind not in ("staff", "coach")
        and (customer_fix or att.get("fixer"))
    )
    if fixer_customer_slack:
        try:
            member = bool(channel.startswith(("C", "G")) and member_check and
                          member_check(channel, config.APPROVER_SLACK_ID))
        except Exception as e:  # noqa: BLE001
            log(f"[slack-convo/outbox] membership read failed for {channel}: "
                f"{type(e).__name__}")
            member = False
        if not member:
            _suppress(bus, row, ticket, identity,
                      "FIXER customer Slack reply requires verified Blake membership "
                      "in destination conversation", log, summary)
            return
    surface = att.get("surface") or ""
    # F1 (audit 8, MAJOR): the audit-7 fix read the surface off the ticket's own inbound row
    # instead of its source -- and a portal ticket's inbound row carries
    # 'portal_ticket_bridge', which is not in this tuple, so the notice STILL posted as a
    # thread reply inside the group DM. Byte-identical behaviour to the bug it replaced, and
    # its test asserted the helper on a Slack MPIM ticket, never the portal case it was for.
    # People do not thread in a DM whatever brought the ticket there.
    thread_ts = (None if surface in TOP_LEVEL_SURFACES
                 else ticket.get("slack_thread_ts"))
    if not channel:
        if not portal_progress and recipient_kind not in ("staff", "coach") \
                and (customer_fix or att.get("fixer") is True):
            bus.mark_message(row["id"], "held", meta_update={
                "held_why": "FIXER client completion requires a Slack conversation "
                            "with Blake; portal-only delivery cannot close this ticket",
                "fixer_slack_route_missing": True,
            })
            summary["held"] += 1
            return
        # D48: no Slack thread is not automatically a dead end. A portal-submitted ticket
        # is delivered to the thread the person wrote it in; only a ticket with neither
        # surface fails. Before this, EVERY conversational row on a portal ticket that had
        # not been group-DMed died here -- marked failed, silently, with the person who
        # submitted the form never told anything at all (found live 2026-09-05 on three
        # escalated portal tickets).
        if not portal_deliverable(ticket):
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        bus.mark_message(row["id"], "posted", meta_update={
            "delivered_via": "portal_thread",
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        })
        summary["posted"] += 1
        _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
        # m4: no Slack call happens on this branch -- "posted" here means migration 0310 now
        # lets the client read it in the thread they wrote from. The receipt says exactly
        # that rather than claiming a message was pushed to them.
        _receipt(bus, ticket, row, identity, kind, att,
                 where="released into the portal support thread they wrote from",
                 summary=summary)
        return
    from . import replay as _replay
    if not _replay.dispatch_allowed(bus, row, identity.name):
        bus.mark_message(row["id"], "held", meta_update={
            "held_why": "Slack replay request or safety authority changed before dispatch"})
        summary["held"] += 1
        return
    sent_body = row["body"]
    if fixer_customer_slack:
        mention = f"<@{config.APPROVER_SLACK_ID}>"
        if mention not in sent_body:
            sent_body = f"{mention} {sent_body}"
        # Persist the exact body BEFORE posting, so the portal thread and subsequent
        # receipt cannot disagree with what Slack actually received.
        if sent_body != row["body"]:
            if att.get("fixer_current_attempt_token"):
                # 0384 freezes a designated notice's body once claimed. New
                # notices include Blake's mention at INSERT; a malformed older
                # row must stay unsent for reconciliation.
                held = bus.hold_fixer_config_missing(
                    row["id"], "FIXER designated notice lacks preinsert Blake mention")
                if held and held.get("delivery_status") == "held":
                    summary["held"] += 1
                    return
                raise RuntimeError("FIXER designated notice body cannot be changed")
            stored = bus.set_message_body_if_posting(row["id"], sent_body)
            if not stored or stored.get("body") != sent_body:
                raise RuntimeError("FIXER Slack body update was not confirmed")
            row = {**row, "body": sent_body}
    # Persisting Blake's exact mention/body is another mutation window. Re-read
    # the durable requester identity at the last possible point before Slack.
    if fixer_grounded_answer or customer_fix:
        fresh = _fresh_fixer_request(
            bus, ticket, att, body=row.get("body") or "",
            require_direct_answer=fixer_grounded_answer)
        release_key = (((fresh or {}).get("verification_after") or {}).get("fixer") or {}).get(
            "request_key")
        if (not fresh
                or fresh.get("slack_channel_id") != channel
                or fresh.get("slack_thread_ts") != ticket.get("slack_thread_ts")
                or (customer_fix and (not _verified_fix_notice(
                    fresh, att, kind, bus=bus, now=now)
                    or release_key != att.get("request_key")))):
            _suppress(bus, row, fresh or ticket, identity,
                      "FIXER requester identity changed before Slack delivery", log, summary)
            return
        ticket = fresh
    if fixer_customer_slack:
        expected_claim_attachments = (
            dict((claim_state.get("attachments") or {}))
            if isinstance(claim_state, dict) else None)
        if expected_claim_attachments is None:
            try:
                claimed_row = bus.message(row["id"])
            except Exception as exc:  # noqa: BLE001
                raise UncertainFixerDelivery(
                    f"FIXER claimant snapshot unavailable: {type(exc).__name__}") from exc
            expected_claim_attachments = dict(
                (claimed_row or {}).get("attachments") or {})
        sender = identity.bot_user_id()
        if not sender or not callable(readback):
            reason = "Echo bot identity or Slack readback unavailable before Slack POST"
            held = bus.hold_fixer_config_missing(row["id"], reason)
            if held and held.get("delivery_status") == "held":
                log(f"[slack-convo/outbox] FIXER delivery held before Slack POST "
                    f"row={row.get('id')}: {reason}")
                summary["held"] += 1
                return
            raise RuntimeError("FIXER pre-POST configuration hold was not persisted")
        intent = {
            "channel": channel, "thread_ts": thread_ts, "body": sent_body,
            "sender": sender, "claimed_at": datetime.now(timezone.utc).isoformat(),
            "request_key": att.get("request_key"),
            "request_version": att.get("request_version"),
        }
        prepared = bus.prepare_fixer_delivery(
            row["id"], intent,
            expected_attachments=expected_claim_attachments)
        if (not prepared or prepared.get("delivery_status") != "posting"
                or (prepared.get("attachments") or {}).get("fixer_slack_delivery_intent")
                != intent):
            raise UncertainFixerDelivery("FIXER Slack delivery intent was not persisted")
        # This timestamp follows the first committed intent. Persist it before
        # post() so crash recovery cannot match an identical pre-intent message.
        intent["not_before"] = datetime.now(timezone.utc).isoformat()
        prepared = bus.prepare_fixer_delivery(
            row["id"], intent,
            expected_attachments=dict(prepared.get("attachments") or {}))
        if (not prepared or prepared.get("delivery_status") != "posting"
                or (prepared.get("attachments") or {}).get("fixer_slack_delivery_intent")
                != intent):
            raise UncertainFixerDelivery("FIXER Slack readback boundary was not persisted")
        # Both durable intent writes are mutation windows. A correction, route
        # change, or release revocation during either one must stop the OLD
        # customer message before Slack, even though the intent itself exists.
        saved_att = prepared.get("attachments") or {}
        def refuse_after_intent(reason, current_ticket):
            if saved_att.get("fixer_current_attempt_token"):
                # Once a tokenized notice has durable intent, SQL treats any
                # outcome as potentially sent. Keep it held for exact readback;
                # never turn it into a resendable/supersedable suppressed row.
                held = bus.hold_uncertain_fixer_delivery(row["id"], reason)
                if not held or held.get("delivery_status") != "held":
                    raise UncertainFixerDelivery(
                        "tokenized FIXER refusal could not be quarantined")
                summary["held"] += 1
                log(f"[slack-convo/outbox] FIXER notice held row={row['id']}: {reason}")
            else:
                _suppress(bus, row, current_ticket, identity, reason, log, summary)

        stable_fields = ("kind", "identity", "recipient_kind", "fixer", "request_key",
                         "request_version", "released_by", "pr_url", "resolve_notice",
                         "triage", "surface")
        if (prepared.get("body") != sent_body
                or any(saved_att.get(field) != att.get(field)
                       for field in stable_fields)):
            refuse_after_intent(
                "FIXER row or release changed during Slack intent persistence", ticket)
            return
        if (not saved_att.get("released_by")
                and (not _recipient_armed(identity, recipient_kind)
                     or kind == _a.KIND_ANSWER
                     and not config.slack_convo_auto_answer_armed(identity.name))):
            refuse_after_intent(
                "FIXER client reply release revoked before Slack delivery", ticket)
            return
        try:
            still_member = bool(member_check and member_check(
                channel, config.APPROVER_SLACK_ID))
        except Exception:  # noqa: BLE001 - membership is required, never assumed
            still_member = False
        if not still_member:
            refuse_after_intent(
                "Blake membership changed before FIXER Slack delivery", ticket)
            return
        fresh = _fresh_fixer_request(
            bus, ticket, saved_att, body=sent_body,
            require_direct_answer=fixer_grounded_answer)
        release_key = (((fresh or {}).get("verification_after") or {}).get("fixer") or {}).get(
            "request_key")
        if (not fresh or fresh.get("slack_channel_id") != channel
                or fresh.get("slack_thread_ts") != ticket.get("slack_thread_ts")
                or (customer_fix and (not _verified_fix_notice(
                    fresh, saved_att, kind, bus=bus, now=now)
                    or release_key != saved_att.get("request_key")))):
            refuse_after_intent(
                "FIXER requester, route, or release changed during intent persistence",
                fresh or ticket)
            return
        # _verified_fix_notice may perform independent store reads. Bind the
        # requester once more after those reads and immediately before POST.
        latest = _fresh_fixer_request(
            bus, fresh, saved_att, body=sent_body,
            require_direct_answer=fixer_grounded_answer)
        if (not latest or latest.get("slack_channel_id") != channel
                or latest.get("slack_thread_ts") != ticket.get("slack_thread_ts")):
            refuse_after_intent(
                "FIXER requester changed at final Slack delivery boundary",
                latest or fresh)
            return
        ticket = latest
        try:
            ts = _post_support_resolution(
                bus, post, row, ticket, identity, sent_body, channel, thread_ts,
                readback, kind=kind, att=att, member_check=member_check,
                require_member=True)
        except SupportResolutionAdmissionError:
            raise
        except Exception as exc:  # noqa: BLE001 - a timeout can follow a successful post
            raise UncertainFixerDelivery(
                f"Slack post outcome unknown: {type(exc).__name__}") from exc
        if not isinstance(ts, str) or not ts:
            raise UncertainFixerDelivery("Slack post returned no message timestamp")
        try:
            record_timestamp = getattr(bus, "record_fixer_delivery_timestamp", None)
            if callable(record_timestamp):
                stamped = record_timestamp(row["id"], intent, ts)
            else:  # older bounded test/store adapters; production Bus owns the race-safe path
                stamped = bus.transition_fixer_delivery(row["id"], "posting", slack_ts=ts)
        except Exception as exc:  # noqa: BLE001
            stamped = None
        if not stamped and att.get("fixer_current_attempt_token"):
            held = bus.hold_uncertain_fixer_delivery(
                row["id"], "Slack returned a timestamp after claim ownership changed")
            if held and held.get("delivery_status") == "held":
                stamped = bus.record_fixer_delivery_timestamp(row["id"], intent, ts)
        if not stamped or stamped.get("slack_ts") != ts:
            raise UncertainFixerDelivery("Slack accepted but timestamp persistence unconfirmed")
        proof, reason = _readback_fixer_message(readback, intent, ts=ts)
        if not proof:
            raise UncertainFixerDelivery(reason)
        try:
            posted = bus.transition_fixer_delivery(
                row["id"], "posted", slack_ts=ts, meta_update=proof,
                expected_intent=intent, expected_ts=ts)
            if not posted:
                current = bus.message(row["id"])
                if ((current or {}).get("delivery_status") == "held"
                        and (current or {}).get("slack_ts") == ts):
                    if (current.get("attachments") or {}).get(
                            "fixer_current_attempt_token"):
                        staged = bus.record_held_fixer_readback(
                            row["id"], proof, expected_intent=intent,
                            expected_ts=ts)
                        if not staged:
                            raise UncertainFixerDelivery(
                                "Slack proof could not be staged on held notice")
                    posted = bus.reconcile_held_fixer_delivery(
                        row["id"], proof, expected_intent=intent, expected_ts=ts)
        except Exception as exc:  # noqa: BLE001
            raise UncertainFixerDelivery(
                f"Slack verified but database completion failed: {type(exc).__name__}") from exc
        if (not posted or posted.get("delivery_status") != "posted"
                or (posted.get("attachments") or {}).get("delivery_readback_verified") is not True):
            raise UncertainFixerDelivery("Slack verified but posted state unconfirmed")
    else:
        if not _replay.dispatch_allowed(bus, row, identity.name):
            bus.mark_message(row["id"], "held", meta_update={
                "held_why": "Slack replay request or safety authority changed before Slack delivery"})
            summary["held"] += 1
            return
        ts = _post_support_resolution(
            bus, post, row, ticket, identity, sent_body, channel, thread_ts,
            readback, kind=kind, att=att)
        if "slack_replay_id" in (row.get("attachments") or {}):
            finished = _replay.finish_delivery(bus, row, ts)
            if not finished or finished.get("delivery_status") != "posted":
                summary["held"] += 1
                return
        else:
            bus.mark_message(row["id"], "posted", slack_ts=ts)
    summary["posted"] += 1
    if fixer_customer_slack:
        _finalize_fixer_post(bus, ticket, posted, identity, log, summary)
        return
    try:
        _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
    except Exception as exc:  # noqa: BLE001 - delivery was already committed
        log(f"[slack-convo/outbox] post-delivery ticket update failed row={row['id']}: "
            f"{type(exc).__name__}")
    _receipt(bus, ticket, row, identity, kind, att, where=f"Slack {channel}", summary=summary)


# Kinds worth a receipt in #fixer. An `ack` ("checking that for you now") is noise; what
# Blake asked to see is anything SUBSTANTIVE that reached the client without him: the answer
# itself, the honest no-draft template, and the resolution notice that closes a ticket.
RECEIPT_KINDS = frozenset({_a.KIND_ANSWER, _a.KIND_TEMPLATE, _a.KIND_STATUS})


def write_receipt(bus, ticket, *, identity, body, kind, where, auto, extra=None):
    """The shared receipt writer, so every path that tells a client something -- this
    module's outbox AND the portal bridge's direct-outreach path (M1) -- produces the same
    card. Written only AFTER a delivery actually succeeded."""
    from datetime import datetime as _dt, timezone as _tz
    sent_at = _dt.now(_tz.utc).isoformat()
    how = "SENT AUTOMATICALLY (no tap)" if auto else "sent"
    meta = {"identity": getattr(identity, "name", ""), "receipt": True,
            "receipt_kind": kind, "auto_answer": bool(auto), "sent_at": sent_at}
    if extra:
        meta.update(extra)
    return bus.record_outbound(
        ticket_id=ticket["id"], author_type="system",
        body=(f"RECEIPT: the client was told this, {how}.\n"
              f"BOT: {getattr(identity, 'name', '?')}   TICKET: {ticket['id']}   "
              f"KIND: {kind}\nWHERE: {where}   WHEN: {sent_at}\n"
              f"STATUS NOW: {(bus.ticket(ticket['id']) or ticket).get('status') or '?'}\n\n"
              f"{body or ''}"),
        delivery_status="ready", kind=_a.KIND_ESCALATION, meta=meta)


def _receipt(bus, ticket, row, identity, kind, att, *, where, summary):
    """D55: a receipt of what the client was ACTUALLY told, posted to #fixer.

    Blake, 2026-09-05: "so Blake never has to wonder whether a ticket actually landed with
    the client". This is written only AFTER the row is marked posted, and it quotes the real
    body that went out with the real timestamp, so it can never claim a delivery that did not
    happen. It is an internal kind: it goes to the fixer channel and never into the person's
    thread. A failure here never fails the post that already succeeded."""
    if kind not in RECEIPT_KINDS:
        return
    if (att or {}).get("recipient_kind") in ("staff", "coach"):
        return  # staff can see their own thread; a receipt would just be an echo
    try:
        sent_at = datetime.now(timezone.utc).isoformat()
        # m4: re-read the ticket so STATUS NOW is the status now, not the one captured
        # before _resolve_on_answer ran a line earlier.
        fresh = None
        try:
            fresh = bus.ticket(ticket["id"])
        except Exception:  # noqa: BLE001
            fresh = None
        status_now = (fresh or ticket).get("status") or "?"
        auto = bool(kind == _a.KIND_ANSWER and not (att or {}).get("released_by"))
        how = ("SENT AUTOMATICALLY (no tap)" if auto else
               ("sent after your tap" if (att or {}).get("released_by") else "sent"))
        # FIXER provenance can remain after a human tap; only the release actor counts.
        if (att or {}).get("released_by") == "fixer":
            how = "sent automatically by FIXER"
        owner = f"<@{config.APPROVER_SLACK_ID}> " if (att or {}).get("fixer") else ""
        writer = getattr(bus, "record_fixer_receipt_once", None)
        if not callable(writer):
            return
        writer(
            source_message_id=row["id"],
            ticket_id=ticket["id"], author_type="system",
            body=(f"{owner}RECEIPT: the client was told this, {how}.\n"
                  f"BOT: {identity.name}   TICKET: {ticket['id']}   KIND: {kind}\n"
                  f"WHERE: {where}   WHEN: {sent_at}\n"
                  f"STATUS NOW: {status_now}\n\n{row.get('body') or ''}"),
            # C3: an ESCALATION row, because that is a kind the portal already hides from
            # clients. attachments.receipt marks it as a receipt for everything on this side.
            delivery_status="ready", kind=_a.KIND_ESCALATION,
            meta={"identity": identity.name, "receipt_for": row["id"], "receipt": True,
                  "receipt_kind": kind, "auto_answer": auto, "sent_at": sent_at})
    except Exception:  # noqa: BLE001 - never undo a successful post over a receipt
        pass


def _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log=print):
    """Round 2 (audit of PR #107, MAJOR 6): what happens to the ticket once an answer is
    with the person. An answer that promised a HUMAN follow-up does not close the ticket --
    it is routed to the FIXER with adapter.FOLLOW_UP_MARKER (idempotent: the Slack adapter
    may already have done this at draft time). Every other answer resolves as before."""
    if "slack_replay_id" in (row.get("attachments") or {}):
        # Replay's draft-time routing already committed atomically. An older
        # posted answer may neither resolve nor re-route a newer human request.
        _resolve_on_answer(bus, ticket, row, kind, summary, att, row.get("body") or "")
        return
    if kind == _a.KIND_ANSWER and (att or {}).get("recipient_kind") not in ("staff", "coach") \
            and _a.promises_human_follow_up(row.get("body") or ""):
        _a.route_follow_up_promise(bus, ticket, ident_name=identity.name,
                                   body=row.get("body") or "",
                                   recipient_kind=(att or {}).get("recipient_kind") or "client",
                                   surface=(att or {}).get("surface") or "",
                                   person=_person_for_card(bus, ticket, identity), log=log)
        return
    _resolve_on_answer(bus, ticket, row, kind, summary, att, row.get("body") or "")


def _resolve_on_answer(bus, ticket, row, kind, summary, att=None, body=""):
    """V-M4: the ticket closes when the person HAS the message, not when we drafted it.

    Two rows close a ticket: the ANSWER that answered it, and the resolve NOTICE a human
    tapped (MINOR 5, audit 7 -- resolve_and_notify used to stamp the ticket itself, before
    delivery, so a failed post left a ticket claiming to be resolved over a failed row)."""
    meta = att or {}
    try:
        current_message = bus.message(row["id"])
    except Exception:  # a stale caller snapshot cannot bypass durable send uncertainty
        return
    if not current_message or not _support_send_completed(current_message):
        return
    fixer = bool(meta.get("fixer"))
    current_notice_enabled = config.slack_convo_echo_current_notice_enabled()
    if meta.get("fixer_current_attempt_token") and not current_notice_enabled:
        # Disabling new notices cannot downgrade an existing reservation to
        # the legacy close path, even after a successful client delivery.
        return
    should_resolve = (
        kind == _a.KIND_ANSWER and ticket.get("status") == "verification"
        or kind == _a.KIND_STATUS and meta.get("resolve_notice") is True
        and ticket.get("status") != "resolved")
    if "slack_replay_id" in (row.get("attachments") or {}):
        if should_resolve:
            from . import replay
            try:
                if replay.resolve_delivery(bus, row).get("resolved") is True:
                    summary["resolved"] += 1
            except Exception:  # failed or unreadable CAS leaves the ticket open
                pass
        return
    if fixer and should_resolve:
        # Slack (or the portal thread) may accept a delivery while the requester
        # corrects it or an operator changes its eligibility. Keep the receipt for
        # exactly what was delivered, but resolve only through the database CAS
        # over the complete ticket identity that was freshly validated here.
        grounded = _fixer_grounded_question_answer(ticket, meta, kind, body)
        fresh = _fresh_fixer_request(
            bus, ticket, meta, body=body, require_direct_answer=grounded)
        current_notice = (ticket.get("product") == "echo"
                          and ticket.get("source") == "website_tab"
                          and current_notice_enabled)
        token = meta.get("fixer_current_attempt_token")
        resolver = getattr(bus, ("resolve_current_notice" if current_notice
                                 else "resolve_current_delivery"), None)
        if current_notice and (not token or not row.get("id") or
                               row.get("delivery_status") != "posted"):
            return
        if not fresh or not callable(resolver):
            return
        if meta.get("resolve_notice"):
            release_key = ((fresh.get("verification_after") or {}).get("fixer") or {}).get(
                "request_key")
            if release_key != meta.get("request_key"):
                return
        expected = {
            "status": fresh.get("status"),
            "classification": fresh.get("classification"),
            **{field: fresh.get(field) for field in _FIXER_REQUEST_IDENTITY_FIELDS},
        }
        try:
            if current_notice:
                resolved = resolver(fresh, row["id"], token,
                                    meta.get("delivery_expected_status"))
            else:
                resolved = resolver(
                    ticket["id"], meta.get("request_version"),
                    expected["status"], expected["classification"],
                    expected["product"], expected["client_id"],
                    expected["bot_identity"], expected["slack_user_id"],
                    expected["slack_channel_id"], expected["slack_thread_ts"])
        except Exception:  # noqa: BLE001 - a failed atomic close leaves it open
            return
        if (not isinstance(resolved, dict)
                or resolved.get("id") != ticket.get("id")
                or resolved.get("status") != "resolved"
                or resolved.get("request_version") != meta.get("request_version")
                or resolved.get("classification") != expected["classification"]
                or resolved.get("escalated") is not False
                or resolved.get("hold_tier") is not None
                or any(resolved.get(field) != expected[field]
                       for field in _FIXER_REQUEST_IDENTITY_FIELDS)):
            return
        summary["resolved"] += 1
        return
    if fixer:
        # A FIXER row that is not presently eligible to resolve must never fall
        # through to the legacy unconditional ticket PATCH below.
        return
    if (ticket.get("product") == "echo"
            and ticket.get("source") == "website_tab"
            and current_notice_enabled):
        # 0384 protects every Echo website-tab resolution, including ordinary
        # answers and human taps. Unreserved legacy rows remain open.
        return
    if kind == _a.KIND_ANSWER and ticket.get("status") == "verification":
        bus.set_ticket(ticket["id"], status="resolved")
        summary["resolved"] += 1
    elif kind == _a.KIND_STATUS and (att or {}).get("resolve_notice"):
        if ticket.get("status") != "resolved":
            bus.set_ticket(ticket["id"], status="resolved")
            summary["resolved"] += 1


@_fence.guarded("operator_release", lambda: False)
def release_held(bus, message_id, *, approved_by, identity=None, log=print):
    """A human tap on a hold notice: flip that held row to ready and stamp the ticket.
    Returns True when a held row was released. Refuses anything not currently held, any
    kind that is not a reply or fixer request, and (when `identity` is given) any row
    another bot wrote (V-m10). The release is stamped (N2: read by gate 5 above to skip the
    trust-ladder recheck exactly once for this row) and restarts the freshness clock."""
    row = bus.message(message_id)
    if not row or row.get("delivery_status") != "held":
        return False
    att = row.get("attachments") or {}
    kind = att.get("kind") or ""
    if kind not in (_a.CONVERSATIONAL_KINDS | {_a.KIND_FIXER_REQUEST}):
        log(f"[slack-convo/outbox] release refused: row {message_id} kind {kind!r}")
        return False
    if identity is not None and (att.get("identity") or "") != identity.name:
        log(f"[slack-convo/outbox] release refused: row {message_id} belongs to "
            f"{att.get('identity') or '?'} not {identity.name}")
        return False
    if (att.get("fixer_slack_delivery_intent") is not None
            or att.get("fixer_slack_delivery_uncertain")
            or att.get("slack_replay_delivery_uncertain")
            or att.get("fixer_slack_route_missing")):
        log(f"[slack-convo/outbox] release refused: FIXER delivery row {message_id} "
            "requires route or Slack readback reconciliation")
        return False
    bus.mark_message(message_id, "ready",
                     meta_update={"released_at": datetime.now(timezone.utc).isoformat(),
                                  "released_by": approved_by})
    try:
        bus.set_ticket(row["ticket_id"], approved_by=approved_by, approved_via="slack_button",
                       approved_at=datetime.now(timezone.utc).isoformat())
    except Exception as e:  # noqa: BLE001 - the release itself already happened
        log(f"[slack-convo/outbox] approval stamp failed: {type(e).__name__}")
    return True


@_fence.guarded("operator_resolve", lambda: False)
def resolve_and_notify(bus, ticket_id, *, approved_by, identity, log=print):
    """A human tap on an escalation card: tell the person it is handled, and close the
    ticket (D48).

    The notice is written as a normal conversational row, so it goes out through every gate
    this module already enforces (first contact, trust ladder, freshness, claim) and lands
    wherever that ticket's person actually is: the group DM if one was opened, the portal
    support thread otherwise. Nothing is posted from here directly.

    Refuses, returning False, when: the ticket is gone, it belongs to another bot (the same
    cross-identity rule release_held holds), it is already resolved (the tap is idempotent --
    a second press must not write a second notice), or there is nowhere to deliver."""
    ticket = bus.ticket(ticket_id)
    if not ticket:
        log(f"[slack-convo/outbox] resolve refused: no ticket {ticket_id}")
        return False
    if identity is not None and (ticket.get("bot_identity") or "") != identity.name:
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} belongs to "
            f"{ticket.get('bot_identity') or '?'} not {identity.name}")
        return False
    if ticket.get("status") == "resolved":
        return False
    customer_fix = _customer_fix_reply(ticket, {})

    def refuse_resolve(reason):
        why = f"Resolve tap on ticket {ticket_id} did NOT go through: {reason}. " \
              "The ticket is unchanged."
        log(f"[slack-convo/outbox] {why}")
        try:
            bus.record_outbound(
                ticket_id=ticket_id, author_type="system", body=why,
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": getattr(identity, "name", ""),
                      "resolve_refused": True})
        except Exception:  # noqa: BLE001 - refusal still stands
            pass
        return False

    if (config.slack_convo_echo_current_notice_enabled()
            and ticket.get("product") == "echo"
            and ticket.get("source") == "website_tab"
            and (not customer_fix or ticket.get("escalated") is True
                 or ticket.get("hold_tier") is not None)):
        # 0384 reserves only current unheld verification/merged notices. A
        # generic or held tap cannot borrow that reservation; 0383 owns the
        # separate held release proof. Leave this ticket and client untouched.
        return refuse_resolve(
            "this Echo website ticket requires current notice or held release proof; "
            "verify the current request through the guarded FIXER workflow before closing")

    # MINOR 5's fix moved the resolved stamp to delivery time, which quietly broke what the
    # status check had been doing double duty for: idempotence. A second tap before the
    # notice posts would have written a SECOND notice. The notice row itself is the record of
    # "this tap already happened", so that is what is checked.
    if _resolve_notice_exists(bus, ticket_id):
        return False
    if not portal_deliverable(ticket) and not ticket.get("slack_channel_id"):
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} has no delivery "
            "surface (no portal thread, no group DM)")
        return False
    # Audit 4, finding 9: this marked the ticket resolved and returned True even when the
    # notice would be HELD by the trust ladder -- Blake taps "Resolved, tell them", the tap
    # reports ok, the ticket reads resolved, and the client is never told. If we cannot
    # deliver the notice, we do not claim the resolution.
    recipient_kind = ticket.get("identity_kind") or "client"
    if not _recipient_armed(identity, recipient_kind):
        # Audit 5, finding 4: refusing silently is its own version of the dead button this
        # whole path exists to fix -- Blake taps "Resolved, tell them" and gets nothing at
        # all. The refusal is written back to the fixer channel, naming the flag to flip.
        flag = ("STAFF_REPLY" if recipient_kind in ("staff", "coach") else "CLIENT_REPLY")
        why = (f"Resolve tap on ticket {ticket_id} did NOT go through: the notice to the "
               f"{recipient_kind} would be held because SLACK_CONVO_"
               f"{getattr(identity, 'name', '?').upper()}_{flag} is off, and marking a "
               f"ticket resolved that the person was never told about is the lie this "
               f"button exists to prevent. Arm that flag, or reply to them directly and "
               f"close it by hand. The ticket is unchanged.")
        log(f"[slack-convo/outbox] {why}")
        try:
            bus.record_outbound(
                ticket_id=ticket_id, author_type="system", body=why,
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": getattr(identity, "name", ""), "resolve_refused": True})
        except Exception:  # noqa: BLE001 - the refusal itself already stands
            pass
        return False
    if customer_fix:
        proof_meta = {"resolve_notice": True, "pr_url": ticket.get("fix_pr_url")}
        if not _verified_fix_notice(ticket, proof_meta, _a.KIND_STATUS, bus=bus):
            return refuse_resolve("customer fix has no current merged, deployed and "
                              "independently verified business postcondition")
        try:
            current_key = _current_fixer_request_key(bus, ticket)
        except Exception:  # noqa: BLE001 - a human tap cannot waive unreadable context
            current_key = None
        release_key = ((ticket.get("verification_after") or {}).get("fixer") or {}).get("request_key")
        if not current_key or release_key != current_key:
            return refuse_resolve("customer request changed or could not be verified")
        if not str(ticket.get("slack_channel_id") or "").startswith(("C", "G")):
            return refuse_resolve("customer fix has no group conversation for Blake to join")
    # Every resolution notice must carry the same immutable requester cycle that
    # the send boundary verifies. Bind it at INSERT so a new request cannot inherit
    # this human tap, including notices outside the 0384 reservation path.
    request_version = ticket.get("request_version")
    if type(request_version) is not int or request_version < 0:
        return refuse_resolve("customer request version is unavailable")
    # MINOR 4 (audit 7): `surface` was the ticket's SOURCE ("website_tab"), which is not one
    # of the surfaces gate 7 knows, so the notice was posted as a THREAD REPLY inside a DM --
    # a place people do not look. The real surface is on the ticket's own inbound rows.
    surface = _surface_of(bus, ticket_id) or (ticket.get("source") or "")
    try:
        bus.record_outbound(
            ticket_id=ticket_id, author_type=getattr(identity, "name", "system"),
            body=RESOLVED_NOTICE, delivery_status="ready", kind=_a.KIND_STATUS,
            expected_request_version=request_version,
            # Match the recipient checked by the trust ladder above.
            meta={"identity": getattr(identity, "name", ""), "recipient_kind": recipient_kind,
                  "surface": surface, "resolved_by": approved_by, "resolve_notice": True,
                  **({"fixer": True, "request_key": current_key,
                      "request_version": request_version,
                      "pr_url": ticket.get("fix_pr_url")} if customer_fix else {})})
    except Exception as exc:  # noqa: BLE001 - an uncertain write never stamps approval
        # The flag or request can change after the precheck; a rejected
        # reservation or lost transport response must not stamp approval.
        # Preserve any committed row for normal readback; do not retry the tap here.
        log(f"[slack-convo/outbox] resolve refused: notice write for ticket "
            f"{ticket_id} failed: {type(exc).__name__}")
        return False
    # MINOR 5 (audit 7): the ticket used to be stamped resolved HERE, before the notice had
    # been delivered -- so a post failure left a ticket permanently asserting it was resolved
    # over a row marked failed. Same rule as an answer (V-M4): the ticket closes when the
    # person HAS the message. _resolve_on_answer closes it when this row posts.
    bus.set_ticket(ticket_id, approved_by=approved_by, approved_via="slack_button",
                   approved_at=datetime.now(timezone.utc).isoformat())
    return True


def _recent(bus, ticket_id, limit=200):
    """The NEWEST rows on a ticket, newest first.

    MINOR 1 (audit 8): bus.messages orders created_at.asc, so a client-side scan of its first
    200 rows reads the OLDEST 200 -- the exact bug count_escalation_cards_since was added to
    fix, reintroduced by hand in three helpers at once. For _resolve_notice_exists the
    failure direction was a DUPLICATE resolve notice to a client."""
    try:
        return bus.recent_messages(ticket_id, limit=limit)
    except AttributeError:
        rows = bus.messages(ticket_id, limit=limit) or []
        return list(reversed(rows))


def _resolve_notice_exists(bus, ticket_id):
    """True once a resolve notice has been written for this ticket, in any delivery state.
    Fails CLOSED (True) on a read failure: a duplicate notice to a client is worse than a
    tap that reports nothing happened."""
    try:
        for m in _recent(bus, ticket_id) or []:
            att = m.get("attachments") or {}
            if m.get("direction") == "outbound" and att.get("resolve_notice"):
                return True
        return False
    except Exception:  # noqa: BLE001
        return True


def _surface_of(bus, ticket_id):
    """The surface this ticket's human actually spoke on, from its own inbound rows."""
    try:
        for m in _recent(bus, ticket_id) or []:
            if m.get("direction") == "inbound":
                s = ((m.get("attachments") or {}).get("surface") or "").strip()
                if s:
                    return s
    except Exception:  # noqa: BLE001
        pass
    return ""
