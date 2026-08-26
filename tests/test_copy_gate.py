"""
Tests for agent/copy_gate.py — the single house-style gate.

Tests cover:
  1-6:  scrub() rewrites
  7-9:  violations() hard failures
  10:   ASK_RE matches
  11-13: soft_flags() quality flags
  14:   repo-wide guard: no local _DASH_RE / banned-char-class definitions
        remain in agent/ (outside copy_gate.py itself, outside tests/).
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent import copy_gate


# ---------------------------------------------------------------------------
# 1. scrub() — em dash becomes ", "
# ---------------------------------------------------------------------------
def test_scrub_em_dash():
    result = copy_gate.scrub("Lose weight—feel great")
    assert "—" not in result
    assert "Lose weight" in result
    assert "feel great" in result


# ---------------------------------------------------------------------------
# 2. scrub() — en dash becomes ", "
# ---------------------------------------------------------------------------
def test_scrub_en_dash():
    result = copy_gate.scrub("Monday–Friday sessions")
    assert "–" not in result
    assert "Monday" in result
    assert "Friday" in result


# ---------------------------------------------------------------------------
# 3. scrub() — intraword hyphen becomes space
# ---------------------------------------------------------------------------
def test_scrub_intraword_hyphen():
    result = copy_gate.scrub("coach-led training")
    assert result == "coach led training"


# ---------------------------------------------------------------------------
# 4. scrub() — URL hyphens pass through untouched
# ---------------------------------------------------------------------------
def test_scrub_url_hyphens_intact():
    url = "https://linktr.ee/no-sweat-intro"
    result = copy_gate.scrub(url)
    assert result == url


# ---------------------------------------------------------------------------
# 5. scrub() — @handle passes through untouched
# ---------------------------------------------------------------------------
def test_scrub_at_handle_intact():
    result = copy_gate.scrub("Follow @coach_amanda for tips")
    assert "@coach_amanda" in result


# ---------------------------------------------------------------------------
# 6. scrub() — #tag passes through untouched
# ---------------------------------------------------------------------------
def test_scrub_hashtag_intact():
    result = copy_gate.scrub("Join us #crossfit")
    assert "#crossfit" in result


# ---------------------------------------------------------------------------
# 7. violations() — returns ["banned_dash"] for em-dash text
# ---------------------------------------------------------------------------
def test_violations_em_dash():
    v = copy_gate.violations("Get fit—stay strong")
    assert "banned_dash" in v


# ---------------------------------------------------------------------------
# 8. violations() — returns ["intraword_hyphen"] for "coach-led program"
# ---------------------------------------------------------------------------
def test_violations_intraword_hyphen():
    v = copy_gate.violations("coach-led program")
    assert "intraword_hyphen" in v


# ---------------------------------------------------------------------------
# 9. violations() — returns [] for clean text
# ---------------------------------------------------------------------------
def test_violations_clean_text():
    v = copy_gate.violations("Sign up today and get started on your journey.")
    assert v == []


# ---------------------------------------------------------------------------
# 10. ASK_RE matches expected call-to-action phrases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", [
    "link in bio",
    "link in our bio",
    "book a call",
    "DM us",
    "sign up",
    "get started",
    "book your intro",
    "try a free class",
    "claim your spot",
    "reserve your spot",
    "schedule a class",
    "start today",
    "start here",
])
def test_ask_re_matches(phrase):
    assert copy_gate.ASK_RE.search(phrase), f"ASK_RE did not match: {phrase!r}"


# ---------------------------------------------------------------------------
# 11. soft_flags() — returns ["no_ask"] for caption with no ask
# ---------------------------------------------------------------------------
def test_soft_flags_no_ask():
    caption = (
        "Our gym community is like no other. "
        "We show up every single day and we push each other to be better. "
        "Come see what the buzz is about and experience it for yourself."
    )
    flags = copy_gate.soft_flags(caption)
    assert "no_ask" in flags


# ---------------------------------------------------------------------------
# 12. soft_flags() — returns ["thin_caption"] for caption under 120 chars
# ---------------------------------------------------------------------------
def test_soft_flags_thin_caption():
    short = "Get fit. Sign up today."
    flags = copy_gate.soft_flags(short)
    assert "thin_caption" in flags


# ---------------------------------------------------------------------------
# 13. soft_flags() — returns ["hook_is_tag"] when first line starts with #
# ---------------------------------------------------------------------------
def test_soft_flags_hook_is_tag():
    caption = "#CrossFit\nJoin us for our 6 am class and get started on your journey.\nSign up today."
    flags = copy_gate.soft_flags(caption)
    assert "hook_is_tag" in flags


# ---------------------------------------------------------------------------
# 14. REPO-WIDE GUARD: no local _DASH_RE definitions or banned-char-class
#     regex compile() calls remain outside copy_gate.py (in agent/, not tests/).
# ---------------------------------------------------------------------------
def test_no_local_dash_regex_definitions():
    """Grep agent/ (excluding copy_gate.py) for definitions of the banned char
    class in re.compile() or re.search() calls.  Zero must remain."""
    agent_dir = Path(__file__).parent.parent / "agent"

    # patterns that indicate a locally-defined dash regex
    # e.g. re.compile(r"[...—–...]") or literal em/en in compile
    pattern_class = re.compile(
        r're\.compile\s*\(\s*[rf]?["\'].*'
        r'(?:\\u2014|\\u2013|\\u2012|\\u2010|'
        r'—|–|‒|‐|'
        r'[‐‑‒–—―−])'
        r'.*["\']'
    )
    # Also catch raw literal em/en dash in a char class inside re.compile
    pattern_literal = re.compile(
        r're\.compile\s*\(.*\[.*[–—―‐‑‒−].*\]'
    )

    violations_found = []
    for py_file in sorted(agent_dir.rglob("*.py")):
        # skip copy_gate itself
        if py_file.name == "copy_gate.py":
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern_class.search(line) or pattern_literal.search(line):
                violations_found.append(f"{py_file.relative_to(agent_dir.parent)}:{lineno}: {line.strip()}")

    assert violations_found == [], (
        "Local dash-regex definitions found outside copy_gate.py. "
        "Migrate them to copy_gate:\n" + "\n".join(violations_found)
    )
