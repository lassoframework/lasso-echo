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


# ==========================================================================
# STORY FORMAT (9:16) — same design system, native tall background
# ==========================================================================

ALL = [f"T{i}" for i in range(1, 11)]


@pytest.mark.parametrize("tid", ALL)
def test_story_renders_1080x1920(tmp_path, cache, tid):
    out = _out(tmp_path, f"{tid}_story.png")
    path = wt.make_welcome(tid, "Iron Forge Fitness", "Jordan Blake", None,
                           format="story", out_path=out, cache_dir=cache)
    assert Image.open(path).size == (wt.STORY_W, wt.STORY_H) == (1080, 1920)


def test_feed_still_1080_square_default_format(tmp_path, cache):
    # the format param defaults to feed and feed dims are unchanged
    out = _out(tmp_path, "feed.png")
    path = wt.make_welcome("T1", "Iron Forge", "Sam", None, out_path=out,
                           cache_dir=cache)
    assert Image.open(path).size == (wt.SIZE, wt.SIZE)


@pytest.mark.parametrize("tid", ALL)
def test_story_logo_zone_at_least_13_percent(tid):
    z = wt.story_zone(wt.get_template(tid))
    frac = (z[2] * z[3]) / float(wt.STORY_W * wt.STORY_H)
    assert frac >= 0.13


@pytest.mark.parametrize("tid", ALL)
def test_story_logo_zone_in_safe_band_and_middle_third(tid):
    zx, zy, zw, zh = wt.story_zone(wt.get_template(tid))
    # inside the 15..85% safe band (also clears the top/bottom 250px platform UI)
    assert zy >= wt.STORY_SAFE_TOP >= 250
    assert zy + zh <= wt.STORY_SAFE_BOTTOM <= wt.STORY_H - 250
    # centered horizontally and sitting in the visual middle third
    assert abs(zx - (wt.STORY_W - zw) // 2) <= 2
    assert 640 <= zy and zy + zh <= 1300


@pytest.mark.parametrize("tid", ALL)
def test_story_grade_passes_blank_and_proof(tmp_path, cache, tid):
    t = wt.get_template(tid)
    wide, _sq = wt.make_test_logos(str(tmp_path / "logos"))
    for label, logo in (("blank", None), ("proof", wide)):
        out = _out(tmp_path, f"{tid}_story_{label}.png")
        _p, _m, text = wt._render(t, "Iron Forge Fitness", "Jordan Blake", logo,
                                  out, cache_dir=cache, fmt="story")
        g = wr.grade_welcome(out, text, t, fmt="story")
        assert g["passed"], f"{tid} story {label} failed: {g['failed']}"


@pytest.mark.parametrize("tid", ALL)
def test_story_layout_guard_all_templates(tid):
    assert wr._story_layout_ok(wt.get_template(tid))


# regression for the audit's CRITICAL: a TWO-LINE gym name must not run its owner
# line past the footer / out of the 85% safe band (the story T5-collision class).
@pytest.mark.parametrize("gym", [
    "Orangetheory Fitness Studio Downtown",   # wraps to two lines
    "CrossFit District H Strength and Conditioning",
    "Iron Forge Fitness",                     # one line (control)
])
@pytest.mark.parametrize("tid", ["T1", "T8", "T9"])
def test_story_two_line_gym_name_stays_in_safe_band(tmp_path, cache, tid, gym):
    t = wt.get_template(tid)
    wide, _sq = wt.make_test_logos(str(tmp_path / "logos"))
    out = _out(tmp_path, f"{tid}_2line.png")
    _p, _m, text = wt._render(t, gym, "Alexandra Fitzgerald", wide, out,
                              cache_dir=cache, fmt="story")
    # the composed bottom block clears the footer (measured the way it is drawn)
    assert wt.story_bottom_bounds(t, gym, "Alexandra Fitzgerald") <= wt.STORY_FOOTER_TOP
    # and the whole card still passes the story grade
    assert wr.grade_welcome(out, text, t, fmt="story")["passed"]


def test_story_guard_would_catch_an_overrunning_bottom_block(monkeypatch):
    # prove the guard is real: force a tiny footer so even a short name overruns,
    # and confirm the guard flips to False (it is not a rubber stamp)
    monkeypatch.setattr(wt, "STORY_FOOTER_TOP", 800)
    assert wr._story_layout_ok(wt.get_template("T1")) is False


def test_story_red_region_check_is_aspect_correct(tmp_path, cache):
    # regression: the red-mask downscale must preserve aspect, else a tall story
    # frame is squashed ~1.8x and thin horizontal accents vanish (T5 rule bug).
    t = wt.get_template("T5")  # accent == rule (a thin red hairline)
    out = _out(tmp_path, "t5_story.png")
    wt._render(t, "Iron Forge", "Sam", None, out, cache_dir=cache, fmt="story")
    assert wr.red_regions(Image.open(out), mask_zone=wt.story_zone(t)) == 1


def test_story_background_native_not_square_crop(cache):
    # story background is regenerated native to 9:16, never a crop of the square art
    t = wt.get_template("T1")
    bg_path, _ = wt.ensure_background(t, cache_dir=cache, fmt="story")
    assert Image.open(bg_path).size == (wt.STORY_W, wt.STORY_H)
    feed_bg, _ = wt.ensure_background(t, cache_dir=cache, fmt="feed")
    assert Image.open(feed_bg).size == (wt.SIZE, wt.SIZE)
    assert bg_path != feed_bg  # cached under separate keys


def test_story_and_feed_backgrounds_cached_separately(cache):
    t = wt.get_template("T2")
    fp, _ = wt.ensure_background(t, cache_dir=cache, fmt="feed")
    sp, _ = wt.ensure_background(t, cache_dir=cache, fmt="story")
    assert os.path.isfile(fp) and os.path.isfile(sp)
    assert Image.open(fp).size == (wt.SIZE, wt.SIZE)
    assert Image.open(sp).size == (wt.STORY_W, wt.STORY_H)


def test_invalid_format_raises():
    with pytest.raises(KeyError):
        wt.make_welcome("T1", "Gym", "Owner", None, format="square")


def test_slots_json_has_per_format_zones():
    d = wt.slots_dict()
    assert set(d["formats"]) == {"feed", "story"}
    assert d["formats"]["story"] == {"w": 1080, "h": 1920, "safe_top": 288,
                                     "safe_bottom": 1632, "margin": 96}
    for tid, spec in d["templates"].items():
        assert spec["feed"]["logo_zone"]["fraction"] >= 0.13
        assert spec["story"]["logo_zone"]["fraction"] >= 0.13
        assert spec["story"]["logo_zone"]["h"] == 540
        # back-compat flat keys still present
        assert "logo_zone" in spec and "text_column" in spec


def test_story_background_prompt_is_vertical_and_calm_center():
    p = wt.background_prompt(wt.get_template("T1"), fmt="story")
    assert "9:16" in p or "vertical" in p.lower()
    assert "CENTER" in p or "middle third" in p.lower()
    # still forbids all text/letters/logos
    assert "NO text" in p and "NO logos" in p
