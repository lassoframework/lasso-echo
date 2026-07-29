# Echo Portal Status

**Last updated:** 2026-07-29 | **SHA:** 698e7948 (portal PR #238) + social connections (portal, LIVE) + Zernio connect (Echo). Blake ruling 2026-07-29: **Zernio is the social vendor; SocialAPI.ai connect is retired** (its publish lane + book-launch queue stay pending a separate migration).

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
