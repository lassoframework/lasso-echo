# Generated client release manifest candidate — 2026-10-09

**Image release state: DRAFT / UNAPPLIED / NOT ACCEPTED.** Portal 0611 has
subsequently been applied; its current read-only ledger receipt is below.
This is a frozen review packet
derived from the PG17 integration tests, not a migration script or deployment
authorization. No production database mutations were made by this packet and no client/provider
outcome is established here. All admission, inventory, remote-drive and
calendar-admission controls remain default-OFF. The dedicated issuer entrypoint
is not provisioned: there are no production LOGINs, tenant grants, secrets,
service configuration or invocation in this packet. The SQL creates only
unprivileged `NOLOGIN` group roles; it does not provision credentials.

## Historical preflight discovery — 2026-10-09 15:52 UTC

**Production-shaped install/reconciliation rehearsal: BLOCKED.** The initial
milestone stopped at a verified whole-file source conflict. Its three changes
were an exact-file read-only diagnostic, focused static/stub checks, and this
record. The compatible overlay checkpoint below supersedes this source-conflict
finding. Neither checkpoint has an apply path or production-shaped PG fixture.
Existing full-manifest tests and their synthetic seed remain separate evidence.

Frozen task HEAD before these uncommitted edits:
`62ea62666363577a54a4c13d36e4b51894e27a8c` (Echo #385). The earlier checkout
identity and exact test install sequence below remain historical local proof.
That sequence is not an approved production installation plan.

The fresh catalog/ledger query ran inside `BEGIN READ ONLY` / `ROLLBACK` on
project `ooqcvmcjspeltuuhcvlh` at `2026-10-09T15:52:12.764759Z`. It read metadata
and migration receipts only. No customer rows, provider calls, flags, roles,
production SQL installation or deployments were changed. The frozen receipt
is reproduced below; CLI-supplied offline receipts require independent
authenticity and freshness verification.

| Scope | Read-only finding |
|---|---|
| Production identity | PostgreSQL 17.6, postgres, system identifier `7642734024280108049` |
| Portal 0611 | Already applied in both ledgers, version `20261009143836`, exact SQL SHA `55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2`; this diagnostic does not repeat the full 0611 catalog verifier |
| Portal 0625 | Pending: no matching receipt in either ledger and no 0625 relation/function/column remnants |
| Current claim function | Exact live body MD5 `c624eedcee819496129639108be991f6`, postgres owner, SECURITY DEFINER, `search_path=public`, ACL only postgres and service_role EXECUTE |
| Calendar entry prerequisite | Drift: expects older body MD5 `db59bf4d6d0be4c42e5e49ab0b5a8b4f` |
| Atomic cutover prerequisite | Pins the older body with lock entry added, MD5 `7c09844f2c8c00190e007b1bca83d2a0` |

The live claim body matches the complete checked-in
`migrations/lasso_october7_catchup_capacity_20261008.sql`, SHA
`c8f1c4c3af064e17df3670252153b2698a64ec607271127093d0f9aaf1d26927`.
Its final ACL statements also match the live explicit ACL. This file preserves
the dated capacity-six October 7 catchup behavior. It is absent from the
historical manifest sequence. The public migration ledger has no matching
filename receipt for this file or the three historical capacity/provenance
foundations queried; absence of a filename receipt is not evidence that their
catalog effects are pending.

Reapplying `calendar_approval_provenance_20261005.sql` would overwrite the live
claim with the older body and make the calendar-entry guard pass by erasing
current behavior. The entry overlay also omits capacity six. Reapplying the
matching October 7 source after entry would erase the required G/C entry call.
No later whole file in the historical sequence reconciles these requirements.
The atomic cutover would refuse an overlay of the current body because its
frozen body pin still targets `7c09844...`. Do not rewrite hashes, extract
replacement bodies, replay foundations, or restore the old body to bypass this.

Required next source-owner deliverables are a reviewed whole calendar-entry
overlay that preserves the current claim behavior plus its corresponding whole
atomic-cutover pins, and an owner-frozen complete baseline DDL/catalog/ACL export
for the actual dependency corpus. Current fixture exports contain function
definitions and selected catalog facts; they do not establish the complete
production schema, triggers, constraints, enums, role inheritance, default ACLs,
extension placement and ledger baseline. A production-shaped rehearsal cannot
be established safely from the accepted synthetic bootstrap/table slices.

The first revision of `tools/generated_image_release_preflight.py` pinned six complete source files
before any connection is opened. It classifies 0611 ledger state, 0625 absence,
the current claim source and the then-incompatible entry prerequisite. Hash, target,
owner, ACL, signature, body or configuration drift stays blocked. It supports a
caller-supplied fresh authorized TLS-verified connection for metadata readback;
it always rolls back and closes that connection, including on a read failure.
It cannot retry issuance or apply. Its CLI only checks local inputs and an
optional JSON catalog receipt; it always exits 2 (no-go) and rejects `--apply`.
This fresh-readback mechanism does not establish a lost-COMMIT-ACK installation
receipt, rollback implementation, complete catalog acceptance or fixture parity.
Those remain unimplemented until the missing sources are resolved. Runtime and
release controls remain unactivated by this milestone.

Focused check command: `python3 -m pytest tests/test_generated_image_production_install_pg.py -q`
returned **23 passed**. These are static/stub diagnostics, with explicit refusal
of hash/ACL/catalog drift and fresh read-only connection cleanup. They are not
a production-shaped PG17 installation proof. Preservation rerun used verified
PostgreSQL 17.11 binaries at `/opt/homebrew/Cellar/postgresql@17/17.11/bin` and
existing psycopg 3.3.6 at `/tmp/echo-0611-test-deps`, without an installation.
`PG17_BIN=/opt/homebrew/Cellar/postgresql@17/17.11/bin PYTHONPATH=/tmp/echo-0611-test-deps python3 -m pytest tests/test_generated_image_production_install_pg.py tests/test_generated_client_release_manifest_pg.py tests/test_generated_client_full_dispatch_pg.py -q -rs`
returned **27 passed in 9.40s**, including all four existing generated/ordinary
full-manifest/full-dispatch cases. The 14 warnings are existing Pillow
`Image.getdata` deprecations in `agent/vision.py`. The first system-Python run
skipped the four PG cases because psycopg was unavailable there; the rerun above
resolved that runtime issue. Source files and accepted seed were unchanged.

Independent review identified two diagnostic defects, now repaired within the
same three owned files. The 0611 reconciliation pins the exact Supabase ledger
version `20261009143836`; a different otherwise valid 14-digit version is drift.
Nested offline receipt rows and field types are validated and projected before
reconciliation. Null/scalar rows, invalid ACL types, malformed MD5 bodies and
invalid UTC timestamps return structured `invalid_catalog_receipt` with no
catalog content reflected. The CLI also handles malformed JSON, invalid UTF-8,
excessive JSON nesting and unreadable files with the same sanitized no-go and
exit 2. Extra offline fields are discarded.

The hardened targeted plus preservation rerun used the same command and shared
runtime above and returned **61 passed in 6.28s**, with the same 14 existing
Pillow warnings. This includes 57 static/stub diagnostics and all four unchanged
PG17 generated/ordinary manifest/dispatch cases. No production read or mutation
was required for these two repairs; the frozen live receipt below is unchanged.

Follow-up independent review found that a missing nullable `claim.config` key
could pass type validation and fail during projection. Every projected row
field now must be present before its value is checked. The deleted-config
API/CLI regression returns sanitized `invalid_catalog_receipt` and exit 2.
The same targeted plus preservation command returned **62 passed in 6.85s**,
including 58 static/stub diagnostics and all four unchanged PG17 cases, with
the same 14 existing Pillow warnings. Production scope remains blocked.

## Compatible whole-overlay checkpoint — 2026-10-09 16:14 UTC

Independent review accepted the whole calendar entry source SHA-256
`ca9e49459b817cee291ce70db71a8f3d4a71a264f091030d28499185ea98aa1a`
and whole atomic cutover source SHA-256
`82a485a0aa8beb339a2ddb906a0fdef241950716826c659a1f3c6e44f6a9cd81`.
The calendar source preserves the complete live October 7 claim body MD5
`c624eedcee819496129639108be991f6` with only the G/C entry insertion before
existing locks. The resulting body MD5 `0f0a4ee00e2a31f2e0d3e7c0f59e7e77`
is pinned in the whole cutover source. Rollback instructions restore the claim
from the complete October 7 migration, preserving capacity six, and the other
five functions from frozen evidence. No production SQL was applied.

The new focused disposable PG17 proof executes whole calendar overlay and
whole cutover, including stale-body atomic refusal, metadata/ACL preservation,
both capacity-six days, prior capacities, negative cases, approval proof and
tenant isolation. Root independently reran it, the existing calendar fixture
and atomic cutover test; all passed. The updated read-only preflight pins the
accepted whole-file hashes and now reports the live claim as matching the
calendar entry **body prerequisite only**. Its CLI still always exits no-go.

The separate read-only public catalog bundle at
`work/evidence/image-production-baseline-20261009` captured a broad metadata
snapshot at 16:08:55 UTC with no customer rows or passwords. It is **not** a
replayable schema-only baseline or complete dependency closure. A verified
schema-only dump, local restore/catalog parity and whole pending-stack install
rehearsal remain required. The historical synthetic install sequence below
retains its original hashes as earlier evidence; it is not a production plan.
Image migration, issuer, hosted-byte writer/reader, flags, provider publishing
and global image outcome remain UNAPPLIED / UNVERIFIED.

Frozen current live receipt:

```json
{
  "claim": [
    {
      "acl": [
        "postgres=X/postgres",
        "service_role=X/postgres"
      ],
      "name": "claim_calendar_publish_slot_owned",
      "owner": "postgres",
      "config": [
        "search_path=public"
      ],
      "definer": true,
      "body_md5": "c624eedcee819496129639108be991f6",
      "identity": "p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean, p_require_approval_proof boolean"
    }
  ],
  "database": "postgres",
  "project_id": "ooqcvmcjspeltuuhcvlh",
  "ledger_0625": [],
  "public_0611": [
    {
      "checksum": "55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2",
      "filename": "0611_echo_source_brand_bundle.sql"
    }
  ],
  "public_0625": [],
  "objects_0625": [],
  "supabase_0611": [
    {
      "name": "0611_echo_source_brand_bundle",
      "version": "20261009143836",
      "sql_sha256": "55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2"
    }
  ],
  "server_version": "17.6",
  "captured_at_utc": "2026-10-09T15:52:12.764759+00:00",
  "system_identifier": "7642734024280108049"
}
```

## Frozen checkout identity

- Repository: Echo combined checkout `work/echo-image-combined-20261009`
- HEAD: `82e2b4ef1c85b2c9443293c0e85cc3334357a34e`
- Hashes below are SHA-256 of the checked-out bytes inspected for this packet.
- The entrypoint and its focused test are committed at this HEAD, but the
  entrypoint remains unprovisioned and default-OFF. They are not SQL install
  inputs in the PG17 release sequence below.

## Exact test install order

The order is the one in `tests/test_generated_client_release_manifest_pg.py`
(`install()`); the 0611 verifier runs immediately after its migration and before
later bridge SQL adds triggers to `echo_source_captures`. The order then continues
through the extension and verifier in
`tests/test_generated_client_full_dispatch_pg.py`. “Apply” means execute the
whole listed SQL file unchanged. Test-harness-only fixture construction is
called out separately and must not be mistaken for deployable SQL.

| # | PG17 sequence | File SHA-256 |
|---:|---|---|
| 1 | Test harness creates synthetic base schema, roles and fixture columns inline; no file | — |
| 2 | Apply foundation | `migrations/DRAFT_fixer_forward_media_claim_20261006.sql` — `2e2f007608c96bd63bd71e874ff7aa0b69b2f21935140f261d6f4041792d6bdb` |
| 3 | Apply foundation | `migrations/DRAFT_fixer_forward_media_observation_bridge_20261007.sql` — `6f13aea721b88d8fa510d3930428f40aba75ce13ea86490b81ee79ca6c4e6bce` |
| 4 | Apply foundation | `migrations/DRAFT_fixer_forward_media_source_history_20261007.sql` — `81bc6101a6b2802b7be44d8b94ec8f6f5ff07c63875955f50f68a234ee67c4ca` |
| 5 | Apply foundation | `migrations/DRAFT_fixer_forward_media_photo_certificate_20261007.sql` — `27ba9dddb8c3c4f0f7a311556cd296e72ff2f63b4bf9fa844d0fb76fb81f1863` |
| 6 | Apply foundation | `migrations/DRAFT_fixer_owner_photo_clearance_20261007.sql` — `140874bdad7673eefe2005cd9ec3b0da45996cec78a9923774f37af47f50e04d` |
| 7 | Apply foundation | `migrations/lasso_bounded_catchup_capacity_20261005.sql` — `a92dd7acc3fd2219e09569af7fb4a8625f35f085a7eb34bbdd0d0815b9aa6130` |
| 8 | Apply foundation | `migrations/lasso_immediate_backlog_capacity_20261005.sql` — `b0229a5ca01341bfddb4c337ff7d1939ded67f88ba9f3c96e02ef742203cde18` |
| 9 | Apply foundation | `migrations/calendar_approval_provenance_20261005.sql` — `ed2ab5e86c7ab8a282c3b7bc30a0e52b089e901e1ba76c278ec154d738e99872` |
| 10 | Execute only the `portal_action_receipt` table slice embedded between the two test markers; not the whole migration | `migrations/portal_action_receipt_draft_20261004.sql` — `9bc59797e217ff96b5e31d9d79b67d1260290c666c5fa7a663a7c25f01d6b83a` |
| 11 | Harness extracts and installs selected function definitions from four corpus-entry sources; it does not execute those whole files | `migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql` — `1818dc65918f1640932e39e198b8b60ad8576131124a832b592f5ba2208d326a`; `migrations/DRAFT_fixer_forward_lock_entry_media_20261008.sql` — `bb00c7c2ccfb9efaaa099cd80f640bd4ec05cc055725980e0b067c1227108255`; `migrations/DRAFT_fixer_forward_lock_entry_publish_caption_20261008.sql` — `916584b16e884f973c8f2999b6d790062bfa95b848ef5c7060b6123c906be4c0`; `migrations/DRAFT_fixer_forward_lock_entry_lasso_20261008.sql` — `7e99e425ac83ff46d6ee9fd0bb84cdae3ad979a78ed82700651e5e9f4f7739f0` |
| 12 | Apply atomic cutover | `migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql` — `9ba1c7cbfc25a197abeb26f12c9b439f849491ad0fab1b60a4bdb07446426d57` |
| 13 | Apply generated foundation | `migrations/DRAFT_fixer_generated_owner_20261007.sql` — `32efe329c3686350c39ad022c45ce710f95e91064d8c6ae0f4ae4471eb1d96f9` |
| 14 | Apply generated foundation | `migrations/DRAFT_fixer_generated_gap_dispatch_20261007.sql` — `2af51f5f19e0369052b4535f7edad8b2c57ac504aa2741e188520d55956154d9` |
| 15 | Apply generated foundation | `migrations/DRAFT_generated_source_palette_authority_20261007.sql` — `10cfd74bd22eee6c3f6515f69ac8f3031252056ce8219c2482ad536473fb4037` |
| 16 | Apply generated foundation | `migrations/DRAFT_generated_send_lease_20261007.sql` — `75c585f5d1f0badabd9f095142b48e7aba05089fe9afee694f4b6dc8c9cd8f05` |
| 17 | Apply portal 0611 held fixture | `tests/fixtures/portal_0611_echo_source_brand_bundle_held_20261008.sql` — `55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2` |
| 18 | Run portal 0611 verifier immediately after 0611, before later migrations add triggers to `echo_source_captures` | `tests/fixtures/generated_client_release_manifest/0611_echo_source_brand_bundle.verify.sql` — `4e99445cf04fff7046d8da64d13e02eb5ca875a7cc988c64cd9ad0e08a67063f` |
| 19 | Apply staged predecessor, in order | `migrations/DRAFT_fixer_forward_media_owner_transport_20261007.sql` — `ac264c4c79968be37f9d1a36614d2dc819609846d90a58644b18bb1c34516893`; `migrations/DRAFT_fixer_generated_bundle_bridge_20261008.sql` — `122a9faeff59999fee7e0377793ccf98af4d5ddc0f225a6d136111533889c888`; `migrations/DRAFT_fixer_forward_visual_index_20261008.sql` — `dbdd4a5b35a0b5d7f74d6fe6234e61a58006c9f6cf04b28c065cd443bdc66017`; `migrations/DRAFT_fixer_forward_schedule_reservation_20261008.sql` — `898dd3d3202add1f3cb6137eb98da954ce1d99ce23cc43bd312c3f9bbc5b6be0`; `migrations/DRAFT_fixer_forward_schedule_stage_20261008.sql` — `31b6007e86df4b46ddd4d4d6b9b7212f76d0f92072159a0012d0f6b390b5fca4`; `migrations/DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql` — `5152ddc07c1eaf8c11febc7c4ee3518f6952d39a125e5187d77a8c08fd3ddb14`; `migrations/DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql` — `e2c9ab6dfd7b5e3406d72ea3a1f81a718e3e60e4dcbebc060988a96764d6c95a`; `migrations/DRAFT_fixer_generated_local_census_producer_20261008.sql` — `2e3130c1ee80f2c1a81453495591d13893ca8e01e2af84e4ecf7cf0a03ba6875`; `migrations/DRAFT_fixer_photo_historical_clearance_20261008.sql` — `c389b8a81b81e7c4044fe43b5b57b0050bd19b47c1e4574b17f3c6ddab64687b`; `migrations/DRAFT_fixer_prospective_photo_authority_20261008.sql` — `6d9396649e50ff442730164233efecc2665a929293277db6a2c061bb42842a2b`; **same historical-clearance file is applied again** — same SHA; `migrations/DRAFT_fixer_prospective_still_v2_20261008.sql` — `2f300cffd13c4702fd476618cdcb28a12f89d7a3ab561c12cc218ecb9aca12f4`; `migrations/DRAFT_fixer_still_v2_owner_transport_20261008.sql` — `ae45a14a69558eeef8d786acd74b35f0f69eba898f7440b14c2f9418d4c274ae`; `migrations/DRAFT_fixer_current_census_reservation_lookup_20261008.sql` — `911699edffb5160add17c0bb76be686e99fb3a79513ddc96c5834b1fdcfe0a06`; `migrations/DRAFT_fixer_inventory_mutation_protocol_20261008.sql` — `50cd1d7666efd1dd6e18256cc0fa577248200a189138dedc9569c3c550d80376`; `migrations/DRAFT_fixer_remote_drive_use_cas_20261008.sql` — `c13697fa7a6a9bb738f538da0858e260f01e958b5fa22fdec221ea35cc324847`; `migrations/DRAFT_fixer_calendar_admission_guard_20261009.sql` — `83f205f0157ad7644194478eda4e3e98ab58a128580fd2adedbc7374c85d8d3c` |
| 20 | Apply portal 0625 fixture | `tests/fixtures/generated_client_release_manifest/DRAFT_0625_generated_client_approval.sql` — `d54a5b9e1f857d31912540f38f626b59c891296a4aebd7b3c2d55a1f4abdc705` |
| 21 | Apply consumer SQL and its two local verification SQL files, in this order | `migrations/DRAFT_generated_hosted_byte_authority_20261009.sql` — `162ef5cbed66e72685b352c1ba2b26e2ce7e74d8f80010fc1b4a59951e03ab94`; `migrations/DRAFT_generated_client_admission_20261009.sql` — `d02782767faf0c9337ce5fba22f9716e8da7360c4f5e091b39a597e885ca1dc6`; `migrations/DRAFT_generated_client_staged_adapter_20261009.sql` — `6af95582cfd5351f6074e06c0e7d41c6aed90c09f5c0979d9e3401afe6c97468`; `migrations/DRAFT_generated_client_admission_20261009.verify.sql` — `b47309bc533e6d65700b59c55e4f5fc1e21be263e9776100514a322536682f1f`; `migrations/DRAFT_generated_client_staged_adapter_20261009.verify.sql` — `026b28c59d5fab6d2fd6154c3fd01123ca32755abdd43212bc2238aada56819b` |
| 22 | Run portal 0625 verifier | `tests/fixtures/generated_client_release_manifest/DRAFT_0625_generated_client_approval.verify.sql` — `19fd8aa6b031bd9fd80aa70a64b34b737daf1ae02d03d0e04bfa6d3bf4953ee7` |
| 23 | Harness checks controls remain disabled and private census/protocol functions are not executable by PUBLIC | In-test assertions; no file |
| 24 | **Append issuer queue migration** | `migrations/DRAFT_generated_issuer_dispatch_20261009.sql` — `eb53bfeafd30f5a38bdcd40b872230a92d0845d8bc2bb208ce51a98fbfb318b4` |
| 25 | Run issuer queue verifier | `migrations/DRAFT_generated_issuer_dispatch_20261009.verify.sql` — `84262c1e905c53128b7612a4271e1f1fb82b9b816edeb4ee615161aaf9738914` |

The portal fixtures used by the test are copied from reviewed source artifacts,
not reconstructed: 0611 apply and verifier bytes match the owner-frozen files in
`portal-brand-source-bundle-20261008/supabase/migrations/` at worktree HEAD
`eb60d15b52cfc993c27d85b4de23cbca44d77b3a` and are pinned by their exact SHA-256
values. The portal checkout had unrelated local modifications, so the commit
identity alone does not identify these file bytes. The 0625 apply and verifier
come from `portal-generated-client-contract-20261009/supabase/migrations/`.
The fixture README records these sources, and the PG17 test pins their hashes.
The seed fixture is copied from `evidence/generated_client_seed_20261009.py`
and hash-pinned as
`d00b1a98c4fe413f84c859ac664682aa61fddb2a32c3d5d5a59f77b55969a4e8`.

## Dispatch proof boundary

The dispatch test appends the queue migration only after the complete generated
client/portal chain above. It then applies the queue verifier. In two isolated
PG17 clusters it exercises exact manifest enqueue, tenant-scoped pending read,
restricted issuer dispatch, committed receipt/result, replay identity and
denied producer claim/table access. Test adapters use synthetic PNG bytes and
fixture data. That is a database/adapter integration proof only; it does not
prove object-store delivery, provider acceptance, platform publication, or any
client outcome. PG17 is required because the queue migration uses native JSON
unique-key validation.

## Entry flow, flags and identities

The entrypoint module is committed, but remains unprovisioned, and the SQL
migration does not launch it. The entrypoint expects separately
provisioned `ECHO_GENERATED_ISSUER_DSN`, `ECHO_GENERATED_ISSUER_LOGIN`,
`ECHO_GENERATED_ISSUER_TENANT`, and `ECHO_GENERATED_ISSUER_URL_PREFIX` values
plus authenticated identities. Its runtime (`AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME`),
job (`AGENT_GENERATED_ISSUER_DISPATCH`) and worker
(`AGENT_GENERATED_HOSTED_BYTE_ISSUER`) flags must all be enabled before it
would run; each defaults OFF. A database queue group role is not a LOGIN or a
credential. The test
inserts synthetic principal-to-tenant grants and creates disposable probe
logins solely inside its temporary clusters. No production principal, tenant
grant, login, password, secret, scheduler or service is created by this packet.

## Dependency order and rollback preconditions

The queue migration explicitly requires the hosted-byte authority objects
created in step 20. Keep that migration after the generated authority and both
portal contracts; verify it immediately afterward. It must be applied as the
database owner (`postgres` in the harness), on PostgreSQL 17. Do not grant either
group role to `anon`, `authenticated` or `service_role`. Production identities
and tenant grants require a separate approved provisioning plan and least-
privilege review; none is implied here.

Rollback is not an automatic `DROP` or inverse migration. Before any future
rollback decision, require an approved maintenance window, issuer stopped and
all queue/worker flags OFF, inventory of submitted requests, quarantine and
ledger rows, reconciliation of every non-null or permanently-null receipt
state against hosted-byte authority, no in-flight transactions, and a verified
backup/restore point. Preserve append-only evidence and reconcile outstanding
dispatches before considering any object removal. This packet supplies no
rollback SQL and authorizes no production apply, identity creation, activation
or data deletion.

## Source-of-truth mismatches / cautions

- `STAGED` in the PG17 test includes `DRAFT_fixer_photo_historical_clearance_20261008.sql`
  twice. This manifest preserves that exact executable test order; it is flagged
  for source-owner review rather than silently deduplicated.
- The accepted seed is transformed in memory by `accepted_seed()` for the
  disposable fixture: it adapts census calls and turns synthetic fixture gates
  on to exercise the preserved flow. Those changes are not checked-in migration
  SQL and do not establish production writer coverage or activation state.
- The fixture test harness slices one table definition from the portal receipt
  draft and extracts function bodies from corpus-entry source files. These are
  harness accommodations, not an endorsed production install substitution.
- The queue migration and its verification are DRAFT. This manifest is not a
  release acceptance or a claim that the whole chain is deployable.

## Proof sources

- `agent/generated_issuer_dispatch_entrypoint.py` — `104a6aed72849c127439d70ca31a404028913749fb826355b38ed3c092cd6238`
- `tests/test_generated_issuer_dispatch_entrypoint.py` — `c05793011c4b6e70567e3d645ae8d57ea03fb7e78baaf525b35b6c1c527207d6`
- `tests/test_generated_client_release_manifest_pg.py` — `215907d0834ef8d3d433d27b8eacc680371836dee883c5ddf0a6c8896a3d341b`
- `tests/test_generated_client_full_dispatch_pg.py` — `a88f6452ce63bf5193da7fc6abaad5fe9e4375eaab57aee8488ad0b565bb4bd4`
- `tests/test_forward_corpus_atomic_cutover_pg.py` (source of entry extraction semantics) — `b4a454597d4fded3dd21eade69eabf39c600e37d8d2b3174cc489a29e51b422a`
- `tests/fixtures/generated_client_release_manifest/README.md` documents portal fixture provenance.
