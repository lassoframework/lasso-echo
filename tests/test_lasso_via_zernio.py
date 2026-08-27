"""
LASSO-via-Zernio cutover (AGENT_LASSO_VIA_ZERNIO), all offline.

WHY the flag exists: metrics_sync ingests Zernio analytics; LASSO's Meta-direct
posts read there as an external/second publisher and taint LASSO's own months for
the learning loop. One publish path = one guard set.

Coverage:
  - flag OFF is byte-for-byte today: lasso rows publish Meta-direct, the zernio
    publisher is never touched, publish_client_gyms never includes lasso.
  - flag ON: publish_client_gyms includes 'lasso' and its rows route through the
    zernio publisher exactly like a client gym; the Meta-direct publisher is NEVER
    called for a lasso row (choke point holds even for a catch_all publish_due).
  - the Meta-direct slot lane (run_slot_ticks) stands down under the flag.
  - NO path double-publishes a row under either flag state (exactly-once claims).
  - missing setup (gyms.zernio_profile_id / zernio_default_fb_page_id) HOLDS the
    lane with ONE deduped alert: no claim, no publish, no Meta-direct fallback;
    the dedup re-arms once setup completes.
  - a lasso STORY routes through the zernio lane with is_story intact, and the
    real zernio_publisher sends contentType story with an EMPTY body (the burned
    caption travels on the media), resolving the profile/page from the 'lasso'
    gyms row via the lasso_ig/lasso_fb -> lasso tenant-base fallback.
  - the setup CLI (lasso_zernio_setup.run) stamps idempotently: never overwrites
    a non-empty profile id, auto-picks the FB page only when unambiguous, honors
    --page, and stamps lasso autonomy so today's no-approval model is kept.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap
from agent import config
from agent import db
from agent import lasso_zernio_setup as lzs
from agent.meta_publisher import PublishResult


RUN_DATE = "2026-08-10"
LATE_NOW = "2026-08-10T23:59:00-04:00"     # past the last slot: everything is due

PROFILE_ID = lzs.LASSO_ZERNIO_PROFILE_ID   # 6a74a3b977a9ae3719f5c0c0
PAGE_ID = "fbpage77"


# ---- fakes -----------------------------------------------------------------

def _row(row_id, account="instagram", fmt="feed", post_date=RUN_DATE,
         status="pending", caption="hello there friends", image_url="https://cdn/x.jpg",
         published_at=None, late_post_id=None):
    return {
        "id": row_id, "gym_id": "lasso", "post_date": post_date,
        "account": account, "format": fmt, "status": status,
        "caption": caption, "image_url": image_url,
        "published_at": published_at, "late_post_id": late_post_id,
    }


class _FakeStore:
    """In-memory content_calendar mirroring the real store's due filter + the
    ATOMIC mark_publishing claim (status in (pending, approved) and unpublished
    wins). gym_autonomy returns None (not autonomous) like an absent settings row."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.published_calls = []
        self.failed_calls = []
        self.publishing_calls = []

    def due_rows(self, gym_id, run_date, catchup_days=0):
        out = []
        for r in self.rows.values():
            if r.get("gym_id") != gym_id:
                continue
            if r.get("post_date") != run_date:
                continue
            if r.get("status") in ("published", "denied", "killed"):
                continue
            if r.get("published_at") or not r.get("image_url"):
                continue
            out.append(dict(r))
        return out

    def mark_publishing(self, row_id):
        self.publishing_calls.append(row_id)
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

    def mark_publish_failed(self, row_id, revert_status="pending",
                            reject_reason=""):
        self.failed_calls.append(row_id)
        r = self.rows.get(row_id)
        if r:
            r["status"] = revert_status
        return r

    def gym_autonomy(self, gym_slug):
        return None


class _KVDict:
    def __init__(self):
        self.d = {}

    def get(self, key, default=""):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _bomb_publisher(draft, account):
    raise AssertionError(
        f"Meta-direct publisher called for {account.key} under the flag")


def _zern_capture(sent):
    def _pub(draft, account, scheduled_for=None):
        sent.append((draft, account, scheduled_for))
        return PublishResult(ok=True, mode="published", media_id="Z1")
    return _pub


def _stamp_lasso_setup():
    """The state lasso-zernio-setup leaves behind (against the isolated test db)."""
    db.gym_upsert("lasso", zernio_profile_id=PROFILE_ID,
                  zernio_default_fb_page_id=PAGE_ID)
    db.set_autonomy("lasso", True)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")


@pytest.fixture
def lasso_flag(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_VIA_ZERNIO", "true")


@pytest.fixture
def alerts(monkeypatch):
    """Capture ops alerts (calendar_autopublish imports ops_alerts lazily)."""
    from agent import ops_alerts
    captured = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: captured.append(msg))
    return captured


# ---- flag OFF: byte-for-byte today ------------------------------------------

def test_flag_off_lasso_publishes_meta_direct_and_zernio_never_touched(armed):
    assert config.lasso_via_zernio_enabled() is False       # default OFF
    store = _FakeStore([_row("a"), _row("b", account="facebook")])
    meta_calls = []

    def _meta(draft, account):
        meta_calls.append((draft.draft_id, account.key))
        return PublishResult(ok=True, mode="published", media_id="M")

    def _zern_bomb(draft, account, scheduled_for=None):
        raise AssertionError("zernio publisher called with the flag OFF")

    s = cap.publish_due(RUN_DATE, store=store, publisher=_meta,
                        zernio_publish=_zern_bomb, now=LATE_NOW)
    assert set(s["published"]) == {"a", "b"}
    assert sorted(k for _, k in meta_calls) == ["lasso_fb", "lasso_ig"]


def test_flag_off_publish_client_gyms_never_includes_lasso(armed):
    store = _FakeStore([_row("a")])                        # a due lasso row exists
    sent = []
    out = cap.publish_client_gyms(RUN_DATE, store=store,
                                  zernio_publish=_zern_capture(sent), now=LATE_NOW)
    assert all(s.get("gym") != "lasso" for s in out)
    assert sent == []                                      # the lasso row untouched
    assert store.rows["a"]["status"] == "pending"


# ---- flag ON: lasso routes through the zernio lane like a client gym --------

def test_flag_on_lasso_rows_route_via_zernio_like_a_client(armed, lasso_flag,
                                                           monkeypatch):
    _stamp_lasso_setup()
    monkeypatch.setattr(cap.meta_publisher, "publish", _bomb_publisher)
    store = _FakeStore([_row("ig1"), _row("fb1", account="facebook")])
    sent = []

    out = cap.publish_client_gyms(RUN_DATE, store=store,
                                  zernio_publish=_zern_capture(sent), now=LATE_NOW)

    lasso = [s for s in out if s.get("gym") == "lasso"]
    assert len(lasso) == 1
    assert set(lasso[0]["published"]) == {"ig1", "fb1"}
    assert lasso[0]["autonomous"] is True                  # setup stamped autonomy
    # routed to the LASSO accounts through the zernio publisher, publish-NOW
    assert sorted(a.key for _, a, _ in sent) == ["lasso_fb", "lasso_ig"]
    assert all(sched is None for _, _, sched in sent)
    # recorded exactly once each with the zernio post id
    assert {rid for rid, mid, _ in store.published_calls} == {"ig1", "fb1"}
    assert all(mid == "Z1" for _, mid, _ in store.published_calls)


def test_flag_on_even_a_catch_all_publish_due_cannot_reach_meta(armed, lasso_flag,
                                                                monkeypatch):
    """The runner's once/day sweep is gated off under the flag, but even if a
    publish_due(gym_id='lasso') call slipped through anywhere, the routing choke
    point sends it through zernio — Meta-direct is impossible under the flag."""
    _stamp_lasso_setup()
    monkeypatch.setattr(cap.meta_publisher, "publish", _bomb_publisher)
    store = _FakeStore([_row("x")])
    sent = []
    s = cap.publish_due(RUN_DATE, store=store, publisher=_bomb_publisher,
                        zernio_publish=_zern_capture(sent), now=LATE_NOW,
                        catch_all=True)
    assert s["published"] == ["x"]
    assert len(sent) == 1 and sent[0][1].key == "lasso_ig"


def test_flag_on_meta_slot_lane_stands_down(armed, lasso_flag):
    _stamp_lasso_setup()
    store = _FakeStore([_row("x")])
    kv = _KVDict()
    fired = cap.run_slot_ticks(RUN_DATE, store=store, publisher=_bomb_publisher,
                               now=LATE_NOW, kv=kv)
    assert fired == []
    assert store.publishing_calls == []                    # nothing claimed
    assert kv.d == {}                                      # no slot marker burned


def test_flag_on_story_row_travels_the_zernio_lane_as_a_story(armed, lasso_flag,
                                                              monkeypatch):
    _stamp_lasso_setup()
    monkeypatch.setattr(cap.meta_publisher, "publish", _bomb_publisher)
    store = _FakeStore([_row("s1", fmt="story")])
    sent = []
    out = cap.publish_client_gyms(RUN_DATE, store=store,
                                  zernio_publish=_zern_capture(sent), now=LATE_NOW)
    lasso = [s for s in out if s.get("gym") == "lasso"][0]
    assert lasso["published"] == ["s1"]
    draft, account, sched = sent[0]
    assert draft.is_story is True and draft.draft_type == "story"
    assert account.key == "lasso_ig" and sched is None


def test_flag_on_real_zernio_publisher_story_and_feed_resolve_the_lasso_gyms_row(
        armed, lasso_flag):
    """End-to-end through the REAL zernio_publisher with a fake client: the
    lasso_ig/lasso_fb account keys resolve profile + page from the 'lasso' gyms
    row (tenant-base fallback), a STORY sends contentType story with an EMPTY
    body (burned caption on media), and a FEED carries its caption + page id."""
    from agent import zernio_publisher
    from agent.accounts import get_account
    _stamp_lasso_setup()

    class _FakeClient:
        def __init__(self):
            self.posts = []

        def list_accounts(self, profile_id):
            assert profile_id == PROFILE_ID
            return {"accounts": [
                {"platform": "instagram", "_id": "igacct1"},
                {"platform": "facebook", "_id": "fbacct1"},
            ]}

        def create_post(self, account_id, body, media_urls=None,
                        scheduled_for=None, page_id=None, platform=None,
                        story=False):
            self.posts.append({"account_id": account_id, "body": body,
                               "page_id": page_id, "platform": platform,
                               "story": story})
            return {"_id": "ZPOST"}

    client = _FakeClient()
    story_row = _row("s", fmt="story", caption="burned words")
    story_draft = cap._draft_for(story_row)
    story_draft.account_key = "lasso_ig"
    res = zernio_publisher.publish(story_draft, get_account("lasso_ig"),
                                   client=client)
    assert res.ok and res.mode == "published" and res.media_id == "ZPOST"
    assert client.posts[0]["story"] is True
    assert client.posts[0]["body"] == ""                   # caption rides the media
    assert client.posts[0]["account_id"] == "igacct1"

    feed_row = _row("f", account="facebook", caption="real feed words here")
    feed_draft = cap._draft_for(feed_row)
    feed_draft.account_key = "lasso_fb"
    res2 = zernio_publisher.publish(feed_draft, get_account("lasso_fb"),
                                    client=client)
    assert res2.ok and res2.mode == "published"
    assert client.posts[1]["story"] is False
    assert client.posts[1]["body"] == "real feed words here"
    assert client.posts[1]["page_id"] == PAGE_ID           # from the lasso gyms row


# ---- exactly one lane owns a row: no double publish under either state ------

def test_flag_on_no_path_double_publishes(armed, lasso_flag, monkeypatch):
    _stamp_lasso_setup()
    monkeypatch.setattr(cap.meta_publisher, "publish", _bomb_publisher)
    store = _FakeStore([_row("x")])
    sent = []
    # a full listener tick: the meta slot lane (stands down) + the zernio lane
    assert cap.run_slot_ticks(RUN_DATE, store=store, publisher=_bomb_publisher,
                              now=LATE_NOW, kv=_KVDict()) == []
    cap.publish_client_gyms(RUN_DATE, store=store,
                            zernio_publish=_zern_capture(sent), now=LATE_NOW)
    # ... and the next tick, plus a rogue direct publish_due: nothing re-sends
    cap.publish_client_gyms(RUN_DATE, store=store,
                            zernio_publish=_zern_capture(sent), now=LATE_NOW)
    cap.publish_due(RUN_DATE, store=store, publisher=_bomb_publisher,
                    zernio_publish=_zern_capture(sent), now=LATE_NOW,
                    catch_all=True)
    assert len(sent) == 1                                  # exactly once
    assert len(store.published_calls) == 1


def test_flag_off_no_path_double_publishes(armed):
    store = _FakeStore([_row("x")])
    meta_calls = []

    def _meta(draft, account):
        meta_calls.append(draft.draft_id)
        return PublishResult(ok=True, mode="published", media_id="M")

    sent = []
    # the meta lane owns the row (last slot's catch_all sweeps it) ...
    cap.run_slot_ticks(RUN_DATE, store=store, publisher=_meta, now=LATE_NOW,
                       kv=_KVDict())
    # ... and the client lane never touches lasso with the flag off
    cap.publish_client_gyms(RUN_DATE, store=store,
                            zernio_publish=_zern_capture(sent), now=LATE_NOW)
    cap.publish_due(RUN_DATE, store=store, publisher=_meta, now=LATE_NOW,
                    catch_all=True)
    assert meta_calls == ["x"]                             # exactly once
    assert sent == []
    assert len(store.published_calls) == 1


# ---- cutover safety: missing setup HOLDS with one deduped alert -------------

def test_missing_setup_holds_never_claims_never_falls_back(armed, lasso_flag,
                                                           alerts, monkeypatch):
    monkeypatch.setattr(cap.meta_publisher, "publish", _bomb_publisher)
    store = _FakeStore([_row("x")])

    def _zern_bomb(draft, account, scheduled_for=None):
        raise AssertionError("published while setup was incomplete")

    s1 = cap.publish_due(RUN_DATE, store=store, publisher=_bomb_publisher,
                         zernio_publish=_zern_bomb, now=LATE_NOW)
    s2 = cap.publish_due(RUN_DATE, store=store, publisher=_bomb_publisher,
                         zernio_publish=_zern_bomb, now=LATE_NOW)
    for s in (s1, s2):
        assert s["ok"] is False and s.get("held") is True
        assert "zernio_profile_id" in s["reason"]
    assert store.publishing_calls == []                    # never claimed
    assert store.rows["x"]["status"] == "pending"          # never dropped
    assert len(alerts) == 1                                # ONE deduped alert
    assert "lasso-zernio-setup" in alerts[0]


def test_partial_setup_still_holds_and_names_the_missing_piece(armed, lasso_flag,
                                                               alerts):
    db.gym_upsert("lasso", zernio_profile_id=PROFILE_ID)   # page still missing
    store = _FakeStore([_row("x")])
    s = cap.publish_due(RUN_DATE, store=store,
                        zernio_publish=lambda *a, **k: None, now=LATE_NOW)
    assert s["ok"] is False and s.get("held") is True
    assert "zernio_default_fb_page_id" in s["reason"]
    assert "zernio_profile_id" not in s["reason"].replace(
        "zernio_default_fb_page_id", "")
    assert len(alerts) == 1


def test_hold_alert_rearms_after_setup_completes(armed, lasso_flag, alerts,
                                                 monkeypatch):
    monkeypatch.setattr(cap.meta_publisher, "publish", _bomb_publisher)
    store = _FakeStore([_row("x")])
    sent = []
    cap.publish_due(RUN_DATE, store=store, zernio_publish=_zern_capture(sent),
                    now=LATE_NOW)
    assert len(alerts) == 1 and sent == []
    _stamp_lasso_setup()                                   # setup completes
    s = cap.publish_due(RUN_DATE, store=store, zernio_publish=_zern_capture(sent),
                        now=LATE_NOW, catch_all=True)
    assert s["published"] == ["x"] and len(sent) == 1
    # a later REGRESSION (page cleared) alerts again — the dedup was re-armed
    db.gym_upsert("lasso", zernio_default_fb_page_id="")
    store2 = _FakeStore([_row("y")])
    s2 = cap.publish_due(RUN_DATE, store=store2, zernio_publish=_zern_capture(sent),
                         now=LATE_NOW)
    assert s2.get("held") is True
    assert len(alerts) == 2


# ---- setup CLI: idempotent stamping ------------------------------------------

class _FakeSetupZernio:
    def __init__(self, pages, fb_connected=True):
        self._pages = pages
        self._fb = fb_connected
        self.calls = []

    def list_accounts(self, profile_id):
        self.calls.append(("list_accounts", profile_id))
        accounts = [{"platform": "instagram", "_id": "igacct1"}]
        if self._fb:
            accounts.append({"platform": "facebook", "_id": "fbacct1"})
        return {"accounts": accounts}

    def list_facebook_pages(self, account_id):
        self.calls.append(("list_facebook_pages", account_id))
        return {"pages": [{"_id": p[0], "name": p[1]} for p in self._pages]}


def test_setup_stamps_profile_page_and_autonomy_idempotently():
    z = _FakeSetupZernio([("pg1", "My Only Page")])
    out = lzs.run(db=db, zclient=z, logger=lambda m: None)
    assert out["ok"] is True
    assert out["profile"] == "stamped"
    assert out["fb_page"] == "stamped"                     # single page auto-picked
    assert out["autonomy"] == "stamped"
    row = db.gym_get("lasso")
    assert row["zernio_profile_id"] == PROFILE_ID
    assert row["zernio_default_fb_page_id"] == "pg1"
    assert db.is_autonomous("lasso") is True
    # re-run: everything 'already', no Zernio call, nothing rewritten
    out2 = lzs.run(db=db, logger=lambda m: None)           # no client needed at all
    assert out2 == {"ok": True, "profile": "already", "fb_page": "already",
                    "autonomy": "already", "pages": []}


def test_setup_auto_picks_the_single_lasso_named_page():
    z = _FakeSetupZernio([("pg1", "Bird Dog CrossFit"),
                          ("pg2", "LASSO Framework"),
                          ("pg3", "Some Other Page")])
    out = lzs.run(db=db, zclient=z, logger=lambda m: None)
    assert out["ok"] is True and out["fb_page"] == "stamped"
    assert db.gym_get("lasso")["zernio_default_fb_page_id"] == "pg2"


def test_setup_ambiguous_pages_exit_asking_for_a_hand_pick():
    z = _FakeSetupZernio([("pg1", "LASSO East"), ("pg2", "LASSO West")])
    lines = []
    out = lzs.run(db=db, zclient=z, logger=lines.append)
    assert out["ok"] is False and out["fb_page"] == "ambiguous"
    assert [p["id"] for p in out["pages"]] == ["pg1", "pg2"]
    assert not (db.gym_get("lasso") or {}).get("zernio_default_fb_page_id")
    assert any("--page" in ln for ln in lines)             # asks for the hand-pick
    # the hand-pick stamps it
    out2 = lzs.run(page_id="pg2", db=db, zclient=z, logger=lambda m: None)
    assert out2["ok"] is True and out2["fb_page"] == "stamped"
    assert db.gym_get("lasso")["zernio_default_fb_page_id"] == "pg2"


def test_setup_rejects_a_hand_pick_not_in_the_list():
    z = _FakeSetupZernio([("pg1", "LASSO East"), ("pg2", "LASSO West")])
    out = lzs.run(page_id="nope", db=db, zclient=z, logger=lambda m: None)
    assert out["ok"] is False and out["fb_page"] == "bad_page"
    assert not (db.gym_get("lasso") or {}).get("zernio_default_fb_page_id")


def test_setup_never_overwrites_a_different_profile_id():
    db.gym_upsert("lasso", zernio_profile_id="deadbeefdeadbeefdeadbeef")
    out = lzs.run(db=db, zclient=_FakeSetupZernio([("pg1", "P")]),
                  logger=lambda m: None)
    assert out["ok"] is False and out["profile"] == "mismatch"
    assert db.gym_get("lasso")["zernio_profile_id"] == "deadbeefdeadbeefdeadbeef"


def test_setup_reports_when_facebook_is_not_connected():
    z = _FakeSetupZernio([], fb_connected=False)
    out = lzs.run(db=db, zclient=z, logger=lambda m: None)
    assert out["ok"] is False and out["fb_page"] == "no_facebook"
    # the profile stamp still landed (it is independent of the page pick)
    assert db.gym_get("lasso")["zernio_profile_id"] == PROFILE_ID
