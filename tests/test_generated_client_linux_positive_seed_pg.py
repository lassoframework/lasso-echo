"""Runtime-valid synthetic source/census prerequisite, without CLI claims."""
import os
from types import SimpleNamespace
from unittest.mock import patch
from tests.fixtures.generated_client_positive_schema.schema_fixture import disposable_schema
from tests.fixtures.generated_client_linux_positive.seed import seed
from agent.generated_infographic_runtime import OwnerSnapshotLoader
from agent.forward_media_owner import ForwardMediaOwnerPersistence, HostedObjectReader


def test_synthetic_source_census_pass_actual_runtime_loader():
    with disposable_schema() as (admin, connect):
        row = seed(admin, connect)
        conn = connect('positive_owner')
        conn.autocommit = False
        env = dict(FORWARD_MEDIA_OWNER_DSN='SYNTHETIC nonsecret',
                   FORWARD_MEDIA_OWNER_ROLE='positive_owner',
                   AGENT_S3_PUBLIC_BASE_URL='https://owned.example')
        try:
            with patch.dict(os.environ, env, clear=True):
                persistence = ForwardMediaOwnerPersistence(conn, 'positive_owner', HostedObjectReader())
                snapshot, history = OwnerSnapshotLoader(persistence).load(row, 'fixture-gym',
                    SimpleNamespace(key='fixture-gym_ig', platform='instagram'))
            assert snapshot['local_census_current'] is True
            assert snapshot['copy_verified'] is True
            assert snapshot['palette_verified'] is True
            for table in ('generated_issuer_dispatch_requests_20261009',
                          'generated_hosted_byte_receipts_20261009',
                          'calendar_generated_artifact_versions'):
                assert admin.execute(f'select count(*) from public.{table}').fetchone()[0] == 0
        finally:
            conn.close()


def test_service_alias_transport_reads_real_restricted_pg(tmp_path):
    from tests.test_generated_client_linux_positive_transport import transport
    from agent.portal_calendar_store import SupabaseCalendarStore
    import json
    import requests
    fixture,_,_=transport(tmp_path)
    with disposable_schema() as (admin,connect):
        fixture.CONFIG.write_text(json.dumps({'service_dsn':
            f'host={admin.info.host} port={admin.info.port} dbname=postgres user=positive_service'}))
        env={'SUPABASE_SERVICE_ROLE_KEY':'SYNTHETIC'}
        with patch.dict(os.environ,env,clear=True),patch.object(requests.sessions.Session,'send',fixture.send):
            store=SupabaseCalendarStore(url='https://supabase.synthetic.example',service_key='SYNTHETIC')
            assert store._forward_batch_tenant('fixture-gym') == 'fixture-gym'
        assert json.loads((tmp_path/'calls.jsonl').read_text()) == {'kind':'tenant_alias_read'}
