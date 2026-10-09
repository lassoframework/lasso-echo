"""Default-OFF runtime bridge for fresh gym Astra originals.

The scheduler is a publisher process and cannot open the dedicated owner DSN.
Its queue-only dispatch requests dates; the isolated owner atomically binds
empty dates to persisted calendar rows. The row runner uses the existing owner,
Astra, reviewer and R2 adapters; it never approves or clears calendar holds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import generated_infographic_preparation as prep

FLAG = 'AGENT_GENERATED_INFOGRAPHIC_RUNTIME'
JOURNAL = '/data/generated-infographic-jobs.sqlite'
PREFIX = 'generated-astra:'


class RuntimeHold(RuntimeError):
    """Static diagnostic only, never provider/credential/private source data."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


def journal_path():
    # One durable journal for every creator and send-boundary reader. Never
    # fall back to the repository, /tmp or a different SQLite job journal.
    path = os.path.realpath(os.getenv('AGENT_GENERATED_INFOGRAPHIC_JOURNAL', JOURNAL))
    if (not path.startswith('/data/') or not os.path.isdir('/data')
            or not os.path.ismount('/data')):
        raise RuntimeHold('generated_durable_journal_unavailable')
    return path


def _account_binding(base, account):
    key = getattr(account, 'key', None)
    if (key not in (base + '_ig', base + '_fb') or
            getattr(account, 'platform', None) !=
            ('instagram' if key == base + '_ig' else 'facebook_page')):
        raise RuntimeHold('generated_account_gym_mismatch')
    return key


# This is a distinct delegated policy contract. Configuration approval never
# sets copy_approved. The SQL adapter must authenticate the current mapping,
# actor authority and latest observation before returning this payload.
BUNDLE_CONTRACT = 'echo-source-brand-delegated-v1'
AUTHORITY_PIN_FIELDS = frozenset(('mode', 'gym_id', 'echo_account_key', 'bundle_id',
    'bundle_version', 'configuration_sha256', 'configuration_receipt_sha256',
    'observation_id', 'observation_sha256', 'validator_revision', 'derivation_sha256'))


def _stamp(value):
    from datetime import datetime, timezone
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError()
    return stamp.astimezone(timezone.utc)


def _uuid_text(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError()
    return value


def _json_exact(raw, sha):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    if (not isinstance(raw, str) or not 0 < len(raw.encode()) <= 40_000_000
            or hashlib.sha256(raw.encode()).hexdigest() != sha):
        raise ValueError()
    return json.loads(raw, object_pairs_hook=unique)


SNAPSHOT_V1_KEYS = frozenset(('schema_version', 'gym_id', 'echo_account_key',
    'captures', 'palette', 'fact_policy', 'selected_facts'))
SNAPSHOT_V2_KEYS = SNAPSHOT_V1_KEYS | {'source_policy', 'provider_status_receipt'}
POLICY_MODES = ('website_only_no_connected_instagram_v2', 'website_and_social_v2')
PROVIDER_IDENTITY = ('zernio', 'zernio_authenticated_accounts')
PROVIDER_ATTESTATION_MAX_AGE = timedelta(minutes=15)


def _numeric_text(value):
    # Numeric Instagram owner identity; distinct from the provider account id.
    if type(value) is bool:
        raise ValueError()
    text = str(value) if type(value) is int else value
    if not isinstance(text, str) or not re.fullmatch(r'[0-9]+', text):
        raise ValueError()
    return text


def _connection_identity(connection):
    # Frozen Instagram identity {id:provider account_id, platform_user_id, handle}.
    if connection is None:
        return None
    if (not isinstance(connection, dict)
            or set(connection) != {'id', 'platform_user_id', 'handle'}
            or not isinstance(connection['id'], str) or not connection['id'].strip()
            or not isinstance(connection['handle'], str) or not connection['handle'].strip()):
        raise ValueError()
    return (connection['id'], _numeric_text(connection['platform_user_id']),
            connection['handle'])


def _source_policy(policy, gym, base):
    """Strict frozen schema-v2 source policy shape pinned to this gym/key."""
    if (not isinstance(policy, dict)
            or set(policy) != {'version', 'mode', 'gym_id', 'echo_account_key',
                               'provider_identity', 'instagram_connection'}
            or type(policy['version']) is not int or policy['version'] != 2
            or policy['mode'] not in POLICY_MODES
            or policy['gym_id'] != gym or policy['echo_account_key'] != base):
        raise ValueError()
    identity = policy['provider_identity']
    if (not isinstance(identity, dict)
            or set(identity) != {'provider', 'source', 'profile_id', 'mapping_revision'}
            or (identity['provider'], identity['source']) != PROVIDER_IDENTITY
            or any(not isinstance(identity[k], str) or not identity[k].strip()
                   for k in ('profile_id', 'mapping_revision'))):
        raise ValueError()
    return dict(mode=policy['mode'],
                provider=(identity['provider'], identity['source'],
                          identity['profile_id'], identity['mapping_revision']),
                connection=_connection_identity(policy['instagram_connection']))


def _provider_status_receipt(receipt):
    # Immutable gym/key-scoped receipt bound into each snapshot/observation.
    if (not isinstance(receipt, dict)
            or set(receipt) != {'id', 'observed_at', 'response_sha256'}
            or not isinstance(receipt['response_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', receipt['response_sha256'])):
        raise ValueError()
    if type(receipt['id']) is not int and not isinstance(receipt['id'], str):
        raise ValueError()
    if type(receipt['id']) is str:
        _numeric_text(receipt['id'])
    elif receipt['id'] < 1:
        raise ValueError()
    _stamp(receipt['observed_at'])
    return receipt


def _provider_current(attestation, gym, base, now):
    """Trusted CURRENT portal attestation readback; never the frozen snapshot.

    Only a fresh complete authenticated lookup counts. Missing, malformed,
    stale, partial, unavailable or unauthenticated evidence holds closed.
    """
    try:
        if (not isinstance(attestation, dict)
                or set(attestation) != {'provider', 'source', 'gym_id', 'echo_account_key',
                        'profile_id', 'mapping_revision', 'lookup_status', 'authenticated',
                        'observed_at', 'response_sha256', 'instagram'}
                or (attestation['provider'], attestation['source']) != PROVIDER_IDENTITY
                or attestation['gym_id'] != gym or attestation['echo_account_key'] != base
                or not isinstance(attestation['mapping_revision'], str)
                or not attestation['mapping_revision'].strip()
                or attestation['lookup_status'] not in ('complete', 'partial', 'unavailable')
                or type(attestation['authenticated']) is not bool):
            raise ValueError()
        observed = _stamp(attestation['observed_at'])
        instagram = attestation['instagram']
        complete = (attestation['lookup_status'] == 'complete'
                and attestation['authenticated'] is True
                and isinstance(attestation['profile_id'], str) and attestation['profile_id'].strip()
                and isinstance(attestation['response_sha256'], str)
                and re.fullmatch(r'[0-9a-f]{64}', attestation['response_sha256'])
                and isinstance(instagram, dict)
                and set(instagram) == {'connected', 'account_id', 'platform_user_id', 'handle'}
                and type(instagram['connected']) is bool)
        if not complete:
            raise ValueError()
        if instagram['connected']:
            connection = _connection_identity(dict(
                id=instagram['account_id'], platform_user_id=instagram['platform_user_id'],
                handle=instagram['handle']))
        elif any(instagram[k] is not None for k in ('account_id', 'platform_user_id', 'handle')):
            raise ValueError()
        else:
            connection = None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeHold('generated_bundle_provider_status_unavailable') from None
    if not now - PROVIDER_ATTESTATION_MAX_AGE <= observed <= now:
        raise RuntimeHold('generated_bundle_provider_status_unavailable')
    return dict(provider=(attestation['provider'], attestation['source'],
                          attestation['profile_id'], attestation['mapping_revision']),
                connection=connection)


def _bundle_snapshot(raw, sha, gym, base, *, fresh, now):
    import base64
    from datetime import timedelta
    snapshot = _json_exact(raw, sha)
    if not isinstance(snapshot, dict):
        raise ValueError()
    keys = set(snapshot)
    if keys == SNAPSHOT_V1_KEYS:
        schema = 1
    elif keys == SNAPSHOT_V2_KEYS:
        schema = 2
    else:
        raise ValueError()
    policy = None
    if schema == 2:
        policy = _source_policy(snapshot['source_policy'], gym, base)
        _provider_status_receipt(snapshot['provider_status_receipt'])
    if (type(snapshot['schema_version']) is not int or snapshot['schema_version'] != schema
            or snapshot['gym_id'] != gym or snapshot['echo_account_key'] != base
            or snapshot['fact_policy'] != 'delegated_supported_facts'
            or not isinstance(snapshot['captures'], list)
            or not 1 <= len(snapshot['captures']) <= 12):
        raise ValueError()
    captures, seen, kinds = {}, set(), set()
    for capture in snapshot['captures']:
        identity = _uuid_text(capture['id'])
        if identity in seen or capture['gym_id'] != gym or capture['echo_account_key'] != base:
            raise ValueError()
        seen.add(identity)
        kinds.add(capture['source_kind'])
        data = base64.b64decode(capture['bytes_base64'], validate=True)
        if not 0 < len(data) <= 2_000_000 or hashlib.sha256(data).hexdigest() != capture['bytes_sha256']:
            raise ValueError()
        fetched = _stamp(capture['fetched_at'])
        if fetched > now or (fresh and fetched < now - timedelta(days=7)):
            raise ValueError()
        for field in ('source_url', 'source_revision', 'mapping_revision'):
            if not isinstance(capture[field], str) or not capture[field].strip():
                raise ValueError()
        if not isinstance(capture['mapping_evidence'], dict) or not capture['mapping_evidence']:
            raise ValueError()
        from urllib.parse import urlsplit
        url = urlsplit(capture['source_url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        if capture['source_kind'] in ('website', 'website_asset'):
            if capture['capture_provider'] != 'direct' or any(capture[k] is not None for k in
                    ('source_locator', 'provider_response_id', 'provider_account_id')):
                raise ValueError()
        elif capture['source_kind'] == 'social':
            locator = urlsplit(capture['source_locator'])
            if (capture['capture_provider'] != 'apify' or url.hostname != 'api.apify.com'
                    or not url.path.startswith('/v2/') or locator.scheme != 'https'
                    or locator.hostname not in ('instagram.com', 'www.instagram.com')
                    or any(not isinstance(capture[k], str) or not capture[k].strip()
                           for k in ('provider_response_id', 'provider_account_id'))):
                raise ValueError()
        else:
            raise ValueError()
        captures[identity] = (capture, data)
    if 'website' not in kinds:
        raise ValueError()
    if schema == 1 or policy['mode'] == 'website_and_social_v2':
        # v1 and both-source v2 require website and social evidence. A
        # both-source policy is frozen only with a connected identity whose
        # numeric owner matches every social capture exactly.
        if 'social' not in kinds:
            raise ValueError()
        if schema == 2:
            if policy['connection'] is None:
                raise ValueError()
            for capture, data in captures.values():
                if (capture['source_kind'] == 'social' and
                        capture['provider_account_id'] != policy['connection'][1]):
                    raise RuntimeHold('generated_bundle_provider_instagram_identity_mismatch')
    else:
        # Website-only v2: never social captures, never a connected identity.
        if 'social' in kinds:
            raise RuntimeHold('generated_bundle_website_only_capture_set_required')
        if policy['connection'] is not None:
            raise ValueError()
    palette = snapshot['palette']
    capture, data = captures[palette['capture_id']]
    if capture['source_kind'] not in ('website', 'website_asset') or palette['bytes_sha256'] != capture['bytes_sha256']:
        raise ValueError()
    import re
    for name in ('primary', 'secondary'):
        offset = palette[name + '_byte_offset']
        if (type(offset) is not int or offset < 0 or not isinstance(palette[name], str)
                or not re.fullmatch(r'#[0-9A-Fa-f]{6}', palette[name])
                or data[offset:offset+7].decode('utf-8') != palette[name]):
            raise ValueError()
    facts, keys = snapshot['selected_facts'], set()
    if not isinstance(facts, list) or not 1 <= len(facts) <= 30:
        raise ValueError()
    for fact in facts:
        if (set(fact) != {'key', 'capture_id', 'bytes_sha256', 'source_locator',
                'byte_offset', 'byte_length', 'text'} or not isinstance(fact['key'], str)
                or not 1 <= len(fact['key']) <= 100 or fact['key'] in keys):
            raise ValueError()
        keys.add(fact['key'])
        capture, data = captures[fact['capture_id']]
        start, length = fact['byte_offset'], fact['byte_length']
        if (type(start) is not int or start < 0 or type(length) is not int
                or not 1 <= length <= 2000 or start + length > len(data)
                or fact['bytes_sha256'] != capture['bytes_sha256']
                or fact['source_locator'] != (capture['source_locator'] or capture['source_url'])
                or data[start:start+length].decode('utf-8') != fact['text']
                or not fact['text'].strip()):
            raise ValueError()
    return snapshot, policy


def validate_authority_pins(pins, base):
    try:
        import re
        if (not isinstance(pins, dict) or set(pins) != AUTHORITY_PIN_FIELDS
                or pins['mode'] != 'delegated_policy' or pins['echo_account_key'] != base
                or type(pins['bundle_version']) is not int or pins['bundle_version'] < 1
                or type(pins['observation_id']) is not int or pins['observation_id'] < 1
                or not isinstance(pins['validator_revision'], str) or not pins['validator_revision'].strip()):
            raise ValueError()
        _uuid_text(pins['gym_id'])
        _uuid_text(pins['bundle_id'])
        for field in ('configuration_sha256', 'configuration_receipt_sha256',
                      'observation_sha256', 'derivation_sha256'):
            if not isinstance(pins[field], str) or not re.fullmatch(r'[0-9a-f]{64}', pins[field]):
                raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeHold('generated_bundle_pins_invalid') from None
    return dict(pins)


def delegated_copy(active, base, *, caption=None, local_date=None, now=None):
    """Exact byte witnesses plus deterministic verbatim derivation, no approval.

    Only consume an authenticated CURRENT active readback. Hashes alone cannot
    establish actor authority, semantic validation or current locator identity.
    The trusted SQL bridge must recheck those under its existing tenant locks.
    """
    from datetime import datetime, timezone, date
    now = _stamp(now) if isinstance(now, str) else now or datetime.now(timezone.utc)
    try:
        if (not isinstance(active, dict) or active.get('fact_approval_mode') != 'delegated_policy'
                or active.get('fact_validation') != 'supported_uncontradicted'):
            raise RuntimeHold('generated_bundle_fact_validation_required')
        bundle, receipt, observation = (active[k] for k in ('bundle', 'approval_receipt', 'observation'))
        if not isinstance(observation, dict):
            raise RuntimeHold('generated_bundle_fact_validation_required')
        gym = _uuid_text(bundle['gym_id'])
        bundle_id = _uuid_text(bundle['id'])
        if (bundle['echo_account_key'] != base or type(bundle['schema_version']) is not int
                or bundle['schema_version'] not in (1, 2) or type(bundle['version']) is not int or bundle['version'] < 1
                or any(receipt.get(k) != v for k, v in dict(gym_id=gym, bundle_id=bundle_id,
                    bundle_version=bundle['version'], content_sha256=bundle['content_sha256'],
                    purpose='echo_source_brand_configuration', action='approve').items())
                or receipt['actor_authority'] not in ('blake', 'source_brand_approver')
                or not isinstance(receipt['actor_clerk_user_id'], str) or not receipt['actor_clerk_user_id'].strip()
                or type(receipt['id']) is not int or receipt['id'] < 1):
            raise ValueError()
        _uuid_text(receipt['request_id'])
        if not _stamp(bundle['created_at']) <= _stamp(receipt['created_at']) <= now:
            raise ValueError()
        if (observation['gym_id'] != gym or observation['bundle_id'] != bundle_id
                or observation['configuration_sha256'] != bundle['content_sha256']
                or observation['validation_report'].get('selected_facts_status') != 'supported_uncontradicted'
                or observation['validation_report'].get('identity_status') != 'verified'
                or not _stamp(receipt['created_at']) <= _stamp(observation['created_at']) <= now):
            raise ValueError()
        configuration, config_policy = _bundle_snapshot(
            bundle['snapshot_bytes'], bundle['content_sha256'], gym, base, fresh=False, now=now)
        if bundle['schema_version'] != configuration['schema_version']:
            raise ValueError()
        ids = bundle['capture_ids']
        if (not isinstance(ids, list) or len(set(ids)) != len(ids)
                or set(ids) != {capture['id'] for capture in configuration['captures']}):
            raise ValueError()
        current, current_policy = _bundle_snapshot(
            observation['snapshot_bytes'], observation['content_sha256'], gym, base, fresh=True, now=now)
        if config_policy is not None:
            # Schema v2: every observation retains the frozen policy exactly.
            # The frozen snapshot alone never proves current state; only a
            # fresh complete authenticated CURRENT provider attestation from
            # the portal readback can, and drift can never be waived here.
            if current_policy != config_policy:
                raise RuntimeHold('generated_bundle_stale_source_policy')
            current_readback = _provider_current(active.get('provider_status'), gym, base, now)
            if current_readback['provider'] != config_policy['provider']:
                raise RuntimeHold('generated_bundle_stale_source_policy')
            if (config_policy['mode'] == 'website_only_no_connected_instagram_v2'
                    and current_readback['connection'] is not None):
                raise RuntimeHold('generated_bundle_connected_instagram_requires_social')
            if current_readback['connection'] != config_policy['connection']:
                raise RuntimeHold('generated_bundle_stale_source_policy')
        def fact_identity(snap):
            return sorted((f['key'], f['text'], f['source_locator']) for f in snap['selected_facts'])
        if (fact_identity(configuration) != fact_identity(current)
                or any(configuration['palette'][k] != current['palette'][k] for k in ('primary', 'secondary'))):
            raise ValueError()
        def identities(snap):
            return sorted((c['source_kind'], c['source_locator'] or c['source_url'], c['capture_provider'],
                c['provider_account_id'], c['mapping_revision'], prep.canonical(c['mapping_evidence'])) for c in snap['captures'])
        if identities(configuration) != identities(current):
            raise ValueError()
        facts = sorted(current['selected_facts'], key=lambda f: f['key'])
        if caption is None:
            selected = facts[date.fromisoformat(local_date).toordinal() % len(facts)]
            caption = selected['text']
            # Repeated identical supported text uses one deterministic witness.
            selected = next(f for f in facts if f['text'] == caption)
        else:
            selected = next((f for f in facts if f['text'] == caption), None)
            if selected is None:
                raise RuntimeHold('generated_bundle_caption_unsupported')
        copy = dict(headline=caption, facts=[caption], cta='', footer='')
        if any(mark in caption for mark in ('-', '–', '—', ':', ';')):
            raise RuntimeHold('generated_copy_style_invalid')
        derivation = dict(policy='verbatim_selected_fact_v1', caption=caption,
                         copy_digest=prep.digest(copy), fact_witness=selected)
        pins = dict(mode='delegated_policy', gym_id=gym, echo_account_key=base,
            bundle_id=bundle_id, bundle_version=bundle['version'],
            configuration_sha256=bundle['content_sha256'], configuration_receipt_sha256=prep.digest(receipt),
            observation_id=observation['id'], observation_sha256=observation['content_sha256'],
            validator_revision=observation['validator_revision'], derivation_sha256=prep.digest(derivation))
        validate_authority_pins(pins, base)
        palette = dict(gym_id=base, verified=True, colors=[current['palette'][k] for k in ('primary','secondary')],
                       evidence_ref='source-brand-observation:sha256:' + observation['content_sha256'])
        return dict(copy=copy, caption=caption, palette=palette, authority_pins=pins,
                    copy_derivation_receipt=derivation, copy_approved=False, copy_verified=True,
                    source_revision='source-brand:sha256:' + prep.digest(pins),
                    palette_revision='source-brand-palette:sha256:' + prep.digest(current['palette']))
    except RuntimeHold:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        raise RuntimeHold('generated_bundle_evidence_invalid') from None


def _owner_bundle_readback(persistence, base):
    """Dedicated role wrapper only; never import collector/service credentials."""
    try:
        persistence._assert_owner_identity()
        with persistence._conn.cursor() as cur:
            cur.execute('select public.fixer_generated_source_brand_active_20261007(%s)', (base,))
            result = cur.fetchone()[0]
        if (not isinstance(result, dict) or result.get('consumer_contract') != BUNDLE_CONTRACT
                or result.get('echo_account_key') != base or not isinstance(result.get('active'), dict)
                or result.get('gym_id') != result['active'].get('bundle', {}).get('gym_id')):
            raise RuntimeHold('generated_bundle_owner_bridge_unavailable')
        return result['active']
    except RuntimeHold:
        raise
    except Exception:
        raise RuntimeHold('generated_bundle_owner_bridge_unavailable') from None
    finally:
        persistence._conn.rollback()

class OwnerSnapshotLoader:
    """Compose DB facts with current authenticated delegated bundle evidence.

    Verbatim facts and palette tokens are checked against retained exact bytes.
    Configuration receipt is never per-caption approval. Reads finish before
    provider/storage work; final SQL must recheck authority under tenant locks.
    """
    def __init__(self, persistence, *, bundle_reader=None, reader=None):
        self.persistence = persistence
        self.bundle_reader = bundle_reader or (lambda base: _owner_bundle_readback(persistence, base))
        self.reader = reader or persistence._reader.read

    def load(self, row_id, base, account, *, observe_history=True):
        from . import forward_media_guard as guard
        key = _account_binding(base, account)
        try:
            snap = guard.generated_snapshot(self.persistence, row_id)
        finally:
            self.persistence._conn.rollback()
        if (not isinstance(snap, dict) or snap.get('gym_id') != base
                or snap.get('account') != ('instagram' if key.endswith('_ig') else 'facebook')
                or snap.get('format') != 'feed'):
            raise RuntimeHold('generated_calendar_binding_changed')
        # Generated fallback is allowed only when the guarded owner RPC has
        # certified the exact latest complete, fresh zero census for this
        # inventory generation and epoch. Legacy/raw snapshots cannot prove it.
        if snap.get('local_census_current') is not True:
            raise RuntimeHold('generated_local_census_unverified')
        copy = snap.get('copy')
        if (not isinstance(copy, dict) or copy.get('gym_id') != base
                or any(copy.get(k) != snap.get(k) for k in ('local_date', 'logical_post_id', 'group_key'))
                or not isinstance(copy.get('caption'), str) or not copy['caption'].strip()):
            raise RuntimeHold('generated_copy_binding_changed')
        caption = copy['caption']
        authority = delegated_copy(self.bundle_reader(base), base, caption=caption)
        graphic_copy, palette = authority['copy'], authority['palette']
        snap = {**snap, 'copy': graphic_copy, 'copy_approved': False, 'copy_verified': True,
                'copy_digest': prep.digest(graphic_copy), 'palette': palette,
                'palette_verified': True, 'palette_digest': prep.digest(palette),
                'palette_revision': authority['palette_revision'],
                'approved_source_revision': authority['source_revision'],
                'authority_pins': authority['authority_pins'],
                'copy_derivation_receipt': authority['copy_derivation_receipt']}
        binding = {k: snap.get(k) for k in prep.BINDING_FIELDS}
        prep.validated_binding(binding)
        prep.checked_snapshot(binding, snap)
        history = snap.get('history')
        if (not isinstance(history, dict) or history.get('scope_complete') is not True
                or history.get('spine_digest') != snap.get('history_revision')
                or not isinstance(history.get('rows'), list) or len(history['rows']) > 10000):
            raise RuntimeHold('generated_history_uncertain')
        visuals = []
        if observe_history:
            seen, observed = set(), {}
            from .visual_scene import scene_fingerprint, normalize_scene
            import re
            for item in history['rows']:
                if (not isinstance(item, dict) or not item.get('history_key')
                        or item['history_key'] in seen or not item.get('published_binding_ref')):
                    raise RuntimeHold('generated_history_uncertain')
                seen.add(item['history_key'])
                proof = item.get('history_proof_ref')
                if proof is not None:
                    # This is returned ONLY by the dedicated owner DB snapshot.
                    # Never accept caller cache flags or recompute authority from
                    # URLs. SQL joins the exact tuple and sealed baseline epoch.
                    if (not isinstance(proof, str) or not re.fullmatch(
                            r'generated-history:sha256:[0-9a-f]{64}', proof)
                            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(item.get('visual_sha256')))
                            or not isinstance(item.get('phash'), str)
                            or normalize_scene(item['phash']) != item['phash']):
                        raise RuntimeHold('generated_history_cache_invalid')
                    visuals.append(dict(item))
                    continue
                url = item.get('visual_url')
                if url not in observed:
                    data = self.reader(url)
                    if not isinstance(data, bytes) or not 0 < len(data) <= prep.MAX_BYTES:
                        raise RuntimeHold('generated_history_bytes_unavailable')
                    sha = 'sha256:' + hashlib.sha256(data).hexdigest()
                    phash = scene_fingerprint(data)
                    if not phash:
                        raise RuntimeHold('generated_history_bytes_unavailable')
                    observed[url] = (sha, phash)
                sha, phash = observed[url]
                if item.get('visual_sha256') not in (None, sha):
                    raise RuntimeHold('generated_history_bytes_changed')
                visuals.append({**item, 'visual_sha256': sha, 'phash': phash})
        return snap, visuals



CENSUS_MAX_AGE = timedelta(minutes=10)


def _latest_local_census(persistence, gym_id, inventory_revision):
    """Newest trusted current-epoch census at the exact inventory revision.

    SQL resolves one latest-authority row by deterministic observed_at/receipt
    ordering; an older zero census never overrides a newer positive or
    incomplete one. Dedicated owner role wrapper only; the read transaction
    always ends here and never spans provider or storage work.
    """
    from .forward_media_owner import ForwardMediaOwnerPersistence
    if type(persistence) is not ForwardMediaOwnerPersistence:
        raise RuntimeHold('generated_owner_required')
    try:
        persistence._assert_owner_identity()
        with persistence._conn.cursor() as cur:
            cur.execute('select public.fixer_generated_local_census_authority_20261008(%s,%s)',
                        (gym_id, inventory_revision))
            row = cur.fetchone()
        return row[0] if row else None
    except RuntimeHold:
        raise
    except Exception:
        raise RuntimeHold('generated_local_census_unavailable') from None
    finally:
        persistence._conn.rollback()


def _census_observed_at(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _require_latest_local_depletion(persistence, gym_id, inventory_revision, *, now=None):
    """Fail-closed local-photo gate before any paid provider I/O.

    Only the single newest trusted current-epoch/current-gym census at the
    exact inventory revision governs: it must be complete, fresh (within ten
    minutes, not future-dated) and zero-supply. Missing, stale, noncomplete or
    nonzero latest rows hold; SQL repeats this same latest-authority rule under
    reservation locks and again at final send.
    """
    if (not isinstance(gym_id, str) or not gym_id.strip()
            or not isinstance(inventory_revision, str) or not inventory_revision.strip()):
        raise RuntimeHold('generated_local_census_unavailable')
    census = _latest_local_census(persistence, gym_id, inventory_revision)
    receipt = census.get('receipt_id') if isinstance(census, dict) else None
    try:
        uuid.UUID(str(receipt))
    except (TypeError, ValueError, AttributeError):
        receipt = None
    if not isinstance(census, dict) or census.get('enabled') is not True or receipt is None:
        raise RuntimeHold('generated_local_census_unavailable')
    if census.get('local_complete') is not True:
        raise RuntimeHold('generated_local_census_incomplete')
    if type(census.get('local_available')) is not int or census['local_available'] != 0:
        raise RuntimeHold('generated_local_photo_available')
    observed = _census_observed_at(census.get('observed_at'))
    now = now or datetime.now(timezone.utc)
    if observed is None or not now - CENSUS_MAX_AGE <= observed <= now:
        raise RuntimeHold('generated_local_census_stale')

def _generation_binding(request, snapshot):
    binding = {**request, 'copy_digest': prep.digest(snapshot['copy']),
               'palette_digest': prep.digest(snapshot['palette']), 'review_policy_id': prep.POLICY,
               **prep.authority_binding(snapshot, request['gym_id'])}
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-astra:' + prep.canonical(binding)))
    return job_id, binding


def _runtime_record(jobs, row_id, *, binding=None, source_revision=None, state=None):
    """Persist row->job BEFORE provider claim; retain incomplete A jobs too."""
    with sqlite3.connect(jobs.path, timeout=10) as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('CREATE TABLE IF NOT EXISTS generated_runtime_rows ('
                    'row_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, state TEXT NOT NULL)')
        columns = {r[1] for r in con.execute('PRAGMA table_info(generated_runtime_rows)')}
        if 'binding' not in columns:
            con.execute('ALTER TABLE generated_runtime_rows ADD COLUMN binding TEXT')
        if 'source_revision' not in columns:
            con.execute('ALTER TABLE generated_runtime_rows ADD COLUMN source_revision TEXT')
        row = con.execute('SELECT job_id,state,binding,source_revision FROM generated_runtime_rows WHERE row_id=?',
                          (row_id,)).fetchone()
        if binding is not None:
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-astra:' + prep.canonical(binding)))
            if row and row[0] != job_id:
                raise RuntimeHold('generated_row_job_changed')
            if not row:
                con.execute('INSERT INTO generated_runtime_rows(row_id,job_id,state,binding,source_revision) VALUES (?,?,?,?,?)',
                            (row_id, job_id, 'ready', prep.canonical(binding), source_revision))
                row = (job_id, 'ready', prep.canonical(binding), source_revision)
        if state is not None:
            if not row:
                raise RuntimeHold('generated_row_job_unavailable')
            con.execute('UPDATE generated_runtime_rows SET state=? WHERE row_id=?', (state, row_id))
            row = (row[0], state, row[2], row[3])
        if not row:
            return None
        if row[1] not in ('ready', 'committing', 'committed'):
            raise RuntimeHold('generated_row_job_unavailable')
        job = con.execute('SELECT binding,state,candidate FROM generated_jobs WHERE job_id=?',
                          (row[0],)).fetchone()
        frozen_binding = json.loads(row[2]) if row[2] else (json.loads(job[0]) if job else None)
        if frozen_binding is None:
            raise RuntimeHold('generated_row_job_unavailable')
        candidate = prep.validate_candidate(json.loads(job[2])) if job and job[1] == 'prepared' else None
        return dict(state=row[1], job_id=row[0], binding=frozen_binding,
                    job_state=job[1] if job else 'unstarted', candidate=candidate, source_revision=row[3])



def _same_post_job(jobs, row_id, frozen, source_revision):
    """Pin logical IG/FB siblings to one existing execution across spine drift.

    No journal fact authorizes reservation. Current owner facts, strict original
    validation and B's sealed history epoch checks still run for each row.
    """
    with sqlite3.connect(jobs.path, timeout=10) as con:
        rows = con.execute('SELECT row_id,binding,source_revision FROM generated_runtime_rows WHERE row_id<>?',
                           (row_id,)).fetchall()
    matches = []
    keys = tuple(k for k in frozen if k != 'history_revision')
    for other_id, raw, revision in rows:
        if raw is None:
            continue
        binding = json.loads(raw)
        if not all(binding.get(k) == frozen.get(k) for k in ('gym_id','local_date','logical_post_id')):
            continue
        if revision != source_revision or any(binding.get(k) != frozen.get(k) for k in keys):
            raise RuntimeHold('generated_logical_binding_changed')
        matches.append(_runtime_record(jobs, other_id))
    if not matches:
        return None
    if len({r['job_id'] for r in matches}) != 1:
        raise RuntimeHold('generated_logical_job_ambiguous')
    if any(r['state'] == 'committing' for r in matches):
        raise RuntimeHold('generated_owner_commit_uncertain')
    if any(r['job_state'] == 'generating' for r in matches):
        raise RuntimeHold('generated_execution_pending_reconciliation')
    return _runtime_record(jobs, row_id, binding=matches[0]['binding'], source_revision=source_revision)


def run_calendar_row(base, account, row_id, *, persistence, loader=None, jobs=None,
                     provider=None, reviewer=None, storage=None, client_admission=None):
    """Generate one exact persisted feed row and acknowledge owner reservation.

    Missing/changed photos, palette, copy and delivered-byte history hold before
    paid work. A lost commit stays ambiguous; generation journal prevents a new
    provider execution on retry. Successful return establishes reservation only.
    """
    if not enabled():
        return dict(ok=False, held=True, reason='generated_runtime_disabled')
    from .generated_client_admission import AdmissionHold
    committing = False
    try:
        _account_binding(base, account)
        if str(uuid.UUID(row_id)) != row_id:
            raise RuntimeHold('generated_calendar_binding_changed')
        from . import forward_media_guard as guard, forward_media_owner as owner
        if not guard.enabled():
            raise RuntimeHold('generated_forward_authority_disabled')
        from .forward_media_owner_worker import settings_from_environment
        tenants, _ = settings_from_environment()
        if base not in tenants:
            raise RuntimeHold('generated_owner_tenant_not_allowed')
        if type(persistence) is not owner.ForwardMediaOwnerPersistence:
            raise RuntimeHold('generated_owner_required')
        loader = loader or OwnerSnapshotLoader(persistence)
        jobs = jobs or prep.SQLiteGenerationJobs(journal_path())
        from . import generated_client_admission as client_contract
        if client_admission is not None:
            if not client_contract.enabled():
                raise RuntimeHold('generated_client_admission_disabled')
            if (type(client_admission) is not client_contract.GeneratedClientAdmission
                    or os.path.realpath(client_admission.journal.path) != os.path.realpath(jobs.path)):
                raise RuntimeHold('generated_client_durable_binding_required')
        elif client_contract.enabled():
            raise RuntimeHold('generated_client_reader_not_provisioned')
        existing = _runtime_record(jobs, row_id)
        if existing and existing['state'] == 'committing' and client_admission is None:
            raise RuntimeHold('generated_owner_commit_uncertain')
        if existing and existing['job_state'] == 'generating':
            raise RuntimeHold('generated_execution_pending_reconciliation')
        before, visuals = loader.load(row_id, base, account)
        # Late local-photo authority before any paid provider I/O: only the
        # newest trusted current-epoch census at this exact inventory revision
        # governs. An older zero census never overrides a newer positive or
        # incomplete one; SQL repeats this under reservation locks and at send.
        _require_latest_local_depletion(persistence, before.get('gym_id'),
                                        before.get('inventory_revision'))
        request = {k: before[k] for k in prep.BINDING_FIELDS}
        expected_job, frozen = _generation_binding(request, before)
        if not existing:
            existing = _same_post_job(jobs, row_id, frozen, before['approved_source_revision'])
        if existing and existing['source_revision'] != before['approved_source_revision']:
            raise RuntimeHold('generated_approved_source_changed')
        if existing and existing['candidate'] is None and existing['binding'] != frozen:
            # An ambiguous/reviewing execution remains pinned even if unrelated
            # history changes. Never let a new history revision create a new job.
            raise RuntimeHold('generated_execution_pending_reconciliation')
        if (not existing or existing['candidate'] is None) and (provider is None or reviewer is None):
            from .image_engine import OPENAI_API_KEY_ENV
            key = os.getenv(OPENAI_API_KEY_ENV, '')
            if not key:
                raise RuntimeHold('generated_astra_unavailable')
            from .infographic_review import AstraReviewer
            provider = provider or prep.AstraOriginalProvider(key)
            reviewer = reviewer or AstraReviewer(key)
        if not existing:
            existing = _runtime_record(jobs, row_id, binding=frozen,
                                       source_revision=before['approved_source_revision'])
        result = (dict(ok=True, candidate=existing['candidate']) if existing['candidate'] is not None else
                  prep.prepare_candidate(request, before, jobs=jobs, provider=provider,
                                         reviewer=reviewer, storage=storage, enabled=True))
        if not result.get('ok'):
            return result
        candidate = result['candidate']
        # Enforce A's strict job/storage/original shape before B's DB seam.
        data = loader.reader(candidate.get('original_url'))
        prep.validate_candidate(candidate, data)
        current, _ = loader.load(row_id, base, account, observe_history=False)
        # A successful reservation adds its own historical row. Reuse the
        # exact durable job and let B revalidate current history under locks;
        # never regenerate because this row's reservation changed the spine.
        bindings = tuple(k for k in prep.BINDING_FIELDS if existing['candidate'] is None or k != 'history_revision')
        if any(current.get(k) != candidate.get(k) for k in
               (*bindings, 'copy_digest', 'palette_digest', 'authority_pins', 'copy_derivation_receipt')):
            raise RuntimeHold('generated_snapshot_binding_changed')
        if current['approved_source_revision'] != existing['source_revision']:
            raise RuntimeHold('generated_approved_source_changed')
        if candidate['job_id'] != existing['job_id']:
            raise RuntimeHold('generated_row_job_changed')
        reservation = guard.reserve_generated(persistence, row_id, candidate, current,
                                              history_visuals=visuals, read_bytes=loader.reader,
                                              **({'client_admission': client_admission}
                                                 if client_admission is not None else {}))
        if client_admission is not None:
            client_admission.before_commit(row_id)
        _runtime_record(jobs, row_id, state='committing')
        committing = True
        persistence._conn.commit()
        _runtime_record(jobs, row_id, state='committed')
        if client_admission is not None:
            client_admission.committed(row_id)
        result = dict(ok=True, reserved=True, calendar_row_id=row_id,
                      job_id=candidate['job_id'], receipt_ref=reservation.get('receipt_ref'))
        if client_admission is not None:
            result.update(admitted=reservation.get('admitted') is True,
                          prepared=reservation.get('prepared') is True,
                          calendar_row_id=reservation.get('calendar_row_id'),
                          placeholder_row_id=row_id, stage_plan=reservation.get('stage_plan'))
        return result
    except Exception as exc:
        if committing:
            return dict(ok=False, held=True, reason='generated_owner_commit_uncertain')
        try:
            persistence._conn.rollback()
        except Exception:
            pass
        reason = str(exc) if isinstance(exc, (RuntimeHold, prep.PreparationHold, AdmissionHold)) else 'generated_runtime_unavailable'
        return dict(ok=False, held=True, reason=reason)


def run_scheduled(base, account, store, *, logger=None, now=None, days_ahead=2):
    """Dispatch empty photo-depleted dates, never calendar writes or owner DSN."""
    if not enabled():
        return dict(ok=False, held=True, filled=0, reason='generated_runtime_disabled')
    try:
        _account_binding(base, account)
        from . import client_infographic_fill as fill, config
        state, detail = fill.real_media_status(base, now=now)
        if state == fill.MEDIA_AVAILABLE:
            return dict(ok=True, dispatched=0, filled=0, reason='generated_photo_available')
        if state != fill.MEDIA_DEPLETED:
            raise RuntimeHold('generated_photo_inventory_uncertain')
        if store is None or not all(callable(getattr(store, name, None))
                                    for name in ('_client','_rest','_headers','list_month')):
            raise RuntimeHold('generated_gap_owner_transport_missing')
        gaps = fill._empty_upcoming_days(store, base, config.posting_timezone_for(base),
                                         min(max(int(days_ahead),1),2), now=now)
        dispatched = []
        from .accounts import get_account
        for day in gaps:
            # A same-gym connected FB mirror gets its own row request with the
            # exact same logical-post identity derived by the owner.
            platforms = ['instagram']
            facebook = get_account(base+'_fb')
            if facebook is not None:
                _account_binding(base, facebook)
                platforms.append('facebook')
            for platform in platforms:
                request_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    'echo-generated-gap-request:'+base+':'+day+':'+platform+':feed'))
                payload = dict(p_request_id=request_id,p_gym=base,p_local_date=day,
                               p_account=platform,p_format='feed')
                response = store._client().post(store._rest('rpc/fixer_generated_gap_dispatch_20261007'),
                    headers=store._headers({'Content-Type':'application/json'}),json=payload,timeout=30)
                result = response.json()
                if (not 200<=response.status_code<300 or not isinstance(result,dict)
                        or result.get('dispatched') is not True or result.get('request_id') != request_id
                        or any(result.get(k)!=v for k,v in dict(gym_id=base,local_date=day,
                                                              account=platform,format='feed').items())):
                    raise RuntimeHold('generated_gap_dispatch_unavailable')
                dispatched.append(request_id)
        return dict(ok=True, dispatched=len(dispatched), request_ids=dispatched, filled=0)
    except Exception as exc:
        reason = str(exc) if isinstance(exc,RuntimeHold) else 'generated_gap_dispatch_unavailable'
        if logger:
            logger(f'{base}: fresh infographic held ({reason})')
        return dict(ok=False, held=True, filled=0, reason=reason)


def _sql_publish_readback(store, row_id):
    """Narrow read-only reservation binding; the publisher's only authority.

    The owner SQLite journal lives on the owner's own /data volume and is never
    consulted here. Any unavailable/malformed readback fails closed.
    """
    if store is None:
        raise RuntimeHold('generated_publish_binding_unavailable')
    try:
        response = store._client().post(
            store._rest('rpc/fixer_generated_publish_readback_20261007'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json={'p_id': str(uuid.UUID(str(row_id)))}, timeout=30)
        payload = response.json()
        if not 200 <= response.status_code < 300:
            raise ValueError()
        return payload
    except RuntimeHold:
        raise
    except Exception:
        raise RuntimeHold('generated_publish_binding_unavailable') from None



def _publisher_bundle_readback(store, base):
    try:
        if store is None:
            raise ValueError()
        response = store._client().post(store._rest('rpc/fixer_generated_source_brand_active_20261007'),
            headers=store._headers({'Content-Type': 'application/json'}), json={'p_base': base}, timeout=30)
        result = response.json()
        if (not 200 <= response.status_code < 300 or not isinstance(result, dict)
                or result.get('consumer_contract') != BUNDLE_CONTRACT
                or result.get('echo_account_key') != base or not isinstance(result.get('active'), dict)
                or result.get('gym_id') != result['active'].get('bundle', {}).get('gym_id')):
            raise ValueError()
        return result['active']
    except Exception:
        raise RuntimeHold('generated_bundle_publish_bridge_unavailable') from None

def validate_publish_palette(row, *, store=None, readback=None, bundle_reader=None):
    """Check the persisted SQL publish binding and current palette at every
    generated provider boundary.

    The immutable generated reservation readback is the sole publisher
    authority for job/row/gym/account/date/logical/group/original/manifest and
    the configuration and observation pins plus delegated copy derivation; the local owner journal grants
    no publish authority and is not read at this boundary. Missing SQL
    readback, identity drift, or a current bundle/observation/copy mismatch
    fails closed. DB lineage and owned-claim checks remain mandatory.
    """
    asset = str(row.get('source_media_asset_id') or '')
    if not asset.startswith(PREFIX):
        return True
    if not enabled():
        raise RuntimeHold('generated_runtime_disabled')
    from . import forward_media_guard
    if not forward_media_guard.enabled():
        raise RuntimeHold('generated_forward_authority_disabled')
    try:
        job_id = asset[len(PREFIX):]
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
        row_id = str(uuid.UUID(str(row.get('id'))))
        binding = readback(row_id) if readback is not None else _sql_publish_readback(store, row_id)
        if row['account'] not in ('instagram', 'facebook'):
            raise ValueError()
        if (not isinstance(binding, dict)
                or type(binding.get('schema_version')) is not int or binding['schema_version'] != 2
                or binding.get('job_id') != job_id
                or binding.get('calendar_row_id') != row_id
                or binding.get('gym_id') != row.get('gym_id')
                or binding.get('account') != row.get('account')
                or binding.get('local_date') != row.get('post_date')
                or binding.get('logical_post_id') != row.get('logical_post_id')
                or binding.get('group_key') != row.get('visual_group_key')
                or binding.get('original_url') != row.get('image_url')
                or row.get('image_url') != row.get('source_media_url')
                or row.get('thumbnail_url') is not None or row.get('format') != 'feed'
                or binding.get('manifest_digest') != row.get('render_manifest_digest')
                or any(not isinstance(binding.get(key), str) or not binding[key]
                       for key in ('source_revision', 'copy_digest',
                                   'palette_revision', 'palette_digest', 'receipt_ref'))):
            raise ValueError()
        if bundle_reader is None:
            active = _publisher_bundle_readback(store, row['gym_id'])
        else:
            active = bundle_reader(row['gym_id'])
        authority = delegated_copy(active, row['gym_id'], caption=row.get('caption'))
        if (binding.get('authority_pins') != authority['authority_pins']
                or binding.get('copy_derivation_receipt') != authority['copy_derivation_receipt']
                or binding['source_revision'] != authority['source_revision']
                or binding['copy_digest'] != prep.digest(authority['copy'])
                or binding['palette_revision'] != authority['palette_revision']
                or binding['palette_digest'] != prep.digest(authority['palette'])):
            raise RuntimeHold('generated_bundle_publish_binding_changed')
    except RuntimeHold:
        raise
    except Exception:
        raise RuntimeHold('generated_publish_binding_unavailable') from None
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--gym')
    parser.add_argument('--account')
    parser.add_argument('--row')
    args = parser.parse_args(argv)
    if not enabled():
        report = dict(ok=False, held=True, reason='generated_runtime_disabled')
    else:
        persistence = None
        try:
            from .accounts import get_account
            from .forward_media_owner import ForwardMediaOwnerPersistence
            persistence = ForwardMediaOwnerPersistence.connect_from_environment()
            if args.scan:
                from .generated_infographic_gap_owner import run_pending
                report = run_pending(persistence=persistence)
            elif all((args.gym,args.account,args.row)):
                report = run_calendar_row(args.gym, get_account(args.account), args.row,
                                          persistence=persistence)
            else:
                report = dict(ok=False,held=True,reason='generated_gap_request_invalid')
        except Exception:
            report = dict(ok=False, held=True, reason='generated_owner_environment_unavailable')
        finally:
            if persistence is not None:
                persistence._conn.close()
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get('ok') or not enabled() else 2


if __name__ == '__main__':
    raise SystemExit(main())
