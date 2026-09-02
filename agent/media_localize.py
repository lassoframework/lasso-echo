"""
media_localize.py — one durable, deterministic local copy of a hosted media file.

WHY THIS EXISTS (2026-09-02): two separate lanes need the SAME thing and got it wrong the
same way, because a Drive-lane creative has a hosted url but NO durable local path at build
time:

  * feed autofit (client_month_run._maybe_format_feed -> feed_image): every one of The
    Bolton Club's 36 Drive photos logged "feed autofit failed ... FileNotFoundError;
    posting the raw photo". The reframe silently never ran for ANY Drive-lane gym, so every
    out-of-spec photo fell through to the publish-time belt instead of the cached build path.
  * the Google Business mirror (gbp_mirror), which needs a local still to crop to 1200x900.

The trap both hit is that a naive tempfile.mkstemp copy is WORSE than useless downstream:
media_host._build_key is echo/<tenant>/<sha1-of-bytes>/<basename>, so a random basename
changes the R2 key on every build, which defeats content dedupe and writes a brand new
object for byte-identical pixels, forever. So the name here is derived from the SOURCE URL:
same media, same filename, same key, and the file is KEPT so the next build reuses it
instead of re-downloading.

Deliberately dumb: it fetches bytes and writes a file. No image decoding, no cropping, no
hosting. The callers own what they do with it. Never raises.
"""
from __future__ import annotations

import hashlib
import os

from . import config

DEFAULT_SUBDIR = "media_src"
# extensions we are willing to treat as a still image source
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def cache_dir(subdir: str = DEFAULT_SUBDIR) -> str:
    """The durable cache root for localized media. Under the persistent data volume when
    there is one (so a redeploy does not wipe it and re-download the fleet), else /tmp."""
    try:
        base = config.data_dir() or "/tmp"
    except Exception:  # noqa: BLE001
        base = "/tmp"
    d = os.path.join(base, subdir)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return "/tmp"
    return d


def stable_name(url, ext: str = "") -> str:
    """A DETERMINISTIC filename for a hosted media url: src_<sha1-16-of-url><ext>.

    The whole point (see the module docstring): a stable basename keeps both the local
    cache AND media_host's content-addressed R2 key stable across builds. A random temp
    name silently defeats both.
    """
    h = hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:16]
    return f"src_{h}{ext or '.jpg'}"


def _ext_for(url, ext_hint: str = "") -> str:
    for candidate in (ext_hint, os.path.splitext(str(url or "").split("?")[0])[1]):
        e = (candidate or "").lower()
        if e in _IMG_EXTS:
            return e
    return ".jpg"


def is_video_url(url) -> bool:
    return str(url or "").split("?")[0].lower().endswith(_VIDEO_EXTS)


def local_copy(url, *, ext_hint: str = "", subdir: str = DEFAULT_SUBDIR,
               downloader=None, logger=None):
    """A local path holding this url's bytes, or None.

    Reuses an already-cached copy (no fetch). Writes through a .part file and os.replace so
    a killed build never leaves a truncated image behind for the next one to decode. A video
    url is refused (callers here want a still). `downloader` is injectable for tests;
    production uses media_host.download_bytes. NEVER raises: any failure is None and the
    caller keeps its existing fallback behavior.
    """
    log = logger or (lambda m: print(f"[media-localize] {m}"))
    u = str(url or "").strip()
    if not u or not u.lower().startswith(("http://", "https://")):
        return None
    if is_video_url(u):
        return None
    dest = os.path.join(cache_dir(subdir), stable_name(u, _ext_for(u, ext_hint)))
    try:
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest                     # cached from an earlier build: no fetch
    except OSError:
        pass
    try:
        if downloader is None:
            from . import media_host
            downloader = media_host.download_bytes
        data = downloader(u)
        if not data:
            return None
        tmp = f"{dest}.{os.getpid()}.part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return dest
    except Exception as exc:  # noqa: BLE001 - never block a build on a fetch
        log(f"could not localize hosted media ({type(exc).__name__})")
        return None


def local_source_for(path, url, *, ext_hint: str = "", subdir: str = DEFAULT_SUBDIR,
                     downloader=None, logger=None):
    """The best local still for a creative: its own local file when it really exists, else a
    durable localized copy of its hosted url, else None.

    This is the one call a build lane needs. `path` is the creative's claimed local path
    (which for a Drive-lane creative exists in the record but NOT on disk, the whole reason
    this module exists) and `url` is its hosted public url.
    """
    p = str(path or "").strip()
    if p and os.path.isfile(p):
        return p
    return local_copy(url, ext_hint=ext_hint or os.path.splitext(p)[1],
                      subdir=subdir, downloader=downloader, logger=logger)
