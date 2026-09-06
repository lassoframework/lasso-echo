# ECHO A+ REBUILD — HANDOFF

Rewritten 2026-09-06, replacing the 943bebd version which predates the whole day.
Resume from this file. Everything is production-verified unless marked otherwise.

---

## 1. STATE OF THE FLEET — READ THIS FIRST

**THE PUBLISHER IS DISARMED FLEET-WIDE.** `AGENT_CALENDAR_AUTOPUBLISH=false`, deploy
`58f079fa`, verified `calendar_autopublish_enabled = False` inside the running container.

**19 gyms are dark. This is an INCIDENT STATE, not a resting state.** Every day dark is its
own client problem. Do not leave it longer than the criteria require, and do not arm early
to shorten it.

### Re-arm criteria: 4 of 5 done

| # | Criterion | State |
|---|---|---|
| 1 | Content ledger backfilled from all 375 published rows | DONE — 238 keys, 17 historical duplicate captions collapsed (itself confirmation of the defect) |
| 2 | Guard caught a seeded duplicate in test AND refused in a dry run against real production content | DONE — 28 real published rows replayed: guard would refuse all 28, 0 ledger misses, a new caption still passes |
| 3 | Planner distinct-caption assertion live | DONE — SOCIAL `ed0dafe`, `ECHO_DAY_SHAPE_ASSERT` default ON |
| 4 | A kv write failure REFUSES the publish, verified not assumed | DONE — structural test asserts the except branch skips and says so, plus a test pinning stamp-before-network-call |
| 5 | **AUDITOR independently confirms 1-4 by querying production** | **THE ONLY GATE. NOT STARTED.** |

**AUD-201 applies to criterion 5.** If AUDITOR confirms any criterion by reading a report
instead of querying production, AUD-201 is still open and **the re-arm does not happen.**

### Re-arm order — doctrine, not preference

**LASSO ALONE FOR 24 HOURS FIRST.** We are client zero; prove it on us. Report the LASSO
day as a real result: how many posts fired, how many the content guard refused, how many the
day-shape assertion blocked, and whether anything published twice. **Only if that report is
clean does the fleet arm, all at once, including Reverb.** Reverb arms WITH the fleet, never
early, even though its own fix is ready.

---

## 2. TODAY'S INCIDENTS AND THEIR OUTCOMES

### AUD-001 — I deleted 140 real posts and restored every one
My dedupe keyed on `(gym, account, post_date, time_slot, format)` and never compared content.
ENG runs 2 posts a day and both can sit in ONE time_slot, separated only by `slot_index`,
which I had dismissed as unusable. Of the 140 deleted: **131 differed by image, 123 by
caption, 75 by slot_index, ZERO were byte-identical.** All 140 restored to pending.

**The correct duplicate key: same slot AND same caption AND same image.** Honest fleet-wide
count is **~18**, not the 441 in the original census and not the 155 of my first correction.
AGENT SOCIAL caught this by comparing deleted rows against their survivors. I did not.

### The publisher double-post — a live client incident
Tough Temple: six publishes in 40 seconds on Instagram, including a re-publish of a day that
went out 19 hours earlier. Fleet-wide: **84 extra publishes, 81 caption groups, 10 gyms**
(eng 23, lasso 19, pierce 15, topfuel 7, zanshin 6, nine7 4, gritx 4, toughtemple 3,
hillcountry 2, chateau 1). Each pair carries a DIFFERENT `late_post_id`, so Zernio accepted
each as a separate post and they are live.

**Root cause is the PLANNER writing one caption into two slots, NOT a missing publisher
key.** Every pair: same gym, same account, same post_date, same caption, different
`time_slot`. A row-id key could never have seen it.

Guard: `published_content_<account>_<platform>_<verbatim_hash(caption)>`, **stamped BEFORE
the network call**. If the process dies between stamp and call the row never publishes again
— a post that silently fails is recoverable, a post that goes out twice is not. Fails closed
on a kv error. Scoped to the account so IG+FB cross-posting still works. Stories exempt.
Row-aware, so the same row re-entering is the row-claim's business.

**THE 84 STAY LIVE AND UNTOUCHED. NO CLIENT HAS BEEN CONTACTED. Blake's ruling: we do not
contact anyone. To a person scrolling it reads as a repost; a proactive apology creates a
problem that does not exist for them. If a client raises it, Blake answers.** CSV of all 84
with both timestamps and both `late_post_id`s is in #echoclaude.

### RLS — anon could TRUNCATE 16 tables
Measured: `relrowsecurity=false`, 0 policies, and `anon` holding
DELETE/INSERT/UPDATE/TRUNCATE/SELECT on all 16. The anon key ships in the browser bundle.

**Enabling RLS alone would NOT have closed it: TRUNCATE is a table-level command and is not
filtered by RLS.** Migration revokes grants FIRST, enables RLS second as defence in depth.
No permissive policies, because Echo and the portal both use service-role keys that bypass
RLS, and `supabaseForUser` (the only anon+Clerk RLS client) has **no importers**.

**PR #588 MERGED.** Migration `0312_rls_sixteen_open_tables.sql`. NOT applied by hand —
production applies through the deploy path and `public.schema_migrations`; out-of-band
application desyncs that ledger and breaks every deploy.

**STILL TO DO: run `0312_..._verify.sql` against production after the deploy lands, paste
the output, and have AUDITOR confirm independently.**

**DO NOT ROTATE THE ANON KEY.** It is public by design. With grants revoked and RLS
deny-by-default, a public anon key is the intended state. Rotating breaks the portal for
every user and buys nothing.

### Tamper check — CLEAN, AND BOUNDED. NEVER ROUND THIS UP.
Zero `anon` requests against the 16 tables, on every window sampled. Aug 8 showed 333,012
PostgREST writes with an unresolved role, which resolved to the `sb_secret_` server key —
new-format keys are not JWTs and carry no role claim, so **"role = none" must never be read
as "role = anon"**.

**Retention floor: 2026-07-10.** I sampled the oldest retained day, a midpoint, and the live
window — not every day. **Anything before 2026-07-10 is UNKNOWABLE, not clean.**

### Stuck LASSO post `d4574f62` — resolved without human eyes
Zernio held a PUBLISHED record for the caption, scheduled 2026-08-28T11:31:17Z. It was live
the entire 8 days it sat in `publishing`. Marked published, stale marker cleared. Zernio also
holds a second published copy from 09-01, so it is also one of the 84.

### Reverb — not a publishing bug
All 93 rows `pending`, **zero ever approved**, 15 already aged past their date. Dean could
not approve because his portal read "93 drafted, none to approve" — the key split fixed this
morning. `publish_flag` OFF, creds NOT SET.
**Ruling: the 15 re-dates are approved. `publish_flag` STAYS OFF — Reverb arms with the
fleet after LASSO's clean 24 hours. Dean's message is Blake's to send, mine to draft.**

---

## 3. DOCTRINE ADDED TODAY — ALL BINDING

1. **Prevent-only guards default ON. Cause-anything guards default OFF.** Flags default OFF
   because a misfiring flag can cause a publish. A guard that can only PREVENT a write earns
   the exception: worst case is a calendar stops refreshing until a human looks; worst case
   of OFF is duplicate content shipping to a client, which happened today to ten gyms.
2. **NO AGENT MAY BLOCK, SUPERSEDE, OR CLOSE ANYTHING ON A SUMMARY. Field-level evidence or
   it does not count. This includes summaries from Blake.** Three times today a summary was
   wrong and the data was right: my AUD-001 key (SOCIAL caught it), Blake's census and
   AUD-006 staleness (I caught them), SOCIAL's DS-2 blocker (I caught it). None were caught
   by reading code or trusting a report.
3. **Before any NEEDS BLAKE, check whether a system already holds the answer.** Human eyes
   are the last resort. The stuck LASSO post was answered by Zernio in one query.
4. **Never hard delete a `content_calendar` row.** Soft delete: `status='deleted'`
   (`'superseded'` is NOT in the CHECK constraint) with a `reject_reason` naming the run.
5. **Migration numbers are claimed by an empty placeholder committed and pushed to origin
   BEFORE the migration is written. First push wins.** Another session took 0311 mid-write
   today; with many live sessions this will collide for real and desync `schema_migrations`.
6. **Alerts must RE-FIRE while a condition persists.** "Alerted once, muted forever" has
   shipped twice (`stuck_publishing`, the held-grade dedupe). Never ship a third.
7. **A test that asserts observed output instead of intended behaviour is worse than no
   test.** Five found today, all rewritten from spec — including two fixture helpers that
   defaulted every row to the caption `"hello"`.

---

## 4. OPEN QUEUE, IN ORDER

1. **AUDITOR on all five re-arm criteria**, independent production queries only (AUD-201).
2. **Migration-number lock** (placeholder-first convention, doctrine 5).
3. **Reverb: 15 re-dates** (approved, reversible). **Draft Dean's reply to #echoclaude** —
   short, plain, no jargon, no apology theatre: his calendar is visible now, he can approve,
   dates were shifted so nothing expired. **Do NOT mention the key split, the incident, or
   anything internal.** No em dashes, no en dashes, no hyphens.
4. **Day-shape alert**: on assertion failure alert #echoclaude the same hour naming gym,
   date and which field collided; include that gym's remaining runway; **RE-FIRE daily while
   blocked**; escalate to SOCIAL at 3 consecutive days as a content defect.
5. **Fill-rate across EVERY `posts_per_day=2` gym.** Known: ENG 24/24, Pierce 6/24,
   Chateau 6/16. **Pierce and Chateau pay for 2x and get ~1.25x. NEW CRITICAL: silent
   under-delivery** — worse than a double-post because nobody notices and it runs forever.
   Suspected cause is media library depth, not logic; if so the fix is a client ask, not
   code. Add fill rate to the metrics contract and the portal report, beside posting
   frequency before/after.
6. **Tough Temple to SOCIAL.** Twelve straight denials across both accounts, every
   `reject_reason` NULL, every caption on one opening frame, `path_to_join` 0/10. On the
   list for three turns, still unstarted.
7. **DEEP BRAIN TRACK — SPEC NOT RECEIVED.** Blake refers to a full spec "in the prior
   message" including a CTA ban and an avatar-word ban to ship as prevent-only. **That spec
   never arrived in this session's context. Ask Blake to re-send it before starting.** Do
   not reconstruct it from memory.
8. **FIXER classifier — DETAIL NOT RECEIVED.** Blake reports Dean's ticket returned
   `question_not_groundable` and parked. Rule to implement: **an unclassifiable ticket routes
   to a human immediately and never parks.** The ticket evidence did not arrive in context;
   confirm before closing.
9. **Deny streak as a content alarm.** Three consecutive denials on one gym escalates to
   SOCIAL. Tough Temple signalled with the deny button for a week and nothing read it. **Log
   as its own defect: a detection gap, not a content gap.**

---

## 5. BRANCHES AND PRs

| Branch / PR | State |
|---|---|
| `track/1-identity` — PR #46 | open, slot idempotency + this file |
| `track/2-reliability` — `25efebf` | pushed, publish content guard + inert-net fixes |
| `track/3-content-quality` — PR #48, `ed0dafe` | open, day-shape assert, media swap, ask lane |
| `track/4-zernio-apify` — PR #47 | open, metrics daily, GBP retry, expiry |
| `track/6-portal-ui` — portal PR #586 | open, window.open lock, verified connected badge |
| portal `fix/late-sync-swallowed-upsert-error` — PR #587 | open, the 7th inert net |
| portal `fix/rls-16-tables` — PR #588 | **MERGED** |

Merge order: 1 identity, 2 reliability, 4 integrations, 5 metrics, 3 content, 6 portal.
Worktrees: `/Users/blakeruff/echo-t{1,2,3,4,5}-*` and `/Users/blakeruff/portal-t6-ui`,
`/Users/blakeruff/portal-rls`. `~/lasso-echo-work` is the shared root — another session uses
it; work from throwaway detached checkouts of `origin/main` and say so every time.
Archived pruned branches: `origin/archive/2026-08-28/*`.

Suite: **5376 on track/2-reliability**, 5442 on track/3. Baseline was 5355 this morning.

---

## 6. STILL BLAKE'S ALONE

- **Deleting anything from a live client account.**
- **Any client communication**, including anything about the 84.

Everything else reversible is authorized without asking.

---

## 7. CONTENT GRADE: D — THIS IS A MERGE GATE

SOCIAL failed the fleet on content and Blake ratified it as a gate, not a note. LASSO's own
account is worst: one post says the same sentence three times. Six consecutive Tough Temple
captions carry zero call to action. Top Fuel opens six of six with "You've tried...". Avatar
rule is clean everywhere. **A content D cannot pass into the final report as a minor.**
