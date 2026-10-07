"""DRAFT generated-original provenance preparation. No DB writes or authority.

This is intentionally unwired/default OFF. A signed producer claim alone is
insufficient. The evidence backend must independently authenticate the actual
provider/runtime execution, pixel review, brand source, current complete photo
inventory/history, and immutable storage version. All reads happen before locks.
A final owner transaction must recheck inventory/history/revisions and acquire
its own reservation; PreparedGeneratedOriginal is never a clearance or grant.

No production backend currently meets this contract. Existing creative_studio
.review.json is producer-writable, ArtifactStore upserts and reuses cached URLs,
and media_host create-once/readback alone is not a retention/version attestation.
Do not adapt those records into positive evidence by trusting response ID strings.

Next bounded integration (not implemented here):
* creative_studio's final approved output/review sidecar
  must hand exact bytes, job/request identity and verified brand revisions to an
  isolated issuer. Capture authenticated provider retrieval OR trusted runtime
  execution evidence that binds the original output hash, uncached generation,
  provider output identity and time. Independently authenticate pixel review.
* media_host._S3Client.put_if_absent/get_bytes must expose a pinned storage
  identity plus independently verified create-only/retention authorization for
  an original namespace. The existing .put path and credentials cannot mutate
  that namespace. A unique URL plus successful GET is not immutability proof.
* Provision a separately approved independent verifier signing key/registry and
  authenticated append-only execution receipt store. No key is provisioned here.
* Implement GenerationEvidenceBackend using those receipts, complete current
  inventory/history and trusted palette/copy sources. Recheck those revisions
  under owner transaction locks before any database grant. Wire only after that
  contract exists, preserving a real-photo-first decision at the final boundary.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Protocol
from types import MappingProxyType
from urllib.parse import urlsplit
import uuid

MAX_BYTES = 134217728
MAX_RECEIPT_BYTES = 32768
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_MD5 = re.compile(r'md5:[0-9a-f]{32}\Z')
_FIELDS = frozenset({
    'schema_version', 'receipt_id', 'key_id', 'tenant_id', 'job_id', 'request_id',
    'post_date', 'requested_at', 'created_at', 'provider', 'model', 'runtime_id',
    'provider_output_id', 'image_url', 'object_version', 'image_sha256',
    'image_fingerprint', 'image_length', 'palette_revision', 'palette_digest',
    'source_copy_revision', 'source_copy_digest', 'pixel_review_id', 'pixel_policy_id',
})


class GeneratedReceiptHold(RuntimeError):
    """Static reasons only; no producer data, URLs or backend errors in messages."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise GeneratedReceiptHold('generated_receipt_shape_invalid') from None


def sha256(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class GenerationRequest:
    """Owner's trusted current request snapshot, never taken from producer packet."""
    tenant_id: str
    job_id: str
    request_id: str
    post_date: str
    requested_at: str
    palette_revision: str
    palette_digest: str
    source_copy_revision: str
    source_copy_digest: str
    pixel_policy_id: str


@dataclass(frozen=True)
class ApprovedGenerationKey:
    """Provisioned verifier key, obtained from an independent approved registry."""
    key_id: str
    public_key_hex: str
    approved: bool
    role: str
    provider: str
    model: str
    runtime_id: str


@dataclass(frozen=True)
class VerifiedGenerationEvidence:
    """Backend-authenticated observations; a producer dict is not this evidence.

    receipt_payload_json must equal the signed payload, binding every field.
    Provider retrieval must show a fresh uncached output for this execution;
    runtime attestation binds these exact original output bytes and their job.
    Pixel review must inspect the pinned original bytes, verify approved copy and
    palette, and meet the current approved policy. History is a complete reviewed
    corpus, including exact/perceptual reuse and undated/unresolved prior assets.
    Photo-first inventory must be complete and show zero eligible gym photos.
    Backend must bound every observation to this receipt/job and current snapshot.
    """
    receipt_payload_json: str
    observed_at: str
    provider_authenticated: bool
    runtime_authenticated: bool
    cache_hit: bool
    immutable_storage_verified: bool
    immutable_image_bytes: bytes
    brand_source_verified: bool
    pixel_review_passed: bool
    history_complete: bool
    history_reviewed_no_match: bool
    history_revision: str
    photo_inventory_complete: bool
    eligible_photo_count: int
    photo_inventory_revision: str


class GenerationEvidenceBackend(Protocol):
    """Read-only integration contract, no implementation or credentials here.

    Implementations use independently authenticated provider/runtime/verifier
    services and immutable version reads. They may not consume ArtifactStore or
    producer sidecars as authenticated proof. Registry and evidence exceptions
    fail closed. No function receives a producer supplied lookup URL: storage
    resolution and authorization happen inside the backend by trusted job ID.
    """
    def approved_key(self, key_id: str) -> ApprovedGenerationKey: ...
    def verified_evidence(self, request: GenerationRequest,
                          receipt_payload_json: str) -> VerifiedGenerationEvidence: ...


@dataclass(frozen=True)
class PreparedGeneratedOriginal:
    """Frozen evidence for later owner review; has NO positive DB authority."""
    receipt_payload_json: str
    signature_hex: str
    receipt_ref: str
    image_bytes: bytes
    history_revision: str
    photo_inventory_revision: str
    evidence_observed_at: str

    @property
    def payload(self):
        return json.loads(self.receipt_payload_json)


def _hold(reason):
    raise GeneratedReceiptHold(reason)


def _text(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 2048 and not any(ord(c) < 32 for c in value))


def _time(value):
    if not _text(value) or not value.endswith('Z'):
        _hold('generated_receipt_shape_invalid')
    parsed = datetime.fromisoformat(value[:-1] + '+00:00')
    if parsed.tzinfo != timezone.utc:
        _hold('generated_receipt_shape_invalid')
    return parsed


def prepare_generated_original(packet, request, backend=None, *, enabled=False,
                               now=None, max_age_seconds=900):
    """Verify signed v1 receipt and independent evidence, then freeze exact bytes.

    ``request`` and ``backend`` must come from the dedicated trusted owner lane.
    ``enabled`` defaults OFF; enabling this pure preparation function grants no
    publishing/DB capability. ``now`` and age are verifier policy, never producer
    input. Future/unavailable/stale evidence and partial inventories always hold.
    Offline injected backends/keys are test seams, not production proof.
    """
    if enabled is not True:
        _hold('generated_preparation_disabled')
    if backend is None:
        _hold('generated_evidence_backend_unavailable')
    try:
        if type(request) is not GenerationRequest:
            _hold('generated_request_invalid')
        if (not isinstance(packet, dict) or set(packet) != {'payload', 'signature_hex'}
                or not isinstance(packet['payload'], dict) or set(packet['payload']) != _FIELDS):
            _hold('generated_receipt_shape_invalid')
        # Capture payload and signature before invoking any external callback.
        # Canonical JSON detaches nested producer containers; every accepted
        # field is scalar, so the read-only mapping is the immutable snapshot
        # used for signature, binding and byte verification throughout.
        text = canonical(packet['payload'])
        if len(text.encode()) > MAX_RECEIPT_BYTES:
            _hold('generated_receipt_shape_invalid')
        p = MappingProxyType(json.loads(text))
        signature_hex = packet['signature_hex']
        if type(p['schema_version']) is not int or p['schema_version'] != 1:
            _hold('generated_receipt_version_unsupported')
        for name in _FIELDS - {'schema_version', 'image_length'}:
            if not _text(p[name]):
                _hold('generated_receipt_shape_invalid')
        for name in ('receipt_id', 'job_id', 'request_id'):
            if str(uuid.UUID(p[name])) != p[name]:
                _hold('generated_receipt_shape_invalid')
        for name in ('image_sha256', 'palette_digest', 'source_copy_digest'):
            if not _SHA.fullmatch(p[name]):
                _hold('generated_receipt_shape_invalid')
        url = urlsplit(p['image_url'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.fragment or any(c.isspace() for c in p['image_url'])):
            _hold('generated_receipt_shape_invalid')
        if (not _MD5.fullmatch(p['image_fingerprint']) or type(p['image_length']) is not int
                or not 0 < p['image_length'] <= MAX_BYTES
                or date.fromisoformat(p['post_date']).isoformat() != p['post_date']):
            _hold('generated_receipt_shape_invalid')
        if any(p[name] != getattr(request, name) for name in request.__dataclass_fields__):
            _hold('generated_request_binding_changed')
        created, requested = _time(p['created_at']), _time(p['requested_at'])
        now = now or datetime.now(timezone.utc)
        if (not isinstance(now, datetime) or now.tzinfo is None
                or type(max_age_seconds) is not int or not 0 < max_age_seconds <= 900
                or not requested <= created <= now
                or (now - requested).total_seconds() > max_age_seconds):
            _hold('generated_receipt_stale_or_future')
        key = backend.approved_key(p['key_id'])
        if (type(key) is not ApprovedGenerationKey or key.approved is not True
                or key.role != 'independent_generation_verifier' or key.key_id != p['key_id']
                or any(getattr(key, n) != p[n] for n in ('provider', 'model', 'runtime_id'))):
            _hold('generated_signer_unapproved')
        sig = bytes.fromhex(signature_hex)
        pub = bytes.fromhex(key.public_key_hex)
        if len(sig) != 64 or len(pub) != 32 or sig.hex() != signature_hex:
            _hold('generated_signature_invalid')
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, text.encode())
        except InvalidSignature:
            _hold('generated_signature_invalid')
        evidence = backend.verified_evidence(request, text)
        if (type(evidence) is not VerifiedGenerationEvidence
                or evidence.receipt_payload_json != text):
            _hold('generated_evidence_binding_changed')
        observed = _time(evidence.observed_at)
        if not created <= observed <= now or (now-observed).total_seconds() > max_age_seconds:
            _hold('generated_evidence_stale_or_future')
        if evidence.provider_authenticated is not True or evidence.runtime_authenticated is not True:
            _hold('generated_provider_or_runtime_unverified')
        if evidence.cache_hit is not False:
            _hold('generated_cached_output_ineligible')
        if evidence.immutable_storage_verified is not True:
            _hold('generated_immutable_storage_unverified')
        data = evidence.immutable_image_bytes
        if (type(data) is not bytes or len(data) != p['image_length'] or sha256(data) != p['image_sha256']
                or 'md5:' + hashlib.md5(data).hexdigest() != p['image_fingerprint']):
            _hold('generated_original_bytes_changed')
        if evidence.brand_source_verified is not True:
            _hold('generated_brand_source_unverified')
        if evidence.pixel_review_passed is not True:
            _hold('generated_pixel_review_failed')
        if (evidence.history_complete is not True or evidence.history_reviewed_no_match is not True
                or not _text(evidence.history_revision)):
            _hold('generated_history_used_or_uncertain')
        if (evidence.photo_inventory_complete is not True
                or type(evidence.eligible_photo_count) is not int or evidence.eligible_photo_count != 0
                or not _text(evidence.photo_inventory_revision)):
            _hold('generated_photo_first_hold')
        ref = 'generated-receipt:' + sha256((text+'\n'+sig.hex()).encode())
        return PreparedGeneratedOriginal(text, sig.hex(), ref, data,
            evidence.history_revision, evidence.photo_inventory_revision, evidence.observed_at)
    except GeneratedReceiptHold:
        raise
    except Exception:
        _hold('generated_verification_unavailable')
