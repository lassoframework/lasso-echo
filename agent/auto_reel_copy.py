"""Gym-focused reel copy from the approved brand bible and existing approved ask.

Footage selects relevance; it is never narrated as copy. Claims come from gym
positioning, not visual guesses. Missing sources or failed checks hold the reel.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import date, timedelta
from types import SimpleNamespace

from . import copy_gate, post_quality, rotation

_WORDS = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.I)
_NARRATION = re.compile(r"\b(?:a|the|one)\s+(?:person|man|woman|athlete)\b|"
                        r"\b(?:people|members|participants|athletes)\s+(?:are\s+)?"
                        r"(?:holding|hold|holds|lifting|lift|lifts|carrying|carry|carries|squatting|squat|squats|standing|stand|walking|walk)\b|"
                        r"\b(?:is doing|in the video|in this video|the footage shows|the camera)\b", re.I)
_ASK_START = re.compile(r'^(?:book|message|dm|save|follow|join|schedule|contact|visit|call|tag|share)\b', re.I)
_DATED_ASK = re.compile(r'\b(?:today|tonight|tomorrow|yesterday|this|next|last|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december|weekend|limited|ends|expires|deadline)\b|\d', re.I)
_URL = re.compile(r'https?://|www\.|\b\w+\.(?:com|ca|co|net|org)\b|@', re.I)


def _clean(text):
    return re.sub(r'[ \t]+', ' ', copy_gate.scrub(str(text)).replace('-', ' ')).strip()


def _parse_json(text):
    """Accept JSON or one complete JSON fence, never surrounding prose."""
    value=str(text or '').strip()
    fenced=re.fullmatch(r'```(?:json)?[ \t]*\n([\s\S]*?)\n```',value,re.I)
    if fenced:
        value=fenced.group(1)
    return json.loads(value)


def _parse_review_json(text):
    """Review-only compatibility: one fenced verdict may have plain rationale.

    Generation remains strict. Never merge a second object, array or fenced
    verdict into the first. Rationale is not published or interpreted as copy.
    The caller still enforces exact schema, grounded is True, and issues is [].
    """
    try:
        return _parse_json(text)
    except (ValueError, TypeError):
        value=str(text or '').strip()
        match=re.fullmatch(r'```(?:json)?[ \t]*\n([\s\S]*?)\n```([\s\S]+)',value,re.I)
        if not match or re.search(r'[{}\[\]`]',match.group(2)):
            raise ValueError('Review must contain one unambiguous JSON verdict')
        return json.loads(match.group(1))


def _held(reason):
    return {'held':True, 'hold_reason':reason, 'caption':'', 'overlay':'',
            'overlay_beats':[], 'ask':'', 'hashtags':[], 'provenance':{}}


def _resolve(gym):
    from .accounts import get_account
    from .client_media_sync import _resolve_client_voice_path
    from .voice import load_voice
    account = get_account(f'{gym}_ig')
    if account is None:
        return None, None
    return account, load_voice(_resolve_client_voice_path(gym, account.voice_doc))


def _brand_content(raw):
    """Use approved identity/avatar/tone content, never TODOs or internal rules."""
    lines = []
    include = True
    for line in raw.splitlines():
        if re.match(r'##\s', line):
            include = bool(re.search(r'who .*is|who .*to|voice and tone|positioning|experience|services', line, re.I))
            continue
        if (not include or line.startswith(('#','>')) or not line.strip()
                or re.search(r'never|guardrail|TODO|https?://|website:|instagram:|facebook:', line, re.I)):
            continue
        lines.append(line.strip())
    return '\n'.join(lines).strip()


def resolve_approved_ask(gym, *, store=None, today=None):
    """Read only: latest suitable exact ask from this gym's approved calendar.

    At most 1000 tenant rows through existing bounded rows_in_range. A 90-day
    lookback and next 31 calendar days include current approved forward posts.
    No fallback to another gym, pending copy, URLs or ambiguous multiple asks.
    """
    try:
        from .portal_calendar_store import SupabaseCalendarStore
        now = today or date.today()
        if not isinstance(now, date):
            return None
        start, end = now-timedelta(days=90), now+timedelta(days=31)
        rows = (store or SupabaseCalendarStore()).rows_in_range(gym,start.isoformat(),end.isoformat())
        valid = []
        for row in rows[:1000]:
            if row.get('gym_id') != gym or row.get('status') not in ('approved','published') or not row.get('id'):
                continue
            try:
                day = date.fromisoformat(str(row.get('post_date') or ''))
            except ValueError:
                continue
            if not start <= day <= end:
                continue
            caption = row.get('caption')
            if not isinstance(caption,str) or not caption.strip() or _URL.search(caption):
                continue
            # Ignore hashtag-only tails, preserving the literal sentence bytes.
            text = re.sub(r'(?:\s+#[A-Za-z0-9_]+)+\s*$', '', caption.strip()).strip()
            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', text) if s.strip()]
            asks = [s for s in sentences if _ASK_START.match(s)]
            if len(asks) != 1 or asks[0] != sentences[-1]:
                continue
            ask = asks[0]
            if _DATED_ASK.search(ask):
                continue
            if len(ask)>120 or not ask.endswith(('.', '!')) or copy_gate.violations(ask) or '-' in ask:
                continue
            # Two instructions in one sentence are also ambiguous.
            if re.search(r'\b(?:and|or|then)\s+(?:book|message|dm|save|follow|join|schedule|contact|visit|call|tag|share)\b', ask,re.I):
                continue
            valid.append((day,str(row['id']),ask,row['status']))
        if not valid:
            return None
        day,row_id,ask,status = max(valid,key=lambda item:(item[0],item[1]))
        return {'text':ask,'provenance':{'gym':gym,'source_id':row_id,'status':status,
                                       'post_date':day.isoformat(),'source':'content_calendar'}}
    except Exception:
        return None


def _write(account, source, voice, creative_key, verified):
    """Existing caption transport, with gym positioning and a grounded-copy check.

    Maximum three generation attempts and one review per structurally valid
    attempt. The separate copy check is a grounding gate, not a quality grade.
    """
    from .drafter import _call_llm_caption
    system = (
        'Write a polished gym reel about the GYM, never narrate the person or movements on screen. '
        'Approved gym content supplies positioning, experience, coaching, audience and benefits. '
        'Footage is only relevance context and never a source of marketing claims. '
        'Use the gym voice. No invented results, numbers, names, offers, guarantees or schedules. '
        'Return JSON with hook and support strings, each 3 to 6 words and at most 40 characters. '
        'Hook: a compact gym positioning idea. Support: a distinct approved experience or benefit. '
        'Never say a person is holding/lifting/doing something. No equipment inventory. '
        'No call to action, hashtags, headings or dashes. The application appends the exact approved ask.')
    payload={'approved_gym_content':source.text,'footage_relevance_only':verified['verified_details']}
    for _ in range(3):
        raw=(_call_llm_caption(system,json.dumps(payload,ensure_ascii=False)) or '').strip()
        try:
            parts=_parse_json(raw)
            if not isinstance(parts,dict) or set(parts)!={'hook','support'}:
                raise ValueError('Return hook and support strings')
            if not all(isinstance(v,str) for v in parts.values()):
                raise ValueError('Hook and support must be text')
            beats=[_clean(parts[k]) for k in ('hook','support')]
            from .auto_reel_render import fit_text
            try:
                for beat in beats:
                    fit_text(beat)
            except ValueError:
                raise ValueError('Shorten the text to 3 to 6 words and at most 40 characters so every beat fits two readable lines')
            if any(not 3<=len(_WORDS.findall(b))<=6 or len(b)>40 for b in beats):
                raise ValueError('Shorten each beat to 3 to 6 words and at most 40 characters')
            body='\n\n'.join(beats)
            if _NARRATION.search(body):
                raise ValueError('Write about gym positioning, not what anyone is doing')
            review_raw=_call_llm_caption(
                'Check marketing copy strictly against approved gym content. '
                'Reject unsupported benefits, outcomes, guarantees, identities, offers, numbers, '
                'urgency, or literal narration of people in footage. Paraphrased approved '
                'gym positioning and experience are allowed. Return only JSON '
                '{"grounded":true|false,"issues":["specific issue"]}.',
                json.dumps({'approved_gym_content':source.text,'copy':body},ensure_ascii=False))
            review=_parse_review_json(review_raw)
            if (not isinstance(review,dict) or set(review)!={'grounded','issues'}
                    or review.get('grounded') is not True
                    or review.get('issues') != []):
                raise ValueError('Grounding review rejected copy: '+str(review.get('issues') if isinstance(review,dict) else 'invalid review'))
            return body+'\n\n'+voice.ctas[0],list(voice.hashtags[:5])
        except (ValueError,TypeError) as exc:
            payload['previous_output']=raw
            payload['correction']=str(exc)
    raise ValueError('Gym copy failed bounded generation and grounding checks')


def prepare_copy(gym, analysis, *, voice=None, account=None, writer=None,
                 resolver=None, creative_key='automatic_reel', approved_ask=None,
                 ask_provenance=None, ask_resolver=None):
    """Return gym caption, two short overlay_beats, exact approved ask, provenance.

    Tests inject writer/resolver; the server defaults resolve the existing active
    voice and read only tenant-approved CTA history when its CTA section is empty.
    """
    gym=str(gym or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_]+',gym):
        return _held('A valid gym account is required')
    if gym.endswith(('_ig','_fb')):
        gym=gym[:-3]
    try:
        if voice is None or account is None:
            a,v=(resolver or _resolve)(gym);account=account or a;voice=voice or v
        if voice is None or not str(getattr(voice,'raw','')).strip() or getattr(voice,'auto_drafted',True):
            return _held('An approved gym voice document is required')
        if account is None or account.key not in (gym,gym+'_ig',gym+'_fb'):
            return _held('Gym voice account does not match the footage tenant')
        if not isinstance(analysis,dict) or analysis.get('analysis_failed'):
            return _held('Verified selected footage is required')
        confidence=analysis.get('confidence')
        if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or not .75<=confidence<=1:
            return _held('Selected footage confidence is too low')
        details=analysis.get('subjects')
        if not isinstance(details,list) or not 1<=len(details)<=20 or any(not isinstance(s,str) or not s.strip() or len(s)>1500 for s in details):
            return _held('Verified footage details are missing')
        brand=_brand_content(voice.raw)
        if len(_WORDS.findall(brand))<8:
            return _held('Approved gym positioning content is missing')
        ctas=list(dict.fromkeys(_clean(s) for s in getattr(voice,'ctas',[])
                               if str(s).strip() and _clean(s) in _clean(voice.raw)))
        ask_source={'kind':'approved_voice'}
        if not ctas and approved_ask is None:
            found=(ask_resolver or resolve_approved_ask)(gym)
            if found:
                approved_ask=found.get('text');ask_provenance=found.get('provenance')
        if not ctas and approved_ask is not None:
            if (not isinstance(approved_ask,str) or not approved_ask.strip()
                    or not isinstance(ask_provenance,dict) or ask_provenance.get('gym')!=gym
                    or not ask_provenance.get('source_id')
                    or ask_provenance.get('status') not in ('approved','published')):
                return _held('Existing call to action needs verified gym source provenance')
            if _DATED_ASK.search(approved_ask):
                return _held('Historical call to action must be evergreen; approve current offer in gym voice')
            ctas=[_clean(approved_ask)];ask_source={'kind':'existing_gym_source',**ask_provenance}
        if not ctas:
            return _held('No approved gym call to action is available')
        if any(len(s)>120 or copy_gate.violations(s) or _URL.search(s) for s in ctas):
            return _held('Approved call to action violates copy limits')
        writer_voice=copy.copy(voice);writer_voice.ctas=ctas
        source=SimpleNamespace(text=brand,category='approved_gym_positioning')
        verified={'ok':True,'verified_details':details,'bucket':'unknown'}
        generated=(writer or _write)(account,source,writer_voice,creative_key,verified)
        if not isinstance(generated,(tuple,list)) or len(generated)!=2:
            return _held('Writer returned invalid gym copy')
        caption,tags=generated
        if not isinstance(caption,str) or not isinstance(tags,list):
            return _held('Writer returned invalid gym copy')
        caption=_clean(caption)
        if not 12<=len(_WORDS.findall(caption))<=75 or not 40<=len(caption)<=500:
            return _held('Gym caption must be concise and complete')
        body=caption;used=[];count=0
        for cta in ctas:
            pattern=re.compile(r'(?<!\w)'+re.escape(cta)+r'(?!\w)',re.I)
            found=pattern.findall(body)
            if found:
                count+=len(found);used.append(cta);body=pattern.sub('',body)
        if count!=1:
            return _held('Caption needs exactly one approved call to action')
        if _NARRATION.search(body):
            return _held('Reel copy must be about the gym, never footage narration')
        numeric_sentences=[s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+',body) if re.search(r'\d',s)]
        if any(_clean(s).lower() not in _clean(brand).lower() for s in numeric_sentences):
            return _held('Caption contains an unsupported numeric statement')
        if not rotation.is_gate_clean(body,approved_claims=[brand]):
            return _held('Caption contains an unsupported figure or claim')
        if not rotation.caption_output_gate_clean(body,brand,verified=verified,photo_hint=''):
            return _held('Caption adds unsupported timing, urgency or audience claims')
        from .client_media_sync import _parse_banned_from_bible
        if copy_gate.violations(caption) or post_quality.avatar_breach(caption,gym=gym):
            return _held('Caption violates gym copy rules')
        if any(re.search(r'\b'+re.escape(word)+r'\b',caption,re.I) for word in _parse_banned_from_bible(voice.raw)):
            return _held('Caption includes banned gym language')
        beats=[b.strip() for b in re.split(r'\n\s*\n',body.strip()) if b.strip()]
        if len(beats)!=2:
            beats=[b.strip() for b in re.split(r'(?<=[.!?])\s+',body.strip()) if b.strip()]
        if len(beats)!=2 or any(not 3<=len(_WORDS.findall(b))<=6 or len(b)>40 for b in beats):
            return _held('Reel needs two compact gym positioning beats')
        from .auto_reel_render import fit_text
        try:
            for beat in beats+[used[0]]:
                fit_text(beat)
        except ValueError:
            return _held('Complete reel copy cannot fit two readable lines')
        if any(t not in voice.hashtags for t in tags):
            return _held('Hashtags must come from the approved gym voice')
        provenance={'method':'approved_gym_positioning_v2','gym':gym,
                    'voice_sha256':hashlib.sha256(voice.raw.encode()).hexdigest(),
                    'facts_sha256':hashlib.sha256(brand.encode()).hexdigest(),
                    'approved_gym_content':brand,'footage_relevance_only':details,
                    'approved_cta':used[0],'ask_source':ask_source}
        return {'held':False,'hold_reason':'','caption':caption,'overlay':beats[0],
                'overlay_beats':beats,'ask':used[0],'hashtags':tags,'provenance':provenance}
    except Exception as exc:
        return _held(f'Automatic gym copy preparation failed ({type(exc).__name__}); no fallback was staged')
