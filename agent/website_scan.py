"""
website_scan.py — fetch a gym's website and pull its logo for the welcome pipeline.

There was no existing website-scan / logo-fetch capability in this repo (confirmed by
a ground-truth audit 2026-08-04), so this is built fresh. It is deliberately small and
dependency-light: `requests` for the fetch (already a dep), stdlib regex for the tag
scan (the targets are a handful of <meta>/<link>/<img> tags, not full DOM parsing), and
PIL for the image work (already a dep).

Logo resolution order, first USABLE candidate wins:
  1. og:image  (<meta property="og:image">)                     — the sharing image
  2. a header / nav <img> whose src|class|id|alt contains 'logo'
  3. apple-touch-icon (<link rel="apple-touch-icon">)
  4. any <img> or <link> whose URL path matches /logo/i

Rejections (never faked with text — the caller surfaces LOGO_NOT_FOUND so a human adds
one): a result under 200px on the long edge, or a favicon-only result.

Output: a PNG with a transparent background (a solid white or solid black backdrop is
knocked out), written per-gym to the persistent volume at a stable path so a re-run
never re-fetches. Blake can always override by dropping a file in the portal; an
override path is honored before any network call.

Everything is injectable (fetch_html / fetch_bytes / out_dir) so the whole thing is
unit-testable offline with no network.
"""

import io
import os
import re
from urllib.parse import urljoin, urlparse

from PIL import Image

from . import config

# --- constants ---------------------------------------------------------------
_UA = ("Mozilla/5.0 (compatible; LASSO-Echo/1.0; +https://lassoframework.com)")
MIN_LONG_EDGE = 200          # anything smaller is a favicon / icon, rejected
_FAVICON_HINT = re.compile(r"favicon|/icon[s]?/|apple-touch-icon-precomposed", re.I)
_RASTER_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Third-party domains that host affiliate badges / franchise marks — never the gym's own logo
_BLOCKED_LOGO_DOMAINS = frozenset([
    "crossfit.com", "www.crossfit.com",
    "mindbodyonline.com", "www.mindbodyonline.com",
    "zingfit.com", "www.zingfit.com",
    "glofox.com", "www.glofox.com",
])

# Franchise / software brand keywords that flag a candidate as a partner badge, not the gym logo.
# _FRANCHISE_BLOB_RE: word-boundary match for natural-language blobs (class/id/alt text).
# _FRANCHISE_PATH_RE: no-boundary match for URL paths (handles camelCase filenames).
_FRANCHISE_BLOB_RE = re.compile(
    r"\b(?:crossfit|hyrox|wodify|mindbody|zingfit|glofox|pushpress|"
    r"pikfit|trainerize|triib|sugarwod)\b",
    re.I
)
_FRANCHISE_PATH_RE = re.compile(
    r"(?i)crossfit|hyrox|wodify|mindbody|zingfit|glofox|pushpress|pikfit|trainerize|sugarwod"
)

STATUS_OK = "OK"
STATUS_NOT_FOUND = "LOGO_NOT_FOUND"


class LogoResult:
    """The outcome of a logo pull. status is OK or LOGO_NOT_FOUND; on OK, path is the
    stored transparent PNG and source names which strategy found it."""

    def __init__(self, status, path=None, source=None, size=None, note=""):
        self.status = status
        self.path = path
        self.source = source
        self.size = size
        self.note = note

    @property
    def ok(self):
        return self.status == STATUS_OK

    def as_dict(self):
        return {"status": self.status, "path": self.path, "source": self.source,
                "size": self.size, "note": self.note}


# --- storage -----------------------------------------------------------------

def logo_dir(out_dir=None):
    """Where per-gym logos are stored. Defaults to the persistent volume when it
    exists (Railway: /data), else the local content library, so a scrape is not
    lost on restart. Overridable for tests."""
    if out_dir:
        return out_dir
    env = os.environ.get("AGENT_WELCOME_LOGO_DIR", "").strip()
    if env:
        return env
    if os.path.isdir("/data"):
        return "/data/welcome_logos"
    return os.path.join(config.LIBRARY_PATH, "welcome_logos")


def stored_logo_path(account_key, out_dir=None):
    return os.path.join(logo_dir(out_dir), f"{account_key}.png")


# --- fetch (injectable) ------------------------------------------------------

def _default_fetch_html(url):
    import requests
    r = requests.get(url, timeout=15, headers={"User-Agent": _UA})
    r.raise_for_status()
    return r.text, str(r.url)


def _default_fetch_bytes(url):
    import requests
    r = requests.get(url, timeout=15, headers={"User-Agent": _UA})
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "")


# --- HTML tag scan (regex, no DOM dep) ---------------------------------------

def _attr(tag, name):
    m = re.search(rf'{name}\s*=\s*"([^"]*)"', tag, re.I) or \
        re.search(rf"{name}\s*=\s*'([^']*)'", tag, re.I)
    return m.group(1).strip() if m else ""


def logo_candidates(html, base_url):
    """Ordered, de-duplicated absolute candidate URLs for the site's logo, best
    first. Pure function over the HTML so it is trivially testable.

    Priority: logo-tagged img > apple-touch-icon > logo-url > og:image (last resort).
    og:image is a social-share hero — on fitness sites it is almost always an action
    photo, not the brand mark. Only use it when nothing else exists.
    """
    cands = []

    def add(u, why, blob=""):
        if not u:
            return
        absu = urljoin(base_url, u)
        if urlparse(absu).netloc in _BLOCKED_LOGO_DOMAINS:
            return
        # Reject franchise/partner badges: blob match for header-img; path match for all
        if why == "header-img" and blob and _FRANCHISE_BLOB_RE.search(blob):
            return
        if _FRANCHISE_PATH_RE.search(urlparse(absu).path):
            return
        if absu not in [c[0] for c in cands]:
            cands.append((absu, why))

    og_images = []

    # 1. a header/nav <img> that looks like a logo (class/id/alt contains "logo"),
    #    but skip partner/franchise badges (CrossFit affiliate, Hyrox, Wodify, etc.)
    for tag in re.findall(r"<img\b[^>]*>", html, re.I):
        blob = " ".join([_attr(tag, "src"), _attr(tag, "class"),
                         _attr(tag, "id"), _attr(tag, "alt")]).lower()
        if "logo" in blob:
            add(_attr(tag, "src") or _attr(tag, "data-src"), "header-img", blob=blob)

    # 2. apple-touch-icon
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        rel = _attr(tag, "rel").lower()
        if "apple-touch-icon" in rel:
            add(_attr(tag, "href"), "apple-touch-icon")

    # 3. anything whose URL path matches /logo/i
    for tag in re.findall(r"<(?:img|link)\b[^>]*>", html, re.I):
        u = _attr(tag, "src") or _attr(tag, "href") or _attr(tag, "data-src")
        if u and re.search(r"logo", u, re.I):
            add(u, "logo-url")

    # 4. og:image / twitter:image — last resort; often a hero photo on fitness sites
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        prop = (_attr(tag, "property") or _attr(tag, "name")).lower()
        if prop in ("og:image", "og:image:url", "twitter:image"):
            og_images.append(_attr(tag, "content"))

    for u in og_images:
        add(u, "og:image")

    return cands


# --- image processing --------------------------------------------------------

def _is_favicon(url):
    path = urlparse(url).path.lower()
    if _FAVICON_HINT.search(url):
        return True
    if path.endswith(".ico"):
        return True
    return False

def _load_raster(data):
    """Bytes -> RGBA PIL image, or None if it is not a raster image we handle
    (e.g. an SVG, which we cannot rasterize without an extra dependency)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def _knockout_bg(img, tol=12):
    """Knock a SOLID white or SOLID black backdrop out to transparent so the mark
    sits cleanly on the card plate. Only fires when the four corners agree on a
    near-white or near-black color (a photo/gradient logo is left untouched)."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]

    def near(c, v):
        return all(abs(c[i] - v) <= tol for i in range(3)) and c[3] >= 250

    if all(near(c, 255) for c in corners):
        key = 255
    elif all(near(c, 0) for c in corners):
        key = 0
    else:
        return img  # not a solid backdrop; leave as-is

    out = img.copy()
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = op[x, y]
            if abs(r - key) <= tol and abs(g - key) <= tol and abs(b - key) <= tol:
                op[x, y] = (r, g, b, 0)
    return out


def _trim_alpha(img):
    """Crop transparent margins so the mark fills its box on the plate."""
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


# --- public ------------------------------------------------------------------

def fetch_logo(website_url, account_key, override_path=None, out_dir=None,
               fetch_html=None, fetch_bytes=None, force=False):
    """Resolve and store a gym's logo. Returns a LogoResult.

    override_path: a human-supplied logo (portal drop) wins before any network call.
    Caching: a previously-stored logo is returned as-is unless force=True.
    fetch_html / fetch_bytes: injectable for tests; default to real HTTP.
    """
    dest = stored_logo_path(account_key, out_dir)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 0. explicit override (portal drop) beats everything
    if override_path and os.path.isfile(override_path):
        img = _trim_alpha(_knockout_bg(Image.open(override_path).convert("RGBA")))
        img.save(dest)
        return LogoResult(STATUS_OK, dest, "override", img.size,
                          note="human-supplied logo")

    if os.path.isfile(dest) and not force:
        return LogoResult(STATUS_OK, dest, "cache", Image.open(dest).size)

    if not website_url:
        return LogoResult(STATUS_NOT_FOUND, note="no website on record")

    fetch_html = fetch_html or _default_fetch_html
    fetch_bytes = fetch_bytes or _default_fetch_bytes

    if not re.match(r"^https?://", website_url, re.I):
        website_url = "https://" + website_url

    try:
        html, final_url = fetch_html(website_url)
    except Exception as e:
        return LogoResult(STATUS_NOT_FOUND, note=f"site fetch failed: {e}")

    tried = []
    for url, why in logo_candidates(html, final_url):
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if _is_favicon(url):
            tried.append((why, "favicon-only, skipped"))
            continue
        if ext and ext not in _RASTER_EXT and ext != "":
            # e.g. .svg we cannot rasterize without a new dependency
            tried.append((why, f"unsupported {ext}"))
            continue
        try:
            data, _ctype = fetch_bytes(url)
        except Exception as e:
            tried.append((why, f"download failed: {e}"))
            continue
        img = _load_raster(data)
        if img is None:
            tried.append((why, "not a raster image"))
            continue
        if max(img.size) < MIN_LONG_EDGE:
            tried.append((why, f"too small {img.size}"))
            continue
        # og:image is typically a landscape hero photo — reject very wide OR very large images.
        # Rationale: fitness og:images are almost always athlete shots; logos are small and
        # square-ish. Anything > 1400px on the long edge is a banner or full-bleed photo.
        if why == "og:image" and img.size[1] > 0:
            ratio = img.size[0] / img.size[1]
            if ratio > 2.2 or max(img.size) > 1400:
                tried.append((why, f"landscape photo rejected ({img.size[0]}×{img.size[1]})"))
                continue
        knocked = _knockout_bg(img)
        alpha_vals = list(knocked.split()[3].getdata())
        opaque_frac = sum(1 for a in alpha_vals if a > 127) / max(len(alpha_vals), 1)
        if opaque_frac < 0.08:
            # Knockout ate the marks — check if original has dark content visible on a white plate
            orig_px = list(img.convert("RGBA").getdata())
            dark_frac = sum(1 for px in orig_px
                            if px[3] > 127 and px[0] < 200 and px[1] < 200 and px[2] < 200
                            ) / max(len(orig_px), 1)
            if dark_frac < 0.03:
                # White logo on white bg — would be invisible on a white plate; try next candidate
                tried.append((why, f"white-on-white logo skipped (dark {dark_frac:.0%})"))
                continue
            img = _trim_alpha(img)  # has dark marks on colored bg; keep original without knockout
        else:
            img = _trim_alpha(knocked)
        if max(img.size) < MIN_LONG_EDGE:
            tried.append((why, f"too small after trim {img.size}"))
            continue
        # Reject logos where most opaque pixels are white/near-white — invisible on the card plate
        fin_px = list(img.getdata())
        opaque_px = [px for px in fin_px if px[3] > 127]
        if opaque_px:
            white_count = sum(1 for px in opaque_px if px[0] > 200 and px[1] > 200 and px[2] > 200)
            if white_count / len(opaque_px) > 0.40:
                tried.append((why, f"white-on-plate logo ({white_count/len(opaque_px):.0%} white opaque)"))
                continue
        img.save(dest)
        return LogoResult(STATUS_OK, dest, why, img.size)

    return LogoResult(STATUS_NOT_FOUND,
                      note="no usable logo (" + "; ".join(f"{w}:{r}" for w, r in tried) + ")"
                      if tried else "no logo candidates on the page")
