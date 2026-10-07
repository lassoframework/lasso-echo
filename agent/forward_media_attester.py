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

Versioned still recipes replay the actual feed_image.build_feed_image,
story_image.build_story_image and gbp.crop_4x3 functions directly from verified
original bytes. They bind renderer code, Pillow/codec versions and Story fonts.
Cache contents, flags, URL suffixes and producer observations are not authority.
Unsupported/animated/video/HEIC sources and runtime drift HOLD. Legacy Story
recipes remain compatible with the earlier attester; new owner packets require
the strict versioned recipe and an exact byte replay before persistence.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
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


# Version 1 is an exact recipe contract, not arbitrary Pillow instructions.
# Add formats only after mapping their actual producer and proving byte replay.
_STILL_NAMES = ('identity', 'feed_autofit_4x5', 'story_photo', 'gbp_crop_4x3')
_MAX_PIXELS = 40_000_000


def _runtime_binding(names):
    """Fingerprint the installed renderer/codec/font inputs; never trust a flag."""
    try:
        from PIL import __version__ as pillow_version, features, Image
        from . import feed_image, story_image, gbp, clipper_render
        paths = [__file__, Image.core.__file__]
        if 'feed_autofit_4x5' in names:
            paths.append(feed_image.__file__)
        if 'gbp_crop_4x3' in names:
            paths.append(gbp.__file__)
        if 'story_photo' in names:
            paths.extend((story_image.__file__, clipper_render.__file__))
            paths.extend(os.path.join(story_image._FONT_DIR, name) for name in
                         ('Montserrat-SemiBold.ttf', 'Oswald-Bold.ttf'))
        # No font fallback is accepted: these are the real production inputs.
        hashes = {}
        for path in paths:
            with open(path, 'rb') as handle:
                hashes[os.path.basename(path)] = hashlib.sha256(handle.read()).hexdigest()
        versions = {name: features.version(name) for name in
                    ('jpg', 'zlib', 'freetype2', 'webp', 'raqm')}
        return {'python': platform.python_version(), 'pillow': pillow_version,
                'codecs': versions, 'files': hashes}
    except Exception as exc:
        raise ForwardMediaVerificationHold('renderer runtime binding unavailable') from exc


def _stage(name, caption=None, gym_name=None):
    stage = {'name': name, 'version': 1}
    if name == 'story_photo':
        stage.update(caption=caption, gym_name=gym_name)
    return stage


def make_still_recipe(image_name, *, caption=None, gym_name=None, thumbnail_name=None):
    """Producer helper: record the actual local still renderer contract.

    The result is an observation until the owner independently reads the hosted
    original and delivered objects and verifies byte-for-byte replay. Neither
    runtime fields nor a producer's asset id establish original ownership or
    historical clearance. Thumbnail stages derive from the ORIGINAL, except
    ``delivered_image`` which aliases the delivered image bytes.
    """
    recipe = {'name': 'echo_still_image', 'version': 1,
              'runtime': _runtime_binding((image_name, thumbnail_name)),
              'image': _stage(image_name, caption, gym_name),
              'thumbnail': (_stage(thumbnail_name, caption, gym_name)
                            if thumbnail_name is not None else None)}
    return validate_still_recipe(recipe)


def validate_still_recipe(recipe):
    """Strict schema and exact current runtime check; return a detached copy."""
    if (not isinstance(recipe, dict)
            or set(recipe) != {'name', 'version', 'runtime', 'image', 'thumbnail'}
            or recipe.get('name') != 'echo_still_image'
            or type(recipe.get('version')) is not int or recipe['version'] != 1):
        raise ForwardMediaVerificationHold('versioned still recipe unavailable')
    for field in ('image', 'thumbnail'):
        stage = recipe[field]
        if field == 'thumbnail' and stage is None:
            continue
        allowed = _STILL_NAMES + (('delivered_image',) if field == 'thumbnail' else ())
        if (not isinstance(stage, dict) or stage.get('name') not in allowed
                or type(stage.get('version')) is not int or stage['version'] != 1):
            raise ForwardMediaVerificationHold('unsupported still recipe stage')
        keys = {'name', 'version'}
        if stage['name'] == 'story_photo':
            keys.update(('caption', 'gym_name'))
            for key, limit in (('caption', 10000), ('gym_name', 300)):
                if not isinstance(stage.get(key), str) or len(stage[key]) > limit:
                    raise ForwardMediaVerificationHold('still recipe text unavailable')
        if set(stage) != keys:
            raise ForwardMediaVerificationHold('unsupported still recipe fields')
    names = [recipe['image']['name']]
    if recipe['thumbnail'] is not None:
        names.append(recipe['thumbnail']['name'])
    if recipe.get('runtime') != _runtime_binding(names):
        raise ForwardMediaVerificationHold('still renderer runtime changed; provenance HOLD')
    return json.loads(json.dumps(recipe))


def replay_still_recipe(source_bytes, recipe, *, has_thumbnail=False):
    """Replay from bounded JPEG/PNG/WebP original bytes, with no cache or flags.

    Calls the same functions as the production feed, paired Story and GBP
    producers. Every output must still match hosted bytes at the owner and
    attester boundaries. Unsupported formats fail closed before rendering.
    """
    recipe = validate_still_recipe(recipe)
    if bool(recipe['thumbnail'] is not None) != bool(has_thumbnail):
        raise ForwardMediaVerificationHold('thumbnail recipe binding unavailable')
    if not isinstance(source_bytes, bytes) or not source_bytes or len(source_bytes) > MAX_BYTES:
        raise ForwardMediaVerificationHold('bounded source bytes unavailable')
    from PIL import Image
    from . import feed_image, story_image, gbp
    try:
        with Image.open(io.BytesIO(source_bytes)) as image:
            if (image.format not in ('JPEG', 'PNG', 'WEBP')
                    or getattr(image, 'n_frames', 1) != 1
                    or image.width * image.height > _MAX_PIXELS):
                raise ForwardMediaVerificationHold('unsupported still source format')
            width, height = image.size
            image.verify()
        with tempfile.TemporaryDirectory(prefix='forward_media_still_') as tmpdir:
            source_path = os.path.join(tmpdir, 'original')
            with open(source_path, 'wb') as handle:
                handle.write(source_bytes)

            def render_stage(stage, label, delivered=None):
                name = stage['name']
                if name == 'identity':
                    return source_bytes
                if name == 'delivered_image':
                    return delivered
                output = os.path.join(tmpdir, label + '.jpg')
                if name == 'feed_autofit_4x5':
                    if not feed_image.needs_autofit(width, height):
                        raise ForwardMediaVerificationHold('feed source does not require autofit')
                    feed_image.build_feed_image(source_path, output)
                elif name == 'story_photo':
                    story_image.build_story_image(source_path, output,
                        caption=stage['caption'], gym_name=stage['gym_name'])
                elif name == 'gbp_crop_4x3':
                    gbp.crop_4x3(source_path, output)
                with open(output, 'rb') as handle:
                    data = handle.read(MAX_BYTES + 1)
                if not data or len(data) > MAX_BYTES:
                    raise ForwardMediaVerificationHold('bounded rendered bytes unavailable')
                return data

            image_bytes = render_stage(recipe['image'], 'image')
            thumbnail = (render_stage(recipe['thumbnail'], 'thumbnail', image_bytes)
                         if has_thumbnail else None)
            return {'image_bytes': image_bytes, 'thumbnail_bytes': thumbnail}
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold('controlled still replay failed') from exc


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
        if any(key in recipe for key in ('name', 'version', 'runtime', 'image', 'thumbnail')):
            recipe = validate_still_recipe(recipe)
            if operation == 'reburn' and recipe['image']['name'] != 'story_photo':
                raise ForwardMediaVerificationHold('reburn requires a versioned Story recipe')
            replayed = replay_still_recipe(source_bytes, recipe,
                                          has_thumbnail=manifest_thumb is not None)
            return {'operation': operation, **replayed}
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
