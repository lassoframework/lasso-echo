"""
podcast_caption.py — ground a podcast clip's caption in the episode's REAL
show-notes Doc (PODCAST_LIBRARY_BUILD_SPEC.md Wave 4). The caption comes from
the actual episode, never invention — that is the whole point.

HARD RAIL: notes Doc missing or exports empty -> the slot does NOT stage (the
builder returns None and fires ONE deduped alert). Echo does not write a
caption about an episode it cannot read.

B2B caption rules enforced here:
  * hook first line, short lines
  * 150-500 chars total
  * exactly ONE ask (the closing 'link in our bio' line; every extracted claim
    that itself contains an ask phrase is dropped so a second family can never
    sneak in)
  * ZERO banned dashes: everything passes copy_gate.scrub and the final text
    must clear copy_gate.violations, or the draft is refused (None)
  * guest @handles are tagged ONLY when the handle appears IN THE DOC and is on
    the gym's mentions allowlist (agent.mentions.allowlisted_handles). No
    guessed handles, ever.

Category is 'podcast' — subject to the existing <=25%-of-a-month cap
(calendar_grade content_mix + real_month_planner._remediate); nothing here
weakens that.
"""
from __future__ import annotations

import re

from . import copy_gate

MIN_LEN = 150
MAX_LEN = 500
MAX_CLAIMS = 5
MIN_CLAIMS = 3

# The single closing ask (family: link_in_bio per publish_guard.ask_families).
ASK_LINE = "Full episode is in the link in our bio."

_HANDLE_RE = re.compile(r"@([A-Za-z0-9_.]{2,30})")
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_GUEST_RE = re.compile(r"(?im)^\s*guest\s*[:\-]\s*(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*••]|\d{1,2}[.)])\s+")
_EP_TITLE_RE = re.compile(
    r"(?i)^(?:gmms|ep(?:isode)?)?[\s#:.\-]*?(\d{2,3})\b[\s:.\-]*(.*)$")


def parse_notes(text):
    """Extract {episode, title, guest, claims, handles} from the notes Doc's
    plain-text export. Pure heuristics over the REAL doc text: nothing here is
    generated or guessed; a field the doc does not carry stays None/empty."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    out = {"episode": None, "title": "", "guest": None, "claims": [],
           "handles": []}
    if not lines:
        return out

    # Title + episode: the first line usually reads like 'GMMS 140: <title>' or
    # 'Episode 140 - <title>'. The episode capture is informational only — the
    # asset's episode (from the folder) is authoritative.
    first = lines[0]
    m = _EP_TITLE_RE.match(first)
    if m:
        out["episode"] = int(m.group(1))
        out["title"] = (m.group(2) or "").strip() or first
    else:
        out["title"] = first

    gm = _GUEST_RE.search(str(text or ""))
    if gm:
        out["guest"] = gm.group(1).strip()

    out["handles"] = sorted({h.lower() for h in _HANDLE_RE.findall(str(text or ""))})

    # Concrete claims: substantive body lines. Bullets and plain sentences both
    # qualify; headers ('...:'), timestamps, URLs-only lines, and lines carrying
    # their own ask phrase are dropped (one-ask rule).
    for ln in lines[1:]:
        body = _BULLET_RE.sub("", ln).strip()
        body = _TIMESTAMP_RE.sub("", body).strip(" -–—:;,")
        if len(body) < 25 or len(body) > 220:
            continue
        if body.endswith(":"):
            continue
        if body.lower().startswith(("http://", "https://", "www.")):
            continue
        if copy_gate.ASK_RE.search(body):
            continue  # a claim may never smuggle in a second ask family
        if body.startswith(("@", "#")):
            continue
        out["claims"].append(copy_gate.scrub(body))
        if len(out["claims"]) >= MAX_CLAIMS:
            break
    return out


def _guest_mention(parsed, gym_id, allowlist_fn=None):
    """The guest's @handle line, ONLY when a handle from the DOC is on the gym's
    mentions allowlist. Missing allowlist / lookup failure -> no tag (fail
    closed: an unverifiable handle is not a handle)."""
    handles = parsed.get("handles") or []
    if not handles:
        return "", ""
    try:
        if allowlist_fn is None:
            from .mentions import allowlisted_handles as allowlist_fn
        allowed = {str(h or "").strip().lstrip("@").lower()
                   for h in (allowlist_fn(gym_id) or [])}
    except Exception:
        return "", ""
    for h in handles:
        if h.lower() in allowed:
            return f"With @{h}.", h
    return "", ""


def draft_caption(episode, notes_text, *, gym_id="lasso", allowlist_fn=None):
    """A grounded caption for one clip of `episode`, or None when the doc cannot
    support one (empty export, too little concrete material, or a copy_gate
    failure). Returns (caption, meta) — meta carries what grounded it (title,
    claims used, tagged handle) for source_fragments/audit.

    Never fabricates: every line is the doc's own text (scrubbed), the episode
    number, or the one fixed ask."""
    if not str(notes_text or "").strip():
        return None, {"reason": "notes_empty"}
    parsed = parse_notes(notes_text)
    claims = parsed["claims"]
    if not claims:
        return None, {"reason": "no_concrete_claims"}

    hook = copy_gate.scrub(parsed["title"]).strip().rstrip(".") if parsed["title"] else ""
    if not hook or hook.startswith(("#", "@")):
        hook = claims[0].rstrip(".")

    guest_line, tagged = _guest_mention(parsed, gym_id, allowlist_fn=allowlist_fn)

    lines = [hook if hook.endswith((".", "!", "?")) else hook + "."]
    used_claims = []
    for c in claims:
        if c.rstrip(".") == hook.rstrip("."):
            continue  # the hook already says it
        used_claims.append(c)
    body_budget = used_claims[:MAX_CLAIMS]

    def _assemble(n_claims):
        parts = list(lines)
        for c in body_budget[:n_claims]:
            parts.append(c if c.endswith((".", "!", "?")) else c + ".")
        if guest_line:
            parts.append(guest_line)
        parts.append(f"All of it is in episode {int(episode)}.")
        parts.append(ASK_LINE)
        return "\n".join(parts)

    # Grow claims until the floor is met; shrink until the ceiling is met.
    max_n = len(body_budget)
    chosen = min(MIN_CLAIMS, max_n)
    caption = _assemble(chosen)
    while len(caption) < MIN_LEN and chosen < max_n:
        chosen += 1
        caption = _assemble(chosen)
    while len(caption) > MAX_LEN and chosen > 0:
        chosen -= 1
        caption = _assemble(chosen)

    caption = copy_gate.scrub(caption)
    if len(caption) < MIN_LEN or len(caption) > MAX_LEN:
        return None, {"reason": f"length_{len(caption)}_outside_{MIN_LEN}_{MAX_LEN}"}
    if copy_gate.violations(caption):
        return None, {"reason": "copy_gate_violation"}
    from .publish_guard import ask_families
    if len(ask_families(caption)) != 1:
        return None, {"reason": "ask_count_not_one"}

    return caption, {"title": parsed["title"], "claims": body_budget[:chosen],
                     "guest": parsed["guest"], "tagged_handle": tagged}
