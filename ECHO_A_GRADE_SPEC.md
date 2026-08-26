# ECHO A GRADE SPEC — Every Calendar Echo Ships Scores an A, Every Gym, Every Month

**Date:** 2026-08-26
**Repo:** `~/lasso-echo-work`
**Status flag convention:** every new behavior ships behind an `AGENT_*` env flag, default OFF, like the rest of the worker.
**Prime directive:** Echo must be structurally unable to stage a month that would grade below an A on the LASSO Social Report Card. Not "writes better" — *unable*. The planner blocks, remediates, and rescores before a human ever sees the queue.

This spec is grounded in an audit run 2026-08-26 against the live lassoframework feed, the Zernio publish log, and this repo. The five defects it fixes were located at file and line, not inferred:

| # | Defect | Location | Consequence observed in production |
|---|--------|----------|-----------------------------------|
| 1 | No `proof` or `call`/`offer` category exists | `agent/content_categories.py:43` — `CATEGORIES = ("podcast","platform","b2b","summit","book","doctrine")` | 1 post with a number in 36; 0 booking asks in 36 |
| 2 | `doctrine` is uncapped gap-filler from a 31-concept pool | `agent/category_plan.py:20`, `brand_voice/lasso_now.md` (93 lines) | Forward book: 125 IG slots from 43 captions, worst caption repeated 20x |
| 3 | No repeat cooldown anywhere | grep for `cooldown|last_used|min_gap|no_repeat` across `category_plan.py`, `doctrine.py`, `real_month_planner.py`, `regen_library.py` returns nothing | Same caption scheduled weekly for 10 straight weeks |
| 4 | No tagging capability in the publish path | zero refs to `mention|tagged_users|usertags` in `socialapi_publisher.py`, `socialapi_client.py`, `zernio_routes.py`, `portal_calendar_store.py` | 0 accounts tagged in 90 days, across every gym |
| 5 | Dash scrubbing reimplemented in 9 modules that disagree | `welcome_review.py:98` and `video_editor.py:143` strip hyphens; `creative_studio.py:570`, `voice_template.py:183`, `weekly_report.py:146`, `pdf_report.py:30` only strip long dashes; `podcast_quote_card.py:47` and `no_creative_fallback.py:78` have their own regex | 4 intraword hyphens leaked to the feed in 17 days while em dashes were clean |

**Naming note:** `agent/grade_gate.py` already exists and is the IMAGE card gate (Q3 single accent, Q6 feed-stopping). Do not touch it. The new calendar scorer is `agent/calendar_grade.py`. Read `tests/test_grade_gate.py` first so the two gates stay clearly separate.

---

## WAVE 0 — Preflight (nothing else is measurable until this is done)

### 0.1 Find and kill the second publisher on lassoframework IG
Between Aug 10 and Aug 26 the public feed carried 65 posts; Echo's calendar accounts for 36. The other 29 include every dash violation in the window and a weekly loop firing near 14:10 ET whose captions have zero rows in `content_calendar`. Suspects, in order: (a) the duplicate Zernio IG connection — `accounts_list` shows lassoframework Instagram twice, IDs `6a69fc9cdf17280d93d0727f` and `6a74b3efd0fe733d1abc6fc1`; (b) a legacy scheduler (Later/Buffer/Meta native) still holding a queue. Action: query Zernio `posts_list` per account ID to attribute the 29; report findings to `#ops` with the evidence. **Do not disconnect anything without Blake's explicit tap** — surface the finding and the recommended disconnect as an approval card.

### 0.2 Dedupe the forward book through Echo, never through SQL
`content_calendar` is a read-side mirror; approvals and status changes flow through Echo's store. Add a one-shot job `agent/jobs/dedupe_forward_book.py`: for every gym, group future `pending` rows by `caption_hash` (defined in Wave 3), keep the earliest occurrence, move the rest to `denied` with `reject_reason='duplicate_purge_2026_08'` via `portal_calendar_store`. Log counts per gym to `#ops`. Expected at minimum: LASSO's 125 IG slots collapse to ~43, then Wave 6 refills the freed slots.

---

## WAVE 1 — One copy gate to rule all nine (`agent/copy_gate.py`)

Every module that renders client-facing text imports from here and **deletes its local copy**. If the gate drifts, everything drifts together — which is the point.

```python
"""copy_gate.py — the single house-style gate for every piece of client-facing
text Echo emits. Captions, welcome posts, video overlays, weekly reports, PDF
copy, quote cards: one scrubber, one validator, zero local reimplementations.

Replaces the local dash logic in: welcome_review, video_editor, creative_studio,
voice_template, weekly_report, pdf_report, clipper_render, podcast_quote_card,
no_creative_fallback. Each of those files should shrink in this wave.
"""
from __future__ import annotations
import re

# em/en/figure/horizontal-bar/minus and friends
_BANNED_DASHES = "‐‑‒–—―−"
_DASH_RE = re.compile("[" + _BANNED_DASHES + "]")
_INTRAWORD_HYPHEN_RE = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")
# protect URLs and @handles/#tags: hyphens inside them are load-bearing
_PROTECTED_RE = re.compile(r"(?:https?://\S+|\b[\w.-]+\.(?:com|net|org|io|co|fit|gym)\S*|[@#][\w.]+)", re.I)

_FILLER_OPENERS = re.compile(
    r"^(we're excited|we are excited|exciting news|just a reminder|don't forget|happy \w+day)\b", re.I)

ASK_RE = re.compile(
    r"(link in (our )?bio|book (a|your) (call|intro|class|spot)|dm us|dm \"?\w+\"?|message us|"
    r"comment \"?\w+\"?|sign up|get started|claim your|reserve your|try a (free )?class|"
    r"schedule (a|your)|start (here|today|your))", re.I)

def scrub(text: str) -> str:
    """Rewrite, never reject. Long dashes become ', '; intraword hyphens become a
    space; URLs, @handles and #tags pass through untouched."""
    out, last = [], 0
    s = str(text)
    for m in _PROTECTED_RE.finditer(s):
        out.append(_scrub_plain(s[last:m.start()])); out.append(m.group(0)); last = m.end()
    out.append(_scrub_plain(s[last:]))
    return "".join(out).strip()

def _scrub_plain(t: str) -> str:
    t = _DASH_RE.sub(", ", t)
    t = _INTRAWORD_HYPHEN_RE.sub(" ", t)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t

def violations(text: str) -> list[str]:
    """Hard failures. A caption with any of these never reaches the queue."""
    v = []
    plain = _PROTECTED_RE.sub("", str(text))
    if _DASH_RE.search(plain): v.append("banned_dash")
    if _INTRAWORD_HYPHEN_RE.search(plain): v.append("intraword_hyphen")
    return v

def soft_flags(text: str) -> list[str]:
    """Quality flags the calendar grader scores against (not hard blocks)."""
    f = []
    t = str(text).strip()
    first = t.splitlines()[0] if t else ""
    if len(t) < 120: f.append("thin_caption")
    if first.startswith("#") or first.startswith("@"): f.append("hook_is_tag")
    if len(first) > 125: f.append("hook_too_long")
    if _FILLER_OPENERS.match(first): f.append("filler_opener")
    if not ASK_RE.search(t): f.append("no_ask")
    return f
```

**Migration of the nine call sites** is mechanical: replace each local scrub/assert with `copy_gate.scrub` on write and `copy_gate.violations` on assert. `pdf_report.py`'s "— → ', ' / – → ' to '" nuance is preserved by scrubbing *before* its typography pass. Add `tests/test_copy_gate.py`: dash scrub, hyphen scrub, URL protection (`linktr.ee/no-sweat-intro` survives), handle protection (`@coach_amanda` survives), ask detection, hook flags. Then a repo-wide guard test: grep source (excluding tests and this module) for the banned char class; assert zero regex definitions remain outside `copy_gate.py`.

---

## WAVE 2 — Categories that can actually produce an A (`content_categories.py` + `category_plan.py`)

### 2.1 New categories
```python
# content_categories.py — was: ("podcast", "platform", "b2b", "summit", "book", "doctrine")
CATEGORIES = ("podcast", "platform", "b2b", "summit", "book", "doctrine", "proof", "call")
# gym-side plans use the gym pillar set:
GYM_PILLARS = ("results", "education", "community", "faces", "offer", "invite")
```

### 2.2 Quotas and caps (`category_plan.py`)
Rules, replacing "doctrine fills every remaining gap (uncapped)":

- **No category may exceed 25% of a month.** Summit's ramp still governs summit but is now also clipped by the cap.
- **LASSO (B2B) weekly quota:** `proof` ≥ 2, `call` ≥ 3 (one of which may be the closing line of a doctrine post), `summit` ≤ ramp, everything else fills to cadence.
- **Gym monthly quota (26–31 slots):** `results` ≥ 4, `offer` ≥ 4 *only while the gym has a live offer* (see 2.3 — offer slots with no live offer become `invite`), `faces` ≥ 3, `community` ≥ 5, `education` ≥ 6, `invite` fills gaps. `invite` is the free-intro ask pointed at the booking link.
- **Every post ends with exactly one ask** (`copy_gate.ASK_RE` must match). The drafter appends the category's default ask when the draft lacks one: gym default is the booking link line, LASSO doctrine default is the call ask, podcast default stays the listen ask.

### 2.3 Grounding rails (these are LASSO org law, encode them)
- **`proof`/`results` drafts are assembled only from stored, gym-approved assets** (testimonial rows, reporting numbers Echo already produces, transformation media with consent flag). If the pool is empty, the slot falls back to `community` and fires ONE deduped coach alert ("proof pool empty for {gym}") — Echo **never invents a result, a number, or a quote**.
- **`offer` posts run only while the gym's offer is live** (offer record with an end date). Expired offer ⇒ slot converts to `invite`.
- **Avatar filter stays upstream:** athlete-leaning drafts (competition, HYROX, "athletes only") are filtered before the queue for every gym, per the existing gen-pop rail.
- Everything still lands `pending`. **The human tap stays. This spec changes what reaches the tap, never whether the tap happens.**

---

## WAVE 3 — Repeat cooldown (`agent/caption_ledger.py`)

```python
"""caption_ledger.py — nothing gets scheduled twice inside its cooldown window.

Normalized-hash ledger over everything Echo has ever staged or published,
per gym. Backed by the portal (survives worker restarts) + kv cache.
"""
from __future__ import annotations
import hashlib, re

COOLDOWN_DAYS = 60          # a caption may not re-enter a calendar within 60 days
HARD_BLOCK_SAME_MONTH = True  # and never twice in the same calendar month, period

def caption_hash(text: str) -> str:
    t = re.sub(r"[#@]\S+", "", str(text).lower())   # tags/mentions don't differentiate
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()[:200]
    return hashlib.sha256(t.encode()).hexdigest()[:16]
```

Portal migration (additive, follows the repo's migration pattern):
```sql
create table if not exists caption_ledger (
  gym_id text not null,
  caption_hash text not null,
  last_used date not null,
  uses int not null default 1,
  primary key (gym_id, caption_hash)
);
```

Wire-in points: `real_month_planner` consults the ledger before accepting a draft (regenerate on hit, max 3 attempts, then pull the next concept — never ship the repeat); `portal_calendar_store` stamps the ledger when a row is staged; `calendar_autopublish` stamps again at publish. Backfill job: hash all historical `content_calendar` captions into the ledger so the cooldown knows about the pre-existing 40x and 12x repeats from day one.

Concept-level cooldown too: `doctrine`/`education` concepts get a `last_used` stamp in the kv (pattern already used everywhere in this repo for alert dedupe) with a 30-day minimum gap — the 31-concept pool stops recycling weekly even in paraphrase.

---

## WAVE 4 — Tagging, end to end (`AGENT_MENTIONS`, default OFF)

The audit's most expensive zero: 0 tagged accounts in 90 days, every gym. Root cause is capability, not prompting — no mentions field exists anywhere in the path.

### 4.1 Data
```sql
alter table content_calendar add column if not exists mentions jsonb not null default '[]';
-- gym-side allowlist, consent-gated:
create table if not exists gym_tag_allowlist (
  gym_id text not null,
  handle text not null,            -- without the @
  kind text not null check (kind in ('own','coach','member','partner')),
  consent boolean not null default false,  -- required true for kind='member'
  primary key (gym_id, handle)
);
```

### 4.2 Rules
- Drafter may only insert handles that exist in the gym's allowlist. `member` handles require `consent=true` (a testimonial release, recorded by the coach). **Never tag a member without the consent flag. Never tag an account not on the list.**
- `results`/`proof` posts: tag the member (if consented) or the gym's own handle. `faces` posts: tag the coach. LASSO `proof` posts: **tag the client gym, every time** — this was the "0 tags in 219 posts" finding; each untagged win forfeits the reach and the third-party credibility.
- Mentions are placed **in the caption text** as `@handle` — plain text through Zernio's `posts_create`, renders as a live mention on IG/FB, zero new API surface, works today. Photo-tag (`usertags`) support in the Late API is a stub: `socialapi_publisher` gets a `mentions` kwarg that appends to caption now and can upgrade to photo tags if/when the API exposes them.
- `copy_gate._PROTECTED_RE` already protects `@handles` from the scrubber (Wave 1) — that interlock is deliberate.
- Approval card shows the mentions so the human tap covers the tag too.

### 4.3 Seed
One-shot job: seed each gym's allowlist with its own IG handle + coach handles from the gym record; seed LASSO's list with every connected client gym handle from Zernio `accounts_list`. Post the seeded lists to `#ops` for review.

---

## WAVE 5 — The calendar grader (`agent/calendar_grade.py`) — the A gate itself

Same six legs, same weights, same bands as the client-facing Social Report Card, computed deterministically over a planned month. Two profiles: `GYM` and `B2B` (B2B swaps Visual Match → Proof and Numbers, Path to Join → Path to a Call).

```python
"""calendar_grade.py — scores a planned (or published) month on the LASSO
Social Report Card rubric. Deterministic, offline, no API calls.

A calendar that cannot score >= 90 (A) DOES NOT STAGE. The planner remediates
and rescores in a loop; only an A reaches the human approval queue.
Distinct from grade_gate.py, which grades individual card IMAGES.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from agent import copy_gate
from agent.caption_ledger import caption_hash

WEIGHTS = {"consistency": 20, "content_mix": 20, "caption_craft": 20,
           "visual_match": 15, "right_audience": 15, "path_to_join": 10}
A_THRESHOLD = 90
BANDS = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F"))

@dataclass
class CalendarGrade:
    total: int
    letter: str
    scores: dict           # leg -> points
    defects: list = field(default_factory=list)   # (leg, row_ref, reason) — remediation worklist

def grade_month(rows, profile="GYM", quotas=None) -> CalendarGrade:
    """rows: planned calendar rows for one gym+platform+month, each with
    .post_date .category .caption .mentions .media_kind .vision_derived"""
    scores, defects = {}, []
    scores["consistency"]   = _consistency(rows, defects)
    scores["content_mix"]   = _content_mix(rows, profile, quotas, defects)
    scores["caption_craft"] = _caption_craft(rows, defects)
    scores["visual_match"]  = (_proof_numbers(rows, defects) if profile == "B2B"
                               else _visual_match(rows, defects))
    scores["right_audience"] = _right_audience(rows, profile, defects)
    scores["path_to_join"]  = _path(rows, profile, defects)
    total = sum(scores.values())
    letter = next(l for floor, l in BANDS if total >= floor)
    return CalendarGrade(total, letter, scores, defects)
```

Scoring functions (deterministic, each deducts from its leg's max and appends a machine-actionable defect):

- **`_consistency` (20):** full-month day coverage with no gap > 1 day (−4 per gap > 1 day, −8 per gap > 3); zero ledger/cooldown hits inside the month (−4 each); duplicate `caption_hash` within the plan is −8 each — the 20x repeat can never score above F on this leg alone.
- **`_content_mix` (20):** every quota from Wave 2 met (−3 per missed quota); no category over 25% (−3 per breach); `results`/`proof` slots present and backed by real assets (−4 if any proof slot is unbacked).
- **`_caption_craft` (20):** `copy_gate.violations` on any row = automatic 0 for the leg (hard rail); else −1 per `soft_flag`, floor 8, and −4 if median caption length < 150.
- **`_visual_match` (15, GYM):** every media row has a vision sidecar and the caption was drafted from it (the ECHO_VISION pipeline) −3 each miss; no stock assets (−5 each); one visual system per grid month (template mix flag −3).
- **`_proof_numbers` (15, B2B):** ≥ 8 posts/month carrying a real number from reporting (−1 per missing), ≥ 8 tagging a client gym (−1 per missing), one canonical gym-count claim everywhere (−3 if mixed — the "500+ vs 1,000+" finding).
- **`_right_audience` (15):** zero athlete-avatar leaks for GYM profile (−5 each, the existing filter feeds this); hook intent matches the gym's declared avatar and age band (−2 each miss).
- **`_path` (10):** 100% of rows carry exactly one ask (−1 each miss); ≥ 5 rows/month point at the booking link (GYM) or ≥ 12 call asks/month (B2B, one per posting day where doctrine runs); no bare typed URLs as the only ask (−1 each — untappable on IG).

### 5.1 The enforcement loop (`real_month_planner`)
```
plan month -> grade_month() -> A? stage as pending, post grade to approval card
                            -> below A? remediate from grade.defects
                               (swap dupes for fresh drafts, fill missed quotas,
                                append missing asks, replace unbacked proof slots)
                               -> regrade. Max 4 remediation passes; if still < A,
                               DO NOT STAGE — fire one deduped ops alert with the
                               defect list. A human decides. Echo never ships a
                               known sub-A month silently.
```
Flag: `AGENT_CALENDAR_GRADE` (default OFF → grade computed and logged, not enforced; ON → gate enforced). Two-step rollout like every other lane.

### 5.2 Publish-time recheck (`calendar_autopublish`)
Each row is rechecked at publish: `copy_gate.violations` empty, ledger cooldown clean, mentions still allowlisted. A failing row flips back to `pending` with `reject_reason` and one deduped alert — the same pattern the worker already uses for media-not-ready.

### 5.3 The monthly card closes the loop (`agent/jobs/grade_sweep.py`)
Nightly per gym: grade the **live trailing 30 days** (published rows) and the **forward book**. Write both to a new `gym_social_grades` table (gym_id, window, total, letter, scores jsonb, graded_at). Alert the coach channel when either drops below B, with the top three defects. Month-end: render the client-facing Social Report Card PDF from `gym_social_grades` — the same card we sell with, generated from the same rubric the planner is gated on. One rubric, three uses: gate, monitor, sales asset.

---

## WAVE 6 — Rollout to every gym

1. **Order:** LASSO first (dogfood, `AGENT_CALENDAR_GRADE` ON for gym_id `lasso` only), then ENG, GRITX, Pierce, TopFuel, then default-ON for new onboards. Per-gym flag map, same pattern as the GBP rollout.
2. After Wave 0.2's dedupe frees slots, run the planner's remediation to refill every gym's forward book to an A before re-staging. Everything refilled lands `pending` — coaches tap through the new queue; nothing publishes without the tap.
3. `#ops` gets a one-page rollout digest per gym: before-grade, after-grade, slots purged, slots refilled, mentions seeded.

---

## Tests (all offline, matching house test style)

- `test_copy_gate.py` — scrub/violations/protection/ask/hook + the repo-wide "no local dash regex outside copy_gate" guard.
- `test_caption_ledger.py` — hash normalization (mentions/hashtags/case/punctuation collapse), cooldown math, same-month hard block.
- `test_calendar_grade.py` — a synthetic perfect month grades A=100; inject each defect class and assert the exact deduction and defect tuple; the observed production pathologies as regression fixtures: the 20x repeat month grades F on consistency, the 0-ask month grades ≤ 4 on path, the summit-44% month breaches the cap.
- `test_mentions.py` — allowlist enforcement, member-consent gate, caption assembly, publisher kwarg, scrubber leaves `@handle` intact.
- `test_planner_gate.py` — remediation loop converges on seeded fixable months; unfixable month does not stage and alerts once.

## Definition of A+ (acceptance)

1. Second publisher identified with evidence in `#ops`; duplicate Zernio IG connection surfaced for Blake's disconnect tap.
2. Zero dash-regex definitions outside `copy_gate.py`; all nine call sites migrated; tests green.
3. LASSO forward book: 0 duplicate hashes, summit ≤ 25%, `proof` and `call` quotas met, forward grade ≥ 90 — visible in `gym_social_grades`.
4. Every gym's forward book grades A; every below-A condition alerts within one nightly sweep.
5. Tags live: LASSO proof posts tag the client gym; gym results posts tag consented members; zero tags outside allowlists.
6. All 90+ existing tests still green; every new lane behind its flag, default OFF; **every post still lands pending; the human tap is untouched.**

## Rails (unchanged, restated because the planner now writes more)

No invented results, numbers, quotes, or offers, ever. Offer posts only while the offer is live. Gen-pop avatar filter on every gym draft. No dashes in anything client-facing. Learn More stays the default ad CTA (untouched by this spec). Human approves every post before publish. `content_calendar` writes go through Echo's store, never direct SQL.

---

## WAVE 7 — The Learning Loop: Echo improves itself every month, per gym (`AGENT_LEARNING_LOOP`, default OFF)

The A gate (Wave 5) makes every month structurally sound. This wave makes every month *smarter than the last one*, per gym, autonomously: after a month publishes, Echo studies what earned engagement and what died, updates that gym's playbook within hard bounds, and plans the next month from it. The rubric stays fixed; what improves is everything inside it — which hooks, which formats, which pillars beyond the floors, which time slots, which concepts.

**Grounded against the live API (probed 2026-08-26):** Zernio `analytics_get_analytics` returns, per post: `impressions, reach, likes, comments, shares, saves, clicks, views, follows, igReelsAvgWatchTime, igReelsVideoViewTotalTime, videoDurationSeconds, engagementRate, mediaProductType, publishedAt, platformPostId, platformPostUrl`, plus per-account `followersCount`, and it syncs **external** posts too (`isExternal: true`) — so the loop sees the whole feed, including posts Echo did not publish. `analytics_get_post_timeline` gives per-post time series; `analytics_get_best_time_to_post` exists but we compute our own from `post_metrics` (per-gym, more honest than a global heuristic).

### 7.1 Metrics ingestion (`agent/metrics_sync.py`, flag `AGENT_METRICS_SYNC`)
Nightly per gym: pull Zernio analytics (`source=all`), join to `content_calendar` via `late_post_id`, fall back to `platformPostId`. Snapshot at post-age days 1, 3, 7, 28 — engagement is a decay curve, and comparing a 2-day-old post against a 3-week-old one is how naive loops lie to themselves. **Dedupe by `platformPostId`** — the duplicate lassoframework connection means the same post can arrive under two account ids; one row wins. Rows with no calendar match are stored with `calendar_id null` and flagged `external` — they inform the gym's baseline but never train the playbook (we don't learn from posts we didn't shape, and we don't let the second publisher poison the data).

```sql
create table if not exists post_metrics (
  gym_id text not null, platform text not null, platform_post_id text not null,
  calendar_id uuid, external boolean not null default false,
  pillar text, format text, hook_family text, ask_type text, time_slot text,
  caption_len_band text, has_member_face boolean, media_product_type text,
  published_at timestamptz, snapshot_day int not null,   -- 1 | 3 | 7 | 28
  impressions int, reach int, likes int, comments int, shares int, saves int,
  clicks int, views int, follows int, watch_time_ms bigint, video_seconds int,
  followers_at_snapshot int,
  primary key (gym_id, platform, platform_post_id, snapshot_day)
);
```

### 7.2 Feature stamping at draft time (the levers)
The retro can only learn levers it can see. The drafter stamps every calendar row at stage time: `hook_family` (question | bold_claim | story_open | number_lead | pain_callout), `ask_type` (booking_link | dm | comment_keyword | bio), `time_slot` (from publish schedule), `caption_len_band` (short <150 | mid 150–500 | long >500), `has_member_face` (from the existing vision sidecar), plus the pillar and format it already has. Additive columns on `content_calendar`, populated going forward; the historical backfill classifies old rows best-effort with the same heuristics.

### 7.3 The score (one number per post, comparable within a gym)
```
engagement_value = 1*likes + 3*comments + 4*shares + 4*saves + 3*clicks + 5*follows
score = engagement_value / max(reach, 0.10 * followers_at_snapshot)
```
Saves and shares outweigh likes because they predict non-follower distribution; follows are the business outcome. Day-7 is the scoring snapshot (day-28 only for follows attribution). The reach floor stops a post that reached 3 people from posting a fake 200% rate. Reels additionally track `watch_ratio = avg_watch_time / duration`.

### 7.4 Honesty guards (small accounts lie loudly — a gym with 18 median likes will generate noise every month)
1. **Minimum sample:** a lever value needs ≥ 6 scored posts in the window before it may be compared at all.
2. **Within-gym comparisons only.** Zanshin's numbers never judge Pierce's content. Cross-gym data is priors only (7.6).
3. **Rolling 90-day window,** recency-weighted, so one viral fluke (the 807-like coaching reel) doesn't own the playbook forever.
4. **Two-month persistence rule:** a lever change is adopted only when the winner beats the alternative by ≥ 30% relative score in two consecutive months, or in one month with ≥ 12 posts per side. Otherwise it stays an observation.
5. **Format-stratified:** reels compare against reels, photos against photos, before any cross-format claim.
6. **Contaminated months don't train:** a month with an active second publisher, a follower spike > 20%, or paid boosts on organic posts is marked tainted and observed but not learned from.

### 7.5 The playbook (`gym_playbook` — what the planner actually reads)
Per gym, one versioned JSON the planner biases toward: pillar weights above the Wave 2 floors, hook_family weights, format split, top time slots, boosted concepts, retired concepts (with why and an auto-expiry so nothing is banned forever on thin evidence).

**Bounds, non-negotiable:** the optimizer tunes *inside* the A-grade structure, never against it. Quota floors, the avatar rail, the ask rule, offer rules, consent rules and the copy gate are invisible to it — it cannot trade them away for engagement. Max drift per weight per month: ±20%. Every playbook write carries `updated_by='monthly_retro'`, the evidence rows behind it, and full history — a coach can read exactly why Echo now prefers story openers for this gym, and revert it.

### 7.6 Cross-gym priors (the compounding moat)
Monthly aggregate across all non-tainted gyms into anonymous lever priors ("member-face photos outscore template graphics at the portfolio median", "comment-keyword asks outperform bare bio asks on accounts under 2,000 followers"). Priors do exactly two jobs: seed a **new** gym's day-one playbook so gym #41 starts where the fleet is, and break ties when a gym's own data is under the sample floor. A gym's own evidence always overrides the prior as it accumulates. Every gym makes every other gym's starting point better — that is the moat.

### 7.7 Experiments (so learning is causal, not just observational)
Each month the planner reserves ~15% of slots as labeled experiments: one lever varied, everything else held (same pillar, same format, same slot class). Only labeled experiments support causal claims in the retro; everything else is trend observation. One lever under test per gym per month — no factorial soup on a 30-post account.

### 7.8 The monthly retro (`agent/jobs/monthly_retro.py`, runs the 5th for the prior month)
1. Pull matured `post_metrics` for the closed month; check taint.
2. Score every lever vs the gym's rolling baseline; evaluate the month's experiment.
3. Produce findings: top 3 keep-doing (with evidence), top 3 stop-doing, the experiment verdict, next month's experiment.
4. Update `gym_playbook` within bounds; write the `monthly_retro` row (gym_id, month, findings jsonb, playbook_diff jsonb, tainted bool).
5. Post the digest to the gym's coach channel; add a "WHAT WE LEARNED AND CHANGED" section to the client's monthly report card PDF — the retro becomes retention proof: the client watches the service get smarter about *their* audience every month.
6. LASSO's own retro posts to #ops. Same loop, B2B levers (doctrine hook families, call-ask placement, summit creative variants).
7. Planner picks up the new playbook for the next month build. Loop closed: **plan → gate at A → publish → measure → learn → plan better.**

```sql
create table if not exists gym_playbook (
  gym_id text not null, version int not null, updated_by text not null,
  playbook jsonb not null, evidence jsonb, created_at timestamptz default now(),
  primary key (gym_id, version)
);
create table if not exists monthly_retro (
  gym_id text not null, month date not null, findings jsonb not null,
  playbook_diff jsonb, tainted boolean not null default false,
  created_at timestamptz default now(), primary key (gym_id, month)
);
```

### Tests
`test_metrics_sync.py` (snapshot dedupe by platformPostId across duplicate accounts, external flagging, calendar join); `test_learning_guards.py` (sample floor, persistence rule, taint exclusion, drift cap — feed it a synthetic viral fluke and assert the playbook does not move); `test_playbook_bounds.py` (optimizer cannot lower a quota floor, touch a rail, or exceed ±20% drift); `test_monthly_retro.py` (synthetic month produces deterministic findings and a bounded diff).

### Wave 7 acceptance
1. `post_metrics` populating nightly for every connected gym, deduped, external posts flagged.
2. First retro runs for every gym with a closed month; digest lands in the coach channel; report card carries the learned section.
3. A synthetic-noise regression proves the guards hold (no playbook movement on a fluke month).
4. Planner provably consumes the playbook (same month planned before and after a playbook change differs in the biased direction, still grades A).
5. All rails hold: floors immutable, human tap untouched, no invented numbers — the retro only ever cites metrics rows it can point to.
