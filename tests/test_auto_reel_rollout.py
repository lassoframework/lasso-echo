import pytest
from agent import auto_reel_roster as roster, config, auto_reel_render as render

class Store:
    def available(self):return True
    def _get_all(self,table,params):
        if table=='gyms':return [{'id':'a','is_demo':False},{'id':'b','is_demo':True}]
        return [{'gym_id':'a','echo_account_key':'Gym_ig'}, {'gym_id':'b','echo_account_key':'demo'},
                {'gym_id':'missing','echo_account_key':'other'}, {'gym_id':'a','echo_account_key':'bad/key'}]

def test_fleet_roster_excludes_demo_unmapped_and_bad_keys():
    assert roster.load(store=Store())==['gym']

def test_fleet_requires_explicit_flag_and_preserves_named_scope(monkeypatch):
    monkeypatch.delenv('AGENT_AUTO_REELS_ALL_ACCOUNTS',raising=False)
    monkeypatch.setenv('AGENT_AUTO_REELS_GYMS','pilot,*')
    monkeypatch.setattr(roster,'gyms',lambda:['one','two'])
    assert config.auto_reels_gyms()==['pilot']
    monkeypatch.setenv('AGENT_AUTO_REELS_ALL_ACCOUNTS','true')
    assert config.auto_reels_gyms()==['one','two']

def test_expired_roster_is_not_reused_after_failure(monkeypatch):
    monkeypatch.setattr(roster,'_cache',(0,['old']))
    monkeypatch.setattr(roster.time,'monotonic',lambda:1000)
    def fail():raise ValueError('offline')
    monkeypatch.setattr(roster,'load',fail)
    with pytest.raises(ValueError):roster.gyms()

@pytest.mark.parametrize('modern',[True,False])
def test_hdr_paths_are_explicit_and_end_in_sdr(monkeypatch,modern):
    monkeypatch.setattr(render,'_modern_scale',lambda:modern)
    f=render._color_filter('arib-std-b67','bt2020')
    assert 'tonemap=tonemap=hable' in f and f.endswith('format=yuv420p,')
    assert ('in_transfer=arib-std-b67' if modern else 'color_trc=arib-std-b67') in f
