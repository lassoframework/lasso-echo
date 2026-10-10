# Generated hosted byte issuer worker draft

Status: local, unwired, no durable ledger implementation. Base HEAD `5954dfb8`.
Built against `agent/generated_hosted_byte_authority.py` issue()/reconcile()
(lost-ACK contract), `agent/generated_client_admission.py` frozen
request/journal, and `docs/GENERATED_HOSTED_BYTE_AUTHORITY_20261009.md`. The
authority SQL remains DRAFT and unapplied. This package adds no flag arming,
no consumer wiring, no production configuration, no deployment.

## Owned contract

`agent/generated_hosted_byte_issuer_worker.py` is a stateless issuer-side
dispatch adapter in front of the trusted `GeneratedHostedByteAuthority`.

- `IssuerDispatchRequest` is a frozen dataclass carrying ONLY identity
  content: tenant, artifact version UUID, exact hosted HTTPS URL, expected
  lowercase 64-hex byte SHA256, and the ORIGINAL manifest bytes. Validation
  reuses the authority's exact binding rules (UUID/SHA/URL spelling, manifest
  identity binding, duplicate-key and nonfinite-JSON rejection). The request
  type has no fields for tenant URL scopes, issuer credentials, DB factory,
  reader or transport; request content cannot configure the worker.
- The authenticated tenant identity arrives as a separate transport-supplied
  dispatch argument, never from request content. It must equal the request
  tenant AND the authority's provisioned tenant, or dispatch holds
  (`generated_issuer_cross_tenant`) before any ledger or authority call.
- The deterministic dispatch key is SHA256 over
  `echo-generated-hosted-byte-dispatch-20261009:<tenant>:<version>`. The
  binding digest separately covers URL, byte SHA and the manifest byte SHA, so
  a replay of the same tenant+version with a changed URL, SHA or manifest is a
  static conflict hold (`generated_issuer_binding_conflict`), never an issue.
- Default OFF: `AGENT_GENERATED_HOSTED_BYTE_ISSUER` must be explicitly enabled
  by the operator; OFF holds (`generated_issuer_off`) before any ledger,
  network or authority work. Unprovisioned construction (authority without its
  trusted reader, or a ledger that is not a `DispatchLedger`) fails closed.
  The Python type check establishes the interface only; the production
  integrator must independently verify atomic shared durability. The in-memory
  test double passes construction but is never a production ledger.
- `authority.issue()` is called at most once per durable dispatch key: only a
  first atomic ledger claim (`is_new=True`) may issue. A lost or uncertain
  COMMIT ACK (`generated_authority_commit_uncertain` /
  `generated_authority_cleanup_uncertain`) triggers exact-binding
  `authority.reconcile()`; an absent or mismatched reconcile result holds
  (`generated_issuer_reconcile_unavailable`). No second asset is generated and
  nothing is re-issued blindly.
- Success returns only `{"receipt_id": <uuid>, "status": "issued" |
  "replayed" | "reconciled"}`. Every failure is an `IssuerDispatchHold` with a
  static code; DSNs, credentials, URLs, byte values and DB exception text are
  never surfaced (covered by `test_no_secret_or_exception_leak_in_holds`).
- All authority URL/DNS/read limits (tenant prefix scopes, public-IP pinning,
  TLS hostname validation, size/MIME bounds) are preserved unchanged; the
  worker adds no network path of its own.

## Durable ledger protocol (injected, NOT implemented here)

`DispatchLedger` defines two operations with mandatory semantics:

- `claim(key, binding_digest)` atomically inserts `(key, binding_digest,
  receipt NULL)` and returns `LedgerClaim(is_new=True, ...)` when the key is
  absent, otherwise returns the stored row (`is_new=False`). The insert must
  be atomic across ALL issuer processes and durably committed to shared
  storage BEFORE returning. Exactly one concurrent claimant may observe
  `is_new=True`; that is the sole mechanism bounding issue() to at most once.
- `commit(key, binding_digest, receipt_id)` durably records the receipt UUID
  exactly once; it must reject a missing key, a binding mismatch, or a
  conflicting existing receipt, and be idempotent for the same receipt.

Restart semantics: a crash between claim and commit leaves a row with receipt
NULL. Any later dispatch of that key observes `is_new=False, receipt_id=None`
and MUST reconcile through the trusted authority; it never issues again.
Unblocking a permanently held row is an operator action by the integration
owner, not this worker.

## Explicitly unimplemented gate

This package ships NO durable ledger implementation and NO transport. An
in-memory dict or a local SQLite file is not shared across Railway services
and CANNOT provide the required cross-process atomic claim; the in-memory
ledger in the tests is a test double only. Wiring a production dispatch
requires the integration owner to inject a ledger backed by shared durable
storage with atomic compare-and-insert (e.g. the same Postgres authority
plane, once its DRAFT SQL is reviewed and applied) plus an authenticated
transport that supplies the tenant identity out of band. Until then the
worker fails closed at construction without a conforming ledger.

## Tests

`tests/test_generated_hosted_byte_issuer_worker.py` (offline, synthetic
connections/reader): OFF/unprovisioned zero-issuance, malformed and
cross-tenant holds before ledger access, changed-manifest/byte holds, single
issue with replay returning one receipt, lost-ACK reconcile of the same frozen
identity with hold-on-absent and no blind re-issue, conflicting replay hold,
secret/exception leak check, and preserved reader scope limits.

    python3 -m pytest -q tests/test_generated_hosted_byte_issuer_worker.py
    # 15 passed
