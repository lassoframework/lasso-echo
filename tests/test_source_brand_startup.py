"""Offline operator startup and no-ingest holds; synthetic authorities only."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from agent.source_brand_startup import initialize_source_capture, load_approved_mappings
from agent.source_brand_ingest import CaptureIngestError
from test_source_brand_capture_runner import setup, environment, GYM


def authority(tmp_path, entries):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / 'approved.json'
    path.write_text(json.dumps({'schema_version': 1, 'approved_mappings': entries}))
    os.chmod(path, 0o600)
    return path


def test_default_off_no_files_or_transport(tmp_path):
    assert initialize_source_capture(environ={}) is None
    assert not list(tmp_path.iterdir())


def test_enabled_missing_authority_holds_before_journals(tmp_path):
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path))
    with pytest.raises(CaptureIngestError, match='private_mapping_authority_required'):
        initialize_source_capture(environ=env)
    assert not list(tmp_path.iterdir())


def test_real_startup_composes_and_retains_exact_mapping(tmp_path):
    original, storage, zernio, apify, _ = setup(tmp_path)
    entries = [asdict(x) for x in original.collector._resolve._approved.values()]
    path = authority(tmp_path, entries)
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path),
               ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE=str(path))
    runner = initialize_source_capture(environ=env, http=storage,
                                      identity_http=zernio, apify_client=apify)
    assert not storage.posts and not zernio.calls and not apify.calls
    runner.collector._website_factory = original.collector._website_factory
    runner.collector._min_host_delay = 0
    assert len(runner.capture(GYM, 'startup-fixture')['captures']) == 2


@pytest.mark.parametrize('change', ['empty', 'wrong_uuid', 'unknown', 'expired', 'duplicate'])
def test_invalid_authority_no_ingestion(tmp_path, change):
    original, storage, zernio, apify, _ = setup(tmp_path)
    row = asdict(next(iter(original.collector._resolve._approved.values())))
    entries = [row]
    if change == 'empty':
        entries = []
    elif change == 'wrong_uuid':
        row['gym_id'] = 'Swift River'
    elif change == 'unknown':
        row['source_verified'] = True
    elif change == 'expired':
        row['valid_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    else:
        entries.append(dict(row))
    path = authority(tmp_path, entries)
    with pytest.raises(CaptureIngestError):
        load_approved_mappings(path)
    assert not storage.posts and not zernio.calls and not apify.calls


def test_public_or_symlink_authority_rejected(tmp_path):
    path = authority(tmp_path, [])
    os.chmod(path, 0o644)
    with pytest.raises(CaptureIngestError, match='private_mapping_authority_required'):
        load_approved_mappings(path)
    os.chmod(path, 0o600)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(CaptureIngestError, match='private_mapping_authority_required'):
        load_approved_mappings(link)


def test_slow_provider_status_never_ingests(tmp_path):
    runner, storage, _, apify, websites = setup(tmp_path)
    identity = runner.collector._resolve._social_identity
    now = datetime.now(timezone.utc)
    values = iter([now, now, now + timedelta(minutes=16)])
    identity._now = lambda: next(values)
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'slow-provider')
    assert not storage.rows and not websites and not apify.calls
    assert storage.attestations[-1]['attestation']['lookup_status'] == 'partial'


def test_wrong_live_tenant_key_holds_without_source_bytes(tmp_path):
    original, storage, zernio, apify, websites = setup(tmp_path)
    row = asdict(next(iter(original.collector._resolve._approved.values())))
    row['echo_account_key'] = 'wrong-reviewed-key'
    path = authority(tmp_path, [row])
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path),
               ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE=str(path))
    runner = initialize_source_capture(environ=env, http=storage,
                                      identity_http=zernio, apify_client=apify)
    runner.collector._website_factory = original.collector._website_factory
    with pytest.raises(CaptureIngestError, match='current_tenant_mapping_mismatch'):
        runner.capture(GYM, 'wrong-live-key')
    assert not storage.rows and not storage.posts and not zernio.calls
    assert not websites and not apify.calls
