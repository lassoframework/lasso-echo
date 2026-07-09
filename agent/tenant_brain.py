"""
Per-gym tenant brain (Stage 2 Part 10): brains/<tenant>.md.

Dormant behind AGENT_TENANT_BRAIN_ENABLED (default OFF: nothing records,
nothing filters, prompts and rotation are untouched). Armed, portal learning
events append STRUCTURED entries to the tenant's own brain file:

    approve_streak   {"streak": N}                the tenant's cadence is landing
    edit_diff        {"before", "after", "rule"}  what the human changed and the
                                                  caption style rule it implies
    deny_reason      {"reason"}                   why a draft was denied
    kill             {"concept"}                  this concept never runs again
                                                  FOR THIS TENANT

Drafting reads the brain ALONGSIDE the voice doc, never instead of it:
  - killed_concepts(tenant): excluded from THAT tenant's rotation only
    (runway.classify_creatives consults it; other tenants never see the kill),
  - style_rules(tenant): caption style rules from edit diffs,
  - prompt_notes(tenant): style rules + deny reasons folded into prompts.

THE BRAIN NEVER ADDS FACTS. Every line prompt_notes returns is passed through
the fabrication gate exactly like a client note: a rule or reason carrying a
claim (a %, a $, a multiplier) that no approved source clears is SKIPPED, so a
brain entry can never smuggle an unverified claim into a caption. The gate
(rotation.is_gate_clean over approved sources) stays the sole authority on
claims; brain text is instructions about style and selection, nothing more.
"""

import json
import os
import re
from datetime import datetime, timezone

from . import config

EVENT_KINDS = ("approve_streak", "edit_diff", "deny_reason", "kill")

_ENTRY_RE = re.compile(r"^## (\S+) (\w+) (\{.*\})$")


def brains_dir(base_dir=None):
    return base_dir or "brains"


def brain_path(tenant_key, base_dir=None):
    return os.path.join(brains_dir(base_dir), f"{tenant_key}.md")


def record_event(tenant_key, kind, base_dir=None, **fields):
    """
    Append one structured learning event to the tenant's OWN brain file.
    Returns True, or False while the flag is OFF / the kind is unknown (loud
    print, never a silent typo). Append-only: the portal never rewrites history.
    """
    if not config.tenant_brain_enabled():
        return False
    if kind not in EVENT_KINDS:
        print(f"[brain] unknown event kind {kind!r} for {tenant_key}; refused "
              f"(known: {', '.join(EVENT_KINDS)})")
        return False
    os.makedirs(brains_dir(base_dir), exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    line = f"## {stamp} {kind} {json.dumps(fields, sort_keys=True)}\n"
    with open(brain_path(tenant_key, base_dir), "a", encoding="utf-8") as fh:
        fh.write(line)
    from . import db
    db.audit("tenant_brain", tenant_key, f"{kind} recorded")
    return True


def read_events(tenant_key, base_dir=None):
    """Every structured entry in the tenant's brain, in order. [] when the flag
    is OFF or the file is absent. Reads ONLY the named tenant's file."""
    if not config.tenant_brain_enabled():
        return []
    try:
        with open(brain_path(tenant_key, base_dir), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = _ENTRY_RE.match(line)
        if not m:
            continue
        try:
            fields = json.loads(m.group(3))
        except ValueError:
            continue
        out.append({"at": m.group(1), "kind": m.group(2), **fields})
    return out


def killed_concepts(tenant_key, base_dir=None):
    """Concept keys this tenant's approver killed: excluded from THIS tenant's
    rotation forever (other tenants are untouched). Empty while the flag is OFF."""
    return {e["concept"] for e in read_events(tenant_key, base_dir)
            if e["kind"] == "kill" and e.get("concept")}


def style_rules(tenant_key, base_dir=None):
    """Caption style rules the tenant's edits imply, most recent last."""
    return [e["rule"] for e in read_events(tenant_key, base_dir)
            if e["kind"] == "edit_diff" and e.get("rule")]


def deny_reasons(tenant_key, base_dir=None):
    return [e["reason"] for e in read_events(tenant_key, base_dir)
            if e["kind"] == "deny_reason" and e.get("reason")]


def prompt_notes(tenant_key, base_dir=None):
    """
    The brain lines drafting folds into prompts: style rules + deny reasons.
    EVERY line passes the fabrication gate first (rotation.is_gate_clean over
    the approved sources): a line carrying a claim no approved source clears is
    SKIPPED, so the brain can never introduce an unverified claim. The voice
    doc and the facts files stay the only sources of claims.
    """
    if not config.tenant_brain_enabled():
        return []
    from . import rotation
    notes = []
    for line in style_rules(tenant_key, base_dir) + deny_reasons(tenant_key, base_dir):
        if rotation.is_gate_clean(line):
            notes.append(line)
        else:
            print(f"[brain] {tenant_key}: a brain line carries an uncleared "
                  "claim and was SKIPPED from prompts (the gate stays the "
                  "sole authority on claims).")
    return notes
