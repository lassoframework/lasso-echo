# Echo Portal Handoff Package

## What Echo Is

Echo is LASSO's organic social AI. It drafts Instagram and Facebook posts, manages a per-gym content calendar, routes drafts through a human approval gate, and (when Blake arms the publish flag) pushes live to Meta. Echo runs as a Python service on Railway. It owns all social data, drafting logic, content state, and truth. The portal is Echo's front door and display layer.

**Live service:** https://echo-intake-web-production.up.railway.app

## What the Portal Builds

The portal (ops.lassoframework.com) is a Next.js application. Its social work is:

1. A gym onboarding intake wizard that posts the completed form to Echo and hands the gym a media upload link.
2. A 30-day content calendar that displays drafts, their states, and approval actions.
3. A reporting view that displays the assembled 30-day report Echo produces.

The portal owns: page layout, navigation, authentication (Clerk), display logic, and the staff-facing admin shell.

Echo owns: all intake processing, draft lifecycle, approval state, social proof gating, recreate budgets, publishing, and every number in a report.

## The Boundary (read this before every build)

| Portal builds | Echo owns |
|---|---|
| Intake form UI (sections, fields, submit button) | All intake parsing, PENDING sources, account proposals |
| Media upload redirect (shows the /u/<token> link) | R2 storage, file validation, tenant isolation |
| Calendar render (day cards, state badges) | Draft lifecycle, approval state, recreate budget |
| Approval action buttons (Approve / Edit / Deny / Kill / Skip) | Approval enforcement, publish gate, re-draft queue |
| Reporting display (numbers, charts, health label) | All report assembly, Graph API reads, gap flags |
| Staff admin shell | All social-facing data and truth |

**The portal never writes to Echo's data store directly. Every write goes through an Echo endpoint. Every display reads data Echo produced.**

## Build Order

Phase 1 (build now): intake form, media upload hand-off, calendar read-only display. The portal reads draft state from Echo's calendar endpoint (PLANNED) and displays it.

Phase 2 (build when AGENT_PORTAL_APPROVALS is armed): portal-native approval buttons wired to Echo's approval API. Until then, approvals happen via Slack. The calendar is read-only.

Phase 3: reporting display, social grade, posting frequency trend.

## Reading This Package

1. Read `STATUS.md` first and last. It tells you what is live vs planned and what the portal currently owes.
2. `API_CONTRACT.md` is the authority on every Echo endpoint the portal calls.
3. `INTAKE_FORM_SPEC.md` and the accompanying HTML are the source of truth for the intake wizard.
4. `CREATIVE_UPLOAD_SPEC.md` covers the media upload lane.
5. `CALENDAR_APPROVALS_SPEC.md` covers the calendar and all approval states.
6. `REPORTING_SPEC.md` covers the reporting display.
7. `ONBOARDING_RUNBOOK.md` is the human sequence for adding a gym.
8. `INTEGRATION_TEST_SCRIPT.md` is the end-to-end test against the live service.

## Standing Housekeeping Rule

At the start of every portal CC session: read `STATUS.md`. Update the PORTAL OWES section when you ship something. Any Echo build that changes the portal-facing contract must update `STATUS.md` in the same commit.
