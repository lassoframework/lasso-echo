"""
fixer_ops.py -- the FIXER's ops-action lane (D72, Blake 2026-09-11).

"Any message someone sends Echo, Ranger, Wrangler, or Scout is addressed, and if it needs
a fix it goes to Claude Code (the FIXER) -- autonomous, without me." The FIXER
(scout-listener, on the Mac) triages support_tickets and drafts answers; this module gives
it HANDS: a small, closed catalog of operator actions Echo already knew how to do by hand
(a connect-link resend, a budget reset, a photo swap, a re-stage), each a thin, idempotent
wrapper over the existing function, reachable over HTTP with a shared secret.

CONTRACT (the FIXER builder reads this block):

  POST /ops/actions/<action>
      headers   X-Fixer-Ops-Secret: <env FIXER_OPS_SECRET>   (constant-time compare)
                Content-Type: application/json
      body      {"gym_key": "<echo account key>", "ticket_id": "<support_tickets.id>",
                 "args": {...},                             (args per action, below)
                 "reservation_key"?: "<8-128 [A-Za-z0-9_-]>"}
      reservation_key is OPTIONAL. Absent: behavior is exactly as before. Present: the
      action runs AT MOST ONCE per key -- a durable receipt (agent/fixer_ops_receipts,
      sqlite kv on the volume host, prefix ops_receipt_) is reserved BEFORE any side
      effect, then committed with the result, failed on a refusal, or marked unknown
      when the outcome cannot be determined. The key is validated EXACTLY as supplied
      (no trimming, no coercion): anything outside 8-128 [A-Za-z0-9_-] is 400
      bad_reservation_key. A replay (same key, same payload hash)
      returns the completed result with "replayed": true and NEVER re-executes;
      reserved, failed, and unknown receipts refuse replay. A key reused with a
      different payload is 409 reservation_conflict. Keyed restage_month is refused
      because its background job registry is not durable. Keyed 2xx bodies carry
      "receipt": {...}; unkeyed calls retain their existing response shape.
      200 {"ok": true, "action": ..., "gym_key": ..., "ticket_id": ..., "result": {...}}
      202 {"ok": true, "action": "restage_month", "job_id": "...", "status": "running", ...}
      400 {"error": "bad_request", "detail": ...}          malformed body / bad args
      401 {"error": "unauthorized"}                          missing or wrong secret
      403 {"error": "org_floor", ...}                        a refused action (see below)
      404 {"error": "unknown_action" | "gym_not_found" | "row_not_found", ...}
      409 {"error": ..., ...}                                refusal, in-progress, failed receipt,
                                                            or unsupported keyed background job
      503 {"error": "ops_secret_unset" | "volume_unavailable" | "store_unavailable" |
                    "receipt_store_not_durable", ...}
  GET  /ops/actions/receipts/<key>?gym_key=<gym>   (same header)
      200 {"ok": true, "receipt": {...}}   the reservation receipt for <key>.
      Tenant-bound: a receipt belonging to another gym is 403 receipt_tenant_mismatch;
      an unknown key is 404 unknown_receipt; a malformed key is 400 bad_reservation_key.
  GET  /ops/actions/jobs/<job_id>          (same header)
      200 {"ok": true, "job": {"id", "action", "gym_key", "ticket_id", "status":
           "running"|"done"|"failed"|"timed_out", "started_at", "finished_at",
           "result"|"error", "steps": [...]}}
      404 {"error": "unknown_job"}
  GET  /ops/actions                        (same header)  -> the catalog, as JSON
  GET  /ops/actions/resend_connect_link/readiness/<gym_key>  (same header)
      200 {"category": "ready"|"missing"|"ambiguous"|"user-email-missing"|
           "portal-unavailable"}
      This is portal-read-only: no Slack lookup, link mint, ticket record, or DM.
  GET  /ops/actions/evidence/...           (same header)
      Bounded read-only diagnostics (agent/fixer_evidence.py). Never runs an action;
      503 volume_unavailable on a host without the worker volume, 503
      evidence_unavailable on a source fault.
  POST /ops/actions/business-evidence/observe  (same header)
      Bounded read-only business postcondition observation. The body is the exact
      versioned Scout contract: schema/contract version, support ticket UUID,
      portal client UUID, current request SHA, merged Git SHA, registered check id,
      and that check's strict params. Echo first re-reads the ticket/client binding,
      then runs fixer_business_evidence.observe. No action, ticket note, or other
      mutation occurs. The response is the canonical evidence record plus params.
  GET  /ops/actions/reply-reconciliation/<snapshot_id>?gym_key=<gym>
      Read-only comparison of a new immutable inbox-alert snapshot with current
      provider evidence. Tenant bound. Missing, incomplete, ambiguous, or older
      than 48 hours is refused. Mentions always remain needs_human. This endpoint
      never replies, hides, deletes, likes, or sends a message.

  RESULT EVIDENCE (canonical, consumed by Scout): every synchronous 2xx result
  carries "captured_at" (ISO utc) marking when its evidence was read, and a
  "postcondition_verified" flag that is true ONLY on a real independent readback:
    resend_connect_link   proves actual sent state with the provider's message
                          identity from the send call ("sent_message_identity"):
                          sent true and postcondition_verified true ONLY with that
                          identity. A bare no-exception return nulls "sent", keeps
                          postcondition_verified false, and adds an "evidence_note"
                          saying the sent state is unproven -- never claimed.
    reset_recreate_budget compares before/after budget values re-read from the kv
                          around the write; 409 postcondition_unconfirmed unless the
                          after-read shows used=0 and remaining=limit.
    release_denied_assets reports the scoped sweep's checked/rolled_back counts
                          exactly as measured, then independently re-proves the
                          rollback: a bounded readback scoped to this gym (the
                          gym_media_use ledger, a strict denied-row calendar
                          re-read, and a per-asset usage-counter re-read) must
                          confirm every rollback the sweep claimed, or
                          postcondition_verified stays false with an evidence_note.
                          A 0-rollback sweep is not a release and never verifies.
    swap_media            exposes the row identity and verifies the handler's write
                          against an independent calendar get_row readback
                          (409 postcondition_unconfirmed on any disagreement).
                          The sibling set is derived INDEPENDENTLY before the swap
                          (bounded gym-scoped month read + the same
                          media_swap.sibling_rows grouping rule the handler uses);
                          the handler's siblings_swapped list must match that
                          derived set exactly -- an extra, omitted, duplicated, or
                          target-row sibling is 409 postcondition_unconfirmed, and
                          a store that cannot support the derivation fails closed.
    requeue_failed_row    succeeds only when an independent get_row readback shows
                          the row at status approved with no post id.
    restage_month         a background job: GET the job for its terminal status and
                          real per-step counts; its result carries captured_at and
                          a postcondition_verified that is true ONLY when an
                          independent before/after comparison of the gym-scoped
                          calendar rows across the requested span (a bounded
                          list_month readback) confirms the build's claimed
                          upserted/deleted counts exactly, at least one row was
                          actually written, and no pre-existing row changed. The
                          builder's own ok/upserted claims are never the proof;
                          unavailable or disagreeing evidence leaves the terminal
                          job unverified with an evidence_note.
  Evidence that is unavailable is said so in the result; success fields are never
  invented.

  Catalog (`args` keys):
    resend_connect_link     {}                 owner DM with a fresh connect link (forced)
    reset_recreate_budget   {}                 zero this gym's month deny/recreate spend
    release_denied_assets   {}                 gym_media_selector.observe_denials sweep
    swap_media              {"row_id": ...}    portal_social.handle_swap_media (free swap)
    requeue_failed_row      {"row_id": ...}    calendar row failed -> approved (GBP lane)
    restage_month           {"days": <=31, "start_date"?: "YYYY-MM-DD",
                             "render_budget"?: <=60}   background job: prerender ->
                             observe_denials -> build_client_month (PR #98 recipe)

  NOT in the catalog, 403 org_floor by name: anything billing/Stripe, pixel/CAPI, ad
  budget, targeting, or deleting a published post. Unknown names are 404, never run.

  Every accepted call writes a support_messages SYSTEM row on the ticket
  ("OPS ACTION <name> by fixer: <summary>", kind escalation so the portal hides it from the
  client, delivery_status null so no outbox posts it) and prints one AUDIT log line.

WHERE IT RUNS. Two Echo services share this repo: `echo-intake-web` (R2 only, NO /data
volume) and the `echo` worker (the volume: content_library, brand_voice, the sqlite kv
that gym_media_use and the recreate budget live in). intake_web mounts these routes on
the web service; connect_web mounts the same routes inside the worker (it already serves
HTTP there on AGENT_CONNECT_PORT). Actions that need the volume (release_denied_assets,
restage_month) answer 503 volume_unavailable on a host without it and say which host to
call instead, rather than pretending. `python -m agent ops-action ...` runs the same
catalog from a shell on the worker.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

SECRET_ENV = "FIXER_OPS_SECRET"
HEADER = "X-Fixer-Ops-Secret"
ACTOR = "fixer"
ROUTE_PREFIX = "/ops/actions"
MAX_BODY_BYTES = 64 * 1024
MAX_DAYS = 31
MAX_RENDER_BUDGET = 60
DEFAULT_RENDER_BUDGET = 60
RESTAGE_DEADLINE_SEC = 3 * 3600   # PR #98: 56 clips is 15-45 min; hard ceiling ~3h
MAX_JOBS_KEPT = 200

_GYM_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
_ROW_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_TICKET_ID = re.compile(r"^[A-Za-z0-9_-]{4,80}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$")
_BUSINESS_UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
_BUSINESS_REQUEST_KEY = re.compile(r"^[0-9a-f]{64}$")
_BUSINESS_RELEASE_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BUSINESS_ROW_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_BUSINESS_STATUS = re.compile(r"^[a-z_]{2,32}$")
_BUSINESS_FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{3,200}$")
BUSINESS_EVIDENCE_PATH = ROUTE_PREFIX + "/business-evidence/observe"
BUSINESS_EVIDENCE_CONTRACT = "echo-business-evidence-v1"
_BUSINESS_FIELDS = frozenset({
    "schema_version", "contract_version", "ticket_id", "client_id",
    "request_key", "merged_sha", "check_id", "params",
})

# Named so a FIXER that asks for one of these gets a 403 that says WHY, not a 404 that
# reads like a typo. Nothing here has an implementation and nothing here may get one
# without Blake's explicit approval (org instructions: billing, pixel/CAPI, budget over
# 20%, targeting over 3 ad sets; plus deleting anything already published).
ORG_FLOOR_ACTIONS = frozenset({
    "charge_card", "refund", "issue_refund", "change_billing", "update_billing",
    "cancel_subscription", "update_stripe", "change_price", "set_price",
    "set_pixel", "update_pixel", "update_capi", "set_capi", "change_ad_budget",
    "set_ad_budget", "increase_budget", "decrease_budget", "change_targeting",
    "set_targeting", "update_audience", "delete_published_post", "delete_post",
    "unpublish_post", "remove_post",
})


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _business_params_valid(check_id, params):
    if not isinstance(params, dict):
        return False
    if check_id == "calendar_row_status":
        return (set(params) == {"row_id", "expected_status"}
                and isinstance(params.get("row_id"), str)
                and _BUSINESS_ROW_ID.fullmatch(params["row_id"]) is not None
                and isinstance(params.get("expected_status"), str)
                and _BUSINESS_STATUS.fullmatch(params["expected_status"]) is not None)
    if check_id == "forward_book_grade_at_least":
        value = params.get("min_total")
        return (set(params) == {"min_total"} and isinstance(value, int)
                and not isinstance(value, bool) and 0 <= value <= 100)
    if check_id == "media_source_active":
        return (set(params) == {"folder_id"}
                and isinstance(params.get("folder_id"), str)
                and _BUSINESS_FOLDER_ID.fullmatch(params["folder_id"]) is not None)
    if check_id == "media_swap_candidate_available":
        return set(params) == {"min_count"} and type(params.get("min_count")) is int and params["min_count"] == 1
    if check_id == "media_swap_completed":
        return (set(params) == {"reservation_key", "row_id"}
                and isinstance(params.get("reservation_key"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", params["reservation_key"]) is not None
                and isinstance(params.get("row_id"), str)
                and _BUSINESS_ROW_ID.fullmatch(params["row_id"]) is not None)
    return False


def _business_reader(deps):
    configured = deps.get("business_evidence")
    if isinstance(configured, dict) and callable(configured.get("read")):
        return configured["read"]
    bus = deps.get("bus")
    if callable(getattr(bus, "_get", None)):
        return bus._get
    # The production PostgREST reader lives with the other bounded, read-only
    # FIXER evidence adapters.  fixer_business_evidence deliberately owns no
    # transport or environment access of its own.
    from .fixer_evidence import read_rest
    return read_rest


def _business_request_key(read, ticket):
    """Recompute Scout's requester-message SHA from a complete bounded read."""
    try:
        rows = read("support_messages", {
            "ticket_id": f"eq.{ticket['id']}", "direction": "eq.inbound",
            "select": "id,ticket_id,created_at,body,author_type,direction,attachments",
            "order": "created_at.asc,id.asc", "limit": "1000"})
    except Exception:  # noqa: BLE001
        return None
    if (not isinstance(rows, list) or len(rows) >= 1000
            or any(not isinstance(row, dict) for row in rows)):
        return None
    requester = []
    for message in rows:
        if message.get("ticket_id") != ticket["id"] or message.get("direction") != "inbound":
            return None
        attachments = message.get("attachments") or {}
        if not isinstance(attachments, dict):
            return None
        author = message.get("author_type")
        client = author == "client" or not author
        operator_mention = (author in ("staff", "blake")
                            and attachments.get("surface") == "mention"
                            and attachments.get("identity_reason") == "operator list")
        if client or operator_mention:
            requester.append([message.get("id"), message.get("created_at"),
                              message.get("body")])
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    requester.sort(key=encode)
    identity = requester or [ticket.get("id"), ticket.get("created_at"),
                             ticket.get("raw_text")]
    return hashlib.sha256(encode(identity).encode("utf-8")).hexdigest()


def _run_business_evidence(raw_body, deps, now=None):
    """Validate Scout's exact pointer contract and run one read-only Echo check."""
    if raw_body and len(raw_body) > MAX_BODY_BYTES:
        return 413, {"error": "too_large"}
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:  # noqa: BLE001 - malformed transport input
        return 400, {"error": "bad_request", "detail": "invalid JSON"}
    if not isinstance(body, dict) or set(body) != _BUSINESS_FIELDS:
        return 400, {"error": "bad_request", "detail": "invalid business evidence schema"}
    if body.get("schema_version") != 1 or body.get("contract_version") != BUSINESS_EVIDENCE_CONTRACT:
        return 400, {"error": "bad_request", "detail": "unsupported business evidence contract"}
    ticket_id = body.get("ticket_id")
    client_id = body.get("client_id")
    request_key = body.get("request_key")
    merged_sha = body.get("merged_sha")
    check_id = body.get("check_id")
    params = body.get("params")
    if (not isinstance(ticket_id, str) or not _BUSINESS_UUID.fullmatch(ticket_id)
            or not isinstance(client_id, str) or not _BUSINESS_UUID.fullmatch(client_id)
            or not isinstance(request_key, str) or not _BUSINESS_REQUEST_KEY.fullmatch(request_key)
            or not isinstance(merged_sha, str) or not _BUSINESS_RELEASE_SHA.fullmatch(merged_sha)
            or not isinstance(check_id, str) or not _business_params_valid(check_id, params)):
        return 400, {"error": "bad_request", "detail": "invalid business evidence identity or check"}

    read = _business_reader(deps)
    try:
        rows = read("support_tickets", {
            "id": f"eq.{ticket_id}",
            "select": "id,product,client_id,created_at,raw_text", "limit": "2"})
    except Exception:  # noqa: BLE001 - source errors never become evidence
        return 503, {"error": "evidence_unavailable"}
    if not isinstance(rows, list) or len(rows) > 2 or any(not isinstance(row, dict) for row in rows):
        return 503, {"error": "evidence_unavailable"}
    if not rows:
        return 404, {"error": "ticket_not_found"}
    if len(rows) != 1 or rows[0].get("id") != ticket_id:
        return 409, {"error": "ticket_identity_unconfirmed"}
    ticket = rows[0]
    if ticket.get("product") != "echo" or ticket.get("client_id") != client_id:
        return 409, {"error": "ticket_tenant_mismatch"}
    current_request_key = _business_request_key(read, ticket)
    if current_request_key is None:
        return 503, {"error": "evidence_unavailable"}
    if current_request_key != request_key:
        return 409, {"error": "request_identity_mismatch"}

    from . import fixer_business_evidence as evidence
    receipt_read = None
    if check_id == "media_swap_completed":
        from . import fixer_ops_receipts as receipts
        receipt_store = deps.get("receipt_store")
        if receipt_store is None:
            receipt_store = receipts.default_store()
        receipt_read = lambda key, echo_key: receipts.get_receipt(receipt_store, key, echo_key)
    record = evidence.observe(
        check_id, gym_key=client_id, request_key=request_key,
        merged_sha=merged_sha, params=params,
        deps={"read": read, "receipt_read": receipt_read},
        ticket_id=ticket_id, now=now)
    if not isinstance(record, dict):
        return 503, {"error": "evidence_unavailable"}
    # Explicit response allowlist. Params are expectations, not proof; returning
    # the exact validated values lets Scout bind the observation to its pointer.
    keys = ("schema_version", "source", "check_id", "gym_key", "request_key",
            "merged_sha", "captured_at", "outcome", "verified",
            "symptom_resolved", "evidence", "reason")
    if any(key not in record for key in keys):
        return 503, {"error": "evidence_unavailable"}
    return 200, {**{key: record[key] for key in keys}, "params": dict(params)}


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------

def secret_configured():
    return bool(os.environ.get(SECRET_ENV, "").strip())


def authorize(headers_get):
    """(status, body) when the request must be refused, else None. 503 when the secret is
    not configured on this host (nothing can authenticate; say so), 401 otherwise."""
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        return 503, {"error": "ops_secret_unset",
                     "detail": f"{SECRET_ENV} is not set on this service"}
    supplied = str(headers_get(HEADER, "") or headers_get(HEADER.lower(), "") or "").strip()
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"),
                                               secret.encode("utf-8")):
        return 401, {"error": "unauthorized"}
    return None


# --------------------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionSpec:
    name: str
    run: object                    # (ctx) -> (status, result_dict)
    needs_volume: bool = False
    background: bool = False
    args_schema: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class Ctx:
    gym_key: str
    ticket_id: str
    args: dict
    deps: dict                     # injectables (tests): see _default_deps
    log: object = print


def _default_deps():
    return {}


def _dep(ctx, name, default_factory):
    if name in ctx.deps and ctx.deps[name] is not None:
        return ctx.deps[name]
    return default_factory()


def volume_available():
    """True when this process has the worker's data volume (the sqlite kv, the media
    libraries, the brand bibles). db.kv_is_durable is the existing single test for it."""
    try:
        from . import db as _db
        return bool(_db.kv_is_durable())
    except Exception:  # noqa: BLE001
        return False


# -- resend_connect_link -----------------------------------------------------------------

def _gym_row_for(ctx):
    """(gym_id, gym_name) for an Echo account key from the portal's own tables, or
    (None, None). Injectable as deps['gym_lookup'] -> (gym_id, name)."""
    lookup = ctx.deps.get("gym_lookup")
    if lookup is not None:
        return lookup(ctx.gym_key)
    from . import intake_web as _iw
    row = _iw._supabase_token_gym(ctx.gym_key)
    if not row or not row.get("gym_id"):
        return None, None
    gym_id = str(row["gym_id"])
    name = ""
    try:
        from .slack_convo.bus import Bus
        rows = Bus()._get("gyms", {"id": f"eq.{gym_id}", "select": "name", "limit": "1"})
        name = str((rows or [{}])[0].get("name") or "").strip()
    except Exception:  # noqa: BLE001 - a name miss falls back to the key below
        name = ""
    return gym_id, (name or ctx.gym_key)


def _is_echo_client(ctx, gym_id):
    """Round 2 (MINOR): notify_new_gym(force=True) DM'd 36 non-clients in a live incident, so
    the resend is gated on the gym actually being an Echo client. The predicate is the ONE
    Echo-client universe, `echo_clients.is_echo_client` (D73: echo_gym_settings rows, by
    gym id or base key) -- the same gate notify_new_gym itself applies since PR #108, so a
    forced resend cannot reach a gym the automatic send would refuse. FAILS CLOSED: unknown,
    no creds, any error -> not a client, no DM. Injectable as deps['is_echo_client'] -> bool."""
    pred = ctx.deps.get("is_echo_client")
    if pred is not None:
        try:
            return bool(pred(gym_id))
        except Exception:  # noqa: BLE001
            return False
    try:
        from . import echo_clients
        return bool(echo_clients.is_echo_client(gym_id)
                    or echo_clients.is_echo_client(ctx.gym_key))
    except Exception:  # noqa: BLE001 - unverifiable means NOT a client
        return False


def _run_resend_connect_link(ctx):
    gym_id, name = _gym_row_for(ctx)
    if not gym_id:
        return 404, {"error": "gym_not_found", "gym_key": ctx.gym_key}
    if not _is_echo_client(ctx, gym_id):
        return 403, {"error": "not_echo_client", "gym_key": ctx.gym_key, "gym_id": gym_id,
                     "summary": ("connect link NOT sent: this gym has no echo_gym_settings "
                                 "row (not an Echo client, or unverifiable); refused")}
    notify = _dep(ctx, "notify_new_gym", lambda: __import__(
        "agent.connect_link_notify", fromlist=["notify_new_gym"]).notify_new_gym)
    alerts = []
    raw = notify(ctx.gym_key, gym_id, name, force=True, alert=alerts.append,
                 return_receipt=True)
    sent, identity = _send_evidence(raw)
    captured = _now_iso()
    if sent and identity is not None:
        return 200, {"sent": True, "gym_id": gym_id, "gym_name": name,
                     "sent_message_identity": identity,
                     "postcondition_verified": True,
                     "captured_at": captured,
                     "summary": f"connect link re-sent to the owner of {name}"}
    if sent:
        # A bare no-exception return is not delivery proof (the contract: actual sent
        # state is proven by provider/message identity from the send call, nothing
        # less). Null the sent claim rather than assert an unproven success.
        return 200, {"sent": None, "gym_id": gym_id, "gym_name": name,
                     "sent_message_identity": None,
                     "postcondition_verified": False,
                     "captured_at": captured,
                     "evidence_note": ("the send call reported no failure but returned "
                                       "no provider message identity; actual sent "
                                       "state is unproven"),
                     "summary": (f"connect link resend for {name}: the send call did "
                                 "not fail, but it returned no provider message "
                                 "identity; sent state unproven")}
    return 409, {"error": "not_sent", "sent": False, "gym_id": gym_id, "gym_name": name,
                 "captured_at": captured,
                 "detail": (alerts[-1] if alerts else "notify_new_gym declined to send"),
                 "summary": "connect link NOT sent: " + (alerts[-1] if alerts else "declined")}


def _send_evidence(raw):
    """(sent, message_identity) from a send call's return.

    A send that can prove itself returns a mapping carrying the provider's own
    message identity ("message_identity", or provider/channel/ts/message_id fields);
    that identity is the readback-grade evidence. A bare truthy/falsy return proves
    nothing beyond no-exception: sent is believed but identity stays None and the
    caller must not claim verification.
    """
    if isinstance(raw, dict):
        sent = bool(raw.get("sent"))
        identity = raw.get("message_identity")
        if not isinstance(identity, dict):
            identity = {"provider": raw.get("provider") or "slack",
                        "channel": raw.get("channel"),
                        "ts": raw.get("ts"),
                        "message_id": raw.get("message_id")}
        provider = identity.get("provider")
        channel = identity.get("channel")
        ts = identity.get("ts")
        message_id = identity.get("message_id")
        if (provider != "slack" or not isinstance(channel, str) or not channel
                or not ((isinstance(ts, str) and ts)
                        or (isinstance(message_id, str) and message_id))):
            identity = None
        else:
            identity = {"provider": provider, "channel": channel,
                        "ts": ts, "message_id": message_id}
        return sent, identity
    return bool(raw), None


def _resend_connect_link_readiness(gym_key, deps):
    """Read only whether the portal owner prerequisite is currently satisfied.

    This intentionally does not call notify_new_gym: its force mode is an operator
    resend and must never be used to poll for a portal-data repair.
    """
    if not _GYM_KEY.match(gym_key):
        return 400, {"error": "bad_request", "detail": "gym_key required (account key)"}
    ctx = Ctx(gym_key=gym_key, ticket_id="", args={}, deps=deps)
    gym_id, _name = _gym_row_for(ctx)
    if not gym_id:
        return 404, {"error": "gym_not_found", "gym_key": gym_key}
    if not _is_echo_client(ctx, gym_id):
        return 403, {"error": "not_echo_client", "gym_key": gym_key, "gym_id": gym_id}
    readiness = _dep(ctx, "owner_readiness", lambda: __import__(
        "agent.connect_link_notify", fromlist=["owner_readiness"]).owner_readiness)
    try:
        category = readiness(gym_id)
    except Exception:  # noqa: BLE001 - never mislabel an outage as missing
        category = "portal-unavailable"
    if category not in {"ready", "missing", "ambiguous", "user-email-missing",
                        "portal-unavailable"}:
        category = "portal-unavailable"
    return 200, {"category": category}


# -- reset_recreate_budget ---------------------------------------------------------------

def _run_reset_recreate_budget(ctx):
    reset = _dep(ctx, "reset_recreate_budget", lambda: __import__(
        "agent.portal_social", fromlist=["reset_recreate_budget"]).reset_recreate_budget)
    out = reset(ctx.gym_key)
    before = (out or {}).get("before") or {}
    after = (out or {}).get("after") or {}
    # before/after are the wrapped function's own kv re-reads around its write, not
    # the write's return value; the comparison below is the postcondition readback.
    captured = _now_iso()
    if after.get("used") != 0 or after.get("remaining") != after.get("limit"):
        return 409, {"error": "postcondition_unconfirmed", "before": before,
                     "after": after, "captured_at": captured,
                     "summary": "recreate budget reset was not confirmed"}
    return 200, {**(out or {}),
                 "postcondition_verified": True,
                 "captured_at": captured,
                 "summary": (f"recreate budget {before.get('used', '?')} used -> "
                             f"{after.get('used', '?')} used "
                             f"({after.get('remaining', '?')} of {after.get('limit', '?')} left)")}


# -- release_denied_assets ---------------------------------------------------------------

class _ReadbackUnavailable(Exception):
    """A verification read failed, or returned a partial or foreign payload. The
    callers catch broadly: evidence gathering never endangers the action itself,
    it only withholds postcondition_verified."""


# Well-formed identity charset for ledger/asset/calendar ids (uuids, base keys,
# asset ids all fit). Anything outside it is not an id Echo wrote.
_LEDGER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DATE_STRICT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _strict_date(value):
    """A date for a strict YYYY-MM-DD REAL calendar date, else None. Never raises."""
    from datetime import date as _date
    s = value if isinstance(value, str) else ""
    if not _DATE_STRICT.match(s):
        return None
    try:
        return _date.fromisoformat(s)
    except ValueError:
        return None


def _strict_count(value):
    """A counter as a non-bool int in range, else None. No coercion: a bool, a
    string, a float, None, or an out-of-range value is a coercion fault, and a
    fault is evidence-unavailable -- never a coerced guess."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value < 10 ** 9:
        return None
    return value


def _strict_iso_or_none(value):
    """(value, ok): ok when value is None or an ISO-8601 parseable timestamp
    string; anything else is a coercion fault."""
    if value is None:
        return None, True
    if not isinstance(value, str) or not value.strip():
        return None, False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None, False
    return value, True


def _strict_use_record(rec, base):
    """One gym_media_use record, strictly parsed, or None when malformed. Every
    field the verification relies on is validated: gym id (exact expected tenant,
    well-formed), asset id (well-formed, nonempty), rolled_back (a real bool),
    prev_used_count (a real in-range int), prev_last_used_at (None or ISO). A
    malformed record is evidence-unavailable -- skipping it could hide a missed
    rollback."""
    if not isinstance(rec, dict):
        return None
    gym = rec.get("gym_id")
    if not isinstance(gym, str) or gym != base or not _LEDGER_ID.match(gym):
        return None
    asset_id = rec.get("asset_id")
    if not isinstance(asset_id, str) or not _LEDGER_ID.match(asset_id):
        return None
    rolled_back = rec.get("rolled_back")
    if not isinstance(rolled_back, bool):
        return None
    prev_count = _strict_count(rec.get("prev_used_count"))
    if prev_count is None:
        return None
    prev_last, ok = _strict_iso_or_none(rec.get("prev_last_used_at"))
    if not ok:
        return None
    return {"asset_id": asset_id, "rolled_back": rolled_back,
            "prev_used_count": prev_count, "prev_last_used_at": prev_last}


def _strict_denial_row(row, base, post_date):
    """(status_lower, asset_id_or_empty) for one content_calendar denial-signal
    row, strictly parsed, or None when the row is malformed."""
    if not isinstance(row, dict):
        return None
    rid = row.get("id")
    if not isinstance(rid, str) or not _LEDGER_ID.match(rid):
        return None
    # The query shape is tenant and date scoped, but a response is evidence only
    # if it also carries those fields and they agree.  A proxy/cache that leaked
    # a neighbouring row must never make this tenant's rollback look complete.
    if row.get("gym_id") != base or row.get("post_date") != post_date:
        return None
    status = row.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    asset = row.get("source_media_asset_id")
    if asset is None:
        asset = ""
    elif isinstance(asset, str):
        asset = asset.strip()
        if asset and not _LEDGER_ID.match(asset):
            return None
    else:
        return None
    return status.strip().lower(), asset


def _default_denial_ledger_read(gym_key):
    """This gym's gym_media_use kv records as (post_date, [record, ...]) pairs, or
    None on any read failure OR any malformed entry (unparseable JSON, a
    non-dict record, a key whose date is not a strict real calendar date). The
    selector's own lenient parse silently skips unreadable values -- right for a
    sweep that does no harm by standing down, wrong for a verification, where a
    skipped value could hide a missed rollback. Bounded: one range scan over the
    exact key prefix plus an exact startswith guard, so no other tenant's ledger
    rows are ever loaded into a tenant-scoped verification."""
    from . import db
    from . import gym_media_selector as selector
    prefix = f"gym_media_use:{selector.base_gym_key(gym_key)}:"
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM kv WHERE key >= ? AND key < ?",
                (prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1))).fetchall()
    except Exception:  # noqa: BLE001 - an unreadable ledger is unavailable evidence
        return None
    out = []
    for r in rows:
        key, value = r["key"], r["value"]
        if not key.startswith(prefix):
            continue
        post_date = key.split(":", 2)[2]
        if _strict_date(post_date) is None:
            return None
        try:
            parsed = json.loads(value or "[]")
        except Exception:  # noqa: BLE001 - unreadable JSON is unavailable evidence
            return None
        if isinstance(parsed, dict):
            parsed = [parsed] if parsed else []
        if not isinstance(parsed, list) or any(not isinstance(rec, dict) for rec in parsed):
            return None
        if parsed:
            out.append((post_date, parsed))
    return out


def _default_denial_rows_read(gym_key, post_date):
    """Strict twin of gym_media_selector._default_fetch_rows: the same bounded
    gym+date content_calendar read, but a fetch failure RAISES instead of
    returning [], and every row is strictly parsed (a malformed row raises too).
    The sweep may fail silent (safe: it then does nothing); a verification that
    cannot tell 'no rows' from 'store down' or from 'unreadable row' would be
    certifying on silence."""
    from . import config
    from . import gym_media_selector as selector
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        raise _ReadbackUnavailable("calendar readback plane is not configured")
    import requests  # lazy, repo pattern
    r = requests.get(
        f"{url}/rest/v1/content_calendar",
        params={"gym_id": f"eq.{gym_key}", "post_date": f"eq.{post_date}",
                "select": "id,gym_id,post_date,status,pillar,source_media_asset_id"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30)
    if r.status_code >= 400:
        raise _ReadbackUnavailable(f"calendar readback failed: {r.status_code}")
    rows = r.json()
    if not isinstance(rows, list):
        raise _ReadbackUnavailable("calendar readback returned a non-list payload")
    if any(_strict_denial_row(row, gym_key, post_date) is None for row in rows):
        raise _ReadbackUnavailable("calendar readback returned a malformed row")
    return rows


def _default_asset_read(asset_id):
    """One media_asset row for the rollback counter readback; raises when the
    store is not there to answer."""
    from . import gym_media_index as _idx
    store = _idx.default_store()
    if not store.available():
        raise _ReadbackUnavailable("media asset store is unavailable")
    return store.get_asset(asset_id)


def _denial_rollback_worklist(gym_key, *, ledger_read, rows_read):
    """{post_date: [strictly-parsed un-rolled use-records]} the calendar currently
    demands rolled back for ONE gym, derived from independent bounded reads taken
    BEFORE the sweep so the answer cannot be shaped by the sweep's own claims.
    Re-implements observe_denials' decision rule read-only (denied gym-media row,
    no live gym-media row on the date). EVERY record relied on is strictly parsed;
    any read failure or malformed record raises: fail closed."""
    from . import gym_media_selector as selector
    ledger = ledger_read(gym_key)
    if ledger is None:
        raise _ReadbackUnavailable("use-ledger readback failed")
    work = {}
    for post_date, records in ledger:
        if not isinstance(post_date, str) or _strict_date(post_date) is None:
            raise _ReadbackUnavailable("use-ledger readback returned a malformed date")
        if not isinstance(records, list):
            raise _ReadbackUnavailable("use-ledger readback returned a malformed record list")
        parsed = []
        for rec in records:
            clean = _strict_use_record(rec, gym_key)
            if clean is None:
                raise _ReadbackUnavailable("use-ledger readback returned a malformed record")
            parsed.append(clean)
        unrolled = [r for r in parsed if not r["rolled_back"]]
        if not unrolled:
            continue
        rows = rows_read(gym_key, post_date) or []
        if not isinstance(rows, list):
            raise _ReadbackUnavailable("calendar readback returned a non-list payload")
        mine = []
        for row in rows:
            clean_row = _strict_denial_row(row, gym_key, post_date)
            if clean_row is None:
                raise _ReadbackUnavailable("calendar readback returned a malformed row")
            if clean_row[1]:
                mine.append(clean_row)
        if not mine:
            continue
        denied_assets = {asset for status, asset in mine if status == "denied"}
        live_assets = {asset for status, asset in mine
                       if status in selector._LIVE_STATUSES}
        staged_assets = {r["asset_id"] for r in unrolled}
        if denied_assets and not live_assets:
            # The legacy sweep is date-scoped, but a verified operator result
            # needs a one-for-one relationship between the denied calendar rows
            # and the use records it will restore.  Without this, denial of A
            # could roll back B merely because both were staged on the same date.
            if staged_assets != denied_assets:
                raise _ReadbackUnavailable(
                    "denial ledger assets do not exactly match denied calendar assets")
            work[post_date] = unrolled
    return work


def _confirm_denial_rollbacks(base, expected, sweep_out, *, ledger_read, asset_read):
    """(verified, evidence_note) -- the independent POST-sweep readback.

    verified is True ONLY when the sweep claimed exactly the rollback dates the
    pre-sweep derivation found, a fresh ledger re-read shows every one of those
    records rolled_back, AND a fresh per-asset re-read shows the EXACT requested
    asset (id equality -- a different or neighbouring asset is not evidence), this
    gym's own, with usage counters back at the values the ledger recorded before
    staging. Any read failure, malformed record, coercion fault, or disagreement
    is (False, note): the sweep's own counts never certify themselves. A
    no-rollback sweep verifies nothing -- a release that released nothing is not
    a release."""
    claimed = sweep_out.get("rolled_back")
    if _strict_count(claimed) is None:
        return False, "the sweep reported no integer rolled_back count to compare"
    if claimed != len(expected):
        return False, (f"the sweep claims {claimed} rollback date(s) but the "
                       f"independent pre-sweep read found {len(expected)}; the state "
                       "moved mid-flight or the claim is wrong")
    if not expected:
        return False, ("no denied asset awaited rollback and the sweep rolled none "
                       "back; a zero-rollback sweep is not a release and verifies "
                       "nothing")
    after = ledger_read(base)
    if after is None:
        return False, "post-sweep use-ledger readback failed"
    after_by_date = {}
    for post_date, records in after:
        if not isinstance(post_date, str) or _strict_date(post_date) is None \
                or not isinstance(records, list):
            return False, "post-sweep use-ledger readback returned a malformed entry"
        for rec in records:
            clean = _strict_use_record(rec, base)
            if clean is None:
                return False, "post-sweep use-ledger readback returned a malformed record"
            after_by_date.setdefault(post_date, {})[clean["asset_id"]] = clean
    for post_date, records in expected.items():
        current = after_by_date.get(post_date, {})
        for rec in records:
            asset_id = rec["asset_id"]
            now = current.get(asset_id)
            if now is None or now["rolled_back"] is not True:
                return False, (f"asset {asset_id} on {post_date} is not rolled back "
                               "in the post-sweep ledger read")
            try:
                asset = asset_read(asset_id)
            except Exception:  # noqa: BLE001 - an unreadable asset is partial evidence
                return False, f"asset {asset_id} counter readback failed"
            if not isinstance(asset, dict) or str(asset.get("id") or "") != asset_id:
                return False, (f"asset readback for {asset_id} returned a different "
                               "or missing asset")
            if str(asset.get("gym_id") or "") != base:
                return False, (f"asset {asset_id} is missing from the readback or is "
                               "not this gym's")
            used = _strict_count(asset.get("used_count"))
            last, ok = _strict_iso_or_none(asset.get("last_used_at"))
            if used is None or not ok:
                return False, (f"asset {asset_id} counters could not be strictly "
                               "parsed")
            if (used != rec["prev_used_count"]
                    or last != rec["prev_last_used_at"]):
                return False, (f"asset {asset_id} usage counters do not match the "
                               "rolled-back state the ledger recorded")
    return True, None


def _observe_denials_for_gym(gym_key, observe):
    """Constrain the global ledger sweep's only path to rollback_use.

    observe_denials has no gym argument. It iterates every tenant's use records,
    but rolls back only after fetch_rows supplies a denied calendar row. Returning
    no rows for other gyms prevents a tenant-scoped ops request from mutating them.
    """
    from . import gym_media_selector as selector
    checked = 0

    def fetch_scoped(gym_id, post_date):
        nonlocal checked
        if gym_id != gym_key:
            return []
        checked += 1
        return selector._default_fetch_rows(gym_id, post_date)

    out = observe(fetch_rows=fetch_scoped) or {}
    # observe_denials counts every ledger key it scanned, including other gyms.
    # Report only the target gym's calendar probes in this tenant-scoped result.
    return {**out, "checked": checked}


def _run_release_denied_assets(ctx):
    observe = _dep(ctx, "observe_denials", lambda: __import__(
        "agent.gym_media_selector", fromlist=["observe_denials"]).observe_denials)
    from . import gym_media_selector as selector
    base = selector.base_gym_key(ctx.gym_key)
    ledger_read = _dep(ctx, "denial_ledger_read", lambda: _default_denial_ledger_read)
    rows_read = _dep(ctx, "denial_rows_read", lambda: _default_denial_rows_read)
    asset_read = _dep(ctx, "asset_read", lambda: _default_asset_read)
    # Independent pre-sweep derivation, bounded to this gym. An evidence fault
    # never blocks the sweep; it only withholds verification.
    try:
        expected = _denial_rollback_worklist(base, ledger_read=ledger_read,
                                             rows_read=rows_read)
        derivation_note = ""
    except Exception as e:  # noqa: BLE001
        expected = None
        derivation_note = (f"independent pre-sweep readback unavailable: "
                           f"{type(e).__name__}: {str(e)[:160]}")
    out = _observe_denials_for_gym(ctx.gym_key, observe)
    # Echo the sweep's measured counts and nothing else: an upstream success flag is
    # stripped, never amplified -- a sweep that rolled nothing back is not a release,
    # whatever the wrapped function claims about itself. Verification comes only
    # from the independent ledger/calendar/asset re-probe, never from `out`.
    result = {k: v for k, v in out.items()
              if k not in ("released", "postcondition_verified", "captured_at")}
    if expected is None:
        verified, note = False, derivation_note
    else:
        verified, note = _confirm_denial_rollbacks(
            base, expected, out, ledger_read=ledger_read, asset_read=asset_read)
    result["postcondition_verified"] = verified
    if not verified:
        result["evidence_note"] = note
    result["captured_at"] = _now_iso()
    result["summary"] = (f"deny sweep checked {out.get('checked', 0)} date(s), "
                         f"rolled back {out.get('rolled_back', 0)} asset(s)")
    return 200, result


# -- swap_media --------------------------------------------------------------------------

def _row_id(ctx):
    rid = str((ctx.args or {}).get("row_id") or "").strip()
    if not _ROW_ID.match(rid):
        return None
    return rid


def _derive_swap_sibling_ids(store, gym_key, rid):
    """The set of sibling row ids the swap SHOULD move, derived BEFORE the swap
    from independent bounded gym-scoped reads, using the SAME grouping rule
    handle_swap_media itself applies (media_swap.sibling_rows over the clicked
    row's own month: same gym + same post_date + an IG/FB feed/story row + the
    same Drive asset id or raw media key). The handler's own siblings_swapped
    list is a self-report and is never the source of truth. Raises
    _ReadbackUnavailable on any read failure or a partial, malformed, or
    cross-tenant payload."""
    row = store.get_row(gym_key, rid)
    if not isinstance(row, dict) or str(row.get("id") or "") != rid:
        raise _ReadbackUnavailable("target row unreadable for sibling derivation")
    if str(row.get("gym_id") or "") != gym_key:
        raise _ReadbackUnavailable("target row crossed the tenant scope")
    pd = _strict_date(row.get("post_date"))
    if pd is None:
        raise _ReadbackUnavailable("target row carries no strict post_date")
    rows = _read_month_rows(store, gym_key, pd.isoformat()[:7])
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            raise _ReadbackUnavailable("sibling derivation read returned a partial row")
        if str(r.get("gym_id") or "") != gym_key:
            raise _ReadbackUnavailable("sibling derivation read crossed the tenant scope")
    from . import media_swap as _ms
    # portal_social moves only pending / coach_review siblings.  Approved and
    # live siblings are intentionally left in place, so including them in the
    # expected set would turn a correct, protected swap into a false failure.
    siblings = [s for s in _ms.sibling_rows(
        row, rows, lib=_ms.library_path_for(gym_key))
        if str(s.get("status") or "").lower() in ("pending", "coach_review")]
    out = set()
    for s in siblings:
        sid = str(s.get("id") or "")
        if not _ROW_ID.match(sid) or sid == rid or sid in out:
            raise _ReadbackUnavailable("grouping rule returned a malformed sibling set")
        out.add(sid)
    return out, dict(row)


def _run_swap_media(ctx):
    rid = _row_id(ctx)
    if not rid:
        return 400, {"error": "bad_request", "detail": "args.row_id required"}
    handler = _dep(ctx, "handle_swap_media", lambda: __import__(
        "agent.portal_social", fromlist=["handle_swap_media"]).handle_swap_media)
    # Derive the expected sibling set INDEPENDENTLY, before the swap: the
    # handler's siblings_swapped list is a self-report and is never the source of
    # truth. A store that cannot support the derivation fails closed below.
    store = None
    expected_siblings = None
    before_row = None
    sibling_note = "independent sibling derivation unavailable"
    try:
        store = _dep(ctx, "calendar_store", lambda: __import__(
            "agent.portal_calendar_store", fromlist=["SupabaseCalendarStore"]
        ).SupabaseCalendarStore())
        expected_siblings, before_row = _derive_swap_sibling_ids(store, ctx.gym_key, rid)
    except Exception as e:  # noqa: BLE001 - evidence fault, never a swap blocker
        sibling_note = (f"independent sibling derivation unavailable: "
                        f"{type(e).__name__}: {str(e)[:160]}")
    status, body = handler(ctx.gym_key, rid, f"{ACTOR}:{ctx.ticket_id}")
    body = dict(body or {})
    body["row_id"] = rid
    body.pop("swap_proof", None)  # only our independent readback may create this
    if int(status) == 200 and body.get("ok") is True:
        if expected_siblings is None:
            return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                         "_outcome_unknown": True,
                         "postcondition_verified": False,
                         "captured_at": _now_iso(),
                         "summary": f"swap-media on row {rid}: {sibling_note}"}
        swapped_ids = body.get("siblings_swapped")
        if (not isinstance(swapped_ids, list)
                or any(not isinstance(v, str) or not _ROW_ID.match(v)
                       for v in swapped_ids)
                or len(set(swapped_ids)) != len(swapped_ids)
                or rid in swapped_ids):
            return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                         "_outcome_unknown": True,
                         "postcondition_verified": False,
                         "captured_at": _now_iso(),
                         "summary": (f"swap-media on row {rid}: handler-reported "
                                     "sibling list is malformed")}
        # The handler's returned row is a write response, not an independent proof.
        # A missing or failed readback is ambiguous: the write may already have landed,
        # so report it without invoking the non-idempotent swap a second time.
        expected = body.get("image_public_url")
        try:
            confirmed = store.get_row(ctx.gym_key, rid) if expected else None
        except Exception:  # noqa: BLE001 - readback failure is not proof of rollback
            confirmed = None
        actual = (confirmed or {}).get("image_url")
        display = (confirmed or {}).get("thumbnail_url") or actual
        if (not confirmed or confirmed.get("id") != rid
                or confirmed.get("gym_id") != ctx.gym_key
                or body.get("siblings_left")
                or (body.get("video_url") and actual != body["video_url"])
                or display != body.get("image_public_url")):
            return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                         "_outcome_unknown": True,
                         "postcondition_verified": False,
                         "captured_at": _now_iso(),
                         "summary": f"swap-media on row {rid}: calendar readback unconfirmed"}
        sibling_results = body.get("sibling_results") or []
        if (not isinstance(sibling_results, list)
                or sorted(swapped_ids)
                != sorted(str(v.get("id")) for v in sibling_results
                          if isinstance(v, dict))):
            return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                         "_outcome_unknown": True,
                         "postcondition_verified": False,
                         "captured_at": _now_iso(),
                         "summary": f"swap-media on row {rid}: sibling evidence unconfirmed"}
        for expected_sibling in sibling_results:
            sid = str(expected_sibling.get("id") or "")
            try:
                sibling = store.get_row(ctx.gym_key, sid)
            except Exception:  # noqa: BLE001
                sibling = None
            sibling_actual = (sibling or {}).get("image_url")
            sibling_display = (sibling or {}).get("thumbnail_url") or sibling_actual
            if (not sibling or sibling.get("id") != sid
                    or sibling.get("gym_id") != ctx.gym_key
                    or (expected_sibling.get("video_url")
                        and sibling_actual != expected_sibling.get("video_url"))
                    or sibling_display != expected_sibling.get("image_public_url")):
                return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                             "_outcome_unknown": True,
                             "postcondition_verified": False,
                             "captured_at": _now_iso(),
                             "summary": (f"swap-media on row {rid}: sibling {sid} "
                                         "readback unconfirmed")}
        if set(swapped_ids) != expected_siblings:
            return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                         "_outcome_unknown": True,
                         "postcondition_verified": False,
                         "captured_at": _now_iso(),
                         "summary": (f"swap-media on row {rid}: handler-reported "
                                     "siblings do not match the independently "
                                     "derived set")}
        body["postcondition_verified"] = True
        # The keyed durable receipt carries this independently read before/after
        # identity. Only a changed, still-waiting Drive row with unchanged caption
        # can later satisfy media_swap_completed; other successful swaps remain
        # operational successes without this specific business proof.
        before_image = before_row.get("image_url") if before_row else None
        after_image = confirmed.get("image_url")
        before_caption = before_row.get("caption") if before_row else None
        after_caption = confirmed.get("caption")
        asset_id = confirmed.get("source_media_asset_id")
        if (isinstance(before_image, str) and bool(before_image)
                and isinstance(after_image, str) and bool(after_image)
                and before_image != after_image
                and isinstance(before_caption, str)
                and after_caption == before_caption
                and confirmed.get("status") in ("pending", "coach_review")
                and isinstance(asset_id, str) and bool(asset_id)
                and asset_id != (before_row.get("source_media_asset_id") or "")):
            digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
            body["swap_proof"] = {
                "row_id": rid,
                "before_image_sha256": digest(before_image),
                "after_image_sha256": digest(after_image),
                "caption_sha256": digest(before_caption),
                "before_asset_id": before_row.get("source_media_asset_id") or None,
                "after_asset_id": asset_id,
            }
        body["captured_at"] = _now_iso()
    body["summary"] = (f"swap-media on row {rid}: "
                       + ("ok" if body.get("ok") else f"refused ({body.get('error', status)})"))
    return int(status), body


# -- requeue_failed_row ------------------------------------------------------------------

def _run_requeue_failed_row(ctx):
    rid = _row_id(ctx)
    if not rid:
        return 400, {"error": "bad_request", "detail": "args.row_id required"}
    store = _dep(ctx, "calendar_store", lambda: __import__(
        "agent.portal_calendar_store", fromlist=["SupabaseCalendarStore"]).SupabaseCalendarStore())
    # Ownership first: get_row is gym scoped, so a row on another gym can never load here.
    row = store.get_row(ctx.gym_key, rid)
    if row is None:
        return 404, {"error": "row_not_found", "row_id": rid, "gym_key": ctx.gym_key}
    if str(row.get("status") or "").lower() != "failed":
        return 409, {"error": "row_not_failed", "row_id": rid,
                     "status": row.get("status"),
                     "summary": f"row {rid} is {row.get('status')!r}, not failed; nothing changed"}
    updated = store.requeue_failed_row(rid)
    if updated is None:
        return 409, {"error": "requeue_matched_nothing", "row_id": rid,
                     "summary": (f"row {rid} did not match failed + googlebusiness + no post "
                                 "id at write time; nothing changed")}
    if (updated.get("id") != rid or updated.get("gym_id") != ctx.gym_key
            or updated.get("status") != "approved"):
        return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                     "_outcome_unknown": True,
                     "summary": f"row {rid} requeue response did not match the target gym"}
    confirmed = store.get_row(ctx.gym_key, rid)
    if (not confirmed or confirmed.get("id") != rid
            or confirmed.get("gym_id") != ctx.gym_key
            or confirmed.get("status") != "approved"
            or confirmed.get("late_post_id") is not None):
        return 409, {"error": "postcondition_unconfirmed", "row_id": rid,
                     "_outcome_unknown": True,
                     "captured_at": _now_iso(),
                     "summary": f"row {rid} requeue was not confirmed by calendar readback"}
    return 200, {"row_id": rid, "status": confirmed.get("status"),
                 "post_date": confirmed.get("post_date"),
                 "postcondition_verified": True,
                 "captured_at": _now_iso(),
                 "summary": f"row {rid} requeued: failed -> {confirmed.get('status')}"}


# -- restage_month (background) -----------------------------------------------------------

# The calendar fields the terminal readback fingerprints. Identity, placement, copy,
# media and status: exactly what a rebuild is allowed to write, so a genuine rebuild
# never trips the "pre-existing row changed" check and any outside edit does.
_ROW_FINGERPRINT_FIELDS = ("post_date", "account", "format", "status", "caption",
                           "image_url", "video_url", "slot_index",
                           "source_media_asset_id")

# Every field a row MUST carry before it can be fingerprinted: the mutable fields
# above plus its identity and tenant. A missing KEY means the store selected a
# partial column set and the comparison would run blind on those fields -- that
# rejects the whole readback. A present key with a None value (a story's null
# video_url, a null caption) is a complete row and fingerprints deterministically.
_ROW_REQUIRED_FIELDS = ("id", "gym_id") + _ROW_FINGERPRINT_FIELDS

# Hard bound on followed pages, so a store that hands out cursors forever is
# evidence-unavailable rather than an endless read.
_MAX_MONTH_PAGES = 64
# Supabase/PostgREST's common default response ceiling.  A legacy bare-list
# store gives us no continuation token, so a full-sized page is indistinguishable
# from a truncated response and cannot certify a restage.
_MAX_UNPAGED_MONTH_ROWS = 1000


def _span_months(start, days):
    """Every 'YYYY-MM' the requested [start, start+days) window touches, computed
    from the REQUEST, never from the builder's own report."""
    from datetime import date as _date, timedelta
    first = _date.fromisoformat(str(start)[:10])
    return sorted({(first + timedelta(days=i)).isoformat()[:7]
                   for i in range(max(int(days), 1))})


def _default_calendar_readback_store():
    from . import config
    if not (config.supabase_url() and config.supabase_service_key()):
        raise _ReadbackUnavailable("calendar readback plane is not configured")
    from .portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore()


def _read_month_rows(store, gym_key, month):
    """Every row the store holds for one gym-month, with pagination followed to
    EXHAUSTION. Two store shapes are honoured: a bare list (a non-paginating
    store, the whole month in one answer) and a page dict {"rows": [...],
    "next": <cursor>} fetched with list_month(gym, month, cursor) until no
    cursor remains. A page that announces truncation, a malformed page or
    cursor, or a page sequence that never terminates is _ReadbackUnavailable --
    a truncated page set is unavailable evidence, never a partial comparison."""
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        raise _ReadbackUnavailable("calendar store exposes no bounded month read")
    cursor = None
    rows = []
    for _ in range(_MAX_MONTH_PAGES):
        page = list_month(gym_key, month) if cursor is None \
            else list_month(gym_key, month, cursor)
        if isinstance(page, list):
            if len(page) >= _MAX_UNPAGED_MONTH_ROWS:
                raise _ReadbackUnavailable(
                    "unpaged calendar readback may be truncated at the response limit")
            rows.extend(page)
            return rows
        if not isinstance(page, dict):
            raise _ReadbackUnavailable("calendar readback returned a non-list payload")
        if page.get("truncated"):
            raise _ReadbackUnavailable("calendar readback reported a truncated page set")
        chunk = page.get("rows")
        if not isinstance(chunk, list):
            raise _ReadbackUnavailable("paginated calendar readback returned a malformed page")
        rows.extend(chunk)
        cursor = page.get("next") or page.get("next_cursor") or page.get("next_page")
        if not cursor:
            return rows
        if not isinstance(cursor, str):
            raise _ReadbackUnavailable("paginated calendar readback returned a malformed cursor")
    raise _ReadbackUnavailable("paginated calendar readback did not terminate")


def _calendar_snapshot(store, gym_key, months, *, first=None, last=None):
    """{row_id: fingerprint} for ONE gym's in-span calendar rows across `months`,
    via pagination-complete bounded month reads. Every returned row is strictly
    validated: all required fields present, well-formed id, exact tenant, and a
    strict real post_date inside one of the REQUESTED months (a row from any
    other month means the read was not actually scoped and rejects the whole
    readback). Rows inside a requested month but outside the [first, last] day
    span are legitimately returned by a month-grained store and are excluded
    from the comparison set -- they are not the build's claimed rows. A partial
    row, a duplicate id, or any read failure raises _ReadbackUnavailable:
    untrustworthy evidence, never a pass."""
    snapshot = {}
    seen_ids = set()
    month_set = set(months)
    for month in months:
        for row in _read_month_rows(store, gym_key, month):
            if not isinstance(row, dict):
                raise _ReadbackUnavailable("calendar readback returned a partial row")
            missing = [k for k in _ROW_REQUIRED_FIELDS if k not in row]
            if missing:
                raise _ReadbackUnavailable(
                    f"calendar readback returned a partial row (missing {missing[0]})")
            rid = row["id"]
            if not isinstance(rid, str) or not _ROW_ID.match(rid):
                raise _ReadbackUnavailable("calendar readback returned a malformed row id")
            if not isinstance(row["gym_id"], str) or row["gym_id"] != gym_key:
                raise _ReadbackUnavailable("calendar readback crossed the tenant scope")
            pd = _strict_date(row["post_date"])
            if pd is None:
                raise _ReadbackUnavailable("calendar readback returned a malformed post_date")
            if pd.isoformat()[:7] not in month_set:
                raise _ReadbackUnavailable("calendar readback escaped the requested span")
            if rid in seen_ids:
                raise _ReadbackUnavailable("calendar readback returned a duplicate row id")
            seen_ids.add(rid)
            if first is not None and last is not None and not (first <= pd <= last):
                continue
            snapshot[rid] = json.dumps(
                {k: row.get(k) for k in _ROW_FINGERPRINT_FIELDS},
                sort_keys=True, default=str)
    return snapshot


def _compare_calendar_snapshots(before, after, build_result):
    """(verified, evidence_note). verified is True ONLY when the independent
    before/after comparison fully confirms the build's claims: exactly `upserted`
    new row ids, exactly `deleted` removed row ids, NOT ONE pre-existing row
    changed -- and the build actually wrote something (a no-op build restaged
    nothing, so there is nothing to verify)."""
    upserted = build_result.get("upserted")
    deleted = build_result.get("deleted", 0)
    for label, value in (("upserted", upserted), ("deleted", deleted)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False, f"the build reported no integer {label} count to compare"
    new_ids = set(after) - set(before)
    gone_ids = set(before) - set(after)
    changed = [rid for rid in set(before) & set(after) if before[rid] != after[rid]]
    if changed:
        return False, (f"{len(changed)} pre-existing row(s) changed while the build "
                       "ran; the calendar moved under the comparison")
    if len(new_ids) != upserted:
        return False, (f"the build claims {upserted} upserted row(s); the independent "
                       f"readback sees {len(new_ids)} new row(s)")
    if len(gone_ids) != deleted:
        return False, (f"the build claims {deleted} deleted row(s); the independent "
                       f"readback sees {len(gone_ids)} removed row(s)")
    if upserted == 0:
        return False, ("the build reported a no-op (0 upserted rows); nothing was "
                       "restaged, so there is no postcondition to verify")
    return True, None


def _restage_args(ctx):
    a = ctx.args or {}
    try:
        days = int(a.get("days", 21))
    except (TypeError, ValueError):
        return None, "args.days must be an integer"
    if not 1 <= days <= MAX_DAYS:
        return None, f"args.days must be 1..{MAX_DAYS}"
    start = str(a.get("start_date") or "").strip()
    if start and not _DATE.match(start):
        return None, "args.start_date must be YYYY-MM-DD"
    try:
        budget = int(a.get("render_budget", DEFAULT_RENDER_BUDGET))
    except (TypeError, ValueError):
        return None, "args.render_budget must be an integer"
    if not 0 <= budget <= MAX_RENDER_BUDGET:
        return None, f"args.render_budget must be 0..{MAX_RENDER_BUDGET}"
    return {"days": days, "start_date": start, "render_budget": budget}, ""


def run_restage_month(gym_key, *, days=21, start_date="", render_budget=DEFAULT_RENDER_BUDGET,
                      deps=None, log=print, steps=None):
    """The PR #98 post-deploy recipe as one function: (0) prerender every active Drive
    source filed under this gym (stale keys remapped), (1) observe_denials, (2)
    build_client_month for `days` from `start_date` (today). Returns a summary; each step's
    outcome is appended to `steps` as it finishes so a poller sees progress."""
    deps = deps or {}
    steps = steps if steps is not None else []
    from datetime import date as _date
    start = start_date or _date.today().isoformat()
    out = {"gym_key": gym_key, "days": days, "start_date": start,
           "render_budget": render_budget}

    # 0. prerender
    if "sync_sources" in deps:
        prerender = deps["sync_sources"](gym_key, render_budget)
    else:
        from . import gym_media_index as _idx, gym_media_routes as _gmr
        from .jobs import sync_gym_media as _sync
        store = _idx.default_store()
        prerender = []
        for src in store.list_sources():
            if str(src.get("kind", "gym_drive")) != "gym_drive":
                continue
            resolved = _gmr._resolve_stale_fingerprint(src.get("gym_id"))
            if resolved != gym_key:
                continue
            src = dict(src)
            src["gym_id"] = resolved
            prerender.append(_sync.sync_source(src, store=store, log=log,
                                               render_budget=render_budget))
    out["prerender"] = prerender
    steps.append({"step": "prerender", "at": _now_iso(),
                  "sources": len(prerender) if isinstance(prerender, list) else None})

    # 1. release denied assets
    observe = deps.get("observe_denials") or __import__(
        "agent.gym_media_selector", fromlist=["observe_denials"]).observe_denials
    out["observe_denials"] = _observe_denials_for_gym(gym_key, observe)
    steps.append({"step": "observe_denials", "at": _now_iso(), **out["observe_denials"]})

    # 2. build
    # Independent calendar readback, bounded to this gym and the REQUESTED span:
    # snapshot the span's rows BEFORE the builder runs so the terminal result can
    # compare what the build claims it wrote against what an independent re-read
    # sees. An evidence fault never blocks the build; it only withholds
    # verification from the terminal job result.
    readback_store = None
    before_rows = None
    evidence_note = "independent calendar readback unavailable"
    try:
        from datetime import date as _date, timedelta as _td
        span_first = _date.fromisoformat(str(start)[:10])
        span_last = span_first + _td(days=max(int(days), 1) - 1)
        readback_store = deps.get("calendar_store") or _default_calendar_readback_store()
        before_rows = _calendar_snapshot(readback_store, gym_key,
                                         _span_months(start, days),
                                         first=span_first, last=span_last)
    except Exception as e:  # noqa: BLE001
        evidence_note = (f"pre-build calendar readback unavailable: "
                         f"{type(e).__name__}: {str(e)[:160]}")
    if "build_month" in deps:
        built = deps["build_month"](gym_key, start, days)
    else:
        from . import client_media_sync as cms
        from .accounts import get_account
        from .voice import load_voice
        from .client_month_run import build_client_month
        account = get_account(f"{gym_key}_ig")
        if account is None:
            raise LookupError(f"no generation account {gym_key}_ig")
        voice = load_voice(cms._resolve_client_voice_path(gym_key, account.voice_doc_path()))
        if voice is None:
            raise LookupError(f"no brand voice doc for {gym_key}")
        store = cms._default_store()
        if store is None:
            raise LookupError("portal calendar store unavailable (Supabase plane off)")
        built = build_client_month(account, gym_key, start, days, voice=voice,
                                   library_path=cms._library_dir(gym_key), store=store,
                                   banned_words=cms._banned_words_for(gym_key), logger=log)
    out["build"] = built
    # The builder's response does not identify which rows survived the calendar
    # write or human edits, and its own ok/upserted claims are never the proof:
    # any postcondition_verified it returns is stripped, and the terminal job
    # verifies ONLY through the independent before/after comparison.
    if isinstance(out["build"], dict):
        out["build"] = {k: v for k, v in out["build"].items()
                        if k != "postcondition_verified"}
    verified = False
    if before_rows is not None and isinstance(built, dict) and built.get("ok") is True:
        try:
            after_rows = _calendar_snapshot(readback_store, gym_key,
                                            _span_months(start, days),
                                            first=span_first, last=span_last)
            verified, evidence_note = _compare_calendar_snapshots(
                before_rows, after_rows, built)
        except Exception as e:  # noqa: BLE001
            verified = False
            evidence_note = (f"post-build calendar readback failed: "
                             f"{type(e).__name__}: {str(e)[:160]}")
    out["postcondition_verified"] = verified
    if not verified:
        out["evidence_note"] = evidence_note
    build_result = built if isinstance(built, dict) else {}
    steps.append({"step": "build", "at": _now_iso(), "ok": build_result.get("ok") is True,
                  "upserted": build_result.get("upserted")})
    if not isinstance(built, dict) or built.get("ok") is not True:
        reason = build_result.get("reason") or "invalid build result"
        raise RuntimeError(f"calendar build did not succeed: {reason or 'ok was not true'}")
    out["summary"] = (f"restage {gym_key}: {len(prerender) if isinstance(prerender, list) else '?'}"
                      f" source(s) prerendered, {out['observe_denials'].get('rolled_back', 0)} "
                      f"asset(s) released, build ok={bool((built or {}).get('ok'))} "
                      f"upserted={(built or {}).get('upserted', '?')} days={days}")
    out["captured_at"] = _now_iso()
    return out


class Jobs:
    """In-memory job registry for background actions. One per process; bounded."""

    def __init__(self, keep=MAX_JOBS_KEPT):
        self._lock = threading.Lock()
        self._jobs = {}
        self._keep = keep

    def create(self, *, action, gym_key, ticket_id, deadline_sec):
        job = {"id": uuid.uuid4().hex, "action": action, "gym_key": gym_key,
               "ticket_id": ticket_id, "status": "running", "started_at": _now_iso(),
               "finished_at": None, "result": None, "error": None, "steps": [],
               "deadline_sec": deadline_sec}
        with self._lock:
            self._jobs[job["id"]] = job
            if len(self._jobs) > self._keep:
                oldest = sorted(self._jobs.values(), key=lambda j: j["started_at"])
                for j in oldest[: len(self._jobs) - self._keep]:
                    if j["status"] != "running":
                        self._jobs.pop(j["id"], None)
        return job

    def finish(self, job_id, *, result=None, error=None):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            j["finished_at"] = _now_iso()
            j["status"] = "failed" if error else "done"
            j["result"], j["error"] = result, error

    def get(self, job_id, now=None):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return None
            j = dict(j)
        if j["status"] == "running":
            try:
                started = datetime.fromisoformat(j["started_at"])
                age = ((now or datetime.now(timezone.utc)) - started).total_seconds()
                if age > j.get("deadline_sec", RESTAGE_DEADLINE_SEC):
                    j["status"] = "timed_out"
            except Exception:  # noqa: BLE001
                pass
        return j


JOBS = Jobs()


def _run_restage_month(ctx):
    parsed, err = _restage_args(ctx)
    if err:
        return 400, {"error": "bad_request", "detail": err}
    jobs = _dep(ctx, "jobs", lambda: JOBS)
    job = jobs.create(action="restage_month", gym_key=ctx.gym_key, ticket_id=ctx.ticket_id,
                      deadline_sec=RESTAGE_DEADLINE_SEC)
    bus = ctx.deps.get("bus")

    def _work():
        try:
            result = run_restage_month(ctx.gym_key, days=parsed["days"],
                                       start_date=parsed["start_date"],
                                       render_budget=parsed["render_budget"],
                                       deps=ctx.deps, log=ctx.log, steps=job["steps"])
            jobs.finish(job["id"], result=result)
            _ticket_note(bus, ctx.ticket_id, "restage_month",
                         f"job {job['id']} done: {result.get('summary', '')}", ctx.log)
        except Exception as e:  # noqa: BLE001 - the job records its own failure
            jobs.finish(job["id"], error=f"{type(e).__name__}: {e}")
            _ticket_note(bus, ctx.ticket_id, "restage_month",
                         f"job {job['id']} FAILED: {type(e).__name__}: {e}", ctx.log)

    runner = ctx.deps.get("thread_runner")
    if runner is not None:
        runner(_work)
    else:
        threading.Thread(target=_work, daemon=True, name=f"fixer-ops-{job['id'][:8]}").start()
    return 202, {"job_id": job["id"], "status": "running",
                 "summary": (f"restage_month started as job {job['id']} "
                             f"(days={parsed['days']}, start={parsed['start_date'] or 'today'}, "
                             f"render_budget={parsed['render_budget']}); poll "
                             f"GET {ROUTE_PREFIX}/jobs/{job['id']}")}


CATALOG = {
    "resend_connect_link": ActionSpec(
        "resend_connect_link", _run_resend_connect_link,
        description="DM the gym owner a fresh connect link (owner from the portal's records)"),
    "reset_recreate_budget": ActionSpec(
        "reset_recreate_budget", _run_reset_recreate_budget,
        description="zero this gym's month deny/recreate spend"),
    "release_denied_assets": ActionSpec(
        "release_denied_assets", _run_release_denied_assets, needs_volume=True,
        description="roll portal-denied Drive assets back into the pool (observe_denials)"),
    "swap_media": ActionSpec(
        "swap_media", _run_swap_media, args_schema={"row_id": "content_calendar row id"},
        description="free photo/video swap on one pending row (siblings move with it)"),
    "requeue_failed_row": ActionSpec(
        "requeue_failed_row", _run_requeue_failed_row,
        args_schema={"row_id": "content_calendar row id"},
        description="move ONE failed googlebusiness row back to approved"),
    "restage_month": ActionSpec(
        "restage_month", _run_restage_month, needs_volume=True, background=True,
        args_schema={"days": f"1..{MAX_DAYS} (default 21)", "start_date": "YYYY-MM-DD (today)",
                     "render_budget": f"0..{MAX_RENDER_BUDGET} (default {DEFAULT_RENDER_BUDGET})"},
        description="prerender -> observe_denials -> build_client_month, as a background job"),
}


def catalog_json():
    return {"ok": True, "actions": [
        {"name": s.name, "args": s.args_schema, "needs_volume": s.needs_volume,
         "background": s.background, "description": s.description}
        for s in CATALOG.values()],
        "org_floor": sorted(ORG_FLOOR_ACTIONS), "volume_available": volume_available()}


# --------------------------------------------------------------------------------------
# the ticket record + audit line
# --------------------------------------------------------------------------------------

def _ticket_note(bus, ticket_id, action, summary, log=print):
    """One support_messages SYSTEM row on the ticket, client-invisible (kind escalation is
    on the portal's denylist), delivery_status null so no outbox ever posts it -- it is a
    record, not a message. Never raises: the action already happened."""
    line = f"OPS ACTION {action} by {ACTOR}: {summary}"
    try:
        if bus is None:
            from .slack_convo.bus import Bus
            bus = Bus()
        bus.record_outbound(ticket_id=ticket_id, author_type="system", body=line[:8000],
                            delivery_status=None, kind="escalation",
                            meta={"ops_action": action, "actor": ACTOR, "record_only": True,
                                  "at": _now_iso()})
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[fixer-ops] ticket note failed ticket={ticket_id}: {type(e).__name__}: {e}")
        return False


def _audit(action, gym_key, ticket_id, status, summary, log=print):
    log(f"[fixer-ops] AUDIT action={action} gym={gym_key} ticket={ticket_id} actor={ACTOR} "
        f"status={status} at={_now_iso()} summary={summary!r}")


def _ticket_tenant(gym_key, ticket_id, deps):
    """Bind an ops request to the persisted ticket before any side effect.

    A portal ticket's client_id is a gym UUID; the portal's exact token mapping
    supplies its Echo account key. Older Echo tickets can carry the account key
    directly. Internal ops_fix alerts have no client_id and remain a separate,
    explicitly identified automation lane. A missing or ambiguous mapping never
    authorizes a write. Only the two identity columns are read from tokens.
    """
    bus = deps.get("bus")
    if bus is None:
        from .slack_convo.bus import Bus
        bus = Bus()
    try:
        tickets = bus._get("support_tickets", {
            "id": f"eq.{ticket_id}",
            "select": "id,product,source,client_id,raw_text,verification_before",
            "limit": "2"})
        if (not isinstance(tickets, list) or len(tickets) != 1
                or not isinstance(tickets[0], dict)
                or tickets[0].get("id") != ticket_id):
            return 409, {"error": "ticket_tenant_unconfirmed"}
        ticket = tickets[0]
        client_id = ticket.get("client_id")
        if client_id is None:
            before = ticket.get("verification_before") or {}
            fixer = before.get("fixer") if isinstance(before, dict) else {}
            triage = fixer.get("triage") if isinstance(fixer, dict) else {}
            bound_key = triage.get("gym_key") if isinstance(triage, dict) else None
            raw_text = str(ticket.get("raw_text") or "")
            raw_has_key = bool(re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(gym_key)}(?![A-Za-z0-9_-])",
                raw_text,
            ))
            if (ticket.get("product") == "echo" and ticket.get("source") == "ops_fix"
                    and bound_key == gym_key and raw_has_key):
                return None
            return 409, {"error": "ticket_tenant_unconfirmed"}
        if not isinstance(client_id, str) or not client_id.strip():
            return 409, {"error": "ticket_tenant_unconfirmed"}
        if not _UUID.fullmatch(client_id):
            return None if client_id == gym_key else (409, {"error": "ticket_tenant_mismatch"})
        tokens = bus._get("echo_intake_tokens", {
            "gym_id": f"eq.{client_id}", "select": "gym_id,echo_account_key", "limit": "2"})
        if (not isinstance(tokens, list) or len(tokens) != 1
                or not isinstance(tokens[0], dict)
                or tokens[0].get("gym_id") != client_id
                or not isinstance(tokens[0].get("echo_account_key"), str)
                or not _GYM_KEY.fullmatch(tokens[0]["echo_account_key"])):
            return 409, {"error": "ticket_tenant_unconfirmed"}
        if tokens[0]["echo_account_key"] != gym_key:
            return 409, {"error": "ticket_tenant_mismatch"}
        aliases = bus._get("echo_intake_tokens", {
            "echo_account_key": f"eq.{gym_key}",
            "select": "gym_id,echo_account_key", "limit": "2"})
        if (not isinstance(aliases, list) or len(aliases) != 1
                or not isinstance(aliases[0], dict)
                or aliases[0].get("gym_id") != client_id
                or aliases[0].get("echo_account_key") != gym_key):
            return 409, {"error": "ticket_tenant_unconfirmed"}
        return None
    except Exception:  # noqa: BLE001 - unreadable identity plane cannot authorize a write
        return 503, {"error": "ticket_tenant_unavailable"}


# --------------------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------------------

def run_action(action, gym_key, ticket_id, args, *, reservation_key=None, deps=None,
               log=print):
    """Validate, run, record. Returns (status, body). Auth is the transport's job."""
    deps = dict(deps or {})
    action = str(action or "").strip()
    if action in ORG_FLOOR_ACTIONS:
        _audit(action, gym_key, ticket_id, 403, "refused: org floor", log)
        # Round 2 (R4): a refused attempt leaves a trace ON THE TICKET too, not only in the
        # process log -- a teammate reading the thread should see the FIXER tried.
        if (_TICKET_ID.fullmatch(str(ticket_id or "").strip())
                and _GYM_KEY.fullmatch(str(gym_key or "").strip())
                and _ticket_tenant(str(gym_key).strip(), str(ticket_id).strip(), deps) is None):
            _ticket_note(deps.get("bus"), str(ticket_id).strip(), action,
                         "REFUSED: org_floor (billing/Stripe, pixel/CAPI, ad budget, "
                         "targeting, deleting published posts are never automated)", log)
        return 403, {"error": "org_floor", "action": action,
                     "detail": ("billing/Stripe, pixel/CAPI, ad budget, targeting and "
                                "deleting published posts are never automated; a person "
                                "with explicit approval does these")}
    spec = CATALOG.get(action)
    if spec is None:
        return 404, {"error": "unknown_action", "action": action,
                     "known": sorted(CATALOG)}
    gym_key = str(gym_key or "").strip()
    ticket_id = str(ticket_id or "").strip()
    if not _GYM_KEY.match(gym_key):
        return 400, {"error": "bad_request", "detail": "gym_key required (account key)"}
    if not _TICKET_ID.match(ticket_id):
        return 400, {"error": "bad_request", "detail": "ticket_id required"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return 400, {"error": "bad_request", "detail": "args must be an object"}
    tenant_refusal = _ticket_tenant(gym_key, ticket_id, deps)
    if tenant_refusal:
        _audit(action, gym_key, ticket_id, tenant_refusal[0],
               tenant_refusal[1]["error"], log)
        return tenant_refusal
    if spec.needs_volume:
        has_volume = deps["volume_available"]() if "volume_available" in deps \
            else volume_available()
        if not has_volume:
            return 503, {"error": "volume_unavailable", "action": action,
                         "detail": (f"{action} needs the worker's data volume (/data); this "
                                    "host has none. Call the same route on the echo worker "
                                    "(connect_web, AGENT_CONNECT_PORT) or run `python -m "
                                    f"agent ops-action {action} ...` there.")}
    if reservation_key is not None and spec.background:
        # The existing job registry is in memory. A 202 launch cannot be a
        # durable completed receipt, and a restart cannot prove job completion.
        return 409, {"error": "reservation_background_unsupported", "action": action}
    # Durable reservation (optional): begin BEFORE any side effect; a replay returns
    # the stored result without re-executing; an unknown outcome is never retried.
    # The key is validated by the receipt store EXACTLY as supplied -- no coercion,
    # no trimming: a padded or non-string key is 400 bad_reservation_key, and the
    # stored identity is byte-for-byte the caller's.
    receipt = None
    receipt_store = None
    if reservation_key is not None:
        from . import fixer_ops_receipts as receipts
        receipt_store = deps.get("receipt_store")
        try:
            if receipt_store is None:
                receipt_store = receipts.default_store()
            receipt = receipts.begin(receipt_store, reservation_key, action, gym_key,
                                     ticket_id, args)
        except receipts.ReceiptError as e:
            _audit(action, gym_key, ticket_id, e.status,
                   f"reservation refused: {e.code}", log)
            return e.status, {"error": e.code, "reservation_key": reservation_key}
        if receipt.get("replay"):
            _audit(action, gym_key, ticket_id, 200,
                   f"replay of reservation {reservation_key}: no side effects", log)
            body = {"ok": True, "action": action, "gym_key": gym_key,
                    "ticket_id": ticket_id, "result": receipt.get("result"),
                    "receipt": receipt, "replayed": True}
            return receipt.get("http_status", 200), body
    ctx = Ctx(gym_key=gym_key, ticket_id=ticket_id, args=args, deps=deps, log=log)
    try:
        status, result = spec.run(ctx)
    except Exception as e:  # noqa: BLE001 - a wrapped function's fault is a 500 with a name
        if receipt is not None:
            # The side effect may or may not have landed: mark the receipt unknown and
            # refuse automation from here on -- never silently retry an unknown outcome.
            detail = f"{type(e).__name__}: {e}"[:500]
            try:
                receipt = receipts.mark_unknown(receipt_store, reservation_key, detail)
            except receipts.ReceiptError:
                pass
            _audit(action, gym_key, ticket_id, 503,
                   f"outcome unknown after reservation {reservation_key}: {detail}", log)
            return 503, {"error": "reservation_outcome_unknown", "detail": detail,
                         "reservation_key": reservation_key, "receipt": receipt}
        status, result = 500, {"error": f"{type(e).__name__}", "detail": str(e)[:300]}
    result = dict(result or {})
    outcome_unknown = result.pop("_outcome_unknown", False) is True
    if receipt is not None:
        try:
            if outcome_unknown:
                original_error = str(result.get("error") or status)
                receipt = receipts.mark_unknown(receipt_store, reservation_key,
                                                original_error)
                status = 503
                result["error"] = "reservation_outcome_unknown"
                result["detail"] = original_error
            elif 200 <= status < 300:
                receipt = receipts.commit(receipt_store, reservation_key, result,
                                          http_status=status)
            elif status < 500:
                receipt = receipts.fail(receipt_store, reservation_key,
                                        str(result.get("error") or status))
            else:
                receipt = receipts.mark_unknown(receipt_store, reservation_key,
                                                str(result.get("error") or status))
        except receipts.ReceiptError as e:
            _audit(action, gym_key, ticket_id, e.status,
                   f"receipt finalize failed: {e.code}", log)
            return e.status, {"error": e.code, "reservation_key": reservation_key}
    summary = str(result.get("summary") or result.get("error") or status)
    _audit(action, gym_key, ticket_id, status, summary, log)
    if status < 500:
        _ticket_note(deps.get("bus"), ticket_id, action, f"{status} {summary}", log)
    body = {"ok": 200 <= status < 300, "action": action, "gym_key": gym_key,
            "ticket_id": ticket_id}
    if 200 <= status < 300 and not spec.background:
        body["result"] = result
    else:
        body.update(result)
    if receipt is not None:
        body["receipt"] = receipt
    return status, body


def handle(method, path, headers_get, raw_body=b"", *, deps=None, log=print, now=None):
    """Transport-agnostic router for the two hosts. Returns (status, body_dict), or None
    when `path` is not one of ours (the caller falls through to its own routes)."""
    from urllib.parse import parse_qs, urlsplit
    parsed = urlsplit(path or '')
    path = parsed.path.rstrip("/") or "/"
    if path != ROUTE_PREFIX and not path.startswith(ROUTE_PREFIX + "/"):
        return None
    refused = authorize(headers_get)
    if refused:
        return refused
    deps = dict(deps or {})
    method = (method or "").upper()
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/reply-reconciliation/"
                 r"([0-9a-f]{32})$", path)
    if m:
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        query = parse_qs(parsed.query)
        if set(query) != {"gym_key"} or len(query.get("gym_key") or []) != 1:
            return 400, {"error": "bad_request", "detail": "gym_key required"}
        gym_key = query["gym_key"][0]
        from .fixer_reply_reconciliation import (
            ReconciliationError, default_store, reconcile_snapshot,
        )
        store = deps.get("reply_snapshot_store")
        if store is None:
            try:
                store = default_store()
            except ReconciliationError as exc:
                return exc.status, {"error": exc.code}
        zernio = deps.get("reply_reconciliation_zernio")
        if zernio is None:
            from .zernio import ZernioClient
            zernio = ZernioClient()
        try:
            result = reconcile_snapshot(m.group(1), gym_key, zernio=zernio,
                                        store=store, now=now)
            return 200, result
        except ReconciliationError as exc:
            return exc.status, {"error": exc.code}
        except Exception:  # noqa: BLE001 - provider/store faults fail closed
            return 503, {"error": "reply_reconciliation_unavailable"}
    if path == BUSINESS_EVIDENCE_PATH:
        if method != "POST":
            return 405, {"error": "method_not_allowed"}
        return _run_business_evidence(raw_body, deps, now=now)
    if path.startswith(ROUTE_PREFIX + "/evidence/media-source/"):
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        from .fixer_evidence import inspect_media_source, EvidenceError
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
            if set(query) != {'gym_key', 'folder_id'} or any(len(v) != 1 for v in query.values()):
                return 400, {"error": "bad_media_identifier"}
            return 200, inspect_media_source(
                path[len(ROUTE_PREFIX + "/evidence/media-source/"):],
                query['gym_key'][0], query['folder_id'][0], deps=deps.get('evidence'))
        except (ValueError, EvidenceError) as exc:
            if isinstance(exc, EvidenceError):
                return exc.status, {"error": exc.code}
            return 400, {"error": "bad_media_identifier"}
        except Exception:
            return 503, {"error": "evidence_unavailable"}
    if path.startswith(ROUTE_PREFIX + "/evidence/"):
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        if not (deps["volume_available"]() if "volume_available" in deps else volume_available()):
            return 503, {"error": "volume_unavailable"}
        from .fixer_evidence import gather, EvidenceError
        try:
            return 200, gather(path[len(ROUTE_PREFIX + "/evidence/"):],
                               deps=deps.get("evidence"), now=now)
        except EvidenceError as exc:
            return exc.status, {"error": exc.code}
        except Exception:
            return 503, {"error": "evidence_unavailable"}
    if method == "GET" and path == ROUTE_PREFIX:
        return 200, catalog_json()
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/resend_connect_link/readiness/"
                 r"([A-Za-z0-9_-]{1,80})$", path)
    if m:
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        return _resend_connect_link_readiness(m.group(1), deps)
    # The key segment is captured loosely on purpose: the contract is that a MALFORMED
    # key (wrong charset, wrong length) is 400 bad_reservation_key, decided by the
    # receipt store's validator -- a route that pre-filters to well-formed keys would
    # silently turn those into a wrong, less truthful 404.
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/receipts/([^/]{{1,256}})$", path)
    if m:
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        from .fixer_ops_receipts import ReceiptError, default_store, get_receipt
        query = parse_qs(parsed.query)
        gym_key = (query.get("gym_key") or [""])[0].strip()
        if not gym_key:
            return 400, {"error": "bad_request", "detail": "gym_key required"}
        store = deps.get("receipt_store")
        try:
            if store is None:
                store = default_store()
            return 200, {"ok": True, "receipt": get_receipt(store, m.group(1), gym_key)}
        except ReceiptError as exc:
            return exc.status, {"error": exc.code}
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/jobs/([0-9a-f]{{32}})$", path)
    if m:
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        jobs = deps.get("jobs") or JOBS
        job = jobs.get(m.group(1), now=now)
        if not job:
            return 404, {"error": "unknown_job"}
        return 200, {"ok": True, "job": job}
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/([A-Za-z0-9_]{{1,64}})$", path)
    if not m:
        return 404, {"error": "not_found"}
    if method != "POST":
        return 405, {"error": "method_not_allowed"}
    if raw_body and len(raw_body) > MAX_BODY_BYTES:
        return 413, {"error": "too_large"}
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:  # noqa: BLE001
        return 400, {"error": "bad_request", "detail": "invalid JSON"}
    if not isinstance(body, dict):
        return 400, {"error": "bad_request", "detail": "body must be an object"}
    return run_action(m.group(1), body.get("gym_key"), body.get("ticket_id"),
                      body.get("args"), reservation_key=body.get("reservation_key"),
                      deps=deps, log=log)
