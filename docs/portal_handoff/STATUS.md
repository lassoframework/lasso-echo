# Echo Portal Status

**Last updated:** 2026-07-18 | **SHA:** fea0e313b4a3f8a2abefa3b9db2f211018dc3e8a

This is a standing coordination file. Any Echo build that changes the portal-facing contract must update this file in the same commit. The portal CC reads this at the start of every session and updates PORTAL OWES when it ships something.

---

## LIVE vs PLANNED

| Feature | State | Notes |
|---|---|---|
| `GET /healthz` | LIVE | Always up, reports `intake_enabled` flag state |
| `GET /intake/<token>` | LIVE | Serves HTML intake form to gym |
| `POST /intake/<token>` (JSON portal path) | LIVE | Portal submits intake; returns `upload_url` |
| `POST /intake/<token>` (urlencoded gym form path) | LIVE | Gym submits form directly |
| `GET /u/<token>` | LIVE | Serves HTML upload page to gym |
| `POST /u/<token>` | LIVE | Gym uploads photos/videos to R2 |
| R2 tenant isolation (`intake/<client>/incoming/`) | LIVE | Token resolves to client key |
| Storage quota enforcement (413 on over-quota) | LIVE | Per-tenant cap from `tenants.py` |
| PENDING sources from intake (listener ingest) | LIVE | Behind `AGENT_INTAKE_ENABLED` |
| Draft lifecycle (pending/approved/skipped/superseded/expired/blocked) | LIVE | `agent/drafter.py` |
| Slack approvals (Approve/Edit/Skip/Kill via Slack) | LIVE | `agent/approvals.py` |
| Trust ladder (level 0 FULL_APPROVAL) | LIVE | All gyms at level 0 today |
| Reporting assembly (`build_report()`) | LIVE | Behind `AGENT_REPORTING_ENABLED` |
| Graph API insights pull (live) | LIVE | Behind `AGENT_REPORTING_ENABLED` |
| Social grade (`compute_grade()`) | LIVE | Behind `AGENT_GRADE_ENABLED` |
| WhatsApp intake lane | PLANNED | Behind `AGENT_WHATSAPP_INTAKE_ENABLED`; awaits Meta App Review |
| Calendar API endpoint (`GET /api/calendar/<key>`) | PLANNED | Portal cannot render live calendar yet |
| Portal-native approval API (`POST /api/approve/<key>/<draft_id>`) | PLANNED | Behind future `AGENT_PORTAL_APPROVALS` flag |
| Reporting API endpoint (`GET /api/report/<key>`) | PLANNED | Portal cannot display live report yet |

---

## PORTAL OWES

- [ ] Intake wizard (7-section form, two acknowledgment checkboxes, POST to Echo, show `upload_url` button after submit)
- [ ] Media upload hand-off (redirect gym to `/u/<token>` after intake)
- [ ] Calendar page (read-only display; show "Approve in Slack" until calendar API is live)
- [ ] Reporting page (show "coming soon" holding card until reporting API is live)
- [ ] Store gym account_key (not the raw token) for portal gym status calls; Echo owns token storage
- [ ] Handle Echo `gaps` in report display (never substitute zero for missing metrics)

---

## ECHO OWES

- [ ] Calendar read API (`GET /api/calendar/<key>?month=YYYY-MM`) — not yet built
- [ ] Calendar read API (`GET /api/calendar/<key>?month=YYYY-MM`) — not yet built
- [ ] Reporting API (`GET /api/report/<key>?days=30`)
- [ ] Update STATUS.md in every commit that changes portal-facing contract

---

## BLOCKED ON BLAKE

- WhatsApp intake: blocked on Meta App Review granting `whatsapp_business_messaging`
- Trust level 1 (auto-publish): blocked on Blake arming `AGENT_TRUST_LADDER_ENABLED` and `AGENT_TRUST_AUTOPUBLISH_ENABLED` per gym
- Portal-native approvals: blocked on Echo building the approval API and defining `AGENT_PORTAL_APPROVALS` flag
