"""
Website logo scrape (welcome-post pipeline, Part B).

Given a gym's resolved website, pull its logo in this order (first hit wins):
  1. <meta property="og:image">
  2. a header <img> inside <nav> or <header>
  3. <link rel="apple-touch-icon">
  4. any <img> whose src/alt/class/id matches /logo/i

Rejects anything below 200px on the long edge or that looks like a bare
favicon (the "LOGO NOT FOUND" case in the spec: never faked with text).
Converts to a transparent PNG, knocking out a uniform solid white or black
background when one is present. Stdlib only (html.parser + urllib), same
"zero extra deps" convention as media_host.py / slack_surface.py.
"""

import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

MIN_LONG_EDGE = 200
_UA = "Mozilla/5.0 (compatible; LassoEchoBot/1.0; +https://lassoframework.com)"


@dataclass
class LogoCandidate:
    url: str
    source: str  # "og:image" | "nav_header_img" | "apple-touch-icon" | "logo_pattern"


class _LogoHTMLParser(HTMLParser):
    """Collects logo candidates in priority order while walking the document
    once. Nav/header nesting is tracked with a small tag stack so an <img>
    inside <nav>...</nav> or <header>...</header> is recognized anywhere in
    that subtree, not only as a direct child."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []
        self.og_image = None
        self.apple_touch_icon = None
        self.nav_header_imgs = []
        self.logo_pattern_imgs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        tag_l = tag.lower()
        if tag_l == "meta" and self.og_image is None:
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop == "og:image" and a.get("content"):
                self.og_image = a["content"].strip()
        elif tag_l == "link" and self.apple_touch_icon is None:
            rel = (a.get("rel") or "").lower()
            if "apple-touch-icon" in rel and a.get("href"):
                self.apple_touch_icon = a["href"].strip()
        elif tag_l == "img":
            src = (a.get("src") or a.get("data-src") or "").strip()
            if src:
                if any(t in ("nav", "header") for t in self._stack):
                    self.nav_header_imgs.append(src)
                haystack = " ".join([
                    src, a.get("alt", ""), a.get("class", ""), a.get("id", ""),
                ]).lower()
                if "logo" in haystack:
                    self.logo_pattern_imgs.append(src)
        if tag_l not in ("img", "meta", "link", "br", "hr", "input"):
            self._stack.append(tag_l)

    def handle_endtag(self, tag):
        tag_l = tag.lower()
        # pop the nearest matching open tag (tolerant of malformed/unclosed HTML)
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == tag_l:
                del self._stack[i:]
                break


def _fetch(url, timeout=15):
    """GET url, return (bytes, content_type) or (None, None) on any failure.
    Never raises: a dead site is a normal, expected outcome here."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get_content_type()
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None, None


def find_logo_candidates(html_text, base_url):
    """Ordered LogoCandidate list per the spec's priority: og:image, nav/header
    img, apple-touch-icon, then /logo/i pattern matches. Duplicate URLs across
    tiers are kept only at their highest-priority occurrence."""
    parser = _LogoHTMLParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    ordered = []
    seen = set()

    def add(url, source):
        if not url:
            return
        abs_url = urllib.parse.urljoin(base_url, url)
        if abs_url in seen:
            return
        seen.add(abs_url)
        ordered.append(LogoCandidate(abs_url, source))

    add(parser.og_image, "og:image")
    for src in parser.nav_header_imgs:
        add(src, "nav_header_img")
    add(parser.apple_touch_icon, "apple-touch-icon")
    for src in parser.logo_pattern_imgs:
        add(src, "logo_pattern")
    return ordered


def _looks_like_favicon(url):
    name = os.path.basename(urllib.parse.urlsplit(url).path).lower()
    return "favicon" in name


def _knockout_solid_background(img):
    """Knock out a uniform solid white or black background (checked at the four
    corners) via flood fill from each corner. Leaves the image untouched when
    the corners are not uniform (already transparent, or a busy/photo logo)."""
    from PIL import ImageDraw

    img = img.convert("RGBA")
    w, h = img.size
    if w < 2 or h < 2:
        return img
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    px = img.load()
    corner_rgb = [px[x, y][:3] for x, y in corners]

    def near(c1, c2, tol=18):
        return all(abs(a - b) <= tol for a, b in zip(c1, c2))

    if not all(near(corner_rgb[0], c) for c in corner_rgb[1:]):
        return img
    ref = corner_rgb[0]
    is_white = all(v > 235 for v in ref)
    is_black = all(v < 20 for v in ref)
    if not (is_white or is_black):
        return img
    for cx, cy in corners:
        try:
            ImageDraw.floodfill(img, (cx, cy), (0, 0, 0, 0), thresh=30)
        except Exception:
            pass
    return img


def _validate_and_convert(image_bytes, source_url, out_path):
    """Open, size-check, background-knockout, save as PNG. Returns a result
    dict; never raises past this point (a corrupt download is just a rejected
    candidate, not a crash)."""
    import io
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:
        return {"ok": False, "reason": f"could not decode image: {type(e).__name__}"}
    long_edge = max(img.size)
    if long_edge < MIN_LONG_EDGE:
        return {"ok": False, "reason": f"below {MIN_LONG_EDGE}px on the long edge "
                                       f"({img.size[0]}x{img.size[1]})"}
    if _looks_like_favicon(source_url) and long_edge < 512:
        return {"ok": False, "reason": "favicon-only result"}
    img = _knockout_solid_background(img)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return {"ok": True, "path": out_path, "url": source_url}


def scrape_logo(website_url, out_path, fetch_fn=None):
    """
    Try each candidate in priority order; the first that downloads, decodes, and
    clears the size/favicon floor wins. fetch_fn(url) -> bytes, overridable for
    tests (defaults to a real HTTP GET). Returns:
      {"ok": True, "path": out_path, "url": ..., "source": "og:image"|...}
      {"ok": False, "reason": "LOGO NOT FOUND", "candidates_tried": N}
    Never fakes a logo with text; that decision is the caller's (welcome_new_clients).
    """
    fetch = fetch_fn or (lambda u: _fetch(u)[0])
    html_bytes = fetch(website_url)
    if not html_bytes:
        return {"ok": False, "reason": "LOGO NOT FOUND: could not fetch website",
               "candidates_tried": 0}
    html_text = html_bytes.decode("utf-8", errors="replace") if isinstance(html_bytes, bytes) else html_bytes
    candidates = find_logo_candidates(html_text, website_url)
    tried = 0
    for cand in candidates:
        tried += 1
        img_bytes = fetch(cand.url)
        if not img_bytes:
            continue
        result = _validate_and_convert(img_bytes, cand.url, out_path)
        if result["ok"]:
            result["source"] = cand.source
            return result
    return {"ok": False, "reason": "LOGO NOT FOUND: no candidate cleared the "
                                   "200px / non-favicon floor", "candidates_tried": tried}
