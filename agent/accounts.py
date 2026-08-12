"""
Account registry.

An Account knows WHICH env var holds its token, never the token itself. The
token is read lazily, used, and discarded. It is never stored on the object,
never returned in a repr, never written to a log.

Stage 1 ships the LASSO accounts. Two are active (lasso_ig, lasso_fb); blake_personal
is kept as an INACTIVE record (personal-profile publishing ended in 2018). Edit
ACCOUNTS or override the target ids via env. Tokens are set by Blake's own hand.
"""

import json
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
    # Publish routing: which publish lane this account's approved posts go out
    # through. "meta_direct" (default) is the Meta Graph path Echo has always
    # used; "socialapi" routes through the SocialAPI.ai lane, but ONLY when
    # AGENT_SOCIALAPI_ENABLED is also armed. LASSO's own accounts stay
    # meta_direct. Changing this flips the publish step ONLY: drafting,
    # approvals, calendar, trust ladder are all identical either way.
    publish_route: str = "meta_direct"

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
    # CrossFit and HYROX ENG (Dale Suslick, Cape Coral FL). Onboarded on the ad-ops
    # portal since 2026-06 AND filled out the DFY social intake 2026-08-09, but that
    # intake was captured with echo_forwarded=false / echo_status=not_routed, so it
    # never reached Echo (no account, no voice doc). These two entries close that gap.
    # Voice doc + social proof are built from ENG's OWN verbatim intake. Still inactive:
    # arm only after the IG/FB tokens + ids are set BY HAND in Railway env under the
    # names below, and Dale confirms the voice doc. IG is the generation account; FB
    # mirrors. base tenant key "eng" = the library prefix content_library/eng.
    Account(
        key="eng_ig",
        display_name="CrossFit and HYROX ENG IG",
        platform=Platform.INSTAGRAM,
        token_env="AGENT_ENG_IG_TOKEN",
        target_id_env="AGENT_ENG_IG_ID",
        voice_doc="brand_voice/eng/lasso_voice.md",
        social_proof_doc="brand_voice/eng/social_proof.md",
        library_prefix="content_library/eng",
        slack_channel="",            # the client's approval channel id, by hand
        approvers=[],                # approver Slack ids, by hand
        active=False,                # Client gyms stay active=False (like gritx/topfuel):
                                     # they post via the CLIENT path (draft-on-upload +
                                     # portal/client-month), NOT LASSO's daily run. ARMED
                                     # for ENG 2026-08-12 via AGENT_DRAFT_ON_UPLOAD; publish
                                     # needs AGENT_ENG_IG_TOKEN/ID + AGENT_PUBLISH_ENABLED.
        # trust defaults to FULL_APPROVAL (level 0). Do not change here.
    ),
    Account(
        key="eng_fb",
        display_name="CrossFit and HYROX ENG Facebook Page",
        platform=Platform.FACEBOOK_PAGE,
        token_env="AGENT_ENG_FB_TOKEN",
        target_id_env="AGENT_ENG_FB_PAGE_ID",
        voice_doc="brand_voice/eng/lasso_voice.md",
        social_proof_doc="brand_voice/eng/social_proof.md",
        library_prefix="content_library/eng",
        slack_channel="",            # the client's approval channel id, by hand
        approvers=[],                # approver Slack ids, by hand
        active=False,                # Client gym: posts via the client path, not LASSO's
                                     # daily run. Publish needs AGENT_ENG_FB_TOKEN/PAGE_ID
                                     # + AGENT_PUBLISH_ENABLED.
        # trust defaults to FULL_APPROVAL (level 0). Do not change here.
    ),
    # ---- CLIENT gyms (social-intake onboarded). These draft the month from their OWN
    # uploaded photos/videos paired with their OWN approved sources (client_month_run),
    # behind AGENT_CLIENT_MONTH + AGENT_CLIENT_SOURCES. A gym with no uploaded media gets
    # no calendar (Echo waits; the portal shows a red "upload your media" banner) and is
    # NEVER given an infographic-only calendar. Three keys per gym: the tenant
    # base ("gritx") is echo_social_intake.client_key AND content_calendar.gym_id; the
    # _ig account is the generation key; the _fb account is the Facebook mirror. All
    # active=False so the daily runner never auto-drafts/publishes them. Tokens + voice
    # docs are filled by hand before arming.
    Account(
        key="gritx_ig",
        display_name="GritX IG",
        platform=Platform.INSTAGRAM,
        token_env="AGENT_GRITX_IG_TOKEN",
        target_id_env="AGENT_GRITX_IG_ID",
        voice_doc="brand_voice/gritx/lasso_voice.md",
        social_proof_doc="brand_voice/gritx/social_proof.md",
        library_prefix="content_library/gritx",
        active=False,
    ),
    Account(
        key="gritx_fb",
        display_name="GritX Facebook Page",
        platform=Platform.FACEBOOK_PAGE,
        token_env="AGENT_GRITX_FB_TOKEN",
        target_id_env="AGENT_GRITX_FB_PAGE_ID",
        voice_doc="brand_voice/gritx/lasso_voice.md",
        social_proof_doc="brand_voice/gritx/social_proof.md",
        library_prefix="content_library/gritx",
        active=False,
    ),
    Account(
        key="topfuel_ig",
        display_name="Top Fuel IG",
        platform=Platform.INSTAGRAM,
        token_env="AGENT_TOPFUEL_IG_TOKEN",
        target_id_env="AGENT_TOPFUEL_IG_ID",
        voice_doc="brand_voice/topfuel/lasso_voice.md",
        social_proof_doc="brand_voice/topfuel/social_proof.md",
        library_prefix="content_library/topfuel",
        active=False,
    ),
    Account(
        key="topfuel_fb",
        display_name="Top Fuel Facebook Page",
        platform=Platform.FACEBOOK_PAGE,
        token_env="AGENT_TOPFUEL_FB_TOKEN",
        target_id_env="AGENT_TOPFUEL_FB_PAGE_ID",
        voice_doc="brand_voice/topfuel/lasso_voice.md",
        social_proof_doc="brand_voice/topfuel/social_proof.md",
        library_prefix="content_library/topfuel",
        active=False,
    ),
    Account(
        key="eng_ig",
        display_name="CrossFit and HYROX ENG IG",
        platform=Platform.INSTAGRAM,
        token_env="AGENT_ENG_IG_TOKEN",
        target_id_env="AGENT_ENG_IG_ID",
        voice_doc="brand_voice/eng/lasso_voice.md",
        social_proof_doc="brand_voice/eng/social_proof.md",
        library_prefix="content_library/eng",
        active=False,
    ),
    Account(
        key="eng_fb",
        display_name="CrossFit and HYROX ENG Facebook Page",
        platform=Platform.FACEBOOK_PAGE,
        token_env="AGENT_ENG_FB_TOKEN",
        target_id_env="AGENT_ENG_FB_PAGE_ID",
        voice_doc="brand_voice/eng/lasso_voice.md",
        social_proof_doc="brand_voice/eng/social_proof.md",
        library_prefix="content_library/eng",
        active=False,
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


# ---- Dynamic client-account registry (AGENT_DYNAMIC_ACCOUNTS, default OFF) ---------
# Scales onboarding to 100+ gyms without hand-editing this file: client-gym Account
# records are loaded from a persisted JSON registry and MERGED with the hardcoded
# ACCOUNTS above. Flag OFF -> the registry is never read, so behavior is byte-for-byte
# today's. Auto-created accounts are ALWAYS inactive (client gyms post via the client /
# draft-on-upload path, not LASSO's daily run); tokens stay by-hand in env; nothing
# publishes. Hardcoded ACCOUNTS always WIN on a key collision (never shadowed).
_dynamic_cache = None  # list[Account] | None; None = not yet loaded this process


def _account_from_registry_row(row):
    """Build one INACTIVE client Account from a registry row. Standard conventions:
    key <base>_ig / <base>_fb, token/id env names AGENT_<BASE>_IG_TOKEN etc., voice
    doc + library under brand_voice/<base>/ and content_library/<base>."""
    base = (row.get("base") or "").strip()
    if not base:
        return []
    name = (row.get("name") or base).strip()
    up = base.upper()
    return [
        Account(
            key=f"{base}_ig", display_name=f"{name} IG",
            platform=Platform.INSTAGRAM,
            token_env=f"AGENT_{up}_IG_TOKEN", target_id_env=f"AGENT_{up}_IG_ID",
            voice_doc=f"brand_voice/{base}/lasso_voice.md",
            social_proof_doc=f"brand_voice/{base}/social_proof.md",
            library_prefix=f"content_library/{base}",
            active=False,
        ),
        Account(
            key=f"{base}_fb", display_name=f"{name} Facebook Page",
            platform=Platform.FACEBOOK_PAGE,
            token_env=f"AGENT_{up}_FB_TOKEN", target_id_env=f"AGENT_{up}_FB_PAGE_ID",
            voice_doc=f"brand_voice/{base}/lasso_voice.md",
            social_proof_doc=f"brand_voice/{base}/social_proof.md",
            library_prefix=f"content_library/{base}",
            active=False,
        ),
    ]


def _load_registry_rows():
    from . import config
    path = config.gym_registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _dynamic_accounts():
    """Dynamic client accounts, cached per process. Empty when the flag is OFF.
    The cache is keyed on the registry path so a runtime path change never serves
    stale rows from a different registry (defense in depth; single-registry in prod)."""
    global _dynamic_cache
    from . import config
    if not config.dynamic_accounts_enabled():
        return []
    path = config.gym_registry_path()
    if _dynamic_cache is None or _dynamic_cache[0] != path:
        hardcoded = {a.key for a in ACCOUNTS}
        out = []
        for row in _load_registry_rows():
            for acct in _account_from_registry_row(row):
                if acct.key not in hardcoded:   # hardcoded always wins
                    out.append(acct)
        _dynamic_cache = (path, out)
    return _dynamic_cache[1]


def all_accounts():
    """Hardcoded ACCOUNTS + dynamic client accounts (when armed)."""
    return list(ACCOUNTS) + _dynamic_accounts()


def register_gym(base, *, name="", ig_handle="", fb_page=""):
    """Persist one client gym to the dynamic registry so its Account records resolve
    without hand-editing accounts.py. Idempotent (a re-register updates in place).
    No-op returning [] when AGENT_DYNAMIC_ACCOUNTS is OFF. Returns the account keys
    now resolvable for this gym. Tokens are NEVER written here (env, by hand)."""
    global _dynamic_cache
    from . import config
    base = (base or "").strip()
    if not base or not config.dynamic_accounts_enabled():
        return []
    path = config.gym_registry_path()
    rows = _load_registry_rows()
    row = {"base": base, "name": (name or base).strip(),
           "ig_handle": (ig_handle or "").strip(), "fb_page": (fb_page or "").strip()}
    rows = [r for r in rows if (r.get("base") or "").strip() != base]
    rows.append(row)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)
    _dynamic_cache = None   # invalidate so the new gym resolves immediately
    return [f"{base}_ig", f"{base}_fb"]


def active_accounts():
    """The accounts the daily runner drafts for: active only (inactive records skipped)."""
    return [a for a in all_accounts() if a.active]


def get_account(key):
    for a in all_accounts():
        if a.key == key:
            return a
    return None
