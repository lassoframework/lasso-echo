# Lainey — full build handoff (for Codex recovery)

Compiled 2026-10-08 by Claude Code, at Blake's request, to hand the entire Lainey build to
Codex with nothing assumed from prior chat context. Everything below was verified by reading
the actual repos, the live GitHub issue/PR queue, and the committed docs — not from memory.
Where something could not be verified (mainly: true current live/production state, which
needs credentials this session does not have), it is flagged, not guessed.

**If you read nothing else, read this:** Lainey is not one small feature. It is a second,
separate, full-sized product repo — `lasso-engage` — roughly 800+ source files, 237 DB
migrations, 15,000+ tests, and over a month of near-daily build sessions. It is NOT the same
thing as the small Slack-support code in `lasso-echo` that also happens to be named "Lainey."
Confusing the two will cost you hours. See the map below.

---

## 1. The two-repo map

| Repo | What it is | Lainey's role in it |
|---|---|---|
| [`lassoframework/lasso-engage`](https://github.com/lassoframework/lasso-engage) (private) | **The actual Lainey product.** A multi-tenant AI lead-nurture agent for gyms, plugged into GoHighLevel (GHL). This is almost certainly the "$20k build" — it is a large, mature, independently-architected SaaS codebase with its own CLAUDE.md, its own CI, its own GitHub issue queue, and over 100 of its own internal handoff documents. | Lainey **is** this product — the name of the AI agent texting/calling leads. |
| [`lassoframework/lasso-echo`](https://github.com/lassoframework/lasso-echo) (public, this repo) | LASSO's **social media agent** (Echo). Separate product, separate purpose. | Lainey appears only as **one of five small internal "product agent" identities** in Echo's Slack support-ticket bridge (Echo/social, Lainey/engage, Scout/portal, Ranger/ads, Wrangler/websites). This is a thin Slack classification/reply-voice layer for routing *support tickets about Lainey*, not Lainey herself. It is small (~22.8K lines across 14 files, mostly tests) and **not armed/not live** — see §4. |

**Do not confuse the two.** If someone asks "where is Lainey," the answer is `lasso-engage`,
not this repo. This repo's Lainey code is a support-ticket routing stub that has never been
turned on.

A third repo, [`lassoframework/engage-flywheel-drill`](https://github.com/lassoframework/engage-flywheel-drill), also exists on the account (last pushed 2026-09-01) and was not opened in this pass — likely a drill/test harness related to Engage's "feedback flywheel" (see §2.6). Worth a look if you need that context.

---

## 2. `lasso-engage` — the real Lainey product

### 2.1 What it does (from the repo's own README)

> Multi-tenant AI lead-nurture agent for gyms. Plugs into any GoHighLevel subaccount, texts
> every new lead within 60 seconds, qualifies, books from real calendar slots, answers from a
> per-gym knowledge base, and hands off to a human the moment it should.

Pricing per CLAUDE.md (2026-09-08 ruling): **$199/mo for current LASSO ads clients, $299/mo
standalone** (sold direct to gym owners). Target: scale to 200 gyms.

### 2.2 How to get it

```bash
git clone https://github.com/lassoframework/lasso-engage
cd lasso-engage
cat CLAUDE.md          # read this FIRST — it is the actual source of truth, 1,350+ lines
cat README.md          # quick start, status, operating runbook
cat handoff/*.md        # ~127 historical build-session handoff docs (see §2.7)
```

Private repo — Codex will need GitHub access granted the same way this session got it
(`lassoframework/lasso-engage`, via whatever repo-attach mechanism Codex's environment uses).

### 2.3 Stack (from CLAUDE.md, "do not relitigate")

- Node 22+ / TypeScript, ESM, pnpm monorepo
- Fastify (API) + BullMQ/Redis (queues, debounce) + Postgres via Supabase
- Claude API: `claude-haiku-4-5` default, Sonnet escalation, prompt caching on a shared prefix
- Deploy: Railway — 4 services (api, worker, dashboard, harness), config in `.railway/railway.ts`
- GHL integration: Private Integration Token (PIT) per subaccount today; public marketplace
  OAuth app (Ed25519-signed webhooks) is built but not shipped (gate, see §2.5)
- Voice: Retell (built, **inert** — gated on legal consent language, see §2.5)

### 2.4 Architecture (from CLAUDE.md)

```
GHL workflow webhook ──► POST /webhooks/ghl/:locationId/:secret   (ack < 100ms)
    │ verify secret (timing-safe) · tenant status gate
    │ dedupe: webhook_events INSERT ON CONFLICT DO NOTHING
    ▼
Redis lastInbound marker + BullMQ delayed job (25s debounce, per-contact jobId)
    ▼
Worker: per-conversation Redis lock ─► agent loop
    build context (cached shared prefix + gym config block + GHL history)
    Claude tool loop: get_free_slots, book_appointment, move_pipeline_stage,
                      add_tag, update_contact_field, escalate_to_human
    ▼
Outbox: governor + guardrails pre-send (consent, frequency caps, DND, quiet hours,
2-segment cap, KB-grounding, ai-off tag check, turn-sequence guard, dedup)
─► humanized 30–120s delay ─► GHL conversations send ─► messages_audit
```

Five "hard rules" govern everything (CLAUDE.md top): one brain owns all outbound (the
`ai_engaged` tag suppresses the gym's own workflows mid-conversation), never double-text,
no invented facts, every reply inside 2 minutes 24/7, and strict multi-tenant isolation by
`location_id`. These are treated as inviolable and are enforced in code, not just policy —
read CLAUDE.md's "Things that look fine and are not" section (line 454+) for the list of
subtle ways each of these broke in the past and how it was fixed.

### 2.5 Status — what's actually built vs. gated vs. live

The repo tracks this itself via a generated file, `readiness-manifest.json` (script:
`scripts/readiness-manifest.mjs`). **The copy in the checked-out snapshot is stamped
`generatedAt: 2026-09-21T17:14:08Z`** — 17 days stale relative to today. Re-run
`pnpm readiness:manifest:live` (needs live DB/Redis credentials) to get a current read before
trusting any status claim, including the ones below.

As of that snapshot:

**Tests:** 15,255 total across the workspace (14,923 in `@lasso/core` alone), 15,048 passed,
0 failed, 207 skipped.

**Declared readiness gates** (`readiness-manifest.json` → `declared.gates`):

| Gate | Status | Blocker |
|---|---|---|
| `pilot-credential` | 🟢 green | — |
| `pii-audit` | 🟢 green | — |
| `brain-audit` | 🟢 green | — |
| `billing` | 🟡 amber | Stripe billing surface exists ($199/$299 policy) but paid rollout is incomplete |
| `text-consent` | 🔴 red | No pilot lead form carries the AI-text consent paragraph yet. Draft exists at `docs/consent-language.md` (variant B, doesn't need counsel) but isn't deployed |
| `marketplace-oauth` | 🔴 red | No public marketplace OAuth app — every gym outside LASSO's own agency needs a hand-created Private Integration per subaccount |
| `voice-counsel` | 🔴 red | Counsel has not reviewed the AI-call consent language (TCPA, ~$500–1500/call exposure). Voice is fully built but switched off at two separate gates |

**Launch gates still open as GitHub issues** (see §2.6 for live links):
- **#10** — sandbox needs a location-scoped GHL PIT (blocks live end-to-end testing)
- **#11** — AI-text/AI-call consent language on pilot lead forms (blocked, compliance, launch-gate label)

**The one-sentence truth from the project's own historical handoff** (2026-08-31,
`SESSION-HANDOFF.md`, now marked superseded but the sentence likely still rhymes):
> The machinery is built, tested and audited to a standard I am comfortable defending; the
> remaining distance to a pilot is one GHL credential that only a human can create, one
> paragraph of consent language, and one audit that needs the rate limit to reset.

By September the build had grown enormously past that snapshot (win-back, heartbeat/reactivation
cadence engine, brain/RAG retrieval, multi-tenant brain namespacing, email channel, FB/IG DM
channel, marketplace OAuth app, feedback flywheel, engagement-pattern mining, backtesting
against real history, a whole release/readiness-verification pipeline). **Whether a real gym has
ever actually gone live is NOT confirmed by this pass** — `handoff/GO-LIVE.md`,
`handoff/GO-LIVE-STATUS.md` (2026-09-10) and the `lainey:golive`/`lainey:status` pnpm scripts
are the places to check, and per that doc's own rule, every "live" claim must be re-verified by
a fresh read against production, never assumed from an old doc.

### 2.6 Current active work — the real frontier (verified live via GitHub, 2026-10-08)

This is the most reliable signal of what Codex should pick up, because it's live API data,
not a static doc.

**18 open issues**, https://github.com/lassoframework/lasso-engage/issues — notably:
- Issues **#1–#9, #14, #17, #19–#23**: a mix of `money-path`, `phase:P2.5/P3/P5`, `ops`,
  `compliance` work, several explicitly deferred/blocked (not urgent).
- **#90** (2026-09-11): the live model-graded eval suite ran for real for the first time and
  found **18 previously-invisible content-quality failures** (mostly a banned-punctuation
  regex surviving redraft, plus 3 prompt-injection/jailbreak-resistance scenarios that
  probably deserve priority). Triage decision not yet made as of this issue's last update.
- **#10, #11**: the two launch-gate blockers above.

**7 open pull requests**, https://github.com/lassoframework/lasso-engage/pulls — this is
where it gets interesting:

| PR | Title | Base → Head | Status |
|---|---|---|---|
| [#185](https://github.com/lassoframework/lasso-engage/pull/185) | Harden Lainey limited-launch intake, sender safety, and release evidence | `release/readiness-d4ecacc-20260924` → `codex/lainey-ci-recovery-20260924` | **Draft, updated 2026-10-08 (today)** — the active frontier |
| [#186](https://github.com/lassoframework/lasso-engage/pull/186) | Support pinned GitHub CLI and preflight drift-reader authority | builds on #185's branch | Draft, incremental on #185 |
| [#187](https://github.com/lassoframework/lasso-engage/pull/187) | Document Astra-led, Kimi-first parallel build policy | builds on #185's branch | Draft, process/docs only |
| [#189](https://github.com/lassoframework/lasso-engage/pull/189) | Review staged operator-only drift snapshot route (**disabled**) | `release/readiness-fec7b8bd-20260925` → further out on the same chain | Draft, explicitly "do not merge, activate, or deploy" |
| [#125](https://github.com/lassoframework/lasso-engage/pull/125) | Fix SMS/Linq replies incorrectly paused by automated CRM messages | `main` → `codex/lainey-sms-workflow-recovery-20260915` | Draft, open since 2026-09-15 |
| [#122](https://github.com/lassoframework/lasso-engage/pull/122) | Repair Lainey delivery safety, owner onboarding, and evaluation validity | `main` → `codex/flagship-audit-20260914` | Draft, open since 2026-09-14, very large |
| [#121](https://github.com/lassoframework/lasso-engage/pull/121) | Commit existing project rules for Codex | `main` → `codex/agent-rules-20260914` | Draft, trivial (just commits AGENTS.md) |

**Read this carefully — it matters:** CLAUDE.md (§"How work moves," line 1072) describes a
standing **autonomous verifier-gated auto-merge pipeline**: a BUILDER opens a PR, a fresh
independent VERIFIER grades it against an 8-point rubric, and it auto-merges at a grade of "A"
with no human in the loop except a 24-hour veto window on anything that weakens a safety rule.
**None of the 7 open PRs above have merged, several have sat open for 3+ weeks, and PR #185 is
still being actively amended today.** That is either (a) genuinely still in flight / failing
the verifier gate honestly, or (b) a sign the auto-merge pipeline itself has stalled or isn't
running. **This is the first thing Codex should figure out** — read the latest comments on
#185/#122/#125, check whether a verifier agent has actually graded them, and check
`handoff/KNOWN-INFRA-GAPS.md` + `scripts/check-known-infra-gaps.ts` (the allowlist mechanism
CLAUDE.md describes for distinguishing real regressions from known infra flakiness).

PR #185's own description is worth reading in full on GitHub — it documents a long chain of
independent CI runs, signed-artifact verification, and release-evidence tooling that's been
built around getting this specific PR safely to production. It references **258 migrations**
applied as of its candidate commit (vs. 237 in this clone's snapshot — confirming there is
newer committed work than what a shallow default clone shows by file count alone; fetch full
history if you need exact parity).

### 2.7 Directory map

```
lasso-engage/
├── CLAUDE.md                 # 1,350+ lines — THE source of truth, read first
├── README.md                 # quick start, status, operating runbook, deploy
├── SESSION-HANDOFF.md         # historical snapshot (2026-08-31), explicitly superseded
├── readiness-manifest.json    # generated status/gates (see §2.5) — re-run, don't trust the snapshot
├── env.example                 # every secret/env var needed, heavily commented (18KB)
├── package.json                # root scripts: build/test/migrate/doctor/readiness/evals/...
├── apps/
│   ├── core/          # Fastify API + BullMQ worker + agent loop + repos (795 files in src/)
│   │                   #   src/agent/   — the Claude tool-use loop, guardrails, governor
│   │                   #   src/outbox/  — send pipeline, governor, dispatcher
│   │                   #   src/cadence/ — win-back + heartbeat/reactivation engine
│   │                   #   src/retrieval/ + src/brain/ — RAG/KB retrieval, multi-tenant namespacing
│   │                   #   src/voice/   — Retell integration (inert)
│   │                   #   src/email/   — email channel + deliverability gate
│   │                   #   src/marketplace/ — GHL OAuth app (not shipped)
│   │                   #   src/engagement/ — "what works" pattern mining + backtesting
│   │                   #   src/provisioning/ — tenant onboarding, harvest crawler
│   │                   #   src/infra/   — alerts, drift checks, env verification, smoke tests
│   │                   #   src/scripts/ — readiness-board, doctor, CLI operator commands
│   ├── dashboard/      # Next.js operator UI (88 files)
│   └── readiness/      # readiness tooling app
├── packages/
│   ├── ghl/            # typed GoHighLevel v2 API client (conversations, contacts, calendars, …)
│   ├── retell/         # typed Retell (voice) API client
│   └── github/         # GitHub API helper package (used by the release-evidence tooling)
├── db/migrations/       # 237+ append-only SQL files, applied via `pnpm migrate`
├── evals/               # 102 files — scripted model-graded conversation scenarios, CI gate
├── docs/                 # 209 files — architecture notes, runbooks, consent language, corpus docs
│   └── runbooks/          #   deploy.md, generic-production-release-package.md, first-gym-supervised-beta.md, etc.
├── handoff/               # 127 files — the FULL historical build-session record (see below)
│   ├── kimi-wave5-20260919/, kimi-wave7/, kimi-launch-wave2/4-20260919/20260920/
│   │                     #   multi-agent "swarm" audit waves — worth reading for independent
│   │                     #   adversarial findings against the build
│   ├── audits/            #   named capability/privacy/staging audits
│   ├── GO-LIVE.md, GO-LIVE-STATUS.md, LAINEY-FULL-RELEASE-*.md  # go-live gate tracking
│   └── CODEX-HANDOFF.md   # ← there is ALREADY a Codex-specific handoff file in this repo; read it
├── e2e/                  # Playwright specs
├── scripts/               # release/readiness/drift/migration verification tooling
└── claude/engage-buildout-plan.md   # the original build spec (phases P0–P5)
```

**`handoff/CODEX-HANDOFF.md` already exists in this repo** — written by a prior session
specifically for Codex. Read it before this document; it will be more current on the exact
mechanics of the release/verification pipeline than anything reconstructed here secondhand.

### 2.8 How work moves (process Codex should follow — CLAUDE.md line 1011+)

1. **The plan doc is the spec, GitHub Issues are the queue.** `claude/engage-buildout-plan.md`
   says what Engage is; issues say what's next. Code wins over docs when they disagree about
   something already built.
2. **Pull issues at session start:**
   `gh issue list --repo lassoframework/lasso-engage --label phase:P3`
3. **Close issues with a commit reference**, never because it "looks done":
   `gh issue close <n> --repo lassoframework/lasso-engage --comment "Closed by $(git rev-parse --short HEAD): ..."`
4. **Labels:** `phase:P2`–`P5`/`ops` (queue), `feedback:pilot` (auto-filed from live behavior),
   `compliance` (blocks launch), `money-path` (TDD required), `blocked`, `launch-gate`.
5. **The BUILDER/VERIFIER/MERGE/DEPLOY pipeline** (§2.6 above) is the standing process — read
   CLAUDE.md line 1072 in full before opening a PR.
6. **Never treat a doc as proof of live state.** CLAUDE.md line 906: "Nothing counts as done
   until it has executed once against a real tenant, output pasted [into the record]."

### 2.9 Secrets/credentials Codex will need (from `env.example`, not fully enumerated here — read the file)

Postgres (Supabase `DATABASE_URL`), Redis (`REDIS_URL`), `VAULT_KEY` (AES-GCM token vault,
32 random bytes base64 — **losing it makes every stored GHL token undecryptable**),
`ANTHROPIC_API_KEY`, optionally `OPENAI_API_KEY`-equivalent for judge/embedding calls
(`EMBEDDING_API_KEY` — note: **not** `OPENAI_API_KEY`, that name is read by nothing, a past
documented footgun), GHL PIT per tenant, Railway project access, and — per CLAUDE.md —
secrets live outside the repo at `~/LASSO/lasso-engage.env` (mode 600) on whatever machine
built this; that file was never committed and this session has no access to it. Codex will
need these re-provisioned or handed over directly by Blake.

### 2.10 Caveats on this section

- This pass **did not run** `pnpm install`, `pnpm test`, `pnpm doctor`, or any live-service
  check. Everything above is read from committed source and the live GitHub API, not executed.
- The clone used here is `--depth 1` (shallow) at commit `2af574e`. For full history (useful
  for understanding how we got here, or for `git blame`), do a full fetch:
  `git fetch --unshallow` (or a bounded `git fetch --depth=2000 origin main` if the repo's
  policy hooks block unshallowing).
- The readiness/status numbers above are a **17-day-old generated snapshot** (2026-09-21) read
  against a repo whose open PRs show work as recent as today (2026-10-08). Treat every status
  claim as "as of the snapshot," not as current truth, until re-measured.

---

## 3. `lasso-echo` — where "Lainey" also appears, and why it's different

This repo (the one this session is attached to) is LASSO's **social media agent**, Echo.
Lainey shows up here only inside the **Slack Conversational Adapter** — a FIXER-bus intake
adapter that lets Echo, Ranger, Scout, Wrangler, and Lainey each handle Slack support tickets
about their own product, routed by a shared `product` column and per-identity config.

### 3.1 What exists, file by file

| File | Lines | What it is |
|---|---:|---|
| `agent/slack_convo/identities.py` | 129 | Registry of the 5 bot identities (Echo/Ranger/Scout/Wrangler/Lainey) — tokens, product tag, reply-voice doc path, fixer channel env. Lainey's entry: `product="lainey"`, tokens `LAINEY_SLACK_BOT_TOKEN`/`LAINEY_SLACK_APP_TOKEN`/`LAINEY_SLACK_BOT_USER_ID` (all unset today), `reply_voice_doc="docs/slack_convo/lainey_reply_voice.md"`. **`ARMING_ORDER` puts Lainey last of the five, unarmed.** |
| `agent/slack_convo/adapter.py` | 1,756 | The actual Slack event handling, ticket routing, outreach, trust-ladder gating — shared code for all 5 identities |
| `agent/client_dm_support/lane.py` | 716 | Client DM support lane logic |
| `brains/support/lainey.md` | 31 | Lainey's per-agent "support brain" — tone/classification hints ONLY, never facts (hard schema separation, see D36/D39 in DECISIONS.md) |
| `docs/slack_convo/lainey_reply_voice.md` | 52 | The voice rules for anything Lainey posts in a client's Slack thread — distinct from, and does not override, the SMS/voice rules that actually govern Lainey-the-product in `lasso-engage` |
| `docs/slack_convo/DECISIONS.md` | 3,114 | Full decision log for the whole Slack Conversational Adapter build (not Lainey-specific, but documents every design choice, audit wave, and open ruling — D1 through D46+) |
| `tests/test_support_brain.py`, `tests/test_client_dm_lane.py`, `tests/test_slack_convo.py`, `tests/test_slack_convo_autonomy.py` | 218 / 1,022 / 5,271 / 2,003 | Test coverage for the above |
| `agent/config.py`, `agent/__main__.py` | (large, shared) | Wiring/config plumbing shared across all 5 identities, not Lainey-specific |

### 3.2 Status: not live

- Lainey has **no Slack bot tokens configured** — `brains/support/lainey.md` says explicitly:
  "Lainey is an SMS/voice persona with no Slack surface today... this brain is ready for when
  she gets one."
- Per `docs/slack_convo/DECISIONS.md` (the 2026-09-04 ruling section, D34+), arming
  Scout/Ranger/Wrangler/Lainey "one at a time, by Blake's own hand" was explicitly scoped
  **out** of that build — it's a manual action, not yet executed, and not something this repo's
  code does on its own.
- The actual FIX-execution path for any Lainey support ticket (`code_fix` tickets) depends on
  a Railway-hosted Claude Code executor that **does not exist yet** (tracked as "D3" across
  multiple decision entries) — so even if Lainey's Slack bot were armed, a code-fix ticket
  would queue correctly but nothing would execute it.

### 3.3 Why this matters for the handoff

If Codex's job is to "recover the Lainey build," **this repo's Lainey code is not it** — it's
a few hundred lines of unarmed Slack routing config that happens to share the name. The real
asset is 100% in `lasso-engage`. Don't spend recovery effort here beyond understanding that
this piece exists and is dormant.

---

## 4. Recommended next steps for Codex

1. **Get `lasso-engage` repo access** and read, in this order: `handoff/CODEX-HANDOFF.md` (it
   already exists, written for exactly this purpose), then `CLAUDE.md` in full, then
   `README.md`.
2. **Re-run the readiness manifest live** (`pnpm readiness:manifest:live`, needs credentials)
   to get a true current status instead of the 2026-09-21 snapshot used in this document.
3. **Figure out why PRs #121/#122/#125/#185/#186/#187/#189 haven't auto-merged** despite the
   repo's own documented auto-merge-at-A pipeline. This is the single highest-value thing to
   resolve — it's either blocking real progress or revealing the pipeline is broken.
4. **Check `handoff/GO-LIVE-STATUS.md` and run `pnpm lainey:status`** (live, needs
   credentials) to find out whether any real gym has ever actually gone live, and if so, which
   one and in what state.
5. **Get the credentials.** Per §2.9, the working secrets file (`~/LASSO/lasso-engage.env`)
   lived on whoever's machine built this and was never committed. Ask Blake directly for it,
   or re-provision from scratch using `env.example` as the checklist.
6. **Close the two launch gates** (#10 — sandbox PIT; #11 — consent language) if the goal is
   getting a real pilot gym live; both are explicitly human-only actions (a credential only a
   human can create, and consent copy that needs to be deployed to a live lead form).

---

## 5. Links (copy-paste ready)

- Lainey product repo: https://github.com/lassoframework/lasso-engage
- Issues: https://github.com/lassoframework/lasso-engage/issues
- Open PRs: https://github.com/lassoframework/lasso-engage/pulls
- Active frontier PR: https://github.com/lassoframework/lasso-engage/pull/185
- This repo (Echo, where Lainey's dormant Slack stub lives): https://github.com/lassoframework/lasso-echo
- Possibly-related drill/test repo, not yet opened: https://github.com/lassoframework/engage-flywheel-drill
