"""Isolated provider bridge checks, without attester credentials or network."""
from datetime import datetime as DateTime, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from agent import forward_media_publish as bridge, forward_media_guard as guard


def fixture_store():
    row = dict(id=str(uuid4()), gym_id='gym', caption='caption', image_url='https://media/x',
               source_media_url='https://media/source', post_date='2026-10-06', visual_group_key='group')
    token = str(uuid4())
    persisted = dict(row, status='publishing', publish_claim_token=token)
    evidence = [{'evidence_id': str(uuid4())}]
    response = SimpleNamespace(status_code=200, json=lambda: evidence)
    snapshot = dict(calendar_row_id=row['id'], gym_id=row['gym_id'], account=None,
                    format=None, gbp_location_id=None,
                    post_date=row['post_date'], group_key=row['visual_group_key'],
                    source_url=row['source_media_url'], image_url=row['image_url'], thumbnail_url=None, revision='revision')
    store = SimpleNamespace(get_row=lambda *a: persisted, _client=lambda: SimpleNamespace(get=lambda *a, **k: response,
                            post=lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: snapshot)),
                            _rest=lambda p: p, _headers=lambda *a: {})
    return row, token, store, persisted, evidence, response


@pytest.mark.parametrize('result', [None, False, 1, 'true', {}, []])
def test_claim_must_be_literal_true(monkeypatch, result):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    row, token, store, *_ = fixture_store()
    monkeypatch.setattr(guard, 'claim', lambda *a: result)
    with pytest.raises(guard.ForwardMediaVerificationHold):
        bridge.authorize(store, row, token)


@pytest.mark.parametrize('failure', ['evidence', 'owner', 'creative', 'read'])
def test_persisted_authority_required(monkeypatch, failure):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    row, token, store, persisted, evidence, response = fixture_store()
    monkeypatch.setattr(guard, 'claim', lambda *a: pytest.fail('invalid evidence reached claim'))
    if failure == 'evidence':
        evidence.clear()
    elif failure == 'owner':
        persisted['publish_claim_token'] = str(uuid4())
    elif failure == 'creative':
        persisted['image_url'] = 'changed'
    else:
        response.status_code = 503
    with pytest.raises(guard.ForwardMediaVerificationHold):
        bridge.authorize(store, row, token)


def test_success_uses_persisted_evidence_and_gbp_facade(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    row, token, store, _, evidence, _ = fixture_store()
    seen = []
    def claim(*args):
        seen.append(args)
        return True
    monkeypatch.setattr(guard, 'claim', claim)
    assert bridge.authorize(SimpleNamespace(_s=store), row, token) is True
    assert seen == [(store, row['id'], token, evidence[0]['evidence_id'], 'revision')]


def test_default_off_has_no_store_or_authority_access(monkeypatch):
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    assert bridge.authorize(None, {}, None) is True


def test_legacy_runner_is_held_before_claim_when_guard_on(monkeypatch):
    from agent import runner, db
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    monkeypatch.setattr(db, 'socialapi_claim', lambda *a: pytest.fail('legacy claim reached'))
    draft = SimpleNamespace(draft_id='legacy', account_key='gym_ig')
    assert runner._claimed_meta_publish(draft, SimpleNamespace(key='gym_ig')) == ('held', None)
    assert runner._autonomous_publish(draft, None, None) is False


def test_snapshot_race_holds_before_evidence_claim(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    row, token, store, *_ = fixture_store()
    client = store._client()
    snapshot = dict(calendar_row_id=row['id'], post_date=row['post_date'], group_key='group',
                    source_url=row['source_media_url'], image_url='changed', thumbnail_url=None, revision='new')
    client.post = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: snapshot)
    store._client = lambda: client
    monkeypatch.setattr(guard, 'claim', lambda *a: pytest.fail('changed outgoing snapshot reached claim'))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='snapshot'):
        bridge.authorize(store, row, token)


@pytest.mark.parametrize('changed', [('gym_id', 'other-gym'), ('account', 'other_ig')])
def test_rerouted_snapshot_holds_before_evidence_claim(monkeypatch, changed):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    row, token, store, *_ = fixture_store()
    client = store._client()
    snapshot = dict(calendar_row_id=row['id'], gym_id=row['gym_id'], account=None,
                    format=None, gbp_location_id=None,
                    post_date=row['post_date'], group_key=row['visual_group_key'],
                    source_url=row['source_media_url'], image_url=row['image_url'],
                    thumbnail_url=None, revision='rerouted')
    snapshot[changed[0]] = changed[1]
    client.post = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: snapshot)
    store._client = lambda: client
    monkeypatch.setattr(guard, 'claim', lambda *a: pytest.fail('reroute reached claim'))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='snapshot'):
        bridge.authorize(store, row, token)


def test_gbp_reserves_local_attempt_day_under_owned_token(monkeypatch):
    from agent import config
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    monkeypatch.setattr(config, 'posting_timezone_for', lambda _gym: 'America/New_York')
    monkeypatch.setattr(bridge, 'datetime', SimpleNamespace(
        now=lambda _tz: DateTime(2026, 10, 7, 1, 30, tzinfo=timezone.utc)))
    row, token, store, persisted, evidence, _ = fixture_store()
    row['account'] = persisted['account'] = 'googlebusiness'
    snapshot = dict(calendar_row_id=row['id'], gym_id=row['gym_id'],
                    account='googlebusiness', format=None, gbp_location_id=None,
                    post_date=row['post_date'], group_key=row['visual_group_key'],
                    source_url=row['source_media_url'], image_url=row['image_url'],
                    thumbnail_url=None, revision='revision')
    patch_calls = []
    def patch(_url, *, params, json, **_kwargs):
        patch_calls.append((params, json))
        persisted.update(json)
        return SimpleNamespace(status_code=200, json=lambda: [dict(persisted)])
    client = SimpleNamespace(
        patch=patch,
        post=lambda *_a, **_k: SimpleNamespace(status_code=200, json=lambda: snapshot),
        get=lambda *_a, **_k: SimpleNamespace(status_code=200, json=lambda: evidence))
    store._client = lambda: client
    monkeypatch.setattr(guard, 'claim', lambda *_a: True)
    assert bridge.authorize(store, row, token) is True
    assert patch_calls[0][0]['publish_claim_token'] == 'eq.' + token
    assert patch_calls[0][1]['publish_reservation_day'] == '2026-10-06'


def test_gbp_stale_attempt_day_holds_before_claim(monkeypatch):
    from agent import config
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    monkeypatch.setattr(config, 'posting_timezone_for', lambda _gym: 'America/New_York')
    monkeypatch.setattr(bridge, 'datetime', SimpleNamespace(
        now=lambda _tz: DateTime(2026, 10, 7, 1, 30, tzinfo=timezone.utc)))
    row, token, store, persisted, *_ = fixture_store()
    row['account'] = persisted['account'] = 'googlebusiness'
    persisted['publish_reservation_day'] = '2026-10-05'
    monkeypatch.setattr(guard, 'claim', lambda *_a: pytest.fail('stale day reached claim'))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='stale'):
        bridge.authorize(store, row, token)


def test_manual_approval_held_before_claim_or_provider(monkeypatch):
    from agent import approvals
    from agent.drafter import DraftStatus
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    monkeypatch.setattr(approvals, '_is_approver', lambda *a, **k: True)
    draft = SimpleNamespace(draft_id='manual', account_key='gym_ig', status=DraftStatus.PENDING)
    store = SimpleNamespace(claim_for_publish=lambda *a: pytest.fail('legacy claim reached'))
    pub = SimpleNamespace(publish=lambda *a: pytest.fail('manual provider reached'))
    result = approvals.handle_action('approve', draft, 'owner', store=store, publisher=pub)
    assert result.ok is False and 'forward_media_verification' in result.detail
    assert draft.status == DraftStatus.PENDING
