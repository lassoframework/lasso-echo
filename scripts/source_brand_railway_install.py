"""Guarded operator transport for the dedicated source service, never publishing.

First run --probe --probe-receipt /private/new-receipt.json after deployment.
For installation supply --expected-sha256 from independent mapping review and
--probe-receipt /private/new-receipt.json with stdin
redirected from the reviewed owner-only regular mapping file. No mapping path
argument is accepted. Probe receipts expire after 15 minutes and bind this
helper, the fixed target and the observed deployment. Probe mode sends only a
built-in public dummy and makes no authority write.

This transport relies on Railway's independently observed interactive-shell
exec behavior. Python must retain the shell PID, verify target environment and
ECHO off, and replace the shell before private input. All PTY output stays
private in memory; unexpected output is a hold. The installer digest receipt
does not replace independent later production readback or approve the mapping.
Any failure after COMMIT can mean installation succeeded without a final receipt;
perform independent readback before retrying. No private transfer has been run
on the dedicated source service by this implementation package.
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import pty
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import termios
import time
import tty
import uuid
import zlib
from pathlib import Path

PROJECT = 'b49e41ea-ae21-4668-bcc8-022d596bbc69'
SERVICE = '5b260bd7-ca42-4a75-b362-2a997a1cddfa'
ENVIRONMENT = '53cb47a0-bb88-4e4f-9209-41e66ea18a11'
_MAX_BYTES = 262144
_DUMMY = b'SB_TRANSPORT_DUMMY_V1'
_PYTHON = '/opt/venv/bin/python'
_RECEIPT_AGE = 900
_PROMPT = re.compile(
    rb'(?:\x1b\[\?2004l\r)?(?:\x1b\[\?2004h)?(?:\x1b\]0;root@[A-Za-z0-9_-]+: /app\x07)?'
    rb'root@[A-Za-z0-9_-]+:/app# '
)
_BANNER = re.compile(
    rb'(?:New version available: v[0-9.]+ visit '
    rb'https://docs\.railway\.com/guides/cli for more info\r?\n)?'
    rb'(?:Using SSH key: [^\r\n\x00-\x1f]{1,1024}\r?\n)?'
)
_CLOSE = re.compile(rb'(?:\r?\n)*(?:Connection to ssh\.railway\.com closed\.\r?\n)?')


class InstallHold(Exception):
    """Only fixed public codes may escape the transport."""

    def __init__(self, code='transport'):
        super().__init__('railway_install_hold_' + code)


def _hold(code):
    raise InstallHold(code) from None


def _private_stat(info, limit):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and info.st_nlink == 1
            and 0 < info.st_size <= limit)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot_stdin(fd=0, *, limit=_MAX_BYTES):
    """Validate and copy one already-open inode, never reopen a source path."""
    try:
        before = os.fstat(fd)
        if not _private_stat(before, limit) or os.lseek(fd, 0, os.SEEK_CUR) != 0:
            _hold('source_file')
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(16384, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if (_identity(before) != _identity(after) or total != before.st_size
                or not 0 < total <= limit):
            _hold('source_file')
        return b''.join(chunks)
    except InstallHold:
        raise
    except (OSError, ValueError):
        _hold('source_file')


def _helper_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _receipt_dir(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        _hold('probe_receipt')
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        _hold('probe_receipt')
    return path


def _load_receipt(path):
    try:
        path = _receipt_dir(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            raw = snapshot_stdin(fd, limit=4096)
        finally:
            os.close(fd)
        receipt = json.loads(raw)
        if (set(receipt) != {'version', 'target', 'helper_sha256', 'deployment', 'created'}
                or receipt['version'] != 1
                or receipt['target'] != [PROJECT, SERVICE, ENVIRONMENT]
                or receipt['helper_sha256'] != _helper_digest()
                or not isinstance(receipt['created'], (int, float))
                or isinstance(receipt['created'], bool)
                or not 0 <= time.time() - receipt['created'] <= _RECEIPT_AGE
                or str(uuid.UUID(receipt['deployment'])) != receipt['deployment']):
            _hold('probe_receipt')
        return receipt['deployment']
    except InstallHold:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        _hold('probe_receipt')


def _save_receipt(path, deployment):
    try:
        path = _receipt_dir(path)
        raw = json.dumps({'version': 1, 'target': [PROJECT, SERVICE, ENVIRONMENT],
                          'helper_sha256': _helper_digest(), 'deployment': deployment,
                          'created': time.time()}, sort_keys=True).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except InstallHold:
        raise
    except OSError:
        _hold('probe_receipt')


class PtySession:
    """A real controlling PTY. Child output is never forwarded to the operator."""

    def __init__(self, argv):
        self.fd, slave = pty.openpty()
        self.buffer = b''
        tty.setraw(slave)

        def own_terminal():
            os.setsid()
            import fcntl
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         close_fds=True, preexec_fn=own_terminal)
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)
        os.set_blocking(self.fd, False)

    def _read(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
            _hold('timeout')
        try:
            chunk = os.read(self.fd, 8192)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return False
            raise
        self.buffer += chunk
        if len(self.buffer) > 16384:
            _hold('output')
        return bool(chunk)

    def write(self, raw, timeout=30):
        deadline = time.monotonic() + timeout
        position = 0
        while position < len(raw):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _hold('timeout')
            readable, writable, _ = select.select([self.fd], [self.fd], [], remaining)
            if readable:
                # No output is allowed during an input frame. Never print it.
                if not self._read(deadline) or self.buffer:
                    _hold('output')
            if writable:
                position += os.write(self.fd, raw[position:position + 4096])

    def prompt(self, initial=False, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            start = 0
            if initial:
                banner = _BANNER.match(self.buffer)
                start = banner.end() if banner else 0
            found = _PROMPT.match(self.buffer, start)
            if found:
                if found.end() != len(self.buffer):
                    _hold('output')
                self.buffer = b''
                return
            if not self._read(deadline):
                _hold('output')

    def line(self, timeout=30):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            if not self._read(deadline):
                _hold('output')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return line.removesuffix(b'\r').removeprefix(b'\x1b[?2004l\r')

    def exact(self, expected, timeout=30, allow_pending=False):
        if self.line(timeout) != expected or (self.buffer and not allow_pending):
            _hold('output')

    def finish(self, timeout=30):
        deadline = time.monotonic() + timeout
        while self._read(deadline):
            pass
        if not _CLOSE.fullmatch(self.buffer):
            _hold('output')
        if self.proc.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            _hold('exit')

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=5)
        os.close(self.fd)


_REMOTE = r'''
import os,sys,termios,tty,hashlib,select,signal,stat,uuid
out=os.dup(1)
null=os.open('/dev/null',os.O_WRONLY)
os.dup2(null,1);os.dup2(null,2);os.close(null)
def say(s): os.write(out,(s+'\n').encode())
def take(n):
 b=b''
 while len(b)<n:
  c=os.read(0,n-len(b))
  if not c: raise ValueError()
  b+=c
 return b
def line():
 b=b''
 while not b.endswith(b'\n'):
  if len(b)>200: raise ValueError()
  b+=take(1)
 return b
def alarm(*a): raise ValueError()
signal.signal(signal.SIGALRM,alarm);signal.alarm(120)
try:
 assert os.getpid()==PID
 assert [os.environ.get(k) for k in ('RAILWAY_PROJECT_ID','RAILWAY_SERVICE_ID','RAILWAY_ENVIRONMENT_ID')]==TARGET
 deployment=os.environ['RAILWAY_DEPLOYMENT_ID']
 assert str(uuid.UUID(deployment))==deployment
 assert not EXPECTED or deployment==EXPECTED
 assert os.isatty(0) and not termios.tcgetattr(0)[3] & (termios.ECHO|termios.ECHONL)
 if MODE=='install':
  from agent.source_brand_approval_install import install_approved_mappings
 tty.setraw(0)
 say('SB_READY '+N+' '+str(os.getpid())+' '+deployment+' echo=off')
 assert take(len(DUMMY))==DUMMY
 assert take(len(N)+8)==('SB_END '+N+'\n').encode()
 say('SB_PROBE '+N+' '+hashlib.sha256(DUMMY).hexdigest()+' '+deployment)
 if MODE=='probe':
  signal.alarm(0);sys.exit(0)
 assert line()==('SB_BEGIN '+N+'\n').encode()
 fields=line().decode('ascii').strip().split(' ')
 assert len(fields)==2 and fields[0].isdigit() and len(fields[1])==64
 n=int(fields[0]);digest=fields[1]
 assert 0<n<=262144 and all(c in '0123456789abcdef' for c in digest)
 say('SB_BODY '+N)
 raw=take(n)
 assert take(len(N)+8)==('SB_END '+N+'\n').encode()
 assert hashlib.sha256(raw).hexdigest()==digest
 say('SB_RECEIVED '+N+' '+digest)
 assert line()==('SB_COMMIT '+N+'\n').encode()
 assert not select.select([0],[],[],0.1)[0]
 install_approved_mappings(raw)
 fd=os.open(os.environ['ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE'],os.O_RDONLY|os.O_NOFOLLOW)
 try:
  info=os.fstat(fd)
  assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077 and info.st_nlink==1 and info.st_size==n
  installed=b''
  while len(installed)<=n:
   chunk=os.read(fd,min(16384,n+1-len(installed)))
   if not chunk: break
   installed+=chunk
  after=os.fstat(fd)
  assert (after.st_dev,after.st_ino,after.st_mode,after.st_uid,after.st_nlink,after.st_size,after.st_mtime_ns,after.st_ctime_ns)==(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
  assert hashlib.sha256(installed).hexdigest()==digest
 finally: os.close(fd)
 say('SB_SUCCESS '+N+' '+digest)
 signal.alarm(0)
except SystemExit: raise
except BaseException:
 say('SB_HOLD '+N);sys.exit(1)
'''


def _exec_command(nonce, pid, mode, deployment):
    source = ('N=' + repr(nonce) + '\nPID=' + repr(pid) + '\nMODE=' + repr(mode)
              + '\nEXPECTED=' + repr(deployment) + '\nTARGET='
              + repr([PROJECT, SERVICE, ENVIRONMENT]) + '\nDUMMY=' + repr(_DUMMY) + '\n' + _REMOTE)
    # Only public bootstrap code is compressed. No mapping enters this command.
    packed = base64.b64encode(zlib.compress(source.encode())).decode('ascii')
    bootstrap = "import base64,zlib;exec(zlib.decompress(base64.b64decode(" + repr(packed) + ")))"
    # Ignore PYTHONOPTIMIZE/PYTHONPATH from the remote environment. The trusted
    # /app working directory supplies the repository's agent package.
    command = 'exec ' + shlex.quote(_PYTHON) + ' -E -u -c ' + shlex.quote(bootstrap) + ' || exit 97\n'
    if len(command.encode()) > 3500:
        _hold('bootstrap')
    return command.encode()


def transport(*, probe_receipt, probe=False, expected_sha256=None,
              stdin_fd=0, session_factory=PtySession):
    raw = None if probe else snapshot_stdin(stdin_fd)
    if not probe and (not isinstance(expected_sha256, str)
                      or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
                      or hashlib.sha256(raw).hexdigest() != expected_sha256):
        _hold('reviewed_digest')
    deployment = '' if probe else _load_receipt(probe_receipt)
    nonce = os.urandom(24).hex()
    argv = ['railway', 'ssh', '-p', PROJECT, '-s', SERVICE, '-e', 'production']
    session = None
    try:
        session = session_factory(argv)
        session.prompt(initial=True)
        session.write(b'stty -echo -echonl -icanon min 1 time 0\n')
        # The nonsecret command echoes once before remote echo is disabled.
        session.exact(b'stty -echo -echonl -icanon min 1 time 0', allow_pending=True)
        session.prompt()
        command = ("unset HISTFILE; printf 'SB_SHELL_%s %s\\n' " + nonce + ' "$$"\n').encode()
        session.write(command)
        shell = session.line()
        match = re.fullmatch(('SB_SHELL_' + nonce + r' ([1-9][0-9]*)').encode(), shell)
        if not match:
            _hold('shell')
        pid = int(match[1])
        session.prompt()
        session.write(_exec_command(nonce, pid, 'probe' if probe else 'install', deployment))
        # Bash exits bracketed paste mode when executing a command.
        ready = session.line()
        ready = ready.removeprefix(b'\x1b[?2004l\r')
        prefix = ('SB_READY ' + nonce + ' ' + str(pid) + ' ').encode()
        if not ready.startswith(prefix) or not ready.endswith(b' echo=off') or session.buffer:
            _hold('ready')
        observed = ready[len(prefix):-len(b' echo=off')].decode('ascii')
        if str(uuid.UUID(observed)) != observed or (deployment and deployment != observed):
            _hold('target')
        footer = ('SB_END ' + nonce + '\n').encode()
        session.write(_DUMMY + footer)
        session.exact(('SB_PROBE ' + nonce + ' ' + hashlib.sha256(_DUMMY).hexdigest()
                       + ' ' + observed).encode(), allow_pending=probe)
        if not probe:
            digest = hashlib.sha256(raw).hexdigest()
            session.write(('SB_BEGIN ' + nonce + '\n' + str(len(raw)) + ' ' + digest + '\n').encode())
            session.exact(('SB_BODY ' + nonce).encode())
            session.write(raw + footer)
            session.exact(('SB_RECEIVED ' + nonce + ' ' + digest).encode())
            session.write(('SB_COMMIT ' + nonce + '\n').encode())
            session.exact(('SB_SUCCESS ' + nonce + ' ' + digest).encode(), timeout=60, allow_pending=True)
        session.finish()
        if probe:
            _save_receipt(probe_receipt, observed)
        return 0
    except InstallHold:
        raise
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        _hold('transport')
    finally:
        if session is not None:
            try:
                session.close()
            except (OSError, subprocess.SubprocessError):
                pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _hold('usage')


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--probe-receipt', required=True)
    parser.add_argument('--expected-sha256')
    try:
        args = parser.parse_args(argv)
        result = transport(probe=args.probe, probe_receipt=args.probe_receipt,
                           expected_sha256=args.expected_sha256)
        if args.probe:
            print('railway_install_probe_verified')
        else:
            # Public operator evidence binds independently reviewed bytes to
            # the fixed source service. No path or mapping content is printed.
            print('railway_install_digest_verified sha256=' + args.expected_sha256
                  + ' service=' + SERVICE + ' environment=' + ENVIRONMENT)
        return result
    except InstallHold as hold:
        print(str(hold), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
