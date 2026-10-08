"""DRAFT isolated-owner census producer. Explicit configuration defaults OFF.

No scheduler, publishing, review, consent or activation writes. Requires the
companion DRAFT_fixer_generated_local_census_producer_20261008.sql. Observation
transactions end before authenticated source reads. Partial evidence is retained
locally with unknown supply represented by None. Newest incomplete observations
are also recorded as incomplete to supersede prior depletion claims; SQL's count
is an observed lower bound and never establishes depletion. Receipts include exact file, durable rotation, connected source,
database inventory, calendar and epoch bindings. Old receipts cannot be imported
or refreshed: every call performs fresh observation and gets a new receipt ID.

Configuration paths are provisioned by the owner, not client request inputs.
The local library must be the canonical gym directory. The rotation DB must be
the owner's verified complete existing SQLite history, including legacy imports;
the producer does not create/migrate a ledger or infer missing history as empty.
Local writers are not fenced by PostgreSQL. Even a clean zero observation is
recorded INCOMPLETE. True complete-zero requires an independently verified
all-writer fence/invalidation through the later generation decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

MAX_ITEMS = 10000
MAX_BYTES = 134217728
MAX_AGE = timedelta(minutes=5)
SNAPSHOT_RPC = 'fixer_generated_local_census_snapshot_20261008'
RECEIPT_RPC = 'fixer_generated_local_census_receipt_20261008'
_ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_MD5 = re.compile(r'[0-9a-f]{32}\Z')
_PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic', '.heif', '.avif', '.tif', '.tiff', '.bmp'}
_FOLDER = 'application/vnd.google-apps.folder'


class CensusHold(RuntimeError):
    """Only static codes; never connector exceptions or credential details."""


@dataclass(frozen=True)
class CensusConfig:
    gym_id: str
    library_path: Path
    rotation_db_path: Path
    receipt_directory: Path
    # A reviewed completeness reference is mandatory: readable SQLite alone
    # cannot prove that prior local planning history was never lost/pruned.
    rotation_history_ref: str = ''
    enabled: bool = False


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return 'sha256:' + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _config(config):
    if (type(config) is not CensusConfig or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', config.gym_id)
            or config.gym_id.endswith(('_ig', '_fb', '_gbp'))):
        raise CensusHold('local_census_configuration_invalid')
    for path in (config.library_path, config.rotation_db_path, config.receipt_directory):
        if not isinstance(path, Path) or not path.is_absolute() or path.resolve() != path:
            raise CensusHold('local_census_configuration_invalid')
    if config.library_path.name != config.gym_id:
        raise CensusHold('local_census_library_binding_invalid')


def _owner(persistence):
    from . import forward_media_owner as owner
    if type(persistence) is not owner.ForwardMediaOwnerPersistence or persistence._conn.autocommit:
        raise CensusHold('local_census_isolated_owner_required')
    try:
        owner.check_environment()
        persistence._assert_owner_identity()
    except Exception:
        raise CensusHold('local_census_isolated_owner_required') from None


def _rpc(persistence, name, params):
    with persistence._conn.cursor() as cur:
        cur.execute('select public.' + name + '(' + ','.join(['%s'] * len(params)) + ')', params)
        row = cur.fetchone()
    if row is None:
        raise CensusHold('local_census_owner_readback_unavailable')
    return row[0]


def _snapshot(persistence, row_id):
    _owner(persistence)
    try:
        return _rpc(persistence, SNAPSHOT_RPC, (row_id,))
    except CensusHold:
        raise
    except Exception:
        raise CensusHold('local_census_owner_snapshot_unavailable') from None


def _validate_snapshot(current, config, row_id):
    if (not isinstance(current, dict) or current.get('calendar_row_id') != row_id
            or current.get('enabled') is not True or not _uuid(current.get('epoch_id'))):
        raise CensusHold('local_census_epoch_unavailable')
    snap = current.get('snapshot')
    if (not isinstance(snap, dict) or snap.get('gym_id') != config.gym_id
            or snap.get('format') != 'feed' or snap.get('account') not in ('instagram', 'facebook')
            or not _uuid(snap.get('logical_post_id')) or not snap.get('local_date') or not snap.get('group_key')
            or any(not _SHA.fullmatch(str(current.get(k, ''))) for k in ('source_revision', 'asset_revision'))
            or not _SHA.fullmatch(str(snap.get('inventory_revision', '')))):
        raise CensusHold('local_census_calendar_binding_invalid')
    if type(snap.get('eligible_photo_count')) is not int or snap['eligible_photo_count'] < 0:
        raise CensusHold('local_census_inventory_or_history_incomplete')
    for kind in ('sources', 'assets'):
        items = current.get(kind)
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise CensusHold('local_census_source_inventory_unavailable')
        ids = set()
        for item in items:
            if (not isinstance(item, dict) or item.get('gym_id') != config.gym_id
                    or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids):
                raise CensusHold('local_census_source_binding_invalid')
            ids.add(item['id'])


def _read_file(path):
    if path.is_symlink() or not path.is_file():
        raise CensusHold('local_census_local_bytes_unavailable')
    before = path.stat()
    if not 0 < before.st_size <= MAX_BYTES:
        raise CensusHold('local_census_local_bytes_unavailable')
    with path.open('rb') as f:
        data = f.read(MAX_BYTES + 1)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(data) != before.st_size:
        raise CensusHold('local_census_local_bytes_changed')
    return data


def _json_file(path, default):
    if not path.exists():
        if path.is_symlink():
            raise CensusHold('local_census_local_metadata_unavailable')
        return default
    try:
        return json.loads(_read_file(path))
    except CensusHold:
        raise
    except Exception:
        raise CensusHold('local_census_local_metadata_unavailable') from None


def _rotation(config):
    if not isinstance(config.rotation_history_ref, str) or not config.rotation_history_ref.strip():
        raise CensusHold('local_census_rotation_history_unproven')
    if not config.rotation_db_path.is_file() or config.rotation_db_path.is_symlink():
        raise CensusHold('local_census_rotation_unavailable')
    try:
        with sqlite3.connect(config.rotation_db_path.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
            rows = conn.execute('select account_key,key,date,content_hash from served '
                                'where account_key in (?,?,?,?) order by id limit ?',
                                (config.gym_id, config.gym_id + '_ig', config.gym_id + '_fb',
                                 config.gym_id + '_gbp', MAX_ITEMS + 1)).fetchall()
    except Exception:
        raise CensusHold('local_census_rotation_unavailable') from None
    relevant = []
    for account, key, day, sha in rows:
        if not isinstance(account, str):
            raise CensusHold('local_census_rotation_incomplete')
        from .rotation import _base_account_key
        if _base_account_key(account) != config.gym_id:
            continue
        if not isinstance(key, str) or not key or not isinstance(day, str) or not day:
            raise CensusHold('local_census_rotation_incomplete')
        if sha not in (None, '') and not re.fullmatch(r'[0-9a-f]{64}', str(sha)):
            raise CensusHold('local_census_rotation_incomplete')
        relevant.append([account, key, day, sha or ''])
        if len(relevant) > MAX_ITEMS:
            raise CensusHold('local_census_rotation_bound_exceeded')
    return relevant


def observe_local(config):
    """Read-only exact local photo/sidecar/rotation observation. No picker cache."""
    rows = _rotation(config)
    if not config.library_path.is_dir() or config.library_path.is_symlink():
        raise CensusHold('local_census_library_unavailable')
    exclusions = _json_file(config.library_path / 'style_exclusions.json', {})
    if not isinstance(exclusions, dict) or not isinstance(exclusions.get('off_style', []), list):
        raise CensusHold('local_census_local_metadata_unavailable')
    files, available = [], 0
    entries = sorted(config.library_path.iterdir())
    if len(entries) > MAX_ITEMS:
        raise CensusHold('local_census_library_bound_exceeded')
    for path in entries:
        if path.name.startswith('._'):
            continue
        if path.is_symlink():
            raise CensusHold('local_census_library_binding_invalid')
        if path.is_dir():
            # A carousel is potential still supply. Unknown child layout cannot
            # disappear behind list_creatives' permissive empty/error behavior.
            if path.name.lower() not in ('reels', 'feedfit'):
                raise CensusHold('local_census_nested_library_unproven')
            continue
        if path.suffix.lower() not in _PHOTO_EXTS:
            continue
        data = _read_file(path)
        sha = hashlib.sha256(data).hexdigest()
        side = _json_file(path.with_suffix('.json'), {})
        if not isinstance(side, dict) or (side.get('dupe_group') and not isinstance(side['dupe_group'], str)):
            raise CensusHold('local_census_local_metadata_unavailable')
        key = side.get('dupe_group') or path.name
        used = any(row[1] == key or row[3] == sha for row in rows)
        from .client_media_sync import is_generated_derivative
        refused = side.get('approved') is False or str(side.get('moderation', '')).strip().lower() == 'rejected'
        excluded = path.name in exclusions.get('off_style', [])
        derivative = is_generated_derivative(path)
        # Pending review is supply waiting for a decision. It must block zero,
        # and this producer never changes coach review or approval state.
        supply = not (used or refused or excluded or derivative)
        if supply:
            try:
                from PIL import Image
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
            except Exception:
                raise CensusHold('local_census_local_bytes_unavailable') from None
            available += 1
        files.append(dict(name=path.name, sha256='sha256:' + sha, metadata=side, used=used, supply=supply))
    return dict(available=available, library_revision=_digest(dict(files=files, exclusions=exclusions)),
                rotation_revision=_digest(dict(rows=rows, completeness_ref=config.rotation_history_ref)),
                binding_revision=_digest(dict(gym_id=config.gym_id, library=str(config.library_path), rotation=str(config.rotation_db_path))),
                files=files, exclusions=exclusions, rotation_rows=rows, rotation_history_ref=config.rotation_history_ref)


class DedicatedDriveInventory:
    """Fresh full paginated recursion through the existing dedicated reader.

    No shared service credentials, tree cache, depth truncation, sync mutations,
    or permissive files=[] coercion. Errors and traversal limits HOLD.
    """
    def __init__(self, drive=None):
        if drive is None:
            from .forward_media_source_verifier import OriginalDriveReader
            drive = OriginalDriveReader()
        self.drive = drive

    def observe(self, source):
        root = source.get('folder_id')
        if source.get('kind') != 'gym_drive' or source.get('active') is not True or source.get('revoked_externally', False) is not False:
            raise CensusHold('local_census_source_unavailable')
        if not isinstance(root, str) or not _ID.fullmatch(root):
            raise CensusHold('local_census_source_binding_invalid')
        service = self.drive._bounded_service()
        root_meta = service.files().get(fileId=root, fields='id,mimeType,trashed', supportsAllDrives=True).execute(num_retries=0)
        if not isinstance(root_meta, dict) or root_meta.get('id') != root or root_meta.get('mimeType') != _FOLDER or root_meta.get('trashed') is not False:
            raise CensusHold('local_census_source_unavailable')
        frontier, seen, files, pages = [root], {root}, [], 0
        while frontier:
            folder, token, tokens = frontier.pop(0), None, set()
            while True:
                pages += 1
                if pages > 100 or len(seen) > 1000:
                    raise CensusHold('local_census_source_bound_exceeded')
                args = dict(q=f"'{folder}' in parents and trashed = false", fields='nextPageToken,incompleteSearch,files(id,name,mimeType,parents,md5Checksum,modifiedTime,trashed)',
                            pageSize=1000, supportsAllDrives=True, includeItemsFromAllDrives=True)
                if token is not None:
                    args['pageToken'] = token
                page = service.files().list(**args).execute(num_retries=0)
                if not isinstance(page, dict) or not isinstance(page.get('files'), list) or page.get('incompleteSearch', False) is not False:
                    raise CensusHold('local_census_source_incomplete')
                for item in page['files']:
                    if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not _ID.fullmatch(item['id'])
                            or item['id'] in seen or item.get('trashed') is not False or item.get('parents') != [folder]
                            or not isinstance(item.get('name'), str) or not isinstance(item.get('mimeType'), str)):
                        raise CensusHold('local_census_source_incomplete')
                    seen.add(item['id'])
                    files.append(item)
                    if len(files) > MAX_ITEMS:
                        raise CensusHold('local_census_source_bound_exceeded')
                    if item['mimeType'] == _FOLDER:
                        frontier.append(item['id'])
                    elif item['mimeType'] == 'application/vnd.google-apps.shortcut':
                        raise CensusHold('local_census_source_shortcut_unproven')
                token = page.get('nextPageToken')
                if token is None:
                    break
                if not isinstance(token, str) or not token or token in tokens:
                    raise CensusHold('local_census_source_incomplete')
                tokens.add(token)
        return sorted(files, key=lambda f: f['id'])


def _sources(current, reader):
    from .gym_media_index import classify
    evidence = []
    for source in current['sources']:
        if source.get('kind') != 'gym_drive' or source.get('active') is not True or source.get('revoked_externally', False) is not False:
            raise CensusHold('local_census_source_unavailable')
        try:
            files = reader.observe(source)
        except CensusHold:
            raise
        except Exception:
            raise CensusHold('local_census_source_unavailable') from None
        if not isinstance(files, list) or len(files) > MAX_ITEMS:
            raise CensusHold('local_census_source_incomplete')
        seen = set()
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in seen:
                raise CensusHold('local_census_source_incomplete')
            seen.add(item['id'])
            if classify(item.get('name'), item.get('mimeType')) != 'photo':
                continue
            md5 = item.get('md5Checksum')
            asset = next((a for a in current['assets'] if a['id'] == item['id'] and a.get('source_id') == source['id']), None)
            if not isinstance(md5, str) or not _MD5.fullmatch(md5) or asset is None or asset.get('content_hash') != md5:
                # A new/changed photo is unindexed supply, never depletion.
                raise CensusHold('local_census_source_inventory_changed')
        evidence.append(dict(source_id=source['id'], binding_revision=_digest(source), inventory_revision=_digest(files), files=files))
    return evidence


def _save(config, receipt):
    config.receipt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config.receipt_directory / (receipt['receipt_id'] + '.json')
    payload = (_canonical(receipt) + '\n').encode()
    # Exclusive immutable evidence; no overwrite or re-import on a later pass.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    if path.read_bytes() != payload:
        raise CensusHold('local_census_evidence_unavailable')
    return str(path)


def run_census(row_id, *, persistence, config, source_reader=None, clock=None):
    """Observe -> end read transaction -> observe -> locked CAS -> readback.

    Returns an immutable local evidence path for complete/partial observations.
    Zero observations ALWAYS record local_complete=false. The helper commits
    only the census authority record. A failed/unknown commit
    is held for reconciliation and never silently retried.
    """
    if type(config) is not CensusConfig or config.enabled is not True:
        return dict(ok=False, held=True, reason='local_census_disabled')
    now = clock or (lambda: datetime.now(timezone.utc))
    receipt = dict(receipt_id=str(uuid.uuid4()), calendar_row_id=row_id, gym_id=config.gym_id,
                   observed_at=now().isoformat(), complete=False, available=None)
    evidence_path = None
    committing = False
    configured = False
    try:
        _config(config)
        configured = True
        if not _uuid(row_id):
            raise CensusHold('local_census_calendar_binding_invalid')
        current = _snapshot(persistence, row_id)
        persistence._conn.rollback()
        _validate_snapshot(current, config, row_id)
        receipt['owner_snapshot_digest'] = _digest(current)
        receipt['epoch_id'] = current['epoch_id']
        receipt['inventory_revision'] = current['snapshot']['inventory_revision']
        reader = source_reader or DedicatedDriveInventory()
        local, sources, issue = None, [], None
        observed_available = current['snapshot']['eligible_photo_count']
        try:
            snap = current['snapshot']
            if (snap.get('photo_inventory_complete') is not True or snap.get('history_complete') is not True
                    or not isinstance(snap.get('history'), dict) or snap['history'].get('scope_complete') is not True
                    or not snap['history'].get('epoch') or not snap.get('history_revision')):
                raise CensusHold('local_census_inventory_or_history_incomplete')
            local = observe_local(config)
            observed_available += local['available']
            sources = _sources(current, reader)
            if sources != _sources(current, reader) or local != observe_local(config):
                raise CensusHold('local_census_observation_changed')
        except CensusHold as exc:
            issue = str(exc)
        except Exception:
            issue = 'local_census_observation_unavailable'
        observed = datetime.fromisoformat(receipt['observed_at'])
        if observed.tzinfo is None or not observed <= now() <= observed + MAX_AGE:
            raise CensusHold('local_census_observation_stale')
        if observed_available == 0 and issue is None:
            issue = 'local_census_all_writer_fence_unavailable'
        receipt.update(local=local, sources=sources, complete=issue is None,
                       available=observed_available if issue is None else None,
                       observed_available=observed_available, reason=issue)
        receipt['evidence_ref'] = 'local-census:' + _digest(receipt)
        evidence_path = _save(config, receipt)
        # No network inside this final DB transaction. The RPC reacquires the
        # established graph/census/row lock order and compares the entire epoch,
        # calendar, source, asset and history envelope under those locks.
        _owner(persistence)
        result = _rpc(persistence, 'fixer_still_inventory_record_20261007',
                      (receipt['receipt_id'], row_id, receipt['inventory_revision'], receipt['complete'], observed_available,
                       receipt['evidence_ref'], receipt['epoch_id'], _canonical(current), receipt['observed_at']))
        if str(result) != receipt['receipt_id']:
            raise CensusHold('local_census_owner_readback_unavailable')
        stored = _rpc(persistence, RECEIPT_RPC, (receipt['receipt_id'], row_id))
        if (not isinstance(stored, dict) or any(str(stored.get(k)) != str(receipt[k]) for k in
                ('receipt_id', 'epoch_id', 'gym_id', 'inventory_revision', 'evidence_ref'))
                or stored.get('local_complete') is not receipt['complete'] or stored.get('local_available') != observed_available
                or datetime.fromisoformat(str(stored.get('observed_at')).replace('Z', '+00:00')) != observed):
            raise CensusHold('local_census_owner_readback_unavailable')
        committing = True
        persistence._conn.commit()
        committing = False
        return dict(ok=issue is None, held=issue is not None, reason=issue,
                    receipt=receipt, evidence_path=evidence_path, recorded=True)
    except Exception as exc:
        reason = ('local_census_commit_uncertain' if committing else
                  str(exc) if isinstance(exc, CensusHold) else 'local_census_unavailable')
        if evidence_path is None and configured:
            receipt.update(complete=False, available=None, reason=reason)
            try:
                evidence_path = _save(config, receipt)
            except Exception:
                reason = 'local_census_evidence_unavailable'
        return dict(ok=False, held=True, reason=reason, receipt=receipt, evidence_path=evidence_path, recorded=False)
    finally:
        if hasattr(persistence, '_conn'):
            try:
                persistence._conn.rollback()
            except Exception:
                pass
