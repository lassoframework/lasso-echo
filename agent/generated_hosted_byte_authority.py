"""UNWIRED trusted generated hosted-byte issuer and immutable receipt lookup.

Only the dedicated issuer process receives the issuer DB credential and trusted
tenant URL scopes. No producer-supplied receipt or local image bytes are input.
No environment/DSN discovery, flag, background job or consumer wiring is added.
Receipts prove a past hosted-byte observation, not approval or future delivery.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import uuid
from urllib.parse import urlsplit


ISSUER_ROLE = "generated_hosted_byte_issuer_20261009"
READER_ROLE = "generated_hosted_byte_reader_20261009"
RECEIPT_KEYS = frozenset({"receipt_id", "gym_id", "artifact_version_id",
                          "hosted_url", "delivered_sha256", "render_manifest"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                   r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


class HostedByteHold(RuntimeError):
    """Static diagnostic only; never expose URLs, bytes, DSNs or DB exceptions."""


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise HostedByteHold("generated_version_uuid_invalid") from None


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise HostedByteHold("generated_digest_invalid")
    return value


def _tenant(value):
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 128:
        raise HostedByteHold("generated_tenant_invalid")
    return value


def _url(value):
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError()
        p = urlsplit(value)
        if (not value.startswith("https://") or p.scheme != "https"
                or not p.hostname or p.username or p.password
                # One spelling per HTTPS origin and exact hosted object.
                # urlsplit lowercases hostname, so equality rejects uppercase,
                # explicit :443, trailing-dot and alternative host spellings.
                or not _HOST.fullmatch(p.hostname) or p.netloc != p.hostname
                or p.port is not None or p.query or p.fragment
                or "\\" in value or not p.path.startswith("/")
                or any(part in (".", "..") for part in p.path.split("/"))
                or "%" in p.path or "//" in p.path):
            raise ValueError()
        return p
    except (ValueError, TypeError):
        raise HostedByteHold("generated_hosted_url_invalid") from None


def _origin(parsed):
    """Canonical HTTPS origin, independent of raw URL netloc spelling."""
    return ("https", parsed.hostname, 443)


def _manifest(data, tenant, version, url, sha):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        if type(data) is not bytes or not 0 < len(data) <= MAX_MANIFEST_BYTES:
            raise ValueError()
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in {
                "gym_id": tenant, "artifact_version_id": version,
                "hosted_url": url, "delivered_sha256": sha}.items()):
            raise ValueError()
        # SQL JSONB rejects values outside finite JSON/Postgres representation.
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HostedByteHold("generated_manifest_binding_invalid") from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to validated IP once, preserving normal hostname TLS validation."""
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.sock.close()
            raise


class HostedObjectReader:
    """Dedicated reader with operator-configured exact HTTPS tenant path scopes.

    Prefixes must end with '/'; use a distinct public immutable object directory
    per tenant. All resolved IPs must be global; the connection pins one of those
    IPs and verifies TLS against the original hostname. Redirects, credentials,
    signed URLs, content encodings and non-image responses fail closed.
    """
    def __init__(self, tenant_url_prefixes, *, timeout=15):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise HostedByteHold("generated_reader_bounds_invalid")
        self.timeout = timeout
        try:
            scopes = {}
            for tenant, prefixes in tenant_url_prefixes.items():
                _tenant(tenant)
                if not isinstance(prefixes, (list, tuple)) or not prefixes:
                    raise ValueError()
                entries = []
                for prefix in prefixes:
                    p = _url(prefix)
                    if not p.path.endswith("/") or p.path == "/":
                        raise ValueError()
                    entries.append((_origin(p), p.path))
                scopes[tenant] = tuple(entries)
            if not scopes:
                raise ValueError()
            # An overlapping namespace lets one tenant read another's objects.
            for a, left in scopes.items():
                for b, right in scopes.items():
                    if a != b and any(x[0] == y[0] and (x[1].startswith(y[1]) or y[1].startswith(x[1]))
                                      for x in left for y in right):
                        raise ValueError()
            self._scopes = scopes
        except (AttributeError, TypeError, ValueError):
            raise HostedByteHold("generated_trusted_url_scopes_required") from None

    def read(self, tenant, exact_url):
        p = _url(exact_url)
        if not any(_origin(p) == origin and p.path.startswith(path)
                   for origin, path in self._scopes.get(tenant, ())):
            raise HostedByteHold("generated_hosted_url_outside_tenant_scope")
        conn = None
        try:
            addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(
                p.hostname, 443, type=socket.SOCK_STREAM)))
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise HostedByteHold("generated_hosted_address_not_public")
            conn = _PinnedHTTPSConnection(p.hostname, addresses[0], self.timeout)
            conn.request("GET", p.path, headers={"Accept-Encoding": "identity"})
            response = conn.getresponse()
            if (response.status != 200
                    or response.getheader("Content-Encoding", "identity").lower() != "identity"
                    or response.getheader("Content-Type", "").split(";", 1)[0].lower()
                    not in ("image/png", "image/jpeg", "image/webp")):
                raise HostedByteHold("generated_hosted_read_unavailable")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_BYTES):
                raise HostedByteHold("generated_hosted_size_invalid")
            data = response.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES or (length is not None and len(data) != int(length)):
                raise HostedByteHold("generated_hosted_size_invalid")
            return data
        except HostedByteHold:
            raise
        except Exception:
            raise HostedByteHold("generated_hosted_read_unavailable") from None
        finally:
            if conn is not None:
                conn.close()


class GeneratedHostedByteAuthority:
    """Trusted process adapter using a new dedicated DB connection per operation.

    Factory, tenant and URL scopes are provisioned by the integration owner.
    Dependency injection is an operator/test boundary, never a producer tool.
    Lookup does not trust a supplied receipt JSON; it reads committed authority.
    """
    def __init__(self, connection_factory, *, tenant_id, reader=None):
        if not callable(connection_factory):
            raise HostedByteHold("generated_dedicated_connection_factory_required")
        self._factory = connection_factory
        self.tenant_id = _tenant(tenant_id)
        if reader is not None and not isinstance(reader, HostedObjectReader):
            raise HostedByteHold("generated_trusted_reader_required")
        self._reader = reader

    @staticmethod
    def _close(conn, *, rollback=False):
        clean = True
        try:
            if rollback:
                conn.rollback()
        except Exception:
            clean = False
        try:
            conn.close()
        except Exception:
            clean = False
        return clean

    def _open(self, purpose):
        conn = None
        try:
            conn = self._factory()
            if conn.autocommit is not False or int(conn.info.transaction_status) != 0:
                raise HostedByteHold("generated_dedicated_idle_connection_required")
            authorized = conn.execute(
                "select public.generated_hosted_byte_authorized_20261009(%s,%s)",
                (self.tenant_id, purpose)).fetchone()[0]
            if authorized is not True:
                raise HostedByteHold("generated_tenant_not_authorized")
            return conn
        except Exception:
            if conn is not None:
                self._close(conn)
            raise HostedByteHold("generated_authority_identity_unavailable") from None

    def _check_receipt(self, value, version, url, sha, manifest, receipt_id=None):
        if (not isinstance(value, dict) or set(value) != RECEIPT_KEYS
                or value.get("gym_id") != self.tenant_id
                or value.get("artifact_version_id") != version
                or value.get("hosted_url") != url or value.get("delivered_sha256") != sha
                or value.get("render_manifest") != manifest
                or (receipt_id is not None and value.get("receipt_id") != receipt_id)):
            raise HostedByteHold("generated_authority_receipt_binding_invalid")
        _uuid(value.get("receipt_id"))
        return value

    def issue(self, *, artifact_version_id, hosted_url, expected_sha256, manifest_bytes):
        version, sha = _uuid(artifact_version_id), _sha(expected_sha256)
        _url(hosted_url)
        manifest = _manifest(manifest_bytes, self.tenant_id, version, hosted_url, sha)
        if self._reader is None:
            raise HostedByteHold("generated_trusted_reader_required")
        # Authorize before remote IO, close the read transaction, then freshly
        # authorize again at write time. No DB locks are held during the GET.
        preflight = self._open("issue")
        if not self._close(preflight, rollback=True):
            raise HostedByteHold("generated_authority_cleanup_uncertain")
        data = self._reader.read(self.tenant_id, hosted_url)
        if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES or _digest(data) != sha:
            raise HostedByteHold("generated_hosted_bytes_mismatch")
        conn = self._open("issue")
        try:
            value = conn.execute(
                "select public.generated_hosted_byte_issue_20261009(%s,%s,%s,%s,%s,%s,%s)",
                (self.tenant_id, version, hosted_url, sha, _digest(manifest_bytes), data, manifest_bytes)
            ).fetchone()[0]
            receipt = self._check_receipt(value, version, hosted_url, sha, manifest)
        except Exception:
            if not self._close(conn, rollback=True):
                raise HostedByteHold("generated_authority_cleanup_uncertain") from None
            raise HostedByteHold("generated_authority_issue_rejected") from None
        try:
            conn.commit()
        except Exception:
            # COMMIT may have reached the server. Reconcile the exact binding
            # through a fresh issuer connection; never retry or invent a UUID.
            raise HostedByteHold("generated_authority_commit_uncertain") from None
        finally:
            if not self._close(conn):
                raise HostedByteHold("generated_authority_cleanup_uncertain") from None
        return receipt

    def reconcile(self, *, artifact_version_id, hosted_url, expected_sha256,
                  manifest_bytes):
        """Issuer-only readback through a fresh connection after a lost ACK.

        Same exact identity bindings as lookup, minus the unknown receipt UUID.
        Never issues, mutates, retries or enumerates; an absent, changed or
        cross-tenant binding is a static hold with no database detail.
        """
        version, sha = _uuid(artifact_version_id), _sha(expected_sha256)
        _url(hosted_url)
        manifest = _manifest(manifest_bytes, self.tenant_id, version, hosted_url, sha)
        conn = self._open("reconcile")
        try:
            value = conn.execute(
                "select public.generated_hosted_byte_reconcile_20261009(%s,%s,%s,%s,%s)",
                (self.tenant_id, version, hosted_url, sha, _digest(manifest_bytes))
            ).fetchone()[0]
            return self._check_receipt(value, version, hosted_url, sha, manifest)
        except HostedByteHold:
            raise
        except Exception:
            raise HostedByteHold("generated_authority_reconcile_unavailable") from None
        finally:
            if not self._close(conn, rollback=True):
                raise HostedByteHold("generated_authority_cleanup_uncertain") from None

    def lookup(self, *, artifact_version_id, hosted_url, expected_sha256,
               manifest_bytes, receipt_id):
        version, sha, receipt_id = _uuid(artifact_version_id), _sha(expected_sha256), _uuid(receipt_id)
        _url(hosted_url)
        manifest = _manifest(manifest_bytes, self.tenant_id, version, hosted_url, sha)
        conn = self._open("lookup")
        try:
            value = conn.execute(
                "select public.generated_hosted_byte_lookup_20261009(%s,%s,%s,%s,%s,%s)",
                (self.tenant_id, version, hosted_url, sha, _digest(manifest_bytes), receipt_id)
            ).fetchone()[0]
            return self._check_receipt(value, version, hosted_url, sha, manifest, receipt_id)
        except HostedByteHold:
            raise
        except Exception:
            raise HostedByteHold("generated_authority_lookup_unavailable") from None
        finally:
            if not self._close(conn, rollback=True):
                raise HostedByteHold("generated_authority_cleanup_uncertain") from None
