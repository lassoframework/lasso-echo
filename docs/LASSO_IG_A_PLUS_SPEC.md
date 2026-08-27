> Delivered by Blake 2026-08-27. Internal spec/reference — NOT approved caption source material. Never cite these numbers in client-facing or LASSO-facing captions.

# LASSO Framework Instagram — A+ Spec for Echo

**Account:** @lassoframework (B2B profile)
**Audited:** August 26, 2026, live IG grid, 84 posts read
**Prepared for:** Echo (post waves 0–7, A-gate live)
**Publisher:** Zernio (verified). Echo currently publishes 7 client-gym accounts through it and **does not publish the LASSO account at all** — see §1.
**Status of the ask:** Echo's A-gate already exists. This document is (a) two live publish-path defects hitting paying clients, (b) the rails the B2B rubric is missing, (c) the LASSO-account configuration if you choose to bring it into Zernio. It does not rebuild anything.

---

## 0. The actual numbers (Zernio analytics, verified)

204 posts synced, June 1 to August 26, 2026. Last sync 2026-08-27.

### Growth
| Metric | Value |
|---|---|
| Followers, Jul 27 | 1,224 |
| Followers, Aug 24 | 1,232 |
| Current | 1,236 |
| **Net growth, 5 weeks** | **+8 followers (+0.65%)** |
| Total media on account | 1,548 |

At roughly 89 posts a month, that is **+8 followers for ~110 posts of effort.**

### Reach, on the 25 *best-performing* posts of the quarter
| Metric | Value |
|---|---|
| Median reach | **125** (about 10% of followers) |
| Median views | 164 |
| Best post reach | 881 |
| Best post views | 1,537 |
| **Total link clicks, all 25 posts** | **0** |
| Total saves, all 25 | 5 |
| Total shares, all 25 | 22 |
| Total follows driven, all 25 | 4 |

Zero clicks across the strongest quarter of content is the headline. The grid is not being asked to do anything, so it does nothing.

### The one post that worked, and why
**July 29 — 881 reach, 1,537 views. Four times the next best post.**

> "Most gym owners plan next year in a notebook that never gets opened again… November 7 and 8, 100 gym owners take over the Virgin Hotel in Nashville… There are only 100 seats. I saved you one. Right now, get it at a discounted early bird special before they are gone! Click Link in bio to sign up!!"

It is the only post in the quarter with a **named event, a date, a real scarcity claim, a price hook, and a direct CTA.** Every other top-25 post is doctrine that ends on a thought. The account's best result came from the one time it asked.

### Timing — the rogue loop sits in the worst slot in the account
Volume-weighted slots (UTC; 14:00 UTC = 10:00 ET):

| Slot | Posts | Avg engagement |
|---|---|---|
| Tue 14:00 | 14 | 1.79 |
| Wed 14:00 | 15 | 1.33 |
| Sun 12:00 | 9 | 1.33 |
| Mon 12:00 | 13 | 0.92 |
| Thu 14:00 | 14 | 0.86 |
| **Fri 14:00** | **14** | **0.29 — worst high-volume slot on the account** |

The four repeating captions fire Wed/Thu/Fri/Sat at ~10:10 ET. They occupy four of the account's highest-volume slots and the weakest engagement numbers in the table. They are not just duplicates — they are duplicates in the dead zone.

### The strategic read
1,236 followers with 125 median reach will not produce meaningful lead volume from organic, no matter how good the content gets. **Stop treating LASSO's IG as a lead channel.** Its job is credibility: when a gym owner sees an ad, hears the podcast, or gets a DM, they check the grid before replying. Right now that check returns 66 text cards, no faces, and the same caption four times.

The goal is not reach. The goal is that the grid **closes** the traffic the other channels already send to it, and that the Summit-style asks get repeated instead of appearing once a quarter.

---

## 1. Where the LASSO grid actually stands today

Measured off the live grid, not the calendar. 84 posts.

| What we counted | Result | The gate leg it hits |
|---|---|---|
| Posts that are a text card with no person in them | **66 of 84 (79%)** | *nothing — see §3, this is the gap* |
| Posts where Instagram detected a human | **0 of 84** | *nothing — see §3* |
| Reels | 17 of 84 (20%) | — |
| Posts tagging a client gym | **2 of 84** | `_proof_numbers` needs ≥ 8/mo |
| Captions carrying a real number ($ , % , MRR, CPL, leads) | **0 of 17 readable** | `_proof_numbers` needs ≥ 8/mo |
| Captions with a call ask | **2 of 17 readable** | `_path` needs ≥ 12/mo |
| Duplicate caption groups still on the public grid | **4 groups, 9 surplus posts** | `_consistency` −8 each |

### The four captions still repeating on the public grid
1. `4x` — "Most gym owners are guessing their way through sales…"
2. `3x` — "Consistent revenue doesn't come from hustle…"
3. `3x` — "If your team doesn't know the exact path, they're improvising…"
4. `3x` — "The gyms winning right now aren't the ones with the most leads…"

These fire weekly around 10:10 ET (Wed/Thu/Fri/Sat).

### Where they are NOT coming from: Zernio

Checked Zernio directly. **78 posts total, every one of them client-gym content** — ENG, GritX, TopFuel, Pierce, Hill Country, Bolton, District. Zero LASSO B2B posts. Zero scheduled posts in the queue.

The `lasso` profile exists (`6a74a3b977a9ae3719f5c0c0`) and the `lassoframework` IG account is connected (`6a69fc9cdf17280d93d0727f`), but **nothing is publishing to it through Zernio.** The entire 84-post LASSO grid — the 66 text cards, the 4 repeaters, all of it — is coming from somewhere else.

So the LASSO account is not an Echo quality problem yet. It is an Echo *coverage* problem: Echo does not run this account at all. Decide which you want:
- **Bring the LASSO account into Zernio** and let the A-gate govern it like a client gym. Everything in §4 applies from that point.
- **Or leave it outside**, and accept that none of the rails below reach it.

Whichever way, find and stop the current publisher first, or Echo and it will fight over the same grid.

---

## 2. The uncomfortable part

We charged Sycamore CrossFit points this month for exactly this:

> *"Points off for the run of branded template graphics, which read as a service posting, not a gym living."*

Sycamore's grid was 31% text cards. **Ours is 79%, with zero humans in 84 posts.** We are selling a rubric we are currently failing harder than the prospect we graded on it. Fix ours before the next card goes out.

---

## 3. The gate gap Echo must close

The B2B profile swaps `_visual_match` (15) for `_proof_numbers` (15). That was the right call for proof and tagging — but it means **nothing in the B2B rubric grades whether a human appears on the grid.** A month of 30 text cards can score a clean A today.

### Add: `_b2b_faces` rail inside `_proof_numbers`

Fold into the existing 15-point leg, no new leg, no band change:

- **≥ 40% of a month's posts must carry a real photo or video of a person** — a coach, the team, a client gym owner, a Summit room, a podcast set. Deduct −1 per post short of the floor.
- **≤ 50% of a month may be text-card creative.** Deduct −3 per 10% over.
- A post counts as carrying a person only from the vision sidecar (`has_member_face`), never from the drafter's intent. Same source of truth Wave 6 already stamps.

### Add: tagging is a hard floor, not a soft one

`_proof_numbers` already wants ≥ 8 tagged posts/month. Two changes:
- Every `proof` post **must** tag the gym it is about. A proof post with no mention is a defect, not a deduction — it goes back to remediation.
- Feed the allowlist from the live client roster so onboarding a gym automatically makes it taggable. We have dozens of client gyms and we tagged two in 84 posts.

---

## 3.5 Two live Echo defects found in Zernio (fix these regardless)

These are on the **client gym** accounts Echo already runs, so they affect paying clients today.

### Defect A — Instagram posts publishing with no caption at all
**26 of 78 published posts have a completely empty caption. Every one of them is Instagram.**

Verified individually, e.g.:
- `6a8f22c1192d58a6cd7a1872` — published 2026-08-26, content empty
- `6a8d157aaff3f7b22cef0c39` — published 2026-08-25, content empty

The pattern points at a caption-dropping bug on the **Instagram publish path specifically**. In several cases the paired Facebook post carries the full caption while its Instagram twin is blank — same concept, same slot, caption lost on one leg only. Example pair: `6a8f04d8…` (facebook, full caption) vs `6a8f22c1…` (instagram, empty).

That is a third of Echo's Instagram output going out with zero words. `_caption_craft` cannot score above 0 on those rows, and no client should be paying $99/mo for a blank post.

**Fix:** assert non-empty caption at the publish boundary, not just at draft time. A row whose caption is empty at publish flips back to `pending` with a reject reason and one deduped alert — same pattern the worker already uses for media-not-ready. Add a regression test that a captionless row cannot reach the Instagram publisher.

### Defect B — one-word captions, and an avatar-rail breach
Two posts published with the entire caption being the single word **"HYROX"** (`6a7e140750b3406b266809b0` instagram, `6a7e140ed3ac8731b8bdaa15` facebook).

Two separate failures in one post:
1. A one-word caption cleared the gate. Minimum caption length is not being enforced at publish.
2. **HYROX is an explicit avatar violation under LASSO org rules** — we do not market to HYROX, competitive CrossFit, or strength-athlete audiences. The avatar filter did not catch it.

**Fix:** enforce the minimum caption length at the publish boundary alongside Defect A, and add HYROX plus the rest of the banned-audience terms to the avatar filter's hard-block list so a draft containing them cannot stage.

---

## 4. LASSO account configuration

### 4.1 Weekly quotas (B2B, 7 posts/week baseline)

| Category | Quota | Notes |
|---|---|---|
| `proof` | **≥ 2 / week** | Must carry a real number AND tag the gym. Empty pool ⇒ alert, never invent. |
| `call` | **≥ 3 / week** | One may be the closing line of a doctrine post. |
| `faces` | **≥ 3 / week** | New for B2B. Coaches, team, Summit, podcast set, client visits. |
| `doctrine` | **≤ 25%** | Already capped. Enforce it — this is what ate the feed. |
| `podcast` | ≤ 25% | — |
| `summit` | ≤ ramp, then ≤ 25% | — |
| `book`, `platform`, `b2b` | fill to cadence | — |

### 4.2 The numbers Echo is allowed to use

Pull only from stored reporting rows. Never generate. Approved shapes:
- A named gym's MRR movement (e.g. "$19K to $47K") — requires the gym's written sign-off flag
- Portfolio CPL, close rate, show rate, booking rate — from the reporting pipeline
- Post/reach counts from publisher analytics
- One canonical gym-count claim, single source. **Fix the "500+ vs 1,000+" inconsistency and store one value.**

If a `proof` slot has no backed number, it becomes `faces`, and one deduped alert fires. It never becomes a doctrine post — that is how doctrine ate 79% of the grid.

### 4.3 Caption rules

- One ask per post. Never two. (We charge clients for "Drop a 💪 below AND DM us" — do not ship it ourselves.)
- Median caption length 150–500 chars. Under 150 is a deduction; over 800 is a wall.
- Hook first line, never an emoji-only opener.
- Copy gate: zero banned dashes. Already clean — keep it.
- Cooldown: **90 days minimum** before any caption concept reappears, and never more than once per concept per quarter. The current 4x repeat is the single worst score on the card.

### 4.4 The ask, specifically

- ≥ 12 call asks per month, one per posting day where doctrine runs.
- Ask must be tappable: DM, comment keyword, or the bio link. **No bare typed URLs** — untappable on IG and already a `_path` deduction.
- Rotate the ask across DM / comment keyword / bio link so the grid does not read as one loop.

---

## 5. Definition of done

Echo may stage a LASSO month only when all of these hold:

- [ ] Grade ≥ 90 on the B2B rubric with `_b2b_faces` active
- [ ] ≥ 40% of posts carry a real human (vision sidecar confirmed)
- [ ] ≤ 50% text-card creative
- [ ] ≥ 8 posts carry a real, stored number
- [ ] Every `proof` post tags its gym
- [ ] ≥ 12 tappable call asks
- [ ] Zero duplicate caption hashes in the plan or within 90 days behind it
- [ ] Zero banned dashes
- [ ] One canonical gym-count claim
- [ ] Every post still lands `pending` — **the human tap does not change**

Below A+, remediate up to 4 passes, then alert a human. Never ship a known sub-A month silently.

---

## 6. Do this first

1. **Fix Defect A.** A third of Echo's Instagram output is publishing with no caption, on paying client accounts. This outranks everything else on this page.
2. **Fix Defect B.** Minimum caption length at the publish boundary, and HYROX onto the avatar hard-block list.
3. **Decide whether Echo runs the LASSO account at all.** It currently does not. Nothing in §4 reaches it until it is in Zernio, and whatever is publishing to it now has to stop first.
4. **Ship `_b2b_faces`.** One rail inside an existing leg. It is the difference between our grid and the grid we sell.
5. **Wire the tagging allowlist to the client roster.** Two tags in 84 posts is free reach we are throwing away.
6. **Re-grade the forward book** with the new rail and remediate before the next month stages.

---

## 7. The 90-day plan for LASSO

### Phase 1 — Weeks 1–2: stop the bleeding
| # | Action | Owner |
|---|---|---|
| 1 | Fix Defect A — captionless Instagram posts. A third of client output is going out blank. | Echo |
| 2 | Fix Defect B — minimum caption length at publish, HYROX onto the avatar hard-block list. | Echo |
| 3 | Find and kill whatever publishes the LASSO grid outside Zernio. Four captions, Wed/Thu/Fri/Sat ~10:10 ET. | Blake |
| 4 | Decide: LASSO account into Zernio under the A-gate, or explicitly out of scope. | Blake |
| 5 | Collect 12 face assets — Summit, podcast set, team, client visits, you on camera. | Blake |

**Exit test:** zero captionless posts published for 7 straight days, and the 10:10 ET loop does not fire.

### Phase 2 — Weeks 3–6: rebuild the rotation
| # | Action | Owner |
|---|---|---|
| 6 | Ship `_b2b_faces` (§3). Zero humans in 84 posts is the gap the B2B rubric cannot currently see. | Echo |
| 7 | Enforce the doctrine ≤25% cap. Doctrine is what ate 79% of the grid. | Echo |
| 8 | Stand up the quotas in §4.1: proof ≥2/wk, call ≥3/wk, faces ≥3/wk. | Echo |
| 9 | Wire the tagging allowlist to the live client roster. Two tags in 84 posts. | Echo |
| 10 | 90-day caption cooldown, backfilled against published history so the 4x repeat cannot recur. | Echo |
| 11 | Fix the canonical gym-count claim. Store one number, use it everywhere. | Blake |

**Exit test:** a staged month grades ≥90 on the B2B rubric with `_b2b_faces` active, and ≥40% of posts carry a human.

### Phase 3 — Weeks 7–12: run the offer engine
| # | Action | Owner |
|---|---|---|
| 12 | Treat the July 29 Summit post as the template, not the exception. **One dated, scarce, priced ask every week.** | Echo |
| 13 | Retire the Fri 14:00 UTC slot. Move that volume to Tue/Wed 14:00 UTC, the account's two strongest high-volume slots. | Echo |
| 14 | Rotate the ask across DM / comment keyword / bio link so the grid stops reading as one loop. | Echo |
| 15 | Turn on the monthly retro against the Phase 1 baseline below. | Echo |

### Baseline to beat (measured today, 90-day window)

| KPI | Today | 90-day target |
|---|---|---|
| Link clicks per month | **0** | 40+ |
| Median reach, top 25 posts | 125 | 200 |
| Posts carrying a human | 0% | ≥40% |
| Posts tagging a client gym | 2 of 84 | ≥8 per month |
| Posts carrying a real number | 0 | ≥8 per month |
| Duplicate captions on the grid | 9 surplus | 0 |
| Captionless posts published | 26 of 78 | 0 |
| Net follower growth per month | +6 | +40 |

Clicks is the KPI that matters. Everything else on this page exists to move it off zero.

### What NOT to do
Do not increase posting volume. 89 posts a month already produced +8 followers and zero clicks. The problem is never that LASSO posts too little.
