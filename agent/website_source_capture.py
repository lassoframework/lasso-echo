"""Bytes-preserving HTTPS website capture for the echo_source_captures lane.

Contract: docs/echo/verified-source-brand-bundle.md in the portal bundle
(read-only). This module is the trusted server-side capture step: it fetches
a website URL over HTTPS only when an explicit entry of the authoritative
portal_domains mapping authorizes that exact host (and path scope), and it
records the ORIGINAL RAW RESPONSE BYTES — never decoded text, never a
reconstructed string. Extraction, fact approval and palette decisions are
out of scope here and happen later, inside the portal.

Hard rules:
  * The portal_domains mapping is the ONLY source of domains/URLs. A URL no
    entry authorizes is refused before any network or DNS work.
  * HTTPS only. Any other scheme fails closed. Userinfo and any explicit
    port other than 443 are refused before DNS/connection, on the initial
    URL and every redirect hop (an explicit :443 is fine).
  * SSRF guard with REAL IP PINNING, not a lookup the connection ignores.
    Every host we connect to — the requested one, every redirect target and
    every robots.txt fetch — is DNS-resolved, every resolved address must be
    public, and the TLS connection is then opened to the VETTED IP itself
    with SNI and certificate hostname verification against the ORIGINAL
    hostname. There is no second, unpinned resolution inside the HTTP
    library, so DNS rebinding between vetting and connect (TOCTOU) cannot
    move the connection to a different address.
  * Redirects are followed manually, bounded (MAX_REDIRECTS), and every hop
    is re-validated the full way: HTTPS scheme, mapping authorization for
    the SAME gym (path scope included), fresh DNS re-resolve + re-vet, and a
    freshly pinned connection to the re-vetted IP.
  * Response bodies are streamed as the ORIGINAL ENTITY BYTES
    (decode_content=False: no text decoding, no decompression, no
    reconstruction) and capped (MAX_BYTES, 2,000,000 per the capture table
    contract). robots.txt is bounded too (MAX_ROBOTS_BYTES). Every response
    is closed only after its body has been fully read (or the read has
    failed); nothing is closed out from under a pending read. Any violation
    fails closed: no partial capture is returned.
  * robots.txt is honored fail-closed (a host whose robots.txt cannot be
    read, exceeds the robots cap, or answers 5xx is not fetched).
  * A minimum per-host delay is respected between fetches.

FIX PATH (lead review 2026-10-07): the default transport is a REAL pinned
transport built on urllib3 (>=2, already a repo dependency via requests):
`urllib3.HTTPSConnectionPool` is opened against the pinned IP with
`server_hostname` / `assert_hostname` set to the original hostname,
`cert_reqs="CERT_REQUIRED"` and the certifi CA bundle. The TCP connect goes
to the vetted IP (no re-resolution); TLS SNI and certificate verification
still authenticate the original hostname, so this is safe pinning proven
from library capabilities, not a separate DNS lookup the connection does
not use. The transport remains injectable so a differently reviewed pinned
transport can be substituted; injection does not weaken the contract
because capture() still vets DNS and passes the pinned IP to whatever
transport is installed.

This module deliberately does NOT use gym_deep_brain.CappedFetcher: that
fetcher decodes bytes to text, and this lane's whole point is preserving
the exact response bytes. No CappedFetcher decoded output is consumed here.

All I/O is injectable (transport, DNS resolver, robots fetcher, sleep,
clock, wall-time) so tests run fully offline with synthetic fakes.
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

import certifi
import urllib3

MAX_BYTES = 2_000_000          # echo_source_captures.raw_bytes upper bound
MIN_BYTES = 1                  # echo_source_captures.raw_bytes lower bound
MAX_ROBOTS_BYTES = 512_000     # robots.txt is bounded too
MAX_REDIRECTS = 5
MIN_HOST_DELAY_S = 1.0
USER_AGENT = "LASSO-Echo-SourceCapture/1.0"
_UA_TOKENS = (USER_AGENT, "LASSO-Echo", "*")

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class CaptureError(Exception):
    """Fail-closed refusal. No partial capture exists when this is raised."""


@dataclass(frozen=True)
class PortalDomainEntry:
    """One entry of the authoritative portal_domains mapping.

    Authorizes captures for exactly one gym on exactly one host, optionally
    restricted to path prefixes. `mapping_revision` and `mapping_evidence`
    are the frozen proof of the explicit tenant mapping the capture table
    requires; they are recorded verbatim on every capture.
    """

    gym_id: str
    echo_account_key: str
    host: str
    source_kind: str = "website"            # website | website_asset
    path_prefixes: tuple = ()               # empty = any path on the host
    mapping_revision: str = ""
    mapping_evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        host = (self.host or "").strip().lower()
        if not host or "/" in host or "@" in host or ":" in host:
            raise CaptureError(f"portal_domains entry has invalid host {self.host!r}")
        if self.source_kind not in ("website", "website_asset"):
            raise CaptureError(f"unsupported source_kind {self.source_kind!r}")
        if not self.gym_id or not self.echo_account_key:
            raise CaptureError("portal_domains entry missing gym_id/echo_account_key")
        object.__setattr__(self, "host", host)

    def authorizes(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() != "https":
            return False
        try:
            _check_url_form(parts, url)
        except CaptureError:
            return False
        if (parts.hostname or "").lower() != self.host:
            return False
        if self.path_prefixes:
            path = parts.path or "/"
            if not any(path.startswith(p) for p in self.path_prefixes):
                return False
        return True


class PortalDomains:
    """The authoritative mapping, injected. The only source of domains."""

    def __init__(self, entries: Iterable[PortalDomainEntry]):
        self._entries = list(entries)

    def entry_for(self, url: str) -> Optional[PortalDomainEntry]:
        for entry in self._entries:
            if entry.authorizes(url):
                return entry
        return None

    def entry_for_gym(self, gym_id: str, url: str) -> Optional[PortalDomainEntry]:
        for entry in self._entries:
            if entry.gym_id == gym_id and entry.authorizes(url):
                return entry
        return None


def _noop_close():
    return None


@dataclass(frozen=True)
class RawResponse:
    """Transport result. `body` is an iterable of bytes chunks (streamed,
    original entity bytes). `close` releases the underlying connection and
    is invoked by the capture loop only after the body has been fully read
    (or the read has failed)."""

    status: int
    headers: dict
    body: Iterable[bytes]
    close: Callable = _noop_close


@dataclass(frozen=True)
class CapturedSource:
    """A completed capture: raw bytes plus provenance. Raw bytes only —
    no decoded text, no extracted facts, no colors."""

    gym_id: str
    echo_account_key: str
    source_kind: str
    requested_url: str
    source_url: str               # final URL after redirects
    status: int
    raw_bytes: bytes
    bytes_sha256: str
    fetched_at: str               # ISO-8601 UTC server fetch timestamp
    mapping_revision: str
    mapping_evidence: dict


def _is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return ip.is_global


def _check_url_form(parts, url: str) -> None:
    """Low-level URL safety rails enforced before any DNS or connection, on
    the initial URL and every redirect hop: no userinfo, and no explicit
    port other than 443 (scheme-default or an explicit :443 is fine). An
    unparseable port (``https://host:abc/``) is a refusal, not a pass."""
    if parts.username is not None or parts.password is not None:
        raise CaptureError(f"userinfo in URL {url!r}; refused")
    try:
        port = parts.port
    except ValueError:
        raise CaptureError(f"unparseable port in URL {url!r}; refused")
    if port is not None and port != 443:
        raise CaptureError(f"explicit port {port} in URL {url!r}; refused")


def _default_resolver(host: str) -> list:
    import socket
    infos = socket.getaddrinfo(host, None)
    return sorted({info[4][0] for info in infos})


def _default_pinned_transport(url: str, pinned_ip: str, headers: dict,
                              timeout: float) -> RawResponse:
    """REAL pinned HTTPS transport (the fix-path documented in the module
    docstring). The TCP connection is opened to `pinned_ip` — an address the
    caller already vetted as public — while TLS SNI and certificate hostname
    verification use the ORIGINAL hostname from `url`. urllib3 never
    re-resolves the hostname for this connection, so a DNS answer that
    changes after vetting cannot redirect the connection.

    The body is streamed with decode_content=False: the exact entity bytes
    as sent, no text decoding, no decompression, no reconstruction. The
    response and pool are released by `close`, which the capture loop calls
    only after the body has been fully read or the read has failed.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    pool = urllib3.HTTPSConnectionPool(
        pinned_ip,
        port=port,
        timeout=timeout,
        retries=False,
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where(),
        assert_hostname=host,
        server_hostname=host,
    )
    try:
        resp = pool.request(
            "GET", path,
            headers={**headers, "Host": host},
            preload_content=False,
            decode_content=False,
        )
    except Exception:
        pool.close()
        raise

    closed = {"done": False}

    def _close():
        if closed["done"]:
            return
        closed["done"] = True
        try:
            resp.close()
        finally:
            pool.close()

    return RawResponse(status=int(resp.status),
                       headers=dict(resp.headers),
                       body=resp.stream(65536),
                       close=_close)


class _RobotsGate:
    """Fail-closed robots.txt policy, same posture as gym_deep_brain."""

    def __init__(self, fetch_robots):
        self._fetch_robots = fetch_robots
        self._cache: dict = {}

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            deny = True
            try:
                status, body = self._fetch_robots(f"https://{host}/robots.txt")
                if status is not None and 200 <= status < 500:
                    rp.parse((body or b"").decode("utf-8", "replace").splitlines())
                    deny = False
            except Exception:
                deny = True
            if deny:
                rp.parse(["User-agent: *", "Disallow: /"])
            self._cache[host] = rp
        rp = self._cache[host]
        for token in _UA_TOKENS:
            try:
                if not rp.can_fetch(token, url):
                    return False
            except Exception:
                return False
        return True


class WebsiteSourceCapture:
    """Mapping-driven, bytes-preserving website capture. Fail closed on any
    safety violation: a raised CaptureError means nothing was captured.

    `transport(url, pinned_ip, headers, timeout) -> RawResponse` must open
    the connection to `pinned_ip` (already vetted public) with TLS SNI and
    certificate verification against the hostname in `url`. The default is
    the real urllib3 pinned transport documented at the top of this module.
    """

    def __init__(self, portal_domains: PortalDomains, *,
                 transport: Optional[Callable] = None,
                 resolver: Optional[Callable] = None,
                 robots_fetch: Optional[Callable] = None,
                 sleep: Optional[Callable] = None,
                 clock: Optional[Callable] = None,
                 now: Optional[Callable] = None,
                 rate_gate: Optional[Callable] = None,
                 max_bytes: int = MAX_BYTES,
                 max_robots_bytes: int = MAX_ROBOTS_BYTES,
                 max_redirects: int = MAX_REDIRECTS,
                 min_host_delay: float = MIN_HOST_DELAY_S,
                 timeout: float = 15.0):
        if portal_domains is None:
            raise CaptureError("portal_domains mapping is required")
        self._mapping = portal_domains
        self._transport = transport or _default_pinned_transport
        self._resolver = resolver or _default_resolver
        self._robots = _RobotsGate(robots_fetch or self._fetch_robots_default)
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_bytes = int(max_bytes)
        self._max_robots_bytes = int(max_robots_bytes)
        self._max_redirects = int(max_redirects)
        self._min_host_delay = float(min_host_delay)
        self._timeout = float(timeout)
        self._rate_gate = rate_gate
        self._last_fetch_by_host: dict = {}

    # -- DNS vetting: resolve once per hop, vet every answer, pin the result --

    def _vet_host(self, url: str) -> tuple:
        """Resolve and vet `url`'s host. Returns (host, pinned_ip) where
        pinned_ip is a vetted public address the transport MUST connect to."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            raise CaptureError(f"no host in {url!r}")
        try:
            addrs = self._resolver(host)
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(f"DNS resolution failed for {host}: {exc}")
        if not addrs:
            raise CaptureError(f"DNS resolution returned no addresses for {host}")
        for addr in addrs:
            if not _is_public_ip(addr):
                raise CaptureError(
                    f"{host} resolves to non-public address {addr}; refused")
        return host, str(addrs[0])

    def _rate_limit(self, host: str):
        """When a shared gate is installed (e.g. by the collector, which
        holds per-host state across capture instances), it owns the delay;
        otherwise this instance's own per-host minimum applies."""
        if self._rate_gate is not None:
            self._rate_gate(host)
            return
        prev = self._last_fetch_by_host.get(host)
        if prev is not None:
            owed = self._min_host_delay - (self._clock() - prev)
            if owed > 0:
                self._sleep(owed)
        self._last_fetch_by_host[host] = self._clock()

    def _read_capped(self, resp: RawResponse, url: str, cap: int) -> bytes:
        """Read the whole streamed body, enforcing the byte cap. The caller
        closes `resp` after this returns or raises."""
        buf = bytearray()
        try:
            for chunk in resp.body or ():
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    raise CaptureError(
                        "transport returned decoded text; raw bytes required")
                buf.extend(chunk)
                if len(buf) > cap:
                    raise CaptureError(f"{url!r} exceeded the {cap} byte cap")
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(f"error reading {url!r}: {exc}")
        return bytes(buf)

    def _fetch_robots_default(self, url: str):
        """Pinned and bounded robots.txt fetch: same DNS vetting and the same
        pinned transport as page fetches, capped at max_robots_bytes."""
        _, pinned = self._vet_host(url)
        resp = self._transport(url, pinned, {"User-Agent": USER_AGENT},
                               self._timeout)
        try:
            return resp.status, self._read_capped(resp, url,
                                                  self._max_robots_bytes)
        finally:
            resp.close()

    def capture(self, url: str) -> CapturedSource:
        url = (url or "").strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() != "https":
            raise CaptureError(f"only HTTPS URLs may be captured: {url!r}")
        _check_url_form(parts, url)
        entry = self._mapping.entry_for(url)
        if entry is None:
            raise CaptureError(
                f"no portal_domains entry authorizes {url!r}; refused")

        fetched_at = self._now()
        current = url
        redirects = 0
        while True:
            # Every hop is fully re-validated: DNS re-resolve + re-vet, then
            # the connection is pinned to the re-vetted IP.
            host, pinned_ip = self._vet_host(current)
            if not self._robots.allowed(current):
                raise CaptureError(f"robots.txt disallows {current!r}")
            self._rate_limit(host)
            try:
                resp = self._transport(current, pinned_ip,
                                       {"User-Agent": USER_AGENT},
                                       self._timeout)
            except CaptureError:
                raise
            except Exception as exc:
                raise CaptureError(f"fetch of {current!r} failed: {exc}")
            try:
                status = int(getattr(resp, "status", 0) or 0)

                if status in _REDIRECT_STATUSES:
                    # Drain (bounded) before close; the redirect body is
                    # discarded, never captured.
                    self._read_capped(resp, current, self._max_bytes)
                    location = (resp.headers or {}).get("Location") or \
                               (resp.headers or {}).get("location")
                    if not location:
                        raise CaptureError(
                            f"redirect from {current!r} without Location header")
                    redirects += 1
                    if redirects > self._max_redirects:
                        raise CaptureError(
                            f"redirect limit {self._max_redirects} exceeded at {current!r}")
                    target = urllib.parse.urljoin(current, location)
                    target_parts = urllib.parse.urlsplit(target)
                    if target_parts.scheme.lower() != "https":
                        raise CaptureError(
                            f"redirect to non-HTTPS URL {target!r}; refused")
                    _check_url_form(target_parts, target)
                    next_entry = self._mapping.entry_for_gym(entry.gym_id, target)
                    if next_entry is None:
                        raise CaptureError(
                            f"redirect to {target!r} is not authorized by a "
                            f"portal_domains entry for gym {entry.gym_id!r}; refused")
                    current = target
                    entry = next_entry
                    continue

                if status < 200 or status >= 300:
                    raise CaptureError(f"{current!r} answered HTTP {status}")

                raw = self._read_capped(resp, current, self._max_bytes)
            finally:
                # Closed only after the body has been fully read (or the read
                # failed); never out from under a pending read.
                resp.close()

            if len(raw) < MIN_BYTES:
                raise CaptureError(f"{current!r} returned an empty body")
            return CapturedSource(
                gym_id=entry.gym_id,
                echo_account_key=entry.echo_account_key,
                source_kind=entry.source_kind,
                requested_url=url,
                source_url=current,
                status=status,
                raw_bytes=raw,
                bytes_sha256=hashlib.sha256(raw).hexdigest(),
                fetched_at=fetched_at.isoformat(),
                mapping_revision=entry.mapping_revision,
                mapping_evidence=dict(entry.mapping_evidence),
            )
