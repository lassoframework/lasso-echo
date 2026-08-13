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
MIN_BODY_WORDS = 8

# Any real dash: em/en/figure/horizontal-bar/minus, or a hyphen used AS a dash
# (surrounded by spaces, or a double hyphen). A hyphen inside a word (co-op) is fine.
_DASH_RE = re.compile(r"[‐-―−]|(?:\s-\s)|--")


def _body(caption):
    """The caption's body: everything before the first blank-line-separated CTA block."""
    return (caption or "").split("\n\n", 1)[0].strip()


def caption_issues(caption, banned_words=()):
    """The list of reasons this caption is NOT A+ (empty list == A+)."""
    issues = []
    cap = (caption or "").strip()
    if not cap:
        return ["empty caption"]
    body = _body(cap)
    if len(cap) < MIN_CAPTION_CHARS:
        issues.append(f"caption too short ({len(cap)} < {MIN_CAPTION_CHARS} chars)")
    if len(body.split()) < MIN_BODY_WORDS:
        issues.append(f"caption body too thin ({len(body.split())} < "
                      f"{MIN_BODY_WORDS} words) — likely the raw source, not a caption")
    if _DASH_RE.search(cap):
        issues.append("caption contains a dash (violates the no-dash copy law)")
    low = cap.lower()
    for w in banned_words or ():
        w = (w or "").strip().lower()
        if w and re.search(r"\b" + re.escape(w) + r"\b", low):
            issues.append(f"caption carries the banned word '{w}'")
            break
    return issues


def post_issues(draft, banned_words=()):
    """Every reason this DRAFT is not A+ (caption issues + media). Empty == A+."""
    issues = caption_issues(getattr(draft, "caption", "") or "", banned_words)
    if not (getattr(draft, "creative_public_url", "") or "").strip():
        issues.append("no media (empty creative url)")
    return issues


def is_a_plus(draft, banned_words=()):
    """True when the draft passes every A+ check."""
    return not post_issues(draft, banned_words)
