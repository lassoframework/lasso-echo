# CADENCE_SPEC — Echo 2x/day cadence with portal toggle

Blake's build brief 2026-08-27, plus his 11 approved design decisions and 3 additions.
This file is the audit ground truth: the independent audit diffs THIS spec against the
build, section by section, BUILT / PARTIAL / MISSING with file:line evidence.

## Approved decisions (Blake: "Go on all 11, no overrules")

D1. 2x cadence applies to the content_calendar staging paths: client_month_run (client
    gyms) and real_month_planner (LASSO dogfood). Legacy Slack daily draw stays 1/day.
D2. posts_per_day is PER ACCOUNT (1 or 2, default 1). Per-platform is backlog (existing
    autonomy wiring has no platform dimension).
D3. Storage mirrors autonomy: portal upserts echo_gym_settings.posts_per_day
    (migration 0291) + best-effort POST ${echo}/portal/<token>/cadence; Echo stamps kv
    portal_cadence_<base>; Echo readers check kv -> Supabase store -> default 1.
D4. Global kill switch ECHO_CADENCE_2X_ENABLED (default OFF; Blake's exact name, noted
    deviation from AGENT_* convention). OFF = byte-for-byte today; portal toggle still
    SAVES the preference, Echo ignores it. Portal UI ships dark behind
    PORTAL_CADENCE_TOGGLE_ENABLED (default OFF).
D5. Second slot = distinct concept. Client path: next-best approved source/category under
    the same media + banned-word guards; never same caption or image twice in one day.
    LASSO path: slot 2 category = next pillar in _FALLBACK_ORDER after the day's pick.
D6. Slot times: slot 1 = 07:30, slot 2 = 18:30, stories midday. Override env
    AGENT_CADENCE_SLOT_TIMES="HH:MM,HH:MM". Deterministic ordinal slot assignment on 2x
    days (replaces id-hash, which can collide two feeds onto one slot).
D7. Toggle triggers replan of UNAPPROVED FUTURE days only (from tomorrow); approved /
    published rows never touched (preserve_and_prune semantics). Missed POST reconciles
    at the next nightly rebuild from the stored setting.
D8. Recreate budget stays 15/month. ADDITION 1: surface per-gym deny volume in the
    monthly rollup (existing surface, no new build) so the 2x watch item has a number.
D9. Mix counter BUG FIX: tally counts DRAWN concepts (calendar row `pillar`), not
    filename/weekday-derived categories. ADDITION 2: audit must verify the tally on
    BOTH 1x and 2x paths; final report calls this out as the bug fix it is.
D10. Day cells render both posts at 2x (portal SocialCalendar + Echo portal calendar
    HTML). Metrics roll up per post and per day.
D11. Approval flow, trust ladder, publish flags UNTOUCHED. Both slots land pending.

ADDITION 3 (done criteria): after push, suite must pass in the Railway container with
/opt/venv/bin/python. Local green is not final green.

## Echo changes (repo lasso-echo)

E1. agent/config.py
    - cadence_2x_enabled(): ECHO_CADENCE_2X_ENABLED, default OFF, _truthy.
    - cadence_slot_times(): parse AGENT_CADENCE_SLOT_TIMES, default ("07:30","18:30");
      invalid value falls back to default (never raises).
    - __main__ flags printout lists the new flag.
E2. agent/db.py
    - set_posts_per_day(base, n): kv key portal_cadence_<base>, value "1"|"2";
      rejects anything else (no-op + False).
    - posts_per_day(base): kv first; missing/invalid -> None (caller falls back).
E3. Cadence resolution helper (single source of truth used by both planners):
    resolve_posts_per_day(base, store=None): flag OFF -> 1 always. Flag ON: kv ->
    store.gym_posts_per_day(base) (echo_gym_settings.posts_per_day via Supabase) -> 1.
E4. agent/portal_social.py + agent/intake_web.py
    - POST /portal/<token>/cadence {"posts_per_day": 1|2} -> stamps kv, returns
      {ok, posts_per_day, replanned}: 400 on bad value. Mirrors handle_autonomy shape.
    - Handler triggers the replan (E6) when flag armed; replanned=false when flag OFF
      (setting saved, no behavior).
E5. Planners
    - client_month_run: at posts_per_day==2, each day emits TWO feed+story pairs with
      DISTINCT concept/source/image/caption (uniqueness enforced in-code, not hoped);
      day with only one usable distinct concept emits ONE pair (never a dup), logged.
      Slot ordinal stamped on the row (slot_index 0|1) for publish-time times.
    - real_month_planner: at 2x for the account, second PlanSlot pair per date with
      category = next _FALLBACK_ORDER pillar after the day's category; grader + caps
      still apply to the whole month.
E6. Replan on toggle: unapproved future days only (>= tomorrow, gym-local), approved /
    published rows preserved. Reuses the existing month build + preserve_and_prune.
E7. agent/calendar_autopublish.py slot_time_for_row: rows carrying slot_index get
    deterministic times from cadence_slot_times(); rows without keep today's hash path
    byte-for-byte.
E8. Mix counter fix (BUG FIX, both paths): grade_card._grade_inputs (and
    monthly_report.refresh_section pillar dim) count pillar from the DRAWN concept:
    calendar row `pillar` when available, filename inference only as fallback for
    legacy posts with no calendar row. Correct at 1x and at 2x (two rows/day = two
    tallies).
E9. Deny volume surfacing: per-gym deny count for the month (recreate-budget usage,
    e.g. "Denies: 4 of 15") added to the existing monthly rollup surface
    (monthly_retro digest line), no new report built.
E10. Calendar rendering: Echo portal calendar day cell renders N feed posts.
E11. Migration: content_calendar.slot_index int null (additive), applied to Supabase
    project ooqcvmcjspeltuuhcvlh + committed under migrations/.

## Portal changes (repo lasso-ops-portal)

P1. supabase/migrations/0291: echo_gym_settings.posts_per_day int not null default 1
    check (posts_per_day in (1,2)) + verify file.
P2. src/lib/server/flags.ts: portalCadenceToggleOn() -> PORTAL_CADENCE_TOGGLE_ENABLED.
P3. src/lib/echo/echo-cadence.ts: getGymPostsPerDay / setGymPostsPerDay (upsert,
    onConflict gym_id) mirroring echo-autonomy.ts.
P4. src/lib/echo/social-connect.ts: postEchoCadence(gymId, postsPerDay) best-effort,
    same shape as postEchoAutonomy (echoLive honest).
P5. src/app/api/gyms/[gymId]/social/cadence/route.ts: GET/POST, canReadGym auth,
    validates 1|2, portal DB write first, Echo best-effort second.
P6. src/app/my/PostCadenceToggle.tsx: segmented 1x/2x control, EchoAutonomyToggle
    pattern (self-fetch, optimistic, revert on error), rendered on the Social page
    ONLY when portalCadenceToggleOn() (boolean passed from server).
P7. Day cells: SocialCalendar renders all posts for a day (verify N-safe; fix if it
    assumes one feed).
P8. tsc + build + tests green. Ships dark.

## Acceptance (audit checks each line with evidence)

A1. ECHO_CADENCE_2X_ENABLED absent/false: full suite green AND zero behavior change
    (planners, publisher, endpoints byte-for-byte; test proves flag-off no-op).
A2. Flag ON + posts_per_day=2: client month emits 2 distinct pairs/day; uniqueness
    (caption AND image differ within a day) proven by test.
A3. Flag ON + posts_per_day=1 (or unset): identical to today.
A4. Toggle POST replans only unapproved future days; approved rows byte-identical
    after replan (test pins it).
A5. Slot times: 2x rows publish-due at 07:30 / 18:30 local; 1x rows unchanged hash.
A6. Mix tally correct on 1x and 2x paths (test: staged pillar counted once per row,
    not per filename family / weekday).
A7. Deny count line present in monthly rollup with real number.
A8. Approval gates untouched: both slots pending; no publish path change (grep +
    test evidence).
A9. Portal: toggle GET/POST round trip; auth enforced; flag-off = control absent.
A10. Suites green: Echo local python3, Echo Railway /opt/venv/bin/python, portal
    typecheck + build + tests.
A11. Both repos pushed to origin/main; SHAs reported.
