"""
Story Studio Wave 5: the five templates. Each carries a segment plan + overlay slots
+ music mood + ask style; no template defaults to chill; vision picks the default and
the lane/brief overrides it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_templates as t  # noqa: E402
from agent import story_music as sm  # noqa: E402


def test_all_five_templates_exist():
    assert set(t.TEMPLATES) == {"athlete_stat", "member_win", "event",
                                "class_promo", "hype_montage"}


def test_no_template_defaults_to_chill():
    for name, tmpl in t.TEMPLATES.items():
        assert tmpl.music_mood != sm.SHELF_CHILL, name
        assert tmpl.music_mood == sm.SHELF_HYPE, name


def test_every_template_has_plan_slots_ask():
    for tmpl in t.TEMPLATES.values():
        assert tmpl.segment_plan.min_segments >= 2
        assert tmpl.segment_plan.max_segments <= 6
        assert tmpl.overlay_slots
        assert tmpl.overlay_slots[-1] == "ask"       # ends on exactly one ask
        assert tmpl.ask_style


def test_chill_default_coerced_at_construction():
    tmpl = t.Template(name="x", segment_plan=t.SegmentPlan(), overlay_slots=["hook", "ask"],
                      music_mood=sm.SHELF_CHILL, ask_style="ASK")
    assert tmpl.music_mood == sm.SHELF_HYPE


def test_vision_picks_default_template():
    assert t.choose_from_vision({"tags": ["event", "party"]}) == "event"
    assert t.choose_from_vision({"mood": "proud milestone"}) == "member_win"
    assert t.choose_from_vision(None) == t.DEFAULT_TEMPLATE


def test_declared_template_overrides_vision():
    name, src = t.resolve_template(declared_template="class_promo",
                                   analysis={"tags": ["event"]})
    assert name == "class_promo"
    assert src == "declared"


def test_vision_used_when_no_declared():
    name, src = t.resolve_template(declared_template=None,
                                   analysis={"tags": ["event", "signage"]})
    assert name == "event"
    assert src == "vision"


def test_invalid_declared_falls_back_to_vision():
    name, src = t.resolve_template(declared_template="not_a_template",
                                   analysis={"mood": "celebrate"})
    assert name == "member_win"
    assert src == "vision"
