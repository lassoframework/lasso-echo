"""DRAFT bounded discovery/binding in the existing isolated owner execution.

Publisher dispatch only enqueues dates; this owner selects supported verbatim
facts and witnessed palette tokens and exact local accounts before atomic row binding. No source
observation bridge, calendar store insert, provider send or approval is used.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from . import generated_infographic_runtime as runtime
from . import generated_infographic_preparation as prep


class GapOwnerTransport:
    def __init__(self, persistence, jobs):
        from .forward_media_owner import ForwardMediaOwnerPersistence
        if type(persistence) is not ForwardMediaOwnerPersistence:
            raise runtime.RuntimeHold('generated_owner_required')
        self.persistence, self.jobs = persistence, jobs

    def rpc(self, operation, args):
        if operation not in ('pending', 'bind_bundle', 'record'):
            raise runtime.RuntimeHold('generated_gap_operation_invalid')
        conn = self.persistence._conn
        info = getattr(conn, 'info', None)
        if info is not None and int(info.transaction_status) != 0:
            raise runtime.RuntimeHold('generated_owner_transaction_busy')
        self.persistence._assert_owner_identity()
        with conn.cursor() as cur:
            cur.execute('select public.fixer_generated_gap_' + operation + '_20261007(' +
                        ','.join(['%s'] * len(args)) + ')', args)
            return cur.fetchone()[0]

    def pending(self, tenants, limit, local_windows):
        try:
            result = self.rpc('pending', (list(tenants), limit, json.dumps(local_windows)))
            if not isinstance(result, list) or len(result) > limit:
                raise runtime.RuntimeHold('generated_gap_discovery_unavailable')
            return result
        finally:
            self.persistence._conn.rollback()

    def phase(self, request_id, *, value=None, binding_digest=None):
        with sqlite3.connect(self.jobs.path, timeout=10) as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('CREATE TABLE IF NOT EXISTS generated_gap_owner_jobs ('
                        'request_id TEXT PRIMARY KEY, phase TEXT NOT NULL, binding_digest TEXT NOT NULL)')
            row = con.execute('SELECT phase,binding_digest FROM generated_gap_owner_jobs WHERE request_id=?',
                              (request_id,)).fetchone()
            if binding_digest is not None:
                if row and row[1] != binding_digest:
                    raise runtime.RuntimeHold('generated_gap_binding_changed')
                if not row:
                    con.execute('INSERT INTO generated_gap_owner_jobs VALUES (?,?,?)',
                                (request_id, 'ready', binding_digest))
                    row = ('ready', binding_digest)
            if value is not None:
                if not row:
                    raise runtime.RuntimeHold('generated_gap_binding_unavailable')
                con.execute('UPDATE generated_gap_owner_jobs SET phase=? WHERE request_id=?', (value, request_id))
                row = (value, row[1])
            return row[0] if row else None

    def bind(self, request, *, caption, source_revision, palette, palette_revision, authority):
        request_id = request['request_id']
        row_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-generated-gap-row:' + request_id))
        logical = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                'echo-generated-gap-post:' + request['gym_id'] + ':' + request['local_date']))
        group = 'vg_generated_' + uuid.UUID(logical).hex
        runtime.validate_authority_pins(authority['authority_pins'], request['gym_id'])
        args = (request_id, row_id, logical, group, caption, source_revision,
                palette_revision, prep.digest(palette), palette['evidence_ref'],
                json.dumps(authority['authority_pins']), json.dumps(authority['copy_derivation_receipt']))
        phase = self.phase(request_id, binding_digest=prep.digest(args))
        if phase in ('committing', 'binding'):
            raise runtime.RuntimeHold('generated_gap_commit_uncertain')
        committing = False
        try:
            # Durable local quarantine precedes DB binding/commit. A crash
            # cannot redispatch an uncertain owner transaction automatically.
            self.phase(request_id, value='binding')
            result = self.rpc('bind_bundle', args)
            if (not isinstance(result, dict) or result.get('bound') is not True
                    or result.get('calendar_row_id') != row_id or result.get('logical_post_id') != logical
                    or any(result.get(k) != request[k] for k in ('gym_id','local_date','account','format'))):
                raise runtime.RuntimeHold('generated_gap_binding_unavailable')
            self.phase(request_id, value='committing')
            committing = True
            self.persistence._conn.commit()
            self.phase(request_id, value='bound')
            return result
        except Exception:
            if committing:
                raise runtime.RuntimeHold('generated_gap_commit_uncertain') from None
            try:
                self.persistence._conn.rollback()
            except Exception:
                raise runtime.RuntimeHold('generated_gap_commit_uncertain') from None
            # Acknowledged rollback establishes zero bound mutation. The same
            # exact content refs can retry later without any provider execution.
            self.phase(request_id, value='ready')
            raise runtime.RuntimeHold('generated_gap_binding_unavailable') from None

    def record(self, request, row_id, result):
        reserved = result.get('ok') is True and result.get('reserved') is True
        reason = None if reserved else result.get('reason', 'generated_runtime_unavailable')
        try:
            value = self.rpc('record', (request['request_id'], row_id, reserved, reason))
            if value is not True:
                raise runtime.RuntimeHold('generated_gap_record_unavailable')
            self.persistence._conn.commit()
        except Exception:
            self.persistence._conn.rollback()
            raise runtime.RuntimeHold('generated_gap_record_unavailable') from None


def run_pending(*, persistence, jobs=None, transport=None, bundle_reader=None, accounts=None,
                row_runner=None, now=None, client_admission=None, issuer_dispatch=None):
    """One finite pass in the existing dedicated owner, default OFF.

    All bundle/account reads complete before bind locks.
    The row runner reloads these facts and uses normal B reservation authority.
    Missing metadata/API/credentials is a hold, never a service-role fallback.
    """
    if not runtime.enabled():
        return dict(ok=False, held=True, reason='generated_runtime_disabled', rows=[])
    from .forward_media_owner_worker import settings_from_environment
    from . import forward_media_guard, accounts as registry, config
    report = []
    try:
        if not forward_media_guard.enabled():
            raise runtime.RuntimeHold('generated_forward_authority_disabled')
        tenants, limit = settings_from_environment()
        jobs = jobs or prep.SQLiteGenerationJobs(runtime.journal_path())
        transport = transport or GapOwnerTransport(persistence, jobs)
        bundle_reader = bundle_reader or (lambda base: runtime._owner_bundle_readback(persistence, base))
        accounts = accounts or registry.get_account
        row_runner = row_runner or runtime.run_calendar_row
        from .calendar_autopublish import _local_now
        from datetime import timedelta
        local_windows = {}
        for base in tenants:
            today = _local_now(now, config.posting_timezone_for(base)).date()
            local_windows[base] = [(today+timedelta(days=i)).isoformat() for i in (1,2)]
        requests = transport.pending(tenants, limit, local_windows)
        for request in requests:
            try:
                base = request['gym_id']
                platform = request['account']
                if (base not in tenants or platform not in ('instagram','facebook')
                        or request.get('format') != 'feed'
                        or str(uuid.UUID(request['request_id'])) != request['request_id']):
                    raise runtime.RuntimeHold('generated_gap_request_invalid')
                if request['local_date'] not in local_windows[base]:
                    raise runtime.RuntimeHold('generated_gap_date_expired')
                account = accounts(base + ('_ig' if platform == 'instagram' else '_fb'))
                runtime._account_binding(base, account)
                authority = runtime.delegated_copy(bundle_reader(base), base,
                    local_date=request['local_date'])
                caption, copy = authority['caption'], authority['copy']
                source_ref = authority['source_revision']
                palette, revision = authority['palette'], authority['palette_revision']
                # Check A's complete copy/palette/style contract before creating
                # a placeholder, without manufacturing photo/history proof.
                from .astra_prompt import build_verified_gym_content_brief
                build_verified_gym_content_brief(base, copy, palette)
                if any(mark in text for text in [copy['headline'], *copy['facts']] for mark in ('-','–','—',':',';')):
                    raise runtime.RuntimeHold('generated_copy_style_invalid')
                bound = transport.bind(request, caption=caption, source_revision=source_ref,
                                       palette=palette, palette_revision=revision, authority=authority)
                # Trusted refs may change between local load and committed bind.
                current = runtime.delegated_copy(bundle_reader(base), base, caption=caption)
                if current != authority:
                    raise runtime.RuntimeHold('generated_gap_binding_changed')
                result = row_runner(base, account, bound['calendar_row_id'],
                                    persistence=persistence, jobs=jobs,
                                    **(dict(client_admission=client_admission, issuer_dispatch=issuer_dispatch)
                                       if client_admission is not None else {}))
                transport.record(request, bound['calendar_row_id'], result)
                report.append(dict(request_id=request['request_id'], **result))
                if result.get('reason') == 'generated_owner_commit_uncertain':
                    break
            except runtime.RuntimeHold as exc:
                report.append(dict(request_id=request.get('request_id'),ok=False,held=True,reason=str(exc)))
                if str(exc) == 'generated_gap_commit_uncertain':
                    break
            except Exception:
                report.append(dict(request_id=request.get('request_id'),ok=False,held=True,
                                   reason='generated_gap_owner_unavailable'))
        return dict(ok=all(row.get('ok') for row in report),rows=report)
    except Exception as exc:
        reason = str(exc) if isinstance(exc,runtime.RuntimeHold) else 'generated_gap_owner_unavailable'
        return dict(ok=False,held=True,reason=reason,rows=report)
