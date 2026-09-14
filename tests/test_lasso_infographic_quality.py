import json
from agent import astra_prompt, config, infographic_review, image_engine


def test_content_brief_preserves_support_and_avoids_template_picker(monkeypatch):
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY', 'true')
    monkeypatch.setattr(astra_prompt, 'style_for', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('template picker called')))
    brief = astra_prompt.build_infographic_brief('The ad is only the start',
        ['Respond while interest is fresh.', 'Connect the response to their goal.'],
        cta='Explore The Full Gym', footer='lassoframework.com/fullgym', account_key='lasso_ig')
    assert 'Respond while interest is fresh.' in brief
    assert 'Explore The Full Gym' in brief
    assert 'lassoframework.com/fullgym' in brief
    assert 'do NOT render these sentences' not in brief
    assert 'COLOR LAW' not in brief


def test_quality_scope_is_lasso_only(monkeypatch):
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY', 'true')
    assert config.lasso_infographic_quality_enabled('lasso_fb')
    assert not config.lasso_infographic_quality_enabled('other_gym_ig')
    assert not config.lasso_infographic_quality_enabled(None)


class Vision:
    def __init__(self, response):
        self.response = response
    def ask_image(self, image_bytes, question):
        assert image_bytes == b'actual-candidate'
        assert 'supporting fact' in question
        return self.response


def review_response(**overrides):
    data = dict(scores=dict(infographic_review.WEIGHTS), copy_complete=True,
                copy_accurate=True, placement_safe=True, issues=[])
    data.update(overrides)
    return json.dumps(data)


def test_omitted_copy_blocks_even_perfect_aesthetic_score():
    grade = infographic_review.evaluate(b'actual-candidate', headline='Approved', facts=['Required fact'],
        vision_client=Vision(review_response(copy_complete=False)))
    assert not grade.passed
    assert 'omitted' in grade.reason


def test_incomplete_review_fails_closed():
    grade = infographic_review.evaluate(b'actual-candidate', headline='Approved', facts=['Required fact'],
        vision_client=Vision('{"scores":{}}'))
    assert grade.status == 'UNGRADED'
    assert not grade.passed


def test_major_issue_blocks_perfect_score():
    grade = infographic_review.evaluate(b'actual-candidate', headline='Approved', facts=['Required fact'],
        vision_client=Vision(review_response(issues=[{'severity':'major','correction':'Move clipped CTA inside frame'}])))
    assert not grade.passed
    assert 'clipped CTA' in grade.reason


def test_lasso_cannot_fall_back_to_gemini(monkeypatch):
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY', 'true')
    monkeypatch.setattr(image_engine, 'engine_chain', lambda *a: (_ for _ in ()).throw(AssertionError('fallback chain called')))
    def unavailable(*args):
        raise image_engine.ImageEngineError('unavailable')
    monkeypatch.setattr(image_engine.AstraImageEngine, 'generate', unavailable)
    marked = []
    monkeypatch.setattr(image_engine, 'mark_needs_human', lambda **kw: marked.append(kw))
    assert image_engine.generate_image('brief', account_key='lasso_ig', sleep=lambda _:None) is None
    assert len(marked) == 1


def test_portal_alias_is_in_quality_scope(monkeypatch):
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY', 'true')
    assert config.lasso_infographic_quality_enabled('lasso-framework-llc')


def test_source_selection_does_not_promote_arbitrary_claim():
    from agent.lasso_infographic_content import select_copy
    selected = select_copy('ads follow up guarantee 99999 new members')
    assert '99999' not in str(selected)
    assert selected['source_id'].startswith('lasso_now:')
    assert len(selected['source_hash']) == 64


def test_astra_review_sends_actual_candidate_and_benchmark_pixels():
    payloads = []
    def transport(url, headers, payload):
        payloads.append(payload)
        return 200, json.dumps({'id':'review-response','output':[{'content':[
            {'type':'output_text','text':review_response()}]}]})
    reviewer = infographic_review.AstraReviewer('test', references=[{'b64':'cmVm'}],transport=transport)
    result = infographic_review.evaluate(b'actual-candidate', headline='Approved',facts=['Required fact'],vision_client=reviewer)
    assert result.passed
    assert payloads[0]['model'] == 'gpt-6-astra'
    images = [part for part in payloads[0]['input'][0]['content'] if part['type']=='input_image']
    assert len(images)==2
    assert reviewer.response_id == 'review-response'


def test_story_safe_region_is_required_even_with_perfect_copy_and_score():
    class StoryVision:
        def ask_image(self, image_bytes, question):
            assert "y=0.17 to 0.80" in question
            return review_response(placement_safe=False)
    result = infographic_review.evaluate(b"candidate", headline="Approved", facts=["Fact"],
        surface="story", vision_client=StoryVision())
    assert not result.passed
    assert "safe region" in result.reason


def test_persisted_draft_retains_required_image_copy():
    from agent import store
    from agent.drafter import Draft
    d = Draft(draft_id="copy-roundtrip", account_key="lasso_ig", platform="instagram",
              caption="Caption", hashtags=[], creative_path="", creative_public_url="", scheduled_for="",
              infographic_copy={"headline":"Hook", "facts":["Fact"], "cta":"Save", "footer":"lassoframework.com"})
    assert store._from_dict(store._to_dict(d)).infographic_copy == d.infographic_copy


def test_story_placement_preserves_whole_panel_inside_safe_rectangle():
    import io
    from PIL import Image
    from agent.infographic_layout import story_frame
    image = Image.new("RGB", (1080,1350), "white")
    # A border at the extreme edges represents copy placed anywhere in the panel.
    from PIL import ImageDraw
    ImageDraw.Draw(image).rectangle((0,0,1079,1349), outline="black", width=20)
    raw=io.BytesIO();image.save(raw,format="PNG")
    result=Image.open(io.BytesIO(story_frame(raw.getvalue())))
    assert result.size==(1080,1920)
    # Every non-background pixel, including original panel border, is safely inset.
    bg=Image.new("RGB",result.size,result.getpixel((0,0)))
    from PIL import ImageChops
    box=ImageChops.difference(result,bg).getbbox()
    assert box[0]>=64 and box[1]>=326 and box[2]<=1015 and box[3]<=1536


def test_minor_polish_does_not_force_another_paid_render():
    grade = infographic_review.evaluate(b'actual-candidate', headline='Approved', facts=['Required fact'],
        vision_client=Vision(review_response(issues=[{'severity':'minor','correction':'Increase the decorative divider spacing.'}])))
    assert grade.passed
    assert 'divider' in grade.reason


def test_unclassified_issue_fails_closed():
    grade = infographic_review.evaluate(b'actual-candidate', headline='Approved', facts=['Required fact'],
        vision_client=Vision(review_response(issues=[{'correction':'Move clipped CTA'}])))
    assert grade.status == 'UNGRADED'


def test_display_copy_avoids_forbidden_punctuation_and_url_protocol():
    from agent.infographic_copy_style import display_text
    text = display_text("Next step: listen; ask. https://lassoframework.com/summit")
    assert ":" not in text and ";" not in text
    assert "lassoframework.com/summit" in text


def test_creative_preference_allows_futuristic_but_bans_punctuation():
    prompt = astra_prompt.build_content_brief("Hook", ["Fact"])
    assert "Futuristic graphics are welcome" in prompt
    assert "never render colons or semicolons" in prompt


def test_reuse_requires_matching_bytes_model_and_current_review(tmp_path):
    import hashlib
    from agent.infographic_evidence import reviewed_asset, POLICY_VERSION, brain_snapshot
    image = tmp_path / 'card.png'
    image.write_bytes(b'approved pixels')
    sidecar = tmp_path / 'card.png.review.json'
    evidence = dict(policy_version=POLICY_VERSION, brain_snapshot=brain_snapshot(), brief_model='gpt-6-astra',
        grade_status='PASS', response_id='generation', review_response_id='review',
        image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        infographic_copy={'headline':'Hook','facts':['Fact'],'cta':'Save','footer':'lassoframework.com'})
    sidecar.write_text(json.dumps(evidence))
    assert reviewed_asset(image)
    image.write_bytes(b'replaced pixels')
    assert reviewed_asset(image) is None
    image.write_bytes(b'approved pixels')
    evidence['brief_model'] = 'other-model'
    sidecar.write_text(json.dumps(evidence))
    assert reviewed_asset(image) is None
    evidence['brief_model'] = 'gpt-6-astra'
    evidence['infographic_copy']['headline'] = 'Hook: more'
    sidecar.write_text(json.dumps(evidence))
    assert reviewed_asset(image) is None


def test_reuse_rejects_changed_brain_snapshot(tmp_path, monkeypatch):
    import hashlib
    from agent import infographic_evidence as evidence_module
    image = tmp_path / 'card.png'
    image.write_bytes(b'pixels')
    snapshot = {'brain.md': 'original'}
    monkeypatch.setattr(evidence_module, 'brain_snapshot', lambda: snapshot)
    evidence = dict(policy_version=evidence_module.POLICY_VERSION,
        brain_snapshot=dict(snapshot), brief_model='gpt-6-astra', grade_status='PASS',
        response_id='generation', review_response_id='review',
        image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        infographic_copy={'headline':'Hook','facts':['Fact']})
    (tmp_path / 'card.png.review.json').write_text(json.dumps(evidence))
    assert evidence_module.reviewed_asset(image)
    snapshot['brain.md'] = 'updated'
    assert evidence_module.reviewed_asset(image) is None
