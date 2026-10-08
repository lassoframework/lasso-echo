"""Server-only mapping and original-byte website collector, OFF by default.

No browser/request authority, name inference, fact approval or provider calls at
import time. ServerMapping records must be installed by the reviewed collector
operator with mapping approval evidence. The legacy name-keyed domain registry
alone is deliberately insufficient. Social bootstrap requires independent
account ID evidence; an Apify response cannot approve its own owner ID.

Social execution uses the reviewed exact run/dataset adapter and never derives
account authority from scraped items. Durable private receipts authenticate
retained captures after process restart; no receipt is accepted from a request.
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
    try:
        raise CaptureIngestError(code) from None
    except CaptureIngestError as error:
        error.__context__ = None
        raise


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
    def __init__(self, *, read_rows, approved_mappings=(), now=None,
                 social_identity_reader=None):
        if not callable(read_rows):
            _fail('mapping_reader_required')
        self._read = read_rows
        self._social_identity = social_identity_reader
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
        provider_evidence = None
        if self._social_identity is not None:
            provider_evidence = self._social_identity(gym_id, entry.echo_account_key)
            if (not isinstance(provider_evidence, dict)
                    or provider_evidence.get('gym_id') != gym_id
                    or provider_evidence.get('echo_account_key') != entry.echo_account_key
                    or provider_evidence.get('source') != 'zernio_authenticated_accounts'
                    or type(provider_evidence.get('connected')) is not bool):
                _fail('authenticated_social_status_required')
            if provider_evidence['connected']:
                if (not entry.instagram_handle
                        or provider_evidence['handle'] != entry.instagram_handle
                        or len(connections) != 1
                        or connections[0].get('state') != 'connected'
                        or connections[0].get('handle') != entry.instagram_handle):
                    _fail('current_social_mapping_mismatch')
                provider_id = provider_evidence['platform_user_id']
                if not isinstance(provider_id, str) or not re.fullmatch(r'[0-9]+', provider_id):
                    _fail('independent_social_id_evidence_missing')
                if entry.instagram_owner_id and entry.instagram_owner_id != provider_id:
                    _fail('current_social_owner_mismatch')
                social = ('https://www.instagram.com/' + entry.instagram_handle + '/',)
            elif any(row.get('state') == 'connected' for row in connections):
                _fail('current_social_mapping_mismatch')
        if entry.instagram_handle and (provider_evidence is None or provider_evidence['connected']):
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
            if entry.instagram_owner_id and provider_evidence is None:
                if (not re.fullmatch(r'[0-9]+', entry.instagram_owner_id)
                        or not entry.owner_id_evidence
                        or entry.owner_id_evidence_source not in
                        ('meta_authenticated_account', 'internal_verified_account_approval')):
                    _fail('independent_social_id_evidence_missing')
                social = ('https://www.instagram.com/' + entry.instagram_handle + '/',)
                provider_id = entry.instagram_owner_id
        elif connections and provider_evidence is None and any(
                row.get('state') == 'connected' for row in connections):
            # An undeclared connected account is a mapping change, never an
            # opportunity to infer a handle from arbitrary response data.
            _fail('unapproved_social_mapping')
        authority = dict(entry.__dict__)
        if any(row.get('gym_id') != gym_id or row.get('platform') != 'instagram'
               or row.get('state') not in ('connected', 'not_connected', 'disconnected', 'expired')
               for row in connections):
            _fail('current_social_mapping_mismatch')
        evidence = {'authority': authority, 'portal_token': tokens[0],
                    'portal_instagram': connections}
        if provider_evidence is not None:
            evidence['provider_instagram'] = provider_evidence
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
                 clock=None, sleep=None, min_host_delay=1.0, receipt_journal=None,
                 apify_client=None, apify_journal=None):
        self._env = os.environ if environ is None else environ
        self._resolve = resolver
        self._website_factory = website_factory
        self._receipts = {}
        self._journal = receipt_journal
        self._apify_client = apify_client
        self._apify_journal = apify_journal
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

    def _persist(self, metadata, raw_bytes, mapping, *, request_id=None, proof=None):
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if self._journal is not None:
            if not request_id:
                _fail('durable_capture_request_required')
            self._journal.prepare(request_id, metadata, raw_bytes, proof or {})
        receipt = object()
        self._receipts[id(receipt)] = (receipt, _json(metadata), raw_bytes, mapping)
        try:
            stored = self._ingest.ingest(metadata, raw_bytes, transport_receipt=receipt,
                expected_length=len(raw_bytes), expected_sha256=digest)
            if self._journal is not None:
                self._journal.confirm(request_id, stored)
            return stored
        finally:
            self._receipts.pop(id(receipt), None)

    def authenticate_capture(self, capture, raw_bytes, mapping):
        try:
            return (self._journal is not None and self._resolve(mapping.gym_id) == mapping
                    and self._journal.authenticate_capture(capture, raw_bytes, mapping))
        except Exception:
            return False

    def _replay(self, request_id, binding, mapping):
        if self._journal is None:
            return None
        prepared = self._journal.claim(request_id, binding)
        if prepared is None:
            return None
        metadata, raw, proof = prepared
        return self._persist(metadata, raw, mapping, request_id=request_id, proof=proof)

    def collect_website(self, gym_id, source_url, *, source_kind='website', request_id=None):
        if (self._env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true'
                or self._env.get('ECHO_SOURCE_CAPTURE_INGEST_ENABLED') != 'true'):
            _fail('source_collector_disabled')
        if source_kind not in ('website', 'website_asset'):
            _fail('invalid_source_kind')
        self._ingest._config()
        mapping = self._resolve(gym_id)
        if source_url not in mapping.website_response_urls:
            _fail('website_mapping_mismatch')
        replay = self._replay(request_id, {'mapping': mapping.__dict__,
            'source_kind': source_kind, 'source_url': source_url}, mapping)
        if replay is not None:
            return replay
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
        return self._persist(metadata, result.raw_bytes, mapping, request_id=request_id,
            proof={'transport': 'reviewed_website_https', 'status': result.status,
                   'response_url': result.source_url})

    def collect_social(self, gym_id, *, request_id=None):
        if self._env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true':
            _fail('source_collector_disabled')
        mapping = self._resolve(gym_id)
        if not mapping.provider_account_id:
            _fail('independent_social_id_evidence_missing')
        if (self._env.get('ECHO_SOURCE_CAPTURE_INGEST_ENABLED') != 'true'
                or self._env.get('ECHO_SOURCE_SOCIAL_RUN_CAPTURE_ENABLED') != 'true'
                or self._journal is None or self._apify_journal is None):
            _fail('authenticated_provider_response_identity_missing')
        self._ingest._config()
        if not request_id:
            _fail('durable_capture_request_required')
        proof = mapping.mapping_evidence.get('provider_instagram', {})
        if (proof.get('source') != 'zernio_authenticated_accounts'
                or proof.get('connected') is not True
                or proof.get('platform_user_id') != mapping.provider_account_id):
            _fail('independent_social_id_evidence_missing')
        actor = self._env.get('ECHO_SOURCE_APIFY_ACTOR_ID', '')
        try:
            charge = float(self._env.get('ECHO_SOURCE_APIFY_MAX_CHARGE_USD', ''))
        except (TypeError, ValueError):
            _fail('social_capture_limits_missing')
        binding = {'mapping': mapping.__dict__, 'source_kind': 'social',
                   'actor': actor, 'max_total_charge_usd': charge,
                   'lookback_days': 90, 'results_limit': 500}
        replay = self._replay(request_id, binding, mapping)
        if replay is not None:
            return replay
        from .apify_run_capture import capture_social_source_run
        result = capture_social_source_run(mapped_handle=proof['handle'],
            mapped_provider_account_id=mapping.provider_account_id,
            mapped_source_locator=mapping.social_locators[0], gym_id=gym_id,
            echo_account_key=mapping.echo_account_key,
            mapping_revision=mapping.mapping_revision, mapping_evidence=mapping.mapping_evidence,
            source_revision='mapped-social:' + mapping.mapping_revision,
            request_id=request_id, expected_actor_id=actor, journal=self._apify_journal,
            max_total_charge_usd=charge, enabled=True, client=self._apify_client)
        if not result.ok:
            _fail('social_run_capture_held')
        p = result.provenance
        if (type(result.raw_bytes) is not bytes
                or result.sha256 != hashlib.sha256(result.raw_bytes).hexdigest()
                or p.get('gym_id') != gym_id or p.get('echo_account_key') != mapping.echo_account_key
                or p.get('source_locator') != mapping.social_locators[0]
                or p.get('provider_run_actor_id') != actor or p.get('run_status') != 'SUCCEEDED'
                or self._resolve(gym_id) != mapping or p.get('mapping_revision') != mapping.mapping_revision
                or p.get('provider_account_id') != mapping.provider_account_id
                or p.get('mapping_evidence') != mapping.mapping_evidence):
            _fail('transport_capture_binding_mismatch')
        # Ingest deliberately rejects every URL query. Persist the canonical
        # endpoint for this exact dataset; the private receipt retains the actual
        # executed URL and bounded query, never a latest-run or reconstructed body.
        source_url = 'https://api.apify.com/v2/datasets/' + p['provider_dataset_id'] + '/items'
        metadata = dict(gym_id=gym_id, echo_account_key=mapping.echo_account_key,
            source_kind='social', source_url=source_url, source_locator=mapping.social_locators[0],
            capture_provider='apify', provider_response_id=p['provider_response_id'],
            provider_account_id=mapping.provider_account_id,
            source_revision=p['provider_response_id'] + ':sha256:' + result.sha256,
            mapping_revision=mapping.mapping_revision, mapping_evidence=mapping.mapping_evidence,
            fetched_at=p['fetched_at'])
        return self._persist(metadata, result.raw_bytes, mapping, request_id=request_id,
                             proof=p)

class CollectorPortalReader:
    """Dedicated server credential, read-only transport for resolver lookups.

    Uses the collector's existing config and credential; no new credentials,
    provider calls, approval actions or endpoint are provisioned here.
    """
    def __init__(self, *, environ=None, http=None):
        self._env = os.environ if environ is None else environ
        self._http = http

    def __call__(self, table, params):
        if table not in ('echo_intake_tokens', 'echo_social_connections', 'echo_gym_settings'):
            _fail('mapping_table_not_allowed')
        # This service credential may resolve one approved gym, never enumerate
        # the shared portal token table or the wider LASSO client universe.
        if (not isinstance(params, dict) or set(params) - {'gym_id', 'platform', 'select'}
                or not isinstance(params.get('gym_id'), str)
                or not params['gym_id'].startswith('eq.')
                or (params.get('platform') is not None
                    and params['platform'] != 'eq.instagram')):
            _fail('mapping_scope_invalid')
        _canonical_gym(params['gym_id'][3:])
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


    def attest_provider(self, status, request_id):
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
            response = http.post(base.rstrip('/') + '/rest/v1/rpc/echo_source_brand_attest_provider',
                headers={'apikey': key, 'Authorization': 'Bearer ' + key},
                json={'p_gym': status['gym_id'], 'p_key': status['echo_account_key'],
                      'p_status': status, 'p_request': request_id},
                timeout=15, allow_redirects=False)
            if response.status_code != 200:
                _fail('provider_attestation_unconfirmed')
            row = response.json()
            if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] < 1
                    or row.get('gym_id') != status['gym_id']
                    or row.get('echo_account_key') != status['echo_account_key']
                    or row.get('attestation') != status or row.get('request_id') != request_id
                    or _timestamp(row.get('created_at')) < _timestamp(status['observed_at'])):
                _fail('provider_attestation_readback_mismatch')
            return row
        except CaptureIngestError:
            raise
        except Exception:
            _fail('provider_attestation_unconfirmed')


def _strict_json(raw):
    """Bounded strict JSON: no duplicate keys, no non-finite constants."""
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError()
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _zernio_health_candidate(client, key, profile, clock):
    """Independent profile ownership/status authority: GET /v1/accounts/health.

    The account list rows omit profileId, so ownership of an account for this
    exact stored profile can only come from the health endpoint. A real
    profile owns one row per platform (e.g. Facebook, Google Business,
    Instagram), so selection filters the exact profile, then requires exactly
    one Instagram row; sibling platform rows are not ambiguity. Duplicate or
    conflicting Instagram identities/IDs, conflicting profile/status data,
    partial or malformed data fails closed. Strict field validation applies
    only to rows whose readable profile identity equals the requested profile;
    rows for other profiles cannot hold this gym, while a row with absent or
    unreadable profile identity could still refer to it and fails closed.
    Reuse of this profile's candidate account ID by any row with a different
    readable profile identity is conflicting ownership evidence and fails
    closed.
    Returns (health_row_or_None,
    observed_at, raw_bytes): None means this profile has no Instagram
    candidate (missing or disconnected — never proof of disconnection).
    Handle is never used to infer ownership.
    """
    response = client.get('https://api.zernio.com/v1/accounts/health',
        headers={'Authorization': 'Bearer ' + key}, timeout=30, allow_redirects=False)
    observed_at = clock().astimezone(timezone.utc).isoformat()
    raw = response.content
    if response.status_code != 200 or type(raw) is not bytes or not 1 <= len(raw) <= 2_000_000:
        _fail('authenticated_social_status_unavailable')
    data = _strict_json(raw)
    rows = data.get('accounts') if isinstance(data, dict) else None
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or any(name in data for name in ('pagination', 'nextCursor', 'hasMore', 'next'))):
        _fail('authenticated_social_status_incomplete')
    # Strict validation is scoped to rows for the requested profile. A row
    # whose readable profile identity differs can hold unrelated malformed
    # non-identity fields without holding this gym; a row whose profile
    # identity is absent or unreadable could refer to this profile and holds.
    candidates = []
    for row in rows:
        if (not isinstance(row.get('profileId'), str) or not row['profileId']):
            _fail('authenticated_social_status_incomplete')
        if row['profileId'] == profile:
            candidates.append(row)
    seen_ids = set()
    for row in candidates:
        if (not isinstance(row.get('accountId'), str) or not row['accountId']
                or not isinstance(row.get('platform'), str)
                or not re.fullmatch(r'[a-z][a-z0-9_]*', row['platform'])
                or not isinstance(row.get('username'), str) or not row['username']
                or not isinstance(row.get('status'), str)
                or type(row.get('tokenValid')) is not bool
                or type(row.get('needsReconnect')) is not bool
                or type(row.get('canPost')) is not bool
                or row['accountId'] in seen_ids):
            _fail('authenticated_social_status_incomplete')
        seen_ids.add(row['accountId'])
    # Cross-profile reuse of a candidate's account ID is conflicting ownership
    # evidence: the same target account cannot be owned by this profile and
    # another readable profile at once. Fail closed even if the other row's
    # non-identity fields are malformed; a non-string accountId on another
    # profile's row cannot equal a validated candidate ID.
    candidate_ids = {row['accountId'] for row in candidates}
    for row in rows:
        if (row['profileId'] != profile
                and isinstance(row.get('accountId'), str)
                and row['accountId'] in candidate_ids):
            _fail('authenticated_social_identity_ambiguous_or_missing')
    instagram = [row for row in candidates if row['platform'] == 'instagram']
    if len(instagram) > 1:
        # Duplicate or conflicting Instagram rows for this exact profile hold.
        _fail('authenticated_social_identity_ambiguous_or_missing')
    if not instagram:
        return None, observed_at, raw
    row = instagram[0]
    if not re.fullmatch(r'[a-z0-9._]{1,30}', row['username']):
        _fail('authenticated_social_status_incomplete')
    if (row['status'] != 'healthy'
            or row['tokenValid'] is not True or row['needsReconnect'] is not False
            or row['canPost'] is not True):
        return None, observed_at, raw
    return row, observed_at, raw


def _zernio_account_match(client, key, profile, health, clock):
    """Pair the health authority row with the profile-scoped account list.

    Matches exactly one account row by stable provider account ID
    (health.accountId -> account _id), never by handle alone, then verifies
    platform, handle and the numeric platform owner ID. Returns
    (platform_user_id, raw_bytes). Ownership is never inferred from the handle.
    """
    response = client.get('https://api.zernio.com/v1/accounts',
        params={'profileId': profile}, headers={'Authorization': 'Bearer ' + key},
        timeout=30, allow_redirects=False)
    raw = response.content
    if response.status_code != 200 or type(raw) is not bytes or not 1 <= len(raw) <= 2_000_000:
        _fail('authenticated_social_status_unavailable')
    data = _strict_json(raw)
    accounts = data.get('accounts') if isinstance(data, dict) else None
    if (not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts)
            or any(name in data for name in ('pagination', 'nextCursor', 'hasMore', 'next'))):
        _fail('authenticated_social_status_incomplete')
    seen = set()
    for a in accounts:
        if (not isinstance(a.get('platform'), str)
                or not re.fullmatch(r'[a-z][a-z0-9_]*', a['platform'])
                or not isinstance(a.get('_id'), str) or not a['_id']):
            _fail('authenticated_social_status_incomplete')
        if a['_id'] in seen:
            _fail('authenticated_social_identity_ambiguous_or_missing')
        seen.add(a['_id'])
        # Account lists can omit profileId or populate it with a profile
        # object. A present scalar or populated _id must exactly match the
        # stored profile; the display name is never ownership evidence.
        if 'profileId' in a:
            account_profile = a['profileId']
            if isinstance(account_profile, dict):
                account_profile = account_profile.get('_id')
            if not isinstance(account_profile, str) or account_profile != profile:
                _fail('authenticated_social_profile_mismatch')
    matches = [a for a in accounts if a['_id'] == health['accountId']]
    if len(matches) != 1 or matches[0].get('platform') != 'instagram':
        _fail('authenticated_social_profile_mismatch')
    row = matches[0]
    metadata = row.get('metadata', {})
    if not isinstance(metadata, dict):
        _fail('independent_social_id_evidence_missing')
    profile_data = metadata.get('profileData', {})
    if not isinstance(profile_data, dict):
        _fail('independent_social_id_evidence_missing')
    ids = [value for value in (row.get('platformUserId'), metadata.get('platformUserId'))
           if value is not None]
    handles = [value for value in (row.get('username'), profile_data.get('username'))
               if value is not None]
    if (not ids or any(type(value) is not str or not re.fullmatch(r'[0-9]+', value) for value in ids)
            or len(set(ids)) != 1 or row['_id'] == ids[0]
            or not handles or any(type(value) is not str
                                  or not re.fullmatch(r'[a-z0-9._]{1,30}', value) for value in handles)
            or len(set(handles)) != 1 or handles[0] != health['username']):
        _fail('independent_social_id_evidence_missing')
    return ids[0], raw


def _zernio_identity_response_sha256(health_raw, accounts_raw):
    """Bind both exact responses in a versioned, domain-separated receipt.

    SHA256 input is the ASCII domain below (including its terminating NUL),
    then, in order, each fixed endpoint label plus NUL, an unsigned 8-byte
    big-endian response length, and the original response bytes. Labels bind
    response roles; lengths prevent ambiguous concatenation. JSON is never
    reserialized. Only complete paired identity evidence uses this composite;
    a partial runtime receipt retains its single health-response digest.
    """
    digest = hashlib.sha256(b'echo:zernio:identity-responses:v1\0')
    for endpoint, raw in ((b'/v1/accounts/health', health_raw),
                          (b'/v1/accounts', accounts_raw)):
        digest.update(endpoint + b'\0')
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    return digest.hexdigest()


class AuthenticatedZernioIdentityReader:
    """Exact UUID→stored profile→health authority→paired account list→receipt.

    Uses existing ZERNIO_API_KEY and the dedicated capture service credential.
    Never searches profile names, creates a profile or uses a scraped item ID.
    GET /v1/accounts/health is the independent profile ownership/status
    authority; the profile-scoped account list only supplies the numeric
    platform owner ID and metadata for the exact health-matched account ID.
    A missing profile/key, partial or malformed list, unavailable transport,
    ambiguous or conflicting health rows, missing numeric platformUserId or a
    mismatched handle holds.
    Every call writes/readbacks a fresh default-off service RPC attestation.
    """
    def __init__(self, *, read_rows, environ=None, http=None, now=None):
        self._read = read_rows
        self._env = os.environ if environ is None else environ
        self._http = http
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __call__(self, gym_id, echo_account_key):
        _canonical_gym(gym_id)
        if self._env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true':
            _fail('source_collector_disabled')
        def report(profile):
            revision = 'zernio-profile:sha256:' + hashlib.sha256(_json({
                'gym_id': gym_id, 'echo_account_key': echo_account_key,
                'profile_id': profile}).encode()).hexdigest()
            return dict(provider='zernio', source='zernio_authenticated_accounts',
                gym_id=gym_id, echo_account_key=echo_account_key, profile_id=profile,
                mapping_revision=revision, lookup_status='unavailable', authenticated=False,
                observed_at=self._now().isoformat(), response_sha256=None, instagram=None)
        status = report(None)
        try:
            rows = self._read('echo_gym_settings', {'gym_id': 'eq.' + gym_id,
                              'select': 'gym_id,zernio_profile_id'})
            if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                    or rows[0].get('gym_id') != gym_id
                    or not isinstance(rows[0].get('zernio_profile_id'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', rows[0]['zernio_profile_id'])):
                _fail('exact_zernio_profile_mapping_required')
            profile = rows[0]['zernio_profile_id']
        except Exception:
            self._read.attest_provider(status, str(uuid.uuid4()))
            _fail('exact_zernio_profile_mapping_required')
        status = report(profile)
        revision = status['mapping_revision']
        try:
            key = self._env.get('ZERNIO_API_KEY', '')
            if not isinstance(key, str) or not key or any(c.isspace() for c in key):
                _fail('zernio_account_credential_required')
            # Fixed reviewed endpoint: environment overrides cannot leak the key.
            client = self._http
            if client is None:
                import requests
                client = requests
            # The health endpoint is the independent profile ownership/status
            # authority; the account list only supplies the numeric owner ID
            # and metadata for the exact health-matched account.
            health, observed_at, health_raw = _zernio_health_candidate(
                client, key, profile, self._now)
            status.update(authenticated=True,
                          response_sha256=hashlib.sha256(health_raw).hexdigest(),
                          lookup_status='partial')
            if health is None:
                # Missing or unhealthy evidence cannot establish a complete
                # negative identity that would permit website-only capture.
                _fail('authenticated_social_identity_ambiguous_or_missing')
            platform_user_id, accounts_raw = _zernio_account_match(
                client, key, profile, health, self._now)
            identity = dict(connected=True, account_id=health['accountId'],
                            platform_user_id=platform_user_id,
                            handle=health['username'])
            if self._now() - _timestamp(status['observed_at']) > timedelta(minutes=15):
                _fail('authenticated_social_status_expired')
            status.update(lookup_status='complete', instagram=identity,
                          response_sha256=_zernio_identity_response_sha256(
                              health_raw, accounts_raw))
        except Exception:
            # Persist the negative/partial lookup as a hold, never as proof of
            # disconnection. Provider exceptions are never propagated or logged.
            self._read.attest_provider(status, str(uuid.uuid4()))
            _fail('authenticated_social_status_unavailable_or_incomplete')
        self._read.attest_provider(status, str(uuid.uuid4()))
        return dict(gym_id=gym_id, echo_account_key=echo_account_key,
            profile_id=profile, provider_mapping_revision=revision,
            source='zernio_authenticated_accounts', **identity)


def read_zernio_identity_readonly(gym_id, echo_account_key, *, read_rows,
                                  environ=None, http=None, now=None):
    """Read exact stored-profile Instagram identity without writing receipts.

    This operator diagnostic deliberately does not use
    ``AuthenticatedZernioIdentityReader``: that runtime reader persists a portal
    attestation. This function only performs gym-scoped portal GETs and two fixed
    Zernio GETs: the health endpoint is the independent profile
    ownership/status authority, and the profile-scoped account list supplies
    the numeric platform owner ID and metadata for the exact health-matched
    account. It returns a minimal identity receipt.
    """
    _canonical_gym(gym_id)
    if (not isinstance(echo_account_key, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', echo_account_key)
            or not callable(read_rows)):
        _fail('exact_tenant_mapping_required')
    env = os.environ if environ is None else environ
    clock = now or (lambda: datetime.now(timezone.utc))
    tokens = read_rows('echo_intake_tokens', {
        'gym_id': 'eq.' + gym_id, 'select': 'gym_id,echo_account_key'})
    if tokens != [{'gym_id': gym_id, 'echo_account_key': echo_account_key}]:
        _fail('current_tenant_mapping_mismatch')
    settings = read_rows('echo_gym_settings', {
        'gym_id': 'eq.' + gym_id, 'select': 'gym_id,zernio_profile_id'})
    if (not isinstance(settings, list) or len(settings) != 1
            or not isinstance(settings[0], dict)
            or settings[0].get('gym_id') != gym_id
            or not isinstance(settings[0].get('zernio_profile_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', settings[0]['zernio_profile_id'])):
        _fail('exact_zernio_profile_mapping_required')
    profile = settings[0]['zernio_profile_id']
    key = env.get('ZERNIO_API_KEY', '')
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        _fail('zernio_account_credential_required')
    client = http
    if client is None:
        import requests
        client = requests
    try:
        health, observed_at, health_raw = _zernio_health_candidate(client, key, profile, clock)
        if health is None:
            # Missing or non-healthy profile candidate: fail closed. This is a
            # hold, never proof of disconnection.
            _fail('authenticated_social_identity_ambiguous_or_missing')
        platform_user_id, accounts_raw = _zernio_account_match(client, key, profile, health, clock)
        return {'profile_id': profile, 'account_id': health['accountId'],
                'platform_user_id': platform_user_id, 'handle': health['username'],
                'observed_at': observed_at,
                'response_sha256': _zernio_identity_response_sha256(health_raw, accounts_raw)}
    except CaptureIngestError:
        raise
    except Exception:
        _fail('authenticated_social_status_unavailable_or_incomplete')


def _readonly_identity_cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Read exact-profile Zernio Instagram identity (no writes).')
    parser.add_argument('--gym', required=True)
    parser.add_argument('--account-key', required=True)
    args = parser.parse_args(argv)
    try:
        env = os.environ
        reader = CollectorPortalReader(environ=env)
        result = read_zernio_identity_readonly(args.gym, args.account_key,
                                               read_rows=reader, environ=env)
        print(_json(result))
        return 0
    except CaptureIngestError as error:
        print(_json({'error': str(error)}))
        return 2
    except Exception:
        print(_json({'error': 'identity_lookup_failed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(_readonly_identity_cli())

def build_collector(*, approved_mappings=(), environ=None, http=None,
                    receipt_journal=None, apify_journal=None, apify_client=None,
                    identity_http=None):
    """Reviewed server composition root. Empty authority/default flags hold.

    Only server startup code supplies approved mappings. Never deserialize a
    request body into this argument. No scheduled runner or live activation.
    """
    reader = CollectorPortalReader(environ=environ, http=http)
    identity = AuthenticatedZernioIdentityReader(read_rows=reader, environ=environ,
                                               http=identity_http)
    resolver = PortalMappingResolver(read_rows=reader, approved_mappings=approved_mappings,
                                     social_identity_reader=identity)
    return TrustedSourceCollector(resolver=resolver, environ=environ, ingest_http=http,
        receipt_journal=receipt_journal, apify_journal=apify_journal, apify_client=apify_client)
