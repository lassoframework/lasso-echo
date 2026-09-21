# Echo channel repair, 2026-09-21

Lead: Astra. Base: origin/main 14ffdf2. Both live Echo services report that commit.
Scope: screenshot-reported LASSO Story quality failures and duplicate/misleading alerts in C0BF3T5HDR6. No GHL work, publishing, flag changes, calendar mutations, or Slack sends in the builder scope.

Observed: on September 21 the September 23, 27, 28 and 29 Story renders repeatedly put wordmark above y=.10 and CTA/destination below y=.85. Latest main already includes September 19 prompt-only grid fix. Each terminal grade failure produces a long house-style alert followed by a misleading studio-dark alert. Corrective retries currently pass prose without rejected candidate pixels.

Acceptance:
1. Quality retries receive the exact rejected candidate pixels plus concrete review feedback; initial requests remain unaffected, review stays independent, all supplied copy remains required.
2. Story brief reserves real clearance inside existing reviewer bounds with size-aware pixel guidance; full-frame art, no inset feed poster, no weakening of x=.06-.94 / y=.10-.85 review.
3. A terminal quality failure produces one concise, contextual alert and retains full scrubbed review evidence locally. Stories distinguish quality rejection, rendering unavailability, and hosting failure. Do not hide unrelated failures.
4. Every retry is reviewed; FAIL/UNGRADED withheld after three attempts; no crop fallback, no approval/publish changes, no cross-tenant changes.
5. Tests cover actual request images, failed candidate replacement on successive retries, single-alert path, hosting failure, full-frame Story output and legacy/feed compatibility. Run focused and full suites, documenting pre-existing failures.
6. Lead reviews actual diffs and live sample pixels/evidence before claiming resolution. No blanket regeneration or replay of published/approved rows.

Kimi wave: kimi-for-coding / low / 256K, four non-overlapping packages once existing swarm releases its lock. Do not start competing swarms.

- Engine worker owns agent/image_engine.py and tests/test_echo_retry_image_engine.py. Add opts repair_image_bytes and optional repair_image_mime (PNG default), independent of benchmark reference count/flag. Include actual repair image with an explicit rejected-candidate label and correction-only instruction. Benchmark references remain separately labeled. Keep request format unchanged absent repair. No public URL fetching for repair. Prove payload and byte identity in tests.
- Studio worker owns agent/creative_studio.py and tests/test_echo_quality_retry.py. On LASSO content-quality corrective retries, pass exact previous failed image_bytes via opts repair_image_bytes. Add optional failure_info dict to generate(), clearing it at call entry and populating reason, stage, reported for terminal failures. reported means an existing ops alert/audit was emitted for that call. Maintain None/success return contract. Record full scrubbed grade reason durably, then emit one bounded contextual quality alert (account, surface, draft id if available, attempts, short reason). Retain complete evidence even when generation record flag is off. Do not change policy/threshold/attempt limit. Tests verify latest rejected pixels used and no PASS bypass.
- Story worker owns agent/stories.py and tests/test_echo_story_failures.py. Pass per-call failure_info={} and draft_id into creative_studio.generate. Suppress only redundant story alert when failure_info says reported. Log distinct quality skip accurately; distinguish hosting-failed from render-unavailable. Never suppress unknown failures, never crop/reuse feed assets. Keep no-9:16-by-design log-only behavior and dormant flags.
- Layout worker owns agent/astra_prompt.py and tests/test_echo_story_layout.py. Improve Story-only content brief interior clearance with normalized AND dimension-aware pixel bounding boxes, preserve full-frame art/copy and artistic flexibility; reference composition never overrides placement. No evaluator changes, no template inset. Exercise long screenshot headlines and custom dimensions. Keep feed and non-LASSO existing behavior.

Builders must read this plan and source, run their targeted tests, report files/commands/results; do not commit, push, deploy, modify credentials, inspect secrets, send messages, touch production, change flags, or claim grades. Do not edit other packages. Lead owns integration, tracker updates, baseline-test fixture repairs, live read-only checks and final audit.

Live evidence note: AGENT_GENERATION_RECORD is unset in production and generation_records has zero rows for today. db.audit truncates reasons at 500 characters. The studio worker must not rely on that field for full review evidence. Persist a scrubbed failure sidecar beside the requested output (e.g. output.png.failure.json) with full reason, copy, review history, account/surface/draft context and candidate hash; do not create an approved PNG or review.json on failure. Sidecar persistence errors must not turn a FAIL into success or conceal the failure. No schema change required. The existing benchmark images are feed-shaped; placement instructions and rejected-candidate correction must take precedence over their layout.

Baseline started on untouched main. Initial stop-after-three run: 548 passed, one skipped, three failures in tests/test_brain_feeds_captions.py. Fixture run_at is September 6 and is now over the fourteen-day TTL; investigate as stale test clock without weakening production freshness checks.

User copy correction, September 21: remove the 11pm premise. Approved headline is now "You did not open a gym to run your own ads." Updated the local approved source and demo hooks; existing rendered pixels must be regenerated from this wording before reuse. Legacy asset filenames remain unchanged to avoid broken references.


## Independent acceptance, September 21

The initial Kimi wave used four observed kimi-for-coding workers at low effort with a 256K cap. Engine, studio, Story and layout packages were delivered. Astra rejected the first visual implementation after real samples repeatedly failed; no quality threshold was lowered. Astra removed prescribed row positions and generation-side feed examples for Stories, retaining benchmark pixels only for independent review. This follows the September 14 content-led creative-freedom contract and September 18 full-frame Story requirement. The prior fixed-grid prompt was already deployed on main and was not sufficient.

| Acceptance | State | Evidence |
| --- | --- | --- |
| Exact latest rejected pixels reach an actual edit request | BUILT | agent/image_engine.py AstraImageEngine.generate; tests/test_echo_channel_integration.py |
| Creative freedom plus pixel-aware safe area on initial and repair calls | BUILT | agent/astra_prompt.py story_text_grid and build_content_brief; tests/test_echo_story_layout.py |
| One contextual failure alert and full scrubbed review evidence | BUILT | agent/creative_studio.py _write_failure_sidecar; tests/test_echo_quality_retry.py |
| Reported quality/provider failure does not trigger duplicate dark alert | BUILT | agent/stories.py build_story_draft; tests/test_echo_story_failures.py |
| Every candidate independently reviewed; failed/unknown images withheld after three attempts | BUILT | tests/test_echo_channel_integration.py and test_echo_quality_retry.py |
| Corrected headline survives regeneration from older saved copy | BUILT | agent/creative_studio.py generate; test_regeneration_from_old_saved_hook_uses_blakes_approved_correction |
| Live full-frame Story rendering | BUILT | Stable local evidence folder ../echo-channel-fix-evidence-20260921/new-standard-ads and new-standard-fire; actual provider + independent reviewer, first-attempt PASS 94 and 93; Astra inspected both images |
| Production release and verification | PENDING | Do not equate the local checks with deployment |

Grok ECHO read the current files independently through the authorized Grok Bot Control handoff ECHO-STORY-REVIEW-0921. Accepted findings: reuse the same pixel bounds in corrective briefs, suppress the already-reported provider-failure duplicate, honor configured repair input fidelity, remove misleading absent-reference text, and add regression checks. Its action=edit compatibility concern was already resolved by actual provider edit calls during this task and the official image-generation tool documentation: https://developers.openai.com/api/docs/guides/tools-image-generation . No fallback or quality bypass was added.

Validation: untouched main originally had eight date-sensitive fixture failures (8228 pass, one skip). Fixed only those fixture clocks, preserving production freshness rules. Stable local full run before final audit changes: 8276 pass, one skip, 328 seconds. Final audit adjustment subset: 256 pass; corrected saved-copy subset: 8 pass. Final-commit CI must finish before release. No GHL, publishing flags, calendar rows, customer messages or billing changed.
