"""Default-OFF fleet byte authority and isolated trusted runtime attester.

Only the attester owns the narrow dedicated DB credentials. Intake/publisher
must not receive them or submit caller hashes/render lineage. The attester
reads persisted exact objects and, for transformed renditions, performs a
controlled render itself and compares its output with the hosted object.
"""
from __future__ import annotations

import hashlib
import os
import uuid

from .forward_media_lane import unknown_environment_names, approved_url, read_public_object

ROLE = 'fixer_forward_media_attester_20261006'
MAX_BYTES = 128 * 1024 * 1024


class ForwardMediaVerificationHold(RuntimeError):
    """No provider send is authorized; evidence/authority is unavailable."""


class ForwardMediaDuplicateHold(ForwardMediaVerificationHold):
    """Verified bytes were consumed by another tenant/content date/group."""


def enabled():
    return os.getenv('AGENT_FORWARD_MEDIA_GUARD', '').lower() in ('1', 'true', 'yes', 'on')


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ForwardMediaVerificationHold('persisted media identity invalid') from exc


def _connect():
    if unknown_environment_names(os.environ, 'attester'):
        raise ForwardMediaVerificationHold('unrecognized attester environment')
    dsn = os.getenv('AGENT_FORWARD_MEDIA_ATTESTER_DSN')
    expected = os.getenv('AGENT_FORWARD_MEDIA_ATTESTER_ROLE')
    if not enabled() or not dsn or expected != ROLE:
        raise ForwardMediaVerificationHold('trusted media attester is not configured')
    try:
        import psycopg
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute('select current_user')
            if cur.fetchone() != (ROLE,):
                conn.close()
                raise ForwardMediaVerificationHold('trusted attester role mismatch')
        return conn
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold('trusted attester database unavailable') from exc


def _read(url, reader):
    if not approved_url(url):
        raise ForwardMediaVerificationHold('object is outside approved media host')
    try:
        data = (reader or read_public_object)(url)
    except Exception as exc:
        raise ForwardMediaVerificationHold('exact object read unavailable') from exc
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ForwardMediaVerificationHold('bounded byte evidence unavailable')
    return data


def attest(calendar_row_id, expected_revision, *, original_verifier=None, controlled_renderer=None,
           connection_factory=None, read_bytes=None):
    """Autonomous trusted attester entry point, run in its isolated credential lane.

    Requests contain only persisted row ID and expected revision. A trusted
    original_verifier must validate actual fetched bytes against the persisted
    original/generated asset registry; a source URL alone is never original
    provenance. It returns literal True, otherwise attestation holds. Renderer is
    configured by that trusted lane, never request data; it is invoked with the
    actual fetched original bytes and persisted snapshot. It returns a dict with
    image_bytes, thumbnail_bytes and operation (render/reburn). Both actual
    hosted objects must equal its output. Rehost of identical bytes needs no
    render claim; same-object URLs never invent ancestry. Fixture injection is
    intended only for isolated tests, not publisher-provided callbacks.
    """
    row_id = _uuid(calendar_row_id)
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ForwardMediaVerificationHold('expected media revision required')
    conn = connection_factory() if connection_factory else _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('select current_user')
            if cur.fetchone() != (ROLE,):
                raise ForwardMediaVerificationHold('trusted attester role mismatch')
            cur.execute('select public.fixer_forward_media_attestation_request_20261006(%s)', (row_id,))
            snapshot = cur.fetchone()[0]
            if not isinstance(snapshot, dict) or snapshot.get('revision') != expected_revision:
                raise ForwardMediaVerificationHold('persisted media revision changed')
            if connection_factory is None:
                # Production never accepts callbacks from a publisher/request.
                # The isolated attester obtains exact-row provenance through
                # its narrow DB RPC using the same authenticated connection.
                if (original_verifier is not None or controlled_renderer is not None
                        or read_bytes is not None):
                    raise ForwardMediaVerificationHold('attester callback override refused')
                from . import forward_media_attester
                original_verifier, controlled_renderer = (
                    forward_media_attester.production_callbacks(
                        conn, row_id, expected_revision=expected_revision))
        # Snapshot and callback capture are read-only. End that transaction
        # before object reads so graph/census locks and a database snapshot are
        # never retained across network work. Final authority below starts a
        # fresh transaction and revalidates revision, provenance and holds.
        conn.rollback()
        urls = [snapshot.get(k) for k in ('source_url', 'image_url', 'thumbnail_url')]
        cache = {}
        for url in urls:
            if url is not None and url not in cache:
                cache[url] = _read(url, read_bytes)
        source, image = cache[urls[0]], cache[urls[1]]
        thumbnail = cache[urls[2]] if urls[2] is not None else None
        if (not callable(original_verifier)
                or original_verifier(dict(snapshot), source) is not True):
            raise ForwardMediaVerificationHold('trusted original asset provenance unavailable')
        if all(url is None or url == urls[0] for url in urls):
            operation = 'same_object'
        elif image == source and (thumbnail is None or thumbnail == source):
            operation = 'rehost'
        else:
            if not callable(controlled_renderer):
                raise ForwardMediaVerificationHold('controlled render ancestry unavailable')
            result = controlled_renderer(source, dict(snapshot))
            if (not isinstance(result, dict)
                    or result.get('operation') not in ('render', 'reburn')
                    or result.get('image_bytes') != image
                    or result.get('thumbnail_bytes') != thumbnail):
                raise ForwardMediaVerificationHold('hosted rendition differs from controlled render')
            operation = result['operation']
        # Recheck exact objects after any controlled render to catch an
        # overwrite during observation. Production object keys must remain
        # immutable/versioned after receipt creation too.
        for url, observed in cache.items():
            if _read(url, read_bytes) != observed:
                raise ForwardMediaVerificationHold('observed media object changed bytes')
        values = []
        for data in (source, image, thumbnail):
            values.extend((('md5:' + hashlib.md5(data).hexdigest(), len(data))
                           if data is not None else (None, None)))
        evidence_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute('select public.fixer_attest_forward_media_20261006('
                        '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (row_id, expected_revision, evidence_id, *values, operation,
                         'trusted-runtime:' + evidence_id))
            if str(cur.fetchone()[0]) != evidence_id:
                raise ForwardMediaVerificationHold('attestation identity mismatch')
        conn.commit()
        return {'evidence_id': evidence_id, 'revision': expected_revision,
                'fingerprints': sorted(set(value for value in values[::2] if value))}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(exc, ForwardMediaVerificationHold):
            raise
        raise ForwardMediaVerificationHold('trusted attestation transaction failed') from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim(store, calendar_row_id, claim_token, evidence_id, expected_revision):
    """Final provider boundary: success requires literal true from atomic RPC.

    Missing evidence, HTTP errors, bad responses and DB exceptions are
    verification holds. The caller must distinguish these from definitive
    duplicate denial and preserve all existing send/ownership controls.
    """
    arguments = dict(zip(('p_calendar_row_id', 'p_claim_token', 'p_evidence_id'),
                         map(_uuid, (calendar_row_id, claim_token, evidence_id))))
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ForwardMediaVerificationHold('expected outgoing media revision required')
    arguments['p_expected_revision'] = expected_revision
    try:
        response = store._client().post(
            store._rest('rpc/fixer_claim_forward_media_20261006'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json=arguments, timeout=30)
        payload = response.json()
        if not 200 <= response.status_code < 300:
            if (isinstance(payload, dict) and payload.get('code') == '23514'
                    and payload.get('message') ==
                    'source or rendition already consumed by another tenant/date/group'):
                raise ForwardMediaDuplicateHold('media bytes were already consumed')
            raise ForwardMediaVerificationHold('atomic forward media authority refused publication')
        if payload is not True:
            raise ForwardMediaVerificationHold('atomic forward media authority refused publication')
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold('atomic forward media authority unavailable') from exc
    return True
