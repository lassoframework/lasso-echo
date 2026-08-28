"""
tests/test_ask_coverage.py — the LASSO-lane ask rules (AGENT_ASK_COVERAGE,
default OFF; report-card build 2026-08-28).

  * REEL RULE: every VIDEO/REEL feed draft carries EXACTLY ONE ask family
    (one destination per POST — nothing bio-related is touched or assumed).
  * COVERAGE FLOOR: a planned month lands >= the configured floor (default
    70%) of feed drafts with an ask, leaving genuine no-ask room
    (testimonial / proof / welcome stay askless while the floor holds).
  * copy_gate: every enforced caption passes violations() (no dashes).
  * WIRING: apply_month_plan enforces at build time on the B2B lane only,
    flag-gated.
All offline.
"""
from __future__ import annotations

import pytest

from agent import ask_coverage as ac
from agent import copy_gate
from agent.publish_guard import ask_families


class _Draft:
    def __init__(self, caption, category="doctrine", day_key="2026-09-01",
                 url="https://cdn/x.jpg", is_story=False):
        self.caption = caption
        self.category = category
        self.day_key = day_key
        self.creative_public_url = url
        self.creative_path = url.rsplit("/", 1)[-1]
        self.is_story = is_story
        self.draft_type = "story" if is_story else "feed"


# ---------------------------------------------------------------------------
# ensure_single_ask
# ---------------------------------------------------------------------------

def test_no_ask_gets_the_default_appended():
    out, changed = ac.ensure_single_ask("Real people, real results this month.")
    assert changed
    assert ask_families(out) == ["book"]
    assert copy_gate.violations(out) == []


def test_one_ask_is_left_alone():
    cap = "We rebuilt the funnel. DM us GROW to see how."
    out, changed = ac.ensure_single_ask(cap)
    assert not changed and out == cap


def test_two_ask_families_pruned_to_one_destination():
    cap = ("Coaches doubled their close rate. Book a call today. "
           "Also comment GROW below. And DM us for the checklist.")
    out, changed = ac.ensure_single_ask(cap)
    assert changed
    assert len(ask_families(out)) == 1
    assert copy_gate.violations(out) == []
    # the surviving copy is a subset of the original + the approved default:
    # nothing new was invented
    for word in ("doubled", "close rate"):
        assert word in out


def test_one_sentence_with_two_families_replaced_by_default():
    cap = "Great month for our gyms. Book a call or DM us GROW."
    out, changed = ac.ensure_single_ask(cap)
    assert changed
    assert ask_families(out) == ["book"]
    assert "Great month for our gyms." in out


def test_default_ask_passes_copy_gate_and_is_one_family():
    assert copy_gate.violations(ac.DEFAULT_ASK) == []
    assert len(ask_families(ac.DEFAULT_ASK)) == 1


# ---------------------------------------------------------------------------
# enforce_drafts: reels + floor over a planned month
# ---------------------------------------------------------------------------

def _month(n_no_ask=10, n_with_ask=4, n_reels=4, n_proof=2):
    drafts = []
    for i in range(n_no_ask):
        drafts.append(_Draft(f"No ask post number {i} with a real body.",
                             day_key=f"2026-09-{i+1:02d}"))
    for i in range(n_with_ask):
        drafts.append(_Draft(f"Asked post {i}. Sign up today.",
                             day_key=f"2026-09-{i+11:02d}"))
    for i in range(n_reels):
        drafts.append(_Draft(f"Reel {i} about member wins, no ask yet.",
                             day_key=f"2026-09-{i+15:02d}",
                             url=f"https://cdn/clip{i}.mp4"))
    for i in range(n_proof):
        drafts.append(_Draft(f"Fit Mamas Tribe proof entry {i}, owner voice.",
                             category="testimonial",
                             day_key=f"2026-09-{i+19:02d}"))
    # stories mirror captions and must never be touched
    drafts.append(_Draft("", is_story=True))
    return drafts


def test_month_meets_floor_and_every_reel_has_exactly_one_ask():
    drafts = _month()
    summary = ac.enforce_drafts(drafts, floor=0.70)
    feeds = [d for d in drafts if not d.is_story]
    covered = sum(1 for d in feeds if ask_families(d.caption))
    assert covered / len(feeds) >= 0.70
    assert summary["coverage"] >= 0.70
    for d in feeds:
        if ac.is_video_draft(d):
            assert len(ask_families(d.caption)) == 1, d.caption
        assert copy_gate.violations(d.caption) == []


def test_genuine_no_ask_room_is_preserved():
    # floor reachable WITHOUT touching the testimonial drafts -> they stay askless
    drafts = _month(n_no_ask=4, n_with_ask=8, n_reels=4, n_proof=2)
    ac.enforce_drafts(drafts, floor=0.70)
    proof = [d for d in drafts if d.category == "testimonial"]
    assert proof and all(not ask_families(d.caption) for d in proof)


def test_stories_and_empty_captions_never_ask_padded():
    story = _Draft("", is_story=True)
    blank = _Draft("")
    ac.enforce_drafts([story, blank], floor=1.0)
    assert story.caption == "" and blank.caption == ""


def test_floor_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_ASK_COVERAGE_FLOOR", "50")
    from agent import config
    assert config.ask_coverage_floor() == 50
    monkeypatch.setenv("AGENT_ASK_COVERAGE_FLOOR", "999")
    assert config.ask_coverage_floor() == 100
    monkeypatch.delenv("AGENT_ASK_COVERAGE_FLOOR", raising=False)
    assert config.ask_coverage_floor() == 70


# ---------------------------------------------------------------------------
# Wiring: apply_month_plan enforces on the B2B (LASSO) lane, flag-gated
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.inserted = []

    def list_month(self, account_key, month):
        return []

    def locked_slots(self, account_key, month):
        return set()

    def delete_month(self, account_key, month, **kw):
        return 0

    def insert_rows(self, account_key, rows):
        out = [dict(r, id=f"n{i}") for i, r in enumerate(rows)]
        self.inserted.extend(out)
        return out


def _real_drafts():
    from agent.drafter import Draft, DraftStatus
    drafts = []
    for i in range(6):
        drafts.append(Draft(
            draft_id=f"d{i}", account_key="lasso", platform="instagram",
            caption=f"Post {i} body with no ask and enough words to be real.",
            hashtags=[], creative_path=f"x{i}.png",
            creative_public_url=f"https://cdn/x{i}.jpg", scheduled_for="",
            status=DraftStatus.PENDING, day_key=f"2026-09-{i+1:02d}",
            draft_type="feed", category="doctrine"))
    drafts.append(Draft(
        draft_id="v0", account_key="lasso", platform="instagram",
        caption="A reel about client wins with no ask.", hashtags=[],
        creative_path="clip.mp4", creative_public_url="https://cdn/clip.mp4",
        scheduled_for="", status=DraftStatus.PENDING, day_key="2026-09-08",
        draft_type="podcast", category="podcast"))
    return drafts


def test_apply_month_plan_enforces_asks_when_armed(monkeypatch):
    monkeypatch.setenv("AGENT_ASK_COVERAGE", "true")
    monkeypatch.delenv("AGENT_CALENDAR_GRADE", raising=False)
    from agent import real_month_planner as rmp
    store = _FakeStore()
    res = rmp.apply_month_plan("lasso", _real_drafts(), store,
                               span_months=["2026-09"])
    assert res["ok"] is True
    feeds = [r for r in store.inserted if r.get("format") == "feed"]
    assert feeds
    covered = sum(1 for r in feeds if ask_families(r.get("caption") or ""))
    assert covered / len(feeds) >= 0.70
    reels = [r for r in feeds
             if str(r.get("image_url", "")).endswith(".mp4")]
    assert reels
    for r in reels:
        assert len(ask_families(r["caption"])) == 1


def test_apply_month_plan_flag_off_leaves_captions_alone(monkeypatch):
    monkeypatch.delenv("AGENT_ASK_COVERAGE", raising=False)
    monkeypatch.delenv("AGENT_CALENDAR_GRADE", raising=False)
    from agent import real_month_planner as rmp
    store = _FakeStore()
    rmp.apply_month_plan("lasso", _real_drafts(), store, span_months=["2026-09"])
    feeds = [r for r in store.inserted if r.get("format") == "feed"]
    assert all(not ask_families(r.get("caption") or "")
               or "Sign up" in r["caption"] for r in feeds)
    # specifically: the no-ask doctrine posts were NOT padded
    assert any(not ask_families(r.get("caption") or "") for r in feeds)


def test_gym_lane_untouched_even_when_armed(monkeypatch):
    monkeypatch.setenv("AGENT_ASK_COVERAGE", "true")
    monkeypatch.delenv("AGENT_CALENDAR_GRADE", raising=False)
    from agent import real_month_planner as rmp
    from agent.drafter import Draft, DraftStatus
    drafts = [Draft(
        draft_id="g0", account_key="ironworks", platform="instagram",
        caption="A gym post with no ask at all.", hashtags=[],
        creative_path="g.png", creative_public_url="https://cdn/g.jpg",
        scheduled_for="", status=DraftStatus.PENDING, day_key="2026-09-01",
        draft_type="feed", category="doctrine")]
    store = _FakeStore()
    rmp.apply_month_plan("ironworks", drafts, store, span_months=["2026-09"])
    feeds = [r for r in store.inserted if r.get("format") == "feed"]
    assert feeds and all(not ask_families(r["caption"]) for r in feeds)
