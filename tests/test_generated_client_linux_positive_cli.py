"""Actual positive subprocess CLI sequence, only in isolated mounted Linux CI.

Synthetic external transports live below production constructors and predicates.
The disposable DB and object journal are real. This never approves or publishes.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from PIL import Image
from tests.fixtures.generated_client_positive_schema.schema_fixture import disposable_schema
from tests.fixtures.generated_client_linux_positive.seed import seed

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT/'tests/fixtures/generated_client_linux_positive'
DATA = Path('/data/positive-transport')


def command(module, env, args=(), code=0):
    result = subprocess.run([sys.executable,'-m',module,*args],cwd=ROOT,
                            env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == code, (result.returncode,result.stdout,result.stderr)
    assert result.stderr == ''
    return json.loads(result.stdout)


def test_actual_cli_mounted_prepare_issuer_resume_stage():
    if sys.platform != 'linux' or not os.path.ismount('/data'):
        pytest.skip('requires isolated Linux with real /data bind mount; never patches mount predicates')
    # Unmodified socket transport proves no outbound connectivity before installing
    # synthetic transports in child processes. Workflow additionally asserts Docker
    # HostConfig.NetworkMode == none before executing this test.
    import socket
    with pytest.raises(OSError):
        socket.create_connection(('1.1.1.1',443),timeout=1)
    assert not DATA.exists(), 'requires empty task-owned data mount'
    DATA.mkdir();(DATA/'objects').mkdir()
    image=Image.new('RGB',(1024,1280),'#112233')
    image.paste('#aabbcc',(512,0,1024,1280));image.save(DATA/'original.png')
    with disposable_schema() as (admin, connect):
        row=seed(admin,connect)
        def dsn(login):
            return f'host={admin.info.host} port={admin.info.port} dbname=postgres user={login}'
        (DATA/'config.json').write_text(json.dumps({'service_dsn':dsn('positive_service')}))
        common=dict(PYTHONPATH=str(TRANSPORT)+os.pathsep+str(ROOT),
                    PYTHONDONTWRITEBYTECODE='1',
                    AGENT_GENERATED_INFOGRAPHIC_RUNTIME='true',
                    AGENT_GENERATED_CLIENT_ADMISSION='true',
                    ECHO_GENERATED_CLIENT_TENANT='fixture-gym')
        owner=dict(common,AGENT_FORWARD_MEDIA_GUARD='true',
                   AGENT_FORWARD_MEDIA_OWNER_TENANTS='fixture-gym',
                   ECHO_GENERATED_CLIENT_ACCOUNT='fixture-gym_ig',
                   FORWARD_MEDIA_OWNER_ROLE='positive_owner',FORWARD_MEDIA_OWNER_DSN=dsn('positive_owner'),
                   ECHO_GENERATED_CLIENT_READER_LOGIN='positive_reader',
                   ECHO_GENERATED_CLIENT_READER_DSN=dsn('positive_reader'),
                   ECHO_GENERATED_CLIENT_PRODUCER_LOGIN='positive_producer',
                   ECHO_GENERATED_CLIENT_PRODUCER_DSN=dsn('positive_producer'),
                   OPENAI_API_KEY='SYNTHETIC',AGENT_HOSTING_ENABLED='true',
                   AGENT_S3_ENDPOINT='https://s3.synthetic.example',AGENT_S3_BUCKET='synthetic-bucket',
                   AGENT_S3_REGION='us-east-1',AGENT_S3_PUBLIC_BASE_URL='https://owned.example',
                   AGENT_S3_ACCESS_KEY_ID='SYNTHETIC',AGENT_S3_SECRET_ACCESS_KEY='SYNTHETIC')
        operator='agent.generated_client_operator_entrypoint'
        first=command(operator,owner,['prepare','--row',row],code=2)
        assert first == dict(ok=False,held=True,reason='generated_client_receipt_pending')
        assert admin.execute('select count(*) from generated_issuer_dispatch_requests_20261009').fetchone()[0] == 1
        issuer=dict(PYTHONPATH=common['PYTHONPATH'],PYTHONDONTWRITEBYTECODE='1',
                    ECHO_GENERATED_ISSUER_DSN=dsn('positive_issuer'),
                    ECHO_GENERATED_ISSUER_LOGIN='positive_issuer',ECHO_GENERATED_ISSUER_TENANT='fixture-gym',
                    ECHO_GENERATED_ISSUER_URL_PREFIX='https://owned.example/echo-generated-originals/fixture-gym/',
                    AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME='true',AGENT_GENERATED_ISSUER_DISPATCH='true',
                    AGENT_GENERATED_HOSTED_BYTE_ISSUER='true')
        issued=command('agent.generated_issuer_dispatch_entrypoint',issuer)
        assert not issued.get('failed') and not issued.get('held'),issued
        resumed=command(operator,owner,['prepare','--row',row])
        assert resumed['ok'] and resumed['prepared'] and resumed['admitted']
        assert command(operator,owner,['prepare','--row',row]) == resumed
        service=dict(common,AGENT_GENERATED_CLIENT_SERVICE_STAGE_BRIDGE='true',
                     AGENT_GBP_STAGED_JOURNAL='true',AGENT_FORWARD_SCHEDULE_RESERVATION='true',
                     AGENT_DB_PATH='/data/generated-infographic-jobs.sqlite',
                     SUPABASE_URL='https://supabase.synthetic.example',SUPABASE_SERVICE_ROLE_KEY='SYNTHETIC')
        staged=command(operator,service,['stage','--row',row])
        assert staged['ok'] and staged['staged'] and staged['active'] is False
        assert command(operator,service,['stage','--row',row]) == staged
        assert command(operator,owner,['prepare','--row',row]) == resumed
        with sqlite3.connect('/data/generated-infographic-jobs.sqlite') as journal:
            assert journal.execute('select count(*) from generated_jobs').fetchone()[0] == 1
            assert journal.execute('select state from generated_jobs').fetchone()[0] == 'prepared'
            assert journal.execute('select state from generated_client_admissions').fetchone()[0] == 'committed'
        assert admin.execute('select count(*) from generated_hosted_byte_receipts_20261009').fetchone()[0] == 1
        assert admin.execute('select count(*) from generated_issuer_dispatch_requests_20261009').fetchone()[0] == 1
        assert admin.execute('select count(*) from forward_schedule_stage_batch_20261008').fetchone()[0] == 1
        assert admin.execute('select state,finalize_receipt from forward_schedule_stage_batch_20261008 where batch_id=%s',
                             (staged['batch_id'],)).fetchone() == ('staged',None)
        assert admin.execute('select calendar_row_id::text from forward_schedule_stage_member_20261008 where batch_id=%s',
                             (staged['batch_id'],)).fetchall() == [(resumed['calendar_row_id'],)]
        assert admin.execute('select status,variant_status,published_at,late_post_id from content_calendar where id=%s',
                             (resumed['calendar_row_id'],)).fetchone() == ('pending','candidate',None,None)
        assert admin.execute('select count(*) from generated_client_finalizer_decision_20261009').fetchone()[0] == 0
        calls=[json.loads(line)['kind'] for line in (DATA/'calls.jsonl').read_text().splitlines()]
        assert calls.count('generate') == calls.count('review') == calls.count('upload') == 1
        assert calls.count('issuer_get') == 1
        objects=list((DATA/'objects').iterdir());assert len(objects) == 1
        assert objects[0].read_bytes() == (DATA/'original.png').read_bytes()
        receipt=dict(positive_cli=True,synthetic_transports=True,production=False,
                     row=row,prepared=resumed,staged=staged,calls=calls)
        Path('/data/positive-cli-receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2))
