import json
import threading
import pytest
from types import SimpleNamespace

from agent import support_sender_fence as fence
from agent.slack_convo import outbox, outreach
from agent import echo_ticket_worker


class Bus:
    def outbox(self, status, limit):
        return []


def pause(monkeypatch, tmp_path):
    path = tmp_path / 'control.json'
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED', 'true')
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_CONTROL_FILE', str(path))
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'test-sha')
    path.write_text(json.dumps({'paused': True, 'generation': '0619-test'}))
    return path


def test_off_ignores_control(monkeypatch):
    monkeypatch.delenv('SUPPORT_MESSAGES_FENCE_ENABLED', raising=False)
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_CONTROL_FILE', '/does/not/exist')
    with fence.admission('test') as admitted:
        assert admitted
    assert not fence.receipt()['local_drained']


def test_all_entrypoints_preserve_rows(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class NoCalls:
        def __getattr__(self, key):
            raise AssertionError(key)
    bus = NoCalls()
    for identity in ('echo', 'scout', 'wrangler', 'ranger'):
        assert outbox.run_once(bus, None, identity=SimpleNamespace(name=identity)) == {'paused': 1}
    assert not outbox.release_held(bus, 'row', approved_by='operator')
    assert not outbox.resolve_and_notify(bus, 'ticket', approved_by='operator', identity=None)
    assert outreach._send(None, None, None, open_group_dm=None,
                          post_first_message=None, record_outbound=None).reason == 'support_sender_paused'
    assert echo_ticket_worker.intake_pass(bus, slack_lookup_email=None, slack_user_info=None,
        portal_lookup=None, open_group_dm=None, post_first_message=None,
        write_hold_notice=None)['paused'] == 1
    assert echo_ticket_worker.fixed_pass(bus, open_group_dm=None,
                                        post_first_message=None)['paused'] == 1


def test_waits_for_admitted_send(monkeypatch, tmp_path):
    path = pause(monkeypatch, tmp_path)
    path.write_text(json.dumps({'paused': False, 'generation': '0619-test'}))
    entered, release = threading.Event(), threading.Event()
    def send():
        with fence.admission('send') as allowed:
            assert allowed
            entered.set()
            assert release.wait(2)
            with fence.admission('nested') as allowed:
                assert allowed
    thread = threading.Thread(target=send)
    thread.start()
    assert entered.wait(2)
    path.write_text(json.dumps({'paused': True, 'generation': '0619-test'}))
    with fence.admission('new') as allowed:
        assert not allowed
    assert not fence.receipt(Bus())['local_drained']
    release.set()
    thread.join(2)
    receipt = fence.receipt(Bus())
    assert receipt['local_drained']
    assert not receipt['fleet_drained']
    assert receipt['generation'] == '0619-test'


def test_ambiguous_database_and_control_block_drain(monkeypatch, tmp_path):
    path = pause(monkeypatch, tmp_path)
    class Posting(Bus):
        def outbox(self, status, limit):
            return [{'id': 'unknown-send', 'direction': 'outbound',
                     'delivery_status': 'posting'}] if status == 'posting' else []
    assert not fence.receipt(Posting())['local_drained']
    class Held(Bus):
        def outbox(self, status, limit):
            return [{'id': 'uncertain', 'direction': 'outbound', 'delivery_status': status,
                     'attachments': {'outreach_delivery_uncertain': True}}]
    assert not fence.receipt(Held())['local_drained']
    path.write_text('invalid')
    assert fence.control()['paused']
    assert not fence.receipt(Bus())['local_drained']


@pytest.mark.parametrize('row', [
    {}, None, [], 'row',
    {'id': '', 'direction': 'outbound', 'delivery_status': 'held'},
    {'id': 'row', 'delivery_status': 'held'},
    {'id': 'row', 'direction': 'inbound', 'delivery_status': 'held'},
    {'id': 'row', 'direction': 'outbound', 'delivery_status': 'ready'},
    {'id': 'row', 'direction': 'outbound', 'delivery_status': 'held', 'attachments': None},
    {'id': 'row', 'direction': 'outbound', 'delivery_status': 'held', 'attachments': []},
])
def test_malformed_held_scan_never_acknowledges_drain(monkeypatch, tmp_path, row):
    pause(monkeypatch, tmp_path)
    class Malformed(Bus):
        def outbox(self, status, limit):
            return [row] if status == 'held' else []
    receipt = fence.receipt(Malformed())
    assert not receipt['local_drained']
    assert 'held_row_malformed' in receipt['blockers']


def test_valid_ordinary_held_row_allows_local_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class Held(Bus):
        def outbox(self, status, limit):
            return [{'id': 'held-row', 'direction': 'outbound', 'delivery_status': 'held',
                     'attachments': {}}] if status == 'held' else []
    assert fence.receipt(Held())['local_drained']
