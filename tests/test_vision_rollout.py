"""
P6 — rollout (ECHO_VISION_SPEC §9): the per-gym enablement flag (default OFF for all), the
§9.4 shadow flag, the ruling-2 per-gym MONTHLY vision-call cap (runaway guard, alarm-once),
and the LASSO dogfood diff (old-picks vs new-picks — the go/no-go deliverable). Offline:
sidecars carry stored analyses so scoring runs without image generation or a network call.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import vision, config, vision_dogfood  # noqa: E402


# ---- per-gym enablement flag (default OFF for all) -------------------------------------

def test_vision_flag_default_off_for_all(monkeypatch):
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)
    assert config.vision_gyms() == set()
    assert config.vision_enabled_for("lasso") is False
    assert config.vision_enabled_for("gritx_ig") is False


def test_vision_flag_matches_base_and_suffix(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYMS", "lasso, gritx")
    assert config.vision_enabled_for("lasso") is True
    assert config.vision_enabled_for("lasso_ig") is True     # suffix stripped to base
    assert config.vision_enabled_for("gritx_fb") is True
    assert config.vision_enabled_for("northgym") is False    # not in the list


def test_shadow_is_independent_of_enablement(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_SHADOW", "northgym")
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)
    # shadow runs analysis/scoring but does NOT flip picks: enabled stays False
    assert config.vision_shadow_for("northgym_ig") is True
    assert config.vision_enabled_for("northgym_ig") is False


# ---- ruling 2: per-gym monthly vision-call cap ----------------------------------------

def test_within_gym_budget_counts_and_caps(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYM_MONTHLY_CAP", "3")
    day = "2026-09-04"
    assert config.vision_gym_monthly_cap() == 3
    assert vision.within_gym_budget("lasso", day) is True   # 1
    assert vision.within_gym_budget("lasso", day) is True   # 2
    assert vision.within_gym_budget("lasso", day) is True   # 3
    assert vision.within_gym_budget("lasso", day) is False  # cap hit


def test_within_gym_budget_is_per_gym_and_per_month(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYM_MONTHLY_CAP", "1")
    assert vision.within_gym_budget("lasso", "2026-09-01") is True
    assert vision.within_gym_budget("lasso", "2026-09-30") is False  # same month, capped
    assert vision.within_gym_budget("lasso", "2026-10-01") is True   # new month resets
    assert vision.within_gym_budget("gritx", "2026-09-02") is True   # other gym untouched


def test_within_gym_budget_alarms_once(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYM_MONTHLY_CAP", "1")
    alerts = []
    day = "2026-09-05"
    assert vision.within_gym_budget("lasso", day, alert=alerts.append) is True
    vision.within_gym_budget("lasso", day, alert=alerts.append)   # first over-cap -> alarm
    vision.within_gym_budget("lasso", day, alert=alerts.append)   # still over -> no re-alarm
    assert len(alerts) == 1
    assert "monthly vision-call cap" in alerts[0]


def test_within_gym_budget_disabled_when_cap_zero(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYM_MONTHLY_CAP", "0")
    for _ in range(50):
        assert vision.within_gym_budget("lasso", "2026-09-01") is True   # never caps


def test_within_gym_budget_noop_without_gym(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYM_MONTHLY_CAP", "1")
    assert vision.within_gym_budget("", "2026-09-01") is True
    assert vision.within_gym_budget(None, "2026-09-01") is True


# ---- LASSO dogfood diff (old-picks vs new-picks) --------------------------------------

def _asset(lib, name, analysis):
    open(os.path.join(lib, name), "wb").close()
    stem = os.path.splitext(name)[0]
    with open(os.path.join(lib, stem + ".json"), "w") as fh:
        json.dump({"media_analysis": {**analysis, "version": 2}}, fh)


def _plannable(setting, activity, people="small_group"):
    """A clean, plannable analysis for the given content."""
    return {"setting": setting, "activity": activity, "people": {"bucket": people},
            "one_line": f"a {activity} moment in the {setting}",
            "subjects": [activity], "visible_details": [setting],
            "text_in_image": "", "safety_flags": [], "avatar_fit": "gen_pop",
            "quality": {"usable": True, "sharp": True, "well_lit": True},
            "phash": "0000000000000000"}


def test_dogfood_diff_prefers_content_matched_photo(tmp_path):
    lib = str(tmp_path)
    # a testimonial-fit photo (a person + a result vibe) and a generic gym-floor photo
    _asset(lib, "coach_hi.png", _plannable("gym_floor", "coaching", people="one_on_one"))
    _asset(lib, "empty_floor.png", _plannable("gym_floor", "equipment", people="none"))
    rows = vision_dogfood.pick_diff("lasso_ig", lib, ["service", "about"], served=[])
    # every row names an off + on pick and an explaining reason
    assert len(rows) == 2
    for r in rows:
        assert r["vision_on"] in ("coach_hi.png", "empty_floor.png")
        assert r["reason"]
        assert r["no_pick"] is False           # a plannable image existed


def test_dogfood_diff_flags_no_plannable_image(tmp_path):
    lib = str(tmp_path)
    # only an unusable image -> nothing plannable for any slot
    _asset(lib, "blurry.png", {"usable": False, "setting": "unknown",
                               "activity": "unknown", "people": "none",
                               "one_line": "", "subjects": [], "visible_details": [],
                               "text_in_image": "", "safety_flags": [],
                               "phash": "0000000000000000"})
    rows = vision_dogfood.pick_diff("lasso_ig", lib, ["service"], served=[])
    assert rows[0]["vision_on"] is None
    assert rows[0]["no_pick"] is True
    assert rows[0]["swapped"] is False          # no_pick is NOT counted as an upgrade
    assert "no plannable image" in rows[0]["reason"]


def test_dogfood_report_renders(tmp_path):
    lib = str(tmp_path)
    _asset(lib, "coach_hi.png", _plannable("gym_floor", "coaching"))
    rows = vision_dogfood.pick_diff("lasso_ig", lib, ["service"], served=[])
    report = vision_dogfood.format_report("lasso_ig", rows)
    assert "dogfood diff" in report
    assert "lasso_ig" in report
