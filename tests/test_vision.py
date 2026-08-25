"""
P1 — Echo Vision core (agent/vision.py): v2 analysis coercion, the identity firewall, the
DCT perceptual hash, and the caption-eligibility / auto-plannable routing. Offline.
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import vision  # noqa: E402


# ---- identity firewall -----------------------------------------------------------------

def test_identity_issues_catches_gender_age_appearance():
    assert "man" in vision.identity_issues("a man squatting")
    assert "woman" in vision.identity_issues("a woman on the rower")
    assert "muscular" in vision.identity_issues("a muscular member")
    assert "young" in vision.identity_issues("a young athlete")
    assert vision.identity_issues("three people in a group class") == []
    assert vision.identity_issues("a coach adjusting form") == []


# ---- coercion + firewall ---------------------------------------------------------------

def test_coerce_strips_leaking_detail_and_flags():
    raw = {"one_line": "A member squatting", "people_bucket": "solo", "activity": "strength",
           "visible_details": [{"detail": "chalk on hands", "confidence": 0.9},
                               {"detail": "a woman in leggings", "confidence": 0.95}]}
    a = vision.coerce_analysis(raw, phash="ab" * 8)
    # the gender-leaking detail is dropped; the clean one survives
    kept = [d["detail"] for d in a["visible_details"]]
    assert "chalk on hands" in kept and "a woman in leggings" not in kept
    assert a["identity_flag"] is True and "woman" in a["identity_terms"]


def test_coerce_normalizes_enums_and_bucket():
    a = vision.coerce_analysis({"one_line": "x", "setting": "spaceship",
                                "people_bucket": "seventeen", "activity": "yoga",
                                "avatar_fit": "??"})
    assert a["setting"] == "other"           # unknown -> other
    assert a["people"]["bucket"] == "none"   # unknown -> none
    assert a["activity"] == "none"
    assert a["avatar_fit"] == "unclear"      # conservative default


def test_coerce_returns_none_on_unparseable():
    assert vision.coerce_analysis("not json at all") is None
    assert vision.coerce_analysis("") is None


def test_caption_eligible_details_threshold():
    a = vision.coerce_analysis({"one_line": "x", "visible_details": [
        {"detail": "chalk", "confidence": 0.9},
        {"detail": "a clock", "confidence": 0.84}]})
    elig = vision.caption_eligible_details(a)
    assert "chalk" in elig and "a clock" not in elig    # 0.84 < 0.85 threshold


# ---- routing ---------------------------------------------------------------------------

def test_auto_plannable_clean_passes():
    a = vision.coerce_analysis({"one_line": "three people in a class", "activity": "class",
                                "people_bucket": "small_group", "avatar_fit": "genpop",
                                "quality": {"usable": True}})
    assert vision.auto_plannable(a) == (True, [])


def test_auto_plannable_excludes_each_flag_class():
    base = {"one_line": "a class", "quality": {"usable": True}, "avatar_fit": "genpop"}
    assert vision.auto_plannable(vision.coerce_analysis(
        {**base, "safety_flags": ["injury_visible"]}))[0] is False
    # athlete is NO LONGER a flag class (Blake 2026-08-18): a competitive shot is plannable
    assert vision.auto_plannable(vision.coerce_analysis(
        {**base, "avatar_fit": "athlete"}))[0] is True
    assert vision.auto_plannable(vision.coerce_analysis(
        {**base, "quality": {"usable": False}}))[0] is False
    assert vision.auto_plannable(None)[0] is False
    assert vision.auto_plannable({"analysis_failed": True})[0] is False


def test_vision_allow_flags_let_photos_through(monkeypatch):
    """AGENT_VISION_ALLOW_FLAGS (Blake 2026-08-25): a listed flag no longer HOLDS a photo from
    auto-pick (still detected/recorded). Default empty = every flag blocks (unchanged)."""
    base = {"one_line": "a class", "quality": {"usable": True}, "avatar_fit": "genpop"}
    brand = vision.coerce_analysis({**base, "safety_flags": ["third_party_brand"]})
    minor = vision.coerce_analysis({**base, "safety_flags": ["minor_prominent"]})
    named = vision.coerce_analysis({**base, "contains_person_name": True})
    # default: all three are blocked
    monkeypatch.delenv("AGENT_VISION_ALLOW_FLAGS", raising=False)
    assert vision.auto_plannable(brand)[0] is False
    assert vision.auto_plannable(minor)[0] is False
    assert vision.auto_plannable(named)[0] is False
    # armed: the three Blake allowed now pass
    monkeypatch.setenv("AGENT_VISION_ALLOW_FLAGS",
                       "third_party_brand,minor_prominent,person_name_in_image")
    assert vision.auto_plannable(brand) == (True, [])
    assert vision.auto_plannable(minor) == (True, [])
    assert vision.auto_plannable(named) == (True, [])
    # a NON-allowed flag still blocks even when others are allowed
    pii = vision.coerce_analysis({**base, "safety_flags": ["pii_visible"]})
    assert vision.auto_plannable(pii)[0] is False


def test_bts_restricted():
    # athlete / athlete_leaning are unrestricted now; only `unclear` stays BTS-only
    assert vision.bts_restricted(vision.coerce_analysis({"one_line": "x",
                                 "avatar_fit": "athlete_leaning"})) is False
    assert vision.bts_restricted(vision.coerce_analysis({"one_line": "x",
                                 "avatar_fit": "athlete"})) is False
    assert vision.bts_restricted(vision.coerce_analysis({"one_line": "x",
                                 "avatar_fit": "unclear"})) is True
    assert vision.bts_restricted(vision.coerce_analysis({"one_line": "x",
                                 "avatar_fit": "genpop"})) is False


# ---- DCT perceptual hash ---------------------------------------------------------------

def _img_bytes(color, size=(64, 64)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_dct_phash_stable_and_hex():
    from PIL import Image
    buf = io.BytesIO()
    # a gradient image (non-uniform, so the DCT is meaningful)
    img = Image.new("L", (64, 64))
    img.putdata([(x + y) % 256 for y in range(64) for x in range(64)])
    img.save(buf, format="PNG")
    h = vision.dct_phash(buf.getvalue())
    assert isinstance(h, str) and len(h) == 16
    assert vision.dct_phash(buf.getvalue()) == h        # deterministic


def test_dct_phash_none_on_garbage():
    assert vision.dct_phash(b"not an image") is None


def test_hamming_distance():
    assert vision.hamming("0000000000000000", "0000000000000000") == 0
    assert vision.hamming("0000000000000000", "0000000000000001") == 1
    assert vision.hamming("ffffffffffffffff", "0000000000000000") == 64
    assert vision.hamming(None, "0") == 999             # missing -> far, never clusters


def test_near_duplicate_images_cluster_far_from_different():
    from PIL import Image
    buf1, buf2, buf3 = io.BytesIO(), io.BytesIO(), io.BytesIO()
    base = [(x * 4) % 256 for y in range(64) for x in range(64)]
    Image.new("L", (64, 64)); img1 = Image.new("L", (64, 64)); img1.putdata(base)
    img1.save(buf1, format="PNG")
    # a JPEG-ish near-dupe: same content, tiny brightness shift
    img2 = Image.new("L", (64, 64)); img2.putdata([min(255, p + 8) for p in base])
    img2.save(buf2, format="PNG")
    # a clearly different image
    img3 = Image.new("L", (64, 64)); img3.putdata([(y * 4) % 256 for y in range(64) for x in range(64)])
    img3.save(buf3, format="PNG")
    h1, h2, h3 = (vision.dct_phash(b.getvalue()) for b in (buf1, buf2, buf3))
    assert vision.hamming(h1, h2) <= 6      # near-dupe clusters
    assert vision.hamming(h1, h3) > 6       # different does not


# ---- analyze_and_store: idempotency + failure escalation --------------------------------

def _write_img(path):
    from PIL import Image
    Image.new("RGB", (64, 64), (40, 60, 80)).save(path)


_GOOD_JSON = ('{"one_line":"three people in a class","setting":"gym_floor",'
              '"people_bucket":"small_group","activity":"class",'
              '"visible_details":[{"detail":"chalk on hands","confidence":0.9}],'
              '"quality":{"usable":true},"avatar_fit":"genpop","safety_flags":[]}')


def test_analyze_and_store_writes_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREATIVE_STUDIO_ENABLED", "true")
    monkeypatch.delenv("AGENT_SPEND_CAP_ENABLED", raising=False)
    img = tmp_path / "p.png"
    _write_img(img)
    a = vision.analyze_and_store(str(img), reader=lambda b: _GOOD_JSON, day="2026-09-01")
    assert a["one_line"].startswith("three people") and a["version"] == 2
    assert a["phash"] and len(a["phash"]) == 16
    assert vision.analysis_state(str(img)) == "ok"


def test_analyze_and_store_idempotent_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREATIVE_STUDIO_ENABLED", "true")
    img = tmp_path / "p.png"
    _write_img(img)
    calls = {"n": 0}

    def _reader(b):
        calls["n"] += 1
        return _GOOD_JSON
    vision.analyze_and_store(str(img), reader=_reader, day="2026-09-01")
    vision.analyze_and_store(str(img), reader=_reader, day="2026-09-01")   # skip: already analyzed
    assert calls["n"] == 1, "an already-analyzed image is not re-read (preserve on re-sync)"


def test_analyze_and_store_escalates_after_3_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREATIVE_STUDIO_ENABLED", "true")
    img = tmp_path / "p.png"
    _write_img(img)
    alerts = []
    for _ in range(2):
        r = vision.analyze_and_store(str(img), reader=lambda b: "garbage", day="2026-09-01",
                                     alert=alerts.append)
        assert r is None                      # not yet escalated
        assert vision.analysis_state(str(img)) in ("missing",)
    r3 = vision.analyze_and_store(str(img), reader=lambda b: "garbage", day="2026-09-01",
                                  alert=alerts.append)
    assert r3["analysis_failed"] is True and vision.analysis_state(str(img)) == "failed"
    assert any("FAILED" in a for a in alerts)
    # a failed analysis excludes the image from auto-planning
    assert vision.auto_plannable(vision.stored_analysis(str(img)))[0] is False


def test_analyze_library_skips_analyzed_and_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREATIVE_STUDIO_ENABLED", "true")
    _write_img(tmp_path / "a.png")
    _write_img(tmp_path / "b.png")
    (tmp_path / "c.mp4").write_bytes(b"not analyzed - video")
    out = vision.analyze_library(str(tmp_path), reader=lambda b: _GOOD_JSON, day="2026-09-01")
    assert out["analyzed"] == 2                # both images, not the video
    out2 = vision.analyze_library(str(tmp_path), reader=lambda b: _GOOD_JSON, day="2026-09-01")
    assert out2["skipped"] == 2 and out2["analyzed"] == 0    # idempotent second pass


# ---- audit fixes: firewall plurals/appearance, precise name heuristic, flat pHash --------

def test_firewall_catches_plurals_and_more_appearance():
    assert "males" in vision.identity_issues("two males on the bench")
    assert "females" in vision.identity_issues("a row of females")
    assert vision.identity_issues("a fat member")          # appearance
    assert vision.identity_issues("a petite person")
    assert vision.identity_issues("a bald coach")
    assert vision.identity_issues("a thin build")


def test_firewall_leak_excludes_via_coerce():
    a = vision.coerce_analysis({"one_line": "Two males spotting on the bench",
                                "quality": {"usable": True}, "avatar_fit": "genpop"})
    assert a["identity_flag"] is True
    assert vision.auto_plannable(a)[0] is False


def test_name_heuristic_precise_ignores_signage():
    # signage must NOT be read as a member name
    for sign in ("Deadlift Area", "Barbell Club", "Rowing Zone", "January Challenge",
                 "Strength And Conditioning", "Squat Rack"):
        assert vision._looks_like_person_name(sign) is False, sign
    # clear name signals DO fire
    assert vision._looks_like_person_name("Great work Marcus") is True
    assert vision._looks_like_person_name("John Smith crushed it") is True
    assert vision._looks_like_person_name("coach Sarah") is True


def test_flat_images_do_not_falsely_cluster():
    from PIL import Image
    import io as _io

    def _solid(v):
        b = _io.BytesIO()
        Image.new("L", (64, 64), v).save(b, format="PNG")
        return vision.dct_phash(b.getvalue())
    dark, light, dark2 = _solid(10), _solid(245), _solid(10)
    assert vision.hamming(dark, dark2) == 0            # identical flats cluster
    assert vision.hamming(dark, light) > 6             # dark vs light do NOT (was a collision)


def test_safety_flag_stored_stripped():
    a = vision.coerce_analysis({"one_line": "x", "safety_flags": [" pii_visible ", "UNSANITARY"]})
    assert a["safety_flags"] == ["pii_visible", "unsanitary"]   # normalized, no stray space
