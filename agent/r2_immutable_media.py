"""Fail-closed immutable R2 evidence; no administrator credential is used here.

An external administrator must inspect R2's enabled bucket lock rules and sign
an attestation with Ed25519. Publisher configuration pins that administrator's
public key and account/bucket/public-base identity. A conditional PUT, object
hash, or an unsigned JSON document alone is never retention evidence.

Only absolute-date locks are accepted. Age-based rules need object
creation-time verification and are deliberately unsupported. The signed
``public_serving`` assertion must describe controlled direct byte serving,
without transforms, redirects, or a rewritable origin mapping through retention.
The signer/attestation producer and live lock provisioning are external work.
"""
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

MAX_ATTESTATION_BYTES = 65536
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
MAX_FRESHNESS_SECONDS = 300


class ImmutableMediaError(ValueError):
    """Safe fixed reason; provider exceptions and secrets are never included."""


@dataclass(frozen=True)
class ImmutableMediaProof:
    public_url: str
    account_id: str
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    observed_at: int
    retention_until: int
    lock_rule_id: str
    attestation_sha256: str


@dataclass(frozen=True)
class ImmutableMediaEvidence:
    """Trusted verifier result preserving the verified lock and fetch horizon."""
    proof: ImmutableMediaProof
    provider_fetch_horizon_seconds: int


class SignedLockRuleSource:
    """Trust boundary: a controlled loader + pinned administrator public key.

    loader returns bounded UTF-8 envelope bytes: {payload: BASE64, signature:
    BASE64}. Signature covers exact payload bytes. Never accept a public key
    embedded in the envelope. The loader may read a controlled local file; no
    live admin token or environment lookup is needed by this module.
    """
    def __init__(self, loader, public_key_bytes):
        if not isinstance(public_key_bytes, bytes) or len(public_key_bytes) != 32:
            raise ImmutableMediaError("invalid administrator public key")
        self._loader = loader
        self._public_key = public_key_bytes

    def verified_document(self):
        try:
            raw = self._loader()
            if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ATTESTATION_BYTES:
                raise ValueError()
            envelope = json.loads(raw)
            payload = base64.b64decode(envelope['payload'], validate=True)
            signature = base64.b64decode(envelope['signature'], validate=True)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(self._public_key).verify(signature, payload)
            doc = json.loads(payload)
            if not isinstance(doc, dict):
                raise ValueError()
            return doc, hashlib.sha256(payload).hexdigest()
        except Exception:
            raise ImmutableMediaError("trusted lock attestation unavailable") from None


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical_url(base, key):
    if not isinstance(base, str) or not isinstance(key, str):
        raise ImmutableMediaError("invalid public object identity")
    parsed = urlsplit(base)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or base != base.rstrip('/') or '%' in base or '\\' in base
            or any(ord(c) <= 32 or ord(c) >= 127 for c in base)):
        raise ImmutableMediaError("invalid public object identity")
    if (not key or key.startswith('/') or '\\' in key
            or any(p in ('', '.', '..') for p in key.split('/'))
            or any(ord(c) < 32 or ord(c) == 127 for c in key)):
        raise ImmutableMediaError("invalid public object identity")
    return base + '/' + quote(key, safe='/')


def _read_bounded(body, cap):
    result = bytearray()
    try:
        while True:
            chunk = body.read(min(65536, cap + 1 - len(result)))
            if not isinstance(chunk, bytes):
                raise ImmutableMediaError("object read unavailable")
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > cap:
                raise ImmutableMediaError("object exceeds byte limit")
        if not result:
            raise ImmutableMediaError("empty object")
        return bytes(result)
    finally:
        body.close()


def _origin_bytes(s3, bucket, key, cap):
    body = None
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj['Body']
        length = obj.get('ContentLength')
        if not _integer(length) or not 0 < length <= cap:
            raise ImmutableMediaError("invalid origin content length")
        data = _read_bounded(body, cap)
        body = None
        if len(data) != length:
            raise ImmutableMediaError("origin content length mismatch")
        return data
    finally:
        if body is not None:
            body.close()


def _public_bytes(url, cap, session=None):
    import requests
    if session is None:
        session = requests.Session()
        session.trust_env = False  # no ambient proxy/netrc credentials on public GET
        owned = True
    else:
        owned = False
    response = None
    try:
        response = session.get(url, stream=True, allow_redirects=False,
                               timeout=(10, 30), headers={'Accept-Encoding': 'identity'})
        if response.status_code != 200 or response.url != url or response.history:
            raise ImmutableMediaError("public object redirects or unavailable")
        headers = response.headers
        if headers.get('Content-Encoding', '').lower() not in ('', 'identity'):
            raise ImmutableMediaError("public object content encoding unsupported")
        declared = headers.get('Content-Length')
        if declared is not None and (not declared.isdigit() or not 0 < int(declared) <= cap):
            raise ImmutableMediaError("invalid public content length")
        response.raw.decode_content = False
        data = _read_bounded(response.raw, cap)
        if declared is not None and len(data) != int(declared):
            raise ImmutableMediaError("public content length mismatch")
        return data
    finally:
        if response is not None:
            response.close()
        if owned:
            session.close()


def verify_immutable_media(public_url, *, key, bucket, account_id, public_base_url,
                           s3, lock_source, required_retention_until,
                           max_bytes=DEFAULT_MAX_BYTES, now=None, session=None):
    """Return exact URL/origin/lock proof or raise a safe ImmutableMediaError.

    s3 must be the authenticated R2 client for the pinned account's endpoint,
    using read-only object credentials. lock_source must be SignedLockRuleSource.
    required_retention_until covers the entire provider fetch/retry horizon;
    callers must reverify near each send and persist this proof with that URL.
    """
    try:
        observed = int(time.time()) if now is None else now
        if (not _integer(observed) or not _integer(required_retention_until)
                or required_retention_until <= observed or not _integer(max_bytes)
                or not 0 < max_bytes <= DEFAULT_MAX_BYTES
                or not isinstance(bucket, str) or not bucket
                or not isinstance(account_id, str) or not account_id):
            raise ImmutableMediaError("invalid verification bounds")
        if public_url != _canonical_url(public_base_url, key):
            raise ImmutableMediaError("public URL does not identify exact object")
        if not isinstance(lock_source, SignedLockRuleSource):
            raise ImmutableMediaError("trusted lock source required")
        if getattr(getattr(s3, 'meta', None), 'endpoint_url', None) != (
                'https://' + account_id + '.r2.cloudflarestorage.com'):
            raise ImmutableMediaError('authenticated origin endpoint mismatch')
        doc, attestation_hash = lock_source.verified_document()
        checked = doc.get('observed_at')
        expires = doc.get('expires_at')
        if (doc.get('schema') != 'echo-r2-lock-attestation-v1'
                or doc.get('account_id') != account_id or doc.get('bucket') != bucket
                or doc.get('public_base_url') != public_base_url
                or doc.get('public_serving') != 'controlled-direct-byte-serving'
                or not _integer(checked) or not 0 <= observed - checked <= MAX_FRESHNESS_SECONDS
                or not _integer(expires) or not observed < expires <= checked + MAX_FRESHNESS_SECONDS):
            raise ImmutableMediaError("lock attestation identity or freshness invalid")
        rules = doc.get('rules')
        if not isinstance(rules, list):
            raise ImmutableMediaError("enabled retention lock missing")
        rule = next((r for r in rules if isinstance(r, dict)
                     and r.get('enabled') is True
                     and isinstance(r.get('id'), str) and r['id']
                     and isinstance(r.get('prefix'), str) and r['prefix']
                     and r['prefix'].endswith('/') and key.startswith(r['prefix'])
                     and r.get('protection') == 'overwrite-and-delete'
                     and _integer(r.get('retention_until'))
                     and r['retention_until'] >= required_retention_until), None)
        if rule is None:
            raise ImmutableMediaError("enabled retention lock missing")
        origin = _origin_bytes(s3, bucket, key, max_bytes)
        public = _public_bytes(public_url, max_bytes, session)
        if public != origin:
            raise ImmutableMediaError("public and origin bytes differ")
        # Reject a proof that became stale during slow reads.
        if now is None and int(time.time()) >= min(expires, required_retention_until):
            raise ImmutableMediaError("verification expired during read")
        return ImmutableMediaProof(public_url, account_id, bucket, key,
                                   hashlib.sha256(public).hexdigest(), len(public), observed,
                                   rule['retention_until'], rule['id'], attestation_hash)
    except ImmutableMediaError:
        raise
    except Exception:
        raise ImmutableMediaError("immutable media verification unavailable") from None


def trusted_bool_adapter(*, bucket, account_id, public_base_url, s3, lock_source,
                         retention_seconds, proof_sink, max_bytes=DEFAULT_MAX_BYTES):
    """Build guard's typed ``immutable_verifier(url, data)`` callback.

    proof_sink must durably record each ImmutableMediaProof; sink failure holds
    the send. The callback also binds the independently fetched outgoing bytes
    to the exact data already inspected by the guard. No self-asserted result or
    hash-only origin check can authorize a send. The legacy adapter name stays
    for configuration compatibility; successful results now retain the actual
    lock deadline and configured provider fetch/retry horizon, never a boolean.
    """
    if (not _integer(retention_seconds) or retention_seconds <= 0
            or not callable(proof_sink)):
        raise ImmutableMediaError('invalid verifier adapter bounds')

    def verify(url, data):
        try:
            if not isinstance(data, bytes) or not 0 < len(data) <= max_bytes:
                return False
            from urllib.parse import unquote
            prefix = public_base_url + '/'
            if not isinstance(url, str) or not url.startswith(prefix):
                return False
            key = unquote(url[len(prefix):], errors='strict')
            proof = verify_immutable_media(
                url, key=key, bucket=bucket, account_id=account_id,
                public_base_url=public_base_url, s3=s3, lock_source=lock_source,
                required_retention_until=int(time.time()) + retention_seconds,
                max_bytes=max_bytes)
            if proof.size_bytes != len(data) or proof.sha256 != hashlib.sha256(data).hexdigest():
                return False
            proof_sink(proof)
            if proof.retention_until < time.time() + retention_seconds:
                return False
            return ImmutableMediaEvidence(proof, retention_seconds)
        except Exception:
            return False

    return verify
