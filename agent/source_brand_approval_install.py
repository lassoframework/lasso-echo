"""Operator-side installer for the private approved-mappings authority file.

The reviewed mapping JSON arrives on stdin only: never CLI arguments,
environment values, or log lines. The target path comes exclusively from
`ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE` (the loader's own contract).

Install is staged and atomic: bytes are written to a fresh owner-only
temporary file in the authority directory, validated with the real loader
(`load_approved_mappings`) against that staged file, and only then renamed
over the target. A failure before replacement leaves any previous authority
untouched. The loader's ownership/permission invariants (0700
directory / 0600 regular file, current OS owner, no symlinks or hard links)
are enforced here, and the private journal directory is created 0700 if
absent. This module never starts the capture runner, fetches, or ingests.

Railway remote stdin forwarding was not proven, so this is a container-side
method: run it inside the container with its stdin attached.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from .source_brand_ingest import CaptureIngestError
from .source_brand_startup import load_approved_mappings

_MAX_STDIN_BYTES = 262144


class _InstallHold(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _hold(code):
    raise _InstallHold(code) from None


def _prepare_directory(path: Path) -> None:
    """Ensure the authority directory is private and owned by the operator."""
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        _hold('private_mapping_authority_required')
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = path.stat()
    except FileExistsError:
        _hold('private_mapping_authority_required')
    except OSError:
        _hold('private_mapping_authority_required')
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISDIR(info.st_mode):
        _hold('private_mapping_authority_required')


def _stage_and_validate(target: Path, raw: bytes) -> Path:
    """Write bytes to a fresh 0600 temp file and validate via the real loader."""
    try:
        fd, staged_name = tempfile.mkstemp(prefix='.approved-', dir=str(target.parent))
    except OSError:
        _hold('private_mapping_authority_required')
    owned_fd = fd
    try:
        os.fchmod(fd, 0o600)
        staged_file = os.fdopen(fd, 'wb')
        owned_fd = None
        with staged_file as staged:
            staged.write(raw)
            staged.flush()
            os.fsync(staged.fileno())
        # The loader enforces schema, identity, expiry, URL and file invariants.
        load_approved_mappings(staged_name)
        return Path(staged_name)
    except BaseException as exc:
        if owned_fd is not None:
            try:
                os.close(owned_fd)
            except OSError:
                pass
        try:
            os.unlink(staged_name)
        except OSError:
            pass
        if isinstance(exc, OSError):
            _hold('private_mapping_authority_required')
        raise


def install_approved_mappings(raw: bytes, *, environ=None) -> None:
    """Validate stdin bytes and atomically install them as loader authority.

    `raw` is the only content channel. `environ` may override the process
    environment for the target path variable in tests.
    """
    env = dict(os.environ if environ is None else environ)
    target_name = env.get('ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE')
    if (not isinstance(target_name, str) or not target_name
            or not Path(target_name).is_absolute()):
        _hold('private_mapping_authority_required')
    target = Path(target_name)
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_STDIN_BYTES:
        _hold('approved_mapping_schema_invalid')
    _prepare_directory(target.parent)
    journal_name = env.get('ECHO_SOURCE_CAPTURE_JOURNAL_DIR')
    if not isinstance(journal_name, str) or not journal_name:
        _hold('private_capture_journal_required')
    journal = Path(journal_name)
    if not journal.is_absolute():
        _hold('private_capture_journal_required')
    _prepare_directory(journal)
    staged = _stage_and_validate(target, raw)
    # Atomic publish: the staged file already passed the real loader. If a
    # previous authority exists it is replaced only after the new one is
    # fully valid. The staged inode already has its final 0600 mode, so no
    # fallible mutation or validation is performed after replacement.
    try:
        os.replace(staged, target)
    except OSError:
        try:
            staged.unlink()
        except OSError:
            pass
        _hold('private_mapping_authority_required')


def main(argv=None) -> int:
    """CLI entry: mapping JSON on stdin, target path from the env contract.

    Only hold codes are printed; mapping content is never echoed to logs.
    """
    args = sys.argv[1:] if argv is None else argv
    if args:
        print('approved_mapping_install_usage', file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        install_approved_mappings(raw)
    except _InstallHold as hold:
        print(hold.code, file=sys.stderr)
        return 1
    except CaptureIngestError as hold:
        print(hold.args[0] if hold.args else 'approved_mapping_schema_invalid',
              file=sys.stderr)
        return 1
    except OSError:
        print('private_mapping_authority_required', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
