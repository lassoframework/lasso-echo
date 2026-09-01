"""
story_overlay.py — the Roxx overlay standard for Story Studio (spec §1, extends
AGENT_STORY_FORMAT). Deterministic, no network, offline-safe.

The Roxx standard (@the_roxx_bhm, LASSO's best overlay client) as ENFORCED RULES:
  * ALL-CAPS heavy condensed. Overlay text is upper-cased at build.
  * Two-beat contrast hook: line 1 tension, line 2 payoff.
  * <= 8 words per line, <= 2 lines per frame. A third line goes to the NEXT frame
    (enforced + re-wrapped, never silently dropped).
  * Identity anchor: a city / brand token appears in the overlay.
  * Stat card = NAME + NUMBER + PLACE. Event card = WHAT + WHEN + one ask.
  * Brand scrim with ~4.5:1 contrast against the sampled backdrop, checked at render;
    a bare-text-on-busy-footage frame gets a scrim added (never bare text).
  * Story safe zones: nothing in the top 250px or bottom 310px of a 1080x1920 frame.
    Enforced (a violating box fails the render).

GROUNDING (spec §0): overlay copy comes from the client brief FIRST, else the vision
sidecar; low confidence -> a generic-safe overlay + a flag, never a fabricated claim.
Grounding, copy_gate (no dashes, on-image too), and the per-gym avatar rail are all
applied to the final overlay text here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import story_layout as _layout

# Frame geometry (spec §1). DEFERRED to story_layout.py (the single merged
# layout authority, 2026-09-01 ruling 3) so this module can never define a
# safe zone that drifts out of sync with what add_brand_frame actually draws.
FRAME_W = _layout.FRAME_W
FRAME_H = _layout.FRAME_H
SAFE_TOP = _layout.SAFE_TOP
# SAFE_BOTTOM is DERIVED from the brand bar's real footprint (story_layout.
# BRAND_BAR_H), not a hardcoded 310px band that can silently fall out of sync
# with what add_brand_frame actually draws — that drift was ruling 3's whole
# complaint. Kept as a module-level constant for callers that still read it.
SAFE_BOTTOM = FRAME_H - _layout.brand_bar_top(FRAME_H) + _layout.BOTTOM_MARGIN

MAX_WORDS_PER_LINE = 8
MAX_LINES_PER_FRAME = 2
# The character cap (ruling 1: overflow is caught by a cap at generation time,
# not shrink-to-fit at render time). Measured from REAL font metrics in
# story_layout.py — see that module for the full show-your-work derivation.
MAX_CHARS_PER_LINE = _layout.MAX_CHARS_PER_LINE

TARGET_CONTRAST = 4.5        # WCAG-style ratio the brand scrim must reach


# ---- wrapping + framing -----------------------------------------------------
def wrap_line(text, max_words=MAX_WORDS_PER_LINE, max_chars=None):
    """Break ONE overlay string into lines of at most `max_words` words each AND
    at most `max_chars` characters each (default: MAX_CHARS_PER_LINE, the REAL
    measured budget for the font ffmpeg's drawtext actually renders — see
    story_layout.py). Words are never split; a line is closed at the word
    boundary as soon as adding the next word would breach EITHER budget.

    Ruling 1a ("regenerate the line, don't truncate/shrink"): this module never
    invents new words (it is the no-fabrication rail — copy comes from the
    client brief or vision, verbatim/compressed, never authored here), so
    "regenerate" means re-derive the line GROUPING until every line clears the
    character budget — which is exactly what this greedy re-split does. A
    SINGLE token that alone still exceeds max_chars cannot be fixed by more
    splitting (no amount of re-wrapping shortens one word): that raises
    OverlayRejected, this module's established HOLD-honestly contract (the
    same one copy_gate / avatar-rail violations use), never truncated, never
    silently shipped over-width."""
    if max_chars is None:
        max_chars = MAX_CHARS_PER_LINE
    words = str(text or "").split()
    lines, cur = [], []
    for w in words:
        candidate = cur + [w]
        candidate_line = " ".join(candidate)
        if cur and (len(candidate) > max_words or len(candidate_line) > max_chars):
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur = candidate
    if cur:
        lines.append(" ".join(cur))
    for ln in lines:
        if len(ln) > max_chars:
            raise OverlayRejected(
                f"a single word {ln!r} is {len(ln)} chars, over the "
                f"{max_chars}-char safe-width budget (story_layout."
                f"MAX_CHARS_PER_LINE) and cannot be fixed by rewrapping; "
                f"overlay HELD rather than ship text that would clip off the "
                f"frame")
    return lines


def frame_lines(lines, max_lines=MAX_LINES_PER_FRAME):
    """Group already-wrapped lines into FRAMES of at most `max_lines` lines each. A
    third line rolls to the next frame (spec §1: <= 2 lines/frame, third -> next
    frame). Returns a list of frames, each a list of lines."""
    frames = []
    for i in range(0, len(lines), max_lines):
        frames.append(lines[i:i + max_lines])
    return frames


def layout_overlay(text, *, max_words=MAX_WORDS_PER_LINE, max_lines=MAX_LINES_PER_FRAME,
                   max_chars=None):
    """Full layout for one overlay block: wrap to <= max_words/line AND
    <= max_chars/line (the real character-width cap, ruling 1), then split into
    frames of <= max_lines lines. Upper-cases every line (the ALL-CAPS rule).
    Returns a list of frames (each a list of ALL-CAPS lines)."""
    wrapped = []
    for chunk in str(text or "").split("\n"):
        wrapped += wrap_line(chunk, max_words, max_chars) if chunk.strip() else []
    wrapped = [ln.upper() for ln in wrapped]
    return frame_lines(wrapped, max_lines)


def line_violations(lines, max_chars=None):
    """The list of Roxx rule violations for a set of lines on ONE frame (empty = ok).
    Used by tests + the render gate: a >8-word line, a >2-line frame, or a line
    over the character-width budget is a violation the layout must fix
    (layout_overlay re-wraps so these never reach the render)."""
    if max_chars is None:
        max_chars = MAX_CHARS_PER_LINE
    v = []
    if len(lines) > MAX_LINES_PER_FRAME:
        v.append(f"{len(lines)} lines on one frame (> {MAX_LINES_PER_FRAME})")
    for ln in lines:
        n = len(str(ln).split())
        if n > MAX_WORDS_PER_LINE:
            v.append(f"line has {n} words (> {MAX_WORDS_PER_LINE}): {ln!r}")
        if len(str(ln)) > max_chars:
            v.append(f"line has {len(str(ln))} chars (> {max_chars}): {ln!r}")
    return v


# ---- safe zones (deferred to story_layout.py, the merged layout authority) --
def safe_zone_ok(box, *, frame_h=FRAME_H):
    """True when a text box (y_top, y_bottom) in pixels sits inside the story safe
    zone. Delegates to story_layout.safe_zone_ok — see that module for why the
    bottom boundary is DERIVED from the brand bar's real footprint rather than a
    hardcoded constant."""
    return _layout.safe_zone_ok(box, frame_h=frame_h)


def safe_zone_bounds(frame_h=FRAME_H):
    """The (min_y, max_y) a text box must stay within."""
    return _layout.safe_zone_bounds(frame_h)


# ---- contrast / scrim -------------------------------------------------------
def _rel_luminance(rgb):
    """WCAG relative luminance of an (r,g,b) 0..255 color."""
    def _c(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * _c(r) + 0.7152 * _c(g) + 0.0722 * _c(b)


def contrast_ratio(fg_rgb, bg_rgb):
    """WCAG contrast ratio between a text color and a backdrop color (1..21)."""
    l1 = _rel_luminance(fg_rgb)
    l2 = _rel_luminance(bg_rgb)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def needs_scrim(text_rgb, backdrop_rgb, target=TARGET_CONTRAST):
    """True when text on this backdrop does NOT reach the target contrast, so a brand
    scrim must be added before the text is drawn (spec §1: never bare text on busy
    footage; ~4.5:1 checked at render)."""
    return contrast_ratio(text_rgb, backdrop_rgb) < target


def scrim_alpha_for(text_rgb, backdrop_rgb, scrim_rgb=(18, 30, 60), target=TARGET_CONTRAST):
    """The minimum scrim alpha (0..255) to blend over `backdrop_rgb` so the resulting
    surface gives `text_rgb` at least `target` contrast. Returns 0 when no scrim is
    needed. Blends scrim over backdrop and finds the least alpha that clears target."""
    if not needs_scrim(text_rgb, backdrop_rgb, target):
        return 0
    for a in range(0, 256, 5):
        blended = tuple(
            int((scrim_rgb[i] * a + backdrop_rgb[i] * (255 - a)) / 255)
            for i in range(3))
        if contrast_ratio(text_rgb, blended) >= target:
            return a
    return 255  # opaque scrim if even that is not enough (guarantees a readable frame)


# ---- identity anchor + card shapes ------------------------------------------
# NOTE (ruling 3, 2026-09-01): build_overlay no longer calls ensure_identity_anchor
# to merge the anchor into frame content — the anchor is now its own layout
# element (OverlaySpec.identity_line, formatted by story_layout.identity_anchor_line)
# burned on every hook frame and, on the ask frame, carried by the brand bar
# instead. These two helpers are kept as general-purpose text utilities.
def has_identity_anchor(text, tokens):
    """True when the overlay carries at least one identity anchor token (city/brand).
    Case-insensitive whole-token match."""
    low = str(text or "").lower()
    for t in tokens or ():
        t = str(t or "").strip().lower()
        if t and re.search(r"\b" + re.escape(t) + r"\b", low):
            return True
    return False


def ensure_identity_anchor(lines, tokens):
    """Guarantee the identity anchor is present: if none of `tokens` appears in the
    lines, prepend an anchor line with the first token (upper-cased). Returns the
    (possibly extended) list of lines."""
    joined = " ".join(lines)
    if has_identity_anchor(joined, tokens) or not tokens:
        return lines
    anchor = str(tokens[0]).strip().upper()
    return [anchor] + list(lines) if anchor else list(lines)


@dataclass
class OverlaySpec:
    """A validated, framed, grounded overlay ready to burn. `frames` is a list of
    frames (each a list of ALL-CAPS lines, BODY COPY ONLY — the identity anchor
    is no longer merged into frame content, see `identity_line` below, ruling 3
    2026-09-01). `flags` carries any non-fatal warnings (e.g. 'generic_safe: no
    brief, Echo guessed the copy')."""
    frames: list = field(default_factory=list)
    grounded_from: str = ""          # 'brief' | 'vision' | 'generic_safe'
    flags: list = field(default_factory=list)
    ask: str = ""
    ask_frame: list = field(default_factory=list)   # the single end-frame's lines
    # The small identity-anchor line (city + gym name), burned on EVERY hook
    # frame as its OWN layout element (never counted against the hook's
    # <=2-line content budget) and, on the ask frame, moved INTO the brand bar
    # in place of its normal handle text — see story_layout.identity_anchor_line
    # / identity_text_for_bar, the ONE place this text is formatted.
    identity_line: str = ""


GENERIC_SAFE_HOOK = "YOUR NEXT REP STARTS HERE"   # never a claim, never a number

# Ask phrasing shapes the copy law recognizes as a call-to-action. Used to count
# asks so the "exactly one ask frame" rule (spec §1) is a checked invariant, not a
# hope. A body overlay must carry ZERO asks (the ask lives only on the end-frame);
# the render must end with EXACTLY ONE ask frame.
_ASK_RE = re.compile(
    r"\b(book|reserve|save your spot|sign up|start (this|today|here|now)|join|"
    r"dm us|message us|comment|claim|get started|tag who|link in bio|try (a|your))\b",
    re.IGNORECASE)


def count_asks(text) -> int:
    """How many call-to-action phrases a block of overlay text carries."""
    return len(_ASK_RE.findall(str(text or "")))


def assert_one_ask_frame(body_frames, ask_text):
    """Enforce spec §1: the render ends with EXACTLY ONE ask frame, and the BODY
    frames carry ZERO asks (the ask is not sprinkled through the montage — the Roxx
    put zero asks on their 5 biggest; we put exactly one, at the end). Raises
    OverlayRejected on a violation. Returns the single ask frame (a list of lines)."""
    body_ask_count = sum(count_asks(" ".join(fr)) for fr in body_frames)
    if body_ask_count:
        raise OverlayRejected(
            f"body overlay carries {body_ask_count} ask(s); the ask belongs only on "
            f"the single end-frame (spec §1: exactly one ask frame)")
    asks_on_end = count_asks(ask_text)
    if asks_on_end != 1:
        raise OverlayRejected(
            f"end-frame carries {asks_on_end} ask(s); a Story render must end with "
            f"EXACTLY ONE ask frame (spec §1)")
    return layout_overlay(ask_text)[0] if layout_overlay(ask_text) else []


def build_overlay(raw_text, *, identity_tokens=(), gym=None, ask="", grounded_from="brief",
                  low_confidence=False, enforce_ask=False):
    """Turn a grounded overlay string into a validated OverlaySpec.

    Applies, IN ORDER: copy_gate scrub (no dashes, on-image too) -> ALL-CAPS layout
    (<=8 words/line, <=2 lines/frame, 3rd line -> next frame) -> identity anchor ->
    the per-gym avatar rail + copy_gate VIOLATION check + the avatar breach check.

    low_confidence=True (no brief and the vision sidecar was not confident): the copy
    is replaced with a GENERIC-SAFE overlay and a flag is added ('no brief, edit
    before approving') — Echo never fabricates a claim. Raises OverlayRejected only
    when the FINAL text still breaches copy_gate or the avatar rail (a real defect,
    surfaced loudly, never shipped)."""
    from . import copy_gate, post_quality

    # spec §1: "Identity anchor: a city / brand token appears in the overlay." There
    # is NO server-side default for identity_tokens (an audit found the frontend must
    # supply it) -- ensure_identity_anchor silently no-ops when tokens is empty, which
    # would ship a Story with no anchor at all. enforce_ask=True marks a REAL render
    # (story_studio), where that hard invariant applies: refuse to guess a city/gym
    # and refuse to silently ship without one -- HOLD with an honest reason instead
    # (enforce_ask=False is card-builder / unit use, where no render is happening).
    if enforce_ask and not identity_tokens:
        raise OverlayRejected(
            "no identity_tokens (city/gym anchor) were provided for this render; "
            "Story Studio will not burn a Story without its identity anchor -- add "
            "a city or gym token and retry")

    if low_confidence or not str(raw_text or "").strip():
        text = GENERIC_SAFE_HOOK
        grounded_from = "generic_safe"
    else:
        text = copy_gate.scrub(str(raw_text))

    # avatar rail on the (pre-cap) copy: HYROX blocks every gym except a per-gym
    # hyrox-avatar allowlisted client.
    breach = post_quality.avatar_breach(text, gym=gym)
    if breach:
        raise OverlayRejected(
            f"overlay copy breaches the LASSO avatar rail ('{breach}')")

    # copy_gate hard violations (banned dashes on-image too).
    if copy_gate.violations(text):
        raise OverlayRejected(
            f"overlay copy fails copy_gate: {copy_gate.violations(text)}")

    frames = layout_overlay(text)
    # Ruling 3 (2026-09-01): the identity anchor is its OWN small mono line on
    # EVERY hook frame, not merged into frame content (that used to consume
    # one of the hook's <=2 content lines AND only appeared on frame 0). The
    # burn primitive (clipper_render._default_overlay_burn) draws this line on
    # every hook frame; the ask frame carries it via the brand bar instead
    # (story_layout.identity_text_for_bar) so the ask keeps its full 2-line
    # budget for the ask alone.
    identity_line = _layout.identity_anchor_line(identity_tokens)

    flags = []
    if grounded_from == "generic_safe":
        flags.append("no brief and low vision confidence: copy is Echo's guess, "
                     "edit before approving")

    ask_text = copy_gate.scrub(str(ask or "")).upper()

    # spec §1: the render ends with EXACTLY ONE ask frame, and the body carries ZERO
    # asks. When a render is being composed (enforce_ask=True, from story_studio) this
    # is a HARD invariant — a violation HOLDS the render (OverlayRejected), never a
    # multi-ask or no-ask Story. When enforce_ask is False (card-builder / unit use)
    # the ask is simply stored.
    ask_frame = []
    if enforce_ask:
        ask_frame = assert_one_ask_frame(frames, ask_text)
    return OverlaySpec(frames=frames, grounded_from=grounded_from, flags=flags,
                       ask=ask_text, ask_frame=ask_frame, identity_line=identity_line)


class OverlayRejected(Exception):
    """The final overlay text still breaches copy_gate or the avatar rail. Surfaced
    loudly; the render is HELD, never shipped with bad on-image copy."""


# ---- card builders (stat / event) ------------------------------------------
def stat_card(name, number, place):
    """A Roxx stat card: NAME + NUMBER + PLACE, one per line, ALL-CAPS. Returns the
    overlay string (pre-layout)."""
    return "\n".join(p.strip().upper() for p in (name, number, place) if str(p).strip())


def event_card(what, when, ask):
    """A Roxx event card: WHAT + WHEN + one ask, ALL-CAPS. Returns the overlay string."""
    return "\n".join(p.strip().upper() for p in (what, when, ask) if str(p).strip())
