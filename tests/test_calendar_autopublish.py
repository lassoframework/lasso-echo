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
    mark_publishing is an ATOMIC claim (only status=='pending' AND no published_at
    wins). A `claim_returns` override lets a test simulate a lost race.
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
        if not r or r.get("status") != "pending" or r.get("published_at"):
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

    def mark_publish_failed(self, row_id):
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


# ---- FIX 1: time-of-day spacing --------------------------------------------
# Slots come from summit_queue.SPRINT_SLOT_TIMES = 07:30, 12:30, 18:30 in
# POSTING_TIMEZONE (America/New_York by default). A row publishes only once its
# assigned slot time is <= the current local `now` (injected). `now` values below
# are given in EDT (-04:00) so the mapping to local time is explicit.


def _edt(hhmm):
    return f"2026-08-10T{hhmm}:00-04:00"


def test_assign_slots_orders_feed_before_story_then_by_id():
    # Deterministic ordering: feed(0) before story(1), then stable by id.
    rows = [_row("z_story", fmt="story"), _row("a_feed", fmt="feed"),
            _row("m_feed", fmt="feed")]
    pairs = cap.assign_slots(rows)
    assert [r["id"] for r, _ in pairs] == ["a_feed", "m_feed", "z_story"]
    assert [slot for _, slot in pairs] == ["07:30", "12:30", "18:30"]


def test_assign_slots_wraps_when_more_rows_than_slots():
    rows = [_row(f"r{i}", fmt="feed") for i in range(4)]
    pairs = cap.assign_slots(rows)
    assert [slot for _, slot in pairs] == ["07:30", "12:30", "18:30", "07:30"]


def test_is_due_predicate_before_and_after_slot():
    assert cap.is_due("12:30", now=_edt("12:30")) is True    # exactly at slot
    assert cap.is_due("12:30", now=_edt("12:29")) is False   # one minute early
    assert cap.is_due("12:30", now=_edt("18:31")) is True    # well past


def test_nothing_publishes_before_first_slot(armed):
    # now (07:00 EDT) is before slot 1 (07:30) -> zero publishes, nothing claimed.
    store = _FakeStore([_row("a"), _row("b"), _row("c")])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("07:00"))

    assert summary["published"] == []
    assert set(summary["waiting"]) == {"a", "b", "c"}
    assert pub.calls == []
    assert store.publishing_calls == []                 # never claimed early


def test_only_first_slot_row_publishes_after_slot_one(armed):
    # now (08:00 EDT) is past slot 1 (07:30) but before slot 2 (12:30).
    store = _FakeStore([_row("a"), _row("b"), _row("c")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("08:00"))

    # Ordered a,b,c -> slots 07:30,12:30,18:30. Only 'a' is due.
    assert summary["published"] == ["a"]
    assert set(summary["waiting"]) == {"b", "c"}
    assert [d.draft_id for d, _ in pub.calls] == ["a"]


def test_all_rows_publish_after_last_slot(armed):
    store = _FakeStore([_row("a"), _row("b"), _row("c")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("19:00"))

    assert set(summary["published"]) == {"a", "b", "c"}
    assert summary["waiting"] == []


def test_story_publishes_after_its_paired_feed(armed):
    # Within one account a feed takes slot 1 and its story slot 2, so at 08:00 EDT
    # only the feed is due; the story waits for the later slot.
    store = _FakeStore([
        _row("feed1", account="instagram", fmt="feed"),
        _row("story1", account="instagram", fmt="story"),
    ])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("08:00"))

    assert summary["published"] == ["feed1"]            # feed first
    assert summary["waiting"] == ["story1"]             # story not yet due

    # A later run past slot 2 drips the story out; the feed is NOT re-published.
    pub2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M2"))
    summary2 = cap.publish_due(RUN_DATE, store=store, publisher=pub2,
                               now=_edt("13:00"))
    assert summary2["published"] == ["story1"]
    assert [d.draft_id for d, _ in pub2.calls] == ["story1"]


def test_drip_across_runs_is_exactly_once(armed):
    # Runs across the day drip the rows out slot by slot; no row ever republishes
    # and no row publishes before its slot on the run that publishes it. (As each
    # earlier row publishes it leaves the due set, so the remaining rows re-rank
    # onto the earlier, already-passed slots on the next run.)
    store = _FakeStore([_row("a"), _row("b"), _row("c")])

    p1 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s1 = cap.publish_due(RUN_DATE, store=store, publisher=p1, now=_edt("08:00"))
    assert s1["published"] == ["a"]                     # only slot-1 row is due
    assert set(s1["waiting"]) == {"b", "c"}

    # A later run past slot 2 (13:00): 'a' is gone; b,c re-rank to slots 1 and 2,
    # both now passed -> both publish. 'a' is never re-touched.
    p2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=p2, now=_edt("13:00"))
    assert set(s2["published"]) == {"b", "c"}
    assert "a" not in [d.draft_id for d, _ in p2.calls]  # 'a' never re-touched

    # Every row published exactly once across the runs.
    assert sorted(rid for rid, _, _ in store.published_calls) == ["a", "b", "c"]


def test_published_row_never_republishes_on_a_later_slot_run(armed):
    # Belt-and-braces exactly-once under spacing: after 'a' is published in run 1,
    # a later-slot run for the same store never claims or re-publishes it.
    store = _FakeStore([_row("a"), _row("b")])
    p1 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    cap.publish_due(RUN_DATE, store=store, publisher=p1, now=_edt("08:00"))
    assert store.rows["a"]["status"] == "published"

    p2 = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=p2, now=_edt("19:00"))
    assert "a" not in s2["published"]
    assert "a" not in [d.draft_id for d, _ in p2.calls]


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
    # the conditional claim: pending + unpublished ONLY
    assert params["id"] == "eq.a"
    assert params["status"] == "eq.pending"
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
