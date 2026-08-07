"""
No-creative fallback (agent/no_creative_fallback.py) + its portal_social wiring.

The client Organic Social calendar must never show a blank / broken card and must never
fabricate a photo. When a scheduled post has NO usable creative image, and only when the
AGENT_NO_CREATIVE_FALLBACK flag is armed, the card degrades to a clean website-style
infographic rendered from the post's OWN approved caption / pillar via the house PIL
renderer. Everything here is offline (pure PIL, no network).

The invariants, one test each (the four the build brief names, plus wiring + copy rules):
  * image_url present            -> returned unchanged, NO render.
  * image_url missing + caption + flag ON -> infographic rendered at the correct size,
    via the HOUSE renderer (renderer is mocked to assert it is the seam that is used).
  * no caption / no pillar text  -> returns None (block, no blank card, no fabrication).
  * flag OFF                      -> no fallback even when image_url is missing (unchanged).
  * flag defaults OFF.
  * the portal_social hook only fills a display image when the flag is ON.
  * HARD COPY RULES -> no em/en/hyphen dashes and never "vendor" in on-image text.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config
from agent import no_creative_fallback as ncf


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _img_size(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


# ---------------------------------------------------------------------------
# flag defaults OFF
# ---------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_NO_CREATIVE_FALLBACK", raising=False)
    assert config.no_creative_fallback_enabled() is False


# ---------------------------------------------------------------------------
# image_url present -> returned unchanged, NO render
# ---------------------------------------------------------------------------

def test_existing_image_returned_unchanged_no_render(monkeypatch):
    # Flag ON, but a usable image is present: the existing URL is returned as-is and the
    # renderer is NEVER called.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    calls = []

    def _spy(*a, **k):
        calls.append((a, k))
        return "SHOULD_NOT_BE_USED"

    post = {"caption": "We chase the leads. You close.", "pillar": "Sales are now",
            "format": "feed", "image_url": "https://cdn.example.com/real.png"}
    out = ncf.display_image_for(post, renderer=_spy)
    assert out == "https://cdn.example.com/real.png"
    assert calls == []  # no render happened


def test_existing_image_via_image_public_url_key(monkeypatch):
    # The portal shape uses image_public_url; a usable one is returned unchanged too.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    post = {"caption": "x", "pillar": "y", "format": "feed",
            "image_public_url": "https://cdn.example.com/real2.png"}
    assert ncf.display_image_for(post) == "https://cdn.example.com/real2.png"


# ---------------------------------------------------------------------------
# image_url missing + caption present + flag ON -> infographic at correct size,
# via the HOUSE renderer (mocked to assert the seam is used)
# ---------------------------------------------------------------------------

def test_missing_image_with_caption_renders_via_house_renderer(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    seen = {}

    def _fake_renderer(eyebrow, headline, deck, out_path, is_story=False):
        seen["eyebrow"] = eyebrow
        seen["headline"] = headline
        seen["deck"] = deck
        seen["is_story"] = is_story
        seen["out_path"] = out_path
        return out_path

    post = {"caption": "We do the heavy lifting. Your social, done for you.",
            "pillar": "We do the heavy lifting", "format": "feed", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path), renderer=_fake_renderer)
    assert out == seen["out_path"]
    # the house-renderer seam was used, fed ONLY the post's approved text
    assert seen["eyebrow"] == "WE DO THE HEAVY LIFTING"
    assert seen["headline"] == "We do the heavy lifting."  # first sentence of the caption
    assert seen["deck"] == "Your social, done for you."
    assert seen["is_story"] is False


def test_missing_image_renders_real_feed_infographic_1080(monkeypatch, tmp_path):
    # End to end through the REAL house PIL renderer: a 1080x1080 feed card is produced.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": ""}
    out = ncf.display_image_for(post, out_dir=str(tmp_path))
    assert out and os.path.isfile(out)
    assert _img_size(out) == (1080, 1080)


def test_missing_image_renders_real_story_infographic_1080x1920(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    post = {"caption": "One login. Every result.",
            "pillar": "The portal", "format": "story", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path))
    assert out and os.path.isfile(out)
    assert _img_size(out) == (1080, 1920)


def test_pillar_only_no_caption_still_renders_from_approved_text(monkeypatch, tmp_path):
    # No caption but a pillar: the pillar (approved text) carries the card as the
    # headline. Still no fabrication; the pillar is the post's own approved text.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    seen = {}

    def _fake(eyebrow, headline, deck, out_path, is_story=False):
        seen.update(headline=headline, out_path=out_path)
        return out_path

    post = {"caption": "", "pillar": "Sales are now", "format": "feed", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path), renderer=_fake)
    assert out == seen["out_path"]
    assert seen["headline"] == "Sales are now"


# ---------------------------------------------------------------------------
# no caption / no pillar text -> None (block, no blank card, no fabrication)
# ---------------------------------------------------------------------------

def test_no_text_returns_none_no_render(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return "X"

    post = {"caption": "", "pillar": "", "format": "feed", "image_url": None}
    assert ncf.display_image_for(post, renderer=_spy) is None
    assert calls == []  # nothing rendered: no blank card, no fabricated copy


def test_whitespace_only_text_returns_none(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    post = {"caption": "   ", "pillar": "  ", "format": "feed", "image_url": None}
    assert ncf.display_image_for(post) is None


# ---------------------------------------------------------------------------
# flag OFF -> no fallback even when image_url is missing (unchanged behavior)
# ---------------------------------------------------------------------------

def test_flag_off_no_fallback_even_when_image_missing(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "false")
    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return "X"

    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": None}
    assert ncf.display_image_for(post, renderer=_spy) is None
    assert calls == []  # flag OFF: no render, unchanged behavior


def test_flag_off_existing_image_still_returned(monkeypatch):
    # Flag OFF but an image IS present: existing behavior returns it unchanged.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "false")
    post = {"caption": "x", "pillar": "y", "format": "feed",
            "image_url": "https://cdn.example.com/keep.png"}
    assert ncf.display_image_for(post) == "https://cdn.example.com/keep.png"


# ---------------------------------------------------------------------------
# HARD COPY RULES: no dashes, never "vendor" on-image
# ---------------------------------------------------------------------------

def test_dashes_scrubbed_from_on_image_text():
    eb, hl, dk = ncf._approved_text(
        caption="Leads to close in 24 to 48 hours — fast.\nNo waiting.",
        pillar="Sales are now")
    for s in (eb, hl, dk):
        assert not any(ch in "‐‑‒–—―−-" for ch in s), s


def test_banned_word_raises_on_render(tmp_path):
    with pytest.raises(ValueError):
        ncf._render_infographic("PILLAR", "A vendor did this", "",
                                str(tmp_path / "x.png"), is_story=False)


# ---------------------------------------------------------------------------
# portal_social wiring: the hook only fills a display image when the flag is ON
# ---------------------------------------------------------------------------

def test_portal_hook_off_leaves_row_unchanged(monkeypatch):
    from agent import portal_social as ps
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "false")
    row = {"id": 7, "post_date": "2026-08-10", "status": "pending",
           "pillar": "All in one offer", "format": "feed",
           "image_url": "", "caption": "One platform for your whole gym."}
    post = ps._content_calendar_post(row)
    assert post["image_public_url"] == ""  # unchanged: no fallback with the flag OFF


def test_portal_hook_on_fills_display_image(monkeypatch, tmp_path):
    from agent import portal_social as ps
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    row = {"id": 8, "post_date": "2026-08-11", "status": "pending",
           "pillar": "The portal", "format": "feed",
           "image_url": "", "caption": "One login. Every result."}
    post = ps._content_calendar_post(row)
    assert post["image_public_url"]
    assert os.path.isfile(post["image_public_url"])
    assert _img_size(post["image_public_url"]) == (1080, 1080)


def test_portal_hook_on_keeps_existing_image(monkeypatch):
    from agent import portal_social as ps
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    row = {"id": 9, "post_date": "2026-08-12", "status": "pending",
           "pillar": "Proof", "format": "feed",
           "image_url": "https://cdn.example.com/hosted.png", "caption": "Built by owners."}
    post = ps._content_calendar_post(row)
    assert post["image_public_url"] == "https://cdn.example.com/hosted.png"
