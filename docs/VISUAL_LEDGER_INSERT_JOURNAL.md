# Visual Ledger INSERT Observation Journal (DRAFT, 2026-10-04)

Migration: `migrations/DRAFT_visual_ledger_insert_journal_20261004.sql`
Tests: `tests/test_visual_ledger_insert_journal_pg.py`

## What it is

An immutable, append-only **observation journal** for INSERTs into
`public.visual_group_usage_ledger`. For every newly inserted ledger row it
records, in the same transaction, the exact raw ledger primary key,
`reserved_date`, raw `calendar_row_id`, `channel`, `state`, and an immutable
JSONB snapshot of the row exactly as inserted.

## What it is NOT

- **Not original-use evidence.** A journal event proves only that a ledger row
  was inserted and what it contained at that instant. It says nothing about
  whether the media was originally used by this tenant.
- **Not provider-byte clearance.** No provider bytes, fingerprints, or URLs are
  read, compared, or cleared here.
- **Not a coverage/activation/import change.** No coverage evaluator,
  activation function, import interpretation, or writer is touched. A test
  (`test_post_install_importer_and_evaluators_unchanged`) proves every
  preexisting public function is byte-identical after installation.
- **Not retroactive.** Existing fingerprint-free history remains unresolved;
  rows predating the journal keep `observed_insert_event_id IS NULL` forever.
  There is deliberately no backfill.
- Nothing in this design uses `xmin`, wall-clock time, or current media as
  evidence of original use.

## Design

1. **Column.** `visual_group_usage_ledger.observed_insert_event_id uuid` —
   nullable, no default, no backfill.
2. **Event assignment (BEFORE INSERT).**
   `visual_group_ledger_assign_insert_event()` overwrites
   `NEW.observed_insert_event_id` with a fresh `gen_random_uuid()`. A
   caller-supplied UUID is discarded: the ID can never be caller-selected or
   reused.
3. **Journal write (AFTER INSERT).**
   `visual_group_ledger_record_insert()` inserts one row into
   `public.visual_group_insert_journal` in the same transaction. If the
   surrounding claim fails and rolls back, both the ledger row and its journal
   row vanish.
4. **Binding without circular insertion.**
   `visual_group_insert_journal` has **no foreign keys** — not to the ledger
   (survives ledger deletion; no circular insert), not to `content_calendar`
   (survives calendar deletion; `calendar_row_id` is a raw preserved value).
   The only FK is `ledger.observed_insert_event_id -> journal.event_id`,
   `DEFERRABLE INITIALLY DEFERRED`: the AFTER-trigger journal write lands in
   the same statement and the FK validates at commit.
5. **Update semantics.** `visual_group_ledger_event_id_immutable()`
   (BEFORE UPDATE) rejects any change to `observed_insert_event_id`,
   including NULL-ing. Updates create no event. Delete + reinsert of the same
   `(gym_id, group_key)` creates a **new** event; both journal rows are kept.
6. **Caller lockout.** RLS enabled; `revoke all` from public/anon/
   authenticated/service_role; `grant select` to service_role only. Trigger
   functions are `SECURITY DEFINER` with pinned `search_path = public`, so
   journal writes ride on the migration owner's privileges, never the
   caller's. A belt-and-suspenders trigger rejects UPDATE/DELETE/TRUNCATE even
   for the table owner (superuser/table-owner privilege can still bypass via
   `session_replication_role` — inherent to PostgreSQL; scratch-test cleanup
   uses exactly that).
7. **Trigger-function lockdown.** `EXECUTE` is revoked from PUBLIC, anon,
   authenticated and service_role on all four trigger functions (trigger
   firing never checks EXECUTE, so the real triggers are unaffected), and
   every function verifies `TG_RELID`/`TG_OP`/`TG_WHEN`/`TG_LEVEL` against its
   exact bound relation, operation, timing and level. A caller therefore
   cannot invoke the functions directly, cannot attach them to a same-shaped
   TEMP table (no EXECUTE), and even the table owner gets a guard error if
   such an attachment is forced — no forged journal rows.

## Journal schema

| column | meaning |
|---|---|
| `event_id` uuid pk | server-generated event UUID, equals the ledger row's `observed_insert_event_id` |
| `ledger_gym_id`, `ledger_group_key` | raw ledger PK, verbatim (aliases never canonicalized) |
| `reserved_date` date | raw value as inserted |
| `calendar_row_id` uuid | raw value as inserted; **no FK**, survives calendar deletion |
| `channel`, `state` | raw values as inserted |
| `insert_snapshot` jsonb | `to_jsonb(NEW)` minus the event column — immutable original INSERT snapshot |
| `recorded_at` timestamptz | audit metadata only; not evidence of anything |

## Test coverage

`tests/test_visual_ledger_insert_journal_pg.py`:

- **10 static source contracts** (always run): single-transaction draft;
  nullable column with no backfill; unconditional server UUID assignment;
  AFTER-INSERT raw snapshot recording; no journal FK to ledger/calendar and
  deferred ledger→journal binding; update immutability with no event
  creation; caller lockout (RLS, revoke, select-only grant, truncate
  trigger); EXECUTE revoked from PUBLIC/anon/authenticated/service_role on
  all four trigger functions with no role grant; every trigger function
  guards `TG_RELID`/`TG_OP`/`TG_WHEN`/`TG_LEVEL` against its exact bound
  source; observation-only wiring (no activation/coverage/import function
  touched, no `xmin`/clock/media evidence semantics).
- **18 disposable-PostgreSQL scenarios** (gated on
  `VISUAL_LEDGER_JOURNAL_TEST_DSN` naming a Unix-socket DB literally named
  `echo_insert_journal_test`, same house rule as the scene ledger gate
  tests), run against the FULL draft stack in dependency order (schema,
  claim trigger — which defines visual_group_row_active(content_calendar)
  that global history's SQL-body functions reference at creation time —
  global history/importer, backfill, activation, claim wave, history
  backfill, ledger coverage gate): preexisting row seeded BEFORE the migration stays NULL;
  update creates no event / cannot change the ID; new exact insert; attempted
  caller UUID discarded; same-transaction update keeps original snapshot;
  failed-claim rollback leaves nothing; an ACTUAL failing
  `visual_global_claim` through the full stack rolls back both ledger row and
  journal event; `SET CONSTRAINTS ALL IMMEDIATE` validates the deferred
  ledger→journal FK in-transaction; ordinary ledger DELETE is rejected by the
  preexisting base permanence guard; privileged scratch-only deletion
  retention across ledger AND calendar delete; privileged scratch-only
  same-key reinsertion creates a new event; raw alias values preserved
  verbatim; trigger functions unforgeable (EXECUTE revoked, TEMP-table
  attach denied for non-owner, owner-forced attach rejected by the
  TG_RELID/OP/WHEN/LEVEL guard) with positive authorized-writer control;
  direct journal mutation/truncate rejected as service_role and append-only
  for owner; post-install importer/coverage/activation/claim function
  definitions byte-identical (full `pg_get_functiondef` keyed by
  schema/name/identity args) with a strictly additive complete-catalog delta;
  negative controls prove body/column/constraint/policy changes are detected
  and the draft re-applies to the original baseline; rollback-only install
  leaves the complete catalog (functions, relations, columns, constraints,
  policies, grants, triggers) byte-identical.

**Execution status (stated, not hidden):** the originating Codex lead ran the
complete suite on 2026-10-04 against disposable PostgreSQL 17.11
(`echo_insert_journal_test` on a local Unix socket): **28 passed in 4.01s**,
including 10 static contracts and 18 PostgreSQL scenarios. The Kimi repair
worker's sandbox could not connect to that socket (EPERM), so this real run
was performed outside that sandbox with

```
PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH \
VISUAL_LEDGER_JOURNAL_TEST_DSN='dbname=echo_insert_journal_test host=/tmp/echo_insert_journal_sock_20261004 port=55443 user=blakeruff' \
python3 -m pytest tests/test_visual_ledger_insert_journal_pg.py -q
```

The passing local run establishes the documented draft behavior in the scratch
schema. It does not establish compatibility with the live production schema,
original-use provenance, historical clearance, or authorization to apply this
migration.

## Status

DRAFT. Not committed to any shared branch, not pushed, not applied to
production.
