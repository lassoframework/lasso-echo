# Echo x Google Business Profile — Full Build Spec (v2)

**Product:** Done For You Social, Google Business rail
**Goal:** Everything Echo does for Facebook and Instagram, done for Google Business Profile. Connection in the Portal, month planned by Echo, owner approves, Zernio publishes, results reported.
**Repo:** lasso-echo-work | **DB:** Supabase `lasso-ops-portal` (`ooqcvmcjspeltuuhcvlh`) | **Publisher:** Zernio (Late)

---

## 0. Phase 0 — Preflight (blockers, resolve before writing code)

| # | Check | Why it blocks | Fallback if no |
|---|---|---|---|
| P0.1 | Analytics add-on is on the Zernio plan (`/v1/analytics/googlebusiness/*`) | Phase 7 reporting is the sales proof | Ship posting without the report page; do NOT put GBP numbers in the client deck until confirmed |
| P0.2 | GBP gallery upload (`gmbmedia` create/list/delete) is enabled on the plan | Photo drops (Phase 4.4) | Cut photo drops from v1; posts still carry images |
| P0.3 | Per-connection cost: each GBP location is a Zernio account. Confirm seat/account pricing | Multi-location gyms could multiply cost | Cap v1 at one location per gym |
| P0.4 | Every DFY gym already has a Zernio `profile_id` from the FB/IG rail (Echo provisioning, `echo_account_key` pattern) | Connect flow needs it on day one | Provision profiles for GBP-only gyms through the same Echo path first |

Nothing below starts until these four have answers in writing.

---

## 1. Why GBP is not just a third checkbox

FB and IG posts are seen by people who already follow the gym. GBP posts are seen by strangers actively searching "gym near me" on Google Search and Maps. Different audience, different job, different copy.

That changes three things versus the FB/IG rail:

1. **Copy rules differ.** No hashtags (dead weight on GBP). Keywords and city name matter. Google truncates to roughly the first 80 characters in Search, so the hook carries the whole load. Max 1,500 characters; 150 to 300 is the target.
2. **Posts have machinery FB/IG do not.** Topic types (update / event / offer), a real CTA button, coupon codes, event date ranges. The planner and the approval card must carry these fields.
3. **The win metric is different.** Not likes. Calls, direction requests, website clicks, booking clicks — all reported by Google and exposed through Zernio's analytics API.

Everything else — the queue, the human tap, the learning loop — reuses the FB/IG rail unchanged.

---

## 2. Architecture (mirror of the FB/IG rail)

```
Portal "Connect Google Business" button
        │  Zernio standard OAuth (Zernio-hosted location picker)
        ▼
gym_gbp_connections                               ← connection state
        │
Echo planner (extended with GBP lane)
        │  writes plan via existing Echo write path
        ▼
content_calendar  account='googlebusiness'        ← read side mirror (portal displays)
        │  owner approves in Organic Social tab / Slack (same queue, same buttons)
        ▼
Echo publish worker → Zernio posts API
        platform: 'googlebusiness' + platformSpecificData
        │  late_post_id stored back, webhooks update status
        ▼
Zernio GBP analytics API → monthly report additions
```

Hard rule carried over: `content_calendar` stays a read side mirror. Portal never writes approvals directly to it. Approve/edit/deny flows through Echo's write path exactly like FB/IG.

---

## 3. Phase 1 — Connection

### 3.1 Portal UI and flow

- In the gym's **Connections** area, next to the Instagram/Facebook rows, add a **Google Business** row: Connect button + status pill (Not connected / Connected / Needs reconnect).
- On click, portal API calls Zernio:
  `GET /v1/connect/googlebusiness?profileId={zernio_profile_id}&redirect_url={portal_callback}`
  using the gym's existing Zernio profile (P0.4) and redirects the user to the returned `authUrl`.
- **v1 uses Zernio's standard (hosted) flow, not headless.** Zernio hosts the location selection UI after OAuth, then redirects back to the portal callback. Zero custom picker code, zero guessing about the pendingDataToken payload. Headless + custom picker is a v2 polish item only if the hosted UI proves confusing for owners.
- **Multi-location gyms: one connect pass per location.** Each pass produces one Zernio account bound to one GBP `locationId`. The portal row lists each connected location under the gym. (Cost per pass confirmed in P0.3.)
- On callback, portal identifies the new account by diffing `accounts_list` (scoped to the profile) against already-stored `zernio_account_id`s and matching on the account's GBP `locationId` — never "take the newest," which breaks under two concurrent connect passes. Upsert the connection row.
- **Timezone source:** at callback, read the location's timezone from the Zernio account / GBP location metadata and write it to the row. Only if unavailable, fall back to the default AND flag the row for staff to set it manually — a wrong timezone publishes a California gym's posts at 5am.

### 3.2 Storage

New table (sibling to `gym_external_connections`, keyed to work with BOTH id systems in play):

```sql
create table gym_gbp_connections (
  id uuid primary key default gen_random_uuid(),
  portal_gym_key text not null,             -- canonical join to content_calendar.gym_id (text; 'lasso' for dogfood)
  gym_uuid uuid references gyms(id),        -- nullable; set when the gym has a gyms row
  gym_location_id uuid references gym_locations(id),  -- nullable for single-location gyms
  zernio_profile_id text not null,
  zernio_account_id text not null,          -- one per connected GBP location
  gbp_location_id text not null,            -- 'locations/123456789'
  location_name text,
  timezone text not null default 'America/Indianapolis',  -- publish scheduling reads this
  status text not null default 'connected', -- connected | needs_reconnect | disconnected
  connected_by text,
  connected_at timestamptz default now(),
  last_ok_at timestamptz,
  updated_at timestamptz default now(),
  unique (portal_gym_key, gbp_location_id)
);
-- RLS: staff read/write, client reads rows matching own gym only. Same policy shape as gym_external_connections.
```

**Canonical join:** worker resolves an approved `content_calendar` row to a connection via
`content_calendar.gym_id = gym_gbp_connections.portal_gym_key` (+ `gbp_location_id` when the row carries one). The dogfood listing uses `portal_gym_key='lasso'` with `gym_uuid` null — no fake `gyms` row needed.

### 3.3 Token health and reconnect

- Zernio owns the Google tokens. Portal stores no Google credentials, ever.
- Nightly job runs a cheap accounts read per connection. On auth failure: `status='needs_reconnect'`, portal pill flips, staff alert fires, **planner pauses GBP for that gym** so the queue never fills with unpublishable posts.
- **Reconnect is in place:** the reconnect button reruns the connect flow for the same `portal_gym_key + gbp_location_id`; the row is updated (new `zernio_account_id` if Zernio mints one) rather than inserted, and historical `late_post_id`s are left untouched — they are Zernio post ids, still resolvable under the profile.

### 3.4 Acceptance

- [ ] Owner connects in under 60 seconds; only external stop is the Google consent + Zernio location screen
- [ ] Multi-location gym connects two locations via two passes; both listed under the gym
- [ ] Revoked access flips the pill within 24h, pauses the planner, alerts staff
- [ ] Duplicate connect attempt upserts, never duplicates (unique constraint proves it)

---

## 4. Phase 2 — Schema for GBP posts

`content_calendar` gains GBP fields. Nullable, ignored by FB/IG rows. Echo's own store mirrors the same shape.

```sql
alter table content_calendar
  add column if not exists gbp_topic_type text,   -- 'STANDARD' | 'EVENT' | 'OFFER'
  add column if not exists gbp_cta_type text,     -- 'LEARN_MORE' | 'BOOK' | 'SIGN_UP' | 'CALL' | 'ORDER' | 'SHOP'
  add column if not exists gbp_cta_url text,
  add column if not exists gbp_event jsonb,       -- { title, schedule:{startDate,startTime,endDate,endTime} } — EVENT posts AND the offer period on OFFER posts (Zernio uses event.schedule as the offer window)
  add column if not exists gbp_offer jsonb,       -- { couponCode, redeemOnlineUrl, termsConditions }
  add column if not exists gbp_location_id text,  -- routes multi-location gyms
  add column if not exists reject_reason text;    -- plain English rejection surface; written by §6.4 (photo drops), §7.1 (routing + send-time rail violations), §7.2 (webhook failures); displayed on the failed card in §6.2
```

**Status enum gains two values:** `draft → pending → approved → published`, plus **`failed`** and **`deleted`**.
A `failed` row NEVER auto-requeues. A coach fixes it and explicitly requeues (which routes through Echo's write path and, if the words changed, back through owner approval).

`account` value: `'googlebusiness'`. `format` values: `'update' | 'event' | 'offer' | 'photo' | 'review_reply'` (last one is Phase 7).

---

## 5. Phase 3 — Echo planner, GBP lane

### 5.1 Cadence (per connected location per month)

| Slot | Count / month | topicType | Notes |
|---|---|---|---|
| Local update | 8 (2/week) | STANDARD | The workhorse. Keyword + city led. |
| Offer | 1 | OFFER | Mirrors the gym's current live front end offer. Runs 7 to 14 days (planner default; validator hard cap 30 days). |
| Event | 0 to 2 | EVENT | Only when the gym actually has one (intake or coach input). Never invented. |
| Photo drop | 4 (1/week) | gallery upload | Requires P0.2 yes. Cut cleanly if no. |

Same content library as FB/IG — same photos, different words.

### 5.2 GBP copy rules (planner prompt additions, override IG style for `account='googlebusiness'`)

- **First 80 characters do all the work.** Hook = outcome + city. "Carmel moms: 6 weeks to your first strength milestone."
- **150 to 300 characters total.** Hard cap 1,500.
- **No hashtags. Ever.**
- **No phone numbers in post text.** Google rejects them. The CALL button exists for that.
- **Name the city or neighborhood once, naturally.**
- **The CTA button carries the ask.** Every STANDARD and EVENT post gets a CTA button; default `LEARN_MORE` per LASSO standard, override per gym record. **OFFER posts get NO callToAction** — Google renders its own "View offer" from `redeemOnlineUrl`; sending both is invalid.
- **UTM rule:** destination URLs get `?utm_source=google&utm_medium=organic_gbp&utm_campaign=echo_{pillar_slug}` where `pillar_slug` = lowercase, spaces to underscores, ascii only. On STANDARD/EVENT the UTMs go on `callToAction.url`; on OFFER they go on `offer.redeemOnlineUrl`. **CALL CTAs are exempt** (no URL exists).
- All LASSO rails still apply: no dashes, nothing invented, gen pop avatar, two level break shape (GBP renders line breaks).

### 5.3 Image rules — crop BEFORE approval

- Exactly 1 image per post. JPEG or PNG (Zernio auto-converts WebP). Max 5MB. Min 400x300.
- **The pipeline crops to 4:3 at 1200x900 at PLANNING time**, stores the processed file in Supabase storage, and puts THAT url on the plan row. The owner approves the exact pixels that publish. No post-approval image transformation, ever — otherwise the human tap approved a different image than the one that ships.
- Prefer faces and the gym floor. No heavy text overlays (quality signal + rejection risk). Never stock photos.
- Supabase storage URLs pass straight through (Zernio auto-proxies them).

---

## 6. Phase 4 — Portal approval queue

### 6.1 Queue changes

- Account filter gains a **Google Business** chip; calendar pills get a GBP color.
- Same statuses, same Approve / Edit / Deny / Kill buttons, same "tell Echo what to change" field. Zero new approval mechanics.

### 6.2 GBP approval card

- **Truncation preview:** first 80 characters bold, remainder dimmed — the owner sees what a searcher sees.
- CTA button preview (type + destination, tappable). OFFER cards show coupon, redeem URL, terms, and the offer window. EVENT cards show title + date range. Photo drop cards show the image and one line: "Adding this to your Google photos this week."
- Edit mode exposes caption, CTA type dropdown, CTA URL, structured event/offer fields with date pickers.
- **Failed cards** render `reject_reason` in plain English at the top, with a Requeue button (coach only) that routes through the Echo write path.
- **Validation:** char counts and date logic (end after start, offer ≤30 days) validate inline. **URL reachability checks run server side** (browser CORS makes client checks lie) and produce a soft warning, not a hard block — plenty of gym landing pages reject HEAD requests.

### 6.3 Approval writes

Approve/edit/deny flows through the same Echo write path as FB/IG. The edit payload may carry the GBP structured fields. Portal never writes `content_calendar` directly.

### 6.4 Photo drops (if P0.2 yes)

Photo drops obey the same gate as posts: the worker fires **at publish time** (`approved` + `scheduled_at` reached + connection checks in §7.1), calling Zernio's GBP media upload (`gmbmedia` create) instead of the posts API. **This endpoint has no webhook coverage — handle the API response synchronously:** 2xx → mark `published` immediately; error → `failed` + `reject_reason` + staff alert. Same one tap experience for the owner either way.

---

## 7. Phase 5 — Publishing worker

### 7.1 Preconditions, then the Zernio call

Worker fires on `approved` + `scheduled_at` reached, **after checking:**

1. Connection row status = `connected`. A `needs_reconnect` gym holds its posts silently — no failure spam. **On reconnect, held posts re-slot into the next valid 8 to 10am window** (never publish at a stale timestamp), and any OFFER row whose offer window lapsed during the outage reverts to `pending` for coach review instead of publishing a dead offer.
2. Row resolves to **exactly one** connection via `portal_gym_key` (+ `gbp_location_id`). The planner stamps `gbp_location_id` on every row for gyms with more than one connection. If the worker resolves 0 or 2+ connections, the row goes to `failed` with `reject_reason='connection routing'` and a staff alert — never a silent hold.

```json
{
  "content": "<caption>",
  "mediaItems": [{ "type": "image", "url": "<pre-cropped 1200x900 supabase url>" }],
  "platforms": [{
    "platform": "googlebusiness",
    "accountId": "<zernio_account_id>",
    "platformSpecificData": {
      "topicType": "STANDARD | EVENT | OFFER",
      "event":  { "...": "EVENT details, or the offer window on OFFER posts" },
      "offer":  { "couponCode": "...", "redeemOnlineUrl": "<utm tagged>", "termsConditions": "..." },
      "callToAction": { "type": "LEARN_MORE", "url": "<utm tagged>" },   // OMIT entirely on OFFER
      "locationId": "locations/123456789"
    }
  }]
}
```

Store the returned post id in `late_post_id` (existing column, existing pattern).

The worker **re-validates the rails at send time** (no phone number, no dashes, no hashtags, image present, CTA rules by topicType). Belt and suspenders: the planner should never produce a violation, and the worker refuses to ship one anyway. **A rail violation caught at send goes to `failed` with a plain English `reject_reason` and a staff alert** — same transition as a routing failure, never a silent hold.

### 7.2 Status reconcile and failure handling

**DECISION (Blake, 2026-08-15): option (b) — sync publish + a bounded reconcile poll. There is NO
existing Zernio webhook receiver (confirmed by code audit; FB/IG marks status synchronously from the
create_post response). A webhook receiver is a v2 upgrade, not v1.**

- **At send:** `create_post` returns → store `late_post_id`, mark `published` (mirrors FB/IG). GBP
  publishes async, so this is provisional until the reconcile confirms.
- **Reconcile poll (NOT nightly):** for each GBP post, poll `GET /v1/posts/{id}` **hourly for the
  first 48 hours after publish, then stop.** Google policy rejections land within hours; a rejected
  OFFER sitting unnoticed for a day is the worst case we care about. After 48h a post is settled.
- **When the reconcile finds a demotion, apply the full classification (never auto-requeue):**
  - Transport/5xx/rate limit → one `posts_retry`. If the retry also fails → `failed` + staff alert.
  - Policy rejection (phone number, image policy, gimmicky text, URL mismatch) → straight to
    `failed`, never retried. Write plain English `reject_reason`. Staff alert. Coach fixes and
    explicitly requeues; if the fix changes the words, it goes back through owner approval.
  - Deleted upstream → status `deleted`, log to activity, never repost.
- **v2 (later):** a real Zernio webhook receiver subscribing to `post.platform.{published,failed,
  deleted}` replaces the poll. Same classification logic, event-driven instead of polled.

### 7.3 Publish timing

No engagement-time algorithm on GBP. Publish weekday mornings 8 to 10am in the **connection row's `timezone`**. The value is listing freshness, not timing precision.

---

## 8. Phase 6 — Reviews (fast follow, ship after posting is stable)

Zernio exposes GBP reviews and owner replies (list + reply, PUT semantics — a second reply overwrites the first).

```sql
create table gym_gbp_reviews (
  id uuid primary key default gen_random_uuid(),
  connection_id uuid not null references gym_gbp_connections(id),
  portal_gym_key text not null,
  zernio_review_id text not null,
  rating int,
  reviewer_name text,
  review_text text,
  review_time timestamptz,
  reply_text text,                 -- what we sent (or null)
  reply_status text default 'none', -- none | drafted | pending_approval | sent | declined
  synced_at timestamptz default now(),
  unique (connection_id, zernio_review_id)
);
```

- Nightly pull of new reviews.
- Echo drafts replies in the gym's voice: thank by first name, reference the specific detail, invite the next visit. Never defensive on negatives. **Never any incentive for reviews** — listing suspension territory.
- Drafts land in the same approval queue as `format='review_reply'`. **Human tap before any reply posts. No exceptions.**
- 1 to 2 star reviews fire a staff alert immediately on pull — churn and reputation signal, not just content.

Schema'd now (above), built second. Posting proves the rail first.

---

## 9. Phase 7 — Reporting (requires P0.1 yes)

- `GET /v1/analytics/googlebusiness/performance` → daily: Maps/Search impressions, website clicks, call clicks, direction requests, booking clicks. 2 to 3 day delay, 18 months history.
- `GET /v1/analytics/googlebusiness/search-keywords` → monthly keywords that surfaced the profile.

Monthly report additions (client facing, plain English):

- People who found you on Google (impressions, Maps vs Search)
- Calls tapped, directions requested, website clicks
- The 5 searches that showed your gym most
- Posts published and the top post by clicks

Rollups stored in `gym_gbp_metrics` (same shape as `gym_ad_metrics`), every number carrying an as-of freshness stamp (the 2 to 3 day delay is visible, not hidden).

---

## 10. Guardrails (the rails, extended)

All six LASSO rails apply unchanged: human tap first, never invent, gen pop avatar, no dashes, confirm the CTA, one gym at a time. GBP adds four:

7. **No phone numbers in post text.** The CALL button is the phone number.
8. **No review bait.** Never post or reply offering anything for reviews. Listing suspension risk.
9. **Offers must be real and current.** Offer fields come only from the gym record, and the coach confirms the offer is live before the month plans. Strangers act on GBP offers; a dead redeem URL burns trust with people who have never met the gym.
10. **Owner's listing, owner's tap.** Nothing publishes and no review reply sends without their approval. And we never touch listing settings, categories, hours, or business info — posting only.

Enforced twice: in the planner prompts AND validated in the publish worker.

---

## 11. Rollout

1. **Week 1 — Dogfood.** Connect LASSO's own GBP listing (`portal_gym_key='lasso'`). Run a full planned month through the real rail.
2. **Week 2 — Shake out.** Fix rejections, tune 80 char hooks, verify analytics land (or confirm the P0.1 fallback).
3. **Week 3 — Founding five.** Offer GBP to the Echo beta gyms. Their connect flow is the acceptance test.
4. **Week 4 — In the deck.** Add the GBP page to the client PDF and coach SOP — the "found you on Google" numbers become the new proof block. Only numbers that P0.1 actually delivers.

---

## 12. A+ acceptance checklist

**Preflight**
- [ ] P0.1 through P0.4 answered in writing; fallbacks invoked where the answer was no

**Connection**
- [ ] Connect → hosted OAuth + location select → pill green, under 60s
- [ ] Multi-location: two passes, two rows, both listed; unique constraint blocks duplicates
- [ ] Revoked access: pill flips within 24h, planner pauses, staff alerted; reconnect updates in place

**Planning**
- [ ] Month plans at the 5.1 cadence from the gym's real library and real live offer
- [ ] Every caption passes: ≤300 chars, hook ≤80, city named, no hashtags, no phone, no dashes, correct CTA/UTM per topicType
- [ ] EVENT/OFFER only from the gym record; images pre-cropped to 1200x900 before the owner sees them

**Approval**
- [ ] GBP chip filters; card shows truncation preview, CTA button, structured fields
- [ ] The image approved is byte-identical to the image published
- [ ] All approvals via Echo write path; zero direct `content_calendar` writes

**Publishing**
- [ ] Worker checks connection status first; holds instead of failing on needs_reconnect
- [ ] Correct payload per topicType (OFFER omits callToAction); `late_post_id` stored
- [ ] Transient failure retries once; policy rejection goes to `failed` with plain English reason and never auto-requeues
- [ ] Photo drops handled synchronously (or cleanly absent if P0.2 was no)

**Reporting**
- [ ] Impressions, calls, directions, clicks, top keywords in the monthly report with as-of stamps (or P0.1 fallback invoked and the deck makes no analytics promise)

**Guardrails**
- [ ] All 10 rails in planner prompts AND worker validation
- [ ] Review replies (when built) never send without the human tap

Grade is A+ when every box checks against the dogfood month, not against intentions.
