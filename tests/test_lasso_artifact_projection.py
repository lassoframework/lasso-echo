"""PostgREST projection regression: unselected proof fields do not exist on wire."""
from copy import deepcopy
from agent.jobs import lasso_held_media_repair as repair, lasso_daily_paired_stories as daily
from agent import variant_regen
from test_lasso_current_artifact import pair

class Reply:
    status_code=200
    def __init__(self,payload):
        self.payload=payload
        self.headers={'Content-Range':f'0-0/{len(payload)}'} if isinstance(payload,list) else {}
    def json(self):return deepcopy(self.payload)

class ProjectedStore:
    def __init__(self,row,artifacts):self.row=deepcopy(row);self.artifacts=deepcopy(artifacts);self.selects=[];self.swaps=[];self.returned=[]
    def _client(self):return self
    def _rest(self,name):return name
    def _headers(self,extra=None):return extra or {}
    def get_row(self,*a):return deepcopy(self.row)
    def rows_in_range_complete(self,*a,**kw):return [deepcopy(self.row)]
    def list_pending_media_between(self,*a):return [deepcopy(self.row)]
    def get(self,name,params,**kwargs):
        assert name=='echo_infographic_artifacts'
        rows=deepcopy(self.artifacts)
        if params.get('tenant','').startswith('eq.'):
            rows=[a for a in rows if 'eq.'+a['tenant']==params['tenant']]
        for field in ('source_id','source_hash'):
            if 'source_identity->>'+field in params:
                rows=[a for a in rows if 'eq.'+a['source_identity'][field]==params['source_identity->>'+field]]
        if params.get('image_url','').startswith('eq.'):
            rows=[a for a in rows if 'eq.'+a['image_url']==params['image_url']]
        fields=params.get('select','*');self.selects.append(fields)
        if fields!='*':rows=[{k:a[k] for k in fields.split(',') if k in a} for a in rows]
        rows=rows[:int(params.get('limit','1000'))]
        self.returned.append(deepcopy(rows))
        return Reply(rows)
    def post(self,name,json,**kwargs):
        assert name=='rpc/replace_lasso_style_feed_media_20261009'
        assert json['p_expected']==self.row and json['p_sha']==self.artifacts[0]['image_sha256']
        self.swaps.append(deepcopy(json))
        self.row.update(image_url=json['p_new_url'],source_media_url=json['p_new_url'],
           source_media_asset_id=None,thumbnail_url=None,media_not_ready_reason=None)
        return Reply({'result':'replaced','row':self.row})

class Leases:
    available=True
    def __init__(self):self.claimed=[];self.released=[]
    def claim(self,*args):self.claimed.append(args);return True
    def release(self,*args):self.released.append(args)


def test_genuine_cached_feed_reused_and_repaired_without_paid_regeneration(pair,monkeypatch):
    f,s,fa,sa=pair
    row={key:None for key in repair._CAS_COLUMNS};row.update(f,media_not_ready_reason=repair.STYLE_HOLD_REASON)
    fa['evidence']['brief_model']='gpt-6-astra'
    store=ProjectedStore(row,[fa]);leases=Leases()
    monkeypatch.setattr(repair.config,'lasso_three_feed_enabled',lambda:True)
    monkeypatch.setattr(repair.config,'lasso_infographic_quality_enabled',lambda *a:True)
    monkeypatch.setattr(variant_regen,'enabled',lambda:True)
    monkeypatch.setattr(repair.visual_writer_prepare,'enabled',lambda:False)
    monkeypatch.setattr(variant_regen,'generate_variant_image',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('paid duplicate')))
    out=repair.run(now='2026-10-09T00:00:00-04:00',store=store,artifact_store=leases)
    assert out['reused']==out['repaired']==1 and out['generated']==out['errors']==0
    assert len(leases.claimed)==len(leases.released)==len(store.swaps)==1
    assert store.row['caption']==f['caption'] and store.row['media_not_ready_reason'] is None
    projection=store.selects[0].split(',')
    assert {'tenant','image_sha256','image_url','evidence','source_identity'}<=set(projection)


def test_paired_story_candidate_wire_keeps_exact_digest_and_tenant(pair):
    f,s,fa,sa=pair
    store=ProjectedStore(f,[sa]);source=sa['source_identity']
    found=daily._candidate_artifact(store,'lasso_ig',source['source_id'],source['source_hash'],{'feed':f,'artifact':fa})
    assert found is not None and found['image_sha256']==sa['image_sha256'] and found['tenant']=='lasso_ig'
    assert set(store.selects[0].split(','))=={'tenant','image_url','image_sha256','evidence','source_identity'}


def test_projection_repair_never_substitutes_review_hash_for_missing_top_digest(pair):
    f,s,fa,sa=pair
    # Feed negative is isolated: its missing digest must not poison the later
    # Story's otherwise genuine, intact feed anchor.
    broken_feed=deepcopy(fa);del broken_feed['image_sha256']
    store=ProjectedStore(f,[broken_feed]);source=broken_feed['source_identity']
    assert repair._reviewed_existing_artifact(store,source['source_id'],source['source_hash']) is None
    assert len(store.returned[0])==1 and 'image_sha256' not in store.returned[0][0]
    story_source=sa['source_identity'];reference={'feed':f,'artifact':fa}
    intact=ProjectedStore(f,[sa])
    assert daily._candidate_artifact(intact,'lasso_ig',story_source['source_id'],story_source['source_hash'],reference) is not None
    broken_story=deepcopy(sa);del broken_story['image_sha256']
    store=ProjectedStore(f,[broken_story])
    assert daily._candidate_artifact(store,'lasso_ig',story_source['source_id'],story_source['source_hash'],reference) is None
    returned=store.returned[0]
    assert len(returned)==1 and returned[0]['source_identity']==story_source
    assert 'image_sha256' not in returned[0]
    assert returned[0]['evidence']['image_sha256']==sa['image_sha256']
