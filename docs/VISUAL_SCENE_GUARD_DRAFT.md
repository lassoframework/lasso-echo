# Global pHash scene-similarity guard — DRAFT / UNAPPLIED / INCOMPLETE

## STATUS: INCOMPLETE — DO NOT APPLY, DO NOT ACTIVATE (Astra review 2026-10-03)

The prep-time writer architecture this package was drafted for was **rejected
by Astra's review**. There is NO runtime scene writer and NO operational gate.
Nothing here claims global near-duplicate enforcement: the exact-byte PR235
behavior (`visual_global_usage`, md5-keyed, frozen contract) is unchanged and
remains the only enforcement layer. This document is the honest record of
what exists, why the writer was rejected, and what a redesign requires.

### Why the prep-time writer was rejected

Recording a scene at preparation time:

1. marks UNUSED candidates as used — preparation is not a usage decision;
2. is not atomic with the real `visual_global_usage` claim, which happens
   later in the calendar-trigger `visual_global_claim_scene` transaction; and
3. still races across gyms (two tenants preparing concurrently each see a
   table without the other's uncommitted record).

Durable review holds were also missing: a hold existed only as a log line.

### What exists (all OFF, none operational)

- `agent/visual_scene.py` — pure module: `scene_fingerprint(bytes)` →
  `scene:phash64:<16 hex>` or None (never raises, never invents an id);
  `normalize_scene` (namespaced-only); `hamming_distance` (999 = no evidence);
  `classify_scene` (nearest-only) and `classify_scene_all` (EVERY ≤6 and
  7..30 match, (distance, phash)-sorted, worst-case band as kind). Bands:
  near_frame ≤ 6 (§3 burst cluster radius), scene_candidate 7..30 — calibrated
  on exactly ONE measured incident pair (Swift River JCK_6328/JCK_6331,
  hamming 28), hold-only — distinct > 30, unknown = fail closed, never
  distinct.
- `agent/config.py` — `AGENT_VISUAL_SCENE_GUARD`, OFF by default, tri-state
  read (ambiguous value counts as armed-fail-closed).
- `agent/visual_writer_prepare.py` — advisory `scene_fingerprint` evidence
  field alongside the md5 identity. It NEVER gates and NEVER raises:
  undecodable bytes record null evidence; no p_scene_* payload, no scene RPC,
  no post_date requirement in any flag state.
- `agent/gym_media_selector.py` — read machinery intact but UNREACHABLE:
  `SCENE_GUARD_OPERATIONAL = False`, so an armed flag raises
  `SceneLedgerUnavailable("...not operational...")` before any scan
  (strict_claims propagates; non-strict returns []). The machinery kept for
  the redesign: unfiltered full-table paginated scan (a one-bit-away phash
  must be caught — no exact-match filter is possible), pinned
  full-primary-key ordering with row-over-row verification, Content-Range
  completeness proofs, mid-scan change detection, bare-phash / tenant-UUID /
  group_key / used_date row validation, and the all-matches policy inputs
  (cross-tenant near ⇒ exclude; same-tenant cross-date near ⇒ hold; any 7..30
  candidate ⇒ hold).
- `migrations/DRAFT_visual_scene_phash_20261003.sql` — design sketch only.
  Its header carries the same STATUS; the two record functions
  (`visual_scene_record_use`, `visual_scene_record_use_tx`) exist but their
  bodies RAISE 0A000 — no working writer even if applied. The table sketch
  (`phash char(16)`, `tenant_id`, `group_key`, `used_date date NOT NULL`,
  PK on phash+tenant+group, `(tenant_id, used_date)` index, immutability
  trigger, SELECT-only service-role grant) is the shape a redesign starts
  from. `migrations/DRAFT_visual_global_history_20261002.sql` is reverted to
  base: prepare RPCs have their original 6-arg signatures and no scene
  surface.

### Required redesign (mirrors the migration STATUS section)

(a) A candidate/staging table written at prep that does NOT count as used —
    preparation evidence must never consume a scene.
(b) Scene recording and the occupied-scene/dates comparison moved INTO the
    claim transaction (the `visual_global_claim_scene` path) under its
    existing locks, owner-attested with the exact object bytes in the same
    call that establishes byte authority.
(c) A durable review-hold table written atomically in that same claim
    transaction whenever a conflict is found.
(d) Hamming comparison of ALL occupied scenes/dates enforced transactionally,
    server-side, never by the client (today's client-side full-table scan is
    O(table) per pick and has no server-side hamming index).
(e) Full backfill before any activation.

Until (a)–(e) land, `SCENE_GUARD_OPERATIONAL` stays False and
`AGENT_VISUAL_SCENE_GUARD` armed means fail-closed, not enforcement. pHash
remains similarity evidence only: it never auto-approves and never writes
`visual_group_scene_link`, which stays human-confirmed via
`visual_group_link_scene`.
