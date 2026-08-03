"""
Level-2 standing audit gate for an episode's podcast output SET.

Before an episode's finished assets surface to Blake, this gate runs automated,
offline checks over the whole SET (4 clips + 1 audiogram + 3 or 4 quote cards) and
either passes it or names every failure. A single named failure is enough to hold
the set: a deliberately broken asset MUST be caught here, not by Blake's eye.

Checks (each a hard line; failure names the offending asset):
  QUOTA              exactly 4 clips GMMS-{num}-S1..S4, exactly 1 audiogram,
                     3 or 4 quote cards
  CAPTIONS           no captioned clip carries a ghost duplicate caption (CRITICAL)
  INTRO              every clip has an animated intro (no static full-screen card)
  BOTTOM             every clip's bottom treatment is ok
  NO STATIC TAKEOVER no clip is a static full-screen takeover
  CAPTION-FREE       every clip ships a caption-free variant
  QUOTE VERBATIM     every quote card is verbatim ok; with a transcript, the
                     quote must appear verbatim (whitespace normalized) in it

Regeneration contract: regenerate_or_flag runs the audit, and for the FIRST
failure attempts exactly ONE regeneration through an injected regen_fn, then
re-audits. It never loops: if the set still fails, the result is returned flagged
with the failing check. regen_fn is injected so this is testable without any real
rendering. Pure functions, no network.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional
import re


@dataclass
class AuditResult:
    passed: bool
    failures: List[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    flagged_check: str = ""   # set by regenerate_or_flag when a fix did not clear


def expected_clip_names(num):
    """The four required clip names for an episode: GMMS-{num}-S1..S4."""
    return [f"GMMS-{num}-S{i}" for i in range(1, 5)]


def _norm(text):
    """Whitespace-normalized, lower-cased text for a tolerant verbatim compare."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _clips(output_set):
    return list(output_set.get("clips") or [])


def _quote_cards(output_set):
    return list(output_set.get("quote_cards") or [])


def _check_quota(output_set, episode_num):
    """QUOTA: exactly 4 correctly named clips, exactly 1 audiogram, 3 or 4 quote
    cards. Returns (ok, [failure strings]) naming each shortfall or excess."""
    fails = []
    clips = _clips(output_set)
    names = [str(c.get("name", "")) for c in clips]
    expected = expected_clip_names(episode_num)
    if len(clips) != 4:
        fails.append(f"QUOTA: expected 4 clips, found {len(clips)}")
    missing = [n for n in expected if n not in names]
    extra = [n for n in names if n not in expected]
    if missing:
        fails.append(f"QUOTA: missing clip(s) {', '.join(missing)}")
    if extra:
        fails.append(f"QUOTA: unexpected clip name(s) {', '.join(extra)}")

    audiogram = output_set.get("audiogram")
    if not audiogram:
        fails.append("QUOTA: expected 1 audiogram, found 0")

    cards = _quote_cards(output_set)
    if not (3 <= len(cards) <= 4):
        fails.append(f"QUOTA: expected 3 or 4 quote cards, found {len(cards)}")
    return (not fails), fails


def _check_captions(output_set):
    """CAPTIONS: a captioned clip with a ghost duplicate caption is CRITICAL."""
    fails = []
    for c in _clips(output_set):
        if c.get("captioned") and c.get("has_ghost_caption"):
            fails.append(
                f"CAPTIONS: clip {c.get('name', '?')} has a ghost duplicate "
                "caption (CRITICAL)")
    return (not fails), fails


def _check_intro(output_set):
    """INTRO: every clip must have an animated intro (no static text card)."""
    fails = []
    for c in _clips(output_set):
        if not c.get("intro_animated"):
            fails.append(
                f"INTRO: clip {c.get('name', '?')} has a static intro "
                "(no animated intro)")
    return (not fails), fails


def _check_bottom(output_set):
    """BOTTOM: every clip's bottom treatment must be ok."""
    fails = []
    for c in _clips(output_set):
        if not c.get("bottom_treatment_ok"):
            fails.append(
                f"BOTTOM: clip {c.get('name', '?')} bottom treatment is not ok")
    return (not fails), fails


def _check_no_static_takeover(output_set):
    """NO STATIC TAKEOVER: no clip may be a static full-screen takeover."""
    fails = []
    for c in _clips(output_set):
        if c.get("static_takeover"):
            fails.append(
                f"NO STATIC TAKEOVER: clip {c.get('name', '?')} is a static "
                "full-screen takeover")
    return (not fails), fails


def _check_caption_free(output_set):
    """CAPTION-FREE: every clip must ship a non-null caption-free variant."""
    fails = []
    for c in _clips(output_set):
        if not c.get("caption_free_variant"):
            fails.append(
                f"CAPTION-FREE: clip {c.get('name', '?')} has no caption-free "
                "variant")
    return (not fails), fails


def _check_quote_verbatim(output_set, transcript_text=None):
    """QUOTE VERBATIM: every quote card must be verbatim ok; when a transcript is
    given, the quote must ALSO appear verbatim (whitespace normalized) in it."""
    fails = []
    norm_transcript = _norm(transcript_text) if transcript_text else None
    for i, card in enumerate(_quote_cards(output_set)):
        label = card.get("path") or f"quote_card[{i}]"
        if not card.get("verbatim_ok"):
            fails.append(f"QUOTE VERBATIM: {label} is not marked verbatim ok")
            continue
        if norm_transcript is not None:
            q = _norm(card.get("quote_text"))
            if not q or q not in norm_transcript:
                fails.append(
                    f"QUOTE VERBATIM: {label} quote does not appear verbatim in "
                    "the transcript")
    return (not fails), fails


# The ordered check registry. audit_episode runs each and records ok in .checks;
# regenerate_or_flag walks this same order to find the FIRST failing check.
_CHECKS = ("QUOTA", "CAPTIONS", "INTRO", "BOTTOM", "NO_STATIC_TAKEOVER",
           "CAPTION_FREE", "QUOTE_VERBATIM")


def audit_episode(output_set, episode_num, transcript_text=None):
    """
    Audit one episode's output SET. Returns an AuditResult with passed (all checks
    clear), failures (every named failure across all checks), and checks (a dict
    of check-name -> bool ok). Nothing is mutated; nothing is rendered.
    """
    results = [
        ("QUOTA", _check_quota(output_set, episode_num)),
        ("CAPTIONS", _check_captions(output_set)),
        ("INTRO", _check_intro(output_set)),
        ("BOTTOM", _check_bottom(output_set)),
        ("NO_STATIC_TAKEOVER", _check_no_static_takeover(output_set)),
        ("CAPTION_FREE", _check_caption_free(output_set)),
        ("QUOTE_VERBATIM", _check_quote_verbatim(output_set, transcript_text)),
    ]
    checks, failures = {}, []
    for name, (ok, fails) in results:
        checks[name] = ok
        failures.extend(fails)
    return AuditResult(passed=not failures, failures=failures, checks=checks)


def _first_failing_check(result):
    """The name of the first failing check in registry order, or '' when clean."""
    for name in _CHECKS:
        if result.checks.get(name) is False:
            return name
    return ""


def regenerate_or_flag(output_set, episode_num, regen_fn, transcript_text=None):
    """
    Audit the set. If it passes, return the clean AuditResult. If it fails, attempt
    exactly ONE regeneration of the failing asset via regen_fn(failure), then
    re-audit ONCE. Never loops.

      - regen_fn(failure) is injected (pure/testable): given the first failing
        check name, it attempts a single fix and may return a NEW output_set to
        re-audit; when it returns None the original set is re-audited as-is.
      - If the re-audit passes, the passing result is returned.
      - If it still fails, the failing AuditResult is returned with flagged_check
        set to the check that remained broken.

    regen_fn is called exactly once (only when the first audit failed).
    """
    result = audit_episode(output_set, episode_num, transcript_text)
    if result.passed:
        return result

    failure = _first_failing_check(result)
    regenerated = regen_fn(failure)
    retry_set = regenerated if regenerated is not None else output_set

    retry = audit_episode(retry_set, episode_num, transcript_text)
    if retry.passed:
        return retry

    retry.flagged_check = _first_failing_check(retry)
    return retry
