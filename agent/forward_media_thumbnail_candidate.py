"""Prepare exact, replay-verified thumbnail bytes for forward-media persistence.

This candidate is intentionally separate from the existing attester, worker,
SQL, certificate and publisher paths. The caller supplies only trusted-lane
snapshot/manifest data and the already verified original/delivered-image bytes.
Hosted objects are read once per distinct URL; returned bytes are the bytes
that must be persisted, avoiding a second remote read between verification and
persistence.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from .forward_media_attester import replay_still_recipe
from .forward_media_guard import ForwardMediaVerificationHold, MAX_BYTES


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(data: bytes) -> str:
    return 'md5:' + hashlib.md5(data).hexdigest()


def _bounded_bytes(data, label):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ForwardMediaVerificationHold('bounded %s bytes unavailable' % label)
    return data


@dataclass(frozen=True)
class ThumbnailCandidate:
    """Verified nullable thumbnail tuple plus retained bytes for persistence."""
    thumbnail_url: str | None
    thumbnail_sha256: str | None
    thumbnail_fingerprint: str | None
    thumbnail_length: int | None
    thumbnail_recipe: Mapping[str, object] | None
    thumbnail_bytes: bytes | None


def prepare_thumbnail_candidate(*, snapshot: dict, manifest: dict,
                                source_bytes: bytes, image_bytes: bytes,
                                read_bytes: Callable[[str], bytes]) -> ThumbnailCandidate:
    """Validate and replay a thumbnail binding, returning exact verified bytes.

    ``read_bytes`` is invoked at most once for a distinct thumbnail URL. If the
    thumbnail aliases the delivered image URL, the already fetched image bytes
    are reused. ``replay_still_recipe`` checks the recorded recipe/runtime and
    deterministically derives the thumbnail from the verified original or
    delivered image according to the recipe stage.
    """
    if not isinstance(snapshot, dict) or not isinstance(manifest, dict):
        raise ForwardMediaVerificationHold('persisted thumbnail provenance unavailable')
    if manifest.get('image_url') != snapshot.get('image_url'):
        raise ForwardMediaVerificationHold('delivered image URL differs from persisted manifest')
    _bounded_bytes(source_bytes, 'original')
    _bounded_bytes(image_bytes, 'delivered image')
    if not callable(read_bytes):
        raise ForwardMediaVerificationHold('thumbnail object reader unavailable')

    url = manifest.get('thumbnail_url')
    if url != snapshot.get('thumbnail_url'):
        raise ForwardMediaVerificationHold('thumbnail URL differs from persisted snapshot')
    sha256 = manifest.get('thumbnail_sha256')
    fingerprint = manifest.get('thumbnail_fingerprint')
    length = manifest.get('thumbnail_length')
    recipe = manifest.get('render_recipe')

    fields = (url, sha256, fingerprint, length)
    if all(value is None for value in fields):
        if not isinstance(recipe, dict) or recipe.get('thumbnail') is not None:
            raise ForwardMediaVerificationHold('thumbnail recipe exists without hosted object binding')
        return ThumbnailCandidate(None, None, None, None, None, None)
    if (not isinstance(url, str) or not url.strip()
            or not isinstance(sha256, str) or len(sha256) != 64
            or any(ch not in '0123456789abcdef' for ch in sha256)
            or not isinstance(fingerprint, str) or len(fingerprint) != 36
            or not fingerprint.startswith('md5:')
            or any(ch not in '0123456789abcdef' for ch in fingerprint[4:])
            or type(length) is not int or length <= 0 or length > MAX_BYTES):
        raise ForwardMediaVerificationHold('partial or invalid thumbnail object binding')
    if not isinstance(recipe, dict) or recipe.get('thumbnail') is None:
        raise ForwardMediaVerificationHold('thumbnail render recipe binding unavailable')
    # Snapshot and freeze the recipe before invoking even the media-host or
    # remote-read callbacks. Those callbacks cannot change what this candidate
    # claims was verified.
    recipe_snapshot = deepcopy(recipe)
    frozen_recipe = MappingProxyType(deepcopy(recipe_snapshot['thumbnail']))
    from . import visual_writer_prepare
    if not visual_writer_prepare._own_media_url(url):
        raise ForwardMediaVerificationHold('thumbnail URL is outside approved media host')

    image_url = manifest.get('image_url')
    if url == image_url:
        hosted = image_bytes
    else:
        try:
            hosted = read_bytes(url)
        except Exception as exc:
            raise ForwardMediaVerificationHold('hosted thumbnail read unavailable') from exc
    _bounded_bytes(hosted, 'thumbnail')
    if len(hosted) != length or _sha256(hosted) != sha256 or _fingerprint(hosted) != fingerprint:
        raise ForwardMediaVerificationHold('hosted thumbnail bytes differ from persisted binding')

    replay_input = deepcopy(recipe_snapshot)
    replayed = replay_still_recipe(source_bytes, replay_input,
                                   has_thumbnail=True)['thumbnail_bytes']
    if replay_input != recipe_snapshot:
        raise ForwardMediaVerificationHold('thumbnail recipe changed during deterministic replay')
    if not isinstance(replayed, bytes) or replayed != hosted:
        raise ForwardMediaVerificationHold('hosted thumbnail differs from deterministic recipe replay')
    return ThumbnailCandidate(url, sha256, fingerprint, length,
                              frozen_recipe, hosted)
