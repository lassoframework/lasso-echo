"""Owner-only preparation helper for the DRAFT forward media claim authority.

Companion to migrations/DRAFT_fixer_forward_media_claim_20261006.sql. That draft
documents the owner-only preparation protocol:

    BEGIN; insert verified original registry tuple; insert clearance using the
    exact tuple and independently audited fleet historical evidence; insert
    versioned render manifest; COMMIT.

This module computes the exact tenant/asset/URL/fingerprint/length tuples and
the sha256 render manifest digest from REAL bytes the owner already verified.
It never connects to a database, never writes production state and never
fabricates evidence: every reference must be supplied by the caller and is only
validated for shape. Clearance decisions follow the draft strictly:

- ONLY a truly fresh, newly produced original (the owner produced these exact
  bytes just now and holds a production evidence reference) AND an independent
  fleet-wide historical audit reference may receive ``cleared_unused``.
- Historical or pre-existing assets, or anything whose byte history is unknown,
  are NEVER auto-cleared here. ``hold_uncertain``/``hold_used`` require their
  own real evidence and permanently quarantine those known bytes.
- Generated (e.g. Astra infographic) originals get ``cleared_unused`` only when
  a genuine generation receipt AND the independent history audit both exist;
  otherwise preparation refuses instead of self-certifying unknown history.
"""
from dataclasses import dataclass
import hashlib
import json
import re

MAX_OBJECT_LENGTH = 134217728  # matches the draft CHECK bounds

_URL_RE = re.compile(r'^https://\S+$')
_FINGERPRINT_RE = re.compile(r'^md5:[0-9a-f]{32}$')
_DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_OPERATIONS = ('same_object', 'render', 'reburn', 'rehost')
_DECISIONS = ('cleared_unused', 'hold_uncertain', 'hold_used')


class PreparationError(ValueError):
    """Raised when preparation inputs cannot support the draft's authority."""


def _require_token(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PreparationError(f'{name} must be a non-blank trimmed token')
    return value


def _require_ref(value, name):
    if not isinstance(value, str) or not value.strip():
        raise PreparationError(f'{name} requires a real evidence reference')
    return value


def _require_url(value, name):
    if not isinstance(value, str) or not _URL_RE.match(value):
        raise PreparationError(f'{name} must be an https URL without whitespace')
    return value


def _require_bytes(value, name):
    if not isinstance(value, (bytes, bytearray)) or not value:
        raise PreparationError(f'{name} requires the actual verified bytes')
    if len(value) > MAX_OBJECT_LENGTH:
        raise PreparationError(f'{name} exceeds the {MAX_OBJECT_LENGTH} byte bound')
    return bytes(value)


def fingerprint_bytes(data):
    """Return ('md5:<hex>', length) for the exact bytes, matching draft CHECKs."""
    data = _require_bytes(data, 'object bytes')
    return 'md5:' + hashlib.md5(data).hexdigest(), len(data)


def _check_fingerprint(value, name):
    if not isinstance(value, str) or not _FINGERPRINT_RE.match(value):
        raise PreparationError(f'{name} must be an md5:<32 lowercase hex> fingerprint')
    return value


@dataclass(frozen=True)
class OriginalRegistration:
    tenant_id: str
    source_asset_id: str
    source_url: str
    source_fingerprint: str
    source_length: int
    registry_evidence_ref: str

    def row(self):
        return {'tenant_id': self.tenant_id, 'source_asset_id': self.source_asset_id,
                'source_url': self.source_url, 'source_fingerprint': self.source_fingerprint,
                'source_length': self.source_length,
                'registry_evidence_ref': self.registry_evidence_ref}


@dataclass(frozen=True)
class HistoryClearance:
    tenant_id: str
    source_asset_id: str
    source_url: str
    source_fingerprint: str
    source_length: int
    registry_evidence_ref: str
    decision: str
    history_evidence_ref: str

    def row(self):
        return {'tenant_id': self.tenant_id, 'source_asset_id': self.source_asset_id,
                'source_url': self.source_url, 'source_fingerprint': self.source_fingerprint,
                'source_length': self.source_length,
                'registry_evidence_ref': self.registry_evidence_ref,
                'decision': self.decision, 'history_evidence_ref': self.history_evidence_ref}


@dataclass(frozen=True)
class RenderManifest:
    manifest_digest: str
    tenant_id: str
    source_asset_id: str
    image_url: str
    image_fingerprint: str
    image_length: int
    thumbnail_url: str
    thumbnail_fingerprint: str
    thumbnail_length: int
    operation: str
    render_recipe: dict
    render_evidence_ref: str

    def row(self):
        return {'manifest_digest': self.manifest_digest, 'tenant_id': self.tenant_id,
                'source_asset_id': self.source_asset_id, 'image_url': self.image_url,
                'image_fingerprint': self.image_fingerprint, 'image_length': self.image_length,
                'thumbnail_url': self.thumbnail_url,
                'thumbnail_fingerprint': self.thumbnail_fingerprint,
                'thumbnail_length': self.thumbnail_length, 'operation': self.operation,
                'render_recipe': self.render_recipe,
                'render_evidence_ref': self.render_evidence_ref}


def register_original(tenant_id, source_asset_id, source_url, source_bytes,
                      registry_evidence_ref):
    """Build the original-registry tuple from owner-verified bytes.

    The fingerprint and length are computed from the supplied bytes; the caller
    must have independently verified that source_url serves exactly these bytes.
    """
    fingerprint, length = fingerprint_bytes(source_bytes)
    return OriginalRegistration(
        tenant_id=_require_token(tenant_id, 'tenant_id'),
        source_asset_id=_require_token(source_asset_id, 'source_asset_id'),
        source_url=_require_url(source_url, 'source_url'),
        source_fingerprint=fingerprint,
        source_length=length,
        registry_evidence_ref=_require_ref(registry_evidence_ref, 'registry_evidence_ref'))


def clear_history(original, decision, history_evidence_ref,
                  production_evidence_ref=None):
    """Build the history-clearance tuple for a registered original.

    ``cleared_unused`` is restricted to truly fresh newly produced originals:
    it requires BOTH an owner production/generation evidence reference proving
    these bytes were just produced AND an independently audited fleet-wide
    historical evidence reference. Anything else must be held; this helper
    never upgrades a historical asset or unknown history to cleared.
    """
    if not isinstance(original, OriginalRegistration):
        raise PreparationError('clearance requires a registered original tuple')
    if decision not in _DECISIONS:
        raise PreparationError(f'decision must be one of {_DECISIONS}')
    if decision == 'cleared_unused':
        _require_ref(production_evidence_ref,
                     'production_evidence_ref proving these bytes were newly produced')
    return HistoryClearance(
        tenant_id=original.tenant_id, source_asset_id=original.source_asset_id,
        source_url=original.source_url, source_fingerprint=original.source_fingerprint,
        source_length=original.source_length,
        registry_evidence_ref=original.registry_evidence_ref,
        decision=decision,
        history_evidence_ref=_require_ref(history_evidence_ref, 'history_evidence_ref'))


def _manifest_payload(tenant_id, source_asset_id, image_url, image_fingerprint,
                      image_length, thumbnail_url, thumbnail_fingerprint,
                      thumbnail_length, operation, render_recipe, render_evidence_ref):
    return {'tenant_id': tenant_id, 'source_asset_id': source_asset_id,
            'image_url': image_url, 'image_fingerprint': image_fingerprint,
            'image_length': image_length, 'thumbnail_url': thumbnail_url,
            'thumbnail_fingerprint': thumbnail_fingerprint,
            'thumbnail_length': thumbnail_length, 'operation': operation,
            'render_recipe': render_recipe, 'render_evidence_ref': render_evidence_ref}


def manifest_digest(payload):
    """Deterministic sha256 digest over the canonical manifest payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def build_render_manifest(original, image_url, image_bytes, operation,
                          render_evidence_ref, thumbnail_url=None,
                          thumbnail_bytes=None, render_recipe=None):
    """Build a versioned render manifest bound to a registered original.

    Image (and optional thumbnail) fingerprints/lengths are computed from the
    actual delivered bytes. Thumbnail fields are all-or-nothing, matching the
    draft CHECK. ``operation`` must be one of the controlled operations; a
    ``same_object`` manifest must deliver the original's exact bytes and URL.
    """
    if not isinstance(original, OriginalRegistration):
        raise PreparationError('manifest requires a registered original tuple')
    if operation not in _OPERATIONS:
        raise PreparationError(f'operation must be one of {_OPERATIONS}')
    image_fingerprint, image_length = fingerprint_bytes(image_bytes)
    image_url = _require_url(image_url, 'image_url')
    if operation == 'same_object':
        if image_url != original.source_url or image_fingerprint != original.source_fingerprint \
                or image_length != original.source_length:
            raise PreparationError('same_object must deliver the original exact bytes and URL')
    if (thumbnail_url is None) != (thumbnail_bytes is None):
        raise PreparationError('thumbnail URL and bytes are all-or-nothing')
    thumb_url = thumb_fp = thumb_len = None
    if thumbnail_bytes is not None:
        thumb_fp, thumb_len = fingerprint_bytes(thumbnail_bytes)
        thumb_url = _require_url(thumbnail_url, 'thumbnail_url')
    if render_recipe is not None and not isinstance(render_recipe, dict):
        raise PreparationError('render_recipe must be a JSON object when supplied')
    payload = _manifest_payload(original.tenant_id, original.source_asset_id,
                                image_url, image_fingerprint, image_length,
                                thumb_url, thumb_fp, thumb_len, operation,
                                render_recipe,
                                _require_ref(render_evidence_ref, 'render_evidence_ref'))
    digest = manifest_digest(payload)
    if not _DIGEST_RE.match(digest):
        raise PreparationError('internal digest failure')
    return RenderManifest(manifest_digest=digest, render_recipe=render_recipe,
                          **{k: payload[k] for k in payload if k not in ('render_recipe',)})


def prepare_generated_original(tenant_id, source_asset_id, source_url, source_bytes,
                               generation_evidence_ref, registry_evidence_ref,
                               history_evidence_ref):
    """Owner preparation for a generated (e.g. Astra infographic) original.

    A generated path may ONLY proceed when genuine independent evidence exists:
    a real generation receipt proving these exact bytes were newly produced,
    plus the independently audited fleet historical evidence. Without both, the
    helper refuses rather than self-certifying unknown history. Returns
    (OriginalRegistration, HistoryClearance) with decision cleared_unused.
    """
    _require_ref(generation_evidence_ref, 'generation_evidence_ref (real generation receipt)')
    original = register_original(tenant_id, source_asset_id, source_url, source_bytes,
                                 registry_evidence_ref)
    clearance = clear_history(original, 'cleared_unused', history_evidence_ref,
                              production_evidence_ref=generation_evidence_ref)
    return original, clearance
