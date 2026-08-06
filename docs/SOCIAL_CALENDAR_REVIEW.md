# Social Media Calendar — Build Review Package (for Fable 5)

Two connected pieces: (A) the Echo engine that produces 30 posts through the real
approval pipeline, and (B) the portal Content Calendar that displays the month.

## A. Echo demo calendar (the engine)
Repo `lassoframework/lasso-echo`, on `main` (merged from `demo-calendar-build`).

- `agent/demo_calendar_queue.py` — `DEMO_POSTS` (30 dated rows), `build_caption()`,
  hooks `build_demo_calendar_draft` / `build_demo_calendar_story_draft`,
  `create_from_manifest()`, `upload_images()` (R2), `served_day` lock so the same
  item serves lasso_ig + lasso_fb once.
- `agent/demo_calendar_render.py` — PIL compositor -> 30 feed (1080^2) + 6 story
  (1080x1920) cards; asserts NO digits and NO dashes on-image.
- `agent/config.py` — `demo_calendar_enabled()` -> `AGENT_DEMO_CALENDAR_ENABLED`, default OFF.
- `agent/drafter.py` — `Draft.force_approval` field (default False).
- `agent/runner.py` — feed hook after the welcome drip, story hook after the welcome
  story; `_post_and_save` skips BOTH auto-publish paths when `force_approval` is set.
- `agent/__main__.py` — `AGENT_DEMO_CALENDAR_ON_START` seed, `demo-calendar` CLI, status line.
- `tests/test_demo_calendar_queue.py` — 19 tests.

Content rules: every hook/body verbatim from `brand_voice/lasso_now.md`,
`brand_voice/knowledge/08_platform_2026.md`, or `brand_voice/knowledge/02_verified_stats.md`.
5-pillar rotation, approved CTAs + hashtags, no fabrication, no dashes.

Gates: flag OFF by default (hooks return None); card-only via `force_approval` (always
cards for approve/deny even with `AGENT_AUTO_APPROVE_ENABLED` on — only strengthens the
gate); publishing still needs `AGENT_PUBLISH_ENABLED`; stories need `AGENT_STORIES_ENABLED`.

Verification: full suite 1965 passed; independent audit A+ / no gaps (0/60 caption
sentences failed source-trace, no new publish path, draft IDs cannot collide with book/welcome).

Not done (last mile): render -> host to R2 -> seed manifest for the daily Slack approval cards.

## B. Portal Content Calendar (the view)
Repo `LASSO-FRAMEWORK/lasso-ops-portal`, live on `main` via PR #389 (a parallel session
shipped it; the duplicate #388 was closed).

- Table `public.content_calendar` (`id, gym_id, account, post_date, pillar, format,
  caption, image_url, status, created_at`), migration `0282_content_calendar.sql`,
  RLS staff-read (owner/executive/coach via Clerk JWT), service-role writes.
- UI `src/app/command-center/calendar/page.tsx` + `ContentCalendar.tsx`; logic in
  `src/lib/content-calendar/{logic,read}.ts`; nav in `src/lib/nav-registry.ts`.
  Month grid + list, filters, status chips, detail drawer. Read-only v1 (approve/deny
  stays in Echo/Slack).
- URL `ops.lassoframework.com/command-center/calendar` (owner/exec).
- Seed applied: 30 LASSO rows (`gym_id='lasso'`, 6 stories, Aug 6 -> Sep 4, status
  `pending`) = the exact demo month, plus 11 sample rows for a real gym from #389.

## What to double-check
1. No fabrication — all 30 captions trace verbatim to the three approved source files.
   Stat posts: 08-15 (71.9% vs 18.5%), 08-20 ($16 CPL, $35K/$17K), 08-25 (Fit Mamas
   $19K->$47K, $99->$167), 08-30 (Courage $84K MRR, 30->80+), 09-04 (North Naples, Old Glory).
2. No dashes (em/en/hyphen) in any caption.
3. Card-only gate: `runner._post_and_save` skips auto-publish for `force_approval` on
   BOTH the `auto_approve_enabled` branch and the trust-autopublish branch.
4. Flag-off inertness: both demo hooks return None when `AGENT_DEMO_CALENDAR_ENABLED` off.
5. Portal RLS: staff-read only; no service key client-side.
6. Date overlap (design call): Aug 12/15/19/22/26 the book queue wins the Echo slot, so
   those demo posts are not served on Echo those days (portal calendar still shows all 30).
7. Two calendars: table-backed `/calendar` (#389) vs older `/social-calendar` holding state.

## Source-of-truth files to audit against
`brand_voice/lasso_now.md`, `brand_voice/knowledge/08_platform_2026.md`,
`brand_voice/knowledge/02_verified_stats.md`, `agent/demo_calendar_queue.py`,
`agent/runner.py`, `agent/drafter.py`.
