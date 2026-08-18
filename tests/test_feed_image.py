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
