import shutil
import subprocess

import pytest
from PIL import ImageFont
from agent.auto_reel_render import render, fit_text, _beats, _font_path
from agent.story_composer import ComposePlan, Segment


def test_music_onset_skips_silence_and_ignores_isolated_tick(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from agent import auto_reel_render as module
    signal=np.zeros(8000,dtype='<f4')
    signal[800:1200]=.2
    signal[4000:]=.1
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=signal.tobytes()))
    assert module.music_start('licensed.wav') == .5
    signal[:]=0
    with pytest.raises(ValueError,match='sustained audible'):
        module.music_start('licensed.wav')


@pytest.fixture(autouse=True)
def arm_test_renderer(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED", "true")

def test_entire_beat_fits_two_measured_lines_without_truncation():
    text = 'Bring your energy. We will bring the coaching.'
    lines, size = fit_text(text)
    assert len(lines) <= 2 and ' '.join(lines) == text
    assert 40 <= size <= 48
    font = ImageFont.truetype(str(_font_path()), size)
    assert all(font.getlength(line) <= 700 for line in lines)


def test_long_complete_beat_holds_instead_of_splitting_into_timed_fragments():
    with pytest.raises(ValueError, match='cannot fit'):
        fit_text('This complete sentence is far too long to display at a readable size and must remain intact rather than being split across several different frames of a short video montage.')


def test_three_beats_keep_one_closing_ask():
    copy = {'overlay_beats': ['Your next session starts here.', 'Bring your energy.'], 'ask': 'Book your first session.'}
    assert _beats(copy) == [*copy['overlay_beats'], copy['ask']]
    assert _beats({'overlay': 'Bring your energy.', 'ask': 'Book your first session.'}) == ['Bring your energy.', 'Book your first session.']


def test_invalid_plan_or_missing_music_holds_without_encoding(tmp_path):
    plan = ComposePlan('gym', segments=[Segment('x', 'gym', 0, 3, source_path='missing')], total_sec=3)
    result = render(plan, tmp_path, {'overlay':'Your next session.', 'ask':'Join us.'}, 'Gym', None)
    assert result.held and 'music' in result.hold_reason
    assert not list(tmp_path.glob('*.mp4'))


def test_real_two_clip_single_encode_with_music(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe required')
    sources=[]
    for index, color in enumerate(['blue', 'green']):
        path=tmp_path/f'raw{index}.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s={"270x480" if index == 0 else "480x270"}:r=30:d=1',
                        '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True,timeout=15)
        sources.append(path)
    music=tmp_path/'music.wav'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=2',str(music)],check=True,capture_output=True,timeout=15)
    plan=ComposePlan('gym',segments=[Segment(str(i),'gym',0,1,source_path=str(path)) for i,path in enumerate(sources)],total_sec=2)
    result=render(plan,tmp_path,{'overlay_beats':['Bring your energy.','Find your rhythm.'],'ask':'Join us.'},'Example Gym',str(music))
    assert not result.held, result.hold_reason
    from agent.auto_reel_validate import validate_output
    verified=validate_output(result.output_path,2)
    assert verified['ok'], verified


def test_display_rotation_and_hlg_metadata_are_respected(monkeypatch):
    import json
    from types import SimpleNamespace
    from agent import auto_reel_render as module
    stream = {'width':1920,'height':1080,'color_primaries':'bt2020',
              'color_transfer':'arib-std-b67','side_data_list':[{'rotation':-90}]}
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({'streams':[stream]})))
    aspect, transfer, primaries = module._source_layout('local.mp4', 'ffprobe')
    assert aspect == 1080/1920
    assert transfer == 'arib-std-b67'
    graph = module._color_filter(transfer, primaries)
    assert ('in_transfer=arib-std-b67:out_transfer=linear' in graph or
            'color_trc=arib-std-b67:colorspace=bt2020nc,zscale=t=linear' in graph)
    assert 'tonemap=' in graph and ('out_transfer=bt709' in graph or 'zscale=t=bt709' in graph)
    assert module._color_filter('bt709','bt709') == ''


def test_render_retains_existing_master_gate(monkeypatch,tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED","false")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED","false")
    result=render(ComposePlan("gym"),tmp_path,{},"Gym",None)
    assert result.held and "existing render gate" in result.hold_reason


def test_reference_montage_titles_leave_clean_intervals():
    from agent.auto_reel_render import _beat_windows
    windows = _beat_windows(17, 3)
    assert windows == [(0, 3), (7, 10), (14, 17)]
    assert all(end-start <= 3 for start,end in windows)
    assert all(windows[i][1] < windows[i+1][0] for i in range(2))
    short = _beat_windows(2, 3)
    assert all(short[i][1] < short[i+1][0] for i in range(2))
