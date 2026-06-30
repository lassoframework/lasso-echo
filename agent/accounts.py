"""
Account registry.

An Account knows WHICH env var holds its token, never the token itself. The
token is read lazily, used, and discarded. It is never stored on the object,
never returned in a repr, never written to a log.

Stage 1 ships three LASSO accounts. Edit ACCOUNTS or override the target ids via
env. Tokens are set by Blake's own hand (see AGENT_README.md).
"""

import os
from dataclasses import dataclass, field

from .trust import TrustLevel, default_trust_for_new_account


class Platform:
    INSTAGRAM = "instagram"            # IG Business/Creator via Instagram Graph API
    FACEBOOK_PAGE = "facebook_page"    # a Facebook Page
    PERSONAL = "personal"              # see README: Graph API cannot post to a personal FB profile


@dataclass
class Account:
    key: str                  # stable id, e.g. "lasso_ig"
    display_name: str         # human label for Slack cards
    platform: str             # one of Platform.*
    token_env: str            # NAME of the env var holding this account's token
    target_id_env: str        # NAME of the env var holding the IG user id / Page id
    trust: TrustLevel = field(default_factory=default_trust_for_new_account)

    def get_token(self):
        """Read the token from env at call time. Returns None if unset. Never logged."""
        return os.environ.get(self.token_env)

    def get_target_id(self):
        return os.environ.get(self.target_id_env)

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
    ),
    Account(
        key="lasso_fb",
        display_name="LASSO Facebook Page",
        platform=Platform.FACEBOOK_PAGE,
        token_env="AGENT_LASSO_FB_TOKEN",
        target_id_env="AGENT_LASSO_FB_PAGE_ID",
    ),
    Account(
        key="blake_personal",
        display_name="Blake Personal",
        platform=Platform.PERSONAL,
        token_env="AGENT_BLAKE_PERSONAL_TOKEN",
        target_id_env="AGENT_BLAKE_PERSONAL_ID",
    ),
]


def active_accounts():
    return list(ACCOUNTS)


def get_account(key):
    for a in ACCOUNTS:
        if a.key == key:
            return a
    return None
