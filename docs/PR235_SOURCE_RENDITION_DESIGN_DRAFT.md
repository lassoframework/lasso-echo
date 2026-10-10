# PR235 source and Story rendition extension — design only

Status: DRAFT, no SQL applied, no guard armed. The current draft includes
additive object-attestation preparation, multi-fingerprint scene claims,
calendar-trigger integration, and multi-fingerprint history import/coverage.
The current dirty implementation also has owner-created read/render receipts,
Story reburn evidence, and writer/PATCH wiring. Remaining gaps include other
transformation and direct-writer paths, historical evidence reconciliation,
acceptance, and activation. This document is the bounded
fallback for the cross-client requirement received on 2026-10-03. The newer
cross-client rule supersedes the same-gym scope in
`global-media-release-plan-20261002.md:7`. The dirty PR235 implementation is
preserved. None of the phases below is safe to activate on its own.

## Why the current draft cannot be activated

| Current contract | Correctness gap |
| --- | --- |
| The legacy `visual_global_identity` compatibility path and `visual_global_claim` still model one selected fingerprint. The newer scene fingerprint set derives multiple MD5s from attested source/rendition members. | The compatibility path cannot represent a raw photo and its burned Story as separate bytes; integrated scene claims must use the set-based path. |
| `visual_global_prepare_bundle` remains a one-delivered-URL legacy path. `visual_global_prepare_source_rendition` registers distinct objects and receipt-backed lineage; the dirty Story writer now produces those receipts. | Reburns and other channel renditions still need complete writer coverage, independent review, and behavioral acceptance. Equal scene identity is not equal byte identity. |
| `visual_global_row_fingerprint` and `visual_global_claim` remain one-fingerprint compatibility helpers; `visual_global_claim_scene` derives the complete component fingerprint set and `visual_global_claim_fingerprint_set` claims it atomically. | Only attested fingerprints in the scene set are protected; missing source/rendition evidence still prevents a complete claim. |
| `migrations/DRAFT_visual_group_claim_trigger_20261002.sql` now routes guarded calendar writes through `visual_group_global_claim` to `visual_global_claim_scene`, which verifies selected row bytes and claims the full attested scene set. | The DRAFT trigger integration exists, but cannot safely arm until every row's source/delivered bytes and lineage are evidenced and the integrated PG/concurrency matrix passes. |
| `visual_global_import_history`, `visual_global_coverage` and `visual_global_history_coverage` now enumerate scene fingerprint sets, import retained local ledger history, and report calendar plus orphan/member/global-owner gaps. | Reports/import cannot reconstruct missing historical bytes or lineage, nor recover rows absent from retained ledger/evidence; those gaps remain activation blockers. |
| `agent/visual_writer_prepare.py` and `agent/visual_owner_receipts.py` now read both exact objects, create owner-only read/render receipts, and call `visual_global_prepare_source_rendition`; `agent/story_reburn.py` returns byte observations and `agent/portal_social.py` passes them through preparation. | This is dirty DRAFT code, not accepted or production evidence. Audit receipt trust, all transformation paths, and the real-PostgreSQL cases before relying on it. |
| `agent/portal_calendar_store.py` now prepares the replacement with render evidence and returns the scoped persisted PATCH row; portal and calendar reburns check that result. The feed-aspect rehost also submits source/output render evidence when writer preparation is enabled and checks the persisted PATCH row. | This code has no production acceptance. Direct operator PATCH fallbacks and the forward-reservation staging/finalization path still need release reconciliation; complete historical byte and lineage coverage remains required. |

## Target invariants and schema

1. Keep the canonical tenant registry and tenant-local `visual_group` scene ID.
   Preserve both `image_url` and `source_media_url` as exact `canonical_url`
   aliases in `visual_group_row_aliases`; they must resolve to the same local
   scene or an explicitly confirmed same-tenant scene component. A source URL
   must never be silently cleared to make a claim pass.
2. Replace the single group fingerprint authority with append-only **object
   attestations**: `(canonical_tenant, exact_url, md5)` plus byte length,
   acquisition method, actor, timestamp, and evidence reference. The exact URL
   includes its query string. A URL may have source and delivered roles; an
   existing URL cannot be rebound to different bytes. A Drive asset attestation
   must validate its tenant and `content_hash`; other objects require a verified
   byte read. SHA evidence may be stored additionally, but global usage uses a
   uniformly computed MD5. Unknown bytes produce no attestation.
3. Add append-only **scene membership** for every attested source or rendition
   fingerprint and an immutable **lineage edge** from source object to rendition
   object. An automatic edge requires a receipt from the actual render/reburn
   operation binding input digest, output digest, both exact URLs, operation,
   and actor. A historical human link requires named reviewer and concrete
   source evidence. Co-occurrence on a row, similar images, matching R2 keys,
   or a pHash match is insufficient. Distinct MD5s remain distinct records.
4. Keep one global usage row per fingerprint with immutable owner/date and
   sticky ambiguity/publication. Change membership to at least
   `(canonical_tenant, scene_group, fingerprint)`; retain row/channel/attempt
   evidence separately so multiple fingerprints and same-day channels coexist.
   A staged claim remains occupied on its first date even after unsent release.
   A confirmed published claim remains permanent after calendar deletion.
5. For every calendar write that creates or changes a claim, derive the full
   fingerprint set from verified exact source and delivered objects plus every
   proven member of the confirmed scene component. Lock scene groups in sorted
   order, then fingerprints in sorted order, re-read membership after locking,
   and insert/check **all** global usage rows in the calendar transaction.
   A conflict on any fingerprint aborts the entire calendar/local/global write.
   Same tenant and date permits IG, FB, Story and GBP siblings. Any other date
   or tenant fails, including when the colliding fingerprint is only the raw
   source. Adding a rendition to an already staged scene must claim its newly
   attested digest against the scene's existing date in the same transaction.
6. The calendar BEFORE trigger remains the authority for direct INSERT,
   UPDATE, DELETE, swap, redate and publish transitions. It must validate the
   exact current URL-to-digest bindings and evidenced lineage, independent of
   Python hints. The row's single `byte_hash` may remain a selected-delivery
   hint, but cannot be the sole global authority. No caller may invoke the
   internal claim/release RPC directly. Published identity and history remain
   immutable; ambiguous attempts need the existing reconciliation evidence.

## Implementation phases and gates

### 1. Data model and atomic claim

The current DRAFTs now contain object attestation, scene membership and
lineage, multi-fingerprint usage membership and claims, append-only guards,
RLS/revokes, deterministic locks, and calendar-trigger integration. The
validated preparation RPC may register attested objects and lineage but cannot
mark usage; the trigger's scene claim marks usage atomically. Before accepting
this phase, verify exact PostgreSQL privileges and concurrency behavior and
confirm every trigger path reaches the set claim without bypasses.

**Current phase 1 subset (2026-10-03):**
`DRAFT_visual_global_history_20261002.sql` adds immutable exact-URL read
receipts, object attestations with MD5 and byte length, source/delivered scene
membership, and a render receipt bound to both reads, URLs and digests. The
owner-only receipt tables are the trust boundary: the service role cannot
create or edit a byte-read or render receipt. The new
`visual_global_prepare_source_rendition` RPC validates canonical tenant,
same-tenant scene and Drive asset scene membership/MD5 where applicable, URL
rebinding and receipt lineage, then registers both objects in one SQL
transaction. A new exact-URL alias trigger serializes later alias registration
against preparation and rejects a conflicting tenant or scene; both paths
require `READ COMMITTED` to prevent stale-snapshot checks. It cannot create
global usage. The dirty writer lane now supplies owner-created read/render
receipts for the Story source/rendition path. Those receipts use byte-read and
render observations; the database cannot infer that evidence from a URL or a
caller assertion. This wiring still needs review and focused PostgreSQL
acceptance, and does not cover every media transformation or writer.

The one-hash preparation and compatibility claim remain in the migration, but
the DRAFT calendar trigger calls the set-based scene claim. The current importer
and both coverage functions enumerate every attested scene fingerprint for
retained local ledger groups, including released staged groups and orphaned
global members/owners. They still cannot fill missing historical byte or render
evidence, and deleted calendar rows are covered only when their local ledger
and relevant evidence remain. Story reburn and feed-aspect rehost now produce
render evidence and verify persisted PATCH results in the draft implementation.
The remaining direct writers and forward-reservation finalization still need
release reconciliation. All-tenant enforcement stays OFF; this
subset is not an accepted or activatable source/rendition release.

### 2. Writer and actual render evidence

The draft implementation now reads both exact objects, verifies source asset
MD5 where present, writes owner receipts, and passes Story reburn evidence
through `agent/calendar_autopublish.py` and `agent/portal_social.py`; both
reburn callers check the persisted PATCH result. The feed-aspect rehost in
`agent/calendar_autopublish.py` also submits source/output render evidence
when writer preparation is enabled and refuses an unverified persisted PATCH.
The operator scripts `scripts/refresh_lasso_calendar.py` and
`scripts/swap_crossfitchateau_blocked_media.py` retain flag-off direct PATCH
fallbacks. `SupabaseCalendarStore.insert_rows` skips visual preparation for
forward-reservation staging; prove the isolated finalization path binds exact
bytes and lineage before activating either flag combination. Independently
review and accept all writer paths. Preserve the raw source field;
a failed preparation or unverified external object must leave the calendar
unchanged or visibly held.

### 3. History, coverage, and activation

Audit the existing multi-fingerprint import and both coverage reports in
`DRAFT_visual_global_history_20261002.sql` against every fingerprint/lineage
edge for every occupied local scene, including released staged groups,
ambiguous attempts and orphaned published ledger rows. Missing raw or delivered
bytes, missing render evidence, unknown tenant/date, and cross-tenant/date
collisions are explicit blockers; do not invent a digest or rewrite published
rows. The DRAFT importer already takes the all-tenant `content_calendar`
barrier and coverage re-read through
`DRAFT_visual_group_activation_20261002.sql:14-31,178-188`; independently
verify its object prerequisites and lock list cover the new tables. Keep the
global activation blocker until this audit and the evidence gates pass.

### 4. Focused real-PostgreSQL acceptance

Use the isolated Unix-socket fixture pattern in
`tests/test_visual_writer_bundle_pg.py:24-66` and add cases for:

- Source MD5 A and burned Story MD5 B are both attested, linked by a render
  receipt, and claimed in one calendar INSERT; both usage rows have the same
  canonical tenant/date while both URL aliases remain present.
- Same-day IG/FB/Story/GBP siblings succeed; a later date or another tenant
  using either A or B fails. Opposite-order concurrent claims and component
  links cannot deadlock or leave partial usage.
- A third fingerprint conflict rolls back all new usage, local ledger and
  calendar changes. A reburn on the original day claims its fresh output while
  retaining A and older output B; an unknown source or output digest refuses.
- Direct SQL INSERT/UPDATE, redate, swap, publish and DELETE cannot bypass the
  claim; unchanged approved/published rows and ambiguous receipts are handled
  according to their existing gates. Published/staged usage cannot be deleted
  or moved, even after the calendar row is gone.
- Backfill imports deleted-row history and multiple fingerprints, is
  idempotent, and refuses missing evidence/collisions. Activation holds a
  global write barrier and refuses any coverage issue with enforcement OFF.
- `anon`/`authenticated`/`service_role` cannot mutate ledger tables or call
  internal claim/import; preparation rejects foreign tenants, re-bound URLs,
  fabricated lineage and inconsistent asset hashes.

Current `tests/test_visual_group_global_sql.py:85-149` and
`tests/test_visual_writer_bundle_pg.py:72-121` assert the one-fingerprint /
one-URL contract and must be replaced with behavioral cases, not made green
by merely changing expected strings. The PostgreSQL fixture is skipped unless
`VISUAL_GROUP_TEST_DSN` names its disposable local database. Static/Python
checks alone do not establish the trigger or race invariants.

## Unresolved release gates

1. Obtain exact byte and transformation receipts for historical source and
   delivered objects, including deleted published rows. Unknown history must
   remain a blocker; the existing one-hash import is insufficient.
2. Reconcile the retired unmapped calendar key and fleet collisions under
   fresh canonical-tenant evidence. The older preflight snapshot in
   `docs/VISUAL_GROUP_LEDGER_DRAFT.md:111-130` is not a current readback.
3. Inventory every active calendar writer and publish path, then prove the
   direct trigger and Python writer contracts together on the integrated head.
4. Run the focused PostgreSQL matrix, full relevant CI, independent SQL and
   application review, rollback-only activation transaction, and guarded
   runtime verification. None is supplied by this design document.
5. Keep all DRAFT migrations unapplied, both writer/enforcement flags OFF,
   and PR235 in draft until these gates pass. Passing static/Python checks do
   not establish source/rendition claim support.
