"""Actual v2 schema and transport/source race refusal; no network or credentials."""
from copy import deepcopy
import hashlib
import pytest
from agent import lasso_current_artifact as proof

@pytest.fixture
def pair(monkeypatch):
    brain={'brand_voice/lasso_visual_standard.md':'f'*64}
    monkeypatch.setattr(proof.infographic_evidence,'brain_snapshot',lambda:brain)
    feed=dict(id='feed',gym_id='lasso',account='instagram',format='feed',post_date='2026-10-09',slot_index=0,
              caption='A steady team builds a stronger gym.',image_url='https://example/feed.png',
              status='pending',variant_status='active',pillar='book',logical_post_id='logical',scheduled_at='2026-10-09T11:30:00Z')
    story=dict(feed,id='story',format='story',caption='',image_url='https://example/story.png',
               source_media_url='https://example/story.png',scheduled_at='2026-10-09T11:45:00Z')
    source={'source_id':'content_calendar:feed:caption','source_hash':hashlib.sha256(feed['caption'].encode()).hexdigest()}
    def artifact(row):
        sha=('a' if row['format']=='feed' else 'b')*64
        evidence=dict(policy_version=proof.infographic_evidence.POLICY_VERSION,brain_snapshot=brain,
             grade_status='PASS',image_sha256=sha,review_response_id='real-review-'+row['format'],
             style_conformant=True,style_violations=[],visual_standard_version='lasso-grounded-editorial-2026-10-09-v1',aspect='4:5')
        if row['format']=='story':
            evidence.update(aspect='9:16',pixels='1080x1920',verified_dimensions=dict(width=1080,height=1920,image_sha256=sha),
              paired_feed_reference=dict(feed_id='feed',image_url=feed['image_url'],image_sha256='a'*64,source_hash=source['source_hash'],review_response_id='real-review-feed'))
        return dict(tenant='lasso_ig',image_url=row['image_url'],image_sha256=sha,evidence=evidence,source_identity=source)
    return feed,story,artifact(feed),artifact(story)

@pytest.mark.parametrize('field,value',[('style_conformant',None),('style_conformant',False),('style_violations',['neon']),
 ('visual_standard_version','old'),('policy_version','old'),('brain_snapshot',{}),('review_response_id','  '),
 ('image_sha256','c'*64),('grade_status','FAIL')])
def test_feed_and_story_refuse_bad_real_evidence(pair,field,value):
    f,s,fa,sa=pair
    for row,art in ((f,fa),(s,sa)):
        art=deepcopy(art);art['evidence'][field]=value
        assert not proof.artifact_current(art,row,f)

@pytest.mark.parametrize('change',[{'image_url':'https://wrong'}, {'source_identity':{}}, {'tenant':'client'}, {'image_sha256':'x'}])
def test_url_uuid_caption_digest_and_tenant_bound(pair,change):
    f,s,fa,sa=pair
    fa.update(change)
    assert not proof.artifact_current(fa,f)

def test_story_measured_hash_and_feed_canvas(pair):
    f,s,fa,sa=pair
    assert proof.artifact_current(fa,f) and proof.artifact_current(sa,s,f)
    sa['evidence']['verified_dimensions']['image_sha256']='c'*64
    assert not proof.artifact_current(sa,s,f)
    fa['evidence']['aspect']='9:16'
    assert not proof.artifact_current(fa,f)

class Reply:
    status_code=200
    def __init__(self,data):
        self.data=data; self.headers={'Content-Range':f'0-0/{len(data)}'} if isinstance(data,list) else {}
    def json(self):return self.data
class Store:
    def __init__(self,pair):self.f,self.s,self.fa,self.sa=deepcopy(pair);self.links=[dict(feed_id='feed',story_id='story')]
    def _client(self):return self
    def _rest(self,t):return t
    def _headers(self,h):return h
    def get(self,t,params,**kw):
        if t=='content_calendar':return Reply([self.f,self.s])
        if t=='lasso_managed_paired_stories':return Reply(self.links)
        return Reply([a for a in (self.fa,self.sa) if 'eq.'+a['image_url']==params['image_url']])
    def post(self,*a,**kw):return Reply(True)
    def lasso_paired_story_ready_for_feed(self,*a):return True

@pytest.mark.parametrize('mutation',['anchor_url','anchor_hash','anchor_source','duplicate_registry','feed_source_race','old_feed_policy','missing_anchor'])
def test_legacy_sql_true_never_certifies_stale_pair(pair,mutation):
    st=Store(pair);f,s,*_=pair
    assert proof.current_pair(st,f) and proof.current_pair(st,s)
    if mutation=='anchor_url':st.sa['evidence']['paired_feed_reference']['image_url']='https://old'
    if mutation=='anchor_hash':st.sa['evidence']['paired_feed_reference']['image_sha256']='c'*64
    if mutation=='anchor_source':st.sa['evidence']['paired_feed_reference']['source_hash']='d'*64
    if mutation=='duplicate_registry':st.links*=2
    if mutation=='feed_source_race':st.f['image_url']='https://new'
    if mutation=='old_feed_policy':st.fa['evidence']['policy_version']='old'
    if mutation=='missing_anchor':del st.sa['evidence']['paired_feed_reference']
    assert not proof.current_pair(st,f)
    assert not proof.current_pair(st,s)

def test_published_source_still_needs_genuine_current_review(pair):
    st=Store(pair);st.f['status']='published';st.fa['evidence']['policy_version']='old'
    assert not proof.current_pair(st,st.s)

def test_partial_artifact_transport_fail_closed(pair):
    st=Store(pair)
    old=st.get
    def get(*a,**kw):
        r=old(*a,**kw);r.headers={'Content-Range':'0-0/999'};return r
    st.get=get
    assert not proof.current_pair(st,st.f)
