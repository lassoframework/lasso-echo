# Test-only frozen snapshot — portal migration 0611 (HELD, not deployed)

File: `portal_0611_echo_source_brand_bundle_held_20261008.sql`

This is an exact byte-for-byte test fixture snapshot of the portal
migration `supabase/migrations/0611_echo_source_brand_bundle.sql`, copied
from the sibling checkout `portal-brand-source-bundle-20261008`
(portal commit `ad3feba3` at copy time, 2026-10-09).

- SHA-256: `d3a3b22d62f2ebc0376f89552a994d785947eb5309dff6e3d655ebf4c0f71178`
- Purpose: let the composed PG test
  `tests/test_gbp_staged_drive_recovery_composed_pg.py` run in ordinary
  Echo CI without a sibling portal checkout.
- Status of the real portal migration: HELD pending the normal portal
  migration gate. This fixture does NOT mean 0611 is deployed anywhere,
  and it must never be referenced by production code or applied as a
  production migration. It is disposable-PG test evidence only.
- If the real portal migration changes, regenerate this fixture from the
  updated reviewed portal file and update the hash assertion in the test.
