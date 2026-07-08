"""
Content category taxonomy, platform sub-topic rotation, and seven-day posting schedule.

Six categories cover every draftable content source:
  podcast   - episode release cards and infographics (podcast_release, podcast_cards)
  platform  - sourced from the four LASSO platform PDFs:
                One Platform, Platform Story, Why LASSO Sites Win,
                Platform Overview 2026.
              NOT the 2027 Growth Playbook (that is summit).
  b2b       - B2B gym-owner creative concepts from regen_library
  summit    - summit campaign content from the Growth Playbook
  book      - The Full Gym sales playbook campaign
  doctrine  - regular LASSO pillars from lasso_now.md (house doctrine)

Platform sub-topics (10 items, rotates deterministically; no repeat within 10 days):
  ads, google, nurture, website, social, portal,
  sales_diagnosis, case_study, pricing, positioning

Platform wording filter (applied at caption build time when the flag is ON):
  - "vendor logins" / "vendors logins" -> "logins"
  - "vendors" -> "companies"
  - "vendor"  -> "company"
  - em dash, en dash, hyphen -> space (stripped per the standing dash law)

Seven-day posting schedule (AGENT_CATEGORY_ROTATION, default OFF):
  Mon: podcast release card  (infographic)
  Tue: platform              (video; fallback infographic + ops alert if no clip)
  Wed: b2b                   (infographic)
  Thu: podcast clip          (video; fallback infographic + ops alert if no clip)
  Fri: summit                (infographic)
  Sat: platform              (video; fallback infographic + ops alert if no clip)
  Sun: podcast episode       (infographic)

Most exports are pure functions. apply_daily_format() fires an ops alert when a
video slot has no clip — it is the only function with a side effect.
All functions are gated behind config.category_rotation_enabled() in the callers.
"""

import re

CATEGORIES = ("podcast", "platform", "b2b", "summit", "book", "doctrine")

PLATFORM_SUBTOPICS = (
    "ads", "google", "nurture", "website", "social",
    "portal", "sales_diagnosis", "case_study", "pricing", "positioning",
)

# Regex patterns for the wording filter
_VENDOR_LOGINS_RE = re.compile(r'\bvendors?\s+logins?\b', re.IGNORECASE)
_VENDOR_RE = re.compile(r'\bvendors?\b', re.IGNORECASE)
# All dash-family characters: em dash, en dash, figure dash, non-breaking hyphen, hyphen-minus
_DASH_RE = re.compile(r'[—–‒‐-]')


def filter_platform_copy(text):
    """
    Reword vendor/vendors and strip dash characters from platform PDF content.
    Applied at caption build time (flag ON only). Pure: no state, no side effects.

    Replacements:
      "vendor logins" / "vendors logins" -> "logins"
      "vendors" -> "companies"
      "vendor"  -> "company"
      Any dash character -> space (per the standing copy law)
    """
    if not text:
        return text
    # Specific compound phrase first (before the generic vendor pattern)
    text = _VENDOR_LOGINS_RE.sub("logins", text)

    def _vendor_repl(m):
        return "companies" if m.group(0).lower().endswith("s") else "company"

    text = _VENDOR_RE.sub(_vendor_repl, text)
    # Strip all dash characters, replace with space
    text = _DASH_RE.sub(" ", text)
    # Collapse runs of spaces, trim
    text = re.sub(r" {2,}", " ", text).strip()
    return text


def platform_subtopic_for_day(day_key):
    """
    The platform sub-topic for a given day. Deterministic rotation across the
    10-item list; consecutive days always get different sub-topics so no sub-topic
    repeats within 10 days.
    """
    from .content_planner import _day_seq
    return PLATFORM_SUBTOPICS[_day_seq(day_key) % len(PLATFORM_SUBTOPICS)]


def category_for_draft(draft):
    """
    Derive the content category for any draft. Returns one of the six CATEGORIES
    strings; never empty (defaults to 'doctrine' when nothing more specific resolves).

    Resolution order:
      1. Explicit draft_type set by the builder (podcast, book, summit, b2b)
      2. source_fragments citation markers (cite:podcast_ep*, cite:platform_2026_*, ...)
      3. creative_path sidecar set field (b2b images from regen_library)
      4. Fallback: 'doctrine'
    """
    dt = (getattr(draft, "draft_type", "") or "").lower()
    if dt == "podcast":
        return "podcast"
    if dt == "book":
        return "book"
    if dt == "summit":
        return "summit"
    if dt == "b2b":
        return "b2b"

    frags = list(getattr(draft, "source_fragments", []) or [])
    for f in frags:
        s = str(f)
        if s.startswith("cite:podcast_ep"):
            return "podcast"
        if s.startswith("cite:platform_2026"):
            return "platform"
        if s.startswith("cite:book") or s.startswith("cite:the_full_gym"):
            return "book"
        if s.startswith("cite:summit") or s.startswith("cite:growth_playbook"):
            return "summit"
        if s == "cite:lasso_now":
            return "doctrine"

    # Check creative path's sidecar set field for b2b library cards
    cpath = getattr(draft, "creative_path", "") or ""
    if cpath:
        try:
            from .rotation import sidecar_set
            if sidecar_set(cpath) == "b2b":
                return "b2b"
        except Exception:
            pass

    return "doctrine"


# Seven-day posting schedule: weekday abbr -> (category, posting_format, fallback_format).
# posting_format "video" slots pull from Opus; if no clip exists, fallback_format is used.
_DAILY_SCHEDULE = {
    "mon": ("podcast",  "infographic", None),
    "tue": ("platform", "video",       "infographic"),
    "wed": ("b2b",      "infographic", None),
    "thu": ("podcast",  "video",       "infographic"),
    "fri": ("summit",   "infographic", None),
    "sat": ("platform", "video",       "infographic"),
    "sun": ("podcast",  "infographic", None),
}


def schedule_for_day(day_key):
    """
    Return (category, posting_format, fallback_format) for day_key.
    Returns None when AGENT_CATEGORY_ROTATION is OFF.
    posting_format: "video" | "infographic".
    fallback_format: "infographic" for video slots; None for infographic-only slots.
    """
    from . import config as _cfg
    if not _cfg.category_rotation_enabled():
        return None
    from .schedule import weekday_abbr
    return _DAILY_SCHEDULE.get(weekday_abbr(day_key))


def apply_daily_format(day_key, has_clip, account_key=""):
    """
    Resolve the posting format for day_key. When a video slot has no clip, fires
    one ops alert naming the slot and account, then returns the fallback format.

    Returns "infographic" and fires no alert when AGENT_CATEGORY_ROTATION is OFF.
    """
    from . import ops_alerts as _ops
    from .schedule import weekday_abbr
    entry = schedule_for_day(day_key)
    if entry is None:
        return "infographic"
    category, fmt, fallback = entry
    if fmt == "video" and not has_clip:
        slot = weekday_abbr(day_key).upper()
        msg = f"empty video slot: {slot} ({category}) has no clip"
        if account_key:
            msg += f" for {account_key}"
        _ops.alert(msg)
        return fallback or "infographic"
    return fmt
