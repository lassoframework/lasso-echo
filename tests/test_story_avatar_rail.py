"""
Story Studio: the per-gym avatar rail. HYROX blocks every gym EXCEPT a per-gym
hyrox-avatar allowlisted client. The profile is per-gym CONFIG, never a hardcode.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import post_quality as pq  # noqa: E402


@pytest.fixture(autouse=True)
def _rail_armed(monkeypatch):
    """These tests describe the rail's behavior WHEN ARMED. Blake turned the rail off
    by default on 2026-09-01 (CrossFit, hyrox and competitive athletics are allowed),
    so the machinery is only exercised with the flag on."""
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")


def test_hyrox_blocks_all_gyms_by_default():
    # No allowlist -> the standard LASSO rail: hyrox breaches for every gym.
    assert pq.avatar_breach("BIRMINGHAM HYROX") == "HYROX"
    assert pq.avatar_breach("BIRMINGHAM HYROX", gym="anygym") == "HYROX"


def test_hyrox_allowed_for_allowlisted_gym(monkeypatch):
    monkeypatch.setenv("STORY_HYROX_AVATAR_GYMS", "birmingham")
    # birmingham's avatar IS hyrox -> no breach for birmingham only.
    assert pq.avatar_breach("BIRMINGHAM HYROX", gym="birmingham") == ""
    assert pq.avatar_breach("BIRMINGHAM HYROX", gym="birmingham_ig") == ""
    # a different gym still breaches.
    assert pq.avatar_breach("HYROX PREP", gym="pierce") == "HYROX"
    # no gym passed -> still all-gyms behavior (existing callers unchanged).
    assert pq.avatar_breach("HYROX PREP") == "HYROX"


def test_other_banned_terms_still_breach_even_for_hyrox_gym(monkeypatch):
    monkeypatch.setenv("STORY_HYROX_AVATAR_GYMS", "birmingham")
    # the hyrox allowlist does NOT open the door to the other banned-audience phrases.
    assert pq.avatar_breach("STRENGTH ATHLETES WANTED", gym="birmingham")
    # a caption mixing an allowed hyrox with a still-banned term breaches on the term.
    b = pq.avatar_breach("BIRMINGHAM HYROX FOR SERIOUS ATHLETES", gym="birmingham")
    assert b and "hyrox" not in b.lower()


def test_no_env_leak_between_gyms(monkeypatch):
    monkeypatch.setenv("STORY_HYROX_AVATAR_GYMS", "birmingham, downtown")
    assert pq.avatar_breach("HYROX", gym="downtown") == ""
    assert pq.avatar_breach("HYROX", gym="birmingham") == ""
    assert pq.avatar_breach("HYROX", gym="northside") == "HYROX"


def test_rail_off_by_default_allows_competitive_athlete_language(monkeypatch):
    """Blake 2026-09-01: "confirmed we can talk about CrossFit, hyrox and competitive
    athletics." With the rail at its default (off) NOTHING breaches on avatar grounds,
    at stage or at the publish boundary."""
    monkeypatch.delenv("AGENT_AVATAR_ATHLETE_RAIL", raising=False)
    for caption in ("BIRMINGHAM HYROX", "Built for competitive CrossFit",
                    "For strength athletes", "serious athletes only"):
        assert pq.avatar_breach(caption) == ""
        assert pq.avatar_breach(caption, gym="anygym") == ""


def test_rail_can_be_re_armed_with_one_env_var(monkeypatch):
    """The machinery is kept intact so the positioning can be tightened again."""
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")
    assert pq.avatar_breach("BIRMINGHAM HYROX") == "HYROX"
