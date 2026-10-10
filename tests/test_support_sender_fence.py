import hashlib
import json
import threading
import pytest
from tests.test_support_slack_replay import pg_bus, pg_engine  # real replay RPC fixture
from types import SimpleNamespace

from agent import support_sender_fence as fence
from agent.slack_convo import outbox, outreach
from agent import echo_ticket_worker


LANE_STATUS = {"lane": "support-resolution-send", "generation": 1, "unresolved": 0,
               "paused": True, "drained": True, "operation_id": "op-test"}

BODY = "Update from the LASSO team: this one is handled."
BODY_SHA = hashlib.sha256(BODY.encode()).hexdigest()
BINDING = {"message_id": "admitted-row",
           "ticket": {"id": "ticket-1", "request_version": 3},
           "sender_identity": "echo", "sender": "U-bot-1", "channel": "C-chan-1",
           "thread_ts": "1700.000001", "body_sha256": BODY_SHA, "request_key": "rk-1"}
LEASE = {"lane": "support-resolution-send", "invocation_id": "inv-1", "generation": 1,
         "deployment": "dep-1", "build": "sha-1", "binding": BINDING,
         "not_before": "2026-10-10T00:00:00+00:00"}
COMPLETION = {"recorded": True, "lane": "support-resolution-send",
              "invocation_id": "inv-1", "outcome": "completed", "generation": 1}
READBACK = {"delivery_readback_verified": True, "delivery_readback_channel": "C-chan-1",
            "delivery_readback_thread_ts": "1700.000001",
            "delivery_readback_ts": "1700.000002", "delivery_readback_sender": "U-bot-1",
            "delivery_readback_body_sha256": BODY_SHA,
            "delivery_readback_request_key": "rk-1",
            "delivery_readback_request_version": 3}


class Bus:
    def support_uncertain_outbound(self, limit=1000):
        return []

    def outbox(self, status, limit):
        return []

    def support_admission_status_lane(self):
        return dict(LANE_STATUS)


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


@pytest.mark.parametrize('identity', ['echo', 'scout'])
@pytest.mark.parametrize('lane', ['portal_intake', 'portal_fixed'])
def test_portal_only_paused_polls_emit_live_process_receipt(monkeypatch, tmp_path,
                                                          identity, lane):
    pause(monkeypatch, tmp_path)
    lines = []
    class ReadOnlyBus(Bus):
        def __init__(self):
            self.reads = []
        def outbox(self, status, limit):
            self.reads.append((status, limit))
            return []
        def __getattr__(self, name):
            raise AssertionError(f'paused poll attempted {name}')
    bus = ReadOnlyBus()
    kwargs = dict(open_group_dm=None, post_first_message=None,
                  identity_name=identity, log=lines.append)
    if lane == 'portal_intake':
        kwargs.update(slack_lookup_email=None, slack_user_info=None,
                      portal_lookup=None, write_hold_notice=None)
        result = echo_ticket_worker.intake_pass(bus, **kwargs)
    else:
        result = echo_ticket_worker.fixed_pass(bus=bus, **kwargs)
    assert result['paused'] == 1
    assert bus.reads == [('posting', 1000), ('held', 1000)]
    assert len(lines) == 1
    observed = json.loads(lines[0].removeprefix('[support-sender-fence] '))
    assert observed['lane'] == lane
    assert observed['process_id'] == fence.receipt()['process_id']
    assert observed['generation'] == '0619-test'
    assert observed['deployed_sha'] == 'test-sha'
    assert observed['observed_at']
    assert observed['active'] == {}
    assert observed['local_drained']
    assert observed['fleet_drained'] is False


def test_receipt_emits_after_admitted_pass_finishes(monkeypatch, tmp_path):
    path = pause(monkeypatch, tmp_path)
    path.write_text(json.dumps({'paused': False, 'generation': '0619-test'}))
    lines = []
    @_fence_pass_decorator()
    def running_pass(bus, *, log):
        assert fence.receipt(bus)['active'] == {'portal_test': 1}
        path.write_text(json.dumps({'paused': True, 'generation': '0619-test'}))
        return {'processed': 1}
    assert running_pass(Bus(), log=lines.append) == {'processed': 1}
    observed = json.loads(lines[0].removeprefix('[support-sender-fence] '))
    assert observed['local_drained']
    assert observed['active'] == {}


def _fence_pass_decorator():
    return fence.guarded('portal_test', lambda: {'paused': 1}, receipt_on_pause=True)


def test_unpaused_hook_does_not_read_database_or_log(monkeypatch, tmp_path):
    path = pause(monkeypatch, tmp_path)
    path.write_text(json.dumps({'paused': False, 'generation': '0619-test'}))
    class NoReads:
        def __getattr__(self, name):
            raise AssertionError(name)
    @_fence_pass_decorator()
    def running_pass(bus, *, log):
        return 'unchanged'
    def no_logs(line):
        raise AssertionError(line)
    assert running_pass(NoReads(), log=no_logs) == 'unchanged'


def test_portal_receipt_database_failure_blocks_acknowledgment(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class FailedRead(Bus):
        def outbox(self, status, limit):
            raise RuntimeError('unavailable')
    lines = []
    assert echo_ticket_worker.fixed_pass(FailedRead(), open_group_dm=None,
        post_first_message=None, log=lines.append)['paused'] == 1
    observed = json.loads(lines[0].removeprefix('[support-sender-fence] '))
    assert observed['blockers'] == ['database_read:RuntimeError']
    assert observed['local_drained'] is False


def test_failed_receipt_log_preserves_result_and_original_exception(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    def failed_log(line):
        raise RuntimeError('log unavailable')
    assert echo_ticket_worker.fixed_pass(Bus(), open_group_dm=None,
        post_first_message=None, log=failed_log) == {'notified': 0, 'paused': 1}
    @_fence_pass_decorator()
    def failed_pass(bus, *, log):
        monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_PAUSED', 'true')
        raise ValueError('original failure')
    monkeypatch.delenv('SUPPORT_MESSAGES_FENCE_CONTROL_FILE')
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_GENERATION', 'exception-test')
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_PAUSED', 'false')
    with pytest.raises(ValueError, match='original failure'):
        failed_pass(Bus(), log=failed_log)
    assert fence.receipt(Bus())['active'] == {}


@pytest.mark.parametrize('identity', ['echo', 'scout', 'ranger', 'wrangler'])
def test_paused_client_dm_producers_do_not_poll_or_write(monkeypatch, tmp_path, identity):
    from agent.client_dm_support import lane
    pause(monkeypatch, tmp_path)
    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(name)
    bus = NoCalls()
    assert lane.run_once(bus=bus, identity=SimpleNamespace(name=identity))['paused'] == 1
    assert lane._deliver(bus, None, None, None, None, None)['paused'] == 1
    assert lane.run_once_all_identities(bus=bus)['handled'] == 0


@pytest.mark.parametrize('identity', ['echo', 'scout', 'ranger', 'wrangler'])
def test_paused_adapter_preserves_inbound_tenant_and_source_without_outbound(
        monkeypatch, tmp_path, pg_bus, identity):
    from tests.test_slack_convo import _deps, _ev
    from agent.slack_convo import adapter
    pause(monkeypatch, tmp_path)
    bus = pg_bus
    result = adapter.handle_event(_ev('my photos are broken'), 'G0MPIM:1.001',
                                  _deps(bus, identity=identity))
    assert result.reason == 'support_sender_paused'
    assert len(bus.tickets) == 1
    ticket = bus.tickets[result.ticket_id]
    assert ticket['source'] == 'slack_conversation'
    assert ticket['client_id'] == 'g-1'
    assert ticket['bot_identity'] == identity
    assert ticket['product'] == _deps(bus, identity=identity).identity.product
    assert len(bus.msgs) == 1
    assert bus.msgs[0]['direction'] == 'inbound'
    assert bus.msgs[0]['ticket_id'] == result.ticket_id
    duplicate = adapter.handle_event(_ev('my photos are broken'), 'G0MPIM:1.001',
                                     _deps(bus, identity=identity))
    assert duplicate.duplicate
    assert len(bus.msgs) == 1


@pytest.mark.parametrize('identity', ['echo', 'scout'])
def test_pre_admitted_adapter_can_finish_exact_owned_outbound_after_pause(
        monkeypatch, tmp_path, pg_bus, identity):
    from tests.test_slack_convo import _deps, _ev
    from agent.slack_convo import adapter
    path = pause(monkeypatch, tmp_path)
    path.write_text(json.dumps({'paused': False, 'generation': '0619-test'}))
    bus = pg_bus
    with fence.admission('existing_producer') as allowed:
        assert allowed
        path.write_text(json.dumps({'paused': True, 'generation': '0619-test'}))
        result = adapter.handle_event(_ev('my photos are broken'), 'G0MPIM:1.001',
                                      _deps(bus, identity=identity))
        outbound = [row for row in bus.msgs if row['direction'] == 'outbound']
        assert outbound
        assert all(row['ticket_id'] == result.ticket_id for row in outbound)
        assert all(row['attachments']['identity'] == identity for row in outbound)
        assert fence.receipt()['active'] == {'existing_producer': 1}
    assert fence.receipt()['active'] == {}


def test_pre_admitted_client_dm_delivery_finishes_after_pause(monkeypatch, tmp_path):
    from agent.client_dm_support import lane
    path = pause(monkeypatch, tmp_path)
    path.write_text(json.dumps({'paused': False, 'generation': '0619-test'}))
    rows = []
    bus = SimpleNamespace(record_outbound=lambda **kw: rows.append(kw))
    decision = lane.Decision(lane.Outcome.REPLY, 'grounded', reply_text='Verified text',
                             gym_key='crossfitlocal', condition_id='test-condition')
    arm = SimpleNamespace(may_reply_to_clients=True)
    monkeypatch.setattr(lane, '_card_text', lambda *args: 'Internal card')
    with fence.admission('existing_client_dm') as allowed:
        assert allowed
        path.write_text(json.dumps({'paused': True, 'generation': '0619-test'}))
        result = lane._deliver(bus, {'id': 'tenant-ticket'}, SimpleNamespace(name='echo'),
                               decision, arm, 'mpim')
        assert result == {'reply': True, 'card': True, 'undelivered': 0}
    assert len(rows) == 2
    assert all(row['ticket_id'] == 'tenant-ticket' for row in rows)
    assert all(row['meta']['identity'] == 'echo' for row in rows)
    assert all(row['delivery_status'] == 'ready' for row in rows)


def test_paused_standalone_producers_and_release_handler_do_not_write(monkeypatch, tmp_path):
    from agent.slack_convo import adapter, listener_wiring
    from agent.jobs import stale_escalation_reminder
    pause(monkeypatch, tmp_path)
    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(name)
    bus = NoCalls()
    assert not adapter._client_hold_notice(bus, None)
    assert not adapter.route_follow_up_promise(bus, None)
    assert adapter.write_hold_notice(bus) is None
    assert adapter.hold_answer_for_team(bus)['why'] == 'support_sender_paused'
    wiring = listener_wiring.ConvoWiring.__new__(listener_wiring.ConvoWiring)
    assert not wiring._release_outreach(None, None, None)
    result = outreach.request_approval(None, None, None)
    assert not result.requested and result.reason == 'support_sender_paused'
    assert stale_escalation_reminder.run(bus=bus, enabled=True)['paused'] == 1


# ---- portal 0633 durable admission lane binding (fail closed) --------------------

def _admission_row(status='posted', lease=None, completion=None, readback=None,
                   **overrides):
    att = {}
    if lease is not None:
        att['support_resolution_send_admission'] = lease
    if completion is not None:
        att['support_resolution_send_completion'] = completion
    if readback is not None:
        att['support_resolution_send_readback'] = readback
    row = {'id': 'admitted-row', 'direction': 'outbound', 'delivery_status': status,
           'ticket_id': 'ticket-1', 'body': BODY, 'delivery_request_version': 3,
           'slack_ts': '1700.000002', 'attachments': att}
    row.update(overrides)
    return row


def _trusted_row(**overrides):
    return _admission_row(lease=dict(LEASE, binding=dict(BINDING)),
                          completion=dict(COMPLETION), readback=dict(READBACK),
                          **overrides)


@pytest.mark.parametrize('status_override', [
    {'paused': False, 'drained': False},
    {'paused': True, 'drained': False},
    {'paused': True, 'drained': True, 'unresolved': 1},
    {'paused': True, 'drained': True, 'generation': True},
    {'paused': True, 'drained': True, 'generation': '1'},
    {'paused': True, 'drained': True, 'generation': -1},
    {'paused': True, 'drained': True, 'operation_id': ''},
    {'paused': True, 'drained': True, 'operation_id': None},
    {'paused': True, 'drained': True, 'lane': 'other-lane'},
    {'paused': 'true', 'drained': True},
])
def test_invalid_admission_lane_status_blocks_drain(monkeypatch, tmp_path,
                                                    status_override):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        def support_admission_status_lane(self):
            return {**LANE_STATUS, **status_override}
    receipt = fence.receipt(LaneBus())
    assert not receipt['local_drained']
    assert 'admission_lane_invalid' in receipt['blockers']


@pytest.mark.parametrize('bad_status', [None, [], 'ok', {'paused': True}])
def test_malformed_admission_lane_status_blocks_drain(monkeypatch, tmp_path, bad_status):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        def support_admission_status_lane(self):
            return bad_status
    receipt = fence.receipt(LaneBus())
    assert not receipt['local_drained']
    assert 'admission_lane_invalid' in receipt['blockers']


def test_admission_lane_change_during_scans_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        calls = 0
        def support_admission_status_lane(self):
            type(self).calls += 1
            status = dict(LANE_STATUS)
            if type(self).calls > 1:
                status['operation_id'] = 'op-next'
            return status
    receipt = fence.receipt(LaneBus())
    assert not receipt['local_drained']
    assert 'admission_lane_changed' in receipt['blockers']


def test_unavailable_admission_lane_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        def support_admission_status_lane(self):
            raise RuntimeError('rpc unavailable')
    assert not fence.receipt(LaneBus())['local_drained']
    class NoLane(Bus):
        support_admission_status_lane = None
    assert not fence.receipt(NoLane())['local_drained']


def test_fully_bound_completed_admission_row_and_zero_unresolved_lane_allows_drain(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_trusted_row()]
    receipt = fence.receipt(AdmissionBus())
    assert receipt['blockers'] == []
    assert receipt['local_drained']


def test_current_notice_flat_verified_readback_allows_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    row = _admission_row(lease=dict(LEASE, binding=dict(BINDING)),
                         completion=dict(COMPLETION))
    row['attachments'].update(READBACK)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [row]
    assert fence.receipt(AdmissionBus())['local_drained']


def test_admission_lease_without_trusted_completion_blocks_at_any_status(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    for status in ('posted', 'failed', 'ready', 'held'):
        class AdmissionBus(Bus):
            def support_uncertain_outbound(self, limit=1000):
                return [_admission_row(status=status, lease=dict(LEASE, binding=dict(BINDING)))]
        receipt = fence.receipt(AdmissionBus())
        assert not receipt['local_drained'], status
        assert 'admission_unresolved:admitted-row' in receipt['blockers']


@pytest.mark.parametrize('status', ['ready', 'failed', 'held'])
def test_completed_admission_row_not_terminal_posted_blocks_drain(
        monkeypatch, tmp_path, status):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_trusted_row(status=status)]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained']
    assert 'admission_unresolved:admitted-row' in receipt['blockers']


@pytest.mark.parametrize('row_override', [
    {'id': 'other-row'},                              # copied lease: wrong message id
    {'ticket_id': 'ticket-2'},                        # wrong ticket id
    {'delivery_request_version': 4},                  # stale request version
    {'body': 'tampered body'},                        # body sha mismatch
    {'slack_ts': None},                               # posted without Slack timestamp
    {'slack_ts': ''},
    {'slack_ts': '1700.999999'},                      # readback ts mismatch
])
def test_lease_binding_mismatch_blocks_drain(monkeypatch, tmp_path, row_override):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_trusted_row(**row_override)]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained'], row_override
    assert 'admission_unresolved:admitted-row' in receipt['blockers'] or \
        f"admission_unresolved:{row_override.get('id')}" in receipt['blockers']


@pytest.mark.parametrize('lease', [
    {k: v for k, v in LEASE.items() if k != 'binding'},      # missing binding
    {**LEASE, 'binding': None},
    {**LEASE, 'binding': {'message_id': 'admitted-row'}},    # binding without ticket
    {**LEASE, 'binding': {**BINDING, 'message_id': 'other-row'}},  # copied lease
    {**LEASE, 'binding': {**BINDING, 'body_sha256': '0' * 64}},
    {**LEASE, 'binding': {**BINDING, 'sender': 'U-other'}},
    {**LEASE, 'binding': {**BINDING, 'channel': 'C-other'}},
    {**LEASE, 'binding': {**BINDING, 'sender': ''}},
    {**LEASE, 'binding': {**BINDING, 'thread_ts': '1700.000009'}},
    {**LEASE, 'binding': {**BINDING, 'ticket': {'id': 'ticket-1', 'request_version': '3'}}},
    {**LEASE, 'lane': 'other-lane'},
    {**LEASE, 'invocation_id': ''},
    {**LEASE, 'generation': '1'},
])
def test_untrusted_lease_blocks_drain(monkeypatch, tmp_path, lease):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_admission_row(lease=lease, completion=dict(COMPLETION),
                                   readback=dict(READBACK))]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained'], lease


@pytest.mark.parametrize('readback', [
    None, 'proof', {},                                    # missing readback
    {**READBACK, 'delivery_readback_verified': False},
    {**READBACK, 'delivery_readback_ts': '1700.000009'},
    {**READBACK, 'delivery_readback_body_sha256': '0' * 64},
    {**READBACK, 'delivery_readback_sender': 'U-other'},
    {**READBACK, 'delivery_readback_channel': 'C-other'},
    {**READBACK, 'delivery_readback_thread_ts': '1700.000009'},
    {**READBACK, 'delivery_readback_request_version': 4},
])
def test_missing_or_unverified_readback_blocks_drain(monkeypatch, tmp_path, readback):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_admission_row(lease=dict(LEASE, binding=dict(BINDING)),
                                   completion=dict(COMPLETION), readback=readback)]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained'], readback


@pytest.mark.parametrize('completion', [
    None, 'done',                                          # missing receipt
    {'recorded': True, 'lane': 'other'},
    {**COMPLETION, 'invocation_id': 'other'},
    {**COMPLETION, 'recorded': False},
    {**COMPLETION, 'generation': 2},
    {**COMPLETION, 'generation': '1'},
    {**COMPLETION, 'outcome': 'unknown'},
    {**COMPLETION, 'outcome': None},
    {k: v for k, v in COMPLETION.items() if k != 'outcome'},
])
def test_untrusted_completion_receipt_blocks_drain(monkeypatch, tmp_path, completion):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_admission_row(lease=dict(LEASE, binding=dict(BINDING)),
                                   completion=completion, readback=dict(READBACK))]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained'], completion
