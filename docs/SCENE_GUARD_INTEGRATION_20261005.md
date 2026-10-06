# Scene Guard Integration — 2026-10-05 (DRAFT / OFF)

Bounded integration milestone: ported the reviewed, minimal scene-similarity
claim-prevention work from draft PR #268 (`codex/echo-phash-global-20261003`,
commit `73b39c2`) onto current `origin/main` (`fa81d1d`) via two child
worktrees. The draft PR #268 branch is preserved untouched.

## What landed (all OFF by default, nothing applied)

- `agent/visual_scene.py` — `scene:phash64:` similarity namespace; fail-closed
  (unknown/undecodable evidence is never DISTINCT).
- `agent/gym_media_selector.py` — tri-state `AGENT_VISUAL_SCENE_GUARD` gate in
  `pickable()`: cross-tenant NEAR_FRAME excludes; SCENE_CANDIDATE / same-tenant
  cross-date near frames become durable `scene_review_hold` candidates
  (surfaced, never auto-approved). Unknown inventory HOLDS.
  `SCENE_GUARD_OPERATIONAL = False` — even armed, the guard is not operational.
- `agent/visual_writer_prepare.py` — advisory `scene_fingerprint` evidence and
  the OFF-default `AGENT_VISUAL_SCENE_CANDIDATE` staging payload
  (`usage_claimed: False`, `counts_as_use: False`).
- `agent/config.py` — `visual_scene_guard_flag()` /
  `visual_scene_candidate_flag()` tri-state readers (ambiguous = fail closed).
- `migrations/DRAFT_visual_scene_ledger_20261005.sql` — minimal durable
  tenant-scoped used-media/variant claim prevention (candidate staging,
  occupied ledger, review holds, claim scan/decide/guard, hold resolve).
  DRAFT/UNAPPLIED; rollback-safe (verified by disposable-PG rollback proof).
- Tests: `test_visual_scene.py`, `test_visual_writer_prepare_scene.py`,
  `test_gym_media_scene_guard.py`, `test_scene_ledger_claim_pg.py`,
  `test_scene_ledger_rollback_pg.py`.

Deliberately NOT ported: the rejected prep-time sketch
`DRAFT_visual_scene_phash_20261003.sql`, `agent/visual_scene_register.py` and
the `portal_calendar_store.py` scene-patch route (depend on unapplied draft
RPCs), the calendar-trigger integration, and the 0A000 backfill stub
(backfill remains unimplemented by design).

## Preserved main behavior

PR #285 (cross-gym Drive source guard), #287 (source-URL clearing on explicit
photo swaps), #288 (FIXER receipt fencing) verified passing post-merge.

## Release blockers (result stays DRAFT/OFF)

1. Scene ledger migration is unapplied; requires the activation draft
   (hold_reactivate, publish_claim_guarded, approval_guarded, calendar
   trigger wiring) before any enforcement.
2. Historical source provenance for the 787 published rows is unknown — no
   historical clearance was granted and no bytes invented; old approved
   photos are NOT quarantined.
3. Backfill of occupied scenes is a fail-closed 0A000 stub pending provenance
   evidence.
4. `SCENE_GUARD_OPERATIONAL = False` until activation blockers resolve.
