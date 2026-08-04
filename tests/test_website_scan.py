"""Tests for agent/website_scan.py: logo candidate extraction + validation.
All HTTP is faked via fetch_fn so these never touch the network."""

import io

import pytest
from PIL import Image

from agent import website_scan as ws


def _png_bytes(size, mode="RGB", color=(10, 20, 30)):
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# candidate extraction (priority order)
# ---------------------------------------------------------------------------

def test_og_image_is_first_priority():
    html = """
    <html><head><meta property="og:image" content="/img/social.png"></head>
    <body><header><img src="/img/header-logo.png"></header></body></html>
    """
    cands = ws.find_logo_candidates(html, "https://acme.com")
    assert cands[0].url == "https://acme.com/img/social.png"
    assert cands[0].source == "og:image"


def test_nav_header_img_before_apple_touch_icon():
    html = """
    <html><head><link rel="apple-touch-icon" href="/icons/touch.png"></head>
    <body><nav><div><img src="/nav/logo.png"></div></nav></body></html>
    """
    cands = ws.find_logo_candidates(html, "https://acme.com")
    sources = [c.source for c in cands]
    assert sources.index("nav_header_img") < sources.index("apple-touch-icon")


def test_logo_pattern_match_by_class_or_alt():
    html = """
    <html><body>
    <img src="/assets/mark.png" class="site-logo" alt="Acme logo">
    </body></html>
    """
    cands = ws.find_logo_candidates(html, "https://acme.com")
    assert any(c.source == "logo_pattern" for c in cands)


def test_duplicate_url_kept_at_highest_priority_only():
    html = """
    <html><head><meta property="og:image" content="/x.png"></head>
    <body><nav><img src="/x.png"></nav></body></html>
    """
    cands = ws.find_logo_candidates(html, "https://acme.com")
    urls = [c.url for c in cands]
    assert urls.count("https://acme.com/x.png") == 1
    assert cands[0].source == "og:image"


def test_relative_urls_resolved_against_base():
    html = '<meta property="og:image" content="logo.png">'
    cands = ws.find_logo_candidates(html, "https://acme.com/about/team")
    assert cands[0].url == "https://acme.com/about/logo.png"


def test_no_candidates_on_empty_page():
    assert ws.find_logo_candidates("<html><body>hi</body></html>", "https://acme.com") == []


# ---------------------------------------------------------------------------
# scrape_logo: validation + rejection rules
# ---------------------------------------------------------------------------

def test_scrape_logo_accepts_first_valid_candidate(tmp_path):
    html = '<meta property="og:image" content="/logo.png">'
    img_bytes = _png_bytes((400, 300))

    def fetch(url):
        if url.endswith("acme.com"):
            return html.encode()
        if url.endswith("/logo.png"):
            return img_bytes
        return None

    out = str(tmp_path / "logo.png")
    result = ws.scrape_logo("https://acme.com", out, fetch_fn=fetch)
    assert result["ok"] is True
    assert result["source"] == "og:image"
    assert result["path"] == out
    im = Image.open(out)
    assert max(im.size) >= ws.MIN_LONG_EDGE


def test_scrape_logo_rejects_below_min_long_edge_and_falls_through(tmp_path):
    html = """
    <meta property="og:image" content="/tiny.png">
    <html><body><nav><img src="/big.png"></nav></body></html>
    """
    tiny = _png_bytes((64, 64))
    big = _png_bytes((500, 500))

    def fetch(url):
        if url.endswith("acme.com"):
            return html.encode()
        if url.endswith("/tiny.png"):
            return tiny
        if url.endswith("/big.png"):
            return big
        return None

    out = str(tmp_path / "logo.png")
    result = ws.scrape_logo("https://acme.com", out, fetch_fn=fetch)
    assert result["ok"] is True
    assert result["source"] == "nav_header_img"


def test_scrape_logo_rejects_favicon_named_result(tmp_path):
    html = '<link rel="apple-touch-icon" href="/favicon.png">'
    small_icon = _png_bytes((180, 180))

    def fetch(url):
        if url.endswith("acme.com"):
            return html.encode()
        if url.endswith("favicon.png"):
            return small_icon
        return None

    out = str(tmp_path / "logo.png")
    result = ws.scrape_logo("https://acme.com", out, fetch_fn=fetch)
    assert result["ok"] is False
    assert "LOGO NOT FOUND" in result["reason"]


def test_scrape_logo_not_found_when_site_unreachable(tmp_path):
    out = str(tmp_path / "logo.png")
    result = ws.scrape_logo("https://dead-site.example", out, fetch_fn=lambda u: None)
    assert result["ok"] is False
    assert "LOGO NOT FOUND" in result["reason"]


def test_scrape_logo_not_found_when_no_candidates_clear_floor(tmp_path):
    html = '<meta property="og:image" content="/tiny.png">'
    tiny = _png_bytes((50, 50))

    def fetch(url):
        if url.endswith("acme.com"):
            return html.encode()
        return tiny

    out = str(tmp_path / "logo.png")
    result = ws.scrape_logo("https://acme.com", out, fetch_fn=fetch)
    assert result["ok"] is False
    assert result["candidates_tried"] == 1


# ---------------------------------------------------------------------------
# background knockout
# ---------------------------------------------------------------------------

def test_knockout_white_background_becomes_transparent_at_corners():
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    out = ws._knockout_solid_background(img)
    assert out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0


def test_knockout_leaves_non_uniform_corners_untouched():
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    img.putpixel((0, 0), (10, 200, 10))  # one corner very different -> not uniform
    out = ws._knockout_solid_background(img)
    assert out.getpixel((99, 99))[3] == 255  # untouched, opaque


def test_looks_like_favicon_by_filename():
    assert ws._looks_like_favicon("https://acme.com/favicon.png") is True
    assert ws._looks_like_favicon("https://acme.com/logo.png") is False
