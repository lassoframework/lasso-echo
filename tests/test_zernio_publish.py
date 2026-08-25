"""
Zernio client-publish lane: an approved CLIENT-gym post publishes to the gym's OWN
connected IG/FB via Zernio (POST /v1/posts), scheduled at its slot time. Fully
OFFLINE: fake Zernio client + fake calendar store, no network.

Asserts:
  - the flag defaults OFF and OFF (or publish OFF) => would_publish, NO network call;
  - create_post builds the verified payload (body, media[].url, accountId,
    scheduledFor omitted for publish-now, platformSpecificData.pageId for FB);
  - account-id resolution returns only a CONNECTED account, per platform;
  - the publisher resolves profile/account/page and returns the Zernio post id;
  - a missing profile / account / FB page is a hard error (never a wrong-account send);
  - calendar routing: LASSO stays Meta-direct + unchanged; a client gym routes to
    Zernio with a real scheduledFor; an UNAPPROVED client row is never published;
  - publish_client_gyms self-gates on all three flags and isolates per gym.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, zernio, zernio_publisher, calendar_autopublish as cap  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


# ---- fakes -------------------------------------------------------------------
class FakeZernioClient:
    def __init__(self, accounts=None, post_id="zpost_1"):
        self._accounts = accounts or {"accounts": [
            {"_id": "ig_acct_1", "platform": "instagram"},
            {"_id": "fb_acct_1", "platform": "facebook"},
        ]}
        self.post_id = post_id
        self.created = []

    def list_accounts(self, profile_id):
        self._last_profile = profile_id
        return self._accounts

    def create_post(self, account_id, body, media_urls=None, scheduled_for=None,
                    page_id=None, platform=None, story=False):
        self.created.append({"account_id": account_id, "body": body,
                             "media_urls": media_urls, "scheduled_for": scheduled_for,
                             "page_id": page_id, "platform": platform, "story": story})
        return {"_id": self.post_id}


def _ig_account(key="eng_ig"):
    return Account(key=key, display_name="ENG IG", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")


def _fb_account(key="eng_fb"):
    return Account(key=key, display_name="ENG FB", platform=Platform.FACEBOOK_PAGE,
                   token_env="T", target_id_env="G")


def _draft(caption="Come train with us.", url="https://r2/img.jpg"):
    class D:
        pass
    d = D()
    d.caption = caption
    d.creative_public_url = url
    return d


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")


# ---- create_post payload (verified shape) ------------------------------------
def test_create_post_payload_publish_now():
    posted = {}

    class Http:
        def post(self, url, json=None, headers=None, timeout=None):
            posted["url"] = url
            posted["json"] = json

            class R:
                status_code = 200

                def json(self):
                    return {"_id": "z1"}
            return R()

    c = zernio.ZernioClient(api_key="k", base="https://api.zernio.com", http=Http())
    c.create_post("acct1", "Hello", media_urls=["https://r2/a.jpg"],
                  platform="instagram")
    assert posted["url"].endswith("/v1/posts")
    body = posted["json"]
    # OpenAPI-verified shape: content + platforms[] + mediaItems (typed)
    assert body["content"] == "Hello"
    assert body["platforms"] == [{"platform": "instagram", "accountId": "acct1"}]
    assert body["mediaItems"] == [{"type": "image", "url": "https://r2/a.jpg"}]
    # no scheduledFor -> MUST be publishNow (else Zernio saves a DRAFT, not a post)
    assert body["publishNow"] is True
    assert "scheduledFor" not in body
    # legacy keys of the broken payload must be gone
    assert "accountId" not in body and "body" not in body and "media" not in body


def test_create_post_payload_scheduled_and_fb_page():
    posted = {}

    class Http:
        def post(self, url, json=None, headers=None, timeout=None):
            posted["json"] = json

            class R:
                status_code = 200

                def json(self):
                    return {"_id": "z2"}
            return R()

    c = zernio.ZernioClient(api_key="k", http=Http())
    c.create_post("fb1", "Hi", media_urls=["u"],
                  scheduled_for="2026-08-13T07:30:00-04:00",
                  page_id="PAGE99", platform="facebook")
    body = posted["json"]
    # scheduledFor is normalized to UTC so it can never disagree with `timezone`
    assert body["scheduledFor"] == "2026-08-13T11:30:00Z"
    assert body["timezone"] == "UTC"
    assert "publishNow" not in body                       # scheduled, not immediate
    entry = body["platforms"][0]
    assert entry["platform"] == "facebook" and entry["accountId"] == "fb1"
    # pageId rides INSIDE the platform entry, not at the top level
    assert entry["platformSpecificData"] == {"pageId": "PAGE99"}
    assert "platformSpecificData" not in body


def test_create_post_story_and_video_media_type():
    posted = {}

    class Http:
        def post(self, url, json=None, headers=None, timeout=None):
            posted["json"] = json
            posted["headers"] = headers

            class R:
                status_code = 200

                def json(self):
                    return {"_id": "z3"}
            return R()

    c = zernio.ZernioClient(api_key="k", http=Http())
    c.create_post("ig1", "Story time", media_urls=["https://r2/clip.mp4"],
                  platform="instagram", story=True)
    body = posted["json"]
    entry = body["platforms"][0]
    assert entry["platformSpecificData"] == {"contentType": "story"}
    assert body["mediaItems"] == [{"type": "video", "url": "https://r2/clip.mp4"}]
    # idempotency header always present
    assert posted["headers"].get("x-request-id")


# ---- account id resolution ---------------------------------------------------
def test_account_id_for_returns_connected_only():
    accts = {"accounts": [
        {"_id": "ig1", "platform": "instagram"},
        {"_id": "fb1", "platform": "facebook", "isActive": False},   # expired
    ]}
    assert zernio.account_id_for(accts, "instagram") == "ig1"
    assert zernio.account_id_for(accts, "facebook") is None          # not connected
    assert zernio.instagram_account_id(accts) == "ig1"


def test_post_id_of_tolerates_shapes():
    assert zernio.post_id_of({"_id": "a"}) == "a"
    assert zernio.post_id_of({"id": "b"}) == "b"
    assert zernio.post_id_of({"post": {"_id": "c"}}) == "c"
    assert zernio.post_id_of({}) == ""


# ---- publisher gating --------------------------------------------------------
def test_flags_off_is_would_publish_no_network(monkeypatch):
    monkeypatch.delenv("AGENT_ZERNIO_PUBLISH", raising=False)
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    client = FakeZernioClient()
    res = zernio_publisher.publish(_draft(), _ig_account(), client=client,
                                   profile_resolver=lambda k: "prof1")
    assert res.mode == "would_publish"
    assert client.created == []                            # NO network call


def test_publish_flag_off_is_would_publish(monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    client = FakeZernioClient()
    res = zernio_publisher.publish(_draft(), _ig_account(), client=client,
                                   profile_resolver=lambda k: "prof1")
    assert res.mode == "would_publish" and client.created == []


# ---- publisher happy path ----------------------------------------------------
def test_publish_ig_resolves_and_posts(monkeypatch):
    _arm(monkeypatch)
    client = FakeZernioClient(post_id="zpost_IG")
    res = zernio_publisher.publish(
        _draft(caption="Train today", url="https://r2/x.jpg"), _ig_account(),
        client=client, scheduled_for="2027-06-01T07:30:00-04:00",
        profile_resolver=lambda k: "prof_eng")
    assert res.ok and res.mode == "published" and res.media_id == "zpost_IG"
    assert len(client.created) == 1
    call = client.created[0]
    assert call["account_id"] == "ig_acct_1"              # the gym's own IG account
    assert call["body"] == "Train today"
    assert call["media_urls"] == ["https://r2/x.jpg"]
    assert call["scheduled_for"] == "2027-06-01T07:30:00-04:00"
    assert call["page_id"] is None                        # IG has no page
    assert call["platform"] == "instagram"                # platforms[] entry
    assert call["story"] is False                         # feed, not story


def test_publish_story_flows_content_type_and_no_caption(monkeypatch):
    _arm(monkeypatch)
    client = FakeZernioClient()
    d = _draft(caption="Story!", url="https://r2/s.jpg")
    d.is_story = True
    d.draft_type = "story"
    res = zernio_publisher.publish(d, _ig_account(), client=client,
                                   scheduled_for="2027-06-01T12:30:00-04:00",
                                   profile_resolver=lambda k: "prof_eng")
    assert res.ok
    assert client.created[0]["story"] is True, \
        "an approved STORY must publish as a story, never as a second feed post"
    # STORIES CARRY NO CAPTION: platforms don't display it, and the paired feed's
    # caption made the story byte-identical to the feed -> Zernio's 24h dedup 409'd
    # and the story was NEVER created while Echo marked it published.
    assert client.created[0]["body"] == "", \
        "a story must publish with an empty body or dedup eats it"


def test_publish_409_carries_existing_post_id(monkeypatch):
    _arm(monkeypatch)

    class DupClient(FakeZernioClient):
        def create_post(self, *a, **k):
            raise zernio.ZernioError(
                409, '{"error":"duplicate","existingPostId":"zp_prior"}')

    res = zernio_publisher.publish(_draft(), _ig_account(), client=DupClient(),
                                   profile_resolver=lambda k: "prof_eng")
    assert res.ok and res.media_id == "zp_prior"


def test_publish_past_slot_flips_to_publish_now(monkeypatch):
    _arm(monkeypatch)
    client = FakeZernioClient()
    res = zernio_publisher.publish(
        _draft(), _ig_account(), client=client,
        scheduled_for="2020-01-01T07:30:00-05:00",         # long past
        profile_resolver=lambda k: "prof_eng")
    assert res.ok
    assert client.created[0]["scheduled_for"] is None, \
        "a past slot must publish NOW, not hand Zernio a past scheduledFor"


def test_publish_409_dedup_is_success_not_retry_loop(monkeypatch):
    _arm(monkeypatch)

    class DupClient(FakeZernioClient):
        def create_post(self, *a, **k):
            raise zernio.ZernioError(409, '{"error":"duplicate post"}')

    res = zernio_publisher.publish(_draft(), _ig_account(), client=DupClient(),
                                   profile_resolver=lambda k: "prof_eng")
    assert res.ok and res.mode == "published"
    assert "dedup" in res.detail


def test_publish_fb_requires_and_sends_page(monkeypatch):
    _arm(monkeypatch)
    client = FakeZernioClient()
    res = zernio_publisher.publish(
        _draft(), _fb_account(), client=client,
        profile_resolver=lambda k: "prof_eng", page_resolver=lambda k: "PAGE_ENG")
    assert res.ok and res.media_id
    assert client.created[0]["account_id"] == "fb_acct_1"
    assert client.created[0]["page_id"] == "PAGE_ENG"


def test_missing_profile_is_hard_error(monkeypatch):
    _arm(monkeypatch)
    import pytest
    with pytest.raises(zernio_publisher.ZernioPublishError):
        zernio_publisher.publish(_draft(), _ig_account(), client=FakeZernioClient(),
                                 profile_resolver=lambda k: None)


def test_missing_fb_page_is_hard_error(monkeypatch):
    _arm(monkeypatch)
    import pytest
    with pytest.raises(zernio_publisher.ZernioPublishError):
        zernio_publisher.publish(_draft(), _fb_account(), client=FakeZernioClient(),
                                 profile_resolver=lambda k: "p", page_resolver=lambda k: None)


# ---- calendar routing (LASSO vs client) --------------------------------------
def test_account_for_lasso_unchanged():
    row_ig = {"account": "instagram"}
    row_fb = {"account": "facebook"}
    assert cap._account_for(row_ig, "lasso").key == "lasso_ig"
    assert cap._account_for(row_fb, "lasso").key == "lasso_fb"
    # default gym_id is lasso (byte-for-byte the original behavior)
    assert cap._account_for(row_ig).key == "lasso_ig"


def test_account_for_client_resolves_own_account():
    assert cap._account_for({"account": "instagram"}, "eng").key == "eng_ig"
    assert cap._account_for({"account": "facebook"}, "eng").key == "eng_fb"


def test_scheduled_iso_for_row_builds_slot_timestamp():
    iso = cap.scheduled_iso_for_row({"id": "r1", "format": "feed",
                                     "post_date": "2026-08-13"})
    assert iso.startswith("2026-08-13T")
    assert iso.count(":") >= 2                             # has a time-of-day


class FakeStore:
    """STORE-FAITHFUL fake: mark_publishing enforces the REAL PostgREST claim
    precondition (status in (pending, approved) AND published_at is null), mirroring
    SupabaseCalendarStore. This is exactly the contract that masked the approved-row
    claim bug when the fake blindly returned True — never weaken it."""

    CLAIMABLE = ("pending", "approved")

    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.published = []
        self.reverts = []

    def due_rows(self, gym_id, run_date):
        return [dict(r) for r in self._rows.values() if r.get("gym_id") == gym_id]

    def mark_publishing(self, row_id):
        row = self._rows.get(row_id)
        if not row or row.get("published_at") or \
                (row.get("status") or "") not in self.CLAIMABLE:
            return False
        row["status"] = "publishing"
        return True

    def mark_published(self, row_id, media_id, published_at):
        self._rows[row_id].update(status="published", published_at=published_at,
                                  late_post_id=media_id)
        self.published.append((row_id, media_id, published_at))
        return {"id": row_id}

    def mark_publish_failed(self, row_id, revert_status="pending"):
        self._rows[row_id]["status"] = revert_status
        self.reverts.append((row_id, revert_status))
        return {"id": row_id}


def test_publish_due_routes_client_through_zernio(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    rows = [{"id": "r1", "gym_id": "eng", "account": "instagram", "status": "approved",
             "post_date": "2026-08-13", "format": "feed", "image_url": "https://r2/i.jpg",
             "caption": "hi"}]
    store = FakeStore(rows)
    zcalls = []

    def fake_zernio(draft, account, scheduled_for=None):
        zcalls.append((account.key, scheduled_for))
        from agent.zernio_publisher import PublishResult
        return PublishResult(ok=True, mode="published", media_id="zp1")

    out = cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=True,
                          zernio_publish=fake_zernio, catch_all=True)
    assert out["ok"] and out["published"] == ["r1"]
    # MANUAL APPROVAL PUBLISHES NOW: a client who tapped Approve expects the post live,
    # not scheduled to a later slot (which reads 'published' while the feed is empty).
    assert zcalls[0] == ("eng_ig", None)
    assert store.published and store.published[0][1] == "zp1"     # late_post_id recorded


def test_publish_due_autonomous_publishes_now_at_slot(monkeypatch):
    """CONTRACT CHANGE (audit 2026-08-25): an autonomous gym no longer hands Zernio a
    future scheduledFor (that was immediately marked published with a published_at hours
    before the post existed, with no reconcile). The lane's slot gate fires the row AT
    its slot; the send itself is always publish-NOW (scheduled_for=None) so published_at
    is truthful. The day still DRIPS because each row's own slot gates it."""
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    rows = [{"id": "r1", "gym_id": "eng", "account": "instagram", "status": "pending",
             "post_date": "2026-08-13", "format": "feed", "image_url": "https://r2/i.jpg",
             "caption": "hi"}]
    store = FakeStore(rows)
    zcalls = []

    def fake_zernio(draft, account, scheduled_for=None):
        zcalls.append((account.key, scheduled_for))
        from agent.zernio_publisher import PublishResult
        return PublishResult(ok=True, mode="published", media_id="zp1")

    out = cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=False,
                          zernio_publish=fake_zernio, catch_all=True)
    assert out["ok"] and out["published"] == ["r1"]
    assert zcalls[0][0] == "eng_ig" and zcalls[0][1] is None, \
        "the send is publish-NOW; the slot gate (not Zernio scheduling) does the dripping"


def test_publish_due_skips_unapproved_client_row(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    rows = [{"id": "r2", "gym_id": "eng", "account": "instagram", "status": "pending",
             "post_date": "2026-08-13", "format": "feed", "image_url": "https://r2/i.jpg"}]
    store = FakeStore(rows)
    called = []
    out = cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=True,
                          zernio_publish=lambda *a, **k: called.append(1), catch_all=True)
    assert out["published"] == [] and called == []          # pending never published
    assert "r2" in out["waiting"]


def test_approved_row_is_claimable_regression(monkeypatch):
    """REGRESSION for the audit CRITICAL: the claim (mark_publishing) must accept an
    APPROVED row, or every client approval dies in skipped forever. The store-faithful
    fake enforces the real precondition, so this test fails if the claim filter ever
    reverts to pending-only."""
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    rows = [{"id": "rA", "gym_id": "eng", "account": "instagram", "status": "approved",
             "post_date": "2026-08-13", "format": "feed", "image_url": "https://r2/i.jpg",
             "caption": "hi"}]
    store = FakeStore(rows)

    def fake_zernio(draft, account, scheduled_for=None):
        from agent.zernio_publisher import PublishResult
        return PublishResult(ok=True, mode="published", media_id="zpA")

    out = cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=True,
                          zernio_publish=fake_zernio, catch_all=True)
    assert out["published"] == ["rA"], f"approved row was not published: {out}"


def test_failed_client_publish_reverts_to_approved(monkeypatch):
    """A transient Zernio failure must revert the CLIENT row to 'approved' (not
    'pending'), so the client never has to re-approve."""
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    rows = [{"id": "rB", "gym_id": "eng", "account": "instagram", "status": "approved",
             "post_date": "2026-08-13", "format": "feed", "image_url": "https://r2/i.jpg"}]
    store = FakeStore(rows)

    def boom(draft, account, scheduled_for=None):
        raise RuntimeError("zernio 500")

    out = cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=True,
                          zernio_publish=boom, catch_all=True)
    assert out["failed"] == ["rB"]
    assert store.reverts == [("rB", "approved")]           # NOT pending
    assert store._rows["rB"]["status"] == "approved"       # ready to retry, no re-approve


def test_exactly_once_across_two_ticks(monkeypatch):
    """A published row is never re-published on a second tick (published_at set +
    status published => not claimable)."""
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    rows = [{"id": "rC", "gym_id": "eng", "account": "instagram", "status": "approved",
             "post_date": "2026-08-13", "format": "feed", "image_url": "https://r2/i.jpg"}]
    store = FakeStore(rows)
    calls = []

    def fake_zernio(draft, account, scheduled_for=None):
        calls.append(1)
        from agent.zernio_publisher import PublishResult
        return PublishResult(ok=True, mode="published", media_id="zpC")

    cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=True,
                    zernio_publish=fake_zernio, catch_all=True)
    cap.publish_due("2026-08-13", gym_id="eng", store=store, approved_only=True,
                    zernio_publish=fake_zernio, catch_all=True)   # second tick
    assert calls == [1]                                    # exactly one network call
    assert len(store.published) == 1


def test_publish_client_gyms_self_gates(monkeypatch):
    # all flags off -> no-op, no store touch
    monkeypatch.delenv("AGENT_ZERNIO_PUBLISH", raising=False)
    assert cap.publish_client_gyms("2026-08-13") == []


# ---- stale-'publishing' watchdog (alert-only, never reverts) -------------------
class _SweepStore:
    def __init__(self, rows):
        self._rows = rows

    def publishing_rows(self):
        return list(self._rows)


class _KV(dict):
    def get(self, k, default=""):
        return dict.get(self, k, default)

    def set(self, k, v):
        self[k] = v


def test_sweep_first_sighting_records_never_alerts():
    kv, alerts = _KV(), []
    out = cap.sweep_stuck_publishing(
        store=_SweepStore([{"id": "s1", "gym_id": "eng", "account": "instagram",
                            "post_date": "2026-08-13"}]),
        kv=kv, now="2026-08-13T10:00:00-04:00", alert=alerts.append)
    assert out == [] and alerts == []
    assert kv["stuck_publishing_s1"].startswith("2026-08-13T10:00")


def test_sweep_alerts_once_past_threshold_and_never_reverts():
    kv, alerts = _KV(), []
    kv["stuck_publishing_s1"] = "2026-08-13T07:00:00-04:00"     # first seen 3h ago
    store = _SweepStore([{"id": "s1", "gym_id": "eng", "account": "instagram",
                          "post_date": "2026-08-13"}])
    out = cap.sweep_stuck_publishing(store=store, kv=kv,
                                     now="2026-08-13T10:00:00-04:00",
                                     alert=alerts.append)
    assert out == ["s1"] and len(alerts) == 1
    assert "stuck in 'publishing'" in alerts[0]
    assert "NOT auto-reverted" in alerts[0]                      # human decision only
    assert kv["stuck_publishing_s1"] == "alerted"
    # second pass: no re-alert
    out2 = cap.sweep_stuck_publishing(store=store, kv=kv,
                                      now="2026-08-13T11:00:00-04:00",
                                      alert=alerts.append)
    assert out2 == [] and len(alerts) == 1


def test_sweep_under_threshold_stays_quiet():
    kv, alerts = _KV(), []
    kv["stuck_publishing_s2"] = "2026-08-13T09:30:00-04:00"      # 30 min ago
    out = cap.sweep_stuck_publishing(
        store=_SweepStore([{"id": "s2", "gym_id": "eng", "account": "facebook",
                            "post_date": "2026-08-13"}]),
        kv=kv, now="2026-08-13T10:00:00-04:00", alert=alerts.append)
    assert out == [] and alerts == []
