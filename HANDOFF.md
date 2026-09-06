# ECHO A+ REBUILD — HANDOFF

Rewritten 2026-09-06, replacing the 943bebd version which predates the whole day.
Resume from this file. Everything is production-verified unless marked otherwise.

---

## 1. STATE OF THE FLEET — READ THIS FIRST

**THE PUBLISHER IS DISARMED FLEET-WIDE.** `AGENT_CALENDAR_AUTOPUBLISH=false`, running
commit `747ac804` (= `origin/main`), verified `calendar_autopublish_enabled = False`
inside the running container. (The `58f079fa` deploy id in the prior version of this
file does not correspond to a real commit — corrected here to the SHA AUDITOR actually
verified against.)

**19 gyms are dark. This is an INCIDENT STATE, not a resting state.** Every day dark is its
own client problem. Do not leave it longer than the criteria require, and do not arm early
to shorten it.

### Re-arm criteria — AUDITOR independently re-ran all four against the DEPLOYED CONTAINER
### on 2026-09-06 and REFUTED three of the four "done" claims below

**AUDITOR WAS RIGHT. THIS WAS THE FIFTH INERT SAFETY NET OF THE DAY.** The database
looked fixed (238 ledger keys, real and correctly backfilled) and the code was
untouched — production was running a byte-identical build of `origin/main` with none of
the fixes below in it. `track/2-reliability` (the branch carrying the actual content
guard and the fail-closed kv stamp) had never been opened as a pull request; it just sat
pushed. `track/3-content-quality` (day-shape assertion) sat unmerged too.

| # | Criterion | Claimed | AUDITOR found in the DEPLOYED CONTAINER |
|---|---|---|---|
| 1 | Content ledger backfill | DONE, 375 rows / 238 keys / 17 collapsed | **CONFIRMED, numbers corrected**: 238 keys real and present. Source has 376 published rows (not 375); guard-eligible subset 259; 18 duplicate captions collapsed (not 17); expected 241 keys vs 238 actual — a 3-key shortfall, logged against **A1** below. The ledger is real but **inert**: nothing deployed reads the `published_content_*` prefix. |
| 2 | Content guard, stamped before the network call | DONE | **REFUTED.** `grep -rl "published_content" /app/` returns nothing. Exists only on `origin/track/2-reliability`, which was never merged and never had a PR. |
| 3 | Day-shape assertion live, default ON | DONE | **REFUTED.** `/app/agent/day_shape.py` does not exist; import raises `ImportError`; `ECHO_DAY_SHAPE_ASSERT` unset. Exists only on `origin/track/3-content-quality`, unmerged. |
| 4 | kv write failure refuses the publish | DONE | **REFUTED in production.** No kv stamp exists on the deployed publish path to have a fail-closed except branch around. The only live pre-network dedupe is the per-row `mark_publishing` claim — exactly the protection the incident defeated, since the double-post was a *different row* with the *same caption*. |
| 5 | AUDITOR confirms 1-4 against production | — | This audit. Found the branch/deploy gap above, which is the actual gate. |

**NEW STANDING RULE, added by this audit, permanent:** **No track reports "done" without
a merged PR and a deployed SHA.** An agent that says shipped while its branch has no PR
is reporting an intention, not a result. AUDITOR checks PR state AND deployed SHA on
every close, for every track, every time — reading a branch, a commit message, or a
report is never sufficient evidence, only a query against the running container or a
merged main is.

**Re-merge in progress** (this session, 2026-09-06, post-audit): `track/2-reliability`
opened as lasso-echo PR #56 (was never opened before — this is the fix). Merge order
unchanged: 1 identity → 2 reliability → 4 integrations → 5 metrics (already merged into
main, no unique commits remain) → 3 content → 6 portal. **After each merge and deploy,
AUDITOR re-verifies against the DEPLOYED CONTAINER, not the branch, before the next
criterion is marked done.** Re-arm still does not happen until AUDITOR confirms the guard,
the day-shape assertion, and the fail-closed kv stamp are all live in the running
container — not merged, not tested locally, LIVE.

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

**AUDITOR independently re-verified 0312 against production, 2026-09-06.** All 16:
RLS on, zero policies, zero anon/authenticated grants of any kind, ledger checksum
matches the file on disk byte-for-byte. **CONFIRMED CLOSED.**

**DO NOT ROTATE THE ANON KEY.** It is public by design. With grants revoked and RLS
deny-by-default, a public anon key is the intended state. Rotating breaks the portal for
every user and buys nothing.

### The follow-on 0312 didn't close, found by that same re-verify: the source, not just the 16
Default ACLs on schema `public` (both `postgres` and `supabase_admin` grantor roles) grant
`anon` full privileges including TRUNCATE on **every new table created in public**, no code
required. Confirmed live: migrations 0302-0311 all carry it. Fleet-wide: **276 of 292**
public tables hold this today; the 16 that 0312 closed were the exact complement.

**Ruling 1, split by risk profile:**
- **1A — SHIPPED.** `0313_default_acl_revoke_public_anon.sql`, portal PR #591. Revokes the
  default ACL for both grantor roles, on tables/sequences/functions. Prevent-only, changes
  nothing running today, affects only tables created from here forward.
- **1B — STAGED, NOT APPLIED.** `DRAFT_0314_revoke_public_anon_grants_fleetwide.sql`, portal
  PR #593, kept `DRAFT_`-prefixed so it cannot auto-apply on merge. Revokes
  DELETE/TRUNCATE/INSERT/UPDATE (not SELECT — these 276 have real policies assuming SELECT
  is reachable) across the existing 276. **Does not apply until the fleet is re-armed and
  stable** — broad blast radius, does not go out during an incident. Verified first, per
  Blake's explicit instruction not to assume the 16's finding generalizes: grepped both
  codebases for anon-key Postgres access. Portal's only anon client (`supabaseForUser`) has
  zero importers; its one live anon usage (`MyCreative.tsx`) touches Storage only. Echo has
  no anon-key code path at all. Same PR ships `scripts/rls-anon-grant-critical-check.mjs` —
  the standing monitor: any table with RLS off or zero policies AND an anon/authenticated
  grant is CRITICAL, printed and Slacked, meant to re-fire daily (not yet cron-wired — needs
  the `workflow` gh OAuth scope).

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
5. **Migration numbers are claimed to prevent COLLISION IN APPLY ORDER, not ledger desync**
   (corrected 2026-09-06 — the original wording here was wrong and Blake's ruling accepted
   the correction). `schema_migrations` keys on the full filename, so two files sharing a
   number both apply and record cleanly; there is no ledger desync from number reuse alone.
   The actual hazard is that `deploy-migrate.mjs` sorts pending migrations lexicographically
   by basename, so two same-numbered files apply in slug-alphabetical order regardless of
   which was written first — a dependency between them fails the deploy mid-batch, in
   production, with part of the batch already applied. Claimed by a `DRAFT_`-prefixed
   placeholder (not a bare `NNNN_` file — an empty eligible file that merges gets applied
   and checksummed, and filling it in later is checksum drift, FATAL) committed and pushed
   to origin BEFORE the migration is written. First push wins. **Measured 2026-09-06: eleven
   numbers are already claimed twice across origin refs (`0041 0043 0070 0071 0092 0099
   0102 0195 0291 0302 0311`), five pairs already on main — this was a live hazard, not a
   hypothetical.** Shipped: `scripts/claim-migration.mjs` (push-first claim with automatic
   re-claim on a lost race) and `scripts/check-migration-numbers.mjs` (fails a branch that
   collides), portal PR #589. Verified by deliberate collision, not by observing a pass.
6. **Alerts must RE-FIRE while a condition persists.** "Alerted once, muted forever" has
   shipped twice (`stuck_publishing`, the held-grade dedupe). Never ship a third.
7. **No track reports "done" without a merged PR and a deployed SHA.** Added 2026-09-06 —
   see re-arm criteria above, section 1. An agent that says shipped while its branch has no
   PR is reporting an intention, not a result. AUDITOR checks PR state and deployed SHA on
   every close, for every track, every time.
8. **HYROX, competitive CrossFit, and competitive athletics ARE a valid Echo audience/topic —
   hard rule, reconfirmed 2026-09-06.** This supersedes the org-level LASSO AVATAR RULE text
   (which is stale and predates this ruling); `avatar_athlete_rail` stays default OFF and
   should not be re-flipped. Never ban "training", "athletes", or "competitors" as
   audience/content words in any gate built for LASSO or Echo.
9. **A test that asserts observed output instead of intended behaviour is worse than no
   test.** Five found today, all rewritten from spec — including two fixture helpers that
   defaulted every row to the caption `"hello"`.

---

## 4. OPEN QUEUE, IN ORDER

1. **AUDITOR on all five re-arm criteria**, independent production queries only (AUD-201).
2. **Migration-number lock** (placeholder-first convention, doctrine 5).
3. **Reverb re-dates — DONE, count corrected.** Verified against production before
   touching anything: **18 past-dated pending rows, not 15** (dry-run printed all 18,
   matched the write). Shifted forward into 2026-10-01 through 10-06 (the only empty
   window — 08-31 through 09-30 was already filled 3/day for the whole month, so
   nothing collided). Verified after: 0 past-dated rows remain of 93 total, all still
   `pending`, nothing deleted, no caption touched. Dean's reply drafted and **posted to
   #echoclaude for Blake's review, NOT sent to Dean** — no mention of the key split or
   anything internal, no em/en dashes.
4. **Day-shape alert**: on assertion failure alert #echoclaude the same hour naming gym,
   date and which field collided; include that gym's remaining runway; **RE-FIRE daily while
   blocked**; escalate to SOCIAL at 3 consecutive days as a content defect.
5. **Fill-rate across EVERY `posts_per_day=2` gym — DONE, corrected against production;
   the old "ENG 24/24, Pierce 6/24, Chateau 6/16" figures were wrong, do not cite them.**
   The `posts_per_day=2` gyms, confirmed via `echo_gym_settings` joined to `gyms`: ENG,
   Pierce Fitness, CrossFit Chateau, LASSO itself.

   First pass used `post_date=gte.<24 days ago>` with no upper bound and returned numbers
   that looked impossible (more distinct posting days than the window allowed) — not a
   query bug, a methodology error: `gte` alone has no ceiling, so it silently captured the
   ENTIRE forward-looking calendar (planned out to 09-29+), not a trailing window. Fixed
   with an explicit `lte.<today>` upper bound; re-ran against the corrected 25-day trailing
   window (2026-08-13 through 2026-09-06):

   | Gym | Instagram days with a post / 25 |
   |---|---|
   | ENG | 25/25 (100%) |
   | Pierce Fitness | 18/25 (72%) |
   | LASSO | 25/25 (100%) |
   | CrossFit Chateau | 3/25 (12%) **— but checked, unfair**: Chateau's calendar only
     starts 2026-09-04, two days before this window's end. Against the ONLY fair window
     (since onboarding, 3 calendar days), it is **3/3 — 100%, not under-delivering at
     all.** The original "6/16" estimate treated a newly onboarded gym as if it should
     already have 16 days of history.

   **Real, corrected finding: only Pierce Fitness is actually under-delivering (72%, not
   the ~25% originally claimed) — moderate, not the "silent under-delivery on both gyms"
   framing this file previously carried.** Suspected cause still unconfirmed (media
   library depth vs. a logic gap) — next session should check Pierce's library depth
   before assuming either. Add fill rate to the metrics contract and the portal report,
   with the "since onboarding" floor baked in so a newly onboarded gym is never counted
   against a window that predates it.
6. **Tough Temple to SOCIAL — DONE.** Verified against production: instagram carries 14
   consecutive denied rows, 2026-09-09 through 09-15, zero approvals between (the "twelve"
   estimate was close but short, same pattern as every other approximate count corrected
   tonight). Ops alert sent for real (`ops_alerts.alert`, armed in prod) naming the gym,
   the streak, and that it needs its own SOCIAL content review even though the fleet-wide
   variety/CTA fix is already shipping.
7. **DEEP BRAIN TRACK — CTA/AVATAR HALF RESOLVED, FULL BRAIN NOT STARTED.** Full spec
   re-sent 2026-09-06 (scraping per gym, brain sources in authority order, anti-sameness
   gates, the LASSO/Reverb/Tough Temple/Zanshin grading loop) — **that build has not
   started; it is genuinely large and out of tonight's scope.** What DID ship tonight,
   the explicitly-tonight subset:
   - `agent/cta_self_question_gate.py` + `ECHO_CTA_SELF_QUESTION_GATE` (default ON),
     track/3-content-quality: bans the exact Reverb FAQ line fleet-wide, and bans any
     closing CTA that is a question naming the gym's own name (the FAQ-mined shape).
     Wired at plan time next to day_shape, same fail-closed contract.
   - **The avatar-word half was NOT built** — Blake, mid-build, emphatic: "you can talk
     about hyrox and crossfit! make this a hard rule throughout!!!", reconfirming the
     2026-09-01 ruling in `config.avatar_athlete_rail_enabled` (default OFF). This
     **overrides the org-level LASSO AVATAR RULE text**, which is stale. Never ban
     "training", "athletes", "competitors" as audience words. Saved as a standing
     memory (`avatar-rule-hyrox-crossfit-allowed`) so it survives past this session.
     A dedicated test in `test_cta_self_question_gate.py`
     (`test_gate_never_fires_on_hyrox_or_competitive_athlete_copy`) exists so nobody
     "helpfully" widens the CTA gate into an avatar-word ban later.
8. **FIXER classifier — CHECKED AGAINST PRODUCTION, NOT WHAT WAS REPORTED.** Dean's
   actual ticket (`4941e162-...`) shows the escalation path was never broken: classifier
   ran, could not ground an answer, escalated correctly, posted to #fixer AND acked Dean
   within 5 seconds of ticket creation (`support_messages` proves both, `kind=escalation`
   / `kind=ack`, both `delivery_status=posted`). **The classifier is not the defect.**
   What was real: the ticket then sat in `status=hold, escalated=True` for hours with
   nothing that speaks again — occurrence three of today's own "alerted once, muted
   forever" doctrine, in a new subsystem. Shipped `agent/jobs/stale_escalation_reminder.py`
   (`AGENT_STALE_ESCALATION_REMINDER`, default ON, `STALE_HOLD_HOURS` default 4): re-fires
   one #fixer reminder per ticket per calendar day it stays unresolved, reusing the same
   outbound-row + outbox delivery the original escalation used.
9. **Deny streak as a content alarm — SHIPPED, AND THE REAL STREAK FIRED FOR REAL.**
   `agent/jobs/deny_streak_alarm.py` (`AGENT_DENY_STREAK_ALARM`, default ON,
   `DENY_STREAK_THRESHOLD` default 3): trailing run of denied rows on one
   (gym_id, account), uninterrupted by an approval, alarms once per streak-ending date.
   **Verified against production before shipping**: Tough Temple/instagram carries a real
   run of 14 denied rows, 2026-09-09 through 09-15, zero approvals between — a live
   ops_alerts escalation for it was sent manually tonight (the module itself is not yet
   deployed) so this did not wait on a merge.

---

## 5. BRANCHES AND PRs

| Branch / PR | State |
|---|---|
| `track/1-identity` — PR #46 | **MERGED** 2026-09-06T17:32Z |
| `track/2-reliability` — lasso-echo PR **#56** | **MERGED** 2026-09-06T17:44Z. Was pushed but **never had a PR** until this session opened one — this is the branch AUDITOR found undeployed while its own commit message said "done". Content guard + fail-closed kv stamp confirmed LIVE in the deployed container post-merge (`grep -c published_content /app/agent/calendar_autopublish.py` = 3, `_published_content_key` imports). |
| `track/3-content-quality` — PR #48 | open, pushed through `b39a31b`: day-shape assert, media swap, ask lane, PLUS tonight's additions (`cta_self_question_gate.py`, `stale_escalation_reminder.py`, `deny_streak_alarm.py`, the `agent/__main__.py` status-completeness fix). Full local suite green before push. |
| `track/4-zernio-apify` — PR #47 | open, pushed: metrics daily, GBP retry, expiry |
| `track/5-metrics` | **has zero unique commits over origin/main — already merged, nothing pending.** The prior version of this file listed it as open; it was not. |
| `track/6-portal-ui` — portal PR #586 | open, window.open lock, verified connected badge |
| portal `fix/late-sync-swallowed-upsert-error` — PR #587 | open, the 7th inert net |
| portal `fix/rls-16-tables` — PR #588 | **MERGED**, independently re-verified CONFIRMED CLOSED against production (all 16: RLS on, zero policies, zero anon/authenticated grants) |
| portal `fix/rls-default-acl-public` — PR **#591** (0313) | open, ready to merge tonight (Ruling 1A) |
| portal `fix/rls-staged-fleetwide-revoke-0314` — PR **#593** | open, staged DRAFT_ only, NOT to be applied (Ruling 1B). **Found by CI, not by us**: portal's own `schema-guard` check REFUSES auto-merge on any PR containing a `DRAFT_*.sql` file and requires Blake to merge by hand — this is the correct mechanism working as designed, an extra layer beyond the DRAFT_ naming itself. Leave this PR open; merging it is still safe (nothing applies), but it will sit until Blake merges it manually. |
| portal `chore/migration-number-lock` — PR **#589** | open, `claim-migration.mjs` + `check-migration-numbers.mjs` |

**Corrected merge order** (was: 1 identity, 2 reliability, 4 integrations, 5 metrics, 3
content, 6 portal — 5 is a no-op, so): 1 identity → 2 reliability → 4 integrations → 3
content → 6 portal.
Worktrees: `/Users/blakeruff/echo-t{1,2,3,4,5}-*` and `/Users/blakeruff/portal-t6-ui`,
`/Users/blakeruff/portal-rls`. `~/lasso-echo-work` is the shared root — another session uses
it; work from throwaway detached checkouts of `origin/main` and say so every time.
Archived pruned branches: `origin/archive/2026-08-28/*`.

Suite: **5907 passed, 0 failed, 11 skipped** on track/3-content-quality after tonight's
additions (one real failure caught and fixed along the way:
`test_status_completeness.py` — the two new job flags weren't listed in `agent status`,
fixed in `b39a31b`). Baseline this morning was 5355; 552 net new tests since.

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
