"""
identities.py — the per-bot config registry. Onboarding a bot is a config entry, not code.

Blake (spec item 7): "bot identity is config. product, worker, allowed lanes, reply voice
doc, and per-identity flags live in one config per bot. Adding Ranger or Lainey is a config
entry, not code. Echo ships first. Others ship with their config present and flags OFF."

Every identity below is fully described. The listener starts one Bolt App per identity
whose tokens are present; an identity with no tokens is simply not started (and says so
once at boot). So arming an identity here is: set its two token env vars, flip its
per-identity flag. No code change.

Ground truth (2026-09-05, Phase 4 arming): Echo, Scout, Ranger, and Wrangler all now have
live tokens in this Railway service's environment and run as separate Bolt Apps in the
SAME process (agent/slack_convo/listener_wiring.py's start_additional_identities()), each
with its own bot token, channel wiring, and SLACK_CONVO_<IDENTITY>_* flags -- all four
are armed (ENABLED + STAFF_REPLY + CLIENT_REPLY). Only Lainey (lasso-engage, an SMS/voice
persona) remains unarmed and has no Slack surface today; its row here is ready for when
it gets one. Wrangler's manifest gained im:write and mpim:write on 2026-09-05 (so it CAN proactively
open a DM/MPIM once outreach.py's ticket-initiated group-DM path is ever wired to it --
that path is not wired today, so this is unused capacity, not live behavior) -- a
reinstall is required before the live bot token actually carries the new scopes; until
then it still only replies in a channel it can join or a DM/MPIM a human already opened,
which is every path this system's own client-reply flow actually uses today.
channels:write/groups:write were not requested and remain absent.

Token env NAMES live here; values are set by hand in the service environment and are
never read into any object, never logged.
"""
from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class BotIdentity:
    name: str                      # 'echo' | 'ranger' | 'scout' | 'wrangler' | 'lainey'
    product: str                   # support_tickets.product value
    bot_token_env: str             # xoxb- token env NAME
    app_token_env: str             # xapp- (Socket Mode) token env NAME
    bot_user_id_env: str           # the bot's own Slack user id env NAME (to ignore self)
    allowed_lanes: tuple = ("hold",)   # lanes this identity's tickets may take
    default_lane: str = "hold"
    reply_voice_doc: str = ""      # path, relative to the repo, of the reply voice rules
    fixer_channel_env: str = "AGENT_FIXER_CHANNEL_ID"  # where holds / escalations go.
    # RA-m5 (2026-09-03 re-audit): every identity used to fall through to this SAME default,
    # so a second identity's holds landed in Echo's channel with no way to tell them apart.
    # Each non-Echo identity below overrides it with its own env name; Echo keeps the
    # default (it is the one already deployed).
    # Defaults for the flag NAMES; the values are read live by config.slack_convo_*.
    flag_suffix: str = ""          # computed from name when empty

    def env(self, key_env: str) -> str:
        return os.environ.get(key_env, "") or ""

    @property
    def suffix(self) -> str:
        return (self.flag_suffix or self.name).upper()

    def tokens_present(self) -> bool:
        return bool(self.env(self.bot_token_env) and self.env(self.app_token_env))

    def bot_user_id(self) -> str:
        return self.env(self.bot_user_id_env).strip()

    def fixer_channel(self) -> str:
        return self.env(self.fixer_channel_env).strip()


# The registry. Order is the arming order Blake asked for in DONE: Echo first.
IDENTITIES = {
    "echo": BotIdentity(
        name="echo", product="echo",
        bot_token_env="AGENT_SLACK_BOT_TOKEN",
        app_token_env="AGENT_SLACK_APP_TOKEN",
        bot_user_id_env="AGENT_SLACK_BOT_USER_ID",
        allowed_lanes=("safe", "hold"), default_lane="hold",
        reply_voice_doc="docs/slack_convo/echo_reply_voice.md",
    ),
    "ranger": BotIdentity(
        name="ranger", product="ranger",
        bot_token_env="RANGER_SLACK_BOT_TOKEN",
        app_token_env="RANGER_SLACK_APP_TOKEN",
        bot_user_id_env="RANGER_SLACK_BOT_USER_ID",
        allowed_lanes=("hold",), default_lane="hold",
        reply_voice_doc="docs/slack_convo/ranger_reply_voice.md",
        fixer_channel_env="RANGER_FIXER_CHANNEL_ID",
    ),
    "scout": BotIdentity(
        name="scout", product="scout",
        bot_token_env="SCOUT_SLACK_BOT_TOKEN",
        app_token_env="SCOUT_SLACK_APP_TOKEN",
        bot_user_id_env="SCOUT_SLACK_BOT_USER_ID",
        allowed_lanes=("hold",), default_lane="hold",
        reply_voice_doc="docs/slack_convo/scout_reply_voice.md",
        fixer_channel_env="SCOUT_FIXER_CHANNEL_ID",
    ),
    "wrangler": BotIdentity(
        name="wrangler", product="websites",
        bot_token_env="WRANGLER_SLACK_BOT_TOKEN",
        app_token_env="WRANGLER_SLACK_APP_TOKEN",
        bot_user_id_env="WRANGLER_SLACK_BOT_USER_ID",
        allowed_lanes=("hold",), default_lane="hold",
        reply_voice_doc="docs/slack_convo/wrangler_reply_voice.md",
        fixer_channel_env="WRANGLER_FIXER_CHANNEL_ID",
    ),
    "lainey": BotIdentity(
        name="lainey", product="lainey",
        bot_token_env="LAINEY_SLACK_BOT_TOKEN",
        app_token_env="LAINEY_SLACK_APP_TOKEN",
        bot_user_id_env="LAINEY_SLACK_BOT_USER_ID",
        allowed_lanes=("hold",), default_lane="hold",
        reply_voice_doc="docs/slack_convo/lainey_reply_voice.md",
        fixer_channel_env="LAINEY_FIXER_CHANNEL_ID",
    ),
}

ARMING_ORDER = ("echo", "ranger", "wrangler", "scout", "lainey")


def get(name: str) -> BotIdentity:
    key = (name or "").strip().lower()
    if key not in IDENTITIES:
        raise KeyError(f"unknown bot identity {name!r}; known: {', '.join(IDENTITIES)}")
    return IDENTITIES[key]


def startable() -> list:
    """Identities whose tokens are present in this environment, in arming order."""
    return [IDENTITIES[n] for n in ARMING_ORDER if IDENTITIES[n].tokens_present()]
