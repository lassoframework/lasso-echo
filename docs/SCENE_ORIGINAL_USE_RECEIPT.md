# Scene original-use receipt (DRAFT / UNAPPLIED / OFF)

Date: 2026-10-04 (round-4 repairs). Branch:
`codex/echo-scene-original-use-receipt-20261004` (PR268-based, isolated).
Status: **draft proof package only — production held, NOT integrated, NOT
wired, no flags enabled, nothing committed for apply. No GoHighLevel content
anywhere in this package.**

## Review lineage

**Round 1** — independent Sol safety review
(`/tmp/fixer_scene_receipt_sol_safety_20261004.log`) killed the trigger-minted
design with three P0s: (1) never mint via a raising terminal AFTER trigger
after the provider send (the send may already have succeeded; the app can
neither roll it back nor safely retry — duplicate-send); (2) same-day
siblings share ONE ledger event/occupancy, so one receipt per sibling is
false; (3) `status='published'` is a local claim, never provider-use proof.

**Round 2** — independent rejection of the two-phase draft with one P0 and
five P1s, all repaired in this revision:

1. **P0 — the migration could not install.**
   `BEFORE UPDATE OR DELETE OR TRUNCATE ... FOR EACH ROW` is invalid
   PostgreSQL: TRUNCATE triggers are statement-level only. Every append-only
   guard is now a `FOR EACH ROW` UPDATE/DELETE trigger **plus** a separate
   `FOR EACH STATEMENT` TRUNCATE trigger (attempt, receipt, attestation).
   A static source contract asserts no row-level trigger mentions TRUNCATE,
   and the rollback-only install probe proves the file applies.
2. **P1 — service_role could forge a delivery.** The old terminal RPC
   accepted `delivered` + any `provider_post_id` + optional `{}` evidence
   from service_role, minting a false receipt. Terminal outcomes now require
   a matching row in **`visual_scene_original_use_attestation`**: an
   owner-only, append-only, RLS-protected authoritative provider outcome
   attestation bound to the exact claim token (FK to the attempt), tenant,
   calendar row, provider/account/channel/post id and the exact delivered
   byte lineage, with **non-empty** `readback_evidence`. service_role has
   SELECT only (catalog-verified in tests: no INSERT/UPDATE/DELETE), so the
   public RPC cannot be used to forge a delivery. **No attester role/runtime
   exists yet** — see "Open dependencies": until that separately reviewed
   integration writes attestations, terminal delivery is IMPOSSIBLE
   (fail-closed by construction). This package records attestation; it does
   not invent provider proof.
3. **P1 — raw gym alias vs canonical ledger keys.** prepare used the row's
   raw `gym_id` for ledger lookup/store while the exact claim flow
   canonicalizes (the exact-byte guard maps `p_new.gym_id :=
   visual_group_tenant_id(p_new.gym_id)` at the trigger boundary; the scene
   scan resolves `visual_group_tenant_strict`) and keys
   visual_group/ledger/sibling/occupancy by the canonical tenant. prepare
   now resolves `visual_group_tenant_strict(v_row.gym_id)` and uses the
   canonical key for the ledger lookup, sibling membership, occupancy
   lookup and the stored `ledger_gym_id` (FK-bound). A static test checks
   this against the claim stack's own source, not this file's claims; PG
   adversarial tests cover both the alias-row positive path and the
   inverse (a ledger keyed by the raw alias must NOT be found).
4. **P1 — caller URLs could diverge from row/occupancy while md5 matched.**
   prepare now requires `delivered_url` to be the row's ACTUAL current
   delivered object via the claim stack's own
   `visual_scene_row_delivered_object` (poster = thumbnail_url for video
   rows, display = image_url otherwise) AND the current occupancy row's
   claim-recorded `evidence->>'exact_url'`; and binds `source_url` to the
   row's `source_media_url` — or, when the row records no distinct source
   media, requires source == delivered (a caller-invented rendered source
   with no row field is unverifiable and refused).
5. **P1 — same-date same-image siblings share ONE occupancy PK.**
   Occupancy's PK is `(phash, tenant_id, group_key, used_date)`; the second
   sibling cannot write its own occupancy row and prepare's old
   `calendar_row_id` binding made it unable to prepare at all. prepare now
   accepts the SHARED occupancy row for the ledger reservation holder OR
   any ACTIVE same-day sibling of the same ledger event. The adversarial
   sibling test uses the IDENTICAL object/pHash and exactly one occupancy
   row — not two separate objects.
6. **P1 — provider-failure test re-scoped.** The old
   `test_pg_provider_success_terminal_db_failure_no_double_send` proved
   only RPC rollback/retry. It is renamed
   `test_pg_terminal_rpc_rollback_retry_converges_one_receipt` and its
   docstring states exactly what SQL proves. Provider send behavior,
   authoritative readback and application-level no-resend remain UNPROVEN
   integration properties.

**Round 3** — independent P1 integration review found two defects that
static 11/11 and scratch-PG 30/30 had missed because **every prior PG
scenario ran with enforcement UNARMED**:

1. **P1 — terminate could never publish on an armed tenant.** The armed
   claim trigger (`DRAFT_visual_group_claim_trigger_20261002.sql`,
   `visual_group_finalization_requires_evidence` /
   `visual_group_finalization_evidenced`) refuses any ambiguous row's
   finalization — and a token-holding row is ambiguous by definition —
   without a `visual_group_reconciliation` receipt written in the **same
   transaction** (matched on `txid_current()`, the exact original claim
   snapshot, every ambiguous sibling attempt, all review holds, and the
   exact date / delivered media / provider post / published-at).
   terminate's `update content_calendar set status='published' ...`
   carried no such proof, so the first terminal publication raised
   `ambiguous publication requires terminal provider reconciliation`.
   terminate now writes that receipt in-band **after** the owner-only
   attestation gate and **before** the calendar transition, from
   validated attempt/attestation/row data only, for BOTH terminal
   outcomes (`confirmed_published` / `confirmed_not_sent`). The existing
   trigger is NOT weakened; service_role has no INSERT on
   `visual_group_reconciliation` (catalog-enforced), so the receipt
   cannot be forged through the public RPC: a forged or missing
   attestation means no reconciliation row and the armed trigger keeps
   refusing. Armed scenarios prove the positive path (receipt contents,
   sibling coverage, trigger-owned ledger move to `published`), the
   missing-proof refusals, a direct marker update on a token-holding row
   still raising, and service_role reconciliation-forgery denial.
2. **P1 — a lawful later same-day sibling could never prepare.** prepare
   required `ledger.state='reserved'`, but the FIRST sibling's terminal
   publication moves the SHARED ledger row to `published` (the armed
   claim trigger does this on finalization, preserving `reserved_date`).
   prepare now accepts a `published` ledger on THIS exact
   `reserved_date`; released, other-date and missing ledgers still
   refuse, and tenant/date/bytes/occupancy binding is unchanged. The
   armed sibling scenario is first-sibling terminal completion THEN
   second-sibling prepare + superseded terminate, plus wrong-date and
   wrong-tenant refusals at the ledger gate.
3. **P2 — expected provider/channel binding.** prepare now immutably
   binds `expected_provider` (the caller-declared send provider) and
   `expected_channel` (the row's account — a row with no account
   refuses), frozen by the attempt guard; the terminal attestation must
   match BOTH exactly, replacing the old soft "row.account if present"
   check, and channel drift between prepare and terminate refuses.
   `provider_account_id` remains attester-asserted: the calendar row
   carries no account-id evidence to bind it against (documented, not
   silently watered down).


**Round 4** — independent rejection of the round-3 draft with one P1 and
one P2, both repaired in this revision:

1. **P1 — historical sibling/hold context was cleared by inclusion.**
   terminate copied ALL `visual_group_usage_sibling` attempts and ALL
   `review_hold` event ids for the calendar row into
   `visual_group_reconciliation`, so unrelated HISTORICAL ambiguity
   appeared resolved merely by being listed. A new shared gate,
   `visual_scene_original_use_check_ambiguity`, now refuses (a) any
   UNRECONCILED ambiguous sibling of the row whose original claim token is
   NULL/older/different than the exact live token, whose original image is
   NULL or differs from the current delivered object, or which carries an
   original provider post id other than the current attested post (ANY set
   post refuses pre-send, where no current post exists); and (b) any
   UNCOVERED `review_hold` for the row without per-claim evidence proving
   it current (non-JSON reason, missing/mismatched `publish_claim_token`
   or `image_url`, mismatched `late_post_id`/`post_date`). prepare runs
   the gate BEFORE the attempt row exists — no attempt, no provider send.
   terminate re-runs it AFTER the attempt and row locks, so ambiguity
   inserted or changed between prepare and terminate cannot be laundered:
   the terminal transaction raises and commits NO receipt, NO
   reconciliation, NO publication and NO historical clearance; the row
   stays held for manual evidence recovery. Reconciliation
   attempts/reserved groups/hold ids are built ONLY from validated
   current ambiguous siblings and holds. A current ambiguous sibling with
   the exact current token and exact current image — and a hold with
   matching per-claim evidence — may still be covered, so the armed valid
   path (first sibling terminal, then sequential second sibling) still
   passes. The armed claim trigger is NOT weakened and no coverage is
   manufactured.
2. **P2 — `provider_account_id` was attester-asserted but unbound.**
   prepare now REQUIRES a non-empty `expected_provider_account_id` in the
   payload: the caller-declared destination provider account, which the
   caller MUST take from a verified provider connection. It is stored
   immutably on the attempt (frozen by the attempt guard; a drifted
   same-token replay conflicts) and the terminal attestation's
   `provider_account_id` must match it EXACTLY. A caller-declared value is
   NOT inherently trusted — it is a binding the owner-only attestation
   must independently reproduce; the runtime integration that would
   perform this verification remains absent (see Open dependencies).

## What was built (exactly three files, nothing else touched)

1. `migrations/DRAFT_visual_scene_original_use_receipt_20261004.sql` —
   additive draft applied strictly after
   `DRAFT_visual_scene_ledger_coverage_gate_20261004.sql`:
   - `visual_scene_original_use_attempt`: immutable pre-send PREPARED
     snapshot per exact claim token, binding canonical tenant, canonical
     ledger PK, row id, exact source/delivered URLs + `md5:` fingerprints +
     delivered pHash, and owner read/render receipt lineage.
   - `visual_scene_original_use_attestation`: owner-only append-only
     authoritative provider outcome attestation (round-2 P1 gate), RLS +
     SELECT-only for service_role.
   - `visual_scene_original_use_receipt`: immutable append-only receipt,
     `unique (ledger_gym_id, group_key, used_date)` — one per ledger event;
     FK-references the attestation it was minted from.
   - `visual_scene_original_use_prepare(jsonb)` (phase 1, pre-send): every
     evidence class above is required and exactly bound; any miss RAISES
     before any provider call could be authorized. Same-token retry with
     identical evidence replays; drifted evidence conflicts.
   - `visual_scene_original_use_terminate(jsonb)` (phase 2, post-send,
     idempotent): requires the matching attestation (fail-closed without
     it); accepts only `delivered` / `confirmed_no_send`; unknown stays
     held forever. Replay of the recorded outcome returns it; conflicting
     outcome raises. `delivered` mints the receipt iff the ledger event has
     none (first confirmed delivery), else records the attempt superseded;
     then marks the row published and clears the spent token.
     `confirmed_no_send` releases the token so a NEW token may prepare.
   - Fail-closed late-install refusal if any tenant is already armed.
   - No trigger on `content_calendar`, no flag flips, no app wiring.
2. `tests/test_scene_original_use_receipt_pg.py` — 15 static source
   contracts (always run; including claim-stack cross-checks, the
   trigger-shape contract and the round-4 fail-closed gate shape) + 28
   disposable-PostgreSQL adversarial scenarios
   behind the guarded scratch DSN: install probes, missing evidence blocks
   pre-send, exact-token binding, published-status-is-not-proof, happy
   path, token retry/release, siblings with shared occupancy → one
   receipt, swaps, terminal replay/conflict, RPC rollback/retry
   convergence (re-scoped), unknown outcome held, historical unknown
   blocked, deletion retention, immutability incl. TRUNCATE, service_role
   forgery refusal (RPC + direct writes + catalog privilege contract),
   attestation match/mismatch matrix, raw-alias canonical keys (positive +
   inverse), caller-URL binding, positive rendered chain — plus the
   round-3 ARMED scenarios (current-tx reconciliation proof, armed
   missing-proof fail-closed, armed first-sibling-terminal then
   second-sibling prepare with wrong-date/wrong-tenant refusals) and the
   expected provider/channel binding matrix — plus the round-4
   scenarios: PRE-SEND refusal of unresolved historical siblings (older
   token U, NULL token, old image B, missing image, old provider post
   P-old) and of review_holds without per-claim evidence (no attempt row
   created); TERMINAL recheck refusal of ambiguity inserted or changed
   after prepare (no receipt/reconciliation/publication/historical
   clearance commits; the attempt stays prepared); the positive
   current-sibling + current-hold coverage path; and the
   expected_provider_account_id required/non-empty/immutable/exact-match
   matrix.
3. This document.

## Integration contract (NOT implemented here — exact handoff)

0. **Attester (HARD DEPENDENCY, separate reviewed integration):** an
   owner-side attester runtime must perform authoritative provider
   readback / authoritative absence checks and write
   `visual_scene_original_use_attestation` rows. **Until it exists, no
   terminal outcome can be recorded and no receipt can ever be minted —
   this is deliberate fail-closed behavior, not a gap to route around.**
   service_role must never gain write access to the attestation table.
1. **Claim/prepare**: the owned claim RPC transaction claims the row, mints
   the token, derives delivered bytes, writes ledger/occupancy, then calls
   `visual_scene_original_use_prepare` in the SAME transaction — including
   `provider_account_id` taken from a VERIFIED provider connection (the
   value is a binding the attestation must independently reproduce, not
   proof in itself). If prepare raises — including on unresolved
   historical sibling/hold ambiguity — the whole claim rolls back and NO
   provider call is made.
2. **Provider send**: send only against a `prepared` attempt, passing the
   claim token as the Zernio idempotency key (the main IG/FB lane currently
   omits it — `agent/zernio_publisher.py:329`, `agent/zernio.py:637`; GBP
   already sends one).
3. **Terminal**: after the attester writes its row, call
   `visual_scene_original_use_terminate`. A transient DB failure retries
   THIS RPC only — never the provider send. Idempotent replay makes that
   safe. terminate itself writes the current-transaction
   `visual_group_reconciliation` receipt the armed claim trigger requires
   for the calendar transition (round-3 P1); the caller must NOT write
   that table (service_role has no INSERT) and must not publish the row
   by any other path.
4. **Unknown outcome**: leave the attempt `prepared`. Reconcile only via
   authoritative provider readback (attested `delivered`) or authoritative
   absence (attested `confirmed_no_send`). Anything else stays held; no
   replay, no new token.
5. **Deletion**: attempts/attestations/receipts carry no FK to
   `content_calendar` and survive row deletion by design. A reviewed
   archive/delete policy for calendar rows with non-terminal `prepared`
   attempts is still an open decision.

## Open dependencies and honest gaps

- **The attester role/runtime does not exist.** Terminal delivery is
  impossible until that separate, independently reviewed integration lands.
  Provider send behavior, authoritative readback and application-level
  no-resend are UNPROVEN by this package — SQL cannot observe them. The
  re-scoped rollback/retry test proves only RPC convergence.
- **No claim-path wiring exists** (asserted by
  `test_static_no_live_wiring`). Claim-wave/claim-RPC changes, the Zernio
  idempotency key on the IG/FB lane, and the reconciler for held attempts
  are separate, unstarted integration work.
- **Direct Meta lanes** lack idempotency keys and authoritative readback;
  the round-1 review requires readback before enabling this gate there.
- **Ledger-gate evaluator unchanged.** Whether/how receipts resolve
  fingerprint-free obligations is an open, separately reviewable question.
  Historical fingerprint-free obligations remain
  `historical_fingerprint_unproven`; nothing here clears them.
- **Local database verification (2026-10-04, round 4).** Static
  contracts pass **15/15** after the round-4 edits. The 28 PostgreSQL
  scenarios (23 prior + 5 new round-4 adversarial/positive cases) were
  SKIPPED in the worker sandbox (no scratch cluster; SysV shared-memory
  calls fail) and **must be re-run by the integration owner on the
  disposable host PostgreSQL 17 cluster after these edits** — the earlier
  37/37 host pass covered the round-3 code, not this revision. This is
  local database evidence, not a production migration or provider
  delivery proof.
- `calendar_row_id` on attempts/attestations/receipts is an evidence
  pointer without FK (deletion retention); referential cleanup policy is
  an open decision.

Not self-graded. Independent re-audit required before any activation talk.
