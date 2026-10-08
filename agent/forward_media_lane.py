"""Exact environment contracts and credential-free public object reads.

These contracts belong only to isolated owner/attester processes, never the
shared publisher. An unrecognized name is rejected even when its value is empty.
"""
import ipaddress
import os
import socket
import time
from urllib.parse import unquote, urlsplit

RUNTIME_NAMES = frozenset({
    'PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
    'PYTHONPATH', 'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
    'PGSSLMODE', 'PGSSLROOTCERT',
})
# The existing public CDN configuration is the only media configuration admitted.
COMMON_NAMES = RUNTIME_NAMES | {'AGENT_S3_PUBLIC_BASE_URL'}
OWNER_NAMES = COMMON_NAMES | {
    'FORWARD_MEDIA_OWNER_DSN', 'FORWARD_MEDIA_OWNER_ROLE',
    'AGENT_FORWARD_MEDIA_OWNER_WORKER', 'AGENT_FORWARD_MEDIA_OWNER_TENANTS',
    'AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', 'AGENT_FORWARD_MEDIA_OWNER_PHOTO_CLEARANCE',
    'FORWARD_MEDIA_OWNER_DRIVE_SA_JSON',
}
ATTESTER_NAMES = COMMON_NAMES | {
    'AGENT_FORWARD_MEDIA_ATTESTER_DSN', 'AGENT_FORWARD_MEDIA_ATTESTER_ROLE',
    'AGENT_FORWARD_MEDIA_GUARD', 'AGENT_FORWARD_MEDIA_ATTESTER_WORKER',
    'AGENT_FORWARD_MEDIA_ATTESTER_TENANTS', 'AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE',
    'AGENT_FORWARD_MEDIA_ATTESTER_INTERVAL_SECONDS',
}


def unknown_environment_names(environ, lane):
    allowed = {'owner': OWNER_NAMES, 'attester': ATTESTER_NAMES}[lane]
    return sorted(name for name in environ if name not in allowed)


class PublicObjectReadError(RuntimeError):
    """Static error only: URLs, driver failures and credentials stay private."""


def approved_url(url, base=None):
    base = os.environ.get('AGENT_S3_PUBLIC_BASE_URL', '') if base is None else base
    if not isinstance(url, str) or not isinstance(base, str):
        return False
    if any(c.isspace() or ord(c) < 32 for c in url + base):
        return False
    try:
        target, origin = urlsplit(url), urlsplit(base)
        if (origin.scheme != 'https' or target.scheme != 'https'
                or not origin.hostname or target.netloc != origin.netloc
                or origin.username or origin.password or target.username or target.password
                or origin.query or origin.fragment or target.fragment
                or origin.port not in (None, 443) or target.port not in (None, 443)
                or not target.path.startswith(origin.path.rstrip('/') + '/')
                or len(target.path) <= len(origin.path.rstrip('/')) + 1):
            return False
        for part in target.path.split('/'):
            decoded = unquote(part)
            if decoded in ('.', '..') or any(c in decoded for c in ('/', '\\')):
                return False
            if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
                return False
        try:
            return ipaddress.ip_address(origin.hostname).is_global
        except ValueError:
            return origin.hostname.lower() != 'localhost' and not origin.hostname.lower().endswith(('.localhost', '.local', '.internal'))
    except ValueError:
        return False


def read_public_object(url, *, max_bytes=128 * 1024 * 1024):
    """GET the exact URL including query; never use S3, netrc, proxies or redirects."""
    if not approved_url(url):
        raise PublicObjectReadError('approved public media origin required')
    try:
        host = urlsplit(url).hostname
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise PublicObjectReadError('public media address required')
        import requests
        deadline = time.monotonic() + 30
        with requests.Session() as session:
            session.trust_env = False
            # trust_env=False also disables ambient CA discovery; admit only the
            # explicit runtime CA path, never requests' ambient proxy/auth state.
            session.verify = (os.environ.get('REQUESTS_CA_BUNDLE')
                              or os.environ.get('SSL_CERT_FILE')
                              or os.environ.get('SSL_CERT_DIR') or True)
            with session.get(url, stream=True, allow_redirects=False,
                             timeout=(5, 5), headers={'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200:
                    raise PublicObjectReadError('public object unavailable')
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise PublicObjectReadError('exact public object encoding required')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise PublicObjectReadError('bounded public object required')
                data = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise PublicObjectReadError('bounded public object required')
                    # read1 returns available bytes after one underlying read;
                    # a slow drip cannot hide inside a full 64 KiB chunk read.
                    chunk = response.raw.read1(64 * 1024, decode_content=False)
                    if not chunk:
                        break
                    if time.monotonic() > deadline or len(data) + len(chunk) > max_bytes:
                        raise PublicObjectReadError('bounded public object required')
                    data.extend(chunk)
                if not data or time.monotonic() > deadline:
                    raise PublicObjectReadError('bounded public object required')
                return bytes(data)
    except PublicObjectReadError:
        raise
    except Exception:
        raise PublicObjectReadError('public object read unavailable') from None
