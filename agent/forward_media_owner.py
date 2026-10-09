"""Owner-only transactional persistence adapter for the DRAFT forward media claim authority.

Companion to migrations/DRAFT_fixer_forward_media_claim_20261006.sql and
agent/forward_media_prepare.py. The draft's direct preparation protocol is:

    BEGIN; insert verified original registry tuple; insert clearance using the
    exact tuple and independently audited fleet historical evidence; insert
    versioned render manifest; COMMIT.

This adapter performs ONLY that owner persistence step. It accepts already
prepared OriginalRegistration + HistoryClearance + RenderManifest tuples,
re-validates their cross-binding, re-reads every referenced object through a
trusted configured reader and requires the immutable bytes to match the tuple
exactly, then inserts registry -> clearance -> manifest in one BEGIN/COMMIT.

Fail-closed guarantees:

- The connection must be opened from an explicitly supplied dedicated owner DSN
  (or the FORWARD_MEDIA_OWNER_DSN environment variable). There is NO generic
  service-role or publisher fallback DSN; a missing DSN is an error.
- ``SELECT current_user`` must equal the expected owner identity; anything else
  (anon/authenticated/service_role/attester) aborts before any write.
- The exact owner environment allowlist must be satisfied; any unknown name,
  even empty, aborts before any write.
- Existing rows are re-read and compared for EXACT equality (idempotent replay
  of an identical persisted tuple); distinct manifests may extend an exact
  registry/clearance pair. Any mismatch, missing registry/clearance sibling or
  uncertain commit outcome fails closed: the transaction is rolled back and an
  OwnerPersistenceError/UncertainCommitError is raised. The caller must resolve
  the conflict manually; nothing is overwritten or repaired automatically.

This module never grants clearance (that decision is an input), never writes to
production state beyond the draft's owner tables, and never activates anything.
"""
import json
import os
import re
from .forward_media_lane import unknown_environment_names, read_public_object
from agent.forward_media_prepare import (
    HistoryClearance,
    OriginalRegistration,
    PreparationError,
    RenderManifest,
    fingerprint_bytes,
    manifest_digest,
    _manifest_payload,
)

MAX_OBJECT_LENGTH = 134217728
DB_DEADLINE_OPTIONS = '-c lock_timeout=5000 -c statement_timeout=20000'

# Tables created by the DRAFT migration. Owner-only: no grant exists for
# anon/authenticated/service_role/attester on these three tables.
REGISTRY_TABLE = 'public.fixer_forward_media_original_registry_20261006'
CLEARANCE_TABLE = 'public.fixer_forward_media_history_clearance_20261006'
MANIFEST_TABLE = 'public.fixer_forward_media_render_manifest_20261006'

OWNER_DSN_ENV = 'FORWARD_MEDIA_OWNER_DSN'

def forbidden_credential_names(environ):
    """Compatibility name: every unknown owner-lane name is forbidden."""
    return unknown_environment_names(environ, 'owner')


_URL_RE = re.compile(r'^https://[^\s]+$')
_MD5_RE = re.compile(r'^md5:[0-9a-f]{32}$')
_SHA256_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_OPS = ('same_object', 'render', 'reburn', 'rehost')
_DECISIONS = ('cleared_unused', 'hold_uncertain', 'hold_used')

INSERT_REGISTRY = (
    f'insert into {REGISTRY_TABLE} (tenant_id, source_asset_id, source_url, '
    'source_fingerprint, source_length, registry_evidence_ref) '
    'values (%(tenant_id)s, %(source_asset_id)s, %(source_url)s, '
    '%(source_fingerprint)s, %(source_length)s, %(registry_evidence_ref)s)')
INSERT_CLEARANCE = (
    f'insert into {CLEARANCE_TABLE} (tenant_id, source_asset_id, source_url, '
    'source_fingerprint, source_length, registry_evidence_ref, decision, '
    'history_evidence_ref) values (%(tenant_id)s, %(source_asset_id)s, '
    '%(source_url)s, %(source_fingerprint)s, %(source_length)s, '
    '%(registry_evidence_ref)s, %(decision)s, %(history_evidence_ref)s)')
INSERT_MANIFEST = (
    f'insert into {MANIFEST_TABLE} (manifest_digest, tenant_id, source_asset_id, '
    'image_url, image_fingerprint, image_length, thumbnail_url, '
    'thumbnail_fingerprint, thumbnail_length, operation, render_recipe, '
    'render_evidence_ref) values (%(manifest_digest)s, %(tenant_id)s, '
    '%(source_asset_id)s, %(image_url)s, %(image_fingerprint)s, %(image_length)s, '
    '%(thumbnail_url)s, %(thumbnail_fingerprint)s, %(thumbnail_length)s, '
    '%(operation)s, %(render_recipe)s::jsonb, %(render_evidence_ref)s)')
SELECT_REGISTRY = (
    f'select tenant_id, source_asset_id, source_url, source_fingerprint, '
    f'source_length, registry_evidence_ref from {REGISTRY_TABLE} '
    'where tenant_id=%(tenant_id)s and source_asset_id=%(source_asset_id)s')
SELECT_CLEARANCE = (
    f'select tenant_id, source_asset_id, source_url, source_fingerprint, '
    f'source_length, registry_evidence_ref, decision, history_evidence_ref '
    f'from {CLEARANCE_TABLE} where tenant_id=%(tenant_id)s '
    'and source_asset_id=%(source_asset_id)s')
SELECT_MANIFEST = (
    f'select manifest_digest, tenant_id, source_asset_id, image_url, '
    f'image_fingerprint, image_length, thumbnail_url, thumbnail_fingerprint, '
    f'thumbnail_length, operation, render_recipe, render_evidence_ref '
    f'from {MANIFEST_TABLE} where manifest_digest=%(manifest_digest)s')
CURRENT_USER = 'select current_user'


class OwnerPersistenceError(RuntimeError):
    """Fail-closed persistence failure; no automatic repair or clearance."""


class UncertainCommitError(OwnerPersistenceError):
    """COMMIT outcome unknown; the caller must verify before any retry."""


class EnvironmentGuardError(OwnerPersistenceError):
    """Publisher/service credentials present, or no dedicated owner DSN."""


def check_environment(environ=None, *, lane='owner'):
    """Require the exact owner environment contract and dedicated DSN/role."""
    environ = os.environ if environ is None else environ
    if lane not in ('owner', 'generated_owner'):
        raise EnvironmentGuardError('unknown owner environment lane')
    offenders = unknown_environment_names(environ, lane)
    if offenders:
        raise EnvironmentGuardError(
            'unrecognized owner environment names; refusing '
            f'owner write: {offenders}')
    dsn = environ.get(OWNER_DSN_ENV)
    if not dsn or not dsn.strip():
        raise EnvironmentGuardError(
            f'dedicated owner DSN missing (set {OWNER_DSN_ENV}); no generic '
            'service-role or publisher fallback is provisioned')
    role = environ.get('FORWARD_MEDIA_OWNER_ROLE', '')
    if (not role or role != role.strip() or role in (
            'service_role', 'anon', 'authenticated',
            'fixer_forward_media_attester_20261006')):
        raise EnvironmentGuardError('dedicated owner role missing or forbidden')


class ObjectReader:
    """Trusted configured reader interface: returns exact immutable object bytes.

    A concrete implementation is supplied by the caller (owner infrastructure).
    The adapter only trusts THIS reader; it never fetches URLs itself and never
    guesses bytes from URLs, asset IDs or derivatives.
    """

    def read(self, url):  # pragma: no cover - interface
        raise NotImplementedError


class HostedObjectReader(ObjectReader):
    """Production bounded reader for exact objects on Echo's configured host."""

    def read(self, url):
        try:
            return read_public_object(url, max_bytes=MAX_OBJECT_LENGTH)
        except Exception:
            raise OwnerPersistenceError('owner exact public object read unavailable') from None


def _check(condition, message):
    if not condition:
        raise OwnerPersistenceError(message)


def _validate_and_bind(original, clearance, manifest):
    """Validate tuple shape and exact cross-binding of the three tuples."""
    if not isinstance(original, OriginalRegistration):
        raise OwnerPersistenceError('original must be an OriginalRegistration')
    if not isinstance(clearance, HistoryClearance):
        raise OwnerPersistenceError('clearance must be a HistoryClearance')
    if not isinstance(manifest, RenderManifest):
        raise OwnerPersistenceError('manifest must be a RenderManifest')
    for value, name in ((original.tenant_id, 'tenant_id'),
                        (original.source_asset_id, 'source_asset_id')):
        _check(isinstance(value, str) and value == value.strip() and value,
               f'original {name} must be a non-blank trimmed token')
    for value, name in ((original.source_url, 'source_url'),
                        (manifest.image_url, 'image_url'),
                        (manifest.thumbnail_url, 'thumbnail_url')):
        if value is not None:
            _check(isinstance(value, str) and _URL_RE.match(value),
                   f'{name} must be an https URL without whitespace')
    for value, name in ((original.source_fingerprint, 'source_fingerprint'),
                        (manifest.image_fingerprint, 'image_fingerprint'),
                        (manifest.thumbnail_fingerprint, 'thumbnail_fingerprint')):
        if value is not None:
            _check(isinstance(value, str) and _MD5_RE.match(value),
                   f'{name} must be md5:<32 lowercase hex>')
    _check(isinstance(manifest.manifest_digest, str)
           and _SHA256_RE.match(manifest.manifest_digest),
           'manifest_digest must be sha256:<64 lowercase hex>')
    _check(isinstance(original.source_length, int)
           and 0 < original.source_length <= MAX_OBJECT_LENGTH,
           'source_length violates draft bounds')
    for value, name in ((manifest.image_length, 'image_length'),
                        (manifest.thumbnail_length, 'thumbnail_length')):
        if value is not None:
            _check(isinstance(value, int) and 0 < value <= MAX_OBJECT_LENGTH,
                   f'{name} violates draft bounds')
    _check(manifest.operation in _OPS, 'manifest operation must be a controlled operation')
    _check(clearance.decision in _DECISIONS, 'clearance decision must be a controlled decision')
    _check((manifest.thumbnail_url is None) == (manifest.thumbnail_fingerprint is None)
           and (manifest.thumbnail_url is None) == (manifest.thumbnail_length is None),
           'thumbnail fields must be all-or-nothing')
    # Exact tuple cross-binding: clearance must carry the registry tuple verbatim.
    for field in ('tenant_id', 'source_asset_id', 'source_url', 'source_fingerprint',
                  'source_length', 'registry_evidence_ref'):
        _check(getattr(clearance, field) == getattr(original, field),
               f'clearance {field} differs from original registry tuple')
    # Manifest must bind to the same registered original.
    _check(manifest.tenant_id == original.tenant_id
           and manifest.source_asset_id == original.source_asset_id,
           'manifest is not bound to the original registry tuple')
    # Immutable manifest bytes: digest must match the canonical payload exactly.
    payload = _manifest_payload(manifest.tenant_id, manifest.source_asset_id,
                                manifest.image_url, manifest.image_fingerprint,
                                manifest.image_length, manifest.thumbnail_url,
                                manifest.thumbnail_fingerprint, manifest.thumbnail_length,
                                manifest.operation, manifest.render_recipe,
                                manifest.render_evidence_ref)
    _check(manifest_digest(payload) == manifest.manifest_digest,
           'manifest digest does not match manifest fields; refusing mutable manifest')


def _verify_reader_bytes(reader, original, manifest):
    """Re-read every referenced object via the trusted reader; exact bytes only."""
    if not isinstance(reader, ObjectReader):
        raise OwnerPersistenceError('a trusted configured ObjectReader is required')

    def expect(url, fingerprint, length, label):
        data = reader.read(url)
        fp, size = fingerprint_bytes(data)
        _check(fp == fingerprint and size == length,
               f'trusted reader bytes do not match the prepared {label} tuple; '
               'immutable object bytes may not be rebound')

    expect(original.source_url, original.source_fingerprint, original.source_length,
           'original registry')
    expect(manifest.image_url, manifest.image_fingerprint, manifest.image_length,
           'render manifest image')
    if manifest.thumbnail_url is not None:
        expect(manifest.thumbnail_url, manifest.thumbnail_fingerprint,
               manifest.thumbnail_length, 'render manifest thumbnail')


class ForwardMediaOwnerPersistence:
    """Owner-only persistence adapter over a dedicated owner connection.

    ``connection`` must be a DB-API style connection (psycopg compatible) opened
    from the dedicated owner DSN, with dict-param ``execute``. ``expected_owner``
    is the exact current_user identity permitted to persist (e.g. the table
    owner); any other role fails closed before any write.
    """

    def __init__(self, connection, expected_owner, reader, *, environment_lane='owner'):
        if environment_lane not in ('owner', 'generated_owner'):
            raise EnvironmentGuardError('unknown owner environment lane')
        self._environment_lane = environment_lane
        self._conn = connection
        self._expected_owner = expected_owner
        self._reader = reader

    @classmethod
    def connect_from_environment(cls, *, reader=None, environment_lane='owner'):
        """Only production construction path; never fall back to publisher DB."""
        if environment_lane == 'owner':
            check_environment()
        else:
            check_environment(lane=environment_lane)
        expected = os.environ.get('FORWARD_MEDIA_OWNER_ROLE', '').strip()
        if not expected or expected in ('service_role', 'anon', 'authenticated',
                                        'fixer_forward_media_attester_20261006'):
            raise EnvironmentGuardError('dedicated owner role missing or forbidden')
        try:
            import psycopg
            conn = psycopg.connect(os.environ[OWNER_DSN_ENV], autocommit=False,
                                   options=DB_DEADLINE_OPTIONS)
        except Exception as exc:
            raise OwnerPersistenceError('dedicated owner database unavailable') from exc
        return cls(conn, expected, reader or HostedObjectReader(),
                   environment_lane=environment_lane)

    def _assert_owner_identity(self):
        if self._environment_lane == 'generated_owner':
            check_environment(lane='generated_owner')
        with self._conn.cursor() as cur:
            cur.execute(CURRENT_USER)
            row = cur.fetchone()
        current = row[0] if row else None
        if current != self._expected_owner:
            raise OwnerPersistenceError(
                f'current_user {current!r} is not the dedicated owner '
                f'{self._expected_owner!r}; refusing to write forward media authority')

    def _fetch(self, query, params):
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def _insert(self, query, params):
        with self._conn.cursor() as cur:
            cur.execute(query, params)

    def persist(self, original, clearance, manifest):
        """Validate, verify bytes, and persist the exact tuple in one transaction.

        Idempotent: existing authority must match exactly. A new render manifest
        may be added to an existing registry/clearance pair; an exact manifest
        replay succeeds as a re-read. Any mismatch fails closed. On any error
        or uncertain commit the transaction is rolled back and nothing is
        reported as persisted.
        """
        try:
            result = self.persist_in_transaction(original, clearance, manifest)
            try:
                self._conn.commit()
            except Exception as exc:
                raise UncertainCommitError(
                    'COMMIT outcome uncertain; verify before any manual retry') from exc
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        return result

    def persist_in_transaction(self, original, clearance, manifest):
        """Stage authority in the caller's transaction; NEVER commit or rollback.

        The caller owns transaction failure handling and must hold its canonical
        row/asset locks through authority, durable outcome, and one final commit.
        A failed SQL statement poisons the transaction: do not convert it to a
        successful hold. This method does not report durable persistence.
        """
        if self._environment_lane == 'owner':
            check_environment()
        else:
            check_environment(lane=self._environment_lane)
        if self._expected_owner != os.environ['FORWARD_MEDIA_OWNER_ROLE']:
            raise EnvironmentGuardError('owner role differs from configured lane')
        _validate_and_bind(original, clearance, manifest)
        _verify_reader_bytes(self._reader, original, manifest)
        if getattr(self._conn, 'autocommit', None) is not False:
            raise OwnerPersistenceError('owner connection must use one transaction')
        # Bound every outer INSERT too: its graph-lock trigger restores its
        # local settings before a unique/FK wait in the INSERT can occur.
        # Caller-owned connections receive the same pre-command protection.
        with self._conn.cursor() as cur:
            cur.execute("set local lock_timeout = '5s'")
            cur.execute("set local statement_timeout = '20s'")
        self._assert_owner_identity()
        registry, clear_row, man_row = original.row(), clearance.row(), manifest.row()
        man_row['render_recipe'] = json.dumps(manifest.render_recipe, sort_keys=True)
        key = {'tenant_id': original.tenant_id, 'source_asset_id': original.source_asset_id}
        existing = {
            'registry': self._fetch(SELECT_REGISTRY, key),
            'clearance': self._fetch(SELECT_CLEARANCE, key),
            'manifest': self._fetch(SELECT_MANIFEST, {'manifest_digest': manifest.manifest_digest}),
        }
        if any(existing.values()):
            self._verify_existing(original, clearance, manifest, existing)
            if not existing['manifest']:
                self._insert(INSERT_MANIFEST, man_row)
        else:
            self._insert(INSERT_REGISTRY, registry)
            self._insert(INSERT_CLEARANCE, clear_row)
            self._insert(INSERT_MANIFEST, man_row)
        return {'registry': registry, 'clearance': clear_row, 'manifest': manifest.row(),
                'replayed': bool(existing['manifest'])}

    def _verify_existing(self, original, clearance, manifest, existing):
        """Idempotent exact re-read: every existing row must match the tuple."""
        registry, clear_row, man_row = original.row(), clearance.row(), manifest.row()

        def compare(rows, expected, label):
            _check(len(rows) == 1,
                   f'{label} exists but is not a single exact row; refusing')
            for field, value in expected.items():
                _check(rows[0].get(field) == value,
                       f'persisted {label} {field} differs from prepared tuple; '
                       'immutable authority may not be overwritten')

        compare(existing['registry'], registry, 'registry')
        compare(existing['clearance'], clear_row, 'clearance')
        if existing['manifest']:
            compare(existing['manifest'], man_row, 'manifest')
