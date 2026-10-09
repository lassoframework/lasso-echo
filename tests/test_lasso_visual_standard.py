"""Owned LASSO visual standard: loader fail-closed, brief wiring, review style
gate, and evidence reuse refusal. Offline; no rendering, no network."""
import hashlib
import json

import pytest

from agent import astra_prompt, infographic_evidence, infographic_review
from agent import lasso_visual_standard as lvs


def _write_review(tmp_path, image_bytes=b'approved pixels', **overrides):
    image = tmp_path / 'card.png'
    image.write_bytes(image_bytes)
    evidence = dict(
        policy_version=infographic_evidence.POLICY_VERSION,
        brain_snapshot=infographic_evidence.brain_snapshot(),
        brief_model='gpt-6-astra', grade_status='PASS',
        response_id='generation', review_response_id='review',
        style_conformant=True, style_violations=[],
        visual_standard_version=lvs.VERSION,
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        infographic_copy={'headline': 'Hook', 'facts': ['Fact'],
                          'cta': 'Save', 'footer': 'lassoframework.com'})
    evidence.update(overrides)
    (tmp_path / 'card.png.review.json').write_text(json.dumps(evidence))
    return image


# ---- the loader -------------------------------------------------------------

def test_real_guide_loads_nonempty_and_matches_version():
    text = lvs.load_guidance()
    assert lvs.VERSION in text
    assert lvs.GUIDE_PATH.name == 'lasso_visual_standard.md'
    assert len(lvs.guide_sha256()) == 64


def test_missing_guide_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(lvs, 'GUIDE_PATH', tmp_path / 'absent.md')
    with pytest.raises(OSError):
        lvs.load_guidance()
    with pytest.raises(OSError):
        astra_prompt.build_content_brief('Hook', ['Fact'])


def test_empty_or_malformed_guide_fails_closed(monkeypatch, tmp_path):
    empty = tmp_path / 'empty.md'
    empty.write_text('   \n', encoding='utf-8')
    monkeypatch.setattr(lvs, 'GUIDE_PATH', empty)
    with pytest.raises(ValueError):
        lvs.load_guidance()
    bad = tmp_path / 'bad.md'
    bad.write_bytes(b'\xff\xfe invalid utf-8 \x80')
    monkeypatch.setattr(lvs, 'GUIDE_PATH', bad)
    with pytest.raises(UnicodeDecodeError):
        lvs.load_guidance()


# ---- brief direction --------------------------------------------------------

def test_lasso_brief_embeds_allowed_and_rejected_direction():
    brief = astra_prompt.build_content_brief('Hook', ['Fact'])
    low = brief.lower()
    for rejected in ('neon light trails', 'glowing orange or blue ribbons',
                     'futuristic machine styling', 'holograms', 'circuits'):
        assert rejected in low
    for allowed in ('dark navy', 'warm lighting', 'icons'):
        assert allowed in low
    assert 'Futuristic graphics are welcome' not in brief


def test_lasso_corrective_brief_embeds_the_same_standard():
    brief = astra_prompt.build_content_brief(
        'Hook', ['Fact'], surface='Story', pixels='1080x1920',
        corrective='Remove the neon trail.')
    assert 'VISUAL STANDARD' in brief
    assert 'EDIT the attached rejected candidate image' in brief
    assert 'Futuristic graphics are welcome' not in brief


def test_client_gym_brief_unaffected_by_lasso_standard():
    brief = astra_prompt.build_content_brief(
        'Hook', ['Fact'], _brand_name='gym',
        _palette_section='BRAND COLORS, VERIFIED FOR THIS GYM: #112233.')
    assert '#112233' in brief
    assert 'VISUAL STANDARD' not in brief
    assert 'neon light trails' not in brief.lower()


# ---- the review style gate --------------------------------------------------

class _Vision:
    def __init__(self, response):
        self.response = response

    def ask_image(self, image_bytes, question):
        return self.response


def _review_json(**overrides):
    data = dict(scores=dict(infographic_review.WEIGHTS), copy_complete=True,
                copy_accurate=True, placement_safe=True,
                placement_violations=[], issues=[],
                style_conformant=True, style_violations=[])
    data.update(overrides)
    return json.dumps(data)


def test_high_score_with_style_violation_fails_closed():
    grade = infographic_review.evaluate(
        b'candidate', owned_lasso=True, headline='Hook', facts=['Fact'],
        vision_client=_Vision(_review_json(
            style_conformant=False,
            style_violations=[{'element': 'glowing orange ribbon',
                               'correction': 'Remove the glowing ribbon entirely.'}])))
    assert grade.status == 'FAIL'
    assert not grade.passed
    assert grade.style_conformant is False
    assert grade.style_violations[0]['element'] == 'glowing orange ribbon'
    assert grade.visual_standard_version == lvs.VERSION
    assert 'glowing ribbon' in grade.reason


def test_conformant_clean_review_passes_and_surfaces_style_evidence():
    grade = infographic_review.evaluate(
        b'candidate', owned_lasso=True, headline='Hook', facts=['Fact'],
        vision_client=_Vision(_review_json()))
    assert grade.passed
    assert grade.style_conformant is True
    assert grade.style_violations == []
    assert grade.visual_standard_version == lvs.VERSION


def test_missing_style_evidence_fails_closed_and_never_claims_true():
    data = json.loads(_review_json())
    del data['style_conformant']
    grade = infographic_review.evaluate(
        b'candidate', owned_lasso=True, headline='Hook', facts=['Fact'],
        vision_client=_Vision(json.dumps(data)))
    assert grade.status == 'UNGRADED'
    assert not grade.passed
    assert grade.style_conformant is False


def test_contradictory_style_evidence_fails_closed():
    grade = infographic_review.evaluate(
        b'candidate', owned_lasso=True, headline='Hook', facts=['Fact'],
        vision_client=_Vision(_review_json(
            style_conformant=True,
            style_violations=[{'element': 'circuit traces',
                               'correction': 'Remove the circuits.'}])))
    assert grade.status == 'UNGRADED'
    assert not grade.passed
    assert grade.style_conformant is False


# ---- evidence reuse ---------------------------------------------------------

def test_reviewed_asset_accepts_current_v2_conformant_evidence(tmp_path):
    image = _write_review(tmp_path)
    assert infographic_evidence.reviewed_asset(image)


def test_reviewed_asset_refuses_stale_v1_policy(tmp_path):
    image = _write_review(tmp_path,
                          policy_version='lasso-astra-2026-09-14-punctuation-v1')
    assert infographic_evidence.reviewed_asset(image) is None


def test_reviewed_asset_refuses_style_violation_or_nonconformance(tmp_path):
    image = _write_review(tmp_path, style_conformant=False)
    assert infographic_evidence.reviewed_asset(image) is None
    image = _write_review(tmp_path, style_violations=[
        {'element': 'hologram', 'correction': 'Remove the hologram.'}])
    assert infographic_evidence.reviewed_asset(image) is None
    image = _write_review(tmp_path, visual_standard_version='stale-v0')
    assert infographic_evidence.reviewed_asset(image) is None


def test_reviewed_asset_refuses_digest_and_source_cache_mismatch(tmp_path):
    image = _write_review(tmp_path)
    # replaced bytes: digest gate refuses
    image.write_bytes(b'replaced pixels')
    assert infographic_evidence.reviewed_asset(image) is None
    # stale brain snapshot (missing the current guide hash): source gate refuses
    image.write_bytes(b'approved pixels')
    stale = dict(infographic_evidence.brain_snapshot())
    stale.pop(lvs.GUIDE_KEY, None)
    _write_review(tmp_path, brain_snapshot=stale)
    assert infographic_evidence.reviewed_asset(image) is None


def test_brain_snapshot_binds_the_loaded_guide_hash():
    snapshot = infographic_evidence.brain_snapshot()
    assert snapshot[lvs.GUIDE_KEY] == lvs.guide_sha256()


@pytest.mark.parametrize('contents', [None, b'   \n', b'\xff invalid utf8'])
def test_required_guide_failure_stops_actual_reviewer_request(monkeypatch, tmp_path, contents):
    path = tmp_path / 'required-guide.md'
    if contents is not None:
        path.write_bytes(contents)
    monkeypatch.setattr(lvs, 'GUIDE_PATH', path)

    class NeverRequested:
        def ask_image(self, image_bytes, question):
            pytest.fail('Unavailable guide must stop the paid vision request')

    result = infographic_review.evaluate(
        b'candidate', owned_lasso=True, headline='Hook', facts=['Fact'], vision_client=NeverRequested())
    assert result.status == 'UNGRADED'
    assert result.passed is False
    assert result.style_conformant is False


def test_reviewer_consumes_current_guide_text(monkeypatch, tmp_path):
    path = tmp_path / 'guide.md'
    monkeypatch.setattr(lvs, 'GUIDE_PATH', path)
    questions = []

    class Capture:
        def ask_image(self, image_bytes, question):
            questions.append(question)
            return _review_json()

    for marker in ('First physical paper direction', 'Revised warm workshop direction'):
        path.write_text(lvs.VERSION + '\n' + marker, encoding='utf-8')
        infographic_review.evaluate(
            b'candidate', owned_lasso=True, headline='Hook', facts=['Fact'], vision_client=Capture())
        assert marker in questions[-1]
    assert 'First physical paper direction' not in questions[-1]


def test_guide_keeps_owned_campaigns_and_existing_source_authority():
    guide = lvs.load_guidance()
    assert 'including owned book and Growth Summit cards' in guide
    assert 'approved dated Summit and editorial catalogs' in guide
    assert 'approved book source packets' in guide
    assert 'Client gyms and unrelated brands keep their own visual standards' in guide
    assert 'sci-fi dashboard' in guide and 'chrome' in guide
    assert 'physical metallic objects' in guide


def test_client_taste_contract_is_preserved_without_owned_guide(monkeypatch, tmp_path):
    monkeypatch.setattr(lvs, 'GUIDE_PATH', tmp_path / 'absent-owned-guide.md')
    brief = astra_prompt.build_content_brief(
        'Hook', ['Fact'], _brand_name='Verified client gym',
        _palette_section='VERIFIED GYM PALETTE #112233')
    assert ('VISUAL TASTE: the user approves a varied mix of editorial, human, tactile '
            'and futuristic designs. Futuristic graphics are welcome when they explain '
            'the content. Choose freely without forcing every card into one style.') in brief
    assert '#112233' in brief
    assert 'VISUAL STANDARD' not in brief
