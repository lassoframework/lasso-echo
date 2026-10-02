"""Prepare an automatic reel using eligible originals and measured shot windows.

No editor input is required. Existing Story Studio owns copy, music, branding,
rendering and pending calendar staging. This module never publishes.
"""
from pathlib import Path
import subprocess
import tempfile

from . import config, story_candidates, story_composer, story_templates, vision


def grounded_analysis(paths, gym, *, analyzer=None):
    """Inspect chosen moments using Echo's existing bounded vision path.

    One midpoint per selected shot supplies conservative copy grounding, not an
    exhaustive safety certification of every video frame. Existing source consent
    and eligibility remain required before this stage.
    """
    analyzer = analyzer or vision.analyze_and_store
    details = []
    for path, moment in paths:
        frame = Path(path).with_suffix('.reel-frame.png')
        ts = (moment['start_ts'] + moment['end_ts']) / 2
        result = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(ts), '-i', str(path),
                                 '-frames:v', '1', '-vf', 'scale=768:-2', str(frame)],
                                capture_output=True, timeout=30)
        if result.returncode or not frame.exists():
            raise ValueError('Could not inspect a selected video moment')
        analysis = analyzer(str(frame), gym=gym)
        allowed, _ = vision.auto_plannable(analysis)
        if not allowed:
            raise ValueError('Selected footage needs a verified visual analysis')
        details.extend(vision.caption_eligible_details(analysis))
    details = list(dict.fromkeys(details))
    if not details:
        raise ValueError('No verified visual details for automatic captions')
    return {'confidence': .85, 'subjects': details[:3], 'setting': '', 'mood': ''}


def create_automatic_reel(request, *, candidates=None, assets_by_id=None, analysis=None,
                          store=None, music_library=None, render_fn=None, output_dir=None,
                          now=None, downloader=None, cal_store=None,
                          moment_selector=None, analyzer=None, copy_preparer=None):
    from . import story_studio, reel_moments
    gym = story_candidates._base_gym(request.get('gym_id'))
    if not config.auto_reels_enabled() or gym not in config.auto_reels_gyms():
        return {'status': 'off', 'reason': 'Automatic reels are not enabled for this gym'}
    if not config.story_studio_render_active_for(gym):
        return {'status': 'off', 'reason': 'Story renderer is not enabled for this gym'}
    if candidates is None:
        candidates, assets_by_id = story_candidates.discover_candidates(gym, request.get('asset_ids'), strict=True)
    assets_by_id = assets_by_id or {}
    selector = moment_selector or reel_moments.select_moment
    # Bound work per job; the worker tracks the batch so future uploads do not
    # repeatedly process the entire library. Only already eligible sources enter.
    candidates = sorted(candidates or [], key=lambda c: (-float(c.get('score') or 0), c['asset_id']))[:10]
    segments = [story_composer.Segment(asset_id=c['asset_id'],gym_id=c['gym_id'],
                start_ts=c['start_ts'],end_ts=c['end_ts']) for c in candidates]
    tmp = None
    final_tmp = None
    diagnostics = []
    def held(reason):
        return {'status': 'held', 'reason': reason, 'request_id': request.get('id'),
                'moment_diagnostics': diagnostics}
    try:
        if len(segments) < 3:
            return held('At least three eligible clips are needed for an automatic reel')
        tmp = tempfile.mkdtemp(prefix="autoreel_sources_")
        story_candidates.bind_source_paths(segments, assets_by_id, gym_id=gym, downloader=downloader, tmp_root=tmp)
        refined, usable = [], []
        for seg in segments:
            moment = selector(seg.source_path, window_sec=7.0)
            diagnostics.append({'asset_id':seg.asset_id, **moment})
            if moment.get('held'):
                continue
            refined.append({'asset_id':seg.asset_id,'gym_id':gym,'start_ts':moment['start_ts'],
                            'end_ts':moment['end_ts'],'score':moment['score']})
            usable.append((seg.source_path,moment))
        if len(refined)<3:
            return held('Not enough clear, usable moments for a finished reel')
        source_paths={s.asset_id:s.source_path for s in segments}
        if analysis is None:
            analysis=grounded_analysis(usable,gym,analyzer=analyzer)
        from .story_grounding import ground_copy
        if ground_copy(analysis=analysis).low_confidence:
            return held('Automatic captions need verified footage details')
        from .auto_reel_copy import prepare_copy
        copy = (copy_preparer or prepare_copy)(gym, analysis)
        if copy.get("held"):
            return held(copy["hold_reason"])
        def render(plan, **kwargs):
            for seg in plan.segments:
                seg.source_path=source_paths[seg.asset_id]
            from .auto_reel_edit import arrange_shots
            arrange_shots(plan)
            if render_fn is None:
                from .auto_reel_render import render as render_automatic
                rendered = render_automatic(plan, copy=copy, **kwargs)
            else:
                rendered = render_fn(plan, **kwargs)
            if not getattr(rendered, 'held', False) and getattr(rendered, 'output_path', ''):
                from .auto_reel_validate import validate_output
                check = validate_output(rendered.output_path,
                    sum(s.end_ts-s.start_ts for s in plan.segments))
                if not check['ok']:
                    return story_composer.ComposeResult(plan=plan, held=True,
                        hold_reason=check['reason'])
                if config.auto_reels_portrait_active_for(gym) and render_fn is None:
                    import json, hashlib
                    from . import db
                    evidence=json.loads(Path(rendered.output_path).with_suffix('.framing.json').read_text())
                    with open(rendered.output_path,'rb') as media:
                        evidence['output_sha256']=hashlib.file_digest(media,'sha256').hexdigest()
                    evidence['request_id']=request.get('id')
                    if not db.kv_is_durable():
                        raise ValueError('Portrait framing evidence requires persistent worker storage')
                    db.kv_set('auto_reel_framing:'+gym+':'+str(request.get('id')),json.dumps(evidence))
            return rendered
        prepared={**request,'_moments_prepared':True,'template':'hype_montage','brief':copy['overlay'],
                  'ask':copy['ask'],'_automatic_copy':copy,
                  'asset_ids':[c['asset_id'] for c in refined]}
        if output_dir is None:
            final_tmp=tempfile.mkdtemp(prefix='autoreel_final_')
        result=story_studio.create_story(prepared,candidates=refined,assets_by_id=assets_by_id,
            analysis=analysis,store=store,music_library=music_library,render_fn=render,
            output_dir=output_dir or final_tmp,now=now,cal_store=cal_store)
        result['moment_diagnostics']=diagnostics
        result['copy_provenance']=copy.get('provenance', {})
        return result
    except Exception as exc:
        return held(f'Automatic reel preparation failed ({type(exc).__name__})')
    finally:
        story_candidates.cleanup(tmp)
        story_candidates.cleanup(final_tmp)
