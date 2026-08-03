"""Tests for agent.podcast_audit - the level-2 standing audit gate (offline)."""

import copy

from agent import podcast_audit as pa


EP = 140
TRANSCRIPT = ("Speed to lead is the whole game. Your close rate is the first leg "
              "to fix. A busy professional wants a result not a workout.")


def _clip(name):
    return {
        "name": name,
        "path": f"/out/{name}.mp4",
        "captioned": True,
        "has_ghost_caption": False,
        "intro_animated": True,
        "bottom_treatment_ok": True,
        "static_takeover": False,
        "caption_free_variant": f"/out/{name}-nocap.mp4",
    }


def _clean_set():
    return {
        "clips": [_clip(n) for n in pa.expected_clip_names(EP)],
        "audiogram": {"name": f"GMMS-{EP}-audiogram", "path": "/out/audiogram.mp4"},
        "quote_cards": [
            {"path": "/out/q1.png", "quote_text": "Speed to lead is the whole game",
             "verbatim_ok": True},
            {"path": "/out/q2.png", "quote_text": "Your close rate is the first leg to fix",
             "verbatim_ok": True},
            {"path": "/out/q3.png", "quote_text": "A busy professional wants a result",
             "verbatim_ok": True},
        ],
    }


# ---- expected_clip_names -------------------------------------------------------------
def test_expected_clip_names():
    assert pa.expected_clip_names(140) == [
        "GMMS-140-S1", "GMMS-140-S2", "GMMS-140-S3", "GMMS-140-S4"]


# ---- clean set passes ----------------------------------------------------------------
def test_clean_set_passes():
    r = pa.audit_episode(_clean_set(), EP)
    assert r.passed is True
    assert r.failures == []
    assert all(r.checks.values())


def test_clean_set_passes_with_transcript():
    r = pa.audit_episode(_clean_set(), EP, transcript_text=TRANSCRIPT)
    assert r.passed is True


def test_clean_set_passes_with_four_quote_cards():
    s = _clean_set()
    s["quote_cards"].append(
        {"path": "/out/q4.png", "quote_text": "A result not a workout",
         "verbatim_ok": True})
    assert pa.audit_episode(s, EP).passed is True


# ---- each broken check is caught -----------------------------------------------------
def test_ghost_caption_caught():
    s = _clean_set()
    s["clips"][0]["has_ghost_caption"] = True
    r = pa.audit_episode(s, EP)
    assert r.passed is False
    assert r.checks["CAPTIONS"] is False
    assert any("ghost" in f.lower() for f in r.failures)


def test_missing_s3_caught():
    s = _clean_set()
    # drop S3 by renaming it to a bogus name (still 4 clips, but S3 missing)
    s["clips"][2]["name"] = "GMMS-140-BOGUS"
    r = pa.audit_episode(s, EP)
    assert r.passed is False
    assert r.checks["QUOTA"] is False
    assert any("GMMS-140-S3" in f for f in r.failures)


def test_wrong_clip_count_caught():
    s = _clean_set()
    s["clips"] = s["clips"][:3]
    r = pa.audit_episode(s, EP)
    assert r.checks["QUOTA"] is False
    assert any("expected 4 clips" in f for f in r.failures)


def test_wrong_quote_card_count_caught():
    s = _clean_set()
    s["quote_cards"] = s["quote_cards"][:2]  # only 2, need 3 or 4
    r = pa.audit_episode(s, EP)
    assert r.passed is False
    assert r.checks["QUOTE_VERBATIM"] is True   # the two present are fine
    assert r.checks["QUOTA"] is False
    assert any("3 or 4 quote cards" in f for f in r.failures)


def test_missing_audiogram_caught():
    s = _clean_set()
    s["audiogram"] = None
    r = pa.audit_episode(s, EP)
    assert r.checks["QUOTA"] is False
    assert any("audiogram" in f.lower() for f in r.failures)


def test_static_takeover_caught():
    s = _clean_set()
    s["clips"][1]["static_takeover"] = True
    r = pa.audit_episode(s, EP)
    assert r.checks["NO_STATIC_TAKEOVER"] is False
    assert any("static" in f.lower() for f in r.failures)


def test_static_intro_caught():
    s = _clean_set()
    s["clips"][0]["intro_animated"] = False
    r = pa.audit_episode(s, EP)
    assert r.checks["INTRO"] is False


def test_bottom_treatment_caught():
    s = _clean_set()
    s["clips"][3]["bottom_treatment_ok"] = False
    r = pa.audit_episode(s, EP)
    assert r.checks["BOTTOM"] is False


def test_missing_caption_free_variant_caught():
    s = _clean_set()
    s["clips"][0]["caption_free_variant"] = None
    r = pa.audit_episode(s, EP)
    assert r.checks["CAPTION_FREE"] is False
    assert any("caption-free" in f.lower() for f in r.failures)


def test_non_verbatim_quote_flagged_ok_but_not_in_transcript():
    s = _clean_set()
    s["quote_cards"][0]["quote_text"] = "This line was never spoken on the episode"
    r = pa.audit_episode(s, EP, transcript_text=TRANSCRIPT)
    assert r.checks["QUOTE_VERBATIM"] is False
    assert any("verbatim" in f.lower() for f in r.failures)


def test_quote_not_verbatim_ok_flag_caught():
    s = _clean_set()
    s["quote_cards"][1]["verbatim_ok"] = False
    r = pa.audit_episode(s, EP)
    assert r.checks["QUOTE_VERBATIM"] is False


# ---- regenerate_or_flag --------------------------------------------------------------
def test_regen_not_called_when_clean():
    calls = []

    def regen(failure):
        calls.append(failure)
        return None

    r = pa.regenerate_or_flag(_clean_set(), EP, regen)
    assert r.passed is True
    assert calls == []   # a clean set never triggers a regeneration


def test_regen_called_once_and_fixes():
    calls = []
    broken = _clean_set()
    broken["clips"][0]["has_ghost_caption"] = True

    def regen(failure):
        calls.append(failure)
        fixed = copy.deepcopy(broken)
        fixed["clips"][0]["has_ghost_caption"] = False
        return fixed

    r = pa.regenerate_or_flag(broken, EP, regen)
    assert calls == ["CAPTIONS"]     # called exactly once, for the first failure
    assert r.passed is True
    assert r.flagged_check == ""


def test_regen_called_once_then_flags_if_still_broken():
    calls = []
    broken = _clean_set()
    broken["clips"][0]["static_takeover"] = True

    def regen(failure):
        calls.append(failure)
        return None   # regeneration fails to actually fix anything

    r = pa.regenerate_or_flag(broken, EP, regen)
    assert len(calls) == 1           # exactly once, never a loop
    assert r.passed is False
    assert r.flagged_check == "NO_STATIC_TAKEOVER"
