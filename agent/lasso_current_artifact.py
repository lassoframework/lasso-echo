"""Narrow owned LASSO current-artifact authority, independent of legacy SQL policy.

SQL remains the pairing/lease identity authority. Only actual current reviewed
artifacts establish visual-standard conformance. No client routing/approval
behavior or artifact metadata is changed here.
"""
import hashlib
import re

from . import infographic_evidence

STYLE_HOLD = 'lasso_visual_style_review_required'
ACCOUNTS = {'instagram':'lasso_ig','facebook':'lasso_fb'}


def owned(row):
    return (isinstance(row,dict) and row.get('gym_id')=='lasso'
            and row.get('account') in ACCOUNTS and row.get('format') in ('feed','story'))


def evidence_current(evidence,digest,*,story=False):
    try:
        from .lasso_visual_standard import VERSION
        if not (isinstance(evidence,dict) and re.fullmatch('[0-9a-f]{64}',str(digest or ''))
                and evidence.get('grade_status')=='PASS' and evidence.get('image_sha256')==digest
                and isinstance(evidence.get('review_response_id'),str) and evidence['review_response_id'].strip()
                and evidence.get('policy_version')==infographic_evidence.POLICY_VERSION
                and evidence.get('brain_snapshot')==infographic_evidence.brain_snapshot()
                and evidence.get('style_conformant') is True
                and evidence.get('style_violations')==[]
                and evidence.get('visual_standard_version')==VERSION):
            return False
        if story:
            dims=evidence.get('verified_dimensions') or {}
            return (type(dims.get('width')) is int and type(dims.get('height')) is int
                    and (dims['width'],dims['height'])==(1080,1920)
                    and dims.get('image_sha256')==digest
                    and evidence.get('aspect')=='9:16' and evidence.get('pixels')=='1080x1920')
        # Feed canvases may vary; a Story canvas is never a feed proof.
        if evidence.get('aspect') not in (None,'','4:5','1:1','1.91:1'): return False
        dims=evidence.get('verified_dimensions')
        if dims is None: return True
        return (isinstance(dims,dict) and type(dims.get('width')) is int
                and type(dims.get('height')) is int and dims['width']>0 and dims['height']>0
                and .8<=dims['width']/dims['height']<=1.91
                and dims.get('image_sha256')==digest)
    except (ImportError,OSError,ValueError,TypeError,KeyError):
        return False


def artifact_current(artifact,row,feed=None):
    if not owned(row) or not isinstance(artifact,dict): return False
    feed=feed or row
    if not owned(feed) or not isinstance(feed.get('id'),str) or not feed['id'].strip() or feed.get('format')!='feed' or not isinstance(feed.get('caption'),str) or not feed['caption'].strip(): return False
    if artifact.get('tenant') not in (ACCOUNTS[row['account']],'lasso') or artifact.get('image_url')!=row.get('image_url'): return False
    source={'source_id':'content_calendar:'+str(feed.get('id'))+':caption',
            'source_hash':hashlib.sha256(feed['caption'].encode()).hexdigest()}
    if artifact.get('source_identity')!=source:
        if row['format']!='story' or artifact.get('tenant')!='lasso': return False
        from .jobs.lasso_paired_story_backfill import source_identity
        if artifact.get('source_identity')!=source_identity(feed): return False
    return evidence_current(artifact.get('evidence'),artifact.get('image_sha256'),story=row['format']=='story')


def _rows(store,table,params):
    response=store._client().get(store._rest(table),params=dict(params,select='*',limit='1000'),
                headers=store._headers({'Prefer':'count=exact'}),timeout=30)
    if response.status_code not in (200,206): raise ValueError('current artifact read unavailable')
    rows=response.json();total=response.headers.get('Content-Range','').rsplit('/',1)[-1]
    if not isinstance(rows,list) or not total.isdigit() or int(total)!=len(rows) or len(rows)>=1000:
        raise ValueError('current artifact read incomplete')
    return rows


def current_artifact(store,row,feed=None):
    if not owned(row): return None
    try:
        records=_rows(store,'echo_infographic_artifacts',{'image_url':'eq.'+str(row.get('image_url') or ''),
                       'tenant':'in.('+ACCOUNTS[row['account']]+',lasso)','order':'tenant.asc,image_url.asc'})
        passing=[a for a in records if artifact_current(a,row,feed)]
        return passing[0] if len(passing)==1 else None
    except Exception: return None


_SOURCE_FIELDS=('id','gym_id','account','format','post_date','slot_index','caption','image_url',
 'source_media_url','source_media_asset_id','thumbnail_url','pillar','logical_post_id','scheduled_at')

def _same_source(left,right):
    return all(left.get(k)==right.get(k) for k in _SOURCE_FIELDS)


def current_pair(store,row,*,require_ready=True):
    """Current Style proof for both rows; historical published feed is source only.

    Exact registry is bidirectional. Existing SQL verifies pairing/occupancy;
    its old nonblank-policy check never substitutes for current artifacts.
    """
    if not owned(row) or type(row.get('slot_index')) is not int or row['slot_index'] not in (0,1,2): return False
    try:
        day=_rows(store,'content_calendar',{'gym_id':'eq.lasso','account':'eq.'+row['account'],
                    'post_date':'eq.'+row['post_date'],'variant_status':'eq.active','order':'id.asc'})
        feeds=[r for r in day if r.get('format')=='feed' and type(r.get('slot_index')) is int
               and r['slot_index']==row.get('slot_index')]
        if len(feeds)!=1: return False
        feed=feeds[0]
        if row['format']=='feed' and not _same_source(row,feed): return False
        links=_rows(store,'lasso_managed_paired_stories',{'feed_id':'eq.'+feed['id'],'order':'story_id.asc'})
        if len(links)!=1 or links[0].get('feed_id')!=feed['id']: return False
        story_id=links[0].get('story_id')
        reverse=_rows(store,'lasso_managed_paired_stories',{'story_id':'eq.'+str(story_id),'order':'feed_id.asc'})
        if len(reverse)!=1 or reverse[0].get('feed_id')!=feed['id'] or reverse[0].get('story_id')!=story_id: return False
        stories=[r for r in day if r.get('id')==story_id]
        if len(stories)!=1: return False
        story=stories[0]
        if row['format']=='story' and not _same_source(row,story): return False
        if any(story.get(k)!=feed.get(k) for k in ('gym_id','account','post_date','slot_index','pillar','logical_post_id')) or story.get('caption')!='': return False
        if story.get('source_media_url')!=story.get('image_url') or story.get('image_url')==feed.get('image_url'): return False
        story_art=current_artifact(store,story,feed)
        feed_art=current_artifact(store,feed)
        if story_art is None or feed_art is None: return False
        if not anchor_current(story_art,feed,feed_art): return False
        reader=store._client().post(store._rest('rpc/lasso_story_current_source'),
            headers=store._headers({'Content-Type':'application/json'}),json={'p_story_id':story_id},timeout=30)
        if reader.status_code!=200 or reader.json() is not True: return False
        if require_ready and feed.get('status')!='published':
            return store.lasso_paired_story_ready_for_feed(feed['id']) is True
        return True
    except Exception: return False


def feed_reference(store,feed):
    """Real current reviewed feed artifact; published rows never get rewritten."""
    artifact=current_artifact(store,feed)
    return {'feed':dict(feed),'artifact':artifact} if artifact is not None else None


def anchor_current(story_artifact,feed,feed_artifact):
    if not artifact_current(feed_artifact,feed): return False
    anchor=(story_artifact.get('evidence') or {}).get('paired_feed_reference')
    return isinstance(anchor,dict) and all(anchor.get(k)==v for k,v in {
        'feed_id':feed['id'],'image_url':feed['image_url'],
        'image_sha256':feed_artifact['image_sha256'],
        'source_hash':hashlib.sha256(feed['caption'].encode()).hexdigest(),
        'review_response_id':feed_artifact['evidence']['review_response_id']}.items())
