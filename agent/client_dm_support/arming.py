"""
arming.py — THE INTERLOCK. Why one flag can never put a client message on the wire.

THE LANDMINE THIS CLOSES, stated precisely because it is live today.

A reply this lane writes is a support_messages row of kind `status`, a CONVERSATIONAL
kind. `outbox._dispatch_one` gates a client-bound conversational row on exactly one
thing (outbox.py:429 -> `_recipient_armed`, outbox.py:141-144):

    config.slack_convo_client_reply_armed(identity.name)

and `SLACK_CONVO_ECHO_CLIENT_REPLY=true` has been armed on the Railway `echo` service
since 2026-09-05 (DECISIONS.md D51, line 1013). So the outbox's own gate is ALREADY
OPEN. Without an interlock here, `AGENT_CLIENT_DM_AUTOFIX=true` -- one boolean, on one
service, typed once -- would be the entire distance between "nothing has ever been
sent" and real autonomous messages to paying gym owners.

That is D56's lesson pointed the other way: nobody should discover months from now
that a flag they set once quietly meant something they never reviewed.

THE DESIGN, and the property that makes it structural rather than procedural.

Three env vars, and NO SINGLE ONE OF THEM changes what reaches a client:

  AGENT_CLIENT_DM_AUTOFIX      the lane runs at all. On its own: the lane polls,
                               probes, may run a scoped fix, and writes a card to the
                               fixer channel for a human. It composes NO client reply --
                               not held, not drafted, not written. Mode `escalate_only`.

  AGENT_CLIENT_DM_CLIENT_REPLY the lane may write a client-bound row. On its own,
                               without the master: the lane does not run at all.

  AGENT_CLIENT_DM_LIVE_ACK     the deliberate act. Its value must equal a token this
                               module DERIVES AT RUNTIME, and the token contains the
                               CURRENT value of `SLACK_CONVO_<IDENTITY>_CLIENT_REPLY`.

The third is the load-bearing part, and it is why this is not just "two booleans".
Because the required token is a function of the OTHER flag's live value:

  * it cannot be written in advance of reading what the other flag is set to;
  * if either the identity or the other flag moves, every previously-set ack goes
    STALE and the lane falls back to escalate_only until a human re-affirms;
  * the refusal names, in one line, exactly what was about to go live -- the identity,
    the surface, and the conditions that could have auto-replied.

So there is no environment variable in this system whose value alone puts a NEW
client message onto the wire at WRITE time, and there is no state in which the lane
writes a client-bound row and nobody has read what the other flag is currently doing.

THE HONEST LIMIT OF THAT GUARANTEE, closed by a SEPARATE mechanism (GAP 2, audit of
PR #68). The paragraph above is a claim about lane.py's WRITE path only. It says
nothing about a row already sitting in support_messages from an earlier pass where
this DID go LIVE: revoking AGENT_CLIENT_DM_AUTOFIX afterwards (or letting the derived
AGENT_CLIENT_DM_LIVE_ACK go stale) stops this lane from writing anything NEW, but by
itself it does nothing to a row already written 'ready' -- because the outbox's own
release gate, `_recipient_armed` (outbox.py:141-144), reads only
SLACK_CONVO_<IDENTITY>_CLIENT_REPLY, a different, pre-existing, already-armed flag
that has no knowledge this lane -- or its revocation -- exists at all. So the real
guarantee is: no single flag arms a NEW reply, AND (as of the fix for GAP 2)
outbox._dispatch_one independently re-runs THIS module's own preflight() at dispatch
time for any row carrying this lane's provenance marker, holding it the moment this
lane's own arming no longer says LIVE -- so a revoke also retracts what was already
queued, not only what would have been written next.

WHAT THIS FILE DOES NOT DO. It does not read, write, wrap or alter any SLACK_CONVO_*
flag's semantics, and it does not touch agent/slack_convo/* except for the one
dispatch-time read described above (outbox.py calling this module's own preflight()).
It READS `slack_convo_client_reply_armed` to build its own token and to tell the
operator the truth; the #fixer bus's own auto-answer gate (D67) is a
separately-owned, still-locked decision and is untouched by every line here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

MODE_OFF = "off"
MODE_ESCALATE_ONLY = "escalate_only"
MODE_LIVE = "live"

ENV_MASTER = "AGENT_CLIENT_DM_AUTOFIX"
ENV_CLIENT_REPLY = "AGENT_CLIENT_DM_CLIENT_REPLY"
ENV_LIVE_ACK = "AGENT_CLIENT_DM_LIVE_ACK"


def _env(name, env=None):
    src = os.environ if env is None else env
    return str(src.get(name, "") or "")


def _truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def required_ack(identity, *, env=None, client_reply_armed=None):
    """The exact token that arms live client replies for THIS identity, RIGHT NOW.

    Derived, never stored. It carries the live value of
    SLACK_CONVO_<IDENTITY>_CLIENT_REPLY, so an ack written while that flag said one
    thing does not survive it saying another.
    """
    ident = str(identity or "").strip().lower()
    if client_reply_armed is None:
        from .. import config as _config
        armed = bool(_config.slack_convo_client_reply_armed(ident))
    else:
        armed = bool(client_reply_armed)
    return (f"client-dm-live:{ident}"
            f":slack-convo-client-reply={'on' if armed else 'off'}")


@dataclass(frozen=True)
class Arming:
    mode: str
    identity: str
    may_reply_to_clients: bool
    outbox_gate_open: bool     # what SLACK_CONVO_<ID>_CLIENT_REPLY currently says
    reason: str                # always populated; OFF must be loud, never silent
    banner: str = ""           # printed at boot when something is about to go live
    required_ack: str = ""


def preflight(identity, *, env=None, client_reply_armed=None):
    """Decide the mode, and say out loud what it means. Never raises.

    D68's "the OFF state must be loud, so OFF is distinguishable from BROKEN" applies
    to all three modes here: every return carries a reason naming the flag responsible.
    """
    ident = str(identity or "").strip().lower()
    if client_reply_armed is None:
        from .. import config as _config
        outbox_open = bool(_config.slack_convo_client_reply_armed(ident))
    else:
        outbox_open = bool(client_reply_armed)

    master = _truthy(_env(ENV_MASTER, env))
    want_reply = _truthy(_env(ENV_CLIENT_REPLY, env))
    ack_seen = _env(ENV_LIVE_ACK, env).strip()
    need = required_ack(ident, env=env, client_reply_armed=outbox_open)

    if not master:
        return Arming(
            mode=MODE_OFF, identity=ident, may_reply_to_clients=False,
            outbox_gate_open=outbox_open, required_ack=need,
            reason=(f"{ENV_MASTER} is off: the client-DM support lane is not running. "
                    f"No ticket is read, no fix is attempted, no card is written."),
        )

    if not want_reply:
        return Arming(
            mode=MODE_ESCALATE_ONLY, identity=ident, may_reply_to_clients=False,
            outbox_gate_open=outbox_open, required_ack=need,
            reason=(f"{ENV_CLIENT_REPLY} is off: the lane diagnoses, may run a scoped "
                    f"fix, and writes a card to the fixer channel for a human. It "
                    f"composes no client-facing reply at all."),
        )

    if ack_seen != need:
        # THE REFUSAL. Loud, and it names exactly what was about to go live.
        seen = "unset" if not ack_seen else f"{ack_seen!r} (stale or wrong)"
        banner = (
            f"[client-dm/{ident}] REFUSED to arm live client replies.\n"
            f"  {ENV_MASTER}={master} and {ENV_CLIENT_REPLY}={want_reply} are both on, "
            f"so the next step would have been REAL messages to paying gym owners.\n"
            f"  About to go live: identity {ident!r}; delivery via the existing "
            f"slack_convo outbox into each client's own Slack channel; conditions that "
            f"could auto-reply: {_condition_names()}.\n"
            f"  SLACK_CONVO_{ident.upper()}_CLIENT_REPLY is currently "
            f"{'ON' if outbox_open else 'OFF'} -- with it ON the outbox's own gate is "
            f"already open, so this interlock is the only thing holding.\n"
            f"  {ENV_LIVE_ACK} is {seen}. To arm deliberately, set it to exactly:\n"
            f"      {need}\n"
            f"  That token contains the CURRENT value of "
            f"SLACK_CONVO_{ident.upper()}_CLIENT_REPLY, so it goes stale if that flag "
            f"moves and this lane falls back to escalate-only until a human looks again."
        )
        return Arming(
            mode=MODE_ESCALATE_ONLY, identity=ident, may_reply_to_clients=False,
            outbox_gate_open=outbox_open, required_ack=need, banner=banner,
            reason=(f"{ENV_LIVE_ACK} does not match the token required right now; "
                    f"holding at escalate-only."),
        )

    banner = (
        f"[client-dm/{ident}] LIVE. Client replies from this lane will be DELIVERED.\n"
        f"  identity: {ident}; conditions that may auto-reply: {_condition_names()}.\n"
        f"  SLACK_CONVO_{ident.upper()}_CLIENT_REPLY is "
        f"{'ON' if outbox_open else 'OFF'}"
        + ("." if outbox_open else
           " -- so the outbox will HOLD every row this lane writes and card a human "
           "instead. Nothing reaches a client until that flag is on too.")
        + f"\n  Every auto-reply also writes an internal card carrying the client's own "
          f"words, because a reply is not a claim that the whole message was handled."
    )
    return Arming(
        mode=MODE_LIVE, identity=ident, may_reply_to_clients=True,
        outbox_gate_open=outbox_open, required_ack=need, banner=banner,
        reason=f"{ENV_MASTER}, {ENV_CLIENT_REPLY} and a matching {ENV_LIVE_ACK} are set.",
    )


def _condition_names():
    from . import conditions as _c
    return ", ".join(sorted(_c.CONDITIONS)) or "(none)"
