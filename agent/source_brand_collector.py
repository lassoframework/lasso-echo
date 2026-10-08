"""Server-only mapping and original-byte website collector, OFF by default.

No browser/request authority, name inference, fact approval or provider calls at
import time. ServerMapping records must be installed by the reviewed collector
operator with mapping approval evidence. The legacy name-keyed domain registry
alone is deliberately insufficient. Social bootstrap requires independent
account ID evidence; an Apify response cannot approve its own owner ID.

The existing social capture adapter has no authenticated run/dataset response
identity. Social collection therefore remains held even with an approved ID.
This lane only calls the reviewed original-byte ingest adapter for persistence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .source_brand_ingest import CaptureIngestError, TrustedCaptureIngest, VerifiedMapping
from .website_source_capture import PortalDomainEntry, PortalDomains, WebsiteSourceCapture


@dataclass(frozen=True)
class ServerMapping:
    gym_id: str
    echo_account_key: str
    website_urls: tuple[str, ...]
    domain_evidence: tuple[str, ...]
    approval_receipt: str
    valid_until: str
    instagram_handle: str | None = None
    instagram_owner_id: str | None = None
    owner_id_evidence: str | None = None
    owner_id_evidence_source: str | None = None


# Read-only source receipts supplied by the lead, 2026-10-08. This is a DRAFT
# mapping, not an approval. No approved default mappings are installed.
SWIFT_RIVER_DRAFT = {
    'gym_id': 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b',
    'echo_account_key': 'swiftrivercrossfite5c9db',
    'website_urls': ('https://swiftrivercrossfit.com/',),
    'domain_evidence': ('https://swiftrivercrossfit.com/',
                        'https://www.crossfit.com/gym/13058/swift-river-crossfit'),
    'instagram_handle': 'swiftrivercrossfit',
    'instagram_owner_id': None,
}


def _fail(code):
    raise CaptureIngestError(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        _fail('mapping_timestamp_invalid')


def _canonical_gym(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        _fail('mapping_gym_invalid')


def _website_url(value):
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.port not in (None, 443)
                or any(c.isspace() for c in value)):
            raise ValueError()
        return p
    except (TypeError, ValueError):
        _fail('mapping_domain_invalid')


class PortalMappingResolver:
    """Read fresh authoritative portal rows on every call, with no writes.

    read_rows(table, params) is trusted server code, authenticated to the portal;
    it must return the complete result or raise. Never pass browser request
    records here. Exact UUID-scoped uniqueness is checked before any capture.
    A deterministic revision binds both operator authority and live mappings.
    """
    def __init__(self, *, read_rows, approved_mappings=(), now=None):
        if not callable(read_rows):
            _fail('mapping_reader_required')
        self._read = read_rows
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._approved = {}
        for entry in approved_mappings:
            if not isinstance(entry, ServerMapping):
                _fail('server_mapping_required')
            _canonical_gym(entry.gym_id)
            if entry.gym_id in self._approved:
                _fail('mapping_duplicate_gym')
            # Freeze nested input authority; caller mutations cannot add URLs.
            self._approved[entry.gym_id] = ServerMapping(
                entry.gym_id, entry.echo_account_key, tuple(entry.website_urls),
                tuple(entry.domain_evidence), entry.approval_receipt,
                entry.valid_until, entry.instagram_handle, entry.instagram_owner_id,
                entry.owner_id_evidence, entry.owner_id_evidence_source)

    def __call__(self, gym_id):
        _canonical_gym(gym_id)
        entry = self._approved.get(gym_id)
        if entry is None:
            _fail('exact_tenant_domain_mapping_missing')
        if (not entry.approval_receipt or not entry.domain_evidence
                or not entry.echo_account_key or not entry.website_urls):
            _fail('mapping_approval_missing')
        if _timestamp(entry.valid_until) <= self._now():
            _fail('mapping_approval_expired')
        for url in entry.website_urls:
            _website_url(url)
        tokens = self._read('echo_intake_tokens', {
            'gym_id': 'eq.' + gym_id, 'select': 'gym_id,echo_account_key'})
        if tokens != [{'gym_id': gym_id, 'echo_account_key': entry.echo_account_key}]:
            _fail('current_tenant_mapping_mismatch')
        connections = self._read('echo_social_connections', {
            'gym_id': 'eq.' + gym_id, 'platform': 'eq.instagram',
            'select': 'gym_id,platform,state,handle,last_verified_at'})
        if not isinstance(connections, list) or any(not isinstance(row, dict) for row in connections):
            _fail('social_mapping_unavailable')
        if len(connections) > 1:
            _fail('social_mapping_ambiguous')
        social = ()
        provider_id = None
        if entry.instagram_handle:
            if not re.fullmatch(r'[a-z0-9._]{1,30}', entry.instagram_handle):
                _fail('social_handle_invalid')
            if (len(connections) != 1 or connections[0].get('gym_id') != gym_id
                    or connections[0].get('platform') != 'instagram'
                    or connections[0].get('state') != 'connected'
                    or connections[0].get('handle') != entry.instagram_handle):
                _fail('current_social_mapping_mismatch')
            verified_at = _timestamp(connections[0].get('last_verified_at'))
            if verified_at > self._now():
                _fail('social_mapping_timestamp_invalid')
            if verified_at < self._now() - timedelta(days=7):
                _fail('social_mapping_verification_expired')
            if entry.instagram_owner_id:
                if (not re.fullmatch(r'[0-9]+', entry.instagram_owner_id)
                        or not entry.owner_id_evidence
                        or entry.owner_id_evidence_source not in
                        ('meta_authenticated_account', 'internal_verified_account_approval')):
                    _fail('independent_social_id_evidence_missing')
                social = ('https://www.instagram.com/' + entry.instagram_handle + '/',)
                provider_id = entry.instagram_owner_id
        elif connections:
            # An undeclared connected account is a mapping change, never an
            # opportunity to infer a handle from arbitrary response data.
            _fail('unapproved_social_mapping')
        authority = dict(entry.__dict__)
        evidence = {'authority': authority, 'portal_token': tokens[0],
                    'portal_instagram': connections}
        revision = 'server-mapping:sha256:' + hashlib.sha256(_json(evidence).encode()).hexdigest()
        return VerifiedMapping(gym_id, entry.echo_account_key, revision,
                               json.loads(_json(evidence)), entry.website_urls,
                               social, provider_id)


class TrustedSourceCollector:
    """Owns capture execution and an opaque, one-use in-process receipt.

    Capture modules and HTTP clients are trusted code dependencies, not request
    inputs. Receipts are never accepted from an external API. A provenance dict
    or identity_verified label cannot authenticate bytes. The capability below
    is created only after this collector executes the reviewed HTTPS transport.
    """
    def __init__(self, *, resolver, environ=None, ingest_http=None,
                 website_factory=WebsiteSourceCapture,
                 clock=None, sleep=None, min_host_delay=1.0):
        self._env = os.environ if environ is None else environ
        self._resolve = resolver
        self._website_factory = website_factory
        self._receipts = {}
        # Cross-call per-host rate gate. collect_website builds a fresh
        # capture instance every call, so instance-level state would let
        # repeated calls bypass the per-host minimum delay. This state lives
        # on the collector (shared across its calls, keyed by host, guarded
        # by one lock). Hosts are global, so keying by host can only ever
        # ADD delay across tenants, never let one tenant's traffic bypass
        # or inherit another's allowance. clock/sleep are injectable.
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._min_host_delay = float(min_host_delay)
        self._rate_lock = threading.Lock()
        self._rate_last = {}
        self._ingest = TrustedCaptureIngest(resolve_mapping=resolver,
            authenticate_response=self.authenticate_response,
            http=ingest_http, environ=self._env)

    def _rate_gate(self, host):
        with self._rate_lock:
            prev = self._rate_last.get(host)
            if prev is not None:
                owed = self._min_host_delay - (self._clock() - prev)
                if owed > 0:
                    self._sleep(owed)
            self._rate_last[host] = self._clock()

    def authenticate_response(self, capture, raw_bytes, receipt, mapping):
        # Object identity is the authority, never fields supplied by a caller.
        registered = self._receipts.get(id(receipt))
        if registered is None or registered[0] is not receipt:
            return False
        return (registered[1] == _json(capture)
                and registered[2] == raw_bytes
                and registered[3] == mapping
                and self._resolve(mapping.gym_id) == mapping)

    def collect_website(self, gym_id, source_url, *, source_kind='website'):
        if (self._env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true'
                or self._env.get('ECHO_SOURCE_CAPTURE_INGEST_ENABLED') != 'true'):
            _fail('source_collector_disabled')
        if source_kind not in ('website', 'website_asset'):
            _fail('invalid_source_kind')
        self._ingest._config()
        mapping = self._resolve(gym_id)
        if source_url not in mapping.website_response_urls:
            _fail('website_mapping_mismatch')
        entries = [PortalDomainEntry(gym_id, mapping.echo_account_key,
                   _website_url(url).hostname, source_kind,
                   path_prefixes=(_website_url(url).path or '/',),
                   mapping_revision=mapping.mapping_revision,
                   mapping_evidence=mapping.mapping_evidence)
                   for url in mapping.website_response_urls]
        result = self._website_factory(PortalDomains(entries),
                                       rate_gate=self._rate_gate).capture(source_url)
        # Redirects can only persist explicitly approved EXACT response URLs.
        if (result.gym_id != gym_id or result.echo_account_key != mapping.echo_account_key
                or result.source_url not in mapping.website_response_urls
                or result.mapping_revision != mapping.mapping_revision
                or result.mapping_evidence != mapping.mapping_evidence
                or result.source_kind != source_kind or not 200 <= result.status < 300
                or type(result.raw_bytes) is not bytes
                or result.bytes_sha256 != hashlib.sha256(result.raw_bytes).hexdigest()
                or self._resolve(gym_id) != mapping):
            _fail('transport_capture_binding_mismatch')
        metadata = dict(gym_id=gym_id, echo_account_key=mapping.echo_account_key,
            source_kind=source_kind, source_url=result.source_url,
            source_locator=None, capture_provider='direct', provider_response_id=None,
            provider_account_id=None, source_revision='response-sha256:' + result.bytes_sha256,
            mapping_revision=mapping.mapping_revision, mapping_evidence=mapping.mapping_evidence,
            fetched_at=result.fetched_at)
        receipt = object()
        self._receipts[id(receipt)] = (receipt, _json(metadata), result.raw_bytes, mapping)
        try:
            return self._ingest.ingest(metadata, result.raw_bytes, transport_receipt=receipt,
                expected_length=len(result.raw_bytes), expected_sha256=result.bytes_sha256)
        finally:
            self._receipts.pop(id(receipt), None)

    def collect_social(self, gym_id):
        if self._env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true':
            _fail('source_collector_disabled')
        mapping = self._resolve(gym_id)
        if not mapping.provider_account_id:
            _fail('independent_social_id_evidence_missing')
        _fail('authenticated_provider_response_identity_missing')

class CollectorPortalReader:
    """Dedicated server credential, read-only transport for resolver lookups.

    Uses the collector's existing config and credential; no new credentials,
    provider calls, approval actions or endpoint are provisioned here.
    """
    def __init__(self, *, environ=None, http=None):
        self._env = os.environ if environ is None else environ
        self._http = http

    def __call__(self, table, params):
        if table not in ('echo_intake_tokens', 'echo_social_connections'):
            _fail('mapping_table_not_allowed')
        if self._env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true':
            _fail('source_collector_disabled')
        base = self._env.get('ECHO_SOURCE_CAPTURE_SUPABASE_URL', '')
        key = self._env.get('ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY', '')
        parsed = _website_url(base)
        if parsed.path not in ('', '/') or not key:
            _fail('collector_service_config_missing')
        http = self._http
        if http is None:
            import requests
            http = requests
        try:
            response = http.get(base.rstrip('/') + '/rest/v1/' + table,
                headers={'apikey': key, 'Authorization': 'Bearer ' + key},
                params=params, timeout=15, allow_redirects=False)
            if response.status_code != 200:
                _fail('mapping_read_unavailable')
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                _fail('mapping_read_invalid')
            return rows
        except Exception:
            _fail('mapping_read_unavailable')


def build_collector(*, approved_mappings=(), environ=None, http=None):
    """Reviewed server composition root. Empty authority/default flags hold.

    Only server startup code supplies approved mappings. Never deserialize a
    request body into this argument. No scheduled runner or live activation.
    """
    reader = CollectorPortalReader(environ=environ, http=http)
    resolver = PortalMappingResolver(read_rows=reader, approved_mappings=approved_mappings)
    return TrustedSourceCollector(resolver=resolver, environ=environ, ingest_http=http)
