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


# ---- cross-day OPENING variety (Ryan Parr, 2026-08-17) ------------------------

def test_opening_signature_normalizes_hook():
    """The signature is the normalized leading words of the first real (non-hashtag)
    line; a hashtag-only first line is skipped."""
    a = drafter.opening_signature("You're juggling too much today.")
    assert a == "youre juggling too much today"
    assert drafter.opening_signature("") == ""
    # a hashtag-only first line is skipped; the signature comes from the real body line
    assert drafter.opening_signature(
        "#gym\nYou're juggling too much today.") == a


def test_openings_collide_detects_shared_hook():
    """Two captions leading with the same hook COLLIDE even when the words after the
    hook differ (Ryan's exact case); a different opener does not collide."""
    avoid = ["you're juggling too much"]
    assert drafter.openings_collide(
        "You're juggling too much and it never stops.", avoid)
    assert drafter.openings_collide(
        "Youre juggling too much this week, we get it.", avoid)
    assert not drafter.openings_collide(
        "Ready to feel strong again? Start here.", avoid)
    assert not drafter.openings_collide("anything", [])
    assert not drafter.openings_collide("", avoid)


def test_avoid_openings_reaches_the_prompt(monkeypatch):
    """The recent openings are folded into the LLM prompt as a HARD 'do not open like
    these' instruction (STYLE-only, never a fact)."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    fake = FakeLLM(body="Ready to feel strong again? We meet you where you are.")
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(
        _voice(), _creative(),
        avoid_openings=["you're juggling too much", "you're swamped"])
    assert "OPENINGS ALREADY USED" in fake.user
    assert "you're juggling too much" in fake.user
    assert "you're swamped" in fake.user


def test_no_avoid_openings_leaves_prompt_unchanged(monkeypatch):
    """A brand-new gym / the first day passes no avoid list -> no avoid block, same
    prompt as before."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    fake = FakeLLM()
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    StoryBrandGenerator().build(_voice(), _creative())     # avoid_openings default ()
    assert "OPENINGS ALREADY USED" not in fake.user


def test_colliding_opening_retries_once_and_prefers_varied(monkeypatch):
    """When the first generation still collides with a recent opening, the generator
    retries ONCE and keeps the more varied result."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    calls = {"n": 0}

    def _fake(system, user):
        calls["n"] += 1
        # first attempt collides with the avoid list; retry (stronger nudge) varies
        return ("You're juggling too much and it never stops."
                if calls["n"] == 1
                else "Strength starts with one honest hour a week.")

    monkeypatch.setattr(drafter, "_call_llm_caption", _fake)
    caption, _h, _f = StoryBrandGenerator().build(
        _voice(), _creative(), avoid_openings=["you're juggling too much"])
    assert calls["n"] == 2                              # retried exactly once
    assert "Strength starts with one honest hour" in caption
    assert "juggling too much" not in caption.lower()


def test_colliding_opening_never_blocks_the_post(monkeypatch):
    """If even the retry still collides, a caption is STILL produced (the varied hook
    is preferred but never required; a post is never blocked over opener repetition)."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    fake = FakeLLM(body="You're juggling too much, every single day.")
    monkeypatch.setattr(drafter, "_call_llm_caption", fake)
    caption, _h, _f = StoryBrandGenerator().build(
        _voice(), _creative(), avoid_openings=["you're juggling too much"])
    assert caption.strip()                              # a caption is always produced
    assert "Book your intro session." in caption        # CTA still appended


# ---- scaffold-leak + retry-once (audit 2026-08-25 CRITICAL) --------------------

_AUGMENTED_NOTE = (
    "Try our 6 week transformation challenge for busy parents."
    "\n\nWHAT THIS POST'S PHOTO/VIDEO SHOWS (reference this so the caption matches the "
    "image; it is a scene hint, NOT a source of facts, numbers, offers, or names to "
    "state): Youth fitness fun\nVERIFIED IN THE IMAGE: a small_group; visible: kids. "
    "Do NOT call it a crowd/packed unless the grouping is crowd."
)


def test_template_fallback_never_leaks_hint_scaffolding(monkeypatch):
    """CRITICAL: when SB7 falls back to the template, the caption must carry ONLY the
    approved source text — never the internal scene/grounding hint blocks appended for
    the LLM (these leaked verbatim into client calendars)."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    # the LLM always emits an unsourced figure -> figure gate rejects the first AND the
    # retry -> template fallback
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: "Get 40% off your first month.")
    creative = _creative(note=_AUGMENTED_NOTE)
    caption, _h, _f = StoryBrandGenerator().build(_voice(), creative)
    assert "WHAT THIS POST'S PHOTO/VIDEO SHOWS" not in caption
    assert "VERIFIED IN THE IMAGE" not in caption
    assert "6 week transformation challenge" in caption     # the real source survives


def test_llm_error_fallback_never_leaks_hint_scaffolding(monkeypatch):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.setattr(drafter, "_call_llm_caption",
                        lambda s, u: (_ for _ in ()).throw(RuntimeError("boom")))
    caption, _h, _f = StoryBrandGenerator().build(_voice(), _creative(note=_AUGMENTED_NOTE))
    assert "WHAT THIS POST'S PHOTO/VIDEO SHOWS" not in caption
    assert "VERIFIED IN THE IMAGE" not in caption
    assert "6 week transformation challenge" in caption


def test_figure_gate_retry_once_rescues_the_day(monkeypatch):
    """A single stray digit no longer costs the whole day a written caption: one
    regeneration (told: no digits) is attempted, and a clean retry is KEPT instead of
    falling back to the template."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    bodies = iter(["Get 40% off your first month for just $99.",     # rejected
                   "Busy week? One coached hour puts you back in charge."])  # clean retry
    calls = []
    def _fake(s, u):
        calls.append(u)
        return next(bodies)
    monkeypatch.setattr(drafter, "_call_llm_caption", _fake)
    creative = _creative(note="Our coaches meet you where you are.")
    caption, _h, _f = StoryBrandGenerator().build(_voice(), creative)
    assert "40%" not in caption and "$99" not in caption
    assert "back in charge" in caption                     # the retry shipped
    assert len(calls) == 2                                  # exactly one retry
    assert "NO digits" in calls[1]                          # the nudge was threaded


def test_hint_free_helper_strips_only_the_hint():
    c = _creative(note=_AUGMENTED_NOTE)
    clean = drafter._hint_free(c)
    assert clean.client_note == "Try our 6 week transformation challenge for busy parents."
    # no marker -> the SAME object back, untouched
    c2 = _creative(note="Plain approved source text.")
    assert drafter._hint_free(c2) is c2
