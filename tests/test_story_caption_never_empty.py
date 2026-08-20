"""
ISSUE 2 (Dale, CrossFit ENG, 2026-08-15): "Today's story went out without a caption."
A client story publishes with an EMPTY body (zernio_publisher), so its caption MUST be
burned into the media. Photo stories became caption cards, but VIDEO stories were "left
alone" -> a raw video story with NO caption. Live evidence: the 2026-08-15 ENG story
image_url ended in .mp4 (a raw video), not a __story.jpg card.

Fix: story_image.get_or_make_story_video burns the caption onto a 9:16 story video, and
client_month_run._maybe_format_story DROPS any story that cannot carry its caption
(rather than shipping it captionless) whenever AGENT_STORY_FORMAT is on. Flag OFF keeps
the documented baseline unchanged.

Offline: the ffmpeg run is stubbed.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run, story_image  # noqa: E402


# ---- get_or_make_story_video: burns caption, gated, never raises ---------------

def _fake_video(tmp_path, name="20260811T165248Z_clip.mp4"):
    p = tmp_path / name
    p.write_bytes(b"fake-video-bytes")
    return str(p)


def test_story_video_off_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr(story_image.config, "story_format_enabled", lambda: False)
    assert story_image.get_or_make_story_video(
        _fake_video(tmp_path), "You are busy. We help.", "ENG", tmp_path) is None


def test_story_video_none_when_no_caption(monkeypatch, tmp_path):
    monkeypatch.setattr(story_image.config, "story_format_enabled", lambda: True)
    # empty caption -> no text to burn -> cannot caption the story video
    assert story_image.get_or_make_story_video(
        _fake_video(tmp_path), "", "ENG", tmp_path) is None


def test_story_video_burns_caption_and_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(story_image.config, "story_format_enabled", lambda: True)
    captured = {}

    def _fake_run(cmd, label="ffmpeg"):
        captured["cmd"] = cmd
        # the last arg is the output path; simulate ffmpeg writing it
        with open(cmd[-1], "wb") as fh:
            fh.write(b"rendered-story-video")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    out = story_image.get_or_make_story_video(
        _fake_video(tmp_path), "You are juggling too much. Energy is shot.",
        "CrossFit ENG", tmp_path, runner=_fake_run)
    assert out and out.endswith("__storyvid.mp4")
    assert os.path.isfile(out)
    # the caption text was burned in via drawtext, no dashes on screen
    vf = " ".join(captured["cmd"])
    assert "drawtext" in vf
    assert "1080:1920" in vf


def test_story_video_drawtext_has_no_dash():
    vf = story_image._story_video_drawtext(
        "You are strong. We coach you well.", "ENG")
    # the on-image copy law: no em/en/hyphen-as-dash characters
    for ch in ("—", "–", " - "):
        assert ch not in vf


def test_story_video_never_raises_on_bad_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(story_image.config, "story_format_enabled", lambda: True)

    def _boom(cmd, label="ffmpeg"):
        raise RuntimeError("ffmpeg not installed")

    assert story_image.get_or_make_story_video(
        _fake_video(tmp_path), "A real caption here.", "ENG", tmp_path,
        runner=_boom) is None


# ---- _maybe_format_story: keep vs DROP so a story is never captionless ----------

class _Draft:
    def __init__(self, caption, creative_path, url="raw-url"):
        self.caption = caption
        self.creative_path = creative_path
        self.creative_public_url = url
        self.thumbnail_url = ""


class _Acct:
    key = "eng_ig"
    display_name = "CrossFit ENG IG"


def test_maybe_format_story_flag_off_keeps_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "story_format_enabled", lambda: False)
    feed = _Draft("cap", str(tmp_path / "clip.mp4"))
    story = _Draft("cap", str(tmp_path / "clip.mp4"))
    # baseline: always keep, media untouched
    assert client_month_run._maybe_format_story(_Acct(), story, feed, tmp_path,
                                                lambda m: None) is True
    assert story.creative_public_url == "raw-url"


def test_maybe_format_story_drops_video_story_when_uncaptionable(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: True)
    # story video render fails -> None -> DROP (never ship captionless)
    monkeypatch.setattr(story_image, "get_or_make_story_video",
                        lambda *a, **k: None)
    feed = _Draft("You are busy. We help.", str(tmp_path / "20260811T_clip.mp4"))
    story = _Draft("You are busy. We help.", str(tmp_path / "20260811T_clip.mp4"))
    kept = client_month_run._maybe_format_story(_Acct(), story, feed, tmp_path,
                                                lambda m: None)
    assert kept is False


def test_maybe_format_story_keeps_captioned_video_story(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(story_image, "get_or_make_story_video",
                        lambda *a, **k: str(tmp_path / "card__storyvid.mp4"))
    import agent.media_host as media_host
    monkeypatch.setattr(media_host, "host_media", lambda p, k: "https://cdn/x__storyvid.mp4")
    feed = _Draft("You are busy. We help.", str(tmp_path / "clip.mp4"))
    story = _Draft("You are busy. We help.", str(tmp_path / "clip.mp4"))
    kept = client_month_run._maybe_format_story(_Acct(), story, feed, tmp_path,
                                                lambda m: None)
    assert kept is True
    assert story.creative_public_url == "https://cdn/x__storyvid.mp4"


def test_maybe_format_story_drops_photo_story_when_hosting_off(monkeypatch, tmp_path):
    # a captioned photo card that cannot be hosted must not fall back to the raw
    # (captionless) photo: drop it.
    monkeypatch.setattr(client_month_run.config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: False)
    monkeypatch.setattr(story_image, "get_or_make_story_image",
                        lambda *a, **k: str(tmp_path / "card__story.jpg"))
    feed = _Draft("You are busy. We help.", str(tmp_path / "photo.jpg"))
    story = _Draft("You are busy. We help.", str(tmp_path / "photo.jpg"))
    assert client_month_run._maybe_format_story(_Acct(), story, feed, tmp_path,
                                                lambda m: None) is False


# ---- _maybe_format_feed: reframe an out-of-spec feed photo, ENHANCE-only (never drop) ----

def test_maybe_format_feed_flag_off_keeps_raw(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "feed_autofit_enabled", lambda: False)
    feed = _Draft("cap", str(tmp_path / "p.jpg"))
    client_month_run._maybe_format_feed(_Acct(), feed, tmp_path, lambda m: None)
    assert feed.creative_public_url == "raw-url"          # untouched


def test_maybe_format_feed_in_spec_keeps_raw(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "feed_autofit_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: True)
    from agent import feed_image
    monkeypatch.setattr(feed_image, "get_or_make_feed_image",
                        lambda p, lib, logger=None: None)   # in-spec -> None
    feed = _Draft("cap", str(tmp_path / "square.jpg"))
    client_month_run._maybe_format_feed(_Acct(), feed, tmp_path, lambda m: None)
    assert feed.creative_public_url == "raw-url"          # posts the raw photo unchanged


def test_maybe_format_feed_swaps_when_reframed(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "feed_autofit_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: True)
    from agent import feed_image
    import agent.media_host as media_host
    monkeypatch.setattr(feed_image, "get_or_make_feed_image",
                        lambda p, lib, logger=None: str(tmp_path / "reframed__feed.jpg"))
    monkeypatch.setattr(media_host, "host_media", lambda p, k: "https://cdn/x__feed.jpg")
    feed = _Draft("cap", str(tmp_path / "tall.jpg"))
    client_month_run._maybe_format_feed(_Acct(), feed, tmp_path, lambda m: None)
    assert feed.creative_public_url == "https://cdn/x__feed.jpg"   # swapped to the reframe


def test_maybe_format_feed_never_drops_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "feed_autofit_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: True)
    from agent import feed_image
    def _boom(*a, **k):
        raise RuntimeError("render blew up")
    monkeypatch.setattr(feed_image, "get_or_make_feed_image", _boom)
    feed = _Draft("cap", str(tmp_path / "tall.jpg"))
    # must NOT raise and must keep the raw media (feed autofit never drops a post)
    client_month_run._maybe_format_feed(_Acct(), feed, tmp_path, lambda m: None)
    assert feed.creative_public_url == "raw-url"


# ---- video drawbox uses INPUT dims (the captionless-video root cause) -------------
def test_story_video_drawbox_uses_input_dims_not_bare_hw():
    """The drawbox band must size against the input frame (ih/iw). Bare h/w inside
    drawbox are self-referential (the box's own size), which failed the whole ffmpeg
    filtergraph in prod -> the video story burn returned None and published captionless
    (Dale, 2026-08-20). Lock the fix."""
    vf = story_image._story_video_drawtext("You are busy. We help.", "ENG")
    box = [f for f in vf.split(",") if f.startswith("drawbox")][0]
    assert "ih*" in box and "iw" in box, box
    # no bare h*/w* geometry left in the drawbox segment (self-referential = broken)
    assert "y=h*" not in box and "w=w:" not in box and "h=h*" not in box, box


# ---- photo vs infographic: caption a real upload, never an infographic -------------
def test_is_infographic_creative_detects_house_card():
    ig = _Draft("cap", "/lib/no_creative_get_stronger_story.png",
                url="https://cdn/no_creative_get_stronger_story.png")
    photo = _Draft("cap", "/lib/20260812T163147Z_Skierg.mp4",
                   url="https://cdn/20260812T163147Z_Skierg.mp4")
    assert client_month_run._is_infographic_creative(ig) is True
    assert client_month_run._is_infographic_creative(photo) is False
    # a REAL client upload whose own name contains 'infographic' must NOT be misread as
    # a house infographic (prefix-only detection): else its caption is skipped ->
    # captionless story, the exact bug. Real uploads are timestamp-prefixed.
    real_named = _Draft("cap", "/lib/20260812T163147Z_gym_infographic.jpg",
                        url="https://cdn/20260812T163147Z_gym_infographic.jpg")
    assert client_month_run._is_infographic_creative(real_named) is False


def test_maybe_format_story_skips_burn_for_infographic(monkeypatch, tmp_path):
    monkeypatch.setattr(client_month_run.config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(client_month_run.config, "hosting_enabled", lambda: True)
    # if the burn engine were called it would explode -> proves it is NOT called
    def _boom(*a, **k):
        raise AssertionError("infographic story must NOT be caption-burned")
    monkeypatch.setattr(story_image, "get_or_make_story_image", _boom)
    monkeypatch.setattr(story_image, "get_or_make_story_video", _boom)
    ig_url = "https://cdn/no_creative_get_stronger_story.png"
    feed = _Draft("Get stronger in Cape Coral.", "/lib/no_creative_get_stronger_story.png",
                  url=ig_url)
    story = _Draft("Get stronger in Cape Coral.", "/lib/no_creative_get_stronger_story.png",
                   url=ig_url)
    kept = client_month_run._maybe_format_story(_Acct(), story, feed, tmp_path,
                                                lambda m: None)
    assert kept is True                       # kept as-is
    assert story.creative_public_url == ig_url  # infographic media untouched (no burn)
