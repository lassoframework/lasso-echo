# Scene history preflight milestone

PR #307 remains DRAFT / UNAPPLIED / OFF. Input head `59f663b4b9b3749e75958074812a857214ac039b`, based on main `a1615405dde8047066ccf8adfced3531ba54028b`.

## Gap and bounded implementation

The exact-byte stack already exposes calendar and orphan-history coverage. Scene occupied backfill still raises `0A000`, and the separate default-OFF authority refuses every activation. The missing diagnostic was a fleet scene-history report identifying why unknown source history or unoccupied owner pHash evidence cannot support clearance.

`DRAFT_visual_scene_history_preflight_20261006.sql` adds one read-only, service-role diagnostic function. Its scratch application barrier requires the named local Unix-socket disposable database. The report never writes settings, calendar rows, candidates, occupancy, holds, imports, review decisions or activation receipts. `activation_available` and `clearance_authorized` are always false. `coverage_complete` describes only this statement's diagnostic snapshot, cannot authorize activation, and is false for an empty fleet.

The fleet report includes every relevant active, published or ambiguous calendar row, retaining archived published history; existing exact-byte coverage issues; source-null historical review blockers; the actual displayed image or distinct poster bound to an immutable owner pHash receipt and matching byte attestation; unknown usage dates; missing permanent displayed-object occupancy; every permanent exact-byte member including orphan and released members; occupied rows without permanent exact-byte members; open scene review holds; and full occupied-pair comparisons across tenants and dates. It uses admission's Hamming bands of 0 through 6 for near frames and 7 through 30 for uncertain scenes. Only same tenant, same group, same date siblings are exempt. Approved pair holds cannot erase a fleet history conflict.

Tenant counts report calendar rows, unknown source rows, missing displayed owner evidence, permanent members, retained released members and orphan members. Unmapped calendar identity retains its null-tenant diagnostic instead of being silently dropped.

## Independent review repairs

Independent Luna review requested a hardened SECURITY DEFINER path and mixed photo/poster coverage. The new function now sets `search_path=pg_catalog,public,pg_temp`, explicitly putting temporary schema lookup last; persistent relations and called functions are also schema-qualified. An adversarial service-role check creates nine same-named temporary history relations and proves calendar/member counts, issues and coverage still come from persistent history.

The existing A2 `visual_scene_row_delivered_object` contract selects a distinct stored poster, even when a source photo coexists. That contract cannot prove provider delivery precedence. This preflight therefore adds `delivery_precedence_review_required` for every mixed image/distinct-poster row rather than silently assuming photo-first or treating poster evidence as clearance. A focused fixture includes a verified source photo, attested distinct poster, and repeated source-photo conflict across tenants/dates. It preserves the conflict, verifies the poster binding exists, and blocks clearance. Existing A2 object selection is unchanged. Provider lineage/precedence acceptance remains a separate unresolved task.

## Verification

No fresh worktree or dependencies. Existing Python environment and private PostgreSQL 17 fixture reused. Measured free space at final repair boundary: 45 GiB on `/`. Actual one native worker; Kimi unavailable under parent's quota-403 ruling. Usage cost unavailable.

```
ECHO_SCENE_REQUIRE_PG17=1 /Users/blakeruff/lasso-echo-work/.venv/bin/python -m pytest -q tests/test_scene_history_preflight_pg.py tests/test_scene_calendar_transaction_pg.py tests/test_visual_scene.py tests/test_visual_scene_candidate_staging.py
```

**Final repaired run: 119 passed in 45.07 seconds**, no skips. Includes 22 new disposable preflight cases and 39 existing PG17 calendar transaction cases. Ten existing Pillow `getdata` deprecation warnings. Tests cover unknown archived published/ambiguous source, missing poster proof, default OFF and candidate-is-not-use, orphan/released/published members, unknown dates, cross-client same-date conflicts, cross-date near/uncertain boundaries, legal siblings, the distinct-band boundary, occupied orphan refusal, read-only service execution, adversarial temporary tables, mixed photo/poster conflict and precedence refusal, ACL, wrong-database refusal and non-activation even on a complete synthetic snapshot. Earlier pre-review run passed 117 checks in 48.08 seconds. `git diff --check` passed.

The complete-snapshot fixture explicitly inserts synthetic occupancy only in the named disposable server; this is not historical proof or a backfill implementation. Tests never supply a production DSN. The first repair run found one fixture ordering error: second-tenant synthetic group activation correctly refused the intentionally incomplete mixed-object history. Seeding that tenant before creating the incomplete history repaired the fixture; no enforcement was weakened.

## Remaining production blockers

Historical source/display lineage recovery, independently verified occupied backfill, barrier-protected final coverage and transactional activation remain missing. This diagnostic deliberately applies conservative member coverage: a source-only byte member without matching verified displayed lineage remains blocked rather than receiving invented pHash or an exemption. A later accepted backfill must establish the correct lineage-derived permanent occupancy.

The snapshot takes no writer barrier and can become stale immediately. It must never be replayed as a clearance receipt. Full pair scanning is quadratic in occupied history and needs production-scale performance acceptance before any authorized rollout. Existing A2 SQL, authority refusal, scratch barriers and default OFF flags are unchanged. No production SQL, activation, deployment, PR-ready transition or merge occurred. Parent independent acceptance remains required.
