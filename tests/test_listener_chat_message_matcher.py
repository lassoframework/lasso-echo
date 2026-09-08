"""agent/listener.py's on_chat_message Bolt matcher (2026-09-07 fix).

THE BUG (Chad Edwards C0BUNHG49EH / John Weeks C0BUWTCS2J3, 2026-09-06): a Slack Bolt
App only ever runs the FIRST listener in registration order whose matcher returns True
for a given incoming event -- ThreadListenerRunner.run() calls the built-in ack()
synchronously (auto_acknowledgement=True for every `@app.message`/`@app.event`
listener) and App.dispatch() returns that ack response the instant ONE listener
matches, before any LATER listener in self._listeners is even checked. It does not run
every listener whose pattern would match, no matter how the two decorators read.

listener.py registers `@app.message("")` (on_chat_message, a Blake-only free-text
publish command) BEFORE `_convo.attach(app, "echo")` registers slack_convo's own
`@app.event("message")` listener a few lines later. An empty keyword matches virtually
every plain human message, so on_chat_message matched EVERY real Slack message on
Echo's identity FIRST -- for every gym, for the entire life of this project -- and
slack_convo's own listener (the thing that turns a message into a support_tickets row)
was never even reached. This is why no real ticket has ever had source=
'slack_conversation': the adapter's own code was correct in isolation and simply never
ran for a real inbound Slack event.

THE FIX moves the "is this Blake?" check out of on_chat_message's function body (too
late -- Bolt had already committed to it) and into a Bolt-level `matchers=[...]`
predicate, so the listener structurally does not match anyone but the approver and
Bolt's dispatch loop falls through to the next listener for everyone else, exactly as
it always should have.

These tests exercise the REAL slack_bolt App.dispatch() path (not a hand-rolled
simulation of it) so a regression to a bare `@app.message("")` is caught here, not
just re-asserted by a test that assumes the fix already works."""
import ast
import importlib
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

slack_bolt = pytest.importorskip("slack_bolt")
from slack_bolt import App  # noqa: E402
from slack_bolt.request import BoltRequest  # noqa: E402
from slack_sdk.web.slack_response import SlackResponse  # noqa: E402

from agent import config  # noqa: E402
from agent.listener import _chat_message_is_from_approver  # noqa: E402 - the REAL matcher


def _fake_auth_test_result():
    return SlackResponse(
        client=None, http_verb="POST", api_url="https://slack.com/api/auth.test",
        req_args={}, data={"ok": True, "user_id": "UBOTID", "team_id": "T1", "bot_id": "B1"},
        headers={}, status_code=200,
    )


def _make_app(monkeypatch):
    # SingleTeamAuthorization.process() calls req.context.client.auth_test() for any
    # event other than app_uninstalled/tokens_revoked/team_access_revoked, and
    # context.client is a WebClient BUILT PER-REQUEST from the token (not app.client),
    # so the stub has to live on the WebClient CLASS to stay offline (conftest's
    # credential quarantine -- this suite must never make a real Slack API call).
    from slack_sdk import WebClient
    monkeypatch.setattr(WebClient, "auth_test", lambda self, **kw: _fake_auth_test_result())
    return App(token="xoxb-fake-token-for-tests", token_verification_enabled=False)


def _message_event(user, text, *, channel_type="mpim", channel="C0TEST", ts="1111.2222"):
    return {
        "token": "x", "team_id": "T1", "api_app_id": "A1", "type": "event_callback",
        "event_id": "Ev1", "event_time": 1111,
        "event": {
            "type": "message", "channel_type": channel_type, "channel": channel,
            "user": user, "text": text, "ts": ts,
        },
    }


def _register_listener_wiring(app, chat_publish_enabled, calls, later_calls, done):
    """Registers on_chat_message (with the REAL production matcher,
    agent.listener._chat_message_is_from_approver, imported above -- not a copy of its
    logic) FIRST, then a stand-in for slack_convo's own `@app.event("message")`
    listener SECOND -- the exact order listener.py uses (on_chat_message registered,
    then `_convo.attach()` a few lines later)."""

    @app.message("", matchers=[_chat_message_is_from_approver])
    def on_chat_message(message, say):
        calls.append(message)
        if not chat_publish_enabled:
            return

    @app.event("message")
    def later_listener(body, event):
        later_calls.append(event)
        done.set()

    return on_chat_message, later_listener


def test_non_approver_message_reaches_the_later_listener(monkeypatch):
    """A real client's message (Chad's shape: mpim, some other user id) must NOT be
    swallowed by on_chat_message -- it has to fall through to the listener registered
    after it. Revert the matcher to bare `@app.message("")` and this fails: Bolt commits
    to on_chat_message first, `later_calls` stays empty, and `done` never gets set."""
    monkeypatch.setattr(config, "APPROVER_SLACK_ID", "UAPPROVER")
    app = _make_app(monkeypatch)
    calls, later_calls, done = [], [], threading.Event()
    _register_listener_wiring(app, chat_publish_enabled=False, calls=calls,
                              later_calls=later_calls, done=done)

    req = BoltRequest(body=_message_event("UCHAD", "I connected our drive photo folder"),
                      mode="socket_mode")
    resp = app.dispatch(req)

    assert resp is not None, "dispatch must still ack the event"
    assert done.wait(timeout=3), "the later (slack_convo-shaped) listener never ran"
    assert len(later_calls) == 1
    assert later_calls[0]["user"] == "UCHAD"
    # on_chat_message's matcher must have refused the match entirely -- not merely
    # returned early from inside the function body.
    assert calls == []


def test_approver_message_still_reaches_on_chat_message(monkeypatch):
    """No regression for Blake's own free-text publish commands: his messages must
    still match on_chat_message (today's behavior), while the later listener's own
    ordinary flow is a separate concern this test does not assert on either way."""
    monkeypatch.setattr(config, "APPROVER_SLACK_ID", "UAPPROVER")
    app = _make_app(monkeypatch)
    calls, later_calls, done = [], [], threading.Event()
    _register_listener_wiring(app, chat_publish_enabled=True, calls=calls,
                              later_calls=later_calls, done=done)

    req = BoltRequest(body=_message_event("UAPPROVER", "publish lasso_ig now"),
                      mode="socket_mode")
    app.dispatch(req)

    # Give the async listener executor a moment; poll rather than a fixed sleep.
    for _ in range(300):
        if calls:
            break
        threading.Event().wait(0.01)
    assert len(calls) == 1
    assert calls[0]["user"] == "UAPPROVER"


# --- GAP CLOSED (2026-09-07 audit of #80) -----------------------------------------
#
# Everything above exercises the REAL `_chat_message_is_from_approver` predicate
# through a REAL slack_bolt App.dispatch() -- but it re-declares its OWN
# `@app.message("", matchers=[...])` listener inside `_register_listener_wiring`
# rather than the actual decorated `on_chat_message` that lives at
# agent/listener.py:988, inside `run_listener()`. Proof this was a real gap: revert
# listener.py:988 back to a bare `@app.message("")` (no matchers) and every test
# above still passes, because they never touch that line.
#
# `run_listener()` cannot safely be called from a unit test to close this the
# "call the real function" way: before it ever registers on_chat_message it runs
# opus_ingest.validated_project_ids() and creative_studio.validate_generation_models()
# (both reach out toward real project/model config), starts a connect_web thread
# when AGENT_CONNECT_ENABLED, requires real-looking AGENT_SLACK_BOT_TOKEN /
# AGENT_SLACK_APP_TOKEN env vars, attaches the entire slack_convo adapter, and ends
# by blocking forever in SocketModeHandler(app, app_token).start() -- a real socket
# connection attempt. Faking all of that just to reach one decorator is exactly the
# kind of mock-everything test that breaks for unrelated reasons on unrelated
# changes elsewhere in run_listener's preamble.
#
# So this closes the gap the other, more robust way: parse the ACTUAL COMMITTED
# SOURCE of agent/listener.py with the stdlib `ast` module and assert, directly
# against that source, that the real on_chat_message registration still carries
# `matchers=[_chat_message_is_from_approver]` and is still registered before
# `_convo.attach(...)`. This is not a copy of the fix's logic (there is nothing to
# duplicate -- it inspects the shipped decorator literally), and it fails on the
# EXACT mutation the auditor ran (reverting :988 to a bare `@app.message("")`).

_LISTENER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent", "listener.py")


def _parse_listener_module():
    with open(_LISTENER_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    return source, ast.parse(source, filename=_LISTENER_PATH)


def _find_message_decorator(tree):
    """Returns the ast.Call node for the `@app.message(...)` decorator on the
    on_chat_message function nested inside run_listener, or None if no such
    decorator exists (e.g. it was reverted to a plain function / different
    decorator entirely)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "on_chat_message":
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "message"):
                    return dec
    return None


def test_shipped_on_chat_message_decorator_still_carries_the_approver_matcher():
    """Static regression check against the literal committed source: on_chat_message's
    real `@app.message(...)` decorator (agent/listener.py:988) must still pass
    `matchers=[_chat_message_is_from_approver]`.

    Revert listener.py:988 to a bare `@app.message("")` (the auditor's exact
    mutation) and this fails: the decorator call keeps its `""` positional arg but
    loses the `matchers` keyword entirely, so `matcher_names` below comes back
    empty and the assertion trips."""
    _, tree = _parse_listener_module()
    dec = _find_message_decorator(tree)
    assert dec is not None, (
        "on_chat_message no longer has an @app.message(...) decorator at all -- "
        "the registration itself was removed or renamed"
    )

    matchers_kw = next((kw for kw in dec.keywords if kw.arg == "matchers"), None)
    assert matchers_kw is not None, (
        "on_chat_message's @app.message(...) decorator has no matchers= kwarg -- "
        "this is the exact regression: a bare @app.message(\"\") matches almost "
        "every plain human message and swallows it before slack_convo ever sees it"
    )

    assert isinstance(matchers_kw.value, ast.List) and matchers_kw.value.elts, (
        "matchers= is present but empty/not a list -- must contain the approver check"
    )
    matcher_names = {
        elt.id for elt in matchers_kw.value.elts if isinstance(elt, ast.Name)
    }
    assert "_chat_message_is_from_approver" in matcher_names, (
        f"matchers=[...] does not reference _chat_message_is_from_approver "
        f"(found: {sorted(matcher_names)}) -- the approver gate may have been "
        f"swapped for something else or removed"
    )


def test_on_chat_message_still_registered_before_slack_convo_attach():
    """Order matters: slack_bolt only runs the FIRST matching listener, so
    on_chat_message being registered ahead of `_convo.attach(...)` is exactly what
    made the bug possible -- and exactly what makes the matchers= fix necessary (a
    matcher that never matches non-approvers lets dispatch fall through to the next
    listener regardless of order, but if a future edit reorders these two AND drops
    the matcher, this pins down that on_chat_message is still the earlier
    registration slack_convo's listener has to fall through from)."""
    _, tree = _parse_listener_module()
    chat_message_line = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "on_chat_message":
            chat_message_line = node.lineno
            break
    assert chat_message_line is not None, "on_chat_message function not found"

    # Find the ACTUAL `_convo.attach(app, ...)` call node (not a docstring/comment
    # that merely mentions it -- e.g. _chat_message_is_from_approver's own docstring
    # above references "_convo.attach(app, ...)" in prose).
    attach_line = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "attach"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_convo"):
            attach_line = node.lineno
            break
    assert attach_line is not None, "_convo.attach(app, ...) call not found"

    assert chat_message_line < attach_line, (
        "on_chat_message is no longer registered before _convo.attach(...) -- "
        "re-verify the swallow-order reasoning still holds for whatever changed"
    )
