# Scene A2 independent database authority milestone

PR #307 remains DRAFT / UNAPPLIED / OFF. Input commit `455536cf615071fc52224e199d4cd335d530fd0c`.
This is a bounded default-OFF/controlled-disarm implementation, not production activation.

## Implemented contract

- `DRAFT_visual_scene_activation_authority_20261006.sql` installs a separate canonical-tenant scene setting with `enforce NOT NULL DEFAULT FALSE`. Missing settings mean OFF. Applying it inserts no setting and arms no tenant.
- TRUE is always refused with `0A000`, including ordinary owner SQL and service-role calls. There is no activation RPC, coverage-receipt shortcut, caller GUC exception, empty-history exception or invented backfill.
- Direct service writes to settings/audit are revoked. Service role can read settings and audit and call controlled disarm; authenticated/anon/public cannot call disarm. Canonical tenant mapping is mandatory; authority cannot be reassigned, deleted or truncated.
- `visual_scene_enforcement_on` resolves the canonical tenant, holds a shared setting-row lock through calendar DML, and refuses scene-ON state if exact-byte authority is absent.
- The existing calendar BEFORE function checks that independent authority before scene scanning. Missing/OFF settings skip scene scans, scene occupancy and scene holds while preserving every exact-byte and approval/provenance predicate. A scene-ON row cannot escape by moving to a scene-OFF tenant. Disarming does not reactivate or alter held calendar rows.
- Controlled disarm requires actor/reason and a fresh writable READ COMMITTED transaction. It acquires the calendar SHARE ROW EXCLUSIVE barrier first, then auxiliary table locks and the setting-row lock with NOWAIT. Competing calendar/table/row locks refuse instead of creating a lock inversion. A same-transaction immutable disarm event authorizes ON-to-OFF; failed/rolled-back transactions leave no partial setting or audit change.
- Disarm touches no calendar row, token, occupied pHash, review hold, owner receipt, reconciliation or exact-byte ledger. Audit UPDATE/DELETE/TRUNCATE is denied.

Both authority and calendar transaction files retain the local Unix-socket / `echo_scene_ledger_test` application barrier. The occupied backfill function still raises `0A000`. No production migration, Python flag, database scene activation or paid review occurred.

## Verification

Measured boundary: 67 GiB free on `/`; reused 158M apparent task-owned checkout, existing Python environment, no new worktree/dependency installation. Updated `/Users/blakeruff/.codex/storage/fixer-echo-scene-a2-20261006.json`.

PostgreSQL 17.11 private temporary cluster, local Unix socket, no network DSN. The fixture applies group/scene prerequisites, dated capacity migrations, current approval provenance, then the new authority, then calendar composition. It rehearses authority DDL and function composition under rollback before committed scratch application.

```
ECHO_SCENE_REQUIRE_PG17=1 /Users/blakeruff/lasso-echo-work/.venv/bin/python -m pytest -q tests/test_scene_calendar_transaction_pg.py tests/test_visual_scene.py tests/test_visual_scene_candidate_staging.py
```

**Final result: 97 passed in 19.95 seconds**, including **39 disposable PG17 transaction checks**. No skipped checks. Ten existing Pillow getdata deprecation warnings arose in `agent/vision.py`. `git diff --check` passed. Existing scene-ON behavior checks use explicit synthetic replica-mode setting fixtures only in this named disposable database; no activation receipt or historical backfill is fabricated. Synthetic publication uses the real existing terminal-reconciliation RPC. The expanded reset clears those scratch reconciliation/member events so they cannot contaminate later scenarios; production immutability is unchanged.

Coverage includes default/missing OFF, exact-byte enforcement with no scene candidate, scene-nearness ignored OFF, manual proof still enforced OFF, direct/GUC arming refusal, service-role ACL, canonical identity, scene-ON destination escape refusal, scene-ON loss of exact-byte authority refusal, immutable audit, disarm commit/rollback, calendar/table/shared-row contention, published row/original token preservation, hold/occupancy retention, and the preceding scene/provenance/capacity cases.

## Remaining production blocker and next bounded package

Historical displayed/source lineage and occupied-history derivation are unresolved. The parent-provided historical audit snapshot reported 1,311 published rows, of which 787 lacked both source URL and asset ID; ten provider GETs exposed delivered URLs only, without source lineage. Those counts are supplied audit evidence, not newly verified live results in this worker task. A delivered URL alone does not prove source ancestry or authorize a historical scene exemption.

The next owner must recover or explicitly hold unknown historical source/display lineage, bind owner-computed pHashes to verified displayed bytes, derive permanent occupancy from the authoritative exact-byte ledger/calendar/provider reconciliation state, and prove global coverage without inventing source aliases or counting staged candidates as historical uses. Activation must take a writer barrier and auxiliary locks in the established order, verify coverage under that barrier, record a fresh immutable activation receipt, and arm the independent setting in the same transaction. Only that independently accepted package may replace unconditional arming refusal and the scratch-only production barrier.

Rollback plan: controlled disarm first; atomically restore the group-only BEFORE function and the current provenance RPC definitions. Retain scene setting/audit, hold/occupancy, receipt and reconciliation evidence. Do not delete history, unhold rows, change original tokens or remove exact-byte authority. Transaction rollback is proven locally; production rollback/activation requires its own reviewed deployment record.
