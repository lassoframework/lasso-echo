# Echo Build Tracker

Living tracker for the Echo social agent build. This markdown is the source of
truth; update the boxes as work completes. The HTML dashboard is the visual view.

Status key: [x] done  ·  [~] ready / in progress  ·  [ ] not started

Last updated: 2026-07-01

---

## Stage 0 — Foundation (the prove-it groundwork)

- [x] Canonical LASSO brand bible written (`brand_voice/lasso_voice.md`)
- [x] Reference repo scaffolded (`lasso-echo`), own body, Ranger spine as pattern
- [x] Gates baked into code (approval, draft-only, trust ladder, no-fabrication)
- [x] Test suite green (31/31: one-per-account, approval required, draft-only no
      network, missing voice blocks, non-approver denied, tokens never logged,
      plus growth pack — CTA rotation, hashtag cap, carousel)
- [x] Stage 1 build prompt for Claude Code (`echo-stage1-build-prompt.md`)
- [x] Railway + separation plan documented (own project, own service, #echoclaude)
- [x] Brain hook stubbed (read-only, proposes, never rewrites voice)

## Stage 1 — LASSO only: draft + approve + publish (DRAFT-ONLY)

Build (reference implementation done; real deploy pending):
- [x] Daily drafter, one feed post per account
- [x] Slack approval cards (Approve / Edit / Skip) to #echoclaude
- [x] Meta publisher with two-place draft-only guard (publish flag OFF)
- [x] Post logging for reporting (no tokens)
- [x] Growth pack: CTA rotation on every draft
- [x] Growth pack: hashtag cap (5)
- [x] Carousel support (multi-slide drafts + IG carousel publish path, draft-only)
- [x] Push growth pack to origin/main (dde2f3a)

Blake-by-hand and deploy (open):
- [ ] Create lasso-echo repo on GitHub
- [ ] New Railway project `lasso-echo`, service `echo`, own env vars
- [ ] Meta App + Graph permissions approved (instagram_content_publish,
      pages_manage_posts, pages_read_engagement, instagram_basic, pages_show_list)
- [ ] Per-account tokens + target ids set by hand in Railway
- [ ] Slack app + #echoclaude channel wired (bot token, channel id)
- [ ] Brand voice doc approved by Blake (currently canonical draft in repo)
- [ ] Host creative library media so IG has public URLs
- [~] Run master ON / publish OFF, watch daily drafts for several days
- [ ] Run the full 30-day loop once, learn what good looks like
- [ ] Arm publishing (AGENT_PUBLISH_ENABLED=true) once drafts look right

## Stage 2 — One paying client (hand-picked, forgiving)

- [ ] Brand voice intake template (turn the bible into a client questionnaire)
- [ ] Client / team approval flow via the portal
- [ ] Prove the voice holds for someone who is not Blake
- [ ] Prove the 30-day refresh lands for a real client

## Stage 3 — Productize ($99/mo)

- [ ] Launch Echo as the $99 Social Media add-on (the revenue target: $99/mo for
      active ad clients, $199/mo for non-ad clients)
- [ ] Template the brand voice intake, approval flow, calendar, monthly review
- [ ] Portal exposes creative library to Echo (read)
- [ ] Portal hosts the reporting dashboard (write/display)
- [ ] Onboard client by client; $99 starts stacking

## Stage 4 — Scale automation (near-zero-touch)

- [ ] Per-account trust ladder climbing (routine auto-publish inside approved
      calendars; off-template still surfaces)
- [ ] Multi-account oversight, one human owns exceptions + monthly review
- [ ] Nightly brain loop armed (read brain + performance, propose, never auto-edit
      voice)

## Stubs (documented, intentionally not built yet)

- [ ] Stories posting
- [ ] Comment handling (Tier 1 auto-safe / Tier 2 surface / no auto DMs)
- [ ] 30-day creative refresh loop (the product)
- [ ] Portal creative-library read
- [ ] Portal reporting write
- [ ] Nightly brain read

---

### Reporting to wire in Stage 3 (per account, per 30-day cycle)

Engagement (rate + raw), saves, likes, comments, reach/impressions, follower
growth (net new + rate), posting frequency before vs after Echo, top 3 and bottom
3 posts by engagement, and a health read (growing / flat / declining).
