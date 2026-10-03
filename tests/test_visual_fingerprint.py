"""Visual identity authority tests. Fully offline."""

import hashlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_media_sync, story_studio, visual_fingerprint as vf  # noqa: E402

ABC_MD5 = "900150983cd24fb0d6963f7d28e17f72"
ABC_SHA256 = hashlib.sha256(b"abc").hexdigest()


def test_source_sha256_is_authority_and_drive_md5_is_explicit_alias():
    aliases = vf.source_aliases(b"abc")
    assert aliases == [
        f"source:sha256:{ABC_SHA256}",
        f"source:md5:{ABC_MD5}",
    ]
    assert vf.fingerprint(b"abc") == aliases[0]
    assert vf.from_drive_md5(ABC_MD5) == aliases[1]
    assert vf.attest_drive(ABC_MD5, b"abc") is True
    assert vf.attest_drive(ABC_MD5, b"different bytes") is False


def test_derived_namespace_cannot_alias_source_namespace():
    aliases = vf.derived_aliases(b"abc")
    assert aliases == [
        f"derived:sha256:{ABC_SHA256}",
        f"derived:md5:{ABC_MD5}",
    ]
    assert vf.normalize(aliases[0]) is None
    assert vf.normalize_any(aliases[0]) == aliases[0]
    assert vf.from_drive_md5(ABC_MD5, derived=True) == aliases[1]
    assert vf.legacy_drive_digest(aliases[1]) == ABC_MD5


def test_empty_nonbytes_and_unknown_source_fail_closed():
    for value in (b"", "abc", None):
        with pytest.raises(ValueError):
            vf.fingerprint(value)
    assert vf.attest_drive(ABC_MD5, b"") is False
    assert vf.from_drive_md5(None) is None
    assert vf.from_drive_md5("") is None


def test_normalize_requires_explicit_namespace():
    strong = f"source:sha256:{ABC_SHA256}"
    assert vf.normalize(strong) == strong
    assert vf.normalize(strong.upper()) == strong
    assert vf.is_fingerprint(strong) is True
    assert vf.normalize(ABC_MD5) is None
    assert vf.normalize(ABC_SHA256) is None
    assert vf.normalize(f"md5:{ABC_MD5}") is None


def test_paths_urls_ids_and_perceptual_hashes_are_never_identity():
    values = [
        "content_library/gritx/photo.jpg",
        "intake/gritx/incoming/20260810_photo.jpg",
        "https://cdn.example.com/echo/gritx/aaaa1111/x.jpg",
        "1Ab2Cd3Ef4Gh5Ij6Kl7",
        "perceptual:0000000000000000",
        "ph:0123456789abcdef",
        None,
        123,
    ]
    assert all(vf.normalize_any(value) is None for value in values)


def test_file_helpers_hash_bytes_not_paths(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "renamed.mp4"
    first.write_bytes(b"abc")
    second.write_bytes(b"abc")
    assert vf.source_aliases_file(first) == vf.source_aliases_file(second)
    assert vf.derived_aliases_file(first) == vf.derived_aliases_file(second)
    with pytest.raises(OSError):
        vf.fingerprint_file(tmp_path / "missing.jpg")
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        vf.fingerprint_file(empty)


def test_media_sidecar_keeps_strong_authority_and_explicit_aliases(tmp_path):
    aliases = vf.source_aliases(b"client photo")
    client_media_sync._write_sidecar(
        str(tmp_path), "photo.jpg", "intake/gym/incoming/photo.jpg", "",
        lambda *_args: None, source_fingerprint=aliases[0],
        source_fingerprint_aliases=aliases[1:])
    import json
    sidecar = json.loads((tmp_path / "photo.json").read_text())
    assert sidecar["source_fingerprint"] == aliases[0]
    assert sidecar["source_fingerprint_aliases"] == aliases[1:]


def test_story_render_maps_derived_bytes_to_segment_sources_before_cleanup(tmp_path):
    source = tmp_path / "source.mov"
    render = tmp_path / "render.mp4"
    source.write_bytes(b"source bytes")
    render.write_bytes(b"derived render bytes")
    segment = SimpleNamespace(asset_id="drive-file-1", source_path=str(source))

    identities, fingerprints, unknown = story_studio._segment_source_identities(
        [segment], {"drive-file-1": {"content_hash": vf.source_md5(b"source bytes")}})
    derived, derived_aliases, legacy = story_studio._derived_identity(render)

    assert unknown == 0
    assert fingerprints == [vf.fingerprint(b"source bytes")]
    assert identities[0]["source_fingerprint"] == fingerprints[0]
    assert f"source:md5:{vf.source_md5(b'source bytes')}" in (
        identities[0]["source_fingerprint_aliases"])
    assert derived == vf.derived_fingerprint(b"derived render bytes")
    assert derived.startswith("derived:sha256:")
    assert all(alias.startswith("derived:") for alias in derived_aliases)
    assert legacy == hashlib.sha256(b"derived render bytes").hexdigest()
    assert story_studio._derived_ledger_keys(derived, derived_aliases, legacy) == [
        hashlib.sha256(b"derived render bytes").hexdigest(),
        vf.source_md5(b"derived render bytes"),
    ]


def test_story_unknown_source_keeps_typed_drive_alias_but_no_authority():
    segment = SimpleNamespace(asset_id="drive-file-1", source_path="")
    identities, fingerprints, unknown = story_studio._segment_source_identities(
        [segment], {"drive-file-1": {"content_hash": ABC_MD5}})
    assert fingerprints == []
    assert unknown == 1
    assert identities == [{
        "asset_id": "drive-file-1",
        "source_fingerprint": None,
        "source_fingerprint_aliases": [f"source:md5:{ABC_MD5}"],
    }]


def test_unmaterialized_render_path_is_never_byte_identity(tmp_path):
    identity, aliases, legacy = story_studio._derived_identity(
        tmp_path / "does-not-exist.mp4")
    assert identity is None
    assert aliases == []
    assert legacy.startswith("unmaterialized:sha256:")
