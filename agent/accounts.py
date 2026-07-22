"""
Account registry.

An Account knows WHICH env var holds its token, never the token itself. The
token is read lazily, used, and discarded. It is never stored on the object,
never returned in a repr, never written to a log.

Stage 1 ships the LASSO accounts. Two are active (lasso_ig, lasso_fb); blake_personal
is kept as an INACTIVE record (personal-profile publishing ended in 2018). Edit
ACCOUNTS or override the target ids via env. Tokens are set by Blake's own hand.
"""

import os
from dataclasses import dataclass, field

from .trust import TrustLevel, default_trust_for_new_account


class Platform:
    INSTAGRAM = "instagram"            # IG Business/Creator via Instagram Graph API
    FACEBOOK_PAGE = "facebook_page"    # a Facebook Page
    PERSONAL = "personal"              # see README: Graph API cannot post to a personal FB profile
    GOOGLE_BUSINESS = "google_business"  # Google Business Profile local posts (own draft-only branch)


@dataclass
class Account:
    key: str                  # stable id, e.g. "lasso_ig"
    display_name: str         # human label for Slack cards
    platform: str             # one of Platform.*
    token_env: str            # NAME of the env var holding this account's token
    target_id_env: str        # NAME of the env var holding the IG user id / Page id
    trust: TrustLevel = field(default_factory=default_trust_for_new_account)
    active: bool = True       # inactive accounts stay in the registry (history) but never draft/publish
    # ---- Multi-client fields (Stage 2). Empty = fall back to the global config, so
    # LASSO's accounts (client zero) behave exactly as before. A client account sets
    # its own paths/channel and can NEVER cross-read another client's docs or library.
    voice_doc: str = ""           # per-client brand bible path
    social_proof_doc: str = ""    # per-client verified social proof path
    library_prefix: str = ""      # per-client content library directory
    slack_channel: str = ""       # per-client approval channel id
    approvers: list = field(default_factory=list)  # per-client approver Slack ids
    # Day 30 narrative framing: "frequency" leads with the posting cadence
    # story (before vs after); "engagement" NEVER ships a frequency comparison
    # (it may appear only in an internal do not publish appendix). Empty falls
    # back to "engagement", the safe framing.
    report_framing: str = ""

    def get_token(self):
        """Read the token at call time. Never logged, never surfaced.
        Order: the hand-set env var ALWAYS WINS when present; with
        AGENT_CONNECT_TOKENS_ENABLED armed, a /connect-stored kv page token
        (keyed by this account's page id) is the fallback. Flag OFF = env only,
        byte-identical to before."""
        env_token = os.environ.get(self.token_env)
        if env_token:
            return env_token
        from . import config as _config
        if _config.connect_tokens_enabled():
            page_id = self.get_target_id()
            if page_id:
                from . import db as _db
                kv_token = _db.kv_get(f"connect_page_token_{page_id}", "")
                if kv_token:
                    return kv_token
        return env_token

    def get_target_id(self):
        return os.environ.get(self.target_id_env)

    # ---- Config resolvers: the account's own value, else the global (client zero)
    # config. Every consumer resolves through these so isolation is by construction.
    @property
    def trust_level(self):
        """The account's trust rung (default: full approval, the Stage 1 gate)."""
        return self.trust

    def voice_doc_path(self):
        from . import config
        return self.voice_doc or config.VOICE_DOC_PATH

    def social_proof_doc_path(self):
        from . import config
        return self.social_proof_doc or config.SOCIAL_PROOF_PATH

    def library_path(self):
        from . import config
        return self.library_prefix or config.LIBRARY_PATH

    def approval_channel(self):
        from . import config
        return self.slack_channel or config.SLACK_CHANNEL_ID

    def approver_ids(self):
        from . import config
        return list(self.approvers) or [config.APPROVER_SLACK_ID]

    def __repr__(self):
        # Deliberately omits any secret. Safe to log.
        return f"<Account {self.key} platform={self.platform} trust={self.trust.name}>"


# Stage 1 LASSO accounts. token/id values come from env, set by hand.
ACCOUNTS = [
    Account(
        key="lasso_ig",
        display_name="LASSO Instagram",
        platform=Platform.INSTAGRAM,
        token_env="AGENT_LASSO_IG_TOKEN",
        target_id_env="AGENT_LASSO_IG_USER_ID",
        # IG regressed on posts per week: the Day 30 story is engagement per
        # post and consistency; a frequency comparison NEVER ships for IG.
        report_framing="engagement",
    ),
    Account(
        key="lasso_fb",
        display_name="LASSO Facebook Page",
        platform=Platform.FACEBOOK_PAGE,
        token_env="AGENT_LASSO_FB_TOKEN",
        target_id_env="AGENT_LASSO_FB_PAGE_ID",
        # FB went from ~0.25 posts per week to daily: the frequency before vs
        # after story IS the headline for this account.
        report_framing="frequency",
    ),
    Account(
        key="district_h_ig",
        display_name="District H Strength and Fitness IG",
        platform=Platform.INSTAGRAM,
        token_env="AGENT_DISTRICT_H_IG_TOKEN",
        target_id_env="AGENT_DISTRICT_H_IG_ID",
        voice_doc="brand_voice/district_h/lasso_voice.md",
        social_proof_doc="brand_voice/district_h/social_proof.md",
        library_prefix="content_library/district_h",
        slack_channel="",            # the client's approval channel id, by hand
        approvers=[],                # approver Slack ids, by hand
        active=False,                # arm after tokens + voice doc are filled
        # trust defaults to FULL_APPROVAL (level 0). Do not change here.
    ),
    # Kept as an INACTIVE record for history. Meta ended personal-profile publishing
    # in 2018 (Graph API cannot post to a personal profile), so this account can never
    # publish and must not generate daily draft cards. active=False excludes it from
    # active_accounts() while leaving it discoverable via get_account().
    Account(
        key="blake_personal",
        display_name="Blake Personal",
        platform=Platform.PERSONAL,
        token_env="AGENT_BLAKE_PERSONAL_TOKEN",
        target_id_env="AGENT_BLAKE_PERSONAL_ID",
        active=False,
    ),
]


def active_accounts():
    """The accounts the daily runner drafts for: active only (inactive records skipped)."""
    return [a for a in ACCOUNTS if a.active]


def get_account(key):
    for a in ACCOUNTS:
        if a.key == key:
            return a
    return None
