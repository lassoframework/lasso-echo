# Echo / LASSO Organic System — Canonical Build Spec

This is the full build-out scope for the LASSO organic social system that Echo
grows into. It is the reference every Claude Code session and agent run should
read before planning work, alongside `PROGRESS.md` (current state) and
`echo_build_tracker.html` (visual dashboard).

How the parts relate:
- Stage 1 (built, draft-only) is the proven core: draft, human-approve, publish.
- Everything below is Stage 2 to 4: the intake pipeline, the creative library/DAM,
  the creative runway, reporting, and the agent that ties them together.
- The non-negotiable gates never come off: human approval, per-account trust
  ladder, client content only (no fabrication), secrets by hand, human owns voice,
  ships behind flags default OFF.

---

# LASSO Framework: A-Plus Build Specification — Friction-Free Creative Intake, Diagnosis, Reporting, and the Embedded Social Media AI Agent

*A build-and-direction document written to be read by the LASSO social media AI agent (and its engineers) so it understands the full scope and how every part connects. Current as of July 1, 2026.*

## TL;DR
- The friction-free centerpiece should be a **texted short-link-to-upload pattern** as the PRIMARY path (client texts a keyword, gets back a per-gym branded upload URL that opens the phone camera/gallery and streams full-resolution files straight to Supabase Storage), because MMS irreversibly compresses media at the carrier or handset before it ever reaches Twilio or GHL. MMS/GHL-inbound is the automatic FALLBACK that still captures whatever arrives, and direct portal upload is the secondary path.
- The system is a **modular monolith inside the existing Next.js/Supabase/Clerk ops portal plus one Railway worker**, with an event-driven ingestion pipeline (webhook → queue → idempotent worker that converts HEIC/MOV, de-dupes via perceptual hash, AI auto-tags/moderates, generates thumbnails), a DAM with member-photo consent tracking, a "creative runway" burn-down diagnostic, automated Meta-Graph-sourced reports, and a Claude Agent SDK agent that reads all of it through a governed, tenant-scoped tool surface with human-in-the-loop approval gates.
- What makes it A-plus: hard multi-tenant isolation (Supabase RLS keyed to Clerk org claims), idempotent queued workers with retries, per-gym agent memory, confirmation texts that feel human, honest metrics (Meta's 2025 "views" migration handled correctly), and everything living in one place for the client.

## Key Findings

**1. MMS cannot deliver library-grade media; the winning pattern is a texted short link.** Carriers and the sending handset transcode and compress MMS *before* it reaches your provider. Twilio's official support documentation states verbatim: "Twilio does not perform any transcoding or resizing on incoming media attachments, but the handset or sending carrier will likely apply transcoding before the file reaches Twilio." Twilio also documents that Verizon allows images up to only 1.2 MB and videos up to 3.5 MB, while T-Mobile has a 1 MB send limit (3 MB receive). A gym owner's crisp 12 MP iPhone photo therefore arrives as a degraded ~1 MB JPEG, and no downstream cleverness recovers lost pixels. Best-in-class UGC tools (BrandLens, Flowbox Media Uploader, Tagshop ReviewHub) instead use a **browser-based capture page opened from a texted/QR short link — "no logins, no downloads"** — that streams originals to storage, preserving full resolution, HEIC/MOV, and EXIF. This is the recommended primary path. A second reason to avoid depending on inbound MMS webhooks: inbound MMS media URLs are ephemeral and expire shortly, so any media that does arrive by text must be downloaded and re-stored immediately.

**2. GHL Social Planner V2 is the publish path and has hard, per-platform media limits (confirmed from HighLevel's official limits doc).** Instagram Business accepts PNG/JPG up to **8 MB** and MP4/MOV video up to **100 MB, 3–60 sec**, up to 10 items per carousel; Facebook Pages accept images up to **10 MB** and video up to **1 GB / 20 min**; Instagram Reels allow MP4/MOV up to **1 GB, 3 sec–15 min**; Instagram Stories allow video up to 100 MB, 3–30 sec. Per-platform posting caps (per active user) include Instagram 200/hr and Facebook 200/hr. GHL's public V2 API rate limit is a **burst of 100 requests per 10 seconds and 200,000 requests/day, per marketplace app per resource** (Location or Company). Two build-time gotchas confirmed from HighLevel sources: (a) the **GHL InboundMessage webhook does not include the attachments array for WhatsApp/MMS media** — the official webhook schema shows `"attachments": []` on the SMS example, and GitHub issue #299 confirms the webhook omits attachments while the message-by-ID lookup can return them; and (b) the legacy webhook signature header **X-WH-Signature (RSA) is deprecated July 1, 2026** in favor of **X-GHL-Signature (Ed25519)** — which is today, so verify against the Ed25519 key.

**3. Meta deprecated impressions/plays in favor of a unified "views" metric (effective April 21, 2025).** Any report built today must use `views` for Instagram, not `impressions`. Meta gave partners a 90-day notice via its developer blog on January 8, 2025, then switched the API on April 21, 2025. Views can run **25%+ higher than impressions**, and Meta deletes IG impressions data recorded on/after January 1, 2025 to preserve year-over-year integrity — so pre- and post-migration figures are non-comparable and must never be charted as one trend line. The Instagram Basic Display API was **permanently shut down December 4, 2024**; all insights now come from the Instagram Graph API on Business/Creator accounts connected to a Facebook Page, at roughly **200 requests/hour/account**. This confirms Meta Graph as the authoritative insights source and GHL as the publish path.

**4. AI auto-tagging is baseline, not a differentiator — the 2026 bar is 90%+ confidence with a human-review layer for business context.** Vision models reliably tag objects, scenes, and colors but cannot infer "approved for spring promo" or a brand-specific taxonomy. The best-practice pattern (Aprimo, Fotoware, MTM): auto-apply high-confidence tags, flag low-confidence tags for review to avoid "dark assets," and pair AI tags with a small set of required human fields (consent status, campaign, usage rights).

**5. Production Claude Agent SDK patterns are mature in 2026.** The SDK provides the agent loop, subagents (isolated context, invoked via the `Agent` tool), sessions/resume, lifecycle hooks (PreToolUse/PostToolUse/Stop/SessionStart) for guardrails and audit logging, MCP for custom tools, and layered memory (CLAUDE.md always-in-context, a Memory Tool for on-demand recall, per-subagent stores added in early 2026). The dominant memory pattern is tiered: a small always-in-context core, plus vector/graph-backed retrieval, plus explicit consolidation/forgetting. Anthropic's 2026 "Outcomes" (rubric-graded output) and "Dreaming" (scheduled memory curation) features address quality enforcement and memory decay directly. Note the June 15, 2026 metering change: Agent SDK usage draws from a separate monthly credit pool.

## Details

### (a) End-to-end system architecture and data flow

**Topology.** Everything lives in the existing monorepo as a modular monolith: Next.js/TypeScript/Tailwind front end plus API routes on Vercel; Supabase (Postgres + Storage) as the system of record; Clerk for auth, organizations, and roles; and a single long-running **Railway worker** for anything slow, retryable, or scheduled (media transcode, AI enrichment, report generation, agent runs). Modules are logically separated — Ingestion, DAM, Runway, Reporting, Agent, Integrations — with clean internal interfaces so any can later be split into its own service if scale demands.

**Core data flow.**
1. **Capture** — Client texts a keyword (e.g., "PICS") to the gym's registered number, or taps a saved link/QR. The inbound text hits GHL/Twilio → webhook → the portal replies with a per-gym signed upload URL. (Fallback: any inbound MMS media is also captured.) Secondary path: staff or logged-in client uploads directly in the portal.
2. **Land** — The upload page streams originals directly to a Supabase Storage "inbox" bucket via a short-lived signed upload URL, writing a row in `ingestion_events` with an idempotency key.
3. **Enqueue** — The row insert / webhook enqueues a job on the Railway worker queue.
4. **Process (idempotent worker)** — validate → AV + content/brand-safety scan → HEIC→JPEG/AVIF and MOV→MP4 conversion (keep the original) → EXIF capture → perceptual-hash dedupe → thumbnail/preview generation → AI auto-tag + auto-caption → write a `creative_assets` row (status `pending_review` or `approved`).
5. **Confirm** — Send a human-sounding, em-dash-free confirmation text ("Got your 4 photos, thanks. They're in your library now.").
6. **Consume** — Assets flow into the DAM, feed the Runway calculation, get scheduled/published via GHL Social Planner V2, and their published performance is pulled back from Meta Graph into Reporting.
7. **Orchestrate** — The AI agent reads library, runway, and performance; drafts/schedules content; and raises alerts, all through governed tools with approval gates.

**Reliability spine.** Every webhook and upload carries an idempotency key; the worker is exactly-once at the effect level (safe to retry). Failed jobs go to a dead-letter queue with exponential backoff. GHL and Meta clients are rate-limit-aware (respect the documented headers; batch and cache). Verify GHL webhook signatures with the Ed25519 X-GHL-Signature key.

### (b) Friction-free text-to-portal ingestion — recommended design

**Primary: texted short-link-to-upload.** The gym owner (or a member) texts the gym's number. An automation replies instantly with a branded, mobile-first upload page at a per-gym signed URL (e.g., `app.lassoframework.com/u/{shortToken}`). The page uses a native file input that opens the camera/gallery, supports multi-select, shows live thumbnails and per-file progress, and uploads originals directly to Supabase Storage via short-lived signed upload URLs (bytes never transit MMS). This is the only path that reliably preserves full resolution, HEIC/MOV, and EXIF. Attribution is automatic because the short token maps to `gym_id`; no login is required.

**Why not MMS as primary:** carrier/handset compression is irreversible and happens before Twilio/GHL, so MMS yields degraded, small files unsuitable for a paid creative library. Twilio's own limits (5 MB total per message; carrier caps around 1–1.2 MB) and the ephemeral nature of inbound media URLs make it unfit as the quality path.

**Fallback 1 — MMS/GHL inbound capture.** Still accept whatever a client texts as media. Because the GHL InboundMessage webhook omits the attachments array for MMS/WhatsApp media, the worker must, on inbound-media events, retrieve the actual file via the GHL "get message by ID" endpoint (or the Twilio media URL) *immediately* — those URLs expire — then run it through the same pipeline, flagged `low_res_source=true` so the UI can nudge: "Text the link for full quality."

**Fallback 2 — direct portal upload.** A drag-and-drop uploader in the portal for staff and logged-in clients; always available, secondary.

**Optional future channel — WhatsApp Business Cloud API.** Higher media size limits than MMS and no carrier compression (webhook payloads up to 3 MB; media handled via media IDs). A strong future addition for owners who prefer WhatsApp; requires Meta Business verification and template approval, so treat as roadmap, not v1.

**Pipeline specifics.**
- **HEIC/MOV conversion:** server-side in the worker (libheif/ImageMagick/Sharp for HEIC → JPEG/AVIF; ffmpeg for MOV → H.264 MP4). Always retain the original in cold storage; generate web-friendly derivatives for preview and platform delivery. Browsers cannot render HEIC natively, so a JPEG/AVIF derivative is mandatory for the library UI.
- **Thumbnails/previews:** ffmpeg (`-ss` seek + single frame, `-frames:v 1`) for video posters; Sharp for image derivatives. Run in the worker (or a Fargate-style container job for large files), never inside a web request; seek past the first frame to avoid black thumbnails.
- **Deduplication:** SHA-256 for exact dupes; **pHash (perceptual hash) with a small Hamming-distance threshold** for near-dupes (resizes, re-compressions); store the hash on the asset row. pHash is excellent for exact/mild-recompression dupes but weak on crops/rotations, so add a CNN embedding second pass if transformed-dupe volume grows.
- **EXIF/metadata:** capture capture-date, geo (if present), device, and dimensions; strip sensitive EXIF from public derivatives while retaining it in the private record.
- **Virus/content scanning:** AV scan plus an image moderation API (Sightengine, Hive, AWS Rekognition, or Azure AI Content Safety) for nudity/violence/brand-safety before an asset is publishable; auto-reject clear violations, flag borderline, auto-approve clean, and start with conservative thresholds.
- **Confirmation replies:** always friendly, branded, and em-dash/en-dash/hyphen-free per the brand rule.
- **Compliance (A2P 10DLC):** US SMS/MMS requires A2P 10DLC brand + campaign registration; since February 2025 carriers block 100% of unregistered A2P traffic. Opt-in and confirmation copy must include business identity, message-frequency and rate disclosure, and STOP/HELP handling; avoid public URL shorteners (use a branded/dedicated short domain, which also improves deliverability). Track the FCC one-to-one consent rule timeline. Conversational inbound (client texts first, business responds) generally needs no separate written consent, which fits the texted-link flow well.

### (c) A-plus creative library / DAM design

**Metadata model.** `creative_assets` carries: `gym_id` (tenant), storage keys (original + derivatives), type, dimensions/duration, source channel, `sha256`, `phash`, EXIF, AI tags with confidence scores, AI caption, moderation verdict/scores, **consent/rights fields**, status (`pending_review`/`approved`/`archived`), and usage tracking. AI tags auto-apply above a confidence threshold; low-confidence tags and all business-context fields (campaign, approved-use, consent) require a human touch.

**AI auto-tagging + auto-captioning.** Use a vision model to produce searchable tags and a StoryBrand-aligned draft caption — route caption/scene reasoning to Claude, and high-volume tag classification to Haiku 4.5 for cost. Store confidence; surface a review queue for low-confidence or off-brand assets so they do not become unfindable "dark assets."

**Content moderation / brand safety.** Screen every asset on ingest across nudity/violence and other categories; auto-reject clear violations, flag borderline for human review, auto-approve clean.

**Consent / rights tracking (critical for a gym).** Gyms photograph members, so model a `consent` object per asset: whether a photo release is on file, which member(s) it covers, a release-document reference, and expiry. Use people/face detection to flag assets containing faces and require a consent record before those assets are publishable. This is both a legal safeguard and a premium-feel differentiator.

**Search, versioning, usage tracking.** Tag + caption + natural-language search across the library; link `creative_assets` to `scheduled_posts`/`published_posts` so an asset is never accidentally reposted and so Runway counts only *unused, approved* assets. Version derivatives without losing the original.

**Storage cost optimization at 100–200 gyms.** Keep one canonical original in standard storage, generate derivatives on demand or cache them, move rarely-touched originals to cheaper cold tiers, and dedupe aggressively so the same asset is never stored twice per gym.

### (d) Creative runway diagnosis

**The metric.** "Creative runway" = **days of content remaining** = (count of unused, approved, publishable assets) ÷ (posting cadence in posts per day). This is inventory burn-down applied to content: assets are inventory, the posting schedule is the burn rate, and runway is days-to-empty — analogous to a SaaS burn-rate dashboard or an aircraft glide-path indicator (the original metaphor behind the agile burndown chart).

**Calculation nuances.** Count only assets that are approved, moderation-clean, consent-cleared, and not yet posted. If a gym's cadence needs N reels + M photos per week, compute per-format runway and report the binding (shortest) constraint. Recompute on every asset add, every post scheduled, and nightly. Track scope changes (assets added mid-period) separately so the burn-down reads honestly.

**Visualization for a non-technical client.** One large "X days of content left" number with a color state (green/amber/red against the threshold), a simple burn-down line (assets remaining over time with a projected zero-date), and one clear call to action ("Text 6 more photos to stay 3 weeks ahead"). Keep it to a single glanceable card in the portal.

**Threshold alerting.** When runway drops below a per-gym threshold (default ~7 days), the agent proactively texts the client a friendly, specific request. Debounce alerts (do not re-fire daily) and escalate only if ignored.

### (e) Automated client reporting

**Data source.** Pull organic insights from the **Meta Graph API** (authoritative) for Instagram + Facebook: reach, **views** (not impressions — respect the April 2025 migration), plays, saves, shares, profile visits, follows, and a computed engagement rate. Manage the ~200 req/hr/account limit with batched requests, field-narrowing (`fields=`), and local caching; store daily snapshots so reports render instantly and retain history that Meta itself deletes.

**What clients love vs. ignore.** Clients ignore vanity dumps (raw likes/follower counts). They value outcomes with context (reach/saves/shares tied to what they mean), benchmarking (this month vs last, vs the gym's own baseline), a plain-language narrative ("Your reel on the 6am class drove your best week, here's why"), and a clear next step. Use tiered cadence: weekly data for content optimization (internal), a monthly client-facing report. Note that because engagement rate is now computed against `views` (typically higher than old impressions), reported engagement-rate percentages will look lower than historical ones — explain this once so clients do not misread it as a decline.

**AI-generated narrative.** Generate the summary with Claude Opus 4.8 (judgment work) from the structured metrics: lead with the win, explain the driver, flag one thing to improve, recommend the next action. Enforce the no-dash brand rule and StoryBrand SB7 voice. Never fabricate; write only from pulled data and note gaps explicitly.

**Delivery.** White-labeled in-portal dashboard (live, always current) plus a scheduled branded PDF using the LASSO palette (Navy #0F1B33, Red #EF3340, Cream #FAF6F0, Charcoal #1F1F1F), mobile-first. Reports live in the same one-place portal as the library and runway.

### (f) AI agent architecture (Claude Agent SDK)

**Role.** An autonomous per-gym social media manager that understands the whole system: it reads the creative library and runway, reads performance, drafts and schedules content, requests more creative when runway is low, and produces/annotates reports — always inside guardrails and approval gates.

**Model routing.** Opus 4.8 for judgment (strategy, narrative, "what's working and what to change"); Sonnet 4.6 for high-volume copy (captions, variations); Haiku 4.5 for high-volume classification (tagging, triage).

**Tool surface (in-process MCP tools / functions).**
- *Read:* `getCreativeLibrary(gym)`, `getRunway(gym)`, `getPerformance(gym, range)`, `getBrandProfile(gym)`, `getSchedule(gym)`.
- *Act (gated):* `draftPost`, `schedulePost(viaGHL)`, `generateReel(viaManus)`, `sendClientText`, `requestMoreCreative`, `generateReport`, `flagForHumanReview`.
Every act-tool is permission-scoped and tenant-scoped; an agent instance for gym A can never read or write gym B.

**Memory / per-gym context.** A per-gym long-term store (brand voice, offers, historically high-performing content, cadence, consent constraints, past decisions) using the 2026 tiered pattern: a compact always-in-context brand/core card, a retrieval layer (vector for similarity, optionally a small graph for entity relations) queried on demand, and explicit consolidation of "learnings" after each run (or via a scheduled Dreaming-style curation pass). Subagents run in isolated context and must receive everything they need in the prompt, since the prompt string is the only parent→subagent channel.

**Reasoning loop.** On each run: read runway + recent performance → identify what is working (which formats/topics drive saves/reach) → rewrite the plan (reorder the queue, draft new posts from top-performing themes, schedule) → if runway is low, trigger a creative request → log decisions and consolidate learnings.

**Triggers.** Scheduled — a monthly strategy pass and a daily maintenance pass, per the brief, via Railway cron; and event-driven — runway crosses threshold, new assets land, or a post significantly over/underperforms.

**Human-in-the-loop gates.** Publishing, client-facing texts, and reports pass through approval gates enforced with PreToolUse hooks (draft → LASSO staff approve → publish). High-confidence, low-risk actions (tagging, drafting into a review queue) can run autonomously; anything client-facing or irreversible is gated.

**Guardrails, permissions, observability.** Enforce tenant isolation at the tool layer. Use hooks for an immutable audit log of every tool call and decision (who/what/why/result), per-run cost tracking, and blocking of out-of-scope or dangerous calls. Store agent reasoning summaries for review. Apply an Outcomes-style rubric grade to check output quality before anything reaches a client. Detect both `Task` and `Agent` tool names in monitoring (the SDK renamed the tool but still emits the legacy name in some fields).

**Composition / skills.** Compose specialized skills/subagents — a Tagging skill, a Copywriting skill (StoryBrand SB7), a Reporting/Analyst skill, and a Runway-Guardian skill — coordinated by an orchestrator that holds per-gym context and delegates with full instructions in each subagent prompt. Keep the subagent count deliberate; subagent sprawl multiplies context, cost, and memory (a common production failure mode).

### (g) What makes each component A-plus
- **Ingestion:** zero-login texted link preserving full resolution; graceful MMS fallback that grabs ephemeral media before it expires; idempotent, queued, retryable processing.
- **DAM:** AI tagging with confidence plus human review, real member-photo consent tracking, usage tracking that prevents reposts, cost-optimized storage.
- **Runway:** one glanceable number with a projected zero-date and a specific text-back CTA; counts only truly usable assets.
- **Reporting:** Meta-authoritative, "views"-correct, benchmarked, narrative-led, white-labeled, outcome-focused.
- **Agent:** governed tool surface, per-gym memory, approval gates, full decision audit trail, cost-aware model routing.
- **Platform:** Supabase RLS keyed to Clerk org claims for hard multi-tenant isolation (index the tenant column, wrap `auth.jwt()` in a subselect for performance, never rely on the service-role key on the client); idempotency keys everywhere; queue + retries + dead-letter; rate-limit-aware GHL/Meta clients; premium client feel (instant human-toned confirmations, everything in one place).

## Recommendations
1. **Build the texted-link ingestion first** (primary path) with direct portal upload as the secondary; wire MMS/GHL inbound as a flagged, expire-aware fallback. Register A2P 10DLC brand + campaign in parallel — it gates all texting and can take up to ~10 business days.
2. **Stand up the idempotent Railway worker pipeline** (convert → dedupe → moderate → tag → thumbnail) before the DAM UI; correctness here is the foundation everything else rests on.
3. **Model consent from day one.** Do not let any member-face asset become publishable without a release on file; face-detect on ingest to enforce it.
4. **Build Runway as a single card plus one alert rule**, then let the agent own the text-back request when it fires.
5. **Build Reporting on `views`, not impressions**, with daily Meta snapshots cached locally; add a one-line explainer about the views/impressions change so clients do not misread lower engagement-rate percentages.
6. **Introduce the agent last, behind approval gates**, autonomous only for tagging and drafting; expand autonomy as audit logs and Outcomes grades prove reliability.

**Thresholds that change the plan:** make runway threshold and cadence per-gym config. If Meta's 200 req/hr limit bites at 100–200 gyms, move fully to batched, snapshotted nightly pulls. If a GHL Social Planner limit blocks a format (e.g., an IG video over 100 MB), fall back to publishing that asset via the direct Meta Graph API. If the agent's client-facing autonomy proves risky, tighten gates rather than widen them.

## Caveats and things to verify at build time
- **Verify against live docs at build:** exact current GHL Social Planner V2 per-platform media limits and the 100-req/10s + 200k/day rate limits; whether the GHL inbound webhook still omits MMS/WhatsApp attachments (issue #299); current Meta Graph API version, exact metric field names, and the 200 req/hr limit; A2P 10DLC and FCC one-to-one consent rules in force; and the GHL webhook signature cutover to X-GHL-Signature (Ed25519), whose stated deprecation of the legacy RSA header is July 1, 2026 — i.e., effective now, so implement Ed25519 verification immediately.
- **MMS is a lossy fallback, not a quality path** — set client expectations explicitly, and remember inbound media URLs expire and must be re-stored on receipt.
- **AI tagging and moderation are imperfect** — keep the human-review layer; never auto-publish member content.
- **Agent autonomy must be earned** — start gated, expand only with audit evidence and rubric-graded output; watch subagent sprawl for cost/memory.
- **Vendor/tool references are illustrative** (DAM, moderation APIs, agent-memory frameworks such as Mem0/Letta/Zep); confirm current pricing and capabilities at build. The Claude Agent SDK's separate metered credit pool (effective June 15, 2026) should be factored into run-cost budgeting.

## Addendum A — Google Business Profile posting channel (added July 2026)

Google Business Profile (GBP) is a first-class publishing channel in Echo,
alongside Instagram and Facebook. It matters for gyms because it is local-search
visibility, where gym buyers actually look. It is embedded scope, not optional.

API reality (verified July 2026): the Local Posts API is active and supports
standard, event, offer, and recurring posts at multi-location scale
(accounts/{id}/locations/{id}/localPosts). The Q&A API shut down November 2025.

Post shape (its own variant, never a copy of the IG caption): one image only
(jpg/png, no video, no carousel), up to 1500 characters, a structured CTA button
(LEARN_MORE, BOOK, ORDER, SHOP, SIGN_UP, CALL; all except CALL need a url), no
hashtags.

Access gate (client-onboarding long pole, start early): Google approval needs a
Cloud project, a verified profile 60+ days old, a business website, and a use-case
review, typically days to weeks; then OAuth 2.0 per location. Quota ~300 QPM.

Plugs into Echo: agent/gbp_publisher.py branch, draft-only guard, flag
AGENT_GBP_ENABLED default OFF, tokens by hand. The content brain writes a GBP
variant from the approved source doc only (no fabrication). Routing sends GBP to
gbp_publisher, IG/FB to meta_publisher. At 200-client scale, multi-location is
native; per-client OAuth tokens by hand; reviews via the Reviews API are a future
add under the Tier 2 human-approval comment policy.