"""Tests for agent/summit_render.py, the shared PIL primitives that
welcome_templates.py and podcast_quote_card.py depend on. This module was
missing entirely from every branch's history (a hallucinated-then-forgotten
dependency) and broke test collection for both consumers; these tests pin its
public contract now that it exists."""

from PIL import Image, ImageDraw

from agent import summit_render as sr


def test_palette_is_rgb_tuples_not_hex():
    for color in (sr.NAVY, sr.RED, sr.CREAM, sr.SKY, sr.WHITE,
                 sr.MUTE_CREAM, sr.MUTE_NAVY):
        assert isinstance(color, tuple) and len(color) == 3
        assert all(isinstance(c, int) and 0 <= c <= 255 for c in color)


def test_palette_matches_house_hex():
    assert sr.NAVY == (18, 30, 60)       # #121E3C
    assert sr.RED == (255, 0, 0)          # #FF0000
    assert sr.CREAM == (250, 246, 240)    # #FAF6F0
    assert sr.SKY == (94, 185, 230)       # #5EB9E6


def test_color_concatenates_with_alpha():
    # welcome_templates/_podcast_quote_card rely on this: `tone + (255,)`
    assert sr.NAVY + (255,) == (18, 30, 60, 255)


def test_size_and_margin():
    assert sr.SIZE == 1080
    assert sr.MARGIN == 96


def test_font_paths_load(tmp_path):
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for font_path in (sr.ANTON, sr.OSWALD, sr.MONT):
        font = sr._f(font_path, 40)
        # must be usable for measurement even if the asset is missing (falls
        # back to the PIL bitmap default rather than raising)
        assert sr._tw(d, "HELLO", font) > 0


def test_font_cache_returns_same_object():
    a = sr._f(sr.ANTON, 50)
    b = sr._f(sr.ANTON, 50)
    assert a is b


def test_wrap_splits_long_text_into_multiple_lines():
    img = Image.new("RGB", (100, 100))
    d = ImageDraw.Draw(img)
    font = sr._f(sr.MONT, 40)
    lines = sr._wrap(d, "one two three four five six seven eight", font, 150)
    assert len(lines) > 1
    assert " ".join(lines).split() == "one two three four five six seven eight".split()


def test_wrap_empty_text_returns_one_empty_line():
    img = Image.new("RGB", (100, 100))
    d = ImageDraw.Draw(img)
    font = sr._f(sr.MONT, 20)
    assert sr._wrap(d, "", font, 100) == [""]


def test_tracked_width_matches_tracked_draw():
    img = Image.new("RGB", (400, 100), (0, 0, 0))
    d = ImageDraw.Draw(img)
    font = sr._f(sr.OSWALD, 30)
    w = sr._tracked_w(d, "ABC", font, tracking=5)
    end_x = sr._tracked(d, (10, 10), "ABC", font, (255, 255, 255), tracking=5)
    assert end_x - 10 == w


def test_th_positive_for_nonempty_text():
    img = Image.new("RGB", (100, 100))
    d = ImageDraw.Draw(img)
    font = sr._f(sr.ANTON, 60)
    assert sr._th(d, "AY", font) > 0
    assert sr._th(d, "", font) == 0
