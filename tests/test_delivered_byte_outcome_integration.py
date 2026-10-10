"""Offline calendar receipts; real outcome guard, fake authority/provider/DB."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import uuid

import pytest

from agent import calendar_autopublish as cap
from agent import delivered_byte_send_guard as exact
from agent import forward_media_send_context as scope
from test_calendar_autopublish import _FakeStore, _FakePublisher, _FakeNotifier, _row, RUN_DATE, LATE_NOW


class OutcomeStore(_FakeStore):
    def __init__(self, rows, *, response=True, mark_error=False):
        super().__init__(rows)
        self.response = response
        self.mark_error = mark_error
        self.outcomes = []
        self.events = []

    def _reservation_rpc(self, name, args, timeout):
        assert name == 'exact_byte_record_outcome_20261010'
        self.events.append('outcome')
        self.outcomes.append(dict(args))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def mark_published(self, *args, **kwargs):
        self.events.append('mark_published')
        if self.mark_error:
            raise RuntimeError('calendar write unavailable')
        return super().mark_published(*args, **kwargs)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv('AGENT_CALENDAR_AUTOPUBLISH', 'true')
    monkeypatch.setenv('AGENT_PUBLISH_ENABLED', 'true')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'false')
    monkeypatch.setenv(exact.FLAG, 'true')
    permits = []

    @contextmanager
    def authority(store, row, token):
        # Tests inject committed authority; production must obtain it from SQL.
        permit = exact.SendPermit(
            context={'provider_target': {'provider': 'meta', 'platform': row['account'],
                                         'account_id': 'target'}},
            images=[], attempt_id=str(uuid.uuid4()), claim_token=str(uuid.uuid4()),
            retention=exact.ProviderFetchRetention(int(__import__('time').time()) + 3600, 600))
        permits.append(permit)
        active = exact._active.set(permit)
        try:
            yield
        finally:
            permit.closed = True
            exact._active.reset(active)
    monkeypatch.setattr(scope, 'authorized_send', authority)
    return permits


def run(store, result=None, error=None, *, consumed=True):
    fake = _FakePublisher(result=result)
    def publisher(draft, account):
        store.events.append('provider')
        permit = exact.active_permit()
        if permit is not None:
            permit.consumed = consumed
        if error:
            raise error
        return fake(draft, account)
    output = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                             notifier=_FakeNotifier(), now=LATE_NOW)
    return output, fake


def test_acceptance_recorded_before_calendar_completion(armed):
    store = OutcomeStore([_row('accepted')])
    output, fake = run(store)
    assert output['published'] == ['accepted']
    assert store.events == ['provider', 'outcome', 'mark_published']
    outcome = store.outcomes[0]
    assert outcome['p_outcome'] == 'provider_accepted'
    evidence = json.loads(outcome['p_evidence_ref'])
    assert evidence['provider_post_id'] == 'MEDIA_1'
    assert evidence['provider_target']['provider'] == 'meta'
    assert evidence['attempt_id'] == armed[0].attempt_id
    assert armed[0].outcome_recorded and armed[0].closed
    assert exact.active_permit() is None


@pytest.mark.parametrize('response', [False, None, RuntimeError('unknown commit')])
def test_receipt_write_failure_holds_without_rpc_or_provider_retry(armed, response):
    store = OutcomeStore([_row('held')], response=response)
    output, fake = run(store)
    assert output['published'] == []
    assert output['recovery_required'] == ['held']
    assert store.rows['held']['status'] == 'publishing'
    assert store.failed_calls == []
    assert store.events == ['provider', 'outcome']
    assert len(store.outcomes) == 1
    assert not armed[0].outcome_recorded


@pytest.mark.parametrize('receipt,dedup', [('', False), (None, False), (' ', False), (123, False), ('old-id', True)])
def test_missing_or_dedup_receipt_is_uncertain_and_held(armed, receipt, dedup):
    store = OutcomeStore([_row('missing')])
    result = SimpleNamespace(ok=True, mode='published', media_id=receipt, dedup=dedup)
    output, _ = run(store, result)
    assert output['recovery_required'] == ['missing']
    assert store.rows['missing']['status'] == 'publishing'
    assert not store.published_calls and not store.failed_calls
    assert store.outcomes[0]['p_outcome'] == 'uncertain'
    assert 'provider_post_id' not in json.loads(store.outcomes[0]['p_evidence_ref'])


@pytest.mark.parametrize('raise_error', [False, True])
def test_unknown_failure_records_uncertain_without_releasing_claim(armed, raise_error):
    store = OutcomeStore([_row('uncertain')])
    output, _ = run(store, SimpleNamespace(ok=False, mode='unknown'),
                    error=TimeoutError('possible acceptance') if raise_error else None)
    assert store.outcomes[0]['p_outcome'] == 'uncertain'
    assert store.rows['uncertain']['status'] == 'publishing'
    assert output['recovery_required'] == ['uncertain']


def test_explicit_no_send_preserves_existing_revert(armed):
    store = OutcomeStore([_row('rejected')])
    output, _ = run(store, SimpleNamespace(ok=False, mode='rejected', definitive_no_post=True))
    assert store.outcomes[0]['p_outcome'] == 'definite_no_send'
    assert store.rows['rejected']['status'] == 'pending'
    assert output['recovery_required'] == []


def test_acceptance_survives_calendar_write_failure(armed):
    store = OutcomeStore([_row('accepted')], mark_error=True)
    output, _ = run(store)
    assert store.outcomes[0]['p_outcome'] == 'provider_accepted'
    assert armed[0].outcome_recorded
    assert store.events == ['provider', 'outcome', 'mark_published']
    assert store.rows['accepted']['status'] == 'publishing'


def test_unconsumed_success_never_certifies_receipt(armed):
    store = OutcomeStore([_row('held')])
    output, _ = run(store, consumed=False)
    assert output['recovery_required'] == ['held']
    assert store.outcomes == [] and store.published_calls == []


def test_off_has_no_rpc_and_preserves_existing_publish_behavior(monkeypatch):
    monkeypatch.setenv('AGENT_CALENDAR_AUTOPUBLISH', 'true')
    monkeypatch.setenv('AGENT_PUBLISH_ENABLED', 'true')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'false')
    monkeypatch.delenv(exact.FLAG, raising=False)
    store = OutcomeStore([_row('off')])
    output, _ = run(store)
    assert output['published'] == ['off']
    assert store.events == ['provider', 'mark_published']
    assert store.outcomes == []


def test_contradictory_success_without_receipt_cannot_prove_no_send(armed):
    store = OutcomeStore([_row('contradictory')])
    output, _ = run(store, SimpleNamespace(ok=True, mode='published', media_id='',
                                          definitive_no_post=True))
    assert store.outcomes[0]['p_outcome'] == 'uncertain'
    assert output['recovery_required'] == ['contradictory']
    assert store.rows['contradictory']['status'] == 'publishing'


@pytest.mark.parametrize('recorded', [True, False])
def test_sibling_cannot_send_before_committed_acceptance(armed, monkeypatch, recorded):
    original_authority = scope.authorized_send
    store = OutcomeStore([_row('first'), _row('sibling', account='facebook')], response=recorded)

    @contextmanager
    def sibling_authority(store, row, token):
        if row['id'] == 'sibling':
            # Offline model of SQL's latest-outcome condition: an attempted write
            # is insufficient. Only DB-confirmed acceptance unlocks a sibling.
            if not (recorded and store.outcomes
                    and store.outcomes[-1]['p_outcome'] == 'provider_accepted'):
                raise exact.ExactByteSendHold('sibling has unresolved exact bytes')
        with original_authority(store, row, token):
            yield
    monkeypatch.setattr(scope, 'authorized_send', sibling_authority)
    output, fake = run(store)
    if recorded:
        assert output['published'] == ['first', 'sibling']
        assert store.events == ['provider', 'outcome', 'mark_published'] * 2
    else:
        assert output['published'] == []
        assert store.events == ['provider', 'outcome']
        assert store.rows['first']['status'] == 'publishing'
        assert output['recovery_required'] == ['first']
    assert len(fake.calls) == (2 if recorded else 1)
