# Scene original-use receipt (DRAFT / UNAPPLIED / OFF)

Date: 2026-10-04. Branch: `codex/echo-scene-original-use-receipt-20261004`
(PR268-based, isolated). Status: **draft package only — production held,
activation NOT ready, NOT integrated, nothing committed for apply.**

## Why

Independent review found that `visual_group_usage_ledger` carries **no
fingerprint**. Its primary key is `(gym_id, group_key)`, so after a media swap
the *same* ledger entry is reused by the replacement media while calendar row
id, group and date stay identical. Neither a matching row id, a surviving
published row, same group/date occupancy, nor an attested replacement member
can prove WHICH bytes the entry originally used. The ledger coverage gate
(`DRAFT_visual_scene_ledger_coverage_gate_20261004.sql`) therefore correctly
leaves every fingerprint-free obligation `unresolved /
historical_fingerprint_unproven`, and activation stays refused.

This package adds the missing evidence class: an **owner-only, immutable,
append-only original-use receipt** bound to the original local ledger creation
event.

## What was built (exactly three files, nothing else touched)

1. `migrations/DRAFT_visual_scene_original_use_receipt_20261004.sql` — additive
   draft applied strictly after the ledger gate:
   - `public.visual_scene_original_use_receipt`: canonical tenant/group/date,
     the **exact raw ledger primary key** (`ledger_gym_id`, FK into
     `visual_group_usage_ledger(gym_id, group_key)`), `calendar_row_id` (FK),
     the row's **real claim token** (`claim_attempt_id`, verified in-band to
     equal the calendar row's `publish_claim_token`), exact `source_url`/MD5
     and `delivered_url`/MD5 (`md5:`-prefixed, stack format), delivered pHash
     candidate, capture txid. Byte evidence is bound by **immutable foreign
     keys** into the owner-created evidence records this stack already
     defines: `source_read_receipt` / `delivered_read_receipt` →
     `visual_global_object_read_receipt`, `render_receipt` →
     `visual_global_render_receipt` (mandatory exactly when delivered bytes
     differ from source bytes, enforced by a table check). CAS unique keys:
     identity `(ledger_gym_id, group_key, used_date, calendar_row_id)` and
     `claim_attempt_id`. Immutability trigger blocks update/delete/truncate.
     Table and capture function are owner-only; `select` granted to
     `service_role` only.
   - `public.visual_scene_original_use_capture(jsonb)`: **claim-mode only**.
     Proves in-band that the fleet scene advisory lock is held by this
     backend; selects the ledger row by its **exact raw primary key** (never
     a canonical tenant/group pick); proves the row was created by THIS
     transaction (epoch-safe: `age(xmin) = 0`; see P1 repair below) with
     matching identity; verifies
     `claim_attempt_id` equals the calendar row's real `publish_claim_token`;
     verifies the calendar row's real `post_date`/canonical tenant equal the
     claimed date/tenant; requires the claim path's OWN
     `visual_scene_phash_occupied` write for this calendar row — exactly the
     claimed delivered url/MD5/pHash, under the EXACT claimed ledger group,
     which must equal the calendar row's `visual_group_key` (the group the
     claim guard writes occupancy under) — to have been written by THIS
     transaction (causal byte binding); verifies each read receipt exists
     for this tenant matching the claimed url/fingerprint and that a render
     receipt chains exactly the two read receipts. Replay raises `23505`.
   - Additive, same-signature replacement of
     `visual_scene_ledger_obligation_evaluate`: a fingerprint-free obligation
     resolves **only** when a receipt exists whose `ledger_gym_id` equals the
     obligation's **`raw_gym_key`**, whose stored tenant AND the raw key's
     CURRENT canonical mapping equal the obligation tenant, AND the receipt's
     delivered bytes tie to ACTUAL recorded scene occupancy BY THE BOUND
     CLAIM (`visual_scene_phash_occupied` row whose `calendar_row_id` equals
     the receipt's, whose `group_key` equals the receipt's claimed ledger
     group EXACTLY — never a scene-linked sibling — with matching pHash AND
     exact delivered fingerprint, same tenant/date). Otherwise unresolved
     (`original_use_receipt_occupancy_unproven`, or
     `historical_fingerprint_unproven` with no receipt). All gate reasons and
     fingerprint-carrying paths are preserved verbatim. Old entries are never
     auto-cleared.
   - Fail-closed late-install refusal if any tenant is already armed.

2. `tests/test_scene_original_use_receipt_pg.py` — static source contracts
   (always run) + disposable-PostgreSQL scenarios behind a guarded scratch
   DSN (`SCENE_RECEIPT_TEST_DSN`, Unix-socket DB literally named
   `echo_scene_receipt_test`, same house rule as the ledger-gate tests):
   rollback-only install proof, armed late-install refusal, no false
   historical clearance, simulated same-transaction claim capture resolving
   via receipt (including the rendered source≠delivered path with a real
   chaining render receipt), replay/CAS refusal, media swap staying
   unresolved, cross-tenant isolation, **raw-gym-key binding** (same
   canonical tenant, two raw keys: only the bound key resolves), refusal of
   wrong raw key / invented claim token / unbound byte evidence / missing
   render receipt, **`historical_verified` mode refused outright**, advisory
   -lock and same-transaction (concurrency/lock) failures, immutability, and
   the 2026-10-04 adversarial set below. A static test asserts the claim-wave
   draft does **not** call capture (no wiring exists). NOTE: reusing the same
   exact delivered URL for a second tenant/group is **blocked** —
   `visual_global_object_attestation` has `exact_url` as its **primary key**,
   so the second attestation (and hence any second capture of those bytes)
   can never exist (`test_pg_repeat_delivered_object_blocked`). An earlier
   revision of this document claimed repeat delivered objects are not
   blocked; that was wrong about the global schema contract.

3. This document.

## Repairs from independent review (this revision)

- **P0 — arbitrary ledger row / canonical-only identity**: capture previously
  selected a ledger row by canonical tenant/group (`order by gym_id limit 1`)
  and the evaluator ignored the obligation's raw gym key. Now capture takes
  `ledger_gym_id` and selects by the exact PK `(gym_id, group_key)`, the
  receipt FK-binds that raw key, identity uniqueness includes it, and the
  evaluator requires `receipt.ledger_gym_id = obligation.raw_gym_key`.
- **P0 — free-form attestation fabricated proof**: `byte_attestation` jsonb
  and free-form `provider_attempt_id` text are **removed**. Byte reads and
  render lineage are bound by immutable FK to owner-created
  read/render receipt records and verified in-band; claim identity binds the
  calendar row's real `publish_claim_token`. `historical_verified` mode is
  **removed entirely**: historical sends predate those owner-created records,
  so any historical receipt could only be built from unattested input.
  Old fingerprint-free entries stay unresolved forever under this package.
- **P0 — no claim-path wiring**: unchanged and explicitly not claimed. See
  gaps below; this package is **not integrated**.
- **P1 — wrong uniqueness**: `unique (delivered_url, delivered_md5)` removed.
  That table-level key would have been redundant in the legal case and wrong
  in the illegal one: a GLOBAL repeat of the same exact delivered URL is
  already blocked upstream by the `visual_global_object_attestation`
  `exact_url` PRIMARY KEY (PG-tested:
  `test_pg_repeat_delivered_object_blocked`), while legitimate same-tenant
  reuse of its own delivered object never needed a CAS key here. CAS is
  identity + real claim token only.

## Repairs from independent Terra review (2026-10-04, this revision)

- **P0 — bytes not causally bound to the claim**: capture previously proved
  the receipt was minted in the same transaction as the raw ledger insert,
  but the claimed source/delivered bytes were only FK-bound to owner-created
  read/render receipts the CALLER selected — those records and "another
  occupancy" could be unrelated to the bytes the claim actually used.
  Capture now additionally requires, in-band: (1) the referenced calendar
  row's real `post_date` and canonical tenant mapping equal the claimed
  date/tenant; (2) the claim path's OWN `visual_scene_phash_occupied` write
  for THIS `calendar_row_id` — carrying EXACTLY the claimed delivered
  url/MD5/pHash — was written by THIS transaction
  (epoch-safe: `age(o.xmin) = 0`), under the exact claimed ledger group
  equal to the row's `visual_group_key`. Occupancy is written only inside
  the claim transaction from its own scene scan, so this binds the
  receipt's bytes to
  the bytes THAT claim used. If the claim's occupancy insert is an
  idempotent no-op (same scene+bytes+date already recorded by an earlier
  claim), capture REFUSES — fail closed.
- **P0 — tenant/raw-ledger false clearance audited and hardened**: the
  evaluator's receipt lookup now also requires the raw key's CURRENT
  canonical mapping (`visual_group_tenant_id(ledger_gym_id)`) to equal the
  obligation tenant, so a receipt minted under an old mapping can never
  clear another tenant's obligation after an alias remap; and the occupancy
  tie now requires `occupancy.calendar_row_id = receipt.calendar_row_id`,
  so an unrelated occupancy row over the same bytes (another claim) is
  never proof. Adversarial PG tests: bytes-the-claim-did-not-use refusal,
  unrelated-occupancy same-bytes non-clearance, alias-remap non-clearance,
  cross-tenant isolation, two-raw-key binding, plus the true case
  (simulated same-transaction claim capture resolving).
- **P1 — docs misstated repeat-delivered-object behavior**: corrected above
  and in the migration header; the global `exact_url` PK blocks cross-
  tenant/group repeats and the revised test asserts exactly that.
- **P1 — sibling-group occupancy accepted (Terra review, 2026-10-04)**:
  the claim guard writes `visual_scene_phash_occupied` with
  `group_key = p_row.visual_group_key`, but capture accepted occupancy for
  ANY group and the evaluator accepted any scene-LINKED member's occupancy —
  a wrong-group/same-row occupancy could mint or satisfy a receipt. Capture
  now requires `occupancy.group_key =` the exact claimed ledger group AND
  `= content_calendar.visual_group_key`; the evaluator's receipt path
  requires `occupancy.group_key = receipt.group_key` exactly. Consequence
  (fail closed, integration-relevant): a claim whose ledger group differs
  from the row's resolved `visual_group_key` can never mint a receipt —
  the claim-path wiring must pass the ledger group the occupancy write
  actually used. Adversarial PG test:
  `test_pg_wrong_group_sibling_occupancy_never_proves` (capture refusal +
  evaluator non-clearance with a scene-linked sibling).
- **P1 — xid comparison was not epoch-safe (Terra review, 2026-10-04)**:
  `xmin::text::bigint = txid_current()` compared a 32-bit xid with the
  64-bit epoch-qualified id, so it only held in epoch 0 and would silently
  break after the first xid wrap. Both same-transaction proofs now use
  `age(xmin)`: PostgreSQL's epoch-aware comparison, 0 exactly when the
  tuple's inserting xid IS this transaction; every real earlier claim's row
  has `age >= 1`, preserving the idempotent-no-op refusal. Assumption
  (standard PostgreSQL xmin discipline): no compared tuple is older than
  2^31 transactions. For the ledger check the `(gym_id, group_key)` primary
  key makes a second row for the same key impossible, so the wrap-alias
  window is unreachable there; for occupancy a false positive additionally
  requires identical row/bytes/url/date plus a 2^32-aligned old xid.

## Lock order (preserved, unchanged from the ledger gate)

1. `content_calendar` SHARE ROW EXCLUSIVE write barrier;
2. calendar rows FOR UPDATE; component targets from calendar AND all ledgers,
   locked sorted NOWAIT;
3. NOWAIT proof/ledger relation barriers;
4. the one fleet-wide `visual_scene_global` advisory lock, LAST;
5. evaluation under the full lock set only (live reread, drift fail-closed).

Receipt writes require the same fleet advisory lock (step 4) that the
backfill/activation evaluator holds while evaluating, so receipt writers and
the coverage evaluator serialize on the advisory lock by construction; the
receipt relation is append-only/immutable, so there is no update/delete drift
channel to re-read. **Note (P1 honesty):** the advisory lock serializes
writers against the evaluator, but the advisory lock alone is not the full
activation barrier — activation remains refused by the ledger coverage gate
until integration below is done and separately authorized.

## Gaps / required integration (fail closed — activation NOT ready)

1. **Claim-path wiring is not done. This package is NOT integrated.** This
   draft deliberately does NOT modify the frozen claim-wave claim guard or
   the atomic write/patch drafts, so NEW claims do not emit receipts.
   Wiring `visual_scene_original_use_capture(...)` into the clean-claim
   success path (same transaction, strictly AFTER the claim's own
   `visual_scene_phash_occupied` write — capture now proves that write
   happened in-transaction — advisory lock held, passing the row's real
   `publish_claim_token`, the exact raw ledger key and the owner-created
   read/render receipt ids) is required, separately reviewed integration.
   Until then every fingerprint-free obligation stays unresolved.
2. **Ledger group must equal the row's `visual_group_key` at claim time.**
   The P1 group binding above means the claim-path integration must capture
   receipts only for claims whose ledger entry group equals the resolved
   `visual_group_key` the occupancy write used; anything else fails closed
   (no receipt, obligation stays unresolved). **XID assumption:** the
   `age(xmin)` proofs inherit the standard PostgreSQL xmin assumption (no
   compared tuple older than 2^31 transactions); see the P1 note above.
3. **No provider-attempt evidence record exists.** The removed free-form
   `provider_attempt_id` has no owner-created table to bind to in this
   stack. If provider proof is required for activation, a real provider
   attempt evidence record (and FK binding) is additional integration work;
   this package deliberately does not pretend it.
4. **NOWAIT receipt relation barrier not folded into the gate's backfill
   lock list.** Safe today by the advisory-lock serialization above; adding
   `visual_scene_original_use_receipt` to the gate's relation barrier list is
   part of the same integration in (1).
5. **No historical clearance path exists.** `historical_verified` was
   removed; old fingerprint-free entries stay unresolved. If a verifiable
   historical evidence class is ever defined, it requires a separately
   reviewed migration.
6. **Tests simulate, not exercise, the claim path.** PG scenarios insert the
   ledger row, the claim path's own occupancy write and the capture call in
   one transaction under the advisory lock with real evidence rows; no
   claim-wave code path is invoked (asserted by
   `test_static_claim_wave_not_wired`).
7. **PG scenarios require an isolated scratch database.** On 2026-10-04 the
   primary host ran all 29 tests against disposable PostgreSQL 17 with
   `SCENE_RECEIPT_TEST_DSN` pointing at the guarded Unix-socket database
   `echo_scene_receipt_test` (29 passed). The independent Terra reviewer ran
   the eight static contracts (8 passed) but could not replay PostgreSQL in
   its read-only sandbox. The tests simulate the capture transaction; they do
   not establish claim-path integration or production activation readiness.
8. **Production apply and activation remain held** pending separate
   authorization; the migration is DRAFT and rollback-only verified.
