# Scene Ledger Coverage Gate (DRAFT / OFF)

File: `migrations/DRAFT_visual_scene_ledger_coverage_gate_20261004.sql`
Status: **DRAFT / UNAPPLIED / OFF** — additive draft only. No production
application or activation approval is granted. Apply strictly after
`DRAFT_visual_scene_history_backfill_20261004.sql`.

## Problem

The 2026-10-04 scene-history backfill iterated only **surviving published
`content_calendar` rows**. Permanent exact-byte ledger uses whose calendar rows
were later deleted, denied or swapped were never audited, so activation could
arm a tenant (and the fleet) while historical obligations were unaccounted
for.

## What the gate does

The draft additively replaces `visual_scene_history_backfill_locked(text)` and
`visual_scene_history_activation_receipt()` (same signatures, same lock order)
so coverage becomes **calendar rows + every ledger obligation**, enumerated
independently with `UNION ALL` (no join may erase an obligation):

| Obligation source | States covered |
| --- | --- |
| `visual_group_usage_ledger` | reserved / published / released |
| `visual_global_usage_member` | all states |
| `visual_global_usage` (owner) | all states, including orphan owners with no member |
| `visual_global_release_history` | every row |

Rules:

- **NULL dates are unresolved** (`missing_date`). Dates are never inferred
  from timestamps, siblings or receipts.
- Unmapped tenants stay visible to a fleet run (`unmapped_tenant`).
- **Proof standard** for an obligation whose calendar row is gone or never
  existed: retained historical identity tied to *actual scene occupancy* — a
  `visual_scene_phash_occupied` row (written only inside a clean claim
  transaction) for the same tenant and date, in the obligation's linked
  scene, with a fingerprint exactly equal to the obligation's fingerprint.
  - Same group/date alone is not proof.
  - A staged candidate is never proof.
  - The current replacement media is never proof of swapped-out old media
    (`fingerprint_mismatch`).
  - Per-gym ledger rows (`visual_group_usage_ledger`) carry **no
    fingerprint**, and nothing in this schema proves **which bytes** such an
    entry used: the SAME `calendar_row_id` can hold the old media's ledger
    use AND the replacement media's occupancy row, and a surviving published
    row can carry the replacement bytes. A matching `calendar_row_id`,
    group/date, surviving current row, or attested replacement member is
    **never** original-byte evidence (neither the surviving-row path nor the
    occupied-row path). Until a truly immutable, row-specific, byte-bound
    original-use receipt exists, every fingerprint-free obligation stays
    **unresolved** with reason `historical_fingerprint_unproven` — an
    operator must review and reconcile it manually. pHashes and dates are
    never invented to clear it, and local usage is never silently cleared.
- Unprovable records are marked **unresolved**; pHashes and dates are never
  invented, and ledger obligations never write `visual_scene_phash_occupied`.
- `already_recorded` requires **exact fingerprint equality** with the live
  row's bytes: a same pHash/tenant/group/date occupancy over different bytes
  is `occupancy_fingerprint_mismatch` (unresolved, no write).
- When the obligation references a surviving published calendar row, the exact
  row proof (`visual_scene_history_evaluate`) is reused and must agree with
  the obligation on tenant/date/linked group/fingerprint
  (`calendar_row_proof`, else `calendar_row_identity_mismatch`).

## Activation gate

`visual_scene_history_activation_receipt()` runs inside the activation RPC's
calendar write barrier and **always recomputes authoritative fleet coverage**
via `visual_scene_history_backfill_locked(null)`. An incoming
`proof->scene_history` receipt is never trusted — it is overwritten by the
recomputed one, so a stale or pre-ledger receipt cannot authorize activation.
Any unresolved calendar row or ledger obligation refuses activation
**fleet-wide** (SQLSTATE 23514) and the whole arming transaction rolls back:
no activation row, no armed setting, no occupied writes survive.

The current base activation importer creates a local usage-ledger row without
an immutable byte fingerprint. Even when a pre-import fleet receipt is clean,
that new row is unresolved and activation stays held. A separate reviewed
byte-bound provenance design is required before this gate can be armed; do not
weaken the unresolved rule to make activation pass.

## Lock order (preserved)

1. `content_calendar` SHARE ROW EXCLUSIVE write barrier.
2. Calendar rows FOR UPDATE + proof-only plan; sorted component targets
   collected from the calendar **and all ledgers**, locked NOWAIT.
3. NOWAIT proof/ledger relation barriers (the four ledger relations included).
4. The one fleet-wide `visual_scene_global` advisory lock, taken LAST.
5. Under the full lock set: ledger obligations are re-read; drift is detected
   (never waited on after the global lock) and is fail-closed
   (`ledger_drift_under_lock`, all writes skipped); every row and obligation
   is then re-evaluated.

## Access

- Mutation helpers (`visual_scene_ledger_obligations`,
  `visual_scene_ledger_obligation_evaluate`,
  `visual_scene_history_backfill_locked`, `visual_scene_backfill_occupied`,
  `visual_scene_history_activation_receipt`): owner-only (EXECUTE revoked from
  public/anon/authenticated/service_role).
- Diagnostics (`visual_scene_ledger_coverage_audit`,
  `visual_scene_history_audit`): read-only, service_role only.

A late-install guard refuses to apply this draft over **any** already-armed
tenant (`gym_visual_guard_settings.enforce = true`), regardless of any stored
receipt: a JSON coverage label on an activation row could have been forged or
copied under the prior hook and is never consulted. Install only over a fully
unarmed fleet.

## Tests

`tests/test_scene_ledger_coverage_gate_pg.py` — static source contracts always
run. Real-PostgreSQL scenarios run only when `SCENE_LEDGER_GATE_TEST_DSN`
names a disposable Unix-socket database literally named
`echo_scene_ledger_gate_test` and local psql is on PATH (this host's
PostgreSQL cannot start: shmget ENOSPC/EPERM — PG scenarios skip here).
Scenarios: deleted published row, denied/released member, swapped old media,
ledger-only reservation, orphan member/owner, release-only history, NULL
date, other-tenant fleet blocker, staged-candidate-is-never-proof, wrong
fingerprint, the SAME-`calendar_row_id` regression (old local ledger use +
new replacement occupancy + surviving published row with new bytes — the
fingerprint-free obligation stays unresolved
`historical_fingerprint_unproven`), `already_recorded` exact-fingerprint
enforcement with activation refusal, verified pre-import history followed by
fail-closed activation when the base importer adds a fingerprint-free local
use,
incoming-receipt bypass, rollback/no-writes on failure, and the read-only
audit surface. Before any committed scenario setup, two install probes run on
the scratch DB: (a) a rollback-only installation probe (the draft's outer
`begin;`/`commit;` stripped, body executed in one transaction then
`ROLLBACK`, catalog verified byte-identical) and (b) a forged-receipt
late-install refusal (armed tenant with a fake ledger-aware receipt →
install fails and nothing is applied).
