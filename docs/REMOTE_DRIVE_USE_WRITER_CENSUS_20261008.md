# Remote DriveUseAuthority — writer census (2026-10-08)

Scope: every `stamp_use` / `rollback_use` / `restore_unstaged` / direct
`used_count`/`last_used_at` write under `agent/` and `scripts/`, plus the
calendar/card builder paths that call them. Target seam: the staged, default-OFF
`agent/remote_drive_use.py` `DriveUseAuthority` (flag
`AGENT_REMOTE_DRIVE_USE_CAS_ENABLED`, currently OFF). The selector has a staged
remote branch, but no production stage caller yet supplies its stable use ID
and exact source/asset snapshot; activation remains held.

Key facts about the seam that drive the propagation changes below:

- `remote_drive_use.validate` requires: `gym_id` base key (no `_ig/_fb/_gbp`
  suffix), `post_date` strict ISO date, full `asset_before` row with
  `used_count == 0`, `last_used_at is None`, `eligible is True`,
  `excluded_by_coach is False`, `content_hash`, and a full `source_before` row
  with `kind == 'gym_drive'` and `active is True` (remote_drive_use.py:34-70).
- `drive_use_version` on `media_asset`/`media_source` exists only in
  `migrations/DRAFT_fixer_remote_drive_use_cas_20261008.sql` — not yet applied
  (UNVERIFIED: no applied migration carries the column).
- Callers today hold only the asset row (from `store.get_asset` /
  `list_assets`); none hold the media_source row. The source row is resolvable
  via `store.list_sources()` + `asset["source_id"]` (pattern at
  `agent/gym_media_index.py:685-691`).
- Stage-use is PERMANENT since 2026-10-02 (`gym_media_selector.py:1134-1138`):
  `rollback_use` only marks kv records `rolled_back`; counters restore ONLY on
  the `restore_unstaged=True` never-landed path. The remote seam's
  `release_never_landed` currently raises
  (`remote_drive_use.py:197-199`), so the restore paths below have no remote
  equivalent yet.

## 1. Write sites (the ledger itself)

| file:line | what | notes for routing |
|---|---|---|
| agent/gym_media_selector.py:1134 (`stamp_use`) | `update_asset` used_count+1/last_used_at + kv append `gym_media_use:{base}:{date}` (list, 2x-day siblings) | Core write. Replace body with staged remote apply when flag on; kv record must persist `use_id` so rollback/readback can target the exact attempt. Not fully idempotent today (docstring :1145-1147) — remote CAS fixes this. |
| agent/gym_media_selector.py:1172 (`rollback_use`) | kv settle; counter restore only when `restore_unstaged` (:1205-1219) | Remote route needs per-attempt settle + a never-landed release protocol (currently unavailable remotely). |
| agent/gym_media_selector.py:1227 (`rollback_asset`) | cross-date kv settle by asset (coach hide) | Dateless — remote settle must scan attempts by asset_id, not use_id. |
| agent/podcast_selector.py:186 (`stamp_use`), :210 (`rollback_use`) | Same pattern for podcast clips (`podcast_use` kv) | **BYPASS**: podcast assets fail remote validation (source `kind != 'gym_drive'`); extending the seam to podcast is UNVERIFIED. |
| agent/gym_media_index.py:290 | index-time insert `used_count: 0` | Initialization, not a use-write; out of scope but must keep default 0 for remote `asset_before` validation. |

## 2. stamp_use call sites (stage paths) — propagation census

| file:line | lane | stage ID / source row at hand | stage order | required change |
|---|---|---|---|---|
| agent/gym_media_builder.py:599 | feed card builder (all client gyms, `_GYM_DRIVE_PILLARS`) | claim_id (`_drive_claim_id`); FRESH asset row via `store.get_asset` (:589); source NOT held — re-fetched via `asset_source_ok` (:594) | stamp AFTER Drive claim (:578) and fresh-row/source guards, BEFORE draft return; card persisted by caller | Mint `use_id` from claim/draft identity; fetch full `media_source` row; call `remote_drive_use.request_for` + `apply` instead of `_sel.stamp_use` when flag on. Unknown apply → hold draft (same fail-closed shape as :600-607). |
| agent/podcast_library_builder.py:328 | podcast clip card builder | draft_id `podlib_{episode}_{clip}_{day}` (:296); asset row held | stamp AFTER draft assembled, BEFORE return; skipped when `defer_use` (:323) | Podcast pool — see §1 bypass. UNVERIFIED whether podcast lanes are in remote scope. |
| agent/real_month_planner.py:1785 | podcast month planner | draft.day_key; asset on draft | stamp AFTER `insert_rows` succeeds and asset id is in `saved_assets` (:1778-1787) | Same podcast caveat; stage order is already post-persist (good model for remote). |
| agent/story_studio.py:639 (`_stamp_segments`) | story render lane | request_id; asset rows per segment | stamp AFTER `_stage_calendar_row` succeeds (:294-320) | **post_date is `story:{request_id}` — fails remote ISO-date validation** (:45). Needs a real stage date or a remote schema extension. Also multi-asset per stage (one use per segment). |
| agent/event_calendar.py:703 (`_stamp_media_usage`) | event calendar builder | none stable — best-effort, swallows all exceptions (:705); asset carried as `row["_media_asset"]` (:680) | stamp AFTER calendar write succeeded (:499, :575) | Needs a durable stage ID (calendar row id) and strict (non-best-effort) handling before remote routing. Today the calendar row can remain durable and the arc report success after a swallowed unknown stamp; only a later attempt hits the tenant-lane reconciliation hold. |
| agent/gbp_planner.py:971 | GBP planner | claim + `pick` (asset/base/day_key/store); original_count snapshot (:965) | stamp AFTER calendar insert landed per-row (:959), bounded 2-attempt retry with readback disambiguation (:968-987) | Replace counter-only retry with exact same-`use_id` receipt resolution. An apply can commit and lose its response; the current `used_count > original_count` shortcut then marks `stamped=True` and completes the claim without the remote receipt or local journal settlement. Unknown must hold until exact receipt/readback. |
| agent/media_swap.py:1075 (`reserve_local_pick`) | swap pre-write reservation | claim_id stored on pick (:1073); asset row via store | stamp BEFORE row write (pre-claim), `_drive_stamped` guards double-stamp (:1014-1015) | Route through remote apply at claim time; `use_id` derivable from claim_id. Rollback on release is `restore_unstaged` (:1118-1122) → needs remote never-landed release (unavailable). |
| agent/media_swap.py:1021 (`after_swap` settle) | swap post-write settlement | row post_date; asset re-fetched (:1020) | stamp AFTER row write confirmed; settles old asset's kv only when no sibling row still carries it (:1004-1013, book_rows=None = unknown → never roll back) | Same-day sibling logic (FB mirror / paired story) must move into the remote settle path; unknown-book window currently fails safe and must keep doing so. |
| scripts/restage_future_igfill_photos.py:309 | one-off restage script | none (script run); asset+store held | direct stamp after restage | Manual lane; gate behind remote authority or document as operator-only bypass. |
| scripts/restage_swift_river_photos.py:298 | one-off restage script | none | direct stamp | Same. |

## 3. rollback_use / restore_unstaged call sites (settle paths)

| file:line | trigger | scoping | remote implication |
|---|---|---|---|
| agent/portal_routes.py:309, :313 (`_rollback_use_for_row`) | portal approve/deny action on content_calendar row | podcast by `pillar`; gym-media by row's own `post_date` + `source_media_asset_id` (same-day 2x safe, :289-297) | Needs attempt lookup by (gym, date, asset) — kv record must carry remote `use_id`; deny settle = mark applied-attempt consumed (no counter change). |
| agent/approvals.py:176, :184 (`on_draft_denied` hooks) | Slack deny | podcast_selector.py:244 / gym_media_selector.py:1266 — date+asset scoped | Same as above. |
| agent/gym_media_selector.py:1333 / podcast_selector.py:292 (`observe_denials`) | nightly deny sweep (jobs/sync_gym_media.py:1195, jobs/index_podcast_library.py:194, tenant-scoped wrapper fixer_ops.py:1256) | whole-date settle when denied and nothing live | Unknown-outcome window: sweep skips on fetch failure (:1351); remote settle must keep that skip-or-hold behavior. |
| agent/gym_media_routes.py:536 (hide) + story_studio.py:657 | coach hide / story deny | **cross-date** `rollback_asset` by asset_id | Remote settle must locate attempts by asset across dates; story path additionally rolls `story:{request_id}` date key (:663). |
| agent/client_month_run.py:943 (`_rollback_drive_asset`) + :984 | build abandoned / grade-gate drop (:3976) | `restore_unstaged=True`, date+asset | Counter RESTORE — no remote equivalent (`release_never_landed` raises). Until the never-landed protocol exists these paths must stay on the local writer or hold. |
| agent/runner.py:997 (`_rollback_unlanded_drive_draft`) | daily-run draft never landed | `restore_unstaged=True` + claim release | Same never-landed gap. |
| agent/media_swap.py:1118 (`release_local_pick`) | swap released before row write | `restore_unstaged=True` | Same gap. |
| agent/media_swap.py:1009 (`after_swap`) | old asset displaced, no sibling carries it | settle-only (no restore) | OK once kv carries use_id. |
| agent/story_studio.py:663 | story deny | `rollback_use(gym, "story:{request_id}")` | Non-ISO date key again (§2). |

## 4. Bypass / guard-gap inventory

1. **podcast_selector lane** (stamp :186, callers podcast_library_builder:328,
   real_month_planner:1785, rollbacks portal_routes:309, approvals:176,
   observe_denials, index_podcast_library:194) — different store/pool; remote
   validation (`kind == 'gym_drive'`) rejects it. Either explicitly out of
   scope or needs seam extension. **UNVERIFIED** which is intended.
2. **story_studio date key** `story:{request_id}` — fails remote `post_date`
   validation; routing stories through the seam requires a schema change or a
   real stage date.
3. **event_calendar best-effort stamp** (:689-706) — no stable stage ID, all
   exceptions swallowed; a remote unknown-outcome would be silently dropped
   and then hold the whole tenant lane on the next apply. Must become strict
   before routing.
4. **Manual restage scripts** (restage_future_igfill_photos.py:309,
   restage_swift_river_photos.py:298) call `stamp_use` directly with no claim,
   no stage ID — operator bypass lane. Decide: route through authority or
   document as break-glass.
5. **fixer_ops ledger verification** (fixer_ops.py:1000-1052, 1240-1253)
   strictly parses local kv records and counters; any remote routing must keep
   the kv record shape (`prev_used_count`, `prev_last_used_at`, `rolled_back`)
   or update this verifier, or `_observe_denials_for_gym` (:1256) and the
   evidence sweep break.
6. **Visual-history guard interaction**: stamp sites that skip stamping on
   guard failure (gym_media_builder:590-598 holds draft pre-stamp;
   client_month_run deny-backfill excludes live/denied assets at :3824,
   :3956-3959) are upstream of the seam — the remote authority does not
   re-check visual history (its own docstring, remote_drive_use.py:6, says it
   does not clear those gates), so guard ordering is preserved as long as the
   remote apply replaces `stamp_use` at the exact same call point.
7. **Direct `update_asset` counter writes outside the selectors**: none found
   other than index-init (gym_media_index.py:290) and the selector/rollback
   bodies above. Read-only consumers (auto_reels:187, fixer_evidence:235,
   media_swap:270-276, client_infographic_fill:290/308, jobs/sync_gym_media:814)
   need no change but depend on counter semantics staying identical.
8. **`store.get_asset` freshness**: several stamp sites stamp a caller-held
   (possibly stale) row (media_swap:1021 falls back to `{"id": new}`;
   event_calendar:703 stamps a dict copy). Remote `request_for` requires the
   exact complete DB row — these sites must re-read before apply or hold.

## 5. Same-day siblings & unknown-outcome windows (existing, must survive)

- 2x-day sibling records: kv list keyed `gym_media_use:{base}:{date}`
  (gym_media_selector.py:1120-1131); asset-scoped rollback
  (portal_routes.py:295-297, gym_media_selector.py:1261-1266).
- FB-mirror / paired-story sibling carry: media_swap.after_swap
  book-carries-asset test (:1004-1013); `book_rows is None` = unknown → no
  rollback (fail safe).
- GBP insert unknown-commit window: gbp_planner.py:930-950 (readback, partial
  recovery, claims retained on unseen rows).
- Story staged-vs-durable window: story_studio.py:291-320 (calendar row must
  exist before stamping).
- client_month_run staged-batch window: `_forward_staged_batch_landed` (:996-1008)
  forbids rollback when a durable preparing receipt exists.
- Remote seam adds its own tenant-lane hold on unresolved attempts
  (`remote_drive_reconciliation_required`, remote_drive_use.py:166-173) —
  every best-effort/swallowing caller above must surface that hold instead of
  continuing.

## Holds / unresolved

- Podcast pool in or out of remote scope: UNVERIFIED.
- Story date-key schema: UNVERIFIED (blocks story routing as written).
- `drive_use_version` migration is DRAFT-only; seam unusable until applied.
- `release_never_landed` unavailable → all `restore_unstaged=True` paths
  (client_month_run:943, runner:997, media_swap:1118) cannot go remote yet.
- No production queries run; line numbers verified against working tree
  (base HEAD a9980124).
