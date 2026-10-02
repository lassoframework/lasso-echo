import json
from types import SimpleNamespace

import pytest

from agent import reel_moments as m


def metric(time, **changes):
    return dict(time=time, mean=.48, clipped=.03, contrast=.15,
                sharpness=250., motion=.06, shake=False, **changes)


def rows(duration):
    return [metric(i / 2) for i in range(int(duration * 2))]


def test_action_starts_late_not_opening_seconds():
    evidence = rows(100)
    for row in evidence:
        row['motion'] = .001 if row['time'] < 70 else .06
    result = m.choose_window(evidence, 100, window_sec=6)
    assert not result['held']
    assert result['start_ts'] >= 69.5
    assert result['end_ts'] <= 100
    assert result['diagnostics']['inspected_through'] == 99.5


def test_black_lead_and_static_tail_are_not_selected():
    evidence = rows(30)
    for row in evidence:
        if row['time'] < 15:
            row.update(mean=.01, clipped=.99, contrast=.0, sharpness=.0)
        elif row['time'] >= 23:
            row['motion'] = .0001
    result = m.choose_window(evidence, 30)
    assert not result['held']
    assert result['start_ts'] >= 14.5
    assert result['end_ts'] <= 24


@pytest.mark.parametrize('change', [dict(motion=0.), dict(mean=.99),
                                   dict(sharpness=1.), dict(shake=True)])
def test_unusable_sources_hold_without_fallback(change):
    evidence = rows(12)
    for row in evidence:
        row.update(change)
    result = m.choose_window(evidence, 12)
    assert result['held'] and result['start_ts'] is None


@pytest.mark.parametrize('duration', [2.9, 300.1, float('nan'), float('inf')])
def test_duration_caps(duration):
    assert m.choose_window([], duration)['held']


@pytest.mark.parametrize('window', [2.9, 15.1, float('nan')])
def test_window_caps(window):
    assert m.choose_window(rows(10), 10, window_sec=window)['held']


def test_full_coverage_and_finite_ordered_timestamps_required():
    assert m.choose_window(rows(10)[:-1], 10)['held']
    evidence = rows(10)
    evidence[-1]['time'] = 400
    assert m.choose_window(evidence, 10)['held']
    evidence = rows(10)
    evidence[-1]['motion'] = float('nan')
    assert m.choose_window(evidence, 10)['held']


def test_short_source_window_fits_and_tie_is_deterministic():
    a = m.choose_window(rows(4), 4)
    assert (a['start_ts'], a['end_ts']) == (0., 4.)
    assert a == m.choose_window(rows(4), 4)


def test_actual_size_rejects_before_running_probe(tmp_path):
    source = tmp_path / 'huge.mp4'
    with source.open('wb') as f:
        f.truncate(m.MAX_BYTES + 1)
    def forbidden(*args, **kwargs):
        pytest.fail('Oversize file must not run probe or ffmpeg')
    assert m.select_moment(source, runner=forbidden)['held']


def test_probe_duration_rejects_before_decode(tmp_path):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'x')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({'streams':[{'codec_type':'video'}],
                                                  'format':{'duration':301}}).encode())
    assert m.select_moment(source, runner=run)['held']
    assert len(calls) == 1


def test_missing_decoder_holds(tmp_path):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'x')
    def run(*args, **kwargs):
        raise FileNotFoundError()
    assert m.select_moment(source, runner=run)['held']


def test_real_pixels_static_pan_and_black_are_not_activity():
    np = pytest.importorskip('numpy')
    pytest.importorskip('cv2')
    rng = np.random.default_rng(8)
    base = rng.integers(30, 220, (m.HEIGHT, m.WIDTH), dtype=np.uint8)
    for frames in ([base.copy() for _ in range(20)],
                   [np.zeros_like(base) for _ in range(20)],
                   [np.roll(base, i, axis=1) for i in range(20)]):
        assert m.choose_window(m.frame_metrics(frames), 10)['held']


def test_real_pixels_local_activity_after_black_intro():
    np = pytest.importorskip('numpy')
    pytest.importorskip('cv2')
    rng = np.random.default_rng(11)
    base = rng.integers(40, 210, (m.HEIGHT, m.WIDTH), dtype=np.uint8)
    frames = []
    for i in range(40):
        image = np.zeros_like(base) if i < 20 else base.copy()
        if i >= 20:
            x = 15 + ((i - 20) % 5) * 10
            image[20:50, x:x+30] = 235
        frames.append(image)
    result = m.choose_window(m.frame_metrics(frames), 20, window_sec=4)
    assert not result['held']
    assert 10 <= result['start_ts'] <= 16


def test_real_camera_shake_is_rejected():
    np = pytest.importorskip('numpy')
    pytest.importorskip('cv2')
    rng = np.random.default_rng(12)
    base = rng.integers(30, 220, (m.HEIGHT, m.WIDTH), dtype=np.uint8)
    frames = [np.roll(base, 4 if i % 2 else -4, axis=1) for i in range(20)]
    assert m.choose_window(m.frame_metrics(frames), 10)['held']


def test_real_ffmpeg_late_action_fractional_duration(tmp_path):
    import shutil
    import subprocess
    np = pytest.importorskip('numpy')
    pytest.importorskip('cv2')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe required for real decoding test')
    rng = np.random.default_rng(11)
    base = rng.integers(40, 210, (m.HEIGHT, m.WIDTH), dtype=np.uint8)
    frames = []
    for i in range(201):
        image = np.zeros_like(base) if i < 100 else base.copy()
        if i >= 100:
            x = 15 + (((i - 100) // 5) % 5) * 10
            image[20:50, x:x+30] = 235
        frames.append(image.tobytes())
    video = tmp_path / 'late-action.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'rawvideo',
                    '-pixel_format', 'gray', '-video_size', '128x72', '-framerate', '10',
                    '-i', 'pipe:0', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
                   input=b''.join(frames), capture_output=True, check=True, timeout=20)
    result = m.select_moment(video, window_sec=4)
    assert not result['held'], result
    assert 10 <= result['start_ts'] < result['end_ts'] <= 20.1
    assert result['diagnostics']['samples'] == 41


def test_video_duration_ignores_audio_tail_and_shorter_moment_keeps_gates(tmp_path, monkeypatch):
    pytest.importorskip('numpy')
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'x')
    evidence = rows(8)
    for row in evidence:
        if row['time'] >= 3:
            row['shake'] = True
    monkeypatch.setattr(m, 'frame_metrics', lambda _frames: evidence)
    def run(command, **kwargs):
        if command[0] == 'ffprobe':
            return SimpleNamespace(stdout=json.dumps({'streams':[{'codec_type':'video','duration':8}],
                                                      'format':{'duration':8.06}}).encode())
        return SimpleNamespace(stdout=bytes(16 * m.WIDTH * m.HEIGHT))
    result = m.select_moment(source, window_sec=7, runner=run)
    assert not result['held']
    assert (result['start_ts'], result['end_ts']) == (0., 3.)
    assert result['diagnostics']['source_duration'] == 8
    assert result['diagnostics']['attempted_window_seconds'] == [7, 6, 5, 4, 3]
    assert result['diagnostics']['selected_quality']['camera_shake_frames'] == 0
