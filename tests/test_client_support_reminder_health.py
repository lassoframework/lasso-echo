from datetime import datetime, timedelta, timezone

import pytest

from agent.jobs import client_support_reminder_health as health

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
TID = '12345678-1234-4234-8234-123456789abc'


class Poster:
    def __init__(self):
        self.posts = []
        self.response = {'ok': True, 'channel': health._INTERNAL_SUPPORT_CHANNEL, 'ts': '123.45'}
        self.readback_change = {}
        self.fail = False

    def _send(self, *args, **kwargs):
        return {'ok': True, 'user_id': 'UBOT', 'bot_id': 'BBOT'}

    def post_notice(self, body):
        self.posts.append(body)
        if self.fail:
            raise TimeoutError()
        return self.response

    def read_conversation_messages(self, channel, **kwargs):
        assert kwargs == {'ts': '123.45', 'oldest': '123.45'}
        message = {'ts': '123.45', 'text': self.posts[-1], 'user': 'UBOT'}
        message.update(self.readback_change)
        return {'ok': True, 'channel': channel, 'messages': [message]}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    health._LAST_ATTEMPT.clear()
    monkeypatch.setenv('AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED', 'true')
    monkeypatch.setenv('AGENT_SLACK_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('AGENT_SUPPORT_CHANNEL_ID', health._INTERNAL_SUPPORT_CHANNEL)


def test_confirmed_exact_bot_readback():
    poster = Poster()
    result = health.run({'ok': True, 'skipped': [TID]}, poster=poster, now=NOW)
    assert result['confirmed'] is True and result['slack_ts'] == '123.45'
    assert TID in poster.posts[0]


@pytest.mark.parametrize('change', [{'user': 'UOTHER'}, {'text': 'different'},
                                  {'ts': '123.46'}, {'thread_ts': '123.44'}])
def test_readback_identity_mismatch_unconfirmed(change):
    poster = Poster()
    poster.readback_change = change
    assert not health.run({'ok': False}, poster=poster, now=NOW)['confirmed']


def test_timeout_is_unconfirmed_and_not_immediately_retried():
    poster = Poster()
    poster.fail = True
    assert health.run({'ok': False}, poster=poster, now=NOW)['state'] == 'unconfirmed'
    assert health.run({'ok': False}, poster=poster, now=NOW)['state'] == 'cooldown'
    assert len(poster.posts) == 1
    health.run({'ok': False}, poster=poster, now=NOW + timedelta(hours=1))
    assert len(poster.posts) == 2


def test_disabled_and_healthy_do_not_post(monkeypatch):
    poster = Poster()
    assert health.run({'ok': True}, poster=poster, now=NOW)['state'] == 'healthy'
    monkeypatch.delenv('AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED')
    assert health.run({'ok': False}, poster=poster, now=NOW)['state'] == 'disabled'
    assert not poster.posts


def test_no_route_fails_closed(monkeypatch):
    poster = Poster()
    monkeypatch.setenv('AGENT_SUPPORT_CHANNEL_ID', '')
    assert health.run({'ok': False}, poster=poster, now=NOW)['reason'] == 'ops_route_unavailable'
    assert not poster.posts


def test_client_channel_override_is_never_alerted(monkeypatch):
    poster = Poster()
    monkeypatch.setenv('AGENT_SUPPORT_CHANNEL_ID', 'CCLIENT123')
    result = health.run({'ok': False, 'skipped': [TID]}, poster=poster, now=NOW)
    assert result['reason'] == 'ops_route_unavailable'
    assert not poster.posts


def test_bounded_ids_and_no_client_text():
    poster = Poster()
    ids = [f'12345678-1234-4234-8234-{i:012d}' for i in range(100)]
    result = health.run({'ok': True, 'unrouted': ids + ['private client words'],
                         'error': 'private exception secret'}, poster=poster, now=NOW)
    assert result['confirmed'] is True
    body = poster.posts[0]
    assert 'private' not in body and '101' in body and '20 of 100' in body
    assert len(body) < 1400


def test_post_response_wrong_channel_unconfirmed():
    poster = Poster()
    poster.response['channel'] = 'CCLIENT'
    assert not health.run({'ok': False}, poster=poster, now=NOW)['confirmed']


def test_unverified_auth_does_not_post():
    poster = Poster()
    poster._send = lambda *a, **kw: {'ok': False}
    assert not health.run({'ok': False}, poster=poster, now=NOW)['confirmed']
    assert not poster.posts
