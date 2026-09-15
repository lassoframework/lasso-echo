from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from agent import auto_reel_status as status

class Store:
    def __init__(self,rows):self.rows=rows;self.params=None
    def available(self):return True
    def _client(self):return self
    def _rest(self,t):return t
    def _headers(self,*args):return {}
    def get(self,url,**kwargs):
        self.params=kwargs['params']
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:self.rows)

def test_shared_status_is_tenant_scoped_and_fresh():
    store=Store([{'gym_id':'gym','updated_at':datetime.now(timezone.utc).isoformat(),'snapshot':{'gym':'gym','ok':True,'jobs':[]}}])
    assert status.read('gym',store=store)['ok']
    assert store.params['gym_id']=='eq.gym'
    assert not status.read('other',store=store)['ok']
    store.rows[0]['updated_at']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    assert not status.read('gym',store=store)['ok']

def test_missing_status_is_not_fake_idle():
    assert not status.read('gym',store=Store([]))['ok']

def test_status_only_does_not_read_story_history(monkeypatch):
    from agent import story_studio_routes as routes,config
    monkeypatch.setattr(routes,'_render_armed',lambda key:True)
    monkeypatch.setattr(config,'auto_reels_portrait_active_for',lambda key:key=='gym')
    seen=[]
    def read(gym):
        seen.append(gym)
        return {'gym':gym,'ok':True,'jobs':[]}
    monkeypatch.setattr(status,'read',read)
    code,body=routes.handle_list_stories('gym_ig',store=object(),automatic_only=True)
    assert code==200 and body['automatic_reels']['enabled']
    assert seen==['gym']
    code,body=routes.handle_list_stories('other_ig',store=object(),automatic_only=True)
    assert body['automatic_reels']=={'enabled':False} and seen==['gym']

def test_automatic_rebuild_cannot_drop_approved_treatment(monkeypatch):
    from agent import story_studio_routes as routes
    monkeypatch.setattr(routes,'_render_armed',lambda key:True)
    store=SimpleNamespace(available=lambda:True,get_request=lambda *a,**kw:{'requested_by':'echo_auto_reels'})
    code,body=routes.handle_rebuild_story('gym','id',{'overlay_text':'Changed','identity_tokens':['Gym']},store=store)
    assert code==409 and 'legacy' in body['error']
