"""Offline independent moderation lane, persistent budget and overlap evidence."""
import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agent import account_key_resolve, config, db, gym_media_moderation, listener, runner

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
KEY = 'gym_media_moderation_day_v1:2026-10-07'


class Store:
    def __init__(self, gyms=13, assets=100):
        self.sources = [dict(id=f's{i}', gym_id=f'g{i:02}', active=True,
                             kind='gym_drive') for i in range(gyms)]
        self.assets = [dict(id=f'a{j:04}', gym_id=s['gym_id'], source_id=s['id'],
                            kind='photo', review_status='pending_review',
                            moderation_status='pending', content_hash='hash')
                       for s in self.sources for j in range(assets)]

    def available(self):
        return True

    def list_sources(self):
        return self.sources

    def list_assets(self, gym, source_id):
        return [a for a in self.assets if a['gym_id'] == gym and a['source_id'] == source_id]


class Drive:
    def available(self):
        return True


@pytest.fixture
def lane(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setattr(config, 'gym_drive_connect_enabled', lambda: True)
    monkeypatch.setattr(config, 'gym_drive_connect_active_for', lambda gym: True)
    monkeypatch.setattr(account_key_resolve, 'resolve_known_source_keys',
                        lambda keys: {key: key for key in keys})
    calls = []

    def moderate(gym, asset_id, **kwargs):
        calls.append((gym, asset_id))
        return {'ok': True}

    monkeypatch.setattr(gym_media_moderation, 'moderate_asset', moderate)
    kwargs = dict(store=Store(), drive=Drive(), vision=lambda *args: '')
    return calls, kwargs


def test_success_fair_cap_and_persistent_restart(lane):
    calls, kwargs = lane
    result = listener._run_media_moderation_day(NOW, **kwargs)
    assert result['attempted'] == result['recorded'] == result['reserved'] == 50
    assert len({gym for gym, _ in calls}) == 13
    counts = [sum(g == source['gym_id'] for g, _ in calls) for source in kwargs['store'].sources]
    assert max(counts) - min(counts) <= 1
    assert len(set(calls)) == 50
    receipt = json.loads(db.kv_get(KEY))
    assert receipt['state'] == 'completed'
    assert receipt['reserved'] == 50
    assert listener._run_media_moderation_day(NOW, **kwargs)['reason'] == 'daily batch already reserved'
    assert len(calls) == 50
    listener._run_media_moderation_day(NOW + timedelta(days=1), **kwargs)
    assert len(calls) == 100


def test_failures_consume_budget_without_approval_or_replay(lane, monkeypatch):
    calls, kwargs = lane

    def fail(gym, asset_id, **unused):
        calls.append((gym, asset_id))
        raise RuntimeError('provider failed')

    monkeypatch.setattr(gym_media_moderation, 'moderate_asset', fail)
    result = listener._run_media_moderation_day(NOW, **kwargs)
    assert result['ok'] is False and result['recorded'] == 0
    assert result['attempted'] == 50
    listener._run_media_moderation_day(NOW, **kwargs)
    assert len(calls) == 50
    assert all(a['review_status'] == 'pending_review' for a in kwargs['store'].assets)


def test_interruption_is_not_replayed(lane, monkeypatch):
    _, kwargs = lane
    monkeypatch.setattr(gym_media_moderation, 'moderate_asset',
                        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        listener._run_media_moderation_day(NOW, **kwargs)
    receipt = json.loads(db.kv_get(KEY))
    assert receipt['state'] == 'interrupted' and receipt['reserved'] == 50
    assert listener._run_media_moderation_day(NOW, **kwargs)['reason'] == 'daily batch already reserved'


@pytest.mark.parametrize('day_offset', [0, 1])
def test_deploy_overlap_full_pass_lock_across_midnight(lane, monkeypatch, day_offset):
    calls, kwargs = lane
    provider_started, release_provider = threading.Event(), threading.Event()

    def pause(gym, asset_id, **unused):
        calls.append((gym, asset_id))
        if len(calls) == 1:
            provider_started.set()
            assert release_provider.wait(timeout=5)
        return {'ok': True}

    monkeypatch.setattr(gym_media_moderation, 'moderate_asset', pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(listener._run_media_moderation_day, NOW, **kwargs)
        try:
            assert provider_started.wait(timeout=5)
            second_day = NOW + timedelta(days=day_offset)
            second = listener._run_media_moderation_day(second_day, **kwargs)
            assert second['reason'] == 'moderation pass already running'
            assert len(calls) == 1
            if day_offset:
                assert not db.kv_get('gym_media_moderation_day_v1:' + second_day.date().isoformat())
        finally:
            release_provider.set()
        assert first.result(timeout=5)['reserved'] == 50
    assert len(calls) == 50
    assert listener._run_media_moderation_day(NOW, **kwargs)['reason'] == 'daily batch already reserved'


def test_interrupted_previous_day_assets_get_only_one_day_cooldown(lane):
    calls, kwargs = lane
    kwargs['store'] = Store(gyms=1, assets=2)
    previous = dict(state='reserved', assets=[['g00', 'a0000']])
    db.kv_set('gym_media_moderation_day_v1:2026-10-06', json.dumps(previous))
    result = listener._run_media_moderation_day(NOW, **kwargs)
    assert result['attempted'] == 1 and calls == [('g00', 'a0001')]
    listener._run_media_moderation_day(NOW + timedelta(days=1), **kwargs)
    assert ('g00', 'a0000') in calls


def test_process_lock_unavailable_fails_before_spend(lane, monkeypatch):
    calls, kwargs = lane
    monkeypatch.setattr(db, 'db_path', lambda: '/nonexistent/volume/echo.db')
    assert listener._run_media_moderation_day(NOW, **kwargs)['reason'] == 'moderation process lock unavailable'
    assert not calls


@pytest.mark.parametrize('disabled', ['drive_flag', 'durability', 'store', 'drive', 'vision'])
def test_disabled_credentials_do_not_reserve_or_scan(lane, monkeypatch, disabled):
    calls, kwargs = lane
    if disabled == 'drive_flag':
        monkeypatch.setattr(config, 'gym_drive_connect_enabled', lambda: False)
        monkeypatch.setattr(config, 'gym_drive_connect_gyms', lambda: [])
    elif disabled == 'durability':
        monkeypatch.setattr(db, 'kv_is_durable', lambda: False)
    elif disabled in ('store', 'drive'):
        kwargs[disabled].available = lambda: False
    else:
        kwargs['vision'] = None
        monkeypatch.setattr(gym_media_moderation, 'default_vision', lambda: None)
    assert listener._run_media_moderation_day(NOW, **kwargs)['ok'] is False
    assert not calls and not db.kv_get(KEY)


def test_source_and_asset_gates_retained(lane, monkeypatch):
    calls, kwargs = lane
    store = Store(gyms=5, assets=1)
    store.sources[0]['active'] = False
    store.sources[1]['revoked_externally'] = True
    store.sources[2]['kind'] = 'untrusted'
    store.assets[3]['review_status'] = 'approved'
    kwargs['store'] = store
    listener._run_media_moderation_day(NOW, **kwargs)
    assert calls == [('g04', 'a0000')]


def test_database_failure_fails_before_spend(lane, monkeypatch):
    calls, kwargs = lane
    monkeypatch.setattr(db, 'connect', lambda: (_ for _ in ()).throw(RuntimeError('DB unavailable')))
    with pytest.raises(RuntimeError):
        listener._run_media_moderation_day(NOW, **kwargs)
    assert not calls


def test_lane_independent_of_daily_draw_and_old_invocation_removed():
    source = inspect.getsource(listener)
    assert 'target=_media_moderation_scheduler' in source
    assert 'target=_daily_scheduler' in source
    assert '_moderation_run' not in inspect.getsource(runner.run_daily)
    assert 'run_daily(' not in inspect.getsource(listener._media_moderation_scheduler)


@pytest.mark.parametrize('result', [
    {"ok": True, "reason": "no pending eligible assets"},
    {"ok": False, "reason": "vision provider unarmed"},
])
def test_independent_lane_polls_hourly_even_empty_or_disabled(monkeypatch, result):
    calls, sleeps = [], []
    monkeypatch.setattr(listener, '_run_media_moderation_day',
                        lambda: (calls.append(True), result)[1])

    def stop(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(listener.time, 'sleep', stop)
    with pytest.raises(KeyboardInterrupt):
        listener._media_moderation_scheduler()
    assert calls == [True] and sleeps == [3600]


def test_budget_durability_and_connection_use_same_configured_volume(monkeypatch, tmp_path):
    monkeypatch.delenv('AGENT_DB_PATH', raising=False)
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    assert db.kv_is_durable() is True
    assert db.db_path() == str(tmp_path / 'echo.db')
    conn = db.connect()
    try:
        assert conn.execute('PRAGMA database_list').fetchone()['file'] == db.db_path()
    finally:
        conn.close()
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path / 'absent'))
    assert db.kv_is_durable() is False


@pytest.mark.parametrize('daily_enabled', ['true', 'false'])
def test_startup_is_independent_of_daily_scheduler_flag(monkeypatch, daily_enabled):
    monkeypatch.setenv('AGENT_SCHEDULER_ENABLED', daily_enabled)
    monkeypatch.setattr(config, 'gym_drive_connect_enabled', lambda: True)
    started = []

    class Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs)

    monkeypatch.setattr(listener.threading, 'Thread', Thread)
    assert listener._start_media_moderation_scheduler() is True
    assert started == [dict(target=listener._media_moderation_scheduler,
                            name='gym-media-moderation', daemon=True)]
    source = inspect.getsource(listener)
    assert source.index('    _start_media_moderation_scheduler()') < source.index(
        '    if str(os.environ.get("AGENT_SCHEDULER_ENABLED"')


def test_disabled_drive_does_not_start_thread(monkeypatch):
    monkeypatch.setattr(config, 'gym_drive_connect_enabled', lambda: False)
    monkeypatch.setattr(config, 'gym_drive_connect_gyms', lambda: [])
    monkeypatch.setattr(listener.threading, 'Thread',
                        lambda **kwargs: pytest.fail('must not start thread'))
    assert listener._start_media_moderation_scheduler() is False
