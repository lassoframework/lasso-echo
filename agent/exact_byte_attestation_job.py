"""Armed signer-service refresh of the exact-byte lock attestation envelope.

Runs on a DEDICATED signer Railway service (railway.exact-byte-attester.json,
TEMPLATE ONLY) as an ALWAYS-ON single-replica worker. Railway's native cron
is documented as a 5-minute-minimum, best-effort schedule, which cannot keep a
<=300s signed envelope fresh, so this worker owns its cadence: it refreshes
immediately on start and then about every
AGENT_EXACT_BYTE_ATTESTATION_REFRESH_SECONDS (default 60, clamped 10..240).
Each refresh cycle:

  1. holds an exclusive in-flight lock for the worker's whole lifetime (a
     second overlapping worker skips and exits);
  2. rebuilds a FRESH <=300s Ed25519-signed attestation envelope from live
     Cloudflare REST lock readback + controlled public byte probe, reusing the
     privileged producer interfaces in
     scripts/exact_byte_sign_lock_attestation.py unchanged;
  3. refuses to run unless the seed-derived public key EQUALS the pinned
     AGENT_EXACT_BYTE_ADMIN_PUBLIC_KEY_B64 (a wrong or missing pin means
     zero uploads, not an overwrite the publisher would hold on), and
     verifies the signed envelope LOCALLY against the pinned identity and
     the derived administrator public key BEFORE any upload, so an
     unverified envelope can never replace a good control object;
  3b. validates envelope freshness (integer non-bool timestamps,
     observed_at <= now, age <=300s, expires_at > now, remaining TTL
     >=60s) BEFORE any upload, so a slow Cloudflare/public probe can
     never publish an already-unusable envelope;
  4. uploads to the PRIVATE MUTABLE R2 CONTROL bucket (separate from the
     locked media bucket) with the control WRITE token held only here;
  5. reads the object back, requires exact byte identity, re-verifies the
     signature/schema/identity and re-validates freshness (remaining TTL
     >=60s) before reporting success.

Each cycle retries boundedly (3 attempts, 2s/4s backoff). A failed cycle is
logged and the worker keeps running: it never silently exits on a transient
refresh failure, and the publisher's stale-envelope hold is never extended
(the publisher holds when the envelope is stale). SIGTERM/SIGINT stop the
worker gracefully between sleeps and cycles.

The publisher service holds ONLY a read-only control token and the pinned
public key; the signing seed, Cloudflare lock-read token and control write
token never leave this service. Everything defaults OFF: without
AGENT_EXACT_BYTE_ATTESTATION_JOB_ENABLED=true the worker logs and exits 0.
Logs are redacted: identity fields, counts and hashes only, never secrets.

Exit codes: 0 = ran and stopped gracefully (or disarmed/skipped),
2 = refused (in-flight lock unavailable).
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

ENV_ENABLED = "AGENT_EXACT_BYTE_ATTESTATION_JOB_ENABLED"
ENV_PROBE_KEY = "AGENT_EXACT_BYTE_PROBE_KEY"
ENV_SERVING_EVIDENCE_PATH = "AGENT_EXACT_BYTE_SERVING_EVIDENCE_PATH"
ENV_LOCK_PATH = "AGENT_EXACT_BYTE_ATTESTATION_LOCK_PATH"
ENV_CONTROL_ACCESS_KEY_ID = "AGENT_EXACT_BYTE_CONTROL_R2_ACCESS_KEY_ID"
ENV_CONTROL_SECRET = "AGENT_EXACT_BYTE_CONTROL_R2_SECRET_ACCESS_KEY"

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 4)
ENV_REFRESH_SECONDS = "AGENT_EXACT_BYTE_ATTESTATION_REFRESH_SECONDS"
DEFAULT_REFRESH_SECONDS = 60
MIN_REFRESH_SECONDS = 10
MAX_REFRESH_SECONDS = 240
#: Readback must leave the publisher at least this much fresh envelope life;
#: the 60s cron cadence plus <=300s freshness makes 60s the floor.
MIN_REMAINING_TTL_SECONDS = 60

_STOP = False


def _stop_requested(signum, _frame):
    global _STOP
    _STOP = True


def _log(event, **fields):
    """Redacted single-line log: never any secret, token, seed or body."""
    safe = {k: v for k, v in fields.items()
            if isinstance(v, (str, int, bool)) and "secret" not in k
            and "token" not in k and "seed" not in k and "key" != k}
    print(json.dumps({"job": "exact-byte-attester", "event": event,
                      **safe}, sort_keys=True), flush=True)


class _Refused(Exception):
    """Safe fixed reason; secrets and provider details are never included."""


def _signing_module():
    """Import the privileged producer with unchanged behavior."""
    root = str(Path(__file__).resolve().parent.parent / "scripts")
    if root not in sys.path:
        sys.path.insert(0, root)
    import exact_byte_sign_lock_attestation as signer
    return signer


def _required(environ, name):
    value = (environ.get(name, "") or "").strip()
    if not value:
        raise _Refused(f"{name} absent")
    return value


def _armed(environ):
    return (environ.get(ENV_ENABLED, "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _control_s3_client(account_id, environ):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=_required(environ, ENV_CONTROL_ACCESS_KEY_ID),
        aws_secret_access_key=_required(environ, ENV_CONTROL_SECRET),
        region_name="auto",
        config=Config(signature_version="s3v4",
                      connect_timeout=10, read_timeout=30,
                      retries={"max_attempts": 2}))


def _bounded_get(client, *, bucket, key, cap):
    from . import r2_immutable_media as media
    body = None
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        length = obj.get("ContentLength")
        if (not isinstance(length, int) or isinstance(length, bool)
                or not 0 < length <= cap):
            raise _Refused("control readback length invalid")
        body = obj["Body"]
        data = body.read(cap + 1)
        if not isinstance(data, bytes) or len(data) != length:
            raise _Refused("control readback truncated")
        return data
    except _Refused:
        raise
    except Exception:
        raise _Refused("control transport unavailable") from None
    finally:
        if body is not None:
            try:
                body.close()
            except Exception:
                pass


def _verified_envelope_bytes(envelope, *, public_key_bytes, account_id, bucket,
                             base):
    """Serialize + locally re-verify the envelope BEFORE any upload."""
    from . import exact_byte_runtime as runtime
    from . import r2_immutable_media as media
    raw = (json.dumps(envelope) + "\n").encode("utf-8")
    if len(raw) > media.MAX_ATTESTATION_BYTES:
        raise _Refused("envelope exceeds byte limit")
    source = runtime._ServingEvidenceLockSource(
        lambda: raw, public_key_bytes,
        account_id=account_id, bucket=bucket, base=base)
    try:
        doc, _digest = source.verified_document()
    except media.ImmutableMediaError:
        raise _Refused("fresh envelope failed local verification") from None
    return raw, doc


def _build_envelope(environ, *, media_s3, session):
    signer = _signing_module()
    account_id = _required(environ, runtime_env("ENV_ACCOUNT_ID"))
    bucket = _required(environ, runtime_env("ENV_BUCKET"))
    base = _required(environ, runtime_env("ENV_PUBLIC_BASE_URL"))
    seed = signer._load_private_key(env=environ)
    token = _required(environ, signer.R2_API_TOKEN_ENV)
    serving = signer._load_serving_evidence(
        _required(environ, ENV_SERVING_EVIDENCE_PATH))
    if media_s3 is None:
        media_s3 = signer._s3_client(account_id, environ)
    envelope = signer.attest(
        s3=media_s3, session=session, account_id=account_id, bucket=bucket,
        public_base_url=base, probe_key=_required(environ, ENV_PROBE_KEY),
        seed=seed, token=token, serving_evidence=serving)
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
    public_key = signer._signer(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    # The seed-derived key must BE the publisher's pinned administrator key.
    # A wrong or missing pin means every publisher verification would hold,
    # so refuse BEFORE any upload instead of overwriting a good object.
    from . import exact_byte_runtime as runtime
    try:
        pinned = runtime._admin_public_key(environ)
    except runtime._ConfigHold:
        raise _Refused("pinned administrator key absent") from None
    if pinned != public_key:
        raise _Refused("signing identity mismatch")
    return account_id, bucket, base, public_key, envelope


def runtime_env(name):
    from . import exact_byte_runtime as runtime
    return getattr(runtime, name)


def _require_fresh_envelope(doc, *, now):
    """Reject malformed/stale/future/expired docs BEFORE overwriting control.

    ``now`` is the local clock at decision time; a slow Cloudflare REST
    lock readback or public probe must never shrink the envelope below the
    publisher's usable window. All failures are fixed, redacted reasons.
    """
    from . import r2_immutable_media as media
    observed = doc.get("observed_at")
    expires = doc.get("expires_at")
    if (not isinstance(observed, int) or isinstance(observed, bool)
            or not isinstance(expires, int) or isinstance(expires, bool)):
        raise _Refused("attestation timestamps malformed")
    if observed > now:
        raise _Refused("attestation from the future")
    if now - observed > media.MAX_FRESHNESS_SECONDS:
        raise _Refused("attestation stale")
    if not observed < expires <= observed + media.MAX_FRESHNESS_SECONDS:
        raise _Refused("attestation lifetime invalid")
    remaining = expires - now
    if remaining <= 0:
        raise _Refused("attestation expired")
    if remaining < MIN_REMAINING_TTL_SECONDS:
        raise _Refused("attestation remaining ttl insufficient")


def _refresh_once(environ, *, media_s3=None, control_s3=None, session=None):
    """One full refresh cycle; raises _Refused on any failed proof."""
    from . import exact_byte_runtime as runtime
    from . import r2_immutable_media as media
    import requests

    owned_session = session is None
    if owned_session:
        session = requests.Session()
        session.trust_env = False
    try:
        account_id, bucket, base, public_key, envelope = _build_envelope(
            environ, media_s3=media_s3, session=session)
        raw, doc = _verified_envelope_bytes(
            envelope, public_key_bytes=public_key, account_id=account_id,
            bucket=bucket, base=base)
        _require_fresh_envelope(doc, now=int(time.time()))

        control_account = _required(environ, runtime.ENV_CONTROL_ACCOUNT_ID)
        control_bucket = _required(environ, runtime.ENV_CONTROL_BUCKET)
        control_key = _required(environ, runtime.ENV_CONTROL_KEY)
        if control_s3 is None:
            control_s3 = _control_s3_client(control_account, environ)
        try:
            control_s3.put_object(Bucket=control_bucket, Key=control_key,
                                  Body=raw)
        except Exception:
            raise _Refused("control upload unavailable") from None

        readback = _bounded_get(control_s3, bucket=control_bucket,
                                key=control_key,
                                cap=media.MAX_ATTESTATION_BYTES)
        if readback != raw:
            raise _Refused("control readback byte mismatch")
        source = runtime._ServingEvidenceLockSource(
            lambda: readback, public_key, account_id=account_id,
            bucket=bucket, base=base)
        try:
            proven, _ = source.verified_document()
        except media.ImmutableMediaError:
            raise _Refused("control readback failed verification") from None
        remaining = int(proven["expires_at"]) - int(time.time())
        _require_fresh_envelope(proven, now=int(time.time()))
        _log("refreshed", account_id=account_id, bucket=bucket,
             control_bucket=control_bucket, observed_at=doc["observed_at"],
             expires_at=doc["expires_at"], rules=len(doc.get("rules", [])),
             remaining_ttl_seconds=remaining,
             envelope_sha256=hashlib.sha256(raw).hexdigest())
        return True
    finally:
        if owned_session:
            session.close()


def _refresh_seconds(environ):
    raw = (environ.get(ENV_REFRESH_SECONDS, "") or "").strip()
    if not raw:
        return DEFAULT_REFRESH_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_REFRESH_SECONDS
    return max(MIN_REFRESH_SECONDS, min(MAX_REFRESH_SECONDS, value))


def _interruptible_sleep(seconds, *, sleeper, should_stop):
    """Sleep in 1s steps so a stop request is honored promptly."""
    remaining = float(seconds)
    while remaining > 0 and not should_stop():
        step = min(1.0, remaining)
        sleeper(step)
        remaining -= step


def _refresh_cycle(environ, *, media_s3, control_s3, session, sleeper,
                   should_stop):
    """One bounded cycle; returns True only on a proven refresh."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if should_stop():
            return False
        try:
            _refresh_once(environ, media_s3=media_s3, control_s3=control_s3,
                          session=session)
            return True
        except _Refused as exc:
            _log("attempt_failed", attempt=attempt, reason=str(exc))
        except Exception:
            _log("attempt_failed", attempt=attempt, reason="refresh unavailable")
        if attempt < MAX_ATTEMPTS and not should_stop():
            _interruptible_sleep(
                BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)],
                sleeper=sleeper, should_stop=should_stop)
    _log("cycle_failed", attempts=MAX_ATTEMPTS)
    return False


def run(environ=None, *, media_s3=None, control_s3=None, session=None,
        lock_handle=None, sleeper=time.sleep, clock=time.monotonic,
        should_stop=None, cycles=None):
    """Always-on worker loop: refresh immediately, then every interval.

    Never exits on a transient refresh failure; the loop continues and the
    publisher's stale-envelope hold is never extended. Returns the process
    exit code; never raises. ``cycles`` bounds the loop for tests; None runs
    until SIGTERM/SIGINT.
    """
    global _STOP
    _STOP = False
    if should_stop is None:
        should_stop = lambda: _STOP
    environ = os.environ if environ is None else environ
    if not _armed(environ):
        _log("disabled")
        return 0
    signal.signal(signal.SIGTERM, _stop_requested)
    signal.signal(signal.SIGINT, _stop_requested)

    interval = _refresh_seconds(environ)
    owned_lock = lock_handle is None
    lock_path = (environ.get(ENV_LOCK_PATH, "") or "").strip() or \
        "/tmp/exact_byte_attester.lock"
    try:
        if owned_lock:
            try:
                lock_handle = open(lock_path, "a+")
            except OSError:
                _log("refused", reason="lock file unavailable")
                return 2
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _log("skipped", reason="another refresh in flight")
            return 0
        try:
            _log("worker_started", refresh_seconds=interval)
            completed = 0
            while not should_stop():
                cycle_started = clock()
                _refresh_cycle(environ, media_s3=media_s3,
                               control_s3=control_s3, session=session,
                               sleeper=sleeper, should_stop=should_stop)
                completed += 1
                if cycles is not None and completed >= cycles:
                    break
                # Cadence is measured from the cycle start. Sleeping a full
                # interval after a slow but successful proof would create a
                # stale-envelope gap before the next refresh.
                delay = max(0.0, interval - (clock() - cycle_started))
                _interruptible_sleep(delay, sleeper=sleeper,
                                     should_stop=should_stop)
            _log("shutdown", cycles=completed)
            return 0
        finally:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        if owned_lock and lock_handle is not None:
            lock_handle.close()


def main():
    return run()


if __name__ == "__main__":
    sys.exit(main())
