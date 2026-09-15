"""One-encode automatic gym montage with restrained type and music-only audio.

Selected original footage is trimmed, orientation-aware framed and concatenated in one graph.
No color wash, header/footer bar, LASSO branding, or intermediate lossy render is used.
This is a technical renderer; it does not assign an editorial quality grade.
"""
from __future__ import annotations

import math
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from functools import lru_cache

from PIL import ImageFont

from .story_composer import ComposeResult, assert_segment_tenant

_WIDTH, _HEIGHT = 1080, 1920
_TEXT_WIDTH = 700


def _font_path():
    root = Path(__file__).parent / 'assets' / 'fonts'
    for name in ('DejaVuSans-Bold.ttf', 'Montserrat-Bold.ttf'):
        candidate = root / name
        if candidate.is_file():
            return candidate
    raise ValueError('Bundled bold font is unavailable')


def fit_text(text, font_path=None, *, max_size=48, min_size=40, max_lines=2, text_width=_TEXT_WIDTH, prefer_two_lines=False):
    """Measure full copy in pixels; never truncate or divide a beat across frames."""
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError('A complete, concise text beat is required')
    text = ' '.join(text.split())
    words = text.split()
    font_path = font_path or _font_path()
    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_path), size)
        if font.getlength(text) <= text_width and not (prefer_two_lines and len(words) > 1):
            return [text], size
        if max_lines == 2:
            fits = []
            for split in range(1, len(words)):
                left, right = ' '.join(words[:split]), ' '.join(words[split:])
                widths = font.getlength(left), font.getlength(right)
                if max(widths) <= text_width:
                    fits.append((abs(widths[0] - widths[1]), left, right))
            if fits:
                _, left, right = min(fits)
                return [left, right], size
    raise ValueError('Complete text beat cannot fit two readable lines')


def _beats(copy):
    if not isinstance(copy, dict) or copy.get('held'):
        raise ValueError('Approved reel copy is required')
    ask = copy.get('ask')
    if not isinstance(ask, str) or not ask.strip():
        raise ValueError('An approved closing ask is required')
    supplied = copy.get('overlay_beats')
    beats = list(supplied) if isinstance(supplied, (list, tuple)) else [copy.get('overlay')]
    if not beats or not all(isinstance(beat, str) and beat.strip() for beat in beats):
        raise ValueError('Complete overlay beats are required')
    beats = [' '.join(beat.split()) for beat in beats]
    ask = ' '.join(ask.split())
    if beats[-1] != ask:
        beats.append(ask)
    if not 2 <= len(beats) <= 3 or ask in beats[:-1]:
        raise ValueError('Use up to two complete body beats and one closing ask')
    return beats


def _filter_path(path):
    return str(path).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")


def _beat_windows(total, count):
    """Brief titles separated by genuinely clean footage, including short previews."""
    span = min(3.0, total / (2 * count - 1))
    if count == 2:
        return [(0.0, span), (total - span, total)]
    return [(0.0, span), ((total - span) / 2, (total + span) / 2), (total - span, total)]


def _source_layout(source, probe):
    result = subprocess.run([probe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                             '-select_streams', 'v:0', '-show_streams', '-of', 'json', str(source)],
                            capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError('Source display orientation could not be verified')
    stream = json.loads(result.stdout)['streams'][0]
    width, height = int(stream['width']), int(stream['height'])
    side = stream.get('side_data_list') or []
    rotation = float((stream.get('tags') or {}).get('rotate') or 0)
    for entry in side:
        if 'rotation' in entry:
            rotation = float(entry['rotation'])
    if round(abs(rotation)) % 180 == 90:
        width, height = height, width
    transfer = stream.get('color_transfer')
    for entry in side:
        if transfer not in ('arib-std-b67', 'smpte2084') and entry.get('dv_profile') == 8:
            transfer = {4: 'arib-std-b67', 1: 'smpte2084'}.get(entry.get('dv_bl_signal_compatibility_id'), transfer)
    return width / height, transfer, stream.get('color_primaries')


@lru_cache(maxsize=1)
def _modern_scale():
    result=subprocess.run(['ffmpeg','-hide_banner','-h','filter=scale'],capture_output=True,text=True,timeout=10,check=True)
    return 'in_transfer' in result.stdout


def _color_filter(transfer, primaries):
    if transfer in ('arib-std-b67', 'smpte2084'):
        if not _modern_scale():
            # Production FFmpeg 6 uses zscale for explicit HDR to SDR conversion.
            return (f'setparams=range=limited:color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc,'
                    'zscale=t=linear:npl=100,'
                    'format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0:peak=10,'
                    'zscale=t=bt709:m=bt709:r=tv,format=yuv420p,')
        # Explicit HDR->linear->tone map->SDR; input autorotation is FFmpeg's default.
        # scale's transfer controls require a recent FFmpeg. An older binary errors
        # and HOLDS instead of silently exporting washed-out HLG footage as SDR.
        return (f'scale=in_transfer={transfer}:out_transfer=linear:in_primaries=bt2020:out_primaries=bt709,'
                'format=gbrpf32le,tonemap=tonemap=hable:desat=0:peak=10,'
                'scale=in_transfer=linear:out_transfer=bt709:out_primaries=bt709,format=yuv420p,')
    if primaries == 'bt2020':
        if not _modern_scale():
            return 'zscale=pin=bt2020:p=bt709,format=yuv420p,'
        return 'scale=in_primaries=bt2020:out_primaries=bt709,format=yuv420p,'
    return ''



def music_start(path):
    """Find sustained audible music in the first ten seconds, bounded and local."""
    import numpy as np
    result = subprocess.run(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe',
        '-i',str(Path(path).resolve()),'-t','10','-vn','-ac','1','-ar','8000',
        '-f','f32le','pipe:1'],capture_output=True,timeout=20,check=True)
    samples = np.frombuffer(result.stdout,dtype='<f4')
    count = len(samples)//400
    if count < 2:
        raise ValueError('Licensed music has no usable opening')
    rms = np.sqrt(np.mean(samples[:count*400].reshape(count,400)**2,axis=1))
    threshold = max(.005,float(rms.max())*.08)
    for index in range(count-1):
        if rms[index] >= threshold and rms[index+1] >= threshold:
            return round(index*.05,3)
    raise ValueError('Licensed music has no sustained audible opening')

def render(plan, output_dir, copy, identity_text, music_path, *, portrait_reader=None, **ignored_old_overlay_kwargs):
    """Produce one MP4 or a held ComposeResult. Room audio is deliberately dropped.

    Music must be the existing licensed bed supplied by the caller. Whole text beats
    appear briefly at the opening, middle and close, with clean footage between. Encoding is bounded
    to five minutes even for a 60 second reel; no stderr/path details escape on failure.
    """
    output = None
    try:
        from .clipper_render import _require_render, RenderError
        try:
            _require_render()
        except RenderError:
            raise ValueError("Rendering disabled or unavailable under the existing render gate") from None
        if plan.held or not plan.segments or len(plan.segments) > 10:
            raise ValueError('A usable selected segment plan is required')
        total = sum(float(seg.duration) for seg in plan.segments)
        if not math.isfinite(total) or not 0 < total <= 60:
            raise ValueError('Reel duration must be between zero and sixty seconds')
        if abs(float(plan.total_sec) - total) > .05:
            raise ValueError('Selected segments do not match the planned duration')
        if not music_path or not Path(music_path).is_file():
            raise ValueError('Licensed music file is required for this music-only reel')
        start_music = music_start(music_path)
        ffmpeg = shutil.which('ffmpeg')
        probe = shutil.which('ffprobe')
        if not ffmpeg or not probe:
            raise ValueError('FFmpeg and ffprobe are required')
        from . import config
        portrait = config.auto_reels_portrait_active_for(plan.gym_id)
        font = Path(__file__).parent/'assets/fonts/Montserrat-Bold.ttf' if portrait else _font_path()
        fitted = [fit_text(beat, font, max_size=62 if portrait else 48,
                          min_size=52 if portrait else 40, text_width=900 if portrait else 700)
                  for beat in _beats(copy)]
        if portrait:
            fitted[-1]=fit_text(_beats(copy)[-1],font,max_size=62,min_size=52,
                                text_width=900,prefer_two_lines=True)
        name, name_size = fit_text(identity_text, font, max_size=34 if portrait else 32,
                                   min_size=28, max_lines=1, text_width=900 if portrait else 700)
        tracks = {}
        if portrait:
            from .auto_reel_portrait import inspect_track
            windows = {}
            for seg in plan.segments:
                assert_segment_tenant(seg, plan.gym_id)
                aspect, _, _ = _source_layout(seg.source_path, probe)
                if aspect < 9/16-.002:
                    raise ValueError('Narrow footage needs a verified full-body framing plan')
                if aspect > 9/16+.002:
                    key = (seg.asset_id, seg.source_path)
                    lo,hi,_ = windows.get(key,(seg.start_ts,seg.end_ts,aspect))
                    windows[key]=(min(lo,seg.start_ts),max(hi,seg.end_ts),aspect)
            for (aid,source),(lo,hi,aspect) in windows.items():
                tracks[aid]=inspect_track(source,lo,hi,aspect,plan.gym_id,reader=portrait_reader)
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / f'auto-reel-{uuid.uuid4().hex}.mp4'
        with tempfile.TemporaryDirectory(prefix='echo-reel-type-') as temporary:
            work = Path(temporary)
            command = [ffmpeg, '-nostdin', '-v', 'error', '-y', '-filter_complex_threads', '2']
            filters = []
            for index, seg in enumerate(plan.segments):
                assert_segment_tenant(seg, plan.gym_id)
                if (not math.isfinite(float(seg.start_ts)) or seg.start_ts < 0
                        or seg.duration <= 0 or not Path(seg.source_path).is_file()):
                    raise ValueError('Selected original footage is unavailable')
                command += ['-ss', f'{seg.start_ts:.6f}', '-t', f'{seg.duration:.6f}',
                            '-protocol_whitelist', 'file,pipe', '-i', str(Path(seg.source_path).resolve())]
                aspect, transfer, primaries = _source_layout(Path(seg.source_path).resolve(), probe)
                base = (f'[{index}:v:0]trim=duration={seg.duration:.6f},setpts=PTS-STARTPTS,'
                        + _color_filter(transfer, primaries) + 'setsar=1,fps=30,')
                if aspect <= (9/16+.002 if portrait else .70):
                    filters.append(base + 'scale=1080:1920:force_original_aspect_ratio=increase,'
                                   f'crop=1080:1920,setsar=1,format=yuv420p[v{index}]')
                elif portrait:
                    from .auto_reel_portrait import crop_expression
                    x = crop_expression(tracks[seg.asset_id],seg.start_ts,seg.duration)
                    filters.append(base + f"crop=ih*9/16:ih:x='{x}':y=0,scale=1080:1920,"
                                   f'setsar=1,format=yuv420p[v{index}]')
                else:
                    # Preserve the entire landscape image instead of stretching a
                    # tiny center crop. Its soft background fills the portrait frame.
                    filters.append(base + f'split=2[bg{index}][fg{index}]')
                    filters.append(f'[bg{index}]scale=270:480:force_original_aspect_ratio=increase,'
                                   f'crop=270:480,gblur=sigma=18,scale=1080:1920[blur{index}]')
                    filters.append(f'[fg{index}]scale=1080:1920:force_original_aspect_ratio=decrease[fit{index}]')
                    filters.append(f'[blur{index}][fit{index}]overlay=(W-w)/2:(H-h)/2,'
                                   f'setsar=1,format=yuv420p[v{index}]')
            count = len(plan.segments)
            command += ['-stream_loop', '-1', '-ss', str(start_music), '-protocol_whitelist', 'file,pipe', '-i', str(Path(music_path).resolve())]
            filters.append(''.join(f'[v{i}]' for i in range(count)) + f'concat=n={count}:v=1:a=0[montage]')
            windows = _beat_windows(total, len(fitted))
            if portrait and len(fitted) == 3 and total >= 15:
                # The accepted B source is the timing reference, scaled to this reel.
                windows=[(0,2.8),(total*.38,total*.38+3.1),(total-3.3,total)]
            name_file = work / 'identity.txt'; name_file.write_text(name[0], encoding='utf-8')
            opening, closing = windows[0], windows[-1]
            identity_enable = (f"lt(t,{opening[1]:.6f})+gte(t,{closing[0]:.6f})")
            name_x,name_y = ('90','190') if portrait else ('(w-text_w)/2','852')
            draw = (f"[montage]drawtext=fontfile='{_filter_path(font)}':textfile='{_filter_path(name_file)}':"
                    f'expansion=none:fontsize={name_size}:fontcolor=white@0.92:x={name_x}:y={name_y}:'
                    f"borderw=1:bordercolor=black@0.3:shadowcolor=black@0.3:shadowx=1:shadowy=1:enable='{identity_enable}'")
            for index, (lines, size) in enumerate(fitted):
                start, end = windows[index]
                # Each line is centered independently; the complete beat is shown
                # together, never split into timed sentence fragments.
                for line_index, line in enumerate(lines):
                    text_file = work / f'beat-{index}-{line_index}.txt'; text_file.write_text(line, encoding='utf-8')
                    x = '90' if portrait else '(w-text_w)/2'
                    y = str(1390+line_index*78) if portrait else str(900+line_index*(size+8))
                    motion = ''
                    if portrait:
                        y += f'+18*max(0,1-(t-{start:.6f})/0.2)'
                        motion = f":alpha='min(1,min(max(0,(t-{start:.6f})/0.2),max(0,({end:.6f}-t)/0.18)))'"
                    draw += (f",drawtext=fontfile='{_filter_path(font)}':textfile='{_filter_path(text_file)}':"
                             f"expansion=none:fontsize={size}:fontcolor=white:x={x}:y='{y}':"
                             "borderw=1:bordercolor=black@0.35:shadowcolor=black@0.35:shadowx=1:shadowy=1:"
                             f"enable='gte(t,{start:.6f})*lt(t,{end:.6f})'{motion}")
            filters.append(draw + ',format=yuv420p[video]')
            filters.append(f'[{count}:a:0]atrim=duration={total:.6f},asetpts=PTS-STARTPTS,'
                           'loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,'
                           f'afade=t=in:d=0.25,afade=t=out:st={max(0,total-.75):.6f}:d=0.75[music]')
            graph = work / 'render.ffscript'; graph.write_text(';\n'.join(filters), encoding='utf-8')
            command += ['-filter_complex_script', str(graph), '-map', '[video]', '-map', '[music]',
                        '-t', f'{total:.6f}', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
                        '-threads', '4', '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709',
                        '-color_trc', 'bt709', '-colorspace', 'bt709', '-c:a', 'aac', '-b:a', '192k',
                        '-movflags', '+faststart', str(output)]
            result = subprocess.run(command, capture_output=True, timeout=min(300, max(60, math.ceil(total * 8))))
            if result.returncode or not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError('FFmpeg render failed')
        if portrait:
            output.with_suffix('.framing.json').write_text(json.dumps({
                'treatment':'portrait_v1','gym_id':plan.gym_id,'tracks':tracks,
                'limitation':'Half-second samples do not verify every frame.'},indent=2))
        return ComposeResult(plan=plan, output_path=str(output))
    except Exception as exc:
        if output is not None:
            output.unlink(missing_ok=True)
        # Deliberate validation messages are safe; external tool stderr never returns.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return ComposeResult(plan=plan, held=True, hold_reason=f'Automatic reel held: {reason}')
