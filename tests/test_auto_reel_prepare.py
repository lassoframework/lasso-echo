from pathlib import Path
from types import SimpleNamespace
import uuid

from agent import auto_reel_prepare as prep, config, story_studio


def test_default_off(monkeypatch):
    monkeypatch.delenv('AGENT_AUTO_REELS_ENABLED',raising=False)
    assert prep.create_automatic_reel({'gym_id':'gym'})['status']=='off'


def test_gym_allowlist_required(monkeypatch):
    monkeypatch.setenv('AGENT_AUTO_REELS_ENABLED','true')
    monkeypatch.setenv('AGENT_AUTO_REELS_GYMS','another')
    assert prep.create_automatic_reel({'gym_id':'gym'})['status']=='off'


def test_late_moments_grounding_and_source_cleanup(monkeypatch):
    monkeypatch.setattr(config,'auto_reels_enabled',lambda:True)
    monkeypatch.setattr(config,'auto_reels_gyms',lambda:['gym'])
    monkeypatch.setattr(config,'story_studio_render_active_for',lambda gym:True)
    candidates=[{'asset_id':str(i),'gym_id':'gym','start_ts':0,'end_ts':15,'score':1} for i in range(3)]
    captured={}
    def bind(segments, assets, **kwargs):
        captured['tmp']=kwargs['tmp_root']
        for s in segments:s.source_path=str(Path(kwargs['tmp_root'])/s.asset_id)
    monkeypatch.setattr(prep.story_candidates,'bind_source_paths',bind)
    def create(req,**kwargs):
        captured.update(request=req,kwargs=kwargs)
        return {'status':'staged'}
    monkeypatch.setattr(story_studio,'create_story',create)
    result=prep.create_automatic_reel({'gym_id':'gym','auto_reel':True}, candidates=candidates,
        moment_selector=lambda *a,**k:{'held':False,'start_ts':12,'end_ts':19,'score':90},
        analysis={'confidence':.9,'subjects':['barbell training']},
        copy_preparer=lambda *a:{'held':False,'caption':'Barbell training in the gym.','overlay':'Barbell training.','ask':'Join us.'})
    assert result['status']=='staged'
    assert all(c['start_ts']==12 for c in captured['kwargs']['candidates'])
    assert captured['request']['_moments_prepared'] is True
    assert not Path(captured['tmp']).exists()


def test_missing_visual_grounding_holds(monkeypatch):
    monkeypatch.setattr(config,'auto_reels_enabled',lambda:True)
    monkeypatch.setattr(config,'auto_reels_gyms',lambda:['gym'])
    monkeypatch.setattr(config,'story_studio_render_active_for',lambda gym:True)
    monkeypatch.setattr(prep.story_candidates,'bind_source_paths',lambda *a,**k:None)
    candidates=[{'asset_id':str(i),'gym_id':'gym','start_ts':0,'end_ts':15} for i in range(3)]
    result=prep.create_automatic_reel({'gym_id':'gym'},candidates=candidates,analysis={},
        moment_selector=lambda *a,**k:{'held':False,'start_ts':10,'end_ts':17,'score':90})
    assert result['status']=='held'
    assert 'verified' in result['reason']


def test_staging_recovers_stable_calendar_id(monkeypatch):
    stable=str(uuid.uuid4())
    monkeypatch.setattr('agent.real_calendar_mirror._real_row',lambda *a:{'account':'instagram','image_url':'https://example.test/reel.mp4','status':'pending','format':'feed'})
    class Calendar:
        row=None
        def get_row(self,gym,id):return self.row
        def insert_rows(self,gym,rows,**kwargs):
            assert kwargs=={'preserve_ids':True}
            self.row=rows[0]
            raise TimeoutError('response lost after successful insert')
    cal=Calendar()
    row_id,error=story_studio._stage_calendar_row('gym',SimpleNamespace(draft_type='auto_reel',draft_id=stable),cal_store=cal)
    assert (row_id,error)==(stable,None)
    assert cal.row['status']=='pending'
    assert story_studio._stage_calendar_row('gym',SimpleNamespace(draft_type='auto_reel',draft_id=stable),cal_store=cal)==(stable,None)
