"""Story placement stays safe without replacing creative freedom with a template."""
import pytest
from agent.astra_prompt import build_content_brief


@pytest.mark.parametrize('pixels,box', [
    ('1080x1920', 'x 86 to 994 px and y 288 to 1498 px'),
    ('1440x2560', 'x 115 to 1325 px and y 384 to 1997 px'),
    ('not-a-size', 'x 86 to 994 px and y 288 to 1498 px'),
])
def test_story_uses_interior_clearance_with_creative_freedom(pixels, box):
    headline = 'You did not open a gym to run your own ads.'
    brief = build_content_brief(headline, ['Approved fact one', 'Approved fact two'],
        cta='Save this post', footer='LASSOFRAMEWORK.COM', surface='Story', pixels=pixels)
    assert box in brief
    assert 'x=0.06 to 0.94 and y=0.10 to 0.85' in brief
    assert 'no prescribed rows' in brief
    assert 'full freedom over colors, typography' in brief
    assert 'meaningful illustration' in brief
    assert 'full 9:16 canvas' in brief
    for copy in (headline, 'Approved fact one', 'Approved fact two', 'Save this post', 'LASSOFRAMEWORK.COM'):
        assert copy in brief


def test_feed_retains_its_existing_content_led_contract():
    brief = build_content_brief('Headline', ['Approved fact'], surface='feed post')
    assert 'STORY SAFE AREA' not in brief
    assert 'Keep essential text within comfortable feed margins.' in brief
    assert 'full freedom over colors, typography' in brief


def test_corrective_brief_focuses_on_edit_with_all_approved_copy():
    brief = build_content_brief('Approved headline', ['Fact one', 'Fact two'],
        cta='Save this post', footer='LASSOFRAMEWORK.COM', surface='Story',
        corrective='Move the URL up', reference_note='IGNORE ME BENCHMARK',
        art_direction='IGNORE ME INITIAL ART DIRECTION')
    for required in ('Approved headline', 'Fact one', 'Fact two', 'Save this post',
                     'LASSOFRAMEWORK.COM', 'Move the URL up'):
        assert required in brief
    assert brief.startswith('EDIT the attached rejected candidate')
    assert 'y=15 to 78 percent' in brief
    assert 'meaningful illustration' in brief
    assert 'x 86 to 994 px and y 288 to 1498 px' in brief
    assert 'IGNORE ME' not in brief
