"""Draft, opt-in preparation for exact visual-group calendar writes.

This is a writer-side belt, not an activation or a substitute for the database
trigger. An RPC failure or an unverified delivered object aborts the write.
"""
from __future__ import annotations

import os
import hashlib
import uuid
from urllib.parse import unquote, urlsplit, urlunsplit

from . import visual_fingerprint as fingerprint

MAX_VISUAL_BYTES = 128 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class VisualPreparationError(ValueError):
    pass


def enabled() -> bool:
    return os.environ.get("AGENT_VISUAL_GLOBAL_WRITER_PREP", "").lower() in ("1", "true", "yes", "on")


def _rpc(store, name, arguments):
    response = store._client().post(
        store._rest("rpc/" + name), headers=store._headers({"Content-Type": "application/json"}),
        json=arguments, timeout=30)
    if response.status_code >= 400:
        raise VisualPreparationError(f"visual preparation RPC {name} failed ({response.status_code})")
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise VisualPreparationError(f"visual preparation RPC {name} returned invalid JSON") from exc


def _tenant(store, account_key):
    key = str(account_key or "").strip()
    if not key:
        raise VisualPreparationError("calendar key has no canonical visual tenant")
    response = store._client().get(
        store._rest("tenant_alias"),
        params={"select": "alias_key,tenant_id", "alias_key": f"eq.{key}", "limit": "1"},
        headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise VisualPreparationError("canonical visual tenant lookup failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("alias_key") != key:
        raise VisualPreparationError("calendar key has no canonical visual tenant")
    try:
        return str(uuid.UUID(str(rows[0].get("tenant_id"))))
    except (TypeError, ValueError, AttributeError) as exc:
        raise VisualPreparationError("calendar key has no canonical visual tenant") from exc


def _own_media_url(url):
    """Accept only the configured public bucket origin and path, exactly."""
    from . import media_host
    base = (media_host.config.S3_PUBLIC_BASE_URL or "").rstrip("/")
    if not isinstance(url, str) or not base or any(c.isspace() for c in url):
        return False
    try:
        parsed, allowed = urlsplit(url), urlsplit(base)
    except ValueError:
        return False
    path_parts = [unquote(part) for part in parsed.path.split("/")]
    return bool(
        allowed.scheme == "https" and parsed.scheme == "https" and
        parsed.netloc == allowed.netloc and parsed.hostname == allowed.hostname and
        not allowed.username and not allowed.password and not allowed.query and
        not allowed.fragment and not parsed.username and not parsed.password and
        not parsed.fragment and
        parsed.path.startswith(allowed.path.rstrip("/") + "/") and
        len(parsed.path) > len(allowed.path.rstrip("/")) + 1 and
        all(part not in (".", "..") and "/" not in part and "\\" not in part
            and not any(char.isspace() or ord(char) < 32 for char in part)
            for part in path_parts)
    )


def _bounded_chunks(chunks):
    data = bytearray()
    for chunk in chunks:
        if chunk:
            if len(data) + len(chunk) > MAX_VISUAL_BYTES:
                return None
            data.extend(chunk)
    return bytes(data) if data else None


def _bytes_for_url(url):
    # A URL/Drive id is never a byte attestation. Only our configured bucket
    # is readable; redirects and unbounded responses fail closed.
    from . import media_host
    if not _own_media_url(url):
        return None
    parsed = urlsplit(url)
    if parsed.query:
        # A query may select different served bytes. Read that exact URL;
        # stripping it and reading the underlying R2 key would mis-attest it.
        import requests
        with requests.get(url, timeout=(5, 30), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return None
            return _bounded_chunks(response.iter_content(chunk_size=_READ_CHUNK))
    object_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    key = media_host._key_from_public_url(object_url)
    if not key:
        return None
    client = media_host._default_client()
    if client is None or not hasattr(client, "_s3"):
        return None
    response = client._s3.get_object(Bucket=client._bucket, Key=key)
    body = response["Body"]
    try:
        if response.get("ContentLength", 0) > MAX_VISUAL_BYTES:
            return None
        return _bounded_chunks(iter(lambda: body.read(_READ_CHUNK), b""))
    finally:
        body.close()


def _asset(store, tenant, asset_id):
    response = store._client().get(
        store._rest("media_asset"),
        params={"select": "id,gym_id,content_hash", "id": f"eq.{asset_id}", "limit": "1"},
        headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise VisualPreparationError("tenant media asset lookup failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1 or str(rows[0].get("id")) != str(asset_id):
        raise VisualPreparationError("source asset is not registered to canonical tenant")
    if _tenant(store, str(rows[0].get("gym_id") or "")) != tenant:
        raise VisualPreparationError("source asset belongs to another tenant")
    return rows[0]


def _exact_bytes(url, reader, role):
    if (not isinstance(url, str) or url != url.strip() or
            any(char.isspace() for char in url) or urlsplit(url).fragment or
            urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc):
        raise VisualPreparationError(f"{role} exact URL is invalid")
    try:
        data = reader(url)
    except Exception as exc:
        raise VisualPreparationError(f"{role} media bytes could not be read") from exc
    if not isinstance(data, bytes) or not data or len(data) > MAX_VISUAL_BYTES:
        raise VisualPreparationError(f"{role} media bytes could not be verified")
    return data


def _md5(data):
    return "md5:" + hashlib.md5(data).hexdigest()


def _known_group(store, tenant, row, source_url, *, required=True):
    response = store._client().get(
        store._rest("visual_group_alias"),
        params={"select": "group_key", "gym_id": f"eq.{tenant}",
                "alias_kind": "eq.canonical_url", "alias_value": f"eq.{source_url}", "limit": "2"},
        headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise VisualPreparationError("source scene lookup failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) > 1:
        raise VisualPreparationError("source scene has no unambiguous registered group")
    if not rows:
        if required:
            raise VisualPreparationError("source scene has no unambiguous registered group")
        return None
    group = rows[0].get("group_key")
    if row.get("visual_group_key") and row["visual_group_key"] != group:
        raise VisualPreparationError("source scene conflicts with row visual group")
    return group


def _register_raw_source(store, tenant, prepared, source_url, source, asset):
    """Bind the observed raw object before any rendition receipt consumes it."""
    if not _own_media_url(source_url):
        raise VisualPreparationError("raw source URL is outside the configured media host")
    source_hash = _md5(source)
    aliases = []
    asset_id = prepared.get("source_media_asset_id")
    if asset:
        aliases.extend((("source_asset", str(asset_id)),
                        ("byte_hash", fingerprint.from_drive_md5(asset["content_hash"]))))
    aliases.extend((("byte_hash", "derived:" + source_hash),
                    ("canonical_url", source_url)))
    drive_id = prepared.get("drive_file_id")
    if drive_id:
        if not asset or str(drive_id) != str(asset_id):
            raise VisualPreparationError("Drive ID has no matching tenant asset")
        aliases.append(("drive_id", str(drive_id)))
    result = _rpc(store, "visual_global_prepare_bundle", {
        "p_tenant": tenant,
        "p_aliases": [{"alias_kind": kind, "alias_value": value} for kind, value in aliases],
        "p_fingerprint": source_hash,
        "p_evidence": {"source": "raw_object_bytes", "verified_bytes": source_hash,
                       "delivered_url": source_url},
        "p_actor": "visual_writer_prepare",
        "p_asset_id": str(asset_id) if asset else None,
    })
    if not isinstance(result, dict) or result.get("fingerprint") != source_hash:
        raise VisualPreparationError("raw source registration returned invalid fingerprint")
    group = result.get("group_key")
    if not isinstance(group, str) or not group.startswith("vg_"):
        raise VisualPreparationError("raw source registration returned no group")
    if prepared.get("visual_group_key") and prepared["visual_group_key"] != group:
        raise VisualPreparationError("raw source conflicts with row visual group")
    return group


def _prepare_source_rendition(store, tenant, prepared, source_url, delivered_url,
                              reader, render_evidence, receipt_writer, asset):
    """Register two exact objects only through owner-created receipt IDs.

    The service-role RPC validates the owner-only rows. This process cannot
    insert those rows; without an owner receipt producer it fails closed.
    """
    source = _exact_bytes(source_url, reader, "source")
    delivered = _exact_bytes(delivered_url, reader, "delivered")
    if asset and not fingerprint.attest_drive(asset.get("content_hash"), source):
        raise VisualPreparationError("Drive asset MD5 does not attest source bytes")
    source_hash, delivered_hash = _md5(source), _md5(delivered)
    supplied_hash = prepared.get("byte_hash")
    if supplied_hash and supplied_hash != "derived:" + delivered_hash:
        raise VisualPreparationError("row byte_hash does not match delivered bytes")
    drive_id = prepared.get("drive_file_id")
    if drive_id and (not asset or str(drive_id) != str(prepared.get("source_media_asset_id"))):
        raise VisualPreparationError("Drive ID has no matching tenant asset")
    r2_key = prepared.get("r2_key")
    if r2_key:
        from . import media_host
        parsed = urlsplit(delivered_url)
        object_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if media_host._key_from_public_url(object_url) != r2_key:
            raise VisualPreparationError("R2 key does not match delivered object")
    evidence = render_evidence.as_dict() if hasattr(render_evidence, "as_dict") else render_evidence
    if not isinstance(evidence, dict) or any(evidence.get(k) != v for k, v in {
        "source_exact_url": source_url, "delivered_exact_url": delivered_url,
        "source_fingerprint": source_hash, "delivered_fingerprint": delivered_hash,
        "source_byte_length": len(source), "delivered_byte_length": len(delivered),
    }.items()) or evidence.get("operation") not in ("render", "reburn", "rehost"):
        raise VisualPreparationError("distinct source URL has no verified rendition lineage")
    if receipt_writer is None:
        from . import visual_owner_receipts
        receipt_writer = visual_owner_receipts.default_writer()
    if not callable(receipt_writer):
        raise VisualPreparationError("owner receipt producer is unavailable for source/rendition")
    group = _known_group(store, tenant, prepared, source_url, required=False)
    if group is None:
        registered = _register_raw_source(store, tenant, prepared, source_url, source, asset)
        group = _known_group(store, tenant, prepared, source_url)
        if group != registered:
            raise VisualPreparationError("raw source registration returned conflicting group")
    if not isinstance(group, str) or not group.startswith("vg_"):
        raise VisualPreparationError("source scene has no unambiguous registered group")
    # The callback must insert owner-only byte-read and render rows using its
    # own privileged boundary. IDs alone are never treated as attestations.
    try:
        receipts = receipt_writer(tenant=tenant, group_key=group, source_bytes=source,
                                  delivered_bytes=delivered, render_evidence=evidence,
                                  asset_id=prepared.get("source_media_asset_id"))
    except Exception as exc:
        raise VisualPreparationError("owner receipt production failed") from exc
    if not isinstance(receipts, dict):
        raise VisualPreparationError("owner receipt producer returned no receipts")
    try:
        ids = {key: str(uuid.UUID(str(receipts[key]))) for key in (
            "source_read_receipt", "delivered_read_receipt", "render_receipt")}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VisualPreparationError("owner receipt producer returned invalid receipts") from exc
    result = _rpc(store, "visual_global_prepare_source_rendition", {
        "p_tenant": tenant, "p_group_key": group,
        "p_source_read_receipt": ids["source_read_receipt"],
        "p_delivered_read_receipt": ids["delivered_read_receipt"],
        "p_render_receipt": ids["render_receipt"], "p_actor": "visual_writer_prepare",
    })
    if not isinstance(result, dict) or result.get("group_key") != group or (
        result.get("source_fingerprint") != source_hash or
        result.get("delivered_fingerprint") != delivered_hash or
        result.get("usage_claimed") is not False
    ):
        raise VisualPreparationError("source/rendition registration returned conflicting identity")
    prepared["visual_group_key"] = group
    prepared["byte_hash"] = "derived:" + delivered_hash
    return prepared


def prepare_same_object(store, account_key, row, *, read_bytes=None, receipt_writer=None,
                        isolated_test_callbacks=False):
    """Attest one exact source/delivered URL through the owner receipt boundary.

    The legacy bundle RPC may create an absent scene, but cannot establish byte
    authority. The owner read receipt and source/rendition RPC do that work.
    """
    if not enabled():
        return row
    prepared = dict(row)
    url = prepared.get("image_url")
    if not _own_media_url(url) or prepared.get("source_media_url", url) != url:
        raise VisualPreparationError("same-object source and delivered URL must match the media host")
    if read_bytes is not None or receipt_writer is not None:
        # Test injection must never be a production route to attest stale bytes
        # or replay a receipt UUID. The sentinel is deliberately not a usable
        # database DSN; real owner connections always use the built-in paths.
        if (not isolated_test_callbacks
                or os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_DSN") != "test-only"
                or os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE") != "receipt_owner"):
            raise VisualPreparationError("same-object callbacks require isolated test configuration")
    from . import visual_owner_receipts
    configured_writer = visual_owner_receipts.default_same_object_writer()
    if configured_writer is None:
        raise VisualPreparationError("owner receipt producer is unavailable for same-object visual")
    if receipt_writer is None:
        receipt_writer = configured_writer
    if not callable(receipt_writer):
        raise VisualPreparationError("owner receipt producer is unavailable for same-object visual")

    tenant = _tenant(store, account_key)
    data = _exact_bytes(url, read_bytes or _bytes_for_url, "same-object")
    digest = _md5(data)
    asset_id = prepared.get("source_media_asset_id")
    asset = _asset(store, tenant, asset_id) if asset_id else None
    if asset and not fingerprint.attest_drive(asset.get("content_hash"), data):
        raise VisualPreparationError("Drive asset MD5 does not attest exact bytes")
    drive_id = prepared.get("drive_file_id")
    if drive_id and (not asset or str(drive_id) != str(asset_id)):
        raise VisualPreparationError("Drive ID has no matching tenant asset")
    r2_key = prepared.get("r2_key")
    if r2_key:
        from . import media_host
        parsed = urlsplit(url)
        object_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if media_host._key_from_public_url(object_url) != r2_key:
            raise VisualPreparationError("R2 key does not match exact object")
    supplied_hash = prepared.get("byte_hash")
    if supplied_hash and supplied_hash != "derived:" + digest:
        raise VisualPreparationError("row byte_hash does not match exact bytes")

    group = _known_group(store, tenant, prepared, url, required=False)
    if group is None:
        registered = _register_raw_source(store, tenant, prepared, url, data, asset)
        group = _known_group(store, tenant, prepared, url)
        if group != registered:
            raise VisualPreparationError("raw source registration returned conflicting group")
    if not isinstance(group, str) or not group.startswith("vg_"):
        raise VisualPreparationError("same-object source has no unambiguous registered group")

    evidence = {"exact_url": url, "fingerprint": digest, "byte_length": len(data),
                "evidence_ref": "visual_writer_prepare:same_object_exact_read",
                "observed_by": "visual_writer_prepare"}
    try:
        receipts = receipt_writer(tenant=tenant, group_key=group, exact_bytes=data,
                                  evidence=evidence, asset_id=str(asset_id) if asset else None)
    except Exception as exc:
        raise VisualPreparationError("owner same-object receipt production failed") from exc
    try:
        receipt_id = str(uuid.UUID(str(receipts["read_receipt"])))
        if receipts.get("render_receipt") is not None:
            raise ValueError("same-object producer returned render lineage")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VisualPreparationError("owner same-object producer returned invalid receipt") from exc
    result = _rpc(store, "visual_global_prepare_source_rendition", {
        "p_tenant": tenant, "p_group_key": group,
        "p_source_read_receipt": receipt_id, "p_delivered_read_receipt": receipt_id,
        "p_render_receipt": None, "p_actor": "visual_writer_prepare",
    })
    if not isinstance(result, dict) or any((
        result.get("group_key") != group,
        result.get("source_fingerprint") != digest,
        result.get("delivered_fingerprint") != digest,
        result.get("usage_claimed") is not False,
    )):
        raise VisualPreparationError("same-object registration returned conflicting identity")
    prepared["visual_group_key"] = group
    prepared["byte_hash"] = "derived:" + digest
    return prepared


def prepare(store, account_key, row, *, read_bytes=None, render_evidence=None,
            receipt_writer=None):
    """Return a prepared row; never trust identity hints or mutate the input.

    ``read_bytes`` is injectable for tests and callers with an exact delivered
    object reader. It must return the bytes currently served at ``image_url``.
    """
    if not enabled():
        return row
    prepared = dict(row)
    tenant = _tenant(store, account_key)
    url = prepared.get("image_url")
    active = prepared.get("variant_status", "active") == "active"
    if not isinstance(url, str) or not url.strip():
        if active:
            raise VisualPreparationError("active visual row has no delivered media")
        return prepared
    reader = read_bytes or _bytes_for_url
    asset_id = prepared.get("source_media_asset_id")
    asset = _asset(store, tenant, asset_id) if asset_id else None
    source_url = prepared.get("source_media_url")
    if source_url and source_url != url:
        return _prepare_source_rendition(store, tenant, prepared, source_url, url,
                                         reader, render_evidence, receipt_writer, asset)
    data = _exact_bytes(url, reader, "delivered")
    if asset and not fingerprint.attest_drive(asset.get("content_hash"), data):
        raise VisualPreparationError("Drive asset MD5 does not attest delivered bytes")
    aliases = []
    if asset:
        aliases.append(("source_asset", str(asset_id)))
    # The draft global claim accepts one canonical MD5 per group. A SHA-only
    # alias cannot be checked against that MD5 by the database at claim time.
    aliases.append(("byte_hash", fingerprint.derived_aliases(data)[1]))
    # Drive's bare content_hash attests this exact selected asset and delivered
    # bytes. A transformed rendition needs a separate proven lineage path.
    if asset:
        aliases.append(("byte_hash", fingerprint.from_drive_md5(asset["content_hash"])))
    aliases.append(("canonical_url", url))
    drive_id = prepared.get("drive_file_id")
    if drive_id:
        if not asset or str(drive_id) != str(asset_id):
            raise VisualPreparationError("Drive ID has no matching tenant asset")
        aliases.append(("drive_id", str(drive_id)))
    r2_key = prepared.get("r2_key")
    if r2_key:
        from . import media_host
        if media_host._key_from_public_url(url) != r2_key:
            raise VisualPreparationError("R2 key does not match delivered object")
        aliases.append(("r2_key", str(r2_key)))
    supplied_hash = prepared.get("byte_hash")
    if supplied_hash and supplied_hash not in [value for kind, value in aliases if kind == "byte_hash"]:
        raise VisualPreparationError("row byte_hash does not match delivered bytes")
    digest = hashlib.md5(data).hexdigest()
    expected_fingerprint = "md5:" + digest
    result = _rpc(store, "visual_global_prepare_bundle", {
        "p_tenant": tenant,
        "p_aliases": [{"alias_kind": kind, "alias_value": value} for kind, value in aliases],
        "p_fingerprint": expected_fingerprint,
        "p_evidence": {"source": "delivered_object_bytes", "verified_bytes": expected_fingerprint,
                       "delivered_url": url},
        "p_actor": "visual_writer_prepare",
        "p_asset_id": str(asset_id) if asset else None,
    })
    if not isinstance(result, dict) or result.get("fingerprint") != expected_fingerprint:
        raise VisualPreparationError("visual bundle registration returned invalid fingerprint")
    group = result.get("group_key")
    if not isinstance(group, str) or not group.startswith("vg_"):
        raise VisualPreparationError("visual bundle registration returned no group")
    supplied_group = prepared.get("visual_group_key")
    if supplied_group and supplied_group != group:
        raise VisualPreparationError("row visual group conflicts with registered identity")
    prepared["visual_group_key"] = group
    prepared["byte_hash"] = "derived:md5:" + digest
    return prepared
