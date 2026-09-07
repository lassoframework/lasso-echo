"""THE TWO-FLAG INTERLOCK.

The landmine: a reply this lane writes is a CONVERSATIONAL kind, and the outbox gates
those on `slack_convo_client_reply_armed(identity)` alone (outbox.py:141-144, called
from :429). SLACK_CONVO_ECHO_CLIENT_REPLY has been true on the Railway echo service
since 2026-09-05 (DECISIONS.md D51). So without an interlock, ONE boolean would be the
whole distance between "nothing has ever been sent" and real autonomous messages to
paying gym owners.

Every test below is written from the operator's side: what happens if somebody sets
one thing.
"""
import pytest

from agent.client_dm_support import arming as A

ON = {A.ENV_MASTER: "true"}
BOTH = {A.ENV_MASTER: "true", A.ENV_CLIENT_REPLY: "true"}


def ack_for(identity="echo", other_flag=True):
    return A.required_ack(identity, client_reply_armed=other_flag)


def pf(env, other_flag=True, identity="echo"):
    return A.preflight(identity, env=env, client_reply_armed=other_flag)


# ---------------------------------------------------------------------------
# THE CORE PROPERTY: no single environment variable arms live client replies.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("env", [
    {},
    {A.ENV_MASTER: "true"},
    {A.ENV_CLIENT_REPLY: "true"},
    {A.ENV_LIVE_ACK: "client-dm-live:echo:slack-convo-client-reply=on"},
    {A.ENV_MASTER: "true", A.ENV_CLIENT_REPLY: "true"},
    {A.ENV_MASTER: "true", A.ENV_LIVE_ACK: "client-dm-live:echo:slack-convo-client-reply=on"},
    {A.ENV_CLIENT_REPLY: "true",
     A.ENV_LIVE_ACK: "client-dm-live:echo:slack-convo-client-reply=on"},
])
def test_no_proper_subset_of_the_three_settings_can_reply_to_a_client(env):
    """THE MUTATION TARGET for the arming interlock. All seven proper subsets, with
    SLACK_CONVO_ECHO_CLIENT_REPLY already ON -- i.e. the live production state."""
    verdict = pf(env, other_flag=True)
    assert verdict.may_reply_to_clients is False, env
    assert verdict.mode in (A.MODE_OFF, A.MODE_ESCALATE_ONLY)


def test_all_three_together_arm_it():
    v = pf({**BOTH, A.ENV_LIVE_ACK: ack_for()}, other_flag=True)
    assert v.mode == A.MODE_LIVE and v.may_reply_to_clients is True


# ---------------------------------------------------------------------------
# WHY THE THIRD SETTING IS NOT JUST A THIRD BOOLEAN.
# ---------------------------------------------------------------------------
def test_the_required_token_carries_the_other_flags_live_value():
    assert ack_for(other_flag=True) != ack_for(other_flag=False)
    assert "slack-convo-client-reply=on" in ack_for(other_flag=True)
    assert "slack-convo-client-reply=off" in ack_for(other_flag=False)


def test_an_ack_goes_stale_the_moment_the_other_flag_moves():
    """The property that makes this structural rather than procedural: an operator
    cannot arm this once and have it stay armed through a change to the flag that
    actually opens the outbox."""
    token = ack_for(other_flag=True)
    live = pf({**BOTH, A.ENV_LIVE_ACK: token}, other_flag=True)
    assert live.mode == A.MODE_LIVE
    after_flip = pf({**BOTH, A.ENV_LIVE_ACK: token}, other_flag=False)
    assert after_flip.mode == A.MODE_ESCALATE_ONLY
    assert after_flip.may_reply_to_clients is False


def test_an_ack_for_another_identity_does_not_arm_this_one():
    token = A.required_ack("ranger", client_reply_armed=True)
    v = pf({**BOTH, A.ENV_LIVE_ACK: token}, other_flag=True, identity="echo")
    assert v.mode == A.MODE_ESCALATE_ONLY


@pytest.mark.parametrize("token", [
    "true", "yes", "1", "client-dm-live", "client-dm-live:echo",
    "client-dm-live:echo:slack-convo-client-reply=ON",
    "client-dm-live:echo:slack-convo-client-reply=off",
    "client-dm-live:ranger:slack-convo-client-reply=on",
])
def test_a_plausible_but_wrong_token_refuses(token):
    """The ack is an exact string, not a truthy value. Typing 'true' into it -- the
    reflex for every other flag in this system -- must not arm anything."""
    assert pf({**BOTH, A.ENV_LIVE_ACK: token}).may_reply_to_clients is False


def test_surrounding_whitespace_is_tolerated_deliberately():
    """A dashboard that pads a value must not produce a lane that reads as BROKEN.
    The token's CONTENT is exact; its framing whitespace is not part of the decision,
    and that is a choice recorded here rather than an accident."""
    padded = f"  {ack_for()}\n"
    assert pf({**BOTH, A.ENV_LIVE_ACK: padded}).mode == A.MODE_LIVE


# ---------------------------------------------------------------------------
# THE REFUSAL MUST NAME WHAT WAS ABOUT TO GO LIVE.
# ---------------------------------------------------------------------------
def test_the_refusal_names_the_identity_the_surface_and_the_conditions():
    v = pf(BOTH, other_flag=True)
    assert v.mode == A.MODE_ESCALATE_ONLY
    banner = v.banner
    assert "REFUSED" in banner
    assert "echo" in banner
    assert "REAL messages to paying gym owners" in banner
    assert "SLACK_CONVO_ECHO_CLIENT_REPLY is currently ON" in banner
    for cid in ("drive_library_empty", "drive_share_revoked", "cta_pool_empty"):
        assert cid in banner
    assert v.required_ack in banner


def test_the_refusal_hands_over_the_exact_token_to_set():
    v = pf(BOTH, other_flag=True)
    armed = pf({**BOTH, A.ENV_LIVE_ACK: v.required_ack}, other_flag=True)
    assert armed.mode == A.MODE_LIVE


def test_the_live_banner_says_when_the_outbox_will_hold_everything_anyway():
    v = pf({**BOTH, A.ENV_LIVE_ACK: ack_for(other_flag=False)}, other_flag=False)
    assert v.mode == A.MODE_LIVE
    assert "will HOLD every row" in v.banner
    assert "Nothing reaches a client until that flag is on too" in v.banner


# ---------------------------------------------------------------------------
# OFF MUST BE LOUD, so OFF is distinguishable from BROKEN (D68).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("env,flag", [
    ({}, A.ENV_MASTER),
    ({A.ENV_MASTER: "true"}, A.ENV_CLIENT_REPLY),
    ({A.ENV_MASTER: "true", A.ENV_CLIENT_REPLY: "true"}, A.ENV_LIVE_ACK),
])
def test_every_refusal_names_the_setting_responsible(env, flag):
    v = pf(env)
    assert v.reason, "a silent refusal is indistinguishable from a broken lane"
    assert flag in v.reason


def test_master_off_means_nothing_is_read_at_all():
    v = pf({A.ENV_CLIENT_REPLY: "true", A.ENV_LIVE_ACK: ack_for()})
    assert v.mode == A.MODE_OFF
    assert "No ticket is read" in v.reason


def test_preflight_reports_the_other_flags_state_without_changing_it():
    """This module must never wrap, shadow or alter SLACK_CONVO_* semantics; it only
    reads that flag to build its own token and to tell the truth in the banner."""
    assert pf(BOTH, other_flag=True).outbox_gate_open is True
    assert pf(BOTH, other_flag=False).outbox_gate_open is False


def test_this_capability_never_touches_a_slack_convo_flag():
    """The #fixer bus's own auto-answer gate (D67) is a separately-owned, still-locked
    decision. Nothing in this package may set, wrap or override a SLACK_CONVO_* value,
    and in particular the D67 override must appear nowhere."""
    import os
    pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "agent", "client_dm_support")
    for fname in sorted(os.listdir(pkg)):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(pkg, fname), encoding="utf-8") as fh:
            src = fh.read()
        assert "SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE" not in src, fname
        assert "os.environ[" not in src, f"{fname} writes an environment variable"
        assert "slack_convo_auto_answer_armed" not in src, fname
