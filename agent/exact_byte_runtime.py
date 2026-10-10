"""Server-owned runtime activation for the exact-byte immutable-media verifier.

Builds the ``_immutable_media_verifier_config`` consumed by
forward_media_send_context.immutable_verifier_for_store EXCLUSIVELY from
operator-pinned environment variables: read-only R2 credentials, pinned R2
account id, bucket, public base URL, attestation path, and the pinned
administrator Ed25519 public key. Tenant or calendar row values can never
influence any of these; this module accepts no row-controlled input at all.

Fail closed by construction: activate() never raises. Any missing or malformed
env yields a RuntimeActivation whose verifier_config is None, which leaves an
armed exact-byte lane HELD and the default-OFF behavior untouched.
"""
import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Optional

from . import r2_immutable_media as media
from . import exact_byte_proof_store as proof_store

ENV_ACCOUNT_ID = "AGENT_EXACT_BYTE_R2_ACCOUNT_ID"
ENV_BUCKET = "AGENT_EXACT_BYTE_R2_BUCKET"
ENV_PUBLIC_BASE_URL = "AGENT_EXACT_BYTE_R2_PUBLIC_BASE_URL"
ENV_RO_KEY_ID = "AGENT_EXACT_BYTE_R2_RO_ACCESS_KEY_ID"
ENV_RO_SECRET = "AGENT_EXACT_BYTE_R2_RO_SECRET_ACCESS_KEY"
ENV_ADMIN_PUBLIC_KEY = "AGENT_EXACT_BYTE_ADMIN_PUBLIC_KEY_B64"
ENV_ATTESTATION_PATH = "AGENT_EXACT_BYTE_ATTESTATION_PATH"
ENV_RETENTION_SECONDS = "AGENT_EXACT_BYTE_RETENTION_SECONDS"

_ACCOUNT_ID_RE = re.compile(r"[0-9a-f]{32}")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_MAX_RETENTION_SECONDS = 86400


@dataclass(frozen=True)
class RuntimeActivation:
    """The server-owned activation decision; never authorizes by omission."""
    verifier_config: Optional[dict]
    hold_reason: str

    @property
    def guard_holds(self):
        """True unless a complete server-owned verifier config was built."""
        return self.verifier_config is None


class _ConfigHold(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _required(environ, name, pattern=None):
    value = (environ.get(name, "") or "").strip()
    if not value or (pattern is not None and not pattern.fullmatch(value)):
        raise _ConfigHold(f"{name} missing or malformed")
    return value


def _public_base_url(environ):
    base = _required(environ, ENV_PUBLIC_BASE_URL)
    try:
        # Reuse the verifier's own strict canonical-URL identity rules.
        media._canonical_url(base, "probe")
    except Exception:
        raise _ConfigHold(f"{ENV_PUBLIC_BASE_URL} missing or malformed") from None
    return base


def _admin_public_key(environ):
    raw = _required(environ, ENV_ADMIN_PUBLIC_KEY)
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise _ConfigHold(f"{ENV_ADMIN_PUBLIC_KEY} missing or malformed") from None
    if len(key) != 32:
        raise _ConfigHold(f"{ENV_ADMIN_PUBLIC_KEY} missing or malformed")
    return key


def _retention_seconds(environ):
    raw = _required(environ, ENV_RETENTION_SECONDS)
    if not raw.isdigit():
        raise _ConfigHold(f"{ENV_RETENTION_SECONDS} missing or malformed")
    value = int(raw)
    if not 0 < value <= _MAX_RETENTION_SECONDS:
        raise _ConfigHold(f"{ENV_RETENTION_SECONDS} missing or malformed")
    return value


def _attestation_loader(path):
    if not os.path.isabs(path):
        raise _ConfigHold(f"{ENV_ATTESTATION_PATH} missing or malformed")

    def load():
        with open(path, "rb") as handle:
            return handle.read(media.MAX_ATTESTATION_BYTES + 1)

    return load


def _require_signed_serving_evidence(doc, *, account_id, bucket, base):
    """Validate the signed serving contract against the operator-pinned identity.

    Called on EVERY freshly verified document, including activation and each
    per-send reload. Freshness remains enforced by the media verifier.
    """
    if (doc.get("schema") != "echo-r2-lock-attestation-v1"
            or doc.get("account_id") != account_id or doc.get("bucket") != bucket
            or doc.get("public_base_url") != base
            or doc.get("public_serving") != "controlled-direct-byte-serving"):
        raise _ConfigHold(f"{ENV_ATTESTATION_PATH} identity invalid")
    evidence = doc.get("serving_evidence")
    review = evidence.get("operator_review") if isinstance(evidence, dict) else None
    if (not isinstance(review, dict)
            or evidence.get("account_id") != account_id
            or evidence.get("bucket") != bucket
            or evidence.get("public_base_url") != base
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"]
            or not isinstance(review.get("reviewed_at"), int)
            or isinstance(review.get("reviewed_at"), bool)
            or review["reviewed_at"] <= 0
            or review.get("configuration") != "controlled-direct-byte-serving"):
        raise _ConfigHold("operator-reviewed serving evidence missing or unbound")


class _ServingEvidenceLockSource(media.SignedLockRuleSource):
    """Pinned signature source enforcing serving evidence on every reload."""

    def __init__(self, loader, public_key_bytes, *, account_id, bucket, base):
        super().__init__(loader, public_key_bytes)
        self._account_id = account_id
        self._bucket = bucket
        self._base = base

    def verified_document(self):
        doc, digest = super().verified_document()
        try:
            _require_signed_serving_evidence(
                doc, account_id=self._account_id, bucket=self._bucket,
                base=self._base)
        except _ConfigHold as exc:
            raise media.ImmutableMediaError(exc.reason) from None
        return doc, digest


def _r2_read_only_client(account_id, key_id, secret):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4",
                      connect_timeout=10, read_timeout=30,
                      retries={"max_attempts": 2}),
    )


def _build(environ, *, store, s3_client, proof_sink):
    """Assemble the pinned config; raises _ConfigHold on any gap."""
    account_id = _required(environ, ENV_ACCOUNT_ID, _ACCOUNT_ID_RE)
    bucket = _required(environ, ENV_BUCKET, _BUCKET_RE)
    base = _public_base_url(environ)
    retention = _retention_seconds(environ)
    key = _admin_public_key(environ)
    loader = _attestation_loader(
        (environ.get(ENV_ATTESTATION_PATH, "") or "").strip())
    if s3_client is None:
        key_id = _required(environ, ENV_RO_KEY_ID)
        secret = _required(environ, ENV_RO_SECRET)
        s3_client = _r2_read_only_client(account_id, key_id, secret)
    if proof_sink is None:
        proof_sink = proof_store.proof_sink_from_env(environ, store=store)
        if proof_sink is None:
            raise _ConfigHold("proof sink missing or malformed")
    lock_source = _ServingEvidenceLockSource(
        loader, key, account_id=account_id, bucket=bucket, base=base)
    try:
        lock_source.verified_document()
    except media.ImmutableMediaError as exc:
        raise _ConfigHold(str(exc)) from None
    return {
        "bucket": bucket,
        "account_id": account_id,
        "public_base_url": base,
        "s3": s3_client,
        "lock_source": lock_source,
        "retention_seconds": retention,
        "proof_sink": proof_sink,
    }


def activate(environ=None, *, store=None, s3_client=None, proof_sink=None):
    """Return the server-owned RuntimeActivation; NEVER raises.

    Every trusted value comes from ``environ`` (os.environ in production) or
    from explicitly injected test doubles. No row, tenant, or database value
    is read here. Missing/malformed env, an unbuildable client, or an
    unavailable proof sink all yield guard_holds=True.
    """
    environ = os.environ if environ is None else environ
    if not callable(getattr(environ, "get", None)):
        return RuntimeActivation(None, "environment mapping unavailable")
    try:
        config = _build(environ, store=store, s3_client=s3_client,
                        proof_sink=proof_sink)
        return RuntimeActivation(config, "")
    except _ConfigHold as exc:
        return RuntimeActivation(None, exc.reason)
    except Exception:
        return RuntimeActivation(None, "runtime activation unavailable")


def verifier_config_or_none(environ=None, *, store=None):
    """Store hook entry point: the pinned config dict, or None (held)."""
    return activate(environ, store=store).verifier_config
