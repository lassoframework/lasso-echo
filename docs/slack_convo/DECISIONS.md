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

---

# Ruling change (2026-09-04): Wrangler becomes a product agent; per-agent brains; outreach

Blake's ruling, verbatim (five items): (1) reverse D1 -- rename the headless dispatcher to
"fixer" (a same-day naming correction from an initial "bus" pass, see D37); Wrangler
becomes the website support agent identity, one of five product agents (Echo/social,
Lainey/engage, Scout/portal, Ranger/ads, Wrangler/websites), routed by product with no
cross-agent posting. (2) a per-agent support brain, `brains/support/<agent>.md`, that
shapes classification and reply style only, never facts. (3) ticket-initiated outreach:
a non-Slack-sourced ticket that resolves to a known client opens a group DM
(client + Blake + the owning agent) and the DM becomes the ticket thread -- reversing D6
for exactly this path. (4) the thread loop (client reply -> re-trigger -> fix -> verified
reply) up to what D3 (the Railway executor) blocks. (5) arming the other four identities,
one at a time, by Blake's own hand -- not built here.

This build (this session) covers items 1-4's code/tests; item 5 is Blake's manual action
and is reported on, not executed, below.

## D34. Routing map keys are the REAL product values in use today, not the ruling's
## business-description labels
The ruling names the five agents' domains as "websites / social / engage / portal / ads".
The scope note handed to this build named only ONE literal `product` column change:
"Wrangler's entry needs product retargeted from 'wrangler' to 'websites'". Echo, Ranger,
Scout, and Lainey's `identities.py` `product` fields were already self-referential
(`echo`/`ranger`/`scout`/`lainey`) before this change, and at least one other system reads
one of those values literally today: the portal's Ranger cron (`fixer-lane.ts`, per
adapter.py's own comment) polls `support_tickets` on `product='ranger'`. Renaming all five
products to the ruling's business labels (`social`/`engage`/`portal`/`ads`) would silently
break that consumer and any other repo's code that matches on `product='echo'` /
`'scout'` / `'lainey'` -- a change with a blast radius outside this repo and outside what
was explicitly asked for.

Resolved conservatively: `agent/slack_convo/routing.py`'s `PRODUCT_TO_IDENTITY` map is
keyed on the ACTUAL product values (`websites`, `echo`, `ranger`, `scout`, `lainey`), with
only Wrangler's `identities.py` entry changed as instructed. `route()` has NO fallthrough
branch regardless -- an unmapped product always raises `UnroutableProduct`, never guesses.
**Ruling still needed from Blake**: does he want the other four identities' `product`
columns literally renamed to `social`/`ads`/`portal`/`engage` too, coordinated with a
change to the portal's Ranger cron query and any other consumer? Flagged, not silently
decided either way.

## D35. Ticket-initiated outreach: narrow, defensive, and it reuses the existing
## group-DM-includes-Blake pattern rather than reinventing it
New module `agent/slack_convo/outreach.py`. This is the one deliberate reversal of D6
("the bot never calls conversations.open; first contact is structural") -- scoped exactly
to Blake's own words and no further:

- Fires ONLY for a ticket whose `source` is on an explicit allowlist
  (`portal_form`, `engage_tenant_event`, `website_intake`) -- an unrecognised source
  refuses rather than being guessed as "probably non-Slack". A new intake source must be
  added to this allowlist deliberately.
- Refuses on ANY unresolved identity: `who.kind` must be `CLIENT`. `identity_gate.py`
  already folds the ambiguous multi-gym-owner case into `UNKNOWN` (see D6's own docstring
  and the `resolve()` implementation), so there is no separate "ambiguous" branch to
  handle here -- `UNKNOWN` alone covers it, with a test proving that specific path.
- Refuses when the reporter is not the client: a `STAFF`/`COACH`-resolved identity can
  never be the outreach recipient (never "staff filed on behalf of"), and a `CLIENT`
  identity is only eligible when the ticket's own `reporter` field (email, matched
  case-insensitively) or `slack_user_id` matches that resolved identity -- never a client
  identity resolved for someone OTHER than who the ticket says asked.
- Reuses, rather than reinvents, the group-DM-includes-Blake pattern already proven in
  the portal (`lasso-ops-portal/src/lib/replies/digest-dm.ts`:
  `resolveDigestDestination` / `openEchoGroupDm` / `postAsEchoApp` / `sendDigestDm`,
  itself Blake's own 2026-09-01 ruling on the daily reply digest). Same shape: exactly
  `[BLAKE_SLACK_USER_ID, client_slack_user_id]` passed to `conversations.open` on the
  OWNING agent's own bot token (never a bare 1:1 client DM, never Blake's token) -- Slack
  adds the calling bot as the third member automatically.
- Row-first even on this one outbound-first path: the ack row is written via
  `record_outbound` BEFORE the live post, same invariant as everywhere else in this
  adapter.
- "The group DM thread becomes the ticket thread" is implemented literally: on a
  successful open + post, `stamp_ticket()` sets the ticket's `slack_channel_id` (and
  `bot_identity`/`slack_user_id`/`identity_kind`) to this new DM, so the client's NEXT
  message in it is picked up by `adapter.handle_event`'s EXISTING MPIM path
  (`match_surface` -> `find_open_ticket_in_conversation`, which matches on
  `slack_channel_id` alone, per D11 -- DMs never thread) with zero new matching code.
  A `stamp_ticket` failure is logged, never raised: the DM was already sent and must not
  be treated as if it silently failed.
- The first message is Slack-escaped nowhere extra because it is entirely
  template-composed from `ident.name` and a clipped, non-model-generated excerpt of the
  ticket's own `raw_text` -- no model call, no untrusted-text-as-instruction surface on
  this path (unlike `fixer_request_text`, which fences a person's free text).

Tests: `tests/test_slack_convo_outreach.py` -- both of Blake's named refusal paths
("outreach refuses on unresolved identity", "outreach refuses when reporter is not the
client") plus the ambiguous-multi-gym-is-UNKNOWN path, the reporter-match-by-email and
by-slack-id paths, row-first ordering, the stamp-ticket wiring, and that a stamp failure
never un-sends an already-posted message.

## D36. The support brain is a hard schema separation, not a promise
`agent/slack_convo/brain.py` + `brains/support/<agent>.md` (one per identity, mirroring
the tenant-brain directory pattern under `brains/`). `BrainHint` -- the ONLY shape this
module can return -- has exactly three fields: `tone_notes`, `classification_hints`,
`common_phrasings`. There is no `facts`/`answer`/`context`/`snippet` field, and
`answer_lane.py` (the only place a factual reply BODY is generated) has ZERO import of
this module -- `tests/test_support_brain.py::test_answer_lane_module_does_not_import_the_
brain_at_all` asserts the source text directly, so a future edit that tries to wire brain
content into the model's factual context fails the moment that import is added, not just
on review. The one section this module appends to automatically from resolved tickets
("Learned from resolved tickets") is deliberately never parsed into any returned field --
`test_learned_section_is_never_parsed_into_any_returned_field` writes a poisoned entry
containing a fake "FACT: the price is $1" instruction and asserts it cannot appear
anywhere in the parsed hint.

**Not wired into `classifier.py` or the reply-generation path in this build.** Blake's
ruling says the brain "shapes classification and reply style", which implies an eventual
call from `classifier.classify()` to `brain.load_hint(ident.name).classification_hint_for(text)`
as an advisory signal, and from the answer lane's *prompt-construction* (tone only, never
merged into the factual snapshot) to `hint.tone_notes`. Given the CLAUDE.md fabrication
gate and the scope note's emphasis on "hard schema/interface separation", this build ships
the isolated, tested module and the seeded files but does NOT modify `classifier.py` or
`answer_lane.py` to consume it yet -- wiring a NEW input into either of those two gated
modules is exactly the kind of change the Big Build Protocol says needs its own audit
pass, not a same-session bolt-on. **Ruling needed from Blake**: wire `classifier.py`'s
classification call to consult `brain.classification_hint_for()` as an advisory signal
(never overriding the classifier's own regex/keyword decision, per
`test_classifier_module_may_reference_brain_only_as_an_optional_advisory_hint`'s
documented boundary), and/or fold `tone_notes` into the reply-voice instructions
`answer_lane.py` already sends the model. Left as a flagged descope, not silently done.

Seeded content: `brains/support/echo.md` from the existing `echo_reply_voice.md`;
`brains/support/{wrangler,scout,ranger,lainey}.md` were re-seeded (2026-09-04, same day)
from `docs/slack_convo/{wrangler,scout,ranger,lainey}_reply_voice.md` once those four
files were committed separately (they did not exist when this build started).

## D37. Naming correction mid-build: the dispatcher is "fixer", not "bus"
This build's first pass toward item 1 named the renamed headless dispatcher (formerly
`~/scout-listener/src/wrangler-service/`) "bus" -- reasoning, at the time, that "Wrangler"
needed to stop being the dispatcher's name and "bus" read as generically accurate for "the
thing that moves rows". Before any code was written under that name, Blake corrected this:
"bus" is ALREADY the name of the FIXER's underlying DATA layer (`support_tickets` +
`support_messages`, `agent/slack_convo/bus.py` in THIS repo) -- a completely different,
pre-existing, unrelated thing. Reusing "bus" for the dispatcher would have made every
future mention of "the bus" ambiguous between "the data layer" and "the HTTP receiver".
The dispatcher is instead "fixer" / "fixer-service" (directory
`src/fixer-service/`, env var `FIXER_SIGNING_SECRET`), chosen specifically to distinguish
it from the data-layer bus. `bus.py` in this repo is untouched by any of this -- it was
never the thing being renamed. See the scout-listener PR (below) for the actual rename
commit.

## D38. Item 4 (thread loop): built up to D3's own boundary, not blocked on it
"Client reply in the group DM writes support_messages, re-triggers the owning agent's
worker, code fixes go to the Claude Code fixer for that product with the before/after
gate, and the agent replies in the same thread only after verification." Broken down:

- **Row and re-trigger**: already correct, unchanged, and covered by the existing suite
  (`_follow_up()` in adapter.py, D18/D30). Once `outreach.stamp_ticket()` has pointed a
  ticket's `slack_channel_id` at the new group DM (D35), the client's next message in that
  DM is a normal MPIM follow-up to `adapter.handle_event` -- no new code needed, only the
  stamp.
- **Routed and tagged by product/identity**: `fixer_request_text()` already embeds
  `ident.product` (adapter.py line ~615), which is now correctly `"websites"` for Wrangler
  tickets after D34's `identities.py` change -- so a Wrangler CODE_FIX row is already
  tagged for the right product with no new code.
- **The verified-reply loop for QUESTION-type tickets**: already fully built and tested
  (`answer_lane.py` + the outbox's verification gate, D19/D24) -- a question asked in an
  outreach-opened thread gets exactly the same grounded-answer-or-escalate treatment as
  one asked in any other conversation, because it is, structurally, the same ticket/thread
  by the time it reaches `handle_event`.
- **CODE_FIX execution**: NOT built, and cannot be, because D3 (a Railway-hosted Claude
  Code executor) does not exist. `fixer_request_text()` still targets the desktop
  `ops-fix-triage.js` worker on Blake's Mac (per D3's own original text), which per D29's
  fix already only trusts Echo's `bot_id` -- so a Wrangler/Scout/Ranger/Lainey CODE_FIX
  row is written correctly (right product tag, right hold lane, right fence/escaping) and
  will sit in `hold`/`triage` correctly-shaped and ready, but genuinely nothing executes
  it until either (a) D3 ships, or (b) `ops-fix-triage.js` is taught to trust more than
  Echo's `bot_id` -- itself a D10/RA-m5 item, still open, still not this session's to
  decide. Blake's separate 2026-09-04 note names the eventual executor's credential env
  var as `FIXER_GITHUB_TOKEN` on the Railway "wrangler" service; that name is scaffolded
  into the renamed `fixer-service`'s header comment (scout-listener PR) as the fixed
  target for whenever D3 is built, and is NOT read by any code yet.

## Rulings still needed from Blake (new, in addition to the unchanged prior list)
- D34: literally rename Echo/Ranger/Scout/Lainey's `product` columns to
  `social`/`ads`/`portal`/`engage` (coordinated with the portal's Ranger cron and any
  other consumer), or leave them self-referential as this build did.
- D36: wire `brain.classification_hint_for()` into `classifier.classify()` as an advisory
  signal, and/or fold `tone_notes` into `answer_lane.py`'s reply-voice instructions.
- D10/RA-m5 (carried forward, now sharper): `ops-fix-triage.js` trusting only Echo's
  `bot_id` means Wrangler/Scout/Ranger/Lainey CODE_FIX rows queue correctly but never
  execute until either that worker is taught to trust more identities, or D3 (the Railway
  executor, now with a named credential env var, `FIXER_GITHUB_TOKEN`) ships.
- Item 5 (arming Scout, Ranger, Wrangler, Lainey in that order): Blake's own manual
  action per his ruling ("by my hand"). Not executed by this build. See the arming-state
  report handed to Blake alongside this doc for what was (read-only) checked.

## D39. Correction to D36: "shapes classification and reply style only" always meant a
## structural boundary, not zero import
D36's original enforcement for the brain (item 2/6 of Blake's ruling) was "answer_lane.py
does not import brain.py at all", proven by a source-grep test. Blake's follow-up ruling
explicitly asked to "wire the per-agent brain into classifier and answer-lane... shapes
classification and reply style only, never facts" -- style wiring into answer_lane.py is
not optional, so a blanket no-import rule cannot be the real enforcement; it was a
stand-in for it while the wiring didn't exist yet. Corrected: `answer_lane.py` now
imports `brain.py` for exactly one purpose, `BrainHint.tone_notes`, appended to the
`{voice}` section of the system prompt alongside (never replacing) the reply-voice doc.
The actual enforcement is now structural and tested behaviorally, not by grepping for an
import: a poisoned `tone_notes` entry is proven (by a real `answer()` call with a fake
brain file) to reach the SYSTEM prompt's voice section and NOT the `facts` dict, not
`grounding['facts']`, and not the FACTS block of the user prompt -- the only three paths
a fact can reach a client's reply.

## D40. Brain wiring, both sides, done
`classifier.py.classify()` takes an optional `brain_hint` (a `BrainHint`) and consults
`classification_hint_for(text)` in the same deterministic slot as the rule-based checks --
before the optional LLM step, since a phrase match is exact-string matching, not a guess.
It is filtered through the identical `_VALID`/no-`FOLLOW_UP` rule the LLM verdict already
uses, so a hint can never mint a label outside the fixed set, never force a re-trigger,
and (proven by test) never overrides a verdict the deterministic rules already reached
(an open ticket's `follow_up`, breakage+domain's `code_fix`, etc.). `adapter.py` loads
the calling identity's hint via `brain.load_hint(ident.name)` right before the classify
call, swallowing any read failure to `None` (a brain is an optimization, never a
dependency the ticket pipeline can be blocked by, matching `brain.py`'s own
`append_resolution` philosophy). `answer_lane.py`'s wiring is described in D39 above.

## Verification loop status (this ruling)
`python3 -m pytest` in lasso-echo-work: 5056 passed / 11 skipped before this build began
this session; 5106 passed / 11 skipped after (50 new tests: routing 13, brain 10,
outreach 27), zero regressions, zero flag defaults changed, no client-reply flag touched.
scout-listener: `feat/fixer-service-rename` branch, 570/570 tests green in an isolated
worktree, opened as a PR (not merged) per instruction. Frame 1 / Frame 2 adversarial
audits are intentionally NOT run by this build -- per the Big Build Protocol, that is a
separate, independent pass.

---

# Frame 1 / Frame 2 audit wave (2026-09-04): two fresh independent agents, PR #23 + scout-listener PR #1

Frame 1 ("row first, verification first") and Frame 2 ("how does a client get a message
they should not have") ran with zero shared context, per Blake's "fix, re-audit to zero".
Both converged on the same module: `outreach.py`, the one new outbound-first path in
this build. Combined: 2 CRITICAL confirmed by both frames independently, 1 CRITICAL
found only by Frame 2, 1 MAJOR, 1 informational (outreach.py has zero production callers
today -- none of this was live-reachable, but had to be fixed before any caller wires it
in). No finding on the pre-existing Slack-sourced path (D1-D33 holds); no finding on the
scout-listener rename.

## D41 (CRITICAL x2 + MAJOR, both frames). Outreach's message was unescaped, the row was
## never closed, and there was no idempotency guard
Three findings in `outreach.py`, one fix each:
- **Unescaped client text** (`first_message_text`): `ticket["raw_text"]` from a
  non-Slack, unauthenticated intake source was interpolated into the first DM message
  with no Slack-escaping, reopening the exact live-markup injection class D27/D32
  already closed on the Slack-sourced path -- worse here, since a public portal form is
  a LOWER-trust origin than a Slack workspace member. Fixed: `_slack_escape` (reused
  from `adapter.py`, not reimplemented) applied to `ask` inside `first_message_text`,
  and again unconditionally in `initiate()` so a caller-supplied `message_text` gets the
  same treatment.
- **Row never closed** (`initiate`): the ack row was written `delivery_status="ready"`
  and posted directly, but never claimed or marked `posted`/`failed` afterward -- so
  once the owning identity's normal outbox loop is armed, its next poll finds the SAME
  row still sitting in `ready` and reposts the identical first message a second time
  into a live client conversation the moment the client replies once (Frame 1 traced
  the exact gate, `inbound_count &lt; 1`, that stops protecting it). Fixed: a new optional
  `mark_message` injected param, called with `"posted"` + the real Slack ts on success,
  `"failed"` (matching `outbox.py`'s own convention) on a failed post -- never left in
  `ready` for a second consumer to find.
- **No idempotency** (`eligible`): a retry after a transient failure had nothing
  stopping it from re-recording and re-posting. Fixed: `eligible()` now refuses outright
  if the ticket already has a `slack_channel_id` stamped (which only `stamp_ticket()`
  ever sets, only after a successful open+post) -- reason `already_outreached`.

## D42 (CRITICAL, Frame 2 only, RULING STILL OPEN). Reporter-match proves internal
## consistency, not provenance -- fails closed until a real producer can assert it
Frame 2 constructed the sharper attack the reporter-match gate didn't cover: matching
`ticket.reporter` to the resolved recipient's email/slack_user_id only proves the ticket
is self-consistent, never that the person who actually submitted the intake form owns
that email. None of `NON_SLACK_SOURCES` (portal_form, engage_tenant_event,
website_intake) has a producer built in this repo yet, so none of them have any
mechanism to prove the submitter's identity today. A stranger who knows or guesses a
real client's email could submit a form as them and have a real client's real Slack
account contacted with attacker-chosen content.

Fixed narrowly and conservatively: `eligible()` now also requires
`ticket["reporter_verified"] is True`, a flag no current producer can set -- this makes
outreach eligible for literally nothing today (fully fail-closed), by design, until a
real producer positively asserts provenance.

**Ruling needed from Blake before any NON_SLACK_SOURCES producer is built**: which
provenance mechanism sets `reporter_verified`? The two shapes Frame 2 suggested: (a) the
intake requires a confirmed step tying the submission to an authenticated identity (a
magic-link click, an authenticated portal session) before the ticket is even created, or
(b) outreach never fires autonomously for a non-Slack source at all -- it goes through a
human tap first, the same hold-lane pattern the fixer_request cards already use. Neither
is implemented; the gate above simply refuses until one is and a producer sets the flag.

## Verification loop status (Frame 1/2 wave)
All three D41 fixes plus the D42 gate landed in `agent/slack_convo/outreach.py`, with 8
new tests (escaping x2, idempotency x1, row-lifecycle x3, D42 x2) alongside the 24
pre-existing outreach tests, all updated to assert `reporter_verified: True` in the base
fixture so they keep testing what they said they test. `tests/test_slack_convo_outreach.py`:
32/32 green. Full suite green. outreach.py remains unwired to any production caller --
D42's open ruling should be resolved before it is.

## D43 (closing audit, narrow, fresh verifier of D41/D42). Two real fixes, D42's provenance
## question confirmed genuinely still open
A fresh agent (not the one that built D41/D42) verified the fix commit against the
original 4 findings: all 4 CLOSED. It also broke two things on its own adversarial pass:
- **MINOR**: `initiate()` was re-escaping `first_message_text()`'s already-escaped
  default output, double-encoding it (`&lt;` -> `&amp;lt;`) whenever `message_text` was
  not overridden. Never a live-markup regression (Slack renders it as ugly-but-inert
  literal text, not live markup), but wrong. Fixed: escaping only applies to a
  caller-supplied `message_text` now; the default path is already escaped once, inside
  `first_message_text()`, and is not re-escaped.
- **MAJOR (latent, unexploitable today, confirmed by execution)**: the `reporter_verified`
  gate was a truthiness check (`if not ...get(...)`), not the `is True` check its own
  comment and DECISIONS.md both claimed. `"True"` (the string), `"yes"`, `1`, and an
  arbitrary truthy object all passed the gate when tested directly. Since no producer
  sets this field today the fail-closed guarantee held in practice, but the FIRST
  producer to set it to anything truthy-but-not-boolean (a token string, a timestamp --
  an easy real mistake) would have silently defeated D42's entire point. Fixed:
  `is not True`, exact boolean match, no other value opens the gate.

Two residual, honestly-flagged (not fixed) design notes from this same pass, neither
exploitable today because nothing wires either path live yet: `mark_message` is optional
with a silent no-op default, so a future caller that forgets to pass it silently
reintroduces D41's original repost bug with no warning; and `eligible()`'s
already-outreached check and the eventual `stamp_ticket()` write are not atomic (no real
`stamp_ticket` implementation exists anywhere in the repo yet, only test doubles), a
narrow TOCTOU gap for whoever builds the real one.

D42's provenance mechanism remains an explicitly open ruling for Blake -- confirmed
genuinely open, not silently resolved, both by this verifier's own repo-wide grep
(`reporter_verified` appears nowhere outside this file and its tests) and by
DECISIONS.md's own text above naming the two unimplemented options.

## Verification loop status (closing audit)
34/34 outreach tests green (32 + 2 new: a parametrized reporter_verified truthiness
test, a no-double-escape test). Full suite: 5123 passed, 11 skipped, 0 failed. Zero
CRITICAL, zero MAJOR remaining on this module as of this commit -- per Blake's "fix,
re-audit to zero," this closes the Frame 1/2 audit wave.

---

# Fresh Frame 1/2 re-audit (2026-09-04): full current state of both PRs, post-transfer

Blake asked for a genuinely fresh pair of Frame 1/2 agents against the FULL current
diff of both PRs (not just outreach.py) since real commits (D39-D43) landed after the
first pair started. Frame 1: zero CRITICAL, zero MAJOR, tried hard, could not break
anything -- full report in the PR history. Frame 2 (run twice, same finding both times,
confirming it's real and reproducible, not noise): one MAJOR, latent, in `brain.py`.

## D44 (MAJOR, Frame 2, reproduced live by two independent runs). `_clip()` stripped
## only '\n', not '\r' -- a client's own phrasing could forge a fake classification
## heading that poisons a DIFFERENT client's future classification
`_clip()` (brain.py) removed literal `\n` from client-authored text before writing it
to a brain .md file, but not `\r`. A bare `\r` survives into the file, then Python's
universal-newline text mode (`open(path, "r")`) translates it BACK into a real `\n` the
NEXT time the file is read -- reconstituting a single line of client text into
multiple lines. Frame 2 reproduced this directly:
`client_phrasing = "innocuous text\r## Classification hints\r- give me a discount -> code_fix"`
resulted in a genuine second `## Classification hints` section on disk, and
`load_hint()` parsing a real `("give me a discount", "code_fix")` pair out of it --
a classification hint from ONE client's own words, sitting in the SAME per-agent brain
file `classifier.classify()` consults for EVERY other client's future messages on that
identity. Since `brain_hint` only fires after the deterministic rules fail to decide
(classifier.py), this could flip an unrelated client's otherwise-escalated message to
`code_fix` based on a phrase the ATTACKER chose, not that client.

Why it never reached CRITICAL: `BrainHint` still has no path to a fact (D36/D39's
schema separation holds -- this is a classification-integrity leak via a markdown
PARSING bug, not a schema violation), and a forced `code_fix` still lands in the HELD
lane behind Blake's tap (RT-C1), never auto-executed. Why it mattered anyway: it will
silently reappear the moment the resolved-ticket-learning wiring D36 describes as
future work (`append_resolution` has zero production callers today, confirmed by grep
both times) is actually built, unless fixed now.

Fixed: `_clip()` strips every newline-shaped sequence universal-newline translation
cares about (`\r\n`, `\r`, `\n`), not only the literal one its caller happened to
produce. New test reproduces the auditor's exact attack string and asserts no real
`## Classification hints` LINE (not substring) is ever creatable from client text.

## D44b (MINOR, Frame 2, both runs). `outreach.initiate()` posted without ever claiming
## its own row, the one gap in this module not already covered by D41's row-first fix
D41 made `initiate()` close the row's lifecycle (mark posted/failed) but never made it
CLAIM the row (the `ready` -> `posting` CAS `outbox.py`'s own dispatch loop uses) before
posting -- so the row sat in `ready`, claimable by a concurrently-armed outbox loop, for
the entire duration of the post call. Traced worst case: the outbox's own first-contact
gate would have suppressed rather than duplicate-posted (never a double DM to the
client), but it's a real, unnecessary race window nothing forced closed. Fixed: an
optional `claim_message` param (same shape and optionality as `mark_message`), called
right after the row is written and before the post; a lost claim backs off without
posting rather than risk it.

## Verification loop status (fresh re-audit wave)
53 outreach + brain tests green (34 + 3 new: D44's fake-heading test, D44b's claim-and
lost-claim tests). Full suite: see commit. Zero CRITICAL, zero MAJOR remaining across
both PRs as of this commit, confirmed by two independent fresh audits per finding.
outreach.py and the resolved-ticket-learning path into brain.py both remain unwired to
any production caller -- D42's provenance ruling is still the one open item before
either is wired live.

---

# D45 (Blake's ruling, 2026-09-04). D42's provenance question resolved: reuse the
# existing #fixer hold-card + tap, don't invent authentication for producers that don't
# exist yet

Blake: "do whatever you recommend for D42." Building real authentication into three
intake producers that do not exist yet (portal_form, engage_tenant_event, website_intake)
is a speculative, much larger project than this feature warrants, and blocking outreach
indefinitely on it wastes the work already done. This system already has a proven,
audited pattern for exactly this shape of problem -- a human tap gates anything that
cannot verify itself (D20's hold-card + Release button, already used for fixer_request
and held replies). Reused here, not reinvented.

`eligible(ticket, who)` (the split-out `_base_eligible()` plus the `reporter_verified is
True` check) stays the FAST, autonomous path for a future strongly-authenticated
producer that genuinely doesn't need a human in the loop -- unchanged, still fails closed
for everyone today, exactly as D42 left it.

New for every OTHER ticket (which today is every ticket, since nothing sets
`reporter_verified`): `eligible_for_approval_request()` (the same base gates, minus
provenance) plus `request_approval()`, which writes the proposed first message as a
`held` row (`kind=KIND_OUTREACH_REQUEST` -- deliberately NOT postable by the normal
outbox loop, which does not know how to `open_group_dm`) and a hold-notice card in
#fixer via the existing `adapter.write_hold_notice`. `release_approved_outreach()` is the
tap handler: validates the held row belongs to THIS ticket and THIS identity (same
discipline as V-m10's cross-identity release check), re-runs the base gates at tap time
(not just at request time -- the ticket could have changed in the window between the
card posting and the tap), then sends via the same `_send()` internals `initiate()`
itself now calls (refactored out to avoid duplicating the escaping/row-lifecycle/claim
logic between the two paths).

One thing this build does NOT do: wire `RELEASE_ACTION_ID`'s Slack button dispatch to
`release_approved_outreach()` in `listener_wiring.py`. That is a small, mechanical follow
up consistent with everything else about `outreach.py` -- it still has zero production
callers today, so this is prep work, not a live capability, exactly like the rest of
this module has been from the start.

## Verification loop status (D45)
outreach.py: 46 tests green (37 existing + 9 new: hold-request success/refusal/escaping,
release validation x2 (kind, ticket, identity), re-check-at-tap-time, backward-compat).
Full suite: see commit.

---

# D46 (Blake, 2026-09-04). The real incident: three dark paths, one built bridge

Blake reported submitting an Echo support ticket through the portal and getting nothing
back. Traced live (a fresh agent, evidence-backed, no code touched during the trace):
THREE completely separate mechanisms exist for "Echo support," and all three were dark.

1. `/api/gyms/[gymId]/support` (portal) writes a real `support_tickets` row
   (`product='portal', source='website_tab'`) -- but the only worker anywhere that reads
   `support_tickets` and posts to Slack (`fixer-lane.ts`, a 5-minute cron) hard-filters
   every query to `product='ranger'`. A `product='portal'` row is invisible to it and
   sits forever. NOT fixed by this build (out of today's scope -- Ranger's cron is a
   separate product line Blake did not ask about); flagged here so it is not forgotten.
2. The Echo tab's "Contact support" button was a plain link to a token-signed URL on the
   separate Echo Railway service; any resolution failure (missing token row, missing
   config) silently fell back to a `mailto:` link -- a click that transmits nothing until
   a human notices a blank compose window. A second, near-identical copy existed in
   `OrganicSocialFlow.tsx`'s header `SupportButton`. **Fixed**: both now point at the
   already-working, already-durable `/api/gyms/[gymId]/support` endpoint instead of an
   external, token-dependent one -- the header button scrolls to the real form, the
   bottom banner IS the real form (an inline textarea + POST, `product: "echo"`).
3. Even a correctly-resolved token URL hits Echo's own `/portal/<token>/support`, which
   is feature-flagged `AGENT_SUPPORT_INBOX=false` by default and 403s before rendering.
   Superseded for the Echo tab by fix #2 above (no longer used for that path); Blake
   asked to arm this flag anyway for its own, older support surface -- done separately,
   see the arming note in the session record, not this file.

## The built bridge: `agent/echo_ticket_worker.py` + `echo_ticket_wiring.py`

Ground truth (Blake's own words): "with echo if someone submits a support echo should
receive that then echo should fix it verify the fix and then send slack message with
them and me in the message." Two poll passes, wired into the existing scheduler loop in
`listener.py`, both no-ops unless `AGENT_PORTAL_ECHO_TICKETS_ENABLED=true`:

- **intake_pass()**: picks up NEW, unclassified `product='echo', source='website_tab'`
  tickets. Resolves the client's Slack identity from the ticket's `reporter` (a real,
  server-authenticated email -- see provenance note below) via `users.lookupByEmail`.
  Classifies via the SAME `classifier.classify()` every Slack-sourced ticket uses. A
  QUESTION gets answered from live state (`answer_lane.answer()`) and, if grounded, sent
  immediately via `outreach.initiate()` with the VERIFIED ANSWER as the first message --
  never a generic "I'm on it" placeholder, per Blake's own ordering (answer/fix, verify,
  THEN send). A CODE_FIX is written as a HELD `fixer_request` card, identical to any
  other code_fix in this system -- **D14's hold gate is completely untouched**: a
  client's code_fix is always held behind Blake's #fixer tap, no exception for this
  source. This build only automates the notify-once-verified step, never the fix step.
- **fixed_pass()**: polls `status='fixing'` tickets for a `verification_after` the
  existing ops-fix-triage.js worker writes back once it has verified a real fix (D3's
  same, unchanged desktop-dependent executor). Once present, sends the client (and
  Blake, always in the DM by outreach.py's own design) the verified result. A ticket not
  yet verified is left exactly as-is for the next poll.

## Provenance (D42's escape hatch, actually used)

`/api/gyms/[gymId]/support` now stamps `reporter` from the AUTHENTICATED Clerk session
server-side (a fresh `app_users` lookup by `clerk_user_id`, never the request body) --
the client cannot spoof it. This is exactly D42's "a confirmed... authenticated portal
session" option, so `echo_ticket_worker.py` builds an in-memory `reporter_verified=True`
ticket dict when calling `outreach.initiate()` (D45's `eligible()` fast path), rather
than the human-tap `request_approval()` path -- Blake asked for a fully automatic
pipeline for this specific, trusted source. Any OTHER non-Slack source added later
without an equivalent authenticated-session guarantee should use the tap path instead.

## D46 also: `website_tab` added to `NON_SLACK_SOURCES`

The real portal source value is `website_tab`, not `website_intake` (D34's spec
paraphrase). Added deliberately per D34's own explicit-allowlist discipline; kept
`website_intake` too rather than rename it.

## Verification loop status (D46)
13 new tests (`tests/test_echo_ticket_worker.py`): config-off no-ops (both passes),
identity resolution (success + 3 failure modes), unresolved-identity escalation,
grounded-question answer + outreach (and the never-a-placeholder assertion), an
ungroundable question escalates, a code_fix is held behind the SAME tap gate (explicit
assertion `delivery_status == "held"`), fixed_pass notifies/leaves-alone/escalates
correctly. Also fixed one pre-existing regression this build surfaced:
`test_status_completeness.py` requires every `_enabled()` config flag to appear in
`agent/__main__.py`'s `_status()` output -- added the missing line for
`portal_echo_tickets_enabled`. Full suite: 5238 passed, 11 skipped, 0 failed.

Portal side (lasso-ops-portal, separate repo): `/api/gyms/[gymId]/support` extended
(product allowlist + authenticated reporter), `EchoSupportLinks.tsx` rewritten (inline
form, no external token dependency), `OrganicSocialFlow.tsx`'s `SupportButton` pointed
at the real banner instead of the dead mailto pattern. `npx tsc --noEmit` clean.
