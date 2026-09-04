# Echo — read this first

You are working on Echo, the LASSO social media agent. Before planning or writing
any code, read these three files. They are the source of truth for scope and state.

1. `BUILD_SPEC.md` — the FULL build-out scope (the organic system Echo grows
   into: intake, DAM, runway, reporting, the Agent SDK). Always know where the
   current task sits inside this bigger picture.
2. `PROGRESS.md` — current state, stage by stage. Update the checkboxes as work
   completes ([x] done, [~] built in sandbox pending push, [ ] not started).
3. `echo_build_tracker.html` — the visual dashboard that mirrors PROGRESS.md.

## LASSO Brain — Echo's knowledge source (Blake, 2026-08-31)
Echo's LASSO-content knowledge source is `~/LASSO/lasso-brain/` — the shared LASSO
Brain (READ-ONLY: agents compile FROM it, never edit those files; the rules live in
its `corpus-map.md`). The compilation contract: caption VOICE from the 40-line voice
bank in `book-doctrine.md` (§22); content PILLARS from `book-chapter-summaries.md`
(22 chapters = 22 pillars); HOOKS from `book-objection-answers.md` (objections make
the best organic hooks); OFFERS/PROOF stats from `website-kb.md` quoted EXACTLY
(lead with the "71.9% booking rate vs. 18.5% industry average" pilot stat); each
podcast transcript in `podcast/transcripts/` is a repurposing source for posts/reels.
New corpus lands in the Brain so every consumer (Echo, Lainey/Engage) inherits it.

## Client-facing Slack comes FROM ECHO (Blake, 2026-08-31, hard rule)
Any message to a gym owner or coach goes out as the Echo app, never as Blake and never
through the Claude Slack connector (that posts as whoever is driving it and stamps
"Sent using @Claude"). Use `scripts/echo_slack.py <channel_id> <message_file>` — it pulls
AGENT_SLACK_BOT_TOKEN from the deployed `echo` service in memory only. Verify the sender
first with `--whoami` (expect bot user U0BE39F02KV). The Claude connector is for internal
notes to LASSO staff only. Write the message to a file so the copy is reviewable before
it sends. Note: ~/scout-listener's Slack token is a DIFFERENT app (scout2), not Echo.

## Non-negotiable gates (never remove, never weaken)
- Approval gate default: every post waits for human approval. Approver Slack id: U06EPUUCL13.
  Exception: `AGENT_AUTO_APPROVE_ENABLED=true` (armed by Blake 2026-07-22) bypasses the
  card and publishes at schedule time, but still sends a lightweight "posted" notice to Slack.
  Do NOT add new auto-approve paths or weaken any other gate.
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

## Process guard: never run a destructive git command in a shared checkout (Blake, 2026-09-04)

Born from the third cross-session collision this week: a build agent ran `git checkout
origin/main -- .` inside a shared checkout that was on another session's in-flight
branch, believing it was cleaning its own scratch work. No data was actually lost that
time (the real WIP was already stashed and untouched), but the failure mode is real and
will eventually destroy something.

**Never run `git checkout <ref> -- .`, `git checkout <ref> -- <paths>`, `git reset --hard`,
`git restore`, `git clean -f`, or any other command that overwrites tracked files or the
index in a checkout you did not create for this task.** This applies to every shared
checkout on this machine (a repo's primary directory, e.g. `~/scout-listener`,
`~/lasso-echo-work`), not just ones with visible uncommitted changes — another session's
work can land there at any moment.

**If a task needs a clean tree, create a worktree.** `git worktree add <path> -b
<new-branch> origin/<base>` gives an isolated, disposable working directory backed by the
same repo. Do all work there. Never clean, reset, or destructively check out the shared
directory to get a clean starting point — branch a worktree from the ref you actually
want instead.

**Before touching any shared checkout at all**, run `git status --short` and `git branch
--show-current` first. If the branch is not the one you expect, or the tree is not clean,
stop and ask, or work in a worktree instead. Assume any shared checkout may belong, right
now, to a session you cannot see.
