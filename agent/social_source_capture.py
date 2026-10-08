"""
social_source_capture.py — verified social SOURCE capture for the portal
source/brand bundle (portal contract at commit 8579400:
portal-brand-source-bundle-20261008/docs/echo/verified-source-brand-bundle.md
and its DRAFT_echo_source_brand_bundle.sql; mirrored by the ingest boundary
in agent/source_brand_ingest.py).

What this does, and only this:
  - Pulls a gym's public Instagram feed through the existing Apify run-sync
    path (agent/social_baseline.ApifyClient.fetch_posts_raw) and returns the
    EXACT raw response bytes, before any JSON parsing, plus a provenance
    record whose fields match the echo_source_captures contract exactly.
    The stored artifact is the raw bytes; JSON is never reconstructed as
    the artifact.

Hard rules (from the portal contract and the review findings):
  - The ONLY account locator is the mapped handle + mapped provider account
    ID + mapped Instagram profile URL supplied by the caller from the
    verified portal mapping. This module never derives, guesses, or looks up
    a locator from any other source (no echo_social_connections, no
    registry, no name inference).
  - Provider provenance stays strictly SEPARATE from the account locator:
    `source_url` is the Apify provider endpoint (api.apify.com/v2/...),
    `capture_provider`/`provider_response_id` identify who served the bytes;
    `source_locator` is the verified instagram.com profile URL and
    `provider_account_id` the verified account identity.
  - The dataset must be NONEMPTY and identity-verified: every item must
    belong to the mapped handle AND its owner account ID must match the
    mapped provider account ID EXACTLY. An empty dataset, a missing owner
    identity, or any mismatch HOLDS (fails closed): no artifact.
  - gym_id / echo_account_key / mapping_revision / mapping_evidence are
    REQUIRED: mapping_evidence is a nonempty dict, exactly the jsonb-object
    shape the contract column enforces.
  - Response size is hard-capped (default 2,000,000 bytes, the contract's
    raw_bytes ceiling) and enforced before the body is fully materialized
    by the client. Oversize responses fail, never truncate.
  - The Apify provider response identity (run/dataset header) is required;
    without it the response cannot be tied to a provider run and the
    capture HOLDS.
  - Tokens are never logged, stored, or embedded in provenance; error text
    is scrubbed of the token before it can travel anywhere.

This module is PURE capture: it writes nothing to any database. Persistence
(into echo_source_captures) belongs to the separately reviewed trusted
collector adapter (agent/source_brand_ingest.py); the caller hands that
adapter the returned CaptureResult (raw_bytes + provenance + sha256).

All I/O is injectable (Apify client, clock) so the whole path tests offline.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from .social_baseline import (
    APIFY_ACTOR_ID,
    APIFY_RUN_SYNC_URL,
    RESULTS_LIMIT,
    ApifyClient,
    ApifyError,
)

# The contract's raw_bytes ceiling: 1 through 2,000,000 bytes.
MAX_CAPTURE_BYTES = 2_000_000

# How far back the capture pulls. The contract wants actual post text
# evidence; 90 days mirrors the social-baseline window and keeps payloads
# well under the byte cap for a boutique-gym feed.
DEFAULT_LOOKBACK_DAYS = 90

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# The contract requires the social source_locator to be a verified
# instagram.com HTTPS profile/post locator (mirrors source_brand_ingest).
_IG_HOSTS = ("instagram.com", "www.instagram.com")


@dataclass
class CaptureResult:
    """A verified social source capture. raw_bytes is the exact Apify
    response body; provenance carries the echo_source_captures contract
    fields. Nothing here is written anywhere by this module."""
    ok: bool
    raw_bytes: bytes = b""
    sha256: str = ""
    provenance: dict = field(default_factory=dict)
    reason: str = ""


def _scrub(text, token):
    """Remove the token (and a bearer-prefixed form) from any string that
    could end up in an error or provenance field."""
    s = str(text or "")
    if token:
        s = s.replace(token, "***").replace(f"Bearer {token}", "***")
    return s


def _norm_handle(h):
    return str(h or "").strip().lstrip("@").lower()


def _item_identity(item):
    """(username, owner_id) an Apify instagram-post-scraper item claims, with
    both absent- and empty-tolerant. Returns (str|None, str|None)."""
    if not isinstance(item, dict):
        return None, None
    username = None
    for key in ("ownerUsername", "username", "owner_username"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            username = v.strip().lstrip("@")
            break
    owner_id = None
    for key in ("ownerId", "owner_id", "igUserId", "userId"):
        v = item.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            owner_id = s
            break
    return username, owner_id


def _verify_identity(items, mapped_handle, mapped_provider_account_id):
    """Fail-closed identity check over the parsed response items.

    The dataset must be NONEMPTY, and EVERY item must belong to the mapped
    handle AND carry an owner account ID that matches the mapped provider
    account ID EXACTLY. An empty dataset or any missing/mismatched identity
    HOLDS."""
    if not items:
        return False, ("empty dataset: no post evidence to capture; holding")
    handle = _norm_handle(mapped_handle)
    pid = str(mapped_provider_account_id or "").strip()
    for item in items:
        username, owner_id = _item_identity(item)
        if username is None or owner_id is None:
            return False, ("response item is missing a verifiable account "
                           "identity (handle and owner ID); holding")
        if _norm_handle(username) != handle:
            return False, (
                f"response item belongs to @{_norm_handle(username)}, "
                f"not the mapped @{handle}; holding")
        if owner_id != pid:
            return False, (
                "response item owner account ID does not exactly match the "
                "mapped provider account ID; holding")
    return True, ""


def _valid_ig_locator(locator):
    """The mapped Instagram profile/post locator must be an HTTPS URL on
    instagram.com (the contract + ingest boundary shape)."""
    try:
        parsed = urlparse(str(locator or "").strip())
    except Exception:  # noqa: BLE001
        return False
    return (parsed.scheme == "https"
            and parsed.hostname in _IG_HOSTS
            and bool(parsed.path.strip("/")))


def capture_social_source(
    *,
    mapped_handle: str,
    mapped_provider_account_id: str,
    mapped_source_locator: str,
    gym_id: str,
    echo_account_key: str,
    mapping_revision: str,
    mapping_evidence: dict,
    source_revision: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_bytes: int = MAX_CAPTURE_BYTES,
    client: Optional[ApifyClient] = None,
    now=None,
) -> CaptureResult:
    """Capture one gym's social source bytes through Apify.

    `mapped_handle` / `mapped_provider_account_id` / `mapped_source_locator`
    MUST come from the verified portal mapping, whose frozen proof is
    `mapping_revision` + `mapping_evidence` (a nonempty dict, the contract's
    jsonb-object shape). No other locator is consulted. Returns a
    CaptureResult; on any failure ok=False with a token-free reason and NO
    raw bytes."""
    handle = _norm_handle(mapped_handle)
    if not handle or not _HANDLE_RE.match(handle):
        return CaptureResult(ok=False, reason=(
            "mapped_handle is missing or not a plausible instagram handle; "
            "only the mapped account locator is accepted"))
    pid = str(mapped_provider_account_id or "").strip()
    if not pid:
        return CaptureResult(ok=False, reason=(
            "mapped_provider_account_id is required for a social capture "
            "(the contract's verified account identity)"))
    locator = str(mapped_source_locator or "").strip()
    if not _valid_ig_locator(locator):
        return CaptureResult(ok=False, reason=(
            "mapped_source_locator must be a verified HTTPS instagram.com "
            "profile/post locator from the mapping"))
    if not str(gym_id or "").strip():
        return CaptureResult(ok=False, reason="gym_id is required")
    if not str(echo_account_key or "").strip():
        return CaptureResult(ok=False, reason="echo_account_key is required")
    if not str(source_revision or "").strip():
        return CaptureResult(ok=False, reason="source_revision is required")
    if not str(mapping_revision or "").strip():
        return CaptureResult(ok=False, reason="mapping_revision is required")
    if not isinstance(mapping_evidence, dict) or not mapping_evidence:
        return CaptureResult(ok=False, reason=(
            "mapping_evidence must be a nonempty dict (the contract's "
            "jsonb-object shape); frozen mapping proof is required"))

    if client is None:
        client = ApifyClient()
    token = client.token() if hasattr(client, "token") else ""
    if not token:
        return CaptureResult(ok=False, reason=(
            "APIFY_TOKEN not set; social source capture is inert"))

    if max_bytes < 1 or max_bytes > MAX_CAPTURE_BYTES:
        return CaptureResult(ok=False, reason=(
            f"max_bytes must be within 1..{MAX_CAPTURE_BYTES} "
            "(the contract raw_bytes ceiling)"))

    fetched_at = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)

    try:
        raw, provider_response_id = client.fetch_posts_raw(
            handle, lookback_days,
            results_limit=RESULTS_LIMIT, max_bytes=max_bytes)
    except ApifyError as exc:
        return CaptureResult(ok=False, reason=_scrub(
            f"apify pull failed: {exc}", token))
    except Exception as exc:  # noqa: BLE001 — never leak raw client errors
        return CaptureResult(ok=False, reason=(
            f"apify pull failed: {type(exc).__name__}"))

    if not raw or len(raw) > max_bytes:
        return CaptureResult(ok=False, reason=(
            "response empty or over the size cap after fetch; refusing"))
    if not provider_response_id:
        return CaptureResult(ok=False, reason=(
            "no Apify provider response identity (run/dataset header); the "
            "response cannot be tied to a provider run; holding"))

    # Parse a COPY for identity verification only. The parsed form is never
    # stored or returned — the artifact is the raw entity bytes. A gzipped
    # response is decompressed into the throwaway copy so identity can still
    # be verified; the stored bytes stay exactly as served.
    text = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = gzip.decompress(raw).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            text = None
    if text is None:
        return CaptureResult(ok=False, reason=(
            "apify response is not parseable JSON; refusing capture"))
    try:
        parsed = json.loads(text)
    except ValueError:
        return CaptureResult(ok=False, reason=(
            "apify response is not parseable JSON; refusing capture"))
    if not isinstance(parsed, list):
        return CaptureResult(ok=False, reason=(
            "apify returned a non-list dataset; refusing capture"))

    ok, why = _verify_identity(parsed, handle, pid)
    if not ok:
        return CaptureResult(ok=False, reason=why)

    digest = hashlib.sha256(raw).hexdigest()
    provenance = {
        # echo_source_captures contract fields (portal bundle @ 8579400).
        "gym_id": str(gym_id).strip(),
        "echo_account_key": str(echo_account_key).strip(),
        "source_kind": "social",
        # The PROVIDER endpoint that served the bytes...
        "source_url": APIFY_RUN_SYNC_URL,
        "capture_provider": "apify",
        "provider_response_id": provider_response_id,
        "provider_actor_id": APIFY_ACTOR_ID,
        # ...kept strictly separate from the ACCOUNT locator that was
        # requested (verified instagram.com profile URL + account identity).
        "source_locator": locator,
        "provider_account_id": pid,
        "account_handle": handle,
        "source_revision": str(source_revision).strip(),
        "mapping_revision": str(mapping_revision).strip(),
        "mapping_evidence": dict(mapping_evidence),
        "fetched_at": fetched_at.isoformat(),
        "byte_count": len(raw),
        "bytes_sha256": digest,
        "items_count": len(parsed),
        "lookback_days": int(lookback_days),
        "results_limit": int(RESULTS_LIMIT),
        "identity_verified": True,
        "token": None,  # tokens are never recorded; explicit tombstone
    }
    return CaptureResult(
        ok=True,
        raw_bytes=raw,
        sha256=digest,
        provenance=provenance,
    )


__all__ = [
    "CaptureResult",
    "capture_social_source",
    "MAX_CAPTURE_BYTES",
    "DEFAULT_LOOKBACK_DAYS",
]
