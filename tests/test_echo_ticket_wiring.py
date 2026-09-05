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
