"""Offline daily cadence, persistent reservation and independent lane evidence."""
import inspect
import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agent import config, db, listener, runner
from agent.jobs import media_repeat_sweep

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
KEY = 'media_repeat_sweep_day_v1:2026-10-07'


@pytest.fixture
def lane(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setattr(config, 'media_repeat_sweep_enabled', lambda: True)
    calls = []
    monkeypatch.setattr(media_repeat_sweep, 'run',
                        lambda gyms, **kwargs: calls.append((gyms, kwargs)) or
                        [{'rows_repointed': 2, 'rows_held': 1}])
    monkeypatch.setattr(runner.ops_alerts, 'alert', lambda message: None)
    return calls


def test_restart_cadence_and_global_apply(lane):
    assert runner.run_media_repeat_sweep_day(NOW)['rows_repointed'] == 2
    assert lane == [([], {'apply': True})]
    assert json.loads(db.kv_get(KEY))['state'] == 'completed'
    assert runner.run_media_repeat_sweep_day(NOW)['reason'] == 'daily repeat sweep already reserved'
    assert len(lane) == 1
    runner.run_media_repeat_sweep_day(NOW + timedelta(days=1))
    assert len(lane) == 2


@pytest.mark.parametrize('failure', ['exception', 'row_error', 'crash'])
def test_failure_consumes_day_without_replay(lane, monkeypatch, failure):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        if failure == 'exception':
            raise RuntimeError('offline')
        if failure == 'crash':
            raise KeyboardInterrupt()
        return [{'error': 'offline'}]
    monkeypatch.setattr(media_repeat_sweep, 'run', fail)
    if failure == 'crash':
        with pytest.raises(KeyboardInterrupt):
            runner.run_media_repeat_sweep_day(NOW)
    else:
        assert runner.run_media_repeat_sweep_day(NOW)['ok'] is False
    assert json.loads(db.kv_get(KEY))['state'] == 'failed'
    runner.run_media_repeat_sweep_day(NOW)
    assert calls == [1]



def test_hold_errors_fail_receipt_and_alert_without_replay(lane, monkeypatch):
    alerts = []
    monkeypatch.setattr(media_repeat_sweep, 'run', lambda *args, **kwargs: [
        {'rows_repointed': 2, 'rows_held': 1, 'hold_errors': 1},
        {'error': 'ReadError'}, {'hold_errors': 2}])
    monkeypatch.setattr(runner.ops_alerts, 'alert', alerts.append)
    result = runner.run_media_repeat_sweep_day(NOW)
    assert result['ok'] is False
    assert result['errors'] == 4
    assert result['hold_errors'] == 3
    assert result['rows_repointed'] == 2
    receipt = json.loads(db.kv_get(KEY))
    assert receipt['state'] == 'failed'
    assert receipt['result'] == result
    assert len(alerts) == 1
    assert runner.run_media_repeat_sweep_day(NOW)['reason'] == 'daily repeat sweep already reserved'
    assert len(alerts) == 1

def _child_attempt(db_path, now, queue):
    import os
    os.environ['AGENT_DB_PATH'] = db_path
    queue.put(runner.run_media_repeat_sweep_day(now))


@pytest.mark.parametrize('offset', [0, 1])
def test_real_process_overlap_across_midnight(lane, monkeypatch, offset):
    started, release = threading.Event(), threading.Event()
    def pause(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return []
    monkeypatch.setattr(media_repeat_sweep, 'run', pause)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(runner.run_media_repeat_sweep_day, NOW)
        assert started.wait(5)
        try:
            ctx = multiprocessing.get_context('spawn')
            queue = ctx.Queue()
            child = ctx.Process(target=_child_attempt,
                                args=(db.db_path(), NOW + timedelta(days=offset), queue))
            child.start()
            result = queue.get(timeout=10)
            child.join(timeout=5)
            assert child.exitcode == 0
            assert result['reason'] == 'repeat sweep already running'
            if offset:
                assert not db.kv_get('media_repeat_sweep_day_v1:2026-10-08')
        finally:
            release.set()
        assert first.result()['ok'] is True


@pytest.mark.parametrize('gate', ['disabled', 'not_durable', 'no_lock'])
def test_fail_closed_before_job(lane, monkeypatch, gate):
    if gate == 'disabled':
        monkeypatch.setattr(config, 'media_repeat_sweep_enabled', lambda: False)
    elif gate == 'not_durable':
        monkeypatch.setattr(db, 'kv_is_durable', lambda: False)
    else:
        monkeypatch.setenv('AGENT_DB_PATH', '/nonexistent-repeat-sweep/echo.db')
        monkeypatch.setattr(db, 'kv_is_durable', lambda: True)
    assert 'reason' in runner.run_media_repeat_sweep_day(NOW)
    assert not lane


def test_reserved_crashed_receipt_blocks_restart(lane):
    db.kv_set(KEY, '{"state":"reserved"}')
    runner.run_media_repeat_sweep_day(NOW)
    assert not lane


def test_lane_is_independent_and_daemon(lane, monkeypatch):
    threads = []
    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(listener.threading, 'Thread', Thread)
    assert listener._start_media_repeat_sweep_scheduler() is True
    assert threads == [dict(target=listener._media_repeat_sweep_scheduler,
                            name='media-repeat-sweep', daemon=True)]
    source = inspect.getsource(listener)
    assert source.index('    _start_media_repeat_sweep_scheduler()') < source.index(
        '    if str(os.environ.get("AGENT_SCHEDULER_ENABLED"')
    assert 'run_daily(' not in inspect.getsource(listener._media_repeat_sweep_scheduler)
    assert 'run_media_repeat_sweep_day()' in inspect.getsource(runner.run_daily)
    monkeypatch.setattr(config, 'media_repeat_sweep_enabled', lambda: False)
    assert listener._start_media_repeat_sweep_scheduler() is False


def test_scheduler_survives_failure_and_polls_hourly(monkeypatch):
    calls, sleeps = [], []
    def attempt():
        calls.append(1)
        raise RuntimeError('offline')
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt()
    monkeypatch.setattr(runner, 'run_media_repeat_sweep_day', attempt)
    monkeypatch.setattr(listener.time, 'sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        listener._media_repeat_sweep_scheduler()
    assert len(calls) == 2
    assert sleeps == [3600, 3600]
