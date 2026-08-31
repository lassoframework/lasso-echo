"""
Tests for zernio.py (pure mappers) + zernio_routes.py (handlers).

Offline: pure mappers run on real-shape fixtures captured from the live Zernio API; handlers use an
injected fake client + a temp SQLite DB. No network. Invariants:
  1. Endpoints are dark (403) when ZERNIO_API_KEY is absent.
  2. Field renames Echo owns: authUrl->oauth_url, _id->id, metadata.profileData.username->handle.
  3. Expiry is derived (disconnect flag / inactive / connectedAt+expires_in), never fabricated.
  4. Reads never provision a Zernio profile; connect may.
  5. platform is validated to instagram|facebook before any Zernio call.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio as z
from agent import zernio_routes as zr
from agent import db as _db


# ---- real-shape fixture (trimmed from a live /v1/accounts response) ----------
CONNECTED_IG = {
    "_id": "acct_ig", "platform": "instagram", "isActive": True, "enabled": True,
    "connectedAt": "2026-07-29T13:14:04.205Z", "displayName": "LASSO",
    "intentionalDisconnectAt": None,
    "metadata": {"expires_in": 5183999, "profileData": {"username": "lassoframework"}},
}


# =============================================================================
# PURE MAPPERS
# =============================================================================
def test_map_status_connected_ig_with_handle():
    out = z.map_status({"accounts": [CONNECTED_IG]})
    assert out["platforms"]["instagram"] == {"connected": True, "handle": "lassoframework", "expired": False}
    assert out["platforms"]["facebook"] == {"connected": False, "handle": None, "expired": False}


def test_map_status_disconnect_flag_is_expired():
    acct = dict(CONNECTED_IG, intentionalDisconnectAt="2026-07-30T00:00:00Z")
    out = z.map_status({"accounts": [acct]})
    assert out["platforms"]["instagram"]["expired"] is True
    assert out["platforms"]["instagram"]["connected"] is False


def test_map_status_inactive_is_expired():
    acct = dict(CONNECTED_IG, isActive=False)
    assert z.account_state(acct) == "expired"


def test_account_state_time_expiry():
    from datetime import datetime, timezone
    # connectedAt + expires_in(1s) is well in the past relative to now -> expired
    acct = {"platform": "facebook", "isActive": True, "connectedAt": "2020-01-01T00:00:00Z",
            "metadata": {"expires_in": 1}}
    assert z.account_state(acct, now=datetime.now(timezone.utc)) == "expired"


def test_map_status_missing_platform_is_not_connected_no_handle():
    out = z.map_status({"accounts": []})
    assert out["platforms"]["facebook"] == {"connected": False, "handle": None, "expired": False}


def test_map_status_defensive_on_garbage():
    for bad in (None, {}, {"accounts": None}, {"accounts": [42, "x"]}):
        out = z.map_status(bad)
        assert set(out["platforms"]) == {"instagram", "facebook", "googlebusiness"}
        assert out["platforms"]["instagram"]["connected"] is False


def test_map_status_google_business_connected():
    # a connected googlebusiness account is reflected so the connect page shows it linked
    gbp = {"_id": "acct_gbp", "platform": "googlebusiness", "isActive": True,
           "enabled": True, "connectedAt": "2026-08-18T00:00:00.000Z",
           "intentionalDisconnectAt": None,
           "metadata": {"expires_in": 5183999}}
    out = z.map_status({"accounts": [gbp]})
    assert out["platforms"]["googlebusiness"]["connected"] is True
    assert out["platforms"]["instagram"]["connected"] is False


def test_bare_account_without_signal_is_not_connected():
    # A malformed/partial payload with only a platform must NOT read as connected.
    assert z.account_state({"platform": "instagram"}) == "not_connected"
    out = z.map_status({"accounts": [{"platform": "instagram"}]})
    assert out["platforms"]["instagram"] == {"connected": False, "handle": None, "expired": False}


def test_map_pages_renames_underscore_id():
    out = z.map_pages({"pages": [{"_id": "111", "name": "My Gym Page"}, {"_id": "", "name": "drop"}]})
    assert out == {"pages": [{"id": "111", "name": "My Gym Page"}]}


def test_facebook_account_id_picks_fb():
    accts = {"accounts": [CONNECTED_IG, {"_id": "fb1", "platform": "facebook"}]}
    assert z.facebook_account_id(accts) == "fb1"
    assert z.facebook_account_id({"accounts": [CONNECTED_IG]}) is None


# ---- profile find-by-name (client method over a fake http GET) ---------------
class _FakeHttp:
    """Minimal requests-like stub: records the GET, returns a canned /v1/profiles body."""
    def __init__(self, profiles):
        self._profiles = profiles
        self.last = None

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    def get(self, url, params=None, headers=None, timeout=None):
        self.last = (url, params)
        return self._Resp({"profiles": self._profiles, "total": len(self._profiles)})


def test_find_profile_id_exact_match():
    # Real-shape /v1/profiles list (matches the live 2026-08-06 probe: _id 24-char, name).
    http = _FakeHttp([
        {"_id": "6a69fc000000000000000001", "name": "Default"},
        {"_id": "6a721f000000000000000002", "name": "district"},
        {"_id": "6a74a3000000000000000003", "name": "lasso"},
    ])
    c = z.ZernioClient(api_key="sk", base="https://api.zernio.com", http=http)
    assert c.find_profile_id("lasso") == "6a74a3000000000000000003"
    # hits the profiles endpoint
    assert http.last[0].endswith("/v1/profiles")


def test_find_profile_id_case_insensitive_and_missing():
    http = _FakeHttp([{"_id": "id1", "name": "Lasso"}])
    c = z.ZernioClient(api_key="sk", base="https://api.zernio.com", http=http)
    assert c.find_profile_id("lasso") == "id1"     # case-insensitive fallback
    assert c.find_profile_id("nope") is None


def test_match_profile_id_pure():
    profiles = [
        {"_id": "id_human", "name": "Zanshin Fitness"},
        {"_id": "id_key", "name": "zanshinfitness630e22"},
    ]
    assert z.match_profile_id(profiles, "Zanshin Fitness") == "id_human"
    assert z.match_profile_id(profiles, "zanshin fitness") == "id_human"   # case-insensitive
    assert z.match_profile_id(profiles, "zanshinfitness630e22") == "id_key"
    assert z.match_profile_id(profiles, "nope") is None
    assert z.match_profile_id(profiles, "") is None
    assert z.match_profile_id([], "x") is None


def test_find_profile_id_any_prefers_earlier_alias_and_falls_back():
    # Two profiles exist; the account_key does NOT match, but the display name does. Trying aliases in
    # order (account_key, display_name, ...) must find the human-named profile rather than miss.
    http = _FakeHttp([
        {"_id": "id_human", "name": "Zanshin Fitness"},
        {"_id": "id_other", "name": "Some Other Gym"},
    ])
    c = z.ZernioClient(api_key="sk", base="https://api.zernio.com", http=http)
    # account_key misses, display_name hits -> returns the real profile
    assert c.find_profile_id_any("zanshinfitness630e22", "Zanshin Fitness") == "id_human"
    # earlier exact alias wins when both match
    http2 = _FakeHttp([
        {"_id": "id_key", "name": "zanshinfitness630e22"},
        {"_id": "id_human", "name": "Zanshin Fitness"},
    ])
    c2 = z.ZernioClient(api_key="sk", base="https://api.zernio.com", http=http2)
    assert c2.find_profile_id_any("zanshinfitness630e22", "Zanshin Fitness") == "id_key"
    # no alias matches -> None (so the caller creates a fresh profile)
    assert c.find_profile_id_any("nope", "also_nope", "") is None


# =============================================================================
# HANDLERS
# =============================================================================
class _FakeClient:
    def __init__(self, accounts=None, connect=None, pages=None, profile=None,
                 existing_profiles=None, create_conflict=False):
        self._accounts = accounts if accounts is not None else {"accounts": []}
        self._connect = connect or {"authUrl": "https://www.instagram.com/oauth/authorize?x=1"}
        self._pages = pages or {"pages": []}
        self._profile = profile or {"_id": "new_profile"}
        # existing_profiles: list of {_id,name} the Zernio account already holds.
        self._existing = list(existing_profiles or [])
        # create_conflict: when True, create_profile raises Zernio 409 (name already exists).
        self._create_conflict = create_conflict
        self.calls = []

    def connect_url(self, pid, platform, redirect_url=None, headless=True):
        self.calls.append(("connect", pid, platform, redirect_url, headless))
        return self._connect

    def list_accounts(self, pid):
        self.calls.append(("accounts", pid)); return self._accounts

    def list_facebook_pages(self, aid):
        self.calls.append(("pages", aid)); return self._pages

    def list_profiles(self):
        self.calls.append(("list_profiles",))
        return {"profiles": list(self._existing), "total": len(self._existing)}

    def find_profile_id(self, name):
        # Delegate to the real client's pure matcher over our fake list response.
        return z.ZernioClient.find_profile_id(self, name)

    def find_profile_id_any(self, *names):
        # Delegate to the real multi-alias matcher over our fake list response.
        return z.ZernioClient.find_profile_id_any(self, *names)

    def create_profile(self, name):
        self.calls.append(("create", name))
        if self._create_conflict:
            # Simulate the profile appearing (as if a concurrent/prior create won the race).
            self._existing.append({"_id": "found_after_409", "name": name})
            raise z.ZernioError(409, 'A profile with this name already exists ... profile_name_conflict')
        return self._profile


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")
    yield


def test_endpoints_dark_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    for status, _ in (
        zr.handle_social_status("gymA"),
        zr.handle_social_connect("gymA", "instagram"),
        zr.handle_facebook_pages("gymA"),
        zr.handle_facebook_page_select("gymA", "111"),
    ):
        assert status == 403


def test_connect_validates_platform(db_env):
    status, body = zr.handle_social_connect("gymA", "twitter", client=_FakeClient())
    assert status == 400
    assert "platform" in body["error"]


def test_connect_allows_google_business(db_env):
    # Google Business connects through the same profile + connect_url path (Dale/ENG).
    fake = _FakeClient(connect={"authUrl": "https://accounts.google.com/o/oauth2/auth?x=1"})
    status, body = zr.handle_social_connect("gymA", "googlebusiness", client=fake)
    assert status == 200
    assert body["oauth_url"].startswith("https://accounts.google.com/")
    assert any(c[:3] == ("connect", "new_profile", "googlebusiness") for c in fake.calls)


def test_connect_url_for_google_business(db_env):
    fake = _FakeClient(connect={"authUrl": "https://accounts.google.com/o/oauth2/auth?x=2"})
    ok, url = zr.connect_url_for("gymA", "googlebusiness", client=fake)
    assert ok is True and url.startswith("https://accounts.google.com/")


def _connect_call(fake):
    """The single ('connect', pid, platform, redirect_url, headless) call recorded."""
    return next(c for c in fake.calls if c[0] == "connect")


def test_connect_passes_portal_redirect_and_headless(db_env, monkeypatch):
    # The portal passes its own return URL; Echo must thread it (and headless=true) to Zernio so
    # the gym owner lands back in the LASSO portal, never the Zernio dashboard.
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient()
    ret = "https://ops.lassoframework.com/my"
    status, body = zr.handle_social_connect("gymA", "instagram", client=fake, redirect_url=ret)
    assert status == 200
    call = _connect_call(fake)
    assert call[3] == ret          # redirect_url threaded through unchanged
    assert call[4] is True         # headless


def test_connect_falls_back_to_portal_origin_never_zernio(db_env, monkeypatch):
    # No redirect_url from the portal -> fall back to the configured portal origin + /my.
    # The redirect is NEVER omitted, so Zernio can never default to its own dashboard.
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient()
    status, body = zr.handle_social_connect("gymA", "instagram", client=fake)
    assert status == 200
    call = _connect_call(fake)
    assert call[3] == "https://ops.lassoframework.com/my"
    assert call[3] and "zernio" not in call[3].lower()
    assert call[4] is True


def test_connect_rejects_non_http_redirect_falls_back(db_env, monkeypatch):
    # A non-http(s) redirect_url (junk/injection) is not trusted; falls back to the portal origin.
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient()
    status, body = zr.handle_social_connect(
        "gymA", "instagram", client=fake, redirect_url="javascript:alert(1)")
    assert status == 200
    call = _connect_call(fake)
    assert call[3] == "https://ops.lassoframework.com/my"


def test_facebook_connect_uses_echo_return_url_not_portal(db_env, monkeypatch):
    # FINALIZE FIX (Zanshin/Pete 2026-08-28): the portal's /my has no headless handshake, so
    # Facebook/Google MUST come back through Echo's own /connect/return (which finalizes the
    # account server-side). When intake_web supplies an echo_return_url, THAT is what Echo hands
    # Zernio for FB/GBP — never the raw portal url that would drop the grant.
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient(connect={"authUrl": "https://www.facebook.com/v24.0/dialog/oauth?x=1"})
    echo_ret = ("https://echo.example/portal/tok123/connect/return"
                "?dest=https%3A%2F%2Fops.lassoframework.com%2Fmy")
    status, _body = zr.handle_social_connect(
        "gymA", "facebook", client=fake,
        redirect_url="https://ops.lassoframework.com/my", echo_return_url=echo_ret)
    assert status == 200
    call = _connect_call(fake)
    assert call[3] == echo_ret     # Echo's finalize return leg, NOT the raw portal /my
    assert "/connect/return" in call[3]


def test_googlebusiness_connect_uses_echo_return_url(db_env, monkeypatch):
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient(connect={"authUrl": "https://accounts.google.com/o/oauth2/auth?x=1"})
    echo_ret = "https://echo.example/portal/tok123/connect/return?dest=https%3A%2F%2Fx%2Fmy"
    status, _body = zr.handle_social_connect(
        "gymA", "googlebusiness", client=fake,
        redirect_url="https://ops.lassoframework.com/my", echo_return_url=echo_ret)
    assert status == 200
    assert _connect_call(fake)[3] == echo_ret


def test_instagram_ignores_echo_return_url(db_env, monkeypatch):
    # Instagram is NOT headless (no page/location select), so it lands the account directly and
    # keeps the portal's own redirect. The Echo finalize return leg would be wrong for it.
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient()
    status, _body = zr.handle_social_connect(
        "gymA", "instagram", client=fake,
        redirect_url="https://ops.lassoframework.com/my",
        echo_return_url="https://echo.example/portal/tok/connect/return")
    assert status == 200
    call = _connect_call(fake)
    assert call[3] == "https://ops.lassoframework.com/my"   # portal redirect, not Echo's return


def test_portal_dest_url_allowlists_only_portal_origin(db_env, monkeypatch):
    # The ?dest= that rides the return leg is validated by the SAME allowlist as the connect
    # redirect: only the portal / intake-web origins are honored, so it can never be an open
    # redirect. Off-origin junk falls back to the configured portal /my.
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    assert zr.portal_dest_url("https://ops.lassoframework.com/my/social") \
        == "https://ops.lassoframework.com/my/social"
    assert zr.portal_dest_url("https://evil.example/steal") \
        == "https://ops.lassoframework.com/my"
    assert zr.portal_dest_url("") == "https://ops.lassoframework.com/my"


def test_client_connect_url_includes_headless_and_redirect(db_env):
    # Verify the actual Zernio GET params carry headless=true + redirect_url (the reproduced bug
    # was Echo omitting redirect_url so Zernio defaulted to its dashboard).
    captured = {}

    class _CapHttp:
        def get(self, url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params

            class _R:
                status_code = 200
                text = ""

                @staticmethod
                def json():
                    return {"authUrl": "https://www.instagram.com/oauth/authorize?x=1"}
            return _R()

    c = z.ZernioClient(api_key="sk_test", base="https://api.zernio.com", http=_CapHttp())
    c.connect_url("pid123", "instagram", redirect_url="https://ops.lassoframework.com/my")
    assert captured["params"]["profileId"] == "pid123"
    assert captured["params"]["headless"] == "true"
    assert captured["params"]["redirect_url"] == "https://ops.lassoframework.com/my"
    assert captured["url"].endswith("/v1/connect/instagram")


def test_connect_url_for_rejects_bad_platform(db_env):
    ok, err = zr.connect_url_for("gymA", "tiktok", client=_FakeClient())
    assert ok is False and "platform must be" in err


def test_connect_url_for_rejects_whitespace_key(db_env):
    """A mis-quoted CLI arg ("gym instagram" as one value) must never reach
    _ensure_profile_id, which would find-or-CREATE a junk Zernio profile."""
    fake = _FakeClient()
    for bad in ("gymA instagram", " gymA", "gymA\t"):
        ok, err = zr.connect_url_for(bad, "instagram", client=fake)
        assert ok is False and "malformed account_key" in err
    assert not getattr(fake, "created_profiles", []), "no profile may be minted"


def test_connect_url_for_dark_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    ok, info = zr.connect_url_for("gymA", "googlebusiness", client=_FakeClient())
    assert ok is False and "disabled" in info


def test_connect_creates_when_no_profile_exists(db_env):
    # No stored binding AND no existing Zernio profile of this name -> create one.
    fake = _FakeClient(connect={"authUrl": "https://www.facebook.com/v24.0/dialog/oauth?x=1"})
    status, body = zr.handle_social_connect("gymA", "facebook", client=fake)
    assert status == 200
    assert body["oauth_url"].startswith("https://www.facebook.com/")
    # tried find-by-name first, found none, then created and stored it
    assert ("list_profiles",) in fake.calls
    assert ("create", "gymA") in fake.calls
    assert (_db.gym_get("gymA") or {}).get("zernio_profile_id") == "new_profile"


def test_connect_reuses_existing_profile_no_create(db_env):
    # LASSO already provisioned in Zernio: reuse the existing profile, never create (would 409).
    fake = _FakeClient(
        connect={"authUrl": "https://www.instagram.com/oauth/authorize?x=1"},
        existing_profiles=[{"_id": "lasso_pid_24charxxxxxx", "name": "lasso"}],
    )
    status, body = zr.handle_social_connect("lasso", "instagram", client=fake)
    assert status == 200
    assert body["oauth_url"].startswith("https://www.instagram.com/")
    # found by name, reused its _id, and NEVER created a duplicate
    assert ("list_profiles",) in fake.calls
    assert not any(c[0] == "create" for c in fake.calls)
    assert any(c[:3] == ("connect", "lasso_pid_24charxxxxxx", "instagram") for c in fake.calls)
    assert (_db.gym_get("lasso") or {}).get("zernio_profile_id") == "lasso_pid_24charxxxxxx"


def test_connect_create_409_falls_back_to_find(db_env):
    # Belt-and-suspenders: find missed (e.g. a race), create 409s, re-find resolves the profile.
    fake = _FakeClient(
        connect={"authUrl": "https://www.instagram.com/oauth/authorize?x=1"},
        existing_profiles=[],       # find returns nothing on the first pass
        create_conflict=True,       # create raises Zernio 409, then the profile "appears"
    )
    status, body = zr.handle_social_connect("gymB", "instagram", client=fake)
    assert status == 200
    assert body["oauth_url"].startswith("https://www.instagram.com/")
    # create was attempted, 409'd, then a second find resolved the id
    assert ("create", "gymB") in fake.calls
    assert fake.calls.count(("list_profiles",)) >= 2
    assert (_db.gym_get("gymB") or {}).get("zernio_profile_id") == "found_after_409"


def test_connect_reuses_profile_named_by_human_name_not_account_key(db_env):
    # ZANSHIN / PETE 2026-08-27: the gym's Zernio profile was pre-created by ops under a HUMAN name
    # ("Zanshin Fitness") while the account_key is "zanshinfitness630e22". Echo used to look up ONLY by
    # account_key, miss, and CREATE a duplicate empty profile — connections then strand on the wrong
    # one. Now find_profile_id_any tries the display_name too and REUSES the real (populated) profile.
    _db.gym_upsert("zanshinfitness630e22", display_name="Zanshin Fitness")
    fake = _FakeClient(
        connect={"authUrl": "https://www.facebook.com/v24.0/dialog/oauth?x=1"},
        existing_profiles=[{"_id": "zanshin_real_pid_24char", "name": "Zanshin Fitness"}],
    )
    status, body = zr.handle_social_connect("zanshinfitness630e22", "facebook", client=fake)
    assert status == 200
    # matched by display_name, reused the real _id, and NEVER created a duplicate under the account_key
    assert not any(c[0] == "create" for c in fake.calls)
    assert any(c[:3] == ("connect", "zanshin_real_pid_24char", "facebook") for c in fake.calls)
    assert (_db.gym_get("zanshinfitness630e22") or {}).get("zernio_profile_id") == "zanshin_real_pid_24char"


def test_connect_still_creates_when_no_alias_matches(db_env):
    # No profile matches ANY alias (account_key, display_name, gym_name) -> create one, named by the
    # first non-empty of gym_name/display_name/account_key. Guards against find_profile_id_any never
    # creating (which would strand a genuinely new gym).
    _db.gym_upsert("brandnewgym", display_name="Brand New Gym")
    fake = _FakeClient(
        connect={"authUrl": "https://www.instagram.com/oauth/authorize?x=1"},
        existing_profiles=[{"_id": "someone_else_pid", "name": "A Different Gym"}],
    )
    status, body = zr.handle_social_connect("brandnewgym", "instagram", client=fake)
    assert status == 200
    # created under the display name (first non-empty name), reused nothing foreign
    assert ("create", "Brand New Gym") in fake.calls
    assert (_db.gym_get("brandnewgym") or {}).get("zernio_profile_id") == "new_profile"


def test_status_not_provisioned_is_all_not_connected_no_client_call(db_env):
    status, body = zr.handle_social_status("gymA")  # no client -> must not call out
    assert status == 200
    assert body["platforms"]["instagram"]["connected"] is False
    assert body["platforms"]["facebook"]["connected"] is False


def test_status_folds_live_shape(db_env):
    _db.gym_upsert("gymA", zernio_profile_id="P1")
    fake = _FakeClient(accounts={"accounts": [CONNECTED_IG]})
    status, body = zr.handle_social_status("gymA", client=fake)
    assert status == 200
    assert body["platforms"]["instagram"] == {"connected": True, "handle": "lassoframework", "expired": False}
    assert ("accounts", "P1") in fake.calls


def test_facebook_pages_empty_when_no_fb_account(db_env):
    _db.gym_upsert("gymA", zernio_profile_id="P1")
    fake = _FakeClient(accounts={"accounts": [CONNECTED_IG]})
    status, body = zr.handle_facebook_pages("gymA", client=fake)
    assert status == 200
    assert body == {"pages": []}


def test_facebook_pages_maps_ids(db_env):
    _db.gym_upsert("gymA", zernio_profile_id="P1")
    fake = _FakeClient(
        accounts={"accounts": [{"_id": "fb1", "platform": "facebook", "isActive": True,
                                "connectedAt": "2026-07-29T13:14:04Z", "metadata": {"expires_in": 999999}}]},
        pages={"pages": [{"_id": "p1", "name": "Reverb Gym"}]},
    )
    status, body = zr.handle_facebook_pages("gymA", client=fake)
    assert status == 200
    assert body == {"pages": [{"id": "p1", "name": "Reverb Gym"}]}


def _fb_fake_with_pages():
    return _FakeClient(
        accounts={"accounts": [{"_id": "fb1", "platform": "facebook",
                                "isActive": True,
                                "connectedAt": "2026-07-29T13:14:04Z",
                                "metadata": {"expires_in": 999999}}]},
        pages={"pages": [{"_id": "p1", "name": "Reverb Gym"}]},
    )


def test_page_select_persists_owned_page(db_env):
    _db.gym_upsert("gymA", zernio_profile_id="P1")
    assert zr.handle_facebook_page_select("gymA", "",
                                          client=_fb_fake_with_pages())[0] == 400
    status, body = zr.handle_facebook_page_select("gymA", "p1",
                                                  client=_fb_fake_with_pages())
    assert status == 200 and body == {"ok": True}
    assert (_db.gym_get("gymA") or {}).get("zernio_default_fb_page_id") == "p1"


def test_page_select_refuses_foreign_page(db_env):
    # OWNERSHIP: a page id the gym's own FB account does not manage is refused and
    # never persisted (was the silent wrong-page/publish-failure trap).
    _db.gym_upsert("gymA", zernio_profile_id="P1")
    status, body = zr.handle_facebook_page_select("gymA", "someone_elses_page",
                                                  client=_fb_fake_with_pages())
    assert status == 400
    assert "does not belong" in body["error"]
    assert not (_db.gym_get("gymA") or {}).get("zernio_default_fb_page_id")


def test_page_select_requires_connected_facebook(db_env):
    _db.gym_upsert("gymB", zernio_profile_id="P2")
    fake = _FakeClient(accounts={"accounts": [CONNECTED_IG]})   # IG only, no FB
    status, body = zr.handle_facebook_page_select("gymB", "p1", client=fake)
    assert status == 400
    assert "no Facebook account" in body["error"]


def test_account_state_present_row_no_positive_signal_is_connected():
    # A real Zernio account ROW (has _id) with NO isActive/connectedAt/profileData must be CONNECTED,
    # not flapped to not_connected. This is the Instagram reconnect-every-session bug: Zernio's list
    # momentarily omits the signal fields on a live connection.
    import agent.zernio as z
    acct = {"_id": "abc123", "platform": "instagram"}
    assert z.account_state(acct) == "connected"


def test_account_state_bare_payload_is_not_connected():
    # A bare/malformed payload with no account id is never optimistically connected.
    import agent.zernio as z
    assert z.account_state({}) == "not_connected"
    assert z.account_state({"platform": "instagram"}) == "not_connected"


def test_account_state_explicit_negatives_still_win_over_present_row():
    import agent.zernio as z
    assert z.account_state({"_id": "x", "platform": "facebook", "intentionalDisconnectAt": "2026-01-01T00:00:00Z"}) == "expired"
    assert z.account_state({"_id": "x", "platform": "facebook", "isActive": False}) == "expired"


# ---- provision_gym: the GBP-first ops path (find-or-create the Zernio profile) --------

def test_provision_gym_creates_when_no_profile(db_env):
    # ENG-style GBP-first gym with no Zernio profile yet -> create one, persist the id.
    fake = _FakeClient()
    ok, pid = zr.provision_gym("eng", client=fake)
    assert ok is True
    assert pid == "new_profile"
    assert ("create", "eng") in fake.calls
    assert (_db.gym_get("eng") or {}).get("zernio_profile_id") == "new_profile"


def test_provision_gym_reuses_existing_no_create(db_env):
    # already provisioned in Zernio -> reuse by name, never create (a create would 409).
    fake = _FakeClient(existing_profiles=[{"_id": "eng_pid", "name": "eng"}])
    ok, pid = zr.provision_gym("eng", client=fake)
    assert ok is True and pid == "eng_pid"
    assert ("create", "eng") not in fake.calls


def test_provision_gym_dark_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    ok, info = zr.provision_gym("eng", client=_FakeClient())
    assert ok is False
    assert "disabled" in info


def test_connect_rejects_untrusted_redirect_origin(db_env, monkeypatch):
    """OPEN-REDIRECT FIX (audit 2026-08-25): a redirect_url on a NON-allowlisted origin
    (e.g. a phishing lookalike) is never threaded to Zernio — the flow falls back to the
    portal origin, so a real OAuth approval can never land the owner on an attacker page."""
    monkeypatch.setenv("PORTAL_PUBLIC_BASE_URL", "https://ops.lassoframework.com")
    fake = _FakeClient()
    status, _body = zr.handle_social_connect(
        "gymA", "instagram", client=fake,
        redirect_url="https://lasso-portal.evil.example/finish-setup")
    assert status == 200
    call = _connect_call(fake)
    assert call[3] == "https://ops.lassoframework.com/my"   # fell back, never the attacker
    # allowlisted portal PATHS still thread through unchanged
    fake2 = _FakeClient()
    zr.handle_social_connect("gymA", "instagram", client=fake2,
                             redirect_url="https://ops.lassoframework.com/my/social")
    assert _connect_call(fake2)[3] == "https://ops.lassoframework.com/my/social"
