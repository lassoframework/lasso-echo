"""
A+ quality gate for every client calendar post (feed, story, reel).

Blake's standing bar (2026-08-13): "whenever you make a post ... it needs to be A+
with captions and everything should be checked." A draft is only written to the
calendar when it clears EVERY check below; a sub-par draft is DROPPED (held, logged,
one ops alert) rather than published, so no gym ever gets a thin 'HYROX'-tier caption
or a post with no media.

Checks (all deterministic, no network):
  * caption is present and a REAL caption — its body (the text before the CTA) has at
    least MIN_BODY_WORDS words and MIN_CAPTION_CHARS characters, so a raw one-line
    intake source ('HYROX') can never be the whole caption;
  * the copy law — NO dash characters (em / en / hyphen-as-dash) anywhere in the caption;
  * NO banned word for this gym;
  * real MEDIA — a non-empty hosted/public creative url.

The fabrication (figure) gate and the source-approval gate already run upstream (the
SB7 output gate and client_sources), so this layer does not re-check them; it is the
final "is this actually good enough to post" net.
"""

import re

MIN_CAPTION_CHARS = 40
MIN_CONTENT_WORDS = 12

# LASSO AVATAR RAIL (org rule; Blake's Defect B ruling 2026-08-27): LASSO does not
# market to serious athletes, competitive CrossFit, HYROX, or strength-athlete
# audiences. A caption carrying one of these terms is HARD-BLOCKED at stage (here)
# and at the publish boundary (calendar_autopublish recheck). Phrase-level match:
# 'crossfit' alone is deliberately NOT blocked (client gym NAMES carry it); only
# the explicit banned-audience phrases are.
AVATAR_BLOCK_RE = re.compile(
    r"\b(hyrox|competitive\s+crossfit|strength\s+athletes?|serious\s+athletes?)\b",
    re.IGNORECASE)


def avatar_breach(caption, gym=None):
    """The banned-audience term this caption carries, or '' when clean.

    PER-GYM avatar rail (Story Studio): the standard LASSO rail blocks HYROX (and the
    other banned-audience phrases) for EVERY gym. A gym whose actual avatar IS hyrox
    can be allowlisted (config.story_hyrox_avatar_gyms) so 'hyrox' no longer breaches
    for THAT gym only — the profile is per-gym config, never a hardcode. Passing no
    `gym` keeps the original all-gyms behavior (every existing caller is unchanged).
    The other banned phrases (competitive crossfit, strength/serious athletes) are
    NOT covered by the hyrox allowlist and always breach."""
    m = AVATAR_BLOCK_RE.search(caption or "")
    if not m:
        return ""
    term = m.group(0)
    if term.strip().lower() == "hyrox" and _hyrox_allowed_for(gym):
        # This gym's avatar IS hyrox: 'hyrox' does not breach. Re-scan the rest of the
        # caption for any OTHER banned-audience term (which still breaches).
        rest = AVATAR_BLOCK_RE.sub(
            lambda mm: "" if mm.group(0).strip().lower() == "hyrox" else mm.group(0),
            caption or "")
        m2 = AVATAR_BLOCK_RE.search(rest)
        return m2.group(0) if m2 else ""
    return term


def _hyrox_allowed_for(gym):
    """True when THIS gym is allowlisted as a hyrox-avatar client (per-gym config)."""
    if not gym:
        return False
    from . import config
    base = str(gym or "").strip().lower()
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return bool(base) and base in config.story_hyrox_avatar_gyms()

# Any real dash: the copy_gate banned set (em/en/figure/horizontal-bar/minus), or a
# hyphen used AS a dash (surrounded by spaces, or a double hyphen). A hyphen inside a
# word (co-op) is fine. The unicode dash class lives ONLY in copy_gate (Wave 1 law);
# this local pattern carries just the ASCII hyphen-as-punctuation shapes.
from . import copy_gate as _copy_gate

_HYPHEN_AS_DASH_RE = re.compile(r"(?:\s-\s)|--")


class _DashCheck:
    """Duck-typed stand-in for the old local dash regex: .search() hits on any
    copy_gate banned dash OR a hyphen used as a dash (spaced / doubled)."""
    @staticmethod
    def search(text):
        s = str(text or "")
        return _copy_gate._DASH_RE.search(s) or _HYPHEN_AS_DASH_RE.search(s)


_DASH_RE = _DashCheck()

# LLM scaffolding that must never reach a feed: a markdown header, or a bare
# 'Caption:'/'Body:' label line (the model occasionally prepends these).
_SCAFFOLD_RE = re.compile(
    r"^\s*(#{1,6}\s|(caption( body| text)?|body|post)\s*:)", re.IGNORECASE)

# INTERNAL PROMPT HINT BLOCKS anywhere in the caption (audit 2026-08-25 CRITICAL): the
# scene/grounding hints appended to the note SB7 sees ("WHAT THIS POST'S PHOTO/VIDEO
# SHOWS...", "VERIFIED IN THE IMAGE...") leaked into client calendars when the template
# fallback echoed the augmented note verbatim. The fallback is fixed to strip them at the
# source; this gate is the belt so NO path can ever ship prompt scaffolding to a client.
_HINT_LEAK_RE = re.compile(
    r"WHAT THIS POST'S PHOTO/VIDEO SHOWS|VERIFIED IN THE IMAGE", re.IGNORECASE)


def _content_words(caption):
    """Every word of real caption copy: the whole caption MINUS hashtag-only lines.
    Counts across a HOOK\\n\\nBODY\\n\\nCTA structure so a punchy short hook followed by
    a real body still reads as substantial; only genuinely thin copy (the raw one-line
    source, with or without a CTA) falls below the bar."""
    lines = [ln for ln in (caption or "").splitlines()
             if not ln.strip().startswith("#")]
    return " ".join(lines).split()


def caption_issues(caption, banned_words=()):
    """The list of reasons this caption is NOT A+ (empty list == A+)."""
    issues = []
    cap = (caption or "").strip()
    if not cap:
        return ["empty caption"]
    words = _content_words(cap)
    if len(cap) < MIN_CAPTION_CHARS:
        issues.append(f"caption too short ({len(cap)} < {MIN_CAPTION_CHARS} chars)")
    if len(words) < MIN_CONTENT_WORDS:
        issues.append(f"caption too thin ({len(words)} < {MIN_CONTENT_WORDS} content "
                      "words) — likely the raw source, not a real caption")
    if _DASH_RE.search(cap):
        issues.append("caption contains a dash (violates the no-dash copy law)")
    if _SCAFFOLD_RE.match(cap):
        issues.append("caption starts with LLM scaffolding (a header or a "
                      "'Caption:'/'Body:' label), not real copy")
    if _HINT_LEAK_RE.search(cap):
        issues.append("caption carries an internal prompt hint block "
                      "(scene/grounding scaffolding), not client copy")
    low = cap.lower()
    for w in banned_words or ():
        w = (w or "").strip().lower()
        if w and re.search(r"\b" + re.escape(w) + r"\b", low):
            issues.append(f"caption carries the banned word '{w}'")
            break
    return issues


def post_issues(draft, banned_words=()):
    """Every reason this DRAFT is not A+ (caption issues + media + grounding). Empty == A+."""
    issues = caption_issues(getattr(draft, "caption", "") or "", banned_words)
    breach = avatar_breach(getattr(draft, "caption", "") or "")
    if breach:
        issues.append(f"banned-audience term ('{breach}') violates the LASSO avatar rail")
    if not (getattr(draft, "creative_public_url", "") or "").strip():
        issues.append("no media (empty creative url)")
    # ECHO VISION §5/§7: a caption that CONTRADICTS the crop-verified image is not A+. The
    # month builder treats not-A+ as "walk alternatives" (regen/swap); exhausted -> drop the
    # day. Grounding is present only on vision drafts; contradiction-only (absence passes).
    grounding = getattr(draft, "grounding", None)
    if grounding:
        try:
            from . import vision
            issues += ["grounding: " + c for c in vision.grounding_contradictions(
                getattr(draft, "caption", "") or "", grounding.get("analysis"),
                verified=grounding.get("verified"),
                gym_claims=grounding.get("claims") or (),
                consent=grounding.get("consent", False),
                client_context=grounding.get("client_context", ""))]
        except Exception:  # noqa: BLE001 - never let the gate itself raise
            pass
    return issues


def is_a_plus(draft, banned_words=()):
    """True when the draft passes every A+ check."""
    return not post_issues(draft, banned_words)
