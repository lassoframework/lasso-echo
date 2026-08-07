"""
No-creative fallback (agent/no_creative_fallback.py) + its portal_social wiring.

The client Organic Social calendar must never show a blank / broken card and must never
fabricate a photo. When a scheduled post has NO usable creative image, and only when the
AGENT_NO_CREATIVE_FALLBACK flag is armed, the card degrades to a clean website-style
infographic rendered from the post's OWN approved caption / pillar via the house PIL
renderer, then HOSTED so the portal (a service with no access to the worker's disk) can
display it: display_image_for returns the PUBLIC hosted url, never a local path.
Everything here is offline (pure PIL + an injected fake host, no network).

The invariants, one test each (the four the build brief names, plus hosting, idempotency,
the font/render guard, wiring, and copy rules):
  * image_url present            -> returned unchanged, NO render, NO host.
  * image_url missing + caption + flag ON -> the HOSTED url is returned (not a local
    path); the house renderer + host seams are both used and fed only approved text.
  * no caption / no pillar text  -> returns None (block, no blank card, no fabrication).
  * flag OFF                      -> no fallback even when image_url is missing.
  * flag defaults OFF.
  * host returns falsy / hosting disabled -> None (empty state, never a local path).
  * a render / font failure       -> None (never raises into the caller / web request).
  * idempotency                   -> repeated reads of the same post reuse the hosted url.
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
# helpers / fixtures
# ---------------------------------------------------------------------------

def _img_size(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


@pytest.fixture(autouse=True)
def _clear_cache():
    """The in-process url cache is process-global; clear it around every test so one
    test's hosted url never leaks into another's assertion."""
    ncf._URL_CACHE.clear()
    yield
    ncf._URL_CACHE.clear()


def _capturing_host():
    """A fake host that records the local path it was handed and returns a stable public
    url derived from the basename, so tests can assert the HOSTED url is returned and the
    render seam produced a real file."""
    seen = {}

    def _host(local_path, tenant):
        seen["local_path"] = local_path
        seen["tenant"] = tenant
        seen["existed"] = os.path.isfile(local_path)
        return f"https://cdn.example.com/hosted/{os.path.basename(local_path)}"

    return _host, seen


# ---------------------------------------------------------------------------
# flag defaults OFF
# ---------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_NO_CREATIVE_FALLBACK", raising=False)
    assert config.no_creative_fallback_enabled() is False


# ---------------------------------------------------------------------------
# image_url present -> returned unchanged, NO render, NO host
# ---------------------------------------------------------------------------

def test_existing_image_returned_unchanged_no_render_no_host(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    calls = []

    def _r(*a, **k):
        calls.append("render"); return "X"

    def _h(*a, **k):
        calls.append("host"); return "Y"

    post = {"caption": "We chase the leads. You close.", "pillar": "Sales are now",
            "format": "feed", "image_url": "https://cdn.example.com/real.png"}
    out = ncf.display_image_for(post, renderer=_r, host=_h)
    assert out == "https://cdn.example.com/real.png"
    assert calls == []  # nothing rendered, nothing hosted


def test_existing_image_via_image_public_url_key(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    post = {"caption": "x", "pillar": "y", "format": "feed",
            "image_public_url": "https://cdn.example.com/real2.png"}
    assert ncf.display_image_for(post) == "https://cdn.example.com/real2.png"


# ---------------------------------------------------------------------------
# image_url missing + caption present + flag ON -> the HOSTED url (not a local path),
# via the HOUSE renderer + host seams, fed only approved text
# ---------------------------------------------------------------------------

def test_missing_image_with_caption_returns_hosted_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    seen = {}

    def _fake_renderer(eyebrow, headline, deck, out_path, is_story=False):
        seen.update(eyebrow=eyebrow, headline=headline, deck=deck,
                    is_story=is_story, out_path=out_path)
        # write a real (tiny) file so the host seam sees a file on disk
        with open(out_path, "wb") as fh:
            fh.write(b"PNGDATA")
        return out_path

    host, host_seen = _capturing_host()
    post = {"caption": "We do the heavy lifting. Your social, done for you.",
            "pillar": "We do the heavy lifting", "format": "feed", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path),
                               renderer=_fake_renderer, host=host)

    # the returned value is the HOSTED url, never the local path
    assert out.startswith("https://cdn.example.com/hosted/")
    assert out != seen["out_path"]
    assert host_seen["local_path"] == seen["out_path"]
    assert host_seen["existed"] is True
    # the house-renderer seam was fed ONLY the post's approved text
    assert seen["eyebrow"] == "WE DO THE HEAVY LIFTING"
    assert seen["headline"] == "We do the heavy lifting."  # first sentence of the caption
    assert seen["deck"] == "Your social, done for you."
    assert seen["is_story"] is False


def test_missing_image_hosts_real_feed_infographic_1080(monkeypatch, tmp_path):
    # End to end through the REAL house PIL renderer: a 1080x1080 feed card is produced
    # on disk and handed to the host; the hosted url is returned.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    host, host_seen = _capturing_host()
    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": ""}
    out = ncf.display_image_for(post, out_dir=str(tmp_path), host=host)
    assert out.startswith("https://cdn.example.com/hosted/")
    assert _img_size(host_seen["local_path"]) == (1080, 1080)


def test_missing_image_hosts_real_story_infographic_1080x1920(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    host, host_seen = _capturing_host()
    post = {"caption": "One login. Every result.",
            "pillar": "The portal", "format": "story", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path), host=host)
    assert out.startswith("https://cdn.example.com/hosted/")
    assert _img_size(host_seen["local_path"]) == (1080, 1920)


def test_tenant_scopes_hosting(monkeypatch, tmp_path):
    # media_host tenant isolation: the gym's key scopes the hosted card.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    host, host_seen = _capturing_host()
    post = {"caption": "Built by gym owners.", "pillar": "Proof", "format": "feed",
            "image_url": None, "account_key": "gym_alpha"}
    ncf.display_image_for(post, out_dir=str(tmp_path), host=host)
    assert host_seen["tenant"] == "gym_alpha"


def test_pillar_only_no_caption_still_hosts_from_approved_text(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    seen = {}

    def _fake(eyebrow, headline, deck, out_path, is_story=False):
        seen.update(headline=headline)
        with open(out_path, "wb") as fh:
            fh.write(b"X")
        return out_path

    host, _ = _capturing_host()
    post = {"caption": "", "pillar": "Sales are now", "format": "feed", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path), renderer=_fake, host=host)
    assert out.startswith("https://cdn.example.com/hosted/")
    assert seen["headline"] == "Sales are now"


# ---------------------------------------------------------------------------
# no caption / no pillar text -> None (block, no blank card, no fabrication)
# ---------------------------------------------------------------------------

def test_no_text_returns_none_no_render_no_host(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    calls = []

    def _r(*a, **k):
        calls.append("render"); return "X"

    def _h(*a, **k):
        calls.append("host"); return "Y"

    post = {"caption": "", "pillar": "", "format": "feed", "image_url": None}
    assert ncf.display_image_for(post, renderer=_r, host=_h) is None
    assert calls == []  # nothing rendered / hosted: no blank card, no fabricated copy


def test_whitespace_only_text_returns_none(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    post = {"caption": "   ", "pillar": "  ", "format": "feed", "image_url": None}
    assert ncf.display_image_for(post) is None


# ---------------------------------------------------------------------------
# host returns falsy / hosting disabled -> None (never a local path)
# ---------------------------------------------------------------------------

def test_host_returns_falsy_yields_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")

    def _host_none(local_path, tenant):
        return None  # hosting unavailable / upload failed

    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path), host=_host_none)
    assert out is None  # a clean empty state, never an unshowable local path


def test_hosting_disabled_default_host_yields_none(monkeypatch, tmp_path):
    # With the real default host and hosting OFF, host_media returns None, so the
    # fallback degrades to None rather than a local path.
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "false")
    assert config.hosting_enabled() is False
    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": None}
    out = ncf.display_image_for(post, out_dir=str(tmp_path))
    assert out is None


# ---------------------------------------------------------------------------
# render / font failure -> None (never raises into the caller / web request)
# ---------------------------------------------------------------------------

def test_render_failure_degrades_to_none_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")

    def _boom(*a, **k):
        raise OSError("cannot open resource: font missing")

    host, _ = _capturing_host()
    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": None}
    # must NOT raise; degrades to the empty state
    out = ncf.display_image_for(post, out_dir=str(tmp_path), renderer=_boom, host=host)
    assert out is None


def test_bundled_fonts_resolve_on_disk():
    # The brand fonts load from repo-bundled files (ship on every service), so a real
    # render never crashes on a missing font.
    from agent import summit_render as sr
    for p in (sr.ANTON, sr.OSWALD, sr.MONT):
        assert os.path.isfile(p), p


# ---------------------------------------------------------------------------
# idempotency: repeated reads of the same post reuse the hosted url
# ---------------------------------------------------------------------------

def test_repeated_reads_reuse_hosted_url_no_rerender(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    renders = {"n": 0}
    hosts = {"n": 0}

    def _r(eyebrow, headline, deck, out_path, is_story=False):
        renders["n"] += 1
        with open(out_path, "wb") as fh:
            fh.write(b"X")
        return out_path

    def _h(local_path, tenant):
        hosts["n"] += 1
        return "https://cdn.example.com/hosted/card.png"

    post = {"id": 42, "caption": "One login. Every result.", "pillar": "The portal",
            "format": "feed", "image_url": None}
    a = ncf.display_image_for(post, out_dir=str(tmp_path), renderer=_r, host=_h)
    b = ncf.display_image_for(post, out_dir=str(tmp_path), renderer=_r, host=_h)
    assert a == b == "https://cdn.example.com/hosted/card.png"
    assert renders["n"] == 1  # second read reused the cache, did not re-render
    assert hosts["n"] == 1    # nor re-host


def test_changed_caption_rerenders(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    renders = {"n": 0}

    def _r(eyebrow, headline, deck, out_path, is_story=False):
        renders["n"] += 1
        with open(out_path, "wb") as fh:
            fh.write(b"X")
        return out_path

    def _h(local_path, tenant):
        return f"https://cdn.example.com/hosted/{renders['n']}.png"

    p1 = {"id": 7, "caption": "First line.", "pillar": "Proof", "format": "feed",
          "image_url": None}
    p2 = {"id": 7, "caption": "A different line.", "pillar": "Proof", "format": "feed",
          "image_url": None}
    ncf.display_image_for(p1, out_dir=str(tmp_path), renderer=_r, host=_h)
    ncf.display_image_for(p2, out_dir=str(tmp_path), renderer=_r, host=_h)
    assert renders["n"] == 2  # a changed caption is a new card, re-rendered


# ---------------------------------------------------------------------------
# flag OFF -> no fallback even when image_url is missing (unchanged behavior)
# ---------------------------------------------------------------------------

def test_flag_off_no_fallback_even_when_image_missing(monkeypatch):
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "false")
    calls = []

    def _r(*a, **k):
        calls.append("render"); return "X"

    def _h(*a, **k):
        calls.append("host"); return "Y"

    post = {"caption": "One platform for your whole gym.",
            "pillar": "All in one offer", "format": "feed", "image_url": None}
    assert ncf.display_image_for(post, renderer=_r, host=_h) is None
    assert calls == []  # flag OFF: no render, no host, unchanged behavior


def test_flag_off_existing_image_still_returned(monkeypatch):
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
           "pillar": "All in one offer", "format": "feed", "gym_id": "gym_a",
           "image_url": "", "caption": "One platform for your whole gym."}
    post = ps._content_calendar_post(row)
    assert post["image_public_url"] == ""  # unchanged: no fallback with the flag OFF


def test_portal_hook_on_fills_hosted_display_image(monkeypatch):
    from agent import portal_social as ps
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    # inject a fake host through the module seam so the wiring test stays offline
    import agent.no_creative_fallback as ncfmod

    captured = {}

    def _fake_display(post, *, tenant=None):
        captured["tenant"] = tenant
        return "https://cdn.example.com/hosted/wired.png"

    monkeypatch.setattr(ncfmod, "display_image_for", _fake_display)
    row = {"id": 8, "post_date": "2026-08-11", "status": "pending",
           "pillar": "The portal", "format": "feed", "gym_id": "gym_b",
           "image_url": "", "caption": "One login. Every result."}
    post = ps._content_calendar_post(row)
    assert post["image_public_url"] == "https://cdn.example.com/hosted/wired.png"
    assert captured["tenant"] == "gym_b"  # gym scoped


def test_portal_hook_on_keeps_existing_image(monkeypatch):
    from agent import portal_social as ps
    monkeypatch.setenv("AGENT_NO_CREATIVE_FALLBACK", "true")
    row = {"id": 9, "post_date": "2026-08-12", "status": "pending",
           "pillar": "Proof", "format": "feed", "gym_id": "gym_c",
           "image_url": "https://cdn.example.com/hosted.png", "caption": "Built by owners."}
    post = ps._content_calendar_post(row)
    assert post["image_public_url"] == "https://cdn.example.com/hosted.png"
