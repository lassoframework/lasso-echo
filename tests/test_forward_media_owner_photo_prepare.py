"""Offline tests for isolated exact-byte/signature owner preparation."""
import copy
import hashlib
import unittest
import uuid
from unittest.mock import patch

from agent.forward_media_owner_photo_prepare import prepare_remote_photo,stage_prepared_photo,reconcile_owner_photo
from agent.forward_media_owner import ForwardMediaOwnerPersistence
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
    def rollback(self):pass


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

    def test_signed_transformed_thumbnail_and_image_alias_prepare_exact_retained_bytes(self):
        from agent.forward_media_attester import replay_still_recipe
        for stage in ('feed_autofit_4x5','delivered_image'):
            snap,drive,data,_,packet,auditor,_=self.setup_candidate()
            recipe=make_still_recipe('identity',thumbnail_name=stage)
            thumb=replay_still_recipe(data,recipe,has_thumbnail=True)['thumbnail_bytes']
            url=URL if stage=='delivered_image' else 'https://media.example.test/thumb.png'
            snap['calendar']['thumbnail_url']=url
            candidate={**packet['payload']['candidate'],'render_recipe_digest':digest(recipe),
                'thumbnail_url':url,'thumbnail_sha256':'sha256:'+hashlib.sha256(thumb).hexdigest(),
                'thumbnail_fingerprint':'md5:'+hashlib.md5(thumb).hexdigest(),'thumbnail_length':len(thumb)}
            packet,key,corpus,_=fixtures(candidate=candidate)
            def rpc(name,values):
                return {'packet':packet,'approved_key':key} if name=='certificate' else corpus
            class Reader(Hosted):
                def read(self,url_now):
                    self.urls.append(url_now)
                    return thumb if url_now==url else data
            reader=Reader()
            with patch.object(auditor,'_rpc',side_effect=rpc),patch('agent.visual_writer_prepare._own_media_url',return_value=True):
                prepared=prepare_remote_photo(snap,drive_reader=drive,hosted_reader=reader,recipe=recipe,
                    auditor=auditor,audit_id=packet['payload']['audit_id'])
            self.assertEqual(prepared.thumbnail_bytes,thumb)
            self.assertEqual(prepared.manifest.thumbnail_url,url)
            self.assertEqual(prepared.manifest.thumbnail_fingerprint,candidate['thumbnail_fingerprint'])
            # Alias consumes the already read delivered image; no third read.
            self.assertEqual(reader.urls.count(url),2 if stage=='delivered_image' else 1)

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

    def test_unsigned_thumbnail_and_sent_candidates_remain_held(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate()
        snap['calendar']['thumbnail_url']='https://media.example.test/thumb.png'
        with self.assertRaisesRegex(Exception,'thumbnail recipe binding unavailable'):
            prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,auditor=auditor,audit_id=packet['payload']['audit_id'])
        snap['calendar']['thumbnail_url']=None;snap['calendar']['status']='published'
        with self.assertRaisesRegex(PhotoCertificateHold,'not_unsent'):
            prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),recipe=recipe,auditor=auditor,audit_id='unused')

    def test_untyped_fake_positive_callback_cannot_stage(self):
        with self.assertRaisesRegex(PhotoCertificateHold,'dedicated_prepared_owner_photo_required'):
            stage_prepared_photo(object(),object())

    def test_stage_signed_sibling_preserves_original_anchor_and_rejects_source_change(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate()
        with patch.object(auditor,'_rpc',side_effect=rpc):
            prepared=prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),
                recipe=recipe,auditor=auditor,audit_id=packet['payload']['audit_id'])
        canonical_original={**prepared.source.original.row(),'registry_evidence_ref':'SYNTHETIC first original receipt'}
        clearance={**canonical_original,'decision':'cleared_unused','history_evidence_ref':'SYNTHETIC first clearance'}
        class ClearanceCursor(Cursor):
            def fetchone(self):return (clearance,)
        class ClearanceConnection(Connection):
            def cursor(self):return ClearanceCursor()
        persistence=ForwardMediaOwnerPersistence(ClearanceConnection(),'offline_owner',Hosted(data))
        with patch.object(persistence,'_assert_owner_identity'), \
             patch.object(ForwardMediaOwnerPersistence,'persist_in_transaction') as persist:
            stage_prepared_photo(persistence,prepared)
            self.assertEqual(persist.call_args.args[0].row(),canonical_original)
            self.assertEqual(persist.call_args.args[1].row(),clearance)
            self.assertEqual(persist.call_args.args[2],prepared.manifest)
            clearance['source_url']='https://media.example.test/other-original.png'
            with self.assertRaisesRegex(PhotoCertificateHold,'original_anchor_mismatch'):
                stage_prepared_photo(persistence,prepared)
            self.assertEqual(persist.call_count,1)

    def test_existing_grant_reconciliation_checks_real_signature_and_outcome(self):
        snap,drive,data,recipe,packet,auditor,rpc=self.setup_candidate()
        with patch.object(auditor,'_rpc',side_effect=rpc):
            prepared=prepare_remote_photo(snap,drive_reader=drive,hosted_reader=Hosted(data),
                recipe=recipe,auditor=auditor,audit_id=packet['payload']['audit_id'])
        key=rpc('certificate',())['approved_key']
        anchor_packet,anchor_key,_,_=fixtures(candidate=packet['payload']['candidate'])
        from agent.forward_media_photo_certificate import canonical
        anchor_ref='photo-audit:sha256:'+hashlib.sha256((canonical(anchor_packet['payload'])+'\n'+anchor_packet['signature_hex']).encode()).hexdigest()
        result={'registry':prepared.source.original.row(),'manifest':prepared.manifest.row(),
            'clearance':{'history_evidence_ref':'owner-photo-reservation:'+anchor_ref},
            'replayed':True,'progress':{'state':'final','outcome':{'status':'persisted'}},
            'certificate':{'packet':packet,'approved_key':key},
            'clearance_certificate':{'packet':anchor_packet,'approved_key':anchor_key}}
        class ExistingCursor(Cursor):
            def fetchone(self):return (result,)
        class ExistingConnection(Connection):
            def cursor(self):return ExistingCursor()
        persistence=ForwardMediaOwnerPersistence(ExistingConnection(),'offline_owner',Hosted(data))
        with patch.object(persistence,'_assert_owner_identity'):
            readback=reconcile_owner_photo(persistence,packet['payload']['audit_id'])
            self.assertEqual(readback['progress']['state'],'final')
            anchor_signature=anchor_packet['signature_hex']
            anchor_packet['signature_hex']='00'*64
            with self.assertRaisesRegex(PhotoCertificateHold,'existing_photo_signature_or_identity_invalid'):
                reconcile_owner_photo(persistence,packet['payload']['audit_id'])
            anchor_packet['signature_hex']=anchor_signature
            packet['signature_hex']='00'*64
            with self.assertRaisesRegex(PhotoCertificateHold,'existing_photo_signature_or_identity_invalid'):
                reconcile_owner_photo(persistence,packet['payload']['audit_id'])

if __name__=='__main__':unittest.main()
