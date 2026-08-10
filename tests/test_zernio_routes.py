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
        assert set(out["platforms"]) == {"instagram", "facebook"}
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

    def connect_url(self, pid, platform):
        self.calls.append(("connect", pid, platform)); return self._connect

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
    assert ("connect", "lasso_pid_24charxxxxxx", "instagram") in fake.calls
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


def test_page_select_persists_and_requires_id(db_env):
    assert zr.handle_facebook_page_select("gymA", "")[0] == 400
    status, body = zr.handle_facebook_page_select("gymA", "p1")
    assert status == 200 and body == {"ok": True}
    assert (_db.gym_get("gymA") or {}).get("zernio_default_fb_page_id") == "p1"


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
