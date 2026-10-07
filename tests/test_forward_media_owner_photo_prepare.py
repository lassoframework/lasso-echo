"""Offline tests for isolated exact-byte/signature owner preparation."""
import copy
import hashlib
import unittest
import uuid
from unittest.mock import patch

from agent.forward_media_owner_photo_prepare import prepare_remote_photo,stage_prepared_photo
from agent.forward_media_photo_certificate import IndependentPhotoAuditor,PhotoCertificateHold,digest
from agent.forward_media_source_verifier import verify_source
from agent.forward_media_attester import make_still_recipe
from tests.test_forward_media_photo_certificate import fixtures
from tests.test_forward_media_source_verifier import Drive,Hosted,FILE,FOLDER,URL
from tests.test_forward_media_owner_two_phase_pg import png


class Cursor:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,*args):pass
    def fetchone(self):return ('sha256:'+'a'*64,)


class Connection:
    def cursor(self):return Cursor()


class OwnerPhotoTests(unittest.TestCase):
    def setup_candidate(self):
        data=png('blue');drive=Drive();drive.data=data
        drive.meta['size']=str(len(data));drive.meta['md5Checksum']=hashlib.md5(data).hexdigest()
        snapshot={'calendar':{'id':str(uuid.uuid4()),'gym_id':'gym','source_media_asset_id':FILE,
            'source_media_url':URL,'image_url':URL,'thumbnail_url':None,'visual_group_key':'group',
            'post_date':'2026-10-10','status':'approved','variant_status':'active'},
            'asset':{'id':FILE,'gym_id':'gym','source_id':'source'},
            'source':{'id':'source','gym_id':'gym','kind':'gym_drive','active':True,'folder_id':FOLDER},
            'revision':'0'*32,'binding_revision':'1'*32}
        source=verify_source(snapshot,drive,Hosted(data));recipe=make_still_recipe('identity')
        candidate={'calendar_row_id':snapshot['calendar']['id'],'tenant_id':'gym','group_key':'group',
            'post_date':'2026-10-10','source_asset_id':FILE,'source_url':URL,'image_url':URL,
            'source_fingerprint':source.original.source_fingerprint,'source_sha256':source.evidence['source_sha256'],
            'source_length':len(data),'source_receipt_ref':source.receipt_ref,
            'image_fingerprint':source.original.source_fingerprint,'image_sha256':source.evidence['source_sha256'],
            'image_length':len(data),'render_recipe_digest':digest(recipe),'content_digest':'sha256:'+'a'*64}
        packet,key,corpus,_=fixtures(candidate=candidate)
        auditor=IndependentPhotoAuditor(Connection(),'offline-dedicated-owner')
        def rpc(name,values):
            return {'packet':packet,'approved_key':key} if name=='certificate' else corpus
        return snapshot,drive,data,recipe,packet,auditor,rpc

    def test_exact_source_hosted_recipe_and_real_ed25519_prepare(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate()
        with patch.object(auditor,'_rpc',side_effect=rpc):
            result=prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,
                auditor=auditor,audit_id=packet['payload']['audit_id'])
        self.assertEqual(result.image_bytes,data)
        self.assertEqual(result.manifest.render_evidence_ref,result.certificate.receipt_ref)
        self.assertEqual(result.manifest.render_recipe,recipe)

    def test_tampered_signature_is_not_a_positive_owner_decision(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate();packet['signature_hex']='00'*64
        with patch.object(auditor,'_rpc',side_effect=rpc),self.assertRaisesRegex(PhotoCertificateHold,'signature_invalid'):
            prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,
                auditor=auditor,audit_id=packet['payload']['audit_id'])

    def test_output_recipe_or_candidate_drift_holds(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate()
        snap['calendar']['visual_group_key']='different signed group'
        with patch.object(auditor,'_rpc',side_effect=rpc),self.assertRaisesRegex(PhotoCertificateHold,'candidate_changed'):
            prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,
                auditor=auditor,audit_id=packet['payload']['audit_id'])

    def test_thumbnail_and_sent_candidates_remain_held(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate()
        snap['calendar']['thumbnail_url']='https://media.example.test/thumb.png'
        with self.assertRaisesRegex(PhotoCertificateHold,'thumbnail_candidate_contract_missing'):
            prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,auditor=auditor,audit_id='unused')
        snap['calendar']['thumbnail_url']=None;snap['calendar']['status']='published'
        with self.assertRaisesRegex(PhotoCertificateHold,'not_unsent'):
            prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,auditor=auditor,audit_id='unused')

    def test_untyped_fake_positive_callback_cannot_stage(self):
        with self.assertRaisesRegex(PhotoCertificateHold,'dedicated_prepared_owner_photo_required'):
            stage_prepared_photo(object(),object())

if __name__=='__main__':unittest.main()
