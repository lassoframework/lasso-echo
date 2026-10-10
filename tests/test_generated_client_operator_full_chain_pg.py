"""Real CLI fail-closed evidence for a missing mounted owner journal.

This is deliberately NOT positive preparation/staging or restored-schema proof.
The narrow PG17 fixture supplies real restricted LOGINs and SQL authority probes.
No filesystem predicate, journal path, production constructor, RPC, provider or
storage implementation is patched. Positive full-chain proof requires an actual
isolated runtime with its durable /data mount.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch

import pytest

from agent import generated_client_operator_entrypoint as entry
from agent import generated_infographic_runtime as runtime
from tests.test_generated_client_operator_entrypoint import local_pg

ROOT = Path(__file__).resolve().parents[1]


def missing_mount():
    if os.path.isdir('/data') and os.path.ismount('/data'):
        pytest.skip('This negative host-prerequisite case requires absent/unmounted /data')


def cli(command, env):
    # Explicit finite environment prevents ambient production credentials or
    # unrelated capability names from entering the subprocess.
    result = subprocess.run(
        [sys.executable, '-m', 'agent.generated_client_operator_entrypoint',
         command, '--row', str(uuid.uuid4())],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stderr == ''
    return json.loads(result.stdout)


def base_env():
    return dict(
        PYTHONPATH=os.pathsep.join(str(p) for p in sys.path if p),
        AGENT_GENERATED_INFOGRAPHIC_RUNTIME='true',
        AGENT_GENERATED_CLIENT_ADMISSION='true',
        ECHO_GENERATED_CLIENT_TENANT='gym-a')


def test_real_prepare_cli_pg17_holds_with_valid_principals_without_data_mount(local_pg):
    missing_mount()
    admin, connect, _ = local_pg
    admin.execute((ROOT / 'migrations/DRAFT_generated_issuer_dispatch_20261009.sql').read_text())
    admin.execute("create role operator_chain_owner login;"
                  "create role fixer_forward_media_owner_20261006 nologin;"
                  "grant fixer_forward_media_owner_20261006 to operator_chain_owner;"
                  "create role operator_chain_producer login;"
                  "grant generated_issuer_dispatch_producer_20261009 to operator_chain_producer;"
                  "insert into generated_issuer_dispatch_principals_20261009"
                  " values('operator_chain_producer','gym-a',true,false)")
    env = base_env()
    env.update(AGENT_FORWARD_MEDIA_GUARD='true',
               AGENT_FORWARD_MEDIA_OWNER_TENANTS='gym-a',
               ECHO_GENERATED_CLIENT_ACCOUNT='gym-a_ig',
               FORWARD_MEDIA_OWNER_ROLE='operator_chain_owner',
               ECHO_GENERATED_CLIENT_READER_LOGIN='reader_a',
               ECHO_GENERATED_CLIENT_PRODUCER_LOGIN='operator_chain_producer')
    def dsn(login):
        # Disposable cluster identity from the actual fixture connection only.
        return f'host={admin.info.host} port={admin.info.port} dbname=postgres user={login}'
    env.update(FORWARD_MEDIA_OWNER_DSN=dsn('operator_chain_owner'),
               ECHO_GENERATED_CLIENT_READER_DSN=dsn('reader_a'),
               ECHO_GENERATED_CLIENT_PRODUCER_DSN=dsn('operator_chain_producer'))
    # Establish the same production identity/tenant probes independently before
    # the actual CLI. This does not claim that the CLI reached the journal:
    # native macOS Python may add __CF_USER_TEXT_ENCODING during imports, which
    # the exact environment contract also rejects before journal construction.
    for login, role, tenant, auto in (
        ('reader_a', entry.READER_ROLE, 'gym-a', False),
        ('operator_chain_producer', entry.PRODUCER_ROLE, 'gym-a', True),
        ('operator_chain_owner', entry.OWNER_ROLE, None, False)):
        conn = entry._factory(entry._connect, dsn(login), login, role,
                              tenant=tenant, autocommit=auto)()
        conn.close()
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(runtime.RuntimeHold, match='^generated_durable_journal_unavailable$'):
            runtime.journal_path()
    assert cli('prepare', env) == dict(
        ok=False, held=True, reason='generated_operator_prepare_failed')
    for table in ('generated_issuer_dispatch_requests_20261009',
                  'generated_hosted_byte_receipts_20261009',
                  'calendar_generated_artifact_versions'):
        assert admin.execute(f'select count(*) from public.{table}').fetchone()[0] == 0


def test_real_stage_cli_holds_before_service_construction_without_data_mount():
    missing_mount()
    env = base_env()
    env.update(AGENT_GENERATED_CLIENT_SERVICE_STAGE_BRIDGE='true',
               AGENT_GBP_STAGED_JOURNAL='true',
               AGENT_DB_PATH='/data/generated-infographic-jobs.sqlite')
    # macOS can inject this capability name during Python imports. The real
    # CLI must preserve its strict environment refusal as well.
    probe = subprocess.run([sys.executable, '-c',
        'import os,json;from agent import generated_client_operator_entrypoint as e;'
        'print(json.dumps(sorted(set(os.environ)-e.STAGE_NAMES)))'],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=30)
    unknown = json.loads(probe.stdout)
    assert set(unknown) <= {'__CF_USER_TEXT_ENCODING'}
    reason = ('generated_operator_stage_environment_rejected' if unknown
              else 'generated_operator_stage_failed')
    assert cli('stage', env) == dict(ok=False, held=True, reason=reason)
