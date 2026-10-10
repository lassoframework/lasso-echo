# Exact-Byte Historical Seed Preparation (20261010)

Operator runbook for `scripts/exact_byte_history_seed_20261010.py`
(library: `agent/exact_byte_history_seed_20261010.py`), which prepares the
trusted-operator historical seed for
`migrations/delivered_byte_send_fence_20261010.sql`.

This tool **prepares only**. It never executes generated SQL, never activates
the gate, never writes to any table, never reads or prints secret environment
values, and never treats a database-published URL fetch as provider receipt or
platform readback evidence. Raw media URLs are never printed to stdout/stderr.

**Artifact privacy.** The manifest and seed SQL contain source media URLs and
`content_calendar` row ids. `--out` must be a NEW owner-only directory (mode
`0700`); artifact files are written mode `0600` with exclusive create. The
tool fails before any write if the output path already exists and is not an
empty directory, so a blocked rerun can never leave stale usable SQL beside an
incomplete manifest. An existing empty directory is accepted and forced to
`0700`.

## What it does on every execution

1. Opens one read-only REPEATABLE READ snapshot and takes a **fresh, complete,
   keyset-paginated census** (`id` order, full pages required) of every
   `content_calendar` row with `status='published'` OR `published_at`
   non-null OR `late_post_id` non-null. No census is bundled; the 1,467-row
   Oct 10 05:32 UTC observation is enforced as a **fail-closed floor that
   callers may raise but never lower** (`--baseline-min` below 1467 is
   clamped up to 1467).
2. Reads each row's revision from the SQL authority
   `exact_byte_row_revision_20261010(c)` and the snapshot digest from
   `exact_byte_historical_snapshot_20261010()`, then recomputes the snapshot
   locally from the frozen (row id, revision) pairs. Any mismatch blocks.
3. Inventories **every** media occurrence per row: `image_url` and
   `thumbnail_url` when present. Occurrences are never collapsed to one
   URL/digest. Gallery/video/alternate media fields (`image_urls`,
   `slide_urls`, `media_urls`, `carousel_urls`, `media_items`, `gbp_media`,
   `video_url`) block the run unless the value is literally null, an empty
   array, or an empty string — an empty object `{}` is **not** empty and
   blocks.
4. Fetches each URL read-only: HTTPS only, exact host allowlist
   (`--allowed-host`), no redirects (a 3xx response blocks), global-IP DNS
   check (SSRF), hard 128 MiB and 30 s bounds, and streamed bytes are
   re-counted against the bound even when `Content-Length` lies. Each fetch
   becomes one `observation_kind='db_published_url_bytes'` row.
5. Applies the explicit mapping input only. Tenant, posting timezone and
   provider targets come solely from the mapping JSON with `evidence_ref`;
   nothing is inferred. Sibling identity is admitted only when ALL hold:
   * each member row's persisted `content_calendar.logical_post_id`
     (migration `migrations/logical_post_id_20261004.sql`) equals the proof's
     `logical_post_id`,
   * the proof's canonical tenant and post_date equal the row's,
   * the proof explicitly lists its precise member row ids
     (`member_row_ids`) and that set exactly equals the `sibling_members`
     input for that proof (no partial, no extra ids),
   * at least two distinct member rows exist — singleton "sibling" proofs
     are rejected.
6. Requires a **reviewed census assertion** (`census_review`) binding a
   nonblank reviewer `evidence_ref` to the CURRENT
   `historical_snapshot_sha256` and the exact fresh census row count.
   Without it, or with a stale digest or count mismatch, the run blocks: no
   `COMPLETE_UNAPPLIED` artifact is emitted. The separate per-row
   `no_media_evidence` requirement is unchanged.
7. Emits deterministic artifacts into `--out`:
   * `exact_byte_history_seed_20261010.sql` (insert authority tables only;
     never the gate, cutover, attempts, or `content_calendar`),
   * `exact_byte_history_readback_20261010.sql`,
   * `exact_byte_history_seed_manifest_20261010.json` (census counts, the
     reviewed census assertion, **every observation occurrence** — row
     id/revision, image vs thumbnail role, exact URL, SHA-256, byte length,
     `db_published_url_bytes` kind, evidence ref — plus SHA-256 checksums of
     the seed and readback outputs, frozen snapshot/corpus digests, and
     before/apply/readback instructions).

## Seed SQL transaction and guards

The generated seed runs as ONE READ COMMITTED transaction. Any raised
exception rolls back ALL inserts — there is no partial-seed state to resume
from, and blind retry onto non-empty tables is refused. Lock discipline,
applied **before** any lock is taken:

* `set local lock_timeout = '5s';` and `set local statement_timeout = '15min';`
  bound every lock wait and statement in the transaction (fail closed, no
  unbounded wait).
* `pg_advisory_xact_lock(hashtextextended('exact_byte_send_20261010', 0))`
  takes the SAME transaction advisory lock used by the migration's
  send/activation path (`exact_byte_seed_lock_20261010`,
  `exact_byte_authorize_send_20261010`, `exact_byte_activate_20261010`, …).
* `lock table public.content_calendar in share mode;` then freezes the
  historical snapshot against concurrent row updates before the pre-guard
  reads it.

The order — advisory lock first, then the SHARE table lock — is exactly the
order of `exact_byte_activate_20261010`, so seed apply serializes with
activation/send instead of deadlocking: both contend on the advisory lock
first, so the SHARE lock and the send path's row-level `FOR UPDATE` never
interleave across the two paths. The seed's own inserts re-acquire the same
advisory lock via the `seed_lock` trigger, which is re-entrant in-session.

**Operator privilege requirement:** the applying login must inherit
`exact_byte_owner_20261010` (insert authority on the seed tables) AND hold
SELECT and UPDATE on `public.content_calendar` through a separately controlled
operator role. PostgreSQL 17 requires UPDATE (or a broader write privilege)
for `LOCK TABLE public.content_calendar IN SHARE MODE`; SELECT alone fails even
though the seed never updates calendar rows. Verify these privileges before
the seed transaction. Do not grant UPDATE to `exact_byte_owner_20261010` or
leave an ad hoc operator grant in place after the run; record the operator
identity and any temporary grant/revocation in the release receipt.

Guards:

* **Pre-apply:** `exact_byte_historical_snapshot_20261010()` must equal the
  frozen digest, and every exact-byte seed table must be empty
  (expect-zero checks).
* **Post-apply (before commit):** per-table row counts must equal the frozen
  manifest counts, `exact_byte_corpus_digest_20261010()` must equal the
  generated digest, `exact_byte_history_complete_20261010()` must be true,
  and the historical snapshot must be unchanged.

The readback SQL is read-only and **raises** on any digest/count/history
mismatch and on any non-empty expect-zero check (revision drift, coverage
outside the historical predicate, orphan observations, non-DB-published
observation kinds, unknown IANA timezones).

## Gate permission constraint (important)

`exact_byte_owner_20261010` has **no SELECT privilege** on
`exact_byte_gate_20261010` under the migration. Neither the seed SQL nor the
readback SQL reads or changes the gate, and they cannot verify its state.
Gate-OFF assurance is a **separate, independently privileged, read-only
operator check**:

1. **Before apply**, a privileged operator runs
   `select enabled from public.exact_byte_gate_20261010;` (must read
   `false`) and records the receipt.
2. **After apply/readback**, the same privileged read is repeated and the
   second receipt recorded.

Keeping the gate OFF remains a separate release prerequisite. This worker
took no production action; the gate reads above are operator steps, not
something this tool performs.

## Inputs

* Mapping JSON: `tenants`, `targets`, `sibling_proofs` (each with explicit
  `member_row_ids`), `sibling_members`, `no_media_evidence`,
  `observation_evidence_ref`, `census_review`
  (`historical_snapshot_sha256`, `census_row_count`, `evidence_ref`). The
  census-review digest/count must come from a current read-only query of
  `exact_byte_historical_snapshot_20261010()` and the census predicate; a
  stale value blocks the run. Every entry carries an `evidence_ref` pointing
  at auditable evidence held outside this repo. Coverage rows for rows WITH
  image/thumbnail observations bind the `census_review` `evidence_ref` (the
  reviewed census assertion), never the generic `observation_evidence_ref`;
  the generic ref binds only the individual observation rows. No-media
  coverage rows keep their per-row `no_media_evidence` ref.
* DB access via libpq environment (e.g. PGSERVICE) for a login inheriting
  `exact_byte_owner_20261010` with read on `content_calendar`. The tool sets
  `read only` on its transaction; it has no write path.

## Fail-closed blockers (no seed artifact is emitted)

Any of: census below the (non-lowerable) baseline floor; incomplete/changed
paging; duplicate or invalid row ids; row revision or historical snapshot
digest mismatch; unsupported/non-empty alternate media fields (including
`{}`); failed/oversized/redirected/non-allowlisted URL fetch; missing or
ambiguous tenant/target mapping; thumbnail not declared by the target's
audited `image_fields`; a row with no `image_url` but a non-empty `thumbnail_url`
(thumbnail-only rows can never satisfy migration completeness: no-media
requires BOTH urls null); no-media row without reviewed no-media evidence;
missing/stale/count-mismatched census review; invalid sibling proof
(logical_post_id mismatch, missing/partial/extra member row ids, singleton).
Blocked runs write only a manifest marked `INCOMPLETE_DO_NOT_APPLY`, with
null artifact digests.

## Verification status of this package

Unit/integration coverage is deterministic and network-free (see
`tests/test_exact_byte_history_seed_20261010.py`). Anything requiring a live
database — the real census, the migration's SQL authority functions, the
seed transaction guards and the privileged gate-OFF readbacks — is **unrun
here** and remains an operator step; nothing in this package should be read
as a passed live-DB verification.

## Remaining operator-supplied evidence and live cutover requirements

The seed is a prerequisite, not a cutover. Still required and out of scope
here: durable storage of all mapping/proof/no-media/census-review evidence
refs; URL immutability evidence; the complete lower-provider call boundary
inventory; runtime commit attestation; the two privileged gate-OFF readback
receipts; and the separate operator activation RPC with deployment/cutover
evidence. All gate flags remain OFF until that release gate.
