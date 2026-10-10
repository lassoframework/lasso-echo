# Test-only frozen snapshot — portal migration 0611 (HELD, not deployed)

File: `portal_0611_echo_source_brand_bundle_held_20261008.sql`

This is an exact byte-for-byte test fixture snapshot of the portal
migration `supabase/migrations/0611_echo_source_brand_bundle.sql`, copied
from the sibling checkout `portal-brand-source-bundle-20261008`, with the
portal owner-frozen bytes verified at worktree HEAD
`eb60d15b52cfc993c27d85b4de23cbca44d77b3a` on 2026-10-09. The source checkout
had other unrelated local changes; this fixture is pinned to the exact file
hash below, not to the checkout commit.

- SHA-256: `55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2`
- Paired verifier `tests/fixtures/generated_client_release_manifest/0611_echo_source_brand_bundle.verify.sql` SHA-256: `4e99445cf04fff7046d8da64d13e02eb5ca875a7cc988c64cd9ad0e08a67063f`
- Purpose: let the composed PG test
  `tests/test_gbp_staged_drive_recovery_composed_pg.py` run in ordinary
  Echo CI without a sibling portal checkout.
- Status of the real portal migration: HELD pending the normal portal
  migration gate. This fixture does NOT mean 0611 is deployed anywhere,
  and it must never be referenced by production code or applied as a
  production migration. It is disposable-PG test evidence only.
- If the real portal migration changes, regenerate both fixtures from the
  updated reviewed portal files and update the hash assertions in the tests.
