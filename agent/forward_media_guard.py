"""Default-OFF fleet byte authority and isolated trusted runtime attester.

Only the attester owns the narrow dedicated DB credentials. Intake/publisher
must not receive them or submit caller hashes/render lineage. The attester
reads persisted exact objects and, for transformed renditions, performs a
controlled render itself and compares its output with the hosted object.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid

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
    from . import visual_writer_prepare as prepare
    if not prepare._own_media_url(url):
        raise ForwardMediaVerificationHold('object is outside approved media host')
    try:
        data = (reader or prepare._bytes_for_url)(url)
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


def generated_snapshot(persistence, calendar_row_id):
    """Read owner DB facts before generation; caller ends this read transaction.

    Inventory covers every same-gym asset/source; history covers the existing
    sealed fleet census and permanent generated reservations. This lookup never
    grants eligibility and retains no graph lock across provider/storage work.
    """
    from .forward_media_owner import ForwardMediaOwnerPersistence
    if type(persistence) is not ForwardMediaOwnerPersistence:
        raise ForwardMediaVerificationHold('dedicated generated owner required')
    persistence._assert_owner_identity()
    with persistence._conn.cursor() as cur:
        cur.execute('select public.fixer_generated_snapshot_20261007(%s)',
                    (_uuid(calendar_row_id),))
        return cur.fetchone()[0]


def reserve_generated(persistence, calendar_row_id, candidate, trusted_snapshot, *,
                      history_visuals, read_bytes=None):
    """Existing dedicated owner prepares an Astra original atomically.

    The configured owner must reload its verified copy/palette/depletion facts
    after generation. ``trusted_snapshot`` is that fresh owner result, never a
    publisher request or the producer's old generation snapshot. Remote bytes
    finish before the final DB transaction. SQL rechecks DB copy/inventory/history
    and binds the original to one gym, content date and logical sibling group.
    History visuals are exact-byte pHashes independently read by this owner.
    Missing history evidence holds; no human coach review is introduced.

    Caller owns final COMMIT and its uncertain-outcome reconciliation. This
    function does not commit, send, approve, or clear an existing calendar hold.
    """
    from .forward_media_owner import ForwardMediaOwnerPersistence
    from . import forward_media_prepare as prepare, visual_scene
    if type(persistence) is not ForwardMediaOwnerPersistence:
        raise ForwardMediaVerificationHold('dedicated generated owner required')
    if persistence._conn.autocommit is not False:
        raise ForwardMediaVerificationHold('generated owner requires transaction')
    # Never discard another owner operation when ending read-only remote prep.
    info = getattr(persistence._conn, 'info', None)
    if info is not None and int(info.transaction_status) != 0:
        raise ForwardMediaVerificationHold('generated remote preparation requires idle owner connection')
    persistence._assert_owner_identity()
    if (not isinstance(candidate, dict) or not isinstance(trusted_snapshot, dict)
            or candidate.get('schema_version') not in (1, 2)
            or candidate.get('source_type') != 'generated_astra_infographic'
            or candidate.get('provider') != 'astra'
            or candidate.get('model') != 'gpt-6-astra'
            or trusted_snapshot.get('photo_inventory_complete') is not True
            or trusted_snapshot.get('eligible_photo_count') != 0
            or trusted_snapshot.get('history_complete') is not True
            or trusted_snapshot.get('palette_verified') is not True
            or trusted_snapshot.get('copy_verified') is not True):
        raise ForwardMediaVerificationHold('fresh verified generated owner facts required')
    import re
    delegated = candidate['schema_version'] == 2
    approved_source_revision = trusted_snapshot.get('approved_source_revision')
    if delegated:
        from . import generated_infographic_preparation as prep, generated_infographic_runtime as runtime
        prep.validate_candidate(candidate)
        if (candidate['authority_pins'] != trusted_snapshot.get('authority_pins')
                or candidate['copy_derivation_receipt'] != trusted_snapshot.get('copy_derivation_receipt')
                or trusted_snapshot.get('copy_approved') is not False):
            raise ForwardMediaVerificationHold('generated canonical bundle pins changed')
        active = runtime._owner_bundle_readback(persistence, candidate['gym_id'])
        authority = runtime.delegated_copy(active, candidate['gym_id'],
            caption=candidate['copy_derivation_receipt']['caption'])
        if (authority['authority_pins'] != candidate['authority_pins']
                or authority['source_revision'] != approved_source_revision
                or candidate['copy_digest'] != prep.digest(authority['copy'])
                or candidate['palette_digest'] != prep.digest(authority['palette'])
                or candidate['palette_revision'] != authority['palette_revision']):
            raise ForwardMediaVerificationHold('generated canonical bundle changed')
    elif (not isinstance(approved_source_revision, str) or not re.fullmatch(
            r'client-source:sha256:[0-9a-f]{64}', approved_source_revision)):
        raise ForwardMediaVerificationHold('verified approved source revision required')
    for key in ('gym_id', 'local_date', 'logical_post_id', 'copy_revision',
                'palette_revision', 'inventory_revision',
                'copy_digest', 'palette_digest'):
        if (not isinstance(candidate.get(key), str) or not candidate[key]
                or candidate[key] != trusted_snapshot.get(key)):
            raise ForwardMediaVerificationHold('generated owner revision changed: ' + key)
    if not isinstance(candidate.get('history_revision'), str) or not candidate['history_revision']:
        raise ForwardMediaVerificationHold('generated historical revision required')
    for key in ('provider_response_id', 'provider_output_id', 'storage_key',
                'review_response_id', 'review_policy_id'):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            raise ForwardMediaVerificationHold('generated provenance unavailable: ' + key)
    _uuid(candidate.get('job_id'))
    # Only this authenticated DB lookup can issue reusable historical proof.
    # Producer/scheduler flags in a supplied history list cannot skip byte reads.
    current = generated_snapshot(persistence, calendar_row_id)
    for key in ('gym_id', 'local_date', 'logical_post_id', 'copy_revision', 'inventory_revision'):
        if current.get(key) != trusted_snapshot.get(key):
            raise ForwardMediaVerificationHold('generated database snapshot changed: ' + key)
    for key in ('account', 'format'):
        if key in trusted_snapshot and current.get(key) != trusted_snapshot[key]:
            raise ForwardMediaVerificationHold('generated database snapshot changed: ' + key)
    if (current.get('photo_inventory_complete') is not True
            or current.get('eligible_photo_count') != 0 or current.get('history_complete') is not True):
        raise ForwardMediaVerificationHold('generated database depletion/history unverified')
    # End read-only identity/snapshot work BEFORE bounded remote object reads.
    persistence._conn.rollback()
    data = _read(candidate.get('original_url'), read_bytes)
    if (candidate.get('original_sha256') != hashlib.sha256(data).hexdigest()
            or candidate.get('original_md5') != hashlib.md5(data).hexdigest()
            or candidate.get('storage_readback_sha256') != candidate.get('original_sha256')
            or candidate.get('original_length') != len(data)
            or candidate.get('original_phash') != visual_scene.scene_fingerprint(data)):
        raise ForwardMediaVerificationHold('generated original byte or perceptual proof changed')
    # Decode, reject animation, and verify exact original dimensions too.
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        if (getattr(im, 'n_frames', 1) != 1 or im.width != candidate.get('width')
                or im.height != candidate.get('height') or im.width * im.height > 40_000_000):
            raise ForwardMediaVerificationHold('generated original dimensions changed')
        im.verify()
    checked_visuals = []
    byte_cache = {candidate['original_url']: data}
    visual_cache = {candidate['original_url']:
                    ('sha256:' + candidate['original_sha256'], candidate['original_phash'])}
    if not isinstance(history_visuals, list):
        raise ForwardMediaVerificationHold('complete historical visual bytes required')
    supplied = {}
    for item in history_visuals:
        if not isinstance(item, dict):
            raise ForwardMediaVerificationHold('historical visual evidence malformed')
        key = (item.get('history_key'), item.get('published_binding_ref'), item.get('visual_url'))
        if key in supplied:
            raise ForwardMediaVerificationHold('historical visual evidence ambiguous')
        supplied[key] = item
    rows = current.get('history', {}).get('rows')
    if not isinstance(rows, list):
        raise ForwardMediaVerificationHold('complete database historical inventory required')
    for item in rows:
        key = (item.get('history_key'), item.get('published_binding_ref'), item.get('visual_url'))
        asserted = supplied.pop(key, None)
        if (item.get('history_proof_ref') and item.get('visual_sha256')
                and visual_scene.normalize_scene(item.get('phash'))):
            # Immutable SQL-issued proof is tied to the exact current history
            # identity, published binding, URL, SHA and pHash. Deleted/missing
            # remote objects do not erase the already observed historical visual.
            if asserted and asserted.get('visual_sha256') != item['visual_sha256']:
                raise ForwardMediaVerificationHold('historical visual bytes changed')
            checked_visuals.append(dict(item))
            continue
        url = item.get('visual_url')
        if url not in byte_cache:
            byte_cache[url] = _read(url, read_bytes)
        prior = byte_cache[url]
        if url not in visual_cache:
            visual_cache[url] = ('sha256:' + hashlib.sha256(prior).hexdigest(),
                                 visual_scene.scene_fingerprint(prior))
        sha, phash = visual_cache[url]
        if ((item.get('visual_sha256') is not None and item['visual_sha256'] != sha)
                or (asserted and asserted.get('visual_sha256') != sha)):
            raise ForwardMediaVerificationHold('historical visual bytes changed')
        if phash is None:
            raise ForwardMediaVerificationHold('historical perceptual evidence unavailable')
        checked_visuals.append({**item, 'visual_sha256': sha, 'phash': phash})
    if supplied:
        raise ForwardMediaVerificationHold('historical visual evidence outside database inventory')
    if _read(candidate['original_url'], read_bytes) != data:
        raise ForwardMediaVerificationHold('generated original changed during observation')
    original = prepare.register_original(
        candidate['gym_id'], 'generated-astra:' + candidate['job_id'],
        candidate['original_url'], data, 'astra-job:' + candidate['job_id'])
    manifest = prepare.build_render_manifest(original, original.source_url, data,
        'same_object', 'generated-astra:' + candidate['job_id'])
    persistence._assert_owner_identity()
    with persistence._conn.cursor() as cur:
        operation = 'fixer_reserve_generated_bundle_20261007' if delegated else 'fixer_reserve_generated_20261007'
        cur.execute('select public.' + operation + '(%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::text)',
                    (_uuid(calendar_row_id), json.dumps(candidate),
                     json.dumps(checked_visuals), json.dumps(manifest.row()), approved_source_revision))
        result = cur.fetchone()[0]
    if not isinstance(result, dict) or result.get('reserved') is not True:
        raise ForwardMediaVerificationHold('atomic generated owner reservation refused')
    return result
