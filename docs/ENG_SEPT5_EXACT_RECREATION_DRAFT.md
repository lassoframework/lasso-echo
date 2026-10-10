# ENG September 5 denied-post recreation: draft only

This package addresses support ticket `35e066d0-d9bc-40e6-aef8-86719a010590`
for ENG client `6ee04ee4-13a5-47db-8416-7b8ee3e61ab8`. The client reported
that a denied post did not re-create within a day. The ticket currently says
`resolved` with no `resolved_at` and has no verified recreation evidence. This
package does **not** repair the live ticket, send a resolution, or close it.

The exact denied September 4 rows are Facebook feed
`2ad9f097-e7cc-49a2-b348-30c8e1cda80d`, Instagram feed
`b1bb4d63-fda7-4b1e-9303-dff3483f4387`, and Instagram Story
`ff792b3e-be08-4c4e-b0f5-0347e075a9ba`. They remain untouched. A future
target date admits one new IG feed, Facebook mirror and Story with common
logical post ID `6d21c00f-349c-5a06-9328-5a570a81babb`. They become
**pending client approval cards**, not retroactive published posts. No team
approval is introduced. The regular gym client approval gate remains.

## Authority and sequence

`migrations/DRAFT_eng_sept5_exact_recreation.sql` creates a default-OFF,
one-ticket gate and durable receipt. The service-role `begin` RPC pins the
support ticket version, exact inbound transcript hash, full original row
snapshots, tenant and future date. It permits one reservation and refuses a
different date or changed request. `bind` accepts only an inactive, staged
forward batch with three exact tenant/account/format/date candidates and zero
old rows. `finalize` locks and rechecks the ticket, transcript, original rows and member IDs,
then calls the existing trusted forward staged finalizer in the same database
transaction. A returned terminal receipt is persisted; an exact replay reads
it without another activation. Concurrent planner slot occupancy or byte
conflicts must be rejected by the existing forward finalizer.

`agent/eng_sept5_recreation.py` is an explicit maintenance coordinator with no
CLI or scheduled entry point. It uses the ordinary Echo A+ caption/creative,
feed/Story pairing, byte screening, rotation reservation, observation bridge,
and `insert_rows(... expected_old_rows=[])` forward staging path. It refuses
the old original bytes and checks exact ENG row identities. Its `stage`,
`bind`, and `finalize` calls are deliberately separate. An uncertain RPC or
stage response requires exact batch status/receipt reconciliation, never a
blind retry or calendar-row inference.

## Activation blockers and release checks

The existing `DRAFT_fixer_forward_schedule_stage_20261008.sql` explicitly
states that trusted owner/photo/attester discovery still selects **active**
rows and does not consult staged candidate membership. Thus the staged rows
in this package cannot currently complete trusted visual preparation. The
reservation, observation bridge and stage migrations are also DRAFT and must
be reviewed/applied in dependency order. No gate may be enabled and no caller
may run against production until staged discovery is implemented, separately
reviewed, deployed, and proven with real tenant-owned bytes and exact
attestation/readback. The regular finalizer must be tested end to end against
the real migration, including a competing planner slot and a changed ticket.

The PG17 tests in `tests/test_eng_sept5_recreation_pg.py` exercise the
one-ticket SQL against a disposable database on the existing local server.
They use a deliberately narrow forward-finalizer fixture, so they establish
the wrapper's admission, replay and rollback properties, **not** visual
attestation or the production forward stack. Before any activation, an
independent agent must verify the actual new cards in the production portal,
the exact source bytes and visual proofs, the preserved client approval
state, and the reported ticket scope. Only then can the guarded support
notice and closeout workflow be used. Future approval or publication is a
separate outcome and must not be claimed as delivered.
