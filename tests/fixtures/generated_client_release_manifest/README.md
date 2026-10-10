# Full generated client release manifest fixtures

These fixtures are exact-byte copies of the reviewed source artifacts used by
`tests/test_generated_client_release_manifest_pg.py`. The accepted seed module
is copied as well so this integration test has no dependency on sibling
worktrees or the root evidence directory. SHA-256 values below are checked by
the test before execution.

| Fixture | Source provenance | SHA-256 |
| --- | --- | --- |
| `../portal_0611_echo_source_brand_bundle_held_20261008.sql` | `portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql`; portal owner-frozen bytes at worktree HEAD `eb60d15b52cfc993c27d85b4de23cbca44d77b3a` | `55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2` |
| `0611_echo_source_brand_bundle.verify.sql` | `portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.verify.sql`; portal owner-frozen bytes at worktree HEAD `eb60d15b52cfc993c27d85b4de23cbca44d77b3a` | `4e99445cf04fff7046d8da64d13e02eb5ca875a7cc988c64cd9ad0e08a67063f` |
| `DRAFT_0625_generated_client_approval.sql` | `portal-generated-client-contract-20261009/supabase/migrations/DRAFT_0625_generated_client_approval.sql` | `d54a5b9e1f857d31912540f38f626b59c891296a4aebd7b3c2d55a1f4abdc705` |
| `DRAFT_0625_generated_client_approval.verify.sql` | `portal-generated-client-contract-20261009/supabase/migrations/DRAFT_0625_generated_client_approval.verify.sql` | `19fd8aa6b031bd9fd80aa70a64b34b737daf1ae02d03d0e04bfa6d3bf4953ee7` |
| `generated_client_seed_20261009.py` | `evidence/generated_client_seed_20261009.py` accepted generated-client release seed module | `d00b1a98c4fe413f84c859ac664682aa61fddb2a32c3d5d5a59f77b55969a4e8` |

The 0611 apply SQL is not duplicated because the existing held fixture is an
exact-byte match. PostgreSQL server binaries must be major version 17. The test
auto-discovers common installations or accepts `PG17_BIN` / `POSTGRESQL_17_BIN`;
it skips only when no usable PostgreSQL 17 installation is available. The
repository's main-target CI installs PostgreSQL 17 and Python test dependencies
before running this manifest check.
