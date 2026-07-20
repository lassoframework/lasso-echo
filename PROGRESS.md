# Echo Build Tracker

Living tracker for the Echo social agent build. This markdown is the source of
truth; the HTML dashboard (`echo_build_tracker.html`) is the visual view. The
full organic-system scope lives in `BUILD_SPEC.md`.

Status key: [x] done  ·  [~] built + tested in reference repo, push/deploy pending  ·  [ ] not started

Last updated: 2026-07-20

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
