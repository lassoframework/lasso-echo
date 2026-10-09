"""Owned style requirements never become client tenant review requirements."""
import json
import pytest
from agent import infographic_review as review
from agent.generated_infographic_preparation import GymPaletteReviewer


def evidence(**changes):
    payload=dict(scores=dict(review.WEIGHTS),copy_complete=True,copy_accurate=True,
                 placement_safe=True,placement_violations=[],issues=[])
    payload.update(changes)
    return json.dumps(payload)

class Vision:
    response_id='review-scope'
    def __init__(self,result):self.result=result;self.questions=[]
    def ask_image(self,image,question):self.questions.append(question);return self.result


def test_client_original_palette_and_placement_without_lasso_guide_or_styles(monkeypatch):
    def forbidden():raise AssertionError('client must not load owned guide')
    monkeypatch.setattr(review.lasso_visual_standard,'load_guidance',forbidden)
    vision=Vision(evidence(palette_matches=True));palette={'colors':['#123456','#abcdef']}
    grade=review.evaluate(b'client-pixels',headline='Gym approved headline',facts=['Gym approved fact'],
                         vision_client=GymPaletteReviewer(vision,palette))
    assert grade.passed and grade.status=='PASS'
    assert not hasattr(grade,'style_conformant') and not hasattr(grade,'visual_standard_version')
    question=vision.questions[0]
    assert '#123456' in question and '#abcdef' in question
    assert 'placement_violations' in question and 'style_conformant' not in question
    assert 'AUTHORITATIVE OWNED VISUAL GUIDANCE' not in question

@pytest.mark.parametrize('changes',[
 {'placement_safe':False}, {'copy_complete':False},
 {'placement_violations':[{'element':'headline','bounds':'x=0','correction':'Move inwards'}]},
 {'issues':[{'severity':'major','correction':'Use correct gym palette'}]}])
def test_client_original_safety_gates_remain(changes):
    grade=review.evaluate(b'client-pixels',headline='H',facts=['F'],vision_client=Vision(evidence(**changes)))
    assert not grade.passed


def test_owned_scope_requires_actual_style_and_guide(monkeypatch):
    monkeypatch.setattr(review.lasso_visual_standard,'load_guidance',lambda:'Actual owned editorial guide')
    no_styles=Vision(evidence())
    grade=review.evaluate(b'owned-pixels',headline='H',facts=['F'],owned_lasso=True,vision_client=no_styles)
    assert grade.status=='UNGRADED' and not grade.passed and grade.style_conformant is False
    assert 'Actual owned editorial guide' in no_styles.questions[0]
    assert 'style_conformant' in no_styles.questions[0]
    good=Vision(evidence(style_conformant=True,style_violations=[]))
    grade=review.evaluate(b'owned-pixels',headline='H',facts=['F'],owned_lasso=True,vision_client=good)
    assert grade.passed and grade.style_conformant is True
    assert grade.visual_standard_version==review.lasso_visual_standard.VERSION

@pytest.mark.parametrize('scope',[None,'true',1,{},[]])
def test_malformed_scope_refuses_before_vision(scope):
    vision=Vision(evidence(style_conformant=True,style_violations=[]))
    grade=review.evaluate(b'pixels',headline='H',facts=['F'],owned_lasso=scope,vision_client=vision)
    assert not grade.passed and grade.status=='UNGRADED' and not vision.questions

@pytest.mark.parametrize('failure',[FileNotFoundError(),ValueError('empty guide'),UnicodeDecodeError('utf8',b'\xff',0,1,'invalid')])
def test_owned_missing_or_invalid_guide_stops_vision(monkeypatch,failure):
    def unavailable():raise failure
    monkeypatch.setattr(review.lasso_visual_standard,'load_guidance',unavailable)
    vision=Vision(evidence(style_conformant=True,style_violations=[]))
    grade=review.evaluate(b'owned',headline='H',facts=['F'],owned_lasso=True,vision_client=vision)
    assert grade.status=='UNGRADED' and not grade.passed and not vision.questions
    assert grade.style_conformant is False


@pytest.mark.parametrize('matches',[False,None,'true'])
def test_client_palette_refusal_remains_uncertain_or_false(matches):
    vision=Vision(evidence(palette_matches=matches))
    grade=review.evaluate(b'client',headline='H',facts=['F'],
       vision_client=GymPaletteReviewer(vision,{'colors':['#123456']}))
    assert not grade.passed and grade.status=='UNGRADED'
