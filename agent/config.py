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
POSTING_SKIP_DAYS = _csv_list("AGENT_POSTING_SKIP_DAYS", ["sat"])
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
NANO_MODEL = os.environ.get("AGENT_NANO_MODEL", "gemini-3-pro-image")
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


def caption_seo_enabled() -> bool:
    """
    2026 caption SEO switch for the content brain. OFF by default = captions are
    assembled exactly as today. ON, the planner may REORDER the approved body lines
    so a line carrying the hook's key topic terms sits first after the hook. It only
    reorders or selects among APPROVED lines; it never writes new text. If no
    reorder satisfies placement, the original order is kept.
    """
    return _truthy(os.environ.get("AGENT_CAPTION_SEO_ENABLED", "false"))


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
OPUS_PROJECT_IDS = _csv_list("AGENT_OPUS_PROJECT_IDS", [])
OPUS_COLLECTION_IDS = _csv_list("AGENT_OPUS_COLLECTION_IDS", [])


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


def runway_enabled() -> bool:
    """
    Creative runway switch. OFF by default. ON, one line per account per day:
    days of approved gate-clean content left, green/amber/red, projected zero
    date; below AGENT_RUNWAY_ALERT_DAYS one debounced ops alert asks for raw
    material. Read-only over the library and the store; never posts content.
    """
    return _truthy(os.environ.get("AGENT_RUNWAY_ENABLED", "false"))


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


def intake_enabled() -> bool:
    """
    Texted-link intake switch (upload page + listener ingest). OFF by default: the
    upload page 404s everything and the ingest step never runs. Tokens are per-client
    env values (AGENT_INTAKE_TOKEN_<CLIENTKEY>), set by hand, never logged.
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
