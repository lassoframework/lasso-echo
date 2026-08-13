"""
Story-image formatter (AGENT_STORY_FORMAT, OFF by default). Offline: renders real
1080x1920 cards with Pillow (no network). Asserts size, no-dash caption law, wrapping,
cache, flag gate, and builder wiring (photo stories formatted, video stories left).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_image as si  # noqa: E402


def _photo(tmp_path, size=(1600, 900), name="p.jpg"):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", size, (70, 90, 110)).save(p, "JPEG")
    return str(p)


def test_story_caption_no_dashes_and_trimmed():
    cap = ("On Thursdays we wear pink - and it matters. Second sentence here too. "
           "Third one should be dropped.")
    out = si.story_caption(cap)
    assert "-" not in out and "–" not in out and "—" not in out
    assert "Third one" not in out          # capped at ~2 sentences
    assert out.startswith("On Thursdays")


def test_build_story_image_is_1080x1920(tmp_path):
    from PIL import Image
    out = si.build_story_image(_photo(tmp_path), str(tmp_path / "s.jpg"),
                               caption="Come train with us today.",
                               gym_name="CrossFit ENG")
    im = Image.open(out)
    assert im.size == (1080, 1920)
    # not blank: a real spread of brightness (photo + card + text)
    px = list(im.convert("L").getdata())[::5000]
    assert max(px) - min(px) > 40


def test_build_story_image_handles_portrait_and_no_caption(tmp_path):
    from PIL import Image
    out = si.build_story_image(_photo(tmp_path, size=(900, 1600)),
                               str(tmp_path / "s2.jpg"), caption="", gym_name="")
    assert Image.open(out).size == (1080, 1920)


def test_flag_off_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_STORY_FORMAT", raising=False)
    assert si.get_or_make_story_image(_photo(tmp_path), "cap", "ENG",
                                      str(tmp_path), logger=lambda m: None) is None


def test_cache_and_recaption(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    photo = _photo(tmp_path)
    a = si.get_or_make_story_image(photo, "Caption one.", "ENG", str(tmp_path),
                                   logger=lambda m: None)
    assert a and a.endswith("__story.jpg")
    b = si.get_or_make_story_image(photo, "Caption one.", "ENG", str(tmp_path),
                                   logger=lambda m: None)
    assert b == a                                   # cached
    c = si.get_or_make_story_image(photo, "A different caption.", "ENG",
                                   str(tmp_path), logger=lambda m: None)
    assert c != a                                   # new caption -> new card


def test_bad_photo_returns_none_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    bad = tmp_path / "notimage.jpg"
    bad.write_bytes(b"not a real jpeg")
    assert si.get_or_make_story_image(str(bad), "cap", "ENG", str(tmp_path),
                                      logger=lambda m: None) is None


# ---- builder wiring --------------------------------------------------------

def test_builder_formats_photo_story_not_video(tmp_path, monkeypatch):
    from agent import client_month_run as cmr

    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    from agent import story_image as _si, media_host as _mh
    monkeypatch.setattr(_si, "get_or_make_story_image",
                        lambda *a, **k: str(tmp_path / "card.jpg"))
    monkeypatch.setattr(_mh, "host_media",
                        lambda path, key: "https://r2/reels/card.jpg")

    class Acct:
        key = "eng_ig"
        display_name = "CrossFit ENG IG"

    class Feed:
        creative_path = str(tmp_path / "photo.jpg")
        caption = "Big class today."

    class Story:
        creative_public_url = "https://r2/raw/photo.jpg"

    story = Story()
    cmr._maybe_format_story(Acct(), story, Feed(), str(tmp_path), lambda m: None)
    assert story.creative_public_url == "https://r2/reels/card.jpg"

    # a VIDEO feed -> the story is left as-is (no photo formatting)
    class VFeed:
        creative_path = str(tmp_path / "clip.mp4")
        caption = "cap"

    vstory = Story()
    cmr._maybe_format_story(Acct(), vstory, VFeed(), str(tmp_path), lambda m: None)
    assert vstory.creative_public_url == "https://r2/raw/photo.jpg"


def test_display_name_strips_platform_suffix():
    from agent import client_month_run as cmr

    class A:
        display_name = "CrossFit ENG IG"
    assert cmr._display_name_for(A()) == "CrossFit ENG"
