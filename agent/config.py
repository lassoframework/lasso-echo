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

# Social proof source: verified quotes/stats WITH permission, beside the voice doc.
# Per-account convention: brand_voice/social_proof.<account_key>.md wins when present.
# Missing/empty file = the feature is silently absent (normal drafting unaffected).
SOCIAL_PROOF_PATH = os.environ.get("AGENT_SOCIAL_PROOF_PATH", "brand_voice/social_proof.md")
# The one weekday a social proof card may enter the plan (proof converts but repels
# when spammed): at most ONE per account per week, structurally.
SOCIAL_PROOF_DAY = os.environ.get("AGENT_SOCIAL_PROOF_DAY", "wed").lower()

# Append-only log of every post we publish (or "would publish" in draft-only).
POST_LOG_PATH = os.environ.get("AGENT_POST_LOG_PATH", "post_log.jsonl")

# ---- Slack control surface ---------------------------------------------------
SLACK_BOT_TOKEN_ENV = "AGENT_SLACK_BOT_TOKEN"  # name of the env var, not the value
SLACK_CHANNEL_ID = os.environ.get("AGENT_SLACK_CHANNEL_ID", "")

# ---- Posting schedule (2026 cadence) -----------------------------------------
# Timing only: which days and what time a post is scheduled for. This never
# publishes and never touches approval (see schedule.py).
def _csv_list(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


POSTING_TIMEZONE = os.environ.get("AGENT_POSTING_TZ", "America/New_York")
POSTING_PRIMARY_TIME = os.environ.get("AGENT_POSTING_PRIMARY_TIME", "18:30")
POSTING_MORNING_TIME = os.environ.get("AGENT_POSTING_MORNING_TIME", "07:30")
POSTS_PER_DAY = int(os.environ.get("AGENT_POSTS_PER_DAY", "1"))
POSTING_SKIP_DAYS = _csv_list("AGENT_POSTING_SKIP_DAYS", [])
POSTING_PRIORITY_DAYS = _csv_list("AGENT_POSTING_PRIORITY_DAYS", ["tue", "wed", "thu"])

# ---- Meta Graph API ----------------------------------------------------------
# v23.0: past the views migration (impressions deprecated for media created
# after July 2 2024; the media insights metric set is the views-era one).
GRAPH_API_VERSION = os.environ.get("AGENT_GRAPH_API_VERSION", "v23.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# ---- Creative studio (Nano Banana infographic generation) --------------------
# OFF by default. The API key is read lazily in creative_studio.py (like tokens),
# never stored on an object and never logged. Only the env var NAME lives here.
NANO_API_KEY_ENV = "AGENT_NANO_API_KEY"  # name of the env var, not the value
# Generation models. NANO_MODEL is the default for ALL cards (Pro tier for text
# accuracy). NANO_MODEL_FLASH is the optional lower-cost route for photographic
# or text-light fills; gated behind nano_flash_enabled() (OFF by default).
# Neither model is hardcoded: both read from env so Blake changes them by hand.
# Source of truth: brand_voice/lasso_house_style.md section 7.
NANO_MODEL = os.environ.get("AGENT_NANO_MODEL", "gemini-3-pro-image")
NANO_MODEL_FLASH = os.environ.get("AGENT_NANO_MODEL_FLASH", "gemini-3.1-flash-image")
# House style source of truth: the prompt scaffold and grade gate are defined
# in this document. Code constants in creative_studio.py must match section 8.
HOUSE_STYLE_PATH = os.environ.get("AGENT_HOUSE_STYLE_PATH",
                                  "brand_voice/lasso_house_style.md")
# VISION READ model (image -> text), SEPARATE from the generation model above.
# The *-image models (Nano Banana family: gemini-3-pro-image, gemini-3.1-flash-image)
# GENERATE images and return image parts, not text, so they cannot transcribe text
# back OUT of an image. OCR / autotag / the pixel fabrication gate need a
# vision-capable TEXT model. Override with AGENT_OCR_MODEL; default is the current
# stable flash model (gemini-2.5-flash was retired for new accounts and returns 404
# / "no longer available"; gemini-3.5-flash is the current default flash and is
# vision-capable). Verify a replacement resolves before shipping it.
OCR_MODEL = os.environ.get("AGENT_OCR_MODEL", "gemini-3.5-flash")
# Image output aspect: IG/FB feed posts are 4:5 PORTRAIT (1080x1350). Tunable via env
# so the target can change without a code edit.
IMAGE_ASPECT = os.environ.get("AGENT_IMAGE_ASPECT", "4:5")
IMAGE_PIXELS = os.environ.get("AGENT_IMAGE_PIXELS", "1080x1350")
# Stories aspect: 9:16 vertical (1080x1920). Per-use, NOT a global switch: the feed
# keeps IMAGE_ASPECT and a Story requests STORY_ASPECT for its own generation call.
STORY_ASPECT = os.environ.get("AGENT_STORY_ASPECT", "9:16")
STORY_PIXELS = os.environ.get("AGENT_STORY_PIXELS", "1080x1920")

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


def socialapi_enabled() -> bool:
    """
    SocialAPI.ai publish-lane master switch. OFF by default. When OFF, EVERY
    account publishes through meta_direct exactly as before, even one whose
    publish_route is 'socialapi'. When ON, an account whose publish_route is
    'socialapi' routes its publish step through the SocialAPI lane; meta_direct
    accounts are untouched. This gates ROUTING only, never whether a post goes
    out: the publish_enabled() draft-only gate still applies inside the lane.
    Arm by hand in Railway env only.
    """
    return _truthy(os.environ.get("AGENT_SOCIALAPI_ENABLED", "false"))


def socialapi_key() -> str:
    """The SocialAPI.ai API key, read lazily BY NAME every call so a rotation
    takes effect without a reimport. Never logged, never stored on an object,
    never returned in any card or report. Empty string when unset."""
    return os.environ.get(SOCIALAPI_KEY_ENV, "")


def socialapi_base_url() -> str:
    """The SocialAPI.ai REST base URL (override for tests / a staging host)."""
    return os.environ.get("AGENT_SOCIALAPI_BASE_URL", SOCIALAPI_BASE_URL_DEFAULT)


def socialapi_max_per_day() -> int:
    """Sanity ceiling on publishes per account per day for the SocialAPI lane.
    One post/day is the norm; more than this is an upstream bug and fires a loud
    ops alert. Default 3."""
    try:
        return int(os.environ.get("AGENT_SOCIALAPI_MAX_PER_DAY", "3"))
    except ValueError:
        return 3


def creative_studio_enabled() -> bool:
    """
    Nano Banana image generation switch. OFF by default. When OFF, generate()
    returns None and makes NO API call. Independent of publishing; this only
    controls whether Echo draws an infographic, never whether it posts.
    """
    return _truthy(os.environ.get("AGENT_NANO_ENABLED", "false"))


def nano_flash_enabled() -> bool:
    """
    Flash model route switch. OFF by default = ALL cards use NANO_MODEL (Pro).
    When ON, photographic or text-light fills route to NANO_MODEL_FLASH instead.
    Text-heavy cards (headline, labels, stats) always stay on the Pro model
    regardless of this flag. The actual model used is logged per card.
    Arm by hand in Railway env only; source of truth: lasso_house_style.md sec 6.
    """
    return _truthy(os.environ.get("AGENT_NANO_FLASH_ENABLED", "false"))


def style_gate_enabled() -> bool:
    """
    House-style six-question grade gate switch. OFF by default. When ON, every
    generated card is scored against the six questions in lasso_house_style.md
    section 10 before entering the approval queue. A card failing two or more
    questions is regenerated once; if it still fails, ops_alert fires with named
    failing questions and the card is withheld from the queue. This is ADDITIVE
    to the fabrication gate: both must pass. OFF = generation behavior unchanged.
    """
    return _truthy(os.environ.get("AGENT_STYLE_GATE_ENABLED", "false"))


def hosting_enabled() -> bool:
    """
    S3-compatible media hosting switch. OFF by default. When OFF, host_media()
    returns None and the draft build keeps its current behavior. Independent of
    publishing; this only controls whether Echo uploads creatives for public URLs.
    """
    return _truthy(os.environ.get("AGENT_HOSTING_ENABLED", "false"))


def welcome_templates_enabled() -> bool:
    """
    Welcome-post template surface switch. OFF by default. When OFF, the daily path
    never draws a welcome-new-gym card. The `welcome-templates` operator command
    (render the review set, post proofs to Slack) runs regardless; this flag only
    gates any future automatic welcome-card path. Review posts never publish.
    """
    return _truthy(os.environ.get("AGENT_WELCOME_TEMPLATES_ENABLED", "false"))


def chat_publish_enabled() -> bool:
    """
    Chat-can-publish switch. OFF by default. When OFF, free-text chat never triggers
    a publish (the listener's message handler is inert). When ON, Blake can publish
    LASSO-owned accounts directly from chat with an EXPLICIT publish verb; client
    accounts are only drafted + scheduled, never chat-published. Requires
    AGENT_PUBLISH_ENABLED for a real Meta write; the ownership scoping and the
    fabrication/grade/dash-vendor gates always apply. Arm by hand in Railway env only.
    """
    return _truthy(os.environ.get("AGENT_CHAT_PUBLISH_ENABLED", "false"))


def welcome_posts_enabled() -> bool:
    """
    Auto welcome-posts-from-new-clients switch. OFF by default. When OFF, the
    pipeline never runs: no Stripe read, no logo scrape, no card. When ON, a brand
    new paying client (first-ever subscription in the window, active, on a core
    tier) gets a feed + story welcome post SURFACED to the approval channel, held
    for Blake's tap. Nothing here publishes; a client account is never chat-published
    to. Arm by hand in Railway env only.
    """
    return _truthy(os.environ.get("AGENT_WELCOME_POSTS_ENABLED", "false"))


def welcome_queue_enabled() -> bool:
    """
    One-per-day welcome DRIP + the automatic new-client trigger. OFF by default.
    When OFF, the daily runner's welcome hooks return None and the daily Stripe scan
    no-ops: byte-for-byte current behavior. When ON, the runner scans Stripe each
    cycle, enqueues every ready new-client welcome (feed + story, hosted), and serves
    the OLDEST queued welcome one gym per day across lasso_ig + lasso_fb. Served
    drafts are PENDING: they card for approval, or auto-publish only when
    AGENT_AUTO_APPROVE_ENABLED is armed (the story also needs AGENT_STORIES_ENABLED).
    Needs AGENT_HOSTING_ENABLED (R2) to host the cards. Arm by hand in Railway env.
    """
    return _truthy(os.environ.get("AGENT_WELCOME_QUEUE_ENABLED", "false"))


def demo_calendar_enabled() -> bool:
    """
    The done-for-you demo calendar: 30 pre-made dated LASSO drafts that flow through
    Echo's real pipeline so onboarding a client onto done-for-you organic can be
    experienced end to end. OFF by default. When OFF, the daily runner's demo hooks
    return None and nothing is served: byte-for-byte current behavior. When ON, the
    runner serves the day's dated demo post (feed cross-posted to lasso_ig + lasso_fb,
    story on lasso_ig) as a PENDING draft. Served drafts card for approval, or
    auto-publish only when AGENT_AUTO_APPROVE_ENABLED is armed (the story also needs
    AGENT_STORIES_ENABLED). Needs AGENT_HOSTING_ENABLED (R2) to host the cards. Arm by
    hand in Railway env. Isolated from the book / welcome / summit queues.
    """
    return _truthy(os.environ.get("AGENT_DEMO_CALENDAR_ENABLED", "false"))


# The gym_id the DEMO calendar content is valid for. The demo manifest drafts only
# ever belong to THIS gym in the shared content_calendar. A real gym id must never
# be this value, so the real-calendar mirror can prove it never leaves demo rows on a
# real gym. Default "lasso_demo" (matches the demo tenant used by media_host). Set by
# hand only if the demo gym id ever changes.
def demo_calendar_gym_id() -> str:
    """The one gym_id demo content is allowed on. Never a real gym."""
    return (os.environ.get("AGENT_DEMO_CALENDAR_GYM_ID", "lasso_demo") or "").strip()


def real_calendar_mirror_enabled() -> bool:
    """
    Real-drafts calendar mirror switch. OFF by default = zero behavior change: the
    runner never mirrors, and the shared content_calendar keeps whatever it already
    holds (byte-for-byte today's behavior). ON, each daily cycle folds a real gym's
    REAL Echo drafts (the ones carrying a hosted creative URL) into the shared
    content_calendar so /social and /calendar serve the gym's actual plan instead of
    demo content, and clears any demo-manifest rows off a real gym. Gym-scoped: the
    mirror only ever touches ONE gym_id per call and never another gym's rows. It
    writes calendar rows only; it never publishes and adds no publish path. Arm by
    hand in Railway env.
    """
    return _truthy(os.environ.get("AGENT_REAL_CALENDAR_MIRROR", "false"))


def real_month_plan_enabled() -> bool:
    """
    REAL month planner switch. OFF by default = zero behavior change: the planner is
    never invoked, so /social and the shared content_calendar keep whatever they
    already hold (byte-for-byte today's behavior). ON, the planner can assemble a full
    month of REAL LASSO drafts (two per day, one feed + one paired 9:16 story) spanning
    every LASSO content type per the weekly rotation, plus book / summit / welcome
    overrides on the days they occur, then upsert them as content_calendar rows for
    gym_id='lasso' and clear ALL demo rows off that gym. The planner is pure and
    injectable: it reuses the EXISTING category draft builders (never invents content),
    a slot whose source or creative is missing is SKIPPED (never fabricated), and it
    writes calendar rows only. It never publishes and adds no publish path. Arm by hand.
    """
    return _truthy(os.environ.get("AGENT_REAL_MONTH_PLAN", "false"))


def no_creative_fallback_enabled() -> bool:
    """
    No-creative fallback switch (the client calendar card image). OFF by default =
    zero behavior change: a calendar row with no usable creative image keeps its empty
    image_public_url exactly as today (the portal renders its existing empty state).
    When ON, a row whose creative image is missing / None (a Gemini or Nano render that
    failed or was skipped, or a DAM with nothing) degrades to a clean, on-brand WEBSITE
    STYLE INFOGRAPHIC rendered from that row's OWN approved text (caption / pillar) via
    the house PIL renderer, so every calendar day shows a presentable, approvable card
    instead of a blank or broken one. NO FABRICATION: the infographic text comes only
    from the row's approved caption / pillar; a row with NO text renders nothing (the
    upstream still blocks / shows its empty state, never a blank card and never invented
    copy). Nothing here publishes and no gate is weakened. Arm by hand in Railway env.
    """
    return _truthy(os.environ.get("AGENT_NO_CREATIVE_FALLBACK", "false"))


def portal_social_enabled() -> bool:
    """
    Portal client-social MASTER switch (Part A). OFF by default. When OFF, EVERY new
    portal-social hook is inert: the per-gym calendar engine serves nothing, the
    collision-shift never runs, and approval_surface routing collapses to today's
    behavior (Slack for everyone) so the pipeline is byte-for-byte unchanged. When ON,
    Echo may key calendars per gym (gym_id + zernio_profile_id), SHIFT a calendar post
    off any day the dated book queue (or another queue) already occupies for an account,
    and route CLIENT-gym drafts to the portal approval surface instead of a Slack card
    (ops_alerts on failures still go to Slack for every gym). Client CONTENT generation
    is out of scope for Part A: this flag arms the ENGINE + per-gym keying + the
    served-once-per-day lock only. Mirrors demo_calendar_enabled: arm by hand in Railway
    env. Every other gate is untouched; no new publish path is added.
    """
    return _truthy(os.environ.get("AGENT_PORTAL_SOCIAL_ENABLED", "false"))


# The Stripe secret is read lazily BY NAME (never stored, never logged), same
# pattern as every other token. Set STRIPE_API_KEY in Railway (a RESTRICTED,
# read-only key: Customers, Subscriptions, Products, Prices). Empty => the welcome
# pipeline reports "no Stripe key" and does nothing (it never guesses a roster).
STRIPE_API_KEY_ENV = "STRIPE_API_KEY"  # name of the env var, not the value


def stripe_api_key() -> str:
    """The Stripe secret, read from env at each call so a rotation takes effect
    without a reimport. Never logged, never returned in any card or report."""
    return os.environ.get(STRIPE_API_KEY_ENV, "").strip()


def podcast_doc_clips_enabled() -> bool:
    """
    Doc-first clip selection switch. OFF by default. When ON, the podcast clipper
    takes its PRIMARY clip candidates from the show-notes doc (Episode Chapters +
    Memorable Quotes, parsed by podcast_docparse) and uses the transcript only to
    locate exact in/out points, falling back to transcript scoring only when the
    doc has no usable quotes (and reporting the fallback). OFF = current
    transcript-scored selection, unchanged.
    """
    return _truthy(os.environ.get("AGENT_PODCAST_DOC_CLIPS", "false"))


def podcast_audit_enabled() -> bool:
    """
    Level-2 standing audit gate switch. OFF by default. When ON, every episode's
    output set is checked by podcast_audit before it surfaces to Slack (quota,
    caption ghost, intro animation, bottom treatment, no static takeover, caption
    free variant, quote verbatim). A failing asset regenerates once, then surfaces
    flagged with the failing check named. OFF = outputs surface unchecked (current).
    """
    return _truthy(os.environ.get("AGENT_PODCAST_AUDIT_ENABLED", "false"))


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


def stories_enabled() -> bool:
    """
    Instagram/Facebook Stories switch. OFF by default = FULLY DORMANT: no Story
    drafts are generated at all. ON, Echo drafts one 9:16 Story per account per day
    reusing the day's approved creative; every Story draft is PENDING and held for
    approval. Publishing a Story additionally requires AGENT_PUBLISH_ENABLED (both
    gates must be armed); with this flag OFF, publish() returns would_publish and
    makes NO network call even when the publish flag is armed.
    """
    return _truthy(os.environ.get("AGENT_STORIES_ENABLED", "false"))


def story_crosspost_enabled() -> bool:
    """When armed, every approved reel or image is also cross-posted to Stories
    on the same account immediately after the main publish succeeds.
    AGENT_STORY_CROSSPOST_ENABLED (default OFF). Requires AGENT_PUBLISH_ENABLED.
    Story failures are non-fatal — the main publish result is unaffected."""
    return _truthy(os.environ.get("AGENT_STORY_CROSSPOST_ENABLED", "false"))


def caption_seo_enabled() -> bool:
    """
    2026 caption SEO switch for the content brain. OFF by default = captions are
    assembled exactly as today. ON, the planner may REORDER the approved body lines
    so a line carrying the hook's key topic terms sits first after the hook. It only
    reorders or selects among APPROVED lines; it never writes new text. If no
    reorder satisfies placement, the original order is kept.
    """
    return _truthy(os.environ.get("AGENT_CAPTION_SEO_ENABLED", "false"))


def episode_inbox_enabled() -> bool:
    """
    Episode inbox watcher switch. OFF by default. When ON, the listener polls
    AGENT_EPISODE_INBOX_PREFIX every AGENT_EPISODE_INBOX_POLL_MINUTES for new
    episode files, runs Phase 1 clip selection, and posts the ranked plan to
    Slack. Nothing renders and nothing posts automatically.
    """
    return _truthy(os.environ.get("AGENT_EPISODE_INBOX_ENABLED", "false"))


def episode_inbox_prefix() -> str:
    """Watched R2 prefix. Default: echo/episode_inbox/<tenant>/."""
    tenant = episode_inbox_tenant()
    return os.environ.get("AGENT_EPISODE_INBOX_PREFIX",
                          f"echo/episode_inbox/{tenant}/")


def episode_inbox_tenant() -> str:
    """Tenant slug scoping the inbox prefix. Default: lasso_episodes."""
    return os.environ.get("AGENT_EPISODE_INBOX_TENANT", "lasso_episodes")


def episode_inbox_poll_minutes() -> int:
    """How often to poll the inbox prefix. Default: 5 minutes."""
    return max(1, int(os.environ.get("AGENT_EPISODE_INBOX_POLL_MINUTES", "5")))


def episode_nudge_time() -> str:
    """Wall-clock HH:MM (America/New_York) when the Monday nudge fires. Default: 09:00."""
    return os.environ.get("AGENT_EPISODE_NUDGE_TIME", "09:00")


def episode_nudge_window_days() -> int:
    """Days back from today the nudge considers an episode 'recent'. Default: 2."""
    return max(1, int(os.environ.get("AGENT_EPISODE_NUDGE_WINDOW_DAYS", "2")))


def platform_variants_enabled() -> bool:
    """
    Per-platform caption variant switch. OFF by default = one identical caption and
    hashtag set for every platform, exactly as today. ON, Instagram keeps up to 5
    approved hashtags and a Facebook Page keeps at most 2 (placed at the end, which
    is where the composer already puts them). Selection only; no new text.
    """
    return _truthy(os.environ.get("AGENT_PLATFORM_VARIANTS_ENABLED", "false"))


def doc_intake_enabled() -> bool:
    """
    Document intake switch (Stage 2 seed). OFF by default. When OFF, process_document
    returns None and reads nothing. A client PDF is raw material held for approval; it
    is never treated as approved fact and nothing here publishes.
    """
    return _truthy(os.environ.get("AGENT_DOC_INTAKE_ENABLED", "false"))


# The LASSO knowledge brain: approved source files under brand_voice/knowledge/.
KNOWLEDGE_DIR = os.environ.get("AGENT_KNOWLEDGE_DIR", "brand_voice/knowledge")

# Summit campaign constants (04_summit_campaign.md is the only drafting source).
SUMMIT_CTA = "Claim your seat"
SUMMIT_URL = "https://lassoframework.com/summit"
SUMMIT_END_DATE = "2026-11-08"          # campaign auto-stops after this day
SUMMIT_DAY = os.environ.get("AGENT_SUMMIT_DAY", "tue").lower()  # the weekly slot


# Creative rotation: the no-repeat window (days) and where the served log lives
# (/data on the listener service so it survives restarts).
ROTATION_WINDOW_DAYS = int(os.environ.get("AGENT_ROTATION_WINDOW_DAYS", "14"))


def rotation_enabled() -> bool:
    """
    Creative rotation + variety guard switch. OFF by default = selection behaves
    exactly as today. ON: no creative repeats within the window, consecutive days
    never share a pillar, the approved library is cycled (generated Nano is one
    source among several), and only gate-clean creatives are ever picked. This
    changes WHICH approved creative a draft proposes, never whether it needs a tap.
    """
    return _truthy(os.environ.get("AGENT_ROTATION_ENABLED", "false"))


# ---- Opus Clip ingest (documented API: https://help.opus.pro/api-reference) ----
# Auth is a Bearer key read lazily by NAME (never logged, never printed); the
# optional org id header covers multi-org accounts. Discovery: the API has NO bulk
# project listing, so we pull clips from pinned project ids and/or collections.
OPUS_API_BASE = os.environ.get("AGENT_OPUS_API_BASE", "https://api.opus.pro")
OPUS_API_KEY_ENV = "OPUS_API_KEY"  # name of the env var, not the value
OPUS_ORG_ID = os.environ.get("AGENT_OPUS_ORG_ID", "")


def opus_api_base() -> str:
    """Opus API root URL, read from env at each call (never import-time cached)."""
    return os.environ.get("AGENT_OPUS_API_BASE", "https://api.opus.pro")


def opus_org_id() -> str:
    """Opus org-id header value, read from env at each call."""
    return os.environ.get("AGENT_OPUS_ORG_ID", "")
# The video factory discovers via the account's COLLECTIONS (the documented API
# has no bulk project-listing endpoint), so no hand-maintained allowlist is
# required for the common case. AGENT_OPUS_PROJECT_IDS remains an optional manual
# escape hatch, honored by BOTH the factory (opus_factory.scan) and the legacy
# pull-opus poller (opus_ingest). AGENT_OPUS_COLLECTION_IDS is used by the legacy
# poller only.
OPUS_PROJECT_IDS = _csv_list("AGENT_OPUS_PROJECT_IDS", [])
OPUS_COLLECTION_IDS = _csv_list("AGENT_OPUS_COLLECTION_IDS", [])


def opus_project_ids():
    """Pinned Opus project ids, read from env at each call (the factory reads
    them live so no module reload is needed). The legacy poller still uses the
    import-time OPUS_PROJECT_IDS constant."""
    return _csv_list("AGENT_OPUS_PROJECT_IDS", [])


def opus_enabled() -> bool:
    """
    Opus Clip ingest switch. OFF by default: pull-opus is a no-op and nothing is
    fetched. ON, finished clips are pulled, hosted, and filed as video assets that
    become Reel DRAFTS through the normal path (held for approval like everything).
    """
    return _truthy(os.environ.get("AGENT_OPUS_ENABLED", "false"))


def opus_poll_enabled() -> bool:
    """
    The scheduled Opus poll switch (listener loop). OFF by default and fully inert.
    ON (with AGENT_OPUS_ENABLED also on), the listener runs the same ingest every
    AGENT_OPUS_POLL_MINUTES (default 60).
    """
    return _truthy(os.environ.get("AGENT_OPUS_POLL_ENABLED", "false"))


def weekly_report_enabled() -> bool:
    """
    Sunday operator report switch. OFF by default = ZERO behavior change
    anywhere: no build, no Slack post, no kv stamp. ON, ONE Slack card lands
    in the approval channel Sundays at 6:00 PM ET: the week's posts per
    account, approvals pending, the views based engagement rollup (IG framed
    on engagement only, never frequency), runway days, the flags delta vs
    last week, and the single most important by hand item. Honest: missing
    data says no data, never a fabricated number.
    """
    return _truthy(os.environ.get("AGENT_WEEKLY_REPORT_ENABLED", "false"))


# ---- Podcast pipeline (feed watcher -> release card -> transcript sources) ----
# The show's RSS feed url, set by hand in env. Empty while the flag is armed =
# the poll STOPS LOUD (missing data is reported, never guessed).
PODCAST_FEED_URL = os.environ.get("AGENT_PODCAST_FEED_URL", "")


def podcast_enabled():
    """
    Podcast pipeline switch. OFF by default = ZERO behavior change anywhere: no
    feed fetch, no episode records, no release cards, no transcript sources, and
    the podcast CLIs refuse to run. ON, the listener polls the RSS feed on the
    existing scheduler cadence and a new episode is stored exactly once
    (idempotent by guid). Every draft this pipeline ever produces still cards
    for approval; nothing here publishes.
    """
    return _truthy(os.environ.get("AGENT_PODCAST_ENABLED", "false"))


# The Full Gym book campaign: approved source docs at the repo-root knowledge/
# folder (env override for tests). The book file is the MASTER source.
BOOK_DIR = os.environ.get("AGENT_BOOK_DIR", "knowledge")
BOOK_SOURCE_FILES = ("full_gym_book.md", "full_gym_case_studies.md",
                     "full_gym_launch_campaign.md")
BOOK_QUEUE_FILE = "BOOK_LAUNCH_QUEUE_WEEK1.md"


def book_campaign_enabled() -> bool:
    """
    Book launch campaign switch. OFF by default. ON, the campaign LEADS the
    calendar: one book post per day takes posting priority (queue verbatim
    first, then angles 1 to 8 rotate; 9 to 11 stay dark until their LOCKED
    blanks fill in full_gym_book.md). Every draft still cards to Blake.
    """
    return _truthy(os.environ.get("AGENT_BOOK_CAMPAIGN_ENABLED", "false"))


def story_premade_enabled() -> bool:
    """
    Story premade-variant switch. OFF by default: Stories keep today's exact
    behavior (9:16 re-render of the day's approved creative, else the feed
    image). ON, a premade *_story render sitting next to the day's creative
    (the regen-library convention) is preferred over generating. Draft flow,
    labels, cadence, and publish gates untouched.
    """
    return _truthy(os.environ.get("AGENT_STORY_PREMADE_ENABLED", "false"))


def knowledge_enabled() -> bool:
    """
    Knowledge brain switch. OFF by default. ON, the drafter may draw facts, hooks,
    pillars, and angles from brand_voice/knowledge/ under hard gates parsed from
    the files themselves: LOCKED / PENDING / NOT FOUND content and *_pending.md
    files are NEVER drafting sources; only USE-marked stats may appear in copy,
    wording matched exactly.
    """
    return _truthy(os.environ.get("AGENT_KNOWLEDGE_ENABLED", "false"))


def summit_campaign_enabled() -> bool:
    """
    Summit campaign switch. OFF by default. ON, one summit post per week enters the
    plan (inside the daily cadence, never additional), drafted ONLY from the
    VERIFIED FACTS and APPROVED ANGLES blocks of 04_summit_campaign.md, rotating
    angles. Auto-stops after SUMMIT_END_DATE.
    """
    return _truthy(os.environ.get("AGENT_SUMMIT_CAMPAIGN_ENABLED", "false"))


def trust_dryrun_enabled() -> bool:
    """
    Trust DRY RUN switch. OFF by default. ON, every draft that WOULD have
    auto-published under the account's trust level is audited and marked on its
    Slack card, but STILL requires the tap. Nothing publishes without approval
    in dry run, ever.
    """
    return _truthy(os.environ.get("AGENT_TRUST_DRYRUN", "false"))


def auto_approve_enabled() -> bool:
    """Master auto-approve: bypass the Slack approval card entirely and publish
    each draft at its scheduled time. A lightweight notice is still posted to Slack
    for visibility. AGENT_AUTO_APPROVE_ENABLED (default OFF).
    Requires AGENT_PUBLISH_ENABLED to actually write to Meta."""
    return _truthy(os.environ.get("AGENT_AUTO_APPROVE_ENABLED", "false"))


def trust_autopublish_enabled() -> bool:
    """
    Trust AUTOPUBLISH switch. OFF by default; a startup warning prints when
    armed. Only queue-verbatim / calendar-routine posts inside a human-approved
    monthly calendar are eligible, and only for a level 1+ account. Anything
    off template, any first post to a new audience or surface, any story, any
    comment, and any book campaign post ALWAYS cards regardless of trust.
    Trust is per account and never transfers.
    """
    return _truthy(os.environ.get("AGENT_TRUST_AUTOPUBLISH", "false"))


def trust_ladder_enabled() -> bool:
    """
    The trust ladder DOUBLE GATE. OFF by default: every account cards every
    draft regardless of its configured level (a level typo changes nothing).
    ON, a level 1 account's drafts inside a human-approved monthly calendar may
    skip the card WHEN the by-hand publish wiring is also done. Level changes
    are hand-edited config only, never code.
    """
    return _truthy(os.environ.get("AGENT_TRUST_LADDER_ENABLED", "false"))


def portal_approvals_enabled() -> bool:
    """
    Portal-callable approval endpoints for per-gym approvers. Default OFF.
    ON, each gym's designated approver can approve, edit, deny, or kill drafts
    scoped to their gym only. An actor authorized for gym A cannot act on gym B.
    """
    return _truthy(os.environ.get("AGENT_PORTAL_APPROVALS", "false"))


def social_billing_delegated() -> bool:
    """The LASSO portal now owns the $99.99/mo social subscription and enforces
    entitlement (isSocialEntitled) BEFORE it ever calls Echo. When this flag is ON,
    Echo stops double-gating on its own Stripe check (which fails closed for gyms that
    have no Echo-side Stripe customer, since billing moved to the portal) and trusts
    the portal's gate. Default OFF preserves the standalone Stripe gate. The token
    auth + AGENT_PORTAL_SOCIAL_ENABLED flag still apply either way."""
    return _truthy(os.environ.get("AGENT_SOCIAL_BILLING_DELEGATED", "false"))


# ---- Portal onboard endpoint shared key --------------------------------------
# The LASSO portal authenticates the self-serve onboard call (POST /portal/onboard)
# with a shared secret in the X-Portal-Key header, compared in constant time. The
# key is read lazily BY NAME every call (so a rotation takes effect without a
# reimport), never logged, never stored on an object. Empty => the endpoint refuses
# every request (401), so a forgotten env var fails CLOSED, never open.
PORTAL_ONBOARD_KEY_ENV = "AGENT_PORTAL_ONBOARD_KEY"  # name of the env var, not the value


def portal_onboard_key() -> str:
    """The shared portal onboard key, read from env at each call. Empty when unset.
    Never logged, never returned in any response."""
    return os.environ.get(PORTAL_ONBOARD_KEY_ENV, "").strip()


# ---- Portal calendar data plane (Supabase content_calendar) ------------------
# The live portal calendar reads/writes the SHARED Supabase content_calendar table
# instead of the local, ephemeral SQLite drafts table. There is NO separate flag:
# the PRESENCE of both creds is the switch. When either is absent, portal_routes
# keeps its existing SQLite path unchanged (so every existing test stays green).
# The service key is read lazily by NAME every call, never logged, never stored.
def supabase_url() -> str:
    """Supabase project REST base (e.g. https://<ref>.supabase.co). Empty when unset."""
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def supabase_service_key() -> str:
    """Supabase service role key, read lazily so a rotation needs no reimport.
    Never logged, never printed, never returned in any card or report."""
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def portal_calendar_supabase_enabled() -> bool:
    """
    Use the shared Supabase content_calendar as the portal data plane iff BOTH
    SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set. No separate flag: creds
    present = use Supabase; creds absent = the existing SQLite behavior, unchanged.
    """
    return bool(supabase_url()) and bool(supabase_service_key())


# Zernio social-connect. The key was set in Railway as ZERNIO_API_KEY (no AGENT_ prefix), so we read
# that exact name. The key's PRESENCE is the switch — no key means the endpoints are dark and return
# a clean disabled response, so nothing accidentally calls a paid vendor without the credential.
ZERNIO_API_KEY_ENV = "ZERNIO_API_KEY"


def zernio_api_base() -> str:
    """Zernio API base URL. Overridable for tests; defaults to the live host."""
    return (os.environ.get("ZERNIO_API_BASE") or "https://api.zernio.com").rstrip("/")


def zernio_enabled() -> bool:
    """Zernio social-connect endpoints are enabled iff the API key is present."""
    return bool((os.environ.get(ZERNIO_API_KEY_ENV) or "").strip())


def zernio_analytics_enabled() -> bool:
    """
    Zernio ANALYTICS pull switch (Part C dependency). OFF by default. When OFF, the
    portal metrics endpoint returns the Part D payload SHAPE with every value null /
    empty (no live numbers, never a fabricated 0). When ON (and the Zernio analytics
    add-on is enabled on the account, a by-hand Blake step), Part C wires the real
    per-post numbers into the same shape. This flag arms the READ only; nothing here
    publishes. Mirrors zernio_enabled: the analytics add-on is a paid capability, so
    the default is dark until Blake confirms it on the account.
    """
    return _truthy(os.environ.get("AGENT_ZERNIO_ANALYTICS_ENABLED", "false"))


def monthly_report_enabled() -> bool:
    """
    Monthly client report switch (Part D). OFF by default. When OFF, the portal
    metrics endpoint answers with the report SHAPE only (null / empty values); no
    before/after story is assembled. When ON, Part D fills the shape from the
    baseline record + the Zernio analytics pull. Missing metrics MUST show as gaps
    ("not available on this account"), never a fabricated 0. Arm by hand in Railway.
    """
    return _truthy(os.environ.get("AGENT_MONTHLY_REPORT_ENABLED", "false"))


def backup_enabled() -> bool:
    """
    Nightly store backup switch. OFF by default. ON, a consistent sqlite
    snapshot of /data/echo.db lands in R2 (echo/backups/) once nightly with a
    14 day retention sweep. One ops alert on failure only.
    """
    return _truthy(os.environ.get("AGENT_BACKUP_ENABLED", "false"))


def brain_proposals_enabled() -> bool:
    """
    Nightly brain switch. OFF by default. ON, one read-only Slack note per night
    (the hour after the digest): what is winning, one angle quoted from approved
    sources, one question when data is thin. Proposes, never creates.
    """
    return _truthy(os.environ.get("AGENT_BRAIN_PROPOSALS_ENABLED", "false"))


def digest_enabled() -> bool:
    """
    Evening digest switch. OFF by default. ON, one Slack line per day at
    AGENT_DIGEST_HOUR_UTC (default 23): drafted / approved / published /
    blocked / runway. A ten second read; posts nothing else.
    """
    return _truthy(os.environ.get("AGENT_DIGEST_ENABLED", "false"))


def consent_guard_enabled() -> bool:
    """
    Consent guard switch. OFF by default. ON, FAIL SAFE: an asset is selectable
    only when its sidecar says people=false, or people=true with
    consent="granted". Unknown = excluded. Arming on an untagged library
    excludes everything until assets are tagged; that is the guard working.
    """
    return _truthy(os.environ.get("AGENT_CONSENT_GUARD_ENABLED", "false"))


def content_moderation_enabled() -> bool:
    """
    Content moderation switch. OFF by default. ON, the ingest moderator calls
    Gemini Vision per image; flagged assets move to intake/<client>/review/ with
    a Slack notice rather than the content library. Video is skipped (image-only
    pass). Fails open: any API error lets the asset through so uploads never stall.
    """
    return _truthy(os.environ.get("AGENT_CONTENT_MODERATION_ENABLED", "false"))


def autotag_enabled() -> bool:
    """
    Auto-tag switch. OFF by default. ON, one lowest-cost Gemini vision call per
    new asset writes tags + people flag + description into the sidecar; low
    confidence marks review=true. Counts against the daily Gemini spend cap.
    """
    return _truthy(os.environ.get("AGENT_AUTOTAG_ENABLED", "false"))


def ocr_check_enabled() -> bool:
    """
    Headline OCR check switch. OFF by default. ON, a rendered card's headline is
    transcribed (Gemini vision, lowest cost) and fuzzy matched to the intended
    headline; a mismatch adds a WARNING line to the Slack card, never a block.
    """
    return _truthy(os.environ.get("AGENT_OCR_CHECK_ENABLED", "false"))


def spend_cap_enabled() -> bool:
    """
    Gemini spend cap switch. OFF by default. ON, generation calls count per day
    in the store; at AGENT_GEMINI_DAILY_CAP (default 40) further generation for
    the day returns None (library-only selection takes over) plus one ops alert.
    """
    return _truthy(os.environ.get("AGENT_SPEND_CAP_ENABLED", "false"))


def image_grade_enabled() -> bool:
    """
    Vision check on the actual generated image. OFF by default. ON, after Gemini
    generates an image a second vision call checks Q1 (left-aligned), Q2 (scale
    contrast), and Q5 (thumbnail legible) against the actual output pixels; if any
    fail the card is regenerated up to two more times before an ops alert fires and
    the card is withheld. Uses OCR_MODEL (the same vision-capable text model as the
    OCR check). Independent of AGENT_STYLE_GATE_ENABLED: both gates can run in the
    same attempt loop, or either can run alone.
    """
    return _truthy(os.environ.get("AGENT_IMAGE_GRADE_ENABLED", "false"))


def runway_enabled() -> bool:
    """
    Creative runway switch. OFF by default. ON, one line per account per day:
    days of approved gate-clean content left, green/amber/red, projected zero
    date; below AGENT_RUNWAY_ALERT_DAYS one debounced ops alert asks for raw
    material. Read-only over the library and the store; never posts content.
    """
    return _truthy(os.environ.get("AGENT_RUNWAY_ENABLED", "false"))


def runway_alerts_enabled() -> bool:
    """
    Separate flag for the text-back refill alert to the gym.
    AGENT_RUNWAY_ENABLED must also be ON for alerts to fire.
    Default OFF. Arm by hand when a gym is onboarded.
    """
    return _truthy(os.environ.get("AGENT_RUNWAY_ALERTS", "false"))


def plan_month_enabled() -> bool:
    """
    Month planner switch. OFF by default. ON, plan-month fills open posting days
    from the eligible creative pool (rotation window + canvas guard respected),
    and approve-month bulk-approves the planned drafts. First post per account
    is always held for the tap; publishing defaults remain OFF.
    """
    return _truthy(os.environ.get("AGENT_PLAN_MONTH_ENABLED", "false"))


def monthly_review_enabled() -> bool:
    """
    Monthly review loop switch. OFF by default. ON, the 30 day per account
    cycle: top and bottom posts, health read, before vs after posting frequency,
    citation-gated angle proposals, and the raw material ask; one Slack digest
    plus a white label PDF. Reads only; drafts nothing, publishes nothing.
    """
    return _truthy(os.environ.get("AGENT_MONTHLY_REVIEW_ENABLED", "false"))


def grade_enabled() -> bool:
    """
    Social Grade switch. OFF by default. ON, the reporting assembler adds a per
    account letter grade (A to F) + subscores to the report payload. Honest grades:
    a missing metric lowers nothing and fakes nothing; it is listed as a gap.
    """
    return _truthy(os.environ.get("AGENT_GRADE_ENABLED", "false"))


def connect_grade_enabled() -> bool:
    """
    Connect-to-grade switch. OFF by default: /connect behavior is byte
    identical to today. ON, completing the connect page selection queues ONE
    Social Grade baseline read for that page and posts an informational
    BASELINE card line to the approval channel. No publish path involved.
    """
    return _truthy(os.environ.get("AGENT_CONNECT_GRADE_ENABLED", "false"))


def connect_tokens_enabled() -> bool:
    """
    Connect-token resolution switch. OFF by default: account tokens come ONLY
    from hand-set env vars, exactly as today. ON, an account whose page id has
    a /connect-stored kv token may use it, but an env token ALWAYS WINS when
    both exist. The kv token is never logged and never surfaced.
    """
    return _truthy(os.environ.get("AGENT_CONNECT_TOKENS_ENABLED", "false"))


def connect_enabled() -> bool:
    """
    Facebook connect page switch. OFF by default: the /connect surface 404s and
    the server thread never starts. ON, clients can link their Page + IG via
    Facebook Login for Business; the page token lands in the /data store. It
    changes NOTHING about posting: every post still cards for approval.
    """
    return _truthy(os.environ.get("AGENT_CONNECT_ENABLED", "false"))


# ---- Intake signed tokens ----------------------------------------------------
# One shared secret mints EVERY gym's intake/upload link (no per-gym env var, no
# redeploy per gym). Read lazily by NAME in intake_tokens.py, never stored on an
# object, never logged. Lives ONLY on the intake-web / listener service, never on
# the ops portal. Legacy per-client AGENT_INTAKE_TOKEN_<KEY> values still verify
# (env fallback) so the cutover is zero-downtime.
INTAKE_SIGNING_SECRET_ENV = "AGENT_INTAKE_SIGNING_SECRET"  # name of the env var, not the value


def intake_enabled() -> bool:
    """
    Texted-link intake switch (upload page + listener ingest). OFF by default: the
    upload page 404s everything and the ingest step never runs. Links are signed
    with one shared secret (AGENT_INTAKE_SIGNING_SECRET); legacy per-client env
    values (AGENT_INTAKE_TOKEN_<CLIENTKEY>) still verify. Neither is ever logged.
    """
    return _truthy(os.environ.get("AGENT_INTAKE_ENABLED", "false"))


def social_proof_enabled() -> bool:
    """
    Social proof cards switch. OFF by default (every new capability ships behind a
    flag that defaults OFF). ON, at most one verified, permissioned quote/stat card
    per account per week enters the plan; entries without permission or a verified
    date are SKIPPED with a notice, never rendered.
    """
    return _truthy(os.environ.get("AGENT_SOCIAL_PROOF_ENABLED", "false"))


def idempotent_drafts_enabled() -> bool:
    """
    Idempotent daily drafts switch. OFF by default = run-daily behaves exactly as
    today (a re-run re-drafts and re-cards). ON, run-daily is idempotent per account
    per day per draft type (feed, story): an unchanged PENDING draft is returned
    as-is with no new draft and no new card, and a genuinely changed draft
    SUPERSEDES the old one (the old Slack card is edited to a superseded state and
    can no longer be approved). Publishing is untouched either way.
    """
    return _truthy(os.environ.get("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "false"))


def ops_alerts_enabled() -> bool:
    """
    Ops alerts switch. OFF by default = failures keep today's behavior (logged
    only, nothing posted). ON, each silent fallback in the draft pipeline (hosting
    failed, creative empty, plan blocked, publish failed, store write failed)
    posts ONE short "ECHO ALERT:" line to the Slack channel. Alerts never carry
    tokens or secrets (see ops_alerts.scrub). Publishing is untouched either way.
    """
    return _truthy(os.environ.get("AGENT_OPS_ALERTS_ENABLED", "false"))


def publish_confirm_enabled() -> bool:
    """
    Publish confirmation switch. OFF by default = publish behavior is exactly
    today's (no read-back). ON, after a real publish Echo reads the post back via
    the Graph API (by media id, a READ), fetches its permalink, and replies it into
    the card's Slack thread. A failed verify warns in-thread and emits an ops
    alert. It NEVER re-publishes and never writes to Meta.
    """
    return _truthy(os.environ.get("AGENT_PUBLISH_CONFIRM_ENABLED", "false"))


def token_watchdog_enabled() -> bool:
    """
    Token watchdog switch. OFF by default = no check, no network. ON, once per
    daily cycle (and via `python -m agent check-tokens`) Echo reads each active
    account token's expiry via the Graph debug_token endpoint (a READ) and posts
    an ops alert when expiry is within token_warn_days(). The token itself is
    never printed, logged, or included in any alert.
    """
    return _truthy(os.environ.get("AGENT_TOKEN_WATCHDOG_ENABLED", "false"))


def token_warn_days() -> int:
    """How many days before token expiry the watchdog starts alerting (default 7)."""
    try:
        return int(os.environ.get("AGENT_TOKEN_WARN_DAYS", "7"))
    except ValueError:
        return 7


def category_rotation_enabled() -> bool:
    """
    Category rotation controller. OFF by default = zero behavior change; drafts
    are built exactly as today. ON, every content source is tagged with one of the
    six categories (podcast, platform, b2b, summit, book, doctrine); platform
    content carries a sub-topic from the 10-item rotation (no repeat within 10
    days); and the platform wording filter (vendor -> companies/software/tools/
    logins; dash removal) is applied at caption build time.
    """
    return _truthy(os.environ.get("AGENT_CATEGORY_ROTATION", "false"))


def client_sources_enabled() -> bool:
    """
    Per-client source docs switch (AGENT_CLIENT_SOURCES). OFF by default = zero
    behavior change; a client (non-LASSO) account drafts exactly as today (a
    library pick, or a blocked card when the library is thin). ON, a client
    account may draft a full, varied month from its OWN approved source docs
    (offer / service / testimonial / faq / about / promo), spread across
    categories like LASSO's doctrine, paired with its uploaded library.

    The fabrication gate stays the SOLE authority on claims: a client caption may
    only state facts present in THAT account's APPROVED sources, never invented,
    never a pending (unapproved) source, never LASSO's stats. Book and summit
    remain LASSO-only and never appear for a client.
    """
    return _truthy(os.environ.get("AGENT_CLIENT_SOURCES", "false"))


def client_month_enabled() -> bool:
    """
    Per-client MONTH builder switch (AGENT_CLIENT_MONTH). OFF by default = zero
    behavior change: build_client_month returns ok:False and touches nothing (no
    render, no host, no calendar write). When ON (and client_sources_enabled() is
    also armed), Echo may assemble a full month of APPROVABLE DRAFT calendar rows for
    a client gym FROM THAT GYM'S OWN UPLOADED PHOTOS/VIDEOS, pairing each day's approved
    fact with a real uploaded image, and upsert them to the shared content_calendar
    (gym_id = the tenant base). A client with NO uploaded media gets NO calendar: the
    builder WAITS (writes nothing, awaiting_media) and the portal shows a red "upload
    your media" banner. Echo NEVER renders an infographic-only calendar for a client;
    the house-infographic fallback is LASSO's OWN dogfood calendar only. Every row is
    PAUSED (pending) and held for human approval; a caption carrying any of the gym's
    banned words is DROPPED, never emitted. Nothing here publishes and no gate is
    weakened. Arm by hand in Railway env.
    """
    return _truthy(os.environ.get("AGENT_CLIENT_MONTH", "false"))


def review_window_days() -> int:
    """
    The review cycle length in days (env AGENT_REVIEW_WINDOW_DAYS, default 14).
    The cycle report (day30.py assembler) windows its metrics on this; the
    pre-Echo posting-cadence baseline comparison stays on its own fixed 30-day
    basis so the before/after story remains apples to apples.
    """
    try:
        return max(1, int(os.environ.get("AGENT_REVIEW_WINDOW_DAYS", "14")))
    except ValueError:
        return 14


def review_cycle_enabled() -> bool:
    """
    Review cycle automation switch. OFF by default = zero behavior change (the
    cycle report stays an on-demand read-only CLI; no ask ever fires). ON, the
    creative refresh ask fires once per review cycle per account (an ops alert
    asking for fresh photos/clips), stamped in kv so a re-run never re-asks.
    """
    return _truthy(os.environ.get("AGENT_REVIEW_CYCLE_ENABLED", "false"))


def book_campaign_every_n_days() -> int:
    """Book campaign frequency cap. At most one book post every N calendar days per
    account. N=1 means uncapped (arms the same every-day behavior as before). Arm by
    setting AGENT_BOOK_CAMPAIGN_EVERY_N_DAYS (e.g. 3) alongside the book campaign flag.
    Default: 1 (off — no change to existing behavior)."""
    try:
        return max(1, int(os.environ.get("AGENT_BOOK_CAMPAIGN_EVERY_N_DAYS", "1")))
    except ValueError:
        return 1


def category_max_consecutive() -> int:
    """Hard consecutive cap for campaign categories (book, podcast, summit). No
    campaign category may post more than this many days in a row per account.
    0 means no cap. The fallback (feed) is never gated.
    Arm by setting AGENT_CATEGORY_MAX_CONSECUTIVE (e.g. 2).
    Default: 0 (off — no change to existing behavior)."""
    try:
        return max(0, int(os.environ.get("AGENT_CATEGORY_MAX_CONSECUTIVE", "0")))
    except ValueError:
        return 0


def media_inbox_enabled() -> bool:
    """
    Media inbox switch (Stage 2). OFF by default = zero behavior change: no
    adapter payload is accepted, nothing is staged, no table is touched. ON,
    provider adapters (GHL, WhatsApp, the upload endpoint) queue client media
    through the one inbox: sender phone resolved to a tenant (never guessed;
    unknown senders are held with one ops alert), idempotent by content hash.
    """
    return _truthy(os.environ.get("AGENT_MEDIA_INBOX_ENABLED", "false"))


def ghl_intake_enabled() -> bool:
    """
    GHL intake adapter switch (Stage 2). OFF by default = the webhook handler
    refuses everything: nothing verified, fetched, staged, or replied. ON, a
    signed GHL message webhook captures photo attachments immediately (carrier
    URLs expire) into the media inbox, and a video MIME auto-replies with the
    tenant's tokenized upload link. Signature (Ed25519, X-GHL-Signature) is
    verified BEFORE the payload is parsed; the public key env is read lazily.
    """
    return _truthy(os.environ.get("AGENT_GHL_INTAKE_ENABLED", "false"))


def whatsapp_intake_enabled() -> bool:
    """
    WhatsApp (WABA) intake adapter switch (Stage 2). OFF by default = the
    webhook handler refuses everything. DO NOT ARM until Meta App Review grants
    whatsapp_business_messaging for this use (see whatsapp_intake.py header).
    ON, a signed WABA webhook (X-Hub-Signature-256, HMAC-SHA256 with the app
    secret) downloads media to the 16MB WABA ceiling and queues it through the
    same media inbox as every other lane.
    """
    return _truthy(os.environ.get("AGENT_WHATSAPP_INTAKE_ENABLED", "false"))


def tenant_brain_enabled() -> bool:
    """
    Per-gym tenant brain switch (Stage 2). OFF by default = zero behavior
    change: no events record, no rotation filtering, prompts untouched. ON,
    portal learning events (approve streak, edit diff, deny reason, kill)
    append to brains/<tenant>.md and drafting reads it ALONGSIDE the voice doc:
    killed concepts excluded from that tenant's rotation only, caption style
    rules and deny reasons folded into prompts. The brain NEVER adds facts:
    every prompt line passes the fabrication gate first.
    """
    return _truthy(os.environ.get("AGENT_TENANT_BRAIN_ENABLED", "false"))


# ---- Opus video factory (back-catalog clip pipeline) -------------------------
def opus_factory_enabled() -> bool:
    """
    Opus video factory master switch. OFF by default = zero behavior change:
    the scan returns nothing, the CLI refuses, nothing is score-gated, tagged,
    captioned, or drafted. ON, the factory enumerates finished Opus clips across
    ALL projects (no allowlist), drops anything below the score floor first,
    tags survivors to a bucket from their transcript, checks the hook, writes an
    evergreen caption from the transcript + approved facts only, dedupes against
    a ledger, and routes each survivor to a calendar slot as a DRAFT held for
    approval. Never publishes.
    """
    return _truthy(os.environ.get("AGENT_OPUS_FACTORY_ENABLED", "false"))


def opus_score_floor() -> float:
    """Opus virality score hard floor (env AGENT_OPUS_SCORE_FLOOR, default 90).
    A clip below this is dropped BEFORE any other factory work."""
    try:
        return float(os.environ.get("AGENT_OPUS_SCORE_FLOOR", "90"))
    except ValueError:
        return 90.0


def opus_duration_min() -> float:
    """Shortest Opus clip the factory will consider (default 15s)."""
    try:
        return float(os.environ.get("AGENT_OPUS_DURATION_MIN", "15"))
    except ValueError:
        return 15.0


def opus_duration_max() -> float:
    """Longest Opus clip the factory will consider (default 95s)."""
    try:
        return float(os.environ.get("AGENT_OPUS_DURATION_MAX", "95"))
    except ValueError:
        return 95.0


def opus_podcast_show() -> str:
    """The podcast show name used to recognize podcast-sourced Opus clips by
    their project title (env AGENT_OPUS_PODCAST_SHOW, default the LASSO show).
    A clip whose source_title contains this is tagged bucket=podcast directly."""
    return os.environ.get("AGENT_OPUS_PODCAST_SHOW", "Gym Marketing Made Simple")


def opus_relevance_floor() -> float:
    """Tier the bucket tagger uses: a non-podcast clip whose transcript relevance
    is below this (env AGENT_OPUS_RELEVANCE_FLOOR, default 0.65) or matches no
    theme is HELD, never drafted."""
    try:
        return float(os.environ.get("AGENT_OPUS_RELEVANCE_FLOOR", "0.65"))
    except ValueError:
        return 0.65


def opus_weekly_cap() -> int:
    """Most Opus factory clips that may be drafted into any one ISO week across
    all buckets (env AGENT_OPUS_WEEKLY_CAP, default 2). Protects the calendar
    from a back-catalog flood."""
    try:
        return max(0, int(os.environ.get("AGENT_OPUS_WEEKLY_CAP", "2")))
    except ValueError:
        return 2


# ---- Native clipper (episode video -> 4-5 finished vertical Reels, inside Echo) ----
# Replaces third-party clip platforms. Phase 1 is SELECTION only (episode intake,
# word-level transcription, Claude moment picking, dry-run plan). Rendering is a
# separate Phase 2. Master flag OFF: no intake, no transcription, no LLM call.
# Secrets (transcription + LLM keys) are read by env var NAME only, never logged.
CLIPPER_TRANSCRIBE_KEY_ENV = "AGENT_TRANSCRIBE_API_KEY"  # name only, not the value
CLIPPER_LLM_KEY_ENV = "ANTHROPIC_API_KEY"               # name only, not the value


def clipper_enabled() -> bool:
    """Native clipper master switch. OFF by default = zero behavior change: the CLI
    refuses, nothing is staged, transcribed, or sent to the LLM. ON, an episode
    video can be staged, transcribed with word-level timestamps, and fed to Claude
    for moment selection (Phase 1 stops at the dry-run plan; rendering is Phase 2)."""
    return _truthy(os.environ.get("AGENT_CLIPPER_ENABLED", "false"))


def clipper_score_floor() -> float:
    """Honest 0-100 strength floor for a candidate moment (env
    AGENT_CLIPPER_SCORE_FLOOR, default 80). Anything below is dropped."""
    try:
        return float(os.environ.get("AGENT_CLIPPER_SCORE_FLOOR", "80"))
    except ValueError:
        return 80.0


def clipper_min_sec() -> float:
    """Shortest candidate moment the selector keeps (env AGENT_CLIPPER_MIN_SEC,
    default 30s)."""
    try:
        return float(os.environ.get("AGENT_CLIPPER_MIN_SEC", "30"))
    except ValueError:
        return 30.0


def clipper_max_sec() -> float:
    """Longest candidate moment the selector keeps (env AGENT_CLIPPER_MAX_SEC,
    default 90s)."""
    try:
        return float(os.environ.get("AGENT_CLIPPER_MAX_SEC", "90"))
    except ValueError:
        return 90.0


def clipper_target_count() -> int:
    """How many candidate moments to ask Claude for (env AGENT_CLIPPER_TARGET_COUNT,
    default 5; the product target is 4-5 finished Reels per episode)."""
    try:
        return max(1, int(os.environ.get("AGENT_CLIPPER_TARGET_COUNT", "5")))
    except ValueError:
        return 5


def clipper_model() -> str:
    """The Claude model used for moment selection (judgment work), env
    AGENT_CLIPPER_MODEL, default Opus 4.8."""
    return os.environ.get("AGENT_CLIPPER_MODEL", "claude-opus-4-8")


def clipper_cache_dir() -> str:
    """Where episode transcripts are cached so re-runs never re-transcribe (env
    AGENT_CLIPPER_CACHE_DIR, default /data/clipper on the persistent volume)."""
    return os.environ.get("AGENT_CLIPPER_CACHE_DIR", "/data/clipper")


def clipper_render_enabled() -> bool:
    """Second flag under the master clipper switch. Phase 2 rendering (cut, caption,
    brand frame) is OFF even when the master AGENT_CLIPPER_ENABLED is ON. Requires
    ffmpeg on PATH. Set AGENT_CLIPPER_RENDER_ENABLED=true to arm."""
    return _truthy(os.environ.get("AGENT_CLIPPER_RENDER_ENABLED", "false"))


def clipper_render_output_dir() -> str:
    """Where rendered Reels are written (env AGENT_CLIPPER_RENDER_DIR, default
    /data/clipper/render on the persistent volume)."""
    return os.environ.get("AGENT_CLIPPER_RENDER_DIR", "/data/clipper/render")


def clipper_broll_enabled() -> bool:
    """B-roll text-card overlay in the render pipeline. OFF by default.
    Set AGENT_CLIPPER_BROLL_ENABLED=true to arm. Requires render also armed."""
    return _truthy(os.environ.get("AGENT_CLIPPER_BROLL_ENABLED", "false"))


# ---- Video editor (Option A: Echo directs, Higgsfield renders) --------------------
# The video editor turns a full podcast episode into finished, ad-ready clips with
# AI b-roll overlays rendered by Higgsfield. Three flags, all default OFF, layered:
#   AGENT_VIDEO_EDITOR_ENABLED  master switch for the video editor pipeline
#   AGENT_VIDEO_BROLL_ENABLED   plan b-roll beats + composite overlays
#   AGENT_VIDEO_RENDER          actually CALL Higgsfield (spends real credits)
# When VIDEO_RENDER is OFF the pipeline plans a b-roll manifest and projects cost
# but renders zero overlays (or uses the text-card fallback). Higgsfield is only
# reachable through an interactive Claude session (claude.ai MCP), never the
# headless Railway cron, so the render arm is Claude-in-the-loop by design.


def video_editor_enabled() -> bool:
    """Video editor master switch. OFF by default. Set AGENT_VIDEO_EDITOR_ENABLED=true."""
    return _truthy(os.environ.get("AGENT_VIDEO_EDITOR_ENABLED", "false"))


def video_broll_enabled() -> bool:
    """B-roll planning + overlay compositing in the video editor. OFF by default.
    Set AGENT_VIDEO_BROLL_ENABLED=true. Requires the editor master also armed."""
    return _truthy(os.environ.get("AGENT_VIDEO_BROLL_ENABLED", "false"))


def video_render_enabled() -> bool:
    """The Higgsfield-call arm: when ON, overlay beats are rendered by calling
    Higgsfield (real credit spend). OFF by default. Set AGENT_VIDEO_RENDER=true.
    When OFF, the pipeline plans + projects cost but spends nothing."""
    return _truthy(os.environ.get("AGENT_VIDEO_RENDER", "false"))


def video_broll_cap() -> int:
    """Max MOTION b-roll renders per episode (Higgsfield video, hard cost guard).
    Hitting the cap stops and surfaces, never silently spends. Env
    AGENT_VIDEO_BROLL_CAP, default 6."""
    try:
        return max(0, int(os.environ.get("AGENT_VIDEO_BROLL_CAP", "6")))
    except (TypeError, ValueError):
        return 6


def video_stills_enabled() -> bool:
    """Arms Nano Banana (Gemini) STILL card overlays in the video editor. OFF by
    default. Set AGENT_VIDEO_STILLS_ENABLED=true. Reuses the SAME creative_studio
    Gemini pipeline / model / key as organic cards (one image source of truth)."""
    return _truthy(os.environ.get("AGENT_VIDEO_STILLS_ENABLED", "false"))


def video_stills_cap() -> int:
    """Max Nano Banana still-card renders per episode (separate, cheaper cap).
    Hitting the cap stops and surfaces, never silently spends. Env
    AGENT_VIDEO_STILLS_CAP, default 6."""
    try:
        return max(0, int(os.environ.get("AGENT_VIDEO_STILLS_CAP", "6")))
    except (TypeError, ValueError):
        return 6


def video_cost_per_still() -> float:
    """Projected credit cost of one Nano Banana still card, for the cost report.
    Env AGENT_VIDEO_COST_PER_STILL overrides; default 2.0."""
    override = os.environ.get("AGENT_VIDEO_COST_PER_STILL")
    if override:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    return 2.0


# ---- A+ finish (Phase 1 polish, Phase 2 fidelity, Phase 3 hero) -------------
# All OFF by default; the base look is unchanged unless armed by hand.

def video_polish_enabled() -> bool:
    """A+ finish pass: caption pop-motion, b-roll cross-dissolves, host color
    grade, hook + CTA cards. OFF by default. Set AGENT_VIDEO_POLISH=true.
    Pure ffmpeg finish — no extra credits, no external calls."""
    return _truthy(os.environ.get("AGENT_VIDEO_POLISH", "false"))


def video_nano_intro_enabled() -> bool:
    """Open each reel with a Gemini/Nano-generated house-style infographic that
    holds ~2s then SLIDES AWAY to reveal the host footage (replaces the code-built
    intro card). Headless-capable (creative_studio + AGENT_NANO_API_KEY). OFF by
    default. Set AGENT_VIDEO_NANO_INTRO=true."""
    return _truthy(os.environ.get("AGENT_VIDEO_NANO_INTRO", "false"))


def video_jumpcuts_enabled() -> bool:
    """Silence/filler jump-cut pacing: remove inter-word gaps longer than
    video_jumpcut_gap() so the clip is tight. OFF by default (content-affecting).
    Set AGENT_VIDEO_JUMPCUTS=true."""
    return _truthy(os.environ.get("AGENT_VIDEO_JUMPCUTS", "false"))


def video_punch_zoom_enabled() -> bool:
    """Punch-in zoom centered on the detected face for podcast reels.
    ON by default when render is enabled. Set AGENT_VIDEO_PUNCH_ZOOM=false to disable."""
    return _truthy(os.environ.get("AGENT_VIDEO_PUNCH_ZOOM", "true"))


# ---- fal.ai headless b-roll renderer ----------------------------------------

def fal_api_key() -> str:
    """API key for fal.ai headless b-roll rendering. Set AGENT_FAL_API_KEY in
    Railway env. When set + AGENT_VIDEO_RENDER=true, motion overlays render
    headlessly via fal.ai instead of requiring an interactive Higgsfield MCP session."""
    return os.environ.get("AGENT_FAL_API_KEY", "").strip()


def fal_video_model() -> str:
    """fal.ai model for motion b-roll clips. Env AGENT_FAL_VIDEO_MODEL,
    default fal-ai/kling-video/v1.6/standard/text-to-video."""
    return (os.environ.get("AGENT_FAL_VIDEO_MODEL", "")
            or "fal-ai/kling-video/v1.6/standard/text-to-video").strip()


def fal_image_model() -> str:
    """fal.ai model for still image overlays. Env AGENT_FAL_IMAGE_MODEL,
    default fal-ai/flux/schnell."""
    return (os.environ.get("AGENT_FAL_IMAGE_MODEL", "")
            or "fal-ai/flux/schnell").strip()


# ---- Higgsfield headless b-roll renderer ------------------------------------

def hf_api_key() -> str:
    """Higgsfield API key presence check. Set HF_API_KEY + HF_API_SECRET
    (or HF_KEY) in Railway. When set, Higgsfield is preferred over fal.ai
    for b-roll rendering."""
    key = os.environ.get("HF_KEY", "").strip()
    if key:
        return key
    return os.environ.get("HF_API_KEY", "").strip()


def hf_video_app() -> str:
    """Higgsfield SDK application path for text-to-video. Env AGENT_HF_VIDEO_APP.
    Default: kling-video/v3.0/text-to-video."""
    return (os.environ.get("AGENT_HF_VIDEO_APP", "")
            or "kling-video/v3.0/text-to-video").strip()


def hf_image_app() -> str:
    """Higgsfield SDK application path for text-to-image. Env AGENT_HF_IMAGE_APP.
    Default: bytedance/seedream/v4/text-to-image (confirmed from SDK docs)."""
    return (os.environ.get("AGENT_HF_IMAGE_APP", "")
            or "bytedance/seedream/v4/text-to-image").strip()


def video_jumpcut_gap() -> float:
    """Inter-word gap (seconds) above which dead air is removed; the kept residual
    is video_jumpcut_keep(). Env AGENT_VIDEO_JUMPCUT_GAP, default 0.45."""
    try:
        return max(0.15, float(os.environ.get("AGENT_VIDEO_JUMPCUT_GAP", "0.45")))
    except (TypeError, ValueError):
        return 0.45


def video_jumpcut_keep() -> float:
    """Residual breathing room (seconds) left in place of a removed gap.
    Env AGENT_VIDEO_JUMPCUT_KEEP, default 0.12."""
    try:
        return max(0.0, float(os.environ.get("AGENT_VIDEO_JUMPCUT_KEEP", "0.12")))
    except (TypeError, ValueError):
        return 0.12


def video_broll_resolution() -> str:
    """Motion b-roll render resolution: '720p' (7.5 cr) or '1080p' (10 cr).
    Env AGENT_VIDEO_BROLL_RESOLUTION, default '720p'."""
    r = (os.environ.get("AGENT_VIDEO_BROLL_RESOLUTION", "720p") or "720p").strip().lower()
    return r if r in ("720p", "1080p") else "720p"


def video_still_resolution() -> str:
    """Nano Banana still-card resolution: '1k' or '2k' (same 2-credit cost).
    Env AGENT_VIDEO_STILL_RESOLUTION, default '2k' (crisper card text, no extra cost)."""
    r = (os.environ.get("AGENT_VIDEO_STILL_RESOLUTION", "2k") or "2k").strip().lower()
    return r if r in ("1k", "2k", "4k") else "2k"


def podcast_auto_enabled() -> bool:
    """Deployed Monday auto-ingest: pull the newest episode from the Drive folder,
    edit it, and schedule the week as HELD drafts. OFF by default. Set
    AGENT_PODCAST_AUTO_ENABLED=true on Railway to arm. Nothing publishes."""
    return _truthy(os.environ.get("AGENT_PODCAST_AUTO_ENABLED", "false"))


def podcast_drive_folder_id() -> str:
    """Google Drive folder id that Riverside auto-exports episodes into; the auto
    job pulls the newest video from here. Env AGENT_PODCAST_DRIVE_FOLDER_ID."""
    return os.environ.get("AGENT_PODCAST_DRIVE_FOLDER_ID", "")


def gdrive_service_account_json() -> str:
    """Path to (or inline JSON of) a Google service-account key with read access to
    the podcast Drive folder, for HEADLESS pulls on Railway (the claude.ai Drive
    connector is interactive-only and unavailable in cron). Env
    AGENT_GDRIVE_SA_JSON."""
    return os.environ.get("AGENT_GDRIVE_SA_JSON", "")


def podcast_account_keys() -> list:
    """Accounts the auto-scheduled podcast clips cross-post to. Env
    AGENT_PODCAST_ACCOUNT_KEY is comma-separated (e.g. 'lasso_ig,lasso_fb') so one
    clip drafts to every listed account. Default: [episode-inbox tenant]."""
    raw = os.environ.get("AGENT_PODCAST_ACCOUNT_KEY", "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys or [episode_inbox_tenant()]


def podcast_account_key() -> str:
    """First account the auto-scheduled podcast clips post under (back-compat).
    See podcast_account_keys() for the full cross-post list."""
    return podcast_account_keys()[0]


def podcast_auto_max_clips() -> int:
    """Max clips the auto job schedules per episode. Env
    AGENT_PODCAST_AUTO_MAX_CLIPS, default 5."""
    try:
        return max(1, int(os.environ.get("AGENT_PODCAST_AUTO_MAX_CLIPS", "5")))
    except (TypeError, ValueError):
        return 5


def video_hero_model() -> str:
    """Optional top-tier model for the single highest-score (hero) beat per reel,
    e.g. 'veo3_1' (~22 cr). Empty = every motion beat uses the standard model.
    Env AGENT_VIDEO_HERO_MODEL, default '' (off)."""
    return (os.environ.get("AGENT_VIDEO_HERO_MODEL", "") or "").strip()


def video_broll_kind() -> str:
    """Overlay type: 'video' (motion, ~7.5 cr each) or 'image' (Ken-Burns still,
    ~2 cr each). Env AGENT_VIDEO_BROLL_KIND, default 'video'."""
    kind = (os.environ.get("AGENT_VIDEO_BROLL_KIND", "video") or "video").strip().lower()
    return kind if kind in ("video", "image") else "video"


def video_cost_per_overlay() -> float:
    """Projected credit cost of one overlay render, for the pre-render cost report.
    Env AGENT_VIDEO_COST_PER_OVERLAY overrides; default depends on overlay kind
    (video 7.5, image 2.0) preflighted against Higgsfield on 2026-07-20."""
    override = os.environ.get("AGENT_VIDEO_COST_PER_OVERLAY")
    if override:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    return 7.5 if video_broll_kind() == "video" else 2.0


def video_output_dir() -> str:
    """Where finished clips are written. Env AGENT_VIDEO_OUTPUT_DIR,
    default /data/clipper/video."""
    return os.environ.get("AGENT_VIDEO_OUTPUT_DIR", "/data/clipper/video")


def video_overlay_cache_dir() -> str:
    """Where rendered Higgsfield overlay assets are cached for reuse across re-runs
    (never re-pay). Env AGENT_VIDEO_OVERLAY_CACHE, default /data/clipper/overlays."""
    return os.environ.get("AGENT_VIDEO_OVERLAY_CACHE", "/data/clipper/overlays")


def video_aspects() -> list:
    """Which aspect ratios to export. Env AGENT_VIDEO_ASPECTS (csv of 9:16,1:1),
    default both."""
    raw = os.environ.get("AGENT_VIDEO_ASPECTS", "9:16,1:1")
    out = [a.strip() for a in raw.split(",") if a.strip() in ("9:16", "1:1")]
    return out or ["9:16", "1:1"]


def services_category_enabled() -> bool:
    """Services category for LASSO own accounts ONLY, never client accounts. OFF by default.
    Draws from brand_voice/lasso_services.md; stub file = SKIP not fabricate."""
    return _truthy(os.environ.get("AGENT_SERVICES_CATEGORY", "false"))


def category_quotas_enabled() -> bool:
    """
    Category quota enforcement for content plans. OFF by default = zero behavior
    change; existing plan logic runs exactly as before. When ON, validate_quotas()
    results are acted upon by the plan builders:

    B2B profile (LASSO own accounts):
      proof >= 2 per week  (from stored, approved social proof assets only)
      call  >= 3 per week  (direct CTA posts driving a next step)
      No category > 25% of the month

    Gym profile (client accounts):
      results   >= 4 per month  (approved before/afters and transformations only)
      offer     >= 4 per month  (only while a live offer is set; expired -> 'invite')
      faces     >= 3 per month
      community >= 5 per month
      education >= 6 per month
      invite fills remaining gaps
      No category > 25% of the month

    Summit ramp and existing weekly caps (podcast/b2b/platform/book) are unchanged.
    Human approval tap and publish-off default are untouched.
    Arm by hand: AGENT_CATEGORY_QUOTAS=true
    """
    return _truthy(os.environ.get("AGENT_CATEGORY_QUOTAS", "false"))


def intake_worker_enabled() -> bool:
    """
    Intake pipeline worker: turns incoming R2 uploads into library-ready assets.
    Distinct from AGENT_INTAKE_ENABLED which gates the upload web surface.
    Default OFF. Arm by hand.
    """
    return _truthy(os.environ.get("AGENT_INTAKE_WORKER", "false"))


def draft_on_upload_enabled() -> bool:
    """
    Immediate draft-on-upload: the instant a gym's new media is INGESTED into its
    library, draft ONE approval card per new asset for that gym instead of waiting
    for the next daily draw. Applies to every organic-social gym (portal gyms +
    LASSO house accounts). OFF by default; arm by hand with AGENT_DRAFT_ON_UPLOAD.

    NEVER weakens a gate: the card goes through the SAME draft_post + _post_and_save
    path as the daily run, so the approval gate, publish-off default, fabrication
    gate, portal-vs-Slack routing, and per-gym autonomy are all identical. A gym
    with no registry account or no voice doc is SKIPPED with one ops alert; nothing
    is fabricated.
    """
    return _truthy(os.environ.get("AGENT_DRAFT_ON_UPLOAD", "false"))


def dynamic_accounts_enabled() -> bool:
    """
    Dynamic, DB-backed client accounts: load client-gym Account records from a
    persisted registry (JSON) IN ADDITION to the hardcoded ACCOUNTS, and let
    onboarding auto-provision a new gym's Account record from its intake so scaling
    to 100+ gyms never requires hand-editing accounts.py. OFF by default: when OFF,
    only the hardcoded ACCOUNTS exist (byte-for-byte today's behavior). Auto-created
    accounts are ALWAYS inactive; tokens stay by-hand (env); nothing publishes.
    """
    return _truthy(os.environ.get("AGENT_DYNAMIC_ACCOUNTS", "false"))


def gym_registry_path() -> str:
    """Where the dynamic client-account registry JSON lives. Defaults to the /data
    volume (survives redeploys) with a local fallback for dev/tests."""
    explicit = os.environ.get("AGENT_GYM_REGISTRY_PATH")
    if explicit:
        return explicit
    if os.path.isdir("/data"):
        return "/data/gym_accounts.json"
    return "gym_accounts.json"


def social_intake_sync_enabled() -> bool:
    """
    Automatic social-intake forward: map EVERY un-routed echo_social_intake row into
    Echo (voice/proof docs + approved client_sources) and mark it routed, so no gym
    is ever stranded the way CrossFit ENG was (captured but never forwarded). OFF by
    default; arm by hand with AGENT_SOCIAL_INTAKE_SYNC. Requires Supabase creds. A
    base with no registry Account is skipped with an alert; nothing publishes.
    """
    return _truthy(os.environ.get("AGENT_SOCIAL_INTAKE_SYNC", "false"))


def onboard_automint_enabled() -> bool:
    """
    Autonomous onboarding token mint switch. OFF by default.
    When OFF, the onboard command creates the gym row and scaffolds files but
    skips intake token minting; AGENT_INTAKE_TOKEN_<KEY> env vars remain
    authoritative. Blake sets this by hand to enable. Nothing in onboarding
    arms itself or touches any Meta credential.
    """
    return _truthy(os.environ.get("AGENT_ONBOARD_AUTOMINT", "false"))


# ---- Intake token encryption key ---------------------------------------------
# Name of the env var holding the Fernet key for encrypting intake tokens at
# rest. When set: intake_tokens.mint() stores the raw token encrypted so
# /portal/gym/<key> can recover and return the upload link without storing
# the token in plaintext. When NOT set: encryption is skipped (dev mode) and
# the upload_link column stores the plaintext URL.
# Generate a key once: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Store it in Railway env only; never commit or log it.
INTAKE_ENC_KEY_ENV = "AGENT_INTAKE_ENC_KEY"

# ---- SocialAPI.ai publish lane ----------------------------------------------
# The API key is read BY NAME only (never stored, never logged). Set it by hand
# in Railway: AGENT_SOCIALAPI_KEY=sapi_key_...  Nothing in code writes it.
SOCIALAPI_KEY_ENV = "AGENT_SOCIALAPI_KEY"           # name of the env var, not the value
SOCIALAPI_BASE_URL_DEFAULT = "https://api.social-api.ai/v1"
# Name of the env var holding the Fernet key for encrypting per-brand SocialAPI
# material (brand id / connected account ids) at rest, same pattern as intake
# tokens. When unset: values are stored in plaintext in the kv table (dev mode).
# Generate once: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SOCIALAPI_ENC_KEY_ENV = "AGENT_SOCIALAPI_ENC_KEY"


# ---- StoryBrand SB7 caption engine ------------------------------------------
# When armed, the drafter uses an LLM to write captions using the SB7 framework
# (problem-first, gym as guide, customer as hero) instead of the verbatim
# TemplateGenerator. Requires ANTHROPIC_API_KEY. OFF by default.
def sb7_enabled() -> bool:
    """StoryBrand SB7 caption engine. OFF by default. Set AGENT_SB7_ENABLED=true to arm."""
    return _truthy(os.environ.get("AGENT_SB7_ENABLED", "false"))


def sb7_model() -> str:
    """Claude model for SB7 caption generation (env AGENT_SB7_MODEL).
    Defaults to Haiku 4.5 for speed and cost."""
    return os.environ.get("AGENT_SB7_MODEL", "claude-haiku-4-5-20251001")


def dedupe_forward_book_enabled() -> bool:
    """
    Forward-book deduplication job switch. OFF by default. When OFF, the job
    `agent/jobs/dedupe_forward_book.py` is a no-op (dry-run only) and makes no
    writes to content_calendar. When ON, the job groups future pending rows per
    gym by caption_hash, keeps the earliest occurrence, and moves the rest to
    'denied' with reject_reason='duplicate_purge_2026_08' via
    portal_calendar_store. Arm by hand in Railway env; only Blake enables this.
    This is a one-shot Wave 0 cleanup job — it is not part of the daily runner.
    """
    return _truthy(os.environ.get("AGENT_DEDUPE_FORWARD_BOOK", "false"))
