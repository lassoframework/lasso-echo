"""
tests/test_not_wired_guard.py -- the sibling of test_db_constraint_contract.py, for the
OTHER half of the same postmortem (2026-09-05).

test_db_constraint_contract.py catches "the code writes a value the database will reject".
This file catches the shape that keeps appearing next to it: THE CAPABILITY EXISTS AND
NOTHING IS CONNECTED TO IT. Three instances of it have now been found in this one system,
each of which looked exactly like working software from the outside because the fallback
path was a legitimate one:

  1. delivery_status='posting'  -- the outbox's compare-and-swap state, written by code that
     had always needed it, rejected by a CHECK constraint that never listed it. Every claim
     failed, so no message had EVER posted. (Caught by the sibling file.)
  2. listener_watch             -- a watchdog loop that shipped and was never started. The
     safety net existed in the repo and nowhere in the process.
  3. classify_llm=None          -- listener_wiring.live_deps() hardcoded None where the
     classifier's model callable belonged, from the module's very first commit (a5a008a,
     2026-09-03) onward. config.slack_convo_model()'s own docstring promised "the LLM
     fallback of the classifier" the whole time. Result: in production the classifier could
     only ever reach its deterministic rules, and everything else fell to ESCALATE by
     construction -- #fixer filling with "the classifier did not decide".
  3b. And its subtler twin, found the same day: echo_ticket_wiring passed the ANSWER LANE's
     llm -- (system, user, model=None) -- as the CLASSIFIER's llm, whose contract is
     (text) -> label. Every call raised TypeError, classify() escalated on the exception
     exactly as designed, and the outcome was indistinguishable from "nothing to say". A
     wrong-shaped wire is the same bug as a missing one, only harder to see.

WHAT CATCHES THIS CLASS: a boot-time assertion that a capability claimed ON by config has a
correctly shaped implementation behind it, plus this static test that the assertion is
actually called from the real wiring path (an assertion nobody calls is itself an instance of
the bug it is meant to catch -- which is the joke this file refuses to become).
"""
import ast
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.slack_convo import answer_lane as AL  # noqa: E402
from agent.slack_convo import classifier as C  # noqa: E402
from agent.slack_convo import identities as IDS  # noqa: E402
from agent.slack_convo import listener_wiring as LW  # noqa: E402

_WIRING = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "agent", "slack_convo", "listener_wiring.py")


def _live_deps_source():
    return inspect.getsource(LW.live_deps)


def _both_live_deps():
    """BOTH wiring paths. The first version of this guard inspected only
    listener_wiring.live_deps -- and RTF-2, the bug that actually reached a real client, was
    in the OTHER one (echo_ticket_wiring). A guard that covers one of two doors is not a
    guard."""
    from agent import echo_ticket_wiring as ETW
    return {"listener_wiring.live_deps": inspect.getsource(LW.live_deps),
            "echo_ticket_wiring.live_deps": inspect.getsource(ETW.live_deps)}


@pytest.mark.parametrize("where", ["listener_wiring.live_deps", "echo_ticket_wiring.live_deps"])
def test_neither_wiring_path_hardcodes_a_none_capability(where):
    src = _both_live_deps()[where]
    tree = ast.parse(src.strip())
    banned = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg and (
                node.arg.endswith("_llm") or node.arg in ("answer", "resolve_identity")):
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                banned.append(node.arg)
    assert not banned, (f"{where} hardcodes {banned} to None -- that is the classify_llm=None "
                        f"bug returning.")


def test_the_portal_bridge_actually_forwards_the_classifier_callable():
    """RTF-2 as a WIRE test, not a substring test. The earlier version asserted a parameter
    name and a source substring, and would still have passed if intake_pass accepted
    classify_llm and dropped it on the floor. This one runs the code and watches the callable
    arrive at classify()."""
    from agent import echo_ticket_worker as ETW
    from agent.slack_convo import classifier as CC
    seen = {}

    class _Bus:
        def find_new_tickets(self, **kw):
            return [{"id": "t-1", "product": "echo", "source": "website_tab",
                     "client_id": "g-1", "reporter": "owner@gym.com",
                     "raw_text": "something ambiguous entirely", "status": "new"}]

        def inbound_count(self, tid):
            return 1

        def ticket(self, tid):
            return {"id": tid, "status": "new"}

        def set_ticket(self, tid, **f):
            return {}

        def record_outbound(self, **kw):
            return {"id": "m-1"}

    def probe(text):
        seen["called_with"] = text
        return CC.QUESTION

    import os as _os
    _os.environ["AGENT_PORTAL_ECHO_TICKETS_ENABLED"] = "true"
    ETW.intake_pass(
        _Bus(), slack_lookup_email=lambda e: "U1",
        slack_user_info=lambda u: {"id": u, "is_bot": False, "email": "owner@gym.com"},
        portal_lookup=lambda e: {"role": "client",
                                 "gyms": [{"gym_id": "g-1", "relationship": "client_owner",
                                           "account_key": "k"}]},
        open_group_dm=lambda ids: {"ok": False}, post_first_message=lambda c, t: {"ok": False},
        write_hold_notice=lambda **kw: None, classify_llm=probe,
        fetch_state=lambda t, w: {}, llm=lambda *a, **k: "",
        log=lambda *a, **k: None)
    assert seen.get("called_with") == "something ambiguous entirely", \
        "intake_pass accepted classify_llm but never handed it to classify()"


def test_live_deps_never_hardcodes_a_none_capability():
    """The literal regression, asserted statically so no future edit can restore it.

    Any `<name>_llm=None` or `answer=None` written as a CONSTANT in live_deps() is the bug:
    the seam exists, the wire is missing, and nothing at runtime will ever say so."""
    tree = ast.parse(_live_deps_source().strip())
    banned = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg and (
                node.arg.endswith("_llm") or node.arg in ("answer", "resolve_identity")):
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                banned.append(node.arg)
    assert not banned, (f"live_deps() hardcodes {banned} to None -- that is the "
                        f"classify_llm=None bug returning. Wire it, or gate it behind a flag "
                        f"whose OFF state is honest about what is not running.")


def test_the_boot_assertion_is_actually_called_from_the_wiring_path():
    """An assertion nobody calls is the very bug it exists to catch."""
    assert "build_classify_llm(" in _live_deps_source()
    assert "assert_classifier_shape(" in inspect.getsource(LW.build_classify_llm)


def test_a_capability_flagged_on_with_nothing_behind_it_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    with pytest.raises(LW.NotWiredError):
        LW.build_classify_llm(IDS.get("echo"), log=lambda *a: None, factory=lambda: None)


def test_the_flag_off_state_says_out_loud_what_is_not_running(monkeypatch):
    """The other half of the rule: OFF is allowed, silence is not. A deployment running with
    deterministic classification only must SAY so at boot, or the next person debugging
    #fixer has no way to tell this state from a working one."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", raising=False)
    said = []
    assert LW.build_classify_llm(IDS.get("echo"), log=said.append) is None
    assert any("deterministic rules only" in line for line in said)


def test_the_answer_lane_callable_can_never_be_wired_as_the_classifier():
    """RTF-2, structurally. The two contracts are different and the check knows it."""
    with pytest.raises(LW.NotWiredError):
        LW.assert_classifier_shape(AL.default_llm, IDS.get("echo"))


def test_every_slack_convo_flag_has_a_config_reader():
    """A flag named in code with no reader in config is another shape of not-wired: the env
    var can be set on the service forever and change nothing."""
    for name in ("slack_convo_enabled", "slack_convo_identity_enabled",
                 "slack_convo_client_reply_armed", "slack_convo_staff_reply_armed",
                 "slack_convo_classifier_llm_enabled", "slack_convo_auto_answer_armed",
                 "slack_convo_cross_product_routing_enabled"):
        assert callable(getattr(config, name)), f"config.{name} is missing"


def test_auto_answer_cannot_be_armed_without_its_prerequisites(monkeypatch):
    """The narrower permission is narrower in the direction that matters: it cannot be the
    thing that lets a bot speak to a client on its own."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLIENT_REPLY", raising=False)
    assert config.slack_convo_auto_answer_armed("echo") is False
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    assert config.slack_convo_auto_answer_armed("echo") is True
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "false")
    assert config.slack_convo_auto_answer_armed("echo") is False


def test_classifier_llm_flag_requires_the_identity_to_be_enabled(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "false")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    assert config.slack_convo_classifier_llm_enabled("echo") is False
