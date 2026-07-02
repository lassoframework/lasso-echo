"""
Service concept set tests. Asserts: every service concept's prompt carries its
assigned archetype constraints plus the brand constants; no digit or percent sign
in any service headline or rendered text; the archetype quota (none more than
twice in the batch) holds; --set filters correctly; the brand/service alternation
preference is respected and LOSES to the no-repeat window. Offline.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, regen_library, rotation  # noqa: E402


SERVICE = {k: v for k, v in regen_library.CONCEPTS.items() if v.get("set") == "service"}

EXPECTED_MAP = {
    "ads_done_for_you": "split", "follow_up_system": "flow",
    "booked_to_close": "path", "sales_training": "hero",
    "funnel_diagnostic": "flow", "social_done_for_you": "split",
    "one_partner": "hero", "website_done_for_you": "headline",
}


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


# ---- the map, the quota, and the source-compliance basics ------------------------
def test_service_map_and_archetype_quota():
    assert {k: v["archetype"] for k, v in SERVICE.items()} == EXPECTED_MAP
    counts = {}
    for v in SERVICE.values():
        counts[v["archetype"]] = counts.get(v["archetype"], 0) + 1
    assert max(counts.values()) <= 2
    assert len(SERVICE) == 8
    # the original brand set is untouched
    assert len([k for k, v in regen_library.CONCEPTS.items() if v["set"] == "brand"]) == 8


def test_no_digits_or_percent_in_service_render_text():
    for key, spec in SERVICE.items():
        assert not re.search(r"[\d%]", spec["headline"]), key      # rendered text
        assert not re.search(r"[—–-]", spec["headline"]), key      # no dash characters
        for line in spec["concept"]:
            assert "%" not in line and not re.search(r"\d", line), key


def test_every_service_prompt_carries_archetype_and_brand():
    for key, spec in SERVICE.items():
        for variant, prompt in regen_library.assemble_prompts(key):
            low = prompt.lower()
            assert f"archetype {spec['archetype']}" in low, key
            assert "cream #faf6f0: the canvas" in low, key
            assert "never a full bleed solid color slab" in low, key
            assert "one idea per card" in low, key
            for hexcode in ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0"):
                assert hexcode in prompt, key


# ---- --set filters ------------------------------------------------------------------
def test_set_filter_generates_only_that_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "library"
    lib.mkdir()
    out = regen_library.run(set_name="service", nano_client=FakeNano(),
                            s3_client=FakeS3(), out_dir=str(lib))
    assert sorted(out) == sorted(EXPECTED_MAP)
    pngs = [p for p in os.listdir(lib) if p.endswith(".png")]
    assert len(pngs) == 8                                    # no story variants in this set
    side = json.loads((lib / "lasso_v2_one_partner.json").read_text())
    assert side["set"] == "service"                           # membership recorded


def test_dry_run_set_spends_nothing(monkeypatch, tmp_path, capsys):
    class Exploding:
        def generate_image(self, *a, **k):
            raise AssertionError("spend during dry run")

    out = regen_library.run(set_name="brand", dry_run=True, nano_client=Exploding(),
                            out_dir=str(tmp_path))
    assert sorted(out) == sorted(k for k, v in regen_library.CONCEPTS.items()
                                 if v["set"] == "brand")
    assert "ads_done_for_you" not in capsys.readouterr().out  # service set untouched


# ---- rotation: set alternation preferred, window still wins ----------------------
def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, cset in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        (lib / (os.path.splitext(name)[0] + ".txt")).write_text("clean note", encoding="utf-8")
        (lib / (os.path.splitext(name)[0] + ".json")).write_text(
            json.dumps({"set": cset}), encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_ROTATION_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_prefers_alternating_sets(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # yesterday was a BRAND day; today an alphabetically-first brand card competes
    # with a service card: the set alternation preference picks the service card
    lib = _lib(tmp_path, [("lasso_p1_aaa_brand.jpg", "brand"),
                          ("lasso_p2_zzz_service.jpg", "service")])
    rotation.record_served("lasso_ig", "yesterday.jpg", "p9", "2026-07-06",
                           set_name="brand")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(creative.path) == "lasso_p2_zzz_service.jpg"


def test_set_alternation_loses_to_no_repeat_window(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # the preferred service card is inside the window: the window wins and the
    # same-set brand card is chosen instead
    lib = _lib(tmp_path, [("lasso_p1_fresh_brand.jpg", "brand"),
                          ("lasso_p2_recent_service.jpg", "service")])
    rotation.record_served("lasso_ig", "lasso_p2_recent_service.jpg", "p2",
                           "2026-07-05", set_name="service")
    rotation.record_served("lasso_ig", "yesterday.jpg", "p9", "2026-07-06",
                           set_name="brand")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(creative.path) == "lasso_p1_fresh_brand.jpg"
