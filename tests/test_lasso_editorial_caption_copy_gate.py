"""Pure actual formatter/mirror/copy-gate regression; no staging or providers."""
from copy import deepcopy
import pytest
from agent import config,copy_gate,lasso_editorial,real_month_planner
from agent.drafter import Draft

URL='https://lassoframework.com/growth-call'

def draft(category,story=False):
    return Draft(draft_id='editorial-copy',account_key='lasso',platform='instagram',
      caption='Original approved caption.',hashtags=[],creative_path='',creative_public_url='https://example/image.png',
      scheduled_for='',day_key='2026-10-15',draft_type='story' if story else 'feed',is_story=story,
      category=category,source_fragments=['One clear plan for your gym.','Keep the next step visible.'])

@pytest.mark.parametrize('category,subject',[('echo','Echo for your gym'),('website','your gym website')])
def test_actual_calendar_rows_pass_copy_gate_without_altering_words_or_url(monkeypatch,category,subject):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    d=draft(category);before=deepcopy(d)
    rows=real_month_planner.to_calendar_rows([d],'lasso')
    expected=d.source_fragments[0]+'\n\n'+d.source_fragments[1]+'\n\nBook a call to talk about '+subject+', '+URL
    assert {r['account'] for r in rows}=={'instagram','facebook'}
    assert all(r['caption']==expected and not copy_gate.caption_violations(r['caption']) for r in rows)
    assert d==before and URL in expected and ': https://' not in expected

@pytest.mark.parametrize('category',['echo','website'])
@pytest.mark.parametrize('scope',['client','disarmed','story'])
def test_formatter_change_preserves_client_disarmed_and_story_mapping(monkeypatch,category,scope):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:scope!='disarmed')
    d=draft(category,story=scope=='story');before=deepcopy(d)
    rows=real_month_planner.to_calendar_rows([d],'client-fixture' if scope=='client' else 'lasso')
    assert rows and all(r['caption']==d.caption for r in rows)
    assert d==before and all(URL not in r['caption'] for r in rows)

@pytest.mark.parametrize('category',['echo','website'])
def test_no_source_fragments_keeps_original_caption(category):
    d=draft(category);d.source_fragments=[]
    assert lasso_editorial.editorial_caption(d)==d.caption

@pytest.mark.parametrize('category',['echo','website'])
def test_source_fragment_order_and_approved_tags_unchanged(category):
    d=draft(category);d.source_fragments.append('Keep every approved fact.');d.hashtags=['#LASSOFramework','#GymMarketing','#GymOwners']
    caption=lasso_editorial.editorial_caption(d)
    assert caption.startswith(d.source_fragments[0]+'\n\n'+' '.join(d.source_fragments[1:]))
    assert caption.endswith('\n\n'+' '.join(d.hashtags))
    assert not copy_gate.caption_violations(caption)
