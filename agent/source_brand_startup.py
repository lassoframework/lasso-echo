"""Dedicated operator startup for source capture; no scheduler or import I/O.

The private authority file is operator state, never an upload or request body.
Its receipts must reference independently reviewed gym/domain approval evidence.
File permissions establish provenance of installation, not truth of approval.
"""
from __future__ import annotations

import os
import stat
from dataclasses import fields
from pathlib import Path

from .source_brand_capture_runner import build_capture_runner
from .source_brand_collector import ServerMapping, PortalMappingResolver
from .source_brand_ingest import CaptureIngestError, TrustedCaptureIngest


def _fail(code):
    raise CaptureIngestError(code) from None


def load_approved_mappings(path):
    """Load a bounded exact schema from a private, operator-owned regular file."""
    location = Path(path)
    if not location.is_absolute():
        _fail('private_mapping_authority_required')
    # Reject symlinks in every component, including the directory ancestry.
    if any(parent.is_symlink() for parent in (location, *location.parents)):
        _fail('private_mapping_authority_required')
    try:
        parent = location.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            _fail('private_mapping_authority_required')
        fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or not 1 <= info.st_size <= 262144):
                _fail('private_mapping_authority_required')
            raw = source.read(262145)
            if not 1 <= len(raw) <= 262144:
                _fail('private_mapping_authority_required')
        from .source_brand_collector import _strict_json
        # Shared strict parser: rejects duplicate keys and NaN/Infinity so a
        # private authority file cannot smuggle non-finite or shadowed values.
        data = _strict_json(raw)
    except CaptureIngestError:
        raise
    except Exception:
        _fail('private_mapping_authority_required')
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'approved_mappings'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or not isinstance(data['approved_mappings'], list)
            or not 1 <= len(data['approved_mappings']) <= 256):
        _fail('approved_mapping_schema_invalid')
    required = {'gym_id', 'echo_account_key', 'website_urls', 'domain_evidence',
                'approval_receipt', 'valid_until'}
    allowed = {field.name for field in fields(ServerMapping)}
    entries = []
    for row in data['approved_mappings']:
        if (not isinstance(row, dict) or not required <= set(row) or set(row) - allowed
                or any(type(row[key]) is not str or not row[key].strip()
                       for key in required - {'website_urls', 'domain_evidence'})
                or any(not isinstance(row[key], list) or not 1 <= len(row[key]) <= 64
                       or any(type(item) is not str or not item.strip() for item in row[key])
                       for key in ('website_urls', 'domain_evidence'))
                or any(row.get(key) is not None and (type(row[key]) is not str or not row[key].strip())
                       for key in allowed - required)):
            _fail('approved_mapping_schema_invalid')
        entries.append(ServerMapping(**dict(row, website_urls=tuple(row['website_urls']),
                                           domain_evidence=tuple(row['domain_evidence']))))
    # Validate canonical identities, duplication, expiry and exact HTTPS URLs
    # without portal/provider calls or ingestion. Real portal read follows capture.
    resolver = PortalMappingResolver(read_rows=lambda *args: [], approved_mappings=entries)
    from .source_brand_collector import _timestamp, _website_url
    from datetime import datetime, timezone
    for entry in entries:
        if _timestamp(entry.valid_until) <= datetime.now(timezone.utc):
            _fail('mapping_approval_expired')
        for url in entry.website_urls:
            _website_url(url)
    return tuple(resolver._approved.values())


def initialize_source_capture(*, environ=None, http=None, identity_http=None,
                              apify_client=None):
    """Return None while OFF; explicit startup holds if real authority is absent.

    Construction does not fetch or ingest. The dedicated owner invokes capture
    with canonical gym UUID and durable orchestration request ID after release.
    """
    env = dict(os.environ if environ is None else environ)
    if env.get('ECHO_SOURCE_CAPTURE_RUNNER_ENABLED') != 'true':
        return None
    if (env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true'
            or env.get('ECHO_SOURCE_CAPTURE_INGEST_ENABLED') != 'true'):
        _fail('source_capture_runner_disabled')
    path = env.get('ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE')
    if not isinstance(path, str) or not path:
        _fail('private_mapping_authority_required')
    mappings = load_approved_mappings(path)
    TrustedCaptureIngest(resolve_mapping=lambda _: None,
        authenticate_response=lambda *args: False, environ=env)._config()
    key = env.get('ZERNIO_API_KEY')
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        _fail('zernio_account_credential_required')
    return build_capture_runner(approved_mappings=mappings, environ=env, http=http,
                                identity_http=identity_http, apify_client=apify_client)
