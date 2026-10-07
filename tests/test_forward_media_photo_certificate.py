import copy
import hashlib
import unittest
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from agent.forward_media_photo_certificate import verify,canonical,digest,PhotoCertificateHold


def fixtures(candidate=None,snapshot=None,private=None):
    private=private or Ed25519PrivateKey.generate()  # SYNTHETIC test key only.
    key={'key_id':'synthetic-key','auditor_id':'synthetic-independent-reviewer',
         'policy_id':'synthetic-policy','approved':True,
         'public_key_hex':private.public_key().public_bytes_raw().hex()}
    snapshot=snapshot or {'baseline_id':str(uuid.uuid4()),'policy_id':key['policy_id'],
        'policy_approved':True,'scope_complete':True,'generation':0,
        'spine_digest':digest('synthetic complete corpus'),
        'rows':[{'history_key':'synthetic-history','resolved':True,'media_kind':'still_photo',
            'visual_sha256':digest('synthetic inspected historical photo'),
            'published_binding_ref':'SYNTHETIC publication receipt'}]}
    candidate=candidate or {'calendar_row_id':str(uuid.uuid4()),'tenant_id':'gym',
        'group_key':'group','post_date':'2026-10-10','source_asset_id':'source-asset',
        'source_url':'https://media.example.test/source.png','image_url':'https://media.example.test/image.png',
        'source_fingerprint':'md5:'+hashlib.md5(b'source').hexdigest(),
        'image_fingerprint':'md5:'+hashlib.md5(b'image').hexdigest(),
        'source_sha256':'sha256:'+hashlib.sha256(b'source').hexdigest(),
        'image_sha256':'sha256:'+hashlib.sha256(b'image').hexdigest(),
        'source_length':6,'image_length':5,'source_receipt_ref':'source-receipt:sha256:'+'a'*64,
        'render_recipe_digest':digest('synthetic recipe'),'content_digest':digest('synthetic exact row content')}
    dispositions=[{'history_key':r['history_key'],'disposition':'reviewed_visual_nonmatch',
        'inspected_sha256':r['visual_sha256'],'published_binding_ref':r['published_binding_ref'],
        'review_evidence_ref':'SYNTHETIC independent per-object visual inspection'} for r in snapshot['rows']]
    p={'schema_version':1,'audit_id':str(uuid.uuid4()),'auditor_id':key['auditor_id'],
       'key_id':key['key_id'],'policy_id':key['policy_id'],'baseline_id':snapshot['baseline_id'],
       'generation':snapshot['generation'],'spine_digest':snapshot['spine_digest'],
       'candidate':candidate,'dispositions':dispositions,'disposition_digest':digest(dispositions),
       'decision':'reviewed_no_prior_visual_use',
       'stated_visual_uncertainty':'SYNTHETIC visual review judgment; not cryptographic proof of nonreuse'}
    return {'payload':p,'signature_hex':private.sign(canonical(p).encode()).hex()},key,snapshot,private


class CertificateTests(unittest.TestCase):
    def test_real_ed25519_signature_accepts_exact_candidate_and_complete_review(self):
        packet,key,snapshot,_=fixtures()
        v=verify(packet,key,snapshot,packet['payload']['candidate'])
        self.assertTrue(v.receipt_ref.startswith('photo-audit:sha256:'))
        self.assertEqual(v.payload,packet['payload'])

    def test_signature_tamper_and_wrong_approved_public_key_reject(self):
        packet,key,snapshot,_=fixtures()
        packet['payload']['candidate']['image_url']='https://media.example.test/substituted.png'
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_signature_invalid'):
            verify(packet,key,snapshot)
        packet,key,snapshot,_=fixtures()
        key['public_key_hex']=Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_signature_invalid'):
            verify(packet,key,snapshot)

    def test_unapproved_key_or_policy_reject(self):
        packet,key,snapshot,_=fixtures()
        key['approved']=False
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_signer_unapproved'):
            verify(packet,key,snapshot)
        key['approved']=True
        snapshot['policy_approved']=False
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_corpus_stale_or_unapproved'):
            verify(packet,key,snapshot)

    def test_unknown_or_video_without_explicit_resolved_scope_reject(self):
        packet,key,snapshot,_=fixtures()
        snapshot['rows'][0]['resolved']=False
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_history_unresolved_or_matching'):
            verify(packet,key,snapshot)
        snapshot['rows'][0]['resolved']=True
        snapshot['rows'][0]['media_kind']='video'
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_history_unresolved_or_matching'):
            verify(packet,key,snapshot)

    def test_stale_generation_or_corpus_reject(self):
        packet,key,snapshot,_=fixtures()
        snapshot['generation']=1
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_corpus_stale_or_unapproved'):
            verify(packet,key,snapshot)
        snapshot['generation']=0
        snapshot['spine_digest']=digest('changed corpus')
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_corpus_stale_or_unapproved'):
            verify(packet,key,snapshot)

    def test_no_dhash_absence_or_sparse_review_can_approve(self):
        packet,key,snapshot,private=fixtures()
        packet['payload']['dispositions'][0]['disposition']='dhash_no_match'
        packet['payload']['disposition_digest']=digest(packet['payload']['dispositions'])
        packet['signature_hex']=private.sign(canonical(packet['payload']).encode()).hex()
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_history_unresolved_or_matching'):
            verify(packet,key,snapshot)
        packet['payload']['dispositions']=[]
        packet['payload']['disposition_digest']=digest([])
        with self.assertRaisesRegex(PhotoCertificateHold,'certificate_dispositions_incomplete'):
            verify(packet,key,snapshot)

    def test_candidate_date_group_tenant_or_output_changes_reject(self):
        packet,key,snapshot,_=fixtures()
        for field,value in [('tenant_id','other'),('group_key','other'),('post_date','2026-10-11'),
                            ('image_sha256',digest('different output'))]:
            candidate=copy.deepcopy(packet['payload']['candidate'])
            candidate[field]=value
            with self.assertRaisesRegex(PhotoCertificateHold,'certificate_candidate_changed'):
                verify(packet,key,snapshot,candidate)


if __name__=='__main__':
    unittest.main()
