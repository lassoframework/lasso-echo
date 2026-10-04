# Scene claim wave — DRAFT / UNAPPLIED / OFF / INCOMPLETE (2026-10-03; P0 repair pass 2026-10-04; wave-2 repair pass 2026-10-04 per `SCENE_REPAIR_WAVE2_SCOPE.md`; wave-3 architecture repair 2026-10-04 per `SCENE_WAVE3_SOL_DESIGN.md`)

STATUS: DRAFT / UNAPPLIED / OFF / INCOMPLETE. The current source package was
independently audited against frozen SQL hashes (group
`199b33279a442cf78816457e588f886d2478b9b2063f0f0c8b599617a1f7d791`, scene
`8fae2bdc788ed874e34a13d9f46ae77262bd93ef928cc2f5a4d8d4de9a5ee570`), and a
fresh independent PostgreSQL 17 run reports 145 passed, exit 0. This is
draft-level source and scratch-database evidence only. Nothing in this wave is
applied anywhere, no guard is armed, no flag is flipped, and no provider is
contacted.
`SCENE_GUARD_OPERATIONAL` stays `False`; an armed `AGENT_VISUAL_SCENE_GUARD`
still means fail-closed, not enforcement. The frozen exact-byte md5 ledger
(`migrations/DRAFT_visual_global_history_20261002.sql`) is unchanged and
remains the only drafted enforcement layer. The calendar trigger installed by
the wave file is a DRAFT on the same OFF branch.

This file is the operational record of the claim-wave redesign (items (a)-(e)
in `docs/VISUAL_SCENE_GUARD_DRAFT.md`) and the 2026-10-04 P0 repair pass
against Sol's audit (`SCENE_AUDIT_GAPS.md`, 2026-10-04 — verdict: safe only
as an inert DRAFT/OFF branch; production activation rejected), written
alongside `migrations/DRAFT_visual_scene_claim_wave_20261003.sql` and the
`tests/test_scene_claim_wave_*.py` contract tests. The acceptance boundary
the repair pass implements is `SCENE_ACTIVATION_REPAIR_SPEC.md`.

## Exact migration order

1. Base schema + claim trigger drafts (existing `DRAFT_visual_group_*` files).
2. `migrations/DRAFT_visual_global_history_20261002.sql` (exact-byte ledger;
   frozen contract, md5-keyed — do not edit).
3. `migrations/DRAFT_visual_scene_claim_wave_20261003.sql` (this wave:
   candidate staging bound to the row's delivered object, permanent occupied
   scenes, idempotent review holds, the shared scan core + internal
   decide/guard functions, guarded publish-claim/approval wrappers,
   hold resolve/reactivate review RPCs, hamming function, backfill stub).
   WAVE-3: the committed held-row contract is realized by the merged
   exact-byte guard `visual_group_guard_trigger` (in the
   `DRAFT_visual_group_claim_trigger_20261002.sql` draft, step 1), not by a
   separate scene trigger — the scene file installs no calendar trigger.
4. `migrations/DRAFT_visual_scene_calendar_atomic_write_20261004.sql`
   (canonical candidate binding + atomic insert writer, integrated in this
   draft branch: unique index
   `visual_scene_candidate_canonical_binding_uq` on
   (tenant_id, group_key, object_role, exact_url), stable same-evidence
   reuse, and revocation of the direct service_role registration route).
5. `migrations/DRAFT_visual_scene_calendar_atomic_patch_20261004.sql` (atomic
   media PATCH scene binding draft, 2026-10-04 wave — see "Atomic media PATCH
   RPC" below; DRAFT/UNAPPLIED/OFF, no calendar trigger, no occupancy writes;
   REQUIRES step 4's canonical binding).
6. Any activation draft — must remain LAST. None exists yet.

`migrations/DRAFT_visual_scene_phash_20261003.sql` is a SUPERSEDED design
sketch (its record functions raise 0A000) and is NOT applied anywhere in this
order.

The wave file may only be applied after step 2 (it FK-references
`visual_group` and `visual_global_object_attestation`) and strictly before
any activation draft.

## What the wave SQL implements (post-repair)

- (a) `visual_scene_candidate` — prep-time staging, owner-attested pHash
  evidence bound to exact verified bytes (FK to
  `visual_global_object_attestation`) AND to the calendar row's exact
  DELIVERED object via `object_role` (`display` = `image_url`, `poster` =
  `thumbnail_url` when distinct) + exact_url + fingerprint. A bare
  tenant/group candidate is never sufficient; `visual_scene_row_candidate`
  resolves the candidate for a row and the scan fails closed on mismatch.
  NEVER counts as use, never gates.
- (b) `visual_scene_phash_occupied` — once-used scenes, written ONLY inside
  the claim path (the draft BEFORE trigger, or the internal decide/guard
  functions on a clean verdict). Occupancy is PERMANENT: append-only,
  never freed on local release, denial, or swap — mirroring the exact-byte
  ledger's "released stays consumed" semantics.
- (c) `visual_scene_review_hold` — durable holds written ONLY by the
  non-raising claim paths. Post wave-2 the hold row also carries
  `candidate_id` (FK), `exact_url`, `fingerprint`, and
  `resolution_evidence`, and the partial unique open-hold index
  `visual_scene_review_hold_open_uq` spans
  (tenant_id, group_key, claim_date, calendar_row_id, candidate_id,
  candidate_phash, matched_phash, matched_tenant_id, matched_group_key,
  matched_used_date) WHERE state='open': a retry of the same conflict for
  the same row inserts no duplicate (`visual_scene_write_holds`
  conflicts-do-nothing), and a later DISTINCT conflict can never reuse an
  earlier approval. Holds carry the full conflict scope; an APPROVED hold
  exempts ONLY the exact reviewed candidate + delivered object + matched
  occupied conflict for that exact claim date.
- (d) `visual_scene_hamming` (immutable plpgsql) + full-table occupied scan
  inside the shared, write-nothing, INTERNAL scan core
  `visual_scene_claim_scan(content_calendar, uuid)` — server-side, no
  exact-match pre-filter, serialized by the single fleet-wide advisory xact
  lock taken AFTER the caller's scene-component locks (fleet lock order,
  enforced inside the trigger path).
- (e) `visual_scene_backfill_occupied` — STUB raising 0A000; backfill is an
  activation-draft deliverable, never ad hoc.
- WAVE-3 ARCHITECTURE (per `SCENE_WAVE3_SOL_DESIGN.md`, superseding the
  wave-2 layout): the separate early scene BEFORE trigger
  `content_calendar_scene_wave_claim_guard` and function
  `visual_scene_calendar_claim_guard` are REMOVED. PostgreSQL runs same-kind
  triggers alphabetically, so a later raising trigger could roll back holds
  written by an earlier scene trigger — that design failed the durable-hold
  contract. The scene decision now lives INSIDE the exact-byte guard
  `visual_group_guard_trigger`
  (`migrations/DRAFT_visual_group_claim_trigger_20261002.sql`), the ONE
  authoritative content_calendar BEFORE path, gated on
  `to_regprocedure('public.visual_scene_claim_scan(public.content_calendar,uuid)')`
  so the guard behaves byte-identically to the pre-scene guard when the
  scene draft is absent. Ordering invariant in the merged guard: AFTER
  unconditional `visual_group_resolve_row` + changed-media refinement +
  component locks + re-check, and BEFORE `visual_group_sync_row`: byte
  attestation (23514 fail-closed) → bound candidate (23514 fail-closed) →
  fleet-locked scan → in-memory held mutation
  (`variant_status='archived'`, `status='pending'`,
  `media_not_ready_reason='scene_review_hold'`, clears BOTH
  `publish_claim_token` AND `publish_reservation_day`). Then
  `visual_group_sync_row` (which may still raise — no hold exists yet), and
  the scene writes are the LAST writes: conflict → idempotent
  `visual_scene_write_holds`, clean → occupancy insert, then immediate
  `RETURN NEW`. Nothing can raise after hold insertion; a failure rolls back
  both calendar row and hold. A caller-prefilled `NEW.visual_group_key` is a
  hint only: unconditional `visual_group_resolve_row` overwrites a mismatched
  caller-prefilled key with the resolved group. A genuinely unresolved identity
  remains not-ready; a stale hint does not itself force that unresolved state.
- `visual_scene_publish_claim_guarded` (returns NULL for held rows; raises
  0A000 for the actual token mint pending calendar integration) and
  `visual_scene_approval_guarded` (held rows never approvable) — both read
  PERSISTED row state, never attempted-update success.
- `visual_scene_hold_resolve` / `visual_scene_hold_reactivate` — wave-3:
  lock order matches actual DML per Sol design — calendar row FOR UPDATE
  FIRST, then component locks, then fleet lock (a normal UPDATE already
  holds the row before its trigger seeks component locks, so
  component/fleet-before-row can deadlock). Both reload the current calendar
  row, rebind exact candidate/tenant/group/role/URL/fingerprint/pHash,
  reload the hold under lock, rescan the live conflict, and reject stale
  binding and cross-tenant review. Approval exempts only the exact reviewed
  conflict and does NOT activate the row. Reactivation rescans, refuses a
  row with no calendar row (`23514`), guards against mid-reactivation
  mutation (ROW_COUNT, `23514`), and verifies persisted identity after the
  update. A persisted tenant/group/date/candidate mismatch raises `23514` and
  rolls back the activation and occupancy changes. A fresh conflict that
  persists as not-ready is reported as `converted_back_to_held`, never as a
  false success.

Policy bands (mirror `agent/visual_scene.py`): hamming ≤ 6 near_frame blocks
(cross-tenant any date, or same-tenant different date); 7..30 uncertain holds
AND blocks; > 30 distinct; unknown or mismatched candidate evidence for the
claimed row fails closed. Same-tenant same-date same-group channel siblings
remain legal and idempotent.

## Sol audit P0 dispositions (SCENE_AUDIT_GAPS.md, 2026-10-04)

- P0-1 candidate not bound to the row's delivered object — FIXED:
  `object_role`/`exact_url`/`fingerprint` binding, `visual_scene_row_candidate`
  resolution, fail-closed on mismatch, fail-closed on >1 distinct pHash staged
  against one delivered object.
- P0-2 claim-path SECURITY DEFINER functions executable by service_role —
  FIXED: EXECUTE revoked from public/anon/authenticated AND service_role on
  scan/decide/guard/row_candidate/write_holds/calendar-trigger/backfill
  (trigger/internal only, exact-byte precedent). The only service_role
  entry points are the safe read/review surfaces that cannot fabricate
  occupancy or holds (hamming, register_candidate, hold_resolve,
  hold_reactivate, publish_claim_guarded, approval_guarded) plus SELECT on
  the three scene tables.
- P0-3 holds without validated row / no open-hold uniqueness / no committed
  held state — FIXED: partial unique open-hold index + idempotent
  `visual_scene_write_holds` + the committed held calendar row (P0-4).
- P0-4 calendar BEFORE trigger does not call the scene decision or
  atomically hold the row — DRAFTED, wave-3 architecture: the scene decision
  is INSIDE the exact-byte guard `visual_group_guard_trigger` (the separate
  `visual_scene_calendar_claim_guard` trigger is removed — Sol wave-3
  design). The merged guard never raises on a scene conflict: it mutates NEW
  to the held state in memory (`variant_status='archived'`,
  `status='pending'`, `media_not_ready_reason='scene_review_hold'`, BOTH
  `publish_claim_token` AND `publish_reservation_day` cleared), runs
  `visual_group_sync_row` (old active local membership released; may still
  raise — no hold exists yet, so a failure rolls back BOTH row and hold),
  and only THEN inserts the idempotent hold as the LAST write before
  `RETURN NEW`, so no later trigger/function can raise after hold insertion.
  Clean path: byte attestation and bound-candidate checks (23514
  fail-closed) → fleet-locked scan → `visual_group_sync_row` → occupancy
  insert as last write. Former ACTIVATION BLOCKER 1 is resolved by
  construction: the scene decision runs after the exact-byte guard's
  unconditional group resolution. A caller-prefilled key is only a hint; a
  mismatched value is overwritten with the resolved group. Truly unresolved
  identities remain not-ready.
- P0-5 publish-claim/approval RPCs can report false success — DRAFTED:
  guarded wrappers enforce the persisted-state contract (NULL token for held
  rows; held rows never approvable). Routing the REAL publish-claim RPC
  (PR230 path) through the guard remains ACTIVATION BLOCKER 2; the wrapper
  raises 0A000 for the actual mint.
- P0-6 backfill 0A000 stub — REMAINS an activation blocker (BLOCKER 4).
  Unknown or source-null published history must STAY HELD for review; the
  backfill may never mark a staged candidate as used.
- P0-7 no validated hold resolution / re-scan / scoped exemption /
  reactivation — DRAFTED: `visual_scene_hold_resolve` (actor + evidence
  validated, current-scene re-evaluation: candidate and matched occupied row
  must still exist at the recorded hamming distance, drift refuses
  resolution; wave-2: candidate validated by phash + exact_url +
  fingerprint, evidence stored one-shot in `resolution_evidence`) and
  `visual_scene_hold_reactivate` (approved-only, fresh re-scan, refuses
  while still blocked). Persisted tenant/group/date/candidate mismatch raises
  `23514`, rolling back activation and occupancy changes; a fresh conflict
  that persists as not-ready is reported as `converted_back_to_held`.
  Exemption scope is enforced in
  `visual_scene_claim_scan`: an approved hold exempts ONLY the exact
  reviewed candidate + delivered object + matched occupied conflict for its
  exact claim date.
- P0-8 fleet lock order unenforced — DRAFTED, wave-3 lock order per Sol
  design: normal claim path (inside the merged guard): unconditional
  resolution → component locks → fleet advisory lock inside the scan.
  Review functions (`visual_scene_hold_resolve`,
  `visual_scene_hold_reactivate`): calendar row FOR UPDATE FIRST → component
  locks → fleet advisory lock — matching actual DML order, since a normal
  UPDATE already holds the row before its trigger seeks component locks
  (component/fleet-before-row would deadlock). Deadlock-freedom is covered
  by a disposable-PG opposing-session scenario (lock_timeout on the row,
  never 40P01). The future backfill's order (component first, then fleet) is
  documented in the wave file for the activation draft.

## Sol audit P1 dispositions

- P1-1 writer emits `scene_candidate` in prepared payloads but nothing
  registers it / no column stores the candidate UUID — the INSERT writer lane
  now has an ATOMIC DRAFT route (2026-10-04, echo-scene-writes):
  `visual_scene_insert_calendar_batch` resolves raw tenant aliases, verifies
  the exact displayed URL/role/md5/`verified_bytes`, stages candidate N before
  row N's BEFORE INSERT trigger, and inserts the full batch in one PostgreSQL
  statement transaction. Any candidate or row error rolls back every earlier
  candidate and row; the rejected post-write registration + compensation
  DELETE path is not used. `agent/visual_scene_register.py` builds and verifies
  this contract behind the exact conjunction
  `AGENT_VISUAL_SCENE_REGISTER=true` AND
  `AGENT_VISUAL_GLOBAL_WRITER_PREP=true` AND
  `AGENT_VISUAL_SCENE_CANDIDATE=true` (all default OFF). Once REGISTER is
  explicitly ON, a missing/off/ambiguous prerequisite raises before either
  new-row REST or RPC insertion; it cannot silently degrade that insertion to
  REST. Existing Story hold reconciliation runs before the new-row insertion
  branch and remains an explicit recovery-path activation blocker below. With
  REGISTER OFF, the normal REST insert remains and makes zero scene RPC calls.
  Candidate staging
  never marks use or occupancy; only the
  calendar claim trigger can do that. No candidate column is needed because
  the trigger binds by canonical tenant + group + exact delivered role/URL and
  the attested fingerprint. The additive draft makes that binding canonical:
  identical siblings/retries reuse one stable candidate UUID under a unique
  tenant/group/role/exact-URL key, while a different pHash or fingerprint on
  the same binding fails closed. It also revokes direct `service_role` EXECUTE
  on the frozen insert-only `visual_scene_register_candidate`; otherwise a
  random-UUID side registration could destabilize row binding. The atomic RPC
  also checks this ACL at runtime and fails closed if a later
  migration or privilege change reopens the direct route. ACTIVATION
  BLOCKER 6 remains OPEN: the SQL is
  UNAPPLIED/OFF, recovered story-hold rows are pre-existing and outside this
  insert path, and media PATCH candidate staging is not yet composed with the
  existing patch CAS in one database transaction.

### Atomic INSERT draft install/upgrade prerequisite

`DRAFT_visual_scene_calendar_atomic_write_20261004.sql` takes SHARE locks on
the frozen candidate and review-hold inventories before changing any ACL,
function, or index. Every pre-existing duplicate tenant/group/role/exact-URL
binding aborts the transaction with candidate IDs, complete identity/evidence,
classification (`identical_evidence`, `conflicting_evidence`, or
`conflicting_identity`), and all referencing `visual_scene_review_hold`
IDs/candidate FKs. The refusal leaves the old function definition, direct
registration ACL, candidates, holds, and indexes unchanged.

No contradiction is auto-merged. The separate owner-only
`DRAFT_visual_scene_calendar_candidate_dedupe_20261004.sql` path accepts only
an explicitly reviewed pair whose tenant, group, role, exact URL, pHash, md5,
evidence JSON, and actor are identical. It requires the frozen resolver's
lowest UUID as canonical, archives the complete duplicate candidate and every
referencing hold row in a remediation receipt, rebinds those hold FKs, and
deletes the duplicate in one transaction. Conflicting evidence/identity stays
blocked for source-level adjudication. After remediation, the installer writes
a zero-duplicate count plus full candidate-snapshot digest receipt, builds and
catalog-validates the exact unique btree index shape, rechecks the receipt, and
only then changes ACLs/functions. Replay repeats those proofs.

The first canonical registration retains immutable provenance in candidate
evidence (`registration_provenance=first_atomic_calendar_registration` and
`first_calendar_row_id`). Sibling/retry responses report
`registration_reused=true`; they never rewrite the first observation. Their
own persisted row-to-candidate binding is still reverified after triggers.
- P1-2 rollback-only pg test wrapper vs migrations that COMMIT internally —
  addressed BY DESIGN: the real-PostgreSQL scenarios run COMMITTED
  transactions on a disposable, dedicated scratch database (never
  production); committed state is exactly what the held-row contract needs
  to prove. See current final-source and fresh PostgreSQL 17 evidence at the
  top and below; earlier counts are retained as historical run records.
- P1-3 denial/swap occupancy permanence naming — fixed in SQL comments and
  here: occupancy is PERMANENT; local release/denial/swap never frees a
  scene, mirroring the exact-byte ledger.

## Durable-hold vs rollback (HOLD-ROLLBACK) — RESOLVED, realized by the committed held row

Enforced semantics: **a hold exists IF AND ONLY IF the claim transaction
commits in a blocked/held state.** Post wave-3 this is REALIZED inside the
merged exact-byte guard `visual_group_guard_trigger` (the separate scene
trigger is removed): the conflict branch completes every potentially-raising
step — resolution, attestation, component locks, fleet lock, scan, in-memory
held mutation, `visual_group_sync_row` — BEFORE the final idempotent hold
insertion, then RETURNs NEW immediately, so no later trigger/function can
raise after the hold write and the holds commit with the held calendar row
in the same transaction. A forced raise in the sync/release path leaves
NEITHER row nor hold (verified on scratch PG). No caller replay, no
errdetail contract. The non-raising `visual_scene_claim_decide` is the
internal function form of the same contract for claim contexts that compose
their own row write. The RAISING `visual_scene_claim_guard` (kept for
hard-fail contexts) still writes NO holds by construction — its own raise
would roll them back (even through a plpgsql EXCEPTION handler's savepoint
— verified on scratch PG); its errors carry full match detail in errdetail
for forensics.

Autonomous-transaction alternatives (a dblink self-connection writing the
hold from inside a raising path) remain REJECTED: fragile under
transaction-mode connection poolers (session state and advisory-lock
assumptions break) and they demand stored connect credentials inside the
database, which this security-definer surface must not require.

## Calendar media-writer inventory (2026-10-04)

Scene guard activation remains blocked until every row that can introduce new
displayed bytes stages its bound candidate in the same database transaction:

- `insert_rows` — covered by the new draft atomic batch RPC only when
  `AGENT_VISUAL_SCENE_REGISTER=true` AND
  `AGENT_VISUAL_GLOBAL_WRITER_PREP=true` AND
  `AGENT_VISUAL_SCENE_CANDIDATE=true`; REGISTER ON with either prerequisite
  absent/off/ambiguous fails before new-row persistence. Story hold recovery
  occurs earlier and is separately uncovered. Normal REST behavior remains in
  place while REGISTER is OFF.
- `create_variant_candidate` — covered by that same atomic RPC for its prepared
  alternate-row INSERT. Variant promotion (`swap_variant`) changes row state in
  an existing transaction but introduces no new bytes.
- `patch_image_url`, `patch_media`, `swap_media`, and the staging branch of
  `restage_held_media` — each has a server-side full-row/media CAS today, but
  preparation currently returns only `visual_group_key`/`byte_hash`; candidate
  staging is not composed with the PATCH in one PostgreSQL transaction.
- `replace_future_infographic_media` — exact receipt-owned CAS exists, but its
  replacement candidate is not transactionally staged with the PATCH.
- hold/release-only methods (`hold_future_infographic_media`,
  `release_future_infographic_media`, the release branch of
  `restage_held_media`) do not introduce new displayed bytes and need no new
  candidate, though their persisted held/readiness contracts still apply.
- recovered Story hold rows are pre-existing rows and do not pass through the
  new-row RPC; their own recovery path must be reconciled before activation.

The uncovered PATCH/recovery surfaces are explicit activation gaps. Existing
CAS reduces stale-write risk but is not atomic scene-candidate registration.

## ACTIVATION BLOCKERS

1. CALENDAR WIRING / RESOLVED-KEY ORDER — RESOLVED by the wave-3
   architecture (Sol design): the separate early scene trigger is removed;
   the scene decision runs inside `visual_group_guard_trigger` AFTER its
   unconditional object/group resolution and component locking. A
   caller-prefilled `visual_group_key` is only a hint; unconditional
   resolution overwrites a mismatched value with the resolved group. Truly
   unresolved identities remain not-ready. No row is skipped for lacking a
   pre-populated key or pre-staged candidate, and no claim, hold, or occupancy
   is based on a forged hint.
2. PUBLISH-CLAIM RPC WIRING. The real publish-claim RPC (PR230 path) is not
   part of this draft stack; `visual_scene_publish_claim_guarded` enforces
   the persisted-state contract (NULL for held rows) and raises 0A000 for
   the actual token mint. The activation draft must route the real RPC
   through this persisted-state check. The approval RPC must likewise
   consult persisted state (`visual_scene_approval_guarded`).
3. PUBLISH RESERVATION COLUMNS — RESOLVED by the wave-2 pass:
   `publish_reservation_day` (real column from
   `calendar_publish_day_capacity_20260917.sql`) is cleared alongside
   `publish_claim_token` in the held mutation.
4. BACKFILL (still 0A000). Occupied history must be derived from the
   exact-byte ledger and attested candidates before activation. Unknown or
   source-null published history must STAY HELD for review, and the backfill
   may never mark a staged candidate as used. Its lock order (component
   locks first, then the fleet advisory lock) is documented in the wave file
   for the activation draft.
5. EXACT-DIFF ACCEPTANCE. The independent 145-pass PostgreSQL 17 run
   verifies the current source package, not a future activation diff. The
   activation reviewer must verify the coverage report and repair-spec suite
   against the exact integrated diff, including clean claim, legal sibling,
   cross-date / cross-tenant near conflict, uncertain band, concurrent
   conflicting claims, preparation without use, denial/swap, invalid
   attestation, persisted hold, approval/claim false-success prevention,
   hold resolution, and unknown-history backfill. `visual_global_coverage` and
   `visual_global_history_coverage` must show reviewed coverage before
   activation.
6. RUNTIME CANDIDATE REGISTRATION — INSERT LANE DRAFT EXISTS, STILL OPEN for
   activation. `agent/visual_scene_register.py` routes prepared inserts through
   the single-transaction `visual_scene_insert_calendar_batch` RPC behind the
   exact explicit-on conjunction of `AGENT_VISUAL_SCENE_REGISTER`,
   `AGENT_VISUAL_GLOBAL_WRITER_PREP`, and `AGENT_VISUAL_SCENE_CANDIDATE`
   (all default OFF). REGISTER ON with either prerequisite not explicitly ON
   fails before new-row insertion; pre-insert Story hold recovery remains
   outside this route. Candidate N
   is registered before row N's BEFORE trigger; candidate/row failures abort
   the full RPC, with no post-write compensation. The blocker remains OPEN
   because the RPC is an UNAPPLIED DRAFT, recovered story-hold rows are outside
   this new-row path, and existing media PATCHes have server-side CAS but do
   not yet stage their replacement candidate inside that same transaction.
   Do not arm the scene guard until every engaged write path is atomic and the
   exact applied diff passes disposable-PG and independent review.

Only after blockers 2, 4, 5 and 6 are closed: flip `SCENE_GUARD_OPERATIONAL`
and arm `AGENT_VISUAL_SCENE_GUARD`. Nothing in this document certifies
activation readiness.

## Scratch-PG verification evidence (2026-10-03 / 2026-10-04)

Draft-level verification against disposable local PostgreSQL instances (never
production; the migration remains UNAPPLIED everywhere):

- Historical 2026-10-03 (pre-repair decide/guard contract): blocked-then-committed
  claim via `visual_scene_claim_decide` leaves durable hold rows with
  `visual_scene_phash_occupied` EMPTY and candidates untouched; the raising
  path leaves ZERO holds (including through an EXCEPTION-handler savepoint);
  band behaviors confirmed (≤ 6 blocks, 7..30 blocks with holds, > 30
  distinct, same-tenant same-date same-group legal, unknown candidate
  fail-closed with no holds).
- Historical 2026-10-04 (agent-3 scratch PostgreSQL 16 run against the repaired SQL):
  - held-commit row state — a conflicting claim COMMITS the calendar row in
    the held state (`variant_status='archived'`, `status='pending'`,
    `media_not_ready_reason='scene_review_hold'`, publish_claim_token NULL)
    with its hold rows durable and no occupancy/exact claim for it;
  - uniqueness retry — re-running the same conflict for the same row inserts
    NO duplicate open hold (stable open-hold uniqueness key);
  - permission-denied — service_role cannot EXECUTE the internal claim path
    (scan/decide/guard/row_candidate/write_holds/trigger function);
  - NULL token — `visual_scene_publish_claim_guarded` returns NULL for a
    held row;
  - resolve/reactivate — validated resolution transitions open→terminal
    exactly once; reactivation succeeds only after approval with a clean
    fresh re-scan and refuses while still blocked;
  - occupancy persistence — occupancy survives local release/denial/swap
    (append-only, mirrors the exact-byte ledger).

- Historical 2026-10-04 (wave-2 pass, disposable PostgreSQL 17, port 54399, dropped
  after; all transactions COMMITTED, nothing rollback-only):
  - clean claim on an UNKEYED row: trigger auto-resolves the exact-byte
    group, commits active, pHash occupancy + local ledger + global usage
    written; same-day sibling stays single-occupancy idempotent;
  - held conflict: row commits `pending/archived/scene_review_hold` with
    BOTH `publish_claim_token` AND `publish_reservation_day` NULL, open hold
    durable in the same transaction, old local membership released, zero
    occupancy for the held row; retry inserts no duplicate hold; a DISTINCT
    row hitting the same conflict gets its own open hold;
  - missing/invalid byte attestation: 23514 fail-closed BEFORE any hold
    write — zero rows, zero holds, zero occupancy;
  - raw-alias gym key: evaluated (canonical tenant resolution) and held
    against the occupied conflict, not skipped;
  - uncertain band: two DISTINCT holds for two matched occupied identities
    (approval-reuse prevention);
  - resolution/reactivation: approval stores actor + `resolution_evidence`
    with exact `exemption_scope`; reactivation refuses a still-blocked
    different conflict with no mutation, succeeds after its own approval,
    records occupancy only for the exempted conflict;
  - busy fleet lock: `visual_scene_hold_resolve` under a held advisory lock
    raises 'global scene claim busy; retry transaction' (55P03), hold stays
    open;
  - media-kind binding: a `display` candidate on a video file URL fails
    closed (23514) for a video row; a `poster` candidate on the exact
    thumbnail URL claims clean;
  - Historical run: `tests/test_scene_claim_wave_*.py`: 127 passed / 0 failed with the
    disposable DSN (25 committed-transaction PG scenarios + 102
    static/classify/flags/prepare), 3 consecutive random-order passes;
    without DSN: 102 passed, 25 skipped (convention preserved).
- Historical 2026-10-04 (wave-3 architecture pass, disposable PostgreSQL 17 clusters,
  dropped after; all transactions COMMITTED):
  - merged guard ordering: conflict commits held row + hold atomically;
    a forced raise in exact-byte release/sync on the conflict path leaves
    NEITHER row nor hold (scratch-only override, restored after);
  - forged `visual_group_key` hint: overwritten by unconditional group
    resolution; a truly unresolved identity remains not-ready;
  - review lock order: concurrent writer holding the calendar row blocks
    `visual_scene_hold_resolve` on the ROW (lock_timeout, never 40P01
    deadlock); resolve succeeds after the writer commits;
  - stale binding and cross-tenant resolutions reject (23514);
  - reactivation identity mismatch raises `23514`, rolling back activation
    and occupancy changes; a fresh conflict that persists as not-ready
    reports `converted_back_to_held`; real reactivation records occupancy
    for the exempted conflict only;
  - scene-absent gate: without the scene draft the guard applies and claims
    exactly as pre-scene (`visual_group_identity_unresolved` for unkeyed
    rows, no scene tables);
  - Historical run: `tests/test_scene_claim_wave_*.py`: 133 passed / 0 failed, EXIT=0 with
    the disposable DSN (31 committed-transaction PG scenarios + 102
    static/classify/flags/prepare); without DSN: 102 passed, 31 skipped,
    EXIT=0. Two scenarios use clearly-marked scratch-only instrumentation
    (forced release raise; emulated re-hold), each restored/dropped in
    `finally` with a positive-path sanity assertion.

- Current independent Astra run (2026-10-04, fresh disposable PostgreSQL 17):
  `tests/test_scene_claim_wave_*.py`: 145 passed, EXIT=0. This supersedes the
  earlier 127/133 run counts as the current verification result. The final
  source audit is recorded in `SCENE_WAVE3_FINAL_SOURCE_AUDIT_20261004.md`;
  it found no P0/P1 in the frozen source. This does not close production
  blockers.

The pg test suite (`tests/test_scene_claim_wave_pg.py`, committed-transaction
scenarios on a disposable DSN by design — Sol P1-2) has been rerun against
the wave-3 merged-guard SQL as recorded above. The latest 145-pass run is
draft-level verification, NOT activation acceptance — the repair-spec
acceptance suite (BLOCKER 5) must still be run by the independent reviewer on
the exact activation diff.

## Test-fixture pattern: Walsh–Hadamard codebook (Astra directive)

pg/classify test fixtures MUST use the deterministic Walsh–Hadamard
codebook, not random pHashes and not an impossible-min-distance codebook:
64-bit words derived from parity(i & j) per bit position, giving pairwise
Hamming distance exactly 32 between distinct words, rendered as 16 lowercase
hex chars (`'016x'` formatting on the 64-bit value). Deterministic words at
mutual distance 32 let tests place any fixture pair at a chosen band
boundary by flipping a known number of bits, with no randomness and no
accidental sub-31 distances. Replace any legacy random-codebook fixtures
with this pattern.

## Rollback

Nothing is applied anywhere: delete
`migrations/DRAFT_visual_scene_claim_wave_20261003.sql`. If it were ever
applied to a scratch database, drop in the order listed in the file header
(trigger → trigger function → wrappers/review RPCs → claim-path functions →
scan/row_candidate/write_holds/register/hamming → triggers/tables). The
exact-byte ledger needs no rollback. Test files under
`tests/test_scene_claim_wave_*.py` are new and self-contained; deleting them
removes this wave's test surface.

## Unresolved limitations

- Production readiness remains blocked by real publish-claim / approval RPC
  wiring, historical occupied-scene backfill, and transactional runtime
  candidate registration. Blocker 5 also requires acceptance on the exact
  activation diff; the current 145-pass source-package run is not that gate.
  `publish_reservation_day` clearing and resolved-key ordering are implemented
  in the draft. Exact-diff acceptance (BLOCKER 5) remains outstanding even
  though the current source package has a fresh independent 145-pass run.
- Runtime candidate registration remains open: no Python writer calls
  `visual_scene_register_candidate`. Post wave-2 an engaged row WITHOUT a
  bound candidate is a fail-closed 23514 rejection (not inert), so partial
  writer wiring changes claim behavior and must land deliberately.
- Backfill is a stub; occupied history is empty until the activation draft
  derives it from the exact-byte ledger (BLOCKER 4).
- The claim path serializes ALL scene claims fleet-wide on one advisory
  lock; throughput under concurrent calendar writes is unmeasured.
- The 7..30 band remains calibrated on exactly one measured incident pair
  (Swift River JCK_6328/JCK_6331, hamming 28) and is hold-only.

## Atomic media PATCH RPC — candidate staging + CAS in one transaction (2026-10-04 wave)

STATUS: DRAFT / UNAPPLIED / OFF. Executed per
`KIMI_SCENE_PATCH_SWARM_20261004.md` (two-child AgentSwarm; parent
integration only in this file). Nothing is wired into Python; the Astra lead
owns integration and independent acceptance. This does NOT close any
activation blocker and does NOT certify activation readiness.

New files (this wave only, both uncommitted in the isolated worktree):

- `migrations/DRAFT_visual_scene_calendar_atomic_patch_20261004.sql`
- `tests/test_scene_candidate_patch_pg.py`

### SQL contract

`public.visual_scene_atomic_media_patch(p_row_id uuid, p_gym_id text,
p_expected jsonb, p_patch jsonb, p_candidate jsonb default null) returns
jsonb` — SECURITY DEFINER, `search_path=public, pg_temp`, EXECUTE granted to
`service_role` only (revoked from public/anon/authenticated).

REPAIRED after independent-review rejection (2026-10-04): the draft now
depends on the canonical insert-writer migration (order step 4) and shares
its stable candidate identity instead of calling the frozen random-UUID
registration route on every call.

0. Dependency guards: `visual_scene_candidate_canonical_binding_uq` must
   exist and `service_role` must NOT hold EXECUTE on the frozen
   `visual_scene_register_candidate` route (both installed by the write
   draft); otherwise the RPC raises (42P01 / 42501) before any write.
0a. Eligible before-image (independent-review P1 repair, 2026-10-04;
   second pass after the P1 REJECT on the negative-check form), checked
   BEFORE any candidate staging or update: NULL-safe POSITIVE eligibility —
   status must PROVE one of `pending`/`coach_review`/`approved` and
   `variant_status` must PROVE exactly `active`. The frozen scene guard's
   active predicate (`visual_group_row_active`) requires
   `variant_status='active'` and includes `approved`, so an approved unsent
   row IS re-decided on a media update and must not be refused (this
   matches `patch_image_url`'s publish-time approved form and the
   approval-preserving `replace_future_infographic_media` shape), while a
   NULL status (`NOT IN` evaluates to NULL, not TRUE) or a non-active
   variant (`'candidate'`/NULL slip an archived-only check) would return
   `patched` without any scene re-decision and is now refused. Published/
   claimed/reserved rows (published_at, publish_claim_token,
   publish_reservation_day or late_post_id set), non-active variants
   (archived/candidate/NULL), currently scene-held rows
   (`media_not_ready_reason='scene_review_hold'`) and rows with an OPEN
   `visual_scene_review_hold` are refused with 23514 before anything is
   staged. Only OPEN holds block: hold resolution is one-shot
   (`open`→`approved`/`rejected`), so resolved hold HISTORY (e.g. on a
   reactivated row) never refuses the patch. Behavioral regressions:
   `test_candidate_variant_refused_before_staging_and_mutation`,
   `test_null_variant_refused_before_staging_and_mutation` and
   `test_null_status_refused_before_staging_and_mutation` in
   tests/test_scene_candidate_patch_pg.py — the ineligible states are
   staged scratch-only via session_replication_role=replica on the
   disposable DB (the scratch content_calendar stub has nullable
   status/variant_status); the production guard is NOT weakened.
1. `p_patch` keys restricted to a fixed media allowlist (`image_url`,
   `thumbnail_url`, `source_media_url`, `source_media_asset_id`,
   `drive_file_id`, `byte_hash`, `r2_key`, `visual_group_key`,
   `media_not_ready_reason`); any other key (status, caption, post_date, …)
   raises 22023 — approval state and tenant scope can never be rewritten.
   `p_expected` must carry EVERY one of the 23 `_VISUAL_MEDIA_CAS_COLUMNS`
   keys (`agent/portal_calendar_store.py:66-72`) — explicit JSON null values
   required where observed; an ABSENT key raises 22023 rather than silently
   comparing as NULL.
2. The candidate must be staging-only evidence in the same shape the insert
   writer accepts (kind/stage/usage_claimed=false/counts_as_use=false), name
   the row's own canonical tenant (`visual_group_tenant_strict` on both
   sides), and bind the POST-PATCH visual group and exact delivered object
   (display=image_url, poster=thumbnail_url when distinct) — a bare tenant
   match is never sufficient (changed group/URL/role raise 23514).
3. Canonical staging BEFORE the UPDATE: look up the ONE canonical binding
   (tenant_id, group_key, object_role, exact_url); reuse the stable UUID
   only when pHash AND fingerprint match exactly (contradictory evidence
   raises 23514); on first registration insert via the frozen
   `visual_scene_register_candidate` as the SECURITY DEFINER owner (never
   the direct service_role route) and catch unique_violation so concurrent
   identical registrations converge on the committed winner. Preparation
   only — never writes `visual_scene_phash_occupied`.
4. One CAS UPDATE whose WHERE mirrors `_visual_media_cas` exactly: `id` +
   `gym_id` plus NULL-safe `is not distinct from` over all 23 expected
   columns. `returning to_jsonb(content_calendar)` yields the persisted
   POST-TRIGGER row (the UPDATE re-enters the merged
   `visual_group_guard_trigger`).
5. Post-trigger disposition: a persisted scene hold (status='pending',
   variant_status='archived', media_not_ready_reason='scene_review_hold',
   no claim token/reservation) returns a DISTINCT
   `{"outcome":"held","candidate_id","candidate_reused","row"}` with the
   actual committed row — never `patched` — and does NOT raise (the
   committed hold is preserved). `patched` additionally requires every
   intended patch value persisted AND the rebound row candidate
   (`visual_scene_row_candidate` + `visual_scene_row_delivered_object`)
   equal to the stable candidate (group/role/exact URL included); any drift
   raises 23514 and rolls the whole transaction back.
6. Stale CAS: zero rows → private marker SQLSTATE `PZ001` rolls the
   candidate insert back inside the plpgsql subtransaction (NO compensating
   DELETE), then classifies `{"outcome":"stale"}` (row exists in scope) vs
   `{"outcome":"not_found"}` (missing or cross-tenant — indistinguishable,
   matching portal 404 discipline). Only PZ001 is caught; all other errors
   propagate.

### Test evidence (draft-level, disposable PG only)

`tests/test_scene_candidate_patch_pg.py` applies the canonical insert
writer resolved from THIS repository's own `migrations/` directory
(integrated stack, never a sibling worktree) BEFORE the patch migration;
the superseded phash sketch is NOT applied. 27 scenarios: stack
applies + service_role-only EXECUTE + canonical binding installed; direct
service_role registration stays revoked; happy path (staging + CAS +
post-trigger row + occupancy only via trigger); same-object retry reuses the
stable candidate UUID; concurrent identical registration converges on ONE
canonical candidate (one `patched`, one distinguishable `stale`);
contradictory pHash on the same binding fails closed; changed
group/URL/role rejected; persisted scene hold returns `held` with the actual
committed archived row (hold preserved, no occupancy fabricated); incomplete
expected image rejected per missing CAS key while explicit JSON nulls pass;
stale CAS rolls back staging (no residue on the canonical binding); missing
and cross-tenant rows both `not_found`; candidate tenant mismatch 23514;
unattested candidate fails closed; non-staging-only candidate evidence
rejected; patch allowlist refuses status/caption/post_date; stale-then-fresh
retry patches cleanly as a fresh insert; approved UNSENT row patches under
the full CAS with approval state preserved; an approved row carrying an
OPEN scene hold is refused before any write; a row reactivated after an
approved (closed) hold patches cleanly — resolved hold history never
blocks; inactive and NULL status/variant rows are refused before staging;
each of published_at, publish_claim_token, publish_reservation_day and
late_post_id independently refuses a media patch before staging.

REPAIR-PASS STATUS: the disposable PG run is BLOCKED in this session —
macOS SysV shared memory is exhausted (`shmget ... No space left on
device`), so the task-owned cluster at `.scratch-scene-patch/` (preserved,
not recreated) cannot start and no other task's socket may be touched. The
earlier 8/8 pass predates the repair and is superseded. Without
`VISUAL_SCENE_PATCH_TEST_DSN` the module skips cleanly (27 skipped),
preserving suite convention. Fresh PG evidence is owed before activation
acceptance.

### Read-only mapping findings (which Python paths fit this RPC)

Shared shape today: Python computes byte attestation → group/prepare RPCs →
separate PATCH with full-row CAS → response re-verification. The window
between registration RPC and PATCH is only DETECTED by the CAS, not
prevented; the new RPC closes it for single-row media writes.
`insert_rows` currently DROPS `scene_candidate`
(`portal_calendar_store.py:2866-2873`) — candidate registration has no
transaction home today; that remains the gap this RPC family addresses.

RPC-suitable single-row paths (candidate + media CAS in one tx):

- `patch_image_url` (`portal_calendar_store.py:520-615`) — status-preserving
  story reburn / feed autofit; publish-time form allows `approved`.
- `patch_media` (`:680-746`) — blank-image backfill, clears
  `media_not_ready_reason`, never overwrites existing media.
- `swap_media` (`:748-807`) — pending/coach_review only, never published or
  claim-token rows.
- `restage_held_media` STAGE arm only (`:845-908`) — best fit: hold-preserving
  stage is a pure candidate+media write.
- `replace_future_infographic_media` (`:998-1064`) — high value:
  approval-PRESERVING igfill placeholder swap + exact hold clear on an
  approved row is exactly the atomic shape.
- `recover_story_media_hold` row CAS (`:2922-2977`) — the PATCH itself fits.

Must remain DISTINCT transactions:

- `restage_held_media` RELEASE arm — operator re-read between stage and
  release is a required interleaving; a separate hold-resolve-style RPC.
- Story support-provenance side effects (`_record_confirmed_story_holds`,
  `_retry_story_hold_provenance`) — fail-open alerting must never roll back
  calendar writes.
- `insert_rows` (`:2860-2920`) — batch multi-row insert + best-effort caption
  ledger; needs its own batch design, not the single-row RPC.
- `swap_variant` (`:1334-1355`) — already an atomic multi-row RPC
  (`content_calendar_swap_variant`); precedent, not consumer.
- `claim_publish_slot` / `approve_ready` — trigger-side contract work per
  ACTIVATION BLOCKER 2, not candidate staging.

### Status vs activation blockers

Unchanged: blockers 2 (publish-claim RPC wiring), 4 (backfill 0A000), 5
(exact-diff independent acceptance), and 6 (runtime candidate registration —
this wave drafts the SQL contract for it but deliberately does NOT wire any
Python; the Astra lead integrates it against the insert-only writer package
before assigning that separate owner). `SCENE_GUARD_OPERATIONAL` stays
`False`; no flag flipped; frozen scene migrations and their tests untouched.
