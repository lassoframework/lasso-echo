# Calendar and Approvals Spec

Source of truth: `agent/approvals.py`, `agent/drafter.py`, `agent/trust.py`, `agent/quotas.py`.

---

## 30-Day Calendar Layout

The calendar shows one month of content for a gym. Each day may have zero or more draft cards. Days with no drafts are empty.

### Per-Day Card Data

Each card represents one draft. Fields the portal displays:

| Field | Source | Notes |
|---|---|---|
| `draft_id` | Unique ID for this draft | Used in approval API calls |
| `status` | One of the states below | Drives badge color and available actions |
| `caption_preview` | First ~60 chars of the caption | Never the full caption in the calendar view |
| `platform` | `instagram` or `facebook` | |
| `scheduled_for` | ISO 8601 datetime | The target posting time in the gym's timezone |
| `platform` | `instagram`, `facebook`, `google_business` | Badge |

---

## Draft States

Verified against `DraftStatus` enum in `agent/drafter.py`:

| State | Label | What it means | Available actions |
|---|---|---|---|
| `pending` | Waiting for approval | Human tap required before anything publishes | Approve, Edit, Skip, Deny, Kill |
| `approved` | Approved | Human approved; published (or would publish in draft-only mode) | None |
| `skipped` | Skipped | Human chose to drop this draft | None |
| `superseded` | Superseded | A newer draft for the same day replaced this one; approving it does nothing | None (read-only, show the newer card instead) |
| `expired` | Expired | The posting day passed while the draft was still pending | None (read-only) |
| `blocked` | Blocked | Cannot draft, typically missing voice doc | None (staff must fix the underlying issue) |

---

## Approval Actions and Their Semantics

These actions are LIVE today via Slack. They are PLANNED for portal-native buttons behind `AGENT_PORTAL_APPROVALS` (not yet defined in Echo's config as of HEAD `fea0e31`).

### Approve

Human confirms the draft is ready to post. Echo calls `publish()` on the Meta or GBP publisher. If the media is still processing on Meta's side, the action is held and the card stays `pending` with a note: "Held: media was still processing. Approve again in a minute to retry." Nothing publishes until Approve succeeds.

**Portal display (today, before AGENT_PORTAL_APPROVALS):** Show the caption and a note: "Approve this post in your Slack channel." No button that sends to Echo.

### Edit (with note)

Human provides an edit instruction. Echo re-drafts the post using the note, replaces the card with a new `pending` draft, and the old draft is marked `superseded`. The recreate budget is NOT charged for an Edit (Deny charges the budget; Edit does not).

**Portal display:** Show a text field for the note + "Request edit" button. Until AGENT_PORTAL_APPROVALS is live, show "Send your edit note in Slack."

### Deny (reason)

Human rejects the draft with a reason. Echo marks the draft `denied` and records the reason in the tenant brain (best effort, behind `AGENT_TENANT_BRAIN_ENABLED`). The recreate budget is charged one unit. When the budget is exhausted, the action still proceeds but no budget unit is logged (the caller is responsible for surfacing the empty-budget state).

Reason chips (suggested in the UI, free text also accepted):
- "Off voice"
- "Wrong offer"
- "Timing not right"
- "Use a different photo"
- "Caption needs work"

**Budget meter:** The portal should display how many recreates remain this month. Default cap is 20 for Stage 2 tenant-onboarded gyms (from `tenants.DEFAULT_MONTHLY_RECREATE_BUDGET`); 0 for legacy env-token clients.

**Portal display today:** "Deny in Slack with your reason." (Portal-native buttons require `AGENT_PORTAL_APPROVALS=true`.)

### Kill (permanent, free, confirm dialog, bans the concept)

Human permanently kills a draft concept for this gym. Kill is free (does not charge the recreate budget). The concept is recorded in the tenant brain so Echo will not regenerate it for this gym. Requires `confirmed=True` in the action call — the portal must show a confirm dialog before sending.

**Confirm dialog text:** "This permanently removes this concept for [gym name]. It will not come back. Are you sure?"

**Portal display today:** "Kill in Slack." (Portal-native buttons require `AGENT_PORTAL_APPROVALS=true`.)

### Skip

Human drops the draft with no action. No recreate. No concept ban. The draft is marked `skipped` and the slot is empty.

---

## Trust Ladder States

What the gym sees depends on their trust level. Verified against `agent/trust.py`:

| Level | Name | Behavior |
|---|---|---|
| 0 | FULL_APPROVAL | Every post waits for a human tap. **All gyms start here. This is the default forever.** |
| 1 | ROUTINE_AUTO | Routine calendar posts may auto-publish after a human approved the monthly calendar. Not armed for any gym today. Requires `AGENT_TRUST_LADDER_ENABLED=true`. |
| 2 | TRUSTED | Future: wider auto-publish, off-template still surfaces. Not defined for any gym today. |

Double gate: even at level 1, nothing auto-publishes unless `AGENT_TRUST_LADDER_ENABLED` is armed. This flag is not set for any gym today.

**Portal display at level 0 (today, everyone):** Every draft card shows an approval prompt. No draft is labeled "will auto-publish."

**Portal display at level 1 (future):** Routine calendar drafts inside an approved monthly calendar are labeled "scheduled to auto-post." Off-template drafts still show an approval prompt.

---

## What Is Live Today vs Planned

| Feature | State |
|---|---|
| Slack approvals (Approve/Edit/Skip via Slack) | LIVE |
| Draft lifecycle (pending/approved/skipped/superseded/expired/blocked) | LIVE in Echo |
| Portal calendar read-only display | PLANNED (awaits calendar API endpoint) |
| Portal-native approval buttons | PLANNED (awaits AGENT_PORTAL_APPROVALS flag) |
| Recreate budget meter in portal | PLANNED |
| Trust level display | PLANNED |

---

## Portal Build Instructions (Phase 1)

Build the calendar as a read-only display. Show a holding message on each day card: "Approve posts in your Slack channel." Link to the gym's Slack approval channel (stored in the portal's gym record alongside the token).

Do not build approval buttons until `STATUS.md` says `AGENT_PORTAL_APPROVALS` is live.
