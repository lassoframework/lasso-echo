"""
Zernio DISCONNECT: a gym owner who connected the WRONG account (e.g. a personal or a
spouse's Instagram) can remove it and reconnect. Fully OFFLINE.

Asserts:
  - the Zernio primitive hits DELETE /v1/accounts/{id};
  - the route finds the platform's connected account, deletes it, and clears the
    portal snapshot so the LASSO dashboard updates;
  - FB disconnect also forgets the stored page binding;
  - idempotent: nothing connected -> {ok, disconnected:0}, no delete call;
  - gated OFF when Zernio is not configured; bad platform -> 400;
  - token isolation: everything keys off the passed account_key.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio, zernio_routes as zr, config  # noqa: E402


class FakeHTTP:
    def __init__(self, delete_status=200):
        self.deleted = []
        self._ds = delete_status

    def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url)

        class R:
            status_code = self._ds

            def json(_self):
                return {}
        R._ds = self._ds
        r = R()
        r.status_code = self._ds
        return r


def test_disconnect_account_hits_delete_endpoint():
    http = FakeHTTP()
    c = zernio.ZernioClient(api_key="k", base="https://api.zernio.com", http=http)
    c.disconnect_account("acct_123")
    assert http.deleted == ["https://api.zernio.com/v1/accounts/acct_123"]


class FakeClient:
    def __init__(self, accounts):
        self._accounts = accounts
        self.disconnected = []

    def find_profile_id(self, name):
        return "prof_gritx"

    def list_accounts(self, pid):
        self._pid = pid
        return self._accounts

    def disconnect_account(self, acct_id):
        self.disconnected.append(acct_id)
        return {}


def _arm(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "test-key")


def test_disconnect_removes_wrong_ig_and_clears_snapshot(monkeypatch):
    _arm(monkeypatch)
    # resolve profile from the stored id
    monkeypatch.setattr(zr, "_resolve_profile_id", lambda k: "prof_gritx")
    accounts = {"accounts": [{"_id": "wrongIG", "platform": "instagram"}]}
    client = FakeClient(accounts)
    cleared = []
    status, body = zr.handle_social_disconnect(
        "gritx_ig", "instagram", client=client,
        snapshot_clear=lambda ak, plat: cleared.append((ak, plat)))
    assert status == 200 and body["disconnected"] == 1
    assert client.disconnected == ["wrongIG"]              # the wrong IG removed
    assert cleared == [("gritx_ig", "instagram")]          # dashboard snapshot cleared


def test_disconnect_idempotent_when_nothing_connected(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(zr, "_resolve_profile_id", lambda k: "prof_gritx")
    client = FakeClient({"accounts": []})
    status, body = zr.handle_social_disconnect(
        "gritx_ig", "instagram", client=client, snapshot_clear=lambda a, p: None)
    assert status == 200 and body["disconnected"] == 0
    assert client.disconnected == []                        # no delete call


def test_disconnect_fb_forgets_page_binding(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(zr, "_resolve_profile_id", lambda k: "prof")
    from agent import db as _db
    saved = {}
    monkeypatch.setattr(_db, "gym_get", lambda k: {"display_name": "GritX",
                                                   "zernio_default_fb_page_id": "PAGE1"})
    monkeypatch.setattr(_db, "gym_upsert",
                        lambda k, **kw: saved.update(kw))
    client = FakeClient({"accounts": [{"_id": "fbAcct", "platform": "facebook"}]})
    status, body = zr.handle_social_disconnect(
        "gritx_fb", "facebook", client=client, snapshot_clear=lambda a, p: None)
    assert body["disconnected"] == 1 and client.disconnected == ["fbAcct"]
    assert saved.get("zernio_default_fb_page_id") == ""     # page binding forgotten


def test_disconnect_bad_platform_400(monkeypatch):
    _arm(monkeypatch)
    status, body = zr.handle_social_disconnect("gritx_ig", "tiktok",
                                               client=FakeClient({"accounts": []}))
    assert status == 400


def test_disconnect_gated_off_when_zernio_unconfigured(monkeypatch):
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    status, body = zr.handle_social_disconnect("gritx_ig", "instagram")
    assert status == 403


def test_disconnect_snapshot_clear_failure_never_blocks(monkeypatch):
    """The Zernio removal succeeded; a snapshot-clear failure must not 500 the route."""
    _arm(monkeypatch)
    monkeypatch.setattr(zr, "_resolve_profile_id", lambda k: "prof")
    client = FakeClient({"accounts": [{"_id": "ig1", "platform": "instagram"}]})

    def boom(ak, plat):
        raise RuntimeError("supabase down")

    status, body = zr.handle_social_disconnect("gritx_ig", "instagram",
                                               client=client, snapshot_clear=boom)
    assert status == 200 and body["disconnected"] == 1     # removal still reported ok
