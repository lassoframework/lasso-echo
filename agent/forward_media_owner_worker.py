"""Default-OFF owner worker; production credentials/factory remain unprovisioned.

DedicatedOwnerTransport implements DRAFT discovery, lock-free remote byte
verification, exact final locks and durable progress. It commits source/history
receipts+hold outcome together and preserves quarantine on crashes/unknown
commits. Unknown history never creates the asset's unique immutable authority
clearance, so a later reviewed positive audit can prepare that authority.
Producer observations and used_count=0 never establish authority.
The generic adapter also supports offline fixtures; those are not live proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid

from . import forward_media_owner as owner
from . import forward_media_owner_packet as packet
from .forward_media_attester import validate_still_recipe, replay_still_recipe
from .forward_media_prepare import register_original

WORKER_ENV = 'AGENT_FORWARD_MEDIA_OWNER_WORKER'
TENANTS_ENV = 'AGENT_FORWARD_MEDIA_OWNER_TENANTS'
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_FORBIDDEN = ('AGENT_SOCIALAPI_KEY', 'AGENT_SOCIALAPI_ENC_KEY', 'ZERNIO_API_KEY',
              'AGENT_GBP_ACCESS_TOKEN', 'AGENT_FORWARD_MEDIA_ATTESTER_DSN',
              'AGENT_VISUAL_RECEIPT_OWNER_DSN')


_REASONS = frozenset({
    'owner_transport_schema_missing', 'verified_history_transport_missing',
    'durable_progress_transport_missing', 'owner_environment_invalid',
    'explicit_tenant_allowlist_required', 'worker_bounds_invalid',
    'candidate_identity_invalid', 'canonical_revision_changed',
    'canonical_tenant_asset_mismatch', 'calendar_not_unsent_candidate',
    'candidate_canonical_binding_invalid', 'thumbnail_candidate_contract_missing',
    'render_bytes_mismatch', 'verified_byte_history_required',
    'source_changed_after_history_verification', 'candidate_batch_invalid',
    'authority_commit_unverified', 'uncertain_authority_commit',
    'durable_progress_commit_unverified', 'owner_asset_source_binding_missing',
    'owner_manual_reconciliation_required', 'owner_transaction_contract_required',
    'verified_local_byte_cache_required', 'owner_source_snapshot_invalid',
    'owner_source_binding_changed', 'source_binding_invalid', 'drive_identity_invalid',
    'drive_metadata_unavailable', 'drive_folder_membership_unknown',
    'drive_original_unavailable', 'source_object_exceeds_bound',
    'drive_original_bytes_mismatch', 'drive_original_changed_during_read',
    'hosted_source_differs_from_drive_original', 'source_verification_unavailable',
    'source_read_deadline_exceeded', 'drive_timeout_contract_unavailable',
    'historical_original_bytes_unknown', 'preexisting_original_has_no_fresh_production_proof',
    'historical_scan_bound_exceeded', 'trusted_original_bytes_previously_used',
})


class OwnerWorkerHold(RuntimeError):
    """Static reason code; never emit driver errors, packet data or credentials."""

    def __init__(self, reason):
        super().__init__(reason if reason in _REASONS else 'owner_transport_unavailable')


class OwnerTransport:
    """Abstract trusted owner infrastructure, never a producer callback.

    pending(tenants, limit): finite sequence of row_id/revision/observation_digest.
    locked_current(candidate): context manager yielding current canonical row,
      asset, observation and revision; maintain locks until outcome is recorded.
    verified_history(original): independently checked owner decision/evidence,
      bound to original.row() exactly; literal verified=True alone is inadequate.
    record(candidate, outcome): stage idempotent exact outcome. Atomic concrete
      transports commit it on normal locked_current exit with staged authority.
      Errors/unknown commit stop the pass for manual reconciliation.
    Never expose these operations through a service/publisher credential client.
    """

    def pending(self, tenants, limit):
        raise OwnerWorkerHold('owner_transport_schema_missing')

    def locked_current(self, candidate):
        raise OwnerWorkerHold('owner_transport_schema_missing')

    def verified_history(self, original):
        raise OwnerWorkerHold('verified_history_transport_missing')

    def record(self, candidate, outcome):
        raise OwnerWorkerHold('durable_progress_transport_missing')


def worker_enabled():
    return os.getenv(WORKER_ENV, '').lower() in ('1', 'true', 'yes', 'on')


def settings_from_environment():
    try:
        owner.check_environment()
    except owner.OwnerPersistenceError:
        raise OwnerWorkerHold('owner_environment_invalid') from None
    role = os.getenv('FORWARD_MEDIA_OWNER_ROLE', '').strip()
    if (not role or role in ('anon', 'authenticated', 'service_role',
                            'fixer_forward_media_attester_20261006')
            or any(os.getenv(name) for name in _FORBIDDEN)):
        raise OwnerWorkerHold('owner_environment_invalid')
    tenants = tuple(dict.fromkeys(t.strip() for t in os.getenv(TENANTS_ENV, '').split(',') if t.strip()))
    if not tenants or len(tenants) > 32 or any(not _TENANT.fullmatch(t) for t in tenants):
        raise OwnerWorkerHold('explicit_tenant_allowlist_required')
    try:
        batch = int(os.getenv('AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', '25'))
    except ValueError:
        raise OwnerWorkerHold('worker_bounds_invalid') from None
    if not 1 <= batch <= 100:
        raise OwnerWorkerHold('worker_bounds_invalid')
    return tenants, batch


def _identity(candidate):
    try:
        row_id = str(uuid.UUID(candidate['calendar_row_id']))
        revision = candidate['revision']
        digest = candidate['observation_digest']
        if (not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{32}', revision)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError()
        return row_id, revision, digest
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OwnerWorkerHold('candidate_identity_invalid') from None


def _candidate_context(candidate, current, tenants, *, asset_binding=True):
    row_id, revision, digest = _identity(candidate)
    if current.get('hold_reason'):
        raise OwnerWorkerHold(current['hold_reason'])
    row, asset, observation = current['calendar'], current['asset'], current['observation']
    if current.get('revision') != revision or row.get('id') != row_id:
        raise OwnerWorkerHold('canonical_revision_changed')
    tenant = row.get('gym_id')
    if (tenant not in tenants or asset.get('gym_id') != tenant
            or row.get('source_media_asset_id') != asset.get('id')):
        raise OwnerWorkerHold('canonical_tenant_asset_mismatch')
    if (row.get('status') not in ('draft', 'pending', 'queued', 'approved')
            or row.get('variant_status') != 'active' or not row.get('post_date')
            or not row.get('visual_group_key')
            or any(row.get(k) is not None for k in ('publish_claim_token', 'published_at',
                                                   'late_post_id', 'render_manifest_digest'))):
        raise OwnerWorkerHold('calendar_not_unsent_candidate')
    if asset_binding and (not asset.get('source_url') or not asset.get('registry_evidence_ref')):
        raise OwnerWorkerHold('owner_asset_source_binding_missing')
    source_url = asset.get('source_url') if asset_binding else row.get('source_media_url')
    # Account-style producer tenant aliases cannot establish canonical ownership.
    # Unresolved alias candidates hold; a future reviewed canonical mapping
    # contract must preserve the immutable raw observation and its digest.
    observed = {k: v for k, v in observation.items() if k != 'observation_digest'}
    computed = hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()
    if (computed != digest or observation.get('observation_digest') != digest
            or type(observation.get('schema_version')) is not int
            or observation['schema_version'] != 1
            or observation.get('provenance_status') != 'unverified'
            or observation.get('tenant') != tenant
            or observation.get('source_asset_id') != asset.get('id')
            or observation.get('source_exact_url') != row.get('source_media_url')
            or observation.get('source_exact_url') != source_url
            or observation.get('delivered_exact_url') != row.get('image_url')):
        raise OwnerWorkerHold('candidate_canonical_binding_invalid')
    recipe = validate_still_recipe(observation.get('recipe'))
    if row.get('thumbnail_url') is not None:
        # Current producer schema has no thumbnail observations/binding.
        raise OwnerWorkerHold('thumbnail_candidate_contract_missing')
    return row, asset, observation, recipe


def _prepare(candidate, current, transport, reader, tenants):
    """Legacy offline fixture adapter; never used by dedicated owner runtime."""
    row, asset, observation, recipe = _candidate_context(candidate, current, tenants)
    tenant = row['gym_id']
    source_bytes = reader.read(asset['source_url'])
    replayed = replay_still_recipe(source_bytes, recipe)
    if (replayed['image_bytes'] != reader.read(row['image_url'])
            or replayed['thumbnail_bytes'] is not None):
        raise OwnerWorkerHold('render_bytes_mismatch')
    original = register_original(tenant, asset['id'], asset['source_url'],
                                 source_bytes, asset['registry_evidence_ref'])
    history = transport.verified_history(original)
    if (not isinstance(history, dict) or history.get('original') != original.row()
            or history.get('decision') not in ('cleared_unused', 'hold_used', 'hold_uncertain')):
        raise OwnerWorkerHold('verified_byte_history_required')
    operation = ('same_object' if row['image_url'] == asset['source_url'] else
                 'rehost' if recipe['image']['name'] == 'identity' else 'render')
    prepared = packet.build_tuples({
        'schema_version': 2, 'tenant_id': tenant, 'source_asset_id': asset['id'],
        'source_url': asset['source_url'], 'image_url': row['image_url'],
        'registry_evidence_ref': asset['registry_evidence_ref'],
        'render_evidence_ref': current['render_evidence_ref'],
        'decision': history['decision'], 'history_evidence_ref': history.get('history_evidence_ref'),
        'production_evidence_ref': history.get('production_evidence_ref'),
        'operation': operation, 'render_recipe': recipe,
    }, reader)
    if prepared[0] != original:
        raise OwnerWorkerHold('source_changed_after_history_verification')
    return prepared


def _prepare_remote(candidate, current, reader, drive_reader, tenants):
    """All source fetches, delivered reads and expensive render replay here."""
    from .forward_media_source_verifier import verify_source
    row, asset, observation, recipe = _candidate_context(candidate, current, tenants, asset_binding=False)
    verified = verify_source(current, drive_reader, reader)
    source_bytes = verified.source_bytes
    image_bytes = source_bytes if row['image_url'] == row['source_media_url'] else reader.read(row['image_url'])
    replayed = replay_still_recipe(source_bytes, recipe)
    if replayed['image_bytes'] != image_bytes or replayed['thumbnail_bytes'] is not None:
        raise OwnerWorkerHold('render_bytes_mismatch')
    # There is no positive history authority yet: verify the replay here but
    # leave registry/clearance/manifest empty until the independent audit exists.
    return verified


def _run_dedicated_candidate(candidate, transport, persistence, reader, drive_reader, tenants):
    """Reserve → remote verification without tx → final recheck/atomic commit."""
    from .forward_media_source_history import SourceHistoryStore
    from .forward_media_source_verifier import SourceVerificationHold
    key = _identity(candidate)
    store = SourceHistoryStore(persistence)
    with transport.reserved_current(candidate) as snapshot:
        prepared, remote_reason = None, None
        try:
            if snapshot.get('hold_reason'):
                raise OwnerWorkerHold(snapshot['hold_reason'])
            prepared = _prepare_remote(candidate, snapshot, reader, drive_reader, tenants)
        except (OwnerWorkerHold, SourceVerificationHold) as exc:
            remote_reason = str(OwnerWorkerHold(str(exc)))
        except Exception:
            remote_reason = 'candidate_verification_failed'
        with transport.locked_current(candidate) as final:
            if final.get('hold_reason'):
                outcome = {'status': 'hold', 'reason': str(OwnerWorkerHold(final['hold_reason']))}
            elif remote_reason:
                outcome = {'status': 'hold', 'reason': remote_reason}
            elif final.get('binding_revision') != snapshot.get('binding_revision'):
                outcome = {'status': 'hold', 'reason': 'owner_source_binding_changed'}
            else:
                # Any SQL failure from here must roll back the whole final phase
                # and retain quarantine; never turn a poisoned tx into a hold.
                verified = prepared
                original = store.stage_source(verified)
                history = store.history(original)
                if (not isinstance(history, dict) or history.get('original') != original.row()
                        or history.get('decision') not in ('hold_used','hold_uncertain')):
                    raise OwnerWorkerHold('verified_byte_history_required')
                # Keep uncertainty in durable source/history/progress receipts.
                # The asset's unique immutable authority clearance must remain
                # empty so a later reviewed positive audit can prepare it.
                # This bounded history transport has NO positive-clearance path.
                outcome = {'status': 'hold', 'decision': history['decision'],
                           'reason': str(OwnerWorkerHold(history['reason'])),
                           'source_receipt_ref': verified.receipt_ref,
                           'history_evidence_ref': history['history_evidence_ref']}
            if transport.record(candidate, outcome) is not True:
                raise OwnerWorkerHold('durable_progress_commit_unverified')
            report = {'calendar_row_id': key[0], 'revision': key[1], **outcome}
    return report


def run_adapter(*, transport, persistence, reader, drive_reader=None):
    """One bounded owner-only pass for infrastructure integration/offline tests.

    Caller owns connection close. No production factory accepts overrides. All
    failures are static holds. Durable progress failure stops further writes.
    Authority replay uses the existing exact immutable persistence comparison.
    """
    if not worker_enabled():
        return {'status': 'disabled', 'rows': []}
    rows = []
    try:
        tenants, limit = settings_from_environment()
        atomic = getattr(transport, 'atomic_authority_outcome', False) is True
        dedicated = isinstance(persistence, owner.ForwardMediaOwnerPersistence)
        if not atomic:
            persistence._assert_owner_identity()
        if dedicated:
            from .forward_media_owner_transport import DedicatedOwnerTransport
            if type(transport) is not DedicatedOwnerTransport or transport.persistence is not persistence:
                # A caller-supplied boolean cannot attest shared transaction semantics.
                raise OwnerWorkerHold('owner_transaction_contract_required')
        candidates = transport.pending(tenants, limit)
        if not isinstance(candidates, (list, tuple)) or len(candidates) > limit:
            raise OwnerWorkerHold('candidate_batch_invalid')
        seen = set()
        for candidate in candidates:
            key = _identity(candidate)
            if key in seen:
                continue
            seen.add(key)
            if dedicated:
                from .forward_media_source_verifier import OriginalDriveReader
                rows.append(_run_dedicated_candidate(candidate, transport, persistence, reader,
                    drive_reader if drive_reader is not None else OriginalDriveReader(), tenants))
                continue
            with transport.locked_current(candidate) as current:
                authority_started = False
                try:
                    tuples = _prepare(candidate, current, transport, reader, tenants)
                    authority_started = True
                    if atomic:
                        # Staged result is only reported after context COMMIT. SQL
                        # errors must abort; no successful hold on a poisoned tx.
                        result = transport.stage_authority(candidate, persistence, tuples)
                    else:
                        result = persistence.persist(*tuples)
                    if not isinstance(result, dict) or type(result.get('replayed')) is not bool:
                        raise OwnerWorkerHold('authority_commit_unverified')
                    outcome = {'status': 'persisted',
                               'decision': tuples[1].decision,
                               'manifest_digest': tuples[2].manifest_digest}
                except owner.UncertainCommitError:
                    # Do not proceed to another asset or try again automatically.
                    raise OwnerWorkerHold('uncertain_authority_commit') from None
                except OwnerWorkerHold as exc:
                    if atomic and authority_started:
                        raise
                    if str(exc) == 'authority_commit_unverified':
                        raise
                    outcome = {'status': 'hold', 'reason': str(exc)}
                except packet.PacketError as exc:
                    if atomic and authority_started:
                        raise OwnerWorkerHold('authority_commit_unverified') from None
                    outcome = {'status': 'hold', 'reason': exc.reason}
                except Exception:
                    if atomic and authority_started:
                        raise OwnerWorkerHold('authority_commit_unverified') from None
                    outcome = {'status': 'hold', 'reason': 'candidate_verification_failed'}
                try:
                    recorded = transport.record(candidate, outcome)
                except Exception:
                    raise OwnerWorkerHold('durable_progress_commit_unverified') from None
                if recorded is not True:
                    raise OwnerWorkerHold('durable_progress_commit_unverified')
                row_report = {'calendar_row_id': key[0], 'revision': key[1], **outcome}
                if outcome['status'] == 'persisted':
                    row_report['replayed'] = result['replayed']
            # The concrete transport commits on context exit. Never report a
            # staged outcome as durable before that commit returns successfully.
            rows.append(row_report)
    except owner.UncertainCommitError:
        return {'status': 'hold', 'reason': 'uncertain_authority_commit', 'rows': rows}
    except OwnerWorkerHold as exc:
        return {'status': 'hold', 'reason': str(exc), 'rows': rows}
    except Exception:
        return {'status': 'hold', 'reason': 'owner_transport_unavailable', 'rows': rows}
    return {'status': 'partial_hold' if any(r['status'] == 'hold'
            or (dedicated and r.get('decision') in ('hold_used','hold_uncertain')) for r in rows) else 'complete',
            'rows': rows}


def run_once():
    """Production remains blocked until a separately reviewed transport exists."""
    if not worker_enabled():
        return {'status': 'disabled', 'rows': []}
    try:
        settings_from_environment()
    except OwnerWorkerHold as exc:
        return {'status': 'hold', 'reason': str(exc), 'rows': []}
    return {'status': 'hold', 'reason': 'owner_transport_schema_missing', 'rows': []}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    report = run_once()
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'disabled' else 2


if __name__ == '__main__':
    raise SystemExit(main())
