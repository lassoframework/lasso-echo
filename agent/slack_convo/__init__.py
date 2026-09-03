"""
slack_convo — the Slack Conversational Adapter, a FIXER intake adapter.

Blake, 2026-09-03: "Confirmed: the FIXER bus (support_tickets + support_messages) IS the
framework. Do not build a parallel system. Build a Slack Conversational Adapter as a new
FIXER intake adapter. Echo first, generic enough that Ranger, Scout, Wrangler, and Lainey
plug in by config."

What this package is:
  A person talks to a bot in Slack the way they would talk to a colleague -- a DM, a group
  DM the bot is in, an @mention, or a reply in a thread the bot already owns -- and that
  conversation becomes a support_tickets row (THREAD EQUALS TICKET), every message becomes
  a support_messages row FIRST, and only then is anything mirrored back to Slack. The row
  is the record. Slack is the view.

The pieces, each pure and injectable so all of it is offline-testable:
  identities.py     the per-bot config registry (product, tokens, lanes, voice, flags)
  identity_gate.py  Slack user -> staff / client / coach / unknown. Never a guess.
  bus.py            the support_tickets / support_messages store (Supabase REST)
  classifier.py     question / code_fix / action_request / follow_up, deterministic first
  adapter.py        the intake: match -> gate -> dedupe -> ticket -> classify -> rows.
                    NEVER calls chat.postMessage. Writes rows only.
  answer_lane.py    grounded answers from live account state; refuses billing/pricing
  outbox.py         the Wrangler outbound role: posts ONLY ready rows, ONLY after the
                    verification gate, ONLY where a human spoke first
  listener_wiring.py registers the adapter on a Bolt App per identity, counts every
                    event type it sees so a missing Slack subscription is visible

Rails that hold everywhere in here:
  - Flags OFF = today, byte for byte. Nothing is recorded, nothing replies.
  - The bot never opens a conversation. It only answers where a person spoke first.
  - No outbound row is posted without the parent ticket carrying an inbound human row.
  - No substantive reply posts without verification_after on the parent ticket.
  - Client-facing replies hold behind the per-identity client-reply flag for a human tap.
  - Billing, pricing, Stripe: never answered, always escalated.
"""
