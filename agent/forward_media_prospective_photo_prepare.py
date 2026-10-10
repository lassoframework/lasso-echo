"""DRAFT prospective permanent photo occupancy admission helper, default OFF.

This module prepares admission of ONE explicitly supplied, independently
evidenced, approved still photo into the prospective permanent occupancy
ledger whose authority is defined by Agent A's
migrations/DRAFT_fixer_prospective_photo_authority_20261008.sql
(public.forward_prospective_photo_occupancy_20261008). It is a helper, not a
worker:

- NO candidate discovery: the caller supplies exactly one owner SQL snapshot
  (fixer_forward_media_source_snapshot_20261007 shape) plus one live-reread
  media_asset row for the same Drive file. Nothing here lists, scans, ranks
  or selects candidates, and the selector's used_count/last_used_at stamps
  play no part in any decision.
- NO certificate fabrication: positive visual authority comes only from the
  existing independently signed photo certificate, re-verified through
  forward_media_photo_certificate.IndependentPhotoAuditor.lookup_for_owner.
- NO automatic media approval: approval/consent/safety evidence is only
  READ, through the existing trusted seam gym_media_selector.is_usable
  (review_status='approved' with review_content_hash bound to the asset's
  current content_hash, plus byte-bound clean moderation evidence). Missing
  or stale evidence holds; nothing here approves, stamps or mutates the
  asset row.
- Exact bytes only: the existing source verifier re-reads live Drive
  metadata/version and the bounded original bytes and requires the exact
  hosted source URL to serve byte-identical bytes; the deterministic render
  recipe is replayed and must reproduce the delivered image bytes exactly;
  the recomputed source hash must equal the asset's attested content_hash.

Gate: AGENT_FORWARD_PROSPECTIVE_PHOTO_PREPARE, tri-state like the global
ledger flag. Default OFF; an ambiguous value fails closed. The SQL side has
its own protected singleton gate (forward_prospective_photo_gate_20261008,
default OFF); both must be armed by hand.

Owner admission RPC (defined by Agent A's migration; this helper calls it
over the dedicated owner connection only, never re-implementing its trust):

    public.admit_prospective_photo_occupancy_20261008(
        p_calendar_row_id uuid, p_logical_post_id uuid,
        p_expected_revision text, p_attestation_ids uuid[],
        p_audit_id uuid) returns uuid  -- the immutable occupancy_id receipt

The RPC re-locks and re-validates everything itself (unsent planned row,
current media revision, signed certificate candidate binding, durable source
receipt, trusted attester object-read receipts, lineage, negative evidence,
permanent use ledger, claims, reservations and existing occupancy) under its
exclusive graph authority; this helper only validates the argument shapes
and the returned receipt. The immutable exact receipt for reconciliation is
the (calendar_row_id, source_sha256) pair plus the returned occupancy_id,
read back through the lane-tagged proof RPC:

    public.forward_prospective_photo_proof_20261008(
        p_calendar_row_id uuid, p_source_sha256 text) returns jsonb

Uncertain COMMIT: the write is never blindly retried. A lost commit response
is reconciled by reading the exact receipt back through the proof RPC
(reconcile_admission); only a lane-tagged 'prospective_occupancy' proof whose
occupancy_id/tenant/date/logical post/audit id match the exact prepared
tuple proves admission. Anything else is 'unknown' for manual resolution.
"""
from dataclasses import dataclass
import hashlib
import os
import re
import uuid

from . import forward_media_prepare as prepare
from . import gym_media_selector
from .forward_media_attester import replay_still_recipe, validate_still_recipe
from .forward_media_owner import ForwardMediaOwnerPersistence, UncertainCommitError
from .forward_media_photo_certificate import (
    IndependentPhotoAuditor,
    digest,
)
from .forward_media_source_verifier import verify_source
from .forward_media_still_certificate_v2 import IndependentStillPhotoAuditorV2
from .forward_media_still_certificate_v2 import verify_still_v2
from .forward_media_thumbnail_candidate import prepare_thumbnail_candidate


FLAG_ENV = 'AGENT_FORWARD_PROSPECTIVE_PHOTO_PREPARE'

# Agent A's RPCs (migrations/DRAFT_fixer_prospective_photo_authority_20261008.sql).
ADMISSION_RPC = 'admit_prospective_photo_occupancy_20261008'
PROOF_RPC = 'forward_prospective_photo_proof_20261008'
PROOF_LANE = 'prospective_occupancy'
_SHA256_BARE = re.compile(r'[0-9a-f]{64}\Z')


class ProspectivePhotoHold(RuntimeError):
    """Static reasons only; never expose packet data, URLs or driver errors."""


def enabled():
    """Tri-state read of the prepare flag: True on, False off/unset, None
    ambiguous (fail closed). Default OFF."""
    raw = (os.environ.get(FLAG_ENV, '') or '').strip().lower()
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('', '0', 'false', 'no', 'off'):
        return False
    return None


@dataclass(frozen=True)
class PreparedProspectivePhoto:
    source: object
    image_bytes: bytes
    manifest: prepare.RenderManifest
    certificate: object
    approval_evidence: dict
    thumbnail_bytes: bytes | None = None


def _uuid(value, reason):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ProspectivePhotoHold(reason)
    except (ValueError, AttributeError, TypeError):
        raise ProspectivePhotoHold(reason) from None
    return value


def _attested_source_sha256(source_bytes, asset):
    """Recompute the source hash and require it to equal the attested hash.

    The approved asset row's content_hash (bound to review_content_hash and
    the moderation evidence by is_usable) is the attestation. A recomputed
    hash that differs from the attested hash rejects the candidate: the
    approved byte version is not the byte version being admitted.
    """
    attested = str(asset.get('content_hash') or '').strip().lower()
    if len(attested) == 64:
        recomputed = hashlib.sha256(source_bytes).hexdigest()
    elif len(attested) == 32:
        recomputed = hashlib.md5(source_bytes).hexdigest()
    else:
        raise ProspectivePhotoHold('prospective_attested_hash_unreadable')
    if recomputed != attested:
        raise ProspectivePhotoHold('prospective_attested_hash_mismatch')
    return recomputed


def _approval_evidence(asset, snapshot):
    """Read-only approval/consent/safety evidence check for ONE reread asset.

    Reuses gym_media_selector.is_usable, the one trusted implementation of
    the byte-bound review + moderation evidence predicate. Never stamps,
    approves or mutates anything; absence of evidence holds.
    """
    row = snapshot.get('calendar') if isinstance(snapshot, dict) else None
    bound_asset = snapshot.get('asset') if isinstance(snapshot, dict) else None
    if not isinstance(asset, dict) or not isinstance(bound_asset, dict) or not isinstance(row, dict):
        raise ProspectivePhotoHold('prospective_approval_evidence_missing')
    # Bind the fresh approval row to the canonical owner snapshot before using
    # its review evidence. IDs and gym alone do not prove source provenance or
    # that this is the approved byte version represented by the snapshot.
    if (asset.get('kind') != 'photo'
            or str(asset.get('id') or '') != str(row.get('source_media_asset_id') or '')
            or str(asset.get('gym_id') or '') != str(row.get('gym_id') or '')
            or not asset.get('source_id')
            or asset.get('source_id') != bound_asset.get('source_id')
            or asset.get('id') != bound_asset.get('id')
            or asset.get('gym_id') != bound_asset.get('gym_id')
            or not asset.get('content_hash')
            or asset.get('content_hash') != bound_asset.get('content_hash')
            or not asset.get('review_content_hash')
            or asset.get('review_content_hash') != bound_asset.get('review_content_hash')):
        raise ProspectivePhotoHold('prospective_approval_evidence_missing')
    if not gym_media_selector.is_usable(asset):
        raise ProspectivePhotoHold('prospective_approval_evidence_missing')
    return {'review_status': asset.get('review_status'),
            'reviewed_by': asset.get('reviewed_by'),
            'reviewed_at': asset.get('reviewed_at'),
            'review_content_hash': asset.get('review_content_hash'),
            'moderation_status': asset.get('moderation_status')}


def prepare_prospective_photo(snapshot, *, asset, drive_reader, hosted_reader,
                              recipe, auditor, audit_id):
    """Read-only remote phase for ONE explicitly supplied candidate.

    Rereads live approved source metadata (the caller-supplied `asset` row,
    read fresh by the caller from the media store), the exact source/version
    and the rendered bytes through the existing trusted seams. No discovery,
    no writes; the caller ends the read transaction before final locks.
    """
    if enabled() is not True:
        raise ProspectivePhotoHold('prospective_prepare_disabled')
    if type(auditor) not in (IndependentPhotoAuditor, IndependentStillPhotoAuditorV2):
        raise ProspectivePhotoHold('dedicated_owner_certificate_client_required')
    row = snapshot['calendar']
    staged = (type(auditor) is IndependentStillPhotoAuditorV2
              and row.get('variant_status') == 'candidate'
              and row.get('media_not_ready_reason') == 'forward_reservation_staged')
    if (row.get('status') not in ('draft', 'pending', 'queued', 'approved')
            or (row.get('variant_status') != 'active' and not staged)
            or any(row.get(k) is not None for k in (
                'publish_claim_token', 'published_at', 'late_post_id'))
            or (row.get('media_not_ready_reason') is not None and not staged)):
        raise ProspectivePhotoHold('prospective_candidate_not_unsent')
    if staged:
        # The marker is insufficient. Read the existing owner-authorized
        # predicate for exact persisted nonterminal batch membership, then end
        # this read transaction BEFORE any remote source/object reads.
        try:
            with auditor.conn.cursor() as cursor:
                cursor.execute('select public.forward_schedule_preparation_eligible_20261008(%s)',
                               (row['id'],))
                result = cursor.fetchone()
                state = result[0] if result else None
        finally:
            auditor.conn.rollback()
        if (not isinstance(state, dict) or state.get('eligible') is not True
                or state.get('mode') != 'staged'
                or state.get('tenant_id') != snapshot.get('tenant_id', row.get('gym_id'))
                or not state.get('batch_id')
                or (snapshot.get('batch_id') is not None
                    and str(snapshot['batch_id']) != str(state['batch_id']))):
            raise ProspectivePhotoHold('prospective_staged_membership_unavailable')
    evidence = _approval_evidence(asset, snapshot)
    recipe = validate_still_recipe(recipe)
    source_snapshot = snapshot
    if type(auditor) is IndependentStillPhotoAuditorV2 and snapshot.get('source_receipt_revision') is not None:
        # Binding the manifest changes the calendar revision between owner
        # phases. Recompute the original immutable source receipt with its
        # independently stored revision; every current byte/version/path and
        # asset/source binding must still produce the signed receipt exactly.
        revision = snapshot['source_receipt_revision']
        if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{32}', revision):
            raise ProspectivePhotoHold('prospective_receipt_ref_invalid')
        source_snapshot = dict(snapshot, revision=revision)
    source = verify_source(source_snapshot, drive_reader, hosted_reader)
    _attested_source_sha256(source.source_bytes, asset)
    image_bytes = hosted_reader.read(row['image_url'])
    replay = replay_still_recipe(source.source_bytes, recipe,
                                 has_thumbnail=row.get('thumbnail_url') is not None)
    if replay['image_bytes'] != image_bytes:
        raise ProspectivePhotoHold('prospective_render_bytes_mismatch')
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
        'image_fingerprint': image_fp, 'image_sha256': 'sha256:' + hashlib.sha256(image_bytes).hexdigest(),
        'image_length': image_len, 'render_recipe_digest': digest(recipe), 'content_digest': content_digest,
    }
    # Read-only lookups end their transaction before final authority (same
    # contract as the owner photo prepare lane).
    auditor.conn.rollback()
    thumbnail = None
    if type(auditor) is IndependentStillPhotoAuditorV2:
        candidate['logical_post_id'] = _uuid(row.get('logical_post_id'), 'prospective_admission_binding_invalid')
        stored = auditor._rpc('certificate', (_uuid(audit_id, 'prospective_receipt_ref_invalid'),))
        signed = stored['packet']['payload']['candidate']
        auditor.conn.rollback()  # End lookup before any hosted thumbnail read.
        thumb_manifest = {'image_url': row['image_url'], 'render_recipe': recipe,
            'thumbnail_url': signed.get('thumbnail_url'),
            'thumbnail_sha256': (signed.get('thumbnail_sha256') or '').removeprefix('sha256:') or None,
            'thumbnail_fingerprint': signed.get('thumbnail_fingerprint'),
            'thumbnail_length': signed.get('thumbnail_length')}
        thumbnail = prepare_thumbnail_candidate(snapshot=row, manifest=thumb_manifest,
            source_bytes=source.source_bytes, image_bytes=image_bytes, read_bytes=hosted_reader.read)
        if 'thumbnail_url' in signed:
            candidate.update(thumbnail_url=thumbnail.thumbnail_url,
                thumbnail_sha256=('sha256:' + thumbnail.thumbnail_sha256 if thumbnail.thumbnail_sha256 else None),
                thumbnail_fingerprint=thumbnail.thumbnail_fingerprint,
                thumbnail_length=thumbnail.thumbnail_length)
    if type(auditor) is IndependentStillPhotoAuditorV2 and snapshot.get('certificate_snapshot') is not None:
        # Dedicated SQL readback removes only this exact existing grant from
        # the live corpus. Foreign reservations and all unknown history remain.
        certificate = verify_still_v2(stored['packet'], stored['approved_key'],
                                     snapshot['certificate_snapshot'], candidate)
    else:
        certificate = auditor.lookup_for_owner(audit_id, candidate)
    source_url = source.original.source_url
    thumb_url = thumbnail.thumbnail_url if thumbnail else None
    thumb_bytes = thumbnail.thumbnail_bytes if thumbnail else None
    if all(url is None or url == source_url for url in (row['image_url'], thumb_url)):
        operation = 'same_object'
    elif image_bytes == source.source_bytes and (thumb_bytes is None or thumb_bytes == source.source_bytes):
        operation = 'rehost'
    else:
        operation = 'render'
    manifest = prepare.build_render_manifest(source.original, row['image_url'], image_bytes,
        operation, certificate.receipt_ref, render_recipe=recipe,
        thumbnail_url=thumb_url, thumbnail_bytes=thumb_bytes)
    return PreparedProspectivePhoto(source, image_bytes, manifest, certificate, evidence, thumb_bytes)


def stage_prepared_still_v2(persistence, prepared):
    """Stage v2 source/clearance/manifest authority; caller commits outcome.

    The accepted prospective helper already checked approval, consent, source,
    rendition and real signature. Existing owner persistence defensively reads
    frozen local bytes only while the SQL grant holds the authority locks.
    Permanent occupancy remains a separate phase after attester evidence.
    """
    if (type(prepared) is not PreparedProspectivePhoto
            or prepared.certificate.payload.get('schema_version') != 2):
        raise ProspectivePhotoHold('dedicated_prepared_prospective_photo_required')
    from .forward_media_owner_photo_prepare import PreparedOwnerPhoto, _stage_prepared
    photo = PreparedOwnerPhoto(prepared.source, prepared.image_bytes, prepared.manifest,
                               prepared.certificate, prepared.thumbnail_bytes)
    return _stage_prepared(persistence, photo, 'fixer_prepare_owner_staged_still_v2_20261008')


def admission_receipt(prepared):
    """The immutable exact receipt identifying one prospective admission.

    The (calendar_row_id, bare source_sha256) pair is the read-back key for
    the lane-tagged proof RPC; the signed certificate audit_id binds this
    helper's prepared tuple to the exact candidate the SQL admission
    revalidates under locks.
    """
    if type(prepared) is not PreparedProspectivePhoto:
        raise ProspectivePhotoHold('dedicated_prepared_prospective_photo_required')
    candidate = prepared.certificate.payload['candidate']
    source_sha256 = str(candidate['source_sha256']).removeprefix('sha256:')
    if not _SHA256_BARE.fullmatch(source_sha256):
        raise ProspectivePhotoHold('prospective_receipt_ref_invalid')
    return {'calendar_row_id': _uuid(candidate['calendar_row_id'],
                                     'prospective_receipt_ref_invalid'),
            'source_sha256': source_sha256,
            'audit_id': _uuid(prepared.certificate.payload['audit_id'],
                              'prospective_receipt_ref_invalid'),
            'tenant_id': candidate['tenant_id'],
            'post_date': candidate['post_date']}


def admit_prepared_photo(persistence, prepared, *, logical_post_id,
                         expected_revision, attestation_ids):
    """Final phase; caller owns transaction and COMMIT, failures roll back.

    Calls ONLY Agent A's owner admission RPC, which rechecks the immutable
    certificate, durable source receipt, trusted byte attestations and every
    occupancy ledger under its exclusive locks. Returns the occupancy_id the
    RPC returned; a return value is not durable commit proof. A lost COMMIT
    response must be reconciled by receipt, never blindly retried.
    """
    if type(persistence) is not ForwardMediaOwnerPersistence \
            or type(prepared) is not PreparedProspectivePhoto:
        raise ProspectivePhotoHold('dedicated_prepared_prospective_photo_required')
    receipt = admission_receipt(prepared)
    logical_post_id = _uuid(logical_post_id, 'prospective_admission_binding_invalid')
    if (not isinstance(expected_revision, str)
            or not expected_revision or expected_revision != expected_revision.strip()):
        raise ProspectivePhotoHold('prospective_admission_binding_invalid')
    if (not isinstance(attestation_ids, (list, tuple)) or len(attestation_ids) != 3
            or len(set(attestation_ids)) != 3):
        raise ProspectivePhotoHold('prospective_admission_binding_invalid')
    attestation_ids = [_uuid(a, 'prospective_admission_binding_invalid')
                       for a in attestation_ids]
    persistence._assert_owner_identity()
    with persistence._conn.cursor() as cursor:
        rpc = ('admit_prospective_still_v2_20261008'
               if prepared.certificate.payload.get('schema_version') == 2 else ADMISSION_RPC)
        cursor.execute('select public.' + rpc + '(%s,%s,%s,%s::uuid[],%s)',
                       (receipt['calendar_row_id'], logical_post_id,
                        expected_revision, list(attestation_ids), receipt['audit_id']))
        row = cursor.fetchone()
    result = row[0] if row else None
    try:
        occupancy_id = str(uuid.UUID(str(result)))
    except Exception:
        raise ProspectivePhotoHold('prospective_occupancy_receipt_invalid') from None
    return {'occupancy_id': occupancy_id, **receipt}


def reconcile_admission(persistence, receipt, *, logical_post_id=None,
                        occupancy_id=None):
    """Read-only read-by-receipt reconciliation of an uncertain commit.

    Reads the lane-tagged proof RPC for the exact (calendar_row_id,
    source_sha256) receipt. Only an ACTIVE lane='prospective_occupancy' proof
    whose tenant/date/audit id match the prepared tuple — and whose logical
    post and occupancy_id match when the caller holds them — proves the
    admission landed. A missing proof (the RPC raises when no active
    occupancy resolves), a wrong lane or any field mismatch is 'unknown',
    never 'admitted'. Ends its own read transaction; never retries or
    rewrites the admission.
    """
    if type(persistence) is not ForwardMediaOwnerPersistence:
        raise ProspectivePhotoHold('dedicated_prepared_prospective_photo_required')
    if (not isinstance(receipt, dict)
            or not _SHA256_BARE.fullmatch(str(receipt.get('source_sha256') or ''))):
        raise ProspectivePhotoHold('prospective_receipt_ref_invalid')
    calendar_row_id = _uuid(receipt.get('calendar_row_id'),
                            'prospective_receipt_ref_invalid')
    try:
        persistence._assert_owner_identity()
        with persistence._conn.cursor() as cursor:
            cursor.execute('select public.' + PROOF_RPC + '(%s,%s)',
                           (calendar_row_id, receipt['source_sha256']))
            row = cursor.fetchone()
        proof = row[0] if row else None
    except Exception:
        proof = None
    finally:
        persistence._conn.rollback()
    if (isinstance(proof, dict) and proof.get('lane') == PROOF_LANE
            and proof.get('tenant_id') == receipt.get('tenant_id')
            and str(proof.get('post_date') or '')[:10] == receipt.get('post_date')
            and proof.get('source_sha256') == receipt.get('source_sha256')
            and str(proof.get('audit_id') or '') == receipt.get('audit_id')
            and (logical_post_id is None
                 or str(proof.get('logical_post_id') or '') == str(logical_post_id))
            and (occupancy_id is None
                 or str(proof.get('occupancy_id') or '') == str(occupancy_id))):
        return {'status': 'admitted', 'occupancy': proof}
    return {'status': 'unknown', 'receipt': receipt}


def admit_single_candidate(persistence, prepared, *, logical_post_id,
                           expected_revision, attestation_ids):
    """Admit one prepared candidate, commit, and reconcile an uncertain commit.

    The admission RPC result and its COMMIT share one outcome. A lost commit
    response is reconciled ONLY by reading the immutable exact receipt
    through the lane-tagged proof RPC; the write is never retried
    automatically, and a reconcile miss reports 'uncertain' for manual
    resolution.
    """
    conn = persistence._conn
    try:
        admitted = admit_prepared_photo(persistence, prepared,
                                        logical_post_id=logical_post_id,
                                        expected_revision=expected_revision,
                                        attestation_ids=attestation_ids)
        try:
            conn.commit()
        except Exception:
            raise UncertainCommitError(
                'prospective occupancy commit uncertain') from None
    except UncertainCommitError:
        try:
            conn.rollback()
        except Exception:
            pass
        reconciled = reconcile_admission(
            persistence, admission_receipt(prepared),
            logical_post_id=logical_post_id,
            occupancy_id=admitted['occupancy_id'])
        if reconciled['status'] == 'admitted':
            return {'status': 'reconciled_committed',
                    'occupancy': reconciled['occupancy']}
        return {'status': 'uncertain', 'receipt': admission_receipt(prepared),
                'occupancy_id': admitted['occupancy_id']}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return {'status': 'admitted', **admitted}
