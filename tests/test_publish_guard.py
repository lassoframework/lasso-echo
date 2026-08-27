"""publish_guard (WIRING.md 2026-08-27): the ONE publish-boundary rail, all offline.

Spec coverage:
  * visible_len semantics: emoji-only / '...' / zero-width junk count as 0;
  * story exemption: a story payload skips the caption rails (empty-body BY
    DESIGN — the audit's '26 empty IG captions' were story rows), but never
    media_ready;
  * feed empty caption blocks;
  * proof-without-mention blocks, and through publish_due the row goes BACK TO
    PENDING (with reject_reason) and the publisher is never called;
  * two distinct asks block; one ask passes (even repeated);
  * HYROX (avatar rail) blocks;
  * the publish_blocked:<gym>:<code> alert fires ONCE and stays quiet on the
    next violating tick (deduped until the state changes).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap
from agent import publish_guard as pg
from agent.meta_publisher import PublishResult
from agent.publish_guard import PublishPayload, check, visible_len

RUN_DATE = "2026-08-10"
LATE_NOW = "2026-08-10T23:59:00-04:00"

# A caption that clears EVERY rail: >= 40 chars, no dash, exactly one ask
# family, no banned-audience term.
CLEAN = ("Your first month at the gym should feel simple and welcoming. "
         "Book your intro today.")


def _payload(**kw):
    base = dict(row_id="r1", gym_id="g1", platform="instagram", caption=CLEAN,
                category="", mentions=[], media_ready=True, is_story=False)
    base.update(kw)
    return PublishPayload(**base)


# ---- visible_len semantics ---------------------------------------------------

def test_visible_len_counts_alphanumerics_only():
    assert visible_len("hello 123") == 8
    assert visible_len("") == 0
    assert visible_len(None) == 0
    assert visible_len("🔥🔥🔥") == 0                    # emoji are not captions
    assert visible_len("...") == 0                       # dots are not captions
    assert visible_len("— – - -- …") == 0                # dashes are not captions
    assert visible_len("​‌‍﻿") == 0  # zero-width junk
    assert visible_len("🔥 a 🔥") == 1


# ---- feed caption rails --------------------------------------------------------

def test_feed_empty_caption_blocks():
    assert pg.EMPTY_CAPTION in check(_payload(caption=""))
    assert pg.EMPTY_CAPTION in check(_payload(caption="🔥🔥 ... —"))


def test_feed_thin_caption_blocks():
    v = check(_payload(caption="Come on in today, we are open now"))  # < 40 chars
    assert pg.THIN_CAPTION in v


def test_clean_feed_passes():
    assert check(_payload()) == []


def test_copy_violation_blocks():
    v = check(_payload(caption=CLEAN + " Real results — real people."))
    assert pg.COPY_VIOLATION in v


# ---- story exemption -----------------------------------------------------------

def test_story_empty_caption_is_exempt():
    """Empty-body BY DESIGN (contentType='story', caption burned on media)."""
    assert check(_payload(caption="", is_story=True)) == []


def test_story_never_exempt_from_media_ready():
    v = check(_payload(caption="", is_story=True, media_ready=False))
    assert v == [pg.MEDIA_MISSING]


# ---- proof/results mention rail --------------------------------------------------

def test_proof_without_mention_blocks():
    v = check(_payload(category="proof"), handles_fn=lambda g: ["gymhandle"])
    assert pg.MISSING_MENTION in v


def test_proof_with_allowlisted_mention_passes():
    v = check(_payload(category="proof", mentions=["@gymhandle"]),
              handles_fn=lambda g: ["gymhandle"])
    assert pg.MISSING_MENTION not in v


def test_proof_with_off_list_mention_blocks():
    v = check(_payload(category="results", mentions=["@randomstranger"]),
              handles_fn=lambda g: ["gymhandle"])
    assert pg.MISSING_MENTION in v


def test_proof_mention_rail_fails_closed_on_allowlist_error():
    def boom(gym_id):
        raise RuntimeError("supabase down")
    v = check(_payload(category="proof", mentions=["@gymhandle"]), handles_fn=boom)
    assert pg.MISSING_MENTION in v


# ---- ask counting ---------------------------------------------------------------

def test_two_distinct_asks_block():
    cap_text = ("Your first month should feel simple and welcoming for everyone. "
                "Book your intro today. Questions first? DM us and we will help.")
    v = check(_payload(caption=cap_text))
    assert pg.MULTI_ASK in v


def test_one_ask_passes_even_repeated():
    cap_text = ("Your first month should feel simple and welcoming for everyone. "
                "Book your intro today. Seriously, book your intro.")
    v = check(_payload(caption=cap_text))
    assert pg.MULTI_ASK not in v


# ---- avatar rail -----------------------------------------------------------------

def test_hyrox_blocks():
    cap_text = ("HYROX season is here and our coaches are ready to guide your "
                "training plan. Book your intro today.")
    assert pg.AVATAR_BLOCK in check(_payload(caption=cap_text))


# ---- publish_due wiring: revert to pending + deduped alert -----------------------

class _Store:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.reject_reasons = {}

    def due_rows(self, gym_id, run_date, catchup_days=0):
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id and r.get("post_date") == run_date
                and not r.get("published_at")
                and r.get("status") not in ("published",)]

    def mark_publishing(self, row_id):
        r = self.rows.get(row_id)
        if not r or r.get("status") not in ("pending", "approved"):
            return False
        r["status"] = "publishing"
        return True

    def mark_published(self, row_id, media_id, published_at):
        r = self.rows[row_id]
        r["status"] = "published"
        r["published_at"] = published_at
        return r

    def mark_publish_failed(self, row_id, revert_status="pending",
                            reject_reason=None):
        r = self.rows[row_id]
        r["status"] = revert_status
        if reject_reason is not None:
            self.reject_reasons[row_id] = reject_reason
        return r


class _Pub:
    def __init__(self):
        self.calls = []

    def __call__(self, draft, account):
        self.calls.append(draft)
        return PublishResult(ok=True, mode="published", media_id="M1")


def _row(row_id, **kw):
    base = dict(id=row_id, gym_id="lasso", post_date=RUN_DATE,
                account="instagram", format="feed", status="pending",
                caption=CLEAN, image_url="https://cdn/x.jpg",
                published_at=None, late_post_id=None)
    base.update(kw)
    return base


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")


def _alerts(monkeypatch):
    from agent import ops_alerts
    sent = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    return sent


def test_proof_without_mention_reverts_to_pending(guarded, monkeypatch):
    """A proof row with no allowlisted mention never publishes: back to pending
    with a reject_reason naming the code; the publisher is never called.
    (Offline: no creds -> the allowlist reads empty -> fail closed.)"""
    sent = _alerts(monkeypatch)
    store = _Store([_row("p1", category="proof")])
    pub = _Pub()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "p1" in summary["failed"] and summary["published"] == []
    assert store.rows["p1"]["status"] == "pending"
    assert "missing_mention" in store.reject_reasons["p1"]
    assert pub.calls == []
    assert any("missing_mention" in m for m in sent)


def test_two_asks_reverts_one_ask_publishes(guarded, monkeypatch):
    _alerts(monkeypatch)
    two = CLEAN + " Questions first? DM us and we will help you get started."
    store = _Store([_row("a2", caption=two), _row("a1", caption=CLEAN)])
    pub = _Pub()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "a2" in summary["failed"]
    assert store.rows["a2"]["status"] == "pending"
    assert "multi_ask" in store.reject_reasons["a2"]
    assert "a1" in summary["published"]                  # one ask sails through


def test_blocked_alert_fires_once_until_state_changes(guarded, monkeypatch):
    """kv key publish_blocked:<gym>:<code>: the first violating tick alerts, the
    retry tick is silent; a clean pass re-arms it and a NEW violation alerts."""
    sent = _alerts(monkeypatch)
    hy = ("HYROX season is here and our coaches are ready to guide your "
          "training plan. Book your intro today.")
    store = _Store([_row("h1", caption=hy)])
    pub = _Pub()
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)  # retry tick
    assert len([m for m in sent if "avatar_block" in m]) == 1            # deduped

    # a clean row publishing changes the state -> the dedup re-arms
    store.rows["ok"] = _row("ok")
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "ok" in [getattr(d, "draft_id", "") for d in pub.calls]
    cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)  # h1 again
    assert len([m for m in sent if "avatar_block" in m]) == 2


def test_story_row_exempt_through_publish_due(guarded, monkeypatch):
    _alerts(monkeypatch)
    store = _Store([_row("st", format="story", caption="")])
    pub = _Pub()
    summary = cap.publish_due(RUN_DATE, store=store, publisher=pub, now=LATE_NOW)
    assert "st" in summary["published"]                  # empty-body by design


# ---- belt and braces in the zernio publisher --------------------------------------

class _FakeZernio:
    def __init__(self):
        self.created = []

    def list_accounts(self, profile_id):
        return {"accounts": [{"_id": "ig1", "platform": "instagram"}]}

    def create_post(self, account_id, body, media_urls=None, scheduled_for=None,
                    page_id=None, platform=None, story=False):
        self.created.append({"body": body, "story": story})
        return {"_id": "z1"}


def _zernio_setup(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")
    from agent.accounts import Account, Platform
    return Account(key="eng_ig", display_name="ENG IG",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="G")


def test_zernio_publisher_raises_on_invisible_feed_body(monkeypatch):
    from agent import zernio_publisher
    acct = _zernio_setup(monkeypatch)
    client = _FakeZernio()

    class D:
        caption = "🔥🔥 ..."                             # zero visible characters
        creative_public_url = "https://r2/a.jpg"
    with pytest.raises(ValueError):
        zernio_publisher.publish(D(), acct, client=client,
                                 profile_resolver=lambda k: "p1")
    assert client.created == []                          # never reached the API


def test_zernio_publisher_story_empty_body_still_allowed(monkeypatch):
    """STORIES CARRY NO CAPTION (zernio_publisher.py): deliberately body=''."""
    from agent import zernio_publisher
    acct = _zernio_setup(monkeypatch)
    client = _FakeZernio()

    class D:
        caption = "burned onto the media"
        creative_public_url = "https://r2/a.jpg"
        is_story = True
    r = zernio_publisher.publish(D(), acct, client=client,
                                 profile_resolver=lambda k: "p1")
    assert r.mode == "published"
    assert client.created == [{"body": "", "story": True}]
