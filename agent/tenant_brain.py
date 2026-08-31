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
    """Where tenant brain files live. An explicit base_dir always wins (tests pass a
    tmp dir). Otherwise the DURABLE per-gym brain root (config.tenant_brain_dir() ->
    <DATA_DIR>/brains on the deployed worker, so edits survive a redeploy; '.'/brains
    in local dev / tests where /data does not exist). Historically this returned the
    repo-relative 'brains', which on the worker was the ephemeral /app/brains and was
    wiped every deploy — the learning loop recorded to a dir that never persisted."""
    if base_dir:
        return base_dir
    try:
        return config.tenant_brain_dir()
    except Exception:  # noqa: BLE001 - config must never break a brain read/write
        return "brains"


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


def seed_from_intake(tenant_key, sections, base_dir=None):
    """Seed a NEW tenant's brain with the caption style rules its own intake stated.

    WHY (Blake, 2026-08-31): a gym's brain started empty and only filled as humans
    edited its posts, so its first weeks of captions ignored the voice preferences it
    had just written down. Echo drafts the brain from intake like it drafts the bible,
    so day one already reflects the gym's own stated style.

    STYLE ONLY, NEVER FACTS. This module's contract is that the brain adds no claims,
    and that is preserved exactly: the only fields read are the voice ones (vibe, the
    words-to-never-use list, content goal, hashtags), which are instructions about HOW
    to write, not assertions about the gym. Offers, pricing and proof are deliberately
    NOT read here; those are facts and they belong in client_sources behind the
    fabrication gate.

    Idempotent: seeds only a brain with no seeded rules yet, so a re-run never stacks
    duplicates and never overwrites what the gym's real edits have taught since.
    Returns the number of rules written."""
    voice = (sections or {}).get("voice") or {}
    if not isinstance(voice, dict):
        return 0
    existing = set(style_rules(tenant_key, base_dir))
    rules = []
    vibe = str(voice.get("vibe") or "").strip()
    if vibe:
        rules.append(f"Write in the gym's own stated voice: {vibe}")
    never = str(voice.get("words_to_never_use") or "").strip()
    if never:
        words = ", ".join(w.strip() for w in never.replace(",", "\n").splitlines()
                          if w.strip())
        if words:
            rules.append(f"Never use these words, the gym asked for them to be "
                         f"avoided: {words}")
    goal = str(voice.get("content_goal") or "").strip()
    if goal:
        rules.append(f"The gym's stated content goal: {goal}")
    tags = str(voice.get("hashtags") or "").strip()
    if tags:
        rules.append(f"Preferred hashtags the gym gave: {tags}")
    written = 0
    for rule in rules:
        if rule in existing:
            continue
        if record_event(tenant_key, "edit_diff", base_dir=base_dir,
                        before="", after="", rule=rule):
            written += 1
    return written


def style_rules(tenant_key, base_dir=None):
    """Caption style rules the tenant's edits imply, most recent last."""
    return [e["rule"] for e in read_events(tenant_key, base_dir)
            if e["kind"] == "edit_diff" and e.get("rule")]


def deny_reasons(tenant_key, base_dir=None):
    return [e["reason"] for e in read_events(tenant_key, base_dir)
            if e["kind"] == "deny_reason" and e.get("reason")]


def edit_examples(tenant_key, base_dir=None, limit=5):
    """
    Recent (before, after) caption edit pairs this tenant's approver made, in
    order, capped to the most recent `limit`. This is the CORE learning signal:
    each pair shows how this gym likes a machine draft revised, so the drafter
    can move its next caption toward the approver's taste and get better every
    time an edit lands.

    FABRICATION-SAFE, same contract as prompt_notes: the `after` text (what the
    human approved) must pass the fabrication gate (rotation.is_gate_clean). A
    pair whose after-text carries a claim no approved source clears is SKIPPED,
    so an edit example can never smuggle an unverified number into a prompt.
    Empty while the flag is OFF.
    """
    if not config.tenant_brain_enabled():
        return []
    from . import rotation
    out = []
    for e in read_events(tenant_key, base_dir):
        if e["kind"] != "edit_diff":
            continue
        after = (e.get("after") or "").strip()
        before = (e.get("before") or "").strip()
        if not after:
            continue
        # BOTH sides must clear the fabrication gate: the after (human-approved
        # text) AND the before (a prior draft that could carry a legacy claim).
        # A pair where either side has an uncleared claim is dropped whole, so an
        # edit example can never surface a %, $N, or Nx the sources don't clear.
        if not rotation.is_gate_clean(after) or not rotation.is_gate_clean(before):
            print(f"[brain] {tenant_key}: an edit example carries an uncleared "
                  "claim and was SKIPPED from prompts (the gate stays the sole "
                  "authority on claims).")
            continue
        out.append((before, after))
    return out[-limit:] if limit else out


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
