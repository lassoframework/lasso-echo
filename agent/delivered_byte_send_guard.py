"""Default-OFF exact delivered-byte authority at a provider mutation boundary.

RPC calls must return only after their database transaction commits (the portal
store's PostgREST wrapper has that contract). Network object reads occur between
RPC calls. This module never sends to a provider and never grants approval.
URL immutability must be established by a trusted integration, not inferred from
a CDN URL or from two matching reads. Without that evidence the armed lane holds.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import os
import re
import time
from threading import Lock, current_thread
import asyncio
import uuid

from .forward_media_lane import approved_url, read_public_object
from .r2_immutable_media import ImmutableMediaEvidence, ImmutableMediaProof

FLAG = 'AGENT_EXACT_BYTE_SEND_GUARD'
MAX_BYTES = 128 * 1024 * 1024
OUTCOMES = frozenset({'uncertain', 'definite_no_send', 'provider_accepted', 'platform_verified'})
_active = ContextVar('exact_byte_send_permit', default=None)
_UNSET = object()


class ExactByteSendHold(RuntimeError):
    """No provider mutation is authorized by this fence."""

    def __init__(self, message, *, definitive_no_post=True):
        super().__init__(message)
        self.definitive_no_post = definitive_no_post


def enabled():
    return os.getenv(FLAG, '').lower() in ('1', 'true', 'yes', 'on')


def active_permit():
    """Observe the current scope without consuming its committed authority."""
    return _active.get()


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ExactByteSendHold('exact byte identity unavailable') from None


def _rpc(store, name, args, *, definitive_no_post=True):
    try:
        return store._reservation_rpc(name, args, timeout=30)
    except Exception:
        # Includes unknown commit outcomes: never retry a possible authorization.
        raise ExactByteSendHold('exact byte authority unavailable',
                                definitive_no_post=definitive_no_post) from None


def _context(value, row_id, claim_token, row):
    if not isinstance(value, dict) or value.get('enabled') is not True:
        raise ExactByteSendHold('exact byte cutover unavailable')
    if (value.get('calendar_row_id') != row_id
            or value.get('publish_claim_token') != claim_token):
        raise ExactByteSendHold('exact byte claim context changed')
    for name in ('row_revision', 'canonical_tenant', 'post_date', 'reservation_day',
                 'posting_timezone', 'provider_target', 'cutover_id'):
        if not value.get(name):
            raise ExactByteSendHold('exact byte context incomplete')
    if 'shared_posting_identity' not in value:
        raise ExactByteSendHold('exact byte context incomplete')
    snapshot = value.get('row_snapshot')
    if (not isinstance(row, dict) or not isinstance(snapshot, dict) or not snapshot
            or snapshot.get('id') != row_id
            or any(key not in row or row[key] != expected for key, expected in snapshot.items())):
        raise ExactByteSendHold('outgoing row differs from exact byte snapshot')
    target = value['provider_target']
    if (not isinstance(target, dict)
            or any(not isinstance(target.get(key), str) or not target[key].strip()
                   for key in ('provider', 'platform', 'account_id'))):
        raise ExactByteSendHold('exact provider target unavailable')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(value.get('corpus_sha256', ''))):
        raise ExactByteSendHold('exact byte corpus evidence unavailable')
    images = value.get('images')
    if not isinstance(images, list) or not 1 <= len(images) <= 2:
        raise ExactByteSendHold('single image context required')
    roles = [item.get('role') if isinstance(item, dict) else None for item in images]
    if roles.count('image') != 1 or any(role not in ('image', 'thumbnail') for role in roles):
        raise ExactByteSendHold('single image context required')
    ordinals = []
    for item in images:
        ordinal = item.get('ordinal')
        expected_ordinal = 0 if item['role'] == 'image' else 1
        if type(ordinal) is not int or ordinal != expected_ordinal or ordinal in ordinals:
            raise ExactByteSendHold('exact byte ordinal unavailable')
        ordinals.append(ordinal)
        if not approved_url(item.get('url')):
            raise ExactByteSendHold('approved exact media URL required')
    return deepcopy(value)


def _actor():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return current_thread(), task


def _positive_integer(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class ProviderFetchRetention:
    """Finite verified protection, with a clock rollback resistant expiry."""
    retention_until: int
    provider_fetch_horizon_seconds: int
    _monotonic_until: float = field(init=False, repr=False)

    def __post_init__(self):
        if (not _positive_integer(self.retention_until)
                or not _positive_integer(self.provider_fetch_horizon_seconds)):
            raise ExactByteSendHold('verified provider retention bounds required')
        object.__setattr__(self, '_monotonic_until',
                           time.monotonic() + self.retention_until - time.time())


def _require_retention(retention, *, definitive_no_post=False):
    if (not isinstance(retention, ProviderFetchRetention)
            or retention.retention_until < time.time() + retention.provider_fetch_horizon_seconds
            or retention._monotonic_until < time.monotonic() + retention.provider_fetch_horizon_seconds):
        raise ExactByteSendHold('verified provider fetch retention expired or insufficient',
                                definitive_no_post=definitive_no_post)


def _verified_retention(evidence, url, data):
    """Check the trusted result is bound to the exact inspected public object."""
    if not isinstance(evidence, ImmutableMediaEvidence):
        raise ExactByteSendHold('immutable provider bytes evidence unavailable')
    proof = evidence.proof
    if (not isinstance(proof, ImmutableMediaProof) or proof.public_url != url
            or proof.sha256 != hashlib.sha256(data).hexdigest()
            or type(proof.size_bytes) is not int or proof.size_bytes != len(data)
            or type(proof.observed_at) is not int or not 0 <= proof.observed_at <= time.time()
            or any(not isinstance(value, str) or not value
                   for value in (proof.account_id, proof.bucket, proof.key, proof.lock_rule_id))
            or not re.fullmatch(r'[0-9a-f]{64}', str(proof.attestation_sha256))):
        raise ExactByteSendHold('immutable provider bytes evidence differs')
    retention = ProviderFetchRetention(proof.retention_until, evidence.provider_fetch_horizon_seconds)
    _require_retention(retention, definitive_no_post=True)
    return retention


@dataclass
class SendPermit:
    context: dict
    images: list
    attempt_id: str
    claim_token: str
    consumed: bool = False
    closed: bool = False
    outcome_recorded: bool = False
    retention: ProviderFetchRetention | None = None
    _continuation: object = field(default=None, repr=False)
    _owner: object = field(default_factory=_actor, repr=False)
    _lock: object = field(default_factory=Lock, repr=False)


@dataclass(frozen=True)
class SendContinuation:
    """One follow-up mutation of the object returned by the first mutation."""
    permit: SendPermit
    provider_target: dict
    object_id: str
    operation: str
    content: object
    used: bool = False


def active_continuation():
    permit = _active.get()
    return permit._continuation if permit is not None else None


def _approved_content(permit):
    snapshot = permit.context.get('row_snapshot')
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('caption'), str):
        raise ExactByteSendHold('approved exact content unavailable', definitive_no_post=False)
    fmt = permit.context.get('format', snapshot.get('format'))
    if fmt not in ('feed', 'story'):
        raise ExactByteSendHold('approved exact format unavailable', definitive_no_post=False)
    return snapshot['caption'], fmt


def check_content(caption, post_format):
    """Pin actual outgoing text and format to persisted approval content."""
    permit = _active.get()
    if permit is None:
        if enabled():
            raise ExactByteSendHold('committed exact byte authorization required')
        return None
    with permit._lock:
        if permit.closed or permit.consumed or permit._owner != _actor():
            raise ExactByteSendHold('same unconsumed exact byte actor required', definitive_no_post=False)
        _require_retention(permit.retention)
        expected_caption, expected_format = _approved_content(permit)
        if (post_format != expected_format
                or (caption != expected_caption and not (caption is None and post_format == 'story'))):
            raise ExactByteSendHold('provider content differs from approved exact content',
                                    definitive_no_post=False)
    return permit


def bind_continuation(permit, provider_target, object_id, operation):
    """Bind a successful first response to one named follow-up operation.

    Call only after validating the provider's response to the first mutation.
    A missing/ambiguous returned ID holds; authority never becomes reusable.
    """
    if permit is None and _active.get() is None and not enabled():
        return None
    if not isinstance(permit, SendPermit) or _active.get() is not permit:
        raise ExactByteSendHold('same exact byte attempt required', definitive_no_post=False)
    with permit._lock:
        if (permit.closed or permit.outcome_recorded or not permit.consumed
                or permit._owner != _actor() or permit._continuation is not None
                or provider_target != permit.context['provider_target']
                or not isinstance(object_id, str) or not object_id.strip()
                or not isinstance(operation, str) or not operation.strip()):
            raise ExactByteSendHold('exact byte continuation unavailable', definitive_no_post=False)
        _require_retention(permit.retention)
        continuation = SendContinuation(permit, deepcopy(provider_target), object_id,
                                        operation, _approved_content(permit))
        permit._continuation = continuation
        return continuation


def require_continuation(continuation, provider_target, object_id, operation, *,
                         caption=_UNSET, post_format=_UNSET):
    """Consume only the same active attempt, returned object and operation.

    Neither a flag flip nor a copied token can grant a second mutation. Unknown
    follow-up outcomes are permanent holds; this API does not authorize retries.
    """
    permit = _active.get()
    if permit is None and continuation is None and not enabled():
        return None
    if (not isinstance(continuation, SendContinuation) or permit is None
            or continuation.permit is not permit):
        raise ExactByteSendHold('bound exact byte continuation required', definitive_no_post=False)
    with permit._lock:
        if (permit.closed or permit.outcome_recorded or not permit.consumed
                or permit._owner != _actor() or permit._continuation is not continuation
                or continuation.used
                or provider_target != permit.context['provider_target']
                or provider_target != continuation.provider_target
                or object_id != continuation.object_id or operation != continuation.operation
                or continuation.content != _approved_content(permit)
                or (caption is not _UNSET and caption != continuation.content[0])
                or (post_format is not _UNSET and post_format != continuation.content[1])):
            raise ExactByteSendHold('exact byte continuation differs or already consumed',
                                    definitive_no_post=False)
        _require_retention(permit.retention)
        object.__setattr__(continuation, 'used', True)
    return permit


@contextmanager
def authorized_send(store, row, claim_token, *, immutable_verifier=None, read_bytes=None):
    """Authorize one persisted image and optional persisted thumbnail.

Call ``require`` immediately before the provider mutation inside this scope.
The caller must retain every existing publish, approval and broad forward gate.
``immutable_verifier(url, bytes)`` returns trusted ``ImmutableMediaEvidence``
proving the URL will serve these bytes for the configured provider fetch horizon.
Bare booleans never grant evidence. No verifier means an armed hold.
``read_bytes`` exists for isolated tests; production uses the approved reader.
"""
    if not enabled():
        # Do not mask an outer live permit with a nested OFF scope.
        yield None
        return
    if _active.get() is not None:
        raise ExactByteSendHold('nested exact byte send refused', definitive_no_post=False)
    row_id = _uuid(row.get('id') if isinstance(row, dict) else row)
    token = _uuid(claim_token)
    context = _context(_rpc(store, 'exact_byte_send_context_20261010', {
        'p_calendar_row_id': row_id, 'p_claim_token': token,
    }), row_id, token, row)
    # The context response has ended its transaction before this network work.
    if not callable(immutable_verifier):
        raise ExactByteSendHold('immutable provider bytes evidence unavailable')
    images = []
    retentions = []
    for item in context['images']:
        try:
            data = (read_bytes or read_public_object)(item['url'])
            if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BYTES:
                raise ExactByteSendHold('bounded exact byte evidence unavailable')
            evidence = immutable_verifier(item['url'], data)
            retentions.append(_verified_retention(evidence, item['url'], data))
        except ExactByteSendHold:
            raise
        except Exception:
            raise ExactByteSendHold('exact media read unavailable') from None
        images.append({**item, 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(),
                       'byte_length': len(data)})
    # Preserve the shortest verified object deadline and the entire configured
    # fetch horizon. A slow thumbnail read must not refresh the first image.
    retention = ProviderFetchRetention(
        min(value.retention_until for value in retentions),
        max(value.provider_fetch_horizon_seconds for value in retentions))
    object.__setattr__(retention, '_monotonic_until',
                       min(value._monotonic_until for value in retentions))
    _require_retention(retention, definitive_no_post=True)
    result = _rpc(store, 'exact_byte_authorize_send_20261010', {
        'p_calendar_row_id': row_id, 'p_claim_token': token,
        'p_expected_context': context, 'p_images': images,
    }, definitive_no_post=False)
    if not isinstance(result, dict) or result.get('authorized') is not True:
        raise ExactByteSendHold('exact byte send denied', definitive_no_post=(
            isinstance(result, dict) and result.get('authorized') is False))
    try:
        attempt_id = _uuid(result.get('attempt_id'))
    except ExactByteSendHold:
        raise ExactByteSendHold('committed exact byte attempt unavailable',
                                definitive_no_post=False) from None
    permit = SendPermit(context, images, attempt_id, token, retention=retention)
    # Database authority may commit after a long network wait. Keep that
    # committed attempt occupied, but hold before entering provider scope.
    _require_retention(permit.retention)
    active_token = _active.set(permit)
    try:
        yield permit
    except ExactByteSendHold as exc:
        # Authority is already committed even if a later caller validation
        # failed before network work. Preserve permanent uncertain occupancy.
        exc.definitive_no_post = False
        raise
    finally:
        with permit._lock:
            permit.closed = True
        _active.reset(active_token)


def _payload_permit(provider_target, image_urls, thumbnail_url=None, *, consume=False,
                    image_bytes=None):
    """Validate the authorization, optionally consuming the first mutation.

Provider target must exactly match the persisted target, and URLs must match
the complete persisted image payload, including the optional thumbnail.
Even if the flag changes after authority was acquired, its scope stays fenced.
"""
    permit = _active.get()
    if permit is None:
        if enabled():
            raise ExactByteSendHold('committed exact byte authorization required')
        return None
    with permit._lock:
        if permit.closed or permit.consumed or permit._owner != _actor():
            raise ExactByteSendHold('exact byte authorization already consumed or actor differs', definitive_no_post=False)
        _require_retention(permit.retention)
        expected = [item['url'] for item in permit.images if item['role'] == 'image']
        thumbnail = next((item['url'] for item in permit.images
                          if item['role'] == 'thumbnail'), None)
        if (provider_target != permit.context['provider_target']
                or not isinstance(image_urls, (list, tuple))
                or list(image_urls) != expected or thumbnail_url != thumbnail):
            raise ExactByteSendHold('provider payload differs from authorized bytes', definitive_no_post=False)
        if image_bytes is not None:
            image = next(item for item in permit.images if item['role'] == 'image')
            if (not isinstance(image_bytes, bytes)
                    or image.get('sha256') != 'sha256:' + hashlib.sha256(image_bytes).hexdigest()
                    or image.get('byte_length') != len(image_bytes)):
                raise ExactByteSendHold('upload bytes differ from authorized exact bytes',
                                        definitive_no_post=False)
        # Large upload hashing can consume the remaining margin after preflight.
        _require_retention(permit.retention)
        if consume:
            permit.consumed = True
    return permit


def require(provider_target, image_urls, thumbnail_url=None):
    """Consume the matching committed authority immediately before mutation."""
    return _payload_permit(provider_target, image_urls, thumbnail_url, consume=True)


def check_payload(provider_target, image_urls, *, image_bytes):
    """Preflight exact bytes without authorizing a mutation."""
    return _payload_permit(provider_target, image_urls, image_bytes=image_bytes)


def require_bytes(provider_target, image_urls, *, image_bytes):
    """Consume at the raw upload boundary, binding the actual uploaded bytes."""
    if _active.get() is not None or enabled():
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ExactByteSendHold('bounded exact upload bytes required', definitive_no_post=False)
    return _payload_permit(provider_target, image_urls, consume=True, image_bytes=image_bytes)


def record_outcome(store, permit, outcome, evidence_ref):
    """Record uncertain/definite_no_send/provider_accepted/platform_verified.

Provider receipt proves acceptance; platform_verified requires actual platform
readback. Every outcome retains occupancy. A failed receipt permits no resend.
"""
    if permit is None:
        return False
    if not isinstance(permit, SendPermit) or not permit.consumed or permit.outcome_recorded:
        raise ExactByteSendHold('consumed exact byte attempt required', definitive_no_post=False)
    if (not isinstance(outcome, str) or outcome not in OUTCOMES
            or not isinstance(evidence_ref, str) or not evidence_ref.strip()):
        raise ExactByteSendHold('exact byte outcome evidence required', definitive_no_post=False)
    result = _rpc(store, 'exact_byte_record_outcome_20261010', {
        'p_attempt_id': permit.attempt_id, 'p_claim_token': permit.claim_token,
        'p_outcome': outcome, 'p_evidence_ref': evidence_ref,
    }, definitive_no_post=False)
    if result is not True:
        raise ExactByteSendHold('exact byte outcome recording unavailable', definitive_no_post=False)
    permit.outcome_recorded = True
    return True
