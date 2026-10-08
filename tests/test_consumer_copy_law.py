"""Consumer copy law (Blake, 2026-10-07): no hyphens, dashes, colons or semicolons
in client captions, SB7 or template; clean hook breaks; own voice doc for SB7;
non-empty hashtags."""
import types

from agent import copy_gate, drafter, client_content, post_quality


def test_scrub_rewrites_every_hyphen_colon_semicolon():
    s = copy_gate.scrub_caption("Free 30-minute assessment. Type: semi-private; well - being -- yes")
    assert s == "Free 30 minute assessment. Type, semi private, well, being, yes"
    assert copy_gate.caption_violations(s) == []


def test_scrub_clock_bullets_and_protected_spans():
    assert copy_gate.scrub_caption("Class at 6:00 and 6:30") == "Class at 6 and 6.30"
    assert copy_gate.scrub_caption("- Free trial class") == "Free trial class"
    out = copy_gate.scrub_caption("Book at https://zanshin.fit/free-trial now")
    assert "https://zanshin.fit/free-trial" in out


def test_violations_catch_digit_letter_hyphen_and_colon():
    assert "hyphen" in copy_gate.caption_violations("Free 30-minute assessment")
    assert "colon" in copy_gate.caption_violations("Type: gym")
    assert "semicolon" in copy_gate.caption_violations("a; b")
    assert copy_gate.caption_violations("See https://x.com/a-b:c") == []


def test_post_quality_flags_copy_law():
    issues = post_quality.post_issues if hasattr(post_quality, "post_issues") else None
    d = drafter.Draft(draft_id="x", account_key="g_ig", platform="instagram",
                      caption=("Strength that lasts starts with one honest session. "
                               "Our coaches meet you where you are. Free 30-minute assessment."),
                      hashtags=[], creative_path="", creative_public_url="https://x/y.jpg",
                      scheduled_for="2026-10-20")
    probs = issues(d, (), require_media=False)
    assert any("consumer copy law" in p for p in probs)


def test_hook_breaks_at_sentence_boundary():
    text = ("You've started a hundred times. Life gets in the way. Work piles up. "
            "Motivation fades. At Zanshin, we don't expect you to figure it out alone.")
    out = drafter._bound_opening_hook(text)
    first = out.splitlines()[0]
    assert len(first) <= drafter._HOOK_MAX_CHARS
    assert first.endswith(".")
    assert out.replace("\n", " ") == text


def test_prompt_carries_copy_law():
    sysp = drafter.StoryBrandGenerator._SYSTEM
    assert "NO hyphens" in sysp and "NO colons" in sysp and "NO semicolons" in sysp


def test_no_own_voice_doc_stays_on_template(tmp_path, monkeypatch):
    lasso = tmp_path / "lasso_voice.md"
    lasso.write_text("LASSO bible\n")
    monkeypatch.setattr(client_content.config, "VOICE_DOC_PATH", str(lasso))
    acct = types.SimpleNamespace(key="mindbodysoulfitness2be97e_ig", voice_doc="")
    assert client_content.has_own_voice_doc(acct, types.SimpleNamespace(raw="LASSO bible")) is False
    assert client_content.has_own_voice_doc(acct, types.SimpleNamespace(raw="Gym bible")) is True
    assert client_content.has_own_voice_doc(types.SimpleNamespace(key="lasso_ig"),
                                            types.SimpleNamespace(raw="LASSO bible")) is True


def test_make_caption_skips_sb7_without_own_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(client_content.config, "sb7_enabled", lambda: True)
    monkeypatch.setattr(client_content, "has_own_voice_doc", lambda a, v=None: False)
    called = []

    class Boom:
        def build(self, *a, **k):
            called.append(1)
            raise AssertionError("SB7 must not run")
    monkeypatch.setattr(drafter, "StoryBrandGenerator", Boom)
    monkeypatch.setattr(client_content, "compose_caption",
                        lambda *a, **k: ("template caption", ["#x"]))
    acct = types.SimpleNamespace(key="g_ig", platform="instagram", display_name="G")
    src = types.SimpleNamespace(text="fact")
    assert client_content.make_caption(acct, src, None, "k") == ("template caption", ["#x"])
    assert not called


def test_fallback_hashtags_from_display_name():
    acct = types.SimpleNamespace(display_name="CrossFit Zanshin (Instagram)", platform="instagram")
    tags = client_content.fallback_hashtags(acct)
    assert tags and tags[0] == "#CrossFitZanshin"
    assert all(t.startswith("#") and "-" not in t for t in tags)
    assert len(tags) <= 5


def test_generic_scrub_unchanged_for_internal_copy():
    assert copy_gate.scrub("Denies this month: 4") == "Denies this month: 4"
    assert copy_gate.violations("one thing only you can do: sell.") == []


def test_numeric_meaning_preserved():
    assert copy_gate.scrub_caption("Ages 8-12") == "Ages 8 to 12"
    assert copy_gate.scrub_caption("It was -5 degrees") == "It was negative 5 degrees"
    assert copy_gate.scrub_caption("Coaching 1:1") == "Coaching 1 to 1"
    assert copy_gate.scrub_caption("Class at 6:30") == "Class at 6.30"


def test_fallback_hashtag_platform_limits():
    fb = types.SimpleNamespace(display_name="Gym X", platform="facebook")
    gbp = types.SimpleNamespace(display_name="Gym X", platform="googlebusiness")
    assert len(client_content.fallback_hashtags(fb)) == 2
    assert client_content.fallback_hashtags(gbp) == []


def test_lasso_prefixed_client_is_not_exempt(tmp_path, monkeypatch):
    lasso = tmp_path / "lasso_voice.md"
    lasso.write_text("LASSO bible")
    monkeypatch.setattr(client_content.config, "VOICE_DOC_PATH", str(lasso))
    acct = types.SimpleNamespace(key="lassofitness_ig")
    assert client_content.has_own_voice_doc(acct, types.SimpleNamespace(raw="LASSO bible")) is False
