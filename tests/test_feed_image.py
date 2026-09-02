"""
feed_image: auto-fit an OUT-OF-SPEC client feed photo into an in-spec 1080x1080 card
(Dale, 2026-08-18 "photo is not to size"). In-spec photos + videos are left untouched;
gated by AGENT_FEED_AUTOFIT; never raises.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import feed_image  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _photo(path, size):
    Image.new("RGB", size, (120, 130, 140)).save(path, "JPEG")
    return path


# ---- ratio gate ------------------------------------------------------------------------

def test_needs_autofit_flags_out_of_range_only():
    assert feed_image.needs_autofit(1080, 1080) is False    # square: in spec
    assert feed_image.needs_autofit(1080, 1350) is False    # 4:5: in spec (min)
    assert feed_image.needs_autofit(1080, 566) is False     # 1.91: in spec (max)
    assert feed_image.needs_autofit(1080, 1920) is True     # 9:16 story-tall: out
    assert feed_image.needs_autofit(2000, 600) is True      # panorama: out
    assert feed_image.needs_autofit(0, 100) is False        # invalid: never reframes


# ---- get_or_make_feed_image ------------------------------------------------------------

def test_flag_off_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_FEED_AUTOFIT", raising=False)
    p = _photo(str(tmp_path / "tall.jpg"), (1080, 1920))
    assert feed_image.get_or_make_feed_image(p, str(tmp_path)) is None


def test_in_spec_photo_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    p = _photo(str(tmp_path / "square.jpg"), (1080, 1080))
    assert feed_image.get_or_make_feed_image(p, str(tmp_path)) is None   # posts raw


def test_out_of_spec_photo_reframed_to_square(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    p = _photo(str(tmp_path / "tall.jpg"), (1080, 1920))
    out = feed_image.get_or_make_feed_image(p, str(tmp_path))
    assert out and os.path.isfile(out)
    with Image.open(out) as im:
        assert im.size == (1080, 1080)                       # in-spec square now
    # cached: a second call returns the same asset, no re-render
    assert feed_image.get_or_make_feed_image(p, str(tmp_path)) == out


def test_video_is_not_our_job(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"not a real video")
    assert feed_image.get_or_make_feed_image(str(vid), str(tmp_path)) is None


def test_unreadable_photo_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"\xff\xd8not-an-image")
    assert feed_image.get_or_make_feed_image(str(bad), str(tmp_path)) is None   # falls back


# ---- publish-time preflight from bytes (ENG/Dale 2026-08-24) ----------------

def _jpeg_bytes(w, h):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 60, 90)).save(buf, "JPEG")
    return buf.getvalue()


def test_make_feed_safe_from_bytes_reframes_too_tall(tmp_path):
    from PIL import Image
    out = str(tmp_path / "o.jpg")
    safe = feed_image.make_feed_safe_from_bytes(_jpeg_bytes(600, 1080), out)  # ratio 0.56
    assert safe == out and os.path.isfile(out)
    with Image.open(out) as im:
        assert im.size == (1080, 1080)                       # now in-spec


def test_make_feed_safe_from_bytes_leaves_in_spec_alone(tmp_path):
    out = str(tmp_path / "o.jpg")
    assert feed_image.make_feed_safe_from_bytes(_jpeg_bytes(1080, 1080), out) is None  # square OK
    assert feed_image.make_feed_safe_from_bytes(_jpeg_bytes(1080, 1350), out) is None  # 4:5 = 0.8 OK
    assert not os.path.isfile(out)


def test_make_feed_safe_from_bytes_never_raises_on_garbage(tmp_path):
    assert feed_image.make_feed_safe_from_bytes(b"\xff\xd8not-an-image",
                                                str(tmp_path / "o.jpg")) is None


# ---- the Drive lane (2026-09-02, The Bolton Club) -------------------------------------
# All 36 of Bolton's Drive photos logged "feed autofit failed ... FileNotFoundError;
# posting the raw photo": a Drive-sourced creative carries a creative_path in the record
# but nothing on disk at build time, so the reframe silently never ran for ANY Drive-lane
# gym and every out-of-spec photo fell through to the publish-time belt instead of the
# cached build path. _maybe_format_feed now localizes from the creative's OWN hosted url.

def test_maybe_format_feed_localizes_a_drive_creative_and_reframes_it(monkeypatch, tmp_path):
    import types
    from agent import client_month_run as cmr, config, media_localize

    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: True)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_localize, "cache_dir",
                        lambda subdir=media_localize.DEFAULT_SUBDIR: str(tmp_path))
    # the hosted bytes are a REAL out-of-spec image, so the reframe has something to do
    from PIL import Image
    src = tmp_path / "wide.jpg"
    Image.new("RGB", (1600, 600), "navy").save(src)
    raw = src.read_bytes()
    monkeypatch.setattr("agent.media_host.download_bytes", lambda u: raw)
    hosted_calls = []
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda p, tenant: hosted_calls.append(p) or "https://cdn/fit.jpg")

    feed = types.SimpleNamespace(
        creative_path="/data/content_library/theboltonclub/not_on_disk.jpg",
        creative_public_url="https://cdn.test/theboltonclub/real.jpg",
        thumbnail_url="")
    account = types.SimpleNamespace(key="theboltonclub_ig", display_name="The Bolton Club")
    logs = []
    cmr._maybe_format_feed(account, feed, str(tmp_path), logs.append)

    assert feed.creative_public_url == "https://cdn/fit.jpg", (
        "the reframed 1080x1080 card must replace the raw Drive photo")
    assert hosted_calls, "the reframe was hosted"
    assert not any("FileNotFoundError" in m for m in logs)


def test_maybe_format_feed_keeps_the_raw_photo_when_nothing_can_be_localized(
        monkeypatch, tmp_path):
    import types
    from agent import client_month_run as cmr, config, media_localize
    monkeypatch.setattr(config, "feed_autofit_enabled", lambda: True)
    monkeypatch.setattr(media_localize, "cache_dir",
                        lambda subdir=media_localize.DEFAULT_SUBDIR: str(tmp_path))
    monkeypatch.setattr("agent.media_host.download_bytes", lambda u: None)
    feed = types.SimpleNamespace(creative_path="/gone.jpg",
                                 creative_public_url="https://cdn.test/gone.jpg",
                                 thumbnail_url="")
    account = types.SimpleNamespace(key="g_ig", display_name="G")
    before = feed.creative_public_url
    cmr._maybe_format_feed(account, feed, str(tmp_path), lambda m: None)
    assert feed.creative_public_url == before, "ENHANCE-only: never drop or blank the post"
