"""
ISSUE 3 (Dale, CrossFit ENG, 2026-08-15): "not sure Echo captured my reasoning ... the
reason why field disappeared." Two ECHO-side root causes:

  (a) DURABILITY: tenant_brain.brains_dir() defaulted to the repo-relative "brains",
      which on the deployed worker is /app/brains -- inside the container image, WIPED
      on every redeploy. So edits/reasons recorded to an ephemeral dir and vanished. Fix
      roots the brain under the persistent /data volume (config.tenant_brain_dir()).

  (b) THE REASON FIELD: the edit path recorded only before/after; a distinct "reason
      why" note (why the approver changed the caption) was dropped. Fix threads `reason`
      through the edit path and records it as the edit's `rule` so it teaches the prompt.

Offline: brain writes to a tmp dir; the reason flows through _learn_from_edit.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, portal_social, tenant_brain  # noqa: E402


# ---- (a) durable brain dir -----------------------------------------------------

def test_tenant_brain_dir_defaults_under_data_volume(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("AGENT_DATA_DIR", str(data))
    monkeypatch.delenv("AGENT_TENANT_BRAIN_DIR", raising=False)
    assert config.tenant_brain_dir() == str(data / "brains")


def test_tenant_brain_dir_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "custom_brains"))
    assert config.tenant_brain_dir() == str(tmp_path / "custom_brains")


def test_brains_dir_uses_config_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "b"))
    # no explicit base_dir -> the durable config dir, NOT the ephemeral "brains"
    assert tenant_brain.brains_dir() == str(tmp_path / "b")
    # an explicit base_dir still wins (tests pass a tmp dir)
    assert tenant_brain.brains_dir(str(tmp_path / "override")) == str(tmp_path / "override")


def test_brain_persists_to_durable_dir_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "brains"))
    ok = tenant_brain.record_event("eng_ig", "edit_diff",
                                   before="old", after="new")
    assert ok is True
    # the file landed under the durable dir, not a repo-relative "brains"
    assert os.path.isfile(str(tmp_path / "brains" / "eng_ig.md"))
    events = tenant_brain.read_events("eng_ig")
    assert events and events[-1]["after"] == "new"


# ---- (b) the reason-why field is captured, not dropped --------------------------

@pytest.fixture(autouse=True)
def _brain(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "brains"))
    yield


def test_learn_from_edit_records_reason_as_rule(tmp_path):
    portal_social._learn_from_edit(
        "eng_ig", before="Adult training focus.",
        after="Your kid's confidence is built here.",
        reason="This is a youth video, keep the caption youth focused.")
    rules = tenant_brain.style_rules("eng_ig")
    assert "youth focused" in " ".join(rules).lower()
    # the before/after pair is still recorded for the diff-learning signal
    ex = tenant_brain.edit_examples("eng_ig")
    assert ex and ex[-1][1].startswith("Your kid's confidence")


def test_learn_from_edit_reason_only_no_caption_change(tmp_path):
    # a reason with no caption change still teaches the gym's style preference
    portal_social._learn_from_edit(
        "eng_ig", before="Same caption.", after="Same caption.",
        reason="Always lead with the parent's time problem.")
    rules = tenant_brain.style_rules("eng_ig")
    assert "time problem" in " ".join(rules).lower()


def test_learn_from_edit_noop_without_change_or_reason(tmp_path):
    portal_social._learn_from_edit("eng_ig", before="x", after="x", reason="")
    assert tenant_brain.read_events("eng_ig") == []


def test_learn_from_edit_reason_reaches_the_prompt(monkeypatch, tmp_path):
    # end to end: a recorded reason surfaces in the SB7 brain guidance block
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    portal_social._learn_from_edit(
        "eng_ig", before="b", after="a real approved caption here",
        reason="Keep captions youth focused for kids content.")

    class _Acct:
        key = "eng_ig"

    from agent.drafter import StoryBrandGenerator
    guidance = StoryBrandGenerator._brain_guidance(_Acct())
    assert "youth focused" in guidance.lower()
