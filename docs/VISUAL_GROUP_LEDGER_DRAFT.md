# Persistent visual group ledger — DRAFT, unapplied

## Global once-used correction (2026-10-02, still DRAFT)

The tenant-local ledger described below does **not** satisfy the updated
all-client once-used requirement. Its `(gym_id, group_key)` primary key and
same-date sibling rule deliberately permit a different tenant to use the same
bytes. Do not run `visual_group_activate_guard` or turn on any guard for a
canary from this package alone.

`DRAFT_visual_global_history_20261002.sql` adds an attested canonical MD5
fingerprint for each tenant-local group, one global fingerprint owner/date row,
stateful member receipts and an append-only release history, an atomic claim RPC, a calendar coverage
report and an import RPC that includes per-tenant ledger history even after a
calendar row was deleted. The import refuses any occupied local group without
a verified fingerprint or any fingerprint already occupied by another tenant
or date. The new tables have service-role read access; writes pass through
SECURITY DEFINER functions. A separate settings trigger refuses `enforce=true`
even if the old tenant-local activation RPC is called; disarming stays allowed.
The integrated global activation migration must replace that blocker only after
all proof checks pass. This migration is additive and remains unapplied.

### Required before schema migration or canary

1. Inventory every calendar key, including retired keys, and bind it to a
   canonical tenant or explicitly reconcile it. The observed retired key with
   42 rows remains unresolved. Recheck live counts and mappings at migration
   time; the snapshot cited below is not live proof.
2. Resolve exact delivered media for every published, ambiguous and active
   row; backfill its tenant-local group. For each occupied group, verify bytes
   and register one canonical MD5 fingerprint. Drive `media_asset.content_hash`
   may attest a matching asset in the same tenant. Generated and externally
   hosted assets require a real byte digest plus named reviewer/source evidence.
   A URL, Drive ID, R2 key, or pHash cannot fill a missing byte digest.
3. Resolve collisions and missing history from both `visual_global_coverage()`
   and the occupied tenant-local ledger. Same-day channel siblings are allowed
   only within one canonical tenant. Another tenant cannot use the fingerprint
   even on the same date. An undated published group remains permanently
   occupied; tenant-local activation currently refuses its date gap.
4. Integrate the global claim into the calendar BEFORE trigger, all swap paths,
   confirmed delivery and release/reconciliation paths. It must be in the same
   transaction as the tenant-local claim, under a deterministic global lock
   order, with exact sibling group membership. The current draft claim RPC is
   a historical import primitive. A draft release RPC now checks the local
   released state and records a permanent receipt, but no calendar trigger calls
   either global RPC. Manual scene links across groups or tenants need one global
   component authority and a collision check before activation.
5. Replace tenant-local activation with one global barrier: lock all calendar
   writes, import historical published and in-flight claims, re-read every
   calendar row and orphaned ledger row, verify no unknown fingerprint,
   conflicting owner/date, unreconciled ambiguity or pending scene decision,
   and arm only in that same transaction. Keep enforcement OFF on refusal.
   Maintain a restore receipt for schema, data and flags; preserve published
   global history on rollback. Verify the guarded runtime revision and a
   rollback-only local transaction before a production migration or canary.

No production migration, activation, or canary is authorized by this draft.

The global activation blocker remains deliberate. Multi-hash manual scene
components are reported by `visual_global_history_coverage()` as
`scene_component_has_distinct_bytes_without_global_authority`. No global
activation transaction, calendar write barrier, component claim, or calendar
trigger integration exists yet. These are release blockers.

Owner: isolated `codex/echo-global-ledger-db-20261002` worktree. Base: PR230
`0ab9f8c`. Governing acceptance: `global-media-release-plan-20261002.md`.
All three new migrations remain `DRAFT_`; no production SQL or enforcement flag
has been applied. The application guard scaffold in PR232 remains default OFF.

## Implementation and review evidence

| Requirement | Database behavior | Real PostgreSQL coverage |
|---|---|---|
| One scene/date per gym, same-day channel siblings | Stable group row is locked before ledger INSERT, so missing-ledger races serialize. Same-date IG/FB/Story/GBP siblings can join before or after first publish. Different dates refuse the whole write. | First-use different-date race; concurrent same-date siblings; four channel siblings following publish; cross-gym isolation |
| Confirmed publish remains used permanently | Published ledger rows cannot UPDATE/DELETE. Calendar deletion preserves usage. Historical published rows with NULL `published_at` are included. A historical date unknowable from both dates becomes NULL only in published ledger state and blocks every dated reuse of that scene. | Publish/delete races; same-row DELETE winning does not fabricate confirmation; immutable ledger after calendar deletion; status-only history and unknown dates |
| Calendar mutation and reservation are one transaction | BEFORE trigger validates current exact identities, real dates and holds before approval, claim or finalize. It throws on unsafe send transitions so PR230 cannot return a token after a trigger stamps a hold. | Stale unheld approved row through unchanged PR230 RPC; finalize bypasses; null dates; held/blank Story |
| Media swaps cannot retain stale scene identity | Delivered image URL must have its own registered alias; every supplied source/byte/Drive/R2 identity must be registered and agree. A known source cannot certify an unknown delivered image on INSERT. Group key is validated against aliases. Changed media cannot rely solely on unchanged carried source aliases: a changed exact alias must confirm the same scene. Unknown pending identity is visibly held. | Stale key URL swap; carried-forward stale source asset; held row hydration |
| Bulk operation changes all siblings or none | `visual_group_swap_siblings(gym, ids, date, media_json)` requires the complete active, unsent same-date group; rejects duplicate IDs, missing/foreign/published/ambiguous rows. It locks old/replacement groups in key order. Replacement clears omitted media identity fields. `visual_group_swap_redate` is its date-only wrapper. `visual_group_swap_siblings_media(gym, rows_json, date)` accepts a distinct complete media payload per expected row (`{"calendar_row_id", "media"}` once per sibling), preserving feed vs Story image_url/source identity/format derivatives; one transaction locks the exact sibling rows, the old group and its one replacement scene in a single sorted pass, and rejects missing/extra/duplicate/stale row IDs, cross-tenant rows, mixed old group/date, conflicting registered aliases, approved/publishing/published or ambiguous rows, unresolved scene review, unrelated replacement scenes, unsupported supplied identity columns, and any occupied target group with no partial writes. The legacy shared-media form routes through the same per-row path (`visual_group_apply_media_swap`). | Partial set refusal; full redate; redate racing new candidate; conflicting media swap rollback; replacement release; distinct derivative per-row swap; missing/extra/duplicate/cross-tenant row refusal; conflict rollback; cross-date race |
| Canceled unsent releases only final sibling | Membership rows track active claims under group lock. Last unsent cancel/delete releases a reserved ledger. `publishing`, `failed`, tokens or provider IDs are treated conservatively as ambiguous. Group/sibling flags are sticky; ordinary writes cannot clear flags or release them. Evidence-only reconciliation binds original IDs and each attempt UUID, preserves other active/uncertain siblings, and makes confirmed delivery permanent. | Sibling cancellation; failed retention; orphan cleanup; timeout/wrong-ID refusal; separate GBP/Story attempts; reconciliation/delete race |
| Stable IDs/unique aliases/review decisions | Alias registration locks an absent alias before creating its group, avoiding orphan races. Existing aliases/groups and decision history cannot be mutated. Exact aliases only; latest pending scene-review events hold claims until explicit confirm/reject. | Alias race, duplicate registration, stable group, grants/sequence permissions, pHash-28 pair kept separate and explicit scene review |
| Historical/future backfill precedes activation | Dry-run default rolls back all writes. Per-row savepoint prevents partial alias bindings on conflict. Published calendar history is unchanged; published evidence is append-only. Active rows and archived/candidate ambiguous sends reserve; unresolved history remains a gym-wide activation blocker after deletion. Row readiness considers only that row/its aliases. Only a row with its own unresolved conflict is held/demoted; verified hydration clears owned visual holds and preserves unrelated holds/approval. | Dry-run/real/idempotent backfill; alias conflicts; future reservations; unknown history; unrelated approved rows; hold recovery |
| Manual same-scene union across distinct group keys | `visual_group_link_scene(gym, a, b, evidence, actor)` is service-role only and requires a named human reviewer plus non-empty evidence; it is never inferred from pHash (the JCK_6328/JCK_6331 distance-28 pair links only by manual evidence). Append-only normalized edges (`visual_group_scene_link`, a<b) are immutable, transitive (one equivalence component per canonical tenant), same-tenant only (cross-tenant/unknown keys raise), cycle-safe and idempotent (re-linking a connected pair is a no-op returning `idempotent=true`). Unions lock the union of both endpoint components in deterministic key order, serializing with reservations, publishes and reverse-order concurrent unions. Claims, swaps and confirmed deliveries treat the component as one scene: pending members share one date across channels, another date fails atomically, an ambiguous member retains the component claim, and any published member makes the component permanently used on every linked key. Group keys, aliases and published ledger/history are never reassigned, deleted or rewritten. | 2- and 3-group components; same-day siblings; published old-group/new-group refusal; cross-tenant refusal; union racing an independent different-day claim; reverse union lock order; ambiguous member retention; historical conflict reporting; linked-scene backfill hold (all pending the lead's real-PostgreSQL run) |
| Planner rebuild cannot drop repeat holds | INSERT/UPSERT without the prior group or hold still resolves known URL aliases. Later-date reuse refuses the write, preserving existing calendar/ledger. | Swift River/GBP-style rebuild and UPSERT regression |

## Canonical tenant alias identity (PR235 package, 2026-10-02)

Source truth: `work/media-tenant-alias-preflight-20261002.md` — 124 current
calendar keys (8,136 rows) each map to exactly one tenant UUID via
`echo_social_intake` / `echo_intake_tokens` / `gyms`; one retired key
(`zz-retired-20260904-f574c06c`, 42 rows) is unmapped and unresolved.

- New service-role-only registry `public.tenant_alias(alias_key PK,
  tenant_id uuid)`: one row per calendar alias key; several keys (old/current
  aliases of one real tenant, e.g. Swift River's
  `swiftrivercrossfitd23567` + `swiftrivercrossfite5c9db`) share one canonical
  tenant UUID. Bindings are immutable (identity trigger) and registration is
  advisory-locked, idempotent for the same tenant, and raises on re-bind to a
  different tenant (ambiguity fails closed). Registration seeds the tenant
  UUID as its own alias key for canonical pass-through.
- `public.visual_group_tenant_id(key)` resolves a raw calendar key to its
  canonical tenant UUID, returning NULL (never raising) for unmapped keys;
  `public.visual_group_tenant_strict(key)` raises on unmapped keys and is used
  by every minting/mutation path (alias registration, backfill, swaps,
  reconciliation).
- ALL internal visual-group tables (groups, aliases, events, usage ledger,
  siblings, guard settings, reconciliation) are keyed by the canonical tenant
  UUID. `content_calendar.gym_id` keeps its raw alias key and existing
  portal/client behavior is unchanged — no production reads change because
  enforcement remains OFF and every table is new.
- Consequence: old and current aliases of one real tenant share one
  date-conflict authority (a second alias cannot re-use the scene on a
  different date), while unrelated tenants stay fully isolated even with
  byte-identical media URLs.
- Fail closed: unmapped keys (including the retired key) cannot arm
  enforcement (`gym_visual_guard_settings_arm_guard` trigger rejects
  `enforce=true` for unknown tenants), cannot register aliases or mint groups,
  and make backfill raise before any write. With enforcement OFF their
  calendar rows pass through untouched and nothing is ever minted.
- Real PG coverage: cross-alias shared date authority + same-date siblings,
  cross-alias concurrent first reservation (one winner), unrelated-tenant
  isolation with identical media, alias registration conflict/idempotence/
  race, tenant-alias immutability, and retired-key refusal across arming,
  registration and backfill plus coverage-report visibility.

## Migration order and contracts

1. `DRAFT_visual_group_schema_20261002.sql`: stable groups, immutable unique
   per-gym aliases, append-only decision events, usage ledger, default-OFF gym
   settings, nullable calendar key; registration/confirmation/rejection RPCs.
2. `DRAFT_visual_group_claim_trigger_20261002.sql`: membership table, exact row
   identity helpers, transactional trigger, complete sibling swap/redate RPCs,
   the per-row distinct-derivative sibling media swap RPC with its shared
   per-row replacement helper, evidence-only ambiguity reconciliation RPC and
   exact attempt bindings.
3. `DRAFT_visual_group_backfill_20261002.sql`: default dry-run backfill and
   read-only coverage/conflict report. Backfill refuses an enforced gym.

Tables have explicit service-role privileges and RLS; anon/authenticated have
no table or RPC privileges. Service-role receives event identity-sequence usage.
Trigger functions run as owner. Existing PR230 claim/approval functions are
applied unchanged in local tests and remain unchanged in these migrations.

`reserved_date` may be NULL **only** for permanently published historical usage
whose calendar/publish date cannot be recovered. Normal active reservations
require a real date. Unknown identity review events may have a NULL group key;
other event actions require a known group. No dates or identities are invented.

Exact aliases support canonical URL, source asset, Drive ID, byte hash, R2 key
and manually confirmed scene labels. URLs retain query strings because Drive
queries can identify different files. No pHash value creates an automatic merge.
The JCK_6328/JCK_6331 distance-28 pair stays distinct until explicit human scene
confirmation. A separate candidate review decision must be persisted with the
candidate's exact alias for the trigger to enforce it.

## Local verification

The real tests use installed `psql`, independent subprocess connections, and a
faithful fixture of the touched production columns (including `post_date`,
`account`, status/variant constraints and claim ownership fields). The only
allowed DSN is an absolute Unix socket host plus the explicitly disposable
`echo_visual_ledger_test` database. Public schema is recreated there once per
run. Tests skip when that DSN is absent; CI needs this local PostgreSQL setup to
exercise concurrency, and skip is never acceptance evidence.

The original draft test's claim that publication always wins against deletion
of the same row was false: DELETE can commit first and UPDATE can affect zero
rows. The repaired test conditions permanence on a confirmed UPDATE; a separate
race finalizes one sibling while deleting another and requires permanent usage.

Initial focused run: **87 passed in 6.20s** (29 real PostgreSQL cases, five
static release-boundary checks, and existing Story/approval/duplicate-claim
regressions). PostgreSQL 17.11 was restarted after the run: all 30 ledger rows,
including eight permanently published rows, retained the same full-row
fingerprint. The task-owned server was then stopped; local data and the
verification receipt are retained for independent review. No production DB was
connected by these checks.

## Independent review repair

Three P1 findings were repaired after commit `805a89a`: uncertainty could be
cleared by changing status/markers; INSERT could accept an unknown delivered URL
through a known source; and backfill skipped ambiguous inactive variants.
The repair persists sticky ambiguity in the ledger and sibling table, refuses
ordinary clear/release/reset writes, requires a verified delivered URL plus all
consistent aliases, and includes ambiguous archived/candidate rows in backfill.
Known GBP/Story derivative aliases may share one scene/date; unknown delivered
media remains held. Unknown failed results also preserve the prior scene claim
and append durable uncertainty evidence before calendar deletion.

After these repairs, **98 focused tests passed in 7.87s**, including 40 real
PostgreSQL cases. A fresh server restart retained the complete ledger, sticky
sibling flags and append-only event fingerprints unchanged; the local server
was stopped again with review data retained.

### Backfill P1 repair (2026-10-02, draft)

Two further P1 findings against `DRAFT_visual_group_backfill_20261002.sql`
were repaired in the backfill lane only:

- **P1-1 (whole-gym abort).** A historical published row meeting an ambiguous
  reserved ledger scene on a different day could make the ledger UPDATE raise
  the immutable ambiguous identity/date (or sticky-ambiguity) trigger outside
  any per-row savepoint, aborting the entire gym backfill and rolling back all
  preceding rows. All ledger/sibling/calendar mutations now run inside a
  second per-row savepoint (alongside the existing alias savepoint), and the
  hold-path calendar demote write is itself savepoint-wrapped because the
  guard trigger refuses identity/status changes on unreconciled ambiguous
  rows. Any such raise becomes a held `review_hold` member event for that row
  only; preceding rows are never lost.
- **P1-2 (fabricated publication).** The backfill previously promoted an
  ambiguous reservation straight to permanently `published` without provider
  confirmation evidence. It now preserves the original ambiguous reservation
  untouched and appends an evidence-bearing `review_hold` event
  (`actor='backfill_ambiguous_review'`,
  `reason='ambiguous_reservation_needs_provider_confirmation'` plus the row's
  claim token, provider post ID, image URL and date) unless
  `visual_group_group_reconciled` confirms evidence-based reconciliation.
  Held rows never receive a `confirmed`/`backfill_published` evidence event.
  Confirmed (non-ambiguous or reconciled) historical published rows still
  finalize permanently as before; published calendar history is never edited.

Behavioral invariants are unchanged: dry run remains the default and rolls
back all writes, the function is idempotent (repeat runs add no duplicate
events), enforcement stays OFF and is never toggled, and execute remains
service-role only.

**P2 independent-review repairs (backfill lane, 2026-10-02):** (4) both
per-row savepoints and the hold-write savepoint also catch `unique_violation`,
so one row's unique conflict (e.g. an alias-registry race) becomes a held
review event instead of aborting the whole gym backfill. (5) any held row
whose group reservation or own markers are ambiguous is written with
`v_amb_hold` (`actor='backfill_ambiguous_review'`, claim-evidence JSON), so it
is visible to the report's `unresolved_ambiguous_history` filter even when the
block itself was an alias/scene/date conflict. (6) `p_gym_id` is trimmed once
before tenant resolution and the calendar scan, so a whitespace-padded key no
longer silently backfills zero rows while reporting success. (8) the report's
`unresolved_published_history` and `unresolved_ambiguous_history` now keep only
the latest unresolved hold per row (`alias_value`), so repeated or superseded
holds do not double-count a row. (7) regression tests assert the
`historical_cross_date_visual_repeat` backfill report reason and the
`cross_date_conflicts` report output, plus the cross-alias published-conflict
hold counting once in unresolved history. Static PL/pgSQL block-balance checks pass; live PostgreSQL
validation is pending on the integration lead (this lane's sandbox blocks the
local server socket/shared memory).

Per-sibling media replacement payloads that preserve feed/Story derivatives
are now implemented in this lane by `visual_group_swap_siblings_media` (above),
with the legacy shared-media RPC routed through the same owner-only per-row
replacement helper; date-only redate is unchanged. All derivatives must resolve
to one common scene. The public service-role wrappers validate and lock the
complete unsent, unapproved sibling set; direct helper execution is denied. The backfill-owned conflict report no longer lists this item in
`activation_blockers` (removed by the backfill lane on 2026-10-02 after the
independent dual-child review); `activation_ready` remains `false` for the
remaining blockers.

### Per-sibling RPC independent repair

The initial builder edits failed two real PostgreSQL cases because a PL/pgSQL
variable conflicted with a SQL column name. Independent inspection also found
an executable internal helper bypassing wrapper checks, old-before-target group
lock inversion, and permission to split one old scene into unrelated new scenes.
These are repaired: the helper is owner-only, old and replacement groups lock in
an explicit sorted key loop with one row-lock statement per key before
full-membership/readiness validation, every derivative
resolves to one replacement scene, and non-null identities absent from the real
calendar schema refuse instead of being silently ignored. Pending scene review
also refuses the entire swap under the group locks.

The operation preserves each row's account and format, supplies its own delivered
URL/source lineage, and clears omitted media identity columns. It accepts only
unoccupied/released replacement groups (or its own old scene); joining an already
occupied scene or splitting scenes is outside this operation. Ordinary same-date
channel INSERTs remain governed by the existing trigger. Calendar, sibling and
ledger writes roll back together on every refusal.

Focused verification: **139 passed in 17.64s**, including 81 real PostgreSQL
cases. Service wrappers/private-helper permissions, derivative lineage and
omitted identity clearing, common-scene enforcement, pending review, unsupported
identity columns and actual optional-column types, rollback, opposite swaps
through both RPCs under alternate planner settings (4 cases, 16 races),
cross-date claims and concurrent
new sibling membership are exercised. Restart fingerprints for all 88 ledger
rows, sibling state, events and 14 reconciliation receipts matched exactly. The
existing local PostgreSQL cluster is stopped and retained; receipt:
`work/visual-ledger-pg-local/per-sibling-sorted-loop-verification-receipt.json`. No new cluster
was initialized and no production SQL or flag was applied.

The following integration requirements remain **unimplemented release blockers**:

- Safe union/redirect for separate, already-bound scene groups, preserving all
  stable aliases, reservations, permanent usage and audit evidence. The current
  registration/confirmation RPC rejects rebinding; it does not implement union.
- A transactional activation write barrier and coverage recheck, including
  concurrent INSERT/UPSERT against the final backfill/enable boundary. The
  current backfill snapshot and direct settings toggle do not provide this.

`visual_group_conflict_report` therefore always returns `activation_ready=false`
and lists these blockers. A green local suite cannot authorize production
activation, even for an otherwise covered gym. No union shortcut is provided.

## F1/F2 reconciliation and row hold repair

`visual_group_reconcile_ambiguous(gym, row_id, outcome, group, date, evidence,
actor)` is available only to `service_role`. Its owner writes immutable receipts;
service role can SELECT the receipt table but cannot directly insert or mutate
it. `confirmed_not_sent` cancels a live row and releases its membership, including
deleted or archived orphans, only after conclusive terminal non-delivery.
`confirmed_published` records permanent usage even when the calendar row was
deleted. A live row becomes published when its exact aliases and scene review
are ready; otherwise the ledger remains permanent and `calendar_updated=false`
signals that calendar review is still required.

The evidence contract requires `source=provider_terminal_readback`, exact gym,
row, group and calendar date, provider/request ID/receipt reference, checked time,
`terminal=true` and `will_retry=false`. Non-delivery must be explicitly
`failed_before_delivery` or `canceled_before_delivery`; timeout, not-found,
unknown delivery and possible retry all refuse. A confirmed delivery must bind
the registered delivered URL, original provider ID and confirmed publish time.
`claims` must equal every current ambiguous membership's group, attempt UUID,
original claim token, original provider post ID and image URL. `hold_claims`
must equal each unresolved unknown-history event ID and its original markers,
date and image. Missing recoverable original claim/provider IDs refuse release
and require separate evidence recovery. This draft offers no blanket unlock.

Receipts are idempotent for the exact evidence and original attempt. A later
attempt receives a new UUID; an old receipt cannot release it. Row/group locks
serialize live reconciliation with ordinary writes, and a per-row advisory lock
serializes receipts for unknown deleted orphans. Each uncertain sibling requires
its own evidence; resolving one cannot release another. Permanent published
usage cannot be reclassified as unsent.

**Provider trust boundary:** SQL validates and binds the supplied receipt but
does not contact or authenticate the provider. The trusted service verifier must
fetch and retain actual authoritative terminal readback conclusively proving
delivery or non-delivery for the original request, including that it cannot
retry. The local tests use synthetic fixtures; provider adapter wiring and live
evidence verification remain release gates.

F2 removes gym-wide historical review events from per-row readiness. Unresolved
history still blocks activation in the coverage report. Verified backfill can
clear its own stale identity/date/scene/repeat holds without touching another
readiness reason or silently restoring a previously demoted approval. Unrelated
approved rows stay approved; repeat backfill does not add duplicate events.

F1/F2 focused result: **122 passed in 13.86s**, including 64 real PostgreSQL
cases and the unchanged PR230 Story/approval/duplicate-claim regressions. The
fresh PostgreSQL 17.11 restart retained all 61 ledger rows (12 published), sibling
attempts, append-only history and 14 immutable receipts with identical full-row
fingerprints. The local server is stopped; data and
`work/visual-ledger-pg-local/f1f2-verification-receipt.json` remain for review.
No production connection or provider readback was performed by these tests.

## Remaining release gates and risks

- Fresh independent code/SQL review, integrated CI, all-gym alias coverage and
  shadow conflict review, explicit production migration approval, per-gym canary
  and live concurrent claim verification remain required. No A/A+ or live-ready
  claim is made by this builder.
- Canonical tenant mapping is a separate fleet integration prerequisite. Swift
  River's historical/current text keys must be mapped from authoritative intake
  and token relationships using `agent/echo_clients.py` assembly, with ambiguous
  or unknown mappings held. This migration never infers tenant links by names.
  Historical and current aliases must enter the same canonical tenant ledger
  before activation; current-key-only coverage is insufficient.
- Portal/planner bulk swaps/redates must call the new RPC. Existing callers are
  not wired by this SQL lane. The existing in-process PR232 ledger is not the
  authoritative transaction or production concurrency guarantee.
- Perceptual classifier hydration, all-gym exact identity inventory, manual
  reconciliation of aliases already assigned to different immutable groups,
  provider verifier integration for evidence-based resolution of ambiguous
  sends, and portal display of holds are separate integration work. A pHash review candidate must persist its
  hold decision; providing an unreviewed group key does not certify similarity.
- Ambiguous sends retain reservations conservatively, including deletion. This
  layer does not offer a privileged release-without-evidence shortcut.
- Live service-role/owner access is trusted administrative authority. Immutable
  usage/event triggers prevent ordinary mutation; owner DDL, disabling triggers
  or deleting production schema is outside this data-path guarantee.

## Rollback

Disable gym enforcement first. Preserve permanent usage and decision evidence.
Remove the calendar trigger and new RPC/helpers in dependency order, then the
sibling table and additive schema only when nothing relies on it. Removing a
function is reversible; deleting historical ledger data is not an acceptable
rollback after activation. Do not DROP PR230 functions or weaken its guards.

### Manual same-scene union package (2026-10-02, draft)

Ground truth: `global-media-release-plan-20261002.md` line 50 ("Safe scene
group union ... remain missing") and the draft ledger code in this lane.

- `migrations/DRAFT_visual_group_schema_20261002.sql` gains the append-only
  `visual_group_scene_link` edge table (normalized a<b, both ends FK to
  `visual_group`, immutable trigger coverage, RLS service-role only), the
  recursive cycle-safe `visual_group_scene_members` component helper, and the
  guarded `visual_group_link_scene` RPC. The RPC demands a non-blank human
  actor and non-empty evidence object, refuses identical/unknown/foreign
  (cross-tenant) group keys, locks the union of both endpoint components in
  key order with a bounded recompute loop, inserts one edge plus a
  `scene_linked` member event (new allowed `action` value) on first link only,
  and returns the merged component, any existing cross-date conflicts,
  published and ambiguous member keys. Nothing is ever inferred from pHash.
- `migrations/DRAFT_visual_group_claim_trigger_20261002.sql` extends the
  claim path (`visual_group_sync_row`), both swap paths
  (`visual_group_apply_media_swap`) and confirmed-delivery reconciliation with
  component-wide date authority under sorted group locks: a non-released
  ledger row on ANY linked member pins the whole component to one date; an
  ambiguous member retains that claim; a published member makes the scene
  permanently used on every key. Unlinked groups take the identical code path
  with a singleton component, so prior behavior is unchanged.
- `migrations/DRAFT_visual_group_backfill_20261002.sql` holds new unsafe rows
  whose date disagrees with a linked member's active/published claim
  (`linked_scene_cross_date_hold`, one per-row held review event inside the
  existing savepoint), and `visual_group_conflict_report` gains
  `scene_cross_date_conflicts` (per-component dates/states, NULL historical
  dates counted) while `activation_ready` stays false and the former
  `safe scene union/redirect` blocker becomes `owner review of reported scene
  cross-date conflicts`. Existing cross-date history is reported, never
  rewritten.
- All three migrations remain DRAFT/unapplied with enforcement default OFF;
  no flags are armed and no production SQL was run.
- Verification in this lane: 9 static release-boundary checks pass; the 10 new
  real-PostgreSQL cases (2-group shared date/other-date refusal, 3-group
  transitivity and cycle-safe idempotent re-link, published old-group/new-group
  refusal and ledger immutability, union-vs-different-day-claim race with
  conflict-report fallback, reverse-order union race without deadlock,
  ambiguous-member claim retention, historical cross-date conflict reporting
  with activation blocked and no rewrite, backfill linked-scene hold with
  idempotent repeat) are written but UNRUN here: this sandbox denies the local
  server's shared-memory call, so the lead runs the live suite.
