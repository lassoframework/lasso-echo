"""
_gyms_short_on must read the ACCOUNT REGISTRY, not the local gyms table.

It used to iterate db.gym_list(), which was ACCIDENTALLY almost the right set: the
worker's SQLite only ever held the ~20 gyms someone had touched on that volume, which
tracked the account registry closely enough that nobody noticed.

Closing the echo.db split brain (the shared echo_gyms record) makes every
self-serve-onboarded gym visible on the worker -- 139 rows and climbing, most of them
onboarding stubs with no registry entry and therefore no content lane at all. Against
the old implementation that turns one interrupted-draw alert into ~140 Supabase round
trips and a Slack line naming ~119 gyms as "have NO rows for <day>", which is both
wrong and the loudest possible way to be wrong.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener


class _Store:
    """Records which bases were asked about. Every gym looks EMPTY for the day, so
    whatever set the function chose to walk is exactly what it reports as short."""

    def __init__(self):
        self.asked = []

    def list_month(self, base, month):
        self.asked.append(base)
        return []


def test_short_gyms_come_from_the_registry_not_the_gyms_table(monkeypatch):
    store = _Store()
    monkeypatch.setattr("agent.portal_calendar_store.SupabaseCalendarStore",
                        lambda *a, **k: store)
    registry = ["eng", "topfuel", "gritx"]
    monkeypatch.setattr("agent.calendar_autopublish.client_gym_bases",
                        lambda: list(registry))

    # The local gyms table is full of onboarding stubs the registry does not know.
    from agent import db
    stubs = [{"account_key": f"stubgym{i}"} for i in range(50)]
    monkeypatch.setattr(db, "gym_list",
                        lambda *a, **k: stubs + [{"account_key": b} for b in registry])

    short = listener._gyms_short_on("2026-09-10")

    assert sorted(short) == sorted(registry), (
        "the alert named gyms that are not in the account registry")
    assert sorted(store.asked) == sorted(registry), (
        f"asked Supabase about {len(store.asked)} gyms; the registry has "
        f"{len(registry)}")
    for name in store.asked:
        assert not name.startswith("stubgym"), (
            "an onboarding stub with no content lane was checked for coverage")


def test_an_empty_registry_reports_unknown_not_fine(monkeypatch):
    """Unchanged contract: checking zero gyms must never read as 'everything is fine'."""
    store = _Store()
    monkeypatch.setattr("agent.portal_calendar_store.SupabaseCalendarStore",
                        lambda *a, **k: store)
    monkeypatch.setattr("agent.calendar_autopublish.client_gym_bases", lambda: [])
    assert listener._gyms_short_on("2026-09-10") is None


def test_a_gym_with_a_row_for_the_day_is_not_short(monkeypatch):
    class _Covered(_Store):
        def list_month(self, base, month):
            self.asked.append(base)
            return ([{"post_date": "2026-09-10"}] if base == "eng" else [])

    store = _Covered()
    monkeypatch.setattr("agent.portal_calendar_store.SupabaseCalendarStore",
                        lambda *a, **k: store)
    monkeypatch.setattr("agent.calendar_autopublish.client_gym_bases",
                        lambda: ["eng", "topfuel"])
    assert listener._gyms_short_on("2026-09-10") == ["topfuel"]
