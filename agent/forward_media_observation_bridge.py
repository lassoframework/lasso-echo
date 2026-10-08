"""Default-OFF producer bridge. Persist observations, never issue authority.

The RPC locks the exact returned calendar row and derives its revision. No
calendar lookup by URL/slot or owner credential is used. Post-insert failures
raise a static hold; the global publish guard still requires owner authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid

ENV = 'AGENT_FORWARD_MEDIA_OBSERVATION_BRIDGE'
METADATA = '_forward_media_observations'
RPC = 'fixer_record_forward_media_observation_20261007'
READY_RPC = 'fixer_forward_media_observation_bridge_ready_20261007'
MAX_JSON_BYTES = 65536
_HEX = re.compile(r'[0-9a-f]{64}\Z')


class ObservationBridgeHold(RuntimeError):
    """Static errors: never include packets, responses, URLs or credentials."""


def enabled():
    return os.getenv(ENV, '').lower() in ('1', 'true', 'yes', 'on')


def draft_metadata(draft):
    if not enabled():
        return {}
    observations = getattr(draft, 'media_materialization_observations', None)
    return {} if observations is None else {METADATA: copy.deepcopy(observations)}


def _json(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > MAX_JSON_BYTES:
            raise ValueError()
        return encoded
    except (ValueError, TypeError, UnicodeError):
        raise ObservationBridgeHold('observation_encoding_invalid') from None


def prepare(row, observations):
    """Select exactly one observation by the complete media edge, not URL alone."""
    if (row.get('status') not in ('draft', 'pending', 'queued')
            or row.get('variant_status', 'active') != 'active'
            or not row.get('post_date') or not row.get('gym_id')
            or not row.get('source_media_asset_id') or not row.get('source_media_url')
            or not row.get('image_url')
            or any(row.get(k) is not None for k in
                   ('publish_claim_token', 'published_at', 'late_post_id',
                    'render_manifest_digest'))):
        raise ObservationBridgeHold('calendar_not_unverified_candidate')
    try:
        row_id = str(uuid.UUID(row['id']))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ObservationBridgeHold('calendar_identity_invalid') from None
    if not isinstance(observations, list) or not 1 <= len(observations) <= 16:
        raise ObservationBridgeHold('observation_batch_invalid')
    matches = [o for o in observations if isinstance(o, dict)
               and o.get('source_asset_id') == row['source_media_asset_id']
               and o.get('source_exact_url') == row['source_media_url']
               and o.get('delivered_exact_url') == row['image_url']]
    # Duplicate observations are accepted only if their encoded bytes agree.
    if not matches or len({_json(o) for o in matches}) != 1:
        raise ObservationBridgeHold('observation_exact_edge_missing_or_ambiguous')
    observation = matches[0]
    digest = observation.get('observation_digest')
    if (type(observation.get('schema_version')) is not int
            or observation['schema_version'] != 1
            or observation.get('provenance_status') != 'unverified'
            or not isinstance(observation.get('tenant'), str)
            or not observation['tenant']
            or not isinstance(digest, str) or not _HEX.fullmatch(digest)):
        raise ObservationBridgeHold('observation_contract_invalid')
    digest_input = _json({k: v for k, v in observation.items()
                          if k != 'observation_digest'})
    if hashlib.sha256(digest_input.encode('utf-8')).hexdigest() != digest:
        raise ObservationBridgeHold('observation_digest_invalid')
    return {'calendar_row_id': row_id, 'observation_digest': digest,
            'observation_json': _json(observation), 'digest_input': digest_input}


def _rpc(store, name, payload):
    try:
        response = store._client().post(store._rest('rpc/' + name),
            headers=store._headers({'Content-Type': 'application/json'}),
            json=payload, timeout=30)
        if response.status_code != 200:
            raise ObservationBridgeHold('observation_bridge_refused')
        return response.json()
    except ObservationBridgeHold:
        raise
    except Exception:
        raise ObservationBridgeHold('observation_bridge_transport_unverified') from None


def preflight(store):
    if _rpc(store, READY_RPC, {}) is not True:
        raise ObservationBridgeHold('observation_bridge_schema_unavailable')


def preflight_replacement(store, account_key, rows):
    """Reject unusable observations before a caller deletes existing rows.

    This is validation only. The writer must still prepare its final payload and
    pair the actual inserted UUID before persisting an observation. In particular,
    cached-rendition placeholders cannot acquire provenance by passing this check.
    """
    if not enabled():
        return
    observed = False
    for row in rows:
        if METADATA not in row:
            continue
        candidate = dict(row, gym_id=account_key, id=str(uuid.uuid4()))
        prepare(candidate, row[METADATA])
        observed = True
    if observed:
        preflight(store)


def persist_inserted(store, payload, returned, candidates):
    """Require exact UUID and payload equality for all rows before any RPC."""
    if not isinstance(returned, list) or len(returned) != len(payload):
        raise ObservationBridgeHold('calendar_insert_pairing_unverified')
    expected = {r['id']: r for r in payload if r.get('id') in candidates}
    paired = {}
    seen = set()
    for row in returned:
        if not isinstance(row, dict) or not row.get('id') or row['id'] in seen:
            raise ObservationBridgeHold('calendar_insert_pairing_unverified')
        seen.add(row['id'])
        if row['id'] not in expected:
            continue
        if not all(k in row and row[k] == v for k, v in expected[row['id']].items()):
            raise ObservationBridgeHold('calendar_insert_pairing_unverified')
        paired[row['id']] = row
    if set(paired) != set(candidates):
        raise ObservationBridgeHold('calendar_insert_pairing_unverified')
    for row_id, candidate in candidates.items():
        result = _rpc(store, RPC, {
            'p_calendar_row_id': row_id, 'p_expected_row': paired[row_id],
            'p_observation_json': candidate['observation_json'],
            'p_digest_input': candidate['digest_input']})
        if (not isinstance(result, dict) or result.get('calendar_row_id') != row_id
                or result.get('observation_digest') != candidate['observation_digest']
                or not isinstance(result.get('revision'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', result['revision'])
                or result.get('provenance_status') != 'unverified'):
            raise ObservationBridgeHold('observation_persistence_unverified')
