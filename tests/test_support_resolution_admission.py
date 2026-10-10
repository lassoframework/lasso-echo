"""Offline 0624 send admission proof, using disposable PostgreSQL 17 only.

The fixture is the unchanged portal draft at 77eec4ea. No live deployment,
migration, identity lookup, Slack call or lane activation is performed.
"""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from agent.slack_convo import adapter, outbox
from agent.slack_convo import identities, listener_wiring
from agent.slack_convo.bus import Bus
from agent import slack_surface
from tests.test_support_slack_replay import (
    Transport, pg_engine, q, pg_bus, paused, ready_answer, admission_post,
)


class AdmissionTransport(Transport):
    def __init__(self, engine):
        super().__init__(engine)
        self.rpc_calls = []
        self.rewrite_receipt = None
        self.lose_patch_once = False

    def post(self, url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        if '/rpc/' in url:
            self.rpc_calls.append((name, json.loads(kwargs['data'])))
        result = super().post(url, **kwargs)
        if self.rewrite_receipt and name == 'support_admission_acquire_lane':
            data = self.rewrite_receipt(result.json())
            return SimpleNamespace(status_code=200, json=lambda: data)
        return result

    def patch(self, url, *, params, data, **kwargs):
        table = url.rsplit('/', 1)[-1]
        fields = json.loads(data)
        where = []
        for key, value in params.items():
            if value == 'is.null':
                where.append(f't.{key} is null')
            else:
                assert value.startswith('eq.')
                where.append(f't.{key}={q(value[3:])}')
        assignments = ','.join(f'{key}=r.{key}' for key in fields)
        result = self._response(lambda: json.loads(self.engine.sql(
            f'with written as (update public.{table} t set {assignments} '
            f'from jsonb_populate_record(null::public.{table},{q(data)}::jsonb) r '
            f'where {" and ".join(where)} returning t.*) '
            f"select coalesce(jsonb_agg(to_jsonb(written)),'[]') from written")))
        if self.lose_patch_once:
            self.lose_patch_once = False
            raise TimeoutError('lost CAS acknowledgement after COMMIT')
        return result


@pytest.fixture(scope='session')
def admission_engine(pg_engine):
    fixture = Path(__file__).parent/'fixtures/support_admission_0624_frozen.sql'
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        'e4481f5d4f6ef4907da7c1772d03bccaacd6135646d9a96e492481c3c8e41982')
    pg_engine.sql(fixture.read_text())
    return pg_engine


@pytest.fixture
def case(admission_engine, monkeypatch):
    engine = admission_engine
    engine.sql('truncate support_messages,support_tickets, '
               'support_admission_invocations cascade')
    engine.sql("update support_admission_control set paused=false, generation=0, "
               "allowed_builds='[{\"deployment\":\"test-deployment\",\"build\":\"test-build\"}]'")
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID', 'test-deployment')
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'test-build')
    monkeypatch.delenv('SUPPORT_MESSAGES_FENCE_ENABLED', raising=False)
    transport = AdmissionTransport(engine)
    bus = Bus(url='http://disposable.invalid', service_key='synthetic', http=transport)
    tid, mid = str(uuid.uuid4()), str(uuid.uuid4())
    ticket = {
        'id': tid, 'product': 'echo', 'source': 'echosupport', 'client_id': 'gym-one',
        'request_version': 1, 'status': 'verification', 'bot_identity': 'echo',
        'identity_kind': 'client', 'slack_user_id': 'U_CLIENT',
        'slack_channel_id': 'C_CLIENT', 'slack_thread_ts': '1.001',
        'verification_after': {'independent_verification': True},
    }
    att = {'identity': 'echo', 'kind': adapter.KIND_ANSWER,
           'recipient_kind': 'client', 'released_by': 'operator', 'request_version': 1}
    body = 'The scheduling fix is independently verified. This ticket is closed.'
    row = {'id': mid, 'ticket_id': tid, 'direction': 'outbound', 'author_type': 'echo',
           'body': body, 'delivery_status': 'posting', 'delivery_request_version': 1,
           'attachments': att}
    bus._insert('support_tickets', ticket)
    bus._insert('support_messages', row)
    ticket, row = bus.ticket(tid), bus.message(mid)
    sent = []
    def post(channel, text, *, thread_ts, blocks):
        ts = f'{datetime.now(timezone.utc).timestamp():.6f}'
        sent.append({'channel': channel, 'text': text, 'thread_ts': thread_ts,
                     'ts': ts, 'user': 'U_ECHO'})
        return ts
    def readback(channel, **kwargs):
        return {'ok': True, 'channel': channel, 'messages': copy.deepcopy(sent)}
    post.verify_sender = lambda: {'ok': True, 'user_id': 'U_ECHO'}
    identity = SimpleNamespace(name='echo', bot_user_id=lambda: 'U_ECHO')
    return SimpleNamespace(engine=engine, bus=bus, transport=transport, ticket=ticket,
                           row=row, att=att, body=body, identity=identity,
                           sent=sent, post=post, readback=readback)


def send(case, **changes):
    args = dict(bus=case.bus, post=case.post, row=case.row, ticket=case.ticket,
                identity=case.identity, body=case.body, channel='C_CLIENT',
                thread_ts='1.001', readback=case.readback,
                kind=adapter.KIND_ANSWER, att=case.att)
    args.update(changes)
    return outbox._post_support_resolution(**args)


def invocations(case):
    return case.engine.rows('support_admission_invocations')


def test_real_0624_lane_and_message_cas_bind_one_verified_send(case):
    ts = send(case)
    assert len(case.sent) == 1
    row = case.bus.message(case.row['id'])
    lease = row['attachments'][outbox.SUPPORT_SEND_ADMISSION_KEY]
    assert lease['binding']['ticket']['id'] == case.ticket['id']
    assert lease['binding']['ticket']['source'] == 'echosupport'
    assert lease['binding']['ticket']['request_version'] == row['delivery_request_version'] == 1
    assert lease['binding']['sender_identity'] == 'echo'
    assert lease['binding']['sender'] == 'U_ECHO'
    assert lease['binding']['channel'] == 'C_CLIENT'
    assert lease['binding']['thread_ts'] == '1.001'
    assert lease['binding']['body_sha256'] == hashlib.sha256(case.body.encode()).hexdigest()
    assert row['slack_ts'] == ts
    assert row['attachments']['support_resolution_send_readback']['delivery_readback_verified']
    assert row['attachments']['support_resolution_send_completion']['outcome'] == 'completed'
    assert invocations(case)[0]['outcome'] == 'completed'
    assert invocations(case)[0]['unresolved'] is False
    assert invocations(case)[0]['deployment'] == lease['deployment'] == 'test-deployment'
    assert invocations(case)[0]['build'] == lease['build'] == 'test-build'
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert len(case.sent) == 1


@pytest.mark.parametrize('field', ['RAILWAY_DEPLOYMENT_ID', 'RAILWAY_GIT_COMMIT_SHA'])
def test_no_identity_fallback(case, monkeypatch, field):
    monkeypatch.delenv(field)
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not case.transport.rpc_calls


def test_paused_lane_holds_without_acquire_or_send(case):
    case.engine.sql("update support_admission_control set paused=true where lane='support-resolution-send'")
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not invocations(case)
    assert [name for name, _ in case.transport.rpc_calls] == ['support_admission_status_lane']


def test_pause_between_status_and_acquire_wins(case):
    def pause_at_acquire(name, body):
        if name == 'support_admission_acquire_lane':
            case.engine.sql("update support_admission_control set paused=true,generation=1 where lane='support-resolution-send'")
    case.transport.before_call = pause_at_acquire
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not invocations(case)
    assert case.bus.message(case.row['id'])['attachments'][outbox.SUPPORT_SEND_ADMISSION_KEY]


def test_disallowed_build_cannot_send(case, monkeypatch):
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'different-build')
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not invocations(case)


@pytest.mark.parametrize('stage', ['support_admission_acquire_lane', 'support_admission_finish_lane'])
def test_lost_ack_never_reacquires_or_resends(case, stage):
    case.transport.lose_once.add(stage)
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert len(case.sent) == (stage == 'support_admission_finish_lane')
    assert invocations(case)[0]['outcome'] == ('unknown' if stage.endswith('acquire_lane') else 'completed')
    assert outbox._hold_support_send(case.bus, case.row, 'ACK uncertainty', lambda *_: None)
    held = case.bus.message(case.row['id'])
    assert held['delivery_status'] == 'held'
    # A manual status edit/release is insufficient to permit another effect.
    case.bus.mark_message(held['id'], 'ready')
    summary = {'skipped': 0}
    outbox._dispatch_one(case.bus, case.post, case.bus.message(held['id']),
                         identity=case.identity, log=lambda *_: None, summary=summary)
    assert summary['skipped'] == 1
    assert sum(name == 'support_admission_acquire_lane' for name, _ in case.transport.rpc_calls) == 1


def test_lost_message_cas_ack_never_acquires_or_sends(case):
    case.transport.lose_patch_once = True
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not invocations(case)
    assert case.bus.message(case.row['id'])['attachments'][outbox.SUPPORT_SEND_ADMISSION_KEY]


@pytest.mark.parametrize('patch', [{'lane': 'ranger-lane'}, {'generation': True},
                                  {'invocation_id': str(uuid.uuid4())}, {'admitted': 'true'}])
def test_malformed_acquire_receipt_cannot_send(case, patch):
    case.transport.rewrite_receipt = lambda result: {**result, **patch}
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent
    assert invocations(case)[0]['outcome'] == 'unknown'


@pytest.mark.parametrize('field,value', [('source', 'portal_form'), ('request_version', 2),
                                       ('slack_channel_id', 'C_OTHER'), ('client_id', 'gym-other'),
                                       ('bot_identity', 'ranger')])
def test_request_route_tenant_sender_drift_after_acquire_cannot_send(case, field, value):
    def change(name, body):
        if name == 'support_admission_acquire_lane':
            case.bus.set_ticket(case.ticket['id'], **{field: value})
    case.transport.before_call = change
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and invocations(case)[0]['outcome'] == 'unknown'


@pytest.mark.parametrize('window', ['auth', 'status', 'acquire'])
@pytest.mark.parametrize('authority', ['staff_correction', 'verification_before'])
def test_resolution_rechecks_real_replay_authority_after_admission_windows(
        pg_bus, paused, monkeypatch, window, authority):
    from agent.slack_convo import replay
    deps, row = ready_answer(pg_bus, paused, monkeypatch)
    sent = []
    post = admission_post(sent)
    original_rpc = outbox._support_send_rpc
    original_auth = post.verify_sender
    changed = False
    auth_calls = 0

    def revoke():
        nonlocal changed
        if changed:
            return
        changed = True
        before = pg_bus.ticket(row['ticket_id'])
        if authority == 'staff_correction':
            pg_bus.record_inbound(ticket_id=row['ticket_id'],
                slack_event_id='admission-race-' + str(uuid.uuid4()), slack_ts='2.002',
                author_type='staff', author_id='U_STAFF',
                body='Please check November instead')
            # A staff correction invalidates replay history without a new
            # request cycle or any change to the bound ticket fields.
            assert pg_bus.ticket(row['ticket_id']) == before
        else:
            pg_bus.set_ticket(row['ticket_id'], verification_before={'changed': True})
            assert pg_bus.ticket(row['ticket_id'])['request_version'] == before['request_version']
        assert not replay.dispatch_allowed(pg_bus, row, 'echo')

    def rpc(bus, name, body):
        result = original_rpc(bus, name, body)
        if (window == 'status' and name == 'support_admission_status_lane'
                or window == 'acquire' and name == 'support_admission_acquire_lane'):
            revoke()
        return result

    def auth():
        nonlocal auth_calls
        auth_calls += 1
        proof = original_auth()
        if window == 'auth' and auth_calls == 2:
            revoke()
        return proof

    monkeypatch.setattr(outbox, '_support_send_rpc', rpc)
    post.verify_sender = auth
    summary = outbox.run_once(pg_bus, post, identity=deps.identity,
                              readback=post.readback, log=lambda *_: None)
    assert changed and summary['resolved'] == 0
    assert not any(item['text'] == row['body'] for item in sent)
    held = pg_bus.message(row['id'])
    assert held['delivery_status'] == 'held' and not held.get('slack_ts')
    lease = held['attachments'][outbox.SUPPORT_SEND_ADMISSION_KEY]
    invocation = pg_bus.engine.rows('support_admission_invocations',
        f"invocation_id={q(lease['invocation_id'])}")[0]
    assert invocation['outcome'] == 'unknown' and invocation['unresolved'] is True
    outbox.run_once(pg_bus, post, identity=deps.identity,
                   readback=post.readback, log=lambda *_: None)
    assert not any(item['text'] == row['body'] for item in sent)


def test_missing_durable_request_version_never_sends(case):
    case.engine.sql(f"update support_messages set delivery_request_version=null where id={q(case.row['id'])}")
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not invocations(case)
    assert outbox._hold_support_send(case.bus, case.row, 'missing request version', lambda *_: None)


def test_slack_timeout_unknown_stale_recovery_cannot_resend(case):
    def timeout(*args, **kwargs):
        case.post(*args, **kwargs)
        raise TimeoutError('Slack accepted; ACK lost')
    timeout.verify_sender = case.post.verify_sender
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, post=timeout)
    assert len(case.sent) == 1 and invocations(case)[0]['outcome'] == 'unknown'
    case.bus.mark_message(case.row['id'], 'posting', meta_update={
        'claimed_at': (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()})
    assert outbox._recover_stale_claims(case.bus, case.identity, lambda *_: None) == 1
    assert case.bus.message(case.row['id'])['delivery_status'] == 'held'
    assert len(case.sent) == 1


def test_readback_wrong_sender_stays_unknown(case):
    def wrong_sender(channel, **kwargs):
        return {'ok': True, 'channel': channel, 'messages': [{**case.sent[0], 'user': 'U_RANGER'}]}
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, readback=wrong_sender)
    assert len(case.sent) == 1 and invocations(case)[0]['outcome'] == 'unknown'
    assert case.bus.message(case.row['id'])['slack_ts'] == case.sent[0]['ts']


@pytest.mark.parametrize('kind,att,body', [
    (adapter.KIND_ACK, {'recipient_kind': 'client'}, 'Checking that now.'),
    (adapter.KIND_STATUS, {'recipient_kind': 'client'}, 'Your website is being reviewed.'),
    (adapter.KIND_ANSWER, {'recipient_kind': 'staff'}, 'Verified internal result.'),
    (adapter.KIND_ANSWER, {'recipient_kind': 'client'}, 'The team will follow up with you.'),
])
def test_unrelated_ack_website_status_staff_and_followup_do_not_acquire(case, kind, att, body):
    att = {**case.att, **att, 'kind': kind}
    case.row = case.bus._patch('support_messages', {'id': 'eq.'+case.row['id']},
                               {'body': body, 'attachments': att})
    send(case, kind=kind, att=att, body=body)
    assert len(case.sent) == 1 and not case.transport.rpc_calls


def test_resolve_notice_is_fenced_even_when_status_kind(case):
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, kind=adapter.KIND_STATUS, att={**case.att, 'resolve_notice': True})
    assert not case.sent


def ready_case(case):
    case.bus._insert('support_messages', {
        'ticket_id': case.ticket['id'], 'direction': 'inbound', 'author_type': 'client',
        'body': 'Please verify this scheduling issue.', 'attachments': {},
    })
    case.bus.set_ticket(case.ticket['id'], request_version=1)
    case.bus.mark_message(case.row['id'], 'ready')


def test_real_outbox_dispatches_resolution_through_0624(case):
    ready_case(case)
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert summary['posted'] == summary['resolved'] == 1
    assert case.bus.message(case.row['id'])['delivery_status'] == 'posted'
    assert len(case.sent) == 1 and invocations(case)[0]['outcome'] == 'completed'


def test_real_outbox_paused_send_is_held_and_ticket_open(case):
    ready_case(case)
    case.engine.sql("update support_admission_control set paused=true where lane='support-resolution-send'")
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert summary['held'] == 1 and summary['posted'] == summary['resolved'] == 0
    assert case.bus.message(case.row['id'])['delivery_status'] == 'held'
    assert case.bus.ticket(case.ticket['id'])['status'] == 'verification'
    assert not case.sent


def test_real_outbox_lost_acquisition_ack_never_sends_on_next_poll(case):
    ready_case(case)
    case.transport.lose_once.add('support_admission_acquire_lane')
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert summary['held'] == 1
    assert invocations(case)[0]['outcome'] == 'unknown'
    case.bus.mark_message(case.row['id'], 'ready')
    again = outbox.run_once(case.bus, case.post, identity=case.identity,
                            readback=case.readback, log=lambda *_: None)
    assert again['skipped'] >= 1
    assert not case.sent


def test_real_outbox_ordinary_website_status_does_not_acquire(case):
    ready_case(case)
    case.bus.set_ticket(case.ticket['id'], product='websites', source='website_tab',
                        bot_identity='wrangler')
    case.identity = SimpleNamespace(name='wrangler', bot_user_id=lambda: 'U_WRANGLER')
    case.post.verify_sender = lambda: {'ok': True, 'user_id': 'U_WRANGLER'}
    case.bus.mark_message(case.row['id'], 'ready', meta_update={
        'identity': 'wrangler', 'kind': adapter.KIND_STATUS})
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert summary['posted'] == 1 and len(case.sent) == 1
    assert not case.transport.rpc_calls


@pytest.mark.parametrize('boundary', [None, 'post_return', 'readback'])
def test_fixer_outbox_intent_timestamp_and_readback_survive_admission_metadata(case, monkeypatch, boundary):
    ready_case(case)
    case.bus.set_ticket(case.ticket['id'], classification='answerable_question')
    case.bus.mark_message(case.row['id'], 'ready', meta_update={
        'fixer': True, 'request_key': 'synthetic-request-hash', 'held_why': 'prior reviewed hold'})
    # This test owns admission and delivery integration. Existing focused
    # requester-hash tests own independent truth of the request-key predicate.
    monkeypatch.setattr(outbox, '_fresh_fixer_request',
                        lambda bus, ticket, att, **kwargs: bus.ticket(ticket['id']))
    base_post, base_readback = case.post, case.readback
    def post(*args, **kwargs):
        ts = base_post(*args, **kwargs)
        if boundary == 'post_return':
            case.bus.hold_uncertain_fixer_delivery(case.row['id'], 'concurrent stale sweep')
        return ts
    post.verify_sender = base_post.verify_sender
    def readback(*args, **kwargs):
        if boundary == 'readback':
            case.bus.hold_uncertain_fixer_delivery(case.row['id'], 'concurrent stale sweep')
        return base_readback(*args, **kwargs)
    summary = outbox.run_once(case.bus, post, identity=case.identity,
                              readback=readback, member_check=lambda *_: True,
                              log=lambda *_: None)
    assert summary['posted'] == 1 and len(case.sent) == 1
    row = case.bus.message(case.row['id'])
    assert row['delivery_status'] == 'posted'
    assert row['attachments']['fixer_slack_delivery_intent']['sender'] == 'U_ECHO'
    assert row['attachments']['delivery_readback_verified'] is True
    assert row['attachments'][outbox.SUPPORT_SEND_ADMISSION_KEY]['binding']['body_sha256'] == (
        hashlib.sha256(case.sent[0]['text'].encode()).hexdigest())
    assert invocations(case)[0]['outcome'] == 'completed'
    assert outbox._support_send_completed(row)
    outbox.run_once(case.bus, post, identity=case.identity, readback=readback,
                     member_check=lambda *_: True, log=lambda *_: None)
    assert len(case.sent) == 1


def test_status_rpc_unavailable_never_acquires_or_sends(case):
    case.transport.lose_once.add('support_admission_status_lane')
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and not invocations(case)


def test_missing_sender_or_readback_never_acquires(case):
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, identity=SimpleNamespace(name='echo', bot_user_id=lambda: ''))
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, readback=None)
    assert not case.sent and not case.transport.rpc_calls


def test_operator_release_revoked_during_admission_never_sends(case):
    def revoke(name, body):
        if name == 'support_admission_acquire_lane':
            case.bus.mark_message(case.row['id'], 'posting', meta_update={'released_by': None})
    case.transport.before_call = revoke
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and invocations(case)[0]['outcome'] == 'unknown'


def test_blake_membership_revoked_during_admission_never_sends(case):
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, require_member=True, member_check=lambda *_: False)
    assert not case.sent and invocations(case)[0]['outcome'] == 'unknown'


@pytest.mark.parametrize('flag', ['SLACK_CONVO_ECHO_CLIENT_REPLY', 'SLACK_CONVO_ECHO_AUTO_ANSWER'])
def test_client_safety_flag_revoked_during_admission_never_sends(case, monkeypatch, flag):
    for env in ('SLACK_CONVO_ECHO_CLIENT_REPLY', 'SLACK_CONVO_ECHO_AUTO_ANSWER'):
        monkeypatch.setenv(env, 'true')
    case.att['released_by'] = None
    case.bus.mark_message(case.row['id'], 'posting', meta_update={'released_by': None})
    def revoke(name, body):
        if name == 'support_admission_acquire_lane':
            monkeypatch.setenv(flag, 'false')
    case.transport.before_call = revoke
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and invocations(case)[0]['outcome'] == 'unknown'


def test_client_dm_lane_revoked_during_admission_never_sends(case, monkeypatch):
    from agent.client_dm_support import arming
    for env in ('SLACK_CONVO_ECHO_CLIENT_REPLY', 'SLACK_CONVO_ECHO_AUTO_ANSWER'):
        monkeypatch.setenv(env, 'true')
    case.att.update({'released_by': None, outbox._client_dm_lane_meta_key(): True})
    case.bus.mark_message(case.row['id'], 'posting', meta_update=case.att)
    monkeypatch.setattr(arming, 'preflight', lambda *_: SimpleNamespace(mode='disabled'))
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and invocations(case)[0]['outcome'] == 'unknown'


class CapturedSlackTransport:
    """Slack API fake routes by the actual bearer captured by SlackPoster."""
    def __init__(self, *, actual_sender='U_RANGER', auth_result=None, auth_status=200):
        self.actual_sender = actual_sender
        self.auth_result = auth_result
        self.auth_status = auth_status
        self.auth_calls = 0
        self.messages = []
        self.auth_headers = []
        self.send_headers = []

    def get(self, url, *, headers, params, timeout):
        if url.endswith('/auth.test'):
            self.auth_calls += 1
            self.auth_headers.append(headers['Authorization'])
            result = self.auth_result if self.auth_result is not None else {
                'ok': True, 'user_id': self.actual_sender}
            return SimpleNamespace(status_code=self.auth_status, json=lambda: result)
        assert 'conversations.' in url
        return SimpleNamespace(status_code=200, json=lambda: {
            'ok': True, 'messages': copy.deepcopy(self.messages)})

    def post(self, url, *, headers, data, timeout):
        assert url.endswith('/chat.postMessage')
        self.send_headers.append(headers['Authorization'])
        payload = json.loads(data)
        ts = f'{datetime.now(timezone.utc).timestamp():.6f}'
        self.messages.append({**payload, 'ts': ts, 'user': self.actual_sender})
        return SimpleNamespace(status_code=200, json=lambda: {'ok': True, 'ts': ts})


def actual_wired_post(case, monkeypatch, transport, *, token='synthetic-ranger-token'):
    monkeypatch.setenv('AGENT_SLACK_BOT_USER_ID', 'U_ECHO')
    monkeypatch.setenv('AGENT_SLACK_BOT_TOKEN', token)
    monkeypatch.setattr(slack_surface, '_requests', lambda: transport)
    wiring = listener_wiring.ConvoWiring.__new__(listener_wiring.ConvoWiring)
    wiring.identity = identities.get('echo')
    post = wiring._default_post()
    case.identity = wiring.identity
    return post


@pytest.mark.parametrize('kind', [adapter.KIND_ANSWER, adapter.KIND_STATUS,
                                  adapter.KIND_ACK, adapter.KIND_TEMPLATE])
def test_actual_wiring_wrong_captured_token_never_posts_client_reply(case, monkeypatch, kind):
    ready_case(case)
    case.bus.mark_message(case.row['id'], 'ready', meta_update={'kind': kind})
    transport = CapturedSlackTransport()
    post = actual_wired_post(case, monkeypatch, transport)
    # The environment now names a correct token; the callback still owns the
    # original wrong token. Authenticating a new client would miss this defect.
    monkeypatch.setenv('AGENT_SLACK_BOT_TOKEN', 'synthetic-echo-token')
    summary = outbox.run_once(case.bus, post, identity=case.identity,
                              log=lambda *_: None)
    assert summary['held'] == 1 and summary['posted'] == summary['resolved'] == 0
    assert case.bus.message(case.row['id'])['delivery_status'] == 'held'
    assert case.bus.ticket(case.ticket['id'])['status'] == 'verification'
    assert transport.auth_calls == 1 and not transport.messages
    assert transport.auth_headers == ['Bearer synthetic-ranger-token']
    assert not invocations(case)


def test_actual_wiring_matching_sender_auth_and_send_share_captured_token(case, monkeypatch):
    ready_case(case)
    transport = CapturedSlackTransport(actual_sender='U_ECHO')
    post = actual_wired_post(case, monkeypatch, transport, token='synthetic-echo-token')
    monkeypatch.setenv('AGENT_SLACK_BOT_TOKEN', 'synthetic-ranger-token')
    # A later transport factory change must not replace the captured client's
    # auth/send/readback transport either.
    replacement = CapturedSlackTransport(actual_sender='U_RANGER')
    monkeypatch.setattr(slack_surface, '_requests', lambda: replacement)
    summary = outbox.run_once(case.bus, post, identity=case.identity, log=lambda *_: None)
    assert summary['posted'] == summary['resolved'] == 1
    assert transport.auth_calls == 2 and len(transport.messages) == 1
    assert transport.auth_headers == ['Bearer synthetic-echo-token'] * 2
    assert transport.send_headers == ['Bearer synthetic-echo-token']
    assert not replacement.auth_calls and not replacement.messages
    assert invocations(case)[0]['outcome'] == 'completed'


@pytest.mark.parametrize('result,status', [
    ({'ok': False, 'user_id': 'U_ECHO'}, 200),
    ({'ok': 'true', 'user_id': 'U_ECHO'}, 200),
    ({'ok': True}, 200),
    ({'ok': True, 'user_id': 'U_ECHO'}, 503),
    ([], 200),
])
def test_auth_test_failure_or_malformed_receipt_holds_actual_wiring(case, monkeypatch, result, status):
    ready_case(case)
    transport = CapturedSlackTransport(auth_result=result, auth_status=status)
    post = actual_wired_post(case, monkeypatch, transport)
    summary = outbox.run_once(case.bus, post, identity=case.identity, log=lambda *_: None)
    assert summary['held'] == 1 and not transport.messages
    assert not invocations(case)


@pytest.mark.parametrize('kind', [adapter.KIND_ANSWER, adapter.KIND_STATUS])
def test_injected_post_without_sender_verifier_is_held(case, kind):
    ready_case(case)
    case.bus.mark_message(case.row['id'], 'ready', meta_update={'kind': kind})
    del case.post.verify_sender
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert summary['held'] == 1 and not case.sent and not invocations(case)


def test_captured_sender_changes_after_admission_never_posts(case):
    def change(name, args):
        if name == 'support_admission_acquire_lane':
            case.post.verify_sender = lambda: {'ok': True, 'user_id': 'U_RANGER'}
    case.transport.before_call = change
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert not case.sent and invocations(case)[0]['outcome'] == 'unknown'


def test_staff_stamp_cannot_bypass_auth_in_client_conversation(case):
    del case.post.verify_sender
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, kind=adapter.KIND_STATUS, att={'recipient_kind': 'staff'})
    assert not case.sent


def test_staff_ticket_and_recipient_preserve_internal_reply_behavior(case):
    del case.post.verify_sender
    send(case, kind=adapter.KIND_STATUS, att={'recipient_kind': 'staff'},
         ticket={**case.ticket, 'identity_kind': 'staff'})
    assert len(case.sent) == 1 and not case.transport.rpc_calls


@pytest.mark.parametrize('kind', [adapter.KIND_STATUS, adapter.KIND_ACK, adapter.KIND_TEMPLATE])
@pytest.mark.parametrize('field,value', [
    ('source', 'portal_form'), ('client_id', 'gym-two'), ('request_version', 2),
    ('slack_channel_id', 'C_OTHER'), ('slack_thread_ts', '9.001'),
    ('bot_identity', 'ranger'), ('identity_kind', 'staff'),
])
def test_ordinary_reply_auth_window_rechecks_exact_ticket_before_post(case, kind, field, value):
    ready_case(case)
    case.bus.mark_message(case.row['id'], 'ready', meta_update={'kind': kind})
    def auth():
        case.bus.set_ticket(case.ticket['id'], **{field: value})
        return {'ok': True, 'user_id': 'U_ECHO'}
    case.post.verify_sender = auth
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert not case.sent and summary['posted'] == 0
    assert case.bus.message(case.row['id'])['delivery_status'] == 'held'


@pytest.mark.parametrize('change', ['body', 'release', 'claim', 'safety', 'replay'])
def test_ordinary_reply_auth_window_rechecks_message_release_and_gates(case, monkeypatch, change):
    ready_case(case)
    case.bus.mark_message(case.row['id'], 'ready', meta_update={'kind': adapter.KIND_STATUS})
    if change == 'safety':
        monkeypatch.setenv('SLACK_CONVO_ECHO_CLIENT_REPLY', 'true')
        case.bus.mark_message(case.row['id'], 'ready', meta_update={'released_by': None})
    allowed = True
    if change == 'replay':
        from agent.slack_convo import replay
        monkeypatch.setattr(replay, 'dispatch_allowed', lambda *_: allowed)
    def auth():
        nonlocal allowed
        if change == 'body':
            case.bus._patch('support_messages', {'id': 'eq.'+case.row['id']}, {'body': 'Changed body'})
        elif change == 'release':
            case.bus.mark_message(case.row['id'], 'posting', meta_update={'released_by': None})
        elif change == 'claim':
            case.bus.mark_message(case.row['id'], 'held')
        elif change == 'safety':
            monkeypatch.setenv('SLACK_CONVO_ECHO_CLIENT_REPLY', 'false')
        else:
            allowed = False
        return {'ok': True, 'user_id': 'U_ECHO'}
    case.post.verify_sender = auth
    summary = outbox.run_once(case.bus, case.post, identity=case.identity,
                              readback=case.readback, log=lambda *_: None)
    assert not case.sent and summary['posted'] == 0
    assert case.bus.message(case.row['id'])['delivery_status'] == 'held'


@pytest.mark.parametrize('boundary', ['post_return', 'readback'])
def test_known_quarantine_preserves_timestamp_proof_and_completes_one_send(case, boundary):
    base_post, base_readback = case.post, case.readback
    def quarantine():
        case.bus.mark_message(case.row['id'], 'held', meta_update={
            'support_resolution_send_held': True, 'held_why': 'known stale sweep'})
    def post(*args, **kwargs):
        ts = base_post(*args, **kwargs)
        if boundary == 'post_return':
            quarantine()
        return ts
    post.verify_sender = base_post.verify_sender
    def readback(*args, **kwargs):
        if boundary == 'readback':
            quarantine()
        return base_readback(*args, **kwargs)
    ts = send(case, post=post, readback=readback)
    row = case.bus.message(case.row['id'])
    assert row['delivery_status'] == 'held' and row['slack_ts'] == ts
    assert row['attachments']['support_resolution_send_readback']['delivery_readback_verified']
    assert outbox._support_send_completed(row)
    assert invocations(case)[0]['outcome'] == 'completed' and len(case.sent) == 1
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case)
    assert len(case.sent) == 1


def test_exact_later_readback_cannot_close_unknown_admission(case):
    def wrong_body(channel, **kwargs):
        return {'ok': True, 'channel': channel, 'messages': [{**case.sent[0], 'text': 'wrong'}]}
    with pytest.raises(outbox.SupportResolutionAdmissionError):
        send(case, readback=wrong_body)
    row = case.bus.message(case.row['id'])
    assert row['slack_ts'] == case.sent[0]['ts'] and invocations(case)[0]['outcome'] == 'unknown'
    proof = {'delivery_readback_verified': True, 'delivery_readback_ts': row['slack_ts']}
    # A legacy delivery reconciler may prove visibility. That proof cannot
    # acknowledge or clear an unresolved 0624 invocation or close its ticket.
    case.bus.mark_message(row['id'], 'posted', meta_update=proof)
    summary = {'resolved': 0}
    outbox._resolve_on_answer(case.bus, case.ticket, case.row, adapter.KIND_ANSWER,
                              summary, case.att, case.body)
    assert summary['resolved'] == 0
    assert case.bus.ticket(case.ticket['id'])['status'] == 'verification'
    assert invocations(case)[0]['outcome'] == 'unknown' and len(case.sent) == 1
