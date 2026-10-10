"""Exact-byte HISTORICAL seed/export preparation (20261010). Operator tool only.

This module prepares, but never applies, the trusted-operator historical seed
for migrations/delivered_byte_send_fence_20261010.sql. Hard rules:

* Read-only DB access through an injected reader (a single stable read-only
  REPEATABLE READ snapshot when the real DB is used). No SQLite substitute.
* Fresh complete census on EVERY run: published status OR published_at
  non-null OR late_post_id non-null, keyset-paginated by id. No bundled census;
  the 1467-row figure is a fail-closed floor from a prior observation only.
* Row revisions, the historical snapshot digest and the corpus digest follow
  the SQL authority functions exactly; mismatches block the seed.
* Every media occurrence (image_url AND thumbnail_url when present) is fetched
  and recorded as observation_kind='db_published_url_bytes'. A DB-published
  URL fetch is NEVER provider receipt or platform readback evidence.
* Tenant/timezone/provider-target and sibling identity come only from explicit
  auditable mapping/proof inputs with evidence_ref. Nothing is inferred.
* Fail closed: any blocker yields a manifest marked INCOMPLETE_DO_NOT_APPLY
  and NO seed SQL artifact.
* Never runs generated SQL, never activates the gate, never touches
  content_calendar, never reads or logs secret environment values.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import queue
import re
import socket
import ssl
import time
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASELINE_MIN_ROWS = 1467  # Oct 10 05:32 UTC observation floor; never a census.
PAGE_SIZE = 500
MAX_BYTES = 134217728
MAX_HEADER_BYTES = 65536
MAX_HEADER_COUNT = 100
MAX_HEADER_LINE_BYTES = 8192
OBSERVATION_KIND = "db_published_url_bytes"
ACCOUNTS = ("instagram", "facebook", "googlebusiness")
FORMATS = ("feed", "story")
IMAGE_FIELDS_OK = (["image_url"], ["image_url", "thumbnail_url"])
MEDIA_COLUMNS = ("image_urls", "slide_urls", "media_urls", "carousel_urls",
                 "media_items", "gbp_media", "video_url")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
URL_RE = re.compile(r"^https://[^\s]+$")
NS = uuid.uuid5(uuid.NAMESPACE_DNS, "echo.exact_byte_history_seed_20261010")

SEED_TABLES = (
    "exact_byte_tenant_20261010", "exact_byte_target_20261010",
    "exact_byte_sibling_proof_20261010", "exact_byte_sibling_member_20261010",
    "exact_byte_history_coverage_20261010",
    "exact_byte_history_observation_20261010",
)


class SeedError(RuntimeError):
    """Hard failure; the run must not emit a seed artifact."""


@dataclass
class CensusRow:
    row_id: str
    revision: str
    gym_id: str
    account: str
    fmt: str
    post_date: str
    image_url: str
    thumbnail_url: str
    logical_post_id: str
    raw: dict


# ---------------------------------------------------------------------------
# Postgres jsonb text rendering for the restricted value set we emit
# (objects, arrays, strings, integers, booleans, null). This mirrors
# jsonb::text: keys sorted by (byte length, bytes), ", " / ": " separators,
# JSON string escapes with lowercase \u00xx control escapes, raw UTF-8.
# The readback plan re-validates the corpus digest inside SQL; any serializer
# drift fails closed there.
# ---------------------------------------------------------------------------

def _pg_string(value):
    out = ['"']
    for ch in value:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\b':
            out.append('\\b')
        elif ch == '\f':
            out.append('\\f')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif o < 0x20:
            out.append('\\u%04x' % o)
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def pg_jsonb_text(value):
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _pg_string(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(pg_jsonb_text(v) for v in value) + ']'
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode('utf-8')), k.encode('utf-8')))
        return '{' + ', '.join(_pg_string(k) + ': ' + pg_jsonb_text(value[k])
                               for k in keys) + '}'
    raise SeedError('unsupported value type for jsonb rendering: %r' % type(value))


def _sha256_text(text):
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def local_historical_snapshot(census):
    """exact_byte_historical_snapshot_20261010() over census (id, revision)."""
    pairs = [{'row_id': r.row_id, 'revision': r.revision}
             for r in sorted(census, key=lambda r: r.row_id)]
    return _sha256_text(pg_jsonb_text(pairs))


def local_corpus_digest(tenants, targets, coverage, observations):
    """exact_byte_corpus_digest_20261010() over the emitted seed rows."""
    doc = {
        'tenants': sorted(tenants, key=lambda t: t['calendar_gym_id']),
        'targets': sorted(targets, key=lambda t: (t['calendar_gym_id'],
                                                  t['account'], t['format'])),
        'coverage': sorted(coverage, key=lambda t: t['calendar_row_id']),
        'observations': sorted(({k: v for k, v in o.items() if k != 'role'}
                                for o in observations),
                               key=lambda t: t['observation_id']),
    }
    return _sha256_text(pg_jsonb_text(doc))


# ---------------------------------------------------------------------------
# URL fetching: HTTPS only, exact allowlisted hosts, no redirects, SSRF-safe,
# hard byte and time limits. Injected in tests.
# ---------------------------------------------------------------------------

class FetchError(RuntimeError):
    """Static error: URLs and network detail stay out of messages."""


def _pg_ts(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')


def _open_pinned_connection(hostname, ip_address, timeout):
    """Open a TLS connection to the pre-validated IP only.

    The TCP peer is the validated public address, never a re-resolved name.
    SNI and certificate hostname validation still use the original hostname
    via ssl.create_default_context().wrap_socket(server_hostname=...), so a
    pinned connection cannot bypass certificate verification.
    """
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((ip_address, 443), timeout=timeout)
    try:
        context = ssl.create_default_context()
        raw.settimeout(_time_remaining(deadline))
        return context.wrap_socket(raw, server_hostname=hostname)
    except Exception:
        raw.close()
        raise


def _time_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError('bounded public object required')
    return remaining


def _resolve_before_deadline(hostname, deadline):
    """Bound caller wait for libc DNS, which Python cannot forcibly cancel.

    A timed-out daemon resolver may finish in the background, but it only
    resolves addresses: it cannot open a connection or fetch any object.
    """
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(
                hostname, 443, type=socket.SOCK_STREAM)))
        except Exception:
            result.put((False, None))

    threading.Thread(target=resolve, daemon=True).start()
    try:
        ok, addresses = result.get(timeout=_time_remaining(deadline))
    except queue.Empty:
        raise FetchError('bounded public object required') from None
    _time_remaining(deadline)
    if not ok:
        raise FetchError('public media address required')
    return addresses


def make_https_fetcher(allowed_hosts, *, max_bytes=MAX_BYTES, timeout=30):
    hosts = frozenset(h.lower() for h in allowed_hosts)
    if not hosts:
        raise SeedError('an explicit HTTPS host allowlist is required')

    def fetch(url):
        deadline = time.monotonic() + timeout
        if not isinstance(url, str) or not URL_RE.match(url):
            raise FetchError('https url required')
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname
                or parts.hostname.lower() not in hosts
                or parts.username or parts.password
                or parts.port not in (None, 443)):
            raise FetchError('url outside approved host allowlist')
        hostname = parts.hostname.lower()
        # Resolve exactly once and pin the connection to a validated public
        # address. A later DNS change (rebinding) cannot reroute the request:
        # no name resolution happens after this point.
        try:
            addresses = _resolve_before_deadline(hostname, deadline)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global
                                    for a in addresses):
                raise FetchError('public media address required')
        except FetchError:
            raise
        except Exception:
            raise FetchError('public media address required') from None
        pinned_ip = addresses[0][4][0]
        try:
            target = parts.path or '/'
            if parts.query:
                target += '?' + parts.query
            request = (
                'GET %s HTTP/1.1\r\n'
                'Host: %s\r\n'
                'Accept-Encoding: identity\r\n'
                'Connection: close\r\n'
                '\r\n' % (target, hostname)
            ).encode('ascii')
            conn = _open_pinned_connection(
                hostname, pinned_ip, min(5, _time_remaining(deadline)))
            fp = None
            try:
                conn.settimeout(_time_remaining(deadline))
                conn.sendall(request)
                fp = conn.makefile('rb')
                buffered = bytearray()

                def read_chunk(size):
                    # read1 performs at most one underlying read. Buffered
                    # read/readline can keep receiving trickled bytes forever
                    # even when the socket's inactivity timeout never expires.
                    conn.settimeout(_time_remaining(deadline))
                    chunk = fp.read1(size)
                    _time_remaining(deadline)
                    return chunk

                def header_line():
                    while True:
                        _time_remaining(deadline)
                        newline = buffered.find(b'\n')
                        if newline >= 0:
                            if newline + 1 > MAX_HEADER_LINE_BYTES:
                                raise FetchError('bounded public object required')
                            line = bytes(buffered[:newline + 1])
                            del buffered[:newline + 1]
                            return line
                        if len(buffered) >= MAX_HEADER_LINE_BYTES:
                            raise FetchError('bounded public object required')
                        chunk = read_chunk(MAX_HEADER_LINE_BYTES - len(buffered))
                        if not chunk:
                            raise FetchError('public object read unavailable')
                        buffered.extend(chunk)

                status_line = header_line()
                header_bytes = len(status_line)
                smatch = re.match(rb'^HTTP/1\.[01] (\d{3})[ \t]', status_line)
                if not smatch or smatch.group(1) != b'200':
                    raise FetchError('public object unavailable')
                headers = {}
                header_count = 0
                while True:
                    line = header_line()
                    header_bytes += len(line)
                    if header_bytes > MAX_HEADER_BYTES:
                        raise FetchError('bounded public object required')
                    if line in (b'\r\n', b'\n'):
                        break
                    header_count += 1
                    if header_count > MAX_HEADER_COUNT:
                        raise FetchError('bounded public object required')
                    if b':' not in line:
                        raise FetchError('public object unavailable')
                    hname, hvalue = line.split(b':', 1)
                    name = hname.strip().lower().decode('ascii', 'replace')
                    if name in headers and name in (
                            'content-length', 'content-encoding',
                            'transfer-encoding'):
                        raise FetchError('public object unavailable')
                    headers[name] = hvalue.strip().decode('ascii', 'replace')

                def body_chunk(size):
                    if buffered:
                        _time_remaining(deadline)
                        chunk = bytes(buffered[:size])
                        del buffered[:size]
                        return chunk
                    return read_chunk(size)

                if headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise FetchError('exact public object encoding required')
                if 'transfer-encoding' in headers:
                    raise FetchError('exact public object encoding required')
                length = headers.get('content-length')
                if length is not None and (not length.isdigit()
                                           or int(length) > max_bytes):
                    raise FetchError('bounded public object required')
                digest = hashlib.sha256()
                total = 0
                if length is not None:
                    remaining = int(length)
                    while remaining > 0:
                        if time.monotonic() > deadline:
                            raise FetchError('bounded public object required')
                        chunk = body_chunk(min(64 * 1024, remaining))
                        if not chunk:
                            raise FetchError('public object read unavailable')
                        remaining -= len(chunk)
                        total += len(chunk)
                        digest.update(chunk)
                else:
                    while True:
                        if time.monotonic() > deadline:
                            raise FetchError('bounded public object required')
                        chunk = body_chunk(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError('bounded public object required')
                        digest.update(chunk)
                if total < 1 or time.monotonic() > deadline:
                    raise FetchError('bounded public object required')
                return 'sha256:' + digest.hexdigest(), total
            finally:
                try:
                    if fp is not None:
                        fp.close()
                finally:
                    conn.close()
        except FetchError:
            raise
        except Exception:
            raise FetchError('public object read unavailable') from None

    return fetch


# ---------------------------------------------------------------------------
# Census: fresh, keyset-paginated, completeness-checked.
# ---------------------------------------------------------------------------

def run_census(reader, *, page_size=PAGE_SIZE):
    rows = []
    seen = set()
    after = None
    while True:
        page = reader.census_page(after, page_size)
        if not isinstance(page, list):
            raise SeedError('census reader returned a non-list page')
        if len(page) > page_size:
            raise SeedError('census reader returned an oversized page')
        for item in page:
            row = _census_row(item)
            if row.row_id in seen:
                raise SeedError('duplicate calendar row id in census')
            if after is not None and row.row_id <= after:
                raise SeedError('census pagination order violated')
            seen.add(row.row_id)
            rows.append(row)
            after = row.row_id
        if len(page) < page_size:
            break
    return rows


def _clean(value):
    return value.strip() if isinstance(value, str) else ''


def _census_row(item):
    if not isinstance(item, dict) or not isinstance(item.get('row'), dict):
        raise SeedError('census item must carry row json')
    raw = item['row']
    try:
        row_id = str(uuid.UUID(str(raw.get('id'))))
    except (ValueError, TypeError, AttributeError):
        raise SeedError('calendar row id is not a valid uuid') from None
    revision = item.get('revision')
    if not isinstance(revision, str) or not SHA256_RE.match(revision):
        raise SeedError('row revision missing or malformed')
    gym_id = _clean(raw.get('gym_id'))
    account = _clean(raw.get('account'))
    fmt = _clean(raw.get('format'))
    post_date = _clean(str(raw.get('post_date') or ''))
    if not gym_id:
        raise SeedError('calendar row missing gym_id')
    if account not in ACCOUNTS or fmt not in FORMATS:
        raise SeedError('calendar row has unsupported account/format')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', post_date):
        raise SeedError('calendar row missing post_date')
    if not (raw.get('status') == 'published' or raw.get('published_at') is not None
            or raw.get('late_post_id') is not None):
        raise SeedError('census row outside the historical predicate')
    logical = _clean(str(raw.get('logical_post_id') or ''))
    if logical:
        try:
            logical = str(uuid.UUID(logical))
        except (ValueError, TypeError, AttributeError):
            raise SeedError('calendar row has invalid logical_post_id') from None
    return CensusRow(row_id=row_id, revision=revision, gym_id=gym_id,
                     account=account, fmt=fmt, post_date=post_date,
                     image_url=_clean(raw.get('image_url')),
                     thumbnail_url=_clean(raw.get('thumbnail_url')),
                     logical_post_id=logical, raw=raw)


# ---------------------------------------------------------------------------
# Mapping / proof inputs: explicit, auditable, never inferred.
# ---------------------------------------------------------------------------

def load_mapping(data):
    if not isinstance(data, dict):
        raise SeedError('mapping input must be a JSON object')
    required = ('tenants', 'targets', 'sibling_proofs', 'sibling_members',
                'no_media_evidence', 'observation_evidence_ref', 'census_review')
    for key in required:
        if key not in data:
            raise SeedError('mapping input missing key %r' % key)
    obs_ref = data['observation_evidence_ref']
    if not _clean(obs_ref):
        raise SeedError('observation_evidence_ref is required')

    review = data['census_review']
    if not isinstance(review, dict):
        raise SeedError('census_review must be an object')
    r_snap = review.get('historical_snapshot_sha256')
    if not isinstance(r_snap, str) or not SHA256_RE.match(r_snap):
        raise SeedError('census_review historical_snapshot_sha256 missing or '
                        'malformed')
    r_count = review.get('census_row_count')
    if not isinstance(r_count, int) or isinstance(r_count, bool) or r_count < 0:
        raise SeedError('census_review census_row_count missing or invalid')
    if not _clean(review.get('evidence_ref')):
        raise SeedError('census_review requires a nonblank evidence_ref')
    review = {'historical_snapshot_sha256': r_snap, 'census_row_count': r_count,
              'evidence_ref': review['evidence_ref'].strip()}

    tenants = {}
    for t in data['tenants']:
        for f in ('calendar_gym_id', 'canonical_tenant', 'posting_timezone', 'evidence_ref'):
            if not _clean(t.get(f)):
                raise SeedError('tenant mapping missing %s' % f)
        gym = t['calendar_gym_id'].strip()
        if gym in tenants:
            raise SeedError('ambiguous tenant mapping for calendar_gym_id')
        try:
            ZoneInfo(t['posting_timezone'].strip())
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise SeedError('tenant mapping has unknown posting_timezone') from None
        tenants[gym] = {'calendar_gym_id': gym,
                        'canonical_tenant': t['canonical_tenant'].strip(),
                        'posting_timezone': t['posting_timezone'].strip(),
                        'evidence_ref': t['evidence_ref'].strip()}

    targets = {}
    for t in data['targets']:
        for f in ('calendar_gym_id', 'account', 'format', 'provider_target',
                  'image_fields', 'evidence_ref'):
            if f not in t:
                raise SeedError('target mapping missing %s' % f)
        key = (_clean(t['calendar_gym_id']), _clean(t['account']), _clean(t['format']))
        if not key[0] or key[1] not in ACCOUNTS or key[2] not in FORMATS:
            raise SeedError('target mapping has unsupported identity')
        if key in targets:
            raise SeedError('ambiguous target mapping')
        pt = t['provider_target']
        if (not isinstance(pt, dict)
                or not _clean(pt.get('provider')) or not _clean(pt.get('platform'))
                or not _clean(pt.get('account_id'))):
            raise SeedError('provider_target requires provider/platform/account_id')
        if t['image_fields'] not in IMAGE_FIELDS_OK:
            raise SeedError('target mapping has unsupported image_fields')
        if not _clean(t['evidence_ref']):
            raise SeedError('target mapping missing evidence_ref')
        targets[key] = {'calendar_gym_id': key[0], 'account': key[1], 'format': key[2],
                        'provider_target': pt, 'image_fields': list(t['image_fields']),
                        'evidence_ref': t['evidence_ref'].strip()}

    proofs = {}
    for p in data['sibling_proofs']:
        try:
            identity = str(uuid.UUID(str(p.get('shared_posting_identity'))))
            logical = str(uuid.UUID(str(p.get('logical_post_id'))))
        except (ValueError, TypeError, AttributeError):
            raise SeedError('sibling proof has invalid uuid') from None
        tenant = _clean(p.get('canonical_tenant'))
        date = _clean(str(p.get('post_date') or ''))
        if not tenant or not re.match(r'^\d{4}-\d{2}-\d{2}$', date) \
                or not _clean(p.get('evidence_ref')):
            raise SeedError('sibling proof incomplete')
        if identity in proofs or (tenant, date, logical) in [
                (q['canonical_tenant'], q['post_date'], q['logical_post_id'])
                for q in proofs.values()]:
            raise SeedError('ambiguous sibling proof')
        raw_ids = p.get('member_row_ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            raise SeedError('sibling proof must explicitly list its precise '
                            'member row ids')
        member_row_ids = set()
        for v in raw_ids:
            try:
                member_row_ids.add(str(uuid.UUID(str(v))))
            except (ValueError, TypeError, AttributeError):
                raise SeedError('sibling proof member row id invalid') from None
        proofs[identity] = {'shared_posting_identity': identity,
                            'canonical_tenant': tenant, 'post_date': date,
                            'logical_post_id': logical,
                            'member_row_ids': member_row_ids,
                            'evidence_ref': p['evidence_ref'].strip()}

    members = []
    seen_members = set()
    for m in data['sibling_members']:
        try:
            row_id = str(uuid.UUID(str(m.get('calendar_row_id'))))
            identity = str(uuid.UUID(str(m.get('shared_posting_identity'))))
        except (ValueError, TypeError, AttributeError):
            raise SeedError('sibling member has invalid uuid') from None
        if not _clean(m.get('evidence_ref')):
            raise SeedError('sibling member missing evidence_ref')
        if (row_id, identity) in seen_members:
            raise SeedError('duplicate sibling member')
        seen_members.add((row_id, identity))
        members.append({'calendar_row_id': row_id, 'shared_posting_identity': identity,
                        'evidence_ref': m['evidence_ref'].strip()})

    no_media = {}
    for n in data['no_media_evidence']:
        try:
            row_id = str(uuid.UUID(str(n.get('calendar_row_id'))))
        except (ValueError, TypeError, AttributeError):
            raise SeedError('no-media evidence has invalid uuid') from None
        if not _clean(n.get('evidence_ref')):
            raise SeedError('no-media evidence missing evidence_ref')
        no_media[row_id] = n['evidence_ref'].strip()

    return {'tenants': tenants, 'targets': targets, 'proofs': proofs,
            'members': members, 'no_media': no_media, 'obs_ref': obs_ref.strip(),
            'review': review}


# ---------------------------------------------------------------------------
# Seed assembly.
# ---------------------------------------------------------------------------

@dataclass
class SeedResult:
    manifest: dict
    seed_sql: str | None
    readback_sql: str | None
    blockers: list = field(default_factory=list)


def build_seed(census, mapping, *, fetcher, baseline_min=BASELINE_MIN_ROWS,
               now=None):
    baseline_min = max(int(baseline_min), BASELINE_MIN_ROWS)
    blockers = []
    now = now or datetime.now(timezone.utc)
    observed_at = _pg_ts(now)

    if len(census) < baseline_min:
        blockers.append('census row count %d below baseline floor %d (prior '
                        'observation only; investigate before proceeding)'
                        % (len(census), baseline_min))

    observations = []
    coverage = []
    members = []
    member_rows = {m['calendar_row_id'] for m in mapping['members']}
    census_by_id = {r.row_id: r for r in census}

    for row in census:
        tenant = mapping['tenants'].get(row.gym_id)
        if tenant is None:
            blockers.append('missing tenant mapping for calendar_gym_id (row %s)'
                            % row.row_id)
            continue
        target = mapping['targets'].get((row.gym_id, row.account, row.fmt))
        if target is None:
            blockers.append('missing provider target mapping for %s/%s/%s (row %s)'
                            % (row.gym_id, row.account, row.fmt, row.row_id))
            continue
        # Fail closed: only null, an empty array or an empty string count as
        # empty alternate media. An empty object '{}' is NOT empty and blocks.
        bad = [c for c in MEDIA_COLUMNS
               if row.raw.get(c) is not None and row.raw.get(c) != []
               and row.raw.get(c) != '']
        if bad:
            blockers.append('row %s carries unsupported gallery/video media fields: %s'
                            % (row.row_id, ', '.join(sorted(bad))))
            continue
        # Fail closed on thumbnail-only rows: migration completeness accepts a
        # coverage row only when image_url is non-blank with every non-blank
        # URL observed, or when BOTH image_url and thumbnail_url are blank
        # (separately evidenced no-media). A non-blank thumbnail_url with a
        # blank image_url can NEVER satisfy either branch, so preparation
        # refuses to emit any seed SQL rather than emit an incomplete corpus.
        if not row.image_url and row.thumbnail_url:
            blockers.append('row %s has a thumbnail_url but no image_url; '
                            'migration completeness cannot cover this row '
                            '(no-media requires BOTH urls null)' % row.row_id)
            continue

        urls = []
        if row.image_url:
            urls.append(('image', row.image_url))
        if row.thumbnail_url:
            if 'thumbnail_url' not in target['image_fields']:
                blockers.append('row %s thumbnail_url not declared by audited '
                                'image_fields for its target' % row.row_id)
                continue
            urls.append(('thumbnail', row.thumbnail_url))
        if not urls:
            ref = mapping['no_media'].get(row.row_id)
            if ref is None:
                blockers.append('row %s has no image/thumbnail URL and no '
                                'independently reviewed no-media evidence' % row.row_id)
                continue
            coverage.append({'calendar_row_id': row.row_id, 'row_revision': row.revision,
                             'complete': True, 'no_images_verified': True,
                             'evidence_ref': ref})
        else:
            ok = True
            for role, url in urls:
                if not URL_RE.match(url):
                    blockers.append('row %s has a non-https media URL' % row.row_id)
                    ok = False
                    break
                try:
                    sha256, byte_length = fetcher(url)
                except Exception:
                    blockers.append('row %s media URL observation failed (fetch '
                                    'denied/unavailable)' % row.row_id)
                    ok = False
                    break
                if (not isinstance(sha256, str) or not SHA256_RE.match(sha256)
                        or not isinstance(byte_length, int)
                        or not 1 <= byte_length <= MAX_BYTES):
                    blockers.append('row %s media observation returned invalid digest'
                                    % row.row_id)
                    ok = False
                    break
                observations.append({
                    'observation_id': str(uuid.uuid5(
                        NS, '%s|%s|%s|%s' % (row.row_id, row.revision, role, url))),
                    'role': role,
                    'calendar_row_id': row.row_id,
                    'row_revision': row.revision,
                    'canonical_tenant': tenant['canonical_tenant'],
                    'post_date': row.post_date,
                    'shared_posting_identity': None,
                    'exact_url': url,
                    'sha256': sha256,
                    'byte_length': byte_length,
                    'observation_kind': OBSERVATION_KIND,
                    'evidence_ref': mapping['obs_ref'],
                    'observed_at': observed_at,
                    'recorded_at': observed_at,
                })
            if not ok:
                continue
            # complete=true is the reviewed census assertion: bind the
            # census_review evidence_ref, never the generic observation ref.
            coverage.append({'calendar_row_id': row.row_id, 'row_revision': row.revision,
                             'complete': True, 'no_images_verified': False,
                             'evidence_ref': mapping['review']['evidence_ref']})

    # Sibling identity: only exact, explicitly evidenced proof input.
    for m in mapping['members']:
        row = census_by_id.get(m['calendar_row_id'])
        proof = mapping['proofs'].get(m['shared_posting_identity'])
        if row is None:
            blockers.append('sibling member references a row outside the census')
            continue
        if proof is None:
            blockers.append('sibling member references an unknown proof')
            continue
        tenant = mapping['tenants'].get(row.gym_id)
        if tenant is None or proof['canonical_tenant'] != tenant['canonical_tenant'] \
                or proof['post_date'] != row.post_date:
            blockers.append('sibling proof does not cover the precise '
                            'tenant/date of row %s' % row.row_id)
            continue
        if not row.logical_post_id or row.logical_post_id != proof['logical_post_id']:
            blockers.append('row %s persisted logical_post_id does not equal the '
                            'sibling proof logical_post_id' % row.row_id)
            continue
        members.append({'calendar_row_id': row.row_id, 'row_revision': row.revision,
                        'shared_posting_identity': m['shared_posting_identity'],
                        'evidence_ref': m['evidence_ref']})
    member_inputs = {}
    for m in mapping['members']:
        member_inputs.setdefault(m['shared_posting_identity'], set()).add(
            m['calendar_row_id'])
    for identity in sorted(member_inputs):
        proof = mapping['proofs'].get(identity)
        if proof is None:
            continue
        if member_inputs[identity] != proof['member_row_ids']:
            blockers.append('sibling proof %s member row ids do not exactly '
                            'equal the sibling member input' % identity)
        if len(member_inputs[identity]) < 2:
            blockers.append('sibling proof %s has fewer than two distinct member '
                            'rows; singleton sibling proofs are rejected'
                            % identity)
    membered = {(m['calendar_row_id'], m['shared_posting_identity']) for m in members}
    for obs in observations:
        for m in members:
            if m['calendar_row_id'] == obs['calendar_row_id']:
                obs['shared_posting_identity'] = m['shared_posting_identity']
    obs_ids = [o['observation_id'] for o in observations]
    if len(set(obs_ids)) != len(obs_ids):
        blockers.append('duplicate observation identity generated')

    tenants = sorted(mapping['tenants'].values(), key=lambda t: t['calendar_gym_id'])
    targets = sorted(mapping['targets'].values(),
                     key=lambda t: (t['calendar_gym_id'], t['account'], t['format']))
    proofs = sorted(mapping['proofs'].values(),
                    key=lambda p: p['shared_posting_identity'])

    return blockers, tenants, targets, proofs, members, coverage, observations


# ---------------------------------------------------------------------------
# Deterministic SQL emission. Insert authority only; never gate/cutover,
# never content_calendar.
# ---------------------------------------------------------------------------

def _sql_str(value):
    if '\\' in value:
        raise SeedError('backslash in emitted literal is unsupported')
    return "'" + value.replace("'", "''") + "'"


def _sql_value(value, cast=None):
    if value is None:
        lit = 'null'
    elif value is True:
        lit = 'true'
    elif value is False:
        lit = 'false'
    elif isinstance(value, int):
        lit = str(value)
    elif isinstance(value, str):
        lit = _sql_str(value)
    elif isinstance(value, list) and cast == 'text[]':
        return 'array[' + ', '.join(_sql_str(v) for v in value) + ']::text[]'
    elif isinstance(value, (list, dict)):
        lit = _sql_str(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':')))
        cast = cast or 'jsonb'
    else:
        raise SeedError('unsupported SQL value')
    return lit + ('::' + cast if cast else '')


def _insert(table, columns, rows, casts):
    lines = ['insert into public.%s (%s) values' % (table, ', '.join(columns))]
    vals = []
    for row in rows:
        vals.append('  (' + ', '.join(_sql_value(row[c], casts.get(c))
                                      for c in columns) + ')')
    lines.append(',\n'.join(vals) + ';')
    return '\n'.join(lines)


def emit_seed_sql(tenants, targets, proofs, members, coverage, observations,
                  *, snapshot_digest, corpus_digest):
    counts = (
        ('exact_byte_tenant_20261010', len(tenants)),
        ('exact_byte_target_20261010', len(targets)),
        ('exact_byte_sibling_proof_20261010', len(proofs)),
        ('exact_byte_sibling_member_20261010', len(members)),
        ('exact_byte_history_coverage_20261010', len(coverage)),
        ('exact_byte_history_observation_20261010', len(observations)),
    )
    zero_if = ('\n    or '.join("(select count(*) from public.%s) <> 0" % t
                                for t, _ in counts))
    count_if = ('\n    or '.join("(select count(*) from public.%s) <> %d" % (t, n)
                                 for t, n in counts))
    pre_guard = (
        "do $$\n"
        "begin\n"
        "  if public.exact_byte_historical_snapshot_20261010() is distinct from\n"
        "     '%s' then\n"
        "    raise exception 'historical snapshot changed since the seed freeze; "
        "refusing to apply';\n"
        "  end if;\n"
        "  if %s then\n"
        "    raise exception 'exact-byte seed tables are not empty; refusing "
        "blind retry or partial-seed reuse';\n"
        "  end if;\n"
        "end $$;" % (snapshot_digest, zero_if))
    post_guard = (
        "do $$\n"
        "begin\n"
        "  if %s then\n"
        "    raise exception 'post-insert seed row counts do not match the "
        "frozen manifest';\n"
        "  end if;\n"
        "  if public.exact_byte_corpus_digest_20261010() is distinct from\n"
        "     '%s' then\n"
        "    raise exception 'post-insert corpus digest differs from the "
        "generated digest';\n"
        "  end if;\n"
        "  if public.exact_byte_history_complete_20261010() is not true then\n"
        "    raise exception 'history completeness check is not true after "
        "seeding';\n"
        "  end if;\n"
        "  if public.exact_byte_historical_snapshot_20261010() is distinct from\n"
        "     '%s' then\n"
        "    raise exception 'historical snapshot changed during seed apply';\n"
        "  end if;\n"
        "end $$;" % (count_if, corpus_digest, snapshot_digest))
    header = [
        '-- EXACT-BYTE HISTORICAL SEED 20261010 (operator-prepared, unapplied)',
        '-- Frozen historical_snapshot digest: %s' % snapshot_digest,
        '-- Frozen corpus digest (post-apply readback must match): %s' % corpus_digest,
        '-- Apply ONLY as a login inheriting exact_byte_owner_20261010, after the',
        '-- migration is applied. Everything below runs in ONE READ COMMITTED',
        '-- transaction: any raised exception rolls back ALL inserts (no partial',
        '-- seed). Pre-guards freeze the historical snapshot and require every',
        '-- seed table to be empty; post-guards re-check counts, the corpus',
        '-- digest, history completeness and the snapshot before commit.',
        '-- This script never WRITES content_calendar and never reads the gate',
        '-- table: the owner role has NO SELECT privilege on it. A separately',
        '-- privileged, read-only gate-OFF readback receipt is required BEFORE',
        '-- and AFTER apply; keeping the gate OFF is a separate release gate.',
        '-- The applying login must ALSO hold SELECT on public.content_calendar:',
        '-- the migration grants exact_byte_owner_20261010 no privilege there,',
        '-- and the SHARE lock + snapshot pre-guard below require it.',
        '-- Lock discipline (mirrors exact_byte_activate_20261010 exactly, so',
        '-- lock ordering cannot deadlock against activation/send): bounded',
        '-- lock_timeout/statement_timeout first, then the shared transaction',
        '-- advisory lock, then content_calendar IN SHARE MODE, then the',
        '-- snapshot pre-guard. SHARE conflicts with the send path row locks,',
        '-- but both paths take the advisory lock first, so they serialize.',
        '-- Observation rows are db_published_url_bytes ONLY: they are not',
        '-- provider receipts and not platform readback evidence.',
        'begin;',
        'set transaction isolation level read committed;',
        "set local lock_timeout = '5s';",
        "set local statement_timeout = '15min';",
        "select pg_catalog.pg_advisory_xact_lock(",
        "       hashtextextended('exact_byte_send_20261010', 0));",
        'lock table public.content_calendar in share mode;',
        '',
        pre_guard,
    ]
    body = []
    if tenants:
        body.append(_insert('exact_byte_tenant_20261010',
                            ('calendar_gym_id', 'canonical_tenant',
                             'posting_timezone', 'evidence_ref'), tenants, {}))
    if targets:
        body.append(_insert('exact_byte_target_20261010',
                            ('calendar_gym_id', 'account', 'format',
                             'provider_target', 'image_fields', 'evidence_ref'),
                            [{**t, 'image_fields': t['image_fields']}
                             for t in targets],
                            {'provider_target': 'jsonb',
                             'image_fields': 'text[]'}))
    if proofs:
        body.append(_insert('exact_byte_sibling_proof_20261010',
                            ('shared_posting_identity', 'canonical_tenant',
                             'post_date', 'logical_post_id', 'evidence_ref'),
                            proofs, {'shared_posting_identity': 'uuid',
                                     'post_date': 'date',
                                     'logical_post_id': 'uuid'}))
    if members:
        body.append(_insert('exact_byte_sibling_member_20261010',
                            ('calendar_row_id', 'row_revision',
                             'shared_posting_identity', 'evidence_ref'),
                            members, {'calendar_row_id': 'uuid',
                                      'shared_posting_identity': 'uuid'}))
    if coverage:
        body.append(_insert('exact_byte_history_coverage_20261010',
                            ('calendar_row_id', 'row_revision', 'complete',
                             'no_images_verified', 'evidence_ref'),
                            coverage, {'calendar_row_id': 'uuid'}))
    if observations:
        body.append(_insert('exact_byte_history_observation_20261010',
                            ('observation_id', 'calendar_row_id', 'row_revision',
                             'canonical_tenant', 'post_date',
                             'shared_posting_identity', 'exact_url', 'sha256',
                             'byte_length', 'observation_kind', 'evidence_ref',
                             'observed_at', 'recorded_at'),
                            observations,
                            {'observation_id': 'uuid', 'calendar_row_id': 'uuid',
                             'post_date': 'date',
                             'shared_posting_identity': 'uuid',
                             'observed_at': 'timestamptz',
                             'recorded_at': 'timestamptz'}))
    return '\n\n'.join(header + body + ['', post_guard, 'commit;']) + '\n'


def emit_readback_sql(*, snapshot_digest, corpus_digest, census_count,
                      coverage_count, observation_count):
    return f"""-- EXACT-BYTE HISTORICAL SEED 20261010 READBACK VERIFICATION (read-only).
-- Run as the trusted operator login AFTER applying the seed and BEFORE any
-- activation. Every check RAISES on mismatch; any exception means DO NOT
-- ACTIVATE and reconcile from evidence. Frozen census: {census_count} rows.
begin read only;

do $$
declare
  v_corpus text;
  v_snapshot text;
  v_complete boolean;
begin
  -- 1. Corpus digest must equal the frozen value.
  select public.exact_byte_corpus_digest_20261010() into v_corpus;
  if v_corpus is distinct from '{corpus_digest}' then
    raise exception 'corpus digest mismatch: expected the frozen value, got %',
      coalesce(v_corpus, 'NULL');
  end if;

  -- 2. Historical snapshot digest must equal the frozen value.
  select public.exact_byte_historical_snapshot_20261010() into v_snapshot;
  if v_snapshot is distinct from '{snapshot_digest}' then
    raise exception 'historical snapshot digest mismatch: got %',
      coalesce(v_snapshot, 'NULL');
  end if;

  -- 3. History completeness must be true.
  select public.exact_byte_history_complete_20261010() into v_complete;
  if v_complete is not true then
    raise exception 'history completeness is not true';
  end if;

  -- 4. Row counts must equal the frozen census exactly.
  if (select count(*) from public.exact_byte_history_coverage_20261010)
       <> {coverage_count}
    or (select count(*) from public.exact_byte_history_observation_20261010)
       <> {observation_count} then
    raise exception 'seed row counts differ from the frozen census '
      '(coverage={coverage_count}, observations={observation_count})';
  end if;

  -- 5. Expect-zero: persisted row revisions must match live row revisions.
  --    Requires the verifying login to hold read on content_calendar.
  if exists(select 1
            from public.exact_byte_history_coverage_20261010 h
            join public.content_calendar c on c.id = h.calendar_row_id
            where h.row_revision is distinct from
                  public.exact_byte_row_revision_20261010(c)) then
    raise exception 'row revision drift between coverage and content_calendar';
  end if;

  -- 6. Expect-zero: coverage outside the historical predicate.
  if exists(select 1
            from public.exact_byte_history_coverage_20261010 h
            left join public.content_calendar c on c.id = h.calendar_row_id
             and (c.status = 'published' or c.published_at is not null
                  or c.late_post_id is not null)
            where c.id is null) then
    raise exception 'coverage rows exist outside the historical predicate';
  end if;

  -- 7. Expect-zero: observations must bind to a same-revision coverage row.
  if exists(select 1
            from public.exact_byte_history_observation_20261010 o
            left join public.exact_byte_history_coverage_20261010 h
             on h.calendar_row_id = o.calendar_row_id
            and h.row_revision = o.row_revision
            where h.calendar_row_id is null) then
    raise exception 'observations exist without a same-revision coverage row';
  end if;

  -- 8. Expect-zero: every observation is a DB-published URL byte observation.
  if exists(select 1
            from public.exact_byte_history_observation_20261010 o
            where o.observation_kind <> 'db_published_url_bytes') then
    raise exception 'observation rows exist that are not db_published_url_bytes';
  end if;

  -- 9. Expect-zero: every tenant timezone is a valid IANA name.
  if exists(select 1
            from public.exact_byte_tenant_20261010 t
            where not exists (select 1 from pg_timezone_names n
                              where n.name = t.posting_timezone)) then
    raise exception 'tenant rows carry an unknown IANA timezone';
  end if;
end $$;

-- The gate is intentionally NOT read here: exact_byte_owner_20261010 has no
-- SELECT privilege on the gate table under the migration. A separately
-- privileged, read-only gate-OFF readback receipt is required BEFORE and
-- AFTER apply (see docs/runbooks/exact-byte-history-seed-20261010.md).

rollback;
"""


# ---------------------------------------------------------------------------
# Top-level run.
# ---------------------------------------------------------------------------

def run(reader, mapping_data, *, fetcher, baseline_min=BASELINE_MIN_ROWS,
        page_size=PAGE_SIZE, now=None):
    """Fresh census + validated mapping + URL observations -> seed artifacts.

    Returns SeedResult. On ANY blocker, seed_sql and readback_sql are None and
    the manifest is marked INCOMPLETE_DO_NOT_APPLY.
    """
    # The 1467-row floor can be raised by callers but never lowered.
    baseline_min = max(int(baseline_min), BASELINE_MIN_ROWS)
    census = run_census(reader, page_size=page_size)
    snapshot_sql = reader.historical_snapshot()
    if not isinstance(snapshot_sql, str) or not SHA256_RE.match(snapshot_sql):
        raise SeedError('SQL historical snapshot digest unavailable')

    blockers = []
    snapshot_local = local_historical_snapshot(census)
    if snapshot_local != snapshot_sql:
        blockers.append('row revision / historical snapshot digest mismatch: '
                        'locally recomputed snapshot differs from SQL authority')

    mapping = load_mapping(mapping_data)
    (b, tenants, targets, proofs, members, coverage,
     observations) = build_seed(census, mapping, fetcher=fetcher,
                                baseline_min=baseline_min, now=now)
    blockers.extend(b)

    # Source of truth: manifest per_account counts EVERY fresh census row by
    # account, independent of mapping validity, media blockers or any other
    # seed assembly outcome. Deterministic: accounts in sorted key order.
    per_account = {a: sum(1 for r in census if r.account == a)
                   for a in sorted({r.account for r in census})}

    # coverage.complete=true is a reviewed census assertion: the reviewer
    # evidence input must be bound to THIS snapshot digest and row count.
    review = mapping['review']
    if review['historical_snapshot_sha256'] != snapshot_sql:
        blockers.append('reviewed census assertion is stale: census_review '
                        'historical_snapshot_sha256 does not equal the current '
                        'historical snapshot digest')
    if review['census_row_count'] != len(census):
        blockers.append('reviewed census assertion count mismatch: census_review '
                        'census_row_count does not equal the fresh census size')

    manifest = {
        'artifact': 'exact_byte_history_seed_20261010',
        'generated_at': _pg_ts(now or datetime.now(timezone.utc)),
        'historical_snapshot_sha256': snapshot_sql,
        'census': {
            'total_rows': len(census),
            'baseline_floor': baseline_min,
            'baseline_note': 'floor from the Oct 10 05:32 UTC observation only; '
                             'this census is fresh per execution, never bundled',
            'per_account': per_account,
            'rows_with_image_url': sum(1 for r in census if r.image_url),
            'rows_with_thumbnail_url': sum(1 for r in census if r.thumbnail_url),
        },
        'reviewed_census_assertion': review,
        'observations': {
            'count': len(observations),
            'occurrences': [
                {'observation_id': o['observation_id'],
                 'calendar_row_id': o['calendar_row_id'],
                 'row_revision': o['row_revision'],
                 'role': o['role'],
                 'exact_url': o['exact_url'],
                 'sha256': o['sha256'],
                 'byte_length': o['byte_length'],
                 'observation_kind': o['observation_kind'],
                 'evidence_ref': o['evidence_ref']} for o in observations],
            'observation_kind': OBSERVATION_KIND,
            'classification': 'DB-published URL byte observation ONLY. Never '
                              'provider_receipt_bytes, never platform_readback_bytes.',
        },
        'counts': {
            'tenants': len(tenants), 'targets': len(targets),
            'sibling_proofs': len(proofs), 'sibling_members': len(members),
            'coverage': len(coverage),
        },
        'blockers': sorted(blockers),
        'remaining_operator_requirements': [
            'Apply the migration; provision a login inheriting '
            'exact_byte_owner_20261010; service credentials cannot seed.',
            'BEFORE apply: a separately privileged, read-only operator must '
            'record a gate-OFF readback receipt; the owner role has no SELECT '
            'privilege on the gate table, so neither the seed nor the '
            'readback SQL can check it.',
            'Apply the seed SQL manually; this tool never executes it. Its '
            'pre/post guards roll back the entire transaction on any '
            'snapshot/digest/count/completeness mismatch.',
            'Run the readback verification SQL; it raises on any mismatch.',
            'AFTER apply: repeat the privileged gate-OFF readback and record '
            'the second receipt.',
            'Only then evaluate the separate operator activation RPC with '
            'deployment/cutover evidence. Gate flags remain OFF.',
        ],
    }

    if blockers:
        manifest['status'] = 'INCOMPLETE_DO_NOT_APPLY'
        manifest['seed_sql_sha256'] = None
        manifest['readback_sql_sha256'] = None
        return SeedResult(manifest=manifest, seed_sql=None, readback_sql=None,
                          blockers=sorted(blockers))

    corpus_digest = local_corpus_digest(tenants, targets, coverage, observations)
    seed_sql = emit_seed_sql(tenants, targets, proofs, members, coverage,
                             observations, snapshot_digest=snapshot_sql,
                             corpus_digest=corpus_digest)
    readback_sql = emit_readback_sql(snapshot_digest=snapshot_sql,
                                     corpus_digest=corpus_digest,
                                     census_count=len(census),
                                     coverage_count=len(coverage),
                                     observation_count=len(observations))
    manifest['status'] = 'COMPLETE_UNAPPLIED'
    manifest['corpus_sha256'] = corpus_digest
    manifest['seed_sql_sha256'] = _sha256_text(seed_sql)
    manifest['readback_sql_sha256'] = _sha256_text(readback_sql)
    manifest['verification'] = {
        'before': 'Confirm migration applied and login inherits '
                  'exact_byte_owner_20261010; record a separately privileged '
                  'read-only gate-OFF receipt; freeze manifest digests.',
        'apply': 'psql the seed SQL as that login; it is one READ COMMITTED '
                 'transaction whose guards roll back everything on mismatch.',
        'readback': 'Run the readback SQL; it raises on any digest/count/'
                    'history mismatch or non-empty expect-zero check. Then '
                    'record the second privileged gate-OFF receipt.',
    }
    return SeedResult(manifest=manifest, seed_sql=seed_sql,
                      readback_sql=readback_sql, blockers=[])
