"""Staged, default-OFF atomic Drive consumption. No callers are wired yet.

Remote apply and durable PG receipt share one transaction. SQLite remembers the
exact attempt before RPC, and an unknown apply outcome only permits readback.
No PG calls run under the canonical local inventory flock. This seam neither
certifies global history nor clears selector/visual/publishing safety gates.
Denial, hide and swap permanently consume media. Exceptional never-landed
release is unavailable until the writer protocol can prove that condition.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .local_inventory_mutation import MutationAuthority, MutationHold, canonical_json

APPLY_RPC = 'fixer_remote_drive_use_apply_20261008'
RECEIPT_RPC = 'fixer_remote_drive_use_receipt_20261008'
_KEYS = {'use_id', 'gym_id', 'epoch_id', 'asset_id', 'source_id', 'content_hash',
         'post_date', 'asset_before', 'source_before'}


def enabled():
    return os.environ.get('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', '').lower() == 'true'


def validate(request):
    try:
        if type(request) is not dict or set(request) != _KEYS:
            raise ValueError
        for key in ('use_id', 'epoch_id'):
            if str(uuid.UUID(request[key])) != request[key]:
                raise ValueError
        gym = request['gym_id']
        if (not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', gym)
                or gym.endswith(('_ig', '_fb', '_gbp'))):
            raise ValueError
        if date.fromisoformat(request['post_date']).isoformat() != request['post_date']:
            raise ValueError
        for key in ('asset_id', 'source_id', 'content_hash'):
            if (not isinstance(request[key], str) or not request[key].strip()
                    or len(request[key]) > 512):
                raise ValueError
        asset, source = request['asset_before'], request['source_before']
        if type(asset) is not dict or type(source) is not dict:
            raise ValueError
        for row in (asset, source):
            if (row.get('gym_id') != gym or type(row.get('drive_use_version')) is not int
                    or row['drive_use_version'] < 1):
                raise ValueError
        if (not request['asset_id'] or not request['source_id'] or not request['content_hash']
                or asset.get('id') != request['asset_id']
                or asset.get('source_id') != request['source_id']
                or asset.get('content_hash') != request['content_hash']
                or source.get('id') != request['source_id']
                or source.get('kind') != 'gym_drive' or source.get('active') is not True
                or type(asset.get('used_count')) is not int or asset['used_count'] != 0
                or asset.get('last_used_at') is not None
                or asset.get('eligible') is not True or asset.get('excluded_by_coach') is not False):
            raise ValueError
        canonical_json(request)
    except Exception:
        raise MutationHold('remote_drive_request_invalid') from None


def request_for(asset, source, *, gym_id, epoch_id, post_date, use_id):
    """Caller supplies stable attempt UUID; exact complete DB rows required."""
    request = dict(use_id=use_id, gym_id=gym_id, epoch_id=epoch_id,
                   asset_id=asset.get('id'), source_id=source.get('id'),
                   content_hash=asset.get('content_hash'), post_date=post_date,
                   asset_before=asset, source_before=source)
    validate(request)
    # Freeze independently of mutable caller snapshots.
    return json.loads(canonical_json(request))


class DriveUseAuthority(MutationAuthority):
    """Same dedicated isolated inventory-mutator login; no service fallback."""
    def apply_use(self, request):
        return self._rpc(APPLY_RPC, (canonical_json(request),))

    def use_receipt(self, request):
        return self._rpc(RECEIPT_RPC, (canonical_json(request),))


def _verify(receipt, request):
    if (type(receipt) is not dict or set(receipt) != {'state', 'request', 'asset_after', 'source_after'}
            or receipt.get('state') != 'applied' or receipt.get('request') != request
            or receipt.get('source_after') != request['source_before']):
        raise MutationHold('remote_drive_receipt_invalid')
    after = receipt.get('asset_after')
    before = request['asset_before']
    if type(after) is not dict or set(after) != set(before):
        raise MutationHold('remote_drive_receipt_invalid')
    stable = set(before) - {'used_count', 'last_used_at', 'drive_use_version'}
    if (any(after[k] != before[k] for k in stable)
            or type(after.get('used_count')) is not int or after['used_count'] != 1
            or not isinstance(after.get('last_used_at'), str) or not after['last_used_at']
            or type(after.get('drive_use_version')) is not int
            or after['drive_use_version'] <= before['drive_use_version']):
        raise MutationHold('remote_drive_receipt_invalid')


def _outside_local_lock(config):
    config.validate()
    config.lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config.lock_directory / (config.gym_id + '.lock')
    if path.is_symlink():
        raise MutationHold('remote_drive_local_lock_unavailable')
    try:
        with path.open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError:
        raise MutationHold('remote_drive_local_lock_held') from None


@contextmanager
def _db(path):
    connection = sqlite3.connect(Path(path).as_uri() + '?mode=rw', uri=True, timeout=30)
    try:
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('CREATE TABLE IF NOT EXISTS remote_drive_use_attempt '
                           '(use_id TEXT PRIMARY KEY, request TEXT NOT NULL, '
                           'state TEXT NOT NULL, receipt TEXT)')
        with connection:
            yield connection
    finally:
        connection.close()


def apply(config, authority, request):
    """Remember immutable attempt before remote apply; uncertain retry reads only.

    This stores receipt bookkeeping, not a served/caption/calendar stage record.
    Keep global visual-history and local reservation checks in their owning lanes.
    """
    if not enabled():
        raise MutationHold('remote_drive_cas_disabled')
    validate(request)
    if request['gym_id'] != config.gym_id or request['epoch_id'] != config.epoch_id:
        raise MutationHold('remote_drive_config_binding_invalid')
    _outside_local_lock(config)
    serialized = canonical_json(request)
    with _db(config.sqlite_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT request,state,receipt FROM remote_drive_use_attempt WHERE use_id=?',
                                (request['use_id'],)).fetchone()
        if existing:
            if existing[0] != serialized:
                raise MutationHold('remote_drive_attempt_conflict')
            if existing[1] == 'confirmed':
                receipt = json.loads(existing[2])
                _verify(receipt, request)
                return receipt
        else:
            # An unresolved prior outcome holds the entire tenant lane. A new
            # UUID must never bypass the exact-attempt readback requirement.
            for (pending,) in conn.execute(
                    'SELECT request FROM remote_drive_use_attempt WHERE state!=?', ('confirmed',)):
                try:
                    pending_gym = json.loads(pending)['gym_id']
                except Exception:
                    raise MutationHold('remote_drive_local_journal_invalid') from None
                if pending_gym == config.gym_id:
                    raise MutationHold('remote_drive_reconciliation_required')
            conn.execute('INSERT INTO remote_drive_use_attempt VALUES (?, ?, ?, NULL)',
                         (request['use_id'], serialized, 'unknown'))
        # The unknown state is committed BEFORE any possible remote outcome.
    _outside_local_lock(config)
    try:
        receipt = authority.use_receipt(request) if existing else authority.apply_use(request)
        _verify(receipt, request)
    except Exception:
        raise MutationHold('remote_drive_outcome_unknown') from None
    with _db(config.sqlite_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        changed = conn.execute('UPDATE remote_drive_use_attempt SET state=?,receipt=? '
                               'WHERE use_id=? AND request=? AND state=?',
                               ('confirmed', canonical_json(receipt), request['use_id'], serialized, 'unknown'))
        if changed.rowcount != 1:
            # A parallel exact readback may have already confirmed this receipt.
            row = conn.execute('SELECT state,receipt FROM remote_drive_use_attempt WHERE use_id=? AND request=?',
                               (request['use_id'], serialized)).fetchone()
            if row != ('confirmed', canonical_json(receipt)):
                raise MutationHold('remote_drive_local_receipt_conflict')
    return receipt


def release_never_landed(*args, **kwargs):
    """No counter restoration until an accepted durable never-landed protocol."""
    raise MutationHold('remote_drive_never_landed_release_unavailable')
