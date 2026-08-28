# Echo Build Tracker

Living tracker for the Echo social agent build. This markdown is the source of
truth; the HTML dashboard (`echo_build_tracker.html`) is the visual view. The
full organic-system scope lives in `BUILD_SPEC.md`.

Status key: [x] done  ·  [~] built + tested in reference repo, push/deploy pending  ·  [ ] not started

Last updated: 2026-08-28

---

## Report-card score fixes (2026-08-28, feat/report-card-score)

LASSO's own Social Report Card (90d ending 2026-08-28) scored 67/100 (D). Five
build-time fixes, every new behavior flag-gated OFF; cadence (20/20) protected:
blocked slots re-draft or refill on the next plan pass, never a posting gap.

- [~] HARD never-verbatim-twice (upgrades AGENT_CAPTION_COOLDOWN when armed):
      caption_ledger verbatim layer (trim/case/whitespace normalize, 180-day
      window, dates-list so a row's own stamp never masks an earlier dup;
      same-date rows = ONE post, incl. the is_on_cooldown same-date fix that
      would have self-blocked every armed publish recheck). Belts: planner
      re-draft, insert_rows drop+alert, publish_guard duplicate_caption,
      meta/zernio publisher wires, chat gate. Tests per lane
      (tests/test_caption_dedup.py).
- [~] Empty-caption double belt: stage-time drop+alert in insert_rows behind
      AGENT_EMPTY_CAPTION_GUARD (stories exempt); publish-time always-on rails
      in meta_publisher (new, parity with zernio's) + zernio + publish_guard +
      chat gate (tests/test_empty_caption_guard.py).
- [~] Ask coverage (AGENT_ASK_COVERAGE, B2B/LASSO lane only): every reel
      carries EXACTLY ONE ask family (one destination per post; bio untouched,
      Blake's ruling), month coverage floor >= 70% (AGENT_ASK_COVERAGE_FLOOR),
      testimonial/proof/welcome stay genuine no-ask room; deletes/appends only,
      copy_gate-clean; wired in apply_month_plan before the grade gate
      (tests/test_ask_coverage.py).
- [~] Reels floor (AGENT_LASSO_REELS_FLOOR, >=35% video feed posts). MEASURED
      FIRST per Blake's ruling: forward plan (video mix ON) lands 5.7-19.4%
      video-preferred, so the floor rebalances minimally (b2b -> platform ->
      doctrine days, earliest first). Sprints byte-for-byte intact, thu/sun
      podcast preserved, dated overrides never converted. Lands 35.8-37.0%
      across the live windows; podcast pillar goes past the 25% mix cap (-3
      content_mix, grade gate still has headroom) — flagged for Blake
      (tests/test_lasso_reels_floor.py).
- [~] Owner-voice testimonial pillar (AGENT_LASSO_TESTIMONIAL_PILLAR):
      alternate-Tuesday slot + agent/testimonial_pillar.py drafting ONLY from
      approved social_proof entries (Permission: yes + Verified date, e.g. Fit
      Mamas Tribe $19K -> $47K); nothing approved -> None -> existing-pillar
      fallback, never a fabricated quote (tests/test_testimonial_pillar.py).

Suite: 4078+ passed; only pre-existing allowed failure
(test_clipper_phase0 faster-whisper prereq). Approval gate, copy_gate, trust
ladder untouched.

---

## Story Studio: raw footage -> finished stories (2026-08-28, feat/story-studio)

Spec: ECHO_STORY_STUDIO_BUILD.md (scratchpad). Builds the four missing pieces on
top of what exists (clipping via opus_factory/clipper_render stays DISARMED after
EP124; story formatting AGENT_STORY_FORMAT stays live). Everything staged lands
PENDING; the human tap is untouched. Suite green (104 story+sync tests; full suite
3835 passed, 1 pre-existing env-only failure in test_clipper_phase0 unrelated to
this work). NOT armed, NOT pushed.

### [~] Classifier + "Sort these" queue + re-ingest guard (STORY_CLASSIFIER, default ON)
  - agent/story_classifier.py: raw/finished/ambiguous, intent-beats-inference
    (declared lane wins), signals (OCR burned text, 9:16 3-60s, 16:9/>90s,
    camera-native filename, cut density), re-ingest guard overrides all.
  - agent/story_ledger.py: render_ledger (Supabase + kv fallback), idempotent,
    is_echo_render membership test. Wired into agent/jobs/sync_gym_media.py
    (_drop_reingested drops Echo's own renders before insert).
  - agent/story_sort_queue.py: ambiguous never auto-staged -> queue + coach digest
    (fires only when non-empty). Classifier wired into the sync path (_sort_ambiguous
    over post-probe rows) so ambiguous media reaches a human, zero silent guesses.

### [~] Roxx overlay standard (extends AGENT_STORY_FORMAT)
  - agent/story_overlay.py: ALL-CAPS, <=8 words/line + <=2 lines/frame (3rd -> next
    frame), safe zones (top 250 / bottom 310 of 1080x1920), ~4.5:1 contrast scrim,
    identity anchor, stat/event cards, copy_gate + avatar rail on overlay copy,
    EXACTLY ONE ask frame enforced (assert_one_ask_frame; body carries zero asks).
  - agent/post_quality.py: avatar_breach now per-gym (config.story_hyrox_avatar_gyms
    allowlists a hyrox-avatar client; no gym arg = original all-gyms behavior).

### [~] Music bed + multi-clip composer
  - agent/story_music.py: LICENSED chart-STYLE bed burned in (NOT trending IG audio,
    stated on the card). Default HIGH ENERGY hype; never defaults to chill; 'none'
    carries neither track_id nor license_ref; empty shelf HOLDS (never silent).
  - agent/story_composer.py: input caps (<=5min AND <=900MB else route to Opus),
    2-6 segments 3-15s each total 15-60s, tenant assertion on EVERY segment, all
    heavy ffmpeg steps injectable (HOLDS on a missing renderer, never crashes).
  - agent/story_templates.py: five templates (athlete_stat, member_win, event,
    class_promo, hype_montage); vision picks default, lane/brief overrides.

### [~] Portal "Create a Story" lane (STORY_STUDIO_RENDER, default OFF; pilot allowlist)
  - agent/story_studio.py: create_story orchestrator -> PENDING draft or honest
    HOLD; deny returns segments to the pool + logs. story_studio_store.py mirrors
    the media store (PostgREST, offline-testable).
  - agent/story_studio_routes.py: (status, body) handlers — create / deny / list
    sort queue / resolve. Footage picker REUSES gym_media_routes.handle_list_assets.
  - migrations/story_studio_20260828.sql: story_request + story_render +
    render_ledger + story_sort_queue. ARMING STEP: apply BY HAND (not auto-run).

### [~] Render-lane ARM: routes mounted + real render proven (2026-08-28, feat/story-render-arm)
  - agent/intake_web.py: the four handlers are now MOUNTED on the HTTP router,
    same pattern as the media/events routes (_studio_route helper; token->
    account_key; revoked/unknown = 404; _origin_ok + per-token rate limit on all
    writes; per-gym render gate lives inside the handlers). Paths:
      POST /portal/<token>/studio/story                        (create-story)
      POST /portal/<token>/studio/story/<id>/deny              (deny-story)
      GET  /portal/<token>/studio/sort-queue                   (sort-queue)
      POST /portal/<token>/studio/sort-queue/<asset_id>/resolve (resolve-sort-item)
    Tests: tests/test_intake_web_studio.py (9 router-guard tests, mirrors
    test_intake_web_media_guard.py).
  - agent/story_composer.py: the DEFAULT render primitives are now the REAL ffmpeg
    lane (were stubs that would HOLD every armed render): _default_normalize =
    loudnorm(-16 LUFS) + color normalize per segment; _default_assemble = real
    concat demuxer; _default_music_burn (new) = amix the licensed bed under the
    video audio (auto-bound when a bed is selected). All reuse clipper_render's
    ffmpeg guard, so an ffmpeg-absent / flag-off env still HOLDS honestly.
  - REAL render PROVEN end-to-end (ffmpeg 8.1.2 present): 3 synthetic multi-segment
    source clips -> cut -> 9:16 reframe -> loudnorm+color normalize -> concat ->
    brand end-frame -> licensed music burn -> a real 1080x1920 H.264 mp4 (21.2s).
    Lands PENDING; content_hash in render_ledger; overlay clean (copy_gate, no
    hyrox); music carries track_id+license_ref. Committed as
    tests/test_story_render_real.py (skips when ffmpeg absent; exercises the
    PRODUCTION default primitives, not test-only fns).

Deferred / flagged (unanswered = GAP): (1) .env.example flag docs not added
(permission-blocked file; flags fully documented in config.py docstrings).
ARMING (Blake's tap): set STORY_STUDIO_RENDER_GYMS=pierce (pilot allowlist, keep
STORY_STUDIO_RENDER=false) AND AGENT_CLIPPER_RENDER_ENABLED=true on BOTH the
worker (echo) and web (echo-intake-web, which serves the portal routes). Apply
migrations/story_studio_20260828.sql by hand first. Live smoke: POST
/portal/<pierce-token>/studio/story {asset_ids:[...]} -> 200 staged (or 200 held
with an honest reason); confirm a PENDING card in the approval queue.

---

## Podcast library ingest Waves 1-4 (2026-08-27, feat/podcast-library)

Spec: docs/PODCAST_LIBRARY_BUILD_SPEC.md. Wave 5 (auto-clipping) deliberately
NOT built. Everything staged lands PENDING; the human tap is untouched.

### [~] Wave 1 — Drive access (built, suite green, committed, NOT armed)
  - agent/integrations/drive_client.py: SA readonly client (GOOGLE_DRIVE_SA_JSON,
    falls back to AGENT_GDRIVE_SA_JSON), list_children/walk/download/
    export_doc_text, 6h kv tree cache, streaming .part-then-rename downloads,
    injectable transport (fully offline-testable). No key -> available() False,
    lane inert.

### [~] Wave 2 — index (podcast_asset in Supabase; migration NOT applied)
  - migrations/podcast_asset_20260827.sql — ARMING STEP, apply by hand
  - agent/podcast_index.py: spec classifier (episode from NEAREST ANCESTOR
    folder, never the filename; unknown names log+skip), §2.3 postability gate
    (fail closed, unprobed=null=never selectable), ffprobe helper, PostgREST
    store (paged; indexer never touches probe/selector columns)
  - agent/jobs/index_podcast_library.py: nightly in run_daily behind
    PODCAST_LIBRARY_INDEX (default ON, inert without the SA key); idempotent;
    vanished ids -> removed_from_drive; budgeted probe pass
    (PODCAST_PROBE_MAX_PER_RUN, 20); deny sweep; one-line #ops summary

### [~] Wave 3 — selector (agent/podcast_selector.py)
  - pick_clip: least-used longest-unused postable; 120d clip + 21d episode
    cooldowns; empty pool -> ONE deduped alert + fall through
  - stamps used_count/last_used_at ONLY at stage time; deny rollback via the
    Slack deny hook (approvals.py) AND the nightly observe_denials sweep

### [~] Wave 4 — grounded captions + planner wiring
  - agent/podcast_caption.py: caption grounds in the episode notes Doc or the
    slot does NOT stage (one deduped alert); 150-500 chars, hook first line,
    exactly one ask, copy_gate clean; guest @handle only from doc + allowlist
  - agent/podcast_library_builder.py + real_month_run._podcast: pick -> caption
    -> download+ffprobe validate -> Zernio presign/upload/ready -> PENDING
    draft, behind PODCAST_LIBRARY_STAGE (default OFF)
  - agent/zernio.py: media_generate_upload_link (POST /v1/media/presign),
    media_upload_file (streamed PUT), media_check_upload_status
  - podcast rows recheck through publish_guard like any row (tested)

### Arming checklist (in order; none done yet)
  1. Create the GCP service account (drive.readonly), download its key
  2. Blake shares `Podcast Episodes` (1hfkXefD7kwOWkNIHSc0jOHLkUFbrh-C6) to the
     SA email as Viewer
  3. Set GOOGLE_DRIVE_SA_JSON in Railway env (by hand, never committed)
  4. Apply migrations/podcast_asset_20260827.sql to Supabase by hand
  5. Watch one week of nightly [podcast-index] summaries (clip counts + probes)
  6. Flip PODCAST_LIBRARY_STAGE=true

---

## LASSO-via-Zernio cutover (2026-08-27, feat/lasso-via-zernio, Blake's ruling)

### [~] AGENT_LASSO_VIA_ZERNIO (default OFF => byte-for-byte today; built, suite green, not pushed)
  - WHY: metrics_sync ingests Zernio analytics; LASSO's Meta-direct posts read
    there as an external/second publisher and taint LASSO's own months for the
    learning loop. One publish path = one guard set = A-gate parity.
  - Armed: LASSO's calendar rows publish through publish_client_gyms ->
    zernio_publisher exactly like the seven client gyms (profile/page resolved
    from the 'lasso' gyms row); the Meta-direct lasso lanes (run_slot_ticks +
    the runner once/day sweep) stand down, plus a routing choke point inside
    publish_due makes a Meta-direct lasso publish impossible under the flag.
  - Cutover safety: missing gyms.zernio_profile_id / zernio_default_fb_page_id
    => the lasso lane HOLDS with ONE deduped alert (no drop, no Meta fallback).
  - Setup CLI (idempotent): python -m agent lasso-zernio-setup [--page <id>] —
    stamps profile 6a74a3b977a9ae3719f5c0c0, picks the FB page via
    list_facebook_pages (auto when unambiguous), stamps lasso autonomy so
    today's no-approval model is kept (only the publisher changes).
  - Note when arming: AGENT_CLIENT_DAILY_PUBLISH_CAP now applies to lasso too —
    keep it 0 or >= LASSO's daily row count (2 feeds + story = 3).

---

## Publish-path hardening (2026-08-27, feat/publish-guard-idempotency)

Three items, one branch (Blake's WIRING.md spec + the live LASSO IG duplicates).

### [~] Item 1 — publish guard wired (branch built, suite green, not pushed)
  - agent/publish_guard.py: ONE rail implementation (empty/thin caption,
    copy_gate, proof/results mention, multi-ask, avatar rail, media_ready);
    visible_len = alphanumerics only; STORY caption rails exempt BY DESIGN
    (the audit's 26 empty IG captions were story rows, verified 2026-08-27
    via late_post_id)
  - agent/mentions.py allowlisted_handles (gym_tag_allowlist, member needs consent)
  - agent/caption_trace.py trace_publish (pure logging, CAPTION LOST detection)
  - agent/lasso_tag_seed.py (+ nightly hook in run_daily behind AGENT_MENTIONS)
  - calendar_autopublish recheck CONSOLIDATED onto publish_guard.check (same
    AGENT_CALENDAR_GRADE gating); revert-to-pending now writes reject_reason;
    deduped alert kv publish_blocked:<gym>:<code>
  - zernio_publisher: ValueError on a FEED body with visible_len 0 (stories exempt)

### [~] Item 2 — exactly-once on the meta-direct lane (LASSO IG triple-publish)
  - listener: the day is CLAIMED before the draw fires (+ deploy-overlap re-read,
    + alert_interrupted_draw fail-closed instead of blind refire)
  - runner._claimed_meta_publish: socialapi_claims BEFORE the external call;
    in-flight claims VERIFY against the post log (caption-hash 24h) or HOLD
    with one alert; non-durable-kv processes never auto-publish
  - meta_publisher: 24h (account, caption-hash, media) dedup; release_dedup is
    the explicit human override

### [~] Item 3 — welcome pacing + once-ever (the 5-in-15-minutes burst)
  - welcome_queue.welcome_publish_gate: durable kv once-ever per (gym, account,
    kind) + fleet-wide AGENT_WELCOME_PER_DAY distinct-gym daily cap, checked
    BEFORE publish; cap-blocked gyms requeue; out-of-band (non-durable kv)
    processes fail closed
  - conftest: POST_LOG_PATH quarantined per test (test rows had leaked into the
    real /data/post_log.jsonl on in-container suite runs)

---

## 2026-08-27 afternoon waves (post-cadence): audit fixes + notification storm + publish hardening

### [x] Cadence audit fix wave (506210e) — A+ loop closed
  - E6/A4 MAJOR: allow_reshape (cadence flip skips never-shrink date guard once);
    cadence_applied stamped ONLY on real apply; regression tests
  - A6/E8 MAJOR: mix-tally tests added (drawn concepts, 1x AND 2x, legacy fallback)
  - E4 MAJOR -> DESCOPE #1: replan is async by architecture (spec amended)
  - Portal: PR #468 merged (9f277411) — 23 cadence tests in CI, actor passthrough;
    9 stale-red suites verified green (fixed on main by PR #461)
### [x] Notification storm killed (Blake's 300 alerts, all classes)
  - Test-leak class: conftest quarantines — credentials (a502cd3), /data paths
    (5c264cc), POST_LOG_PATH + LIBRARY_PATH + SLACK_CHANNEL_ID import-time bakes
    (9de7082, bf5adcf). Root cause of the gritx Oct storm + gritx posts_per_day=2
    live write (reverted) was OUR OWN container suite runs against live env.
    /data cleaned: 10 test post_log rows, 40 fake gritx library photos.
  - needs-media: durable-or-silent dedup + preview opt-out (kv_is_durable)
  - infographic fill NameError (never ran since armed) + district_h
    UnboundLocalError + topfuel_fb publish-failure alert dedup — all fixed
  - welcome queue: status filter (onboarding/inactive/archived leads excluded),
    one-summary-line policy, prune CLI, 90-day freshness window (Blake's rule:
    only new clients at 90 days or newer get welcomes; stale queued entries expire)
### [x] Grade F self-fix (2b22bfe, AGENT_GRADE_SELF_FIX=true ARMED 2026-08-27)
  - GYM rubric drops _visual_match (clients upload their own media — Blake's
    ruling); renormalized x100/85; A achievable on caption+mix+consistency+
    audience+ask alone
  - Forward book below A: dup captions regenerated fresh on same photo (pending
    rows only), over-cap days re-pillared, gaps refilled via existing lanes,
    then REGRADED; trailing_30 never alerts; held alerts deduped (state-change +
    1/gym/day); one sweep summary line
  - DESCOPE #2: dup captions now counted across DISTINCT post_dates (same-date
    IG/FB/story mirrors share captions BY DESIGN; old counting made A impossible)
### [x] Publish guard + exactly-once (bf45564, per Blake's WIRING.md)
  - publish_guard.check at the boundary: visible_len (alnum only), copy gate,
    proof/results MUST @mention (allowlist), one-ask rail, avatar rail; stories
    exempt from caption rails BY DESIGN (26 'captionless' posts were stories —
    verified via late_post_id); caption_trace CAPTION LOST detection;
    lasso_tag_seed nightly behind AGENT_MENTIONS
  - Duplicate-post root causes (Pipeboard-verified): meta-direct approve path
    double-fire (no claim) -> socialapi_claims wrapper + 24h caption-hash dedup
    in meta_publisher; daily-draw refire on mid-deploy restart -> day stamped
    BEFORE the draw + interrupted-draw alert (never blind-refires)
  - Welcome burst: once-ever per gym gate + durable fleet-wide daily cap
    (an out-of-band volume-less process had re-welcomed 5 gyms in 15 min)
### [x] Container done-criterion: suite green IN the Railway container
  (3424 passed 0 failed) via four quarantine layers in tests/conftest.py
### Blake taps still open
  - [ ] Kill the EP124 podcast-promo loop (Meta Business Suite Planner first,
    then Zapier/Make/Buffer/Later) — BLOCKS the podcast library build
  - [ ] Pick topfuel's Facebook page (row 8151a344 retries until then)
  - [ ] Rule on descopes #1-#2 above + arm ECHO_CADENCE_2X_ENABLED +
    PORTAL_CADENCE_TOGGLE_ENABLED when ready to launch 2x

---

## 2x posting cadence + portal toggle (2026-08-27, ECHO_CADENCE_2X_ENABLED)

Blake's big build (spec: CADENCE_SPEC.md — 11 approved decisions + 3 additions).
Flag defaults OFF: byte-for-byte unchanged until armed by hand. The portal toggle
SAVES the preference while dark; behavior follows only after the env flip.

### [~] Echo side (built, suite green 3289, UNARMED)
  - config.cadence_2x_enabled (ECHO_CADENCE_2X_ENABLED) + cadence_slot_times
    (AGENT_CADENCE_SLOT_TIMES, default 07:30/18:30); flags printout updated
  - db.set_posts_per_day / posts_per_day (kv portal_cadence_<base>, 1|2 only)
  - agent/cadence.py resolve_posts_per_day: flag OFF -> 1; kv -> shared plane -> 1
  - POST /portal/<token>/cadence (intake_web + portal_social.handle_cadence):
    saves while dark; shared-plane write REQUIRED when Supabase configured (503 on
    miss, both directions — the worker only reads the shared plane)
  - portal_calendar_store gym_posts_per_day / set_gym_posts_per_day (echo_gym_settings)
  - client_month_run: 2 distinct feed+story pairs/day at 2x (avoid_captions hard
    check; photo no-reuse; slot_index 0/1 stamped; feed budget days*ppd); single
    distinct concept -> one pair, logged, never a dup
  - real_month_planner: posts_per_day param; 2nd pair = next _FALLBACK_ORDER pillar;
    cap re-point preserves cadence_slot; _dedup_cadence_categories guard
  - calendar_autopublish slot_time_for_row: slot_index rows -> deterministic
    07:30/18:30 (flag-gated); 1x rows keep the stable hash byte-for-byte
  - Replan on toggle: client_media_sync cadence_applied_<base> stamp forces rebuild
    on change (2x->1x shrinks, 1x->2x grows); approved/locked days never touched;
    LASSO replan happens at its next real-month plan run
  - Mix-counter BUG FIX: grade_card + monthly_report tally the DRAWN concept
    (drafts.data category via monthly_report.pillar_for_post), filename inference
    only as legacy fallback; correct at 1x and 2x
  - Deny volume surfaced: monthly_retro digest line "Denies this month: N of 15
    recreate budget used" (SupabaseRetroStore.deny_count, real count or no line)
  - Migration cadence_20260827.sql APPLIED live (echo_gym_settings.posts_per_day +
    cadence_updated_by; content_calendar.slot_index)
  - tests/test_cadence.py: 20 tests (flag-off no-op, dual-draw uniqueness, thin
    media, locked days, slot times, replan trigger, endpoint contract, avatar rail)

### [x] Defect audit (Blake's Zernio audit 2026-08-27, docs/LASSO_IG_A_PLUS_SPEC.md)
  - Defect A (26 captionless IG posts) INVESTIGATED: both verified ids are STORY
    rows (topfuel/piercefitness) — empty body BY DESIGN, contentType='story',
    caption burned on media (AGENT_STORY_FORMAT=true live). NOT a bug. Added the
    belt-and-suspenders anyway: publish-boundary caption floor for FEED rows
    (empty/thin -> revert pending + alert; stories exempt).
  - Defect B (HYROX): REAL, predates the armed A+ gate (posts 2026-08-13).
    Fixed: post_quality.avatar_breach hard-block (hyrox / competitive crossfit /
    strength|serious athletes; 'crossfit' alone deliberately allowed — gym names)
    at stage (post_issues) AND at the publish boundary. Both ride the already-armed
    AGENT_CALENDAR_GRADE recheck -> live on deploy.

### Backlog from Blake's 2026-08-27 delivery (docs/INBOX_2026-08-27.md)
  - [ ] Podcast library Drive ingest Waves 1-4 (docs/PODCAST_LIBRARY_BUILD_SPEC.md);
        Blake taps: service account + share Drive folder as Viewer
  - [ ] _b2b_faces rail + LASSO account config (docs/LASSO_IG_A_PLUS_SPEC.md §3-4);
        Blake decisions: LASSO into Zernio; kill the rogue ~10:10 ET publisher

---

## Post A-Grade upgrades 2026-08-26/27

Four upgrades, all authorized by Blake, built sequentially. New flags default OFF in code;
Blake's session arms them after audit. Suite green before each commit.

### [~] Grade self-remediation + quiet alerts — agent/jobs/grade_fix.py (AGENT_GRADE_SELF_FIX, default OFF; Blake 2026-08-27: "it should fix it on its own without sending me alot of slacks")
  - GYM RUBRIC (rides AGENT_CALENDAR_GRADE, scoring definition): visual_match leg SKIPPED for
    GYM (clients upload their own media; Echo owns captions + mix only); remaining 5 legs
    (raw 85) renormalized x100/85 to 0-100; B2B proof_numbers untouched; bands + A_THRESHOLD 90 unchanged
  - DUP COUNTING (scoring definition, needs Blake's eyes): same-date IG feed + FB mirror +
    paired story are ONE post (by-design caption sharing); a hash on >1 post_date is the true dup.
    Without this every client book graded F forever on its own cross-post mirrors
  - Self-fix (flag ON, forward book < A): true dup days rewritten FRESH on the SAME photo via
    _clean_draft_for_day (A+ gate, banned words, copy gate, variety), PATCHed pending-only
    (store.patch_pending_plan carries a server-side wipeable status filter: approved/published/
    denied can NEVER be touched); over-cap days re-pillared from a different approved source;
    gaps refilled ONLY via existing lanes (client grow scan / LASSO real month refill, once per
    gym per day); unfillable gaps recorded once in kv, never re-announced
  - ALERT POLICY (flag ON): trailing_30 never alerts; forward_book alerts only after remediation,
    still < A, (score, defect-set) changed vs last kv stamp, max one per gym per day, <= 3 lines;
    plus max ONE aggregated sweep summary line per run. Flag OFF: sweep + alerts byte-for-byte today
  - Tests: tests/test_grade_self_fix.py (12) + test_calendar_grade.py rubric updates (4 new)
  - [~] CAPTION-CRAFT + PATH EXTENSION (2026-08-27 pm, feat/grade-fix-caption-craft; Blake:
    "only focus on the caption and mixture, make it an A+ from our end"): same flag, passes
    ordered dups -> over-cap -> craft/path -> regrade. Craft pass regenerates flagged wipeable
    days (no_ask / thin_caption / hook_too_long) fresh on the SAME photo; the fresh caption
    lands ONLY when it clears the bar (exactly one ask, hook <= 125 chars, length 150 to 500,
    zero soft flags) — never swaps a decent caption for a worse one (attempted/succeeded tracked).
    Booking-ask leg: when < 5 forward rows carry a booking term, ask-free regen days carry the
    gym's REAL voice-doc booking CTA (no intake booking-link field exists; missing CTA = honest
    skip, never invented). Over-cap pass now ITERATES (bounded 6, target-headroom guard) until
    every category <= 25% or nothing can honestly move. Sweep runs up to 3 remediation passes
    while the regraded score improves (gap lanes keep their once-per-gym-per-day kv stamp).
    LASSO/B2B craft pass skipped by design (its gaps are content supply). Flag OFF byte-for-byte.
    Tests: test_grade_self_fix.py now 29.

### [x] Reply-needed coach alerts — agent/inbox_alerts.py (AGENT_INBOX_ALERTS=true, ARMED + VERIFIED LIVE 2026-08-27: "[inbox-alerts] 2 card(s) sent" in worker logs)
  - Daily READ-ONLY sweep per gym (client gyms + lasso): unhandled post comments
    (GET /v1/inbox/comments + per-post threads), mentions, reviews (FB + Google Business) from Zernio
  - Classifier: member_comment / spam / neutral; homoglyph-normalized (the live topfuel spam
    uses Cyrillic lookalikes: "hit hеr uр οn snap"); pinned in tests/test_inbox_alerts.py
  - ONE card per gym per day max (kv stamp inbox_alert_<gym>_<date>, written only after a send);
    card only when actionable; capped at 5 lines, each with real URL + text (100-char truncate)
  - Coach channel via accounts.slack_channel, ops channel fallback (monthly_retro notifier pattern)
  - NEVER replies/hides/deletes; per-gym AND per-source error isolation; wired in runner daily section
  - DRY run against live topfuel data proven (4 member comments + 1 homoglyph spam caught; no Slack sent)

### [x] Hook-quality metric fields — metrics_sync (rides AGENT_METRICS_SYNC; VERIFIED LIVE 2026-08-27: 28 post_metrics rows with engagement_rate populated, is_ad false across the board)
  - post_metrics + reels_skip_rate, watch_total_ms (igReelsVideoViewTotalTime), engagement_rate,
    is_ad (not null default false); migration applied: post_metrics_hook_fields_20260827
  - is_ad rows are observed only, NEVER train the playbook (same treatment as external) —
    excluded in monthly_retro lever_stats + experiment_verdict, still inform the baseline
  - learning_score: reel_skip_rate + reel_watch_ratio (direct avg first, watch_total_ms/views fallback)

### [x] Engaged-audience demographics — agent/jobs/demographics_sync.py (AGENT_AUDIENCE_DEMOGRAPHICS=true, ARMED + VERIFIED LIVE 2026-08-27: 7 rows in gym_audience_demographics — eng/gritx/topfuel engaged+followers, piercefitness followers only, lasso none; see open check below)
  - Weekly per gym (7-day kv gate, zernio_link_ts pattern): IG follower AND engaged-audience
    breakdowns by age/city/country/gender -> gym_audience_demographics (verbatim jsonb, never reshaped)
  - Migration applied: gym_audience_demographics_20260827; run(gyms=None) callable standalone
  - Monthly retro digest cites the newest STORED engaged row ("Engaged audience: 61% women,
    peak 35 to 44" — no dashes); no row or flag OFF -> no line

### [x] Upload confirmation — agent/intake_web.py (no new flag; display-only UX fix, Chris Shimley / Top Fuel ask)
  - Per-file green check ("received") + running "N photos and M videos received" counter
  - Completion banner: "Received! Your content is in. New posts built from these usually appear
    in your approval queue within the hour. You approve everything before it posts." (dash-free)
  - Backend already answered 2xx only after durable R2 store (verified + pinned in
    tests/test_upload_confirmation.py: mid-batch storage failure -> 503, never a fake success)
  - Honest failure copy ("not sent, tap Send to retry"); sent items never re-post on a later Send

Arming status (verified 2026-08-27 from live Railway env + Supabase + worker logs):
ALL FOUR LIVE. AGENT_INBOX_ALERTS=true (2 cards sent), AGENT_AUDIENCE_DEMOGRAPHICS=true
(7 rows written), hook fields riding AGENT_METRICS_SYNC (28 populated rows), upload
confirmation deployed (echo-intake-web SUCCESS 2026-08-26 18:30, after the 18:22 commit).

Open check from the first demographics run: piercefitness stored followers but no engaged
row, and lasso stored nothing. Likely IG API minimums (engaged-audience needs enough
recent engagers) or a missing IG connection for lasso — worth one look, not a defect
until confirmed.

---

## Wave 7 — The Learning Loop (2026-08-26, AGENT_METRICS_SYNC + AGENT_LEARNING_LOOP)

Both flags default OFF: [~] built-unarmed. Flag flip = human tap (WAVE6_HUMAN_TAPS.md TAP 3:
metrics first, retro only after a full closed month of clean metrics). When OFF the system is
byte-for-byte unchanged. DO NOT run the first monthly_retro on real data — built and proven on
synthetic months only. Every post still lands pending; the human approval tap is untouched.

### [~] 7.1 Metrics ingestion — agent/metrics_sync.py (AGENT_METRICS_SYNC, default OFF)
  - Nightly per gym: Zernio analytics pull with source=all; snapshots at post-age days 1/3/7/28
  - Dedupe by platformPostId (duplicate lassoframework IG connection -> one row wins)
  - Calendar join via late_post_id, platformPostId fallback; no match -> calendar_id null, external=true
  - External rows inform the baseline, NEVER train the playbook; null-not-zero on every metric
  - Injectable zernio client + store; run(gyms=None, now=None); read only on the social side
  - Migration applied to Supabase (ooqcvmcjspeltuuhcvlh): post_metrics_20260826 (+ migrations/*.sql in repo)

### [~] 7.2 Feature stamping — agent/lever_stamp.py + calendar lever columns
  - hook_family / ask_type / caption_len_band / time_slot classifiers (ask regexes = copy_gate ASK_RE families)
  - has_member_face only ever from the vision sidecar, never guessed
  - Stamped at stage time in real_month_planner.apply_month_plan (behind AGENT_LEARNING_LOOP)
  - Historical backfill: agent/jobs/backfill_levers.py (best-effort, same heuristics, behind flag)
  - Migration applied: calendar_lever_columns_20260826 (5 additive columns)

### [~] 7.3 The score — agent/learning_score.py
  - engagement_value = 1*likes + 3*comments + 4*shares + 4*saves + 3*clicks + 5*follows
  - score = engagement_value / max(reach, 0.10 * followers_at_snapshot); day-7 scoring snapshot,
    day-28 for follows attribution only; reels watch_ratio = avg_watch_time / duration

### [~] 7.4 Honesty guards — agent/learning_guards.py (each a testable function, regression-tested)
  - sample_floor (MIN_SAMPLE=6); within-gym only (structural); rolling 90-day recency weighting;
    persistence_rule (>=30% lift two consecutive months, or one month at 12+/side);
    format-stratified comparisons; month_is_tainted (second publisher / >20% follower spike / paid boosts)
  - Synthetic viral-fluke regression proves the playbook does NOT move on noise (807-like outlier month)

### [~] 7.5 gym_playbook + bounds — agent/playbook.py
  - load_playbook / propose_update (NEW version row every write, updated_by='monthly_retro',
    evidence jsonb required, old versions immutable — the store is insert-only by construction)
  - apply_bounds: plus/minus 20% drift cap per weight per month; PROTECTED_KEYS refused outright
    (quota floors, avatar rails, ask rules, offer rules, consent, copy gate, approval/publish gates)
  - Migration applied: gym_playbook_20260826 (gym_playbook + monthly_retro tables)

### [~] 7.6 Cross-gym priors — agent/playbook.py compute_priors / seed_playbook_from_priors / break_tie
  - Non-tainted gyms only, anonymous lever aggregates; exactly two jobs (seed a new gym's day-one
    playbook; break ties under the sample floor); own evidence always overrides

### [~] 7.7 Experiments — playbook.label_experiments wired into the planner (behind AGENT_LEARNING_LOOP)
  - ~15% of feed slots labeled '<lever>:<YYYY-MM>'; ONE lever under test per gym per month
  - Migration applied: experiment_column_20260826 (content_calendar.experiment_label)

### [~] 7.8 Monthly retro — agent/jobs/monthly_retro.py (runs the 5th for the prior month, behind flag)
  - Matured metrics -> taint check -> lever scores vs rolling baseline -> experiment verdict ->
    top 3 keep / top 3 stop (with evidence row keys) -> bounded playbook update -> monthly_retro row
  - Digest to the gym's coach channel (SlackPoster notice pattern); LASSO's retro to #ops (ops_alerts)
  - NEVER cites a number without a post_metrics row behind it; digest scrubbed through copy_gate
  - run(month=None, gyms=None, store=None, now=None, notifier=None) — fully injectable; tested on
    synthetic months ONLY (never pointed at real data in this wave)

### [~] Planner consumption (behind AGENT_LEARNING_LOOP)
  - real_month_planner reads load_playbook(gym_id): fallback pillar order biased by pillar_weights,
    time_slot stamps biased by top_time_slots — INSIDE the Wave 2 floors and Wave 5 A-gate, never against them

### [x] Tests — 59 new tests, all green; full suite green (2536 passed; test_higgsfield_renderer
    fails only on this machine's missing higgsfield_client pip module, pre-existing on HEAD)
  - tests/test_metrics_sync.py (dedupe across duplicate accounts, external flagging, join + fallback, flag-off no-op, source=all, null-not-zero)
  - tests/test_learning_guards.py (sample floor 5-vs-6, persistence rule variants, taint exclusion, drift cap, viral-fluke regression)
  - tests/test_playbook_bounds.py (floor/rail/consent/copy-gate refusals, drift clamps, version increments, immutability, priors)
  - tests/test_monthly_retro.py (deterministic synthetic findings, bounded diff, evidence-backed digest, tainted month observed-not-trained)

### Flags (both [~] built-unarmed; flag flip = human tap, WAVE6_HUMAN_TAPS.md TAP 3)
  - [~] AGENT_METRICS_SYNC — default OFF
  - [~] AGENT_LEARNING_LOOP — default OFF

---

## Wave 6 — Rollout infrastructure (2026-08-26, per-gym AGENT_CALENDAR_GRADE_{GYM_ID})

All new behavior behind AGENT_CALENDAR_GRADE as the global gate (default OFF). When OFF the system is byte-for-byte unchanged.
Two human taps in this wave cannot be performed autonomously. See WAVE6_HUMAN_TAPS.md.

### [x] 6.1 Per-gym grade toggle infrastructure — config.calendar_grade_enabled_for()
Added calendar_grade_enabled_for(gym_id) to agent/config.py:
  - Checks AGENT_CALENDAR_GRADE_{GYM_ID.upper().replace('-','_')} first (per-gym override)
  - Falls back to calendar_grade_enabled() (global AGENT_CALENDAR_GRADE)
  - Rollout order per spec: lasso -> ENG -> GRITX -> Pierce -> TopFuel -> global default-ON
  - HUMAN TAP REQUIRED to flip each gym's flag on Railway (see WAVE6_HUMAN_TAPS.md TAP 2)
Updated real_month_planner.py apply_month_plan():
  - calendar_grade_enabled() -> calendar_grade_enabled_for(account_key) for per-gym enforcement
  - Added Wave 6 refill comment: re-run planner after dedupe_forward_book.run() to refill freed slots

### [x] 6.2 Rollout digest job — agent/jobs/rollout_digest.py
Created agent/jobs/rollout_digest.py with run(gyms=None, store=None) -> list[str]:
  - For each gym: reads gym_social_grades (trailing_30 + forward_book) for before/after grade picture
  - Reads count of denied rows with reject_reason='duplicate_purge_2026_08' (Wave 0.2 purged slots)
  - Reads count of pending rows in content_calendar (refilled slots)
  - Reads count of seeded gym_tag_allowlist entries (mentions seeded)
  - Formats a one-page digest per gym ending with "READY FOR FLAG FLIP (human tap required)"
  - Behind AGENT_CALENDAR_GRADE; returns informational message when OFF
  - Injectable store for tests; Supabase REST + Content-Range count headers for live path
  - CLI: python3 -m agent jobs rollout_digest [--gym GYM_ID ...]

### [x] 6.3 Flag-flip checklist — WAVE6_HUMAN_TAPS.md
Created WAVE6_HUMAN_TAPS.md at repo root:
  - TAP 1: PENDING BLAKE TAP — second publisher disconnect (wave0_publisher_finding.md evidence)
  - TAP 2: PENDING BLAKE TAP — per-gym AGENT_CALENDAR_GRADE flag flips (all 5 gyms + global)
  - Exact Railway env var names + post-flip verification commands for each gym

### [x] 6.4 Forward book refill stubs
Added comment in real_month_planner.py apply_month_plan() docstring:
  "Wave 6: after dedupe_forward_book.run(), re-run this planner for each gym to refill freed
   slots. Everything refilled lands 'pending' — coaches tap through."

### [x] 6.5 Tests — tests/test_wave6_rollout.py
7 tests, all green (5 required + 2 bonus):
  1. calendar_grade_enabled_for('lasso'): AGENT_CALENDAR_GRADE=false + AGENT_CALENDAR_GRADE_LASSO=true -> True
  2. calendar_grade_enabled_for('eng'): AGENT_CALENDAR_GRADE=false, no per-gym flag -> False
  3. calendar_grade_enabled_for('topfuel'): AGENT_CALENDAR_GRADE=true (global) -> True (inherits)
  4. rollout_digest.run() returns list of strings with gym name + "READY FOR FLAG FLIP"
  5. WAVE6_HUMAN_TAPS.md exists and mentions both human taps (TAP 1 publisher, TAP 2 per-gym)
  6. (bonus) rollout_digest.run() returns flag-off message when AGENT_CALENDAR_GRADE=false
  7. (bonus) calendar_grade_enabled_for('pierce-fitness') resolves to AGENT_CALENDAR_GRADE_PIERCE_FITNESS

Human taps: TAP 1 (publisher disconnect) and TAP 2 (all per-gym flag flips) are PENDING BLAKE TAP.

Full suite: 2477 passed, 6 skipped, 0 new failures (pre-existing higgsfield_renderer failure excluded; unrelated missing module).

---

## Wave 5 — Calendar grader: A gate (2026-08-26, AGENT_CALENDAR_GRADE)

All new behavior behind AGENT_CALENDAR_GRADE (default OFF). When OFF the system is byte-for-byte unchanged.

### [x] 5.1 agent/calendar_grade.py created
Deterministic, offline LASSO Social Report Card grader. Six legs: consistency (20), content_mix (20), caption_craft (20), visual_match/proof_numbers (15), right_audience (15), path_to_join (10). Total 100 points; A_THRESHOLD = 90. Distinct from grade_gate.py (image gate).
Key behaviors:
  - _consistency: day-gap detection (-4/gap-of-1, -8/gap->3), caption_hash dup detection (-8 per dup occurrence after first; floors at 0)
  - _content_mix: inline 25% category cap (-3 each), unbacked proof slots (-4 each); category_plan.validate_quotas used when quotas passed in
  - _caption_craft: hard block (any copy_gate violation = 0 for whole leg); soft flags (-1 each, floor at 8); median caption < 150 = -4
  - _visual_match (GYM): no vision_derived = -3; stock asset = -5; mixed template_ids = -3
  - _proof_numbers (B2B): numbers in caption (want >=8, -1/missing); @mentions (want >=8, -1/missing); mixed gym_count claims = -3
  - _right_audience: athlete-avatar leak on first line = -5 each; elite/advanced language = -2 each
  - _path_to_join: missing ask = -1 each; GYM booking terms (want >=5) = -1/missing; B2B call asks (want >=12) = -1/missing; bare URL as only ask = -1 each
CalendarGrade dataclass: .total (int), .letter (str), .scores (dict), .defects (list of (leg, row_ref, reason) tuples)

### [x] 5.2 Enforcement loop in real_month_planner.py
Added behind AGENT_CALENDAR_GRADE flag in apply_month_plan():
  - Grades the planned rows before staging (profile via _profile_for: LASSO=B2B, clients=GYM)
  - Remediation loop: up to 4 passes (_remediate: clears dup captions, appends missing asks, rebalances over-cap categories)
  - If still < 90 after 4 passes: ops_alert fires ("NOT STAGING") and returns {"ok": False, ...}
  - If >= 90: attaches grade summary to return dict ("Grade: A (92/100)")
  - Added _profile_for(gym_id) and _remediate(rows, defects) helpers
  - config.calendar_grade_enabled() added to agent/config.py (AGENT_CALENDAR_GRADE, default false)

### [x] 5.3 Publish-time recheck in calendar_autopublish.py
Added behind AGENT_CALENDAR_GRADE flag in publish_due() immediately after EXACTLY-ONCE CLAIM and before the route-by-gym publish block:
  - copy_gate.violations check: row with banned dash/intraword hyphen reverts to pending + ops_alert fires
  - caption_ledger.is_on_cooldown check: row on cooldown reverts to pending + ops_alert fires
  - Both checks are non-fatal to other rows; only the flagged row is held

### [x] 5.4 gym_social_grades table + grade_sweep job
SQL migration "gym_social_grades_20260826" applied to Supabase project ooqcvmcjspeltuuhcvlh (success:true):
  - gym_social_grades table: id uuid pk, gym_id text, "window" text (trailing_30|forward_book), total int, letter text, scores jsonb, defects jsonb, graded_at timestamptz
  - Index: gym_social_grades_gym_window on (gym_id, "window", graded_at desc)
  - Note: "window" is quoted (PostgreSQL reserved word)
agent/jobs/grade_sweep.py created:
  - run(gyms, store, now, alert_fn): nightly per-gym grader; trailing 30 days + forward book
  - Writes to gym_social_grades via injectable store.insert_grade or Supabase REST
  - Alerts coach channel (ops_alerts.alert) when either grade < B (80)
  - Behind AGENT_CALENDAR_GRADE; no-op when OFF
  - __main__ block for standalone: python3 -m agent jobs grade_sweep

### [x] 5.5 Tests
tests/test_calendar_grade.py: 10 tests, all green
  1. Perfect 28-post month grades A (>= 90)
  2. 20 duplicate captions -> consistency score 0, total < 90
  3. 0 ask-containing posts -> path_to_join <= 4
  4. Summit at 44% -> content_mix cap violation defect
  5. Em-dash caption -> caption_craft = 0
  6. profile="B2B" uses proof_numbers (not visual_match)
  7. grade_month returns CalendarGrade with all fields
  8. Band logic: total 90->A, 89->B, 79->C, 69->D, 59->F
  9. A_THRESHOLD is 90
  10. Defects present for violated legs

tests/test_planner_gate.py: 4 tests, all green
  1. Flag ON + plan >= 90 -> stages normally (ok=True)
  2. Flag ON + plan fails then remediation fixes it -> stages after loop
  3. Flag ON + 4 passes can't fix -> NOT staged, one alert fired
  4. Flag OFF -> no grade check, stages regardless

Full suite: 2470 passed, 6 skipped, 0 new failures (pre-existing higgsfield_renderer failure excluded; unrelated missing module).

---

## Wave 4 — Tagging, end to end (2026-08-26, AGENT_MENTIONS)

All new behavior behind AGENT_MENTIONS (default OFF). When OFF the system is byte-for-byte unchanged.

### [x] 4.1 Supabase migrations applied (project ooqcvmcjspeltuuhcvlh)
- mentions_column_20260826: `alter table content_calendar add column if not exists mentions jsonb not null default '[]';`
- gym_tag_allowlist_20260826: `create table if not exists gym_tag_allowlist (gym_id text, handle text, kind text check (kind in ('own','coach','member','partner')), consent boolean default false, primary key (gym_id, handle));`
Both applied via MCP mcp__claude_ai_Supabase__apply_migration. success:true.

### [x] 4.2 agent/tag_allowlist.py created
Consent-gated handle allowlist for @mentions in captions.
Functions:
  allowlisted_handles(gym_id, kind=None, consent_only=False, store=None) -> list[str]
    Returns handles from gym_tag_allowlist optionally filtered by kind and consent.
  validate_mentions(gym_id, mentions, store=None) -> list[str]
    Returns only allowlisted mentions; drops non-list handles and members without consent silently.
  handles_for_category(gym_id, category, store=None) -> list[str]
    Returns [] when AGENT_MENTIONS OFF; otherwise returns kind-appropriate handles per _CATEGORY_KINDS map.
    results/proof -> member (consented) + own; faces -> coach; LASSO b2b -> partner; default -> own.
Store injectable for tests; Supabase REST used in live path.

### [x] 4.3 agent/jobs/seed_tag_allowlist.py created
One-shot seed job (behind AGENT_MENTIONS):
  - Each gym in client_gym_bases(): seeds own IG handle (kind='own', consent=True) from dynamic registry
  - LASSO: seeds own handle (AGENT_LASSO_IG_HANDLE env, default 'lassoframework') + all connected client handles from Zernio (kind='partner', consent=True)
  - Logs to agent/jobs/seed_log_allowlist.txt
  - run(zernio_client=None, log_path=...) -> {seeded, skipped, gyms}
  - Has __main__ block for standalone execution

### [x] 4.4 zernio_publisher.py — mention wiring in publish()
When AGENT_MENTIONS ON and body (caption) is non-empty:
  - Strips gym tenant suffix (_ig/_fb) to get gym_id
  - Calls handles_for_category(gym_id, category) from tag_allowlist
  - Appends handles as @handle lines (newline-separated) after the caption body
  - Stories skip this block (body is '' for stories)
  - Failures are non-fatal: post still goes out if handle resolution errors

### [x] 4.5 Copy gate interlock verified
python3 -c "from agent.copy_gate import scrub; print(scrub('@coach_amanda great session'))"
Output: '@coach_amanda great session' — @handles pass through _PROTECTED_RE untouched.

### [x] 4.6 config.mentions_enabled() added
AGENT_MENTIONS (default false). Printed in __main__.py _status() so test_status_completeness passes.

### [x] 4.7 tests/test_mentions.py — 12 tests, all green
Spec's 8 required tests + 4 bonus:
  1. validate_mentions: own handle on allowlist (consent=True) -> returned
  2. validate_mentions: handle NOT on allowlist -> silently dropped
  3. validate_mentions: member handle without consent -> silently dropped
  4. validate_mentions: member handle WITH consent -> returned
  5. handles_for_category: AGENT_MENTIONS=OFF -> returns []
  6. handles_for_category: AGENT_MENTIONS=ON, category='faces' -> coach handles only
  7. allowlisted_handles: kind='member', consent_only=True -> only consented members
  8. copy_gate.scrub leaves @handle untouched in caption
  9. validate_mentions strips leading @ from input handles
 10. handles_for_category with empty allowlist -> []
 11. validate_mentions with empty input -> []
 12. handles_for_category 'results' -> member (consented) + own

Full suite: 2456 passed (excluding pre-existing higgsfield_renderer failure, unrelated), 6 skipped, 0 new failures.

---

## Wave 3 — Repeat cooldown: caption_ledger (2026-08-26, AGENT_CAPTION_COOLDOWN)

All new behavior behind AGENT_CAPTION_COOLDOWN (default OFF). When OFF the system is
byte-for-byte unchanged.

### [x] 3.1 agent/caption_ledger.py created
60-day caption cooldown + same-month hard block + 30-day concept gap.

Functions:
  caption_hash(text) — normalized SHA-256 fingerprint, 16-char hex. Strips @handles
    and #tags, lowercases, removes non-alphanumeric, collapses whitespace, truncates at 200.
  ledger_key(gym_id, h) — kv key for a gym+hash pair.
  is_on_cooldown(gym_id, caption_text, planned_date, db=None) -> bool
    Returns True when last_used is within COOLDOWN_DAYS (60) of planned_date, OR
    when last_used is in the same calendar month (HARD_BLOCK_SAME_MONTH=True).
    Returns False on any error — never blocks content on kv failure.
  record_staged(gym_id, caption_text, date_str, db=None)
    Upserts kv: {"last_used": "YYYY-MM-DD", "uses": N}. last_used advances only
    when date_str is more recent than the stored value (backfill safety).
  record_published(gym_id, caption_text, date_str, db=None) — same pattern.
  concept_is_on_cooldown(gym_id, concept_key, planned_date, db=None) -> bool
    30-day gap for doctrine/education concept pool. Same kv pattern.
  record_concept_used(gym_id, concept_key, date_str, db=None)

Constants: COOLDOWN_DAYS=60, HARD_BLOCK_SAME_MONTH=True, CONCEPT_COOLDOWN_DAYS=30.
All db calls are injectable; defaults to agent.db via lazy import.

### [x] 3.2 SQL migration applied (Supabase ooqcvmcjspeltuuhcvlh)
Migration name: caption_ledger_20260826
Table:
  caption_ledger (gym_id text, caption_hash text, last_used date, uses int default 1,
                  primary key (gym_id, caption_hash))
Applied via MCP mcp__claude_ai_Supabase__apply_migration. Supabase returned success:true.

### [x] 3.3 agent/real_month_planner.py — cooldown check wired into _build_feed_with_fallback
Behind AGENT_CAPTION_COOLDOWN.
  _cooldown_checked(first_draft, builder, target, day_key, cat, log, _max_attempts=3)
    Up to 3 builder calls per pillar; if all 3 hit cooldown, falls through to the next
    real fallback pillar. Never ships a repeat, never fabricates.
  _resolve_gym_id(target) — resolves account_key from str or object.
  _draft_caption(draft) — extracts caption text from Draft.
  Lazy import of caption_ledger so flag-off path has zero cost.

### [x] 3.4 agent/portal_calendar_store.py — record_staged() wired in insert_rows
Behind AGENT_CAPTION_COOLDOWN. After a successful POST, iterates the returned rows
and calls caption_ledger.record_staged(gym_id, caption, post_date). Failure is
non-fatal (rows are already inserted; ledger is best-effort cache).

### [x] 3.5 agent/portal_calendar_store.py — record_published() wired in mark_published
mark_published(row_id, media_id, published_at, gym_id, caption, post_date) added to
SupabaseCalendarStore. Behind AGENT_CAPTION_COOLDOWN: after a successful PATCH to
'published', calls caption_ledger.record_published(). Also added:
  due_rows(gym_id, run_date, catchup_days=0)
  mark_publishing(row_id, gym_id) -> bool (atomic claim)
  mark_publish_failed(row_id, gym_id)
  stamp_scheduled(row_id, scheduled_at, gym_id)
These are the autopublish store methods calendar_autopublish.py calls; they live here
so tests can inject a FakeStore without needing the real file.

### [x] 3.6 agent/jobs/backfill_caption_ledger.py created
Reads all historical content_calendar rows from Supabase in PAGE_SIZE=1000 pages.
Hashes each caption and calls caption_ledger.record_staged(). Reports count per gym.
Behind AGENT_CAPTION_COOLDOWN (no-op when OFF). Has run(dry_run=False, http=None)
function callable standalone or from CLI (python -m agent.jobs.backfill_caption_ledger).
--dry-run flag counts without writing.

### [x] 3.7 concept-level cooldown in caption_ledger.py
CONCEPT_COOLDOWN_DAYS=30. concept_is_on_cooldown() / record_concept_used() added.
Same kv pattern; concept_key is a short identifier like 'doctrine:speed_to_lead'.

### [x] 3.8 tests/test_caption_ledger.py — 12 tests, all green
Spec's 8 required tests + 4 additional:
  1. caption_hash normalizes @/# tags and punct (same as "ready set go")
  2. caption_hash whitespace invariant
  3. is_on_cooldown False for brand-new caption (not in kv)
  4. is_on_cooldown True when last_used 30 days ago (within 60-day window)
  5. is_on_cooldown False when last_used 61 days ago (outside window)
  6. HARD_BLOCK_SAME_MONTH: same month blocks even 5 days apart
  7. record_staged / is_on_cooldown round-trip: staged -> next day is blocked
  8. concept_is_on_cooldown: blocks within 30 days, allows at 30+ days
  9. record_staged increments uses counter and advances last_used
 10. ledger_key is gym-scoped (gym1 != gym2)
 11. is_on_cooldown returns False on kv error (never blocks content)
 12. record_staged silently passes on kv error (never raises)

config.caption_cooldown_enabled() flag added (AGENT_CAPTION_COOLDOWN, default false).
Printed in __main__.py _status() so test_status_completeness passes.

Full suite: 2444 passed, 1 pre-existing higgsfield_renderer failure (unrelated),
6 skipped, 0 new failures.

---

## Wave 2 — proof/call categories, quotas, 25% cap (2026-08-26, AGENT_CATEGORY_QUOTAS)

### [x] agent/content_categories.py — CATEGORIES and GYM_PILLARS updated
CATEGORIES expanded from 6 to 8: added "proof" (stored, approved social proof assets;
empty pool falls back to 'community' + ops alert, never fabricated) and "call"
(direct CTA posts driving a next step).
GYM_PILLARS tuple added: ("results", "education", "community", "faces", "offer", "invite").
Six gen-pop boutique fitness pillars for client gym account monthly quota planning.
schedule_for_day() docstring updated: notes that proof/call and GYM_PILLARS are governed
by the quota layer, not the fixed 7-day LASSO B2B schedule.

### [x] agent/category_plan.py — quotas, caps, validate_quotas(), category_pct()
All new behavior behind AGENT_CATEGORY_QUOTAS (default OFF); existing plan logic unchanged.

Constants added:
  CATEGORY_HARD_CAP_PCT = 25.0  (hard: any category over 25% = violation)
  B2B_WEEKLY_MIN = {proof: 2, call: 3}
  GYM_MONTHLY_MIN = {results: 4, offer: 4, faces: 3, community: 5, education: 6}

Functions added:
  category_pct(plan_rows, category) -> float
    Percentage of plan_rows that belong to category. Pure, no side effects.
  validate_quotas(plan_rows, profile="GYM") -> list[str]
    Violation strings (empty = compliant). profile = GYM | B2B | ANY.
    Violation format: "<cat>_below_min:<actual>/<required>" or "category_over_cap:<cat>:<pct>%"

Grounding rails documented as inline comments:
  - proof/results: stored, approved assets only; empty pool -> community + alert
  - offer: only while gym's live offer is set; expired -> invite
  - Avatar filter (gen-pop only) is upstream, not relaxed by quota rules
  - Human tap is untouched; quotas govern what reaches the approval queue

### [x] agent/config.py — category_quotas_enabled() flag added
category_quotas_enabled() reads AGENT_CATEGORY_QUOTAS (default false). Full docstring
documents both B2B and GYM quota behaviors, summit ramp unchanged note, and hard rails.
AGENT_CATEGORY_QUOTAS added to _status() in __main__.py so test_status_completeness passes.

### [x] tests/test_category_plan.py — 24 tests total (14 pre-existing + 10 new Wave 2)
Wave 2 tests added:
  test_categories_includes_proof / test_categories_includes_call
  test_gym_pillars_has_six_entries / test_gym_pillars_includes_all_required
  test_validate_quotas_violation_for_zero_proof_posts
  test_validate_quotas_violation_for_zero_call_posts
  test_validate_quotas_compliant_b2b_plan_no_violations
  test_validate_quotas_no_violations_for_compliant_plan
  test_category_pct_one_of_four_returns_25 / _empty_rows / _all_same
  test_25_pct_cap_violation_detected / _format / test_exactly_25_pct_is_not_a_violation

Full suite: 2444 passed, 4 pre-existing higgsfield_renderer failures (unrelated), 0 new failures.

---

## Wave 1 — copy_gate.py single dash gate, 9 call sites migrated (2026-08-26)

### [x] agent/copy_gate.py created
Single house-style gate for every piece of client-facing text Echo emits.
Functions: scrub() (rewrite, never reject), violations() (hard failures),
soft_flags() (quality flags for calendar grader), ASK_RE (CTA detection).
URL/handle/hashtag hyphens pass through untouched. Intraword hyphens
become spaces. Banned dashes (em/en/figure/bar/minus) become ", ".

### [x] 9 call sites migrated to copy_gate
Files that shrunk their local dash logic:
- welcome_review.py: no_banned_copy() now delegates to copy_gate.violations()
- video_editor.py: build_higgsfield_prompt() uses copy_gate.scrub()
- creative_studio.py: _scrub_dashes() delegates to copy_gate._DASH_RE (prompt
  text only; intraword hyphens like 'left-aligned' preserved in prompts)
- voice_template.py: render_template() assertion delegates to copy_gate.violations()
- weekly_report.py: build_report() assertion delegates to copy_gate.violations()
- pdf_report.py: _scrub() applies PDF typography then copy_gate.scrub()
- podcast_quote_card.py: _guard_verbatim() uses copy_gate.violations() + hyphen check
- no_creative_fallback.py: _clean() delegates to copy_gate.scrub()
- clipper_render.py: scrub_onscreen() delegates to copy_gate.scrub()
Also migrated: content_categories.py, podcast_release.py (found by repo-wide guard).
Backward-compat shim (_DashRE) added to podcast_release for existing importers
(podcast_cards, podcast_learn, podcast_month, podcast_touches).

### [x] tests/test_copy_gate.py — 26 tests, all green
Tests 1-6: scrub() rewrites (em dash, en dash, intraword hyphen, URL, handle, tag).
Tests 7-9: violations() hard failures.
Test 10: ASK_RE matches 13 CTA phrases.
Tests 11-13: soft_flags() quality flags.
Test 14: repo-wide guard (zero local _DASH_RE definitions outside copy_gate.py).

Full suite: 2430 passed, 4 pre-existing higgsfield failures (unrelated), 0 new failures.

---

## Wave 0 — Preflight: second publisher + forward-book dedupe (2026-08-26, commit 39fbf62)

### [x] 0.1 Second publisher investigation
Evidence assembled in `wave0_publisher_finding.md`: 29 posts on lassoframework IG
(Aug 10-26) not attributable to Echo's calendar; duplicate Zernio IG connections
(6a69fc9cdf17280d93d0727f in Default profile, 6a74b3efd0fe733d1abc6fc1 in lasso
profile); 14:10 ET cadence pattern on the unexplained posts. Recommendation +
disconnect checklist surfaced to #ops. NOTHING disconnected — Blake tap required
(WAVE6_HUMAN_TAPS.md TAP 1).

### [x] 0.2 Forward-book dedupe job — agent/jobs/dedupe_forward_book.py
Groups future pending rows per gym by caption_hash (Wave 3 definition), keeps the
earliest, denies the rest with reject_reason='duplicate_purge_2026_08' through
portal_calendar_store (never direct SQL). Behind AGENT_DEDUPE_FORWARD_BOOK
(default OFF; OFF forces dry-run). tests/test_dedupe_forward_book.py green.

### [x] 0.2 EXECUTED for lasso (2026-08-26, Blake-authorized one-shot)
Dry-run then live via `railway run --service echo`: 369 pending future rows,
300 duplicates denied, 69 unique kept, 0 errors. Before/after posted to #ops.
Flag set inline for the one-shot only; Railway env untouched. Freed slots refill
via the planner once the Wave 5 A-gate is armed.

---

## Blake's 4 rulings + 5-domain A+ sweep + studio unblock (2026-08-24/25)

Nine commits, all merged to main and LIVE on both Railway services (worker `echo` +
`echo-intake-web` deployed SUCCESS 2026-08-25 13:46, SHA e87a0f0). Suite 3013 green.

### [x] Client posts silently not reaching IG/FB (8cfbf5d, 7b44903, b3afee9)
ENG: out-of-aspect portrait photos 400'd at Zernio -> publish-time feed aspect preflight
re-frames any out-of-aspect image to an in-spec 1080x1080 card before the network call
(heals stuck rows + guarantees no bad aspect ever ships); preflight fails SAFE (confirmed
out-of-aspect it cannot fix -> HOLD, never send). Pierce: gym's Zernio profile now also
matches by DISPLAY NAME (portal UUID-keyed gyms had an unlinked profile).

### [x] Studios not posting — served-ledger un-poisoned (b2216bd)
rotation.record_served ran at PICK time, before the A+ gate dropped the draft; the frequent
lane burned every plannable photo into its reuse window (GritX: 43,136 served rows for 193
photos). Served is now recorded on ACCEPTANCE only.

### [x] AGENT_VISION_ALLOW_FLAGS — operator-approved safety flags may auto-pick (5bf4462)
Comma list of vision flags that no longer BLOCK auto-pick (still detected + recorded on the
sidecar). Default EMPTY = unchanged safe default. ARMED for the studios with
third_party_brand,person_name_in_image,minor_prominent (Blake 2026-08-25).

### [x] Calendar never shrinks + rebuild churn stopped (ad5543e, 32d3381, 6513c0d)
Never-wipe-to-empty generalized to never-SHRINK on rebuild; grow-guard (kv built_media_<base>)
rebuilds an already-built gym ONLY when its library has grown — data safe AND the wasteful
every-pass caption regen stops.

### [x] 5-domain A+ sweep — every CRITICAL + top MAJORs closed (1392cea)
Five independent domain audits (connecting/paying/posting/captions/portal) + live Pierce
upload bug. CRITICALs: **SECURITY** GET /portal/gym/<key> now REQUIRES X-Portal-Key (it
reconstructs the gym's raw portal token; unauthenticated = slug-guess portal takeover);
**scaffold leak** SB7 fallbacks strip prompt-hint blocks + post_quality rejects hint markers
anywhere; **publish timing truth** client lane fires each row AT its slot with publishNow
(no more midnight catch-all sweep / future scheduledFor); **display_name clobber** fixed;
**race guards** approve 409s mid-claim rows, mark_published stamps only the claimed row.
MAJORs: GBP exactly-once claim + 409-dedup=success, open-redirect allowlist, per-gym edit
claim gating, sources base<->_ig fallback + approve-sources CLI, autonomy-off hard 503 on
shared-write failure, stall-state alerts, truncated-JPEG salvage on intake.

### [~] Billing gate — AGENT_PUBLISH_BILLING_GATE (e87a0f0, flag OFF)
A client gym whose Stripe sub shows CANCELED holds ALL publishing (rows stay approved) +
one deduped ops alert. Fail-OPEN polarity: only positive cancellation evidence blocks; no
key/no customer/flaky read never holds a paying gym. kv-cached ~6h, read-only Stripe.
UNARMED — arm on Railway when Blake says go.

### [x] Per-gym timezones — gyms.posting_timezone (e87a0f0)
Each gym's slots fire on ITS OWN wall clock; publish-lane slot gate is date-aware in
gym-local time; scheduled_at stamps in the gym's tz. Unset gyms keep global
POSTING_TIMEZONE (zero behavior change until set). CLI: set-timezone.

### [~] Infographic fill — AGENT_CLIENT_INFOGRAPHIC_FILL (e87a0f0, flag OFF)
Blake's override of the MEDIA-ONLY law for the no-photos case: on-brand nano infographic
cards from the gym's own APPROVED sources, SB7-captioned, full A+ gate, INSERT-only PENDING
rows, IG+FB mirror, capped 2/pass. Wired at awaiting_media + has_calendar-running-dry.
UNARMED — arm on Railway when Blake says go.

### [x] X-Portal-Key header fix SHIPPED (lasso-ops-portal PR #453) — but see the audit below
PR #453 (squash a032244) merged to portal main; Vercel production READY, aliased
ops.lassoframework.com. AGENT_PORTAL_KEY set in Vercel Production. HARD LESSON on the key:
`vercel env add` silently stores an EMPTY value from piped/redirected stdin (CLI 54.13.0
says "Added" either way) — the first three attempts stored "". Final set went through the
Vercel REST API, then verified by sha256 round-trip (env pull hash == Railway hash,
695f1300…) AND live: anon GET /portal/gym/<key> -> 401, with-key -> past auth. NEVER trust
`vercel env add` without a pull-and-hash check. Production redeployed to bake the value.

### [x] P1 + P2 BUILT, AUDITED, LIVE-VERIFIED (Blake ruled "build both", 2026-08-25)
- **P1 (portal PR #454, squash 3e63ec0, prod READY on ops.lassoframework.com):** loadGym
  reads echo_account_key from echo_intake_tokens by gym_id (parallel with the gyms name
  row); the erroring gyms-column select is gone.
- **P2 (Echo 8e48694, intake-web deployed SUCCESS 16:26):** sqlite miss -> existence via
  Supabase echo_intake_tokens (exact eq; select never touches intake_token_encrypted);
  fail CLOSED on no creds / error / no row BEFORE any HMAC, so no blind mint. Dead
  decrypt_token call replaced with link_for (deterministic mint reconstruction),
  plaintext fallback kept. Independent security audit: 0 CRITICAL / 0 MAJOR; all 4
  MINORs applied (fallback consults R2 denylist so revoked reads REVOKED cross-container;
  REVOKED gym serves NO link on either path; Supabase timeout 8s; hermetic secret test).
  Suite 3022 green (+9 tests).
- **LIVE E2E (16:27 ET):** lasso/eng/districth/topfuel -> 200 with reconstructed
  upload_link + ACTIVE; never-onboarded slug -> 404; anon -> 401. Panel chain is whole:
  page reads the right table, sends the right header, key matches, Echo answers.
- Follow-up nit (pre-existing, not built): the do_GET route calls
  handle_portal_gym_status without r2, so last_upload_at/upload_count are always null
  on this route; wire the R2 client through if the panel should show upload activity.

### [x] Hill Country partial-connection fix + connection_watch (2026-08-26, commit 24ac033)
Root cause: Hill Country connected Instagram only; Meta's Instagram OAuth dialog mentions
Facebook Pages permissions, so Gina reasonably believed all three platforms were connected.
Nothing on our side noticed for days — she reported it in Slack herself.

**UX fix (intake_web.py CONNECT_PAGE):** "All three need their own approval" explicit warning
above the platform buttons; "Not yet" state on unlinked platforms with a counter showing
"X of 3 connected. Y still to go." and "All set" on full connection. Closes the Meta-dialog
confusion for every future gym owner.

**Systemic fix (agent/connection_watch.py, ARMED):** Sweeps every client gym via Zernio
every 6h (paced). When a gym has SOME platforms but not all, and that exact missing set has
persisted past the 24h grace window, fires ONE deduped ops alert naming the missing platforms
and the CLI command to mint the gym's connect link. Fully connected clears stamps so a later
disconnect + partial re-alerts. Zero connected is not partial (onboarding lane owns that).
Per-gym Zernio errors never block the sweep. AGENT_CONNECTION_WATCH armed on Railway worker.
Tests: 10 new (all green); full suite 3032 green.

Send Gina her connect link to complete Facebook + Google Business:
https://echo-intake-web-production.up.railway.app/portal/aGlsbGNvdW50cnk.xni0RY7v8gfglQd3fy5eKj1vgm0/connect

### Original audit finding (2026-08-25, for the record): the staff social-status panel
### had NEVER worked end-to-end — two pre-existing defects (both now fixed above)
The header + key were necessary but not sufficient. Verified live with the real key:
- **P1 (portal repo):** `social-status/page.tsx` `loadGym` selects `echo_account_key` FROM
  `gyms`, but the portal DB has no such column — it lives on `echo_intake_tokens` (migration
  0254, keyed by gym_id; real values are base slugs: lasso, eng, districth, gritx, topfuel,
  piercefitness…). The Supabase query errors -> every gym renders "not found".
- **P2 (Echo, intake_web.handle_portal_gym_status):** intake-web has NO volume, so
  db.gym_get reads the repo-committed echo.db (0 gyms) -> authed requests 404 for EVERY
  real key (verified live: lasso/eng/districth all 404 with a valid key). Also the
  upload-link reconstruction calls `intake_tokens.decrypt_token()` which DOES NOT EXIST
  (AttributeError swallowed by bare except — dead code). The real reconstruction primitive
  is the deterministic `intake_tokens.mint()`; existence should resolve via Supabase
  echo_intake_tokens (intake-web has SUPABASE_URL + service key), never by blind-minting
  (a mint for a never-onboarded slug still verifies -> uploads would land under it).
Neither defect is new — the endpoint 404'd before the auth change too. Fix plan needs
Blake's go: (P1) page reads echo_account_key from echo_intake_tokens by gym_id;
(P2) endpoint falls back to Supabase-token existence + mint-reconstructed upload link
when sqlite misses. Both small; P2 touches a raw-token surface so it gets the full
build->independent audit loop.

---

## Echo Vision — image understanding + grounded captions (ECHO_VISION_SPEC.md, 2026-08-17)

Autonomous build, phase loop (build → independent audit → fix → re-audit to zero → this log).
KEY FINDING at spec-map time: this is a v2 EXTENSION of the existing DAM v1 (`agent/dam.py`:
autotag/near-dupe/consent), not greenfield. Rulings (Blake): analysis lives on the DAM
SIDECAR (no DB table; `sync_uploads` preserves it across re-syncs); reuse the Gemini path +
spend cap (+ per-slot/per-gym-monthly on top in P4); DCT-pHash; crop-verify checks what
SHIPS (GBP 1200×900 crop, IG/FB the original); `intake_web` owns the upload UI;
`text_in_image` firewalled from the drafter. Per-gym flag `AGENT_VISION_GYMS` (default none);
flips only at next `build_client_month` with pending/approved frozen; LASSO dogfood diff to
Blake before any client gym; adversarial set must route 100% before any default-on.

- **[x] P0 — adversarial harness** (`tests/test_vision_adversarial.py`): §9.3 set routes 100%
  through the real coerce+routing (name tag, whiteboard-PII, before/after collage, athlete
  comp, minor-prominent, blurry burst, empty-gym, gender-leak, third-party-brand). Standing
  acceptance bar.
- **[x] P1 — analysis v2** (`agent/vision.py`): v2 `media_analysis` schema; identity
  firewall on one_line/subjects/details (never text_in_image); DCT-pHash + Hamming;
  `caption_eligible_details` (≥0.85); `auto_plannable` (excludes safety/person-name/athlete/
  identity-leak/unusable/missing-failed); `analyze_and_store` on the sidecar (idempotent =
  preserve-on-re-sync; 3-attempt fail → `analysis_failed`+alert); `analyze_library` backfill;
  ingest hook in `client_media_sync` (per-gym, best-effort). Independent audit → 3 findings
  (1 CRITICAL guardrail-11 leak, 1 MAJOR name over-block, 1 MINOR flat-pHash) FIXED; re-audit
  0 material remaining (one safe-direction over-block noted for the dogfood-diff revisit).
- **[x] P2 — library hygiene** (`agent/vision.py` cluster_library/cluster_count;
  `agent/rotation.py` reuse_blocked; `client_media_sync` starvation wiring): Hamming-≤6
  near-dupe clustering on the ingest pHash (writes dupe_group; rotation collapses a burst to
  one creative); cluster-count starvation guard caps the month at clusters + fires a coach
  gap alert before a thin month; per-platform reuse windows (IG/FB 60d, GBP-after-IG 14d,
  GBP-same-month 30d). Served prune bumped to ≥60d. Independent audit → 1 defect (greedy
  clustering was order-dependent / non-transitive); FIXED with UNION-FIND (deterministic,
  transitive-closure "burst=one cluster") + an order-independence test. Other 4 items
  CONFIRMED (no false-merge, safe starvation floor, correct windows, no rotation regression).
- **[x] P3 — planner content scoring** (`vision.content_score`/_SLOT_PREFS;
  `client_content.pick_image` vision branch; `client_month_run` weak_match alert): pick_image
  content-scores images to the slot job (activity+people+setting affinity + quality +
  recency), excludes every flag class (guardrail 13), restricts athlete_leaning/unclear to
  BTS slots, skips reuse-windowed clusters, below-floor → weak_match (per-build staff alert,
  never silent). Deterministic tie-break. Legacy rotation unchanged for non-vision gyms
  (recency now cluster-keyed via dam.rotation_key = basename fallback). Independent audit → 0
  material findings.
- **[x] P4 — caption chain** (`vision.crop_verify`/`grounding_contradictions`/`policy_screen`;
  `client_content.build_client_draft` verify-then-draft; `post_quality.post_issues` grounding
  gate): §3.5 crop-verify re-checks the SHIPPED pixels (ruling 4: GBP the 1200x900 crop, IG/FB
  the original) and confirms the people bucket + each caption-eligible (≥0.85) detail; a caption
  may lean ONLY on survivors. Closed 4-claim contradiction gate (people-quantity fails CLOSED
  on an unconfirmed bucket, outdoor, crop-rejected objects by word-boundary, high-risk
  identity+numbers) runs in the A+ gate — contradiction-only, absence passes; inert on non-vision
  drafts. Independent audit → 1 CRITICAL (crop_verify fell back to the stale ingest bucket) +
  1 MAJOR (false-positive solo/outdoor words + substring object check) + number-gating FIXED;
  re-audit CONFIRMED both closed, 0 material.
- **[x] P5 — consent + client_context** (`intake_web.handle_upload`; `client_media_sync`
  `_read_context_consent`/`_write_sidecar`; `build_client_draft` context screen): per-file
  `client_context` (raw material, never verbatim output — screened by `context_usable`:
  health/review-bait/weight-promise + dash/hashtag/phone/banned, no shape checks) and consent
  (CHECKBOX only — the laundering guard means consent is NEVER inferred from context text). A
  consented term unlocks an identity/number claim ONLY when it also appears in the context.
  Independent audit → 0 material defects (laundering, stale-bucket, index-alignment, non-vision
  regression all CONFIRMED closed); one latent-fragility note (B4) hardened below.
- **[x] P6 — rollout** (`agent/config.py` flags; `vision.within_gym_budget`;
  `client_content._shadow_log_pick`; `agent/vision_dogfood.py`): per-gym `AGENT_VISION_GYMS`
  (default EMPTY — off for every gym); a gym flips only at its next `build_client_month` with
  pending/approved frozen. Ruling 2: `within_gym_budget` per-(gym,month) Gemini-call cap
  (`AGENT_VISION_GYM_MONTHLY_CAP`, default 400) layered on the global daily cap, alarm-once
  runaway guard. §9.4 SHADOW (`AGENT_VISION_SHADOW`): analysis + scoring run and LOG the
  would-be pick, but the ship stays FULLY legacy. **LASSO dogfood diff** (`agent.vision_dogfood`,
  `python3 -m agent.vision_dogfood lasso`): per-pillar old-pick (vision off) vs new-pick (vision
  on) + reason — the go/no-go deliverable to Blake BEFORE any client gym converts. Audit B4
  hardening: `_write_sidecar` now MERGES into a pre-existing sidecar (never clobbers a reviewed
  note) so a stale sidecar can't swallow this upload's consent+context; consent recorded at most
  once via a marker. Independent audit → 0 material defects (all 6 probes BUILT: default-off,
  budget layered-not-replacing, shadow ships legacy, B4 no-clobber/no-laundering, dogfood
  read-only, no rail regression); one cosmetic note (monthly-cap key derived from folder
  basename) hardened by threading the canonical `base_key` through `analyze_library(gym=)`.

**Rollout gate (hard limits, unchanged):** the adversarial set (`test_vision_adversarial.py`)
must route 100% before any per-gym flag defaults on; the LASSO dogfood diff goes to Blake before
any client gym converts; `AGENT_VISION_GYMS` ships empty. Full suite green after P4-P6.

**Shipped + turned ON (2026-08-18):** merged to main (PR #18); `AGENT_VISION_GYMS=district_h,
eng,gritx,topfuel` set on the Railway `echo` worker (Blake turned vision on for ALL client gyms,
consciously overriding the dogfood-first gate; approval gate + adversarial routing + identity
firewall are the backstops). The GritX calendar-rebuild-churn fix shipped in the same merge for
every gym.

**Avatar rule amended (2026-08-18, Blake):** competitive CrossFit / HYROX / athletes are now a
valid LASSO audience for EVERY gym. `auto_plannable` no longer excludes `avatar_fit == "athlete"`;
`bts_restricted` now only restricts `unclear` (athlete/athlete_leaning score like any photo);
the video b-roll prompt no longer excludes athletes; the adversarial `athlete_comp` fixture flips
to plannable + caption-safe. Safety exclusions (minors, PII, third-party brand) and the identity/
body-word firewall are unchanged — captions describe the athletic/HYROX ACTIVITY, never a person's
body. The org-level avatar rule still needs a manual edit in organization settings to be
authoritative across sessions.

---

## Dale round 2 beta feedback (Aug 18 post, 2026-08-17) — 5 items, gym-agnostic

Independent trace confirmed each root cause in live code before the fix. Suite 2730 -> 2750
green (+20 tests). No gate weakened (nothing publishes without a human tap; no direct
content_calendar approval; no fabrication).

### [x] 1. Content mismatch on a youth VIDEO — round-1 grounding VERIFIED to cover video
The round-1 scene-hint fix already covers VIDEO: `client_content.pick_image` returns image
OR video and `build_client_draft` passes the ACTUAL picked creative into
`make_caption(creative=...)`, which feeds `photo_grounding(creative)` (sidecar note +
humanized filename) to SB7 as a scene hint. No code change needed. Regression test
`tests/test_youth_video_grounding.py` pins a youth VIDEO end-to-end -> youth-matched
grounding reaches the SB7 prompt (a hash-named video adds no false scene).

### [x] 2. Edit/save glitches — durable-first, learning can never fail a saved edit
`agent/portal_social.py::_handle_edit_supabase` restructured: the store round-trips (the
DURABLE caption write) run in their own try; `_learn_from_edit` runs AFTER, guarded, so a
slow/failing brain write can no longer flip a persisted edit into a 500 the client keeps
retrying. Same durable-first note in `agent/portal_routes.py::_handle_action_supabase`.
Test `tests/test_edit_save_resilient.py` (caption persists + returns 200 even when learning
raises; patch precedes learn).

### [x] 3. "Reason" text leaked into the caption — proven backend-clean, frontend specced
Both edit routes write `content_calendar.caption` = EXACTLY the note (never the reason);
the reason is recorded only as the edit's tenant_brain `rule` and echoed via
`reason_captured`. The leak is a PORTAL concat bug. Test
`tests/test_edit_reason_no_caption_leak.py` (caption == note byte-for-byte, reason absent,
reason_captured true, on both routes). Frontend fix specced:
`docs/PORTAL_SPEC_disconnect_and_scheduled_time.md` §5a.

### [x] 4. False approval on the next day — proven backend-clean, frontend specced
Approve flips ONLY `content_calendar.id == <id>` (+ gym_id) via `set_status`; the PATCH is
filtered by id AND gym_id, one row, never a sibling / cursor advance. The false "Approved"
on the next card is PORTAL optimistic state. Test
`tests/test_approve_marks_only_target_row.py` (exactly one PATCH, target id only, sibling
untouched). Frontend fix specced: §5b.

### [x] 5. No caption on a story — publisher HOLDS stale/blank, rebuild honors the edit
A story publishes empty-body, so the caption lives only on the burned media; editing a
story caption updated content_calendar.caption but the hosted image_url still carried the
old/absent caption. Schema-free fix: the burned story media filename embeds the caption key
(`story_image.story_media_carries_caption`), so `calendar_autopublish._story_media_is_stale`
HOLDS a story whose media does not carry its current caption (never ships stale/blank), and
the calendar rebuild RE-RENDERS the story with the CLIENT'S edited caption
(`client_month_run._edited_story_captions` + `_maybe_format_story` prefers the story's own
caption). Test `tests/test_story_caption_saved_shows.py`. Residual: content_calendar keeps
no RAW source URL, so instant edit-time re-burn needs a `source_media_url` column (portal
migration) — logged in §5c as a backlog decision, not a blocker.

---

## GBP rail (Google Business) — §12 A+ bar MET, all 7 hand-off gaps CLOSED (2026-08-17)

Approved spec: `GBP_BUILD_SPEC.md` (v2). My scope: Phase 0 (preflight), Phase 3 (planner
GBP lane), Phase 5 (publish worker), Phase 6 (reviews, later). Portal owns Phase 1
(connection UI), Phase 2 (migrations), Phase 4 (approval card). Phase 7 (reporting) not mine.

### [x] Phase 0 preflight — all four answered
- **P0.1 analytics add-on: YES.** Read-only probe of `/v1/analytics/googlebusiness/performance`
  and `/search-keywords` with our live key returned HTTP 400 "Invalid accountId format" — past
  auth + entitlement to param validation (an unentitled add-on 402/403s first). No entitlement
  wall. Definitive confirmation lands with a real connected GBP account at dogfood.
- **P0.2 gmb-media (photo drops): YES.** Probe of `/v1/accounts/{id}/gmb-media` also returned
  400 invalid-accountId (reachable), AND Blake confirmed live from billing. Photo drops STAY in v1.
  (Spec said `gmbmedia`; real endpoint is `gmb-media` under `/accounts/{accountId}/`.)
- **P0.3 per-location cost: ~$0.30/month per connected location** (Blake, from live billing). No
  multi-location cap needed for v1.
- **P0.4 profile_id per DFY gym:** existing DFY gyms have one (ENG, GritX verified). Founding-five
  GBP-only gaps close through `zernio_routes._ensure_profile_id` on connect.

### [x] §7.2 status model DECISION (Blake): option (b) sync + reconcile, NOT webhook
There is NO existing Zernio webhook receiver (code-audited; FB/IG marks status synchronously). v1:
`create_post` returns → mark published (provisional) → **reconcile polls `GET /v1/posts/{id}` HOURLY
for the first 48h after publish, then stops.** A demotion applies the full §7.2 classification
(transient→one retry; policy rejection→`failed` + plain-English `reject_reason` + staff alert;
deleted→`deleted`); NEVER auto-requeues. Webhook receiver is a v2 upgrade (noted in spec §7.2).

### [!] Legacy: `agent/gbp_publisher.py` is SUPERSEDED — do NOT extend
It publishes direct to mybusiness.googleapis.com/v4 with a hand-set token (imported by
approvals.py, config.py). Per Blake's portal-session ruling, ALL GBP publishing routes through
`zernio_publisher.py` per the spec. Leave the legacy file untouched.

### [x] Task #28 (ruled to the Echo session, 2026-08-17) — CLOSED, 0 audit findings
Echo-side backend support for the two portal (Vercel) optimistic-state bugs + the story
instant re-burn; the exact Vercel frontend diff lives in
`docs/PORTAL_SPEC_disconnect_and_scheduled_time.md §6` for the portal session to apply.
- **5a reason field:** edit responses ECHO `reason` + `reason_captured` (both surfaces); caption
  stays the note only (fabrication gate runs first; reason never enters the caption).
- **5b false-approval:** approve/deny/kill/requeue + the generic action path return the WRITTEN
  row's authoritative `status` + `day_key` (`_action_result`); `/social` already per-post
  authoritative. UI binds each card to its own id — no carry-over.
- **5c story re-burn:** DRAFT migration `migrations/DRAFT_content_calendar_source_media_url.sql`
  (to Blake, NOT applied). Behind `AGENT_STORY_SOURCE_MEDIA` (default OFF, under
  AGENT_STORY_FORMAT): planner stores each story's raw source url (omitted from the row when
  unset → pre-migration inserts safe), a story-caption edit re-burns immediately + swaps
  image_url; best-effort (never fails the saved edit). SEQUENCE: apply migration → arm flag.
Independent audit: 6/6 CONFIRMED, 0 material defects. Suite 2811 passed.

### [x] Phase 2 migration LANDED (confirmed 2026-08-15)
`information_schema` on the portal project (ooqcvmcjspeltuuhcvlh) shows all six GBP columns on
`content_calendar`: `gbp_topic_type`, `gbp_cta_type`, `gbp_cta_url`, `gbp_event` (jsonb),
`gbp_offer` (jsonb), `gbp_location_id`. Planner writes against these live.

### [x] Offer/CTA source DISCOVERED — real columns, gaps flagged
Live front-end offer NAME → `onboarding_intake.offers` (jsonb array). Redeem/CTA URL →
`onboarding_intake.ghl_link` (the GHL funnel). **No CTA-type override column** and **no coupon /
offer-window / terms source** exist. Rulings applied: CTA defaults to `LEARN_MORE`; offer window is
a planner default (10d, validator cap 30); coupon/terms are OMITTED, never invented. `resolve_offer`
requires BOTH a real name AND a redeem URL, else the OFFER slot is skipped (never a dead offer).
GAP for Blake (backlog, not a blocker): if you want per-gym CTA-type overrides or real coupon
codes/terms in GBP OFFER posts, a source column must be added; today those fields are absent by
design, not fabricated.

### [x] Phase 3 planner lane — BUILT (`agent/gbp_planner.py`, draft-only)
Cadence §5.1: 8 STANDARD + 1 OFFER (only when a real offer resolves) + 0–2 EVENT (real only) +
4 photo drops. Every STANDARD/EVENT caption clears `gbp.caption_issues` (A+) or its slot is
SKIPPED — A+ or nothing. GBP copy rules enforced (80-char hook, 150–300 chars / 1500 hard cap, no
hashtags, no phone, city named once, no dashes, CTA carries the ask, UTM `?utm_source=google&
utm_medium=organic_gbp&utm_campaign=echo_{pillar_slug}`, OFFER omits callToAction, CALL exempt from
UTM). Image cropped to 1200×900 (4:3) at PLAN time (the exact pixels the owner approves). Rows land
`status='pending'`, `account='googlebusiness'`.

### [x] Phase 5 publish worker + §7.2 reconcile — BUILT (`agent/gbp_worker.py`, draft-only)
`publish_due_gbp` (approved+due rows → Zernio `create_post_raw(draft=True)` this run), photo drops
via `create_gmb_media` (§6.4; draft simulates, live uploads), and `reconcile_gbp` (option (b): poll
`GET /v1/posts/{id}` hourly for 48h then stop; policy→failed no-retry, transient→one retry,
deleted→deleted, never auto-requeue). Routing: 0/2+ connections → failed + alert; `needs_reconnect`
→ silent hold. Listener lane (`agent/listener.py`) runs both only when `gbp_publish_enabled()` and
is a no-op otherwise; draft unless BOTH `AGENT_GBP_PUBLISH` and `AGENT_PUBLISH_ENABLED` are armed.

### [x] Independent audit — two findings CLOSED, re-audit clean
Round 1 (planner+worker): **#1 MAJOR** photo-drop rows (format=photo, empty caption) were
un-sendable → added `zernio.create_gmb_media` + `gbp_worker.publish_photo_drop` + photo routing in
`publish_one`. **#2 MODERATE** phone regex missed bare 7-digit locals (555-0198) → `_PHONE_RE`
fixed (image dims `1200 900` and year ranges `2015 2020` still pass; regression asserts added).
Round 2 (dogfood entrypoint + listener lane + store): 6 findings → all CLOSED, re-audit clean,
0 material remaining. **2 CRITICAL:** swallowed idempotency read error re-inserted a duplicate
month (now aborts without write); dead offer resolver called `_get` on the wrong object (now reads
via `GbpStore.onboarding_intake`). **1 MAJOR:** `GbpStore.available()` was always True so the
creds-less no-op could never fire (now checks `_url`/`_key`). **3 MODERATE/MINOR:** stale terminal
row blocked re-plan forever (now excludes failed/denied/deleted); planner reported phantom success
without `insert_rows` (now returns ok:False); `resolve_offer` stringified a dict element (now reads
name/title/label). +7 regression tests. Hard limits (zero live publishes, pending-only, no
fabrication, legacy dead, OFFER omits CTA) verified intact through both rounds.

Full suite: **2726 passed, 11 skipped** on main (GBP rail + Dale's FB/IG fixes merged).

### [x/!] Dogfood for `portal_gym_key='lasso'` — PROVEN on real material; WRITE runs on the worker
`agent/gbp_dogfood.py` (idempotent, gym-agnostic core, flag/CLI: `python3 -m agent.gbp_dogfood
lasso Carmel`) resolves the gym's REAL voice + library + offer + connection and calls the planner.
Proven end-to-end on 100% real LASSO material (no fabrication):
- **7/8 real facts** from `brand_voice/lasso_now.md` produced A+ GBP captions (each names Carmel,
  no hashtags, in-range). The 8th was correctly **skipped by the figure-fabrication gate** — the
  anti-invention guard firing as designed.
- **5/5 real library images** cropped to exactly 1200×900; `content_library/` holds **14 real
  lasso images** ≥ the 12 needed (8 STANDARD + 4 photo). LASSO has no `onboarding_intake` offer
  row, so the OFFER slot is skipped by design; no real events → 0 EVENT.
- **[x] Pending rows LANDED (2026-08-17).** Ran `agent.gbp_dogfood lasso Carmel` on the deployed
  worker (where R2 + material live): **12 pending googlebusiness rows for gym_id='lasso'** — 8
  STANDARD (real captions from lasso_now.md, each names Carmel, LEARN_MORE → lassoframework.com,
  real R2-hosted 1200×900 crops) + 4 photo drops (image only). 0 skipped. Verified in
  content_calendar.
- **Bespoke-material fix (Blake ruling 2026-08-17):** the live dogfood exposed that LASSO's content
  is NOT in the client_sources pipeline (0 approved sources for lasso_ig; cards flat in
  content_library/lasso_*). The planner correctly BLOCKED rather than fabricate. Fix: injectable
  `facts` source in plan_gbp_month (LASSO's facts parsed from lasso_now.md copy bank) + a flat-card
  image picker. 100% real approved material; every A+/figure/no-dash gate still runs. No FB/IG lane
  change.
- **Rows plan with no gbp_location_id (LASSO not connected yet); this is correct** — the worker's
  `resolve_connection` binds LASSO's single connection at publish time. **Your only actions:** click
  Connect on LASSO's GBP listing, then approve the pending posts.

### GATE 2 REVERSED (Blake ruling 2026-08-25): coach screening is OFF
Blake does not want a coach reviewing every gym's first month ("that is pending on the
first month, take that off"). AGENT_COACH_SCREEN_FIRST_MONTH=false and
AGENT_GBP_COACH_SCREEN=false set on the echo worker; first months now write straight to
`pending` (owner sees them immediately). Zero rows were held in coach_review at flip
time, so nothing needed releasing. The code paths stay in place (flags could re-arm
later); GATE 1 (OFFER-only-when-confirmed) is unchanged.

### [x] Two planner gates (Blake, 2026-08-17) — "a failure we can't eat"
Both live in the Echo planner (+ the Echo backend read), NOT the Vercel portal. Defaults CLOSED.
- **GATE 1 — OFFER-only-when-confirmed.** `plan_gbp_month(offer_confirmed=...)`: the OFFER slot is
  planned ONLY when a real offer resolves AND the gym is confirmed. Source: `AGENT_GBP_OFFER_CONFIRMED`
  (comma list of base gym keys; **default EMPTY => OFFER OFF for every gym**). A wrong offer to Google
  is unrecoverable, so OFFER stays off per gym until a human confirms. Local updates, events, photo
  drops are unaffected (day-one safe). Interim mechanism; a portal-driven confirmation column is the
  upgrade path.
- **GATE 2 — coach-screens-first-month.** A gym's FIRST GBP month is written in status `coach_review`
  (withheld) instead of `pending`. `AGENT_GBP_COACH_SCREEN` **defaults ON**. The owner `/social` read
  (`portal_social._handle_social_supabase`) filters out `coach_review` rows, and the approve action
  rejects them (409) — the owner cannot see or approve month-1 until a coach releases it. Release:
  `python3 -m agent.gbp_dogfood release <gym>` flips the gym's `coach_review` rows -> `pending`. First
  month = the gym has no prior googlebusiness rows (`GbpStore.any_gbp_rows`).
- **LASSO's 12 dogfood rows stay `pending`** — EXEMPT BY DESIGN (`gbp_dogfood.run` skips GATE 2 for
  base `lasso`; `build_client_month` skips it too). Blake is LASSO's own owner+coach; approving the
  raw month IS the client-experience test.
- **GATE 2 EXTENDED to the FB/IG client month (Blake ruling 2026-08-17).** Coach screens every gym's
  first month on EVERY platform before the owner sees it (the coach SOP, now enforced in software).
  `build_client_month` writes a client gym's FIRST month `coach_review` when
  `AGENT_COACH_SCREEN_FIRST_MONTH` (default ON) and the gym has no owner-visible rows yet
  (`SupabaseCalendarStore.has_owner_visible_rows`). Gyms with a month already in flight are
  GRANDFATHERED (they have owner-visible rows -> not first month -> never re-withheld). Safe default:
  a store lacking the signal is treated as established (no withhold). The coach release is
  gym-wide across all platforms: `python3 -m agent.gbp_dogfood release <gym>` flips every
  `coach_review` row (GBP + FB/IG) -> `pending` in one shot. The owner `/social` read already hides
  `coach_review` (platform-agnostic filter), so the FB/IG withhold needed no extra read change.

### [x] §12 A+ bar — ALL 7 gaps CLOSED (build → audit → fix → re-audit, 2026-08-17)
Every Echo code gap the hand-off reconciliation found is now built, tested, and cleared by
independent audit to ZERO material findings. Two audit rounds; round 1 flagged 2 defects (G3 key
split, G7 double-post/draft-bypass) which were fixed and re-audited clean.

- **[x] G4 — planner pauses on `needs_reconnect`.** `gbp_dogfood._connection_status`; a
  needs_reconnect-only gym plans nothing (connected/none still plan). Audit: CONFIRMED.
- **[x] G2 — `requeue` action + words-changed routing.** `portal_routes` + `patch/requeue` store
  method: failed-only (else 409); words changed → OWNER `pending` + fabrication gate + learn;
  unchanged → `approved`; reject_reason cleared; legacy plane 400. Audit: CONFIRMED.
- **[x] G3 — worker owns `posts_published` + `top_post_id`.** posts_published incremented at publish;
  top_post_id seeded at publish + ranked BY CLICKS during reconcile from real per-post data (never
  fabricated). Row stamped with the connection location at publish so publish/reconcile key on the
  same (gym, location, month). Audit: CONFIRMED (round-1 key-split fixed).
- **[x] G7 — transient retry.** One retry at SEND time (`publish_gbp_row`, transport-only, honors
  draft, a failed send never went live → no double-post); reconcile NEVER re-sends (transient →
  keep polling). Audit: CONFIRMED (round-1 reconcile-resend removed).
- **[x] G5 — 8-10am publish window in the connection timezone (§7.3).** `in_publish_window` (weekday
  8-10am local; missing tz → publish; `AGENT_GBP_PUBLISH_WINDOW` default ON); off-window rows HOLD as
  `approved`, never dropped. Audit: CONFIRMED.
- **[x] G6 — lapsed-OFFER→pending + reconnect re-slot.** `offer_window_lapsed` reverts a dead OFFER to
  `pending` + staff alert (status write only, never publishes); held rows re-slot at the next
  in-window tick. Audit: CONFIRMED.
- **[x] G1 — `edit` accepts + persists GBP structured fields.** `gbp` object (camelCase or column
  names) validated (topic/cta) BEFORE any write, persisted via `patch_gbp_fields`, reverts to
  `pending`; requeue + gbp flow through the intake_web POST route; OFFER-omits-CTA still enforced at
  build. Audit: CONFIRMED.

DEVIATION RATIFIED (Blake 2026-08-17): images host to R2 (`media_host`), not Supabase storage —
functionally fetchable (Zernio proxies a public URL); spec + hand-off wording updated. P2 reviews
(§8) remain deferred to Phase 6. Full suite green (2800 passed).

### [!] Legacy `agent/gbp_publisher.py` — untouched and dead (verified)
The new rail uses `account='googlebusiness'` rows + the GBP worker lane. `approvals.py` still routes
its legacy `Platform.GOOGLE_BUSINESS` path to `gbp_publisher`; that path is not used by the new
rail and the legacy file was not modified.

---

## Portal organic DEEP DIVE: publish payload, builder collapse, edit gate, finality (2026-08-13)

Four independent audit agents swept every portal-organic surface (read model, actions,
connections, upload->publish pipeline). Root causes found live and fixed:

### [x] CRITICAL: Zernio create_post payload was WRONG — nothing client-side ever published
Every client publish 400'd ("Missing required field: platforms") and retried every
minute forever. Rebuilt to the OpenAPI-verified shape: content + platforms[] +
mediaItems (typed image/video/gif) + publishNow when immediate (omitting scheduledFor
used to silently create a DRAFT). Stories now publish as STORIES
(platformSpecificData.contentType) instead of duplicate feed posts. x-request-id
idempotency per call; Zernio's 24h duplicate 409 maps to already-posted (no retry loop).
Past-slot approvals publish NOW instead of handing Zernio a past timestamp.

### [x] CRITICAL: month builder collapse (Dale's empty Fri/Sat) + locked-slot awareness
A served-ledger polluted by daily plan-then-delete passes made pick_image return the
SAME photo every day -> a 34-photo gym collapsed to ONE feed day. Fixed by threading an
exclude-keys set through the pick. Builder now also reads existing HUMAN-OWNED rows
first: locked feed days are skipped outright (no competing drafts, no orphan pairs),
locked photos are never re-picked (no double-posts), and the cap accounts for them.
VIDEOS are now placeable (a gym uploading videos gets them posted). originals/ (raw
archives of converted videos) no longer sync -> no double-count/spurious rebuilds.

### [x] CRITICAL: legacy edit route had NO fabrication gate + finality guards
/portal/<token>/edit wrote captions with no is_gate_clean check (a fabricated stat
could enter an autonomous gym's caption and publish). Gate added. Both route families
now refuse ALL actions on published (409) and mid-claim 'publishing' (409) rows —
closing the portal-action double-post race. Deny is idempotent (no double budget burn).
Revoked-token checks added to legacy actions + page-select + calendar/library/report.

### [x] HIGH: stranded approvals + silent retry loops + autonomy split-brain
- due_rows catch-up window (7d, client lane): approving yesterday's post publishes it.
- Repeat publish failures alert a human once at 5 consecutive fails (was silent print).
- Autonomy toggle now DUAL-WRITES echo_gym_settings (the plane the publisher reads);
  local-kv-only writes were invisible across services. Response carries shared_persisted.
- facebook-page-select validates ownership against the gym's own connected FB account.
- Disconnect double-click 404 = already-disconnected success, not a 502.

### [x] MEDIUM: /social read surface honest + complete
Posts now carry platform (IG/FB badge), published_at, late_post_id; scheduled_at is
synthesized from the deterministic slot when unstamped (time visible pre-approval);
low_creative computed from RAW image_url before the fallback substitutes infographics;
/calendar map_row returns the real scheduled_at; metrics ?days clamped to 365.

### [x] Wave 2 (re-audit findings, SHA 9f1f762): locked-day siblings survive the
rebuild delete (preserve_dates); denied/killed photos stay re-draftable; scheduledFor
normalized to UTC; autonomy actor recorded. Independent re-audit verdict on wave 1:
all 10 fixes FIXED, no gate weakened anywhere.

### [x] LIVE-VERIFIED on ENG (first successful client publish ever, 2026-08-13 13:11 ET)
- IG story published immediately (past slot -> publish-now path).
- IG + FB feeds handed to Zernio with post ids, scheduled 2026-08-13T22:30Z (6:30 PM ET)
  — probed Zernio directly: status 'scheduled', correct UTC time.
- Calendar rebuilt: 30 feed days, 30 DISTINCT photos, 7 videos placed, locked day
  skipped, published photo never re-picked.
- /social payload live-probed: platform, published_at, synthesized scheduled_at all
  present; low_creative honest.

### [x] DECISIONS — Blake ruled 2026-08-13 ("do all your recommendations")
1. account_state anti-flap rule KEPT: a present Zernio account row = connected (the
   94f29e7 ruling stands; the repeat-failure alert lane covers dead connections).
   Pinned by test_account_state_present_row_no_positive_signal_is_connected.
2. VIDEOS in client calendars KEPT ON (feed videos publish as Reels). Pinned by
   test_videos_are_placed.
3. Unconfirmed kill RETIRED: the legacy /portal/<token>/kill route now requires
   confirm=true (400 otherwise), matching Part-B — no route family offers a
   one-click permanent kill anymore. Portal spec updated; the UI must send
   {confirm: true} on kill. Pinned by tests/test_portal_actions_finality.py.

---

## Welcome post: no owner name + no story (Project Evolve, 2026-08-14)

### [x] Owner name missing on portal welcomes -> pull from onboarding_intake
scan_portal_and_enqueue hard-coded owner="" for portal gyms; the gyms table has no
owner column (owner_client_id null on a portal onboard). The owner the client typed
lives in onboarding_intake.owner_name. portal_gyms.owner_names() now reads it and
list_recent_portal_gyms enriches each gym; the card + queue get the title-cased owner.
Verified: Project Evolve owner = "Jake Raleigh". Every future portal welcome gets it.

### [x] Story not posted -> re-posted live; publish path verified working
The story asset was a genuine 1080x1920 9:16 (hosted, passed every guard) but the
original day's run silently skipped publishing it. Re-posted live (media_id
1808427114247699); mode=published. Both build lanes + the publish path work, so it was
a one-off flow miss, not a broken path.

### [ ] Logo reads small -> design constraint, flagged for Blake
_fit_into correctly fills the logo_zone, but a WIDE logo ("[PROJECT EVOLVE] PERSONAL
TRAINING") is width-limited so it looks short/small in a squareish zone. Not a clear
bug; enlarging risks layout overlap. DECISION for Blake: widen the logo zone / allow
wide logos to scale bigger, or leave as-is.

NOTE: the already-LIVE Project Evolve feed + the re-posted story used the pre-fix
assets (no owner, small logo). Instagram forbids editing/deleting a live post, so
those specific posts can't be corrected; the fix applies to all future welcomes.

---

## GritX stuck at 1 day despite 179 uploads — batch-insert key mismatch (2026-08-14)

Ryan (GritX) uploaded ~179 media (171 photos + 8 videos) but Echo only ever built 1 day.

### [x] Root cause: PostgREST PGRST102 "All object keys must match"
insert_rows sent a HETEROGENEOUS batch — video rows carry thumbnail_url, photo rows
don't — and PostgREST 400s a mixed-key batch, failing the ENTIRE month insert (0 rows
written). The build generated all days fine; only the WRITE failed, silently, every
rebuild. GritX was the first gym to rebuild after thumbnail_url joined the row shape.
Affects ANY gym with both photo and video posts.

### [x] Fix: insert_rows normalizes every batch to the UNION of keys (missing -> None).
Rebuilt GritX live: **90 rows / 30 days**, 24 video rows placed, 52 distinct media, 0
suspect captions (all A+). The 3 published Aug-13 posts were PRESERVED (rebuild composed
with them, not over them). 2 new store tests. Suite 2630 green.

---

## Echo LEARNS from portal caption edits (Dale round 7, 2026-08-14)

Dale edited tomorrow's captions with detailed reasons (a youth-fitness video had an
adult-parent caption) and wants Echo to learn so future captions match.

### [x] Portal edits now feed the learning loop (tenant_brain edit_diff)
The loop existed (edit_diff -> edit_examples -> drafter._brain_guidance -> SB7 prompt)
but the portal edit path never recorded to it. Both edit routes now call
portal_social._learn_from_edit(before, after), keyed to the GENERATION account
({base}_ig — the exact key _brain_guidance reads, NOT the bare base). AGENT_TENANT_BRAIN
_ENABLED armed. Best effort, never breaks an edit.
- Recorded Dale's real youth edit into eng_ig's brain; verified it reaches the prompt.
- VERIFIED end-to-end: a fresh ENG youth caption now reads "Your kid's confidence is
  built in the gym, not on a screen. We coach young athletes to move well, build real
  strength..." (youth-development framing) instead of the old adult-parent angle.
- Added "Step Up for Students Provider" as an approved ENG source so youth captions can
  truthfully cite the credential (Dale's guidance).

### [x] Edit persistence confirmed working; portal UI issues documented
Dale's edits DID persist (tomorrow's posts carry his wording, approved). "Not sticking"
= the portal UI not re-fetching after save; "kicked out twice" = a portal session issue.
Both documented for the Vercel team in PORTAL_SPEC §4 (the edit response already returns
{caption, status} for an in-place update).

---

## A+ quality gate — every post checked, no sub-par caption reaches a calendar (2026-08-13)

Blake standing bar: "whenever you make a post (ig, story, reel, anything) to calendar
for a gym it needs to be A+ with captions and everything checked."

### [x] agent/post_quality.py — the A+ gate, enforced at BUILD time
A client draft is written ONLY when its caption is a REAL caption (>=12 content words,
>=40 chars, counts across HOOK/BODY/CTA not just the first paragraph), carries NO dash
(copy law), NO banned word, NO LLM scaffolding, and has real media. A sub-par draft is
DROPPED (walk neighbour days for a better source, else hold) — never published. Active
whenever SB7 (the real-caption engine) is on; SB7-off keeps the documented baseline.

### [x] LLM scaffold leak fixed (GritX story "# Caption Body:")
SB7 occasionally prepended a markdown header / 'Caption:' label. drafter._strip_llm_
scaffold removes it before use; post_quality rejects any caption still starting with it.
GritX's live story reformatted with a clean real caption ("You're juggling too much.
Energy is shot.") + the 9:16 branded card.

### [x] Verified: 0 captions fail A+ across ALL gyms (ENG + GritX, every month/status).
Gym-agnostic (subagent-audited earlier). Every future gym inherits the gate + SB7 + the
story/poster/reel lanes automatically. Suite 2627 green.

---

## Real StoryBrand client captions — no more raw intake word (Dale round 6, 2026-08-13)

### [x] "why is the caption only HYROX?!" -> SB7 engine now writes client captions
The client month builder dumped the raw one-line approved SOURCE as the caption
(compose_caption = source.text + CTA), so a thin intake entry ("HYROX") was the whole
caption, while the full SB7 StoryBrand generator (Claude, grounded ONLY in the gym's
voice doc, figure-fabrication-gated) sat unused on the client path.
- client_content.make_caption routes every client caption through SB7 when
  AGENT_SB7_ENABLED (grounds on the day's source topic + the gym's OWN durable voice
  doc), clean fallback to source+CTA on flag-off/LLM-error/no-improvement echo. Wired
  into both the image and thin-library paths. GYM-AGNOSTIC.
- Independent subagent audit: ALL 12 session fixes verified GYM-AGNOSTIC, no cross-gym
  voice/CTA leak, SB7 output still clears banned-word + figure gates. 2621 tests collect.
- Backfilled live: ENG's 87 rows -> 26 days got real captions (e.g. "You walked in
  nervous... Coach Lester met you with patience. That's the difference between a gym and
  a family."), 20 story cards re-rendered; 2 skipped by the fabrication gate (tried to
  add unapproved numbers). GritX all-published (nothing to backfill); other gyms get it
  automatically when their calendar builds.
- Today's live ENG story replaced with the formatted card + real caption. NOTE:
  Instagram does not support Zernio unpublish (FB/YT/LI/X only), so the old raw IG story
  can't be deleted — it auto-expires in 24h while the new formatted one posts fresh.

---

## Story look + video previews + publish-now (Dale rounds 4-5, 2026-08-13)

### [x] "I don't see the post to their main feed" (CRITICAL, status honesty)
A manually-approved client post was handed to Zernio with a FUTURE slot (6:30pm) ->
Zernio held it 'scheduled' while Echo marked the row 'published' -> green check, empty
feed. Fix: manual approvals (approved_only) publish NOW; autonomous gyms keep the slot
drip. Remediated live: deleted the 3 scheduled posts, republished now. Dale confirmed
Thursday 13th is live.

### [x] "make it story size + add a caption" -> agent/story_image.py (AGENT_STORY_FORMAT)
Raw photo stories showed centered on black bars, no text. New formatter fills the 9:16
frame (full photo on a blurred cover background) with the day's approved caption burned
into a bottom card + gym-name brand line (stories publish empty-body, so text must be on
the image). Pure Pillow, all local fonts, ENHANCE-only. Flag ARMED. 22 ENG stories
reformatted live; verified render looks clean.

### [x] "photo preview not present for 14th and future" -> video poster frames
A video URL in an <img> is blank. action_reel.get_or_make_poster extracts a hosted
poster; builder sets content_calendar.thumbnail_url; /social serves the poster as the
display image for video rows (display-only; video still publishes). 21 ENG video rows
backfilled live; portal confirmed serving posters. New column thumbnail_url.

---

## Ghost stories + video previews (Dale round 3, 2026-08-13)

### [x] IG story said "published" but never appeared (CRITICAL, my own 409 mapping)
A story shares its paired feed's photo AND caption on the same IG account -> byte
identical -> Zernio's 24h content-hash dedup 409'd -> Echo's 409-as-published mapping
marked it done while Zernio created NOTHING (late_post_id=''). Both ENG and GritX
stories were ghosts (LASSO's Meta-direct stories unaffected). Fix: stories publish
with an EMPTY body (platforms don't display story captions anyway) so the pair can
never collide; 409s now carry Zernio's existingPostId into media_id. Ghost rows
flipped back to approved for a real re-publish.

### [x] "No photo preview" on video posts
A video URL in an <img> tag renders blank. /social posts now carry media_kind
(video|image); spec instructs the portal to render <video muted playsinline> for
videos. Videos are ~21 of ENG's 57 cards — exactly the blanks Dale saw.

---

## Action-cut Reels: client videos edited into engaging Reels (2026-08-13, SHA 4a3913f)

Blake: "yes but i want not for podcast but more video action to be engaging." New lane,
MOTION-based (not the transcript/podcast editor): AGENT_CLIENT_VIDEO_EDIT (default OFF)
+ AGENT_REEL_TARGET_SEC (default 22s).

### [x] agent/action_reel.py — pure ffmpeg, zero AI spend, deterministic
- Scene-score motion profile on a downscaled stream -> highest-action 2.0-3.5s windows
  fast-cut chronologically to ~22s; short clips kept whole; static camera falls back to
  even spacing. 9:16 cover crop 1080x1920@30, h264/aac faststart, source audio kept.
- 3s TEXT HOOK from the day's OWN approved caption (first sentence, word-truncated,
  scrub_onscreen: the no-dash on-screen law). Never invented copy.
- Content-hash cache in <library>/reels/ (invisible to media count + pick pool).
- Editing only ENHANCES: flag off / image / edit fail / hosting fail all post the raw
  video. Approval gate unchanged. Verified with a REAL 60s encode -> 8 cuts, 22.1s,
  1080x1920, audio + hook intact. 13 offline tests.

### [x] Builder wiring: video feed drafts swap creative for the HOSTED reel; the paired
story inherits it (_maybe_edit_video in client_month_run).

---

## Client approvals now HOLD — rebuild never destroys/rewords an approved post (2026-08-13, SHA 5efd78d)

Dale (CrossFit ENG) reported three symptoms of ONE root cause: after approving a post it
(1) reverted to "waiting on you", (2) came back with different caption wording, and
(3) approved posts disappeared. The nightly delete-then-insert calendar rebuild (client
month / real month / demo->real mirror) wiped the whole gym-month — approved rows
included — and re-inserted fresh `pending` rows with newly generated captions.

### [x] Rebuild preserves human-owned rows (centralized at the store)
- `portal_calendar_store.delete_month(preserve_human=True)` deletes ONLY wipeable rows
  (status null/pending/draft/queued) via a PostgREST `or=` status guard.
- `locked_slots()` + `preserve_and_prune()`: rebuild also SKIPS inserting a row that
  collides with an already-approved `(post_date,account,format)` slot (no duplicate).
- All three rebuild lanes route through `preserve_and_prune` before delete+insert.
- Independent audit (code-analyzer): invariant UPHELD across every write path, no
  CRITICAL/MAJOR. Guard test pins `_WIPEABLE_STATUSES`. Suite 2550 passed. DEPLOYED.
- Live-verified ENG publish resolver: Zernio profile + IG + FB + FB page all resolve, so
  an approved ENG post publishes to his own IG/FB at slot time.

### [ ] Portal side (Vercel) — spec updated, needs Blake/portal team
- `docs/PORTAL_SPEC_disconnect_and_scheduled_time.md` §3: portal approve/deny must PATCH
  status only (never caption, never delete-recreate); no client-side calendar reseed.
  DECISION NEEDED from Blake: assign the portal build to whoever owns the Vercel repo.

---

## Portal go-live: intake upload-URL fix + encrypted portal tokens written (2026-08-06, SHA 587177b)

Executed the portal go-live handoff so the client Social Media page turns on.

### [x] Step 1 — intake upload-URL domain fixed (SHA 587177b, merged to main)
Root cause was NOT code: `AGENT_UPLOAD_BASE_URL` on the `echo-intake-web` Railway
service held the literal setup placeholder `<paste the Step 7 domain here>`, so
portal intake returned `upload_url = "<paste ...>/u/<token>"`. Corrected the env
var to `https://echo-intake-web-production.up.railway.app` (redeploy confirmed
live) AND hardened the code: new `intake_web._upload_base_url()` treats a blank,
placeholder (`<`/`paste`), or non-http value as unset and falls back to the
canonical service origin, so a forgotten env var can never leak a broken link
again. Routed link_for / handle_portal_intake / handle_portal_gym_status through
it. Live in-container submit now returns
`upload_url = https://echo-intake-web-production.up.railway.app/u/<token>`, no
placeholder. Full suite green (1984). +3 tests.

### [x] Step 4 — encrypted portal tokens written (portal Supabase ooqcvmcjspeltuuhcvlh)
`echo_intake_tokens` had 2 rows with `intake_token_encrypted` NULL. On the
echo-intake-web container, Fernet-encrypted each gym's raw token with the shared
`AGENT_INTAKE_ENC_KEY` (round-trip verified True in-container) and wrote the
ciphertext to Supabase: gym_id 31a41f5f… (lasso) and 2459ca79… (districth), both
now non-null (len 140, `gAAAAAB…`). VERIFIED end-to-end: decrypt stored blob ->
raw token -> `GET {base}/portal/<token>/social-status` returns HTTP 200 with real
JSON (ig/fb connected:false, nothing fabricated) for BOTH gyms. The portal Social
Media page now resolves.

### [x] Step 5 — LASSO Zernio connect links generated (dogfood; OAuth needs Blake)
Live `GET {base}/portal/<lasso_token>/social-connect?platform=…` returns HTTP 200
with real Meta OAuth URLs via Zernio for both instagram and facebook. Links +
by-hand OAuth steps handed to Blake. After Blake connects, "plan a month" for the
connected account lights up the calendar + Approve/Deny/Kill.

### [ ] Step 2 — report endpoint: BLOCKED (branch does not exist)
`feat/portal-report-endpoint @89e70ad` was not pushed — absent locally, on origin,
and the SHA is not a valid object. No `/portal/<token>/report` route exists in
main. Cannot review/merge a nonexistent branch. Not built fresh here: it would
call `_pr`/`day30.assemble` and touch the do_GET dispatcher that Part B is
actively editing in portal_routes.py — building now risks clobbering in-flight
work. Awaiting the real branch (or Blake's OK to build it fresh after Part B lands).

---

## Summit sprint: agenda + panel + story cards + backward-anchored calendar (2026-08-05, branch summit-finish)

Finishes the summit sprint asset set and wires the sprint calendar. Everything stays
dormant behind AGENT_SUMMIT_CAMPAIGN_ENABLED (default OFF); every draft is still HELD.

### [~] Agenda cards (Day 1 / Day 2) — summit_render.render_agenda
Two navy agenda cards. Session titles are VERBATIM from 02_verified_stats.md SUMMIT
SPEAKERS receipts, attributed to each speaker. Day 1 = NOV 7, Day 2 = NOV 8 (both
verified). FABRICATION GUARD: the verified source publishes session titles but NO
session times and NO per-day running order, so the cards carry NO times and the day
split is a THEMATIC grouping (growth engine / business engine), not a scheduling
claim. Whether specific sessions lock to specific days is a BLOCK item for Blake.

### [~] Future of Gym Growth panel card — summit_render.render_panel
Panelists Streamfit, HireVP, Tommy Allen (Blake ruling). PushPress does NOT appear
anywhere (asserted in tests). Ink headline, single red accent on the first panelist
tile, verified fact strip.

### [~] Paired 1080x1920 story cards — summit_render.render_card_story / render_all_stories
Native-tall (not a crop) story version of every feed concept, both treatments (20
stories). Photo A cards keep the athleisure crowd behind a tall legibility scrim;
data B cards carry the concept's data element (bars / checklist / steps / grid /
contrast / tiles / bignums). Top+bottom safe bands clear IG chrome. Bold readable
type, one red accent, verified facts only, no dashes. Lets the sprint run a story
alongside a feed post (stories sit on top of the feed cadence).

### [~] Backward-anchored sprint calendar — summit_queue.sprint_calendar / create_sprint_drafts
Cycles: Aug 21-30, Sep 7-16, Sep 24-Oct 3, continuous Oct 11-Nov 6 (57 posting days);
Nov 7 + 8 DARK (event days). Up to 3 feed posts/day (Blake); welcome / new-client
does NOT count toward the cadence (own queue, sits on top); stories sit on top too.
Slot times 07:30 / 12:30 / 18:30 in POSTING_TIMEZONE (DST correct). No card lands
twice in a row. Feed captions come from the Blake-approved SUMMIT_CONCEPTS + verified
agenda/panel captions (NOT the legacy SUMMIT_POSTS block, which still carries
pre-ruling scarcity/pricing copy and is untouched). create_sprint_drafts is a no-op
with the flag OFF; ON, every draft is PENDING. CLI: summit-queue --sprint.
Tests: tests/test_summit_sprint.py (+ existing suite green, 1946 before this branch).

---

## Welcome story format + auto welcome posts + chat-publish (2026-08-04, SHA 102f11c)

Three builds landed on main today, each behind an OFF-by-default flag, built to the
big-build protocol (independent audit -> fix wave -> re-audit to zero CRITICAL/MAJOR).
Suite 1782 -> 1928 green (+146 tests). Fresh audit agents (not the builders) verified
each; the re-audit returned zero CRITICAL/MAJOR.

### [x] Welcome templates now ship feed + story (9:16) — SHA 1f2a364
`make_welcome(template_id, gym, owner, logo, format="feed"|"story")`. Feed 1080x1080
unchanged (byte-identical); story 1080x1920 with a background REGENERATED native to the
tall frame (not a crop), a vertical stack (eyebrow, WELCOME TO LASSO larger, centered
logo plate in the middle third, gym name, owner, footer), safe band 15-85% clear of the
top/bottom 250px, logo zone 14% of canvas. `slots.json` v2 carries per-format coords.
Fixed: red-region check now preserves aspect on the tall frame; a shared height-clamped
bottom-block fitter keeps a long two-line gym name inside the safe band, and the story
grade guard measures the composed block (not just static geometry).

### [x] Auto welcome posts from new clients — SHA 976ee67 (flag AGENT_WELCOME_POSTS_ENABLED)
`agent/welcome_posts.py` + `agent/website_scan.py` (logo scraper). Brand-new-client is
defined by SUBSCRIPTION (first-ever sub in window, active, not canceled, on a core tier
Launch/Ascend/Apex), NOT customer.created (skips sponsors). Guards: delinquent excluded,
dedupe by gym, kv ledger (never twice, stamped on surface), owner title-cased, CONFIRMED
vs INFERRED (INFERRED held for yes/no before any post). Logo scrape order og:image ->
nav img -> apple-touch-icon -> /logo/i, white/black knockout, <200px+favicon reject,
stored on /data, portal-drop override wins. Generates feed+story rotating the 6 kept
templates, surfaces held to the approval channel. NOTHING publishes. CLI: welcome-backfill.
- Action required by Blake (Railway): STRIPE_API_KEY (restricted read-only; copy from the
  Vercel "portal" project env) + AGENT_WELCOME_POSTS_ENABLED=true to run the 45-day backfill.

### [~] One-per-day welcome DRIP + automatic new-client trigger (flag AGENT_WELCOME_QUEUE_ENABLED, OFF)
`agent/welcome_queue.py`. Turns the backfill roster into a steady drip: the daily runner
scans Stripe each cycle (the new-client TRIGGER; Stripe subscription only, portal enriches),
enqueues every READY welcome (feed+story, hosted to R2, idempotent by gym_key, ledger-stamped),
and serves the OLDEST queued welcome ONE gym/day cross-posted to lasso_ig+lasso_fb with its
story on lasso_ig. Sits behind the dated book queue so book-launch dates keep their slot.
Served drafts are PENDING: they auto-publish only when AGENT_AUTO_APPROVE_ENABLED is armed
(story also needs AGENT_STORIES_ENABLED; a real Meta write needs AGENT_PUBLISH_ENABLED).
Fixed dash-free caption over gym name + owner (no fabrication). CLI: welcome-queue.
Names/logos/force-include carried by <logo_dir> override files (name_overrides.json,
welcome_force_include.json, overrides/<key>.<ext>); All Kine (returning 2020 client) is
force-included. Catch-up ships via a manifest like the book queue: `welcome-queue
--build-manifest` renders + hosts the 9 ready cards from the Mac (where overrides live) and
writes welcome_queue_manifest.json (committed); AGENT_WELCOME_QUEUE_ON_START seeds Railway's
queue from it on deploy (no override files needed on Railway for the catch-up). Built + full
suite green (1946); flag OFF = byte-for-byte current behavior.
- 9-gym catch-up manifest built + hosted (All Kine, Op Ht, Catalyst, GritX, Mindbodysoul,
  Pierce, Bell House, Sycamore, Bird Dog), oldest-first.
- Action required by Blake (Railway): AGENT_WELCOME_QUEUE_ON_START=true (one-time seed) +
  AGENT_WELCOME_QUEUE_ENABLED=true, then deploy. AUTO_APPROVE/PUBLISH/STORIES already on
  (auto-approve is GLOBAL: all LASSO content auto-publishes, not just welcomes). Ongoing
  new-client auto-pickup needs the override files on /data (catch-up does not).

### [x] Two recurring welcome defects fixed permanently, with regression tests (2026-08-06)
Defect 1 (welcome STORY posted at square size, cutting off the gym name/logo — hit All Kine
and GritX): THREE layers of guard now make a non-9:16 story impossible. (a) RENDER: a new
`wt.is_story_size` / `STORY_SIZE`; `_render` + `make_welcome(format="story")` assert exactly
1080x1920 before returning; `welcome_posts.generate_posts` re-opens the story file and raises
if it is not 9:16 (so a worker re-render is safe). (b) HOST: `welcome_queue._local_story_is_9_16`
gates every story host, so `enqueue` and `build_manifest` never host a square/None/off-size as
a story_url (feed still queues; story_url stays empty + a loud log). (c) PUBLISH backstop
(mirrors 2c21a10): `build_welcome_story_draft` returns None when story_url is missing, and with
an optional cheap dimension probe blocks + fires ONE ops alert on a non-9:16 hosted asset.
Defect 2 (identical captions across clients): `welcome_caption` now selects 1 of 7 on-brand
StoryBrand variants deterministically by a stable hash of the gym name (same gym stable, gyms
differ). All dash-free, no "vendor", gym name + owner the only fills, no fabrication.
Regression tests in tests/test_welcome_story_guards.py (fail if any guard is removed). Full
suite green. Gates unchanged; nothing publishes from this work.

### [x] Chat can publish, scoped by account ownership — SHA 7b563be (flag AGENT_CHAT_PUBLISH_ENABLED)
Flag-gated @app.message handler. LASSO accounts (lasso_ig/lasso_fb/blake_personal):
explicit publish verb -> direct publish + permalink. Client accounts: draft+schedule
only, never chat-published. Generation words never publish; ambiguous -> one question;
undo within 5 min (LASSO only, best-effort; FB deletable, IG -> manual). Only Blake's
Slack id; fabrication + dash/vendor gate before every chat publish; draft-only returns an
honest "would_publish". Requires AGENT_PUBLISH_ENABLED for a real Meta write.
- Action required by Blake (Railway): AGENT_CHAT_PUBLISH_ENABLED=true when ready.

### chore
Removed accidentally-tracked agent-tooling/worktree/cache files (SHA 102f11c) and
gitignored them; kept summit_render.py/summit_rebuild.py/summit_logo.png (real imports of
welcome_templates + podcast_quote_card that had been untracked, so Railway needs them).

---

## Stage 4 Python agent features built (2026-07-22, SHA ed26210)

Three Stage 4 gaps closed and armed (where applicable). Stage 2-3 also fully armed.

### Stage 2-3 arming complete
All flags now on in Railway: AGENT_AUTOTAG_ENABLED, AGENT_BRAIN_PROPOSALS_ENABLED,
AGENT_GRADE_ENABLED, AGENT_MONTHLY_REVIEW_ENABLED, AGENT_GRAPH_API_VERSION=v23.0,
AGENT_GEMINI_DAILY_CAP=100. AGENT_TRUST_LADDER_ENABLED intentionally NOT armed
(deliberate by-hand step per CLAUDE.md).

### [x] GHL inbound webhook HTTP route
`POST /ghl/inbound` wired into intake_web.py (same 404-dark / 403-bad-sig / 200-ok
pattern as the WhatsApp route). Logic in ghl_intake.py was complete; it was missing
the HTTP surface. Armed when AGENT_GHL_INTAKE_ENABLED=true is set in Railway.

### [x] Consent audit log
New `consent_log` SQLite table (asset_path, action, member_ref, granted_by, note,
recorded_at) in the shared echo.db schema. `dam.set_consent()` writes both the sidecar
(existing enforcement path) AND an audit row. `dam.consent_log_entries()` reads the
history. The fail-safe consent guard in dam.py is unchanged.

### [x] Gemini Vision content moderation
`_moderate_default()` in intake_ingest.py is no longer a stub. Behind
AGENT_CONTENT_MODERATION_ENABLED (default OFF): calls Gemini Vision with a
nudity/violence/explicit_text prompt; flagged images route to intake/<client>/review/
with a Slack notice (the interface was already wired). Fails open on any API error so
uploads never stall. Video is skipped (image-only pass). Armed by setting
AGENT_CONTENT_MODERATION_ENABLED=true in Railway.

### Stage 4 items NOT in this Python build (by design)
- A2P/10DLC registration: carrier compliance, done by hand in Twilio/GHL
- Supabase/Next.js portal: the BUILD_SPEC full vision uses Next.js; this repo is the
  Railway Python worker only

10 new tests added. Suite: 1556 passing, 3 pre-existing failures (unchanged).

---

## Auto-approve + story crosspost confirmed live (2026-07-22)

`AGENT_AUTO_APPROVE_ENABLED=true` — posts now publish at schedule time without a Slack
approval card. A lightweight "Auto-published" notice fires to Slack instead. Gate
documented in CLAUDE.md; non-negotiable gates otherwise unchanged.

`AGENT_STORY_CROSSPOST_ENABLED=true` — every approved reel/image also posts to IG Story
+ FB Story automatically after the main publish. Confirmed live today: IG story
`17879609124495208`, FB story `2304453533658075`.

Caption spacing rule fixed in `brand_voice/lasso_house_style.md`: blank lines on
BEAT SHIFTS only, not every sentence. Related sentences stay in the same paragraph.
Verified against Blake's example (agencies/cockpit caption). Memory saved.

`run-daily --force` flag added to bypass same-day idempotency (testing + ops use only).

Grade: **B+** (unchanged). Grade moves to A when first client completes a full
30-day cycle AND Meta App Review is cleared for client-owned pages.

---

## Video text = overlays on live footage (kill full-screen takeovers) (2026-07-21)

Blake locked two approved text treatments; full-screen static text cards BANNED.
- Treatment A (default): word-highlight captions over the full-frame live host.
- Treatment B (concept/hook/CTA): burn_side_panels composites a semi-transparent
  navy gradient panel over ~left 55% (fading to transparent, host stays live +
  in motion on the right), house-style text (Oswald sky-blue eyebrow + red rule
  + Anton headline with ONE red word, left-aligned), fades/slides in and clears.
- Retired the still-card full-frame cutaway + hook/CTA concat cards. Captions are
  suppressed inside any panel window (no collision). All text animates in.
- 3 samples posted to #echoclaude. Suite 1517. SHA a8af0e8.

## Video editor A+ finish (Phase 1-3) (2026-07-20)

Phases layered on the v1 A-build, all behind flags default OFF.
- Phase 1 (AGENT_VIDEO_POLISH + AGENT_VIDEO_JUMPCUTS, ffmpeg, $0):
  caption pop-motion (active word 118->100% + fade), b-roll cross-dissolves,
  host color grade (eq), jump-cut pacing (removes inter-word dead air with
  time-map remapping of overlay offsets + caption transcript so A/V stays in
  sync), hook + end CTA title cards (Anton, grounded hook text, _concat_av
  normalizes streams). Host punch-in DEFERRED (zoompan desync).
- Phase 2: AGENT_VIDEO_BROLL_RESOLUTION (1080p) + AGENT_VIDEO_STILL_RESOLUTION
  (2k, same cost) + house-style card prompt.
- Phase 3: AGENT_VIDEO_HERO_MODEL (e.g. veo3_1) for the hero beat.
- 3 sample reels re-rendered at A+ (Veo 3.1 hero on Reel A, 2k still cards,
  full Phase 1 finish) and sent to #echoclaude held for approval. Suite 1511.
- Deep-dive doc: editing/Higgsfield/Gemini levers with preflighted costs
  (image 2cr, kling turbo 1080p 10cr, Veo 3.1 22cr).

## Video editor v1 A-build (Minimal Broadcast + Word Highlight + routing) (2026-07-20)

v1 layered on the video editor. Independent re-audit (2 fresh agents): parity
13/13 BUILT, gates 8/8 PASS, zero CRITICAL/MAJOR, all prior gaps closed. SHA d72cf8f.

- Overlay ROUTING per beat: motion/scene -> Higgsfield video (motion cap);
  stat/number/quote/framework -> Nano Banana still card via the SAME
  creative_studio Gemini pipeline (stills cap). AGENT_VIDEO_STILLS_ENABLED (OFF).
- Bottom treatment "Minimal Broadcast": real LASSO wordmark (pulled from
  lassoframework.com, cropped to LASSO, transparent) bottom-left sized by width,
  @GYMMARKETINGMADESIMPLE bottom-right Oswald tracked caps, navy gradient scrim,
  no bar/line. Assets in agent/assets/brand + fonts (persistent).
- Captions "Word Highlight": Anton ALL CAPS word-by-word, ONE red active word
  (255,42,42), rest white, heavy outline+shadow, no ghost, lower third, face
  avoidance via opencv (graceful fallback). Fonts bundled.
- Fabrication gate extended to NUMBERS (invented stat cannot reach a still card).
  Clip in/out snap to whole-word boundaries (no mid-word cuts) with degenerate
  guard. Transcription source reported. Silent drops/clamps now logged.
- Separate per-episode budgets (motion + stills), stop+log, never overspend.
- 3 sample reels (4/3/4 overlays each, motion + still) rendered + sent to
  #echoclaude held for approval. Suite 1508 green.

## Video editor shipped (Option A: Echo directs, Higgsfield renders) (2026-07-20)

Full podcast-to-clips video editor in `agent/video_editor.py`, on top of the clipper.
Pipeline: transcribe (Deepgram) -> select moments -> plan b-roll manifest ->
render AI overlays via Higgsfield (Claude-in-the-loop) -> assemble 9:16 AND 1:1,
captioned AND caption-free ad -> held Slack review card. Nothing publishes.

- Flags (all default OFF): AGENT_VIDEO_EDITOR_ENABLED, AGENT_VIDEO_BROLL_ENABLED,
  AGENT_VIDEO_RENDER. Cap AGENT_VIDEO_BROLL_CAP (default 6, per EPISODE), kind
  video|image, cost/overlay, dirs, aspects.
- B-roll planner: restrained beats (cap + 8s gap + 4s min offset), house-style
  Higgsfield prompt per beat, fabrication-gated against the clip transcript.
- Overlay renderer: pluggable interface + content-hash cache (re-runs never
  re-pay) + RenderBudget episode-level cost guard (stops + logs, never silent).
- Captions moved to lower-middle (second/third) via height fraction; karaoke
  3-word groups; scrub_onscreen strips dashes + "vendor" from all burned text.
- Brand frame redesigned: thin navy #121E3C bar, red #FF0000 accent, LASSO left
  + handle right. Palette aligned to locked V3 house style.
- Higgsfield reachable ONLY via interactive claude.ai MCP (never Railway cron):
  render arm is Claude-in-the-loop by design; headless plans + projects cost only.
- Cost preflighted: image overlay 2 cr, video overlay 7.5 cr. Default cap 6 ->
  ~45 cr/episode (video) or ~12 (image).
- Slack inline video FIXED: upload_clip used PUT (Slack 302s it) -> POST. Clips
  now play inline in #echoclaude (files:write scope was present all along).
- Independent audit (2 fresh agents) ran per BIG BUILD protocol: found per-clip
  (not per-episode) cap + on-screen dash/vendor gaps; both fixed + re-tested.
- 20 new tests. Suite 1494 green. SHA b61246f.

---

## Admin tracker route + image grade check shipped (2026-07-20)

### Admin tracker: /admin/tracker/<token>[/handoff]
Read-only admin view of the build tracker and handoff docs. Served by the
connect_web.py listener (port 8090). Token set by hand via AGENT_TRACKER_TOKEN
(never logged, fingerprint only). Route matches [A-Za-z0-9_-]{8,}.

Files served:
- /admin/tracker/<token>         -> echo_build_tracker.html (live build dashboard)
- /admin/tracker/<token>/handoff -> ECHO_HANDOFF.html (static) OR /data/handoff_live.html (live, if generated)

### Image grade check on generated output (AGENT_IMAGE_GRADE_ENABLED)
Vision check on the actual generated PNG, not just the prompt. After each Gemini
image generation, a second Gemini vision call (OCR_MODEL) checks Q1 (left-aligned),
Q2 (scale contrast), and Q5 (thumbnail legible) against the actual output pixels.
Fails trigger up to 2 more retries (3 total). Both gates (style_gate + image_grade)
run in a unified retry loop. Card withheld after 3 failed attempts + one ops alert.
Flag: AGENT_IMAGE_GRADE_ENABLED (OFF by default). 57 tests added.

Grade: B+ (unchanged). New flag does not move the grade.

---

## post-captions CLI + Section 9 caption standard wired (2026-07-17, SHA d858d5a + this commit)

`python3 -m agent post-captions` writes 6 hand-crafted feed drafts to the DB
(INSERT OR REPLACE, idempotent) and posts Slack approval cards to #echoclaude.
Section 9 of lasso_house_style.md is the live caption standard for ALL future cards.

Deploy: Railway auto-deploys from main. After it picks up, run on the Railway console:
```
python3 -m agent post-captions
```
No flags needed. Idempotent: safe to run twice.

Grade: B+. Does NOT move to A.
Gate to A: one real gym client, one full month on approval.
Open items:
- Blake: re-record Meta App Review screencast and resubmit (2026-07-18)
- Blake: create Railway cron service (runbook at docs/SCHEDULER_CRON.md)
- Blake: confirm client auto-mint onboarding status (merged or still pending)
- Cleanup: ENV.md drift (160 read vs 164 documented, 30 mismatched)

---

## Caption fix: exact Blake captions applied to 3 cards on lasso_ig + lasso_fb (2026-07-17)

6 feed drafts written directly to echo.db (status=pending, draft_type=feed). No code
changed; DB is gitignored. Images untouched (V2 virtual files on R2, not local).

Cards and day assignments:
- `lasso_v2_built_by_gym_owners` — Jul 17 (lasso_ig + lasso_fb)
- `lasso_v2_speed_to_lead_concept` — Jul 22 (lasso_ig + lasso_fb)
- `lasso_v2_follow_up_problem` — Jul 28 (lasso_ig + lasso_fb)

Verified: captions match character-for-character, line break structure preserved
(paragraphs double-newline, CTA pair single-newline), no dashes, no "vendor",
hashtags in separate list field, draft_type=feed, status=pending.

**Deploy note**: echo.db is local. Push to Railway and trigger the listener (or run
`python3 -m agent run-daily`) to send Slack approval cards to #echoclaude.

---

## Generation 404 fix: response_modalities + startup model validation guard (2026-07-17)

### Root cause

`_GeminiImageClient.generate_image()` called `generate_content(model=model, contents=prompt)`
without `config=GenerateContentConfig(response_modalities=["TEXT","IMAGE"])`. Image-specific
Gemini models (gemini-3-pro-image, gemini-3.1-flash-image) require this parameter to route
to the image generation endpoint; without it the API returns 404 NotFound — same class of
break as the gemini-2.5-flash retirement. Model IDs in config were correct; the request
format was wrong.

Evidence: API dashboard shows authentication succeeding (requests reach Google), 404s
started July 16 on both Pro and Flash models identically.

### What shipped

**`agent/creative_studio.py`** — three changes:
1. `_GeminiImageClient.generate_image()`: added `response_modalities=["TEXT","IMAGE"]` to
   `GenerateContentConfig`; added ERROR-level debug logging (model string + error body on
   exception); updated response traversal to try `resp.parts` (modern SDK) before legacy
   `resp.candidates[0].content.parts`; config build is wrapped in try/except ImportError
   so the code runs without the SDK installed (dev/test).
2. `validate_generation_models()` added: startup guard that calls `client.models.list()`,
   checks NANO_MODEL + NANO_MODEL_FLASH against the live list, fires ONE ops_alert naming
   bad model strings and listing available image-capable models if either 404s.
3. Section reference in `_route_model` docstring updated (section 6 → section 7).

**`agent/listener.py`** — `run_listener()` calls `creative_studio.validate_generation_models()`
at boot, same pattern as the Opus project-ID startup guard.

**`tests/test_creative_studio.py`** — 4 new tests for `validate_generation_models`
(silent OK, alert on bad ID, skips without key, skips when flag off). `_FakeModels` updated
to accept `**kwargs` in `generate_content`. Suite: 1399 passed, 5 skipped.

### Verified current model IDs (per Google Gemini API docs, 2026-07-17)

| AGENT_NANO_MODEL (Pro) | `gemini-3-pro-image` |
| AGENT_NANO_MODEL_FLASH | `gemini-3.1-flash-image` |
| Flash-Lite (not used)  | `gemini-3.1-flash-lite-image` |

These IDs were confirmed live in the Gemini API documentation. They ARE correct.
The 404 was entirely caused by the missing `response_modalities` config.

### Action required by Blake (Railway)

No model ID changes needed in Railway env. The fix ships in this commit.
On next deploy, startup guard will log `[creative-studio] model validation OK`
and generation will succeed. To verify: run `regen-library --only built_by_gym_owners`
on the container and confirm a real URL is returned.

---

## House-style archetype tuned: editorial-for-social + Q6 grade gate (2026-07-17)

### What shipped

**`brand_voice/lasso_house_style.md`** — section numbering fixed (duplicate section 4
resolved), section 5 now "Layout Archetypes." Archetype 1 (EDITORIAL) updated:
- Visual anchor now REQUIRED in every editorial concept spec (color block, duotone,
  or oversized headline scale)
- NO VACANT THIRDS rule added
- Reference changed to "magazine COVER or Nike/Alo campaign card, never book interior"
- Eyebrow explicitly RED in the doc (matching the creative_studio ARCHETYPES entry)
Section 9 renamed "Six-Question Grade Gate" with Q6 added.

**`agent/grade_gate.py`** — added `_q6_feed_stopping_heuristic()` and Q6 to `grade_card()`.
- Q6 is programmatic: passes when prompt names an illustrated element OR a visual anchor
  (color block, full-width, duotone, magazine cover). All non-editorial cards auto-pass
  (Block D always includes "ILLUSTRATED ELEMENT"). Editorial cards pass only when the
  concept spec explicitly declares a visual anchor.
- `PASS_THRESHOLD` raised from 4 to 5 (≤1 hard False of 6 questions allowed).

**Cross-reference updates** — section numbers updated in `creative_studio.py` and
`config.py` to match the new numbering (section 7 Model Routing, section 8 Scaffold,
section 9 Grade Gate).

**`tests/test_grade_gate.py`** — 10 new tests for Q6 heuristic and grade_card integration.
Suite: 1395 passed, 5 skipped (was 1385).

### Remaining action required by Blake (Railway)

Run `regen-library --set all` on Railway to regenerate all 38+ cards under the updated
archetype (editorial-for-social) and the updated grade gate (Q6 enforced):

```
/opt/venv/bin/python -m agent regen-library --set all
```

Ensure `AGENT_STYLE_GATE_ENABLED=true` is set in Railway env (confirmed set 2026-07-17).

---

## built_by_gym_owners editorial archetype + library gap partial fix (2026-07-17)

### What shipped

**`agent/creative_studio.py`** — added `"editorial"` to `ARCHETYPES` dict.
Type-led card: no illustration, eyebrow + oversized headline + deck, negative
space as designed element, optional single hairline or dumbbell motif, red once
or not at all. Does NOT appear in `ARCHETYPE_ORDER` (regen-only archetype).

**`agent/regen_library.py`** — `built_by_gym_owners` concept rebuilt:
- Archetype changed: `flow` → `editorial`
- Concept lines now specify eyebrow "OWNER'S ADVANTAGE" + deck line for rendering
- Clip-art two-figures-with-gears illustration REMOVED entirely

**`brand_voice/lasso_house_style.md`** — added section 4 "Layout Archetypes"
documenting the six archetypes (editorial opener + five illustration archetypes).

**`content_library/speed_to_lead.jpg`** deleted — 32-byte corrupt stub, no
pending drafts referenced it. Clears THIN warning on both accounts.

**lasso_fb plan drafts unblocked**:
- Jul 22 (speed_to_lead_carousel) + Jul 29 (summit): status reset from blocked
  back to pending. Blocks were OCR fail-close (no reader locally), NOT stat
  violations. All stats in both creatives are in `02_verified_stats.md`.

**Tests** — 1385 green. Updated snapshots in 4 test files:
- `test_archetypes.py`: `built_by_gym_owners` expected archetype → "editorial"
- `test_b2b_concepts.py` + `test_platform_concepts.py` + `test_platform_ads_concepts.py`:
  HOUSE_SHA256 updated to reflect new concept spec
- `test_story_first.py`: editorial archetype exempt from tension/resolution check

### Action required on Railway

1. Run `regen-library --only built_by_gym_owners` FIRST (new editorial spec).
2. Then `regen-library --set all` to generate the 13 missing lasso_v2_* files.
3. Both commands: `/opt/venv/bin/python -m agent regen-library --only built_by_gym_owners`
   then `/opt/venv/bin/python -m agent regen-library --set all`

---

## House style system wired into creative pipeline (2026-07-17)

### What shipped (suite 1385 green)

**`brand_voice/lasso_house_style.md`** — source of truth for every generated card.
Sections 1-9: brand DNA, hard copy rules, model routing, generation prompt scaffold
(Blocks A-D), five-question grade gate, and retired patterns.

**`agent/config.py`** — two new flags + two new constants:
- `NANO_MODEL_FLASH` (env `AGENT_NANO_MODEL_FLASH`, default `gemini-3.1-flash-image`)
- `HOUSE_STYLE_PATH` (env `AGENT_HOUSE_STYLE_PATH`)
- `nano_flash_enabled()` (env `AGENT_NANO_FLASH_ENABLED`, **OFF** by default)
- `style_gate_enabled()` (env `AGENT_STYLE_GATE_ENABLED`, **OFF** by default)

**`agent/creative_studio.py`** — typographic system + layout rules wired in:
- `HOUSE_STYLE_TYPOGRAPHIC_SYSTEM` + `HOUSE_STYLE_LAYOUT_RULES` constants (section 7)
- `_HOUSE_STYLE_LEAD` updated to include eyebrow, left-aligned headline, deck, asymmetric layout, one depth layer
- `_check_headline_hard_rules()` — raises ValueError for "vendor" in headline
- `_check_prompt_hard_rules()` — raises ValueError for banned centered/symmetric phrases
- `_route_model()` — default ALL cards to Pro; Flash opt-in via `AGENT_NANO_FLASH_ENABLED`
- `generate()` — uses `_route_model()`, logs routing per card, returns `model` + `route` in dict, wires grade gate when `AGENT_STYLE_GATE_ENABLED`

**`agent/grade_gate.py`** — new module. Five-question house-style grade gate:
- Q3/Q4 programmatic; Q1/Q2/Q5 vision-model (pass-through when vision unavailable)
- `grade_card()` returns `GradeResult(scores, passed, failed_questions)`
- Pass threshold: ≤1 hard False of 5 questions

**`agent/__main__.py`** — `regen-weak-cards` command added (built_by_gym_owners +
speed_to_lead_stat; Pro model; fabrication + grade gate; draft only, never publishes).
Also added `nano_flash` and `style_gate` to `_status()` output.

**Tests** — 3 new/updated test files, 23 new assertions:
- `tests/test_house_style.py` — 8 new assertions (eyebrow, left-aligned, deck, never centered, asymmetric, depth layer, banned phrases absent)
- `tests/test_model_routing.py` — 7 new tests (flash off/on routing + return dict)
- `tests/test_grade_gate.py` — 14 new tests (Q3/Q4 heuristics, GradeResult, grade_card)

Two open decisions in PROGRESS.md unchanged: brand palette and publish path.

---

## Incident post-mortem + story public URL fix (2026-07-17, SHA `dc982bc`)

### Root cause: all pending drafts had no public URL

All 13 drafts in the pending queue had `creative_public_url = ""`. Root cause
chain:
1. Library creatives do not have `public_url` in their JSON sidecars.
2. `AGENT_HOSTING_ENABLED` is OFF on production (default), so `drafter.py`
   never calls `host_media()` and the URL stays empty.
3. Facebook Page FEED posts silently fall back to text-only when no URL
   is present (`_publish_fb_page` in meta_publisher.py). No error, no alert.
4. Instagram feed posts and ALL story posts (both platforms) raise
   `PublishError("needs a PUBLIC media URL")`, which is caught in
   `approvals.py`, posts an ops_alert, then re-raises. The approval handler
   does post an alert, but the story stays silently unposted.
5. The "164 min late" scheduler warning in the digest was STALE historical
   data from before SHA `74d2395` (the `>=` fix). Confirmed: `listener.py`
   line 233 is already `>= target_hour`. Not a fresh regression.

### What shipped (SHA `dc982bc`, suite 1362 green)

`agent/stories.py` — two new blocks after the studio-creative path:

1. **Fallback hosting**: if `creative_public_url` is still empty after the
   studio path, attempt `media_host.host_media()` on the feed creative
   (library or studio) before giving up.
2. **Hard block**: if URL is STILL empty after the hosting attempt, fire a
   named `ops_alerts.alert` ("story draft blocked for … no public URL for …
   Enable AGENT_HOSTING_ENABLED or add public_url to the creative sidecar")
   and return None. No broken draft enters the pending queue, no silent
   publish failure at approval time.

Tests added: `test_story_no_url_blocks_draft_and_fires_alert`,
`test_story_fallback_hosting_provides_url`. Runner test updated to add a
sidecar URL to the test asset.

### Remaining action required by Blake

- **lasso_fb Jul 17-31**: 13 `lasso_v2_*` creatives MISSING. Run on Railway:
  `/opt/venv/bin/python -m agent regen-library --set all`
  (requires `AGENT_NANO_ENABLED=true` + `GEMINI_API_KEY`). Once files exist,
  the 13 pending plan drafts (Jul 17-21, 23-28, 30-31) unblock automatically.
  Jul 22 (speed_to_lead_carousel) and Jul 29 (summit) are now unblocked —
  they were fail-closed by local OCR absence, stats are approved.
  Note: `built_by_gym_owners` MUST be regenerated FIRST (editorial archetype,
  type-led card, new spec) — see below.
- **speed_to_lead.jpg**: 32-byte stub deleted (no drafts referenced it).
- **Public URLs**: all library creatives need either (a) `AGENT_HOSTING_ENABLED`
  armed with R2 credentials so the agent uploads on draft creation, OR (b) a
  `public_url` field in each creative's `.json` sidecar. Without one of these,
  feed posts on FB survive (text-only fallback) but IG feed posts and ALL
  stories continue to fail.
- **Railway cron**: still needs manual dashboard click (see `docs/SCHEDULER_CRON.md`).
- **Fabrication scan**: run on container with OCR key to confirm the 3 blocked
  cards (lasso_ig aee14e3b97, lasso_ig 67cbbbdf3e, lasso_fb ee7b182033) are
  OCR-reader errors vs genuine stat blocks (model-name fix shipped 2026-07-16).

### Grade: B+ (unchanged)

Story public URL failure is now loud and early instead of silent at approval
time. Grade still needs one real gym completing a full 30-day posting month
and Meta App Review cleared for client-owned assets.

---

## Fable 5 Tier 2/3 remainder (2026-07-16)

### Step 1 DONE: locked pre-Echo baseline (SHA `710be29`, suite 1368 passed)

`pre_echo_baselines` table added to the DB (write-once per account: PRIMARY KEY on
account_key, no silent overwrite). New functions in `agent/baseline.py`:
`lock_pre_echo_baseline()`, `read_pre_echo_baseline()`, `baseline_report()`.
Two new CLI commands: `capture-baseline` now also locks the DB record after the
JSON snapshot; `baseline-report --account <key>` reads and prints the locked row.

Confidence grades:
  clean                   first confirmed Echo post found in posts table; pre-Echo
                          window is 8 weeks
  partially contaminated  cutoff from would_publish (draft-only) posts, or no Echo
                          post found at all and window ends at current time
  no reliable pre-Echo data found
                          no API token available, or Graph read failed

On production, run `python -m agent capture-baseline` to lock the number now.
Running again without `--force` is safe (refuses to overwrite). 16 new tests.

### Step 2 ALREADY DONE (prior session): SQLite store on /data

`PendingStore` and the full DB layer are already fully SQLite-backed (WAL, echo.db).
`AGENT_SQLITE_STORE` flag was not added retroactively; the migration shipped complete.
No work done here beyond documenting the already-done status.

### Step 3 DONE: Gemini spend-status CLI + digest alert (visibility only, no auto-reload)

`agent/spend.py` added: reads `gemini_calls:<account_key>` counters from the DB
and computes pct-of-cap for each account. `spend-status` CLI prints a per-account
table with calls, cap, pct, and armed/disarmed state. Digest alert fires at 80% of
cap (one alert per day per bucket, stored in kv to suppress duplicates).

Auto-reload is deliberately NOT built. Whether to raise the cap or top up billing
is Blake's call in the Google Cloud console. See `agent/spend.py` module docstring.
7 new tests.

### Grade: B+ (unchanged)

Fable 5 visibility tracks complete. Grade moves to A when a real gym completes a
full 30-day posting month and Meta App Review is cleared for client-owned assets.

---

## Auto-mint completion + library gap audit (2026-07-16)

### Step 0 complete: encrypted token at rest

All four auto-mint tracks were already merged. This session audited the merged
state against the spec and shipped the CRITICAL correction: intake tokens are
now stored ENCRYPTED AT REST (Fernet), not hashed, so the portal can recover
the raw token and reconstruct the upload link.

Changes shipped (SHA `3f3a13a`, suite 1363 passed):
- `AGENT_INTAKE_ENC_KEY` env var: base64url-encoded Fernet key, set in Railway
  by hand. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `intake_token_encrypted TEXT` column added to gyms table via additive migration
- `intake_tokens.mint()` and `rotate()` store the encrypted blob when the key is set
- `intake_tokens.decrypt_token(account_key)` recovers the raw token for portal use
- `onboard.py` section (g): builds upload link on fresh mint, recovers via decrypt_token on idempotent re-run, falls back to stored plaintext link
- `intake_web.py` portal: reconstructs upload link from encrypted blob first, falls back to stored link
- `onboard_verify.py`: fixed to read token status from gyms table (via `_token_status()`) when AUTOMINT is ON, not the kv store (which was never written)
- `__main__.py`: reads `AGENT_UPLOAD_BASE_URL` env var when `--base-url` is absent
- `docs/ENV.md`: added `AGENT_ONBOARD_AUTOMINT` and `AGENT_INTAKE_ENC_KEY` rows

Acceptance: onboard fresh gym -> token + voice + brain + trust=FULL_APPROVAL + publish OFF + creds-pending + link printed. Re-run idempotent. onboard-verify reports READY-FOR-UPLOADS=YES, READY-TO-PUBLISH=NO, reason "publish creds pending by hand." No Meta credential ever touched.

### Step 1: library gap — 13 lasso_v2_* assets MISSING (LOUD, action required)

`library-audit --account lasso_fb` reports 13 MISSING creatives for Jul 17-31:
lasso_v2_built_by_gym_owners, lasso_v2_b2b_five_companies, lasso_v2_platform_719_booking,
lasso_v2_platform_ads_booking_bars, lasso_v2_summit_announce, lasso_v2_one_screen,
lasso_v2_b2b_35k_caught, lasso_v2_platform_stuck_lasso, lasso_v2_platform_ads_stuck,
lasso_v2_summit_playbook, lasso_v2_follow_up_problem, lasso_v2_b2b_speed_to_lead,
lasso_v2_platform_six_engines.

These assets must exist in `content_library/` for those days to draft. The
`regen-library` command generates them but requires `AGENT_NANO_ENABLED=true` +
`AGENT_NANO_API_KEY` set in the container. `plan-month --replan` would substitute
available assets but requires `AGENT_PLAN_MONTH_ENABLED=true` (currently OFF).

**BLAKE BY HAND (choose one):**
1. Run `python -m agent regen-library --set all` on the container (NANO must be armed).
   Each generated card appears at the `lasso_v2_*` path the plans already reference.
2. OR set `AGENT_PLAN_MONTH_ENABLED=true` in Railway and run:
   `python -m agent plan-month --account lasso_fb --month 2026-07 --from 2026-07-17 --replan --write`
   This substitutes available library assets for the 13 missing days.

Until one of these runs, the 13 lasso_fb drafts for Jul 17-31 will draft with MISSING
creative (BLOCKED at queue time). The daily scheduler WILL alert on each miss.

Also THIN: `speed_to_lead.jpg` is a 32-byte placeholder on both accounts. Not
referenced by any current plan (lasso_fb Jul 22 uses `speed_to_lead_carousel`, which
is healthy). Replace the stub with the real image if this slot is to be used standalone.

### Step 2: Railway cron — manual Blake dashboard action required

`docs/SCHEDULER_CRON.md` has the complete click-by-click runbook (create
`echo-daily-cron` service, set cron `30 14 * * *`, attach same `/data` volume,
copy env vars). This CANNOT be created via code; it requires clicking through
the Railway dashboard.

**BLAKE BY HAND:** follow `docs/SCHEDULER_CRON.md` steps 1-7.

### Step 3: speed-to-lead editorial card — not yet drafted

`content_library/lasso_p2_speed_to_lead_stat.png` (3.8MB, Jul 1) exists. This is
the editorial card from a prior session. Status:
- No pending draft references it on lasso_ig or lasso_fb
- Fabrication gate cannot verify locally (OCR model requires API key absent in dev shell)
- The card's `.json` note uses "80 percent of conversions happen when you respond in
  under 5 minutes" — phrasing differs from the USE line on 02_verified_stats.md line 41
  ("Contact a new lead within 5 minutes and you can lift conversions up to 80 percent.")
- The LOCKED conflict ("80% more conversions" in three versions) needs resolution before
  this card can safely post

**BLAKE BY HAND:**
1. Run `python -m agent fabrication-scan` on the container (OCR key present) to read the
   card's rendered pixels and confirm CLEAN vs BLOCKED.
2. If CLEAN: add `lasso_p2_speed_to_lead_stat` to a future plan slot for lasso_ig and lasso_fb.
3. If BLOCKED: the card's rendered stat is a locked variant; use regen-library to regenerate
   with the exact USE wording ("Contact a new lead within 5 minutes and you can lift
   conversions up to 80 percent.") once NANO is armed.

### Grade: B+ (unchanged)
Auto-mint is complete. No new grade gate cleared. A still requires one real gym completing
a full 30-day posting month and Meta App Review cleared for client-owned assets.

---

## OCR model name fix + model-404 sanity check (2026-07-16)

`fabrication-scan --all` on production failed every OCR read:
"This model models/gemini-2.5-flash is no longer available to new users." The
fail-closed rule worked (3 cards BLOCKED, 0 passthrough) so nothing fabricated
shipped. Model-name fix only, not a design change.

- **OCR_MODEL default is now `gemini-3.5-flash`** (was `gemini-2.5-flash`, which
  Google retired for new accounts). Verified against Google's live model listing
  as the current stable, vision-capable default flash (the target of
  `gemini-flash-latest`). Still overridable by hand via `AGENT_OCR_MODEL`, still a
  separate config from the image-generation model. Only the default value changed.
- **Model-not-found sanity check (added).** `ocr_check._warn_if_model_missing`
  posts ONE loud ops warning naming the bad model string on the model-not-found
  family (incl. "no longer available"), then re-raises so the read fails. So this
  class of break is loud immediately, not discovered mid-scan.
- **Fail-closed unchanged.** A bad model name makes the read raise -> the OCR
  attempt reports it could not run -> a card with rendered pixels is BLOCKED
  ("could not verify rendered text against approved claims"), never a passthrough.
  Proven by test_gate_fails_closed_when_reader_raises.

### Verification note (honest)
The live resolve call could NOT be run from the dev shell used for this fix: it
has no API key and no `google` SDK installed (existence-checked; no key value ever
read or printed). The model name was verified against Google's official model
listing instead. Local `fabrication-scan --dry-run`: checked 16, clean 13, WOULD
BLOCK 3, UNVERIFIABLE-passthrough 0 (no reader locally, so pixel-bearing cards
fail closed, as designed). Blake: run `python -m agent fabrication-scan` on the
container (key present) to confirm the 3 production cards (lasso_ig aee14e3b97,
lasso_ig 67cbbbdf3e, lasso_fb ee7b182033) now resolve to CLEAN or a genuine
stat-BLOCK rather than a model-error block.

### Grade: B+ (unchanged)
A model-name correction does not move the grade. A still needs a real gym's full
30-day month of posts and Meta App Review cleared.

---

## OCR reader wiring + fail-closed pixel gate (2026-07-16)

`fabrication-scan` returned UNVERIFIABLE on every card. Root cause, from the code:

- **Wiring bug (fixed).** The OCR reader (`ocr_check._default_reader`, and DAM
  autotag) called `config.NANO_MODEL` to transcribe text out of an image. NANO_MODEL
  is the image GENERATION model (Nano Banana: `gemini-3-pro-image`, and Blake's
  `gemini-3.1-flash-image`). Generation models return image parts, not text, so
  `resp.text` was always empty and the read produced nothing. Fix: new
  `config.OCR_MODEL` (default `gemini-2.5-flash`, override `AGENT_OCR_MODEL`), a
  vision-capable TEXT model, used ONLY for reading. The generation model is
  unchanged. Same API key.
- **Backfill (added).** `fabrication-scan` OCR-reads any card lacking recorded
  sidecar text RIGHT NOW and records the read, so pre-gate cards get scanned
  instead of sitting unverifiable forever. (Cards drafted before the gate shipped
  had no recorded text; this is what backfill is for.)
- **Fail-closed (the critical rule).** UNVERIFIABLE was treated as passable, the
  same fail-open hole under a new name. Now: a card that HAS rendered pixels the
  gate cannot read or verify is BLOCKED, reason "could not verify rendered text
  against approved claims", not passed. A creative with NO renderable text (a video,
  or no image) is exempt. A successful read that finds no text records an exempt
  sentinel (pure photo). The gate distinguishes never-scanned from scanned-no-text
  from unreadable. Fail-closed is active whenever the studio is armed (production);
  with the studio fully disarmed the gate falls back to the deterministic note
  check so dev / non-OCR deployments still function. Ships ON, no flag.

### fabrication-scan output after the fix (local dry-run, no reader here)
checked 16, clean 13, WOULD BLOCK 3, UNVERIFIABLE-passthrough 0. The 3 blocked
(`book_campaign`, `speed_to_lead_carousel`, `summit`) have rendered pixels on disk
but no reader in this local shell, so they fail closed instead of passing. The
other 13 reference `lasso_v2_*` files absent from this tree (no renderable creative
= exempt). On the container (reader wired via OCR_MODEL, files present) those cards
are read, recorded, and resolve to clean or BLOCKED(stat); fail-closed then bites
only on a genuine read outage. Run `python -m agent fabrication-scan` there to
backfill and clear the queue.

### Grade: B+ (unchanged)
Does NOT move to A. The reader is wired and the gate is fail-closed, but A still
needs a real gym completing a full 30-day month of posts and Meta App Review
cleared for client-owned assets. Code correctness is not the gate to A.

---

## Fabrication gate on pixels + stat-slab retirement (2026-07-16)

A card scheduled 2026-07-16 rendered "80% more conversions" as a giant stat slab.
That number is NOT an approved receipt (verified_stats.md LOCKS "80% more
conversions" pending Blake's kill-or-source; the approved wording is "lift
conversions up to 80 percent", a different claim). The caption fabrication gate
was watching the words in the post but NOT the words baked INTO the image.

### Two fixes shipped (both hold the human-approval gate; nothing auto-publishes)

**1. Fabrication gate extended to the pixels.** `agent/pixel_gate.py` applies the
SAME claim rule captions obey (rotation.is_gate_clean + knowledge USE-lines +
approved social proof) to the text rendered INTO a creative. Any number/percent/
claim with no approved receipt BLOCKS the card and NAMES the number; never softens,
never falls back, never publishes.
- Ships ON (a safety gate is never off): the deterministic layer is free and
  always runs. `draft_post` (all library-card paths), `daily_studio` (the generated
  headline), and social proof all gate before a card can go PENDING.
- OCR at ingest, gate daily (Blake's call): a card's rendered text is recorded to
  its sidecar (`rendered_text`) once at generation/regen; every later draw gates it
  for free. When the studio is armed, the OCR belt reads the pixels once and records
  the read, so a silent generator drift (the slab class) is caught the first time
  the card is seen. `agent/ocr_check.py` gained `headline_block`: a number on the
  image the approved headline never asked for now BLOCKS (was warn-only).
- Retro scan: `python -m agent fabrication-scan [--dry-run]` walks the pending/
  planned queue and AUTO-BLOCKS (Blake's call) any card whose rendered pixels carry
  an unapproved stat, naming the number. Dry-run reports without blocking.

**2. Stat-slab template retired.** The giant-number-on-navy layout is off brand.
`stat_hero` is removed from `creative_studio.LAYOUTS`; any concept naming it remaps
to `chart` (a labeled data visual, never a colossal single figure) and a
`NO_STAT_SLAB_LAW` rides every prompt. The social-proof `NUMBER_CARD_STYLE` no
longer renders a HUGE slab; the stat reads as one clear line in the house style.
The 17 b2b/platform/platform_ads concepts that used stat_hero now derive chart.
Navy/red canvases stay (brand colors); only the slab LAYOUT is gone.

### Fabrication-scan output (2026-07-16, local dry-run)
16 pending lasso_fb planned cards are UNVERIFIABLE locally (no OCR key, and the
lasso_v2 assets they reference are absent from this tree). On production (studio
armed, real /data) the scan reads their pixels, records the text, and auto-blocks
any carrying an unapproved stat. Run `fabrication-scan` there to clear the queue.

### Grade: B+ (unchanged)
Gate to A: one real gym completes a full 30-day month + Meta App Review cleared.

---

## Scheduler reliability fix + library audit (2026-07-16)

Root cause confirmed: `now.hour == target_hour` strict equality created a 60-minute
draw window. Any Railway redeploy after 14:59 UTC silently skipped the day's draw.
Evidence: 164 min late (2026-07-15) and 589 min late (2026-07-16).

### Shipped

- **Fire condition fix**: `now.hour == target_hour` → `now.hour >= target_hour` in
  `_daily_scheduler()`. A restart at any hour on or after the target fires today's
  draw immediately if it has not already run.
- **`run-daily` idempotency**: CLI reads `scheduler_state.json` before running.
  If today's draw is already recorded, exits clean. Belt + suspenders: both the
  in-listener loop and the cron service are safe to fire on the same day.
- **`_next_fire()` fix**: removed `now.hour <= target_hour` condition; now returns
  today's fire time whenever today has not run (regardless of current hour).
- **`scheduler-status` CLI**: `python -m agent scheduler-status` — loop liveness,
  last draw, next expected draw, cron fallback note.
- **`docs/SCHEDULER_CRON.md`**: click-by-click runbook for the Railway cron service
  (third service, same repo + volume, cron schedule `30 14 * * *`).
- **Library audit** (`python -m agent library-audit --account <key>` / `--all`):
  walks every creative in the account's library and any pending planned drafts;
  reports MISSING (file/dir absent or pending draft references absent path) and
  THIN (image < 10KB, video < 100KB, carousel stub < 2 slides). Hidden dirs
  excluded (.DS_Store, .claude-flow etc). Preflight warning in `runner.py` fires
  an ops alert when `pick_next()` returns a creative with a known issue.
- **22 new tests** across `tests/test_scheduler_fix.py` and
  `tests/test_library_audit.py`.

### `library-audit --all` output (2026-07-16)

```
LIBRARY AUDIT -- lasso_ig  (content_library)
  creatives found: 18
  MISSING (0): none
  THIN (1)
    speed_to_lead [image]  THIN (32 bytes < 10000 minimum)

LIBRARY AUDIT -- lasso_fb  (content_library)
  creatives found: 18
  MISSING (13)
    lasso_v2_built_by_gym_owners   pending draft plan_lasso_fb_2026-07-17 on 2026-07-17
    lasso_v2_b2b_five_companies    pending draft plan_lasso_fb_2026-07-18 on 2026-07-18
    lasso_v2_platform_719_booking  pending draft plan_lasso_fb_2026-07-19 on 2026-07-19
    lasso_v2_platform_ads_booking_bars  pending draft plan_lasso_fb_2026-07-20 on 2026-07-20
    lasso_v2_summit_announce       pending draft plan_lasso_fb_2026-07-21 on 2026-07-21
    lasso_v2_one_screen            pending draft plan_lasso_fb_2026-07-23 on 2026-07-23
    lasso_v2_b2b_35k_caught        pending draft plan_lasso_fb_2026-07-24 on 2026-07-24
    lasso_v2_platform_stuck_lasso  pending draft plan_lasso_fb_2026-07-25 on 2026-07-25
    lasso_v2_platform_ads_stuck    pending draft plan_lasso_fb_2026-07-26 on 2026-07-26
    lasso_v2_summit_playbook       pending draft plan_lasso_fb_2026-07-27 on 2026-07-27
    lasso_v2_follow_up_problem     pending draft plan_lasso_fb_2026-07-28 on 2026-07-28
    lasso_v2_b2b_speed_to_lead     pending draft plan_lasso_fb_2026-07-30 on 2026-07-30
    lasso_v2_platform_six_engines  pending draft plan_lasso_fb_2026-07-31 on 2026-07-31
  THIN (1)
    speed_to_lead [image]  THIN (32 bytes < 10000 minimum)
```

**Action needed:** 13 `lasso_v2_*` creatives referenced by planned lasso_fb drafts
(Jul 17-31) are missing from `content_library/`. Upload the `lasso_v2` assets or
replan those days. `speed_to_lead.jpg` is a 32-byte stub — replace with the real image.
`speed_to_lead_carousel/` (3 slides) is clean.

### Grade: B+ (unchanged)
Gate to A: one real gym completes a full 30-day month + Meta App Review cleared.

---

---

## Autonomous onboarding + intake-token store (2026-07-16)

- T1 (Intake Token Store): gyms table, SHA-256 hashed token store, mint/rotate/revoke, tokens --list CLI. SHA: d7f93bdb643e45f24126140f3d0ddfe43ea4d1b2
- T2 (Autonomous Onboard): onboard CLI, voice+brain scaffold, trust=FULL_APPROVAL, publish OFF, upload link. SHA: b9f9aa074c13b164634d1273eba313da349bd42f
- T3 (Intake Web + Portal): data-store token lookup, per-token rate limit, portal /portal/gym/<key> endpoint. SHA: 4b443c25ef592097f3a52791c8e0ac28ded07927
- T4 (Onboard Verify): onboard-verify CLI, READY-FOR-UPLOADS vs READY-TO-PUBLISH per gym. SHA: c828d371cd4cf0cdc18795e1b66bae51d35b15cc

### Readiness grade: B+
Grade does NOT move to A. Gate is one real gym completing a full 30-day month of posts plus Meta App Review cleared for client-owned assets.

---

## Stage 2 client-readiness build (2026-07-15)

- T1 (Intake Worker): AGENT_INTAKE_WORKER flag, thumbnail gen, missing-caption gate, low-res flag, intake-worker/intake-status CLI. SHA: 4e29f2c96e8d301d0bba8d0e6f8864258f52caed
- T2 (Portal Approvals): Kill/Deny actions, per-gym scoping, portal-callable endpoints, trust CLI. SHA: 27b4eea2940315c31af1bf7d3bcbf15a69b54057
- T3 (Voice Brain): voice-template CLI, brain-export CLI, brain events wired to approval flow. SHA: 4c98fc2d219ee7b71b81caae8ab4091439564371
- T4 (Runway Alerts): AGENT_RUNWAY_ALERTS flag, dash-free text-back, glanceable runway card. SHA: bc605631dc4edf836f937a07bed0c67e638c1cd2

### Readiness grade: B+
Grade does NOT move to A. The gate to A requires: (1) a real gym completes a full 30-day month of posts, (2) Meta App Review cleared for client-owned assets. Not code alone.

---

## Overnight parallel build 2 (2026-07-15)

- Track 1 (Reporting Live): monthly report uploads HTML to R2, posts URL to Slack, --html flag on report CLI. SHA: f6134ca482f07577294b09736c9c2e12aeb3ab3e
- Track 2 (Calendar view): calendar-export JSON + standalone HTML V3 brand palette, multi-account switcher. SHA: 16c246b15af87b34972d77041ab1b7cc16588c4a
- Track 3 (Onboard dryrun): onboard-dryrun 30-day harness, no live tokens, HTML review bundle. SHA: 2b19293abf0c36f34b2e42a27d866f258c784a2c
- Track 4 (Meta check): meta-check token scopes reachability publishable status. SHA: d45c75426289ca473877ecfbb87d62b0e06b71b5

### Readiness grade: B+ (honest)
Code is complete. Not A until: (1) real gym month of posts, (2) Meta App Review cleared for clients.

---

Commits since last update:
- `171f488` — intake-web deployable: `/healthz` route, `build_server(port=0)`, Procfile
  `web:` entry, `docs/INTAKE_DEPLOY.md` Railway runbook, 5 tests.
- `da0fb16` — preflight command (`python -m agent preflight --account <key> [--live]`),
  8 checks (PASS/WARN/FAIL), READY/NOT READY verdict, exit nonzero on FAIL; channel
  ownership guard in run-daily skips (with alert) any client account missing
  `slack_channel` when a shared channel is configured. Suite: **1107 passed, 0 failed**.
- `[this commit]` — 7-day cadence: `POSTING_SKIP_DAYS` default changed from `["sat"]`
  to `[]`. Saturday is now a posting day by default. `AGENT_POSTING_SKIP_DAYS`
  env override still works. Tests updated to monkeypatch `POSTING_SKIP_DAYS=["sat"]`
  where they need the old behavior; new `test_all_seven_days_post_by_default` +
  `test_skip_days_env_override` assert the new default.

---

## Scheduler reliability — heartbeat + cron fallback (2026-07-14)

The listen process now writes a SCHEDULER HEARTBEAT (timestamp + next fire time)
to the store every loop cycle; `python -m agent status` shows it under
"-- scheduler --". If today's draw is more than 30 minutes past the target hour
(AGENT_DAILY_HOUR_UTC, default 14:00 UTC) with no run recorded, ONE deduped ops
alert fires naming the fix.

RAILWAY CRON FALLBACK (if the in-listener scheduler ever proves unreliable):
1. Railway -> the echo project -> New -> Service -> from this same repo.
2. Settings -> Cron Schedule: `30 14 * * *`  (14:30 UTC daily, 30 min after the
   listener's own window so they never race; idempotent drafts make a double
   fire a no-op anyway).
3. Settings -> Custom Start Command: `/opt/venv/bin/python -m agent run-daily`
4. Share the same env vars as the echo worker service (tokens, DB path/volume,
   channel, flags). Attach the SAME /data volume so it reads the same store.
5. Optionally set AGENT_SCHEDULER_ENABLED=false on the listener to hand the
   draw fully to cron (the listener keeps Slack buttons + polling lanes).
The cron service runs `run-daily` once and exits; every gate (approval, publish
flag, first post never automated) applies exactly as in the listener.

---

## Posting cadence — 2026-07-12 (current live rotation)

7 days a week, one post per account per day. `AGENT_CATEGORY_ROTATION=true` must be
set in Railway env. `AGENT_POSTING_SKIP_DAYS` defaults to empty (no skip days).

| Day | Slot |
|-----|------|
| Mon | podcast release |
| Tue | platform |
| Wed | b2b |
| Thu | podcast clip |
| Fri | summit (doctrine fills until the summit ramp starts in Sept) |
| Sat | platform |
| Sun | podcast infographic |

Book campaign leads the calendar when armed (`AGENT_BOOK_CAMPAIGN_ENABLED`), capped
at 1 post/week. Slots above describe the fallback pillar when the book is not running.

Clipper status: **Phase 1 SELECTION ONLY** — selection logic, ranked plan, Slack post
of the plan. Renders no video. `AGENT_CLIPPER_ENABLED` defaults OFF. Blocked on the
first Riverside export dropped into the R2 episode inbox
(`echo/episode_inbox/lasso_episodes/`). Phase 2 (FFmpeg render, captions, audiogram)
and Phase 3 (wire into Echo as held drafts) are **built but dark** behind
`AGENT_CLIPPER_RENDER_ENABLED`.

---

## Hardening pass — 2026-07-11 (pre 10-client launch)

Suite: 1091 passed, 0 failed, run with `.venv/bin/python -m pytest`. The
7 "reportlab reds" were an interpreter problem (system python has no
reportlab); those suites now SKIP with the reason named when run wrong.

### Fixes shipped (each its own commit, all pushed)
- Store read funnel survives NULL/malformed data blobs and unknown statuses:
  one legacy row can no longer kill the daily run or the Approve tap.
- Review-cycle loop no longer crashes the run tail when the scheduler calls
  run_daily with accounts=None (guaranteed TypeError whenever armed, fixed).
- Slack transport errors degrade to a failed post instead of aborting the
  whole run (the pre-loop voice notice was a single point of fleet failure).
- plan-month --replan without --write is a TRUE preview: it deleted pending
  drafts even in preview mode (destructive dry run, fixed + tested).
- Per-client approval isolation: cards route to each account's own Slack
  channel; each account's own approvers can act (global approver still can).
- Gemini spend cap is per account: one client's volume can no longer starve
  every other client's creative for the day.
- Book queue items are consumed only after the draft is confirmed built; a
  studio/hosting outage no longer silently eats a verbatim queue post.
- requirements.txt now declares cryptography, faster-whisper, anthropic
  (GHL webhook verify and clipper crashed on a clean deploy when armed).
- status shows all 43 capability flags (11 were invisible) + source paths;
  a guard test derives the flag list from config.py so it can never rot.
- Honest CLI everywhere: run-daily states its reason and splits
  pending/blocked; `help` lists all ~40 commands; unknown commands print
  usage; every bare-zero command states WHY (backfill, seed-calendar,
  check-tokens, runway, capture-baseline, report account-filter misses,
  podcast-cards, clip plan); scheduler announces every lane armed/dormant.
- Silent swallows are loud: failed dead-letter no longer reprocesses the
  same bad file forever; unreadable episode table alerts; audit-write
  failures print.
- Runtime SQLite store gitignored (client draft data was one git add -A
  from being committed).
- 12-account launch simulation lives in the suite: 3 corrupt-row gyms,
  1 token-less, 1 empty library — run completes, healthy accounts draft,
  the empty library cards BLOCKED with the reason, nothing publishes,
  a crashing account alerts and skips while the rest continue.

### Multi-client readiness grade: C+ (honest)
- Safety and isolation: B+. Approval gates, per-account trust ladder,
  token isolation, draft-ID isolation, per-client channels/approvers/spend
  all verified or fixed this pass. Nothing publishes without a tap.
- Client content depth: D. Every campaign and brain feature is LASSO-only
  by design; a client gym only ever gets the plain library-pick draft. A
  gym with a thin library gets a BLOCKED card every day. Launchable ONLY
  if each gym ships day 1 with a stocked content library.
- Operations: C. Onboarding is manual (paste the Account entry, hand-set
  tokens/channel/approvers) with no preflight validator; intake-web has no
  deploy target; fan-out is serial with no Slack 429 backoff.

### Ranked remaining gaps for the 10-client launch
1. (L) Client content engine: per-account source docs + brain plumbing, or
   an explicit "library-only product" decision + stocked libraries per gym.
2. (DONE — da0fb16) intake-web deployable: `/healthz`, Procfile `web:` entry,
   `docs/INTAKE_DEPLOY.md` runbook. Still needs: Blake creates the Railway web
   service and sets env vars per the runbook.
3. (DONE — da0fb16) Onboarding preflight: `python -m agent preflight --account
   <key>` runs 8 checks, prints READY/NOT READY, exits nonzero on FAIL. Channel
   ownership guard prevents silent cross-client routing.
4. (M) Fan-out hardening at 12+ accounts: Slack 429 backoff/retry; consider
   chunking. (Per-client channels shipped this pass reduce the burst risk.)
5. (S) Document the ~40 undocumented env vars incl. META_APP_ID/SECRET;
   single-owner constants for GEMINI_DAILY_CAP, REPORTS_DIR, BASELINE_DIR.

### Podcast / clipper status
Phase 1 selection only. Ranked clip plans post to Slack; nothing renders.
AGENT_CLIPPER_ENABLED off. Blocked on the first Riverside export drop.

---

## Stage 0 — Foundation
- [x] Canonical LASSO brand bible (`brand_voice/lasso_voice.md`)
- [x] Reference repo scaffolded (`lasso-echo`), own body, Ranger spine as pattern
- [x] Gates baked into code (approval, draft-only, trust ladder, no-fabrication)
- [x] Test suite green (175, deployed at cd8000b)
- [x] Stage 1 build prompt for Claude Code
- [x] Railway + separation plan documented (own project, own service, #echoclaude)
- [x] Brain hook stubbed (read-only, proposes, never rewrites voice)

## Stage 1 — LASSO only: draft + approve + publish (DRAFT-ONLY)
- [x] Daily drafter, one feed post per account
- [x] Slack approval cards (Approve / Edit / Skip) to #echoclaude
- [x] Meta publisher with draft-only guard (publish flag OFF)
- [x] Post logging (no tokens)
- [x] lasso-echo repo (private), Railway project + echo service + env vars
- [x] Meta App "LASSO Social Poster" (Dev mode ok for own accounts)
- [x] Per-account tokens + ids set by hand
- [x] Slack app + #echoclaude wired; first cards proven; app renamed to Echo
- [x] Voice doc loading fixed (real bible on Railway)
- [x] CTA rotation (growth-biased, placeholder-filtered)
- [x] Hashtags capped to 5; bible updated for 2026 (3 to 5 tags, caption SEO)
- [x] Carousel support (folder = carousel, draft-only)
- [x] Reels support (draft-only, a video = a Reel)
- [x] Growth pack pushed via Claude Code + redeployed
- [x] Inline creative preview on the approval card (see the image before approving)
- [x] Creative Studio module wired (Nano Banana infographics, flag AGENT_NANO_ENABLED OFF)
- [x] Media hosting shipped: S3-compatible, 200-client hardened (tenant-scoped, dedupe, retry), wired into drafts, flag OFF; stand up bucket + creds by hand to arm
- [x] Infographics target 4:5 PORTRAIT (1080x1350) for the IG/FB feed; V3 palette + clean house style locked; aspect tunable via AGENT_IMAGE_ASPECT
- [x] Dropped personal FB: blake_personal marked inactive (Meta ended personal-profile publishing 2018); run-daily drafts lasso_ig + lasso_fb only (record kept)
- [~] Content brain: drafts the daily post from the source doc (brand_voice/lasso_now.md)
      across the 5 pillars, growth CTA, 5 hashtags, no fabrication; flag AGENT_CONTENT_BRAIN_ENABLED OFF
- [~] Google Business Profile posting branch (local posts): gbp_publisher, draft-only guard,
      routing (GBP -> gbp_publisher, IG/FB -> meta_publisher), content-brain GBP variant
      (trimmed summary, one image, CTA button, no hashtags); flag AGENT_GBP_ENABLED OFF. See BUILD_SPEC.md Addendum A
- [x] Stories draft path: one 9:16 (1080x1920) Story per account per day alongside the feed
      post, reusing the day's approved creative (9:16 re-render via per-use aspect when the
      studio is armed, else the feed image as is); no caption, PENDING in the same card flow,
      loudly labeled STORY. Publish path (IG STORIES container / FB photo_stories) sits behind
      BOTH the publish flag AND AGENT_STORIES_ENABLED (code default OFF; ARMED in production)
- [~] Caption SEO (2026): content brain front-loads the hook and moves a body line carrying the
      hook's topic terms first among the bodies; reorder of APPROVED lines only, never new text;
      flag AGENT_CAPTION_SEO_ENABLED OFF
- [~] Per-platform caption variants: IG keeps up to 5 approved tags, FB Page keeps at most 2 at
      the end; selection only from the approved set; flag AGENT_PLATFORM_VARIANTS_ENABLED OFF
- [~] Creative rotation + variety guard: no-repeat window (default 14 days, served log on
      /data), consecutive days never share a pillar, approved library cycles alongside the
      Nano card (Nano one source among several), fabrication gate supreme (stat-bearing
      creatives excluded until their claim is cleared in knowledge USE stats or approved
      social proof; thin pool falls back to oldest approved + one ops alert);
      flag AGENT_ROTATION_ENABLED OFF, window env AGENT_ROTATION_WINDOW_DAYS
- [x] House style LOCKED to the illustrated-diagram concept: cream canvas (never a solid
      slab), one navy headline top, body is a line-icon diagram with UPPERCASE labels +
      flow arrows, red as the single accent, one idea per card; Stories designed 9:16 from
      scratch. Seed library swept: ALL 14 slab cards classified OFF-STYLE and held out via
      content_library/style_exclusions.json (nothing deleted; regenerate card by card and
      remove each line to bring the slot back). BLAKE BY HAND: regenerate replacements
- [~] regen-library CLI (manual, like capture-baseline; no flag, never scheduled): rebuilds
      the seed library in the v2 house style. 8 non stat concepts (2 with from scratch 9:16
      story variants), lasso_v2_ files + json sidecars with hosted public_url, prints one URL
      per card for the eyeball pass; --only <key> single card redo, --dry-run prints prompts
      free. Story variants never enter feed rotation. BLAKE BY HAND: run it in the container,
      eyeball every URL, redo misses with --only
- [x] Layout archetypes inside the locked house style: FLOW, SPLIT, HERO, PATH, HEADLINE
      (structure varies, brand never does; secondary knobs per archetype: illustration
      scale, label density, red accent placement). Regen batch assigned (no archetype more
      than twice); story variants inherit the archetype recomposed 9:16 with safe zones;
      daily Nano cards rotate archetypes deterministically; rotation logs the served
      archetype and softly prefers alternation (never overrides the no repeat window or
      the fabrication gate)
- [~] Opus Clip ingest: pulls finished clips via the documented API (Bearer key
      OPUS_API_KEY by hand; discovery = pinned AGENT_OPUS_PROJECT_IDS + collections since
      the API has no bulk project listing; webhooks are outbound only so polling it is).
      CLI `pull-opus` (manual first): watermark on /data, sha256 dedupe, R2 hosting, video
      asset + sidecar (source=opus, clip id, title, duration, pulled, note = the clip's
      own title/words), one URL printed per clip. Clip drafts as a Reel through the normal
      path, held for approval; video is its own rotation pillar. Dormant poll behind
      AGENT_OPUS_POLL_ENABLED (interval AGENT_OPUS_POLL_MINUTES, default 60), failed clips
      retry then dead-letter with one ops alert. Flags AGENT_OPUS_ENABLED +
      AGENT_OPUS_POLL_ENABLED, both OFF
- [x] SQLite store on /data (echo.db, WAL): drafts, posts, served, snapshots, counters,
      kv; legacy pending_drafts.json / rotation_served.json / post_log.jsonl migrate once
      with .migrated.bak backups; storage swap only, no behavior change
- [~] Reporting live path: daily Graph snapshot job in the listener after the daily
      draft (VIEWS never impressions + reach/likes/comments/saves/shares/followers,
      per post metrics refreshed), monthly-report CLI builds the per account 30 day
      HTML report (V3 brand, /data/reports) + Slack summary + the creative REFRESH
      proposal (pillar/archetype/set performance from real data, three angles cited
      only from approved sources, plain raw material ask list). Gate stays
      AGENT_REPORTING_ENABLED (OFF)
- [~] Creative runway card: days of approved gate-clean content left per account
      (in-style, unposted, gate-clean only), one daily line with green/amber/red +
      projected zero date, debounced low-runway ops alert asking for raw material;
      flag AGENT_RUNWAY_ENABLED OFF, threshold AGENT_RUNWAY_ALERT_DAYS (7)
- [~] Trust ladder as data: per account levels (0 full approval forever by default,
      1 routine calendar auto AFTER a human approved the monthly calendar), levels
      hand-edited config only, typos fail safe to 0; DOUBLE GATE via
      AGENT_TRUST_LADDER_ENABLED (OFF) so nothing changes today; the auto-publish
      wiring itself stays a deliberate by-hand step. Enforcement unchanged
- [~] add-client CLI (manual): full per client scaffold in one command (voice doc TODO
      template, social_proof.md with the Permission: yes rule header, library folder,
      printed Account config entry at level 0 + the by-hand checklist). Touches no env,
      creates no tokens, arms nothing; idempotent re-run never destroys hand edits
- [~] Quality + cost guards: headline OCR check (Gemini vision transcription, lowest
      cost, since the container has no pure python OCR; mismatch = warning line on the
      card, never a block; flag AGENT_OCR_CHECK_ENABLED OFF) and Gemini spend cap
      (per day counter in the store, at AGENT_GEMINI_DAILY_CAP default 40 generation
      pauses for the day with one ops alert and library-only selection takes over;
      flag AGENT_SPEND_CAP_ENABLED OFF)
- [~] Evening digest: one Slack line per day at AGENT_DIGEST_HOUR_UTC (default 23):
      drafted / approved / published / blocked / runway days, assembled from the /data
      store; sent mark persisted (restart never double-sends); flag AGENT_DIGEST_ENABLED OFF
- [~] White label PDF export: monthly-report --pdf renders the 30 day report as a
      clean branded PDF (reportlab rebuild; weasyprint/wkhtmltopdf need system libs
      the container lacks), per account white labeling (display name + optional
      brand_voice/<client>/logo.png; LASSO default), dash free text layer
- [~] Social Grade client report card: grade-card CLI renders the computed grade
      (A to F + six area rubric + before/after posting frequency) as one page V3
      HTML + PDF from live store data; respects AGENT_GRADE_ENABLED; drafts nothing
- [~] DAM v1: consent guard (fail safe: people=true needs consent=granted, unknown
      excluded; absolute in rotation + runway; flag AGENT_CONSENT_GUARD_ENABLED OFF;
      arming an untagged library excludes everything until tagged, by design),
      perceptual near-dupe collapse (dam-scan marks dupe_group sidecars; rotation
      keys on the group so the window blocks near-identical reposts), auto-tag (one
      Gemini vision call per new asset: tags + people flag + description; low
      confidence marks review=true; counts against the spend cap; flag
      AGENT_AUTOTAG_ENABLED OFF)
- [~] Decision audit log: append-only audit table records every selection (and WHY),
      every gate exclusion (fabrication, consent, style), publish confirms, and every
      ops alert (even when Slack is dormant); reasons pass the secret scrub; `audit`
      CLI prints the readable trail. Always on, no flag: logging truth is not optional
- [~] Nightly brain (the read only proposer the spec stubbed, now real): one Slack
      note per night after the digest hour: what is winning (pillar/archetype/set from
      real engagement), one angle QUOTED from approved sources with its citation
      (LOCKED knowledge can never appear), one question when data is thin. Proposes,
      never creates, never schedules; flag AGENT_BRAIN_PROPOSALS_ENABLED OFF
- [~] Store backup + restore: nightly consistent sqlite snapshot to R2
      (echo/backups/, 14 day retention, one ops alert on failure only; flag
      AGENT_BACKUP_ENABLED OFF, hour AGENT_BACKUP_HOUR_UTC default 2) and
      restore-store CLI (staging + verification counts; never touches the live db
      without --confirm; old db kept as .pre_restore.bak)
- [~] Client welcome kit: welcome-kit CLI renders one V3 page (HTML + PDF) per
      client: how approval works, texting creative in, what the report covers, the
      trust rules in plain language; fixed template copy only, no pricing, no dashes
- [~] THE FULL GYM book campaign: knowledge/ book docs registered as approved
      sources (book = MASTER; its LOCKED section blocks like locked stats: LAUNCH
      DATE, BUY LINK, PRICE, subtitle of record never guessed). Armed, the campaign
      LEADS the calendar: week 1 queue posts VERBATIM in order one per day, then
      angles 1 to 8 rotate (9 to 11 dark until blanks fill). Case study numbers
      character exact or the draft blocks; numbers pending studies unselectable;
      first person voice law enforced; cover style (black canvas, red and white
      type) is the ONE documented exception to the cream house spec, scoped to book
      cards only; premade cards in content_library/book_campaign/ used before
      generating. Known conflicts (subtitle, author bio figure) flag as card
      warnings. Flag AGENT_BOOK_CAMPAIGN_ENABLED OFF
- [~] Facebook connect page: /connect on the listener (small HTTP thread, needs the
      /data store for the page token), cream V3 single page, Facebook Login for
      Business with exactly the five publish scopes, callback picks the Page and
      resolves the linked IG professional account, page token kv-stored (never
      logged, never rendered, audit scrubbed). Whole surface 404s while
      AGENT_CONNECT_ENABLED is OFF. Publish gates untouched: connecting changes
      nothing about posting
- [~] OVERNIGHT STAGES BUILD (2026-07-03): publish verify 400 fixed with honest alert
      split; connect kv tokens into account resolution (AGENT_CONNECT_TOKENS_ENABLED);
      premade story variants (AGENT_STORY_PREMADE_ENABLED); two tier comment engine
      hardened (conservative tiering, Graph reads, held cards, DMs structurally
      untouchable); monthly review loop (AGENT_MONTHLY_REVIEW_ENABLED: digest + PDF +
      citation gated proposals); trust ladder WIRED (AGENT_TRUST_DRYRUN +
      AGENT_TRUST_AUTOPUBLISH, both OFF; first post never automated, off template
      always cards); one command onboarding (onboard-client + intake_template.md);
      fleet hardening (per account isolation + fleet-status). .env.example now the
      complete flag reference. All new flags OFF
- [~] EVIDENCE AND ARMING PREP (2026-07-03 overnight): monthly-review --dry runs read
      only without the flag; backfill-insights CLI (idempotent, 429 aware, views never
      impressions); scheduler heartbeat + missed run alert (no flag, observability);
      comments first poll flood guard (pre arm backlog never carded); connect queues a
      Social Grade baseline (AGENT_CONNECT_GRADE_ENABLED, OFF); seed-calendar CLI from
      approval evidence only; Opus discovery fixed (pinned ids honored, collections
      paginated, honest empty messaging + exact remediation in opus-check); gbp-check
      readiness probe. All new flags OFF
- [x] Queue triage maintenance (2026-07-04, from Scout's findings): flagless card
      self-expiry (past-due PENDING cards flip EXPIRED, buttons removed, one log line;
      hourly listener sweep + at every daily run; retroactively clears the 22 stale
      cards and 4 dead loopers on first production cycle) and the retry-storm root fix
      (blocked drafts stored + deduped per account/day/type so a failing slot cards
      ONCE; empty-caption drafts block instead of growing buttons). FB verify-400:
      already fixed Jul 3 (Photo node field set); both observed events predate the
      deploy; no Meta-side action needed
- [x] backfill-insights 400 patch (2026-07-04): root cause was the metric list, not
      access. IG MEDIA insights metric is saved not saves (every IG media read 400d);
      FB Page posts use a different insights namespace entirely (every lasso_fb read
      400d) and now read likes/comments/shares via object fields; stories get their
      own metric set with a graceful "story insights expired" skip past 24h; Graph pin
      bumped v21.0 to v23.0 (past the views migration). ONE media-type-aware metric
      builder feeds both the backfill and the daily snapshot; every skip line and
      audit row now carries the Graph error code/subcode/message (token scrubbed) and
      names the missing permission when it is one
- [x] Micro patch (2026-07-04 pm): FB photo-node metrics (bare photo ids resolve
      their owning post via page_story_id, then read reactions/comments/shares; the
      field likes is never requested on any FB node) and Opus collection id
      extraction made shape tolerant (collectionId/string/anything-Id; an
      extracted-vs-returned mismatch warns loudly with the keys seen, never a
      silent zero)
- [x] Service concept set for regen-library: 8 source-verified service cards (ads,
      follow up, lead to member path, sales training, funnel diagnostic, social,
      all in one place, website), archetypes assigned none more than twice; --set
      brand|service|all; sidecars record set; rotation softly alternates brand and
      service days (never overriding the window, pillar rule, or fabrication gate).
      Unsupported lines swapped for sourced wording; the 30 day review concept was
      dropped entirely (no approved source) and replaced with website_done_for_you
- [x] Story first cards (the stranger test): every card depicts a concrete gym world
      scene with a TENSION and a RESOLUTION readable at a glance; meaningful labels
      (LEADS, BOOKED, MEMBERS), generic process labels banned (STEP N, PLAN, GROW,
      LEARN, DISCOVER, LAUNCH, START, FINISH); all 16 concept contexts rewritten as
      Tension/Resolution micro stories modeled on follow_up_problem
- [x] BE CLEAR, NOT CUTE headline law: headlines state plainly what the card is about
      or what LASSO does; two second test in the spec; six slogan headlines rewritten
      to plain statements (three_step_path, posting_cadence, speed_to_lead_concept,
      system_runs_itself, coach_in_your_corner, one_partner); approved voice framings
      stay verbatim. BLAKE BY HAND: rerun regen-library for the story first batch
### Fable 5 review - Tier 1 hardening (2026-07-01, deployed at cd8000b; all four flags
### code default OFF, ARMED in production)
- [x] Idempotent daily drafts + card supersede/expire: one draft per (account, day, type);
      a same-content re-run returns the existing draft (no duplicate card); changed content
      SUPERSEDES the old card (edited in place, buttons removed); a pending card whose day
      passed EXPIRES the same way; stale approve on either = friendly no-op. Flag
      AGENT_IDEMPOTENT_DRAFTS_ENABLED (ARMED in production)
- [x] Ops alerts: one "ECHO ALERT:" line to #echoclaude on hosting failure, empty
      generation, blocked plan, publish failure, store write failure; media_host no longer
      swallows exceptions invisibly; secret env values scrubbed from every alert. Flag
      AGENT_OPS_ALERTS_ENABLED (ARMED in production)
- [x] Publish confirmation: after a real publish, one Graph READ verifies the post and
      replies "LIVE: <permalink>" in the card's thread; a failed verify warns in-thread +
      one ops alert; never re-publishes. Flag AGENT_PUBLISH_CONFIRM_ENABLED (ARMED in production)
- [x] Token watchdog: debug_token expiry check once per daily cycle + CLI
      `python -m agent check-tokens`; alerts within AGENT_TOKEN_WARN_DAYS (default 7);
      token value never printed. Flag AGENT_TOKEN_WATCHDOG_ENABLED (ARMED in production)
- [x] Baseline capture CLI `python -m agent capture-baseline`: manual-only BY DESIGN
      (no flag, never scheduled, nothing in the agent imports it); reads 8 weeks of posting
      history per account, writes dated JSON to /data, prints a summary. Done.

  Env vars to add to .env.example BY HAND (the file is permission-locked for agents):
  ```
  # --- Tier 1 hardening (Fable 5 review). Every flag defaults OFF. ---
  AGENT_IDEMPOTENT_DRAFTS_ENABLED=false  # one draft per (account, day, type); re-runs reuse, changes supersede, stale cards expire
  AGENT_OPS_ALERTS_ENABLED=false         # one "ECHO ALERT:" Slack line per pipeline failure (secrets scrubbed)
  AGENT_PUBLISH_CONFIRM_ENABLED=false    # Graph read-back after a real publish; permalink replied in the card thread
  AGENT_TOKEN_WATCHDOG_ENABLED=false     # daily debug_token expiry check; token value never printed
  AGENT_TOKEN_WARN_DAYS=7                # days before token expiry the watchdog starts alerting
  ```

- [x] Set Gemini key (AGENT_NANO_API_KEY) by hand (proven by the first live card, 2026-07-01)
- [x] Run master ON / publish OFF, watch daily drafts (superseded: publish is now armed)
- [ ] Run the full 30-day loop once (see the 30-day IG plan below)
- [x] Arm publishing: AGENT_PUBLISH_ENABLED ARMED in production (Railway env; code default
      stays false so a fresh checkout is always draft-only)

## Stage 2 — One paying client (hand-picked, forgiving)
### Built, not armed (2026-07-02 buildout; every flag defaults OFF)
- [~] Multi-client foundation: per-account voice_doc / social_proof_doc / library_prefix /
      slack_channel / approvers with global fallback; LASSO = client zero, behavior unchanged.
      No flag (pure config; enforcement wiring of per-account approvers deliberately deferred,
      the global approver gate stays the hard gate)
- [~] Brand voice intake template: brand_voice/BRAND_VOICE_INTAKE.example.md + CLI
      `python -m agent draft-bible --client <key> --intake <path>` writes DRAFT bible +
      social_proof to brand_voice/drafts/<client>/ (manual only, never auto-activated)
- [~] Texted-link intake, client half: tokenized mobile upload page to R2
      (intake/<client>/incoming/), own Railway service (`python -m agent intake-web`,
      R2 only, no /data), allowlist + size caps + rate limit; flag AGENT_INTAKE_ENABLED OFF
- [~] Texted-link intake, processing half: ingest INSIDE the listener (HEIC to JPG,
      orientation, SHA-256 + phash dedupe, moderation hook to review/ + notice, note filed
      as the drafter's .txt sidecar), idempotent manifest, dead-letter + one ops alert;
      same AGENT_INTAKE_ENABLED flag
- [~] Social Grade v1: honest A to F + subscores (consistency, mix, engagement, growth,
      verified proof) + baseline before/after posts per week; rubric docs/SOCIAL_GRADE.md;
      flag AGENT_GRADE_ENABLED OFF
- [x] Meta App Review kit (docs/META_APP_REVIEW_KIT.md, permissions derived from code) +
      Stage 2 onboarding runbook (docs/STAGE2_RUNBOOK.md)
- [~] Knowledge brain: brand_voice/knowledge/ as gated source material (LOCKED / PENDING /
      NOT FOUND and *_pending.md never draft; only USE-marked stats in copy, wording exact;
      03_social_proof_pending.md excluded, proof flows only through social_proof.md);
      flag AGENT_KNOWLEDGE_ENABLED OFF. BLAKE BY HAND: the echo_brain folder was not found
      on disk, so brand_voice/knowledge/ is empty; drop the files in and commit
- [~] Summit campaign: one summit post per week inside the daily cadence (summit day, default
      Tue), drafted ONLY from 04_summit_campaign.md VERIFIED FACTS + APPROVED ANGLES, angle
      rotation (no repeat within 3 weeks), CTA "Claim your seat" +
      https://lassoframework.com/summit, auto-stops after 2026-11-08;
      flag AGENT_SUMMIT_CAMPAIGN_ENABLED OFF
- [~] Podcast pipeline (4 parts, one commit each): (A) RSS feed watcher on the scheduler
      cadence, idempotent episode records (guid keyed), podcast:transcript namespace,
      loud on malformed feed or missing AGENT_PODCAST_FEED_URL; (B) podcast_release
      house-style card (EPISODE <N> / <TITLE> / one-sentence dash-free about line from
      the feed description only) in the daily slot AFTER the book campaign and BEFORE
      pillar rotation, newest episode only (no backlog blast), cards once per episode,
      max one podcast draft per account per day; (C) transcript ingest (CLI
      podcast-transcript --episode N --file|--url, plus auto ingest from the feed) as an
      APPROVED SOURCE scoped per episode, citation podcast_ep<N>, episode-tagged drafts
      only, no transcript text in logs beyond the 120-char CLI preview; (D) episode
      infographics (CLI podcast-cards --episode N [--count 2|3]), hook + support VERBATIM
      from the transcript, citations must resolve at queue AND serve time, spread 1/day
      behind book priority, same house builder with no style overrides, 18 existing
      concepts untouched. Every card held for approval; nothing publishes;
      flag AGENT_PODCAST_ENABLED OFF
- [~] Podcast release templates (B2): four LOCKED navy poster templates
      (podcast_release_a classic poster / _b bold split / _c on air studio /
      _e podcast player), scoped palette exception like the book cover;
      deterministic rotation episode mod 4 over A B C E (131=E, 132=A, 133=B,
      134=C), 3-digit episode slot, 2-line word-boundary title (~40 chars/line),
      dash-free about line, chosen template logged in the audit row
- [~] Podcast memory (2 parts, one commit each): (E) episode learnings (CLI
      podcast-learn --episode N, also rides podcast-cards): 3-7 VERBATIM
      takeaway+quote learnings with podcast_ep<N> citations and pillar taxonomy
      tags into brand_voice/knowledge/podcast/ep<N>_learnings.md + rolling
      INDEX.md; additive only, paraphrases refused, the global gate never reads
      the subfolder; (F) standing claim promotion PROPOSE ONLY: quantitative /
      named-framework learnings card PROPOSED STANDING CLAIM (quote, citation,
      the exact USE line); the approver tap is the ONLY write path into
      02_verified_stats.md, citation attached on landing; book conflicts named,
      blocked, rechecked at tap time. Rides AGENT_PODCAST_ENABLED (OFF)
- [~] B2B swipe file (2 commits): (A) four receipts in 02_verified_stats.md
      ("LASSO B2B Ad Swipe File, July 2026, Blake approved": $16 blended CPL,
      $35,000 caught / $17,000 flagged, twice-monthly reconciliation, 7 dead
      buttons; 500+ referenced not duplicated), gate clears cited receipts and
      still blocks uncited claims; (B) 10 b2b_* concepts in the house library
      (set "b2b", pillars verbatim, stat headlines carry cites), same locked
      builder, 16 house concepts byte untouched (frozen hash),
      regen-library --only/--set b2b per key. Render by hand via regen-library
- [~] Operator hygiene (4 parts, one commit each): (A) regen batch guard: one
      live regen-library run at a time (stale safe lock, dead pid + age auto
      clear, second invocation refuses naming the holder) + end of batch
      summary table (concept, content hash, url) with superseded note on
      re-runs; (B) contact-sheet CLI (--set <name>|--all [--out PATH]):
      self contained HTML review grid from live library sidecars (key, pillar,
      review hints; stat cards get the numeral hint), uploads to
      echo/contact_sheets/<set>_<date>.html, read only against the library;
      (C) podcast-status read only probe (feed reachable, items seen, latest
      parsed, armed watermark, honest next poll forecast per the mod 4
      rotation) + 139 episode first poll proof (only the newest episode ever
      drafts) + backlog guard on transcript auto ingest (newest only past 3
      new episodes in one poll); (D) runway --account <key> --explain: the
      runway math in plain lines on the digest's own shared implementation
      (eligible by name, exclusion reasons, consumption, days). All read only
      or by hand; no daily behavior change anywhere
- [~] House style variant system (3 parts, one commit each): (A) locked canvas
      + layout tokens in the house builder: 4 canvases (cream / navy #1A2340 /
      red / split) and 5 layouts (stat_hero / framework / contrast / checklist
      / poster) under a constant brand grammar (one type family, two fonts
      max, logo lockup, #E03131 single accent, footer) and a shared
      readability bar (high contrast, mobile legible, thumbnail headline);
      no variant fields = the original render path byte for byte; (B)
      deterministic assignment (explicit per concept override wins, else key
      hash over the canvas order) + rotation canvas guard (same canvas never
      serves two days running where an alternative exists, never starving);
      b2b set assigned per brief (4 stat_hero, 2 framework, 1 each checklist /
      contrast, 2 poster across navy / cream / red / split); (C) full test
      coverage incl. adversarial guard + 20 combo render smoke; 16 house
      concepts unchanged
- [~] Platform doctrine + concept set (2 parts, one commit each): (A)
      brand_voice/knowledge/08_platform_2026.md, the PRIMARY POSTING SOURCE
      ("LASSO Platform Overview 2026, Blake approved July 2026"; book stays
      top of the citation hierarchy, this ranks under it above lasso_now):
      positioning lines, six engines + funnel order, verified receipts
      ($16 CPL, $35K+ saved, 71.9% vs 18.5%, 297/141/100+, 8 of 10, 70%+
      close, 25 point audit, 7+ dead buttons), eight named case studies
      (Fit Mamas, Courage, North Naples, Old Glory, Granite Forged, Loup,
      Hoosier, Liminal), all USE lines with platform_2026 anchors, NO
      pricing; (B) 10 platform_* concepts (set "platform") through the
      variant system with per key canvas/layout from the brief, stat
      headlines cited, house 16 + b2b 10 frozen, variance guard green
      across the 36 concept library, regen-library --set platform. Render
      by hand via regen-library
- [~] Grammar V2 + platform ad set (2 parts, one commit each): (A) three V2
      layout tokens (chart: one data visual with big labeled numbers;
      diagram: funnel / hub and spoke / flow arrows, labeled nodes; device:
      phone / browser / profile grid mockup in thin outline), same grammar
      and readability bar, five originals frozen; (B) 10 platform_ads_*
      concepts through the V2 grammar with per key canvas/layout from the
      brief, every concept citing platform_2026, every CTA routing
      quiz.lassoframework.com; house 16 + b2b 10 + platform 10 frozen;
      46 concept library; regen-library --set platform_ads
- [~] Day 30 readiness + doctrine wiring (4 parts, one commit each): (A)
      per account framed Day 30 assembler (report_framing on the account:
      lasso_fb leads with the frequency before/after story + multiplier;
      lasso_ig is engagement and consistency ONLY, frequency confined to an
      internal do not publish appendix, safe default engagement); backfilled
      per post insights + snapshots, top/bottom 3, health read, honest gaps;
      CLI report --account --dry (exact Slack text, watermarked, writes
      nothing); (B) platform doctrine wired as the primary caption source
      (book untouched on top, 08_platform_2026.md second, lasso_now
      fallback), pillar angles resolve doctrine USE lines with citations,
      dormant behind AGENT_KNOWLEDGE_ENABLED, unverifiable angles dropped
      with audited reason, monthly review proposals labeled by source; (C)
      monday-preview read only GO / NO GO preflight (feed forecast, runway,
      quiet token days, heartbeats, pending approvals, flags snapshot),
      zero side effects; (D) Sunday operator report behind
      AGENT_WEEKLY_REPORT_ENABLED (OFF): one card Sundays 6 PM ET, posts /
      approvals / views based engagement on the Day 30 framing rules /
      runway / flags delta / by hand item, honest no data gaps
- [x] Month calendar artifact V2 (2026-07-06, 2 commits): (A) read only
      month assembler from existing state (posts + the same drafts store
      the Slack cards read, seed calendar keys, schedule skip days,
      specials from draft evidence + the Monday podcast expectation); per
      day concept/caption/canvas/layout/status + special, empty days emit
      an open slot never an invented concept; (B) calendar-html CLI V2:
      full post per cell (image or placeholder, complete caption,
      hashtags, canvas/layout chips, status), tap-to-expand modal (full
      image, complete caption, hashtags, canvas/layout, citation source
      line, status; Approve/Edit/Kill display-only previews, no write
      back); uploads to echo/calendars/<account>_<month>.html
- [x] Runway v2 source + gate fix + plan-month (2026-07-06, 3 commits): (A)
      classify_creatives reads BOTH physical library files (old format, style
      exclusions apply) AND all 46 regen library concept definitions from
      regen_library.CONCEPTS; v2 concepts (lasso_v2_*) are never off-style by
      default; runway --explain prints per-set breakdown (house/b2b/platform/
      platform_ads); (B) fabrication gate decoupled from AGENT_KNOWLEDGE_ENABLED:
      _approved_claims uses usable_stats_always() so USE-line stats clear the gate
      regardless of flag; three speed_to_lead_carousel sentences added as USE lines
      to 02_verified_stats.md; adversarial uncited claims still fail; (C) plan-month
      CLI fills open posting days from the eligible pool (14-day rotation window,
      canvas guard, schedule skip, no double-booking); approve-month bulk-approves
      pending plan drafts; first post per account held for the tap; both behind
      AGENT_PLAN_MONTH_ENABLED (OFF). Suite 623 green (7 pre-existing reportlab).

### Opus video factory (2026-07-09 buildout; eight parts, master flag OFF)
Master flag AGENT_OPUS_FACTORY_ENABLED (default OFF). Turns the back catalogue of
finished Opus clips into DRAFTS held for approval; extends the existing Opus
client, never publishes. New CLI: `python -m agent opus-pull [--write]`.
- [~] All-project scan: OpusAPI.list_projects enumerates EVERY project (no
      allowlist, no collection id); opus_factory.scan normalizes each finished
      clip (clip_id, project_id, source_title, title, opus_score, duration_s,
      transcript, download_url); unfinished clips excluded
- [~] Score gate FIRST: AGENT_OPUS_SCORE_FLOOR (default 90) drops a clip before
      any other work; AGENT_OPUS_DURATION_MIN/MAX (15-95s)
- [~] Bucket tagger: podcast-sourced clips (source title = the show,
      AGENT_OPUS_PODCAST_SHOW) tag podcast; others classify from the transcript
      against the 6 buckets + the LASSO theme lexicon; below
      AGENT_OPUS_RELEVANCE_FLOOR (0.65) or no theme => HOLD + ops alert, never
      drafted; transcript only, never invents
- [~] Hook check: the opening ~2s must carry a claim, number, or question, else
      demote to shortlist not draft
- [~] Caption writer: evergreen (back-catalog, never "new episode is live"),
      hook + payoff from the clip's own words, soft CTA to the full episode +
      podcast footer on podcast clips, bucket CTA and no footer otherwise; no
      dashes, never vendor; the fabrication gate stays sole authority (caption
      asserts only what the transcript or the approved facts file already say)
- [~] Dedupe + no-repost ledger: clip_id ledger in the volume kv (opus_drafted_
      / opus_posted_); a clip is drafted at most once; posted clips tracked for
      reporting before/after; a re-run never re-drafts
- [~] Calendar routing: drafted clips fill VIDEO slots on their bucket's cadence
      (podcast Thu, platform Tue/Sat), respecting weekly quotas, no-repeat
      spacing, and a per-week Opus cap AGENT_OPUS_WEEKLY_CAP (default 2); every
      draft PENDING and held; draft-only + trust ladder + first-post gate honored
- [~] opus-pull CLI: dry-run prints the ranked plan (score, bucket, hook line) +
      the held/rejected list with reasons (below floor, off-topic, weak hook,
      dupe) and writes nothing; --write builds the held drafts, posts each to
      the ops channel for the tap + one digest line
- RETIRED: the hand-built "Echo Export" Opus collection step and the
      AGENT_OPUS_PROJECT_IDS pin are no longer required (the factory scans all
      projects). Both vars remain only for the legacy pull-opus ingest poller.

### Opus scan auth guard (2026-07-09; four parts, built after dry-run returned 0)
Root cause: OPUS_API_KEY in the live container was `sk-2vtUf...` (rotated/leaked
key). The scan was swallowing the 401 silently and returning [], which looked like
a clean zero-results run.
- [x] Part 1: OpusScanError — typed exception; _get raises it on non-2xx with
      HTTP status + scrubbed body snippet; scan() re-raises it; opus_pull_cli()
      catches it and prints "AUTH ERROR (HTTP N)" instead of "0 drafted"
- [x] Part 2: call-time env reads — config.opus_api_base() and opus_org_id()
      read from env at each call (no import-time cache); _default_api() uses them
      and logs the key prefix (first 6 chars) for operator confirmation
- [x] Part 3: opus-doctor — new `agent opus-doctor` command (behind
      AGENT_OPUS_FACTORY_ENABLED): key prefix, HTTP status, project count, first
      project raw status. Five-second "is this key working?" preflight.
- [x] Part 4: finished-clip filter widened — _FINISHED_STATUSES accepts "done/
      completed/finished/ready/exported/success/succeeded/published"; exportUrl
      and export_url key aliases added; verbose scan logs raw status of every
      excluded clip so the operator can identify new status values
- Root cause verdict: the key VALUE is stale (sk-2vtUf is the rotated key).
  BLAKE BY HAND: set the current OPUS_API_KEY in Railway env and redeploy.
  Run `agent opus-doctor` after redeploy to confirm auth before running opus-pull.

### Opus route fix (2026-07-09; four parts, built after opus-doctor returned 404)
Second root cause, SEPARATE from the key: the factory scan was built against a
GUESSED endpoint, GET /api/projects?q=mine, which does not exist and returns 404
NotFoundException. A correct key against the wrong route still saw zero clips.
The documented Opus API has NO bulk project-listing endpoint; the legacy
pull-opus poller is the source of truth (it lists via collections).
- [x] Part 1: route contract documented — proven routes are
      GET /api/collections?q=mine (discovery) and GET /api/exportable-clips
      (findByCollectionId / findByProjectId); base URL + auth header were never
      wrong (shared with the legacy poller via OpusAPI._get). No behavior change.
- [x] Part 2: client corrected — OpusAPI.list_collections_detailed() lists via
      the proven /api/collections route; the dead list_projects (/api/projects)
      is removed; opus_factory.scan discovers via collections (all-collection
      scan, no allowlist) plus pinned AGENT_OPUS_PROJECT_IDS (read at call time
      via config.opus_project_ids); clips queried with findByCollectionId; the
      call-time key read and OpusScanError propagation kept. Tests assert the
      scan hits /api/collections + /api/exportable-clips and NEVER /api/projects.
- [x] Part 3: opus-doctor made definitive — calls the corrected /api/collections
      route; 404 => "ENDPOINT WRONG" (route/base URL bad), 401/403 => "AUTH
      WRONG" (key rejected), never collapsed; reports key prefix, resolved base
      URL, HTTP status, collection count, first collection raw status.
- [x] Part 4: finished-clip filter confirmed against the documented
      exportable-clips shape (uriForExport present = finished by contract); test
      flows the documented field set end to end through the corrected scan.
- Routes verdict: the discovery route was wrong. WAS GET /api/projects?q=mine
      (404); NOW GET /api/collections?q=mine. The legacy pull-opus poller was
      the source of the correct contract. Base URL + auth header were correct
      all along. NOTE: this is separate from the stale-key issue above; both
      must be right for opus-pull to see clips. BLAKE BY HAND: after setting the
      current OPUS_API_KEY, run `agent opus-doctor` — it should print HTTP 200
      with a collection count. Podcast clips must live in a collection named
      after the show (AGENT_OPUS_PODCAST_SHOW) for podcast tagging.

### Opus response shape fix (2026-07-09; four parts, built after opus-doctor 200 then crashed)
Third issue in the chain: with the key + route correct, opus-doctor got HTTP 200
and reported 3 collections, then crashed KeyError: 0 on items[0]. The live
/api/collections response wraps its records as an ID-KEYED DICT, not a list:
the actual shape is {"data": {"<collection id>": {...}, ...}}. The old parse did
body.get("data", []) which returned that dict, and items[0] then did a dict-key
lookup for 0.
- [x] Part 1: captured the real shape — _shape_desc(body) logs the top-level
      type and key NAMES only (capped, never values that could carry tokens/PII);
      opus-doctor prints it right after the JSON parse.
- [x] Part 2: single normalizer — normalize_list_response(body) returns a flat
      list of record dicts for any shape: a bare list; a wrapper {data|collections
      |clips|items|results|docs: <list or dict>}; a bare id-keyed dict (the key is
      injected as the record id, existing ids preserved); or empty -> []. Wired
      into list_collections, list_collections_detailed, list_exportable_clips (so
      scan is covered), preserving the legacy list_collections WARNING contract.
- [x] Part 3: opus-doctor robust — consumes the normalizer instead of items[0];
      reports collection count + first collection id/status on any shape; empty
      says so plainly and never indexes.
- [x] Part 4: tests feed the normalizer every shape (list, wrapped list, id-keyed
      dict, bare id-keyed dict, alternate wrappers, empty/None/metadata/non-JSON)
      and assert a correct flat list; scan + doctor both proven to consume it end
      to end at the HTTP layer.
- Shape verdict: the live /api/collections returns an ID-KEYED DICT under "data"
      ({"data": {"<id>": {...}}}), not a list. The route and auth were correct;
      this was purely a response-shape parse bug. Fixed for every consumer via
      one normalizer.

### Opus organize (2026-07-09; three parts, built after opus-doctor showed 0 collections)
Fourth issue: opus-doctor got HTTP 200 but 0 collections. The Opus API docs
confirm collections are created via POST /api/collections and start empty; the
account's clips live in PROJECTS and were never added to a collection, so the
factory scan (collections only) had nothing to read. Routes verified against
help.opus.pro/api-reference/openapi.json BEFORE coding.
- [x] Part 1: OpusAPI collection-management methods — _post() helper;
      create_collection(name) POST /api/collections {collectionName} -> id;
      list_project_clips(project_id) GET /api/exportable-clips q=findByProjectId;
      add_clip_to_collection / add_clips_to_collection POST /api/collection-contents
      {collectionId, contentId} one clip per call (no batch route). Call-time key
      read + OpusScanError raising reused. NOTE: ExportableClipRepresentation has
      NO score field, so clip scores are not available from the API.
- [x] Part 2: `agent opus-organize` (behind AGENT_OPUS_FACTORY_ENABLED). Projects
      from AGENT_OPUS_PROJECT_IDS (no bulk project-listing endpoint exists).
      Dry-run (default) prints the plan, writes nothing; --write creates the
      target collection if absent (name from AGENT_OPUS_PODCAST_SHOW else
      "LASSO Clips") and adds qualifying finished clips, idempotently (reads the
      collection's current contents, skips ids already in). --name overrides.
- [x] Part 3: opus-doctor prints per collection its name + clip count, so after
      organizing we can confirm clips landed.
- Routes verdict (exact): CREATE = POST /api/collections body {"collectionName"}
      -> CollectionDto {collectionId}. ADD = POST /api/collection-contents body
      {"collectionId","contentId"} where contentId is the clip's composite id
      {projectId}.{curationId}; ONE clip per call, no batch. LIST PROJECT CLIPS =
      GET /api/exportable-clips?q=findByProjectId&projectId=. KNOWN GAP: the API
      returns no clip score, so the factory score gate (floor 90) would bench
      every clip; scoring is a separate follow-up before opus-pull is useful.
      BLAKE BY HAND: set OPUS_API_KEY + AGENT_OPUS_PROJECT_IDS (ids from each
      project URL) in Railway, run `agent opus-organize` (dry-run) then
      `--write`, then `agent opus-doctor` to confirm the collection clip count.

### Native clipper, end to end (2026-07-09; all phases, every flag defaults OFF)
Abandoning third-party clip platforms. Durable path: episode video in, 4-5
finished vertical Reels out, entirely inside Echo. Claude selects moments;
mechanical layers cut, caption, frame. Zero external dependency. All flags OFF.

Phase 0 (prereq + scaffold, SHA 81f1546):
- [x] detect_prereqs() — reports HAS_FFMPEG / HAS_FASTER_WHISPER /
      HAS_TRANSCRIBE_API_KEY at call time, never logs a key value.
      ffmpeg 8.1.2 present on this machine; faster-whisper NOT installed.
- [x] clipper_render_enabled() — AGENT_CLIPPER_RENDER_ENABLED, second flag
      under master so selection ships independently of rendering.
- [x] clipper_render_output_dir() — AGENT_CLIPPER_RENDER_DIR.
  BLAKE BY HAND (if ffmpeg absent on Railway): apt-get install ffmpeg

Phase 1 (selection, SHA 0db3223; four parts, flag AGENT_CLIPPER_ENABLED OFF):
- [x] Part 1: episode intake — stage to tenant-scoped R2 key (read-only src).
- [x] Part 2: word-level transcription, cached on R2 key (faster-whisper or
      AGENT_TRANSCRIBE_API_KEY). BLAKE BY HAND: install faster-whisper or set
      AGENT_TRANSCRIBE_API_KEY in Railway.
- [x] Part 3: Claude moment selection (THE CORE) — scored, duration-gated,
      fabrication-gated candidates (hook + rationale each checked separately).
- [x] Part 4: dry-run plan printed; nothing rendered, nothing written.

Phase 2 (render, SHA 261a718; behind AGENT_CLIPPER_RENDER_ENABLED=false):
- [x] Part 5: cut_segment — stream-copy lossless cut of the selected moment.
- [x] Part 6: frame_vertical — 9:16 fill-scale + center crop (video) or
      audiogram (audio: navy canvas, red showwaves); output 1080x1920.
- [x] Part 7: burn_captions — ASS word-by-word karaoke from word timestamps;
      only words in [start_ts, end_ts] included (fabrication-safe); 220px
      margin above the lower-third brand frame.
- [x] Part 8: add_brand_frame — navy lower-third bar (LOWER_H=180px) with
      LASSO logo + red social handle burned via ffmpeg drawbox + drawtext.
      render_clip() is the 4-stage orchestrator (cut → frame → captions → brand).
  BLAKE BY HAND: set AGENT_CLIPPER_RENDER_ENABLED=true when ready to render.

Phase 3 (wire into Echo, SHA 9397de2; held drafts, never auto-post):
- [x] Part 9: save_clip_draft() — creates a PENDING Draft (never auto-publishes
      regardless of trust ladder), posts Slack approval card, saves Slack
      ts/channel for edit-in-place. source_fragments carry source=clipper /
      kind=reel / score / bucket / rationale for audit. Evergreen check flags
      captions that imply recency. Always full-approval.
- [x] Part 10: log_episode_cost() — writes per-episode token cost + transcribe_sec
      + estimated USD to db kv under clipper_cost_{day}_{key}. Visible for the
      $99 SKU margin check.
- clip_episode orchestrator extended: calls render_clip() per accepted moment
      when render flag is armed; clip_episode_cli updated with --render flag.

MORNING REPORT (2026-07-09):
Checkpoint reached: CHECKPOINT 3 (full pipeline shipped dark).
SHAs: Phase 0 = 81f1546, Phase 2 = 261a718, Phase 3 = 9397de2.
  (Phase 1 = 0db3223, built previous session.)

Transcription backend: faster-whisper NOT installed. HAS_FFMPEG=true
  (/opt/homebrew/bin/ffmpeg 8.1.2).

BLAKE BY HAND to arm this pipeline:
  1. Set AGENT_CLIPPER_ENABLED=true in Railway.
  2. Set AGENT_HOSTING_ENABLED=true + R2 credentials (already deployed?).
  3. Set ANTHROPIC_API_KEY in Railway (name only; never print).
  4. Install transcriber: `pip install faster-whisper` in the Railway service,
     OR set AGENT_TRANSCRIBE_API_KEY to an API-backed transcription key.
  5. Set AGENT_CLIPPER_SCORE_FLOOR=80 (or leave default).
  6. Run: `agent clip-episode --source <episode.mp4>` and read the plan.
  7. Confirm the picks look right on a real episode.
  8. Then: set AGENT_CLIPPER_RENDER_ENABLED=true and re-run with --render.
  9. Rendered Reels appear as HELD PENDING drafts in the Slack approval queue.
  10. Approve each Reel individually via the Slack card tap.

Parts that self-skipped: none (all phases built). Rendering is ARMED but
  AGENT_CLIPPER_RENDER_ENABLED defaults OFF — will self-skip silently.

Pipeline ready for a real Gym Marketing Made Simple episode dry-run: YES,
  AFTER steps 1-6 above are done by hand. All flags default OFF; nothing
  runs in production until Blake arms them.

### Episode inbox watcher + Monday nudge (2026-07-10; 5 parts, master flag OFF)
Human workflow: export from Riverside, drop file in the inbox prefix. Echo takes
it from there. Polling watcher inside the existing listener; no new infra.
Master flag AGENT_EPISODE_INBOX_ENABLED (default OFF, all flags OFF).

Part 1 (inbox convention + state, SHA 990d81f):
- [x] Watched prefix AGENT_EPISODE_INBOX_PREFIX (default echo/episode_inbox/<tenant>/).
      Tenant AGENT_EPISODE_INBOX_TENANT (default lasso_episodes).
- [x] Accept mp4/mov/mp3/wav only (extension filter).
- [x] Exactly-once claim: kv marker claimed before processing; re-poll skips
      claimed keys; marker survives restarts (persistent SQLite kv).
- [x] _S3Client.list_prefix() added to media_host — paginated R2 prefix listing.

Part 2 (watcher loop, SHA 990d81f + Phase 2/3 wiring 2026-07-20):
- [x] poll() every AGENT_EPISODE_INBOX_POLL_MINUTES (default 5) in _daily_scheduler.
- [x] Size-stability guard: file must have same size across two consecutive polls
      before it is claimed (guards against in-progress uploads from Riverside).
- [x] Claim + invoke Phase 1 clip selection; post ranked plan to Slack #echoclaude
      as a held plan message. When AGENT_CLIPPER_RENDER_ENABLED is armed, also runs
      Phase 2 (render via clipper_render) and Phase 3 (save_clip_draft per Reel,
      PENDING regardless of trust ladder, Slack approval card per reel). Plan notice
      always posts; drafts only post when render flag is armed. 6 new tests added.
- [x] Exception in processing marks file FAILED, alerts via ops_alerts, loop
      continues uninterrupted.

Part 3 (ops surface, SHA 43a3653):
- [x] inbox_status() returns enabled, prefix, poll interval, last run, counts.
- [x] `agent inbox-status` CLI prints the full status (read only, no side effects).

Part 4 (RSS episode matching, SHA 990d81f):
- [x] _latest_episode_from_db() queries podcast_episodes table for newest episode.
- [x] Plan Slack message header includes episode number, title, publish date.
- [x] _evergreen_check() rejects banned recency phrases in plan output; guard fires
      in header construction (replaces title, alerts via ops_alerts).
- [x] _mark_ep_matched() / _is_ep_matched() track inbox file -> episode linkage.

Part 5 (Monday 9am nudge, SHA 43a3653):
- [x] check_monday_nudge(): Monday gate, nudge-time gate (America/New_York), recency
      window (AGENT_EPISODE_NUDGE_WINDOW_DAYS, default 2 days), episode match check.
- [x] Idempotent: nudge key ep_guid + date stored in kv; second call same day is
      a no-op (status: already_sent).
- [x] Already-matched episode is silent (no nudge).
- [x] Stale episode outside window is silent.
- [x] Nudge slot added to _daily_scheduler (never crashes loop).

45 tests, all green. Suite 1473 passed (2026-07-20).

BLAKE BY HAND to arm this pipeline:
  Phase 1 only (selection plan to Slack):
  1. Set AGENT_EPISODE_INBOX_ENABLED=true in Railway.
  2. Ensure AGENT_HOSTING_ENABLED=true + R2 credentials set (for list_prefix).
  3. Ensure AGENT_CLIPPER_ENABLED=true (Phase 1 clip selection).
  4. Set AGENT_PODCAST_FEED_URL (for RSS episode matching in the plan header).
  5. Set AGENT_EPISODE_INBOX_PREFIX if the default is wrong
     (default: echo/episode_inbox/lasso_episodes/).
  6. Optional: AGENT_EPISODE_INBOX_POLL_MINUTES (default 5),
     AGENT_EPISODE_NUDGE_TIME (default 09:00),
     AGENT_EPISODE_NUDGE_WINDOW_DAYS (default 2).
  7. Export a finished episode from Riverside, drop it in the inbox prefix.
  8. Within one poll cycle, a ranked clip plan appears in #echoclaude.

  Phase 2+3 (render + held drafts): after Phase 1 is confirmed working:
  9. Set AGENT_CLIPPER_RENDER_ENABLED=true in Railway.
  10. Each new episode file dropped in the inbox now produces rendered Reels
      AND Slack approval cards (Approve / Edit / Skip). Drafts are PENDING,
      never auto-published. Approve each Reel individually in #echoclaude.

### Stage 2 foundation (2026-07-09 buildout; ten parts, every flag defaults OFF)
- [x] 7-day cadence: POSTING_SKIP_DAYS default is now empty (no skip days); Saturday
      is a posting day by default. AGENT_POSTING_SKIP_DAYS env override re-enables
      any custom skip list. With AGENT_CATEGORY_ROTATION on, August plans 31/31.
- [~] 14-day review cycle: AGENT_REVIEW_WINDOW_DAYS (default 14) windows the
      day30 assembler (now the cycle report; 30-day window keeps the DAY 30
      title); pre-Echo cadence baseline comparison stays on the fixed 30-day
      basis; creative refresh ask once per account per cycle behind
      AGENT_REVIEW_CYCLE_ENABLED (OFF), wired into run_daily
- [~] intake-create: one intake JSON scaffolds a tenant under
      brand_voice/tenants/<key>/ (voice.md, avatar.md, verified_facts.md USE
      lines feeding the fabrication gate, tenant.json with approver + sender
      phones + media lanes + trust 0 + quota fields); blocks loud on missing
      fields, all-or-nothing; AGENT_INTAKE_ENABLED
- [~] Trust ladder wired to tenants: level_for_tenant reads only the named
      tenant's record, fail-safe to FULL_APPROVAL; a new tenant can never
      auto-publish (level 0 + double gate + first-post gate, adversarially locked)
- [~] Media inbox core: provider-agnostic queue behind AGENT_MEDIA_INBOX_ENABLED;
      sender phone -> tenant (never guessed; unknown = HELD + one masked alert
      per sender per day), idempotent by sha256, texted sentence = caption note
- [~] Ingest worker: perceptual dedupe per tenant, consent + autotag hooks,
      thumbnail, tenant-scoped R2 keys via media_host isolation; CAPTION GATE:
      no sentence = not filed + one auto-ask; attach_caption releases
- [~] GHL adapter: Ed25519 X-GHL-Signature verified BEFORE parsing; photos
      captured immediately (carrier URLs expire); video MIME auto-replies with
      the tenant's tokenized upload link; AGENT_GHL_INTAKE_ENABLED
- [~] WhatsApp adapter: X-Hub-Signature-256 (HMAC) verified before parsing,
      16MB WABA ceiling (refused, never truncated), same queue;
      AGENT_WHATSAPP_INTAKE_ENABLED. DO NOT ARM before the
      whatsapp_business_messaging App Review addition is granted
- [~] Upload quotas + tenant token watchdog: per-tenant storage cap enforced at
      the upload endpoint (413 over a MEASURED total; unmeasurable or legacy
      never blocks), monthly recreate budget kv-counted per month; the token
      watchdog flags upload-lane tenants with no AGENT_INTAKE_TOKEN_<KEY> set
- [~] Per-gym tenant brain: brains/<tenant>.md append-only learning events
      (approve_streak / edit_diff / deny_reason / kill); killed concepts
      excluded from THAT tenant's rotation only; style rules + deny reasons
      fold into prompts THROUGH the fabrication gate (the brain never adds
      facts); AGENT_TENANT_BRAIN_ENABLED
- [~] Immediate draft-on-upload (AGENT_DRAFT_ON_UPLOAD, default OFF): the instant a
      gym's media is INGESTED (agent/intake_ingest._process_client), Echo drafts ONE
      approval card per newly filed asset via runner.draft_for_new_upload — no waiting
      for the once-daily draw. Reuses draft_post + _post_and_save so every gate is
      identical (approval, publish-off, fabrication, portal-vs-Slack, per-gym autonomy).
      Resolves the generation account (get_account(tenant) or <tenant>_ig); a gym with
      no account or no voice doc is SKIPPED with one ops alert (no fabrication, media
      preserved). Fixes Dale/CrossFit-ENG "nothing in the queue": before this, ingest
      filed media but never drafted, and the daily draw only served active_accounts()
      (LASSO only) — client/portal gyms (active=False or unregistered) were never drafted.
      NOTE: a portal gym still needs a registry Account (<tenant>_ig) with a voice doc
      before it can draft; the skip-alert names exactly what to add.
- [~] Automatic social-intake forward (AGENT_SOCIAL_INTAKE_SYNC, default OFF):
      agent/social_intake_reader.sync_unrouted() maps EVERY un-routed
      echo_social_intake row into Echo (voice/proof docs via onboard_from_social +
      approved client_sources) and marks the row routed. Per-gym error isolation
      (one bad/no-account gym never blocks the batch); a base with no registry
      Account is skipped with one ops alert. CLI: `python -m agent social-intake-sync
      (--all | --base <slug>)`; listener polls every AGENT_SOCIAL_INTAKE_SYNC_MINUTES
      (default 15) when armed. This is the durable fix for the CrossFit ENG miss
      (intake captured 2026-08-09 with echo_forwarded=false / not_routed, never
      forwarded). Tests: tests/test_social_intake_sync.py (incl. a 100-gym scale case).
      KNOWN SCALE LIMIT: each gym still needs a registry Account (<base>_ig) in
      accounts.py; the sync skips-with-alert until it exists (see next-step note).
- [~] CrossFit and HYROX ENG onboarded to Echo (2026-08-12): eng_ig + eng_fb in
      accounts.py (inactive, client-gym convention); brand_voice/eng/lasso_voice.md +
      social_proof.md built from ENG's OWN verbatim intake (HYROX included per Blake);
      4 SB7 posts generated and stored PENDING (ready to approve). Publish still needs
      AGENT_ENG_IG_TOKEN/ID + AGENT_ENG_FB_TOKEN/PAGE_ID + AGENT_PUBLISH_ENABLED (by hand).
- [~] StoryBrand SB7 caption engine + edit-learning feedback loop: the
      StoryBrandGenerator (agent/drafter.py) writes SB7 captions via Claude
      (AGENT_SB7_ENABLED, model AGENT_SB7_MODEL default Haiku 4.5) drawing ONLY
      from the voice doc + client note. With the tenant brain armed it folds THIS
      gym's learned preferences into the prompt: past before/after edits
      (tenant_brain.edit_examples) + deny reasons (prompt_notes), so every edit
      moves the next caption toward the approver's taste. BOTH sides of an edit
      example pass the fabrication gate; per-tenant file keying = no cross-gym
      leak; any LLM failure falls back to TemplateGenerator (a card always gets a
      caption). Both flags default OFF = zero behavior change. Also: approving an
      EXPIRED draft now publishes immediately (past scheduled_for ignored);
      non-approve actions on expired drafts still no-op.
- [x] July 16-31 replanned for both accounts (plan-month --from 2026-07-16,
      days 1-15 structurally untouched), 16 pending drafts per account held
      for approval in the LOCAL sandbox store. BLAKE BY HAND: run the same two
      plan-month commands on the deployed listener with AGENT_CATEGORY_ROTATION
      + AGENT_PLAN_MONTH_ENABLED armed in Railway env (the sandbox store is not
      the deployed store)

### Still open
- [ ] Client / team approval flow via the portal
- [ ] Prove the voice holds for someone who is not Blake
- [ ] Prove the 30-day refresh lands for a real client
- [~] Document intake: client sends a PDF (texts + email); Echo extracts the text,
      splits it into N post ideas (deterministic, no LLM), drafts an infographic card
      per idea, holds ALL for approval. No-fabrication gate applies (PDF is raw material,
      never approved fact). Built in sandbox: agent/doc_intake.py + CLI intake-doc,
      reuses creative_studio + media_host; flag AGENT_DOC_INTAKE_ENABLED OFF. pypdf added.
      This is the seed of client intake. Flag AGENT_DOC_INTAKE_ENABLED, default OFF.

## Stage 3 — Productize ($99/mo)
- [ ] Launch as the $99 Social Media add-on ($99 ad clients, $199 non-ad)
- [ ] Template intake, approval, calendar, monthly review
- [ ] DAM with member-photo consent tracking
- [ ] Creative runway card (days of content left) + text-back alert
- [ ] Reporting on Meta Graph views (not impressions), daily snapshots cached
- [ ] Portal exposes creative library (read); portal hosts reporting dashboard (write)
- [ ] Onboard client by client; $99 starts stacking

## Stage 4 — Scale automation (near-zero-touch)
- [ ] Claude Agent SDK agent behind approval-gate hooks
- [ ] Per-account trust ladder climbing (routine auto-publish inside approved calendars)
- [ ] Multi-account oversight; one human owns exceptions + monthly review
- [ ] Per-gym agent memory + audit log prove reliability
- [ ] Nightly brain loop armed (read brain + performance, propose, never auto-edit voice)

## Roadmap / next builds (scoped, not started)
- [x] Daily Stories posting (warm-audience signal) -> built, moved into Stage 1 as [~]
- [x] Caption SEO baked into the drafter -> built into the content brain, in Stage 1 as [~]

## Stubs (documented, intentionally not built yet)
- [ ] Comment handling (Tier 1 auto-safe / Tier 2 surface / no auto DMs)
- [ ] 30-day creative refresh loop (the product)
- [ ] Portal creative-library read; portal reporting write; nightly brain read

---

## Full build spec — the organic system (see BUILD_SPEC.md)
The complete scope Echo grows into. Everything plugs onto the proven Stage 1 core.
- Ingestion: texted short link primary (MMS/portal fallback); event-driven queue to
  idempotent Railway worker; HEIC/MOV convert; SHA-256 + pHash dedupe; AV + moderation;
  thumbnails; dead-letter + backoff.
- DAM: asset metadata + AI tags with confidence; human review queue; member-photo
  CONSENT tracking (release required to publish); usage tracking prevents reposts.
- Creative runway: days of content left = unused approved assets / posts per day; one
  glanceable green/amber/red card with a zero-date; below threshold the agent texts a request.
- Agent (Claude Agent SDK): model routing (Opus judgment, Sonnet copy, Haiku classify);
  gated act-tools; per-gym memory; PreToolUse approval hooks; decision audit log; SB7 skills.
- Platform + reporting: Supabase RLS + Clerk org isolation; idempotency; rate-limit-aware
  GHL + Meta clients; reporting on views not impressions; white-label dashboard + branded PDF.
- Google Business Profile: first-class publishing channel (local posts) alongside IG + FB;
  own draft-only branch, own post variant (one image, <=1500 chars, CTA button, no hashtags).
  Full scope + access gate in BUILD_SPEC.md Addendum A.

---

## Portal Handoff Package (2026-07-18)

`docs/portal_handoff/` — 9 markdown specs + 2 HTML reference files.

Portal CC reads this to build: intake wizard, media upload hand-off, calendar display, reporting display.

**Live today:** POST /intake/<token> (JSON portal path), GET/POST /u/<token>, GET /healthz. Approval via Slack (approve/edit/skip only; no deny or kill action in approvals.py today).

**PLANNED:** Calendar API, portal-native approval API (behind future AGENT_PORTAL_APPROVALS), reporting API.

---

## Open risks / watch items
- Repo divergence: deployed repo has commits from other agents (ruvnet, Manus); the
  reference sandbox may differ. Code ships as a behavior-described Claude Code prompt,
  never a wholesale push.
- Rotate secrets by hand: Meta app secret + long-lived token, Slack tokens, client Page tokens.
- Verify one caption line ("That difference is your revenue") was Blake's own note edit.
- DECISION (resolved) — brand palette: canonical = V3 Navy #121E3C / Red #FF0000 / Sky #5EB9E6 /
  Cream #FAF6F0. Locked in creative_studio.py; BUILD_SPEC.md updated; #0F1B33 draft superseded.
- DECISION — publish path: spec routes through GHL Social Planner V2; Echo publishes direct via
  Meta Graph today. Lean: keep direct Meta for LASSO now, move to GHL at 100+ client scale.

## Reporting to wire in Stage 3 (per account, per 30-day cycle)
From the Meta Graph API, on VIEWS not impressions (Meta migrated April 2025; pre/post are not
comparable). Engagement (rate + raw), saves, sends/shares, likes, comments, reach, views,
follower growth (net + rate), posting frequency before vs after Echo, top 3 / bottom 3 posts,
health read (growing / flat / declining).

## 30-day IG plan (pre-publish gate)
Diagnosis: the account is high-output, low-reach (1,308 posts, ~1,169 followers). Fix is reach
and follow-through, not volume. Plan biases to Reels + carousels with save/send CTAs and caption
SEO, one post/day rotating the 5 pillars. Full plan: `lasso_ig_30day_plan.md`. Refine with real
per-post data once Stage 3 reporting is live.
