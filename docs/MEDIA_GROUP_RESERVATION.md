# Media Group Reservation Runbook

DRAFT runbook for the global no-repeat media repair. The migration
`migrations/DRAFT_media_group_usage_20261002.sql` is **not applied** to any
database. Nothing here runs until the `DRAFT_` prefix is removed and this
runbook is signed off.

## What this adds

- `public.media_group` — per-gym visual group (one row per pHash cluster):
  stable `group_key`, `phash_centroid` (64-bit dct pHash, 16 hex chars, from
  `agent/vision.py dct_phash`), `member_phashes`, `source_lineage_ids`
  (Drive file IDs / asset IDs rolled into the group), terminal-aware `status`
  (`active` / `exhausted` / `unknown` / `retired`).
- `public.media_group_usage` — reservation/usage ledger keyed
  `(gym_id, group_key, usage_date, sibling_id)` with `channel`
  (ig/fb/story/gbp) and `status` (reserved/published).
- RPCs (all `SECURITY DEFINER`, `search_path = pg_catalog, public`,
  service-role-only execute):
  - `claim_media_group(gym, key, date, sibling, channel)` — atomic claim;
    locks the group row `FOR UPDATE`. Outcomes: `claimed`, `shared`
    (same-date sibling), `held_conflict` (reserved on another date),
    `held_used` (published history), `held_terminal`, `missing_group`.
    Unknown/missing identity fails closed.
  - `publish_media_group_usage(gym, key, date[, sibling])` — flips reserved
    rows to published. Permanent: trigger blocks reopen/delete, and the claim
    RPC refuses the group on every other date forever.
  - `release_media_group_reservation(gym, key, date, sibling)` — deletes a
    reserved row only; returns remaining row count, so the group is reusable
    exactly when the last active sibling is removed.
  - `hold_media_group(gym, key, status)` — one-way move to a terminal status.

## Application order

1. Review window: keep the `DRAFT_` prefix; do not apply while the media
   reuse guard work in `agent/portal_calendar_store.py` (PR #230, separate
   hold) is in flight — confirm that PR has merged or its hold is lifted.
2. Apply `media_group_usage_20261002.sql` (renamed, prefix removed) via the
   normal Supabase migration path. It is idempotent (`if not exists`,
   `drop trigger if exists`, `create or replace function`).
3. Smoke check with the service role: insert one group, claim two same-date
   siblings, claim a different date (must return `held_conflict`), publish,
   claim a third date (must return `held_used`), release a published row
   (must raise).
4. Wire callers (owned by other workers/parent):
   - Selection path calls `claim_media_group` before scheduling a slot;
     any non-`claimed`/`shared` outcome holds the slot visibly and never
     falls back to recycling another group.
   - Publish path calls `publish_media_group_usage` only after the platform
     publish succeeds.
   - Slot removal/cancel path calls `release_media_group_reservation`.
   - Runway/identity exhaustion calls `hold_media_group(..., 'exhausted')`;
     unclusterable assets call it with `'unknown'`.

## Backfill for existing published rows

For each gym, cluster existing published media with
`agent/vision.py cluster_library` (dct pHash + Hamming threshold; treat
distinct Drive IDs/MD5s in one Hamming neighborhood as ONE group — the
Swift River Oct 7–10 incident is the canonical case). For every cluster:

1. Insert one `media_group` row (`status = 'active'`, centroid = cluster
   representative hash, `source_lineage_ids` = all Drive/asset IDs in the
   cluster).
2. For each historical publish date, insert one `media_group_usage` row per
   published sibling with `status = 'published'`, `published_at` =
   historical publish timestamp. Backfill writes go through a one-off
   service-role script, not the RPCs (the RPCs only flip reserved rows).
3. Gyms with cross-day repeats (the five exact-repeat gyms) will therefore
   have those groups permanently used; future dates must claim new groups.
4. Any asset that clusters to nothing or to a group whose identity is
   disputed gets its own group in `status = 'unknown'` — visible hold,
   never claimable.

## Conflict reconciliation

- `held_conflict`: the group is actively reserved for another date. Either
  release the other date's reservations (cancel those slots) or pick a
  different group. Never force the claim.
- `held_used`: permanent. Pick a different group; if the library is empty,
  hold the slot and alert (fail closed).
- `missing_group`: the asset was never clustered or the tenant key is wrong.
  Verify `gym_id` is the canonical Echo tenant key; otherwise route the asset
  through clustering before any claim.
- Split-group repairs (two groups later judged visually identical): pick the
  survivor, `hold_media_group` the loser as `retired`, re-point
  `source_lineage_ids`. Published history on the loser stays published.

## Rollback

The migration is additive (new tables, triggers, functions) and is not yet
referenced by application code at apply time, so rollback is:

```sql
drop function if exists public.hold_media_group(text, text, text);
drop function if exists public.release_media_group_reservation(text, text, date, text);
drop function if exists public.publish_media_group_usage(text, text, date, text);
drop function if exists public.claim_media_group(text, text, date, text, text);
drop table if exists public.media_group_usage;
drop table if exists public.media_group;
drop function if exists public.media_group_usage_guard();
drop function if exists public.media_group_status_guard();
```

Rollback destroys reservation/publish history, so once callers are wired and
live claims exist, prefer forward-fix over rollback.

## Integration status (2026-10-02, parent integration pass)

Wired behind `AGENT_MEDIA_GROUP_GUARD` (default OFF; flag OFF is byte-identical
to prior behavior):

- `agent/media_group_guard.py` — flag helper, in-process `GroupLedger`
  mirroring the RPC semantics (the DB RPCs are authoritative once applied),
  `check_candidate` decisions, the operator resolution paths
  (`register_manual_group` wrapping `assign_manual_group`,
  `register_scene_confirmation` wrapping `confirm_scene_match`,
  `register_scene_rejection` wrapping `reject_scene_match` — all with audit
  notes persisted in the registered known-groups list), and the seam filters
  below. Group keys are frozen at creation (`{gym_id}::{group_id}`); the
  ledger keys on the frozen key, so membership growth and merges never disturb
  a reservation. Persistence must store `group_id`/`group_key` explicitly —
  in-process ids are deterministic per construction sequence only.
- Swap lane: `agent/media_swap.py` `candidates_for` filters candidates through
  `media_group_guard.filter_swap_candidates`; the exhaustion fallback
  (least-recently-used lane) is filtered too, so a used/reserved-elsewhere
  group is never recycled across dates — an empty result takes the existing
  `no_fresh_photo` path.
- Selector: `agent/gym_media_selector.py` `pickable` and `cooldown_fallback`
  exclude permanently-published/terminal groups, unverifiable identity, and
  unconfirmed scene matches via `selector_candidate_allowed`.
- Outbound gates: `agent/calendar_autopublish.py` (right after the
  `publish_hold_reason` reuse hold) and `agent/gbp_worker.py`
  (`_media_group_hold` at both non-draft send sites) hold with a
  `media_group_*` reject_reason through the existing revert / held-result
  machinery.

### Corrected threshold story (supersedes "cluster at <=6" for uniqueness)

Measured evidence from the Swift River incident — exactly ONE real pair,
measured on original Drive thumbnails via the portal: **JCK_6328 vs JCK_6331**,
visibly the same rig/class scene, dct_phash `a1c7e3a2583fa81e` vs
`b8c1e09b0cfd9be0`, Hamming distance **28**. No other neighboring-frame
distances were measured on real media (earlier 18–30 figures came from
distinct generated graphics / synthetic bands). The scene band below is a
conservative policy choice that includes this one measured pair — it is NOT a
calibrated complete scene range. `agent/visual_identity.py` is tiered:

- hamming **<= 6**: near-frame (recompressed/resized copies) → `SAME_GROUP`.
- hamming **7–30**: scene band → `SCENE_MATCH_CANDIDATE` — a HOLD, never an
  approval and never proof of difference. Every wired seam holds these closed
  as `media_group_scene_unconfirmed` pending corroboration or an operator
  decision. `assign_group` / `group_key_for` never merge them.
- hamming **> 30**: `NEW_GROUP`.
- No bytes and no usable pHash: `UNKNOWN_IDENTITY`, fail closed as before.

Scene-band holds resolve through the guard's operator paths:
`register_scene_confirmation` (asset IS the same scene — moves it into the
suspect group with phash/hamming evidence audited; same-date share /
cross-date block / publish permanence then apply to the merged group) or
`register_scene_rejection` (asset is DIFFERENT — recorded in the group's
`rejections`, keyed by stable identity, so future classification returns
`NEW_GROUP` instead of re-holding; byte equality still overrides a
rejection).

**Follow-up for the migration owner:** the DRAFT migration has no persistence
for group `rejections` or the audit trail — a `rejections` column (and audit
notes) on `media_group` is likely required before operator decisions survive
restarts. Not edited here; the migration files are frozen in this worktree.

### Swift River remediation path (manual groups)

The required posture for that gym is a HOLD, not substitution — but note the
current live state accurately: the 16 Oct 7–10 rows are PENDING rows staged
with distinct generated graphics (not a visible media HOLD produced by this
system), with `source_media_asset_id` NULL, all four dates unpublished; no
never-used approved-clean asset is pickable, and cooldown assets must not be
recycled to fill the gap. Remediation sequence, in order:

1. Operator resolves each held suspect: `register_scene_confirmation` /
   `register_scene_rejection` per scene-band pair, or
   `register_manual_group(gym, assets, "swift-river-scene", note=...)` —
   one manual group (`{gym_id}::manual:swift-river-scene`) covering the four
   dates' media — with the assertion recorded in the group's audit trail.
2. Re-plan the four pending dates to DISTINCT visual groups (one group cannot
   be reserved on four different dates) or keep the rows held.
3. Apply the migration (rename off `DRAFT_`, plus the rejections/audit
   follow-up above).
4. Backfill: cluster historical media via `vision.cluster_library`, register
   groups/ledger into the guard, backfill published usage rows.
5. Only then consider arming `AGENT_MEDIA_GROUP_GUARD` — with an empty
   registry the guard fails closed (holds every unverifiable candidate) by
   design, and the outbound gate holds NULL-identity rows closed
   (`media_group_identity_unknown`) in the meantime.

**The global fix is NOT complete**: the migration is still DRAFT and not
applied anywhere, the in-process ledger is not authoritative across workers,
no backfill has run, the flag is OFF, and no live Swift River acceptance is
claimed from the synthetic tests in this worktree.

### Out of scope here

The PR #230 Story-hold recovery / clearing-reason work is owned by a separate
Terra agent; this integration touches nothing in that area beyond the seams
listed above.

NOT wired, and why:

- `insert_rows` stage-door gate in `agent/portal_calendar_store.py` — that
  file is held by draft PR #230; the claim-at-staging door stays unwired in
  this worktree until the hold lifts.
- Variant activation (`create_variant_candidate`) bypasses `insert_rows` and
  lives in the same held file — same blocker.
- Approved-row redates in `calendar_autopublish.py` plant repeats
  deliberately; whether redating may move a group across dates is a product
  decision.
- The GBP 14-day rotation window conflicts with cross-date uniqueness;
  reconciling the two policies is still open.
- The DRAFT migration is not applied; the in-process ledger is a stand-in and
  cannot serialize across workers.
- Backfill must cluster historical media via `vision.cluster_library` and
  register groups/ledger before the flag is armed — with an empty registry
  the guard fails closed (holds every unverifiable candidate) by design.

