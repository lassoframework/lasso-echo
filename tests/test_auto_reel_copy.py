from datetime import date
from types import SimpleNamespace
import json
import pytest
from agent.auto_reel_copy import prepare_copy, resolve_approved_ask

FACTS={'confidence':.9,'subjects':['a person holding a barbell on their upper back']}
RAW='''# Demo Gym
## 1. Who Demo Gym is
Demo Gym helps busy adults fit sustainable fitness into real life.
## 2. Who we talk TO
Busy adults value expert coaching, structure and welcoming support.
## 3. Voice and tone
Warm, direct, approachable expertise. Fitness should feel achievable.
## 4. Hard guardrails
Words to NEVER use: shredded
### CTA rotation
Book a free consultation.
'''
VOICE=SimpleNamespace(raw=RAW,auto_drafted=False,ctas=['Book a free consultation.'],hashtags=['#Gym'])
ACCOUNT=SimpleNamespace(key='demo_ig')
GOOD='Fitness that fits your life.\n\nExpert coaching. Real support.\n\nBook a free consultation.'


def make(caption=GOOD,**kwargs):
    return prepare_copy('demo',kwargs.pop('analysis',FACTS),voice=kwargs.pop('voice',VOICE),
                        account=kwargs.pop('account',ACCOUNT),writer=kwargs.pop('writer',lambda *a:(caption,['#Gym'])),**kwargs)


def test_copy_is_about_gym_not_literal_footage():
    result=make()
    assert not result['held'],result
    assert result['overlay_beats']==['Fitness that fits your life.','Expert coaching. Real support.']
    assert result['ask']=='Book a free consultation.'
    assert result['provenance']['method']=='approved_gym_positioning_v2'
    assert 'busy adults' in result['provenance']['approved_gym_content']


@pytest.mark.parametrize('caption',[
    'A person holds a barbell.\n\nTheir arms are holding the weights.\n\nBook a free consultation.',
    'Members are carrying weights.\n\nExpert coaching. Real support.\n\nBook a free consultation.',
    'The footage shows a gym.\n\nExpert coaching. Real support.\n\nBook a free consultation.',
])
def test_literal_narration_is_rejected(caption):
    assert make(caption)['held']


def test_missing_approved_voice_and_cross_tenant_hold():
    assert make(voice=SimpleNamespace(raw=RAW,auto_drafted=True))['held']
    assert make(account=SimpleNamespace(key='other_ig'))['held']
    assert make(voice=None,resolver=lambda gym:(ACCOUNT,None))['held']


@pytest.mark.parametrize('analysis',[{}, {'confidence':float('nan'),'subjects':['x']},
                                    {'confidence':.4,'subjects':['x']}])
def test_unverified_media_holds(analysis):
    assert make(analysis=analysis)['held']


def test_unapproved_offer_stats_duplicate_ask_banned_copy_hold():
    assert make(GOOD.replace('Book a free consultation.','Join our new program.'))['held']
    assert make(GOOD.replace('Expert coaching. Real support.','Lose 20 pounds in one week.'))['held']
    assert make(GOOD+' Book a free consultation.')['held']
    assert make(GOOD.replace('Real support.','Get shredded.'))['held']


def test_dashes_scrubbed_and_long_hooks_hold():
    assert not make(GOOD.replace('Expert coaching.','Expert coaching —'))['held']
    assert make(GOOD.replace('Fitness that fits your life.','Fitness that fits your very busy and full daily life.'))['held']


def test_existing_ask_automatically_resolved_without_mutating_voice():
    voice=SimpleNamespace(**{**VOICE.__dict__,'ctas':[]})
    calls=[]
    def resolver(gym):
        calls.append(gym)
        return {'text':'Book a free consultation.','provenance':{'gym':gym,'source_id':'row','status':'approved','post_date':'2026-09-29'}}
    result=make(voice=voice,ask_resolver=resolver)
    assert not result['held'],result
    assert calls==['demo'] and voice.ctas==[]
    assert result['provenance']['ask_source']['source_id']=='row'
    assert make(voice=voice,ask_resolver=lambda gym:None)['held']


def test_explicit_ask_provenance_cannot_cross_tenants():
    voice=SimpleNamespace(**{**VOICE.__dict__,'ctas':[]})
    assert make(voice=voice,approved_ask='Book a free consultation.',
                ask_provenance={'gym':'other','source_id':'row','status':'approved'})['held']


@pytest.mark.parametrize('ask', ['Join us this Saturday.', 'Book before September ends.', 'Try 30 days free.', 'Join tomorrow.'])
def test_historical_asks_cannot_reuse_expired_or_numeric_offers(ask):
    voice=SimpleNamespace(**{**VOICE.__dict__,'ctas':[]})
    result=make(voice=voice,approved_ask=ask,
                ask_provenance={'gym':'demo','source_id':'row','status':'approved'})
    assert result['held']


def test_default_writer_uses_brand_and_reviews_grounding(monkeypatch):
    from agent import drafter
    from agent.auto_reel_copy import _write
    calls=[]
    def transport(system,user):
        calls.append((system,json.loads(user)))
        if len(calls)%2:
            return json.dumps({'hook':'Fitness that fits your life.','support':'Expert coaching. Real support.'})
        return '{"grounded":true,"issues":[]}'
    monkeypatch.setattr(drafter,'_call_llm_caption',transport)
    result=make(writer=_write)
    assert not result['held'],result
    assert len(calls)==2
    assert 'busy adults' in calls[0][1]['approved_gym_content']
    assert 'never narrate' in calls[0][0]


def test_grounding_rejection_has_bounded_retries_and_no_raw_fallback(monkeypatch):
    from agent import drafter
    from agent.auto_reel_copy import _write
    calls=[]
    def transport(system,user):
        calls.append(user)
        if len(calls)%2:
            return '{"hook":"Fitness that fits your life.","support":"Guaranteed results for every member."}'
        return '{"grounded":false,"issues":["Unsupported guarantee"]}'
    monkeypatch.setattr(drafter,'_call_llm_caption',transport)
    result=make(writer=_write)
    assert result['held'] and not result['caption']
    assert len(calls)==6


class Store:
    def __init__(self,rows):self.rows=rows;self.calls=[]
    def rows_in_range(self,*args):self.calls.append(args);return self.rows


def row(**changes):
    return dict({'gym_id':'demo','id':'row','post_date':'2026-09-29','status':'approved',
                 'caption':'Fitness that fits your life.\n\nBook a free consultation.'},**changes)


def test_history_resolver_tenant_date_bounds_and_exact_provenance():
    store=Store([row()]);result=resolve_approved_ask('demo',store=store,today=date(2026,9,14))
    assert result['text']=='Book a free consultation.'
    assert result['provenance']=={'gym':'demo','source_id':'row','status':'approved',
                                 'post_date':'2026-09-29','source':'content_calendar'}
    assert store.calls==[('demo','2026-06-16','2026-10-15')]


@pytest.mark.parametrize('changes',[
    {'gym_id':'other'},{'status':'pending'},{'post_date':'2026-01-01'},
    {'post_date':'2027-01-01'},{'id':''},{'caption':''},
    {'caption':'Visit https://example.com. Book a free consultation.'},
    {'caption':'Save this post. Book a free consultation.'},
    {'caption':'Book a consultation and message us.'},
    {'caption':'Fitness for busy adults.'},
    {'caption':'Book a consultation?'},
])
def test_history_resolver_rejects_untrusted_or_ambiguous_copy(changes):
    assert resolve_approved_ask('demo',store=Store([row(**changes)]),today=date(2026,9,14)) is None


def test_strict_json_fence_parser_rejects_surrounding_prose():
    from agent.auto_reel_copy import _parse_json
    assert _parse_json('```json\n{"ok":true}\n```')=={'ok':True}
    assert _parse_json('```\n{"ok":true}\n```')=={'ok':True}
    for raw in ('Here is JSON: {"ok":true}', '```json\n{"ok":true}\n```\nExplanation',
                '```python\n{"ok":true}\n```'):
        with pytest.raises(ValueError):
            _parse_json(raw)


def test_fenced_generation_and_grounding_review(monkeypatch):
    from agent import drafter
    from agent.auto_reel_copy import _write
    answers=iter(['```json\n{"hook":"Fitness that fits your life.","support":"Expert coaching. Real support."}\n```',
                  '```json\n{"grounded":true,"issues":[]}\n```'])
    monkeypatch.setattr(drafter,'_call_llm_caption',lambda *a:next(answers))
    result=make(writer=_write)
    assert not result['held'],result


def test_failure_reports_only_exception_class():
    def fail(*args):
        raise RuntimeError('sensitive internal diagnostic')
    result=make(writer=fail)
    assert 'RuntimeError' in result['hold_reason']
    assert 'sensitive' not in result['hold_reason']


def test_review_plain_rationale_allowed_but_generation_remains_strict():
    from agent.auto_reel_copy import _parse_json, _parse_review_json
    raw='```json\n{"grounded":true,"issues":[]}\n```\nThe copy follows approved gym positioning.'
    assert _parse_review_json(raw)=={'grounded':True,'issues':[]}
    with pytest.raises(ValueError):
        _parse_json(raw)
    for suffix in ('\n{"grounded":false,"issues":["unsupported"]}',
                   '\n```json\n{"grounded":false,"issues":[]}\n```',
                   '\n[{"grounded":false}]'):
        with pytest.raises(ValueError):
            _parse_review_json(raw+suffix)


def test_grounding_false_with_rationale_still_holds(monkeypatch):
    from agent import drafter
    from agent.auto_reel_copy import _write
    count=[]
    def call(system,user):
        count.append(user)
        if len(count)%2:
            return '{"hook":"Fitness that fits your life.","support":"Expert coaching. Real support."}'
        return '```json\n{"grounded":false,"issues":["Unsupported"]}\n```\nThe source does not support this claim.'
    monkeypatch.setattr(drafter,'_call_llm_caption',call)
    assert make(writer=_write)['held']
    assert len(count)==6


def test_actual_font_fit_rejects_long_support_before_grounding_review(monkeypatch):
    from agent import drafter
    from agent.auto_reel_copy import _write
    from agent.auto_reel_render import fit_text
    long='Welcoming welcoming welcoming welcoming welcoming welcoming welcoming welcoming community support.'
    with pytest.raises(ValueError):
        fit_text(long)
    calls=[]
    def transport(system,user):
        calls.append((system,json.loads(user)))
        if len(calls)==1:
            return json.dumps({'hook':'Fitness that fits your life.','support':long})
        if len(calls)==2:
            assert 'Shorten' in json.loads(user)['correction']
            return '{"hook":"Fitness that fits your life.","support":"Expert coaching. Real support."}'
        return '{"grounded":true,"issues":[]}'
    monkeypatch.setattr(drafter,'_call_llm_caption',transport)
    result=make(writer=_write)
    assert not result['held'],result
    assert len(calls)==3  # Two generations, only one grounding review.
    assert 'Write a polished gym reel' in calls[1][0]


def test_prepare_copy_enforces_font_fit_even_for_injected_writer(monkeypatch):
    from agent import auto_reel_render
    def fail(text):
        raise ValueError('Cannot fit')
    monkeypatch.setattr(auto_reel_render,'fit_text',fail)
    result=make()
    assert result['held'] and result['hold_reason']=='Complete reel copy cannot fit two readable lines'
