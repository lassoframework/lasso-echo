"""Google Business must be able to READ as connected and to be DISCONNECTED.

Two defects made Google the worst of the three platforms:

1. `_handle_of` read only `profileData.username` / `displayName`. A Google Business
   listing has no username, so the handle came back None, and the portal's phantom
   filter downgrades any connected row with a null handle to "not connected". A real,
   working Google connection therefore rendered as "Not connected yet" forever and the
   owner reconnected on a loop. This is THE reason Google never showed as connected.

2. `handle_social_disconnect` validated against PLATFORMS (the POSTING set, IG/FB only),
   so every Google disconnect 400'd — while the portal rendered the button and reported
   success. An owner who linked the wrong listing could never unlink it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio as z            # noqa: E402
from agent import zernio_routes as zr    # noqa: E402


def _gbp(**over):
    acct = {"platform": "googlebusiness", "_id": "acct_gbp"}
    acct.update(over)
    return acct


# ---- 1. the handle -------------------------------------------------------------

@pytest.mark.parametrize("meta_key", ["selectedLocationName", "locationName",
                                      "location_name", "title"])
def test_gbp_handle_reads_every_observed_location_name_spelling(meta_key):
    acct = _gbp(metadata={meta_key: "Hill Country MVMT"})
    assert z._handle_of(acct) == "Hill Country MVMT"


def test_gbp_handle_falls_back_to_displayName_then_to_the_location_id():
    assert z._handle_of(_gbp(displayName="Hill Country MVMT")) == "Hill Country MVMT"
    # An id is real data we hold. A name we do not have is never invented.
    assert z._handle_of(_gbp(metadata={"selectedLocationId": "locations/123"})) \
        == "locations/123"


def test_a_bare_gbp_account_still_reports_connected_even_with_no_handle():
    out = z.map_status({"accounts": [_gbp()]})
    gbp = out["platforms"]["googlebusiness"]
    assert gbp["connected"] is True
    assert gbp["handle"] is None      # honest; the portal no longer downgrades on this


def test_map_status_surfaces_the_gbp_listing_name_as_the_handle():
    out = z.map_status({"accounts": [_gbp(metadata={"locationName": "Zanshin Fitness"})]})
    assert out["platforms"]["googlebusiness"] == {
        "connected": True, "handle": "Zanshin Fitness", "expired": False}


def test_instagram_and_facebook_handles_are_unchanged():
    ig = {"platform": "instagram", "_id": "a1",
          "metadata": {"profileData": {"username": "crossfit_zanshin"}}}
    fb = {"platform": "facebook", "_id": "a2", "displayName": "CrossFit Zanshin"}
    assert z._handle_of(ig) == "crossfit_zanshin"
    assert z._handle_of(fb) == "CrossFit Zanshin"
    assert z._handle_of({"platform": "instagram", "_id": "a3"}) is None


# ---- 2. disconnect -------------------------------------------------------------

class _FakeDisc:
    def __init__(self):
        self.deleted = []

    def list_accounts(self, pid):
        return {"accounts": [_gbp(), {"platform": "instagram", "_id": "acct_ig"}]}

    def disconnect_account(self, account_id):
        self.deleted.append(account_id)
        return {"ok": True}

    def find_profile_id(self, name):
        return "prof_1"


def test_google_business_disconnect_is_accepted_not_400(monkeypatch, tmp_path):
    monkeypatch.setenv("ZERNIO_API_KEY", "k")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "e.db"))
    fake = _FakeDisc()
    status, body = zr.handle_social_disconnect("gymA", "googlebusiness", client=fake)
    assert status == 200, body
    assert "acct_gbp" in fake.deleted, "the Google account row must actually be deleted"


def test_an_unsupported_platform_is_still_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("ZERNIO_API_KEY", "k")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "e.db"))
    status, body = zr.handle_social_disconnect("gymA", "tiktok", client=_FakeDisc())
    assert status == 400 and "platform must be one of" in body["error"]
