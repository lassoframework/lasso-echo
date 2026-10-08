"""DRAFT independently signed schema 2 clearance for published still images.

No inference from missing history, Drive times or asset stage-use. Reviewed video
rows are accounted explicitly outside this precise scope; their frames remain
unreviewed and no all-visual clearance is asserted. Unknown media kinds HOLD.
V1 verifier and zero-exclusion guards are unchanged. No connections or keys are
created; caller owns isolated auditor/owner connections and transaction outcome.
"""
import json
import uuid
import hashlib
from .forward_media_photo_certificate import (
    PhotoCertificateHold, VerifiedPhotoCertificate, validate_candidate,
    canonical, digest, _SHA, MAX_CORPUS_ROWS, MAX_PACKET_BYTES,
    IndependentPhotoAuditor,
)
CORPUS_SCHEMA_VERSION=2


def require_accounted_videos(payload,snapshot):
    videos=snapshot.get('accounted_video_rows')
    dispositions=payload.get('accounted_videos')
    if (payload.get('scope')!='published_still_images_and_derivatives'
            or payload.get('candidate_media_kind')!='still_photo'
            or snapshot.get('scope')!='published_still_images_and_derivatives'
            or type(snapshot.get('excluded_rows_count')) is not int
            or not isinstance(videos,list) or not isinstance(dispositions,list)
            or len(videos)!=snapshot['excluded_rows_count']
            or len(videos)!=len(dispositions)
            or payload.get('accounted_video_digest')!=snapshot.get('excluded_rows_digest')
            or not _SHA.fullmatch(str(payload.get('accounted_video_digest','')))
            or len(videos)>MAX_CORPUS_ROWS):
        raise PhotoCertificateHold('still_scope_videos_unaccounted')
    indexed={v.get('history_key'):v for v in videos}
    if len(indexed)!=len(videos) or len({d.get('history_key') for d in dispositions})!=len(videos):
        raise PhotoCertificateHold('still_scope_videos_unaccounted')
    for d in dispositions:
        v=indexed.get(d.get('history_key'))
        if (v is None or v.get('media_kind')!='reviewed_video_scope_exclusion'
                or not v.get('published_binding_ref')
                or d.get('published_binding_ref')!=v.get('published_binding_ref')
                or d.get('disposition')!='accounted_out_of_scope_video_frames_unreviewed'
                or not isinstance(d.get('review_evidence_ref'),str)
                or not d['review_evidence_ref'].strip()):
            raise PhotoCertificateHold('still_scope_unknown_kind_hold')


def verify_still_v2(packet,approved_key,snapshot,expected_candidate=None):
    """Verify actual signature and a COMPLETE independently reviewed manifest.

    Snapshot/key must come from the dedicated database RPC, not a producer or
    arbitrary JSON config. Injected dictionaries are offline fixture seams.
    Dispositions are the independent auditor's judgments; algorithms do not
    certify visual exclusion. Unknown rows or unapproved scope always hold.
    """
    try:
        if not isinstance(packet,dict) or set(packet)!={'payload','signature_hex'}:
            raise PhotoCertificateHold('certificate_shape_invalid')
        p=packet['payload']
        required={'schema_version','audit_id','auditor_id','key_id','policy_id',
                  'baseline_id','generation','spine_digest','candidate','dispositions',
                  'disposition_digest','decision','stated_visual_uncertainty','scope','accounted_video_digest','accounted_videos','candidate_media_kind'}
        if (not isinstance(p,dict) or set(p)!=required or type(p['schema_version']) is not int
                or p['schema_version']!=CORPUS_SCHEMA_VERSION or str(uuid.UUID(p['audit_id']))!=p['audit_id']
                or p['decision']!='no_prior_published_still_image_or_derivative_use'
                or not isinstance(p['stated_visual_uncertainty'],str)
                or not p['stated_visual_uncertainty'].strip()):
            raise PhotoCertificateHold('certificate_shape_invalid')
        if (approved_key.get('approved') is not True or p['key_id']!=approved_key.get('key_id')
                or p['auditor_id']!=approved_key.get('auditor_id')
                or p['policy_id']!=approved_key.get('policy_id')):
            raise PhotoCertificateHold('certificate_signer_unapproved')
        if (snapshot.get('policy_approved') is not True or snapshot.get('scope_complete') is not True
                or p['policy_id']!=snapshot.get('policy_id')
                or p['baseline_id']!=snapshot.get('baseline_id')
                or type(p['generation']) is not int or p['generation']!=snapshot.get('generation')
                or p['spine_digest']!=snapshot.get('spine_digest')):
            raise PhotoCertificateHold('certificate_corpus_stale_or_unapproved')
        require_accounted_videos(p,snapshot)
        validate_candidate({k:v for k,v in p['candidate'].items() if k!='logical_post_id'})
        if str(uuid.UUID(p['candidate'].get('logical_post_id',''))) != p['candidate']['logical_post_id']:
            raise PhotoCertificateHold('certificate_candidate_invalid')
        if expected_candidate is not None and p['candidate']!=expected_candidate:
            raise PhotoCertificateHold('certificate_candidate_changed')
        rows=snapshot.get('rows')
        dispositions=p['dispositions']
        if (not isinstance(rows,list) or not 0<=len(rows)<=MAX_CORPUS_ROWS
                or not isinstance(dispositions,list) or len(dispositions)!=len(rows)
                or p['disposition_digest']!=digest(dispositions)):
            raise PhotoCertificateHold('certificate_dispositions_incomplete')
        indexed={r['history_key']:r for r in rows}
        if len(indexed)!=len(rows) or len({d['history_key'] for d in dispositions})!=len(rows):
            raise PhotoCertificateHold('certificate_dispositions_incomplete')
        for d in dispositions:
            r=indexed.get(d.get('history_key'))
            if (r is None or r.get('resolved') is not True or r.get('media_kind')!='still_photo'
                    or d.get('disposition')!='reviewed_visual_nonmatch'
                    or d.get('inspected_sha256')!=r.get('visual_sha256')
                    or not _SHA.fullmatch(str(d.get('inspected_sha256','')))
                    or d.get('published_binding_ref')!=r.get('published_binding_ref')
                    or not isinstance(d.get('review_evidence_ref'),str)
                    or not d['review_evidence_ref'].strip()):
                raise PhotoCertificateHold('certificate_history_unresolved_or_matching')
        text=canonical(p)
        if len(text.encode())>MAX_PACKET_BYTES:
            raise PhotoCertificateHold('certificate_shape_invalid')
        sig=bytes.fromhex(packet['signature_hex'])
        pub=bytes.fromhex(approved_key['public_key_hex'])
        if len(sig)!=64 or len(pub)!=32:
            raise PhotoCertificateHold('certificate_signature_invalid')
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig,text.encode())
        except InvalidSignature:
            raise PhotoCertificateHold('certificate_signature_invalid') from None
        ref='photo-audit:sha256:'+hashlib.sha256((text+'\n'+packet['signature_hex']).encode()).hexdigest()
        return VerifiedPhotoCertificate(text,packet['signature_hex'],ref)
    except PhotoCertificateHold:
        raise
    except Exception:
        raise PhotoCertificateHold('certificate_verification_unavailable') from None


class IndependentStillPhotoAuditorV2(IndependentPhotoAuditor):
    """Independent authenticated verifier; owner re-verifies exact v2 bytes."""
    def _snapshot(self):
        with self.conn.cursor() as cur:
            cur.execute('select public.fixer_still_photo_snapshot_v2_20261008()')
            return cur.fetchone()[0]

    def submit(self,packet):
        with self.conn.cursor() as cur:
            cur.execute("select current_user,pg_has_role(current_user,'fixer_forward_media_photo_auditor_20261007','member'),pg_has_role(current_user,'fixer_forward_media_owner_20261006','member'),pg_has_role(current_user,'service_role','member')")
            identity=cur.fetchone()
        if identity!=(self.expected_role,True,False,False) or self.expected_role in ('service_role','anon','authenticated'):
            raise PhotoCertificateHold('independent_auditor_identity_required')
        key=self._rpc('approved_key',(packet['payload']['key_id'],))
        verified=verify_still_v2(packet,key,self._snapshot())
        with self.conn.cursor() as cur:
            cur.execute('select public.fixer_still_photo_record_v2_20261008(%s,%s,%s)',
                (verified.payload_json,verified.signature_hex,verified.receipt_ref))
            if cur.fetchone()[0] is not True:
                raise PhotoCertificateHold('certificate_receipt_staging_failed')
        return verified

    def lookup_for_owner(self,audit_id,expected_candidate):
        result=self._rpc('certificate',(str(uuid.UUID(audit_id)),))
        return verify_still_v2(result['packet'],result['approved_key'],self._snapshot(),expected_candidate)
