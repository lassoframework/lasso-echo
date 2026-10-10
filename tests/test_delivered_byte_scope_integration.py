"""Offline scope integration; injected evidence never represents live setup."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from test_delivered_byte_send_guard import immutable_evidence
from test_delivered_byte_send_guard import immutable_evidence
from agent import delivered_byte_send_guard as exact
from agent import forward_media_send_context as scope
from agent import gbp_worker, zernio
from test_gbp_worker import _row, _conn

URL = 'https://owned.example/card.jpg'

class Store:
    def __init__(self, row, claim, target):
        self.calls = []
        self.context = dict(enabled=True, calendar_row_id=row['id'], publish_claim_token=claim,
            row_revision='revision', canonical_tenant='tenant', post_date='2026-10-10',
            reservation_day='2026-10-10', posting_timezone='UTC', row_snapshot=deepcopy(row),
            provider_target=target, shared_posting_identity=None,
            corpus_sha256='sha256:' + 'a' * 64, cutover_id=str(uuid.uuid4()),
            images=[dict(ordinal=0, role='image', url=URL)])

    def _reservation_rpc(self, name, args, timeout):
        self.calls.append(name)
        if name == 'exact_byte_send_context_20261010':
            return self.context
        if name == 'exact_byte_authorize_send_20261010':
            return dict(authorized=True, attempt_id=str(uuid.uuid4()))
        if name == 'exact_byte_record_outcome_20261010':
            return True
        raise AssertionError(name)

class Http:
    def __init__(self):
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {'_id': 'receipt'})

@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_RESERVATION', raising=False)
    monkeypatch.setenv('AGENT_S3_PUBLIC_BASE_URL', 'https://owned.example')


def setup_scope(target=None):
    row = _row(id=str(uuid.uuid4()), image_url=URL, format='feed',
               gbp_event=None, gbp_offer=None)
    claim = str(uuid.uuid4())
    target = target or {'provider': 'zernio', 'platform': 'instagram', 'account_id': 'account'}
    return row, claim, Store(row, claim, target)


def test_valid_scope_entry_does_not_consume_and_lower_http_does(monkeypatch):
    row, claim, store = setup_scope()
    http = Http()
    api = zernio.ZernioClient(api_key='offline', http=http)
    @scope.guarded_publisher('zernio')
    def publish(draft, account):
        scope.boundary('zernio')
        assert exact.active_permit().consumed is False
        return api.create_post('account', row['caption'], [URL], platform='instagram')
    with scope.authorized_send(store, row, claim, immutable_verifier=immutable_evidence,
                               read_bytes=lambda url: b'approved image'):
        monkeypatch.delenv(exact.FLAG, raising=False)
        assert publish(None, None) == {'_id': 'receipt'}
        assert exact.active_permit().consumed
    assert len(http.calls) == 1


def test_missing_storage_verifier_holds_without_mutation():
    row, claim, store = setup_scope()
    with pytest.raises(exact.ExactByteSendHold, match='immutable'):
        with scope.authorized_send(store, row, claim):
            pytest.fail('missing verifier admitted')
    assert store.calls == ['exact_byte_send_context_20261010']


def test_armed_direct_wrapper_requires_scope():
    calls = []
    @scope.guarded_publisher('zernio')
    def publish(draft, account):
        calls.append(True)
    with pytest.raises(scope.ProviderSendHold, match='authorization required'):
        publish(None, None)
    assert calls == []


def test_store_does_not_accept_unsigned_lock_or_row_boolean():
    row, claim, store = setup_scope()
    row['immutable'] = True
    store._immutable_media_verifier_config = dict(bucket='bucket', account_id='account',
        public_base_url='https://owned.example', s3=object(), lock_source=lambda: True,
        retention_seconds=300, proof_sink=lambda proof: None)
    assert scope.immutable_verifier_for_store(store) is None


@pytest.mark.parametrize('gallery', [False, True])
def test_gbp_injected_trusted_scope_reaches_real_lower_boundary(monkeypatch, gallery):
    conn = _conn()
    target = {'provider': 'zernio', 'platform': 'googlebusiness',
              'account_id': conn['zernio_account_id']}
    if not gallery:
        target['location_id'] = conn['gbp_location_id']
    row, claim, store = setup_scope(target)
    monkeypatch.setattr(scope, 'immutable_verifier_for_store', lambda _: immutable_evidence)
    monkeypatch.setattr(exact, 'read_public_object', lambda url: b'approved image')
    monkeypatch.setattr(gbp_worker, '_media_reuse_hold', lambda *a, **k: None)
    monkeypatch.setattr('agent.publish_billing_gate.publishing_blocked', lambda _: False)
    http = Http()
    api = zernio.ZernioClient(api_key='offline', http=http)
    method = gbp_worker.publish_photo_drop if gallery else gbp_worker.publish_gbp_row
    result = method(row, conn, client=api, draft=False, authority_store=store,
                    idempotency_key=claim)
    assert result['ok'] is True, result
    assert result['late_post_id'] == 'receipt'
    assert len(http.calls) == 1
    assert store.calls[-1] == 'exact_byte_record_outcome_20261010'


def test_consumed_continuation_bound_to_wrapper_even_with_broad_off(monkeypatch):
    row, claim, store = setup_scope({'provider': 'meta', 'platform': 'instagram',
                                      'account_id': 'account'})
    @scope.guarded_publisher('meta')
    def publish(draft, account):
        exact.require(store.context['provider_target'], [URL])
        scope.boundary('meta')  # follow-up validation in same owned call
        with pytest.raises(scope.ProviderSendHold):
            scope.boundary('socialapi')
        with pytest.raises(exact.ExactByteSendHold):
            exact.require(store.context['provider_target'], [URL])
        return 'accepted'
    with scope.authorized_send(store, row, claim, immutable_verifier=immutable_evidence,
                               read_bytes=lambda url: b'approved image'):
        assert publish(None, None) == 'accepted'
        with pytest.raises(scope.ProviderSendHold):
            scope.boundary('meta')  # inherited permit cannot invent continuation
        with pytest.raises(scope.ProviderSendHold):
            publish(None, None)


def test_gbp_inherited_scope_flag_flip_keeps_payload_binding(monkeypatch):
    conn = _conn()
    target = {'provider': 'zernio', 'platform': 'googlebusiness',
              'account_id': 'wrong-account', 'location_id': conn['gbp_location_id']}
    row, claim, store = setup_scope(target)
    monkeypatch.setattr(gbp_worker, '_media_reuse_hold', lambda *a, **k: None)
    monkeypatch.setattr('agent.publish_billing_gate.publishing_blocked', lambda _: False)
    http = Http()
    api = zernio.ZernioClient(api_key='offline', http=http)
    with exact.authorized_send(store, row, claim, immutable_verifier=immutable_evidence,
                               read_bytes=lambda url: b'approved image'):
        monkeypatch.delenv(exact.FLAG, raising=False)
        result = gbp_worker.publish_gbp_row(row, conn, client=api, draft=False,
                                           authority_store=store, idempotency_key=claim)
        assert result['status'] == 'publishing'
        assert exact.active_permit().consumed is False
    assert http.calls == []


def test_consumed_call_identity_cannot_be_inherited_by_async_task():
    import asyncio
    row, claim, store = setup_scope({'provider': 'meta', 'platform': 'instagram',
                                      'account_id': 'account'})
    @scope.guarded_publisher('meta')
    def publish(draft, account):
        exact.require(store.context['provider_target'], [URL])
        async def inherited():
            with pytest.raises(scope.ProviderSendHold):
                scope.boundary('meta')
        asyncio.run(inherited())
        scope.boundary('meta')
    with scope.authorized_send(store, row, claim, immutable_verifier=immutable_evidence,
                               read_bytes=lambda url: b'approved image'):
        publish(None, None)


@pytest.mark.parametrize('inherited', [False, True])
@pytest.mark.parametrize('provider_result', [
    {'ok': True, 'status': 'published', 'mode': 'live', 'late_post_id': 'earlier-post', 'dedup': True},
    {'ok': True, 'status': 'published', 'mode': 'live', 'late_post_id': ''},
    {'ok': True, 'status': 'published', 'mode': 'live', 'late_post_id': '   '},
])
def test_gbp_consumed_attempt_without_acceptance_keeps_claim(monkeypatch, inherited, provider_result):
    row, claim, store = setup_scope()
    monkeypatch.setattr(scope, 'immutable_verifier_for_store', lambda _: immutable_evidence)
    monkeypatch.setattr(exact, 'read_public_object', lambda url: b'approved image')
    outcomes = []
    original_rpc = store._reservation_rpc
    def rpc(name, args, timeout):
        if name == 'exact_byte_record_outcome_20261010':
            outcomes.append(args['p_outcome'])
        return original_rpc(name, args, timeout)
    store._reservation_rpc = rpc
    def send():
        exact.require(store.context['provider_target'], [URL])
        return deepcopy(provider_result)
    if inherited:
        with exact.authorized_send(store, row, claim, immutable_verifier=immutable_evidence,
                                   read_bytes=lambda url: b'approved image'):
            monkeypatch.delenv(exact.FLAG, raising=False)
            result = gbp_worker._run_exact_gbp_send(store, row, claim, send)
    else:
        result = gbp_worker._run_exact_gbp_send(store, row, claim, send)
    assert result['ok'] is False
    assert result['status'] == 'publishing'
    assert result['held'] == 'ambiguous_send'
    assert result['late_post_id'] == ''
    assert outcomes == ['uncertain']
