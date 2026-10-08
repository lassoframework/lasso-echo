"""DRAFT independent still-photo audit certificate verification, default OFF.

No key generation, credential provisioning, sends or connection factory.
An isolated auditor verifies Ed25519 before its authenticated append RPC. The
owner verifies again before using the immutable receipt. PostgreSQL trusts that
narrow authenticated verifier receipt, like the existing trusted byte attester;
it does not implement Ed25519. Approved public keys/policies/baselines require
separate administrator provisioning. Producer hashes, dHash absence, header
classification and free-form evidence references cannot create an approval.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_MD5 = re.compile(r'md5:[0-9a-f]{32}\Z')
_MAX_PACKET = 4*1024*1024
_CANDIDATE_FIELDS = frozenset({'calendar_row_id','tenant_id','group_key','post_date',
    'source_asset_id','source_url','source_fingerprint','source_sha256','source_length',
    'source_receipt_ref','image_url','image_fingerprint','image_sha256','image_length',
    'render_recipe_digest','content_digest'})


class PhotoCertificateHold(RuntimeError):
    """Static reasons only; never expose packet data, URLs or driver errors."""


def canonical(value):
    try:
        return json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,
                          allow_nan=False)
    except (ValueError,TypeError):
        raise PhotoCertificateHold('certificate_shape_invalid') from None


def digest(value):
    return 'sha256:'+hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_candidate(candidate):
    if not isinstance(candidate,dict) or set(candidate)!=_CANDIDATE_FIELDS:
        raise PhotoCertificateHold('certificate_candidate_invalid')
    for k in ('calendar_row_id',):
        if str(uuid.UUID(candidate[k])) != candidate[k]:
            raise PhotoCertificateHold('certificate_candidate_invalid')
    for k in ('source_sha256','image_sha256','render_recipe_digest','content_digest'):
        if not isinstance(candidate[k],str) or not _SHA.fullmatch(candidate[k]):
            raise PhotoCertificateHold('certificate_candidate_invalid')
    for k in ('source_fingerprint','image_fingerprint'):
        if not isinstance(candidate[k],str) or not _MD5.fullmatch(candidate[k]):
            raise PhotoCertificateHold('certificate_candidate_invalid')
    for k in ('source_length','image_length'):
        if type(candidate[k]) is not int or not 0<candidate[k]<=134217728:
            raise PhotoCertificateHold('certificate_candidate_invalid')
    for k in ('tenant_id','group_key','source_asset_id','source_receipt_ref'):
        if not isinstance(candidate[k],str) or not candidate[k] or candidate[k]!=candidate[k].strip():
            raise PhotoCertificateHold('certificate_candidate_invalid')
    for k in ('source_url','image_url'):
        if not isinstance(candidate[k],str) or not re.fullmatch(r'https://[^\s]+',candidate[k]):
            raise PhotoCertificateHold('certificate_candidate_invalid')
    from datetime import date
    if date.fromisoformat(candidate['post_date']).isoformat()!=candidate['post_date']:
        raise PhotoCertificateHold('certificate_candidate_invalid')
    return candidate


@dataclass(frozen=True)
class VerifiedPhotoCertificate:
    payload_json: str
    signature_hex: str
    receipt_ref: str

    @property
    def payload(self):
        return json.loads(self.payload_json)


def verify(packet,approved_key,snapshot,expected_candidate=None):
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
                  'disposition_digest','decision','stated_visual_uncertainty'}
        if (not isinstance(p,dict) or set(p)!=required or type(p['schema_version']) is not int
                or p['schema_version']!=1 or str(uuid.UUID(p['audit_id']))!=p['audit_id']
                or p['decision']!='reviewed_no_prior_visual_use'
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
        validate_candidate(p['candidate'])
        if expected_candidate is not None and p['candidate']!=expected_candidate:
            raise PhotoCertificateHold('certificate_candidate_changed')
        rows=snapshot.get('rows')
        dispositions=p['dispositions']
        if (not isinstance(rows,list) or not 0<=len(rows)<=2500
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
        if len(text.encode())>_MAX_PACKET:
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


class IndependentPhotoAuditor:
    """Existing explicit auditor connection only; no login/key creation.

    Authenticated database receipt records verification by this independent
    runtime. Caller owns COMMIT; unknown commit must never cause automatic retry.
    """
    def __init__(self,connection,expected_role):
        self.conn,self.expected_role=connection,expected_role

    def _rpc(self,name,values):
        with self.conn.cursor() as cur:
            cur.execute('select public.fixer_forward_media_photo_'+name+'_20261007('
                        +','.join(['%s']*len(values))+')',values)
            return cur.fetchone()[0]

    def submit(self,packet):
        with self.conn.cursor() as cur:
            cur.execute("select current_user,pg_has_role(current_user,'fixer_forward_media_photo_auditor_20261007','member'),pg_has_role(current_user,'fixer_forward_media_owner_20261006','member'),pg_has_role(current_user,'service_role','member')")
            identity=cur.fetchone()
        if identity!=(self.expected_role,True,False,False) or self.expected_role in ('service_role','anon','authenticated'):
            raise PhotoCertificateHold('independent_auditor_identity_required')
        key=self._rpc('approved_key',(packet['payload']['key_id'],))
        snapshot=self._rpc('snapshot',())
        verified=verify(packet,key,snapshot)
        if self._rpc('record',(verified.payload_json,verified.signature_hex,verified.receipt_ref)) is not True:
            raise PhotoCertificateHold('certificate_receipt_staging_failed')
        return verified

    def lookup_for_owner(self,audit_id,expected_candidate):
        """Owner re-verifies signature, complete corpus and candidate binding.

        This method is read-only; it never creates source clearance authority.
        Runtime code must check enabled gates and revalidate under final locks.
        """
        result=self._rpc('certificate',(str(uuid.UUID(audit_id)),))
        return verify(result['packet'],result['approved_key'],self._rpc('snapshot',()),expected_candidate)
