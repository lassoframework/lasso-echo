# Global pHash scene-similarity guard — DRAFT / UNAPPLIED / INCOMPLETE

## STATUS: INCOMPLETE — DO NOT APPLY, DO NOT ACTIVATE (Astra review 2026-10-03; claim-wave DRAFT landed 2026-10-03, still OFF)

The prep-time writer architecture this package was drafted for was **rejected
by Astra's review**. There is NO runtime scene writer and NO operational gate.
Nothing here claims global near-duplicate enforcement: the exact-byte PR235
behavior (`visual_global_usage`, md5-keyed, frozen contract) is unchanged and
remains the only enforcement layer. This document is the honest record of
what exists, why the writer was rejected, and what a redesign requires.

### Why the prep-time writer was rejected

Recording a scene at preparation time:

1. marks UNUSED candidates as used — preparation is not a usage decision;
2. is not atomic with the real `visual_global_usage` claim, which happens
   later in the calendar-trigger `visual_global_claim_scene` transaction; and
3. still races across gyms (two tenants preparing concurrently each see a
   table without the other's uncommitted record).

Durable review holds were also missing: a hold existed only as a log line.

### What exists (all OFF, none operational)

- `agent/visual_scene.py` — pure module: `scene_fingerprint(bytes)` →
  `scene:phash64:<16 hex>` or None (never raises, never invents an id);
  `normalize_scene` (namespaced-only); `hamming_distance` (999 = no evidence);
  `classify_scene` (nearest-only) and `classify_scene_all` (EVERY ≤6 and
  7..30 match, (distance, phash)-sorted, worst-case band as kind). Bands:
  near_frame ≤ 6 (§3 burst cluster radius), scene_candidate 7..30 — calibrated
  on exactly ONE measured incident pair (Swift River JCK_6328/JCK_6331,
  hamming 28), hold-only — distinct > 30, unknown = fail closed, never
  distinct.
- `agent/config.py` — `AGENT_VISUAL_SCENE_GUARD`, OFF by default, tri-state
  read (ambiguous value counts as armed-fail-closed).
- `agent/visual_writer_prepare.py` — advisory `scene_fingerprint` evidence
  field alongside the md5 identity. It NEVER gates and NEVER raises:
  undecodable bytes record null evidence; no p_scene_* payload, no scene RPC,
  no post_date requirement in any flag state.
- `agent/gym_media_selector.py` — read machinery intact but UNREACHABLE:
  `SCENE_GUARD_OPERATIONAL = False`, so an armed flag raises
  `SceneLedgerUnavailable("...not operational...")` before any scan
  (strict_claims propagates; non-strict returns []). The machinery kept for
  the redesign: unfiltered full-table paginated scan (a one-bit-away phash
  must be caught — no exact-match filter is possible), pinned
  full-primary-key ordering with row-over-row verification, Content-Range
  completeness proofs, mid-scan change detection, bare-phash / tenant-UUID /
  group_key / used_date row validation, and the all-matches policy inputs
  (cross-tenant near ⇒ exclude; same-tenant cross-date near ⇒ hold; any 7..30
  candidate ⇒ hold).
- `migrations/DRAFT_visual_scene_phash_20261003.sql` — design sketch only.
  Its header carries the same STATUS; the two record functions
  (`visual_scene_record_use`, `visual_scene_record_use_tx`) exist but their
  bodies RAISE 0A000 — no working writer even if applied. The table sketch
  (`phash char(16)`, `tenant_id`, `group_key`, `used_date date NOT NULL`,
  PK on phash+tenant+group, `(tenant_id, used_date)` index, immutability
  trigger, SELECT-only service-role grant) is the shape a redesign starts
  from. `migrations/DRAFT_visual_global_history_20261002.sql` is reverted to
  base: prepare RPCs have their original 6-arg signatures and no scene
  surface.

### Required redesign (mirrors the migration STATUS section)

(a) A candidate/staging table written at prep that does NOT count as used —
    preparation evidence must never consume a scene.
(b) Scene recording and the occupied-scene/dates comparison moved INTO the
    claim transaction (the `visual_global_claim_scene` path) under its
    existing locks, owner-attested with the exact object bytes in the same
    call that establishes byte authority.
(c) A durable review-hold table written atomically in that same claim
    transaction whenever a conflict is found.
(d) Hamming comparison of ALL occupied scenes/dates enforced transactionally,
    server-side, never by the client (today's client-side full-table scan is
    O(table) per pick and has no server-side hamming index).
(e) Full backfill before any activation.

Until (a)–(e) land, `SCENE_GUARD_OPERATIONAL` stays False and
`AGENT_VISUAL_SCENE_GUARD` armed means fail-closed, not enforcement. pHash
remains similarity evidence only: it never auto-approves and never writes
`visual_group_scene_link`, which stays human-confirmed via
`visual_group_link_scene`.

### Claim-wave DRAFT (2026-10-03; P0 repair pass 2026-10-04) — partial landing, still OFF

A draft of the (a)–(d) redesign exists, UNAPPLIED and OFF, repaired against
Sol's independent audit (`SCENE_AUDIT_GAPS.md`, 2026-10-04 — verdict: safe
only as an inert DRAFT/OFF branch; production activation rejected;
acceptance boundary `SCENE_ACTIVATION_REPAIR_SPEC.md`).
`migrations/DRAFT_visual_scene_claim_wave_20261003.sql` adds
`visual_scene_candidate` staging bound to the calendar row's exact DELIVERED
object via `object_role`/`exact_url`/`fingerprint` (a; never counts as used;
Sol P0-1 fixed), PERMANENT `visual_scene_phash_occupied` written only inside
the claim path on a clean decision (b; never freed on release/denial/swap,
mirroring the exact-byte ledger), `visual_scene_review_hold` with a stable
open-hold uniqueness key so retries insert no duplicate (c; Sol P0-3 fixed),
server-side `visual_scene_hamming` over the ENTIRE occupied table inside the
claim path under the fleet-wide advisory lock taken after the existing
scene-component locks (d; Sol P0-8 drafted), and a backfill STUB raising
0A000 (e — still an activation blocker, Sol P0-6).

The audit's central P0s are addressed in the draft:

- HOLD-ROLLBACK (the original Astra P0 — the old guard inserted holds and
  then RAISED, so rollback erased them) is RESOLVED and REALIZED: a hold
  exists IF AND ONLY IF the claim transaction commits in a blocked/held
  state. The DRAFT calendar BEFORE trigger
  `visual_scene_calendar_claim_guard` never raises on a scene conflict — it
  writes idempotent holds, mutates NEW to the held state
  (`variant_status='archived'`, `status='pending'`,
  `media_not_ready_reason='scene_review_hold'`, publish_claim_token
  cleared), still passes NEW through `visual_group_sync_row` (old active
  local membership released; no exact claim, no pHash occupancy for the held
  row), and RETURNs NEW so the held row commits WITH its holds (Sol P0-4
  drafted). The non-raising `visual_scene_claim_decide` is the internal
  function form; the RAISING `visual_scene_claim_guard` writes NO holds by
  construction. Autonomous-transaction (dblink) alternatives remain
  REJECTED (pooler fragility, stored credentials in the database).
- EXECUTE on all claim-path SECURITY DEFINER functions (scan/decide/guard/
  row_candidate/write_holds/calendar-trigger/backfill) is revoked from
  public/anon/authenticated AND service_role — trigger/internal only (Sol
  P0-2 fixed). The only service_role entry points are the safe read/review
  surfaces that cannot fabricate occupancy or holds.
- Guarded caller contracts drafted (Sol P0-5):
  `visual_scene_publish_claim_guarded` returns NULL for a held row (raises
  0A000 for the real token mint — PR230 routing is activation work) and
  `visual_scene_approval_guarded` never approves a held row; both read
  PERSISTED state.
- Hold review path drafted (Sol P0-7): `visual_scene_hold_resolve`
  (validated actor + evidence, current-scene re-scan under the fleet lock,
  conflict-scope drift refuses resolution; an approval exempts ONLY the
  exact reviewed conflict pair for its exact claim date) and
  `visual_scene_hold_reactivate` (approved-only, fresh bound-candidate
  re-scan must be clean, refuses while still blocked).

STATUS remains honest: this feature is DRAFT/UNAPPLIED/OFF/INCOMPLETE. Five
ACTIVATION BLOCKERS are named verbatim in the migration header and in
`docs/SCENE_CLAIM_WAVE.md`: (1) calendar wiring / resolved-key order (the
draft trigger sees pre-alias-resolution keys; the scene decision must move
inside `visual_group_guard_trigger` post-resolution), (2) real publish-claim
RPC (PR230) routing through the persisted-state guard, (3) naming/clearing
any production publish-reservation columns beyond publish_claim_token, (4)
backfill (still 0A000; unknown or source-null published history stays HELD),
(5) coverage report + live acceptance runs of the repair-spec suite by the
independent reviewer. Sol P1-1 is still open: no Python writer calls
`visual_scene_register_candidate`. Scratch-PG evidence (disposable instances,
2026-10-03/2026-10-04, draft-level not acceptance): blocked-committed holds
persist with occupied empty and candidates untouched; the raising path
leaves zero holds; the repaired trigger commits the held row state;
uniqueness retries insert no duplicate; service_role is permission-denied on
the claim path; held rows get a NULL token; resolve/reactivate behave;
occupancy persists across denial/swap. Test fixtures must use Astra's
deterministic Walsh–Hadamard codebook (64-bit words, parity(i&j), pairwise
Hamming 32, `'016x'` format) — no random or impossible-min-distance
codebooks. Full details, exact migration order, dispositions, activation
blockers, evidence, and rollback steps: `docs/SCENE_CLAIM_WAVE.md`. Contract
tests: `tests/test_scene_claim_wave_*.py` (pg scenarios run committed
transactions on a disposable local DSN by design; the suite is being rerun
against the repaired SQL).
