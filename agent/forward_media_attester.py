"""Production trusted callbacks for agent.forward_media_guard.attest().

The guard's attest() entry point requires two trusted-lane callbacks:
``original_verifier(snapshot, source_bytes)`` and
``controlled_renderer(source_bytes, snapshot)``. This module builds the
production versions from a persisted narrow attester lookup ONLY. Callers
(publishers, intake) never supply hashes, URLs or render lineage; every
binding below comes from the trusted attester's own read of:

- ``fixer_forward_media_original_registry_20261006`` (original provenance:
  tenant + source_media_asset_id + immutable hosted source URL + exact
  bytes/fingerprint), and
- ``fixer_forward_media_render_manifest_20261006`` (versioned render recipe
  binding a manifest digest to exact delivered objects).

Anything caller-supplied, missing, forged or unsupported is a
ForwardMediaVerificationHold — never an attestation.

Missing-recipe policy (documented, not invented proof):
- ``same_object`` / ``rehost`` need no render recipe; the guard compares
  exact bytes and never reaches the renderer.
- ``reburn`` and ``render`` are replayable ONLY when the persisted manifest
  carries the complete recipe observed in agent/story_reburn.py and
  agent/story_image.py: {caption, gym_name} plus the exact source bytes.
  ``story_image.get_or_make_story_image`` keys its cache on sha256(source
  bytes)+caption and re-renders deterministically from those inputs, so an
  exact replay is possible from persisted data.
- If the manifest lacks the recipe (or names an operation with no current
  deterministic replay implementation — e.g. paired feed-card renders whose
  inputs are not persisted), the renderer HOLDS and names the missing
  recipe field(s) rather than approximating ancestry.
- Thumbnails have NO persisted render recipe in current code
  (agent/story_reburn.py produces a single burned object). A transformed
  thumbnail (thumbnail_url distinct from image_url with different bytes)
  therefore always HOLDs until a versioned thumbnail recipe exists.
"""
from __future__ import annotations

import hashlib
import os
import tempfile

from .forward_media_guard import ForwardMediaVerificationHold

MAX_BYTES = 128 * 1024 * 1024
SUPPORTED_REPLAY_OPERATIONS = ('render', 'reburn')


def _fingerprint(data):
    return 'md5:' + hashlib.md5(data).hexdigest()


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ForwardMediaVerificationHold(
            'persisted %s unavailable; provenance HOLD' % field)
    return value.strip()


# --------------------------------------------------------------------------
# Original provenance verifier
# --------------------------------------------------------------------------

def make_original_verifier(registry_lookup, *, expected_revision=None):
    """Return a trusted ``original_verifier`` callback for attest().

    ``registry_lookup(tenant_id, source_asset_id)`` must be the narrow
    trusted-lane read of fixer_forward_media_original_registry_20261006
    returning one row dict or None. The callback binds gym/tenant, source
    asset identity, the immutable hosted source URL and the exact source
    bytes; caller-supplied hashes/URLs are never consulted. When
    ``expected_revision`` is given, a snapshot at any other revision HOLDs
    (stale calendar revision bound to this lookup context).
    """
    if not callable(registry_lookup):
        raise ForwardMediaVerificationHold('trusted registry lookup unavailable')

    def verify(snapshot, source_bytes):
        if not isinstance(snapshot, dict):
            raise ForwardMediaVerificationHold('persisted snapshot unavailable')
        revision = snapshot.get('revision')
        if expected_revision is not None and revision != expected_revision:
            raise ForwardMediaVerificationHold('stale calendar revision for attester lookup')
        tenant = _require_text(snapshot.get('tenant_id'), 'tenant identity')
        asset_id = _require_text(snapshot.get('source_asset_id'),
                                 'source media asset identity')
        source_url = _require_text(snapshot.get('source_url'), 'source object URL')
        if not isinstance(source_bytes, bytes) or not source_bytes or len(source_bytes) > MAX_BYTES:
            raise ForwardMediaVerificationHold('bounded source bytes unavailable')
        row = registry_lookup(tenant, asset_id)
        if not isinstance(row, dict):
            raise ForwardMediaVerificationHold(
                'original provenance HOLD: no registry row for tenant/asset '
                '(missing or forged registry entry)')
        if _require_text(row.get('tenant_id'), 'registry tenant') != tenant:
            raise ForwardMediaVerificationHold(
                'original provenance HOLD: registry tenant mismatch')
        if _require_text(row.get('source_asset_id'), 'registry asset') != asset_id:
            raise ForwardMediaVerificationHold(
                'original provenance HOLD: registry asset identity mismatch')
        if _require_text(row.get('source_url'), 'registry source URL') != source_url:
            raise ForwardMediaVerificationHold(
                'original provenance HOLD: hosted URL is not the registered '
                'immutable original version')
        length = row.get('source_length')
        if (not isinstance(length, int) or length <= 0 or length > MAX_BYTES
                or length != len(source_bytes)):
            raise ForwardMediaVerificationHold(
                'original provenance HOLD: source byte length differs from registry')
        if _require_text(row.get('source_fingerprint'), 'registry fingerprint') \
                != _fingerprint(source_bytes):
            raise ForwardMediaVerificationHold(
                'original provenance HOLD: source object bytes changed since '
                'registration')
        return True

    return verify


# --------------------------------------------------------------------------
# Controlled renderer (deterministic replay only)
# --------------------------------------------------------------------------

def _story_burn(source_bytes, caption, gym_name, source_url):
    """Replay the story caption burn from agent/story_reburn.py exactly.

    Uses story_image.get_or_make_story_image / get_or_make_story_video on the
    verified original bytes, in an isolated temp library so the
    content-addressed cache (sha256(source)+caption key) cannot substitute a
    foreign object. Returns the rendered bytes or None on render failure.
    """
    from . import story_image
    from .media_types import VIDEO_EXTS
    ext = os.path.splitext(str(source_url).split('?')[0])[1].lower() or '.jpg'
    with tempfile.TemporaryDirectory(prefix='forward_media_replay_') as tmpdir:
        src = os.path.join(tmpdir, 'source' + ext)
        try:
            with open(src, 'wb') as fh:
                fh.write(source_bytes)
            if ext in VIDEO_EXTS:
                asset = story_image.get_or_make_story_video(src, caption, gym_name, tmpdir)
            else:
                asset = story_image.get_or_make_story_image(src, caption, gym_name, tmpdir)
            if not asset or os.path.dirname(os.path.abspath(asset)) != os.path.abspath(tmpdir) \
                    and not os.path.abspath(asset).startswith(os.path.abspath(tmpdir) + os.sep):
                return None
            with open(asset, 'rb') as fh:
                return fh.read()
        except Exception:
            return None


def make_controlled_renderer(manifest_lookup, *, burn=None):
    """Return a trusted ``controlled_renderer`` callback for attest().

    ``manifest_lookup(manifest_digest, tenant_id)`` must be the narrow
    trusted-lane read of fixer_forward_media_render_manifest_20261006
    returning one row dict or None. Only operations in
    SUPPORTED_REPLAY_OPERATIONS with a fully persisted recipe replay; every
    other case HOLDs and names the missing recipe in the hold message.
    ``burn`` is injectable only for isolated tests; production uses
    _story_burn.
    """
    if not callable(manifest_lookup):
        raise ForwardMediaVerificationHold('trusted render manifest lookup unavailable')
    replay = burn or _story_burn

    def render(source_bytes, snapshot):
        if not isinstance(snapshot, dict):
            raise ForwardMediaVerificationHold('persisted snapshot unavailable')
        tenant = _require_text(snapshot.get('tenant_id'), 'tenant identity')
        digest = _require_text(snapshot.get('render_manifest_digest'),
                               'render manifest digest')
        row = manifest_lookup(digest, tenant)
        if not isinstance(row, dict):
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: no persisted manifest for this revision')
        if _require_text(row.get('tenant_id'), 'manifest tenant') != tenant:
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: manifest tenant mismatch')
        if (_require_text(row.get('manifest_digest'), 'manifest digest') != digest
                or _require_text(row.get('source_asset_id'), 'manifest source asset') !=
                _require_text(snapshot.get('source_asset_id'), 'snapshot source asset')):
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: manifest source or revision mismatch')
        operation = _require_text(row.get('operation'), 'manifest operation')
        if operation not in SUPPORTED_REPLAY_OPERATIONS:
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: operation %r has no deterministic replay '
                'in current rendering code; a versioned recipe must be persisted '
                'before this operation can attest' % (operation,))
        # The delivered immutable object identity must come from the persisted
        # manifest bound to this tenant/digest, never from request data.
        if _require_text(row.get('image_url'), 'manifest image URL') \
                != _require_text(snapshot.get('image_url'), 'snapshot image URL'):
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: hosted image is not the manifest version')
        manifest_thumb = row.get('thumbnail_url')
        if manifest_thumb != snapshot.get('thumbnail_url'):
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: thumbnail identity differs from manifest version')
        recipe = row.get('render_recipe')
        if not isinstance(recipe, dict):
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: missing persisted recipe fields '
                '{caption, gym_name} for %s replay' % operation)
        caption = _require_text(recipe.get('caption'), 'recipe caption')
        gym_name = _require_text(recipe.get('gym_name'), 'recipe gym_name')
        if not isinstance(source_bytes, bytes) or not source_bytes or len(source_bytes) > MAX_BYTES:
            raise ForwardMediaVerificationHold('bounded source bytes unavailable')
        image_bytes = replay(source_bytes, caption, gym_name, snapshot['source_url'])
        if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_BYTES:
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: controlled %s replay failed determinism' % operation)
        # No persisted thumbnail recipe exists in current code
        # (agent/story_reburn.py delivers a single object). A distinct
        # thumbnail therefore cannot be proven and must HOLD.
        if snapshot.get('thumbnail_url') not in (None, snapshot.get('image_url')):
            raise ForwardMediaVerificationHold(
                'render ancestry HOLD: missing recipe for transformed thumbnail; '
                'current rendering code persists no thumbnail render recipe')
        return {'operation': operation, 'image_bytes': image_bytes,
                'thumbnail_bytes': (image_bytes if snapshot.get('thumbnail_url')
                                    else None)}

    return render


# --------------------------------------------------------------------------
# Narrow trusted-lane lookups (attester connection only)
# --------------------------------------------------------------------------

def production_callbacks(conn, calendar_row_id, *, expected_revision):
    """Read one bound provenance RPC with the attester role; no table grants."""
    from .forward_media_guard import _uuid
    with conn.cursor() as cur:
        cur.execute('select public.fixer_forward_media_provenance_lookup_20261006(%s)',
                    (_uuid(calendar_row_id),))
        result = cur.fetchone()
    provenance = result[0] if result else None
    if (not isinstance(provenance, dict)
            or not isinstance(provenance.get('original'), dict)
            or not isinstance(provenance.get('manifest'), dict)):
        raise ForwardMediaVerificationHold('narrow trusted provenance lookup unavailable')
    original, manifest = provenance['original'], provenance['manifest']
    return (
        make_original_verifier(
            lambda tenant, asset: original if (original.get('tenant_id') == tenant
                                               and original.get('source_asset_id') == asset) else None,
            expected_revision=expected_revision),
        make_controlled_renderer(
            lambda digest, tenant: manifest if (manifest.get('manifest_digest') == digest
                                                and manifest.get('tenant_id') == tenant) else None),
    )
