# Echo Vision — Image Understanding + Grounded Captions Build Spec (v2)

**Product:** Done For You Social, all platforms (IG, FB, GBP)
**Goal:** Echo looks at every photo a client uploads, understands what is actually in it, and writes the caption FROM the image. No more generic caption paired with a random photo.
**Repo:** lasso-echo-work | **Touches:** media ingest, planner image pick, drafter, post_quality gate
**Does NOT touch:** the portal, the approval queue, publishing. Upstream of everything already built.
**Canonical media store:** R2 (public URLs, Zernio-proxied) — as ratified in GBP_BUILD_SPEC. One store, both specs.

---

## 0. Why this is the highest leverage upgrade left

The tell that social is automated is never the words alone. It is the mismatch: a caption about community on an empty squat rack, a transformation caption on the front desk. One mismatch and the owner stops trusting the queue.

Today `pick_image` selects by filename/rotation and the drafter writes from the gym record. Image and words never meet. This build makes the image the source of the caption — and the most common coach FIX verdict (wrong photo for the words) should measurably drop.

Architecture rule, same as everything else: analyze once, store as data, every consumer reads the stored analysis.

**One honest caveat that shapes this whole spec:** vision models hallucinate too. A grounding system that trusts a wrong analysis ships *confidently specific* fabrications, which is worse than generic captions. So this build treats the analysis itself as untrusted input: confidence scores, crop-level verification of anything a caption actually uses, and contradiction-only gating. Grounded means verified, not just cross-referenced.

---

## 1. Architecture

```
Client uploads media (existing intake / upload link)
        │
Ingest hook (client_media_sync) → VISION ANALYSIS on the ORIGINAL (once)
        │  structured output, per-detail confidence, identity firewall
        ▼
media_analysis stored on the media record (R2 object + DB row)
        ▼
Planner (pick_image) → matches slot JOB to image CONTENT
        │  crops to 1200x900 (existing pipeline)
        ▼
CROP VERIFY (cheap second vision pass on the shipped pixels)
        │  confirms count bucket + any detail the caption will use
        ▼
Drafter (make_caption) → grounded on VERIFIED details only
        ▼
Grounding gate (post_quality) → contradiction check, cost-capped
        ▼
Existing rail: pending → coach screen → owner tap → publish (unchanged)
```

---

## 2. Phase 1 — Vision analysis at ingest

### 2.1 When it runs

- Hook into the existing ingest path: every new image analyzed once at upload, before planning eligibility.
- Backfill for active DFY gyms: idempotent batch, throttled, skips analyzed rows.
- **Failure handling (safety-critical):** in a vision-enabled gym, an image with null analysis is EXCLUDED from auto-planning — the whole point is screening, and an unscreened image must not slip in through an error path. Nightly retry sweep, max 3 attempts, then `analysis_failed` + staff alert. "Legacy mode" exists only as a whole-gym setting (vision flag off), never per-image.
- **Videos are out of scope for v1.** They remain coach hand-pick, excluded from vision scoring. One line in the library view marks them "not auto-planned."

### 2.2 The analysis schema (`media_analysis` jsonb, `media_analysis_version` column)

```json
{
  "version": 2,
  "one_line": "Three people mid squat in a group class, a coach adjusting form",
  "setting": "gym_floor | front_desk | exterior | outdoor | studio | event | other",
  "subjects": ["group_class", "coaching_moment", "barbell"],
  "people": { "bucket": "none | solo | pair | small_group | crowd", "includes_children": false },
  "activity": "strength | cardio | class | coaching | community | facility | food | none",
  "visible_details": [ { "detail": "chalk on hands", "confidence": 0.92 } ],
  "text_in_image": "legible text verbatim, else null",
  "contains_person_name": false,
  "quality": { "sharp": true, "well_lit": true, "usable": true, "reject_reason": null },
  "avatar_fit": "genpop | athlete_leaning | athlete | unclear",
  "safety_flags": [],
  "phash": "64-bit perceptual hash"
}
```

Field rules:

- **`one_line` is the caption seed. Neutral person terms only** — "three people," "a member," "a coach." No names, no gender, no age, no body or appearance commentary, no health judgments. The identity firewall covers gender and appearance, not just names — a misgendered caption is a client-relationship fire.
- **Identity validation scope:** the name/identity validator runs on `one_line`, `subjects`, and `visible_details`. It does NOT run on `text_in_image` — that field must capture name tags and whiteboards verbatim to be useful. Instead, `text_in_image` is firewalled from the drafter entirely, and any person-name in it sets `contains_person_name: true`, which routes like a safety flag (no auto-planning; coach hand-pick only).
- **`people.bucket`, not a count.** Vision models miscount past six; buckets are what captions actually need. "Packed house" requires `crowd`; "one on one" requires `solo|pair`.
- **`visible_details` carry confidence.** Only details ≥0.85 are caption-eligible; below that they exist for search/debug only.
- **`avatar_fit`:** `athlete` never auto-picked; `athlete_leaning` and `unclear` both restricted to Behind the scenes only (conservative default for `unclear`).
- **`safety_flags` enum:** `minor_prominent`, `third_party_brand`, `unsanitary`, `injury_visible`, **`pii_visible`** (whiteboards/screens showing member names, phones, payment info — the most common real gym leak). Any flag = excluded from auto-planning; coach may hand-pick deliberately.
- **No `mood` field.** Subjective, hallucination-prone, and the pick logic works on activity + grouping + quality. Cut.
- `quality.usable=false` (blurry, dark, sliver, screenshot) = excluded from planning; listed in a **staff-facing** library view with reasons. Never shown raw to gym owners — the coach translates it into the monthly "fresh material" ask.

### 2.3 The call

One vision call per image, structured output enforced, low temperature, prompt forbids identity/appearance inference and mandates "describe only what is visible." Cost: once per image, forever; version column allows selective re-runs on schema bumps.

---

## 3. Phase 2 — Library hygiene

- **Near-duplicate collapse:** phash clusters at Hamming distance ≤6 (64-bit), biased toward same-upload-batch (burst shots). Planner treats a cluster as one image, picks the best-quality member.
- **Starvation guard:** the library-gap report counts CLUSTERS, not images. If clusters < planned slots, the gap flag fires to the coach BEFORE planning — never a silently thin month, never forced mismatches.
- **Usage tracking:** global usage record with per-platform recency windows. GBP may reuse an IG-published image after 14 days (different audience, different surface); IG never reuses within 60 days; the same phash cluster never appears twice in one month on one platform.

---

## 4. Phase 3 — Planner: match the job to the picture

`pick_image` moves from rotation to content scoring:

| Slot job | Image preference (from analysis) |
|---|---|
| Transformation | solo/pair, coaching_moment or strength |
| Education | coaching_moment, equipment detail, demonstration |
| Community | small_group/crowd, class, candid |
| Behind the scenes | coach solo, facility (also the only home for athlete_leaning/unclear) |
| Offer | best-lit facility or group shot in the library |
| GBP local update | exterior, facility, group — what a stranger checking the listing wants |
| GBP photo drop | highest quality within reuse windows, rotating settings |

Rules:

- Score = pillar affinity + quality + recency + cluster-not-used-this-month. **Determinism is scoped:** identical picks given the same (gym, month, library snapshot, analysis_version). Rows past `pending` are FROZEN — a mid-month re-plan may only touch rows still in `draft/pending`, never re-pick or re-caption anything the owner has seen or approved.
- Flagged images (safety, athlete, contains_person_name, analysis_failed, null) never auto-picked.
- No image above the score floor → slot planned with best available AND flagged `weak_match` for the coach. Never silent.

---

## 5. Phase 3.5 — Crop verify (the anti-hallucination step)

Analysis ran on the original; the owner approves and Google/Meta receive the 1200x900 crop. Cropping can remove the chalk hands or the fourth person — recreating the exact mismatch this build kills. So:

- At plan time, after the crop is produced and BEFORE the drafter runs, run ONE cheap vision verify on the crop: confirm `people.bucket` and yes/no every ≥0.85 caption-eligible detail for that image. The drafter then writes using only the survivors. (Strictly verify-then-draft — never draft first and verify claims after; that inverts the pipeline and double-charges the §7 cost cap.)
- A detail that survived ingest but died in the crop is simply not used — the caption falls back one rung on the specificity ladder.
- **Verify-call failure:** if the verify call itself errors, the slot degrades safely — zero verified details, no crowd/object/setting claims, generic-safe caption. It does not escalate and does not block the slot.
- Cost: one small call per planned post (~12-15/gym/month). An image swap (§7) triggers one additional crop + verify for the replacement — that verify is INSIDE the slot's total model-call cap.

---

## 6. Phase 4 — Grounded caption generation

`make_caption` receives verified analysis + gym record + pillar. New rules on top of ALL existing copy rules (StoryBrand, break shape, no dashes, gen pop, platform variants, figure-fabrication guard):

1. **Write from the picture.** Hook or body connects to a crop-verified element.
2. **Specificity ladder:** specific and verified beats general; general and safe beats specific and unverified. Never specific and invented. Details below the confidence threshold or failing crop-verify are rungs you cannot stand on.
3. **Identity firewall:** role words only — "our members," "the 6am crew," "one of our coaches." No names, gender, age, bodies, health. Exception only via Phase 6 consent.
4. **Numbers stay banned** unless from the gym record or Phase 6 context. A before/after collage yields zero numeric claims.
5. **Count honesty:** crowd words require the `crowd` bucket, verified on the crop.
6. Platform split unchanged (IG / FB / GBP 80-char hook).

---

## 7. Phase 5 — The grounding gate (extends post_quality)

- **Closed claim taxonomy** — the gate checks exactly four claim classes: objects named in the hook, people quantity words, setting words, activity words. Not free-form claim mining; two engineers build the same gate.
- **Contradiction-gated, not absence-gated.** Fail only when a claim CONTRADICTS the verified analysis (says crowd, image is solo; names an object the crop-verify rejected) or makes a high-risk unsupported claim (identity, numbers, health). Claims merely absent from `visible_details` pass — the analysis cannot enumerate everything visible, and absence-failing guarantees regen loops on good captions.
- **Hard cost cap per slot:** one regeneration, then one image swap + regeneration, then coach escalation as `grounding_unresolved`. That is the TOTAL model-call budget for the slot, including platform variants. Per-gym monthly token spend logged; budget alarm on the monthly analysis + generation total.
- **Identity leak = hard fail, no regenerate, straight to coach.** Any person-name token without a Phase 6 consent record.

---

## 8. Phase 6 — Client-provided context (the consent unlock)

Upload flow gains one optional free-text field per image plus one checkbox:

- **"Tell us about this photo"** → stored verbatim as `client_context` on the media record.
- **Checkbox: "I have this person's permission to be named or featured."** → `consent_confirmed` boolean. The identity exception requires the CHECKBOX, not mere presence of text — an owner typing "Sarah lost 40lbs" is not Sarah's consent, and deriving consent from text presence is consent laundering.
- `client_context` is **raw material, never verbatim output.** Caption use of it passes: (a) the full post_quality gate (an owner's dashes, hashtags, or phone number would otherwise hard-fail at the publish worker after approval — the worst failure sequencing possible), and (b) a **platform-policy screen**: health/medical claims, review bait, before/after weight claims phrased as promises — all real Meta/GBP policy violations even when owner-written and true. Policy-violating context routes to the coach with the reason; it never silently becomes a caption.
- No context = role-words-only captions. The field is upside, not a requirement.
- Intake copy (client-facing, break shape): "Who or what is in this photo? If you name someone and check the permission box, we may use it. Skip it and we keep captions general."

---

## 9. Rollout and cutover

1. **Flag flips at the next `build_client_month` run, never mid-month.** Pending and approved rows are frozen; vision only shapes months planned after the flip. No owner ever re-approves something that changed under them.
2. **Dogfood first:** backfill LASSO's library, re-plan the next dogfood month vision-on, and produce the old-picks vs new-picks DIFF. The diff is the demo and the go/no-go evidence.
3. **Adversarial photo test set** (build it before shipping): name tags legible, whiteboard with member names, before/after collage, athlete comp shot, minor prominent, blurry burst duplicates, empty-gym shot. Every one must route correctly (flag, exclude, or caption safely). This test set is the acceptance bar, not vibes.
4. **Shadow month = plumbing smoke test, not the metric.** Shadow means: analysis + scoring run and log; drafter and picks stay FULLY legacy. One month at n≈12 posts cannot power a FIX-rate comparison — run shadow for plumbing confidence, and make the ship decision on the dogfood diff + test set.
5. Per-gym flag, default ON for new DFY gyms after the founding gyms convert cleanly.

---

## 10. Guardrails (extends the rails to 14)

11. **Never identify a person from a photo.** No names, gender, age, bodies, health. Role words only, unless `consent_confirmed` AND the client wrote it.
12. **Never invent results from an image.** Photos prove presence, not outcomes. Numbers come from the gym record or consented context only.
13. **Flagged and unanalyzed images never auto-plan.** Safety flags, athlete imagery, person-name text, failed analysis — coach hand-pick only. An error path is not a bypass path.
14. **Grounding means verified.** A caption detail must survive the confidence threshold AND the crop verify. Contradictions never ship; unverifiable specificity falls back to safe generality; the system swaps or asks a human, never ships the mismatch.

Enforced twice, as always: generation prompts AND the post_quality gate.

---

## 11. A+ acceptance checklist

**Analysis**
- [ ] Every new upload analyzed at ingest; backfill done; 3-retry sweep then analysis_failed + alert
- [ ] Null/failed analysis excluded from auto-planning in vision-on gyms (tested via forced failure)
- [ ] Identity validator scoped correctly: fires on one_line/subjects/details, NOT on text_in_image; name-tag photo sets contains_person_name and is excluded (tested)
- [ ] No gender/appearance terms in any analysis output (adversarial test set)

**Library**
- [ ] phash clusters at Hamming ≤6; cluster count drives the gap report; starvation fires the flag before a thin month plans
- [ ] Reuse windows enforced: IG 60d, GBP-after-IG 14d, no cluster twice per platform per month

**Planning**
- [ ] Content-scored picks, deterministic per (gym, month, snapshot, version); rows past pending frozen through any re-plan
- [ ] weak_match flagged, never silent; all flag classes excluded from auto-pick

**Captions + gate**
- [ ] Every specific detail used is crop-verified; specificity falls back instead of inventing
- [ ] Gate fails contradictions and high-risk claims only; absence passes; slot cost cap = 1 regen + 1 swap total
- [ ] Identity leak hard-fails to coach; client_context requires checkbox consent AND passes policy screen + full quality gate

**Rollout**
- [ ] Flag flips at next month build only; dogfood diff produced; adversarial test set 100% correct routing
- [ ] Shadow month runs fully-legacy drafter; per-gym token spend logged with budget alarm

Grade is A+ when the dogfood diff shows better picks AND the adversarial test set routes 100% correctly — not before.
