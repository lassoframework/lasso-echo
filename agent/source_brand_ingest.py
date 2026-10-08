"""Collector-only original-byte ingestion; no owner, browser, or publisher hook.

This adapter is OFF by default. Its dedicated service credential must be scoped
operationally to a separate collector process, never passed to a generation
owner. The database enforces service authentication. The two injected trust
hooks must come from reviewed server code: resolve_mapping reads authoritative
current mappings; authenticate_response checks the actual collector transport
receipt against the complete response. A dict/actor label is not authentication.
No live collector or semantic fact validator is installed by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

MAX_CAPTURE_BYTES = 2_000_000
_NAMESPACE = uuid.UUID('906284af-0452-4788-9389-2f1e4d735843')
_FIELDS = ('gym_id', 'echo_account_key', 'source_kind', 'source_url',
           'source_locator', 'capture_provider', 'provider_response_id',
           'provider_account_id', 'source_revision', 'mapping_revision',
           'mapping_evidence', 'fetched_at', 'raw_bytes')


class CaptureIngestError(RuntimeError):
    """Safe fixed error codes; never includes response bodies or credentials."""


@dataclass(frozen=True)
class VerifiedMapping:
    """Resolved by a trusted server lookup, not deserialized from a request.

    URLs are exact response URLs (including explicitly mapped website assets).
    Social locators and provider identity must be independently mapped. The
    resolver owns authority/freshness checking of mapping revision/evidence.
    """
    gym_id: str
    echo_account_key: str
    mapping_revision: str
    mapping_evidence: dict
    website_response_urls: tuple[str, ...]
    social_locators: tuple[str, ...]
    provider_account_id: str | None = None


def _fail(code):
    raise CaptureIngestError(code)


def _url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        _fail('invalid_source_url')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
        _fail('invalid_source_url')
    # Never persist provider credentials carried in a URL query.
    if parsed.query:
        _fail('credential_or_unmapped_query_url')
    return parsed


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            _fail('invalid_fetch_timestamp')
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        _fail('invalid_fetch_timestamp')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


class TrustedCaptureIngest:
    """Bounded PostgREST insert and byte/tenant readback for trusted collectors.

    authenticate_response(capture, raw_bytes, transport_receipt, mapping) must
    return True ONLY after independently checking complete, authenticated bytes,
    final response URL, account identity and response revision. There is no
    permissive default. Hooks are injected code dependencies, not request data.
    """
    def __init__(self, *, resolve_mapping, authenticate_response, http=None,
                 environ=None, sleep=time.sleep, attempts=3):
        if not callable(resolve_mapping) or not callable(authenticate_response):
            _fail('trusted_collector_hooks_required')
        if type(attempts) is not int or not 1 <= attempts <= 3:
            _fail('invalid_retry_bound')
        self._resolve = resolve_mapping
        self._authenticate = authenticate_response
        self._http = http
        self._env = os.environ if environ is None else environ
        self._sleep = sleep
        self._attempts = attempts

    def _config(self):
        if self._env.get('ECHO_SOURCE_CAPTURE_INGEST_ENABLED') != 'true':
            _fail('capture_ingest_disabled')
        base = self._env.get('ECHO_SOURCE_CAPTURE_SUPABASE_URL', '')
        key = self._env.get('ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY', '')
        try:
            parsed = _url(base)
        except (ValueError, CaptureIngestError):
            _fail('collector_service_config_missing')
        if parsed.path not in ('', '/') or not _text(key):
            _fail('collector_service_config_missing')
        return base.rstrip('/') + '/rest/v1/', {
            'apikey': key, 'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json',
        }

    def _request(self, method, url, headers, **kwargs):
        client = self._http
        if client is None:
            import requests
            client = requests
        try:
            return getattr(client, method)(url, headers=headers, timeout=15,
                                          allow_redirects=False, **kwargs)
        except Exception:
            # A transport exception can contain Authorization or URL content.
            return None

    @staticmethod
    def _rows(response):
        if response is None or response.status_code != 200:
            return None
        try:
            rows = response.json()
        except Exception:
            _fail('ambiguous_readback')
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            _fail('ambiguous_readback')
        return rows

    def ingest(self, capture, raw_bytes, *, transport_receipt, expected_length,
               expected_sha256):
        """Return capture id/digest only after confirmed storage readback.

        A timeout after insert is reconciled by GET, then retried with the same
        immutable primary key. Conflict-ignore cannot overwrite an existing row.
        A mismatch or ambiguous response fails closed, even if a row was stored.
        """
        base, headers = self._config()
        if not isinstance(capture, dict) or set(capture) != set(_FIELDS) - {'raw_bytes'}:
            _fail('invalid_capture_fields')
        try:
            capture = json.loads(json.dumps(capture, allow_nan=False))
        except (ValueError, TypeError):
            _fail('invalid_capture_metadata')
        if (type(raw_bytes) is not bytes or not 1 <= len(raw_bytes) <= MAX_CAPTURE_BYTES
                or type(expected_length) is not int or len(raw_bytes) != expected_length):
            _fail('incomplete_or_oversize_bytes')
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if expected_sha256 != digest:
            _fail('response_digest_mismatch')
        try:
            gym = str(uuid.UUID(capture['gym_id']))
        except (ValueError, TypeError, AttributeError):
            _fail('invalid_gym_identity')
        if gym != capture['gym_id']:
            _fail('invalid_gym_identity')
        for field in ('echo_account_key', 'source_revision', 'mapping_revision'):
            if not _text(capture[field]):
                _fail('invalid_capture_identity')
        if not isinstance(capture['mapping_evidence'], dict) or not capture['mapping_evidence']:
            _fail('mapping_evidence_required')
        fetched = _stamp(capture['fetched_at'])
        if fetched > datetime.now(timezone.utc):
            _fail('future_fetch_timestamp')
        try:
            parsed = _url(capture['source_url'])
            mapping = self._resolve(gym)
        except Exception:
            _fail('mapping_verification_failed')
        if not isinstance(mapping, VerifiedMapping):
            _fail('trusted_mapping_required')
        for field in ('gym_id', 'echo_account_key', 'mapping_revision', 'mapping_evidence'):
            if capture[field] != getattr(mapping, field):
                _fail('mapping_identity_mismatch')
        kind = capture['source_kind']
        if kind in ('website', 'website_asset'):
            if (capture['source_url'] not in mapping.website_response_urls
                    or capture['capture_provider'] != 'direct'
                    or any(capture[x] is not None for x in
                           ('source_locator', 'provider_account_id', 'provider_response_id'))):
                _fail('website_mapping_mismatch')
        elif kind == 'social':
            try:
                locator = _url(capture['source_locator'])
            except Exception:
                _fail('social_mapping_mismatch')
            if (capture['source_locator'] not in mapping.social_locators
                    or locator.hostname not in ('instagram.com', 'www.instagram.com')
                    or not _text(mapping.provider_account_id)
                    or capture['provider_account_id'] != mapping.provider_account_id
                    or capture['capture_provider'] != 'apify'
                    or parsed.hostname != 'api.apify.com'
                    or not parsed.path.startswith('/v2/')
                    or not _text(capture['provider_response_id'])):
                _fail('social_mapping_mismatch')
        else:
            _fail('invalid_source_kind')
        frozen_capture = json.dumps(capture, sort_keys=True, allow_nan=False)
        try:
            authenticated = self._authenticate(capture, raw_bytes, transport_receipt, mapping)
        except Exception:
            _fail('response_authentication_failed')
        if json.dumps(capture, sort_keys=True, allow_nan=False) != frozen_capture:
            _fail('response_authentication_mutated_metadata')
        if authenticated is not True:
            _fail('response_authentication_failed')
        # JSON round-trip freezes mutable caller metadata before any HTTP call.
        try:
            payload = json.loads(json.dumps(capture, allow_nan=False))
            payload['fetched_at'] = fetched.isoformat()
            payload['raw_bytes'] = '\\x' + raw_bytes.hex()
            identity = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError):
            _fail('invalid_capture_metadata')
        payload['id'] = str(uuid.uuid5(_NAMESPACE, identity))
        params = {'id': 'eq.' + payload['id'], 'gym_id': 'eq.' + gym, 'select': '*'}
        for attempt in range(self._attempts):
            # Current portal key is checked on every write attempt, never inferred.
            try:
                current_mapping = self._resolve(gym)
            except Exception:
                _fail('mapping_verification_failed')
            if current_mapping != mapping:
                _fail('mapping_changed_during_ingest')
            rows = self._rows(self._request('get', base + 'echo_intake_tokens', headers,
                              params={'gym_id': 'eq.' + gym, 'select': 'gym_id,echo_account_key'}))
            if rows is None:
                _fail('current_mapping_unavailable')
            if len(rows) != 1 or rows[0] != {'gym_id': gym, 'echo_account_key': payload['echo_account_key']}:
                _fail('current_mapping_mismatch')
            response = self._request('post', base + 'echo_source_captures',
                                    dict(headers, Prefer='resolution=ignore-duplicates,return=minimal'),
                                    params={'on_conflict': 'id'}, json=payload)
            if response is not None and response.status_code not in (200, 201, 204, 409, 429, 500, 502, 503, 504):
                _fail('capture_insert_rejected')
            rows = self._rows(self._request('get', base + 'echo_source_captures', headers, params=params))
            if rows:
                if len(rows) != 1:
                    _fail('ambiguous_readback')
                row = rows[0]
                for field in ('id',) + _FIELDS:
                    expected = payload[field]
                    actual = row.get(field)
                    if field == 'fetched_at':
                        if _stamp(actual) != fetched:
                            _fail('capture_readback_mismatch')
                    elif actual != expected:
                        _fail('capture_readback_mismatch')
                if row.get('bytes_sha256') != digest:
                    _fail('capture_readback_digest_mismatch')
                captured = _stamp(row.get('captured_at'))
                if captured < fetched:
                    _fail('capture_readback_timestamp_mismatch')
                current = self._rows(self._request('get', base + 'echo_intake_tokens', headers,
                                    params={'gym_id': 'eq.' + gym, 'select': 'gym_id,echo_account_key'}))
                if current != [{'gym_id': gym, 'echo_account_key': payload['echo_account_key']}]:
                    _fail('current_mapping_changed_after_insert')
                return {'id': payload['id'], 'gym_id': gym,
                        'echo_account_key': payload['echo_account_key'], 'bytes_sha256': digest}
            if attempt + 1 < self._attempts:
                self._sleep(0.25 * (2 ** attempt))
        _fail('capture_storage_unconfirmed')
