"""
StoryBrand SB7 caption generator + the edit-learning feedback loop.

Fully OFFLINE: the Anthropic call (_call_llm_caption) is monkeypatched to a fake
that records the prompt it receives, so we can assert what guidance the drafter
folds in without touching the network.

Asserts:
  - the SB7 flag defaults OFF, and OFF means the drafter uses TemplateGenerator
    (verbatim client note), exactly today's behavior;
  - ON, the generator calls the LLM and returns its body + an approved CTA;
  - a gym's learned preferences (past edits + deny reasons) are folded into the
    prompt, so every edit makes the next caption move toward the approver's taste;
  - an edit whose after-text carries an unverified claim is never fed to the LLM;
  - one gym's learned preferences never leak into another gym's prompt;
  - any LLM failure falls back to the template (a card always gets a caption).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, drafter, tenant_brain  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import StoryBrandGenerator, TemplateGenerator  # noqa: E402
from agent.library import Creative  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


def _voice():
    return VoiceDoc(
        raw='We help busy people reclaim their fitness.\n\n'
            '### CTA rotation\n"Book your intro session."\n\n'
            '## Hashtags\n#GymLife',
        hashtags=["#GymLife"],
        ctas=["Book your intro session."],
    )


def _creative(note="Our 6am class is filling up for the new members joining."):
    return Creative(path="/lib/asset.png", media_type="image", client_note=note)


def _acct(key="gym_a"):
    return Account(key=key, display_name="Gym A", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")


class FakeLLM:
    """Records the (system, user) prompt and returns a fixed body."""

    def __init__(self, body="Stuck at 6am with no time? We help you reclaim it."):
        self.body = body
        self.system = None
        self.user = None
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        self.system = system
        self.user = user
        return self.body


def _arm_brain(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    bdir = str(tmp_path / "brains")
    # point every brain read/write at the tmp dir
    monkeypatch.setattr(tenant_brain, "brains_dir", lambda base_dir=None: bdir)
    return bdir


# ---- flag defaults + OFF = today's behavior -----------------------------------

def test_sb7_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_SB7_ENABLED", raising=False)
    assert config.sb7_enabled() is False


def test_flag_off_drafter_uses_template(monkeypatch):
    monkeypatch.delenv("AGENT_SB7_ENABLED", raising=False)
    # a fake LLM that would explode if the SB7 path ran
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: (_ for _ in ()).throw(AssertionError("LLM ran")))
    voice, creative = _voice(), _creative()
    d = drafter.draft_post(_acct(), creative, "2027-01-01T18:30:00+00:00", voice=voice)
    # the template path uses the client note verbatim
    assert creative.client_note in d.caption


# ---- ON: the LLM is called and its body is used -------------------------------

def test_sb7_on_calls_llm_and_appends_cta(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    caption, hashtags, fragments = StoryBrandGenerator().build(_voice(), _creative())
    assert fake.calls == 1
    assert fake.body in caption
    assert "Book your intro session." in caption      # approved CTA appended
    assert hashtags == ["#GymLife"]


# ---- the learning loop: edits + deny reasons fold into the prompt -------------

def test_past_edits_are_folded_into_the_prompt(monkeypatch, tmp_path):
    _arm_brain(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    tenant_brain.record_event("gym_a", "edit_diff",
                              before="A wordy machine draft here.",
                              after="Tight. Human. Done.", rule="style")
    tenant_brain.record_event("gym_a", "deny_reason",
                              reason="Never mention the parking lot.")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)

    StoryBrandGenerator().build(_voice(), _creative(), account=_acct("gym_a"))

    assert "LEARNED PREFERENCES" in fake.user
    assert "A wordy machine draft here." in fake.user       # the BEFORE
    assert "Tight. Human. Done." in fake.user               # the preferred AFTER
    assert "Never mention the parking lot." in fake.user    # the deny reason


def test_prompt_notes_are_deduped(monkeypatch, tmp_path):
    """The edit path records a generic style rule each time; the prompt must not
    repeat the identical line 10x."""
    _arm_brain(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    for _ in range(4):
        tenant_brain.record_event("gym_a", "edit_diff",
                                  before="b", after="a",
                                  rule="human edited: style preference")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(_voice(), _creative(), account=_acct("gym_a"))
    assert fake.user.count("human edited: style preference") == 1


def test_uncleared_edit_after_never_reaches_the_prompt(monkeypatch, tmp_path):
    _arm_brain(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    tenant_brain.record_event("gym_a", "edit_diff",
                              before="x", after="We save you $5,000 a year.",
                              rule="style")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(_voice(), _creative(), account=_acct("gym_a"))
    assert "$5,000" not in fake.user


def test_learned_preferences_never_leak_across_gyms(monkeypatch, tmp_path):
    _arm_brain(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    tenant_brain.record_event("gym_a", "edit_diff",
                              before="a-before", after="a-after-secret", rule="style")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(_voice(), _creative(), account=_acct("gym_b"))
    assert "a-after-secret" not in fake.user               # gym_b never sees gym_a


def test_no_account_means_no_guidance_block(monkeypatch, tmp_path):
    _arm_brain(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    tenant_brain.record_event("gym_a", "edit_diff",
                              before="b", after="a", rule="style")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(_voice(), _creative())      # account=None
    assert "LEARNED PREFERENCES" not in fake.user


def test_brain_flag_off_means_no_guidance(monkeypatch, tmp_path):
    # brain file exists but the flag is OFF -> reads are inert, no guidance
    bdir = str(tmp_path / "brains")
    monkeypatch.setattr(tenant_brain, "brains_dir", lambda base_dir=None: bdir)
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    tenant_brain.record_event("gym_a", "edit_diff", before="b", after="a", rule="style")
    monkeypatch.delenv("AGENT_TENANT_BRAIN_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(_voice(), _creative(), account=_acct("gym_a"))
    assert "LEARNED PREFERENCES" not in fake.user


# ---- fallback safety ----------------------------------------------------------

def test_llm_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: (_ for _ in ()).throw(RuntimeError("no key")))
    creative = _creative()
    caption, _hashtags, _fragments = StoryBrandGenerator().build(_voice(), creative)
    assert creative.client_note in caption                 # template's verbatim note


def test_hallucinated_number_falls_back_to_template(monkeypatch):
    """OUTPUT fabrication gate: if the LLM emits a figure NOT in the approved
    sources (note/voice), the caption is rejected and we fall back to the verbatim
    template. No invented stat/price/count ever reaches a caption."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    # LLM invents "40% off" and "$99" that are nowhere in the note or voice doc
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: "Get 40% off your first month for just $99.")
    creative = _creative(note="Our coaches meet you where you are.")
    caption, _h, _f = StoryBrandGenerator().build(_voice(), creative)
    assert "40%" not in caption and "$99" not in caption
    assert creative.client_note in caption          # fell back to the template


def test_real_number_in_note_passes_the_output_gate(monkeypatch):
    """A figure that IS in the client note passes (rephrasing is fine)."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: "579 five star reviews say it all. Come see why.")
    creative = _creative(note="We have 579 five star reviews from real members.")
    caption, _h, _f = StoryBrandGenerator().build(_voice(), creative)
    assert "579 five star reviews say it all" in caption   # SB7 output kept


def test_empty_client_note_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: (_ for _ in ()).throw(AssertionError("LLM ran")))
    caption, _h, _f = StoryBrandGenerator().build(_voice(), _creative(note=""))
    # no note -> template path -> at least the CTA lands, no LLM call
    assert "Book your intro session." in caption


def test_template_generator_accepts_account_kwarg():
    """Uniform interface: TemplateGenerator.build ignores account cleanly."""
    caption, _h, _f = TemplateGenerator().build(_voice(), _creative(), account=_acct())
    assert _creative().client_note in caption
