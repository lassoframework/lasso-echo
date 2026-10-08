"""Default-off, run-scoped Instagram source capture. No DB ingestion or publishing.

API contracts: https://docs.apify.com/api/v2/actors-runs-post,
https://docs.apify.com/api/v2/actor-run-get and
https://docs.apify.com/api/v2/dataset-items-get. No undocumented response-ID
headers, latest-run lookup, guessed mapping, or retry of an Actor start.

The caller owns a persistent, private journal directory and stable request_id.
An uncertain start remains fenced indefinitely for operator reconciliation.
Known runs can be read again without starting another Actor. A local journal
cannot coordinate multiple hosts: all starts for a request must share this
journal. This module is intentionally not wired into the collector yet.

raw_bytes preserves the exact raw HTTP response bytes before content decoding
or JSON parsing. A gzip response is kept compressed; parsing uses a separately
bounded decoded copy. The artifact is never reconstructed or truncated.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import zlib
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

API_ROOT = "https://api.apify.com/v2"
ACTOR = "apify~instagram-post-scraper"
MAX_CAPTURE_BYTES = 2_000_000
_ID = re.compile(r"^[A-Za-z0-9]{1,128}$")
_HANDLE = re.compile(r"^[a-z0-9._]{1,30}$")
_PENDING = {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}


class _SafeHoldReason(Enum):
    """Closed set of static reasons; provider strings never become diagnostics."""

    FAILED = "run-scoped capture failed; holding"
    DUPLICATE_JSON = "ambiguous duplicate JSON keys; holding"
    JSON_SIZE = "provider JSON byte limit exceeded; holding"
    GZIP_JSON = "ambiguous or oversized gzip JSON; holding"
    NONFINITE_JSON = "nonfinite JSON value; holding"
    INVALID_JSON = "invalid provider JSON; holding"
    JOURNAL_UNAVAILABLE = "private durable start journal unavailable; holding"
    JOURNAL_PERMISSIONS = "start journal must be private; holding"
    BINDING_CHANGED = "request binding changed; holding"
    UNCERTAIN_START = "uncertain Actor start is durably fenced; holding"
    JOURNAL_CONFLICT = "Actor start journal conflict; holding"
    TOKEN_INVALID = "APIFY_TOKEN missing or invalid; holding"
    UNTRUSTED_ENDPOINT = "untrusted provider endpoint; holding"
    HTTP_FAILED = "provider HTTP request failed; holding"
    ENDPOINT_MISMATCH = "provider response endpoint mismatch; holding"
    NOT_JSON = "provider response is not JSON; holding"
    TRANSPORT_FAILED = "provider transport failed; holding"
    ENCODING_UNSUPPORTED = "unsupported provider content encoding; holding"
    RESPONSE_SIZE = "provider response exceeds byte limit; holding"
    WIRE_SIZE = "provider response exceeds wire byte limit; holding"
    ENTITY_SIZE = "provider response exceeds entity byte limit; holding"
    GZIP_RESPONSE = "ambiguous or incomplete gzip response; holding"
    LENGTH_MISMATCH = "provider response length mismatch; holding"
    ENCODING_INVALID = "invalid provider response encoding; holding"
    EMPTY_RESPONSE = "empty provider response; holding"
    RUN_MISSING = "provider run object missing; holding"
    RUN_ID_MISSING = "provider run identity missing or invalid; holding"
    RUN_ID_MISMATCH = "provider run/dataset/actor identity mismatch; holding"
    RUN_STATUS_UNKNOWN = "unknown provider run status; holding"
    DATASET_EMPTY = "empty or invalid dataset; holding"
    ITEM_ID_MISSING = "missing item identity; holding"
    ITEM_HANDLE_MISMATCH = "item handle identity mismatch or ambiguity; holding"
    ITEM_OWNER_MISMATCH = "item owner identity mismatch or ambiguity; holding"
    POST_TEXT_MISSING = "dataset contains no actual post text evidence; holding"
    MAPPING_INVALID = "explicit mapped Instagram identity/locator invalid; holding"
    BINDING_REQUIRED = "tenant/account/revision/request binding required; holding"
    MAPPING_PROOF_REQUIRED = "frozen mapping evidence required; holding"
    ACTOR_ID_REQUIRED = "independently configured Actor ID required; holding"
    LIMITS_INVALID = "bounded capture limits invalid; holding"
    JOURNAL_REQUIRED = "durable start journal required; holding"
    BINDING_SECRET = "secret detected in capture binding; holding"
    RUN_SECRET = "secret detected in provider run; holding"
    JOURNAL_ACTOR_MISMATCH = "journal Actor identity mismatch; holding"
    POLLING_TIMEOUT = "capture polling timeout; known run remains fenced"
    RUN_FAILED = "Actor run failed or aborted; holding"
    DATASET_TIMEOUT = "capture dataset timeout; holding"
    DATASET_SIZE = "dataset entity byte limit exceeded; holding"
    DATASET_SECRET = "secret detected in provider dataset; holding"
    DATASET_COUNT = "dataset item limit exceeded; holding"
    TIMESTAMP_INVALID = "timezone-aware capture timestamp required; holding"


class CaptureHold(Exception):
    """Sanitize arguments before raising, including injected client failures."""

    def __init__(self, reason):
        # Never call str(reason): arbitrary provider objects may expose secrets.
        self.safe_reason = _SafeHoldReason.FAILED
        if type(reason) is str:
            try:
                self.safe_reason = _SafeHoldReason(reason)
            except ValueError:
                pass
        elif type(reason) is _SafeHoldReason:
            self.safe_reason = reason
        super().__init__(self.safe_reason.value)


@dataclass
class RunCaptureResult:
    ok: bool
    raw_bytes: bytes = b""
    sha256: str = ""
    provenance: dict = field(default_factory=dict)
    reason: str = ""


def _json(raw, max_bytes=MAX_CAPTURE_BYTES):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise CaptureHold("ambiguous duplicate JSON keys; holding")
            obj[key] = value
        return obj
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= max_bytes:
            raise CaptureHold("provider JSON byte limit exceeded; holding")
        if isinstance(raw, bytes) and raw.startswith(b"\x1f\x8b"):
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = decoder.decompress(raw, max_bytes + 1)
            if (len(raw) > max_bytes or not decoder.eof
                    or decoder.unconsumed_tail or decoder.unused_data):
                raise CaptureHold("ambiguous or oversized gzip JSON; holding")
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              CaptureHold("nonfinite JSON value; holding")))
    except CaptureHold:
        raise
    except Exception:
        raise CaptureHold("invalid provider JSON; holding") from None


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _secret_present(value, token):
    # Reject instead of modifying source bytes, including JSON-escaped values.
    variants = (token, quote(token, safe=""), quote(token, safe="").lower())
    if isinstance(value, bytes):
        return any(v.encode() in value for v in variants)
    if isinstance(value, str):
        return any(v in value for v in variants)
    if isinstance(value, dict):
        return any(_secret_present(k, token) or _secret_present(v, token)
                   for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_secret_present(v, token) for v in value)
    return False


class SQLiteStartJournal:
    """Atomic durable fence. Never delete a pending entry to retry a start."""

    def __init__(self, path):
        self.path = Path(path)
        # The operator supplies an existing durable directory, not /tmp.
        if not self.path.parent.is_dir() or self.path.is_symlink():
            raise CaptureHold("private durable start journal unavailable; holding")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            if not self.path.is_file() or self.path.stat().st_mode & 0o077:
                raise CaptureHold("start journal must be private; holding") from None
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS starts "
                       "(request_key TEXT PRIMARY KEY, binding TEXT NOT NULL, "
                       "run_id TEXT, dataset_id TEXT, actor_id TEXT)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def claim(self, request_id, binding):
        key = hashlib.sha256(request_id.encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT binding,run_id,dataset_id,actor_id FROM starts "
                             "WHERE request_key=?", (key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO starts(request_key,binding) VALUES(?,?)",
                           (key, binding))
                return key, None
            if row[0] != binding:
                raise CaptureHold("request binding changed; holding")
            if not all(row[1:]):
                raise CaptureHold("uncertain Actor start is durably fenced; holding")
            return key, row[1:]

    def bind_run(self, key, binding, run_id, dataset_id, actor_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE starts SET run_id=?,dataset_id=?,actor_id=? "
                                 "WHERE request_key=? AND binding=? AND run_id IS NULL",
                                 (run_id, dataset_id, actor_id, key, binding)).rowcount
            if changed != 1:
                raise CaptureHold("Actor start journal conflict; holding")


class RunScopedApifyClient:
    """Authenticated transport. No redirects or retries; streaming raw body."""

    def __init__(self, token=None, transport=None):
        self._token = token if token is not None else os.getenv("APIFY_TOKEN", "")
        self._transport = transport  # Optional httpx transport for offline tests.

    def token(self):
        return self._token

    def request(self, method, url, *, payload=None, max_bytes=MAX_CAPTURE_BYTES,
                timeout=30):
        import httpx
        if (not isinstance(self._token, str) or not self._token
                or any(c.isspace() for c in self._token)):
            raise CaptureHold("APIFY_TOKEN missing or invalid; holding")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "api.apify.com"
                or not parsed.path.startswith("/v2/") or "token" in parsed.query.lower()):
            raise CaptureHold("untrusted provider endpoint; holding")
        try:
            with httpx.Client(transport=self._transport, timeout=timeout,
                              follow_redirects=False, trust_env=False) as client:
                with client.stream(method, url, json=payload,
                                   headers={"Authorization": "Bearer " + self._token,
                                            "Accept": "application/json",
                                            "Accept-Encoding": "identity"}) as response:
                    if response.status_code != (201 if method == "POST" else 200):
                        raise CaptureHold("provider HTTP request failed; holding")
                    if str(response.url) != url:
                        raise CaptureHold("provider response endpoint mismatch; holding")
                    if response.headers.get("content-type", "").split(";", 1)[0].lower().strip() != "application/json":
                        raise CaptureHold("provider response is not JSON; holding")
                    return _entity_bytes(response, max_bytes)
        except CaptureHold:
            raise
        except Exception:
            # Never expose exception strings, URLs, headers, or provider errors.
            raise CaptureHold("provider transport failed; holding") from None


def _entity_bytes(response, max_bytes):
    encoding = response.headers.get("content-encoding", "identity").lower().strip()
    if encoding not in ("", "identity", "gzip"):
        raise CaptureHold("unsupported provider content encoding; holding")
    length = response.headers.get("content-length")
    if length is not None:
        if not length.isdigit() or int(length) > max_bytes:
            raise CaptureHold("provider response exceeds byte limit; holding")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    wire_size = 0
    result = bytearray()
    raw_result = bytearray()
    try:
        for chunk in response.iter_raw(chunk_size=65536):
            wire_size += len(chunk)
            if wire_size > max_bytes:
                raise CaptureHold("provider response exceeds wire byte limit; holding")
            raw_result.extend(chunk)
            entity = decoder.decompress(chunk, max_bytes - len(result) + 1) if decoder else chunk
            result.extend(entity)
            if len(result) > max_bytes or (decoder and decoder.unconsumed_tail):
                raise CaptureHold("provider response exceeds entity byte limit; holding")
        if decoder and (not decoder.eof or decoder.unused_data):
            raise CaptureHold("ambiguous or incomplete gzip response; holding")
        if length is not None and int(length) != wire_size:
            raise CaptureHold("provider response length mismatch; holding")
    except CaptureHold:
        raise
    except Exception:
        raise CaptureHold("invalid provider response encoding; holding") from None
    if not result:
        raise CaptureHold("empty provider response; holding")
    return bytes(raw_result)


def _run(raw, expected_actor_id, *, run_id=None, dataset_id=None,
         max_bytes=MAX_CAPTURE_BYTES):
    obj = _json(raw, max_bytes)
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        raise CaptureHold("provider run object missing; holding")
    for key in ("id", "defaultDatasetId", "actId"):
        if not isinstance(data.get(key), str) or not _ID.fullmatch(data[key]):
            raise CaptureHold("provider run identity missing or invalid; holding")
    if (data["actId"] != expected_actor_id
            or (run_id is not None and data["id"] != run_id)
            or (dataset_id is not None and data["defaultDatasetId"] != dataset_id)):
        raise CaptureHold("provider run/dataset/actor identity mismatch; holding")
    if data.get("status") not in _PENDING | {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
        raise CaptureHold("unknown provider run status; holding")
    return data


def _verify_items(items, handle, owner_id):
    if not isinstance(items, list) or not items:
        raise CaptureHold("empty or invalid dataset; holding")
    for item in items:
        if not isinstance(item, dict) or not item:
            raise CaptureHold("missing item identity; holding")
        handles = [item[k] for k in ("ownerUsername", "username", "owner_username") if k in item]
        owners = [item[k] for k in ("ownerId", "owner_id", "igUserId", "userId") if k in item]
        if not handles or not owners:
            raise CaptureHold("missing item identity; holding")
        # Conflicting alias fields must never be hidden by a first-match lookup.
        if any(not isinstance(v, str) or v.strip().lstrip("@").lower() != handle for v in handles):
            raise CaptureHold("item handle identity mismatch or ambiguity; holding")
        if any(type(v) not in (str, int) or str(v) != owner_id for v in owners):
            raise CaptureHold("item owner identity mismatch or ambiguity; holding")
    if not any(isinstance(item.get("caption"), str) and item["caption"].strip()
               for item in items):
        raise CaptureHold("dataset contains no actual post text evidence; holding")


def capture_social_source_run(
    *, mapped_handle, mapped_provider_account_id, mapped_source_locator,
    gym_id, echo_account_key, mapping_revision, mapping_evidence,
    source_revision, request_id, expected_actor_id, journal,
    max_total_charge_usd, enabled=False, client=None, lookback_days=90,
    results_limit=500, max_bytes=MAX_CAPTURE_BYTES, timeout_seconds=180,
    poll_interval=1, monotonic=time.monotonic, sleep=time.sleep, now=None,
):
    """Return verified original response bytes; uncertainty returns no artifact.

    expected_actor_id is the independently configured Apify Actor's real ID;
    ACTOR is its endpoint alias. mapped IDs/locator come from portal mapping,
    never Apify. enabled=True must be authorized by the caller. No retries of
    POST, aborts, account changes, writes to portal, or live activation here.
    """
    if enabled is not True:
        return RunCaptureResult(False, reason="run-scoped Apify capture disabled")
    try:
        # Freeze the entire JSON tree before validation, journal binding or any
        # client callback. A shallow dict copy still shares nested caller data,
        # allowing later mutations to change validated evidence or provenance.
        mapping_evidence = _json(_canonical(mapping_evidence))
        handle = str(mapped_handle or "").strip().lstrip("@").lower()
        owner_id = str(mapped_provider_account_id or "")
        locator = urlsplit(str(mapped_source_locator or ""))
        if (not _HANDLE.fullmatch(handle) or not re.fullmatch(r"[0-9]+", owner_id)
                or locator.scheme != "https" or locator.netloc not in ("instagram.com", "www.instagram.com")
                or locator.path.strip("/").lower() != handle or locator.query or locator.fragment):
            raise CaptureHold("explicit mapped Instagram identity/locator invalid; holding")
        if any(not isinstance(v, str) or not v.strip() for v in
               (gym_id, echo_account_key, mapping_revision, source_revision, request_id)):
            raise CaptureHold("tenant/account/revision/request binding required; holding")
        if not isinstance(mapping_evidence, dict) or not mapping_evidence:
            raise CaptureHold("frozen mapping evidence required; holding")
        if not isinstance(expected_actor_id, str) or not _ID.fullmatch(expected_actor_id):
            raise CaptureHold("independently configured Actor ID required; holding")
        if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CAPTURE_BYTES
                or type(results_limit) is not int or not 1 <= results_limit <= 500
                or type(lookback_days) is not int or not 1 <= lookback_days <= 90
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 600
                or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval)
                or not 0 < poll_interval <= 30
                or type(max_total_charge_usd) not in (int, float)
                or not math.isfinite(max_total_charge_usd) or not 0 < max_total_charge_usd <= 5):
            raise CaptureHold("bounded capture limits invalid; holding")
        if not isinstance(journal, SQLiteStartJournal):
            raise CaptureHold("durable start journal required; holding")
        client = client or RunScopedApifyClient()
        token = client.token()
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise CaptureHold("APIFY_TOKEN missing or invalid; holding")
        mapping = dict(gym_id=gym_id, echo_account_key=echo_account_key,
                       mapping_revision=mapping_revision, mapping_evidence=mapping_evidence,
                       source_revision=source_revision, account_handle=handle,
                       provider_account_id=owner_id, source_locator=mapped_source_locator)
        payload = {"username": [handle], "resultsLimit": results_limit,
                   "onlyPostsNewerThan": f"{lookback_days} days",
                   "skipPinnedPosts": True, "dataDetailLevel": "detailedData"}
        bound = {"mapping": mapping, "input": payload, "actor": ACTOR,
                 "expected_actor_id": expected_actor_id, "max_bytes": max_bytes,
                 "max_total_charge_usd": max_total_charge_usd,
                 "timeout_seconds": timeout_seconds}
        if _secret_present(bound, token) or _secret_present(request_id, token):
            raise CaptureHold("secret detected in capture binding; holding")
        binding = hashlib.sha256(_canonical(bound)).hexdigest()
        key, known = journal.claim(request_id, binding)
        deadline = monotonic() + timeout_seconds
        if known is None:
            start_url = f"{API_ROOT}/actors/{ACTOR}/runs?" + urlencode({
                "restartOnError": "false", "timeout": timeout_seconds,
                "maxItems": results_limit, "maxTotalChargeUsd": max_total_charge_usd})
            raw = client.request("POST", start_url, payload=payload,
                                 max_bytes=max_bytes, timeout=min(30, timeout_seconds))
            run = _run(raw, expected_actor_id, max_bytes=max_bytes)
            if _secret_present(run, token) or _secret_present(raw, token):
                raise CaptureHold("secret detected in provider run; holding")
            run_id, dataset_id = run["id"], run["defaultDatasetId"]
            journal.bind_run(key, binding, run_id, dataset_id, run["actId"])
        else:
            run_id, dataset_id, actor_id = known
            if (actor_id != expected_actor_id
                    or any(not isinstance(v, str) or not _ID.fullmatch(v)
                           for v in known)):
                raise CaptureHold("journal Actor identity mismatch; holding")
        # Always independently read the exact run, even if POST said SUCCEEDED.
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CaptureHold("capture polling timeout; known run remains fenced")
            raw = client.request("GET", f"{API_ROOT}/actor-runs/{run_id}",
                                 max_bytes=max_bytes, timeout=min(30, remaining))
            run = _run(raw, expected_actor_id, run_id=run_id, dataset_id=dataset_id,
                       max_bytes=max_bytes)
            if _secret_present(run, token) or _secret_present(raw, token):
                raise CaptureHold("secret detected in provider run; holding")
            if run["status"] == "SUCCEEDED":
                break
            if run["status"] not in _PENDING:
                raise CaptureHold("Actor run failed or aborted; holding")
            sleep(min(poll_interval, max(0, deadline - monotonic())))
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CaptureHold("capture dataset timeout; holding")
        url = f"{API_ROOT}/datasets/{dataset_id}/items?" + urlencode({
            "format": "json", "clean": "false", "skipHidden": "false",
            "skipEmpty": "false", "offset": 0, "limit": results_limit})
        raw = client.request("GET", url, max_bytes=max_bytes, timeout=min(30, remaining))
        if monotonic() > deadline:
            raise CaptureHold("capture dataset timeout; holding")
        if type(raw) is not bytes or not 1 <= len(raw) <= max_bytes:
            raise CaptureHold("dataset entity byte limit exceeded; holding")
        items = _json(raw, max_bytes)
        if _secret_present(raw, token) or _secret_present(items, token):
            raise CaptureHold("secret detected in provider dataset; holding")
        _verify_items(items, handle, owner_id)
        if len(items) > results_limit:
            raise CaptureHold("dataset item limit exceeded; holding")
        stamp = now or datetime.now(timezone.utc)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise CaptureHold("timezone-aware capture timestamp required; holding")
        digest = hashlib.sha256(raw).hexdigest()
        provenance = {**mapping, "source_kind": "social", "source_url": url,
                      "capture_provider": "apify", "provider_actor_id": ACTOR,
                      "provider_response_id": f"apify:run:{run_id}:dataset:{dataset_id}",
                      "provider_run_id": run_id, "provider_dataset_id": dataset_id,
                      "provider_run_actor_id": expected_actor_id, "run_status": "SUCCEEDED",
                      "byte_count": len(raw), "items_count": len(items),
                      "bytes_sha256": digest, "lookback_days": lookback_days,
                      "results_limit": results_limit,
                      "identity_verified": True, "fetched_at": stamp.isoformat(),
                      "capture_window": {"lookback_days": lookback_days,
                                         "results_limit": results_limit},
                      "byte_representation": "http_raw_response_before_content_decoding"}
        return RunCaptureResult(True, raw, digest, provenance)
    except CaptureHold as exc:
        # An injected subclass can override __str__ or forge instance fields.
        # Only the closed enum on our exact exception type is trusted.
        candidate = vars(exc).get("safe_reason") if type(exc) is CaptureHold else None
        safe_reason = (candidate if type(candidate) is _SafeHoldReason
                       else _SafeHoldReason.FAILED)
        return RunCaptureResult(False, reason=safe_reason.value)
    except Exception:
        return RunCaptureResult(False, reason="run-scoped capture failed; holding")
