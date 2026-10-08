"""DRAFT independent still-photo audit certificate verification, default OFF.

No key generation, credential provisioning, sends or connection factory.
An isolated auditor verifies Ed25519 before its authenticated append RPC. The
owner verifies again before using the immutable receipt. PostgreSQL trusts that
narrow authenticated verifier receipt, like the existing trusted byte attester;
it does not implement Ed25519. Approved public keys/policies/baselines require
separate administrator provisioning. Producer hashes, dHash absence, header
classification and free-form evidence references cannot create an approval.

Versioned corpus contract (schema_version 1, the only accepted version):
- Still photos only. Any corpus row whose media_kind is not 'still_photo'
  (video, carousel, unknown) holds the packet; such rows can never be silently
  omitted because dispositions must cover every snapshot row exactly and the
  signed spine_digest binds the exact reviewed corpus generation.
- At most MAX_CORPUS_ROWS rows and MAX_PACKET_BYTES canonical payload bytes;
  anything larger fails closed, so an oversized or truncated corpus can never
  be partially approved. A larger corpus requires a future reviewed
  schema_version, never truncation.
- Absence from the bounded negative-only source-history index
  (forward_media_source_history) is not evidence of nonuse and plays no part in
  acceptance here; only the complete signed corpus plus exact candidate binding
  and an approved independent signature verify.
- Exclusion evidence: the frozen snapshot RPC omits baseline excluded_rows_json
  (video rows under a reviewed exclusion) from 'rows', so dispositions can
  never cover them. schema_version 1 therefore accepts a corpus only when the
  snapshot carries SQL-computed proof of ZERO exclusions: integer
  excluded_rows_count == 0 and excluded_rows_digest equal to the canonical
  digest of the empty array (both emitted by
  fixer_forward_media_photo_snapshot_exclusion_20261008). Old-format snapshots
  missing these fields, non-integer or positive counts and digest mismatches
  all fail closed (certificate_exclusions_unaccounted); accepting excluded
  history requires a future reviewed frame-aware schema_version, never a skip.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_MD5 = re.compile(r'md5:[0-9a-f]{32}\Z')
CORPUS_SCHEMA_VERSION = 1
MAX_CORPUS_ROWS = 2500
MAX_PACKET_BYTES = 4*1024*1024
_CANDIDATE_FIELDS = frozenset({'calendar_row_id','tenant_id','group_key','post_date',
    'source_asset_id','source_url','source_fingerprint','source_sha256','source_length',
    'source_receipt_ref','image_url','image_fingerprint','image_sha256','image_length',
    'render_recipe_digest','content_digest'})
_THUMBNAIL_FIELDS = frozenset({'thumbnail_url', 'thumbnail_fingerprint',
    'thumbnail_sha256', 'thumbnail_length'})


def zero_exclusion_digest():
    """Canonical digest of an empty excluded_rows_json array ('[]')."""
    return digest([])


def require_zero_exclusions(snapshot):
    """Fail-closed schema_version 1 exclusion gate (see module docstring)."""
    if (not isinstance(snapshot, dict)
            or type(snapshot.get('excluded_rows_count')) is not int
            or snapshot['excluded_rows_count'] != 0
            or snapshot.get('excluded_rows_digest') != zero_exclusion_digest()):
        raise PhotoCertificateHold('certificate_exclusions_unaccounted')


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
    if not isinstance(candidate,dict) or set(candidate) not in (_CANDIDATE_FIELDS, _CANDIDATE_FIELDS | _THUMBNAIL_FIELDS):
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
    if _THUMBNAIL_FIELDS <= set(candidate):
        values = [candidate[k] for k in _THUMBNAIL_FIELDS]
        if not all(value is None for value in values):
            if (not isinstance(candidate['thumbnail_url'], str)
                    or not re.fullmatch(r'https://[^\s]+', candidate['thumbnail_url'])
                    or not isinstance(candidate['thumbnail_sha256'], str)
                    or not _SHA.fullmatch(candidate['thumbnail_sha256'])
                    or not isinstance(candidate['thumbnail_fingerprint'], str)
                    or not _MD5.fullmatch(candidate['thumbnail_fingerprint'])
                    or type(candidate['thumbnail_length']) is not int
                    or not 0 < candidate['thumbnail_length'] <= 134217728):
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
                or p['schema_version']!=CORPUS_SCHEMA_VERSION or str(uuid.UUID(p['audit_id']))!=p['audit_id']
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
        require_zero_exclusions(snapshot)
        validate_candidate(p['candidate'])
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


def reconcile_review_evidence(evidence, snapshot, expected_candidate=None):
    """Pure offline pre-signing reconciliation; NEVER a clearance or authority.

    Checks that an independent review packet (the shape produced by
    tools/historical_photo_review_packet.py, decision 'review_packet_only')
    covers EXACTLY the signed corpus snapshot: same history keys, every row a
    resolved still photo with a 'reviewed_nonmatch' disposition bound to the
    row's visual_sha256 via reachable delivered bytes, zero unresolved or
    frame-held visuals, and zero snapshot exclusions. When expected_candidate
    is given, the packet's candidate block must bind the exact certificate
    candidate fields (identity, source object, and the all-or-nothing
    thumbnail object). Success returns a small fact summary an independent
    auditor may use while signing; it creates no eligibility by itself and
    verify() re-checks everything against the signed packet anyway. Any
    ambiguity raises PhotoCertificateHold (fail closed, static reasons only).
    """
    require_zero_exclusions(snapshot)
    rows = snapshot.get('rows')
    if not isinstance(rows, list) or not 0 <= len(rows) <= MAX_CORPUS_ROWS:
        raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    if (not isinstance(evidence, dict)
            or evidence.get('schema_version') != CORPUS_SCHEMA_VERSION
            or evidence.get('packet_kind') != 'historical_photo_review_evidence'
            or evidence.get('decision') != 'review_packet_only'
            or evidence.get('clearance') is not False
            or evidence.get('no_automatic_positive_decision') is not True
            or evidence.get('packet_status') != 'complete_for_independent_review'):
        raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    coverage = evidence.get('coverage')
    if (not isinstance(coverage, dict)
            or coverage.get('unresolved_visuals') != 0
            or coverage.get('byte_unreachable_visuals') != 0
            or coverage.get('frames_held_visuals') != 0):
        raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    visuals = evidence.get('visuals')
    if not isinstance(visuals, list) or len(visuals) != len(rows):
        raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    row_keys = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get('history_key'), str):
            raise PhotoCertificateHold('review_evidence_reconciliation_failed')
        row_keys.append(r['history_key'])
    if len(set(row_keys)) != len(row_keys):
        raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    indexed = {}
    for v in visuals:
        if not isinstance(v, dict) or v.get('history_key') in indexed:
            raise PhotoCertificateHold('review_evidence_reconciliation_failed')
        indexed[v.get('history_key')] = v
    if set(indexed) != set(row_keys):
        raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    for r in rows:
        v = indexed[r['history_key']]
        delivered = v.get('delivered')
        if (r.get('resolved') is not True or r.get('media_kind') != 'still_photo'
                or v.get('media_kind') != 'still_photo'
                or v.get('disposition') != 'reviewed_nonmatch'
                or v.get('unresolved_reasons') is not None
                or not isinstance(v.get('review_evidence_ref'), str)
                or not v['review_evidence_ref'].strip()
                or not isinstance(delivered, dict)
                or delivered.get('reachable') is not True
                or delivered.get('sha256') != r.get('visual_sha256')):
            raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    if expected_candidate is not None:
        validate_candidate(expected_candidate)
        c = evidence.get('candidate')
        if not isinstance(c, dict):
            raise PhotoCertificateHold('review_evidence_reconciliation_failed')
        for k in ('calendar_row_id', 'tenant_id', 'group_key', 'post_date',
                  'source_asset_id', 'source_receipt_ref'):
            if c.get(k) != expected_candidate.get(k):
                raise PhotoCertificateHold('review_evidence_reconciliation_failed')
        source = c.get('source')
        if (not isinstance(source, dict)
                or source.get('url') != expected_candidate.get('source_url')
                or source.get('sha256') != expected_candidate.get('source_sha256')
                or source.get('length') != expected_candidate.get('source_length')):
            raise PhotoCertificateHold('review_evidence_reconciliation_failed')
        thumbnail = c.get('thumbnail')
        if 'thumbnail_url' in expected_candidate:
            if (not isinstance(thumbnail, dict)
                    or thumbnail.get('url') != expected_candidate.get('thumbnail_url')
                    or thumbnail.get('sha256') != expected_candidate.get('thumbnail_sha256')
                    or thumbnail.get('length') != expected_candidate.get('thumbnail_length')):
                raise PhotoCertificateHold('review_evidence_reconciliation_failed')
        elif thumbnail is not None:
            raise PhotoCertificateHold('review_evidence_reconciliation_failed')
    return {'status': 'reconciled_for_independent_signing', 'visuals': len(visuals),
            'corpus_digest': evidence.get('corpus_digest')}


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

    def _snapshot(self):
        """Exclusion-aware snapshot RPC only, never the frozen 20261007 one.

        The frozen snapshot RPC omits baseline excluded_rows_json from 'rows'
        and carries no exclusion count, so its output can never satisfy
        verify's zero-exclusion gate. If the additive
        fixer_forward_media_photo_snapshot_exclusion_20261008 wrapper is not
        applied this call errors and the caller holds — never fail open."""
        with self.conn.cursor() as cur:
            cur.execute('select public.fixer_forward_media_photo_snapshot_exclusion_20261008()')
            row = cur.fetchone()
            return row[0] if row else None

    def submit(self,packet):
        with self.conn.cursor() as cur:
            cur.execute("select current_user,pg_has_role(current_user,'fixer_forward_media_photo_auditor_20261007','member'),pg_has_role(current_user,'fixer_forward_media_owner_20261006','member'),pg_has_role(current_user,'service_role','member')")
            identity=cur.fetchone()
        if identity!=(self.expected_role,True,False,False) or self.expected_role in ('service_role','anon','authenticated'):
            raise PhotoCertificateHold('independent_auditor_identity_required')
        key=self._rpc('approved_key',(packet['payload']['key_id'],))
        snapshot=self._snapshot()
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
        return verify(result['packet'],result['approved_key'],self._snapshot(),expected_candidate)
