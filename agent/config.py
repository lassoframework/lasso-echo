"""
Central config: feature flags, the approver gate, and file paths.

Two hard rules live here:
  1. The master flag and the publish flag BOTH default OFF. Nothing runs and
     nothing publishes unless Blake arms it by hand via an environment variable.
  2. Tokens are NEVER read in this file. Tokens live in env and are read lazily
     in accounts.py, never logged, never stored on an object.
"""

import os

# ---- Approver gate -----------------------------------------------------------
# Only this Slack user can approve a post. Overridable by env, defaults to Blake.
APPROVER_SLACK_ID = os.environ.get("AGENT_APPROVER_SLACK_ID", "U06EPUUCL13")

# ---- Paths -------------------------------------------------------------------
# The brand voice doc is the ONLY source of voice + approved claims. If it is
# missing, the agent drafts nothing (see voice.py + drafter.py).
VOICE_DOC_PATH = os.environ.get("AGENT_VOICE_DOC_PATH", "brand_voice/lasso_voice.md")

# Stage 1 content library is a local folder. Portal wiring comes later (stubs.py).
LIBRARY_PATH = os.environ.get("AGENT_LIBRARY_PATH", "content_library")

# The approved "LASSO Now" source doc: the ONLY well of story, pillars, copy bank,
# CTAs, and hashtags the daily content brain may draw from. Missing -> brain blocks.
SOURCE_DOC_PATH = os.environ.get("AGENT_SOURCE_DOC_PATH", "brand_voice/lasso_now.md")

# Append-only log of every post we publish (or "would publish" in draft-only).
POST_LOG_PATH = os.environ.get("AGENT_POST_LOG_PATH", "post_log.jsonl")

# ---- Slack control surface ---------------------------------------------------
SLACK_BOT_TOKEN_ENV = "AGENT_SLACK_BOT_TOKEN"  # name of the env var, not the value
SLACK_CHANNEL_ID = os.environ.get("AGENT_SLACK_CHANNEL_ID", "")

# ---- Meta Graph API ----------------------------------------------------------
GRAPH_API_VERSION = os.environ.get("AGENT_GRAPH_API_VERSION", "v21.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# ---- Creative studio (Nano Banana infographic generation) --------------------
# OFF by default. The API key is read lazily in creative_studio.py (like tokens),
# never stored on an object and never logged. Only the env var NAME lives here.
NANO_API_KEY_ENV = "AGENT_NANO_API_KEY"  # name of the env var, not the value
NANO_MODEL = os.environ.get("AGENT_NANO_MODEL", "gemini-3-pro-image")

# ---- Media hosting (S3-compatible; scale-hardened for 200+ clients) ----------
# OFF by default. Credentials are read lazily in media_host.py by the env var NAMES
# below, never stored here and never logged. Only NAMES live here, not values.
S3_ENDPOINT = os.environ.get("AGENT_S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("AGENT_S3_BUCKET", "")
S3_REGION = os.environ.get("AGENT_S3_REGION", "")
S3_PUBLIC_BASE_URL = os.environ.get("AGENT_S3_PUBLIC_BASE_URL", "")
S3_MAX_RETRIES = int(os.environ.get("AGENT_S3_MAX_RETRIES", "3"))
S3_ACCESS_KEY_ID_ENV = "AGENT_S3_ACCESS_KEY_ID"          # name of the env var, not the value
S3_SECRET_ACCESS_KEY_ENV = "AGENT_S3_SECRET_ACCESS_KEY"  # name of the env var, not the value

# ---- Google Business Profile (local posts) -----------------------------------
# OFF by default. Real writes ALSO require publish_enabled() (the publish flag governs
# every real write). The access token is read lazily by NAME below, never logged.
GBP_API_BASE = os.environ.get("AGENT_GBP_API_BASE", "https://mybusiness.googleapis.com/v4")
GBP_ACCOUNT_ID = os.environ.get("AGENT_GBP_ACCOUNT_ID", "")
GBP_LOCATION_ID = os.environ.get("AGENT_GBP_LOCATION_ID", "")
GBP_TOKEN_ENV = "AGENT_GBP_ACCESS_TOKEN"  # name of the env var, not the value
GBP_CTA_TYPES = ("LEARN_MORE", "BOOK", "ORDER", "SHOP", "SIGN_UP", "CALL")
GBP_DEFAULT_CTA = os.environ.get("AGENT_GBP_DEFAULT_CTA", "LEARN_MORE")
GBP_SUMMARY_LIMIT = 1500
# The url the GBP call-to-action button points at (booking/site link). Empty -> no
# button is attached (except CALL, which needs no url). Set by hand when armed.
GBP_CTA_URL = os.environ.get("AGENT_GBP_CTA_URL", "")


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def master_enabled() -> bool:
    """Master kill switch. OFF by default. If OFF, the agent does nothing."""
    return _truthy(os.environ.get("AGENT_ENABLED", "false"))


def publish_enabled() -> bool:
    """
    Real publishing switch. OFF by default = DRAFT-ONLY mode.
    When OFF, Approve only logs 'would publish' and never writes to Meta.
    Blake arms this by hand once the drafts look right.
    """
    return _truthy(os.environ.get("AGENT_PUBLISH_ENABLED", "false"))


def creative_studio_enabled() -> bool:
    """
    Nano Banana image generation switch. OFF by default. When OFF, generate()
    returns None and makes NO API call. Independent of publishing; this only
    controls whether Echo draws an infographic, never whether it posts.
    """
    return _truthy(os.environ.get("AGENT_NANO_ENABLED", "false"))


def hosting_enabled() -> bool:
    """
    S3-compatible media hosting switch. OFF by default. When OFF, host_media()
    returns None and the draft build keeps its current behavior. Independent of
    publishing; this only controls whether Echo uploads creatives for public URLs.
    """
    return _truthy(os.environ.get("AGENT_HOSTING_ENABLED", "false"))


def content_brain_enabled() -> bool:
    """
    Daily content brain switch. OFF by default. When OFF (or for a non-LASSO
    account) the drafter keeps its current per-creative behavior. When ON for a
    LASSO account, the caption is composed ONLY from the approved source doc; a
    missing doc or pillar BLOCKS the draft. Independent of publishing.
    """
    return _truthy(os.environ.get("AGENT_CONTENT_BRAIN_ENABLED", "false"))


def gbp_enabled() -> bool:
    """
    Google Business Profile posting branch switch. OFF by default. When OFF (or when
    the publish flag is OFF) gbp_publisher.publish() makes NO network call and returns
    a would_publish result. Independent of the Meta path.
    """
    return _truthy(os.environ.get("AGENT_GBP_ENABLED", "false"))


def reporting_enabled() -> bool:
    """
    30-day reporting switch. OFF by default. When OFF, fetch_insights() returns None
    and reads nothing. Reporting is READ-ONLY: it never posts, edits, or sends.
    """
    return _truthy(os.environ.get("AGENT_REPORTING_ENABLED", "false"))


def comments_enabled() -> bool:
    """
    Comment/DM handling switch. OFF by default. Nothing here ever auto-sends: replies
    are drafted and HELD for human approval; a first-contact DM is always surfaced.
    """
    return _truthy(os.environ.get("AGENT_COMMENTS_ENABLED", "false"))
