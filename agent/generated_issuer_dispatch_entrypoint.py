"""Finite operator command: python -m agent.generated_issuer_dispatch_entrypoint.

No CLI configuration is accepted. Provision ECHO_GENERATED_ISSUER_DSN,
ECHO_GENERATED_ISSUER_LOGIN, ECHO_GENERATED_ISSUER_TENANT and
ECHO_GENERATED_ISSUER_URL_PREFIX in the dedicated issuer process environment.
The LOGIN needs both issuer group memberships and admin-provisioned tenant
grants in both migrations. No migrations or grants are applied here.
Enable runtime, dispatch-job and hosted-byte-worker flags explicitly to run.
"""
from __future__ import annotations

import json
import os
import sys

from .generated_hosted_byte_authority import GeneratedHostedByteAuthority, HostedObjectReader
from .generated_hosted_byte_issuer_worker import enabled as worker_enabled
from .generated_issuer_dispatch_job import enabled as job_enabled, off_result
from .generated_issuer_dispatch_runtime import IssuerDispatchRuntime, TrustedTenantNamespace, enabled
from .generated_issuer_dispatch_store import GeneratedIssuerDispatchStore


class EntrypointHold(RuntimeError):
    """Contains only a fixed diagnostic code."""


# Positive role allowlist also rejects indirect membership in broad built-in
# or custom roles, including service_role and producer roles. USAGE requires
# privileges effective without SET ROLE (MEMBER alone permits NOINHERIT).
_IDENTITY_SQL = """select session_user, current_user,
 rolcanlogin and not (rolsuper or rolbypassrls or rolcreaterole or rolcreatedb or rolreplication),
 pg_has_role(session_user,'generated_issuer_dispatch_issuer_20261009','USAGE'),
 pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','USAGE'),
 not pg_has_role(session_user,'generated_issuer_dispatch_producer_20261009','MEMBER'),
 not exists (select 1 from pg_roles other
   where other.rolname <> session_user
     and other.rolname not in ('generated_issuer_dispatch_issuer_20261009',
                              'generated_hosted_byte_issuer_20261009')
     and pg_has_role(session_user,other.oid,'MEMBER'))
 from pg_roles where rolname=session_user"""


def _connect(dsn, *, autocommit):
    import psycopg
    return psycopg.connect(dsn, autocommit=autocommit, connect_timeout=10)


def run_once(*, environ=None, connect=None):
    """One tenant, one bounded run. Injection is an operator/test boundary."""
    # Check gates before reading credentials, importing drivers or connecting.
    if not (enabled() and job_enabled() and worker_enabled()):
        return off_result().as_dict()
    env = os.environ if environ is None else environ
    connector = _connect if connect is None else connect
    try:
        dsn, login, tenant, prefix = (
            env['ECHO_GENERATED_ISSUER_' + key]
            for key in ('DSN', 'LOGIN', 'TENANT', 'URL_PREFIX'))
        if any(type(v) is not str or not v or v != v.strip()
               for v in (dsn, login, tenant, prefix)):
            raise ValueError()
        if login.lower() in ('service_role', 'postgres', 'supabase_admin'):
            raise ValueError()
        reader = HostedObjectReader({tenant: [prefix]})
        namespace = TrustedTenantNamespace([tenant])
    except Exception:
        raise EntrypointHold('generated_entrypoint_configuration_invalid') from None

    def factory(autocommit):
        conn = None
        try:
            conn = connector(dsn, autocommit=autocommit)
            row = conn.execute(_IDENTITY_SQL).fetchone()
            if row != (login, login, True, True, True, True, True):
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
            raise EntrypointHold('generated_entrypoint_identity_rejected') from None

    try:
        store = GeneratedIssuerDispatchStore(lambda: factory(True))
        authority = GeneratedHostedByteAuthority(
            lambda: factory(False), tenant_id=tenant, reader=reader)
        return IssuerDispatchRuntime(
            store=store, authority=authority, namespace=namespace,
            batch_limit=10, max_ticks=1).run_once()
    except Exception:
        raise EntrypointHold('generated_entrypoint_run_failed') from None


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv):
        print('{"hold":"generated_entrypoint_arguments_rejected"}')
        return 2
    try:
        result = run_once()
        print(json.dumps(result, sort_keys=True))
        return 2 if result.get('failed') or result.get('held') else 0
    except Exception:
        print('{"hold":"generated_entrypoint_run_failed"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
