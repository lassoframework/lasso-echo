"""Default-off server capture composition and private durable origin receipts.

One operator-owned journal coordinates one collector across restarts. It is
trusted application state, never a browser-uploadable file or request receipt.
Raw source bytes are private local state and are never emitted in diagnostics.
No scheduler, deployment, publishing, approval, or provider call occurs on import.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .source_brand_ingest import CaptureIngestError


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _fail(code):
    raise CaptureIngestError(code) from None


class CaptureReceiptJournal:
    """Append-only request bindings, retained transport evidence, and readbacks.

    A prepared receipt is written only by trusted collector execution. A capture
    becomes observation authority only after ingest confirms exact DB readback.
    Retries reuse retained bytes and their immutable request/mapping binding.
    All processes must share this operator-owned durable path. Multiple hosts
    with separate local journals are not supported.
    """
    def __init__(self, path):
        self.path = Path(path)
        parent = self.path.parent
        if (not parent.is_dir() or parent.is_symlink() or self.path.is_symlink()
                or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077):
            _fail('private_capture_journal_required')
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            _fail('private_capture_journal_required')
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts ('
                'request_key TEXT PRIMARY KEY,binding TEXT NOT NULL,metadata TEXT,'
                'raw BLOB,proof TEXT,stored TEXT)')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _key(request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            _fail('durable_capture_request_required')
        return hashlib.sha256(request_id.encode()).hexdigest()

    def claim(self, request_id, binding):
        key = self._key(request_id)
        binding = _json(binding)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT binding,metadata,raw,proof FROM receipts '
                             'WHERE request_key=?', (key,)).fetchone()
            if row is None:
                db.execute('INSERT INTO receipts(request_key,binding) VALUES(?,?)', (key, binding))
                return None
            if row[0] != binding:
                _fail('capture_request_binding_changed')
            if row[1] is None:
                return None
            return json.loads(row[1]), bytes(row[2]), json.loads(row[3])

    def prepare(self, request_id, metadata, raw, proof):
        key = self._key(request_id)
        if type(raw) is not bytes or not 1 <= len(raw) <= 2_000_000:
            _fail('incomplete_or_oversize_bytes')
        frozen = (_json(metadata), raw, _json(proof))
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT metadata,raw,proof FROM receipts WHERE request_key=?', (key,)).fetchone()
            if row is None:
                _fail('capture_request_not_claimed')
            if row[0] is not None and row != frozen:
                _fail('capture_receipt_conflict')
            if row[0] is None:
                db.execute('UPDATE receipts SET metadata=?,raw=?,proof=? WHERE request_key=?', (*frozen, key))

    def confirm(self, request_id, stored):
        key = self._key(request_id)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT metadata,raw,stored FROM receipts WHERE request_key=?', (key,)).fetchone()
            if row is None or row[0] is None:
                _fail('capture_request_not_prepared')
            metadata = json.loads(row[0])
            if (set(stored) != {'id', 'gym_id', 'echo_account_key', 'bytes_sha256'}
                    or stored['gym_id'] != metadata['gym_id']
                    or stored['echo_account_key'] != metadata['echo_account_key']
                    or stored['bytes_sha256'] != hashlib.sha256(row[1]).hexdigest()):
                _fail('capture_receipt_readback_mismatch')
            frozen = _json(stored)
            if row[2] is not None and row[2] != frozen:
                _fail('capture_receipt_conflict')
            db.execute('UPDATE receipts SET stored=? WHERE request_key=?', (frozen, key))

    def authenticate_capture(self, capture, raw_bytes, mapping):
        if type(raw_bytes) is not bytes or not isinstance(capture, dict):
            return False
        with self._connect() as db:
            rows = db.execute('SELECT metadata,raw,stored FROM receipts WHERE stored IS NOT NULL').fetchall()
        for metadata, raw, stored in rows:
            receipt = json.loads(stored)
            if receipt['id'] != capture.get('id'):
                continue
            source = json.loads(metadata)
            if (bytes(raw) != raw_bytes or capture.get('raw_bytes') != '\\x' + raw_bytes.hex()
                    or capture.get('bytes_sha256') != receipt['bytes_sha256']
                    or any(capture.get(k) != v for k, v in source.items())
                    or any(source.get(k) != getattr(mapping, k) for k in
                           ('gym_id', 'echo_account_key', 'mapping_revision', 'mapping_evidence'))):
                return False
            return True
        return False


class SourceBrandCaptureRunner:
    """Bounded collection of every exact approved location; no owner inputs.

    Construct in dedicated server startup using reviewed mapping authority and
    a persistent private journal directory. Request IDs are stable orchestration
    IDs; their hashes only enter receipts. A request cannot change its mapping.
    """
    def __init__(self, *, collector, environ=None):
        self.collector = collector
        self.environ = os.environ if environ is None else environ

    def capture(self, gym_id, request_id):
        if self.environ.get('ECHO_SOURCE_CAPTURE_RUNNER_ENABLED') != 'true':
            _fail('source_capture_runner_disabled')
        if self.collector._journal is None:
            _fail('private_capture_journal_required')
        mapping = self.collector._resolve(gym_id)
        status = mapping.mapping_evidence.get('provider_instagram', {})
        if status.get('source') != 'zernio_authenticated_accounts':
            _fail('authenticated_social_status_required')
        CaptureReceiptJournal._key(request_id)
        self.collector._journal.claim(request_id, {'purpose': 'source_collection',
                                                   'mapping': mapping.__dict__})
        captures = []
        for url in mapping.website_response_urls:
            # An explicitly approved exact URL ending .css is a palette asset,
            # not fact evidence. Classification is of the approved URL itself;
            # no discovery and no new URL is ever introduced here.
            kind = ('website_asset'
                    if urlsplit(url).path.lower().endswith('.css') else 'website')
            scoped = hashlib.sha256((request_id + ':website:' + url).encode()).hexdigest()
            captures.append(self.collector.collect_website(gym_id, url, source_kind=kind,
                                                           request_id=scoped))
        if mapping.social_locators:
            captures.append(self.collector.collect_social(gym_id, request_id=hashlib.sha256((request_id + ':instagram').encode()).hexdigest()))
        if self.collector._resolve(gym_id) != mapping:
            _fail('mapping_changed_during_collection')
        return {'gym_id': gym_id, 'echo_account_key': mapping.echo_account_key,
                'mapping_revision': mapping.mapping_revision,
                'source_policy': 'website_and_instagram' if mapping.social_locators else 'website_only',
                'captures': captures}


def build_capture_runner(*, approved_mappings=(), environ=None, http=None,
                         identity_http=None, apify_client=None):
    """No journal or I/O is initialized until all default-off gates pass."""
    from .source_brand_collector import build_collector
    from .apify_run_capture import SQLiteStartJournal
    env = os.environ if environ is None else environ
    if (env.get('ECHO_SOURCE_CAPTURE_RUNNER_ENABLED') != 'true'
            or env.get('ECHO_SOURCE_COLLECTOR_ENABLED') != 'true'
            or env.get('ECHO_SOURCE_CAPTURE_INGEST_ENABLED') != 'true'):
        _fail('source_capture_runner_disabled')
    directory = Path(env.get('ECHO_SOURCE_CAPTURE_JOURNAL_DIR', ''))
    if not env.get('ECHO_SOURCE_CAPTURE_JOURNAL_DIR') or not directory.is_absolute():
        _fail('private_capture_journal_required')
    receipts = CaptureReceiptJournal(directory / 'source-origin.sqlite3')
    starts = SQLiteStartJournal(directory / 'apify-starts.sqlite3')
    collector = build_collector(approved_mappings=approved_mappings, environ=env,
        http=http, identity_http=identity_http, receipt_journal=receipts,
        apify_journal=starts, apify_client=apify_client)
    return SourceBrandCaptureRunner(collector=collector, environ=env)
