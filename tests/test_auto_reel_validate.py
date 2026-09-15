import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from agent import auto_reel_validate as validate


def metadata(video_seconds=17, audio_seconds=17):
    return {'format': {'duration': '60'}, 'streams': [
        {'codec_type': 'video', 'codec_name': 'h264', 'pix_fmt': 'yuv420p',
         'width': 1080, 'height': 1920, 'duration': str(video_seconds)},
        {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2, 'sample_rate': '48000',
         'duration': str(audio_seconds)}]}


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    path = tmp_path / 'reel.mp4'
    path.write_bytes(b'test placeholder; subprocess is mocked')
    monkeypatch.setattr(validate.shutil, 'which', lambda name: name)
    calls = []
    current = metadata()
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(current) if command[0] == 'ffprobe' else '')
    monkeypatch.setattr(validate.subprocess, 'run', run)
    return path, current, calls


def test_valid_streams_require_full_decode(artifact):
    path, _, calls = artifact
    result = validate.validate_output(path, 17)
    assert result['ok'] and result['metadata']['decode_checked']
    assert result['metadata']['video_duration'] == 17
    assert len(calls) == 2
    assert '-xerror' in calls[-1][0]
    assert calls[-1][1]['timeout'] == 51


def test_wrong_geometry_is_held_before_decode(artifact):
    path, data, calls = artifact
    data['streams'][0]['width'] = 1920
    assert '1080 by 1920' in validate.validate_output(path, 17)['reason']
    assert len(calls) == 1


def test_truncated_video_not_hidden_by_audio_or_container_duration(artifact):
    path, data, _ = artifact
    data['streams'][0]['duration'] = '4'
    data['streams'][1]['duration'] = '60'
    result = validate.validate_output(path, 60)
    assert not result['ok']
    assert result['metadata']['video_duration'] == 4


def test_duration_must_belong_to_stream_not_container(artifact):
    path, data, _ = artifact
    data['streams'][0].pop('duration')
    assert not validate.validate_output(path, 17)['ok']
    data['streams'][0].update(duration_ts=17000, time_base='1/1000')
    assert validate.validate_output(path, 17)['ok']


def test_duration_tolerances_and_audio_mismatch(artifact):
    path, data, _ = artifact
    data['streams'][0]['duration'] = '16.25'
    data['streams'][1]['duration'] = '16.75'
    assert validate.validate_output(path, 17)['ok']
    data['streams'][0]['duration'] = '16.24'
    assert not validate.validate_output(path, 17)['ok']
    data['streams'][0]['duration'] = '17'
    data['streams'][1]['duration'] = '17.51'
    assert 'different durations' in validate.validate_output(path, 17)['reason']


def test_audio_requirement_can_only_be_explicitly_disabled(artifact):
    path, data, _ = artifact
    data['streams'].pop()
    assert not validate.validate_output(path, 17)['ok']
    assert validate.validate_output(path, 17, require_audio=False)['ok']


def test_wrong_codec_or_pixels_are_held(artifact):
    path, data, _ = artifact
    data['streams'][0]['pix_fmt'] = 'yuv444p'
    assert not validate.validate_output(path, 17)['ok']
    data['streams'][0].update(pix_fmt='yuv420p', codec_name='hevc')
    assert not validate.validate_output(path, 17)['ok']


def test_missing_empty_file_and_invalid_expected_duration(tmp_path):
    assert not validate.validate_output(tmp_path / 'missing.mp4', 17)['ok']
    empty = tmp_path / 'empty.mp4'; empty.touch()
    assert not validate.validate_output(empty, 17)['ok']
    for value in [None, 0, -1, float('nan'), float('inf'), 61]:
        assert not validate.validate_output(empty, value)['ok']


def test_decode_error_or_timeout_fails_closed(artifact, monkeypatch):
    path, data, _ = artifact
    def failed(command, **kwargs):
        return SimpleNamespace(returncode=0 if command[0] == 'ffprobe' else 1, stdout=json.dumps(data))
    monkeypatch.setattr(validate.subprocess, 'run', failed)
    assert 'failed full decoding' in validate.validate_output(path, 17)['reason']
    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 15)
    monkeypatch.setattr(validate.subprocess, 'run', timed_out)
    assert not validate.validate_output(path, 17)['ok']


def test_real_short_vertical_h264_with_audio_decodes(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe are required for encoded artifact test')
    output = tmp_path / 'actual.mp4'
    subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', 'color=c=blue:s=1080x1920:r=24:d=1',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-t', '1', str(output)], check=True, capture_output=True, timeout=30)
    result = validate.validate_output(output, 1)
    assert result['ok'], result
    assert result['metadata']['decode_checked']
    assert result['metadata']['audio_duration'] > 0
