# PROPOSAL: Put the draft ID in publish/skip confirmations

Status: PROPOSED — staged by Scout 2026-07-15, not applied. Echo lane owner decides.
No Echo code has been touched. This file is the entire change so far.

## Problem

Scout's morning brief reconciles the approval queue against Echo's resolution
signals in #echoclaude, keyed by draft ID. Most Echo signals carry the ID
(EXPIRED/SUPERSEDED tombstones, "Draft <id> not found", publish-verify alerts).
The one that does not is the approve/skip confirmation itself:

- `agent/approvals.py:162` builds `detail=f"{result.mode}: media_id={post_ref or '-'}"`
- `agent/listener.py:407-411` renders it as `*Approved* by <@user> \n published: media_id=...`

`media_id` cannot be mapped back to a draft. Live consequence (2026-07-14/15):
four published drafts (933dc8e874, feafb11ef3, 27ff4235f3, 56e520af1d) kept
rendering as "waiting" in the morning brief; re-taps risked double publishes.
Scout-listener now filters these by scheduled-day expiry, but same-day
approve-then-rebrief remains unreconcilable without the ID.

Secondary observation, worth its own look in the Echo lane: those four cards
were never edited to their Approved state — the confirmations landed as NEW
channel messages, which matches the `except` fallback in `agent/listener.py:411-412`
(`chat_update` failing, plain `chat_postMessage` taking over). The cards kept
live Approve buttons after publish.

## Proposed change (one line)

In `agent/approvals.py:162` (and the equivalent skip path), include the draft ID:

    detail=f"{result.mode}: draft {draft.draft_id} media_id={post_ref or '-'}"

Both the `chat_update` text and the fallback `chat_postMessage` inherit it, so
every confirmation becomes reconcilable no matter which path fires.

## Paired follow-up in scout-listener (separate approval, not part of this)

Add `\*Approved\* by|\*Skipped\* by` to `RESOLUTION_SIGNAL_RE` in
`scout-listener/src/brief.js` so the enriched confirmations count as
resolution signals. Harmless to ship before Echo does: today's confirmations
contain no `draft <id>` token, so nothing matches until Echo ships.

## Definition of Done

- Approve/skip confirmations in #echoclaude contain `draft <draft_id>`.
- A card approved at 8am no longer shows as waiting in a rerun brief the same day.
- Verified live on one real approval before checking the box.
