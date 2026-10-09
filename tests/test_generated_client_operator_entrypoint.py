"""Finite composition and real PG17 principal checks; no production calls."""
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import uuid

import pytest

from agent import generated_client_operator_entrypoint as entry
from agent import forward_media_lane as lane, forward_media_owner as owner
from agent import generated_infographic_runtime as runtime
from agent.generated_client_admission import GeneratedClientAdmission
from agent.generated_issuer_dispatch_client import IssuerDispatchClient
import test_generated_hosted_byte_authority_pg as hosted_pg
from test_calendar_oct7_overlay_cutover_pg17 import pg17_bin_dir


@pytest.fixture(scope='module')
def local_pg():
    # Reuse the existing schema/migration fixture with portable exact-PG17
    # discovery. No dependency install or production DSN is consulted.
    import shutil
    pg = pg17_bin_dir()
    if pg is None:
        pytest.skip('existing PostgreSQL 17 unavailable; set PG17_BIN')
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    prior = hosted_pg.PG
    hosted_pg.PG = pg
    try:
        yield from hosted_pg.local_pg.__wrapped__()
    finally:
        hosted_pg.PG = prior



def config():
    return dict(AGENT_GENERATED_INFOGRAPHIC_RUNTIME='true',
        AGENT_GENERATED_CLIENT_ADMISSION='true', AGENT_FORWARD_MEDIA_GUARD='true',
        FORWARD_MEDIA_OWNER_DSN='synthetic-owner', FORWARD_MEDIA_OWNER_ROLE='owner_login',
        AGENT_FORWARD_MEDIA_OWNER_TENANTS='gym-a', ECHO_GENERATED_CLIENT_TENANT='gym-a',
        ECHO_GENERATED_CLIENT_ACCOUNT='gym-a_ig',
        ECHO_GENERATED_CLIENT_READER_DSN='synthetic-reader', ECHO_GENERATED_CLIENT_READER_LOGIN='reader_login',
        ECHO_GENERATED_CLIENT_PRODUCER_DSN='synthetic-producer', ECHO_GENERATED_CLIENT_PRODUCER_LOGIN='producer_login')


@pytest.fixture
def environment(monkeypatch, tmp_path):
    env = config()
    with patch.dict(os.environ, env, clear=True):
        monkeypatch.setattr(runtime, 'journal_path', lambda: str(tmp_path/'one.sqlite'))
        yield env


class Connection:
    def __init__(self, login, autocommit, events, *, valid=True, authorized=True):
        self.login, self.autocommit, self.events = login, autocommit, events
        self.valid, self.authorized = valid, authorized
        self.info = SimpleNamespace(transaction_status=0)
    def execute(self, sql, args=None):
        self.events.append((self.login, sql, args))
        if sql == entry.IDENTITY_SQL:
            value = (self.login, self.login, self.valid, True, True)
        else:
            assert 'authorized_20261009' in sql
            value = (self.authorized,)
        return SimpleNamespace(fetchone=lambda: value)
    def rollback(self):
        self.events.append((self.login, 'rollback'))
    def close(self):
        self.events.append((self.login, 'close'))


def connector(events, **overrides):
    def connect(dsn, *, autocommit):
        login = dict(zip(['synthetic-owner','synthetic-reader','synthetic-producer'],
                        ['owner_login','reader_login','producer_login']))[dsn]
        return Connection(login, autocommit, events, **overrides)
    return connect


def test_off_does_not_read_config_create_journal_or_connect(monkeypatch):
    with patch.dict(os.environ, {}, clear=True):
        forbidden = lambda *a, **kw: pytest.fail('side effect while OFF')
        monkeypatch.setattr(runtime, 'journal_path', forbidden)
        assert entry.prepare_once('invalid', environ={}, connect=forbidden)['reason'].endswith('disabled')
        assert entry.stage_once('invalid', environ={})['reason'].endswith('disabled')


@pytest.mark.parametrize('name', ['ECHO_GENERATED_ISSUER_DSN', 'SUPABASE_SERVICE_ROLE_KEY',
    'FORWARD_MEDIA_OWNER_DRIVE_SA_JSON', 'AWS_ACCESS_KEY_ID', 'AGENT_SOCIALAPI_KEY'])
def test_generated_environment_rejects_unowned_capabilities(environment, name):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    os.environ[name] = 'not-a-real-credential'
    with pytest.raises(entry.OperatorHold, match='prepare_failed'):
        entry.prepare_once(str(uuid.uuid4()), connect=lambda *a, **kw: pytest.fail('connected'))
    assert name not in lane.GENERATED_OWNER_NAMES


def test_ordinary_owner_contract_stays_ordinary(environment):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    assert lane.unknown_environment_names(os.environ, 'owner')
    owner.check_environment(lane='generated_owner')
    with pytest.raises(owner.EnvironmentGuardError):
        owner.check_environment()
    assert 'OPENAI_API_KEY' not in lane.OWNER_NAMES
    assert 'ECHO_GENERATED_CLIENT_READER_DSN' not in lane.ATTESTER_NAMES


@pytest.mark.parametrize('key,value', [('ECHO_GENERATED_CLIENT_PRODUCER_LOGIN','reader_login'),
    ('AGENT_FORWARD_MEDIA_OWNER_TENANTS','gym-a,foreign'),
    ('ECHO_GENERATED_CLIENT_ACCOUNT','foreign_ig')])
def test_scope_or_reused_login_rejected_before_connections(environment, key, value):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    os.environ[key] = value
    with pytest.raises(entry.OperatorHold):
        entry.prepare_once(str(uuid.uuid4()), connect=lambda *a, **kw: pytest.fail('connected'))


@pytest.mark.parametrize('kwargs', [dict(valid=False), dict(authorized=False)])
def test_missing_principal_capability_stops_before_provider(environment, monkeypatch, kwargs):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    events=[]
    monkeypatch.setattr(runtime, 'run_calendar_row', lambda *a, **k: pytest.fail('paid runtime'))
    with pytest.raises(entry.OperatorHold, match='identity_rejected'):
        entry.prepare_once(str(uuid.uuid4()), connect=connector(events, **kwargs))
    assert events[-1] == ('reader_login','close')


def test_exact_production_constructors_and_resume_no_new_provider(environment, monkeypatch):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    events=[]
    monkeypatch.setattr(runtime, '_runtime_record', lambda jobs, row: dict(candidate={'already':'prepared'}))
    def run(tenant, account, row, **kw):
        assert tenant=='gym-a'
        assert account.key=='gym-a_ig' and account.platform=='instagram'
        assert not hasattr(account, 'get_token')
        assert type(kw['persistence']) is owner.ForwardMediaOwnerPersistence
        assert kw['persistence']._environment_lane=='generated_owner'
        assert type(kw['client_admission']) is GeneratedClientAdmission
        assert type(kw['issuer_dispatch']) is IssuerDispatchClient
        assert kw['client_admission'].authority._reader is None
        assert kw['client_admission'].journal.path==kw['jobs'].path
        assert kw['storage'] is None
        return dict(ok=True, admitted=True, prepared=True, reserved=True,
                    stage_plan={'caption':'private'}, receipt_ref='private-url')
    monkeypatch.setattr(runtime, 'run_calendar_row', run)
    result=entry.prepare_once(str(uuid.uuid4()), connect=connector(events))
    assert result==dict(ok=True,admitted=True,prepared=True,reserved=True)
    assert [e for e in events if e[1]==entry.IDENTITY_SQL][:3]==[
        ('reader_login',entry.IDENTITY_SQL,(entry.READER_ROLE,entry.READER_ROLE)),
        ('producer_login',entry.IDENTITY_SQL,(entry.PRODUCER_ROLE,entry.PRODUCER_ROLE)),
        ('owner_login',entry.IDENTITY_SQL,(entry.OWNER_ROLE,entry.OWNER_ROLE))]
    assert events[-1]==('owner_login','close')


def test_missing_storage_credentials_hold_before_paid_work(environment, monkeypatch):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    monkeypatch.setattr(runtime, 'run_calendar_row', lambda *a, **k: pytest.fail('paid runtime'))
    with pytest.raises(entry.OperatorHold, match='configuration_invalid'):
        entry.prepare_once(str(uuid.uuid4()), connect=connector([]))


def test_generated_owner_rechecks_environment_at_identity_boundary(environment):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self,*a): pass
        def fetchone(self): return ('owner_login',)
    persistence=owner.ForwardMediaOwnerPersistence(SimpleNamespace(cursor=Cursor),
        'owner_login',None,environment_lane='generated_owner')
    persistence._assert_owner_identity()
    os.environ['ECHO_GENERATED_ISSUER_DSN']='synthetic forbidden'
    with pytest.raises(owner.EnvironmentGuardError):
        persistence._assert_owner_identity()


def test_stage_rejects_generator_credentials_before_reading_journal(environment):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    os.environ['AGENT_GENERATED_CLIENT_SERVICE_STAGE_BRIDGE']='true'
    with pytest.raises(entry.OperatorHold, match='stage_environment_rejected'):
        entry.stage_once(str(uuid.uuid4()))


def test_stage_same_journal_exact_tenant_inactive_only(monkeypatch, tmp_path):
    from test_generated_client_service_stage_bridge import lane as bridge_lane
    # Reuse the actual transport/store/journal fixture, not a mock bridge.
    gen = bridge_lane.__wrapped__(tmp_path, monkeypatch)
    obj, placeholder, http = gen
    env=dict(AGENT_GENERATED_INFOGRAPHIC_RUNTIME='true',AGENT_GENERATED_CLIENT_ADMISSION='true',
             AGENT_GENERATED_CLIENT_SERVICE_STAGE_BRIDGE='true',AGENT_GBP_STAGED_JOURNAL='true',
             ECHO_GENERATED_CLIENT_TENANT='gym',AGENT_DB_PATH=obj.journal.path)
    monkeypatch.setattr(runtime,'journal_path',lambda:obj.journal.path)
    with patch.dict(os.environ,env,clear=True):
        result=entry.stage_once(placeholder,store=obj.store)
        assert result['ok'] and result['active'] is False and result['staged']
        assert http.status['state']=='staged' and http.status['finalize_receipt'] is None
        assert all('finalize' not in str(c[0]) for c in http.calls)
        os.environ['ECHO_GENERATED_CLIENT_TENANT']='foreign'
        count=len(http.calls)
        with pytest.raises(entry.OperatorHold,match='binding_unverified'):
            entry.stage_once(placeholder,store=obj.store)
        assert len(http.calls)==count
        os.environ['AGENT_DB_PATH']=str(tmp_path/'another.sqlite')
        with pytest.raises(entry.OperatorHold,match='journal_mismatch'):
            entry.stage_once(placeholder,store=obj.store)


def test_factory_pg17_requires_effective_role_grant_and_exact_tenant(local_pg):
    admin, connect, pg=local_pg
    admin.execute("create role operator_reader login;grant generated_hosted_byte_reader_20261009 to operator_reader;"
                  "insert into generated_hosted_byte_principals_20261009 values('operator_reader','gym-a',false,true)")
    factory=lambda **kw: entry._factory(lambda dsn,autocommit:connect('operator_reader',autocommit),
        'synthetic','operator_reader',entry.READER_ROLE,autocommit=False,**kw)
    conn=factory(tenant='gym-a')();conn.close()
    with pytest.raises(entry.OperatorHold,match='identity_rejected'):
        factory(tenant='foreign')()
    admin.execute('grant pg_read_all_data to operator_reader')
    with pytest.raises(entry.OperatorHold,match='identity_rejected'):
        factory(tenant='gym-a')()
    admin.execute('revoke pg_read_all_data from operator_reader;revoke generated_hosted_byte_reader_20261009 from operator_reader;grant generated_hosted_byte_reader_20261009 to operator_reader with inherit false')
    with pytest.raises(entry.OperatorHold,match='identity_rejected'):
        factory(tenant='gym-a')()



def test_producer_and_owner_factory_pg17_separation(local_pg):
    from pathlib import Path
    admin, connect, pg = local_pg
    migration = Path(__file__).resolve().parents[1] / 'migrations/DRAFT_generated_issuer_dispatch_20261009.sql'
    admin.execute(migration.read_text())
    admin.execute("create role operator_producer login;grant generated_issuer_dispatch_producer_20261009 to operator_producer;"
                  "insert into generated_issuer_dispatch_principals_20261009 values('operator_producer','gym-a',true,false);"
                  "create role fixer_forward_media_owner_20261006 nologin;create role operator_owner login;"
                  "grant fixer_forward_media_owner_20261006 to operator_owner")
    def factory(login, group, tenant=None):
        return entry._factory(lambda dsn,autocommit:connect(login,autocommit),
            'synthetic',login,group,autocommit=group==entry.PRODUCER_ROLE,tenant=tenant)
    factory('operator_producer',entry.PRODUCER_ROLE,'gym-a')().close()
    factory('operator_owner',entry.OWNER_ROLE)().close()
    with pytest.raises(entry.OperatorHold,match='identity_rejected'):
        factory('operator_producer',entry.PRODUCER_ROLE,'foreign')()
    admin.execute('grant generated_hosted_byte_issuer_20261009 to operator_producer')
    with pytest.raises(entry.OperatorHold,match='identity_rejected'):
        factory('operator_producer',entry.PRODUCER_ROLE,'gym-a')()
    admin.execute('grant generated_issuer_dispatch_producer_20261009 to operator_owner')
    with pytest.raises(entry.OperatorHold,match='identity_rejected'):
        factory('operator_owner',entry.OWNER_ROLE)()


def test_cli_sanitizes_raw_errors(environment, monkeypatch, capsys):
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    monkeypatch.setattr(entry,'prepare_once',lambda *a:dict(ok=False,reason='private DSN password'))
    assert entry.main(['prepare','--row',str(uuid.uuid4())])==2
    assert 'private' not in capsys.readouterr().out
