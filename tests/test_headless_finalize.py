"""
Zernio headless OAuth finalization (Hill Country MVMT, 2026-08-26).

Echo mints connect URLs with headless=true, but in headless mode Zernio does NOT
create the account after OAuth: it redirects back with tempToken/userProfile/
step/connect_token (GBP: pendingDataToken, step=select_location) and the
integrator must call the selection endpoints. Echo dropped that return leg on
the floor, so every Facebook grant silently created nothing. These tests prove
the fix offline: param parse from a realistic redirect URL, single-page
auto-select, multi-page picker, zero-page honesty, expired-token honesty, the
fb page id landing on the gym row, and the GBP select_location path. Injectable
fakes only — no live calls.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio as z
from agent import zernio_routes as zr
from agent import db as _db


HC_PROFILE = "6a8ed482b1d2b4029ea60df0"
HC_PAGE = {"id": "883355", "name": "Hill Country MVMT"}

# A realistic headless redirect back to the connect page (userProfile URL-encoded JSON).
REDIRECT_URL = (
    "https://echo-intake.example.com/portal/tok_abc123/connect"
    "?tempToken=tt_9f2e1&userProfile=%7B%22id%22%3A%22fbu_77%22%2C%22"
    "username%22%3A%22hillcountrymvmt%22%2C%22displayName%22%3A%22Hill%20"
    "Country%20MVMT%22%7D&step=select_page&connect_token=ct_5b6a7&platform=facebook"
)


class FakeZernio:
    """Records every headless call; returns canned shapes. Never touches a network."""

    def __init__(self, pages=None, locations=None, accounts_after=None,
                 list_error=None, select_error=None):
        self._pages = pages if pages is not None else {"pages": []}
        self._locations = locations if locations is not None else {"locations": []}
        # accounts_after: what list_accounts returns AFTER a select call.
        self._accounts_after = accounts_after if accounts_after is not None \
            else {"accounts": []}
        self._list_error = list_error
        self._select_error = select_error
        self.calls = []
        self._selected = False

    def fb_pages_after_oauth(self, profile_id, temp_token, user_profile=None,
                             connect_token=None):
        self.calls.append(("fb_pages", profile_id, temp_token, user_profile,
                           connect_token))
        if self._list_error:
            raise self._list_error
        return self._pages

    def fb_select_page(self, profile_id, page_id, temp_token, user_profile=None,
                       connect_token=None):
        self.calls.append(("fb_select", profile_id, page_id, temp_token,
                           user_profile, connect_token))
        if self._select_error:
            raise self._select_error
        self._selected = True
        return {"success": True}

    def gbp_locations_after_oauth(self, profile_id, pending_data_token,
                                  connect_token=None):
        self.calls.append(("gbp_locations", profile_id, pending_data_token,
                           connect_token))
        if self._list_error:
            raise self._list_error
        return self._locations

    def gbp_select_location(self, profile_id, location_id, pending_data_token,
                            account_id=None, connect_token=None):
        self.calls.append(("gbp_select", profile_id, location_id,
                           pending_data_token, account_id, connect_token))
        if self._select_error:
            raise self._select_error
        self._selected = True
        return {"success": True}

    def list_accounts(self, pid):
        self.calls.append(("accounts", pid))
        return self._accounts_after if self._selected else {"accounts": []}


@pytest.fixture
def hc_env(tmp_path, monkeypatch):
    """Temp DB + Zernio armed + the Hill Country gym row provisioned."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")
    _db.gym_upsert("hillcountry", display_name="Hill Country MVMT",
                   zernio_profile_id=HC_PROFILE)
    yield


def _fb_body(**over):
    body = {"step": "select_page", "platform": "facebook",
            "tempToken": "tt_9f2e1",
            "userProfile": '{"id":"fbu_77","username":"hillcountrymvmt"}',
            "connect_token": "ct_5b6a7", "pendingDataToken": ""}
    body.update(over)
    return body


# =============================================================================
# 1. param parse from a realistic redirect URL
# =============================================================================
def test_headless_params_from_realistic_redirect_url():
    p = zr.headless_params(REDIRECT_URL)
    assert p["step"] == "select_page"
    assert p["platform"] == "facebook"
    assert p["temp_token"] == "tt_9f2e1"
    assert p["connect_token"] == "ct_5b6a7"
    assert p["pending_data_token"] == ""
    # userProfile decodes to the JSON object the POST select-page body needs
    assert p["user_profile"] == {"id": "fbu_77", "username": "hillcountrymvmt",
                                 "displayName": "Hill Country MVMT"}
    assert '"fbu_77"' in p["user_profile_raw"]


def test_headless_params_from_forwarded_json_body():
    # The connect page forwards URLSearchParams values (already URL-decoded).
    p = zr.headless_params(_fb_body())
    assert p["step"] == "select_page" and p["temp_token"] == "tt_9f2e1"
    assert p["user_profile"] == {"id": "fbu_77", "username": "hillcountrymvmt"}
    # tolerant of a still URL-encoded userProfile (a client forwarding the raw param)
    raw = zr.headless_params(_fb_body(
        userProfile="%7B%22id%22%3A%22fbu_77%22%7D"))
    assert raw["user_profile"] == {"id": "fbu_77"}


# =============================================================================
# 2. single page -> auto-select server-side, no user action
# =============================================================================
def test_single_page_auto_selects_and_finalizes(hc_env):
    fake = FakeZernio(
        pages={"pages": [dict(HC_PAGE)]},
        accounts_after={"accounts": [{"platform": "facebook", "_id": "acct_fb",
                                      "isActive": True}]})
    status, body = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                              client=fake)
    assert status == 200
    assert body["finalized"] is True and body["platform"] == "facebook"
    assert body["selected"] == {"id": "883355", "name": "Hill Country MVMT"}
    # the select call carried the redirect tokens + the DECODED userProfile object
    sel = next(c for c in fake.calls if c[0] == "fb_select")
    assert sel[1] == HC_PROFILE and sel[2] == "883355" and sel[3] == "tt_9f2e1"
    assert sel[4] == {"id": "fbu_77", "username": "hillcountrymvmt"}
    assert sel[5] == "ct_5b6a7"
    # account creation was VERIFIED via list_accounts after the select
    assert ("accounts", HC_PROFILE) in fake.calls


# =============================================================================
# 3. multiple pages -> options for the branded picker (nothing selected yet)
# =============================================================================
def test_multiple_pages_return_options_and_select_nothing(hc_env):
    fake = FakeZernio(pages={"pages": [dict(HC_PAGE),
                                       {"id": "990011", "name": "Second Page"}]})
    status, body = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                              client=fake)
    assert status == 200
    assert body["finalized"] is False
    assert [o["id"] for o in body["options"]] == ["883355", "990011"]
    assert not any(c[0] == "fb_select" for c in fake.calls)
    # nothing stored on the gym row yet
    assert not (_db.gym_get("hillcountry") or {}).get("zernio_default_fb_page_id")


def test_choice_finalizes_that_page_and_rejects_a_foreign_one(hc_env):
    pages = {"pages": [dict(HC_PAGE), {"id": "990011", "name": "Second Page"}]}
    ok_accounts = {"accounts": [{"platform": "facebook", "_id": "acct_fb"}]}
    fake = FakeZernio(pages=pages, accounts_after=ok_accounts)
    status, body = zr.handle_connect_finalize(
        "hillcountry", _fb_body(choice_id="990011"), client=fake)
    assert status == 200 and body["selected"]["id"] == "990011"
    # a choice that is not among the listed pages is refused before any select
    fake2 = FakeZernio(pages=pages, accounts_after=ok_accounts)
    status2, body2 = zr.handle_connect_finalize(
        "hillcountry", _fb_body(choice_id="666"), client=fake2)
    assert status2 == 400
    assert not any(c[0] == "fb_select" for c in fake2.calls)


# =============================================================================
# 4. zero pages -> honest empty options (no page access on that login)
# =============================================================================
def test_zero_pages_returns_empty_options_not_an_error(hc_env):
    fake = FakeZernio(pages={"pages": []})
    status, body = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                              client=fake)
    assert status == 200
    assert body["finalized"] is False and body["options"] == []
    assert not any(c[0] == "fb_select" for c in fake.calls)


# =============================================================================
# 5. expired/used tempToken (Zernio 4xx) -> honest, retryable, never silent
# =============================================================================
def test_expired_temp_token_is_reported_honestly(hc_env):
    fake = FakeZernio(list_error=z.ZernioError(401, "temp token expired"))
    status, body = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                              client=fake)
    assert status == 400
    assert body.get("expired") is True
    fake2 = FakeZernio(pages={"pages": [dict(HC_PAGE)]},
                       select_error=z.ZernioError(400, "temp token already used"))
    status2, body2 = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                                client=fake2)
    assert status2 == 400 and body2.get("expired") is True
    # a Zernio 5xx is a 502, not "expired"
    fake3 = FakeZernio(list_error=z.ZernioError(500, "server error"))
    status3, body3 = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                                client=fake3)
    assert status3 == 502 and "expired" not in body3


# =============================================================================
# 6. successful finalize stores zernio_default_fb_page_id on the gym row
# =============================================================================
def test_fb_page_id_stored_on_gym_row_after_finalize(hc_env):
    fake = FakeZernio(
        pages={"pages": [dict(HC_PAGE)]},
        accounts_after={"accounts": [{"platform": "facebook", "_id": "acct_fb"}]})
    status, _ = zr.handle_connect_finalize("hillcountry", _fb_body(), client=fake)
    assert status == 200
    row = _db.gym_get("hillcountry") or {}
    assert row.get("zernio_default_fb_page_id") == "883355"
    # display name preserved (gym_upsert contract)
    assert row.get("display_name") == "Hill Country MVMT"


def test_no_account_after_select_means_no_store_and_honest_502(hc_env):
    # Zernio 2xx on select but the account row never appears -> the exact silent
    # failure class this flow fixes must NOT be reported as success.
    fake = FakeZernio(pages={"pages": [dict(HC_PAGE)]},
                      accounts_after={"accounts": []})
    status, body = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                              client=fake)
    assert status == 502
    assert not (_db.gym_get("hillcountry") or {}).get("zernio_default_fb_page_id")


# =============================================================================
# 7. Google Business select_location path (pendingDataToken, not tempToken)
# =============================================================================
def _gbp_body(**over):
    body = {"step": "select_location", "platform": "googlebusiness",
            "tempToken": "", "userProfile": "",
            "connect_token": "ct_5b6a7", "pendingDataToken": "pdt_31337"}
    body.update(over)
    return body


def test_gbp_single_location_auto_selects(hc_env):
    fake = FakeZernio(
        locations={"locations": [{"id": "locations/12345",
                                  "title": "Hill Country MVMT",
                                  "accountId": "accounts/987"}]},
        accounts_after={"accounts": [{"platform": "googlebusiness",
                                      "_id": "acct_gbp"}]})
    status, body = zr.handle_connect_finalize("hillcountry", _gbp_body(),
                                              client=fake)
    assert status == 200
    assert body["finalized"] is True and body["platform"] == "googlebusiness"
    assert body["selected"]["id"] == "locations/12345"
    sel = next(c for c in fake.calls if c[0] == "gbp_select")
    # (profile_id, location_id, pending_data_token, account_id, connect_token)
    assert sel[1:] == (HC_PROFILE, "locations/12345", "pdt_31337",
                       "accounts/987", "ct_5b6a7")
    # the list call used the pendingDataToken (which does not consume it)
    lst = next(c for c in fake.calls if c[0] == "gbp_locations")
    assert lst[1:] == (HC_PROFILE, "pdt_31337", "ct_5b6a7")


def test_gbp_multiple_locations_return_options(hc_env):
    fake = FakeZernio(locations={"locations": [
        {"id": "locations/1", "title": "North"},
        {"id": "locations/2", "title": "South"}]})
    status, body = zr.handle_connect_finalize("hillcountry", _gbp_body(),
                                              client=fake)
    assert status == 200 and body["finalized"] is False
    assert [o["name"] for o in body["options"]] == ["North", "South"]
    assert not any(c[0] == "gbp_select" for c in fake.calls)


# =============================================================================
# gates + wiring
# =============================================================================
def test_finalize_dark_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    status, _ = zr.handle_connect_finalize("hillcountry", _fb_body(),
                                           client=FakeZernio())
    assert status == 403


def test_finalize_validates_step_and_tokens(hc_env):
    assert zr.handle_connect_finalize("hillcountry", {"step": "select_board"},
                                      client=FakeZernio())[0] == 400
    assert zr.handle_connect_finalize("hillcountry",
                                      _fb_body(tempToken=""),
                                      client=FakeZernio())[0] == 400
    assert zr.handle_connect_finalize("hillcountry",
                                      _gbp_body(pendingDataToken="",
                                                tempToken=""),
                                      client=FakeZernio())[0] == 400


def test_finalize_requires_a_provisioned_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")
    status, body = zr.handle_connect_finalize("ghostgym", _fb_body(),
                                              client=FakeZernio())
    assert status == 400 and "profile" in body["error"]


def test_map_locations_tolerant_shapes():
    out = z.map_locations({"locations": [
        {"id": "locations/1", "title": "North", "accountId": "accounts/9"},
        {"locationId": "locations/2", "displayName": "South"},
        {"name": "locations/3"},
        {"title": "no id, dropped"},
        "garbage"]})
    assert out["locations"] == [
        {"id": "locations/1", "name": "North", "account_id": "accounts/9"},
        {"id": "locations/2", "name": "South", "account_id": ""},
        {"id": "locations/3", "name": "locations/3", "account_id": ""}]
    assert z.map_locations(None) == {"locations": []}


def test_connect_page_handles_the_return_leg():
    """The connect page must detect the headless redirect params, call the
    finalize endpoint, strip the params, and re-poll status — never a silent
    bounce (the exact Hill Country bug)."""
    from agent.intake_web import CONNECT_PAGE
    assert "/connect/finalize" in CONNECT_PAGE
    assert "tempToken" in CONNECT_PAGE and "pendingDataToken" in CONNECT_PAGE
    assert "history.replaceState" in CONNECT_PAGE
    assert "That link expired" in CONNECT_PAGE
    assert "does not manage any" in CONNECT_PAGE
    # never any vendor branding in front of a client
    assert "zernio" not in CONNECT_PAGE.lower()
    # existing behaviors preserved (test_connection_watch invariants)
    assert 'window.open(url, "_blank"' in CONNECT_PAGE
    assert 'addEventListener("focus"' in CONNECT_PAGE
    assert "setInterval" in CONNECT_PAGE


def test_client_headless_methods_hit_documented_endpoints():
    """The four new client methods call the documented paths with the documented
    params/body and forward connect_token as X-Connect-Token. No token logging."""
    calls = []

    class Resp:
        status_code = 200
        text = ""
        def json(self):
            return {}

    class Http:
        def get(self, url, params=None, headers=None, timeout=None):
            calls.append(("GET", url, params, headers)); return Resp()
        def post(self, url, json=None, headers=None, timeout=None):
            calls.append(("POST", url, json, headers)); return Resp()

    c = z.ZernioClient(api_key="sk", base="https://api.zernio.com", http=Http())
    c.fb_pages_after_oauth("prof1", "tt1", user_profile='{"id":"u"}',
                           connect_token="ct1")
    c.fb_select_page("prof1", "pg1", "tt1", user_profile={"id": "u"},
                     connect_token="ct1")
    c.gbp_locations_after_oauth("prof1", "pdt1", connect_token="ct1")
    c.gbp_select_location("prof1", "locations/5", "pdt1",
                          account_id="accounts/2", connect_token="ct1")

    m, url, params, headers = calls[0]
    assert (m, url.endswith("/v1/connect/facebook/select-page")) == ("GET", True)
    assert params == {"profileId": "prof1", "tempToken": "tt1",
                      "userProfile": '{"id":"u"}'}
    assert headers["X-Connect-Token"] == "ct1"

    m, url, body, headers = calls[1]
    assert (m, url.endswith("/v1/connect/facebook/select-page")) == ("POST", True)
    assert body == {"profileId": "prof1", "pageId": "pg1", "tempToken": "tt1",
                    "userProfile": {"id": "u"}}
    assert headers["X-Connect-Token"] == "ct1"

    m, url, params, headers = calls[2]
    assert url.endswith("/v1/connect/googlebusiness/locations")
    assert params == {"profileId": "prof1", "pendingDataToken": "pdt1"}

    m, url, body, headers = calls[3]
    assert (m, url.endswith("/v1/connect/googlebusiness/select-location")) == ("POST", True)
    assert body == {"profileId": "prof1", "locationId": "locations/5",
                    "pendingDataToken": "pdt1", "accountId": "accounts/2"}


# ---- FINALIZE FIX: the Echo return leg URL (Zanshin/Pete 2026-08-28) -------------
def test_connect_return_url_wraps_portal_dest(monkeypatch):
    """Echo hands Zernio its OWN token-scoped /connect/return (which finalizes the
    account server-side), carrying the portal's real landing inside ?dest=. Without
    this the portal's /my got the redirect and dropped the grant (no /my handshake)."""
    from agent import intake_web
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://echo-intake.example.com")
    url = intake_web._connect_return_url("tok_abc123",
                                         "https://ops.lassoframework.com/my")
    assert url.startswith("https://echo-intake.example.com/portal/tok_abc123/connect/return")
    # the portal landing rides through, url-encoded, as ?dest=
    assert "dest=https%3A%2F%2Fops.lassoframework.com%2Fmy" in url


def test_connect_return_url_omits_bad_dest(monkeypatch):
    from agent import intake_web
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://echo-intake.example.com")
    # a non-http(s) dest is not appended; the return route falls back to the portal /my.
    url = intake_web._connect_return_url("tok_x", "javascript:alert(1)")
    assert url == "https://echo-intake.example.com/portal/tok_x/connect/return"
    # no token -> no url (caller falls back to the portal redirect)
    assert intake_web._connect_return_url("", "https://ops.lassoframework.com/my") == ""


def test_connect_return_route_registered_in_get_handler():
    """The GET dispatcher must recognize /portal/<token>/connect/return so Zernio's
    post-OAuth bounce is finalized server-side, not 404'd."""
    import inspect
    from agent import intake_web
    src = inspect.getsource(intake_web)
    assert "connect/return" in src
    # it drives the SAME finalize the connect page JS uses, then bounces to the portal.
    assert "handle_connect_finalize" in src
    assert "_send_redirect" in src
