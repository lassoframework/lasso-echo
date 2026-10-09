"""
tests/test_echo_ticket_wiring.py -- D46/D47 live-bug regression (2026-09-04).

Found running Blake's requested real Echo regression test: `_slack_lookup_email_factory`
built its URL by raw string concatenation, leaving '+' un-percent-encoded. Slack's API
(like most application/x-www-form-urlencoded parsers) decodes an unencoded '+' in a query
string as a literal SPACE, so any email containing '+' -- a real Gmail "+alias" a real
client might use, not just the test account that surfaced this -- silently failed the
lookup. resolve_client_identity then reported "no Slack account for this authenticated
email" for a client who actually had one, escalating instead of answering.
"""
from agent import echo_ticket_wiring as W
from agent.slack_convo.bus import Bus


class _FakePoster:
    def __init__(self, response):
        self.calls = []
        self._response = response

    def _send(self, url, payload):
        self.calls.append(url)
        return self._response


def test_slack_lookup_email_factory_percent_encodes_a_plus_in_the_email():
    poster = _FakePoster({"ok": True, "user": {"id": "U123"}})
    lookup = W._slack_lookup_email_factory(poster)
    result = lookup("blake+zztest@lassoframework.com")
    assert result == "U123"
    assert len(poster.calls) == 1
    url = poster.calls[0]
    # The literal '+' must never appear un-encoded in the query string -- that is
    # exactly the bug: Slack's parser reads a raw '+' there as a space.
    assert "email=blake+zztest" not in url
    assert "email=blake%2Bzztest%40lassoframework.com" in url


def test_slack_lookup_email_factory_returns_none_on_not_ok():
    poster = _FakePoster({"ok": False, "error": "users_not_found"})
    lookup = W._slack_lookup_email_factory(poster)
    assert lookup("nobody@lassoframework.com") is None


def test_live_echo_stamp_uses_request_and_identity_cas():
    calls = []
    bus = Bus.__new__(Bus)
    bus._patch = lambda table, match, fields: calls.append((table, match, fields)) or None
    stamp = W._stamp_ticket_factory(bus)
    original = {
        "request_version": 4, "status": "verification",
        "classification": "answerable_question", "product": "echo",
        "client_id": "gym-1", "bot_identity": "echo", "slack_user_id": "U_CLIENT",
        "slack_channel_id": None, "slack_thread_ts": None,
    }
    stamp("ticket-1", expected_ticket=original, channel_id="G123",
          thread_ts="9999.1", slack_user_id="U_CLIENT",
          bot_identity="echo", identity_kind="client")
    table, match, fields = calls[0]
    assert table == "support_tickets"
    assert match["request_version"] == "eq.4"
    assert match["bot_identity"] == "eq.echo"
    assert match["slack_user_id"] == "eq.U_CLIENT"
    assert match["slack_channel_id"] == "is.null"
    assert match["slack_thread_ts"] == "is.null"
    assert fields["slack_channel_id"] == "G123"


def test_post_first_message_factory_binds_verify_sender_to_the_captured_poster():
    """The current-notice send admission authenticates the exact transport that
    POSTs: verify_sender must be the SAME captured poster's auth.test, not a
    later environment lookup."""
    poster = _FakePoster({"ok": True, "ts": "1.2"})
    poster.auth_test = lambda: {"ok": True, "user_id": "U_ECHO"}
    post_first_message = W._post_first_message_factory(poster)
    assert post_first_message.verify_sender == poster.auth_test
    assert post_first_message.verify_sender()["user_id"] == "U_ECHO"
