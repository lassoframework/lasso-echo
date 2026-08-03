"""Tests for the welcome-post template pipeline (offline, procedural backgrounds).

Covers: all 10 templates render 1080x1080; the two fill fields land; the logo safe
zone is >= 13% and stays calm; grade gate passes (single red accent, no banned copy);
backgrounds cache to the volume and re-fills do not re-pay; the Pro path is used when a
client is injected; slots.json v2 is well formed; no dashes anywhere on the cards.
"""
import json
import os

import pytest
from PIL import Image

from agent import welcome_templates as wt
from agent import welcome_review as wr


@pytest.fixture
def cache(tmp_path):
    return str(tmp_path / "cache")


def _out(tmp_path, name):
    return str(tmp_path / name)


def test_ten_templates_exist_and_ids_unique():
    ids = [t["id"] for t in wt.TEMPLATES]
    assert ids == [f"T{i}" for i in range(1, 11)]
    assert len(set(ids)) == 10


@pytest.mark.parametrize("tid", [f"T{i}" for i in range(1, 11)])
def test_make_welcome_renders_1080(tmp_path, cache, tid):
    out = _out(tmp_path, f"{tid}.png")
    path = wt.make_welcome(tid, "Iron Forge Fitness", "Jordan Blake", None,
                           out_path=out, cache_dir=cache)
    assert os.path.isfile(path)
    im = Image.open(path)
    assert im.size == (wt.SIZE, wt.SIZE)
    assert im.mode == "RGB"


@pytest.mark.parametrize("tid", [f"T{i}" for i in range(1, 11)])
def test_logo_zone_at_least_13_percent(tid):
    t = wt.get_template(tid)
    x, y, w, h = t["logo_zone"]
    frac = (w * h) / float(wt.SIZE * wt.SIZE)
    assert frac >= 0.13, f"{tid} logo zone only {frac:.3f} of canvas"
    # zone stays on canvas
    assert 0 <= x and 0 <= y and x + w <= wt.SIZE and y + h <= wt.SIZE


@pytest.mark.parametrize("tid", [f"T{i}" for i in range(1, 11)])
def test_calm_zone_on_procedural_background(tid, cache):
    t = wt.get_template(tid)
    bg_path, mode = wt.ensure_background(t, cache_dir=cache)
    assert mode == "placeholder"
    img = Image.open(bg_path).convert("RGB")
    assert wt.calm_zone_ok(img, t["logo_zone"]), f"{tid} logo zone not calm"


@pytest.mark.parametrize("tid", [f"T{i}" for i in range(1, 11)])
def test_grade_passes_blank_and_both_proofs(tmp_path, cache, tid):
    t = wt.get_template(tid)
    wide, square = wt.make_test_logos(str(tmp_path / "logos"))
    for label, logo in (("blank", None), ("wide", wide), ("square", square)):
        out = _out(tmp_path, f"{tid}_{label}.png")
        _p, _m, text = wt._render(t, "Iron Forge Fitness", "Jordan Blake", logo,
                                  out, cache_dir=cache)
        g = wr.grade_welcome(out, text, t)
        assert g["passed"], f"{tid} {label} failed grade: {g['failed']} " \
                            f"(red_regions={g['red_regions']})"


def test_fill_fields_present_in_on_card_text(tmp_path, cache):
    t = wt.get_template("T1")
    out = _out(tmp_path, "fill.png")
    _p, _m, text = wt._render(t, "Peak Performance Gym", "Sam Rivera", None,
                              out, cache_dir=cache)
    assert "PEAK PERFORMANCE GYM" in text
    assert "Sam Rivera" in text


def test_no_dashes_in_any_template_copy():
    # eyebrows, directions, headline, proof stat: no en/em dash or hyphen
    for t in wt.TEMPLATES:
        assert wr.no_banned_copy(t["eyebrow"]), t["id"]
    assert wr.no_banned_copy(wt.HEADLINE)
    assert wr.no_banned_copy(wt.PROOF_STAT)


def test_no_banned_copy_catches_dashes_and_vendor():
    assert not wr.no_banned_copy("best-in-class")   # hyphen
    assert not wr.no_banned_copy("great — deal")  # em dash
    assert not wr.no_banned_copy("your vendor here")  # banned word
    assert wr.no_banned_copy("Welcome to LASSO")


def test_background_cached_not_regenerated(cache):
    t = wt.get_template("T1")
    p1, _ = wt.ensure_background(t, cache_dir=cache)
    mtime1 = os.path.getmtime(p1)
    p2, _ = wt.ensure_background(t, cache_dir=cache)
    assert p1 == p2
    assert os.path.getmtime(p2) == mtime1  # reused, not rewritten


class _FakeProClient:
    """Returns a tiny solid-navy PNG as 'Pro' art; records the prompt it saw."""
    def __init__(self):
        self.prompts = []

    def generate_image(self, prompt, model):
        self.prompts.append((prompt, model))
        import io
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (18, 30, 60)).save(buf, format="PNG")
        return buf.getvalue()


def test_pro_client_path_used_when_present(cache):
    t = wt.get_template("T2")
    client = _FakeProClient()
    path, mode = wt.ensure_background(t, bg_client=client, cache_dir=cache)
    assert mode == "pro"
    assert path.endswith("_pro.png")
    assert os.path.isfile(path)
    assert Image.open(path).size == (wt.SIZE, wt.SIZE)  # normalized to canvas
    # prompt forbids text/logos and requests a calm zone
    prompt, model = client.prompts[0]
    assert "NO text" in prompt and "NO logos" in prompt
    assert "CALM" in prompt
    assert model == wt.config.NANO_MODEL


def test_background_prompt_forbids_text_for_all():
    for t in wt.TEMPLATES:
        p = wt.background_prompt(t)
        low = p.lower()
        assert "no text" in low and "no letters" in low and "no logos" in low
        assert "no dash" not in low  # not asserting our own copy rule into the art
        assert wr.no_banned_copy(t["direction"])


def test_slots_json_structure():
    d = wt.slots_dict()
    assert d["canvas"] == {"w": 1080, "h": 1080}
    assert d["fill_fields"] == ["gym_name", "owner_name"]
    assert set(d["templates"]) == {f"T{i}" for i in range(1, 11)}
    for tid, spec in d["templates"].items():
        z = spec["logo_zone"]
        assert z["fraction"] >= 0.13
        assert spec["text_column"] in ("left", "right")


def test_write_slots_json(tmp_path):
    p = wt.write_slots_json(str(tmp_path / "slots.json"))
    d = json.load(open(p))
    assert len(d["templates"]) == 10


def test_test_logos_generated(tmp_path):
    wide, square = wt.make_test_logos(str(tmp_path / "l"))
    assert os.path.isfile(wide) and os.path.isfile(square)
    assert Image.open(wide).size[0] > Image.open(wide).size[1]      # wide
    assert Image.open(square).size[0] == Image.open(square).size[1]  # square


def test_red_regions_single_for_word_accent(tmp_path, cache):
    t = wt.get_template("T1")  # accent == word (one red word)
    out = _out(tmp_path, "t1.png")
    wt._render(t, "Iron Forge", "Sam", None, out, cache_dir=cache)
    assert wr.red_regions(Image.open(out), mask_zone=t["logo_zone"]) == 1


def test_gym_logo_red_not_counted_as_accent(tmp_path, cache):
    # a gym logo containing red must NOT trip the single-accent rule
    t = wt.get_template("T1")
    wide, _ = wt.make_test_logos(str(tmp_path / "l"))  # has a red dot
    out = _out(tmp_path, "t1logo.png")
    _p, _m, text = wt._render(t, "Iron Forge", "Sam", wide, out, cache_dir=cache)
    g = wr.grade_welcome(out, text, t)
    assert g["passed"]
    assert g["red_regions"] == 1  # only the headline word, logo zone excluded


def test_ocr_clean_procedural_background(cache):
    t = wt.get_template("T6")
    bg_path, _ = wt.ensure_background(t, cache_dir=cache)
    res = wr.ocr_clean(bg_path, vision_client=None)
    assert res["clean"] is True


def test_unknown_template_raises():
    with pytest.raises(KeyError):
        wt.get_template("T99")


@pytest.mark.parametrize("tid", [f"T{i}" for i in range(1, 11)])
def test_no_text_logo_overlap_all_templates(tid):
    # the text column must not overlap the logo zone (the T5-collision regression)
    assert wr._no_text_logo_overlap(wt.get_template(tid))


def test_overlap_guard_catches_a_collision():
    # a centered zone defeats the opposite-side column and collides; the guard
    # (and therefore the grade) must catch it
    bad = dict(wt.get_template("T1"))
    bad["logo_zone"] = (330, 300, 420, 400)  # the old, broken T5 zone
    assert not wr._no_text_logo_overlap(bad)


def test_calm_zone_fails_on_noisy_region():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (wt.SIZE, wt.SIZE), (18, 30, 60))
    d = ImageDraw.Draw(img)
    zone = wt.get_template("T1")["logo_zone"]
    x, y, w, h = zone
    # high-contrast noise stripes inside the zone -> not calm
    for i in range(0, w, 8):
        d.rectangle([x + i, y, x + i + 4, y + h], fill=(255, 255, 255))
    assert not wt.calm_zone_ok(img, zone)
