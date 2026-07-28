"""
SocialAPI lane: portal connect/status endpoints (token isolation), reporting
honesty, and the route-flip-back guarantee. Offline — HTTP is faked.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, intake_web, intake_tokens, socialapi_store, reporting_live
from agent.accounts import Account, Platform


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, routes=None):
        self.routes = routes or []
        self.calls = []

    def _match(self, method, url):
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                return resp
        return _Resp(200, {})

    def post(self, url, headers=None, json=None, files=None, timeout=None):
        self.calls.append(("POST", url))
        return self._match("POST", url)

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url))
        return self._match("GET", url)


def _sa_account(key):
    return Account(key=key, display_name=f"{key} IG", platform=Platform.INSTAGRAM,
                   token_env=f"AGENT_{key.upper()}_TOKEN",
                   target_id_env=f"AGENT_{key.upper()}_ID",
                   publish_route="socialapi")


# ---- portal: connect -------------------------------------------------------

def test_social_connect_returns_auth_urls(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    monkeypatch.setattr(intake_web, "_is_socialapi_account", lambda k: True)
    socialapi_store.set_brand_id("gyma_ig", "brand_a")
    http = FakeHttp([("POST", "/accounts/connect",
                      _Resp(202, {"auth_url": "https://ig/oauth?x=1"}))])
    status, body = intake_web.handle_portal_social_connect("gyma_ig", http=http)
    assert status == 200
    assert body["brand_id"] == "brand_a"
    assert body["connect"]["instagram"] == "https://ig/oauth?x=1"


def test_social_connect_404_for_meta_account(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr(intake_web, "_is_socialapi_account", lambda k: False)
    status, body = intake_web.handle_portal_social_connect("lasso_ig")
    assert status == 404


def test_social_connect_403_when_portal_off(monkeypatch):
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = intake_web.handle_portal_social_connect("gyma_ig")
    assert status == 403


def test_social_connect_409_without_brand(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    monkeypatch.setattr(intake_web, "_is_socialapi_account", lambda k: True)
    status, body = intake_web.handle_portal_social_connect("nobrand_ig")
    assert status == 409


# ---- portal: status --------------------------------------------------------

def test_social_status_maps_platforms(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    monkeypatch.setattr(intake_web, "_is_socialapi_account", lambda k: True)
    socialapi_store.set_brand_id("gyma_ig", "brand_a")
    http = FakeHttp([("GET", "/accounts", _Resp(200, {"accounts": [
        {"id": "acc_ig", "platform": "instagram", "status": "connected"},
        {"id": "acc_fb", "platform": "facebook", "status": "expired"}]}))])
    status, body = intake_web.handle_portal_social_status("gyma_ig", http=http)
    assert status == 200
    assert body["status"]["instagram"] == "connected"
    assert body["status"]["facebook"] == "expired"
    # a connected account id is remembered for the publisher
    assert socialapi_store.get_account_id("gyma_ig", "instagram") == "acc_ig"


# ---- token isolation: gym A token can never resolve to gym B ---------------

def test_token_isolation(monkeypatch):
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, "shared-secret-value")
    tok_a = intake_tokens.mint("gyma_ig")
    tok_b = intake_tokens.mint("gymb_ig")
    assert intake_web.client_for_token(tok_a) == "gyma_ig"
    assert intake_web.client_for_token(tok_b) == "gymb_ig"
    # A's token never resolves to B, and a forged/foreign token resolves to None
    assert intake_web.client_for_token(tok_a) != "gymb_ig"
    assert intake_web.client_for_token("not-a-real-token-value") is None


def test_connect_isolated_by_token_resolution(monkeypatch):
    """The dispatch resolves token->one account_key; the handler only ever reads
    that account's brand. Two gyms with two brands never cross."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    monkeypatch.setattr(intake_web, "_is_socialapi_account", lambda k: True)
    socialapi_store.set_brand_id("gyma_ig", "brand_a")
    socialapi_store.set_brand_id("gymb_ig", "brand_b")
    http = FakeHttp([("POST", "/accounts/connect", _Resp(202, {"auth_url": "u"}))])
    _, body_a = intake_web.handle_portal_social_connect("gyma_ig", http=http)
    _, body_b = intake_web.handle_portal_social_connect("gymb_ig", http=http)
    assert body_a["brand_id"] == "brand_a"
    assert body_b["brand_id"] == "brand_b"


# ---- route flip back to meta_direct: zero data-model change -----------------

def test_route_flip_zero_datamodel_change():
    from agent.drafter import Draft
    # drafting has NO route-specific field: flipping publish_route changes nothing
    # about how drafts are built or stored.
    assert "publish_route" not in Draft.__dataclass_fields__
    from agent import approvals, meta_publisher, socialapi_publisher
    acct = _sa_account("flip_ig")
    os.environ["AGENT_SOCIALAPI_ENABLED"] = "true"
    try:
        assert approvals._publisher_for(acct) is socialapi_publisher
        # flip back to meta_direct: same object, one field, meta routing restored
        acct.publish_route = "meta_direct"
        assert approvals._publisher_for(acct) is meta_publisher
    finally:
        os.environ.pop("AGENT_SOCIALAPI_ENABLED", None)


# ---- reporting honesty: engagement only, gaps not fake zeros ---------------

def test_reporting_socialapi_engagement_only(monkeypatch):
    monkeypatch.setenv("AGENT_SOCIALAPI_KEY", "k")
    acct = _sa_account("rep_ig")
    today = "2026-07-28"
    # seed a published post row for this account
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, media_id, mode, "
            "published_at) VALUES (?,?,?,?,?,?)",
            ("d1", acct.key, "instagram", "p_1", "published",
             today + "T12:00:00"))
        conn.commit()
    http = FakeHttp([("GET", "/posts/p_1/metrics", _Resp(200, {
        "targets": [{"likes": 10, "comments": 3, "saves": 2, "shares": 1}]}))])
    reporting_live.snapshot_socialapi_account(acct, today, http=http)

    with db.connect() as conn:
        row = dict(conn.execute(
            "SELECT likes, comments, saves, shares, views, reach FROM posts "
            "WHERE media_id='p_1'").fetchone())
        snap = conn.execute(
            "SELECT metrics FROM snapshots WHERE account_key=?",
            (acct.key,)).fetchone()
    assert row["likes"] == 10 and row["comments"] == 3
    assert row["saves"] == 2 and row["shares"] == 1
    # impressions/reach/views are NOT available -> stay NULL (gap, not fake zero)
    assert row["views"] is None and row["reach"] is None
    marker = json.loads(snap["metrics"])
    assert marker["data_source"] == "socialapi"
    assert "reach" not in marker and "views" not in marker and "followers" not in marker
