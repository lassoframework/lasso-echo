"""Lead acceptance checks through Story -> studio -> engine -> pixel reviewer."""
import base64
import io
import json

import pytest
from PIL import Image

from agent import creative_studio, image_engine, infographic_review, ops_alerts, stories
from agent.accounts import Account, Platform
from agent.drafter import Draft, DraftStatus


@pytest.mark.parametrize('eventual_pass', [False, True])
def test_story_retries_use_reviewed_pixels_and_one_terminal_alert(monkeypatch, tmp_path, eventual_pass):
    for name in ('AGENT_STORIES_ENABLED', 'AGENT_NANO_ENABLED', 'AGENT_LASSO_INFOGRAPHIC_QUALITY'):
        monkeypatch.setenv(name, 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-key')
    monkeypatch.setattr(creative_studio, '_default_client', lambda: None)
    monkeypatch.setattr(creative_studio, '_reference_images_for', lambda *a, **k: ('', []))
    monkeypatch.setattr(stories, '_story_out_path', lambda *a, **k: str(tmp_path/'story.png'))
    monkeypatch.setattr(stories.schedule, 'should_post_on', lambda day: True)
    alerts, hosted, requests, reviewed = [], [], [], []
    monkeypatch.setattr(ops_alerts, 'alert', lambda message, **kw: alerts.append(message))
    def host(path, account, **kw):
        hosted.append(path)
        return 'https://cdn.example.test/story.png'
    monkeypatch.setattr(stories.media_host, 'host_media', host)
    images = []
    for color in ('red', 'green', 'blue'):
        data = io.BytesIO()
        Image.new('RGB', (1080, 1920), color).save(data, format='PNG')
        images.append(data.getvalue())

    def provider(self, payload):
        if 'tools' in payload:
            requests.append(payload)
            raw = images[len(requests)-1]
            return 200, json.dumps({'id':f'gen-{len(requests)}', 'output':[
                {'type':'image_generation_call', 'result':base64.b64encode(raw).decode()}]})
        content = payload['input'][0]['content']
        reviewed.append(base64.b64decode(next(p['image_url'].split(',',1)[1]
                                             for p in content if p['type']=='input_image')))
        passes = eventual_pass and len(reviewed) == 2
        grade = dict(scores=dict(infographic_review.WEIGHTS), copy_complete=True,
                     copy_accurate=True, placement_safe=passes,
                     placement_violations=[] if passes else [{
                         'element': 'destination URL',
                         'bounds': 'x=0.10 to 0.50, y=0.84 to 0.87',
                         'correction': 'Move the complete URL above y=1632 pixels.',
                     }],
                     issues=[] if passes else [{'severity':'major',
                         'correction':'Move the complete URL above y=1632 pixels.'}])
        return 200, json.dumps({'id':f'review-{len(reviewed)}', 'output':[
            {'content':[{'type':'output_text','text':json.dumps(grade)}]}]})
    monkeypatch.setattr(image_engine.AstraImageEngine, '_post', provider)
    account = Account(key='lasso_ig', display_name='LASSO', platform=Platform.INSTAGRAM,
                      token_env='TEST_TOKEN', target_id_env='TEST_TARGET')
    feed = Draft(draft_id='feed-test', account_key='lasso_ig', platform='instagram',
        caption='Approved copy', hashtags=[], creative_path='nano_test.png',
        creative_public_url='https://cdn.example.test/feed.png',
        scheduled_for='2027-07-07T18:30:00-04:00', status=DraftStatus.PENDING,
        source_fragments=['Approved headline', 'Approved supporting fact'],
        infographic_copy={'headline':'Approved headline','facts':['Approved supporting fact'],
                          'cta':'Save this post.','footer':'LASSOFRAMEWORK.COM'})
    draft = stories.build_story_draft(account, '2027-07-07', feed_draft=feed)
    expected = 2 if eventual_pass else 3
    assert len(requests) == len(reviewed) == expected
    assert reviewed == images[:expected]
    for i in range(1, expected):
        assert requests[i]["tools"][0]["action"] == "edit"
        content = requests[i]['input'][0]['content']
        attached = [base64.b64decode(p['image_url'].split(',',1)[1])
                    for p in content if p['type']=='input_image']
        assert attached == [reviewed[i-1]], 'Retry must edit the last rejected candidate'
        assert 'Move the complete URL' in str(content)
    if eventual_pass:
        assert draft.status == DraftStatus.PENDING
        assert draft.is_story
        assert (tmp_path/'story.png').read_bytes() == reviewed[-1]
        assert len(hosted) == 1
        assert alerts == []
    else:
        assert draft is None
        assert not (tmp_path/'story.png').exists()
        assert hosted == []
        assert len(alerts) == 1
        assert len(alerts[0]) < 1000
        assert 'came back dark' not in alerts[0]
