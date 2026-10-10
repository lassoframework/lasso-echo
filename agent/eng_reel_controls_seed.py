"""One-ticket recovery producer for the ENG website_tab reel-controls ticket.

Scope (hard-bound, exactly one ticket):
  ticket_id    554ac760-b0b3-413a-919f-4e13cff6d3fc
  client_id    6ee04ee4-13a5-47db-8416-7b8ee3e61ab8
  echo key     eng
  original reel request b185a4e4-425b-5bc3-8ec6-7c9145426918

This producer re-reads the exact current support ticket and its current
requester key through the service-role Bus, validates the immutable ticket
identity (source website_tab, product echo, classification code_fix, status
merged, request_version 1, exact portal client, no existing different business
plan), validates the exact bidirectional Echo tenant mapping, and validates the
worker-owned durable auto_reels.gym_status for the ENG tenant holding exactly
this original job in a truthful terminal portrait hold.  Only then does it
build the schema_version 1 / echo-business-evidence-v1 seed for check
automatic_reel_controls_repaired with params {"request_id": original UUID}.

Default is a dry run: the seed is printed and nothing is sent.  ``--write``
POSTs the seed to the fixed Scout fixer-service HTTPS origin at
/fixer/business-check-seed with the FIXER_OPS_SECRET header (never logged),
refuses redirects, bounds the response, and requires the authenticated binder
response to confirm the exact current request key.  On write it then re-reads the ticket
metadata and current request key and requires the persisted plan and unchanged
request identity, proving the binding landed. The reel seed carries the
prechecked expected_request_key; Scout refuses a newer requester key before
writing ticket metadata.

Fail closed on any changed ticket/thread/tenant/job and on any ambiguous HTTP
response.  This module never sends Slack, never resolves or closes the ticket,
never submits media, and performs no git or deploy action.

Known limitations:
  - The terminal-hold proof covers the original job only; reel completion,
    customer approval and provider publication are separate follow-ups and are
    never claimed here.
  - issued_at is the producer clock, formatted as Scout's UTC milliseconds.
    Persisted plans bind the current request key and do not contain issued_at.
  - The bidirectional tenant mapping uses the service-role REST resolver by
    default; callers may inject a resolver for tests only.

Run on the worker as:
  /opt/venv/bin/python -m agent.eng_reel_controls_seed          # dry run
  /opt/venv/bin/python -m agent.eng_reel_controls_seed --write  # persist
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable

TICKET_ID = "554ac760-b0b3-413a-919f-4e13cff6d3fc"
CLIENT_ID = "6ee04ee4-13a5-47db-8416-7b8ee3e61ab8"
GYM_KEY = "eng"
REQUEST_ID = "b185a4e4-425b-5bc3-8ec6-7c9145426918"

SCHEMA_VERSION = 1
CONTRACT_VERSION = "echo-business-evidence-v1"
SOURCE = "website_tab"
CHECK_ID = "automatic_reel_controls_repaired"

DEFAULT_BASE_URL = "https://wrangler-production.up.railway.app"
SEED_PATH = "/fixer/business-check-seed"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 32 * 1024

_SEED_KEYS = frozenset({
    "schema_version", "contract_version", "ticket_id", "client_id", "source",
    "issued_at", "check_id", "params", "expected_request_key",
})
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_PLAN_KEYS = frozenset({
    "schema_version", "contract_version", "ticket_id", "client_id",
    "request_key", "check_id", "params",
})
_REQUEST_KEY = re.compile(r"[0-9a-f]{64}\Z")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")
PORTRAIT_HOLD_REASON = "Automatic reel held: The complete athlete will not fit a portrait crop"


class ReelSeedError(ValueError):
    """The recovery seed cannot be built, sent, or confirmed; fail closed."""

    def __init__(self, reason: str):
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{2,64}", reason):
            reason = "invalid_reason"
        self.reason = reason
        super().__init__(reason)


def _utc_now(now=None) -> datetime:
    moment = now if now is not None else datetime.now(timezone.utc)
    if (not isinstance(moment, datetime) or moment.tzinfo is None
            or moment.utcoffset() is None):
        raise ReelSeedError("observer_clock_unreadable")
    return moment.astimezone(timezone.utc)


def build_plan(now=None, *, expected_request_key: str) -> dict:
    """Build the exact versioned seed for this one hard-bound ticket."""
    if (not isinstance(expected_request_key, str)
            or not _REQUEST_KEY.fullmatch(expected_request_key)):
        raise ReelSeedError("current_request_unavailable")
    return {
        "expected_request_key": expected_request_key,
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "ticket_id": TICKET_ID,
        "client_id": CLIENT_ID,
        "source": SOURCE,
        "issued_at": _utc_now(now).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "check_id": CHECK_ID,
        "params": {"request_id": REQUEST_ID},
    }


def _existing_plan(ticket: dict):
    """The business plan already bound to this ticket, or None."""
    before = ticket.get("verification_before")
    fixer = before.get("fixer") if isinstance(before, dict) else None
    plan = fixer.get("business_check") if isinstance(fixer, dict) else None
    return plan if isinstance(plan, dict) else None


def _bound_plan(seed: dict, request_key: str) -> dict:
    return {key: seed[key] for key in _PLAN_KEYS - {"request_key"}} | {
        "request_key": request_key,
    }


def _validate_ticket(ticket: Any) -> dict:
    if not isinstance(ticket, dict) or ticket.get("id") != TICKET_ID:
        raise ReelSeedError("ticket_unavailable")
    if ticket.get("source") != "website_tab" or ticket.get("product") != "echo":
        raise ReelSeedError("ticket_identity_mismatch")
    if ticket.get("classification") != "code_fix":
        raise ReelSeedError("ticket_identity_mismatch")
    if ticket.get("status") != "merged":
        raise ReelSeedError("ticket_not_merged")
    version = ticket.get("request_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ReelSeedError("ticket_request_version_mismatch")
    if ticket.get("client_id") != CLIENT_ID:
        raise ReelSeedError("tenant_mismatch")
    return ticket


def _reconcile_existing_plan(ticket: dict, plan: dict) -> None:
    """Refuse stale, foreign, or malformed existing bindings."""
    before = ticket.get("verification_before")
    if before is not None and not isinstance(before, dict):
        raise ReelSeedError("different_business_plan")
    fixer = before.get("fixer") if isinstance(before, dict) else None
    if fixer is not None and not isinstance(fixer, dict):
        raise ReelSeedError("different_business_plan")
    existing = fixer.get("business_check") if isinstance(fixer, dict) else None
    if existing is not None and existing != plan:
        raise ReelSeedError("different_business_plan")


def _fresh_binding(bus, ticket: dict, request_key: str) -> dict:
    from .slack_convo.outbox import _current_fixer_request_key
    fresh = _validate_ticket(bus.ticket(TICKET_ID))
    if (_current_fixer_request_key(bus, fresh) != request_key
            or any(fresh.get(field) != ticket.get(field) for field in (
                "slack_channel_id", "slack_thread_ts", "client_id",
                "bot_identity", "slack_user_id", "source", "product",
                "request_version"))):
        raise ReelSeedError("ticket_changed_during_write")
    return fresh


def _resolve_client(client_resolver: Callable[[str], Any] | None) -> str:
    if client_resolver is not None:
        client_id = client_resolver(GYM_KEY)
    else:
        from . import fixer_business_seed_client
        # Default resolver performs the exact forward AND reverse mapping read.
        client_id = fixer_business_seed_client.resolve_portal_client_id(GYM_KEY)
    if not isinstance(client_id, str) or not _UUID.fullmatch(client_id):
        raise ReelSeedError("tenant_mapping_unavailable")
    if client_id.lower() != CLIENT_ID:
        raise ReelSeedError("tenant_mismatch")
    return CLIENT_ID


def _validate_worker_state(status: Any) -> dict:
    """Require the durable ENG snapshot with exactly the original job in a
    truthful terminal portrait hold."""
    if (not isinstance(status, dict) or status.get("ok") is not True
            or status.get("gym") != GYM_KEY):
        raise ReelSeedError("worker_status_unavailable")
    jobs = status.get("jobs")
    if not isinstance(jobs, list):
        raise ReelSeedError("worker_status_unavailable")
    matches = [job for job in jobs
               if isinstance(job, dict) and job.get("request_id") == REQUEST_ID]
    if len(matches) != 1:
        raise ReelSeedError("original_job_unavailable")
    job = matches[0]
    # Older exhausted jobs retain their final retry timestamp. The worker's
    # status projection marks them exhausted by attempt count; a past retry
    # timestamp does not make them runnable again.
    retry_at = job.get("next_attempt_at")
    if job.get("status") != "exhausted" or (
            retry_at is not None
            and (isinstance(retry_at, bool)
                 or not isinstance(retry_at, (int, float))
                 or not math.isfinite(retry_at)
                 or not 0 <= retry_at <= datetime.now(timezone.utc).timestamp())):
        raise ReelSeedError("original_job_not_terminal_hold")
    if job.get("reason") != PORTRAIT_HOLD_REASON:
        raise ReelSeedError("original_job_not_portrait_hold")
    return job


def _default_post(url: str, *, headers: dict, body: bytes, timeout: float):
    import requests
    return requests.post(url, headers=headers, data=body, timeout=timeout,
                         allow_redirects=False)


def send_plan(plan: dict, *, base_url: str | None = None,
              secret: str | None = None, post: Callable[..., Any] | None = None,
              request_key: str,
              timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """POST the exact seed and require the binder to confirm the request key.

    The shared secret travels only in the request header.  Redirects,
    non-success or ambiguous statuses, oversized or invalid bodies, and any
    request mismatch all fail closed without logging credentials or bodies.
    """
    if (not isinstance(plan, dict) or set(plan) != _SEED_KEYS
            or not isinstance(plan.get("issued_at"), str)
            or not _ISO_UTC.fullmatch(plan["issued_at"])
            or any(plan.get(key) != value for key, value in build_plan(expected_request_key=request_key).items()
                   if key != "issued_at")
            or not isinstance(request_key, str) or not _REQUEST_KEY.fullmatch(request_key)):
        raise ReelSeedError("invalid_seed")
    shared_secret = secret if secret is not None else os.getenv("FIXER_OPS_SECRET", "")
    if not shared_secret:
        raise ReelSeedError("missing_secret")
    host = (base_url if base_url is not None
            else os.getenv("FIXER_SERVICE_URL", DEFAULT_BASE_URL)).rstrip("/")
    if host != DEFAULT_BASE_URL:
        raise ReelSeedError("fixer_service_unavailable")
    try:
        budget = float(timeout)
    except (TypeError, ValueError):
        raise ReelSeedError("invalid_timeout") from None
    if not 0 < budget <= 15:
        raise ReelSeedError("invalid_timeout")
    body = json.dumps({"seed": plan}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        response = (post or _default_post)(
            f"{host}{SEED_PATH}",
            headers={
                "X-Fixer-Ops-Secret": shared_secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            timeout=budget,
        )
    except ReelSeedError:
        raise
    except Exception:  # noqa: BLE001 - transport faults are never success
        raise ReelSeedError("fixer_service_unavailable") from None
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        raise ReelSeedError("invalid_response")
    if status != 200:
        raise ReelSeedError(
            "seed_rejected" if 400 <= status < 500 else "fixer_service_unavailable")
    raw = getattr(response, "content", b"")
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
        raise ReelSeedError("invalid_response")
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReelSeedError("invalid_response") from None
    if (not isinstance(value, dict) or set(value) != {"ok", "request_key"}
            or value.get("ok") is not True):
        raise ReelSeedError("invalid_response")
    if value.get("request_key") != request_key:
        raise ReelSeedError("binder_request_mismatch")


def run(*, write: bool = False, bus=None, client_resolver=None,
        gym_status: Callable[[str], Any] | None = None,
        secret: str | None = None, post=None, base_url: str | None = None,
        now=None) -> dict:
    """Re-read, validate, build, and (only with write=True) persist the seed."""
    if bus is None:
        from .slack_convo.bus import Bus
        bus = Bus()
    from .slack_convo.outbox import _current_fixer_request_key
    ticket = _validate_ticket(bus.ticket(TICKET_ID))
    request_key = _current_fixer_request_key(bus, ticket)
    if not isinstance(request_key, str) or not _REQUEST_KEY.fullmatch(request_key):
        raise ReelSeedError("current_request_unavailable")
    seed = build_plan(now, expected_request_key=request_key)
    plan = _bound_plan(seed, request_key)
    _reconcile_existing_plan(ticket, plan)
    _resolve_client(client_resolver)
    status_reader = gym_status
    if status_reader is None:
        from . import auto_reels
        status_reader = auto_reels.gym_status
    _validate_worker_state(status_reader(GYM_KEY))
    result = {
        "ok": True,
        "mode": "write" if write else "dry_run",
        "ticket_id": TICKET_ID,
        "client_id": CLIENT_ID,
        "request_key": request_key,
        "plan": plan,
        "seed": seed,
    }
    if not write:
        return result
    # Worker and resolver reads may take time. Recheck request identity before
    # giving Scout authority to bind a plan, then verify its durable readback.
    before_send = _fresh_binding(bus, ticket, request_key)
    _reconcile_existing_plan(before_send, plan)
    send_plan(seed, base_url=base_url, secret=secret, post=post,
              request_key=request_key)
    fresh = _fresh_binding(bus, ticket, request_key)
    if _existing_plan(fresh) != plan:
        raise ReelSeedError("persisted_plan_mismatch")
    result["persisted"] = True
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="eng_reel_controls_seed",
        description="Dry-run or persist the ENG reel-controls recovery seed.")
    parser.add_argument("--write", action="store_true",
                        help="POST the seed to the Scout fixer service and "
                             "verify the persisted binding (default: dry run).")
    args = parser.parse_args(argv)
    try:
        result = run(write=bool(args.write))
    except ReelSeedError as exc:
        # Reasons are fixed lowercase tokens; never print secrets or bodies.
        print(f"refused: {exc.reason}")
        return 2
    except Exception:  # noqa: BLE001 - unexpected faults still fail closed
        print("refused: unexpected_error")
        return 2
    if result["mode"] == "dry_run":
        print(json.dumps({"mode": "dry_run", "seed": result["seed"]},
                         indent=2, sort_keys=True))
        print("dry run only; re-run with --write to persist the seed")
    else:
        print(json.dumps({"mode": "write", "persisted": result.get("persisted") is True,
                          "ticket_id": result["ticket_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
