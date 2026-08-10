"""
BOLD SUMMIT photo-background compositing tests. Offline: no live network.

Blake ruling: each bold summit card should sit OVER a real event-scene PHOTO
(gym owners at a summit in athleisure), not the flat dark base. The photo is
cover-cropped to the exact card size, a two-band BOLD_BG scrim is composited for
legibility, and the EXISTING bold overlay (accent rail, oversized ANTON headline
with the one accent word, numeric callouts, the dash-free event lockup, the
PRESENTED WITH sponsor strip) draws ON TOP. When no background is provided the
flat BOLD_BG behavior is unchanged, and a missing/failed background never crashes.

Covers:
  - feed / story composited over a provided bg: exact sizes, event lockup +
    headline present, the photo shows through under the mid scrim, the top band is
    scrimmed dark so cream text reads;
  - background None -> flat BOLD_BG (unchanged bold behavior);
  - background dir empty / missing -> None path (no crash), same as flat dark;
  - selection is stable per concept and varies across concepts; feed bgs come from
    feed/, story bgs from story/;
  - text legibility invariant: the headline band mean luminance stays low.
"""

import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import summit_render as sr  # noqa: E402
from agent.summit_rebuild import SUMMIT_CONCEPTS  # noqa: E402

_CONCEPT = SUMMIT_CONCEPTS[0]  # 01_invitation


# ---- helpers ---------------------------------------------------------------
def _make_photo(path, w, h, color):
    """A tiny throwaway solid-color 'photo' background. A flat vivid color is the
    cleanest probe: after the mid scrim it is neither pure BOLD_BG nor pure text, so
    we can prove the photo shows through."""
    Image.new("RGB", (w, h), color).save(path)
    return str(path)


def _mean_luma(im, box):
    """Mean luminance of a crop (Rec.601 via PIL 'L'). Low = dark = cream text reads."""
    from PIL import ImageStat
    lum = im.crop(box).convert("L")
    return ImageStat.Stat(lum).mean[0]


def _near(c, target, tol):
    return all(abs(a - b) <= tol for a, b in zip(c, target))


# A vivid green photo: distinct from BOLD_BG (dark navy), BOLD_INK (cream),
# and BOLD_ACCENT (orange), so "shows through" is unambiguous.
PHOTO = (40, 200, 90)


# ---- feed over a provided bg -----------------------------------------------
def test_feed_over_bg_is_1080_square(tmp_path):
    bg = _make_photo(tmp_path / "p.png", 1600, 1200, PHOTO)
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "feed.png"), background_path=bg)
    assert Image.open(p).size == (1080, 1080)


def test_feed_over_bg_photo_shows_through_middle(tmp_path):
    """A mid-card pixel is tinted by the photo: neither pure BOLD_BG nor pure text.
    The green channel is clearly lifted by the photo under the ~41% mid scrim."""
    bg = _make_photo(tmp_path / "p.png", 1600, 1200, PHOTO)
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "feed.png"), background_path=bg)
    im = Image.open(p).convert("RGB")
    # sample a middle-left region clear of headline glyphs and the accent rail
    c = im.getpixel((sr.SIZE // 2, int(sr.SIZE * 0.55)))
    assert not _near(c, sr.BOLD_BG, 14), f"mid pixel {c} is pure flat base (no photo)"
    assert not _near(c, sr.BOLD_INK, 24), f"mid pixel {c} is pure text ink"
    # the photo's green tint is present (green above red/blue), proving it shows through
    assert c[1] > c[0] and c[1] > c[2], f"mid pixel {c} carries no green photo tint"


def test_feed_over_bg_flat_base_differs_from_bg_render(tmp_path):
    """The composited-over-photo card differs from the flat-dark card (the bg is
    actually used, not silently dropped)."""
    bg = _make_photo(tmp_path / "p.png", 1600, 1200, PHOTO)
    over = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "over.png"), background_path=bg)
    flat = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "flat.png"))
    assert Image.open(over).tobytes() != Image.open(flat).tobytes()


def test_feed_over_bg_top_band_is_scrimmed_dark(tmp_path):
    """Text legibility invariant: the top headline band mean luminance stays LOW so
    cream/white text reads, and a top pixel is close to BOLD_BG under the strong scrim."""
    bg = _make_photo(tmp_path / "p.png", 1600, 1200, PHOTO)
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "feed.png"), background_path=bg)
    im = Image.open(p).convert("RGB")
    # a near-top pixel to the right of the accent rail, above the eyebrow/headline
    c = im.getpixel((sr.SIZE - 40, 12))
    assert _near(c, sr.BOLD_BG, 40), f"top band pixel {c} not scrimmed toward the base"
    # the headline zone (held under the strong scrim) stays dark (mean luma low) so
    # cream text stays legible. Probe the strong-hold zone (~12% to ~28%), clear of
    # the eased middle where the photo is intentionally allowed to show through.
    band = _mean_luma(im, (30, int(sr.SIZE * 0.12), sr.SIZE - 30, int(sr.SIZE * 0.28)))
    assert band < 80, f"headline band mean luma {band:.1f} too bright for cream text"


def test_feed_over_bg_still_has_lockup_and_headline(tmp_path):
    """Blanking the event lockup + the headline changes the pixels, proving both the
    lockup and the oversized headline are still composited on top of the photo."""
    bg = _make_photo(tmp_path / "p.png", 1600, 1200, PHOTO)
    normal = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "n.png"), background_path=bg)
    n_bytes = Image.open(normal).tobytes()

    orig_lock = sr.EVENT_LOCKUP
    try:
        sr.EVENT_LOCKUP = ("", "", "")
        blanked = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "bl.png"),
                                      background_path=bg)
    finally:
        sr.EVENT_LOCKUP = orig_lock
    assert n_bytes != Image.open(blanked).tobytes(), "event lockup not drawn over photo"

    blank_head = dict(_CONCEPT)
    blank_head["headline"] = ""
    blank_head["red_word"] = ""
    nohead = sr.render_bold_feed(blank_head, "a", str(tmp_path / "nh.png"),
                                 background_path=bg)
    assert n_bytes != Image.open(nohead).tobytes(), "headline not drawn over photo"


def test_feed_over_bg_accent_rail_present(tmp_path):
    """The loud accent side rail stays over a photo (left edge carries the accent)."""
    bg = _make_photo(tmp_path / "p.png", 1600, 1200, PHOTO)
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "feed.png"), background_path=bg)
    im = Image.open(p).convert("RGB")
    assert _near(im.getpixel((6, sr.SIZE // 2)), sr.BOLD_ACCENT, 14), "accent rail lost"


# ---- story over a provided bg ----------------------------------------------
def test_story_over_bg_is_1080x1920(tmp_path):
    bg = _make_photo(tmp_path / "p.png", 1200, 2000, PHOTO)
    p = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "story.png"), background_path=bg)
    assert Image.open(p).size == (1080, 1920)


def test_story_over_bg_photo_shows_through_and_top_scrimmed(tmp_path):
    bg = _make_photo(tmp_path / "p.png", 1200, 2000, PHOTO)
    p = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "story.png"), background_path=bg)
    im = Image.open(p).convert("RGB")
    mid = im.getpixel((sr.STORY_W // 2, int(sr.STORY_H * 0.55)))
    assert not _near(mid, sr.BOLD_BG, 14), f"story mid {mid} shows no photo"
    assert mid[1] > mid[0] and mid[1] > mid[2], f"story mid {mid} lacks green tint"
    # a top pixel in the strong-hold headline zone is scrimmed close to the base
    top = im.getpixel((sr.STORY_W - 40, int(sr.STORY_H * 0.06)))
    assert _near(top, sr.BOLD_BG, 40), f"story top {top} not scrimmed dark"


def test_story_over_bg_still_has_lockup(tmp_path):
    bg = _make_photo(tmp_path / "p.png", 1200, 2000, PHOTO)
    normal = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "n.png"), background_path=bg)
    n_bytes = Image.open(normal).tobytes()
    orig = sr.EVENT_LOCKUP
    try:
        sr.EVENT_LOCKUP = ("", "", "")
        blanked = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "bl.png"),
                                       background_path=bg)
    finally:
        sr.EVENT_LOCKUP = orig
    assert n_bytes != Image.open(blanked).tobytes(), "story lockup not drawn over photo"


# ---- background None -> flat BOLD_BG, unchanged -----------------------------
def test_feed_none_bg_is_flat_dark_unchanged(tmp_path):
    """No bg -> the dominant color is the flat BOLD_BG base (unchanged bold behavior)."""
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "flat.png"))
    im = Image.open(p).convert("RGB")
    counts = {c: n for n, c in im.getcolors(maxcolors=1_000_000)}
    dominant = max(counts, key=counts.get)
    assert _near(dominant, sr.BOLD_BG, 18), f"flat dominant {dominant} not BOLD_BG"


def test_story_none_bg_is_flat_dark_unchanged(tmp_path):
    p = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "flat.png"))
    im = Image.open(p).convert("RGB")
    counts = {c: n for n, c in im.getcolors(maxcolors=1_000_000)}
    dominant = max(counts, key=counts.get)
    assert _near(dominant, sr.BOLD_BG, 18), f"flat story dominant {dominant} not BOLD_BG"


# ---- background dir: selection, empty, missing -----------------------------
def _seed_bg_dir(root, feed_names, story_names, color=PHOTO):
    feed = os.path.join(root, "feed")
    story = os.path.join(root, "story")
    os.makedirs(feed, exist_ok=True)
    os.makedirs(story, exist_ok=True)
    for n in feed_names:
        Image.new("RGB", (1600, 1200), color).save(os.path.join(feed, n))
    for n in story_names:
        Image.new("RGB", (1200, 2000), color).save(os.path.join(story, n))
    return root


def test_empty_bg_dir_falls_back_to_flat_no_crash(tmp_path):
    """An empty (or subdir-less) bg dir resolves to None -> flat dark, never crashes."""
    root = str(tmp_path / "bg_empty")
    os.makedirs(os.path.join(root, "feed"), exist_ok=True)  # present but empty
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "f.png"), background_dir=root)
    im = Image.open(p).convert("RGB")
    counts = {c: n for n, c in im.getcolors(maxcolors=1_000_000)}
    assert _near(max(counts, key=counts.get), sr.BOLD_BG, 18)


def test_missing_bg_dir_falls_back_to_flat_no_crash(tmp_path):
    """A dir that does not exist -> None -> flat dark; must not raise."""
    missing = str(tmp_path / "does_not_exist")
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "f.png"), background_dir=missing)
    assert Image.open(p).size == (1080, 1080)


def test_corrupt_bg_never_crashes_falls_back(tmp_path):
    """A corrupt/unreadable background file must fall back to flat dark, never crash."""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a real image")
    p = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "f.png"),
                            background_path=str(bad))
    im = Image.open(p).convert("RGB")
    counts = {c: n for n, c in im.getcolors(maxcolors=1_000_000)}
    assert _near(max(counts, key=counts.get), sr.BOLD_BG, 18)


def test_selection_is_stable_per_concept(tmp_path):
    """A given concept always resolves to the same background across calls."""
    root = _seed_bg_dir(str(tmp_path / "bg"),
                        ["a.png", "b.png", "c.png", "d.png"],
                        ["a.png", "b.png", "c.png", "d.png"])
    first = sr._pick_summit_bg(_CONCEPT["id"], root, "feed")
    for _ in range(5):
        assert sr._pick_summit_bg(_CONCEPT["id"], root, "feed") == first


def test_selection_varies_across_concepts(tmp_path):
    """Different concepts spread across the available backgrounds (not all identical)."""
    root = _seed_bg_dir(str(tmp_path / "bg"),
                        [f"f{i}.png" for i in range(6)],
                        [f"s{i}.png" for i in range(6)])
    picks = {c["id"]: sr._pick_summit_bg(c["id"], root, "feed") for c in SUMMIT_CONCEPTS}
    assert len(set(picks.values())) > 1, "every concept picked the same background"


def test_feed_selects_from_feed_subdir_story_from_story_subdir(tmp_path):
    """Feed bgs come from feed/, story bgs from story/."""
    root = _seed_bg_dir(str(tmp_path / "bg"), ["only_feed.png"], ["only_story.png"])
    fp = sr._pick_summit_bg(_CONCEPT["id"], root, "feed")
    sp = sr._pick_summit_bg(_CONCEPT["id"], root, "story")
    assert fp is not None and os.path.basename(fp) == "only_feed.png"
    assert sp is not None and os.path.basename(sp) == "only_story.png"
    assert os.path.dirname(fp).endswith(os.path.join("bg", "feed"))
    assert os.path.dirname(sp).endswith(os.path.join("bg", "story"))


def test_render_bold_feed_via_dir_composites_over_photo(tmp_path):
    """The public entry threads a bg dir -> the feed composites over the selected photo
    (differs from the flat card)."""
    root = _seed_bg_dir(str(tmp_path / "bg"), ["only.png"], ["only.png"])
    over = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "over.png"),
                               background_dir=root)
    flat = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "flat.png"))
    assert Image.open(over).tobytes() != Image.open(flat).tobytes()


def test_render_bold_story_via_dir_composites_over_photo(tmp_path):
    root = _seed_bg_dir(str(tmp_path / "bg"), ["only.png"], ["only.png"])
    over = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "over.png"),
                               background_dir=root)
    flat = sr.render_bold_story(_CONCEPT, "a", str(tmp_path / "flat.png"))
    assert Image.open(over).tobytes() != Image.open(flat).tobytes()


def test_explicit_path_wins_over_dir(tmp_path):
    """An explicit background_path takes precedence over background_dir selection."""
    root = _seed_bg_dir(str(tmp_path / "bg"), ["dir.png"], ["dir.png"], color=(10, 10, 10))
    explicit = _make_photo(tmp_path / "explicit.png", 1600, 1200, PHOTO)
    a = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "a.png"),
                            background_path=explicit, background_dir=root)
    b = sr.render_bold_feed(_CONCEPT, "a", str(tmp_path / "b.png"),
                            background_path=explicit)
    assert Image.open(a).tobytes() == Image.open(b).tobytes()
