# Echo Build Tracker

Living tracker for the Echo social agent build. This markdown is the source of
truth; the HTML dashboard (`echo_build_tracker.html`) is the visual view. The
full organic-system scope lives in `BUILD_SPEC.md`.

Status key: [x] done  ·  [~] built + tested in reference repo, push/deploy pending  ·  [ ] not started

Last updated: 2026-07-10 (Episode inbox watcher + Monday nudge shipped dark: Parts 1-5
complete. Polling watcher in existing listener, exactly-once claim markers, size-stability
guard, Phase 1 clip selection on arrival, ranked plan to Slack, RSS episode matching,
evergreen guard, Monday 9am nudge (idempotent, window-gated). `agent inbox-status` CLI.
39 tests green. Master flag AGENT_EPISODE_INBOX_ENABLED OFF. Suite 1000 green, 7
pre-existing reportlab. Prev: 2026-07-09 Native clipper end to end shipped dark.)

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
- [~] Social Grade client report card: grade-card CLI renders the computed grade
      (A to F + six area rubric + before/after posting frequency) as one page V3
      HTML + PDF from live store data; respects AGENT_GRADE_ENABLED; drafts nothing
- [~] DAM v1: consent guard (fail safe: people=true needs consent=granted, unknown
      excluded; absolute in rotation + runway; flag AGENT_CONSENT_GUARD_ENABLED OFF;
      arming an untagged library excludes everything until tagged, by design),
      perceptual near-dupe collapse (dam-scan marks dupe_group sidecars; rotation
      keys on the group so the window blocks near-identical reposts), auto-tag (one
      Gemini vision call per new asset: tags + people flag + description; low
      confidence marks review=true; counts against the spend cap; flag
      AGENT_AUTOTAG_ENABLED OFF)
- [~] Decision audit log: append-only audit table records every selection (and WHY),
      every gate exclusion (fabrication, consent, style), publish confirms, and every
      ops alert (even when Slack is dormant); reasons pass the secret scrub; `audit`
      CLI prints the readable trail. Always on, no flag: logging truth is not optional
- [~] Nightly brain (the read only proposer the spec stubbed, now real): one Slack
      note per night after the digest hour: what is winning (pillar/archetype/set from
      real engagement), one angle QUOTED from approved sources with its citation
      (LOCKED knowledge can never appear), one question when data is thin. Proposes,
      never creates, never schedules; flag AGENT_BRAIN_PROPOSALS_ENABLED OFF
- [~] Store backup + restore: nightly consistent sqlite snapshot to R2
      (echo/backups/, 14 day retention, one ops alert on failure only; flag
      AGENT_BACKUP_ENABLED OFF, hour AGENT_BACKUP_HOUR_UTC default 2) and
      restore-store CLI (staging + verification counts; never touches the live db
      without --confirm; old db kept as .pre_restore.bak)
- [~] Client welcome kit: welcome-kit CLI renders one V3 page (HTML + PDF) per
      client: how approval works, texting creative in, what the report covers, the
      trust rules in plain language; fixed template copy only, no pricing, no dashes
- [~] THE FULL GYM book campaign: knowledge/ book docs registered as approved
      sources (book = MASTER; its LOCKED section blocks like locked stats: LAUNCH
      DATE, BUY LINK, PRICE, subtitle of record never guessed). Armed, the campaign
      LEADS the calendar: week 1 queue posts VERBATIM in order one per day, then
      angles 1 to 8 rotate (9 to 11 dark until blanks fill). Case study numbers
      character exact or the draft blocks; numbers pending studies unselectable;
      first person voice law enforced; cover style (black canvas, red and white
      type) is the ONE documented exception to the cream house spec, scoped to book
      cards only; premade cards in content_library/book_campaign/ used before
      generating. Known conflicts (subtitle, author bio figure) flag as card
      warnings. Flag AGENT_BOOK_CAMPAIGN_ENABLED OFF
- [~] Facebook connect page: /connect on the listener (small HTTP thread, needs the
      /data store for the page token), cream V3 single page, Facebook Login for
      Business with exactly the five publish scopes, callback picks the Page and
      resolves the linked IG professional account, page token kv-stored (never
      logged, never rendered, audit scrubbed). Whole surface 404s while
      AGENT_CONNECT_ENABLED is OFF. Publish gates untouched: connecting changes
      nothing about posting
- [~] OVERNIGHT STAGES BUILD (2026-07-03): publish verify 400 fixed with honest alert
      split; connect kv tokens into account resolution (AGENT_CONNECT_TOKENS_ENABLED);
      premade story variants (AGENT_STORY_PREMADE_ENABLED); two tier comment engine
      hardened (conservative tiering, Graph reads, held cards, DMs structurally
      untouchable); monthly review loop (AGENT_MONTHLY_REVIEW_ENABLED: digest + PDF +
      citation gated proposals); trust ladder WIRED (AGENT_TRUST_DRYRUN +
      AGENT_TRUST_AUTOPUBLISH, both OFF; first post never automated, off template
      always cards); one command onboarding (onboard-client + intake_template.md);
      fleet hardening (per account isolation + fleet-status). .env.example now the
      complete flag reference. All new flags OFF
- [~] EVIDENCE AND ARMING PREP (2026-07-03 overnight): monthly-review --dry runs read
      only without the flag; backfill-insights CLI (idempotent, 429 aware, views never
      impressions); scheduler heartbeat + missed run alert (no flag, observability);
      comments first poll flood guard (pre arm backlog never carded); connect queues a
      Social Grade baseline (AGENT_CONNECT_GRADE_ENABLED, OFF); seed-calendar CLI from
      approval evidence only; Opus discovery fixed (pinned ids honored, collections
      paginated, honest empty messaging + exact remediation in opus-check); gbp-check
      readiness probe. All new flags OFF
- [x] Queue triage maintenance (2026-07-04, from Scout's findings): flagless card
      self-expiry (past-due PENDING cards flip EXPIRED, buttons removed, one log line;
      hourly listener sweep + at every daily run; retroactively clears the 22 stale
      cards and 4 dead loopers on first production cycle) and the retry-storm root fix
      (blocked drafts stored + deduped per account/day/type so a failing slot cards
      ONCE; empty-caption drafts block instead of growing buttons). FB verify-400:
      already fixed Jul 3 (Photo node field set); both observed events predate the
      deploy; no Meta-side action needed
- [x] backfill-insights 400 patch (2026-07-04): root cause was the metric list, not
      access. IG MEDIA insights metric is saved not saves (every IG media read 400d);
      FB Page posts use a different insights namespace entirely (every lasso_fb read
      400d) and now read likes/comments/shares via object fields; stories get their
      own metric set with a graceful "story insights expired" skip past 24h; Graph pin
      bumped v21.0 to v23.0 (past the views migration). ONE media-type-aware metric
      builder feeds both the backfill and the daily snapshot; every skip line and
      audit row now carries the Graph error code/subcode/message (token scrubbed) and
      names the missing permission when it is one
- [x] Micro patch (2026-07-04 pm): FB photo-node metrics (bare photo ids resolve
      their owning post via page_story_id, then read reactions/comments/shares; the
      field likes is never requested on any FB node) and Opus collection id
      extraction made shape tolerant (collectionId/string/anything-Id; an
      extracted-vs-returned mismatch warns loudly with the keys seen, never a
      silent zero)
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
- [~] Podcast pipeline (4 parts, one commit each): (A) RSS feed watcher on the scheduler
      cadence, idempotent episode records (guid keyed), podcast:transcript namespace,
      loud on malformed feed or missing AGENT_PODCAST_FEED_URL; (B) podcast_release
      house-style card (EPISODE <N> / <TITLE> / one-sentence dash-free about line from
      the feed description only) in the daily slot AFTER the book campaign and BEFORE
      pillar rotation, newest episode only (no backlog blast), cards once per episode,
      max one podcast draft per account per day; (C) transcript ingest (CLI
      podcast-transcript --episode N --file|--url, plus auto ingest from the feed) as an
      APPROVED SOURCE scoped per episode, citation podcast_ep<N>, episode-tagged drafts
      only, no transcript text in logs beyond the 120-char CLI preview; (D) episode
      infographics (CLI podcast-cards --episode N [--count 2|3]), hook + support VERBATIM
      from the transcript, citations must resolve at queue AND serve time, spread 1/day
      behind book priority, same house builder with no style overrides, 18 existing
      concepts untouched. Every card held for approval; nothing publishes;
      flag AGENT_PODCAST_ENABLED OFF
- [~] Podcast release templates (B2): four LOCKED navy poster templates
      (podcast_release_a classic poster / _b bold split / _c on air studio /
      _e podcast player), scoped palette exception like the book cover;
      deterministic rotation episode mod 4 over A B C E (131=E, 132=A, 133=B,
      134=C), 3-digit episode slot, 2-line word-boundary title (~40 chars/line),
      dash-free about line, chosen template logged in the audit row
- [~] Podcast memory (2 parts, one commit each): (E) episode learnings (CLI
      podcast-learn --episode N, also rides podcast-cards): 3-7 VERBATIM
      takeaway+quote learnings with podcast_ep<N> citations and pillar taxonomy
      tags into brand_voice/knowledge/podcast/ep<N>_learnings.md + rolling
      INDEX.md; additive only, paraphrases refused, the global gate never reads
      the subfolder; (F) standing claim promotion PROPOSE ONLY: quantitative /
      named-framework learnings card PROPOSED STANDING CLAIM (quote, citation,
      the exact USE line); the approver tap is the ONLY write path into
      02_verified_stats.md, citation attached on landing; book conflicts named,
      blocked, rechecked at tap time. Rides AGENT_PODCAST_ENABLED (OFF)
- [~] B2B swipe file (2 commits): (A) four receipts in 02_verified_stats.md
      ("LASSO B2B Ad Swipe File, July 2026, Blake approved": $16 blended CPL,
      $35,000 caught / $17,000 flagged, twice-monthly reconciliation, 7 dead
      buttons; 500+ referenced not duplicated), gate clears cited receipts and
      still blocks uncited claims; (B) 10 b2b_* concepts in the house library
      (set "b2b", pillars verbatim, stat headlines carry cites), same locked
      builder, 16 house concepts byte untouched (frozen hash),
      regen-library --only/--set b2b per key. Render by hand via regen-library
- [~] Operator hygiene (4 parts, one commit each): (A) regen batch guard: one
      live regen-library run at a time (stale safe lock, dead pid + age auto
      clear, second invocation refuses naming the holder) + end of batch
      summary table (concept, content hash, url) with superseded note on
      re-runs; (B) contact-sheet CLI (--set <name>|--all [--out PATH]):
      self contained HTML review grid from live library sidecars (key, pillar,
      review hints; stat cards get the numeral hint), uploads to
      echo/contact_sheets/<set>_<date>.html, read only against the library;
      (C) podcast-status read only probe (feed reachable, items seen, latest
      parsed, armed watermark, honest next poll forecast per the mod 4
      rotation) + 139 episode first poll proof (only the newest episode ever
      drafts) + backlog guard on transcript auto ingest (newest only past 3
      new episodes in one poll); (D) runway --account <key> --explain: the
      runway math in plain lines on the digest's own shared implementation
      (eligible by name, exclusion reasons, consumption, days). All read only
      or by hand; no daily behavior change anywhere
- [~] House style variant system (3 parts, one commit each): (A) locked canvas
      + layout tokens in the house builder: 4 canvases (cream / navy #1A2340 /
      red / split) and 5 layouts (stat_hero / framework / contrast / checklist
      / poster) under a constant brand grammar (one type family, two fonts
      max, logo lockup, #E03131 single accent, footer) and a shared
      readability bar (high contrast, mobile legible, thumbnail headline);
      no variant fields = the original render path byte for byte; (B)
      deterministic assignment (explicit per concept override wins, else key
      hash over the canvas order) + rotation canvas guard (same canvas never
      serves two days running where an alternative exists, never starving);
      b2b set assigned per brief (4 stat_hero, 2 framework, 1 each checklist /
      contrast, 2 poster across navy / cream / red / split); (C) full test
      coverage incl. adversarial guard + 20 combo render smoke; 16 house
      concepts unchanged
- [~] Platform doctrine + concept set (2 parts, one commit each): (A)
      brand_voice/knowledge/08_platform_2026.md, the PRIMARY POSTING SOURCE
      ("LASSO Platform Overview 2026, Blake approved July 2026"; book stays
      top of the citation hierarchy, this ranks under it above lasso_now):
      positioning lines, six engines + funnel order, verified receipts
      ($16 CPL, $35K+ saved, 71.9% vs 18.5%, 297/141/100+, 8 of 10, 70%+
      close, 25 point audit, 7+ dead buttons), eight named case studies
      (Fit Mamas, Courage, North Naples, Old Glory, Granite Forged, Loup,
      Hoosier, Liminal), all USE lines with platform_2026 anchors, NO
      pricing; (B) 10 platform_* concepts (set "platform") through the
      variant system with per key canvas/layout from the brief, stat
      headlines cited, house 16 + b2b 10 frozen, variance guard green
      across the 36 concept library, regen-library --set platform. Render
      by hand via regen-library
- [~] Grammar V2 + platform ad set (2 parts, one commit each): (A) three V2
      layout tokens (chart: one data visual with big labeled numbers;
      diagram: funnel / hub and spoke / flow arrows, labeled nodes; device:
      phone / browser / profile grid mockup in thin outline), same grammar
      and readability bar, five originals frozen; (B) 10 platform_ads_*
      concepts through the V2 grammar with per key canvas/layout from the
      brief, every concept citing platform_2026, every CTA routing
      quiz.lassoframework.com; house 16 + b2b 10 + platform 10 frozen;
      46 concept library; regen-library --set platform_ads
- [~] Day 30 readiness + doctrine wiring (4 parts, one commit each): (A)
      per account framed Day 30 assembler (report_framing on the account:
      lasso_fb leads with the frequency before/after story + multiplier;
      lasso_ig is engagement and consistency ONLY, frequency confined to an
      internal do not publish appendix, safe default engagement); backfilled
      per post insights + snapshots, top/bottom 3, health read, honest gaps;
      CLI report --account --dry (exact Slack text, watermarked, writes
      nothing); (B) platform doctrine wired as the primary caption source
      (book untouched on top, 08_platform_2026.md second, lasso_now
      fallback), pillar angles resolve doctrine USE lines with citations,
      dormant behind AGENT_KNOWLEDGE_ENABLED, unverifiable angles dropped
      with audited reason, monthly review proposals labeled by source; (C)
      monday-preview read only GO / NO GO preflight (feed forecast, runway,
      quiet token days, heartbeats, pending approvals, flags snapshot),
      zero side effects; (D) Sunday operator report behind
      AGENT_WEEKLY_REPORT_ENABLED (OFF): one card Sundays 6 PM ET, posts /
      approvals / views based engagement on the Day 30 framing rules /
      runway / flags delta / by hand item, honest no data gaps
- [x] Month calendar artifact V2 (2026-07-06, 2 commits): (A) read only
      month assembler from existing state (posts + the same drafts store
      the Slack cards read, seed calendar keys, schedule skip days,
      specials from draft evidence + the Monday podcast expectation); per
      day concept/caption/canvas/layout/status + special, empty days emit
      an open slot never an invented concept; (B) calendar-html CLI V2:
      full post per cell (image or placeholder, complete caption,
      hashtags, canvas/layout chips, status), tap-to-expand modal (full
      image, complete caption, hashtags, canvas/layout, citation source
      line, status; Approve/Edit/Kill display-only previews, no write
      back); uploads to echo/calendars/<account>_<month>.html
- [x] Runway v2 source + gate fix + plan-month (2026-07-06, 3 commits): (A)
      classify_creatives reads BOTH physical library files (old format, style
      exclusions apply) AND all 46 regen library concept definitions from
      regen_library.CONCEPTS; v2 concepts (lasso_v2_*) are never off-style by
      default; runway --explain prints per-set breakdown (house/b2b/platform/
      platform_ads); (B) fabrication gate decoupled from AGENT_KNOWLEDGE_ENABLED:
      _approved_claims uses usable_stats_always() so USE-line stats clear the gate
      regardless of flag; three speed_to_lead_carousel sentences added as USE lines
      to 02_verified_stats.md; adversarial uncited claims still fail; (C) plan-month
      CLI fills open posting days from the eligible pool (14-day rotation window,
      canvas guard, schedule skip, no double-booking); approve-month bulk-approves
      pending plan drafts; first post per account held for the tap; both behind
      AGENT_PLAN_MONTH_ENABLED (OFF). Suite 623 green (7 pre-existing reportlab).

### Opus video factory (2026-07-09 buildout; eight parts, master flag OFF)
Master flag AGENT_OPUS_FACTORY_ENABLED (default OFF). Turns the back catalogue of
finished Opus clips into DRAFTS held for approval; extends the existing Opus
client, never publishes. New CLI: `python -m agent opus-pull [--write]`.
- [~] All-project scan: OpusAPI.list_projects enumerates EVERY project (no
      allowlist, no collection id); opus_factory.scan normalizes each finished
      clip (clip_id, project_id, source_title, title, opus_score, duration_s,
      transcript, download_url); unfinished clips excluded
- [~] Score gate FIRST: AGENT_OPUS_SCORE_FLOOR (default 90) drops a clip before
      any other work; AGENT_OPUS_DURATION_MIN/MAX (15-95s)
- [~] Bucket tagger: podcast-sourced clips (source title = the show,
      AGENT_OPUS_PODCAST_SHOW) tag podcast; others classify from the transcript
      against the 6 buckets + the LASSO theme lexicon; below
      AGENT_OPUS_RELEVANCE_FLOOR (0.65) or no theme => HOLD + ops alert, never
      drafted; transcript only, never invents
- [~] Hook check: the opening ~2s must carry a claim, number, or question, else
      demote to shortlist not draft
- [~] Caption writer: evergreen (back-catalog, never "new episode is live"),
      hook + payoff from the clip's own words, soft CTA to the full episode +
      podcast footer on podcast clips, bucket CTA and no footer otherwise; no
      dashes, never vendor; the fabrication gate stays sole authority (caption
      asserts only what the transcript or the approved facts file already say)
- [~] Dedupe + no-repost ledger: clip_id ledger in the volume kv (opus_drafted_
      / opus_posted_); a clip is drafted at most once; posted clips tracked for
      reporting before/after; a re-run never re-drafts
- [~] Calendar routing: drafted clips fill VIDEO slots on their bucket's cadence
      (podcast Thu, platform Tue/Sat), respecting weekly quotas, no-repeat
      spacing, and a per-week Opus cap AGENT_OPUS_WEEKLY_CAP (default 2); every
      draft PENDING and held; draft-only + trust ladder + first-post gate honored
- [~] opus-pull CLI: dry-run prints the ranked plan (score, bucket, hook line) +
      the held/rejected list with reasons (below floor, off-topic, weak hook,
      dupe) and writes nothing; --write builds the held drafts, posts each to
      the ops channel for the tap + one digest line
- RETIRED: the hand-built "Echo Export" Opus collection step and the
      AGENT_OPUS_PROJECT_IDS pin are no longer required (the factory scans all
      projects). Both vars remain only for the legacy pull-opus ingest poller.

### Opus scan auth guard (2026-07-09; four parts, built after dry-run returned 0)
Root cause: OPUS_API_KEY in the live container was `sk-2vtUf...` (rotated/leaked
key). The scan was swallowing the 401 silently and returning [], which looked like
a clean zero-results run.
- [x] Part 1: OpusScanError — typed exception; _get raises it on non-2xx with
      HTTP status + scrubbed body snippet; scan() re-raises it; opus_pull_cli()
      catches it and prints "AUTH ERROR (HTTP N)" instead of "0 drafted"
- [x] Part 2: call-time env reads — config.opus_api_base() and opus_org_id()
      read from env at each call (no import-time cache); _default_api() uses them
      and logs the key prefix (first 6 chars) for operator confirmation
- [x] Part 3: opus-doctor — new `agent opus-doctor` command (behind
      AGENT_OPUS_FACTORY_ENABLED): key prefix, HTTP status, project count, first
      project raw status. Five-second "is this key working?" preflight.
- [x] Part 4: finished-clip filter widened — _FINISHED_STATUSES accepts "done/
      completed/finished/ready/exported/success/succeeded/published"; exportUrl
      and export_url key aliases added; verbose scan logs raw status of every
      excluded clip so the operator can identify new status values
- Root cause verdict: the key VALUE is stale (sk-2vtUf is the rotated key).
  BLAKE BY HAND: set the current OPUS_API_KEY in Railway env and redeploy.
  Run `agent opus-doctor` after redeploy to confirm auth before running opus-pull.

### Opus route fix (2026-07-09; four parts, built after opus-doctor returned 404)
Second root cause, SEPARATE from the key: the factory scan was built against a
GUESSED endpoint, GET /api/projects?q=mine, which does not exist and returns 404
NotFoundException. A correct key against the wrong route still saw zero clips.
The documented Opus API has NO bulk project-listing endpoint; the legacy
pull-opus poller is the source of truth (it lists via collections).
- [x] Part 1: route contract documented — proven routes are
      GET /api/collections?q=mine (discovery) and GET /api/exportable-clips
      (findByCollectionId / findByProjectId); base URL + auth header were never
      wrong (shared with the legacy poller via OpusAPI._get). No behavior change.
- [x] Part 2: client corrected — OpusAPI.list_collections_detailed() lists via
      the proven /api/collections route; the dead list_projects (/api/projects)
      is removed; opus_factory.scan discovers via collections (all-collection
      scan, no allowlist) plus pinned AGENT_OPUS_PROJECT_IDS (read at call time
      via config.opus_project_ids); clips queried with findByCollectionId; the
      call-time key read and OpusScanError propagation kept. Tests assert the
      scan hits /api/collections + /api/exportable-clips and NEVER /api/projects.
- [x] Part 3: opus-doctor made definitive — calls the corrected /api/collections
      route; 404 => "ENDPOINT WRONG" (route/base URL bad), 401/403 => "AUTH
      WRONG" (key rejected), never collapsed; reports key prefix, resolved base
      URL, HTTP status, collection count, first collection raw status.
- [x] Part 4: finished-clip filter confirmed against the documented
      exportable-clips shape (uriForExport present = finished by contract); test
      flows the documented field set end to end through the corrected scan.
- Routes verdict: the discovery route was wrong. WAS GET /api/projects?q=mine
      (404); NOW GET /api/collections?q=mine. The legacy pull-opus poller was
      the source of the correct contract. Base URL + auth header were correct
      all along. NOTE: this is separate from the stale-key issue above; both
      must be right for opus-pull to see clips. BLAKE BY HAND: after setting the
      current OPUS_API_KEY, run `agent opus-doctor` — it should print HTTP 200
      with a collection count. Podcast clips must live in a collection named
      after the show (AGENT_OPUS_PODCAST_SHOW) for podcast tagging.

### Opus response shape fix (2026-07-09; four parts, built after opus-doctor 200 then crashed)
Third issue in the chain: with the key + route correct, opus-doctor got HTTP 200
and reported 3 collections, then crashed KeyError: 0 on items[0]. The live
/api/collections response wraps its records as an ID-KEYED DICT, not a list:
the actual shape is {"data": {"<collection id>": {...}, ...}}. The old parse did
body.get("data", []) which returned that dict, and items[0] then did a dict-key
lookup for 0.
- [x] Part 1: captured the real shape — _shape_desc(body) logs the top-level
      type and key NAMES only (capped, never values that could carry tokens/PII);
      opus-doctor prints it right after the JSON parse.
- [x] Part 2: single normalizer — normalize_list_response(body) returns a flat
      list of record dicts for any shape: a bare list; a wrapper {data|collections
      |clips|items|results|docs: <list or dict>}; a bare id-keyed dict (the key is
      injected as the record id, existing ids preserved); or empty -> []. Wired
      into list_collections, list_collections_detailed, list_exportable_clips (so
      scan is covered), preserving the legacy list_collections WARNING contract.
- [x] Part 3: opus-doctor robust — consumes the normalizer instead of items[0];
      reports collection count + first collection id/status on any shape; empty
      says so plainly and never indexes.
- [x] Part 4: tests feed the normalizer every shape (list, wrapped list, id-keyed
      dict, bare id-keyed dict, alternate wrappers, empty/None/metadata/non-JSON)
      and assert a correct flat list; scan + doctor both proven to consume it end
      to end at the HTTP layer.
- Shape verdict: the live /api/collections returns an ID-KEYED DICT under "data"
      ({"data": {"<id>": {...}}}), not a list. The route and auth were correct;
      this was purely a response-shape parse bug. Fixed for every consumer via
      one normalizer.

### Opus organize (2026-07-09; three parts, built after opus-doctor showed 0 collections)
Fourth issue: opus-doctor got HTTP 200 but 0 collections. The Opus API docs
confirm collections are created via POST /api/collections and start empty; the
account's clips live in PROJECTS and were never added to a collection, so the
factory scan (collections only) had nothing to read. Routes verified against
help.opus.pro/api-reference/openapi.json BEFORE coding.
- [x] Part 1: OpusAPI collection-management methods — _post() helper;
      create_collection(name) POST /api/collections {collectionName} -> id;
      list_project_clips(project_id) GET /api/exportable-clips q=findByProjectId;
      add_clip_to_collection / add_clips_to_collection POST /api/collection-contents
      {collectionId, contentId} one clip per call (no batch route). Call-time key
      read + OpusScanError raising reused. NOTE: ExportableClipRepresentation has
      NO score field, so clip scores are not available from the API.
- [x] Part 2: `agent opus-organize` (behind AGENT_OPUS_FACTORY_ENABLED). Projects
      from AGENT_OPUS_PROJECT_IDS (no bulk project-listing endpoint exists).
      Dry-run (default) prints the plan, writes nothing; --write creates the
      target collection if absent (name from AGENT_OPUS_PODCAST_SHOW else
      "LASSO Clips") and adds qualifying finished clips, idempotently (reads the
      collection's current contents, skips ids already in). --name overrides.
- [x] Part 3: opus-doctor prints per collection its name + clip count, so after
      organizing we can confirm clips landed.
- Routes verdict (exact): CREATE = POST /api/collections body {"collectionName"}
      -> CollectionDto {collectionId}. ADD = POST /api/collection-contents body
      {"collectionId","contentId"} where contentId is the clip's composite id
      {projectId}.{curationId}; ONE clip per call, no batch. LIST PROJECT CLIPS =
      GET /api/exportable-clips?q=findByProjectId&projectId=. KNOWN GAP: the API
      returns no clip score, so the factory score gate (floor 90) would bench
      every clip; scoring is a separate follow-up before opus-pull is useful.
      BLAKE BY HAND: set OPUS_API_KEY + AGENT_OPUS_PROJECT_IDS (ids from each
      project URL) in Railway, run `agent opus-organize` (dry-run) then
      `--write`, then `agent opus-doctor` to confirm the collection clip count.

### Native clipper, end to end (2026-07-09; all phases, every flag defaults OFF)
Abandoning third-party clip platforms. Durable path: episode video in, 4-5
finished vertical Reels out, entirely inside Echo. Claude selects moments;
mechanical layers cut, caption, frame. Zero external dependency. All flags OFF.

Phase 0 (prereq + scaffold, SHA 81f1546):
- [x] detect_prereqs() — reports HAS_FFMPEG / HAS_FASTER_WHISPER /
      HAS_TRANSCRIBE_API_KEY at call time, never logs a key value.
      ffmpeg 8.1.2 present on this machine; faster-whisper NOT installed.
- [x] clipper_render_enabled() — AGENT_CLIPPER_RENDER_ENABLED, second flag
      under master so selection ships independently of rendering.
- [x] clipper_render_output_dir() — AGENT_CLIPPER_RENDER_DIR.
  BLAKE BY HAND (if ffmpeg absent on Railway): apt-get install ffmpeg

Phase 1 (selection, SHA 0db3223; four parts, flag AGENT_CLIPPER_ENABLED OFF):
- [x] Part 1: episode intake — stage to tenant-scoped R2 key (read-only src).
- [x] Part 2: word-level transcription, cached on R2 key (faster-whisper or
      AGENT_TRANSCRIBE_API_KEY). BLAKE BY HAND: install faster-whisper or set
      AGENT_TRANSCRIBE_API_KEY in Railway.
- [x] Part 3: Claude moment selection (THE CORE) — scored, duration-gated,
      fabrication-gated candidates (hook + rationale each checked separately).
- [x] Part 4: dry-run plan printed; nothing rendered, nothing written.

Phase 2 (render, SHA 261a718; behind AGENT_CLIPPER_RENDER_ENABLED=false):
- [x] Part 5: cut_segment — stream-copy lossless cut of the selected moment.
- [x] Part 6: frame_vertical — 9:16 fill-scale + center crop (video) or
      audiogram (audio: navy canvas, red showwaves); output 1080x1920.
- [x] Part 7: burn_captions — ASS word-by-word karaoke from word timestamps;
      only words in [start_ts, end_ts] included (fabrication-safe); 220px
      margin above the lower-third brand frame.
- [x] Part 8: add_brand_frame — navy lower-third bar (LOWER_H=180px) with
      LASSO logo + red social handle burned via ffmpeg drawbox + drawtext.
      render_clip() is the 4-stage orchestrator (cut → frame → captions → brand).
  BLAKE BY HAND: set AGENT_CLIPPER_RENDER_ENABLED=true when ready to render.

Phase 3 (wire into Echo, SHA 9397de2; held drafts, never auto-post):
- [x] Part 9: save_clip_draft() — creates a PENDING Draft (never auto-publishes
      regardless of trust ladder), posts Slack approval card, saves Slack
      ts/channel for edit-in-place. source_fragments carry source=clipper /
      kind=reel / score / bucket / rationale for audit. Evergreen check flags
      captions that imply recency. Always full-approval.
- [x] Part 10: log_episode_cost() — writes per-episode token cost + transcribe_sec
      + estimated USD to db kv under clipper_cost_{day}_{key}. Visible for the
      $99 SKU margin check.
- clip_episode orchestrator extended: calls render_clip() per accepted moment
      when render flag is armed; clip_episode_cli updated with --render flag.

MORNING REPORT (2026-07-09):
Checkpoint reached: CHECKPOINT 3 (full pipeline shipped dark).
SHAs: Phase 0 = 81f1546, Phase 2 = 261a718, Phase 3 = 9397de2.
  (Phase 1 = 0db3223, built previous session.)

Transcription backend: faster-whisper NOT installed. HAS_FFMPEG=true
  (/opt/homebrew/bin/ffmpeg 8.1.2).

BLAKE BY HAND to arm this pipeline:
  1. Set AGENT_CLIPPER_ENABLED=true in Railway.
  2. Set AGENT_HOSTING_ENABLED=true + R2 credentials (already deployed?).
  3. Set ANTHROPIC_API_KEY in Railway (name only; never print).
  4. Install transcriber: `pip install faster-whisper` in the Railway service,
     OR set AGENT_TRANSCRIBE_API_KEY to an API-backed transcription key.
  5. Set AGENT_CLIPPER_SCORE_FLOOR=80 (or leave default).
  6. Run: `agent clip-episode --source <episode.mp4>` and read the plan.
  7. Confirm the picks look right on a real episode.
  8. Then: set AGENT_CLIPPER_RENDER_ENABLED=true and re-run with --render.
  9. Rendered Reels appear as HELD PENDING drafts in the Slack approval queue.
  10. Approve each Reel individually via the Slack card tap.

Parts that self-skipped: none (all phases built). Rendering is ARMED but
  AGENT_CLIPPER_RENDER_ENABLED defaults OFF — will self-skip silently.

Pipeline ready for a real Gym Marketing Made Simple episode dry-run: YES,
  AFTER steps 1-6 above are done by hand. All flags default OFF; nothing
  runs in production until Blake arms them.

### Episode inbox watcher + Monday nudge (2026-07-10; 5 parts, master flag OFF)
Human workflow: export from Riverside, drop file in the inbox prefix. Echo takes
it from there. Polling watcher inside the existing listener; no new infra.
Master flag AGENT_EPISODE_INBOX_ENABLED (default OFF, all flags OFF).

Part 1 (inbox convention + state, SHA 990d81f):
- [x] Watched prefix AGENT_EPISODE_INBOX_PREFIX (default echo/episode_inbox/<tenant>/).
      Tenant AGENT_EPISODE_INBOX_TENANT (default lasso_episodes).
- [x] Accept mp4/mov/mp3/wav only (extension filter).
- [x] Exactly-once claim: kv marker claimed before processing; re-poll skips
      claimed keys; marker survives restarts (persistent SQLite kv).
- [x] _S3Client.list_prefix() added to media_host — paginated R2 prefix listing.

Part 2 (watcher loop, SHA 990d81f):
- [x] poll() every AGENT_EPISODE_INBOX_POLL_MINUTES (default 5) in _daily_scheduler.
- [x] Size-stability guard: file must have same size across two consecutive polls
      before it is claimed (guards against in-progress uploads from Riverside).
- [x] Claim + invoke Phase 1 clip selection; post ranked plan to Slack #echoclaude
      as a held plan message. NOTHING renders, NOTHING posts.
- [x] Exception in processing marks file FAILED, alerts via ops_alerts, loop
      continues uninterrupted.

Part 3 (ops surface, SHA 43a3653):
- [x] inbox_status() returns enabled, prefix, poll interval, last run, counts.
- [x] `agent inbox-status` CLI prints the full status (read only, no side effects).

Part 4 (RSS episode matching, SHA 990d81f):
- [x] _latest_episode_from_db() queries podcast_episodes table for newest episode.
- [x] Plan Slack message header includes episode number, title, publish date.
- [x] _evergreen_check() rejects banned recency phrases in plan output; guard fires
      in header construction (replaces title, alerts via ops_alerts).
- [x] _mark_ep_matched() / _is_ep_matched() track inbox file -> episode linkage.

Part 5 (Monday 9am nudge, SHA 43a3653):
- [x] check_monday_nudge(): Monday gate, nudge-time gate (America/New_York), recency
      window (AGENT_EPISODE_NUDGE_WINDOW_DAYS, default 2 days), episode match check.
- [x] Idempotent: nudge key ep_guid + date stored in kv; second call same day is
      a no-op (status: already_sent).
- [x] Already-matched episode is silent (no nudge).
- [x] Stale episode outside window is silent.
- [x] Nudge slot added to _daily_scheduler (never crashes loop).

39 tests, all green. Suite 1000 passed, 7 pre-existing reportlab failures.

BLAKE BY HAND to arm this pipeline:
  1. Set AGENT_EPISODE_INBOX_ENABLED=true in Railway.
  2. Ensure AGENT_HOSTING_ENABLED=true + R2 credentials set (for list_prefix).
  3. Ensure AGENT_CLIPPER_ENABLED=true (Phase 1 clip selection).
  4. Set AGENT_PODCAST_FEED_URL (for RSS episode matching in the plan header).
  5. Set AGENT_EPISODE_INBOX_PREFIX if the default is wrong
     (default: echo/episode_inbox/lasso_episodes/).
  6. Optional: AGENT_EPISODE_INBOX_POLL_MINUTES (default 5),
     AGENT_EPISODE_NUDGE_TIME (default 09:00),
     AGENT_EPISODE_NUDGE_WINDOW_DAYS (default 2).
  7. Export a finished episode from Riverside, drop it in the inbox prefix.
  8. Within one poll cycle, a ranked clip plan appears in #echoclaude.

### Stage 2 foundation (2026-07-09 buildout; ten parts, every flag defaults OFF)
- [~] Saturday fix locked: with AGENT_CATEGORY_ROTATION on the planner posts all
      seven days (August plans 31/31; flag off keeps the Saturday skip, 26)
- [~] 14-day review cycle: AGENT_REVIEW_WINDOW_DAYS (default 14) windows the
      day30 assembler (now the cycle report; 30-day window keeps the DAY 30
      title); pre-Echo cadence baseline comparison stays on the fixed 30-day
      basis; creative refresh ask once per account per cycle behind
      AGENT_REVIEW_CYCLE_ENABLED (OFF), wired into run_daily
- [~] intake-create: one intake JSON scaffolds a tenant under
      brand_voice/tenants/<key>/ (voice.md, avatar.md, verified_facts.md USE
      lines feeding the fabrication gate, tenant.json with approver + sender
      phones + media lanes + trust 0 + quota fields); blocks loud on missing
      fields, all-or-nothing; AGENT_INTAKE_ENABLED
- [~] Trust ladder wired to tenants: level_for_tenant reads only the named
      tenant's record, fail-safe to FULL_APPROVAL; a new tenant can never
      auto-publish (level 0 + double gate + first-post gate, adversarially locked)
- [~] Media inbox core: provider-agnostic queue behind AGENT_MEDIA_INBOX_ENABLED;
      sender phone -> tenant (never guessed; unknown = HELD + one masked alert
      per sender per day), idempotent by sha256, texted sentence = caption note
- [~] Ingest worker: perceptual dedupe per tenant, consent + autotag hooks,
      thumbnail, tenant-scoped R2 keys via media_host isolation; CAPTION GATE:
      no sentence = not filed + one auto-ask; attach_caption releases
- [~] GHL adapter: Ed25519 X-GHL-Signature verified BEFORE parsing; photos
      captured immediately (carrier URLs expire); video MIME auto-replies with
      the tenant's tokenized upload link; AGENT_GHL_INTAKE_ENABLED
- [~] WhatsApp adapter: X-Hub-Signature-256 (HMAC) verified before parsing,
      16MB WABA ceiling (refused, never truncated), same queue;
      AGENT_WHATSAPP_INTAKE_ENABLED. DO NOT ARM before the
      whatsapp_business_messaging App Review addition is granted
- [~] Upload quotas + tenant token watchdog: per-tenant storage cap enforced at
      the upload endpoint (413 over a MEASURED total; unmeasurable or legacy
      never blocks), monthly recreate budget kv-counted per month; the token
      watchdog flags upload-lane tenants with no AGENT_INTAKE_TOKEN_<KEY> set
- [~] Per-gym tenant brain: brains/<tenant>.md append-only learning events
      (approve_streak / edit_diff / deny_reason / kill); killed concepts
      excluded from THAT tenant's rotation only; style rules + deny reasons
      fold into prompts THROUGH the fabrication gate (the brain never adds
      facts); AGENT_TENANT_BRAIN_ENABLED
- [x] July 16-31 replanned for both accounts (plan-month --from 2026-07-16,
      days 1-15 structurally untouched), 16 pending drafts per account held
      for approval in the LOCAL sandbox store. BLAKE BY HAND: run the same two
      plan-month commands on the deployed listener with AGENT_CATEGORY_ROTATION
      + AGENT_PLAN_MONTH_ENABLED armed in Railway env (the sandbox store is not
      the deployed store)

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
