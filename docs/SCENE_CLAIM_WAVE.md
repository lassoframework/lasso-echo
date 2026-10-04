# Scene claim wave — DRAFT / UNAPPLIED / OFF (2026-10-03; P0 repair pass 2026-10-04)

STATUS: DRAFT / UNAPPLIED / OFF / INCOMPLETE. Nothing in this wave is applied
anywhere, no guard is armed, no flag is flipped, no provider is contacted.
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
3. `migrations/DRAFT_visual_scene_phash_20261003.sql` (superseded design
   sketch; its record functions raise 0A000).
4. `migrations/DRAFT_visual_scene_claim_wave_20261003.sql` (this wave:
   candidate staging bound to the row's delivered object, permanent occupied
   scenes, idempotent review holds, the shared scan core + internal
   decide/guard functions, the DRAFT calendar BEFORE trigger realizing the
   committed held-row contract, guarded publish-claim/approval wrappers,
   hold resolve/reactivate review RPCs, hamming function, backfill stub).
5. Any activation draft — must remain LAST. None exists yet.

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
  non-raising claim paths. A partial unique index on
  (tenant_id, group_key, claim_date, calendar_row_id, candidate_phash,
  matched_phash) WHERE state='open' is the stable open-hold uniqueness key:
  a retry of the same conflict for the same row inserts no duplicate
  (`visual_scene_write_holds` conflicts-do-nothing). Holds carry the full
  conflict scope; an APPROVED hold exempts ONLY that exact reviewed conflict
  pair for that exact claim date.
- (d) `visual_scene_hamming` (immutable plpgsql) + full-table occupied scan
  inside the shared, write-nothing, INTERNAL scan core
  `visual_scene_claim_scan(content_calendar, uuid)` — server-side, no
  exact-match pre-filter, serialized by the single fleet-wide advisory xact
  lock taken AFTER the caller's scene-component locks (fleet lock order,
  enforced inside the trigger path).
- (e) `visual_scene_backfill_occupied` — STUB raising 0A000; backfill is an
  activation-draft deliverable, never ad hoc.
- DRAFT calendar BEFORE trigger `content_calendar_scene_wave_claim_guard`
  (function `visual_scene_calendar_claim_guard`) — fires before
  `content_calendar_visual_group_guard` (alphabetical), realizes the
  committed held-row contract (below).
- `visual_scene_publish_claim_guarded` (returns NULL for held rows; raises
  0A000 for the actual token mint pending calendar integration) and
  `visual_scene_approval_guarded` (held rows never approvable) — both read
  PERSISTED row state, never attempted-update success.
- `visual_scene_hold_resolve` (validated one-shot open→approved|rejected
  with named actor + non-empty evidence + current-scene re-scan under the
  fleet lock; refuses on conflict-scope drift) and
  `visual_scene_hold_reactivate` (approved-only; fresh bound-candidate
  re-scan must be clean; reactivation UPDATE passes back through the trigger
  chain).

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
  atomically hold the row — DRAFTED: `visual_scene_calendar_claim_guard`
  never raises on a scene conflict; it writes idempotent holds, mutates NEW
  to the held state (`variant_status='archived'`, `status='pending'`,
  `media_not_ready_reason='scene_review_hold'`, `publish_claim_token`
  cleared), still passes NEW through `visual_group_sync_row` (old active
  local membership released; no exact claim and no pHash occupancy for the
  held row), and RETURNs NEW so the transaction commits the held row with
  its holds. Clean path: `visual_group_sync_row` FIRST (hard-rejects on
  missing/invalid byte attestation), THEN pHash occupancy under the
  still-held fleet lock. Calendar wiring into `visual_group_guard_trigger`
  (resolved-key order) remains ACTIVATION BLOCKER 1.
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
  resolution) and `visual_scene_hold_reactivate` (approved-only, fresh
  re-scan, refuses while still blocked). Exemption scope is enforced in
  `visual_scene_claim_scan`: an approved hold exempts ONLY the exact
  reviewed pair for its exact claim date.
- P0-8 fleet lock order unenforced — DRAFTED: every occupancy writer, hold
  writer, hold resolution and the future backfill uses the same order —
  existing scene-component locks (`visual_group_lock_scene_components`)
  FIRST, THEN the single fleet-wide scene advisory lock inside the scan.
  The trigger takes component locks before the scan; resolution touches only
  scene tables so it takes the advisory lock alone.

## Sol audit P1 dispositions

- P1-1 writer emits `scene_candidate` in prepared payloads but nothing
  registers it / no column stores the candidate UUID — STILL OPEN (writer
  subagent scope). In the repaired SQL the candidate is no longer carried on
  the calendar row at all: the trigger RESOLVES it from the row's delivered
  object via `visual_scene_row_candidate`, so no column is needed on the SQL
  side — but no Python writer calls `visual_scene_register_candidate` yet,
  so staging remains unwired.
- P1-2 rollback-only pg test wrapper vs migrations that COMMIT internally —
  addressed BY DESIGN: the real-PostgreSQL scenarios now run COMMITTED
  transactions on a disposable, dedicated scratch database (never
  production); committed state is exactly what the held-row contract needs
  to prove. The suite is being rerun against the repaired SQL (below).
- P1-3 denial/swap occupancy permanence naming — fixed in SQL comments and
  here: occupancy is PERMANENT; local release/denial/swap never frees a
  scene, mirroring the exact-byte ledger.

## Durable-hold vs rollback (HOLD-ROLLBACK) — RESOLVED, realized by the committed held row

Enforced semantics: **a hold exists IF AND ONLY IF the claim transaction
commits in a blocked/held state.** Post-repair this is REALIZED, not just
enforceable-by-contract: the conflict path in the draft BEFORE trigger never
raises — it writes the hold evidence, mutates NEW to the held state, runs
`visual_group_sync_row`, and RETURNs NEW, so the holds commit with the held
calendar row in the same transaction. No caller replay, no errdetail
contract. The non-raising `visual_scene_claim_decide` is the internal
function form of the same contract for claim contexts that compose their own
row write. The RAISING `visual_scene_claim_guard` (kept for hard-fail
contexts) still writes NO holds by construction — its own raise would roll
them back (even through a plpgsql EXCEPTION handler's savepoint — verified
on scratch PG); its errors carry full match detail in errdetail for
forensics.

Autonomous-transaction alternatives (a dblink self-connection writing the
hold from inside a raising path) remain REJECTED: fragile under
transaction-mode connection poolers (session state and advisory-lock
assumptions break) and they demand stored connect credentials inside the
database, which this security-definer surface must not require.

## ACTIVATION BLOCKERS (verbatim from the migration header; ALL open)

1. CALENDAR WIRING / RESOLVED-KEY ORDER. The draft trigger
   `content_calendar_scene_wave_claim_guard` fires BEFORE
   `content_calendar_visual_group_guard` (alphabetical) so a held row never
   reaches the exact-byte claim. It therefore sees `new.visual_group_key`
   BEFORE `visual_group_guard_trigger` resolves aliases, and engages only
   when the row already carries a resolved visual_group_key + post_date
   (armed tenant, active unsent row). The ACTIVATION draft MUST move the
   scene decision inside `visual_group_guard_trigger` after
   visual_group_key resolution (or add the post-resolution hook there),
   without ever creating an exact claim or occupancy for a row that ends
   held.
2. PUBLISH-CLAIM RPC WIRING. The real publish-claim RPC (PR230 path) is not
   part of this draft stack; `visual_scene_publish_claim_guarded` enforces
   the persisted-state contract (NULL for held rows) and raises 0A000 for
   the actual token mint. The activation draft must route the real RPC
   through this persisted-state check. The approval RPC must likewise
   consult persisted state (`visual_scene_approval_guarded`).
3. PUBLISH RESERVATION COLUMNS. In this draft stack the only publish
   reservation marker on content_calendar is publish_claim_token (plus
   late_post_id as provider id); no separate publish reservation column
   exists. The held mutation clears publish_claim_token. If production
   content_calendar has additional reservation columns, the activation draft
   must name and clear them explicitly — they are integration prerequisites,
   not invented here.
4. BACKFILL (still 0A000). Occupied history must be derived from the
   exact-byte ledger and attested candidates before activation. Unknown or
   source-null published history must STAY HELD for review, and the backfill
   may never mark a staged candidate as used.
5. COVERAGE REPORT + LIVE ACCEPTANCE RUNS. visual_global_coverage /
   visual_global_history_coverage must show reviewed coverage, and the
   repair-spec acceptance suite (clean claim, legal sibling, cross-date /
   cross-tenant near conflict, uncertain band, concurrent conflicting
   claims, preparation without use, denial/swap, invalid attestation,
   persisted hold, approval/claim false-success prevention, hold resolution,
   backfill with unknown history) must be run live against a disposable
   database by the independent reviewer on the exact diff.

Only after all five: flip `SCENE_GUARD_OPERATIONAL` and arm
`AGENT_VISUAL_SCENE_GUARD`. Nothing in this document certifies activation
readiness.

## Scratch-PG verification evidence (2026-10-03 / 2026-10-04)

Draft-level verification against disposable local PostgreSQL instances (never
production; the migration remains UNAPPLIED everywhere):

- 2026-10-03 (pre-repair decide/guard contract): blocked-then-committed
  claim via `visual_scene_claim_decide` leaves durable hold rows with
  `visual_scene_phash_occupied` EMPTY and candidates untouched; the raising
  path leaves ZERO holds (including through an EXCEPTION-handler savepoint);
  band behaviors confirmed (≤ 6 blocks, 7..30 blocks with holds, > 30
  distinct, same-tenant same-date same-group legal, unknown candidate
  fail-closed with no holds).
- 2026-10-04 (agent-3 scratch PostgreSQL 16 run against the repaired SQL):
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

The pg test suite (`tests/test_scene_claim_wave_pg.py`, committed-transaction
scenarios on a disposable DSN by design — Sol P1-2) is being RERUN against
the repaired SQL; treat the static SQL-text contract tests as draft pins
until that rerun lands. These runs are draft-level verification, NOT
activation acceptance — the repair-spec acceptance suite (BLOCKER 5) must
still be run live by the independent reviewer on the exact diff.

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

- The 5 ACTIVATION BLOCKERS above are all open. In particular: the trigger
  still sees pre-alias-resolution keys (BLOCKER 1), the real publish-claim /
  approval RPCs are not routed through the guarded wrappers (BLOCKER 2), and
  any production reservation columns beyond publish_claim_token are unnamed
  (BLOCKER 3).
- Sol P1-1 remains open: no Python writer calls
  `visual_scene_register_candidate`; a scene with no staged candidates is
  inert (feature OFF for that scene), and once any candidate IS staged the
  scene is fail-closed for rows lacking a bound candidate — so partial
  writer wiring changes claim behavior and must land deliberately.
- Backfill is a stub; occupied history is empty until the activation draft
  derives it from the exact-byte ledger (BLOCKER 4).
- The claim path serializes ALL scene claims fleet-wide on one advisory
  lock; throughput under concurrent calendar writes is unmeasured.
- The 7..30 band remains calibrated on exactly one measured incident pair
  (Swift River JCK_6328/JCK_6331, hamming 28) and is hold-only.
