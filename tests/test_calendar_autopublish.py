"""
Scheduled calendar auto-publisher (agent/calendar_autopublish.py), all offline.

TOP PRIORITY is EXACTLY-ONCE: a row is published at most one time even across a
re-run or a second concurrent worker. Every path injects a fake store / publisher /
notifier so NO real network and NO real Meta write ever happens in these tests.

Coverage:
  - publishes today's due rows once, mark_published per row with the fake media id.
  - EXACTLY-ONCE: an already-published row is skipped; a losing claim
    (mark_publishing -> False) is not published; a re-run after success publishes nil.
  - ONLY the run date: rows dated yesterday/tomorrow are never in the due set (the
    store filter proves it) and are not published.
  - flag OFF -> no-op (publisher never called); publish_enabled() False -> no-op.
  - mode 'would_publish' -> NOT marked published, claim reverted (retryable).
  - IG/FB + feed/story account mapping.
  - a publish failure reverts to pending and never blocks the other rows.
  - Slack notice sent once with the right summary; no secret in it.
  - store read/claim/update REST params verified with a fake http.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap
from agent import config
from agent import portal_calendar_store as pcs
from agent.meta_publisher import PublishResult


RUN_DATE = "2026-08-10"

# A local "now" past the last SPRINT_SLOT_TIME (18:30) so every assigned slot is
# due. Tests that assert publishing pass this so the time-of-day spacing gate
# (FIX 1) never withholds a row; the spacing behavior itself is covered in its
# own section below with earlier `now` values.
LATE_NOW = "2026-08-10T23:59:00-04:00"


# ---- fakes -----------------------------------------------------------------

def _row(row_id, account="instagram", fmt="feed", post_date=RUN_DATE,
         status="pending", caption="hello", image_url="https://cdn/x.jpg",
         published_at=None, late_post_id=None):
    return {
        "id": row_id, "gym_id": "lasso", "post_date": post_date,
        "account": account, "format": fmt, "status": status,
        "caption": caption, "image_url": image_url,
        "published_at": published_at, "late_post_id": late_post_id,
    }


class _FakeStore:
    """
    In-memory content_calendar. due_rows honors the run-date + unpublished filter;
    mark_publishing is an ATOMIC claim mirroring the REAL store's precondition
    (status in (pending, approved) AND no published_at wins — 'approved' became
    claimable with the Zernio client lane). A `claim_returns` override lets a test
    simulate a lost race.
    """

    def __init__(self, rows, claim_returns=None):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.published_calls = []      # (row_id, media_id, published_at)
        self.failed_calls = []         # row_id
        self.publishing_calls = []     # row_id
        self._claim_returns = claim_returns or {}

    def due_rows(self, gym_id, run_date):
        out = []
        for r in self.rows.values():
            if r.get("gym_id") != gym_id:
                continue
            if r.get("post_date") != run_date:          # ONLY the run date
                continue
            if r.get("status") in ("published", "denied", "killed"):
                continue
            if r.get("published_at"):
                continue
            if not r.get("image_url"):
                continue
            out.append(dict(r))
        return out

    def mark_publishing(self, row_id):
        self.publishing_calls.append(row_id)
        if row_id in self._claim_returns:
            won = self._claim_returns[row_id]
            if won:
                self.rows[row_id]["status"] = "publishing"
            return won
        r = self.rows.get(row_id)
        if not r or r.get("status") not in ("pending", "approved") \
                or r.get("published_at"):
            return False
        r["status"] = "publishing"
        return True

    def mark_published(self, row_id, media_id, published_at):
        self.published_calls.append((row_id, media_id, published_at))
        r = self.rows.get(row_id)
        if r:
            r["status"] = "published"
            r["published_at"] = published_at
            r["late_post_id"] = media_id
        return r

    def mark_publish_failed(self, row_id, revert_status="pending"):
        self.failed_calls.append(row_id)
        r = self.rows.get(row_id)
        if r:
            r["status"] = "pending"
        return r


class _FakePublisher:
    """Records each publish call and returns a canned PublishResult per account."""

    def __init__(self, result=None, per_row=None):
        self.calls = []            # (draft, account)
        self._result = result or PublishResult(ok=True, mode="published",
                                               media_id="MEDIA_1")
        self._per_row = per_row or {}

    def __call__(self, draft, account):
        self.calls.append((draft, account))
        if draft.draft_id in self._per_row:
            return self._per_row[draft.draft_id]
        return self._result


class _FakeNotifier:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")


# ---- publishes today's due rows once ---------------------------------------

def test_publishes_todays_rows_once(armed):
    store = _FakeStore([_row("a"), _row("b"), _row("c")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    note = _FakeNotifier()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, notifier=note,
                              now=LATE_NOW)

    assert summary["ok"] is True
    assert set(summary["published"]) == {"a", "b", "c"}
    assert summary["failed"] == []
    assert len(pub.calls) == 3
    # every row was claimed then recorded published with the fake media id + now
    assert set(store.publishing_calls) == {"a", "b", "c"}
    assert {rid for rid, _, _ in store.published_calls} == {"a", "b", "c"}
    for _rid, media_id, published_at in store.published_calls:
        assert media_id == "M"
        assert published_at == LATE_NOW


# ---- EXACTLY-ONCE / dup guard ----------------------------------------------

def test_already_published_row_is_skipped(armed):
    # A row already stamped published_at is never claimed and never re-published.
    store = _FakeStore([
        _row("done", status="published", published_at="2026-08-10T00:00:00+00:00",
             late_post_id="OLD"),
        _row("fresh"),
    ])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == ["fresh"]
    assert "done" not in store.publishing_calls          # never claimed
    assert [d.draft_id for d, _ in pub.calls] == ["fresh"]


def test_lost_claim_is_not_published(armed):
    # mark_publishing returns False (another worker won the claim) -> SKIP, no publish.
    store = _FakeStore([_row("x"), _row("y")], claim_returns={"x": False, "y": True})
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == ["y"]
    assert "x" in summary["skipped"]
    assert [d.draft_id for d, _ in pub.calls] == ["y"]   # x never published


def test_rerun_after_success_publishes_nothing(armed):
    store = _FakeStore([_row("a"), _row("b")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    first = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert set(first["published"]) == {"a", "b"}

    # Same store, a second run: rows are now published, so due_rows returns none.
    pub2 = _FakePublisher()
    second = cap.publish_due(RUN_DATE, store=store, publisher=pub2, now=LATE_NOW)
    assert second["published"] == []
    assert pub2.calls == []                              # NEVER double-posts to live


# ---- ONLY the run date -----------------------------------------------------

def test_only_run_date_yesterday_and_tomorrow_excluded(armed):
    store = _FakeStore([
        _row("yest", post_date="2026-08-09"),
        _row("today"),
        _row("tomo", post_date="2026-08-11"),
    ])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == ["today"]
    assert [d.draft_id for d, _ in pub.calls] == ["today"]  # no backfill, no future


# ---- flag gates ------------------------------------------------------------

def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("AGENT_CALENDAR_AUTOPUBLISH", raising=False)
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    store = _FakeStore([_row("a")])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub)

    assert summary["ok"] is False
    assert "flag OFF" in summary["reason"]
    assert pub.calls == []
    assert store.publishing_calls == []


def test_publish_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    store = _FakeStore([_row("a")])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub)

    assert summary["ok"] is False
    assert "publish flag OFF" in summary["reason"]
    assert pub.calls == []
    assert store.publishing_calls == []


# ---- would_publish revert (a gate off inside publish) ----------------------

def test_would_publish_reverts_claim_and_is_retryable(armed):
    store = _FakeStore([_row("a")])
    pub = _FakePublisher(PublishResult(ok=True, mode="would_publish",
                                       detail="stories flag OFF"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == []
    assert summary["failed"] == ["a"]
    assert store.published_calls == []            # NOT recorded as published
    assert store.failed_calls == ["a"]            # claim reverted
    assert store.rows["a"]["status"] == "pending"  # retryable next run


# ---- IG/FB + feed/story mapping --------------------------------------------

def test_account_and_story_mapping(armed):
    store = _FakeStore([
        _row("ig_feed", account="instagram", fmt="feed"),
        _row("fb_feed", account="facebook", fmt="feed"),
        _row("ig_story", account="instagram", fmt="story"),
    ])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    by_id = {d.draft_id: (d, a) for d, a in pub.calls}
    assert by_id["ig_feed"][1].key == "lasso_ig"
    assert by_id["ig_feed"][0].is_story is False
    assert by_id["fb_feed"][1].key == "lasso_fb"
    assert by_id["ig_story"][1].key == "lasso_ig"
    assert by_id["ig_story"][0].is_story is True
    assert by_id["ig_story"][0].draft_type == "story"


# ---- one bad row never blocks the rest -------------------------------------

def test_publish_failure_reverts_and_others_still_publish(armed):
    class _Boom(Exception):
        pass

    def _raise(draft, account):
        raise _Boom("meta 500")

    store = _FakeStore([_row("bad"), _row("good")])
    # publisher: 'bad' raises, 'good' publishes.
    pub = _FakePublisher(per_row={})
    calls = []

    def publisher(draft, account):
        calls.append(draft.draft_id)
        if draft.draft_id == "bad":
            raise _Boom("meta 500")
        return PublishResult(ok=True, mode="published", media_id="M")

    summary = cap.publish_due(RUN_DATE, store=store, publisher=publisher, now=LATE_NOW)

    assert summary["published"] == ["good"]
    assert summary["failed"] == ["bad"]
    assert store.failed_calls == ["bad"]                 # claim reverted
    assert store.rows["bad"]["status"] == "pending"      # retryable
    assert store.rows["good"]["status"] == "published"


# ---- Slack notice ----------------------------------------------------------

def test_slack_notice_sent_once_with_summary(armed):
    store = _FakeStore([_row("a", account="instagram"),
                        _row("b", account="facebook")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    note = _FakeNotifier()
    cap.publish_due(RUN_DATE, store=store, publisher=pub, notifier=note, now=LATE_NOW)

    assert len(note.notices) == 1
    msg = note.notices[0]
    assert "2" in msg                       # count
    assert RUN_DATE in msg
    assert "lasso_ig" in msg and "lasso_fb" in msg
    # no secret / token leaked
    assert "Bearer" not in msg and "apikey" not in msg


def test_no_notice_when_nothing_published(armed):
    store = _FakeStore([_row("a")])
    pub = _FakePublisher(PublishResult(ok=True, mode="would_publish"))
    note = _FakeNotifier()
    cap.publish_due(RUN_DATE, store=store, publisher=pub, notifier=note, now=LATE_NOW)
    assert note.notices == []


# ---- FIX 1 (reworked): STABLE per-row time-of-day spacing ------------------
# Slots come from summit_queue.SPRINT_SLOT_TIMES = 07:30, 12:30, 18:30 in
# POSTING_TIMEZONE (America/New_York by default). Each ROW has a STABLE slot
# derived from the row itself (NOT its position in the shrinking due set): a story
# -> midday (12:30); a feed -> AM (07:30) or PM (18:30) by a stable hash of its id.
# Known stable slots for the ids used below (see slot_time_for_row):
#   feed 'a' -> 07:30 (AM)   feed 'e' -> 18:30 (PM)   story -> 12:30 (midday)
# `now` values are given in EDT (-04:00) so the local-time mapping is explicit.

AM_FEED = "a"      # slot_time_for_row -> 07:30
PM_FEED = "e"      # slot_time_for_row -> 18:30


def _edt(hhmm):
    return f"2026-08-10T{hhmm}:00-04:00"


def test_slot_is_stable_function_of_the_row_itself():
    # A feed maps to AM or PM; a story maps to midday. Same id -> same slot always.
    assert cap.slot_time_for_row(_row(AM_FEED, fmt="feed")) == "07:30"
    assert cap.slot_time_for_row(_row(PM_FEED, fmt="feed")) == "18:30"
    assert cap.slot_time_for_row(_row("s", fmt="story")) == "12:30"
    # Stability: recomputing gives the identical slot (no per-process salt).
    assert cap.slot_time_for_row(_row(AM_FEED, fmt="feed")) == \
        cap.slot_time_for_row(_row(AM_FEED, fmt="feed"))


def test_story_slot_is_midday_after_its_am_feed():
    assert cap.slot_index_for_row(_row("s", fmt="story")) == 1     # middle slot
    # A feed never lands on the story's midday slot.
    for i in "abcdefgh":
        assert cap.slot_index_for_row(_row(i, fmt="feed")) != 1


def test_is_due_compares_the_rows_own_slot(monkeypatch):
    am = _row(AM_FEED, fmt="feed")   # 07:30
    pm = _row(PM_FEED, fmt="feed")   # 18:30
    assert cap.is_due(am, now=_edt("08:00")) is True     # AM slot passed
    assert cap.is_due(pm, now=_edt("08:00")) is False    # PM slot not yet
    assert cap.is_due(pm, now=_edt("19:00")) is True     # PM slot passed


def test_nothing_publishes_before_first_slot(armed):
    # now (07:00 EDT) is before the earliest slot (07:30) -> nothing claimed.
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED)])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("07:00"))

    assert summary["published"] == []
    assert set(summary["waiting"]) == {AM_FEED, PM_FEED}
    assert pub.calls == []
    assert store.publishing_calls == []                 # never claimed early


def test_only_am_slot_publishes_before_pm_slot(armed):
    # now (08:00 EDT) is past the AM slot (07:30) but before the PM slot (18:30).
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED)])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("08:00"))

    assert summary["published"] == [AM_FEED]
    assert summary["waiting"] == [PM_FEED]
    assert [d.draft_id for d, _ in pub.calls] == [AM_FEED]


def test_slot_does_not_move_when_a_sibling_publishes(armed):
    # THE FIX: after the AM feed publishes and leaves the due set, the PM feed's
    # slot stays PM (it does NOT re-rank to AM). At 13:00 (before 18:30) it waits.
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED)])
    p1 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s1 = cap.publish_due(RUN_DATE, store=store, publisher=p1, now=_edt("08:00"))
    assert s1["published"] == [AM_FEED]

    p2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=p2, now=_edt("13:00"))
    assert s2["published"] == []                        # PM slot has NOT moved up
    assert s2["waiting"] == [PM_FEED]
    assert p2.calls == []

    p3 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s3 = cap.publish_due(RUN_DATE, store=store, publisher=p3, now=_edt("19:00"))
    assert s3["published"] == [PM_FEED]                 # publishes at its own slot
    # Exactly-once across the whole day.
    assert sorted(rid for rid, _, _ in store.published_calls) == sorted(
        [AM_FEED, PM_FEED])


def test_story_publishes_at_midday_after_its_feed(armed):
    store = _FakeStore([
        _row(AM_FEED, account="instagram", fmt="feed"),      # 07:30
        _row("s", account="instagram", fmt="story"),         # 12:30
    ])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s1 = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=_edt("08:00"))
    assert s1["published"] == [AM_FEED]                 # feed first (AM)
    assert s1["waiting"] == ["s"]                       # story waits for midday

    pub2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M2"))
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=pub2, now=_edt("13:00"))
    assert s2["published"] == ["s"]                     # story at midday
    assert [d.draft_id for d, _ in pub2.calls] == ["s"]


def test_all_rows_publish_after_last_slot(armed):
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED), _row("s", fmt="story")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("19:00"))

    assert set(summary["published"]) == {AM_FEED, PM_FEED, "s"}
    assert summary["waiting"] == []


def test_published_row_never_republishes_on_a_later_slot_run(armed):
    # Belt-and-braces exactly-once under spacing: after the AM feed publishes,
    # a later-slot run never claims or re-publishes it.
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED)])
    p1 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=p1, now=_edt("08:00"))
    assert store.rows[AM_FEED]["status"] == "published"

    p2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=p2, now=_edt("19:00"))
    assert AM_FEED not in s2["published"]
    assert AM_FEED not in [d.draft_id for d, _ in p2.calls]


# ---- NO ORPHANS: catch_all + once/day draw ---------------------------------

def test_catch_all_publishes_every_due_row_regardless_of_slot(armed):
    # The once/day draw (10am ET) uses catch_all=True: even PM-slot rows publish
    # immediately, so nothing is orphaned when the scheduler fires only once.
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED), _row("s", fmt="story")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("10:00"), catch_all=True)

    assert set(summary["published"]) == {AM_FEED, PM_FEED, "s"}
    assert summary["waiting"] == []                     # NOTHING left behind


def test_once_a_day_single_call_orphans_nothing(armed):
    # Simulate the real ONCE/DAY scheduler: a single publish_due at 10am ET with
    # catch_all=True. Every due row publishes that day; none is orphaned.
    store = _FakeStore([_row("d0"), _row("d1"), _row(PM_FEED),
                        _row("s", fmt="story")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("10:00"), catch_all=True)

    assert set(summary["published"]) == {"d0", "d1", PM_FEED, "s"}
    assert summary["waiting"] == []
    for r in store.rows.values():
        assert r["status"] == "published"              # zero orphans


def test_catch_all_after_slot_ticks_is_exactly_once(armed):
    # AM tick publishes the AM feed; the end-of-day catch-all sweeps the PM feed
    # WITHOUT re-publishing the AM feed (the atomic claim holds).
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED)])
    p1 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=p1, now=_edt("08:00"))

    p2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=p2, now=_edt("18:30"),
                         catch_all=True)
    assert s2["published"] == [PM_FEED]
    assert AM_FEED not in [d.draft_id for d, _ in p2.calls]
    assert sorted(rid for rid, _, _ in store.published_calls) == sorted(
        [AM_FEED, PM_FEED])


# ---- listener slot-fire lane -----------------------------------------------

class _FakeKV:
    def __init__(self):
        self.store = {}

    def get(self, key, default=""):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


def test_run_slot_ticks_flag_off_makes_no_calls(monkeypatch):
    monkeypatch.delenv("AGENT_CALENDAR_AUTOPUBLISH", raising=False)
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    store = _FakeStore([_row(AM_FEED)])
    pub = _FakePublisher()
    out = cap.run_slot_ticks(RUN_DATE, store=store, publisher=pub,
                             now=_edt("19:00"), kv=_FakeKV())
    assert out == []
    assert pub.calls == []
    assert store.publishing_calls == []


def test_run_slot_ticks_fires_reached_slots_and_dedupes(armed):
    # At 13:00 the 07:30 and 12:30 slots have been reached; 18:30 has not.
    store = _FakeStore([_row(AM_FEED), _row("s", fmt="story"), _row(PM_FEED)])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    kv = _FakeKV()

    out = cap.run_slot_ticks(RUN_DATE, store=store, publisher=pub,
                             now=_edt("13:00"), kv=kv)
    # Two slots fired (07:30 + 12:30); the AM feed and the story published.
    assert len(out) == 2
    all_published = [rid for s in out for rid in s["published"]]
    assert set(all_published) == {AM_FEED, "s"}
    # Both reached slots are marked done; the PM slot was not reached.
    assert kv.get(cap._slot_fire_key(RUN_DATE, "07:30")) == "done"
    assert kv.get(cap._slot_fire_key(RUN_DATE, "12:30")) == "done"
    assert kv.get(cap._slot_fire_key(RUN_DATE, "18:30")) == ""

    # A SECOND tick at the same time re-fires NOTHING (deduped per slot+day).
    pub2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    out2 = cap.run_slot_ticks(RUN_DATE, store=store, publisher=pub2,
                              now=_edt("13:00"), kv=kv)
    assert out2 == []
    assert pub2.calls == []


def test_run_slot_ticks_last_slot_is_catch_all(armed):
    # A tick at 19:00 (past every slot): the 18:30 slot fires with catch_all, so
    # the PM feed AND any straggler publish. Earlier slots (07:30/12:30) also fire.
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED), _row("s", fmt="story")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    kv = _FakeKV()

    out = cap.run_slot_ticks(RUN_DATE, store=store, publisher=pub,
                             now=_edt("19:00"), kv=kv)
    # By end of day every due row has published (no orphans).
    for r in store.rows.values():
        assert r["status"] == "published"
    # Exactly-once: three rows, three published records total.
    assert len(store.published_calls) == 3
    assert kv.get(cap._slot_fire_key(RUN_DATE, "18:30")) == "done"


def test_run_slot_ticks_multi_tick_across_day_orphans_nothing_exactly_once(armed):
    # Full realistic drip: ticks at 08:00, 13:00, 19:00. Spaced, exactly-once,
    # nothing orphaned by end of day.
    store = _FakeStore([_row(AM_FEED), _row(PM_FEED), _row("s", fmt="story")])
    kv = _FakeKV()

    p1 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.run_slot_ticks(RUN_DATE, store=store, publisher=p1, now=_edt("08:00"), kv=kv)
    assert store.rows[AM_FEED]["status"] == "published"
    assert store.rows["s"]["status"] == "pending"      # midday not yet
    assert store.rows[PM_FEED]["status"] == "pending"  # PM not yet

    p2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.run_slot_ticks(RUN_DATE, store=store, publisher=p2, now=_edt("13:00"), kv=kv)
    assert store.rows["s"]["status"] == "published"    # midday reached
    assert store.rows[PM_FEED]["status"] == "pending"  # PM still waiting

    p3 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.run_slot_ticks(RUN_DATE, store=store, publisher=p3, now=_edt("19:00"), kv=kv)
    # End of day: all published, exactly once.
    for r in store.rows.values():
        assert r["status"] == "published"
    assert len(store.published_calls) == 3


# ---- store REST params (unit test the filter/claim/update SQL) --------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _RecordingHTTP:
    def __init__(self, get_resp=None, patch_resp=None):
        self.calls = []
        self._get = get_resp or _Resp(200, [])
        self._patch = patch_resp or _Resp(200, [])

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params or {}, headers or {}))
        return self._get

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("patch", url, params or {}, headers or {}, json or {}))
        return self._patch


def _store(http):
    return pcs.SupabaseCalendarStore(url="https://x.supabase.co",
                                     service_key="SECRET_KEY", http=http)


def test_due_rows_rest_filter():
    http = _RecordingHTTP(get_resp=_Resp(200, [_row("a")]))
    store = _store(http)
    rows = store.due_rows("lasso", RUN_DATE)

    assert rows == [_row("a")]
    _, url, params, _headers = http.calls[0]
    assert url.endswith("/rest/v1/content_calendar")
    assert params["gym_id"] == "eq.lasso"
    assert params["post_date"] == f"eq.{RUN_DATE}"       # only the run date
    assert params["status"] == "not.in.(published,denied,killed)"
    assert params["published_at"] == "is.null"           # never re-publish
    assert params["image_url"] == "not.is.null"


def test_mark_publishing_atomic_claim_params_and_true_on_one_row():
    # PostgREST returned exactly one representation row -> the claim was won.
    http = _RecordingHTTP(patch_resp=_Resp(200, [{"id": "a", "status": "publishing"}]))
    store = _store(http)
    won = store.mark_publishing("a")

    assert won is True
    _, _url, params, _headers, body = http.calls[0]
    # the conditional claim: unclaimed (pending OR client-approved) + unpublished ONLY.
    # 'approved' became claimable with the Zernio client lane (a client approves BEFORE
    # the publish lane picks the row up); exactly-once holds because a claimed row is
    # 'publishing', which is not in the set.
    assert params["id"] == "eq.a"
    assert params["status"] == "in.(pending,approved)"
    assert params["published_at"] == "is.null"
    assert body == {"status": "publishing"}


def test_mark_publishing_false_when_no_row_updated():
    # Zero rows came back -> another worker already claimed/published it.
    http = _RecordingHTTP(patch_resp=_Resp(200, []))
    store = _store(http)
    assert store.mark_publishing("a") is False


def test_mark_published_writes_status_time_and_media():
    http = _RecordingHTTP(patch_resp=_Resp(200, [{"id": "a"}]))
    store = _store(http)
    store.mark_published("a", "MEDIA_9", "2026-08-10T18:30:00+00:00")

    _, _url, params, _headers, body = http.calls[0]
    assert params["id"] == "eq.a"
    assert body["status"] == "published"
    assert body["published_at"] == "2026-08-10T18:30:00+00:00"
    assert body["late_post_id"] == "MEDIA_9"


def test_mark_publish_failed_reverts_to_pending_only():
    http = _RecordingHTTP(patch_resp=_Resp(200, [{"id": "a"}]))
    store = _store(http)
    store.mark_publish_failed("a")

    _, _url, params, _headers, body = http.calls[0]
    assert params["id"] == "eq.a"
    assert body == {"status": "pending"}                 # nothing else recorded


def test_account_for_skips_non_ig_fb_platforms():
    # Dale/ENG 2026-08-22: a googlebusiness row must NEVER be mapped into the IG/FB lane
    # (it was silently posting the Google caption to Instagram). _account_for returns None
    # for any platform that is not instagram/facebook, so the caller skips it.
    from agent import calendar_autopublish as ca
    assert ca._account_for({"account": "googlebusiness"}, "eng") is None
    assert ca._account_for({"account": "youtube"}, "eng") is None
    assert ca._account_for({"account": ""}, "eng") is None


def test_stale_story_self_heals_and_publishes_same_tick(armed, monkeypatch):
    """Dale/ENG 2026-08-22: an edited-caption story used to strand silently on 'approved'.
    Now the publish lane re-burns the current caption onto fresh media and publishes it in
    the SAME tick (only holds if the re-burn cannot run)."""
    from agent import story_image, story_reburn
    NEW = "https://cdn/healed__story.jpg"
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url="https://cdn/old__story.jpg", caption="edited caption")
    row["source_media_url"] = "https://cdn/raw.jpg"
    store = _FakeStore([row])
    store.patch_image_url = lambda gym, rid, url: store.rows[rid].__setitem__("image_url", url)
    # OLD media is stale; the re-burned NEW media carries the caption.
    monkeypatch.setattr(story_image, "story_media_carries_caption", lambda url, cap: url == NEW)
    monkeypatch.setattr(story_reburn, "should_reburn", lambda r: True)
    monkeypatch.setattr(story_reburn, "reburn", lambda *a, **k: NEW)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                    approved_only=True, catch_all=True)
    # it healed the media and published this tick, rather than holding
    assert store.rows["s"]["image_url"] == NEW
    assert [c[0] for c in store.published_calls] == ["s"]


def test_stale_story_without_source_media_holds_not_publishes(armed, monkeypatch):
    """The other side: a stale story that CANNOT re-burn (no source_media_url) is HELD,
    never published captionless (no regression)."""
    from agent import story_image, story_reburn
    row = _row("s2", account="instagram", fmt="story", status="approved",
               image_url="https://cdn/old__story.jpg", caption="edited caption")
    store = _FakeStore([row])
    monkeypatch.setattr(story_image, "story_media_carries_caption", lambda url, cap: False)
    monkeypatch.setattr(story_reburn, "should_reburn", lambda r: False)  # no source_media_url
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                    approved_only=True, catch_all=True)
    assert store.published_calls == []          # held, not published
    assert store.rows["s2"]["status"] == "approved"


# ---- anti-flood daily cap + feed aspect preflight (Dale/Bryan 2026-08-24) ---

def test_daily_cap_publishes_up_to_cap_and_drips_rest(armed, monkeypatch):
    """A repaired gym's backlog must DRIP, not flood: with a cap of 2, only 2 of 5 due
    rows publish this run; the rest are left approved/pending for a later day."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    store = _FakeStore([_row(x) for x in ("a", "b", "c", "d", "e")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True, daily_cap=2)
    assert len(summary["published"]) == 2          # only the cap went out
    assert len(pub.calls) == 2                      # only 2 network calls made
    assert len(summary["waiting"]) == 3            # the rest dripped to a later day


def test_daily_cap_counts_rows_already_published_today(armed, monkeypatch):
    """The cap counts what already went out earlier today (kv), so a second run in the
    same day does not blow past the daily limit."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 2)   # cap already used up
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    store = _FakeStore([_row("a"), _row("b")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True, daily_cap=2)
    assert summary["published"] == []              # nothing more today
    assert pub.calls == []


def test_no_cap_when_daily_cap_none(armed, monkeypatch):
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 999)  # ignored when cap None
    store = _FakeStore([_row("a"), _row("b"), _row("c")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True, daily_cap=None)
    assert set(summary["published"]) == {"a", "b", "c"}


def _jpeg(w, h):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 50, 80)).save(buf, "JPEG")
    return buf.getvalue()


def test_feed_preflight_reframes_out_of_aspect_then_publishes(armed, monkeypatch):
    """ENG/Dale 2026-08-24: a too-tall feed photo used to 400 at Zernio and strand on
    'approved'. The preflight reframes it to an in-spec card, swaps image_url, and the
    row publishes in the same tick."""
    from agent import feed_image, media_host
    NEW = "https://cdn/NEW__feed.jpg"
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_host, "download_bytes", lambda url, client=None: _jpeg(600, 1080))
    monkeypatch.setattr(feed_image, "make_feed_safe_from_bytes", lambda b, out: out)  # "reframed"
    monkeypatch.setattr(media_host, "host_media", lambda path, key: NEW)
    row = _row("f", account="instagram", fmt="feed", status="approved",
               image_url="https://cdn/old.jpg")
    store = _FakeStore([row])
    store.patch_image_url = lambda g, rid, url: store.rows[rid].__setitem__("image_url", url)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                    approved_only=True, catch_all=True)
    assert store.rows["f"]["image_url"] == NEW
    assert [rid for rid, _, _ in store.published_calls] == ["f"]


def test_feed_preflight_noop_when_in_spec(armed, monkeypatch):
    """An already-in-spec image is untouched and still publishes with its original url."""
    from agent import media_host
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_host, "download_bytes", lambda url, client=None: _jpeg(1080, 1080))
    row = _row("f", account="instagram", fmt="feed", status="approved",
               image_url="https://cdn/ok.jpg")
    store = _FakeStore([row])
    store.patch_image_url = lambda g, rid, url: store.rows[rid].__setitem__("image_url", url)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                    approved_only=True, catch_all=True)
    assert store.rows["f"]["image_url"] == "https://cdn/ok.jpg"
    assert [rid for rid, _, _ in store.published_calls] == ["f"]


def test_feed_preflight_holds_known_bad_image_when_rehost_fails(armed, monkeypatch):
    """FAIL-SAFE (audit MAJOR): when the image is CONFIRMED out-of-aspect but the re-frame /
    re-host cannot run, the row is HELD (left approved, never published) rather than shipping
    a known-400 image. It never gets marked published and stays retryable."""
    from agent import feed_image, media_host
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_host, "download_bytes", lambda url, client=None: _jpeg(600, 1080))
    monkeypatch.setattr(feed_image, "make_feed_safe_from_bytes", lambda b, out: out)  # reframed ok
    monkeypatch.setattr(media_host, "host_media", lambda path, key: None)  # ...but re-host FAILS
    monkeypatch.setattr(cap, "_alert_feed_needs_reframe", lambda rid, gym: None)
    row = _row("f", account="instagram", fmt="feed", status="approved",
               image_url="https://cdn/old.jpg")
    store = _FakeStore([row])
    store.patch_image_url = lambda g, rid, url: store.rows[rid].__setitem__("image_url", url)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)
    assert store.published_calls == []             # never published a known-bad image
    assert pub.calls == []                          # never even reached the network call
    assert store.rows["f"]["status"] == "approved"  # held, still retryable
    assert "f" in summary["waiting"]


def test_feed_preflight_unknown_aspect_fails_open(armed, monkeypatch):
    """If the aspect can't be DETERMINED (fetch failed), pass through unchanged (fail open,
    self-heals next tick) — only a CONFIRMED-bad image is held."""
    from agent import media_host
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_host, "download_bytes", lambda url, client=None: None)  # fetch fails
    row = _row("f", account="instagram", fmt="feed", status="approved",
               image_url="https://cdn/old.jpg")
    store = _FakeStore([row])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                    approved_only=True, catch_all=True)
    assert [rid for rid, _, _ in store.published_calls] == ["f"]  # still published as-is


def test_feed_preflight_skips_story_rows(armed, monkeypatch):
    """A story is framed by its own burner; the feed preflight must never touch it."""
    from agent import media_host
    calls = []
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_host, "download_bytes",
                        lambda url, client=None: calls.append(url))
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url="https://cdn/story.jpg")
    store = _FakeStore([row])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                    approved_only=True, catch_all=True)
    assert calls == []                             # preflight never fetched a story image


# ---- client lane fires AT the slot, publish-now, truthful published_at ----------
# (audit 2026-08-25 CRITICAL: catch_all=True swept pre-approved rows at ~midnight, and
#  autonomous rows were handed to Zernio as scheduled yet marked published immediately)

def _zern_capture(results):
    """A fake zernio_publish that records scheduled_for per row."""
    def _pub(draft, account, scheduled_for=None):
        results.append((draft.draft_id, scheduled_for))
        return PublishResult(ok=True, mode="published", media_id="Z1")
    return _pub


def test_client_row_waits_for_its_slot_then_publishes_now(armed, monkeypatch):
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    # a client-gym row; route it to a fake client account so the zernio path runs
    class _Acct:
        key = "gymx_ig"; platform = "instagram"; display_name = "Gym X"
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    row = _row("cx", status="approved")
    row["gym_id"] = "gymx"
    store = _FakeStore([row])
    store.rows["cx"]["gym_id"] = "gymx"
    slot = cap.slot_time_for_row(row)                      # the row's OWN stable slot
    hh, mm = slot.split(":")
    before = f"{RUN_DATE}T{int(hh)-1 if int(hh) > 0 else 0:02d}:{mm}:00-04:00"
    after = f"{RUN_DATE}T{hh}:{mm}:01-04:00"
    sent = []
    # BEFORE the slot: held (waiting), nothing published — no midnight firing
    s1 = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=before,
                         approved_only=True, catch_all=False,
                         zernio_publish=_zern_capture(sent))
    assert s1["published"] == [] and "cx" in s1["waiting"] and sent == []
    # AT the slot: publishes NOW (scheduled_for=None -> truthful published_at)
    s2 = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=after,
                         approved_only=True, catch_all=False,
                         zernio_publish=_zern_capture(sent))
    assert s2["published"] == ["cx"]
    assert sent == [(store.rows["cx"].get("draft_id") or sent[0][0], None)] or \
           (len(sent) == 1 and sent[0][1] is None)


def test_autonomous_client_also_publishes_now_at_slot(armed, monkeypatch):
    """Autonomous gyms no longer hand Zernio a future scheduledFor (which was marked
    published immediately, hours before the post existed). They fire at slot time too."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    class _Acct:
        key = "gymx_ig"; platform = "instagram"; display_name = "Gym X"
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    row = _row("ax", status="pending")
    row["gym_id"] = "gymx"
    store = _FakeStore([row])
    sent = []
    s = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=LATE_NOW,
                        approved_only=False, catch_all=False,
                        zernio_publish=_zern_capture(sent))
    assert s["published"] == ["ax"]
    assert len(sent) == 1 and sent[0][1] is None            # publish NOW, never scheduled


def test_past_date_catchup_row_is_always_due(armed, monkeypatch):
    """A late-approved YESTERDAY row must sweep immediately (its day already passed),
    even before today's identical wall-clock slot."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    class _Acct:
        key = "gymx_ig"; platform = "instagram"; display_name = "Gym X"
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    row = _row("px", status="approved", post_date="2026-08-09")   # yesterday
    row["gym_id"] = "gymx"
    store = _FakeStore([row])
    # store's due_rows filters post_date == run_date; widen the fake for catchup reads
    store.due_rows = lambda gym_id, run_date, catchup_days=0: [dict(store.rows["px"])]
    sent = []
    early = f"{RUN_DATE}T00:05:00-04:00"                     # long before any slot
    s = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=early,
                        approved_only=True, catch_all=False, catchup_days=7,
                        zernio_publish=_zern_capture(sent))
    assert s["published"] == ["px"] and len(sent) == 1 and sent[0][1] is None


# ---- per-gym posting timezone (Blake 2026-08-25) ---------------------------------

def test_gym_timezone_slots_fire_on_the_gyms_own_wall_clock(armed, monkeypatch):
    """A Denver gym's 18:30 slot fires at 18:30 DENVER time (20:30 ET), not 18:30 ET."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "posting_timezone_for",
                        lambda gym: "America/Denver" if gym == "gymden" else "America/New_York")
    class _Acct:
        key = "gymden_ig"; platform = "instagram"; display_name = "Gym Denver"
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    row = _row("d1", status="approved")
    row["gym_id"] = "gymden"
    store = _FakeStore([row])
    store.rows["d1"]["gym_id"] = "gymden"
    slot = cap.slot_time_for_row(row)                    # e.g. "18:30"
    hh, mm = slot.split(":")
    sent = []
    def _z(draft, account, scheduled_for=None):
        sent.append(scheduled_for)
        return PublishResult(ok=True, mode="published", media_id="Z")
    # at slot time ET (= slot-2h in Denver): NOT due for the Denver gym
    at_slot_et = f"{RUN_DATE}T{hh}:{mm}:01-04:00"
    s1 = cap.publish_due(RUN_DATE, gym_id="gymden", store=store, now=at_slot_et,
                         approved_only=True, catch_all=False, zernio_publish=_z)
    assert s1["published"] == [] and "d1" in s1["waiting"]
    # at slot time DENVER (= slot+2h ET): due, publishes now
    at_slot_denver = f"{RUN_DATE}T{int(hh)+2:02d}:{mm}:01-04:00"
    s2 = cap.publish_due(RUN_DATE, gym_id="gymden", store=store, now=at_slot_denver,
                         approved_only=True, catch_all=False, zernio_publish=_z)
    assert s2["published"] == ["d1"] and sent == [None]


def test_future_local_date_row_waits_even_when_et_day_has_turned(armed, monkeypatch):
    """DATE-AWARE gate: at 00:30 ET it is still YESTERDAY in Los Angeles — an LA gym's
    rows dated the new ET day must NOT fire the evening before their local date."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    monkeypatch.setattr(config, "posting_timezone_for",
                        lambda gym: "America/Los_Angeles")
    class _Acct:
        key = "gymla_ig"; platform = "instagram"; display_name = "Gym LA"
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    row = _row("la1", status="approved")                 # dated RUN_DATE
    row["gym_id"] = "gymla"
    store = _FakeStore([row])
    store.rows["la1"]["gym_id"] = "gymla"
    # 00:30 ET on RUN_DATE = 21:30 LA time the previous evening
    just_past_midnight_et = f"{RUN_DATE}T00:30:00-04:00"
    s = cap.publish_due(RUN_DATE, gym_id="gymla", store=store,
                        now=just_past_midnight_et, approved_only=True,
                        catch_all=False, zernio_publish=lambda *a, **k: None)
    assert s["published"] == [] and "la1" in s["waiting"]


# ---- publish-boundary caption floor + avatar rail (CADENCE_SPEC defect rider) ----
# The Wave 5.3 recheck (AGENT_CALENDAR_GRADE) gained two HOLD-only checks
# 2026-08-27: a FEED whose caption is empty/thin never publishes (stories are
# exempt BY DESIGN: they publish empty-body with the caption burned on media),
# and a caption carrying a banned-audience term (LASSO avatar rail) never
# publishes. Both revert the row to pending; the publisher is never called.

@pytest.fixture
def graded(monkeypatch, armed):
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")


def _capture_alerts(monkeypatch):
    from agent import ops_alerts
    sent = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    return sent


def test_recheck_thin_feed_caption_reverts(graded, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    store = _FakeStore([_row("thin", caption="HYROX")])   # 5 chars, under the floor
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "thin" in summary["failed"] and summary["published"] == []
    assert store.rows["thin"]["status"] == "pending"      # held, not lost
    assert pub.calls == []                                 # never reached the network
    # consolidated path: publish_guard names the violation code in the alert
    assert any("thin_caption" in m for m in sent)


def test_recheck_story_empty_caption_is_exempt(graded, monkeypatch):
    _capture_alerts(monkeypatch)
    store = _FakeStore([_row("st", fmt="story", caption="")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "st" in summary["published"]                    # story publishes by design


def test_recheck_avatar_term_reverts(graded, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    caption = ("HYROX season starts soon and our coaches are ready to help you "
               "train for it. Save your spot today.")
    store = _FakeStore([_row("av", caption=caption)])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "av" in summary["failed"] and pub.calls == []
    assert store.rows["av"]["status"] == "pending"
    # consolidated path: publish_guard names the violation code in the alert
    assert any("avatar_block" in m for m in sent)


def test_recheck_floor_and_rail_off_when_grade_flag_off(armed, monkeypatch):
    """Flag-off no-op: without AGENT_CALENDAR_GRADE the new checks never run."""
    _capture_alerts(monkeypatch)
    store = _FakeStore([_row("thin2", caption="HYROX")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "thin2" in summary["published"]                 # pre-cadence behavior


# ---- repeat-failure alert dedup per (row, reason) per day (topfuel_fb 2026-08-27) --
def test_repeat_failure_alert_once_per_day_per_reason(monkeypatch):
    """A stuck row that needs a HUMAN (e.g. 'no Facebook page selected') must alert
    once when it crosses the threshold and then at most once per UTC day per distinct
    reason, NOT on every ~1-min retry. The retry loop itself is untouched (the counter
    keeps counting; nothing here blocks another attempt)."""
    from datetime import datetime, timezone
    from agent import ops_alerts
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: fired.append(m))
    exc = RuntimeError("topfuel_fb: no Facebook page selected; the gym must pick a page.")
    day1 = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)

    for _ in range(8):                                # attempts 1..8, same reason
        cap._note_repeat_failure("row-8151a344", "topfuel", exc, now=day1)
    assert len(fired) == 1                            # threshold alert, then silence
    assert "no Facebook page selected" in fired[0]

    day2 = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    for _ in range(3):                                # still stuck the next day
        cap._note_repeat_failure("row-8151a344", "topfuel", exc, now=day2)
    assert len(fired) == 2                            # one nudge per day, no more

    # a DIFFERENT failure reason is new signal: it gets one alert of its own
    cap._note_repeat_failure("row-8151a344", "topfuel",
                             ValueError("token expired"), now=day2)
    assert len(fired) == 3

    # a different ROW crossing the threshold alerts independently
    for _ in range(5):
        cap._note_repeat_failure("row-other", "topfuel", exc, now=day2)
    assert len(fired) == 4


# ---- expired-row watchdog: rows that can never publish must be reported ----------
class _ExpiredStore:
    def __init__(self, rows):
        self._rows = rows
        self.asked = []

    def expired_rows(self, before_date, statuses=("approved", "pending")):
        self.asked.append(before_date)
        return self._rows


class _MemKV:
    def __init__(self):
        self.d = {}

    def get(self, k, default=""):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def test_expired_rows_alert_once_per_gym_per_day():
    """LIVE GAP: due_rows only looks back 7 days, so an older approved row is never
    read, claimed or failed — it just stops existing to the publisher. 11 APPROVED
    LASSO posts and 26 GritX rows died silently this way with no reject_reason."""
    rows = [
        {"id": "1", "gym_id": "lasso", "account": "instagram",
         "post_date": "2026-08-07", "status": "approved"},
        {"id": "2", "gym_id": "lasso", "account": "facebook",
         "post_date": "2026-08-11", "status": "approved"},
        {"id": "3", "gym_id": "gritx", "account": "instagram",
         "post_date": "2026-08-17", "status": "pending"},
    ]
    store, kv, seen = _ExpiredStore(rows), _MemKV(), []
    out = cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                                 now="2026-08-30T12:00:00")
    assert sorted(out) == ["gritx", "lasso"]
    assert len(seen) == 2
    lasso_line = next(m for m in seen if m.startswith("lasso:"))
    assert "2 calendar row(s)" in lasso_line
    assert "2 already APPROVED" in lasso_line
    assert "2026-08-07" in lasso_line               # names the oldest
    # The cutoff is today minus the catch-up window, not today.
    assert store.asked == ["2026-08-23"]
    # Same day again: silent (no storm).
    seen2 = []
    cap.sweep_expired_rows(store=store, kv=kv, alert=seen2.append,
                           now="2026-08-30T18:00:00")
    assert seen2 == []


def test_expired_sweep_is_silent_when_nothing_expired():
    store, kv, seen = _ExpiredStore([]), _MemKV(), []
    assert cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                                  now="2026-08-30T12:00:00") == []
    assert seen == []


def test_expired_sweep_survives_a_read_failure():
    class _Boom:
        def expired_rows(self, before_date, statuses=("approved", "pending")):
            raise RuntimeError("supabase down")

    seen = []
    assert cap.sweep_expired_rows(store=_Boom(), kv=_MemKV(), alert=seen.append,
                                  now="2026-08-30T12:00:00") == []
    assert seen == []


def test_expired_sweep_excludes_google_business_rows():
    """GBP publishes through its OWN lane (gbp_store.approved_gbp_rows), which has NO
    age cutoff at all — an aged approved GBP row is still perfectly publishable.
    due_rows excludes them for the same reason, so counting them here would fire false
    'can never publish' alerts on healthy rows."""
    from agent.portal_calendar_store import SupabaseCalendarStore

    seen = {}

    class _Http:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(params or {})

            class _R:
                status_code = 200

                def json(self):
                    return []
            return _R()

    store = SupabaseCalendarStore(url="http://x", service_key="k", http=_Http())
    store.expired_rows("2026-08-23")
    assert seen.get("account") == "neq.googlebusiness", \
        "GBP rows must not be reported as expired"
