# Forward schedule reservation draft, October 8, 2026

**DRAFT / UNAPPLIED / DEFAULT OFF.** This work authorizes no production SQL,
provider calls, gate activation or credential provisioning. It extends the
base media claim and visual index drafts and requires the committed logical
post identity column. All three draft gates retain their existing defaults.

The SQL migration is authoritative. The coordinator must finish staging and
trusted attestation before one atomic finalization RPC. Final publication is
protected in SQL as well as checked by the Python caller.

## Trusted preparation release hold

The atomic finalizer is implemented for candidates with **genuine persisted
trusted proof**. Production observation writes are unverified and do not
create registry, clearance, render manifests, lineage or visual attestations.
The planner must preserve old rows and report `preparation_pending` when that
proof is absent. It cannot borrow owner/attester credentials to finish inline.

The current trusted preparation pipeline still rejects the marked inactive
stage in `fixer_record_forward_media_observation_20261007`,
`fixer_forward_media_owner_pending_20261007`,
`fixer_owner_photo_pending_20261007`, `fixer_prepare_owner_photo_20261007`,
`fixer_forward_media_pending_attestations_20261006`, and the Python owner,
owner-photo and manifest-binder prefilters/discovery. This SQL repair does not
broaden those lanes. A separately reviewed two-phase runtime contract must
prepare the same explicit staged UUIDs, then resume finalization with exact
current proof and old-row snapshots. Its flags and credentials remain OFF.

The attester discovery also excludes a row once current lineage exists. A
visual-attestation failure after lineage commit therefore needs a separate
current-lineage/missing-visual-role retry discovery, reusing immutable lineage
and any existing role attestations. Creating a second lineage or blindly
respawning a quarantined owner job is not a recovery contract. This remains a
release blocker. Local PG fixtures provision genuine trusted proof directly
under the isolated test roles; that establishes SQL behavior, not a completed
production preparation service or resume workflow.

## Immutable claim identity

`forward_visual_claim_identity_20261008` records a new visual claim's logical
post ID in the transaction that inserts its visual proof. It has no calendar
foreign key. A deleted, edited or reinserted calendar row cannot change this
claim evidence. Existing visual proofs receive `historical_unknown` and NULL
logical identity during migration. No historical authorization is inferred
from a current calendar row. Unknown identity holds both reservation and final
publication when the reservation authority is ON.

Matching visual bytes or pHash similarity are reusable only for the exact same
canonical tenant, post date and frozen logical post identity. Existing base
byte ancestry and visual group constraints remain additional requirements;
the extension grants no group, owner, clearance or approval bypass.

## Stage and finalize contract

Production accepts `variant_status` values `active`, `candidate`, `archived`.
It accepts status values `draft`, `pending`, `approved`, `published`, `denied`,
`killed`, `failed`, `publishing`, `deleted`, `coach_review`. The PG17 acceptance
fixture includes these exact CHECKs. This draft introduces no new enum value
and does not weaken a CHECK.

1. Persist generated rows with explicit UUIDs, `variant_status='candidate'`,
   `media_not_ready_reason='forward_reservation_staged'`, and `status='pending'`
   or `draft`. This disjoint marker distinguishes reservation preparation
   from other candidate rows. They remain inactive and unapproved.
2. Obtain the existing trusted lineage and all three visual attestations for
   every row. The existing media revision deliberately excludes mutable
   status, variant status and media reason, so activation does not change
   the attested revision. Staging performs no source reservation.
3. Read exact raw old `content_calendar` row snapshots. Keep every column,
   including NULLs. Do not use a display projection or transformed row.
4. Invoke exactly one RPC for the entire prepared batch:

```sql
public.finalize_forward_schedule_batch_20261008(
 p_tenant_id text,
 p_candidates jsonb,
 p_expected_old_rows jsonb
) returns jsonb
```

```json
{
  "p_tenant_id": "canonical-tenant",
  "p_candidates": [{
    "calendar_row_id": "uuid",
    "logical_post_id": "uuid",
    "expected_revision": "32-character persisted revision",
    "attestation_ids": ["original-uuid", "delivered-uuid", "thumbnail-uuid"],
    "expected_reservation_id": null
  }],
  "p_expected_old_rows": [{"id": "old-uuid", "all_actual_columns": "including null values"}]
}
```

`expected_reservation_id` is optional. Supply the exact incumbent reservation
UUID only for a deliberate source replacement on the same logical/date slot.
An omitted or wrong token holds a conflicting replacement. Old rows and new
candidate IDs must each be unique and disjoint. Tenant aliases resolve through
the existing protected alias authority.

The return pairs remain in **candidate input order**:

```json
{"row_ids": ["calendar-uuid"], "reservation_ids": ["reservation-uuid"]}
```

Sibling row IDs are distinct but may have the same reservation UUID. Preserve
this positional association; do not independently deduplicate the arrays.

The RPC takes shared graph, exclusive census, sorted calendar row locks, then
slot/byte/token locks used by the existing stack. It validates every old row's
full snapshot and tenant before changing anything. Old rows must be active,
pending/draft, without media hold, publish claim token, reservation day,
published timestamp or provider ID. Approved, queued, publishing and published
rows are protected. NULL/unknown status holds. Candidates must be inactive with
the exact stage marker and pending/draft; approved candidates are refused.

Each candidate is activated and its marker cleared inside the transaction,
then passed through the existing reservation's current owner provenance,
clearance, source, lineage, revision, exact receipt and visual checks. **Any
failed candidate aborts the entire RPC**, including prior candidate reservations
and activation. Only after every candidate passes does it archive the exact
old unapproved rows and release their obsolete reservations. Existing old
approvals are never transferred to new content.

There is no per-row partial success contract and no exception catch that
commits successful candidates. A caller must treat an error as a held batch.
An ambiguous network outcome requires exact database readback, never a second
planner attempt that assumes success or deletes prepared rows. A retry with
old snapshots after successful finalization holds on the now changed old
rows; this is deliberate fail-closed behavior.

Inactive stage rows can remain after a failed or interrupted preparation.
Their existence is not an active reservation or successful calendar result.
Preserve them on ambiguous finalization until exact readback proves the
transaction outcome. After a known failure, the task owner may inspect and
retire only its inactive, unclaimed stage rows through the normal cleanup
workflow. Keep their trusted immutable evidence. This migration performs no
blanket cleanup.

## Reservation and publication RPCs

`reserve_forward_slot_20261008(uuid,uuid,text,uuid[],uuid default null)` remains
available for already existing unsent active rows. It returns one durable
active reservation per `(tenant_id, post_date, logical_post_id)`. Exact source
siblings share it only after each sibling's current proof passes validation.
`forward_schedule_reservation_binding_20261008` stores an immutable per-row
revision, lineage ID and attestation set. Reattestation with a different frozen
set for an already bound revision holds. A source SHA match alone cannot lend
another row's publication proof. Source replacement uses the exact incumbent
reservation UUID and supersedes it atomically. Revocation is terminal for the
same tenant/source asset identity.

`forward_reservation_proof_20261008(uuid,text)` returns the active slot binding
and the current caller row's frozen proof:

```json
{
 "reservation_id": "uuid", "tenant_id": "tenant", "post_date": "YYYY-MM-DD",
 "logical_post_id": "uuid", "source_sha256": "64 lowercase hex",
 "row_revision": "revision", "lineage_evidence_id": "uuid",
 "attestation_ids": ["uuid", "uuid", "uuid"]
}
```

This response is advisory outside a transaction. It holds if the current row
revision has no exact sibling binding. `fixer_forward_visual_proof_20261008`
also returns the original attestation's trusted `source_sha256`, allowing the
Python caller to compare its outgoing proof before claiming.

The existing final publication RPC signature is preserved:

```sql
fixer_forward_visual_index_claim_20261008(
 p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,
 p_expected_revision text,p_attestation_ids uuid[]
) returns boolean
```

With the schedule gate OFF, this delegates to the preceding visual claim
implementation, preserving the preactivation claim behavior. With the gate
ON, SQL locks graph, census and the owned calendar row; independently derives
the source SHA from the trusted original attestation; and requires the exact
active reservation plus the current row's frozen revision, lineage and all
three attestation IDs. It checks immutable committed logical identity for all
outgoing roles, then invokes the existing visual/base claim stack in the same
transaction. Current source clearance, exact object receipts, negatives,
history, claim ownership and approval protections remain enforced there.

The renamed implementation loses all service-role execute grants. The public
base fallback refuses when either visual or schedule authority is armed, so
turning the visual gate OFF cannot bypass reservation enforcement.

`release_forward_slot_20261008(uuid,text)` and
`revoke_source_reservations_20261008(text,text,text)` take the same shared graph
and exclusive census transaction locks before touching reservations. Thus a
revocation that commits first makes a waiting publisher fail with no receipt.
A claim that passes first retains the authority fence through receipt commit,
and a following release waits. A previously read Python proof does not
permit publication after release or revocation.

`check_reservation_conflicts_20261008(text,date,uuid,text,bigint)` remains a
read-only advisory candidate screen. Equal SHA or pHash distance <=6 blocks
outside the exact sibling scope; distance 7 through 30 holds for review.
A bounded search and an empty index prove no historical clearance or depletion.

## Gate and access

The reservation singleton is installed `enabled=false`. Reserve and finalize
hold while OFF. Release/revoke can reduce occupancy while OFF. Gates serialize
with claims through the graph lock. Table writes and private claim execution
are unavailable to service, public, anon, authenticated and attester roles.
Service can read the protected activation bit, not mutate it. Reservations,
per-row bindings and claim identities are immutable durable evidence.

While ON, statement triggers acquire graph then census before calendar row
locks. A service caller cannot directly insert an active logical row, activate
an inactive row, retire or delete an active logical row. Activation/replacement
belongs to the protected finalization RPC. Existing guarded approval and
publication status transitions continue through their original workflow.
Privileged migration ownership remains trusted; this is not a superuser fence.

Malformed input raises `22023`; validation/conflict/history holds `23514`;
OFF holds `55000`; unauthorized mutation `42501`; unsupported isolation
`25000`. Reserve, finalize and final claim require READ COMMITTED. No SQL RPC
performs network or object I/O.

## Local acceptance and release limits

```sh
python3 tests/test_forward_schedule_reservation_pg.py
python3 -m pytest -q tests/test_forward_schedule_reservation_pg.py
```

Uses an existing real disposable PG17 server and two clusters; no DSN,
production connection, dependency installation or persistent cluster. The
suite covers immutable deleted/reinserted claim identity, pre-migration
historical unknown holds, source/sibling/tenant/date binding, visual conflicts,
clearance/negative/revision drift, exact old snapshot CAS, full batch rollback
on the second candidate, competing batch one-winner behavior, ACL/direct
activation denial, approved-old preservation, current sibling proof, and
revocation-before-claim / claim-before-release transaction fences.

These are local acceptance results. Production application, independent
integration acceptance, guarded release and separately authorized activation
remain outside this SQL repair scope. Before use the draft objects can be
removed in dependency order. After use, preserve durable evidence and restore
public wrappers through a reviewed rollback migration; simply dropping a
reservation table is not a valid rollback.
