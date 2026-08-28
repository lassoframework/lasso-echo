"""
gym_event arc planner + grounding, all offline (no I/O, no network, no writes).

Covers EVENT_CAMPAIGNS_BUILD.md §5 for the planner/grounding legs:
  * full-lead event -> full arc (announce/how-it-works/last-call/during/final/recap)
  * 2-day notice -> compresses (announce+how-it-works merge, no T-7)
  * created after start -> during + final + recap only, with a note
  * dated posts fire in the GYM'S tz (a Pacific gym vs UTC rollover)
  * copy contains no fact absent from the form (property test on drafted arcs)
  * recap post blocked until real event media exists
  * every arc row lands pending
  * copy_gate (no dashes) + one-ask on every non-recap draft
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_event as ge
from agent import copy_gate


def _event(**over):
    base = dict(
        id="evt_baf_x", gym_id="pete", name="Bring a Friend Week",
        type="bring_a_friend", starts_on="2026-09-22", ends_on="2026-09-28",
        tz="America/New_York",
        offer_text="Your partner trains free all week",
        link="https://petegym.com/baf", brief="Who are you bringing?",
        media_ids=(), status="scheduled",
    )
    base.update(over)
    return ge.GymEvent.from_row(base)


# ---- validation ----------------------------------------------------------------

def test_bad_type_raises():
    with pytest.raises(ValueError):
        _event(type="not_a_type")


def test_ends_before_starts_raises():
    with pytest.raises(ValueError):
        _event(starts_on="2026-09-28", ends_on="2026-09-22")


def test_bad_tz_raises():
    with pytest.raises(ValueError):
        _event(tz="Mars/Olympus")


# ---- full lead -> full arc -----------------------------------------------------

def test_full_lead_full_arc():
    ev = _event()
    arc = ge.plan_arc(ev, today=date(2026, 9, 1))  # ~3 weeks out
    kinds = [p.kind for p in arc]
    assert ge.ANNOUNCE in kinds
    assert ge.HOW_IT_WORKS in kinds
    assert ge.LAST_CALL in kinds
    assert ge.DURING in kinds
    assert ge.FINAL_DAY in kinds
    assert ge.RECAP in kinds
    # Announce is T-7, how-it-works T-4, last-call T-1, recap T+2.
    by_kind = {p.kind: p.post_date for p in arc if p.kind != ge.DURING}
    assert by_kind[ge.ANNOUNCE] == "2026-09-15"
    assert by_kind[ge.HOW_IT_WORKS] == "2026-09-18"
    assert by_kind[ge.LAST_CALL] == "2026-09-21"
    assert by_kind[ge.FINAL_DAY] == "2026-09-28"
    assert by_kind[ge.RECAP] == "2026-09-30"
    # during posts fall strictly inside the window, 1-2 days apart.
    durs = sorted(p.post_date for p in arc if p.kind == ge.DURING)
    assert durs and all("2026-09-22" < d < "2026-09-28" for d in durs)


def test_arc_is_date_ordered():
    ev = _event()
    arc = ge.plan_arc(ev, today=date(2026, 9, 1))
    dates = [p.post_date for p in arc]
    assert dates == sorted(dates)


# ---- 2-day notice -> compresses ------------------------------------------------

def test_two_day_notice_compresses():
    ev = _event()
    # today is 2 days before start: announce (T-7) is in the past.
    arc = ge.plan_arc(ev, today=date(2026, 9, 20))
    kinds = [p.kind for p in arc]
    # No T-7 announce on its original day; the merged pre-window post carries a note.
    announces = [p for p in arc if p.kind == ge.ANNOUNCE]
    assert len(announces) == 1
    assert announces[0].post_date >= "2026-09-20"
    assert "merged" in announces[0].note.lower()
    # never a pre-window post dated before today.
    assert all(p.post_date >= "2026-09-20" for p in arc)
    # still has during/final/recap.
    assert ge.FINAL_DAY in kinds and ge.RECAP in kinds


def test_no_next_week_post_for_near_event():
    # event starts tomorrow: no announce that reads like "next week".
    ev = _event(starts_on="2026-09-22", ends_on="2026-09-22")
    arc = ge.plan_arc(ev, today=date(2026, 9, 21))
    for p in arc:
        assert p.post_date >= "2026-09-21"


# ---- created after start -> during + final + recap only ------------------------

def test_created_mid_event_during_final_recap_only():
    ev = _event()
    arc = ge.plan_arc(ev, today=date(2026, 9, 24))  # mid-window
    kinds = {p.kind for p in arc}
    assert ge.ANNOUNCE not in kinds
    assert ge.HOW_IT_WORKS not in kinds
    assert ge.LAST_CALL not in kinds
    assert ge.RECAP in kinds
    # a portal note explains why the pre-run is gone.
    assert any("after the event started" in (p.note or "").lower() for p in arc)
    # no post dated before today.
    assert all(p.post_date >= "2026-09-24" for p in arc)


def test_created_after_end_recap_only():
    ev = _event()
    arc = ge.plan_arc(ev, today=date(2026, 9, 29))  # after end, before recap
    kinds = [p.kind for p in arc]
    assert kinds == [ge.RECAP]


# ---- gym-tz firing vs UTC rollover ---------------------------------------------

def test_scheduled_for_fires_in_gym_tz_not_utc():
    ev = _event(tz="America/Los_Angeles")
    iso = ge.scheduled_for(ev, "2026-09-22")
    # Pacific in September is PDT, -07:00 — never a UTC 'Z' / +00:00.
    assert iso.endswith("-07:00")
    assert "T10:00:00" in iso


def test_scheduled_for_east_vs_west_differ():
    east = ge.scheduled_for(_event(tz="America/New_York"), "2026-09-22")
    west = ge.scheduled_for(_event(tz="America/Los_Angeles"), "2026-09-22")
    assert east.endswith("-04:00")   # EDT
    assert west.endswith("-07:00")   # PDT
    assert east != west


# ---- grounding: no invented facts (property test) ------------------------------

@pytest.mark.parametrize("etype", list(ge.EVENT_TYPES))
@pytest.mark.parametrize("offer,link,brief", [
    ("Your partner trains free all week", "https://g.com/x", "Bring a friend"),
    ("First class is $19", "", "New spring offer"),
    ("", "https://g.com/y", ""),
    ("50% off a 6 week challenge", "https://g.com/z", "Starts Monday"),
])
def test_drafted_copy_has_no_fact_absent_from_form(etype, offer, link, brief):
    ev = _event(type=etype, offer_text=offer, link=link, brief=brief,
                media_ids=("m1",))
    arc = ge.plan_arc(ev, today=date(2026, 9, 1))
    rows = ge.draft_arc(ev, arc, avatar="busy professionals 30 to 50")
    assert rows  # something drafted
    for row in rows:
        bad = ge.fact_ok(row["caption"], ev)
        assert not bad, f"{etype}/{row['arc_kind']} invented facts {bad}: {row['caption']!r}"


def test_every_arc_row_pending():
    ev = _event(media_ids=("m1",))
    arc = ge.plan_arc(ev, today=date(2026, 9, 1))
    rows = ge.draft_arc(ev, arc)
    assert rows
    assert all(r["status"] == "pending" for r in rows)


def test_every_row_stamps_event_id_and_offer_category():
    ev = _event(media_ids=("m1",))
    rows = ge.draft_arc(ev, ge.plan_arc(ev, today=date(2026, 9, 1)))
    assert all(r["event_id"] == ev.id for r in rows)
    assert all(r["pillar"] == ge.ARC_CATEGORY for r in rows)


def test_copy_gate_no_dashes_and_one_ask():
    ev = _event(media_ids=("m1",),
                offer_text="Bring a partner, they train free, no strings")
    rows = ge.draft_arc(ev, ge.plan_arc(ev, today=date(2026, 9, 1)))
    for row in rows:
        assert not copy_gate.violations(row["caption"])
        if row["arc_kind"] != ge.RECAP:
            assert copy_gate.ASK_RE.search(row["caption"]), row["caption"]


# ---- recap blocked until real media -------------------------------------------

def test_recap_blocked_without_media():
    ev = _event(media_ids=())        # empty pool
    rows = ge.draft_arc(ev, ge.plan_arc(ev, today=date(2026, 9, 1)))
    recap = [r for r in rows if r["arc_kind"] == ge.RECAP]
    assert recap and recap[0]["recap_blocked"] is True


def test_recap_unblocked_with_real_media():
    ev = _event(media_ids=("photo_from_the_event",))
    rows = ge.draft_arc(ev, ge.plan_arc(ev, today=date(2026, 9, 1)))
    recap = [r for r in rows if r["arc_kind"] == ge.RECAP]
    assert recap and recap[0]["recap_blocked"] is False


# ---- event -> Story Studio one-tap hook (real entry point, correct shape) -------

def test_event_story_request_maps_to_create_story_shape():
    """story_studio_create_request must produce EXACTLY the keys story_studio.create_story
    consumes (gym_id, account_key, asset_ids, brief, ...), grounded only in event facts."""
    ev = _event(media_ids=("clip_a", "clip_b"))
    req = ge.story_studio_create_request(ev, account_key="pete_ig", requested_by="U1")
    # the real create_story request keys are present and correctly mapped.
    assert req["gym_id"] == "pete"
    assert req["account_key"] == "pete_ig"
    assert req["asset_ids"] == ["clip_a", "clip_b"]   # media_ids -> asset_ids
    assert req["requested_by"] == "U1"
    # brief grounded ONLY in event facts (name, dates, offer), never fabricated.
    assert ev.name in req["brief"]
    assert ev.offer_text in req["brief"]
    assert req["event_id"] == ev.id


def test_render_event_story_reaches_real_create_story_entry():
    """render_event_story must resolve the REAL Story Studio entry (create_story), not the
    dead 'render_from_request' name, and hand it a proper create_story request shape."""
    import agent.story_studio as _ss
    # 1. the live-path name the hook resolves actually exists on story_studio.
    assert callable(getattr(_ss, "create_story", None))
    assert getattr(_ss, "render_from_request", None) is None  # the dead name stays gone

    # 2. a GymEvent handed to render_event_story reaches create_story with the real shape.
    ev = _event(media_ids=("clip_a",))
    captured = {}

    def _fake_create_story(request):
        captured.update(request)
        return {"status": "off", "reason": "not armed"}   # mimic the OFF-gate default

    out = ge.render_event_story(ev, renderer=_fake_create_story, account_key="pete_ig")
    assert captured.get("gym_id") == "pete"
    assert captured.get("asset_ids") == ["clip_a"]
    assert "brief" in captured and ev.name in captured["brief"]
    assert out == {"status": "off", "reason": "not armed"}


def test_render_event_story_honest_stub_on_renderer_exception():
    """The honest-stub rail: a renderer that raises returns None (offer only), never a
    fabricated/broken render leaking to the caller."""
    ev = _event(media_ids=("clip_a",))

    def _boom(_req):
        raise RuntimeError("renderer blew up")

    assert ge.render_event_story(ev, renderer=_boom) is None
