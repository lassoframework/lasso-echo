"""Draft, opt-in preparation for exact visual-group calendar writes.

This is a writer-side belt, not an activation or a substitute for the database
trigger. An RPC failure or an unverified delivered object aborts the write.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from urllib.parse import unquote, urlsplit, urlunsplit

from . import visual_fingerprint as fingerprint

MAX_VISUAL_BYTES = 128 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class VisualPreparationError(ValueError):
    pass


def enabled() -> bool:
    return os.environ.get("AGENT_VISUAL_GLOBAL_WRITER_PREP", "").lower() in ("1", "true", "yes", "on")


def _distinct_poster_url(row, poster_render_evidence):
    """Return the distinct poster object URL requiring a second render edge.

    A blank poster adds no object; a byte-for-byte identical URL selects the
    already verified delivered object. Any other value is a third scene object
    and must carry explicit poster render evidence: without it the row fails
    closed before any lookup or RPC rather than being cleared or assigned
    invented lineage.
    """
    thumbnail = row.get("thumbnail_url")
    if thumbnail is None or (isinstance(thumbnail, str) and not thumbnail.strip()):
        return None
    if isinstance(thumbnail, str) and thumbnail == row.get("image_url"):
        return None
    if not isinstance(thumbnail, str) or poster_render_evidence is None:
        raise VisualPreparationError(
            "distinct thumbnail object has no verified poster scene lineage")
    return thumbnail


def _prepare_poster_edge(store, tenant, group, image_url, image_bytes, image_hash,
                         poster_url, poster_render_evidence, reader, receipt_writer,
                         scene_armed=False):
    """Attest the poster as a second owner render edge image -> thumbnail.

    The selected image is this edge's source and the poster its delivered
    object, reusing the existing owner receipt boundary and source/rendition
    RPC in the same linked scene. The row's byte identity stays about the
    selected image only; this adds the poster object and its lineage so the
    database verifier can prove the full 3-object scene.
    """
    poster = _exact_bytes(poster_url, reader, "poster")
    poster_hash = _md5(poster)
    evidence = (poster_render_evidence.as_dict()
                if hasattr(poster_render_evidence, "as_dict") else poster_render_evidence)
    if not isinstance(evidence, dict) or any(evidence.get(k) != v for k, v in {
        "source_exact_url": image_url, "delivered_exact_url": poster_url,
        "source_fingerprint": image_hash, "delivered_fingerprint": poster_hash,
        "source_byte_length": len(image_bytes), "delivered_byte_length": len(poster),
    }.items()) or evidence.get("operation") not in ("render", "reburn", "rehost"):
        raise VisualPreparationError(
            "distinct thumbnail object has no verified poster scene lineage")
    if receipt_writer is None:
        from . import visual_owner_receipts
        receipt_writer = visual_owner_receipts.default_writer()
    if not callable(receipt_writer):
        raise VisualPreparationError("owner receipt producer is unavailable for poster rendition")
    try:
        receipts = receipt_writer(tenant=tenant, group_key=group,
                                  source_bytes=image_bytes, delivered_bytes=poster,
                                  render_evidence=evidence, asset_id=None)
    except Exception as exc:
        raise VisualPreparationError("owner poster receipt production failed") from exc
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
        result.get("source_fingerprint") != image_hash or
        result.get("delivered_fingerprint") != poster_hash or
        result.get("usage_claimed") is not False
    ):
        raise VisualPreparationError("poster registration returned conflicting identity")
    return {"role": "poster", "exact_url": poster_url,
            "fingerprint": poster_hash, "byte_length": len(poster),
            "scene_fingerprint": _scene_fingerprint(poster) if scene_armed else None,
            "exact_bytes": poster}


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
            # Our host percent-encodes ordinary filename spaces. Raw URL
            # whitespace is rejected above; decoded separators and controls
            # remain ineligible even when percent-encoded.
            and not any((char.isspace() and char != " ") or ord(char) < 32
                        or 127 <= ord(char) <= 159 for char in part)
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


def _scene_fingerprint(data):
    """Additive, advisory pHash scene evidence ('scene:phash64:<16 hex>') for
    bytes whose exact md5 identity is already verified, or None.

    This decoder never gates or raises: undecodable bytes record null
    evidence alongside exact md5 identity. The separately armed DRAFT durable
    adapter requires decodable evidence for the actual displayed object; its
    candidate registration never consumes a scene or records use."""
    try:
        from . import visual_scene
        return visual_scene.scene_fingerprint(data)
    except Exception:  # noqa: BLE001 - no scene evidence is not byte evidence
        return None


def _scene_evidence_armed():
    """Read both OFF-default scene switches before doing any pHash work."""
    try:
        from . import config
        return (config.visual_scene_candidate_flag() is not False or
                config.visual_scene_guard_flag() is not False)
    except Exception:  # noqa: BLE001 - config read failure means no new metadata
        return False


def _candidate_emission_armed():
    """OFF-default tri-state read of AGENT_VISUAL_SCENE_CANDIDATE.

    Unset/off -> False (no candidate payload, byte-for-byte prior behavior).
    Explicitly on OR ambiguous -> True: an ambiguous flag value counts as
    armed fail-closed, never a silent default. Emission is advisory staging
    evidence only — it never gates, never raises and never counts as use, and
    any failure to read the flag itself fails closed to no emission."""
    try:
        from . import config
        return config.visual_scene_candidate_flag() is not False
    except Exception:  # noqa: BLE001 - config read failure fails closed
        return False


def _scene_candidate(tenant, group, objects, *, force=False):
    """Owner-attested CANDIDATE pHash evidence for the visual_scene_candidate
    staging contract (docs/VISUAL_SCENE_GUARD_DRAFT.md redesign item (a)).

    ``objects`` is an iterable of ``(role, exact_url, md5_fingerprint,
    byte_length, scene_fingerprint_or_None)`` tuples — every one computed from
    the EXACT verified object bytes this same call already attests (the same
    bytes, same call that establishes md5 byte authority). Returns None when
    emission is off.

    The payload is CANDIDATE/STAGING evidence only: usage_claimed is False and
    counts_as_use is False, so it never consumes a scene and never excludes
    another candidate. It never gates and never raises. An object whose bytes
    did not decode carries phash=None and stageable=False: the phash-NOT-NULL
    staging table can never take it, and null pHash evidence is never treated
    as DISTINCT (unknown fails closed)."""
    if not force and not _candidate_emission_armed():
        return None
    entries = []
    for role, url, md5_fp, length, scene_fp in objects:
        bare = None
        if isinstance(scene_fp, str) and scene_fp.startswith("scene:phash64:"):
            digest = scene_fp.rsplit(":", 1)[-1]
            bare = digest if re.fullmatch(r"[0-9a-f]{16}", digest) else None
        entries.append({"role": role, "phash": bare,
                        "scene_fingerprint": scene_fp, "exact_url": url,
                        "fingerprint": md5_fp, "byte_length": length,
                        "stageable": bare is not None})
    return {"kind": "visual_scene_candidate", "stage": "candidate",
            "tenant_id": tenant, "group_key": group,
            "usage_claimed": False, "counts_as_use": False,
            "excludes_candidates": False,
            "observed_by": "visual_writer_prepare",
            "evidence_ref": "visual_writer_prepare:candidate_scene_evidence",
            "objects": entries}


def _scene_guard_armed():
    """The DRAFT guard is OFF by default; unknown flag values fail closed."""
    from . import config
    try:
        state = config.visual_scene_guard_flag()
    except Exception as exc:
        raise VisualPreparationError("scene guard flag could not be verified") from exc
    if state is None:
        raise VisualPreparationError("scene guard flag state is ambiguous")
    return state is True


def _prepared_scene_candidate(store, tenant, group, prepared, objects, exact_objects):
    """Stage only the actual displayed object after owner byte preparation.

    Advisory candidate emission remains independently optional. When the scene
    guard is explicitly armed, registration is mandatory and the SQL RPC must
    verify the owner attestation for this exact tenant/group/URL/MD5. It owns
    atomic retry identity; this adapter sends deterministic evidence so an
    identical retry returns the same candidate UUID. No registration is use.
    """
    armed = _scene_guard_armed()
    candidate = _scene_candidate(tenant, group, objects, force=armed)
    if not armed:
        return candidate
    poster_url = prepared.get("thumbnail_url")
    has_poster = isinstance(poster_url, str) and bool(poster_url.strip()) and (
        poster_url != prepared.get("image_url"))
    role = "poster" if has_poster else "display"
    url = poster_url if has_poster else prepared.get("image_url")
    allowed_roles = ("poster",) if has_poster else ("delivered", "same_object")
    displayed = [item for item in candidate["objects"]
                 if item["role"] in allowed_roles and item["exact_url"] == url]
    if len(displayed) != 1:
        raise VisualPreparationError("scene candidate has no verified displayed object")
    entry = displayed[0]
    if not entry["stageable"]:
        raise VisualPreparationError("displayed scene candidate has no decodable pHash")
    if not re.fullmatch(r"md5:[0-9a-f]{32}", str(entry["fingerprint"])):
        raise VisualPreparationError("displayed scene candidate has no verified MD5")
    from . import visual_owner_receipts
    scene_writer = visual_owner_receipts.default_scene_writer()
    if not callable(scene_writer):
        raise VisualPreparationError("owner scene receipt producer is unavailable")
    try:
        receipt = scene_writer(tenant=tenant, group_key=group, object_role=role,
                               exact_url=url, exact_bytes=exact_objects[url])
        receipt_id = str(uuid.UUID(str(receipt["receipt_id"])))
        if receipt["phash"] != entry["phash"] or receipt["fingerprint"] != entry["fingerprint"]:
            raise ValueError("owner scene receipt differs from displayed bytes")
    except Exception as exc:
        raise VisualPreparationError("owner scene receipt production failed") from exc
    # Owner receipt IDs are deterministic for the exact byte binding. Keep
    # timestamps and caller-supplied candidate identity out. Raw source objects
    # never acquire the SQL display role merely because their pHash decodes.
    arguments = {
        "p_tenant": tenant, "p_group_key": group,
        "p_phash": entry["phash"], "p_exact_url": url,
        "p_fingerprint": entry["fingerprint"],
        "p_evidence": {"source": "owner_prepared_exact_object_bytes",
                       "verified_bytes": entry["fingerprint"],
                       "byte_length": entry["byte_length"],
                       "owner_phash_receipt": receipt_id},
        "p_actor": "visual_writer_prepare", "p_object_role": role,
    }
    try:
        result = _rpc(store, "visual_scene_register_candidate", arguments)
    except VisualPreparationError:
        raise
    except Exception as exc:
        raise VisualPreparationError("scene candidate registration failed") from exc
    try:
        candidate_id = str(uuid.UUID(result))
    except (TypeError, ValueError, AttributeError) as exc:
        raise VisualPreparationError("scene candidate registration returned invalid UUID") from exc
    entry["candidate_id"] = candidate_id
    entry["object_role"] = role
    candidate["candidate_id"] = candidate_id
    candidate["object_role"] = role
    return candidate


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


def _register_raw_source(store, tenant, prepared, source_url, source, asset,
                         scene_fp=None, scene_armed=False):
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
    evidence = {"source": "raw_object_bytes", "verified_bytes": source_hash,
                "delivered_url": source_url}
    if scene_armed:
        evidence["scene_fingerprint"] = scene_fp
    result = _rpc(store, "visual_global_prepare_bundle", {
        "p_tenant": tenant,
        "p_aliases": [{"alias_kind": kind, "alias_value": value} for kind, value in aliases],
        "p_fingerprint": source_hash,
        "p_evidence": evidence,
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
                              reader, render_evidence, receipt_writer, asset,
                              poster_render_evidence=None):
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
    scene_armed = _scene_evidence_armed()
    source_scene = _scene_fingerprint(source) if scene_armed else None
    delivered_scene = _scene_fingerprint(delivered) if scene_armed else None
    if group is None:
        registered = _register_raw_source(store, tenant, prepared, source_url, source, asset,
                                          scene_fp=source_scene, scene_armed=scene_armed)
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
    poster_url = _distinct_poster_url(prepared, poster_render_evidence)
    poster = None
    if poster_url is not None:
        # Second owner-attested render edge: selected image -> poster object.
        poster = _prepare_poster_edge(store, tenant, group, delivered_url, delivered,
                                      delivered_hash, poster_url, poster_render_evidence,
                                      reader, receipt_writer, scene_armed=scene_armed)
    objects = [("source", source_url, source_hash, len(source), source_scene),
               ("delivered", delivered_url, delivered_hash, len(delivered), delivered_scene)]
    if poster is not None:
        objects.append((poster["role"], poster["exact_url"], poster["fingerprint"],
                        poster["byte_length"], poster["scene_fingerprint"]))
    candidate = _prepared_scene_candidate(store, tenant, group, prepared, objects,
        {poster["exact_url"]: poster["exact_bytes"]} if poster is not None else {delivered_url: delivered})
    if candidate is not None:
        prepared["scene_candidate"] = candidate
    prepared["visual_group_key"] = group
    prepared["byte_hash"] = "derived:" + delivered_hash
    return prepared


def _prepare_same_object_row(store, account_key, row, *, read_bytes=None, receipt_writer=None,
                             isolated_test_callbacks=False, poster_render_evidence=None):
    """Attest one exact source/delivered URL through the owner receipt boundary.

    The legacy bundle RPC may create an absent scene, but cannot establish byte
    authority. The owner read receipt and source/rendition RPC do that work.
    """
    guard_armed = _scene_guard_armed()
    if not enabled():
        if guard_armed:
            raise VisualPreparationError("scene guard requires owner byte preparation")
        return row
    prepared = dict(row)
    _distinct_poster_url(prepared, poster_render_evidence)
    url = prepared.get("image_url")
    source_url = prepared.get("source_media_url")
    if (not _own_media_url(url) or not isinstance(source_url, str) or
            not source_url.strip() or source_url != url):
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

    scene_armed = _scene_evidence_armed()
    scene_fp = _scene_fingerprint(data) if scene_armed else None
    group = _known_group(store, tenant, prepared, url, required=False)
    if group is None:
        registered = _register_raw_source(store, tenant, prepared, url, data, asset,
                                          scene_fp=scene_fp, scene_armed=scene_armed)
        group = _known_group(store, tenant, prepared, url)
        if group != registered:
            raise VisualPreparationError("raw source registration returned conflicting group")
    if not isinstance(group, str) or not group.startswith("vg_"):
        raise VisualPreparationError("same-object source has no unambiguous registered group")

    evidence = {"exact_url": url, "fingerprint": digest, "byte_length": len(data),
                "evidence_ref": "visual_writer_prepare:same_object_exact_read",
                "observed_by": "visual_writer_prepare"}
    if scene_armed:
        evidence["scene_fingerprint"] = scene_fp
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
    poster_url = _distinct_poster_url(prepared, poster_render_evidence)
    poster = None
    if poster_url is not None:
        # The same-object writer is one-read form; the poster edge reuses the
        # configured source/rendition owner writer for image -> thumbnail.
        poster = _prepare_poster_edge(store, tenant, group, url, data, digest,
                                      poster_url, poster_render_evidence,
                                      read_bytes or _bytes_for_url, None,
                                      scene_armed=scene_armed)
    objects = [("same_object", url, digest, len(data), scene_fp)]
    if poster is not None:
        objects.append((poster["role"], poster["exact_url"], poster["fingerprint"],
                        poster["byte_length"], poster["scene_fingerprint"]))
    candidate = _prepared_scene_candidate(store, tenant, group, prepared, objects,
        {poster["exact_url"]: poster["exact_bytes"]} if poster is not None else {url: data})
    if candidate is not None:
        prepared["scene_candidate"] = candidate
    prepared["visual_group_key"] = group
    prepared["byte_hash"] = "derived:" + digest
    return prepared


def prepare_same_object(store, account_key, row_or_url, *, flag_on=None,
                        read_bytes=None, receipt_writer=None,
                        isolated_test_callbacks=False, poster_render_evidence=None):
    """Prepare one exact object through the owner receipt boundary.

    Calendar rows retain their row shape; generated-artifact URL callers receive
    a compact identity for staging. Both forms share byte, tenant, group, and
    owner receipt checks. Injected callbacks are isolated-test-only.
    """
    actual = enabled()
    if not actual and _scene_guard_armed():
        raise VisualPreparationError("scene guard requires owner byte preparation")
    if flag_on is not None and bool(flag_on) is not actual:
        raise VisualPreparationError("writer preparation flag state is ambiguous")
    if isinstance(row_or_url, dict):
        return _prepare_same_object_row(
            store, account_key, row_or_url, read_bytes=read_bytes,
            receipt_writer=receipt_writer,
            isolated_test_callbacks=isolated_test_callbacks,
            poster_render_evidence=poster_render_evidence)
    if not actual:
        raise VisualPreparationError("writer preparation flag is off")
    if not isinstance(row_or_url, str):
        raise VisualPreparationError("same-object URL is invalid")
    url = row_or_url
    prepared = _prepare_same_object_row(
        store, account_key, {"image_url": url}, read_bytes=read_bytes,
        receipt_writer=receipt_writer,
        isolated_test_callbacks=isolated_test_callbacks)
    result = {"visual_group_key": prepared["visual_group_key"],
              "byte_hash": prepared["byte_hash"], "usage_claimed": False,
              "url": url}
    if prepared.get("scene_candidate") is not None:
        result["scene_candidate"] = prepared["scene_candidate"]
    return result


def prepare(store, account_key, row, *, read_bytes=None, render_evidence=None,
            receipt_writer=None, isolated_test_callbacks=False,
            poster_render_evidence=None):
    """Return a prepared row; never trust identity hints or mutate the input.

    ``read_bytes`` is injectable for tests and callers with an exact delivered
    object reader. It must return the bytes currently served at ``image_url``.
    Injected callbacks are isolated-test-only: both the same-object path and
    the distinct source/rendition path bind owner-created receipts, so a
    production caller must never supply the byte reader or receipt writer.

    A raw same-object row requires an explicit nonblank ``source_media_url``
    equal to ``image_url`` and is prepared through the one-read owner receipt
    path (``prepare_same_object``). Distinct source/delivered objects require
    verified render evidence. A guarded row never infers its source identity
    from a delivered rendition.

    A distinct ``thumbnail_url`` poster is a third scene object: it prepares
    as a second owner-attested render edge from the selected image bytes to
    the poster bytes and requires ``poster_render_evidence``. Without that
    evidence the row fails closed before any lookup, RPC or calendar write.
    Blank posters and posters exactly equal to ``image_url`` need no edge.
    """
    guard_armed = _scene_guard_armed()
    if not enabled():
        if guard_armed:
            raise VisualPreparationError("scene guard requires owner byte preparation")
        return row
    prepared = dict(row)
    _distinct_poster_url(prepared, poster_render_evidence)
    tenant = _tenant(store, account_key)
    url = prepared.get("image_url")
    active = prepared.get("variant_status", "active") == "active"
    if not isinstance(url, str) or not url.strip():
        if active:
            raise VisualPreparationError("active visual row has no delivered media")
        return prepared
    source_url = prepared.get("source_media_url")
    if not isinstance(source_url, str) or not source_url.strip():
        raise VisualPreparationError("active visual row has no explicit source media URL")
    reader = read_bytes or _bytes_for_url
    asset_id = prepared.get("source_media_asset_id")
    asset = _asset(store, tenant, asset_id) if asset_id else None
    if source_url != url:
        if read_bytes is not None or receipt_writer is not None:
            # Test injection must never be a production route to attest stale
            # source/delivered bytes or replay receipt UUIDs for a transformed
            # write. The sentinel is deliberately not a usable database DSN;
            # real owner connections always use the built-in paths.
            if (not isolated_test_callbacks
                    or os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_DSN") != "test-only"
                    or os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE") != "receipt_owner"):
                raise VisualPreparationError(
                    "source/rendition callbacks require isolated test configuration")
        return _prepare_source_rendition(store, tenant, prepared, source_url, url,
                                         reader, render_evidence, receipt_writer, asset,
                                         poster_render_evidence)
    # Raw same-object row: the delivered object IS the source object. Route
    # through the owner one-read receipt boundary so the guarded row carries
    # owner source+delivered scene object members; never the legacy bundle
    # RPC, which cannot establish byte authority for the global claim.
    return _prepare_same_object_row(
        store, account_key, prepared, read_bytes=read_bytes,
        receipt_writer=receipt_writer,
        isolated_test_callbacks=isolated_test_callbacks,
        poster_render_evidence=poster_render_evidence)
