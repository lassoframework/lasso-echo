"""
Approval-card inline preview tests. The preview is ADDITIVE: the existing fields,
Creative line, buttons, and reply protocol must all still be present.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import slack_surface  # noqa: E402
from agent.drafter import Draft  # noqa: E402


def _draft(**kw):
    base = dict(draft_id="d1", account_key="k", platform="instagram", caption="c",
                hashtags=[], creative_path="", creative_public_url="",
                scheduled_for="t")
    base.update(kw)
    return Draft(**base)


def _images(blocks):
    return [b for b in blocks if b.get("type") == "image"]


def _all_text(blocks):
    out = []

    def add(v):
        if isinstance(v, str):
            out.append(v)

    for b in blocks:
        t = b.get("text")
        if isinstance(t, dict):
            add(t.get("text"))
        for el in b.get("elements", []) or []:
            if isinstance(el, dict):
                add(el.get("text"))  # button text is a dict, not a str -> skipped
        for fld in b.get("fields", []) or []:
            if isinstance(fld, dict):
                add(fld.get("text"))
    return "\n".join(out)


def test_single_hosted_image_gets_image_block():
    blocks = slack_surface.build_card_blocks(
        _draft(creative_public_url="https://cdn.example.com/a.png"))
    imgs = _images(blocks)
    assert len(imgs) == 1
    assert imgs[0]["image_url"] == "https://cdn.example.com/a.png"
    # existing structure intact
    assert any(b.get("type") == "actions" for b in blocks)
    assert "approve d1" in _all_text(blocks)


def test_carousel_shows_slide_one_cover():
    blocks = slack_surface.build_card_blocks(_draft(
        slides=["/s1.png", "/s2.png", "/s3.png"],
        slide_urls=["https://x/1.png", "https://x/2.png", "https://x/3.png"]))
    imgs = _images(blocks)
    assert len(imgs) == 1
    assert imgs[0]["image_url"] == "https://x/1.png"
    assert imgs[0]["alt_text"] == "carousel cover, slide 1 of 3"
    assert "slide 1 of 3" in _all_text(blocks)


def test_video_gets_no_image_block_watch_link():
    blocks = slack_surface.build_card_blocks(
        _draft(creative_public_url="https://cdn.example.com/reel.mp4"))
    assert _images(blocks) == []
    text = _all_text(blocks).lower()
    # Card must have a watch link pointing at the URL
    assert "https://cdn.example.com/reel.mp4" in _all_text(blocks)
    # No stale "not previewed inline" fallback when a URL is present
    assert "not previewed inline" not in text


def test_video_no_url_gets_fallback_note():
    # local-only path with no public URL -> fallback note
    blocks = slack_surface.build_card_blocks(
        _draft(creative_path="/tmp/clip.mp4", creative_public_url=""))
    assert _images(blocks) == []
    text = _all_text(blocks).lower()
    assert "not yet hosted" in text


def test_unhosted_creative_gets_note_not_image():
    blocks = slack_surface.build_card_blocks(_draft(creative_path="/local/a.png"))
    assert _images(blocks) == []
    assert "hosted at a public url" in _all_text(blocks).lower()
