"""
THE CONNECTION-STATUS BUG (gritx / hillcountry, 2026-08-28) and its A+ fix.

Root cause proven live: the status route runs on echo-intake-web, which has NO /data
volume, so the local SQLite echo.db is empty and _resolve_profile_id (db-only) returned
None — status answered not_connected for EVERY platform even though Zernio held the live
account. The 6h portal cron then re-wrote that false not_connected into
echo_social_connections and cleared ever_connected, defeating reconcileWithPriorConnection.

These tests prove the fix offline (injected fakes, no network, no volume):
  1. STATUS returns connected when Zernio has the account even with NO locally-stored id
     (the exact bug) — via a read-only find-by-name that never mutates Zernio.
  2. The Supabase-persisted id is read by the resolver (both planes agree).
  3. A null IG/FB username keeps a live connection connected (handle falls back, not None).
  4. A dead/expired token reads not_connected/expired, never connected.
  5. The re-verify sweep overwrites a poisoned row and repairs ever_connected.
  6. A read (status) NEVER provisions or creates anything on Zernio.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio as z
from agent import zernio_routes as zr
from agent import zernio_reverify as rv
from agent import db as _db
from agent import portal_calendar_store as pcs


GRITX_PROFILE = "6b1c2d3e4f5061728394a0b1"


# ---------------------------------------------------------------------------
# A Zernio client fake that records EVERY call, so we can assert reads never
# provision (no create_profile / no connect_url / no select on a status read).
# ---------------------------------------------------------------------------
class FakeZernio:
    def __init__(self, profiles=None, accounts=None):
        # profiles: list of {_id,name}; accounts: {accounts:[...]} for list_accounts
        self._profiles = profiles if profiles is not None else []
        self._accounts = accounts if accounts is not None else {"accounts": []}
        self.calls = []

    def list_profiles(self):
        self.calls.append(("list_profiles",))
        return {"profiles": list(self._profiles)}

    def find_profile_id(self, name):
        self.calls.append(("find_profile_id", name))
        return z.match_profile_id(self._profiles, name)

    def find_profile_id_any(self, *names):
        self.calls.append(("find_profile_id_any", names))
        for n in names:
            pid = z.match_profile_id(self._profiles, n)
            if pid:
                return pid
        return None

    def list_accounts(self, pid):
        self.calls.append(("list_accounts", pid))
        return self._accounts

    # These must NEVER be called on a status read.
    def create_profile(self, name):
        self.calls.append(("create_profile", name))
        raise AssertionError("create_profile called on a read path")

    def connect_url(self, *a, **k):
        self.calls.append(("connect_url", a, k))
        raise AssertionError("connect_url called on a read path")


@pytest.fixture
def local_only_env(tmp_path, monkeypatch):
    """Zernio armed; a temp (empty) local db standing in for the volume-less service;
    Supabase creds ABSENT so the resolver must fall through to find-by-name."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    yield


# ===========================================================================
# 1. THE BUG: status connected when Zernio has the account with NO stored id
# ===========================================================================
def test_status_connected_via_find_by_name_with_no_stored_id(local_only_env):
    # The local db knows the gym's display name but has NO zernio_profile_id (the
    # volume-less service reality). Zernio holds the profile under that name + a live IG/FB.
    _db.gym_upsert("gritx", display_name="Gritx", gym_name="Gritx")
    fake = FakeZernio(
        profiles=[{"_id": GRITX_PROFILE, "name": "Gritx"}],
        accounts={"accounts": [
            {"platform": "instagram", "_id": "ig1",
             "metadata": {"profileData": {"username": "gritx"}}},
            {"platform": "facebook", "_id": "fb1", "displayName": "Gritx Gym"},
        ]},
    )
    status, body = zr.handle_social_status("gritx", client=fake)
    assert status == 200
    assert body["platforms"]["instagram"]["connected"] is True
    assert body["platforms"]["facebook"]["connected"] is True
    # A READ must never provision: no create_profile / connect_url in the call log.
    assert not any(c[0] in ("create_profile", "connect_url") for c in fake.calls)


def test_status_not_connected_when_truly_unfindable(local_only_env):
    _db.gym_upsert("ghostgym", display_name="Ghost Gym")
    fake = FakeZernio(profiles=[{"_id": "other", "name": "Someone Else"}])
    status, body = zr.handle_social_status("ghostgym", client=fake)
    assert status == 200
    # Honest not-connected, never fabricated.
    assert body["platforms"]["instagram"]["connected"] is False
    assert body["platforms"]["facebook"]["connected"] is False


def test_find_by_name_resolution_is_persisted_locally(local_only_env):
    _db.gym_upsert("gritx", display_name="Gritx")
    fake = FakeZernio(
        profiles=[{"_id": GRITX_PROFILE, "name": "Gritx"}],
        accounts={"accounts": [{"platform": "instagram", "_id": "ig1",
                                "metadata": {"profileData": {"username": "gritx"}}}]},
    )
    zr.handle_social_status("gritx", client=fake)
    row = _db.gym_get("gritx")
    assert (row or {}).get("zernio_profile_id") == GRITX_PROFILE


# ===========================================================================
# 2. the SHARED plane id is read by the resolver (both planes)
# ===========================================================================
class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload

    @property
    def text(self):
        return ""


# The REAL echo_social_connections columns (mirrors the live DB). A write to any other
# column is rejected 400 here exactly as PostgREST would — this is what catches a phantom
# column (e.g. the ever_connected regression) in tests instead of only in production.
_ESC_COLS = {"id", "gym_id", "platform", "state", "handle",
             "first_connected_at", "last_verified_at", "updated_at"}


class _RoutingHttp:
    """A PostgREST stand-in that answers by table + filter, backing a tiny in-memory
    'gyms' and 'echo_gym_settings' and 'echo_social_connections'. Records writes and
    rejects writes to columns that do not exist on echo_social_connections."""

    def __init__(self, gyms=None, settings=None, conns=None):
        self.gyms = gyms or {}          # slug -> uuid
        self.settings = settings or {}  # uuid -> {col: val}
        self.conns = conns or {}        # (uuid, platform) -> row
        self.writes = []

    def _table(self, url):
        return url.rstrip("/").split("/rest/v1/")[-1]

    def get(self, url, params=None, headers=None, timeout=None):
        t = self._table(url)
        params = params or {}
        if t == "gyms":
            slug = (params.get("slug") or "").replace("eq.", "")
            uuid = self.gyms.get(slug)
            return _Resp(200, [{"id": uuid}] if uuid else [])
        if t == "echo_gym_settings":
            uuid = (params.get("gym_id") or "").replace("eq.", "")
            row = self.settings.get(uuid)
            return _Resp(200, [row] if row else [])
        if t == "echo_social_connections":
            uuid = (params.get("gym_id") or "").replace("eq.", "")
            plat = (params.get("platform") or "").replace("eq.", "")
            row = self.conns.get((uuid, plat))
            return _Resp(200, [row] if row else [])
        return _Resp(200, [])

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        t = self._table(url)
        self.writes.append(("post", t, json))
        if t == "echo_gym_settings":
            for r in json or []:
                uuid = r.get("gym_id")
                cur = dict(self.settings.get(uuid) or {})
                cur.update({k: v for k, v in r.items() if k != "gym_id"})
                self.settings[uuid] = cur
        if t == "echo_social_connections":
            # UPSERT (Prefer: resolution=merge-duplicates on gym_id,platform).
            body = json if isinstance(json, dict) else (json or [{}])[0]
            unknown = set(body) - _ESC_COLS
            if unknown:
                col = sorted(unknown)[0]
                return _Resp(400, {"message": f"column \"{col}\" of relation "
                                              f"\"echo_social_connections\" does not exist"})
            uuid = body.get("gym_id")
            plat = body.get("platform")
            row = dict(self.conns.get((uuid, plat)) or {})
            # merge-duplicates only SETs the columns present in the payload.
            row.update({k: v for k, v in body.items()})
            self.conns[(uuid, plat)] = row
            return _Resp(201, [row])
        return _Resp(201, json or [])

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        t = self._table(url)
        self.writes.append(("patch", t, params, json))
        if t == "echo_social_connections":
            unknown = set(json or {}) - _ESC_COLS
            if unknown:
                col = sorted(unknown)[0]
                return _Resp(400, {"message": f"column \"{col}\" of relation "
                                              f"\"echo_social_connections\" does not exist"})
            uuid = (params.get("gym_id") or "").replace("eq.", "")
            plat = (params.get("platform") or "").replace("eq.", "")
            row = dict(self.conns.get((uuid, plat)) or {"gym_id": uuid, "platform": plat})
            row.update(json or {})
            self.conns[(uuid, plat)] = row
            return _Resp(200, [row])
        return _Resp(200, [])


@pytest.fixture
def shared_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-secret")
    yield


def test_shared_plane_id_is_read_by_resolver(shared_env, monkeypatch):
    http = _RoutingHttp(gyms={"gritx": "uuid-gritx"},
                        settings={"uuid-gritx": {"zernio_profile_id": GRITX_PROFILE}})
    # Inject a real store instance carrying the fake http, via the resolver's _shared_store.
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-secret", http=http)
    monkeypatch.setattr(zr, "_shared_store", lambda: store)

    fake = FakeZernio(
        profiles=[],  # NOT findable by name — proves the shared plane alone resolved it
        accounts={"accounts": [{"platform": "facebook", "_id": "fb1",
                                "displayName": "Gritx Gym"}]},
    )
    status, body = zr.handle_social_status("gritx", client=fake)
    assert status == 200
    assert body["platforms"]["facebook"]["connected"] is True
    assert ("list_accounts", GRITX_PROFILE) in fake.calls


def test_persist_profile_id_dual_writes_local_and_shared(shared_env, monkeypatch):
    http = _RoutingHttp(gyms={"gritx": "uuid-gritx"})
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-secret", http=http)
    monkeypatch.setattr(zr, "_shared_store", lambda: store)
    zr._persist_profile_id("gritx", GRITX_PROFILE, fb_page_id="pg1")
    # local
    assert (_db.gym_get("gritx") or {}).get("zernio_profile_id") == GRITX_PROFILE
    # shared
    assert http.settings["uuid-gritx"]["zernio_profile_id"] == GRITX_PROFILE
    assert http.settings["uuid-gritx"]["zernio_default_fb_page_id"] == "pg1"


# ===========================================================================
# 3. null IG/FB username keeps a live connection connected
# ===========================================================================
def test_null_username_keeps_ig_fb_connected(local_only_env):
    _db.gym_upsert("gritx", display_name="Gritx", zernio_profile_id=GRITX_PROFILE)
    # A live IG row with NO username (list momentarily omits profileData). Must stay connected.
    fake = FakeZernio(accounts={"accounts": [{"platform": "instagram", "_id": "ig9"}]})
    status, body = zr.handle_social_status("gritx", client=fake)
    assert body["platforms"]["instagram"]["connected"] is True
    assert body["platforms"]["instagram"]["handle"]  # non-null (falls back to the id)


# ===========================================================================
# 4. a dead/expired token reads not_connected/expired, never connected
# ===========================================================================
def test_dead_token_reads_expired_not_connected():
    for acct in (
        {"platform": "instagram", "_id": "a", "tokenExpired": True},
        {"platform": "instagram", "_id": "a", "needsReconnect": True},
        {"platform": "facebook", "_id": "a", "status": "expired"},
        {"platform": "facebook", "_id": "a", "connectionStatus": "revoked"},
        {"platform": "instagram", "_id": "a", "expires_at": "2000-01-01T00:00:00Z"},
    ):
        assert z.account_state(acct) == "expired", acct
    out = z.map_status({"accounts": [{"platform": "facebook", "_id": "f",
                                      "tokenExpired": True}]})
    assert out["platforms"]["facebook"]["connected"] is False
    assert out["platforms"]["facebook"]["expired"] is True


def test_a_bare_live_row_still_reads_connected_no_flap():
    # The anti-flap guarantee: absence of expiry fields is NOT expired.
    assert z.account_state({"platform": "instagram", "_id": "a"}) == "connected"


# ===========================================================================
# 5. re-verify sweep overwrites a poisoned row + repairs the durable connect signal
# ===========================================================================
def test_reverify_overwrites_poisoned_row_and_repairs_first_connected_at(shared_env, monkeypatch):
    # Poisoned cache: FB marked not_connected by the bad cron, no first_connected_at.
    http = _RoutingHttp(
        gyms={"gritx": "uuid-gritx"},
        settings={"uuid-gritx": {"zernio_profile_id": GRITX_PROFILE}},
        conns={("uuid-gritx", "facebook"): {"gym_id": "uuid-gritx", "platform": "facebook",
                                            "state": "not_connected",
                                            "first_connected_at": None}},
    )
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-secret", http=http)
    monkeypatch.setattr(zr, "_shared_store", lambda: store)
    # Zernio truth: FB is live.
    fake = FakeZernio(accounts={"accounts": [{"platform": "facebook", "_id": "fb1",
                                              "displayName": "Gritx Gym"}]})
    out = rv.reverify_gym("gritx", client=fake, store=store)
    assert out["ok"] is True
    row = http.conns[("uuid-gritx", "facebook")]
    assert row["state"] == "connected"
    # The durable "was connected" signal is first_connected_at (this schema has NO
    # ever_connected column); a genuinely-connected platform gets it stamped.
    assert row.get("first_connected_at")  # truthy timestamp, repaired
    assert row.get("last_verified_at")    # verification bumped
    assert row["handle"] == "Gritx Gym"


def test_reverify_inserts_row_for_unseeded_connected_gym(shared_env, monkeypatch):
    # topfuel gap: gym exists, NO echo_social_connections row seeded, but Zernio says
    # connected. A PATCH-only writer no-oped and left the gym reading not_connected;
    # the upsert must INSERT a connected row with first_connected_at.
    http = _RoutingHttp(
        gyms={"topfuel": "uuid-topfuel"},
        settings={"uuid-topfuel": {"zernio_profile_id": GRITX_PROFILE}},
        conns={},  # nothing seeded
    )
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-secret", http=http)
    monkeypatch.setattr(zr, "_shared_store", lambda: store)
    fake = FakeZernio(accounts={"accounts": [{"platform": "instagram", "_id": "ig1",
                                              "displayName": "Top Fuel"}]})
    out = rv.reverify_gym("topfuel", client=fake, store=store)
    assert out["ok"] is True
    row = http.conns[("uuid-topfuel", "instagram")]
    assert row["state"] == "connected"
    assert row["handle"] == "Top Fuel"
    assert row.get("first_connected_at")  # stamped on the fresh insert


def test_reverify_preserves_original_first_connected_at(shared_env, monkeypatch):
    # An existing first_connected_at must never be overwritten by a re-verify.
    http = _RoutingHttp(
        gyms={"gritx": "uuid-gritx"},
        settings={"uuid-gritx": {"zernio_profile_id": GRITX_PROFILE}},
        conns={("uuid-gritx", "facebook"): {"gym_id": "uuid-gritx", "platform": "facebook",
                                            "state": "connected",
                                            "first_connected_at": "2026-01-01T00:00:00+00:00"}},
    )
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-secret", http=http)
    monkeypatch.setattr(zr, "_shared_store", lambda: store)
    fake = FakeZernio(accounts={"accounts": [{"platform": "facebook", "_id": "fb1",
                                              "displayName": "Gritx Gym"}]})
    rv.reverify_gym("gritx", client=fake, store=store)
    row = http.conns[("uuid-gritx", "facebook")]
    assert row["first_connected_at"] == "2026-01-01T00:00:00+00:00"  # untouched


def test_reverify_writes_not_connected_but_does_not_stamp_first_connected_when_unresolved(
        shared_env, monkeypatch):
    http = _RoutingHttp(gyms={"gritx": "uuid-gritx"})  # no settings row, no profile
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-secret", http=http)
    monkeypatch.setattr(zr, "_shared_store", lambda: store)
    fake = FakeZernio(profiles=[])  # unfindable
    out = rv.reverify_gym("gritx", client=fake, store=store)
    assert out["ok"] is True
    row = http.conns[("uuid-gritx", "facebook")]
    assert row["state"] == "not_connected"
    # first_connected_at must NOT be forced when we have no positive signal.
    assert not row.get("first_connected_at")
