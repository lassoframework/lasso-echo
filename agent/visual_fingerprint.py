"""Stable, explicit visual byte-identity namespaces.

Identity and similarity are deliberately separate:

* ``source:sha256:<hex>`` is the authoritative identity of source bytes.
* ``source:md5:<hex>`` is a compatibility alias for Drive ``md5Checksum``.
* ``derived:sha256:<hex>`` identifies transformed/rendered bytes.
* ``derived:md5:<hex>`` is a compatibility alias used to recognize an Echo
  render after Drive indexes the rendered file by MD5.

Paths, URLs, object keys, Drive IDs, and perceptual hashes are locators or
similarity evidence. They are never byte identity. Unknown/empty bytes fail
closed instead of producing an invented identifier.
"""
from __future__ import annotations

import hashlib
import re

SOURCE_SHA256 = "source:sha256"
SOURCE_MD5 = "source:md5"
DERIVED_SHA256 = "derived:sha256"
DERIVED_MD5 = "derived:md5"

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAMESPACES = {
    SOURCE_SHA256: _HEX64,
    SOURCE_MD5: _HEX32,
    DERIVED_SHA256: _HEX64,
    DERIVED_MD5: _HEX32,
}


def _require_bytes(data: bytes) -> bytes:
    if not isinstance(data, bytes) or not data:
        raise ValueError("non-empty bytes are required for visual identity")
    return data


def source_sha256(data: bytes) -> str:
    """Bare SHA-256 hex of source bytes (the authoritative digest)."""
    return hashlib.sha256(_require_bytes(data)).hexdigest()


def source_md5(data: bytes) -> str:
    """Bare MD5 hex for an explicit Drive compatibility alias."""
    return hashlib.md5(_require_bytes(data)).hexdigest()


def fingerprint(data: bytes) -> str:
    """Authoritative source-byte fingerprint."""
    return f"{SOURCE_SHA256}:{source_sha256(data)}"


def source_aliases(data: bytes) -> list[str]:
    """Strong source identity first, followed by its Drive MD5 alias."""
    return [fingerprint(data), f"{SOURCE_MD5}:{source_md5(data)}"]


def derived_fingerprint(data: bytes) -> str:
    """Authoritative identity of transformed/rendered bytes."""
    return f"{DERIVED_SHA256}:{hashlib.sha256(_require_bytes(data)).hexdigest()}"


def derived_aliases(data: bytes) -> list[str]:
    """Strong derived identity first, then the Drive re-ingest MD5 alias."""
    data = _require_bytes(data)
    return [derived_fingerprint(data),
            f"{DERIVED_MD5}:{hashlib.md5(data).hexdigest()}"]


def fingerprint_file(path) -> str:
    with open(path, "rb") as fh:
        return fingerprint(fh.read())


def source_aliases_file(path) -> list[str]:
    with open(path, "rb") as fh:
        return source_aliases(fh.read())


def derived_aliases_file(path) -> list[str]:
    with open(path, "rb") as fh:
        return derived_aliases(fh.read())


def normalize(value, *, namespace=SOURCE_SHA256) -> str | None:
    """Validate a namespaced fingerprint without guessing its namespace.

    Bare digests are rejected here. Call :func:`from_drive_md5` for Drive's
    explicitly typed bare ``md5Checksum`` value.
    """
    if not isinstance(value, str) or namespace not in _NAMESPACES:
        return None
    v = value.strip().lower()
    prefix = f"{namespace}:"
    if not v.startswith(prefix):
        return None
    digest = v[len(prefix):]
    return v if _NAMESPACES[namespace].fullmatch(digest) else None


def normalize_any(value) -> str | None:
    """Validate any recognized explicit byte-identity namespace."""
    for namespace in _NAMESPACES:
        normalized = normalize(value, namespace=namespace)
        if normalized:
            return normalized
    return None


def is_fingerprint(value, *, namespace=SOURCE_SHA256) -> bool:
    return normalize(value, namespace=namespace) is not None


def from_drive_md5(md5_checksum, *, derived=False) -> str | None:
    """Type Drive's bare MD5 attestation as a source or derived alias."""
    if not isinstance(md5_checksum, str):
        return None
    digest = md5_checksum.strip().lower()
    if not _HEX32.fullmatch(digest):
        return None
    namespace = DERIVED_MD5 if derived else SOURCE_MD5
    return f"{namespace}:{digest}"


def attest_drive(md5_checksum, data: bytes, *, derived=False) -> bool:
    """Verify Drive's MD5 attestation against bytes in the requested domain."""
    alias = from_drive_md5(md5_checksum, derived=derived)
    if alias is None or not isinstance(data, bytes) or not data:
        return False
    aliases = derived_aliases(data) if derived else source_aliases(data)
    return alias in aliases


def legacy_drive_digest(alias) -> str | None:
    """Return bare MD5 only from an explicitly typed MD5 alias.

    This boundary exists for legacy stores whose schema calls Drive MD5 a
    ``content_hash``. SHA-256 and source/derived domains are never collapsed.
    """
    normalized = normalize_any(alias)
    if not normalized:
        return None
    domain, algorithm, digest = normalized.split(":", 2)
    if algorithm != "md5" or domain not in {"source", "derived"}:
        return None
    return digest
