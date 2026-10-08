import pytest

from agent.astra_prompt import build_verified_gym_content_brief


def _palette(**updates):
    value = {
        "verified": True,
        "gym_id": "gym-123",
        "evidence_ref": "owner-palette-receipt:abc",
        "colors": ["#123456", "#F4A261"],
    }
    value.update(updates)
    return value


def _copy(**updates):
    value = {
        "headline": "Build a stronger habit",
        "facts": ["Two coached sessions each week", "Progress tracked monthly"],
        "cta": "Book a visit",
        "footer": "examplegym.com",
    }
    value.update(updates)
    return value


def test_gym_brief_reuses_complete_content_layout_with_gym_identity_and_palette():
    brief = build_verified_gym_content_brief("gym-123", _copy(), _palette())

    assert "finished gym infographic" in brief
    assert "1024x1280" in brief and "4:5" in brief
    assert "readable supporting copy at 360 pixels wide" in brief
    assert "Do not omit a fact to simplify the layout" in brief
    assert "Two coached sessions each week" in brief
    assert "Progress tracked monthly" in brief
    assert "Book a visit" in brief and "examplegym.com" in brief
    assert "supported source DATA" in brief
    assert "approved source" not in brief.lower()
    assert "BRAND COLORS, VERIFIED FOR THIS GYM" in brief
    assert "#123456, #F4A261" in brief
    assert "Use only the verified gym palette" in brief
    assert "LASSO" not in brief
    assert "deep navy" not in brief and "rich red" not in brief


@pytest.mark.parametrize("palette", [
    None,
    {},
    _palette(verified=False),
    _palette(gym_id="another-gym"),
    _palette(evidence_ref=""),
    _palette(colors=[]),
    _palette(colors=["#123456", "not-a-color"]),
])
def test_gym_brief_holds_without_a_verified_gym_palette(palette):
    with pytest.raises(ValueError, match="Verified gym palette required"):
        build_verified_gym_content_brief("gym-123", _copy(), palette)


@pytest.mark.parametrize("copy", [
    None,
    {},
    _copy(headline=""),
    _copy(facts=[]),
    _copy(facts=[" "]),
])
def test_gym_brief_holds_without_complete_supported_source_facts(copy):
    with pytest.raises(ValueError, match="Supported gym facts required"):
        build_verified_gym_content_brief("gym-123", copy, _palette())
