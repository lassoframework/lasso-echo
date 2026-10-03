"""Owner-only producer for immutable source/rendition byte receipts.

This module has no service-role fallback.  It is enabled only with a dedicated
database-owner DSN and expected database role, and is used after the writer has
already read the exact bounded objects.  It writes the two object-read rows and
their render binding in one owner transaction.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid


_TRUE = ("1", "true", "yes", "on")
_GROUP = re.compile(r"vg_[A-Za-z0-9_-]{1,120}\Z")
_OPS = {"render", "reburn", "rehost"}


class OwnerReceiptError(RuntimeError):
    pass


def enabled():
    """The separate owner boundary is off unless explicitly armed."""
    return os.environ.get("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS", "").lower() in _TRUE


def default_writer():
    """Return the production writer only with complete owner configuration."""
    if not enabled():
        return None
    if not (os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_DSN")
            and os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE")
            and os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE") != "service_role"):
        return None
    return produce


def default_same_object_writer():
    """The same-object producer, armed by exactly the same owner boundary."""
    if not enabled():
        return None
    if not (os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_DSN")
            and os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE")
            and os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE") != "service_role"):
        return None
    return produce_same_object


def _md5(data):
    return "md5:" + hashlib.md5(data).hexdigest()


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise OwnerReceiptError(f"{label} is required")
    return value


def _validate(tenant, group_key, source_bytes, delivered_bytes, evidence, asset_id):
    try:
        tenant = str(uuid.UUID(str(tenant)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise OwnerReceiptError("canonical tenant is invalid") from exc
    if not isinstance(group_key, str) or not _GROUP.fullmatch(group_key):
        raise OwnerReceiptError("visual group is invalid")
    if (not isinstance(source_bytes, bytes) or not source_bytes
            or not isinstance(delivered_bytes, bytes) or not delivered_bytes):
        raise OwnerReceiptError("exact byte observations are required")
    from . import visual_writer_prepare as prepare
    if (len(source_bytes) > prepare.MAX_VISUAL_BYTES
            or len(delivered_bytes) > prepare.MAX_VISUAL_BYTES):
        raise OwnerReceiptError("exact byte observations exceed the limit")
    if not isinstance(evidence, dict):
        raise OwnerReceiptError("render evidence is required")
    source_url = _text(evidence.get("source_exact_url"), "source exact URL")
    delivered_url = _text(evidence.get("delivered_exact_url"), "delivered exact URL")
    if source_url == delivered_url:
        raise OwnerReceiptError("a rendition must bind distinct exact URLs")
    if not prepare._own_media_url(source_url) or not prepare._own_media_url(delivered_url):
        raise OwnerReceiptError("receipt URLs must use the configured media host")
    source_hash, delivered_hash = _md5(source_bytes), _md5(delivered_bytes)
    expected = {"source_fingerprint": source_hash,
                "delivered_fingerprint": delivered_hash,
                "source_byte_length": len(source_bytes),
                "delivered_byte_length": len(delivered_bytes)}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise OwnerReceiptError("render evidence differs from observed bytes")
    operation = evidence.get("operation")
    if operation not in _OPS:
        raise OwnerReceiptError("render operation is invalid")
    evidence_ref = _text(evidence.get("evidence_ref"), "render evidence reference")
    observed_by = _text(evidence.get("observed_by"), "byte observer")
    rendered_by = _text(evidence.get("rendered_by"), "renderer")
    if asset_id is not None and (not isinstance(asset_id, str) or not asset_id.strip()):
        raise OwnerReceiptError("source asset is invalid")
    return (tenant, group_key, source_url, delivered_url, source_hash, delivered_hash,
            operation, evidence_ref, observed_by, rendered_by, asset_id)


def _connect():
    dsn = os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_DSN")
    expected_role = os.environ.get("AGENT_VISUAL_RECEIPT_OWNER_ROLE")
    if not enabled() or not dsn or not expected_role or expected_role == "service_role":
        raise OwnerReceiptError("owner receipt boundary is not configured")
    try:
        import psycopg
        connection = psycopg.connect(dsn)
    except Exception as exc:  # noqa: BLE001 - credentials and host stay private
        raise OwnerReceiptError("owner receipt database is unavailable") from exc
    with connection.cursor() as cursor:
        cursor.execute("select current_user")
        current = cursor.fetchone()
    if not current or current[0] != expected_role:
        connection.close()
        raise OwnerReceiptError("owner receipt database role is not authorized")
    return connection


def produce(*, tenant, group_key, source_bytes, delivered_bytes, render_evidence,
            asset_id=None, connection_factory=None):
    """Insert immutable source/delivered/render receipts and return their UUIDs.

    ``connection_factory`` exists only for isolated tests or a separately
    provisioned owner runtime. It receives no caller-provided SQL or credentials.
    """
    (tenant, group_key, source_url, delivered_url, source_hash, delivered_hash,
     operation, evidence_ref, observed_by, rendered_by, asset_id) = _validate(
         tenant, group_key, source_bytes, delivered_bytes, render_evidence, asset_id)
    connection = connection_factory() if connection_factory else _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into public.visual_global_object_read_receipt "
                "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,asset_id,"
                "evidence_ref,observed_by) values (%s,%s,%s,%s,%s,%s,%s,%s) returning receipt_id",
                (tenant, source_url, source_hash, len(source_bytes),
                 "drive_asset" if asset_id else "verified_object_read", asset_id,
                 evidence_ref, observed_by))
            source_receipt = cursor.fetchone()[0]
            cursor.execute(
                "insert into public.visual_global_object_read_receipt "
                "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,asset_id,"
                "evidence_ref,observed_by) values (%s,%s,%s,%s,'verified_object_read',null,%s,%s) "
                "returning receipt_id",
                (tenant, delivered_url, delivered_hash, len(delivered_bytes),
                 evidence_ref, observed_by))
            delivered_receipt = cursor.fetchone()[0]
            cursor.execute(
                "insert into public.visual_global_render_receipt "
                "(tenant_id,source_read_receipt,delivered_read_receipt,source_exact_url,"
                "delivered_exact_url,source_fingerprint,delivered_fingerprint,operation,"
                "evidence_ref,rendered_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "returning receipt_id",
                (tenant, source_receipt, delivered_receipt, source_url, delivered_url,
                 source_hash, delivered_hash, operation, evidence_ref, rendered_by))
            render_receipt = cursor.fetchone()[0]
        connection.commit()
    except Exception as exc:  # noqa: BLE001 - rollback immutable triple as a unit
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise OwnerReceiptError("owner receipt transaction failed") from exc
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    return {"source_read_receipt": str(source_receipt),
            "delivered_read_receipt": str(delivered_receipt),
            "render_receipt": str(render_receipt)}


def _validate_same_object(tenant, group_key, exact_bytes, evidence, asset_id):
    """One exact object serving as BOTH source and delivered evidence.

    The draft SQL (visual_global_prepare_source_rendition) accepts the same exact
    URL for both roles with a NULL render receipt -- there is no render operation
    for an object that was never transformed, and this producer must never invent
    one. Evidence carrying a render operation is rejected outright."""
    try:
        tenant = str(uuid.UUID(str(tenant)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise OwnerReceiptError("canonical tenant is invalid") from exc
    if not isinstance(group_key, str) or not _GROUP.fullmatch(group_key):
        raise OwnerReceiptError("visual group is invalid")
    if not isinstance(exact_bytes, bytes) or not exact_bytes:
        raise OwnerReceiptError("an exact byte observation is required")
    from . import visual_writer_prepare as prepare
    if len(exact_bytes) > prepare.MAX_VISUAL_BYTES:
        raise OwnerReceiptError("exact byte observation exceeds the limit")
    if not isinstance(evidence, dict):
        raise OwnerReceiptError("same-object evidence is required")
    if any(evidence.get(key) is not None for key in
           ("operation", "rendered_by", "render_receipt")):
        raise OwnerReceiptError("a same-object receipt never carries a render operation")
    exact_url = _text(evidence.get("exact_url"), "exact URL")
    if not prepare._own_media_url(exact_url):
        raise OwnerReceiptError("receipt URL must use the configured media host")
    fingerprint = _md5(exact_bytes)
    if (evidence.get("fingerprint") != fingerprint
            or evidence.get("byte_length") != len(exact_bytes)):
        raise OwnerReceiptError("same-object evidence differs from observed bytes")
    evidence_ref = _text(evidence.get("evidence_ref"), "byte evidence reference")
    observed_by = _text(evidence.get("observed_by"), "byte observer")
    if asset_id is not None and (not isinstance(asset_id, str) or not asset_id.strip()):
        raise OwnerReceiptError("source asset is invalid")
    return tenant, group_key, exact_url, fingerprint, evidence_ref, observed_by, asset_id


def produce_same_object(*, tenant, group_key, exact_bytes, evidence, asset_id=None,
                        connection_factory=None):
    """Insert ONE immutable object-read receipt for bytes that are simultaneously
    the source and the delivered object (e.g. a generated artifact published at
    the exact URL it was rendered to). The returned receipt UUID is evidence for
    BOTH roles; there is deliberately NO render receipt -- no lineage is
    fabricated for an object that was never transformed.

    Same owner-only boundary as produce(): no service-role fallback, and the
    owner DSN/role configuration must be complete. ``connection_factory`` exists
    only for isolated tests or a separately provisioned owner runtime."""
    (tenant, group_key, exact_url, fingerprint, evidence_ref, observed_by,
     asset_id) = _validate_same_object(
         tenant, group_key, exact_bytes, evidence, asset_id)
    connection = connection_factory() if connection_factory else _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into public.visual_global_object_read_receipt "
                "(tenant_id,exact_url,fingerprint,byte_length,acquisition_method,asset_id,"
                "evidence_ref,observed_by) values (%s,%s,%s,%s,%s,%s,%s,%s) returning receipt_id",
                (tenant, exact_url, fingerprint, len(exact_bytes),
                 "drive_asset" if asset_id else "verified_object_read", asset_id,
                 evidence_ref, observed_by))
            read_receipt = cursor.fetchone()[0]
        connection.commit()
    except Exception as exc:  # noqa: BLE001 - rollback the immutable row as a unit
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise OwnerReceiptError("owner receipt transaction failed") from exc
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    return {"read_receipt": str(read_receipt), "render_receipt": None}
