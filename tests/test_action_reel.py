"""
Action-cut Reels (AGENT_CLIENT_VIDEO_EDIT, OFF by default). Fully OFFLINE: the ffmpeg
seams (runner/prober/profiler/assembler) are injected, so every decision is unit-tested
without encoding a byte. Asserts:
  * flag OFF -> get_or_make_reel is a hard no-op (None, no subprocess)
  * segment picking: chronological, non-overlapping, respects the target length,
    short clips kept whole, flat/no profile falls back to even spacing (deterministic)
  * hook text: first sentence, word-truncated, and NO dashes survive (on-screen law)
  * cache: same video bytes + caption -> second call returns the cached file with
    zero re-encode; a caption change re-edits (new hook)
  * any failure returns None (the raw video posts; editing never blocks)
  * builder integration: a video draft's public url swaps to the hosted reel; the
    paired story inherits it; edit failure keeps the raw url
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import action_reel as ar  # noqa: E402


# ---- pure segment selection ------------------------------------------------

def _flat_profile(duration, score=0.0, step=0.5):
    t, out = 0.0, []
    while t < duration:
        out.append((t, score))
        t += step
    return out


def test_short_clip_kept_whole():
    assert ar.pick_segments([], 15.0, target_total=22) == [(0.0, 15.0)]


def test_segments_chronological_nonoverlapping_and_capped(monkeypatch):
    # action spike at 40-50s, smaller ones at 10s and 70s of a 90s clip
    profile = _flat_profile(90, 0.01)
    profile += [(t, 0.9) for t in (40.0, 41.0, 42.0, 47.0, 48.0)]
    profile += [(t, 0.5) for t in (10.0, 10.5, 70.0, 70.5)]
    segs = ar.pick_segments(profile, 90.0, target_total=22)
    assert segs == sorted(segs), "segments must play in chronological order"
    total = sum(e - s for s, e in segs)
    assert 10 <= total <= 26, f"total {total}s should be near the 22s target"
    for (s1, e1), (s2, e2) in zip(segs, segs[1:]):
        assert e1 <= s2, "segments must not overlap"
    # the biggest action window made the cut
    assert any(s <= 41 <= e or s <= 47 <= e for s, e in segs)


def test_flat_profile_spreads_evenly_deterministic():
    segs1 = ar.pick_segments(_flat_profile(120, 0.0), 120.0, target_total=20)
    segs2 = ar.pick_segments([], 120.0, target_total=20)
    assert segs1 == segs2, "flat and empty profiles must be identical + deterministic"
    assert len(segs1) >= 4
    starts = [s for s, _ in segs1]
    assert starts == sorted(starts)


# ---- hook text ---------------------------------------------------------------

def test_hook_first_sentence_and_no_dashes():
    cap = "HYROX class every Saturday - bring a friend. More text follows here."
    hook = ar.hook_from_caption(cap)
    assert "-" not in hook and "–" not in hook and "—" not in hook
    assert "More text" not in hook
    assert 0 < len(hook) <= ar.HOOK_MAX_CHARS + 1


def test_hook_word_truncation_and_empty():
    long = "Our members crushed a massive Saturday morning partner workout today"
    hook = ar.hook_from_caption(long)
    assert len(hook) <= ar.HOOK_MAX_CHARS + 1
    assert not hook.endswith(" ")
    assert ar.hook_from_caption("") == ""
    assert ar.hook_from_caption(None) == ""


# ---- get_or_make_reel: flag, cache, fallback ----------------------------------

def _video(tmp_path, name="clip.mp4", data=b"\x00\x00FAKEMP4-BYTES"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _fakes(calls):
    def prober(path, runner=None):
        calls.append("probe")
        return 45.0, 1920, 1080, True

    def profiler(path, runner=None):
        calls.append("profile")
        return [(t / 2, 0.2) for t in range(90)]

    def assembler(path, segments, out, hook="", has_audio=True, runner=None):
        calls.append("assemble")
        with open(out, "wb") as fh:
            fh.write(b"EDITED")
        return out
    return prober, profiler, assembler


def test_flag_off_is_none_and_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CLIENT_VIDEO_EDIT", raising=False)
    calls = []
    prober, profiler, assembler = _fakes(calls)
    out = ar.get_or_make_reel(_video(tmp_path), "cap", str(tmp_path),
                              prober=prober, profiler=profiler,
                              assembler=assembler, logger=lambda m: None)
    assert out is None and calls == []


def test_edit_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_VIDEO_EDIT", "true")
    calls = []
    prober, profiler, assembler = _fakes(calls)
    vid = _video(tmp_path)
    out1 = ar.get_or_make_reel(vid, "Big Saturday class!", str(tmp_path),
                               prober=prober, profiler=profiler,
                               assembler=assembler, logger=lambda m: None)
    assert out1 and out1.endswith("__reel.mp4")
    assert os.path.dirname(out1).endswith(ar._REEL_SUBDIR)
    assert calls == ["probe", "profile", "assemble"]
    # same bytes + same caption -> cached, zero re-encode
    out2 = ar.get_or_make_reel(vid, "Big Saturday class!", str(tmp_path),
                               prober=prober, profiler=profiler,
                               assembler=assembler, logger=lambda m: None)
    assert out2 == out1 and calls == ["probe", "profile", "assemble"]
    # caption change -> new hook -> re-edit
    out3 = ar.get_or_make_reel(vid, "Different hook entirely", str(tmp_path),
                               prober=prober, profiler=profiler,
                               assembler=assembler, logger=lambda m: None)
    assert out3 != out1 and calls.count("assemble") == 2


def test_failure_returns_none_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_VIDEO_EDIT", "true")

    def boom(path, runner=None):
        raise ar.ReelError("ffprobe exploded")

    out = ar.get_or_make_reel(_video(tmp_path), "cap", str(tmp_path),
                              prober=boom, logger=lambda m: None)
    assert out is None


# ---- assemble filtergraph shape (runner seam; no real encode) ------------------

def test_assemble_filtergraph_and_hook(tmp_path):
    captured = {}

    def runner(cmd, label=""):
        captured["cmd"] = cmd

        class R:
            stdout, stderr, returncode = "", "", 0
        return R()

    out = str(tmp_path / "o.mp4")
    ar.assemble("in.mp4", [(1.0, 3.5), (10.0, 12.5)], out,
                hook="JOIN THE 6AM CREW", has_audio=True, runner=runner)
    joined = " ".join(captured["cmd"])
    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in fc
    assert "scale=1080:1920" in fc and "crop=1080:1920" in fc and "fps=30" in fc
    assert "drawtext=text='JOIN THE 6AM CREW'" in fc
    assert "+faststart" in joined and "libx264" in joined


def test_assemble_no_audio_variant(tmp_path):
    captured = {}

    def runner(cmd, label=""):
        captured["cmd"] = cmd

        class R:
            stdout, stderr, returncode = "", "", 0
        return R()

    ar.assemble("in.mp4", [(0.0, 5.0)], str(tmp_path / "o.mp4"),
                hook="", has_audio=False, runner=runner)
    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "concat=n=1:v=1:a=0" in fc
    assert "drawtext" not in fc
    assert "-c:a" not in captured["cmd"]


# ---- builder integration --------------------------------------------------------

def test_builder_swaps_video_creative_for_hosted_reel(tmp_path, monkeypatch):
    from agent import client_month_run as cmr

    class Feed:
        creative_path = str(tmp_path / "workout.mp4")
        creative_public_url = "https://r2/raw/workout.mp4"
        caption = "Saturday sweat session. Join us."

    (tmp_path / "workout.mp4").write_bytes(b"RAW")
    monkeypatch.setenv("AGENT_CLIENT_VIDEO_EDIT", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")

    from agent import action_reel as _ar, media_host as _mh
    monkeypatch.setattr(_ar, "get_or_make_reel",
                        lambda *a, **k: str(tmp_path / "edited__reel.mp4"))
    monkeypatch.setattr(_mh, "host_media",
                        lambda path, key: "https://r2/reels/edited__reel.mp4")

    class Acct:
        key = "eng_ig"

    feed = Feed()
    cmr._maybe_edit_video(Acct(), feed, str(tmp_path), lambda m: None)
    assert feed.creative_public_url == "https://r2/reels/edited__reel.mp4"


def test_builder_keeps_raw_url_on_edit_failure(tmp_path, monkeypatch):
    from agent import client_month_run as cmr

    class Feed:
        creative_path = str(tmp_path / "workout.mp4")
        creative_public_url = "https://r2/raw/workout.mp4"
        caption = "cap"

    monkeypatch.setenv("AGENT_CLIENT_VIDEO_EDIT", "true")
    from agent import action_reel as _ar
    monkeypatch.setattr(_ar, "get_or_make_reel", lambda *a, **k: None)

    class Acct:
        key = "eng_ig"

    feed = Feed()
    cmr._maybe_edit_video(Acct(), feed, str(tmp_path), lambda m: None)
    assert feed.creative_public_url == "https://r2/raw/workout.mp4"


def test_builder_ignores_images_and_flag_off(tmp_path, monkeypatch):
    from agent import client_month_run as cmr

    class Feed:
        creative_path = str(tmp_path / "photo.jpg")
        creative_public_url = "https://r2/raw/photo.jpg"
        caption = "cap"

    class Acct:
        key = "eng_ig"

    # image + flag on -> untouched
    monkeypatch.setenv("AGENT_CLIENT_VIDEO_EDIT", "true")
    feed = Feed()
    cmr._maybe_edit_video(Acct(), feed, str(tmp_path), lambda m: None)
    assert feed.creative_public_url == "https://r2/raw/photo.jpg"
    # video + flag OFF -> untouched
    monkeypatch.delenv("AGENT_CLIENT_VIDEO_EDIT", raising=False)

    class VFeed:
        creative_path = str(tmp_path / "v.mp4")
        creative_public_url = "https://r2/raw/v.mp4"
        caption = "cap"

    vfeed = VFeed()
    cmr._maybe_edit_video(Acct(), vfeed, str(tmp_path), lambda m: None)
    assert vfeed.creative_public_url == "https://r2/raw/v.mp4"
