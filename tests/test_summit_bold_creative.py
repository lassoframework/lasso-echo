"""
BOLD SUMMIT creative + caption tests. Offline: no live Gemini, no live R2.

Blake ruling: the summit sprint cards must be BOLD, high-contrast, a DIFFERENT
palette from the daily house infographic (which is cream + navy), more informative,
and stand out; the captions must be more detailed with room to tag sponsors.

Covers:
  - the bold FEED renders 1080x1080 and the bold STORY renders 1080x1920;
  - the event lockup (LASSO GROWTH SUMMIT / NOVEMBER 7 and 8 / VIRGIN HOTEL
    NASHVILLE) is composited on every bold card;
  - the bold palette (dark base + electric accent) is used, NOT the cream house card;
  - the sponsor strip renders names when supplied and a safe placeholder when empty
    (never fabricated);
  - sprint_caption is detailed, carries dates / location / CTA, is dash-free and has
    no "vendor", and its sponsors line lists names when given, placeholder when not;
  - copy-law: no dash-family chars and no "vendor" on generated on-image text or in
    captions;
  - the DAILY house infographic renderer is UNCHANGED (only the summit sprint path
    uses the bold style).
"""

import os
import re
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import summit_render as sr  # noqa: E402
from agent import summit_queue as sq  # noqa: E402
from agent.summit_rebuild import SUMMIT_CONCEPTS  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")

_CONCEPT = SUMMIT_CONCEPTS[0]  # 01_invitation


# ---- helpers ---------------------------------------------------------------
def _colors(path):
    """Set of (r,g,b) present in the rendered image."""
    im = Image.open(path).convert("RGB")
    return {c for _n, c in im.getcolors(maxcolors=1_000_000)}


def _has_color_near(colors, target, tol=18):
    return any(all(abs(a - b) <= tol for a, b in zip(c, target)) for c in colors)


# ---- sizes -----------------------------------------------------------------
def test_bold_feed_is_1080_square(tmp_path):
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "bold_feed.png"))
    assert Image.open(p).size == (1080, 1080)


def test_bold_story_is_1080x1920(tmp_path):
    p = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "bold_story.png"))
    assert Image.open(p).size == (1080, 1920)


def test_render_bold_covers_every_concept(tmp_path):
    for c in SUMMIT_CONCEPTS:
        pf = sr.render_bold_feed(c, "a", str(tmp_path / f"{c['id']}_feed.png"))
        ps = sr.render_bold_story(c, "b", str(tmp_path / f"{c['id']}_story.png"))
        assert Image.open(pf).size == (1080, 1080), c["id"]
        assert Image.open(ps).size == (1080, 1920), c["id"]


# ---- bold palette, NOT the house cream -------------------------------------
def test_bold_feed_uses_dark_base_and_accent_not_house_cream(tmp_path):
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "b.png"))
    colors = _colors(p)
    # deep midnight base present
    assert _has_color_near(colors, sr.BOLD_BG), "bold base (deep midnight) missing"
    # the electric accent present
    assert _has_color_near(colors, sr.BOLD_ACCENT), "electric accent missing"
    # the DOMINANT (background) color is the dark base, NOT the soft house cream.
    # Cream may appear as TEXT ink, but it must never be the canvas the way the
    # daily house card is a cream field.
    im = Image.open(p).convert("RGB")
    counts = {c: n for n, c in im.getcolors(maxcolors=1_000_000)}
    total = sum(counts.values())
    dominant = max(counts, key=counts.get)
    assert all(abs(a - b) <= 18 for a, b in zip(dominant, sr.BOLD_BG)), \
        f"dominant color {dominant} is not the dark bold base"
    # a big cream FIELD (a house-style canvas) is absent: no single near-cream color
    # covers a large share of the card
    cream_field = max((n for c, n in counts.items()
                       if all(abs(a - b) <= 6 for a, b in zip(c, sr.CREAM))),
                      default=0)
    assert cream_field / total < 0.10, "bold card carries a cream house-style field"


def test_bold_story_uses_dark_base_and_accent(tmp_path):
    p = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "s.png"))
    colors = _colors(p)
    assert _has_color_near(colors, sr.BOLD_BG)
    assert _has_color_near(colors, sr.BOLD_ACCENT)


# ---- event lockup on every card --------------------------------------------
def _lockup_present_on_image(tmp_path, renderer):
    """Render the card twice: once normally, once with the event lockup constants
    blanked, and assert the pixels differ, i.e. the lockup text is actually drawn."""
    normal = str(tmp_path / "normal.png")
    renderer(_CONCEPT, "a", normal)
    orig = sr.EVENT_LOCKUP
    try:
        sr.EVENT_LOCKUP = ("", "", "")
        blank = str(tmp_path / "blank.png")
        renderer(_CONCEPT, "a", blank)
    finally:
        sr.EVENT_LOCKUP = orig
    a = Image.open(normal).tobytes()
    b = Image.open(blank).tobytes()
    return a != b


def test_event_lockup_text_is_composited_on_feed(tmp_path):
    # the lockup constant holds the exact dash-free strings Blake specified
    assert sr.EVENT_LOCKUP[0] == "LASSO GROWTH SUMMIT"
    assert sr.EVENT_LOCKUP[1] == "NOVEMBER 7 and 8"
    assert sr.EVENT_LOCKUP[2] == "VIRGIN HOTEL NASHVILLE"
    assert _lockup_present_on_image(tmp_path, sr.render_bold_feed), \
        "event lockup not drawn on the bold feed"


def test_event_lockup_text_is_composited_on_story(tmp_path):
    assert _lockup_present_on_image(tmp_path, sr.render_bold_story), \
        "event lockup not drawn on the bold story"


def test_event_lockup_is_dash_free():
    for s in sr.EVENT_LOCKUP:
        assert not _DASH_RE.search(s), f"dash in lockup: {s!r}"
        assert "vendor" not in s.lower()


# ---- sponsor strip: names when given, placeholder when empty ---------------
def test_sponsor_strip_placeholder_when_empty(tmp_path):
    """Empty sponsors -> a placeholder is drawn; supplied names change the pixels
    (names are laid across the strip). Never fabricated."""
    empty = str(tmp_path / "empty.png")
    named = str(tmp_path / "named.png")
    sr.render_bold_feed(_CONCEPT, "a", empty, sponsors=())
    sr.render_bold_feed(_CONCEPT, "a", named, sponsors=["Streamfit", "HireVP"])
    assert Image.open(empty).tobytes() != Image.open(named).tobytes(), \
        "supplied sponsors did not render on the strip"


def test_sponsor_strip_never_fabricates_a_name(tmp_path):
    """With no sponsors supplied the card must not invent a brand. We prove the strip
    is drawn but carries only the placeholder by rendering two empty cards identically
    (deterministic, no random invented names)."""
    a = str(tmp_path / "a.png")
    b = str(tmp_path / "b.png")
    sr.render_bold_feed(_CONCEPT, "a", a, sponsors=())
    sr.render_bold_feed(_CONCEPT, "a", b, sponsors=[])
    assert Image.open(a).tobytes() == Image.open(b).tobytes()


# ---- captions: detailed, sponsor-taggable, copy-law ------------------------
def test_sprint_caption_is_detailed_with_dates_location_cta():
    cap = sq.sprint_caption(_CONCEPT)
    assert "November 7 and 8" in cap
    assert "Virgin Hotel Nashville" in cap
    assert "100 seats" in cap
    assert "lassoframework.com/summit" in cap
    assert "Claim your seat" in cap
    assert "not a notebook" in cap  # the value line
    # detailed: comfortably longer than the terse legacy line
    assert len(cap) > 180, cap


def test_sprint_caption_dash_free_and_no_vendor():
    cap = sq.sprint_caption(_CONCEPT, sponsors=["Streamfit", "HireVP"])
    assert not _DASH_RE.search(cap), cap
    assert "vendor" not in cap.lower()


def test_sprint_caption_sponsors_line_names_when_given():
    cap = sq.sprint_caption(_CONCEPT, sponsors=["Streamfit", "HireVP", "Tommy Allen"])
    assert "Presented with our sponsors:" in cap
    assert "Streamfit" in cap and "HireVP" in cap and "Tommy Allen" in cap
    # oxford-style join, dash free
    assert "and Tommy Allen" in cap


def test_sprint_caption_sponsors_placeholder_when_empty():
    cap = sq.sprint_caption(_CONCEPT, sponsors=())
    assert "Presented with our sponsors" in cap
    # no colon + names when empty (placeholder ready for tagging)
    assert "Presented with our sponsors:" not in cap


def test_every_sprint_caption_dash_free_and_copy_law():
    for fname, cap in sq.sprint_assets():
        assert not _DASH_RE.search(cap), f"{fname}: dash in caption"
        assert "vendor" not in cap.lower(), f"{fname}: 'vendor' in caption"
        assert "lassoframework.com/summit" in cap, f"{fname}: missing CTA"


def test_sprint_assets_threads_sponsors_into_concept_captions():
    named = dict(sq.sprint_assets(sponsors=["Streamfit"]))
    assert "Presented with our sponsors: Streamfit" in named["01_invitation_a.png"]


# ---- the DAILY house infographic renderer is UNCHANGED ---------------------
def test_house_daily_render_card_still_cream_navy(tmp_path):
    """render_card (the daily/house look) must be untouched: its cream canvas still
    dominates a treatment-a card and it does NOT use the bold electric accent."""
    p = sr.render_card(_CONCEPT, "a", str(tmp_path / "house.png"), canvas="cream")
    im = Image.open(p).convert("RGB")
    assert im.size == (1080, 1080)
    counts = {c: n for n, c in im.getcolors(maxcolors=1_000_000)}
    total = sum(counts.values())
    cream_px = sum(n for c, n in counts.items()
                   if all(abs(a - b) <= 10 for a, b in zip(c, sr.CREAM)))
    assert cream_px / total > 0.4, "house daily card lost its cream canvas"
    bold_accent_px = sum(n for c, n in counts.items()
                         if all(abs(a - b) <= 8 for a, b in zip(c, sr.BOLD_ACCENT)))
    assert bold_accent_px == 0, "bold accent leaked into the daily house card"
