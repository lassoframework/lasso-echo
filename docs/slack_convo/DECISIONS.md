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
