# Generated Issuer Dispatch Runtime (2026-10-09) — OFF / UNWIRED

Status: **OFF and unwired.** No production service, credential, DSN, database
role, migration, flag change, schedule, or transport has been provisioned for
this module. Nothing here runs in production or claims production readiness.

This is the CHILD B composition root for the issuer dispatch split. It
assembles the existing bounded pieces — the restricted issuer store (owned
separately), the trusted hosted authority, a trusted tenant namespace config,
and the bounded `IssuerDispatchJob` — into one finite runtime. The client
admission/runtime side (Child A) is separate; this module exposes only
injected dependency protocols and never imports or implements a store,
authority, reader, or transport.

## Files

- `agent/generated_issuer_dispatch_runtime.py` — `IssuerDispatchRuntime`,
  `TrustedTenantNamespace`, `RestrictedIssuerStoreView`, and the runtime flag
  gate.
- `tests/test_generated_issuer_dispatch_runtime.py` — 20 offline tests
  (stub connections, in-memory fake issuer store, synthetic reader; no
  database, network, credential, or real store).

## Assembly contract

`IssuerDispatchRuntime(store=..., authority=..., namespace=...,
batch_limit=None, max_ticks=None)` is the whole surface. Every piece is
injected; the module reads only its own flag and the existing job/worker flags
and accepts no DSN, credential, token, connection string, URL, or file path
in any parameter — string/bytes values passed as store or authority are
rejected, and the fixed keyword-only signature rejects unexpected arguments
(such as a `dsn=`) with `TypeError`.

- **store** (protocol, injected): must expose `pending(allowed_tenants,
  limit)` and the `DispatchLedger` protocol `claim(key, binding_digest)` /
  `commit(key, binding_digest, receipt_id)` from
  `agent/generated_hosted_byte_issuer_worker.py`. The runtime wraps it in
  `RestrictedIssuerStoreView`, so the assembled job and worker can reach only
  those three methods; producer-side `submit`/`result` and any other store
  attribute are unreachable through this runtime.
- **authority**: must be exactly `GeneratedHostedByteAuthority`, provisioned
  by the integration owner with its trusted `HostedObjectReader` (exact
  HTTPS tenant path scopes). A readerless authority fails closed at assembly.
- **namespace**: a `TrustedTenantNamespace` — a frozen, validated, non-empty
  set of canonical tenants. It must contain the authority's own
  `tenant_id`; a runtime granted no coverage for its authority is rejected
  at assembly. Each instance polls only the authority's tenant even if the
  trusted namespace lists others.

## Runtime semantics

- Default OFF: `AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME` must be explicitly
  enabled AND `AGENT_GENERATED_ISSUER_DISPATCH` (job) AND
  `AGENT_GENERATED_HOSTED_BYTE_ISSUER` (worker) must be enabled. Otherwise
  `run_once()` returns the static `generated_issuer_dispatch_off` tally
  having made zero store/authority/ledger/worker calls.
- Finite and terminating: `run_once()` performs exactly one bounded job run
  (at most `max_ticks` ticks, hard cap 10; at most `batch_limit` rows per
  tick, hard cap 100 — the job's own caps) and returns the job's safe tally
  dict. There is no loop, no scheduling, no daemon, no retry, and no CLI
  wiring.
- Fail closed and quiet: assembly and run failures surface only as static
  `IssuerDispatchRuntimeHold` codes plus the job's count-only tally. This
  module logs nothing and never surfaces rows, tenants, URLs, DSNs, or
  exception detail (covered by `test_no_secret_or_exception_leak_in_holds`).
- At-most-once issuance, replay, reconcile, cross-tenant hold, and
  poison-row quarantine semantics all live unchanged in the worker, job, and
  store; the runtime adds no dispatch behavior of its own.

## Deployment and principal requirements (exact)

All of the following are integration-owner actions under separate lead
review. None exist as of this document:

1. **Migration**: the DRAFT durable-store migration
   (`migrations/DRAFT_generated_issuer_dispatch_20261009.sql`) reviewed,
   applied, and verified. This runtime ships no SQL and touches no
   migrations.
2. **Principals**: two separate restricted PostgreSQL LOGIN roles — a
   producer role and an issuer role — with admin-provisioned per-tenant
   grants enforced in SQL. The injected store's connection factory must
   authenticate as the **restricted issuer role only** (pending/claim/
   commit/quarantine functions; no submit, no result writes, no grant
   writes, no service role). The producer role must never be wired into
   this runtime. Role provisioning is by hand; the runtime cannot verify
   the role and fails closed only at the SQL permission boundary.
3. **Authority principal**: the `GeneratedHostedByteAuthority` connection
   factory must supply a dedicated idle autocommit connection authenticated
   as the authority's own restricted role, with the operator-configured
   `HostedObjectReader` tenant URL scopes (exact HTTPS prefixes, one
   immutable object directory per tenant).
4. **Namespace config**: `TrustedTenantNamespace` provisioned by the
   operator from the same trusted tenant directory as the authority; it is
   configuration, never request content.
5. **Flags**: all three flags armed by hand in the environment —
   `AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME=true`,
   `AGENT_GENERATED_ISSUER_DISPATCH=true`,
   `AGENT_GENERATED_HOSTED_BYTE_ISSUER=true`. Any one absent means the
   static off result.
6. **Invocation**: an operator-chosen finite trigger (e.g. a one-shot
   container run or a platform cron invoking `run_once()` once per firing).
   No long-running process, loop, or daemon is part of this module.

## Tests

    python3 -m pytest -q tests/test_generated_issuer_dispatch_runtime.py
    # 20 passed

Coverage: OFF tally with zero dependency calls; runtime flag overrides armed
inner flags; DSN/string store and authority rejection; missing issuer
surface rejection; wrong authority type rejection; namespace must cover the
authority tenant; namespace rejects strings/empty/non-tenant values;
unexpected keyword (credential smuggling) rejection; the restricted view
hides producer methods; readerless authority fails closed; one armed run
issues exactly once and terminates within bounds; a second run replays with
no second issue; cross-tenant rows are held and never dispatched; bounded
tick/limit caps enforced; pending failure counted not raised; no secret or
exception text in holds.
