"""The calendar slot idempotency belt, and the production numbers that forced it.

2026-09-05. The forward book held 155 genuine duplicate slots across 14 gyms and was still
GROWING hour over hour: 290 by one count at 08:00, 321 at 09:35. A re-plan was appending
instead of replacing, so the leak outran every cleanup.

Two distinct causes, measured against production, both closed at insert_rows because that
is the single door every staging lane walks through:
  * 94 duplicates were written TWICE IN THE SAME SECOND by one run, so the batch already
    held the row twice before the POST.
  * 61 spanned different runs, because delete_month deliberately PRESERVES human-owned
    rows and a re-plan then inserted a fresh row on top of a preserved one.

THE SLOT KEY IS THE WHOLE POINT. A slot is (account, post_date, time_slot, format), never
(account, post_date). A gym running 2x a day plus a story owns three legitimate rows on one
date. Keying on the date alone counted 441 rows as duplicates when only 155 were, and
superseding on that key would have destroyed live client content including approved posts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs  # noqa: E402


def _row(account="instagram", date="2026-09-10", slot="evening", fmt="feed", **kw):
    r = {"account": account, "post_date": date, "time_slot": slot, "format": fmt,
         "caption": kw.pop("caption", "c"), "status": kw.pop("status", "pending")}
    r.update(kw)
    return r


class _Store:
    def __init__(self, live=()):
        self._live = list(live)

    def rows_in_range(self, account_key, start, end):
        return self._live


def _armed(monkeypatch, on=True):
    from agent import config
    monkeypatch.setattr(config, "slot_dedupe_enabled", lambda: on)


# ---- cause 1: the same run staged the slot twice ---------------------------------------

def test_an_in_batch_duplicate_is_dropped(monkeypatch):
    _armed(monkeypatch)
    batch = [_row(caption="first"), _row(caption="second")]
    out = pcs._dedupe_slots(_Store(), "eng", batch)
    assert len(out) == 1 and out[0]["caption"] == "first", "the FIRST row for a slot wins"


def test_the_zanshin_shape_from_production(monkeypatch):
    """The worst real slot: one run at 12:38:33 inserted two feed rows and two story rows
    for the same date, so four rows landed where two belong."""
    _armed(monkeypatch)
    batch = [_row(slot="evening", fmt="feed"), _row(slot="early_morning", fmt="story"),
             _row(slot="evening", fmt="feed"), _row(slot="early_morning", fmt="story")]
    out = pcs._dedupe_slots(_Store(), "zanshinfitness630e22", batch)
    assert len(out) == 2
    assert {(r["time_slot"], r["format"]) for r in out} == {
        ("evening", "feed"), ("early_morning", "story")}


# ---- the distinction that protects real content ----------------------------------------

def test_two_posts_a_day_plus_a_story_are_NOT_duplicates(monkeypatch):
    """The guard must never treat a legitimate 2x-a-day-plus-story date as duplication.
    This is the exact shape the coarse key destroyed."""
    _armed(monkeypatch)
    batch = [_row(slot="early_morning", fmt="feed"),
             _row(slot="evening", fmt="feed"),
             _row(slot="early_morning", fmt="story")]
    out = pcs._dedupe_slots(_Store(), "eng", batch)
    assert len(out) == 3, "three distinct slots on one date are all legitimate"


def test_different_accounts_on_one_date_are_kept(monkeypatch):
    _armed(monkeypatch)
    batch = [_row(account="instagram"), _row(account="facebook"),
             _row(account="googlebusiness")]
    assert len(pcs._dedupe_slots(_Store(), "eng", batch)) == 3


# ---- cause 2: the slot is already live in the database ---------------------------------

def test_a_slot_already_live_is_not_staged_again(monkeypatch):
    """delete_month preserves an APPROVED row because it is a client decision. A re-plan
    then landed a second row on that same slot. 61 of the fleet's duplicates were this."""
    _armed(monkeypatch)
    live = [_row(status="approved", caption="the client already approved this")]
    out = pcs._dedupe_slots(_Store(live), "zanshinfitness630e22", [_row(caption="new")])
    assert out == [], "a slot the gym already holds live must not be filled twice"


def test_a_denied_or_deleted_row_frees_its_slot(monkeypatch):
    """Denied, killed and deleted rows are gone from the client's calendar, so their slot
    is available. Otherwise a denied post could never be replaced."""
    _armed(monkeypatch)
    for gone in ("denied", "killed", "deleted"):
        live = [_row(status=gone)]
        out = pcs._dedupe_slots(_Store(live), "eng", [_row(caption="replacement")])
        assert len(out) == 1, f"status {gone} must free the slot"


def test_a_failed_live_read_fails_open(monkeypatch):
    """A staging lane must never stop because a dedupe lookup failed. Worst case is the
    behaviour that shipped before this belt existed."""
    _armed(monkeypatch)

    class _Boom:
        def rows_in_range(self, *a, **k):
            raise RuntimeError("supabase down")

    out = pcs._dedupe_slots(_Boom(), "eng", [_row(), _row()])
    assert len(out) == 1, "in-batch dedupe still applies; the live check degrades"


# ---- the flag ---------------------------------------------------------------------------

def test_the_escape_hatch_restores_the_old_behaviour(monkeypatch):
    _armed(monkeypatch, on=False)
    batch = [_row(), _row(), _row()]
    assert pcs._dedupe_slots(_Store(), "eng", batch) == batch


def test_the_belt_defaults_ON(monkeypatch):
    """Default-on is deliberate: this PREVENTS damage, and the leak was still growing when
    it shipped. Same posture as the plan-horizon belt."""
    from agent import config
    monkeypatch.delenv("AGENT_SLOT_DEDUPE", raising=False)
    assert config.slot_dedupe_enabled() is True
    monkeypatch.setenv("AGENT_SLOT_DEDUPE", "false")
    assert config.slot_dedupe_enabled() is False
