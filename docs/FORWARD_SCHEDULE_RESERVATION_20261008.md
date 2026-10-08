# Forward Schedule Reservation — DRAFT / OFF (2026-10-08)

Unapplied draft. No production activation, provider calls, migrations applied,
gates enabled or credentials provisioned. Requires, and never modifies, the
base forward-media claim draft (`DRAFT_fixer_forward_media_claim_20261006.sql`)
and the visual index draft (`DRAFT_fixer_forward_visual_index_20261008.sql`),
plus the committed `logical_post_id_20261004.sql` column. The visual claim RPC
remains the sole byte-occupancy and publication authority; this draft adds a
FUTURE slot reservation layer in front of it. It does not re-implement,
replace or bypass any visual, claim, approval or history check.

This note is the RPC contract Child 2 (Python coordinator) codes against.

## Purpose

Committed visual claims protect publication but do not reserve FUTURE calendar
slots: two planner passes (or a planner and a publisher retry) can select the
same photo for different future days before either claims. The reservation
authority binds one attested source/rendition proof to exactly one
(tenant, post_date, logical_post_id) future slot, durably and atomically, so
cross-day and cross-gym reuse is denied at selection time instead of only at
send time. Unknown historical inventory always holds; it never counts as
clearance or exhaustion.

## Concepts

- Slot key: `(tenant_id, post_date, logical_post_id)`. `tenant_id` is the
  canonical alias-resolved tenant (same resolution as the claim stack).
  `logical_post_id` is the validated persisted `content_calendar`
  column, NOT `visual_group_key`; the two stay separate.
- A slot holds at most one ACTIVE reservation (partial unique index).
- Sibling rows (IG feed / FB mirror / Story) of one logical post share the
  slot: re-reserving the same slot with the identical source/proof is an
  idempotent retry returning the existing reservation id. This is the exact
  same-logical-post sibling retry path. Same tenant and same date alone are
  NOT sufficient: a matching source_sha256 with a DIFFERENT logical_post_id
  on that tenant/date is denied, against active reservations and committed
  visual claims alike.
- Reservations are durable. There is no delete. State transitions are
  one-way: `active` -> `released` | `superseded` | `revoked`. Terminal rows
  persist as occupancy evidence. Direct DML is revoked from every non-owner
  role and a guard trigger enforces the transition rules.

## Gate

`public.forward_schedule_reservation_gate_20261008` — protected singleton,
`enabled boolean not null default false`, installed `false`. Reserve refuses
(`55000`) unless enabled. Release, revoke and the read-only conflict check
remain callable while OFF (they only reduce occupancy or read). Gate writes
take the exclusive graph lock via trigger; reserve takes the shared graph
lock, so activation cannot race an in-flight reserve. No service, attester or
media-owner role can mutate the gate. The matching Python flag is a Child 2
handoff and must default OFF.

## Table DDL (summary; migration is authoritative)

```sql
public.forward_schedule_reservation (
  reservation_id uuid primary key default gen_random_uuid(),
  tenant_id text not null,
  calendar_row_id uuid not null,          -- first reserving row (audit)
  logical_post_id uuid not null,
  post_date date not null,
  source_asset_id text not null,
  source_url text not null,               -- https only
  source_sha256 text not null,            -- 64 lowercase hex, from the 'original' attestation
  phash_version integer not null default 1 check (phash_version = 1),
  phash_v1 bigint not null,               -- from the 'original' attestation
  row_revision text not null,             -- persisted outgoing revision at reserve time
  lineage_evidence_id uuid not null references fixer_forward_media_lineage_20261006,
  attestation_ids uuid[] not null check (cardinality(attestation_ids) = 3),
  state text not null default 'active' check (state in ('active','released','superseded','revoked')),
  state_reason text,
  created_at timestamptz not null default now(),
  state_changed_at timestamptz
);
unique index (tenant_id, post_date, logical_post_id) where state = 'active';
```

RLS enabled; all table privileges revoked from public/anon/authenticated/
service_role/attester/owner. `service_role` receives SELECT only (planner
readback). All writes go through the SECURITY DEFINER RPCs below; a
SECURITY INVOKER guard trigger rejects any insert/update/delete/truncate
whose `current_user` is not the RPC owner, and enforces transition rules.

## RPC contract

All functions are `security definer`, `set search_path = pg_catalog, public`,
require READ COMMITTED (`25000` otherwise), and take locks in the existing
stack order: graph (shared) -> census (exclusive) -> row (`for update`) ->
slot advisory -> byte/token. None performs network or object I/O. Raises use
`23514` for validation/conflict/hold, `22023` for malformed arguments,
`55000` for gate OFF. Callers must treat any exception as HOLD (fail closed).

### `public.reserve_forward_slot_20261008(p_calendar_row_id uuid, p_logical_post_id uuid, p_expected_revision text, p_attestation_ids uuid[], p_expected_reservation_id uuid default null) returns uuid`

Executable by `service_role` only. Creates (or idempotently returns) the
active reservation for the slot of the given calendar row.

Validation, in order; any failure raises and nothing persists:

1. Gate ON (`55000 'schedule reservation authority is OFF pending review'`).
2. Row exists, `for update`; unsent (`status in
   ('draft','pending','queued','approved')`, `publish_claim_token is null`,
   `published_at is null`, `late_post_id is null`,
   `variant_status = 'active'`, `media_not_ready_reason is null`,
   `post_date is not null`). A publishing/published/ready row is refused —
   reservations are for future planning only and are never a ready-row or
   claim bypass.
3. `row.logical_post_id = p_logical_post_id` (both non-null). The persisted
   validated value is authoritative; the caller cannot invent one.
4. Full persisted media identity present (gym, source asset/url, image url,
   thumbnail url shape, render manifest digest) and
   `fixer_forward_media_attestation_request_20261006(id)->>'revision'`
   equals `p_expected_revision`. Changed media invalidates.
5. Provenance + CURRENT clearance: the owner registry tuple must match and
   `fixer_forward_media_history_clearance_20261006` must show
   `cleared_unused` for the exact tuple with no fleet hold for the source
   fingerprint. Missing or unknown history raises (fail closed).
6. Revocation fence: any `revoked` reservation for the same
   (tenant, source_asset_id) is terminal for that source under this draft.
   Re-reservation raises `'reservation source revoked; fresh source identity
   and clearance required'`. (Owner reinstatement, if ever desired, is a new
   source identity with fresh clearance — see limits.)
7. Attestation proof: exactly three distinct roles
   (original/delivered/thumbnail), each bound to this tenant, the bigint
   projection of the CURRENT revision, one shared lineage evidence id whose
   lineage row matches row/revision/tenant/group/asset/manifest, exact
   persisted URLs per role, and trusted object-read receipts whose MD5 and
   length equal the attestation. Producer-asserted hashes are never
   accepted; unknown attestation ids hold.
8. Negatives: any `forward_media_visual_negative` matching the original
   SHA256 exactly, or pHash v1 Hamming distance <= 30, raises
   `'visual negative evidence blocks reservation'`.
9. Committed visual occupancy: join visual attestations ->
   `forward_media_visual_claim_proof_20261008` ->
   `fixer_forward_media_claim_receipt_20261006`. Equal SHA256 raises
   `'visual byte ancestry already consumed by another tenant/date/logical
   post'` unless the claim is the exact same tenant AND post_date AND its
   receipt's calendar row still carries the same persisted
   `logical_post_id` (a deleted or unidentifiable claim row fails closed).
   For different bytes, the pHash policy is unchanged: distance <= 6 outside
   that exact logical-post sibling scope blocks; 7-30 raises `'visual
   similarity held for review'`; > 30 no match.
10. Active-reservation occupancy: equal SHA256 on any slot other than the
    exact own (tenant, post_date, logical_post_id) slot raises `'source
    already reserved for another tenant/date/logical post'` — including a
    different logical_post_id on the same tenant/date. pHash policy
    identical to step 9 against other active slots.
11. Slot resolution under the slot advisory lock + partial unique index:
    - No active reservation: insert `active` row, return new id.
    - Active reservation with identical source_sha256: return the existing
      id (idempotent same-slot retry / exact same-logical-post sibling
      reuse). The caller's own per-row proof was fully validated in steps
      5-10 above; sibling rows legitimately carry different revisions and
      attestation ids.
    - Active reservation with different source and
      `p_expected_reservation_id` equal to its id: CAS replace — the old row
      transitions `active -> superseded` and a new active row is inserted
      atomically in the same transaction. Returns the new id.
    - Otherwise raise `'schedule reservation slot conflict'` (wrong or
      missing CAS token).

### `public.release_forward_slot_20261008(p_reservation_id uuid, p_reason text) returns boolean`

Executable by `service_role` only. Transitions the reservation
`active -> released` under its row lock, freeing the slot for another
source. Idempotent: an already-terminal reservation returns `true`. Unknown
id raises `23514 'schedule reservation unavailable'`. Never deletes.

### `public.revoke_source_reservations_20261008(p_tenant_id text, p_source_sha256 text, p_reason text) returns integer`

Executable by `service_role` only. Transitions every active reservation for
that tenant + source SHA256 to `revoked`, returns the count (0 is allowed).
Fleet-wide byte eviction remains the negative-evidence authority of the
visual stack; this RPC is the tenant-scoped reservation complement. Revoked
sources fail step 6 of reserve until a fresh source identity + clearance
exists.

### `public.check_reservation_conflicts_20261008(p_tenant_id text, p_post_date date, p_logical_post_id uuid, p_source_sha256 text, p_phash_v1 bigint) returns jsonb`

Executable by `service_role` only. Read-only ADVISORY candidate-screening
helper for the planner; it takes no locks and grants no authority — only
`reserve_forward_slot_20261008` admits occupancy. Returns:

```json
{"allowed": bool,
 "conflicts": [{"kind": "negative" | "committed_claim" | "active_reservation" |
                "phash_block" | "phash_review",
                "reservation_id": ..., "claim_token": ...,
                "tenant_id": ..., "post_date": ..., "distance": ...}]}
```

`allowed` is true only with zero conflicts. A bounded search that stops here
without reserving proves nothing about depletion.

### `public.forward_reservation_proof_20261008(p_calendar_row_id uuid, p_source_sha256 text) returns jsonb`

Executable by `service_role` only. The consult-the-exact-reservation helper
for the final publication path. Recomputes tenant/revision from the
persisted row and returns
`{reservation_id, tenant_id, post_date, logical_post_id, source_sha256, row_revision, attestation_ids}`
for the ACTIVE reservation matching (tenant, row.post_date,
row.logical_post_id, p_source_sha256). Byte identity (SHA256) is the binding;
per-row revision freshness remains with the claim stack, which revalidates
revision, evidence and receipts at claim time. Raises `23514 'schedule
reservation proof unavailable'` when none matches (wrong date/gym/logical
post/source or released/superseded/revoked state). No other slot can borrow
the reservation.

## Integration handoffs (NOT in this child scope)

- Final-claim enforcement: `fixer_forward_visual_index_claim_20261008` is
  unchanged. Wiring publication to REQUIRE a matching
  `forward_reservation_proof_20261008` result (publisher-side check plus a
  future DB-side consult) belongs to the integrator (Child 2 publisher path
  + Sol review). Until wired, both DB gates stay OFF.
- Python coordinator (`gym_media_selector`, `client_month_run`,
  `portal_calendar_store.insert_rows`, `forward_media_visual_index.attest`
  pipeline) calls and the env flag are Child 2 scope.
- Candidate exhaustion -> explicit held slot is a Child 2 behavior; the SQL
  layer stores reservations only, never "exhausted" markers.

## Concurrency and lock order

Reserve serializes with attestation/negative/history appends (exclusive
graph lock on their inserts vs shared here), with claims (shared graph +
exclusive census), and with other reserves (exclusive census, then slot
advisory, then the partial unique index as backstop). Two concurrent
reserves for one slot with different sources: exactly one commits; the loser
raises after the census/slot wait under a fresh READ COMMITTED snapshot and
leaves no partial reservation. Reserve never takes the graph lock
exclusively, so it cannot block attester evidence appends beyond its
transaction.

## Rollback limits

Before any use: drop the new objects in dependency order (functions, gate,
table) in a privileged session; nothing else references them. After use:
preserve all reservation rows including terminal states — they are occupancy
evidence. Do not apply over an armed visual/claim stack without the normal
release gate. No production rollback or activation is authorized by this
draft.

## Acceptance

```sh
python3 -m pytest -q tests/test_forward_schedule_reservation_pg.py
```

Real disposable PostgreSQL 17 (initdb/pg_ctl/psql on PATH; psycopg not
required by this suite). Covers: OFF gate hold; happy path + idempotent
retry; exact-logical-post sibling reuse; cross-logical-post (same date),
cross-day, cross-gym denial for active reservations AND committed claims;
CAS replace + wrong-token conflict; release/re-reserve; revocation terminal
fence; committed-claim conflict (planner vs publisher occupancy); concurrent
one-winner slot race with no loser occupancy; revision drift; logical-post
forgery; ready-row bypass denial; reservation proof consult and borrow
denial; advisory conflict check; ACL denials; isolation guard;
immutable/transition guard; rollback-only installation.
