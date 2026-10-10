"""Offline independent fence integration; no provider mutations or live SQL."""
from contextlib import contextmanager
from types import SimpleNamespace
import uuid

import pytest

from agent import calendar_autopublish as cap
from agent import delivered_byte_send_guard as exact
from agent import forward_media_send_context as scope
from test_calendar_autopublish import (
    _FakeStore, _FakePublisher, _FakeNotifier, _row, RUN_DATE, LATE_NOW,
)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv('AGENT_CALENDAR_AUTOPUBLISH', 'true')
    monkeypatch.setenv('AGENT_PUBLISH_ENABLED', 'true')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'false')


def run_calendar(store, publisher):
    return cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                           notifier=_FakeNotifier(), now=LATE_NOW)


def test_off_calendar_keeps_existing_publish_behavior(armed, monkeypatch):
    monkeypatch.delenv(exact.FLAG, raising=False)
    store = _FakeStore([_row('off')])
    publisher = _FakePublisher()
    result = run_calendar(store, publisher)
    assert result['published'] == ['off']
    assert len(publisher.calls) == 1


def test_on_scope_without_trusted_verifier_never_calls_publisher(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setenv('AGENT_S3_PUBLIC_BASE_URL', 'https://owned.example')
    row = {'id': str(uuid.uuid4())}
    token = str(uuid.uuid4())
    context = dict(enabled=True, calendar_row_id=row['id'], publish_claim_token=token,
        row_revision='revision', canonical_tenant='tenant', post_date=RUN_DATE,
        reservation_day=RUN_DATE, posting_timezone='UTC', row_snapshot=dict(row),
        provider_target={'provider': 'meta', 'platform': 'instagram', 'account_id': 'a'},
        shared_posting_identity=None, corpus_sha256='sha256:' + 'a' * 64,
        cutover_id=str(uuid.uuid4()),
        images=[dict(ordinal=0, role='image', url='https://owned.example/card.png')])
    calls = []
    class Store:
        def _reservation_rpc(self, name, args, timeout):
            calls.append(name)
            return context
    publisher = _FakePublisher()
    with pytest.raises(exact.ExactByteSendHold, match='immutable'):
        with scope.authorized_send(Store(), row, token):
            publisher(None, None)
    assert publisher.calls == []
    assert calls == ['exact_byte_send_context_20261010']


@pytest.mark.parametrize('ambiguous', [False, True])
def test_calendar_independent_fence_and_ambiguous_claim_retention(armed, monkeypatch, ambiguous):
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr(scope.guard, 'enabled', lambda: False)
    calls = []
    @contextmanager
    def hold(store, row, token, **kwargs):
        calls.append((row['id'], token))
        error = exact.ExactByteSendHold('authority unavailable' if ambiguous else 'immutable unavailable')
        error.definitive_no_post = not ambiguous
        raise error
        yield
    monkeypatch.setattr(exact, 'authorized_send', hold)
    store = _FakeStore([_row('held')])
    publisher = _FakePublisher()
    result = run_calendar(store, publisher)
    assert calls == [('held', None)]
    assert publisher.calls == []
    assert result['forward_media_holds'] == {'held': 'exact_byte_verification'}
    assert store.rows['held']['status'] == ('publishing' if ambiguous else 'pending')
    assert store.failed_calls == ([] if ambiguous else ['held'])
    assert result['recovery_required'] == (['held'] if ambiguous else [])


@pytest.mark.parametrize('provider', ['meta', 'zernio', 'socialapi'])
def test_independent_on_cannot_bypass_wrapper_with_broad_off(monkeypatch, provider):
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr(scope.guard, 'enabled', lambda: False)
    calls = []
    @scope.guarded_publisher(provider)
    def publish(draft, account):
        calls.append(provider)
    with pytest.raises(scope.ProviderSendHold, match='exact provider payload') as caught:
        publish(SimpleNamespace(), SimpleNamespace())
    assert caught.value.definitive_no_post is True
    assert calls == []


def test_direct_boundary_on_holds_with_broad_off(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr(scope.guard, 'enabled', lambda: False)
    with pytest.raises(scope.ProviderSendHold, match='exact provider payload'):
        scope.boundary('meta', attempt=True)


def test_committed_scope_stays_held_after_flag_changes_off(monkeypatch):
    monkeypatch.delenv(exact.FLAG, raising=False)
    monkeypatch.setattr(scope.guard, 'enabled', lambda: False)
    monkeypatch.setattr(exact, 'active_permit', lambda: object())
    calls = []
    @scope.guarded_publisher('meta')
    def publish(draft, account):
        calls.append('publish')
    with pytest.raises(scope.ProviderSendHold) as caught:
        publish(None, None)
    assert caught.value.definitive_no_post is False
    assert calls == []


def test_off_scope_and_wrapper_do_not_touch_store(monkeypatch):
    monkeypatch.delenv(exact.FLAG, raising=False)
    monkeypatch.setattr(scope.guard, 'enabled', lambda: False)
    calls = []
    @scope.guarded_publisher('meta')
    def publish(draft, account):
        calls.append('publish')
        return 'original result'
    with scope.authorized_send(object(), None, None):
        assert publish(None, None) == 'original result'
    assert calls == ['publish']
