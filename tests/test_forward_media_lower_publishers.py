"""Scoped provider boundary tests. Fake REST authority + fake vendors only."""
import asyncio
from contextvars import copy_context
import hashlib
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest
from agent import config,forward_media_publish as bridge,forward_media_send_context as scope
from agent import meta_publisher as meta,socialapi_publisher as social,zernio_publisher as zernio
from agent import forward_media_guard as guard

DATA=b'synthetic verified image bytes'


class Authority:
    def __init__(self,row,token):
        self.row=dict(row,status='publishing',publish_claim_token=token)
        self.token=token
        self.claim_ok=True
        self.evidence_id=str(uuid4())
        self.calls=[]

    def get_row(self,*args): return dict(self.row)
    def _client(self): return self
    def _headers(self,*args): return {}
    def _rest(self,path): return path
    def post(self,path,**kwargs):
        self.calls.append(path)
        if path.endswith('fixer_claim_forward_media_20261006'):
            return SimpleNamespace(status_code=200,json=lambda:self.claim_ok)
        row=self.row
        snapshot={'calendar_row_id':row['id'],'gym_id':row['gym_id'],'tenant_id':row['gym_id'],
            'account':row['account'],'format':row['format'],'gbp_location_id':None,
            'post_date':row['post_date'],'group_key':row['visual_group_key'],
            'source_url':row['source_media_url'],'image_url':row['image_url'],'thumbnail_url':None,
            'revision':'synthetic-current-revision'}
        return SimpleNamespace(status_code=200,json=lambda:snapshot)
    def get(self,path,**kwargs):
        row=self.row
        if path=='fixer_forward_media_lineage_20261006':
            result={'evidence_id':self.evidence_id}
        elif path=='fixer_forward_media_claim_receipt_20261006':
            result={'claim_token':self.token,'calendar_row_id':row['id'],'tenant_id':row['gym_id'],
                'post_date':row['post_date'],'group_key':row['visual_group_key'],
                'source_url':row['source_media_url'],'image_url':row['image_url'],'thumbnail_url':None,
                'fingerprints':['md5:'+hashlib.md5(DATA).hexdigest()]}
        else:
            result={'tenant_id':row['gym_id'],'exact_url':row['image_url'],
                    'fingerprint':'md5:'+hashlib.md5(DATA).hexdigest(),'byte_length':len(DATA)}
        return SimpleNamespace(status_code=200,json=lambda:[result])


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD','true')
    monkeypatch.setattr(config,'publish_enabled',lambda:True)
    monkeypatch.setattr(config,'zernio_publish_enabled',lambda:True)
    monkeypatch.setattr(config,'stories_enabled',lambda:True)
    monkeypatch.setattr(config,'caption_cooldown_enabled',lambda:False)
    monkeypatch.setattr(config,'story_crosspost_enabled',lambda:False)
    from agent import publish_billing_gate
    monkeypatch.setattr(publish_billing_gate,'publishing_blocked',lambda *_:False)
    monkeypatch.setattr(meta,'_recent_duplicate',lambda *_:None)
    monkeypatch.setattr(meta,'_stamp_published',lambda *_:None)
    monkeypatch.setattr(meta,'_await_container_ready',lambda *a,**kw:None)
    monkeypatch.setattr(social,'_already_published',lambda *_:(False,''))
    monkeypatch.setattr(social.db,'socialapi_claim',lambda *_:('won',''))
    monkeypatch.setattr(social.db,'socialapi_claim_release',lambda *_:None)
    monkeypatch.setattr(social.socialapi_store,'get_account_id',lambda *_:'native-sapi-account')
    monkeypatch.setattr(social,'_fetch_bytes',lambda *_:DATA)
    monkeypatch.setattr(social,'_interpret',lambda *_:('published','social-post','','',''))
    monkeypatch.setattr(social,'_resolve',lambda *args:social.PublishResult(True,'published','social-post'))
    requests=[]
    monkeypatch.setattr(social.socialapi_client,'upload_media',lambda *a,**k:requests.append(('upload',a,k)) or 'media-id')
    monkeypatch.setattr(social.socialapi_client,'create_post',lambda *a,**k:requests.append(('social-post',a,k)) or {})
    def factory(provider,platform='instagram',fmt='feed'):
        row={'id':str(uuid4()),'gym_id':'gym','account':'facebook' if platform=='facebook_page' else 'instagram',
            'format':fmt,'post_date':'2026-10-10','caption':'Come train with us.',
            'visual_group_key':'same-content-group','image_url':'https://media.example.test/image.jpg',
            'source_media_url':'https://media.example.test/source.jpg','thumbnail_url':None}
        token=str(uuid4()); authority=Authority(row,token)
        key='gym_fb' if platform=='facebook_page' else 'gym_ig'
        account=SimpleNamespace(key=key,platform=platform,get_token=lambda:'SYNTHETIC provider token',
                                get_target_id=lambda:'native-meta-target')
        draft=SimpleNamespace(draft_id=row['id'],account_key=key,platform=platform,
            caption=row['caption'],hashtags=[],creative_public_url=row['image_url'],creative_path='',
            day_key=row['post_date'],is_story=fmt=='story',slide_urls=[])
        class Vendor:
            def post(self,path,**kwargs):
                requests.append(('meta',path,kwargs))
                return SimpleNamespace(status_code=200,json=lambda:{'id':'meta-id'})
            def list_accounts(self,profile):
                return {'accounts':[{'_id':'native-zernio-account','platform':'facebook' if platform=='facebook_page' else 'instagram'}]}
            def create_post(self,*args,**kwargs):
                requests.append(('zernio',args,kwargs))
                return {'_id':'zernio-post'}
        vendor=Vendor()
        def publish():
            if provider=='meta': return meta.publish(draft,account,http=vendor)
            if provider=='socialapi': return social.publish(draft,account,http=vendor)
            return zernio.publish(draft,account,client=vendor,profile_resolver=lambda *_:'profile',
                                  page_resolver=lambda *_:'native-fb-page')
        return row,token,authority,draft,account,vendor,publish,requests
    return factory


@pytest.mark.parametrize('provider',['meta','socialapi','zernio'])
@pytest.mark.parametrize('platform',['instagram','facebook_page'])
def test_direct_entry_holds_before_provider_for_all_routes(lane,provider,platform):
    row,token,authority,draft,account,vendor,publish,requests=lane(provider,platform)
    draft.forward_authorized=True  # A producer boolean/claim attribute is not a scope.
    draft.publish_claim_token=token
    with pytest.raises(scope.ProviderSendHold,match='scope required'):
        publish()
    assert requests==[]
    assert bridge.authorize(authority,row,token) is True
    with pytest.raises(scope.ProviderSendHold,match='scope required'):
        publish()
    assert requests==[]


@pytest.mark.parametrize('provider',['meta','socialapi','zernio'])
@pytest.mark.parametrize('platform',['instagram','facebook_page'])
@pytest.mark.parametrize('fmt',['feed','story'])
def test_explicit_current_scope_permits_one_bound_fake_send(lane,provider,platform,fmt):
    row,token,authority,draft,account,vendor,publish,requests=lane(provider,platform,fmt)
    with bridge.authorized_send(authority,row,token):
        assert publish().ok is True
        with pytest.raises(scope.ProviderSendHold,match='already used'):
            publish()
    assert requests
    if provider=='zernio': assert requests[-1][2]['idempotency_key']==token
    with pytest.raises(scope.ProviderSendHold,match='scope required'):
        publish()


@pytest.mark.parametrize('field,value',[('draft_id',str(uuid4())),('day_key','2026-10-11'),
    ('creative_public_url','https://media.example.test/other.jpg'),('caption','changed'),
    ('gym_id','foreign'),('visual_group_key','another-group'),('publish_claim_token',str(uuid4())),
    ('account_key','foreign_ig'),('slide_urls',['https://media.example.test/extra.jpg'])])
def test_outgoing_identity_or_media_change_holds(lane,field,value):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    with bridge.authorized_send(authority,row,token):
        setattr(draft,field,value)
        with pytest.raises(scope.ProviderSendHold): publish()
    assert requests==[]


def test_current_lease_recheck_rejects_db_mutation(lane):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    with bridge.authorized_send(authority,row,token):
        authority.row['publish_claim_token']=str(uuid4())
        with pytest.raises(scope.ProviderSendHold) as held: publish()
        assert held.value.definitive_no_post is True
    assert requests==[]


def test_meta_rechecks_token_after_container_poll_before_publication(lane,monkeypatch):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    def changed_after_upload(*args,**kwargs): authority.row['publish_claim_token']=str(uuid4())
    monkeypatch.setattr(meta,'_await_container_ready',changed_after_upload)
    with bridge.authorized_send(authority,row,token):
        with pytest.raises(scope.ProviderSendHold) as held: publish()
        assert held.value.definitive_no_post is False  # Prior provider attempt needs readback.
    assert len(requests)==1 and requests[0][1].endswith('/media')


def test_native_meta_target_change_is_held_before_network(lane):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    targets=iter(['native-meta-target','changed-target'])
    account.get_target_id=lambda:next(targets)
    with bridge.authorized_send(authority,row,token):
        with pytest.raises(scope.ProviderSendHold,match='target changed'): publish()
    assert requests==[]


def test_duplicate_failure_keeps_duplicate_classification(lane,monkeypatch):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    original=bridge.authorize
    def duplicate(*args): raise guard.ForwardMediaDuplicateHold('synthetic used bytes')
    monkeypatch.setattr(bridge,'authorize',duplicate)
    with pytest.raises(guard.ForwardMediaDuplicateHold) as held:
        with bridge.authorized_send(authority,row,token): publish()
    assert held.value.definitive_no_post is True
    monkeypatch.setattr(bridge,'authorize',original)
    with bridge.authorized_send(authority,row,token):
        monkeypatch.setattr(bridge,'authorize',duplicate)
        with pytest.raises(guard.ForwardMediaDuplicateHold): publish()
    assert requests==[]


def test_socialapi_byte_change_holds_before_upload(lane,monkeypatch):
    row,token,authority,draft,account,vendor,publish,requests=lane('socialapi')
    monkeypatch.setattr(social,'_fetch_bytes',lambda *_:b'overwritten hosted object')
    with bridge.authorized_send(authority,row,token):
        with pytest.raises(scope.ProviderSendHold,match='uploaded bytes'): publish()
    assert requests==[]


def test_scope_cleanup_after_exception_and_copied_thread_context(lane):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    with pytest.raises(ValueError):
        with bridge.authorized_send(authority,row,token): raise ValueError('fixture before send')
    with pytest.raises(scope.ProviderSendHold): publish()
    outcomes=[]
    with bridge.authorized_send(authority,row,token):
        copied=copy_context()
        def child():
            try: copied.run(publish)
            except scope.ProviderSendHold: outcomes.append('held')
        thread=threading.Thread(target=child); thread.start(); thread.join(3)
        assert outcomes==['held'] and requests==[]
        assert publish().ok


def test_child_async_task_cannot_inherit_send_authority(lane):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    async def run():
        with bridge.authorized_send(authority,row,token):
            async def child():
                with pytest.raises(scope.ProviderSendHold): publish()
            await asyncio.create_task(child())
            assert requests==[]
            assert publish().ok
    asyncio.run(run())


def test_meta_private_helper_cannot_send_without_active_publisher(lane):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    with bridge.authorized_send(authority,row,token):
        with pytest.raises(scope.ProviderSendHold):
            meta._publish_fb_page(vendor,account,draft,draft.caption,'SYNTHETIC token')
    assert requests==[]


def test_feed_authority_does_not_authorize_automatic_story_crosspost(lane,monkeypatch):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    monkeypatch.setattr(config,'story_crosspost_enabled',lambda:True)
    with bridge.authorized_send(authority,row,token): assert publish().ok
    assert len(requests)==2  # IG feed create+publish, zero automatic Story requests.


def test_off_keeps_existing_provider_request_and_skips_scope_reads(lane,monkeypatch):
    row,token,authority,draft,account,vendor,publish,requests=lane('zernio')
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD')
    with bridge.authorized_send(None,None,None): assert publish().ok
    assert len(requests)==1 and 'idempotency_key' not in requests[0][2]
    assert authority.calls==[]


def test_false_or_nonboolean_claim_never_creates_a_scope(lane):
    row,token,authority,draft,account,vendor,publish,requests=lane('meta')
    for result in (False,1,'true'):
        authority.claim_ok=result
        with pytest.raises(scope.ProviderSendHold):
            with bridge.authorized_send(authority,row,token): publish()
    assert requests==[]
