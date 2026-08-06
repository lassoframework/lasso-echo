"""
LASSO Growth Summit sprint tests. Offline, no network, no API keys.

Covers the summit-finish sprint work:
  - the backward-anchored 5-cycle calendar (cycle boundaries, gap days absent,
    Nov 7 + 8 dark, up to 3 feed slots/day, DST-correct slot times, no card
    twice in a row);
  - the flag gate (AGENT_SUMMIT_CAMPAIGN_ENABLED default OFF => create_sprint_drafts
    is a no-op);
  - fabrication guards on every sprint caption (dash free, no blocked scarcity /
    pricing copy, no PushPress);
  - the agenda cards use ONLY verbatim session titles from 02_verified_stats.md and
    carry NO fabricated session times;
  - the panel card names Streamfit / HireVP / Tommy and NEVER PushPress;
  - the render paths produce the right-size PNGs (1080x1080 feed/agenda/panel,
    1080x1920 stories).
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import summit_queue as sq  # noqa: E402
from agent import summit_render as sr  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")

# copy that must never appear on a sprint asset (pre-ruling scarcity + pricing, and
# the excluded panel vendor)
_BLOCKED = [
    "$299", "$449", "early bird", "8 or more", "8+", "30 to 40", "30 to 40%",
    "half full", "last seats", "moving fast", "final call", "sold out", "pushpress",
]


# ---- calendar shape --------------------------------------------------------
def test_sprint_days_cycles_and_dark_days():
    days = sq.sprint_days()
    # cycle boundaries present
    for b in ("2026-08-21", "2026-08-30", "2026-09-07", "2026-09-16",
              "2026-09-24", "2026-10-03", "2026-10-11", "2026-11-06"):
        assert b in days, f"missing boundary {b}"
    # gap days between cycles are absent
    for g in ("2026-08-31", "2026-09-06", "2026-09-17", "2026-09-23",
              "2026-10-04", "2026-10-10"):
        assert g not in days, f"gap day leaked {g}"
    # event days dark
    assert "2026-11-07" not in days
    assert "2026-11-08" not in days
    # ordered, unique
    assert days == sorted(days)
    assert len(days) == len(set(days))


def test_sprint_calendar_slots_and_no_back_to_back():
    assets = [f"c{i:02d}.png" for i in range(1, 23)]
    plan = sq.sprint_calendar(assets, posts_per_day=3)
    assert len(plan) == len(sq.sprint_days()) * 3
    # up to 3 slots/day, indices 0..2
    per_day = {}
    for s in plan:
        per_day.setdefault(s["date"], []).append(s["slot_index"])
    for date, slots in per_day.items():
        assert slots == [0, 1, 2], (date, slots)
    # no card lands twice in a row across the whole ordered plan
    prev = None
    for s in plan:
        assert s["filename"] != prev, s
        prev = s["filename"]


def test_sprint_calendar_caps_posts_per_day_at_three():
    plan = sq.sprint_calendar(["a.png", "b.png"], posts_per_day=9)
    per_day = {}
    for s in plan:
        per_day.setdefault(s["date"], 0)
        per_day[s["date"]] += 1
    assert set(per_day.values()) == {sq.SPRINT_MAX_FEED_PER_DAY}
    assert sq.SPRINT_MAX_FEED_PER_DAY == 3


def test_sprint_slot_times_are_dst_correct_iso():
    plan = sq.sprint_calendar(["a.png", "b.png", "c.png"], posts_per_day=3)
    first_day = [s for s in plan if s["date"] == "2026-08-21"]
    times = sorted(s["scheduled_for"] for s in first_day)
    assert times[0].startswith("2026-08-21T07:30:00")
    assert times[1].startswith("2026-08-21T12:30:00")
    assert times[2].startswith("2026-08-21T18:30:00")
    # August is EDT (-04:00)
    assert all(t.endswith("-04:00") for t in times), times
    # November run is EST (-05:00)
    nov = [s for s in plan if s["date"] == "2026-11-06"]
    assert all(s["scheduled_for"].endswith("-05:00") for s in nov), nov


def test_empty_assets_calendar_is_empty():
    assert sq.sprint_calendar([]) == []


# ---- flag gate -------------------------------------------------------------
def test_create_sprint_drafts_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", raising=False)
    # even with a full manifest, flag off => zero drafts, no store writes
    manifest = {f: "https://r2.example/" + f for f, _ in sq.sprint_assets()}
    assert sq.create_sprint_drafts(manifest=manifest) == 0


# ---- fabrication guards on captions ---------------------------------------
def test_sprint_captions_dash_free_and_no_blocked_copy():
    # sanity: the detector actually fires
    assert _DASH_RE.search("planted-hyphen")
    for fname, cap in sq.sprint_assets():
        assert not _DASH_RE.search(cap), f"{fname}: dash in caption"
        low = cap.lower()
        for banned in _BLOCKED:
            assert banned not in low, f"{fname}: blocked copy {banned!r}"


def test_every_sprint_caption_has_registration_cta():
    # Every sprint caption drives to the verified registration link. (Not every
    # approved concept caption repeats the date line, so only the CTA is required.)
    for fname, cap in sq.sprint_assets():
        assert "lassoframework.com/summit" in cap, fname


# ---- agenda: verbatim sessions, NO fabricated times -----------------------
def _verified_sessions():
    """Session titles verbatim from 02_verified_stats.md SUMMIT SPEAKERS block."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "brand_voice", "knowledge", "02_verified_stats.md")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_agenda_sessions_are_verbatim_from_verified_stats():
    stats = _verified_sessions()
    for spec in (sr.AGENDA_DAY1, sr.AGENDA_DAY2):
        for speaker, title in spec["sessions"]:
            assert title in stats, f"agenda title not in verified stats: {title!r}"


def test_agenda_carries_no_fabricated_times():
    # No clock times (e.g. 9:00, 10 AM, 2pm) anywhere in the agenda data.
    time_re = re.compile(r"\b\d{1,2}\s?(:\d{2})?\s?(am|pm|AM|PM)\b|\b\d{1,2}:\d{2}\b")
    for spec in (sr.AGENDA_DAY1, sr.AGENDA_DAY2):
        blob = spec["day"] + spec["date"] + spec["theme"] + \
            " ".join(s + t for s, t in spec["sessions"])
        assert not time_re.search(blob), f"fabricated time in agenda: {blob!r}"


def test_agenda_dates_are_the_verified_event_dates():
    assert sr.AGENDA_DAY1["date"] == "NOV 7"
    assert sr.AGENDA_DAY2["date"] == "NOV 8"


# ---- panel: correct panelists, never PushPress ----------------------------
def test_panel_names_streamfit_hirevp_tommy_and_not_pushpress():
    names = [n.lower() for n in sr.PANEL["panelists"]]
    assert any("streamfit" in n for n in names)
    assert any("hirevp" in n for n in names)
    assert any("tommy" in n for n in names)
    blob = " ".join(names) + sr.PANEL["title"].lower() + sr.PANEL["deck"].lower()
    assert "pushpress" not in blob


# ---- render smoke tests (right sizes, no crash) ---------------------------
def test_agenda_and_panel_render_1080_square(tmp_path):
    from PIL import Image
    a1 = sr.render_agenda(sr.AGENDA_DAY1, str(tmp_path / "d1.png"))
    a2 = sr.render_agenda(sr.AGENDA_DAY2, str(tmp_path / "d2.png"))
    pn = sr.render_panel(sr.PANEL, str(tmp_path / "panel.png"))
    for p in (a1, a2, pn):
        assert Image.open(p).size == (1080, 1080), p


def test_story_render_is_1080x1920(tmp_path):
    from PIL import Image
    from agent.summit_rebuild import SUMMIT_CONCEPTS
    c = SUMMIT_CONCEPTS[0]
    for t in ("a", "b"):
        p = sr.render_card_story(c, t, str(tmp_path / f"s_{t}.png"))
        assert Image.open(p).size == (1080, 1920), p


def test_render_all_stories_covers_every_concept(tmp_path):
    from agent.summit_rebuild import SUMMIT_CONCEPTS
    paths = sr.render_all_stories(str(tmp_path))
    assert len(paths) == len(SUMMIT_CONCEPTS) * 2
    for p in paths:
        assert p.endswith("_story.png") and os.path.isfile(p)
