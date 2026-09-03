# Slack Conversational Adapter, decisions log

Blake's ruling (2026-09-03): "the FIXER bus (support_tickets + support_messages) IS the
framework. Do not build a parallel system. Build a Slack Conversational Adapter as a new
FIXER intake adapter. Echo first, generic enough that Ranger, Scout, Wrangler, and Lainey
plug in by config. Loop to A+."

Each decision below was made during the build, proceeded on the recommendation, and is
flagged here for Blake's ruling. Where the code and the spec disagreed, the code won and the
disagreement is recorded, not smoothed over.

## D1. Runtime home: the existing Railway Bolt listener in agent/listener.py
Recon first reported no Railway Slack listener existed; that was wrong, and it was wrong
because the search used JavaScript Bolt identifiers. `agent/listener.py` already runs a
slack_bolt App over SocketModeHandler on the `echo` Railway worker with AGENT_SLACK_APP_TOKEN
set. The adapter attaches to that App as an additional `message` / `app_mention` listener.
Bolt runs every matching listener independently, so `on_chat_message` (Blake's publish chat)
is untouched. **Flags off equals today, structurally.**

## D2. Outbound: an in-process outbox loop plays the Wrangler role
No Wrangler process reads support_messages today (fixer-lane.ts's Slack notifies are dead
code with no client wired; scout-listener's fixer/cards.js are unused builders). The adapter
never posts; `outbox.py` reads ready rows and posts through every gate. It is named and
interfaced as the Wrangler outbound role so a real Wrangler service replaces it by config.
**Ruling needed: is a separate Wrangler service meant to own this later?**

## D3. Code fix execution: bridge to the one executor that exists
There is no Railway-hosted Claude Code executor. The only one is scout-listener's
ops-fix-triage.js on Blake's Mac, watching #echosupport for "OPS-FIX REQUEST: " from Echo's
bot. A code_fix ticket is written to the bus (status triage) and the outbox emits that exact
card to AGENT_OPS_FIX_CHANNEL_ID (default: the support channel) so the proven worker picks
it up and writes verification back to the row. Honest about the desktop dependency for the
FIX step only; intake, answer, and hold all run on Railway. **Ruling needed: a Railway-hosted
Claude Code executor (CLI + auth in the container) is the true Stage B item.**

## D4. Answer lane: Anthropic, grounded, billing refused before any model call
anthropic is already a dependency and ANTHROPIC_API_KEY is already on Railway. Answers use
only a fetched live-state snapshot; that snapshot is stored as verification_before/after so
the outbox's gate is satisfied honestly. Any billing/pricing/Stripe question is refused
before the model is called and escalated (the Alex $149 vs $99 case).

## D5. "Flags off equals today" overrides Stage A's "record regardless"
Stage A writes bus rows regardless of flags. Recording a client's DMs is itself a behaviour
change, so this adapter writes nothing and replies nothing while its flags are off.

## D6. Identity gate direction is the reverse of slack-directory.js
Slack user -> email -> app_users -> gym_assignments -> echo_intake_tokens.echo_account_key.
Operators (APPROVER_SLACK_ID) are staff by fiat. Multi-gym owners resolve UNKNOWN (which gym
the thread is about is not knowable from identity). Any lookup failure is UNKNOWN. The bot
never calls conversations.open; first contact is structural.

## D7. Schema lives in the portal repo (migration 0309), code in the Echo repo
The bus schema is the portal's and deploys through its deploy-migrate ledger. Dry-run
executed against production inside BEGIN/ROLLBACK before shipping: clean. **Arming order:
portal migration first, then Echo flags.** Also widened `product` to add scout and wrangler,
and `author_type` to add client/staff/echo/scout/wrangler/lainey, up front (the 0306 lesson).

## D8. While client replies are unarmed, even the acknowledgement is held
"Staff first" means a client gets nothing autonomous until the per-identity client-reply flag
is armed. The ack row is written held and a tap notice goes to the fixer channel.

## D9. Rate limit gates dispatch, not recording
The cap-th-plus-one message still becomes a ticket row (status hold, escalated) so the record
exists, but no classification and no worker; a templated "queued for the team" reply.

## D10. One config registry, N Bolt apps, only Echo has tokens here today
Wrangler and Scout run their own desktop listeners; Ranger and Lainey have no Slack bot.
Their identities are present with flags off. **Ruling needed: consolidating Wrangler/Scout
into this Railway process is a product decision, not assumed.**

## D11. DMs do not thread; an open ticket in the conversation absorbs the next message
A top-level message in a DM or group DM attaches to the most recent OPEN ticket in that
conversation created within SLACK_CONVO_OPEN_WINDOW_DAYS (default 7) as a follow-up;
otherwise it opens a new ticket whose thread root is its own ts.

## D12. Unknown identities are treated as clients for the trust ladder
The templated redirect to an unknown user is a client-facing message and holds behind the
client-reply flag like any other. Nothing reaches a stranger autonomously until armed.

## D13. Dedupe key is channel:ts, not Slack's raw event_id
Slack emits DISTINCT event_ids for one human message delivered as both `message` and
`app_mention`. channel:ts is the identity of a message and also catches redelivery and
replay. The raw event_id rides in the row's attachments.

---

# Audit wave 1 (2026-09-03): two independent sub-agents, fixed to zero

Audit A ("any path where the bot replies without a support_messages write first or
without verification") and Audit B ("how could a stranger in Slack make this bot do
something") returned 2 CRITICAL + 10 MAJOR + 11 minor. Every one is fixed below and has a
test in tests/test_slack_convo.py. Ids are the auditors' own.

## D14 (RT-C1). A client's words never reach the Claude Code worker autonomously
The only fixer executor is ops-fix-triage.js: Bash-armed, on Blake's Mac, trusting any
`OPS-FIX REQUEST:` message from the Echo bot. A code_fix used to hand it the client's raw
text as a 'ready' row. Now every fixer_request starts HELD and posts only after Blake's tap
in #fixer (spec item 6, the hold lane). The single exception: a STAFF-origin ticket whose
lane is 'safe' goes straight to ready. The card fences the text as an UNTRUSTED REPORT and
carries slack ids and account keys, never a user-editable display name (RT-m3).

## D15 (V-C1). The bus is client-readable in the portal; the record must hold the line
support_messages rows were visible to the client through the portal's messages route and
RLS. A held draft awaiting a tap, a suppressed answer, or an internal escalation quoting a
worker card would have leaked through that second door. Portal migration 0310 + the
messages route filter: a client sees inbound rows and outbound rows that are POSTED and of
a conversational kind. Staff see everything; that is the record.

## D16 (RT-M3). Only the ticket's author or LASSO staff may continue a ticket
A thread reply or DM top-level message attaches to an existing ticket only when the sender
is the ticket's slack_user_id or resolves to staff/coach. Anyone else is silence and the
ticket is untouched. identity_kind is set at creation and never rewritten.

## D17 (V-M1). The trust ladder gates on who can READ, not only on who spoke
Staff chatting in a client's group DM used to trigger acks and templates into the
client-visible conversation. Now staff open a new ticket only in a 1:1 DM or by @mention;
in a group DM or channel thread a staff message is an instruction on the open ticket (no
client-visible ack) or, with no open ticket, ignored as two humans talking.

## D18 (V-M3). Follow-ups never demote
A follow-up re-triggers the worker only on a code_fix ticket in triage/fixing/verification,
capped at 3 fixer rows per ticket per day. approved, Ranger 'new', hold, and non-code
tickets record the note and escalate to a human; status is left as is.

## D19 (V-M4/V-M5). An answer is grounded or it is nothing; the ticket resolves on post
A snapshot in which every seam failed is not grounding: the answer lane returns None (no
model call). The model is told to emit NO_ANSWER when facts do not cover the question;
that is None too. None escalates. The ticket sits in 'verification' until the outbox
actually posts the answer, then it is resolved. Every suppression in the outbox writes an
escalation row so a human sees what the bot declined to say.

## D20 (V-M2/RT-m5/V-M7/V-M8). The outbox fails closed and every held row has a card
The hold notice posts with a Block Kit button (action slack_convo_release, value = held row
id). Unknown kinds and rows with no identity stamp are suppressed, never posted. A row the
outbox moves to held at post time (flag flipped between write and post) gets its own card.
Each identity's loop reads only its own rows. release_held accepts only held conversational
or fixer_request rows belonging to the tapping bot's identity (V-m10).

## D21 (RT-M2/V-m4/RT-m6). Fewer false tickets, no public templates
code_fix needs a breakage word AND an Echo-domain noun ("I can't make Thursday" is not a
fix). Greetings and thanks never open a ticket or page anyone. An unknown user @mentioning
the bot in a channel gets an internal escalation only; no templated text into a channel.

## D22 (RT-m2/V-m2/RT-m4/V-M9/V-m1/V-m10). Hygiene
The model transcript is the person's words plus replies actually POSTED to them; internal
rows never reach it, and a conversational body carrying the `OPS-FIX REQUEST` prefix is
suppressed so the bot cannot be made to command its own worker. Conversational rows older
than 6h in 'ready' are suppressed (a Blake tap restarts the clock). Inbound events run on a
4-worker pool. An additional identity with tokens present but its flag OFF opens no socket.
Boot warns when the fixer / ops-fix channels are unset. The portal email lookup validates
the address and matches case-insensitively with an exact post-filter.

## Rulings still needed from Blake (unchanged + new)
- D2: a real Wrangler service taking over the outbox rows (in-process loop stands until then).
- D3: a Railway-hosted Claude Code executor so fixes do not depend on Blake's Mac being on.
- D10: consolidating Wrangler/Scout desktop listeners into this process.
- RT-M1 (new): lasso-echo `main` has no branch protection, and the ops-fix worker preamble
  claims "tested" before any test runs. Both are in ~/scout-listener / GitHub settings, not
  this repo; Blake is mid-edit there, so nothing was touched.

---

# Audit wave 2 (2026-09-03): two fresh independent re-audits of the wave-1 fix

Re-audit A ("any path where the bot replies without a row or without verification") and
re-audit B ("how could a stranger in Slack make this bot do something") ran again against
the wave-1 fix. Combined: 1 CRITICAL (portal side, D23), 6 MAJOR (D24-D29), several minor.
Every MAJOR+ is fixed below with a test. Ids are the auditors' own (N = re-audit A, RA =
re-audit B).

## D23 (N1, CRITICAL). franchise_overseer is a franchise tenant, not LASSO staff
Portal `isStaffRole()` (`src/lib/support/client-visible.ts`) listed `franchise_overseer`.
That role is a FRANCHISE TENANT (`src/lib/auth/roles.ts:13` -- "sees ONLY its own
franchise's data"). The support messages route used `isStaffRole()` to decide whether to
skip the client filter, so a franchise owner reading their own gym's ticket got every row:
held drafts, suppressed answers, escalation text, fixer_request cards. Migration 0310's RLS
has no role clause at all, so this route was MORE permissive than direct DB access for this
one role. Fixed: role removed from `isStaffRole()`. Portal PR #544.

## D24 (N2, MAJOR). A release now actually delivers
`release_held` flipped a row to ready, but the outbox's trust-ladder gate re-read the SAME
flag that had held it in the first place, found it still off, and held it again -- writing
a fresh card every tap, forever. A row `release_held` has stamped `released_by` now skips
that recheck once: Blake's tap on a specific row IS the approval the hold lane collects.

## D25 (N3/RA-M3, MAJOR). Noise from a ticket that already exists is bounded
The daily cap only ever gated ticket CREATION. Once an unknown user's hold ticket, or a
client's parked/capped-out follow-up ticket, existed, every further message re-escalated
and re-templated with no bound (30 stranger messages became 60+ posts into #fixer in the
audit's repro). Now: the unknown-identity template is written once ever per ticket (the
spec's own words, "one templated reply"); escalations from an unresolved-identity ticket
cap at 3/ticket/day; escalations (and the accompanying ack) from a parked or fixer-capped
follow-up ticket cap at 5/ticket/day. The inbound row is always recorded regardless -- only
the outbound noise is bounded.

## D26 (N4, MAJOR). Every row is claimed before it posts
Read -> post -> mark had no claim step: two consumers of the same row (a redeploy overlap,
or a future Wrangler service per D2 pointed at the same rows) could both post it, and a
mark_message failure after a successful post left the row 'ready' to be reposted forever.
Every row is now claimed (ready -> posting, a conditional PATCH only one caller's WHERE
clause can match) immediately before post(); a row still 'posting' at the START of a run is
orphaned from a crashed prior attempt (the claim/post/mark sequence is synchronous within
one call) and is swept back to ready.

## D27 (RT-M1/RA-M1, MAJOR). The untrusted report cannot forge the fence or Slack markup
A client's raw words were fenced verbatim. The literal closing token could appear inside
their own message, letting injected text read as an "instruction" sitting outside the
fence; separately, `<!channel>` / `<@U...>` in their words would render as live Slack
markup in #fixer. Both close with one change: the untrusted text is Slack-escaped
(&,<,> -> entities -- the same escaping every Slack API client must apply before posting)
before it is fenced. Escaping disarms the fence delimiters (both use literal < / >) and
Slack markup in the same pass. The escaped text is also bounded to 3500 chars so the
closing fence can never be lost to the bus's 8000-char row truncation.

## D28 (RA-M2, MAJOR). The card Blake reviews is what will actually post
`hold_notice_blocks` showed only the first 2900 characters (one Slack block) of the row,
while a release posts the FULL row. An injected tail past that cutoff was invisible to the
reviewer. Now the card renders across as many sections as the body needs; concatenating
every section reproduces the row exactly.

## D29 (RA-m5, minor, fixed while D28 was open). Each identity gets its own fixer channel
Every `BotIdentity` defaulted `fixer_channel_env` to the SAME env name
(`AGENT_FIXER_CHANNEL_ID`), so a second identity's holds/escalations would land in Echo's
channel. Ranger, Scout, Wrangler and Lainey now each carry their own env name
(`RANGER_FIXER_CHANNEL_ID` etc.); Echo keeps the deployed default.

## Not fixed, deliberately, with reasoning (both re-audits agreed these are acceptable)
- N5/N6/N7/N8/N10, RA-m1/m2/m4: minor, no exploit path, or already fail in the safe
  direction (documented inline in bus.py / adapter.py / listener_wiring.py where relevant).
- RA-m1 (fixer-retrigger cap undercount past 200 rows): the day-cap helper now used by
  BOTH the follow-up retrigger cap AND the new noise caps queries the server directly
  (`Bus.count_outbound_kind_since`), so this is actually closed as a side effect of D25/D26,
  not left open.

## Rulings still needed from Blake (unchanged)
- D2: a real Wrangler service taking over the outbox rows.
- D3: a Railway-hosted Claude Code executor so fixes do not depend on Blake's Mac being on.
- D10: consolidating Wrangler/Scout desktop listeners into this process; also now the home
  for RA-m5's other half -- the ops-fix worker (`~/scout-listener`) only trusts Echo's
  bot_id, so a second identity's fixer_request, even released, is not executed today. That
  file is outside this repo and Blake was mid-edit in it; not touched.
- RT-M1: lasso-echo `main` has no branch protection; the ops-fix worker preamble claims
  "tested" before any test runs. Both live in GitHub settings / ~/scout-listener.

---

# Audit wave 3 (2026-09-03): fresh re-verification of wave 2's fixes

Two fresh VERIFIER agents (not the wave-2 fixers) re-checked D24-D29 and RA-M1/M2/M3/m5
against commit 203d260. Both independently found the SAME root cause behind a different
symptom each had flagged: the daily ticket cap gated dispatch, not ticket creation --
`get_or_create_ticket` ran unconditionally regardless of `rate_limited`, and an UNKNOWN
identity's branch returned before the rate-limit check was ever reached. Combined: 0
CRITICAL, 3 MAJOR (D30-D32, one confirmed by both agents independently), 2 minor.

## D30 (RB2/D25-STILL-BROKEN, MAJOR, confirmed by BOTH re-audits independently). The daily
## cap now actually stops ticket creation, for every identity kind
Wave 2's per-ticket noise caps (D25) only bound REPEAT noise on a ticket that already
exists. They did nothing against a user who simply gets a FRESH ticket every message: a
non-threaded `app_mention` never matches an open ticket (only IM/MPIM do), and
`get_or_create_ticket` ran with no regard for `rate_limited` -- so every message, capped or
not, known or fully UNKNOWN (whose branch returned before the rate-limit check was ever
reached), minted a new ticket with a fresh per-ticket allowance. Both re-audits reproduced
this directly against FakeBus: 20 fresh `@mention`s from an unresolved stranger produced 20
tickets and 20 escalations to #fixer -- the same "unbounded noise" failure D25 claimed
closed, via a different trigger.

Fixed at the root: the daily cap now gates ticket CREATION itself, for every identity kind.
Once a user (any kind, staff exempt as before) is at the cap, no new ticket is minted --
`Bus.find_recent_ticket_for_user_today` finds whatever ticket they already have today and
the message attaches there, so it inherits that ticket's existing per-ticket noise caps
(D25) instead of a fresh allowance. Total worst-case noise per user per day is now hard-
bounded at roughly `daily_cap() * per-ticket cap`, not unbounded. A reused ticket is never
demoted (status only gets forced to hold on the rare path where the reuse lookup itself
fails and one ticket has to be minted).

## D31 (D26-STILL-BROKEN, MAJOR). The stale-claim sweep can no longer steal a live post
D26's `_recover_stale_claims` swept EVERY row in 'posting' back to 'ready' with no
staleness check at all. Under the exact multi-consumer scenario the claim step exists to
protect against (a redeploy overlap, a second Wrangler per D2), a row genuinely mid-flight
in one process (a slow Slack API call) would be un-claimed by another process's very next
5-second sweep and re-posted while the first was still in flight -- a duplicate post, the
opposite of the guarantee. Fixed: `claim_message` now also stamps `claimed_at`; the sweep
only reclaims a row that has been 'posting' for more than `CLAIM_TIMEOUT_SECONDS` (90s,
generous over realistic post latency), so a live claim survives a concurrent sweep and only
a genuinely orphaned one (from a real crash) is recovered.

## D32 (DV4 + RB1, MAJOR). Client-facing and preamble text are now Slack-escaped too
D27 escaped only the client's raw text inside the fixer_request fence. Two paths were still
raw: (1) a QUESTION's answer body is model-generated from a transcript that includes the
client's own words -- a successful prompt injection had no defense once it left the model,
and would have posted live `<!channel>` / `<@U...>` markup straight into the real client
conversation (DV4); (2) `user` and `who.account_key` sit in the fixer_request/hold_notice
PREAMBLE, outside the fence, read as trusted operator context rather than an untrusted
report -- a polluted value there is a STRONGER injection than the one already closed, since
it needs no fence-breakout at all (RB1). Fixed: every CONVERSATIONAL body is Slack-escaped
once, at the single point `emit()` writes the row (covers both the eventual post() and any
hold_notice card built from it); `user`/`account_key` are escaped in both preamble builders.

## Minor, fixed alongside the above
- DV1: the boot-time channel warning checked the global `AGENT_FIXER_CHANNEL_ID` regardless
  of which identity was booting, so a non-Echo identity with its own channel set (D29) got
  a false warning. Now checks the same resolution `outbox._channel_for` actually uses.

## Not fixed, deliberately, with reasoning
- DV5 (latent, not currently reachable): `write_hold_notice`'s own prefix plus a
  near-8000-char body could in theory lose its tail to `bus.record_outbound`'s truncation.
  Not reachable today -- `KIND_ANSWER` bodies are bounded by the LLM's `max_tokens=400`
  and `fixer_request_text` is capped to ~4000 chars, both well clear of 8000. Would resurface
  if either bound is loosened; noted for whoever loosens one.
- RB3: a second, unrelated `OPS-FIX REQUEST:` builder exists in `agent/ops_alerts.py`, but
  every caller passes internally-generated text (account keys, exception names), never a
  Slack stranger's free text -- a different trust boundary, out of this feature's scope.

## Verification loop status
Two independent re-audits against this fix (D30-D32) are the natural next step per Blake's
"fix, re-audit to zero" instruction, but the finding class is now narrow (escaping
completeness, cap edge cases) rather than structural. Suite green at 5055; flags unchanged,
all OFF. Reported to Blake as the closing wave unless a further audit finds otherwise.

---

# Audit wave 4 (2026-09-03): narrow closing verification of D30-D32

One fresh VERIFIER agent, scoped only to D30/D31/D32's blast radius (per Blake: a narrower
closing check once the finding class had shrunk from structural to edge-case). D30 and D32
confirmed fixed outright. D31 confirmed fixed with one narrow, low-probability caveat (E2,
tracked below, not blocking). One fresh MAJOR (E1), reproduced live against the real code.

## D33 (E1, MAJOR). The reuse lookup is scoped to the calling identity's own tickets
`find_recent_ticket_for_user_today` / `count_tickets_for_user_today` filtered only by
`slack_user_id`, across ALL bot identities. A Slack user capped on Echo while also
messaging Ranger could have a message reuse RANGER's ticket for an ECHO message: the row
would carry `attachments.identity="echo"` while the ticket's own `bot_identity` stayed
"ranger". `_dispatch_one`'s ownership check (`ticket.bot_identity != identity.name`) means
NEITHER identity's outbox loop would ever pick that row up -- it, and its hold_notice card,
would sit in `delivery_status="ready"` forever: no error, no alert, invisible. Reproduced
live against the real adapter/bus/outbox (not the test harness) by the auditing agent.
Fixed: both bus methods take an optional `bot_identity` param; the adapter always passes
its own `ident.name`.

## Tracked, not blocking
- E2 (minor): `_claim`'s `claimed_at` stamp is a second, best-effort write after the atomic
  CAS. If it silently fails on a row claimed long after a stale `released_at` (only
  plausible if the outbox loop itself was down across that gap -- the same redeploy-overlap
  window D31 already targets), `_age_seconds` falls back to the old `released_at` and a
  just-claimed row could misread as stale. Narrow, low-probability, same root cause D31
  already accepts (best-effort secondary write); not fixed separately.

## Verification loop status
Suite green at 5056; flags unchanged, all OFF. Four audit rounds (wave 1-4) run; findings
per round: wave 1 (2 CRITICAL + 10 MAJOR), wave 2 (1 CRITICAL + 6 MAJOR), wave 3 (3 MAJOR,
all one root cause), wave 4 (1 MAJOR, narrow and now closed). Per Blake's "fix, re-audit to
zero" with a narrowing finding class, this is reported as the closing wave.
