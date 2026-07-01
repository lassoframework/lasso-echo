# Echo — read this first

You are working on Echo, the LASSO social media agent. Before planning or writing
any code, read these three files. They are the source of truth for scope and state.

1. `BUILD_SPEC.md` — the FULL build-out scope (the organic system Echo grows
   into: intake, DAM, runway, reporting, the Agent SDK). Always know where the
   current task sits inside this bigger picture.
2. `PROGRESS.md` — current state, stage by stage. Update the checkboxes as work
   completes ([x] done, [~] built in sandbox pending push, [ ] not started).
3. `echo_build_tracker.html` — the visual dashboard that mirrors PROGRESS.md.

## Non-negotiable gates (never remove, never weaken)
- Every post waits for human approval. Approver Slack id: U06EPUUCL13.
- Publishing defaults OFF (`AGENT_PUBLISH_ENABLED=false`). Draft-only until armed by hand.
- Client content only. No invented facts, offers, prices, or stats. If a required
  note or the voice doc is missing, BLOCK the draft, do not fabricate.
- Per-account trust ladder: trust is earned per account, not globally.
- Human owns voice. Draft only from the approved brand bible + the source doc
  (`brand_voice/` and the LASSO Now source doc).
- Secrets and tokens are set by hand in env only. Never log, print, or commit them.
- Every new capability ships behind a flag that defaults OFF.

## Working rules
- This repo has had commits from multiple agents. Do not assume the sandbox equals
  what is deployed. Make targeted edits to real files; run `python3 -m pytest` and
  confirm green before finishing.
- No em dashes, en dashes, or hyphens in any published marketing copy or on-image text.
- Two open decisions are logged in PROGRESS.md (brand palette; publish path). Do not
  silently resolve them; flag them.
