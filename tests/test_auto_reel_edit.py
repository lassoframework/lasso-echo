import pytest
from agent import auto_reel_edit
from agent.story_composer import ComposePlan, Segment


def test_mixed_framing_keeps_verified_bounds_and_portrait_ending(monkeypatch):
    monkeypatch.setattr(auto_reel_edit, '_source_layout', lambda path, probe: (1.78 if path == 'wide' else .5625, '', ''))
    monkeypatch.setattr(auto_reel_edit.shutil, 'which', lambda name: name)
    source = [Segment('a', 'gym', 0, 3, source_path='tall'),
              Segment('b', 'gym', 17, 24, source_path='tall'),
              Segment('c', 'gym', 20, 27, source_path='wide')]
    plan = ComposePlan('gym', segments=source, total_sec=17)
    auto_reel_edit.arrange_shots(plan)
    assert plan.total_sec == 15
    assert plan.segments[0].source_path == plan.segments[-1].source_path == 'tall'
    assert max(s.duration for s in plan.segments if s.source_path == 'wide') == 2.5
    assert sum(s.duration for s in plan.segments if s.source_path == 'wide') == 5
    for original in source:
        selected = sorted((s.start_ts, s.end_ts) for s in plan.segments if s.asset_id == original.asset_id)
        assert all(original.start_ts <= start < end <= original.end_ts for start, end in selected)
        assert all(selected[i][1] <= selected[i+1][0] for i in range(len(selected)-1))


def test_short_verified_coverage_holds_instead_of_extending(monkeypatch):
    monkeypatch.setattr(auto_reel_edit, '_source_layout', lambda *a: (.5625, '', ''))
    monkeypatch.setattr(auto_reel_edit.shutil, 'which', lambda name: name)
    plan = ComposePlan('gym', segments=[Segment('a','gym',2,5,source_path='tall')], total_sec=3)
    with pytest.raises(ValueError, match='fifteen seconds'):
        auto_reel_edit.arrange_shots(plan)
