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
                              now="2026-08-10T18:30:00+00:00")

    assert summary["ok"] is True
    assert set(summary["published"]) == {"a", "b", "c"}
    assert summary["failed"] == []
    assert len(pub.calls) == 3
    # every row was claimed then recorded published with the fake media id + now
    assert set(store.publishing_calls) == {"a", "b", "c"}
    assert {rid for rid, _, _ in store.published_calls} == {"a", "b", "c"}
    for _rid, media_id, published_at in store.published_calls:
        assert media_id == "M"
        assert published_at == "2026-08-10T18:30:00+00:00"


# ---- EXACTLY-ONCE / dup guard ----------------------------------------------

def test_already_published_row_is_skipped(armed):
    # A row already stamped published_at is never claimed and never re-published.
    store = _FakeStore([
        _row("done", status="published", published_at="2026-08-10T00:00:00+00:00",
             late_post_id="OLD"),
        _row("fresh"),
    ])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub)

    assert summary["published"] == ["fresh"]
    assert "done" not in store.publishing_calls          # never claimed
    assert [d.draft_id for d, _ in pub.calls] == ["fresh"]


def test_lost_claim_is_not_published(armed):
    # mark_publishing returns False (another worker won the claim) -> SKIP, no publish.
    store = _FakeStore([_row("x"), _row("y")], claim_returns={"x": False, "y": True})
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub)

    assert summary["published"] == ["y"]
    assert "x" in summary["skipped"]
    assert [d.draft_id for d, _ in pub.calls] == ["y"]   # x never published


def test_rerun_after_success_publishes_nothing(armed):
    store = _FakeStore([_row("a"), _row("b")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    first = cap.publish_due(RUN_DATE, store=store, publisher=pub)
    assert set(first["published"]) == {"a", "b"}

    # Same store, a second run: rows are now published, so due_rows returns none.
    pub2 = _FakePublisher()
    second = cap.publish_due(RUN_DATE, store=store, publisher=pub2)
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
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub)

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
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub)

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
    cap.publish_due(RUN_DATE, store=store, publisher=pub)

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

    summary = cap.publish_due(RUN_DATE, store=store, publisher=publisher)

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
    cap.publish_due(RUN_DATE, store=store, publisher=pub, notifier=note)

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
    cap.publish_due(RUN_DATE, store=store, publisher=pub, notifier=note)
    assert note.notices == []


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
