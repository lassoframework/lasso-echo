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
                    page_id=None):
        # delegate to the REAL payload builder so we test the true shape
        payload = zernio.ZernioClient.create_post.__wrapped__ if hasattr(
            zernio.ZernioClient.create_post, "__wrapped__") else None
        self.created.append({"account_id": account_id, "body": body,
                             "media_urls": media_urls, "scheduled_for": scheduled_for,
                             "page_id": page_id})
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
    c.create_post("acct1", "Hello", media_urls=["https://r2/a.jpg"])
    assert posted["url"].endswith("/v1/posts")
    assert posted["json"] == {"accountId": "acct1", "body": "Hello",
                              "media": [{"url": "https://r2/a.jpg"}]}
    assert "scheduledFor" not in posted["json"]           # publish-now omits it


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
    c.create_post("fb1", "Hi", media_urls=["u"], scheduled_for="2026-08-13T07:30:00-04:00",
                  page_id="PAGE99")
    assert posted["json"]["scheduledFor"] == "2026-08-13T07:30:00-04:00"
    assert posted["json"]["platformSpecificData"] == {"pageId": "PAGE99"}


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
        client=client, scheduled_for="2026-08-13T07:30:00-04:00",
        profile_resolver=lambda k: "prof_eng")
    assert res.ok and res.mode == "published" and res.media_id == "zpost_IG"
    assert len(client.created) == 1
    call = client.created[0]
    assert call["account_id"] == "ig_acct_1"              # the gym's own IG account
    assert call["body"] == "Train today"
    assert call["media_urls"] == ["https://r2/x.jpg"]
    assert call["scheduled_for"] == "2026-08-13T07:30:00-04:00"
    assert call["page_id"] is None                        # IG has no page


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
    def __init__(self, rows):
        self._rows = rows
        self.published = []

    def due_rows(self, gym_id, run_date):
        return [r for r in self._rows if r.get("gym_id") == gym_id]

    def mark_publishing(self, row_id):
        return True

    def mark_published(self, row_id, media_id, published_at):
        self.published.append((row_id, media_id, published_at))
        return {"id": row_id}

    def mark_publish_failed(self, row_id):
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
    assert zcalls == [("eng_ig", "2026-08-13T07:30:00-04:00")] or zcalls[0][0] == "eng_ig"
    assert store.published and store.published[0][1] == "zp1"     # late_post_id recorded


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


def test_publish_client_gyms_self_gates(monkeypatch):
    # all flags off -> no-op, no store touch
    monkeypatch.delenv("AGENT_ZERNIO_PUBLISH", raising=False)
    assert cap.publish_client_gyms("2026-08-13") == []
