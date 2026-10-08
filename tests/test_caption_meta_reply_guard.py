"""Caption guard against model clarification / meta replies (Bolton Club, 2026-10-07).

A Bolton photo whose only scene hint was the camera filename prefix "DSC" got an SB7
"caption" that was the model asking what the photo shows. It was staged on three
pending rows. These tests pin: the copy gate flags that shape (and only that shape),
SB7 retries once then falls back, make_caption never returns it, the template path
never returns it, the A+ gate refuses it, and "DSC" is no longer a scene hint.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, copy_gate, drafter, post_quality  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import StoryBrandGenerator  # noqa: E402
from agent.library import Creative  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402

# The real text staged on Bolton's Oct 26 slot 1 rows (3276b31c, b4db4799, fbd91cd4).
BOLTON_DSC = (
    'You said the photo shows "DSC" but didn\'t describe what that image contains.\n\n'
    "I need to see what's actually in the photo or video to write a caption that "
    "matches the scene.\n\nCan you tell me what the image shows?\n\nFor example, A "
    "member mid workout?\n\nBefore/after transformation?\n\nA coach demonstrating an "
    "exercise?\n\nThe gym space itself?\n\nA specific training session or class in "
    "progress?\n\nOnce you describe the visual, I'll write a fresh, problem first "
    'caption that ties the scene to the "30 Day Results In Advance" theme.\n\n'
    "Send us a message to get started")

META = [
    BOLTON_DSC,
    "Can you tell me what the image shows?",
    "You said the photo shows a squat rack.",
    "I can't see the image you attached.",
    "I don\u2019t see an image attached to this request.",
    "I don't have enough information to write this caption.",
    "I don't have the photo, so here is a general post.",
    "Could you describe the scene so I can match it?",
    "As an AI, I cannot view images.",
    "Here's a caption for your post",
    "I'll write a caption once I know more.",
    # crossfitlocal rows 04b1bcc5 / fb4bec7c (2026-09-07), found by the fleet scan
    'You mentioned the photo shows "feedfit" but I need clarification: is that a member '
    "eating, a nutrition coaching moment, a post workout meal?",
    'You said this post\'s photo shows "feedfit" but I need clarity.',
]

REAL = [
    "You don't have to do it alone.",
    'She told us "I don\'t have time." Six weeks later she never misses a Tuesday.',
    "I need to know I'm making progress. That's what every member tells us first.",
    "I don't see the point of another random video workout. Neither do our members.",
    "I can't believe how strong she got.",
    "Can you squat your bodyweight? Most people can't yet. We can help.",
    "Could you be next? Send us a message to get started",
    "You've tried everything.\n\nDifferent routines, apps, gyms that felt too crowded.",
    "What's your excuse? Book a free intro.",
]


@pytest.mark.parametrize("text", META)
def test_meta_replies_are_flagged(text):
    assert copy_gate.is_meta_reply(text)
    assert "meta_reply" in copy_gate.violations(text)
    assert "meta_reply" in copy_gate.caption_violations(text)


@pytest.mark.parametrize("text", REAL)
def test_real_copy_is_not_flagged(text):
    assert not copy_gate.is_meta_reply(text)
    assert "meta_reply" not in copy_gate.violations(text)


def test_a_plus_gate_refuses_the_bolton_text():
    issues = post_quality.caption_issues(BOLTON_DSC)
    assert any("meta reply" in i for i in issues)


def _voice():
    return VoiceDoc(
        raw='We help busy people get strong.\n\n### CTA rotation\n"Book your intro session."',
        hashtags=["#GymLife"], ctas=["Book your intro session."])


def _creative():
    return Creative(path="/lib/DSC_4412.jpg", media_type="image",
                    client_note="Personalized strength training for busy adults.")


class _SeqLLM:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = 0
        self.users = []

    def __call__(self, system, user):
        self.calls += 1
        self.users.append(user)
        return self.bodies.pop(0) if self.bodies else ""


def test_sb7_retries_once_and_keeps_a_clean_retry(monkeypatch):
    good = "Busy weeks wreck your plans. Our coaches build strength around your life."
    fake = _SeqLLM(BOLTON_DSC, good)
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    monkeypatch.setattr(drafter, "_note_sb7_fallback", lambda *a, **k: None)
    caption, _tags, _frags = StoryBrandGenerator().build(_voice(), _creative())
    assert fake.calls == 2
    assert "previous attempt asked a question" in fake.users[1]
    assert good in caption and not copy_gate.is_meta_reply(caption)


def test_sb7_falls_back_to_template_after_two_meta_replies(monkeypatch):
    fake = _SeqLLM(BOLTON_DSC, "Can you tell me what the image shows?")
    reasons = []
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    monkeypatch.setattr(drafter, "_note_sb7_fallback", lambda k, r: reasons.append(r))
    caption, _tags, _frags = StoryBrandGenerator().build(_voice(), _creative())
    assert fake.calls == 2
    assert reasons == ["meta_reply"]
    assert not copy_gate.is_meta_reply(caption)
    assert "Personalized strength training" in caption      # template = client note


class _Src:
    def __init__(self, text):
        self.text = text
        self.id = "s1"


def _acct():
    return Account(key="theboltonclub_ig", display_name="The Bolton Club",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="TID")


def test_make_caption_never_returns_a_meta_reply_from_sb7(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.setattr(client_content, "has_own_voice_doc", lambda a, v=None: True)
    monkeypatch.setattr(StoryBrandGenerator, "build",
                        lambda self, *a, **k: (BOLTON_DSC, ["#x"], ["b"]))
    voice = VoiceDoc(raw="v", hashtags=["#x"], ctas=["Send us a message to get started"])
    cap, _ = client_content.make_caption(
        _acct(), _Src("Personalized strength training for busy adults."), voice, "k")
    assert not copy_gate.is_meta_reply(cap)
    assert "Personalized strength training" in cap            # baseline


def test_template_path_never_returns_a_meta_reply(monkeypatch):
    monkeypatch.delenv("AGENT_SB7_ENABLED", raising=False)
    voice = VoiceDoc(raw="v", hashtags=["#x"], ctas=["Book now."])
    cap, _ = client_content.make_caption(
        _acct(), _Src("Can you tell me what the image shows?"), voice, "k")
    assert cap == ""                       # empty == the existing "do not stage" signal


@pytest.mark.parametrize("stem", ["DSC_4412", "DSC04412", "DSCN0042", "PXL_20260901_123",
                                  "GOPR0123", "IMG_5144"])
def test_camera_default_filenames_are_not_scene_hints(stem):
    assert client_content._humanize_stem(stem) == ""


def test_descriptive_filenames_still_hint():
    assert client_content._humanize_stem("Dale_Peace_Run") == "Dale Peace Run"
