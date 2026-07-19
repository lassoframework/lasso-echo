# Echo Portal Status

**Last updated:** 2026-07-19 | **SHA:** 67594a7d92492c95f795c45563e149809246aae9

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

---

## PORTAL OWES

What the portal CC is building next, in priority order:

- [ ] Intake wizard — 7-section form, two acknowledgment checkboxes, POST JSON to `POST /intake/<token>`, show `upload_url` as "Upload your media now" button after submit
- [ ] Media upload hand-off — after intake submit, link or redirect gym to `/u/<token>` (Echo serves that page; portal does not build upload UI)
- [ ] Gym status panel — call `GET /portal/gym/<account_key>` (requires `AGENT_PORTAL_APPROVALS=true`) to show staff: token status, upload link, last upload timestamp, upload count
- [ ] Calendar page — read-only display; show "Approve posts in your Slack channel" on every card until `GET /api/calendar/<key>` is live
- [ ] Reporting page — show "Reporting coming soon" holding card until `GET /api/report/<key>` is live; when live, display gaps explicitly, never substitute zero for a missing metric
- [ ] Approval action buttons — Approve, Edit, Skip, Deny, Kill wired to `POST /api/approve/<key>/<draft_id>`; Kill requires a confirm dialog; do not build until that endpoint is in STATUS.md as LIVE

---

## ECHO OWES

What Echo CC must ship before the portal can wire each item. Named dependency pairs.

- [ ] `GET /api/calendar/<key>?month=YYYY-MM` — portal cannot render live calendar or show real draft states until this ships
- [ ] `POST /api/approve/<key>/<draft_id>` — portal cannot send Approve/Edit/Skip/Deny/Kill actions until this ships; Slack is the only approval channel until then
- [ ] `GET /api/report/<key>?days=30` — portal cannot display live 30-day report until this ships
- Echo must update STATUS.md in every commit that changes any portal-facing endpoint, flag, or response shape

---

## BLOCKED ON BLAKE

Items that cannot move until Blake takes a manual action:

- **District H token** — copy `AGENT_INTAKE_TOKEN_DISTRICTH` from Echo's Railway env; hand to portal CC via secure channel (not chat, not git) so it can be stored as an encrypted secret keyed to the districth account
- **District H Slack channel ID** — add to the gym's portal record so the calendar can link there
- **WhatsApp intake** — blocked on Meta App Review granting `whatsapp_business_messaging`; do not arm `AGENT_WHATSAPP_INTAKE_ENABLED` before that grant
- **Trust level 1 arm** — raising any gym above level 0 requires Blake to set `AGENT_TRUST_LADDER_ENABLED=true` and `AGENT_TRUST_AUTOPUBLISH_ENABLED=true` by hand; nothing in code does it automatically
