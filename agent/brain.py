"""
Nightly "brain" hook (later stage, defaults OFF).

The brain is LASSO's knowledge vault (e.g. an Obsidian folder of markdown). This
hook lets the agent get smarter every night by READING it. It does NOT let the
agent change who it is.

Hard line, set by the gates:
  - Gate 5 "Human owns voice": this hook may PROPOSE new angles, surface what
    worked / flopped, and suggest content ideas. It may NEVER auto-edit the
    approved brand voice doc.
  - Gate 3 "no fabrication": proposals are grounded in the brain + real
    performance data. The agent does not invent offers, claims, or facts.
  - Anything that would change what the agent says in public surfaces for a
    human tap, same as a post.

Wire `read_brain` to your vault path. Keep `BRAIN_ENABLED` OFF until ready.
"""

import os

from .stubs import NotImplementedYet


def brain_enabled():
    return str(os.environ.get("AGENT_BRAIN_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}


def read_brain(vault_path=None):
    """
    Later: read the LASSO Obsidian vault (read-only) and return notes/context the
    nightly proposal step can learn from. Read-only. Never writes to the vault.
    """
    raise NotImplementedYet("Brain read is a later stage. Point it at the vault path.")


def propose_nightly(account, brain_context, performance):
    """
    Later: combine brain context + yesterday's performance into PROPOSALS only:
      - new creative angles to try
      - what worked / what flopped
      - gaps to ask the client to fill
    Returns proposals to surface to the human. Writes nothing live. Never edits
    the approved voice doc.
    """
    raise NotImplementedYet("Nightly proposal step is a later stage.")
