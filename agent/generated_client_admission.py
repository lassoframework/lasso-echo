"""Default-OFF consumer composition using committed authority and existing owner.

Exports a durable issuance request. A separately provisioned issuer returns its
receipt UUID. This module never issues or receives an issuer credential. Generic
draft/mirror consumers retain their unconditional hold.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid

from . import generated_infographic_preparation as prep
from .generated_hosted_byte_authority import GeneratedHostedByteAuthority, HostedByteHold

FLAG = 'AGENT_GENERATED_CLIENT_ADMISSION'


class AdmissionHold(RuntimeError):
    """Static diagnostic only; no credentials or source data."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise AdmissionHold('generated_client_identity_invalid') from None


def candidate_row(placeholder_id, job_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-generated-client-candidate:' + _uuid(placeholder_id) + ':' + _uuid(job_id)))


def artifact_version(row_id, job_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-generated-client:' + _uuid(row_id) + ':' + _uuid(job_id)))


class ClientAdmissionJournal:
    """Original manifest bytes and identities frozen BEFORE issuer dispatch.

    Receipt UUID attachment is immutable. Uncertain owner COMMIT stays quarantined
    until exact readback; no new version/manifest is generated. Uses the existing
    durable generation journal, never a second runtime path.
    """
    def __init__(self, jobs):
        if type(jobs) is not prep.SQLiteGenerationJobs:
            raise AdmissionHold('generated_client_durable_journal_required')
        self.path = jobs.path
        with self._connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS generated_client_admissions ('
                        'row_id TEXT PRIMARY KEY, binding TEXT NOT NULL, '
                        'version_id TEXT NOT NULL, manifest_bytes BLOB NOT NULL, '
                        'receipt_id TEXT, state TEXT NOT NULL)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def freeze(self, row_id, candidate, manifest, source_revision, *, stage_plan=None):
        row_id = _uuid(row_id)
        prep.validate_candidate(candidate)
        if candidate['schema_version'] != 2:
            raise AdmissionHold('generated_client_bundle_required')
        candidate_row_id = row_id if stage_plan is None else _uuid(stage_plan['planned_row']['id'])
        binding = dict(calendar_row_id=candidate_row_id, candidate=candidate,
                       reservation_manifest=manifest, source_revision=source_revision)
        if stage_plan is not None:
            if stage_plan.get('placeholder_row_id') != row_id or candidate_row_id == row_id:
                raise AdmissionHold('generated_client_stage_plan_invalid')
            binding['stage_plan'] = stage_plan
        version = artifact_version(candidate_row_id, candidate['job_id'])
        raw = prep.canonical(dict(**binding, gym_id=candidate['gym_id'],
                                 artifact_version_id=version, hosted_url=candidate['original_url'],
                                 delivered_sha256=candidate['original_sha256'])).encode()
        if len(raw) > 65536:
            raise AdmissionHold('generated_client_manifest_too_large')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT binding,version_id,manifest_bytes FROM generated_client_admissions '
                                   'WHERE row_id=?', (row_id,)).fetchone()
            if existing:
                if existing != (prep.canonical(binding), version, raw):
                    raise AdmissionHold('generated_client_binding_changed')
            else:
                con.execute('INSERT INTO generated_client_admissions VALUES (?,?,?,?,NULL,?)',
                            (row_id, prep.canonical(binding), version, raw, 'awaiting_receipt'))
        return self.load(row_id)

    def load(self, row_id):
        with self._connect() as con:
            row = con.execute('SELECT binding,version_id,manifest_bytes,receipt_id,state '
                              'FROM generated_client_admissions WHERE row_id=?', (_uuid(row_id),)).fetchone()
        if not row:
            raise AdmissionHold('generated_client_binding_unavailable')
        return dict(binding=json.loads(row[0]), artifact_version_id=row[1],
                    manifest_bytes=bytes(row[2]), receipt_id=row[3], state=row[4])

    def attach_receipt(self, row_id, receipt_id):
        receipt_id = _uuid(receipt_id)
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT receipt_id,state FROM generated_client_admissions WHERE row_id=?',
                              (_uuid(row_id),)).fetchone()
            if not row or (row[0] is not None and row[0] != receipt_id):
                raise AdmissionHold('generated_client_receipt_changed')
            if row[0] is None:
                con.execute("UPDATE generated_client_admissions SET receipt_id=?,state='ready' WHERE row_id=?",
                            (receipt_id, row_id))
        return self.load(row_id)

    def state(self, row_id, state):
        if state not in ('ready', 'dispatching', 'committing', 'committed'):
            raise AdmissionHold('generated_client_state_invalid')
        with self._connect() as con:
            where = " AND state='ready'" if state == 'dispatching' else ''
            cur = con.execute('UPDATE generated_client_admissions SET state=? WHERE row_id=? AND receipt_id IS NOT NULL' + where,
                              (state, _uuid(row_id)))
            if cur.rowcount != 1:
                raise AdmissionHold('generated_client_receipt_unavailable')


class GeneratedClientAdmission:
    """One reader authority plus existing isolated owner, explicitly injected.

    No env/DSN discovery and no issuer API. SQL enforces the dedicated read-only
    principal's grant as well as the owner principal's independent control.
    """
    def __init__(self, *, jobs, authority):
        if type(authority) is not GeneratedHostedByteAuthority or authority._reader is not None:
            raise AdmissionHold('generated_client_reader_only_required')
        self.journal = ClientAdmissionJournal(jobs)
        def reader_factory():
            conn = None
            try:
                conn = authority._factory()
                if conn.autocommit is not False or int(conn.info.transaction_status) != 0:
                    raise AdmissionHold('generated_client_reader_only_required')
                valid = conn.execute(
                    "select pg_has_role(session_user,'generated_hosted_byte_reader_20261009','MEMBER') "
                    "and not pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER') "
                    "and not pg_has_role(session_user,'service_role','MEMBER') "
                    "and not pg_has_role(session_user,'fixer_forward_media_owner_20261006','MEMBER') "
                    "and not exists(select 1 from pg_roles where rolname=session_user and (rolsuper or rolbypassrls))"
                ).fetchone()[0]
                conn.rollback()
                if valid is not True:
                    raise AdmissionHold('generated_client_reader_only_required')
                return conn
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                raise HostedByteHold('generated_client_reader_only_required') from None
        self.authority = GeneratedHostedByteAuthority(reader_factory, tenant_id=authority.tenant_id)

    def stage(self, persistence, row_id, candidate, visuals, manifest, source_revision):
        if not enabled():
            raise AdmissionHold('generated_client_admission_disabled')
        if candidate.get('gym_id') != self.authority.tenant_id:
            raise AdmissionHold('generated_client_tenant_mismatch')
        prepared_id = candidate_row(row_id, candidate['job_id'])
        version_id = artifact_version(prepared_id, candidate['job_id'])
        try:
            prior = self.journal.load(row_id)
        except AdmissionHold as e:
            if str(e) != 'generated_client_binding_unavailable':
                raise
            prior = None
        if prior is None:
            if persistence is None:
                raise AdmissionHold('generated_client_owner_plan_required')
            with persistence._conn.cursor() as cur:
                cur.execute('select public.generated_client_plan_20261009(%s,%s,%s,%s::jsonb,%s::jsonb)',
                            (row_id, prepared_id, version_id, prep.canonical(candidate), prep.canonical(manifest)))
                plan = cur.fetchone()[0]
            # No owner transaction may remain open across the independent
            # committed-authority lookup or external issuance boundary.
            persistence._conn.rollback()
        else:
            plan = prior['binding'].get('stage_plan')
            if not isinstance(plan, dict):
                raise AdmissionHold('generated_client_stage_plan_required')
        frozen = self.journal.freeze(row_id, candidate, manifest, source_revision, stage_plan=plan)
        if frozen['binding']['calendar_row_id'] != prepared_id or frozen['artifact_version_id'] != version_id:
            raise AdmissionHold('generated_client_binding_changed')
        if not frozen['receipt_id']:
            raise AdmissionHold('generated_client_receipt_pending')
        # Always use a NEW dedicated lookup, including replay. Caller receipt
        # dictionaries and a standalone version row grant no authority.
        try:
            self.authority.lookup(artifact_version_id=frozen['artifact_version_id'],
                                  hosted_url=candidate['original_url'], expected_sha256=candidate['original_sha256'],
                                  manifest_bytes=frozen['manifest_bytes'], receipt_id=frozen['receipt_id'])
        except HostedByteHold:
            raise AdmissionHold('generated_client_committed_receipt_unavailable') from None
        if frozen['state'] in ('dispatching', 'committing', 'committed'):
            with persistence._conn.cursor() as cur:
                cur.execute('select public.generated_client_reconcile_20261009(%s,%s,%s,%s)',
                            (prepared_id, frozen['artifact_version_id'], frozen['receipt_id'],
                             hashlib.sha256(frozen['manifest_bytes']).hexdigest()))
                result = cur.fetchone()[0]
            if not self._result_matches(result, prepared_id, frozen):
                raise AdmissionHold('generated_client_commit_uncertain')
            return result
        self.journal.state(row_id, 'dispatching')
        try:
            with persistence._conn.cursor() as cur:
                cur.execute('select public.generated_client_prepare_staged_20261009('
                            '%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s)',
                            (row_id, prepared_id, prep.canonical(candidate), prep.canonical(visuals), prep.canonical(manifest),
                             source_revision, frozen['artifact_version_id'], frozen['receipt_id'], frozen['manifest_bytes']))
                result = cur.fetchone()[0]
            if not self._result_matches(result, prepared_id, frozen):
                raise AdmissionHold('generated_client_staging_unavailable')
            return result
        except Exception:
            try:
                persistence._conn.rollback()
            except Exception:
                raise AdmissionHold('generated_client_commit_uncertain') from None
            self.journal.state(row_id, 'ready')
            raise AdmissionHold('generated_client_staging_unavailable') from None

    @staticmethod
    def _result_matches(result, row_id, frozen):
        return (isinstance(result, dict) and result.get('admitted') is True
                and result.get('reserved') is True and result.get('prepared') is True
                and result.get('stage_plan') == frozen['binding'].get('stage_plan')
                and result.get('calendar_row_id') == row_id
                and result.get('artifact_version_id') == frozen['artifact_version_id']
                and result.get('receipt_id') == frozen['receipt_id']
                and result.get('manifest_sha256') == hashlib.sha256(frozen['manifest_bytes']).hexdigest())

    def before_commit(self, row_id):
        self.journal.state(row_id, 'committing')

    def committed(self, row_id):
        self.journal.state(row_id, 'committed')
