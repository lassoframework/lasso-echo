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
process identifiers, not an invented fleet identity. Portal-only processes need
an in-process receipt hook or must be accounted for through the operational
shutdown boundary below; they do not emit listener health.

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
