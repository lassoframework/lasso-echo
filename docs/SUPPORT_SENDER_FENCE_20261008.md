# Support sender cutover fence (local draft)

Default is OFF: `SUPPORT_MESSAGES_FENCE_ENABLED` must equal `true` to enable.
This gates only support bus delivery operations. Social posting is unaffected.
All identities using outbox.run_once, outreach._send, operator releases and the
portal ticket worker intake/fixed passes share one process admission gate.
Existing queued and held rows are unchanged when a new operation is refused.
Already admitted operations finish, including nested sends, while new operations
are refused. The active count includes recovery, claims, network delivery, receipt
persistence and resolution. No stale posting row is recovered while paused.

For a running-process drain, provision `SUPPORT_MESSAGES_FENCE_CONTROL_FILE`
on every replica before the cutover. Atomically replace its JSON with
`{"paused": true, "generation": "unique-0619-cutover-id"}`. Do not edit in place.
Never reuse a generation or toggle pause off during a drain or migration. An
unreadable or malformed enabled control fails closed. Without a file, enabled
mode uses `SUPPORT_MESSAGES_FENCE_PAUSED` (defaults true) and requires
`SUPPORT_MESSAGES_FENCE_GENERATION`; changing deployment environment may restart
processes, so it cannot establish completion of sends in the replaced process.

Listener health includes support_sender_fence. A receipt can also be obtained
by calling support_sender_fence.receipt with the live support Bus inside the
actual running sender process. A separate shell process has a different process
UUID and cannot prove another process drained. The receipt includes deployment
SHA from RAILWAY_GIT_COMMIT_SHA (or explicitly provisioned
SUPPORT_MESSAGES_DEPLOYED_SHA), process UUID, PID, hostname, optional
RAILWAY_REPLICA_ID, exact generation, active operations, and database blockers.
Replica ID is null when the platform does not provide it; hostname and UUID are
process identifiers, not an invented fleet identity. Portal-only Echo and Scout
ticket passes emit the same process-local receipt to their own log when paused,
after admission exits. That log is an in-process receipt, not listener health;
the operator must still identify every actual sender process and reconcile the
durable outbox before accepting a fleet drain. A sender without a receipt hook
must be accounted for through the operational shutdown boundary below.

`local_drained` requires paused valid control, no admitted operations, a known
SHA, and successful complete bounded scans with no posting rows or held delivery
intent/uncertainty. A scan reaching 1000 rows blocks acknowledgment rather than
asserting complete coverage. Unknown/malformed rows or read errors block drain.
No row is changed by receipt collection. Ordinary queued or approval-held rows
are preserved. Any ambiguous posting/uncertain row requires independently
verified Slack/database reconciliation; do not clear it merely to obtain drain.
`fleet_drained` always stays false.

Before 0619, the operator must inventory every actual sender process and replica,
including overlap during deploy, and obtain matching generation and deployed SHA
receipts for each after all local operations finish. Keep pause armed throughout
the source-CAS migration. Existing old-code replicas cannot acknowledge this
fence. A local receipt cannot prove absent old replicas, hidden services, external
writers, or previously terminated in-flight sends. Where actual runtime inventory
or in-process receipts are unavailable, stop all support sender owners, verify
process exit and reconcile all ambiguous durable claims before cutover. Process
exit alone does not prove a provider request was never accepted. This code adds
no migration, remote writer fence, fleet coordinator, flag activation or live
proof; those remain operational prerequisites.

## Producer admission coverage (2026-10-08 local follow-up)

Pausing also refuses new support outbox producers before their poll or write.
The client DM lane's per-identity pass and direct delivery helper share admission.
The Slack adapter preserves ticket creation and inbound event capture, then refuses
its outbound dispatch phase when paused. Its standalone hold-answer, client notice,
follow-up routing and hold-card helpers are admitted separately. A new paused event
returns `support_sender_paused` with its captured ticket and no outbound rows;
inbound event deduplication remains effective.

**PARTIAL / release blocker:** `DRAFT_support_slack_replay_20261009.sql` now
implements durable, request-bound capture and replay of paused inbound events.
The source-aware replay safety check uses actual inbound/context evidence rather
than nonexistent ticket STOP/DND/takeover columns, and the claim and commit
paths recheck that evidence. This remains draft local code: the SQL is unapplied
and pause/resume continuity has not been proved against the live worker fleet.
Before enabling the fence, stop or deny every old sender owner, reconcile
ambiguous durable claims, apply and verify the scoped replay SQL, and collect
same-generation process receipts. Producer guards and a local replay test do
not establish production intake continuity by themselves.

The production `record_outbound` call-site census is:

| Producer | Admission owner |
| --- | --- |
| `client_dm_support/lane.py` client reply and staff card | `client_dm_producer`, nested `client_dm_delivery_producer` |
| `slack_convo/adapter.py` emit, client hold notice, follow-up card, hold card | Outbound phase `slack_adapter_producer`; standalone helper guards |
| `slack_convo/listener_wiring.py` outreach-release refusal card | Entire `outreach_release_handler`, including refusal |
| `slack_convo/outreach.py` direct row and held approval request | Existing `direct_outreach`; new `outreach_approval_producer` (no production request-approval caller found) |
| `slack_convo/outbox.py` recovery/suppression/uncertainty cards, shared delivery receipt and resolve notices | Existing `outbox` / `operator_resolve` admissions; shared receipt called from admitted outbox or portal passes |
| `slack_convo/bus.py` suppressed-current-notice alert | Called inside admitted outbox or direct/portal outreach operations |
| `echo_ticket_worker.py` intake cards, client-details rows and completion records | Existing `portal_intake` / `portal_fixed` admissions |
| `jobs/stale_escalation_reminder.py` ready reminder | `stale_reminder_producer`, before poll and kv dedup reads/writes |
| `fixer_ops.py::_ticket_note` | Explicit limit: unchanged audit-only INSERT, `record_only=true`, `kind=escalation`, `delivery_status=None`; never a ready/held/posting sender row or posted answer/status CAS proof |

Already admitted operations retain nested admission after pause and finish their
exact owned ticket/identity writes; their active count prevents drain acknowledgment
until they exit. Default-OFF behavior, tenant ownership checks, source classification
and outbound metadata remain unchanged. This fence controls outbox producers, not
all audit or inbound support_messages writes. Other service owners and old replicas
still require the operational inventory and cutover receipts above.

Local validation: 955 focused tests passed using the existing Echo environment.
Added checks cover paused no-poll/no-write producer refusal, preserved inbound
source/tenant/identity and event deduplication for Echo/Scout/Ranger/Wrangler,
already-admitted adapter and client DM writes after pause, and standalone approval,
release, hold and reminder refusal. No production mutation or send occurred.
