"""DRAFT isolated owner positive photo preparation, no factory or activation.

Remote Drive/hosted reads and deterministic replay finish before final locks.
The owner independently verifies Ed25519 and exact candidate/corpus, then SQL
rechecks the authenticated immutable certificate under exclusive graph authority.
Only that RPC can create a positive visual reservation plus the exact immutable
original/clearance/manifest tuple. No publisher uses this adapter.
"""
from dataclasses import dataclass
import hashlib
import json

from . import forward_media_prepare as prepare
from .forward_media_attester import replay_still_recipe, validate_still_recipe
from .forward_media_owner import ForwardMediaOwnerPersistence, ObjectReader
from .forward_media_photo_certificate import IndependentPhotoAuditor, PhotoCertificateHold, digest
from .forward_media_source_history import SourceHistoryStore
from .forward_media_source_verifier import verify_source


@dataclass(frozen=True)
class PreparedOwnerPhoto:
    source: object
    image_bytes: bytes
    manifest: prepare.RenderManifest
    certificate: object


class _FrozenBytes(ObjectReader):
    def __init__(self, source, image_url, image_bytes):
        self.values = {source.original.source_url: source.source_bytes, image_url: image_bytes}

    def read(self, url):
        if url not in self.values:
            raise PhotoCertificateHold('certified_owner_bytes_unavailable')
        return self.values[url]


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
    if row.get('thumbnail_url') is not None:
        raise PhotoCertificateHold('thumbnail_candidate_contract_missing')
    recipe = validate_still_recipe(recipe)
    source = verify_source(snapshot, drive_reader, hosted_reader)
    image_bytes = hosted_reader.read(row['image_url'])
    replay = replay_still_recipe(source.source_bytes, recipe)
    if replay['image_bytes'] != image_bytes or replay['thumbnail_bytes'] is not None:
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
    certificate = auditor.lookup_for_owner(audit_id, candidate)
    operation = ('same_object' if row['image_url'] == source.original.source_url
                 else 'rehost' if recipe['image']['name'] == 'identity' else 'render')
    manifest = prepare.build_render_manifest(source.original, row['image_url'], image_bytes,
        operation, certificate.receipt_ref, render_recipe=recipe)
    return PreparedOwnerPhoto(source, image_bytes, manifest, certificate)


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
    original = SourceHistoryStore(persistence).stage_source(prepared.source)
    with persistence._conn.cursor() as cursor:
        cursor.execute('select public.fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',
            (prepared.certificate.payload['audit_id'], json.dumps(original.row()),
             json.dumps(prepared.manifest.row(), ensure_ascii=False)))
        clearance_row = cursor.fetchone()[0]
    clearance = prepare.HistoryClearance(**clearance_row)
    frozen = ForwardMediaOwnerPersistence(persistence._conn, persistence._expected_owner,
        _FrozenBytes(prepared.source, prepared.manifest.image_url, prepared.image_bytes))
    return frozen.persist_in_transaction(original, clearance, prepared.manifest)
