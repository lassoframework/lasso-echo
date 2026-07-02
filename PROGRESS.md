# Echo Build Tracker

Living tracker for the Echo social agent build. This markdown is the source of
truth; the HTML dashboard (`echo_build_tracker.html`) is the visual view. The
full organic-system scope lives in `BUILD_SPEC.md`.

Status key: [x] done  ·  [~] built + tested in reference repo, push/deploy pending  ·  [ ] not started

Last updated: 2026-07-02 evening (STAGE 2 BUILDOUT landed, suite 226 green: multi-client
foundation, texted-link intake web + listener ingest, draft-bible CLI, Social Grade v1,
social proof cards, Meta App Review kit + Stage 2 runbook. Every new flag defaults OFF
(AGENT_INTAKE_ENABLED, AGENT_GRADE_ENABLED, AGENT_SOCIAL_PROOF_ENABLED). Morning:
scheduler hardened (loud no-card alerts + persisted run date). Production armed set
unchanged: publish + stories + Tier 1 via Railway env; code defaults stay OFF.)

---

## Stage 0 — Foundation
- [x] Canonical LASSO brand bible (`brand_voice/lasso_voice.md`)
- [x] Reference repo scaffolded (`lasso-echo`), own body, Ranger spine as pattern
- [x] Gates baked into code (approval, draft-only, trust ladder, no-fabrication)
- [x] Test suite green (175, deployed at cd8000b)
- [x] Stage 1 build prompt for Claude Code
- [x] Railway + separation plan documented (own project, own service, #echoclaude)
- [x] Brain hook stubbed (read-only, proposes, never rewrites voice)

## Stage 1 — LASSO only: draft + approve + publish (DRAFT-ONLY)
- [x] Daily drafter, one feed post per account
- [x] Slack approval cards (Approve / Edit / Skip) to #echoclaude
- [x] Meta publisher with draft-only guard (publish flag OFF)
- [x] Post logging (no tokens)
- [x] lasso-echo repo (private), Railway project + echo service + env vars
- [x] Meta App "LASSO Social Poster" (Dev mode ok for own accounts)
- [x] Per-account tokens + ids set by hand
- [x] Slack app + #echoclaude wired; first cards proven; app renamed to Echo
- [x] Voice doc loading fixed (real bible on Railway)
- [x] CTA rotation (growth-biased, placeholder-filtered)
- [x] Hashtags capped to 5; bible updated for 2026 (3 to 5 tags, caption SEO)
- [x] Carousel support (folder = carousel, draft-only)
- [x] Reels support (draft-only, a video = a Reel)
- [x] Growth pack pushed via Claude Code + redeployed
- [x] Inline creative preview on the approval card (see the image before approving)
- [x] Creative Studio module wired (Nano Banana infographics, flag AGENT_NANO_ENABLED OFF)
- [x] Media hosting shipped: S3-compatible, 200-client hardened (tenant-scoped, dedupe, retry), wired into drafts, flag OFF; stand up bucket + creds by hand to arm
- [x] Infographics target 4:5 PORTRAIT (1080x1350) for the IG/FB feed; V3 palette + clean house style locked; aspect tunable via AGENT_IMAGE_ASPECT
- [x] Dropped personal FB: blake_personal marked inactive (Meta ended personal-profile publishing 2018); run-daily drafts lasso_ig + lasso_fb only (record kept)
- [~] Content brain: drafts the daily post from the source doc (brand_voice/lasso_now.md)
      across the 5 pillars, growth CTA, 5 hashtags, no fabrication; flag AGENT_CONTENT_BRAIN_ENABLED OFF
- [~] Google Business Profile posting branch (local posts): gbp_publisher, draft-only guard,
      routing (GBP -> gbp_publisher, IG/FB -> meta_publisher), content-brain GBP variant
      (trimmed summary, one image, CTA button, no hashtags); flag AGENT_GBP_ENABLED OFF. See BUILD_SPEC.md Addendum A
- [x] Stories draft path: one 9:16 (1080x1920) Story per account per day alongside the feed
      post, reusing the day's approved creative (9:16 re-render via per-use aspect when the
      studio is armed, else the feed image as is); no caption, PENDING in the same card flow,
      loudly labeled STORY. Publish path (IG STORIES container / FB photo_stories) sits behind
      BOTH the publish flag AND AGENT_STORIES_ENABLED (code default OFF; ARMED in production)
- [~] Caption SEO (2026): content brain front-loads the hook and moves a body line carrying the
      hook's topic terms first among the bodies; reorder of APPROVED lines only, never new text;
      flag AGENT_CAPTION_SEO_ENABLED OFF
- [~] Per-platform caption variants: IG keeps up to 5 approved tags, FB Page keeps at most 2 at
      the end; selection only from the approved set; flag AGENT_PLATFORM_VARIANTS_ENABLED OFF
### Fable 5 review - Tier 1 hardening (2026-07-01, deployed at cd8000b; all four flags
### code default OFF, ARMED in production)
- [x] Idempotent daily drafts + card supersede/expire: one draft per (account, day, type);
      a same-content re-run returns the existing draft (no duplicate card); changed content
      SUPERSEDES the old card (edited in place, buttons removed); a pending card whose day
      passed EXPIRES the same way; stale approve on either = friendly no-op. Flag
      AGENT_IDEMPOTENT_DRAFTS_ENABLED (ARMED in production)
- [x] Ops alerts: one "ECHO ALERT:" line to #echoclaude on hosting failure, empty
      generation, blocked plan, publish failure, store write failure; media_host no longer
      swallows exceptions invisibly; secret env values scrubbed from every alert. Flag
      AGENT_OPS_ALERTS_ENABLED (ARMED in production)
- [x] Publish confirmation: after a real publish, one Graph READ verifies the post and
      replies "LIVE: <permalink>" in the card's thread; a failed verify warns in-thread +
      one ops alert; never re-publishes. Flag AGENT_PUBLISH_CONFIRM_ENABLED (ARMED in production)
- [x] Token watchdog: debug_token expiry check once per daily cycle + CLI
      `python -m agent check-tokens`; alerts within AGENT_TOKEN_WARN_DAYS (default 7);
      token value never printed. Flag AGENT_TOKEN_WATCHDOG_ENABLED (ARMED in production)
- [x] Baseline capture CLI `python -m agent capture-baseline`: manual-only BY DESIGN
      (no flag, never scheduled, nothing in the agent imports it); reads 8 weeks of posting
      history per account, writes dated JSON to /data, prints a summary. Done.

  Env vars to add to .env.example BY HAND (the file is permission-locked for agents):
  ```
  # --- Tier 1 hardening (Fable 5 review). Every flag defaults OFF. ---
  AGENT_IDEMPOTENT_DRAFTS_ENABLED=false  # one draft per (account, day, type); re-runs reuse, changes supersede, stale cards expire
  AGENT_OPS_ALERTS_ENABLED=false         # one "ECHO ALERT:" Slack line per pipeline failure (secrets scrubbed)
  AGENT_PUBLISH_CONFIRM_ENABLED=false    # Graph read-back after a real publish; permalink replied in the card thread
  AGENT_TOKEN_WATCHDOG_ENABLED=false     # daily debug_token expiry check; token value never printed
  AGENT_TOKEN_WARN_DAYS=7                # days before token expiry the watchdog starts alerting
  ```

- [x] Set Gemini key (AGENT_NANO_API_KEY) by hand (proven by the first live card, 2026-07-01)
- [x] Run master ON / publish OFF, watch daily drafts (superseded: publish is now armed)
- [ ] Run the full 30-day loop once (see the 30-day IG plan below)
- [x] Arm publishing: AGENT_PUBLISH_ENABLED ARMED in production (Railway env; code default
      stays false so a fresh checkout is always draft-only)

## Stage 2 — One paying client (hand-picked, forgiving)
### Built, not armed (2026-07-02 buildout; every flag defaults OFF)
- [~] Multi-client foundation: per-account voice_doc / social_proof_doc / library_prefix /
      slack_channel / approvers with global fallback; LASSO = client zero, behavior unchanged.
      No flag (pure config; enforcement wiring of per-account approvers deliberately deferred,
      the global approver gate stays the hard gate)
- [~] Brand voice intake template: brand_voice/BRAND_VOICE_INTAKE.example.md + CLI
      `python -m agent draft-bible --client <key> --intake <path>` writes DRAFT bible +
      social_proof to brand_voice/drafts/<client>/ (manual only, never auto-activated)
- [~] Texted-link intake, client half: tokenized mobile upload page to R2
      (intake/<client>/incoming/), own Railway service (`python -m agent intake-web`,
      R2 only, no /data), allowlist + size caps + rate limit; flag AGENT_INTAKE_ENABLED OFF
- [~] Texted-link intake, processing half: ingest INSIDE the listener (HEIC to JPG,
      orientation, SHA-256 + phash dedupe, moderation hook to review/ + notice, note filed
      as the drafter's .txt sidecar), idempotent manifest, dead-letter + one ops alert;
      same AGENT_INTAKE_ENABLED flag
- [~] Social Grade v1: honest A to F + subscores (consistency, mix, engagement, growth,
      verified proof) + baseline before/after posts per week; rubric docs/SOCIAL_GRADE.md;
      flag AGENT_GRADE_ENABLED OFF
- [x] Meta App Review kit (docs/META_APP_REVIEW_KIT.md, permissions derived from code) +
      Stage 2 onboarding runbook (docs/STAGE2_RUNBOOK.md)
### Still open
- [ ] Client / team approval flow via the portal
- [ ] Prove the voice holds for someone who is not Blake
- [ ] Prove the 30-day refresh lands for a real client
- [~] Document intake: client sends a PDF (texts + email); Echo extracts the text,
      splits it into N post ideas (deterministic, no LLM), drafts an infographic card
      per idea, holds ALL for approval. No-fabrication gate applies (PDF is raw material,
      never approved fact). Built in sandbox: agent/doc_intake.py + CLI intake-doc,
      reuses creative_studio + media_host; flag AGENT_DOC_INTAKE_ENABLED OFF. pypdf added.
      This is the seed of client intake. Flag AGENT_DOC_INTAKE_ENABLED, default OFF.

## Stage 3 — Productize ($99/mo)
- [ ] Launch as the $99 Social Media add-on ($99 ad clients, $199 non-ad)
- [ ] Template intake, approval, calendar, monthly review
- [ ] DAM with member-photo consent tracking
- [ ] Creative runway card (days of content left) + text-back alert
- [ ] Reporting on Meta Graph views (not impressions), daily snapshots cached
- [ ] Portal exposes creative library (read); portal hosts reporting dashboard (write)
- [ ] Onboard client by client; $99 starts stacking

## Stage 4 — Scale automation (near-zero-touch)
- [ ] Claude Agent SDK agent behind approval-gate hooks
- [ ] Per-account trust ladder climbing (routine auto-publish inside approved calendars)
- [ ] Multi-account oversight; one human owns exceptions + monthly review
- [ ] Per-gym agent memory + audit log prove reliability
- [ ] Nightly brain loop armed (read brain + performance, propose, never auto-edit voice)

## Roadmap / next builds (scoped, not started)
- [x] Daily Stories posting (warm-audience signal) -> built, moved into Stage 1 as [~]
- [x] Caption SEO baked into the drafter -> built into the content brain, in Stage 1 as [~]

## Stubs (documented, intentionally not built yet)
- [ ] Comment handling (Tier 1 auto-safe / Tier 2 surface / no auto DMs)
- [ ] 30-day creative refresh loop (the product)
- [ ] Portal creative-library read; portal reporting write; nightly brain read

---

## Full build spec — the organic system (see BUILD_SPEC.md)
The complete scope Echo grows into. Everything plugs onto the proven Stage 1 core.
- Ingestion: texted short link primary (MMS/portal fallback); event-driven queue to
  idempotent Railway worker; HEIC/MOV convert; SHA-256 + pHash dedupe; AV + moderation;
  thumbnails; dead-letter + backoff.
- DAM: asset metadata + AI tags with confidence; human review queue; member-photo
  CONSENT tracking (release required to publish); usage tracking prevents reposts.
- Creative runway: days of content left = unused approved assets / posts per day; one
  glanceable green/amber/red card with a zero-date; below threshold the agent texts a request.
- Agent (Claude Agent SDK): model routing (Opus judgment, Sonnet copy, Haiku classify);
  gated act-tools; per-gym memory; PreToolUse approval hooks; decision audit log; SB7 skills.
- Platform + reporting: Supabase RLS + Clerk org isolation; idempotency; rate-limit-aware
  GHL + Meta clients; reporting on views not impressions; white-label dashboard + branded PDF.
- Google Business Profile: first-class publishing channel (local posts) alongside IG + FB;
  own draft-only branch, own post variant (one image, <=1500 chars, CTA button, no hashtags).
  Full scope + access gate in BUILD_SPEC.md Addendum A.

---

## Open risks / watch items
- Repo divergence: deployed repo has commits from other agents (ruvnet, Manus); the
  reference sandbox may differ. Code ships as a behavior-described Claude Code prompt,
  never a wholesale push.
- Rotate secrets by hand: Meta app secret + long-lived token, Slack tokens, client Page tokens.
- Verify one caption line ("That difference is your revenue") was Blake's own note edit.
- DECISION (resolved) — brand palette: canonical = V3 Navy #121E3C / Red #FF0000 / Sky #5EB9E6 /
  Cream #FAF6F0. Locked in creative_studio.py; BUILD_SPEC.md updated; #0F1B33 draft superseded.
- DECISION — publish path: spec routes through GHL Social Planner V2; Echo publishes direct via
  Meta Graph today. Lean: keep direct Meta for LASSO now, move to GHL at 100+ client scale.

## Reporting to wire in Stage 3 (per account, per 30-day cycle)
From the Meta Graph API, on VIEWS not impressions (Meta migrated April 2025; pre/post are not
comparable). Engagement (rate + raw), saves, sends/shares, likes, comments, reach, views,
follower growth (net + rate), posting frequency before vs after Echo, top 3 / bottom 3 posts,
health read (growing / flat / declining).

## 30-day IG plan (pre-publish gate)
Diagnosis: the account is high-output, low-reach (1,308 posts, ~1,169 followers). Fix is reach
and follow-through, not volume. Plan biases to Reels + carousels with save/send CTAs and caption
SEO, one post/day rotating the 5 pillars. Full plan: `lasso_ig_30day_plan.md`. Refine with real
per-post data once Stage 3 reporting is live.
