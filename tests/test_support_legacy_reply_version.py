"""Disabled production retains atomic pending intake, never a relabeled reply."""
import pytest
from agent.slack_convo import adapter, replay
from tests.test_slack_convo import _deps, _ev
from tests.test_support_slack_replay import pg_engine, pg_bus, paused, queue, resume


def test_flag_off_capture_dedupes_and_replays_after_arming(pg_bus, paused, monkeypatch):
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED', 'false')
    deps = _deps(pg_bus, client_armed=True)
    event = _ev('is the calendar loaded?')
    result = adapter.handle_event(event, 'G0MPIM:1.001', deps)
    assert result.reason == 'support_atomic_replay_required'
    assert queue(pg_bus)[0]['state'] == 'pending'
    assert all(row['direction'] == 'inbound' for row in pg_bus.msgs)
    duplicate = adapter.handle_event(event, 'G0MPIM:1.001', deps)
    assert duplicate.duplicate
    assert len(pg_bus.msgs) == len(queue(pg_bus)) == 1
    assert replay.run_once(deps) == {}
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED', 'true')
    resume(paused)
    assert replay.run_once(deps) == {'committed': 1}
    assert any(row['direction'] == 'outbound' for row in pg_bus.msgs)


def test_flag_off_concurrent_request_never_borrows_new_version(pg_bus, paused, monkeypatch):
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED', 'false')
    calls = []
    deps = _deps(pg_bus, client_armed=True, auto_answer=True,
                 answer=lambda *args: calls.append(args) or {
                     'body': 'Your calendar is loaded.', 'grounding': {'loaded': True}})
    adapter.handle_event(_ev('is the calendar loaded?'), 'G0MPIM:1.001', deps)
    adapter.handle_event(_ev('why is my facebook disconnected?', ts='1.002'), 'G0MPIM:1.002', deps)
    rows = {row['event_key']: row for row in queue(pg_bus)}
    assert rows['G0MPIM:1.001']['ticket_snapshot']['request_version'] == 1
    assert rows['G0MPIM:1.002']['ticket_snapshot']['request_version'] == 2
    assert rows['G0MPIM:1.001']['state'] == 'pending'
    assert not calls
    assert all(row['direction'] == 'inbound' for row in pg_bus.msgs)
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED', 'true')
    resume(paused)
    replay.process(deps, rows['G0MPIM:1.001']['id'])
    assert not calls
    assert all(row['direction'] == 'inbound' for row in pg_bus.msgs)


def test_flag_off_missing_atomic_capture_fails_without_legacy_insert(pg_bus, paused, monkeypatch):
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED', 'false')
    pg_bus.transport.before_call = lambda *args: (_ for _ in ()).throw(ConnectionError('DB unavailable'))
    with pytest.raises(ConnectionError):
        adapter.handle_event(_ev('my photos are broken'), 'G0MPIM:1.001', _deps(pg_bus))
    assert not pg_bus.msgs
