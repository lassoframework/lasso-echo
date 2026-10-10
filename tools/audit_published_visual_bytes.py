"""Bounded, read-only historical delivered-image byte audit.

Reads a private local JSON array of calendar rows (id, gym_id, image_url,
status, post_date), optionally fetches each unique image URL for published
rows, or for approved and pending rows with explicit --include-future, over
HTTPS from an explicitly allowed origin,
and writes a mode-0600 JSON receipt mapping row ids to the exact URL and its
MD5/SHA256 digests, byte length and per-row status.

Transport is fail-closed: the allowed host is resolved once, every resolved
address must be a public globally-routable IP, and the TLS connection is
opened directly to that pinned IP via raw socket + ssl + http.client (no
urllib opener, no proxy handling) while certificate verification and the Host
header still use the original hostname (DNS-rebinding resistant). Redirects
(any non-200 status) are refused. Statuses outside the selected scope are
never fetched.

No database access, no DB writes, no provider calls, no redirects, no
cross-host fetches, 128 MiB streamed cap. Network is OFF unless --fetch is
passed explicitly. Never infers source_media_url, source_asset_id, lineage,
visual scene, or approval.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
from urllib.parse import urlsplit

MAX_BYTES = 128 * 1024 * 1024  # 128 MiB
REQUIRED_FIELDS = ("id", "gym_id", "image_url", "status", "post_date")
PUBLISHED_STATUS = "published"
FUTURE_STATUSES = ("approved", "pending")


def _selected_statuses(include_future):
    """Statuses (after trim + casefold) eligible for auditing.

    Default is published-only; --include-future also audits approved and
    pending rows. Rows in other statuses are never fetched.
    """
    selected = {PUBLISHED_STATUS}
    if include_future:
        selected.update(FUTURE_STATUSES)
    return selected


def _validate_url(url, allowed_origin):
    """Return (ok, reason).

    Enforces HTTPS, host == allowed origin host, default port only (443),
    no userinfo, and a real path. The allowed origin itself must be a bare
    host origin: https scheme, no path, no non-default port, no userinfo.
    """
    if not isinstance(url, str) or not url.strip():
        return False, "missing_image_url"
    try:
        parts = urlsplit(url.strip())
        url_port = parts.port
    except ValueError:
        return False, "invalid_url"
    if parts.scheme != "https":
        return False, "not_https"
    if parts.hostname is None:
        return False, "missing_host"
    if url_port is not None and url_port != 443:
        return False, "non_default_port"
    try:
        origin_parts = urlsplit(
            allowed_origin if "://" in allowed_origin else "https://" + allowed_origin
        )
        origin_port = origin_parts.port
    except ValueError:
        return False, "invalid_allowed_origin"
    if origin_parts.scheme != "https" or not origin_parts.hostname:
        return False, "invalid_allowed_origin"
    if origin_port not in (None, 443):
        return False, "invalid_allowed_origin"
    if origin_parts.username or origin_parts.password:
        return False, "invalid_allowed_origin"
    if origin_parts.path not in ("", "/") or origin_parts.query or origin_parts.fragment:
        return False, "invalid_allowed_origin"
    if parts.hostname.lower() != origin_parts.hostname.lower():
        return False, "host_not_allowed"
    if parts.username or parts.password:
        return False, "userinfo_not_allowed"
    if not parts.path or parts.path == "/":
        return False, "missing_path"
    return True, None


def _resolve_public_ip(host, resolver=None):
    """Resolve host once; return (pinned_ip, None) or (None, reason).

    Fails closed unless every resolved address is a public globally-routable
    IP. Any private, loopback, link-local, multicast or reserved result
    rejects the fetch (DNS-rebinding / SSRF guard).
    """
    if resolver is None:
        resolver = socket.getaddrinfo
    try:
        infos = resolver(host, 443, type=socket.SOCK_STREAM)
    except Exception:
        return None, "resolution_failed"
    addrs = []
    for info in infos or []:
        try:
            addrs.append(info[4][0])
        except (IndexError, TypeError):
            continue
    if not addrs:
        return None, "resolution_failed"
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return None, "non_public_resolution"
        if not (ip.is_global and not ip.is_multicast):
            return None, "non_public_resolution"
    # Pin the first public address; all resolved addresses were validated above.
    return addrs[0], None


class _PinnedHTTPSConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a pinned IP but does TLS SNI/hostname
    verification against the original host. Raw socket only: no proxy
    handling of any kind."""

    def __init__(self, pinned_ip, server_hostname, timeout):
        super().__init__(pinned_ip, 443, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._server_hostname = server_hostname

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, 443), self.timeout)
        context = ssl.create_default_context()  # verifies cert for server_hostname
        self.sock = context.wrap_socket(sock, server_hostname=self._server_hostname)


def _default_connection_factory(pinned_ip, host, timeout):
    return _PinnedHTTPSConnection(pinned_ip, host, timeout)


def _fetch_and_hash(url, allowed_origin, resolver=None, connection_factory=None):
    """Stream the exact delivered bytes over a pinned-IP HTTPS connection.

    Returns dict with digests/length, or a failed status with reason.
    Redirects (any non-200 response) are refused.
    """
    ok, reason = _validate_url(url, allowed_origin)
    if not ok:
        return {"status": "failed", "reason": reason}
    url = url.strip()
    parts = urlsplit(url)
    host = parts.hostname
    pinned_ip, reason = _resolve_public_ip(host, resolver=resolver)
    if pinned_ip is None:
        return {"status": "failed", "reason": reason}
    if connection_factory is None:
        connection_factory = _default_connection_factory
    target = parts.path + ("?" + parts.query if parts.query else "")
    conn = None
    try:
        conn = connection_factory(pinned_ip, host, 30)
        conn.request("GET", target, headers={"Host": host, "Accept": "*/*"})
        resp = conn.getresponse()
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return {"status": "failed", "reason": f"fetch_error:{type(exc).__name__}"}
    try:
        if resp.status != 200:  # includes every 3xx: redirects are refused
            return {"status": "failed", "reason": "redirect_or_non_200_refused"}
        md5 = hashlib.md5()  # noqa: S324 - audit fingerprint only, not security
        sha256 = hashlib.sha256()
        total = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                return {"status": "failed", "reason": "exceeds_128mib_cap"}
            md5.update(chunk)
            sha256.update(chunk)
        return {
            "status": "fetched",
            "md5": md5.hexdigest(),
            "sha256": sha256.hexdigest(),
            "length": total,
        }
    except Exception as exc:
        return {"status": "failed", "reason": f"read_error:{type(exc).__name__}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_rows(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("input must be a JSON array of calendar rows")
    return data


def audit(rows, allowed_origin, fetch=False, include_future=False,
          resolver=None, connection_factory=None):
    receipt = {}
    seen = {}
    selected = _selected_statuses(include_future)
    not_selected_reason = (
        "status_not_selected" if include_future else "status_not_published"
    )
    counts = {
        "rows_total": 0,
        "rows_ok": 0,
        "rows_failed": 0,
        "rows_skipped_nonpublished": 0,
        "unique_urls": 0,
        "urls_fetched": 0,
        "include_future": bool(include_future),
        "selected_statuses": sorted(selected),
        "rows_skipped_status": 0,
    }
    for idx, row in enumerate(rows):
        counts["rows_total"] += 1
        row_id = row.get("id") if isinstance(row, dict) else None
        key = row_id if row_id is not None else f"__row_index_{idx}"
        if not isinstance(row, dict) or any(f not in row for f in REQUIRED_FIELDS):
            receipt[str(key)] = {"status": "failed", "reason": "missing_required_field"}
            counts["rows_failed"] += 1
            continue
        status = row.get("status")
        if not (isinstance(status, str) and status.strip().lower() in selected):
            receipt[str(key)] = {"status": "skipped", "reason": not_selected_reason}
            counts["rows_skipped_nonpublished"] += 1
            counts["rows_skipped_status"] += 1
            continue
        url = row.get("image_url")
        ok, reason = _validate_url(url, allowed_origin) if isinstance(url, str) else (False, "missing_image_url")
        if not ok:
            receipt[str(key)] = {"status": "failed", "reason": reason}
            counts["rows_failed"] += 1
            continue
        url = url.strip()
        if url not in seen:
            counts["unique_urls"] += 1
            if fetch:
                counts["urls_fetched"] += 1
                seen[url] = _fetch_and_hash(
                    url, allowed_origin, resolver=resolver, connection_factory=connection_factory
                )
            else:
                seen[url] = {"status": "not_fetched", "reason": "fetch_disabled"}
        result = dict(seen[url])
        result["url"] = url
        receipt[str(key)] = result
        if result.get("status") == "failed":
            counts["rows_failed"] += 1
        else:
            counts["rows_ok"] += 1
    return receipt, counts


def write_receipt(receipt, path, selected_statuses=None):
    payload = {
        "audit": "published_visual_bytes",
        "version": 1,
        "rows": receipt,
    }
    if selected_statuses is not None:
        # Make the audited scope explicit without implying every row was published.
        payload["selected_statuses"] = list(selected_statuses)
    # A new receipt path prevents a check/open race and protects prior evidence.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="private local JSON array of calendar rows")
    parser.add_argument("--allowed-origin", required=True, help="exact HTTPS origin host allowed, e.g. https://cdn.example.com")
    parser.add_argument("--receipt", required=True, help="output receipt path (written mode 0600)")
    parser.add_argument("--fetch", action="store_true", help="actually fetch bytes over HTTPS (default: no network)")
    parser.add_argument(
        "--include-future",
        action="store_true",
        help="also audit rows with status approved or pending (default: published only)",
    )
    args = parser.parse_args(argv)

    rows = load_rows(args.input)
    receipt, counts = audit(
        rows, args.allowed_origin, fetch=args.fetch, include_future=args.include_future
    )
    write_receipt(receipt, args.receipt, selected_statuses=counts["selected_statuses"])
    print(
        "coverage: rows_total={rows_total} rows_ok={rows_ok} rows_failed={rows_failed} "
        "rows_skipped_nonpublished={rows_skipped_nonpublished} unique_urls={unique_urls} "
        "urls_fetched={urls_fetched} fetch={fetch} "
        "include_future={include_future} selected_statuses={selected_statuses}".format(
            fetch="on" if args.fetch else "off", **counts
        )
    )
    return 0 if counts["rows_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
