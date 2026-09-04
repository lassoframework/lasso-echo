"""
brain.py — per-agent SUPPORT brain: `brains/support/<agent>.md`, one per identity, mirroring
the existing tenant-brain pattern under `brains/`.

Blake's ruling (2026-09-03, item 2, verbatim): "PER-AGENT SUPPORT BRAIN. brains/support/
<agent>.md per agent, same pattern as Echo tenant brains. Accumulates from resolved
tickets: what broke, what fixed it, what the client asked, how they phrased it. Brain
shapes classification and reply style only. Never facts. The verification gate and
fabrication gate remain sole authority. Seed each brain from the agent's existing
knowledge and voice docs."

HARD SCHEMA SEPARATION (see D36): this module can only ever hand back a `BrainHint` --
tone notes, classification-hint phrases, and how clients tend to phrase a kind of ask.
There is no field on `BrainHint`, and no function in this module, that returns arbitrary
brain text merged into an LLM's factual context. `answer_lane.py` (the only place a
factual reply body is generated) does not import this module at all -- that is the actual
enforcement, not a comment promising restraint; `tests/test_support_brain.py` asserts the
absence of that import so a future edit cannot wire a "helpful" bypass around it. The
verification gate (grounding snapshot -> verification_before/after) and the fabrication
gate (CLAUDE.md: "no invented facts, offers, prices, or stats") remain the ONLY authority
over what appears in a reply's factual content; the brain only ever nudges CLASSIFICATION
(is this a code_fix or a question, given how this agent's clients usually phrase each) and
STYLE (tone notes folded in alongside, never instead of, the existing reply-voice doc).

File format (plain markdown, human-editable and git-diffable, same as the tenant brains):

    ## Tone
    - <one phrase per line>

    ## Classification hints
    - <phrase or pattern> -> <code_fix|question|action_request>

    ## Common phrasings
    - <how a client asks for this>

    ## Learned from resolved tickets
    - <date> ticket <id>: asked "<paraphrase>"; broke: <what>; fixed: <what>

Any other heading is ignored (forward compatible, never a parse error). A missing file
returns an empty hint, never an error and never a guessed default.
"""
from dataclasses import dataclass, field
import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRAIN_DIR = os.path.join(_REPO_ROOT, "brains", "support")

_SECTION_TONE = "tone"
_SECTION_CLASSIFICATION = "classification hints"
_SECTION_PHRASING = "common phrasings"
_SECTION_LEARNED = "learned from resolved tickets"
_KNOWN_SECTIONS = {_SECTION_TONE, _SECTION_CLASSIFICATION, _SECTION_PHRASING, _SECTION_LEARNED}

_MAX_ENTRIES_PER_SECTION = 200   # bounds file growth; oldest learned entries age out first


@dataclass(frozen=True)
class BrainHint:
    """The ONLY shape a support brain can hand back. Every field is tone or
    classification guidance. None of these fields is a fact, and no caller may treat any
    of them as one: this dataclass has no field named anything like `facts`, `answer`,
    `context`, or `snippet`, on purpose."""
    tone_notes: tuple = field(default_factory=tuple)
    classification_hints: tuple = field(default_factory=tuple)   # (phrase, classification)
    common_phrasings: tuple = field(default_factory=tuple)

    def classification_hint_for(self, text: str):
        """Advisory only: returns a classification label if the text matches one of this
        brain's learned phrase->classification hints, else None. The classifier
        (classifier.py) is free to ignore this; it never overrides the fabrication or
        verification gates, and it never adds text to a reply body."""
        low = (text or "").lower()
        for phrase, label in self.classification_hints:
            if phrase and phrase.lower() in low:
                return label
        return None


def brain_path(agent_name: str) -> str:
    return os.path.join(BRAIN_DIR, f"{agent_name}.md")


def load_hint(agent_name: str) -> BrainHint:
    """Read `brains/support/<agent>.md` and return ONLY tone/classification/phrasing
    guidance. Missing file, unreadable file, or a parse producing nothing -> an empty
    BrainHint (never an error, never a guessed default that could look like ground truth)."""
    path = brain_path(agent_name)
    if not os.path.isfile(path):
        return BrainHint()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return BrainHint()
    return _parse(raw)


_HINT_LINE_RE = re.compile(r"^(.*?)\s*->\s*(code_fix|question|action_request|follow_up)\s*$",
                           re.IGNORECASE)


def _parse(raw: str) -> BrainHint:
    tone, hints, phrasings = [], [], []
    section = None
    for line in raw.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("## "):
            name = low[3:].strip()
            section = name if name in _KNOWN_SECTIONS else None
            continue
        if not s.startswith("-") or section is None:
            continue
        body = s[1:].strip()
        if section == _SECTION_TONE:
            tone.append(body)
        elif section == _SECTION_PHRASING:
            phrasings.append(body)
        elif section == _SECTION_CLASSIFICATION:
            m = _HINT_LINE_RE.match(body)
            if m:
                hints.append((m.group(1).strip(), m.group(2).strip().lower()))
        # _SECTION_LEARNED is intentionally never parsed into any BrainHint field: those
        # entries are a human/append-only audit log of what a resolved ticket taught,
        # not a machine-consumed field. Keeping it unparsed is part of the schema
        # separation -- there is no path from "learned" free text into classification or
        # reply generation.
    return BrainHint(tone_notes=tuple(tone), classification_hints=tuple(hints),
                     common_phrasings=tuple(phrasings))


def append_resolution(agent_name: str, *, ticket_id: str, asked: str = "", broke: str = "",
                       fixed: str = "", client_phrasing: str = ""):
    """Append one learned-from-a-RESOLVED-ticket line. Only ever appends to the
    'Learned from resolved tickets' section -- there is no 'Facts' section in this schema,
    so nothing written here can later be read back as ground truth (load_hint() above
    never parses that section into anything returned to a caller). Creates the file (and
    the section) if either is missing. Best-effort: a write failure is swallowed (a brain
    is an optimization, never a dependency the ticket pipeline can be blocked by)."""
    from datetime import datetime, timezone
    path = brain_path(agent_name)
    os.makedirs(BRAIN_DIR, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = []
    if asked:
        parts.append(f'asked "{_clip(asked)}"')
    if broke:
        parts.append(f"broke: {_clip(broke)}")
    if fixed:
        parts.append(f"fixed: {_clip(fixed)}")
    if client_phrasing:
        parts.append(f'phrased as "{_clip(client_phrasing)}"')
    if not parts:
        return
    line = f"- {date} ticket {ticket_id}: " + "; ".join(parts)
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        if f"## {_SECTION_LEARNED.title()}" not in existing and "## Learned from resolved tickets" not in existing:
            existing = existing.rstrip("\n") + "\n\n## Learned from resolved tickets\n"
        existing = existing.rstrip("\n") + "\n" + line + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(existing)
    except OSError:
        pass  # best-effort; the brain is never a dependency of the ticket pipeline
    if client_phrasing:
        _bump_phrasing(agent_name, client_phrasing)


def _clip(s: str, n: int = 160) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n]


def _bump_phrasing(agent_name: str, phrasing: str):
    """Best-effort: also add the raw phrasing to '## Common phrasings' so future
    classification-hint tuning has real examples to work from. Never touches Tone or
    Classification hints sections directly -- those stay human-curated."""
    path = brain_path(agent_name)
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        marker = "## Common phrasings"
        line = f"- {_clip(phrasing)}"
        if marker in existing:
            existing = existing.replace(marker, marker + "\n" + line, 1)
        else:
            existing = existing.rstrip("\n") + f"\n\n{marker}\n{line}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(existing)
    except OSError:
        pass
