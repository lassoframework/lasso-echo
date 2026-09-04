"""
echo_ticket_wiring.py -- D46: the real-service glue for echo_ticket_worker.py.

echo_ticket_worker.py is pure and fully dependency-injected (matching every other module
in agent/slack_convo/); this is the one file that actually imports a live Slack client
and the live bus, mirroring agent/slack_convo/listener_wiring.py's own split between
"pure logic" and "the wiring that touches real services". Nothing here is imported at
module scope by anything that runs with the config flag off.
"""
import json
from dataclasses import dataclass

from . import config
from .slack_convo import adapter as _a
from .slack_convo import answer_lane as _al
from .slack_convo import identities as _ids
from .slack_convo.bus import Bus
from .slack_surface import SlackPoster


def _slack_lookup_email_factory(poster):
    def lookup(email):
        resp = poster._send(
            "https://slack.com/api/users.lookupByEmail?email=" + email, {})
        return (resp or {}).get("user", {}).get("id") if (resp or {}).get("ok") else None
    return lookup


def _open_group_dm_factory(poster):
    def open_group_dm(user_ids):
        resp = poster._send("https://slack.com/api/conversations.open",
                            {"users": ",".join(user_ids)})
        if not (resp or {}).get("ok"):
            return {"ok": False}
        return {"ok": True, "channel_id": (resp.get("channel") or {}).get("id") or ""}
    return open_group_dm


def _post_first_message_factory(poster):
    def post_first_message(channel_id, text):
        res = poster._chat_post(text=text, blocks=None, channel=channel_id)
        if not (res or {}).get("ok"):
            return {"ok": False}
        return {"ok": True, "ts": res.get("ts") or ""}
    return post_first_message


def _account_key_for_gym_factory(bus):
    def account_key_for_gym(gym_id):
        rows = bus._get("echo_intake_tokens", {"gym_id": f"eq.{gym_id}",
                                               "select": "echo_account_key",
                                               "limit": "1"})
        return (rows[0].get("echo_account_key") if rows else "") or ""
    return account_key_for_gym


def _write_hold_notice_factory(bus):
    def write_hold_notice(**kwargs):
        return _a.write_hold_notice(bus, **kwargs)
    return write_hold_notice


def _stamp_ticket_factory(bus):
    def stamp_ticket(ticket_id, *, channel_id, thread_ts, slack_user_id, bot_identity,
                     identity_kind):
        return bus.set_ticket(ticket_id, slack_channel_id=channel_id,
                              slack_thread_ts=thread_ts, slack_user_id=slack_user_id,
                              bot_identity=bot_identity, identity_kind=identity_kind)
    return stamp_ticket


@dataclass
class Deps:
    bus: object
    intake_kwargs: dict
    fixed_kwargs: dict


def live_deps(*, product=None, source=None, identity_name="echo", bus=None, log=print):
    """Everything echo_ticket_worker.intake_pass()/fixed_pass() need for the given
    (product, identity) pair, wired to real services: that identity's OWN bot token
    (never Blake's, matching every other outbound path in this system, and never a
    different identity's token -- D47 generalized this from Echo-only to any
    registered identity via identities.py's own bot_token_env, the same lookup
    listener_wiring.py's live_deps() already uses), the real bus, the real
    answer-lane grounding + LLM. product/source default to None so the worker's own
    PRODUCT/SOURCE defaults apply when this is called for Echo (unchanged call site);
    a caller wiring the portal->scout pass passes both explicitly."""
    bus = bus or Bus()
    ident = _ids.IDENTITIES[identity_name]
    token = ident.env(ident.bot_token_env)
    poster = SlackPoster(token=token)

    routing_kwargs = {"identity_name": identity_name}
    if product is not None:
        routing_kwargs["product"] = product
    if source is not None:
        routing_kwargs["source"] = source

    shared = dict(
        open_group_dm=_open_group_dm_factory(poster),
        post_first_message=_post_first_message_factory(poster),
        mark_message=bus.mark_message,
        claim_message=bus.claim_message,
        stamp_ticket=_stamp_ticket_factory(bus),
        log=log,
    )
    intake_kwargs = dict(shared)
    intake_kwargs.update(routing_kwargs)
    intake_kwargs.update(
        slack_lookup_email=_slack_lookup_email_factory(poster),
        account_key_for_gym=_account_key_for_gym_factory(bus),
        write_hold_notice=_write_hold_notice_factory(bus),
        fetch_state=_al.default_fetch_state,
        llm=_al.default_llm,
    )
    fixed_kwargs = dict(shared)
    fixed_kwargs.update({k: v for k, v in routing_kwargs.items() if k != "source"})
    return Deps(bus=bus, intake_kwargs=intake_kwargs, fixed_kwargs=fixed_kwargs)
