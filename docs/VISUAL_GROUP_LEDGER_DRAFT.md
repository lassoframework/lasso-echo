# Persistent visual group ledger — DRAFT, unapplied

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
| Media swaps cannot retain stale scene identity | Group key is validated against aliases. Changed media cannot rely solely on unchanged carried source aliases: a changed exact alias must confirm the same scene. Unknown pending identity is visibly held. | Stale key URL swap; carried-forward stale source asset; held row hydration |
| Bulk operation changes all siblings or none | `visual_group_swap_siblings(gym, ids, date, media_json)` requires the complete active, unsent same-date group; rejects duplicate IDs, missing/foreign/published/ambiguous rows. It locks old/replacement groups in key order. Replacement clears omitted media identity fields. `visual_group_swap_redate` is its date-only wrapper. | Partial set refusal; full redate; redate racing new candidate; conflicting media swap rollback; replacement release |
| Canceled unsent releases only final sibling | Membership rows track active claims under group lock. Last unsent cancel/delete releases a reserved ledger. `publishing`, `failed`, tokens or provider IDs are treated conservatively as ambiguous; deletion keeps membership and reservation. | Sibling cancellation and failed/ambiguous retention; variant archive/reactivation; draft reservation |
| Stable IDs/unique aliases/review decisions | Alias registration locks an absent alias before creating its group, avoiding orphan races. Existing aliases/groups and decision history cannot be mutated. Exact aliases only; latest pending scene-review events hold claims until explicit confirm/reject. | Alias race, duplicate registration, stable group, grants/sequence permissions, pHash-28 pair kept separate and explicit scene review |
| Historical/future backfill precedes activation | Dry-run default rolls back all writes. Per-row savepoint prevents partial alias bindings on conflict. Published calendar history is unchanged; published evidence is append-only. Active rows reserve, later dates receive a durable hold and approved rows become pending. Unknown historical identity remains a durable gym-wide review blocker even after row deletion. | Dry-run/real/idempotent backfill; alias conflicts; future reservations; status-only publications; unknown events; deletion retaining historical blocker |
| Planner rebuild cannot drop repeat holds | INSERT/UPSERT without the prior group or hold still resolves known URL aliases. Later-date reuse refuses the write, preserving existing calendar/ledger. | Swift River/GBP-style rebuild and UPSERT regression |

## Migration order and contracts

1. `DRAFT_visual_group_schema_20261002.sql`: stable groups, immutable unique
   per-gym aliases, append-only decision events, usage ledger, default-OFF gym
   settings, nullable calendar key; registration/confirmation/rejection RPCs.
2. `DRAFT_visual_group_claim_trigger_20261002.sql`: membership table, exact row
   identity helpers, transactional trigger, complete sibling swap/redate RPCs.
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

Final focused run: **87 passed in 6.20s** (29 real PostgreSQL cases, five
static release-boundary checks, and existing Story/approval/duplicate-claim
regressions). PostgreSQL 17.11 was restarted after the run: all 30 ledger rows,
including eight permanently published rows, retained the same full-row
fingerprint. The task-owned server was then stopped; local data and the
verification receipt are retained for independent review. No production DB was
connected by these checks.

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
  explicit evidence-based resolution of ambiguous sends, and portal display of
  holds are separate integration work. A pHash review candidate must persist its
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
