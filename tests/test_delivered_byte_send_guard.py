"""Offline exact-byte authority and provider-boundary acceptance."""
from copy import deepcopy
import hashlib
import time
import uuid

import pytest

from agent import delivered_byte_send_guard as guard
from agent.r2_immutable_media import ImmutableMediaEvidence, ImmutableMediaProof


def immutable_evidence(url, data, *, retention_until=None, horizon=600):
    """Finite trusted-verifier fixture; no production bypass or forever proof."""
    now = int(time.time())
    proof = ImmutableMediaProof(url, 'account', 'bucket', 'fixture/object',
        hashlib.sha256(data).hexdigest(), len(data), now,
        now + 3600 if retention_until is None else retention_until, 'lock', 'a' * 64)
    return ImmutableMediaEvidence(proof, horizon)


class Store:
    def __init__(self, context):
        self.context = context
        self.calls = []
        self.authorized = True
        self.fail = None
        self.attempt = str(uuid.uuid4())

    def _reservation_rpc(self, name, args, timeout):
        self.calls.append((name, deepcopy(args)))
        if self.fail == name:
            raise RuntimeError('transport outcome unknown')
        if 'context' in name:
            return deepcopy(self.context)
        if 'authorize' in name:
            return {'authorized': self.authorized, 'attempt_id': self.attempt}
        return True


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setenv(guard.FLAG, 'true')
    monkeypatch.setenv('AGENT_S3_PUBLIC_BASE_URL', 'https://owned.example')
    row, claim = str(uuid.uuid4()), str(uuid.uuid4())
    context = dict(enabled=True, calendar_row_id=row, publish_claim_token=claim,
                   row_revision='revision', canonical_tenant='tenant', post_date='2026-10-11',
                   reservation_day='2026-10-11', posting_timezone='UTC',
                   provider_target={'provider': 'meta', 'platform': 'instagram', 'account_id': 'a'},
                   format='feed', shared_posting_identity=None, corpus_sha256='sha256:' + 'a' * 64,
                   cutover_id=str(uuid.uuid4()),
                   row_snapshot={'id': row, 'caption': 'approved caption'},
                   images=[dict(ordinal=0, role='image', url='https://owned.example/card.png')])
    return Store(context), row, claim


def authorize(lane, **kwargs):
    store, row, claim = lane
    return guard.authorized_send(store, {'id': row, 'caption': 'approved caption'}, claim,
                                 immutable_verifier=kwargs.pop('immutable_verifier', immutable_evidence),
                                 read_bytes=kwargs.pop('read_bytes', lambda url: b'actual bytes'), **kwargs)


def consume(lane):
    return guard.require(lane[0].context['provider_target'], ['https://owned.example/card.png'])


def test_off_never_touches_authority_or_reads(lane, monkeypatch):
    monkeypatch.delenv(guard.FLAG)
    with guard.authorized_send(lane[0], None, None) as permit:
        assert permit is None
        assert guard.require(None, []) is None
    assert lane[0].calls == []


def test_exact_sha_and_committed_authorization_precede_boundary(lane):
    def read(url):
        assert [call[0] for call in lane[0].calls] == ['exact_byte_send_context_20261010']
        return b'actual bytes'
    with authorize(lane, read_bytes=read) as permit:
        assert len(lane[0].calls) == 2
        assert consume(lane) is permit
        with pytest.raises(guard.ExactByteSendHold, match='already consumed'):
            consume(lane)
    args = lane[0].calls[1][1]
    assert args['p_expected_context'] == lane[0].context
    assert args['p_images'] == [dict(ordinal=0, role='image', url='https://owned.example/card.png',
                                    sha256='sha256:' + hashlib.sha256(b'actual bytes').hexdigest(),
                                    byte_length=12)]
    assert permit.closed


@pytest.mark.parametrize('authorized', [False, 'true', 1, None])
def test_literal_authorization_required(lane, authorized):
    lane[0].authorized = authorized
    with pytest.raises(guard.ExactByteSendHold, match='denied'):
        with authorize(lane):
            pytest.fail('must not reach provider')


def test_unknown_commit_outcome_never_allows_provider_or_retry(lane):
    lane[0].fail = 'exact_byte_authorize_send_20261010'
    with pytest.raises(guard.ExactByteSendHold, match='authority unavailable'):
        with authorize(lane):
            pytest.fail('must not reach provider')
    assert len(lane[0].calls) == 2


def test_missing_immutable_evidence_holds_before_read_and_authorization(lane):
    with pytest.raises(guard.ExactByteSendHold, match='immutable'):
        with authorize(lane, immutable_verifier=None):
            pass
    assert len(lane[0].calls) == 1


@pytest.mark.parametrize('proof', [True, False, 1, 'true'])
def test_untrusted_immutable_proof_holds(lane, proof):
    with pytest.raises(guard.ExactByteSendHold, match='immutable'):
        with authorize(lane, immutable_verifier=lambda *args: proof):
            pass
    assert len(lane[0].calls) == 1


@pytest.mark.parametrize('data', [b'', None, 'bytes'])
def test_invalid_exact_bytes_hold(lane, data):
    with pytest.raises(guard.ExactByteSendHold, match='bounded'):
        with authorize(lane, read_bytes=lambda url: data):
            pass


def test_byte_limit_holds(lane, monkeypatch):
    monkeypatch.setattr(guard, 'MAX_BYTES', 3)
    with pytest.raises(guard.ExactByteSendHold, match='bounded'):
        with authorize(lane):
            pass


@pytest.mark.parametrize('changes', [dict(enabled=1), dict(row_revision=None),
                                    dict(corpus_sha256='md5:' + 'a' * 32),
                                    dict(publish_claim_token=str(uuid.uuid4())),
                                    dict(images=[dict(ordinal=0, role='video', url='https://owned.example/v')]),
                                    dict(images=[dict(ordinal=0, role='image', url='https://foreign.example/v')]),
                                    dict(images=[dict(ordinal=0, role='image', url='https://owned.example/v'),
                                                 dict(ordinal=1, role='image', url='https://owned.example/w')])])
def test_invalid_context_holds_before_any_object_read(lane, changes):
    lane[0].context.update(changes)
    with pytest.raises(guard.ExactByteSendHold):
        with authorize(lane, read_bytes=lambda url: pytest.fail('invalid context read')):
            pass
    assert len(lane[0].calls) == 1


def test_thumbnail_is_hashed_and_complete_payload_required(lane):
    lane[0].context['images'].append(dict(ordinal=1, role='thumbnail', url='https://owned.example/thumb.png'))
    with authorize(lane):
        with pytest.raises(guard.ExactByteSendHold, match='payload differs'):
            consume(lane)
        guard.require(lane[0].context['provider_target'], ['https://owned.example/card.png'],
                      'https://owned.example/thumb.png')
    assert len(lane[0].calls[1][1]['p_images']) == 2


def test_target_and_url_substitution_hold_without_consuming(lane):
    with authorize(lane):
        with pytest.raises(guard.ExactByteSendHold, match='payload differs'):
            guard.require({'provider': 'other'}, ['https://owned.example/card.png'])
        with pytest.raises(guard.ExactByteSendHold, match='payload differs'):
            guard.require(lane[0].context['provider_target'], ['https://owned.example/different.png'])
        consume(lane)


def test_unwrapped_provider_call_holds(lane):
    with pytest.raises(guard.ExactByteSendHold, match='committed'):
        consume(lane)


def test_scope_stays_fenced_if_flag_changes(lane, monkeypatch):
    with authorize(lane):
        monkeypatch.delenv(guard.FLAG)
        consume(lane)
        with pytest.raises(guard.ExactByteSendHold, match='already consumed'):
            consume(lane)


def test_truthful_outcome_records_attempt_and_claim_once(lane):
    with authorize(lane) as permit:
        consume(lane)
    assert guard.record_outcome(lane[0], permit, 'provider_accepted', 'provider:receipt') is True
    assert lane[0].calls[-1][1] == dict(p_attempt_id=permit.attempt_id, p_claim_token=lane[2],
                                      p_outcome='provider_accepted', p_evidence_ref='provider:receipt')
    with pytest.raises(guard.ExactByteSendHold, match='consumed'):
        guard.record_outcome(lane[0], permit, 'provider_accepted', 'provider:receipt')


def test_failed_outcome_record_does_not_reopen_send(lane):
    with authorize(lane) as permit:
        consume(lane)
        lane[0].fail = 'exact_byte_record_outcome_20261010'
        with pytest.raises(guard.ExactByteSendHold, match='authority unavailable'):
            guard.record_outcome(lane[0], permit, 'uncertain', 'provider:unknown')
        with pytest.raises(guard.ExactByteSendHold, match='already consumed'):
            consume(lane)


@pytest.mark.parametrize('outcome', ['success', 'failed', 'unknown', '', None])
def test_unsupported_outcome_rejected(lane, outcome):
    with authorize(lane) as permit:
        consume(lane)
    with pytest.raises(guard.ExactByteSendHold, match='evidence required') as error:
        guard.record_outcome(lane[0], permit, outcome, 'receipt')
    assert error.value.definitive_no_post is False
    assert len(lane[0].calls) == 2


@pytest.mark.parametrize('outcome', sorted(guard.OUTCOMES))
def test_sql_outcome_enum_accepted(lane, outcome):
    with authorize(lane) as permit:
        consume(lane)
    assert guard.record_outcome(lane[0], permit, outcome, 'receipt') is True


def test_stale_outbound_caption_holds_before_authority(lane):
    lane[0].context['row_snapshot']['caption'] = 'changed persisted caption'
    with pytest.raises(guard.ExactByteSendHold, match='outgoing row differs') as error:
        with authorize(lane):
            pass
    assert error.value.definitive_no_post is True
    assert len(lane[0].calls) == 1


def test_known_refusal_definitively_no_post(lane):
    lane[0].authorized = False
    with pytest.raises(guard.ExactByteSendHold) as error:
        with authorize(lane):
            pass
    assert error.value.definitive_no_post is True


def test_ambiguous_authorize_is_not_definitively_no_post(lane):
    lane[0].fail = 'exact_byte_authorize_send_20261010'
    with pytest.raises(guard.ExactByteSendHold) as error:
        with authorize(lane):
            pass
    assert error.value.definitive_no_post is False


def test_committed_authority_failure_is_not_definitively_no_post(lane):
    with pytest.raises(guard.ExactByteSendHold) as error:
        with authorize(lane):
            raise guard.ExactByteSendHold('caller held')
    assert error.value.definitive_no_post is False


def test_payload_failure_after_authority_is_not_definitively_no_post(lane):
    with pytest.raises(guard.ExactByteSendHold) as error:
        with authorize(lane):
            guard.require({}, [])
    assert error.value.definitive_no_post is False


def test_active_permit_observer_survives_flag_change_and_scope_cleans_up(lane, monkeypatch):
    assert guard.active_permit() is None
    with authorize(lane) as permit:
        assert guard.active_permit() is permit
        monkeypatch.delenv(guard.FLAG)
        assert guard.active_permit() is permit
        assert permit.consumed is False
        consume(lane)
    assert guard.active_permit() is None


def test_active_permit_clears_when_scope_raises(lane):
    with pytest.raises(RuntimeError):
        with authorize(lane) as permit:
            assert guard.active_permit() is permit
            raise RuntimeError('provider outcome unknown')
    assert guard.active_permit() is None


def test_bound_continuation_is_one_shot_and_survives_flag_flip(lane, monkeypatch):
    target = lane[0].context['provider_target']
    with authorize(lane) as permit:
        consume(lane)
        continuation = guard.bind_continuation(permit, target, 'returned-id', 'meta:media_publish')
        monkeypatch.delenv(guard.FLAG)
        assert guard.require_continuation(continuation, target, 'returned-id', 'meta:media_publish') is permit
        with pytest.raises(guard.ExactByteSendHold):
            guard.require_continuation(continuation, target, 'returned-id', 'meta:media_publish')
        with pytest.raises(guard.ExactByteSendHold):
            guard.bind_continuation(permit, target, 'other', 'meta:media_publish')
        with pytest.raises(guard.ExactByteSendHold):
            consume(lane)


@pytest.mark.parametrize('kind', ['target', 'id', 'operation', 'copy', 'closed'])
def test_continuation_substitution_or_stale_scope_holds(lane, kind):
    from copy import copy
    target = lane[0].context['provider_target']
    with authorize(lane) as permit:
        consume(lane)
        continuation = guard.bind_continuation(permit, target, 'returned-id', 'meta:media_publish')
        object_id, operation = 'returned-id', 'meta:media_publish'
        if kind == 'target': target = {**target, 'account_id': 'wrong'}
        if kind == 'id': object_id = 'wrong'
        if kind == 'operation': operation = 'other'
        if kind == 'copy': continuation = copy(continuation)
        if kind == 'closed': permit.closed = True
        with pytest.raises(guard.ExactByteSendHold) as error:
            guard.require_continuation(continuation, target, object_id, operation)
        assert error.value.definitive_no_post is False


def test_continuation_cannot_outlive_scope_or_cross_attempt(lane):
    target = lane[0].context['provider_target']
    with authorize(lane) as permit:
        consume(lane)
        continuation = guard.bind_continuation(permit, target, 'returned-id', 'meta:media_publish')
    with pytest.raises(guard.ExactByteSendHold):
        guard.require_continuation(continuation, target, 'returned-id', 'meta:media_publish')
    with authorize(lane):
        consume(lane)
        with pytest.raises(guard.ExactByteSendHold):
            guard.require_continuation(continuation, target, 'returned-id', 'meta:media_publish')


@pytest.mark.parametrize('object_id', [None, '', 123])
def test_missing_or_ambiguous_provider_object_id_holds(lane, object_id):
    target = lane[0].context['provider_target']
    with authorize(lane) as permit:
        consume(lane)
        with pytest.raises(guard.ExactByteSendHold):
            guard.bind_continuation(permit, target, object_id, 'meta:media_publish')
        assert permit.consumed and guard.active_continuation() is None


def test_initial_response_binding_requires_consumed_authority(lane):
    target = lane[0].context['provider_target']
    with authorize(lane) as permit:
        with pytest.raises(guard.ExactByteSendHold):
            guard.bind_continuation(permit, target, 'id', 'operation')


def test_continuation_off_without_scope_is_noop(monkeypatch):
    monkeypatch.delenv(guard.FLAG, raising=False)
    assert guard.bind_continuation(None, {}, None, '') is None
    assert guard.require_continuation(None, {}, None, '') is None


def test_inherited_thread_cannot_consume_first_authority(lane):
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    with authorize(lane) as permit:
        inherited = copy_context()
        def steal():
            with pytest.raises(guard.ExactByteSendHold):
                consume(lane)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(inherited.run, steal).result(timeout=5)
        assert not permit.consumed
        assert consume(lane) is permit


def test_approved_content_and_format_hold_before_mutation(lane):
    with authorize(lane) as permit:
        assert guard.check_content('approved caption', 'feed') is permit
        with pytest.raises(guard.ExactByteSendHold):
            guard.check_content('wrong caption', 'feed')
        with pytest.raises(guard.ExactByteSendHold):
            guard.check_content('approved caption', 'story')
        assert not permit.consumed


def test_continuation_requires_unchanged_approved_content(lane):
    target = lane[0].context['provider_target']
    with authorize(lane) as permit:
        consume(lane)
        continuation = guard.bind_continuation(permit, target, 'id', 'operation')
        permit.context['row_snapshot']['caption'] = 'changed'
        with pytest.raises(guard.ExactByteSendHold):
            guard.require_continuation(continuation, target, 'id', 'operation')
        assert not continuation.used


@pytest.fixture
def retention_clock(monkeypatch):
    ticks = {'wall': 2000000000.0, 'mono': 100.0}
    monkeypatch.setattr(guard.time, 'time', lambda: ticks['wall'])
    monkeypatch.setattr(guard.time, 'monotonic', lambda: ticks['mono'])
    return ticks


@pytest.mark.parametrize('boundary', ['require', 'require_bytes', 'check_payload'])
@pytest.mark.parametrize('elapsed', [401, 1001])
def test_delayed_boundary_requires_full_remaining_fetch_horizon(lane, retention_clock,
                                                               boundary, elapsed):
    ticks = retention_clock
    verifier = lambda url, data: immutable_evidence(url, data,
        retention_until=int(ticks['wall']) + 1000)
    with authorize(lane, immutable_verifier=verifier) as permit:
        assert permit.retention.retention_until == 2000001000
        assert permit.retention.provider_fetch_horizon_seconds == 600
        ticks['wall'] += elapsed
        ticks['mono'] += elapsed
        target, urls = lane[0].context['provider_target'], ['https://owned.example/card.png']
        with pytest.raises(guard.ExactByteSendHold, match='retention') as error:
            if boundary == 'require':
                guard.require(target, urls)
            else:
                getattr(guard, boundary)(target, urls, image_bytes=b'actual bytes')
        assert error.value.definitive_no_post is False
        assert not permit.consumed


def test_continuation_rechecks_retention_even_after_flag_flip(lane, retention_clock, monkeypatch):
    ticks = retention_clock
    target = lane[0].context['provider_target']
    verifier = lambda url, data: immutable_evidence(url, data, retention_until=2000001000)
    with authorize(lane, immutable_verifier=verifier) as permit:
        consume(lane)
        continuation = guard.bind_continuation(permit, target, 'id', 'operation')
        ticks['wall'] += 401
        ticks['mono'] += 401
        monkeypatch.delenv(guard.FLAG)
        with pytest.raises(guard.ExactByteSendHold, match='retention') as error:
            guard.require_continuation(continuation, target, 'id', 'operation')
        assert error.value.definitive_no_post is False
        assert not continuation.used


def test_clock_rollback_cannot_extend_verified_retention(lane, retention_clock):
    ticks = retention_clock
    verifier = lambda url, data: immutable_evidence(url, data, retention_until=2000001000)
    with authorize(lane, immutable_verifier=verifier) as permit:
        ticks['wall'] -= 100
        ticks['mono'] += 401
        with pytest.raises(guard.ExactByteSendHold, match='retention'):
            consume(lane)
        assert not permit.consumed


def test_shortest_image_retention_survives_slow_thumbnail_read(lane, retention_clock):
    ticks = retention_clock
    lane[0].context['images'].append(dict(ordinal=1, role='thumbnail',
                                        url='https://owned.example/thumb.png'))
    def verifier(url, data):
        if url.endswith('thumb.png'):
            ticks['wall'] += 401
            ticks['mono'] += 401
            return immutable_evidence(url, data, retention_until=2000010000)
        return immutable_evidence(url, data, retention_until=2000001000)
    with pytest.raises(guard.ExactByteSendHold, match='retention') as error:
        with authorize(lane, immutable_verifier=verifier):
            pytest.fail('provider scope entered after retention margin exhausted')
    assert error.value.definitive_no_post is True
    assert len(lane[0].calls) == 1


def test_slow_authorization_commit_holds_with_uncertain_occupancy(lane, retention_clock):
    ticks = retention_clock
    original = lane[0]._reservation_rpc
    def slow_rpc(name, args, timeout):
        result = original(name, args, timeout)
        if 'authorize' in name:
            ticks['wall'] += 401
            ticks['mono'] += 401
        return result
    lane[0]._reservation_rpc = slow_rpc
    verifier = lambda url, data: immutable_evidence(url, data, retention_until=2000001000)
    with pytest.raises(guard.ExactByteSendHold, match='retention') as error:
        with authorize(lane, immutable_verifier=verifier):
            pytest.fail('expired committed authorization entered provider scope')
    assert error.value.definitive_no_post is False
    assert len(lane[0].calls) == 2
    assert guard.active_permit() is None


@pytest.mark.parametrize('field,value', [('retention_until', None), ('retention_until', True),
    ('provider_fetch_horizon_seconds', 0), ('provider_fetch_horizon_seconds', True)])
def test_invalid_typed_retention_bounds_hold(lane, field, value):
    from dataclasses import replace
    def invalid(url, data):
        evidence = immutable_evidence(url, data)
        if field == 'retention_until':
            return replace(evidence, proof=replace(evidence.proof, retention_until=value))
        return replace(evidence, provider_fetch_horizon_seconds=value)
    with pytest.raises(guard.ExactByteSendHold, match='retention'):
        with authorize(lane, immutable_verifier=invalid):
            pass
    assert len(lane[0].calls) == 1


@pytest.mark.parametrize('field,value', [('public_url', 'https://owned.example/other.png'),
    ('sha256', 'b' * 64), ('size_bytes', 999), ('observed_at', 9999999999),
    ('attestation_sha256', 'unsigned')])
def test_typed_proof_still_requires_exact_identity(lane, field, value):
    from dataclasses import replace
    def invalid(url, data):
        evidence = immutable_evidence(url, data)
        return replace(evidence, proof=replace(evidence.proof, **{field: value}))
    with pytest.raises(guard.ExactByteSendHold, match='evidence differs'):
        with authorize(lane, immutable_verifier=invalid):
            pass
    assert len(lane[0].calls) == 1


def test_manual_permit_without_deadline_fails_closed(lane):
    permit = guard.SendPermit(lane[0].context, lane[0].context['images'],
                             str(uuid.uuid4()), lane[2])
    token = guard._active.set(permit)
    try:
        with pytest.raises(guard.ExactByteSendHold, match='retention'):
            consume(lane)
        assert not permit.consumed
    finally:
        guard._active.reset(token)


def test_upload_hashing_cannot_exhaust_retention_before_consumption(lane, retention_clock, monkeypatch):
    ticks = retention_clock
    verifier = lambda url, data: immutable_evidence(url, data, retention_until=2000001000)
    with authorize(lane, immutable_verifier=verifier) as permit:
        original = guard.hashlib.sha256
        def slow_hash(data):
            result = original(data)
            ticks['wall'] += 401
            ticks['mono'] += 401
            return result
        monkeypatch.setattr(guard.hashlib, 'sha256', slow_hash)
        with pytest.raises(guard.ExactByteSendHold, match='retention'):
            guard.require_bytes(lane[0].context['provider_target'],
                                ['https://owned.example/card.png'], image_bytes=b'actual bytes')
        assert not permit.consumed
