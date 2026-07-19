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
| `pending` | Waiting for approval | Human tap required before anything publishes | Approve, Edit, Skip |
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

Human provides an edit instruction. Echo re-drafts the post using the note, replaces the card with a new `pending` draft, and the old draft is marked `superseded`. The recreate budget is NOT charged for an Edit (only a future Deny action will charge the budget; not wired today).

**Portal display:** Show a text field for the note + "Request edit" button. Until AGENT_PORTAL_APPROVALS is live, show "Send your edit note in Slack."

### Deny and Kill (PLANNED, not yet in approval handler)

Neither "Deny" nor "Kill" is a real approval action in `agent/approvals.py` today. The approval handler only knows: `approve`, `edit`, `skip`. The Slack surface only surfaces these three.

The tenant brain (`agent/tenant_brain.py`) records `deny_reason` and `kill` events as structured learning entries for the gym's voice tuning, but only behind `AGENT_TENANT_BRAIN_ENABLED` (default OFF). These are not approval gate actions.

**Portal display today:** Only show Approve, Edit, and Skip. Do not build Deny or Kill buttons.

**Future (PLANNED):** When a Deny/Kill approval action is added to Echo's approval handler and the portal approvals API is live, update the spec. The recreate budget mechanic (`quotas.py`) is already built in Echo and will be wired to Deny at that time.

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
