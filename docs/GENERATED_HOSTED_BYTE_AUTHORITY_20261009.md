# Generated hosted byte authority draft

Status: local, unapplied, unwired. Base inspected at Echo `fe58072a` and the
portal generated-client-contract draft `DRAFT_0625_generated_client_approval.sql`.
This package does not change the unconditional generated hold in `drafter.py`,
calendar insertion, approval, publish entry points, or production configuration.

## Owned contract

`agent/generated_hosted_byte_authority.py` provides a trusted-process issuer and
an exact immutable lookup. It has no credential/environment discovery, automatic
worker launch, feature flag, retries, CLI, provider calls or consumer integration.
The integration owner supplies a factory that opens a new dedicated idle
transactional connection, canonical tenant, and trusted tenant URL scopes.

The issuer accepts an artifact version UUID, exact public HTTPS object URL,
expected lowercase 64-hex SHA256, and original UTF8 manifest JSON bytes. The
manifest must bind `gym_id`, `artifact_version_id`, `hosted_url` and
`delivered_sha256`. Duplicate JSON keys and nonfinite numeric values are rejected.
The manifest digest is SHA256 of its exact original bytes, including whitespace,
not reconstructed JSON. Retain those bytes for subsequent exact lookup.

The issuer first verifies its authenticated database principal and tenant grant,
ends that read transaction, independently GETs the hosted object, compares the
read bytes against the expected SHA, then opens a new connection and checks the
grant again. The write RPC computes the delivered and manifest SHA256 itself
from the supplied bytes and verifies the manifest identity. No DB locks are held
during the GET. The grant row is locked until issuance commits, so concurrent
grant revocation serializes with that transaction.

`HostedObjectReader` uses distinct operator-configured HTTPS directory prefixes
per tenant. Overlapping tenant namespaces, root prefixes, credentials, signed
URLs, fragments, nondefault ports, percent-encoded or ambiguous paths and
cross-tenant URLs hold. Scope comparisons use the canonical HTTPS origin tuple.
Only lowercase `https://` scheme and DNS hostnames without an explicit port or
trailing dot are accepted. Scheme/hostname case, explicit `:443`, zero-padded
ports, empty ports and trailing-dot aliases are rejected in both the worker and
SQL issuer. Alias spelling cannot create another tenant namespace or bypass
hosted URL uniqueness. Every DNS answer must be a public global address. The
connection pins a validated IP and retains normal certificate/hostname TLS
validation, preventing a second DNS lookup from redirecting to a private host.
Redirects, content encoding, HTTP errors, non-image MIME types, incomplete reads
and images outside 1 byte to 8 MiB hold. Socket operations time out after the
configured interval, 15 seconds by default; DNS resolution uses the platform
resolver and has no independently enforced deadline.

## SQL authority and portal compatibility

`migrations/DRAFT_generated_hosted_byte_authority_20261009.sql` requires the
portal's existing `calendar_generated_artifact_versions` table and both enabled
immutable/no-truncate triggers. It does not create or replace the portal stack.
Apply only after independent review of the composed portal/forward/generated
approval stack and ordinary release authorization. This draft has not been
applied to any existing database.

The portal table receives the existing schema exactly:

| Portal column | Binding |
| --- | --- |
| `id` | Original canonical artifact version UUID |
| `gym_id` | Authenticated tenant grant |
| `image_url` | Exact independently read HTTPS URL |
| `delivered_sha256` | SHA256 of GET-read image bytes, lowercase 64 hex |
| `render_manifest_digest` | SHA256 of exact original manifest bytes, lowercase 64 hex |
| `delivery_receipt` | Authority-created JSON with the six existing Draft receipt keys |
| `verified_at` | First committed observation timestamp |

The six receipt keys are `receipt_id`, `gym_id`, `artifact_version_id`,
`hosted_url`, `delivered_sha256` and `render_manifest`. Receipt IDs are generated
inside Postgres, never supplied by the producer. Both version and receipt-log
rows commit atomically. The additional immutable receipt log records the
authenticated issuer, original manifest bytes/digest and delivered byte count;
image bytes are hashed during issuance and are not duplicated in Postgres.

Dedicated `generated_hosted_byte_issuer_20261009` and
`generated_hosted_byte_reader_20261009` roles are unprivileged NOLOGIN roles.
No memberships, login credentials or tenant grants are provisioned by this
draft. Admin-only `generated_hosted_byte_principals_20261009` maps
`session_user` to exact tenant and issue/lookup permissions. No caller-provided
actor, alias, JWT, application grant dictionary or `SET ROLE` establishes tenant
authority. The trusted issuer role receives execute privileges only; it cannot
directly select or mutate either authority table or the portal version table.
The reader receives only authorized lookup/identity RPC execution. Ordinary
`service_role`, `anon` and `authenticated` cannot issue or write receipts;
the portal's existing read-only service-role version access is preserved.
Functions use a fixed search path and all new table/function default privileges
are explicitly revoked, including a hostile broad-default-privilege fixture.

Lookup requires tenant, version, exact URL, delivered SHA, exact original
manifest bytes and receipt UUID. It returns only a committed portal row joined
to a matching authority issuance row. Producer-supplied receipt shape alone and
even an otherwise well-formed preexisting portal row never pass this lookup.
Every binding mismatch or missing receipt is a hold in the Python adapter.

Exact version replay returns the original receipt without changing its
timestamp. The Python issuer independently GETs the object on every replay.
Changed bytes, URL or manifest cannot rewrite a version. A hosted URL is unique
in the issuance log and cannot transfer to another version or tenant; use a new
immutable object key and UUID for a new rendition. Concurrent identical
issuance serializes and creates one receipt. UPDATE, DELETE and TRUNCATE fail
for both portal versions and authority receipts, including ordinary admin DML.
Commit uncertainty holds and never triggers an automatic retry. Reconcile using
the exact pinned lookup on a fresh connection; do not invent a replacement ID.

## Evidence

The focused command used the existing task dependency directory, without an
installation:

```sh
PYTHONPATH=/tmp/echo-pg-test-deps-20261009 python3 -m pytest -q \
  tests/test_generated_hosted_byte_authority.py \
  tests/test_generated_hosted_byte_authority_pg.py \
  tests/test_generated_client_contract_transport.py
```

Result after the independent-review origin-alias repair: **118 passed**. The PG
fixture starts a temporary local PG17 cluster on
a private Unix socket with TCP listening disabled, then stops it and removes the
temporary cluster. It reproduces only the portal immutable version table schema;
it does not claim full portal calendar/approval-stack composition parity.

| Requirement | Evidence |
| --- | --- |
| Independent GET precedes write with no read transaction held | Unit ordering and real DB adapter tests |
| Forged receipt, inaccessible object and byte mismatch hold | Unit reader/readback tests; PG forged-row lookup and SHA tests |
| Exact tenant/version/URL/SHA/manifest binding | Unit manifest tests and real SQL mismatch/reuse tests |
| Canonical origin prevents same-object tenant namespace and URL alias reuse | Adversarial unit and PG scheme/host-case, default/empty/zero-padded-port and trailing-dot alias regressions |
| Producer and reader cannot mint or alter authority | PG execute/table/grant ACL tests with BYPASSRLS service role |
| Immutable exact replay and one receipt under concurrency | PG serial and concurrent replay tests |
| Revocation holds before write and serializes with in-flight issue | PG preflight revocation and grant-lock tests |
| UPDATE/DELETE/TRUNCATE cannot rewrite evidence | PG portal and receipt immutability tests |
| Existing generated hold still holds well-formed receipts | Unit Draft hold and unchanged generated transport suite |

## Remaining limits and handoff

The SQL function cannot independently contact HTTPS. The trusted issuer process
and its dedicated credential are the byte-observation trust boundary. Anyone
with that credential could submit arbitrary bytes directly to the RPC; it must
never be exposed to producers, browsers, service-role workers or agent tools.
The object reader and connection factory are trusted dependency injection
points; synthetic tests replace them, and those fixtures do not establish live
receipt authenticity. Database superusers/schema owners can alter DDL and are
outside the application ACL boundary.

The authority establishes hosted-byte provenance at an observation time. It
does not establish generation-source approval, artwork quality, member consent,
immutable behavior of the CDN/object store, current bytes after issuance,
approval to publish, provider delivery or platform publication. Future consumer
wiring must pin and validate the authority receipt and preserve final hosted
readback, replacement approval, tenant isolation and send gates. Consumers must
not trust cached receipt JSON or portal-table existence as a substitute for the
dedicated lookup. Production object storage must enforce immutable distinct
version keys; no storage policy changes are part of this draft.

Still required: independent source review, full composed stack testing, separate
operator provisioning of least-privilege principals and tenant URL scopes,
guarded migration/deployment, live hosted-object readback evidence, and a
separately reviewed consumer integration. No production SQL, secrets, deployment,
send, commit, push or paid Claude review was performed in this lane.
