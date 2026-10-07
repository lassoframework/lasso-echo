"""DRAFT isolated owner positive photo preparation, separately gated runtime.

Remote Drive/hosted reads and deterministic replay finish before final locks.
The owner independently verifies Ed25519 and exact candidate/corpus, then SQL
rechecks the authenticated immutable certificate under exclusive graph authority.
Only that RPC can create a positive visual reservation plus the exact immutable
original/clearance/manifest tuple. No publisher uses this adapter.
"""
from dataclasses import dataclass
import hashlib
import json
import uuid

from . import forward_media_prepare as prepare
from .forward_media_attester import replay_still_recipe, validate_still_recipe
from .forward_media_owner import ForwardMediaOwnerPersistence, ObjectReader
from .forward_media_photo_certificate import IndependentPhotoAuditor, PhotoCertificateHold, digest
from .forward_media_source_verifier import verify_source
from .forward_media_thumbnail_candidate import prepare_thumbnail_candidate


@dataclass(frozen=True)
class PreparedOwnerPhoto:
    source: object
    image_bytes: bytes
    manifest: prepare.RenderManifest
    certificate: object
    thumbnail_bytes: bytes | None = None


class _FrozenBytes(ObjectReader):
    def __init__(self, source, image_url, image_bytes, thumbnail_url=None, thumbnail_bytes=None):
        self.values = {source.original.source_url: source.source_bytes, image_url: image_bytes}
        if thumbnail_url is not None:
            if thumbnail_url in self.values and self.values[thumbnail_url] != thumbnail_bytes:
                raise PhotoCertificateHold('certified_owner_thumbnail_alias_mismatch')
            self.values[thumbnail_url] = thumbnail_bytes

    def read(self, url):
        if url not in self.values:
            raise PhotoCertificateHold('certified_owner_bytes_unavailable')
        return self.values[url]


def reconcile_owner_photo(persistence, audit_id):
    """Recheck an existing grant only; caller owns transaction/COMMIT.

    This does not compare the grant against a new corpus as a new candidate and
    creates no eligibility. SQL rechecks the immutable identity and current
    negative authority; the owner independently verifies the stored signature.
    No remote reads or automatic retry of an uncertain COMMIT occur here.
    progress=None means authority-only manual preparation; quarantine means its
    runtime outcome is unverified. Only final/persisted is durable worker proof.
    """
    if type(persistence) is not ForwardMediaOwnerPersistence:
        raise PhotoCertificateHold('dedicated_prepared_owner_photo_required')
    persistence._assert_owner_identity()
    with persistence._conn.cursor() as cursor:
        cursor.execute('select public.fixer_reconcile_owner_photo_20261007(%s)', (audit_id,))
        result = cursor.fetchone()[0]
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from .forward_media_photo_certificate import canonical
        if not result['clearance']['history_evidence_ref'].startswith('owner-photo-reservation:'):
            raise ValueError()
        # The original has one immutable clearance. Each same-day rendition has
        # its own certificate; independently verify both stored signatures.
        for name, expected_ref in (
                ('certificate', result['manifest']['render_evidence_ref']),
                ('clearance_certificate', result['clearance']['history_evidence_ref'].removeprefix('owner-photo-reservation:'))):
            packet, key = result[name]['packet'], result[name]['approved_key']
            payload = packet['payload']
            if (key.get('approved') is not True
                    or (name == 'certificate' and payload['audit_id'] != audit_id)
                    or payload['key_id'] != key['key_id'] or payload['auditor_id'] != key['auditor_id']
                    or payload['policy_id'] != key['policy_id']):
                raise ValueError()
            payload_json = canonical(payload)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key['public_key_hex'])).verify(
                bytes.fromhex(packet['signature_hex']), payload_json.encode())
            ref = 'photo-audit:sha256:' + hashlib.sha256(
                (payload_json+'\n'+packet['signature_hex']).encode()).hexdigest()
            if expected_ref != ref:
                raise ValueError()
        if result['replayed'] is not True:
            raise ValueError()
    except Exception:
        raise PhotoCertificateHold('existing_photo_signature_or_identity_invalid') from None
    return {k: result[k] for k in ('registry', 'clearance', 'manifest', 'replayed', 'progress')}


def prepare_remote_photo(snapshot, *, drive_reader, hosted_reader, recipe, auditor, audit_id):
    """Read-only remote phase. Uses an existing explicit owner auditor client.

    The source receipt must already have been staged/committed by the existing
    isolated source verifier before the independent auditor signs its certificate.
    This function provisions no key, login, approval or database connection.
    """
    if type(auditor) is not IndependentPhotoAuditor:
        raise PhotoCertificateHold('dedicated_owner_certificate_client_required')
    row = snapshot['calendar']
    if (row.get('status') not in ('draft','pending','queued','approved') or row.get('variant_status')!='active'
            or any(row.get(k) is not None for k in ('publish_claim_token','published_at','late_post_id','render_manifest_digest'))):
        raise PhotoCertificateHold('certified_owner_candidate_not_unsent')
    recipe = validate_still_recipe(recipe)
    source = verify_source(snapshot, drive_reader, hosted_reader)
    image_bytes = hosted_reader.read(row['image_url'])
    replay = replay_still_recipe(source.source_bytes, recipe,
                                 has_thumbnail=row.get('thumbnail_url') is not None)
    if replay['image_bytes'] != image_bytes:
        raise PhotoCertificateHold('certified_owner_render_bytes_mismatch')
    image_fp, image_len = prepare.fingerprint_bytes(image_bytes)
    with auditor.conn.cursor() as cursor:
        cursor.execute("select 'sha256:'||encode(sha256(convert_to(public.fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')", (row['id'],))
        content_digest = cursor.fetchone()[0]
    candidate = {
        'calendar_row_id': row['id'], 'tenant_id': row['gym_id'],
        'group_key': row['visual_group_key'], 'post_date': row['post_date'],
        'source_asset_id': row['source_media_asset_id'], 'source_url': row['source_media_url'],
        'source_fingerprint': source.original.source_fingerprint,
        'source_sha256': source.evidence['source_sha256'], 'source_length': len(source.source_bytes),
        'source_receipt_ref': source.receipt_ref, 'image_url': row['image_url'],
        'image_fingerprint': image_fp, 'image_sha256': 'sha256:'+hashlib.sha256(image_bytes).hexdigest(),
        'image_length': image_len, 'render_recipe_digest': digest(recipe), 'content_digest': content_digest,
    }
    # Immutable signed database packet provides the thumbnail binding; remote
    # bytes cannot choose it. Exact candidate/signature/corpus verification below
    # binds every field before any authority is staged.
    stored = auditor._rpc('certificate', (str(uuid.UUID(audit_id)),))
    signed = stored['packet']['payload']['candidate']
    # This phase performs read-only lookups. End their transaction before the
    # thumbnail host read, as source/image reads already do; final SQL authority
    # rechecks the complete immutable certificate and creative under locks.
    auditor.conn.rollback()
    thumb_manifest = {'image_url': row['image_url'], 'render_recipe': recipe,
        'thumbnail_url': signed.get('thumbnail_url'),
        'thumbnail_sha256': (signed.get('thumbnail_sha256') or '').removeprefix('sha256:') or None,
        'thumbnail_fingerprint': signed.get('thumbnail_fingerprint'),
        'thumbnail_length': signed.get('thumbnail_length')}
    thumbnail = prepare_thumbnail_candidate(snapshot=row, manifest=thumb_manifest,
        source_bytes=source.source_bytes, image_bytes=image_bytes, read_bytes=hosted_reader.read)
    if 'thumbnail_url' in signed:
        candidate.update(thumbnail_url=thumbnail.thumbnail_url,
            thumbnail_sha256=('sha256:'+thumbnail.thumbnail_sha256 if thumbnail.thumbnail_sha256 else None),
            thumbnail_fingerprint=thumbnail.thumbnail_fingerprint, thumbnail_length=thumbnail.thumbnail_length)
    certificate = auditor.lookup_for_owner(audit_id, candidate)
    # Match guard.attest's complete object tuple: a transformed thumbnail
    # requires replay even when the delivered image is the original object.
    source_url = source.original.source_url
    if all(url is None or url == source_url for url in (row['image_url'], thumbnail.thumbnail_url)):
        operation = 'same_object'
    elif image_bytes == source.source_bytes and (
            thumbnail.thumbnail_bytes is None or thumbnail.thumbnail_bytes == source.source_bytes):
        operation = 'rehost'
    else:
        operation = 'render'
    manifest = prepare.build_render_manifest(source.original, row['image_url'], image_bytes,
        operation, certificate.receipt_ref, render_recipe=recipe,
        thumbnail_url=thumbnail.thumbnail_url, thumbnail_bytes=thumbnail.thumbnail_bytes)
    return PreparedOwnerPhoto(source, image_bytes, manifest, certificate, thumbnail.thumbnail_bytes)


def stage_prepared_photo(persistence, prepared):
    """Final phase; caller owns transaction and COMMIT, failures must roll back.

    No remote reads. The SQL grant creates all authority/reservation rows in one
    transaction, then existing persistence compares the exact immutable tuples
    using frozen bytes from remote verification. A return value is not delivery
    or durable commit proof. A lost COMMIT response must be reconciled.
    """
    if type(persistence) is not ForwardMediaOwnerPersistence or type(prepared) is not PreparedOwnerPhoto:
        raise PhotoCertificateHold('dedicated_prepared_owner_photo_required')
    persistence._assert_owner_identity()
    # The independent certificate already refers to a committed source receipt.
    # Re-inserting it would revalidate its obsolete full calendar revision after
    # the service binder changes render_manifest_digest. SQL instead checks the
    # immutable receipt, current source binding and signed creative under locks.
    original = prepared.source.original
    with persistence._conn.cursor() as cursor:
        cursor.execute('select public.fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',
            (prepared.certificate.payload['audit_id'], json.dumps(original.row()),
             json.dumps(prepared.manifest.row(), ensure_ascii=False)))
        clearance_row = cursor.fetchone()[0]
    clearance = prepare.HistoryClearance(**clearance_row)
    canonical_original = {k: clearance_row[k] for k in original.row()}
    if any(canonical_original[k] != v for k, v in original.row().items()
           if k != 'registry_evidence_ref'):
        raise PhotoCertificateHold('certified_owner_original_anchor_mismatch')
    original = prepare.OriginalRegistration(**canonical_original)
    frozen = ForwardMediaOwnerPersistence(persistence._conn, persistence._expected_owner,
        _FrozenBytes(prepared.source, prepared.manifest.image_url, prepared.image_bytes,
                     prepared.manifest.thumbnail_url, prepared.thumbnail_bytes))
    return frozen.persist_in_transaction(original, clearance, prepared.manifest)


def run_photo_pass(*, persistence, reader, drive_reader, tenants, limit):
    """Bounded signed-certificate worker pass, reached by owner_worker.run_once.

    A durable attempt quarantine precedes remote I/O. Authority and its outcome
    share one acknowledged final COMMIT. Failed reads, crashes and uncertain
    commits remain excluded from discovery for manual reconciliation.
    """
    from .forward_media_source_history import SourceHistoryStore
    from .forward_media_owner import UncertainCommitError
    from psycopg.pq import TransactionStatus
    reports = []
    if (type(persistence) is not ForwardMediaOwnerPersistence
            or persistence._conn.info.transaction_status != TransactionStatus.IDLE):
        return {'status': 'hold', 'reason': 'owner_transaction_contract_required', 'rows': []}
    conn = persistence._conn

    def rpc(name, args):
        with conn.cursor() as cursor:
            cursor.execute('select public.fixer_owner_photo_'+name+'_20261007('
                           + ','.join(['%s']*len(args))+')', args)
            return cursor.fetchone()[0]

    def commit():
        try:
            conn.commit()
        except Exception:
            raise UncertainCommitError('owner photo commit uncertain') from None

    try:
        persistence._assert_owner_identity()
        candidates = rpc('pending', (list(tenants), limit))
        conn.rollback()
        if not isinstance(candidates, list) or len(candidates) > limit:
            raise PhotoCertificateHold('certified_owner_batch_invalid')
        for candidate in candidates:
            audit_id = str(uuid.UUID(candidate['audit_id']))
            token = str(uuid.uuid4())
            persistence._assert_owner_identity()
            reserved = rpc('reserve', (audit_id, token))
            commit()
            if reserved is not True:
                # Another worker admitted the exact audit; never duplicate it.
                continue
            try:
                snapshot = SourceHistoryStore(persistence).snapshot(
                    candidate['calendar_row_id'], candidate['revision'])
                prepared = prepare_remote_photo(snapshot, drive_reader=drive_reader,
                    hosted_reader=reader, recipe=candidate['recipe'],
                    auditor=IndependentPhotoAuditor(conn, persistence._expected_owner), audit_id=audit_id)
                conn.rollback()  # End read-only certificate tx before final locks.
                staged = stage_prepared_photo(persistence, prepared)
                outcome = {'status': 'persisted', 'decision': 'cleared_unused',
                           'manifest_digest': staged['manifest']['manifest_digest']}
                if rpc('finish', (audit_id, token, json.dumps(outcome))) is not True:
                    raise PhotoCertificateHold('certified_owner_outcome_unverified')
                commit()
                reports.append({'audit_id': audit_id, 'calendar_row_id': candidate['calendar_row_id'], **outcome})
            except UncertainCommitError:
                raise
            except Exception:
                conn.rollback()
                reports.append({'audit_id': audit_id, 'calendar_row_id': candidate['calendar_row_id'],
                                'status': 'hold', 'reason': 'certified_owner_verification_failed'})
    except UncertainCommitError:
        # Never re-admit the audit or continue after a lost COMMIT response.
        return {'status': 'hold', 'reason': 'uncertain_authority_commit', 'rows': reports}
    except Exception:
        conn.rollback()
        return {'status': 'hold', 'reason': 'certified_owner_transport_unavailable', 'rows': reports}
    return {'status': 'partial_hold' if any(r['status']=='hold' for r in reports) else 'complete', 'rows': reports}
