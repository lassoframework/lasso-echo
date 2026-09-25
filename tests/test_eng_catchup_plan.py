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
