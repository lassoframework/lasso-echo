"""
client_dm_support — an autonomous support lane for a client's own Slack message,
gated on WHAT THE ANSWER IS DERIVED FROM rather than on what the question is about.

Read docs/slack_convo/DECISIONS.md D67 and D68 first, then readings.py's docstring.

The whole capability in five sentences:

  * A Reading is one object holding a measurement, the production function that
    produces it, and the ONLY sentence Echo may say about it. There is nowhere else in
    this package to type a client-facing sentence.
  * A Condition is one object holding a predicate over those readings, at most one
    action from a closed set of ONE, the comparator that proves the action worked, and
    the ordered readings it reports.
  * A reply is the byte-identical reconstruction of those readings' registered
    sentences over the values measured AFTER the action. Anything else refuses.
  * Everything that does not match exactly one condition escalates to a human, and
    every reply also cards a human, because a reply is not a claim that the message
    was handled.
  * Nothing reaches a client without THREE deliberate settings, one of which is a
    token derived from the live value of another flag (arming.py).

AGENT_CLIENT_DM_AUTOFIX defaults OFF. Nothing here is armed.
"""
from __future__ import annotations

from . import arming, conditions, lane, no_ad_rail, probes, readings  # noqa: F401

__all__ = ["arming", "conditions", "lane", "no_ad_rail", "probes", "readings",
           "run_once"]


def run_once(**kw):
    return lane.run_once(**kw)
