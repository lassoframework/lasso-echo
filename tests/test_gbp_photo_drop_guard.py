"""
The empty-caption stage belt must not eat Google Business PHOTO drops
(agent/portal_calendar_store._stage_belts).

2026-09-02, found by the first real fleet GBP run: gbp_planner plans 4 photo drops per
month (§5.1 cadence) with caption="" ON PURPOSE -- Google takes them as photo uploads on
the listing, they are not feed posts. The stage-time empty-caption belt ("a feed post may
not ship without real words") dropped every one of them, so ENG's month planned 12 rows
and persisted 8: a third of the month silently lost, on every gym, every month. Stories
were already exempt for exactly the same reason; this pins the photo-drop exemption
alongside them, and pins that nothing else about the belt changed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs  # noqa: E402


def _gbp_photo_row(date="2026-09-03"):
    """The exact shape gbp_planner._row builds for a photo drop."""
    return {"gym_id": "eng", "account": "googlebusiness", "post_date": date,
            "pillar": "photo", "format": "photo", "caption": "",
            "image_url": "https://cdn.example/eng/photo.jpg", "status": "pending",
            "gbp_topic_type": "STANDARD"}


def _feed_row(caption, date="2026-09-04"):
    return {"gym_id": "eng", "account": "eng_ig", "post_date": date, "pillar": "about",
            "format": "feed", "caption": caption, "image_url": "https://cdn/x.jpg",
            "status": "pending"}


def _armed(monkeypatch):
    monkeypatch.setattr(pcs.config, "empty_caption_guard_enabled", lambda: True)
    monkeypatch.setattr(pcs.config, "caption_cooldown_enabled", lambda: False)


def test_gbp_photo_drop_survives_the_empty_caption_belt(monkeypatch):
    _armed(monkeypatch)
    kept = pcs._stage_belts("eng", [_gbp_photo_row()])
    assert len(kept) == 1, "a caption-less GBP photo drop is legitimate, not an empty post"


def test_a_full_gbp_month_keeps_all_four_photo_drops(monkeypatch):
    # the real failure: 8 captioned standard rows + 4 caption-less photo drops -> 12,
    # not 8. This is the exact count that regressed in production.
    _armed(monkeypatch)
    payload = [_feed_row(f"Real caption number {i}.", f"2026-09-{i:02d}")
               for i in range(5, 13)]
    payload += [_gbp_photo_row(f"2026-09-{d:02d}") for d in (3, 10, 17, 24)]
    kept = pcs._stage_belts("eng", payload)
    assert len(kept) == 12
    assert sum(1 for r in kept if r["format"] == "photo") == 4


def test_a_genuinely_empty_FEED_row_is_still_dropped(monkeypatch):
    # the belt's real job is untouched: a feed post with no words still cannot ship.
    _armed(monkeypatch)
    kept = pcs._stage_belts("eng", [_feed_row("")])
    assert kept == []


def test_a_caption_less_non_photo_gbp_row_is_still_dropped(monkeypatch):
    # only format='photo' is exempt. A caption-less GBP *update* is still a defect --
    # the exemption is narrow on purpose.
    _armed(monkeypatch)
    row = _gbp_photo_row()
    row["format"] = "update"
    assert pcs._stage_belts("eng", [row]) == []


def test_a_caption_less_photo_row_on_a_NON_gbp_account_is_still_dropped(monkeypatch):
    # both halves of the check matter: an IG row claiming format='photo' with no caption
    # is not a Google listing photo upload and stays dropped.
    _armed(monkeypatch)
    row = _gbp_photo_row()
    row["account"] = "eng_ig"
    assert pcs._stage_belts("eng", [row]) == []


def test_belt_off_passes_everything_through_unchanged(monkeypatch):
    monkeypatch.setattr(pcs.config, "empty_caption_guard_enabled", lambda: False)
    monkeypatch.setattr(pcs.config, "caption_cooldown_enabled", lambda: False)
    payload = [_gbp_photo_row(), _feed_row("")]
    assert pcs._stage_belts("eng", payload) == payload
