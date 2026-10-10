"""Dummy-only local PTY tests. Railway, network and production are never called."""
import hashlib
import json
import os
import sys
import termios
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_brand_railway_install as helper
from test_source_brand_approval_install import mapping_bytes, environment_for

DEPLOYMENT = '11111111-1111-4111-8111-111111111111'


def source(tmp_path, raw=b'dummy', mode=0o600):
    path = tmp_path / 'source.json'
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def receipt_path(tmp_path):
    directory = tmp_path / 'receipts'
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / 'probe.json'


@pytest.fixture
def local_shell(monkeypatch):
    for name, value in zip(('RAILWAY_PROJECT_ID', 'RAILWAY_SERVICE_ID', 'RAILWAY_ENVIRONMENT_ID'),
                           (helper.PROJECT, helper.SERVICE, helper.ENVIRONMENT)):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID', DEPLOYMENT)
    monkeypatch.setenv('PS1', 'root@localtest:/app# ')
    monkeypatch.setenv('TERM', 'dumb')
    monkeypatch.setenv('BASH_SILENCE_DEPRECATION_WARNING', '1')
    monkeypatch.setattr(helper, '_PYTHON', sys.executable)
    seen = []

    def factory(argv):
        seen.append(argv)
        assert argv == ['railway', 'ssh', '-p', helper.PROJECT, '-s', helper.SERVICE,
                        '-e', 'production']
        session = helper.PtySession(['/bin/bash', '--noprofile', '--norc', '--noediting', '-i'])
        # The local PTY stands in for Railway's *remote* cooked terminal here.
        attrs = termios.tcgetattr(session.fd)
        attrs[0] |= termios.ICRNL
        attrs[1] |= termios.OPOST | termios.ONLCR
        attrs[3] |= termios.ECHO | termios.ICANON | termios.ISIG
        termios.tcsetattr(session.fd, termios.TCSANOW, attrs)
        return session
    factory.seen = seen
    return factory


def test_real_local_pty_exec_raw_dummy_probe(tmp_path, local_shell):
    receipt = receipt_path(tmp_path)
    assert helper.transport(probe=True, probe_receipt=receipt, session_factory=local_shell) == 0
    assert helper._load_receipt(receipt) == DEPLOYMENT
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_real_local_pty_installs_dummy_valid_mapping(tmp_path, local_shell, monkeypatch):
    env = environment_for(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt = receipt_path(tmp_path)
    helper.transport(probe=True, probe_receipt=receipt, session_factory=local_shell)
    raw = mapping_bytes(tmp_path)
    with source(tmp_path, raw).open('rb') as stream:
        assert helper.transport(probe_receipt=receipt, stdin_fd=stream.fileno(),
                                expected_sha256=hashlib.sha256(raw).hexdigest(),
                                session_factory=local_shell) == 0
    assert Path(env['ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE']).read_bytes() == raw
    assert raw not in repr(local_shell.seen).encode()


@pytest.mark.parametrize('raw,mode', [(b'', 0o600), (b'x' * 262145, 0o600), (b'x', 0o640)])
def test_snapshot_invalid_source(tmp_path, raw, mode):
    with source(tmp_path, raw, mode).open('rb') as stream:
        with pytest.raises(helper.InstallHold, match='source_file'):
            helper.snapshot_stdin(stream.fileno())


def test_snapshot_rejects_pipe_and_hardlink_and_nonzero_offset(tmp_path):
    r, w = os.pipe()
    try:
        with pytest.raises(helper.InstallHold, match='source_file'):
            helper.snapshot_stdin(r)
    finally:
        os.close(r); os.close(w)
    path = source(tmp_path)
    os.link(path, tmp_path / 'link')
    with path.open('rb') as stream:
        with pytest.raises(helper.InstallHold, match='source_file'):
            helper.snapshot_stdin(stream.fileno())
    (tmp_path / 'link').unlink()
    with path.open('rb') as stream:
        stream.read(1)
        with pytest.raises(helper.InstallHold, match='source_file'):
            helper.snapshot_stdin(stream.fileno())


def test_snapshot_does_not_reopen_replaced_path(tmp_path, monkeypatch):
    path = source(tmp_path)
    with path.open('rb') as stream:
        os.rename(path, tmp_path / 'original')
        path.write_bytes(b'other')
        assert helper.snapshot_stdin(stream.fileno()) == b'dummy'


def test_snapshot_detects_drift(tmp_path, monkeypatch):
    path = source(tmp_path)
    original = helper.os.read
    once = False

    def changing(fd, n):
        nonlocal once
        raw = original(fd, n)
        if not once:
            once = True
            path.write_bytes(b'changed')
        return raw
    monkeypatch.setattr(helper.os, 'read', changing)
    with path.open('rb') as stream:
        with pytest.raises(helper.InstallHold, match='source_file'):
            helper.snapshot_stdin(stream.fileno())


def test_probe_receipt_missing_does_not_launch(tmp_path):
    def forbidden(*args):
        pytest.fail('must not launch without a probe receipt')
    with source(tmp_path).open('rb') as stream:
        with pytest.raises(helper.InstallHold, match='probe_receipt'):
            helper.transport(probe_receipt=receipt_path(tmp_path), stdin_fd=stream.fileno(),
                             expected_sha256=hashlib.sha256(b'dummy').hexdigest(),
                             session_factory=forbidden)


@pytest.mark.parametrize('change', ['created', 'target', 'helper_sha256', 'extra'])
def test_receipt_expiry_target_helper_and_extra_fields_hold(tmp_path, change):
    path = receipt_path(tmp_path)
    helper._save_receipt(path, DEPLOYMENT)
    data = json.loads(path.read_bytes())
    data[change] = {'created': 1, 'target': [], 'helper_sha256': 'x', 'extra': True}[change]
    path.write_text(json.dumps(data))
    with pytest.raises(helper.InstallHold, match='probe_receipt'):
        helper._load_receipt(path)


def test_remote_wrong_target_cannot_write_authority(tmp_path, local_shell, monkeypatch):
    monkeypatch.setenv('RAILWAY_SERVICE_ID', 'other')
    with pytest.raises(helper.InstallHold):
        helper.transport(probe=True, probe_receipt=receipt_path(tmp_path), session_factory=local_shell)
    assert not receipt_path(tmp_path).exists()


def test_changed_remote_deployment_holds_before_mapping(tmp_path, local_shell, monkeypatch):
    receipt = receipt_path(tmp_path)
    helper._save_receipt(receipt, DEPLOYMENT)
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID', '22222222-2222-4222-8222-222222222222')
    with source(tmp_path).open('rb') as stream:
        with pytest.raises(helper.InstallHold):
            helper.transport(probe_receipt=receipt, stdin_fd=stream.fileno(), session_factory=local_shell,
                             expected_sha256=hashlib.sha256(b'dummy').hexdigest())


def test_remote_installer_error_is_sanitized(tmp_path, local_shell, monkeypatch):
    for key, value in environment_for(tmp_path).items():
        monkeypatch.setenv(key, value)
    receipt = receipt_path(tmp_path)
    helper._save_receipt(receipt, DEPLOYMENT)
    with source(tmp_path, b'INVALID_DUMMY_MAPPING').open('rb') as stream:
        with pytest.raises(helper.InstallHold) as raised:
            helper.transport(probe_receipt=receipt, stdin_fd=stream.fileno(), session_factory=local_shell,
                             expected_sha256=hashlib.sha256(b'INVALID_DUMMY_MAPPING').hexdigest())
    assert 'INVALID_DUMMY_MAPPING' not in str(raised.value)


def test_bootstrap_contains_no_mapping_bytes_or_source_path():
    command = helper._exec_command('a' * 48, 123, 'probe', '')
    assert len(command) < 3500
    assert command.startswith(b'exec /opt/venv/bin/python ')
    assert b' -E -u -c ' in command
    assert b' || exit 97\n' in command
    assert b'INVALID_DUMMY_MAPPING' not in command


def test_exact_rejects_extra_output():
    session = object.__new__(helper.PtySession)
    session.buffer = b'READY\nTRAILING\n'
    with pytest.raises(helper.InstallHold, match='output'):
        session.exact(b'READY')


def test_finish_rejects_rc_only_success():
    session = object.__new__(helper.PtySession)
    session.buffer = b'root@localtest:/app# '
    session._read = lambda _: False
    with pytest.raises(helper.InstallHold, match='output'):
        session.finish()


@pytest.mark.parametrize('change', ['pid', 'echo', 'early_exit', 'duplicate_ready'])
def test_bootstrap_anomalies_send_no_mapping(tmp_path, local_shell, monkeypatch, change):
    receipt = receipt_path(tmp_path)
    helper._save_receipt(receipt, DEPLOYMENT)
    code = helper._REMOTE
    if change == 'pid':
        code = code.replace('assert os.getpid()==PID', 'PID += 1; assert os.getpid()==PID')
    elif change == 'echo':
        code = code.replace('assert os.isatty(0)',
                            'attrs=termios.tcgetattr(0);attrs[3]|=termios.ECHO;termios.tcsetattr(0,termios.TCSANOW,attrs)\n assert os.isatty(0)')
    elif change == 'early_exit':
        code = code.replace('assert take(len(DUMMY))==DUMMY', 'sys.exit(0)\n assert take(len(DUMMY))==DUMMY')
    else:
        code = code.replace('assert take(len(DUMMY))==DUMMY', "say('SB_READY '+N+' '+str(os.getpid())+' '+deployment+' echo=off')\n assert take(len(DUMMY))==DUMMY")
    monkeypatch.setattr(helper, '_REMOTE', code)
    # Recreate a receipt for the test-injected protocol source. No live receipt.
    receipt.unlink()
    helper._save_receipt(receipt, DEPLOYMENT)
    sent = []

    def traced_factory(argv):
        session = local_shell(argv)
        write = session.write

        def tracked(raw, timeout=30):
            sent.append(raw)
            return write(raw, timeout)
        session.write = tracked
        return session
    with source(tmp_path, b'UNIQUE_DUMMY_MAPPING').open('rb') as stream:
        with pytest.raises(helper.InstallHold):
            helper.transport(probe_receipt=receipt, stdin_fd=stream.fileno(), session_factory=traced_factory,
                             expected_sha256=hashlib.sha256(b'UNIQUE_DUMMY_MAPPING').hexdigest())
    assert all(b'UNIQUE_DUMMY_MAPPING' not in frame for frame in sent)


def test_received_digest_mismatch_prevents_commit(tmp_path, local_shell, monkeypatch):
    env = environment_for(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = helper._REMOTE.replace("say('SB_RECEIVED '+N+' '+digest)", "say('SB_RECEIVED '+N+' '+'0'*64)")
    monkeypatch.setattr(helper, '_REMOTE', code)
    receipt = receipt_path(tmp_path)
    helper._save_receipt(receipt, DEPLOYMENT)
    raw = mapping_bytes(tmp_path)
    with source(tmp_path, raw).open('rb') as stream:
        with pytest.raises(helper.InstallHold):
            helper.transport(probe_receipt=receipt, stdin_fd=stream.fileno(), session_factory=local_shell,
                             expected_sha256=hashlib.sha256(raw).hexdigest())
    assert not Path(env['ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE']).exists()


def test_installed_digest_mismatch_is_not_success(tmp_path, local_shell, monkeypatch):
    env = environment_for(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(helper, '_REMOTE', helper._REMOTE.replace(
        "say('SB_SUCCESS '+N+' '+digest)", "say('SB_SUCCESS '+N+' '+'0'*64)"))
    receipt = receipt_path(tmp_path)
    helper._save_receipt(receipt, DEPLOYMENT)
    raw = mapping_bytes(tmp_path)
    with source(tmp_path, raw).open('rb') as stream:
        with pytest.raises(helper.InstallHold):
            helper.transport(probe_receipt=receipt, stdin_fd=stream.fileno(), session_factory=local_shell,
                             expected_sha256=hashlib.sha256(raw).hexdigest())
    # A lost/bad receipt after COMMIT is ambiguous, so independent readback is required.
    assert Path(env['ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE']).exists()


def test_cli_unknown_private_argument_is_not_logged(capsys):
    assert helper.main(['UNIQUE_DUMMY_MAPPING']) == 1
    assert capsys.readouterr().err == 'railway_install_hold_usage\n'


def test_known_prompt_controls_and_banner_are_bounded():
    session = object.__new__(helper.PtySession)
    session.buffer = (b'New version available: v5.64.1 visit https://docs.railway.com/guides/cli for more info\r\n'
                      b'Using SSH key: /dummy/key.pub\r\n'
                      b'\x1b[?2004h\x1b]0;root@container-123: /app\x07root@container-123:/app# ')
    session.prompt(initial=True)
    assert session.buffer == b''


@pytest.mark.parametrize('expected', [None, '0' * 64, 'not-a-digest'])
def test_missing_or_mismatched_reviewed_digest_never_launches(tmp_path, expected):
    def forbidden(*args):
        pytest.fail('unreviewed mapping must not launch Railway')
    with source(tmp_path).open('rb') as stream:
        with pytest.raises(helper.InstallHold, match='reviewed_digest'):
            helper.transport(probe_receipt=receipt_path(tmp_path), stdin_fd=stream.fileno(),
                             expected_sha256=expected, session_factory=forbidden)
