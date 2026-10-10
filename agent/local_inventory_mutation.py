"""DRAFT local supply mutation fence. OFF until explicitly configured.

This is a reusable writer seam, not evidence that all local writers participate.
The owner must provision an isolated mutator login, active epoch, canonical
library and durable SQLite/journal paths. No service-role credential fallback.
PG begin commits before flock. Files are atomically replaced and SQLite commits
under one canonical gym flock; PG completion happens only after its release.
Any uncertain response or crash keeps the exact mutation pending indefinitely.
Local journals are operator reconciliation evidence, never automatic cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

BEGIN_RPC = 'fixer_inventory_mutation_begin_20261008'
COMPLETE_RPC = 'fixer_inventory_mutation_complete_20261008'
RECEIPT_RPC = 'fixer_inventory_mutation_receipt_20261008'
MUTATOR_ROLE = 'fixer_inventory_mutator_20261008'
_GYM = re.compile(r'[a-z0-9][a-z0-9_-]{0,127}\Z')
_KIND = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_FORBIDDEN_ROLES = ('service_role', 'anon', 'authenticated',
                    'fixer_forward_media_owner_20261006',
                    'fixer_forward_media_attester_20261006')


class MutationHold(RuntimeError):
    """Static error codes only. Never expose database errors or credentials."""


def enabled():
    return os.environ.get('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', '').lower() == 'true'


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def atomic_write_bytes(path, data):
    """Same-directory rename + file/directory fsync; never expose truncation."""
    path = Path(path)
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise MutationHold('local_mutation_path_invalid')
    fd, temporary = tempfile.mkstemp(prefix='.inventory-mutation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path, value):
    atomic_write_bytes(path, canonical_json(value).encode())


@dataclass(frozen=True)
class MutationConfig:
    gym_id: str
    epoch_id: str
    library_path: Path
    sqlite_path: Path
    journal_directory: Path
    lock_directory: Path

    def validate(self):
        from . import config
        if (not _GYM.fullmatch(self.gym_id) or self.gym_id.endswith(('_ig', '_fb', '_gbp'))
                or not _uuid(self.epoch_id)):
            raise MutationHold('local_mutation_binding_invalid')
        for path in (self.library_path, self.sqlite_path,
                     self.journal_directory, self.lock_directory):
            if not isinstance(path, Path) or not path.is_absolute() or path.resolve() != path:
                raise MutationHold('local_mutation_path_invalid')
        # The deployment's canonical library root is trusted configuration.
        # A caller-supplied pair of gym/path values cannot establish ownership.
        root = Path(config.LIBRARY_PATH).absolute()
        if root.resolve() != root or self.library_path != root / self.gym_id:
            raise MutationHold('local_mutation_library_binding_invalid')
        if not self.library_path.is_dir() or not self.sqlite_path.is_file():
            raise MutationHold('local_mutation_durable_paths_unavailable')
        if self.journal_directory == self.library_path or self.lock_directory == self.library_path:
            raise MutationHold('local_mutation_path_invalid')

    def asset_path(self, path):
        self.validate()
        path = Path(path)
        if (not path.is_absolute() or path.resolve() != path
                or path.parent != self.library_path):
            raise MutationHold('local_mutation_asset_binding_invalid')
        return path


class MutationAuthority:
    """Dedicated DB-API connection; each RPC is one committed PG transaction."""
    def __init__(self, connection, expected_login):
        self.connection = connection
        self.expected_login = expected_login

    @classmethod
    def from_environment(cls):
        dsn = os.environ.get('LOCAL_INVENTORY_MUTATOR_DSN', '')
        login = os.environ.get('LOCAL_INVENTORY_MUTATOR_LOGIN', '')
        if not dsn.strip() or not login or login != login.strip() or login in _FORBIDDEN_ROLES:
            raise MutationHold('local_mutation_dedicated_authority_unavailable')
        try:
            import psycopg
            connection = psycopg.connect(dsn, autocommit=False, connect_timeout=10,
                                        options='-c lock_timeout=5000 -c statement_timeout=20000')
        except Exception:
            raise MutationHold('local_mutation_dedicated_authority_unavailable') from None
        return cls(connection, login)

    def _rpc(self, name, arguments):
        try:
            if (self.connection.autocommit is not False
                    or getattr(getattr(self.connection, 'info', None), 'transaction_status', None) != 0):
                raise MutationHold('local_mutation_dedicated_authority_unavailable')
            with self.connection.cursor() as cur:
                cur.execute('select current_user, pg_has_role(current_user, %s, %s), '
                            'rolsuper, rolbypassrls from pg_roles where rolname=current_user',
                            (MUTATOR_ROLE, 'member'))
                row = cur.fetchone()
                if (not row or row[0] != self.expected_login or row[1] is not True
                        or row[2] is not False or row[3] is not False):
                    raise MutationHold('local_mutation_dedicated_authority_unavailable')
                for role in _FORBIDDEN_ROLES:
                    cur.execute('select pg_has_role(current_user, %s, %s)', (role, 'member'))
                    if cur.fetchone() != (False,):
                        raise MutationHold('local_mutation_dedicated_authority_unavailable')
                cur.execute('select public.' + name + '(' + ','.join(['%s'] * len(arguments)) + ')',
                            arguments)
                row = cur.fetchone()
            if not row or not isinstance(row[0], dict):
                raise MutationHold('local_mutation_authority_response_invalid')
            result = row[0]
            self.connection.commit()
            return result
        except Exception:
            try:
                self.connection.rollback()
            except Exception:
                pass
            # Even COMMIT failure is uncertain, never proof that begin failed.
            raise MutationHold('local_mutation_authority_uncertain') from None

    def begin(self, request):
        return self._rpc(BEGIN_RPC, (request['mutation_id'], request['gym_id'],
                                    request['epoch_id'], request['kind'], request['request_digest']))

    def complete(self, request, result_digest):
        return self._rpc(COMPLETE_RPC, (request['mutation_id'], request['gym_id'],
                                       request['epoch_id'], request['request_digest'], result_digest))

    def receipt(self, request):
        return self._rpc(RECEIPT_RPC, (request['mutation_id'], request['gym_id'],
                                      request['epoch_id'], request['request_digest']))

    def close(self):
        self.connection.close()


def _receipt(receipt, request, state, result_digest=None, generation=None):
    if (not isinstance(receipt, dict)
            or any(receipt.get(key) != request[key] for key in
                   ('mutation_id', 'gym_id', 'epoch_id', 'kind', 'request_digest'))
            or receipt.get('state') != state or type(receipt.get('generation')) is not int
            or receipt['generation'] < 1 or not receipt.get('begun_at')
            or receipt.get('result_digest') != result_digest
            or (generation is not None and receipt['generation'] != generation)
            or (state == 'complete' and not receipt.get('completed_at'))):
        raise MutationHold('local_mutation_receipt_invalid')


def assert_settled(config):
    """Unresolved local attempts block retries, including already-approved paths."""
    config.validate()
    if not config.journal_directory.exists():
        return
    try:
        for path in config.journal_directory.glob('*.json'):
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or 'gym_id' not in record:
                raise ValueError
            if record['gym_id'] == config.gym_id and record.get('local_state') != 'complete':
                raise MutationHold('local_mutation_reconciliation_required')
    except MutationHold:
        raise
    except Exception:
        raise MutationHold('local_mutation_journal_unavailable') from None


def run(config, authority, kind, request_payload, apply):
    """apply(conn) performs local effects only and returns canonical result data.

    Files and SQLite cannot share a transaction. Partial files on error remain
    fenced by durable pending PG state and require operator reconciliation.
    The caller must not swallow a hold and certify success or clear depletion.
    """
    config.validate()
    if not _KIND.fullmatch(kind):
        raise MutationHold('local_mutation_kind_invalid')
    assert_settled(config)
    request = {'mutation_id': str(uuid.uuid4()), 'gym_id': config.gym_id,
               'epoch_id': config.epoch_id, 'kind': kind,
               'request_digest': digest(request_payload)}
    config.journal_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = config.journal_directory / (request['mutation_id'] + '.json')
    record = dict(request, local_state='prepared')
    atomic_write_json(journal, record)  # retain exact identity BEFORE uncertain begin
    receipt = authority.begin(request)
    _receipt(receipt, request, 'pending')
    record.update(local_state='pending', begin_receipt=receipt)
    atomic_write_json(journal, record)
    lock_path = config.lock_directory / (config.gym_id + '.lock')
    if lock_path.is_symlink():
        raise MutationHold('local_mutation_lock_unavailable')
    try:
        with open(lock_path, 'a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            conn = sqlite3.connect(config.sqlite_path.as_uri() + '?mode=rw', uri=True, timeout=30)
            try:
                conn.execute('PRAGMA synchronous=FULL')
                conn.execute('BEGIN IMMEDIATE')
                result = apply(conn)
                result_digest = digest(result)
                conn.commit()
                record.update(local_state='local_committed', result_digest=result_digest)
                atomic_write_json(journal, record)
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        completed = authority.complete(request, result_digest)
        _receipt(completed, request, 'complete', result_digest, receipt['generation'])
        record.update(local_state='complete', complete_receipt=completed)
        atomic_write_json(journal, record)
        return result
    except Exception:
        raise MutationHold('local_mutation_pending_reconciliation') from None


def configured(gym_id, library_path):
    """Explicit deployment configuration; no guessed epoch or transient DB."""
    from . import db
    database = Path(db.db_path()).absolute()
    if not os.environ.get('AGENT_DB_PATH') and not os.environ.get('AGENT_DATA_DIR'):
        raise MutationHold('local_mutation_durable_paths_unavailable')
    result = MutationConfig(gym_id, os.environ.get('LOCAL_INVENTORY_MUTATION_EPOCH', ''),
                            Path(library_path).absolute(), database,
                            database.parent / 'inventory-mutation-receipts',
                            database.parent / 'inventory-mutation-locks')
    result.validate()
    return result


def gym_for_asset(path):
    """Only canonical direct-child library paths bind implicitly to a gym."""
    from . import config
    path = Path(path).absolute()
    root = Path(config.LIBRARY_PATH).absolute()
    if path.resolve() != path or root.resolve() != root or path.parent.parent != root:
        raise MutationHold('local_mutation_asset_binding_invalid')
    gym = path.parent.name
    if not _GYM.fullmatch(gym) or gym.endswith(('_ig', '_fb', '_gbp')):
        raise MutationHold('local_mutation_binding_invalid')
    return gym


def write_sidecar(path, updates):
    """Fenced checked merge; unreadable existing sidecars never become empty."""
    path = Path(path).absolute()
    gym = gym_for_asset(path)
    cfg = configured(gym, path.parent)
    cfg.asset_path(path)
    sidecar = path.with_suffix('.json')
    def apply(conn):
        old = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        if not isinstance(old, dict):
            raise MutationHold('local_mutation_sidecar_invalid')
        payload = dict(old)
        payload.update(updates)
        atomic_write_json(sidecar, payload)
        return payload
    authority = MutationAuthority.from_environment()
    try:
        return run(cfg, authority, 'dam_sidecar', {'asset': path.name, 'updates': updates}, apply)
    finally:
        authority.close()
