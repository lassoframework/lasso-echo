# Scene A2 SQL provenance repair

Recorded 2026-10-06 UTC. PR #307, branch `codex/echo-scene-a2-review-20261006`.
Input commit `d3313173076d300339f0281456add03334c2d90d`.
Status: bounded draft repair; unapplied; no production activation or no-repeat completion claim.

## Repair

The scene transaction draft now composes with `calendar_approval_provenance_20261005.sql`, rather than recreating obsolete six-argument claim and two-argument approval RPCs. It drops obsolete signatures and exposes one defaulted seven-argument claim and one defaulted three-argument approval. Service-role grants and public/anon/authenticated revocations are retained.

The claim predicates before its final UPDATE are exactly the authoritative source predicates. This preserves current DB autonomy resolution, locked human proof/digest checks, stale reservation/token rejection, third Story capacity and dated LASSO 5/15 capacity envelopes. Approval retains the exact expected-card comparison and clears actor provenance while recording the digest; human identity remains the separate trusted stamp.

UPDATE RETURNING checks the row after the BEFORE trigger. A scene hold commits without returning a claim UUID, approval row, GBP publish snapshot or successful human-proof stamp. Holds clear approval kind, actor, time and digest, as well as claim token/reservation. Existing reconciliation protections remain in place.

The draft refuses application outside the explicitly named `echo_scene_ledger_test` database over a local Unix socket. This is a scratch-only barrier, not the missing production scene activation design. Python's OFF flag does not control the database trigger, so the draft must not be applied to an already armed production exact-byte tenant.

## Evidence

Measured disk headroom before local cluster work: 66 GiB available on `/`.
Runtime: PostgreSQL 17.11 (Homebrew), private temporary Unix socket, no supplied network DSN.

Command:

```
ECHO_SCENE_REQUIRE_PG17=1 /Users/blakeruff/lasso-echo-work/.venv/bin/python -m pytest -q tests/test_scene_calendar_transaction_pg.py tests/test_visual_scene.py tests/test_visual_scene_candidate_staging.py
```

Result: **86 passed in 14.60 seconds**, including **28 disposable PG17 transaction checks**. Ten existing Pillow getdata deprecation warnings arose in `agent/vision.py`; no test failures/skips. `git diff --check` passed.

The fixture applies real dated capacity migrations and then authoritative approval provenance before this draft. It rehearses the complete transaction under rollback and compares all public function bodies before and after. Tests cover absent-provenance order rejection, wrong-database application rejection without mutations, single RPC signatures/defaults/ACL, exact expected-card rejection, proof reset, manual stale-digest rejection, proof-gated claim hold durability, human-stamp hold filtering, GBP mode-snapshot refusal, no-token hold behavior, stale reservations, legal siblings, concurrency, ambiguous attempt protection, and 3 Story / 5 (3 current + 2 backlog) / 15 (3 current + 12 backlog) capacity with scene guards active.

No SQL applied to production. No feature flag enabled. No paid review run. Native parent review is still required for acceptance; this receipt is worker evidence.

## Smallest next production package

One SQL owner should implement and independently review these coupled requirements before replacing the scratch-only application barrier:

1. **Default-OFF database activation barrier.** Separate scene authority from exact-byte enforcement. Installing the production replacement must leave existing exact-byte behavior unchanged for scene-OFF tenants. Scene-ON must fail closed for unknown or held identity, without depending on a Python caller flag. Test every authoritative claim/approval/direct UPDATE lane, including GBP, and mode/activation races.
2. **Verified historical occupied backfill.** Replace the currently raising occupied-history stub with an auditable backfill derived from the authoritative calendar and provider/reconciliation state, bound to verified displayed-byte fingerprints and owner pHash receipts. Include published history, active reservations and ambiguous attempts. Unknown coverage must block activation. Lock/serialize admission with the backfill and require coverage manifests that match the frozen import; no gap between final coverage check and arming.
3. **Activation and rollback proof.** Test OFF install, coverage rejection, ON admission and cross-tenant concurrency on disposable PG17. Preserve exact-byte authority, provenance, permanent occupancy/hold history, ambiguous sends and original tokens. Rollback restores the group-only trigger and all four preceding provenance RPC definitions atomically; it must not reactivate held calendar rows, erase hold evidence or issue new tokens. Record actual production before/after state only after separately authorized deployment.

Historical backfill, database activation and independent production acceptance remain unresolved. Merging this draft repair does not activate scene no-repeat protection globally.
