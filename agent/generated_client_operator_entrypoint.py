"""Finite commands for the existing generated client pipeline.

``python -m agent.generated_client_operator_entrypoint prepare --row UUID``
freezes one owner plan and enqueues its hosted-byte receipt. Run the EXISTING
issuer command separately, then repeat prepare with the same row to reconcile
and prepare without generating again. ``stage --row UUID`` runs in a separate
service environment and only stages the exact persisted preparation inactive.
No command issues receipts, approves, finalizes, publishes or changes flags.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass

from . import generated_infographic_runtime as runtime
from . import generated_client_admission as admission
from . import generated_client_service_stage_bridge as stage_bridge
from .generated_hosted_byte_authority import GeneratedHostedByteAuthority, _uuid
from .generated_issuer_dispatch_client import IssuerDispatchClient
from .generated_issuer_dispatch_store import GeneratedIssuerDispatchStore
from .generated_infographic_preparation import SQLiteGenerationJobs
from .forward_media_lane import RUNTIME_NAMES

PREFIX = 'ECHO_GENERATED_CLIENT_'
READER_ROLE = 'generated_hosted_byte_reader_20261009'
PRODUCER_ROLE = 'generated_issuer_dispatch_producer_20261009'
OWNER_ROLE = 'fixer_forward_media_owner_20261006'
# Positive membership allowlist rejects even NOINHERIT/SET ROLE access to any
# unlisted role, including issuer, service_role and built-in broad data roles.
IDENTITY_SQL = """select session_user,current_user,
 rolcanlogin and not (rolsuper or rolbypassrls or rolcreaterole or rolcreatedb or rolreplication),
 pg_has_role(session_user,%s,'USAGE'),
 not exists(select 1 from pg_roles r where r.rolname<>session_user
   and r.rolname<>%s and pg_has_role(session_user,r.oid,'MEMBER'))
 from pg_roles where rolname=session_user"""
STAGE_NAMES = RUNTIME_NAMES | {
    PREFIX+'TENANT', 'AGENT_GENERATED_INFOGRAPHIC_RUNTIME',
    'AGENT_GENERATED_INFOGRAPHIC_JOURNAL', 'AGENT_GENERATED_CLIENT_ADMISSION',
    'AGENT_GENERATED_CLIENT_SERVICE_STAGE_BRIDGE', 'AGENT_GBP_STAGED_JOURNAL',
    'AGENT_FORWARD_SCHEDULE_RESERVATION', 'AGENT_DB_PATH',
    'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY',
}


@dataclass(frozen=True)
class _ScopedAccount:
    # Request identity only. The existing owner SQL source-brand readback
    # authenticates the actual gym mapping. No registry enumeration or
    # publisher/token configuration is needed in this isolated process.
    key: str
    platform: str


class OperatorHold(RuntimeError):
    """Fixed diagnostic codes only; never emit credentials or source content."""


def _value(env, key):
    value = env.get(key)
    if type(value) is not str or not value or value != value.strip():
        raise OperatorHold('generated_operator_configuration_invalid')
    return value


def _tenant(env):
    tenant = _value(env, PREFIX+'TENANT')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', tenant):
        raise OperatorHold('generated_operator_configuration_invalid')
    return tenant


def _connect(dsn, *, autocommit):
    import psycopg
    return psycopg.connect(dsn, autocommit=autocommit, connect_timeout=10,
                          options='-c lock_timeout=5000 -c statement_timeout=20000')


def _factory(connector, dsn, login, role, *, autocommit, tenant=None):
    def open_connection():
        conn = None
        try:
            conn = connector(dsn, autocommit=autocommit)
            if conn.autocommit is not autocommit or int(conn.info.transaction_status) != 0:
                raise ValueError()
            if conn.execute(IDENTITY_SQL, (role, role)).fetchone() != (login, login, True, True, True):
                raise ValueError()
            if tenant is not None:
                if role == READER_ROLE:
                    sql = 'select public.generated_hosted_byte_authorized_20261009(%s,%s)'
                    purpose = 'lookup'
                else:
                    sql = 'select public.generated_issuer_dispatch_authorized_20261009(%s,%s)'
                    purpose = 'submit'
                if conn.execute(sql, (tenant, purpose)).fetchone() != (True,):
                    raise ValueError()
            if not autocommit:
                conn.rollback()
            return conn
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise OperatorHold('generated_operator_identity_rejected') from None
    return open_connection


def _safe(report):
    # Runtime results contain the owner's source/caption plan. The CLI emits
    # only bounded outcome facts, never plans, manifests, URLs or raw errors.
    if report.get('ok') is True:
        return {key: report[key] for key in ('ok', 'reserved', 'admitted', 'prepared',
                'calendar_row_id', 'placeholder_row_id') if key in report}
    reason = report.get('reason')
    if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,120}', reason):
        reason = 'generated_operator_run_failed'
    return dict(ok=False, held=True, reason=reason)


def prepare_once(row_id, *, environ=None, connect=None):
    """Compose owner + reader + queue producer. No issuer is constructed."""
    from .forward_media_guard import enabled as guard_enabled
    if not (runtime.enabled() and admission.enabled() and guard_enabled()):
        return dict(ok=False, held=True, reason='generated_operator_prepare_disabled')
    env = os.environ if environ is None else environ
    conn = None
    try:
        from .forward_media_owner import ForwardMediaOwnerPersistence, check_environment
        check_environment(env, lane='generated_owner')
        row_id, tenant = _uuid(row_id), _tenant(env)
        account_key = _value(env, PREFIX+'ACCOUNT')
        if account_key not in (tenant+'_ig', tenant+'_fb'):
            raise OperatorHold('generated_operator_account_mismatch')
        if _value(env, 'AGENT_FORWARD_MEDIA_OWNER_TENANTS') != tenant:
            raise OperatorHold('generated_operator_tenant_scope_invalid')
        owner_login = _value(env, 'FORWARD_MEDIA_OWNER_ROLE')
        reader_login = _value(env, PREFIX+'READER_LOGIN')
        producer_login = _value(env, PREFIX+'PRODUCER_LOGIN')
        if len({owner_login, reader_login, producer_login}) != 3:
            raise OperatorHold('generated_operator_principals_not_separate')
        connector = _connect if connect is None else connect
        owner_factory = _factory(connector, _value(env, 'FORWARD_MEDIA_OWNER_DSN'),
                                 owner_login, OWNER_ROLE, autocommit=False)
        reader_factory = _factory(connector, _value(env, PREFIX+'READER_DSN'),
                                  reader_login, READER_ROLE, autocommit=False, tenant=tenant)
        producer_factory = _factory(connector, _value(env, PREFIX+'PRODUCER_DSN'),
                                    producer_login, PRODUCER_ROLE, autocommit=True, tenant=tenant)
        # All independent capabilities must be present before any paid work.
        # These idle probes close before source read, generation or upload.
        for factory in (reader_factory, producer_factory):
            probe = factory()
            probe.close()
        conn = owner_factory()
        from .forward_media_owner import HostedObjectReader
        persistence = ForwardMediaOwnerPersistence(conn, owner_login, HostedObjectReader(),
                                                  environment_lane='generated_owner')
        jobs = SQLiteGenerationJobs(runtime.journal_path())
        existing = runtime._runtime_record(jobs, row_id)
        storage = None
        if not existing or existing['candidate'] is None:
            # Provision every paid-work dependency before paying for generation.
            # A durable prepared candidate needs only exact public readback on
            # resume and does not require a new provider or storage credential.
            from . import media_host, config
            _value(env, 'OPENAI_API_KEY')
            for name in ('AGENT_S3_ENDPOINT', 'AGENT_S3_BUCKET',
                         'AGENT_S3_PUBLIC_BASE_URL', 'AGENT_S3_ACCESS_KEY_ID',
                         'AGENT_S3_SECRET_ACCESS_KEY'):
                _value(env, name)
            if not config.hosting_enabled():
                raise OperatorHold('generated_operator_storage_unavailable')
            storage = media_host._default_client()
            if (storage is None or not callable(getattr(storage, 'put_bytes_if_absent', None))
                    or not callable(getattr(storage, 'get_bytes', None))):
                raise OperatorHold('generated_operator_storage_unavailable')
        client = admission.GeneratedClientAdmission(jobs=jobs,
            authority=GeneratedHostedByteAuthority(reader_factory, tenant_id=tenant))
        queue = IssuerDispatchClient(store=GeneratedIssuerDispatchStore(producer_factory))
        account = _ScopedAccount(account_key, 'instagram' if account_key.endswith('_ig') else 'facebook_page')
        return _safe(runtime.run_calendar_row(tenant, account, row_id,
            persistence=persistence, jobs=jobs, storage=storage,
            client_admission=client, issuer_dispatch=queue))
    except OperatorHold:
        raise
    except Exception:
        raise OperatorHold('generated_operator_prepare_failed') from None
    finally:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass


def stage_once(row_id, *, environ=None, store=None):
    """Separate service process; exact preparation to inactive staging only."""
    if not (runtime.enabled() and admission.enabled() and stage_bridge.enabled()):
        return dict(ok=False, held=True, reason='generated_operator_stage_disabled')
    env = os.environ if environ is None else environ
    try:
        if any(name not in STAGE_NAMES for name in env):
            raise OperatorHold('generated_operator_stage_environment_rejected')
        tenant, row_id = _tenant(env), _uuid(row_id)
        path = runtime.journal_path()
        # The forward staged journal and admission journal share one mounted
        # /data file. Refuse the standard echo.db fallback or a second file.
        if os.path.realpath(_value(env, 'AGENT_DB_PATH')) != path:
            raise OperatorHold('generated_operator_stage_journal_mismatch')
        journal = admission.ClientAdmissionJournal(SQLiteGenerationJobs(path))
        frozen = journal.load(row_id)
        if (frozen['state'] != 'committed'
                or frozen['binding']['candidate']['gym_id'] != tenant):
            raise OperatorHold('generated_operator_stage_binding_unverified')
        if store is None:
            from .portal_calendar_store import SupabaseCalendarStore
            url = _value(env, 'SUPABASE_URL')
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment or parsed.path):
                raise OperatorHold('generated_operator_configuration_invalid')
            store = SupabaseCalendarStore(url=url,
                service_key=_value(env, 'SUPABASE_SERVICE_ROLE_KEY'))
        result = stage_bridge.GeneratedClientServiceStageBridge(journal=journal, store=store).stage(row_id)
        return dict(ok=True, staged=True, active=False, batch_id=result['batch_id'])
    except OperatorHold:
        raise
    except Exception:
        raise OperatorHold('generated_operator_stage_failed') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('prepare', 'stage'))
    parser.add_argument('--row', required=True)
    args = parser.parse_args(argv)
    try:
        result = (prepare_once if args.command == 'prepare' else stage_once)(args.row)
    except OperatorHold as exc:
        result = dict(ok=False, held=True, reason=str(exc))
    if result.get('ok') is not True:
        result = _safe(result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
