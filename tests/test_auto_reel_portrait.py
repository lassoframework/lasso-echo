import pytest
from agent import auto_reel_portrait as p, config

def reading(boxes):
    return {'single_subject':True,'frames':[{'index':i,'confidence':.95,'full_body':True,'box':dict(zip(('left','top','right','bottom'),b))} for i,b in enumerate(boxes)]}

def test_track_keeps_entire_athlete_and_uses_source_times():
    track={'points':p.validated_track(reading([[.35,.2,.45,.8],[.40,.2,.50,.8]]),[20,21],16/9)}
    assert p.left_at(track,20.5) == pytest.approx(.425-(9/16)/(16/9)/2)
    expr=p.crop_expression(track,20.5,.5)
    assert 't-0.000000' in expr and '20.5' not in expr

@pytest.mark.parametrize('box',[[.1,.2,.9,.8],[-.1,.2,.4,.8],[.2,0,.3,.8],[.2,.2,float('nan'),.8]])
def test_unsafe_crop_holds(box):
    with pytest.raises(ValueError):p.validated_track(reading([box]),[0],16/9)

def test_missing_frame_or_multiple_subjects_holds():
    r=reading([[.35,.2,.45,.8]])
    with pytest.raises(ValueError):p.validated_track(r,[0,1],16/9)
    r['single_subject']=False
    with pytest.raises(ValueError):p.validated_track(r,[0],16/9)

@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),1.1,'0.95'])
def test_invalid_confidence_holds(value):
    r=reading([[.35,.2,.45,.8]])
    r['frames'][0]['confidence']=value
    with pytest.raises(ValueError):p.validated_track(r,[0],16/9)

def test_flag_requires_every_gate(monkeypatch):
    for key in ('AGENT_AUTO_REELS_PORTRAIT_ENABLED','AGENT_AUTO_REELS_ENABLED'):
        monkeypatch.delenv(key,raising=False)
    assert not config.auto_reels_portrait_active_for('gym')
    monkeypatch.setenv('AGENT_AUTO_REELS_PORTRAIT_ENABLED','true')
    monkeypatch.setenv('AGENT_AUTO_REELS_ENABLED','true')
    monkeypatch.setenv('AGENT_AUTO_REELS_GYMS','gym')
    monkeypatch.setenv('STORY_STUDIO_RENDER_GYMS','gym')
    assert config.auto_reels_portrait_active_for('gym')
    assert not config.auto_reels_portrait_active_for('other')

def test_b_typography_preserves_complete_copy():
    from agent.auto_reel_render import fit_text,_font_path
    text='Expert coaching, welcoming community'
    lines,size=fit_text(text,_font_path(),max_size=62,min_size=52,text_width=900)
    assert ' '.join(lines)==text and len(lines)<=2 and size>=52


def test_array_coordinates_are_rejected_even_when_bounds_look_valid():
    r=reading([[.2,.45,.4,.55]])
    r["frames"][0]["box"]=[.2,.45,.4,.55]
    with pytest.raises(ValueError,match="named coordinate"):
        p.validated_track(r,[0],16/9)


def test_closing_ask_uses_complete_balanced_two_lines():
    from agent.auto_reel_render import fit_text
    from pathlib import Path
    font=Path(__file__).resolve().parents[1]/"agent/assets/fonts/Montserrat-Bold.ttf"
    lines,size=fit_text("Book a free consultation.",font,max_size=62,min_size=52,text_width=900,prefer_two_lines=True)
    assert lines == ["Book a free","consultation."]
    assert size == 62
