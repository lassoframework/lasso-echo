"""Operator install CLI: stdin-only authority bytes, staged, loader-validated."""
from dataclasses import asdict
import json
import os
import stat
import io
from types import SimpleNamespace

import pytest

from agent.source_brand_approval_install import install_approved_mappings, main
from agent.source_brand_startup import load_approved_mappings
from agent.source_brand_ingest import CaptureIngestError
from test_source_brand_capture_runner import setup


def mapping_bytes(tmp_path):
    original, *_ = setup(tmp_path)
    entries = [asdict(x) for x in original.collector._resolve._approved.values()]
    return json.dumps({'schema_version': 1, 'approved_mappings': entries}).encode()


def environment_for(tmp_path):
    return {
        'ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE': str(tmp_path / 'private' / 'approved.json'),
        'ECHO_SOURCE_CAPTURE_JOURNAL_DIR': str(tmp_path / 'journal'),
    }


def test_installs_loader_valid_authority_with_private_modes(tmp_path):
    raw = mapping_bytes(tmp_path)
    env = environment_for(tmp_path)
    install_approved_mappings(raw, environ=env)
    target = tmp_path / 'private' / 'approved.json'
    assert load_approved_mappings(str(target))
    assert stat.S_IMODE((tmp_path / 'private').stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / 'journal').stat().st_mode) == 0o700


def test_invalid_bytes_fail_closed_and_preserve_existing_authority(tmp_path):
    raw = mapping_bytes(tmp_path)
    env = environment_for(tmp_path)
    install_approved_mappings(raw, environ=env)
    target = tmp_path / 'private' / 'approved.json'
    before = target.read_bytes()
    with pytest.raises(CaptureIngestError):
        install_approved_mappings(b'{"schema_version":1,"approved_mappings":[]}', environ=env)
    assert target.read_bytes() == before
    assert load_approved_mappings(str(target))
    assert not list((tmp_path / 'private').glob('.approved-*'))


def test_missing_target_env_holds_and_installs_nothing(tmp_path):
    with pytest.raises(Exception, match='private_mapping_authority_required'):
        install_approved_mappings(b'{}', environ={})
    assert not list(tmp_path.iterdir())


def test_relative_target_path_holds(tmp_path):
    env = {'ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE': 'relative/approved.json'}
    with pytest.raises(Exception, match='private_mapping_authority_required'):
        install_approved_mappings(b'{}', environ=env)
    assert not list(tmp_path.iterdir())


def test_oversized_stdin_holds(tmp_path):
    env = environment_for(tmp_path)
    with pytest.raises(Exception, match='approved_mapping_schema_invalid'):
        install_approved_mappings(b' ' * 262145, environ=env)
    assert not (tmp_path / 'private').exists()


def test_main_reads_stdin_and_never_echoes_mapping(tmp_path, capsys, monkeypatch):
    raw = mapping_bytes(tmp_path)
    env = environment_for(tmp_path)
    monkeypatch.setattr('agent.source_brand_approval_install.os.environ', env)
    monkeypatch.setattr('agent.source_brand_approval_install.sys.stdin',
                        SimpleNamespace(buffer=io.BytesIO(raw)))
    assert main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == ''
    assert raw.decode()[:20] not in captured.err
    assert load_approved_mappings(env['ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE'])


def test_main_rejects_arguments_and_bad_stdin(tmp_path, capsys, monkeypatch):
    assert main(['extra']) == 2
    monkeypatch.setattr('agent.source_brand_approval_install.sys.argv',
                        ['source_brand_approval_install', 'extra'])
    assert main() == 2
    monkeypatch.setattr('agent.source_brand_approval_install.os.environ',
                        environment_for(tmp_path))
    monkeypatch.setattr('agent.source_brand_approval_install.sys.stdin',
                        SimpleNamespace(buffer=io.BytesIO(b'')))
    assert main([]) == 1
    assert 'approved_mapping_schema_invalid' in capsys.readouterr().err
    assert not (tmp_path / 'private').exists()


def test_missing_journal_path_holds_before_publish(tmp_path):
    env = environment_for(tmp_path)
    env.pop('ECHO_SOURCE_CAPTURE_JOURNAL_DIR')
    with pytest.raises(Exception, match='private_capture_journal_required'):
        install_approved_mappings(mapping_bytes(tmp_path), environ=env)
    assert not (tmp_path / 'private' / 'approved.json').exists()


def test_replace_failure_preserves_existing_authority(tmp_path, monkeypatch):
    env = environment_for(tmp_path)
    raw = mapping_bytes(tmp_path)
    install_approved_mappings(raw, environ=env)
    target = tmp_path / 'private' / 'approved.json'
    before = target.read_bytes()

    def fail_replace(*_args):
        raise OSError('synthetic replace failure')

    monkeypatch.setattr('agent.source_brand_approval_install.os.replace', fail_replace)
    with pytest.raises(Exception, match='private_mapping_authority_required'):
        install_approved_mappings(raw, environ=env)
    assert target.read_bytes() == before
    assert load_approved_mappings(str(target))


def test_staging_permission_failure_closes_descriptor(tmp_path, monkeypatch):
    env = environment_for(tmp_path)
    import agent.source_brand_approval_install as installer
    opened = []
    real_mkstemp = installer.tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        opened.append(fd)
        return fd, name

    def fail_chmod(*_args):
        raise OSError('synthetic permission failure')

    monkeypatch.setattr(installer.tempfile, 'mkstemp', capture_mkstemp)
    monkeypatch.setattr(installer.os, 'fchmod', fail_chmod)
    with pytest.raises(Exception, match='private_mapping_authority_required'):
        install_approved_mappings(mapping_bytes(tmp_path), environ=env)
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not list((tmp_path / 'private').glob('.approved-*'))


def test_stdin_io_failure_prints_only_hold_code(capsys, monkeypatch):
    class BrokenInput:
        def read(self, _limit):
            raise OSError('sensitive input failure detail')

    monkeypatch.setattr('agent.source_brand_approval_install.sys.stdin',
                        SimpleNamespace(buffer=BrokenInput()))
    assert main([]) == 1
    assert capsys.readouterr().err == 'private_mapping_authority_required\n'
