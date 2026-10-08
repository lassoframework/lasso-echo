# Forward Visual Index — DRAFT / OFF (2026-10-08)

Unapplied draft. No production activation, provider calls, gates enabled or
credentials provisioned. Requires the base forward-media claim draft.

## Authority and activation fence

The migration renames the unchanged base implementation to
`fixer_claim_forward_media_internal_20261008` and revokes all caller EXECUTE
privileges. Its ownership, token, revision, provenance, history, tenant, ready
media and byte occupancy checks remain intact.

The original public `fixer_claim_forward_media_20261006(uuid,uuid,uuid,text)`
remains the default OFF dispatcher. It delegates to the private implementation
only while the protected singleton `forward_media_visual_gate_20261008` is
explicitly false. Missing gate or an armed gate refuses this RPC. The service,
attester and media owner cannot mutate the gate. A privileged, separately
approved activation must set the database gate true and the publisher's
`AGENT_FORWARD_MEDIA_VISUAL_INDEX` flag true. Mismatched activation holds.
An armed visual environment flag also makes every existing calendar and lower
provider wrapper treat the guard as effective ON. If the base environment flag
is unset or false, claims hold for incoherent configuration, even when the DB
visual gate is ON. Both environment flags OFF retain the existing OFF behavior.

Gate changes take the exclusive graph lock and claims take its shared lock,
so activation cannot race an old-path claim transaction.

The armed publisher calls `fixer_forward_visual_proof_20261008` with its
original row, token, evidence and outgoing revision, then calls
`fixer_forward_visual_index_claim_20261008` with those same four arguments and
`p_attestation_ids uuid[]`. Both RPCs are available only to service_role; the
protected claim returns literal boolean true on success. An armed service_role
has no executable base implementation or alternate bypass.

## Isolated attester

The publisher reads persisted trusted proof only. It never fetches bytes or
opens the dedicated attester DSN. The isolated lane calls
`agent.forward_media_visual_index.attest` ahead of publication. Its dedicated
role obtains only the exact row/revision/evidence receipts through
`fixer_forward_visual_receipts_20261008`; it has no direct lineage-table read.
The read transaction ends before actual object bytes are fetched. SHA256, MD5,
length and versioned 64-bit DCT pHash are computed from actual bytes, followed
by a second byte read to catch mutation during observation.

Original, delivered and thumbnail attestations are append-only. A null
thumbnail uses the delivered URL and its trusted receipt; it remains an honest
third role of the same fetched object. The trusted attester can append negative
evidence only through a lineage/tenant-bound RPC. Direct table INSERT remains
reserved to the media owner. Unreadable objects without any byte evidence hold;
no invented negative hash is recorded.

## Atomic proof and occupancy

The protected RPC uses graph → census → row → token locks and invokes the
private base claim in the same transaction as visual checks and frozen proof
insertion. Any failure rolls back byte occupancy, the base receipt and the
visual proof together. The immutable `forward_media_visual_claim_proof_20261008`
ledger permanently binds token, row, evidence, full outgoing revision and the
exact three attestation IDs. A replay cannot replace its proof; changing a
claimed row's token holds. The publisher lookup returns the frozen proof on
replay, rather than later preparation records.

Only frozen claimed proof participates in visual occupancy. Unclaimed or later
preparation records never contaminate occupancy. The ledger and base receipt
survive calendar deletion and replacement.

Each proof validates role set, tenant, current revision, exact URLs, lineage
and trusted object receipts including MD5 and length. Matching negative
SHA256 or pHash distance <=30 blocks even replay. Explicit unused historical
clearance remains mandatory; uncertain or used history holds.

Across different tenant/date/logical groups, equal SHA256 blocks; pHash distance
<=6 blocks, 7–30 holds for review (including the incident pair at 28), and >30
has no match. Authorized same tenant/date/logical-group siblings and exact-token
replays may reuse their original/rendered/thumbnail proof. Distinct roles of a
single post never compete with themselves.

## Acceptance

`python3 -m pytest -q tests/test_forward_media_guard.py tests/test_forward_media_visual_index.py tests/test_forward_media_publish.py tests/test_forward_media_lower_publishers.py`

Real disposable PostgreSQL 17 plus existing psycopg runtime:

```sh
PYTHONPATH=/tmp/echo-forward-media-pg-20261007/lib/python3.14/site-packages PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH python3 tests/test_forward_visual_index_pg.py
```

The assembled regression runs actual Python attestation with the dedicated
role, actual Python guard through a psql-backed service REST adapter, OFF base
call, armed base/private bypass denial, negative persistence/replay denial,
distinct role derivatives, siblings, frozen proof pollution, token drift,
unknown history holds, cross-tenant/date comparisons, concurrent one-winner
claims with no loser occupancy and immutable evidence. Provider behavior is
represented by a synthetic lower provider invoked through the real provider
decorator, plus actual Meta/SocialAPI/Zernio wrappers using existing fake vendor
fixtures. Mismatched environment flags hold under database gate ON and OFF;
coherent ON authorizes one synthetic invocation and database OFF holds. No live
provider is exercised.

## Rollback limits

Do not apply an older base migration over this installed fence. Reverting an
installed draft requires privileged restoration of the original base function
name and ACL after removing its dispatcher, plus removal of unused extension
objects in dependency order. After claims exist, preserve all proof, negative
and occupancy evidence. No production rollback or activation is authorized by
this draft.
