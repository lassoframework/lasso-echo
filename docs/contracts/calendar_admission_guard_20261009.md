# Calendar media admission draft

`DRAFT_fixer_calendar_admission_guard_20261009.sql` applies last, after the
complete reservation, stage, worker/attester discovery, observation, trusted
preparation and prospective still v2 stack. Its independent protected gate is
inserted OFF. This work authorizes no production SQL or gate activation.

When armed, active media INSERT always refuses, including null logical IDs.
Media enters inactive through registered staging and becomes active only in the
exact persisted batch finalizer, whose existing revision, lineage, reservation,
permanent source/derivative occupancy and conflict checks remain transactional.
The legacy finalizer routes to the persisted batch finalizer while armed. A
failed downstream check rolls back activation and the private capability.

The boundary freezes active source, delivered and thumbnail identity, tenant,
date, logical ID, account, format, location, visual group and signed caption.
Ordinary source backfills, swaps and clearing every media field refuse. The
trusted manifest binder may fill an initially null digest for its exact row.
Registered inactive preparation remains unchanged. Exact old-row archival uses
the persisted live snapshot; active media deletion and arbitrary retirement
refuse. Status, approval, claim and delivery bookkeeping remain writable under
the preceding guards. Text-only rows remain writable.

Capabilities are private database records bound to backend, transaction and
request row IDs. Only four exact trusted entry wrappers manage them; renamed
bodies lose all runtime execute grants. Entry signatures and prior execute
ACLs are preserved after clearing every nonowner creation grant, including
non-PUBLIC ALTER DEFAULT PRIVILEGES grants. Shared `postgres` ownership and caller-set session flags
are insufficient. A database superuser or schema owner remains an administrator
who can disable triggers or change the gate; this is not an admin sandbox.
Graph then census locks serialize armed operations at read committed isolation.

Focused disposable PG17 acceptance uses synthetic signed Ed25519 schema 2,
source and lineage records. It covers genuine stage/preparation/admission/
finalization and persisted old-row archival, null-logical inserts, ordinary and
legacy-definer PATCH, borrowed/stale authority, cross-tenant/date source and
derivative conflicts, concurrent refusal, capability cleanup and nonmedia
operations. Positive platform sibling preparation/finalization remains covered
by the preceding stack's own harness, not newly demonstrated by this module.
No production behavior, gate activation or provider delivery is claimed.
