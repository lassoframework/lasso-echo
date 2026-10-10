"""Default-OFF service bridge from persisted admission to inactive staging.

No issuer, owner connection, approval, finalization or calendar mutation API is
exposed. Both the admission journal and SQL preparation must agree exactly.
"""
from __future__ import annotations

import hashlib
import json
import os

from .generated_client_admission import ClientAdmissionJournal, _uuid
from .portal_calendar_store import (
    SupabaseCalendarStore, _canonical_json, forward_batch_identity,
    gbp_staged_journal_flag,
)

FLAG = 'AGENT_GENERATED_CLIENT_SERVICE_STAGE_BRIDGE'
PREPARATION_RPC = 'generated_client_service_preparation_20261009'


class ServiceStageHold(RuntimeError):
    """Static diagnostics only; never propagate source or transport details."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


class GeneratedClientServiceStageBridge:
    """One exact frozen admission, staged through the existing service store.

    SQL verifies the service principal, live control and current preparation.
    The existing durable staged journal must be explicitly armed by the caller.
    A retry reconstructs the identical request from persisted manifest bytes;
    an uncertain write is read back once, never retried within this call.
    """
    def __init__(self, *, journal, store):
        if type(journal) is not ClientAdmissionJournal or type(store) is not SupabaseCalendarStore:
            raise ServiceStageHold('generated_client_service_dependencies_required')
        self.journal = journal
        self.store = store

    def stage(self, placeholder_row_id):
        if not enabled():
            raise ServiceStageHold('generated_client_service_stage_disabled')
        if gbp_staged_journal_flag() is not True:
            raise ServiceStageHold('generated_client_service_durable_stage_required')
        try:
            placeholder = _uuid(placeholder_row_id)
            frozen = self.journal.load(placeholder)
            raw = json.loads(frozen['manifest_bytes'])
            binding = frozen['binding']
            tenant = raw['gym_id']
            row_id = _uuid(raw['calendar_row_id'])
            version = _uuid(frozen['artifact_version_id'])
            receipt = _uuid(frozen['receipt_id'])
            plan = binding['stage_plan']
            planned, old = plan['planned_row'], plan['old_snapshot']
            expected_raw = dict(**binding, gym_id=tenant, artifact_version_id=version,
                                hosted_url=binding['candidate']['original_url'],
                                delivered_sha256=binding['candidate']['original_sha256'])
            if (raw != expected_raw or raw['stage_plan'] != plan
                    or not isinstance(tenant, str) or not tenant or tenant.strip() != tenant
                    or binding['candidate']['gym_id'] != tenant
                    or binding['calendar_row_id'] != row_id
                    or plan['placeholder_row_id'] != placeholder
                    or old['id'] != placeholder or old['gym_id'] != tenant
                    or planned['id'] != row_id or planned['gym_id'] != tenant
                    or row_id == placeholder or planned.get('observation') is not None
                    or 'observation' in planned):
                raise ValueError()
            manifest_sha = hashlib.sha256(frozen['manifest_bytes']).hexdigest()
            preparation = self.store._reservation_rpc(PREPARATION_RPC, {
                'p_tenant': tenant, 'p_row': row_id, 'p_version': version,
                'p_receipt': receipt, 'p_manifest_sha': manifest_sha,
                'p_stage_plan': plan,
            }, timeout=30)
            if (not isinstance(preparation, dict)
                    or any(preparation.get(key) is not True for key in ('admitted', 'reserved', 'prepared'))
                    or preparation != dict(admitted=True, reserved=True, prepared=True,
                                   calendar_row_id=row_id, artifact_version_id=version,
                                   receipt_id=receipt, manifest_sha256=manifest_sha,
                                   stage_plan=plan)):
                raise ValueError()
            # Same canonical request construction as the existing stage API.
            request = dict(tenant_id=tenant,
                           members=[dict(row=planned, observation=None)], old_rows=[old])
            text = _canonical_json(request)
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            batch = forward_batch_identity(tenant, digest)
            attempt = dict(batch_id=batch, tenant_id=tenant, request_digest=digest,
                           member_row_ids=[row_id], old_row_ids=[placeholder])
        except Exception:
            raise ServiceStageHold('generated_client_service_preparation_unverified') from None

        def exact_request(value):
            # Called by the existing store after durable freezing and before
            # sending: tenant aliases or altered request bytes cannot stage.
            if value != request or _canonical_json(value) != text:
                raise ServiceStageHold('generated_client_service_request_changed')

        # Avoid accepting a previous call's stale attempt after a pre-write hold.
        self.store.last_forward_stage_attempt = None
        try:
            self.store.stage_forward_schedule_batch(
                tenant, [planned], [old], before_forward_stage=exact_request)
        except Exception:
            if self.store.last_forward_stage_attempt != attempt:
                raise ServiceStageHold('generated_client_service_stage_unverified') from None
            # Lost ACK, malformed receipt, journal hold or definite refusal:
            # only exact persisted status may establish this call's outcome.
        try:
            status = self.store.forward_schedule_batch_status(
                batch, tenant_id=tenant, request_digest=digest,
                member_row_ids=[row_id], old_row_ids=[placeholder])
            if (not isinstance(status, dict)
                    or any(status.get(key) != value for key, value in attempt.items())
                    or status.get('state') != 'staged'
                    or status.get('observation_row_ids') != []
                    or status.get('finalize_receipt') is not None):
                raise ValueError()
            from . import gbp_drive_use_journal as stage_journal
            stage_journal.record_forward_stage_receipt(batch, status)
            return status
        except Exception:
            raise ServiceStageHold('generated_client_service_stage_unverified') from None
