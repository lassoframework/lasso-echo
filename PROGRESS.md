# Echo Build Tracker

Living tracker for the Echo social agent build. This markdown is the source of
truth; the HTML dashboard (`echo_build_tracker.html`) is the visual view. The
full organic-system scope lives in `BUILD_SPEC.md`.

Status key: [x] done  ·  [~] built + tested in reference repo, push/deploy pending  ·  [ ] not started

Last updated: 2026-07-01 (FIRST LIVE CARD SHIPPED — Nano Banana infographic generated
+ R2-hosted + posted to #echoclaude, approval gate proven, publishing still OFF.
Cleared today: Gemini billing, model env, /data volume, R2 hosting auth, V3 palette,
clean house style. Next: drop personal FB, run the 30-day loop, then arm publishing.)

---

## Stage 0 — Foundation
- [x] Canonical LASSO brand bible (`brand_voice/lasso_voice.md`)
- [x] Reference repo scaffolded (`lasso-echo`), own body, Ranger spine as pattern
- [x] Gates baked into code (approval, draft-only, trust ladder, no-fabrication)
- [x] Test suite green (35 deployed; +5 Creative Studio in sandbox pending push)
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
- [~] Content brain: drafts the daily post from the source doc (brand_voice/lasso_now.md)
      across the 5 pillars, growth CTA, 5 hashtags, no fabrication; flag AGENT_CONTENT_BRAIN_ENABLED OFF
- [~] Google Business Profile posting branch (local posts): gbp_publisher, draft-only guard,
      routing (GBP -> gbp_publisher, IG/FB -> meta_publisher), content-brain GBP variant
      (trimmed summary, one image, CTA button, no hashtags); flag AGENT_GBP_ENABLED OFF. See BUILD_SPEC.md Addendum A
- [ ] Set Gemini key (AGENT_NANO_API_KEY) by hand; leave the flag OFF until output looks right
- [~] Run master ON / publish OFF, watch daily drafts
- [ ] Run the full 30-day loop once (see the 30-day IG plan below)
- [ ] Arm publishing once drafts look right

## Stage 2 — One paying client (hand-picked, forgiving)
- [ ] Brand voice intake template (turn the bible into a client questionnaire)
- [ ] Texted-link intake as the primary path (full-res upload page; MMS is fallback)
- [ ] Idempotent Railway ingest worker (convert, dedupe, moderate, tag, thumbnail)
- [ ] Client / team approval flow via the portal
- [ ] Prove the voice holds for someone who is not Blake
- [ ] Prove the 30-day refresh lands for a real client
- [ ] Document intake: client sends a PDF (texts + email); Echo extracts the text,
      splits it into N post ideas, drafts an infographic card per idea, holds ALL for
      approval. No-fabrication gate applies (PDF is raw material, never approved fact).
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
- [ ] Daily Stories posting (warm-audience signal)
- [ ] Caption SEO baked into the drafter (keyword guidance in notes)

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
