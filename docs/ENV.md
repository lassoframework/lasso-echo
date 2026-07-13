# Echo environment variables — the complete reference

Every env var the agent or the intake web service reads, generated from a code
sweep at SHA 4213ca6. Owner legend:

- **BLAKE** — set by hand (Railway env or local shell); never in code, never committed.
- **code** — has a safe code default; override only when needed.

"Per account" = the var name embeds the account/tenant key, one per client.

`.env.example` is permission-locked for agents and lags this file; this file is
the authoritative inventory. Vars marked **(was undocumented)** appeared nowhere
before this file existed.

## Core gates (the ones that keep Echo safe)

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_ENABLED | false | BLAKE | Master switch. Nothing drafts while off. |
| AGENT_PUBLISH_ENABLED | false | BLAKE | Publish gate. Code default stays false forever; armed only in Railway. |
| AGENT_APPROVER_SLACK_ID | U06EPUUCL13 | code | The global approver. |
| AGENT_OPS_ALERTS_ENABLED | false | BLAKE | One ECHO ALERT line per pipeline failure. |
| AGENT_IDEMPOTENT_DRAFTS_ENABLED | false | BLAKE | One draft per (account, day, type); supersede/expire cards. |

## Slack

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_SLACK_BOT_TOKEN | (unset) | BLAKE | xoxb token. Never logged. |
| AGENT_SLACK_APP_TOKEN | (unset) | BLAKE | xapp token (Socket Mode). |
| AGENT_SLACK_CHANNEL_ID | "" | BLAKE | The shared default channel (LASSO client zero). Clients set Account.slack_channel instead. |

## Meta / publishing

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_LASSO_IG_TOKEN / AGENT_LASSO_IG_USER_ID | (unset) | BLAKE | Per account (LASSO IG). |
| AGENT_LASSO_FB_TOKEN / AGENT_LASSO_FB_PAGE_ID | (unset) | BLAKE | Per account (LASSO FB Page). |
| `<Account.token_env>` / `<Account.target_id_env>` | (unset) | BLAKE | Per account: every client account names its own pair in the registry. |
| META_APP_ID | (unset) | BLAKE | **(was undocumented)** App id for the token watchdog's debug_token read. |
| META_APP_SECRET | (unset) | BLAKE | **(was undocumented)** App secret for the same read. Rotate by hand only. |
| AGENT_GRAPH_API_VERSION | v23.0 | code | Graph pin (post views-migration). |
| AGENT_PUBLISH_CONFIRM_ENABLED | false | BLAKE | Read-back verify + LIVE permalink reply. |
| AGENT_TOKEN_WATCHDOG_ENABLED | false | BLAKE | Daily debug_token expiry check. |
| AGENT_TOKEN_WARN_DAYS | 7 | code | Days-out warning threshold. |

## Scheduler & cadence

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_DAILY_HOUR_UTC | 14 | code | The daily draw hour (~10am ET). |
| AGENT_SCHEDULER_ENABLED | true | code | false hands the draw to the Railway cron fallback (PROGRESS.md). |
| AGENT_SCHEDULER_STATE_DIR | /data | code | Fire-date persistence (no double fire across redeploys). |
| AGENT_POSTING_TZ | America/New_York | code | |
| AGENT_POSTING_PRIMARY_TIME | 18:30 | code | |
| AGENT_POSTING_MORNING_TIME | 07:30 | code | |
| AGENT_POSTS_PER_DAY | 1 | code | |
| AGENT_POSTING_SKIP_DAYS | (empty = 7 days/week) | code | csv, e.g. "sat". |
| AGENT_POSTING_PRIORITY_DAYS | tue,wed,thu | code | |
| AGENT_CATEGORY_ROTATION | false | BLAKE | The seven-day category schedule. ARMED in production. |
| AGENT_CATEGORY_MAX_CONSECUTIVE | 0 | code | Hard consecutive cap on campaign categories. |
| AGENT_BOOK_CAMPAIGN_EVERY_N_DAYS | 1 | code | Book frequency cap (7 = once a week). |

## Content sources & drafting

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_VOICE_DOC_PATH | brand_voice/lasso_voice.md | code | Global bible; clients set Account.voice_doc. |
| AGENT_SOURCE_DOC_PATH | brand_voice/lasso_now.md | code | |
| AGENT_SOCIAL_PROOF_PATH | brand_voice/social_proof.md | code | |
| AGENT_SOCIAL_PROOF_ENABLED | false | BLAKE | |
| AGENT_SOCIAL_PROOF_DAY | wed | code | |
| AGENT_LIBRARY_PATH | content_library | code | Global library; clients set Account.library_prefix. |
| AGENT_KNOWLEDGE_ENABLED | false | BLAKE | Knowledge brain (USE-line stats). |
| AGENT_KNOWLEDGE_DIR | brand_voice/knowledge | code | |
| AGENT_CONTENT_BRAIN_ENABLED | false | BLAKE | LASSO-only daily planner. |
| AGENT_CLIENT_SOURCES | false | BLAKE | Client accounts draft from their own approved sources. |
| AGENT_CAPTION_SEO_ENABLED | false | BLAKE | |
| AGENT_PLATFORM_VARIANTS_ENABLED | false | BLAKE | IG 5 tags / FB 2 tags. |
| AGENT_ROTATION_ENABLED | false | BLAKE | Creative variety guard. |
| AGENT_ROTATION_WINDOW_DAYS | 14 | code | |
| AGENT_ROTATION_STATE_DIR | (store) | code | Legacy state dir override. |
| AGENT_PLAN_MONTH_ENABLED | false | BLAKE | plan-month / approve-month / replan. |
| AGENT_BOOK_CAMPAIGN_ENABLED | false | BLAKE | |
| AGENT_BOOK_DIR | knowledge book dir | code | |
| AGENT_SUMMIT_CAMPAIGN_ENABLED | false | BLAKE | Auto-stops after 2026-11-08. |
| AGENT_SUMMIT_DAY | tue | code | |
| AGENT_STORIES_ENABLED | false | BLAKE | Second gate under publish for Stories. |
| AGENT_STORY_PREMADE_ENABLED | false | BLAKE | |
| AGENT_TRUST_LADDER_ENABLED / AGENT_TRUST_DRYRUN / AGENT_TRUST_AUTOPUBLISH | false | BLAKE | Trust wiring; first post never automated regardless. |
| AGENT_TENANT_BRAIN_ENABLED | false | BLAKE | Per-gym learning notes. |

## Creative studio (Gemini)

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_NANO_ENABLED | false | BLAKE | Infographic generation. |
| AGENT_NANO_API_KEY | (unset) | BLAKE | Gemini key. Never logged. |
| AGENT_NANO_MODEL | gemini-3-pro-image | code | |
| AGENT_IMAGE_ASPECT / AGENT_IMAGE_PIXELS | 4:5 / 1080x1350 | code | Feed target. |
| AGENT_STORY_ASPECT / AGENT_STORY_PIXELS | 9:16 / 1080x1920 | code | Story target. |
| AGENT_SPEND_CAP_ENABLED | false | BLAKE | Per-account daily generation cap. |
| AGENT_GEMINI_DAILY_CAP | 40 | code | Calls/day/account under the cap. |
| AGENT_OCR_CHECK_ENABLED | false | BLAKE | Headline OCR warning (never blocks). |
| AGENT_AUTOTAG_ENABLED | false | BLAKE | DAM auto-tag on ingest. |
| AGENT_CONSENT_GUARD_ENABLED | false | BLAKE | People=consent gate. |

## Hosting (R2/S3)

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_HOSTING_ENABLED | false | BLAKE | |
| AGENT_S3_BUCKET / AGENT_S3_ENDPOINT / AGENT_S3_REGION | (unset) | BLAKE | |
| AGENT_S3_ACCESS_KEY_ID / AGENT_S3_SECRET_ACCESS_KEY | (unset) | BLAKE | Never logged. |
| AGENT_S3_PUBLIC_BASE_URL | (unset) | BLAKE | Public CDN base. |
| AGENT_S3_MAX_RETRIES | code default | code | |

## Intake (upload page + ingest worker)

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_INTAKE_ENABLED | false | BLAKE | Gates the page AND the worker AND doc intake. |
| AGENT_INTAKE_TOKEN_`<CLIENTKEY>` | (unset) | BLAKE | Per account: the tokenized upload link. |
| AGENT_UPLOAD_BASE_URL | (unset) | BLAKE | Public base of the intake web service (also builds the upload_url the portal API returns). |
| AGENT_INTAKE_PORTAL_ORIGIN | "" (same-origin only) | BLAKE | The ONE origin allowed to POST JSON intakes cross-origin (the ops portal). Never all origins. |
| PORT | 8080 | code | Railway sets it on the web service. |
| AGENT_INTAKE_MAX_FILE_MB / AGENT_INTAKE_MAX_REQUEST_MB | code defaults | code | |
| AGENT_INTAKE_RATE_PER_MINUTE | code default | code | Per-IP rate limit. |
| AGENT_INTAKE_POLL_MINUTES | 5 | code | Worker pass interval. |
| AGENT_DOC_INTAKE_ENABLED | false | BLAKE | PDF -> held draft posts. |
| AGENT_MEDIA_INBOX_ENABLED / AGENT_MEDIA_INBOX_DIR | false / code | BLAKE | Provider-agnostic texted-media queue. |
| AGENT_GHL_INTAKE_ENABLED / AGENT_GHL_PUBLIC_KEY | false / (unset) | BLAKE | Ed25519-verified GHL webhook. |
| AGENT_WHATSAPP_INTAKE_ENABLED / AGENT_WHATSAPP_APP_SECRET | false / (unset) | BLAKE | Do not arm before App Review grants the scope. |

## Podcast / clipper / Opus

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_PODCAST_ENABLED / AGENT_PODCAST_FEED_URL / AGENT_PODCAST_POLL_MINUTES | false / (unset) / 60 | BLAKE | |
| AGENT_CLIPPER_ENABLED | false | BLAKE | Phase 1 selection. |
| AGENT_CLIPPER_RENDER_ENABLED / AGENT_CLIPPER_RENDER_DIR | false / code | BLAKE | Phase 2 render. |
| AGENT_CLIPPER_SCORE_FLOOR / _MIN_SEC / _MAX_SEC / _TARGET_COUNT / _MODEL / _CACHE_DIR | code defaults | code | |
| AGENT_TRANSCRIBE_API_KEY | (unset) | BLAKE | Or install faster-whisper. |
| AGENT_WHISPER_MODEL | code default | code | |
| ANTHROPIC_API_KEY | (unset) | BLAKE | Clipper moment selection. |
| AGENT_EPISODE_INBOX_ENABLED / _PREFIX / _TENANT / _POLL_MINUTES | false / code / lasso_episodes / 5 | BLAKE | Riverside drop watcher. |
| AGENT_EPISODE_NUDGE_TIME / _WINDOW_DAYS | 09:00 / 2 | code | Monday nudge. |
| AGENT_OPUS_ENABLED / AGENT_OPUS_POLL_ENABLED / AGENT_OPUS_FACTORY_ENABLED | false | BLAKE | Legacy Opus lanes. |
| OPUS_API_KEY | (unset) | BLAKE | **(was undocumented at times)** rotate by hand. |
| AGENT_OPUS_API_BASE / _ORG_ID / _PODCAST_SHOW / _PROJECT_IDS / _COLLECTION_IDS / _SCORE_FLOOR / _RELEVANCE_FLOOR / _DURATION_MIN / _DURATION_MAX / _WEEKLY_CAP / _POLL_MINUTES / _STATE_DIR | code defaults | code | |

## Reporting / review / digest

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_REPORTING_ENABLED | false | BLAKE | Daily Graph snapshots (views, never impressions). |
| AGENT_REPORTS_DIR / AGENT_BASELINE_DIR | code defaults | code | |
| AGENT_GRADE_ENABLED | false | BLAKE | Social Grade. |
| AGENT_MONTHLY_REVIEW_ENABLED / AGENT_REVIEW_CYCLE_ENABLED / AGENT_REVIEW_WINDOW_DAYS | false / false / 14 | BLAKE | |
| AGENT_DIGEST_ENABLED / AGENT_DIGEST_HOUR_UTC | false / 23 | BLAKE | |
| AGENT_WEEKLY_REPORT_ENABLED | false | BLAKE | Sunday operator card. |
| AGENT_BRAIN_PROPOSALS_ENABLED / AGENT_BRAIN_HOUR_UTC | false / code | BLAKE | Nightly read-only proposer. |
| AGENT_RUNWAY_ENABLED / AGENT_RUNWAY_ALERT_DAYS | false / 7 | BLAKE | |
| AGENT_COMMENTS_ENABLED | false | BLAKE | Comment engine (held cards, no auto DMs). |
| AGENT_PREFLIGHT_MIN_LIBRARY / AGENT_PREFLIGHT_WARN_LIBRARY | 15 / 30 | code | |

## Storage / infra

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_DB_PATH | /data/echo.db (else ./echo.db) | code | |
| AGENT_DATA_DIR | /data | code | |
| AGENT_PENDING_PATH | pending_drafts.json (legacy) | code | Migrated store path. |
| AGENT_POST_LOG_PATH | post_log.jsonl (legacy) | code | |
| AGENT_BACKUP_ENABLED / _HOUR_UTC / _RETENTION_DAYS | false / 2 / 14 | BLAKE | Nightly store snapshot to R2. |
| AGENT_OPUS_STATE_DIR | code default | code | |

## Facebook connect / GBP

| Var | Default | Owner | Notes |
|---|---|---|---|
| AGENT_CONNECT_ENABLED / _TOKENS_ENABLED / _GRADE_ENABLED | false | BLAKE | /connect surface. |
| AGENT_CONNECT_BASE_URL / AGENT_CONNECT_PORT | (unset) / code | BLAKE | |
| AGENT_GBP_ENABLED | false | BLAKE | Google Business Profile branch. |
| AGENT_GBP_ACCESS_TOKEN / AGENT_GBP_ACCOUNT_ID / AGENT_GBP_LOCATION_ID | (unset) | BLAKE | |
| AGENT_GBP_API_BASE / AGENT_GBP_DEFAULT_CTA / AGENT_GBP_CTA_URL | code defaults | code | |

## Previously read in code but documented nowhere (now closed)

- **META_APP_ID / META_APP_SECRET** — the token watchdog's debug_token app
  credentials. Without them the watchdog reports token state as unknown.
- **OPUS_API_KEY** — the Opus API key env (no AGENT_ prefix; easy to miss).
- **AGENT_WHATSAPP_APP_SECRET** — HMAC key for the WhatsApp webhook signature.
- **AGENT_SCHEDULER_STATE_DIR / AGENT_ROTATION_STATE_DIR / AGENT_OPUS_STATE_DIR**
  — state-dir overrides used by tests; production leaves them unset.
- **PORT** — the intake web service bind (Railway injects it).

Keep this file in sync: any new `os.environ` read lands here in the same commit.
