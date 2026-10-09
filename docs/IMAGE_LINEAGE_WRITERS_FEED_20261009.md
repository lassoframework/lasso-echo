# Image lineage census — feed / photo / direct content_calendar media writers (2026-10-09)

Scope: every lane under `agent/` that creates or rewrites a `content_calendar`
row's media (feed / photo / direct writers, GBP included), plus every
repost / rehost / thumbnail / rendition entry point those lanes and the publish
boundary use. For each: source original byte/asset identity, rendered output
identity, persisted row binding, history occupancy/reservation, approval gating,
fail-open/hold behavior, and the exact missing requirement for a GLOBAL one-use
guarantee (no reused images, ever).

This complements, and does not duplicate,
`docs/REMOTE_DRIVE_USE_WRITER_CENSUS_20261008.md`, which covers the `stamp_use` /
`rollback_use` counter ledger and the default-OFF `remote_drive_use` CAS seam.
The forward-media authority drafts are covered by
`docs/FORWARD_MEDIA_ISOLATED_LANES.md`, `docs/FORWARD_VISUAL_INDEX_20261008.md`,
and `docs/FORWARD_SCHEDULE_RESERVATION_20261008.md` (all DRAFT / default OFF).

Line numbers verified against the working tree on branch
`codex/echo-image-lineage-census-20261009`.

---

## 0. Shared helpers — build the fix ONCE here, not per writer

| helper | file:line | role | one-use relevance |
|---|---|---|---|
| `media_host.host_media` | agent/media_host.py:152 | THE rehost door: every local/rendered object becomes a durable public URL (`echo/<tenant>/<sha1-16>/<filename>`). Content-addressed dedupe by tenant+sha1; `forward_media_guard.enabled()` switches it to create-once + exact readback (:187-221). | One delivered URL per (tenant, bytes). Same source re-rendered (crop/autofit/poster) yields DIFFERENT bytes → different sha1 → different URL: delivered-URL identity can never prove source identity by itself. |
| `media_host.host_generated_original` | agent/media_host.py:251 | Conditional-PUT + readback for provider-generated originals (`echo-generated-originals/<gym>/<sha256>.png`). | The only byte-exact generated-source host; currently used only by the generated-infographic runtime lane. |
| `gym_media_index.ensure_rendition` | agent/gym_media_index.py:~480-569 (persist :630) | HEIC→JPEG / HEVC→H.264 rendition, cached by content_hash, persists `rendition_url`/`rendition_key` on the media_asset row. Optional observation sink. | Rendition becomes the SERVED object while `content_hash` stays the source identity. One-use must key on content_hash, not rendition_url. |
| `gym_media_index.materialization_observation` | agent/gym_media_index.py:572 | Byte-bound source→delivered observation with sha256 pair + recipe; self-marks `provenance_status: unverified`, requires owner registry/manifest receipts (:604-611). | Candidate evidence only — every writer's "proof" funnels here, and every one of them is unverified by design. |
| `gym_media_builder.still_materialization_observation` | agent/gym_media_builder.py:751 | Replay-verified still observation (recipe replay must equal producer bytes). | Shared by GBP crop evidence and the Drive builder's identity observation. |
| `gym_media_builder.video_poster_url` / `video_poster_with_evidence` | agent/gym_media_builder.py:628 / :649 | Thumbnail/poster entry points: legacy best-effort ffmpeg frame → host (never raises, `""` on failure) vs writer-prep byte-bound receipt path (fail closed, returns None). | THE two thumbnail doors for feed video; podcast builder and media_swap call these too. |
| `feed_image.get_or_make_feed_image` / `build_feed_image` / `make_feed_safe_from_bytes` | agent/feed_image.py:90 / :39 / :61 | Feed autofit rendition (1080x1350, cache `<library>/feedfit/4x5-v1/<sha12>__feed.jpg`), plus publish-time bytes-only belt. All paths fail OPEN (None → raw photo posts). | Rendition identity is `sha256(source)[:12]__feed.jpg`; media_guard.reframe_map resolves it back to the raw basename — the ONLY existing reframe→source resolver. |
| `gbp.crop_4x3` + `gbp_planner._cropped_image` / `_transformed_gbp_image` / `_render_evidence_dict` | agent/gbp_planner.py:124 / :251 / :183 | GBP 1200x900 crop rendition + host, with default-OFF byte-bound render evidence. | GBP's rendition door; same "new delivered bytes" identity problem. |
| `visual_writer_prepare` | agent/visual_writer_prepare.py:28 (`AGENT_VISUAL_GLOBAL_WRITER_PREP`, default OFF) | The prepared-writer boundary: same-object rows and source-rendition rows get exact-URL byte re-reads + owner receipts at insert (:392, :489, :639). | The single seam where source/delivered lineage is verified at write time — when armed. OFF everywhere in production. |
| `rotation.reserve_local_photo_once` / `reserve_local_media_once` / `release_served` / `load_served_strict` | agent/rotation.py:242 / :205 / :382 / :61 | Local-library occupancy ledger (reservation rows with account/key/path/SHA-256 binding; guarded exact release). | The only durable occupancy record for LOCAL photos; per-gym, not global, and bypassed entirely by Drive lanes (they use used_count + socialapi_claims). |
| `gym_media_selector.pick_media` / `pickable` / `claim_drive_content` / `stamp_use` / `is_usable` / `asset_source_ok` | agent/gym_media_selector.py:1067 / :713 / :642 / :1191 / :532 / :616 | Drive pool selection with global-ledger and scene-guard flags (`global_ledger_flag` :66, `scene_guard_flag` :250), tenant + source verification, atomic claim, use stamp. | Selection-time one-use gate for the Drive pool. See REMOTE_DRIVE_USE census §1-2 for the stamp internals. |
| `media_guard` (book_state / row_media_key / reframe_map) + `portal_calendar_store` insert belts | agent/media_guard.py:53-127; agent/portal_calendar_store.py:6211-6300+ (`_media_stage_belt`), :6100 (`_caption_stage_belts`) | Best-effort ONE PHOTO ONE DAY staging belt at the calendar INSERT door. Cross-day belt defaults ON (`AGENT_MEDIA_CROSS_DAY_GUARD`); caption/empty belts default OFF. Media-changing PATCH paths bypass this insert belt. | Keyed by BASENAME (`media_key` :53) of `source_media_url or image_url`. Different names/renditions evade it. On read failure, `portal_calendar_store.py:6309-6318,6372-6377` stages unguarded; exceptions at :6415-6419 also return unguarded payload. Small-library fully colliding multi-day batches may pass at :6391-6405. It is not a global invariant. |
| `media_reuse_policy.publish_hold_reason` | agent/media_reuse_policy.py:94 | Publish-time 9-month reuse hold. `reuse_months` (:35) returns 9 ONLY for `zanshinfitness630e22`, 0 for every other gym. | Global no-repeat at publish exists for exactly ONE tenant. |
| `forward_media_*` lanes (guard/prepare/attester/owner/visual index/thumbnail) | agent/forward_media_guard.py, forward_media_prepare.py:203, forward_media_attester.py:261, forward_media_thumbnail_candidate.py:47 | DRAFT authority chain: original registry, render manifest, visual attestations, occupancy, replay-verified thumbnail bytes. All default OFF / unapplied (see the three FORWARD_* docs). | The designed end-state one-use authority; nothing in production feeds it trusted proof yet. |
| `no_creative_fallback.display_image_for` | agent/no_creative_fallback.py:252 | PORTAL DISPLAY ONLY: renders a typographic card from the row's own approved text and hosts it so the portal can show SOMETHING for a media-less row. Never writes content_calendar. | Rehost/rendition entry point that produces hosted images with no lineage record at all — fine today (display only), but it is one flag flip away from becoming a media writer with zero one-use identity. |

---

## 1. Writers

### 1.1 Drive feed/photo/video builder — `gym_media_builder.build_gym_media_draft`
agent/gym_media_builder.py:168-614 (stamp call :599, draft :527-546)

- Source identity: Drive `media_asset` row (`content_hash` = MD5 of original bytes,
  re-verified against fresh row + downloaded bytes at :306-318 under writer-prep).
  Source URL: hosted original `media_host.host_media(tmp_path)` (:476), kept on the
  draft as `source_media_url` only when it is also the served object (:552-555).
- Rendered identity: rendition URL when HEIC/HEVC converted (`ensure_rendition`,
  :350-365); video poster thumbnail via `video_poster_url` (:420) or the evidenced
  `video_poster_with_evidence` (:403-412). Served media = rendition-or-original (:475).
- Row binding: `Draft(source_media_asset_id=asset.id)` (:545) →
  `content_calendar.source_media_asset_id` via `_row_from_draft`
  (client_month_run.py:662-699) → `insert_rows`. `_drive_claim_id` rides the draft (:584).
- History occupancy: `claim_drive_content` (:578) + fresh-row prior-use check (:589-591)
  + cross-gym source guard (:594) + `stamp_use` (:599). Optional DRAFT forward
  reservation screen of exact source bytes (:502-523, `_screen_reservation_candidate`
  :94-145) — gate default OFF; video holds while armed (no pHash, :503-509).
- Approval gating: PENDING always (:536); claim retained even if receipt write fails (:608-612).
- Fail-open/hold: FAIL CLOSED on claim/stamp/source failures (:579-607 — slot held, any
  landed counter still excludes the asset). Hosting failure holds (:477-480). Writer-prep
  poster failure holds (:405-411). Vision/caption failures try next asset, bounded (:43-47).
- Missing for global one-use: (a) the DRAFT reservation screen is advisory and OFF; the
  sole occupancy authority (`reserve_forward_slot_20261008` at insert) is unapplied;
  (b) used_count is per-asset-row — duplicate Drive uploads of the same bytes are only
  caught by `_byte_hash` alias logic inside pickable, not by a global byte registry;
  (c) stamp→claim-done is two non-atomic writes (census §2 row 1).

### 1.2 Local-photo month lane — `client_month_run` + `client_content.build_client_draft`
agent/client_content.py:174-420 (pick), :860-952 (draft); agent/client_month_run.py:560-604
(accept), :632-659 (`_record_feed_served`), :749-828 (`_finish_feed_with_story`),
:2952-2970 (`_to_rows` + FB mirror), :2589-2644 + :3500-3600 (`_insert_rows_with_poster_evidence`)

- Source identity: local library file path (`creative_path`), cluster key
  `dam.rotation_key(path)`; hosted original URL via `media_host.host_media`
  (client_content.py:889-893). No byte hash is stamped on the row itself.
- Rendered identity: action-cut reel (`_maybe_edit_video`), video poster
  (`_attach_video_poster`), feed autofit (`_maybe_format_feed` :2833-2882 —
  swaps `creative_public_url` to the `__feed.jpg` reframe; held entirely when the
  visual writer guard is armed, :2840-2844), story caption burn (captioned story
  media hosted at :2809-2814).
- Row binding: `_row_from_draft` (real_calendar_mirror.py:~195-259 via
  client_month_run.py:662) carries `image_url`, `thumbnail_url`, `source_media_url`
  (only when armed), `logical_post_id`, reservation proof side channel
  (client_month_run.py:691-698). FB mirror = same image_url on a second row
  (:2963-2969); paired story = same photo by design (`_story_from_feed` :2906-2927).
- History occupancy: `rotation.reserve_local_photo_once` at ACCEPTANCE
  (`_record_feed_served` :646; record_serve=False at pick time by design,
  client_content.py:901-909), exact-binding release for never-landed drafts
  (`_release_unlanded_reservations` :702-741). Pick-time once-used guard reads
  `load_served_strict` (client_content.py:248-262, fail closed) + global byte
  availability `globally_available_local_paths` (:277-281, raises
  `LocalPhotoGlobalLedgerUnavailable`).
- Approval gating: every row PENDING (`_real_row` mapping); grade gate and day-shape /
  slot-capacity assertions abort before any delete (real_month_planner.py:1711-1765
  pattern mirrored in client lane `_apply` :3500+).
- Fail-open/hold: served-ledger write failure refuses the draft (:906-909, :656-659);
  autofit/poster/reel lanes fail OPEN (raw media posts) except under the writer-prep
  guard (:2840-2844 holds). `_capture_raw_hosted_source` + transformed-media hold
  (:763-773) fail closed when the guard is armed.
- Missing for global one-use: rotation ledger is per-gym and local-only; autofit
  reframe identity is resolved back to source only by media_guard.reframe_map at the
  insert belt (basename+sha12) — there is no durable source_bytes→row binding on
  legacy rows, and the guard-dependent `source_media_url` column is unpopulated for
  pre-guard rows.

### 1.3 Client infographic fill (Astra-generated) — `client_infographic_fill.fill_gaps`
agent/client_infographic_fill.py:468-715 (insert :706)

- Source identity: NONE — provider-generated bytes (Astra only, `_generate_astra_only`
  :395-434; NEEDS HUMAN marking on total failure). Same-object contract stamped only
  under the writer guard (`_stamp_same_object_source` :437-465:
  `source_media_url = image_url`).
- Rendered identity: PNG written to library then hosted byte-for-byte (:611-623).
- Row binding: Draft → `_to_rows` (IG feed + FB mirror share the URL, :685-703),
  `logical_post_id` per draft when enabled (:644-646), `insert_rows` (:706).
- History occupancy: none on a media ledger — generated art is unique per render by
  construction; occupancy is the calendar-day rechecks (:547-561, :666-684) and the
  tri-state depletion proof `real_media_status` (:484-494, fail closed on UNCERTAIN).
- Approval gating: PENDING (:635), post_quality gate (:650-653), verified palette or
  hold (:527-535).
- Fail-open/hold: held on inventory uncertainty, missing palette, brief/render/hosting
  failure; insert exception returns reason (:707-709). Check-to-insert race is
  acknowledged non-atomic (:659-665).
- Missing for global one-use: generated rows carry no asset identity; if the same
  hosted URL were ever reused the only guard is the basename belt. Needs the
  generated-original registry (`host_generated_original`) bound at insert — currently
  only the runtime lane (1.5) does that.

### 1.4 No-media Astra seed — `no_media_astra_seed`
agent/no_media_astra_seed.py:300-355 (insert :349)

Direct row writer (no Draft): rendered grounded infographic hosted then inserted as
`{gym_id, account: instagram, format: feed, status: pending}` (:313-317). Same-object
`source_media_url = url` only when `visual_writer_prepare.enabled()`, with an
unknown-guard fail-closed stamp (:326-331). Depletion + empty-day rechecked before
batch insert (:336-347, acknowledged non-atomic). logical_post_id required (:318-320).
Missing: same generated-art identity gap as 1.3; no FB mirror row here (IG only).

### 1.5 Generated infographic runtime — `generated_infographic_runtime.run_calendar_row`
agent/generated_infographic_runtime.py:706-805 (`run_scheduled` :805+)

The prepared owner lane for 1.3/1.4: loads a frozen calendar row
(`OwnerSnapshotLoader` :463-485, binding-change hold), requires latest local
depletion census (:597), runs owner/Astra/reviewer/R2 adapters, and reserves/stages
the result through the owner transaction (`reserved=True`, calendar_row_id, :792).
Never approves or clears holds (:5-6). This is the only lane that hosts generated
bytes via `host_generated_original` with a sha256-addressed original. It patches an
EXISTING pending row's media rather than inserting new rows; the
`image_url != source_media_url` check (:931) enforces same-object lineage. All behind
`enabled()` (:30, default OFF). Missing: flag OFF in production; legacy lanes
(1.3/1.4) still write unbound generated rows when they run.

### 1.6 GBP planner — `gbp_planner.plan_gbp_month`
agent/gbp_planner.py:894-1434

- Source identity: Drive pick (`_drive_photo_candidate` :283-372 — selector-owned
  tenant/eligibility; HEIC rendition becomes the crop's raw source, byte-verified
  against the hosted rendition at :336-347) or local pick (`client_content.pick_image`
  :995, global-ledger hold :998-1001). Raw source hosted for the transformed path
  (:270-275) or byte-verified supplied (:264-269).
- Rendered identity: 1200x900 crop (`_cropped_image` :124-155, mtime crop cache);
  writer-guard path adds `_render_evidence_dict` (:183-223 — md5 pair +
  replay-verified `still_materialization_observation` + per-render UUID).
- Row binding: `_row` (:788-829) carries `image_url` (crop), `source_media_url`
  (raw, guard only), `source_media_asset_id` (propagated, never minted, :820-824),
  singleton `logical_post_id` (:827-828). `insert_rows(render_evidence_by_url=...)`
  side channel when armed (:1242-1253).
- History occupancy: per-run `used_drive_ids`/`used_drive_hashes` (:943-945, :980-991),
  atomic `claim_drive_content` per Drive pick (:1144-1153), local reservations via
  `rotation.reserve_local_photo_once` (:1166-1193) with exact-binding release
  (:685-702), DRAFT armed journal (`gbp_drive_use_journal` :1194-1241 + settle
  :561-653 + recovery :656-682). Insert readback disambiguation (:712-768,
  :1256-1297); stamps only landed rows (:1298-1374).
- Approval gating: every row `pending` (:803); OFFER gated on confirmed real offer
  (:1063-1087); captions A+ or slot skipped.
- Fail-open/hold: extensively fail CLOSED — unseen rows retain claims, stamp failures
  become operational holds (:1375-1429). One residual gap (census §2): the legacy
  `used_count > original_count` disambiguation (:1350-1357) treats counter drift as a
  successful stamp without a receipt.
- Missing for global one-use: same source photo can legitimately appear on GBP after
  the IG reuse window BY DESIGN (media_guard scope note, media_guard.py:16-19) — a
  global "never reuse bytes" guarantee would need the forward occupancy authority ON
  plus a policy decision for the GBP 14-day allowance.

### 1.7 Event arc writer — `event_calendar.stage_arc` / `_attach_media` / `backfill_missing_media`
agent/event_calendar.py:340-507 (insert :474-499), :608-686, :510-578

- Source identity: Drive pool asset via `_sel.pick_media` (:630), cross-gym source
  guard (:644-660); `source_media_url` carried only when the asset supplies one
  (:674-678); `source_media_asset_id` (:679).
- Rendered identity: `_host_asset` download+host (:718+); no transform.
- Row binding: direct row dicts → `_db_row` → `insert_rows` (:477-479); durable-row
  matching by `_media_write_identity` composite (:484-498, :709-715). Backfill PATCHes
  existing rows via `store.patch_media` (:559-577).
- History occupancy: per-call `used` list only (:631, :681). NO claim, NO reservation;
  `_stamp_media_usage` (:689-706) is best-effort and swallows all exceptions.
- Approval gating: rows land pending; A-gate before staging (:399-409, advisory for
  top-up :603-605); approved backfill rows held for reapproval (:547-553).
- Fail-open/hold: THE weak lane — image-less rows are held (:683-686, good), but a
  swallowed stamp leaves the asset reofferable while its row is durable (census §4.3).
  list_month failure treats the book as empty (:370-374) for thinning purposes.
- Missing for global one-use: strict stamp semantics (or remote routing), a durable
  stage ID, and claim integration identical to 1.1.

### 1.8 LASSO real month planner (incl. podcast clips) — `real_month_planner`
agent/real_month_planner.py:1649-1790 (insert :1774, stamp :1778-1787)

Feeds/stories built upstream (drafter/client lanes); this is the delete+insert door
for LASSO's month. Media identity comes from each draft's `creative_public_url`
(hosted earlier by its producer lane). Occupancy: podcast clips stamped only for
assets present in the EXACT inserted rows (:1778-1787, `podcast_selector.stamp_use`);
local-photo occupancy is the rotation ledger from the producing lane. Guards:
day-shape + slot-capacity assertions (:1711-1765), `preserve_and_prune` (:1708-1710),
grade gate (:1600-1643). Fail-open: `delete_month`+`insert_rows` is not one
transaction; an exception mid-apply returns partial counts (:1788-1790) with the
month already deleted. Podcast pool is a separate ledger — see census §1/§4.1
(remote validation rejects non-`gym_drive` sources).

### 1.9 Podcast clip card builder — `podcast_library_builder`
agent/podcast_library_builder.py:200-332 (stamp :328)

Download → probe gate (fail closed :204-208) → host (own-host + byte-verify under
writer-prep :240-271; provider/Zernio otherwise :227-236) → thumbnail via
`video_poster_with_evidence` / `video_poster_url` (:272-282) → PENDING Draft with
`source_media_asset_id` + `podcast_asset` (:295-322). FAIL-OPEN stamp: a stamp
exception is only printed and the draft is still returned (:327-331) — the clip's
calendar row can go durable with the pool counter unstamped (bounded by
podcast_selector's cooldown only). `defer_use` (:323) defers to real_month_planner
(1.8). Missing: strict stamp-or-hold; remote seam rejects this pool (census §4.1).

### 1.10 Story studio — `story_studio.create_story`
agent/story_studio.py:56-212+ (calendar write :456-463; stamps :639; census §2 row 4)

Multi-asset montage: picks eligible Drive RAW pool segments, downloads real bytes,
renders via `story_composer.render_compose` (:185-208), records derived-render
identity in `render_ledger` (both bare SHA-256 and Drive-MD5 alias, :211-212+),
persists story_request + story_render, stages ONE calendar row per render through
`insert_rows` (:456-463) then stamps per segment (:639). Approval: PENDING always
(:20). Holds on missing renderer / ungroundable render (:157-208).
Missing: `post_date` is `story:{request_id}` (fails remote ISO validation — census
§4.2); one row consumes N assets but the row binds no list of source asset ids; the
render ledger is a separate store from content_calendar lineage.

### 1.11 Portal media swap — `media_swap`
agent/media_swap.py:530-704 (`swap_fresh`/`_finish`), :1000-1122 (census §2/§3)

Replaces the media on an EXISTING row (and same-date siblings) after deny/hide:
fresh candidate from Drive or local pool, hosted (:631-637), poster evidence under
writer-prep (`require_original_proof`, :644-666), story re-burn (`_finish` :706+),
then `patch_media`-style row writes with per-variant shapes. Occupancy:
`reserve_local_pick` pre-write reservation (:1075, census) + `after_swap` settle
(:1021) with sibling carry (:1004-1013) and never-landed restore (:1118). Fail
closed on evidence failure (:659-666); never writes on candidate failure.
Missing: pre-write reservation vs post-write settle spans the row write without a
single transaction; local swap path relies on the rotation ledger only.

### 1.12 Onboarding demo seed — `onboarding_demo.seed`
agent/onboarding_demo.py:150-230 (insert :223)

Sample rows (status `draft`, SAMPLE pillar) with an injected `image_for_day(i)`
sample URL shared by that day's feed+FB+story rows (:161-192). logical_post_id per
row (:171, :190). Refuses to seed beside real content (:212-216); cleared before real
content lands (:233-249). Not a publishable-media lane (draft status is outside every
publish lane), but it IS a content_calendar media writer: sample image identity is
untracked — if a real asset URL were ever passed as `image_for_day`, no ledger would
record the occupancy.

### 1.13 Real calendar mirror — `real_calendar_mirror.mirror_to_supabase`
agent/real_calendar_mirror.py:419-466 (`_real_row` :195-259)

LASSO-only mirror: converts stored drafts to rows (`_real_row`, carries image_url /
thumbnail_url / source_media_url when set / source_media_asset_id / slot / event /
needs-media hold / logical_post_id, plus the forward-media observation side channel
:255-258) and delete+inserts them (:443-466). Adds no new media identity of its own;
it is the persistence door for lanes 1.2/1.8 drafts, so every lineage field those
lanes set (or omit) lands exactly here. Poster evidence side channel
`collect_real_drafts(..., poster_evidence_out)` (:285-319).

### 1.14 Publish-time rendition/rehost belt — `calendar_autopublish`
agent/calendar_autopublish.py:617-627, :2114-2115

At publish, out-of-spec image bytes are re-framed (`feed_image.make_feed_safe_from_bytes`)
and the REFRAME is re-hosted under the account tenant, and that new URL is what ships.
This is a repost/rehost entry point that mints a new delivered identity at the last
moment: no lineage record ties it to the row's source (media_guard.reframe_map is the
only post-hoc resolver). `media_reuse_policy.publish_hold_reason` runs here
(:2114-2115) but only for the single 9-month-policy tenant (§0).

### 1.15 GBP send lane — `gbp_worker`
agent/gbp_worker.py:156-169 (`_media_reuse_hold`), :199-227 (`_atomic_gbp_send_hold`)

Publish-side: same single-tenant reuse policy as 1.14, plus the DRAFT
`fixer_authorize_gbp_forward_send_20261008` atomic authority (duplicate-consumption
rejection :219-223). No new media is minted here; it sends the row's exact staged
`image_url`. Relevant because it is the last point a cross-lane duplicate could be
refused — today that refusal exists only in the DRAFT RPC and the one-tenant policy.

---

## 2. P0 gaps (block any global one-use guarantee)

1. **No active global authority.** Every global mechanism is DRAFT/OFF: forward-media
   occupancy + visual index (3 draft docs), `remote_drive_use` CAS
   (`AGENT_REMOTE_DRIVE_USE_CAS_ENABLED` OFF, `drive_use_version` migration
   DRAFT-only), forward schedule reservation (DRAFT 2026-10-08),
   `AGENT_VISUAL_GLOBAL_WRITER_PREP` OFF. Always-on protection today = per-gym
   rotation ledger + Drive `used_count` + best-effort basename insert belt. Nothing stops the
   same source bytes from shipping twice via different lanes, different tenants of a
   multi-profile gym, or different renditions.
2. **Delivered-URL identity is not source identity.** Crop (GBP), autofit reframe,
   video poster, story burn, and publish-time belt all mint new bytes/URLs for one
   source. Only `media_guard.reframe_map` (basename+sha12) and the OFF writer guard
   map rendition→source. Legacy rows have no `source_media_url`; there is no durable
   source-sha256 column populated on content_calendar rows. Fix must bind source
   content hash at STAGE time in the single door (`insert_rows`), reusing
   `visual_writer_prepare`/`materialization_observation` — not per-lane.
3. **Fail-open stamp/settle lanes.** event_calendar `_stamp_media_usage`
   (event_calendar.py:689-706, swallows everything), podcast_library_builder stamp
   (:327-331, draft returned anyway), GBP legacy counter-drift disambiguation
   (gbp_planner.py:1350-1357). Each can leave a durable calendar row whose media is
   still reofferable. (Also census §4.3.)
4. **Publish-time repeat protection covers one tenant.** `reuse_months` returns 9
   only for zanshinfitness630e22 (media_reuse_policy.py:35-41); every other gym has
   zero publish-time repeat refusal.

## 3. P1 gaps

1. **Basename-keyed insert belt.** `_media_stage_belt` keys on
   `media_key(source_media_url or image_url)` — the same photo hosted under two names
   (re-upload, different rendition filename) evades the one-photo-one-day rule.
   Byte/pHash keying needs the forward visual index (OFF).
2. **Podcast pool is a parallel ledger.** Separate store, separate kv, rejected by the
   remote CAS validation (census §4.1) — out of scope or seam extension is
   UNVERIFIED. Its fail-open stamp (P0.3) compounds this.
3. **Story lanes.** `story:{request_id}` non-ISO date key blocks remote routing
   (census §4.2); multi-segment renders bind no per-segment asset list on the row;
   story caption burns create new media identities keyed only via `source_media_url`.
4. **Generated-art identity.** client_infographic_fill / no_media_astra_seed insert
   generated rows with no asset binding unless the writer guard is armed; only the
   OFF runtime lane (1.5) hosts via the sha256-addressed `host_generated_original`.
5. **Never-landed restore has no remote equivalent.** `restore_unstaged=True` paths
   (client_month_run.py:943, runner.py:997, media_swap.py:1118) restore counters
   locally; `remote_drive_use.release_never_landed` raises (census Holds) — these
   lanes must stay local or hold until that protocol exists.
6. **Delete+insert non-atomicity.** real_month_planner / client `_apply` delete the
   month then insert; mid-apply failure leaves a partially rebuilt book with
   occupancy already consumed (real_month_planner.py:1767-1790).
7. **no_creative_fallback** hosts display-only images with no lineage record; safe
   today, but any future promotion of those URLs into `image_url` bypasses every
   guard (§0).
8. **Onboarding demo** writes untracked sample media URLs; harmless while status
   stays `draft`, but nothing validates that a real asset URL is never injected.

## 4. Where the fix should live (avoid duplicate work)

- ONE new-row write-door binding: extend `SupabaseCalendarStore.insert_rows` +
  `visual_writer_prepare` to require/persist source content-hash lineage for
  calendar INSERT writers. Media-changing PATCH paths such as event backfill
  (`event_calendar.py:510-578`) and portal swap
  (`portal_calendar_store.py:1269-1355`) need the same shared authority at
  their mutation boundary; they do not pass through insert_rows.
- ONE occupancy authority: the forward reservation / visual index RPCs
  (`reserve_forward_slot_20261008`, `fixer_forward_visual_index_claim_20261008`)
  instead of new per-lane checks.
- ONE stamp seam: `gym_media_selector.stamp_use` → `remote_drive_use` CAS, with the
  kv record carrying `use_id` (census §2 propagation table); make event_calendar and
  podcast_library_builder strict before routing.
- ONE rendition identity scheme: `materialization_observation` /
  `still_materialization_observation` / `video_poster_with_evidence` /
  `_render_evidence_dict` / `forward_media_thumbnail_candidate` already share the
  byte-bound receipt shape — reuse it for the publish-time belt (1.14) instead of
  inventing a new receipt.
- ONE reuse policy: generalize `media_reuse_policy.reuse_months` /
  `publish_hold_reason` beyond the single tenant before any publish-side global
  guarantee is claimed.
