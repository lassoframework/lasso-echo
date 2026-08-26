"""copy_gate.py — the single house-style gate for every piece of client-facing
text Echo emits. Captions, welcome posts, video overlays, weekly reports, PDF
copy, quote cards: one scrubber, one validator, zero local reimplementations.

Replaces the local dash logic in: welcome_review, video_editor, creative_studio,
voice_template, weekly_report, pdf_report, clipper_render, podcast_quote_card,
no_creative_fallback. Each of those files should shrink in this wave.
"""
from __future__ import annotations
import re

# em/en/figure/horizontal-bar/minus and friends
_BANNED_DASHES = "‐‑‒–—―−"
_DASH_RE = re.compile("[" + _BANNED_DASHES + "]")
_INTRAWORD_HYPHEN_RE = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")
# protect URLs and @handles/#tags: hyphens inside them are load-bearing
_PROTECTED_RE = re.compile(r"(?:https?://\S+|\b[\w.-]+\.(?:com|net|org|io|co|fit|gym)\S*|[@#][\w.]+)", re.I)

_FILLER_OPENERS = re.compile(
    r"^(we're excited|we are excited|exciting news|just a reminder|don't forget|happy \w+day)\b", re.I)

ASK_RE = re.compile(
    r"(link in (our )?bio|book (a|your) (call|intro|class|spot)|dm us|dm \"?\w+\"?|message us|"
    r"comment \"?\w+\"?|sign up|get started|claim your|reserve your|try a (free )?class|"
    r"schedule (a|your)|start (here|today|your))", re.I)

def scrub(text: str) -> str:
    """Rewrite, never reject. Long dashes become ', '; intraword hyphens become a
    space; URLs, @handles and #tags pass through untouched."""
    out, last = [], 0
    s = str(text)
    for m in _PROTECTED_RE.finditer(s):
        out.append(_scrub_plain(s[last:m.start()])); out.append(m.group(0)); last = m.end()
    out.append(_scrub_plain(s[last:]))
    return "".join(out).strip()

def scrub_prompt(text: str) -> str:
    """Scrub banned dashes from AI generation prompt text (not client-facing copy).
    Banned dashes become a space (not ', ') and intraword hyphens are preserved
    because they are valid technical markup in generation prompts (e.g. 'left-aligned').
    URLs, @handles and #tags pass through untouched."""
    if not text:
        return ""
    s = str(text)
    cleaned = _DASH_RE.sub(" ", s)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

def _scrub_plain(t: str) -> str:
    t = _DASH_RE.sub(", ", t)
    t = _INTRAWORD_HYPHEN_RE.sub(" ", t)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t

def violations(text: str) -> list[str]:
    """Hard failures. A caption with any of these never reaches the queue."""
    v = []
    plain = _PROTECTED_RE.sub("", str(text))
    if _DASH_RE.search(plain): v.append("banned_dash")
    if _INTRAWORD_HYPHEN_RE.search(plain): v.append("intraword_hyphen")
    return v

def soft_flags(text: str) -> list[str]:
    """Quality flags the calendar grader scores against (not hard blocks)."""
    f = []
    t = str(text).strip()
    first = t.splitlines()[0] if t else ""
    if len(t) < 120: f.append("thin_caption")
    if first.startswith("#") or first.startswith("@"): f.append("hook_is_tag")
    if len(first) > 125: f.append("hook_too_long")
    if _FILLER_OPENERS.match(first): f.append("filler_opener")
    if not ASK_RE.search(t): f.append("no_ask")
    return f
