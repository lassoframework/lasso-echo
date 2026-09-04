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
32/32 green. Full suite green (see commit for exact count). outreach.py remains
unwired to any production caller -- D42's open ruling should be resolved before it is.
