# Generated Issuer Dispatch Transport (2026-10-09) — OFF / UNWIRED

Status: **OFF and unwired.** No production service, credential, DSN, database
role, migration, flag change, or transport has been provisioned for this
package. No Railway configuration exists or was added; wiring this package
requires separate lead review. Nothing here runs in production, on a schedule,
or in any background daemon.

This package is the AGENT B half of the issuer dispatch split: a bounded
issuer polling job plus a producer-side enqueue/readback client. The durable
store module is owned separately; both halves build only against the frozen
API below. These modules never import or create a store implementation.

## Files

- `agent/generated_issuer_dispatch_job.py` — `IssuerDispatchJob`, a finite,
  bounded polling runner for the separate issuer authority.
- `agent/generated_issuer_dispatch_client.py` — `IssuerDispatchClient`,
  producer-side enqueue/readback only.
- `tests/test_generated_issuer_dispatch_transport.py` — fake-based tests plus
  COMPOSED tests wiring the real `IssuerDispatchJob` to the real
  `GeneratedIssuerDispatchStore` over an in-memory stub connection layer below
  the store's public API (no database, no psycopg, no network).

## Composed test coverage (2026-10-09)

`tests/test_generated_issuer_dispatch_transport.py` runs the real job against
the real store class (imported, not faked) with stub autocommit connections
that mimic only the DRAFT SQL functions' committed semantics in memory. It
proves: the store accepts the job's frozenset tenant collection in
`pending(allowed_tenants, limit)`; a pending row flows through the real worker
into dispatch, ledger commit, and the safe result tally; issued rows excluded
from `pending` are never re-dispatched (the authority sees no second issue);
and OFF flag means zero store calls.

## Reviewed-and-fixed design invariants (review of 2026-10-09)

Five review blockers were fixed in the store half and are now invariants this
package depends on (store owned separately; fixes verified here only through
the frozen API):

1. Exact authority receipt verification at commit — a receipt is committed
   only against the exact binding the authority returned.
2. Issued-row exclusion from pending — rows with a committed receipt are never
   returned by `pending` again.
3. SQL canonical binding digest verification — binding digests are validated
   canonically in SQL, never trusted from request content.
4. Poison-row containment — submit rejects duplicate JSON keys with PostgreSQL
   17's native unique-key predicate. If a row accepted by SQL still fails the
   frozen Python request validation, the issuer durably quarantines that exact
   row; pending excludes it before its bounded LIMIT. A failed or uncertain
   quarantine raises a static hold and must never appear as successful drain.
5. Frozenset tenants — `pending` accepts any non-string tenant iterable,
   including the job's frozenset, and passes tenants to SQL only as a bound
   parameter.

This coverage changes nothing about status: the package remains OFF and
unwired, and no production readiness is claimed.

## Frozen shared API (unchanged by this package)

- Store producer `submit(request)`: stores immutable original manifest bytes
  plus tenant/version/binding. SQL authenticates session_user and the tenant
  grant; the request's tenant field is content, never identity.
- Store `result(tenant, version, binding)`: returns only the exact committed
  safe status and receipt UUID.
- Issuer `pending(allowed_tenants, limit)`: returns immutable requests whose
  tenant/provenance is SQL-established.
- The issuer ledger implements the existing `DispatchLedger.claim(key,
  binding_digest)` / `commit(key, binding_digest, receipt_uuid)` protocol from
  `agent/generated_hosted_byte_issuer_worker.py:99`. Claim is durable and
  committed before returning `is_new=True`; two concurrent claims of one key
  yield exactly one `is_new=True`. Same tenant+version with a changed binding
  is a conflict, never a second issue; no lease expiry or retry authorizes a
  second issue.
- Separate restricted DB roles: producer role and issuer role. The producer
  cannot issue, reconcile, write results, or write grants.

## IssuerDispatchJob semantics

- Default OFF: `run()` returns a static `generated_issuer_dispatch_off`
  result and makes zero store/authority/ledger calls unless
  `AGENT_GENERATED_ISSUER_DISPATCH` is explicitly enabled (same env-flag
  pattern as the existing worker's `AGENT_GENERATED_HOSTED_BYTE_ISSUER`) AND a
  store plus a worker (or authority + durable ledger) are provisioned.
- Bounded: at most `max_ticks` ticks (default 1, hard cap 10), each pulling at
  most `batch_limit` rows (default 25, hard cap 100) via
  `store.pending(allowed_tenants, limit)`. No infinite loop, no retries, no
  daemon, no CLI wiring.
- Per row, the job builds the existing frozen `IssuerDispatchRequest` and
  calls `HostedByteIssuerWorker.dispatch(request,
  authenticated_tenant=row.tenant)`. The authenticated tenant comes only from
  the SQL-established provenance on the pending row — never from a
  caller-supplied field. A row whose provenance is outside `allowed_tenants`
  is held (counted, never dispatched).
- Per-request failure is counted (`failed`) and never aborts the tick and
  never raises with detail. The result is a safe tally only: status, ticks,
  processed, issued, replayed, reconciled, held, failed. The worker's own
  at-most-once, replay and reconcile semantics are unchanged.

## IssuerDispatchClient semantics

- Enqueue/readback only: `submit(request)` and `result(tenant, version,
  binding)` delegate to the injected store.
- `submit` accepts only the frozen `IssuerDispatchRequest` content object;
  any extra caller keyword (tenant, authenticated_tenant, session_user, role,
  DSN, credential, or anything else) is rejected with a static hold. Identity
  comes from the store's authenticated session, never from the caller.
- `result` validates the lookup shape and returns exactly `{'status',
  'receipt_id'}` — any other fields the store might carry are stripped.
- The client exposes no issue, reconcile, claim, commit, dispatch, pending, or
  lookup methods and holds no credentials, DSN, or transport configuration.

## Provisioning boundary (not done here)

Turning this on requires, at minimum and under separate lead review: applying
and verifying the DRAFT durable-store migration, restricted producer/issuer DB
roles and tenant grants,
the `AGENT_GENERATED_ISSUER_DISPATCH` and `AGENT_GENERATED_HOSTED_BYTE_ISSUER`
flags armed by hand, trusted tenant URL scopes and reader, and service
wiring. None of that exists as of this document.
