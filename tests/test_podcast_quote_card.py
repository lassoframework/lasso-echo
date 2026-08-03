"""Tests for agent.podcast_quote_card - pure PIL quote card compositor (offline)."""

import os

import pytest
from PIL import Image

from agent import podcast_quote_card as qc


QUOTE = "Speed to lead is the whole game, most gyms wait a full day to reply."
GUEST = "Jane Doe"


# ---- CAPS emphasis helper (unit-tested directly) -------------------------------------
def test_caps_span_none_first_clause():
    caps, rest = qc.split_caps_emphasis(QUOTE, caps_span=None)
    assert caps == "SPEED TO LEAD IS THE WHOLE GAME"
    assert rest == "most gyms wait a full day to reply."


def test_caps_span_int_first_n_words():
    caps, rest = qc.split_caps_emphasis(QUOTE, caps_span=3)
    assert caps == "SPEED TO LEAD"
    assert rest.startswith("is the whole game")


def test_caps_span_str_prefix():
    caps, rest = qc.split_caps_emphasis("Your close rate is the first leg.",
                                        caps_span="Your close rate")
    assert caps == "YOUR CLOSE RATE"
    assert rest == "is the first leg."


def test_caps_span_no_clause_all_caps():
    caps, rest = qc.split_caps_emphasis("Do the work", caps_span=None)
    assert caps == "DO THE WORK"
    assert rest == ""


def test_caps_span_empty():
    assert qc.split_caps_emphasis("", None) == ("", "")


# ---- verbatim dash guard -------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "Speed to lead - the whole game",   # ASCII hyphen
    "Speed to lead — the whole game",  # em dash
    "Speed to lead – the whole game",  # en dash
])
def test_dash_bearing_quote_raises(tmp_path, bad):
    out = os.path.join(str(tmp_path), "bad.png")
    with pytest.raises(ValueError):
        qc.render_quote_card(bad, GUEST, 140, out)


# ---- render output -------------------------------------------------------------------
def test_render_produces_1080_png(tmp_path):
    out = os.path.join(str(tmp_path), "card.png")
    result = qc.render_quote_card(QUOTE, GUEST, 140, out)
    assert result == out
    assert os.path.isfile(out)
    with Image.open(out) as im:
        assert im.size == (1080, 1080)
        assert im.format == "PNG"


def test_render_cream_canvas(tmp_path):
    out = os.path.join(str(tmp_path), "cream.png")
    qc.render_quote_card(QUOTE, GUEST, 140, out, canvas="cream")
    with Image.open(out) as im:
        assert im.size == (1080, 1080)


def test_logo_actually_pasted(tmp_path):
    """The real wordmark must land on the card: rendering onto a blank navy canvas
    via _paste_wordmark must change the wordmark's own box (proving the real asset
    was composited, not that a fake logo was typeset)."""
    from PIL import Image as _Image
    canvas = _Image.new("RGB", (qc.SIZE, qc.SIZE), qc.NAVY)
    box = qc._paste_wordmark(canvas, "navy")
    x, y, w, h = box
    region = canvas.crop((x, y, x + w, y + h))
    colors = region.getcolors(maxcolors=100000) or []
    # More than one color in the wordmark region == the asset was composited there.
    assert len(colors) > 1
    # And some pixels are markedly lighter than the navy canvas (the white mark).
    lightest = max((sum(c) for _n, c in colors), default=0)
    assert lightest > sum(qc.NAVY) + 120


def test_logo_present_on_full_render(tmp_path):
    """Sanity: a full render still composites the wordmark (its box carries white
    pixels on the navy card)."""
    out = os.path.join(str(tmp_path), "full.png")
    qc.render_quote_card(QUOTE, GUEST, 140, out, canvas="navy")
    # Re-render the wordmark box coordinates against a fresh canvas to know where
    # it lands, then check the real card has light pixels there.
    from PIL import Image as _Image
    probe = _Image.new("RGB", (qc.SIZE, qc.SIZE), qc.NAVY)
    x, y, w, h = qc._paste_wordmark(probe, "navy")
    with Image.open(out) as im:
        region = im.convert("RGB").crop((x, y, x + w, y + h))
    lightest = max((sum(c) for _n, c in (region.getcolors(maxcolors=100000) or [])),
                   default=0)
    assert lightest > sum(qc.NAVY) + 120


def test_render_differs_between_quotes(tmp_path):
    """Different quotes produce different pixels (the text is actually drawn)."""
    a = os.path.join(str(tmp_path), "a.png")
    b = os.path.join(str(tmp_path), "b.png")
    qc.render_quote_card(QUOTE, GUEST, 140, a)
    qc.render_quote_card("Your close rate is the first leg to fix.", GUEST, 141, b)
    with Image.open(a) as ia, Image.open(b) as ib:
        assert ia.convert("RGB").tobytes() != ib.convert("RGB").tobytes()


def test_render_accepts_string_episode(tmp_path):
    out = os.path.join(str(tmp_path), "strep.png")
    qc.render_quote_card(QUOTE, GUEST, "140", out)
    assert os.path.isfile(out)
