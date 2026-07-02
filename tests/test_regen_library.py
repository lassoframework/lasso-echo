"""
regen-library tests. Fully OFFLINE, no Gemini (fake nano + S3 clients, exploding
clients for dry-run). Asserts: the batch writes lasso_v2_ files + sidecars (with
story variants for the two +STORY concepts); --only regenerates one concept;
--dry-run spends nothing; new v2 cards are rotation-eligible while old exclusions
hold and story variants never enter feed rotation; every concept's prompt carries
the locked style constraints and the no-dash rule.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, regen_library, rotation  # noqa: E402


class FakeNano:
    def __init__(self):
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


class ExplodingClient:
    def generate_image(self, *a, **k):
        raise AssertionError("Gemini call during dry-run!")

    def exists(self, *a, **k):
        raise AssertionError("hosting call during dry-run!")

    def put(self, *a, **k):
        raise AssertionError("hosting call during dry-run!")


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "library"
    lib.mkdir()
    return str(lib)


# ---- the batch writes v2 files + sidecars -----------------------------------
def test_batch_writes_v2_files_and_sidecars(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    out = regen_library.run(nano_client=FakeNano(), s3_client=FakeS3(), out_dir=lib)
    assert len(out) == 8
    pngs = sorted(p for p in os.listdir(lib) if p.endswith(".png"))
    assert len(pngs) == 10                                   # 8 feed + 2 story variants
    assert all(p.startswith("lasso_v2_") for p in pngs)
    assert "lasso_v2_built_by_gym_owners_story.png" in pngs  # the two +STORY concepts
    assert "lasso_v2_three_step_path_story.png" in pngs
    # sidecars carry concept/headline/date/style and the hosted public_url
    side = json.loads((tmp_path / "library" / "lasso_v2_one_screen.json").read_text())
    assert side["concept"] == "one_screen"
    assert side["headline"].startswith("Every lead")
    assert side["style"] == "v2"
    assert re.match(r"\d{4}-\d{2}-\d{2}$", side["generated"])
    assert side["public_url"].startswith("https://cdn.echo.test/echo/lasso_library/")


# ---- --only regenerates a single concept -------------------------------------
def test_only_regenerates_single_concept(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    out = regen_library.run(only="one_screen", nano_client=FakeNano(),
                            s3_client=FakeS3(), out_dir=lib)
    assert list(out) == ["one_screen"]
    pngs = [p for p in os.listdir(lib) if p.endswith(".png")]
    assert pngs == ["lasso_v2_one_screen.png"]


def test_unknown_concept_is_a_clear_noop(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    out = regen_library.run(only="nope", nano_client=FakeNano(),
                            s3_client=FakeS3(), out_dir=lib)
    assert out == {} and os.listdir(lib) == []


# ---- --dry-run spends nothing --------------------------------------------------
def test_dry_run_spends_nothing(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    out = regen_library.run(dry_run=True, nano_client=ExplodingClient(),
                            s3_client=ExplodingClient(), out_dir=lib)
    assert all(v["dry_run"] for v in out.values())
    assert os.listdir(lib) == []                               # no files written
    printed = capsys.readouterr().out
    assert "one_screen" in printed and "ILLUSTRATED DIAGRAM" in printed


# ---- rotation eligibility: v2 in, old exclusions hold, story never feed --------
def test_v2_cards_rotate_old_exclusions_hold(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_ROTATION_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)

    regen_library.run(only="built_by_gym_owners", nano_client=FakeNano(),
                      s3_client=FakeS3(), out_dir=lib)         # feed + story files
    (tmp_path / "library" / "lasso_card_1_final.png").write_bytes(b"old slab")
    (tmp_path / "library" / "style_exclusions.json").write_text(
        json.dumps({"off_style": ["lasso_card_1_final.png"]}), encoding="utf-8")

    kind, creative = rotation.choose("lasso_ig", "2026-07-06", lib)
    assert kind == "library"
    assert os.path.basename(creative.path) == "lasso_v2_built_by_gym_owners.png"
    # across the week: never the old slab, never the 9:16 story variant as feed
    for day in ("2026-07-07", "2026-07-08"):
        k, c = rotation.choose("lasso_ig", day, lib)
        if c is not None:
            assert os.path.basename(c.path) not in (
                "lasso_card_1_final.png", "lasso_v2_built_by_gym_owners_story.png")


# ---- every concept's prompt carries the locked style + no-dash rule ------------
def test_all_concept_prompts_carry_style_and_no_dashes():
    for key in regen_library.CONCEPTS:
        for variant, prompt in regen_library.assemble_prompts(key):
            low = prompt.lower()
            assert "illustrated diagram" in low, key
            assert "cream #faf6f0: the canvas" in low, key
            assert "never a full bleed solid color slab" in low, key
            assert "one idea per card" in low, key
            assert "no em dashes" in low, key
            assert "—" not in prompt and "–" not in prompt, key
            if variant == "story":
                assert "9:16" in prompt and "1080x1920" in prompt
                assert "never a cropped, stretched, or reused feed card" in low
    # headlines themselves carry no em/en dashes or hyphens
    for key, spec in regen_library.CONCEPTS.items():
        assert not re.search(r"[—–-]", spec["headline"]), key
