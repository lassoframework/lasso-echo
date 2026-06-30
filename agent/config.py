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

# Append-only log of every post we publish (or "would publish" in draft-only).
POST_LOG_PATH = os.environ.get("AGENT_POST_LOG_PATH", "post_log.jsonl")

# ---- Slack control surface ---------------------------------------------------
SLACK_BOT_TOKEN_ENV = "AGENT_SLACK_BOT_TOKEN"  # name of the env var, not the value
SLACK_CHANNEL_ID = os.environ.get("AGENT_SLACK_CHANNEL_ID", "")

# ---- Meta Graph API ----------------------------------------------------------
GRAPH_API_VERSION = os.environ.get("AGENT_GRAPH_API_VERSION", "v21.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


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
