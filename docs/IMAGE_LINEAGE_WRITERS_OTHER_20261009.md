# Image lineage census — event, Story, podcast, media swap, intake/library, generated infographic fallback (2026-10-09)

Child-agent read-only census of the image/media writers and callers in `agent/`
that were NOT the subject of `docs/REMOTE_DRIVE_USE_WRITER_CENSUS_20261008.md`
(which covers the `stamp_use`/`rollback_use` ledger seams; referenced below as
"the use-ledger census" and not re-derived here). Line numbers verified against
the working tree of branch `codex/echo-image-lineage-census-20261009`.

Terms used consistently below:

- **Source original**: the client-owned bytes (Drive file, texted-link upload,
  podcast clip, intake form text) a row's media derives from.
- **Rendered output / derivative**: bytes Echo produced (rendition, poster
  frame, burned story card, Astra infographic, story montage).
- **Persisted row binding**: the durable record tying media to a calendar slot
  (`content_calendar` row, `story_render`/`story_request`, Draft object).
- **Global repeat guard**: what stops the same bytes/scene being placed twice
  (the Drive once-used rule, served ledger, global visual ledger, scene guard).
- **Selection vs persistence vs publication**: selection = choosing the asset;
  persistence = writing the calendar/draft row; publication = the provider send
  (GHL/Zernio/Meta/GBP). None of these lanes publish; publication is the
  separate claim/claim-guard path.

PENDING row state is not proof that a client approval gate is active in every
deployment. The global auto-approve path must be checked separately, especially
for generated graphics. This census does not establish provider publication.

## 0. Shared selection contract (the Drive pool every lane reads)

`agent/gym_media_selector.py` is the single selection implementation. All Drive
lanes below are thin callers of it, so its guards are enumerated once here:

- `pickable()` `gym_media_selector.py:713-983` — tenant re-assertion (:934),
  cross-gym source guard via `verified_source_ids` (:583-613, fails closed on
  any row contradicting the gym filter), `is_usable` (eligible + not
  coach-hidden, :939-941), in-flight claim exclusion (:945-947), DRAFT global
  visual ledger exact-byte cross-client exclusion (:790-813, flag
  `AGENT_GLOBAL_VISUAL_LEDGER`, fail closed on uncertainty), DRAFT scene guard
  cross-tenant near-frame exclusion + same-tenant cross-date hold
  `scene_review_hold` (:824-928, **not operational**: writer architecture
  rejected, armed = pool closes, :833-840), and the **global once-used rule**
  (:963-969: any prior stage-use of the asset OR a same-byte alias is out
  forever; stage-use is permanent since 2026-10-02).
- `pick_media()` :1067-1115 — `pickable(...)[0]`; owns the one deduped
  pool-empty alert (:1106-1111).
- Approval/coach behavior at selection: coach hides set
  `excluded_by_coach=True` (`gym_media_routes.py:509-526`, index default False
  at `gym_media_index.py:289`) and are honored by every lane through
  `is_usable`. Selection itself never approves anything; every lane below
  stages PENDING behind the human approval gate.
- **Missing here**: the scene guard has no legitimate writer (its own code says
  so), so near-duplicate global repeat protection does not exist; exact-byte
  cross-client protection is behind an OFF flag; `drive_use_version` CAS is
  DRAFT-only (use-ledger census §Holds).

## 1. Event calendar arcs

**Files**: `agent/event_calendar.py` (stage/backfill/publish guard),
`agent/event_engine.py` (arc planning), `agent/gym_event.py`,
`agent/portal_events.py`.

Call path: client/portal event -> `event_engine.plan_event_arc` ->
`stage_arc` (`event_calendar.py`, insert at :473-507).

- **Source original**: none inherently — arc rows are copy-only beats. A row
  with no `image_url` gets a real gym Drive photo via `_attach_media`
  (:608-686): `picker` defaults to `_sel.pick_media` (:630), i.e. the §0
  contract. `_host_asset` (:718-736) downloads the Drive original and hosts the
  same bytes via `media_host.host_media` (identity rendition).
- **Rendered output/derivative**: none (the hosted original is the served
  media). `source_media_url` persisted only when the asset row explicitly
  carries one (:671-678); `source_media_asset_id` always set (:679).
- **Persisted row binding**: `store.insert_rows` of `_db_row` payloads
  (:473-499). The stamp follows ONLY the rows the store returned as inserted,
  matched by `_media_write_identity` (:709-715) — a count-only receipt stamps
  nothing (:495-498). `_stamp_media_usage` (:689-706) is **best-effort and
  swallows all exceptions** (use-ledger census §2/§4.3).
- **Global repeat guard**: the §0 once-used rule plus per-call `used` exclusion
  (:631, :681) within one stage pass. Re-stage occupancy guard (:439-462)
  prevents duplicate arc rows (deny = recreate, max one recreate per beat).
- **Approval / no-coach-review**: rows land `pending` behind the approval gate;
  `backfill_missing_media` (:510-578) explicitly HOLDS approved rows (media
  change requires owner reapproval, :547-553) and patches only pending rows
  through `store.patch_media` (:560-577). Coach hides honored via the selector.
- **Failure/hold/retry**: image-less rows are HELD out of staging, never staged
  blank (:666-668, :683-686); cross-gym source guard holds (:644-660); grade
  gate can refuse the whole arc (hard) or stage pending with an advisory alert
  (:400-416); insert failure returns `ok:False` with nothing stamped
  (:500-502). `guard_publish` (:786-826) is the per-publish guard: cancelled /
  ended / dead link / recap-without-media all revert the row to pending with
  `reject_reason` (`_revert` :829-845) and dead links also alert (:818-824).
- **Exact missing requirement**: the stamp has no stable stage ID and is
  best-effort (a durable row can exist with an unstamped asset, or vice versa
  on a swallowed error); no byte-bound render/source evidence is persisted on
  the row beyond the two URL/asset-id columns; backfill leaves approved
  image-less rows permanently held with no reapproval trigger.

## 2. Story Studio (Story render lane)

**Files**: `agent/story_studio.py`, `agent/story_candidates.py`,
`agent/story_composer.py`, `agent/story_overlay.py`, `agent/story_ledger.py`,
`agent/story_studio_routes.py`, `agent/story_studio_store.py`. Flag
`STORY_STUDIO_RENDER`, default OFF, per-gym allowlist
(`story_studio.py:83-85`).

Call path: coach tap / event one-tap (`gym_event.py:372-404`) /
`story_studio_routes.py:44,243` -> `create_story` (`story_studio.py:56`).

- **Source original**: raw eligible Drive pool assets discovered by
  `story_candidates.discover_candidates` (:100-105; RAW-lane-only, tenant
  asserted, not coach-hidden, re-ingest guard excludes Echo's own past renders
  via the story ledger — `story_candidates.py` header). Each segment is bound
  to the real downloaded source file via the same Drive download the sync job
  uses (:186-193, `bind_source_paths`), and per-segment source identity is
  captured BEFORE render (`_segment_source_identities` :563-600: strong
  SHA-256 when bytes available, else namespaced aliases incl. Drive MD5;
  unknowns counted, never fabricated).
- **Rendered output/derivative**: the multi-clip montage from
  `story_composer.render_compose` (:199-201). Derived identity is dual-keyed:
  strong derived fingerprint + Drive-MD5 alias recorded in the render ledger
  (`_derived_identity` :530-543, `_derived_ledger_keys` :546-555,
  `story_ledger.record_render` :216-218) so a later Drive walk recognizes the
  rendered bytes (the re-ingest guard). Poster/overlay frames are Roxx overlay
  + per-gym avatar rail + safe zones (:133-142).
- **Persisted row binding**: THREE artifacts with a known ordering hazard:
  1. `story_request` + `story_render` persisted BEFORE the calendar row
     (`_persist` :513-527) with per-segment `segment_plan` carrying
     source/derived fingerprints (:247-286) — the only durable JSONB
     provenance field;
  2. the PENDING `content_calendar` row via `_stage_calendar_row`
     (:416-468) — refuses a non-platform `account` (:438-440) and blank media
     (:441-442), stamps `logical_post_id = request_id` when armed (:436-437),
     and returns the REAL row id, which is then re-recorded
     (`_remember_calendar_row` :402-412) because the persisted
     `story_render.calendar_row_id` was written before the insert (:314-317);
  3. segment usage stamped AFTER the row exists (`_stamp_segments` :623-641)
     with date key `story:{request_id}` (:639) — a NON-ISO date that fails the
     remote CAS validation (use-ledger census §4.2).
- **Global repeat guard**: re-ingest ledger (own renders can never come back
  in as raw), §0 selector rules for the pool, per-segment stamp_use so a deny
  can roll back (`_rollback_segments` :644-667).
- **Approval / no-coach-review**: EVERY render lands PENDING
  (DraftStatus.PENDING :236); the module never approves/publishes (module
  header :19-25). Coach hides respected at discovery. `deny()`
  (:334-364) flips the calendar row first (:345), returns segments to the pool
  (:346), and gym-scopes the request/render updates (:353-360).
- **Failure/hold/retry**: a HOLD is honest and stages nothing: overlay reject
  (:140-142), music hold/missing licensed audio (:153-155, :166-173), plan
  hold (:160-162), renderer hold/no output (:206-209), calendar staging
  failure AFTER persist (`_held(..., already_persisted=True)` :309-313,
  :471-510 — updates the already-inserted rows instead of duplicate-key
  inserting; render row goes to `denied` because the status constraint has no
  'held' value, :483-486). An unconfigured calendar store is an error, not a
  silent skip (:444-447). Identity anchor for server-built requests comes from
  the gym's own row (`gym_identity.tokens_for`, :129-132) — invents nothing,
  holds when absent.
- **Exact missing requirement**: `story:{request_id}` date key breaks the
  remote use ledger; `story_render.status` has no 'held' value (held renders
  are recorded as denied); segment plan JSONB is the only durable
  source->derived provenance (no lineage columns); `_stamp_segments` and
  `_persist` are best-effort (store failure leaves a PENDING calendar row
  whose `story_render` may not exist, :245-246).

## 3. Podcast clip lane

**Files**: `agent/podcast_library_builder.py`, `agent/podcast_selector.py`,
`agent/podcast_index.py`, `agent/podcast_caption.py`,
`agent/real_month_planner.py` (:1778-1787 post-persist stamp). Flag
`PODCAST_LIBRARY_STAGE`, default OFF.

Call path: planner slot -> `build_podcast_clip_draft`
(`podcast_library_builder.py:101`) -> bounded retry loop (:138-331,
`_MAX_CLIP_ATTEMPTS = 12` :37).

- **Source original**: Drive clip library asset from
  `podcast_selector.pick_clip` (:139). Caption grounding is the RSS feed entry
  (primary, :134, :154) plus the Drive show-notes Doc (:157-163); filename-vs-
  episode disagreement skips the clip (:146-150). NO fabrication: no feed text
  AND no Doc text = the whole episode is excluded and one deduped alert fires
  (:171-177).
- **Rendered output/derivative**: the clip itself is the original bytes
  (downloaded :199, ffprobe-validated :204-225, probe written back :213-221).
  The poster frame is the derivative: legacy `video_poster_url`
  (`gym_media_builder.py:628-646`, best-effort, never blocks) or, under
  `AGENT_VISUAL_GLOBAL_WRITER_PREP`, the byte-bound
  `video_poster_with_evidence` (:649-748) which renders from the EXACT hosted
  video bytes, verifies JPEG structure, reads the hosted poster back, and
  returns a full evidence dict (:716-728) riding the Draft as a NON-DB side
  channel (`draft.poster_render_evidence`, `podcast_library_builder.py:315` —
  "no column yet").
- **Persisted row binding**: a PENDING Draft (:295-322) with
  `source_media_asset_id = asset["id"]` (:320), `source_fragments` carrying
  the RSS/Doc/clip/claim grounding (:290-309); the planner's
  `insert_rows` persists it (stamp AFTER persist at
  `real_month_planner.py:1778-1787`).
- **Global repeat guard**: `podcast_selector.stamp_use` (:327-328, or deferred
  via `defer_use` :323) with rollback on deny
  (`podcast_selector.on_draft_denied`/`observe_denials`; use-ledger census
  §1). **This pool is NOT gym_drive-sourced — it bypasses the remote use seam
  entirely** (use-ledger census §4.1: `kind != 'gym_drive'` fails remote
  validation; in/out of remote scope UNVERIFIED).
- **Approval / no-coach-review**: Draft PENDING (:304); publish-time recheck
  is the ordinary publish_guard (module header :21-22). No coach-hide concept
  exists for the podcast pool (no `excluded_by_coach` on podcast assets).
- **Failure/hold/retry**: upload failure stops the slot (:237-238); own-host
  byte-mismatch holds the slot under writer prep (:262-271); evidenced-poster
  failure holds (:275-278); un-probed / gate-failed clips are marked and the
  next clip tried (:204-225); temp files always deleted (:283-288). A total
  pool exhaustion returns None and the planner falls through to the legacy
  podcast logic (:141-142).
- **Exact missing requirement**: poster render evidence has no DB column
  (side channel only); provider `publicUrl` is explicitly NOT lineage
  (:240-247 comment); podcast pool is outside the remote use ledger scope;
  own-host byte verification exists only under the OFF writer-prep flag.

## 4. Media swap (portal self-service + cross-day sweep)

**Files**: `agent/media_swap.py`, `agent/portal_calendar_store.py:1269-1355`
(`swap_media`), `agent/portal_social.py`, `agent/media_guard.py`.

Call path: portal swap tap / cross-day guard -> `pick_replacement`
(`media_swap.py:525`) -> `reserve_local_pick` (:1050) -> row PATCH via
`SupabaseCalendarStore.swap_media` -> `after_swap` (:979).

- **Source original**: either a Drive pool asset (`drive_candidates`, §0
  selector incl. claims/cooldowns) or a local-library creative
  (`local_candidates`, served-ledger de-duped). The Drive branch re-reads the
  asset and claims content bytes before any row write
  (`reserve_local_pick` :1058-1084: `claim_drive_content` :1069, stamp :1075).
- **Rendered output/derivative**: story swaps RE-BURN the caption onto the new
  media (`_reburn_story_video` :939-954 via `story_image`), feed swaps may
  transcode; byte-bound evidence when produced (`_feed_render_evidence`
  :910-936: exact source URL readback + delivered == rendered bytes + md5
  fingerprints + evidence_ref).
- **Persisted row binding**: `swap_media` (`portal_calendar_store.py:1269`) is
  status-guarded SERVER-SIDE to `pending`/`coach_review` (:1275-1278,
  :1310-1311, :1316) — an approved/publishing/published row can NEVER be
  repointed; caption, status and date untouched; extra columns limited to
  `thumbnail_url` + `source_media_asset_id` (`media_swap.swap_fields` :970-976,
  `_SWAP_EXTRA_COLUMNS`); CAS pins approval/claim/schedule when
  `expected_row` supplied (:1330-1339); `media_not_ready_reason` released in
  the same write (:1297-1301); scene-alias columns cleared on the old object
  (:1324-1329).
- **Global repeat guard**: Drive hash claim + pre-write stamp
  (`_drive_stamped` :1080); old asset's use record settled only when no live
  sibling still carries it (`after_swap` :1004-1013; `book_rows is None` =
  unknown = NEVER rolled back — fail safe); stage-use permanent (counters
  never restored, :985-989); local picks reserved in the served ledger
  pre-write with exact release identity (:1090-1110).
- **Approval / no-coach-review**: approval is structurally untouchable
  (status-guarded PATCH + CAS pins `approval_kind/approved_by/approved_at/
  approval_digest` :1333). Coach review state preserved; a coach-hidden asset
  is never picked (§0).
- **Failure/hold/retry**: claim/stamp failure HOLDS the write (:1076-1079);
  release is guarded and retains the reservation on unknown outcome
  (`release_local_pick` :1113-1158, `restore_unstaged=True` for Drive —
  which has NO remote equivalent per the use-ledger census);
  `REASON_*` + `client_message` (:1161-1188) give the owner an honest
  plain-language reason; swap receipts exist as a DRAFT RPC lane
  (`portal_calendar_store.py:1357-1378`, `ECHO_SWAP_ACTION_RECEIPT` OFF).
- **Exact missing requirement**: the pre-write stamp precedes the row write,
  so a failed write must rely on `release_local_pick` (never-landed restore),
  the exact path the remote seam cannot do; `after_swap` is best-effort and a
  failed ledger settle is only logged (:1022-1023); evidence
  (`_feed_render_evidence`) is produced only on some paths and persisted
  through the writer-prep lane, not a swap-owned column.

## 5. Intake + library selection

**Files**: `agent/intake_web.py` (upload page + intake form, R2-only process),
`agent/intake_ingest.py` (listener-side processing), `agent/ghl_intake.py`,
`agent/whatsapp_intake.py`, `agent/gym_media_index.py` (Drive index),
`agent/jobs/sync_gym_media.py`, `agent/client_sources.py`.

- **Source original**: (a) texted-link uploads streamed to R2
  `intake/<client>/incoming/` with sidecar (`intake_web.py` header; token
  fingerprint only, raw token never persisted); (b) the intake FORM's fact
  sections -> `client_sources.submit_intake(..., status="pending")`
  (`client_sources.py:138`) — PENDING, never auto-approved; (c) the gym's
  Drive folder -> `gym_media_index` rows (dedupe on Drive md5 `content_hash`,
  eligibility gate fail-closed, HEIC/HEVC rendition on first use cached by
  content_hash — `gym_media_index.py` header).
- **Rendered output/derivative**: HEIC->JPG (EXIF-normalized) and MOV->MP4
  conversions in `intake_ingest.py` (originals ALWAYS archived to
  `originals/` before the incoming copy is deleted, step 3 of the header);
  Drive renditions cached by content_hash in Echo's bucket.
- **Persisted row binding**: SHA-256 dedupe at both raw and converted stages;
  perceptual-hash near-dupes are NEVER deleted — held under `hold/` for a
  human (a pHash is similarity, never identity — `intake_ingest.py` step 4);
  accepted media filed into the client's content-library prefix with the
  client's sentence as the caption note; moderation flagged -> `review/` +
  one Slack notice (step 5). Idempotent via the R2 manifest. The armed
  mutation-receipt guard fences the whole disposition
  (`local_inventory_mutation`, :54-77).
- **Global repeat guard**: content-hash dedupe at index + intake; this is
  INGEST dedupe, not the once-used placement guard (§0 handles placement).
- **Approval / no-coach-review**: intake facts are PENDING sources — the
  drafting path reads ONLY `approved_sources` (`client_sources.py:235-244`),
  so an unapproved intake can never ground a post. Media enters the pool
  eligible but unpublished; every consumer still stages PENDING. No coach
  review happens at intake time; coach hides come later via the portal media
  library.
- **Failure/hold/retry**: zero-byte -> deadletter + alert; conversion failure
  -> deadletter, never crashes the loop; per-file failure -> deadletter + ONE
  ops alert, processing continues; manifest re-run is a no-op.
- **Exact missing requirement**: intake moderation hook is a stub (header
  step 5); near-dupe holds require human action with no SLA/retry loop;
  intake-library media (R2/local) and Drive-index media are two parallel
  inventories with separate repeat ledgers (served ledger vs use ledger) — no
  unified byte lineage across them (only the DRAFT global ledger bridges, and
  only for Drive photos under an OFF flag).

## 6. Generated infographic fallback (client)

**Files**: `agent/client_infographic_fill.py` (flag
`AGENT_CLIENT_INFOGRAPHIC_FILL`, default OFF), `agent/astra_prompt.py`,
`agent/image_engine.py` (`mark_needs_human` :805), `agent/media_bridge.py`,
plus the DRAFT owner lane `agent/generated_infographic_gap_job.py` /
`generated_infographic_gap_owner.py` / `generated_infographic_preparation.py`
/ `generated_infographic_runtime.py` (flag `AGENT_GENERATED_GAP_CRON`, default
OFF, explicit tenant allowlists).

- **Source original (facts)**: ONLY the gym's APPROVED client sources
  (`client_sources.approved_sources` :509-511) derived from its website +
  intake form; the on-image headline is the source's own first clause,
  dash-scrubbed, ~8 words (`_headline_from` :355-365). Nothing invented;
  missing voice doc or sources = no-op (:477-478, :510-511).
- **Source original (palette)**: a configured gym brand palette from
  `<DATA_DIR>/brand_voice/<base>/brand_colors.json` (durable wins, repo
  fallback) via `astra_prompt.load_gym_brand_palette` (:382-401) — values
  must be hex and a nonempty `source_url` or `owner_approved=True` is required.
  The loader does not verify that a URL is official (`astra_prompt.py:401-416`);
  external provenance remains a separate acceptance step. A bare list is
  rejected (:341-356). **No accepted configured palette =
  FAIL CLOSED, the fill is held with the reason** (`client_infographic_fill.py:527-535`).
  LASSO's own account is exempt (locked V3 palette).
- **Drive-photo-first sourcing**: `real_media_status` (:111-263) is the
  three-answer depletion gate — MEDIA_AVAILABLE / MEDIA_DEPLETED /
  MEDIA_UNCERTAIN. Local library AND the Drive selector (with
  `strict_claims=True`, completed-sync proof, pending-moderation hold,
  and `_unproven_empty_pool_detail` :266-334) must BOTH prove exhaustion;
  any unverifiable inventory (stale source link, in-flight claim, unreadable
  counters) is UNCERTAIN = fallback held, never "photos exhausted". Rechecked
  at scan (:484), again per card at generation time (:547-561), and a third
  time immediately before insert (:666-684).
- **Rendered output/derivative**: ASTRA ONLY (`_generate_astra_only` :395-434;
  Blake's 2026-10-02 ruling: no Gemini rung, no generic LASSO fallback), with
  the gym's own voice doc + verified palette threaded
  (`build_infographic_brief` with `gym_palette`, :588-596;
  `gym_brand_palette_section` `astra_prompt.py:428-447` — "use ONLY these
  colors"). Total Astra failure = `mark_needs_human` + day stays empty.
- **Persisted row binding**: PENDING Draft (:626-643) ->
  `client_month_run._to_rows` (IG feed + FB cross-post together) ->
  `store.insert_rows` (:704-709). `logical_post_id` minted and carried onto
  both rows when armed (:44-67, :689-703; invalid existing identity fails
  closed). Category carries the `::needs_client_safe_review` suffix (:82-87)
  so a reviewer can tell Echo's scrape-grounded card from a client photo.
- **Global repeat guard**: photo-first depletion gate above; INSERT-only,
  never replaces a live row (:502-503, :558-561); capped 2/run (:32); same-
  object provenance under the visual writer guard: hosted URL == source
  (`_stamp_same_object_source` :437-465; a stamp that cannot be retained
  holds the day).
- **Approval / no-coach-review**: every row is initially PENDING (:635).
  `::needs_client_safe_review` is reviewer metadata only; no module consumes
  the suffix as an enforced gate (`client_infographic_fill.py:75-87`). The
  normal approval path must remain active, and the global auto-approve path
  (`config.py:1361-1366`, `runner.py:510-524`) needs an explicit generated-card
  exclusion before client approval can be claimed as enforced. No coach review
  is required by this lane.
- **Failure/hold/retry**: brief build failure / hard-rule miss skips the day
  (:569-575, :593-596); render failure marks NEEDS HUMAN (:606-610); hosting
  failure skips (:620-623); caption not A+ skips (:650-653); insert failure
  reported, no partial claim (:706-709); media-bridge notice debounced and
  re-armed only by a new intake upload (:495-508, :712-714). The DRAFT owner
  lane adds journaled bind phases with `generated_gap_commit_uncertain` hold
  (`generated_infographic_gap_owner.py:80-90`) and refuses any calendar
  insert/provider send/approval of its own (module header :1-6) — dispatch
  only enqueues dates; binding uses frozen palette `evidence_ref` +
  `authority_pins` + copy-derivation receipt (:70-83).
- **Exact missing requirement**: the check-to-insert interval is not atomic
  (its own comment :659-665: "necessarily subject to a concurrent writer");
  no byte-bound render evidence is persisted on generated rows (only
  same-object URL equality when the OFF writer guard is armed); the owner
  lane is DRAFT with no production activation.

## 7. Feed card builder (the primary Drive writer, for contrast)

`agent/gym_media_builder.py` (flag-gated per pillar set). Selection is §0;
caption grounded from the vision-analyzed frame with facts only from
`source`/`voice` (:456-472); hosted original or rendition (:474-480);
`source_media_url` stamped only when the hosted original IS the served media
(:552-555); shared Drive content claim (:576-584) then fresh-row prior-use +
cross-gym-source re-check before `stamp_use` (:586-599); stamp failure holds
the draft before persistence (:600-607); claim receipt failure retains the
claim (:608-612). Under `AGENT_VISUAL_GLOBAL_WRITER_PREP` video posters
require the byte-bound evidenced path (:649-748); forward schedule
reservation screening (DRAFT, OFF) screens exact source bytes pre-claim and
holds videos it cannot pHash (:496-523). Missing: the remote CAS seam (use-
ledger census §2 row 1), and poster evidence remains a Draft side channel
with no column (:562-567).

## 8. Cross-lane P0/P1 gaps

**P0**

1. **Global no-repeat is incomplete.** Exact-byte cross-client exclusion is
   behind OFF `AGENT_GLOBAL_VISUAL_LEDGER`; the scene/near-frame guard has NO
   legitimate writer and fails closed forever when armed
   (`gym_media_selector.py:833-840`). Different rendered URLs of the same
   source photo still have no immutable source->delivered provenance (matches
   PROGRESS.md 2026-10-04 hold). Affects every lane above.
2. **Render/poster evidence has no DB home.** Story segment provenance lives
   only in `story_render.segment_plan` JSONB; podcast/feed poster evidence and
   media-swap render evidence ride Draft side channels ("no column yet",
   `podcast_library_builder.py:315`, `gym_media_builder.py:562-567`). Nothing
   durable binds source bytes -> rendered bytes -> delivered URL except the
   DRAFT forward visual index (`docs/FORWARD_VISUAL_INDEX_20261008.md`,
   unapplied).
3. **Story lane cannot join the remote use ledger**: `story:{request_id}`
   date key fails ISO validation, and `story_render.status` has no 'held'
   value (held renders recorded as denied) (`story_studio.py:639`,
   :483-486).
4. **Event arc stamp is best-effort with no stage ID** — a durable calendar
   row and an unstamped asset can coexist after a swallowed error
   (`event_calendar.py:689-706`; use-ledger census §4.3).

**P1**

5. Podcast pool is outside the remote use ledger scope (`kind !=
   'gym_drive'`); in/out UNVERIFIED (use-ledger census §4.1). No coach-hide
   concept for podcast assets.
6. Generated-infographic scan-to-insert is non-atomic (self-documented,
   `client_infographic_fill.py:659-665`); same-object provenance exists only
   under OFF `AGENT_VISUAL_GLOBAL_WRITER_PREP`.
7. Intake (R2/local served ledger) and Drive (use ledger) are parallel
   inventories with separate repeat ledgers and no shared byte lineage.
8. Never-landed release (`restore_unstaged=True`: media_swap
   `release_local_pick`, client_month_run, runner) has no remote equivalent —
   `release_never_landed` raises (use-ledger census §Holds).
9. `drive_use_version` migration is DRAFT-only; the remote seam is unusable
   until applied (use-ledger census §Holds).
10. Intake moderation hook is a stub; pHash near-dupe holds have no human-
    action SLA (`intake_ingest.py` steps 4-5).

## 9. Where Drive-photo-first and verified palette/facts actually come from

- **Drive-photo-first**: `client_infographic_fill.real_media_status`
  (:111-263) + per-generation and pre-insert rechecks (:547-561, :666-684);
  selection itself is `gym_media_selector.pickable` (:713) whose inventory is
  `gym_media_index` Drive rows (md5 `content_hash` dedupe, fail-closed
  eligibility) backed by `media_source` rows verified by
  `verified_source_ids` (:583-613).
- **Configured brand palette**: `astra_prompt.load_gym_brand_palette`
  (:382-401) reading `brand_voice/<base>/brand_colors.json` (durable
  `<DATA_DIR>` first, repo fallback) requiring hex values plus a nonempty
  `source_url` or `owner_approved=True`; code does not validate that the URL
  is official. The owner lane instead requires a frozen
  palette snapshot with `verified is True` + `evidence_ref`
  (`astra_prompt.build_verified_gym_content_brief` :1004-1016).
- **Verified facts**: `client_sources.approved_sources` (:235-244) — intake
  form + website material a human approved (submit_intake always lands
  PENDING, :138); podcast captions ground in the RSS feed + Drive show-notes
  Doc only (`podcast_library_builder.py:152-177`); LASSO content compiles
  from the read-only LASSO Brain (CLAUDE.md).
