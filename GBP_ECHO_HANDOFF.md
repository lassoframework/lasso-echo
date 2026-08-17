# Google Business Profile — Echo Hand-Off

**From:** Portal build (Waves 0-4, shipped + live behind `GBP_RAIL_ENABLED=1`, 2026-08-17).
**To:** Echo session (planner + publish worker owner).
**Companion spec:** `GBP_BUILD_SPEC.md` (v2). This doc is the delta: what the portal already delivers,
and what Echo must build to complete the rail. Nothing publishes to a real GBP listing until the two
P0 safety gates below are wired.

---

## What the portal already delivers (the seam — build against this, do not rebuild)

- **DB (live in prod):**
  - `content_calendar` gained GBP columns: `gbp_topic_type` (STANDARD|EVENT|OFFER), `gbp_cta_type`
    (LEARN_MORE|BOOK|SIGN_UP|CALL|ORDER|SHOP), `gbp_cta_url`, `gbp_event` jsonb `{title, schedule:{startDate,startTime,endDate,endTime}}`,
    `gbp_offer` jsonb `{couponCode, redeemOnlineUrl, termsConditions}`, `gbp_location_id`, `reject_reason`.
    Status enum now includes `failed` and `deleted`. `account='googlebusiness'`;
    `format` in `update|event|offer|photo|review_reply`.
  - `gym_gbp_connections` — connection state, **portal-owned**. Canonical join:
    `content_calendar.gym_id = gym_gbp_connections.portal_gym_key` (text; `'lasso'` for dogfood),
    plus `gbp_location_id` for multi-location gyms. Carries `zernio_account_id`, `zernio_profile_id`,
    `gbp_location_id`, `timezone`, `status` (connected|needs_reconnect|disconnected).
  - `gym_gbp_reviews`, `gym_gbp_metrics` — schema'd; metrics populated by a portal cron (see below).
- **Connection flow:** the client connects GBP in their portal (entitled DFY-Social gyms only). The
  connection row is written by the portal. **Echo reads `gym_gbp_connections.status`.**
- **Approval UI:** clients approve/edit/deny in the portal (or Slack) exactly like FB/IG. The portal
  relays every action to Echo's existing write path:
  `POST /portal/<token>/posts/<id>/<action>` for `approve|edit|deny|kill|requeue`.
  - **`edit` now forwards GBP structured fields** in the payload (`gbp` object). Echo must accept them.
  - **`requeue`** is a new action (a coach fixes a failed row and requeues; if the words changed it must
    re-enter owner approval — Echo owns that routing).
- **Metrics pull:** the portal cron `gbp-metrics-sync` (daily) reads Zernio GBP analytics and upserts
  `gym_gbp_metrics`. It deliberately OMITS `posts_published` and `top_post_id` so it never clobbers the
  publish rail's values — **Echo's worker should set those two** on publish.
- **Health:** the portal cron `gbp-health` (nightly) flips a connection to `needs_reconnect` on auth
  failure and alerts staff. **Echo's planner must pause GBP for a gym whose connection is
  `needs_reconnect`** so the queue never fills with unpublishable posts.
- **Portal report + client surfaces are live.** Nothing else is needed from the portal for v1.

---

## P0 — Safety gates that BLOCK real OFFER posts (build these first)

### 1. OFFER only when the gym's live offer is confirmed
`GBP_BUILD_SPEC` §5.1 / §10 rail 9. The planner must NOT emit an OFFER post unless the gym's live
front-end offer is confirmed in Echo's gym record, and the coach confirms it before the month plans.
Validator hard cap: offer window ≤ 30 days (portal `validateGbpPost` already enforces the field shape as
belt-and-suspenders, but the planner is the real gate). **Until this is wired, keep the OFFER slot OFF
per gym.** STANDARD updates, EVENT, and photo drops run day one. Blake: "a wrong offer in front of
Google strangers is the one failure we cannot eat."

### 2. Coach screens the first month before the owner sees it
`GBP_BUILD_SPEC` §11 rollout; mirror the FB/IG onboarding pattern. The first planned month per gym is
coach-screened before it surfaces to the owner. The portal shows whatever Echo surfaces via
`fetchSocialMonth`, so Echo withholds month-1 from the client feed until a coach releases it.

---

## P1 — The GBP planner lane (`GBP_BUILD_SPEC` §5)

- **Cadence per connected location per month:** 8 local updates (STANDARD, 2/wk), 1 offer (OFFER, gated
  by P0.1), 0-2 events (EVENT, only when the gym actually has one — never invented), 4 photo drops (1/wk,
  gallery upload — see §6.4 / P0.2).
- **Copy rules (override IG style for `account='googlebusiness'`):** first 80 chars carry the hook
  (outcome + city); 150-300 chars, hard cap 1500; **no hashtags; no phone numbers in text** (CALL button
  instead); name the city once; every STANDARD/EVENT gets a CTA button (default `LEARN_MORE`); **OFFER
  gets NO callToAction** (Google renders its own View-offer from `redeemOnlineUrl`; sending both is
  invalid). UTM rule: `?utm_source=google&utm_medium=organic_gbp&utm_campaign=echo_{pillar_slug}` on
  `callToAction.url` (STANDARD/EVENT) or `offer.redeemOnlineUrl` (OFFER); CALL CTAs exempt. All LASSO
  rails: no dashes, nothing invented, gen-pop avatar, two-level break shape.
- **Image crop at PLANNING time:** exactly 1 image, cropped to 4:3 at 1200x900, hosted on **R2**
  (`media_host.host_media` — ratified by Blake 2026-08-17 over Supabase storage; R2 is where every
  other Echo creative already lives), and THAT public url on the plan row — the owner approves the
  exact pixels that publish. No post-approval transform. Zernio auto-proxies the public url.

---

## P1 — The publish worker (`GBP_BUILD_SPEC` §7)

- **Route via ZERNIO, not the legacy direct-Google lane.** `agent/gbp_publisher.py`
  (`mybusiness.googleapis.com/v4`, hand-set `AGENT_GBP_ACCESS_TOKEN`) is **SUPERSEDED** — do not extend
  it. All GBP publishing goes through Zernio's posts API. (Blake's ruling.)
- **Preconditions before the Zernio call:** connection `status='connected'`; the row resolves to exactly
  one connection via `portal_gym_key` (+ `gbp_location_id` when the gym has more than one); a
  `needs_reconnect` gym HOLDS its posts silently (no failure spam) and, on reconnect, held posts re-slot
  into the next valid 8-10am window; an OFFER whose window lapsed during an outage reverts to `pending`.
  0 or 2+ connection matches → `failed` + `reject_reason='connection routing'` + staff alert.
- **Payload** (Zernio posts API): `platform='googlebusiness'`, `platformSpecificData` = `{ topicType,
  event?, offer?, callToAction? (OMIT on OFFER), locationId }`, plus one pre-cropped 1200x900 media url.
  Store the returned id in `late_post_id` (existing column/pattern). Re-validate the rails at send time
  (no phone, no dashes, no hashtags, image present, CTA rules by topicType) — a violation → `failed` +
  plain-English `reject_reason` + staff alert, never a silent hold.
- **Photo drops (§6.4):** fire at publish time via Zernio `gmbmedia` create (no webhook coverage —
  handle synchronously: 2xx → `published`, error → `failed` + reason + staff alert).
- **Publish timing (§7.3):** weekday mornings 8-10am in the **connection row's `timezone`**.

### Failure handling — decided with Blake (webhooks are v2)
- v1 = **sync publish + poll reconcile** on `GET /v1/posts/{id}`, hourly for the first 48h after each GBP
  publish, then stop. (No webhook receiver in v1.)
- On demote/failure: `failed` + plain-English `reject_reason` + **staff alert**, **never auto-requeue**.
  A coach fixes and explicitly requeues (portal relays `requeue`); if the words changed, back through
  owner approval. Transport/5xx/rate-limit → one retry, then `failed`. Policy rejection → straight to
  `failed`. `post.platform.deleted` semantics → `deleted`, never repost.
- **All plumbing (reconnect, failed, 48h reconcile) alerts STAFF, not the client.** Clients never see it.

---

## P2 — Reviews (`GBP_BUILD_SPEC` §8), after posting is stable
Nightly pull of new reviews into `gym_gbp_reviews`; Echo drafts replies in the gym's voice (thank by
first name, reference the detail, invite the next visit, never defensive, **never any incentive for
reviews** — suspension risk). Drafts land in the same approval queue as `format='review_reply'`; **human
tap before any reply posts.** 1-2 star reviews fire a staff alert on pull.

---

## Provisioning note (sales lane, not planner)
GBP turns on per gym via `gbpGymEntitled(gymId)` = comped-exempt (`SOCIAL_SUBSCRIPTION_EXEMPT_GYM_IDS`)
OR a `gym_products` `product='social'` row with active status OR an active DFY-Social Stripe sub. The
Stripe-sub path already lights it up on signup. If you want the explicit `gym_products` social line
written at checkout, that is a sales/provisioning change (portal `recordGymProduct` currently writes
`ads`/`website` only) — not required for entitlement to work, but noted.

---

## Acceptance (the A+ bar, per §12) — grade against the dogfood month
- Month plans at the §5.1 cadence from the gym's real library + real live offer (offer only when
  confirmed).
- Every caption: ≤300 chars, hook ≤80, city named, no hashtags, no phone, no dashes, correct CTA/UTM
  per topicType; images pre-cropped to 1200x900 before the owner sees them.
- Worker checks connection status first; holds on `needs_reconnect`; correct payload per topicType (OFFER
  omits callToAction); `late_post_id` stored; transient retries once; policy rejection → `failed` +
  reason, never auto-requeue; photo drops synchronous.
- First month coach-screened before the owner sees it. Review replies never send without a human tap.
- Nothing publishes without the owner's tap. OFFER never ships on an unconfirmed offer.
