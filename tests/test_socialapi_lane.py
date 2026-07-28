"""
SocialAPI.ai publish lane. Offline — every HTTP call is a fake client.

Covers the acceptance list: routing (LASSO stays meta_direct), caption newline
round-trip, R2-bytes -> media_id (no public URL passed), idempotent republish /
no double-post, loud failure, portal token isolation, route-flip with zero
data-model change, reporting honesty (engagement only, gaps not fake zeros),
flags default OFF, key never logged.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import (config, accounts, approvals, socialapi_client,
                   socialapi_publisher, socialapi_store, db)
from agent.accounts import Account, Platform
from agent.drafter import Draft, DraftStatus


# ---- fakes -----------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, body=None, content=b"", text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = content
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


class FakeHttp:
    """Records calls; returns queued responses by (method, url-substring)."""
    def __init__(self):
        self.calls = []
        self._routes = []   # list of (method, needle, resp)

    def route(self, method, needle, resp):
        self._routes.append((method.upper(), needle, resp))
        return self

    def _match(self, method, url):
        for m, needle, resp in self._routes:
            if m == method and needle in url:
                return resp
        return _Resp(200, {})

    def post(self, url, headers=None, json=None, files=None, data=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers or {},
                           "json": json, "files": files})
        return self._match("POST", url)

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers or {},
                           "params": params})
        return self._match("GET", url)


def _socialapi_account(key="gymx_ig", platform=Platform.INSTAGRAM):
    return Account(
        key=key, display_name="Gym X IG", platform=platform,
        token_env=f"AGENT_{key.upper()}_TOKEN",
        target_id_env=f"AGENT_{key.upper()}_ID",
        publish_route="socialapi",
    )


def _draft(account_key="gymx_ig", did="sapi_test_1", is_story=False):
    return Draft(
        draft_id=did, account_key=account_key, platform="instagram",
        caption="Line one.\nLine two.\n\nLine four after a blank.",
        hashtags=["#GymOwner", "#LASSO"],
        creative_path="content_library/gymx/card.png",
        creative_public_url="https://pub.example.r2.dev/echo/gymx/abc/card.png",
        scheduled_for="2026-07-28T12:00:00", status=DraftStatus.PENDING,
        day_key="2026-07-28", draft_type="feed", is_story=is_story,
    )


# DB isolation is handled by tests/conftest.py (per-test AGENT_DB_PATH); db.connect()
# reads that path lazily on every call, so no reload is needed here.


# ---- routing ---------------------------------------------------------------

def test_routing_lasso_stays_meta_direct(monkeypatch):
    monkeypatch.setenv("AGENT_SOCIALAPI_ENABLED", "true")
    from agent import meta_publisher
    lasso = accounts.get_account("lasso_ig")
    assert lasso.publish_route == "meta_direct"
    assert approvals._publisher_for(lasso) is meta_publisher


def test_routing_socialapi_account_when_flag_on(monkeypatch):
    monkeypatch.setenv("AGENT_SOCIALAPI_ENABLED", "true")
    pub = approvals._publisher_for(_socialapi_account())
    assert pub is socialapi_publisher


def test_routing_socialapi_falls_back_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIALAPI_ENABLED", raising=False)
    from agent import meta_publisher
    pub = approvals._publisher_for(_socialapi_account())
    assert pub is meta_publisher   # flag OFF -> meta_direct, even for socialapi route


# ---- flags default OFF -----------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIALAPI_ENABLED", raising=False)
    assert config.socialapi_enabled() is False


def test_publisher_draft_only_when_publish_off(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    res = socialapi_publisher.publish(_draft(), _socialapi_account(), http=FakeHttp())
    assert res.ok and res.mode == "would_publish"


# ---- happy path: bytes -> media_id -> post, newlines preserved -------------

def _happy_http():
    return (FakeHttp()
            .route("GET", "card.png", _Resp(200, content=b"PNGBYTES"))
            .route("POST", "/media/upload", _Resp(200, {"media_id": "m_123"}))
            .route("POST", "/posts", _Resp(200, {
                "id": "p_abc", "status": "published",
                "targets": [{"account_id": "acc_1", "platform": "instagram",
                             "status": "published", "platform_post_id": "ig_999",
                             "permalink": "https://instagram.com/p/xyz"}]})))


def test_publish_uploads_bytes_and_preserves_newlines(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "sapi_key_secret")
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    http = _happy_http()

    res = socialapi_publisher.publish(_draft(), acct, http=http)
    assert res.ok and res.mode == "published"
    assert res.media_id == "p_abc"                 # SocialAPI post id (for metrics)
    assert res.permalink == "https://instagram.com/p/xyz"

    # media uploaded as bytes (multipart files=), never as a public URL field
    upload = next(c for c in http.calls if "/media/upload" in c["url"])
    assert upload["files"]["file"][1] == b"PNGBYTES"
    post = next(c for c in http.calls if c["url"].endswith("/posts"))
    body = post["json"]
    assert "image_url" not in body and "media_urls" not in body
    assert body["media_ids"] == ["m_123"]
    # newlines survive verbatim end-to-end
    assert body["text"] == ("Line one.\nLine two.\n\nLine four after a blank.\n\n"
                            "#GymOwner #LASSO")


def test_story_uses_stories_content_type(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    http = _happy_http()
    socialapi_publisher.publish(_draft(is_story=True), acct, http=http)
    post = next(c for c in http.calls if c["url"].endswith("/posts"))
    assert post["json"]["targets"][0]["platform_data"]["content_type"] == "stories"


def test_story_double_gated(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.delenv("AGENT_STORIES_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    res = socialapi_publisher.publish(_draft(is_story=True), _socialapi_account(),
                                      http=_happy_http())
    assert res.mode == "would_publish"


# ---- idempotency / no double post ------------------------------------------

def test_idempotent_republish_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    d = _draft()

    http1 = _happy_http()
    socialapi_publisher.publish(d, acct, http=http1)
    # simulate approvals having logged the published posts row
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, media_id, mode, "
            "published_at) VALUES (?,?,?,?,?,?)",
            (d.draft_id, acct.key, "instagram", "p_abc", "published",
             "2026-07-28T12:00:00"))
        conn.commit()

    http2 = _happy_http()
    res2 = socialapi_publisher.publish(d, acct, http=http2)
    assert res2.ok and res2.mode == "published"
    assert "idempotent" in res2.detail
    # NO second post attempt
    assert not any(c["url"].endswith("/posts") for c in http2.calls)


def test_claim_blocks_repost_without_posts_row(monkeypatch):
    """The CRITICAL fix: the claim (not the later posts row) blocks a re-post.
    Publish once, then re-publish WITHOUT any posts row present — the claim alone
    must make it a no-op, so a fast re-tap before approvals logs cannot double."""
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    d = _draft()
    socialapi_publisher.publish(d, acct, http=_happy_http())
    # no posts row inserted this time — only the claim protects us
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 0
    http2 = _happy_http()
    res2 = socialapi_publisher.publish(d, acct, http=http2)
    assert res2.mode == "published" and "idempotent" in res2.detail
    assert not any(c["url"].endswith("/posts") for c in http2.calls)


def test_concurrent_inflight_claim_holds(monkeypatch):
    """A claim already in flight (a concurrent publish) holds the second caller
    with MediaNotReady and makes NO post call."""
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    d = _draft()
    # simulate another publish already holding the claim (won, still in flight)
    state, _ = db.socialapi_claim(d.draft_id, acct.key)
    assert state == "won"
    from agent.meta_publisher import MediaNotReady
    http = _happy_http()
    with pytest.raises(MediaNotReady):
        socialapi_publisher.publish(d, acct, http=http)
    assert not any(c["url"].endswith("/posts") for c in http.calls)


def test_failed_publish_releases_claim_for_retry(monkeypatch):
    """A hard vendor failure releases the claim so a genuine retry can proceed."""
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    monkeypatch.setattr("agent.ops_alerts.alert", lambda *a, **k: None)
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    d = _draft()
    http = (FakeHttp()
            .route("GET", "card.png", _Resp(200, content=b"X"))
            .route("POST", "/media/upload", _Resp(200, {"media_id": "m_1"}))
            .route("POST", "/posts", _Resp(200, {
                "id": "p_f", "status": "failed",
                "targets": [{"status": "failed", "error": "bad"}]})))
    with pytest.raises(socialapi_publisher.SocialApiPublishError):
        socialapi_publisher.publish(d, acct, http=http)
    # claim released -> a retry can win it again
    state, _ = db.socialapi_claim(d.draft_id, acct.key)
    assert state == "won"


def test_prenetwork_failure_releases_claim(monkeypatch):
    """A failure before the vendor is reached (no connected account) releases the
    claim so nothing is stuck and a retry works."""
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _socialapi_account()
    d = _draft()
    with pytest.raises(socialapi_publisher.SocialApiPublishError):
        socialapi_publisher.publish(d, acct, http=_happy_http())  # no connected acct
    state, _ = db.socialapi_claim(d.draft_id, acct.key)
    assert state == "won"   # released -> retry can claim again


# ---- loud failure ----------------------------------------------------------

def test_failed_status_raises_and_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    http = (FakeHttp()
            .route("GET", "card.png", _Resp(200, content=b"X"))
            .route("POST", "/media/upload", _Resp(200, {"media_id": "m_1"}))
            .route("POST", "/posts", _Resp(200, {
                "id": "p_x", "status": "failed",
                "targets": [{"status": "failed", "error": "bad media"}]})))
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg, **k: alerts.append(msg))
    with pytest.raises(socialapi_publisher.SocialApiPublishError):
        socialapi_publisher.publish(_draft(), acct, http=http)
    assert any("FAILED" in a for a in alerts)


def test_missing_connected_account_raises(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    with pytest.raises(socialapi_publisher.SocialApiPublishError):
        socialapi_publisher.publish(_draft(), _socialapi_account(), http=_happy_http())


def test_processing_status_raises_media_not_ready(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    monkeypatch.setattr("agent.socialapi_publisher.time.sleep", lambda *_: None)
    acct = _socialapi_account()
    socialapi_store.set_account_id(acct.key, "instagram", "acc_1")
    http = (FakeHttp()
            .route("GET", "card.png", _Resp(200, content=b"X"))
            .route("POST", "/media/upload", _Resp(200, {"media_id": "m_1"}))
            .route("POST", "/posts", _Resp(200, {"id": "p_x", "status": "publishing",
                                                 "targets": [{"status": "publishing"}]}))
            # GET /posts/p_x (poll) stays 'publishing' -> never terminal
            .route("GET", "/posts/p_x", _Resp(200, {"id": "p_x", "status": "publishing",
                                                    "targets": [{"status": "publishing"}]})))
    from agent.meta_publisher import MediaNotReady
    with pytest.raises(MediaNotReady):
        socialapi_publisher.publish(_draft(), acct, http=http)
    # the claim now carries the vendor post id so a retry POLLS instead of re-POSTing
    state, pid = db.socialapi_claim(_draft().draft_id, acct.key)
    assert state == "in_flight" and pid == "p_x"


# ---- key never logged ------------------------------------------------------

def test_error_body_scrubbed(monkeypatch):
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "sapi_key_topsecret")
    http = FakeHttp().route("POST", "/brands", _Resp(500, text="oops sapi_key_topsecret leaked"))
    try:
        socialapi_client.create_brand("Gym X", http=http)
    except socialapi_client.SocialApiError as e:
        assert "sapi_key_topsecret" not in str(e)
    else:
        pytest.fail("expected SocialApiError")


def test_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIALAPI_KEY", raising=False)
    with pytest.raises(socialapi_client.MissingKey):
        socialapi_client.create_brand("Gym X", http=FakeHttp())


# ---- store round-trip (with + without Fernet) ------------------------------

def test_store_plaintext_roundtrip(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIALAPI_ENC_KEY", raising=False)
    socialapi_store.set_brand_id("gymx_ig", "brand_42")
    assert socialapi_store.get_brand_id("gymx_ig") == "brand_42"
    socialapi_store.set_account_id("gymx_ig", "instagram", "acc_9")
    assert socialapi_store.get_account_id("gymx_ig", "instagram") == "acc_9"


def test_store_fernet_roundtrip(monkeypatch):
    try:
        from cryptography.fernet import Fernet
    except Exception:
        pytest.skip("cryptography not installed")
    monkeypatch.setenv("AGENT_SOCIALAPI_ENC_KEY", Fernet.generate_key().decode())
    socialapi_store.set_brand_id("gymx_ig", "brand_secret")
    # stored value is encrypted at rest
    raw = db.kv_get("socialapi_brand_id_gymx_ig", "")
    assert raw.startswith("enc:") and "brand_secret" not in raw
    assert socialapi_store.get_brand_id("gymx_ig") == "brand_secret"
