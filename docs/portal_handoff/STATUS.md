# Echo Portal Status

**Last updated:** 2026-08-06 | **SHA:** 9fe4162 — PART A merged (per-gym calendar engine + 3-tier collision + approval-surface routing + baseline capture, all behind `AGENT_PORTAL_SOCIAL_ENABLED`, default OFF) + demo distinct-art fix. Independent audit: A+, zero critical/major; full suite 2008 passed. Prior: 698e7948 (portal PR #238) + Zernio connect (Echo). Blake ruling 2026-07-29: **Zernio is the social vendor; SocialAPI.ai connect is retired** (publish lane + book-launch queue pending a separate migration).

This is a standing coordination file. Any Echo build that changes the portal-facing contract must update this file in the same commit. The portal CC reads this at the start of every session and updates PORTAL OWES when it ships something.

---

## LIVE vs PLANNED

Endpoint-level. One line per Echo endpoint the portal calls or will call.

| Endpoint | State | Gate / notes |
|---|---|---|
| `GET /healthz` | LIVE | No gate; answers even when intake is off |
| `GET /intake/<token>` | LIVE | `AGENT_INTAKE_ENABLED`; serves HTML intake form |
| `POST /intake/<token>` (JSON) | LIVE | `AGENT_INTAKE_ENABLED`; portal submits intake, returns `upload_url` |
| `POST /intake/<token>` (form) | LIVE | `AGENT_INTAKE_ENABLED`; gym submits form directly (HTML path) |
| `GET /u/<token>` | LIVE | `AGENT_INTAKE_ENABLED`; serves HTML upload page |
| `POST /u/<token>` | LIVE | `AGENT_INTAKE_ENABLED`; gym uploads photos/videos to R2 |
| `GET /portal/gym/<account_key>` | LIVE | `AGENT_PORTAL_APPROVALS`; returns token status, upload link, R2 upload count |
| `GET /api/calendar/<key>?month=YYYY-MM` | PLANNED | Portal cannot render live calendar until Echo ships this |
| `POST /api/approve/<key>/<draft_id>` | PLANNED | Portal cannot send approval actions until Echo ships this |
| `GET /api/report/<key>?days=30` | PLANNED | Portal cannot display live report until Echo ships this |
| `GET /portal/<token>/social-status` | LIVE (Zernio) | Gated by ZERNIO_API_KEY; revoked gym -> 404. Returns `{platforms:{instagram:{connected,handle,expired},facebook:{...}}}` |
| `GET /portal/<token>/social-connect?platform=instagram\|facebook` | LIVE (Zernio) | Per-platform; returns `{oauth_url}` from Zernio authUrl |
| `GET /portal/<token>/facebook-pages` | LIVE (Zernio) | Returns `{pages:[{id,name}]}` |
| `POST /portal/<token>/facebook-page-select` | LIVE (Zernio) | Persists the gym's chosen page (Echo injects per post) |
| ~~`GET /portal/<token>/social-connect` (SocialAPI.ai)~~ | RETIRED | Superseded by the Zernio per-platform routes above (Blake ruling 2026-07-29). SocialAPI connect handlers remain as dead code pending the full SocialAPI purge. |
| `GET /portal/<token>/social?month=YYYY-MM` | BUILT-behind-flag | `AGENT_PORTAL_SOCIAL_ENABLED` (default OFF => 404). Stripe social product ACTIVE else 402+empty. Month calendar for THIS gym: `{account_key,month,active,posts:[{day_key,status,pillar,format,image_public_url,caption}],recreate_budget:{limit,used,remaining},low_creative,days_remaining}`. Token isolation: every row scoped to account_key. |
| `POST /portal/<token>/posts/<id>/approve` | BUILT-behind-flag | Same flag+Stripe gate. Idempotent (already APPROVED => 200 no-op). Delegates to portal_approvals (gated publish, no new path). |
| `POST /portal/<token>/posts/<id>/edit` | BUILT-behind-flag | Body `{actor_id,note}`. Re-runs the fabrication gate on the note => 422 on an unsupported claim. |
| `POST /portal/<token>/posts/<id>/deny` | BUILT-behind-flag | Body `{actor_id,note}`. Decrements a SERVER-enforced 15/month recreate budget (kv, per account+month) => 409 when exhausted; budget charged only on a successful deny. |
| `POST /portal/<token>/posts/<id>/kill` | BUILT-behind-flag | Body `{actor_id,confirm}`. Permanent + FREE (never charges budget); requires `confirm=true` else 400. |
| `GET /portal/<token>/metrics?days=N` | BUILT-behind-flag (shape only) | Same flag+Stripe gate. Returns the PART D payload SHAPE with values null/empty (Zernio analytics pull is Part C). Missing metrics are GAPS, never a fabricated 0. `analytics_available`/`report_available` read `AGENT_ZERNIO_ANALYTICS_ENABLED`/`AGENT_MONTHLY_REPORT_ENABLED` (both OFF). |

---

## PORTAL OWES

What the portal CC is building next, in priority order:

- [x] Intake wizard — 7-section form, two acknowledgment checkboxes, POST JSON to `POST /intake/<token>`, show `upload_url` as "Upload your media now" button after submit _(shipped portal PR #238, commit 698e7948)_
- [x] Media upload hand-off — after intake submit, link or redirect gym to `/u/<token>` (Echo serves that page; portal does not build upload UI) _(shipped portal PR #238)_
- [x] Gym status panel — call `GET /portal/gym/<account_key>` (requires `AGENT_PORTAL_APPROVALS=true`) to show staff: token status, upload link, last upload timestamp, upload count _(shipped portal PR #238 — /command-center/social-status)_
- [x] Calendar page — read-only display; show "Approve posts in your Slack channel" on every card until `GET /api/calendar/<key>` is live _(shipped portal PR #238 — /command-center/social-calendar; holding state until Echo ships calendar API)_
- [x] Reporting page — show "Reporting coming soon" holding card until `GET /api/report/<key>` is live; when live, display gaps explicitly, never substitute zero for a missing metric _(shipped portal PR #238 — /command-center/social-report; holding card live)_
- [ ] Approval action buttons — Approve, Edit, Skip, Deny, Kill wired to `POST /api/approve/<key>/<draft_id>`; Kill requires a confirm dialog; do not build until that endpoint is in STATUS.md as LIVE
- [x] Social Connections page — client /my tab (portal PRs #342 + #344, LIVE): per-platform Connect Instagram / Connect Facebook (+ per-platform Reconnect), a "Which Page is your gym?" picker (facebook-pages + facebook-page-select, one-Page auto-select behind a confirm), all against the Zernio routes. Portal decrypts the gym token server-side and holds NO credentials or Page ids.

---

## PART A — PORTAL CLIENT-SOCIAL BACKEND (per-gym calendar + routing)

Behind the master flag `AGENT_PORTAL_SOCIAL_ENABLED` (default OFF; flag off = every
new hook inert, pipeline byte-for-byte unchanged). Branch `feat/portal-social-partA`.
Endpoints stay PLANNED until deploy; this section tracks the BACKEND ENGINE only.

- [x] Per-gym calendar ENGINE — `agent/gym_calendar_queue.py`. Generalizes the demo
  calendar to per-gym dated queues keyed by (gym_id, account_key, day_key), each row
  carrying gym_id + zernio_profile_id + account_key. New table `gym_calendar_queue`
  mirrors the demo queue shape; the served-once-per-day lock is kept (new
  `served_ledger` table: at most one served post per (account_key, day_key)). The
  LASSO demo is one gym; client gyms are additional gyms. Client CONTENT generation is
  OUT OF SCOPE for Part A (later phase).
- [x] THREE-TIER collision priority (Blake ruling; supersedes the earlier "book wins,
  shift") — the gym/demo calendar gets its own daily slot but is subordinate. Priority
  on any contested served_day per account: (1) live book queue FIRST, (2) welcome queue
  SECOND, (3) demo/gym calendar THIRD. The calendar serves its own slot and does NOT
  wait for the welcome queue to drain, but if book OR welcome occupies/served that
  account's day it SHIFTS to the next open day in the pillar rotation. It never
  displaces book or welcome and NEVER two posts on one served_day per account. Tiering
  lives in `gym_calendar_queue._day_contested` (`_book_queue_occupies` reads
  `book_queue.BOOK_POSTS`; `_welcome_queue_occupies` reads served rows off the
  welcome_queue table; the `served_ledger` table is the per-account per-day lock).
  Regression matrix in `tests/test_gym_calendar_queue.py`: both clear -> serves same
  day; book occupies (2026-08-12/15/19/22/26) -> shift; welcome occupies -> shift;
  shift lands on the next open day preserving rotation; book beats welcome beats
  calendar.
- [x] approval_surface ROUTING — `gym_calendar_queue.approval_surface_for(account)`
  returns "slack" for LASSO accounts (key starts "lasso") and "portal" for client
  gyms. `runner._post_and_save` SKIPS the Slack approval card for portal-surface
  drafts (they are approved on the portal) but STILL saves them PENDING +
  force_approval=True. ops_alerts (failures) STILL go to Slack for every gym. One
  draft lifecycle, two surfaces.
- [x] BASELINE capture (Part D dependency) — `baseline_posts_per_week` +
  `baseline_captured_at` columns added to the `gyms` table (additive migration);
  `db.set_baseline_posts_per_week(account_key, posts_per_week, captured_at=None)` and
  `db.get_baseline_posts_per_week(account_key)`. Timestamped on the gym record.
  Accepts a manual/explicit value now; the Zernio-history source is Part C.

NOTE: the per-gym approval ENDPOINTS the portal calls (`POST /api/approve/...`,
`GET /api/calendar/...`) remain PLANNED below. Part A ships the engine + routing that
those endpoints will drive; the endpoints themselves are a later phase (do not wire).

---

## ECHO OWES

What Echo CC must ship before the portal can wire each item. Named dependency pairs.

- [ ] `GET /api/calendar/<key>?month=YYYY-MM` — portal cannot render live calendar or show real draft states until this ships
- [ ] `POST /api/approve/<key>/<draft_id>` — portal cannot send Approve/Edit/Skip/Deny/Kill actions until this ships; Slack is the only approval channel until then
- [ ] `GET /api/report/<key>?days=30` — portal cannot display live 30-day report until this ships
- [x] Zernio connect routes SHIPPED (Echo, this commit): social-connect?platform, social-status, facebook-pages, facebook-page-select. Field mapping Echo owns is documented in the ZERNIO MAPPING block below.
- [ ] Reporting parity — a social vendor may expose only per-post likes/comments/saves/shares; impressions/reach/follower-count may be unavailable. When the report endpoint ships, a gym's report MUST show missing metrics as gaps ("not available on this account"), never a fabricated 0, and MUST name the data source per account. (Originally written for SocialAPI; applies to Zernio too. Zernio DOES expose follower/analytics fields, so re-verify availability at report-build time.)
- [ ] FOLLOW-UP (Blake ruling 2026-07-29, SocialAPI retired): fully remove the SocialAPI.ai publish lane + socialapi_client/socialapi_store modules and migrate the book-launch queue's posting path to Zernio. Separate build with its own audit — the book-launch posting path must be re-verified before removal.
- Echo must update STATUS.md in every commit that changes any portal-facing endpoint, flag, or response shape

### ZERNIO MAPPING (from docs.zernio.com/llms-full.txt, 2026-07-29) — Echo owns the translation; the portal contract above does NOT change
Echo brokers Zernio; the portal never sees Zernio. Echo must fold Zernio responses into the portal shapes above:
- **A gym = a Zernio profile.** Scope every call with the gym's `profileId`. Auth to Zernio is `Authorization: Bearer $ZERNIO_API_KEY` (portal never sees the key).
- **REUSE, don't re-create (verified live 2026-08-06).** LASSO (and other gyms) are already set up in Zernio, so their profiles pre-exist; `POST /v1/profiles` 409s `profile_name_conflict` on a duplicate. `GET /v1/profiles` returns `{profiles:[{_id,name,...}], total, skip, limit}` (id field `_id`, 24-char). `_ensure_profile_id` now resolves in order: stored `zernio_profile_id` -> find-by-name (`ZernioClient.find_profile_id`, exact then case-insensitive) -> create only if none -> on a 409, fall back to find-by-name. The resolved id is persisted per gym.
- **Connect:** Zernio `GET /v1/connect/{platform}?profileId=...` returns `{ authUrl }`. Echo maps `authUrl` -> `oauth_url`. Flow (one OAuth per platform) matches.
- **Status:** Zernio `GET /v1/accounts?profileId=...` returns a FLAT `accounts[]` with `{ _id, platform, username, status }`. Echo must fold this into `{ platforms: { instagram, facebook } }`, mapping `username` -> `handle` and `status`/webhook state -> `connected`.
- **EXPIRY IS PUSH, NOT PULL (most important).** Zernio has no `expired`/`expiresAt` field. Echo must subscribe to the Zernio `account.disconnected` webhook (also `account.connected`; at-least-once, dedupe on event id, max 10 webhooks/team), persist per-account state, and DERIVE the `expired` bool the portal's amber "Needs Reconnect" reads. The portal cannot poll expiry.
- **Facebook Page:** Zernio `GET /v1/accounts/{accountId}/facebook-page` returns `{ pages: [{ _id, name }] }` (id field is `_id`, map to `id`). Zernio has NO page-select endpoint — the Page is a default/per-post setting (`platformSpecificData.pageId`). Echo owns persisting the gym's chosen `page_id` and injecting it per post; the portal's `facebook-page-select` POST just hands Echo the choice.
- **OPEN (confirm with Zernio):** whether Instagram connect REQUIRES a linked Facebook Page (Meta normally does; Zernio docs are silent). Zernio docs have no Instagram section yet — verify IG is supported before arming the IG connect button.

---

## BLOCKED ON BLAKE

Items that cannot move until Blake takes a manual action:

- **District H token** — copy `AGENT_INTAKE_TOKEN_DISTRICTH` from Echo's Railway env; hand to portal CC via secure channel (not chat, not git) so it can be stored as an encrypted secret keyed to the districth account
- **District H Slack channel ID** — add to the gym's portal record so the calendar can link there
- **WhatsApp intake** — blocked on Meta App Review granting `whatsapp_business_messaging`; do not arm `AGENT_WHATSAPP_INTAKE_ENABLED` before that grant
- **Trust level 1 arm** — raising any gym above level 0 requires Blake to set `AGENT_TRUST_LADDER_ENABLED=true` and `AGENT_TRUST_AUTOPUBLISH_ENABLED=true` by hand; nothing in code does it automatically
- **SocialAPI lane arm** — the lane is built but dark. To use it Blake must: (1) set `AGENT_SOCIALAPI_KEY` in Railway env, (2) set `AGENT_SOCIALAPI_ENABLED=true`, (3) set `publish_route="socialapi"` on the gym's Account in `agent/accounts.py`, (4) run `python -m agent socialapi-onboard --account <key>` then hand the gym the `socialapi-connect` link. LASSO's own accounts stay `meta_direct`.
- **SocialAPI vendor confirmation (in writing)** — before arming, get written confirmation from SocialAPI.ai that their approved Meta app covers ORGANIC IG feed + IG Stories + FB Page publishing on CLIENT-owned accounts (their public docs confirm the API capability; the app-permission scope on client accounts is the business assurance to secure). This is the gate that lets Echo publish for clients without Blake's own Meta App Review.
- **SocialAPI redirect URI** — register the OAuth callback with the vendor dashboard and set `AGENT_SOCIALAPI_REDIRECT_URI` so the connect links return the gym to the portal.

---

## PART A — MERGED 2026-08-06 (SHA 9fe4162), independent audit A+

**Shipped (Echo internal — NO new portal endpoints; all behind `AGENT_PORTAL_SOCIAL_ENABLED`, default OFF):**
- `agent/gym_calendar_queue.py` — per-gym calendar engine. Table `gym_calendar_queue` UNIQUE(gym_id, account_key, day_key), each row carries gym_id + zernio_profile_id + account_key. Draft ids `gcalf_`/`gcals_` (never collide with book_/demo_/welc_). Cross-queue served-once-per-day lock in a separate `served_ledger` table PK(account_key, day_key).
- Three-tier collision priority: **book queue > welcome queue > gym/demo calendar**. The calendar has its OWN slot (does not wait for the welcome queue to drain) but SHIFTS to the next open day in pillar rotation when book or welcome owns the day; never displaces either; never two posts per account per served_day. Build-time re-guard prevents same-cycle doubling.
- `approval_surface` routing: "slack" for LASSO (`key.startswith("lasso")`), "portal" for client gyms. Client drafts are saved PENDING + returned BEFORE any `post_approval_card` — client drafts NEVER card to #echoclaude. `ops_alerts` still fire to Slack. `force_approval=True` on all client posts (gate strengthened, no new publish path).
- `baseline_posts_per_week` + `baseline_captured_at` on the gym record (`db.set/get_baseline_posts_per_week`), manual/explicit now; Zernio-history source lands in Part C.
- Demo distinct-art fix: `agent/demo_calendar_render.py` renders 36 unique images (per-post variant keyed on (pillar, position)); `render_all` raises on any duplicate hash; no-digit/no-dash on-image asserts intact; one red per card.

**Descopes ruled INTENDED for Part A (not defects):** the engine is dormant in prod even flag-ON — `build_gym_calendar_draft` is not yet called by the runner and nothing seeds client gym rows; wiring lands with the content/endpoints phase. Client-caption dash/"vendor" gate and the empty-caption end-to-end interaction also belong to the content phase.

---

## PART B — TOKEN-SCOPED PORTAL ENDPOINTS (built behind flag, NOT merged)

Branch `feat/portal-social-partB`. All six endpoints BUILT behind the SAME master flag
`AGENT_PORTAL_SOCIAL_ENABLED` (default OFF => routes 404, byte-for-byte current behavior).
Endpoints stay PLANNED->BUILT-behind-flag until deploy; the contract is fixed above.

- [x] `agent/portal_social.py` — the six handlers. Every route: (1) master flag ON else
  disabled/404; (2) the gym's Stripe SOCIAL product ACTIVE else 402 + empty-state payload
  (fail closed: no product id configured / no customer id / no key / any read error =>
  not active, never a live calendar); (3) TOKEN ISOLATION proven on every route — reads
  scope every calendar/metric row to account_key; actions load the draft then REJECT it
  as 404 unless `draft.account_key == account_key` (a cross-gym id never even confirms the
  other gym's draft exists). `store.get(draft_id)` is not account-scoped, so this ownership
  re-check is the isolation guard.
- [x] `GET /social` — month calendar from `gym_calendar_queue`, scoped to account_key.
  `low_creative` = no queued (unserved) row left this month; `days_remaining` = whole days
  left in the calendar month (null when today is outside the month). recreate_budget state
  included.
- [x] `POST /posts/<id>/approve` — idempotent (already APPROVED => 200 no-op, never a double
  publish). Delegates to `portal_approvals.approve` -> `approvals.handle_action` (same gated
  publish Slack uses; NO new publish path).
- [x] `POST /posts/<id>/edit` — re-runs `rotation.is_gate_clean(note)`; a note carrying a
  stat/percent/price with no approved receipt => 422. Clean note delegates to
  `portal_approvals.edit`.
- [x] `POST /posts/<id>/deny` — server-enforced 15/month recreate budget in
  `portal_social.spend_recreate` (kv key `portal_recreate_spent_<account>_<YYYY-MM>`); 409
  when exhausted; the budget is charged ONLY after a successful deny (a failed/unauthorized
  deny never costs a unit). Per-account isolated (key carries account_key).
- [x] `POST /posts/<id>/kill` — permanent + FREE (never touches the budget); requires
  `confirm=true` else 400. Delegates to `portal_approvals.kill(confirmed=True)`.
- [x] `GET /metrics` — the Part D payload SHAPE with null/empty values (documented in
  `_metrics_shape`). Missing metrics are GAPS ("no numbers are shown rather than a made up
  zero"), never a fabricated 0. The real baseline (Part A) is included so Part D's
  before/after has its "before" the moment analytics land.
- [x] Wiring: `agent/intake_web.py` routes `/portal/<token>/social`, `/portal/<token>/metrics`
  (GET) and `/portal/<token>/posts/<id>/{approve|edit|deny|kill}` (POST). Token->account via
  the existing `client_for_token`; unknown/revoked token = 404 (indistinguishable).
- [x] `portal_approvals` gate EXTENDED (not rebuilt): the account-scoped functions are now
  callable when EITHER `AGENT_PORTAL_APPROVALS` OR `AGENT_PORTAL_SOCIAL_ENABLED` is armed, so
  Part B's master flag alone is enough. Both OFF => inert, unchanged.
- [x] Migrations (additive): `gyms.stripe_customer_id` (gym -> Stripe customer for the
  active-product check). Flags: `AGENT_ZERNIO_ANALYTICS_ENABLED`, `AGENT_MONTHLY_REPORT_ENABLED`
  (both default OFF), plus env `STRIPE_SOCIAL_PRODUCT_ID` (the social product id; empty =>
  no gym reads as active).
- [x] Copy rules: no em/en/hyphen dashes and never "vendor" in any client-facing string
  (runtime message test + source-literal grep). Verified stats only.
- [x] Tests: `tests/test_portal_social.py` (25) — flag-off disabled, Stripe-not-active 402,
  token isolation on EVERY route (social/metrics reads + approve/edit/deny/kill actions +
  per-gym budget), budget 15 + 409 exhausted + free kill + failed-deny-no-charge, kill
  confirm, edit fabrication 422, approve idempotent, metrics gaps-not-zeros, HTTP layer
  (unknown token 404, flag-off 404, action routing carries the resolved account_key,
  kill confirm plumbed). Full suite 2033 green.

**Descopes ruled INTENDED for Part B (numbered for Blake, none block the contract):**
1. Metrics returns the SHAPE only; the live Zernio analytics numbers are Part C (behind
   `AGENT_ZERNIO_ANALYTICS_ENABLED`, dead until Blake confirms the analytics add-on).
2. `stripe_customer_id` is set by hand / by onboarding; nothing in Part B provisions it. A
   gym with no customer id reads as not-active (402), which is correct until onboarding
   fills it.
3. Client CONTENT (captions/images) is still a later phase; `/social` shows whatever the
   Part A engine rows already hold (may be empty), never a fabricated caption.

BLAKE RULINGS NEEDED (numbered): (a) confirm `STRIPE_SOCIAL_PRODUCT_ID` is the correct
Stripe product id for the client-social subscription, and (b) confirm 15/month is the
recreate budget (currently a code constant `MONTHLY_RECREATE_BUDGET`, not per-gym tunable).

---

## PART B HANDOFF (paste as ground truth for the next session)

Part A shipped the per-gym calendar ENGINE, the 3-tier collision rule (book > welcome > gym calendar, shift-not-displace, served_ledger lock), approval-surface routing (clients never card to Slack; force_approval holds them), and baseline storage — all behind `AGENT_PORTAL_SOCIAL_ENABLED` (default OFF), merged at SHA 9fe4162, independent-audit A+, full suite 2008 green. The Zernio CONNECT lane (`zernio.py`/`zernio_routes.py`) and the account-scoped approve/edit/deny/kill FUNCTIONS (`portal_approvals.py`, gated by `AGENT_PORTAL_APPROVALS`) already exist from the prior portal build — EXTEND them, do not rebuild. Part B builds the token-scoped HTTP endpoints: `GET /portal/<token>/social` (month calendar: posts, statuses, pillar, format, image public_urls, recreate-budget state, low_creative flag + days_remaining), `POST /portal/<token>/posts/<id>/{approve|edit|deny|kill}` (approve idempotent; edit re-runs the fabrication gate -> 422; deny decrements a server-enforced 15/month recreate budget -> 409 when exhausted; kill permanent+free+confirm=true), and `GET /portal/<token>/metrics` (payload shape = Part D). Every route: per-gym portal token auth, Stripe social product must be ACTIVE else 402/empty, and TOKEN ISOLATION is the hardest audit line — gym A's token must never read or act on gym B's anything, proven by a test on EVERY route. Flags stay OFF (`AGENT_PORTAL_SOCIAL_ENABLED`, `AGENT_ZERNIO_ANALYTICS_ENABLED`, `AGENT_MONTHLY_REPORT_ENABLED`). Start Part B with the full opener (git status, pull, suite, SHA, ground-truth re-read incl. this file). Blake by-hand still pending: confirm the Zernio Analytics ADD-ON is enabled on the account (Part C dead without it).
