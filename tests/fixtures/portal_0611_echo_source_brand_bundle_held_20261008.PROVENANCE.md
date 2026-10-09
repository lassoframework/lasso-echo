# Test-only frozen snapshot — portal migration 0611 (HELD, not deployed)

File: `portal_0611_echo_source_brand_bundle_held_20261008.sql`

This is an exact byte-for-byte test fixture snapshot of the portal
migration `supabase/migrations/0611_echo_source_brand_bundle.sql`, copied
from the sibling checkout `portal-brand-source-bundle-20261008`
(portal git HEAD `82c174c9` at copy time, 2026-10-08).

- SHA-256: `5277e3d1192a56454f4a3233fb87756a24b1977ded898649c55883d06e1eb8ef`
- Purpose: let the composed PG test
  `tests/test_gbp_staged_drive_recovery_composed_pg.py` run in ordinary
  Echo CI without a sibling portal checkout.
- Status of the real portal migration: HELD pending the normal portal
  migration gate. This fixture does NOT mean 0611 is deployed anywhere,
  and it must never be referenced by production code or applied as a
  production migration. It is disposable-PG test evidence only.
- If the real portal migration changes, regenerate this fixture from the
  updated reviewed portal file and update the hash assertion in the test.
