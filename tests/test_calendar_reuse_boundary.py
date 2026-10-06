"""Exercise the actual calendar send boundary with its ISO-string clock."""
from types import SimpleNamespace
import pytest
from agent import calendar_autopublish as cap, media_source_store, publish_billing_gate
from agent import media_reuse_policy
from tests.test_calendar_autopublish import RUN_DATE, LATE_NOW, _row, _FakePublisher
from tests.test_calendar_media_review_boundary import ReviewCalendarStore

GYM = 'zanshinfitness630e22'

@pytest.mark.parametrize('platform', ['instagram', 'facebook'])
@pytest.mark.parametrize('history_kind', ['reused', 'unavailable', 'fresh'])
def test_calendar_checks_history_before_external_send(monkeypatch, platform, history_kind):
    monkeypatch.setenv('AGENT_CALENDAR_AUTOPUBLISH', 'true')
    monkeypatch.setenv('AGENT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(publish_billing_gate, 'publishing_blocked', lambda _: False)
    monkeypatch.setattr(cap, '_account_for', lambda *a: SimpleNamespace(
        key=GYM + '_ig', platform=platform, display_name='Zanshin'))
    monkeypatch.setattr(cap, '_alert_publish_blocked', lambda *a, **kw: None)
    monkeypatch.setattr(media_source_store, 'default_store', lambda: SimpleNamespace(list_assets=lambda _: []))
    row = _row('new', account=platform, status='approved')
    row['gym_id'] = GYM
    calendar = ReviewCalendarStore([row])
    seen = []
    def history(gym, cutoff):
        seen.append((gym, cutoff))
        if history_kind == 'unavailable':
            raise RuntimeError('database unavailable')
        return [dict(id='old', gym_id=GYM, image_url=row['image_url'])] if history_kind == 'reused' else []
    calendar.list_media_publish_history = history
    publisher = _FakePublisher()
    def send(draft, account, **kwargs):
        return publisher(draft, account)
    result = cap.publish_due(RUN_DATE, gym_id=GYM, store=calendar,
        now=LATE_NOW, catch_all=True, approved_only=True, zernio_publish=send)
    assert seen == [(GYM, '2025-11-11T03:59:00+00:00')]
    if history_kind == 'fresh':
        assert result['published'] == ['new']
        assert len(publisher.calls) == 1
    else:
        assert publisher.calls == []
        assert result['failed'] == ['new']
        assert calendar.rows['new']['status'] == 'approved'
        assert calendar.rollbacks[0][2] == ('media_reuse_nine_month_hold' if history_kind == 'reused' else 'media_reuse_history_unavailable')


def test_calendar_resolves_account_library_path_before_reuse_check(monkeypatch):
    monkeypatch.setenv('AGENT_CALENDAR_AUTOPUBLISH', 'true')
    monkeypatch.setenv('AGENT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(publish_billing_gate, 'publishing_blocked', lambda _: False)
    account = SimpleNamespace(
        key=GYM + '_ig', platform='instagram', display_name='Zanshin',
        library_path=lambda: '/data/libraries/zanshin')
    monkeypatch.setattr(cap, '_account_for', lambda *a: account)
    monkeypatch.setattr(cap, '_alert_publish_blocked', lambda *a, **kw: None)
    seen = []

    def reuse_check(row, gym_id, store, **kwargs):
        seen.append(kwargs['library_path'])
        return None

    monkeypatch.setattr(media_reuse_policy, 'publish_hold_reason', reuse_check)
    row = _row('new', account='instagram', status='approved')
    row['gym_id'] = GYM
    calendar = ReviewCalendarStore([row])
    publisher = _FakePublisher()

    result = cap.publish_due(
        RUN_DATE, gym_id=GYM, store=calendar, now=LATE_NOW,
        catch_all=True, approved_only=True,
        zernio_publish=lambda draft, resolved, **kwargs: publisher(draft, resolved))

    assert seen == ['/data/libraries/zanshin']
    assert result['published'] == ['new']
    assert len(publisher.calls) == 1
