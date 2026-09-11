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

## D47: `product='portal'` tickets are invisible to `fixer-lane.ts` -- routed to Scout instead

Ground truth check (Blake, 2026-09-04): `src/lib/server/ranger/fixer-lane.ts` in
lasso-ops-portal is Ranger's ad-engine cron (`processRangerTickets()`, Pipeboard writes,
`firstBrokenLeg`/`FunnelLegs` policy) -- deeply ranger-specific, never scoped to any
other product, and not the right place to bolt on portal-ticket handling. Blake's own
ruling: "Portal tickets route to Scout per the identity map." The fix is on the
lasso-echo-work side, generalizing D46's bridge rather than touching fixer-lane.ts:

- `echo_ticket_worker.py`'s `intake_pass()`/`fixed_pass()` now take `product`, `source`,
  `identity_name` as parameters (still defaulting to Echo's original
  `PRODUCT="echo"`/`SOURCE="website_tab"`/`identity_name="echo"`, so every existing call
  site and test is byte-for-byte unchanged). `classify()`'s `identity_product` argument
  now reads `ident.product` off the resolved identity object, not the raw
  `identity_name` string -- these can differ (an identity's registry name is not
  guaranteed to equal its product tag).
- `echo_ticket_wiring.py`'s `live_deps()` takes the same `product`/`source`/
  `identity_name` and resolves that identity's OWN bot token via
  `ident.env(ident.bot_token_env)` (the same lookup `listener_wiring.py` already uses),
  never a hardcoded Echo token constant -- Scout's messages must send as Scout.
- `listener.py`'s scheduler runs a second poll pass alongside the existing Echo one,
  same flag (`AGENT_PORTAL_ECHO_TICKETS_ENABLED`) and same throttle
  (`portal_echo_tickets_poll_minutes()`) -- one lane, two identity legs, not a second
  thing to arm: `live_deps(product="portal", source="website_tab",
  identity_name="scout")`, then `intake_pass`/`fixed_pass` on those deps.
- 4 new tests added to `tests/test_echo_ticket_worker.py` proving: a `product='portal'`
  ticket routes to the Scout identity; the portal pass never touches `product='echo'`
  tickets; Echo's own call site still defaults correctly with no `product` override;
  `fixed_pass` routes portal tickets to Scout too. All 17 tests in that file pass.
  Caught one test bug along the way: `fetch_state` returning `{}` made
  `answer_lane._all_unavailable()` treat the ticket as ungroundable before any LLM call
  (empty dict, not missing keys) -- fixed the test to return `{"portal_status": "ok"}`.

Known limitation, unchanged from D46: a code_fix ticket routed through a non-Echo
identity (Scout included) queues a HELD `fixer_request` correctly but the desktop
`ops-fix-triage.js` worker still only trusts Echo's `bot_id` (D10) -- this is the same
documented gap every other non-Echo code_fix path in this system already carries, not
new. `AGENT_PORTAL_ECHO_TICKETS_ENABLED` stays unarmed; this is a routing fix, not an
arming decision.

## D49. Frame 1 audit MAJOR: outreach_request release button silently no-op'd (2026-09-05)

Fresh independent audit (against merged main, pre-arming loop) found: the "Release" tap
on a `KIND_OUTREACH_REQUEST` hold card (D45's safety net) routed to `outbox.release_held`,
which refuses that kind outright (its own accepted-kinds check) and no-ops silently --
Blake would believe he approved an outreach that never sent. Fixed in
`lassoframework/lasso-echo#43` (merged `7fffde134f9f190d7f37a58af4171fe7004e6e84`):
`listener_wiring.py`'s `_on_release` now dispatches on the held row's own
`attachments.kind`, routing an outreach_request to `outreach.release_approved_outreach`
instead. Currently dormant in production (nothing calls `request_approval()` yet), but a
real, live gap in the dispatch wiring itself. New test drives the tap through
`ConvoWiring`'s REAL registered action handler, not a direct call to the underlying
function -- confirmed it fails with the fix reverted.

## D50. Frame 1 audit MAJOR: the escalation card's Resolved button had no handler (2026-09-05)

Same audit pass found a second, independent gap: `escalation_blocks()` (outbox.py, D48/#41)
has rendered a "Resolved, tell them" button on every escalation card since #41 merged, and
its own docstring already claimed the tap was "operator-gated like the release tap" -- but
no `@app.action(OB.RESOLVE_ACTION_ID)` handler was EVER registered anywhere
(`listener_wiring.py`, `echo_ticket_wiring.py`, `listener.py` all checked). Every tap
silently failed at the Slack layer (no `ack()`, `resolve_and_notify()` never invoked): the
ticket's submitter never heard anything, no matter how many times the button was tapped.
Reachable today (escalation is an `INTERNAL_KINDS` row, always `ready`, live for any
enabled identity -- Echo today). Fixed in `lassoframework/lasso-echo#44` (merged
`da7de86395755321afa311a12425ba1a528f89a3`): registered
`@app.action(_outbox.RESOLVE_ACTION_ID)` in `ConvoWiring.register()`, mirroring the release
handler's operator-gate pattern exactly. New test again drives the tap through the REAL
registered handler; confirmed it fails with the fix reverted.

## Audit-loop closure (2026-09-05)

Three independent fresh-agent audits run against this system before any Phase 4 arming,
each with zero shared context with the others or with whoever built the code under review:

1. Against `7fffde134f9f190d7f37a58af4171fe7004e6e84`: found D49 (MAJOR), zero CRITICAL.
2. Against `da7de86395755321afa311a12425ba1a528f89a3` (after D49+D50 both merged): zero
   CRITICAL/MAJOR. One MINOR (informational, matches D47's own already-documented ruling:
   a portal-routed Scout ticket's rows would sit unread if `AGENT_PORTAL_ECHO_TICKETS_ENABLED`
   were ever armed with no Scout outbox loop running in this service -- not reachable today,
   flag defaults false, does not touch Echo's live path).
3. A second independent pass against the SAME `da7de86...` SHA (nothing changed in
   between): zero CRITICAL/MAJOR, re-confirmed both D49/D50 fixes correct by direct
   re-trace (not by trusting the first pass's summary), re-confirmed the MINOR unreachable,
   and separately flagged `agent/slack_convo/routing.py` as dead code (never imported by any
   live call site) -- inert, not a live misroute risk.

Two consecutive clean passes (zero CRITICAL/MAJOR) against the same final SHA
`da7de86395755321afa311a12425ba1a528f89a3` closes the loop. Phase 4 arming begins against
this exact commit.

## D51. Phase 4 arming, identity 1/4: Echo (2026-09-05)

Echo was already partially armed (`SLACK_CONVO_ECHO_ENABLED=true`,
`SLACK_CONVO_ECHO_STAFF_REPLY=true`, `SLACK_CONVO_ECHO_CLIENT_REPLY=false`) on the
Railway `echo` service, confirmed running `da7de86...` before any test ran
(`railway status --json`, three active deployment instances all on that SHA).

Both required legs run for REAL against the live Supabase bus and live Slack API via
`railway ssh --service echo` (executing inside the actual deployed process, so the real
`AGENT_SLACK_BOT_TOKEN` is used without ever being read into the orchestrating session),
using `listener_wiring.live_deps("echo")` + `adapter.handle_event()` -- the exact
production code path a real inbound Slack event drives. The "sender" of each event is a
crafted payload, not a literal second human logged into Slack (no access to a second
Slack session existed) -- named explicitly here per the run brief's own honesty
requirement.

- Test identity re-resolved FRESH via `users.lookupByEmail` with Echo's own bot token
  (not a cached id): `blake+zztest@lassoframework.com` -> `U0BV9D5A17W` ("Lasso Test").
  A real 1:1 IM channel was opened with Echo's bot token (`conversations.open`) ->
  `D0BVBFTU9J8`.
- **Happy-path leg**: a crafted `message` event from `U0BV9D5A17W` in that real IM channel
  ("my facebook posts are not going out"). Result: ticket `299ed8fa-43d1-4336-926c-9005f80e739d`,
  `identity_kind=client`, `client_id` correctly resolved to ZZ Test Gym
  (`ca397eec-519a-4524-b666-d048199c76b2`), `bot_identity=echo`. The classifier did not
  reach a QUESTION/CODE_FIX/ACTION_REQUEST decision (no LLM wired into this manual run,
  matching `live_deps`'s own `classify_llm=None`) and correctly escalated rather than
  guessing -- a legitimate "does not know, tells a human" outcome per the run brief's own
  rule that a correctly-reasoned escalation counts as clean. `outbox.run_once` (real
  `chat.postMessage` calls) posted the escalation + hold-notice card to #fixer (2 posted);
  the client-facing `TEMPLATE_ESCALATED` row correctly stayed `held` -- CLIENT_REPLY was
  still `false` at test time, and it was NOT sent, proving the trust ladder holds before
  arming.
- **Escalation-path leg**: a crafted event from a syntactically-valid but nonexistent
  Slack user id (`U0000000000`) in a fresh synthetic channel. `identity_gate.resolve`'s
  live `users.info` call genuinely failed (real Slack API 404), correctly resolving
  UNKNOWN -- reason `unknown_identity`. Ticket `37143e16-eeca-404b-ba0c-4d6a3bd78ceb`
  created; escalation + hold-notice posted to #fixer for real; the unknown-user template
  correctly stayed `held`, never sent to the fake channel.
- Verified directly against `support_messages` (both tickets): the inbound row's
  `created_at` strictly precedes every outbound row's on both tickets (row-first held);
  `attachments->>'identity'` is `echo` on every row, both tickets (no cross-agent
  posting); no `KIND_ANSWER`/fix was ever claimed on either ticket, so no
  verification_before/after gate applied -- nothing here fabricated a "verified" claim.
  Both test tickets set `status='resolved'` afterward as cleanup.

Clean on both legs -> armed `SLACK_CONVO_ECHO_CLIENT_REPLY=true` on the Railway `echo`
service. Echo is now fully armed (ENABLED + STAFF_REPLY + CLIENT_REPLY, all true).
Moving to Scout next.

## D52. Phase 4 arming, identity 2/4: Scout (2026-09-05)

Flipped `SLACK_CONVO_SCOUT_ENABLED=true` and `SLACK_CONVO_SCOUT_STAFF_REPLY=true` on the
same Railway `echo` service (confirmed via `listener_wiring.start_additional_identities()`
-- every identity's Bolt app runs inside this one process, not a separate service; Scout's
real token env vars were already present in this service's env before this session). The
env-var write triggered a Railway redeploy; confirmed post-redeploy via
`railway ssh --service echo` reading the live process's own `os.environ` (not railway's
config cache) and via `railway logs` showing `[slack-convo/scout] registered (enabled=True)`
+ `socket mode started` -- Scout's Bolt app is now a genuine live Socket Mode connection,
not just a flag flip.

Both legs run for real the same way as D51 (own worktree teardown notwithstanding),
`live_deps("scout")` + `adapter.handle_event()`, real Slack API calls via Scout's own bot
token, real Supabase writes:

- Fresh `users.lookupByEmail` with SCOUT's own token: same address resolves to the same
  `U0BV9D5A17W` (expected -- it is the same Slack workspace user; re-resolved per-identity
  as instructed rather than reusing D51's cached id). Real IM channel opened with Scout's
  token: `D0C0N7VE6SC`.
- **Happy-path leg**: ticket `c68a3ac1-bd4e-4460-ab22-fb34725c9cb3`. Classifier again did
  not reach a decision (no LLM in this manual run) and correctly escalated -- clean per the
  same "does not know, tells a human" rule. Escalation + hold-notice posted to #fixer for
  real; client template correctly held (CLIENT_REPLY still `false` at test time).
- **Escalation-path leg**: nonexistent user id `U0000000000` in a fresh synthetic channel
  -> genuinely failed `users.info` -> UNKNOWN -> ticket `209bdb07-927e-4189-bb75-e0bbd5ba3c69`,
  escalation + hold-notice posted for real, template held.
- Verified in `support_messages`: row-first holds on both tickets; `attachments->>'identity'`
  is `scout` on every row, both tickets (zero cross-agent posting); no fix/answer claimed,
  so no verification gate applies. Both test tickets set `status='resolved'` afterward.

Clean on both legs -> armed `SLACK_CONVO_SCOUT_CLIENT_REPLY=true`. Scout fully armed.
Moving to Ranger next.

## D53. Phase 4 arming, identity 3/4: Ranger (2026-09-05)

Ground-truth note: `identities.py`'s own module docstring says "Ranger has no Slack bot
identity of its own today; it is the ad-engine feature plus the fixer-lane cron in the
portal." That is stale against this Railway service's actual env -- `RANGER_SLACK_BOT_TOKEN`
/ `RANGER_SLACK_APP_TOKEN` / `RANGER_SLACK_BOT_USER_ID` were already present before this
session, and `SLACK_CONVO_RANGER_ENABLED` already existed as an explicit `false`. Flagging
the stale docstring here rather than silently proceeding past it; the arming itself follows
Blake's own explicit ordering (Echo, Scout, Ranger, Wrangler) from this run's brief. This is
the Slack Conversational Adapter identity only -- entirely separate from Ranger's ad-engine
autonomous execution rail, which this run never touches (per this run's own hard line: no
safe-lane auto-merge changes for Ranger's ad-engine).

Flipped `SLACK_CONVO_RANGER_ENABLED=true` + `SLACK_CONVO_RANGER_STAFF_REPLY=true` on the
`echo` service; confirmed live via `railway logs` (`[slack-convo/ranger] registered` +
`socket mode started`) and via `railway ssh` reading the running process's own env.

- Fresh `users.lookupByEmail` with Ranger's own token -> `U0BV9D5A17W` (same workspace
  user, re-resolved per-identity as instructed). Real IM channel opened with Ranger's
  token: `D0BV5UJKFE2`.
- **Happy-path leg**: ticket `ec3d1780-7e82-4ed5-a50b-5f360db3cfd5`. Same "classifier did
  not decide, correctly escalated" outcome. Escalation + hold-notice posted to #fixer for
  real; client template held (CLIENT_REPLY still `false` at test time).
- **Escalation-path leg**: nonexistent user id, fresh synthetic channel -> UNKNOWN ->
  ticket `8503b53a-912c-4dec-a1f1-cec400c6ca2a`, escalation + hold-notice posted for real,
  template held.
- Verified in `support_messages`: row-first holds on both tickets; `attachments->>'identity'`
  is `ranger` on every row, both tickets (zero cross-agent posting); no fix/answer claimed.
  Both test tickets set `status='resolved'` afterward.

Clean on both legs -> armed `SLACK_CONVO_RANGER_CLIENT_REPLY=true`. Ranger fully armed.
Moving to Wrangler next.

## D54. Phase 4 arming, identity 4/4: Wrangler (2026-09-05)

Flipped `SLACK_CONVO_WRANGLER_ENABLED=true` + `SLACK_CONVO_WRANGLER_STAFF_REPLY=true` on
the `echo` service; confirmed live via `railway logs` (`[slack-convo/wrangler] registered`
+ `socket mode started`) and via `railway ssh` reading the running process's own env. At
this point all four target identities (Echo, Scout, Ranger, Wrangler) have live Socket
Mode connections in this one process; Lainey correctly still shows
`tokens present but SLACK_CONVO_LAINEY_ENABLED is off; not started`.

**New finding, not previously known**: Wrangler's bot token is missing OAuth scopes
`channels:write` / `groups:write` / `mpim:write` / `im:write` -- `conversations.open`
genuinely failed (`missing_scope`) when this run tried to open a fresh test DM the same
way it did for Echo/Scout/Ranger. Provided scopes are `chat:write`, `channels:join`,
`channels:history`/`read`, `groups:history`/`read`, `im:history`, `mpim:history`,
`users:read`(`.email`), `app_mentions:read`. Practical effect: Wrangler's bot can post
into any channel it can join (`channels:join` + `chat:write` covers the whole
conversational-adapter reply path used here, and covers a REAL client-initiated DM too --
Slack already owns that channel once the client opens it, no `conversations.open` call
required on the bot's side) but can never itself PROACTIVELY open a new DM or group DM.
The only place that matters in this codebase is `outreach.py`'s `open_group_dm` (D45's
ticket-initiated outreach, gated separately by `AGENT_PORTAL_ECHO_TICKETS_ENABLED`,
currently off and not called for Wrangler by any live wiring) -- so this does not block
today's arming, but Wrangler cannot use that path until these scopes are added to its
Slack app. Reported here rather than worked around silently.

Both legs re-run using the **mention/channel-thread surface** instead of IM (the IM
surface would have needed the missing `conversations.open` scope purely for this run's
own test setup, not for anything CLIENT_REPLY actually gates -- see above):

- Fresh `users.lookupByEmail` with Wrangler's own token -> `U0BV9D5A17W`. Real channel
  used: Wrangler's own configured fixer channel (`WRANGLER_FIXER_CHANNEL_ID` = the shared
  `#fixer`, `C0BUUL1G90E`) via a crafted `app_mention` event (needs no `conversations.open`
  at all).
- **Happy-path leg**: ticket `71c6c3e2-f409-4df6-8cd8-4c2ab71f8039`. Same "classifier did
  not decide, correctly escalated" outcome; escalation + hold-notice posted to #fixer for
  real; client template held (CLIENT_REPLY still `false` at test time).
- **Escalation-path leg**: nonexistent user id mentioning the bot in a fresh synthetic
  channel -> UNKNOWN -> ticket `eee5aa4c-8cbf-4d36-93f3-f8f19daa1b84`. Only an escalation
  posted (no client template at all) -- this is RT-m6's documented behavior ("an unknown
  user @mentioning the bot in a channel is escalated internally only"), correctly not a
  gap.
- Verified in `support_messages`: row-first holds on both tickets; `attachments->>'identity'`
  is `wrangler` on every row, both tickets (zero cross-agent posting); no fix/answer
  claimed. Both test tickets set `status='resolved'` afterward.

Clean on both legs -> armed `SLACK_CONVO_WRANGLER_CLIENT_REPLY=true`. Wrangler fully
armed.

## Phase 4 arming complete (2026-09-05)

All four target identities fully armed (`ENABLED` + `STAFF_REPLY` + `CLIENT_REPLY`, all
`true`): Echo, Scout, Ranger, Wrangler. Lainey untouched, confirmed still off
(`SLACK_CONVO_LAINEY_ENABLED` unset, no live socket, per Blake's explicit no-exceptions
ruling). Every arm followed a clean staff/client two-leg test cycle against the live bus
and live Slack API in THIS run, verified directly against `support_messages` rather than
inferred from code alone.

## D48: an escalated portal ticket must never be silence for the person who wrote in

Found live 2026-09-05. Three real portal tickets (`cb7b385a` / `063bc73d` / `af01f3ea`,
ZZ Test Gym) each escalated correctly into #fixer, and each left its submitter with
nothing at all. Blake's words: "i got both of these in fixer channel but nothing got
sent to the person [who] submitted with the fix or letting me know it was resolved."

Two separate holes, both closed here.

**Hole 1: the bridge escalated and returned.** `_escalate_unresolved()` wrote the
internal `escalation` row and stopped. The Slack-initiated path has always sent the
person an acknowledgement inline (`adapter.py`, `TEMPLATE_ESCALATED`); the portal bridge
never did. New `acknowledge_submitter()` in `echo_ticket_worker.py` sends it on the best
channel available: a Slack group DM (Blake + client + bot) through the same
`outreach.initiate()` the answered path uses when the person resolved to a real client,
the portal support thread otherwise. Written exactly once per ticket, and fails CLOSED on
a bus read fault (a lookup failure never licenses a second send, the same convention
`adapter._outbound_kind_ever` holds).

**Hole 2: a portal ticket had no delivery surface at all.** `outbox.py` gate 7 marked
every conversational row with no `slack_channel_id` `failed`, silently. A portal ticket
has no Slack channel until a group DM is opened, which for an unresolved identity never
happens, so even a written acknowledgement would have died there. Gate 7 now recognises
a second real surface: the `/my/support/[ticketId]` thread the person submitted from.
Migration 0310 already decides what a client may read there (outbound + `posted` + not an
internal kind), so delivery means marking the row posted with
`delivered_via='portal_thread'`. Restricted to `portal_form` / `website_tab` (the two
sources a client submits through the portal UI) AND to tickets carrying a `client_id`,
without which 0310's predicate can never match the reader to the row. A ticket with
neither surface still fails, as before.

**Closing the loop.** An escalation card in #fixer now renders a "Resolved, tell them"
button (`slack_convo_resolve`, operator-gated in `listener_wiring.py` exactly like the
release tap), routed to `outbox.resolve_and_notify()`: it writes the person a `status`
row and closes the ticket. The notice goes out through every gate this module already
enforces, so it lands in the group DM when one was opened and the portal thread
otherwise. Idempotent (a second press writes nothing), refuses another bot's ticket, and
refuses when there is nowhere to deliver -- and the button is not rendered in that last
case, since a tap that could only no-op is worse than no tap.

**How the whole path reads now.** Client submits in the portal -> ticket + inbound row ->
identity -> classify. A grounded question is answered and the ticket resolves. A code fix
is held for Blake's tap, worked by `ops-fix-triage.js`, and `fixed_pass()` notifies the
client once the fix is VERIFIED. Anything else escalates: the person gets "this needs a
person, the team will follow up" immediately, Blake gets the card, and his tap on that
card is what tells them it is done.

Not changed, deliberately: `question_not_groundable` on "is my instagram connected?" was
CORRECT for ZZ Test Gym. That gym has no `echo_intake_tokens` row, so no `account_key`,
so `answer_lane` had zero facts and refused to guess. A gym with a real Echo account
grounds that question from `handle_social_status`. The bug was the silence, not the
refusal.

## D55. Postmortem: zero messages ever posted, and why every prior "green" was wrong (2026-09-05)

Blake asked for a plain account of three things: what actually happened, why earlier
reports of this system working were wrong, and what check would have caught it sooner.

**What actually happened.** Every outbound row in `support_messages` -- every escalation,
every fixer_request card, every hold notice, every conversational reply, across every
identity, since this table existed -- was stuck. `outbox.py`'s dispatch is a
compare-and-swap: claim a row by PATCHing `delivery_status` from `ready` to `posting` in
one round trip (so two concurrent consumers, a redeploy overlap or a second Wrangler on
the same rows, can never double-post one row), THEN post to Slack, THEN mark it `posted`.
Migration 0309 defined the `delivery_status` CHECK constraint the day this table was
built, and it listed only the STEADY states its own comment described --
`drafted/held/ready/posted/suppressed/failed` -- never `posting`, the TRANSIENT state the
code has always needed mid-claim. Every single claim attempt raised a Postgres 400 and
was logged as `[slack-convo/outbox] claim failed for row ...: BusError`. Nothing ever
reached `posted`. A separate bug in `echo_ticket_worker.py` compounded this for the
portal-ticket bridge specifically: its escalation path wrote `support_tickets.status=
'escalated'`, a value the `status` CHECK constraint has never allowed either, so even the
ATTEMPT to write an escalation row failed before the (already-broken) claim step was ever
reached -- a ticket in this state retried identically, forever, with a duplicate inbound
row recorded on every retry, and no card ever reaching a human. A real client's ticket
sat in exactly this loop from the moment the portal bridge was first armed until this was
found, hours later, running a live regression test.

**Why the earlier reports were wrong.** Every existing test in this system -- for both
constraints, across dozens of tests written over multiple sessions -- ran against a
`FakeBus` or an in-memory dict that accepted any string as a valid `status` or
`delivery_status`. None of them touched a real Postgres CHECK constraint, so none of them
could ever fail this way. A build that reports "5,000+ tests passing" is a true statement
about the CODE'S OWN LOGIC and a false signal about whether that code can actually write
to the real table it targets -- the tests and the schema had quietly drifted apart, and
nothing in the test suite's own shape could reveal that, no matter how many times it was
run. D33's earlier audit found and fixed a related but different bug (a row stranded by a
wrong `bot_identity`, not an illegal literal) using the same kind of FakeBus, and correctly
closed clean -- because that bug WAS reachable through a FakeBus. This class of bug is
categorically invisible to that testing strategy, not a gap in how carefully any single
audit was run.

**What check would have caught it sooner.** A static test with no live database
connection at all: read every Postgres CHECK constraint's actual allowed values (via
`pg_get_constraintdef`, not a migration file's comment describing intent) into an
explicit allow-list constant, then statically scan every place in the codebase that
writes to that column for string literals, and assert every one is a member of that
constant. This is now `tests/test_db_constraint_contract.py` -- it parses the known
writer files with Python's own `ast` module (no test double, no live DB) and would have
failed on day one of either bug: `'escalated'` is not in `support_tickets.status`'s
allow-list, and `'posting'` was not in `support_messages.delivery_status`'s allow-list
until this postmortem's own fix. The two-way guard matters as much as the check itself:
the test also asserts the allow-list constants themselves still contain `posting` and
`hold`+`escalated=True` (not the string `'escalated'`) -- so a future edit that quietly
narrows the allow-list back to the broken state fails immediately, not months later on a
real client's ticket.

The generalizable lesson, not specific to this table: a FakeBus (or any test double) is
only as good as the constraints it happens to enforce. When the real backing store has
constraints the double does not model -- a CHECK constraint, a foreign key, a uniqueness
rule -- passing tests prove the code's logic is internally consistent, not that it can
actually talk to production. A schema-contract test, run statically against the actual
DDL rather than a description of it, is the check that closes that specific gap, and is
worth having for any column with a narrow, hand-maintained CHECK constraint that
application code writes literals into.

---

## D56 (2026-09-05) -- "built but not wired": naming the pattern after its third instance

Blake, looking at #fixer with all four identities armed: *"#fixer is not autonomous. Every
ticket says 'the classifier did not decide' and every held draft is the escalation
placeholder, not a real answer."*

The direct cause was one line, present since this package's first commit (`a5a008a`,
2026-09-03) and never anything else:

```python
# agent/slack_convo/listener_wiring.py, live_deps()
answer=answer, classify_llm=None, log=log)
```

`classifier.classify()` consults its injected model ONLY after every deterministic rule
declines. With `classify_llm=None` hardcoded in the one function that builds production
dependencies, that branch was unreachable in production for the entire life of the system.
Every message the regexes did not recognise fell to `ESCALATE` -- not by failure, BY
CONSTRUCTION. `config.slack_convo_model()`'s own docstring had promised "the LLM fallback of
the classifier" the whole time; the env var could be set on the service forever and change
nothing.

Its twin, found the same day in the portal bridge (`RTF-2`): `echo_ticket_wiring.py` passed
`answer_lane.default_llm` -- signature `(system, user, model=None)` -- as the CLASSIFIER's
`llm`, whose contract is `(text) -> label`. Every call raised `TypeError`; `classify()`
caught it and escalated, exactly as designed for a model fault; and the outcome was
indistinguishable from "the classifier had nothing to say". **A wrong-shaped wire is the
same bug as a missing one, only harder to see.** This is the path the one real client ticket
of 2026-09-05 (`35e066d0`, the "nothing was recreated" report) actually travelled.

### The pattern, now three deep in one system

| # | Instance | What existed | What was connected | How it looked from outside |
|---|----------|--------------|--------------------|----------------------------|
| 1 | `delivery_status='posting'` | the outbox CAS, always | the CHECK constraint never allowed the state | every claim 400'd; NOTHING had ever posted |
| 2 | `listener_watch` | the watchdog loop, in the repo | nothing started it | a safety net that shipped inert |
| 3 | `classify_llm=None` | the LLM classifier, tested | `None`, hardcoded in `live_deps()` | "the classifier did not decide", forever |
| 3b | `llm=` in the portal bridge | a callable WAS passed | the wrong callable's shape | identical to instance 3, one layer subtler |

The shape is always the same, and it is why none of these were caught by tests: **the
capability exists, config says it is on, and the fallback path is a legitimate one.**
Escalating to a human when the classifier is unsure is CORRECT behaviour. Failing closed on
a model exception is CORRECT behaviour. That is exactly what makes this class invisible --
the broken state is byte-for-byte identical to a healthy system that simply had nothing to
say. "All tests green" said nothing about it, because every test injected its own working
fake into the seam that production left empty.

### The check that catches the whole class

`tests/test_not_wired_guard.py`, a sibling of `tests/test_db_constraint_contract.py` and
written in the same spirit -- static, no network, no test double:

1. **No hardcoded `None` capability in `live_deps()`.** The function is parsed with `ast`;
   any `*_llm=None` / `answer=None` written as a constant fails the test by name.
2. **The boot assertion is actually called.** `build_classify_llm()` refuses to boot
   (`NotWiredError`) when a flag says a capability is on and nothing can be built behind it,
   and `assert_classifier_shape()` refuses a callable whose signature is not `(text)`. The
   test asserts both are reachable from the real wiring path -- *an assertion nobody calls is
   itself an instance of the bug it exists to catch.*
3. **OFF must be loud.** A deployment running deterministic-only classification says so at
   boot. Silence is what let this live for two days; the OFF state is allowed, being unable
   to tell OFF from BROKEN is not.
4. **Every flag has a config reader**, so an env var set on the service cannot be inert.

### The generalizable rule

Pair the D55 lesson with this one and they cover both halves of the same failure:

> **D55:** a test double is only as good as the constraints it models; check the code against
> the real schema, statically.
> **D56:** a capability is only as real as its wiring; check that the flag, the seam, and the
> implementation are connected, statically -- and make the disconnected state fail loudly at
> boot rather than degrade into a legitimate-looking fallback.

Whenever a new capability ships behind a flag (the standing repo rule, and the right rule),
the flag's ON state must be unable to boot into a no-op. That is the whole fix.

### Live impact, checked against the bus rather than assumed

Queried directly (`support_tickets`, project `ooqcvmcjspeltuuhcvlh`, 2026-09-03 onward,
excluding `[phase4-audit]` probes and the `U0000000000` synthetic sender):

* **`source='slack_conversation'`: zero real client tickets, ever.** The Slack path where
  `classify_llm=None` lived carried no genuine client traffic in the whole window. Nobody was
  told anything wrong, and no client saw the placeholder. That is a good outcome and it is
  worth stating plainly rather than softening: the bug was real, its blast radius was not.
* **`source='website_tab'`: exactly one real client ticket affected** -- `35e066d0`
  (dale@brokerdale.realestate, 2026-09-05 01:02 UTC), a legitimate breakage report that
  escalated with `classification=null` where a working classifier would have opened a fix
  request. It travelled the RTF-2 wrong-shape path, not the `None` one.
* `a9efa713` (2026-09-04, "Can we add our group sessions schedule to the website?") never
  reached the classifier at all: it failed identity resolution first (`reporter` NULL, a row
  created ~18 hours before the portal began stamping `reporter` at all). Structurally
  unroutable, not a classifier failure. See D57.

---

## D57 (2026-09-05) -- a9efa713, and what "identity_unknown" was hiding

Ticket `a9efa713-c9f0-4688-9580-5a93dfa4b4f2`, portal Website tab, 2026-09-04 02:22 UTC:
*"Can we add our group sessions schedule to the website?"* It reached #fixer as
`Portal ticket a9efa713... (scout) could not be routed automatically: identity_unknown` and
stopped there. Two separate failures, traced independently.

**(a) Identity: structurally unfixable for this ticket, and not a bug today.**
`echo_ticket_worker.resolve_client_identity()` returns UNKNOWN before any lookup when the
row has no `reporter`:

```python
email = (ticket.get("reporter") or "").strip()
claimed_gym_id = ticket.get("client_id") or ""
if not email or not claimed_gym_id:
    return _ig.Identity(_ig.UNKNOWN, "", reason="ticket missing reporter or client_id")
```

This row's `reporter` is NULL and always was: the portal only began stamping `reporter`
in `db126a60` (2026-09-04 16:45), roughly 18 hours AFTER this ticket was inserted. There is
no email anywhere on the row to recover, so no code change can route it. A human attributing
it to its gym (`client_id b536c122-49b6-4b98-9021-b0713750bf82`) is the only path. Of the
five `website_tab` tickets in the table, this pre-fix row is the only one with a NULL
reporter; all four created after the fix carry one.

What WAS a bug is what the card said. `_escalate_unresolved` threw away the specific
`Identity.reason` and printed the coarse bucket, so "identity_unknown" was true, useless, and
indistinguishable from a Slack outage. Cards now carry the reason verbatim ("no reporter
email on this ticket"), plus the person and gym in words -- D53.

**Not closed, and reported rather than silently patched:** the current portal route can STILL
produce `reporter: NULL`. It initialises `reporter = null` and never rejects the insert if it
stays null, so three live paths still reach it: `clerkConfigured()` false (which ALSO skips
the `canReadGym` check), `auth()` returning no `userId`, and the `app_users` lookup missing or
erroring (the query's `error` is discarded, so a Supabase fault is indistinguishable from "no
row"). Narrowed, not closed. Fixing it means changing portal auth behaviour, which is Blake's
call, not this session's.

**(b) Routing: a website question had no path to the website bot.** `product='portal'` routes
to Scout, hardcoded as a literal pair in `listener.py`'s scheduler; Wrangler's
`product="websites"` is a label nothing polls for, and no producer ever writes it. So this
question could not reach the identity that knows about websites even by hand.

**D50, the fix, and the shape chosen deliberately:** cross-product routing changes WHICH
BOT'S KNOWLEDGE drafts the answer, and nothing else. When `classifier.product_hint()` is
CONFIDENT (an unmistakable website noun with no competing product noun) and the flag
`SLACK_CONVO_<IDENTITY>_CROSS_PRODUCT` is armed, the answer lane is called with the website
identity's knowledge and voice. The ticket's `bot_identity`, `product`, channel, thread,
`client_id` and every delivery decision are untouched, and `who` (the asking person, their
account key, their gym) is passed through unchanged, so every live fact is still keyed off
the asking gym's own account. A website question from Gym A is still a Gym A ticket answered
in Gym A's own conversation. Low confidence stays with the entry-point identity, unchanged.
Lainey can never be a routing target. The Frame 2 containment argument is asserted directly
in `tests/test_slack_convo_autonomy.py`, not just described here.

---

## D58 (2026-09-05) -- auto-answer is a narrower permission than CLIENT_REPLY, with hard lines

Arming `SLACK_CONVO_<IDENTITY>_CLIENT_REPLY` was reviewed as "the bot may reply to a client".
It silently also meant "the bot may send a model-written statement about that client's live
account, unattended" -- because a `kind=answer` row went `ready` on exactly the same flag as
an acknowledgement. Those are not the same permission and should never have shared a flag.

`SLACK_CONVO_<IDENTITY>_AUTO_ANSWER` now gates the grounded-answer path alone. It requires
the identity to be enabled AND client-reply armed (it can never be the flag that lets a bot
speak to a client at all), and acks, templates and status rows are unaffected by it.

**Hard lines, not tunable, checked at draft time AND at post time:** billing and price,
refunds, hours and schedule changes, injuries, liability. `adapter.AUTO_ANSWER_FORBIDDEN` is
a module constant with no env var behind it; a matching message is held for a tap no matter
what any flag says. Code fixes, action requests and anything the classifier was unsure about
were already held and stay held.

**D55 (receipts):** whenever a client is actually told something -- auto-answered, released
by a tap, or resolved by a human -- a RECEIPT card is written to #fixer AFTER the post
succeeds, quoting the exact text that went out, where, when, and whether it sent with no tap.
A receipt is never written for a delivery that did not happen, which is the whole point of
writing it after rather than before.

---

## D59 (2026-09-05) -- what the first independent audit found, and why it mattered

The wave above shipped green: 5415 tests passing, CI green, every new behaviour covered. A
fresh auditor with no shared context found **three CRITICALs in it**, all of which the suite
was green through. Recorded here because each one is a distinct lesson, not a typo.

**C1 -- the boot assertion could not fire.** `default_classify_llm()` built and returned its
closure unconditionally; the `ANTHROPIC_API_KEY` check lived inside `answer_lane.default_llm`
at CALL time. So `build_classify_llm`'s `NotWiredError` branches were unreachable for the only
factory production uses, and a keyless deployment booted, logged *"classifier LLM wired"*, and
escalated every message -- **the D51 flood wearing the badge of the fix for it**. The tests
passed because both "refuses to boot" tests injected `factory=lambda: None`: they proved the
seam and never the rule. The key is now checked at BUILD time, and the test that would have
caught this (flag on, key unset, production factory) exists. *A test that injects its own
failure proves the handler, not the requirement.*

**C2 -- a second door to the client with none of the gates.** The portal bridge's QUESTION
branch sends through `outreach.initiate`, not `outbox._dispatch_one`, so D54's trust ladder,
AUTO_ANSWER flag and hard lines never applied to it. The auditor reproduced a model-written
answer posting to a client's group DM with CLIENT_REPLY off, AUTO_ANSWER off, on a hard-line
topic -- using this system's own real ticket text. Every D54 test ran through
`adapter.handle_event`, which is exactly why a green suite could not see it. The gates now sit
in front of the send on that path too. *"Checked at draft time and post time" has to mean
every path that can reach a client, not every path that happens to use one module.*

**C3 -- a new internal kind is client-VISIBLE by default, in another repo.** Client visibility
is decided in lasso-ops-portal by a DENYLIST (`client-visible.ts` and migration 0310, both
listing exactly `escalation / fixer_request / hold_notice`). The new `kind='receipt'` was in
neither, so the receipt -- the fixer channel id, the ops status, and the words *"SENT
AUTOMATICALLY (no tap)"* -- was readable by the client in their own portal thread. Receipts
now ride on an `escalation` row with `attachments.receipt`, and `INTERNAL_KINDS <=
CLIENT_INVISIBLE_KINDS` is asserted by a test that fails the moment someone adds a kind. *A
denylist in another repo means every new internal kind ships visible until someone remembers
it; the contract needs a test on THIS side, because this side is where kinds are invented.*

The through-line with D56: all three are the same family. A capability that looks armed and
is not; a gate that exists on one path and not its twin; a safety list that defaults to
"allowed". **Green tests plus a careful build is not evidence. An independent read is.**

---

## D60 (2026-09-05) -- the second audit, and the fake that hid a CRITICAL

The D59 fixes shipped green (5425 tests). A second fresh auditor found **two more CRITICALs**,
one of them introduced BY the D59 fix, plus five MAJORs. The pattern in every one is worth
more than the individual bugs.

**C1 -- the fix for C2 threw on every invocation.** The new held-answer branch called
`write_hold_notice(tid=..., ...)` without `ident_name`, which the production callable
(`adapter.write_hold_notice`) requires. Every held portal answer raised `TypeError` inside
`intake_pass`'s per-ticket `except`: no hold card, no escalation row, nothing to the client --
and the ticket was already out of `status='new'`, so the intake poll never returned it again.
**Permanently silent in both directions**, on the exact path built to stop silence.

It was invisible because every test in three files passed `write_hold_notice=lambda **kw:
...`. A `**kwargs` sponge accepts any signature, including the broken one. *A fake that cannot
fail the way production fails is not a test double, it is a blindfold.* The regression test now
builds the REAL factory (`echo_ticket_wiring._write_hold_notice_factory`) and checks the call
site against the real signature.

**C2 -- `opened` is not `delivered`.** `OutreachResult.opened` means `conversations.open`
succeeded; it is `True` on `claim_failed`, `lost_claim` AND `post_failed`. Three callers read
it as "the client was told". So a failed `chat.postMessage` resolved the ticket AND wrote the
new receipt asserting *"the client was told this, SENT AUTOMATICALLY (no tap)"* over a row
whose own `delivery_status` was `failed`. The receipt -- the very thing added so Blake would
never have to wonder whether a ticket landed -- could state a delivery that did not happen.
`delivered` is now a separate field, true only after the post returns ok, and the three
callers read it instead.

**M1 -- the flags stopped at one branch.** D59 gated the QUESTION branch; its two siblings
(`acknowledge_submitter`, `fixed_pass`) DM clients through the same `outreach.initiate` and
checked no slack_convo flag at all, not even the identity master switch. "Flags off equals
today" was simply untrue for the portal bridge. *Gating the path the audit found is not the
same as gating the paths that share its door.*

**M2 -- "re-checked at post time" was re-reading a boolean.** Gate 5a trusted the stored
`attachments.auto_answer_forbidden`, so a row from any writer that omits it posted a hard-line
answer unattended. The body is re-evaluated now. The old test injected the marker and asserted
the lookup: it proved the boolean, not the rule, which is precisely how M2 shipped underneath a
test named for it.

**M3 -- a denylist of topics is whack-a-mole, and the auditor won.** Eleven ordinary sentences
walked past `AUTO_ANSWER_FORBIDDEN`: *"what time does the gym open on saturday"*, *"our
saturday classes are moving to 8am"*, *"a member tweaked her back, what do we tell her"*, *"how
much is this going to run us each month"*. So the rule is **inverted** for unattended sending:
`AUTO_ANSWER_ALLOWED` is an allowlist of what this system can actually observe in live account
state (connection status, posts, calendar, approvals, uploads) -- the entire universe
`answer_lane.default_fetch_state` can even fetch -- and a message must pass BOTH it and the
denylist. Anything phrased any other way holds for a person. *When a safety rule must
enumerate every dangerous phrasing, enumerate the safe ones instead.*

**M4 -- "refuses to boot" was caught by a catch-all one level up.** `NotWiredError` raised
inside `listener.py`'s `try` around `attach()` meant `attach` and `start_additional_identities`
were both skipped: **all four identities silently dark while the listener reported healthy** --
a worse instance of the exact pattern D56 named. It re-raises now, on both lanes, so a
misconfigured deployment crashes visibly instead of lobotomising itself.

**M5 -- a fix claim nobody verified.** `_fix_summary_text` said *"Fixed it and confirmed the
change is live"* whenever `verification_after` was merely non-empty; it never looked inside. A
snapshot saying `{"verified": false, "reason": "could not reproduce"}` would have been
announced to the client as a confirmed fix. It reads the verdict now, and makes no claim at all
when the snapshot does not affirmatively say the fix was verified.

Also from this audit: `TEMPLATE_UNKNOWN` and `TEMPLATE_QUEUED` carried the same promise-shape
D52 removed ("the team will pick it up there", "Someone will pick it up") and are reworded --
**the ban is on promising future human action as a fact, not on one particular sentence**, and
the test asserts the requirement now rather than grepping for the old string.

### The lesson that outranks all seven

Two independent audits, five CRITICALs between them, and the full suite was green through every
single one. Each bug lived exactly where the tests were shaped like the build instead of like
the requirement: a fake that accepts any signature, an injected failure that proves the
handler, a substring assertion that survives a dropped value, a marker check that proves the
boolean. **When a test and its subject were written by the same author in the same hour, the
test tends to encode what the code does, not what the rule is.** That is what an independent
read buys, and it is why the loop is two consecutive clean audits by fresh eyes, never one.

---

## D61 (2026-09-05) -- the third audit: half-fixes are their own failure mode

Third fresh auditor, third pair of CRITICALs, and both of them were **halves of the fixes the
second audit asked for**. That is the entry.

**Finding 1 (CRITICAL) -- M5's fix moved the lie one layer in.** `verification_succeeded` was
written as a denylist of five false strings, so `{"verified": "not verified"}`,
`{"verified": "pending"}`, `{"passed": "0 of 3"}`, `{"ok": "timeout"}`, `{"success":
"partial"}` and a bare PR link **all** read as success, and the client was told *"Fixed it and
confirmed the change is live."* This is the identical whack-a-mole shape M3 had just declared
unacceptable for auto-answer, reused one file away for the sentence that most directly lies to
a paying client -- written in the same hour as the ruling against it. The column is produced by
`ops-fix-triage.js`, in another repo, so its vocabulary is not ours to guess: an **allowlist of
affirmative verdicts** now, and anything unrecognised makes no claim at all.

**Finding 2 (CRITICAL) -- the allowlist existed on one of the two paths.** M3 introduced
`AUTO_ANSWER_ALLOWED` as *the primary gate* for unattended sending, and the portal bridge --
the exact path the previous audit's C2 was filed against -- called `auto_answer_forbidden`
twice and `auto_answer_allowed` never. *"a member tweaked her back, what do we tell her?"*
posted to a client's group DM, no tap, ticket resolved. The tests could not see it because they
asserted the **predicate** (`auto_answer_allowed(text)`) rather than the **call site**, so they
would have passed with the allowlist deleted from every caller -- which was very nearly the
state of the code. There is now ONE function, `adapter.may_auto_answer`, called by both paths,
and the tests drive the call sites.

Also closed: `release_approved_outreach` still read `opened` instead of `delivered` (a failed
tap marked the row `posted`, and `outreach_request` is not on the portal's hidden list, so a
client could read a message never sent); a present-but-INVALID API key builds fine and cannot
be caught at boot, so the classifier now LOGS every model failure loudly and escalates its own
log level after three consecutive ones -- the requirement is not "detect a bad key", it is
"never be silent about one"; `ACK_CODE_FIX` had restored V-M6's removed promise in a different
constant, and the promise test now scans EVERY client-facing constant by reflection rather than
three by name; `exclude_test` was inert in the daily cap (`select: "id"` meant the predicate saw
no columns); M4's `raise` in the scheduler lane would have killed the entire daily scheduler
thread, so "refuse to start" stays at boot and the same misconfiguration is merely loud per
cycle; and a routed answer introduced itself as *"You are Wrangler"* out of Scout's bot.

### The pattern this audit adds

D59 and D60 said green tests are not evidence. D61 says something narrower and more
uncomfortable: **a fix written in response to an audit is the least-reviewed code in the
repo.** It arrives with urgency, it is written by whoever just had the bug explained to them,
and it lands after the audit that would have caught it. Three of the seven findings here were
introduced by the previous round's fixes; two of those were CRITICAL. The loop is two
CONSECUTIVE clean audits for exactly this reason -- one clean pass after a fix wave proves
nothing about the fix wave itself.

---

## D62 (2026-09-05) -- the fourth audit: I guessed another repo's contract instead of reading it

**Finding 1 (CRITICAL) -- D61's own fix made `fixed_pass` permanently inert.** The audit-3 fix
replaced a denylist of false verdict strings with an allowlist of affirmative ones. Both were
guesses. The auditor did the thing neither previous round did: **went and read the producer.**
`~/scout-listener` `src/index.js`'s `runVerify` resolves

```js
{ phase, exit_code, tail, at }
```

-- a pytest exit code and no verdict word anywhere. So the allowlist matched **nothing the real
writer emits**: every verification snapshot fell through to "not a success", `fixed_pass` could
never notify a client for any real ticket, and it wrote a #fixer card asserting the
verification had failed over one that **passed**. Worse, that path flipped the ticket
`fixing` -> `hold`, which removes it from `find_fixing_tickets` forever, so a fix that verified
later could never be reported at all.

`verification_succeeded` now reads `exit_code` first, because that is what the producer
actually writes; keeps the affirmative-word path for other writers; requires ALL present
verdict keys to agree (finding 10: `{"status":"completed","result":"failed"}` read as
success); and distinguishes *unreadable* from *failed* -- an unreadable snapshot leaves the
ticket in `fixing`, still polled, and says exactly that in one bounded card instead of
announcing a failure that was never reported.

**The lesson, and it is the sharpest one in this whole run:** two consecutive rounds wrote a
rule about another process's data by reasoning about what such a process *probably* writes.
Both were wrong, in opposite directions, and the second one was wrong in a way that silently
disabled a capability rather than loudly breaking it. **A contract with another repo is read,
never inferred** -- and this one was two directories away the entire time. The same failure
also means the deeper gap stands and is REPORTED rather than papered over: the ops-fix worker
writes a *new* `source='ops_fix'` ticket (`src/index.js:334`, `intake.fromOpsFix`) rather than
writing `verification_after` back onto the portal-bridge ticket, so nothing populates that
column for these tickets today. `fixed_pass` is correct now and still has no producer. That is
Blake's call to wire, not this session's to invent.

**Finding 2 (MAJOR)** -- `ACK_CODE_FIX`, reworded in the previous wave to fix exactly this
class, told the client the request had been *"put in front of them"* while the card that does
that was marked `failed`. Reworded again to claim only that it was written up, and an internal
card that fails to post now logs CRITICAL rather than dying as a quiet `failed` row.

**Finding 3 (MAJOR)** -- the publish guard, added the previous wave, leaked eight ordinary
phrasings ("make a post that says we are moving to 8am", "let everyone know on instagram that
we are closed"). Enumerating polite request forms was the wrong axis; it matches a CONTENT VERB
plus a CLAIM MARKER now, in either order.

**Finding 4 (MAJOR)** -- `may_auto_answer` was created so "no path can enforce half the rule",
and the post-time gate then enforced half the rule: denylist only, never the allowlist. It runs
the whole decision now, using the ticket's own `raw_text` as the question.

**Finding 5 (MAJOR)** -- the cross-product voice fix swapped one token of the system prompt
while the appended VOICE DOC still named the other bot five times over. The voice doc now comes
from the bot that is actually speaking, and the routed subject is named by PRODUCT, so nothing
in the prompt gives the client a second bot's name at all.

Also: a transient release failure no longer burns Blake's Release card (finding 6); the
dead-key counter is read and rendered on the health line instead of being written to nobody
(finding 7 -- D56's pattern inside the fix for it, again); `is_test` got the migration file it
never had (finding 8); and `resolve_and_notify` refuses rather than marking a ticket resolved
when the trust ladder would hold the client's notice (finding 9).

### Four audits, nine CRITICALs, and what actually generalises

| Round | CRITICALs | How many were introduced by the previous round's fixes |
|-------|-----------|--------------------------------------------------------|
| 1 | 3 | -- |
| 2 | 2 | 1 |
| 3 | 2 | 2 |
| 4 | 1 | 1 |

The suite was green for every single one. Three rules earned the hard way:

1. **A contract with another system is read, not inferred.** (D62)
2. **A fix written in response to an audit is the least-reviewed code in the repo.** (D61)
3. **A test written by the author of the code, in the same hour, encodes what the code does
   rather than what the rule is** -- so it asserts predicates instead of call sites, injects
   its own failures, greps source text, and accepts `**kwargs` where production requires a
   signature. (D59, D60)

---

## D63 (2026-09-05) -- the fifth audit: reading one line further

**Finding 1 (CRITICAL) -- D62 read the producer's SHAPE and stopped one line short of its
SEMANTICS.** D62's whole lesson was "a contract with another system is read, not inferred",
and the fix it produced read `~/scout-listener`'s `runVerify` output shape -- `{phase,
exit_code, tail, at}` -- and treated `exit_code == 0` as a pass. The command that produces it
is:

```bash
python3 -m pytest -q 2>&1 | tail -5 || true; echo "__EXIT__:$?"
```

`|| true`, and `$?` is `tail`'s status rather than pytest's. **`exit_code` is always 0**, for a
passing suite and a failing one alike. So `verification_succeeded` became a constant `True` for
the only producer there is, and *"Fixed it and confirmed the change is live before sending
this"* would have gone to a paying client over a failing test suite. The inert bug D62 fixed
was replaced by a lying one.

The resolution is to stop trying to extract a verdict that is not there. **A field
structurally incapable of expressing failure is not a verdict**, so that snapshot is
UNREADABLE: no claim, ticket stays in `fixing` and still polled, one honest card to a human.
Parsing the `tail` text was considered and rejected -- it is the same guess wearing a different
hat. Announcing a verified fix requires an explicit verdict field, and until the ops-fix worker
writes one this path stays quiet. **That wiring is a cross-repo change and Blake's call**; it
is listed in the open items rather than invented here.

**Finding 2 (CRITICAL) -- the publish guard was rewritten on the wrong axis, twice.** D62's
rewrite REPLACED the previous alternatives rather than adding to them, and measured against a
realistic corpus it held **37% of ordinary state questions** ("were my posts scheduled?", "any
update on our posts?") while MISSING three publish requests the version before it caught ("post
the flyer for the open house"). Leakier and blunter simultaneously, and the test that was meant
to prove otherwise used five hand-picked strings.

The axis is not vocabulary, it is SHAPE: a request asks us to author something (imperative, or
a polite request form, or a content verb bound to a claim we would assert on the client's
behalf); a question asks what is already true. Claim markers are now only the ones that
introduce authored content ("that", "saying", "announcing", "telling") -- never ordinary words
like "were" or "our" that any question contains. Measured on a 20-question / 14-request corpus
that is now IN the test file: 0 false positives, 0 false negatives, both asserted per string.

Also closed: `resolve_and_notify` gated on the ticket's `identity_kind` while writing
`recipient_kind: "client"`, so a staff ticket passed the gate and held the row (finding 3); a
refused resolve tap did nothing visible at all, which is the dead button that path exists to
fix, and now writes the reason back to the channel it was tapped in (finding 4); leaving a
released row `held` on `claim_failed`/`lost_claim` let a retap send the client a SECOND DM,
because `_send` had already written a row another consumer owns -- three reasons now map to
three distinct states (finding 5); the voice-doc swap had removed the routed identity's doc,
which was the only thing D50 routing actually moves, so the routed guidance is kept with the
other bot's NAME rewritten to the speaker's, and the grounding record names both roles
honestly instead of claiming the wrong one (finding 6); and receipts ride on `kind=escalation`,
so counting escalations let one receipt permanently suppress the unreadable-snapshot card
(finding 7).

### Five audits: what the numbers say

| Round | CRITICAL | MAJOR | Introduced by the previous round's fixes |
|-------|----------|-------|-------------------------------------------|
| 1 | 3 | 1 | -- |
| 2 | 2 | 5 | 1 |
| 3 | 2 | 5 | 3 |
| 4 | 1 | 4 | 3 |
| 5 | 2 | 4 | 5 |

The suite was green for all eleven CRITICALs. The trend that matters is the last column: **the
fix waves are now the primary source of new defects**, which is the strongest possible argument
for the two-consecutive-clean-audits gate and against ever treating a single clean pass, or a
green suite, as done. Every round has also gotten narrower -- round 1 found "the classifier
never ran at all"; round 5 found a regex 37% too broad and a shell `|| true` two repos over.

---

## D64 (2026-09-05) -- the sixth audit: zero CRITICAL, and the two MAJORs worth reading

First round with **no CRITICAL findings**. Two MAJORs, and both are more interesting than the
bugs they describe.

**Finding 1 -- I applied the publish guard to our own answer.** `may_auto_answer` ran
`asks_us_to_publish` against the model-written BODY as well as the question. An answer
legitimately says *"your post about the new class went out tuesday"* -- a content verb next to
a claim marker -- so on an independent corpus **4 of 10 realistic answers were held**, and
3 of 6 legitimate grounded answers never reached `ready` end to end. D63's own record claimed
"0 false positives, 0 false negatives", measured on a corpus I wrote myself, with the default
EMPTY body -- so the test never exercised half the predicate it was proving. A request to
publish can only ever appear in what the PERSON wrote; our own reply cannot ask us to do
anything. The publish guard reads the question only now; the topic denylist still reads both,
because a hard-line subject can surface in an answer a benign question invited.

**Finding 2 -- D63's own explanation of the inert path was wrong, one step short again.** D63
said the verified-fix notification was blocked only by the absence of a verdict field. Reading
`~/scout-listener` one step further: the ops-fix worker polls `status='new'` and mints a
**brand new `support_tickets` row** (`src/index.js:334` -> `intake.fromOpsFix` ->
`store.insertNew`); the verification lands on THAT row. The ticket `fixed_pass` watches keeps
`verification_after` NULL forever. **Wiring a verdict field would not have fixed it.** That is
the third consecutive round where a claim about another repo was one read short of the truth.

The response is deliberately not a workaround: inventing our own verdict is precisely the
guessing D62 and D63 exist to condemn. Instead the inertness is made VISIBLE -- a ticket
sitting in `fixing` past 24h with no verification gets one honest card per day naming the exact
cause and saying a person must close it. **A capability that cannot work should say so on a
schedule, not wait silently to be discovered by an audit.** Wiring the worker to write back to
the originating ticket is a cross-repo change and stays Blake's call.

Also closed: the escalation-card bound had been reimplemented as a client-side scan of
`bus.messages(tid, limit=200)` -- ordered `created_at.asc`, i.e. the OLDEST 200 rows, so on a
long ticket today's card is outside the window and the daily bound silently vanishes. That is
verbatim the bug `count_outbound_kind_since`'s docstring exists to prevent, reintroduced by
hand to filter receipts out; it is a server-side filtered count now (finding 3). A failed
outreach Release tap wrote a log line and nothing a human sees (finding 4). The routed voice
doc's name substitution turned another bot's self-description into a FALSE ROLE CLAIM in a
client-facing prompt ("Scout is the LASSO team member who builds and maintains gym websites"),
and `\bname\b` case-insensitive over a whole doc would silently corrupt text the day routing
ever targets Echo, whose name is an ordinary English word -- no substitution now; the speaker's
own doc is the voice, and only the routed doc's non-identity lines travel (findings 5, 6).
`classifier_health` could not tell "flag off" from "flagged on and dead" (finding 8).
`exclude_test` called itself "the one call every report should use" and had no call site
(finding 9) -- D56's pattern again, now described honestly instead of promising.

Six of the auditor's six named test weaknesses are fixed, including the two that mattered: the
corpus now attaches realistic answer bodies (which is where finding 1 lived), and the
boot-assertion guard RUNS `live_deps` instead of grepping its source.

### Six rounds

| Round | CRITICAL | MAJOR | Introduced by the previous round's fixes |
|-------|----------|-------|-------------------------------------------|
| 1 | 3 | 1 | -- |
| 2 | 2 | 5 | 1 |
| 3 | 2 | 5 | 3 |
| 4 | 1 | 4 | 3 |
| 5 | 2 | 4 | 5 |
| 6 | **0** | 2 | 1 |

The severity curve finally broke. Round 1 was "the classifier never ran in production at all";
round 6 is a regex applied to one argument too many and a docstring one repo-read short. But
the gate is two CONSECUTIVE clean rounds, and round 6 was not clean, so nothing arms yet.

---

## D65 (2026-09-05) -- the seventh audit: two auditors disagreed, and the disagreement was the answer

Zero CRITICAL for the second round running. Three MAJORs, two of them mine from the previous
wave, and one of them is the most interesting thing in this whole sequence.

**MAJOR 1 -- the two audits contradicted each other, and both were right.** Audit 6 measured
that the body-side publish guard held 4 of 10 realistic answers, so D64 removed it. Audit 7
replayed the same predicate against the same commit and found it returned False for all six of
this repo's own `REALISTIC_ANSWERS` and for all 39 of its own legitimate bodies -- and then
measured that removing it cost **four new leaks for zero recovered answers**:

> "I **will publish** the open house flyer that afternoon so the slot is not empty."
> "I **can post** the third one for you now if you want it out today."

Both measurements were correct on their own corpora. What separated them is a distinction
neither round had named: **our own reply cannot REQUEST anything, but it absolutely can COMMIT
to something.** D64 removed the whole check on the strength of the first half of that sentence.
Descriptive answers ("your post about the new class went out tuesday") now pass; answers that
promise future action -- V-M6 and D52's named class, on the very capability being armed -- are
held by a separate, narrower `answer_commits_to_action`. Measured after: 0 false positives over
25 questions x 6 bodies, 0 false negatives over 16 requests and 5 committing answers.

**The lesson: when two independent measurements disagree, the disagreement is data.** The
temptation is to pick the newer one. The right move was to find the distinction that makes both
true, which is also the only version that survives a third corpus.

**MAJOR 2 -- the guard held 55% of natural phrasing, and the repo's corpus could not see it.**
`tell` and `let ... know` were CONTENT VERBS, and the polite-request rule matched a content verb
anywhere within 25 characters -- so *"can you tell me if my instagram is connected"* was a
publish request **by construction**. 11 of 20 natural questions held. Every corpus in this repo
was written by me, and none of them contained a single "can you tell me..." or "please let me
know..." -- the commonest way a person actually asks a bot something. Two fixes: telling ME is a
question and telling EVERYONE is a broadcast, so audience decides; and the polite rule now
requires the verb in VERB POSITION, because "how many posts are scheduled" is a noun and a
participle, not a request. 0/25 held after.

**MAJOR 3 -- the stuck-fixing card I added one round ago stated the wrong cause as fact.**
`_intake_one` sets `status='fixing'` BEFORE writing the fixer_request card, so a ticket whose
card is still HELD awaiting a tap is indistinguishable from the cross-repo gap by looking at the
ticket row alone -- and my card asserted the gap, and told Blake to close the ticket by hand,
when the correct action was to tap the Release button sitting in #fixer. It reads the ticket's
own rows now. It also claimed "nothing has been said to the client" while the acknowledgement
was two rows away.

Also: the resolve notice was posted as a THREAD REPLY INSIDE A DM (`surface` was the ticket's
*source*, which gate 7 does not recognise) where nobody looks; the ticket was stamped resolved
before the notice was delivered, so a post failure left a ticket permanently asserting a
resolution over a failed row -- and moving that stamp to delivery time quietly broke the
idempotence the status check had been doing double duty for, which the existing suite caught
immediately (the notice row is the record of the tap now); `_domain_guidance_only` truncated at
2000 chars against a 3396-char doc, dropping 41% of the routed guidance including its entire
Escalation section; and a compiled regex with an unformatted `{}` placeholder and no call site
sat in `answer_lane.py` -- D56's pattern, inside the wave that cites D56.

All four behaviours this wave shipped untested now have tests, the receipt test drives the REAL
`bus.Bus` over a PostgREST-shaped transport instead of reimplementing the filter inside its own
fake, and the "cross-repo contract test" that compared two constants in the same file now reads
the portal's actual TypeScript Set and SQL predicate at `origin/main`.

### Seven rounds

| Round | CRITICAL | MAJOR | From the previous round's fixes |
|-------|----------|-------|-------------------------------------------|
| 1 | 3 | 1 | -- |
| 2 | 2 | 5 | 1 |
| 3 | 2 | 5 | 3 |
| 4 | 1 | 4 | 3 |
| 5 | 2 | 4 | 5 |
| 6 | 0 | 2 | 1 |
| 7 | 0 | 3 | 2 |

Two consecutive rounds with no CRITICAL. The gate is two consecutive rounds with no CRITICAL
**and** no MAJOR, and that has not happened, so nothing arms.

---

## D66 (2026-09-05) -- the eighth audit: the hard lines were never structural

Zero CRITICAL for the third round running. Four MAJORs, and one of them is the finding that
matters most for arming, because it is about the promise Blake made non-negotiable.

**F3 -- "these are hard lines, not tunable" was not true.** `AUTO_ANSWER_FORBIDDEN` and
`AUTO_ANSWER_ALLOWED` were both bag-of-words ORs over the whole message, ANDed together. So any
message that ALSO named a post or a calendar passed both. Measured on the auditor's own corpus:
**8 of 10 injury/liability messages, 4 of 5 gym-hours messages and 4 of 5 billing messages would
have auto-sent with no tap** -- *"a member pulled a hamstring doing the workout in our reel,
should we take the post down"*, *"how many posts do we get for what we pay"*. The comment
claiming these hold "however it is phrased" was false, and had been since the hard lines were
written. Not introduced by any recent wave -- measured identical on the old code -- which makes
it worse, not better: it was the load-bearing safety claim of the whole capability.

Widening the denylist again was not an option; this system has now lost to whack-a-mole four
separate times (D61's verdict strings, D62's exit codes, D63's publish verbs, D65's polite
forms). The property that actually separates the two classes is **shape, not vocabulary**: an
auto-answerable message is a SINGLE SELF-CONTAINED QUESTION ABOUT OBSERVABLE STATE. The moment
it carries a second subject -- a third party, advice being sought, or simply too many words to
be one question -- it is not that, whatever nouns it contains. Three structural conditions on
top of the two lists, all checkable without enumerating a single topic. Measured after: 9/9
mixed-subject messages held, 15/15 ordinary questions still answered.

**F4 -- the commitment guard listed the verbs it was written against**, so 14 of 24 realistic
promises walked past it: *"I'll add a third post for friday"*, *"Let me get that scheduled"*,
*"That will go up this afternoon"*, *"Consider it posted"*. Listing verbs was the same mistake
as listing topics, made one round after the ruling against it. A promise is a **first-person
future marker**, whatever verb follows; the only first-person futures that are not promises are
perception and reporting ("I can see...", "I can confirm..."), so those are excepted and
everything else holds.

**F1 -- last round's fix was inert for the exact case it named.** The resolve notice was
threading inside a DM; D65 fixed it by reading the surface off the ticket's own inbound row
instead of its source -- and a portal ticket's inbound row carries `portal_ticket_bridge`,
which gate 7 does not recognise as a top-level surface, so the behaviour was byte-identical to
the bug. Its test asserted the HELPER on a Slack MPIM ticket, never `resolve_and_notify`, never
a portal ticket. The auditor's mutation test is the damning part: reverting the call site to the
old code left all 449 tests in the touched files green.

**F2 -- the stuck-fixing card still stated three things it did not know**: an unreadable bus
read became "was dispatched, close this by hand" (the wrong remedy, as fact); a request sitting
in `ready`/`failed`/`suppressed` counted as dispatched; and a HELD ack counted as the client
having been acknowledged. When it cannot tell, it now says it cannot tell.

Also: three helpers added last round scanned `bus.messages` (ordered ASC) and so read the
OLDEST 200 rows -- the exact bug `count_escalation_cards_since` was added to fix, reintroduced
by hand in three places at once, with a duplicate resolve notice to a client as one failure
direction; and `find_fixing_tickets` was the one poll with no test-ticket filter, so a probe
parked in `fixing` produced a card every day forever.

### Reported, not fixed: a cross-repo hazard to settle before arming anything

`~/scout-listener`'s FIXER worker polls `support_tickets?product=eq.echo&status=eq.new` with
**no `source` filter**, claims `new -> triage`, and hands `raw_text` to a Bash-armed headless
`claude -p --permission-mode acceptEdits`. Both the portal and this repo insert client-typed
tickets at `status='new'`. If `FIXER_ENABLED` + `FIXER_ECHO_ENABLED` are ever armed there, a
client's raw text reaches that worker **with no tap**, contradicting this repo's own "a code_fix
from a client is ALWAYS held behind Blake's #fixer tap" -- and the portal bridge silently loses
the ticket, because its status leaves `new` and `find_new_tickets` never returns it again.
Verified latent today: `FIXER_ENABLED` is absent from that service's env, so the poll loop never
starts. This is Blake's to rule on; it is not something to quietly patch from here.

### Eight rounds

| Round | CRITICAL | MAJOR | From the previous round's fixes |
|-------|----------|-------|-------------------------------------------|
| 1 | 3 | 1 | -- |
| 2 | 2 | 5 | 1 |
| 3 | 2 | 5 | 3 |
| 4 | 1 | 4 | 3 |
| 5 | 2 | 4 | 5 |
| 6 | 0 | 2 | 1 |
| 7 | 0 | 3 | 2 |
| 8 | 0 | 4 | 2 |

---

## D67 (2026-09-05) -- STOPPING THE LOOP: the auto-answer gate is the wrong shape, and that is Blake's call

Nine independent audits. Twelve CRITICALs. The suite was green for every one of them.

**Round 9 found the same CRITICAL as round 8, after the rewrite meant to fix exactly it.**
Round 8: Blake's named hard lines -- billing, gym hours, class-schedule changes, injuries,
liability -- did not hold; 8 of 10 injury messages auto-sent because they also named a post.
The fix made the gate structural (single self-contained question, no third party, no
advice-seeking shape, word cap). Round 9 then measured, end to end through the real
`handle_event` and the real `outbox.run_once`: **15 of 16 must-hold messages still posted to a
client with no tap.** *"we open at 5 now, does the calendar know?"* -- a gym-hours commitment.
*"can you cancel the story scheduled for tonight?"* -- an action request. *"did you guys take
money out twice this month?"* -- billing.

Four attempts now, each closing its measured cases and opening new ones:

| Attempt | Gate | Closed | Opened |
|---------|------|--------|--------|
| D54 | topic denylist | the listed words | everything phrased differently |
| D63 | + allowlist of observable nouns | most off-topic | any message that ALSO names a post |
| D65 | + publish-request shapes | request forms | polite question forms (55% FP) |
| D66 | + message shape (third party, advice, length) | mixed-subject examples | hours/schedule/action questions that are short, single-subject and personless |

**The pattern is not that the regex needs one more pass. It is that the question is the wrong
thing to classify.** A free-text sentence from a gym owner does not carry a reliable signal for
"is it safe to answer this unattended", and every rule that tries to extract one is an
enumeration -- of topics, of verbs, of shapes -- which is the failure mode this system has now
lost to five separate times (D61 verdict strings, D62 exit codes, D63 publish verbs, D65 polite
forms, D66 topics-again).

**What would actually work, and why I am not building it tonight.** Gate on what the ANSWER is
DERIVED FROM, not on what the question is about: auto-send only when the reply is a restatement
of a small enumerated set of fact keys from the grounding snapshot (`social_status.connected`,
`calendar_this_month` counts) and contains no sentence that is not traceable to one of them.
That is checkable mechanically, it fails closed on anything novel, and it does not care how the
question was phrased. It is also a redesign of the capability, not a patch -- it changes what
auto-answer IS -- and after nine rounds of me deciding the next fix on my own judgment, this
one is Blake's to make.

**So the flag is locked, not left as a landmine.** `SLACK_CONVO_<IDENTITY>_AUTO_ANSWER` set on
its own now REFUSES, logs the open finding by name, and points here.
`SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE=true` is the single deliberate act that says "I
have read D67 and I want it anyway". The point is that nobody discovers months from now that a
flag they set once quietly meant something they never reviewed -- which is D56's lesson pointed
the other way.

### Everything that is NOT blocked by this

The rest of the wave is audited clean and independent of auto-answer, and it is the part that
actually answers Blake's original complaint:

* the classifier is wired to a model in production for the first time (D51/RTF-2), behind a
  flag that refuses to boot if it is on with nothing behind it;
* the false-promise template is gone and cannot come back (D52), and no client-facing constant
  may promise future human action -- asserted by reflection over every one of them;
* cards name the person and the gym in words, with the specific unresolved-identity reason
  (D53); receipts show what the client was actually told, written only after delivery (D55);
* the eight arming probes are purged from #fixer and marked `is_test` so they never resurface;
* cross-product routing, verified clean by four consecutive auditors, moves knowledge and voice
  only -- never a ticket, a channel, a gym or a delivery.

### The count

| Round | CRITICAL | MAJOR | From the previous round's fixes |
|-------|----------|-------|----------------------------------|
| 1 | 3 | 1 | -- |
| 2 | 2 | 5 | 1 |
| 3 | 2 | 5 | 3 |
| 4 | 1 | 4 | 3 |
| 5 | 2 | 4 | 5 |
| 6 | 0 | 2 | 1 |
| 7 | 0 | 3 | 2 |
| 8 | 0 | 4 | 2 |
| 9 | 1 | 3 | 2 |

Round 9 also proved, by reverting each change alone against the full suite, that **five of the
previous wave's fixes are asserted by no test at all** -- including two that the wave's own
decision record cited as closed. Those tests were mine, and they were shaped like the code
instead of like the rule, which is the third lesson in D65 arriving for the third time.

**The honest summary: the severity curve broke (CRITICALs 3,2,2,1,2,0,0,0,1) but the loop did
not converge, and the one CRITICAL that came back is the safety promise the whole capability
rests on. Nothing is armed. The gate needs a redesign, and the decision is Blake's.**

## D68 (2026-09-06) -- Postmortem of the nine-round loop, and three fences closed

Blake, on being handed the D67 report: *"the postmortem is worth more than another pass."*
This entry is that postmortem, plus the three open items he ruled on the same morning.
It is written for the next session, not as a status update: if you are about to build a
gate, or about to trust a green suite, the two sections marked **READ THIS FIRST** are
the ones that will save you time.

---

# PART 1 -- THE LOOP

## What the numbers were

Nine independent audits of the Slack conversational adapter's auto-answer capability.
Twelve CRITICALs. **The full test suite was green for every single one of them** -- before
each audit, and after each fix wave.

| Round | CRITICAL | MAJOR | Of those, INTRODUCED by the previous round's fixes |
|-------|----------|-------|----------------------------------------------------|
| 1 | 3 | 1 | -- |
| 2 | 2 | 5 | 1 |
| 3 | 2 | 5 | 3 |
| 4 | 1 | 4 | 3 |
| 5 | 2 | 4 | 5 |
| 6 | 0 | 2 | 1 |
| 7 | 0 | 3 | 2 |
| 8 | 0 | 4 | 2 |
| 9 | 1 | 3 | 2 |

Read the last column again. From round 3 onward, **most findings in each round were
created by the previous round's fixes.** Rounds 3, 4 and 5 were, in the majority, the
build cleaning up after itself. The severity curve looks like convergence
(3,2,2,1,2,0,0,0,1) and was not: round 9 rediscovered round 8's CRITICAL, after the
rewrite whose entire purpose was to fix exactly that finding.

## Round 9, specifically

Round 8 found that Blake's named hard lines -- billing, gym hours, class-schedule
changes, injuries, liability -- did not hold. The fix made the gate structural rather
than topical: single self-contained question, no third party, no advice-seeking shape,
word cap. Round 9 then measured end to end, through the real `handle_event` and the real
`outbox.run_once`, and found **15 of 16 must-hold messages still auto-posted to a client
with no human tap**, including:

* *"we open at 5 now, does the calendar know?"* -- a commitment about gym hours.
* *"can you cancel the story scheduled for tonight?"* -- an action request, answered
  rather than performed or escalated.
* *"did you guys take money out twice this month?"* -- a billing question.

Any one of those going to a paying gym owner unattended is the failure the whole
capability was gated to prevent.

## Five fixes were asserted by no test at all

Round 9 also ran a mutation check: revert each of the previous wave's fixes
INDIVIDUALLY, run the full suite, see whether anything goes red. **Five stayed green** --
meaning five fixes were held in place by nothing but the fact that nobody had touched
them since. Two of those five were cited as CLOSED in this very decision log by the wave
that shipped them.

That is the more useful half of the finding, because it generalises past this feature:

> **A test written alongside a fix tends to be shaped like the code, not like the rule.**
> It asserts what the function now does. Revert the function and rewrite it a different
> wrong way, and the test still passes. The only cheap way to know a test is load-bearing
> is to break the thing on purpose and watch it fail.

Mutation-checking is now the standard for this system, and every behaviour change in
Part 2 below was put through it, with the results recorded.

## READ THIS FIRST -- why the loop did not converge

Not because the regex needed one more pass. **Because classifying the QUESTION is the
wrong thing to gate on.**

A free-text sentence from a gym owner carries no reliable signal for *"is it safe to
answer this unattended."* There is no feature of the input that separates the safe cases
from the unsafe ones, because safety is not a property of the question -- it is a
property of the ANSWER, and of what that answer is derived from. Every rule that tries to
extract a safety signal from the question is therefore an **enumeration**: of topics, of
verbs, of shapes. And enumerations lose to novel phrasing, always, because the space of
phrasings is open and the enumeration is finite.

Four successive gate designs, each of which closed its own measured cases and opened new
ones:

| Attempt | Gate | Closed | Opened |
|---------|------|--------|--------|
| D54 | topic denylist | the listed words | everything phrased differently |
| D63 | + allowlist of observable nouns | most off-topic | any message that ALSO names a post |
| D65 | + publish-request shapes | request forms | polite question forms (55% false positive) |
| D66 | + message shape (third party, advice, length) | mixed-subject examples | hours/schedule/action questions that are short, single-subject and personless |

### Name it so you recognise it early

**ENUMERATING AN OPEN SET.** The tell, in every instance: your gate is a list of things
that are bad (or a list of things that are good), the list is drawn from the examples you
happened to measure, and the input space is natural language, user-supplied identifiers,
or another process's output. Each round you add the newly-found case to the list and the
measured failures go to zero, which feels exactly like progress. It is not progress; it
is fitting to the sample.

This system has now lost to this same shape **five separate times**, in five different
files: D61 (verdict strings), D62 (exit codes), D63 (publish verbs), D65 (polite forms),
D66 (topics again). Two more instances are fenced in Part 2 of this entry alone.

**If you are on round three of "add the missed case to the list", stop. You are not one
case away. Change what you are gating on.**

### READ THIS FIRST -- what the gate design should be instead

Gate on **what the ANSWER is derived from, not what the question is about.**

Concretely: auto-send only when the reply is a restatement of an enumerated set of fact
keys from the grounding snapshot (`social_status.connected`, `calendar_this_month`
counts, and so on) and contains nothing else -- no sentence not traceable to one of those
keys.

Why this inverts the failure mode, which is the whole point:

* The old gate's input was **open** (any sentence a human might type) and its rule was a
  finite list, so novel input defaulted to ALLOW. Every unanticipated phrasing was a
  potential auto-send.
* The new gate's input is **closed** (the fact keys the grounding snapshot actually
  contains, which this codebase defines) and anything outside it defaults to REFUSE.
  Novel input has nowhere to match, so it holds.

So it is mechanically checkable, it fails closed on anything novel, and it is **completely
indifferent to phrasing**: *"did you guys take money out twice this month?"* is not held
because it matched the word "billing", but because no fact key in the grounding snapshot
answers it, so no compliant reply can be constructed at all. The unsafe cases stop being
cases to enumerate and become a structural impossibility.

Note what this changes: auto-answer stops being "answer the question unless it looks
dangerous" and becomes "restate known facts, or say nothing". That is a **redesign of the
capability**, not a patch on the gate, which is why D67 stopped rather than shipping a
fifth attempt, and why it is Blake's call and not the build's.

**Nothing is armed. The hold lane is untouched by this entry.**
`SLACK_CONVO_<IDENTITY>_AUTO_ANSWER` still refuses on its own and still requires
`SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE=true` as a deliberate act.

## The other thread: "built but not wired", now four deep

D56 named this pattern after its third instance. It has a fourth, closed in Part 2:

| # | Instance | What existed | What was connected | How it looked from outside |
|---|----------|--------------|--------------------|----------------------------|
| 1 | `delivery_status='posting'` | the outbox compare-and-swap, always | the CHECK constraint never allowed the state | every claim 400'd; NOTHING had ever posted |
| 2 | `listener_watch` | the watchdog loop, in the repo | nothing started it | a safety net that shipped inert |
| 3 | `classify_llm=None` | the LLM classifier, tested | `None`, hardcoded in `live_deps()` | "the classifier did not decide", forever |
| 3b | `llm=` in the portal bridge | a callable WAS passed | the wrong callable's shape | identical to 3, one layer subtler |
| 4 | `verification_after` in `fixed_pass` | the fix-verified notify path | no producer writes that column on that row | "no fix has been verified yet", forever |

The shape is always the same, and it is why tests never catch it: **the capability
exists, config says it is on, and the inert state is byte-for-byte identical to a
legitimate healthy one.** Escalating when the classifier is unsure is correct. Waiting
when a fix is not yet verified is correct. Nothing posting because there is nothing to
post is correct. That is precisely what makes the class invisible -- and "all tests
green" says nothing about it, because every test injects its own working fake into the
exact seam production left empty.

### The general check that catches this class

**For every capability, name the PRODUCER of the value it gates on, and assert that
producer exists -- statically, in a test, with no test double and no live service.**

That one question ("what process, by name, writes this?") would have caught all five
instances. It is deliberately not "test the integration", which is expensive and which
teams skip; it is a static assertion about wiring, and it is cheap. Three now exist in
this repo, in the same spirit:

* `tests/test_db_constraint_contract.py` (D55) -- parses real `pg_get_constraintdef`
  output into an allow-list constant and `ast`-scans every writer for literals outside
  it. The producer of a legal value is the constraint itself.
* `tests/test_not_wired_guard.py` (D56) -- `ast`-scans `live_deps()` for hardcoded `None`
  capabilities, asserts the boot assertions are reachable from the real wiring path
  (*an assertion nobody calls is itself an instance of the bug it exists to catch*), and
  requires the OFF state to be loud, so OFF is distinguishable from BROKEN.
* `FIX_VERIFICATION_PRODUCERS` + its two-way guard (Part 2 below) -- an explicit, empty
  registry of the processes allowed to write a fix verdict, and a test that fails if it
  is ever filled without a corresponding cross-repo wiring change.

And the two-way guard is as important as the check: assert the allow-list still CONTAINS
what it must, not only that writers stay inside it. Otherwise a future edit quietly
narrows the list back to the broken state and everything stays green.

---

# PART 2 -- THE THREE ITEMS BLAKE RULED ON (2026-09-06)

## D68.1 -- `reporter: NULL` closed at the source (lasso-ops-portal)

Blake: *"All three portal write paths must stamp a reporter or reject the submit. Clerk
unconfigured is not an excuse to write a null."*

The investigation was told to expect three NULL paths in one route. It found **two
routes and an RLS policy**, and four distinct NULL branches in the first route:

| # | Path | What it did |
|---|------|-------------|
| A | `api/gyms/[gymId]/support/route.ts` | reporter from a SECOND Clerk `auth()` + `app_users` lookup |
| A1 | -- Clerk unconfigured | block skipped entirely -> NULL |
| A2 | -- no `userId` | NULL (unreachable today; the 403 above catches it first) |
| A3 | -- no `app_users` row | NULL (likewise unreachable) |
| A4 | -- **`error` never bound** | any transient failure of that lookup silently wrote NULL and returned 200 |
| B | `api/gyms/[gymId]/ranger-request/route.ts` | **`reporter` absent from the insert entirely -- NULL on 100% of rows** |
| C | RLS policy `"clients can submit tickets"` (`0302`) | constrains only `client_id`; any authenticated JWT may insert directly via PostgREST with `reporter` spoofed or omitted |

Why a NULL reporter is not cosmetic, both reasons worth knowing:
`echo_ticket_worker.py` treats a portal ticket carrying a reporter as D42 provenance
("an authenticated portal session") and will act on it autonomously; and the support
route's per-reporter daily flood cap is written `if (reporter)`, so **a NULL reporter
silently bypassed the rate limit as well.**

**Closed now.** One resolver, `src/lib/auth/ticket-reporter.ts`, used by both routes, so
the rule cannot drift between them again. `GymAccess` now carries the `email` that
`getGymAccess()` was already looking up and discarding -- which deletes A4 outright by
removing the duplicate query rather than by checking its error. A2/A3 became explicit
refusals instead of defence-by-adjacency ("the line above catches it" is exactly how the
NULL survived review). Path B stamps the reporter and, while there, `author_id` was
found written NULL on every Ranger message ever sent: the expression was
`(access as { userId?: string })?.userId` and `GymAccess` has no `userId` field -- the
cast silenced the type error that would have said so.

**A1 and C are NOT closed, deliberately, and are with Blake.** Both are authorization
changes rather than bug fixes. When Clerk is unconfigured, `getGymAccess()` returns null
and BOTH routes' checks read `clerkConfigured() && !canReadGym(access)` -- so the
authorization check is skipped entirely and the only remaining gate is the spoofable
Origin/Referer guard. Rejecting would turn "anyone past the origin guard may submit"
into "nobody may submit" in **local development and Vercel PREVIEW deploys** (`proxy.ts`
documents that the Clerk keys are Production-scoped there). Production and CI both carry
keys and are unaffected either way. `CLERK_OFF` in the resolver is a single named
constant so the flip is one edit, and `tests/support-reporter.test.mjs` locks in today's
behaviour with a comment naming the environments that change when it is flipped.

## D68.2 -- `verification_after` refuses to be read as a pass

Blake: *"Either wire the producer or make the field refuse to be read as a pass. An
inert verification field is worse than no field."*

`fixed_pass()` polls `status='fixing'` and waits for `verification_after`. Nothing can
ever write it: scout-listener's ops-fix worker polls `status='new'`, MINTS A NEW
`support_tickets` ROW (`intake.js`), and writes its verification onto that row
(`store.js` `setVerificationAfter`). The originating ticket's column stays NULL forever.
Instance 4 in the table above.

**Chose REFUSE, not wire.** Three reasons:

1. Wiring the producer newly arms a client-facing "your fix is verified" DM path that has
   never once fired -- arming an untested client-facing lane at the end of a nine-round
   audit loop is the exact move D67 declined.
2. It is contradictory in the same pass as D68.3, which deliberately REDUCES that same
   worker's blast radius. Growing its responsibilities while fencing it is incoherent.
3. **The column is overloaded.** The ANSWER lane writes its grounding snapshot to this
   same `verification_after` (`answer_pass`, `slack_convo/adapter.py`). That lane is
   wired, correct and untouched -- but it means a non-NULL `verification_after` has never
   meant "a fix was verified", so "populated" was never a safe proxy even before the
   producer gap. The right producer design (write back to the originating row, or have
   the bridge follow the minted one?) is a genuine cross-repo decision, not a detail.

**What refusing looks like.** `FIX_VERIFICATION_PRODUCERS`, an explicitly EMPTY frozen
registry, not a flag and not settable from the environment. `read_fix_verification()` is
now the only sanctioned way to read that column as a fix verdict: it RAISES
`InertVerificationLane` while the registry is empty, and once a producer is registered it
still returns None for a snapshot carrying no recognised `producer` -- so the answer
lane's grounding snapshot can never be mistaken for a fix verdict. `fixed_pass()` no
longer opens with `if not ticket.get("verification_after"): continue`, a line
indistinguishable at every call site and in every log from a healthy "not yet". It
refuses by name, every cycle, and says so in its return value
(`{"notified": 0, "refused": "fix_verification_lane_unwired", "fixing": N}`) so a caller
or a metric can tell IMPOSSIBLE from WAITING. `_report_stuck_fixing` still runs first:
refusing to CLAIM a fix is not refusing to REPORT one.

Mutation-checked. Reverting the refusal, or making the accessor return None instead of
raising, or dropping the producer-attribution check, each turns tests red -- including
`test_fixed_pass_refuses_rather_than_silently_polling_a_gate_that_cannot_open`, which is
built from a ticket verified as hard as a ticket can be.

## D68.3 -- the highest-risk item: fencing the FIXER worker (scout-listener)

Blake: *"Fence it now: source allowlist plus no Bash on that path. This is the highest-risk
thing in the report and it is another service's posture, so I want it closed not flagged."*

**What was true before.** `store.listNew()` polled
`?product=eq.echo&status=eq.new` with **no source filter at all**, and the row's
`raw_text` became the prompt of a headless Claude process whose grant was
`--allowedTools Bash,Read,Grep,Glob,Write,Edit,MultiEdit` with
`--permission-mode acceptEdits`. The deny-list held specific Bash PATTERNS
(`Bash(git push:*)`, `Bash(gh pr merge:*)`, ...), not Bash itself. Latent only because
`FIXER_ENABLED` / `FIXER_ECHO_ENABLED` are off -- one env var from firing.

An `intake.js` allowlist (`echosupport`/`portal_social`/`ops_fix`) existed but sat on the
WRITE side and was bypassable two ways: `slack-adapter/messages-store.js` inserts rows
directly without going through `intake.record()` (default source `slack_adapter`), and
anything holding `SUPABASE_SERVICE_ROLE_KEY` bypasses RLS by design -- which includes the
portal's own `website_tab` and `coach_portal` client-submitted tickets. So the worker's
real trust boundary was "any row with `product='echo'` and `status='new'`", whoever wrote
it. Its own preamble meanwhile stated it was reading *"an internal Echo system alert ...
never a client"*. It was reading clients.

**Fence 1, provenance.** `src/fixer/sources.js` now holds both allowlists side by side,
because the two are only meaningful read together: writing a row and being picked up by
the fix worker are different privileges, and the second is strictly smaller.
`WORKER_POLL_SOURCES` is `{'ops_fix'}` -- the one lane whose text Echo's own automation
writes. Enforced in TWO places on purpose: server-side in the PostgREST query so
untrusted rows never leave the database, and again in JS over whatever comes back, which
is what holds when the query string is wrong or a client is faked. Fails closed on a
missing, mistyped or non-string source. `echosupport` / `portal_social` rows are still
recorded, carded and nudged -- they are simply never turned into a prompt.

**Fence 2, no shell.** Tool grant before and after:

```
before   --allowedTools     Bash,Read,Grep,Glob,Write,Edit,MultiEdit
after    --allowedTools     Read,Grep,Glob,Write,Edit,MultiEdit
before   --disallowedTools  Bash(git push:*),Bash(git push),Bash(git commit:*--no-verify*),
                            Bash(git push:*--force*),Bash(gh pr merge:*),Bash(git merge:*),
                            Bash(railway:*),Bash(gh workflow run:*),Bash(gh release:*)
after    --disallowedTools  Bash,  + all of the above
         --permission-mode  acceptEdits   (unchanged)
```

Blanket `Bash` on the deny-list AND removed from the grant, so two independent controls
must both fail to restore a shell (deny beats allow in Claude Code). Blanket rather than
more patterns because the pattern list is an ENUMERATION OF AN OPEN SET -- the exact
failure Part 1 is about. `git push` has a hundred spellings: an alias, a `sh -c`, a
script written with `Write` and then executed. `fixAllowedTools()` throws rather than
returning a grant containing a shell tool, so a bad edit fails at startup instead of
quietly arming one.

**And the reason this is not just a config line.** Committing was the ONE step on this
path that genuinely needed the shell -- worker.js already did the isolate, branch, push,
PR and both verify runs in JS, but the preamble told the MODEL to `git commit`. Removing
Bash without moving the commit would have left the fix lane structurally unable to
produce a PR: instance 5 of "built but not wired", created by the very entry that names
the pattern. So the commit moved into `worker.js` where every other git call already
lived, bounded by step 3's existing hard-reset + clean, refusing an empty diff rather
than opening an empty PR, with the model's summary as the commit body (control chars
stripped, trailers neutralised, passed as one argv element -- `execFile`, never a shell).
The preamble was rewritten to match, including the `OUTPUT SHAPE` section, which used to
require a commit sha and a suite pass count the model can no longer honestly produce.

Fifteen tests in `test/fixer-source-fence.test.js`, written as security properties. The
poll fake deliberately IGNORES the query string, so the tests would pass vacuously if the
fence lived only in the URL. All four mutations -- remove the source fence, restore Bash
to the grant, remove the blanket deny, remove the worker commit -- turn tests red.

**Residual, noted not fixed (scope):** the prompt fence around `raw_text` in
`src/index.js` has an asymmetric delimiter (`<<<UNTRUSTED_REQUEST` opening,
`UNTRUSTED_REQUEST` closing) and checks neither for occurrence inside the text, so
crafted text can close the fence early. This matters much less now -- the text reaching
that prompt is `ops_fix` only, and the process has no shell -- but it is real. Also
`websites-worker.js` passes `raw_text` to `runFix` with no sanitisation at all, and
`armFixer` silently drops the `workDir` it is passed (harmless only because the fallback
literal happens to equal `cfg.ECHO_WORK_DIR`).

## D69 (2026-09-07) -- the client-DM support lane, rebuilt after seven rounds of not converging

PR #66 (`feat/client-dm-autofix`, ~2,900 lines across 11 modules) built an autonomous
support lane for a client's own Slack message. Its own audit history graded
**D -> B -> B -> C -> B -> C -> B**. That is not a convergence curve, and Blake's
instruction was the same one D68 records: diagnose why it oscillates, then fix the
DESIGN, not the instances. This entry is the diagnosis and what replaced it. Nothing
is armed; `AGENT_CLIENT_DM_AUTOFIX` still defaults OFF.

### PART 1 -- WHAT ACTUALLY HAPPENED, ROUND BY ROUND

| Round | Grade | What it FIXED | What it BROKE or newly exposed |
|-------|-------|---------------|-------------------------------|
| 1 | D | 2 CRITICAL + 7 MAJOR: the poll's `source` was guessed (`slack`, not `slack_conversation`); surface read off the ticket, not the message; the gym key was a portal uuid; `find_new_tickets` filters `classification is.null` so no classified DM was visible; both delivery sinks dead (no `identity` stamp; escalations written `held` while the outbox reads `ready`); a client-controlled Drive folder name interpolated into an auto-sent reply; `requires` was a presence test; unnormalised scope paths; no tenant re-check in the diagnostic | exposed the AST ad-scanner as bypassable three ways; 6 guards asserted by nothing |
| 2 | B | 5 MAJOR: the arming docstring was false in both halves (arming takes TWO flags); the lane read the client's OLDEST message; four more scanner bypasses; `requires_true` had no polarity twin | 8 more guards asserted by nothing, incl. the entire round-1 slot-bounding commit |
| 3 | B | 4 MAJOR: two controls at the ticket->gym seam, not one (a wrong answer ran gym B's sync and wrote gym B's counts into gym A's thread); **the AST-scan-as-proof claim withdrawn after 11 MORE bypasses**; the "forbidden ad import" table named no ad-money surface | 10 more guards asserted by nothing; 2 first-attempt tests were shadowed by a downstream check and stayed green under mutation |
| 4 | C | 1 CRITICAL + 4 MAJOR: **the poll asked for statuses a client question never lands in** -- both documented cases land in `hold`, the poll asked `("new","triage")`, so the lane was inert on exactly the two shapes it exists for; `confirm_gym_binding` was half tautology; the ad keyword belt missed 10 of 10 plain phrasings | the starvation fix (paging past answered tickets) |
| 5 | B | 3 MAJOR: the round-4 starvation fix MOVED its failure (stopping rule and loop used different predicates); `AD_RAIL_MARKERS` could be emptied with all 6501 tests green (the test derived its data from the table under test); the card said "I auto-replied" before delivery was known | -- (also fixed its own mutation harness, which shared one backup path) |
| 6 | C | 1 CRITICAL + 3 MAJOR: **`store` had no production default**, so the flagship remedy TypeErrored in production while its docstring claimed the opposite; a success reply claimed an outcome no fact key measures; one ticket's exception sank the whole pass; two of Blake's hard limits mutated GREEN | -- |
| 7 | B | 3 MAJOR: `cta_section_is_todo` did not measure what its name said (a gym with three working CTAs was auto-told it had none); `limit` limited nothing (`run_once(limit=3)` handled 40 tickets); a single-source assumption under singular client-facing copy | 3 residuals: the section detector was wider than the extractor; `multi` counted every source not every active one |

### PART 2 -- THE DIAGNOSIS. It is NOT D68's pattern, and that matters.

The obvious answer was "this is D68 again -- enumerating an open set." **The evidence
says otherwise, and getting this wrong would have produced another wrong fix.**

D68's pattern requires that novel input default to ALLOW. The reply gate here did the
opposite from commit 1: `reply.assert_is_template_render()` reconstructs the reply from
a registered template over enumerated fact keys and demands byte equality, so novel
input has nowhere to match and REFUSES. **And it held. After round 1's slot-injection
fix, not one of rounds 2-7 found an escape from the reply gate.** The design D67 asked
for was built and it worked.

Three real causes, ranked by how many findings they account for:

**1. Contract-guessing about other modules (rounds 1,2,3,4,5 -- the majority of all
findings).** The poll's `source`; the surface's location; the gym key's type;
`find_new_tickets`'s hidden `classification` filter; the outbox's `identity` stamp; the
outbox's `ready`-only read; `bus.recent_messages`'s DESC ordering; the two-flag arming
truth; the ticket statuses a client DM actually lands in. Nine separate contracts of
OTHER modules, each guessed, each wrong, each found one round at a time. This IS an
open-set problem -- D68's own sentence includes "another process's output" -- but the
open set was **the production environment**, not the client's sentence, and no amount
of work on the gate could reach it.

**2. Fake-injection blindness.** The named finding, and it hid BOTH round-7-era
defects. Every test injected `deps=drive_deps(store, sync)` into the exact seam
production left empty, so `store`-with-no-default survived six rounds; the poll's fake
ignored its query params, so a selector matching zero real rows survived four. In both
cases the broken state was byte-identical to a healthy one.

**3. The closed set was closed over KEYS, not over MEANINGS -- and this is where the
honest answer to "did the discipline hold end to end?" is NO.** The gate proves every
slot traces to a fact key. It cannot prove the fact key is right, and it cannot see the
template's constant prose at all, because the reconstruction contains the same words.
Every round-6 and round-7 finding lives in that gap: a sentence no fact measured ("New
posts will draw from those"), a fact whose name promised more than its query
(`cta_section_is_todo`), a detector wider than the extractor it fed, a count that
included rows the selector rejects. So: **the closed-set discipline was applied at the
final reply step and nowhere upstream.** Routing, diagnosis and fix-planning could and
did hand the reply stage a wrong-but-well-formed fact set, and the gate passed it.

Underneath all three: **surface area**. 11 modules, ~2,900 lines, five registries
joined by string name, for two diagnosable conditions. D68's last column applies here
too -- rounds 3-5 were, in the majority, the build cleaning up after itself.

### PART 3 -- THE REDESIGN

Six modules, ~1,500 lines including the honest docstrings. The changes that matter:

1. **A Reading is ONE object holding the measurement, the production function that
   produces it, and the only sentence Echo may say about it.** `facts.py` +
   `diagnostics.py` + `reply.py`'s templates collapse into `readings.py`. A name, a
   measurement and a claim can no longer drift apart, because they cannot be edited
   separately. There is nowhere in the package to type a client-facing sentence that
   is not attached to a measurement -- which closes round 6's constant-prose hole
   structurally rather than by asking the next author to write carefully.
2. **`say_when` replaces `requires`/`requires_true`/`requires_false`.** One field with
   one meaning cannot have a missing polarity twin (rounds 1, 2 and 3 each found one).
3. **A Condition is ONE object holding the predicate, the action, the comparator that
   proves it worked, and the readings it reports.** `remedies.py` + `scope_gate.py`
   collapse in. The registry refuses an action with no expectation, so "verification
   skipped" is impossible to express rather than possible to route around.
4. **A closed set of ONE action, in one domain.** No action-kind DSL, no code-fix lane,
   no 420-line scope gate. Ad budget/targeting/campaigns, billing, pixel/CAPI, secrets,
   schema, flags and cross-gym data are not cases this code declines -- they are things
   it has no way to express. The AST scanner survives, renamed `no_ad_rail.py`,
   labelled a TRIPWIRE, with nothing resting on it.
5. **No second implementation of any reading.** `cta_pool_count` is
   `agent.voice._extract_ctas`; `drive_library_usable` is
   `agent.gym_media_selector.is_usable` (newly exported, and `pick_media` now calls it,
   so there is one predicate rather than two). A test asserts the package compiles no
   regex of its own outside two anchored key-shape patterns.
6. **Tests that call the real production default with no fake**, plus a static check
   that no entry point has a seam production cannot fill.

**On the belt that still misses.** `tests/test_client_dm_lane.py::BELT_MISSES` records
three phrasings the keyword belt provably does not catch -- *"did you guys take money
out twice this month?"*, *"we open at 5 now, does the calendar know?"*, *"can you look
at the other gym's account"* -- which are D67's round-9 cases verbatim. They are
recorded rather than fixed, and there is a test asserting the belt does NOT catch them,
because adding them is D68's failure mode. Each still produces no reply: not because a
keyword matched, but because no probe measures a fact that answers them, so no
condition matches and no sentence can be constructed at all.

### PART 4 -- LIVE SUPABASE DISPROVED THREE THINGS

Read-only against the production project, 2026-09-07 -- something no prior round did,
which is part of why its grade was not trustworthy.

* **The poll matched zero real rows on BOTH axes.** All three non-test
  client-originated tickets are `source='website_tab'`, `status='hold'`; every
  `slack_conversation` row in the table is a phase-4 arming probe with `is_test=true`.
  The lane polled `source='slack_conversation'` with statuses `("new","triage","hold")`.
  Both are now pinned constants covering what production actually writes.
* **`media_source.gym_id` and `media_asset.gym_id` DISAGREE for 2 of 12 DISTINCT
  connected gyms** -- corrected here per the audit of PR #68: "17" was a
  `media_source` ROW count, not a distinct-gym count. The documented account-key
  split-brain landing on this lane's flagship
  fact. `toughtemple086f51`'s source owns 70 assets stamped `toughtemple52040e`;
  `crossfitsunnyside2616ac`'s owns 13 stamped `crossfitsunnysidef574c0`. Asked "why do
  my posts have no photos?", a gym-id-keyed probe for `toughtemple52040e` -- which is
  the key the portal hands us, and John Weeks's gym -- finds NO source at all while
  counting 70 assets. `drive_identity_split` is now measured and every condition
  refuses on it.
* **Multiple active sources per gym is real, not an edge case:** `train7164ae502` has
  six. Every Drive sentence here is singular, so it escalates.
* Also confirmed: **Chad Edwards's case resolved itself.** `crossfitlocal` now holds 54
  usable assets -- the scheduled sync ran, as the leading hypothesis said. The lane
  therefore matches no condition for him today and cards a human, which is correct.

### PART 5 -- THE TWO-FLAG LANDMINE, CLOSED

A reply from this lane is a CONVERSATIONAL kind, and `outbox._dispatch_one` gates a
client-bound conversational row on `slack_convo_client_reply_armed(identity)` alone
(outbox.py:429 -> :141-144). `SLACK_CONVO_ECHO_CLIENT_REPLY=true` has been live on the
Railway `echo` service since 2026-09-05 (D51). So **one boolean was the whole distance
between "nothing has ever been sent" and real autonomous messages to paying gym
owners.**

Three settings now, and no single one of them changes what reaches a client:

* `AGENT_CLIENT_DM_AUTOFIX` -- the lane runs. On its own: measures, may run the scoped
  fix, cards a human. Composes no client reply at all.
* `AGENT_CLIENT_DM_CLIENT_REPLY` -- may compose one. On its own (master off): nothing.
* `AGENT_CLIENT_DM_LIVE_ACK` -- must equal a token **derived at runtime**, and the token
  contains the CURRENT value of `SLACK_CONVO_<IDENTITY>_CLIENT_REPLY`.

The third is what makes this structural rather than procedural: the required token
cannot be written before reading what the other flag says, and it goes stale the moment
either flag moves, dropping the lane back to escalate-only until a human re-affirms.
The refusal names, in one line, exactly what was about to go live. All seven proper
subsets of the three settings are asserted to be unable to reply, with
`SLACK_CONVO_ECHO_CLIENT_REPLY` ON -- i.e. against the live production state.

Nothing in `agent/slack_convo/*` is touched and no `SLACK_CONVO_*` flag's semantics
change. The #fixer bus's own auto-answer gate (D67) remains locked and is untouched; a
test asserts this package never names `SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE`,
never writes an environment variable, and never writes a `KIND_ANSWER` row.

## D70 (2026-09-07) -- an ask is spent once the client has answered it

D69's rebuilt lane is sound and this does not change its design. It closes one gap that
only a live case could show, and the way it was found is the point.

**The measurement.** `lane.decide()` was run against John Weeks' REAL brand bible --
read byte-for-byte off the production volume at
`/data/brand_voice/toughtemple52040e/lasso_voice.md` -- and his REAL latest message:

> "Yes let's include a call to action to book a free intro class, similar to what our
> paid ads are doing"

It returned `Outcome.REPLY` and composed *"What would you like your posts to ask people
to do?"* -- the question Echo had already asked him, and that this message IS the answer
to.

**Every control passed, and each was right to.** `cta_pool_count` is genuinely 0 (his
CTA rotation section is still the intake TODO -- so is Dean's, and so is every gym
onboarded through `write_brand_docs`, because `social_intake_reader._build_intake_text`
emits only sections 1-4 while `bible_drafter.draft_bible` renders CTAs from `_sec(s, 7)`;
that is a separate, fleet-wide bug and it is Blake's, not this lane's). `COND_CTA_EMPTY`
genuinely applies. The reply is faithfully grounded in a measured reading. Nothing in the
grounding discipline is capable of noticing the problem, because the problem is not in
the grounding.

**The gap.** `_already_handled` asks *"has THIS LANE written on this ticket"*. Echo's
ORDINARY reply path is what asked John. So a lane that had never itself spoken on that
thread saw a fresh ticket and planned the same question again.

**The fix, and the shape it deliberately is not.** The tempting version reads his words
and notices they are an answer. That is classifying the QUESTION over an open input
space -- D67's failure exactly, and the reason this package exists. The closed version is
a fact about the CONVERSATION, which this codebase owns and can count: *Echo has spoken
on this thread and the client has written since.* Phrasing-independent, cheap, and it
fails closed (escalate).

Scoped to ACTION-LESS conditions on purpose. A condition with an action CHANGES
something; running a gym's Drive sync is still right after Echo has talked to them. Only
the ask/tell-only outcomes -- `cta_needed`, `drive_reshare` -- have nothing left to say
once the client has responded. Over-refusing the rest would quietly turn the capability
off, which is its own failure.

**What was checked and deliberately NOT built.** Dean (CrossFit Reverb, ticket
`4941e162`, still `hold`) is the one live actionable row the poll matches today. Echo has
already answered him twice, and he has not written since -- so this rule does not fire
for him. It does not need to: `route()` returns None for his message and the lane
escalates. A second rule for "Echo already answered this exact message" is reasoned about
and not built, because no case demonstrates it; it is recorded here rather than left as a
silent gap.

**AND THE PREDICATE ITSELF WAS GUESSED, TWICE, WHICH IS THE PART WORTH READING.** The
first version counted any outbound whose `author_type` was not `"staff"`, and recognised
inbound authors from the list `("client", "user", "human", "")`. Both were drawn from
what seemed reasonable rather than from the code, and both were wrong in a different
direction:

* **Over-refusal, caught by live data.** `support_messages` on prod holds
  `author_type='system'` rows for `escalation`, `hold_notice` and `fixer_request` --
  INTERNAL cards delivered to the fixer channel, which the gym owner never sees. Several
  carry `attachments.recipient_kind='client'`, so that field is no discriminator either.
  Counting one as "Echo spoke" would have suppressed a legitimate FIRST ask on any ticket
  that had merely been escalated internally -- which, on live data, is most of them.
* **Under-refusal, caught by an adversarial audit.** `("client","user","human","")`
  contains two values production never writes and omits one it does.
  `adapter.author_type_for`'s whole range is `{staff, coach, client}` (adapter.py:550):
  a gym OWNER resolves to CLIENT, a gym's COACH to COACH, and STAFF is Blake and the
  LASSO team. A **coach** answering Echo's question left the predicate blind, so the
  original re-ask bug survived untouched on every coach-authored thread.

Both are now read from the producing modules. Client-visibility is
`adapter.CONVERSATIONAL_KINDS` ({ack, answer, template, status}) -- tested as an
ALLOWLIST, not as "not internal", because adapter.py's own comment records that the
PORTAL decides visibility by a denylist and a new internal kind is client-visible by
default over there. Delivery is required too: a `held` or `ready` row reached nobody.
The gym side is "not `identity_gate.STAFF`", written that way deliberately: an author
type nobody anticipated counts as the gym having spoken, which escalates to a human,
and the opposite default would re-ask a paying client.

**The same guessed list lived TWICE in this file**, and `_newest_client_message` had it
too -- so a thread whose newest message came from a coach skipped it and fell through to
`ticket.raw_text`, the ORIGINAL message. That is the "decided on the client's oldest
words" defect that function's own docstring says was fixed, arriving through the author
filter instead of through the ordering, and it takes the hard-line belt with it: a coach
writing *"actually, just double our ad budget"* would never have been read. Fixed in both
places, because correcting one copy of a duplicated wrong list and leaving the other is
the two-implementations failure this package exists to stop.

That correction is the lesson this log has now recorded a dozen times, and it arrived
again inside the fix for it: **when a predicate needs to know what another module writes,
read that module. The plausible answer and the true one differ often enough that guessing
is not a shortcut.**

**Evidence.** 13 mutations on the new rule, `__pycache__` cleared between each, all 7 turn
the suite red -- including the one that hard-codes the argument at the single call site
joining the helper to the decision, which is the "built but not wired" shape and was
caught by mutation rather than by review. Full suite green. Nothing armed.

## D71 (2026-09-07) -- three real gaps from an adversarial audit of D69, closed

Blake, directly: "I thought we built out that the agent could read the message, submit
to support themselves, or diagnose the issue and reply." He was right that it did not
work, and confirmed it from his own experience with Chad Edwards's and John Weeks's real
messages. This entry closes that (GAP 1), plus two audit findings (GAP 2, GAP 3) and four
minor items. Nothing is armed by this entry; `AGENT_CLIENT_DM_AUTOFIX` still defaults OFF.

### GAP 1 -- the lane was scoped to ONE identity's tickets, out of four armed ones

`run_once()`'s `identity` defaulted to `echo`, its `product` defaulted to the fixed
constant `"echo"`, and `agent/runner.py`'s only production caller invoked it with **no
arguments at all**. But `identities.py` has FOUR armed identities (echo, ranger, scout,
wrangler -- all `SLACK_CONVO_*_ENABLED=true` on Railway, verified live 2026-09-07) whose
Bolt Apps each independently create `support_tickets` rows via
`bus.get_or_create_ticket(product=identity.product, ...)`, correctly stamping
source/reporter/client_id/identity_kind every time. Real clients' Slack group DMs are
established practice to route through the SCOUT identity (Blake + owner + Scout bot), not
Echo. So this lane's poll asked `product='echo'` forever, while real client tickets landed
under `product='scout'` (or ranger, or wrangler) and were never once asked for -- a fourth
instance of D68's "built but not wired" pattern, this time at the CALLER, not the callee.

Read-only against production (2026-09-07) confirms this was not hypothetical: zero
`support_tickets` rows of any product other than `echo` carry a real (`is_test` is not
true) `client_id` + `slack_channel_id` today, and Chad Edwards has NO `support_tickets`
row at all, under any product, ever.

**Closed**: `run_once()`'s `product` now defaults to the passed identity's OWN
`.product` (never the fixed constant), and a new `run_once_all_identities()` -- the
function `agent/runner.py` now calls instead -- loops every client-carrying identity
(echo, ranger, scout, wrangler; lainey excluded, no Slack surface) against one shared
bus connection. This is safe by construction: each identity's own arming, own outbox
loop and own bot token are untouched (`outbox._dispatch_one` already scopes delivery to
`ticket.bot_identity == the posting identity`), so looping here only widens which
tickets get ASKED for, not what any identity may do with them.

`tests/test_client_dm_end_to_end.py` is the strongest proof: it builds a raw Slack
`message` event shaped exactly like a real group-DM message (mpim, John Weeks's real
words), runs it through the REAL `adapter.handle_event()` with NO ticket inserted by
hand, and asserts the ticket the adapter itself produces (`product='scout'`,
`source='slack_conversation'`, `status='hold'`) is invisible to the OLD call shape
(`run_once()` with no arguments) and visible to `run_once_all_identities()`.

**What this does NOT close, honestly.** If a real client's message never reaches
`adapter.handle_event()` at all -- the Slack app's event subscription not covering that
channel type, or the relevant bot not being a member of that specific group DM -- no
code change in this repo can see it; that is a Slack-app-configuration question outside
this codebase, and `ConvoWiring.health_line()`'s per-channel-type event counts (D-something,
listener_wiring.py) are the existing instrument for checking it. This fix closes the
gap that was provably real and in this repo's own control; it does not rule out a
second, Slack-side contributing cause, and Blake should check bot channel membership
for Scout/Echo in the actual gym group DMs as defense in depth.

### GAP 2 -- re-examined: the described write-time escape does not reproduce; a real,
### narrower dispatch-time gap did, and is now closed

The audit's literal claim (arming.py's docstring, pre-fix) was that with
`AGENT_CLIENT_DM_AUTOFIX=true`, `AGENT_CLIENT_DM_CLIENT_REPLY=true` and the ack token
wrong/stale, the lane still WRITES a `ready` client reply row. Traced through
`lane._decide()` step by step and reproduced with a script: this is **not what the
code does**. `may_reply=arm.may_reply_to_clients` is `False` whenever the ack does not
match, and `_decide()` returns an ESCALATE at "if not may_reply" BEFORE `conditions.
compose()` is ever called -- no reply row is written. `arming.py`'s own docstring
overclaimed in one respect (see below) but the write-time gate itself holds; this
audit re-attempt could not break it.

**The real, adjacent gap**: a row written to `ready` DURING a genuine LIVE window
persists in `support_messages` after that window ends -- `AGENT_CLIENT_DM_AUTOFIX`
revoked, or the derived `AGENT_CLIENT_DM_LIVE_ACK` gone stale. `outbox._dispatch_one`'s
own release gate, `_recipient_armed` (outbox.py:141-144), reads only
`SLACK_CONVO_<IDENTITY>_CLIENT_REPLY` -- a different, pre-existing, already-armed flag
(`true` on Railway since D51) with zero awareness this lane, or its revocation, exists.
So a revoke stops NEW writes but does nothing to rows already queued.

**Closed**: `outbox._dispatch_one` now re-runs `client_dm_support.arming.preflight()`
at DISPATCH time for any row carrying this lane's own provenance marker
(`attachments.client_dm_lane`), holding it (with a card) the moment that lane's own
arming no longer says LIVE -- independent of, and in addition to, the existing
`_recipient_armed` check. `arming.py`'s module docstring is corrected to state the real
guarantee (write-time only puts nothing new on the wire; the new dispatch-time check is
what makes a revoke retract what was already queued) rather than the broader claim it
made before. Two tests in `tests/test_slack_convo.py` cover both directions: held when
the lane's own arming has lapsed with `SLACK_CONVO_ECHO_CLIENT_REPLY` unchanged and ON
the whole time (the exact "one boolean" framing), and posted normally when the lane is
genuinely live.

### GAP 3 -- the Drive-revoked notice bypassed every gate; closed before any gym is exposed

`jobs/sync_gym_media.py`'s Drive-revoked notice, new-media digest, and sort-queue
digest all called `_post_digest(msg, channel=_coach_channel(gym_id))` -- a raw
`SlackPoster._chat_post` straight to the gym's own coach Slack channel, entirely
outside `conditions.compose()`, the outbox, and the three-flag interlock. Reachable via
the `gym_drive_sync` action (itself gated on `AGENT_CLIENT_DM_AUTOFIX`) AND,
independently, via the nightly `gym_drive_connect` cron with NO client_dm_support flag
involved at all. No gym has `slack_channel` configured yet (confirmed against
production), so this had only ever reached `#ops` in practice -- closed now, before one
does, not after.

**Closed**: a new `_client_channel_if_armed(gym_id)` gate requires
`config.slack_convo_client_reply_armed('echo')` -- the SAME flag every other
client-facing send in this repo already requires -- before returning the gym's own
channel; unarmed, it returns `''` and the existing `_post_digest` fallback to `#ops`
applies unchanged. This is not a full `compose()`/outbox migration (the notice is a
fixed factual string, not a Reading-assembled claim, and building a ticket-based outbox
path for a background-job notification is a larger, separate change); it closes the
"reachable on no gate at all" bypass to the same bar every other client-facing string in
this system already clears.

### MINOR ITEMS

* **`ASKS["cta_needed"]` re-asked a question the client had already answered.**
  SUPERSEDED, not fixed here: a phrase-matching version of this fix was built and
  then removed after D70 above (PR #73, merged to `main` while this fix was in
  flight) turned out to already close the SAME live case John Weeks reported, via
  the right axis -- a fact about the CONVERSATION (`_echo_already_asked`), not
  about the client's WORDS. D70's own test
  (`test_the_identical_message_on_a_fresh_thread_still_gets_the_ask`) is explicit
  that a phrase-matching version is WRONG: the ask must still fire for John's exact
  words on a thread Echo has never spoken on. The phrase-matching draft here broke
  precisely that test, which is the correct outcome for that test to have and the
  reason the draft was removed rather than kept alongside D70's fix.
* **`ASKS["drive_reshare"]` asserted an unmeasured outcome as fact** ("will let the
  sync resume"). Reworded as a genuine question per this module's own rule that an
  ask must make no claim: "Would you be able to re-share that folder with Echo in
  your portal? Once it is shared again, we can check whether the sync picks it back
  up."
* **`ESCALATE_ALWAYS` missed four plain ad-money phrasings** the auditor tried ("put
  more money behind the tuesday one", "scale us up to $50 a day", "promote that post
  to more people", "stop showing our ads to men over 60"), each producing an
  on-topic-but-wrong reply about photos instead of an escalation. Added seven terms
  (`more money`, `scale up`, `scale us up`, `promote`, `our ads`, `show our ads`,
  `showing our ads`) -- not to enumerate every future phrasing (see `BELT_MISSES` in
  `tests/test_client_dm_lane.py` for why that race is deliberately never run to
  completion on the ROUTER), but because these are ordinary phrasings on Blake's
  OWN always-escalate belt, which nothing in this capability's safety rests on
  completing (see `no_ad_rail.py`) but which is worth widening anyway.
* **"2 of 17 connected gyms" was wrong** in `probes.py` and this file: 17 was a
  `media_source` ROW count, not a distinct-gym count. The real number is 2 of 12
  DISTINCT gyms. The underlying fix (measuring `drive_identity_split` and refusing to
  speak on it) was already correct either way; only the prose was wrong.

Mutation-checked, `__pycache__` cleared between each: reverting the `run_once()`
product default, the runner's caller, the outbox dispatch-time recheck, or the
`ESCALATE_ALWAYS` additions each turns the corresponding test red, including
`test_client_dm_end_to_end.py`'s full real-adapter path.

## D72 (2026-09-11) -- no silent holds; the FIXER gets hands; Dean's false hard line

**Blake's ruling (2026-09-11), verbatim, supersedes the 2026-09-05 "hold for Blake's tap"
language in D54 / adapter.py:** "Any message someone sends Echo, Ranger, Wrangler, or Scout
is addressed, and if it needs a fix it goes to Claude Code (the FIXER) -- autonomous,
without me." No client message may terminate in a silent hold that waits on Blake. The
only things that may still refuse to auto-commit are the org floor: billing/price/refunds,
injury/liability, the GYM's real hours or class schedule (a real-world commitment), and
pixel/CAPI/ad-budget/targeting changes.

### What was actually holding Dean (ticket 27728832, CrossFit Reverb)

Reproduced against the stored rows (SELECT only). The FIXER drafted a correct answer to
"is there a way to keep the caption but switch out the picture" and the outbox held it as
"hard line (billing, hours or schedule, injury or liability)". Neither the question nor the
answer touched any of those. The two rules that fired were:

* `MAX_AUTO_ANSWER_WORDS = 25` -- Dean's question is 35 words (two plain sentences).
* `_ANSWER_COMMITS` on "let us know which one and **we will** take a look" -- a promise of
  human ATTENTION read as a promise of ACTION.

The only other held answer row of the last 30 days (eb3be7d8, CrossFit Zanshin, Pete
Mongeau) held on the same two rules (44 words; "I'll flag this for someone on the team").
Both wore the hard-line label because outbox._dispatch_one collapsed every
`may_auto_answer` failure into one string. The label was the lie, and it is gone.

### What changed

1. **The verdict has a tier** (`adapter.auto_answer_verdict` -> `AnswerVerdict(ok, tier,
   rule, detail, topic)`; `may_auto_answer` is now `.ok`). `org_floor` = the topic denylist
   on question or answer (split by topic so the client and the card can name it; the
   ad-settings leg is new), or a first-person commitment whose SENTENCE names a real-world
   object (`commitment_is_real_world`). `needs_review` = the structural checks on an
   ECHO-drafted answer (not about state, publish request, third party, advice, >60 words,
   a content-action promise). A FIXER-authored row (`attachments.fixer`) is checked against
   the org floor ONLY -- the FIXER is the reviewer the structural checks stood in for, and
   re-applying them would bounce the same ticket between the two systems forever.
2. **Attention is not a promise.** `_NOT_A_PROMISE` gained take a look / flag / follow up /
   get back / escalate / loop in / dig into / pass it along. A promise of human follow-up
   in a SENT answer (`promises_human_follow_up`) escalates the ticket to the FIXER
   (hold + escalated + classification NULL) so the sentence becomes true by mechanism.
3. **The word cap is 60**, not 25. The third-party and advice-shape guards catch a second
   subject; length only catches a wall of text.
4. **One disposition for every hold** (`adapter.hold_answer_for_team`), shared by the
   Slack adapter (draft time), the portal bridge (`echo_ticket_worker`, draft time) and the
   outbox (post time): (a) the #fixer card, headed "HELD REPLY: needs a teammate" (never
   "awaiting your tap"), WHY naming the rule and match; (b) the client's honest line as a
   `template` row that posts with no tap -- for a floor: "I can't confirm <billing or
   pricing | injury or liability | your gym's hours or class schedule | ad budget,
   targeting or tracking> details myself, so I have not answered that part; a LASSO
   teammate will follow up here today"; otherwise "I have not sent you an answer on this
   one myself; it is with the LASSO team now and someone will follow up here" -- once per
   ticket per tier; (c) the ticket open, `escalated=true`, `hold_tier='routine'` (the
   CHECK constraint allows only routine|framework, and framework means Blake-only, which
   is exactly what this must not be), the detail in `verification_after.hold = {tier,
   rule, detail, topic, fixer_authored, at}`, and for `needs_review` on an Echo draft
   `classification=NULL` so the FIXER's poll (hold + escalated + classification.is.null)
   takes it. The AUTO_ANSWER-off hold (`auto_answer_unarmed`) tells the client too.
5. **The FIXER's ops-action lane** (`agent/fixer_ops.py`, mounted on `echo-intake-web`
   via intake_web and INSIDE the worker via connect_web, plus `python -m agent ops-action`):
   `POST /ops/actions/<action>` with `X-Fixer-Ops-Secret` == env `FIXER_OPS_SECRET`
   (constant-time; 401 wrong, 503 unset), body `{gym_key, ticket_id, args}`. Catalog:
   resend_connect_link, reset_recreate_budget, release_denied_assets, swap_media(row_id),
   requeue_failed_row(row_id), restage_month(days<=31, background job; `GET
   /ops/actions/jobs/<id>`). Billing/Stripe, pixel/CAPI, ad budget, targeting and deleting
   published posts are 403 `org_floor` by name. Every call writes a system row on the
   ticket ("OPS ACTION <name> by fixer: ...", kind escalation so the portal hides it,
   delivery_status null so nothing posts it) and one AUDIT log line. Actions that need the
   worker's /data volume (release_denied_assets, restage_month) answer 503
   `volume_unavailable` on the web service and say to call the worker.

### Still Blake's, reported not decided

* `SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE` is NOT set on the live `echo` service, so
  `config.slack_convo_auto_answer_armed` returns False and every client answer -- Echo's
  and the FIXER's -- still holds at post time (now with the client told and a team card,
  never silently). D67's lock encodes the finding this entry's tiers address; lifting it is
  one env var and Blake's call.
* `FIXER_OPS_SECRET` exists on `echo-intake-web` but not on `echo`; the worker mount (the
  one with the volume) authenticates nothing until it is set there too.
* A new `hold_tier` CHECK value ('org_floor', 'needs_review') is a portal migration in the
  other repo; until then the column says 'routine' and the JSON says which.

Tests: `tests/test_no_silent_holds.py` (the two real held rows as fixtures, eight genuine
floors, all three paths), `tests/test_fixer_ops.py` (auth, catalog, every action, org floor,
volume preflight, jobs, the intake_web mount).
