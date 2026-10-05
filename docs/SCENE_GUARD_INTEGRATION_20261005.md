# Scene Guard Integration — 2026-10-05 (DRAFT / OFF)

This is a code-and-disposable-database milestone on `codex/echo-phash-global-integration-20261005` at parent HEAD `589cc84`. It is not an applied migration, an armed guard, a production rollout, or release acceptance. The original draft PR #268 branch remains separate.

## Current implementation

- `agent/visual_scene.py` defines the `scene:phash64:` namespace and fail-closed perceptual-hash classification. Unknown or undecodable evidence is never treated as DISTINCT.
- `agent/gym_media_selector.py` has the tri-state `AGENT_VISUAL_SCENE_GUARD` selection gate. When the code path is explicitly enabled, cross-tenant `NEAR_FRAME` is excluded; same-tenant cross-date near frames and `SCENE_CANDIDATE` become review holds; unknown inventory holds. `SCENE_GUARD_OPERATIONAL = False` still prevents operational use.
- `agent/visual_writer_prepare.py` adds scene fingerprints and an OFF-default `AGENT_VISUAL_SCENE_CANDIDATE` payload. When the scene guard is explicitly armed, preparation obtains an immutable owner pHash receipt for the exact displayed bytes and calls `visual_scene_register_candidate()`; the payload says `usage_claimed: false` and `counts_as_use: false`, so staging is not use. Local tests cover receipt production and candidate registration. This is not evidence that the owner receipt runtime is provisioned in production.
- `agent/config.py` supplies tri-state flag readers. Ambiguous values fail closed.
- `migrations/DRAFT_visual_scene_ledger_20261005.sql` defines candidate staging, permanent occupied-scene evidence, review holds, claim scan/decision/guard and hold resolution. Its `visual_scene_backfill_occupied()` remains a stub that raises `0A000`.
- `migrations/DRAFT_visual_scene_calendar_transaction_20261005.sql` now replaces the existing `visual_group_guard_trigger()` path so the calendar row, scene decision, review hold, and exact-byte ledger participate in the same transaction. A conflict commits the row as pending/archived with `scene_review_hold`, with claim fields cleared; a clean claim records occupancy in that transaction. The SQL does not arm settings or backfill history. The legacy publish claim RPC alone is not release-safe: callers must verify the persisted row before sending.
- Tests include scene classification, selector behavior, writer preparation, scene-ledger PG claim/rollback, and the new transaction PG17 fixture.

## Exact disposable-PG test order

The local PostgreSQL 17 transaction fixture builds this draft stack, in this exact order:

1. `migrations/DRAFT_visual_group_schema_20261002.sql`
2. `migrations/DRAFT_visual_group_claim_trigger_20261002.sql`
3. `migrations/DRAFT_visual_global_history_20261002.sql`
4. `migrations/DRAFT_visual_group_backfill_20261002.sql`
5. `migrations/DRAFT_visual_group_activation_20261002.sql`
6. `migrations/DRAFT_visual_scene_ledger_20261005.sql`
7. `migrations/DRAFT_visual_scene_calendar_transaction_20261005.sql`

This is the fixture setup in `tests/test_scene_ledger_claim_pg.py` plus the ledger and transaction SQL applied by `tests/test_scene_calendar_transaction_pg.py`. It tests SQL behavior on a private disposable local PG17 cluster. The activation *draft* is installed in that fixture, but the fixture does not arm a tenant or establish a production migration history. No item in this list has thereby been applied to production.

The separate rollback fixture tests reverse removal of the scene-ledger draft before any activation. The calendar-transaction SQL header documents scratch-only rollback by restoring the prior group trigger and RPC definitions. Permanent occupancy and review evidence must be preserved after real activation; destructive rollback is not a production recovery plan.

## Still incomplete

- **Owner provisioning and live candidate flow remain unverified.** The local runtime path now creates an owner-computed pHash receipt for the exact displayed object and submits it to `visual_scene_register_candidate()` when the scene guard is explicitly armed. The receipt producer requires its dedicated owner database role/configuration; its production provisioning and deployment have not been verified. With flags at their defaults, candidate emission/registration is OFF. Do not infer a live row-bound candidate from the local tests.
- **Historical occupied-scene backfill is unimplemented.** `visual_scene_backfill_occupied()` raises `0A000`; the transaction migration does not change that. The wider visual-group backfill is also a draft operation and is not historical scene coverage. No historical clearance or scene occupancy has been asserted for historical published rows; do not quarantine or silently clear old approved photos based on this draft.
- **Activation/arming is unresolved.** All migrations remain `DRAFT_` and unapplied; `AGENT_VISUAL_SCENE_GUARD` and `AGENT_VISUAL_SCENE_CANDIDATE` default OFF; and `SCENE_GUARD_OPERATIONAL` is hard-coded false. The new calendar transaction draft explicitly adds no arming barrier. A safe activation path needs evidence-backed coverage, provisioned owner receipts and candidate staging, implemented historical scene backfill, and an activation/arming design that verifies the coverage atomically. The existing group activation draft alone does not satisfy those scene-specific gaps.
- **End-to-end caller behavior is not certified.** The transaction fixture covers IG/GBP claims, approval filtering, direct-patch holds, clean occupancy and rollback in scratch PG17. It does not prove every live caller checks the persisted row after a trigger turns a would-be claim into a held row, nor does it verify provider sends or deployed behavior.
- **Release review is outstanding.** Independent acceptance and the required cloud Anthropic Ultra Review of frozen SHAs have not been recorded here. No production readiness claim is made.

## Preserved behavior and release state

PR #285 (cross-gym Drive source guard), #287 (source-URL clearing on explicit photo swaps), and #288 (FIXER receipt fencing) were recorded as passing after merge in the preceding milestone. That prior evidence is not reissued or revalidated by this document update.

**State: DRAFT / OFF / UNAPPLIED.** The code and local PG17 fixtures exercise owner-receipt candidate registration and a candidate calendar transaction design. They do not establish owner-runtime provisioning, historical coverage, an armed guard, production application, or permission to publish.
