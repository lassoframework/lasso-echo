"""Tests for the gym logo scraper (offline; HTTP is injected, never real)."""
import io
import os

import pytest
from PIL import Image

from agent import website_scan as ws


def _png_bytes(w=400, h=300, color=(20, 30, 60, 255), bg=None):
    """A PNG with an optional solid backdrop and a colored mark in the middle."""
    img = Image.new("RGBA", (w, h), bg if bg else (0, 0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(img).ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fetchers(html, images):
    """Build (fetch_html, fetch_bytes) closures over a fixed page + url->bytes map."""
    def fetch_html(url):
        return html, url

    def fetch_bytes(url):
        if url not in images:
            raise RuntimeError(f"404 {url}")
        return images[url], "image/png"
    return fetch_html, fetch_bytes


def test_candidate_order_prefers_og_image():
    html = '''
      <link rel="apple-touch-icon" href="/touch.png">
      <img src="/nav-logo.png" class="logo">
      <meta property="og:image" content="https://x.com/og.png">
      <img src="/hero-logo-banner.png">
    '''
    c = ws.logo_candidates(html, "https://x.com/")
    order = [why for _u, why in c]
    # logo-tagged header img must beat og:image (og:image is last resort on fitness sites)
    assert "header-img" in order
    assert "apple-touch-icon" in order
    assert "og:image" in order
    assert order.index("header-img") < order.index("og:image")


def test_absolute_and_relative_urls_resolved():
    html = '<meta property="og:image" content="/img/og.png">'
    c = ws.logo_candidates(html, "https://gym.com/home")
    assert c[0][0] == "https://gym.com/img/og.png"


def test_override_wins_before_network(tmp_path):
    override = str(tmp_path / "mylogo.png")
    Image.frombytes  # noqa (keep import warm)
    with open(override, "wb") as fh:
        fh.write(_png_bytes(500, 400))
    res = ws.fetch_logo("https://gym.com", "acme_ig", override_path=override,
                        out_dir=str(tmp_path / "out"))
    assert res.ok and res.source == "override"
    assert os.path.isfile(res.path)


def test_picks_first_usable_and_stores(tmp_path):
    html = '<meta property="og:image" content="https://g.com/og.png">'
    imgs = {"https://g.com/og.png": _png_bytes(600, 400)}
    fh, fb = _fetchers(html, imgs)
    res = ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=fh, fetch_bytes=fb)
    assert res.ok and res.source == "og:image"
    assert res.path == ws.stored_logo_path("acme_ig", str(tmp_path))
    assert Image.open(res.path).size[0] >= 200


def test_favicon_only_rejected(tmp_path):
    html = '<link rel="apple-touch-icon" href="https://g.com/favicon.ico">'
    imgs = {"https://g.com/favicon.ico": _png_bytes(64, 64)}
    fh, fb = _fetchers(html, imgs)
    res = ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=fh, fetch_bytes=fb)
    assert not res.ok and res.status == ws.STATUS_NOT_FOUND


def test_too_small_rejected(tmp_path):
    html = '<meta property="og:image" content="https://g.com/tiny.png">'
    imgs = {"https://g.com/tiny.png": _png_bytes(120, 120)}
    fh, fb = _fetchers(html, imgs)
    res = ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=fh, fetch_bytes=fb)
    assert not res.ok


def test_svg_skipped_falls_through_to_next(tmp_path):
    html = ('<img src="https://g.com/logo.svg" class="logo">'
            '<meta property="og:image" content="https://g.com/og.png">')
    imgs = {"https://g.com/og.png": _png_bytes(500, 500)}  # svg not in map
    fh, fb = _fetchers(html, imgs)
    res = ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=fh, fetch_bytes=fb)
    assert res.ok and res.source == "og:image"


def test_white_background_knocked_out(tmp_path):
    html = '<meta property="og:image" content="https://g.com/logo.png">'
    imgs = {"https://g.com/logo.png": _png_bytes(400, 400, bg=(255, 255, 255, 255))}
    fh, fb = _fetchers(html, imgs)
    res = ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=fh, fetch_bytes=fb)
    assert res.ok
    im = Image.open(res.path).convert("RGBA")
    # a corner that was solid white must now be transparent
    assert im.getpixel((0, 0))[3] == 0


def test_cache_returned_without_refetch(tmp_path):
    html = '<meta property="og:image" content="https://g.com/og.png">'
    imgs = {"https://g.com/og.png": _png_bytes(500, 500)}
    fh, fb = _fetchers(html, imgs)
    ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                  fetch_html=fh, fetch_bytes=fb)

    def boom(_):
        raise AssertionError("should not refetch when cached")
    res = ws.fetch_logo("https://g.com", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=boom, fetch_bytes=boom)
    assert res.ok and res.source == "cache"


def test_no_website_is_not_found(tmp_path):
    res = ws.fetch_logo("", "acme_ig", out_dir=str(tmp_path))
    assert not res.ok and "no website" in res.note


def test_site_fetch_failure_is_not_found(tmp_path):
    def boom(_):
        raise RuntimeError("dns error")
    res = ws.fetch_logo("https://nope.gym", "acme_ig", out_dir=str(tmp_path),
                        fetch_html=boom)
    assert not res.ok and "fetch failed" in res.note


# ---- SVG rasterization + small-logo upscaling (2026-08-13: pull every logo) -------

def _svg_bytes(w=300, h=120):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
            f'<rect width="{w}" height="{h}" fill="#0b1e3c"/>'
            f'<circle cx="{w//2}" cy="{h//2}" r="{h//3}" fill="#e23b2e"/></svg>'
            ).encode("utf-8")


def test_looks_svg_detects_markup_and_ext():
    assert ws._looks_svg(_svg_bytes(), ".svg") is True
    assert ws._looks_svg(_svg_bytes(), "", "image/svg+xml") is True
    assert ws._looks_svg(_svg_bytes(), "") is True            # sniffed from <svg
    assert ws._looks_svg(_png_bytes(), ".png") is False


def test_svg_logo_is_rasterized_and_accepted(tmp_path):
    pytest.importorskip("cairosvg")
    html = '<img src="/brand-logo.svg" class="logo">'
    images = {"https://gym.com/brand-logo.svg": _svg_bytes()}
    fh, fb = _fetchers(html, images)
    # the fetcher returns image/svg+xml content-type
    def fb_svg(url):
        if url not in images:
            raise RuntimeError("404")
        return images[url], "image/svg+xml"
    res = ws.fetch_logo("https://gym.com", "svgtest",
                        out_dir=str(tmp_path), fetch_html=fh, fetch_bytes=fb_svg)
    assert res.ok, res.note
    assert "svg" in res.source                                # rasterized path
    assert max(Image.open(res.path).size) >= ws.MIN_LONG_EDGE


def test_small_raster_logo_is_accepted_and_upscaled(tmp_path):
    # a real ~150px brand mark (fills its frame on a colored bg) used to be rejected as
    # "too small"; now accepted + upscaled. Navy bg so knockout is a no-op and trim keeps it.
    from PIL import ImageDraw
    img = Image.new("RGBA", (150, 110), (11, 30, 60, 255))
    ImageDraw.Draw(img).ellipse([20, 15, 130, 95], fill=(226, 59, 46, 255))
    buf = io.BytesIO(); img.save(buf, "PNG")
    html = '<img src="/logo.png" class="logo">'
    fh, fb = _fetchers(html, {"https://gym.com/logo.png": buf.getvalue()})
    res = ws.fetch_logo("https://gym.com", "smalltest",
                        out_dir=str(tmp_path), fetch_html=fh, fetch_bytes=fb)
    assert res.ok, res.note
    assert max(Image.open(res.path).size) >= ws.UPSCALE_TO    # upscaled to fill the zone


def test_upscale_never_downscales():
    from PIL import Image as _I
    big = _I.new("RGBA", (800, 600))
    assert ws._upscale_to(big).size == (800, 600)             # already large -> unchanged
