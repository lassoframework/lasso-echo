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
