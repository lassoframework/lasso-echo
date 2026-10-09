"""Event Drive photo producer. Candidates only; pinned listener owns consumption.

The default-OFF remote-use switch selects this lane. Deterministic operation
identity precedes selection. A single SQLite transaction owns the byte claim,
frozen exact stage request and use UUID. Legacy media-held rows stay held until
SQL supports their atomic replacement. There are no direct calendar writes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile

from . import gbp_drive_use_journal as journal


def required_gates():
    from . import config, forward_media_observation_bridge as bridge, remote_drive_use
    from .feed_drive_use import _required_gates
    if not remote_drive_use.enabled() or not config.logical_post_id_enabled() or not bridge.enabled():
        raise journal.JournalHold('event_staged_authority_unavailable')
    _required_gates()



def _visual_group_for(operation):
    """Stable event placement group, independent of hosting or process lifetime.

    The logical UUID already binds the event beat/generation/old-row identity.
    Explicit tenant/date fields keep the visual binding scoped to this exact
    operation. This producer value is not a registry or attestation receipt.
    """
    from .local_inventory_mutation import canonical_json
    binding = dict(namespace='echo:event-visual-group:v1', gym_id=operation['gym_id'],
                   post_date=operation['identity']['post_date'],
                   logical_post_id=operation['logical_post_id'])
    return 'vg_' + hashlib.sha256(canonical_json(binding).encode('utf-8')).hexdigest()


def select(gym_id, row, *, media_store=None, exclude_ids=()):
    """Read-only selection with eligible/exhausted/unknown outcomes.

    A complete ready own-gym source snapshot and strict claim/ledger reads are
    prerequisites for exhaustion. Neither an unavailable store nor a partial
    sync is an empty pool. The scheduled date drives scene sibling decisions.
    """
    from . import gym_media_index, gym_media_selector as selector
    store = media_store or gym_media_index.default_store()
    try:
        if not store.available() or selector.base_gym_key(gym_id) != gym_id:
            raise ValueError('store or tenant unavailable')
        assets, sources = store.list_assets(gym_id), store.list_sources(gym_id)
        if not isinstance(assets, list) or not isinstance(sources, list):
            raise ValueError('incomplete inventory')
        active = [s for s in sources if s.get('kind') == 'gym_drive' and s.get('active') is True]
        if not active or any(s.get('gym_id') != gym_id or s.get('revoked_externally')
                             or s.get('sync_status') != 'ready' or not s.get('sync_finished_at')
                             for s in active):
            raise ValueError('incomplete source sync')
        class Snapshot:
            def available(self):
                return True
            def list_assets(self, _gym):
                return assets
            def list_sources(self, _gym, include_inactive=False):
                return sources
        candidates = selector.pickable(gym_id, kind_preference='photo', store=Snapshot(),
                                       strict_claims=True, exclude_ids=exclude_ids,
                                       post_date=row['post_date'])
        if not candidates:
            return dict(state='exhausted', store=store)
        return dict(state='eligible', asset=candidates[0], store=store)
    except Exception:
        return dict(state='unknown', store=store)


def materialize(gym_id, row, asset, media_store, calendar_store):
    """Exact own-gym original and deterministic feed derivative evidence.

    These are unverified producer observations, never owner receipts. The
    separate attester/finalizer independently checks the hosted objects.
    Unsupported original formats and unknown reservation reads hold.
    """
    from . import gbp_planner, gym_media_index, gym_media_builder
    from . import forward_media_attester as attester, forward_media_visual_index
    from . import media_host
    from .integrations.drive_client import DriveClient
    snapshots = gbp_planner._authoritative_drive_snapshots(dict(base=gym_id, asset=asset, store=media_store))
    if snapshots is None:
        raise journal.JournalHold('event_original_snapshot_unavailable')
    before, source = snapshots
    drive = DriveClient()
    if not drive.available():
        raise journal.JournalHold('event_original_download_unavailable')
    with tempfile.TemporaryDirectory(prefix='event-drive-') as work:
        original = Path(work) / 'original.jpg'
        drive.download(str(before.get('drive_file_id') or before['id']), original)
        raw = gym_media_index.bounded_materialization_bytes(original)
        if hashlib.md5(raw).hexdigest() != before['content_hash'].lower():
            raise journal.JournalHold('event_original_checksum_mismatch')
        from PIL import Image
        import io
        from . import feed_image
        with Image.open(io.BytesIO(raw)) as image:
            recipe_name = ('feed_autofit_4x5' if feed_image.needs_autofit(*image.size)
                           else 'identity')
        recipe = attester.make_still_recipe(recipe_name)
        rendered = attester.replay_still_recipe(raw, recipe)
        if rendered['thumbnail_bytes'] is not None:
            raise journal.JournalHold('event_derivative_contract_unavailable')
        phash = forward_media_visual_index.phash_v1(raw)
        if phash is None:
            raise journal.JournalHold('event_original_phash_unavailable')
        authority = getattr(calendar_store, '_s', calendar_store)
        screen = authority.check_reservation_conflicts(
            gym_id, post_date=row['post_date'], logical_post_id=row['logical_post_id'],
            source_sha256=hashlib.sha256(raw).hexdigest(), phash_v1=phash)
        if not isinstance(screen, dict) or screen.get('allowed') is not True:
            raise journal.JournalHold('event_reservation_not_clear')
        derivative = Path(work) / 'feed.jpg'
        derivative.write_bytes(rendered['image_bytes'])
        source_url = media_host.host_media(str(original), gym_id)
        delivered_url = media_host.host_media(str(derivative), gym_id)
        if not source_url or not delivered_url:
            raise journal.JournalHold('event_hosted_media_unavailable')
        observation = gym_media_builder.still_materialization_observation(
            raw, rendered['image_bytes'], delivered_url, tenant=gym_id,
            source_asset_id=before['id'], source_url=source_url, image_name=recipe_name)
    fresh = gbp_planner._authoritative_drive_snapshots(dict(base=gym_id, asset=asset, store=media_store))
    if fresh != snapshots:
        raise journal.JournalHold('event_source_changed_during_materialization')
    return dict(image_url=delivered_url, source_media_url=source_url,
                source_media_asset_id=before['id'], observation=observation,
                reservation_proof=dict(source_sha256=hashlib.sha256(raw).hexdigest(),
                                       phash_v1=phash, source_media_asset_id=before['id']),
                asset_before=before, source_before=source)


class _AtomicStage:
    def __init__(self, operation, media, media_store):
        self.operation, self.media, self.media_store = operation, media, media_store

    def __call__(self, request):
        # The writer invokes this only after freeze_atomic. Assert persisted
        # bytes; never alter its canonical request or acquire another claim.
        required_gates()
        bound = journal.forward_stage_for_member(request['members'][0]['row']['id'])
        import json
        if bound is None or json.loads(bound['request_text']) != request:
            raise journal.JournalHold('event_frozen_wire_mismatch')

    def freeze_atomic(self, attempt, raw_tenant):
        import json
        from . import gbp_planner, gym_media_selector
        required_gates()
        if raw_tenant != self.operation['gym_id']:
            raise journal.JournalHold('event_raw_tenant_mismatch')
        row = json.loads(attempt['request_text'])['members'][0]['row']
        if row.get('visual_group_key') != _visual_group_for(self.operation):
            raise journal.JournalHold('event_visual_group_mismatch')
        asset, source = self.media['asset_before'], self.media['source_before']
        fresh = gbp_planner._authoritative_drive_snapshots(
            dict(base=raw_tenant, asset=asset, store=self.media_store))
        if fresh != (asset, source):
            raise journal.JournalHold('event_fresh_source_mismatch')
        aliases = self.media_store.list_assets(raw_tenant)
        if not isinstance(aliases, list):
            raise journal.JournalHold('event_alias_inventory_unknown')
        chosen_hash = gym_media_selector._byte_hash(asset)
        if not chosen_hash:
            raise journal.JournalHold('event_selected_hash_unusable')
        ids = []
        for alias in aliases:
            if (not isinstance(alias, dict) or alias.get('gym_id') != raw_tenant
                    or not isinstance(alias.get('id'), str) or not alias['id']):
                raise journal.JournalHold('event_alias_inventory_unknown')
            alias_hash = gym_media_selector._byte_hash(alias)
            if not alias_hash:
                raise journal.JournalHold('event_alias_hash_unusable')
            if alias_hash == chosen_hash:
                ids.append(alias['id'])
        request = dict(gym_id=raw_tenant, logical_post_id=self.operation['logical_post_id'],
                       claim_id=gym_media_selector.drive_content_claim_id(raw_tenant, asset),
                       post_date=row['post_date'], content_hash=asset['content_hash'],
                       asset_id=asset['id'], source_id=source['id'], calendar_row=row, payload=row,
                       asset_before=asset, source_before=source,
                       epoch_id=gbp_planner._drive_use_epoch_id(raw_tenant))
        journal.freeze_event_stage(self.operation['operation_key'], attempt, request,
                                   alias_asset_ids=ids)


def stage_rows(store, gym_id, event_id, rows, log, *, generations=None, old_rows=None):
    """Stage one exact event member per operation; never stamp or finalize here."""
    from . import portal_calendar_store as pcs, forward_media_observation_bridge as bridge
    from .event_calendar import _db_row, _slot_key
    result = dict(staged=0, held=0, exhausted=0, unknown=0)
    try:
        required_gates()
    except Exception:
        return dict(result, held=len(rows), unknown=len(rows))
    authority = getattr(store, '_s', store)
    generations = generations or {}
    seen = set()
    for proposed in rows:
        try:
            old = (old_rows or {}).get(proposed.get('id'))
            if old is not None and (old.get('status') not in ('pending', 'draft')
                                    or old.get('media_not_ready_reason') is not None):
                raise journal.JournalHold('event_legacy_backfill_sql_milestone_2_hold')
            identity = dict(gym_id=gym_id, event_id=str(event_id),
                            beat=str(proposed.get('arc_kind') or 'event'),
                            post_date=proposed['post_date'], account=proposed.get('account'),
                            format=proposed.get('format'),
                            generation=generations.get(_slot_key(proposed), 0),
                            old_row_id=old.get('id') if old else None)
            input_row = _db_row(proposed)
            supplied_group = input_row.pop('visual_group_key', None)
            operation = journal.event_operation(identity, input_row)
            group = _visual_group_for(operation)
            if (supplied_group is not None and supplied_group != group
                    or operation['input_row'].get('visual_group_key') not in (None, group)):
                raise journal.JournalHold('event_visual_group_mismatch')
            if operation['operation_key'] in seen:
                raise journal.JournalHold('event_duplicate_operation')
            seen.add(operation['operation_key'])
            if operation['batch_id']:
                bound = journal.get_forward_stage(operation['batch_id'])
                import json
                frozen_row = json.loads(bound['request_text'])['members'][0]['row']
                if frozen_row.get('visual_group_key') != group:
                    raise journal.JournalHold('event_frozen_visual_group_mismatch')
                if bound['state'] == 'stage_intent':
                    authority.replay_frozen_event_stage(operation['batch_id'], gym_id)
                result['staged'] += 1
                continue
            row = dict(operation['input_row'], visual_group_key=group)
            pick = select(gym_id, row)
            if pick['state'] != 'eligible':
                result['held'] += 1
                result[pick['state']] += 1
                continue
            media = materialize(gym_id, row, pick['asset'], pick['store'], store)
            row.update({k: media[k] for k in ('image_url', 'source_media_url', 'source_media_asset_id')})
            row[pcs.RESERVATION_PROOF] = media['reservation_proof']
            row[bridge.METADATA] = [media['observation']]
            store.insert_rows(gym_id, [row], expected_old_rows=[old] if old else [],
                              before_forward_stage=_AtomicStage(operation, media, pick['store']))
            # This is an inactive candidate receipt; no consumption/approval.
            result['staged'] += 1
        except Exception as exc:
            result['held'] += 1
            result['unknown'] += 1
            log(f'event Drive operation held ({type(exc).__name__}); frozen state retained')
    return result


def recover(*, store=None, logger=None, tenant_id=None):
    from .jobs.event_drive_use_recovery import run
    return run(store=store, logger=logger, tenant_id=tenant_id)
