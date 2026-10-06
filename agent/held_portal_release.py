"""Explicit 0383 portal closeout; no scheduler, Slack send or proof creation.

An operator must independently inspect the genuine HTTPS review/behavior
receipts and create the immutable 0383 proof first. This helper publishes only
that proof's exact notice to its existing portal ticket and calls the release
CAS. It never changes a ticket directly or mutates a posted notice. A posted
portal row is client-visible under 0310 and the portal's clientVisible filter;
this is delivery to the portal thread, not evidence that the client read it.
"""
import json
import re

from .slack_convo.bus import BusError

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_FIELDS = ("product", "client_id", "bot_identity", "slack_user_id",
           "slack_channel_id", "slack_thread_ts")


def _require(ok, reason):
    if not ok:
        raise BusError(409, reason)


def _notice_matches(row, expected):
    return isinstance(row, dict) and all(row.get(k) == v for k, v in expected.items()
                                        if k != "delivery_status")


def deliver_held_portal_notice(bus, *, ticket_id, request_version, proof_id):
    """Publish a pre-authorized exact notice, then attempt 0383's held release.

    Resume always uses the proof's UUID: a lost INSERT/PATCH/RPC response cannot
    produce another notice. Transport failures propagate; retry this same tuple
    after readback. This intentionally uses a held row, excluded from ordinary
    outbox sweeps, and has no post-delivery housekeeping metadata to conflict
    with 0383/0384's posted-release immutability guard.
    """
    _require(_UUID.fullmatch(str(ticket_id)) and _UUID.fullmatch(str(proof_id))
             and type(request_version) is int and request_version >= 0,
             "invalid held portal release identity")
    ticket = bus.ticket(ticket_id)
    rows = bus._get("fixer_release_proofs", {
        "id": f"eq.{proof_id}", "ticket_id": f"eq.{ticket_id}",
        "request_version": f"eq.{request_version}", "select": "*", "limit": "2"})
    _require(isinstance(rows, list) and len(rows) == 1 and isinstance(ticket, dict),
             "exact persisted held release proof required")
    proof = rows[0]
    evidence = proof.get("repair_evidence")
    _require(isinstance(evidence, dict) and evidence.get("verified") is True
             and evidence.get("ticket_id") == ticket_id
             and type(evidence.get("request_version")) is int
             and evidence["request_version"] == request_version
             and all(isinstance(evidence.get(k), str) and evidence[k].strip()
                     for k in ("verifier_identity", "repair_author_identity", "repair_revision"))
             and evidence["verifier_identity"].strip() != evidence["repair_author_identity"].strip()
             and all(isinstance(evidence.get(k), str)
                     and re.fullmatch(r"https://\S+", evidence[k])
                     for k in ("review_receipt", "behavior_receipt")),
             "independent held release receipts required")
    _require(proof.get("id") == proof_id and proof.get("ticket_id") == ticket_id
             and proof.get("request_version") == request_version
             and ticket.get("request_version") == request_version
             and ticket.get("source") == "website_tab" and ticket.get("product") == "echo"
             and bool(ticket.get("client_id")) and ticket.get("bot_identity") == "echo"
             and ticket.get("slack_channel_id") is None and ticket.get("slack_thread_ts") is None
             and all(proof.get(k) == ticket.get(k) for k in _FIELDS)
             and proof.get("expected_classification") == ticket.get("classification")
             and isinstance(ticket.get("verification_before"), dict)
             and bool(ticket["verification_before"])
             and ((ticket.get("verification_after") or {}).get("fixer") or {}).get(
                 "held_release_review") == evidence,
             "held portal release ticket or destination changed")
    notice_id = proof.get("notice_message_id")
    _require(_UUID.fullmatch(str(notice_id)) and isinstance(proof.get("notice_body"), str)
             and 0 < len(proof["notice_body"].strip()) and len(proof["notice_body"]) <= 8000,
             "exact bounded held release notice required")
    att = {"kind": "status", "resolve_notice": True, "fixer": True,
           "fixer_release": True, "fixer_release_proof_id": proof_id,
           "delivery_identity_fence": True, "identity": "echo",
           "recipient_kind": "client", "surface": "held_portal_release",
           "request_version": request_version,
           "delivery_expected_status": proof.get("expected_status"),
           "delivery_expected_classification": proof.get("expected_classification")}
    att.update({f"delivery_expected_{k}": proof.get(k) for k in _FIELDS})
    expected = {"id": notice_id, "ticket_id": ticket_id, "author_type": "ranger",
                "author_id": None, "body": proof["notice_body"], "direction": "outbound",
                "delivery_request_version": request_version, "attachments": att,
                "slack_ts": None, "slack_event_id": None}
    notice = bus.message(notice_id)
    if proof.get("consumed_at") is not None:
        _require(ticket.get("status") == "resolved" and ticket.get("escalated") is False
                 and ticket.get("hold_tier") is None
                 and proof.get("consumed_message_id") == notice_id
                 and _notice_matches(notice, expected) and notice.get("delivery_status") == "posted",
                 "consumed release proof does not match completed portal notice")
        return {"notice_message_id": notice_id, "delivered": True, "resolved": True}
    _require(ticket.get("escalated") is True and ticket.get("hold_tier") == "routine"
             and proof.get("hold_tier") == "routine"
             and ticket.get("status") == proof.get("expected_status")
             and ((ticket.get("status") == "merged" and ticket.get("classification") == "code_fix")
                  or (ticket.get("status") == "verification" and ticket.get("classification") in
                      {"answerable_question", "code_fix", "ops_fix", "action_request"})),
             "current verified routine held predecessor required")
    if notice is None:
        bus._insert("support_messages", {**expected, "delivery_status": "held"})
        notice = bus.message(notice_id)
    _require(_notice_matches(notice, expected) and notice.get("delivery_status") in {"held", "posted"},
             "proof-designated portal notice changed")
    if notice["delivery_status"] == "held":
        # 0383/0384 validates the current unconsumed proof and full identity under
        # the ticket lock before this row becomes client-visible. Readback,
        # rather than the PATCH representation, establishes delivery.
        bus._patch("support_messages", {"id": f"eq.{notice_id}",
                   "delivery_status": "eq.held", "ticket_id": f"eq.{ticket_id}",
                   "delivery_request_version": f"eq.{request_version}",
                   "attachments": "eq." + json.dumps(att, separators=(",", ":")),
                   "body": "eq." + proof["notice_body"]}, {"delivery_status": "posted"})
        notice = bus.message(notice_id)
    _require(_notice_matches(notice, expected) and notice.get("delivery_status") == "posted",
             "posted portal notice readback required")
    params = {"p_ticket_id": ticket_id, "p_expected_request_version": request_version,
              "p_expected_status": proof["expected_status"],
              "p_expected_classification": proof["expected_classification"],
              "p_expected_hold_tier": "routine", "p_proof_id": proof_id,
              "p_notice_message_id": notice_id}
    params.update({f"p_expected_{k}": proof.get(k) for k in _FIELDS})
    # No blind RPC retry. If its response is lost, the next invocation verifies
    # the consumed proof and immutable posted notice before reporting success.
    response = bus._client().post(
        bus._rest("rpc/fixer_release_held_delivery"), data=json.dumps(params),
        headers=bus._headers(), timeout=30)
    if response.status_code >= 400:
        raise BusError(response.status_code, "held portal release CAS failed")
    latest = bus.ticket(ticket_id)
    proofs = bus._get("fixer_release_proofs", {"id": f"eq.{proof_id}", "select": "*", "limit": "2"})
    resolved = (isinstance(latest, dict) and latest.get("status") == "resolved"
                and latest.get("request_version") == request_version
                and latest.get("classification") == proof["expected_classification"]
                and latest.get("escalated") is False and latest.get("hold_tier") is None
                and all(latest.get(k) == proof.get(k) for k in _FIELDS)
                and len(proofs) == 1 and proofs[0].get("consumed_at") is not None
                and proofs[0].get("consumed_message_id") == notice_id)
    return {"notice_message_id": notice_id, "delivered": True, "resolved": resolved}
