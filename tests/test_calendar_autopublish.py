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
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

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


def test_now_iso_serializes_injected_datetime_for_supabase_json():
    now = datetime(2026, 9, 28, 21, 19, 32, 750000, tzinfo=timezone.utc)

    assert cap._now_iso(now) == "2026-09-28T21:19:32.750000+00:00"


def test_now_iso_preserves_existing_iso_string():
    assert cap._now_iso(LATE_NOW) == LATE_NOW


# ---- fakes -----------------------------------------------------------------

def _row(row_id, account="instagram", fmt="feed", post_date=RUN_DATE,
         status="pending", caption=None, image_url="https://cdn/x.jpg",
         published_at=None, late_post_id=None):
    # UNIQUE PER ROW (2026-09-05). These fixtures defaulted every row to one caption,
    # which is not a real month and collides with the publisher content guard added
    # after the Tough Temple double post (a caption already sent to an account is
    # never sent again). Tests that are ABOUT content still pass an explicit caption.
    caption = caption if caption is not None else f"caption for {row_id}"
    return {
        "id": row_id, "gym_id": "lasso", "post_date": post_date,
        "account": account, "format": fmt, "status": status,
        "caption": caption, "image_url": image_url,
        "published_at": published_at, "late_post_id": late_post_id,
        "source_media_url": None, "media_not_ready_reason": None,
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

    def _transition_unpublished_claim(self, gym_id, row_id, status, reason):
        row = self.rows.get(row_id)
        if (not row or row.get("gym_id") != gym_id or row.get("status") != "publishing"
                or row.get("published_at") is not None or row.get("late_post_id") is not None):
            return None
        row.update(status=status, reject_reason=reason)
        return dict(row)

    def mark_duplicate_content(self, gym_id, row_id, reason):
        return self._transition_unpublished_claim(gym_id, row_id, "deleted", reason)

    def release_content_ledger_claim(self, gym_id, row_id, previous, reason):
        return self._transition_unpublished_claim(gym_id, row_id, previous, reason)


class _AtomicSlotStore(_FakeStore):
    """Offline model of the SQL advisory-lock reservation and row claim."""

    def __init__(self, rows):
        super().__init__(rows)
        self._slot_lock = threading.Lock()

    def claim_publish_slot(self, row_id, gym, day, timezone_name, capacity,
                           approved_only):
        with self._slot_lock:
            row = self.rows[row_id]
            if row.get("status") not in ("pending", "approved"):
                return False
            if approved_only and row["status"] != "approved":
                return False
            used = sum(r.get("status") in ("publishing", "published")
                       and r.get("publish_reservation_day") == day
                       for r in self.rows.values()
                       if (r.get("gym_id"), r.get("account"), r.get("format")) ==
                       (gym, row.get("account"), row.get("format")))
            if used >= capacity:
                return False
            if not super().mark_publishing(row_id):
                return False
            row["publish_reservation_day"] = day
            return True


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


def test_needs_client_safe_review_pillar_never_autopublishes(armed):
    """HARD BLOCK (2026-09-11): a row from the no-media Astra fallback
    (pillar==no_media_astra_seed.NEEDS_CLIENT_SAFE_REVIEW_PILLAR) must never
    auto-publish, even when every other gate would otherwise let it through:
    status already 'approved' and catch_all bypassing the slot gate. Proves
    the block is unconditional, not merely a side effect of approved_only/
    trust already covering it (this test runs with approved_only OFF, the
    LASSO-lane default that would otherwise auto-publish a pending/approved
    row with no extra gate at all)."""
    from agent import no_media_astra_seed as nmas
    row = _row("needs-review", status="approved")
    row["pillar"] = nmas.NEEDS_CLIENT_SAFE_REVIEW_PILLAR
    ordinary = _row("ordinary", status="approved")
    store = _FakeStore([row, ordinary])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "needs-review" not in summary["published"]
    assert "needs-review" in summary["skipped"]
    assert "needs-review" not in store.publishing_calls   # never even claimed
    assert "ordinary" in summary["published"]
    assert [d.draft_id for d, _ in pub.calls] == ["ordinary"]


def test_client_infographic_fill_suffixed_pillar_never_autopublishes(armed):
    """HARD BLOCK covers client_infographic_fill.py too: its pillar is the
    source's real category PLUS a suffix (e.g. 'offer::needs_client_safe_
    review'), not an exact match to no_media_astra_seed's marker -- proves
    the endswith() check, not just the == check, actually fires."""
    from agent import client_infographic_fill as cif
    row = _row("needs-review-2", status="approved")
    row["pillar"] = "offer" + cif._NEEDS_CLIENT_SAFE_REVIEW_SUFFIX
    ordinary = _row("ordinary2", status="approved")
    store = _FakeStore([row, ordinary])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "needs-review-2" not in summary["published"]
    assert "needs-review-2" in summary["skipped"]
    assert "needs-review-2" not in store.publishing_calls
    assert "ordinary2" in summary["published"]


# ---- legacy igfill media hard block (2026-10-04) -----------------------------
# Swift River rows e11f7bec-7ec4-47da-b4b7-b53da85ff0eb / 8de2ef1e-167e-42bb-9954-
# db0800c1348e (dated Sep 25) published Oct 1 on legacy igfill_2026-09-10 media
# because the 2026-09-11 client-safe-review block only checks the PILLAR and the
# row's pillar carried no suffix. The block now also keys on the MEDIA URL (feed
# image_url and story source_media_url) and is fail-closed when it cannot run.

def test_legacy_igfill_media_blocked_with_plain_pillar(armed):
    """A feed row whose image_url is legacy client_infographic_fill media must
    never auto-publish even when the pillar has NO review suffix, even with
    status 'approved' and catch_all -- approved/autonomous lanes included."""
    row = _row("legacy-igfill", status="approved",
               image_url="https://cdn.example.com/lib/x/igfill_2026-09-10_path.png")
    ordinary = _row("ordinary3", status="approved")
    store = _FakeStore([row, ordinary])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "legacy-igfill" not in summary["published"]
    assert "legacy-igfill" in summary["skipped"]
    assert "legacy-igfill" not in store.publishing_calls   # never even claimed
    assert "ordinary3" in summary["published"]


def test_legacy_igfill_story_source_media_blocked(armed):
    """STORY variant: the legacy igfill media may sit on source_media_url while
    image_url is the (separately named) burned story card -- the block must key
    on source_media_url for stories, not only the feed image_url."""
    row = _row("legacy-story", fmt="story", status="approved",
               image_url="https://cdn.example.com/story_burned_x.png",
               )
    row["source_media_url"] = \
        "https://cdn.example.com/lib/x/igfill_2026-09-10_path.png"
    ordinary = _row("ordinary4", fmt="story", status="approved",
                    image_url="https://cdn.example.com/story_burned_y.png")
    ordinary["source_media_url"] = "https://cdn.example.com/raw/client_y.jpg"
    store = _FakeStore([row, ordinary])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "legacy-story" not in summary["published"]
    assert "legacy-story" in summary["skipped"]
    assert "legacy-story" not in store.publishing_calls
    assert "ordinary4" in summary["published"]


def test_legacy_igfill_feed_with_swapped_real_image_publishes(armed):
    """P1 repair: the legacy-igfill check must examine the FEED image_url ONLY.
    A feed whose live image_url was already swapped to a client-real photo must
    NOT stay blocked by a stale igfill provenance on source_media_url."""
    row = _row("swapped-feed", status="approved",
               image_url="https://cdn.example.com/client/real_photo_oct.jpg")
    row["source_media_url"] = \
        "https://cdn.example.com/lib/x/igfill_2026-09-10_path.png"
    blocked = _row("still-legacy", status="approved",
                   image_url="https://cdn.example.com/lib/x/igfill_2026-09-10_b.png")
    store = _FakeStore([row, blocked])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "swapped-feed" in summary["published"]       # stale provenance ignored
    assert "still-legacy" in summary["skipped"]         # real igfill still blocked
    assert "still-legacy" not in store.publishing_calls


def test_legacy_igfill_with_review_pillar_still_alerts(armed, monkeypatch):
    """P2 repair: a legacy-media row whose pillar ALSO carries a review suffix is
    held by both rails, but the legacy-media alert must still fire (so the media
    gets swapped) -- the pillar hold must not swallow it."""
    sent = _capture_alerts(monkeypatch)
    from agent import no_media_astra_seed as nmas
    row = _row("legacy-and-review", status="approved",
               image_url="https://cdn.example.com/lib/x/igfill_2026-09-10_c.png")
    row["pillar"] = nmas.NEEDS_CLIENT_SAFE_REVIEW_PILLAR
    store = _FakeStore([row])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "legacy-and-review" not in summary["published"]
    assert "legacy-and-review" in summary["skipped"]
    assert "legacy-and-review" not in store.publishing_calls
    assert any("legacy-and-review" in m and "legacy client_infographic_fill" in m
               for m in sent)


def test_failed_alert_then_retry_is_not_suppressed(armed, monkeypatch):
    """P1 repair: the KV dedupe stamp happens ONLY after a CONFIRMED delivered
    alert. A transient delivery failure (alert returns None, as ops_alerts does
    when the Slack post fails) must leave the key un-stamped so the next tick
    retries the notice; after a confirmed delivery the key stamps and the alert
    dedupes. Fully isolated from persistent KV via a fresh in-memory ledger."""
    from agent import db as real_db
    ledger = {}

    monkeypatch.setattr(real_db, "kv_get",
                        lambda k, default="": ledger.get(k, default))
    monkeypatch.setattr(real_db, "kv_set", lambda k, v: ledger.__setitem__(k, v))

    calls = []

    def flaky_alert(message, **kwargs):
        calls.append(message)
        # attempts 1 and 2 fail transiently; attempt 3 confirms delivery
        return None if len(calls) < 3 else {"ok": True}

    monkeypatch.setattr("agent.ops_alerts.alert", flaky_alert)

    for _ in range(3):
        cap._legacy_igfill_blocked_alert("row-1", "gymx")
    assert len(calls) == 3                     # no permanent suppression on failure
    assert ledger.get("legacy_igfill_blocked_gymx_row-1") == "1"  # stamped on success

    cap._legacy_igfill_blocked_alert("row-1", "gymx")
    assert len(calls) == 3                     # confirmed alert dedupes afterwards

    # A failure-shaped dict (Slack ok=False) is ALSO not a confirmed delivery.
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda m, **k: {"ok": False, "error": "channel_not_found"})
    cap._legacy_igfill_blocked_alert("row-2", "gymx")
    assert "legacy_igfill_blocked_gymx_row-2" not in ledger


def test_separately_branded_astra_fallback_media_still_allowed(armed):
    """Ruling preservation: future separately branded Astra fallback media
    (no_media_/seed_ naming) is NOT legacy igfill and must stay publishable."""
    row = _row("astra-fallback", status="approved",
               image_url="https://cdn.example.com/lib/x/no_media_2026-10-04_card.png")
    seed = _row("seed-fallback", status="approved",
                image_url="https://cdn.example.com/lib/x/seed_2026-10-04_card.png")
    store = _FakeStore([row, seed])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    assert "astra-fallback" in summary["published"]
    assert "seed-fallback" in summary["published"]


def test_client_safe_review_rail_exception_fails_closed(armed, monkeypatch):
    """If the rail's own imports cannot load, the row is SKIPPED and an internal
    ops alert fires -- never a silent pass into the approval/claim path (the
    Oct 1 defect class: the import-exception branch used to `pass`)."""
    sent = _capture_alerts(monkeypatch)
    # A rail dependency that blows up mid-evaluation (TypeError from a
    # non-str suffix here; the production case is an import/config error) must
    # hit the same fail-closed branch.
    from agent import client_infographic_fill as _cif
    monkeypatch.setattr(_cif, "_NEEDS_CLIENT_SAFE_REVIEW_SUFFIX", 123)
    row = _row("rail-down", status="approved")
    ordinary = _row("ordinary5", status="approved")
    store = _FakeStore([row, ordinary])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True)

    # EVERY row fails closed while the rail cannot evaluate (ordinary5 too) --
    # publishing past an unevaluable rail is exactly what this repair forbids.
    assert summary["published"] == []
    assert "rail-down" in summary["skipped"]
    assert "ordinary5" in summary["skipped"]
    assert store.publishing_calls == []          # nothing was ever claimed
    assert any("rail-down" in m and "fail-closed" in m for m in sent)


def test_sample_rail_exception_fails_closed(armed, monkeypatch):
    """A failed sample check cannot let a demo row reach the claim path."""
    from agent import onboarding_demo
    sent = _capture_alerts(monkeypatch)

    def broken_sample_check(_row):
        raise RuntimeError("sample rail unavailable")

    monkeypatch.setattr(onboarding_demo, "is_sample_row", broken_sample_check)
    store = _FakeStore([_row("sample-rail-down", status="approved")])
    summary = cap.publish_due(RUN_DATE, store=store, publisher=_FakePublisher(),
                              now=LATE_NOW, catch_all=True)

    assert summary["published"] == []
    assert "sample-rail-down" in summary["skipped"]
    assert store.publishing_calls == []
    assert any("sample-rail-down" in m and "fail-closed" in m for m in sent)


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

def test_timeout_after_provider_accept_holds_claim_and_others_still_publish(
        armed, monkeypatch):
    class _Boom(Exception):
        pass

    store = _FakeStore([_row("bad"), _row("good")])
    # publisher: 'bad' raises, 'good' publishes.
    pub = _FakePublisher(per_row={})
    calls = []

    def publisher(draft, account):
        calls.append(draft.draft_id)
        if draft.draft_id == "bad":
            raise _Boom("timeout after accept")
        return PublishResult(ok=True, mode="published", media_id="M")

    monkeypatch.setattr(cap, "_alert_ambiguous_publish", lambda *a, **kw: None)
    summary = cap.publish_due(RUN_DATE, store=store, publisher=publisher, now=LATE_NOW)

    assert summary["published"] == ["good"]
    assert summary["failed"] == ["bad"]
    assert store.failed_calls == []
    assert store.rows["bad"]["status"] == "publishing"
    assert summary["held"] is True
    assert summary["recovery_required"] == ["bad"]
    assert store.rows["good"]["status"] == "published"
    assert calls == ["bad", "good"]

    cap.publish_due(RUN_DATE, store=store, publisher=publisher, now=LATE_NOW)
    assert calls == ["bad", "good"]


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


def test_lasso_paired_story_waits_for_its_feed_slot_even_in_catch_all(
        armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    pm_story = _row("pm-story", fmt="story")
    pm_story["slot_index"] = 1
    paired_feed = _row("pm-feed", status="published",
                       published_at=_edt("18:31"), late_post_id="FEED-1")
    paired_feed["slot_index"] = 1
    store = _PairedFeedStore([paired_feed, pm_story])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    before = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                             now=_edt("18:30"), catch_all=True)
    assert before["published"] == []
    assert before["waiting"] == ["pm-story"]
    assert store.publishing_calls == []

    after = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                            now=_edt("18:45"), catch_all=True)
    assert after["published"] == ["pm-story"]


class _PairedFeedStore(_FakeStore):
    def due_rows(self, gym_id, run_date):
        return [row for row in super().due_rows(gym_id, run_date)
                if row.get("status") in ("pending", "approved")]

    def list_active_logical_post_rows(self, gym, logical_id):
        assert gym == "lasso"
        return [dict(row) for row in self.rows.values()
                if row.get("logical_post_id") == logical_id]

    def rows_in_range_complete(self, gym, first, last):
        assert gym == "lasso" and first == last
        return [dict(row) for row in self.rows.values()
                if row.get("gym_id") == gym and row.get("post_date") == first]


def _paired_rows(*, logical_id="11111111-1111-4111-8111-111111111111",
                 feed_status="published", feed_media_id="META-1"):
    feed = _row("paired-feed", status=feed_status,
                published_at=(_edt("18:31") if feed_status == "published" else None),
                late_post_id=feed_media_id if feed_status == "published" else None)
    story = _row("paired-story", fmt="story")
    for row in (feed, story):
        row["slot_index"] = 1
        row["logical_post_id"] = logical_id
    return feed, story


def test_lasso_paired_story_requires_delivered_logical_feed(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    feed, story = _paired_rows()
    store = _PairedFeedStore([feed, story])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="STORY-1"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("18:45"), catch_all=True)
    assert summary["published"] == ["paired-story"]
    assert store.rows["paired-story"]["status"] == "published"


def test_due_lasso_feed_missing_story_is_held_and_reports_stall(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    row = _row("missing-story-feed", post_date="2026-10-05")
    row["slot_index"] = 0
    store = _FakeStore([row])
    monkeypatch.setattr(store, "lasso_paired_story_ready_for_feed", lambda _: False,
                        raising=False)
    failures = []
    monkeypatch.setattr(cap, "_note_repeat_failure",
                        lambda rid, gym, exc: failures.append((rid, gym, str(exc))))
    pub = _FakePublisher()
    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)
    assert result["waiting"] == [row["id"]]
    assert result["published"] == []
    assert store.publishing_calls == []
    assert failures == [(row["id"], "lasso",
                         "paired Story source proof unavailable; feed remains held")]


def test_lasso_hold_release_jobs_require_cadence_flag(armed, monkeypatch):
    from agent.jobs import lasso_backlog_feed_hold_release as backlog
    from agent.jobs import lasso_daily_paired_stories as paired
    monkeypatch.setattr(config, "lasso_three_feed_enabled", lambda: False)
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    store = _FakeStore([])
    store._client = lambda: None
    def forbidden(*args, **kwargs):
        raise AssertionError("disarmed release job called")
    monkeypatch.setattr(backlog, "run", forbidden)
    monkeypatch.setattr(paired, "release_ready_holds", forbidden)
    result = cap.publish_due("2026-10-05", store=store,
                             publisher=_FakePublisher(),
                             now="2026-10-05T23:59:00-04:00")
    assert result["published"] == []


def test_lasso_story_accepts_persisted_zernio_dedup_receipt(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    feed, story = _paired_rows(feed_media_id="")
    store = _PairedFeedStore([feed, story])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="S"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                              now=_edt("18:45"), catch_all=True)
    assert summary["published"] == ["paired-story"]


def test_summit_only_direct_ticks_include_third_story(armed, monkeypatch):
    monkeypatch.setattr(config, "lasso_three_feed_enabled", lambda: False)
    monkeypatch.setattr(config, "lasso_summit_daily_enabled", lambda day: True)
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    monkeypatch.setattr(cap, "publish_due", lambda *args, **kwargs: {"ok": True})
    kv = _FakeKV()
    cap.run_slot_ticks(RUN_DATE, now=_edt("19:00"), kv=kv)
    assert kv.get(cap._slot_fire_key(RUN_DATE, "12:15")) == "done"
    assert kv.get(cap._slot_fire_key(RUN_DATE, "18:45")) == "done"


def test_lasso_story_holds_until_logical_feed_is_actually_live(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    for status, media_id in (("pending", None), ("publishing", None),
                             ("published", None)):
        feed, story = _paired_rows(feed_status=status, feed_media_id=media_id)
        if status == "pending":
            feed["image_url"] = ""  # the paired feed is held, not publishable
        store = _PairedFeedStore([feed, story])
        pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="S"))
        summary = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                                  now=_edt("18:45"), catch_all=True)
        assert summary["published"] == []
        assert summary["waiting"] == ["paired-story"]
        assert store.publishing_calls == []


def test_lasso_story_with_logical_id_never_falls_back_to_slot(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    feed, story = _paired_rows()
    feed["logical_post_id"] = "22222222-2222-4222-8222-222222222222"
    store = _PairedFeedStore([feed, story])
    summary = cap.publish_due(RUN_DATE, store=store, publisher=_FakePublisher(),
                              now=_edt("18:45"), catch_all=True)
    assert summary["published"] == []
    assert summary["waiting"] == ["paired-story"]


def test_legacy_lasso_story_pairs_by_exact_date_account_and_slot(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    feed, story = _paired_rows(logical_id=None)
    store = _PairedFeedStore([feed, story])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="S"))
    assert cap.publish_due(RUN_DATE, store=store, publisher=pub,
                           now=_edt("18:45"))["published"] == ["paired-story"]

    feed, story = _paired_rows(logical_id=None)
    other = dict(feed, id="ambiguous-feed")
    store = _PairedFeedStore([feed, other, story])
    summary = cap.publish_due(RUN_DATE, store=store, publisher=_FakePublisher(),
                              now=_edt("18:45"))
    assert summary["published"] == []
    assert summary["waiting"] == ["paired-story"]


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


def test_lasso_three_story_direct_tick_fires_after_pm_feed(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    story = _row("pm-story", fmt="story")
    story["slot_index"] = 1
    feed = _row("pm-feed", status="published", published_at=_edt("18:31"),
                late_post_id="FEED-1")
    feed["slot_index"] = 1
    store = _PairedFeedStore([feed, story])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    kv = _FakeKV()

    cap.run_slot_ticks(RUN_DATE, store=store, publisher=pub,
                       now=_edt("18:30"), kv=kv)
    assert store.rows["pm-story"]["status"] == "pending"
    assert kv.get(cap._slot_fire_key(RUN_DATE, "18:45")) != "done"

    cap.run_slot_ticks(RUN_DATE, store=store, publisher=pub,
                       now=_edt("18:45"), kv=kv)
    assert store.rows["pm-story"]["status"] == "published"
    assert kv.get(cap._slot_fire_key(RUN_DATE, "18:45")) == "done"


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
    # The media-hold prefetch GET sees a ready row, so the claim PATCH proceeds.
    http = _RecordingHTTP(
        get_resp=_Resp(200, [{"id": "a", "image_url": "https://cdn/x.jpg",
                              "media_not_ready_reason": None}]),
        patch_resp=_Resp(200, [{"id": "a", "status": "publishing"}]))
    store = _store(http)
    won = store.mark_publishing("a")

    assert won is True
    _, _url, params, _headers, body = [c for c in http.calls if c[0] == "patch"][0]
    # the conditional claim: unclaimed (pending OR client-approved) + unpublished ONLY.
    # 'approved' became claimable with the Zernio client lane (a client approves BEFORE
    # the publish lane picks the row up); exactly-once holds because a claimed row is
    # 'publishing', which is not in the set.
    assert params["id"] == "eq.a"
    assert params["status"] == "in.(pending,approved)"
    assert params["variant_status"] == "eq.active"
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
    token = "11111111-1111-4111-8111-111111111111"
    http = _RecordingHTTP(patch_resp=_Resp(
        200, [{"id": "a", "gym_id": "lasso", "status": "pending"}]))
    store = _store(http)
    store.mark_publish_failed(
        "a", gym_id="lasso", expected_claim_token=token)

    _, _url, params, _headers, body = http.calls[0]
    assert params["id"] == "eq.a"
    assert params["gym_id"] == "eq.lasso"
    assert params["status"] == "eq.publishing"
    assert params["publish_claim_token"] == f"eq.{token}"
    assert body == {"status": "pending", "publish_reservation_day": None,
                    "publish_claim_token": None}


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
    def patch_image_url(gym, rid, url, *, expected_row):
        assert expected_row == row
        store.rows[rid]["image_url"] = url
        return dict(store.rows[rid])
    store.patch_image_url = patch_image_url
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


def test_stale_story_transports_verified_reburn_evidence_when_writer_prep_is_armed(monkeypatch):
    from agent import story_reburn
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url="https://cdn/old__story.jpg", caption="edited caption")
    row["source_media_url"] = "https://cdn/raw.jpg"
    evidence = {"source_exact_url": row["source_media_url"],
                "delivered_exact_url": "https://cdn/healed__story.jpg",
                "source_fingerprint": "md5:" + "a" * 32,
                "delivered_fingerprint": "md5:" + "b" * 32,
                "source_byte_length": 10, "delivered_byte_length": 11,
                "operation": "reburn", "evidence_ref": "story_reburn:test",
                "observed_by": "story_reburn", "rendered_by": "story_reburn"}
    captured = {}

    class Store:
        def patch_image_url(self, gym, row_id, url, **kwargs):
            captured.update(gym=gym, row_id=row_id, url=url, **kwargs)
            return {**row, "image_url": url}

    monkeypatch.setattr(story_reburn, "should_reburn", lambda _: True)
    monkeypatch.setattr(story_reburn, "reburn_with_evidence",
                        lambda *_a, **_k: ("https://cdn/healed__story.jpg",
                                           type("Evidence", (), {"as_dict": lambda _: evidence})()))
    healed = cap._reburn_stale_story(row, SimpleNamespace(display_name="LASSO IG", key="lasso_ig"), Store())
    assert healed["image_url"] == "https://cdn/healed__story.jpg"
    assert captured == {"gym": "lasso", "row_id": "s", "url": "https://cdn/healed__story.jpg",
                        "expected_row": row, "render_evidence": evidence}


@pytest.mark.parametrize(("raw_url", "reburned_url"), [
    ("https://cdn/raw-photo.jpg", "https://cdn/healed__story.jpg"),
    ("https://cdn/raw-video.mp4", "https://cdn/healed__storyvid.mp4"),
])
def test_visual_writer_same_object_story_reburns_before_publish(
        armed, monkeypatch, raw_url, reburned_url):
    """A writer-attested raw Story is source provenance, never caption-burn proof."""
    from agent import story_image, story_reburn
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url=raw_url, caption="current caption")
    row["source_media_url"] = raw_url
    store = _FakeStore([row])
    evidence = {"source_exact_url": raw_url, "delivered_exact_url": reburned_url}
    reburn_calls = []

    def patch_image_url(gym, row_id, url, *, expected_row, render_evidence):
        assert expected_row == row
        assert render_evidence == evidence
        store.rows[row_id]["image_url"] = url
        return dict(store.rows[row_id])

    store.patch_image_url = patch_image_url
    monkeypatch.setattr(story_image, "story_media_carries_caption",
                        lambda url, caption: url == reburned_url)
    monkeypatch.setattr(story_reburn, "reburn_with_evidence", lambda *args: (
        reburn_calls.append(args) or reburned_url,
        SimpleNamespace(as_dict=lambda: evidence),
    ))
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)

    assert summary["published"] == ["s"]
    assert reburn_calls == [(raw_url, "current caption", "LASSO", "lasso_ig")]
    assert [draft.creative_public_url for draft, _ in pub.calls] == [reburned_url]


def test_visual_writer_same_object_story_holds_when_reburn_fails(armed, monkeypatch):
    from agent import story_reburn
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    raw_url = "https://cdn/raw-photo.jpg"
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url=raw_url, caption="current caption")
    row["source_media_url"] = raw_url
    store = _FakeStore([row])
    monkeypatch.setattr(story_reburn, "reburn_with_evidence", lambda *args: None)
    monkeypatch.setattr(cap, "_alert_story_needs_render", lambda *args: None)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)

    assert summary["waiting"] == ["s"]
    assert pub.calls == []
    assert store.publishing_calls == []


def test_visual_writer_same_object_story_holds_when_guard_evaluation_fails(
        armed, monkeypatch):
    from agent import visual_writer_prepare
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    raw_url = "https://cdn/raw-photo.jpg"
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url=raw_url, caption="current caption")
    row["source_media_url"] = raw_url
    store = _FakeStore([row])

    def broken_guard_evaluator():
        raise RuntimeError("writer guard unavailable")

    monkeypatch.setattr(visual_writer_prepare, "enabled", broken_guard_evaluator)
    monkeypatch.setattr(cap, "_alert_story_needs_render", lambda *args: None)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)

    assert summary["waiting"] == ["s"]
    assert pub.calls == []
    assert store.publishing_calls == []


def test_same_object_story_preserves_behavior_when_writer_guard_off(armed, monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    raw_url = "https://cdn/raw-photo.jpg"
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url=raw_url, caption="current caption")
    row["source_media_url"] = raw_url
    store = _FakeStore([row])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)

    assert summary["published"] == ["s"]
    assert [draft.creative_public_url for draft, _ in pub.calls] == [raw_url]


@pytest.mark.parametrize("changed_field", [None, "status", "caption", "image_url",
                                                  "source_media_url", "published_at", "slot_index"])
@pytest.mark.parametrize("slot_index", [0, 1, None])
@pytest.mark.parametrize("visual_guard", [False, True])
def test_approved_story_reburn_real_store_conditional_patch(monkeypatch, changed_field, slot_index, visual_guard):
    """Exercise the real store method against a fake that applies PostgREST filters."""
    from agent import story_reburn
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1" if visual_guard else "0")
    old = "https://cdn/old__story.jpg"
    new = "https://cdn/new__story.jpg"
    saved = _row("s", fmt="story", status="approved", image_url=old,
                 caption='edited, "quoted" caption')
    saved["source_media_url"] = "https://cdn/raw.jpg"
    saved["slot_index"] = slot_index
    observed = dict(saved)

    class ConditionalHTTP:
        def __init__(self):
            self.row = dict(saved)
            self.calls = []

        def patch(self, url, *, params, headers, json, timeout):
            self.calls.append((params, json))
            # requests URL-encodes each top-level predicate. PostgREST retains
            # quote characters in eq values rather than stripping them.
            import requests
            from urllib.parse import parse_qsl, urlsplit
            wire = requests.Request("PATCH", url, params=params).prepare().url
            decoded = dict(parse_qsl(urlsplit(wire).query))
            assert decoded == params
            for key, condition in decoded.items():
                if condition == "is.null":
                    matches = self.row.get(key) is None
                else:
                    matches = condition == f"eq.{self.row.get(key)}"
                if not matches:
                    return _Resp(200, [])
            self.row.update(json)
            return _Resp(200, [dict(self.row)])

    http = ConditionalHTTP()
    changes = {"status": "publishing", "caption": "newer caption",
               "image_url": "https://cdn/other.jpg",
               "source_media_url": "https://cdn/other-source.jpg",
               "published_at": "2026-08-10T20:00:00Z",
               "slot_index": 1 if slot_index == 0 else 0}
    if changed_field:
        http.row[changed_field] = changes[changed_field]
    store = _store(http)
    monkeypatch.setattr(story_reburn, "should_reburn", lambda row: True)
    monkeypatch.setattr(story_reburn, "reburn", lambda *args: new)
    monkeypatch.setattr(story_reburn, "reburn_with_evidence", lambda *args: (
        new, SimpleNamespace(as_dict=lambda: {})))
    monkeypatch.setattr(store, "_prepare_visual_media",
                        lambda account_key, row_id, payload, **kwargs: payload)

    healed = cap._reburn_stale_story(
        observed, SimpleNamespace(display_name="LASSO IG", key="lasso_ig"), store)

    params, body = http.calls[0]
    assert params["status"] == "eq.approved"
    assert params["caption"] == 'eq.edited, "quoted" caption'
    assert params["image_url"] == f"eq.{old}"
    assert params["source_media_url"] == "eq.https://cdn/raw.jpg"
    assert params["slot_index"] == ("is.null" if slot_index is None else f"eq.{slot_index}")
    assert params["published_at"] == params["late_post_id"] == "is.null"
    assert params["media_not_ready_reason"] == "is.null"
    assert body == {"image_url": new, "media_not_ready_reason": None}
    if changed_field:
        assert healed is None
        assert http.row["image_url"] != new
    else:
        assert healed["image_url"] == http.row["image_url"] == new
        assert http.row["status"] == "approved"


@pytest.mark.parametrize("proved", [False, True])
def test_reburned_manual_story_still_requires_fresh_approval(armed, monkeypatch, proved):
    from agent import story_image, story_reburn
    from test_approval_provenance import _ProofStore, human_prove, canonical_digest

    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    old = "https://cdn/old__story.jpg"
    new = "https://cdn/healed__story.jpg"
    row = _row("manual-reburn", fmt="story", status="approved", image_url=old,
               caption="Build strength today.")
    row["source_media_url"] = "https://cdn/raw.jpg"
    if proved:
        row = human_prove(row)
    store = _ProofStore([row], autonomy={"lasso": False})

    def patch_image_url(gym, rid, url, *, expected_row):
        assert expected_row["image_url"] == old
        store.rows[rid]["image_url"] = url
        return dict(store.rows[rid])

    store.patch_image_url = patch_image_url
    monkeypatch.setattr(story_image, "story_media_carries_caption", lambda url, caption: url == new)
    monkeypatch.setattr(story_reburn, "should_reburn", lambda row: True)
    monkeypatch.setattr(story_reburn, "reburn", lambda *args: new)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store, publisher=pub,
                              now=LATE_NOW, approved_only=True, catch_all=True)

    assert store.rows["manual-reburn"]["image_url"] == new
    assert store.rows["manual-reburn"].get("approval_digest") != canonical_digest(store.rows["manual-reburn"])
    assert store.claim_calls == [("manual-reburn", True)]
    assert summary["published"] == []
    assert pub.calls == []
    assert store.rows["manual-reburn"]["status"] == "approved"


@pytest.mark.parametrize("patch_result", ["missing", None, {"image_url": "https://cdn/old__story.jpg"}])
def test_stale_approved_story_holds_when_reburn_url_is_not_persisted(
        armed, monkeypatch, patch_result):
    from agent import story_image, story_reburn
    old = "https://cdn/old__story.jpg"
    new = "https://cdn/healed__story.jpg"
    row = _row("s", account="instagram", fmt="story", status="approved",
               image_url=old, caption="edited caption")
    row["source_media_url"] = "https://cdn/raw.jpg"
    store = _FakeStore([row])
    if patch_result != "missing":
        store.patch_image_url = lambda gym, rid, url, *, expected_row: patch_result
    monkeypatch.setattr(story_image, "story_media_carries_caption", lambda url, cap: url == new)
    monkeypatch.setattr(story_reburn, "should_reburn", lambda r: True)
    monkeypatch.setattr(story_reburn, "reburn", lambda *a, **k: new)
    monkeypatch.setattr(cap, "_alert_story_needs_render", lambda *a: None)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)

    assert summary["waiting"] == ["s"]
    assert pub.calls == []
    assert store.publishing_calls == []
    assert store.rows["s"]["status"] == "approved"
    assert store.rows["s"]["image_url"] == old


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
    def patch_image_url(g, rid, url, *, expected_row):
        assert expected_row == row
        store.rows[rid]["image_url"] = url
        return dict(store.rows[rid])
    store.patch_image_url = patch_image_url
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


@pytest.mark.parametrize("patch_result", ["missing", None, "stale", "different_status"])
def test_feed_preflight_holds_before_publish_claim_without_verified_patch(
        armed, monkeypatch, patch_result):
    from agent import feed_image, media_host
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(media_host, "download_bytes", lambda *a, **k: _jpeg(600, 1080))
    monkeypatch.setattr(feed_image, "make_feed_safe_from_bytes", lambda b, out: out)
    monkeypatch.setattr(media_host, "host_media", lambda *a: "https://cdn/new__feed.jpg")
    monkeypatch.setattr(cap, "_alert_feed_needs_reframe", lambda *a: None)
    row = _row("f", status="approved", image_url="https://cdn/old.jpg")
    store = _FakeStore([row])
    if patch_result != "missing":
        returned = None if patch_result is None else dict(row)
        if patch_result == "different_status":
            returned.update(status="publishing", image_url="https://cdn/new__feed.jpg")
        store.patch_image_url = lambda *a, **k: returned
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              approved_only=True, catch_all=True)
    assert summary["waiting"] == ["f"]
    assert pub.calls == store.publishing_calls == store.published_calls == []
    assert store.rows["f"] == row


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


def test_client_approved_rows_respect_platform_day_capacity(armed, monkeypatch):
    """Separate approved FB rows cannot all publish on a 1x gym day."""
    from agent import cadence

    class _Acct:
        key = "gymx_fb"; platform = "facebook"; display_name = "Gym X"

    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda gym, store: 1)
    rows = [_row(rid, account="facebook", status="approved")
            for rid in ("fb1", "fb2", "fb3")]
    for row in rows:
        row["gym_id"] = "gymx"
    store = _AtomicSlotStore(rows)
    sent = []
    summary = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=LATE_NOW,
                              approved_only=True, catch_all=True,
                              zernio_publish=_zern_capture(sent))
    assert summary["published"] == ["fb1"]
    assert set(summary["waiting"]) == {"fb2", "fb3"}
    assert store.publishing_calls == ["fb1"]

    # A real 2x preference still permits its second distinct feed.
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda gym, store: 2)
    again = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=LATE_NOW,
                            approved_only=True, catch_all=True,
                            zernio_publish=_zern_capture(sent))
    assert again["published"] == ["fb2"]
    assert again["waiting"] == ["fb3"]


def test_client_slot_capacity_keeps_cross_platform_pair_and_approval_gate(
        armed, monkeypatch):
    from agent import cadence

    class _Acct:
        def __init__(self, platform):
            self.key = f"gymx_{'fb' if platform == 'facebook' else 'ig'}"
            self.platform = platform
            self.display_name = "Gym X"

    monkeypatch.setattr(cap, "_account_for",
                        lambda row, gym_id: _Acct(row["account"]))
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda gym, store: 1)
    rows = [_row("ig", status="approved"),
            _row("fb", account="facebook", status="approved"),
            _row("unapproved", account="facebook", status="pending")]
    for row in rows:
        row["gym_id"] = "gymx"
    store = _AtomicSlotStore(rows)
    sent = []
    summary = cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=LATE_NOW,
                              approved_only=True, catch_all=True,
                              zernio_publish=_zern_capture(sent))
    assert set(summary["published"]) == {"ig", "fb"}
    assert summary["waiting"] == ["unapproved"]
    assert "unapproved" not in store.publishing_calls


def test_autonomous_overlap_and_catchup_share_actual_day_capacity(armed, monkeypatch):
    """Two workers must not send distinct rows from yesterday and today together."""
    from agent import cadence

    class _Acct:
        key = "gymx_fb"; platform = "facebook"; display_name = "Gym X"

    barrier = threading.Barrier(2)

    class _ConcurrentStore(_AtomicSlotStore):
        def due_rows(self, gym, run_date, catchup_days=0):
            return [dict(r) for r in self.rows.values()
                    if r.get("status") == "pending"]

        def claim_publish_slot(self, *args):
            barrier.wait(timeout=5)  # both workers enter before either can reserve
            return super().claim_publish_slot(*args)

    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda gym, store: 1)
    rows = [_row("yesterday", account="facebook", status="pending",
                 post_date="2026-08-09"),
            _row("today", account="facebook", status="pending")]
    for row in rows:
        row["gym_id"] = "gymx"
    store = _ConcurrentStore(rows)
    sent = []
    results = []

    def worker():
        results.append(cap.publish_due(
            RUN_DATE, gym_id="gymx", store=store, now=LATE_NOW,
            approved_only=False, catchup_days=1, catch_all=True,
            zernio_publish=_zern_capture(sent)))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(sent) == 1
    assert sum(len(result["published"]) for result in results) == 1
    assert {r.get("publish_reservation_day") for r in store.rows.values()
            if r.get("status") == "published"} == {RUN_DATE}


def test_slot_reservation_refreshes_local_day_after_preflight(armed, monkeypatch):
    """A render crossing midnight reserves the day of the network send."""
    from agent import cadence

    class _Acct:
        key = "gymx_ig"; platform = "instagram"; display_name = "Gym X"

    class _RecordingStore(_AtomicSlotStore):
        def claim_publish_slot(self, row_id, gym, day, timezone_name,
                               capacity, approved_only):
            self.claim_day = day
            return super().claim_publish_slot(row_id, gym, day, timezone_name,
                                              capacity, approved_only)

    original_local_now = cap._local_now
    calls = 0

    def crossing_midnight(now, timezone_name):
        nonlocal calls
        calls += 1
        instant = "2026-08-10T23:59:00-04:00" if calls == 1 else \
                  "2026-08-11T00:01:00-04:00"
        return original_local_now(instant, timezone_name)

    monkeypatch.setattr(cap, "_local_now", crossing_midnight)
    monkeypatch.setattr(cap, "_account_for", lambda row, gym_id: _Acct())
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda gym, store: 1)
    row = _row("cross-midnight", status="approved")
    row["gym_id"] = "gymx"
    store = _RecordingStore([row])
    cap.publish_due(RUN_DATE, gym_id="gymx", store=store, now=LATE_NOW,
                    approved_only=True, catch_all=True,
                    zernio_publish=_zern_capture([]))
    assert store.claim_day == "2026-08-11"


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
    # The avatar rail is OFF by default since Blake's 2026-09-01 ruling (CrossFit,
    # hyrox and competitive athletics are allowed). This test describes the rail's
    # behavior WHEN ARMED, so it arms it explicitly.
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")
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


# ---- audit 2026-08-30: four silent-failure defects in publish_due ---------------
# The portal said nothing was wrong and the post never went out, and no human was
# told. All four fixes route into either the SAME _note_repeat_failure counter used
# by the existing publish-exception path, or a direct ops_alerts.alert call, or a
# kv-deduped per-gym-per-day stamp — never a NEW unbounded alert path.

def test_ambiguous_failed_result_holds_and_alerts_without_retry(armed, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    store = _FakeStore([_row("soft")])
    pub = _FakePublisher(PublishResult(ok=False, mode="failed", detail="ig 400"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "soft" in summary["failed"]
    assert summary["recovery_required"] == ["soft"]
    assert store.rows["soft"]["status"] == "publishing"
    assert len(sent) == 1
    assert "AMBIGUOUS" in sent[0] and "ok=False" in sent[0]
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert len(pub.calls) == 1


def test_explicit_provider_rejection_proving_no_post_reverts_for_retry(
        armed, monkeypatch):
    from types import SimpleNamespace

    result = SimpleNamespace(ok=False, mode="rejected", media_id="",
                             detail="validation rejected before create",
                             definitive_no_post=True)
    store = _FakeStore([_row("definite")])
    pub = _FakePublisher(result)
    monkeypatch.setattr(cap, "_alert_ambiguous_publish", lambda *a, **kw: None)

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["failed"] == ["definite"]
    assert summary["recovery_required"] == []
    assert store.rows["definite"]["status"] == "pending"
    assert store.failed_calls == ["definite"]


def test_failed_result_without_explicit_no_post_contract_never_reclaims(
        armed, monkeypatch):
    result = PublishResult(ok=False, mode="rejected", detail="provider said no")
    store = _FakeStore([_row("uncertain")])
    pub = _FakePublisher(result)
    monkeypatch.setattr(cap, "_alert_ambiguous_publish", lambda *a, **kw: None)

    first = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    second = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert first["recovery_required"] == ["uncertain"]
    assert second["published"] == []
    assert store.rows["uncertain"]["status"] == "publishing"
    assert len(pub.calls) == 1


def test_soft_failure_alert_does_not_storm_past_threshold(armed, monkeypatch):
    """DEFECT 1 anti-storm: once alerted, the SAME reason on the SAME row must not
    re-fire on every further ~1-min tick (matches _note_repeat_failure's existing
    per-(row,reason)-per-day dedupe)."""
    sent = _capture_alerts(monkeypatch)
    store = _FakeStore([_row("soft2")])
    pub = _FakePublisher(PublishResult(ok=False, mode="failed"))

    for _ in range(cap.REPEAT_FAILURE_ALERT_AT + 6):    # well past the threshold
        cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert len(sent) == 1


def test_claim_exception_now_feeds_the_repeat_failure_counter(armed, monkeypatch):
    """DEFECT 4: an exception from store.mark_publishing (the atomic claim) used to
    be print-only with no _note_repeat_failure call, unlike the publish-exception
    path. A row whose claim keeps throwing (flaky store connection) must now count
    and eventually alert the same way."""
    sent = _capture_alerts(monkeypatch)

    class _ClaimBoomStore(_FakeStore):
        def mark_publishing(self, row_id):
            self.publishing_calls.append(row_id)
            raise RuntimeError("supabase connection reset")

    store = _ClaimBoomStore([_row("claimboom")])
    pub = _FakePublisher()

    for _ in range(cap.REPEAT_FAILURE_ALERT_AT - 1):
        cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert sent == []

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "claimboom" in summary["failed"]
    assert len(sent) == 1
    assert "supabase connection reset" in sent[0]
    assert pub.calls == []                          # never reached the network


def test_mark_published_write_failure_alerts_directly(armed, monkeypatch):
    """DEFECT 2: the post REALLY published, but the mark_published write itself
    failed. The comment already said 'report it loudly instead' but only print()d.
    Must now alert directly via ops_alerts (not wait on the 2h stale sweep), and
    must NOT revert the claim (that would republish a post already live)."""
    sent = _capture_alerts(monkeypatch)

    class _MarkPublishedBoomStore(_FakeStore):
        def mark_published(self, row_id, media_id, published_at):
            raise RuntimeError("supabase write timeout")

    store = _MarkPublishedBoomStore([_row("livebutlost")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert "livebutlost" in summary["failed"]
    assert summary["published"] == []
    # NOT reverted: mark_publishing already flipped it to 'publishing' and the
    # failed mark_published write must leave it there for the 2h backstop sweep.
    assert store.rows["livebutlost"]["status"] == "publishing"
    assert len(sent) == 1
    assert "PUBLISHED live" in sent[0]
    assert "supabase write timeout" in sent[0]


def test_published_state_conflict_seeds_exact_row_first_fixer_event():
    """Only the explicit guarded 409 after a real provider success is actionable.

    The seed carries the exact calendar row and tenant-bound expected terminal state,
    never an error string or an inferred target from a support message.
    """
    row = _row("calendar_conflict_123", status="publishing")
    row["gym_id"] = "chateau"
    sent = []

    def event_builder(**kwargs):
        sent.append(("event", kwargs))
        return {"event": kwargs}

    def sender(event):
        sent.append(("send", event))
        return SimpleNamespace(ok=True)

    result = cap._submit_published_state_conflict(
        gym_id="chateau", row=row,
        error=pcs.PortalStoreError(409, "refusing to stamp published over conflict"),
        resolve_client_id=lambda gym: (
            "22222222-2222-4222-8222-222222222222" if gym == "chateau" else None),
        event_builder=event_builder, sender=sender,
    )

    assert result is True
    event = sent[0][1]
    assert event == {
        "submission_key": str(uuid5(
            NAMESPACE_URL,
            "lasso:fixer:calendar-published-state-conflict:v1:chateau:"
            "calendar_conflict_123",
        )),
        "client_id": "22222222-2222-4222-8222-222222222222",
        "row_id": "calendar_conflict_123",
        "expected_status": "published",
    }
    assert sent[1] == ("send", {"event": event})


@pytest.mark.parametrize("error, gym_id", [
    (RuntimeError("supabase write timeout"), "chateau"),
    (pcs.PortalStoreError(503, "temporarily unavailable"), "chateau"),
    (pcs.PortalStoreError(409, "state conflict"), "other-gym"),
])
def test_published_state_conflict_never_infers_ticket_from_transient_or_wrong_tenant(
        error, gym_id):
    called = []
    row = _row("calendar_conflict_456", status="publishing")
    row["gym_id"] = "chateau"

    assert cap._submit_published_state_conflict(
        gym_id=gym_id, row=row, error=error,
        resolve_client_id=lambda _: called.append("resolve"),
        event_builder=lambda **_: called.append("event"),
        sender=lambda _: called.append("send"),
    ) is False
    assert called == []


@pytest.mark.parametrize("ticket_accepted", [True, False])
def test_mark_published_state_conflict_submits_without_changing_claim(
        armed, monkeypatch, ticket_accepted):
    class _ConflictStore(_FakeStore):
        def mark_published(self, row_id, media_id, published_at):
            raise pcs.PortalStoreError(
                409, "row changed out of publishing; refusing terminal write")

    seen = []
    monkeypatch.setattr(
        cap, "_submit_published_state_conflict",
        lambda **kwargs: seen.append(kwargs) or ticket_accepted,
    )
    store = _ConflictStore([_row("calendar-conflict-live")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["failed"] == ["calendar-conflict-live"]
    assert summary["published"] == []
    assert store.rows["calendar-conflict-live"]["status"] == "publishing"
    assert len(seen) == 1
    assert seen[0]["gym_id"] == "lasso"
    assert seen[0]["row"]["id"] == "calendar-conflict-live"
    assert isinstance(seen[0]["error"], pcs.PortalStoreError)
    assert seen[0]["error"].status == 409


def test_mark_published_write_failure_alert_does_not_storm_across_ticks(armed, monkeypatch):
    """Anti-storm for DEFECT 2: the exactly-once claim means this row can never
    win mark_publishing again, so repeated ticks over the same stuck row must fire
    the direct alert only ONCE, not once per tick."""
    sent = _capture_alerts(monkeypatch)

    class _MarkPublishedBoomStore(_FakeStore):
        def mark_published(self, row_id, media_id, published_at):
            raise RuntimeError("timeout")

    store = _MarkPublishedBoomStore([_row("livebutlost2")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    for _ in range(5):
        cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert len(sent) == 1


def test_daily_cap_hit_alerts_once_per_gym_per_day(armed, monkeypatch):
    """DEFECT 3: the first row THIS TICK to be throttled by the daily cap must fire
    ONE ops alert for the gym; further rows throttled the SAME tick (same gym, same
    day) must not re-fire (kv-deduped, same stamp shape as onboarding_watch.stamp)."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 0)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    sent = _capture_alerts(monkeypatch)
    store = _FakeStore([_row(x) for x in ("a", "b", "c")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                              catch_all=True, daily_cap=1)

    assert len(summary["published"]) == 1
    assert len(summary["waiting"]) == 2             # 2 rows throttled this tick
    assert len(sent) == 1                           # but only ONE alert
    assert "lasso" in sent[0] and "daily publish cap" in sent[0] and RUN_DATE in sent[0]


def test_daily_cap_hit_alert_does_not_storm_across_ticks(armed, monkeypatch):
    """Anti-storm for DEFECT 3: a gym sitting at/over its cap for the WHOLE day
    (the ~1-min tick keeps re-checking) must alert only once per day, not once
    per tick, across many repeated ticks."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 5)   # already at cap
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    sent = _capture_alerts(monkeypatch)
    store = _FakeStore([_row("z")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    for _ in range(6):
        cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW,
                        catch_all=True, daily_cap=1)
    assert len(sent) == 1


def test_daily_cap_hit_alert_rearms_on_a_new_day(armed, monkeypatch):
    """The dedupe key includes run_date, so a NEW day re-arms the alert instead of
    permanently silencing a gym that is capped again tomorrow."""
    monkeypatch.setattr(cap, "_pub_count_today", lambda g, d: 5)
    monkeypatch.setattr(cap, "_bump_pub_count", lambda g, d: None)
    sent = _capture_alerts(monkeypatch)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    store1 = _FakeStore([_row("day1")])
    cap.publish_due(RUN_DATE, store=store1, publisher=pub, now=LATE_NOW,
                    catch_all=True, daily_cap=1)
    day2 = "2026-08-11"
    store2 = _FakeStore([_row("day2", post_date=day2)])
    cap.publish_due(day2, store=store2, publisher=pub,
                    now="2026-08-11T23:59:00-04:00", catch_all=True, daily_cap=1)
    assert len(sent) == 2


# ---- expired-row SELF-HEAL (Blake 2026-08-31: no human re-dating) -----------------

class _RedateStore(_ExpiredStore):
    """Expired store + the re-date surface: list_month (occupied days), patch_post_date,
    set_status. Tracks every write."""

    def __init__(self, rows, occupied=()):
        super().__init__(rows)
        self._occupied = list(occupied)      # rows already on the future book
        self.redates = []                    # (row_id, new_date)
        self.status_sets = []                # (row_id, status)

    def list_month(self, gym, month):
        return [dict(r) for r in self._occupied
                if str(r.get("post_date", "")).startswith(month)]

    def patch_post_date(self, row_id, new_date):
        self.redates.append((row_id, new_date))
        return {"id": row_id, "post_date": new_date}

    def set_status(self, gym, row_id, status):
        self.status_sets.append((row_id, status))
        return {"id": row_id, "status": status}


def test_expired_rows_self_heal_redate(monkeypatch):
    """Armed with proof off: expired rows are re-dated and legacy approvals persist,
    the digest says 'No action needed', and NO ask-a-human alert fires."""
    monkeypatch.setenv("AGENT_EXPIRED_AUTO_REDATE", "true")
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    rows = [
        {"id": "a", "gym_id": "lasso", "account": "instagram", "format": "feed",
         "post_date": "2026-08-07", "status": "approved"},
        {"id": "b", "gym_id": "lasso", "account": "instagram", "format": "feed",
         "post_date": "2026-08-08", "status": "approved"},
    ]
    # 08-31 is occupied for (instagram, feed) -> first open day is 09-01
    occupied = [{"account": "instagram", "format": "feed",
                 "post_date": "2026-08-31", "status": "pending"}]
    store, kv, seen = _RedateStore(rows, occupied), _MemKV(), []
    out = cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                                 now="2026-08-30T12:00:00")
    assert out == ["lasso"]
    assert store.redates == [("a", "2026-09-01"), ("b", "2026-09-02")]
    assert store.status_sets == []                       # nothing retired
    assert len(seen) == 1 and "No action needed" in seen[0]
    assert "re-dated 2" in seen[0]
    assert "(existing approval status preserved)" in seen[0]
    assert "approvals preserved" not in seen[0]
    assert seen[0].endswith("No action needed.")
    assert not any("Re-date them to publish" in m for m in seen)   # no human ask


def test_expired_rows_self_heal_alert_reports_proof_invalidation(monkeypatch):
    """Proof mode re-dates approved rows only after clearing proof and pending them."""
    monkeypatch.setenv("AGENT_EXPIRED_AUTO_REDATE", "true")
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    rows = [{"id": "a", "gym_id": "lasso", "account": "instagram", "format": "feed",
             "post_date": "2026-08-07", "status": "approved"}]
    store, kv, seen = _RedateStore(rows), _MemKV(), []
    cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                           now="2026-08-30T12:00:00")
    assert len(seen) == 1
    assert "(approval proof cleared; any approved row returned to pending)" in seen[0]
    assert "approvals preserved" not in seen[0]
    assert seen[0].endswith("Check current gym mode and approval state before release.")
    assert "No action needed" not in seen[0]


def test_expired_rows_pending_snapshot_alert_covers_approval_race(monkeypatch):
    """A pending sweep snapshot can be approved before patch_post_date re-reads
    it; the alert must not promise pending was preserved or no action is needed."""
    monkeypatch.setenv("AGENT_EXPIRED_AUTO_REDATE", "true")
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    rows = [{"id": "p", "gym_id": "lasso", "account": "instagram", "format": "feed",
             "post_date": "2026-08-07", "status": "pending"}]
    store, kv, seen = _RedateStore(rows), _MemKV(), []
    cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                           now="2026-08-30T12:00:00")
    assert len(seen) == 1
    assert "(approval proof cleared; any approved row returned to pending)" in seen[0]
    assert seen[0].endswith("Check current gym mode and approval state before release.")
    assert "No action needed" not in seen[0]


def test_expired_unapproved_twice_is_retired(monkeypatch):
    """A PENDING row that already got its one re-date and expired AGAIN is retired
    (killed); an APPROVED second expiry gets another chance forward (never dropped)."""
    monkeypatch.setenv("AGENT_EXPIRED_AUTO_REDATE", "true")
    rows = [
        {"id": "p1", "gym_id": "gritx", "account": "instagram", "format": "feed",
         "post_date": "2026-08-10", "status": "pending"},
        {"id": "ap1", "gym_id": "gritx", "account": "facebook", "format": "feed",
         "post_date": "2026-08-10", "status": "approved"},
    ]
    store, kv, seen = _RedateStore(rows), _MemKV(), []
    kv.set("redated_p1", "1")                            # both expired once before
    kv.set("redated_ap1", "1")
    cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                           now="2026-08-30T12:00:00")
    assert store.status_sets == [("p1", "killed")]       # unapproved: retired
    assert [rid for rid, _ in store.redates] == ["ap1"]  # approved: moved again
    assert any("retired 1" in m for m in seen)


def test_expired_flag_off_keeps_alert_only(monkeypatch):
    monkeypatch.delenv("AGENT_EXPIRED_AUTO_REDATE", raising=False)
    rows = [{"id": "x", "gym_id": "lasso", "account": "instagram", "format": "feed",
             "post_date": "2026-08-07", "status": "approved"}]
    store, kv, seen = _RedateStore(rows), _MemKV(), []
    cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                           now="2026-08-30T12:00:00")
    assert store.redates == [] and store.status_sets == []
    assert len(seen) == 1 and "Re-date them to publish" in seen[0]


def test_expired_row_with_full_book_is_retired_not_stranded(monkeypatch):
    """BOOK FULL: when every horizon day already has content for the row's
    (account, format), the expired row is redundant — retired with an info alert,
    never bounced to a human digest forever (LASSO 2026-08-31: 4 approved rows
    could not fit a 31-day-full book and fell through silently)."""
    monkeypatch.setenv("AGENT_EXPIRED_AUTO_REDATE", "true")
    rows = [{"id": "full1", "gym_id": "lasso", "account": "instagram",
             "format": "feed", "post_date": "2026-08-09", "status": "approved"}]
    from datetime import date, timedelta
    occupied = [{"account": "instagram", "format": "feed",
                 "post_date": (date(2026, 8, 30) + timedelta(days=i)).isoformat(),
                 "status": "pending"} for i in range(1, 33)]
    store, kv, seen = _RedateStore(rows, occupied), _MemKV(), []
    cap.sweep_expired_rows(store=store, kv=kv, alert=seen.append,
                           now="2026-08-30T12:00:00")
    assert store.redates == []
    assert store.status_sets == [("full1", "killed")]
    assert len(seen) == 1 and "No action needed" in seen[0] and "retired 1" in seen[0]


def test_unreadable_content_ledger_neither_publishes_nor_deletes(armed, monkeypatch):
    class UnreadableLedger:
        def get(self, key, default=""):
            raise RuntimeError("ledger unavailable")

    store = _FakeStore([_row("ledger-read-failure")])
    cleanup_calls = []
    monkeypatch.setattr(cap, "_kv_default", lambda: UnreadableLedger())
    monkeypatch.setattr(cap, "_mark_duplicate_content",
                        lambda *args: cleanup_calls.append(args))
    publisher = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                              notifier=_FakeNotifier(), now=LATE_NOW)
    assert summary["published"] == []
    assert summary["skipped"] == ["ledger-read-failure"]
    assert publisher.calls == []
    assert cleanup_calls == []
    assert store.rows["ledger-read-failure"]["status"] == "pending"


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_duplicate_refusal_reports_real_cleanup_without_publishing(armed, monkeypatch, cleanup_ok):
    from agent import ops_alerts
    class ClaimedLedger:
        def get(self, key, default=""):
            return "different-row|2026-09-10T12:00:00Z"
    store = _FakeStore([_row("duplicate-row")])
    if not cleanup_ok:
        monkeypatch.setattr(store, "mark_duplicate_content", lambda *args: None)
    monkeypatch.setattr(cap, "_kv_default", lambda: ClaimedLedger())
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)
    publisher = _FakePublisher()
    result = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                             notifier=_FakeNotifier(), now=LATE_NOW)
    assert publisher.calls == []
    assert result["skipped"] == ["duplicate-row"]
    assert store.rows["duplicate-row"]["status"] == ("deleted" if cleanup_ok else "publishing")
    assert any(("soft-deleted" if cleanup_ok else "Cleanup was not confirmed") in s for s in alerts)


@pytest.mark.parametrize("rollback_result", [False, None])
def test_unconfirmed_pre_network_rollback_is_held_without_send_or_reclaim(
        armed, monkeypatch, rollback_result):
    """A zero-row rollback must be visible as recovery work, never a successful revert."""
    class ZeroRowRollbackStore(_FakeStore):
        def mark_publish_failed(self, row_id, revert_status="pending", reject_reason=""):
            self.failed_calls.append(row_id)
            return rollback_result

    store = ZeroRowRollbackStore([_row("stranded")])
    alerts = []
    monkeypatch.setattr(cap, "_drive_asset_usable_at_send", lambda *_: False)
    monkeypatch.setattr(cap, "_alert_publish_blocked",
                        lambda *args, **kwargs: alerts.append((args, kwargs)))
    publisher = _FakePublisher()

    result = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                             now=LATE_NOW, catch_all=True)

    assert result["failed"] == ["stranded"]
    assert result["held"] is True
    assert result["recovery_required"] == ["stranded"]
    assert store.rows["stranded"]["status"] == "publishing"
    assert publisher.calls == []
    assert alerts[0][1]["reverted"] is False

    # An unconfirmed rollback leaves its claim in place, so another tick cannot post it.
    retry = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                            now=LATE_NOW, catch_all=True)
    assert retry["published"] == []
    assert publisher.calls == []


def test_pre_network_rollback_supplies_tenant_and_holds_on_cas_miss(
        armed, monkeypatch):
    token = "11111111-1111-4111-8111-111111111111"

    class ConditionalRollbackStore(_FakeStore):
        def claim_publish_slot(self, row_id, gym_id, day, timezone_name,
                               capacity, approved_only):
            return token if self.mark_publishing(row_id) else None

        def mark_publish_failed(self, row_id, revert_status="pending",
                                reject_reason=None, gym_id=None,
                                expected_claim_token=None):
            self.rollback_args = (row_id, revert_status, reject_reason,
                                  gym_id, expected_claim_token)
            return None  # concurrent status change, zero rows updated

    store = ConditionalRollbackStore([_row("raced")])
    monkeypatch.setattr(cap, "_drive_asset_usable_at_send", lambda *_: False)
    monkeypatch.setattr(cap, "_alert_publish_blocked", lambda *a, **kw: None)
    monkeypatch.setattr(cap, "_published_content_key", lambda *a: "")
    publisher = _FakePublisher()

    result = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                             now=LATE_NOW, catch_all=True)

    assert store.rollback_args == ("raced", "pending",
                                   "media_asset_review_required", "lasso", token)
    assert result["held"] is True
    assert result["recovery_required"] == ["raced"]
    assert publisher.calls == []


@pytest.mark.parametrize("previous", ["pending", "approved"])
def test_content_stamp_failure_releases_for_retry_and_preserves_approval(armed, monkeypatch, previous):
    class FailedStampLedger:
        def get(self, key, default=""):
            return ""
        def set(self, key, value):
            raise RuntimeError("write unavailable")
    store = _FakeStore([_row("stamp-failure", status=previous)])
    monkeypatch.setattr(cap, "_kv_default", lambda: FailedStampLedger())
    publisher = _FakePublisher()
    result = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                             notifier=_FakeNotifier(), now=LATE_NOW)
    assert publisher.calls == []
    assert result["skipped"] == ["stamp-failure"]
    assert store.rows["stamp-failure"]["status"] == previous
    assert store.rows["stamp-failure"]["reject_reason"] == "content_stamp_failed"


@pytest.mark.parametrize("ledger_mode", ["read", "write", "duplicate"])
def test_owned_claim_token_reaches_each_pre_network_ledger_transition(
        armed, monkeypatch, ledger_mode):
    """End-to-end publisher wiring must carry the exact claim UUID to cleanup."""
    token = "33333333-3333-4333-8333-333333333333"

    class OwnedTransitionStore(_FakeStore):
        def claim_publish_slot(self, row_id, gym_id, day, timezone_name,
                               capacity, approved_only):
            row = self.rows[row_id]
            row.update(status="publishing", publish_claim_token=token)
            return token

        def release_content_ledger_claim(self, gym_id, row_id, previous, reason,
                                         expected_claim_token=None):
            self.transition = ("release", gym_id, row_id, previous, reason,
                               expected_claim_token)
            if expected_claim_token != self.rows[row_id].get("publish_claim_token"):
                return None
            row = self.rows[row_id]
            row.update(status=previous, publish_claim_token=None,
                       reject_reason=reason)
            return dict(row)

        def mark_duplicate_content(self, gym_id, row_id, reason,
                                   expected_claim_token=None):
            self.transition = ("duplicate", gym_id, row_id, reason,
                               expected_claim_token)
            if expected_claim_token != self.rows[row_id].get("publish_claim_token"):
                return None
            row = self.rows[row_id]
            row.update(status="deleted", publish_claim_token=None,
                       reject_reason=reason)
            return dict(row)

    class Ledger:
        def get(self, key, default=""):
            if ledger_mode == "read":
                raise RuntimeError("read unavailable")
            if ledger_mode == "duplicate":
                return "another-row|2026-09-18T12:00:00Z"
            return ""

        def set(self, key, value):
            if ledger_mode == "write":
                raise RuntimeError("write unavailable")

    store = OwnedTransitionStore([_row(f"owned-{ledger_mode}")])
    monkeypatch.setattr(cap, "_kv_default", lambda: Ledger())
    publisher = _FakePublisher()

    result = cap.publish_due(
        RUN_DATE, store=store, publisher=publisher,
        notifier=_FakeNotifier(), now=LATE_NOW)

    assert publisher.calls == []
    assert result["skipped"] == [f"owned-{ledger_mode}"]
    assert store.transition[-1] == token
    assert store.rows[f"owned-{ledger_mode}"]["publish_claim_token"] is None
    expected_status = "deleted" if ledger_mode == "duplicate" else "pending"
    assert store.rows[f"owned-{ledger_mode}"]["status"] == expected_status


@pytest.mark.parametrize('cooldown,fmt,blocked', [
    (False, 'feed', False), (True, 'story', False), (True, 'feed', True),
])
def test_calendar_grade_obeys_caption_cooldown_switch_and_story_exemption(
        armed, monkeypatch, cooldown, fmt, blocked):
    from agent import caption_ledger, publish_guard
    monkeypatch.setenv('AGENT_CALENDAR_GRADE', 'true')
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', str(cooldown).lower())
    checked = []
    monkeypatch.setattr(caption_ledger, 'is_blocked',
                        lambda *a, **k: checked.append(a) or True)
    monkeypatch.setattr(publish_guard, 'check', lambda _: [])
    store = _FakeStore([_row('cooldown-row', fmt=fmt, status='approved')])
    pub = _FakePublisher()
    result = cap.publish_due(RUN_DATE, store=store, publisher=pub,
                             notifier=_FakeNotifier(), now=LATE_NOW,
                             approved_only=True)
    assert bool(checked) is blocked
    assert len(pub.calls) == (0 if blocked else 1)
    assert store.rows['cooldown-row']['status'] == ('pending' if blocked else 'published')


# ---- legacy semicolon auto-heal at the publish boundary (2026-10-05) --------
# Rows approved before the copy_gate semicolon rail shipped keep their
# semicolons (the correction lanes skip approved/held/Story/published rows),
# so the publish_guard copy_violation rail would silently stop every one of
# them at schedule time. The publish boundary now auto-formats a due FEED row
# (semicolons -> commas, status preserved) and publishes clean; a semicolon
# glued inside a URL (format_caption refuses to corrupt the link) HOLDS the
# row instead. Story rows pass through untouched (burned-media semantics).

class _PreservingStore(_FakeStore):
    """_FakeStore plus the status-preserving caption patch the heal uses."""

    def __init__(self, rows):
        super().__init__(rows)
        self.preserve_patches = []      # (gym_id, row_id, caption)

    def patch_caption_preserve_status(self, gym_id, row_id, new_caption):
        self.preserve_patches.append((gym_id, row_id, new_caption))
        r = self.rows.get(row_id)
        if r is None:
            return None
        r["caption"] = new_caption      # status DELIBERATELY untouched
        return dict(r)


def test_approved_legacy_semicolon_caption_is_formatted_and_publishes(armed):
    store = _PreservingStore([_row(
        "semi1", status="approved",
        caption="Move well; build strength. Book a class; bring a friend.")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == ["semi1"]
    sent = pub.calls[0][0].caption
    assert ";" not in sent, "a raw semicolon must never reach the wire"
    assert "Move well, build strength." in sent
    # persisted through the STATUS-PRESERVING patch (approval kept), and the row
    # went on to publish this same tick
    assert store.preserve_patches == [("lasso", "semi1", sent)]
    assert store.rows["semi1"]["status"] == "published"


def test_approved_legacy_spacing_is_formatted_before_publish(armed, monkeypatch):
    notices = []
    monkeypatch.setattr(cap, "_note_caption_formatted", lambda *args: notices.append(args))
    store = _PreservingStore([_row(
        "spacing1", status="approved",
        caption="Move well today. Build strength tomorrow. Join us.")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == ["spacing1"]
    sent = pub.calls[0][0].caption
    assert sent == "Move well today.\n\nBuild strength tomorrow.\n\nJoin us."
    # #320: whitespace-only formatting is sent on the wire but NOT persisted, so
    # the human-stamped stored caption (and its approval digest) stays intact.
    assert store.preserve_patches == []
    assert store.rows["spacing1"]["status"] == "published"
    assert notices == []


def test_semicolon_heal_survives_a_store_without_the_patch_method(armed):
    """A legacy/fake store lacking patch_caption_preserve_status still publishes
    the CLEAN caption — the local row is authoritative for the send."""
    class _NoPatchStore(_FakeStore):
        patch_caption_preserve_status = None
    store = _NoPatchStore([_row(
        "semi2", status="approved",
        caption="Move well; build strength. Book a class today with us.")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == ["semi2"]
    assert ";" not in pub.calls[0][0].caption


@pytest.mark.parametrize("caption", [
    "1. Warm up. 2. Cool down for 3. Final stretch now.",
    "1. Hold for 2. Rest.",
    "1. Squats 2. Lunges",
])
def test_ambiguous_numbered_caption_waits_without_send_or_mutation(armed, caption):
    store = _PreservingStore([_row("ambiguous", status="approved", caption=caption)])
    before = dict(store.rows["ambiguous"])
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "ambiguous" in summary["waiting"]
    assert pub.calls == []
    assert store.preserve_patches == []
    assert store.rows["ambiguous"] == before


def test_numbered_lines_with_an_ambiguous_quantity_still_wait(armed):
    store = _PreservingStore([_row("list-lines", status="approved",
        caption="1. Warm up.\n2. Cool down for 3. Final stretch now.")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    # The second line still has an ambiguous inline number, so it must wait.
    assert "list-lines" in summary["waiting"]
    assert pub.calls == []


def test_unambiguous_numbered_lines_publish(armed):
    store = _PreservingStore([_row("list-safe", status="approved",
        caption="1. Warm up.\n2. Cool down.\n3. Stretch.")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert summary["published"] == ["list-safe"]
    assert pub.calls[0][0].caption == "1. Warm up.\n\n2. Cool down.\n\n3. Stretch."


def test_semicolon_glued_inside_url_holds_the_row(armed):
    """format_caption raises rather than corrupt a link; the row is HELD (never
    claimed) for a human edit instead of publishing or crashing the lane."""
    store = _PreservingStore([_row(
        "badurl", status="approved",
        caption="Visit https://example.com/a;b for details. Book a class today.")])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == []
    assert "badurl" in summary["waiting"]
    assert pub.calls == []
    assert store.preserve_patches == []
    assert store.rows["badurl"]["status"] == "approved"   # untouched


def test_story_row_with_semicolon_is_held_until_media_is_corrected(armed, monkeypatch):
    """A Story's old media may contain the semicolon, so it must not publish."""
    from agent import story_image
    story_caption = "Story words; burned onto media."
    store = _PreservingStore([_row(
        "story1", fmt="story", status="approved", caption=story_caption,
        image_url="https://cdn.example.com/story_burned_y.png")])
    store.rows["story1"]["source_media_url"] = "https://cdn.example.com/raw/y.jpg"
    monkeypatch.setattr(story_image, "story_media_carries_caption",
                        lambda url, caption: True)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)

    assert summary["published"] == []
    assert "story1" in summary["waiting"]
    assert pub.calls == []
    assert store.preserve_patches == []
    assert store.rows["story1"]["caption"] == story_caption


# ---- paired Story source-integrity hold after caption cleanup (PR29705) -----
# The paired feed gate proves the Story source over the caption as read, then
# the meta-strip / semicolon auto-heal can legitimately change that caption.
# The prepared Story was bound to the PRE-cleanup caption and the proof is by
# feed ID only, so a changed caption is ALWAYS held (waiting, unclaimed) -- a
# second ID-only proof could pass against stale DB text. An unchanged caption
# pays no second RPC and publishes.

def test_lasso_feed_caption_change_after_proof_holds_without_recheck(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    row = _row("recheck-feed", post_date="2026-10-05",
               caption="Move well; build strength with us today.")
    row["slot_index"] = 0
    store = _PreservingStore([row])
    proofs = []
    # The ID-only proof is rigged ALWAYS true; the hold must not consult it.
    monkeypatch.setattr(store, "lasso_paired_story_ready_for_feed",
                        lambda _: proofs.append(1) or True,
                        raising=False)
    failures = []
    monkeypatch.setattr(cap, "_note_repeat_failure",
                        lambda rid, gym, exc: failures.append((rid, gym, str(exc))))
    pub = _FakePublisher()

    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)

    assert result["published"] == []
    assert result["waiting"] == ["recheck-feed"]
    assert store.publishing_calls == []          # never claimed
    assert pub.calls == []                       # no network call
    assert len(proofs) == 1, "a changed caption holds; only the first proof runs"
    assert failures == [("recheck-feed", "lasso",
                         "paired Story source proof invalid after caption "
                         "cleanup; feed remains held")]
    assert store.rows["recheck-feed"]["status"] == "pending"


def test_lasso_feed_caption_change_holds_even_when_persistence_fails(armed, monkeypatch):
    """The lead's counterexample: the persistence patch FAILS (DB keeps the old
    caption), so an ID-only recheck would return true against text the outgoing
    caption no longer matches. The row must still hold with no claim/network."""
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    row = _row("unpersisted-feed", post_date="2026-10-05",
               caption="Move well; build strength with us today.")
    row["slot_index"] = 0

    class _FailingPatchStore(_FakeStore):
        def patch_caption_preserve_status(self, gym_id, row_id, new_caption):
            raise OSError("store unavailable")   # DB keeps the OLD caption

    store = _FailingPatchStore([row])
    proofs = []
    monkeypatch.setattr(store, "lasso_paired_story_ready_for_feed",
                        lambda _: proofs.append(1) or True,   # always true
                        raising=False)
    failures = []
    monkeypatch.setattr(cap, "_note_repeat_failure",
                        lambda rid, gym, exc: failures.append((rid, gym, str(exc))))
    pub = _FakePublisher()

    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)

    assert result["published"] == []
    assert result["waiting"] == ["unpersisted-feed"]
    assert store.publishing_calls == []
    assert pub.calls == []
    assert len(proofs) == 1
    assert failures == [("unpersisted-feed", "lasso",
                         "paired Story source proof invalid after caption "
                         "cleanup; feed remains held")]
    # The failed patch means the DB row still carries the pre-cleanup caption.
    assert store.rows["unpersisted-feed"]["caption"] == \
        "Move well; build strength with us today."
    assert store.rows["unpersisted-feed"]["status"] == "pending"


def test_lasso_feed_with_unchanged_caption_pays_no_second_proof(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    row = _row("clean-feed", post_date="2026-10-05",
               caption="Move well and build strength with us today.")
    row["slot_index"] = 0
    store = _FakeStore([row])
    proofs = []
    monkeypatch.setattr(store, "lasso_paired_story_ready_for_feed",
                        lambda _: proofs.append(1) or True, raising=False)
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)

    assert result["published"] == ["clean-feed"]
    assert len(proofs) == 1, "an unchanged caption must not pay a second proof RPC"


# ---- leased-row source revalidation for paired LASSO feeds (freeze) ---------
# due_rows is a snapshot; the owned claim pins ownership, not outgoing content.
# After a successful string-token claim the publisher re-fetches the leased row
# and requires the same token, 'publishing', source-field equality with the
# snapshot, and Story readiness on the LEASED row -- before any ledger stamp
# or network call.

class _LeasedPairStore(_FakeStore):
    """Owned string-token claim plus get_row over the live row dict."""

    TOKEN = "44444444-4444-4444-8444-444444444444"

    def __init__(self, rows, mutate=None):
        super().__init__(rows)
        self._mutate = mutate
        self.get_row_calls = 0
        self.ready_calls = 0

    def claim_publish_slot(self, row_id, gym_id, day, timezone_name,
                           capacity, approved_only):
        row = self.rows.get(row_id)
        if not row or row.get("status") not in ("pending", "approved"):
            return None
        row.update(status="publishing", publish_claim_token=self.TOKEN,
                   publish_reservation_day=day)
        if self._mutate:
            self._mutate(row)          # a patch landing while the row was pending
        return self.TOKEN

    def get_row(self, gym_id, row_id):
        self.get_row_calls += 1
        row = self.rows.get(row_id)
        return dict(row) if row and row.get("gym_id") == gym_id else None

    def lasso_paired_story_ready_for_feed(self, feed_id):
        self.ready_calls += 1
        return True

    def mark_publish_failed(self, row_id, revert_status="pending",
                            reject_reason=None, gym_id=None,
                            expected_claim_token=None):
        self.failed_calls.append(row_id)
        row = self.rows.get(row_id)
        if (not row or expected_claim_token != row.get("publish_claim_token")):
            return None
        row.update(status=revert_status, publish_claim_token=None,
                   reject_reason=reject_reason)
        return dict(row)

    def mark_published(self, row_id, media_id, published_at,
                       expected_claim_token=None):
        row = self.rows.get(row_id)
        if (not row or expected_claim_token != row.get("publish_claim_token")):
            return None
        self.published_calls.append((row_id, media_id, published_at))
        row.update(status="published", published_at=published_at,
                   late_post_id=media_id, publish_claim_token=None)
        return dict(row)


def _leased_pair_row(row_id="leased-feed"):
    row = _row(row_id, post_date="2026-10-05",
               caption="Move well and build strength with us today.")
    row["slot_index"] = 0
    return row


def test_leased_pair_mutated_between_proof_and_claim_rolls_back(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    store = _LeasedPairStore([_leased_pair_row()],
                             mutate=lambda row: row.update(
                                 caption="patched while pending"))
    stamps = []

    class LedgerSpy:
        def get(self, key, default=""):
            return ""
        def set(self, key, value):
            stamps.append((key, value))

    monkeypatch.setattr(cap, "_kv_default", lambda: LedgerSpy())
    failures = []
    monkeypatch.setattr(cap, "_note_repeat_failure",
                        lambda rid, gym, exc: failures.append((rid, gym, str(exc))))
    pub = _FakePublisher()

    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)

    assert result["published"] == []
    assert result["failed"] == ["leased-feed"]
    assert pub.calls == []                        # no network call
    assert stamps == []                           # no content-ledger stamp
    assert store.get_row_calls == 1
    # The snapshot/lease mismatch short-circuits: no second readiness RPC.
    assert store.ready_calls == 1
    assert failures == [("leased-feed", "lasso",
                         "leased paired feed changed after the Story source "
                         "proof; feed remains held")]
    rolled = store.rows["leased-feed"]
    assert rolled["status"] == "pending"
    assert rolled["publish_claim_token"] is None
    assert rolled["reject_reason"] == "leased_feed_source_mismatch"


def test_leased_pair_unchanged_publishes_with_readiness_on_lease(armed, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)
    store = _LeasedPairStore([_leased_pair_row()])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)

    assert result["published"] == ["leased-feed"]
    assert store.get_row_calls == 1
    # First proof on the snapshot, second on the leased row.
    assert store.ready_calls == 2
    assert store.rows["leased-feed"]["status"] == "published"


def test_legacy_bool_claim_store_skips_the_lease_gate(armed, monkeypatch):
    """Legacy injectable stores return True from mark_publishing (no string
    token); the re-fetch gate must not apply to them."""
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setattr(config, "lasso_via_zernio_enabled", lambda: False)

    class _LegacyStore(_FakeStore):
        def lasso_paired_story_ready_for_feed(self, feed_id):
            return True
        def get_row(self, gym_id, row_id):
            raise AssertionError("lease gate must not run on a bool claim")

    store = _LegacyStore([_leased_pair_row("legacy-feed")])
    pub = _FakePublisher(PublishResult(ok=True, mode="published", media_id="M"))

    result = cap.publish_due("2026-10-05", store=store, publisher=pub,
                             now="2026-10-05T23:59:00-04:00", catch_all=True)

    assert result["published"] == ["legacy-feed"]


@pytest.mark.parametrize("kind", ["verification", "duplicate"])
def test_forward_media_hold_prevents_provider_and_releases_owned_lease(armed, monkeypatch, kind):
    from agent import forward_media_guard as guard, forward_media_publish as bridge
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    row = _row("guarded")
    store = _FakeStore([row], claim_returns={"guarded": "owned-token"})
    pub = _FakePublisher()
    monkeypatch.setattr(cap.meta_publisher, "publish", pub)
    released = []
    def release(store_arg, row_id, **kwargs):
        released.append(kwargs)
        return True
    def refuse(*args):
        exc = guard.ForwardMediaDuplicateHold if kind == "duplicate" else guard.ForwardMediaVerificationHold
        raise exc("persisted evidence unavailable")
    monkeypatch.setattr(bridge, "authorize", refuse)
    monkeypatch.setattr(cap, "_revert_to_pending", release)
    monkeypatch.setattr(cap, "_alert_publish_blocked", lambda *a, **kw: None)
    out = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert pub.calls == [] and out["published"] == []
    assert released[0]["expected_claim_token"] == "owned-token"
    assert released[0]["reject_reason"] == "forward_media_" + kind


def test_forward_media_missing_owned_token_never_calls_provider(armed, monkeypatch):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    store = _FakeStore([_row("no-token")])
    pub = _FakePublisher()
    monkeypatch.setattr(cap.meta_publisher, "publish", pub)
    monkeypatch.setattr(cap, "_alert_publish_blocked", lambda *a, **kw: None)
    out = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert pub.calls == [] and out["published"] == []
    assert out["forward_media_holds"] == {"no-token": "forward_media_verification"}
    assert store.failed_calls == []  # no unscoped rollback of an unknown lease


def test_forward_media_actual_draft_mismatch_never_claims_or_sends(armed, monkeypatch):
    from agent import forward_media_publish as bridge
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    original = cap._draft_for
    def changed(row):
        draft = original(row)
        draft.creative_public_url = "https://changed/media"
        return draft
    monkeypatch.setattr(cap, "_draft_for", changed)
    monkeypatch.setattr(bridge, "authorize", lambda *a: pytest.fail("changed draft reached authority"))
    monkeypatch.setattr(cap, "_alert_publish_blocked", lambda *a, **kw: None)
    store = _FakeStore([_row("changed")], claim_returns={"changed": "owned-token"})
    pub = _FakePublisher()
    monkeypatch.setattr(cap.meta_publisher, "publish", pub)
    out = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert pub.calls == [] and out["forward_media_holds"] == {"changed": "forward_media_verification"}

@pytest.mark.parametrize('fmt',['feed','story'])
def test_owned_current_style_failure_preclaim_never_calls_provider(armed,monkeypatch,fmt):
    from agent import lasso_current_artifact
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY','true')
    monkeypatch.setattr(lasso_current_artifact,'current_pair',lambda *a,**k:False)
    row=_row('style-block',fmt=fmt)
    store=_FakeStore([row]);pub=_FakePublisher(PublishResult(ok=True,mode='published',media_id='M'))
    result=cap.publish_due(RUN_DATE,store=store,publisher=pub,now=LATE_NOW)
    assert result['waiting']==['style-block']
    assert not store.publishing_calls and not pub.calls

@pytest.mark.parametrize('fmt',['feed','story'])
def test_owned_current_style_changes_after_claim_reverts_owned_token(armed,monkeypatch,fmt):
    from agent import lasso_current_artifact
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY','true')
    # Registry/artifact source was current before lease, then changed after it.
    monkeypatch.setattr(lasso_current_artifact,'current_pair',lambda _,r:r.get('status')!='publishing')
    row=_row('style-race',fmt=fmt)
    store=_LeasedPairStore([row]);pub=_FakePublisher(PublishResult(ok=True,mode='published',media_id='M'))
    result=cap.publish_due(RUN_DATE,store=store,publisher=pub,now=LATE_NOW)
    assert result['failed']==['style-race'] and not pub.calls
    assert store.rows['style-race']['status']=='pending'
    assert store.rows['style-race']['publish_claim_token'] is None
    assert store.rows['style-race']['reject_reason']=='lasso_current_visual_proof_missing'
