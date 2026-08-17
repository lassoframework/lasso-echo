"""
ISSUE 1 (Dale, CrossFit ENG, round 2, 2026-08-17): a youth VIDEO got adult-oriented copy
again; Dale believes it predates the round-1 fix.

VERIFICATION: the round-1 scene-hint grounding covers VIDEO creatives, not just photos.
pick_image returns image OR video (client_content.pick_image), and build_client_draft
passes the ACTUAL picked creative (photo or video) into make_caption(creative=...), which
feeds photo_grounding(creative) to SB7 as a scene hint. So a youth-named VIDEO yields
youth-matched grounding exactly like a photo. These regression tests pin that end to end
so a youth video can never silently regress to adult copy.

Fully offline: a real temp library holds a youth VIDEO; the SB7 generator is stubbed so we
can assert exactly what grounding the video path passes it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content            # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc            # noqa: E402


class _Source:
    def __init__(self, text, sid="s1", citation="c1", category="service"):
        self.text = text
        self.id = sid
        self.citation = citation
        self.category = category


def _voice():
    return VoiceDoc(raw="We help busy families get fit.", hashtags=["#Fit"],
                    ctas=["Book a free intro."])


def _acct():
    return Account(key="eng_ig", display_name="CrossFit ENG",
                   platform=Platform.INSTAGRAM,
                   token_env="X_TOK", target_id_env="X_ID")


def _youth_library(tmp_path):
    """A library holding ONE youth-named VIDEO with a youth sidecar note."""
    lib = tmp_path / "lib"
    lib.mkdir()
    vid = lib / "20260817T090000Z_Youth_Wall_Sit_w_smiles.mp4"
    vid.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video-bytes")
    # sidecar note (client's own words about the shot)
    (lib / "20260817T090000Z_Youth_Wall_Sit_w_smiles.txt").write_text(
        "Youth fitness class, kids doing wall sits with big smiles")
    return str(lib)


def test_video_creative_is_picked_like_a_photo(tmp_path):
    lib = _youth_library(tmp_path)
    picked = client_content.pick_image("eng_ig", "2026-08-17", lib)
    assert picked is not None
    assert picked.media_type == "video"
    assert picked.path.endswith(".mp4")


def test_youth_video_yields_youth_grounding_to_sb7(tmp_path, monkeypatch):
    monkeypatch.setattr(client_content.config, "sb7_enabled", lambda: True)
    monkeypatch.setattr(client_content.config, "client_sources_enabled", lambda: True)
    monkeypatch.setattr(client_content.config, "hosting_enabled", lambda: False)

    lib = _youth_library(tmp_path)
    source = _Source("We coach members every day.")

    # stub the source/gate machinery so build_client_draft reaches the caption path
    monkeypatch.setattr(client_content.client_sources, "categories_present",
                        lambda ak: ["service"])
    monkeypatch.setattr(client_content, "category_for_day", lambda *a, **k: "service")
    monkeypatch.setattr(client_content, "_source_for_day", lambda *a, **k: source)
    monkeypatch.setattr(client_content.client_sources, "approved_claims", lambda ak: [])
    monkeypatch.setattr(client_content.rotation, "is_gate_clean",
                        lambda *a, **k: True)

    seen = {}

    class _FakeSB7:
        def build(self, voice, creative, account=None):
            seen["note"] = creative.client_note
            return ("Your kid's confidence is built in the gym, not on a screen.",
                    ["#Youth"], [])

    import agent.drafter as drafter
    monkeypatch.setattr(drafter, "StoryBrandGenerator", _FakeSB7)

    draft = client_content.build_client_draft(_acct(), "2026-08-17", _voice(), lib)
    assert draft is not None
    # the VIDEO's own youth note reached the SB7 prompt as a scene hint (round-1 fix
    # covers video, not only photos) — so the caption matches the youth video
    assert "Youth fitness class" in seen["note"]
    assert "scene hint" in seen["note"].lower()
    assert draft.caption.startswith("Your kid's confidence")


def test_hash_named_video_adds_no_false_grounding(tmp_path):
    """A hash-named video with no note yields empty grounding (no invented scene)."""
    lib = tmp_path / "lib2"
    lib.mkdir()
    v = lib / "20260817T090000Z_df9b08b46f194d489c532120652e6231.mp4"
    v.write_bytes(b"fake")
    from agent.library import list_creatives
    creative = [c for c in list_creatives(str(lib)) if c.media_type == "video"][0]
    assert client_content.photo_grounding(creative) == ""
