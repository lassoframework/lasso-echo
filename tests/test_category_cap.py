"""
Regression: book campaign monopolizes the daily slot.

Without the category cap, build_book_draft() fires every day when armed and
preempts rotation — all 14 days become book posts. After the fix, the
frequency cap (AGENT_BOOK_CAMPAIGN_EVERY_N_DAYS=3) and the consecutive cap
(AGENT_CATEGORY_MAX_CONSECUTIVE=2) are checked before calling the builder.

This test FAILS on pre-fix code (book ignores caps, runs every day) and
PASSES after the fix.
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

START_DATE = date(2026, 7, 1)  # Wednesday

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


def _fake_book_draft(account, day_key, **kwargs):
    """Stub that always returns a book Draft — simulates a fully-armed book campaign
    with content available for every day."""
    return Draft(
        draft_id=f"book-{day_key}",
        account_key=account.key,
        platform="instagram",
        caption=f"Book draft for {day_key}",
        hashtags=["#LASSO"],
        creative_path="",
        creative_public_url="",
        scheduled_for=f"{day_key}T18:30:00+00:00",
        draft_type="book",
    )


def _run_14_days(monkeypatch, tmp_path, every_n=3, max_consec=2):
    """Run 14 consecutive days of run_daily with book campaign mocked to always
    return a draft. Returns list of (day_key, draft_type) for each day that
    actually produced a feed draft (skip days yield nothing)."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "true")
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_EVERY_N_DAYS", str(every_n))
    monkeypatch.setenv("AGENT_CATEGORY_MAX_CONSECUTIVE", str(max_consec))

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")

    monkeypatch.setattr("agent.book_campaign.build_book_draft", _fake_book_draft)

    account = _lasso_account()
    store = PendingStore(path=db_path)
    poster = _FakePoster()

    results = []
    for i in range(14):
        day = START_DATE + timedelta(days=i)
        day_key = day.isoformat()
        out = run_daily(
            poster=poster,
            voice_path=str(voice),
            library_path=str(lib),
            scheduled_for=f"{day_key}T18:30:00+00:00",
            accounts=[account],
            store=store,
        )
        # Skip days yield no drafts
        feed = next(
            (d for d in out.get("drafts", []) if not getattr(d, "is_story", False)),
            None,
        )
        if feed is not None:
            cat = getattr(feed, "draft_type", "") or "feed"
            results.append((day_key, cat))
    return results


# ---------------------------------------------------------------------------
# Reproduction: these tests FAIL on pre-fix code (book ignores caps) and
# PASS after the fix is applied.
# ---------------------------------------------------------------------------

def test_book_respects_frequency_cap(monkeypatch, tmp_path):
    """With every_n_days=3, no two book posts may be fewer than 3 calendar days
    apart. Pre-fix: book fires every day, gap=1 < 3 → FAIL. Post-fix: PASS."""
    results = _run_14_days(monkeypatch, tmp_path, every_n=3, max_consec=0)
    categories = [cat for _, cat in results]
    book_days = [day for day, cat in results if cat == "book"]

    assert book_days, "book campaign never posted — check mock setup"

    for idx in range(len(book_days) - 1):
        d0 = date.fromisoformat(book_days[idx])
        d1 = date.fromisoformat(book_days[idx + 1])
        gap = (d1 - d0).days
        assert gap >= 3, (
            f"Book appeared only {gap} calendar day(s) apart "
            f"({book_days[idx]} → {book_days[idx + 1]}); "
            f"expected gap >= 3.\nFull 14-day category list: {categories}"
        )


def test_book_never_monopolizes_all_14_days(monkeypatch, tmp_path):
    """With the frequency cap armed, book must not fill every posting day.
    Pre-fix: all posting days are book posts. Post-fix: rotation kicks in."""
    results = _run_14_days(monkeypatch, tmp_path, every_n=3, max_consec=0)
    categories = [cat for _, cat in results]
    unique = set(categories)

    assert len(unique) > 1, (
        f"Expected variety across 14 days but only saw: {unique}.\n"
        f"Full list: {categories}"
    )

    book_count = categories.count("book")
    assert book_count < len(categories), (
        f"Book filled every posting day ({book_count}/{len(categories)}). "
        f"Rotation never ran."
    )


def test_no_campaign_category_exceeds_consecutive_cap(monkeypatch, tmp_path):
    """With max_consecutive=2, book must never appear more than 2 consecutive
    posting days in a row. With every_n_days=2 (allowing up to every other day),
    the consecutive cap is the binding constraint."""
    results = _run_14_days(monkeypatch, tmp_path, every_n=2, max_consec=2)
    categories = [cat for _, cat in results]

    # Check all windows of 3 consecutive posting days
    for i in range(len(categories) - 2):
        triple = (categories[i], categories[i + 1], categories[i + 2])
        if triple[0] == triple[1] == triple[2] == "book":
            assert False, (
                f"Book appeared 3 consecutive posting days at indices "
                f"{i}, {i+1}, {i+2}.\nFull 14-day: {categories}"
            )


# ---------------------------------------------------------------------------
# unit tests for the category_cap module itself
# ---------------------------------------------------------------------------

def test_is_allowed_frequency_cap(monkeypatch, tmp_path):
    """is_allowed returns False when category ran within (every_n_days-1) days."""
    from agent.category_cap import is_allowed, record_win
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))

    record_win("lasso_ig", "book", "2026-07-01")

    assert not is_allowed("lasso_ig", "book", "2026-07-02",
                          every_n_days=3), "should be blocked: 1 day since last book"
    assert not is_allowed("lasso_ig", "book", "2026-07-03",
                          every_n_days=3), "should be blocked: 2 days since last book"
    assert is_allowed("lasso_ig", "book", "2026-07-04",
                      every_n_days=3), "should be allowed: 3 days since last book"


def test_is_allowed_consecutive_cap(monkeypatch, tmp_path):
    """is_allowed returns False when category has hit max_consecutive days in a row."""
    from agent.category_cap import is_allowed, record_win
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))

    record_win("lasso_ig", "podcast", "2026-07-01")
    record_win("lasso_ig", "podcast", "2026-07-02")

    assert not is_allowed("lasso_ig", "podcast", "2026-07-03",
                          max_consecutive=2), "2 consecutive, should be blocked for 3rd"
    assert is_allowed("lasso_ig", "book", "2026-07-03",
                      max_consecutive=2), "book tail=0, should be allowed"


def test_record_win_is_idempotent(monkeypatch, tmp_path):
    """Calling record_win twice for the same day_key replaces the entry."""
    from agent.category_cap import get_history, record_win
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))

    record_win("lasso_ig", "book", "2026-07-01")
    record_win("lasso_ig", "feed", "2026-07-01")  # replace

    history = get_history("lasso_ig")
    entries_for_day = [e for e in history if e[0] == "2026-07-01"]
    assert len(entries_for_day) == 1, "only one entry per day_key"
    assert entries_for_day[0][1] == "feed", "second write wins"


def test_is_allowed_uncapped_defaults(monkeypatch, tmp_path):
    """With defaults (every_n_days=1, max_consecutive=0), always allowed."""
    from agent.category_cap import is_allowed, record_win
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))

    for i in range(7):
        day = f"2026-07-0{i + 1}"
        assert is_allowed("lasso_ig", "book", day), f"should always allow with defaults on {day}"
        record_win("lasso_ig", "book", day)
