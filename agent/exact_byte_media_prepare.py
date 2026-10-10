"""Protected exact-byte media preparation for the 20261010 send fence.

Copies each reviewed, eligible outgoing image (and optional thumbnail) to a
dedicated protected R2 prefix WITHOUT any byte transformation, then proves the
prepared object through agent.r2_immutable_media.verify_immutable_media:
bounded authenticated origin read, bounded public readback, and a signed
absolute-date bucket-lock attestation covering the provider fetch horizon.
A URL, a conditional PUT acknowledgment, or two matching reads alone are
never treated as immutability proof.

Reusable by future newly produced media (prepare_media_object /
prepare_calendar_row_media) and by the operator CLI for current eligible rows
(scripts/exact_byte_prepare_media.py). This module never touches the database,
never creates/demotes approvals, never sends, and is inert unless an operator
explicitly invokes it. All fence flags stay OFF.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from agent.r2_immutable_media import (
    ImmutableMediaError, SignedLockRuleSource, _canonical_url, _integer,
    verify_immutable_media, DEFAULT_MAX_BYTES,
)

# Interim supported outbound shape, mirroring the fence migration: a single
# image plus an explicitly owner-mapped thumbnail. Galleries/videos refuse.
UNSUPPORTED_MEDIA_COLUMNS = (
    "image_urls", "slide_urls", "media_urls", "carousel_urls",
    "media_items", "gbp_media", "video_url",
)
ALLOWED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".avi")
ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")
SOURCE_READ_TIMEOUT = (10, 60)


class MediaPrepareError(ValueError):
    """Safe fixed reason; provider exceptions and secrets are never included."""


@dataclass(frozen=True)
class PreparedMediaBinding:
    """One verified protected copy, bound to its exact reviewed source field."""
    ordinal: int
    role: str  # 'image' | 'thumbnail'
    source_url: str
    prepared_url: str
    key: str
    sha256: str  # 'sha256:<64 hex>'
    byte_length: int
    content_type: str
    lock_rule_id: str
    retention_until: int
    attestation_sha256: str

    def receipt(self):
        return {
            "ordinal": self.ordinal, "role": self.role,
            "source_url": self.source_url, "prepared_url": self.prepared_url,
            "sha256": self.sha256, "byte_length": self.byte_length,
            "content_type": self.content_type,
            "lock_rule_id": self.lock_rule_id,
            "retention_until": self.retention_until,
            "attestation_sha256": self.attestation_sha256,
        }


def _validate_https_url(url, what="media URL"):
    if not isinstance(url, str):
        raise MediaPrepareError("invalid %s" % what)
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url)):
        raise MediaPrepareError("invalid %s" % what)
    return parsed


def _image_extension(url):
    path = urlsplit(url).path.lower()
    if any(path.endswith(ext) or (ext + "?") in path for ext in VIDEO_EXTENSIONS):
        raise MediaPrepareError("video media unsupported")
    for ext in ALLOWED_IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return ext
    raise MediaPrepareError("unsupported image media type")


def _validate_protected_prefix(prefix):
    if (not isinstance(prefix, str) or not prefix or not prefix.endswith("/")
            or prefix.startswith("/") or "\\" in prefix or "%" in prefix
            or any(p in ("", ".", "..") for p in prefix.split("/")[:-1])
            or any(ord(c) < 32 or ord(c) == 127 for c in prefix)):
        raise MediaPrepareError("invalid protected prefix")
    return prefix


def _normalize_source_allowlist(allowlist):
    """Operator-pinned source host allowlist (exact host or dot-suffix).

    Required and non-empty: an empty allowlist fails CLOSED, so no code path
    can fetch an arbitrary HTTPS URL. Current R2 and Unsplash sources are
    preserved by listing their exact hosts in operator configuration.
    """
    if not isinstance(allowlist, (list, tuple)):
        raise MediaPrepareError("source host allowlist required")
    entries = []
    for entry in allowlist:
        if not isinstance(entry, str):
            raise MediaPrepareError("source host allowlist required")
        host = entry.strip().lower().rstrip(".")
        if (not host or "/" in host or "@" in host or ":" in host
                or "*" in host or any(ord(c) <= 32 or ord(c) >= 127 for c in host)):
            raise MediaPrepareError("source host allowlist entry invalid")
        entries.append(host)
    if not entries:
        raise MediaPrepareError("source host allowlist required")
    return tuple(entries)


def _host_allowed(hostname, allowlist):
    host = hostname.lower().rstrip(".")
    return any(host == entry or host.endswith("." + entry) for entry in allowlist)


def _resolve_host_addresses(hostname, port=443, resolver=None):
    """All resolved addresses for a host, via the injectable resolver."""
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except Exception:
        raise MediaPrepareError("source media host unresolvable") from None
    addresses = set()
    for info in infos or ():
        try:
            sockaddr = info[4]
            addresses.add(ipaddress.ip_address(sockaddr[0]))
        except Exception:
            raise MediaPrepareError("source media host resolution invalid") from None
    if not addresses:
        raise MediaPrepareError("source media host unresolvable")
    return addresses


def _reject_non_global_addresses(addresses):
    for address in addresses:
        if (not getattr(address, "is_global", False)
                or getattr(address, "is_multicast", False)):
            raise MediaPrepareError("source media host resolves to a non-public address")


def _validate_source_host(url, allowlist, resolver=None):
    """SSRF guard: allowlist-pinned, globally-routable source hosts only.

    Rejects credentials/fragments (already enforced by _validate_https_url),
    literal IP hosts, hosts outside the operator allowlist, and any host
    resolving to a private/loopback/link-local/reserved/multicast address.
    Returns the validated address set for the rebinding re-check.
    """
    hostname = urlsplit(url).hostname
    try:
        ipaddress.ip_address(hostname)
        is_literal_ip = True  # literal IPs are never allowlisted
    except ValueError:
        is_literal_ip = False
    if is_literal_ip:
        raise MediaPrepareError("source media host invalid")
    if not _host_allowed(hostname, allowlist):
        raise MediaPrepareError("source media host not allowed")
    addresses = _resolve_host_addresses(hostname, resolver=resolver)
    _reject_non_global_addresses(addresses)
    return addresses


def _verify_no_dns_rebinding(url, addresses, resolver=None):
    """Re-resolve immediately after the fetch: the second answer must be a
    subset of the pre-validated global addresses, so a flipped DNS answer
    (rebinding) cannot have steered the connection to a private target."""
    current = _resolve_host_addresses(urlsplit(url).hostname, resolver=resolver)
    _reject_non_global_addresses(current)
    if not current.issubset(addresses):
        raise MediaPrepareError("source media host resolution changed")


def _make_pin_for(allowed_hosts, resolver=None):
    """pin_for(host, port) -> one validated globally-routable IP string.

    Fail-closed address pinning for the HTTPS connection: only allowlisted
    hosts pin, EVERY resolved address must be globally routable, and the
    socket is then bound to one pre-validated address, so a
    public-private-public DNS flip cannot steer the connection."""
    hosts = tuple(h.lower().rstrip(".") for h in allowed_hosts)

    def pin_for(host, port=443):
        if not _host_allowed(host, hosts):
            raise MediaPrepareError("source media host not allowed")
        addresses = _resolve_host_addresses(host, port=port or 443,
                                            resolver=resolver)
        _reject_non_global_addresses(addresses)
        return str(sorted(addresses, key=str)[0])

    return pin_for


def _pinned_https_session(pin_for):
    """requests Session whose HTTPS sockets dial only a pinned, validated IP.

    The TCP dial target is pin_for(host) -- one pre-validated globally
    routable address -- while the connection keeps the real hostname for the
    Host header, TLS SNI and certificate hostname verification (urllib3's
    ``host`` property also feeds ``_dns_host``, so the pin is applied in a
    dedicated HTTPSConnection subclass instead of mutating ``_dns_host``;
    default CERT_REQUIRED + CA bundle verification is untouched). trust_env
    stays False: no ambient proxy/netrc credentials. Redirects remain
    disabled at each call site, so no unpinned host can be reached through a
    redirect either. A connection that was never pinned fails closed."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.exceptions import (ConnectTimeoutError, NameResolutionError,
                                    NewConnectionError)
    from urllib3.poolmanager import PoolManager
    from urllib3.util.connection import create_connection

    class _PinnedHTTPSConnection(HTTPSConnection):
        pinned_ip = None  # set by the pool AFTER construction; never trusted

        def _new_conn(self):
            pinned = self.pinned_ip
            if not pinned:
                raise MediaPrepareError("source media host not allowed")
            try:
                sock = create_connection(
                    (pinned, self.port), self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options)
            except socket.gaierror as e:
                raise NameResolutionError(self.host, self, e) from e
            except socket.timeout as e:
                raise ConnectTimeoutError(
                    self, "Connection to %s timed out. (connect timeout=%s)"
                    % (self.host, self.timeout)) from e
            except OSError as e:
                raise NewConnectionError(
                    self, "Failed to establish a new connection: %s" % e) from e
            return sock

    class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = _PinnedHTTPSConnection

        def _new_conn(self):
            conn = super()._new_conn()
            # Dial the pinned, pre-validated address only; conn.host (and
            # conn._dns_host) keep the real hostname for Host/SNI/certificate
            # verification, and TLS is wrapped by urllib3 after _new_conn.
            conn.pinned_ip = pin_for(conn.host, conn.port or 443)
            return conn

    class _PinnedHTTPSAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            manager = PoolManager(num_pools=connections, maxsize=maxsize,
                                  block=block, **kw)
            # pool_classes_by_scheme is a class attribute (not a constructor
            # kwarg); override it on the instance.
            manager.pool_classes_by_scheme = {
                "http": HTTPConnectionPool, "https": _PinnedHTTPSConnectionPool}
            self.poolmanager = manager

    session = requests.Session()
    session.trust_env = False
    session.mount("https://", _PinnedHTTPSAdapter())
    return session


def _read_source(url, max_bytes, session=None, source_allowlist=None, resolver=None):
    """Bounded, redirect-free, credential-free exact source read.

    SSRF-guarded: the host must be pinned by the operator allowlist and must
    resolve only to globally routable addresses. The default transport dials
    only a pinned, pre-validated address (Host/SNI/certificate checks keep
    the real hostname), and resolution is re-checked after the fetch to
    refuse DNS rebinding."""
    addresses = _validate_source_host(url, source_allowlist, resolver=resolver)
    if session is None:
        session = _pinned_https_session(
            _make_pin_for((urlsplit(url).hostname,), resolver=resolver))
        owned = True
    else:
        owned = False
    response = None
    try:
        response = session.get(url, stream=True, allow_redirects=False,
                               timeout=SOURCE_READ_TIMEOUT,
                               headers={"Accept-Encoding": "identity"})
        _verify_no_dns_rebinding(url, addresses, resolver=resolver)
        if response.status_code != 200 or response.url != url or response.history:
            raise MediaPrepareError("source media redirects or unavailable")
        headers = response.headers
        if headers.get("Content-Encoding", "").lower() not in ("", "identity"):
            raise MediaPrepareError("source media content encoding unsupported")
        declared = headers.get("Content-Length")
        if declared is not None and (not declared.isdigit()
                                     or not 0 < int(declared) <= max_bytes):
            raise MediaPrepareError("invalid source content length")
        content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise MediaPrepareError("source media content type unsupported")
        response.raw.decode_content = False
        data = bytearray()
        while True:
            chunk = response.raw.read(min(65536, max_bytes + 1 - len(data)))
            if not isinstance(chunk, bytes):
                raise MediaPrepareError("source media read unavailable")
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise MediaPrepareError("source media exceeds byte limit")
        if not data:
            raise MediaPrepareError("empty source media")
        if declared is not None and len(data) != int(declared):
            raise MediaPrepareError("source content length mismatch")
        return bytes(data), content_type
    finally:
        if response is not None:
            response.close()
        if owned:
            session.close()


def _create_if_absent(s3, bucket, key, data, content_type):
    """Write-once upload. An existing object is never overwritten; a
    create-if-absent conflict means a concurrent/previous identical prepare
    and is resolved by the later full verification, never by trusting the PUT."""
    try:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return False  # already present; bytes proven by verification below
        except Exception as head_error:
            code = str(getattr(head_error, "response", {}) or {})
            status = getattr(getattr(head_error, "response", None), "get", None)
            http = None
            resp = getattr(head_error, "response", None)
            if isinstance(resp, dict):
                http = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if http not in (404,) and "404" not in code and "NotFound" not in code \
                    and "NoSuchKey" not in code:
                raise MediaPrepareError("protected object lookup unavailable")
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=data,
                          ContentType=content_type, IfNoneMatch="*")
            return True
        except Exception as put_error:
            resp = getattr(put_error, "response", None)
            err = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
            if err in ("PreconditionFailed", "ConditionalRequestConflict", "412") \
                    or "PreconditionFailed" in str(put_error):
                return False  # lost a create race; verification decides
            raise MediaPrepareError("protected object write unavailable")
    except MediaPrepareError:
        raise
    except Exception:
        raise MediaPrepareError("protected object write unavailable") from None


def prepare_media_object(source_url, *, role, ordinal, bucket, account_id,
                         public_base_url, protected_prefix, s3, lock_source,
                         required_retention_until, source_allowlist,
                         max_bytes=DEFAULT_MAX_BYTES,
                         now=None, session=None, resolver=None):
    """Copy one exact media object into the protected prefix and prove it.

    Returns a PreparedMediaBinding or raises MediaPrepareError /
    ImmutableMediaError with a safe fixed reason. The R2 key is derived from
    the content digest, so identical bytes always map to one write-once
    object; the key carries no tenant or row authority.
    """
    try:
        if role not in ("image", "thumbnail") or not _integer(ordinal) or ordinal < 0:
            raise MediaPrepareError("invalid media binding role")
        if not _integer(required_retention_until) or not _integer(max_bytes) \
                or not 0 < max_bytes <= DEFAULT_MAX_BYTES:
            raise MediaPrepareError("invalid preparation bounds")
        if not isinstance(lock_source, SignedLockRuleSource):
            raise MediaPrepareError("trusted lock source required")
        _validate_https_url(source_url, "source media URL")
        allowlist = _normalize_source_allowlist(source_allowlist)
        prefix = _validate_protected_prefix(protected_prefix)
        extension = _image_extension(source_url)
        owned_session = None
        if session is None:
            # One address-pinned transport for BOTH the source read and the
            # protected public readback: pin the operator allowlist plus the
            # operator-pinned public base host. Without a pinned dial
            # address, requests resolves at connect time and a
            # public-private-public DNS flip (rebinding) could steer the
            # connection after validation.
            pin_hosts = list(allowlist)
            base_host = urlsplit(public_base_url).hostname or ""
            if not _host_allowed(base_host, pin_hosts):
                pin_hosts.append(base_host.lower().rstrip("."))
            session = _pinned_https_session(_make_pin_for(pin_hosts,
                                                          resolver=resolver))
            owned_session = session
        try:
            data, content_type = _read_source(source_url, max_bytes, session,
                                              source_allowlist=allowlist,
                                              resolver=resolver)
            digest = hashlib.sha256(data).hexdigest()
            key = prefix + "sha256/" + digest + extension
            prepared_url = _canonical_url(public_base_url, key)
            _create_if_absent(s3, bucket, key, data, content_type)
            proof = verify_immutable_media(
                prepared_url, key=key, bucket=bucket, account_id=account_id,
                public_base_url=public_base_url, s3=s3, lock_source=lock_source,
                required_retention_until=required_retention_until,
                max_bytes=max_bytes, now=now, session=session)
            if proof.size_bytes != len(data) or proof.sha256 != digest:
                raise MediaPrepareError("prepared object differs from reviewed bytes")
            return PreparedMediaBinding(
                ordinal=ordinal, role=role, source_url=source_url,
                prepared_url=proof.public_url, key=key, sha256="sha256:" + proof.sha256,
                byte_length=proof.size_bytes, content_type=content_type,
                lock_rule_id=proof.lock_rule_id,
                retention_until=proof.retention_until,
                attestation_sha256=proof.attestation_sha256)
        finally:
            if owned_session is not None:
                owned_session.close()
    except (MediaPrepareError, ImmutableMediaError):
        raise
    except Exception:
        raise MediaPrepareError("media preparation unavailable") from None


def prepare_calendar_row_media(row, *, image_fields=("image_url",), max_bytes=DEFAULT_MAX_BYTES,
                               **kwargs):
    """Prepare every outbound image of one reviewed calendar row.

    row is a mapping with at least image_url and (when declared by the audited
    target's image_fields) thumbnail_url. Refuses any unsupported complete
    outbound shape (gallery, video, extra media columns) exactly like the
    fence's send context, so a prepared row can never surprise the fence.
    Returns [PreparedMediaBinding, ...] in ordinal order.
    """
    if tuple(image_fields) not in (("image_url",), ("image_url", "thumbnail_url")):
        raise MediaPrepareError("unsupported audited image fields")
    if not isinstance(row, dict):
        raise MediaPrepareError("invalid calendar row")
    # Fail closed before any read when the operator allowlist is missing.
    _normalize_source_allowlist(kwargs.get("source_allowlist"))
    for column in UNSUPPORTED_MEDIA_COLUMNS:
        value = row.get(column)
        if value not in (None, "", [], {}):
            raise MediaPrepareError("unsupported complete outbound image shape")
    image_url = row.get("image_url")
    _validate_https_url(image_url, "row image URL")
    _image_extension(image_url)  # refuses video by URL shape before any read
    bindings = [prepare_media_object(image_url, role="image", ordinal=0,
                                     max_bytes=max_bytes, **kwargs)]
    if "thumbnail_url" in image_fields:
        thumbnail_url = row.get("thumbnail_url")
        if thumbnail_url:
            _validate_https_url(thumbnail_url, "row thumbnail URL")
            _image_extension(thumbnail_url)
            bindings.append(prepare_media_object(thumbnail_url, role="thumbnail",
                                                 ordinal=1, max_bytes=max_bytes,
                                                 **kwargs))
    return bindings
