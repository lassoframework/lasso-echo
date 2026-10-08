"""Offline v2 helper: real signatures, staged membership, frozen thumbnail.

Database/source/host adapters are explicit isolated fixture seams. No external I/O.
"""
import copy
import hashlib
import uuid
from unittest.mock import patch

import pytest
from agent.forward_media_attester import make_still_recipe
from agent.forward_media_guard import ForwardMediaVerificationHold
from agent.forward_media_photo_certificate import canonical, digest
from agent.forward_media_prospective_photo_prepare import prepare_prospective_photo, ProspectivePhotoHold, FLAG_ENV
from agent.forward_media_still_certificate_v2 import IndependentStillPhotoAuditorV2
from agent.forward_media_source_verifier import verify_source
from tests.test_forward_media_photo_certificate import fixtures
from tests import test_forward_media_prospective_photo_prepare as legacy
from tests.test_forward_media_prospective_photo_prepare import Connection, Cursor, LOGICAL_POST
from tests.test_forward_media_source_verifier import Hosted


class StagedCursor(Cursor):
    def execute(self,query,args=None):
        if 'forward_schedule_preparation_eligible_20261008' in query:
            self._conn.queries.append((query,args));self._row=(self._conn.eligibility,)
        else:super().execute(query,args)


class StagedConnection(Connection):
    def cursor(self):return StagedCursor(self)


def case():
    snapshot,drive,data,asset,_,base_packet,_,_=legacy.ProspectivePrepareTests().setup_candidate()
    snapshot['calendar'].update(variant_status='candidate',media_not_ready_reason='forward_reservation_staged',logical_post_id=LOGICAL_POST,thumbnail_url='https://media.example.test/thumb.png')
    snapshot.update(tenant_id='gym',batch_id=str(uuid.uuid4()))
    recipe=make_still_recipe('identity',thumbnail_name='identity')
    source=verify_source(snapshot,drive,Hosted(data))
    candidate=copy.deepcopy(base_packet['payload']['candidate'])
    candidate.update(logical_post_id=LOGICAL_POST,source_receipt_ref=source.receipt_ref,
        render_recipe_digest=digest(recipe),thumbnail_url=snapshot['calendar']['thumbnail_url'],
        thumbnail_sha256='sha256:'+hashlib.sha256(data).hexdigest(),
        thumbnail_fingerprint='md5:'+hashlib.md5(data).hexdigest(),thumbnail_length=len(data))
    packet,key,corpus,private=fixtures(candidate={k:v for k,v in candidate.items() if k!='logical_post_id'})
    corpus.update(scope='published_still_images_and_derivatives',excluded_rows_count=113,
        excluded_rows_digest=digest('SYNTHETIC 113 retained video bindings'),accounted_video_rows=[
            {'history_key':'video:'+str(i),'media_kind':'reviewed_video_scope_exclusion','published_binding_ref':'SYNTHETIC video:'+str(i)} for i in range(113)])
    packet['payload'].update(schema_version=2,scope=corpus['scope'],candidate_media_kind='still_photo',
        decision='no_prior_published_still_image_or_derivative_use',candidate=candidate,
        accounted_video_digest=corpus['excluded_rows_digest'],accounted_videos=[
            {'history_key':v['history_key'],'published_binding_ref':v['published_binding_ref'],
             'disposition':'accounted_out_of_scope_video_frames_unreviewed','review_evidence_ref':'SYNTHETIC video classification'} for v in corpus['accounted_video_rows']])
    packet['signature_hex']=private.sign(canonical(packet['payload']).encode()).hex()
    conn=StagedConnection();conn.eligibility={'eligible':True,'mode':'staged','tenant_id':'gym','batch_id':snapshot['batch_id']}
    auditor=IndependentStillPhotoAuditorV2(conn,'offline-dedicated-owner')
    def rpc(name,values):
        assert name=='certificate'
        return {'packet':packet,'approved_key':key}
    return snapshot,drive,data,asset,recipe,packet,auditor,conn,rpc,corpus


def test_v2_staged_helper_retains_exact_hosted_thumbnail_and_real_signature():
    snapshot,drive,data,asset,recipe,packet,auditor,conn,rpc,corpus=case()
    class Reader(Hosted):
        def read(self,url):
            assert conn.rollbacks>=1,'read transaction crossed remote read'
            return super().read(url)
    hosted=Reader(data)
    with patch.dict('os.environ',{FLAG_ENV:'true'}),patch.object(auditor,'_rpc',side_effect=rpc),patch.object(auditor,'_snapshot',return_value=corpus),patch('agent.visual_writer_prepare._own_media_url',return_value=True):
        prepared=prepare_prospective_photo(snapshot,asset=asset,drive_reader=drive,hosted_reader=hosted,recipe=recipe,auditor=auditor,audit_id=packet['payload']['audit_id'])
    assert prepared.thumbnail_bytes==data
    assert prepared.manifest.thumbnail_url==snapshot['calendar']['thumbnail_url']
    assert prepared.certificate.payload['candidate']['logical_post_id']==LOGICAL_POST
    assert prepared.certificate.payload['schema_version']==2
    assert snapshot['calendar']['thumbnail_url'] in hosted.urls


def test_v2_staged_marker_without_persisted_membership_holds_before_remote_reads():
    snapshot,drive,data,asset,recipe,packet,auditor,conn,rpc,corpus=case()
    conn.eligibility={'eligible':False,'mode':None,'tenant_id':'gym','batch_id':None}
    hosted=Hosted(data)
    with patch.dict('os.environ',{FLAG_ENV:'true'}),pytest.raises(ProspectivePhotoHold,match='staged_membership'):
        prepare_prospective_photo(snapshot,asset=asset,drive_reader=drive,hosted_reader=hosted,recipe=recipe,auditor=auditor,audit_id=packet['payload']['audit_id'])
    assert hosted.urls==[]


def test_v2_wrong_hosted_thumbnail_holds():
    snapshot,drive,data,asset,recipe,packet,auditor,conn,rpc,corpus=case()
    class Reader(Hosted):
        def read(self,url):
            return b'wrong hosted thumbnail' if url==snapshot['calendar']['thumbnail_url'] else super().read(url)
    with patch.dict('os.environ',{FLAG_ENV:'true'}),patch.object(auditor,'_rpc',side_effect=rpc),patch.object(auditor,'_snapshot',return_value=corpus),patch('agent.visual_writer_prepare._own_media_url',return_value=True),pytest.raises(ForwardMediaVerificationHold,match='thumbnail bytes differ'):
        prepare_prospective_photo(snapshot,asset=asset,drive_reader=drive,hosted_reader=Reader(data),recipe=recipe,auditor=auditor,audit_id=packet['payload']['audit_id'])
