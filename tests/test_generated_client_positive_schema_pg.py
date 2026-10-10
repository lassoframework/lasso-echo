"""Construction-only prerequisite for a future isolated Linux positive rehearsal."""
import pytest
from tests.fixtures.generated_client_positive_schema.schema_fixture import (
    PRINCIPALS, TENANT, disposable_schema,
)


def test_positive_schema_pg17_restricted_principals_and_default_off():
    pytest.importorskip('psycopg')
    from agent import generated_client_operator_entrypoint as operator
    with disposable_schema() as (admin, connect):
        assert admin.info.server_version // 10000 == 17
        assert len({login for login, _ in PRINCIPALS.values()}) == 5
        for kind, (login, role) in PRINCIPALS.items():
            with connect(login) as connection:
                identity = connection.execute(operator.IDENTITY_SQL, (role, role)).fetchone()
                assert identity[:4] == (login,login,True,True)
                assert identity[4] is (kind != 'issuer')
                attrs = connection.execute('select rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls from pg_roles where rolname=current_user').fetchone()
                assert attrs == (False,)*5
                expected = {role}
                if kind == 'issuer': expected.add('generated_hosted_byte_issuer_20261009')
                for authority in {value[1] for value in PRINCIPALS.values()} | {'generated_hosted_byte_issuer_20261009'}:
                    assert connection.execute('select pg_has_role(current_user,%s,\'MEMBER\')',(authority,)).fetchone()[0] == (authority in expected)
                if kind in ('reader','issuer'):
                    purpose = 'lookup' if kind == 'reader' else 'issue'
                    assert connection.execute('select generated_hosted_byte_authorized_20261009(%s,%s)',(TENANT,purpose)).fetchone() == (True,)
                    assert connection.execute('select generated_hosted_byte_authorized_20261009(%s,%s)',('foreign-gym',purpose)).fetchone() == (False,)
                if kind in ('producer','issuer'):
                    purpose = 'submit' if kind == 'producer' else 'dispatch'
                    assert connection.execute('select generated_issuer_dispatch_authorized_20261009(%s,%s)',(TENANT,purpose)).fetchone() == (True,)
                    assert connection.execute('select generated_issuer_dispatch_authorized_20261009(%s,%s)',('foreign-gym',purpose)).fetchone() == (False,)
                for table in ('generated_client_control_20261009','generated_hosted_byte_principals_20261009','generated_issuer_dispatch_principals_20261009'):
                    with pytest.raises(Exception) as denied:
                        connection.execute(f'select * from public.{table}')
                    assert denied.value.sqlstate == '42501'
        assert admin.execute('select enabled from generated_client_control_20261009').fetchall() == [(False,)]
        for table in ('fixer_inventory_protocol_control_20261008','fixer_remote_drive_use_control_20261008','fixer_calendar_admission_gate_20261009'):
            assert admin.execute(f'select enabled from {table}').fetchall() == [(False,)]
        for table in ('content_calendar','calendar_generated_artifact_versions','generated_hosted_byte_receipts_20261009','generated_client_admission_20261009','forward_schedule_stage_batch_20261008','generated_issuer_dispatch_requests_20261009'):
            assert admin.execute(f'select count(*) from {table}').fetchone() == (0,)
