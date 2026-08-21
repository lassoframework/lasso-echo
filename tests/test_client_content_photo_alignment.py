"""
ISSUE 1 (Dale, CrossFit ENG, 2026-08-15): "the copy isn't quite matching the actual
photo or video in the post." The client caption was grounded ONLY in the rotated
source topic, never in what the picked photo/video actually shows. These tests pin the
alignment fix: the picked creative's OWN sidecar note + a humanized filename hint are
fed to the caption generator as a SCENE HINT so the copy references the shot, WITHOUT
fabricating (the hint carries no claims; the figure gate still owns claims).

Fully offline: the SB7 generator is stubbed so we can assert exactly what grounding the
caption path passes it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


class _Source:
    def __init__(self, text, sid="s1", citation="c1"):
        self.text = text
        self.id = sid
        self.citation = citation


class _Creative:
    def __init__(self, path, client_note=""):
        self.path = path
        self.client_note = client_note

    @property
    def stem(self):
        return os.path.splitext(os.path.basename(self.path))[0]


def _voice():
    return VoiceDoc(raw="We help busy people get fit.", hashtags=["#Fit"],
                    ctas=["Save this."])


def _acct():
    return Account(key="eng_ig", display_name="CrossFit ENG",
                   platform=Platform.INSTAGRAM,
                   token_env="X_TOK", target_id_env="X_ID")


# ---- _humanize_stem: real descriptive words in, noise out ----------------------

def test_humanize_stem_strips_timestamp_and_keeps_words():
    assert client_content._humanize_stem(
        "20260812T181128Z_Dale_Peace_Run.jpg") == "Dale Peace Run"


def test_humanize_stem_youth_photo():
    assert client_content._humanize_stem(
        "20260810T165039Z_Youth_Wall_Sit_w_smiles") == "Youth Wall Sit w smiles"


def test_humanize_stem_drops_hash_and_img_noise():
    # a hash-named upload and IMG_#### carry no description -> empty
    assert client_content._humanize_stem(
        "20260811T165248Z_df9b08b46f194d489c532120652e6231.mp4") == ""
    assert client_content._humanize_stem("20260811T170017Z_IMG_2704.jpg") == ""


# ---- photo_grounding: sidecar note + filename, both non-fabricated -------------

def test_photo_grounding_prefers_sidecar_note_then_filename():
    c = _Creative("20260810T165039Z_Youth_Wall_Sit_w_smiles.jpg",
                  client_note="Youth fitness fun with smiles")
    g = client_content.photo_grounding(c)
    assert "Youth fitness fun with smiles" in g


def test_photo_grounding_uses_filename_when_no_note():
    c = _Creative("20260812T181128Z_Dale_Peace_Run.jpg", client_note="")
    assert client_content.photo_grounding(c) == "Dale Peace Run"


def test_photo_grounding_empty_when_no_signal():
    c = _Creative("20260811T165248Z_df9b08b46f194d489c532120652e6231.mp4")
    assert client_content.photo_grounding(c) == ""
    assert client_content.photo_grounding(None) == ""


# ---- _SourceCreative folds the hint into the note SB7 sees ---------------------

def test_source_creative_appends_photo_hint_as_scene_hint():
    sc = client_content._SourceCreative(_Source("We coach youth athletes."),
                                        "key1", photo_hint="Youth Wall Sit w smiles")
    assert "We coach youth athletes." in sc.client_note
    assert "Youth Wall Sit w smiles" in sc.client_note
    # tagged as a scene hint, explicitly NOT a source of facts to state
    assert "scene hint" in sc.client_note.lower()
    assert "NOT a source of facts" in sc.client_note


def test_source_creative_no_hint_is_unchanged_note():
    sc = client_content._SourceCreative(_Source("HYROX"), "key1", photo_hint="")
    assert sc.client_note == "HYROX"


# ---- make_caption threads the PICKED creative's grounding into SB7 -------------

def test_make_caption_grounds_sb7_in_the_photo(monkeypatch):
    monkeypatch.setattr(client_content.config, "sb7_enabled", lambda: True)
    seen = {}

    class _FakeSB7:
        def build(self, voice, creative, account=None, avoid_openings=(), angle="", avoid_angles=()):
            seen["note"] = creative.client_note
            return ("Your kid's confidence is built in the gym.", ["#Youth"], [])

    import agent.drafter as drafter
    monkeypatch.setattr(drafter, "StoryBrandGenerator", _FakeSB7)

    creative = _Creative("20260810T165039Z_Youth_Wall_Sit_w_smiles.jpg",
                         client_note="Youth fitness fun with smiles")
    cap, tags = client_content.make_caption(
        _acct(), _Source("We help members win."), _voice(),
        "key1", creative=creative)

    assert cap.startswith("Your kid's confidence")
    # the SB7 prompt actually SAW the photo's own note (the alignment fix)
    assert "Youth fitness fun with smiles" in seen["note"]
    assert "We help members win." in seen["note"]


def test_make_caption_without_creative_has_no_scene_hint(monkeypatch):
    monkeypatch.setattr(client_content.config, "sb7_enabled", lambda: True)
    seen = {}

    class _FakeSB7:
        def build(self, voice, creative, account=None, avoid_openings=(), angle="", avoid_angles=()):
            seen["note"] = creative.client_note
            return ("A real caption body here.", ["#Fit"], [])

    import agent.drafter as drafter
    monkeypatch.setattr(drafter, "StoryBrandGenerator", _FakeSB7)

    client_content.make_caption(_acct(), _Source("We help members win."),
                                _voice(), "key1")  # no creative
    assert "scene hint" not in seen["note"].lower()
