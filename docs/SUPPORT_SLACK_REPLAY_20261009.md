# Slack sender-pause replay (local draft)

This package replaces the adapter's stranded-event behavior only while
`SUPPORT_MESSAGES_FENCE_ENABLED=true`. The new migration is DRAFT and unapplied.
No production deployment, migration, flag activation, or Slack send was performed.

## Contract

The capture RPC inserts the inbound support message and its durable replay item
in one transaction. It preserves the exact Slack envelope, channel/timestamp
dedupe key, original resolved user/account/gym, bot and product, classification,
surface, and post-inbound ticket/request-version and message snapshots. Capture
still runs while sender admission is paused. An unavailable RPC fails closed;
there is no local queue or legacy outbound fallback.

The listener checks pending items before each outbox pass after resume, including
after a process restart. A five-minute token selects pure planning only. Existing
adapter ticket/outbound operations run against an explicit buffering bus. All
ticket fields and deterministic outbound IDs commit together under the token and
exact request, tenant, source, identity and transcript CAS. A changed snapshot is
held with its reason. A lost capture/claim/commit response retries the same key,
token, or frozen plan; a committed plan cannot be regenerated or changed.

When multiple messages arrive before the first request was dispatched, only the
newest snapshot can commit. Its saved dispatch context composes the initial
request and all subsequent inbound notes, preserving the original event records.
Earlier snapshots remain held and do not emit separate stale prompts.

An authorized staff note can supply the newest snapshot without becoming the
origin of a pending client request. Capture selects the original ticket owner
under the same bot, product, source, tenant, channel and thread binding. The
dispatch context keeps that owner's identity, original event/surface,
classification, unknown-user and rate-limit gates. Replay verifies both the
latest speaker and original owner against current identity resolution. Commit
rejects a plan whose actor, recipient kind or surface differs from that dispatch
origin. A staff note therefore cannot grant staff reply or safe-lane privileges
to a client-origin plan. Original STOP and human-takeover holds still prevent
replay. The normal code-fix deployment/verified-notice guard also remains in force.

Benign chatter is captured through the same transaction. The replay path uses a
strict acknowledgement allowlist, checked again by SQL; arbitrary trailing words
are regular follow-up intake. A pending initial issue followed by "thanks" routes
the initial issue from the newest snapshot once. After a plan has committed, a
benign note may renew delivery authority for exact-body, never-attempted ready or
approval-held rows. Their original IDs, bodies, plan and commit snapshots remain
immutable. Separate delivery snapshots record the renewal. A prior claim,
timestamp, uncertainty, substantive correction, changed identity or safety hold
prevents renewal. A held row stays held and receives no automatic approval.

Every resulting row carries `slack_replay_id` and its request version. The outbox
rechecks its original body, bot, product, source, tenant, channel/user/thread,
current request version and current safety authority immediately before internal
or client Slack dispatch. Normal arming, hard-line, verification and human release
gates still apply. STOP, explicit DND and human takeover keep the scoped replay
authority held.
This helper also considers prior inbound STOP/takeover messages on that bound
tenant/user/channel and staff takeover instructions on the exact ticket.
Production support tickets have no STOP, DND or takeover booleans. This authority
comes from verified inbound messages and their immutable replay context. Explicit
"DND" and "do not disturb" requests use that same durable opt-out authority.

A replay-tagged stale posting claim or ambiguous send/persistence result becomes
held with `slack_replay_delivery_uncertain=true`. It is never automatically
requeued or released by a button. That marker also blocks a local drain receipt.
Provider/database readback is required to reconcile it; this package does not
invent a provider receipt or mark an uncertain send delivered.

Replay delivery claims atomically persist a unique token and claim timestamp.
Completion and quarantine serialize on the message under that token. When
quarantine wins, a late provider timestamp is retained for reconciliation and
the row remains held and uncertain. When completion wins, stale recovery cannot
overwrite the posted receipt. Lost claim/completion responses reuse the exact
token and timestamp. Drain scans uncertainty independently of delivery status,
including rows incorrectly marked posted, and fail closed on incomplete scans.

A replay answer resolves only through a separate database CAS over its posted
receipt, claim token, original body, ticket identity, request version and complete
current inbound snapshot. A newer client message or same-version staff note
between completion and resolution leaves the current request open. Full resolution
proof stays in the private queue; it is not copied to client-visible attachments.
Ordinary untagged delivery and resolution behavior is unchanged.

## Release prerequisites and limits

- Apply the draft through the normal portal migration gate before enabling the
  fence. Keep the fence enabled after resuming so the durable replay worker remains
  available. Turning it off uses the original intake path and stops queue polling.
- Confirm the draft against the actual production support schema and request-cycle
  triggers. Local PostgreSQL 17 acceptance uses a minimal compatible support fixture;
  it does not prove the production schema, deployed listeners, or provider delivery.
- No historical inbound is adopted without its matching replay item. The former
  paused adapter did not store enough exact capture context to justify an automatic
  historical backfill. Such events require independently verified reconciliation.
- `CANCEL_POST` remains a visible durable held item with reason
  `external_cancel_requires_reconciliation`. Its external calendar mutation cannot
  be made atomic with this support-message transaction. No cancellation is claimed.
- Planning failures, identity changes, expired ownership, changed request/ticket
  snapshots and STOP/takeover remain explicit holds. There is no automatic unhold
  or replay of an ambiguous effect.
- Safety authority here governs this replay queue and its tagged outbox rows.
  Existing unrelated producers do not consult this new helper. This is not proof
  of ticket-wide STOP enforcement across every separate producer or of external
  GHL/SMS provider DND integration.
- Capture currently accepts Slack-conversation origin tickets. Existing portal
  origin Slack threads require an explicit producer/source handoff and are not
  silently adopted by this queue.
- The read immediately before a provider call binds current durable authority;
  it does not make a database read and an external Slack request one transaction.

## Local acceptance

`tests/test_support_slack_replay.py` runs the actual draft capture/claim/commit and
dispatch functions against a disposable existing PostgreSQL 17 runtime, through
the production Bus HTTP contract. It covers pause/restart/resume, duplicate and
multi-event intake, two-worker contention, lost responses, tenant/source/user/
request CAS, STOP/takeover, transaction rollback, expired tokens, private grants,
external-cancel holds, final outbox checks, ambiguous send quarantine, no repeat
send, manual-release refusal and drain blocking. No network or real Slack posts.

After the selected independent-review repairs, the latest affected integration
run passed **1094 tests in 32.64s** across replay, sender fence, Slack
adapter/autonomy/outreach/owner/repeat/cancel, portal ticket worker and client DM
lane suites. This includes 79 PostgreSQL 17 replay acceptance cases, both delivery
race outcomes, chatter renewal boundaries, status-independent uncertainty scans,
client/staff resolution races, and verified inbound controls against a fixture
without imaginary safety columns. The 17 additional cases cover pending client
requests followed by client/staff notes across armed/unarmed and safe/hold lanes,
original rate/safety gates, fresh identity checks for both actors, commit refusal
of forged staff-origin metadata, and one simulated grounded-question Slack post.
Code-fix acknowledgements remain suppressed by the normal verified-notice gate.
`git diff --check` passed. A fresh independent
integration review remains required.
