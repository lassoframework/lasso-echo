"""
GBP connection sync (gbp_conn_sync): populate gym_gbp_connections from the LIVE Zernio
connection so the publish lane can route. Fully OFFLINE via injected store + zernio fakes.

Asserts:
  * flag OFF -> no-op, store untouched
  * a connected Google account -> a 'connected' connection row with the location id +
    a tz inferred from the address (FL -> America/New_York), written only on INSERT
  * an inactive Zernio account -> 'needs_reconnect'
  * no Google account but an existing connected row -> flipped to needs_reconnect
  * no selectedLocationId -> skipped (cannot route)
  * an existing row's timezone is PRESERVED across a re-sync (never clobbered)
  * NOTHING here publishes
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_conn_sync as gcs  # noqa: E402


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_CONN_SYNC", "true")
    yield


class FakeStore:
    def __init__(self, existing=None):
        self.rows = [dict(r) for r in (existing or [])]
        self.upserts = []

    def available(self):
        return True

    def connections_for(self, key):
        return [dict(r) for r in self.rows if r.get("portal_gym_key") == key]

    def upsert_connection(self, conn):
        self.upserts.append(dict(conn))
        for r in self.rows:
            if (r.get("portal_gym_key") == conn.get("portal_gym_key")
                    and r.get("gbp_location_id") == conn.get("gbp_location_id")):
                for k, v in conn.items():
                    if k == "timezone":          # preserve an existing tz (mirror the store)
                        continue
                    r[k] = v
                return r
        r = dict(conn)
        self.rows.append(r)
        return r


class FakeZernio:
    def __init__(self, profiles, accounts):
        self._p = profiles          # name -> profile_id
        self._a = accounts          # profile_id -> accounts list

    def find_profile_id(self, name):
        return self._p.get(name)

    def list_accounts(self, pid):
        return self._a.get(pid, [])


def _gbp_acct(loc="15450740315559491026", active=True, name="CrossFit ENG",
              addr="326 Southwest 2nd Terrace, Cape Coral, Florida"):
    return {"_id": "zacct_eng", "platform": "googlebusiness", "isActive": active,
            "metadata": {"selectedLocationId": loc, "selectedLocationName": name,
                         "locationAddress": addr,
                         "connectedAt": "2026-08-19T18:53:19.828Z"}}


def test_flag_off_touches_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_CONN_SYNC", "false")
    store = FakeStore()
    z = FakeZernio({"eng": "pid_eng"}, {"pid_eng": [_gbp_acct()]})
    out = gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert out["ok"] is False
    assert store.upserts == []


def test_connected_account_upserts_routable_row_with_inferred_tz():
    store = FakeStore()
    z = FakeZernio({"eng": "pid_eng"}, {"pid_eng": [_gbp_acct()]})
    out = gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert out["ok"] and out["connected"] == 1
    row = store.rows[0]
    assert row["portal_gym_key"] == "eng"
    assert row["gbp_location_id"] == "15450740315559491026"
    assert row["zernio_account_id"] == "zacct_eng"
    assert row["status"] == "connected"
    assert row["timezone"] == "America/New_York"   # inferred from 'Florida'
    assert row["location_name"] == "CrossFit ENG"


def test_inactive_account_is_needs_reconnect():
    store = FakeStore()
    z = FakeZernio({"eng": "pid_eng"}, {"pid_eng": [_gbp_acct(active=False)]})
    out = gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert out["needs_reconnect"] == 1
    assert store.rows[0]["status"] == "needs_reconnect"


def test_no_google_account_flips_existing_connected_to_needs_reconnect():
    store = FakeStore(existing=[{
        "portal_gym_key": "eng", "gbp_location_id": "15450740315559491026",
        "zernio_profile_id": "pid_eng", "zernio_account_id": "zacct_eng",
        "status": "connected", "timezone": "America/New_York"}])
    # profile exists but no googlebusiness account under it anymore
    z = FakeZernio({"eng": "pid_eng"}, {"pid_eng": []})
    out = gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert out["needs_reconnect"] == 1
    assert store.rows[0]["status"] == "needs_reconnect"


def test_no_selected_location_is_skipped():
    store = FakeStore()
    z = FakeZernio({"eng": "pid_eng"}, {"pid_eng": [_gbp_acct(loc="")]})
    out = gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert out["skipped"] == 1
    assert store.rows == []          # nothing routable was written


def test_existing_timezone_is_preserved_on_resync():
    # A human corrected the tz to Chicago; a re-sync must NOT clobber it back to the
    # address-inferred value.
    store = FakeStore(existing=[{
        "portal_gym_key": "eng", "gbp_location_id": "15450740315559491026",
        "zernio_profile_id": "pid_eng", "zernio_account_id": "zacct_eng",
        "status": "connected", "timezone": "America/Chicago"}])
    z = FakeZernio({"eng": "pid_eng"}, {"pid_eng": [_gbp_acct()]})
    gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert store.rows[0]["timezone"] == "America/Chicago"   # preserved, not overwritten


def test_tz_inference_precise_no_token_collision():
    # full state name -> correct tz
    assert gcs._tz_from_address("326 SW 2nd Terrace, Cape Coral, Florida") == "America/New_York"
    # 2-letter code as the state segment (with ZIP) -> correct tz
    assert gcs._tz_from_address("100 Main St, Phoenix, AZ 85016") == "America/Phoenix"
    # a street/city word that collides with a state code must NOT be read as a state:
    # 'Oregon' spelled would be OR, but here only a colliding token appears in the street.
    assert gcs._tz_from_address("5 Indiana Ave, Chicago, Illinois") == "America/Chicago"
    # bare colliding token in a street name, no real state segment -> None (default+flag)
    assert gcs._tz_from_address("42 OR Lane Suite IN") is None
    # unknown / non-US -> None
    assert gcs._tz_from_address("10 Queen St, Kitchener, Ontario") is None
    assert gcs._tz_from_address("") is None


# ---- multi-word state names (2026-09-02, Swift River CrossFit) --------------------------
# "64 Hobbs Street #3, Conway, New Hampshire" came back unparseable in production. Root
# cause was two bugs, both real: New Hampshire was simply absent from _STATE_NAMES, and
# separately the word-matching split each address segment into individual words BEFORE
# comparing, so a two-word dict key ("NEWHAMPSHIRE", "NEWYORK", ...) could never match
# ANY address, regardless of whether the name was in the dict. These pin both fixes.

def test_tz_multi_word_state_names_now_resolve():
    # the exact production address that surfaced this.
    assert gcs._tz_from_address(
        "64 Hobbs Street #3, Conway, New Hampshire") == "America/New_York"
    assert gcs._tz_from_address("1 Main St, Buffalo, New York") == "America/New_York"
    assert gcs._tz_from_address("1 Main St, Newark, New Jersey") == "America/New_York"
    assert gcs._tz_from_address("1 Main St, Providence, Rhode Island") == "America/New_York"
    assert gcs._tz_from_address("1 Main St, Fargo, North Dakota") == "America/Chicago"
    assert gcs._tz_from_address("1 Main St, Sioux Falls, South Dakota") == "America/Chicago"
    assert gcs._tz_from_address("1 Main St, Charleston, West Virginia") == "America/New_York"
    # still no false positives from an unrelated two-word street/city name.
    assert gcs._tz_from_address("1 New Street, Anytown, Ontario") is None


def test_no_profile_is_skipped():
    store = FakeStore()
    z = FakeZernio({}, {})           # no profile for eng
    out = gcs.sync_gbp_connections(store=store, zernio=z, clients=["eng"])
    assert out["skipped"] == 1
    assert store.upserts == []


# ---- tz inference is SELF-RUNNING (Blake 2026-08-31: no VERIFY tasks) --------------

def test_tz_split_zone_states_use_dominant_zone():
    from agent.gbp_conn_sync import _tz_from_address
    assert _tz_from_address(
        "150 Russell Lane Building 4, Dripping Springs, TX") == "America/Chicago"
    assert _tz_from_address("123 Main St, Nashville, TN 37201") == "America/Chicago"
    assert _tz_from_address("9 Elm, Louisville, KY 40202") == "America/New_York"
    assert _tz_from_address("1 A St, Indianapolis, IN 46204") == \
        "America/Indiana/Indianapolis"


def test_tz_off_zone_metros_are_corrected():
    from agent.gbp_conn_sync import _tz_from_address
    assert _tz_from_address("500 N Mesa, El Paso, TX 79901") == "America/Denver"
    assert _tz_from_address("12 Beach Rd, Pensacola, FL") == "America/Chicago"
    assert _tz_from_address("77 Gay St, Knoxville, TN") == "America/New_York"
