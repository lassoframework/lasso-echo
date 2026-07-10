"""
Rotation controller: when AGENT_CATEGORY_ROTATION=true, run_daily routes each
weekday to the correct builder and never lets a campaign builder pre-empt the
schedule.

Week starting 2026-07-13 (Monday):
  Mon 2026-07-13  podcast   → build_podcast_slot_draft
  Tue 2026-07-14  platform  → no campaign builder (creative layer fills)
  Wed 2026-07-15  b2b       → no campaign builder
  Thu 2026-07-16  podcast   → build_podcast_slot_draft
  Fri 2026-07-17  summit    → build_summit_draft
  Sat 2026-07-18  platform  → no campaign builder
  Sun 2026-07-19  podcast   → build_podcast_slot_draft
"""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.drafter import Draft, DraftStatus
from agent.runner import run_daily
from agent.store import PendingStore

# 2026-07-13 is a Monday.
_MON = date(2026, 7, 13)
_WEEK = [(_MON + timedelta(days=i)).isoformat() for i in range(7)]
# Weekdays: Mon/Tue/Wed/Thu/Fri/Sat/Sun
_SCHEDULE = ["podcast", "platform", "b2b", "podcast", "summit", "platform", "podcast"]

_VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class _FakePoster:
    def post_approval_card(self, draft):
        return {"channel": "C1", "ts": "ts1"}

    def post_notice(self, text):
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        pass


def _lasso_account():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")


def _fake_podcast_draft(account, day_key, **kwargs):
    return Draft(draft_id=f"podcast-{day_key}", account_key=account.key,
                 platform="instagram", caption="New episode.",
                 hashtags=["#pod"], creative_path="", creative_public_url="",
                 scheduled_for=f"{day_key}T18:30:00+00:00", draft_type="podcast")


def _fake_summit_draft(account, day_key, **kwargs):
    return Draft(draft_id=f"summit-{day_key}", account_key=account.key,
                 platform="instagram", caption="Summit content.",
                 hashtags=["#summit"], creative_path="", creative_public_url="",
                 scheduled_for=f"{day_key}T18:30:00+00:00", draft_type="summit")


def _fake_book_draft(account, day_key, **kwargs):
    return Draft(draft_id=f"book-{day_key}", account_key=account.key,
                 platform="instagram", caption="Book content.",
                 hashtags=["#book"], creative_path="", creative_public_url="",
                 scheduled_for=f"{day_key}T18:30:00+00:00", draft_type="book")


def _arm(monkeypatch, tmp_path, rotation=True, book=True):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true" if rotation else "false")
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "true" if book else "false")
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", "true")

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")

    monkeypatch.setattr("agent.podcast_release.build_podcast_slot_draft",
                        _fake_podcast_draft)
    # build_summit_draft is a top-level import in runner.py, so patch it there.
    monkeypatch.setattr("agent.runner.build_summit_draft", _fake_summit_draft)
    monkeypatch.setattr("agent.book_campaign.build_book_draft", _fake_book_draft)

    return str(voice), str(lib), db_path


def _run_day(monkeypatch, tmp_path, day_key, rotation=True, book=True):
    voice, lib, db_path = _arm(monkeypatch, tmp_path, rotation=rotation, book=book)
    account = _lasso_account()
    store = PendingStore(path=db_path)
    out = run_daily(poster=_FakePoster(), voice_path=voice, library_path=lib,
                    scheduled_for=f"{day_key}T18:30:00+00:00",
                    accounts=[account], store=store)
    feed = next((d for d in out.get("drafts", [])
                 if not getattr(d, "is_story", False)), None)
    return feed


# ---------------------------------------------------------------------------
# Per-weekday dispatch assertions
# ---------------------------------------------------------------------------

def test_monday_routes_to_podcast(monkeypatch, tmp_path):
    feed = _run_day(monkeypatch, tmp_path, "2026-07-13")
    assert feed is not None
    assert getattr(feed, "draft_type", "") == "podcast", (
        f"Mon should be podcast; got {getattr(feed, 'draft_type', '')}")


def test_thursday_routes_to_podcast(monkeypatch, tmp_path):
    feed = _run_day(monkeypatch, tmp_path, "2026-07-16")
    assert feed is not None
    assert getattr(feed, "draft_type", "") == "podcast"


def test_sunday_routes_to_podcast(monkeypatch, tmp_path):
    feed = _run_day(monkeypatch, tmp_path, "2026-07-19")
    assert feed is not None
    assert getattr(feed, "draft_type", "") == "podcast"


def test_friday_routes_to_summit(monkeypatch, tmp_path):
    feed = _run_day(monkeypatch, tmp_path, "2026-07-17")
    assert feed is not None
    assert getattr(feed, "draft_type", "") == "summit", (
        f"Fri should be summit; got {getattr(feed, 'draft_type', '')}")


def test_platform_day_skips_campaign_builders(monkeypatch, tmp_path):
    """Tuesday is a platform day. No campaign builder (book/podcast/summit) should
    claim the slot; the creative layer fills it from the library."""
    feed = _run_day(monkeypatch, tmp_path, "2026-07-14")
    assert feed is not None
    dt = getattr(feed, "draft_type", "") or ""
    assert dt not in ("podcast", "book", "summit"), (
        f"Tue (platform) should not be claimed by a campaign builder; got {dt!r}")


def test_b2b_day_skips_campaign_builders(monkeypatch, tmp_path):
    """Wednesday is a b2b day. No campaign builder should claim the slot."""
    feed = _run_day(monkeypatch, tmp_path, "2026-07-15")
    assert feed is not None
    dt = getattr(feed, "draft_type", "") or ""
    assert dt not in ("podcast", "book", "summit"), (
        f"Wed (b2b) should not be claimed by a campaign builder; got {dt!r}")


def test_saturday_platform_skips_campaign_builders(monkeypatch, tmp_path):
    """Saturday is a platform day (video). No campaign builder should pre-empt."""
    feed = _run_day(monkeypatch, tmp_path, "2026-07-18")
    assert feed is not None
    dt = getattr(feed, "draft_type", "") or ""
    assert dt not in ("podcast", "book", "summit"), (
        f"Sat (platform) should not be claimed by a campaign builder; got {dt!r}")


# ---------------------------------------------------------------------------
# Book is the schedule authority when rotation routes to a book week
# ---------------------------------------------------------------------------

def test_book_only_fires_on_its_scheduled_day(monkeypatch, tmp_path):
    """With rotation ON and book armed, book must NOT fire on a podcast day.
    Book's slot is governed by category_plan's alternating week schedule; on
    even ISO weeks the flex slot (Sat/Fri/Tue) is book. On a Monday (podcast),
    book must never fire regardless of whether it is enabled."""
    feed = _run_day(monkeypatch, tmp_path, "2026-07-13", book=True)
    dt = getattr(feed, "draft_type", "")
    assert dt != "book", (
        f"Book must not fire on Monday (a podcast day); got {dt!r}")


# ---------------------------------------------------------------------------
# Rotation OFF: legacy chain unchanged
# ---------------------------------------------------------------------------

def test_legacy_chain_with_rotation_off(monkeypatch, tmp_path):
    """With AGENT_CATEGORY_ROTATION=false, the legacy priority chain is used.
    Book (armed) fires on Monday because it sits first in the old chain, not
    because the schedule says so — proving the old behavior is preserved."""
    feed = _run_day(monkeypatch, tmp_path, "2026-07-13", rotation=False, book=True)
    dt = getattr(feed, "draft_type", "")
    assert dt == "book", (
        f"Rotation OFF + book armed: legacy chain should put book first; got {dt!r}")
