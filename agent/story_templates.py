"""
story_templates.py — the five Story Studio templates (spec §2).

Each template is an ordered SEGMENT PLAN + OVERLAY SLOTS + MUSIC MOOD + ASK STYLE.
Vision tags (people count, movement, equipment, mood) pick the DEFAULT template; the
client's declared lane / brief OVERRIDES the default (intent beats inference).

Launch five:
  * athlete_stat  — a single athlete result. Stat card (NAME + NUMBER + PLACE).
  * member_win    — a member milestone. Stat-style card (name, number, gym).
  * event         — a gym event. Event card (WHAT + WHEN + one ask).
  * class_promo   — a class / offer promo. Hook + one ask.
  * hype_montage  — a multi-clip energy montage. Two-beat hook + one closing ask.

A template NEVER defaults its music to chill (spec §3): every default mood is 'hype'.
The segment plan bounds how many segments the composer pulls and their length window.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import story_music as _music

# Ask styles (the single closing ask each template ends on; ONE ask, always).
ASK_BOOK = "BOOK YOUR FREE INTRO"
ASK_CELEBRATE = "TAG WHO YOU TRAIN WITH"
ASK_EVENT = "SAVE YOUR SPOT"
ASK_JOIN = "START THIS WEEK"


@dataclass
class SegmentPlan:
    """How many segments the composer pulls and their length window (spec §3: 2..6
    segments, 3..15s each, total 15..60s)."""
    min_segments: int = 2
    max_segments: int = 6
    seg_min_sec: float = 3.0
    seg_max_sec: float = 15.0
    total_min_sec: float = 15.0
    total_max_sec: float = 60.0


@dataclass
class Template:
    name: str
    segment_plan: SegmentPlan
    overlay_slots: list           # ordered slot names the overlay pass fills
    music_mood: str               # default shelf; NEVER 'chill'
    ask_style: str
    card_kind: str = ""           # 'stat' | 'event' | '' (plain hook)

    def __post_init__(self):
        # Enforce the no-chill-default rail at construction (spec §3).
        if self.music_mood == _music.SHELF_CHILL:
            self.music_mood = _music.SHELF_HYPE


TEMPLATES = {
    "athlete_stat": Template(
        name="athlete_stat",
        segment_plan=SegmentPlan(min_segments=2, max_segments=4,
                                 total_min_sec=15, total_max_sec=40),
        overlay_slots=["hook", "stat_card", "ask"],
        music_mood=_music.SHELF_HYPE, ask_style=ASK_JOIN, card_kind="stat"),
    "member_win": Template(
        name="member_win",
        segment_plan=SegmentPlan(min_segments=2, max_segments=4,
                                 total_min_sec=15, total_max_sec=40),
        overlay_slots=["hook", "stat_card", "ask"],
        music_mood=_music.SHELF_HYPE, ask_style=ASK_CELEBRATE, card_kind="stat"),
    "event": Template(
        name="event",
        segment_plan=SegmentPlan(min_segments=2, max_segments=5,
                                 total_min_sec=15, total_max_sec=50),
        overlay_slots=["hook", "event_card", "ask"],
        music_mood=_music.SHELF_HYPE, ask_style=ASK_EVENT, card_kind="event"),
    "class_promo": Template(
        name="class_promo",
        segment_plan=SegmentPlan(min_segments=2, max_segments=5,
                                 total_min_sec=15, total_max_sec=50),
        overlay_slots=["hook", "ask"],
        music_mood=_music.SHELF_HYPE, ask_style=ASK_BOOK, card_kind=""),
    "hype_montage": Template(
        name="hype_montage",
        segment_plan=SegmentPlan(min_segments=3, max_segments=6,
                                 total_min_sec=20, total_max_sec=60),
        overlay_slots=["hook", "ask"],
        music_mood=_music.SHELF_HYPE, ask_style=ASK_JOIN, card_kind=""),
}

DEFAULT_TEMPLATE = "hype_montage"


def get(name):
    """The Template by name, or None."""
    return TEMPLATES.get(str(name or "").strip().lower())


def choose_from_vision(analysis):
    """Pick the DEFAULT template from the vision tags (spec §2). Heuristics:
      * a single subject + a number/result mood -> athlete_stat
      * a single/pair subject, celebratory mood -> member_win
      * event signage / a crowd + 'event' cues  -> event
      * equipment-forward, class cues           -> class_promo
      * high movement / a crowd, no result cue  -> hype_montage
    A missing/weak analysis -> DEFAULT_TEMPLATE. This only picks the DEFAULT; the
    caller lets the lane/brief override it."""
    if not analysis or analysis.get("analysis_failed"):
        return DEFAULT_TEMPLATE
    people = (analysis.get("people") or {}).get("bucket") or ""
    mood = str(analysis.get("mood") or "").lower()
    tags = " ".join(str(t) for t in (analysis.get("tags") or [])).lower()
    setting = str(analysis.get("setting") or "").lower()

    if "event" in tags or "party" in tags or "signage" in tags:
        return "event"
    if any(k in mood for k in ("proud", "celebrate", "milestone", "victory")):
        return "member_win"
    if people in ("solo", "pair") and any(k in tags for k in ("result", "pr", "time",
                                                              "finish", "medal")):
        return "athlete_stat"
    if any(k in tags for k in ("class", "equipment", "coach", "workout")) and (
            people in ("small_group", "solo", "pair")):
        return "class_promo"
    if people == "crowd" or "energetic" in mood or "high" in mood or "outdoor" in setting:
        return "hype_montage"
    return DEFAULT_TEMPLATE


def resolve_template(*, declared_template=None, analysis=None):
    """The final template name for a request. Intent beats inference: a
    declared_template (from the card / lane) wins when valid; otherwise the vision
    default is used. Returns (name, source) where source is 'declared' | 'vision'."""
    if declared_template and get(declared_template):
        return str(declared_template).strip().lower(), "declared"
    return choose_from_vision(analysis), "vision"
