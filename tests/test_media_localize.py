"""
media_localize tests (agent/media_localize.py), fully offline — no network, no R2.

Why this module exists (2026-09-02): two build lanes needed the same thing and both broke
the same way on a Drive-lane creative, which carries a hosted url but NO durable local path
at build time.

  * feed autofit: all 36 of The Bolton Club's Drive photos logged "feed autofit failed ...
    FileNotFoundError; posting the raw photo". The reframe silently never ran for ANY
    Drive-lane gym.
  * the Google Business mirror, which needs a local still to crop to 1200x900.

And the trap that makes a naive fix worse than useless: media_host._build_key is
echo/<tenant>/<sha1-of-bytes>/<basename>, so a random temp basename changes the R2 key on
every build, defeating content dedupe and writing a brand new object for byte-identical
pixels forever. The deterministic name is the point, not a detail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import media_localize as ml  # noqa: E402


# ---- the deterministic name (the R2-key trap) ----------------------------------------

def test_the_same_url_always_yields_the_same_filename():
    a = ml.stable_name("https://cdn.test/gym/photo-one.jpg", ".jpg")
    b = ml.stable_name("https://cdn.test/gym/photo-one.jpg", ".jpg")
    assert a == b, "a stable basename is what keeps the R2 content key stable across builds"
    assert "tmp" not in a and a.startswith("src_") and a.endswith(".jpg")


def test_different_urls_do_not_collide():
    assert ml.stable_name("https://cdn.test/a.jpg", ".jpg") != \
        ml.stable_name("https://cdn.test/b.jpg", ".jpg")


def test_extension_comes_from_the_hint_then_the_url_then_defaults():
    assert ml.stable_name("https://cdn.test/x", ".png").endswith(".png")
    assert ml._ext_for("https://cdn.test/x.webp") == ".webp"
    assert ml._ext_for("https://cdn.test/x.webp", ".png") == ".png"
    assert ml._ext_for("https://cdn.test/no-extension-here") == ".jpg"
    # a query string must not be read as part of the extension
    assert ml._ext_for("https://cdn.test/x.png?sig=abc123") == ".png"


# ---- local_copy ------------------------------------------------------------------------

def test_it_downloads_once_and_reuses_the_cached_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))
    calls = []
    dl = lambda u: calls.append(u) or b"realbytes"  # noqa: E731
    url = "https://cdn.test/gym/photo.jpg"
    first = ml.local_copy(url, downloader=dl, logger=lambda m: None)
    second = ml.local_copy(url, downloader=dl, logger=lambda m: None)
    assert first == second
    assert open(first, "rb").read() == b"realbytes"
    assert len(calls) == 1, "the second build must reuse the cache, not re-fetch"


def test_a_partial_write_is_never_left_behind_for_the_next_build(monkeypatch, tmp_path):
    # the write goes through a .part file + os.replace, so a killed build cannot leave a
    # truncated image that the next build then tries to decode.
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))

    def _boom(_u):
        raise RuntimeError("connection dropped mid-download")

    assert ml.local_copy("https://cdn.test/x.jpg", downloader=_boom,
                         logger=lambda m: None) is None
    assert [p.name for p in tmp_path.iterdir() if not p.name.endswith(".part")] == []


def test_an_empty_download_is_none_not_an_empty_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))
    assert ml.local_copy("https://cdn.test/x.jpg", downloader=lambda u: b"",
                         logger=lambda m: None) is None
    assert list(tmp_path.iterdir()) == []


def test_a_video_url_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))
    called = []
    for u in ("https://cdn.test/clip.mp4", "https://cdn.test/clip.mov?x=1"):
        assert ml.local_copy(u, downloader=lambda x: called.append(x) or b"x",
                             logger=lambda m: None) is None
    assert called == [], "callers here want a still; a video is never fetched"


def test_a_non_http_or_empty_url_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))
    for u in ("", None, "file:///etc/passwd", "not a url", "ftp://cdn.test/x.jpg"):
        assert ml.local_copy(u, downloader=lambda x: b"x", logger=lambda m: None) is None


def test_subdirs_keep_lanes_separate(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR:
                        seen.append(subdir) or str(tmp_path))
    ml.local_copy("https://cdn.test/x.jpg", subdir="gbp_mirror_src",
                  downloader=lambda u: b"x", logger=lambda m: None)
    assert seen == ["gbp_mirror_src"]


# ---- local_source_for: the one call a build lane makes --------------------------------

def test_a_real_local_file_is_used_directly_and_nothing_is_fetched(monkeypatch, tmp_path):
    real = tmp_path / "on_disk.jpg"
    real.write_bytes(b"local")
    calls = []
    got = ml.local_source_for(str(real), "https://cdn.test/other.jpg",
                              downloader=lambda u: calls.append(u) or b"x",
                              logger=lambda m: None)
    assert got == str(real)
    assert calls == [], "a creative that really is on disk must never be re-downloaded"


def test_a_drive_creative_with_a_claimed_path_that_is_not_on_disk_falls_back_to_the_url(
        monkeypatch, tmp_path):
    """THE Bolton Club case: the record carries a creative_path, the file is not there, and
    the hosted url is the only real source. Before this, the caller raised
    FileNotFoundError and silently posted the raw photo."""
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))
    got = ml.local_source_for("/data/content_library/theboltonclub/gone.jpg",
                              "https://cdn.test/gym/real.jpg",
                              downloader=lambda u: b"drivebytes",
                              logger=lambda m: None)
    assert got and os.path.isfile(got)
    assert open(got, "rb").read() == b"drivebytes"


def test_no_path_and_no_url_is_none():
    assert ml.local_source_for("", "", logger=lambda m: None) is None
    assert ml.local_source_for(None, None, logger=lambda m: None) is None


def test_the_claimed_paths_extension_is_carried_onto_the_localized_copy(
        monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "cache_dir", lambda subdir=ml.DEFAULT_SUBDIR: str(tmp_path))
    got = ml.local_source_for("/gone/photo.png", "https://cdn.test/x",
                              downloader=lambda u: b"x", logger=lambda m: None)
    assert got.endswith(".png")
