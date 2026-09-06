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

# ZERO-WIDTH / INVISIBLE CHARACTERS (Dean Holcomb, CrossFit Reverb, 2026-09-05).
# 90 of Reverb's 93 forward-book rows ended with a line that began U+200B, because
# the CTA was mined out of a source doc PASTED from a web page and nothing on the
# caption path ever normalized invisible characters: Python's \s does not match
# U+200B (category Cf), str.strip() does not remove it, and scrub only handled
# dashes. An invisible character in published copy is never intentional, so it is
# scrubbed the same way a banned dash is: rewritten out, never rejected.
#
# DELIBERATELY NOT STRIPPED: U+200D ZERO WIDTH JOINER and U+200C ZERO WIDTH
# NON-JOINER. Those are load-bearing inside emoji sequences (a ZWJ family or
# profession emoji is one grapheme held together by U+200D) and inside several
# scripts. Stripping them would corrupt emoji that gyms legitimately post.
_ZERO_WIDTH = "​⁠﻿­"
_ZERO_WIDTH_RE = re.compile("[" + _ZERO_WIDTH + "]")
_INTRAWORD_HYPHEN_RE = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")
# protect URLs and @handles/#tags: hyphens inside them are load-bearing
_PROTECTED_RE = re.compile(r"(?:https?://\S+|\b[\w.-]+\.(?:com|net|org|io|co|fit|gym)\S*|[@#][\w.]+)", re.I)

_FILLER_OPENERS = re.compile(
    r"^(we're excited|we are excited|exciting news|just a reminder|don't forget|happy \w+day)\b", re.I)

# The booking ask, as gyms actually write it. The article and the noun are almost never
# adjacent in real copy ("Book a FREE NO SWEAT intro", "Book your FIRST class"), and the
# original adjacent-only pattern read every one of those as having NO ASK. That single
# false negative was the dominant defect code across the whole fleet on 2026-08-31 and
# left gyms with a perfectly good CTA stuck below grade. Up to three modifier words are
# allowed between them; more than that is a sentence, not an ask.
_ASK_NOUN = r"(call|intro|class|spot|session|consult|consultation|visit|trial|tour|assessment)"
_ASK_MODS = r"(?: [\w'-]+){0,3}"

ASK_RE = re.compile(
    r"(link in (our )?bio"
    rf"|book (a|your|an){_ASK_MODS} {_ASK_NOUN}"
    rf"|schedule (a|your|an){_ASK_MODS} {_ASK_NOUN}"
    rf"|start with (a|your|an){_ASK_MODS} {_ASK_NOUN}"
    r"|dm us|dm \"?\w+\"?|message us|"
    r"comment \"?\w+\"?|sign up|get started|claim your|reserve your|try a (free )?class|"
    r"schedule (a|your)|start (here|today|your))", re.I)

def scrub(text: str) -> str:
    """Rewrite, never reject. Long dashes become ', '; intraword hyphens become a
    space; zero-width/invisible characters are removed; URLs, @handles and #tags
    pass through untouched."""
    out, last = [], 0
    s = _ZERO_WIDTH_RE.sub("", str(text))
    for m in _PROTECTED_RE.finditer(s):
        out.append(_scrub_plain(s[last:m.start()])); out.append(m.group(0)); last = m.end()
    out.append(_scrub_plain(s[last:]))
    return "".join(out).strip()

def scrub_prompt(text: str) -> str:
    """Scrub banned dashes from AI generation prompt text (not client-facing copy).
    Banned dashes become a space (not ', ') and intraword hyphens are preserved
    because they are valid technical markup in generation prompts (e.g. 'left-aligned').
    Zero-width/invisible characters are removed.
    URLs, @handles and #tags pass through untouched."""
    if not text:
        return ""
    s = _ZERO_WIDTH_RE.sub("", str(text))
    cleaned = _DASH_RE.sub(" ", s)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

def _scrub_plain(t: str) -> str:
    t = _DASH_RE.sub(", ", t)
    t = _INTRAWORD_HYPHEN_RE.sub(" ", t)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t

# ---------------------------------------------------------------------------
# CTA SHAPE (Dean Holcomb, CrossFit Reverb, 2026-09-05)
# ---------------------------------------------------------------------------
# "they all end with 'How do I get started with training at CrossFit Reverb?'
#  which doesn't make sense."
#
# He is right, and the reason is mechanical. ASK_RE asks only "does this text
# CONTAIN an ask phrase". An FAQ HEADING out of the gym's own source doc
# ("How do I get started with training at CrossFit Reverb?") contains the ask
# phrase "get started", so it passed ASK_RE and got stapled onto 90 captions as
# if it were a call to action. It is a QUESTION THE READER IS BEING ASKED, not
# a thing the reader is being asked to DO.
#
# A closing ask must tell the reader what to do. These rules say what a CTA is
# NOT. They are deliberately narrow: reject what is provably not an ask, and
# never try to judge whether approved copy is "good".
_INTERROGATIVE_OPENERS = re.compile(
    r"^\s*(how|what|why|when|where|who|which|whose|whom|"
    r"can|could|should|would|will|do|does|did|is|are|was|were|am|have|has|had)\b",
    re.I)

# A rhetorical hook inside a caption body is fine; this only judges text being
# used AS the closing ask.
def cta_defects(text: str) -> list[str]:
    """Why this text cannot serve as a post's closing call to action.

    Empty list means the text is usable as a CTA. Any entry means it is not,
    and the caller must pick a different one or leave the post with no ask
    (an ask-less post is legitimate; see calendar_grade's ask-rate band).
    """
    d = []
    t = _ZERO_WIDTH_RE.sub("", str(text or "")).strip()
    if not t:
        return ["cta_empty"]
    if not ASK_RE.search(t):
        d.append("cta_no_ask_phrase")
    # A question is not an instruction. Either terminal '?' or an interrogative
    # opener is enough; the Reverb line had both.
    if t.endswith("?") or _INTERROGATIVE_OPENERS.match(t):
        d.append("cta_is_question")
    # A heading, not a sentence: no terminal punctuation AND title-ish length.
    # Kept generous so a bare imperative ("Book your free intro") still passes.
    if len(t) > 120:
        d.append("cta_too_long")
    return d


def is_cta_shaped(text: str) -> bool:
    """True when `text` can honestly serve as a post's closing ask."""
    return not cta_defects(text)


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
