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
- [~] Creative rotation + variety guard: no-repeat window (default 14 days, served log on
      /data), consecutive days never share a pillar, approved library cycles alongside the
      Nano card (Nano one source among several), fabrication gate supreme (stat-bearing
      creatives excluded until their claim is cleared in knowledge USE stats or approved
      social proof; thin pool falls back to oldest approved + one ops alert);
      flag AGENT_ROTATION_ENABLED OFF, window env AGENT_ROTATION_WINDOW_DAYS
- [x] House style LOCKED to the illustrated-diagram concept: cream canvas (never a solid
      slab), one navy headline top, body is a line-icon diagram with UPPERCASE labels +
      flow arrows, red as the single accent, one idea per card; Stories designed 9:16 from
      scratch. Seed library swept: ALL 14 slab cards classified OFF-STYLE and held out via
      content_library/style_exclusions.json (nothing deleted; regenerate card by card and
      remove each line to bring the slot back). BLAKE BY HAND: regenerate replacements
- [~] regen-library CLI (manual, like capture-baseline; no flag, never scheduled): rebuilds
      the seed library in the v2 house style. 8 non stat concepts (2 with from scratch 9:16
      story variants), lasso_v2_ files + json sidecars with hosted public_url, prints one URL
      per card for the eyeball pass; --only <key> single card redo, --dry-run prints prompts
      free. Story variants never enter feed rotation. BLAKE BY HAND: run it in the container,
      eyeball every URL, redo misses with --only
- [x] Layout archetypes inside the locked house style: FLOW, SPLIT, HERO, PATH, HEADLINE
      (structure varies, brand never does; secondary knobs per archetype: illustration
      scale, label density, red accent placement). Regen batch assigned (no archetype more
      than twice); story variants inherit the archetype recomposed 9:16 with safe zones;
      daily Nano cards rotate archetypes deterministically; rotation logs the served
      archetype and softly prefers alternation (never overrides the no repeat window or
      the fabrication gate)
- [~] Opus Clip ingest: pulls finished clips via the documented API (Bearer key
      OPUS_API_KEY by hand; discovery = pinned AGENT_OPUS_PROJECT_IDS + collections since
      the API has no bulk project listing; webhooks are outbound only so polling it is).
      CLI `pull-opus` (manual first): watermark on /data, sha256 dedupe, R2 hosting, video
      asset + sidecar (source=opus, clip id, title, duration, pulled, note = the clip's
      own title/words), one URL printed per clip. Clip drafts as a Reel through the normal
      path, held for approval; video is its own rotation pillar. Dormant poll behind
      AGENT_OPUS_POLL_ENABLED (interval AGENT_OPUS_POLL_MINUTES, default 60), failed clips
      retry then dead-letter with one ops alert. Flags AGENT_OPUS_ENABLED +
      AGENT_OPUS_POLL_ENABLED, both OFF
- [x] SQLite store on /data (echo.db, WAL): drafts, posts, served, snapshots, counters,
      kv; legacy pending_drafts.json / rotation_served.json / post_log.jsonl migrate once
      with .migrated.bak backups; storage swap only, no behavior change
- [~] Reporting live path: daily Graph snapshot job in the listener after the daily
      draft (VIEWS never impressions + reach/likes/comments/saves/shares/followers,
      per post metrics refreshed), monthly-report CLI builds the per account 30 day
      HTML report (V3 brand, /data/reports) + Slack summary + the creative REFRESH
      proposal (pillar/archetype/set performance from real data, three angles cited
      only from approved sources, plain raw material ask list). Gate stays
      AGENT_REPORTING_ENABLED (OFF)
- [~] Creative runway card: days of approved gate-clean content left per account
      (in-style, unposted, gate-clean only), one daily line with green/amber/red +
      projected zero date, debounced low-runway ops alert asking for raw material;
      flag AGENT_RUNWAY_ENABLED OFF, threshold AGENT_RUNWAY_ALERT_DAYS (7)
- [~] Trust ladder as data: per account levels (0 full approval forever by default,
      1 routine calendar auto AFTER a human approved the monthly calendar), levels
      hand-edited config only, typos fail safe to 0; DOUBLE GATE via
      AGENT_TRUST_LADDER_ENABLED (OFF) so nothing changes today; the auto-publish
      wiring itself stays a deliberate by-hand step. Enforcement unchanged
- [~] add-client CLI (manual): full per client scaffold in one command (voice doc TODO
      template, social_proof.md with the Permission: yes rule header, library folder,
      printed Account config entry at level 0 + the by-hand checklist). Touches no env,
      creates no tokens, arms nothing; idempotent re-run never destroys hand edits
- [~] Quality + cost guards: headline OCR check (Gemini vision transcription, lowest
      cost, since the container has no pure python OCR; mismatch = warning line on the
      card, never a block; flag AGENT_OCR_CHECK_ENABLED OFF) and Gemini spend cap
      (per day counter in the store, at AGENT_GEMINI_DAILY_CAP default 40 generation
      pauses for the day with one ops alert and library-only selection takes over;
      flag AGENT_SPEND_CAP_ENABLED OFF)
- [~] Evening digest: one Slack line per day at AGENT_DIGEST_HOUR_UTC (default 23):
      drafted / approved / published / blocked / runway days, assembled from the /data
      store; sent mark persisted (restart never double-sends); flag AGENT_DIGEST_ENABLED OFF
- [~] White label PDF export: monthly-report --pdf renders the 30 day report as a
      clean branded PDF (reportlab rebuild; weasyprint/wkhtmltopdf need system libs
      the container lacks), per account white labeling (display name + optional
      brand_voice/<client>/logo.png; LASSO default), dash free text layer
- [x] Service concept set for regen-library: 8 source-verified service cards (ads,
      follow up, lead to member path, sales training, funnel diagnostic, social,
      all in one place, website), archetypes assigned none more than twice; --set
      brand|service|all; sidecars record set; rotation softly alternates brand and
      service days (never overriding the window, pillar rule, or fabrication gate).
      Unsupported lines swapped for sourced wording; the 30 day review concept was
      dropped entirely (no approved source) and replaced with website_done_for_you
- [x] Story first cards (the stranger test): every card depicts a concrete gym world
      scene with a TENSION and a RESOLUTION readable at a glance; meaningful labels
      (LEADS, BOOKED, MEMBERS), generic process labels banned (STEP N, PLAN, GROW,
      LEARN, DISCOVER, LAUNCH, START, FINISH); all 16 concept contexts rewritten as
      Tension/Resolution micro stories modeled on follow_up_problem
- [x] BE CLEAR, NOT CUTE headline law: headlines state plainly what the card is about
      or what LASSO does; two second test in the spec; six slogan headlines rewritten
      to plain statements (three_step_path, posting_cadence, speed_to_lead_concept,
      system_runs_itself, coach_in_your_corner, one_partner); approved voice framings
      stay verbatim. BLAKE BY HAND: rerun regen-library for the story first batch
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
- [~] Knowledge brain: brand_voice/knowledge/ as gated source material (LOCKED / PENDING /
      NOT FOUND and *_pending.md never draft; only USE-marked stats in copy, wording exact;
      03_social_proof_pending.md excluded, proof flows only through social_proof.md);
      flag AGENT_KNOWLEDGE_ENABLED OFF. BLAKE BY HAND: the echo_brain folder was not found
      on disk, so brand_voice/knowledge/ is empty; drop the files in and commit
- [~] Summit campaign: one summit post per week inside the daily cadence (summit day, default
      Tue), drafted ONLY from 04_summit_campaign.md VERIFIED FACTS + APPROVED ANGLES, angle
      rotation (no repeat within 3 weeks), CTA "Claim your seat" +
      https://lassoframework.com/summit, auto-stops after 2026-11-08;
      flag AGENT_SUMMIT_CAMPAIGN_ENABLED OFF
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
