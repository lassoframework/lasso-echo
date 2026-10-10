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
CLOSE_STATUS = {"lane": "support-ticket-close", "generation": 1, "unresolved": 0,
                "paused": True, "drained": True, "operation_id": "op-test-close"}

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


def _status_for(lane):
    return dict(LANE_STATUS if lane == "support-resolution-send" else CLOSE_STATUS)


def empty_page(lane):
    status = _status_for(lane)
    return {"lane": lane, "generation": status["generation"],
            "operation_id": status["operation_id"], "paused": True,
            "limit": fence._INVENTORY_PAGE, "returned": 0, "has_more": False,
            "next_after_started": None, "next_after_invocation": None,
            "invocations": []}


class Bus:
    def support_uncertain_outbound(self, limit=1000):
        return []

    def outbox(self, status, limit):
        return []

    def support_admission_status_lane(self, lane="support-resolution-send"):
        return _status_for(lane)

    def support_admission_inventory(self, lane, *, limit, after_started=None,
                                    after_invocation=None):
        return empty_page(lane)


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
    assert receipt['local_drained'] and receipt['blockers'] == []
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


def test_valid_ordinary_held_row_drains_with_verified_empty_inventory(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class Held(Bus):
        def outbox(self, status, limit):
            return [{'id': 'held-row', 'direction': 'outbound', 'delivery_status': 'held',
                     'attachments': {}}] if status == 'held' else []
    receipt = fence.receipt(Held())
    assert receipt['blockers'] == []
    assert receipt['local_drained']
    assert receipt['fleet_drained'] is False


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
        def support_uncertain_outbound(self, limit=1000):
            return []
        def support_admission_status_lane(self, lane="support-resolution-send"):
            return _status_for(lane)
        def support_admission_inventory(self, lane, *, limit, after_started=None,
                                        after_invocation=None):
            return empty_page(lane)
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
    assert observed['blockers'] == []
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
    assert observed['blockers'] == []
    assert observed['active'] == {}
    assert observed['fleet_drained'] is False


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
        def support_admission_status_lane(self, lane="support-resolution-send"):
            return {**_status_for(lane), **status_override}
    receipt = fence.receipt(LaneBus())
    assert not receipt['local_drained']
    assert any(blocker.startswith('admission_lane_invalid:')
               for blocker in receipt['blockers'])


@pytest.mark.parametrize('bad_status', [None, [], 'ok', {'paused': True}])
def test_malformed_admission_lane_status_blocks_drain(monkeypatch, tmp_path, bad_status):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        def support_admission_status_lane(self, lane="support-resolution-send"):
            return bad_status
    receipt = fence.receipt(LaneBus())
    assert not receipt['local_drained']
    assert any(blocker.startswith('admission_lane_invalid:')
               for blocker in receipt['blockers'])


def test_admission_lane_change_during_scans_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        calls = 0
        def support_admission_status_lane(self, lane="support-resolution-send"):
            type(self).calls += 1
            status = _status_for(lane)
            if type(self).calls > 1:
                status['operation_id'] = 'op-next'
            return status
    receipt = fence.receipt(LaneBus())
    assert not receipt['local_drained']
    assert any(blocker.startswith('admission_lane_changed:')
               for blocker in receipt['blockers'])


def test_unavailable_admission_lane_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class LaneBus(Bus):
        def support_admission_status_lane(self, lane="support-resolution-send"):
            raise RuntimeError('rpc unavailable')
    assert not fence.receipt(LaneBus())['local_drained']
    class NoLane(Bus):
        support_admission_status_lane = None
    assert not fence.receipt(NoLane())['local_drained']


def test_completed_admission_row_fails_closed_pending_external_verification(
        monkeypatch, tmp_path):
    # 0633 revoked raw invocation SELECT and attachments is mutable: even a fully
    # self-consistent lease/completion/readback JSON is never a drain basis.
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_trusted_row()]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained']
    assert ('admission_external_verification_required:admitted-row'
            in receipt['blockers'])


def test_no_admission_zero_invocation_inventory_drains(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class PlainBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_admission_row()]
    receipt = fence.receipt(PlainBus())
    assert receipt['blockers'] == []
    assert receipt['local_drained']
    assert receipt['fleet_drained'] is False


def test_current_notice_flat_verified_readback_still_fails_closed(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    row = _admission_row(lease=dict(LEASE, binding=dict(BINDING)),
                         completion=dict(COMPLETION))
    row['attachments'].update(READBACK)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [row]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained']
    assert ('admission_external_verification_required:admitted-row'
            in receipt['blockers'])


def test_admission_lease_without_trusted_completion_blocks_at_any_status(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    for status in ('posted', 'failed', 'ready', 'held'):
        class AdmissionBus(Bus):
            def support_uncertain_outbound(self, limit=1000):
                return [_admission_row(status=status, lease=dict(LEASE, binding=dict(BINDING)))]
        receipt = fence.receipt(AdmissionBus())
        assert not receipt['local_drained'], status
        assert ('admission_external_verification_required:admitted-row'
                in receipt['blockers'])


@pytest.mark.parametrize('status', ['ready', 'failed', 'held'])
def test_completed_admission_row_not_terminal_posted_blocks_drain(
        monkeypatch, tmp_path, status):
    pause(monkeypatch, tmp_path)
    class AdmissionBus(Bus):
        def support_uncertain_outbound(self, limit=1000):
            return [_trusted_row(status=status)]
    receipt = fence.receipt(AdmissionBus())
    assert not receipt['local_drained']
    assert ('admission_external_verification_required:admitted-row'
            in receipt['blockers'])


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
    row_id = row_override.get('id', 'admitted-row')
    assert (f'admission_external_verification_required:{row_id}'
            in receipt['blockers'])


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


# ---- portal 0633 paginated invocation inventory verification -------------------

INV_A = '11111111-1111-4111-8111-111111111111'
INV_B = '11111111-1111-4111-8111-111111111112'
MSG_A = '22222222-2222-4222-8222-222222222222'
TICKET_A = '33333333-3333-4333-8333-333333333333'


def _invocation(inv_id=INV_A, *, lane='support-resolution-send', outcome='completed',
                unresolved=False, started='2026-10-10 00:00:00+00',
                ended='2026-10-10 00:00:01+00', generation=1,
                ticket_id=TICKET_A, request_version=3, message_id=MSG_A):
    return {'invocation_id': inv_id, 'lane': lane, 'generation': generation,
            'deployment': 'dep-1', 'build': 'sha-1', 'started_at': started,
            'ended_at': ended, 'outcome': outcome, 'unresolved': unresolved,
            'ticket_id': ticket_id, 'request_version': request_version,
            'message_id': message_id}


def _page(lane, rows, *, has_more=False, **overrides):
    status = _status_for(lane)
    page = {'lane': lane, 'generation': status['generation'],
            'operation_id': status['operation_id'], 'paused': True,
            'limit': fence._INVENTORY_PAGE, 'returned': len(rows),
            'has_more': has_more,
            'next_after_started': rows[-1]['started_at'] if rows else None,
            'next_after_invocation': rows[-1]['invocation_id'] if rows else None,
            'invocations': rows}
    page.update(overrides)
    return page


class InventoryBus(Bus):
    """Serves fixed send-lane inventory pages; the close lane stays empty."""
    pages = ()

    def __init__(self):
        self.calls = []

    def support_admission_inventory(self, lane, *, limit, after_started=None,
                                    after_invocation=None):
        self.calls.append((lane, after_started, after_invocation))
        if lane != 'support-resolution-send':
            return empty_page(lane)
        index = 0
        if after_started is not None:
            index = 1 + next(i for i, page in enumerate(self.pages)
                             if page['invocations']
                             and (page['invocations'][-1]['started_at'],
                                  page['invocations'][-1]['invocation_id'])
                             == (after_started, after_invocation))
        return self.pages[index]


def _verified_message(**overrides):
    row = {'id': MSG_A, 'ticket_id': TICKET_A, 'direction': 'outbound',
           'body': BODY, 'delivery_status': 'posted', 'delivery_request_version': 3,
           'slack_ts': '1700.000002',
           'attachments': {'support_resolution_send_readback': dict(READBACK)}}
    row.update(overrides)
    return row


def test_paginated_completed_send_inventory_requires_external_proof(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    monkeypatch.setattr(fence, '_INVENTORY_PAGE', 1)
    first = _invocation(INV_A, started='2026-10-10 00:00:00+00')
    second = _invocation(INV_B, started='2026-10-10 00:00:02+00',
                         message_id=MSG_A)
    class Paged(InventoryBus):
        pages = (_page('support-resolution-send', [first], has_more=True),
                 _page('support-resolution-send', [second]))
        def message(self, message_id):
            assert message_id == MSG_A
            return _verified_message()
    bus = Paged()
    receipt = fence.receipt(bus)
    assert not receipt['local_drained']
    assert f'admission_external_verification_required:{MSG_A}' in receipt['blockers']
    assert receipt['fleet_drained'] is False
    assert bus.calls == [('support-resolution-send', None, None),
                         ('support-resolution-send', '2026-10-10 00:00:00+00', INV_A),
                         ('support-ticket-close', None, None)]


def test_stripped_marker_and_forged_stored_readback_cannot_clear_drain(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class Forged(InventoryBus):
        pages = (_page('support-resolution-send', [_invocation()]),)
        def message(self, message_id):
            row = _verified_message()
            # No admission marker remains. The mutable proof appears valid.
            assert 'support_resolution_send_admission' not in row['attachments']
            return row
    receipt = fence.receipt(Forged())
    assert not receipt['local_drained']
    assert f'admission_external_verification_required:{MSG_A}' in receipt['blockers']


def test_first_lane_resume_during_second_lane_scan_blocks_drain(
        monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class Changed(Bus):
        changed = False
        def support_admission_status_lane(self, lane='support-resolution-send'):
            status = _status_for(lane)
            if lane == 'support-resolution-send' and self.changed:
                status['paused'] = False
                status['drained'] = False
            return status
        def support_admission_inventory(self, lane, *, limit,
                                        after_started=None, after_invocation=None):
            if lane == 'support-ticket-close':
                self.changed = True
            return empty_page(lane)
    receipt = fence.receipt(Changed())
    assert not receipt['local_drained']
    assert 'admission_lane_changed:support-resolution-send' in receipt['blockers']


@pytest.mark.parametrize('override', [
    {'limit': 50},                                     # page size mismatch
    {'returned': 0},                                   # returned count mismatch
    {'returned': 2},
    {'has_more': 'yes'},                               # non-boolean has_more
    {'has_more': True, 'next_after_started': None},    # cursor dropped
    {'next_after_invocation': INV_B},                  # cursor past last item
    {'lane': 'support-ticket-close'},                  # wrong lane echoed
    {'generation': True},                              # bool is not generation 1
    {'generation': 2},                                 # generation changed mid-scan
    {'operation_id': 'op-other'},                      # operation changed mid-scan
    {'paused': False},                                 # lane resumed mid-scan
])
def test_tampered_inventory_page_blocks_drain(monkeypatch, tmp_path, override):
    pause(monkeypatch, tmp_path)
    malformed_page = _page('support-resolution-send', [_invocation()])
    malformed_page.update(override)
    class Tampered(InventoryBus):
        pages = (malformed_page,)
        def message(self, message_id):
            return _verified_message()
    receipt = fence.receipt(Tampered())
    assert not receipt['local_drained'], override
    assert 'admission_inventory_malformed:support-resolution-send' in receipt['blockers']


@pytest.mark.parametrize('rows', [
    [_invocation(INV_B, started='2026-10-10 00:00:00+00'),
     _invocation(INV_A, started='2026-10-10 00:00:00+00')],   # order not unique-increasing
    [_invocation(INV_A, started='2026-10-10 00:00:00+00'),
     _invocation(INV_A, started='2026-10-10 00:00:00+00')],   # duplicate key
    [_invocation(INV_A, started='2026-10-10 00:00:00+00'),
     _invocation(INV_A, started='2026-10-10 00:00:02+00')],   # duplicate id
    [_invocation('not-a-uuid')],                              # malformed invocation id
    [{**_invocation(), 'lane': 'support-ticket-close'}],      # wrong lane item
])
def test_inventory_ordering_and_identity_defects_block_drain(
        monkeypatch, tmp_path, rows):
    pause(monkeypatch, tmp_path)
    class BadRows(InventoryBus):
        pages = (_page('support-resolution-send', rows),)
        def message(self, message_id):
            return _verified_message()
    receipt = fence.receipt(BadRows())
    assert not receipt['local_drained']
    assert 'admission_inventory_malformed:support-resolution-send' in receipt['blockers']


def test_completed_send_with_missing_message_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class Missing(InventoryBus):
        pages = (_page('support-resolution-send', [_invocation()]),)
        def message(self, message_id):
            return None
    receipt = fence.receipt(Missing())
    assert not receipt['local_drained']
    assert f'admission_completed_unverified:{INV_A}' in receipt['blockers']


@pytest.mark.parametrize('override', [
    {'ticket_id': '33333333-3333-4333-8333-333333333334'},
    {'delivery_request_version': 4},
    {'body': 'tampered body'},
    {'slack_ts': None},
    {'slack_ts': '1700.999999'},                          # readback ts mismatch
    {'attachments': {'support_resolution_send_readback':
                     {**READBACK, 'delivery_readback_verified': False}}},
    {'attachments': {}},                                  # readback proof missing
])
def test_completed_send_message_or_readback_mismatch_blocks_drain(
        monkeypatch, tmp_path, override):
    pause(monkeypatch, tmp_path)
    class Mismatch(InventoryBus):
        pages = (_page('support-resolution-send', [_invocation()]),)
        def message(self, message_id):
            return _verified_message(**override)
    receipt = fence.receipt(Mismatch())
    assert not receipt['local_drained'], override
    assert f'admission_completed_unverified:{INV_A}' in receipt['blockers']


@pytest.mark.parametrize('outcome,unresolved,ended,prefix', [
    ('running', True, None, 'admission_running:'),
    ('unknown', True, '2026-10-10 00:00:01+00', 'admission_unknown:'),
])
def test_unresolved_inventory_invocation_blocks_drain(
        monkeypatch, tmp_path, outcome, unresolved, ended, prefix):
    pause(monkeypatch, tmp_path)
    row = _invocation(outcome=outcome, unresolved=unresolved, ended=ended)
    class Unresolved(InventoryBus):
        pages = (_page('support-resolution-send', [row]),)
    receipt = fence.receipt(Unresolved())
    assert not receipt['local_drained']
    assert any(blocker.startswith(prefix) for blocker in receipt['blockers'])


def test_legacy_unbound_completed_invocation_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    row = _invocation(ticket_id=None, request_version=None, message_id=None)
    class Legacy(InventoryBus):
        pages = (_page('support-resolution-send', [row]),)
    receipt = fence.receipt(Legacy())
    assert not receipt['local_drained']
    assert f'admission_legacy_unbound:support-resolution-send:{INV_A}' in receipt['blockers']


def test_close_lane_completed_invocation_fails_closed(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    row = _invocation(lane='support-ticket-close')
    class CloseHistory(Bus):
        def support_admission_inventory(self, lane, *, limit, after_started=None,
                                        after_invocation=None):
            if lane == 'support-ticket-close':
                return _page(lane, [row])
            return empty_page(lane)
    receipt = fence.receipt(CloseHistory())
    assert not receipt['local_drained']
    assert f'admission_close_unverified:support-ticket-close:{INV_A}' in receipt['blockers']


def test_close_lane_unresolved_invocation_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    row = _invocation(lane='support-ticket-close', outcome='unknown',
                      unresolved=True, ticket_id=TICKET_A, request_version=3,
                      message_id=MSG_A)
    class CloseUnknown(Bus):
        def support_admission_inventory(self, lane, *, limit, after_started=None,
                                        after_invocation=None):
            if lane == 'support-ticket-close':
                return _page(lane, [row])
            return empty_page(lane)
    receipt = fence.receipt(CloseUnknown())
    assert not receipt['local_drained']
    assert any(blocker.startswith('admission_unknown:support-ticket-close:')
               for blocker in receipt['blockers'])


def test_unavailable_inventory_rpc_blocks_drain(monkeypatch, tmp_path):
    pause(monkeypatch, tmp_path)
    class NoInventory(Bus):
        support_admission_inventory = None
    receipt = fence.receipt(NoInventory())
    assert not receipt['local_drained']
    assert 'admission_inventory_unavailable:support-resolution-send' in receipt['blockers']
    class Failing(Bus):
        def support_admission_inventory(self, lane, *, limit, after_started=None,
                                        after_invocation=None):
            raise RuntimeError('rpc unavailable')
    receipt = fence.receipt(Failing())
    assert not receipt['local_drained']
    assert any(blocker.startswith('admission_inventory_read:')
               for blocker in receipt['blockers'])


def test_verified_inventory_never_sets_fleet_drain(monkeypatch, tmp_path):
    # No process-local evidence, however complete, may assert a fleet-wide drain.
    pause(monkeypatch, tmp_path)
    receipt = fence.receipt(Bus())
    assert receipt['local_drained'] and receipt['blockers'] == []
    assert receipt['fleet_drained'] is False
