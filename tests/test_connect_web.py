"""
Facebook connect page tests. Offline (mocked Graph). Asserts: every route 404s
while the flag is OFF; the OAuth URL carries EXACTLY the five scopes; the
callback renders the page list from a mocked Graph response; the IG account
resolves from the mocked Page; the token and app secret NEVER appear in any
rendered HTML or captured log output; the stored token is kv-held and audit
entries are scrubbed.
"""

import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import connect_web, db  # noqa: E402

USER_TOKEN = "EAAuser-token-1234567890"
PAGE_TOKEN = "EAApage-token-0987654321"
APP_SECRET = "app-secret-shhh-123"


class FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeGraph:
    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": params or {}})
        if url.endswith("/oauth/access_token"):
            return FakeResp({"access_token": USER_TOKEN})
        if url.endswith("/me/accounts"):
            return FakeResp({"data": [{"name": "Iron Path Gym", "id": "PAGE1"},
                                      {"name": "Second Gym", "id": "PAGE2"}]})
        if url.endswith("/PAGE1"):
            return FakeResp({"name": "Iron Path Gym", "access_token": PAGE_TOKEN,
                             "instagram_business_account":
                                 {"username": "ironpathgym", "id": "IG9"}})
        return FakeResp({})


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECT_ENABLED", "true")
    monkeypatch.setenv("META_APP_ID", "123456")
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("AGENT_CONNECT_BASE_URL", "https://connect.echo.test")
    connect_web._sessions.clear()


# ---- inert when OFF ---------------------------------------------------------------
def test_all_routes_404_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_CONNECT_ENABLED", raising=False)
    assert connect_web.handle_connect()[0] == 404
    assert connect_web.handle_callback({"code": "x", "state": "y"})[0] == 404
    assert connect_web.handle_select({"state": "y", "page_id": "P"})[0] == 404


# ---- the OAuth URL: exactly the five scopes ------------------------------------------
def test_oauth_url_exact_scopes(monkeypatch):
    _arm(monkeypatch)
    status, html = connect_web.handle_connect()
    assert status == 200
    assert "Login with Facebook" in html
    assert "Connect your gym's Facebook and Instagram" in html
    url = html.split('href="')[1].split('"')[0]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    scopes = qs["scope"][0].split(",")
    assert sorted(scopes) == sorted(["pages_show_list", "pages_read_engagement",
                                     "pages_manage_posts", "instagram_basic",
                                     "instagram_content_publish"])
    assert qs["client_id"] == ["123456"]
    assert qs["redirect_uri"] == ["https://connect.echo.test/connect/callback"]
    assert APP_SECRET not in html                       # secret never echoed


# ---- callback: page list from mocked Graph ------------------------------------------
def _start_flow(monkeypatch):
    _arm(monkeypatch)
    connect_web.handle_connect()
    return next(iter(connect_web._sessions))


def test_callback_renders_page_list(monkeypatch):
    state = _start_flow(monkeypatch)
    graph = FakeGraph()
    status, html = connect_web.handle_callback({"code": "C0DE", "state": state},
                                               http=graph)
    assert status == 200
    assert "Iron Path Gym" in html and "PAGE1" in html
    assert "Second Gym" in html and "PAGE2" in html
    assert 'type="radio"' in html
    # the exchange used the secret in the REQUEST, never in the page
    assert USER_TOKEN not in html and APP_SECRET not in html


def test_bad_state_rejected(monkeypatch):
    _arm(monkeypatch)
    status, _ = connect_web.handle_callback({"code": "C0DE", "state": "forged"},
                                            http=FakeGraph())
    assert status == 400


# ---- select: IG resolution + storage + confirmation -----------------------------------
def test_select_resolves_ig_and_stores_token(monkeypatch, capsys):
    state = _start_flow(monkeypatch)
    graph = FakeGraph()
    connect_web.handle_callback({"code": "C0DE", "state": state}, http=graph)
    status, html = connect_web.handle_select({"state": state, "page_id": "PAGE1"},
                                             http=graph)
    assert status == 200
    assert "Connected" in html
    assert "Iron Path Gym" in html and "@ironpathgym" in html
    assert "approval before anything publishes" in html
    # token stored in kv, never rendered, never logged
    assert db.kv_get("connect_page_token_PAGE1") == PAGE_TOKEN
    meta = json.loads(db.kv_get("connect_page_meta_PAGE1"))
    assert meta["ig_username"] == "ironpathgym"
    printed = capsys.readouterr().out
    for secret in (PAGE_TOKEN, USER_TOKEN, APP_SECRET):
        assert secret not in html
        assert secret not in printed
    # audit entry exists and is scrubbed
    rows = db.audit_rows()
    connect_rows = [r for r in rows if r["kind"] == "connect"]
    assert connect_rows and "ironpathgym" in connect_rows[0]["reason"]
    assert PAGE_TOKEN not in json.dumps(rows)
    # the state nonce is single use
    status2, _ = connect_web.handle_select({"state": state, "page_id": "PAGE1"},
                                           http=graph)
    assert status2 == 400
