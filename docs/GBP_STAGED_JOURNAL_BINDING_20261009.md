# GBP staged-journal binding (2026-10-09, OFF by default)

Bounded package on frozen commit `41967091` in isolated worktree
`echo-gbp-staged-journal-20261009`. No production access, no migrations, no
provider calls, no flag enablement, no SQL change (no missing read projection
found — the existing batch-status RPC is the sole readback authority and is
sufficient).

## Problem

The GBP Drive-use journal treated any exact active calendar row as landed. With
forward reservation armed, the store stages INACTIVE candidate rows and an
isolated finalizer activates them later; `last_forward_stage_attempt` was
process memory only. A candidate is not a successful placement and must never
consume remote Drive use.

## Change (all flag-gated by `AGENT_GBP_STAGED_JOURNAL`, default OFF)

- `agent/gbp_drive_use_journal.py`: new durable local table
  `gbp_forward_stage_journal` with states `stage_intent -> staged_pending ->
  finalized`. `record_forward_stage_intent` freezes the exact canonical stage
  attempt (batch id, tenant, request digest, exact request text, member and
  old-row id sets) BEFORE the stage RPC; exact replay resumes, any identity
  difference under the same batch id holds (`stage_identity_conflict`).
  `record_forward_stage_receipt` binds only an exact `staged` receipt.
  `record_forward_finalized` binds only an exact terminal finalize receipt
  (batch/tenant/digest, exact member set as row ids, one reservation id per
  row, exact archived old-row set); it also settles `stage_intent` directly
  for the lost-stage-plus-lost-finalize recovery. `pending_forward_stages`
  is the restart recovery list; `forward_remote_use_allowed` is True only for
  `finalized`; `forward_stage_for_member` binds a calendar row id to its
  durable batch without trusting the row's marker text.
- `agent/portal_calendar_store.py`: `gbp_staged_journal_flag()` (tri-state;
  ambiguous fails closed before any write). When armed,
  `stage_forward_schedule_batch` records the durable intent before the RPC and
  binds the validated staged receipt after it (a receipt-binding hold after a
  committed stage raises UNKNOWN; nothing is resent). `resolve_forward_stage_attempt`
  falls back to the durable pending attempt (exactly one, else ambiguous hold)
  and rebinds the status readback into the journal. New
  `bind_forward_finalization(batch_id)` reads the batch-status RPC bound to
  the DURABLE identity and records finalization only on exact terminal proof.
  New `forward_remote_use_settlement_allowed(batch_id)` answers True only on a
  durable finalized binding. Flag OFF: byte-identical legacy behavior.
- `agent/gbp_planner.py`: armed-only guard in `_settle_armed_drive_landing` —
  a staged candidate (`variant_status != active` or the
  `forward_reservation_staged` marker) is held, and an active member readback
  of a non-finalized bound batch is held. The legacy OFF path is unchanged;
  the existing exact `confirm_landed` comparison is untouched. Candidate
  status is still not an allowed omitted default; the use counter is never
  landing evidence.

## Acceptance mapping

- Freeze canonical stage request before POST: store already canonicalizes;
  the intent is now durable before the RPC (test asserts durability during
  the fake RPC call).
- Persist batch identity + member/tenant/request/old-row binding: durable
  table, exact-match replay/conflict rules.
- staged_pending vs finalized: separate states; `staged_pending` never
  authorizes remote use.
- Hold remote consumption without a unique terminal binding and exact active
  member readback: planner guard plus journal lookup. The helper
  `forward_remote_use_settlement_allowed` has no production caller yet;
  reservation/lineage authority is not fully composed in this slice.
- Lost acknowledgments recover via bound batch-status RPC: restart test uses
  a fresh store with empty memory resolving purely from the durable journal.

## Tests

`tests/test_gbp_staged_journal_binding.py` — 81 passed, including independent
review regressions for missing/conflicting binding, restart recovery, ambiguous
flag, frozen-row mismatch, terminal-receipt corruption, and valid same-logical
siblings sharing one reservation. Final bounded adjacent selection: **265
passed**. Lead reran the 81 new tests and three real PostgreSQL
caption-composition tests outside the Kimi sandbox: **84 passed**. A fresh
independent Codex review found no remaining P0–P2 within this local slice.
`git diff --check` was clean. The Kimi sandbox
denied `initdb` shared memory (`shmget`); this is a limitation of that runner,
not evidence about PostgreSQL behavior. A broader non-PG suite was interrupted
after unrelated failures and environment collection errors; it is not a green
full-suite result.

## Remaining blockers / limits

- The armed full-stack path (real stage RPC -> isolated finalizer -> active
  member readback -> settlement) is NOT proven; these are local binding and
  guard tests only. No production enablement is justified.
- The production caller does not yet invoke `bind_forward_finalization` or
  `forward_remote_use_settlement_allowed`; automatic finalized progression
  remains unverified.
- `forward_stage_for_member` scans the local journal (bounded: pending batches
  are few, members ≤100); acceptable for this slice.
- Settlement does not yet re-verify reservation/lineage beyond the bound
  terminal receipt; a deeper wired caller remains follow-up.
