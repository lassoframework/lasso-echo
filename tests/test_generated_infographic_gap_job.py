"""Offline checks for the generated infographic gap cron job wrapper."""
import json
import os
import re

import pytest

from agent import generated_infographic_gap_job as job
from agent import generated_infographic_runtime as runtime


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv(job.FLAG, 'true')
    monkeypatch.setenv(runtime.FLAG, 'true')
    monkeypatch.setenv(job.TENANTS_ENV, 'same-gym,other-gym')
    monkeypatch.setenv(job.CRON_TENANTS_ENV, 'same-gym')


def test_off_does_nothing_and_opens_no_db(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runtime, 'main', lambda argv: calls.append(argv) or 0)
    monkeypatch.delenv(job.FLAG, raising=False)
    assert job.run() == 0
    assert calls == []
    report = json.loads(capsys.readouterr().out)
    assert report == dict(ok=False, held=True, reason='generated_gap_cron_disabled')


def test_scan_is_scoped_to_cron_allowlist(armed, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(runtime, 'main', lambda argv: seen.update(argv=argv, tenants=os.environ.get(job.TENANTS_ENV)) or 0)
    assert job.run() == 0
    assert seen == {'argv': ['--scan'], 'tenants': 'same-gym'}
    # The caller's owner allowlist is restored after the pass.
    assert os.environ[job.TENANTS_ENV] == 'same-gym,other-gym'
    assert capsys.readouterr().out == ''


def test_cron_tenant_outside_owner_allowlist_holds_without_scan(armed, monkeypatch, capsys):
    monkeypatch.setenv(job.CRON_TENANTS_ENV, 'same-gym,rogue-gym')
    calls = []
    monkeypatch.setattr(runtime, 'main', lambda argv: calls.append(argv) or 0)
    assert job.run() == 2
    assert calls == []
    report = json.loads(capsys.readouterr().out)
    assert report['held'] and report['reason'] == 'generated_gap_cron_tenant_not_allowlisted'


@pytest.mark.parametrize('cron,owner,reason', [
    ('', 'same-gym', 'generated_gap_cron_tenants_required'),
    ('bad tenant!', 'same-gym', 'generated_gap_cron_tenants_required'),
    ('same-gym', '', 'explicit_tenant_allowlist_required'),
    ('same-gym', 'other-gym', 'generated_gap_cron_tenant_not_allowlisted'),
])
def test_invalid_allowlists_hold(armed, monkeypatch, capsys, cron, owner, reason):
    monkeypatch.setenv(job.CRON_TENANTS_ENV, cron)
    monkeypatch.setenv(job.TENANTS_ENV, owner)
    calls = []
    monkeypatch.setattr(runtime, 'main', lambda argv: calls.append(argv) or 0)
    assert job.run() == 2
    assert calls == []
    report = json.loads(capsys.readouterr().out)
    assert report == dict(ok=False, held=True, reason=reason)


def test_job_defers_to_runtime_report_and_exit(armed, monkeypatch, capsys):
    # The wrapper owns no owner path: runtime.main's report and exit code pass
    # through unchanged (held run exits 2; the wrapper adds nothing).
    monkeypatch.setattr(runtime, 'main', lambda argv: print(json.dumps(
        dict(ok=False, held=True, reason='generated_owner_environment_unavailable'))) or 2)
    assert job.run() == 2
    report = json.loads(capsys.readouterr().out)
    assert report['held'] and report['reason'] == 'generated_owner_environment_unavailable'


def test_railway_config_is_venv_cron_without_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'railway.generated-gap.json')) as fh:
        config = json.load(fh)
    assert config['build']['builder'] == 'NIXPACKS'
    assert config['deploy']['startCommand'] == (
        '/opt/venv/bin/python -m agent.generated_infographic_gap_job')
    assert re.fullmatch(r'\S+ \S+ \S+ \S+ \S+', config['deploy']['cronSchedule'])
    assert 'PATH' not in json.dumps(config)


def test_module_exits_and_opens_no_resources_when_runtime_off(armed, monkeypatch, capsys):
    # Cron armed but the runtime flag OFF: the runtime's own OFF receipt is
    # printed and the process exits 0 with no owner connection attempted.
    monkeypatch.delenv(runtime.FLAG, raising=False)
    assert job.run() == 0
    report = json.loads(capsys.readouterr().out)
    assert report == dict(ok=False, held=True, reason='generated_runtime_disabled')
