"""Authenticated Echo -> FIXER row-first business-ticket transport.

This is the narrow production client for Scout's
``POST /fixer/business-ticket`` boundary.  Its caller must already own all of
the durable identity needed by that endpoint:

* a deterministic submission UUID for the domain event;
* the exact portal client UUID resolved from the originating Echo account;
* one allowlisted, domain-owned target (calendar row/status or Drive folder).

Nothing in this module reads ticket text, asks a model to choose a target, or
extracts an id from a Slack/portal message.  A human-originated report currently
has no safe caller unless the originating domain operation also records those
structured values.  In particular, an Alex-style "my Drive folder is broken"
message cannot safely select a folder when an account may own more than one.

The shared ``FIXER_OPS_SECRET`` is sent only in the request header.  The body is
the exact versioned seed accepted by Scout.  Missing configuration, malformed
identity, redirects, non-success responses, oversized/invalid bodies, and request
errors all fail closed without logging credentials or response bodies.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable


SCHEMA_VERSION = 1
CONTRACT_VERSION = "echo-business-evidence-v1"
DEFAULT_BASE_URL = "https://wrangler-production.up.railway.app"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAPPING_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_BYTES = 32 * 1024

SOURCE = "ops_fix"
MEDIA_INCIDENT_NAMESPACE = uuid.UUID("4fdf46e1-68a6-4ced-9372-26cc937bdbe8")

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)
_ROW_ID = re.compile(r"[A-Za-z0-9_-]{6,80}\Z")
_STATUS = re.compile(r"[a-z_]{2,32}\Z")
_FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{3,200}\Z")
_REQUEST_KEY = re.compile(r"[0-9a-f]{64}\Z")
_GYM_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z")
_INCIDENT_KIND = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_INCIDENT_VALUE = re.compile(r"[A-Za-z0-9_.:+-]{0,160}\Z")
_EVENT_KEYS = frozenset({
    "schema_version", "contract_version", "submission_key", "client_id",
    "source", "check_id", "params",
})


class BusinessSeedError(ValueError):
    """The producer did not provide one exact, allowlisted durable identity."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    reason: str
    ticket_id: str = ""
    request_key: str = ""
    outcome: str = ""


def _identity(submission_key: str, client_id: str) -> dict:
    if not isinstance(submission_key, str) or not _UUID.fullmatch(submission_key):
        raise BusinessSeedError("invalid submission UUID")
    if not isinstance(client_id, str) or not _UUID.fullmatch(client_id):
        raise BusinessSeedError("invalid portal client UUID")
    return {"submission_key": submission_key, "client_id": client_id,
            "source": SOURCE}


def calendar_row_seed(*, submission_key: str, client_id: str,
                      row_id: str, expected_status: str) -> dict:
    """Build a plan for one exact tenant-owned calendar row."""
    ident = _identity(submission_key, client_id)
    if not isinstance(row_id, str) or not _ROW_ID.fullmatch(row_id):
        raise BusinessSeedError("invalid calendar row id")
    if not isinstance(expected_status, str) or not _STATUS.fullmatch(expected_status):
        raise BusinessSeedError("invalid expected calendar status")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        **ident,
        "check_id": "calendar_row_status",
        "params": {"row_id": row_id, "expected_status": expected_status},
    }


def calendar_row_event(*, submission_key: str, client_id: str,
                       row_id: str, expected_status: str) -> dict:
    """Compatibility name used by authoritative calendar transition callers."""
    return calendar_row_seed(
        submission_key=submission_key, client_id=client_id, row_id=row_id,
        expected_status=expected_status,
    )


def media_source_seed(*, submission_key: str, client_id: str,
                      folder_id: str) -> dict:
    """Build a plan for one exact tenant-owned Drive folder source."""
    ident = _identity(submission_key, client_id)
    if not isinstance(folder_id, str) or not _FOLDER_ID.fullmatch(folder_id):
        raise BusinessSeedError("invalid Drive folder id")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        **ident,
        "check_id": "media_source_active",
        "params": {"folder_id": folder_id},
    }


def _default_get(url: str, *, params: dict, headers: dict, timeout: float):
    import requests
    return requests.get(url, params=params, headers=headers, timeout=timeout,
                        allow_redirects=False)


def _mapping_rows(table: str, params: dict, *, get=None,
                  timeout: float = DEFAULT_MAPPING_TIMEOUT_SECONDS) -> list:
    from . import config
    url, key = config.supabase_url(), config.supabase_service_key()
    if not url or not key:
        raise BusinessSeedError("portal client resolver unavailable")
    try:
        budget = float(timeout)
    except (TypeError, ValueError) as exc:
        raise BusinessSeedError("invalid mapping timeout") from exc
    if not 0 < budget <= 5:
        raise BusinessSeedError("invalid mapping timeout")
    try:
        response = (get or _default_get)(
            f"{url.rstrip('/')}/rest/v1/{table}", params=params,
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Accept": "application/json"}, timeout=budget)
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise BusinessSeedError("portal client resolver unavailable")
        rows = response.json()
    except BusinessSeedError:
        raise
    except Exception as exc:
        raise BusinessSeedError("portal client resolver unavailable") from exc
    if (not isinstance(rows, list) or len(rows) > 2
            or any(not isinstance(row, dict) for row in rows)):
        raise BusinessSeedError("portal client resolver unavailable")
    return rows


def resolve_portal_client_id(gym_key: str, *, bus=None, get=None,
                             timeout: float = DEFAULT_MAPPING_TIMEOUT_SECONDS) -> str:
    """Resolve one exact Echo account key to one portal UUID.

    The bounded service-role lookup reads only ``echo_intake_tokens`` in both
    directions and refuses zero, duplicate, malformed, or non-exact mappings.
    Display names, gym slugs, ticket text, and client-supplied values are never
    fallbacks.  Tests may inject the existing Bus resolver directly.
    """
    if not isinstance(gym_key, str) or not _GYM_KEY.fullmatch(gym_key):
        raise BusinessSeedError("invalid gym key")
    if bus is not None:
        resolver = getattr(bus, "portal_client_id", None)
        if not callable(resolver):
            raise BusinessSeedError("portal client resolver unavailable")
        client_id = resolver(gym_key)
    else:
        forward = _mapping_rows("echo_intake_tokens", {
            "echo_account_key": f"eq.{gym_key}",
            "select": "gym_id,echo_account_key", "limit": "2",
        }, get=get, timeout=timeout)
        if (len(forward) != 1 or forward[0].get("echo_account_key") != gym_key):
            raise BusinessSeedError("portal client identity unconfirmed")
        client_id = forward[0].get("gym_id")
        reverse = _mapping_rows("echo_intake_tokens", {
            "gym_id": f"eq.{client_id}",
            "select": "gym_id,echo_account_key", "limit": "2",
        }, get=get, timeout=timeout)
        if (len(reverse) != 1 or reverse[0].get("gym_id") != client_id
                or reverse[0].get("echo_account_key") != gym_key):
            raise BusinessSeedError("portal client identity unconfirmed")
    if not isinstance(client_id, str) or not _UUID.fullmatch(client_id):
        raise BusinessSeedError("portal client identity unconfirmed")
    return client_id


def media_incident_submission_key(*, gym_key: str, folder_id: str,
                                  incident_kind: str, source_id: str = "",
                                  prior_sync_status: str = "",
                                  prior_sync_requested_at: str = "") -> str:
    """UUIDv5 for one authoritative media connection/indexing incident.

    A retry against the unchanged source state produces the same key.  A source
    that later advances and encounters a new failure has a different prior
    status/request stamp and therefore creates a different incident.  No prose
    or wall-clock value participates in the identity.
    """
    if not isinstance(gym_key, str) or not _GYM_KEY.fullmatch(gym_key):
        raise BusinessSeedError("invalid gym key")
    if not isinstance(folder_id, str) or not _FOLDER_ID.fullmatch(folder_id):
        raise BusinessSeedError("invalid Drive folder id")
    if (not isinstance(incident_kind, str)
            or not _INCIDENT_KIND.fullmatch(incident_kind)):
        raise BusinessSeedError("invalid media incident kind")
    fields = (source_id, prior_sync_status, prior_sync_requested_at)
    if any(not isinstance(value, str) or not _INCIDENT_VALUE.fullmatch(value)
           for value in fields):
        raise BusinessSeedError("invalid media incident identity")
    identity = {
        "folder_id": folder_id,
        "gym_key": gym_key,
        "incident_kind": incident_kind,
        "prior_sync_requested_at": prior_sync_requested_at,
        "prior_sync_status": prior_sync_status,
        "source_id": source_id,
    }
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True)
    return str(uuid.uuid5(MEDIA_INCIDENT_NAMESPACE, canonical))


def media_incident_event(*, gym_key: str, folder_id: str,
                         incident_kind: str, source_id: str = "",
                         prior_sync_status: str = "",
                         prior_sync_requested_at: str = "", bus=None) -> dict:
    """Build Scout's row-first event from one exact media-domain failure."""
    client_id = resolve_portal_client_id(gym_key, bus=bus)
    submission_key = media_incident_submission_key(
        gym_key=gym_key, folder_id=folder_id, incident_kind=incident_kind,
        source_id=source_id, prior_sync_status=prior_sync_status,
        prior_sync_requested_at=prior_sync_requested_at,
    )
    return media_source_seed(submission_key=submission_key,
                             client_id=client_id, folder_id=folder_id)


def _valid_seed(seed: Any) -> bool:
    if not isinstance(seed, dict) or set(seed) != _EVENT_KEYS:
        return False
    try:
        ident = _identity(seed["submission_key"], seed["client_id"])
    except (BusinessSeedError, KeyError):
        return False
    if any(seed.get(key) != value for key, value in ident.items()):
        return False
    if seed.get("schema_version") != SCHEMA_VERSION \
            or seed.get("contract_version") != CONTRACT_VERSION:
        return False
    params = seed.get("params")
    if seed.get("check_id") == "calendar_row_status":
        return (isinstance(params, dict)
                and set(params) == {"row_id", "expected_status"}
                and isinstance(params["row_id"], str)
                and _ROW_ID.fullmatch(params["row_id"]) is not None
                and isinstance(params["expected_status"], str)
                and _STATUS.fullmatch(params["expected_status"]) is not None)
    if seed.get("check_id") == "media_source_active":
        return (isinstance(params, dict) and set(params) == {"folder_id"}
                and isinstance(params["folder_id"], str)
                and _FOLDER_ID.fullmatch(params["folder_id"]) is not None)
    return False


def _default_post(url: str, *, headers: dict, body: bytes, timeout: float):
    import requests
    return requests.post(url, headers=headers, data=body, timeout=timeout,
                         allow_redirects=False)


def send(seed: dict, *, base_url: str | None = None, secret: str | None = None,
         post: Callable[..., Any] | None = None,
         timeout: float = DEFAULT_TIMEOUT_SECONDS) -> SendResult:
    """Send one exact seed and confirm Scout's bounded acknowledgement."""
    if not _valid_seed(seed):
        return SendResult(False, "invalid_seed")
    shared_secret = secret if secret is not None else os.getenv("FIXER_OPS_SECRET", "")
    host = (base_url if base_url is not None
            else os.getenv("FIXER_SERVICE_URL", DEFAULT_BASE_URL)).rstrip("/")
    if not shared_secret or not host.startswith("https://"):
        return SendResult(False, "unavailable")
    try:
        budget = float(timeout)
    except (TypeError, ValueError):
        return SendResult(False, "invalid_timeout")
    if not 0 < budget <= 15:
        return SendResult(False, "invalid_timeout")
    body = json.dumps({"event": seed}, ensure_ascii=False, separators=(",", ":")).encode()
    try:
        response = (post or _default_post)(
            f"{host}/fixer/business-ticket",
            headers={
                "X-Fixer-Ops-Secret": shared_secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            timeout=budget,
        )
    except Exception:  # noqa: BLE001 - producer stays open and retries later
        return SendResult(False, "unavailable")
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in (200, 201):
        return SendResult(False, "rejected" if 400 <= status < 500 else "unavailable")
    raw = getattr(response, "content", b"")
    if isinstance(raw, str):
        raw = raw.encode()
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
        return SendResult(False, "invalid_response")
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SendResult(False, "invalid_response")
    if (not isinstance(value, dict)
            or set(value) != {"ok", "outcome", "ticket_id", "request_key"}
            or value.get("ok") is not True
            or value.get("outcome") not in {"created", "existing"}
            or not isinstance(value.get("ticket_id"), str)
            or _UUID.fullmatch(value["ticket_id"]) is None
            or not isinstance(value.get("request_key"), str)
            or _REQUEST_KEY.fullmatch(value["request_key"]) is None):
        return SendResult(False, "invalid_response")
    expected_status = 201 if value["outcome"] == "created" else 200
    if status != expected_status:
        return SendResult(False, "invalid_response")
    return SendResult(True, "", value["ticket_id"], value["request_key"],
                      value["outcome"])
