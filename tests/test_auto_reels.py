from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest
from agent import auto_reels as ar


class Calendar:
    def __init__(self):
        self.rows = {}
    def get_row(self, gym, row_id):
        return self.rows.get((gym, row_id))


@pytest.fixture
def lane(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setattr(ar.config, 'auto_reels_enabled', lambda: True, raising=False)
    monkeypatch.setattr(ar.config, 'auto_reels_gyms', lambda: ['gym'], raising=False)
    monkeypatch.setattr(ar.config, 'auto_reels_debounce_seconds', lambda: 300, raising=False)
    monkeypatch.setattr(ar.config, 'story_studio_render_active_for', lambda gym: True)
    assets = {str(i): {'id': str(i), 'gym_id': 'gym', 'content_hash': f'hash{i}', 'used_count': 0} for i in range(3)}
    candidates = [{'asset_id': str(i), 'gym_id': 'gym', 'score': 100-i, 'start_ts': 0, 'end_ts': 7} for i in range(3)]
    calls = []
    calendar = Calendar()
    def create(request, **kwargs):
        assert len(request['asset_ids']) >= 3
        assert sum(min(7, c['end_ts'] - c['start_ts']) for c in kwargs['candidates']) >= 15
        calls.append(request)
        calendar.rows['gym', request['id']] = {'id': request['id']}
        return {'status': 'staged', 'used_clips': request['asset_ids']}
    options = dict(discover=lambda *a, **k: (candidates, assets), create=create,
                   identity=lambda g: ['Gym'], calendar=calendar)
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    return options, now, calls, candidates, assets


def run(lane, seconds=0):
    options, now, *_ = lane
    return ar.run_gym('gym', now=now + timedelta(seconds=seconds), **options)


def test_off_allowlist_and_ephemeral_storage_fail_closed(lane, monkeypatch):
    monkeypatch.setattr(ar.config, 'auto_reels_enabled', lambda: False)
    assert run(lane)['status'] == 'off'
    monkeypatch.setattr(ar.config, 'auto_reels_enabled', lambda: True)
    monkeypatch.setattr(ar.config, 'auto_reels_gyms', lambda: ['other'])
    assert run(lane)['status'] == 'off'
    monkeypatch.setattr(ar.config, 'auto_reels_gyms', lambda: ['gym'])
    monkeypatch.setattr(ar.db, 'kv_is_durable', lambda: False)
    assert run(lane)['status'] == 'held'
    assert not lane[2]


def test_settles_then_stages_once_across_restarts(lane):
    assert run(lane)['status'] == 'waiting'
    assert run(lane, 299)['status'] == 'waiting'
    assert run(lane, 301)['status'] == 'staged'
    assert run(lane, 900)['status'] == 'waiting'
    assert len(lane[2]) == 1
    request = lane[2][0]
    assert request['auto_reel'] is True
    assert request['requested_by'] == 'echo_auto_reels'
    assert request['asset_ids'] == ['0', '1', '2']
    assert 'publish' not in request


def test_changed_upload_restarts_debounce_and_new_batch_never_reuses_old(lane):
    run(lane)
    lane[4]['0']['content_hash'] = 'newbytes'
    assert run(lane, 250)['status'] == 'waiting'
    assert run(lane, 400)['status'] == 'waiting'
    assert run(lane, 551)['status'] == 'staged'
    for aid in ['new', 'new2', 'new3']:
        add_source(lane, aid)
    run(lane, 700)
    assert run(lane, 1001)['status'] == 'staged'
    assert lane[2][-1]['asset_ids'] == ['new', 'new2', 'new3']


def test_missing_identity_holds_without_consuming_sources(lane):
    lane[0]['identity'] = lambda g: []
    assert run(lane)['status'] == 'held'
    lane[0]['identity'] = lambda g: ['Gym']
    run(lane, 100)
    assert run(lane, 401)['status'] == 'staged'


def test_cross_tenant_candidates_never_enter_job(lane):
    lane[4]['1']['gym_id'] = 'foreign'
    run(lane)
    run(lane, 301)
    assert not lane[2]  # two valid sources cannot satisfy the real renderer


def test_transient_failure_retries_same_id_and_respects_backoff(lane):
    original = lane[0]['create']
    attempts = []
    def flaky(request, **kwargs):
        attempts.append(request['id'])
        if len(attempts) == 1:
            raise TimeoutError('secret must not be logged')
        return original(request, **kwargs)
    lane[0]['create'] = flaky
    run(lane)
    assert run(lane, 301)['reason'] == 'TimeoutError'
    assert run(lane, 400)['status'] == 'waiting'
    assert run(lane, 602)['status'] == 'staged'
    assert attempts[0] == attempts[1]


def test_lost_success_reply_reconciles_without_duplicate_even_if_sources_used(lane):
    original = lane[0]['create']
    def lost(request, **kwargs):
        original(request, **kwargs)
        for asset in lane[4].values():
            asset['used_count'] = 1
        raise TimeoutError()
    lane[0]['create'] = lost
    run(lane); run(lane, 301)
    assert run(lane, 602)['reconciled'] is True
    assert len(lane[2]) == 1


def test_retries_bound_expensive_work(lane):
    calls = []
    def fail(request, **kwargs):
        calls.append(request['id'])
        return {'status': 'held', 'reason': 'No grounded content'}
    lane[0]['create'] = fail
    run(lane)
    for second in [301, 602, 1503, 5000, 10000]:
        run(lane, second)
    assert len(calls) == 3
    assert len(set(calls)) == 1


def test_process_lock_excludes_concurrent_worker(lane):
    started, release = threading.Event(), threading.Event()
    original = lane[0]['create']
    def slow(request, **kwargs):
        started.set(); release.wait(5)
        return original(request, **kwargs)
    lane[0]['create'] = slow
    run(lane)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run, lane, 301)
        assert started.wait(5)
        assert run(lane, 301)['status'] == 'busy'
        release.set()
        assert first.result()['status'] == 'staged'
    assert len(lane[2]) == 1


def test_only_best_ten_sources_in_one_batch(lane):
    for i in range(3, 16):
        lane[3].append({'asset_id': str(i), 'gym_id': 'gym', 'start_ts': 0, 'end_ts': 7})
        lane[4][str(i)] = {'id': str(i), 'gym_id': 'gym', 'content_hash': str(i)}
    run(lane); run(lane, 301)
    assert len(lane[2][0]['asset_ids']) == 10


def test_listener_does_not_start_a_second_render_thread(lane, monkeypatch):
    from agent import listener
    class Active:
        def is_alive(self):
            return True
    existing = Active()
    monkeypatch.setattr(listener.threading, 'Thread', lambda **kwargs: pytest.fail('overlapping worker'))
    assert listener._auto_reels_tick(existing, lane[1]) is existing
    monkeypatch.setattr(ar.config, 'auto_reels_enabled', lambda: False)
    assert listener._auto_reels_tick(None, lane[1]) is None


def test_retry_never_uses_replaced_or_hidden_source(lane):
    calls = []
    lane[0]['create'] = lambda request, **kwargs: (calls.append(request) or {'status': 'held', 'reason': 'Transient'})
    run(lane); run(lane, 301)
    lane[4]['0']['content_hash'] = 'replacement'
    result = run(lane, 602)
    assert result['status'] == 'held'
    assert 'no longer eligible' in result['reason']
    assert len(calls) == 1


def test_new_enrichment_reopens_exhausted_hold_without_daily_duplicate(lane):
    calls = []
    lane[0]['create'] = lambda request, **kwargs: (calls.append(request) or {'status': 'held', 'reason': 'Missing content'})
    run(lane)
    for sec in [301, 602, 1503, 5000]:
        run(lane, sec)
    assert len(calls) == 3
    for asset in lane[4].values():
        asset['vision_json'] = {'confidence': 0.9, 'subjects': ['Coach demonstrating squat']}
    run(lane, 6000); run(lane, 6301)
    assert len(calls) == 4
    assert calls[-1]['id'] != calls[0]['id']
    assert calls[-1]['asset_ids'] == ['0', '1', '2']


def test_missing_footage_holds(lane):
    lane[3].clear(); lane[4].clear()
    assert run(lane)['reason'] == 'No eligible raw footage'
    assert not lane[2]


def add_source(lane, aid, seconds=7):
    lane[3].append({'asset_id': aid, 'gym_id': 'gym', 'start_ts': 0, 'end_ts': seconds})
    lane[4][aid] = {'id': aid, 'gym_id': 'gym', 'content_hash': 'hash_' + aid, 'used_count': 0}


def jobs():
    import json
    return json.loads(ar.db.kv_get('auto_reels:v1:gym', '{}')).get('jobs', {})


def test_one_settled_clip_plus_two_later_form_one_reel(lane):
    lane[3][:] = lane[3][:1]
    lane[4].pop('1'); lane[4].pop('2')
    run(lane); run(lane, 301); run(lane, 1200)
    assert not jobs() and not lane[2]
    add_source(lane, 'later1'); add_source(lane, 'later2')
    run(lane, 1300)
    assert run(lane, 1601)['status'] == 'staged'
    assert lane[2][0]['asset_ids'] == ['0', 'later1', 'later2']
    assert len(jobs()) == 1


def test_three_short_clips_wait_unreserved_until_total_fifteen_seconds(lane):
    for c in lane[3]:
        c['end_ts'] = 3
    run(lane); run(lane, 301)
    assert not jobs()
    add_source(lane, 'longer', seconds=7)
    run(lane, 400)
    assert run(lane, 701)['status'] == 'staged'


def test_insufficient_usable_hold_combines_old_clear_clips_with_new_arrival(lane):
    original = lane[0]['create']
    attempts = []
    def create(request, **kwargs):
        attempts.append(request['id'])
        if 'new' not in request['asset_ids']:
            return {'status': 'held', 'reason': 'Not enough clear, usable moments for a finished reel',
                    'moment_diagnostics': [{'asset_id': '0', 'held': False, 'score': 90},
                                           {'asset_id': '1', 'held': False, 'score': 88},
                                           {'asset_id': '2', 'held': True, 'reason': 'Dark footage',
                                            'source_path': '/private/must-not-persist'}]}
        return original(request, **kwargs)
    lane[0]['create'] = create
    run(lane); run(lane, 301)
    for sec in [602, 1503, 5000]:
        run(lane, sec)
    assert len(attempts) == 1
    job = next(iter(jobs().values()))
    assert job['moment_diagnostics'][-1]['reason'] == 'Dark footage'
    assert 'source_path' not in job['moment_diagnostics'][-1]
    add_source(lane, 'new')
    run(lane, 6000)
    assert run(lane, 6301)['status'] == 'staged'
    assert lane[2][0]['asset_ids'] == ['0', '1', 'new']
    assert len(attempts) == 2


def test_same_drive_id_new_bytes_not_lost_to_our_stale_usage_count(lane):
    run(lane); run(lane, 301)
    for asset in lane[4].values():
        asset['used_count'] = 1
        asset['content_hash'] += '_new_version'
    run(lane, 400)
    assert run(lane, 701)['status'] == 'staged'
    assert len(lane[2]) == 2
    assert lane[2][0]['id'] != lane[2][1]['id']


def test_unknown_or_extra_other_lane_usage_stays_excluded(lane):
    for asset in lane[4].values():
        asset['used_count'] = 1
    run(lane); run(lane, 301)
    assert not lane[2]
    for asset in lane[4].values():
        asset['used_count'] = 0
    run(lane, 400); run(lane, 701)
    for asset in lane[4].values():
        asset['used_count'] = 2
        asset['content_hash'] += '_new'
    run(lane, 800); run(lane, 1101)
    assert len(lane[2]) == 1


def test_recovery_uses_durable_exact_segment_ids_and_releases_unused_sources(lane):
    import json
    add_source(lane, 'unused1'); add_source(lane, 'unused2')
    original = lane[0]['create']
    def lost(request, **kwargs):
        original(request, **kwargs)
        ar.db.kv_set('story_studio_segs:' + request['id'], json.dumps({'gym_id': 'gym', 'asset_ids': ['0', '1', '2']}))
        for aid in ['0', '1', '2']:
            lane[4][aid]['used_count'] = 1
        raise TimeoutError()
    lane[0]['create'] = lost
    run(lane); run(lane, 301)
    assert run(lane, 602)['reconciled']
    assert next(iter(jobs().values()))['used'] == ['0', '1', '2']
    lane[0]['create'] = original
    add_source(lane, 'new')
    run(lane, 700)
    assert run(lane, 1001)['status'] == 'staged'
    assert lane[2][-1]['asset_ids'] == ['unused1', 'unused2', 'new']


def test_legacy_exhausted_single_clip_reservation_is_released(lane):
    import json
    entry = {'id': '0', 'key': 'hash0', 'context': ar._digest([None, None])}
    ar.db.kv_set('auto_reels:v1:gym', json.dumps({'jobs': {'legacy': {
        'entries': [entry], 'attempts': 3, 'status': 'held',
        'reason': 'At least three eligible clips are needed for an automatic reel'}}}))
    run(lane)
    assert run(lane, 301)['status'] == 'staged'
    assert lane[2][0]['asset_ids'] == ['0', '1', '2']


# ---- operator retry / status / held reporting (ECHO-REEL-RECOVERY-01) --------

def _exhaust_hold(lane, reason="Approved CTA is missing"):
    calls = []
    lane[0]["create"] = lambda request, **kwargs: (
        calls.append(request) or {"status": "held", "reason": reason})
    run(lane)
    for sec in [301, 602, 1503, 5000]:
        run(lane, sec)
    assert len(calls) == 3
    return calls


def test_exhausted_surfaces_instead_of_false_waiting(lane):
    _exhaust_hold(lane)
    out = run(lane, 8000)
    assert out["status"] == "exhausted"
    assert out["request_id"]
    assert "operator retry" in out["reason"].lower() or out["attempts"] >= 3
    snap = ar.gym_status("gym")
    assert snap["ok"] is True
    assert snap["exhausted_count"] == 1
    assert snap["jobs"][0]["status"] == "exhausted"
    assert "http" not in snap["jobs"][0]["reason"].lower()


def test_operator_retry_after_exhausted_prerequisite_hold(lane):
    """Voice/CTA/music style holds exhaust; explicit retry keeps same UUID then stages."""
    calls = _exhaust_hold(lane, reason="Approved CTA is missing")
    request_id = calls[0]["id"]
    out = run(lane, 8000)
    assert out["status"] == "exhausted"
    # Simulate prerequisite fixed: create succeeds on next attempt (same UUID).
    def succeed(request, **kwargs):
        calls.append(request)
        lane[0]["calendar"].rows["gym", request["id"]] = {"id": request["id"]}
        return {"status": "staged", "used_clips": request["asset_ids"]}
    lane[0]["create"] = succeed
    retried = ar.retry_exhausted("gym", request_id, now=lane[1] + __import__("datetime").timedelta(seconds=8100),
                                 note="CTA approved")
    assert retried["ok"] is True
    assert retried["request_id"] == request_id
    assert retried["operator_retries"] == 1
    staged = run(lane, 8200)
    assert staged["status"] == "staged"
    assert calls[-1]["id"] == request_id
    assert len([c for c in calls if c["id"] == request_id]) == 4  # 3 fails + 1 success
    assert jobs()[request_id]['attempts_total'] == 4


def test_retry_cannot_reuse_bytes_staged_by_enriched_replacement(lane):
    original=lane[0]['create']
    calls=_exhaust_hold(lane)
    old=calls[0]['id']
    for asset in lane[4].values():
        asset['brief']='Corrected approved context'
    lane[0]['create']=original
    run(lane,8100)
    assert run(lane,8500)['status']=='staged'
    for asset in lane[4].values():
        asset['used_count']=1
    assert ar.retry_exhausted('gym',old,now=lane[1])['ok'] is False
    run(lane,9000)
    assert len(lane[0]['calendar'].rows)==1


def test_hold_recurrence_and_missing_storage_deduplicate(lane,monkeypatch):
    a={'gym':'gym','status':'held','reason':'Gym identity is missing'}
    b={**a,'reason':'Persistent Echo worker storage is required'}
    assert [ar.report_outcome(x) for x in [a,a,b,a]]==[True,False,True,True]
    assert ar.report_outcome({'gym':'gym','status':'waiting'}) is False
    assert ar.report_outcome(a) is True
    monkeypatch.setattr(ar.db,'kv_is_durable',lambda:False)
    ar._LOG_TRANSITIONS.clear()
    assert [ar.report_outcome(x) for x in [a,a,b,a]]==[True,False,True,True]


def test_actual_storage_failure_logs_once_per_process(lane,monkeypatch,capsys):
    def broken(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(ar.db,'kv_get',broken)
    ar._LOG_TRANSITIONS.clear()
    held={'gym':'gym','status':'held','reason':'Job state is unavailable'}
    assert ar.report_outcome(held) is True
    assert ar.report_outcome(held) is False
    assert 'Job state is unavailable' in capsys.readouterr().out


def test_operator_retry_refuses_staged_and_wrong_gym(lane):
    run(lane); assert run(lane, 301)["status"] == "staged"
    request_id = lane[2][0]["id"]
    refused = ar.retry_exhausted("gym", request_id, now=lane[1])
    assert refused["ok"] is False
    assert refused["status"] == "staged"
    missing = ar.retry_exhausted("gym", "no-such-job", now=lane[1])
    assert missing["ok"] is False
    assert "No such job" in missing["reason"]
    foreign = ar.retry_exhausted("other-gym", request_id, now=lane[1])
    assert foreign["ok"] is False


def test_operator_retry_respects_lock(lane):
    _exhaust_hold(lane)
    request_id = next(iter(jobs()))
    started, release = threading.Event(), threading.Event()
    original_create = lane[0]["create"]

    def slow(request, **kwargs):
        started.set(); release.wait(5)
        return {"status": "staged", "used_clips": request["asset_ids"]}

    # Queue retry then hold lock via a running create on another path:
    # acquire lock by starting run_gym after retry queued.
    assert ar.retry_exhausted("gym", request_id, now=lane[1]).get("ok") is True
    lane[0]["create"] = slow
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run, lane, 9000)
        assert started.wait(5)
        busy = ar.retry_exhausted("gym", request_id, now=lane[1])
        assert busy["status"] == "busy"
        release.set()
        assert first.result()["status"] == "staged"


def test_report_outcome_deduplicates_transitions(lane, capsys):
    outcome = {"gym": "gym", "status": "held", "reason": "Gym identity is missing",
               "request_id": "preclaim"}
    assert ar.report_outcome(outcome) is True
    assert ar.report_outcome(outcome) is False  # same transition
    # New reason => new transition
    assert ar.report_outcome({**outcome, "reason": "Persistent Echo worker storage is required"}) is True
    # waiting stays quiet
    assert ar.report_outcome({"gym": "gym", "status": "waiting", "reason": "No settled new batch"}) is False
    captured = capsys.readouterr().out
    assert captured.count("[auto-reels] held for gym") == 2
    assert "http" not in captured.lower()


def test_listener_logs_held_via_report_outcome(lane, monkeypatch):
    from agent import listener
    seen = []
    monkeypatch.setattr(ar, "poll", lambda now=None: [
        {"gym": "gym", "status": "held", "reason": "Gym identity is missing"}])
    monkeypatch.setattr(ar, "report_outcome", lambda outcome: seen.append(outcome) or True)
    monkeypatch.setattr(listener.threading, "Thread",
                        lambda target=None, **kwargs: type("T", (), {
                            "start": lambda self: target(),
                            "is_alive": lambda self: False,
                        })())
    worker = listener._auto_reels_tick(None, lane[1])
    assert seen and seen[0]["status"] == "held"
    assert worker is not None


def test_response_loss_still_reconciles_after_operator_tools(lane):
    """Regression: calendar reply-loss path unchanged beside status/retry helpers."""
    original = lane[0]["create"]
    def lost(request, **kwargs):
        original(request, **kwargs)
        for asset in lane[4].values():
            asset["used_count"] = 1
        raise TimeoutError()
    lane[0]["create"] = lost
    run(lane); run(lane, 301)
    assert run(lane, 602)["reconciled"] is True
    assert len(lane[2]) == 1
    snap = ar.gym_status("gym")
    assert snap["jobs"][0]["status"] == "staged"
