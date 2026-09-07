"""
client_dm_support — the direct-client-DM support lane.

A gym owner writes in their own group DM (their bot + Blake + them). Echo routes
that ticket to an ENUMERATED diagnostic, executes a fix that is scoped to that one
gym's own data, re-runs the SAME diagnostic to prove the fix landed, and only then
composes a reply that is byte-identical to a registered template rendered from the
verified facts. Anything that does not fit that shape escalates to a human.

WHY THIS SHAPE (D67 / D68, docs/slack_convo/DECISIONS.md).
The #fixer bus lost nine audit rounds trying to classify the QUESTION -- "is this
message safe to answer unattended". Every attempt was an ENUMERATION OF AN OPEN SET:
the input was any sentence a human might type, the rule was a finite list, and novel
phrasing therefore defaulted to ALLOW. D68's conclusion is to gate on what the ANSWER
is DERIVED FROM instead: auto-send only when the reply restates an enumerated set of
fact keys from the grounding snapshot and contains nothing else. This package is that
design, built for this capability only. Its input is CLOSED (the fact keys
facts.ALL_FACT_KEYS defines) and anything outside it defaults to REFUSE.

This is a SEPARATE capability from the #fixer bus's auto-answer gate. It does not
read, write, re-enable or modify any SLACK_CONVO_* flag, and it does not modify
anything under agent/slack_convo/ -- it only CONSUMES tickets that the unmodified
adapter there already wrote, and it reuses that lane's existing held-card escalation
sink rather than inventing a second one.

The whole package is dark behind config.client_dm_autofix_enabled()
(AGENT_CLIENT_DM_AUTOFIX, default OFF).

Module map:
  facts.py        the closed set of fact keys + the GroundingSnapshot type
  ad_block.py     the structural never-zone for ad money (no import, no call path)
  scope_gate.py   the foundation-trigger check, as real code
  diagnostics.py  read-only, per-gym, typed-fact diagnostic queries
  remedies.py     plan + execute a remedy; only client-scoped ones can execute
  verify.py       re-run the SAME diagnostic and prove the fact moved
  reply.py        the grounded-reply gate (byte-identical template render, or refuse)
  flow.py         the end-to-end decision for one ticket
  consumer.py     the trigger surface: flag-gated, reads tickets, never posts itself
"""
