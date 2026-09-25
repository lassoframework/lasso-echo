import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('catchup', Path(__file__).parents[1] / 'tools/eng_catchup_plan.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    rows = [dict(id=str(i), gym_id='eng', platform='instagram', format='feed',
                 post_date='2026-09-19', status='approved', late_post_id=None,
                 reject_reason='media_asset_review_required') for i in range(2)]
    slots = [dict(platform='instagram', format='feed', scheduled_at=f'2026-09-{d}T07:30:00-04:00')
             for d in (26, 27, 28)]
    rows.append(dict(id='future', gym_id='eng', platform='instagram', format='feed',
                     post_date='2026-09-26', status='pending', scheduled_at=slots[0]['scheduled_at']))
    return dict(timezone='America/New_York', posts_per_day=1,
                as_of='2026-09-25T16:00:00-04:00', rows=rows, slots=slots)


def test_reservations_preserved_oldest_first_no_dump():
    out = m.plan(fixture(), expected=2)
    assert [(r['id'], r['post_date']) for r in out['rows']] == [('0', '2026-09-27'), ('1', '2026-09-28')]
    assert all(r['required_status'] == 'pending' for r in out['rows'])


@pytest.mark.parametrize('fault', ['null_schedule', 'overflow', 'missing_reservation', 'wrong_count'])
def test_unknown_or_overbooked_schedule_refused(fault):
    data = fixture()
    if fault == 'null_schedule':
        data['rows'][-1]['scheduled_at'] = None
    elif fault == 'overflow':
        data['slots'].append(dict(platform='instagram', format='feed', scheduled_at='2026-09-26T18:30:00-04:00'))
    elif fault == 'missing_reservation':
        data['slots'].pop(0)
    with pytest.raises(ValueError):
        m.plan(data, expected=37 if fault == 'wrong_count' else 2)


def export_rows():
    return m.text_rows((Path(__file__).parents[1] / 'docs/eng-rows-20260925.txt').read_text())


def test_actual_eng_export_moves_all_missed_before_future_without_flood():
    from collections import Counter
    rows = export_rows()
    out = m.forward_plan(rows, '2026-09-26T00:00:00-04:00')
    assert out['count'] == 68
    assert len(out['untouched_gbp_ids']) == 2
    assert len(out['review_asset_ids']) == 23
    assert sum(r['reject_reason'] == 'media_asset_review_required' for r in out['rows']) == 37
    assert max(Counter((r['platform'], r['post_date']) for r in out['rows']).values()) <= 2
    assert len({(r['platform'], r['scheduled_at']) for r in out['rows']}) == 68
    missed = [r for r in out['rows'] if r['old_post_date'] <= '2026-09-25']
    future = [r for r in out['rows'] if r['old_post_date'] > '2026-09-25']
    assert len(missed) == 42 and len(future) == 26
    assert max(r['scheduled_at'] for r in missed) < min(r['scheduled_at'] for r in future)
    for asset in out['review_asset_ids']:
        assert len({r['post_date'] for r in out['rows'] if r['source_media_asset_id'] == asset}) == 1
    assert out['rows'][0]['scheduled_at'] == '2026-09-26T11:30:00+00:00'
    assert out['rows'][-1]['scheduled_at'] == '2026-10-18T22:30:00+00:00'


@pytest.mark.parametrize('cutoff', ['2026-09-26T07:30:00-04:00', '2026-09-29T19:00:00-04:00'])
def test_later_review_shifts_whole_plan_strictly_after_completion(cutoff):
    out = m.forward_plan(export_rows(), cutoff)
    assert all(m.stamp(r['scheduled_at']) > m.stamp(cutoff) for r in out['rows'])
    assert out['rows'][0]['post_date'] > cutoff[:10]
