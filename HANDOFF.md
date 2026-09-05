# ECHO A+ REBUILD — HANDOFF

Written 2026-09-05. Resume from this file. Everything here is production-verified unless
marked UNVERIFIED.

## 1. THE CORRECTED GROUND-TRUTH QUERY

Blake's original census keyed duplicates on `(gym_id, account, post_date)`. **That key is
retired.** It counts a legitimate second post on a 2x-per-day account as a duplicate, and
acting on it deleted 140 real posts (all restored). Use this key instead:

    duplicate  ==  same slot AND same content
               ==  (gym_id, account, post_date, time_slot, format, slot_index,
                    normalised caption, image_url)

Run the census as: pull every `content_calendar` row with `post_date >= current_date` and
`status in (pending, approved, publishing, published, coach_review)`, page at 1000, group
on the key above, and count groups larger than one.

### Current numbers (2026-09-05, after the restore)

| metric | value | note |
|---|---|---|
| dup_slots, corrected key | **~18** fleet-wide | the honest number |
| dup_slots, Blake's old coarse key | 322 | retired, counts legitimate 2x/day |
| odd_key_gyms | **0** | column-vs-token AGREE 13, SPLIT 0, LEGACY 2 |
| conn_mismatch (IG+FB) | 5 | 30 incl. GBP, of which 25 are a platform STRING split |
| stuck_rows | 6 | 1 publishing (8d), 2 failed GBP, 3 stale approved |
| gym_social_metrics_daily | **336 rows / 16 gyms** | was 0 this morning |
| post_metrics newest published_at | 2026-09-03 | see AUD-006, withdrawn |
| reply_queue draft+skipped | 5 | 10 rows total, newest 2026-08-31 |

## 2. GAP TABLE

| ID | Sev | Finding | Status |
|---|---|---|---|
| AUD-001 | major | Calendar duplicate leak | Belt built (PR #46). **Not in production.** UNVERIFIED |
| AUD-102 | critical | Belt had no `event_id` exemption; killed 4 Bolton event rows | FIXED, rows restored |
| AUD-103 | major | Belt live-set listed dead `draft`, omitted `coach_review` | FIXED |
| AUD-104 | minor | Rows with null `time_slot` never deduped | RULED: under-block is the safe direction |
| **AUD-140** | **critical** | **My dedupe deleted 140 real posts. 131 differed by image, 123 by caption, 0 identical** | **FIXED, all 140 restored, key rewritten** |
| AUD-005 | critical | `echo_social_connections` vs `gym_social_accounts` | FIXED (PR #47), migration UNAPPLIED |
| AUD-112 | major | `googlebusiness` vs `google_business` string split, 25 of 30 mismatches | VERIFIED |
| AUD-113 | major | Demo Fitness + LASSO share `late_account_id` | OPEN |
| AUD-007/D1 | critical | `gym_social_metrics_daily` empty | FIXED, 336 rows, flag OFF |
| AUD-101 | critical | **7th inert net**: `late-sync.ts` logs ok while writing nothing | FIXED (portal #587) |
| AUD-003 | major | Failed GBP rows never retried (one 16 days) | FIXED, flag OFF |
| C14 | major | Bare `ZernioError` unwrapped | FIXED |
| C15 | critical | 13 GBP tokens "expiring" | **FALSE ALARM.** Rolling 1h access tokens |
| AUD-006 | major | post_metrics stale | **WITHDRAWN by Blake.** `published_at` is post time |
| AUD-106 | major | Stale-publishing marker never cleared, row muted 8 days | FIXED |
| AUD-107 | major | Claim release swallowed; lasso_ig claim 4 days stale | FIXED |
| AUD-108 | major | `inbox_alerts` burned the daily stamp on a failed send | FIXED |
| AUD-109 | major | The two Railway services have DISJOINT `/data` volumes | OPEN |
| AUD-008 | critical | Reply engine ingests nothing for 13 of 15 gyms (mapping) | Watchdog FIXED, mapping OPEN |
| AUD-105 | major | REPLY NEEDED never reads `echo_reply_queue` | FIXED |
| B1/B2 | major | Reverb folders + "93 posts drafted" | FIXED, live-verified, Dean DM'd |
| B3 | critical | Sunnyside connect | FIXED, live-verified, Michael DM'd |
| B5 | major | Repeat photos | Reporting FIXED; repeats need a ruling |
| B6/P-11 | major | Photo swap was a MISSING action, not a mispriced one | FIXED, flag OFF |
| B7 | major | Story surface lives in another repo | NOT CLOSED, needs a ruling |
| **B8** | major | ENG "1 post/day, no stories" | **NOT REPRODUCIBLE.** ENG publishes 2-6/day incl. 18 stories, all with `late_post_id` |
| B9 | major | GBP "nothing planned" said which cause for neither | FIXED |
| B12 | major | Denied rows stayed visible | FIXED, guard ON |
| C6 | major | Grades dropping; `caption_craft` is 0 on any copy violation | NOT CLOSED, needs a ruling |
| C7 | major | Gym stuck at B forever | FIXED, flag OFF |
| P-01 | critical | `window.open` lock built AND proven by planting a violation | FIXED |
| P-02 | critical | Lock cannot BLOCK a merge | **NEEDS BLAKE: `gh auth refresh -h github.com -s workflow`** |
| P-03 | critical | Connected badge was unfalsifiable, no age bound | FIXED |
| P-05 | major | District H repeat tickets: support status existed, nothing rendered it | FIXED |
| P-13 | major | As-of stamp policy | NEEDS RULING, proceeding with numbers + amber warning |
| **AUD-201** | **critical** | **AUDITOR verified a destructive op from the acting agent's summary** | **OPEN** |
| **AUD-202** | **critical** | **The suite defends defects** | **OPEN**, 3 caught so far |

## 3. THE FOUR REAL STOPS

1. **RLS on 16 tables** — write policies, commit, DO NOT apply. NOT STARTED.
2. **Hard DELETE of client data** — none has occurred. Soft delete only.
3. **Secrets / flag arming in Railway** — every new flag ships OFF. See section 5.
4. **The stuck LASSO Instagram post** — row `d4574f62`, 2026-08-28, 9 days.
   Caption opens "A plan, not a notebook." POSTED to #echoclaude, unanswered.

Also posted and unanswered: the `gh workflow` scope command, the D3 rung ruling, the
as-of stamp ruling.

## 4. STANDING AUTHORIZATION (verbatim, still in force)

Proceed WITHOUT asking on: any code change, refactor, migration file, test, lint rule or CI
change; creating/pushing/rebasing/pruning any branch or worktree; opening PRs and merging
your own after AUDITOR passes them; any schema migration that ADDS a table, column, index,
constraint or policy; backfilling any table that is currently EMPTY or additive-only; any
read call to Zernio, Apify, Supabase or Slack; any Apify run inside the cost ceiling;
retiring the stale-key resolver once the migration is verified; fixing anything AUDITOR
finds including items not in the original spec; deciding your own sequencing inside a track.

THE SOFT-DELETE UNLOCK: never hard delete a `content_calendar` row. You may ALWAYS set
`status='deleted'` (NOT 'superseded' -- it is not in the CHECK constraint) with a populated
`reject_reason` naming the run. Fully reversible, needs no approval.

Never block a track on Blake's reply. Post the item to #echoclaude and keep building.

## 5. FLAGS SHIPPED (all OFF unless noted)

- `AGENT_SLOT_DEDUPE` — **defaults ON** (prevents damage, escape hatch `false`)
- `ECHO_MEDIA_SWAP_FREE`, `ECHO_GRADE_STUCK_ESCALATION`, `AGENT_REPLY_ENGINE_WATCH`,
  `AGENT_APIFY_SOCIAL_BACKFILL`, the metrics-daily and GBP-retry flags — all OFF
- `ECHO_PORTAL_SHOW_REJECTED` — escape hatch; the guard defaults ON

## 6. AUD-201 — AUDITOR MUST NOT TRUST A SUMMARY

AGENT SOCIAL caught the 140-post deletion. AUDITOR did not, because it accepted the acting
agent's own report of a destructive operation. Standing rule from here:

- AUDITOR may NEVER verify a destructive or state-changing operation by reading the acting
  agent's report.
- It must independently query the BEFORE and AFTER row sets and diff them FIELD BY FIELD.
- For any operation that removes, supersedes or rewrites rows, compare each affected row
  against its survivor on EVERY content field, never on the key the acting agent chose.
  **Choosing the wrong key IS the failure mode.**
- Does not close until AUDITOR catches a SEEDED destructive change in a test.

## 7. AUD-202 — THE SUITE DEFENDS DEFECTS

Caught so far (all rewritten from spec):
1. `test_slot_idempotency.py::test_an_in_batch_duplicate_is_dropped` — staged two rows with
   DIFFERENT captions and asserted one was dropped.
2. `test_slot_idempotency.py::test_a_slot_already_live_is_not_staged_again` — same shape.
3. `test_zernio_publish.py::test_sweep_alerts_once_past_threshold_and_never_reverts` —
   asserted `kv == "alerted"` and that a stuck row NEVER re-alerts.

Still to sweep: every test asserting `content_calendar` row counts, dedupe keys, slot
collapsing, or "expected N rows after planning". For each, ask whether it asserts the
INTENDED behaviour or merely whatever the code did the day it was written. Rewrite from
spec or delete. **Blocks A+ sign-off.**

## 8. BRANCHES

`track/1-identity` (PR #46), `track/2-reliability` (pushed), `track/3-content-quality`
(PR #48), `track/4-zernio-apify` (PR #47), `track/5-metrics`, `track/6-portal-ui`
(portal PR #586), portal `fix/late-sync-swallowed-upsert-error` (PR #587).

Merge order: 1 identity, 2 reliability, 4 integrations, 5 metrics, 3 content, 6 portal.
Six worktrees under `/Users/blakeruff/echo-t*` and `/Users/blakeruff/portal-t6-ui`.
`~/lasso-echo-work` is the shared root; another session uses it. Work from throwaway
detached checkouts of `origin/main` when it is occupied, and say so every time.

Archived branches (pruned worktrees, recoverable): `origin/archive/2026-08-28/*`.

## 9. CONTENT GRADE: D — THIS IS A MERGE GATE

AGENT SOCIAL failed the fleet on content and Blake ratified it as a gate, not a note.
LASSO's own account is worst: one post says the same sentence three times. Six consecutive
Tough Temple captions carry zero call to action. Top Fuel opens six of six with
"You've tried...". Line-break shape is unenforced (toughtemple 14/14 captions have none).
Avatar rule is clean everywhere. A content D cannot pass into the final report as a minor.
